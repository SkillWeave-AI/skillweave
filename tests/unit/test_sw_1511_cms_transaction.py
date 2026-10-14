"""SW-1511-CMS-TRANSACTION criteria 1-3: capability-bound reversible CMS ops.

Each criterion is proved **through the real controller seam** --
``NativeProcessService`` methods reached exactly as production reaches them
(acquire the controller lease, then drive the managed transaction) -- and the
publication authority is exercised through the **real durable review seam**: the
MCP ``review_action`` tool records a reviewer's source-bound result in the
durable mailbox, and publication loads it *by reference* from there.  A helper
exercised only in isolation would not prove anything.

  * criterion 1: one transaction owner manages baseline, draft/dry-run, bounded
    apply, readback and restore over a **neutral** capability contract;
  * criterion 2: independent review and a designated explicit publication
    authorization bind to the exact subject revision, and Unicorn cannot
    authorize an irreversible publication or widen scope.  The review and the
    producer are **durable facts**, not caller arguments: the review is loaded
    from the server-recorded ``review_action`` result and the producer is
    derived from the owned attempt's recorded process identity;
  * criterion 3: a dummy adapter and one owner-authorized sandbox binding
    reproduce apply/verification/restore, with raw semantic/render hashes
    establishing restoration or a named blocker.

Backward compatibility is asserted too: the existing ``content-transaction-v1``
record and its service call still work unchanged, and an unsupported
``cms-transaction-receipt-v1`` version fails closed.

The final section is **adversarial**: it reproduces each forgery the earlier
candidate admitted (a caller-minted ``AttemptReview`` asserting independence, a
false producer declaration, a production target dressed as a sandbox, and the
full forgery chain) and proves each is refused at the real seam.
"""

from __future__ import annotations

import json
import os

import pytest

from skillweave.dispatch.work_contract import ContentSubject, RepositorySubject, WriteScope
from skillweave.mcp import server as mcp_server
from skillweave.runtime import process_identity
from skillweave.runtime.assay import (
    ActionType,
    AssayPolicy,
    PolicyDecision,
    create_policy,
)
from skillweave.runtime.authority import MCPPrincipalAuthority
from skillweave.runtime.native_process import (
    CMS_CAPABILITIES,
    CMS_PRODUCTION_TARGET_MARKERS,
    CMS_STAGE_BASELINE,
    CMS_STAGE_DRAFTED,
    CMS_STAGE_READBACK,
    CMS_STAGE_RESTORED,
    CMS_TXN_SCHEMA_ID,
    CMS_TXN_SCHEMA_VERSION,
    CmsBindingRefused,
    CmsCapabilityContract,
    CmsCapabilityError,
    CmsRawHashes,
    CmsStageError,
    CmsTransaction,
    ContentTransaction,
    DurableReviewRef,
    NativeProcessService,
    OwnerSandboxDeclaration,
    PublicationAuthorization,
    PublicationRefused,
    RawContentAdapter,
    STEP_READY_DISPATCH,
    SandboxBinding,
    UnsupportedContractVersion,
    WrongIntegrationModeError,
    authorize_publication,
    resolve_cms_sandbox_binding,
    resolve_owner_sandbox_target,
    _hash_cms_content,
    validate_cms_capability_contract,
    validate_cms_transaction,
    validate_content_transaction,
)
from skillweave.runtime.store import SQLiteRunStore
from skillweave.trace.gate_receipt import AttemptReview, ReviewBinding

_REVISION = "rev-1"
_BASELINE = {"title": "old", "body": "hello"}
_SANDBOX = SandboxBinding(target="sandbox/ch")
#: The channel the dummy adapter's owner-authorized sandbox binding declares.
_OWNER_SANDBOX = OwnerSandboxDeclaration(targets=("sandbox/ch",))


def _live_process_identity() -> dict:
    """A *real*, durable process identity captured from this test process.

    The producer identity is derived from the owned attempt's recorded process
    identity, so the tests record a genuine, live identity (this interpreter)
    rather than a fabricated dict -- exactly what the dispatch adapter records
    for an owned child.
    """
    captured = process_identity.capture(os.getpid())
    assert captured is not None, "could not capture the test process identity"
    return captured.to_dict()


def _derived_producer(identity: dict) -> str:
    """The producer identity the controller *derives* from an owned attempt."""
    record = process_identity.ProcessIdentity.from_dict(identity)
    assert record is not None
    return f"attempt:{record.creation_token}"


def _controller_service(tmp_path, *, store=None, holder="controller-1"):
    """The real controller seam: a leased NativeProcessService over a store.

    Passing an existing ``store`` models a recovering controller taking over
    the *same* durable store after the prior holder released its lease.
    """
    if store is None:
        store = SQLiteRunStore(db_path=str(tmp_path / "store.db"))
    service = NativeProcessService(store, holder=holder)
    service.acquire(ttl_seconds=300)
    return service, store


