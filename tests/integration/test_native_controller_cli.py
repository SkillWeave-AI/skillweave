"""SW-SCOPE-010 unit A: the ``native-controller`` production entry point.

These tests prove the **real CLI router** reaches the state-driven,
verdict-driven controller, acquires its fenced lease over a real
``SQLiteRunStore`` on a temporary path, and drives owned attempts through
all six declared steps — or refuses/terminates as the durable state dictates.

Every test supplies both the **worker** and **reviewer** adapter ports and
pre-seeds the durable run state plus the review record the controller reads
its verdict from.  The worker is a fake that launches nothing; the reviewer
supplies independent verifier evidence through the gate-receipt evaluator.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skillweave.cli import native_controller as nc
from skillweave.cli.main import main as router_main
from skillweave.runtime.authority import MCPPrincipalAuthority
from skillweave.runtime.native_process import (
    INTENT_FAILED,
    INTENT_INTEGRATED,
    INTENT_LAUNCHED,
    INTENT_REVIEWED,
    IntentStateConflictError,
    NativeProcessService,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
    STEP_REPAIR,
    STEP_RE_REVIEW,
)
from skillweave.runtime.store import SQLiteRunStore

# ── Helpers ────────────────────────────────────────────────────────────────

_EXPECTED_STEPS = (
    STEP_READY_DISPATCH,
    STEP_INDEPENDENT_REVIEW,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
)

_PASS_EVIDENCE = (
    {
        "schema_version": "independent-gate-receipt/1.0.0",
        "evidence_type": "member",
        "verifier_identity": "reviewer-1",
        "verdict": "PASS",
    },
)

_FAIL_EVIDENCE = (
    {
        "schema_version": "independent-gate-receipt/1.0.0",
        "evidence_type": "member",
        "verifier_identity": "reviewer-1",
        "verdict": "FAIL",
    },
)


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "store.db")


def _seed_run(store: SQLiteRunStore, run_id: str) -> None:
    """Create a run and transition it to *batch_selection* (the pre-review state
    the durable review record is written from)."""
    store.create_run(run_id)
    record = store.get_run(run_id)
    store.transition(
        run_id, "batch_selection", record.state, record.version,
        reason="test setup",
    )


def _seed_review_record(
    store: SQLiteRunStore, run_id: str, verdict: str, comment: str = "ok",
) -> None:
    """Write a durable review record into ``transitions_log``, simulating the
    row the MCP ``review_run`` tool writes via ``store.transition``.

    The controller reads its verdict from this row — it is never supplied by
    the reviewer port.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    store._conn.execute(
        "INSERT INTO transitions_log "
        "(run_id, from_state, to_state, reason, version, timestamp, role) "
        "VALUES (?, 'batch_selection', 'advance_or_stop', ?, 1, ?, 'reviewer')",
        (run_id, f"Review verdict {verdict}: {comment}", now),
    )
    store._conn.commit()


class FakeWorker:
    """Records the dispatch and returns a synthetic execution — launches nothing."""

    def __init__(self, artifact: str = "artifact:produced"):
        self.artifact = artifact
        self.calls: list = []

    def execute(self, dispatch):
        self.calls.append(dispatch)
        return nc.AttemptExecution(producer_artifact=self.artifact)


class ExplodingWorker:
    """A launch seam that fails, to prove the failure path is fail-closed."""

    def execute(self, dispatch):
        raise RuntimeError("worker blew up")


class FakeReviewer:
    """Returns a verdict with independent evidence.  On the second call
    (re-review) it also writes a new durable review record into the store so
    ``_require_recorded_verdict`` reads a fresh verdict — the same thing the
    MCP ``review_run`` tool does in production."""

    def __init__(self, store, run_id, verdicts, evidence_list, verifier_identity="reviewer-1"):
        self.store = store
        self.run_id = run_id
        self.verdicts = list(verdicts)
        self.evidence_list = list(evidence_list)
        self.verifier_identity = verifier_identity
        self.calls: list = []

    def review(self, dispatch, execution):
        idx = len(self.calls)
        verdict = self.verdicts[min(idx, len(self.verdicts) - 1)]
        evidence = self.evidence_list[min(idx, len(self.evidence_list) - 1)]
        self.calls.append((verdict, evidence))
        # On re-review: write the new durable record the controller reads.
        if idx >= 1:
            _seed_review_record(self.store, self.run_id, verdict, f"re-review #{idx}")
        return nc.ReviewVerdict(
            verdict=verdict, evidence=evidence,
            verifier_identity=self.verifier_identity,
        )


class _NoOpReviewer:
    """A reviewer stub for tests where the controller never reaches the review
    step (corrupt store, unwritable store, refused port, etc.)."""

    def review(self, dispatch, execution):
        return nc.ReviewVerdict("pass", evidence=_PASS_EVIDENCE, verifier_identity="r1")


def _result(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(out)


# ── 1. Passing review drives all six steps and accepts ─────────────────────

def test_passing_review_drives_all_six_steps_and_accepts(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    worker = FakeWorker()
    reviewer = FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE])

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=worker, reviewer=reviewer, store=store,
    )

    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    # No repair/re-review needed for a passing review.
    assert tuple(result["steps"]) == (
        STEP_READY_DISPATCH, STEP_INDEPENDENT_REVIEW,
        STEP_CONTROLLED_INTEGRATION, STEP_DEPENDENT_WAVE,
    )
    assert result["accepted"] is True
    assert result["integration"] == "git_merge"
    assert result["fencing_token"] == 1
    assert len(worker.calls) == 1
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0][0] == "pass"

    # The drive is durable: the intent is in the store.
    intents = store.list_dispatch_intents(run_id="run-1")
    assert len(intents) == 1
    store.close()


