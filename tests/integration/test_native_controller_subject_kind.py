"""SW-SCOPE-010 unit C, criterion 1: the integration surface is discriminated
from the subject **kind**, not from the mere presence of a subject.

The single production caller under test is the CLI entry point
:func:`skillweave.cli.native_controller.main` (reached through the controller's
step 5, "controlled integration").  Three branches, each with one adversarial
test:

* a **non-repository** subject runs through the controller with the guarded
  content integration (``content_integration_advance`` +
  ``record_content_integration``) and a CMS-shaped content transaction;
* a **repository** subject still integrates through Git (the Git merge
  record), never through the content transaction surface;
* a subject of **unknown kind** is refused **by name**
  (:data:`~skillweave.cli.native_controller.HOLD_SUBJECT_KIND_UNKNOWN`), with
  exit code ``EXIT_REFUSED`` and *no* durable integration left behind.

Every test drives the *real* entry point (a ``SQLiteRunStore`` on a temp path,
a fake worker whose only job is to hand the attempt an integration subject, and
a reviewer that supplies independent verifier evidence).  No provider, gateway,
network or CMS call is made.  The process-wide state the controller touches
(``SIGTERM``/``SIGINT`` handlers installed by the lease guard, the store
connection) is restored in fixtures so the tests are order-independent.
"""

from __future__ import annotations

import datetime
import json
import signal

import pytest

from skillweave.cli import native_controller as nc
from skillweave.dispatch.work_contract import ConfigurationSubject, RepositorySubject
from skillweave.runtime.native_process import (
    INTENT_INTEGRATED,
    INTENT_LAUNCHED,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
)
from skillweave.runtime.store import SQLiteRunStore

_PASS_EVIDENCE = (
    {
        "schema_version": "independent-gate-receipt/1.0.0",
        "evidence_type": "member",
        "verifier_identity": "reviewer-1",
        "verdict": "PASS",
    },
)


# ── Fixtures (restore process-wide state) ───────────────────────────────────


@pytest.fixture
def restore_signal_handlers():
    """Snapshot and restore the process-wide SIGTERM/SIGINT handlers.

    The controller's lease guard installs handlers for the duration of a drive.
    Restoring them afterwards keeps a later test (or the runner) from inheriting
    a displaced handler regardless of test order.
    """
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


@pytest.fixture
def store(tmp_path):
    """A real ``SQLiteRunStore`` on a temp path, closed on teardown."""
    s = SQLiteRunStore(str(tmp_path / "store.db"))
    try:
        yield s
    finally:
        s.close()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _seed_run(store: SQLiteRunStore, run_id: str) -> None:
    store.create_run(run_id)
    record = store.get_run(run_id)
    store.transition(
        run_id, "batch_selection", record.state, record.version, reason="test setup",
    )