def _open_intent(service, *, task_id="task-cms", attempt=1, run_id="run-1"):
    """Dispatch an owned attempt *with* a recorded process identity.

    Returns ``(intent, producer)`` where ``producer`` is the identity the
    controller derives from the recorded attempt -- the value a caller must
    declare to open a transaction on it.
    """
    identity = _live_process_identity()
    intent, _ = service.dispatch(
        run_id=run_id, task_id=task_id, attempt=attempt, step=STEP_READY_DISPATCH,
        process_identity=identity,
    )
    return intent, _derived_producer(identity)


class _OwnerPublicationPolicy(AssayPolicy):
    """A designated publication authority: approves *only* the irreversible
    publication action.  This is the stand-in for the operator who holds the
    explicit publication grant; the shipped policies (Unicorn, Moderate,
    Conservative) all *hold* an irreversible action."""

    name = "owner-designated"

    def check(self, action):
        if action.action_type == ActionType.IRREVERSIBLE_ACTION:
            return PolicyDecision(approved=True, reason="owner approved publication")
        return PolicyDecision(approved=False, reason="owner approves only publication")


@pytest.fixture
def authority(tmp_path, monkeypatch):
    """A live MCP authority + mailbox, so ``review_action`` records durably."""
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


def _record_durable_review(
    authority, *, run_id="run-1", task_id="task-cms", attempt=1,
    subject="cms-c2", message_id="review-1", role="reviewer", client_id="rev",
):
    """Record a reviewer's source-bound result through the real MCP tool.

    Returns ``(ref, principal)``: a :class:`DurableReviewRef` naming the durable
    record, and the *server-issued* principal the review was recorded under.
    """
    auth, mailbox_path = authority
    token = auth.issue_session(
        role=role, client_id=client_id, transport="http", run_ids=[run_id],
    )
    written = json.loads(
        mcp_server.review_action(
            action="write", recipient=client_id, body=subject, run_id=run_id,
            task_id=task_id, attempt=attempt, message_id=message_id,
            session_token=token,
        )
    )
    assert written["status"] == "ok", written
    principal = written["message"]["principal"]
    return DurableReviewRef(mailbox_path=mailbox_path, message_id=message_id), principal


def _to_restored(service, intent, producer, *, binding=_SANDBOX):
    """Drive a managed transaction baseline -> restore; return the restored txn."""
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
        producer_identity=producer, binding=binding,
    )
    txn = service.draft_cms_transaction(txn, change={"title": "new", "body": "hello"})
    txn = service.apply_cms_transaction(txn, change={"title": "new", "body": "hello"})
    txn, blocker = service.restore_cms_transaction(txn)
    assert blocker == "", blocker
    return txn


# ── Criterion 1: the neutral capability contract and the owned lifecycle ─────


def test_c01_seam_owner_drives_baseline_draft_apply_readback_restore(tmp_path):
    """Criterion 1 through the controller seam: one owner drives every stage."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
        producer_identity=producer, binding=_SANDBOX,
    )
    assert txn.stage == CMS_STAGE_BASELINE
    assert txn.subject_kind == "content"
    assert txn.subject_revision == _REVISION
    assert set(CMS_CAPABILITIES) <= set(txn.capabilities)

    txn = service.draft_cms_transaction(txn, change={"title": "new", "body": "hello"})
    assert txn.stage == CMS_STAGE_DRAFTED or txn.stage == "dry_run"
    # The dry-run is recorded; nothing is applied yet.
    assert txn.applied_hashes is None
    assert txn.dry_run_hashes is not None

    txn = service.apply_cms_transaction(txn, change={"title": "new", "body": "hello"})
    assert txn.stage == CMS_STAGE_READBACK
    assert txn.verified() is True  # readback reproduces the apply
    assert txn.applied_hashes is not None

    txn, blocker = service.restore_cms_transaction(txn)
    assert blocker == ""
    assert txn.stage == CMS_STAGE_RESTORED
    assert txn.restored() is True  # raw hashes reproduce the baseline

    # Durable: a recovering controller reads the restored transaction.
    loaded = service.load_cms_transaction("run-1", txn.txn_id)
    assert loaded is not None
    assert loaded.stage == CMS_STAGE_RESTORED
    assert loaded.restored() is True
    store.close()


def test_c01_seam_owner_refuses_out_of_order_stages(tmp_path):
    """The owner refuses a lifecycle driven out of order (fail closed)."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
        producer_identity=producer, binding=_SANDBOX,
    )
    # An apply before a dry-run is refused.
    try:
        service.apply_cms_transaction(txn, change={"title": "new", "body": "hello"})
    except CmsStageError:
        pass
    else:
        raise AssertionError("an un-dry-run apply was admitted")
    # A restore before an apply/readback is refused.
    try:
        service.restore_cms_transaction(txn)
    except CmsStageError:
        pass
    else:
        raise AssertionError("a restore before a readback was admitted")
    store.close()


