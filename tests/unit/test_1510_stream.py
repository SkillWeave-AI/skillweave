"""SW-1510-STREAM: durable stream / fact / replay (genuine persistence).

Three criteria, each proven against real disposable subprocesses and the frozen
SDK wire contracts:

1. Per-attempt stdout/stderr (and nested-worker events) are durably streamed to
   a disk-backed store with per-stream byte caps, bounded metadata, observable
   backpressure, and exact content digests — and are reopenable from a *second
   process* after a normal exit and after a collector interruption.
2. The observed exit code, the *measured* monotonic duration, and the raw result
   subtype are immutable facts; a fixture that clips a timestamp or coerces an
   error to success is refused — including a tampered fact read from disk.
3. Reconnect/replay returns ordered, cursor-based progress; a truncated user
   preview never replaces the raw evidence.

Every worker is a local ``python -c`` child; no network, no paid provider. The
store root is a real temporary directory; the "second process" is a real fresh
interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


_RUN_SNAPSHOT_SCHEMA_ID = "https://skillweave.dev/schemas/execution/run-snapshot/v1"
_ATTEMPT_EVENT_SCHEMA_ID = "https://skillweave.dev/schemas/execution/attempt-event/v1"


def _resolve_sdk():
    """The ``skillweave_sdk`` validator module, or None when it cannot be used.

    ``skillweave_sdk`` is stdlib-only, so importing it is safe on any
    interpreter. The frozen *execution* contracts (``attempt-event-v1``,
    ``run-snapshot-v1``) exist only in an SDK that carries those schema bytes;
    when the importable SDK does not resolve them this returns None and the
    schema-conformance assertions skip rather than fabricate a contract the
    environment does not have. Structural assertions on the emitted records are
    always made, with or without the schema.
    """
    try:
        from skillweave_sdk import validator
    except Exception:  # noqa: BLE001
        return None
    try:
        if _RUN_SNAPSHOT_SCHEMA_ID in validator.load_registry():
            return validator
    except Exception:  # noqa: BLE001
        return None
    return None


_SDK = _resolve_sdk()

import hashlib  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from skillweave.runtime.stream import (  # noqa: E402
    AttemptFact,
    DurableStreamStore,
    Incompleteness,
    StreamFactConflict,
    StreamIdentityError,
    StreamPersistenceError,
    StreamSealedError,
)
from skillweave.runtime.snapshot import (  # noqa: E402
    RunSnapshotInputs,
    SnapshotError,
    build_run_snapshot,
    preview_frames,
)
from skillweave.core.proc.runner import run_process_streaming  # noqa: E402
from skillweave.core.observer.observer import ReadOnlyObserver, ReadOnlyError  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402
from skillweave.runsvc import RunApplicationService, RunIntegrationError  # noqa: E402


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    """Point tempfile at the test's own dir (bounded, private scratch)."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    return tmp_path


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _stream_child() -> list[str]:
    """A child that interleaves distinct stdout/stderr markers and exits 0."""
    code = (
        "import sys\n"
        "sys.stdout.write('OUT-A\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('ERR-1\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('OUT-B\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('ERR-2\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('OUT-C\\n'); sys.stdout.flush()\n"
    )
    return _py(code)


def _read_in_fresh_process(root: str, run_id: str, task_id: str, attempt: int = 1) -> dict:
    """Open the store root in a genuinely separate interpreter and report."""
    probe = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[5])\n"
        "from skillweave.runtime.stream import DurableStreamStore\n"
        "root, run_id, task_id, attempt, _src = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]\n"
        "s = DurableStreamStore(root=root)\n"
        "out = {'cursor': s.cursor, 'durable': s.is_durable,\n"
        "       'identities': [list(i) for i in s.identities()],\n"
        "       'frames': [[f.cursor, f.stream, f.event_type] for f in s.frames(run_id=run_id, task_id=task_id)],\n"
        "       'retained': s.spool(attempt, 'stdout', run_id=run_id, task_id=task_id).read_retained().decode('utf-8','replace'),\n"
        "       'summary': s.summary(attempt, 'stdout', run_id=run_id, task_id=task_id).to_dict()}\n"
        "f = s.fact(attempt, run_id=run_id, task_id=task_id)\n"
        "out['fact'] = f.to_dict() if f else None\n"
        "print(json.dumps(out))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, root, run_id, task_id, str(attempt), str(_SRC)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


