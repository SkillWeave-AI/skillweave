"""E2E test for the research-synthesis.v1 WorkProfile (SW-160-VERT-002).

Four acceptance criteria, each as a red/green proof:

1. Research profile resolves through the same contract and run service as
   software delivery. Identical resolver (``resolve_dispatch_profile``),
   identical run service (``RunApplicationService``), identical six record
   kinds — the research category is a *profile*, not a second engine.
2. Evidence and review requirements are category-specific rather than copied
   from software delivery. The evidence ids, kinds, strengths and the review
   requirement are research properties; the software-delivery set is asserted
   absent.
3. No source-code or Git mutation is required for the positive research
   fixture. The positive run is a pure in-process print: the repository's HEAD
   and porcelain status are byte-identical before and after, and the profile
   names no ``code`` change surface.
4. A full positive run produces resolvable contract-derived receipts. Every
   record kind is present, the raw digest resolves back to the exact bytes,
   and the receipt's identity (tool/model/digest) is derived from the profile
   contract, not invented by the test.

Hermetic: in-memory SQLite and a trivial subprocess, never a real model or a
network call. ``jsonschema`` + ``referencing`` validate the profile's contract
blocks against the SDK-owned schemas in ``schemas/lifecycle-contracts/``.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

from skillweave.dispatch.profile_resolution import (  # noqa: E402
    ProfileResolutionError,
    resolve_dispatch_profile,
    resolve_limits,
)
from skillweave.routing import load_profiles_from_location  # noqa: E402
from skillweave.routing.profile import (  # noqa: E402
    CAP_APPROVE_GATE,
    CAP_MUTATE_RUN_STATE,
    RoutingProfile,
    RoutingProfileError,
    from_dict,
)
from skillweave.runsvc import RunApplicationService, RunExecution  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent.parent
_PROFILE_PATH = _REPO / "profiles" / "research-synthesis.v1.yaml"
_CONTRACTS_DIR = _REPO / "schemas" / "lifecycle-contracts"

_REQUIRED_ROLES = ("ops", "reviewer", "observer")

# The evidence and deliverable vocabulary the software-delivery vertical
# declares. AC2 is the assertion that NONE of it is copied into the research
# profile: these ids are the research profile's negative space.
_SOFTWARE_DELIVERY_DELIVERABLES = frozenset(
    {"prd", "architecture", "implementation", "tests", "release-notes"}
)
_SOFTWARE_DELIVERY_EVIDENCE = frozenset(
    {"test-results", "review-attestation", "build-artifact"}
)
_SOFTWARE_DELIVERY_CHANGE_SURFACES = frozenset(
    {"code", "configuration", "infrastructure", "documents"}
)

_SUBJECT_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _service(tmp_db=":memory:"):
    store = SQLiteRunStore(tmp_db)
    journal = EventJournal(store)
    raw = RawArtifactStore()
    return RunApplicationService(store, journal, raw), store, journal, raw


import skillweave_sdk.validator as _sdk_validator


def _sdk_schema(contract: str) -> dict:
    """Return the SDK schema dict for a lifecycle contract name."""
    reg = _sdk_validator.load_registry()
    for sid, schema in reg.items():
        if f"lifecycle/{contract}" in sid:
            return schema
    raise ValueError(f"SDK schema not found for lifecycle contract {contract!r}")


def _contract_registry() -> Registry:
    """Every lifecycle contract registered by its own ``$id`` from the installed SDK.

    The profile's contract blocks are validated against the SDK schemas —
    the SDK is contract authority.
    """
    reg = _sdk_validator.load_registry()
    resources = []
    for sid, schema in reg.items():
        if "lifecycle" in sid:
            resources.append(
                (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
            )
    return Registry().with_resources(resources)


def _validate_contract(contract: str, instance: dict) -> None:
    validator = Draft202012Validator(_sdk_schema(contract), registry=_contract_registry())
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert errors == [], f"{contract}: {[e.message for e in errors]}"


def _workprofile_projection(raw: dict) -> dict:
    """The WorkProfile contract fields, exactly as the schema defines them."""
    keys = (
        "contractVersion", "id", "title", "category", "kernelStages",
        "topology", "humanCoupling", "changeSurfaces", "deliverables", "evidence",
    )
    return {k: raw[k] for k in keys if k in raw}


def _software_delivery_like_profile() -> dict:
    """A software-delivery-shaped RoutingProfile, built in-test.

    The sibling vertical's file (``profiles/software-delivery.v2.yaml``) is not
    on this branch, so the "same run service" proof carries its own
    software-delivery-shaped data rather than depending on an unmerged path.
    """
    return {
        "name": "software-delivery-like",
        "tier": "balanced",
        "limits": {
            "timeout": 120.0,
            "max_retries": 2,
            "min_models_required": 2,
            "on_model_failure": "skip",
        },
        "roles": {
            "observer": {"observer": True, "capabilities": {"can_observe_run": True}},
            "ops": {
                "model": "faigate/deepseek-v4-pro",
                "tool": {
                    "name": "opencode",
                    "launch_command": "opencode run --model faigate/deepseek-v4-pro -",
                },
                "capabilities": {"can_mutate_run_state": True},
            },
            "reviewer": {
                "model": "faigate/deepseek-v4-pro",
                "tool": {
                    "name": "opencode",
                    "launch_command": "opencode run --model faigate/deepseek-v4-pro -",
                },
                "capabilities": {"can_approve_gate": True},
            },
        },
    }


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


# ---------------------------------------------------------------------------
# Criterion 1: same contract and same run service as software delivery
# ---------------------------------------------------------------------------

def test_profile_is_data_at_a_declared_location():
    """The profile is data in the tree, loadable through the routing seam."""
    assert _PROFILE_PATH.is_file(), f"missing profile data at {_PROFILE_PATH}"
    profiles = load_profiles_from_location(_PROFILE_PATH)
    assert "research-synthesis-v1" in profiles


def test_profile_carries_valid_workprofile_contract_fields():
    """The WorkProfile block is schema-valid against the SDK-owned contract."""
    raw = _load_yaml(_PROFILE_PATH)
    projection = _workprofile_projection(raw)
    _validate_contract("work-profile", projection)

    assert projection["contractVersion"] == "1.0.0"
    assert projection["id"] == "research-synthesis.v1"
    assert projection["category"] == "research"
    assert projection["kernelStages"] == ["K0", "K1", "K2", "K3", "K4", "K5", "K6"]


def test_research_profile_resolves_through_the_same_resolver():
    """The same ``resolve_dispatch_profile`` resolves both categories.

    One resolver, one failure vocabulary: a required role that is absent names
    that role for the research profile exactly as it does for any other.
    """
    resolved = resolve_dispatch_profile(str(_PROFILE_PATH), _REQUIRED_ROLES)
    assert resolved.profile_name == "research-synthesis-v1"
    assert set(resolved.roles) == set(_REQUIRED_ROLES)
    assert resolved.role("ops").is_launch() is True
    assert resolved.role("observer").in_place is True

    with pytest.raises(ProfileResolutionError) as exc:
        resolve_dispatch_profile(str(_PROFILE_PATH), ("ops", "nonexistent-role"))
    assert exc.value.field == "roles.nonexistent-role"


def test_research_run_goes_through_the_same_run_service_as_software_delivery():
    """Both categories drive the identical run service to the identical records.

    This is criterion 1 stated as behaviour: the research profile is not a
    second engine. It is resolved, then executed by the same
    ``RunApplicationService.execute`` and lands the same six record kinds with
    the same terminal state and gate verdict.
    """
    outcomes = {}
    for label, raw in (
        ("research", _load_yaml(_PROFILE_PATH)),
        ("software-delivery", _software_delivery_like_profile()),
    ):
        tmp = tempfile.mkdtemp()
        profile_path = os.path.join(tmp, f"{label}.yaml")
        with open(profile_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(raw, handle)

        resolved = resolve_dispatch_profile(profile_path, list(_REQUIRED_ROLES))
        service, store, journal, raw_store = _service()
        run_id = f"same-svc-{label}"
        result = service.execute(
            [sys.executable, "-c", f"print('{label}-output')"],
            run_id=run_id,
            tool=resolved.role("ops").tool.name,
            model=resolved.role("ops").model.resolved,
            subject_repo="skillweave",
            subject_commit=_SUBJECT_COMMIT,
            created_at="2026-09-27T00:00:00Z",
        )

        assert isinstance(result, RunExecution)
        assert result.run.state == "advance_or_stop"
        assert result.gate_state == "pass"
        assert journal.has_gaps(run_id) is False

        # The six record kinds, read back through the same seams for both.
        outcomes[label] = {
            "record_kinds": {
                "run": store.get_run(run_id) is not None,
                "journal": len(result.journal) >= 1,
                "raw_artifact": raw_store.resolve(result.raw_digest) == result.raw_bytes,
                "receipt": store.get_evidence(result.receipt.artifact_id) is not None,
                "verification": result.verification["verified_by"],
                "gate": result.gate_state,
            },
            "evidence_type": result.receipt.evidence_type,
        }

    assert outcomes["research"] == outcomes["software-delivery"], outcomes
    assert all(outcomes["research"]["record_kinds"].values())


def test_resolved_limits_travel_the_one_precedence_chain():
    """The research profile's limits are profile data on the one chain.

    No side table: the declared values flow through ``resolve_limits``
    unchanged, and an override wins field-by-field.
    """
    profile = from_dict(_load_yaml(_PROFILE_PATH))
    resolved = resolve_limits(profile.limits, None)
    assert resolved.timeout == 90.0
    assert resolved.max_retries == 3
    assert resolved.on_model_failure == "retry"

    # Overriding one field leaves the rest on the profile's values.
    from skillweave.routing.profile import Limits

    import dataclasses
    overridden = resolve_limits(profile.limits, dataclasses.replace(profile.limits, timeout=5.0))
    assert overridden.timeout == 5.0
    assert overridden.max_retries == 3
    assert overridden.on_model_failure == "retry"


# ---------------------------------------------------------------------------
# Criterion 2: category-specific evidence and review requirements
# ---------------------------------------------------------------------------

def test_evidence_requirements_are_not_copied_from_software_delivery():
    """The evidence vocabulary is research's, not software delivery's."""
    raw = _load_yaml(_PROFILE_PATH)
    evidence_ids = set(raw.get("evidence", []))

    assert evidence_ids == {"source-attribution", "contradiction-analysis", "synthesis-review"}
    assert evidence_ids.isdisjoint(_SOFTWARE_DELIVERY_EVIDENCE)
    assert "test-results" not in evidence_ids
    assert "build-artifact" not in evidence_ids


def test_deliverables_are_not_copied_from_software_delivery():
    raw = _load_yaml(_PROFILE_PATH)
    deliverables = set(raw.get("deliverables", []))

    assert deliverables == {
        "research-question",
        "source-corpus",
        "claim-evidence-map",
        "synthesis-narrative",
        "open-questions",
    }
    assert deliverables.isdisjoint(_SOFTWARE_DELIVERY_DELIVERABLES)
    assert "implementation" not in deliverables


def test_change_surfaces_exclude_the_code_surface():
    """Research lands documents and knowledge; it does not name ``code``."""
    raw = _load_yaml(_PROFILE_PATH)
    surfaces = set(raw.get("changeSurfaces", []))
    assert surfaces == {"documents", "knowledge", "data", "external_system"}
    assert "code" not in surfaces
    assert surfaces != _SOFTWARE_DELIVERY_CHANGE_SURFACES


def test_evidence_contract_instances_are_schema_valid_and_research_shaped():
    """Each evidence requirement is a valid, strength-carrying contract item."""
    raw = _load_yaml(_PROFILE_PATH)
    contract = raw["metadata"]["evidence_contract"]
    _validate_contract("evidence-contract", contract)

    assert contract["category"] == "research"
    by_id = {r["id"]: r for r in contract["requirements"]}
    assert set(by_id) == set(raw["evidence"]), (
        "the contract instances and the WorkProfile evidence list must be one truth"
    )
    # Research evidence is reproduced/independently verified, never merely
    # declared: a claim with no traceable source is not evidence.
    assert by_id["source-attribution"]["strength"] == "reproduced"
    assert by_id["contradiction-analysis"]["strength"] == "reproduced"
    assert by_id["synthesis-review"]["strength"] == "independently_verified"
    assert all(r.get("required") is True for r in contract["requirements"])


def test_deliverable_contract_instances_are_schema_valid_and_research_shaped():
    raw = _load_yaml(_PROFILE_PATH)
    contract = raw["metadata"]["deliverable_contract"]
    _validate_contract("deliverable-contract", contract)

    assert contract["category"] == "research"
    assert "code" not in contract["changeSurfaces"]
    entrypoint_ids = {e["id"] for e in contract["entrypoints"]}
    assert entrypoint_ids == set(raw["deliverables"]), (
        "the contract entrypoints and the WorkProfile deliverables must be one truth"
    )


def test_review_requirement_is_category_specific_and_independent():
    """The review requirement is research's, and it is not self-approval.

    The ops role cannot approve the gate; only the reviewer can, and the
    research review demands independent verification of the synthesis.
    """
    raw = _load_yaml(_PROFILE_PATH)
    review = raw["metadata"]["review_requirement"]

    assert review["id"] == "synthesis-review"
    assert review["strength"] == "independently_verified"
    assert review["reviewer_role"] == "reviewer"
    assert review["id"] != "review-attestation"  # software delivery's review id

    profile = from_dict(raw)
    ops = profile.role("ops")
    reviewer = profile.role("reviewer")
    assert ops.can(CAP_MUTATE_RUN_STATE) is True
    assert ops.can(CAP_APPROVE_GATE) is False
    assert reviewer.can(CAP_APPROVE_GATE) is True
    assert reviewer.can(CAP_MUTATE_RUN_STATE) is False


def test_self_approval_is_refused_for_the_research_profile_too():
    """The self-approval guard is category-independent."""
    bad = _software_delivery_like_profile()
    bad["roles"] = {
        "ops": {
            "model": "faigate/deepseek-v4-pro",
            "capabilities": {"can_mutate_run_state": True, "can_approve_gate": True},
        }
    }
    with pytest.raises(RoutingProfileError, match="self-approval"):
        from_dict(bad)


# ---------------------------------------------------------------------------
# Criterion 3: no source-code or Git mutation in the positive fixture
# ---------------------------------------------------------------------------

def test_positive_fixture_command_is_a_pure_print():
    """The positive run's command mutates nothing: it is an in-process print."""
    command = [sys.executable, "-c", "print('research-synthesis-positive')"]
    assert command[1] == "-c"
    # No shell, no git, no write verb anywhere in the vector.
    blob = " ".join(command).lower()
    for verb in ("git ", "rm ", "mv ", "cp ", "touch ", ">", ">>", "tee "):
        assert verb not in blob, verb


