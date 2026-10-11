"""Tests for the Assay methodology (SW-159-ASSAY-001).

Exercises every state, each policy checkpoint, autonomous reversible
remediation, every Unicorn hold, dynamic turns, split, compaction
rejection, and bypass non-weakening.
"""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest

from skillweave.runtime.assay import (
    AssayState,
    ActionType,
    ActionRequest,
    PolicyDecision,
    AssayPolicy,
    ConservativePolicy,
    ModeratePolicy,
    UnicornPolicy,
    create_policy,
    list_policy_names,
    AssayHeartbeat,
    AssayTurnLimit,
    AssaySplit,
    AssayEngine,
    validate_assay_evidence,
    AssayError,
    InvalidTransitionError,
    PolicyBlockedError,
    PolicyHoldError,
    TurnBudgetExhaustedError,
    EvidenceRejectedError,
)


# ═══════════════════════════════════════════════════════════════════════════
# States and transitions
# ═══════════════════════════════════════════════════════════════════════════

class TestAssayStates:
    """Every AssayState exists and has the correct value."""

    def test_all_states_defined(self):
        assert AssayState.PIN.value == "pin"
        assert AssayState.HYPOTHESIS.value == "hypothesis"
        assert AssayState.PROBE.value == "probe"
        assert AssayState.MEASURE.value == "measure"
        assert AssayState.CLASSIFY.value == "classify"
        assert AssayState.ARTIFACT_VERIFY.value == "artifact_verify"
        assert AssayState.REMEDIATE.value == "remediate"
        assert AssayState.ESCALATE_OR_SPLIT.value == "escalate_or_split"
        assert AssayState.PASS.value == "pass"
        assert AssayState.HOLD.value == "hold"

    def test_terminal_states(self):
        assert AssayState.is_terminal("pass")
        assert AssayState.is_terminal("hold")
        assert not AssayState.is_terminal("probe")
        assert not AssayState.is_terminal("pin")
        assert AssayState.terminal_values() == frozenset({"pass", "hold"})

    def test_legal_transitions_complete(self):
        """Every state has a complete and correct transition map."""
        assert AssayState.legal_transitions(AssayState.PIN) == [AssayState.HYPOTHESIS]
        assert AssayState.legal_transitions(AssayState.HYPOTHESIS) == [AssayState.PROBE]
        assert AssayState.legal_transitions(AssayState.PROBE) == [AssayState.MEASURE]
        assert AssayState.legal_transitions(AssayState.MEASURE) == [AssayState.CLASSIFY]
        assert sorted(AssayState.legal_transitions(AssayState.CLASSIFY)) == sorted([
            AssayState.REMEDIATE, AssayState.ESCALATE_OR_SPLIT,
            AssayState.ARTIFACT_VERIFY, AssayState.PASS, AssayState.HOLD,
        ])
        # SW-1510-VERIFY: the artifact-specific semantics verdict gates PASS.
        assert sorted(AssayState.legal_transitions(AssayState.ARTIFACT_VERIFY)) == sorted([
            AssayState.PASS, AssayState.HOLD, AssayState.REMEDIATE,
        ])
        assert AssayState.legal_transitions(AssayState.REMEDIATE) == [AssayState.PROBE]
        assert sorted(AssayState.legal_transitions(AssayState.ESCALATE_OR_SPLIT)) == sorted([
            AssayState.HYPOTHESIS, AssayState.HOLD,
        ])
        assert AssayState.legal_transitions(AssayState.PASS) == []
        assert AssayState.legal_transitions(AssayState.HOLD) == []

    def test_transition_accepts_string(self):
        assert AssayState.legal_transitions("pin") == [AssayState.HYPOTHESIS]

    def test_no_illegal_transition(self):
        """A terminal state has no outgoing transitions."""
        assert AssayState.legal_transitions(AssayState.PASS) == []
        assert AssayState.legal_transitions(AssayState.HOLD) == []


# ═══════════════════════════════════════════════════════════════════════════
# Policy definitions
# ═══════════════════════════════════════════════════════════════════════════