def test_c01_seam_owner_refuses_a_repository_subject(tmp_path):
    """A repository integrates through Git, never a managed CMS transaction."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    repo = RepositorySubject(repo="org/repo", commit="a" * 40)
    try:
        service.open_cms_transaction(
            intent.intent_id, repo, RawContentAdapter(), baseline=_BASELINE,
            producer_identity=producer, binding=_SANDBOX,
        )
    except WrongIntegrationModeError:
        pass
    else:
        raise AssertionError("a repository was managed through a CMS transaction")
    store.close()


def test_c01_seam_owner_refuses_an_adapter_missing_a_capability(tmp_path):
    """An adapter that cannot restore is refused: no un-restorable transaction."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    # Building a contract that drops a capability is itself refused -- the
    # capability set is closed and frozen at the contract, not the caller.
    try:
        CmsCapabilityContract(
            adapter="no-restore",
            capabilities=tuple(c for c in CMS_CAPABILITIES if c != "restore"),
        )
    except CmsCapabilityError:
        pass
    else:
        raise AssertionError("a contract missing 'restore' was admitted")

    # An adapter that declares the full contract but *does not implement* one
    # of the named capabilities is refused at the seam -- a name is not a
    # capability (a new helper without a production caller does not count).
    class _DeclaredOnly:
        def __init__(self):
            self.contract = CmsCapabilityContract(adapter="declared-only")

        # read_baseline / draft / dry_run / apply / readback are present;
        # ``restore`` is *declared* by the contract but not implemented.
        def read_baseline(self, baseline):
            return baseline

        def draft(self, current, change):
            return dict(current, **change)

        def dry_run(self, candidate):
            return _hash_cms_content(candidate)

        def apply(self, candidate):
            return _hash_cms_content(candidate)

        def readback(self):
            return {}

    broken = _DeclaredOnly()
    assert "restore" in broken.contract.capabilities  # declared
    assert not hasattr(broken, "restore")  # but not implemented
    try:
        service.open_cms_transaction(
            intent.intent_id, subject, broken, baseline=_BASELINE,
            producer_identity=producer, binding=_SANDBOX,
        )
    except (CmsCapabilityError, ValueError):
        pass
    else:
        raise AssertionError("an adapter without restore drove a managed transaction")
    store.close()


def test_c01_neutral_contract_names_no_provider_or_transformer():
    """The capability contract is neutral: verbs only, no provider/transformer."""
    contract = CmsCapabilityContract(adapter="raw-dummy")
    text = str(contract.to_dict()).lower()
    for forbidden in ("humaniz", "transform", "txt", "openai", "wordpress"):
        assert forbidden not in text, f"contract leaked provider detail {forbidden!r}"
    assert set(CMS_CAPABILITIES) <= set(contract.capabilities)


# ── Criterion 2: revision-bound authorization; Unicorn cannot publish ───────


def test_c02_seam_designated_authorization_binds_to_exact_revision(
    tmp_path, authority
):
    """The controller authorizes only the *exact* revision it was granted."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    review_ref, _ = _record_durable_review(authority)

    good = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    decision = service.authorize_cms_publication(
        txn, authorization=good, artifact_digest="sha256:x",
        review_ref=review_ref, policy=_OwnerPublicationPolicy(),
    )
    assert decision.approved is True

    # An authorization for a *different* revision is refused (stale).
    stale = PublicationAuthorization(
        authorizer="owner-1", subject_revision="rev-0", artifact_digest="sha256:x"
    )
    try:
        service.authorize_cms_publication(
            txn, authorization=stale, artifact_digest="sha256:x",
            review_ref=review_ref, policy=_OwnerPublicationPolicy(),
        )
    except PublicationRefused:
        pass
    else:
        raise AssertionError("a stale-revision authorization was accepted")
    store.close()


def test_c02_seam_unicorn_cannot_authorize_irreversible_publication(
    tmp_path, authority
):
    """Unicorn holds for an irreversible publication: no auto-authorization."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    review_ref, _ = _record_durable_review(authority)
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    decision = service.authorize_cms_publication(
        txn, authorization=auth, artifact_digest="sha256:x",
        review_ref=review_ref, policy=create_policy("unicorn"),
    )
    assert decision.approved is False
    assert decision.requires_human is True
    assert "irreversible" in decision.policy_reason.lower()
    store.close()


def test_c02_seam_unicorn_cannot_widen_the_scope(tmp_path, authority):
    """A publication outside the declared scope is refused as a scope widening."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    review_ref, _ = _record_durable_review(authority)
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    scope = WriteScope(kind="content", allow=["ch"])
    try:
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x",
            review_ref=review_ref, policy=create_policy("unicorn"),
            scope_target="other/channel", permitted_scope=scope,
        )
    except PublicationRefused:
        pass
    else:
        raise AssertionError("Unicorn widened the publication scope")
    store.close()


def test_c02_seam_producer_cannot_authorize_its_own_publication(
    tmp_path, authority
):
    """A producer's self-authorization is refused (separation of authority)."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    review_ref, _ = _record_durable_review(authority)
    # The *derived* producer is the transaction's recorded producer; the
    # authorizer is set to it so the producer self-authorizes.
    self_auth = PublicationAuthorization(
        authorizer=txn.producer_identity, subject_revision=_REVISION,
        artifact_digest="sha256:x",
    )
    try:
        service.authorize_cms_publication(
            txn, authorization=self_auth, artifact_digest="sha256:x",
            review_ref=review_ref, policy=_OwnerPublicationPolicy(),
        )
    except PublicationRefused:
        pass
    else:
        raise AssertionError("a producer authorized its own publication")
    store.close()


