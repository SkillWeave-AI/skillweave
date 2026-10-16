"""SW-1511A-RESULT-001 (repair): the *production* producers of the record.

The accepted finding on the reviewed candidate ``48873de`` is that the
``attempt-final-result/v1`` contract module had no production caller: the
contract, its schema validation and its red proof were sound, but no launched
attempt ever wrote a record, so the criteria -- which are worded as behavior of
launched attempts -- were not satisfied by the module alone.

These tests are the adversarial proof for the repair. Each one drives the
*real* production entry point (``skillweave.cli.run.main`` and, for the
non-CLI surface, ``RunApplicationService.execute_streaming_native``) end to end
on a **fake process** -- ``sys.executable -c`` running an inline script -- so no
network is opened, no provider credential is read and no wall clock is trusted.
Nothing here imports the contract module to construct a record by hand: every
record asserted on is one the production path wrote, found by walking the
default final-result root.

On the reviewed candidate these tests fail: ``run.py`` never calls the module,
so no directory of records exists and every walk is empty. After the wiring the
production path writes exactly one ``attempt-final-result/v1`` per launched
attempt in the directory the contract's store owns.
"""

import contextlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


def _phrase() -> str:
    """One inline child program, so the proof spawns a real process.

    The child is the running interpreter, so the launch is hermetic: no
    network, no provider, no credential, no external binary.
    """
    return "print('child-ran')"


def _walk_records(root: str) -> list:
    """Every persisted record under ``root``, read from disk.

    Deliberately a filesystem walk, not a store API call: the assertion is that
    the production path *durably wrote* a record, not that some object exists.
    """
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".json"):
                continue
            with open(os.path.join(dirpath, name), "r", encoding="utf-8") as fh:
                out.append((os.path.join(dirpath, name), json.load(fh)))
    return out


def _run_cli(tmp: str, code: str, extra: list) -> tuple:
    """Drive the installed ``skillweave run`` entry end to end on a fake child.

    Returns ``(exit_code, printed_json_or_None, default_final_result_root)``.
    """
    from skillweave.cli import run as run_cli

    stream_root = os.path.join(tmp, "streams")
    argv = [
        "--tool", "opencode",
        "--model", "m",
        "--subject-repo", "repo",
        "--subject-commit", "c0ffee",
        "--task-id", "SW-1511A-RESULT-001",
        "--attempt", "1",
        "--db-path", os.path.join(tmp, "cli.db"),
        "--stream-root", stream_root,
        "--artifacts-path", os.path.join(tmp, "artifacts"),
        *extra,
        "--", sys.executable, "-c", code,
    ]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = run_cli.main(argv)
    printed = json.loads(out.getvalue()) if out.getvalue().strip() else None
    # The wrapper's default root: the stream root's parent. The contract's own
    # store nests the records one level deeper, under ``attempt-final-results``.
    return rc, printed, os.path.dirname(os.path.abspath(stream_root))


def _store_root(default_root: str) -> str:
    """The directory the contract store writes into under a root."""
    return os.path.join(default_root, "attempt-final-results")


# ── criterion 1 ──────────────────────────────────────────────────────────────


def test_criterion1_production_wrapper_writes_exactly_one_full_record(tmp_path):
    """Criterion 1 through the real entry point: one record, all six dimensions.

    Red on ``48873de``: the wrapper never calls the contract module, so the
    default record directory is never created and the walk yields nothing.
    """
    tmp = str(tmp_path)
    rc, printed, default_root = _run_cli(tmp, _phrase(), [])

    # The production launch succeeded and printed its unchanged public JSON.
    assert rc == 0
    assert isinstance(printed, dict) and printed.get("run_id")

    # The production path wrote exactly one record for the launched attempt.
    records = _walk_records(_store_root(default_root))
    assert len(records) == 1, (
        "the launched attempt must have exactly one attempt-final-result/v1 "
        f"written by the production path, found {len(records)}"
    )
    _path, record = records[0]

    # Every dimension the criterion names is present and is its own field.
    from skillweave.trace.attempt_final_result import (
        ATTEMPT_FINAL_RESULT_SCHEMA_ID,
    )

    assert record["schema"] == ATTEMPT_FINAL_RESULT_SCHEMA_ID
    assert record["run_id"] == printed["run_id"]
    assert record["task_id"] == "SW-1511A-RESULT-001"
    assert record["attempt"] == 1
    assert record["child_exit"] == 0
    assert record["wrapper_exit"] == 0
    assert record["terminal_reason"] == "child_exited"
    assert record["verdict"] == "pass"
    assert record["outcome"] == "exit_code"
    # The evidence digest is a real sha256 over the observed child bytes, not a
    # placeholder: the child printed a known phrase.
    assert isinstance(record["evidence_digest"], str)
    assert len(record["evidence_digest"]) == 64

    # One attempt means one identity: a second walk must not find a second
    # record for the same (run, task, attempt).
    identities = {
        (r["run_id"], r["task_id"], r["attempt"]) for _p, r in records
    }
    assert identities == {(printed["run_id"], "SW-1511A-RESULT-001", 1)}