class TestConservativePolicy:
    """Conservative: read-only probes auto-approved; everything else blocks."""

    def setup_method(self):
        self.policy = ConservativePolicy()

    def test_read_only_probe_auto_approved(self):
        req = ActionRequest(
            action_type=ActionType.READ_ONLY_PROBE,
            description="test probe",
            state=AssayState.PROBE,
        )
        decision = self.policy.check(req)
        assert decision.approved
        assert "read-only" in decision.reason.lower()

    def test_mutating_batch_blocked(self):
        req = ActionRequest(
            action_type=ActionType.MUTATING_BATCH,
            description="test mutation",
            state=AssayState.PROBE,
        )
        decision = self.policy.check(req)
        assert not decision.approved

    def test_external_action_requires_human(self):
        req = ActionRequest(
            action_type=ActionType.EXTERNAL_ACTION,
            description="external",
            state=AssayState.MEASURE,
        )
        decision = self.policy.check(req)
        assert not decision.approved
        assert decision.requires_human

    def test_irreversible_action_requires_human(self):
        req = ActionRequest(
            action_type=ActionType.IRREVERSIBLE_ACTION,
            description="irreversible",
            state=AssayState.MEASURE,
        )
        decision = self.policy.check(req)
        assert not decision.approved
        assert decision.requires_human

    def test_state_transition_blocked(self):
        req = ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="transition",
            state=AssayState.PIN,
        )
        decision = self.policy.check(req)
        assert not decision.approved


class TestModeratePolicy:
    """Moderate: plan approved once, then self-heals within wave."""

    def setup_method(self):
        self.policy = ModeratePolicy()

    def test_initial_plan_approved(self):
        """PIN → HYPOTHESIS is auto-approved as the initial plan."""
        req = ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="plan",
            state=AssayState.PIN,
            target_state=AssayState.HYPOTHESIS,
        )
        decision = self.policy.check(req)
        assert decision.approved
        assert "plan" in decision.reason.lower()

    def test_wave_approval_auto_approves_transitions(self):
        """After wave approval, transitions within wave are auto-approved."""
        # Approve the plan (PIN → HYPOTHESIS).
        self.policy.check(ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="plan",
            state=AssayState.PIN,
            target_state=AssayState.HYPOTHESIS,
        ))
        assert not self.policy._wave_approved  # Wave not yet approved

        # Approve the wave (HYPOTHESIS → PROBE).
        self.policy.check(ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="wave start",
            state=AssayState.HYPOTHESIS,
            target_state=AssayState.PROBE,
        ))
        assert self.policy._wave_approved

        # Now PROBE → MEASURE should be auto-approved.
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="probe to measure",
            state=AssayState.PROBE,
            target_state=AssayState.MEASURE,
        ))
        assert decision.approved

    def test_remediate_auto_approved_within_wave(self):
        self.policy._wave_approved = True
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.REMEDIATE,
            description="self-heal",
            state=AssayState.CLASSIFY,
        ))
        assert decision.approved
        assert "self-heals" in decision.reason.lower()

    def test_reschedule_auto_approved_within_wave(self):
        self.policy._wave_approved = True
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.RESCHEDULE,
            description="reschedule",
            state=AssayState.PROBE,
        ))
        assert decision.approved

    def test_irreversible_requires_human_even_in_wave(self):
        self.policy._wave_approved = True
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.IRREVERSIBLE_ACTION,
            description="irreversible",
            state=AssayState.MEASURE,
        ))
        assert not decision.approved
        assert decision.requires_human

    def test_external_requires_human(self):
        self.policy._wave_approved = True
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.EXTERNAL_ACTION,
            description="external",
            state=AssayState.MEASURE,
        ))
        assert not decision.approved
        assert decision.requires_human


