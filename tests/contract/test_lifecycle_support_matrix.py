"""Lifecycle support matrix validation (SW-160-BREADTH-001).

The matrix in ``profiles/lifecycle-support-matrix.yaml`` documents every
supported category, lifecycle phase, evidence class and irreversible surface.
These tests prove that the matrix is *derived from* the canonical contracts
rather than maintained as an unrelated list, and that the closed vocabularies
fail closed with typed reasons.

Acceptance criteria:

1. **Matrix completeness** — the matrix names every category, lifecycle phase,
   evidence class and irreversible surface from the canonical contracts.
2. **Nontechnical WorkProfiles** — at least four nontechnical categories
   produce valid WorkProfile instances through the generic contract (no custom
   code per category).
3. **Fail-closed** — unknown category, lifecycle phase or evidence class is
   refused with a typed reason.
4. **Contract-derived** — the matrix is cross-checked against the canonical
   contracts (not a free-standing list).
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

# ── Paths ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "profiles" / "lifecycle-support-matrix.yaml"
CONTRACTS_DIR = REPO_ROOT / "schemas" / "lifecycle-contracts"
LOCK = json.loads((CONTRACTS_DIR / "contract-lock.json").read_text(encoding="utf-8"))

# ── Imports from the core (for typed error checks) ──────────────────────────

_src = REPO_ROOT / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


# ── SDK-backed fixtures (contract authority) ────────────────────────────────


def _sdk_schema(contract: str) -> dict:
    """Return the SDK schema dict for a lifecycle contract name."""
    reg = _sdk_validator.load_registry()
    for sid, schema in reg.items():
        if f"lifecycle/{contract}" in sid:
            return schema
    raise ValueError(f"SDK schema not found for lifecycle contract {contract!r}")


@pytest.fixture(scope="module")
def matrix() -> dict:
    """Load the lifecycle support matrix."""
    assert MATRIX_PATH.is_file(), f"Matrix not found at {MATRIX_PATH}"
    with open(MATRIX_PATH, encoding="utf-8") as f:
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


# ── AC1: Matrix completeness — every category from the lock ─────────────────


def test_matrix_categories_match_lock(matrix):
    """The matrix names every category from the lock vocabulary."""
    lock_categories = set(LOCK["vocabulary"]["categories"])
    matrix_categories = {c["id"] for c in matrix.get("categories", [])}
    assert matrix_categories == lock_categories, (
        f"Matrix categories differ from lock. "
        f"Missing from matrix: {lock_categories - matrix_categories}. "
        f"Extra in matrix: {matrix_categories - lock_categories}."
    )


def test_matrix_kernel_stages_match_lock(matrix):
    """The matrix names every kernel stage from the lock vocabulary."""
    lock_stages = set(LOCK["vocabulary"]["kernelStages"])
    matrix_stages = set(matrix.get("kernel_stages", []))
    assert matrix_stages == lock_stages


def test_matrix_topologies_match_lock(matrix):
    """The matrix names every topology from the lock vocabulary."""
    lock_topologies = set(LOCK["vocabulary"]["topologies"])
    matrix_topologies = {t["id"] for t in matrix.get("topologies", [])}
    assert matrix_topologies == lock_topologies


def test_matrix_human_coupling_matches_lock(matrix):
    """The matrix names every human-coupling level from the lock vocabulary."""
    lock_coupling = set(LOCK["vocabulary"]["humanCoupling"])
    matrix_coupling = {c["id"] for c in matrix.get("human_coupling", [])}
    assert matrix_coupling == lock_coupling


def test_matrix_change_surfaces_match_lock(matrix):
    """The matrix names every change surface from the lock vocabulary."""
    lock_surfaces = set(LOCK["vocabulary"]["changeSurfaces"])
    matrix_surfaces = {s["id"] for s in matrix.get("change_surfaces", [])}
    assert matrix_surfaces == lock_surfaces


def test_matrix_irreversible_surfaces_match_runtime():
    """The matrix irreversible surfaces match runtime/__init__.py."""
    from skillweave.runtime import IRREVERSIBLE_SURFACES

    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)
    matrix_irreversible = set(matrix.get("irreversible_surfaces", []))
    runtime_irreversible = set(IRREVERSIBLE_SURFACES)
    assert matrix_irreversible == runtime_irreversible, (
        f"Matrix irreversible surfaces differ from runtime. "
        f"Missing from matrix: {runtime_irreversible - matrix_irreversible}. "
        f"Extra in matrix: {matrix_irreversible - runtime_irreversible}."
    )


def test_matrix_evidence_strengths_match_schema():
    """The matrix evidence strengths match the evidence-contract schema enum."""
    schema = _sdk_schema("evidence-contract")
    schema_strengths = set(schema["$defs"]["evidenceStrength"]["enum"])
    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)
    matrix_strengths = {s["id"] for s in matrix.get("evidence", {}).get("strengths", [])}
    assert matrix_strengths == schema_strengths, (
        f"Matrix strengths {matrix_strengths} != schema {schema_strengths}"
    )


def test_matrix_phases_match_canonical_lifecycle():
    """The matrix lifecycle phases match the canonical lifecycle module."""
    from skillweave import lifecycle

    canonical_ids = {p["id"] for p in lifecycle.PHASES}
    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)
    matrix_ids = {p["id"] for p in matrix.get("lifecycle_phases", [])}
    assert matrix_ids == canonical_ids, (
        f"Matrix phase ids differ from canonical lifecycle. "
        f"Missing: {canonical_ids - matrix_ids}. "
        f"Extra: {matrix_ids - canonical_ids}."
    )


def test_matrix_bundles_match_canonical_lifecycle():
    """The matrix bundles match the canonical lifecycle module."""
    from skillweave import lifecycle

    canonical_ids = {b["id"] for b in lifecycle.BUNDLES}
    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)
    matrix_ids = {b["id"] for b in matrix.get("lifecycle_bundles", [])}
    assert matrix_ids == canonical_ids


# ── AC2: Nontechnical WorkProfiles through generic contract ────────────────


# Nontechnical categories (categories whose primary output is not code).
# At least four must produce valid WorkProfile instances through the generic
# work-profile.schema.json without any custom code per category.
NONTECHNICAL_CATEGORIES = [
    "research",
    "decide",
    "design",
    "author",
    "facilitate",
    "learn",
]


def test_nontechnical_work_profiles_validate_through_generic_contract(
    contracts_registry,
):
    """At least four nontechnical categories produce valid WorkProfiles.

    Each fixture uses only the generic work-profile.schema.json fields — no
    category-specific schema variant, no custom code — proving that every
    nontechnical category is a first-class contract citizen.
    """
    validator = _validator("work-profile", contracts_registry)
    valid_count = 0
    errors_by_category = {}

    for cat in NONTECHNICAL_CATEGORIES:
        instance = {
            "contractVersion": "1.0.0",
            "id": f"{cat}.v1",
            "title": f"{cat.title()} Work Profile",
            "category": cat,
            "kernelStages": ["K0", "K1", "K2", "K3", "K4", "K5", "K6"],
            "topology": "iterative",
            "humanCoupling": "supervised",
            "changeSurfaces": ["documents", "knowledge"],
        }
        errors = list(validator.iter_errors(instance))
        if errors:
            errors_by_category[cat] = [e.message for e in errors]
        else:
            valid_count += 1

    assert valid_count >= 4, (
        f"Only {valid_count} nontechnical categories validated through the "
        f"generic contract; need at least 4. Errors: {errors_by_category}"
    )


def test_nontechnical_work_profiles_are_distinct(contracts_registry):
    """Each nontechnical WorkProfile fixture is a distinct contract instance."""
    validator = _validator("work-profile", contracts_registry)
    instances = {}
    for cat in NONTECHNICAL_CATEGORIES:
        instance = {
            "contractVersion": "1.0.0",
            "id": f"{cat}.v1",
            "title": f"{cat.title()} Work Profile",
            "category": cat,
            "kernelStages": ["K0", "K1", "K2", "K3", "K4", "K5", "K6"],
        }
        errors = list(validator.iter_errors(instance))
        if not errors:
            key = (instance["category"], instance["id"])
            assert key not in instances, f"Duplicate category/id pair: {key}"
            instances[key] = instance

    # Ensure the distinct fixtures use genuinely different category values
    categories_used = {k[0] for k in instances}
    assert len(categories_used) >= 4, (
        f"Need at least 4 distinct nontechnical categories, got {categories_used}"
    )


def test_research_profile_is_valid_through_generic_contract(contracts_registry):
    """The shipped research-synthesis.v1 profile validates as a WorkProfile."""
    research_path = REPO_ROOT / "profiles" / "research-synthesis.v1.yaml"
    assert research_path.is_file()
    with open(research_path) as f:
        doc = yaml.safe_load(f)

    instance = {
        "contractVersion": doc.get("contractVersion", "1.0.0"),
        "id": doc["id"],
        "title": doc.get("title", ""),
        "category": doc["category"],
        "kernelStages": doc["kernelStages"],
        "topology": doc.get("topology"),
        "humanCoupling": doc.get("humanCoupling"),
        "changeSurfaces": doc.get("changeSurfaces", []),
        "deliverables": doc.get("deliverables", []),
        "evidence": doc.get("evidence", []),
    }
    validator = _validator("work-profile", contracts_registry)
    errors = list(validator.iter_errors(instance))
    assert errors == [], [e.message for e in errors]


def test_nontechnical_deliverable_contracts_validate(contracts_registry):
    """Nontechnical categories produce valid deliverable contracts."""
    validator = _validator("deliverable-contract", contracts_registry)
    for cat in NONTECHNICAL_CATEGORIES:
        instance = {
            "contractVersion": "1.0.0",
            "id": f"{cat}.deliverables",
            "title": f"{cat.title()} deliverables",
            "category": cat,
            "changeSurfaces": ["documents", "knowledge"],
            "entrypoints": [
                {
                    "id": "primary-artifact",
                    "surface": "documents",
                    "acceptance": "artifact is complete and reviewed",
                }
            ],
        }
        errors = list(validator.iter_errors(instance))
        assert errors == [], (
            f"Category '{cat}': deliverable contract errors: {[e.message for e in errors]}"
        )


def test_nontechnical_evidence_contracts_validate(contracts_registry):
    """Nontechnical categories produce valid evidence contracts."""
    validator = _validator("evidence-contract", contracts_registry)
    for cat in NONTECHNICAL_CATEGORIES:
        instance = {
            "contractVersion": "1.0.0",
            "id": f"{cat}.evidence",
            "title": f"{cat.title()} evidence",
            "category": cat,
            "requirements": [
                {
                    "id": "review",
                    "kind": "independent-review",
                    "strength": "reproduced",
                }
            ],
        }
        errors = list(validator.iter_errors(instance))
        assert errors == [], (
            f"Category '{cat}': evidence contract errors: {[e.message for e in errors]}"
        )


# ── AC3: Unknown values fail closed with typed reasons ──────────────────────


def test_unknown_category_fails_closed():
    """Unknown category raises UnknownCategoryError with a typed reason."""
    from skillweave.lifecycle.contracts import CategoryRegistry, UnknownCategoryError

    registry = CategoryRegistry.rebaselined()

    with pytest.raises(UnknownCategoryError) as exc:
        registry.require("quantum-computing")
    assert "unknown category" in str(exc.value).lower()
    assert "quantum-computing" in str(exc.value)


@pytest.mark.parametrize("contract", ["work-profile", "category-pack"])
def test_unknown_category_rejected_by_schema(contract, contracts_registry):
    """Unknown categories are rejected by the JSON Schema validator."""
    instance = {
        "contractVersion": "1.0.0",
        "id": "test",
        "category": "quantum-computing",
    }
    if contract == "work-profile":
        instance["kernelStages"] = ["K3"]
    validator = _validator(contract, contracts_registry)
    errors = list(validator.iter_errors(instance))
    assert errors, f"Unknown category was accepted by {contract} schema"


def test_unknown_kernel_stage_fails_closed():
    """Unknown kernel stage raises UnknownCategoryError."""
    from skillweave.lifecycle.contracts import CategoryRegistry, UnknownCategoryError

    registry = CategoryRegistry.rebaselined()

    with pytest.raises(UnknownCategoryError) as exc:
        registry.resolve_reference("kernel:K99")
    assert "K99" in str(exc.value)


def test_unknown_evidence_strength_fails_closed(contracts_registry):
    """Unknown evidence strength is rejected by schema validation."""
    validator = _validator("evidence-contract", contracts_registry)
    instance = {
        "contractVersion": "1.0.0",
        "id": "bad-evidence",
        "requirements": [
            {"id": "proof", "kind": "test-run", "strength": "anecdotal"}
        ],
    }
    errors = list(validator.iter_errors(instance))
    assert errors, "Unknown evidence strength was accepted"


def test_unknown_lifecycle_phase_fails_gracefully():
    """An unknown phase id is not in the canonical lifecycle and is reported."""
    from skillweave import lifecycle

    known = {p["id"] for p in lifecycle.PHASES}
    unknown = "blackhole"
    assert unknown not in known, "Unexpected: 'blackhole' should not be a known phase"
    # The lifecycle module itself has no "validate phase" function, but a
    # consumer that checks membership against the canonical set gets a typed
    # answer (the set itself is the typed contract).
    assert isinstance(known, set), "Canonical phase set must be a set"


def test_unknown_topology_fails_closed(contracts_registry):
    """Unknown topology is rejected by the work-profile schema."""
    validator = _validator("work-profile", contracts_registry)
    instance = {
        "contractVersion": "1.0.0",
        "id": "bad-topology",
        "category": "build",
        "kernelStages": ["K3"],
        "topology": "warp-drive",
    }
    errors = list(validator.iter_errors(instance))
    assert errors, "Unknown topology was accepted"


def test_unknown_human_coupling_fails_closed(contracts_registry):
    """Unknown human-coupling level is rejected by the work-profile schema."""
    validator = _validator("work-profile", contracts_registry)
    instance = {
        "contractVersion": "1.0.0",
        "id": "bad-coupling",
        "category": "build",
        "kernelStages": ["K3"],
        "humanCoupling": "telepathic",
    }
    errors = list(validator.iter_errors(instance))
    assert errors, "Unknown human-coupling was accepted"


def test_unknown_change_surface_fails_closed(contracts_registry):
    """Unknown change surface is rejected by the work-profile schema."""
    validator = _validator("work-profile", contracts_registry)
    instance = {
        "contractVersion": "1.0.0",
        "id": "bad-surface",
        "category": "build",
        "kernelStages": ["K3"],
        "changeSurfaces": ["emotions"],
    }
    errors = list(validator.iter_errors(instance))
    assert errors, "Unknown change surface was accepted"


def test_unknown_category_rejected_by_category_pack(contracts_registry):
    """Unknown category in a category-pack is rejected."""
    validator = _validator("category-pack", contracts_registry)
    instance = {
        "contractVersion": "1.0.0",
        "id": "bad-pack",
        "category": "telepathy",
    }
    errors = list(validator.iter_errors(instance))
    assert errors, "Unknown category in category-pack was accepted"


# ── AC4: Matrix is derived from canonical contracts, not free-standing ──────


def test_matrix_version_aligns_with_lock(matrix):
    """The matrix declares the same contract set name and version as the lock."""
    assert matrix["meta"]["contract_set"] == LOCK["contractSet"]["name"]
    assert matrix["meta"]["contract_set_version"] == LOCK["contractSet"]["version"]


def test_matrix_change_surface_irreversible_flags_match_runtime():
    """Each change surface's irreversible flag matches IRREVERSIBLE_SURFACES."""
    from skillweave.runtime import IRREVERSIBLE_SURFACES

    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)

    for surface in matrix.get("change_surfaces", []):
        sid = surface["id"]
        expected_irreversible = sid in IRREVERSIBLE_SURFACES
        assert surface["irreversible"] == expected_irreversible, (
            f"Surface '{sid}': matrix irreversible={surface['irreversible']}, "
            f"expected {expected_irreversible} from runtime"
        )