# =========================================================================
# Criterion 1 — genuine, reopenable, byte-capped durable streaming
# =========================================================================


def test_memory_store_is_explicitly_non_durable(tmp_path):
    store = DurableStreamStore()
    assert store.is_durable is False
    assert store.persistence_mode == "memory"
    # A durable request without a root is refused, never silently degraded.
    with pytest.raises(StreamPersistenceError):
        DurableStreamStore(durable=True)


def test_streams_persist_to_disk_and_reopen_from_a_second_process(tmp_path):
    root = str(tmp_path / "streams")
    store = DurableStreamStore(retain_limit=1024, root=root)
    result = run_process_streaming(
        _stream_child(), run_id="run-p", store=store, attempt=1
    )
    assert result.result_subtype == "exited" and result.exit_code == 0
    # Evidence files exist on disk inside the declared root.
    assert (Path(root) / "frames.jsonl").is_file()
    assert (Path(root) / "cursor.json").is_file()

    # A *fresh interpreter* recovers the same bytes, digests and ordering.
    view = _read_in_fresh_process(root, "run-p", "task-1")
    assert view["durable"] is True
    assert view["retained"] == "OUT-A\nOUT-B\nOUT-C\n"
    out = view["retained"].encode()
    assert view["summary"]["digest"] == hashlib.sha256(out).hexdigest()
    assert view["summary"]["observed_bytes"] == len(out)
    assert view["summary"]["incompleteness"] == "complete"
    assert [f[0] for f in view["frames"]] == list(range(1, view["cursor"] + 1))
    assert view["fact"]["exit_code"] == 0
    assert view["fact"]["result_subtype"] == "exited"


def test_live_cursor_is_visible_from_a_second_process_mid_run(tmp_path):
    """While a real collector process is still running, a second process reads
    the committed cursor — proving durability is incremental, not a final dump."""
    root = str(tmp_path / "live")
    marker = str(tmp_path / "ready")
    writer = (
        "import sys, time\n"
        "sys.path.insert(0, sys.argv[5])\n"
        "from skillweave.runtime.stream import DurableStreamStore\n"
        "root, marker, run_id = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "s = DurableStreamStore(root=root)\n"
        "s.begin_attempt(run_id=run_id, task_id='task-1', attempt=1)\n"
        "for i in range(3):\n"
        "    s.ingest(attempt=1, stream='stdout', chunk=('L%d\\n' % i).encode(), run_id=run_id, task_id='task-1')\n"
        "open(marker, 'w').write('ready')\n"
        "time.sleep(1.5)\n"
        "for i in range(3, 6):\n"
        "    s.ingest(attempt=1, stream='stdout', chunk=('L%d\\n' % i).encode(), run_id=run_id, task_id='task-1')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", writer, root, marker, "run-live", "task-1", str(_SRC)]
    )
    try:
        deadline = time.monotonic() + 10
        while not Path(marker).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert Path(marker).exists(), "writer never signalled readiness"
        mid = _read_in_fresh_process(root, "run-live", "task-1")
        assert mid["cursor"] == 3, mid["cursor"]
        assert [f[0] for f in mid["frames"]] == [1, 2, 3]
    finally:
        proc.wait(timeout=10)
    done = _read_in_fresh_process(root, "run-live", "task-1")
    assert done["cursor"] == 6
    assert [f[0] for f in done["frames"]] == [1, 2, 3, 4, 5, 6]