def test_criterion1_child_failure_is_failed_record_not_wrapper_failure(tmp_path):
    """Criterion 1: a failing child is recorded as such, wrapper exit stays 0.

    The six dimensions are *separated*: the child exited 3 while the wrapper
    returned 0, so the record must not conflate the two.
    """
    tmp = str(tmp_path)
    rc, printed, default_root = _run_cli(tmp, "import sys; sys.exit(3)", [])

    # The wrapper's public behavior is unchanged by the record write.
    assert rc == 0 and isinstance(printed, dict)
    records = _walk_records(_store_root(default_root))
    assert len(records) == 1
    record = records[0][1]
    assert record["child_exit"] == 3
    assert record["wrapper_exit"] == 0
    assert record["terminal_reason"] == "child_exited"
    assert record["verdict"] == "fail"


def test_criterion1_signal_is_its_own_outcome(tmp_path):
    """Criterion 1: a signalled child is `signal`, never a fabricated exit code."""
    tmp = str(tmp_path)
    rc, _printed, default_root = _run_cli(
        tmp, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)", []
    )
    assert rc == 0
    records = _walk_records(_store_root(default_root))
    assert len(records) == 1
    record = records[0][1]
    assert record["child_signal"] == 9
    assert record["child_exit"] is None
    assert record["terminal_reason"] == "child_signaled"
    assert record["outcome"] == "signal"


# ── criterion 2 ──────────────────────────────────────────────────────────────


def test_criterion2_production_named_hold_never_a_process_failure(tmp_path):
    """Criterion 2 through the real entry point: absent artifacts -> named hold.

    An Assay run whose launch *succeeds* yet whose native PASS is withheld for
    artifact-specific semantics (here: no artifact proof graph was declared) is
    the intentionally-absent-artifact case. Red on ``48873de``: no record is
    written at all, so nothing distinguishes the hold from a process failure.
    """
    tmp = str(tmp_path)
    rc, printed, default_root = _run_cli(tmp, _phrase(), ["--methodology", "assay"])

    # The wrapper withholds the Assay PASS with its non-zero exit -- unchanged
    # by the record write.
    assert rc == 1
    assert isinstance(printed, dict)

    records = _walk_records(_store_root(default_root))
    assert len(records) == 1
    record = records[0][1]

    # The child *succeeded*; the missing artifact is a named hold, named.
    assert record["child_exit"] == 0
    assert record["verdict"] == "hold"
    assert record["gate_state"] == "hold"
    assert record["assay_state"] == "hold"
    assert record["artifact_semantics"] == "named_hold"
    assert record["hold_name"] == "artifact-semantics"
    assert record["hold_reason"], "a named hold must carry its reason"
    # ... and it is explicitly *not* an undifferentiated process failure.
    assert record["verdict"] != "fail"


# ── criterion 3 ──────────────────────────────────────────────────────────────


def test_criterion3_record_survives_retention_and_needs_no_resume(tmp_path):
    """Criterion 3 through the real entry point: the record outlives the stream.

    The record is written *outside* the retention-capped stream log and is read
    back by a fresh process over the same root, so the verdict and its receipt
    need no resume to recover. The evidence digest is over every observed byte,
    so it does not shrink to whatever the capped log happened to retain.

    Red on ``48873de``: no record exists to recover.
    """
    tmp = str(tmp_path)
    # A child whose output exceeds the default retention cap on each stream, so
    # retention genuinely truncated the retained stream bytes.
    code = (
        "import sys\n"
        "for _ in range(4000):\n"
        "    sys.stdout.write('x' * 512)\n"
        "    sys.stderr.write('e' * 512)\n"
    )
    rc, printed, default_root = _run_cli(tmp, code, [])
    assert rc == 0 and isinstance(printed, dict)

    store_root = _store_root(default_root)
    records = _walk_records(store_root)
    assert len(records) == 1
    path, record = records[0]

    # The retention cap genuinely truncated the *stream* evidence: each
    # stream's whole-observed digest differs from its retained-prefix digest,
    # and the observed byte count is far larger than the retained one.
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.trace.attempt_final_result import (
        ATTEMPT_FINAL_RESULT_SCHEMA_ID,
        AttemptFinalResultStore,
        evidence_digest,
    )

    stream_store = DurableStreamStore(root=os.path.join(tmp, "streams"))
    summaries = {
        name: stream_store.summary(
            1, name, run_id=printed["run_id"], task_id="SW-1511A-RESULT-001"
        )
        for name in ("stdout", "stderr")
    }
    assert summaries["stdout"].observed_bytes == 4000 * 512
    assert summaries["stdout"].retained_bytes < summaries["stdout"].observed_bytes
    assert summaries["stdout"].digest != summaries["stdout"].retained_digest

    # The record's digest is the whole-observed digest of both streams -- a
    # receipt for everything that arrived, not for the retained prefix.
    assert record["evidence_digest"] == evidence_digest(
        [summaries["stdout"].digest, summaries["stderr"].digest]
    )
    assert record["evidence_digest"] != evidence_digest(
        [summaries["stdout"].retained_digest, summaries["stderr"].retained_digest]
    )

    # A brand-new store over the same root -- a fresh process -- recovers the
    # verdict and the receipt with no resume, because they live in their own
    # durable directory beside the stream evidence rather than in the capped log.
    reopened = AttemptFinalResultStore(root=default_root)
    recovered = reopened.read(printed["run_id"], "SW-1511A-RESULT-001", 1)
    assert recovered.to_dict() == record
    assert recovered.evidence_digest == record["evidence_digest"]
    assert recovered.gate_state == "pass"

    # The persisted bytes are the contract's own envelope, not a preview, and
    # the record lives outside the stream directory.
    on_disk = json.loads(Path(path).read_text(encoding="utf-8"))
    assert on_disk["schema"] == ATTEMPT_FINAL_RESULT_SCHEMA_ID
    assert os.path.join(tmp, "streams") not in os.path.abspath(path)