# ── 2. Router routes the subcommand end-to-end ─────────────────────────────

def test_router_routes_the_subcommand(tmp_path):
    worker = FakeWorker()
    exit_code = router_main(["native-controller", "--run-id", "r", "--task-id", "t"])
    assert exit_code in (nc.EXIT_OK, nc.EXIT_REFUSED, nc.EXIT_FAILED)
    assert len(worker.calls) == 0  # the router never saw our fake worker


# ── 3. Second controller refused while first holds the lease ───────────────

def test_second_controller_refused_by_live_lease(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    holder = NativeProcessService(store, "first-controller")
    holder.acquire(ttl_seconds=3600)

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db, "--holder", "second"],
        worker=FakeWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
    )

    assert exit_code == nc.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err.lower()
    # First controller's lease was not stolen.
    assert store.current_fencing_token() == holder.fencing_token
    holder.release()
    store.close()


# ── 4. Lease released after successful drive ───────────────────────────────

def test_lease_released_after_successful_drive(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    first = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=FakeWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert first == nc.EXIT_OK
    capsys.readouterr()

    # Second drive succeeds — the lease was released.
    _seed_run(store, "run-2")
    _seed_review_record(store, "run-2", "pass")
    second = nc.main(
        ["--run-id", "run-2", "--task-id", "task-b", "--db-path", db],
        worker=FakeWorker(),
        reviewer=FakeReviewer(store, "run-2", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert second == nc.EXIT_OK
    result = _result(capsys)
    assert result["run_id"] == "run-2"
    store.close()


# ── 5. Worker failure is fail-closed, lease still released ─────────────────

def test_worker_failure_fail_closed_and_releases_lease(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=ExplodingWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
    )
    assert exit_code == nc.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err

    # Lease did not leak.
    _seed_run(store, "run-2")
    _seed_review_record(store, "run-2", "pass")
    follow_up = nc.main(
        ["--run-id", "run-2", "--task-id", "task-b", "--db-path", db],
        worker=FakeWorker(),
        reviewer=FakeReviewer(store, "run-2", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert follow_up == nc.EXIT_OK
    store.close()


# ── 6. Failing review → repair → re-review pass → integrate ────────────────

def test_failing_review_repair_and_re_review_pass(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "fail", "needs repair")

    worker = FakeWorker()
    reviewer = FakeReviewer(
        store, "run-1",
        verdicts=["fail", "pass"],
        evidence_list=[_FAIL_EVIDENCE, _PASS_EVIDENCE],
    )

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=worker, reviewer=reviewer, store=store,
    )

    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert tuple(result["steps"]) == _EXPECTED_STEPS
    assert result["accepted"] is True
    assert result["integration"] == "git_merge"
    assert len(reviewer.calls) == 2
    assert reviewer.calls[0][0] == "fail"
    assert reviewer.calls[1][0] == "pass"
    store.close()


# ── 7. Failing re-review → terminal stop ───────────────────────────────────

def test_failing_re_review_stops_terminally(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "fail", "needs repair")

    worker = FakeWorker()
    reviewer = FakeReviewer(
        store, "run-1",
        verdicts=["fail", "fail"],
        evidence_list=[_FAIL_EVIDENCE, _FAIL_EVIDENCE],
    )

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=worker, reviewer=reviewer, store=store,
    )

    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert tuple(result["steps"]) == (
        STEP_READY_DISPATCH, STEP_INDEPENDENT_REVIEW,
        STEP_REPAIR, STEP_RE_REVIEW,
    )
    assert result["accepted"] is False
    assert result["integration"] == "none"
    assert result["released_dependents"] == []
    assert len(reviewer.calls) == 2
    store.close()


# ── 8. Missing required flag ───────────────────────────────────────────────

def test_missing_required_flag_is_usage_error(tmp_path):
    exit_code = nc.main(["--run-id", "run-1", "--db-path", _db(tmp_path)])
    assert exit_code == nc.EXIT_INVALID_INPUT


# ── 9. Non-positive attempt ────────────────────────────────────────────────

def test_non_positive_attempt_is_usage_error(tmp_path):
    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--attempt", "0",
         "--db-path", _db(tmp_path)],
    )
    assert exit_code == nc.EXIT_INVALID_INPUT


# ── 10. No worker adapter → refused fail-closed ────────────────────────────

def test_no_worker_adapter_is_refused(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        reviewer=_NoOpReviewer(),
        store=store,
    )
    assert exit_code == nc.EXIT_REFUSED
    assert "no production worker adapter" in capsys.readouterr().err
    store.close()


# ── 11. No reviewer adapter → refused fail-closed ──────────────────────────

def test_no_reviewer_adapter_is_refused(tmp_path, capsys):
    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", _db(tmp_path)],
        worker=FakeWorker(),
    )
    assert exit_code == nc.EXIT_REFUSED
    assert "no production reviewer adapter" in capsys.readouterr().err


# ── 12. Non-repository subject integrates via content transaction ──────────