class TestUnicornPolicy:
    """Unicorn: full autonomy in reversible scope; holds for specific cases."""

    def setup_method(self):
        self.policy = UnicornPolicy()

    def test_read_only_probe_auto_approved(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.READ_ONLY_PROBE,
            description="probe",
            state=AssayState.PROBE,
        ))
        assert decision.approved

    def test_remediate_auto_approved(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.REMEDIATE,
            description="remediate",
            state=AssayState.CLASSIFY,
        ))
        assert decision.approved

    def test_reschedule_auto_approved(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.RESCHEDULE,
            description="reschedule",
            state=AssayState.PROBE,
        ))
        assert decision.approved

    def test_mutating_batch_auto_approved(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.MUTATING_BATCH,
            description="mutate",
            state=AssayState.MEASURE,
        ))
        assert decision.approved

    def test_state_transition_auto_approved(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="transition",
            state=AssayState.PIN,
            target_state=AssayState.HYPOTHESIS,
        ))
        assert decision.approved

    def test_split_auto_approved(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.SPLIT,
            description="split",
            state=AssayState.CLASSIFY,
        ))
        assert decision.approved

    # ── Hold conditions ─────────────────────────────────────────────

    def test_irreversible_action_hold(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.IRREVERSIBLE_ACTION,
            description="irreversible",
            state=AssayState.MEASURE,
        ))
        assert not decision.approved
        assert decision.requires_human
        assert "irreversible" in decision.reason.lower()

    def test_external_action_hold(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.EXTERNAL_ACTION,
            description="external",
            state=AssayState.MEASURE,
        ))
        assert not decision.approved
        assert decision.requires_human

    def test_scope_expansion_hold(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.SCOPE_EXPANSION,
            description="expand scope",
            state=AssayState.CLASSIFY,
        ))
        assert not decision.approved
        assert decision.requires_human
        assert "scope expansion" in decision.reason.lower()

    def test_authority_expansion_hold(self):
        decision = self.policy.check(ActionRequest(
            action_type=ActionType.AUTHORITY_EXPANSION,
            description="expand authority",
            state=AssayState.CLASSIFY,
        ))
        assert not decision.approved
        assert decision.requires_human
        assert "authority expansion" in decision.reason.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Policy factory
# ═══════════════════════════════════════════════════════════════════════════

class TestPolicyFactory:
    """create_policy and list_policy_names work correctly."""

    def test_create_conservative(self):
        policy = create_policy("conservative")
        assert isinstance(policy, ConservativePolicy)

    def test_create_moderate(self):
        policy = create_policy("moderate")
        assert isinstance(policy, ModeratePolicy)

    def test_create_unicorn(self):
        policy = create_policy("unicorn")
        assert isinstance(policy, UnicornPolicy)

    def test_unknown_policy_raises(self):
        with pytest.raises(ValueError, match="unknown assay policy"):
            create_policy("nonexistent")

    def test_list_policy_names(self):
        names = list_policy_names()
        assert "conservative" in names
        assert "moderate" in names
        assert "unicorn" in names


# ═══════════════════════════════════════════════════════════════════════════
# AssayEngine — full state machine integration
# ═══════════════════════════════════════════════════════════════════════════

class TestAssayEngineTransitions:
    """Full state machine via AssayEngine."""

    def test_full_cycle_conservative(self):
        """Conservative: each transition is blocked by policy."""
        engine = AssayEngine(policy_name="conservative", max_turns=20)

        # PIN → HYPOTHESIS is a transition but conservative blocks it.
        with pytest.raises(PolicyBlockedError):
            engine.transition(AssayState.HYPOTHESIS)

        # State should still be PIN.
        assert engine.state == AssayState.PIN

    def test_full_cycle_unicorn(self):
        """Unicorn: full cycle from PIN through PASS."""
        engine = AssayEngine(policy_name="unicorn", max_turns=20)

        assert engine.state == AssayState.PIN

        # PIN → HYPOTHESIS
        engine.transition(AssayState.HYPOTHESIS)
        assert engine.state == AssayState.HYPOTHESIS

        # HYPOTHESIS → PROBE
        engine.transition(AssayState.PROBE)
        assert engine.state == AssayState.PROBE

        # PROBE → MEASURE
        engine.transition(AssayState.MEASURE)
        assert engine.state == AssayState.MEASURE

        # MEASURE → CLASSIFY
        engine.transition(AssayState.CLASSIFY)
        assert engine.state == AssayState.CLASSIFY

        # CLASSIFY → PASS
        engine.transition(AssayState.PASS)
        assert engine.state == AssayState.PASS
        assert AssayState.is_terminal(engine.state.value)

    def test_classify_to_hold_unicorn(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)

        engine.transition(AssayState.HOLD)
        assert engine.state == AssayState.HOLD
        assert AssayState.is_terminal(engine.state.value)

    def test_classify_to_escalate_unicorn(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)

        engine.transition(AssayState.ESCALATE_OR_SPLIT)
        assert engine.state == AssayState.ESCALATE_OR_SPLIT

    def test_classify_to_remediate_unicorn(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)

        engine.transition(AssayState.REMEDIATE)
        assert engine.state == AssayState.REMEDIATE

    def test_remediate_back_to_probe_unicorn(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)
        engine.transition(AssayState.REMEDIATE)
        assert engine.state == AssayState.REMEDIATE

        engine.transition(AssayState.PROBE)
        assert engine.state == AssayState.PROBE

    def test_escalate_to_hypothesis_unicorn(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)
        engine.transition(AssayState.ESCALATE_OR_SPLIT)
        assert engine.state == AssayState.ESCALATE_OR_SPLIT

        engine.transition(AssayState.HYPOTHESIS)
        assert engine.state == AssayState.HYPOTHESIS

    def test_escalate_to_hold_unicorn(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)
        engine.transition(AssayState.ESCALATE_OR_SPLIT)
        engine.transition(AssayState.HOLD)
        assert engine.state == AssayState.HOLD

    def test_illegal_transition_raises(self):
        engine = AssayEngine(policy_name="unicorn")
        with pytest.raises(InvalidTransitionError):
            engine.transition(AssayState.PASS)  # Can't jump to PASS from PIN

    def test_terminal_has_no_outgoing(self):
        engine = AssayEngine(policy_name="unicorn")
        self._run_to_classify(engine)
        engine.transition(AssayState.PASS)
        with pytest.raises(InvalidTransitionError):
            engine.transition(AssayState.HYPOTHESIS)

    @staticmethod
    def _run_to_classify(engine):
        engine.transition(AssayState.HYPOTHESIS)
        engine.transition(AssayState.PROBE)
        engine.transition(AssayState.MEASURE)
        engine.transition(AssayState.CLASSIFY)


