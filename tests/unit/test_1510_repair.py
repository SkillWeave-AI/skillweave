"""SW-1510-REPAIR: classified bounded repair bound to the Assay controller.

The task is a *seam*. Existing semantic preflight repair
(:mod:`skillweave.runtime.semantic_repair`) is connected to the Assay
controller (:class:`skillweave.runtime.assay.AssayRepairController`), so a
classified failure becomes exactly one permitted action, and a permitted
action only happens when the Assay policy and the admission gates say so.

Criterion 1 -- six error classes, each with one tested allowed action:
  ``auth_quota``/``capability_mismatch`` -> hold, ``context_pressure`` -> split,
  ``malformed_artifact`` -> remediate, ``source_drift`` -> reschedule,
  ``repeated_semantic_failure`` -> hold.

Criterion 2 -- every correction preserves the prior attempts and records the
  failure fingerprint, hypothesis, changed input, verifier result and a bounded
  correction count, under the frozen ``attempt-event-v1`` contract.

Criterion 3 -- repeated fingerprints, broader writes, changed authority/subject,
  exhausted budget and unsupported routes all HOLD; shrinking scope is explicit
  and acceptance criteria are never altered silently.

Criteria 1-3 are proven through the *actual consumer wiring* (``BoundedRepairer``
and ``repair_plan_from_grounding`` hand a classified action to
``AssayRepairController``, which gates it through the Assay policy and admission
envelope, redispatches only on approval, and validates/records the emitted
``attempt-event-v1`` correction) and again through a *fresh installed wheel's
own interpreter* with the source tree off ``sys.path`` -- so a passing helper
name or a source-tree shim cannot stand in for the binding.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.runtime import semantic_repair as sr  # noqa: E402
from skillweave.runtime.assay import (  # noqa: E402
    REPAIR_ALLOWED_ACTION,
    AdmissionRefused,
    AssayAdmission,
    AssayEngine,
    AssayRepairController,
    AssayState,
    RepairActionRequest,
    StaticActorResolver,
)
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.preflight import (  # noqa: E402
    PreflightResult,
    digest_target,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The frozen execution-contract id the correction records are emitted under.
ATTEMPT_EVENT_SCHEMA_ID = "https://skillweave.dev/schemas/execution/attempt-event/v1"

#: class -> the single permitted action, as the contract requires (the six
#: distinguishable classes of criterion 1).
_CONTRACT_ALLOWED = [
    (sr.ErrorClass.AUTH_QUOTA.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.CAPABILITY_MISMATCH.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.CONTEXT_PRESSURE.value, sr.RepairAction.SPLIT.value),
    (sr.ErrorClass.MALFORMED_ARTIFACT.value, sr.RepairAction.REMEDIATE.value),
    (sr.ErrorClass.SOURCE_DRIFT.value, sr.RepairAction.RESCHEDULE.value),
    (sr.ErrorClass.REPEATED_SEMANTIC_FAILURE.value, sr.RepairAction.HOLD.value),
]

#: The fail-closed class: a failure that matches no typed signal. It is a HOLD
#: so an unnamed failure can never default to a permissive remediation.
_FAIL_CLOSED_ALLOWED = (sr.ErrorClass.UNCLASSIFIED.value, sr.RepairAction.HOLD.value)

#: Every class in the vocabulary and its single permitted action.
_EXPECTED_ALLOWED = [*_CONTRACT_ALLOWED, _FAIL_CLOSED_ALLOWED]


# ---------------------------------------------------------------------------
# Real Assay controller, admission and engine (no stand-ins for the gate)
# ---------------------------------------------------------------------------

def _engine(policy_name="unicorn", envelope=None, tokens=None):
    resolver = StaticActorResolver(tokens or {"good": "worker-1"})
    engine = AssayEngine(
        policy_name=policy_name, max_turns=10, run_id="run-1",
        actor_resolver=resolver, envelope=envelope,
    )
    engine.pin(subject="subj-1", brief="brief-1")
    return engine


def _admission(engine, token="good", subject=None, brief=None):
    return AssayAdmission(
        actor_token=token,
        subject=subject if subject is not None else engine.subject_digest,
        brief=brief if brief is not None else engine.brief_digest,
        envelope=engine.envelope,
    )


def _controller(policy_name="unicorn", *, engine=None, admission=None):
    if engine is None:
        engine = _engine(policy_name)
    if admission is None:
        admission = _admission(engine)
    return engine, AssayRepairController(engine=engine, admission=admission)


def _repo(tmp_path):
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "real_module.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Criterion 1: six error classes, one tested allowed action each
# ---------------------------------------------------------------------------

def test_module_and_controller_agree_on_six_class_action_table():
    # The module is the authority; the controller-side mirror must not drift.
    module_table = {
        cls.value: action.value for cls, action in sr.ALLOWED_ACTION.items()
    }
    assert module_table == dict(_EXPECTED_ALLOWED)
    assert REPAIR_ALLOWED_ACTION == module_table
    # The six distinguishable classes of criterion 1, plus the fail-closed
    # class that keeps an unclassifiable failure out of a permissive action.
    assert len(_CONTRACT_ALLOWED) == 6
    assert len(sr.ErrorClass) == 7


@pytest.mark.parametrize("error_class,action", _EXPECTED_ALLOWED)
def test_allowed_action_is_the_single_permitted_action(error_class, action):
    assert sr.allowed_action(error_class) == action
    assert sr.ALLOWED_ACTION_BY_VALUE[error_class] == action


def test_unknown_error_class_is_refused_not_defaulted():
    with pytest.raises(sr.UnsupportedErrorClass):
        sr.allowed_action("totally_made_up")


#: Real provider rate-limit / usage-limit / auth phrasings. Each is an
#: auth/quota condition and must NEVER be classified as a malformed artifact
#: (which would permit an automated ``remediate`` of a quota failure).
_QUOTA_PHRASINGS = [
    "HTTP 429 rate_limit: you exceeded the 5-hour usage limit",
    "429 Too Many Requests",
    "rate limit exceeded, retry later",
    "you have reached your usage limit",
    "insufficient_quota: you exceeded your current quota",
    "insufficient credit balance",
    "402 Payment Required",
    "401 Unauthorized",
    "403 Forbidden",
    "you are out of credits",
]


@pytest.mark.parametrize("message", _QUOTA_PHRASINGS)
def test_provider_quota_phrasings_are_auth_quota_never_remediated(message):
    error_class = sr.classify_error(message=message)
    assert error_class == sr.ErrorClass.AUTH_QUOTA.value
    assert error_class != sr.ErrorClass.MALFORMED_ARTIFACT.value
    # A quota failure is never automated: its only permitted action is HOLD.
    assert sr.allowed_action(error_class) == sr.RepairAction.HOLD.value

    # ...and the Assay controller holds it (no automation) rather than
    # approving a remediation on the strength of the wrong class.
    engine, controller = _controller("unicorn")
    decision = controller.act(
        RepairActionRequest(error_class=error_class, action="remediate")
    )
    assert decision.approved is False
    assert decision.requires_human is True


def test_mixed_signal_message_resolves_to_the_most_specific_class():
    # "capability ... route": both signals present; the specific capability
    # signal wins deterministically, so the class is not the incidental route.
    message = (
        "model does not support tool use; the requested capability is "
        "unavailable on this route"
    )
    assert sr.classify_error(message=message) == sr.ErrorClass.CAPABILITY_MISMATCH.value
    # A message that is *only* a quota signal still classifies as quota.
    assert sr.classify_error(message="quota exhausted on the route") == (
        sr.ErrorClass.AUTH_QUOTA.value
    )
    # Repeated application is deterministic (no ordering flakiness).
    assert sr.classify_error(message=message) == sr.classify_error(message=message)


def test_unclassifiable_failure_fails_closed_to_a_hold(tmp_path):
    # A message carrying no typed signal is *not* silently a malformed artifact.
    assert sr.classify_error(message="zzz nothing recognisable here") == (
        sr.ErrorClass.UNCLASSIFIED.value
    )

    # Through the real seam: an absence of any grounded failure, error or
    # signal is a HOLD, never a PASS.
    repo = _repo(tmp_path)
    plan = {"id": "T-u", "lane": {"modifies": ["src/real_module.py"]}}
    outcome = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=digest_target(plan),
        authorize=lambda r, c, f: True,
    )
    assert outcome.held is True
    assert outcome.error_class == sr.ErrorClass.UNCLASSIFIED.value
    assert outcome.action_taken == sr.RepairAction.HOLD.value

    # The controller holds the unclassified class as a human decision.
    engine, controller = _controller("unicorn")
    decision = controller.act(
        RepairActionRequest(error_class="unclassified", action="hold")
    )
    assert decision.approved is False
    assert decision.requires_human is True


def test_correction_count_must_be_a_positive_integer(tmp_path):
    from skillweave.runtime.preflight import failure_for_missing_path
    failure = failure_for_missing_path(
        path="src/x.py", task_id="T", lane_field="modifies", target_digest="a" * 64,
    )
    attempt = sr.RepairAttempt(
        attempt=1, failure_fingerprint=failure.fingerprint,
        failure_class=failure.failure_class, implicated_fields=["modifies"],
        before_digest="x", after_digest="y", command="promptchain-validate",
        exit_code=0, outcome="repaired", error_class="malformed_artifact",
        correction_count=1,
    )
    good = attempt.to_attempt_event(run_id="r", task_id="T")
    sr.validate_attempt_event(good)  # a 1-based count is accepted

    # The bounded correction count is 1-based: zero, negatives, non-integers
    # and booleans are refused rather than guessed into a supported shape.
    for bad in (-1, 0, "3", 1.5, True):
        event = dict(
            good, payload=dict(good["payload"], correction_count=bad)
        )
        with pytest.raises(ValueError):
            sr.validate_attempt_event(event)

    # A correction event carrying an unknown class is refused too.
    unknown = dict(good, payload=dict(good["payload"], error_class="not_a_class"))
    with pytest.raises(ValueError):
        sr.validate_attempt_event(unknown)


@pytest.mark.parametrize("error_class,action", _EXPECTED_ALLOWED)
def test_classify_error_produces_each_of_the_six_classes(error_class, action):
    """A concrete diagnosis maps to each of the six classes, action included."""
    if error_class == sr.ErrorClass.AUTH_QUOTA.value:
        assert sr.classify_error(budget_exhausted=True) == error_class
        assert sr.classify_error(error=AdmissionRefused("actor", "no credential")) == error_class
    elif error_class == sr.ErrorClass.CAPABILITY_MISMATCH.value:
        assert sr.classify_error(
            error=AdmissionRefused("capability", "role lacks capability")
        ) == error_class
    elif error_class == sr.ErrorClass.CONTEXT_PRESSURE.value:
        assert sr.classify_error(
            error=RuntimeError("context window exceeded during probe")
        ) == error_class
    elif error_class == sr.ErrorClass.MALFORMED_ARTIFACT.value:
        from skillweave.runtime.preflight import failure_for_missing_path
        failure = failure_for_missing_path(
            path="src/x.py", task_id="T", lane_field="modifies",
            target_digest="a" * 64,
        )
        assert sr.classify_error(failure=failure) == error_class
    elif error_class == sr.ErrorClass.SOURCE_DRIFT.value:
        from skillweave.runtime.preflight import classify_failure
        failure = classify_failure(
            {"field": "branch", "expected": "a", "actual": "b"},
            target_digest="b" * 64,
        )
        assert sr.classify_error(failure=failure) == error_class
    elif error_class == sr.ErrorClass.REPEATED_SEMANTIC_FAILURE.value:
        from skillweave.runtime.preflight import failure_for_missing_path
        failure = failure_for_missing_path(
            path="src/x.py", task_id="T", lane_field="modifies",
            target_digest="c" * 64,
        )
        assert sr.classify_error(
            failure=failure, prior_fingerprints=[failure.fingerprint]
        ) == error_class
    elif error_class == sr.ErrorClass.UNCLASSIFIED.value:
        # Nothing typed matched: the classification fails closed, it does not
        # default to a permissive "malformed artifact -> remediate".
        assert sr.classify_error(message="zzz unrecognised gibberish") == error_class
        assert sr.classify_error() == error_class
    assert sr.allowed_action(error_class) == action


@pytest.mark.parametrize("error_class,action", _EXPECTED_ALLOWED)
def test_controller_permits_only_the_allowed_action_for_each_class(error_class, action):
    engine, controller = _controller("unicorn")
    decision = controller.act(
        RepairActionRequest(error_class=error_class, action=action)
    )
    if action == sr.RepairAction.HOLD.value:
        # A hold class is a human decision by design, never automated.
        assert decision.approved is False
        assert decision.requires_human is True
    else:
        # Unicorn approves the permitted reversible action...
        assert decision.approved is True
        assert decision.requires_human is False
    assert decision.error_class == error_class
    assert decision.policy == "unicorn"
    # ...and a non-allowed route for the same class is refused.
    other = next(a for _, a in _EXPECTED_ALLOWED if a != action)
    refused = controller.act(RepairActionRequest(error_class=error_class, action=other))
    assert refused.approved is False
    assert "unsupported route" in refused.reason


def test_conservative_policy_holds_even_a_permitted_reversible_action():
    engine, controller = _controller("conservative")
    decision = controller.act(
        RepairActionRequest(error_class="malformed_artifact", action="remediate")
    )
    assert decision.approved is False
    assert decision.policy == "conservative"
    assert "requires approval" in decision.reason
    assert engine.state == AssayState.PIN  # nothing mutated


# ---------------------------------------------------------------------------
# Criterion 2: every correction records the required evidence, append-only
# ---------------------------------------------------------------------------

def test_successful_correction_records_all_required_facts(tmp_path):
    repo = _repo(tmp_path)
    plan = {
        "id": "T-1", "acceptanceCriteria": ["keep intent"],
        "lane": {"modifies": ["src/missing.py"], "tests": ["tests/t.py"]},
    }
    target = digest_target(plan)
    engine, controller = _controller("unicorn")

    outcome = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True,
        revalidate=lambda p: PreflightResult(passed=True),
        redispatch=lambda p: "dispatch:T-1:1",
        controller=controller, scope="lane-T-1",
    )

    assert outcome.repaired is True
    assert outcome.error_class == sr.ErrorClass.MALFORMED_ARTIFACT.value
    assert outcome.action_taken == sr.RepairAction.REMEDIATE.value
    assert outcome.repaired_plan["lane"]["modifies"] == ["src/real_module.py"]

    event = outcome.attempt_event
    assert event is not None
    assert event["schema_id"] == ATTEMPT_EVENT_SCHEMA_ID
    assert event["schema_version"] == sr.ATTEMPT_EVENT_SCHEMA_VERSION
    payload = event["payload"]
    # The five facts criterion 2 requires of every correction.
    assert payload["failure_fingerprint"]  # fingerprint
    assert payload["hypothesis"].startswith("malformed_artifact:")  # hypothesis
    assert payload["changed_input"] == {
        "modifies": {"from": ["src/missing.py"], "to": ["src/real_module.py"]}
    }  # changed input, bounded to the implicated field
    assert payload["verifier_result"] == "revalidation_passed"  # verifier result
    assert payload["correction_count"] == 1  # bounded correction count
    # Task intent and non-implicated fields survive untouched.
    assert outcome.repaired_plan["acceptanceCriteria"] == ["keep intent"]
    assert outcome.repaired_plan["lane"]["tests"] == ["tests/t.py"]
    assert plan["lane"]["modifies"] == ["src/missing.py"]  # input not mutated


def test_attempt_event_validates_and_is_recorded_by_the_consumer(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-2", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    engine, controller = _controller("unicorn")
    outcome = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True, controller=controller,
        redispatch=lambda p: "dispatch:T-2:1",
    )
    event = outcome.attempt_event
    sr.validate_attempt_event(event)  # does not raise
    recorded = controller.record_correction(event)
    assert recorded is event
    assert controller.corrections == [event]


def test_unsupported_attempt_event_version_is_refused_not_guessed(tmp_path):
    from skillweave.runtime.preflight import failure_for_missing_path
    failure = failure_for_missing_path(
        path="src/x.py", task_id="T", lane_field="modifies", target_digest="a" * 64,
    )
    attempt = sr.RepairAttempt(
        attempt=1, failure_fingerprint=failure.fingerprint,
        failure_class=failure.failure_class, implicated_fields=["modifies"],
        before_digest="x", after_digest="y", command="promptchain-validate",
        exit_code=0, outcome="repaired", error_class="malformed_artifact",
        correction_count=1,
    )
    event = attempt.to_attempt_event(run_id="r", task_id="T")
    sr.validate_attempt_event(event)
    bad = dict(event, schema_version="9.9.9")
    with pytest.raises(sr.UnsupportedAttemptEventVersion):
        sr.validate_attempt_event(bad)
    engine, controller = _controller("unicorn")
    with pytest.raises(sr.UnsupportedAttemptEventVersion):
        controller.record_correction(bad)
    assert controller.corrections == []


def test_prior_attempts_are_preserved_across_repair_calls(tmp_path):
    """The correction ledger is append-only: the earlier attempt survives.

    ``repair_plan_from_grounding`` builds a fresh repairer per call and seeds
    it from the durable journal, so the second (held) outcome still carries the
    first correction -- prior attempts are preserved, never overwritten.
    """
    repo = _repo(tmp_path)
    plan = {"id": "T-3", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    journal = EventJournal(":memory:")

    first = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True,
        redispatch=lambda p: "dispatch:T-3:1",
        journal=journal, journal_run_id="run-T-3",
    )
    assert first.repaired is True
    assert len(first.attempts) == 1

    second = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True,
        redispatch=lambda p: "dispatch:T-3:2",
        journal=journal, journal_run_id="run-T-3",
    )
    assert second.held is True
    # Prior attempt preserved, and the new hold appended (1 -> 2).
    assert len(second.attempts) == 2
    assert second.attempts[0].failure_fingerprint == first.attempts[0].failure_fingerprint
    assert second.attempts[0].outcome == "repaired"
    assert second.attempts[-1].outcome == "held"
    # The bounded count is real evidence of how many corrections were tried.
    assert second.attempts[0].correction_count == 1
    assert second.attempts[-1].correction_count == 2
    assert len(journal.get_events("run-T-3")) == 2


# ---------------------------------------------------------------------------
# Criterion 3: HOLD for every refusal; shrinking scope is explicit
# ---------------------------------------------------------------------------

def _missing_failure(tmp_path, plan, *, target_digest=None):
    target = target_digest or digest_target(plan)
    return sr.detect_surface_failure(plan, _repo(tmp_path), target_digest=target)


def test_hold_on_target_drift_is_source_drift_reschedule_surface(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-4", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    failure = sr.detect_surface_failure(plan, repo, target_digest=target)
    drifted = digest_target({"moved": True})
    # The very same repair against a moved target is held, and the HOLD is a
    # source-drift decision whose permitted action is reschedule.
    outcome = sr.BoundedRepairer(max_attempts=5, authorize=lambda r, c, f: True).repair(
        plan, failure, role="ops", current_target_digest=drifted,
    )
    assert outcome.hold_reason == sr.HoldReason.TARGET_DRIFT.value
    assert outcome.error_class == sr.ErrorClass.SOURCE_DRIFT.value
    assert outcome.action_taken == sr.RepairAction.RESCHEDULE.value


def test_hold_on_error_class_action_table_matches_outcomes(tmp_path):
    """Each HOLD outcome exposes a class whose permitted action is 'hold'."""
    repo = _repo(tmp_path)
    plan = {"id": "T-5", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)

    # Attempts exhausted -> auth_quota (no authorized fallback). A prior
    # attempt (distinct fingerprint) is seeded first so the loop is already at
    # its ceiling.
    failure = sr.detect_surface_failure(plan, repo, target_digest=target)
    authz = lambda r, c, f: True  # noqa: E731
    prior = sr.RepairAttempt(
        attempt=1, failure_fingerprint="prior-fingerprint",
        failure_class="nonexistent_modifies_path", implicated_fields=["modifies"],
        before_digest="x", after_digest="x", command="promptchain-validate",
        exit_code=1, outcome="held",
    )
    exhausted = sr.BoundedRepairer(max_attempts=1, authorize=authz)
    exhausted.seed_prior_attempts([prior.to_dict()])
    held = exhausted.repair(plan, failure, role="ops", current_target_digest=target)
    assert held.hold_reason == sr.HoldReason.ATTEMPTS_EXHAUSTED.value
    assert held.error_class == sr.ErrorClass.AUTH_QUOTA.value
    assert held.action_taken == sr.RepairAction.HOLD.value

    # Authority expansion -> capability_mismatch; held.
    denied = sr.BoundedRepairer(max_attempts=5, authorize=lambda r, c, f: False)
    held2 = denied.repair(plan, failure, role="reviewer", current_target_digest=target)
    assert held2.hold_reason == sr.HoldReason.AUTHORITY_EXPANSION.value
    assert held2.error_class == sr.ErrorClass.CAPABILITY_MISMATCH.value
    assert held2.action_taken == sr.RepairAction.HOLD.value

    # Repeated fingerprint -> repeated_semantic_failure; held.
    repeat = sr.BoundedRepairer(max_attempts=5, authorize=authz)
    first = repeat.repair(plan, failure, role="ops", current_target_digest=target)
    assert first.repaired is True
    second = repeat.repair(plan, failure, role="ops", current_target_digest=target)
    assert second.hold_reason == sr.HoldReason.REPEATED_FINGERPRINT.value
    assert second.error_class == sr.ErrorClass.REPEATED_SEMANTIC_FAILURE.value
    assert second.action_taken == sr.RepairAction.HOLD.value


def test_controller_admission_refusal_holds_authority_or_subject_change(tmp_path):
    """A changed/forged authority or a swapped subject never becomes a mutation."""
    repo = _repo(tmp_path)
    plan = {"id": "T-6", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)

    # A forged bearer token (changed authority).
    engine = _engine("unicorn")
    engine_bad = AssayRepairController(engine=engine, admission=_admission(engine, token="bad"))
    forged = engine_bad.act(
        RepairActionRequest(error_class="malformed_artifact", action="remediate")
    )
    assert forged.approved is False
    assert "admission refused (actor)" in forged.reason

    # A swapped subject (the pinned subject changed under the action).
    engine2 = _engine("unicorn")
    swapped = AssayRepairController(
        engine=engine2, admission=_admission(engine2, subject="different-subject")
    ).act(RepairActionRequest(error_class="malformed_artifact", action="remediate"))
    assert swapped.approved is False
    assert "admission refused (subject)" in swapped.reason

    # And through the real repair seam: a refused admission holds the repair.
    engine3 = _engine("unicorn")
    controller3 = AssayRepairController(engine=engine3, admission=None)  # no admission
    outcome = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True,
        revalidate=lambda p: PreflightResult(passed=True),
        redispatch=lambda p: "must-not-happen",
        controller=controller3, scope="lane-T-6",
    )
    assert outcome.held is True
    assert outcome.hold_reason == sr.HoldReason.CONTROLLER_HELD.value
    assert outcome.dispatch_identity is None
    assert "admission refused" in outcome.attempts[-1].verifier_result


def test_repair_never_widens_write_scope_and_keeps_acceptance_criteria(tmp_path):
    """A multi-entry write set stays the same width; criteria are untouched."""
    repo = _repo(tmp_path)
    plan = {
        "id": "T-7", "acceptanceCriteria": ["do not change me"],
        "lane": {"modifies": ["src/missing.py", "src/other_missing.py"]},
    }
    target = digest_target(plan)
    outcome = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True, redispatch=lambda p: "dispatch:T-7:1",
    )
    assert outcome.repaired is True
    repaired = outcome.repaired_plan["lane"]["modifies"]
    # 1:1 substitution: the width is unchanged (expanding the write set would be
    # exactly the authority expansion a bounded repair must refuse).
    assert len(repaired) == len(plan["lane"]["modifies"])
    assert repaired == ["src/real_module.py", "src/other_missing.py"]
    assert outcome.repaired_plan["acceptanceCriteria"] == ["do not change me"]
    # The only recorded change is the implicated field.
    assert outcome.attempts[-1].changed_input == {
        "modifies": {
            "from": ["src/missing.py", "src/other_missing.py"],
            "to": ["src/real_module.py", "src/other_missing.py"],
        }
    }


def test_exhausted_budget_is_never_a_fallback(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-8", "lane": {"modifies": ["src/real_module.py"]}}
    outcome = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=digest_target(plan),
        authorize=lambda r, c, f: True, budget_exhausted=True,
    )
    assert outcome.held is True
    assert outcome.error_class == sr.ErrorClass.AUTH_QUOTA.value
    assert outcome.action_taken == sr.RepairAction.HOLD.value
    assert sr.allowed_action(outcome.error_class) == sr.RepairAction.HOLD.value


def test_non_repairable_identity_failure_is_classified_and_held(tmp_path):
    from skillweave.runtime.preflight import classify_failure
    failure = classify_failure(
        {"field": "branch", "expected": "feature/a", "actual": "feature/b"},
        target_digest="d" * 64,
    )
    outcome = sr.BoundedRepairer(max_attempts=5, authorize=lambda r, c, f: True).repair(
        {"id": "T-9", "lane": {}}, failure, role="ops", current_target_digest="d" * 64,
    )
    assert outcome.held is True
    assert outcome.hold_reason == sr.HoldReason.NON_REPAIRABLE_CLASS.value
    assert outcome.error_class == sr.ErrorClass.SOURCE_DRIFT.value


# ---------------------------------------------------------------------------
# Installed native consumer: a fresh wheel's interpreter, source off sys.path
# ---------------------------------------------------------------------------

_INSTALLED_DRIVER = r'''
import json, sys
import skillweave
from skillweave.runtime import assay as A
from skillweave.runtime import semantic_repair as SR
from skillweave.runtime.preflight import PreflightResult, digest_target

repo = sys.argv[1]

resolver = A.StaticActorResolver({"good": "worker-1"})
engine = A.AssayEngine(policy_name="unicorn", max_turns=10, run_id="run-x",
                       actor_resolver=resolver)
engine.pin(subject="subj-1", brief="brief-1")
admission = A.AssayAdmission(actor_token="good", subject=engine.subject_digest,
                             brief=engine.brief_digest, envelope=engine.envelope)
controller = A.AssayRepairController(engine=engine, admission=admission)

plan = {"id": "T", "acceptanceCriteria": ["keep intent"],
        "lane": {"modifies": ["src/missing.py"]}}
target = digest_target(plan)
outcome = SR.repair_plan_from_grounding(
    plan, repo, role="ops", current_target_digest=target,
    authorize=lambda r, c, f: True,
    revalidate=lambda p: PreflightResult(passed=True),
    redispatch=lambda p: "dispatch:T:1",
    controller=controller, scope="lane-T",
)
event = outcome.attempt_event
SR.validate_attempt_event(event)
controller.record_correction(event)

# The consumer refuses a version it cannot support instead of guessing.
bad = json.loads(json.dumps(event))
bad["schema_version"] = "9.9.9"
refused = False
try:
    controller.record_correction(bad)
except SR.UnsupportedAttemptEventVersion:
    refused = True

# An unsupported route is refused by the controller, not executed.
route = controller.act(A.RepairActionRequest(error_class="source_drift",
                                             action="remediate"))

print(json.dumps({
    "module_file": skillweave.__file__,
    "repaired": outcome.repaired,
    "error_class": outcome.error_class,
    "action_taken": outcome.action_taken,
    "plan_modifies": outcome.repaired_plan["lane"]["modifies"],
    "acceptance": outcome.repaired_plan["acceptanceCriteria"],
    "dispatch": outcome.dispatch_identity,
    "schema_id": event["schema_id"],
    "schema_version": event["schema_version"],
    "payload": event["payload"],
    "corrections": len(controller.corrections),
    "unsupported_version_refused": refused,
    "unsupported_route_approved": route.approved,
    "engine_state": engine.state.value,
    "policy": controller.policy.name,
}, sort_keys=True))
'''


def _clean_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _venv_site(venv_dir):
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _build_wheel(work):
    (work / "src").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / "src", work / "src")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, work / name)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
         "--no-build-isolation", "-w", str(work / "dist"), str(work)],
        cwd=str(work), capture_output=True, text=True, env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted((work / "dist").glob("skillweave-*.whl"))
    assert wheels, f"no wheel built in {work / 'dist'}"
    return wheels[-1]


def _install_into_fresh_venv(work, wheel):
    venv_dir = work / "venv"
    env = _clean_env()
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True, env=env)
    site = _venv_site(venv_dir)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", "--no-index",
         str(wheel)],
        check=True, capture_output=True, text=True, env=env,
    )
    # The offline install cannot fetch PyYAML; bundle the ambient copy so the
    # installed package imports without reaching the network or source tree.
    import yaml  # noqa: PLC0415 - intentional late import

    ambient_yaml = Path(yaml.__file__).resolve().parent
    shutil.copytree(ambient_yaml, site / "yaml")
    extra = ambient_yaml.parent / "_yaml"
    if extra.exists():
        shutil.copytree(extra, site / "_yaml")
    return venv_dir


def _run_installed(venv_dir, tmp_path, repo):
    driver = tmp_path / "driver.py"
    driver.write_text(_INSTALLED_DRIVER, encoding="utf-8")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = _clean_env()
    env["TMPDIR"] = str(tmp_path / "tmp")
    # cwd carries no ``skillweave`` and PYTHONPATH is cleared, so the import
    # can only resolve to the *installed* wheel.
    proc = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-B", str(driver), str(repo)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_installed_wheel_binds_classified_repair_to_the_assay_controller(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "real_module.py").write_text("x = 1\n", encoding="utf-8")

    result = _run_installed(venv_dir, tmp_path, repo)

    # The installed native consumer -- not the source tree -- ran the binding.
    assert str(venv_dir) in result["module_file"]
    assert result["repaired"] is True
    assert result["policy"] == "unicorn"
    assert result["plan_modifies"] == ["src/real_module.py"]
    assert result["acceptance"] == ["keep intent"]
    assert result["dispatch"] == "dispatch:T:1"
    assert result["schema_id"] == ATTEMPT_EVENT_SCHEMA_ID
    assert result["schema_version"] == sr.ATTEMPT_EVENT_SCHEMA_VERSION
    payload = result["payload"]
    assert payload["error_class"] == "malformed_artifact"
    assert payload["correction_count"] == 1
    assert payload["verifier_result"] == "revalidation_passed"
    assert payload["failure_fingerprint"]
    assert payload["changed_input"] == {
        "modifies": {"from": ["src/missing.py"], "to": ["src/real_module.py"]}
    }
    assert result["corrections"] == 1
    assert result["unsupported_version_refused"] is True
    assert result["unsupported_route_approved"] is False
    assert result["engine_state"] == "pin"  # the gate never mutated the run


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
