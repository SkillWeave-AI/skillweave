"""SW-1511-REVIEW criteria 1-3: the independent gate receipt.

* criterion 1 -- *invoked/assigned*, *running*, *artifact-produced*, *reviewed*
  and *accepted* are five distinct states; a launched skill without its required
  producer artifact cannot release dependents;
* criterion 2 -- review criteria, subject revision, artifact digest, role and
  the actual answering identity bind to the attempt; a code/content change
  invalidates prior reviews;
* criterion 3 -- Model PASS, multiple terminal results, narrative receipts,
  unsupported schemas and unknown evidence never auto-accept; actual independent
  verifier evidence decides the gate.

Every test is deterministic: no provider probe, no wall-clock race, no
skip/xfail and no weakened assertion.  The frozen boundary contract
``independent-gate-receipt-v1`` is checked for backward compatibility (a v1
record round-trips unchanged) and fail-closed version handling (an unsupported
version is refused, never coerced).
"""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.trace.gate_receipt import (
    ACCEPTANCE_ACCEPTED,
    ACCEPTANCE_ARTIFACT_PRODUCED,
    ACCEPTANCE_INVOKED,
    ACCEPTANCE_ORDER,
    ACCEPTANCE_REVIEWED,
    ACCEPTANCE_RUNNING,
    ACCEPTANCE_STATES,
    ACCEPTANCE_TERMINAL,
    GATE_FAIL,
    GATE_PASS,
    GATE_RECEIPT_SCHEMA_ID,
    GATE_RECEIPT_SCHEMA_VERSION,
    GATE_UNDECIDED,
    GATE_VERDICTS,
    MODEL_PASS_REASON,
    MULTIPLE_TERMINALS_REASON,
    NARRATIVE_RECEIPT_REASON,
    SUPPORTED_GATE_RECEIPT_VERSIONS,
    UNKNOWN_EVIDENCE_REASON,
    UNSUPPORTED_SCHEMA_REASON,
    AttemptAcceptance,
    AttemptReview,
    GateDecision,
    IllegalStateTransition,
    IndependentGateReceipt,
    MissingProducerArtifact,
    ReviewBinding,
    StaleReviewError,
    UnsupportedContractVersion,
    assert_review_current,
    acceptance_rank,
    evaluate_gate,
    release_dependents,
    validate_independent_gate_receipt,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _binding(
    *,
    review_id: str = "rev-1",
    criteria=("criterion-a", "criterion-b"),
    revision: str = "git:abc123",
    digest: str = "sha256:deadbeef",
) -> ReviewBinding:
    return ReviewBinding(
        review_id=review_id,
        review_criteria=tuple(criteria),
        subject_revision=revision,
        artifact_digest=digest,
    )


def _member(
    *,
    verifier_identity: str = "verifier-1",
    verdict: str = GATE_PASS,
    schema_version: str = "independent-gate-receipt/1.0.0",
    evidence_type: str = "member",
) -> dict:
    return {
        "schema_version": schema_version,
        "evidence_type": evidence_type,
        "verifier_identity": verifier_identity,
        "verdict": verdict,
    }


def _accepted_attempt(artifact: str = "artifact-1") -> AttemptAcceptance:
    attempt = AttemptAcceptance(
        attempt_id="att-1", dependents=("dep-a", "dep-b")
    )
    attempt.record_producer_artifact(artifact)
    attempt.advance(ACCEPTANCE_RUNNING)
    attempt.advance(ACCEPTANCE_ARTIFACT_PRODUCED)
    attempt.advance(ACCEPTANCE_REVIEWED)
    attempt.advance(ACCEPTANCE_ACCEPTED)
    return attempt


# ── criterion 1 ────────────────────────────────────────────────────────────

def test_criterion_1_five_acceptance_states_are_distinct():
    assert ACCEPTANCE_STATES == (
        ACCEPTANCE_INVOKED,
        ACCEPTANCE_RUNNING,
        ACCEPTANCE_ARTIFACT_PRODUCED,
        ACCEPTANCE_REVIEWED,
        ACCEPTANCE_ACCEPTED,
    )
    # "invoked/assigned" is not "accepted" -- they are five separate states.
    assert len(set(ACCEPTANCE_STATES)) == 5
    assert ACCEPTANCE_INVOKED != ACCEPTANCE_ACCEPTED


def test_criterion_1_order_is_the_only_legal_progression():
    assert ACCEPTANCE_ORDER[ACCEPTANCE_INVOKED] == ACCEPTANCE_RUNNING
    assert ACCEPTANCE_ORDER[ACCEPTANCE_RUNNING] == ACCEPTANCE_ARTIFACT_PRODUCED
    assert ACCEPTANCE_ORDER[ACCEPTANCE_ARTIFACT_PRODUCED] == ACCEPTANCE_REVIEWED
    assert ACCEPTANCE_ORDER[ACCEPTANCE_REVIEWED] == ACCEPTANCE_ACCEPTED
    assert ACCEPTANCE_ORDER[ACCEPTANCE_ACCEPTED] is None
    assert ACCEPTANCE_TERMINAL == ACCEPTANCE_ACCEPTED


def test_criterion_1_acceptance_rank_totally_orders_the_states():
    ranks = [acceptance_rank(s) for s in ACCEPTANCE_STATES]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 5
    assert acceptance_rank("not-a-state") == -1


def test_criterion_1_launch_records_invoked_and_no_more():
    attempt = AttemptAcceptance(attempt_id="att-launch")
    assert attempt.state == ACCEPTANCE_INVOKED
    assert attempt.history == [ACCEPTANCE_INVOKED]
    assert attempt.producer_artifact == ""


def test_criterion_1_advance_refuses_skipping_a_state():
    attempt = AttemptAcceptance(attempt_id="att-skip")
    # invoked -> artifact_produced skips "running": refused.
    try:
        attempt.advance(ACCEPTANCE_ARTIFACT_PRODUCED)
    except IllegalStateTransition as exc:
        assert exc.from_state == ACCEPTANCE_INVOKED
        assert exc.to_state == ACCEPTANCE_ARTIFACT_PRODUCED
    else:
        raise AssertionError("skipping 'running' was not refused")
    # A launched skill cannot jump straight to accepted either.
    try:
        attempt.advance(ACCEPTANCE_ACCEPTED)
    except IllegalStateTransition:
        pass
    else:
        raise AssertionError("invoked -> accepted was not refused")
    assert attempt.state == ACCEPTANCE_INVOKED


def test_criterion_1_advance_refuses_backward_and_self_transitions():
    attempt = AttemptAcceptance(attempt_id="att-back")
    attempt.advance(ACCEPTANCE_RUNNING)
    for bad in (ACCEPTANCE_INVOKED, ACCEPTANCE_RUNNING):
        try:
            attempt.advance(bad)
        except IllegalStateTransition:
            pass
        else:
            raise AssertionError(f"transition to {bad!r} was not refused")
    assert attempt.state == ACCEPTANCE_RUNNING
    assert attempt.history == [ACCEPTANCE_INVOKED, ACCEPTANCE_RUNNING]


def test_criterion_1_artifact_produced_requires_the_artifact():
    attempt = AttemptAcceptance(attempt_id="att-noart")
    attempt.advance(ACCEPTANCE_RUNNING)
    # running -> artifact_produced without the artifact: refused.
    try:
        attempt.advance(ACCEPTANCE_ARTIFACT_PRODUCED)
    except IllegalStateTransition:
        pass
    else:
        raise AssertionError("artifact_produced without the artifact was allowed")
    assert attempt.state == ACCEPTANCE_RUNNING
    attempt.record_producer_artifact("artifact-x")
    attempt.advance(ACCEPTANCE_ARTIFACT_PRODUCED)
    assert attempt.state == ACCEPTANCE_ARTIFACT_PRODUCED


def test_criterion_1_launched_skill_without_artifact_cannot_release_dependents():
    attempt = AttemptAcceptance(
        attempt_id="att-launched", dependents=("dep-a",)
    )
    # Invoked/assigned only: no producer artifact.
    try:
        release_dependents(attempt)
    except MissingProducerArtifact as exc:
        assert exc.attempt_id == "att-launched"
    else:
        raise AssertionError("a launched artifact-less attempt released dependents")

    # Running, still no artifact: still refused.
    attempt.advance(ACCEPTANCE_RUNNING)
    try:
        release_dependents(attempt)
    except MissingProducerArtifact:
        pass
    else:
        raise AssertionError("a running artifact-less attempt released dependents")


def test_criterion_1_dependents_release_only_from_accepted_with_artifact():
    attempt = _accepted_attempt()
    assert release_dependents(attempt) == ("dep-a", "dep-b")

    # An artifact-carrying attempt that is not yet accepted refuses.
    pending = AttemptAcceptance(attempt_id="att-pending", dependents=("dep-x",))
    pending.record_producer_artifact("artifact-p")
    pending.advance(ACCEPTANCE_RUNNING)
    pending.advance(ACCEPTANCE_ARTIFACT_PRODUCED)
    pending.advance(ACCEPTANCE_REVIEWED)
    try:
        release_dependents(pending)
    except IllegalStateTransition as exc:
        assert exc.from_state == ACCEPTANCE_REVIEWED
        assert exc.to_state == ACCEPTANCE_TERMINAL
    else:
        raise AssertionError("a reviewed-but-not-accepted attempt released deps")


# ── criterion 2 ────────────────────────────────────────────────────────────

def test_criterion_2_binding_carries_criteria_revision_and_digest():
    binding = _binding()
    payload = binding.to_dict() if hasattr(binding, "to_dict") else None
    assert binding.review_criteria == ("criterion-a", "criterion-b")
    assert binding.subject_revision == "git:abc123"
    assert binding.artifact_digest == "sha256:deadbeef"
    # The fingerprint is stable and content-addressed over revision+digest.
    assert binding.fingerprint() == _binding().fingerprint()
    changed = _binding(revision="git:999999")
    assert changed.fingerprint() != binding.fingerprint()
    assert payload is None or isinstance(payload, dict)


def test_criterion_2_binding_refuses_empty_components():
    for kwargs in (
        {"review_id": ""},
        {"revision": "   "},
        {"digest": ""},
        {"criteria": ()},
    ):
        try:
            _binding(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"empty binding component {kwargs} was accepted")


def test_criterion_2_review_binds_role_and_actual_answering_identity():
    review = AttemptReview(
        attempt_id="att-1",
        binding=_binding(),
        role="verifier",
        answering_identity="verifier-1",
        producer_identity="worker-1",
        verdict=GATE_PASS,
    )
    assert review.role == "verifier"
    assert review.answering_identity == "verifier-1"
    assert review.independent is True

    self_review = AttemptReview(
        attempt_id="att-1",
        binding=_binding(),
        role="verifier",
        answering_identity="worker-1",
        producer_identity="worker-1",
    )
    assert self_review.independent is False


def test_criterion_2_matching_subject_accepts_and_changed_subject_is_stale():
    review = AttemptReview(
        attempt_id="att-1",
        binding=_binding(revision="git:abc123", digest="sha256:deadbeef"),
        role="verifier",
        answering_identity="verifier-1",
        producer_identity="worker-1",
        verdict=GATE_PASS,
    )
    # Unchanged subject: the review is still current.
    assert (
        assert_review_current(
            review, subject_revision="git:abc123", artifact_digest="sha256:deadbeef"
        )
        is None
    )

    # A code/content change (revision) invalidates the prior review.
    try:
        assert_review_current(
            review, subject_revision="git:999999", artifact_digest="sha256:deadbeef"
        )
    except StaleReviewError as exc:
        assert exc.attempt_id == "att-1"
        assert "revision changed" in exc.reason
    else:
        raise AssertionError("a changed revision did not invalidate the review")

    # A changed artifact digest invalidates the prior review too.
    try:
        assert_review_current(
            review, subject_revision="git:abc123", artifact_digest="sha256:NEWDIGEST"
        )
    except StaleReviewError as exc:
        assert "digest changed" in exc.reason
    else:
        raise AssertionError("a changed digest did not invalidate the review")


# ── criterion 3 ────────────────────────────────────────────────────────────

def test_criterion_3_gate_verdicts_are_the_frozen_set():
    assert GATE_VERDICTS == (GATE_PASS, GATE_FAIL, GATE_UNDECIDED)


def test_criterion_3_independent_verifier_pass_decides_the_gate():
    attempt = _accepted_attempt()
    decision = evaluate_gate(
        attempt,
        evidence=[_member(verifier_identity="verifier-1", verdict=GATE_PASS)],
        producer_identity="worker-1",
    )
    assert isinstance(decision, GateDecision)
    assert decision.verdict == GATE_PASS
    assert decision.accepted is True
    assert decision.reasons == ()
    assert decision.deciders == ("verifier-1",)


def test_criterion_3_independent_verifier_fail_does_not_accept():
    attempt = _accepted_attempt()
    decision = evaluate_gate(
        attempt,
        evidence=[_member(verifier_identity="verifier-1", verdict=GATE_FAIL)],
        producer_identity="worker-1",
    )
    assert decision.verdict == GATE_FAIL
    assert decision.accepted is False
    assert decision.reasons


def test_criterion_3_model_pass_never_auto_accepts():
    attempt = _accepted_attempt()
    decision = evaluate_gate(attempt, model_pass=True, producer_identity="worker-1")
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert MODEL_PASS_REASON in decision.reasons

    # A model_pass evidence row is just as inadmissible as the flag.
    row = _member(verifier_identity="verifier-1")
    row["role"] = "model_pass"
    decision_row = evaluate_gate(
        attempt, evidence=[row], producer_identity="worker-1"
    )
    assert decision_row.accepted is False
    assert MODEL_PASS_REASON in decision_row.reasons


def test_criterion_3_producers_own_receipt_is_not_independent():
    attempt = _accepted_attempt()
    decision = evaluate_gate(
        attempt,
        evidence=[_member(verifier_identity="worker-1", verdict=GATE_PASS)],
        producer_identity="worker-1",
    )
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert decision.deciders == ()
    assert any("not" in r and "independent" in r for r in decision.reasons)


def test_criterion_3_multiple_terminals_never_auto_accept():
    attempt = _accepted_attempt()
    decision = evaluate_gate(
        attempt,
        evidence=[
            _member(verifier_identity="verifier-1", verdict=GATE_PASS),
            {"role": "terminal", "result": "done-1"},
            {"role": "terminal", "result": "done-2"},
        ],
        producer_identity="worker-1",
    )
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert MULTIPLE_TERMINALS_REASON in decision.reasons


def test_criterion_3_narrative_receipt_never_auto_accepts():
    attempt = _accepted_attempt()
    prose = (
        "VERDICT: the work looks good overall, see notes below. "
        '{"verdict": "PASS", "verifier_identity": "verifier-1"}'
    )
    decision = evaluate_gate(attempt, evidence=[prose], producer_identity="worker-1")
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert NARRATIVE_RECEIPT_REASON in decision.reasons


def test_criterion_3_unsupported_schema_never_auto_accepts():
    attempt = _accepted_attempt()
    decision = evaluate_gate(
        attempt,
        evidence=[
            _member(
                verifier_identity="verifier-1",
                verdict=GATE_PASS,
                schema_version="independent-gate-receipt/9.9.9",
            )
        ],
        producer_identity="worker-1",
    )
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert UNSUPPORTED_SCHEMA_REASON in decision.reasons


def test_criterion_3_unknown_evidence_never_auto_accepts():
    attempt = _accepted_attempt()
    decision = evaluate_gate(
        attempt,
        evidence=[
            _member(
                verifier_identity="verifier-1",
                verdict=GATE_PASS,
                evidence_type="something-else",
            )
        ],
        producer_identity="worker-1",
    )
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert UNKNOWN_EVIDENCE_REASON in decision.reasons


def test_criterion_3_no_evidence_does_not_accept():
    attempt = _accepted_attempt()
    decision = evaluate_gate(attempt, producer_identity="worker-1")
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert decision.reasons


def test_criterion_3_missing_producer_artifact_blocks_even_with_pass_evidence():
    attempt = AttemptAcceptance(attempt_id="att-artifactless")
    decision = evaluate_gate(
        attempt,
        evidence=[_member(verifier_identity="verifier-1", verdict=GATE_PASS)],
        producer_identity="worker-1",
    )
    assert decision.accepted is False
    assert decision.verdict == GATE_UNDECIDED
    assert any("no producer artifact" in r for r in decision.reasons)


# ── boundary contract: backward compatibility + fail-closed ─────────────────

def test_contract_v1_receipt_round_trips_unchanged():
    receipt = IndependentGateReceipt(
        attempt_id="att-1",
        state=ACCEPTANCE_ACCEPTED,
        producer_artifact="artifact-1",
        binding=_binding(),
        role="verifier",
        answering_identity="verifier-1",
        producer_identity="worker-1",
        accepted=True,
    )
    payload = receipt.to_dict()
    assert payload["schema_id"] == GATE_RECEIPT_SCHEMA_ID
    assert payload["schema_version"] == GATE_RECEIPT_SCHEMA_VERSION
    # A v1 record validates unchanged.
    validate_independent_gate_receipt(payload)


def test_contract_unsupported_version_is_refused_not_coerced():
    assert SUPPORTED_GATE_RECEIPT_VERSIONS == frozenset({GATE_RECEIPT_SCHEMA_VERSION})
    payload = IndependentGateReceipt(
        attempt_id="att-1",
        state=ACCEPTANCE_RUNNING,
        schema_version=GATE_RECEIPT_SCHEMA_VERSION,
    ).to_dict()
    payload["schema_version"] = "2.0.0"
    try:
        validate_independent_gate_receipt(payload)
    except UnsupportedContractVersion as exc:
        assert exc.version == "2.0.0"
        assert exc.supported == SUPPORTED_GATE_RECEIPT_VERSIONS
    else:
        raise AssertionError("an unsupported version was coerced")

    # Constructing a record with an unsupported version fails closed too.
    try:
        IndependentGateReceipt(
            attempt_id="att-1", state=ACCEPTANCE_RUNNING, schema_version="nope"
        )
    except UnsupportedContractVersion:
        pass
    else:
        raise AssertionError("an unsupported version constructed a record")


def test_contract_tampered_acceptance_is_refused():
    # An acceptance claiming accepted without its artifact is refused.
    try:
        IndependentGateReceipt(
            attempt_id="att-1",
            state=ACCEPTANCE_ACCEPTED,
            binding=_binding(),
            answering_identity="verifier-1",
            producer_identity="worker-1",
            accepted=True,
        )
    except MissingProducerArtifact:
        pass
    else:
        raise AssertionError("an artifact-less acceptance was constructed")

    # A self-answered acceptance is refused.
    try:
        IndependentGateReceipt(
            attempt_id="att-1",
            state=ACCEPTANCE_ACCEPTED,
            producer_artifact="artifact-1",
            binding=_binding(),
            answering_identity="worker-1",
            producer_identity="worker-1",
            accepted=True,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a self-answered acceptance was constructed")


def test_contract_unknown_state_is_refused():
    try:
        IndependentGateReceipt(attempt_id="att-1", state="teleported")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown acceptance state was constructed")


# ── the real consumer seam: build_schedule + controller acceptance ──────────
#
# These exercise criteria 1 and 3 through the *production* seams named by the
# contract — the scheduler's dependent-release path (``build_schedule``) and the
# controller acceptance service (``NativeProcessService``) — not only by direct
# calls to the gate-receipt module.

from skillweave.runtime.dagscheduler import Task, Sequence, build_schedule
from skillweave.runtime.native_process import NativeProcessService


def _sched_sequence() -> Sequence:
    # dep depends on producer; producer's gate releases dep.
    return Sequence(
        session_boundary="session-1",
        tasks=[
            Task(id="producer", gate="gate-p"),
            Task(id="dep", depends_on=["producer"]),
        ],
    )


def _receipt_record(
    *,
    state: str,
    producer_artifact: str,
    accepted: bool,
    answering: str = "verifier-1",
    producer: str = "worker-1",
) -> dict:
    return IndependentGateReceipt(
        attempt_id="att-producer",
        state=state,
        producer_artifact=producer_artifact,
        binding=_binding(),
        role="verifier",
        answering_identity=answering,
        producer_identity=producer,
        accepted=accepted,
    ).to_dict()


def test_seam_launched_attempt_without_artifact_does_not_release_dependents():
    """Criterion 1/3 through build_schedule: a launched producer releases nothing.

    The producer carries no producer artifact and its gate receipt is only
    *invoked* — so its dependent must be blocked by the real scheduler.
    """
    sequence = _sched_sequence()
    receipts = {
        "gate-p": _receipt_record(
            state=ACCEPTANCE_INVOKED, producer_artifact="", accepted=False
        )
    }
    schedule = build_schedule(sequence, gate_receipts=receipts)
    assert "dep" in schedule.blocked
    assert "dep" not in {tid for b in schedule.batches for tid in b.task_ids}


def test_seam_accepted_attempt_with_artifact_releases_dependents():
    sequence = _sched_sequence()
    receipts = {
        "gate-p": _receipt_record(
            state=ACCEPTANCE_ACCEPTED, producer_artifact="artifact-1", accepted=True
        )
    }
    schedule = build_schedule(sequence, gate_receipts=receipts)
    assert "dep" not in schedule.blocked
    assert "dep" in {tid for b in schedule.batches for tid in b.task_ids}


def test_seam_accepted_state_without_artifact_releases_nothing():
    """An accepted *state* with no producer artifact is refused, not released.

    The record is built as a raw mapping so the *seam's* validation (not the
    frozen constructor) is what refuses it.
    """
    sequence = _sched_sequence()
    raw = _receipt_record(
        state=ACCEPTANCE_ACCEPTED, producer_artifact="artifact-1", accepted=True
    )
    raw["producer_artifact"] = ""  # tamper: drop the required producer artifact
    try:
        build_schedule(sequence, gate_receipts={"gate-p": raw})
    except MissingProducerArtifact:
        pass
    else:
        raise AssertionError("an artifact-less accepted receipt released dependents")


def test_seam_unsupported_receipt_version_fails_closed():
    sequence = _sched_sequence()
    receipts = {
        "gate-p": _receipt_record(
            state=ACCEPTANCE_ACCEPTED, producer_artifact="artifact-1", accepted=True
        )
    }
    receipts["gate-p"]["schema_version"] = "9.9.9"
    try:
        build_schedule(sequence, gate_receipts=receipts)
    except UnsupportedContractVersion:
        pass
    else:
        raise AssertionError("an unsupported receipt version was coerced by the seam")


def test_seam_omitting_gate_receipts_preserves_existing_behavior():
    """Backward compatibility: no receipts -> the legacy gate_results path."""
    sequence = _sched_sequence()
    # gate_results says the gate failed: dep blocked, exactly as before.
    blocked_schedule = build_schedule(sequence, gate_results={"gate-p": False})
    assert "dep" in blocked_schedule.blocked

    passed_schedule = build_schedule(sequence, gate_results={"gate-p": True})
    assert "dep" not in passed_schedule.blocked


def _controller_service(tmp_path):
    from skillweave.runtime.store import SQLiteRunStore

    store = SQLiteRunStore(db_path=str(tmp_path / "store.db"))
    service = NativeProcessService(store, holder="controller-1")
    service.acquire(ttl_seconds=300)
    return service


def test_seam_controller_acceptance_refuses_launched_artifactless(tmp_path):
    """Criterion 1 through the controller acceptance service."""
    service = _controller_service(tmp_path)
    launched = AttemptAcceptance(attempt_id="att-launched", dependents=("dep-a",))
    # A launched, artifact-less attempt cannot release dependents.
    try:
        service.release_accepted_dependents(launched)
    except MissingProducerArtifact:
        pass
    else:
        raise AssertionError("controller released an artifact-less launch")

    # Recording an acceptance for it is refused too: no producer artifact.
    try:
        service.record_acceptance(launched, accepted=True, answering_identity="v1")
    except MissingProducerArtifact:
        pass
    else:
        raise AssertionError("controller accepted a launched artifact-less attempt")


def test_seam_controller_acceptance_requires_independent_identity(tmp_path):
    """Criterion 3 through the controller acceptance service."""
    service = _controller_service(tmp_path)
    accepted = _accepted_attempt()
    # A producer answering its own acceptance never yields an accepted record.
    try:
        service.record_acceptance(
            accepted,
            producer_artifact="artifact-1",
            binding=_binding(),
            role="verifier",
            answering_identity="worker-1",
            producer_identity="worker-1",
            accepted=True,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("the controller accepted a self-answered attempt")

    # A genuine independent verifier produces a valid accepted receipt.
    receipt = service.record_acceptance(
        accepted,
        producer_artifact="artifact-1",
        binding=_binding(),
        role="verifier",
        answering_identity="verifier-1",
        producer_identity="worker-1",
        accepted=True,
    )
    assert receipt.accepted is True
    assert receipt.state == ACCEPTANCE_ACCEPTED


# ── criterion 4: collected package reviews ─────────────────────────────────
#
# Collecting related package reviews must preserve per-criterion receipts,
# the Ops/reviewer separation and the designated escalation/pro-review policy.
# These exercise both the collection seam directly and the *real* controller
# seam (``NativeProcessService.collect_package_reviews``).

from skillweave.trace import (  # noqa: E402
    OPS_ROLE,
    REVIEW_POLICY_ESCALATED,
    REVIEW_POLICY_STANDARD,
    ReviewPolicyError,
    ReviewerAuthorityPolicy,
    PackageReview,
    PackageReviewCollection,
    collect_package_review,
)


def _package_receipt(
    *,
    attempt_id: str,
    artifact: str,
    accepted: bool = True,
    answering: str = "verifier-1",
    producer: str = "worker-1",
    revision: str = "git:abc123",
    digest: str = "sha256:deadbeef",
) -> IndependentGateReceipt:
    return IndependentGateReceipt(
        attempt_id=attempt_id,
        state=ACCEPTANCE_ACCEPTED,
        producer_artifact=artifact,
        binding=ReviewBinding(
            review_id=f"rev-{attempt_id}",
            review_criteria=(attempt_id,),
            subject_revision=revision,
            artifact_digest=digest,
        ),
        role="verifier",
        answering_identity=answering,
        producer_identity=producer,
        accepted=accepted,
    )


def _ok_review(criterion: str, *, answering: str = "verifier-1") -> PackageReview:
    return PackageReview(
        criterion=criterion,
        receipt=_package_receipt(
            attempt_id=f"att-{criterion}", artifact=f"artifact-{criterion}"
        ),
        role="verifier",
        answering_identity=answering,
        producer_identity="worker-1",
    )


def test_c04_collects_related_reviews_preserving_per_criterion_receipts():
    policy = ReviewerAuthorityPolicy()
    review_a = _ok_review("criterion-a")
    review_b = _ok_review("criterion-b")
    collection = collect_package_review(
        "package-1",
        criteria=["criterion-a", "criterion-b"],
        reviews=[review_b, review_a],
        policy=policy,
    )
    assert isinstance(collection, PackageReviewCollection)
    # every criterion keeps *its own* receipt, and ordered deterministically
    assert collection.accepted_criteria == ("criterion-a", "criterion-b")
    assert collection.accepted is True
    assert collection.required_criteria == ()
    assert collection.receipt_for("criterion-a") is review_a.receipt
    assert collection.receipt_for("criterion-b") is review_b.receipt
    assert set(collection.receipts()) == {"criterion-a", "criterion-b"}
    # the per-criterion receipt round-trips unchanged
    assert collection.receipt_for("criterion-a").to_dict() == review_a.receipt_dict()


def test_c04_negative_receipt_reuse_is_refused():
    """No two criteria may collapse into one receipt."""
    shared = _package_receipt(attempt_id="att-shared", artifact="artifact-shared")
    policy = ReviewerAuthorityPolicy()
    reviews = [
        PackageReview(
            criterion="criterion-a",
            receipt=shared,
            role="verifier",
            answering_identity="verifier-1",
            producer_identity="worker-1",
        ),
        PackageReview(
            criterion="criterion-b",
            receipt=shared,
            role="verifier",
            answering_identity="verifier-1",
            producer_identity="worker-1",
        ),
    ]
    try:
        collect_package_review(
            "package-1",
            criteria=["criterion-a", "criterion-b"],
            reviews=reviews,
            policy=policy,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a reused per-criterion receipt was collected")


def test_c04_negative_unsupported_receipt_version_fails_closed():
    """A tampered per-criterion receipt is re-validated, never coerced."""
    good = _package_receipt(attempt_id="att-a", artifact="artifact-a")
    raw = good.to_dict()
    raw["schema_version"] = "9.9.9"
    # Reconstructing a tampered version is refused fail-closed.
    try:
        IndependentGateReceipt.from_dict(raw)
    except UnsupportedContractVersion:
        pass
    else:
        raise AssertionError("from_dict coerced an unsupported version")
    # The consumer's own validation refuses it too.
    try:
        validate_independent_gate_receipt(raw)
    except UnsupportedContractVersion:
        pass
    else:
        raise AssertionError("an unsupported per-criterion receipt was coerced")


def test_c04_negative_ops_role_reviewer_is_refused():
    """Ops/reviewer separation: the producer role may never answer."""
    policy = ReviewerAuthorityPolicy()
    review = PackageReview(
        criterion="criterion-a",
        receipt=_package_receipt(attempt_id="att-a", artifact="artifact-a"),
        role=OPS_ROLE,
        answering_identity="ops-1",
        producer_identity="worker-1",
    )
    try:
        collect_package_review(
            "package-1", criteria=["criterion-a"], reviews=[review], policy=policy
        )
    except ReviewPolicyError as exc:
        assert exc.criterion == "criterion-a"
    else:
        raise AssertionError("an Ops-role review was collected as independent")

    # A review answered by the producer identity is likewise not independent.
    self_answered = PackageReview(
        criterion="criterion-a",
        receipt=_package_receipt(attempt_id="att-a", artifact="artifact-a"),
        role="verifier",
        answering_identity="worker-1",
        producer_identity="worker-1",
    )
    try:
        collect_package_review(
            "package-1",
            criteria=["criterion-a"],
            reviews=[self_answered],
            policy=policy,
        )
    except ReviewPolicyError:
        pass
    else:
        raise AssertionError("a producer-answered review was collected as independent")


def test_c04_negative_escalated_criterion_needs_designated_pro_review():
    """A designated escalation is not quietly taken by the ordinary reviewer."""
    policy = ReviewerAuthorityPolicy(
        escalation_policy=REVIEW_POLICY_ESCALATED,
        pro_review_identity="pro-verifier-9",
        pro_reviewer_role="verifier",
    )
    ordinary = _ok_review("criterion-a", answering="verifier-1")
    try:
        collect_package_review(
            "package-1", criteria=["criterion-a"], reviews=[ordinary], policy=policy
        )
    except ReviewPolicyError as exc:
        assert "designated" in exc.reason or "pro" in exc.reason
    else:
        raise AssertionError("an escalated criterion was answered by the cold reviewer")

    # The designated pro reviewer — by role *and* identity — collects.
    pro_review = PackageReview(
        criterion="criterion-a",
        receipt=_package_receipt(
            attempt_id="att-a", artifact="artifact-a", answering="pro-verifier-9"
        ),
        role="verifier",
        answering_identity="pro-verifier-9",
        producer_identity="worker-1",
    )
    collection = collect_package_review(
        "package-1", criteria=["criterion-a"], reviews=[pro_review], policy=policy
    )
    assert collection.escalations() == ("criterion-a",)
    assert collection.accepted is True


def test_c04_negative_partial_package_is_not_accepted():
    """A package without an accepted receipt for every criterion is required."""
    policy = ReviewerAuthorityPolicy()
    failed = PackageReview(
        criterion="criterion-b",
        receipt=_package_receipt(
            attempt_id="att-b", artifact="artifact-b", accepted=False
        ),
        role="verifier",
        answering_identity="verifier-1",
        producer_identity="worker-1",
    )
    collection = collect_package_review(
        "package-1",
        criteria=["criterion-a", "criterion-b"],
        reviews=[_ok_review("criterion-a"), failed],
        policy=policy,
    )
    assert collection.accepted is False
    assert collection.accepted_criteria == ("criterion-a",)
    assert collection.required_criteria == ("criterion-b",)


def test_c04_backward_compat_criteria_1_3_reviews_collect_unchanged():
    """A standard package of criteria-1-3 receipts is additive; nothing changes."""
    policy = ReviewerAuthorityPolicy(escalation_policy=REVIEW_POLICY_STANDARD)
    collection = collect_package_review(
        "package-1",
        criteria=["c1", "c2", "c3"],
        reviews=[_ok_review("c1"), _ok_review("c2"), _ok_review("c3")],
        policy=policy,
    )
    assert collection.accepted is True
    assert collection.escalations() == ()
    # v1 records round-trip verbatim (backward compatibility of the contract).
    for criterion in ("c1", "c2", "c3"):
        record = collection.receipt_for(criterion).to_dict()
        assert record["schema_version"] == GATE_RECEIPT_SCHEMA_VERSION
        validate_independent_gate_receipt(record)


def test_c04_negative_unknown_criterion_review_is_refused():
    policy = ReviewerAuthorityPolicy()
    try:
        collect_package_review(
            "package-1",
            criteria=["criterion-a"],
            reviews=[_ok_review("criterion-a"), _ok_review("criterion-z")],
            policy=policy,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a review for an undeclared criterion was collected")


# ── criterion 4 through the real controller seam ───────────────────────────

def test_c04_seam_controller_collects_package_reviews(tmp_path):
    """Criterion 4 through the controller: a package collects at the lease."""
    service = _controller_service(tmp_path)
    policy = ReviewerAuthorityPolicy()
    collection = service.collect_package_reviews(
        "package-1",
        criteria=["criterion-a", "criterion-b"],
        reviews=[_ok_review("criterion-a"), _ok_review("criterion-b")],
        policy=policy,
    )
    assert collection.accepted is True
    assert collection.accepted_criteria == ("criterion-a", "criterion-b")


def test_c04_seam_controller_refuses_policy_violating_package(tmp_path):
    """The controller refuses a package that reuses a receipt or leaks Ops."""
    service = _controller_service(tmp_path)
    policy = ReviewerAuthorityPolicy()

    shared = _package_receipt(attempt_id="att-shared", artifact="artifact-shared")
    reused = [
        PackageReview(
            criterion="criterion-a",
            receipt=shared,
            role="verifier",
            answering_identity="verifier-1",
            producer_identity="worker-1",
        ),
        PackageReview(
            criterion="criterion-b",
            receipt=shared,
            role="verifier",
            answering_identity="verifier-1",
            producer_identity="worker-1",
        ),
    ]
    try:
        service.collect_package_reviews(
            "package-1",
            criteria=["criterion-a", "criterion-b"],
            reviews=reused,
            policy=policy,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("the controller collected a reused-receipt package")

    ops_review = PackageReview(
        criterion="criterion-a",
        receipt=_package_receipt(attempt_id="att-a", artifact="artifact-a"),
        role=OPS_ROLE,
        answering_identity="ops-1",
        producer_identity="worker-1",
    )
    try:
        service.collect_package_reviews(
            "package-1", criteria=["criterion-a"], reviews=[ops_review], policy=policy
        )
    except ReviewPolicyError:
        pass
    else:
        raise AssertionError("the controller collected an Ops-role package")


def test_c04_seam_controller_requires_controller_lease(tmp_path):
    """The collection seam is gated by the controller lease, like acceptance."""
    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.runtime.native_process import NotControllerError

    store = SQLiteRunStore(db_path=str(tmp_path / "store.db"))
    other = NativeProcessService(store, holder="controller-2")
    try:
        other.collect_package_reviews(
            "package-1",
            criteria=["criterion-a"],
            reviews=[_ok_review("criterion-a")],
            policy=ReviewerAuthorityPolicy(),
        )
    except NotControllerError:
        pass
    else:
        raise AssertionError("a non-controller collected a package review")