def test_non_repository_subject_integrates_via_content_transaction(tmp_path, capsys):
    from skillweave.dispatch.work_contract import ConfigurationSubject

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    subject = ConfigurationSubject(config_id="cfg-1", environment="prod", version="v1")

    class ContentWorker:
        def execute(self, dispatch):
            return nc.AttemptExecution(
                producer_artifact="artifact:content",
                integration_subject=subject,
                baseline_digest="baseline:c",
                applied_digest="applied:c",
            )

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=ContentWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["integration"] == "content_transaction"
    assert tuple(result["steps"]) == (
        STEP_READY_DISPATCH, STEP_INDEPENDENT_REVIEW,
        STEP_CONTROLLED_INTEGRATION, STEP_DEPENDENT_WAVE,
    )
    store.close()


# ── 13. Content integration guarded: out-of-order attempt refused ──────────

def test_content_integration_out_of_order_refused(tmp_path):
    """Calling content_integration_advance when the intent is still at
    INTENT_LAUNCHED (before review) is refused by the guarded CAS.

    This exercises the bug fixed in SW-SCOPE-010: on 1f5cd17 the non-repo
    integration path called record_content_integration (unguarded) directly,
    so the intent silently moved to INTEGRATED from any state — this test
    fails (no exception raised).  After the fix the guarded
    content_integration_advance (CAS from INTENT_REVIEWED) raises
    IntentStateConflictError when the intent is not at INTENT_REVIEWED.
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")

    # Create the intent at LAUNCHED — the state before review.
    intent = store.record_dispatch_intent(
        "run-1", "task-a", 1, "run-1/task-a/1",
        step=STEP_READY_DISPATCH, state=INTENT_LAUNCHED,
    )

    svc = NativeProcessService(store, "test-holder-13")
    svc.acquire(ttl_seconds=60)
    try:
        with pytest.raises(IntentStateConflictError):
            svc.content_integration_advance(intent.intent_id)
    finally:
        svc.release()
    store.close()


# ── 14. Content integration guarded: repeat after success refused ──────────

def test_content_integration_repeat_after_success_is_refused(tmp_path, capsys):
    """After a successful content-transaction drive through main(),
    re-advancing the same intent (now at INTEGRATED) is refused.

    On 1f5cd17 the unguarded record_content_integration accepts the repeat
    silently — this test fails (no exception raised).  After the fix the
    guarded content_integration_advance (CAS from INTENT_REVIEWED) refuses
    because the intent is at INTEGRATED, not INTENT_REVIEWED.
    """
    from skillweave.dispatch.work_contract import ConfigurationSubject

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    subject = ConfigurationSubject(config_id="cfg-1", environment="prod", version="v1")

    class ContentWorker:
        def execute(self, dispatch):
            return nc.AttemptExecution(
                producer_artifact="artifact:content",
                integration_subject=subject,
                baseline_digest="baseline:c",
                applied_digest="applied:c",
            )

    # First drive succeeds through main() — intent is now at INTEGRATED.
    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=ContentWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["integration"] == "content_transaction"

    # The intent is at INTEGRATED; a repeat advance must be refused.
    intent = store.list_dispatch_intents(run_id="run-1")[0]
    assert intent.state == INTENT_INTEGRATED
    svc = NativeProcessService(store, "test-holder-14")
    svc.acquire(ttl_seconds=60)
    try:
        with pytest.raises(IntentStateConflictError):
            svc.content_integration_advance(intent.intent_id)
    finally:
        svc.release()
    store.close()


# ── 15. Content integration guarded: refused after failing re-review ───────

def test_content_integration_after_failing_re_review_is_refused(tmp_path, capsys):
    """After a failing re-review through main(), the intent is at FAILED;
    attempting content_integration_advance must be refused.

    On 1f5cd17 the unguarded record_content_integration accepts the
    integration silently even though the attempt was terminally stopped —
    this test fails (no exception raised).  After the fix the guarded
    content_integration_advance (CAS from INTENT_REVIEWED) refuses because
    the intent is at FAILED, not INTENT_REVIEWED.
    """
    from skillweave.dispatch.work_contract import ConfigurationSubject

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "fail", "needs repair")

    subject = ConfigurationSubject(config_id="cfg-1", environment="prod", version="v1")

    class ContentWorker:
        def execute(self, dispatch):
            return nc.AttemptExecution(
                producer_artifact="artifact:content",
                integration_subject=subject,
                baseline_digest="baseline:c",
                applied_digest="applied:c",
            )

    # Fail→fail drive: terminal stop, intent at FAILED.
    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=ContentWorker(),
        reviewer=FakeReviewer(
            store, "run-1", verdicts=["fail", "fail"],
            evidence_list=[_FAIL_EVIDENCE, _FAIL_EVIDENCE],
        ),
        store=store,
    )
    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["accepted"] is False
    assert result["integration"] == "none"

    # Intent at FAILED; a content integration advance must be refused.
    intent = store.list_dispatch_intents(run_id="run-1")[0]
    assert intent.state == INTENT_FAILED
    svc = NativeProcessService(store, "test-holder-15")
    svc.acquire(ttl_seconds=60)
    try:
        with pytest.raises(IntentStateConflictError):
            svc.content_integration_advance(intent.intent_id)
    finally:
        svc.release()
    store.close()


# ── 16. Content integration guarded: correct order succeeds ────────────────

def test_content_integration_guarded_correct_order_succeeds(tmp_path, capsys):
    """The guarded CAS advance from INTENT_REVIEWED succeeds when the
    intent is at the correct state — the happy path through main()."""
    from skillweave.dispatch.work_contract import ConfigurationSubject

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    subject = ConfigurationSubject(config_id="cfg-1", environment="prod", version="v1")

    class ContentWorker:
        def execute(self, dispatch):
            return nc.AttemptExecution(
                producer_artifact="artifact:content",
                integration_subject=subject,
                baseline_digest="baseline:c",
                applied_digest="applied:c",
            )

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=ContentWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["integration"] == "content_transaction"
    assert result["accepted"] is True
    store.close()


# ── 17. Stale-lease takeover with a strictly higher fencing token ──────────

def test_stale_lease_takeover_with_higher_token(tmp_path, capsys):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)

    holder = NativeProcessService(store, "stale-holder")
    holder.acquire(ttl_seconds=1)
    stale_token = holder.fencing_token
    holder.release()

    # Simulate a crashed controller whose lease expired: set released_at=NULL
    # and lease_until in the distant past.
    store._conn.execute(
        "UPDATE controller_lease SET lease_until = '2020-01-01T00:00:00', "
        "released_at = NULL WHERE lease_key = 'controller'"
    )
    store._conn.commit()

    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db,
         "--holder", "new-holder"],
        worker=FakeWorker(),
        reviewer=FakeReviewer(store, "run-1", ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )
    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["fencing_token"] > stale_token
    store.close()


# ── 14. Corrupt store (non-database file) ──────────────────────────────────

def test_corrupt_store_fails_cleanly(tmp_path, capsys):
    bad_db = tmp_path / "not-a-db.txt"
    bad_db.write_text("this is not a sqlite database")

    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", str(bad_db)],
        worker=FakeWorker(),
        reviewer=_NoOpReviewer(),
    )
    assert exit_code == nc.EXIT_FAILED
    assert capsys.readouterr().err != ""


# ── 15. Unwritable store path ──────────────────────────────────────────────

def test_unwritable_store_path_fails_cleanly(tmp_path):
    bad_path = str(tmp_path / "nonexistent" / "store.db")
    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", bad_path],
        worker=FakeWorker(),
        reviewer=_NoOpReviewer(),
    )
    assert exit_code == nc.EXIT_FAILED


# ── 16. Real subprocess SIGTERM releases the lease ─────────────────────────

def test_sigterm_releases_lease(tmp_path):
    """Launch the controller in a real subprocess with a blocking worker, send
    SIGTERM, and verify the lease is released afterwards."""
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "sigterm-test")
    _seed_review_record(store, "sigterm-test", "pass")
    store.close()

    repo_root = Path(__file__).resolve().parent.parent.parent
    src = str(repo_root / "src")

    driver = tmp_path / "sigterm_driver.py"
    driver.write_text(f'''
import sys, time
sys.path.insert(0, {src!r})
from skillweave.cli.native_controller import main, AttemptExecution, ReviewVerdict
from skillweave.runtime.store import SQLiteRunStore

class SlowWorker:
    def execute(self, dispatch):
        time.sleep(30)
        return AttemptExecution(producer_artifact="artifact:test")

class PassReviewer:
    def review(self, dispatch, execution):
        return ReviewVerdict(
            "pass",
            evidence=({{"schema_version": "independent-gate-receipt/1.0.0",
                      "evidence_type": "member",
                      "verifier_identity": "reviewer-1",
                      "verdict": "PASS"}},),
            verifier_identity="reviewer-1",
        )

store = SQLiteRunStore({db!r})
sys.exit(main(
    ["--run-id", "sigterm-test", "--task-id", "task-sig", "--db-path", {db!r}],
    worker=SlowWorker(),
    reviewer=PassReviewer(),
    store=store,
))
''')

    proc = subprocess.Popen(
        [sys.executable, str(driver)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Give the subprocess time to acquire the lease and enter the worker.
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # The lease must be released: a new controller can acquire it.
    store2 = SQLiteRunStore(db)
    try:
        svc = NativeProcessService(store2, "post-sigterm")
        svc.acquire(ttl_seconds=60)
        assert svc.fencing_token > 0
        svc.release()
    finally:
        store2.close()


# ── The production worker port (criterion 1) ───────────────────────────────
#
# ``build_production_ports`` is the production caller the criterion names: it
# composes the *worker* port that launches the owned attempt through the
# existing dispatch application inline seam, with a server-issued scoped grant
# and a verified captured child identity, and records the answering gateway
# endpoint/route/upstream model.  Nothing here launches a real process or opens
# a socket: the runner adapter's ``start_process`` is replaced with a fake and
# the reachability probe and answer reader are injected, so every assertion is
# about the port's own refusal/recording decision, not about a provider.

_APPROVED_ROUTE = "faigate/deepseek-v4"


def _write_route_policy(tmp_path: Path) -> str:
    """The *operator* route policy the port reads its authority from.

    The port never defines the approved set; this file is the operator
    configuration stand-in, naming the approved gateway the port must probe and
    the single approved route.
    """
    path = tmp_path / "route-policy.json"
    path.write_text(
        json.dumps(
            {
                "schema": "route-policy-v1",
                "required_revision": "1.3.11",
                "approved_gateway": {"host": "127.0.0.1", "port": 8090},
                "approved_routes": [_APPROVED_ROUTE],
            }
        )
    )
    return str(path)


class _FakePortHandle:
    """A live-process stand-in for the inline seam.

    ``_sw_effective_env`` is the child's *observed* effective configuration: the
    dispatch seam hands it to ``verify_child_launch``, which refuses a child
    that did not resolve the granted process service.  ``pid`` is the pid the
    worker port captures and verifies; the tests set it to the live test
    process so the capture/verify pair sees a genuinely matching process, and to
    ``0`` to exercise the unobservable-identity refusal.
    """

    def __init__(self, *, pid: int, effective_env: dict, stdout: bytes = b"answer"):
        self.pid = pid
        self._sw_effective_env = dict(effective_env)
        self._stdout = stdout
        self._stderr = b""

    def wait(self, timeout=None):
        class _Result:
            exit_code = 0
            signal = None
            termination = "exited"
            stdout = b""
            stderr = b""
            stdout_receipt = None
            stderr_receipt = None

            @property
            def succeeded(self):
                return True

        _Result.stdout = self._stdout
        _Result.stderr = self._stderr
        return _Result()


class _ProductionPortFixture:
    """Builds the production worker port over a durable store + operator policy.

    ``runner_adapter.start_process`` is replaced by a fake that records the
    command/model it was handed and returns a :class:`_FakePortHandle` carrying
    the child's observed effective environment (which the fake computes from the
    very ``env``/``env_allowlist`` the seam passed it, so the grant verification
    is a real round trip through the child environment, never a constant).
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.db = str(tmp_path / "store.db")
        self.mailbox = str(tmp_path / "agent-mailbox.jsonl")
        self.artifacts = str(tmp_path / "artifacts")
        self.stream_root = str(tmp_path / "streams")
        self.policy = _write_route_policy(tmp_path)
        self.store = SQLiteRunStore(self.db)
        self.recorded: list = []
        self._monkeypatch = None

    def install(self, monkeypatch, *, child_pid: int) -> None:
        import skillweave.runtime.runner_adapter as runner_adapter

        self._monkeypatch = monkeypatch
        fixture = self

        class _FakeRunner:
            def start_process(self, command, *, env=None, env_allowlist=None, **kw):
                fixture.recorded.append(
                    {"command": list(command), "kw": dict(kw), "env": dict(env or {})}
                )
                return _FakePortHandle(pid=child_pid, effective_env=dict(env or {}))

        monkeypatch.setattr(runner_adapter, "start_process", _FakeRunner().start_process)

    def authority(self) -> MCPPrincipalAuthority:
        from skillweave.mcp import server as mcp_server

        auth = MCPPrincipalAuthority()
        # ``bind_authority`` writes *process-wide* module state. Register the
        # global with this test's ``monkeypatch`` first, so pytest hands the
        # host's previous authority back at teardown. Binding it directly (the
        # pre-fix form) leaked the authority into later, unrelated MCP test
        # modules, which then resolved a stale principal and failed.
        self._monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        mcp_server.bind_authority(auth)
        return auth

    def build(self, *, model: str, endpoint: str, answer_reader=None, probe=None,
              authority=None) -> object:
        return nc.build_production_ports(
            store=self.store,
            command=["true"],
            subject_repo="owner/repo",
            subject_commit="c0ffee",
            tool="worker",
            model=model,
            endpoint=endpoint,
            route=_APPROVED_ROUTE,
            answer_reader=answer_reader,
            reachability_probe=probe if probe is not None else (lambda h, p: True),
            authority=authority if authority is not None else self.authority(),
            policy_path=self.policy,
            db_path=self.db,
            mailbox_path=self.mailbox,
            artifacts_path=self.artifacts,
            stream_root=self.stream_root,
            timeout=5.0,
        )

    def dispatch(self, **over):
        base = {
            "run_id": "run-1",
            "task_id": "task-a",
            "attempt": 1,
            "route": _APPROVED_ROUTE,
        }
        base.update(over)
        return SimpleNamespace(**base)