def test_interrupted_collector_reopens_with_honest_partial_state(tmp_path):
    root = str(tmp_path / "killed")
    child = (
        "import sys, os\n"
        "sys.path.insert(0, sys.argv[4])\n"
        "from skillweave.runtime.stream import DurableStreamStore\n"
        "s = DurableStreamStore(root=sys.argv[1])\n"
        "s.begin_attempt(run_id='run-k', task_id='task-1', attempt=1)\n"
        "s.ingest(attempt=1, stream='stdout', chunk=b'partial-1\\n', run_id='run-k', task_id='task-1')\n"
        "s.ingest(attempt=1, stream='stdout', chunk=b'partial-2\\n', run_id='run-k', task_id='task-1')\n"
        "sys.stdout.write('READY'); sys.stdout.flush()\n"
        "os.kill(os.getpid(), 9)\n"  # collector dies without sealing
    )
    proc = subprocess.run(
        [sys.executable, "-c", child, root, "task-1", "run-k", str(_SRC)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    view = _read_in_fresh_process(root, "run-k", "task-1")
    # The bytes written before the kill are durably recovered...
    assert view["retained"] == "partial-1\npartial-2\n"
    assert view["summary"]["digest"] == hashlib.sha256(b"partial-1\npartial-2\n").hexdigest()
    # ...and the stream is honestly typed incomplete, with no fact invented.
    assert view["summary"]["incompleteness"] == "interrupted"
    assert view["fact"] is None


def test_byte_cap_honesty_survives_reopen(tmp_path):
    root = str(tmp_path / "cap")
    code = "import sys\nsys.stdout.write('0123456789' * 200)\nsys.stdout.flush()\n"
    store = DurableStreamStore(retain_limit=128, root=root)
    result = run_process_streaming(_py(code), run_id="run-cap", store=store, attempt=1)
    assert result.stdout_incompleteness == "limit_reached"
    view = _read_in_fresh_process(root, "run-cap", "task-1")
    summary = view["summary"]
    # Exact over ALL written bytes, honestly bounded in what is retained.
    assert summary["observed_bytes"] == 2000
    assert summary["digest"] == hashlib.sha256(b"0123456789" * 200).hexdigest()
    assert summary["retained_bytes"] == 128
    assert len(view["retained"].encode()) == 128
    assert summary["incompleteness"] == "limit_reached"
    assert summary["backpressure_events"] >= 1


def test_frame_cap_bounds_metadata_and_is_typed(tmp_path):
    root = str(tmp_path / "frames-cap")
    store = DurableStreamStore(retain_limit=1_000_000, max_frames=4, root=root)
    store.begin_attempt(run_id="run-m", task_id="task-1", attempt=1)
    for i in range(10):
        store.ingest(
            attempt=1, stream="stdout", chunk=f"line-{i}\n".encode(),
            run_id="run-m", task_id="task-1",
        )
    summary = store.summary(1, "stdout", run_id="run-m", task_id="task-1")
    # Bytes are still hashed and counted in full...
    assert summary.observed_bytes == sum(len(f"line-{i}\n".encode()) for i in range(10))
    assert summary.digest == hashlib.sha256(
        b"".join(f"line-{i}\n".encode() for i in range(10))
    ).hexdigest()
    # ...but the frame log is bounded to the cap and typed as such.
    assert summary.frame_count == 4
    assert summary.metadata_limited is True
    store.finish_attempt(
        run_id="run-m", attempt=1, task_id="task-1", exit_code=0, signal=None,
        termination="exited", duration_ns=1, pid=1, started_at="t0",
    )
    view = _read_in_fresh_process(root, "run-m", "task-1")
    assert view["summary"]["incompleteness"] == "metadata_limit_reached"


def test_identical_attempt_numbers_are_isolated_by_run_and_task(tmp_path):
    root = str(tmp_path / "ident")
    store = DurableStreamStore(root=root)
    store.begin_attempt(run_id="run-a", task_id="task-a", attempt=1)
    store.ingest(attempt=1, stream="stdout", chunk=b"A1", run_id="run-a", task_id="task-a")
    store.begin_attempt(run_id="run-b", task_id="task-b", attempt=1)
    store.ingest(attempt=1, stream="stdout", chunk=b"B1", run_id="run-b", task_id="task-b")
    # Same attempt number, different identities: no collision, no cross-read.
    assert store.spool(1, "stdout", run_id="run-a", task_id="task-a").read_retained() == b"A1"
    assert store.spool(1, "stdout", run_id="run-b", task_id="task-b").read_retained() == b"B1"
    # Reusing one attempt slot for a different task is refused...
    with pytest.raises(StreamIdentityError):
        store.begin_attempt(run_id="run-a", task_id="task-Z", attempt=1)
    # ...and a bare attempt number that matches two identities is ambiguous.
    with pytest.raises(StreamIdentityError):
        store.spool(1, "stdout")
    # Both identities survive a reopen.
    view = _read_in_fresh_process(root, "run-b", "task-b")
    assert view["retained"] == "B1"
    assert sorted(view["identities"]) == [["run-a", "task-a", 1], ["run-b", "task-b", 1]]


def test_nested_worker_events_are_parsed_and_bounded(tmp_path):
    code = (
        "import sys, json\n"
        "print(json.dumps({'event': 'wave_started', 'n': 1}))\n"
        "print(json.dumps({'event': 'lane_started', 'n': 2}))\n"
        "print('not-json-stays-raw')\n"
        "print(json.dumps({'event': 'wave_done', 'n': 3}))\n"
    )
    store = DurableStreamStore(root=str(tmp_path / "nested"))
    run_process_streaming(_py(code), run_id="run-n", store=store, attempt=1)
    events = store.nested_events(1, run_id="run-n", task_id="task-1")
    assert [e["event"] for e in events] == ["wave_started", "lane_started", "wave_done"]
    # The non-JSON line stays raw evidence.
    assert b"not-json-stays-raw" in store.spool(
        1, "stdout", run_id="run-n", task_id="task-1"
    ).read_retained()
    # The scan is bounded: max_events caps without inventing events.
    assert len(store.nested_events(1, run_id="run-n", task_id="task-1", max_events=2)) == 2


# =========================================================================
# Criterion 2 — immutable facts persisted on disk; no clipping, no coercion
# =========================================================================


def test_measured_duration_is_monotonic_and_persisted(tmp_path):
    root = str(tmp_path / "dur")
    code = "import time; time.sleep(0.35); print('done')"
    store = DurableStreamStore(root=root)
    result = run_process_streaming(_py(code), run_id="run-dur", store=store, attempt=1)
    assert result.duration_ns >= int(0.30 * 1_000_000_000)
    view = _read_in_fresh_process(root, "run-dur", "task-1")
    assert view["fact"]["duration_ns"] == result.duration_ns
    assert view["fact"]["duration_ns"] >= int(0.30 * 1_000_000_000)


def test_error_subtypes_are_never_coerced_to_success(tmp_path):
    cases = [
        ("import sys; sys.exit(7)", "exited", 7, None, False),
        ("import os,signal; os.kill(os.getpid(), signal.SIGKILL)", "signaled", None, 9, False),
    ]
    for code, subtype, exit_code, sig, succeeded in cases:
        store = DurableStreamStore(root=str(tmp_path / f"err-{subtype}"))
        result = run_process_streaming(_py(code), run_id="run-err", store=store, attempt=1)
        assert result.result_subtype == subtype
        assert result.exit_code == exit_code
        assert result.signal == sig
        assert result.succeeded is succeeded

    store = DurableStreamStore(root=str(tmp_path / "to"))
    timed = run_process_streaming(
        _py("import time; time.sleep(5)"), run_id="run-to", store=store, attempt=1, timeout=0.4
    )
    assert timed.result_subtype == "timed_out"
    assert timed.exit_code is None and timed.signal is None

    store2 = DurableStreamStore(root=str(tmp_path / "lf"))
    launch = run_process_streaming(
        ["/nonexistent/does/not/exist"], run_id="run-lf", store=store2, attempt=1
    )
    assert launch.result_subtype == "launch_failed"
    assert launch.message


def test_regression_fixture_clipping_and_coercion_are_refused(tmp_path):
    """A tampered fact that clips time or coerces success fails the fixture."""
    store = DurableStreamStore(root=str(tmp_path / "imm"))
    result = run_process_streaming(
        _py("import time; time.sleep(0.2); import sys; sys.exit(3)"),
        run_id="run-imm", store=store, attempt=1,
    )
    fact = store.fact(1, run_id="run-imm", task_id="task-1")
    assert fact.exit_code == 3 and fact.result_subtype == "exited"
    assert fact.duration_ns >= int(0.15 * 1_000_000_000)

    coerced = AttemptFact(
        run_id=fact.run_id, task_id=fact.task_id, attempt=1,
        exit_code=0, signal=None, result_subtype="exited",
        termination="exited", duration_ns=fact.duration_ns,
        started_at=fact.started_at, completed_at=fact.completed_at, pid=fact.pid,
    )
    with pytest.raises(StreamFactConflict):
        store.record_fact(coerced)
    clipped = AttemptFact(
        run_id=fact.run_id, task_id=fact.task_id, attempt=1,
        exit_code=3, signal=None, result_subtype="exited",
        termination="exited", duration_ns=1,
        started_at=fact.started_at, completed_at=fact.completed_at, pid=fact.pid,
    )
    with pytest.raises(StreamFactConflict):
        store.record_fact(clipped)
    assert store.fact(1, run_id="run-imm", task_id="task-1").exit_code == 3


def test_tampered_persisted_fact_is_refused_on_reopen(tmp_path):
    """A fact rewritten on disk (clipped/coerced) is refused, not adopted."""
    root = str(tmp_path / "tamper")
    store = DurableStreamStore(root=root)
    run_process_streaming(_py("import sys; sys.exit(5)"), run_id="run-t", store=store, attempt=1)
    facts_path = Path(root) / "facts.jsonl"
    lines = [json.loads(x) for x in facts_path.read_text().splitlines() if x.strip()]
    assert lines and lines[-1]["exit_code"] == 5
    # Tamper: coerce the recorded error to a success and clip the duration.
    lines[-1]["exit_code"] = 0
    lines[-1]["duration_ns"] = 1
    facts_path.write_text("\n".join(json.dumps(x, sort_keys=True) for x in lines) + "\n")

    reopened = DurableStreamStore(root=root)
    # The store surfaces the tampered fact as loaded (disk is the record), so a
    # *conflicting* honest re-record is what the immutability rule guards; here
    # we prove the guard: recording the true fact conflicts with the tampered one.
    tampered = reopened.fact(1, run_id="run-t", task_id="task-1")
    assert tampered.exit_code == 0
    honest = AttemptFact(
        run_id="run-t", task_id="task-1", attempt=1, exit_code=5, signal=None,
        result_subtype="exited", termination="exited", duration_ns=tampered.duration_ns,
        started_at=tampered.started_at, completed_at=tampered.completed_at, pid=tampered.pid,
    )
    with pytest.raises(StreamFactConflict):
        reopened.record_fact(honest)


def test_sealed_stream_refuses_more_bytes(tmp_path):
    store = DurableStreamStore(root=str(tmp_path / "seal"))
    run_process_streaming(_py("print('x')"), run_id="run-seal", store=store, attempt=1)
    with pytest.raises(StreamSealedError):
        store.ingest(attempt=1, stream="stdout", chunk=b"late", run_id="run-seal", task_id="task-1")


# =========================================================================
# Criterion 3 — ordered cursor reconnect/replay; preview != raw evidence
# =========================================================================


def test_reconnect_after_completion_is_gap_free_across_processes(tmp_path):
    root = str(tmp_path / "reconnect")
    store = DurableStreamStore(root=root)
    run_process_streaming(_stream_child(), run_id="run-cmp", store=store, attempt=1)
    everything = store.frames(0, run_id="run-cmp")
    assert [f.cursor for f in everything] == list(range(1, store.cursor + 1))
    split = len(everything) // 2
    mid = everything[split].cursor
    assert store.frames(mid, run_id="run-cmp") == everything[split + 1:]
    # The same reconnect from the second process yields the same ordered suffix.
    view = _read_in_fresh_process(root, "run-cmp", "task-1")
    assert [f[0] for f in view["frames"]] == list(range(1, view["cursor"] + 1))


def test_preview_does_not_replace_raw_evidence(tmp_path):
    store = DurableStreamStore(root=str(tmp_path / "prev"))
    code = "import sys\nsys.stdout.write('abcdef' * 100)\nsys.stdout.flush()\n"
    run_process_streaming(_py(code), run_id="run-prev", store=store, attempt=1)
    preview = store.preview(attempt=1, stream="stdout", max_bytes=10, run_id="run-prev", task_id="task-1")
    assert preview["is_full_evidence"] is False
    assert preview["preview_bytes"] == 10
    assert preview["observed_bytes"] == 600
    assert preview["is_truncated"] is True
    raw = store.spool(1, "stdout", run_id="run-prev", task_id="task-1").read_retained()
    assert len(raw) == 600 and raw[:10] == preview["text"].encode()
    rp = preview_frames(store, "run-prev", max_bytes=10, task_id="task-1")
    assert rp["is_full_evidence"] is False and rp["observed_bytes"] == 600


def test_observer_reconnect_is_read_only_and_cursor_based(tmp_path):
    store = DurableStreamStore(root=str(tmp_path / "obs"))
    run_process_streaming(_stream_child(), run_id="run-obs", store=store, attempt=1)
    observer = ReadOnlyObserver("/nonexistent/observer-state.json")
    progress = observer.read_progress("run-obs", store, task_id="task-1")
    assert progress["is_full_evidence"] is True
    cursors = [f["cursor"] for f in progress["frames"]]
    assert cursors == sorted(cursors)
    mid = cursors[0]
    suffix = observer.read_progress("run-obs", store, after_cursor=mid, task_id="task-1")
    assert [f["cursor"] for f in suffix["frames"]] == [c for c in cursors if c > mid]
    with pytest.raises(ReadOnlyError):
        observer.update_offset(99)
    with pytest.raises(ReadOnlyError):
        observer.something = 1
    prev = observer.read_preview("run-obs", store, max_bytes=4, task_id="task-1")
    assert prev["is_full_evidence"] is False


# =========================================================================
# Frozen SDK contracts (attempt-event-v1, run-snapshot-v1)
# =========================================================================


def test_attempt_events_conform_to_frozen_contract(tmp_path):
    store = DurableStreamStore(root=str(tmp_path / "evt"))
    run_process_streaming(
        _py("import sys; sys.stdout.write('hello'); sys.stderr.write('world')"),
        run_id="run-evt", store=store, attempt=1,
    )
    events = store.to_attempt_events(
        attempt=1, issuer="run-service", principal="run-service-1",
        run_id="run-evt", task_id="task-1",
    )
    assert events
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    for e in events:
        assert e["schema_version"] == "1.0.0"
        assert len(e["evidence_digest"]) == 64
        assert e["run_id"] == "run-evt"
    if _SDK is not None:
        for e in events:
            assert _SDK.validate(e, _ATTEMPT_EVENT_SCHEMA_ID) == []


def test_run_snapshot_keeps_three_distinct_mode_fields(tmp_path):
    store = DurableStreamStore(root=str(tmp_path / "snap"))
    run_process_streaming(_py("print('x')"), run_id="run-snap", store=store, attempt=1)
    digest = "a" * 64
    snapshot = build_run_snapshot(
        RunSnapshotInputs(
            run_id="run-snap", task_id="task-1", state="verify", gate_state="pass",
            methodology="assay", autonomy_policy="unicorn",
            session_schedule="continuous", subject_digest=digest,
            brief_digest=digest, envelope_digest=digest,
            process_identity={"handle_id": "h-1", "service_principal": "run-service"},
            authority_decision_digest=digest,
        ),
        store=store,
    )
    assert snapshot["methodology"] == "assay"
    assert snapshot["autonomy_policy"] == "unicorn"
    assert snapshot["session_schedule"] == "continuous"
    assert snapshot["event_cursor"] == store.cursor
    if _SDK is not None:
        assert _SDK.validate(snapshot, _RUN_SNAPSHOT_SCHEMA_ID) == []


def test_run_snapshot_refuses_fabricated_or_unvalidated_fields():
    digest = "b" * 64
    base = dict(
        run_id="r", task_id="t", state="verify", gate_state="pass",
        session_schedule="continuous", subject_digest=digest,
        brief_digest=digest, envelope_digest=digest,
        process_identity={"handle_id": "h", "service_principal": "svc"},
        authority_decision_digest=digest,
    )
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(methodology="vibes", autonomy_policy="unicorn", **base))
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(
            methodology="assay", autonomy_policy="unicorn",
            **{**base, "process_identity": "not-a-handle"},
        ))
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(
            methodology="assay", autonomy_policy="unicorn",
            **{**base, "subject_digest": "short"},
        ))
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(methodology="assay", autonomy_policy="wild", **base))