def _seed_review_record(store: SQLiteRunStore, run_id: str, verdict: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    store._conn.execute(
        "INSERT INTO transitions_log "
        "(run_id, from_state, to_state, reason, version, timestamp, role) "
        "VALUES (?, 'batch_selection', 'advance_or_stop', ?, 1, ?, 'reviewer')",
        (run_id, f"Review verdict {verdict}: ok", now),
    )
    store._conn.commit()


class _EvenKindWorker:
    """The launch seam: hands the attempt the subject under test.

    ``subject`` is exactly what the adapter supplies to the controller; the
    discrimination under test is what the *controller* does with it.  A
    ``subject`` of ``None`` reaches the controller as the worker's execution
    record; here we model an unknown kind by an object whose ``kind`` is not in
    the generic contract's vocabulary.
    """

    def __init__(self, subject):
        self.subject = subject

    def execute(self, dispatch):
        return nc.AttemptExecution(
            producer_artifact="artifact:produced",
            integration_subject=self.subject,
            baseline_digest="baseline:x",
            applied_digest="applied:x",
        )


def _result(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(out)


def _drive(store, run_id, worker, *, capsys=None, argv=None):
    """Run the real entry point with a fake worker and a passing reviewer."""
    from tests.integration.test_native_controller_cli import FakeReviewer

    return nc.main(
        argv
        or ["--run-id", run_id, "--task-id", "task-a", "--db-path", store.db_path],
        worker=worker,
        reviewer=FakeReviewer(store, run_id, ["pass"], [_PASS_EVIDENCE]),
        store=store,
    )


# ── 1. Non-repository subject -> guarded content integration ────────────────


def test_non_repository_subject_uses_content_integration(
    store, capsys, restore_signal_handlers
):
    """A ``configuration`` subject integrates through the guarded content path.

    Positive pin (not a red proof): base and fix agree that a non-repository
    subject integrates through the content transaction, so this test passes on
    the base afdf20d as well.  It guards the branch against a *future* change
    that would route a discriminated non-repository kind anywhere else, and it
    reads the persisted ``content-transaction-v1`` record back from durable
    state.  The red proofs for this criterion are the repository branch
    (test 2) and the unknown-kind refusals (tests 3 and 4).
    """
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    subject = ConfigurationSubject(config_id="cfg-1", environment="sandbox", version="v1")
    exit_code = _drive(store, "run-1", _EvenKindWorker(subject))

    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["integration"] == "content_transaction"
    assert tuple(result["steps"]) == (
        STEP_READY_DISPATCH, STEP_INDEPENDENT_REVIEW,
        STEP_CONTROLLED_INTEGRATION, STEP_DEPENDENT_WAVE,
    )
    # The durable content transaction exists and is verified by its own readback.
    intent = store.list_dispatch_intents(run_id="run-1")[0]
    assert intent.state == INTENT_INTEGRATED
    txn = store.get_content_transaction("run-1", f"run-1/{intent.intent_id}")
    assert txn is not None
    assert txn["subject_kind"] == "configuration"
    assert txn["readback_digest"] == txn["applied_digest"]


# ── 2. Repository subject -> Git merge, never the content surface ───────────


def test_repository_subject_integrates_through_git_not_content(
    store, capsys, restore_signal_handlers
):
    """A repository subject integrates through Git.

    Adversarial on the base afdf20d: the base routes every non-``None`` subject
    through the content transaction surface, so an explicit *repository* subject
    would be sent to ``content_integration_advance``/``record_content_integration``
    and ``record_content_integration`` would refuse it as a wrong integration
    mode (a crash, ``EXIT_FAILED``) rather than integrating it through Git.  This
    test drives the real entry point with a repository subject and proves the
    Git surface is selected and the intent integrates.
    """
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    subject = RepositorySubject(repo="owner/repo", commit="c0ffee" * 6 + "1234")
    exit_code = _drive(store, "run-1", _EvenKindWorker(subject))

    assert exit_code == nc.EXIT_OK
    result = _result(capsys)
    assert result["integration"] == "git_merge"
    assert result["accepted"] is True
    intent = store.list_dispatch_intents(run_id="run-1")[0]
    assert intent.state == INTENT_INTEGRATED
    # No content transaction was written for a repository subject.
    assert store.get_content_transaction("run-1", f"run-1/{intent.intent_id}") is None


# ── 3. Unknown subject kind -> refused by name, no durable integration ──────


def test_unknown_subject_kind_is_refused_by_name(
    store, capsys, restore_signal_handlers
):
    """A subject of unknown kind is refused by name before any mutation.

    Adversarial on the base afdf20d: the base never discriminates the kind, so
    an unknown-kind subject carrying a non-``None`` ``integration_subject`` is
    routed through the content surface and fails late (an untyped crash,
    ``EXIT_FAILED``) *after* the review transitions have advanced the intent --
    or, for a ``None``-ish subject, silently integrates through Git.  This test
    pins the *named* refusal: ``EXIT_REFUSED``, the hold is named, and the intent
    is left exactly at its pre-integration (reviewed) state with no content
    transaction written.
    """
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    class _UnknownKind:
        kind = "not-a-discriminated-kind"

    exit_code = _drive(store, "run-1", _EvenKindWorker(_UnknownKind()))
    err = capsys.readouterr().err

    assert exit_code == nc.EXIT_REFUSED
    assert nc.HOLD_SUBJECT_KIND_UNKNOWN in err
    # The refusal happened *before* the integration transition: the intent is
    # left at the reviewed state, never integrated through either surface.
    intent = store.list_dispatch_intents(run_id="run-1")[0]
    assert intent.state == "reviewed"
    assert store.get_content_transaction("run-1", f"run-1/{intent.intent_id}") is None


def test_unknown_subject_kind_mapping_is_refused_by_name(
    store, capsys, restore_signal_handlers
):
    """A mapping subject with an unknown ``kind`` is refused by name too.

    The discriminator reads ``kind`` from an object *or* a mapping, so a raw
    payload cannot smuggle an unknown kind past the refusal.
    """
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")

    exit_code = _drive(store, "run-1", _EvenKindWorker({"kind": "surprise"}))
    err = capsys.readouterr().err

    assert exit_code == nc.EXIT_REFUSED
    assert nc.HOLD_SUBJECT_KIND_UNKNOWN in err
