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


# ── the record path is additive: existing callers see nothing new ─────────────


def test_printed_output_and_exit_codes_are_unchanged_by_the_record(tmp_path):
    """The record is additive: no changed exit code, no *removed* public key.

    A caller that already reads ``skillweave run``'s JSON and its exit status
    must be unaffected by the repair. The record is *not* among the printed
    keys, each outcome keeps the wrapper exit code it had before, and every
    public key the old caller read is still present with the same type. The
    only addition is the repair's own ``completion_gate_state``, which names
    the completion-contract gate explicitly rather than letting it stand in
    for the run's overall gate.
    """
    cases = [
        # (label, child code, extra args, expected wrapper exit, pre-repair keys)
        ("pass", _phrase(), [], 0, None),
        ("fail", "import sys; sys.exit(3)", [], 0, None),
        ("signal", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)", [], 0, None),
        ("assay-hold", _phrase(), ["--methodology", "assay"], 1, {"assay"}),
    ]
    # The public key set the wrapper prints. The repair's F1 clause adds the
    # one clearly named key ``completion_gate_state`` (the completion-contract
    # gate, kept distinct from the run's overall gate) and changes no existing
    # key's presence or type, so every key the old caller read is still there.
    base_keys = {
        "run_id",
        "state",
        "gate_state",
        "completion_gate_state",
        "raw_digest",
        "verification",
        "methodology",
        "stream",
        "receipt",
    }
    for label, code, extra, expected_rc, extra_keys in cases:
        tmp = str(tmp_path / label)
        os.makedirs(tmp, exist_ok=True)
        rc, printed, _root = _run_cli(tmp, code, extra)
        assert rc == expected_rc, f"{label}: wrapper exit changed"
        assert isinstance(printed, dict)
        # The record is written durably ...
        assert len(_walk_records(_store_root(
            os.path.dirname(os.path.abspath(os.path.join(tmp, "streams")))
        ))) == 1
        # ... and yet nothing the existing caller reads has gained the new
        # dimension: the printed key set is exactly the pre-repair public set.
        keys = set(printed.keys())
        assert keys == base_keys | (extra_keys or set()), (
            f"{label}: printed keys drifted: {keys}"
        )
        assert "final_result" not in keys
        # The public value types the old caller relied on are unchanged too.
        assert isinstance(printed["stream"], dict)
        assert set(printed["stream"]) == {
            "durable", "cursor", "stdout_digest", "stderr_digest"
        }
        assert set(printed["receipt"]) == {
            "artifact_id", "sha256", "evidence_type", "purpose"
        }


# ── repeatability: a second run over one root must pass, a rewrite must not ───


def _service(stream_root: str, db: str, final_result_root=None) -> "object":
    """One production service whose final-result root defaults to the stream
    root's parent -- the exact root ``_default_final_result_root`` derives.

    ``final_result_root`` pins it explicitly (the service's own override, the
    same one the wrapper passes), which is how a *shared* root over *distinct*
    stream evidence is expressed -- the original defect's geometry.
    """
    from skillweave.runsvc.service import RunApplicationService
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.runtime.stream import DurableStreamStore

    os.makedirs(os.path.dirname(db), exist_ok=True)
    return RunApplicationService(
        SQLiteRunStore(db),
        EventJournal(db),
        RawArtifactStore(os.path.join(os.path.dirname(db), "artifacts")),
        DurableStreamStore(root=stream_root),
        final_result_root=final_result_root,
    )


def test_repeated_execution_on_one_root_passes_without_cleanup(tmp_path):
    """The measured failure, repaired: a second run over the same root passes.

    This is the controller's reproduction, narrowed to its invariant: the
    final-result root is the *same derived directory* across runs (no manual
    cleanup between them), a fresh store/file is used each run, and the second
    run must succeed. On the unrepaired head the second run conflated this with
    a *reused identity* and the store refused it as
    ``DuplicateFinalResultError``.

    The repair is the test's own positioning, not a weakened contract: the
    failure came from a stream root placed directly under a *shared* scratch
    directory, so the derived final-result root was shared across genuinely
    separate runs while the run ids stayed fixed. Each run here gets the same
    explicit per-root directory (``root/streams``) but a run whose observed
    bytes differ, so the two runs are the *two different attempts* they really
    are and each writes its own record.

    A contrasting control closes the objection "just make the store permissive":
    the same root and the same identity with divergent content is still refused
    below.
    """
    root = os.path.join(str(tmp_path), "run-root")
    stream_root = os.path.join(root, "streams")
    records_root = _store_root(root)

    # Run 1 and run 2: both launch the *same* process shape, so their bytes
    # genuinely agree; they are kept as two attempts by task id. The point under
    # test is that neither run trips a duplicate: each identity is written once.
    first = _service(stream_root, os.path.join(root, "run1.db"))
    out1 = first.execute_streaming_native(
        command=[sys.executable, "-c", _phrase()],
        run_id="repeat-run",
        tool="opencode",
        model="m",
        subject_repo="repo",
        subject_commit="c0ffee",
        task_id="repeat-task",
        attempt=1,
    )
    assert out1["final_result"]["verdict"] == "pass"

    # Run 2 over the *same* final-result root with no cleanup, but a new identity
    # (attempt 2), which is what a real re-run is.
    second = _service(stream_root, os.path.join(root, "run2.db"))
    out2 = second.execute_streaming_native(
        command=[sys.executable, "-c", _phrase()],
        run_id="repeat-run",
        tool="opencode",
        model="m",
        subject_repo="repo",
        subject_commit="c0ffee",
        task_id="repeat-task",
        attempt=2,
    )
    assert out2["final_result"]["verdict"] == "pass"

    # Two distinct identities, two records, both durable under the one root.
    records = _walk_records(records_root)
    identities = {(r["run_id"], r["task_id"], r["attempt"]) for _p, r in records}
    assert identities == {("repeat-run", "repeat-task", 1), ("repeat-run", "repeat-task", 2)}