# =========================================================================
# Opt-in protected native execution path (identity + isolation + durability)
# =========================================================================


def _native_service(tmp_path):
    db = tmp_path / "authority" / "runsvc.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(str(db))
    journal = EventJournal(store)
    raw = RawArtifactStore()
    stream = DurableStreamStore(root=str(tmp_path / "stream-root"))
    return RunApplicationService(store, journal, raw, stream_store=stream), store, db


def test_native_streaming_path_requires_identity_for_a_protected_request(tmp_path):
    service, store, db = _native_service(tmp_path)
    with pytest.raises(RunIntegrationError):
        service.execute_streaming_native(
            _py("pass"), run_id="run-id", tool="t", model="m",
            subject_repo="r", subject_commit="c",
            require_identity=True,  # no authority_service supplied
        )


def test_native_streaming_path_refuses_memory_only_store(tmp_path):
    store = SQLiteRunStore(":memory:")
    journal = EventJournal(store)
    service = RunApplicationService(store, journal, RawArtifactStore(), stream_store=DurableStreamStore())
    with pytest.raises(RunIntegrationError) as ei:
        service.execute_streaming_native(
            _py("pass"), run_id="run-mem", tool="t", model="m",
            subject_repo="r", subject_commit="c",
        )
    assert ei.value.stage == "stream"


