"""Unit tests for versioned methodology registry and compatibility matrix
(SW-159-METHOD-001).

Covers:
- Registry has REX, Ralph, Gauntlet, Assay with no harness/model identities
- Separate serialization of topology, methodology, policy, risk, authority, checkpoints
- Rejection of incompatible combinations with named reasons
- Deterministic legacy migration
- Same DAG with different methodologies; same methodology with different policies
"""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest

from skillweave.runtime.methodology import (
    Methodology,
    TopologyType,
    PolicyType,
    RiskLevel,
    AuthorityLevel,
    MethodologyDefinition,
    MethodologyRegistry,
    DEFAULT_REGISTRY,
    check_compatibility,
    assert_compatible,
    MethodologyError,
    migrate_legacy_method,
    serialize_methodology_state,
)


# ---------------------------------------------------------------------------
# Registry: four methodologies without harness/model identities
# ---------------------------------------------------------------------------

class TestRegistryHasFourMethodologies:
    """The registry contains exactly REX, Ralph, Gauntlet, Assay and nothing
    that references a harness or model identity."""

    def test_registry_contains_all_four(self):
        reg = MethodologyRegistry()
        names = {m.methodology for m in reg.list()}
        assert names == {
            Methodology.REX,
            Methodology.RALPH,
            Methodology.GAUNTLET,
            Methodology.ASSAY,
        }

    def test_default_registry_is_populated(self):
        assert len(DEFAULT_REGISTRY.list()) == 4

    def test_get_returns_definition(self):
        reg = MethodologyRegistry()
        d = reg.get(Methodology.REX)
        assert isinstance(d, MethodologyDefinition)
        assert d.methodology == Methodology.REX

    def test_get_unknown_raises(self):
        reg = MethodologyRegistry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")  # type: ignore

    def test_contains(self):
        reg = MethodologyRegistry()
        assert Methodology.REX in reg
        assert Methodology.RALPH in reg
        assert Methodology.GAUNTLET in reg
        assert Methodology.ASSAY in reg

    def test_no_harness_or_model_identity_in_definitions(self):
        """MethodologyDefinition carries no harness, model, or provider fields."""
        reg = MethodologyRegistry()
        for d in reg.list():
            d_dict = d.to_dict()
            # Only methodology-specific keys; no harness/model/provider
            assert set(d_dict.keys()) == {
                "methodology", "description", "supported_topologies",
                "supported_policies", "risk", "authority",
                "checkpoints", "version",
            }, (
                f"{d.methodology.value} carries unexpected keys: "
                f"{set(d_dict.keys())}"
            )
            # Verify no harness/model leaks
            for key in d_dict:
                assert "harness" not in key.lower(), (
                    f"harness reference found in key '{key}'"
                )
                assert "model" not in key.lower(), (
                    f"model reference found in key '{key}'"
                )
                assert "provider" not in key.lower(), (
                    f"provider reference found in key '{key}'"
                )

    def test_rex_defaults(self):
        d = DEFAULT_REGISTRY.get(Methodology.REX)
        assert d.description
        assert TopologyType.SEQUENTIAL in d.supported_topologies
        assert TopologyType.DAG in d.supported_topologies
        assert PolicyType.SUPERVISED in d.supported_policies
        assert PolicyType.AUTONOMOUS in d.supported_policies
        assert d.risk == RiskLevel.LOW
        assert d.authority == AuthorityLevel.REVIEWER
        assert not d.checkpoints

    def test_ralph_defaults(self):
        d = DEFAULT_REGISTRY.get(Methodology.RALPH)
        assert d.description
        assert TopologyType.DAG in d.supported_topologies
        assert TopologyType.WAVE in d.supported_topologies
        assert PolicyType.APPROVAL_REQUIRED in d.supported_policies
        assert PolicyType.COLLABORATIVE in d.supported_policies
        assert d.risk == RiskLevel.MEDIUM
        assert d.authority == AuthorityLevel.APPROVER
        assert d.checkpoints

    def test_gauntlet_defaults(self):
        d = DEFAULT_REGISTRY.get(Methodology.GAUNTLET)
        assert d.description
        assert TopologyType.PARALLEL_LANES in d.supported_topologies
        assert TopologyType.DAG in d.supported_topologies
        assert PolicyType.COLLABORATIVE in d.supported_policies
        assert PolicyType.HUMAN_LED in d.supported_policies
        assert d.risk == RiskLevel.HIGH
        assert d.authority == AuthorityLevel.HUMAN
        assert d.checkpoints

    def test_assay_defaults(self):
        d = DEFAULT_REGISTRY.get(Methodology.ASSAY)
        assert d.description
        # Assay supports all topologies
        for t in TopologyType:
            assert t in d.supported_topologies, (
                f"Assay should support {t.value}"
            )
        assert PolicyType.SUPERVISED in d.supported_policies
        assert d.risk == RiskLevel.LOW
        assert d.authority == AuthorityLevel.REVIEWER
        assert not d.checkpoints