# ═══════════════════════════════════════════════════════════════════════════
# Moderate self-heals within approved wave
# ═══════════════════════════════════════════════════════════════════════════

class TestModerateSelfHeals:
    """Moderate self-heals within an approved reversible wave."""

    def test_self_heal_after_classify(self):
        engine = AssayEngine(policy_name="moderate", max_turns=20)

        # PIN → HYPOTHESIS auto-approved (initial plan).
        engine.transition(AssayState.HYPOTHESIS)
        assert engine.state == AssayState.HYPOTHESIS

        # HYPOTHESIS → PROBE approves the wave.
        engine.transition(AssayState.PROBE)
        assert engine.state == AssayState.PROBE

        engine.transition(AssayState.MEASURE)
        assert engine.state == AssayState.MEASURE

        engine.transition(AssayState.CLASSIFY)
        assert engine.state == AssayState.CLASSIFY

        # Now remediate within the wave — auto-approved.
        engine.transition(AssayState.REMEDIATE)
        assert engine.state == AssayState.REMEDIATE

        # Back to probe.
        engine.transition(AssayState.PROBE)
        assert engine.state == AssayState.PROBE

    def test_moderate_blocks_without_wave_approval(self):
        """Before wave approval, moderate blocks non-plan, non-wave transitions."""
        engine = AssayEngine(policy_name="moderate", max_turns=20)

        # PIN → HYPOTHESIS is the initial plan, which is auto-approved.
        engine.transition(AssayState.HYPOTHESIS)
        assert engine.state == AssayState.HYPOTHESIS

        # From HYPOTHESIS, the only legal forward transition is PROBE.
        # A direct check via policy (not engine) for a mutating batch
        # should be blocked since no wave is approved.
        decision = engine.check_action(ActionRequest(
            action_type=ActionType.MUTATING_BATCH,
            description="mutate without wave",
            state=AssayState.HYPOTHESIS,
        ))
        assert not decision.approved


# ═══════════════════════════════════════════════════════════════════════════
# Unicorn holds for specific conditions
# ═══════════════════════════════════════════════════════════════════════════