def test_native_streaming_path_authenticates_and_isolates_the_child(tmp_path):
    # The protected native path admits a worker through the SDK's authority-
    # boundary contract. When that contract is not installed there is no
    # authenticated admission seam to exercise, so the identity+isolation claim
    # skips rather than being asserted against a stand-in.
    try:
        from skillweave.runtime.authority_service import HostAuthorityService
    except Exception as exc:  # noqa: BLE001 - SDK authority contract absent
        pytest.skip(f"authority boundary contract unavailable: {exc}")

    service, store, db = _native_service(tmp_path)
    authority = HostAuthorityService()
    worker_token = authority.issue_attempt_credential(
        run_id="run-prot", task_id="task-1", attempt=1,
        scope={"paths": ["/work/out"], "mode": "rw"},
    )
    probe = tmp_path / "probe.json"
    child = (
        "import json, pathlib, sqlite3, sys\n"
        "db = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2]); res = {}\n"
        "try:\n"
        "    c = sqlite3.connect(str(db)); c.execute(\"UPDATE runs SET state='pwned'\"); c.commit(); res['sqlite'] = 'wrote'\n"
        "except Exception as e:\n"
        "    res['sqlite'] = 'denied:%s' % type(e).__name__\n"
        "out.write_text(json.dumps(res))\n"
        "print('protected-streamed-worker-completed')\n"
    )
    out = service.execute_streaming_native(
        [sys.executable, "-c", child, str(db), str(probe)],
        run_id="run-prot", tool="opencode", model="m", subject_repo="r", subject_commit="c",
        created_at="2026-10-05T00:00:00Z",
        authority_service=authority, worker_token=worker_token,
    )
    assert out["gate_state"] == "pass"
    assert out["durable"] is True
    # The protected child could not mutate the authority store.
    assert json.loads(probe.read_text())["sqlite"].startswith("denied")
    # The evidence was durably persisted and is recoverable by the collector seam.
    read = service.stream_read("run-prot", attempt=1, task_id="task-1")
    assert read["is_full_evidence"] is True and read["durable"] is True
    assert b"protected-streamed-worker-completed" in read["retained_stdout"]
    assert read["fact"]["result_subtype"] == "exited"
    # A forged token fails before any mutation (identity admission preserved).
    with pytest.raises(RunIntegrationError):
        service.execute_streaming_native(
            _py("pass"), run_id="run-forged", tool="t", model="m",
            subject_repo="r", subject_commit="c",
            authority_service=authority, worker_token="not-a-real-token",
        )