class TestProductionWorkerPort:
    def test_success_records_answering_endpoint_route_and_upstream_model(
        self, tmp_path, monkeypatch
    ):
        """The success path: launch through the real inline seam, verify the
        child's captured identity, and record the *observed* answer durably."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="deepseek-v4-pro"
            ),
        )

        execution = port.execute(fx.dispatch())

        # The seam was reached with the *requested* model, and a grant was
        # provisioned into the child environment.
        assert len(fx.recorded) == 1
        assert fx.recorded[0]["kw"]["model"] == _APPROVED_ROUTE
        assert "SKILLWEAVE_MCP_SESSION_TOKEN" in fx.recorded[0]["env"]
        assert execution.producer_artifact.startswith("sha256:")

        # The receipt is durable, run-scoped, and carries the observed answer.
        evidence = fx.store.list_evidence(run_id="run-1")
        assert len(evidence) == 1
        receipt = evidence[0].metadata["receipt"]
        assert receipt["gateway_endpoint"] == "http://127.0.0.1:8090"
        assert receipt["route"] == _APPROVED_ROUTE
        assert receipt["upstream_model"] == "deepseek-v4-pro"
        assert receipt["requested_model"] == _APPROVED_ROUTE
        assert receipt["endpoint_verified"] is True
        assert receipt["answering_identity"] == (
            f"http://127.0.0.1:8090|{_APPROVED_ROUTE}|deepseek-v4-pro"
        )
        # The identity recorded is the verified one (pid + start_time), not a
        # bare PID.
        assert receipt["process_identity"]["pid"] == os.getpid()
        assert receipt["process_identity"]["start_time"]
        fx.store.close()

    def test_unconfigured_endpoint_refuses_named_hold_and_records_nothing(
        self, tmp_path, monkeypatch
    ):
        """No operator endpoint configuration -> the port never assumes the
        default gateway: a named hold, and nothing recorded."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        for key in ("FAIGATE_BASE_URL", "FAIGATE_HOST", "FAIGATE_PORT"):
            monkeypatch.delenv(key, raising=False)
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="deepseek-v4-pro"
            ),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch())
        assert "unconfigured" in str(excinfo.value)
        assert nc.HOLD_GATEWAY_UNCONFIGURED in str(excinfo.value)
        # Nothing launched, nothing recorded.
        assert fx.recorded == []
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()

    def test_unreachable_endpoint_refuses_named_hold_and_records_nothing(
        self, tmp_path, monkeypatch
    ):
        """A configured approved gateway that does not accept a connection is a
        named hold, not a launch against a dead endpoint."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="deepseek-v4-pro"
            ),
            probe=lambda host, port: False,
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch())
        assert nc.HOLD_GATEWAY_UNREACHABLE in str(excinfo.value)
        assert fx.recorded == []
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()

    def test_fallback_capable_model_refuses_named_hold_and_records_nothing(
        self, tmp_path, monkeypatch
    ):
        """A model whose resolved target is the gateway's fallback pool is a
        named hold *before* launch: a masked fallback never becomes a receipt."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        port = fx.build(
            model="faigate/llama-4",  # resolves to ``openrouter-fallback``
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="some-other-model"
            ),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch())
        assert nc.HOLD_GATEWAY_FALLBACK_CAPABLE in str(excinfo.value)
        assert fx.recorded == []
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()

    def test_observed_answer_that_substitutes_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """The answer reader observes a *different* upstream than the one
        requested: a masked substitution is a named hold at the port."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="anthropic-sonnet", served_by="anthropic-sonnet"
            ),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch())
        assert nc.HOLD_GATEWAY_FALLBACK_CAPABLE in str(excinfo.value)
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()

    def test_unobservable_child_identity_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """A child whose process identity cannot be captured is a named hold:
        the port never records an unverified child."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=0)
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="deepseek-v4-pro"
            ),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch())
        assert nc.HOLD_IDENTITY_UNVERIFIED in str(excinfo.value)
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()

    def test_unobserved_answer_refuses_named_hold(self, tmp_path, monkeypatch):
        """The answer reader observed no upstream model: the port refuses rather
        than recording the *requested* model as the answering one."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch())
        assert nc.HOLD_ANSWER_UNOBSERVED in str(excinfo.value)
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()

    def test_route_outside_the_operator_policy_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """A route the operator policy does not approve is a named hold: the
        port never defines the approved set itself."""
        fx = _ProductionPortFixture(tmp_path)
        fx.install(monkeypatch, child_pid=os.getpid())
        port = fx.build(
            model=_APPROVED_ROUTE,
            endpoint="http://127.0.0.1:8090",
            answer_reader=lambda dispatch, child: nc.GatewayAnswer(
                upstream_model="deepseek-v4-pro"
            ),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.execute(fx.dispatch(route="faigate/unapproved-route"))
        assert nc.HOLD_ROUTE_UNAPPROVED in str(excinfo.value)
        assert fx.recorded == []
        assert fx.store.list_evidence(run_id="run-1") == []
        fx.store.close()


# ── The production reviewer port (criterion 2) ─────────────────────────────
#
# ``build_production_reviewer_port`` is the production caller the criterion
# names: it composes the *reviewer* port whose verdict is the one the review
# tool (``skillweave.mcp.server.review_action``) wrote into the durable mailbox
# under a **server-issued reviewer principal**, read back by the controller
# from that durable record -- never synthesised by the port.  Nothing here
# launches a real process or opens a socket: the review tool seam is injected
# (or driven through the real MCP tool over a temp mailbox) so every assertion
# is about the port's own independence/read-back decision, not about a provider.

_WORKER_ROUTE = "faigate/deepseek-v4"
_REVIEWER_ROUTE = "faigate/anthropic-sonnet"


def _write_review_route_policy(tmp_path: Path, *, routes=None) -> str:
    """The *operator* route policy, approving the worker and reviewer routes.

    The reviewer port never defines the approved set itself; this file is the
    operator configuration stand-in naming the approved routes the reviewer
    port must be in.  A reviewer route absent from it is refused by name.
    """
    path = tmp_path / "route-policy.json"
    approved = list(routes if routes is not None else (_WORKER_ROUTE, _REVIEWER_ROUTE))
    path.write_text(
        json.dumps(
            {
                "schema": "route-policy-v1",
                "required_revision": "1.3.11",
                "approved_gateway": {"host": "127.0.0.1", "port": 8090},
                "approved_routes": approved,
            }
        )
    )
    return str(path)


class _ReviewerPortFixture:
    """Builds the production reviewer port over a durable store + live mailbox.

    The MCP authority is bound to the process and the server-owned mailbox is
    pointed at a temp path, so both the real ``review_action`` tool **and** the
    port's read-back resolve against the *same* durable mailbox.  ``build``
    passes ``reviewer_route`` distinct from ``worker_route`` unless a test
    overrides it.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.db = str(tmp_path / "store.db")
        self.mailbox = str(tmp_path / "agent-mailbox.jsonl")
        self.artifacts = str(tmp_path / "artifacts")
        self.stream_root = str(tmp_path / "streams")
        self.policy = _write_review_route_policy(tmp_path)
        self.store = SQLiteRunStore(self.db)

    def authority(self, monkeypatch) -> MCPPrincipalAuthority:
        from skillweave.mcp import server as mcp_server

        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, self.mailbox)
        auth = MCPPrincipalAuthority()
        # Both of these are *process-wide* module state: register the previous
        # values with ``monkeypatch`` so pytest restores the host's authority
        # and ready token at teardown. Setting them directly (the pre-fix form)
        # leaked a foreign authority (and a cleared token) into later MCP test
        # modules, which then resolved a stale principal and failed.
        monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        monkeypatch.setattr(mcp_server, "_READY_SESSION_TOKEN", None)
        mcp_server.bind_authority(auth)
        return auth

    def build(self, *, reviewer_route=_REVIEWER_ROUTE, worker_route=_WORKER_ROUTE,
              policy_path=None, mailbox_path=None, authority=None,
              review_writer=None) -> object:
        return nc.build_production_reviewer_port(
            store=self.store,
            subject_repo="owner/repo",
            subject_commit="c0ffee",
            worker_route=worker_route,
            reviewer_route=reviewer_route,
            authority=authority,
            policy_path=policy_path if policy_path is not None else self.policy,
            db_path=self.db,
            mailbox_path=self.mailbox if mailbox_path is None else mailbox_path,
            artifacts_path=self.artifacts,
            stream_root=self.stream_root,
            review_writer=review_writer,
        )

    def dispatch(self, **over):
        base = {
            "run_id": "run-1",
            "task_id": "task-a",
            "attempt": 1,
            "route": _WORKER_ROUTE,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def execution(self, artifact: str = "artifact:produced") -> object:
        return nc.AttemptExecution(producer_artifact=artifact)


class TestProductionReviewerPort:
    def test_independent_review_recorded_under_server_principal_is_read_back(
        self, tmp_path, monkeypatch
    ):
        """The success path: the review tool writes the result durably under a
        server-issued reviewer principal, and the controller reads the verdict
        back *from that durable record* -- it never authors it."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        # The real ``review_action`` tool is the reviewer (no fake): the port
        # mints a server-issued reviewer grant, drives the tool, reads back.
        port = fx.build(authority=auth)

        verdict = port.review(fx.dispatch(), fx.execution())

        assert verdict.verdict == "pass"
        assert verdict.published is True
        # The answering identity is the *server-issued* reviewer principal, not
        # a caller string and never the producer.
        assert verdict.verifier_identity.startswith("mcp:reviewer:")
        assert len(verdict.evidence) == 1
        member = verdict.evidence[0]
        assert member["evidence_type"] == "member"
        assert member["verdict"] == "PASS"
        assert member["verifier_identity"] == verdict.verifier_identity
        # A real independent verifier receipt (identity != producer) decides
        # the gate PASS.
        from skillweave.trace import gate_receipt as gr

        decision = gr.evaluate_gate(
            gr.AttemptAcceptance(
                attempt_id="intent-1",
                state=gr.ACCEPTANCE_REVIEWED,
                producer_artifact="artifact:produced",
            ),
            evidence=verdict.evidence,
            producer_identity="worker-producer",
        )
        assert decision.verdict == gr.GATE_PASS
        assert decision.accepted is True
        fx.store.close()

    def test_reviewer_route_equal_to_worker_route_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """A reviewer route equal to the worker route is refused by name: the
        independent review never runs on the producer's own route."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        port = fx.build(reviewer_route=_WORKER_ROUTE, authority=auth)

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(), fx.execution())
        assert nc.HOLD_REVIEWER_ROUTE_NOT_INDEPENDENT in str(excinfo.value)
        fx.store.close()

    def test_unconfigured_reviewer_route_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """No reviewer route configured (no operator value, no env) is a named
        hold: the port never defaults the reviewer onto the worker route."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        monkeypatch.delenv(nc._ENV_REVIEWER_ROUTE, raising=False)
        port = fx.build(reviewer_route="", worker_route="", authority=auth)

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(route=""), fx.execution())
        assert nc.HOLD_REVIEWER_ROUTE_UNCONFIGURED in str(excinfo.value)
        fx.store.close()

    def test_unapproved_reviewer_route_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """A reviewer route the operator policy does not approve is a named
        hold: the port never defines the approved set itself."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        port = fx.build(reviewer_route="faigate/unapproved", authority=auth)

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(), fx.execution())
        assert nc.HOLD_REVIEWER_ROUTE_UNAPPROVED in str(excinfo.value)
        fx.store.close()

    def test_mailbox_mismatch_refuses_named_hold(self, tmp_path, monkeypatch):
        """A reviewer port pointed at a mailbox other than the server-owned one
        is a named hold: a review is only ever read back from the record the
        review tool actually wrote."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        port = fx.build(
            mailbox_path=str(tmp_path / "some-other-mailbox.jsonl"), authority=auth
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(), fx.execution())
        assert nc.HOLD_REVIEW_MAILBOX_MISMATCH in str(excinfo.value)
        fx.store.close()

    def test_unrecorded_review_refuses_named_hold(self, tmp_path, monkeypatch):
        """A review tool that records nothing (a refusal) is a named hold: the
        port never invents the reviewer's verdict."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        port = fx.build(
            authority=auth,
            review_writer=lambda **kw: json.dumps(
                {"status": "refused", "code": "unauthenticated_principal"}
            ),
        )

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(), fx.execution())
        assert nc.HOLD_REVIEW_UNRECORDED in str(excinfo.value)
        fx.store.close()

    def test_review_answering_a_different_attempt_refuses_named_hold(
        self, tmp_path, monkeypatch
    ):
        """A durable review that answers a *different* attempt is refused by
        name: the controller never binds a verdict to the wrong work."""
        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        from skillweave.mcp import server as mcp_server

        def _wrong_attempt_writer(**kw):
            # A genuine server-recorded review -- but for attempt 2.
            return mcp_server.review_action(
                action="write",
                recipient=kw["recipient"],
                body=kw["body"],
                run_id=kw["run_id"],
                task_id=kw["task_id"],
                attempt=2,
                verdict="pass",
                subject_revision=kw["subject_revision"],
                artifact_digest=kw["artifact_digest"],
                criteria=kw["criteria"],
                session_token=kw["session_token"],
            )

        port = fx.build(authority=auth, review_writer=_wrong_attempt_writer)

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(attempt=1), fx.execution())
        assert nc.HOLD_REVIEW_UNVERIFIED in str(excinfo.value)
        fx.store.close()

    def test_a_producer_cannot_resolve_as_its_own_reviewer(
        self, tmp_path, monkeypatch
    ):
        """Adversarial: a producer that posts its own ``status`` review -- even
        one *claiming* the reviewer role -- must not resolve as an independent
        review, because the recorded principal is the worker's, not a
        server-issued reviewer's.

        This is the criterion-2 independence refusal: the durable record's
        *server-issued principal* is what is trusted, never a claimed role.  On
        a base without a production reviewer port this path is unreachable and
        the forged self-review would be taken at face value, so the test fails
        there.
        """
        from skillweave.dispatch import messaging as _messaging
        from skillweave.runtime.native_process import (
            _server_issued_principal_role,
        )

        fx = _ReviewerPortFixture(tmp_path)
        auth = fx.authority(monkeypatch)
        # The forged record carries the worker's own principal; the server-issued
        # role it derives is "worker", never the reviewer role it claims.
        forged_principal = "mcp:worker:native-controller-worker"
        assert _server_issued_principal_role(forged_principal) == "worker"

        def _forge_self_review(**kw):
            mailbox = _messaging.AgentMailbox(fx.mailbox)
            message = _messaging.AgentMessage(
                kind="status",
                sender="mcp:worker:native-controller-worker",
                recipient="mcp:worker:native-controller-worker",
                principal="mcp:worker:native-controller-worker",
                run_id=kw["run_id"],
                task_id=kw["task_id"],
                attempt=kw["attempt"],
                body={
                    "assignment": kw["body"],
                    "run_id": kw["run_id"],
                    "task_id": kw["task_id"],
                    "attempt": kw["attempt"],
                    # The producer *claims* the reviewer role in the body ...
                    "role": "reviewer",
                    "verdict": "pass",
                    "subject_revision": kw["subject_revision"],
                    "artifact_digest": kw["artifact_digest"],
                    "criteria": [],
                },
            )
            record = mailbox.post(message)
            return json.dumps(
                {"status": "ok", "action": "write", "message": record}
            )

        port = fx.build(authority=auth, review_writer=_forge_self_review)

        with pytest.raises(nc._RefusedError) as excinfo:
            port.review(fx.dispatch(), fx.execution())
        # The claimed "reviewer" role is not believed: the worker principal is
        # not server-issued as a reviewer, so no review is recorded.
        assert nc.HOLD_REVIEW_UNRECORDED in str(excinfo.value)
        fx.store.close()


