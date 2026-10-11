"""CMS Operate — Blueprint-always and no-ceremony tests (SW-159-CMS-PACK-001).

Tests:
  - Blueprint-always: every implementation run starts from a Blueprint-produced PRD.
  - No-ceremony counterexample: Discovery, Design, and Council are conditionally
    invoked; their absence is provably correct with a recorded skip-reason.
  - Dummy CMS adapter: the profile passes with a dummy adapter and no transformer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = REPO_ROOT / "profiles"
PROFILE_PATH = PROFILES_DIR / "cms-operate.v1.yaml"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def profile() -> dict:
    assert PROFILE_PATH.is_file(), f"Profile not found at {PROFILE_PATH}"
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Blueprint-always ────────────────────────────────────────────────────────


class TestBlueprintAlways:
    """Every implementation run starts from a Blueprint-produced PRD."""

    def test_profile_deliverables_include_prd(self, profile):
        """The profile's deliverables must include 'prd'."""
        deliverables = profile.get("deliverables", [])
        assert "prd" in deliverables, (
            "Profile must declare 'prd' as a deliverable (Blueprint-always)"
        )

    def test_profile_kernel_stages_include_K1(self, profile):
        """K1 (blueprint phase) must be in kernel stages."""
        stages = profile.get("kernelStages", [])
        assert "K1" in stages, (
            "Profile must include K1 kernel stage (blueprint phase)"
        )

    def test_deliverable_contract_entrypoint_has_prd_at_K1(self, profile):
        """The deliverable contract entrypoint for PRD must be at K1."""
        meta = profile.get("metadata", {})
        deliverable = meta.get("deliverable_contract", {})
        for entry in deliverable.get("entrypoints", []):
            if entry["id"] == "prd":
                assert entry.get("kernelStage") == "K1", (
                    "PRD deliverable must be at kernel stage K1 (blueprint phase)"
                )
                return
        pytest.fail("Deliverable contract missing 'prd' entrypoint")


# ── No-ceremony counterexample ──────────────────────────────────────────────


class TestNoCeremonyCounterexample:
    """Discovery, Design, and Council are optional — their absence is provably
    correct with a skip-reason recorded in the profile's lifecycle choices."""

    def test_profile_has_skip_reasons_block(self, profile):
        """The profile must document why optional phases are skipped."""
        meta = profile.get("metadata", {})
        assert "lifecycle_choices" in meta, (
            "Profile metadata must include 'lifecycle_choices' block documenting "
            "why Discovery, Design, and Council were selected or skipped"
        )

    def test_discovery_skip_reason_is_recorded(self, profile):
        """Discovery skip must have an evidence-based reason."""
        choices = profile.get("metadata", {}).get("lifecycle_choices", {})
        discovery = choices.get("discovery", {})
        assert "skip_reason" in discovery, (
            "Discovery skip_reason must be recorded"
        )
        reason = discovery["skip_reason"]
        assert len(reason) > 0, "Skip reason must not be empty"
        # The reason must reference framing or evidence resolution
        assert any(kw in reason.lower() for kw in ("framing", "evidence", "resolve", "clear")), (
            f"Discovery skip reason must reference framing/evidence resolution: '{reason}'"
        )

    def test_design_skip_reason_is_recorded(self, profile):
        """Design skip must have an evidence-based reason."""
        choices = profile.get("metadata", {}).get("lifecycle_choices", {})
        design = choices.get("design", {})
        assert "skip_reason" in design, (
            "Design skip_reason must be recorded"
        )
        reason = design["skip_reason"]
        assert len(reason) > 0, "Skip reason must not be empty"
        assert any(kw in reason.lower() for kw in ("ux", "design", "architecture", "competing", "evidenced")), (
            f"Design skip reason must reference UX/design/architecture: '{reason}'"
        )

    def test_council_skip_reason_is_recorded(self, profile):
        """Council skip must have an evidence-based reason."""
        choices = profile.get("metadata", {}).get("lifecycle_choices", {})
        council = choices.get("council", {})
        assert "skip_reason" in council, (
            "Council skip_reason must be recorded"
        )
        reason = council["skip_reason"]
        assert len(reason) > 0, "Skip reason must not be empty"
        assert any(kw in reason.lower() for kw in ("material uncertainty", "competing", "decision gate", "unanimous")), (
            f"Council skip reason must reference material uncertainty or decision gates: '{reason}'"
        )

    def test_blueprint_is_always_invoked(self, profile):
        """Blueprint must not be skipped (it is recommended/core for CMS)."""
        choices = profile.get("metadata", {}).get("lifecycle_choices", {})
        blueprint = choices.get("blueprint", {})
        assert blueprint.get("invoked") is True or blueprint.get("phase_type") == "recommended", (
            "Blueprint must always be invoked"
        )

    def test_no_ceremony_counterexample_is_provable(self, profile):
        """A no-ceremony run skips Discovery, Design, and Council legitimately.
        The profile must prove this by recording skip-reasons for all three
        optional phases."""
        choices = profile.get("metadata", {}).get("lifecycle_choices", {})
        for phase in ("discovery", "design", "council"):
            entry = choices.get(phase, {})
            reason = entry.get("skip_reason", "")
            assert len(reason) > 0, (
                f"No-ceremony: '{phase}' must have a skip_reason"
            )


# ── Dummy CMS adapter ────────────────────────────────────────────────────────


class TestDummyCmsAdapter:
    """The profile works with a dummy CMS adapter (no real provider)."""

    def test_profile_has_no_cms_adapter_reference(self, profile):
        """The generic profile must not reference any CMS adapter by name."""
        text = yaml.dump(profile)
        assert "cms_adapter" not in text, "Profile must not name a CMS adapter"

    def test_profile_has_no_transformer_reference(self, profile):
        """The generic profile must not reference any text transformer."""
        text = yaml.dump(profile)
        transformers = ("txtHumanizer", "txthumanizer", "text_transform")
        for t in transformers:
            assert t.lower() not in text.lower(), (
                f"Profile must not reference transformer '{t}'"
            )

    def test_dummy_adapter_pattern_is_supported(self):
        """The profile's evidence contract uses generic kinds that a dummy
        adapter can satisfy (test-run, independent-review, build-output,
        rollback-test)."""
        with open(PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        evidence = data.get("metadata", {}).get("evidence_contract", {})
        for req in evidence.get("requirements", []):
            kind = req.get("kind", "")
            # All kinds must be generic enough for a dummy adapter
            assert kind in ("test-run", "independent-review", "build-output", "rollback-test"), (
                f"Evidence kind '{kind}' may not be dummy-adapter compatible"
            )

    def test_no_text_transformation_required(self, profile):
        """The profile must not require any text transformation capability."""
        caps_used = set()
        for role_name, role_data in profile.get("roles", {}).items():
            caps = role_data.get("capabilities", {})
            caps_used.update(caps.keys())
        transformer_caps = {c for c in caps_used if "transform" in c.lower() or "humaniz" in c.lower()}
        assert not transformer_caps, (
            f"Profile must not require text transformation capabilities: {transformer_caps}"
        )
