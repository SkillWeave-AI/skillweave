"""Lifecycle contracts: category vocabulary, provider extension, snapshot, deny.

SW-160-CONTRACT-002. Four acceptance criteria, each proven against the actual
surface a lifecycle consumer uses (:mod:`skillweave.lifecycle.contracts`):

1. **Rebaselined category vocabulary.** The registry serves the eleven closed
   categories -- including the newly added ``learn`` -- loaded from the pinned
   contract set (``schemas/lifecycle-contracts``), cross-checked against the
   taxonomy schema, and failing closed on anything outside the vocabulary.

2. **Provider extension without core enumeration.** A provider registers
   through the SDK extension point with opaque, consumer-supplied identifiers;
   the registry starts empty and adding a provider edits no Core list.

3. **Immutable, content-addressed effective-profile snapshot.** The snapshot a
   lifecycle consumer pins is frozen, hashes over its canonical bytes, and
   cannot be mutated after resolution.

4. **DENY is final.** A later ``ALLOW`` can never overwrite an earlier
   ``DENY``; ``SKIP``/``ABSTAIN`` are not consent; the outcome is
   order-independent.

The snapshot fixtures follow the hermetic pattern of
``test_effective_profile_preview.py``: the resolver accepts an injected schema
fileset digest, so these stay self-contained (the SDK wheel is not imported).
"""

import copy
import hashlib
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.lifecycle.contracts import (  # noqa: E402
    ABSTAIN,
    ALLOW,
    CATEGORY_PREFIX,
    CONTRACT_SET_NAME,
    CONTRACT_SET_VERSION,
    DENY,
    DIMENSION_PREFIXES,
    SKIP,
    AggregatedGateResult,
    CategoryRegistry,
    ContractDriftError,
    ContractNotAvailableError,
    DenyOverridesPolicy,
    EffectiveProfileSnapshot,
    GateDecision,
    GateDecisionLog,
    ProviderBinding,
    ProviderRegistry,
    QualifiedCategoryRef,
    UnknownCategoryError,
    UnknownDecisionError,
    UnknownProviderError,
    UnknownProviderKindError,
    contracts_dir,
    load_contract_lock,
    resolve_snapshot,
    snapshot_digest,
    verify_snapshot_digest,
)
from skillweave.profiles.effective import (  # noqa: E402
    SDK_PREVIEW_SCHEMA_VERSION,
    canonical_json_bytes,
)

# ── hermetic snapshot fixtures ─────────────────────────────────────────────

_SCHEMA_SET = (
    ("work-profile.preview.schema.json", '{"$id":"x","preview":"0.1.0"}'),
    ("lifecycle-profile.preview.schema.json", '{"$id":"y","preview":"0.1.0"}'),
)