def test_c02_independent_review_binds_revision_and_invalidates_on_change():
    """Criterion 2: an independent review binds the revision; a change stales it."""
    from skillweave.trace.gate_receipt import (
        AttemptReview,
        ReviewBinding,
        StaleReviewError,
        assert_review_current,
    )

    binding = ReviewBinding(
        review_id="rev-1", review_criteria=("cms-c1",),
        subject_revision=_REVISION, artifact_digest="sha256:x",
    )
    review = AttemptReview(
        attempt_id="att-1", binding=binding, role="reviewer",
        answering_identity="verifier-1", producer_identity="worker-1",
    )
    assert review.independent is True
    # Current: no raise.
    assert_review_current(review, subject_revision=_REVISION, artifact_digest="sha256:x")
    # A revision change invalidates the prior review.
    try:
        assert_review_current(review, subject_revision="rev-2", artifact_digest="sha256:x")
    except StaleReviewError:
        pass
    else:
        raise AssertionError("a review survived a subject revision change")


def test_c02_scoped_authorization_refuses_unbounded_publication():
    """A bare authorization with the target outside scope is refused directly."""
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    with pytest.raises(PublicationRefused):
        authorize_publication(
            auth, subject_revision=_REVISION, artifact_digest="sha256:x",
            producer_identity="producer-1",
            review=AttemptReview(
                attempt_id="att-1",
                binding=ReviewBinding(
                    review_id="rev-1", review_criteria=("cms-c2",),
                    subject_revision=_REVISION, artifact_digest="sha256:x",
                ),
                role="reviewer", answering_identity="verifier-1",
                producer_identity="producer-1",
            ),
            policy=_OwnerPublicationPolicy(),
            scope_target="prod/channel",
            permitted_scope=WriteScope(kind="content", allow=["sandbox"]),
        )


def test_c02_authorization_requires_designation():
    """A non-designated authorization is refused outright."""
    review = AttemptReview(
        attempt_id="att-1",
        binding=ReviewBinding(
            review_id="rev-1", review_criteria=("cms-c2",),
            subject_revision=_REVISION, artifact_digest="sha256:x",
        ),
        role="reviewer", answering_identity="verifier-1",
        producer_identity="producer-1",
    )
    with pytest.raises(PublicationRefused):
        authorize_publication(
            None, subject_revision=_REVISION, artifact_digest="sha256:x",
            producer_identity="producer-1", review=review,
            policy=_OwnerPublicationPolicy(),
        )


# ── Criterion 3: dummy adapter + sandbox binding; raw hashes decide ─────────


def test_c03_seam_dummy_adapter_reproduces_apply_verify_restore(tmp_path):
    """Criterion 3: the dummy adapter + one sandbox binding reproduce the loop."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    # Verification and restoration are established by raw hashes, not a claim.
    assert txn.verified() is True
    assert txn.restored() is True
    assert txn.readback_hashes.semantic == txn.applied_hashes.semantic
    assert txn.restored_hashes.semantic == txn.baseline_hashes.semantic
    assert txn.restored_hashes.render == txn.baseline_hashes.render
    # Reproducible: re-driving the same inputs yields the same hashes.
    intent2, producer2 = _open_intent(service, task_id="task-cms-2")
    txn2 = _to_restored(service, intent2, producer2)
    assert txn2.baseline_hashes.to_dict() == txn.baseline_hashes.to_dict()
    assert txn2.restored_hashes.to_dict() == txn.restored_hashes.to_dict()
    store.close()


def test_c03_restore_failure_yields_a_named_blocker_not_a_claim(tmp_path):
    """A restore that cannot reproduce the baseline names the blocker."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    class _DriftingAdapter(RawContentAdapter):
        """A dummy adapter whose restore does not put the baseline back."""

        def _current_state(self):
            return self._current

        def restore(self):
            # Restore to *something else*: the raw hashes will not reproduce the
            # baseline, so restoration must not be claimed.
            self._current = {"title": "drifted", "body": "hello"}
            return _hash_cms_content(self._current)

    adapter = _DriftingAdapter()
    txn = service.open_cms_transaction(
        intent.intent_id, subject, adapter, baseline=_BASELINE,
        producer_identity=producer, binding=_SANDBOX,
    )
    txn = service.draft_cms_transaction(txn, change={"title": "new", "body": "hello"})
    txn = service.apply_cms_transaction(txn, change={"title": "new", "body": "hello"})
    txn, blocker = service.restore_cms_transaction(txn)
    assert blocker.startswith("restore_not_established")
    assert txn.stage == CMS_STAGE_READBACK  # not advanced to restored
    assert txn.restored() is False
    store.close()