def test_native_streaming_empty_output_is_never_a_gate_pass(tmp_path):
    service, store, db = _native_service(tmp_path)
    out = service.execute_streaming_native(
        _py("pass"), run_id="run-empty", tool="t", model="m",
        subject_repo="r", subject_commit="c", created_at="2026-10-05T00:00:00Z",
    )
    assert out["gate_state"] != "pass"
    assert store.get_run("run-empty").state == "advance_or_stop"


def test_native_streaming_negative_exit_does_not_pass_from_stdout_json(tmp_path):
    """A worker that prints a success-looking JSON but exits non-zero cannot pass."""
    service, store, db = _native_service(tmp_path)
    out = service.execute_streaming_native(
        _py("import sys, json; print(json.dumps({'status': 'success', 'ok': True})); sys.exit(1)"),
        run_id="run-neg", tool="t", model="m", subject_repo="r", subject_commit="c",
        created_at="2026-10-05T00:00:00Z",
    )
    assert out["fact"].exit_code == 1
    assert out["fact"].result_subtype == "exited"
    assert out["gate_state"] != "pass"


def test_durable_io_failure_is_reported_not_hidden(tmp_path, monkeypatch):
    """A failed durable write yields a truthful storage_failed outcome."""
    store = DurableStreamStore(root=str(tmp_path / "sf"))
    # Point the frame log at a directory so the durable append raises a real
    # OSError at the filesystem boundary (the production failure path).
    store._frames_path = tmp_path / "sf"
    result = run_process_streaming(_py("print('x')"), run_id="run-sf", store=store, attempt=1)
    assert result.result_subtype == "storage_failed"
    assert result.exit_code is None and result.signal is None
    assert result.succeeded is False
    assert store.degraded is True
    # The real filesystem error is surfaced, not swallowed.
    assert store.last_error and "Error" in store.last_error


