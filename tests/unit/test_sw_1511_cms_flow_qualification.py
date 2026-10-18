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

Three outcomes need more than the dummy adapter and are measured against their
own durable seams (an MCP-authority-bound durable review, a persisted
transition checkpoint, a recorded wait), each read back from durable state.
The record claims ``performed`` **only** when all six required outcomes were
genuinely measured; with no bound MCP authority the ``publication-refusal``
outcome cannot be measured and the consumer refuses fail-closed.

On the base SHA (bdb1c6c) the module under test does not exist, so this file
fails at import -- the red proof that the consumer is new.
"""

from __future__ import annotations

import json

import pytest

from skillweave.blueprint.cms_path_qualification import (
    DISPOSITION_NOT_PERFORMED,
    DISPOSITION_PERFORMED,
    INITIAL_CMS_PATHS,
    PATH_LANDING_PAGE,
    REQUIRED_OUTCOMES,
    SCHEMA_VERSION,
    CmsPath,
)
from skillweave.cli import native_controller as nc
from skillweave.mcp import server as mcp_server
from skillweave.runtime import process_identity
from skillweave.runtime.authority import MCPPrincipalAuthority
from skillweave.runtime.cms_flow_qualification import (
    CMS_QUALIFICATION_SANDBOX_TARGET,
    WAIT_METADATA_KEY,
    CmsFlowQualificationRefused,
    qualify_cms_path,
)
from skillweave.runtime.native_process import (
    INTENT_CLAIMING_STATES,
    INTENT_FAILED,
    RESUME_ADOPTED,
)
from skillweave.runtime.store import SQLiteRunStore, RunStateModel


def _store(tmp_path) -> SQLiteRunStore:
    return SQLiteRunStore(db_path=str(tmp_path / "store.db"))


@pytest.fixture
def authority(tmp_path, monkeypatch):
    """Bind a live MCP authority + mailbox, restored afterwards.

    The ``publication-refusal`` outcome is measured against a *durable
    independent review*, which is only ever recorded under a server-issued
    session.  This binds the real authority (the same seam the MCP server uses
    in production) and points the server-owned mailbox at a temp file, then
    restores both process-wide globals so no other test inherits them.
    """
    mailbox = tmp_path / "agent-mailbox.jsonl"
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox))
    auth = MCPPrincipalAuthority()
    previous = mcp_server.get_authority()
    previous_ready = mcp_server._READY_SESSION_TOKEN
    mcp_server.bind_authority(auth)
    mcp_server._READY_SESSION_TOKEN = None
    yield auth, str(mailbox)
    mcp_server._AUTHORITY = previous
    mcp_server._READY_SESSION_TOKEN = previous_ready


def _live_identity() -> dict:
    captured = process_identity.capture(__import__("os").getpid())
    assert captured is not None
    return captured.to_dict()


def _declared_path() -> CmsPath:
    """The landing-page path, taken from the *frozen* five-path declaration."""
    return INITIAL_CMS_PATHS[1]


# ── the library consumer ────────────────────────────────────────────────────


def test_qualify_cms_path_drives_the_lifecycle_and_reads_it_back(tmp_path, authority):
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


def test_all_six_required_outcomes_are_measured_and_the_record_is_performed(
    tmp_path, authority
):
    """All six required outcomes are measured, so the record may claim performed.

    A ``performed`` claim is only honest when *every* required outcome was
    genuinely measured and read back from durable state.  This asserts the
    record names all six and carries no limitation -- the claim is earned, not
    prewritten.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, "cms-generic-request", run_id="run-all", task_id="task-all"
        )
        q = result.qualification
        assert q.disposition == DISPOSITION_PERFORMED
        assert q.is_performed is True
        assert q.measured_outcomes == REQUIRED_OUTCOMES
        assert q.limitations == ()
        # The measured outcomes are named by the durable facts, one per outcome.
        assert result.failure_state == INTENT_FAILED
        assert result.cancel_run_state == "advance_or_stop"
        assert result.wait_decision == "resume_same_route"
        assert result.resume_decision == RESUME_ADOPTED
        assert result.publication_refusal_field == "authorization"
    finally:
        store.close()


def test_without_a_bound_mcp_authority_the_consumer_fails_closed(tmp_path):
    """No bound MCP authority: the consumer refuses rather than forge a record.

    The ``publication-refusal`` outcome must be measured against a durable
    independent review, which is only ever recorded under a server-issued
    session.  With no authority bound that review cannot exist, so the
    measurement must be refused -- the record stays ``not-performed`` rather
    than claiming an outcome the consumer did not measure.
    """
    store = _store(tmp_path)
    try:
        try:
            qualify_cms_path(store, PATH_LANDING_PAGE, run_id="run-np")
        except CmsFlowQualificationRefused as exc:
            assert "publication-refusal" in str(exc)
        else:  # pragma: no cover - the refusal is the assertion
            raise AssertionError(
                "without a bound MCP authority the consumer must fail closed, "
                "not return a qualification record"
            )
    finally:
        store.close()


# ── the two newly measured outcomes (adversarial) ───────────────────────────


def test_failure_outcome_is_measured_from_durable_state_not_claimed(
    tmp_path, authority
):
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


def test_cancel_outcome_is_measured_from_durable_state_not_claimed(
    tmp_path, authority
):
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