# ---------------------------------------------------------------------------
# Separate serialization: each concern is an independent top-level key
# ---------------------------------------------------------------------------

class TestSeparateSerialization:
    """Topology, methodology, policy, risk, authority, and checkpoints are
    serialized as separate top-level keys — no key nests another concern."""

    def test_all_six_keys_are_top_level(self):
        result = serialize_methodology_state(
            methodology=Methodology.REX,
            topology=TopologyType.DAG,
            policy=PolicyType.SUPERVISED,
        )
        assert set(result.keys()) == {
            "topology", "methodology", "policy",
            "risk", "authority", "checkpoints",
        }

    def test_no_nested_keys(self):
        """No value is itself a dict that merges concerns."""
        result = serialize_methodology_state(
            methodology=Methodology.RALPH,
            topology=TopologyType.DAG,
            policy=PolicyType.APPROVAL_REQUIRED,
        )
        for key, value in result.items():
            if key == "checkpoints":
                assert isinstance(value, list)
            elif key == "methodology":
                assert isinstance(value, str)
            elif key == "topology":
                assert isinstance(value, str)
            elif key == "policy":
                assert isinstance(value, str)
            elif key == "risk":
                assert isinstance(value, str)
            elif key == "authority":
                assert isinstance(value, str)

    def test_risk_and_authority_derive_from_methodology_when_not_given(self):
        result = serialize_methodology_state(
            methodology=Methodology.REX,
            topology=TopologyType.DAG,
            policy=PolicyType.SUPERVISED,
        )
        assert result["risk"] == "low"
        assert result["authority"] == "reviewer"

    def test_explicit_risk_and_authority(self):
        result = serialize_methodology_state(
            methodology=Methodology.REX,
            topology=TopologyType.DAG,
            policy=PolicyType.SUPERVISED,
            risk=RiskLevel.HIGH,
            authority=AuthorityLevel.HUMAN,
        )
        assert result["risk"] == "high"
        assert result["authority"] == "human"

    def test_checkpoints_default_to_empty_list(self):
        result = serialize_methodology_state(
            methodology=Methodology.REX,
            topology=TopologyType.DAG,
            policy=PolicyType.SUPERVISED,
        )
        assert result["checkpoints"] == []

    def test_checkpoints_with_data(self):
        result = serialize_methodology_state(
            methodology=Methodology.REX,
            topology=TopologyType.DAG,
            policy=PolicyType.SUPERVISED,
            checkpoints=[{"id": "cp-1", "state": "done"}],
        )
        assert result["checkpoints"] == [{"id": "cp-1", "state": "done"}]


# ---------------------------------------------------------------------------
# Reject incompatible combinations with named reasons
# ---------------------------------------------------------------------------

class TestRejectIncompatibleCombinations:
    """Incompatible methodology/topology/policy combinations are rejected with
    a named reason instead of silent coercion."""

    def test_rex_sequential_human_led_rejected(self):
        reason = check_compatibility(
            Methodology.REX,
            TopologyType.SEQUENTIAL,
            PolicyType.HUMAN_LED,
        )
        assert reason is not None
        assert "REX" in reason
        assert "human-led" in reason

    def test_ralph_sequential_approval_rejected(self):
        reason = check_compatibility(
            Methodology.RALPH,
            TopologyType.SEQUENTIAL,
            PolicyType.APPROVAL_REQUIRED,
        )
        assert reason is not None
        assert "Ralph" in reason or "Ralph Loop" in reason
        assert "sequential" in reason

    def test_gauntlet_parallel_lanes_autonomous_rejected(self):
        reason = check_compatibility(
            Methodology.GAUNTLET,
            TopologyType.PARALLEL_LANES,
            PolicyType.AUTONOMOUS,
        )
        assert reason is not None
        assert "Gauntlet" in reason
        assert "autonomously" in reason

    def test_assay_sequential_approval_required_rejected(self):
        reason = check_compatibility(
            Methodology.ASSAY,
            TopologyType.SEQUENTIAL,
            PolicyType.APPROVAL_REQUIRED,
        )
        assert reason is not None
        assert "Assay" in reason
        assert "experimental" in reason

    def test_unsupported_topology_rejected(self):
        """REX does not support PARALLEL_LANES topology."""
        reason = check_compatibility(
            Methodology.REX,
            TopologyType.PARALLEL_LANES,
            PolicyType.SUPERVISED,
        )
        assert reason is not None
        assert "topology" in reason.lower()
        assert "rex" in reason.lower()
        assert "parallel_lanes" in reason.lower()

    def test_unsupported_policy_rejected(self):
        """Assay does not support HUMAN_LED policy."""
        reason = check_compatibility(
            Methodology.ASSAY,
            TopologyType.DAG,
            PolicyType.HUMAN_LED,
        )
        assert reason is not None
        assert "policy" in reason.lower()
        assert "assay" in reason.lower()
        assert "human_led" in reason.lower()

    def test_assert_compatible_raises_with_reason(self):
        with pytest.raises(MethodologyError) as exc_info:
            assert_compatible(
                Methodology.REX,
                TopologyType.SEQUENTIAL,
                PolicyType.HUMAN_LED,
            )
        assert exc_info.value.reason
        assert isinstance(exc_info.value.reason, str)

    def test_assert_compatible_passes(self):
        """A compatible combination does not raise."""
        assert_compatible(
            Methodology.REX,
            TopologyType.DAG,
            PolicyType.SUPERVISED,
        )


