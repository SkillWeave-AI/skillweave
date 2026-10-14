"""SW-1511-CMS-TRANSACTION criteria 1-3: capability-bound reversible CMS ops.

Each criterion is proved **through the real controller seam** --
``NativeProcessService`` methods reached exactly as production reaches them
(acquire the controller lease, then drive the managed transaction), the same
seam pattern ``test_sw_1511_review_gate_receipt.py`` uses.  A helper exercised
only in isolation would not prove anything.

  * criterion 1: one transaction owner manages baseline, draft/dry-run, bounded
    apply, readback and restore over a **neutral** capability contract;
  * criterion 2: independent review and a designated explicit publication
    authorization bind to the exact subject revision, and Unicorn cannot
    authorize an irreversible publication or widen scope;
  * criterion 3: a dummy adapter and one owner-authorized sandbox binding
    reproduce apply/verification/restore, with raw semantic/render hashes
    establishing restoration or a named blocker.

Backward compatibility is asserted too: the existing ``content-transaction-v1``
record and its service call still work unchanged, and an unsupported
``cms-transaction-receipt-v1`` version fails closed.
"""

from __future__ import annotations

import pytest

from skillweave.dispatch.work_contract import ContentSubject, RepositorySubject, WriteScope
from skillweave.runtime.assay import (
    ActionType,
    AssayPolicy,
    PolicyDecision,
    create_policy,
)
from skillweave.runtime.native_process import (
    CMS_CAPABILITIES,
    CMS_STAGE_BASELINE,
    CMS_STAGE_DRAFTED,
    CMS_STAGE_READBACK,
    CMS_STAGE_RESTORED,
    CMS_TXN_SCHEMA_ID,
    CMS_TXN_SCHEMA_VERSION,
    CmsCapabilityContract,
    CmsCapabilityError,
    CmsRawHashes,
    CmsStageError,
    CmsTransaction,
    ContentTransaction,
    NativeProcessService,
    PublicationAuthorization,
    PublicationRefused,
    RawContentAdapter,
    STEP_READY_DISPATCH,
    UnsupportedContractVersion,
    WrongIntegrationModeError,
    authorize_publication,
    validate_cms_capability_contract,
    validate_cms_transaction,
    validate_content_transaction,
)
from skillweave.runtime.store import SQLiteRunStore

_REVISION = "rev-1"
_BASELINE = {"title": "old", "body": "hello"}


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


def _open_intent(service):
    intent, _ = service.dispatch(
        run_id="run-1", task_id="task-cms", attempt=1, step=STEP_READY_DISPATCH
    )
    return intent


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


def _to_restored(service, intent):
    """Drive a managed transaction baseline -> restore; return the restored txn."""
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE
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
    intent = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE
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
    intent = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE
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
    intent = _open_intent(service)
    repo = RepositorySubject(repo="org/repo", commit="a" * 40)
    try:
        service.open_cms_transaction(
            intent.intent_id, repo, RawContentAdapter(), baseline=_BASELINE
        )
    except WrongIntegrationModeError:
        pass
    else:
        raise AssertionError("a repository was managed through a CMS transaction")
    store.close()


def test_c01_seam_owner_refuses_an_adapter_missing_a_capability(tmp_path):
    """An adapter that cannot restore is refused: no un-restorable transaction."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
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
            intent.intent_id, subject, broken, baseline=_BASELINE
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


def test_c02_seam_designated_authorization_binds_to_exact_revision(tmp_path):
    """The controller authorizes only the *exact* revision it was granted."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
    txn = _to_restored(service, intent)

    good = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    decision = service.authorize_cms_publication(
        txn, authorization=good, artifact_digest="sha256:x",
        producer_identity="worker-1", policy=_OwnerPublicationPolicy(),
    )
    assert decision.approved is True

    # An authorization for a *different* revision is refused (stale).
    stale = PublicationAuthorization(
        authorizer="owner-1", subject_revision="rev-0", artifact_digest="sha256:x"
    )
    try:
        service.authorize_cms_publication(
            txn, authorization=stale, artifact_digest="sha256:x",
            producer_identity="worker-1", policy=_OwnerPublicationPolicy(),
        )
    except PublicationRefused:
        pass
    else:
        raise AssertionError("a stale-revision authorization was accepted")
    store.close()