def test_positive_run_leaves_head_and_working_tree_unchanged():
    """A full positive run mutates no tracked source and no Git state.

    The repository's HEAD, porcelain status and stash list are captured before
    and after the run; all three must be identical. This is the executable form
    of "no source-code or Git mutation is required".
    """
    has_git = shutil.which("git") is not None
    before = None
    if has_git:
        before = (_git("rev-parse", "HEAD"), _git("status", "--porcelain"), _git("stash", "list"))

    resolved = resolve_dispatch_profile(str(_PROFILE_PATH), list(_REQUIRED_ROLES))
    service, store, journal, raw_store = _service()
    result = service.execute(
        [sys.executable, "-c", "print('research-synthesis-positive')"],
        run_id="research-no-mutation",
        tool=resolved.role("ops").tool.name,
        model=resolved.role("ops").model.resolved,
        subject_repo="skillweave",
        subject_commit=_SUBJECT_COMMIT,
        created_at="2026-09-27T00:00:00Z",
    )
    assert result.gate_state == "pass"

    if has_git:
        after = (_git("rev-parse", "HEAD"), _git("status", "--porcelain"), _git("stash", "list"))
        assert after == before, f"the positive run mutated Git state: {before!r} -> {after!r}"

    # The profile itself names no code surface, so a research run cannot be
    # required to touch source even outside the fixture.
    raw = _load_yaml(_PROFILE_PATH)
    assert "code" not in raw.get("changeSurfaces", [])


