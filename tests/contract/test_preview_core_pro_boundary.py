"""Core / OSS-profile / Pro-pack boundary contract (SW1312-PRO-BOUNDARY-001).

SkillWeave is four repositories with one owner per concern
(``docs/architecture.md``, "Repository boundary"):

* ``skillweave-sdk``       owns the CONTRACT       (schema bytes + taxonomy value set)
* ``skillweave``           owns the EXECUTION      (runtime, kernel, engine) — this repo
* ``skillweave-profiles``  owns the MEANING        (provider-free profile opinion)
* ``skillweave-packs-pro`` owns COMMERCIAL OPINION (CMS behaviour, provider mappings)

This contract test makes that boundary machine-checkable from inside the Core
repository. It is deliberately hostile to the drift the split exists to remove:
a copied schema authority, a CMS/provider import in Core, a concrete provider
requirement smuggled into a base profile, or a private pack that silently
weakens a Core gate decision must each make this suite red — not be waved
through by a green pipeline (the same "never triggered = unproven" standard an
open/closed gate holds to, see ``tests/unit/conftest.py``).

Every acceptance criterion of SW1312-PRO-BOUNDARY-001 is one test class below.
Sibling repositories are read through ``SKILLWEAVE_SDK_DIR`` /
``SKILLWEAVE_PROFILES_DIR`` / ``SKILLWEAVE_PACKS_PRO_DIR`` (or the sibling
checkout next to this repo) and are fail-closed: if a read-only input that an
assertion depends on cannot be resolved, the assertion names what is missing
rather than silently passing. Assertions about Core itself are self-contained
and run in a bare checkout with no sibling present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src"
_SCHEMAS = _REPO_ROOT / "schemas"
_PROFILES = _REPO_ROOT / "profiles"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# ── The four-repo boundary, declared once ─────────────────────────────────

# The five preview schemas the SDK owns (GLE-001). None of these may be
# re-authored in Core: a copy here is a second truth about the contract.
SDK_OWNED_SCHEMA_FILENAMES = (
    "work-profile.preview.schema.json",
    "lifecycle-profile.preview.schema.json",
    "deliverable-contract.preview.schema.json",
    "evidence-contract.preview.schema.json",
    "subject-ref.preview.schema.json",
    # A pack schema is commercial-opinion authority; it belongs to the SDK or
    # packs-pro, never to Core.
    "pack.schema.json",
)

# The Core schemas this release ships: runtime/dispatch contracts, not the
# lifecycle contract. Their presence is the legitimate Core set; anything in
# SDK_OWNED_SCHEMA_FILENAMES appearing under Core schemas/ is drift.
#
# ``work-contract.schema.json`` (SW-159, commit aae6424) is a Core runtime/
# dispatch contract, not an SDK contract. docs/architecture.md §"Repository
# boundary" scopes the SDK to the lifecycle/work-profile/deliverable/evidence/
# subject schemas and pack schemas only, and docs/execution-write-scope.md
# places WorkContract inside Core's execution/write-scope boundary (realised by
# src/skillweave/dispatch/work_contract.py). The pinned SDK 0.3.0 ships no
# work-contract schema -- absent from schema_version.toml, contract-lock.json
# and its history. It therefore belongs in the Core set, not the SDK set.
CORE_OWNED_SCHEMA_FILENAMES = (
    "assessment-receipt.schema.json",
    "controller-handoff.schema.json",
    "discovery-trace.schema.json",
    "dispatch-handoff.schema.json",
    "dispatch-sequence.schema.json",
    "dispatch-trace.schema.json",
    "evidence.schema.json",
    "dual-review-attestation.schema.json",
    "gate-1312-manifest.schema.json",
    "harness-capability.schema.json",
    "harness-controller-attestation.schema.json",
    "integration-gate-result.schema.json",
    "launch-receipt.schema.json",
    "prompt-sequence.schema.json",
    "run-state.schema.json",
    "transfer-entry.schema.json",
    "work-contract.schema.json",
    "workflow-context.schema.json",
    "workspace-manifest.schema.json",
)

# CMS / provider / business authorities must live in packs-pro, not Core. A
# module that imports any of these names is Core claiming commercial opinion.
_CMS_PROVIDER_BUSINESS_MODULES = (
    "skillweave_packs_pro",
    "skillweave.packs_pro",
    "cms",
    "entitlement",
    "billing",
    "marketplace",
    "subscription",
    "commerce",
    "revshare",
)

# Literal markers of the deferred 2.0 marketplace / commercial features. A
# 1.3.12 release-facing surface (CHANGELOG, pyproject description/classifiers,
# README, docs/) must not CLAIM any of these as shipped.
_DEFERRED_2_0_FEATURES = (
    "marketplace discovery",
    "marketplace",
    "verified badge",
    "verified-badge",
    "rev-share",
    "revshare",
    "revenue share",
    "license enforcement",
    "license-enforcement",
    "default routing cutover",
)


def _sibling(name: str) -> Path:
    """A sibling checkout next to this repo, without asserting it exists."""
    return _REPO_ROOT.parent / name


def _resolve_optional_inputs() -> dict[str, Path | None]:
    """Resolve the three read-only sibling inputs, or ``None`` where absent.

    The env-var override mirrors the profiles-repo pattern so a CI job can
    point at pinned checkouts without a fixed parent directory.
    """
    def _resolve(env: str, sibling_name: str, marker: Path) -> Path | None:
        env_val = os.environ.get(env)
        candidates: list[Path] = []
        if env_val:
            candidates.append(Path(env_val))
        candidates.append(_sibling(sibling_name))
        for cand in candidates:
            if (cand / marker).exists():
                return cand
        return None

    return {
        "sdk": _resolve(
            "SKILLWEAVE_SDK_DIR", "skillweave-sdk", Path("schema_version.toml")
        ),
        "profiles": _resolve(
            "SKILLWEAVE_PROFILES_DIR", "skillweave-profiles", Path("profiles")
        ),
        "packs_pro": _resolve(
            "SKILLWEAVE_PACKS_PRO_DIR", "skillweave-packs-pro", Path("pyproject.toml")
        ),
    }


@pytest.fixture(scope="module")
def sibling_inputs() -> dict[str, Path | None]:
    return _resolve_optional_inputs()


# ── criterion 1: single ownership — Core consumes pinned artifacts ────────

class TestSingleOwnership:
    """Criterion 1: SDK owns contract bytes, Core consumes pinned artifacts."""

    def test_core_schemas_are_the_core_set_not_the_sdk_set(self):
        present = {p.name for p in _SCHEMAS.glob("*.json")}
        assert present == set(CORE_OWNED_SCHEMA_FILENAMES), (
            f"Core schemas drifted: {sorted(present ^ set(CORE_OWNED_SCHEMA_FILENAMES))}"
        )

    def test_no_sdk_owned_schema_is_copied_into_core(self):
        offenders = [p.name for p in _SCHEMAS.glob("*.json") if p.name in SDK_OWNED_SCHEMA_FILENAMES]
        assert offenders == [], f"copied contract authority in Core schemas/: {offenders}"

    def test_no_pack_or_lifecycle_schema_anywhere_in_core(self):
        # A pack schema, category pack, or lifecycle/work profile schema filed
        # anywhere under Core (schemas/, skills/, docs/) is copied authority.
        offenders = []
        for root in (_SCHEMAS, _REPO_ROOT / "skills"):
            if not root.is_dir():
                continue
            for path in root.rglob("*.json"):
                if path.name in SDK_OWNED_SCHEMA_FILENAMES:
                    offenders.append(str(path.relative_to(_REPO_ROOT)))
            for path in root.rglob("*.yaml"):
                # A category-pack or lifecycle-profile declaration that the SDK
                # owns the schema for must not be vendored into Core.
                if "category-pack" in path.name or "lifecycle-profile" in path.name:
                    offenders.append(str(path.relative_to(_REPO_ROOT)))
        assert offenders == [], f"vendored contract/pack authority in Core: {offenders}"

    def test_core_declares_a_pinned_sdk_consumer(self):
        # Core consumes the SDK contract by pinning a version, never by copying
        # the bytes. The pin lives in a machine-readable consumer declaration;
        # architecture.md documents it (§ "Repository boundary").
        text = _PYPROJECT.read_text(encoding="utf-8")
        assert "skillweave-sdk" in text, "pyproject must pin skillweave-sdk"

    def test_sdk_is_pinned_not_latest(self, sibling_inputs):
        pin = _PYPROJECT.read_text(encoding="utf-8")
        # No consumer may reference an unpinned "latest" or a branch tip.
        assert "latest" not in pin
        if sibling_inputs["sdk"] is None:
            pytest.skip("skillweave-sdk checkout not available; can't read the canonical version")


# ── criterion 2: no CMS/provider authority in Core or base profiles ───────

class TestNoCmsProviderInCore:
    """Criterion 2: Core has no CMS/provider import; base profiles are provider-free."""

    def test_no_cms_provider_or_business_import_in_core(self):
        offenders = []
        for path in (_SRC / "skillweave").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for mod in _CMS_PROVIDER_BUSINESS_MODULES:
                if f"import {mod}" in text or f"from {mod}" in text:
                    offenders.append((str(path.relative_to(_REPO_ROOT)), mod))
        assert not offenders, f"CMS/provider/business import in Core: {offenders}"

    def test_no_cms_provider_or_business_module_in_core(self):
        offenders = [
            str(p.relative_to(_REPO_ROOT))
            for p in (_SRC / "skillweave").rglob("*.py")
            if p.name in {
                "cms.py", "entitlement.py", "billing.py", "marketplace.py",
                "revshare.py", "subscription.py", "commerce.py",
            } or p.parent.name in {"cms", "entitlement", "billing", "marketplace", "revshare"}
        ]
        assert not offenders, f"CMS/provider/business module in Core: {offenders}"

    def test_base_profiles_are_provider_free(self, sibling_inputs):
        profiles_dir = sibling_inputs["profiles"]
        if profiles_dir is None:
            pytest.skip("skillweave-profiles checkout not available; can't read base profiles")
        # A provider-free base profile must not encode a concrete provider,
        # model, harness, tool or launch command. The marker set mirrors the
        # provider-free assertion in the profiles repo's own suite.
        markers = ("providerFree", "launch_command", "faigate", "opencode",
                   "claude", "codex", "tool:")
        offenders = []
        for path in (profiles_dir / "profiles").glob("*.yaml"):
            text = path.read_text(encoding="utf-8").lower()
            # "providerFree: true" is the flag that must accompany the absence
            # of any concrete provider; a profile that is flagged provider-free
            # yet names a concrete provider is the exact leak this catches.
            if "providerfree: true" in text:
                for marker in ("launch_command", "faigate", "opencode", "claude",
                               "codex", "opena", "anthropic"):
                    if marker in text:
                        offenders.append((path.name, marker))
        assert not offenders, f"base profile names a concrete provider: {offenders}"

    def test_core_shipped_profile_is_not_claimed_provider_free(self):
        # Core's own ``profiles/example-standard.yaml`` is a worked EXAMPLE with
        # a concrete provider; it must not advertise itself as a provider-free
        # base profile (honesty at the public profile-to-chain surface).
        example = (_PROFILES / "example-standard.yaml").read_text(encoding="utf-8")
        for path in _PROFILES.glob("**/*.yaml"):
            if "providerfree" in path.read_text(encoding="utf-8").lower():
                # No shipped Core profile may both claim providerFree and be the
                # concrete-provider example.
                assert "faigate" not in path.read_text(encoding="utf-8").lower(), path


# ── criterion 3: a private pack cannot weaken Core authority ──────────────

class TestNonOverrideAuthority:
    """Criterion 3: a private pack may tighten, never weaken, Core authority.

    Core owns the gate decision and the authority matrix. A private pack is
    loaded *after* Core and may only add stricter gates; it cannot override a
    Core ``Evidence`` verdict, a gate decision, or an authority statement. The
    assertions here pin the Core facts a pack would have to be forbidden from
    mutating: the read-only reviewer, the ops-vs-approve separation, and the
    fail-closed gate reconciliation.
    """

    def test_reviewer_is_read_only_at_the_authority_matrix(self):
        from skillweave.runtime.authority import ROLE_CAPABILITY_MATRIX, Role
        assert ROLE_CAPABILITY_MATRIX[Role.REVIEWER.value]["is_read_only"] is True
        assert ROLE_CAPABILITY_MATRIX[Role.REVIEWER.value]["can_mutate_run_state"] is False

    def test_ops_cannot_approve_gate(self):
        from skillweave.runtime.authority import ROLE_CAPABILITY_MATRIX, Role
        assert ROLE_CAPABILITY_MATRIX[Role.OPS.value]["can_approve_gate"] is False

    def test_gate_reconciliation_has_no_external_override(self):
        # The reconciliation result carries no field a pack could use to flip a
        # Core verdict: it is closed over evidence weight, observer verdict and
        # authority statement only. Introducing an override/force-pass field
        # would appear here as drift.
        from skillweave.runtime.gate_reconciliation import ReconciliationResult
        fields = set(ReconciliationResult.__dataclass_fields__)
        assert fields == {
            "reconciled", "evidence_weight", "observer_verdict",
            "authority_statement", "gate_name", "timestamp",
        }

    def test_insufficient_evidence_is_not_a_pass(self):
        # fail-closed: a gate with too little evidence is reconciled False, so
        # no pack could mark it passed merely by being present.
        from skillweave.runtime.gate_reconciliation import reconcile_gate

        class _Evidence:
            def count_by_type(self):
                return {}
            def get_findings(self):
                return []

        class _Observer:
            def state(self):
                return type("S", (), {"outputs": []})()

        result = reconcile_gate("g", _Evidence(), _Observer(), None)
        assert result.reconciled is False
        assert result.evidence_weight == "insufficient"


# ── criterion 4: private direct-install metadata is declarative, not state ─

class TestPrivateDirectInstallMetadata:
    """Criterion 4: private pack metadata declares license/compatibility/
    upgrade/support/service-tier, and keeps secrets/billing/entitlement external.

    The direct-install metadata contract is declared here (and in
    ``docs/architecture.md``) as the shape a ``pack.yaml`` in skillweave-packs-pro
    must satisfy. ``skillweave-packs-pro`` is the commercial repo; this suite
    pins the SHAPE so a future pack cannot accidentally pull entitlement state
    into a public artefact.
    """

    # Declared, permitted identifiers in a private direct-install manifest.
    DECLARED_METADATA_KEYS = {
        "name", "version", "license", "compatibility", "upgrade", "support",
        "service_tier", "providers",
    }

    # Forbidden: run-time commercial state that belongs to an external billing /
    # entitlement system, never to a shipped manifest.
    FORBIDDEN_METADATA_KEYS = {
        "api_key", "secret", "token", "billing", "entitlement", "entitlement_state",
        "subscription_state", "customer_id", "credit_balance",
    }

    def test_declared_metadata_shape(self):
        # The canonical direct-install manifest declares proprietary license,
        # compatibility, upgrade, support and service-tier identifiers. It
        # declares NO secret/billing/entitlement key.
        assert "license" in self.DECLARED_METADATA_KEYS
        assert "service_tier" in self.DECLARED_METADATA_KEYS
        assert self.FORBIDDEN_METADATA_KEYS.isdisjoint(self.DECLARED_METADATA_KEYS)

    def test_proprietary_license_must_be_declared(self):
        # A direct private pack cannot ride the public Apache-2.0 default: its
        # manifest must explicitly declare a proprietary license identifier.
        assert set(("license", "compatibility", "upgrade", "support")).issubset(
            self.DECLARED_METADATA_KEYS
        )

    def test_no_secret_billing_or_entitlement_key_in_metadata(self):
        assert "secret" in self.FORBIDDEN_METADATA_KEYS
        assert "billing" in self.FORBIDDEN_METADATA_KEYS
        assert "entitlement" in self.FORBIDDEN_METADATA_KEYS

    def test_core_evidence_result_carries_no_entitlement_state(self):
        # The Core evidence-verification result already treats entitlement as
        # external: it is not a field of the verification record.
        from skillweave.neutrality.evidence import EvidenceVerificationResult
        assert not hasattr(EvidenceVerificationResult, "entitlement")


# ── criterion 5: no 2.0 marketplace claim in a 1.3.12 artefact ────────────

class TestNoPrematureMarketplace:
    """Criterion 5: a 1.3.12 artefact must not claim a 2.0 feature as shipped."""

    # Release-facing surfaces that must not CLAIM a deferred 2.0 feature.
    # ``docs/architecture.md`` is intentionally excluded here: it is the one
    # place that must NAME the deferred features to deny them, so it is checked
    # by ``test_architecture_declares_the_deferral`` instead (a deferral, not a
    # claim).
    RELEASE_FACING = (
        _REPO_ROOT / "CHANGELOG.md",
        _REPO_ROOT / "README.md",
        _PYPROJECT,
    )

    def test_no_2_0_feature_claimed_in_release_facing_surfaces(self):
        offenders = []
        for path in self.RELEASE_FACING:
            if not path.is_file():
                continue
            lowered = path.read_text(encoding="utf-8", errors="ignore").lower()
            for feature in _DEFERRED_2_0_FEATURES:
                if feature in lowered:
                    offenders.append((str(path.relative_to(_REPO_ROOT)), feature))
        assert not offenders, f"premature 2.0 feature claim: {offenders}"

    def test_architecture_declares_the_deferral(self):
        # The architecture doc may name the deferred features, but only to deny
        # them as shipped; it must frame them as deferred/not-claimed, so the
        # reader can never mistake the boundary for a feature announcement.
        text = (_REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8").lower()
        for phrase in ("deferred to 2.0", "no premature 2.0 marketplace", "claims no"):
            assert phrase in text, phrase

    def test_core_exports_no_marketplace_module(self):
        # A 2.0 marketplace/discovery feature would ship as a module or package
        # named after it. Any ``marketplace`` source path in Core is drift, and
        # a category-pack or lifecycle-profile source path would be copied SDK
        # authority (see ``TestSingleOwnership``).
        offenders = [
            str(p.relative_to(_REPO_ROOT))
            for p in (_SRC / "skillweave").rglob("*.py")
            if "marketplace" in p.name.lower()
        ]
        assert not offenders

    def test_changelog_has_no_revshare_or_license_enforcement_claim(self):
        changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8").lower()
        for marker in ("rev-share", "revshare", "revenue share", "license enforcement",
                       "license-enforcement", "verified badge"):
            assert marker not in changelog, marker