# ── the non-CLI producer surface (the child result reconciler) ────────────────


def test_reconciler_surface_writes_the_record_without_the_cli(tmp_path):
    """The child result reconciler is a producer in its own right.

    The data contract names *two* producers. A caller that drives the service
    directly (not through ``skillweave run``) must still get exactly one record,
    written by the reconciler inside ``execute_streaming_native``.
    """
    from skillweave.runsvc.service import RunApplicationService
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.runtime.stream import DurableStreamStore

    tmp = str(tmp_path)
    db = os.path.join(tmp, "svc.db")
    svc = RunApplicationService(
        SQLiteRunStore(db),
        EventJournal(db),
        RawArtifactStore(os.path.join(tmp, "artifacts")),
        DurableStreamStore(root=os.path.join(tmp, "streams")),
    )
    out = svc.execute_streaming_native(
        command=[sys.executable, "-c", _phrase()],
        run_id="run-direct-1511a",
        tool="opencode",
        model="m",
        subject_repo="repo",
        subject_commit="c0ffee",
        task_id="SW-1511A-RESULT-001",
        attempt=1,
    )
    # The service's own result object carries the reconciled record ...
    assert out["final_result"]["verdict"] == "pass"
    # ... and the production surface durably wrote exactly one record beside
    # the stream root, in the same directory the wrapper's default uses.
    default_root = os.path.dirname(os.path.abspath(os.path.join(tmp, "streams")))
    records = _walk_records(_store_root(default_root))
    assert len(records) == 1
    assert records[0][1] == out["final_result"]
    # The two producers agree on the *default* root: a direct service caller
    # and the CLI wrapper both land in `<stream-root-parent>/attempt-final-results`.
    assert os.path.abspath(os.path.dirname(os.path.dirname(records[0][0]))) == (
        os.path.abspath(default_root)
    )


# ── the explicit root (the caller's own location) ─────────────────────────────


def test_explicit_final_result_root_wins_over_the_default(tmp_path):
    """A caller-named ``--final-result-root`` is where the one record lands.

    The default is a convenience; the explicit location must be reproducible
    and must win, so an operator can point the receipt at a durable, published
    directory without it also being written to the default sibling.
    """
    tmp = str(tmp_path)
    explicit = os.path.join(tmp, "explicit-results")
    _rc, printed, default_root = _run_cli(
        tmp, _phrase(), ["--final-result-root", explicit]
    )

    records = _walk_records(_store_root(explicit))
    assert len(records) == 1
    assert records[0][1]["verdict"] == "pass"
    assert records[0][1]["run_id"] == printed["run_id"]
    # Exactly one location: the default sibling is not also written.
    assert _walk_records(_store_root(default_root)) == []


# ── the presentation flag is orthogonal to the record ────────────────────────


def test_output_root_does_not_disturb_the_record(tmp_path):
    """``--output-root`` redirects the child's writes, not this receipt.

    The scope-enforced launch flags must not change which record is written:
    the same launched attempt still yields exactly one record with its machine
    outcome, whether or not an output root is named.
    """
    tmp = str(tmp_path)
    out_root = os.path.join(tmp, "checkout")
    rc, printed, default_root = _run_cli(
        tmp, "import sys; sys.stdout.write('hello-child')", ["--output-root", out_root]
    )
    assert rc == 0 and isinstance(printed, dict)
    records = _walk_records(_store_root(default_root))
    assert len(records) == 1
    record = records[0][1]
    assert record["child_exit"] == 0
    assert record["terminal_reason"] == "child_exited"
    assert record["verdict"] == "pass"


if __name__ == "__main__":  # pragma: no cover - manual runner
    raise SystemExit(pytest.main([__file__, "-q"]))
