"""CMS Operate Profile — generic contract tests (SW-159-CMS-PACK-001).

Validates that the cms-operate.v1.yaml profile is:
  - A valid WorkProfile through the generic contract schema
  - Provider-free (no harness, router, provider, model, tool, or pin)
  - A valid RoutingProfile with generic roles
  - Backed by valid evidence, deliverable and review contracts
  - Consistent with the "operate" category constraints (reactive/continuous
    topology, supervised or tighter human coupling)

These tests use the installed SDK (skillweave-sdk) as the contract authority.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import skillweave_sdk.validator as _sdk_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = REPO_ROOT / "profiles"
CONTRACTS_DIR = REPO_ROOT / "schemas" / "lifecycle-contracts"
LOCK = json.loads((CONTRACTS_DIR / "contract-lock.json").read_text(encoding="utf-8"))

PROFILE_PATH = PROFILES_DIR / "cms-operate.v1.yaml"

# Provider names that must never appear in a generic profile.
FORBIDDEN_PROVIDER_NAMES = (
    "faigate",
    "openrouter",
    "omniroute",
    "kilo",
    "9router",
    "anthropic",
    "openai",
    "google",
    "gemini",
    "brave",
    "tavily",
    "serpapi",
    "bing",
    "duckduckgo",
)

# Fixed runtime identities that must not appear in a generic profile.
FORBIDDEN_RUNTIME_IDS = (
    "opencode",
    "claude",
    "codex",
    "elementeer",
    "txthumanizer",
    "txtHumanizer",
)


# ── SDK-backed fixtures (contract authority) ────────────────────────────────


def _sdk_schema(contract: str) -> dict:
    """Return the SDK schema dict for a lifecycle contract name."""
    reg = _sdk_validator.load_registry()
    for sid, schema in reg.items():
        if f"lifecycle/{contract}" in sid:
            return schema
    raise ValueError(f"SDK schema not found for lifecycle contract {contract!r}")


@pytest.fixture(scope="module")
def profile() -> dict:
    """Load the CMS operate profile YAML."""
    assert PROFILE_PATH.is_file(), f"Profile not found at {PROFILE_PATH}"
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def contracts_registry() -> Registry:
    """All lifecycle schemas registered by ``$id`` from the installed SDK."""
    reg = _sdk_validator.load_registry()
    resources = []
    for sid, schema in reg.items():
        if "lifecycle" in sid:
            resources.append(
                (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
            )
    return Registry().with_resources(resources)


def _validator(contract_name: str, registry: Registry) -> Draft202012Validator:
    """Return a validator for the named contract using the SDK schema."""
    doc = _sdk_schema(contract_name)
    return Draft202012Validator(doc, registry=registry)


# ── WorkProfile contract validation ─────────────────────────────────────────


class TestWorkProfileContract:
    """The profile is a valid WorkProfile through the generic schema."""

    def test_profile_is_valid_yaml(self, profile):
        assert isinstance(profile, dict)
        assert "contractVersion" in profile
        assert "id" in profile
        assert "category" in profile

    def test_contract_version_is_valid(self, profile):
        assert profile["contractVersion"] == "1.0.0"

    def test_profile_id_follows_naming_convention(self, profile):
        import re
        assert re.match(r"^[a-z0-9][a-z0-9._-]*$", profile["id"])

    def test_category_is_operate(self, profile):
        assert profile["category"] == "operate"
        assert profile["category"] in LOCK["vocabulary"]["categories"]

    def test_kernel_stages_are_valid(self, profile):
        valid = set(LOCK["vocabulary"]["kernelStages"])
        for ks in profile.get("kernelStages", []):
            assert ks in valid, f"Invalid kernel stage: {ks}"
        assert len(profile.get("kernelStages", [])) >= 1

    def test_topology_is_reactive_or_continuous(self, profile):
        """'operate' category only allows reactive or continuous topology."""
        valid_operate_topologies = {"reactive", "continuous"}
        topology = profile.get("topology")
        assert topology in valid_operate_topologies, (
            f"'operate' category requires reactive or continuous topology, got '{topology}'"
        )

    def test_human_coupling_is_supervised_or_tighter(self, profile):
        """'operate' category default coupling is supervised."""
        coupling = profile.get("humanCoupling")
        valid = {"supervised", "approval_required", "collaborative", "human_led"}
        assert coupling in valid, (
            f"'operate' category requires supervised or tighter, got '{coupling}'"
        )

    def test_change_surfaces_are_valid(self, profile):
        valid = set(LOCK["vocabulary"]["changeSurfaces"])
        for surface in profile.get("changeSurfaces", []):
            assert surface in valid, f"Invalid change surface: {surface}"

    def test_work_profile_validates_against_schema(self, profile, contracts_registry):
        """Full schema validation of the profile as a WorkProfile instance."""
        instance = {
            "contractVersion": profile["contractVersion"],
            "id": profile["id"],
            "title": profile.get("title", ""),
            "category": profile["category"],
            "kernelStages": profile["kernelStages"],
            "topology": profile.get("topology"),
            "humanCoupling": profile.get("humanCoupling"),
            "changeSurfaces": profile.get("changeSurfaces", []),
            "deliverables": profile.get("deliverables", []),
            "evidence": profile.get("evidence", []),
        }
        validator = _validator("work-profile", contracts_registry)
        errors = list(validator.iter_errors(instance))
        assert errors == [], [e.message for e in errors]


# ── Provider-leak / Runtime-identity leak checks ────────────────────────────


class TestProviderFree:
    """The profile must not name any concrete provider or runtime identity."""

    def test_no_provider_name_in_profile(self, profile):
        """Check YAML data values (not comments) for provider name leaks."""
        data_text = yaml.dump(profile).lower()
        offenders = [n for n in FORBIDDEN_PROVIDER_NAMES if n in data_text]
        assert not offenders, f"Provider name leaked into generic profile: {offenders}"

    def test_no_runtime_identity_in_profile(self, profile):
        """Check YAML values (not comments) for runtime identity leaks.

        Comments may reference these names to explain what is excluded;
        the test checks the actual data fields: roles, metadata, etc.
        """
        # Serialize only the YAML data (stripping comment context) and check.
        # We check the roles dict for any model/tool/pin fields that contain
        # runtime identities, and the metadata for identity references.
        roles = profile.get("roles", {})
        for role_name, role_data in roles.items():
            text = yaml.dump(role_data).lower()
            for ident in FORBIDDEN_RUNTIME_IDS:
                assert ident.lower() not in text, (
                    f"Runtime identity '{ident}' found in role '{role_name}'"
                )
        meta = profile.get("metadata", {})
        meta_text = yaml.dump(meta).lower()
        for ident in FORBIDDEN_RUNTIME_IDS:
            assert ident.lower() not in meta_text, (
                f"Runtime identity '{ident}' found in metadata"
            )

    def test_no_model_field_in_roles(self, profile):
        roles = profile.get("roles", {})
        for role_name, role_data in roles.items():
            assert "model" not in role_data, (
                f"Role '{role_name}' has concrete model: {role_data['model']}"
            )

    def test_no_tool_field_in_roles(self, profile):
        roles = profile.get("roles", {})
        for role_name, role_data in roles.items():
            assert "tool" not in role_data, (
                f"Role '{role_name}' has concrete tool spec"
            )

    def test_no_pin_field_in_roles(self, profile):
        roles = profile.get("roles", {})
        for role_name, role_data in roles.items():
            assert "pin" not in role_data, (
                f"Role '{role_name}' has concrete pin: {role_data.get('pin')}"
            )

    def test_no_launch_command_in_profile(self, profile):
        data_text = yaml.dump(profile)
        assert "launch_command" not in data_text, "Profile contains launch_command"

    def test_no_harness_field_in_profile(self, profile):
        """The profile must not declare a 'harness' field (RoutingProfile
        refuses it at load time; this tests the data before loading)."""
        assert "harness" not in profile, "Profile declares a 'harness' field"


# ── RoutingProfile validation ────────────────────────────────────────────────


class TestRoutingProfile:
    """The profile loads as a valid RoutingProfile with generic roles."""

    def _load_routing_profile(self):
        """Import and load the profile as a RoutingProfile."""
        _src = REPO_ROOT / "src"
        if str(_src) not in sys.path:
            sys.path.insert(0, str(_src))

        from skillweave.routing.profile import RoutingProfile, load_profiles

        with open(PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return RoutingProfile.from_dict(data)

    def test_routing_profile_loads(self):
        profile = self._load_routing_profile()
        assert profile.name == "cms-operate-v1"
        assert profile.tier == "balanced"

    def test_routing_profile_has_generic_roles(self):
        profile = self._load_routing_profile()
        # Observer role is generic with no model
        observer = profile.role("observer")
        assert observer is not None
        assert observer.model is None
        assert observer.tool is None
        assert observer.is_observer is True

    def test_routing_profile_has_producer_role(self):
        profile = self._load_routing_profile()
        producer = profile.role("producer")
        assert producer is not None
        assert producer.model is None
        assert producer.tool is None
        assert producer.can("can_mutate_run_state") is True

    def test_routing_profile_has_reviewer_role(self):
        profile = self._load_routing_profile()
        reviewer = profile.role("reviewer")
        assert reviewer is not None
        assert reviewer.model is None
        assert reviewer.tool is None
        assert reviewer.can("can_approve_gate") is True

    def test_routing_profile_has_approver_role(self):
        profile = self._load_routing_profile()
        approver = profile.role("approver")
        assert approver is not None
        assert approver.can("can_approve_gate") is True

    def test_routing_profile_has_transaction_owner_role(self):
        profile = self._load_routing_profile()
        owner = profile.role("transaction-owner")
        assert owner is not None
        assert owner.can("can_observe_run") is True

    def test_no_self_approval_in_roles(self):
        """No single role has both can_mutate_run_state and can_approve_gate."""
        profile = self._load_routing_profile()
        for key, role in profile.roles.items():
            can_mutate = role.can("can_mutate_run_state")
            can_approve = role.can("can_approve_gate")
            assert not (can_mutate and can_approve), (
                f"Role '{key}' has both mutate and approve capabilities (self-approval)"
            )

    def test_limits_are_valid(self):
        profile = self._load_routing_profile()
        assert profile.limits.timeout == 120.0
        assert profile.limits.max_retries == 2
        assert profile.limits.min_models_required == 2
        assert profile.limits.on_model_failure == "skip"

    def test_routing_profile_to_dict_is_deterministic(self):
        profile = self._load_routing_profile()
        d1 = profile.to_dict()
        d2 = profile.to_dict()
        assert d1 == d2


# ── Evidence contract validation ─────────────────────────────────────────────


class TestEvidenceContract:
    """The profile's evidence contract validates against the evidence schema."""

    def test_evidence_contract_is_present(self, profile):
        meta = profile.get("metadata", {})
        assert "evidence_contract" in meta

    def test_evidence_contract_validates(self, profile, contracts_registry):
        evidence = profile["metadata"]["evidence_contract"]
        validator = _validator("evidence-contract", contracts_registry)
        errors = list(validator.iter_errors(evidence))
        assert errors == [], [e.message for e in errors]

    def test_evidence_requirements_have_rollback_verification(self, profile):
        evidence = profile["metadata"]["evidence_contract"]
        req_ids = {r["id"] for r in evidence.get("requirements", [])}
        assert "rollback-verification" in req_ids, (
            "Evidence contract must include rollback-verification requirement"
        )

    def test_evidence_requirements_are_valid(self, profile):
        evidence = profile["metadata"]["evidence_contract"]
        for req in evidence.get("requirements", []):
            assert "id" in req
            assert "kind" in req
            assert req["strength"] in ("declared", "reproduced", "independently_verified")
            assert req.get("kernelStage", "K0") in LOCK["vocabulary"]["kernelStages"]