def test_wait_outcome_is_recorded_and_survives_in_durable_state(
    tmp_path, authority
):
    """The ``wait`` outcome is a *persisted* record that survives, not a flag.

    Adversarial on the base SHA (208be60): ``qualify_cms_path`` listed ``wait``
    as unmeasured, so this test fails there.  The returned record must name the
    durable metadata key the provider-quota wait was written under, and a
    *fresh* read of the persisted run must show the recorded decision -- a wait
    that only lived in memory would be gone.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, PATH_LANDING_PAGE, run_id="run-wait", task_id="task-wait"
        )
        assert "wait" in result.qualification.measured_outcomes
        assert result.wait_decision == "resume_same_route"
        assert result.wait_marker == WAIT_METADATA_KEY

        # The wait really survived: re-read it from the durable run record.
        persisted = store.get_run("run-wait")
        assert persisted is not None
        recorded = persisted.metadata[result.wait_marker]
        assert recorded["decision"] == "resume_same_route"
        assert recorded["run_state"] == "blocked_waiting_for_gate"
        assert recorded["run_id"] == "run-wait"
    finally:
        store.close()


def test_resume_outcome_is_adopted_from_a_durable_checkpoint_and_not_duplicated(
    tmp_path, authority
):
    """The ``resume`` outcome is a durable adoption, and work is not duplicated.

    Adversarial on the base SHA (208be60): ``resume`` was listed as unmeasured,
    so this test fails there.  The record must show a restarted controller
    adopted the in-flight attempt -- the adopted decision read back from the
    *durable* checkpoint -- and the adopted attempt's own scope must be the one
    a re-dispatch refuses (the work was adopted, not replayed).
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, PATH_LANDING_PAGE, run_id="run-resume", task_id="task-resume"
        )
        assert "resume" in result.qualification.measured_outcomes
        assert result.resume_decision == RESUME_ADOPTED
        assert result.resume_decision_verified is True

        # The adoption is a durable fact: the adopted intent is persisted under
        # the exact execution scope the resume decision named.
        persisted = store.get_dispatch_intent(result.resume_intent_id)
        assert persisted is not None
        assert persisted.execution_scope == result.resume_execution_scope
        assert persisted.state in INTENT_CLAIMING_STATES
        # ...and the checkpoint the restarted controller adopted from is durable.
        checkpoint = store.get_checkpoint("run-resume")
        assert checkpoint is not None
        assert checkpoint.metadata["in_flight"]
    finally:
        store.close()


def test_publication_refusal_is_measured_from_durable_state_not_claimed(
    tmp_path, authority
):
    """Irreversible publication is refused for want of a designated authorization.

    Adversarial on the base SHA (208be60): ``publication-refusal`` was listed as
    unmeasured, so this test fails there.  The refusal must be the *authorization*
    check (a current independent durable review is present, so the seam got past
    the review check), the answering principal must be the server-issued
    reviewer, and nothing is published.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, PATH_LANDING_PAGE, run_id="run-pub", task_id="task-pub"
        )
        assert "publication-refusal" in result.qualification.measured_outcomes
        assert result.publication_refusal_field == "authorization"
        # The refusal was raised against a *server-issued* reviewer principal.
        assert result.publication_refusal_principal == "mcp:reviewer:cms-qual-run-pub"

        # Nothing was published: the transaction the refusal was measured
        # against is still at its reversible terminal stage, on the declared
        # sandbox -- no publication step ever advanced it.
        persisted_txn = store.get_cms_transaction(result.run_id, result.txn_id)
        assert persisted_txn is not None
        assert persisted_txn["stage"] == "restored"
        assert persisted_txn["sandbox_target"] == CMS_QUALIFICATION_SANDBOX_TARGET
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


def test_native_controller_cli_qualifies_a_cms_path(tmp_path, capsys, authority):
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
    assert payload["qualification"]["disposition"] == DISPOSITION_PERFORMED
    assert payload["qualification"]["readback_verified"] is True
    assert payload["qualification"]["restore_verified"] is True
    # Every required outcome is measured through the same command, not only the
    # library: the durable facts of each are in the payload.
    assert payload["qualification"]["measured_outcomes"] == list(REQUIRED_OUTCOMES)
    assert payload["failure_state"] == INTENT_FAILED
    assert payload["cancel_run_state"] == "advance_or_stop"
    assert payload["cancel_lifecycle_state"] == "killed"
    assert payload["wait_decision"] == "resume_same_route"
    assert payload["wait_marker"] == WAIT_METADATA_KEY
    assert payload["resume_decision"] == RESUME_ADOPTED
    assert payload["resume_decision_verified"] is True
    assert payload["publication_refusal_field"] == "authorization"


def test_native_controller_cli_fails_closed_without_a_bound_authority(
    tmp_path, capsys
):
    """Without a bound MCP authority the command refuses, and never forges.

    The ``publication-refusal`` outcome cannot be measured without a durable
    independent review, which needs a bound MCP authority.  The real command
    must surface that as a refusal, never as a fabricated ``performed`` record.
    """
    store = _store(tmp_path)
    try:
        code = nc.main(
            [
                "--run-id", "run-nolive",
                "--task-id", "task-nolive",
                "--qualify-cms-path", PATH_LANDING_PAGE,
            ],
            store=store,
        )
        captured = capsys.readouterr()
    finally:
        store.close()

    assert code == nc.EXIT_INVALID_INPUT
    assert "publication-refusal" in captured.err
    # Nothing was written to stdout: a refusal is never a record.
    assert captured.out == ""


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
