"""SW-1510-REPAIR: classified bounded repair bound to the Assay controller.

The task is a *seam*. Existing semantic preflight repair
(:mod:`skillweave.runtime.semantic_repair`) is connected to the Assay
controller (:class:`skillweave.runtime.assay.AssayRepairController`), so a
classified failure becomes exactly one permitted action, and a permitted
action only happens when the Assay policy and the admission gates say so.

Criterion 1 -- the distinguishable error classes, each with one tested allowed
  action: ``auth_quota`` (legacy umbrella) and the SW-1511-REPAIR split of it
  into ``provider_quota``/``auth_refusal``/``budget_exhausted``/
  ``correction_exhausted``/``transport_failure`` -> hold,
  ``capability_mismatch`` -> hold, ``context_pressure`` -> split,
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
from datetime import datetime, timedelta, timezone
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
    AssayEnvelope,
    AssayRepairController,
    AssayState,
    RepairActionRequest,
    StaticActorResolver,
    TurnBudgetExhaustedError,
)
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.preflight import (  # noqa: E402
    PreflightResult,
    digest_target,
    failure_for_missing_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The frozen execution-contract id the correction records are emitted under.
ATTEMPT_EVENT_SCHEMA_ID = "https://skillweave.dev/schemas/execution/attempt-event/v1"

#: class -> the single permitted action, as the contract requires. The five
#: SW-1511-REPAIR condition classes (split out of the legacy ``auth_quota``
#: umbrella) each get their own entry and a HOLD.
_CONTRACT_ALLOWED = [
    (sr.ErrorClass.AUTH_QUOTA.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.PROVIDER_QUOTA.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.AUTH_REFUSAL.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.BUDGET_EXHAUSTED.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.CORRECTION_EXHAUSTED.value, sr.RepairAction.HOLD.value),
    (sr.ErrorClass.TRANSPORT_FAILURE.value, sr.RepairAction.HOLD.value),
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

#: Criterion 1 (SW-1511-REPAIR): each condition a coarse failure message used
#: to conflate must land in its OWN class -- a mechanical correction exhaustion,
#: a repeated semantic deadend, an actual provider quota, an authentication
#: refusal and a local tool/transport failure are five distinct typed classes.
_DISTINCT_CONDITION_CLASSES = [
    (sr.ErrorClass.CORRECTION_EXHAUSTED.value, "correction budget exhausted"),
    (sr.ErrorClass.REPEATED_SEMANTIC_FAILURE.value, "repeated fingerprint"),
    (sr.ErrorClass.PROVIDER_QUOTA.value, "HTTP 429 usage limit exceeded"),
    (sr.ErrorClass.AUTH_REFUSAL.value, "401 unauthorized"),
    (sr.ErrorClass.TRANSPORT_FAILURE.value, "connection refused to the tool"),
]

#: SW-1510 legacy values still persisted in durable records and still emitted by
#: the adaptive ``backoff`` kind. They must remain live vocabulary members whose
#: action is still HOLD, so an old record replays rather than being refused.
_LEGACY_AUTH_QUOTA = sr.ErrorClass.AUTH_QUOTA.value


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

def test_module_and_controller_agree_on_class_action_table():
    # The module is the authority; the controller-side mirror must not drift.
    module_table = {
        cls.value: action.value for cls, action in sr.ALLOWED_ACTION.items()
    }
    assert module_table == dict(_EXPECTED_ALLOWED)
    assert REPAIR_ALLOWED_ACTION == module_table
    # The distinguishable condition classes (SW-1511-REPAIR split the legacy
    # ``auth_quota`` umbrella into five), plus the fail-closed class that keeps
    # an unclassifiable failure out of a permissive action.
    assert len(_CONTRACT_ALLOWED) == 11
    assert len(sr.ErrorClass) == 12
    # The legacy umbrella value survives for persisted records...
    assert sr.ErrorClass.AUTH_QUOTA.value == "auth_quota"
    # ...while the live conditions it used to conflate are distinct.
    assert len({
        sr.ErrorClass.PROVIDER_QUOTA, sr.ErrorClass.AUTH_REFUSAL,
        sr.ErrorClass.BUDGET_EXHAUSTED, sr.ErrorClass.CORRECTION_EXHAUSTED,
        sr.ErrorClass.TRANSPORT_FAILURE,
    }) == 5


@pytest.mark.parametrize("error_class,action", _EXPECTED_ALLOWED)
def test_allowed_action_is_the_single_permitted_action(error_class, action):
    assert sr.allowed_action(error_class) == action
    assert sr.ALLOWED_ACTION_BY_VALUE[error_class] == action


def test_unknown_error_class_is_refused_not_defaulted():
    with pytest.raises(sr.UnsupportedErrorClass):
        sr.allowed_action("totally_made_up")


#: Real *provider quota* phrasings: the provider itself refused with a real
#: rate/usage/credit condition. Each is a ``provider_quota`` and must NEVER be
#: classified as a malformed artifact (which would permit an automated
#: ``remediate`` of a quota failure) nor conflated with an auth refusal.
_PROVIDER_QUOTA_PHRASINGS = [
    "HTTP 429 rate_limit: you exceeded the 5-hour usage limit",
    "429 Too Many Requests",
    "rate limit exceeded, retry later",
    "you have reached your usage limit",
    "insufficient_quota: you exceeded your current quota",
    "insufficient credit balance",
    "402 Payment Required",
    "you are out of credits",
]

#: Real *authentication refusal* phrasings: the acting authority refused the
#: credential. Distinct from a provider quota, they must classify as
#: ``auth_refusal`` and never be remediated.
_AUTH_REFUSAL_PHRASINGS = [
    "401 Unauthorized",
    "403 Forbidden",
    "authentication failed: unrecognised credential",
    "invalid api key",
]


@pytest.mark.parametrize("message", _PROVIDER_QUOTA_PHRASINGS)
def test_provider_quota_phrasings_are_provider_quota_never_remediated(message):
    error_class = sr.classify_error(message=message)
    assert error_class == sr.ErrorClass.PROVIDER_QUOTA.value
    assert error_class != sr.ErrorClass.MALFORMED_ARTIFACT.value
    assert error_class != sr.ErrorClass.AUTH_REFUSAL.value
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


@pytest.mark.parametrize("message", _AUTH_REFUSAL_PHRASINGS)
def test_auth_refusal_phrasings_are_auth_refusal_never_remediated(message):
    error_class = sr.classify_error(message=message)
    assert error_class == sr.ErrorClass.AUTH_REFUSAL.value
    assert error_class != sr.ErrorClass.MALFORMED_ARTIFACT.value
    assert error_class != sr.ErrorClass.PROVIDER_QUOTA.value
    assert sr.allowed_action(error_class) == sr.RepairAction.HOLD.value

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
    # A message that is *only* a quota signal still classifies as a provider
    # quota (never an incidental capability/route class).
    assert sr.classify_error(message="quota exhausted on the route") == (
        sr.ErrorClass.PROVIDER_QUOTA.value
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
def test_classify_error_produces_each_of_the_classes(error_class, action):
    """A concrete diagnosis maps to each class, its permitted action included."""
    if error_class == sr.ErrorClass.AUTH_QUOTA.value:
        # The legacy umbrella value is still a live vocabulary member; it is no
        # longer what live classification produces for the condition classes it
        # used to conflate, so we only assert it is a recognised, held class.
        assert sr.allowed_action(error_class) == action
        return
    if error_class == sr.ErrorClass.PROVIDER_QUOTA.value:
        assert sr.classify_error(
            message="HTTP 429 rate_limit: exceeded usage limit"
        ) == error_class
    elif error_class == sr.ErrorClass.AUTH_REFUSAL.value:
        assert sr.classify_error(
            error=AdmissionRefused("actor", "no credential")
        ) == error_class
        assert sr.classify_error(message="401 Unauthorized") == error_class
    elif error_class == sr.ErrorClass.BUDGET_EXHAUSTED.value:
        assert sr.classify_error(budget_exhausted=True) == error_class
        assert sr.classify_error(
            error=AdmissionRefused("deadline", "deadline exceeded")
        ) == error_class
    elif error_class == sr.ErrorClass.CORRECTION_EXHAUSTED.value:
        assert sr.classify_error(message="correction budget exhausted") == error_class
    elif error_class == sr.ErrorClass.TRANSPORT_FAILURE.value:
        assert sr.classify_error(message="connection refused to tool harness") == error_class
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

    # Attempts exhausted -> correction_exhausted (a local control-policy
    # exhaustion, distinct from a provider quota or an auth refusal). A prior
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
    assert held.error_class == sr.ErrorClass.CORRECTION_EXHAUSTED.value
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
    assert outcome.error_class == sr.ErrorClass.BUDGET_EXHAUSTED.value
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
# Criterion 1 (SW-1511-REPAIR): the five condition classes are distinct
# ---------------------------------------------------------------------------

def test_the_five_conditions_have_five_distinct_typed_classes():
    """No two of the five conditions collapse into one class.

    The point of SW-1511-REPAIR is that a mechanical correction exhaustion, a
    repeated semantic deadend, a real provider quota, an authentication refusal
    and a local transport failure are *separately typed*, so each can carry a
    distinct control policy. A regression that re-coalesced any pair would make
    this fail.
    """
    classes = [sr.classify_error(message=msg) for _, msg in _DISTINCT_CONDITION_CLASSES]
    assert len(set(classes)) == len(classes), classes
    for (expected, msg), got in zip(_DISTINCT_CONDITION_CLASSES, classes):
        assert got == expected, f"{msg!r} -> {got!r} (expected {expected!r})"


def test_condition_classes_are_never_the_legacy_umbrella_or_unclassified():
    """The live classification produces the specific class, never ``auth_quota``.

    ``auth_quota`` remains a live value for backward compatibility, but no live
    message or axis may classify *into* it -- that would re-conflate the very
    conditions SW-1511-REPAIR separated. None may fall to ``unclassified``
    either (that would be an under-classification, not a distinct class).
    """
    for expected, msg in _DISTINCT_CONDITION_CLASSES:
        got = sr.classify_error(message=msg)
        assert got == expected
        assert got != _LEGACY_AUTH_QUOTA
        assert got != sr.ErrorClass.UNCLASSIFIED.value

    # The structured axes drive the same split, so a raised refusal (not just a
    # message) classifies distinctly too.
    def _refused(axis):
        return AdmissionRefused(axis, f"{axis} refused")

    assert sr.classify_error(error=_refused("actor")) == sr.ErrorClass.AUTH_REFUSAL.value
    assert sr.classify_error(error=_refused("quota")) == sr.ErrorClass.PROVIDER_QUOTA.value
    assert sr.classify_error(error=_refused("correction")) == (
        sr.ErrorClass.CORRECTION_EXHAUSTED.value
    )
    assert sr.classify_error(error=_refused("tool")) == (
        sr.ErrorClass.TRANSPORT_FAILURE.value
    )
    assert sr.classify_error(error=_refused("deadline")) == (
        sr.ErrorClass.BUDGET_EXHAUSTED.value
    )


# ---------------------------------------------------------------------------
# Criterion 2: no hard ceiling for an authorized-unconstrained run; the finite
# owner envelope is what remains enforced
# ---------------------------------------------------------------------------

def test_unconstrained_authorized_run_has_no_hard_attempt_ceiling(tmp_path):
    """``max_attempts=None`` imposes no artificial attempt/round ceiling.

    An explicitly owner-authorized unconstrained run carries distict fingerprints
    that each repair successfully; the loop must not stop it at an invented
    default ceiling. Distinct fingerprints keep the *repeated-fingerprint* hold
    (a real progress signal) out of the way, so what is measured here is the
    absence of a cumulative ceiling -- not a progress hold.
    """
    repo = _repo(tmp_path)
    world = sr.collect_grounding_evidence(repo)
    repairer = sr.BoundedRepairer(max_attempts=None, authorize=lambda r, c, f: True)

    # 25 distinct, grounded failures. Each `modifies` names a different missing
    # file and each carries its own target digest, so no two failures share a
    # fingerprint: the *repeated-fingerprint* hold is a real progress signal and
    # is kept out of the way, isolating what is measured here -- the absence of
    # an invented cumulative ceiling. The repair scan supplies the grounding
    # evidence a real preflight would have produced.
    for i in range(25):
        target = digest_target({"probe": "unbounded", "n": i})
        plan = {"id": "T", "lane": {"modifies": ["src/missing_%d.py" % i]}}
        failure = sr.detect_surface_failure(
            plan, repo, target_digest=target, evidence=world
        )
        assert failure is not None and failure.failure_class == (
            sr.FailureClass.NONEXISTENT_MODIFIES_PATH.value
        ), i
        outcome = repairer.repair(
            plan, failure, role="ops", current_target_digest=target,
            redispatch=lambda p, i=i: "dispatch:u:%d" % i,
        )
        assert outcome.repaired is True, (i, outcome.hold_reason)
        assert outcome.repaired_plan["lane"]["modifies"] == ["src/real_module.py"]

    assert len(repairer.attempts) == 25
    # No hold of the attempts/budget kind was ever produced.
    assert all(a.outcome == "repaired" for a in repairer.attempts)


def test_finite_attempt_ceiling_is_still_enforced_when_declared(tmp_path):
    """The no-ceiling path is opt-in: a declared finite ceiling still holds.

    ``max_attempts=1`` refuses a second correction with the
    correction-exhausted class, so removing a hard ceiling for an
    *unconstrained* run did not silently remove the finite ceiling a caller
    declares.
    """
    repo = _repo(tmp_path)
    world = sr.collect_grounding_evidence(repo)
    repairer = sr.BoundedRepairer(max_attempts=1, authorize=lambda r, c, f: True)
    base = digest_target({"probe": "bounded", "n": 0})
    plan_a = {"id": "T", "lane": {"modifies": ["src/a.py"]}}
    f1 = sr.detect_surface_failure(plan_a, repo, target_digest=base, evidence=world)
    first = repairer.repair(
        plan_a, f1, role="ops", current_target_digest=base,
    )
    assert first.repaired is True

    moved = digest_target({"probe": "bounded", "n": 1})
    plan_b = {"id": "T", "lane": {"modifies": ["src/b.py"]}}
    f2 = sr.detect_surface_failure(plan_b, repo, target_digest=moved, evidence=world)
    second = repairer.repair(
        plan_b, f2, role="ops", current_target_digest=moved,
    )
    assert second.held is True
    assert second.hold_reason == sr.HoldReason.ATTEMPTS_EXHAUSTED.value
    assert second.error_class == sr.ErrorClass.CORRECTION_EXHAUSTED.value


def test_max_attempts_non_positive_or_bool_is_refused_not_a_hidden_ceiling():
    for bad in (0, -1, True, False, 1.5, "3"):
        with pytest.raises(ValueError):
            sr.BoundedRepairer(max_attempts=bad, authorize=lambda r, c, f: True)


def test_no_cumulative_limit_does_not_weaken_a_finite_owner_envelope():
    """A finite owner envelope is enforced independently of the attempt ceiling.

    ``max_turns=None`` removes the *turn* ceiling, but a finite
    ``remaining_budget`` still refuses the next turn at admission -- the
    "no hard cumulative ceiling" allowance never disables the owner envelope.
    """
    resolver = StaticActorResolver({"good": "worker-1"})
    engine = AssayEngine(
        policy_name="unicorn", max_turns=None, run_id="run-env",
        actor_resolver=resolver,
        envelope=AssayEnvelope(remaining_budget=2),
    )
    engine.pin(subject="subj-1", brief="brief-1")
    admission = AssayAdmission(
        actor_token="good", subject=engine.subject_digest,
        brief=engine.brief_digest, envelope=engine.envelope,
    )
    engine.advance_turn(admission=admission)
    engine.advance_turn(admission=admission)
    with pytest.raises(AdmissionRefused) as exc:
        engine.advance_turn(admission=admission)
    assert exc.value.axis == "budget"
    assert engine.current_turn == 2


def test_default_retry_handler_budget_is_not_wired_to_the_repair_path():
    """The repair path imposes no hidden ``RetryBudget`` cumululative ceiling.

    ``skillweave.execution.retry`` has its own module-local ``RetryBudget``; the
    bounded repair loop must not import it, or a second, hidden retry ceiling
    would silently cap an authorized-unconstrained run. This test fails if the
    repair module ever starts depending on that counter.
    """
    import importlib

    retry = importlib.import_module("skillweave.execution.retry")
    assert retry.RetryBudget().max_retries == 3  # a real, separate default
    # The repair module's own source names no retry-budget symbol.
    src_text = Path(sr.__file__).read_text(encoding="utf-8")
    assert "RetryBudget" not in src_text
    assert "execution.retry" not in src_text
    assert "execution import retry" not in src_text


def test_unsupported_implicated_field_holds_rather_than_guessing(tmp_path):
    """A failure naming a field the repair cannot edit must HOLD, not guess.

    Fail-closed control policy: a bounded repair edits ONLY the implicated
    field. If that field is not one the lane carries, there is nothing safe to
    change, so the loop holds with a causal reason rather than inventing a
    correction against some other field.
    """
    repo = _repo(tmp_path)
    world = sr.collect_grounding_evidence(repo)
    target = digest_target({"probe": "unknown-field"})
    plan = {"id": "T", "lane": {"modifies": ["src/missing.py"]}}
    failure = failure_for_missing_path(
        path="src/missing.py", task_id="T", lane_field="deletes",  # not a lane field
        target_digest=target,
        evidence=[
            {"kind": "missing_target", "path": "src/missing.py"},
            {"kind": "grounding_candidate", "path": world[0]},
        ],
    )
    repairer = sr.BoundedRepairer(max_attempts=5, authorize=lambda r, c, f: True)
    outcome = repairer.repair(plan, failure, role="ops", current_target_digest=target)
    assert outcome.held
    assert outcome.hold_reason == sr.HoldReason.IMPLICATED_FIELD_UNKNOWN.value
    # The plan is untouched: nothing was guessed, nothing was widened.
    assert plan["lane"] == {"modifies": ["src/missing.py"]}


def test_repair_never_widens_the_declared_write_scope(tmp_path):
    """A 1-element write set stays 1 element: a repair is a 1:1 substitution.

    Replacing the single offending entry with every grounded candidate would
    broaden the lane's declared write scope -- the authority expansion a bounded
    repair must refuse. The repaired set must have the same cardinality.
    """
    target = digest_target({"probe": "no-widen"})
    plan = {"id": "T", "lane": {"modifies": ["src/missing.py"]}}
    candidates = ["src/real_module.py", "src/notes.md", "docs/guide.md"]
    failure = failure_for_missing_path(
        path="src/missing.py", task_id="T", lane_field="modifies",
        target_digest=target,
        evidence=[
            {"kind": "missing_target", "path": "src/missing.py"},
            *({"kind": "grounding_candidate", "path": p} for p in candidates),
        ],
    )
    repairer = sr.BoundedRepairer(max_attempts=5, authorize=lambda r, c, f: True)
    outcome = repairer.repair(plan, failure, role="ops", current_target_digest=target)
    assert outcome.repaired
    repaired = outcome.repaired_plan["lane"]["modifies"]
    # One offending entry in, exactly one substitution out: the declared write
    # scope never grows, however many candidates were grounded.
    assert len(repaired) == len(plan["lane"]["modifies"]) == 1
    assert repaired[0] == candidates[0]


# ---------------------------------------------------------------------------
# Criterion 3: productive attempts extend the turn window; progress-free loops
# escalate or hold with causal evidence (never blind repeat or PASS)
# ---------------------------------------------------------------------------

def test_productive_attempt_extends_the_turn_window_with_actor_and_kind():
    """An authorized productive attempt can extend its own turn window.

    A run that is making progress may raise its turn ceiling; the change is a
    recorded, actor-attributed ``extension`` -- not a silent widening.
    """
    resolver = StaticActorResolver({"good": "alice"})
    engine = AssayEngine(
        policy_name="unicorn", max_turns=2, run_id="run-x", actor_resolver=resolver,
    )
    engine.pin(subject="subj-1", brief="brief-1")
    admission = AssayAdmission(
        actor_token="good", subject=engine.subject_digest,
        brief=engine.brief_digest, envelope=engine.envelope,
    )
    assert engine.max_turns == 2
    record = engine.set_turn_limit(8, admission=admission)
    assert record.max_turns == 8
    assert record.kind == "extension"
    assert record.actor == "alice"
    assert engine.max_turns == 8
    for _ in range(8):
        engine.advance_turn(admission=admission)
    assert engine.current_turn == 8


def test_unconstrained_turn_window_is_recorded_as_no_cumulative_limit():
    resolver = StaticActorResolver({"good": "alice"})
    engine = AssayEngine(
        policy_name="unicorn", max_turns=2, run_id="run-x", actor_resolver=resolver,
    )
    engine.pin(subject="subj-1", brief="brief-1")
    admission = AssayAdmission(
        actor_token="good", subject=engine.subject_digest,
        brief=engine.brief_digest, envelope=engine.envelope,
    )
    record = engine.set_turn_limit(None, admission=admission)
    assert record.kind == "no_cumulative_limit"
    for _ in range(30):
        engine.advance_turn(admission=admission)
    assert engine.current_turn == 30


def test_unauthorized_actor_cannot_extend_the_turn_window():
    resolver = StaticActorResolver({"good": "alice", "bad": ""})
    engine = AssayEngine(
        policy_name="unicorn", max_turns=2, run_id="run-x", actor_resolver=resolver,
    )
    engine.pin(subject="subj-1", brief="brief-1")
    bad = AssayAdmission(
        actor_token="bad", subject=engine.subject_digest,
        brief=engine.brief_digest, envelope=engine.envelope,
    )
    with pytest.raises(AdmissionRefused) as exc:
        engine.set_turn_limit(999, admission=bad)
    assert exc.value.axis == "actor"
    assert engine.max_turns == 2


def test_finite_turn_window_still_stops_a_finite_engine():
    resolver = StaticActorResolver({"good": "alice"})
    engine = AssayEngine(
        policy_name="unicorn", max_turns=2, run_id="run-x", actor_resolver=resolver,
    )
    engine.pin(subject="subj-1", brief="brief-1")
    admission = AssayAdmission(
        actor_token="good", subject=engine.subject_digest,
        brief=engine.brief_digest, envelope=engine.envelope,
    )
    engine.advance_turn(admission=admission)
    engine.advance_turn(admission=admission)
    with pytest.raises(TurnBudgetExhaustedError):
        engine.advance_turn(admission=admission)


def test_progress_free_repair_loop_holds_with_causal_evidence_not_blind_repeat(tmp_path):
    """A progress-free loop holds on the repeated fingerprint, with evidence.

    Re-presenting the *same* failure fingerprint must produce a HOLD carrying
    the repeated-semantic-failure class, the ``repeated_fingerprint`` reason and
    a positive correction count -- and must not redispatch. This is the
    "escalate or hold instead of blind repeat or PASS" half of criterion 3.
    """
    repo = _repo(tmp_path)
    plan = {"id": "T", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    failure = sr.detect_surface_failure(plan, repo, target_digest=target)
    assert failure is not None
    journal = EventJournal(":memory:")
    dispatched = []

    first = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True,
        redispatch=lambda p: dispatched.append("first") or "dispatch:T:1",
        journal=journal, journal_run_id="loop",
    )
    assert first.repaired is True

    second = sr.repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=lambda r, c, f: True,
        redispatch=lambda p: dispatched.append("second") or "dispatch:T:2",
        journal=journal, journal_run_id="loop",
    )
    assert second.held is True
    assert second.hold_reason == sr.HoldReason.REPEATED_FINGERPRINT.value
    assert second.error_class == sr.ErrorClass.REPEATED_SEMANTIC_FAILURE.value
    assert second.dispatch_identity is None
    assert dispatched == ["first"]  # the loop never blindly redispatched
    assert second.attempts[-1].correction_count >= 1
    assert second.attempts[-1].verifier_result == "repeated_fingerprint"


def test_adaptive_progress_free_loop_escalates_or_blocks_with_causal_evidence():
    """A Flash lane that stops progressing escalates to Pro, or blocks explicitly.

    With no cost ceiling the escalation is to Pro; with a Flash-only ceiling
    (``cost_ceiling=0``) it blocks and names the reason. Neither path loops, and
    neither silently *passes* -- the outcome is an explicit escalation or an
    explicit block.
    """
    from skillweave.dispatch.model_policy import (
        ESCALATION_THRESHOLD,
        EscalationState,
        ModelPolicyDeclaration,
        ModelTier,
    )

    def _drive(state, declaration):
        for _ in range(ESCALATION_THRESHOLD):
            state.record_non_progress()
        return state.escalate(declaration)

    # No ceiling: escalate to Pro (bounded, evidence-carrying).
    state = EscalationState()
    assert _drive(state, ModelPolicyDeclaration(cost_ceiling=None)).tier is ModelTier.PRO
    assert state.blocked is False

    # Flash-only ceiling: block explicitly instead of looping.
    blocked = EscalationState()
    outcome = _drive(blocked, ModelPolicyDeclaration(cost_ceiling=0))
    assert outcome.tier is ModelTier.FLASH
    assert outcome.blocked is True
    assert outcome.blocked_reason  # a reason, not a silent hold
    # Under the threshold no escalation decision is taken.
    assert EscalationState().escalate(ModelPolicyDeclaration()).tier is ModelTier.FLASH


def test_progress_free_loop_records_an_escalation_with_causal_evidence():
    """The escalation is recorded with its cause/revisions/outcome, not dropped.

    The adaptive ledger is the durable evidence that a progress-free loop was
    escalated rather than blindly repeated.
    """
    from skillweave.runtime.dagscheduler import (
        KIND_ESCALATION,
        SelectionLedger,
    )

    ledger = SelectionLedger(policy_revision="p-7", catalog_revision="c-3")
    record = ledger.record_escalation(
        cause="two accepted non-progress cycles at Flash",
        candidates=("pro/model-a",),
        rejected_routes=("flash/model-b",),
        budget_delta=0.5,
        outcome="escalated",
        subject="pro/model-a",
    )
    assert record.kind == KIND_ESCALATION
    assert record.cause
    assert record.policy_revision == "p-7"
    assert record.catalog_revision == "c-3"
    assert record.budget_delta == 0.5
    assert record.outcome == "escalated"
    persisted = ledger.to_dict()["records"]
    assert len(persisted) == 1
    assert persisted[0]["kind"] == KIND_ESCALATION
    assert persisted[0]["cause"] == record.cause


# ---------------------------------------------------------------------------
# Backward compatibility: the legacy ``auth_quota`` umbrella still replays
# ---------------------------------------------------------------------------

def test_legacy_auth_quota_is_still_live_and_held():
    assert _LEGACY_AUTH_QUOTA in {c.value for c in sr.ErrorClass}
    assert sr.allowed_action(_LEGACY_AUTH_QUOTA) == sr.RepairAction.HOLD.value
    assert REPAIR_ALLOWED_ACTION[_LEGACY_AUTH_QUOTA] == "hold"
    # A legacy table entry is still a recognised held class on the controller.
    engine, controller = _controller("unicorn")
    decision = controller.act(
        RepairActionRequest(error_class=_LEGACY_AUTH_QUOTA, action="hold")
    )
    assert decision.approved is False
    assert decision.requires_human is True


def test_legacy_auth_quota_attempt_event_from_the_adaptive_backoff_still_validates():
    """A persisted legacy ``auth_quota`` correction record still validates.

    The adaptive ``backoff`` kind maps to ``auth_quota`` (its frozen v1 error
    class); an such a record must still emit and validate under the frozen
    ``attempt-event-v1`` contract, so old durable records replay rather than
    being refused by the additive SW-1511 change.
    """
    from skillweave.runtime.dagscheduler import AdaptiveDecisionRecord, KIND_BACKOFF

    record = AdaptiveDecisionRecord(
        kind=KIND_BACKOFF,
        cause="provider asked for a reset window",
        candidates=("flash/model-a",),
        rejected_routes=("pro/model-a",),
        outcome="backoff",
        subject="flash/model-a",
    )
    event = record.to_attempt_event(run_id="r", task_id="T")
    assert event["payload"]["error_class"] == _LEGACY_AUTH_QUOTA
    sr.validate_attempt_event(event)  # still a supported, held class


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


# ---------------------------------------------------------------------------
# SW-1511-REPAIR criterion 4: without a permitted fallback, a real provider
# quota stores the reset/backoff window and resumes the SAME owned session on
# the SAME route; cancellation remains available while waiting.  Fail-closed.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


def _quota_wait(**overrides):
    kwargs = dict(
        error_class=sr.ErrorClass.PROVIDER_QUOTA.value,
        run_id="run-4747",
        task_id="SW-1511-REPAIR",
        attempt=2,
        session_id="sess-abc",
        route="faigate/pro-model",
        backoff_seconds=120,
        now=_NOW,
    )
    kwargs.update(overrides)
    return sr.attempt_condition_for_quota(**kwargs)


def test_provider_quota_without_fallback_waits_and_resumes_same_route():
    condition = _quota_wait()
    assert condition.error_class == sr.ErrorClass.PROVIDER_QUOTA.value
    # The permitted action is still the HOLD (SW-1511-REPAIR criterion 1): the
    # wait is not a repair, it is the holding control policy.
    assert condition.action == sr.ALLOWED_ACTION[sr.ErrorClass.PROVIDER_QUOTA].value
    assert condition.decision == sr.CONDITION_RESUME_SAME_ROUTE
    assert condition.waiting is True
    # The reset/backoff window is stored, not guessed.
    assert condition.backoff_seconds == 120
    assert condition.reset_at is not None
    reset = datetime.fromisoformat(condition.reset_at)
    assert reset == _NOW + timedelta(seconds=120)
    # The resume target is the SAME owned session on the SAME route.
    assert condition.route == "faigate/pro-model"
    assert condition.resume_route == condition.route
    assert condition.resume_attempt == condition.attempt
    assert condition.session_id == "sess-abc"
    # No fallback was taken, and none is inferred.
    assert condition.fallback_used is False
    assert condition.fallback_permitted is False


def test_waiting_attempt_stays_non_terminal_and_cancellable():
    from skillweave.runtime.store import RunStateModel

    condition = _quota_wait()
    assert condition.cancellable is True
    assert condition.run_state == RunStateModel.BLOCKED_WAITING_FOR_GATE.value
    # The wait state is deliberately non-terminal, so cancellation stays live.
    assert not RunStateModel.is_terminal(condition.run_state)
    assert RunStateModel.BLOCKED_WAITING_FOR_GATE in RunStateModel.legal_transitions(
        RunStateModel.IN_PROGRESS
    )
    # And the run can leave the wait to fail (the cancellation path).
    assert RunStateModel.FAILED in RunStateModel.legal_transitions(
        RunStateModel.BLOCKED_WAITING_FOR_GATE
    )


def test_local_budget_exhaustion_holds_without_a_stored_window():
    condition = sr.attempt_condition_for_quota(
        error_class=sr.ErrorClass.BUDGET_EXHAUSTED.value,
        run_id="run-1",
        now=_NOW,
    )
    assert condition.decision == sr.CONDITION_HOLD
    assert condition.waiting is False
    # No provider reset window is fabricated for a local envelope exhaustion.
    assert condition.reset_at is None
    assert condition.backoff_seconds is None
    assert condition.resume_attempt is None
    assert condition.resume_route == ""


def test_provider_quota_without_a_storable_window_fails_closed_to_hold():
    # A provider quota the caller cannot date must NOT be reported as a wait:
    # the decision degrades to a HOLD, never a fabricated schedule.
    undated = _quota_wait(backoff_seconds=None, reset_at=None)
    assert undated.decision == sr.CONDITION_HOLD
    assert undated.waiting is False
    assert undated.reset_at is None

    # An already-elapsed reset window is equally non-storable.
    elapsed = _quota_wait(
        backoff_seconds=None,
        reset_at=(_NOW - timedelta(seconds=1)).isoformat(),
    )
    assert elapsed.decision == sr.CONDITION_HOLD
    assert elapsed.reset_at is None


def test_a_stored_reset_at_in_the_past_is_refused_by_the_record():
    with pytest.raises(ValueError):
        sr.AttemptCondition(
            condition=sr.AttemptConditionKind.PROVIDER_QUOTA.value,
            error_class=sr.ErrorClass.PROVIDER_QUOTA.value,
            action=sr.RepairAction.HOLD.value,
            decision=sr.CONDITION_RESUME_SAME_ROUTE,
            run_id="r",
            attempt=1,
            session_id="s",
            route="faigate/pro-model",
            reset_at=(_NOW - timedelta(seconds=5)).isoformat(),
            backoff_seconds=5,
            resume_attempt=1,
            resume_route="faigate/pro-model",
            measured_at=_NOW.isoformat(),
        )


def test_a_substituted_resume_route_is_refused_by_the_record():
    with pytest.raises(ValueError):
        sr.AttemptCondition(
            condition=sr.AttemptConditionKind.PROVIDER_QUOTA.value,
            error_class=sr.ErrorClass.PROVIDER_QUOTA.value,
            action=sr.RepairAction.HOLD.value,
            decision=sr.CONDITION_RESUME_SAME_ROUTE,
            run_id="r",
            attempt=1,
            session_id="s",
            route="faigate/pro-model",
            reset_at=(_NOW + timedelta(seconds=5)).isoformat(),
            backoff_seconds=5,
            resume_attempt=1,
            # A different route would be a credential/route substitution.
            resume_route="faigate/other-model",
            measured_at=_NOW.isoformat(),
        )


def test_a_non_cancellable_wait_is_refused_by_the_record():
    with pytest.raises(ValueError):
        sr.AttemptCondition(
            condition=sr.AttemptConditionKind.PROVIDER_QUOTA.value,
            error_class=sr.ErrorClass.PROVIDER_QUOTA.value,
            action=sr.RepairAction.HOLD.value,
            decision=sr.CONDITION_RESUME_SAME_ROUTE,
            run_id="r",
            attempt=1,
            session_id="s",
            route="faigate/pro-model",
            reset_at=(_NOW + timedelta(seconds=5)).isoformat(),
            backoff_seconds=5,
            resume_attempt=1,
            resume_route="faigate/pro-model",
            cancellable=False,
            measured_at=_NOW.isoformat(),
        )


def test_a_permitted_fallback_is_never_recorded():
    # The criterion is the *no-permitted-fallback* case; a record that claims a
    # permitted fallback is outside this contract and refused rather than
    # silently accepted.
    with pytest.raises(ValueError):
        sr.AttemptCondition(
            condition=sr.AttemptConditionKind.PROVIDER_QUOTA.value,
            error_class=sr.ErrorClass.PROVIDER_QUOTA.value,
            action=sr.RepairAction.HOLD.value,
            decision=sr.CONDITION_RESUME_SAME_ROUTE,
            run_id="r",
            attempt=1,
            session_id="s",
            route="faigate/pro-model",
            reset_at=(_NOW + timedelta(seconds=5)).isoformat(),
            backoff_seconds=5,
            resume_attempt=1,
            resume_route="faigate/pro-model",
            fallback_permitted=True,
            measured_at=_NOW.isoformat(),
        )


def test_a_class_this_contract_does_not_govern_is_refused():
    # It must not become a second error vocabulary: an auth refusal, transport
    # failure or correction exhaustion is not a quota wait.
    for error_class in (
        sr.ErrorClass.AUTH_REFUSAL.value,
        sr.ErrorClass.TRANSPORT_FAILURE.value,
        sr.ErrorClass.CORRECTION_EXHAUSTED.value,
        sr.ErrorClass.CONTEXT_PRESSURE.value,
        sr.ErrorClass.UNCLASSIFIED.value,
    ):
        with pytest.raises(ValueError):
            sr.attempt_condition_for_quota(error_class=error_class, now=_NOW)


def test_message_only_call_still_classifies_provider_quota_distinctly():
    condition = sr.attempt_condition_for_quota(
        message="HTTP 429 Too Many Requests: usage limit exceeded",
        run_id="run-1",
        session_id="s",
        route="faigate/pro-model",
        backoff_seconds=60,
        now=_NOW,
    )
    assert condition.error_class == sr.ErrorClass.PROVIDER_QUOTA.value
    assert condition.decision == sr.CONDITION_RESUME_SAME_ROUTE
    # A message that matches nothing classifies as UNCLASSIFIED and is refused
    # by the factory (not a governed class), so the fail-closed path never
    # fabricates a wait for an unnamed failure.
    classified = sr.classify_error(message="something odd")
    assert classified == sr.ErrorClass.UNCLASSIFIED.value
    with pytest.raises(ValueError):
        sr.attempt_condition_for_quota(message="something odd", now=_NOW)


def test_attempt_condition_round_trips_and_validates():
    condition = _quota_wait()
    record = condition.to_attempt_condition(principal="worker-1")
    sr.validate_attempt_condition(record)
    assert record["schema_id"] == sr.ATTEMPT_CONDITION_SCHEMA_ID
    assert record["schema_version"] == sr.ATTEMPT_CONDITION_SCHEMA_VERSION
    assert record["route"] == "faigate/pro-model"
    assert record["evidence_digest"]
    assert record["payload"]["route"] == "faigate/pro-model"


def test_unsupported_attempt_condition_version_is_refused_not_guessed():
    record = _quota_wait().to_attempt_condition()
    record["schema_version"] = "9.9.9"
    with pytest.raises(sr.UnsupportedAttemptConditionVersion):
        sr.validate_attempt_condition(record)


def test_tampered_attempt_condition_is_refused_by_the_consumer():
    record = _quota_wait().to_attempt_condition()
    # A substituted decision/window would be a fabricated schedule.
    record["payload"] = dict(record["payload"])
    record["payload"]["resume_route"] = "faigate/other-model"
    with pytest.raises(ValueError):
        sr.validate_attempt_condition(record)

    # A valid record whose payload is edited without re-binding the digest is
    # refused too.
    record2 = _quota_wait().to_attempt_condition()
    record2["payload"] = dict(record2["payload"])
    record2["payload"]["reset_at"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        sr.validate_attempt_condition(record2)


# Installed native consumer: a fresh wheel's own interpreter, source off
# sys.path -- so a source-tree shim cannot stand in for the contract.

_CONDITION_INSTALLED_DRIVER = r'''
import json, sys
import skillweave
from skillweave.runtime import semantic_repair as SR
from skillweave.runtime.store import RunStateModel

condition = SR.attempt_condition_for_quota(
    error_class=SR.ErrorClass.PROVIDER_QUOTA.value,
    run_id="run-1", task_id="T", attempt=3, session_id="sess-1",
    route="faigate/pro-model", backoff_seconds=90,
)
record = condition.to_attempt_condition(principal="worker-1")
SR.validate_attempt_condition(record)

refused_version = False
bad = json.loads(json.dumps(record))
bad["schema_version"] = "9.9.9"
try:
    SR.validate_attempt_condition(bad)
except SR.UnsupportedAttemptConditionVersion:
    refused_version = True

refused_substitution = False
sub = json.loads(json.dumps(record))
sub["payload"]["resume_route"] = "faigate/other"
try:
    SR.validate_attempt_condition(sub)
except ValueError:
    refused_substitution = True

print(json.dumps({
    "module_file": skillweave.__file__,
    "schema_id": record["schema_id"],
    "schema_version": record["schema_version"],
    "decision": condition.decision,
    "resume_route": condition.resume_route,
    "route": condition.route,
    "resume_attempt": condition.resume_attempt,
    "attempt": condition.attempt,
    "backoff_seconds": condition.backoff_seconds,
    "reset_at": condition.reset_at,
    "run_state": condition.run_state,
    "terminal": RunStateModel.is_terminal(condition.run_state),
    "cancellable": condition.cancellable,
    "fallback_used": condition.fallback_used,
    "refused_version": refused_version,
    "refused_substitution": refused_substitution,
}, sort_keys=True))
'''


def _run_condition_installed(venv_dir, tmp_path):
    driver = tmp_path / "condition_driver.py"
    driver.write_text(_CONDITION_INSTALLED_DRIVER, encoding="utf-8")
    (tmp_path / "tmp2").mkdir(exist_ok=True)
    env = _clean_env()
    env["TMPDIR"] = str(tmp_path / "tmp2")
    proc = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-B", str(driver)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_installed_wheel_binds_the_attempt_condition_contract(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)

    result = _run_condition_installed(venv_dir, tmp_path)

    assert str(venv_dir) in result["module_file"]
    assert result["schema_id"] == sr.ATTEMPT_CONDITION_SCHEMA_ID
    assert result["schema_version"] == sr.ATTEMPT_CONDITION_SCHEMA_VERSION
    assert result["decision"] == sr.CONDITION_RESUME_SAME_ROUTE
    assert result["route"] == "faigate/pro-model"
    assert result["resume_route"] == "faigate/pro-model"
    assert result["resume_attempt"] == result["attempt"] == 3
    assert result["backoff_seconds"] == 90
    assert result["reset_at"]
    assert result["run_state"] == "blocked_waiting_for_gate"
    assert result["terminal"] is False
    assert result["cancellable"] is True
    assert result["fallback_used"] is False
    assert result["refused_version"] is True
    assert result["refused_substitution"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
