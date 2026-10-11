"""Focused tests for bounded semantic preflight repair (SW-159-REPAIR-001).

Covers the versioned ``PreflightFailure`` contract, the two required grounded
auto-repairs (nonexistent ``modifies`` path, wrong-language source path), the
in-place revalidate + redispatch against an unchanged target digest, and the
four holds: repeated fingerprint, target drift, exhausted attempt/budget, and
authority expansion.

Self-contained ``sys.path`` handling follows the ``test_preflight_sw135.py``
convention.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from skillweave.runtime.journal import EventJournal
from skillweave.runtime.preflight import (
    PREFLIGHT_FAILURE_SCHEMA_VERSION,
    FailureClass,
    PreflightFailure,
    PreflightResult,
    Retryability,
    digest_target,
)
from skillweave.runtime.semantic_repair import (
    BoundedRepairer,
    HoldReason,
    collect_grounding_evidence,
    detect_surface_failure,
    language_of,
    repair_plan_from_grounding,
)


def _yes(role, capability, failure):
    return True


def _no(role, capability, failure):
    return False


def _repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real_module.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
    return tmp_path


# --- versioned contract -----------------------------------------------------

def test_failure_json_carries_required_fields():
    failure = PreflightFailure(
        failure_class=FailureClass.NONEXISTENT_MODIFIES_PATH.value,
        target_digest="a" * 64,
        evidence=[{"kind": "missing_target", "path": "src/x.py"}],
        implicated_fields=["modifies"],
        retryability=Retryability.AUTO_REPAIRABLE.value,
        authority_requirement="can_mutate_run_state",
    )
    payload = failure.to_dict()
    for key in (
        "schema_version",
        "failure_class",
        "target_digest",
        "evidence",
        "implicated_fields",
        "retryability",
        "authority_requirement",
    ):
        assert key in payload
    assert payload["schema_version"] == PREFLIGHT_FAILURE_SCHEMA_VERSION


def test_failure_round_trips_and_rejects_unknown_version():
    failure = PreflightFailure(
        failure_class=FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value,
        target_digest="b" * 64,
        evidence=[],
        implicated_fields=["modifies"],
        retryability=Retryability.AUTO_REPAIRABLE.value,
        authority_requirement="can_mutate_run_state",
    )
    restored = PreflightFailure.from_dict(failure.to_dict())
    assert restored.fingerprint == failure.fingerprint
    try:
        PreflightFailure(
            failure_class="x", target_digest="c", evidence=[],
            implicated_fields=[], retryability="manual",
            authority_requirement="y", schema_version="9.9.9",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("unknown schema_version must be rejected")


def test_fingerprint_is_stable_and_ignores_volatile_fields():
    a = PreflightFailure(
        failure_class=FailureClass.NONEXISTENT_MODIFIES_PATH.value,
        target_digest="d" * 64,
        evidence=[{"kind": "missing_target", "path": "src/x.py"}],
        implicated_fields=["modifies"],
        retryability=Retryability.AUTO_REPAIRABLE.value,
        authority_requirement="can_mutate_run_state",
    )
    b = PreflightFailure(
        failure_class=FailureClass.NONEXISTENT_MODIFIES_PATH.value,
        target_digest="d" * 64,
        evidence=[{"kind": "missing_target", "path": "src/x.py"}],
        implicated_fields=["modifies"],
        retryability=Retryability.AUTO_REPAIRABLE.value,
        authority_requirement="can_mutate_run_state",
        detail="different prose",
    )
    assert a.fingerprint == b.fingerprint


# --- grounded detection of the two required auto-repairs --------------------

def test_repair_nonexistent_modifies_path_from_grounding(tmp_path):
    repo = _repo(tmp_path)
    plan = {
        "id": "T-1",
        "acceptanceCriteria": ["keep intent"],
        "lane": {"modifies": ["src/missing_module.py"], "tests": ["tests/t.py"]},
    }
    target = digest_target(plan)
    failure = detect_surface_failure(plan, repo, target_digest=target)
    assert failure.failure_class == FailureClass.NONEXISTENT_MODIFIES_PATH.value
    assert failure.implicated_fields == ["modifies"]

    outcome = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target, authorize=_yes
    )
    assert outcome.repaired
    # The missing path is replaced by a real one from the grounding scan.
    assert outcome.repaired_plan["lane"]["modifies"] == ["src/real_module.py"]
    # Task intent and every non-implicated field survive untouched.
    assert outcome.repaired_plan["acceptanceCriteria"] == ["keep intent"]
    assert outcome.repaired_plan["lane"]["tests"] == ["tests/t.py"]
    # Original plan is not mutated.
    assert plan["lane"]["modifies"] == ["src/missing_module.py"]


def test_repair_wrong_language_source_path_from_grounding(tmp_path):
    repo = _repo(tmp_path)
    plan = {
        "id": "T-2",
        "acceptanceCriteria": ["c"],
        "language": "Python",
        "lane": {"modifies": ["src/notes.md"]},
    }
    target = digest_target(plan)
    failure = detect_surface_failure(plan, repo, target_digest=target)
    assert failure.failure_class == FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value
    assert language_of("src/notes.md") == "Markdown"
    assert language_of("src/real_module.py") == "Python"

    outcome = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target, authorize=_yes
    )
    assert outcome.repaired
    repaired = outcome.repaired_plan["lane"]["modifies"]
    assert repaired == ["src/real_module.py"]
    assert language_of(repaired[0]) == "Python"


def test_no_failure_when_surfaces_are_consistent(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-3", "lane": {"modifies": ["src/real_module.py"]}}
    assert detect_surface_failure(plan, repo, target_digest=digest_target(plan)) is None


# --- revalidate + redispatch against the unchanged target digest ------------

def test_revalidate_and_redispatch_against_unchanged_digest(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-4", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    seen = {}

    def revalidate(repaired_plan):
        seen["revalidated"] = repaired_plan["lane"]["modifies"]
        return PreflightResult(passed=True)

    def redispatch(repaired_plan):
        seen["redispatched"] = repaired_plan["lane"]["modifies"]
        return "dispatch:lane-T-4:attempt-1"

    outcome = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target,
        authorize=_yes, revalidate=revalidate, redispatch=redispatch,
    )
    assert outcome.repaired
    assert seen["revalidated"] == ["src/real_module.py"]
    assert seen["redispatched"] == ["src/real_module.py"]
    assert outcome.dispatch_identity == "dispatch:lane-T-4:attempt-1"
    assert outcome.target_digest == target


def test_failed_revalidation_holds_and_does_not_redispatch(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-5", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    dispatched = []
    outcome = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target, authorize=_yes,
        revalidate=lambda p: PreflightResult(passed=False, mismatches=[{"field": "x"}]),
        redispatch=lambda p: dispatched.append(p) or "nope",
    )
    assert outcome.held
    assert dispatched == []


# --- the four holds ----------------------------------------------------------

def test_hold_on_repeated_fingerprint(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-6", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    failure = detect_surface_failure(plan, repo, target_digest=target)
    repairer = BoundedRepairer(max_attempts=5, authorize=_yes)
    first = repairer.repair(
        plan, failure, role="ops", current_target_digest=target,
        evidence=failure.evidence,
        redispatch=lambda p: "dispatch:1",
    )
    assert first.repaired
    # Re-presenting the SAME failure must hold instead of repairing again.
    second = repairer.repair(
        plan, failure, role="ops", current_target_digest=target,
        evidence=failure.evidence,
    )
    assert second.hold_reason == HoldReason.REPEATED_FINGERPRINT.value
    assert second.attempts[-1].hold_reason == HoldReason.REPEATED_FINGERPRINT.value


def test_hold_on_target_drift(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-7", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    failure = detect_surface_failure(plan, repo, target_digest=target)
    repairer = BoundedRepairer(max_attempts=5, authorize=_yes)
    drifted = digest_target({"moved": True})
    outcome = repairer.repair(
        plan, failure, role="ops", current_target_digest=drifted,
        evidence=failure.evidence,
    )
    assert outcome.hold_reason == HoldReason.TARGET_DRIFT.value


def test_hold_on_exhausted_attempts_and_budget(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-8", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    failure = detect_surface_failure(plan, repo, target_digest=target)

    # Exhausted by declaring a single prior attempt up front.
    repairer = BoundedRepairer(max_attempts=1, authorize=_yes)
    repairer.attempts.append(_dummy_attempt(failure))
    held = repairer.repair(
        plan, failure, role="ops", current_target_digest=target,
        evidence=failure.evidence,
    )
    assert held.hold_reason == HoldReason.ATTEMPTS_EXHAUSTED.value

    # Budget: max_correction_rounds caps attempts more tightly.
    budgeted = BoundedRepairer(max_attempts=10, max_correction_rounds=1, authorize=_yes)
    budgeted.attempts.append(_dummy_attempt(failure))
    held_budget = budgeted.repair(
        plan, failure, role="ops", current_target_digest=target,
        evidence=failure.evidence,
    )
    assert held_budget.hold_reason == HoldReason.BUDGET_EXHAUSTED.value


def _dummy_attempt(failure):
    from skillweave.runtime.semantic_repair import RepairAttempt

    return RepairAttempt(
        attempt=1, failure_fingerprint="prior", failure_class="prior",
        implicated_fields=[], before_digest="x", after_digest="x",
        command="promptchain-validate", exit_code=1, outcome="held",
    )


def test_hold_on_authority_expansion(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-9", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    failure = detect_surface_failure(plan, repo, target_digest=target)
    repairer = BoundedRepairer(max_attempts=5, authorize=_no)
    outcome = repairer.repair(
        plan, failure, role="reviewer", current_target_digest=target,
        evidence=failure.evidence,
    )
    assert outcome.hold_reason == HoldReason.AUTHORITY_EXPANSION.value


def test_identity_mismatch_is_non_retryable():
    from skillweave.runtime.preflight import classify_failure

    failure = classify_failure(
        {"field": "branch", "expected": "feature/a", "actual": "feature/b"},
        target_digest="e" * 64,
    )
    assert failure.retryability == Retryability.NON_RETRYABLE.value
    assert failure.implicated_fields == ["branch"]
    # And a non-retryable failure is held, never auto-applied.
    repairer = BoundedRepairer(max_attempts=5, authorize=_yes)
    outcome = repairer.repair(
        {"id": "T-10", "lane": {}}, failure, role="ops",
        current_target_digest="e" * 64,
    )
    assert outcome.hold_reason == HoldReason.NON_REPAIRABLE_CLASS.value


# --- append-only ledger ------------------------------------------------------

def test_attempt_ledger_records_before_after_command_and_dispatch(tmp_path):
    repo = _repo(tmp_path)
    plan = {"id": "T-11", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    journal = EventJournal(":memory:")
    outcome = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target, authorize=_yes,
        redispatch=lambda p: "dispatch:lane-T-11:1",
        journal=journal, journal_run_id="run-1",
    )
    assert outcome.repaired
    record = outcome.attempts[0]
    assert record.before_digest != record.after_digest
    assert record.exit_code == 0
    assert record.dispatch_identity == "dispatch:lane-T-11:1"

    events = journal.get_events("run-1")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["before_digest"] == record.before_digest
    assert payload["after_digest"] == record.after_digest
    assert payload["command"] == "promptchain-validate"
    assert payload["dispatch_identity"] == "dispatch:lane-T-11:1"


def test_repair_substitutes_one_candidate_and_never_widens_write_scope(tmp_path):
    """A 1-element write set stays 1 element, even with many candidates.

    Substituting every grounded candidate would broaden the lane's declared
    write scope — exactly the authority expansion a bounded repair must refuse.
    """
    from skillweave.runtime.preflight import failure_for_missing_path

    plan = {"id": "T-12", "lane": {"modifies": ["src/missing.py"]}}
    failure = failure_for_missing_path(
        path="src/missing.py", task_id="T-12", lane_field="modifies",
        target_digest=digest_target(plan),
        evidence=[
            {"kind": "missing_target", "path": "src/missing.py", "task_id": "T-12"},
            {"kind": "grounding_candidate", "path": "src/real_module.py"},
            {"kind": "grounding_candidate", "path": "src/notes.md"},
        ],
    )
    repairer = BoundedRepairer(max_attempts=5, authorize=_yes)
    outcome = repairer.repair(
        plan, failure, role="ops", current_target_digest=digest_target(plan),
    )
    assert outcome.repaired
    repaired = outcome.repaired_plan["lane"]["modifies"]
    assert repaired == ["src/real_module.py"]
    assert len(repaired) == len(plan["lane"]["modifies"])


def test_repeated_fingerprint_holds_across_fresh_repairers(tmp_path):
    """The durable journal, not instance state, enforces the fingerprint hold.

    ``repair_plan_from_grounding`` builds a fresh repairer per call, so the
    hold must be reconstructed from the append-only ledger; otherwise a loop
    would re-repair and re-dispatch the same failure forever.
    """
    repo = _repo(tmp_path)
    plan = {"id": "T-13", "lane": {"modifies": ["src/missing.py"]}}
    target = digest_target(plan)
    journal = EventJournal(":memory:")

    first = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target, authorize=_yes,
        redispatch=lambda p: "dispatch:T-13:1",
        journal=journal, journal_run_id="loop-run",
    )
    assert first.repaired

    second = repair_plan_from_grounding(
        plan, repo, role="ops", current_target_digest=target, authorize=_yes,
        redispatch=lambda p: "dispatch:T-13:2",
        journal=journal, journal_run_id="loop-run",
    )
    assert second.held
    assert second.hold_reason == HoldReason.REPEATED_FINGERPRINT.value
    assert second.dispatch_identity is None


def test_grounding_evidence_is_bounded_and_relative(tmp_path):
    repo = _repo(tmp_path)
    evidence = collect_grounding_evidence(repo)
    assert "src/real_module.py" in evidence
    assert all(not os.path.isabs(p) for p in evidence)
    assert not any(p.startswith(".git/") for p in evidence)