# ---------------------------------------------------------------------------
# Accepted combinations (compatibility matrix)
# ---------------------------------------------------------------------------

class TestAcceptedCombinations:
    """These combinations are explicitly supported."""

    def test_rex_dag_supervised(self):
        assert check_compatibility(
            Methodology.REX,
            TopologyType.DAG,
            PolicyType.SUPERVISED,
        ) is None

    def test_rex_dag_autonomous(self):
        assert check_compatibility(
            Methodology.REX,
            TopologyType.DAG,
            PolicyType.AUTONOMOUS,
        ) is None

    def test_rex_sequential_supervised(self):
        assert check_compatibility(
            Methodology.REX,
            TopologyType.SEQUENTIAL,
            PolicyType.SUPERVISED,
        ) is None

    def test_ralph_dag_approval_required(self):
        assert check_compatibility(
            Methodology.RALPH,
            TopologyType.DAG,
            PolicyType.APPROVAL_REQUIRED,
        ) is None

    def test_ralph_dag_collaborative(self):
        assert check_compatibility(
            Methodology.RALPH,
            TopologyType.DAG,
            PolicyType.COLLABORATIVE,
        ) is None

    def test_ralph_wave_approval_required(self):
        assert check_compatibility(
            Methodology.RALPH,
            TopologyType.WAVE,
            PolicyType.APPROVAL_REQUIRED,
        ) is None

    def test_gauntlet_parallel_lanes_collaborative(self):
        assert check_compatibility(
            Methodology.GAUNTLET,
            TopologyType.PARALLEL_LANES,
            PolicyType.COLLABORATIVE,
        ) is None

    def test_gauntlet_dag_human_led(self):
        assert check_compatibility(
            Methodology.GAUNTLET,
            TopologyType.DAG,
            PolicyType.HUMAN_LED,
        ) is None

    def test_assay_dag_supervised(self):
        assert check_compatibility(
            Methodology.ASSAY,
            TopologyType.DAG,
            PolicyType.SUPERVISED,
        ) is None

    def test_assay_sequential_supervised(self):
        assert check_compatibility(
            Methodology.ASSAY,
            TopologyType.SEQUENTIAL,
            PolicyType.SUPERVISED,
        ) is None

    def test_assay_wave_supervised(self):
        assert check_compatibility(
            Methodology.ASSAY,
            TopologyType.WAVE,
            PolicyType.SUPERVISED,
        ) is None


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------

class TestLegacyMigration:
    """Legacy execution_model strings map deterministically to methodologies."""

    def test_cold_maps_to_rex(self):
        assert migrate_legacy_method("cold") == Methodology.REX

    def test_warm_maps_to_ralph(self):
        assert migrate_legacy_method("warm") == Methodology.RALPH

    def test_resume_maps_to_ralph(self):
        assert migrate_legacy_method("resume") == Methodology.RALPH

    def test_unknown_raises_with_actionable_message(self):
        with pytest.raises(MethodologyError) as exc_info:
            migrate_legacy_method("hot")
        assert "hot" in str(exc_info.value)
        assert "cold" in str(exc_info.value) or "cold" in exc_info.value.reason
        assert exc_info.value.reason == "unknown_legacy_value:hot"

    def test_empty_string_raises(self):
        with pytest.raises(MethodologyError):
            migrate_legacy_method("")

    def test_none_raises(self):
        with pytest.raises(MethodologyError):
            migrate_legacy_method(None)  # type: ignore