# ── Deliverable contract validation ──────────────────────────────────────────


class TestDeliverableContract:
    """The profile's deliverable contract validates against the deliverable schema."""

    def test_deliverable_contract_is_present(self, profile):
        meta = profile.get("metadata", {})
        assert "deliverable_contract" in meta

    def test_deliverable_contract_validates(self, profile, contracts_registry):
        deliverable = profile["metadata"]["deliverable_contract"]
        validator = _validator("deliverable-contract", contracts_registry)
        errors = list(validator.iter_errors(deliverable))
        assert errors == [], [e.message for e in errors]

    def test_deliverable_entrypoints_have_prd(self, profile):
        deliverable = profile["metadata"]["deliverable_contract"]
        entry_ids = {e["id"] for e in deliverable.get("entrypoints", [])}
        assert "prd" in entry_ids, (
            "Deliverable contract must include PRD entrypoint (Blueprint-always)"
        )

    def test_deliverable_entrypoints_are_valid(self, profile):
        deliverable = profile["metadata"]["deliverable_contract"]
        valid_surfaces = set(LOCK["vocabulary"]["changeSurfaces"])
        for entry in deliverable.get("entrypoints", []):
            assert "id" in entry
            assert entry["surface"] in valid_surfaces
            assert len(entry.get("acceptance", "")) > 0


