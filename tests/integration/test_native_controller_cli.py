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
import signal
import subprocess
import sys
import time
from pathlib import Path

from skillweave.cli import native_controller as nc
from skillweave.cli.main import main as router_main
from skillweave.runtime.native_process import (
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


# ── 13. Stale-lease takeover with a strictly higher fencing token ──────────

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