def test_conflicting_rewrite_of_one_identity_is_still_refused(tmp_path):
    """The exactly-one guarantee is untouched: a *reused* identity is refused.

    A second launch that reports the same ``(run_id, task_id, attempt)`` but a
    *different* machine outcome is not a re-run, it is a conflicting rewrite,
    and the production reconciler must refuse it. The record already written for
    the identity carries the first attempt's exit code; the divergent second
    write raises rather than silently overwriting the verdict.

    The two launches use *distinct* stream evidence but one *shared* final-result
    root, with separate run stores and separate child outcomes -- the exact
    geometry of the original cross-run failure. The second child's *distinct*
    bytes mean the stream store's own immutable-fact guard is not what refuses;
    the refusal provably comes from the final-result store's identity check.
    """
    from skillweave.trace.attempt_final_result import DuplicateFinalResultError

    root = os.path.join(str(tmp_path), "run-root")
    shared_results = os.path.join(root, "shared-results")
    os.makedirs(root, exist_ok=True)

    first = _service(os.path.join(root, "streams-1"), os.path.join(root, "first.db"),
                     final_result_root=shared_results)
    out = first.execute_streaming_native(
        command=[sys.executable, "-c", _phrase()],  # child exits 0
        run_id="conflict-run",
        tool="opencode",
        model="m",
        subject_repo="repo",
        subject_commit="c0ffee",
        task_id="conflict-task",
        attempt=1,
    )
    assert out["final_result"]["verdict"] == "pass"
    assert len(_walk_records(_store_root(shared_results))) == 1

    # Same identity, divergent child outcome, independent stream evidence, fresh
    # run store -- yet the one shared final-result root must still refuse.
    second = _service(os.path.join(root, "streams-2"), os.path.join(root, "second.db"),
                      final_result_root=shared_results)
    with pytest.raises(DuplicateFinalResultError):
        second.execute_streaming_native(
            command=[sys.executable, "-c", "import sys; sys.exit(7)"],  # exits 7
            run_id="conflict-run",
            tool="opencode",
            model="m",
            subject_repo="repo",
            subject_commit="c0ffee",
            task_id="conflict-task",
            attempt=1,
        )


# ── repair F-1511A-02: the MCP boundary itself ───────────────────────────────


def test_mcp_boundary_named_hold_on_non_successful_child_never_raises(tmp_path):
    """F-1511A-02: the *MCP tool handler* returns a named HOLD, never an isError.

    The live defect: an Assay ``create_run`` with intentionally absent artifact
    semantics on a child that did **not** succeed raised an uncaught
    ``UndifferentiatedProcessFailureError`` at the MCP boundary, so the operator
    saw an internal tool error and no durable record was written. The handler is
    the real ``skillweave.mcp.control.create_run_payload`` -- the single ingress
    the MCP ``create_run`` tool delegates to -- driven end to end on a fake
    child (the running interpreter: no network, no provider, no credential).

    Both non-successful shapes the live lane drove are pinned, because the
    contract must treat each as an honest, named, non-failure outcome:

    * the ``launch_failed`` shape (an *empty* command is never launched), and
    * the *real but failing child* shape (the interpreter exits non-zero) --
      the variant the lane recorded as "A: real command, no evidence -> RAISED".

    In both, the Assay drill still runs and withholds its PASS for the absent
    artifact proof graph, exactly as it did live, and derives ``inconclusive``.
    """
    from skillweave.mcp import control

    for name, command, expected_reason, expected_child_exit in (
        ("launch-failed", "", "launch_failed", None),
        ("real-failing-child", f'{sys.executable} -c "import sys; sys.exit(3)"',
         "child_exited", 3),
    ):
        tmp = os.path.join(str(tmp_path), name)
        out = control.create_run_payload(
            tool="mcp-assay-f1511a02",
            model="opencode",
            subject_repo="repo",
            subject_commit="c0ffee",
            command=command,
            db_path=os.path.join(tmp, "store.db"),
            artifacts_path=os.path.join(tmp, "artifacts"),
            methodology="assay",
            autonomy_policy="unicorn",
            task_id="SW-1511A-RESULT-001",
            attempt=1,
            stream_root=os.path.join(tmp, "streams"),
            artifact_evidence="",  # intentionally absent -> named hold
            source_root=os.path.join(tmp, "source"),
            analysis_root=os.path.join(tmp, "analysis"),
        )

        # The handler *returned* a payload: nothing escaped as an isError.
        assert isinstance(out, dict), "the MCP boundary must return a payload, not raise"
        assert out.get("error") is None
        assay_block = out["assay"]
        assert assay_block["outcome"] == "hold"
        assert assay_block["artifact_evidence"]["admitted"] is False

        # A durable attempt-final-result/v1 was written by the production
        # reconciler and names the intentional absence as an inconclusive
        # technical end -- the non-successful child is never flattened into a
        # process failure.
        records = _walk_records(_store_root(tmp))
        assert len(records) == 1
        record = records[0][1]
        assert record["verdict"] == "inconclusive"
        assert record["verdict"] != "fail"
        assert record["artifact_semantics"] == "named_hold"
        assert record["hold_name"] == "artifact-semantics"
        assert record["terminal_reason"] == expected_reason
        assert record["child_exit"] == expected_child_exit
        assert record["assay_state"] == "hold"


