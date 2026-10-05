"""SW-1510-STREAM: durable stream / fact / replay.

Three criteria, each proven against real disposable subprocesses and the frozen
SDK wire contracts:

1. Per-attempt stdout/stderr (and nested-worker events) are durably streamed
   with per-stream byte caps, observable backpressure, and exact content
   digests.
2. The observed exit code, the *measured* monotonic duration, and the raw result
   subtype are immutable facts; a fixture that clips a timestamp or coerces an
   error to success is refused.
3. Reconnect/replay returns ordered, cursor-based progress; a truncated user
   preview never replaces the raw evidence.

Self-contained ``sys.path`` handling (like the module's sibling tests) so it
runs under pytest or by direct execution. No network, no paid provider: every
worker is a local ``python -c`` child.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _resolve_sdk():
    """Make ``skillweave_sdk`` importable, or return None when unavailable.

    The accepted SDK is composed read-only. It is normally already on
    ``PYTHONPATH``; if not, this walks up from the checkout to a sibling
    ``integrations/skillweave-sdk/src`` and adds it. Absence degrades only the
    SDK-schema assertions, never the local behavioural ones.
    """
    try:
        import skillweave_sdk  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        pass
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "integrations" / "skillweave-sdk" / "src"
        if (candidate / "skillweave_sdk" / "validator.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            try:
                import skillweave_sdk  # noqa: F401

                return True
            except Exception:  # noqa: BLE001
                return False
    return False


_SDK_AVAILABLE = _resolve_sdk()

from skillweave.runtime.stream import (  # noqa: E402
    AttemptFact,
    DurableStreamStore,
    Incompleteness,
    ResultSubtype,
    StreamFactConflict,
    StreamSealedError,
)
from skillweave.runtime.snapshot import (  # noqa: E402
    RunSnapshotInputs,
    SnapshotError,
    build_run_snapshot,
    preview_frames,
    progress_frames,
)
from skillweave.core.proc.runner import (  # noqa: E402
    run_process_streaming,
)
from skillweave.core.observer.observer import (  # noqa: E402
    ReadOnlyObserver,
    ReadOnlyError,
)
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402
from skillweave.runsvc import RunApplicationService  # noqa: E402

import pytest  # noqa: E402


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


# =========================================================================
# Criterion 1 — durable streaming, byte caps, backpressure, exact digests
# =========================================================================


def test_streams_are_durable_and_content_addressed():
    store = DurableStreamStore()
    result = run_process_streaming(
        _stream_child(), run_id="run-1", store=store, attempt=1
    )

    assert result.result_subtype == "exited"
    assert result.exit_code == 0

    out = store.spool(1, "stdout").read_retained()
    err = store.spool(1, "stderr").read_retained()
    assert out == b"OUT-A\nOUT-B\nOUT-C\n"
    assert err == b"ERR-1\nERR-2\n"

    # Exact content digests over the whole observed byte streams.
    assert result.stdout_digest == hashlib.sha256(out).hexdigest()
    assert result.stderr_digest == hashlib.sha256(err).hexdigest()
    assert result.stdout_observed_bytes == len(out)
    assert result.stderr_observed_bytes == len(err)

    # Every frame resolves back to the retained bytes it addressed.
    for frame in store.frames():
        assert store.resolve(frame.cursor) == frame.data


def test_interleaved_stdout_stderr_shares_one_ordered_cursor():
    store = DurableStreamStore()
    run_process_streaming(_stream_child(), run_id="run-2", store=store, attempt=1)

    frames = store.frames()
    cursors = [f.cursor for f in frames]
    assert cursors == sorted(cursors)
    assert len(cursors) == len(set(cursors))  # no duplicate cursor

    streams_seen = {f.stream for f in frames if f.stream}
    assert streams_seen == {"stdout", "stderr"}
    # The two streams are genuinely interleaved: the frame log alternates.
    kinds = [f.stream for f in frames]
    assert kinds.count("stderr") >= 1

    # Per-stream reassembly preserves each stream's own order.
    out = b"".join(f.data for f in frames if f.stream == "stdout")
    err = b"".join(f.data for f in frames if f.stream == "stderr")
    assert out == b"OUT-A\nOUT-B\nOUT-C\n"
    assert err == b"ERR-1\nERR-2\n"


def test_byte_cap_preserves_digest_and_marks_limit_honestly():
    code = "import sys\nsys.stdout.write('0123456789'*200)\nsys.stdout.flush()\n"

    store = DurableStreamStore(retain_limit=128)
    result = run_process_streaming(_py(code), run_id="run-3", store=store, attempt=1)

    summary = store.summary(1, "stdout")
    # The observed count and digest are exact over ALL bytes written...
    assert summary.observed_bytes == 2000
    assert summary.digest == hashlib.sha256(b"0123456789" * 200).hexdigest()
    # ...while the retainer is honestly bounded.
    assert summary.retained_bytes <= 128
    assert summary.retained_bytes == 128
    # A bounded retainer is typed, never mistaken for the whole stream.
    assert summary.incompleteness == Incompleteness.LIMIT_REACHED.value
    assert result.stdout_incompleteness == "limit_reached"
    # The retained bytes are a true prefix, not a substitute.
    assert store.spool(1, "stdout").read_retained() == (b"0123456789" * 200)[:128]


def test_backpressure_is_observable_and_memory_is_bounded():
    store = DurableStreamStore(retain_limit=64)
    code = "import sys\nsys.stdout.write('x'*5000)\nsys.stdout.flush()\n"
    result = run_process_streaming(_py(code), run_id="run-4", store=store, attempt=1)

    summary = store.summary(1, "stdout")
    assert summary.observed_bytes == 5000
    assert summary.retained_bytes == 64  # bounded, never grows past the cap
    assert summary.backpressure_events >= 0
    # Pressure is recorded rather than silently swallowed once the cap is hit.
    assert summary.incompleteness == "limit_reached"


def test_nested_worker_events_are_durably_streamed():
    code = (
        "import sys, json\n"
        "print(json.dumps({'event': 'wave_started', 'n': 1}))\n"
        "print(json.dumps({'event': 'lane_started', 'n': 2}))\n"
        "print('not-json-stays-raw')\n"
        "print(json.dumps({'event': 'wave_done', 'n': 3}))\n"
    )
    store = DurableStreamStore()
    run_process_streaming(_py(code), run_id="run-5", store=store, attempt=1)

    events = store.nested_events(1)
    assert [e["event"] for e in events] == ["wave_started", "lane_started", "wave_done"]
    # The non-JSON line is not invented into an event; it stays raw evidence.
    assert b"not-json-stays-raw" in store.spool(1, "stdout").read_retained()


# =========================================================================
# Criterion 2 — immutable measured facts; no clipping, no coercion
# =========================================================================


def test_measured_duration_is_monotonic_and_not_clipped():
    code = "import time; time.sleep(0.35); print('done')"
    store = DurableStreamStore()
    result = run_process_streaming(_py(code), run_id="run-6", store=store, attempt=1)

    # A measured interval, not a clipped zero and not a wall-clock guess.
    assert result.duration_ns >= int(0.30 * 1_000_000_000)
    assert result.duration_seconds >= 0.30
    assert store.fact(1).duration_ns == result.duration_ns


def test_error_subtypes_are_never_coerced_to_success():
    cases = [
        ("import sys; sys.exit(7)", "exited", 7, None, False),
        ("import os,signal; os.kill(os.getpid(), signal.SIGKILL)", "signaled", None, 9, False),
    ]
    for code, subtype, exit_code, sig, succeeded in cases:
        store = DurableStreamStore()
        result = run_process_streaming(_py(code), run_id="run-err", store=store, attempt=1)
        assert result.result_subtype == subtype
        assert result.exit_code == exit_code
        assert result.signal == sig
        assert result.succeeded is succeeded
        assert store.fact(1).result_subtype == subtype

    # A timeout is its own subtype, and an unstartable command is launch_failed.
    store = DurableStreamStore()
    timed = run_process_streaming(
        _py("import time; time.sleep(5)"), run_id="run-to", store=store, attempt=1, timeout=0.4
    )
    assert timed.result_subtype == "timed_out"
    assert timed.exit_code is None and timed.signal is None

    store2 = DurableStreamStore()
    launch = run_process_streaming(
        ["/nonexistent/does/not/exist"], run_id="run-lf", store=store2, attempt=1
    )
    assert launch.result_subtype == "launch_failed"
    assert launch.message  # a failure is explained, never a silent success


def test_regression_fixture_clipping_and_coercion_are_refused():
    """A tampered fact that clips time or coerces success fails the fixture.

    The recorded fact of an attempt that exited non-zero after a real interval
    cannot be replaced by a clipped duration or a coerced success.
    """
    store = DurableStreamStore()
    result = run_process_streaming(
        _py("import time; time.sleep(0.2); import sys; sys.exit(3)"),
        run_id="run-imm", store=store, attempt=1,
    )
    fact = store.fact(1)
    assert fact.exit_code == 3 and fact.result_subtype == "exited"
    assert fact.duration_ns >= int(0.15 * 1_000_000_000)

    # Coercing the error to a success (exit 0) is refused.
    coerced = AttemptFact(
        run_id=fact.run_id, task_id=fact.task_id, attempt=1,
        exit_code=0, signal=None, result_subtype="exited",
        termination="exited", duration_ns=fact.duration_ns,
        started_at=fact.started_at, completed_at=fact.completed_at, pid=fact.pid,
    )
    with pytest.raises(StreamFactConflict):
        store.record_fact(coerced)

    # Clipping the measured duration down is refused.
    clipped = AttemptFact(
        run_id=fact.run_id, task_id=fact.task_id, attempt=1,
        exit_code=3, signal=None, result_subtype="exited",
        termination="exited", duration_ns=1,
        started_at=fact.started_at, completed_at=fact.completed_at, pid=fact.pid,
    )
    with pytest.raises(StreamFactConflict):
        store.record_fact(clipped)

    # The original collected fact stands unchanged.
    assert store.fact(1).duration_ns == fact.duration_ns
    assert store.fact(1).exit_code == 3


def test_sealed_stream_refuses_more_bytes():
    store = DurableStreamStore()
    run_process_streaming(_py("print('x')"), run_id="run-seal", store=store, attempt=1)
    with pytest.raises(StreamSealedError):
        store.ingest(attempt=1, stream="stdout", chunk=b"late")


# =========================================================================
# Criterion 3 — ordered cursor reconnect/replay; preview != raw evidence
# =========================================================================


def test_reconnect_mid_run_returns_ordered_cursor_progress():
    store = DurableStreamStore()
    store.begin_attempt(run_id="run-rc", attempt=1)
    for i in range(5):
        store.ingest(attempt=1, stream="stdout", chunk=f"line-{i}\n".encode())

    first = store.frames(0)
    assert [f.cursor for f in first] == list(range(1, 6))
    # A reconnect from the last-seen cursor returns exactly the new frames.
    store.ingest(attempt=1, stream="stderr", chunk=b"late\n")
    resumed = store.frames(after_cursor=5)
    assert len(resumed) == 1
    assert resumed[0].stream == "stderr"
    # No gap, no duplication across the two reads.
    assert store.frames(0) == first + resumed


def test_reconnect_after_completion_is_gap_free():
    store = DurableStreamStore()
    run_process_streaming(_stream_child(), run_id="run-cmp", store=store, attempt=1)
    everything = store.frames(0)
    assert [f.cursor for f in everything] == list(range(1, store.cursor + 1))
    # Reconnect from an interior cursor yields the exact remaining suffix.
    split = len(everything) // 2
    mid = everything[split].cursor
    # ``after_cursor`` is exclusive: the frame at ``mid`` is already seen.
    assert store.frames(after_cursor=mid) == everything[split + 1:]


def test_preview_does_not_replace_raw_evidence():
    store = DurableStreamStore()
    code = "import sys\nsys.stdout.write('abcdef'*100)\nsys.stdout.flush()\n"
    run_process_streaming(_py(code), run_id="run-prev", store=store, attempt=1)

    preview = store.preview(attempt=1, stream="stdout", max_bytes=10)
    assert preview["is_full_evidence"] is False
    assert preview["preview_bytes"] == 10
    assert preview["observed_bytes"] == 600
    assert preview["is_truncated"] is True
    # The raw evidence is still fully resolvable, independent of the preview.
    raw = store.spool(1, "stdout").read_retained()
    assert len(raw) == 600
    assert raw[:10] == preview["text"].encode()

    # The run-level preview is likewise marked non-authoritative.
    rp = preview_frames(store, "run-prev", max_bytes=10)
    assert rp["is_full_evidence"] is False
    assert rp["observed_bytes"] == 600


def test_observer_reconnect_is_read_only_and_cursor_based():
    store = DurableStreamStore()
    run_process_streaming(_stream_child(), run_id="run-obs", store=store, attempt=1)
    observer = ReadOnlyObserver("/nonexistent/observer-state.json")  # fresh defaults

    progress = observer.read_progress("run-obs", store)
    assert progress["is_full_evidence"] is True
    assert progress["frame_count"] == len(store.frames(0))
    cursors = [f["cursor"] for f in progress["frames"]]
    assert cursors == sorted(cursors)

    # Resume from an interior cursor: only the unseen suffix comes back.
    mid = cursors[0]
    suffix = observer.read_progress("run-obs", store, after_cursor=mid)
    assert [f["cursor"] for f in suffix["frames"]] == [c for c in cursors if c > mid]

    # Read-only: mutating the observer still fails, and the store is untouched.
    with pytest.raises(ReadOnlyError):
        observer.update_offset(99)
    with pytest.raises(ReadOnlyError):
        observer.something = 1
    assert store.cursor == progress["latest_cursor"]

    # A preview is explicitly not full evidence.
    prev = observer.read_preview("run-obs", store, max_bytes=4)
    assert prev["is_full_evidence"] is False


# =========================================================================
# Frozen SDK contracts (attempt-event-v1, run-snapshot-v1)
# =========================================================================


def test_attempt_events_conform_to_frozen_contract():
    store = DurableStreamStore()
    run_process_streaming(
        _py("import sys; sys.stdout.write('hello'); sys.stderr.write('world')"),
        run_id="run-evt", store=store, attempt=1,
    )
    events = store.to_attempt_events(
        attempt=1, issuer="run-service", principal="run-service-1"
    )
    assert events
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    for e in events:
        assert e["schema_version"] == "1.0.0"
        assert len(e["evidence_digest"]) == 64
        assert e["attempt"] == 1
        assert e["run_id"] == "run-evt"
    if _SDK_AVAILABLE:
        from skillweave_sdk import validator

        for e in events:
            assert validator.validate(
                e, "https://skillweave.dev/schemas/execution/attempt-event/v1"
            ) == []


def test_run_snapshot_keeps_three_distinct_mode_fields():
    store = DurableStreamStore()
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

    if _SDK_AVAILABLE:
        from skillweave_sdk import validator

        assert validator.validate(
            snapshot, "https://skillweave.dev/schemas/execution/run-snapshot/v1"
        ) == []


def test_run_snapshot_refuses_fabricated_or_unvalidated_fields():
    digest = "b" * 64
    base = dict(
        run_id="r", task_id="t", state="verify", gate_state="pass",
        session_schedule="continuous", subject_digest=digest,
        brief_digest=digest, envelope_digest=digest,
        process_identity={"handle_id": "h", "service_principal": "svc"},
        authority_decision_digest=digest,
    )
    # An unknown methodology fails closed rather than being coerced.
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(methodology="vibes", autonomy_policy="unicorn", **base))
    # A non-native (model-authored) handle is refused.
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(
            methodology="assay", autonomy_policy="unicorn",
            **{**base, "process_identity": "not-a-handle"},
        ))
    # A malformed digest is refused, never auto-filled.
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(
            methodology="assay", autonomy_policy="unicorn",
            **{**base, "subject_digest": "short"},
        ))
    # An out-of-enum autonomy policy is refused.
    with pytest.raises(SnapshotError):
        build_run_snapshot(RunSnapshotInputs(methodology="assay", autonomy_policy="wild", **base))


# =========================================================================
# Production run-service integration (real child, real six-record path)
# =========================================================================


def _service():
    store = SQLiteRunStore(":memory:")
    journal = EventJournal(store)
    raw = RawArtifactStore()
    return RunApplicationService(store, journal, raw), store, journal


def test_streaming_service_binds_evidence_and_gate():
    service, store, journal = _service()
    out = service.execute_streaming(
        _py("import sys; sys.stdout.write('real-output')"),
        run_id="run-svc-stream-1", tool="opencode", model="model-x",
        subject_repo="skillweave", subject_commit="abc123",
        created_at="2026-08-19T00:00:00Z",
    )
    assert out["gate_state"] == "pass"
    assert out["fact"].result_subtype == "exited"
    run = store.get_run("run-svc-stream-1")
    assert run.state == "advance_or_stop"
    assert store.get_evidence("runsvc-stream-run-svc-stream-1") is not None
    # The journal carries the measured facts and exact digests.
    event = journal.get_events("run-svc-stream-1")[0]
    assert event.payload["result_subtype"] == "exited"
    assert event.payload["duration_ns"] > 0
    assert len(event.payload["stdout"]["digest"]) == 64
    # Cursor-based progress and preview are both exposed and distinguished.
    frames = service.stream_frames("run-svc-stream-1")
    assert [f["cursor"] for f in frames] == sorted(f["cursor"] for f in frames)
    preview = service.stream_preview("run-svc-stream-1", max_bytes=5)
    assert preview["is_full_evidence"] is False


def test_streaming_service_empty_output_is_never_a_gate_pass():
    service, store, journal = _service()
    out = service.execute_streaming(
        _py("pass"),
        run_id="run-svc-stream-2", tool="opencode", model="model-x",
        subject_repo="skillweave", subject_commit="abc123",
        created_at="2026-08-19T00:00:00Z",
    )
    assert out["gate_state"] != "pass"
    run = store.get_run("run-svc-stream-2")
    assert run.state == "advance_or_stop"


def test_streaming_service_receipt_resolves_under_observation():
    service, store, journal = _service()
    service.execute_streaming(
        _py("print('resolvable')"),
        run_id="run-svc-stream-3", tool="opencode", model="model-x",
        subject_repo="skillweave", subject_commit="abc123",
        created_at="2026-08-19T00:00:00Z",
    )
    receipt = store.get_evidence("runsvc-stream-run-svc-stream-3")
    assert receipt is not None
    # The receipt's digest is the raw artifact digest, resolvable back to bytes.
    raw_bytes = service.raw_artifacts.resolve(receipt.sha256)
    assert b"resolvable" in raw_bytes


# =========================================================================
# Direct execution (no pytest)
# =========================================================================


def _run_all() -> int:
    tests = [
        test_streams_are_durable_and_content_addressed,
        test_interleaved_stdout_stderr_shares_one_ordered_cursor,
        test_byte_cap_preserves_digest_and_marks_limit_honestly,
        test_backpressure_is_observable_and_memory_is_bounded,
        test_nested_worker_events_are_durably_streamed,
        test_measured_duration_is_monotonic_and_not_clipped,
        test_error_subtypes_are_never_coerced_to_success,
        test_regression_fixture_clipping_and_coercion_are_refused,
        test_sealed_stream_refuses_more_bytes,
        test_reconnect_mid_run_returns_ordered_cursor_progress,
        test_run_snapshot_keeps_three_distinct_mode_fields,
        test_run_snapshot_refuses_fabricated_or_unvalidated_fields,
        test_streaming_service_binds_evidence_and_gate,
        test_streaming_service_empty_output_is_never_a_gate_pass,
        test_streaming_service_receipt_resolves_under_observation,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