# ---------------------------------------------------------------------------
# Same DAG can use different methodologies
# ---------------------------------------------------------------------------

class TestSameDagDifferentMethodologies:
    """The same topology (DAG) can be used with different methodologies
    where supported."""

    def test_dag_with_rex(self):
        assert check_compatibility(
            Methodology.REX,
            TopologyType.DAG,
            PolicyType.SUPERVISED,
        ) is None

    def test_dag_with_ralph(self):
        assert check_compatibility(
            Methodology.RALPH,
            TopologyType.DAG,
            PolicyType.APPROVAL_REQUIRED,
        ) is None

    def test_dag_with_gauntlet(self):
        assert check_compatibility(
            Methodology.GAUNTLET,
            TopologyType.DAG,
            PolicyType.HUMAN_LED,
        ) is None

    def test_dag_with_assay(self):
        assert check_compatibility(
            Methodology.ASSAY,
            TopologyType.DAG,
            PolicyType.SUPERVISED,
        ) is None

    def test_all_four_use_dag(self):
        """All four methodologies support DAG topology."""
        combos = [
            (Methodology.REX, PolicyType.SUPERVISED),
            (Methodology.RALPH, PolicyType.APPROVAL_REQUIRED),
            (Methodology.GAUNTLET, PolicyType.HUMAN_LED),
            (Methodology.ASSAY, PolicyType.SUPERVISED),
        ]
        for methodology, policy in combos:
            reason = check_compatibility(methodology, TopologyType.DAG, policy)
            assert reason is None, (
                f"{methodology.value} + DAG + {policy.value} should be "
                f"compatible but got: {reason}"
            )


# ---------------------------------------------------------------------------
# One methodology can use different policies where supported
# ---------------------------------------------------------------------------

class TestOneMethodologyDifferentPolicies:
    """A single methodology can use different policies where supported."""

    def test_rex_supervised_and_autonomous(self):
        """REX supports both SUPERVISED and AUTONOMOUS."""
        assert check_compatibility(
            Methodology.REX, TopologyType.DAG, PolicyType.SUPERVISED,
        ) is None
        assert check_compatibility(
            Methodology.REX, TopologyType.DAG, PolicyType.AUTONOMOUS,
        ) is None

    def test_ralph_approval_required_and_collaborative(self):
        """Ralph supports both APPROVAL_REQUIRED and COLLABORATIVE."""
        assert check_compatibility(
            Methodology.RALPH, TopologyType.DAG, PolicyType.APPROVAL_REQUIRED,
        ) is None
        assert check_compatibility(
            Methodology.RALPH, TopologyType.DAG, PolicyType.COLLABORATIVE,
        ) is None

    def test_gauntlet_collaborative_and_human_led(self):
        """Gauntlet supports both COLLABORATIVE and HUMAN_LED."""
        assert check_compatibility(
            Methodology.GAUNTLET,
            TopologyType.PARALLEL_LANES,
            PolicyType.COLLABORATIVE,
        ) is None
        assert check_compatibility(
            Methodology.GAUNTLET,
            TopologyType.DAG,
            PolicyType.HUMAN_LED,
        ) is None

    def test_assay_only_supervised(self):
        """Assay only supports SUPERVISED policy."""
        assert check_compatibility(
            Methodology.ASSAY, TopologyType.DAG, PolicyType.SUPERVISED,
        ) is None
        # Every other policy should be rejected
        for policy in PolicyType:
            if policy == PolicyType.SUPERVISED:
                continue
            reason = check_compatibility(
                Methodology.ASSAY, TopologyType.DAG, policy,
            )
            assert reason is not None, (
                f"Assay should reject policy {policy.value}"
            )


# ---------------------------------------------------------------------------
# Serialization round-trip (to_dict)
# ---------------------------------------------------------------------------

class TestMethodologyDefinitionSerialization:

    def test_to_dict_returns_expected_keys(self):
        d = DEFAULT_REGISTRY.get(Methodology.RALPH)
        result = d.to_dict()
        assert result["methodology"] == "ralph"
        assert "dag" in result["supported_topologies"]  # serialized values, not enum names
        assert result["risk"] == "medium"
        assert result["authority"] == "approver"
        assert result["checkpoints"] is True


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            if type(e).__name__ == "Skipped":
                print(f"SKIP {t.__name__}")
                continue
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