# ── Isolation of the process-wide MCP authority (regression) ───────────────
#
# The production-port fixtures bind the *process-wide* MCP authority
# (``mcp.server._AUTHORITY``) -- the seam the port's own grant issuance reads --
# and must hand it back when the test ends. Setting it directly leaves a
# foreign authority bound in the test process: a later, unrelated MCP test
# module (test_1511_mcp_bootstrap, test_1510_mcp_auth) then resolves a stale
# principal and fails, while the same files pass alone or in reverse order.
# These two tests pin the invariant the fixtures must preserve.


def test_binding_the_production_authority_does_not_leak_it(tmp_path, monkeypatch):
    """The worker-port fixture restores the host authority at teardown.

    A foreign authority left bound in the process is what failed the combined
    three-file run; this fails on the base revision, where the fixture bound it
    directly with no restore.
    """
    from skillweave.mcp import server as mcp_server

    previous_authority = mcp_server._AUTHORITY
    fixture = _ProductionPortFixture(tmp_path)
    fixture.install(monkeypatch, child_pid=os.getpid())
    assert mcp_server._bound_authority() is None

    fixture.authority()
    # While the fixture is live the bound authority is its own ...
    assert mcp_server._bound_authority() is not None
    fixture.store.close()

    # ... and at teardown the host's previous authority returns.
    monkeypatch.undo()
    assert mcp_server._AUTHORITY is previous_authority


def test_reviewer_port_fixture_restores_the_host_authority_and_ready_token(
    tmp_path, monkeypatch
):
    """Both process-wide globals are restored, never left mutated.

    ``_READY_SESSION_TOKEN`` is set to ``None`` by the reviewer fixture and the
    authority is bound; neither may survive the test.
    """
    from skillweave.mcp import server as mcp_server

    previous_authority = mcp_server._AUTHORITY
    previous_ready = mcp_server._READY_SESSION_TOKEN

    fixture = _ReviewerPortFixture(tmp_path)
    fixture.authority(monkeypatch)
    assert mcp_server._AUTHORITY is not previous_authority

    # Pytest restores both registered globals; simulate teardown.
    monkeypatch.undo()
    assert mcp_server._AUTHORITY is previous_authority
    assert mcp_server._READY_SESSION_TOKEN is previous_ready
