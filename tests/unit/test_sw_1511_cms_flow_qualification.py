"""SW-1511-CMS-FLOWS: the declared CMS paths have a production consumer.

The frozen declaration module (:mod:`skillweave.blueprint.cms_path_qualification`)
shipped with no production caller -- it was imported only by its own test, so
"the five CMS paths exist" held at library level and nowhere else. This test
binds the *consumer* that closes that gap:

* :func:`skillweave.runtime.cms_flow_qualification.qualify_cms_path` takes a
  :class:`~skillweave.blueprint.cms_path_qualification.CmsPath` from
  ``INITIAL_CMS_PATHS`` and produces a
  :class:`~skillweave.blueprint.cms_path_qualification.CmsProcessQualification`
  through the *existing* native controller seam;
* the flow is reachable from the **real production entry point** -- the
  ``skillweave native-controller`` command, via its additive ``--qualify-cms-path``
  flag -- not only from the library.

Every assertion of behaviour is a **fake at the process boundary**: the dummy
:class:`~skillweave.runtime.native_process.RawContentAdapter` over in-memory
content and a declared sandbox binding. No provider, gateway or CMS call is made
and no network is touched.

On the base SHA (bdb1c6c) the module under test does not exist, so this file
fails at import -- the red proof that the consumer is new.
"""

from __future__ import annotations

import json

from skillweave.blueprint.cms_path_qualification import (
    DISPOSITION_NOT_PERFORMED,
    INITIAL_CMS_PATHS,
    PATH_LANDING_PAGE,
    SCHEMA_VERSION,
    CmsPath,
)
from skillweave.cli import native_controller as nc
from skillweave.runtime.cms_flow_qualification import (
    CMS_QUALIFICATION_SANDBOX_TARGET,
    CmsFlowQualificationRefused,
    qualify_cms_path,
)
from skillweave.runtime.native_process import INTENT_CLAIMING_STATES, INTENT_FAILED
from skillweave.runtime.store import SQLiteRunStore, RunStateModel


def _store(tmp_path) -> SQLiteRunStore:
    return SQLiteRunStore(db_path=str(tmp_path / "store.db"))


def _declared_path() -> CmsPath:
    """The landing-page path, taken from the *frozen* five-path declaration."""
    return INITIAL_CMS_PATHS[1]


# ── the library consumer ────────────────────────────────────────────────────


def test_qualify_cms_path_drives_the_lifecycle_and_reads_it_back(tmp_path):
    """A declared path is driven end-to-end and read back from durable state."""
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, _declared_path(), run_id="run-qual", task_id="task-qual"
        )
        # The path is the *frozen* declaration, not a caller invention.
        assert result.path is _declared_path()
        assert result.path.path_id == PATH_LANDING_PAGE

        # The transaction genuinely ran the reversible lifecycle to its end.
        assert result.final_stage == "restored"

        # The readback and restore verdicts come from the *persisted* record.
        persisted = store.get_cms_transaction(result.run_id, result.txn_id)
        assert persisted is not None
        assert persisted["stage"] == "restored"
        assert persisted["verified"] is True
        assert persisted["restored"] is True

        q = result.qualification
        assert q.artifact == SCHEMA_VERSION
        assert q.readback_verified is True
        assert q.restore_verified is True
        # The producer is *derived* from the owned attempt, never declared here.
        assert q.producer == persisted["producer_identity"]
        assert q.producer.startswith("attempt:")
        # Source-bound pins are the real, persisted hashes.
        assert q.pins["source_digest"] == persisted["baseline_hashes"]["semantic"]
    finally:
        store.close()


def test_qualification_stays_not_performed_and_names_what_it_did_not_measure(tmp_path):
    """Only three of the six required outcomes are measured, so it stays honest.

    A ``performed`` claim requires all six measurable outcomes; this consumer
    measures only ``success``, ``failure`` and ``cancel``, so it must not forge
    the rest -- it returns a ``not-performed`` record carrying the measured
    evidence and a named limitation, never a prewritten receipt.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, "cms-generic-request", run_id="run-np", task_id="task-np"
        )
        q = result.qualification
        assert q.disposition == DISPOSITION_NOT_PERFORMED
        assert q.is_performed is False
        # It genuinely measured the success, failure and cancel outcomes...
        assert q.measured_outcomes == ("success", "failure", "cancel")
        # ...and names the three outcomes it did not measure.
        assert q.limitations == (
            "only the 'success', 'failure' and 'cancel' outcomes were measured by "
            "driving the native CMS lifecycle and the durable attempt/cancellation "
            "seams; 'wait', 'resume' and 'publication-refusal' are not measured by "
            "this consumer, so the record stays 'not-performed' by contract",
        )
    finally:
        store.close()


# ── the two newly measured outcomes (adversarial) ───────────────────────────


def test_failure_outcome_is_measured_from_durable_state_not_claimed(tmp_path):
    """The ``failure`` outcome is a *persisted terminal* fact, not a live claim.

    Adversarial on the base SHA (afdf20d): ``qualify_cms_path`` measured only
    ``success``, so this test fails there.  It refuses a prewritten receipt:
    the returned run must point at a durable dispatch intent whose **persisted**
    state is the terminal ``failed``, and that intent must be a *separate* owned
    attempt from the success transaction's own intent.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, PATH_LANDING_PAGE, run_id="run-fail", task_id="task-fail"
        )
        # The consumer declares it measured the failure outcome...
        assert "failure" in result.qualification.measured_outcomes
        assert result.failure_state == INTENT_FAILED

        # ...because a durable intent really is terminal-failed.
        persisted = store.get_dispatch_intent(result.failure_intent_id)
        assert persisted is not None
        assert persisted.state == INTENT_FAILED
        # A failed intent is *resolved*: its scope is released, so it is no
        # longer claiming and cannot be a still-running attempt.
        assert persisted.state not in INTENT_CLAIMING_STATES
        assert persisted.run_id == "run-fail"

        # The failed attempt is distinct from the success transaction's intent.
        success_txn = store.get_cms_transaction(result.run_id, result.txn_id)
        assert success_txn is not None
        assert success_txn["intent_id"] != persisted.intent_id

        # The success transaction is untouched: it kept its terminal stage.
        assert result.final_stage == "restored"
    finally:
        store.close()