def test_positive_run_hashes_no_tracked_source_file_before_and_after():
    """Every tracked file's content hash is unchanged across the run."""
    if shutil.which("git") is None:
        pytest.skip("git not available to enumerate tracked files")

    tracked = [p for p in _git("ls-files").splitlines() if p]
    relevant = [p for p in tracked if p.startswith(("src/", "profiles/", "schemas/"))]

    def _hashes():
        out = {}
        for rel in relevant:
            path = _REPO / rel
            if path.is_file():
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out

    before = _hashes()
    service, store, journal, raw_store = _service()
    service.execute(
        [sys.executable, "-c", "print('research-synthesis-hash-proof')"],
        run_id="research-no-hash-mutation",
        tool="opencode",
        model="faigate/deepseek-v4-pro",
        subject_repo="skillweave",
        subject_commit=_SUBJECT_COMMIT,
        created_at="2026-09-27T00:00:00Z",
    )
    assert _hashes() == before


# ---------------------------------------------------------------------------
# Criterion 4: resolvable, contract-derived receipts
# ---------------------------------------------------------------------------

def test_positive_run_produces_resolvable_contract_derived_receipts():
    """A full positive research run leaves six resolvable record kinds."""
    resolved = resolve_dispatch_profile(str(_PROFILE_PATH), list(_REQUIRED_ROLES))
    ops = resolved.role("ops")

    service, store, journal, raw_store = _service()
    run_id = "research-synthesis-receipt"
    result = service.execute(
        [sys.executable, "-c", "print('research-synthesis-receipt-output')"],
        run_id=run_id,
        tool=ops.tool.name,
        model=ops.model.resolved,
        subject_repo="skillweave",
        subject_commit=_SUBJECT_COMMIT,
        created_at="2026-09-27T00:00:00Z",
    )

    # 1. Run: persisted, terminal.
    run = store.get_run(run_id)
    assert run is not None
    assert run.state == "advance_or_stop"

    # 2. Journal: gap-free.
    assert len(result.journal) >= 1
    assert result.journal[0].sequence == 1
    assert journal.has_gaps(run_id) is False

    # 3. Raw artifact: content-addressed and resolvable back to exact bytes.
    assert len(result.raw_digest) == 64
    assert raw_store.resolve(result.raw_digest) == result.raw_bytes
    assert b"research-synthesis-receipt-output" in result.raw_bytes
    assert hashlib.sha256(result.raw_bytes).hexdigest() == result.raw_digest

    # 4. Receipt: bound to the run, the bytes, and the resolved contract.
    assert result.receipt.artifact_id == f"runsvc-{run_id}"
    assert result.receipt.sha256 == result.raw_digest
    assert result.receipt.metadata["run_id"] == run_id
    # Contract-derived: the tool and model come from the profile contract, not
    # from the test's own literals.
    assert result.receipt.metadata["tool"] == ops.tool.name
    assert result.receipt.metadata["model"] == ops.model.resolved
    assert result.receipt.metadata["model"] == resolved.role("ops").model.requested
    assert store.get_evidence(result.receipt.artifact_id) is not None

    # 5. Verification: a separate verifier's verdict with its own identity, and
    #    a digest that recomputes from the contract's fields.
    assert result.verification["subject_artifact_id"] == result.receipt.artifact_id
    assert result.verification["verified_by"] == "verifier"
    assert result.verification["artifact_id"] == f"verify-{result.receipt.artifact_id}"
    assert result.verification["gate_state"] == "pass"
    expected_verify_digest = hashlib.sha256(
        f"{result.verification['subject_artifact_id']}|"
        f"{result.verification['grade']}|"
        f"{result.verification['gate_state']}".encode("utf-8")
    ).hexdigest()
    assert result.verification["sha256"] == expected_verify_digest

    # 6. Gate: the completion contract's verdict, PASS for real output.
    assert result.gate_state == "pass"