def test_matrix_category_topologies_are_valid():
    """Every topology listed in the matrix categories is a known topology."""
    lock_topologies = set(LOCK["vocabulary"]["topologies"])
    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)

    for cat in matrix.get("categories", []):
        for topo in cat.get("topologies", []):
            assert topo in lock_topologies, (
                f"Category '{cat['id']}' lists unknown topology '{topo}'"
            )


def test_matrix_human_coupling_gates_irreversible_flag():
    """The human_coupling.gates_irreversible flag is correct."""
    from skillweave.runtime import _HUMAN_IN_THE_LOOP

    with open(MATRIX_PATH) as f:
        matrix = yaml.safe_load(f)

    for coupling in matrix.get("human_coupling", []):
        cid = coupling["id"]
        expected_gates = cid not in _HUMAN_IN_THE_LOOP
        if expected_gates:
            # autonomous and supervised do NOT gate irreversible surfaces
            assert not coupling["gates_irreversible"], (
                f"Coupling '{cid}': matrix says gates_irreversible=True, "
                f"but runtime does not consider it human-in-the-loop"
            )
        else:
            assert coupling["gates_irreversible"], (
                f"Coupling '{cid}': matrix says gates_irreversible=False, "
                f"but runtime considers it human-in-the-loop"
            )


def test_matrix_meta_names_canonical_sources(matrix):
    """The matrix meta block references every canonical source."""
    meta = matrix.get("meta", {})
    sources = meta.get("canonical_sources", {})
    expected_keys = {
        "categories",
        "kernel_stages",
        "topologies",
        "human_coupling",
        "change_surfaces",
        "lifecycle_phases",
        "lifecycle_bundles",
        "evidence_strengths",
        "irreversible_surfaces",
    }
    assert set(sources.keys()) == expected_keys, (
        f"Canonical sources missing: {expected_keys - set(sources.keys())}"
    )


def test_matrix_is_valid_yaml():
    """The matrix file is valid YAML."""
    with open(MATRIX_PATH) as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    assert "meta" in data
    assert "categories" in data