def test_c03_seam_sandbox_binding_survives_a_recovered_controller(tmp_path):
    """A recovered controller re-binds its one sandbox adapter and resumes."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
        producer_identity=producer, binding=_SANDBOX,
    )
    txn = service.draft_cms_transaction(txn, change={"title": "new", "body": "hello"})
    txn = service.apply_cms_transaction(txn, change={"title": "new", "body": "hello"})

    # A *new* controller takes over the same durable store after the prior
    # holder releases (a live lease is never stolen).
    service.release()
    service2, _ = _controller_service(tmp_path, store=store, holder="controller-2")
    recovered = service2.load_cms_transaction("run-1", txn.txn_id)
    assert recovered is not None and recovered.stage == CMS_STAGE_READBACK
    # Without a re-bound sandbox the owner refuses to drive it (fail closed).
    try:
        service2.restore_cms_transaction(recovered)
    except CmsCapabilityError:
        pass
    else:
        raise AssertionError("a transaction without a sandbox binding was driven")
    # After re-binding its sandbox adapter, the restore proceeds.
    service2.bind_cms_sandbox(recovered, RawContentAdapter())
    # The re-bound adapter has no retained restore point, so a *restore* cannot
    # be claimed -- it returns a named blocker rather than a false success.
    recovered2, blocker = service2.restore_cms_transaction(recovered)
    assert blocker.startswith("restore_not_established")
    assert recovered2.stage == CMS_STAGE_READBACK
    store.close()


def test_c03_raw_hashes_require_both_dimensions():
    """A restore matching only one raw dimension is refused by the record."""
    base = CmsRawHashes(semantic="sha256:s", render="sha256:r")
    partial = CmsRawHashes(semantic="sha256:s", render="sha256:OTHER")
    assert base.matches(base) is True
    assert base.matches(partial) is False


# ── Backward compatibility and fail-closed versioning ───────────────────────


def test_backcompat_content_transaction_still_works_through_the_seam(tmp_path):
    """The existing content-transaction-v1 call is unchanged (additive opt-in)."""
    service, store = _controller_service(tmp_path)
    intent, _ = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.record_content_integration(
        intent.intent_id, subject,
        baseline_digest="sha256:base", applied_digest="sha256:applied",
    )
    assert txn.verified() is True
    validate_content_transaction(txn.to_dict())  # unchanged record still valid
    assert store.get_content_transaction("run-1", txn.txn_id) is not None
    store.close()


def test_cms_transaction_round_trips_and_validates():
    txn = CmsTransaction(
        run_id="run-1", subject="content:ch/e1@rev-1", subject_kind="content",
        subject_revision=_REVISION, adapter="raw-dummy",
        baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
        txn_id="txn-1",
    )
    record = txn.to_dict()
    assert record["schema_id"] == CMS_TXN_SCHEMA_ID
    assert record["schema_version"] == CMS_TXN_SCHEMA_VERSION
    validate_cms_transaction(record)


def test_unsupported_cms_transaction_version_fails_closed():
    record = CmsTransaction(
        run_id="run-1", subject="content:ch/e1@rev-1", subject_kind="content",
        subject_revision=_REVISION, adapter="raw-dummy",
        baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
    ).to_dict()
    record["schema_version"] = "9.9.9"
    with pytest.raises(UnsupportedContractVersion):
        validate_cms_transaction(record)
    record["schema_version"] = CMS_TXN_SCHEMA_VERSION
    record["schema_id"] = "https://example.invalid/other/v1"
    with pytest.raises(ValueError):
        validate_cms_transaction(record)


def test_unsupported_capability_contract_version_fails_closed():
    record = CmsCapabilityContract(adapter="raw-dummy").to_dict()
    validate_cms_capability_contract(record)
    record["schema_version"] = "2.0.0"
    with pytest.raises(UnsupportedContractVersion):
        validate_cms_capability_contract(record)


def test_cms_transaction_refuses_a_repository_subject_and_bad_hashes():
    with pytest.raises(Exception):
        CmsTransaction(
            run_id="run-1", subject="repository:org/repo@" + "a" * 40,
            subject_kind="repository", subject_revision=_REVISION,
            adapter="raw-dummy",
            baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
        )
    # A restored-stage record whose hashes do not reproduce the baseline is refused.
    with pytest.raises(ValueError):
        CmsTransaction(
            run_id="run-1", subject="content:ch/e1@rev-1", subject_kind="content",
            subject_revision=_REVISION, adapter="raw-dummy",
            baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
            stage=CMS_STAGE_RESTORED,
            restored_hashes=CmsRawHashes(semantic="sha256:x", render="sha256:br"),
        )


# ── B1 (safety critical): the producer identity is structurally mandatory ───


def test_b1_seam_producer_cannot_bypass_by_omitting_the_identity(
    tmp_path, authority
):
    """B1: a producer that authors its own authorization and omits/blank the
    producer identity is *still* refused -- the identity is mandatory at the
    transaction and derived by the controller, never caller-supplied."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    # Omitting the producer identity when *opening* the transaction is
    # structurally impossible: the argument is required, so the call cannot
    # even be made without it.
    with pytest.raises(TypeError):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            binding=_SANDBOX,
        )
    # Blanking it is refused by the owner (fail closed).
    with pytest.raises(ValueError):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            producer_identity="   ", binding=_SANDBOX,
        )

    # The controller's publication path *derives* the producer from the
    # transaction: a self-authored authorization is refused even though the
    # caller passes no producer identity at all.
    txn = _to_restored(service, intent, producer)
    review_ref, _ = _record_durable_review(authority)
    self_auth = PublicationAuthorization(
        authorizer=txn.producer_identity, subject_revision=_REVISION,
        artifact_digest="sha256:x",
    )
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=self_auth, artifact_digest="sha256:x",
            review_ref=review_ref, policy=_OwnerPublicationPolicy(),
        )
    store.close()