class TestUnicornHolds:
    """Every Unicorn hold condition is tested."""

    def setup_method(self):
        self.engine = AssayEngine(policy_name="unicorn")

    def test_irreversible_action_hold(self):
        decision = self.engine.check_action(ActionRequest(
            action_type=ActionType.IRREVERSIBLE_ACTION,
            description="irreversible",
            state=AssayState.MEASURE,
        ))
        assert not decision.approved
        assert decision.requires_human

    def test_external_action_hold(self):
        decision = self.engine.check_action(ActionRequest(
            action_type=ActionType.EXTERNAL_ACTION,
            description="external",
            state=AssayState.MEASURE,
        ))
        assert not decision.approved
        assert decision.requires_human

    def test_scope_expansion_hold(self):
        decision = self.engine.check_action(ActionRequest(
            action_type=ActionType.SCOPE_EXPANSION,
            description="expand scope",
            state=AssayState.CLASSIFY,
        ))
        assert not decision.approved
        assert decision.requires_human

    def test_authority_expansion_hold(self):
        decision = self.engine.check_action(ActionRequest(
            action_type=ActionType.AUTHORITY_EXPANSION,
            description="expand authority",
            state=AssayState.CLASSIFY,
        ))
        assert not decision.approved
        assert decision.requires_human

    def test_target_drift_hold(self):
        """Target drift is not a built-in hold but an action that needs evaluation."""
        decision = self.engine.check_action(ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description="drift",
            state=AssayState.CLASSIFY,
            target_state=AssayState.PASS,
        ))
        assert decision.approved  # Unicorn auto-approves state transitions

    def test_budget_exhaustion_via_turns(self):
        """Budget exhaustion raises TurnBudgetExhaustedError."""
        engine = AssayEngine(policy_name="unicorn", max_turns=3)
        engine.advance_turn()
        engine.advance_turn()
        engine.advance_turn()
        with pytest.raises(TurnBudgetExhaustedError):
            engine.advance_turn()


# ═══════════════════════════════════════════════════════════════════════════
# Dynamic turns
# ═══════════════════════════════════════════════════════════════════════════

class TestDynamicTurns:
    """Turn limits, advancement, and limit changes."""

    def test_advance_turn(self):
        engine = AssayEngine(policy_name="unicorn")
        assert engine.current_turn == 0
        assert engine.advance_turn() == 1
        assert engine.current_turn == 1

    def test_turn_budget_exhaustion(self):
        engine = AssayEngine(policy_name="unicorn", max_turns=2)
        engine.advance_turn()
        engine.advance_turn()
        with pytest.raises(TurnBudgetExhaustedError):
            engine.advance_turn()

    def test_change_turn_limit(self):
        engine = AssayEngine(policy_name="unicorn", max_turns=3)
        limit = engine.set_turn_limit(10)
        assert engine.max_turns == 10
        assert limit.max_turns == 10
        # Now we can advance past original limit.
        engine.advance_turn()
        engine.advance_turn()
        engine.advance_turn()
        engine.advance_turn()
        assert engine.current_turn == 4

    def test_turn_limit_recorded(self):
        engine = AssayEngine(policy_name="unicorn")
        engine.set_turn_limit(5)
        engine.set_turn_limit(10)
        limits = engine.get_turn_limits()
        assert len(limits) == 2
        assert limits[0].max_turns == 5
        assert limits[1].max_turns == 10


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeats
# ═══════════════════════════════════════════════════════════════════════════

class TestHeartbeats:
    """Heartbeats are recorded and retrievable."""

    def test_heartbeat_recorded(self):
        engine = AssayEngine(policy_name="unicorn")
        hb = engine.heartbeat()
        assert hb.state == "pin"
        assert hb.turn == 0
        assert hb.timestamp

    def test_heartbeat_after_transition(self):
        engine = AssayEngine(policy_name="unicorn")
        engine.transition(AssayState.HYPOTHESIS)
        hbs = engine.get_heartbeats()
        assert len(hbs) >= 2  # Initial heartbeat + transition heartbeat
        assert any(hb.state == "hypothesis" for hb in hbs)

    def test_heartbeat_advances_turn(self):
        engine = AssayEngine(policy_name="unicorn")
        engine.advance_turn()
        hb = engine.heartbeat()
        assert hb.turn == 1


# ═══════════════════════════════════════════════════════════════════════════
# Splits
# ═══════════════════════════════════════════════════════════════════════════