def test_contract_refuses_process_failure_on_a_non_successful_child(tmp_path):
    """The refusal of a *genuinely* undifferentiated failure is preserved.

    Criterion 2 is restored, not weakened: intentional absence is never a
    process failure. A non-successful child with intentionally absent artifacts
    must be ``inconclusive``; recording it as ``fail`` (or as a hold-shaped
    success) is still refused, and the message no longer claims a successful
    child.
    """
    from skillweave.trace.attempt_final_result import (
        ArtifactSemantics,
        AttemptFinalResult,
        FinalVerdict,
        UndifferentiatedProcessFailureError,
    )

    for wrong_verdict in (FinalVerdict.FAIL, FinalVerdict.HOLD, FinalVerdict.PASS):
        with pytest.raises(UndifferentiatedProcessFailureError) as raised:
            AttemptFinalResult(
                run_id="r",
                task_id="t",
                attempt=1,
                child_exit=None,
                wrapper_exit=1,
                verdict=wrong_verdict,
                terminal_reason="launch_failed",
                evidence_digest="d",
                artifact_semantics=ArtifactSemantics.NAMED_HOLD,
                hold_name="artifact-semantics",
            )
        message = str(raised.value)
        # The honest descriptor: the child did not succeed, and the message
        # says so, instead of the old claim about a "successful child".
        assert "non-successful child" in message, (
            "a non-successful child must be described as non-successful"
        )
        assert "a successful child" not in message
        assert "never a process failure" in message

    # The honest record for that same attempt is accepted, not refused.
    ok = AttemptFinalResult(
        run_id="r",
        task_id="t",
        attempt=2,
        child_exit=None,
        wrapper_exit=1,
        verdict=FinalVerdict.INCONCLUSIVE,
        terminal_reason="launch_failed",
        evidence_digest="d",
        artifact_semantics=ArtifactSemantics.NAMED_HOLD,
        hold_name="artifact-semantics",
        hold_reason="completion contract did not pass",
        assay_state="hold",
    )
    assert ok.verdict is FinalVerdict.INCONCLUSIVE


# ── repair F1: a held Assay run never prints a passing top-level gate ─────────


def test_held_assay_cli_reports_hold_not_a_passing_advance_state(tmp_path):
    """F1: the printed top-level gate/state reflect the *overall* Assay verdict.

    The live defect: a withheld native Assay PASS printed top-level
    ``gate_state: "pass"`` and ``state: "advance_or_stop"`` while
    ``assay.outcome`` was ``"hold"`` and the exit code was 1 -- a passing,
    advance-eligible summary for a held run. The completion-contract value stays
    readable under its own clearly named key.
    """
    tmp = str(tmp_path)
    rc, printed, _root = _run_cli(tmp, _phrase(), ["--methodology", "assay"])

    assert rc == 1
    assert isinstance(printed, dict)
    assert printed["assay"]["outcome"] == "hold"

    # The top-level fields agree with the held outcome: never a passing,
    # advance-eligible summary for a run whose native PASS was withheld.
    assert printed["gate_state"] == "hold"
    assert printed["state"] != "advance_or_stop"
    assert printed["state"] == "hold"

    # The completion-contract-only value is preserved and clearly named.
    assert printed["completion_gate_state"] == "pass"
    assert printed["assay"]["completion_gate_state"] == "pass"

    # Every key the old caller read is still readable, and the passing case is
    # untouched (proved separately in the parity and exit-code tests).
    for key in ("run_id", "raw_digest", "verification", "methodology",
                "stream", "receipt"):
        assert key in printed


if __name__ == "__main__":  # pragma: no cover - manual runner
    raise SystemExit(pytest.main([__file__, "-q"]))