def test_c02_seam_unicorn_cannot_authorize_irreversible_publication(tmp_path):
    """Unicorn holds for an irreversible publication: no auto-authorization."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
    txn = _to_restored(service, intent)
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    decision = service.authorize_cms_publication(
        txn, authorization=auth, artifact_digest="sha256:x",
        producer_identity="worker-1", policy=create_policy("unicorn"),
    )
    assert decision.approved is False
    assert decision.requires_human is True
    assert "irreversible" in decision.policy_reason.lower()
    store.close()


def test_c02_seam_unicorn_cannot_widen_the_scope(tmp_path):
    """A publication outside the declared scope is refused as a scope widening."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
    txn = _to_restored(service, intent)
    auth = PublicationAuthorization(
        authorizer="owner-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    scope = WriteScope(kind="content", allow=["ch"])
    try:
        service.authorize_cms_publication(
            txn, authorization=auth, artifact_digest="sha256:x",
            producer_identity="worker-1", policy=create_policy("unicorn"),
            scope_target="other/channel", permitted_scope=scope,
        )
    except PublicationRefused:
        pass
    else:
        raise AssertionError("Unicorn widened the publication scope")
    store.close()


def test_c02_seam_producer_cannot_authorize_its_own_publication(tmp_path):
    """A producer's self-authorization is refused (separation of authority)."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
    txn = _to_restored(service, intent)
    self_auth = PublicationAuthorization(
        authorizer="worker-1", subject_revision=_REVISION, artifact_digest="sha256:x"
    )
    try:
        service.authorize_cms_publication(
            txn, authorization=self_auth, artifact_digest="sha256:x",
            producer_identity="worker-1", policy=_OwnerPublicationPolicy(),
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
            producer_identity="worker-1", policy=_OwnerPublicationPolicy(),
            scope_target="prod/channel",
            permitted_scope=WriteScope(kind="content", allow=["sandbox"]),
        )


def test_c02_authorization_requires_designation():
    """A non-designated authorization is refused outright."""
    with pytest.raises(PublicationRefused):
        authorize_publication(
            None, subject_revision=_REVISION, artifact_digest="sha256:x",
            policy=_OwnerPublicationPolicy(),
        )


# ── Criterion 3: dummy adapter + sandbox binding; raw hashes decide ─────────


def test_c03_seam_dummy_adapter_reproduces_apply_verify_restore(tmp_path):
    """Criterion 3: the dummy adapter + one sandbox binding reproduce the loop."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
    txn = _to_restored(service, intent)
    # Verification and restoration are established by raw hashes, not a claim.
    assert txn.verified() is True
    assert txn.restored() is True
    assert txn.readback_hashes.semantic == txn.applied_hashes.semantic
    assert txn.restored_hashes.semantic == txn.baseline_hashes.semantic
    assert txn.restored_hashes.render == txn.baseline_hashes.render
    # Reproducible: re-driving the same inputs yields the same hashes.
    intent2, _ = service.dispatch(
        run_id="run-1", task_id="task-cms-2", attempt=1, step=STEP_READY_DISPATCH
    )
    txn2 = _to_restored(service, intent2)
    assert txn2.baseline_hashes.to_dict() == txn.baseline_hashes.to_dict()
    assert txn2.restored_hashes.to_dict() == txn.restored_hashes.to_dict()
    store.close()


def test_c03_restore_failure_yields_a_named_blocker_not_a_claim(tmp_path):
    """A restore that cannot reproduce the baseline names the blocker."""
    service, store = _controller_service(tmp_path)
    intent = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)

    class _DriftingAdapter(RawContentAdapter):
        """A dummy adapter whose restore does not put the baseline back."""

        def _current_state(self):
            return self._current

        def restore(self):
            # Restore to *something else*: the raw hashes will not reproduce the
            # baseline, so restoration must not be claimed.
            self._current = {"title": "drifted", "body": "hello"}
            from skillweave.runtime.native_process import _hash_cms_content

            return _hash_cms_content(self._current)

    adapter = _DriftingAdapter()
    txn = service.open_cms_transaction(
        intent.intent_id, subject, adapter, baseline=_BASELINE
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
    intent = _open_intent(service)
    subject = ContentSubject(channel="ch", content_id="e1", revision=_REVISION)
    txn = service.open_cms_transaction(
        intent.intent_id, subject, RawContentAdapter(), baseline=_BASELINE
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
    intent = _open_intent(service)
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


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