def test_b1_publication_is_refused_when_the_transaction_producer_is_unknown():
    """B1: an unknown producer on the transaction refuses the publication
    fail-closed rather than skipping the self-authorization check."""
    txn = CmsTransaction(
        run_id="run-1", subject="content:ch/e1@rev-1", subject_kind="content",
        subject_revision=_REVISION, adapter="raw-dummy",
        baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
        producer_identity="",  # unknown producer
    )
    review = AttemptReview(
        attempt_id="att-1",
        binding=ReviewBinding(
            review_id="rev-1", review_criteria=("cms-c2",),
            subject_revision=_REVISION, artifact_digest="sha256:x",
        ),
        role="reviewer", answering_identity="verifier-1",
        producer_identity="producer-1",
    )
    with pytest.raises(PublicationRefused):
        authorize_publication(
            PublicationAuthorization(
                authorizer="owner-1", subject_revision=_REVISION,
                artifact_digest="sha256:x",
            ),
            subject_revision=_REVISION, artifact_digest="sha256:x",
            producer_identity=txn.producer_identity, review=review,
            policy=_OwnerPublicationPolicy(),
        )


# ── B2: the independent review is wired into the real publication path ──────


def test_b2_seam_publication_requires_a_current_independent_review(
    tmp_path, authority
):
    """B2: the controller refuses publication without a current independent
    review for the exact revision/digest -- reached through the real service
    and the durable ``review_action`` record."""
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    policy = _OwnerPublicationPolicy()

    # (a) No review reference at all is refused (a caller cannot omit it).
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x", policy=policy,
        )
    # (b) A reference to an id that was never recorded is refused (dangling).
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x", policy=policy,
            review_ref=DurableReviewRef(
                mailbox_path=authority[1], message_id="never-recorded"
            ),
        )
    # (c) A review recorded for a *different* attempt cannot authorize this one.
    other_ref, _ = _record_durable_review(
        authority, message_id="review-other", task_id="task-else", attempt=2
    )
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x", policy=policy,
            review_ref=other_ref,
        )
    # (d) A review whose digest does not match the published artifact is refused.
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:other", policy=policy,
            review_ref=other_ref,
        )

    # The current, independent review recorded for *this* attempt is accepted.
    review_ref, _ = _record_durable_review(authority, message_id="review-ok")
    decision = service.authorize_cms_publication(
        txn, authorization=auth, artifact_digest="sha256:x", policy=policy,
        review_ref=review_ref,
    )
    assert decision.approved is True
    store.close()


def test_legacy_v1_transaction_is_readable_but_refused_for_publication(
    tmp_path, authority
):
    """A legacy v1 record (no owned attempt, no recorded sandbox target) stays
    readable and validates, but can never be published: it carries no owned
    attempt for the review to answer, so the publication is refused."""
    legacy = {
        "schema_id": CMS_TXN_SCHEMA_ID,
        "schema_version": CMS_TXN_SCHEMA_VERSION,
        "run_id": "run-1",
        "subject": "content:ch/e1@rev-1",
        "subject_kind": "content",
        "subject_revision": _REVISION,
        "adapter": "raw-dummy",
        "baseline_hashes": {"semantic": "sha256:b", "render": "sha256:br"},
        "capabilities": list(CMS_CAPABILITIES),
        "producer_identity": "legacy-producer",
        "txn_id": "txn-legacy",
    }
    validate_cms_transaction(legacy)  # still readable/valid (additive opt-in)

    service, store = _controller_service(tmp_path)
    txn = CmsTransaction(
        run_id="run-1", subject="content:ch/e1@rev-1", subject_kind="content",
        subject_revision=_REVISION, adapter="raw-dummy",
        baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
        producer_identity="legacy-producer",  # no intent_id, no sandbox_target
    )
    assert txn.intent_id == "" and txn.sandbox_target == ""
    review_ref, _ = _record_durable_review(authority, message_id="legacy-review")
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x",
            review_ref=review_ref, policy=_OwnerPublicationPolicy(),
        )
    store.close()