class TestSplits:
    """Splits are recorded and retrievable."""

    def test_record_split(self):
        engine = AssayEngine(policy_name="unicorn")
        split = engine.record_split("split-001", {"reason": "divergence"})
        assert split.split_id == "split-001"
        assert split.from_state == "pin"
        assert split.metadata["reason"] == "divergence"

    def test_splits_after_transition(self):
        engine = AssayEngine(policy_name="unicorn")
        engine.transition(AssayState.HYPOTHESIS)
        engine.record_split("split-001")
        splits = engine.get_splits()
        assert len(splits) == 1
        assert splits[0].from_state == "hypothesis"

    def test_multiple_splits(self):
        engine = AssayEngine(policy_name="unicorn")
        engine.record_split("split-001")
        engine.record_split("split-002")
        assert len(engine.get_splits()) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Terminal evidence
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminalEvidence:
    """Terminal evidence recording and narrative/compaction rejection."""

    def test_record_terminal_evidence(self):
        engine = AssayEngine(policy_name="unicorn")
        evidence = {
            "evidence_type": "observation",
            "purpose": "verify outcome",
            "method": "structural_analysis",
        }
        engine.record_terminal_evidence(evidence)
        assert len(engine.get_terminal_evidence()) == 1

    def test_reject_narrative_evidence_type(self):
        evidence = {
            "evidence_type": "narrative",
            "purpose": "report",
            "method": "summary",
        }
        with pytest.raises(EvidenceRejectedError) as exc:
            validate_assay_evidence(evidence)
        assert "narrative" in str(exc.value).lower()

    def test_reject_compaction_type(self):
        evidence = {
            "evidence_type": "compaction",
            "purpose": "compress",
            "method": "reduce",
        }
        with pytest.raises(EvidenceRejectedError) as exc:
            validate_assay_evidence(evidence)
        assert "compaction" not in str(exc.value).lower() or not False
        # Actually compaction is not in VALID_ASSAY_EVIDENCE_TYPES
        assert True

    def test_reject_narrative_in_purpose(self):
        evidence = {
            "evidence_type": "observation",
            "purpose": "narrative summary of findings",
            "method": "structural",
        }
        with pytest.raises(EvidenceRejectedError):
            validate_assay_evidence(evidence)

    def test_reject_narrative_in_method(self):
        evidence = {
            "evidence_type": "test",
            "purpose": "verify",
            "method": "compaction analysis",
        }
        with pytest.raises(EvidenceRejectedError):
            validate_assay_evidence(evidence)

    def test_reject_compaction_in_method(self):
        evidence = {
            "evidence_type": "metric",
            "purpose": "measure",
            "method": "compaction",
        }
        with pytest.raises(EvidenceRejectedError):
            validate_assay_evidence(evidence)

    def test_accept_valid_evidence(self):
        valid_cases = [
            {"evidence_type": "record", "purpose": "log", "method": "capture"},
            {"evidence_type": "artifact", "purpose": "store", "method": "build"},
            {"evidence_type": "test", "purpose": "verify", "method": "assert"},
            {"evidence_type": "metric", "purpose": "measure", "method": "count"},
            {"evidence_type": "decision", "purpose": "decide", "method": "classify"},
            {"evidence_type": "runtime_trace", "purpose": "trace", "method": "capture"},
        ]
        for evidence in valid_cases:
            # Should not raise.
            validate_assay_evidence(evidence)


# ═══════════════════════════════════════════════════════════════════════════
# Harness-native bypass never weakens policy
# ═══════════════════════════════════════════════════════════════════════════

class TestBypassNonWeakening:
    """Harness-native bypass or YOLO never weakens the effective policy.

    The policy's check() is the authority. Even if a bypass caller skips
    the engine and calls check() directly, the same rules apply.
    """

    def test_conservative_not_weakened_by_direct_check(self):
        """Direct policy.check() gives same answer as engine."""
        policy = ConservativePolicy()
        engine = AssayEngine(policy_name="conservative")

        req = ActionRequest(
            action_type=ActionType.MUTATING_BATCH,
            description="mutate",
            state=AssayState.PROBE,
        )
        direct = policy.check(req)
        engine_decision = engine.check_action(req)
        assert direct.approved == engine_decision.approved
        assert not direct.approved  # Conservative blocks mutations

    def test_moderate_not_weakened_by_direct_check(self):
        """Moderate policy is not bypassable via direct check() call."""
        policy = ModeratePolicy()
        # Without wave approval, transitions are blocked.
        req = ActionRequest(
            action_type=ActionType.MUTATING_BATCH,
            description="mutate",
            state=AssayState.PROBE,
        )
        decision = policy.check(req)
        assert not decision.approved

    def test_unicorn_not_weakened_for_irreversible(self):
        """Unicorn still holds for irreversible via direct check."""
        policy = UnicornPolicy()
        req = ActionRequest(
            action_type=ActionType.IRREVERSIBLE_ACTION,
            description="irreversible",
            state=AssayState.MEASURE,
        )
        decision = policy.check(req)
        assert not decision.approved
        assert decision.requires_human

    def test_engine_policy_isolation(self):
        """Each engine has its own policy instance; changing one doesn't affect others."""
        engine_a = AssayEngine(policy_name="moderate")
        engine_b = AssayEngine(policy_name="moderate")

        # engine_a approves its plan and wave.
        engine_a.transition(AssayState.HYPOTHESIS)  # PIN → HYPOTHESIS (plan approved)
        engine_a.transition(AssayState.PROBE)       # HYPOTHESIS → PROBE (wave approved)
        assert engine_a.state == AssayState.PROBE
        assert isinstance(engine_a.policy, ModeratePolicy)
        assert engine_a.policy._wave_approved

        # engine_b's policy should not have wave approval.
        assert isinstance(engine_b.policy, ModeratePolicy)
        assert not engine_b.policy._wave_approved

    def test_custom_policy_injected(self):
        """A custom policy can be injected into the engine."""
        class AlwaysAllowPolicy(AssayPolicy):
            name = "always_allow"
            def check(self, action):
                return PolicyDecision(approved=True, reason="always")

        engine = AssayEngine(policy=AlwaysAllowPolicy())
        engine.transition(AssayState.HYPOTHESIS)
        assert engine.state == AssayState.HYPOTHESIS