# ── Role and capability tests ────────────────────────────────────────────────


class TestRoleCapabilities:
    """The profile declares the expected generic roles."""

    GENERIC_ROLES = {"observer", "producer", "reviewer", "approver", "transaction-owner"}

    def test_all_generic_roles_are_declared(self, profile):
        declared = set(profile.get("roles", {}).keys())
        assert self.GENERIC_ROLES.issubset(declared), (
            f"Missing roles: {self.GENERIC_ROLES - declared}"
        )

    def test_observer_capabilities(self, profile):
        observer = profile["roles"].get("observer", {})
        assert observer.get("observer") is True
        caps = observer.get("capabilities", {})
        assert caps.get("can_observe_run") is True

    def test_producer_capabilities(self, profile):
        producer = profile["roles"].get("producer", {})
        caps = producer.get("capabilities", {})
        assert caps.get("can_mutate_run_state") is True

    def test_reviewer_capabilities(self, profile):
        reviewer = profile["roles"].get("reviewer", {})
        caps = reviewer.get("capabilities", {})
        assert caps.get("can_approve_gate") is True

    def test_approver_capabilities(self, profile):
        approver = profile["roles"].get("approver", {})
        caps = approver.get("capabilities", {})
        assert caps.get("can_approve_gate") is True

    def test_transaction_owner_capabilities(self, profile):
        owner = profile["roles"].get("transaction-owner", {})
        caps = owner.get("capabilities", {})
        assert caps.get("can_observe_run") is True