def test_no_output_never_gates_pass_for_research_either():
    """The gate is the completion contract's, not a category-specific bypass."""
    service, store, journal, raw_store = _service()
    result = service.execute(
        [sys.executable, "-c", "pass"],
        run_id="research-empty",
        tool="opencode",
        model="faigate/deepseek-v4-pro",
        subject_repo="skillweave",
        subject_commit=_SUBJECT_COMMIT,
        created_at="2026-09-27T00:00:00Z",
    )
    assert result.gate_state != "pass"
    run = store.get_run("research-empty")
    assert run is not None
    assert run.state == "advance_or_stop"
    assert run.metadata.get("stop_reason") == "before_gate"


def test_research_evidence_receipt_is_resolvable_by_its_own_digest():
    """The evidence receipt is content-addressed, not merely descriptive.

    Saving the research synthesis evidence the profile declares and resolving
    it back by digest is what makes the receipt resolvable rather than a claim.
    """
    from skillweave.runtime.registry import ArtifactReceipt, EvidenceType

    raw = _load_yaml(_PROFILE_PATH)
    evidence_contract = raw["metadata"]["evidence_contract"]
    body = json.dumps(evidence_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()

    raw_store = RawArtifactStore()
    raw_store.put(body)

    receipt = ArtifactReceipt(
        artifact_id=evidence_contract["id"],
        sha256=digest,
        schema_version=evidence_contract["contractVersion"],
        producer_command="resolve_dispatch_profile",
        subject_repo="skillweave",
        subject_commit=_SUBJECT_COMMIT,
        created_at="2026-09-27T00:00:00Z",
        evidence_type=EvidenceType.ARTIFACT.value,
        purpose="research synthesis evidence",
        method="research-synthesis.v1",
        system_source="runsvc",
        metadata={"category": evidence_contract["category"]},
    )
    assert raw_store.resolve(receipt.sha256) == body
    assert receipt.metadata["category"] == "research"
    # Resolving the digest is the proof: it is not a self-declared status.
    assert hashlib.sha256(raw_store.resolve(digest)).hexdigest() == digest


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "Skipped":
                print(f"SKIP {t.__name__}")
                continue
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