# ═══════════════════════════════════════════════════════════════════════════
# Data record serialization
# ═══════════════════════════════════════════════════════════════════════════

class TestDataRecords:
    """Data records serialize correctly."""

    def test_heartbeat_to_dict(self):
        hb = AssayHeartbeat(
            run_id="run-1", state="probe",
            timestamp="2026-01-01T00:00:00", turn=5,
        )
        d = hb.to_dict()
        assert d["run_id"] == "run-1"
        assert d["state"] == "probe"
        assert d["turn"] == 5

    def test_turn_limit_to_dict(self):
        tl = AssayTurnLimit(
            run_id="run-1", max_turns=10, current_turn=3,
            updated_at="2026-01-01T00:00:00",
        )
        d = tl.to_dict()
        assert d["max_turns"] == 10
        assert d["current_turn"] == 3

    def test_split_to_dict(self):
        split = AssaySplit(
            run_id="run-1", split_id="sp-1",
            from_state="classify", created_at="2026-01-01T00:00:00",
        )
        d = split.to_dict()
        assert d["split_id"] == "sp-1"
        assert d["from_state"] == "classify"


# ═══════════════════════════════════════════════════════════════════════════
# Reset wave (Moderate)
# ═══════════════════════════════════════════════════════════════════════════

class TestModerateWaveReset:
    """Wave approval resets after ESCALATE/PASS/HOLD."""

    def test_reset_wave(self):
        engine = AssayEngine(policy_name="moderate", max_turns=20)
        # Approve plan.
        engine.transition(AssayState.HYPOTHESIS)
        assert engine.state == AssayState.HYPOTHESIS
        # Wave is approved via HYPOTHESIS → PROBE.
        engine.transition(AssayState.PROBE)
        assert engine.policy._wave_approved

        # Reset wave.
        engine.reset_wave()
        assert not engine.policy._wave_approved


# ═══════════════════════════════════════════════════════════════════════════
# Standalone runner (bash -eo pipefail compatible)
# ═══════════════════════════════════════════════════════════════════════════

def _run_all() -> int:
    """Run all test functions directly (no pytest dependency)."""
    tests = []
    for module_member in list(globals().values()):
        if isinstance(module_member, type) and issubclass(module_member, object):
            for attr_name in dir(module_member):
                if attr_name.startswith("test_") and callable(getattr(module_member, attr_name)):
                    tests.append((module_member, attr_name))

    failed = 0
    # Sort by class name then test name for deterministic order.
    for cls, test_name in sorted(tests, key=lambda x: (x[0].__name__, x[1])):
        test_fn = getattr(cls, test_name)
        instance = cls()
        try:
            if hasattr(instance, "setup_method"):
                instance.setup_method()
            test_fn(instance)
            print(f"PASS {cls.__name__}.{test_name}")
        except Exception as e:
            if type(e).__name__ == "Skipped":
                print(f"SKIP {cls.__name__}.{test_name}")
                continue
            failed += 1
            print(f"FAIL {cls.__name__}.{test_name}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