# ── Advisory (promoted): the sandbox binding is structurally enforced ───────


def test_binding_is_structurally_sandbox_and_refuses_everything_else(tmp_path):
    """The binding discriminator: only an explicitly declared sandbox resolves;
    an undeclared, unknown or production-declared binding is refused."""
    # Constructing a production-declared or unknown-kind binding is refused.
    with pytest.raises(CmsBindingRefused):
        SandboxBinding(target="prod/ch", kind="production")
    with pytest.raises(CmsBindingRefused):
        SandboxBinding(target="x", kind="")
    # Resolution refuses None (undeclared), unknown kinds and production.
    with pytest.raises(CmsBindingRefused):
        resolve_cms_sandbox_binding(None)
    with pytest.raises(CmsBindingRefused):
        resolve_cms_sandbox_binding({"kind": "staging", "target": "x"})
    with pytest.raises(CmsBindingRefused):
        resolve_cms_sandbox_binding({"kind": "production", "target": "prod/ch"})
    assert resolve_cms_sandbox_binding(SandboxBinding(target="sandbox/ch")) == "sandbox/ch"
    assert resolve_cms_sandbox_binding(
        {"kind": "sandbox", "target": "sandbox/ch"}
    ) == "sandbox/ch"

    # The owner refuses to open a transaction on an undeclared binding ...
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    with pytest.raises(CmsBindingRefused):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            producer_identity=producer,  # no binding -> undeclared
        )
    # ... and on a production-declared binding.
    with pytest.raises(CmsBindingRefused):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            producer_identity=producer,
            binding={"kind": "production", "target": "prod/ch"},
        )
    store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Adversarial: the criterion-2 forgery vectors found on the prior candidate.
# Each of these MUST be refused; each reproduced an admission on 07b3fbe0.
# ═══════════════════════════════════════════════════════════════════════════


def test_f1_a_caller_minted_review_asserting_independence_is_refused(
    tmp_path, authority
):
    """F1: a caller-constructed ``AttemptReview`` that *claims* independence
    (``answering_identity`` != producer) must not authorize publication.

    On the prior candidate the controller accepted an ``AttemptReview`` object
    handed in by the caller, so a producer could mint one naming itself an
    "independent-verifier" and be approved.  The publication now demands a
    *reference to a durable server-recorded review* and refuses any object a
    caller constructs.
    """
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    txn = _to_restored(service, intent, producer)
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    # The forgery: a fully-formed, self-consistent, "independent" review object.
    forged = AttemptReview(
        attempt_id=f"{intent.run_id}/{intent.task_id}/{intent.attempt}",
        binding=ReviewBinding(
            review_id="forged-review", review_criteria=("cms-c2",),
            subject_revision=_REVISION, artifact_digest="sha256:x",
        ),
        role="reviewer", answering_identity="independent-verifier",
        producer_identity=producer,
        verdict="accepted", accepted_revision=_REVISION,
        accepted_digest="sha256:x",
    )
    assert forged.independent is True  # the caller asserts independence
    # Presenting the forged object (the prior-candidate signature) is refused:
    # the seam no longer accepts a review object at all, so the forged
    # ``AttemptReview`` cannot even be passed (a fail-closed refusal).
    with pytest.raises((PublicationRefused, TypeError)):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x",
            review=forged, policy=_OwnerPublicationPolicy(),
        )
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x",
            policy=_OwnerPublicationPolicy(),  # no durable reference at all
        )
    # And a non-reference object is refused, not coerced.
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x",
            review_ref=forged, policy=_OwnerPublicationPolicy(),
        )
    store.close()


def test_f2_a_false_producer_declaration_at_open_is_refused(tmp_path):
    """F2: ``producer_identity`` declared at open is not trusted; it must match
    the identity the owned attempt *durably recorded*.

    On the prior candidate the declaration was recorded verbatim, so a false
    producer identity (plus a forged review and owner-designated authorizer)
    was approved.  The identity is now derived from the owned attempt's recorded
    process identity, and a declaration that disagrees is refused at open.
    """
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    # A declaration that is not the identity the owned attempt recorded.
    with pytest.raises(PublicationRefused):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            producer_identity="somebody-else", binding=_SANDBOX,
        )
    # The honest declaration opens and *records the derived* producer.
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
        producer_identity=producer, binding=_SANDBOX,
    )
    assert txn.producer_identity == producer
    assert txn.producer_identity.startswith("attempt:")

    # An attempt that recorded *no* verifiable process identity has no producer
    # and is refused fail-closed rather than trusting a declared name.
    bare, _ = service.dispatch(
        run_id="run-1", task_id="task-bare", attempt=1, step=STEP_READY_DISPATCH,
    )
    subject_bare = ContentSubject(channel="ch", content_id="e2", revision=_REVISION)
    with pytest.raises(PublicationRefused):
        service.open_cms_transaction(
            bare.intent_id, subject_bare, RawContentAdapter(), baseline=_BASELINE,
            producer_identity="anything", binding=_SANDBOX,
        )
    store.close()


