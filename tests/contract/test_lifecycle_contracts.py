"""Generic Lifecycle Extension — standalone lifecycle contracts (SW-160).

These contracts are validated against the installed SDK (skillweave-sdk==0.2.0),
which is the contract authority. Core is a consumer: it loads contract IDs,
validation and digest from the installed SDK API rather than from hard-coded
unpublished bytes.

Acceptance criteria proven here:

* **AC1** — an external consumer validates every contract without installing
  SkillWeave core (the SDK is the single dependency; a child interpreter
  reproduces it).
* **AC2** — legacy software delivery stays representable without semantic loss
  (the canonical seven-phase substrate maps onto kernel stages with every id,
  order, skill, capability and phase type preserved).
* **AC3** — the model and search provider contracts name no concrete provider;
  a provider is an opaque, consumer-supplied identifier.
* **AC4** — unknown contract versions and unknown categories fail closed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

import skillweave_sdk.validator as _sdk_validator

CONTRACTS_DIR = (
    Path(__file__).resolve().parents[2] / "schemas" / "lifecycle-contracts"
)
LOCK = json.loads((CONTRACTS_DIR / "contract-lock.json").read_text(encoding="utf-8"))

# The seven canonical legacy software-delivery phases, in order, with the
# kernel stage each one collapses onto. Domain-neutral consumers read the
# kernel stage; legacy consumers keep reading the phase id.
LEGACY_PHASE_TO_KERNEL = {
    "discovery": "K0",
    "blueprint": "K1",
    "design": "K2",
    "build": "K3",
    "release": "K4",
    "launch": "K5",
    "post-release": "K6",
}

# Names that must never appear in a provider-neutral contract. A contract that
# hard-codes any of these would bind SkillWeave to a concrete vendor.
FORBIDDEN_PROVIDER_NAMES = (
    "openrouter",
    "faigate",
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

# ── SDK contract registry (contract authority) ──────────────────────────────

# The eight lifecycle contracts the SDK owns, keyed by short contract name.
# Derived from the installed SDK registry $id URLs.
_LIFECYCLE_CONTRACT_NAMES = [
    "work-profile",
    "lifecycle-profile",
    "deliverable-contract",
    "evidence-contract",
    "category-pack",
    "category-taxonomy",
    "model-provider",
    "search-provider",
]


def _sdk_registry() -> Registry:
    """Build a ``referencing`` Registry from the installed SDK schemas."""
    reg = _sdk_validator.load_registry()
    resources = []
    for sid, schema in reg.items():
        if "lifecycle" in sid:
            resources.append(
                (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
            )
    return Registry().with_resources(resources)


def _sdk_schema(contract: str) -> dict:
    """Return the SDK schema dict for a lifecycle contract name."""
    reg = _sdk_validator.load_registry()
    for sid, schema in reg.items():
        if f"lifecycle/{contract}" in sid:
            return schema
    raise ValueError(f"SDK schema not found for lifecycle contract {contract!r}")


def _sdk_validator_for(contract: str) -> Draft202012Validator:
    """Return a validator for the named contract using SDK schemas."""
    return Draft202012Validator(_sdk_schema(contract), registry=_sdk_registry())


def _minimal_instances() -> dict[str, dict]:
    """One smallest-legal instance per lifecycle contract."""
    return {
        "work-profile": {
            "contractVersion": "1.0.0",
            "id": "example-work",
            "category": "build",
            "kernelStages": ["K3"],
        },
        "lifecycle-profile": {
            "contractVersion": "1.0.0",
            "id": "example-lifecycle",
            "phases": [{"id": "phase-one", "order": 1, "kernelStage": "K0"}],
        },
        "deliverable-contract": {
            "contractVersion": "1.0.0",
            "id": "example-deliverable",
            "entrypoints": [
                {"id": "artifact", "surface": "code", "acceptance": "tests pass"}
            ],
        },
        "evidence-contract": {
            "contractVersion": "1.0.0",
            "id": "example-evidence",
            "requirements": [
                {"id": "proof", "kind": "test-run", "strength": "reproduced"}
            ],
        },
        "category-pack": {
            "contractVersion": "1.0.0",
            "id": "example-pack",
            "category": "assure",
        },
        "category-taxonomy": {
            "contractVersion": "1.0.0",
            "categories": LOCK["vocabulary"]["categories"],
            "kernelStages": LOCK["vocabulary"]["kernelStages"],
            "topologies": LOCK["vocabulary"]["topologies"],
            "humanCoupling": LOCK["vocabulary"]["humanCoupling"],
            "changeSurfaces": LOCK["vocabulary"]["changeSurfaces"],
        },
        "model-provider": {
            "contractVersion": "1.0.0",
            "id": "example-model",
            "hostFrameworkIdentifier": "consumer-supplied-host",
            "catalogueIdentifier": "consumer-supplied-catalogue",
        },
        "search-provider": {
            "contractVersion": "1.0.0",
            "id": "example-search",
            "hostFrameworkIdentifier": "consumer-supplied-host",
            "catalogueIdentifier": "consumer-supplied-catalogue",
        },
    }


def test_ac1_external_consumer_validates_every_contract_without_core():
    """AC1: every lifecycle contract validates from the installed SDK alone."""
    assert set(_minimal_instances()) == set(_LIFECYCLE_CONTRACT_NAMES)
    for contract, instance in _minimal_instances().items():
        errors = sorted(
            _sdk_validator_for(contract).iter_errors(instance),
            key=lambda e: list(e.path),
        )
        assert errors == [], f"{contract}: {[e.message for e in errors]}"


CHILD_SCRIPT = '''\
import json
import sys

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

import skillweave_sdk.validator as _sdk_validator

assert "skillweave" not in sys.modules, "Core leaked onto the import path"

reg = _sdk_validator.load_registry()

# Build a referencing Registry from the SDK schemas
resources = []
schemas_by_contract = {}
for sid, schema in reg.items():
    if "lifecycle" not in sid:
        continue
    resources.append((schema["$id"], Resource.from_contents(schema)))
    # Extract contract name from URL like .../lifecycle/<name>/v1
    contract_name = sid.split("/lifecycle/")[1].split("/v")[0]
    schemas_by_contract[contract_name] = schema

registry = Registry().with_resources(resources)

# One smallest-legal instance per lifecycle contract
lock_path = sys.argv[1]
lock = json.loads(open(lock_path).read())
instances = {
    "work-profile": {"contractVersion": "1.0.0", "id": "w", "category": "build", "kernelStages": ["K3"]},
    "lifecycle-profile": {"contractVersion": "1.0.0", "id": "l", "phases": [{"id": "p", "order": 1, "kernelStage": "K0"}]},
    "deliverable-contract": {"contractVersion": "1.0.0", "id": "d", "entrypoints": [{"id": "a", "surface": "code", "acceptance": "tests pass"}]},
    "evidence-contract": {"contractVersion": "1.0.0", "id": "e", "requirements": [{"id": "r", "kind": "test-run", "strength": "reproduced"}]},
    "category-pack": {"contractVersion": "1.0.0", "id": "c", "category": "assure"},
    "category-taxonomy": {"contractVersion": lock["contractSet"]["version"], **lock["vocabulary"]},
    "model-provider": {"contractVersion": "1.0.0", "id": "m", "hostFrameworkIdentifier": "h", "catalogueIdentifier": "c"},
    "search-provider": {"contractVersion": "1.0.0", "id": "s", "hostFrameworkIdentifier": "h", "catalogueIdentifier": "c"},
}

for name, instance in instances.items():
    schema = schemas_by_contract.get(name)
    if schema is None:
        print(f"{name}: no SDK schema found")
        sys.exit(1)
    validator = Draft202012Validator(schema, registry=registry)
    errors = list(validator.iter_errors(instance))
    if errors:
        print(f"{name}: {errors[0].message}")
        sys.exit(1)

print("OK")
'''


def test_ac1_standalone_child_interpreter_validates_contracts(tmp_path):
    """AC1: a fresh interpreter with no Core on the path validates the set."""
    script = tmp_path / "external_consumer.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    lock_path = CONTRACTS_DIR / "contract-lock.json"
    proc = subprocess.run(
        [sys.executable, str(script), str(lock_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK", proc.stdout


def test_ac2_legacy_software_delivery_representable_without_semantic_loss():
    """AC2: the canonical seven-phase substrate survives a lossless mapping."""
    substrate = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "substrate-root"
        / ".skillweave"
        / "phases.yaml"
    )
    legacy = yaml.safe_load(substrate.read_text(encoding="utf-8"))
    legacy_phases = legacy["phases"]
    assert [p["id"] for p in legacy_phases] == list(LEGACY_PHASE_TO_KERNEL)

    profile = {
        "contractVersion": "1.0.0",
        "id": "software-product-delivery",
        "title": "Legacy software product delivery",
        "category": "build",
        "topology": "linear",
        "humanCoupling": "supervised",
        "changeSurfaces": ["code", "configuration", "documents"],
        "phases": [
            {
                "id": p["id"],
                "order": p["order"],
                "kernelStage": LEGACY_PHASE_TO_KERNEL[p["id"]],
                "phaseType": p["phase_type"],
                "skills": p["skills"],
                "capabilities": p["capabilities"],
            }
            for p in legacy_phases
        ],
        "globalSkills": [
            {"id": g["id"], "type": g["type"], "description": g["description"]}
            for g in legacy["global_skills"]
        ],
    }

    validator = _sdk_validator_for("lifecycle-profile")
    errors = sorted(validator.iter_errors(profile), key=lambda e: list(e.path))
    assert errors == [], [e.message for e in errors]

    # No semantic loss: ids, order, phase type, skills and capabilities are
    # carried across verbatim, and every phase names a kernel stage.
    by_id = {p["id"]: p for p in profile["phases"]}
    assert len(by_id) == len(legacy_phases) == 7
    for original in legacy_phases:
        mapped = by_id[original["id"]]
        assert mapped["order"] == original["order"]
        assert mapped["phaseType"] == original["phase_type"]
        assert mapped["skills"] == original["skills"]
        assert mapped["capabilities"] == original["capabilities"]
        assert mapped["kernelStage"] in LOCK["vocabulary"]["kernelStages"]

    # Global skills are still representable (a legacy-only notion kept whole):
    # the accepted profile carries every fixture global across verbatim.
    carried = {g["id"]: g for g in profile["globalSkills"]}
    assert len(carried) == len(legacy["global_skills"])
    for original in legacy["global_skills"]:
        assert carried[original["id"]]["type"] == original["type"]
        assert carried[original["id"]]["description"] == original["description"]


def test_ac3_provider_contracts_accept_opaque_ids_and_name_no_provider():
    """AC3: providers are opaque consumer identifiers, never concrete names."""
    for contract in ("model-provider", "search-provider"):
        validator = _sdk_validator_for(contract)
        for opaque in ("acme-host", "internal-gateway", "vendor-neutral.catalog"):
            instance = {
                "contractVersion": "1.0.0",
                "id": "binding",
                "hostFrameworkIdentifier": opaque,
                "catalogueIdentifier": opaque,
            }
            errors = list(validator.iter_errors(instance))
            assert errors == [], [e.message for e in errors]

    # No contract in the SDK names a concrete provider.
    reg = _sdk_validator.load_registry()
    for sid, schema in reg.items():
        if "lifecycle" not in sid:
            continue
        text = json.dumps(schema).lower()
        for banned in FORBIDDEN_PROVIDER_NAMES:
            assert banned not in text, f"{sid} names provider {banned!r}"


@pytest.mark.parametrize("contract", sorted(_minimal_instances()))
def test_ac4_unknown_contract_versions_fail_closed(contract):
    """AC4: an unsupported contractVersion is rejected, never coerced."""
    instance = dict(_minimal_instances()[contract])
    instance["contractVersion"] = "9.9.9"
    assert list(_sdk_validator_for(contract).iter_errors(instance)), (
        f"{contract} accepted an unknown contract version"
    )


def test_ac4_unknown_categories_and_vocabulary_fail_closed():
    """AC4: unknown categories and unknown vocabulary members are rejected."""
    work = dict(_minimal_instances()["work-profile"])
    work["category"] = "unknown_category"
    assert list(_sdk_validator_for("work-profile").iter_errors(work))

    pack = dict(_minimal_instances()["category-pack"])
    pack["category"] = "not-a-category"
    assert list(_sdk_validator_for("category-pack").iter_errors(pack))

    for field, value in (
        ("kernelStages", ["K99"]),
        ("topology", "teleport"),
        ("humanCoupling", "unbounded"),
        ("changeSurfaces", ["emotions"]),
    ):
        instance = dict(_minimal_instances()["work-profile"])
        instance[field] = value
        assert list(_sdk_validator_for("work-profile").iter_errors(instance)), (
            f"unknown {field} accepted"
        )

    # Taxonomy is a closed enum on every dimension.
    tax = dict(_minimal_instances()["category-taxonomy"])
    tax["categories"] = LOCK["vocabulary"]["categories"] + ["unknown_category"]
    assert list(_sdk_validator_for("category-taxonomy").iter_errors(tax))

    # The lock advertises exactly the canonical 11 categories.
    assert LOCK["vocabulary"]["categories"] == [
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
    ]


def test_every_locked_contract_exists_in_the_sdk_registry():
    """Every lifecycle contract name resolves to an SDK schema."""
    reg = _sdk_validator.load_registry()
    sdk_contracts = set()
    for sid in reg:
        if "lifecycle" in sid:
            name = sid.split("/lifecycle/")[1].split("/v")[0]
            sdk_contracts.add(name)
    for contract in _LIFECYCLE_CONTRACT_NAMES:
        assert contract in sdk_contracts, f"{contract} not in SDK registry"