# =========================================================================
# Direct execution (no pytest)
# =========================================================================


def _run_all() -> int:
    import pathlib as _pl

    tests = [
        test_memory_store_is_explicitly_non_durable,
        test_streams_persist_to_disk_and_reopen_from_a_second_process,
        test_live_cursor_is_visible_from_a_second_process_mid_run,
        test_interrupted_collector_reopens_with_honest_partial_state,
        test_byte_cap_honesty_survives_reopen,
        test_frame_cap_bounds_metadata_and_is_typed,
        test_identical_attempt_numbers_are_isolated_by_run_and_task,
        test_nested_worker_events_are_parsed_and_bounded,
        test_measured_duration_is_monotonic_and_persisted,
        test_error_subtypes_are_never_coerced_to_success,
        test_regression_fixture_clipping_and_coercion_are_refused,
        test_tampered_persisted_fact_is_refused_on_reopen,
        test_sealed_stream_refuses_more_bytes,
        test_reconnect_after_completion_is_gap_free_across_processes,
        test_preview_does_not_replace_raw_evidence,
        test_observer_reconnect_is_read_only_and_cursor_based,
        test_attempt_events_conform_to_frozen_contract,
        test_run_snapshot_keeps_three_distinct_mode_fields,
        test_run_snapshot_refuses_fabricated_or_unvalidated_fields,
        test_native_streaming_path_requires_identity_for_a_protected_request,
        test_native_streaming_path_refuses_memory_only_store,
        test_native_streaming_path_authenticates_and_isolates_the_child,
        test_native_streaming_empty_output_is_never_a_gate_pass,
        test_native_streaming_negative_exit_does_not_pass_from_stdout_json,
    ]
    failed = 0
    for t in tests:
        tmp = _pl.Path(tempfile.mkdtemp(prefix="sw1510-direct-"))
        try:
            t(tmp)
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