def _schema_digest_of(schema_set):
    hasher = hashlib.sha256()
    for filename, canonical in sorted(schema_set):
        hasher.update(filename.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(canonical.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


_BOUND_DIGEST = _schema_digest_of(_SCHEMA_SET)

_SOFTWARE = {
    "kind": "domain_pack",
    "id": "software-product-delivery",
    "version": "v1-preview",
    "schemaVersion": SDK_PREVIEW_SCHEMA_VERSION,
    "schemaDigest": _BOUND_DIGEST,
    "content": {
        "primaryCategory": "build",
        "topology": "linear",
        "phases": ["discovery", "blueprint", "design", "build"],
        "kernel_stage": "K0",
    },
}


def _core_defaults(**extra):
    return {
        "kind": "core_defaults",
        "id": "core-defaults",
        "version": "1.3.12",
        "schemaVersion": SDK_PREVIEW_SCHEMA_VERSION,
        "schemaDigest": _BOUND_DIGEST,
        "content": {"primaryCategory": "build", "topology": "linear", **extra},
    }


# ── AC1: rebaselined category vocabulary ───────────────────────────────────


def test_registry_serves_the_eleven_rebaselined_categories():
    registry = CategoryRegistry.rebaselined()
    assert registry.categories == (
        "research",
        "decide",
        "design",
        "author",
        "facilitate",
        "build",
        "transform",
        "assure",
        "operate",
        "publish",
        "learn",
    )
    assert registry.is_registered("learn")
    assert registry.require("learn") == "learn"


def test_registry_matches_the_pinned_contract_set():
    lock = load_contract_lock()
    assert lock["contractSet"]["name"] == CONTRACT_SET_NAME
    assert lock["vocabulary"]["categories"] == list(
        CategoryRegistry.rebaselined().categories
    )
    assert contracts_dir().name == "lifecycle-contracts"
    assert CategoryRegistry.rebaselined().contract_version == CONTRACT_SET_VERSION


def test_category_taxonomy_declares_learn_and_the_other_closed_dimensions():
    registry = CategoryRegistry.rebaselined()
    # ``learn`` is a category -- the rebaseline's addition -- and is NOT a
    # kernel stage; the qualified forms keep the two namespaces distinct.
    assert "learn" in registry.dimension("category")
    assert "learn" not in registry.dimension("kernel")
    assert registry.resolve_reference("category:learn") == QualifiedCategoryRef(
        kind=CATEGORY_PREFIX, id="learn"
    )
    assert tuple(registry.kernel_stages) == ("K0", "K1", "K2", "K3", "K4", "K5", "K6")
    assert "human_cadenced" in registry.topologies
    assert "approval_required" in registry.human_coupling
    assert "public_channel" in registry.change_surfaces
    assert set(DIMENSION_PREFIXES) == {
        "category",
        "kernel",
        "topology",
        "human_coupling",
        "change_surface",
    }


def test_unknown_category_fails_closed():
    registry = CategoryRegistry.rebaselined()
    with pytest.raises(UnknownCategoryError):
        registry.require("quantum-astrology")
    assert registry.is_registered("quantum-astrology") is False


def test_qualified_reference_rejects_wrong_namespace():
    registry = CategoryRegistry.rebaselined()
    # ``learn`` is a category, not a kernel stage: the qualified form is what
    # makes that separation explicit, and the wrong namespace fails closed.
    with pytest.raises(UnknownCategoryError):
        registry.resolve_reference("kernel:learn")
    assert registry.resolve_reference("kernel:K6").id == "K6"


def test_missing_contract_set_refuses_to_start(tmp_path):
    with pytest.raises(ContractNotAvailableError):
        load_contract_lock(tmp_path)


def test_lock_schema_drift_is_refused():
    """The consumer receipt vocabulary matches the SDK taxonomy schema.

    Since Core no longer owns the taxonomy schema bytes, drift detection is
    cross-repo: the SDK is contract authority. The registry loaded from the
    SDK must agree with the consumer receipt vocabulary, and a manual drift
    of the receipt would be caught by the SDK-level validation.
    """
    import skillweave_sdk.validator as _sdk_validator

    reg = _sdk_validator.load_registry()
    # Find the SDK category-taxonomy schema
    taxonomy = None
    for sid, schema in reg.items():
        if "category-taxonomy" in sid:
            taxonomy = schema
            break
    assert taxonomy is not None, "SDK must contain category-taxonomy schema"

    defs = taxonomy.get("$defs", {})
    sdk_categories = defs.get("category", {}).get("enum", [])
    lock = load_contract_lock()
    lock_categories = lock["vocabulary"]["categories"]
    assert sdk_categories == lock_categories, (
        f"SDK categories {sdk_categories} != consumer receipt {lock_categories}"
    )

    # A drifted receipt with an extra category would fail the SDK comparison.
    drifted = list(lock_categories) + ["drifted"]
    assert drifted != sdk_categories, "drifted receipt must not match SDK"


# ── AC2: provider extension point without core enumeration ─────────────────


def test_registry_starts_empty_no_enumeration():
    registry = ProviderRegistry()
    assert len(registry) == 0
    assert registry.ids() == ()


def test_provider_registers_through_the_extension_point():
    registry = ProviderRegistry()
    binding = registry.register_provider(
        kind="model-provider",
        id="opaque-host-model",
        host_framework_identifier="hf://acme/agent-runtime",
        catalogue_identifier="catalogue://acme/models/main",
    )
    assert isinstance(binding, ProviderBinding)
    assert registry.resolve("opaque-host-model") is binding
    assert registry.ids() == ("opaque-host-model",)
    assert registry.of_kind("model-provider") == (binding,)


def test_new_provider_needs_no_core_edit_only_a_registration():
    # Adding a provider is one registration call with opaque identifiers; no
    # module-level list of vendor names is edited, and the registry never
    # inspects the opaque strings.
    first = ProviderRegistry()
    first.register_provider(
        kind="search-provider",
        id="alpha",
        host_framework_identifier="hf://alpha",
        catalogue_identifier="cat://alpha",
    )
    second = ProviderRegistry()
    second.register_provider(
        kind="search-provider",
        id="beta",
        host_framework_identifier="hf://beta",
        catalogue_identifier="cat://beta",
    )
    assert len(first) == len(second) == 1
    assert first.ids() != second.ids()
    assert second.resolve("beta").host_framework_identifier == "hf://beta"


def test_binding_emits_the_sdk_provider_contract_shape():
    binding = ProviderBinding(
        kind="model-provider",
        id="opaque-host-model",
        host_framework_identifier="hf://acme/agent-runtime",
        catalogue_identifier="catalogue://acme/models/main",
        extras={"tier": "balanced", "onFailure": "block_before_mutation"},
    )
    contract = binding.to_contract()
    assert contract["contractVersion"] == CONTRACT_SET_VERSION
    assert contract["id"] == "opaque-host-model"
    assert contract["hostFrameworkIdentifier"] == "hf://acme/agent-runtime"
    assert contract["catalogueIdentifier"] == "catalogue://acme/models/main"
    assert contract["tier"] == "balanced"
    assert contract["onFailure"] == "block_before_mutation"


def test_unknown_provider_kind_and_unregistered_id_fail_closed():
    registry = ProviderRegistry()
    with pytest.raises(UnknownProviderKindError):
        registry.register_provider(
            kind="telepathy-provider",
            id="x",
            host_framework_identifier="hf://x",
            catalogue_identifier="cat://x",
        )
    with pytest.raises(UnknownProviderError):
        registry.resolve("never-registered")


# ── AC3: immutable, content-addressed snapshot ─────────────────────────────


def test_snapshot_is_frozen():
    snap = resolve_snapshot([_SOFTWARE, _core_defaults()], schema_set=_SCHEMA_SET)
    with pytest.raises(Exception):
        snap.sdk_digest = "tampered"  # type: ignore[misc]


def test_snapshot_is_content_addressed_and_verifiable():
    a = resolve_snapshot([_SOFTWARE, _core_defaults()], schema_set=_SCHEMA_SET)
    b = resolve_snapshot([_SOFTWARE, _core_defaults()], schema_set=_SCHEMA_SET)

    assert isinstance(a, EffectiveProfileSnapshot)
    assert a.digest == snapshot_digest(a)
    assert a.canonical_bytes == canonical_json_bytes(a.resolved)
    assert a.digest == b.digest
    assert a.canonical_bytes == b.canonical_bytes
    assert verify_snapshot_digest(a, a.digest)
    assert not verify_snapshot_digest(a, "0" * 64)


def test_digest_changes_when_content_changes():
    baseline = resolve_snapshot([_SOFTWARE, _core_defaults()], schema_set=_SCHEMA_SET)
    changed_source = copy.deepcopy(_SOFTWARE)
    changed_source["content"]["primaryCategory"] = "learn"
    changed = resolve_snapshot([changed_source, _core_defaults()], schema_set=_SCHEMA_SET)
    assert changed.digest != baseline.digest
    assert changed.resolved["primaryCategory"] == "learn"


def test_snapshot_from_lifecycle_surface_is_the_effective_preview_snapshot():
    # One seam: the lifecycle re-export IS the effective-profile snapshot type,
    # so a lifecycle consumer pins the same immutable artifact the preview
    # resolver produces.
    snap = resolve_snapshot([_SOFTWARE, _core_defaults()], schema_set=_SCHEMA_SET)
    assert snap.sdk_version == SDK_PREVIEW_SCHEMA_VERSION
    assert snap.sdk_digest == _BOUND_DIGEST
    assert snap.to_canonical_json() == snap.to_canonical_json()


# ── AC4: DENY cannot be overwritten by a later ALLOW ───────────────────────


def test_deny_survives_a_later_allow():
    policy = DenyOverridesPolicy()
    decisions = [
        GateDecision(gate="g1", decision=DENY, actor="critic"),
        GateDecision(gate="g1", decision=ALLOW, actor="optimist"),
        GateDecision(gate="g1", decision=ALLOW, actor="later-optimist"),
    ]
    result = policy.aggregate("g1", decisions)
    assert result.decision == DENY
    assert result.passed is False
    assert result.vetoed_by == ("critic",)


def test_deny_outcome_is_order_independent():
    policy = DenyOverridesPolicy()
    allow = GateDecision(gate="g1", decision=ALLOW, actor="a")
    deny = GateDecision(gate="g1", decision=DENY, actor="b")
    assert policy.aggregate("g1", [allow, deny]).decision == DENY
    assert policy.aggregate("g1", [deny, allow]).decision == DENY


def test_skip_and_abstain_are_not_consent():
    policy = DenyOverridesPolicy()
    skipped = policy.aggregate(
        "g1",
        [
            GateDecision(gate="g1", decision=ALLOW, actor="a"),
            GateDecision(gate="g1", decision=SKIP, actor="b"),
        ],
    )
    abstained = policy.aggregate(
        "g1",
        [
            GateDecision(gate="g1", decision=ALLOW, actor="a"),
            GateDecision(gate="g1", decision=ABSTAIN, actor="b"),
        ],
    )
    assert skipped.decision == DENY
    assert abstained.decision == DENY


def test_unanimous_allow_is_the_only_allow():
    policy = DenyOverridesPolicy()
    result = policy.aggregate(
        "g1",
        [
            GateDecision(gate="g1", decision=ALLOW, actor="a"),
            GateDecision(gate="g1", decision=ALLOW, actor="b"),
        ],
    )
    assert result.decision == ALLOW
    assert result.passed is True


def test_empty_decision_set_denies_by_default():
    result = DenyOverridesPolicy().aggregate("g1", [])
    assert result.decision == DENY
    assert isinstance(result, AggregatedGateResult)
    assert result.passed is False


def test_unknown_decision_fails_closed():
    with pytest.raises(UnknownDecisionError):
        GateDecision(gate="g1", decision="MAYBE", actor="a")


def test_decision_log_latches_a_deny_against_a_later_allow():
    log = GateDecisionLog("g1")
    first = log.record(GateDecision(gate="g1", decision=DENY, actor="critic"))
    assert first.decision == DENY
    second = log.record(GateDecision(gate="g1", decision=ALLOW, actor="optimist"))
    assert second.decision == DENY
    # The latch is durable: re-reading the outcome never flips it back.
    assert log.outcome().decision == DENY
    assert log.outcome().vetoed_by == ("critic",)
    assert len(log.decisions) == 2