def test_cancel_outcome_is_measured_from_durable_state_not_claimed(tmp_path):
    """The ``cancel`` outcome is a *persisted terminal* run, not a live claim.

    Adversarial on the base SHA (afdf20d): only ``success`` was measured, so the
    test fails there.  The returned run must point at a **durable** run record
    that is terminal ``advance_or_stop`` carrying the ``killed`` lifecycle state,
    re-read from the store rather than asserted from a reconciler.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, PATH_LANDING_PAGE, run_id="run-cancel", task_id="task-cancel"
        )
        assert "cancel" in result.qualification.measured_outcomes
        assert result.cancel_run_state == "advance_or_stop"
        assert result.cancel_lifecycle_state == "killed"

        # The run really is terminal in durable state -- cancel is a durable
        # outcome, not an in-memory flag.
        persisted = store.get_run("run-cancel")
        assert persisted is not None
        assert persisted.state == "advance_or_stop"
        assert RunStateModel.is_terminal(persisted.state) is True
        assert persisted.metadata["runlifecycle"]["lifecycle_state"] == "killed"
        # The cancellation is bound to the declared sandbox, never production.
        assert persisted.metadata["cms_qualification_sandbox"] == CMS_QUALIFICATION_SANDBOX_TARGET
    finally:
        store.close()


def test_unknown_path_is_refused_before_any_effect(tmp_path):
    """A path outside the frozen five is refused, not silently qualified."""
    store = _store(tmp_path)
    try:
        try:
            qualify_cms_path(store, "cms-does-not-exist", run_id="run-x")
        except CmsFlowQualificationRefused as exc:
            assert "cms-does-not-exist" in str(exc)
        else:  # pragma: no cover - the refusal is the assertion
            raise AssertionError("an undeclared CMS path must be refused")
    finally:
        store.close()


# ── the production entry point ──────────────────────────────────────────────


def test_native_controller_cli_qualifies_a_cms_path(tmp_path, capsys):
    """The flow is reachable from the real command, not only the library."""
    store = _store(tmp_path)
    try:
        code = nc.main(
            [
                "--run-id", "run-cli",
                "--task-id", "task-cli",
                "--qualify-cms-path", PATH_LANDING_PAGE,
            ],
            store=store,
        )
        out = capsys.readouterr().out
    finally:
        store.close()

    assert code == nc.EXIT_OK
    payload = json.loads(out)
    assert payload["path"]["path_id"] == PATH_LANDING_PAGE
    assert payload["final_stage"] == "restored"
    assert payload["qualification"]["artifact"] == SCHEMA_VERSION
    assert payload["qualification"]["disposition"] == DISPOSITION_NOT_PERFORMED
    assert payload["qualification"]["readback_verified"] is True
    assert payload["qualification"]["restore_verified"] is True
    # The extra outcomes are measured through the same command, not only the
    # library: the failure intent and the cancel run terminal are in the payload.
    assert payload["qualification"]["measured_outcomes"] == [
        "success", "failure", "cancel",
    ]
    assert payload["failure_state"] == INTENT_FAILED
    assert payload["cancel_run_state"] == "advance_or_stop"
    assert payload["cancel_lifecycle_state"] == "killed"


def test_native_controller_cli_refuses_an_undeclared_path(tmp_path, capsys):
    """The command maps an undeclared path to a usage refusal (exit 2)."""
    store = _store(tmp_path)
    try:
        code = nc.main(
            [
                "--run-id", "run-bad",
                "--task-id", "task-bad",
                "--qualify-cms-path", "cms-not-declared",
            ],
            store=store,
        )
        err = capsys.readouterr().err
    finally:
        store.close()

    assert code == nc.EXIT_INVALID_INPUT
    assert "cms-not-declared" in err


def test_qualification_sandbox_target_is_a_declared_sandbox():
    """The driven target is an explicit sandbox, never a production surface."""
    assert CMS_QUALIFICATION_SANDBOX_TARGET == "sandbox/ch"
    for marker in ("production", "prod", "live", "real-channel"):
        assert marker not in CMS_QUALIFICATION_SANDBOX_TARGET
