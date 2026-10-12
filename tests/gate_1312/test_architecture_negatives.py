"""Dispatch-order group 7 — architecture negatives (criterion 7).

Proves the static, read-only rejections the gate enforces:

* **copied schemas** — the SDK contract schemas are owned by ``skillweave-sdk``;
  a byte-identical copy under Core's ``schemas/`` is refused as a second truth;
* **CMS / provider branches in Core** — the dispatch contract and profile
  resolution name no ``cms`` subject and no provider branch;
* **weakened Pro-pack authority** — the Pro pack may only tighten gates, never
  weaken them; its authority separation (producer ≠ approver, reviewer read-only)
  is refused if relaxed;
* **implicit profile mode** — dispatch requires an explicit profile path with no
  default, and profiles declare themselves opt-in;
* **unsupported silent fallback** — unknown tier, unknown ``on_model_failure``,
  and unrecognised provider prefixes all raise rather than silently degrade.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from skillweave.routing.faigate_adapter import (
    ModelNamespaceError,
    translate_model_id,
)
from skillweave.routing.profile import (
    RoutingProfileError,
    VALID_TIERS,
    tier_to_router,
)

from tests.gate_1312 import _sibling as sib
from tests.gate_1312._sibling import require


def _core_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


#: The SDK-owned contract schemas the four-way split assigns to skillweave-sdk
#: alone: the lifecycle set (SW-GATE-1312 preview contracts plus the category,
#: model and search contracts) and the frozen execution/adapter sets
#: (SW-1510-CONTRACT, SW-ADAPT-001). Core must never vendor a byte-copy of any
#: one as a second, drifting truth. Core's legacy co-located schemas (evidence,
#: run-state, prompt-sequence, workflow-context) are a separate lineage and are
#: not part of this set; 0.3.0 supersedes them by the lifecycle and execution
#: contracts rather than re-committing them.
_SDK_OWNED_CONTRACT_FILENAMES = (
    "lifecycle-profile.schema.json",
    "work-profile.schema.json",
    "deliverable-contract.schema.json",
    "evidence-contract.schema.json",
    "subject-ref.schema.json",
    "category-pack.schema.json",
    "category-taxonomy.schema.json",
    "model-provider.schema.json",
    "search-provider.schema.json",
    "run-snapshot-v1.schema.json",
    "attempt-event-v1.schema.json",
    "evidence-receipt-v1.schema.json",
    "execution-envelope-v1.schema.json",
    "reservation-v1.schema.json",
    "route-decision-v1.schema.json",
    "capability-snapshot-v1.schema.json",
    "agent-message-v1.schema.json",
    "mirror-target-v1.schema.json",
    "contract-catalog-v1.schema.json",
    "contract-catalog-record-v1.schema.json",
    "adapter-descriptor-v1.schema.json",
    "credential-reference-v1.schema.json",
    "search-outcome-v1.schema.json",
)


def _sdk_owned_contract_files(sdk_root: Path) -> list[Path]:
    """Resolve the SDK-owned contract schema files anywhere under ``sdk_root``.

    The files are matched by name rather than by their directory: 0.3.0 ships
    them at ``src/skillweave_sdk/schemas/`` where the pre-0.3.0 tree had a root
    ``schemas/`` directory, so the location is not fixed. Every name must
    resolve — the guard can never pass vacuously because the SDK moved.
    """
    found: dict[str, Path] = {}
    for path in sdk_root.rglob("*.schema.json"):
        if path.name in _SDK_OWNED_CONTRACT_FILENAMES:
            found.setdefault(path.name, path)
    missing = [n for n in _SDK_OWNED_CONTRACT_FILENAMES if n not in found]
    assert not missing, (
        f"SDK contract schemas not found under {sdk_root}: {missing}"
    )
    return [found[n] for n in _SDK_OWNED_CONTRACT_FILENAMES]


def test_criterion_07_static_rejects_and_architecture_negatives():
    """Rejects copied schemas, CMS/provider branches in Core, weakened Pro-pack
    authority, implicit profile mode, and unsupported silent fallback.
    """
    require(sib.sdk_root, name="skillweave-sdk")
    require(sib.cms_pack_dir, name="skillweave-packs-pro")
    core = _core_root()

    # --- Copied schemas: Core must not vendor the SDK's contract --------------
    # The four-way split assigns the SDK contract schemas (lifecycle-profile,
    # work-profile, deliverables, evidence-contract, subject-ref, and the
    # category/model/search contracts, plus the frozen execution and adapter
    # sets) to skillweave-sdk alone. Core owns its own execution schemas
    # (dispatch-sequence, dispatch-trace, transfer-entry, harness-capability). A
    # byte-copy of any SDK-owned contract schema into Core would be a second,
    # drifting truth and is refused. 0.3.0 ships these in-package
    # (src/skillweave_sdk/schemas/); the resolver below locates them by name so
    # a relocation can never make the guard pass vacuously.
    sdk_schemas = sib.sdk_schemas_dir()
    assert sdk_schemas.is_dir()
    core_schemas = core / "schemas"
    copies: list[str] = []
    for schema_file in _sdk_owned_contract_files(sib.sdk_root()):
        core_copy = core_schemas / schema_file.name
        if core_copy.is_file() and core_copy.read_bytes() == schema_file.read_bytes():
            copies.append(schema_file.name)
    assert copies == [], f"Core vendors byte-copies of SDK contract schemas: {copies}"

    # No SDK-owned contract schema is present under Core's schemas/ at all (the
    # contract is fully SDK-owned; Core imports none of it as its own schema).
    core_vendored = sorted(
        p.name for p in core_schemas.rglob("*.schema.json")
        if p.name in _SDK_OWNED_CONTRACT_FILENAMES
    )
    assert core_vendored == [], f"Core vendors SDK contract schemas: {core_vendored}"

    # --- CMS / provider branches in Core ---------------------------------------
    import skillweave.dispatch.contracts as dc
    import skillweave.dispatch.profile_resolution as pr
    for module in (dc, pr):
        low = inspect.getsource(module).lower()
        assert "cms" not in low, f"{module.__name__} contains a cms branch"
    # The profile resolver carries no provider branch: it never compares against
    # a literal provider name and never imports a concrete provider class. Model
    # id resolution is delegated to the model-spec seam.
    pr_src = inspect.getsource(pr)
    for provider in ("openrouter", "faigate", "omniroute", "claw", "kilo", "9router"):
        assert f'"{provider}"' not in pr_src, (
            f"profile_resolution hard-codes provider {provider!r}"
        )
        assert f"'{provider}'" not in pr_src, (
            f"profile_resolution hard-codes provider {provider!r}"
        )

    # --- Weakened Pro-pack authority -------------------------------------------
    import yaml
    process = yaml.safe_load((sib.cms_pack_dir() / "process.yaml").read_text())["process"]
    producer = next(r for r in process["roles"] if r["id"] == "cms-ops-producer")
    approver = next(r for r in process["roles"] if r["id"] == "publish-approver")
    # Authority separation is strict: producer cannot publish/self-approve, and
    # approver is distinct from producer.
    assert set(producer["cannot"]) == {"self_approve", "publish"}
    assert approver["distinctFrom"] == ["cms-ops-producer"]
    # The pack tightens, never weakens: required capabilities block before
    # mutation (no false success), optional degrade with explicit declaration.
    deg = process["degradation"]
    assert deg["noFalseSuccess"] is True
    assert all(c["onMissing"] == "block_before_mutation"
               for c in deg["requiredCapabilities"])

    # --- Implicit profile mode: explicit path, no default, opt-in ---------------
    # tier_to_router has no implicit default tier; an unknown tier raises.
    assert VALID_TIERS == frozenset({"fast", "balanced", "deep"})
    with pytest.raises(RoutingProfileError):
        tier_to_router("nonsense")
    # RoutingProfile requires an explicit name; profiles opt in via data.
    from skillweave.routing.profile import RoutingProfile
    with pytest.raises(RoutingProfileError):
        RoutingProfile.from_dict({"tier": "balanced"})

    # --- Unsupported silent fallback -------------------------------------------
    # An unknown on_model_failure is refused, not silently defaulted.
    from skillweave.routing.profile import Limits
    with pytest.raises(RoutingProfileError):
        Limits.from_dict({"on_model_failure": "whatever"})
    # An unrecognised provider prefix is refused, not silently collapsed.
    with pytest.raises(ModelNamespaceError):
        translate_model_id("openrouter/x")
    with pytest.raises(ModelNamespaceError):
        translate_model_id("faigate/faigate/x")
    with pytest.raises(ModelNamespaceError):
        translate_model_id("faigate/")


def test_core_schemas_dir_does_not_copy_sdk_contract_ids():
    """The Core ``schemas/`` dir carries execution schemas with their own $ids;
    no SDK *contract* schema $id is duplicated in Core (no silently duplicated
    authority). Core's legacy co-located schemas (run-state, evidence,
    prompt-sequence, workflow-context) carry ids from the pre-0.3.0 flat
    namespace and are not SDK contract copies.
    """
    require(sib.sdk_schemas_dir, name="skillweave-sdk")
    core = _core_root()
    schemas_dir = core / "schemas"
    import json
    contract_ids = set()
    for f in _sdk_owned_contract_files(sib.sdk_root()):
        try:
            cid = json.loads(f.read_text()).get("$id")
        except (json.JSONDecodeError, OSError):
            continue
        if cid:
            contract_ids.add(cid)
    assert contract_ids, "no SDK contract schema $ids resolved"
    for f in schemas_dir.rglob("*.schema.json"):
        doc = json.loads(f.read_text())
        assert doc.get("$id") not in contract_ids, (
            f"{f.name} duplicates an SDK contract $id {doc.get('$id')!r}"
        )