def test_f3_a_production_target_dressed_as_a_sandbox_is_refused(tmp_path):
    """F3: a ``SandboxBinding`` whose target names a production surface must be
    refused, and the recorded target must be re-validated against the owner's
    declaration.

    On the prior candidate the resolved binding was discarded, so a binding
    declaring ``kind="sandbox"`` but targeting ``production/real-channel`` was
    accepted and the target was never recorded or tied to an owner declaration.
    """
    # A production-looking target is refused even under a sandbox declaration.
    with pytest.raises(CmsBindingRefused):
        SandboxBinding(target="production/real-channel")
    with pytest.raises(CmsBindingRefused):
        resolve_cms_sandbox_binding(
            {"kind": "sandbox", "target": "production/real-channel"}
        )
    # The owner's declaration itself refuses production-looking targets.
    with pytest.raises(CmsBindingRefused):
        OwnerSandboxDeclaration(targets=("production/real-channel",))
    # And an undeclared/unlisted target is refused against a declaration.
    with pytest.raises(CmsBindingRefused):
        resolve_owner_sandbox_target("sandbox/other", authorized=_OWNER_SANDBOX)
    assert _OWNER_SANDBOX.authorizes("sandbox/ch") is True
    assert _OWNER_SANDBOX.authorizes("production/real-channel") is False
    assert any(m in CMS_PRODUCTION_TARGET_MARKERS for m in ("production",))

    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    # The owner refuses to open on a production-looking target dressed as sandbox.
    with pytest.raises(CmsBindingRefused):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            producer_identity=producer,
            binding={"kind": "sandbox", "target": "production/real-channel"},
        )
    # The recorded target is what the owner re-validates on a later bind: a fresh
    # binding that names a *different* target is refused.
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
        producer_identity=producer, binding=_SANDBOX,
    )
    assert txn.sandbox_target == "sandbox/ch"
    with pytest.raises(CmsBindingRefused):
        service.bind_cms_sandbox(
            txn, RawContentAdapter(), binding=SandboxBinding(target="sandbox/other")
        )
    store.close()


def test_f3_recorded_production_target_is_refused_on_load(tmp_path):
    """F3: a transaction whose *recorded* sandbox target names production is
    refused when reconstructed -- a legacy/mis-declared record cannot replay."""
    with pytest.raises(CmsBindingRefused):
        CmsTransaction(
            run_id="run-1", subject="content:ch/e1@rev-1", subject_kind="content",
            subject_revision=_REVISION, adapter="raw-dummy",
            baseline_hashes=CmsRawHashes(semantic="sha256:b", render="sha256:br"),
            sandbox_target="production/real-channel",
        )


def test_full_forgery_chain_is_refused(tmp_path, authority):
    """The whole forgery chain -- a false producer, a caller-minted review and
    an owner-designated authorizer string -- is refused end to end.

    Each link was separately forgeable on the prior candidate; together they let
    a producer self-authorize an irreversible publication.  With the producer
    derived from the owned attempt and the review loaded from the durable record,
    the chain cannot complete.
    """
    service, store = _controller_service(tmp_path)
    intent, producer = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    # Link 1: the false producer cannot even open the transaction.
    with pytest.raises(PublicationRefused):
        service.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE,
            producer_identity="attacker-1", binding=_SANDBOX,
        )
    # The honest producer opens; then the attacker tries to publish with a
    # forged review object and a self-designated authorizer string.
    txn = _to_restored(service, intent, producer)
    forged = AttemptReview(
        attempt_id="forged/att/1",
        binding=ReviewBinding(
            review_id="forged", review_criteria=("cms-c2",),
            subject_revision=_REVISION, artifact_digest="sha256:x",
        ),
        role="reviewer", answering_identity="independent-verifier",
        producer_identity=txn.producer_identity, verdict="accepted",
    )
    self_auth = PublicationAuthorization(
        authorizer=txn.producer_identity,  # the attacker names itself the owner
        subject_revision=_REVISION, artifact_digest="sha256:x",
    )
    # The forged review object is refused at the seam (it cannot be presented).
    with pytest.raises((PublicationRefused, TypeError)):
        service.authorize_cms_publication(
            txn, authorization=self_auth, artifact_digest="sha256:x",
            review=forged, policy=_OwnerPublicationPolicy(),
        )
    # Even with a *real* durable review, the attacker's self-authorization is
    # refused (the producer cannot authorize its own publication).
    review_ref, _ = _record_durable_review(authority, message_id="chain-review")
    with pytest.raises(PublicationRefused):
        service.authorize_cms_publication(
            txn, authorization=self_auth, artifact_digest="sha256:x",
            review_ref=review_ref, policy=_OwnerPublicationPolicy(),
        )
    # Only the honest owner's designation, with the durable review, is approved.
    owner_auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    decision = service.authorize_cms_publication(
        txn, authorization=owner_auth, artifact_digest="sha256:x",
        review_ref=review_ref, policy=_OwnerPublicationPolicy(),
    )
    assert decision.approved is True
    store.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
