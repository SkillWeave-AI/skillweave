"""Generic work contracts: subject, evidence, capability, authority, exact brief.

Covers the SW-159-WORK-001 acceptance surface:

1. Discriminated ``SubjectRef`` variants for repository, content,
   configuration, deployment and incident subjects — the non-Git variants are
   never required to synthesize a Git SHA.
2. ``EvidenceReceipt`` binds to any ``SubjectRef`` without forcing Git fields.
3. ``WorkContract`` declares authority, write scope, irreversible actions,
   verification, rollback, budget, methodology and policy.
4. Capabilities resolve through catalogue/profile data with **separated**
   declared / detected / runtime-attested states, and no harness, router,
   provider or model identifier appears in the module.
5. The exact-brief contract binds submitted bytes (or an immutable
   content-addressed reference) and fails **before** mutation when the worker
   and adherence digests differ.
6. The existing Git dispatch lane still produces a ``RepositorySubject``.

Self-contained sys.path handling, following the convention of
``test_dispatch_contract.py``. Schema validity is asserted structurally against
the shipped JSON schema file, with no ``jsonschema`` dependency.
"""

import json
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.dispatch.contracts import Lane  # noqa: E402
from skillweave.dispatch.work_contract import (  # noqa: E402
    CAPABILITY_DECLARED,
    CAPABILITY_DETECTED,
    CAPABILITY_RUNTIME_ATTESTED,
    CAPABILITY_STATES,
    IRREVERSIBLE_KINDS,
    SUBJECT_KINDS,
    WORK_AUTHORITY_ROLES,
    Budget,
    BriefBinding,
    CapabilityRegistry,
    CapabilityResolutionError,
    ConfigurationSubject,
    ContentSubject,
    DeploymentSubject,
    EvidenceBindingError,
    EvidenceReceipt,
    ExactBriefError,
    IncidentSubject,
    IrreversibleAction,
    RepositorySubject,
    SubjectRefError,
    VerificationClause,
    WorkContract,
    WorkContractError,
    WriteScope,
    assert_brief_digests_match,
    assert_brief_has_no_conflict_markers,
    assert_brief_matches,
    bind_brief,
    bind_brief_reference,
    bind_evidence,
    is_git_subject,
    load_capability_registry,
    prepare_brief_binding,
    sha256_digest,
    subject_identity,
    subject_ref_from_dict,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "work-contract.schema.json"
)

FULL_SHA = "0ef44d4ae2d41fb608c01b3d729995ffee5c22ae"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ── Subtask 1: discriminated SubjectRef variants ───────────────────────────


def test_subject_kinds_are_the_five_discriminated_variants():
    assert SUBJECT_KINDS == (
        "repository",
        "content",
        "configuration",
        "deployment",
        "incident",
    )


def test_repository_subject_requires_a_full_sha():
    subject = RepositorySubject(repo="skillweave/skillweave", commit=FULL_SHA)
    assert subject.kind == "repository"
    assert subject.commit == FULL_SHA
    assert subject.to_dict()["commit"] == FULL_SHA


def test_repository_subject_refuses_a_branch_name():
    try:
        RepositorySubject(repo="skillweave/skillweave", commit="main")
    except SubjectRefError as exc:
        assert exc.field == "commit"
    else:
        raise AssertionError("a branch name must not be accepted as a commit")


def test_repository_subject_refuses_an_empty_repo():
    try:
        RepositorySubject(repo="  ", commit=FULL_SHA)
    except SubjectRefError as exc:
        assert exc.field == "repo"
    else:
        raise AssertionError("an empty repo must fail")


def test_content_subject_carries_no_git_field():
    subject = ContentSubject(channel="blog", content_id="post-42", revision="rev-7")
    assert subject.kind == "content"
    payload = subject.to_dict()
    assert payload["channel"] == "blog"
    assert payload["content_id"] == "post-42"
    assert payload["revision"] == "rev-7"
    assert "repo" not in payload and "commit" not in payload
    assert not hasattr(subject, "commit")


def test_configuration_subject_carries_its_own_fields():
    subject = ConfigurationSubject(
        config_id="feature-flags", environment="staging", version="v12"
    )
    assert subject.kind == "configuration"
    assert subject.to_dict() == {
        "kind": "configuration",
        "config_id": "feature-flags",
        "environment": "staging",
        "version": "v12",
    }


def test_deployment_subject_artifact_digest_is_not_validated_as_a_sha():
    # A deployment artifact digest is a content address, not a Git commit; a
    # short/opaque digest must be accepted where a 40-hex commit would not be.
    subject = DeploymentSubject(
        target="edge-cluster", environment="prod", artifact_digest="sha256:abc123"
    )
    assert subject.kind == "deployment"
    assert subject.artifact_digest == "sha256:abc123"


def test_incident_subject_carries_state_and_severity():
    subject = IncidentSubject(incident_id="INC-77", state="mitigating", severity="sev2")
    assert subject.kind == "incident"
    assert subject.state == "mitigating"


def test_every_non_git_variant_rejects_an_empty_required_field():
    builders = [
        lambda: ContentSubject(channel="", content_id="c", revision="r"),
        lambda: ConfigurationSubject(config_id="", environment="e", version="v"),
        lambda: DeploymentSubject(target="", environment="e", artifact_digest="d"),
        lambda: IncidentSubject(incident_id="", state="s", severity="sev"),
    ]
    for build in builders:
        try:
            build()
        except SubjectRefError:
            pass
        else:
            raise AssertionError("an empty non-Git field must fail")


def test_subject_ref_from_dict_dispatches_on_kind():
    assert isinstance(
        subject_ref_from_dict({"kind": "repository", "repo": "r", "commit": FULL_SHA}),
        RepositorySubject,
    )
    assert isinstance(
        subject_ref_from_dict(
            {"kind": "content", "channel": "c", "content_id": "i", "revision": "r"}
        ),
        ContentSubject,
    )
    assert isinstance(
        subject_ref_from_dict(
            {
                "kind": "configuration",
                "config_id": "c",
                "environment": "e",
                "version": "v",
            }
        ),
        ConfigurationSubject,
    )
    assert isinstance(
        subject_ref_from_dict(
            {
                "kind": "deployment",
                "target": "t",
                "environment": "e",
                "artifact_digest": "d",
            }
        ),
        DeploymentSubject,
    )
    assert isinstance(
        subject_ref_from_dict(
            {
                "kind": "incident",
                "incident_id": "i",
                "state": "s",
                "severity": "sev1",
            }
        ),
        IncidentSubject,
    )


def test_subject_ref_from_dict_refuses_an_unknown_kind():
    try:
        subject_ref_from_dict({"kind": "email"})
    except SubjectRefError as exc:
        assert exc.field == "subject.kind"
    else:
        raise AssertionError("an unknown subject kind must fail, never be inferred")


def test_subject_ref_from_dict_refuses_a_missing_kind():
    try:
        subject_ref_from_dict({"repo": "r", "commit": FULL_SHA})
    except SubjectRefError as exc:
        assert exc.field == "subject.kind"
    else:
        raise AssertionError("a missing kind must fail")


def test_is_git_subject_is_true_only_for_the_repository_variant():
    assert is_git_subject(RepositorySubject(repo="r", commit=FULL_SHA))
    assert not is_git_subject(ContentSubject(channel="c", content_id="i", revision="r"))
    assert not is_git_subject(
        ConfigurationSubject(config_id="c", environment="e", version="v")
    )
    assert not is_git_subject(
        DeploymentSubject(target="t", environment="e", artifact_digest="d")
    )
    assert not is_git_subject(
        IncidentSubject(incident_id="i", state="s", severity="sev1")
    )


def test_subject_identity_is_variant_specific():
    assert (
        subject_identity(RepositorySubject(repo="r/x", commit=FULL_SHA))
        == f"repository:r/x@{FULL_SHA}"
    )
    assert (
        subject_identity(ContentSubject(channel="blog", content_id="p1", revision="r7"))
        == "content:blog/p1@r7"
    )
    assert (
        subject_identity(
            ConfigurationSubject(config_id="cfg", environment="stg", version="v3")
        )
        == "configuration:cfg@stg#v3"
    )
    assert (
        subject_identity(
            DeploymentSubject(target="edge", environment="prod", artifact_digest="sha256:9")
        )
        == "deployment:edge@prod#sha256:9"
    )
    assert (
        subject_identity(IncidentSubject(incident_id="INC-1", state="open", severity="sev1"))
        == "incident:INC-1@open"
    )


# ── Subtask 2: evidence bound to a subject without forcing Git fields ──────


def test_git_receipt_exposes_git_fields():
    receipt = bind_evidence(
        RepositorySubject(repo="r/x", commit=FULL_SHA),
        artifact_digest="a" * 64,
        evidence_type="test-run",
        purpose="prove the suite passes",
        method="pytest",
    )
    assert receipt.git_fields == {"repo": "r/x", "commit": FULL_SHA}
    payload = receipt.to_dict()
    assert payload["subject_repo"] == "r/x"
    assert payload["subject_commit"] == FULL_SHA


def test_non_git_receipt_is_complete_without_git_fields():
    for subject in (
        ContentSubject(channel="blog", content_id="p1", revision="r7"),
        ConfigurationSubject(config_id="cfg", environment="stg", version="v3"),
        DeploymentSubject(target="edge", environment="prod", artifact_digest="sha256:9"),
        IncidentSubject(incident_id="INC-1", state="open", severity="sev1"),
    ):
        receipt = bind_evidence(
            subject,
            artifact_digest="b" * 64,
            evidence_type="attestation",
            purpose="record the observed state",
        )
        assert receipt.git_fields is None
        payload = receipt.to_dict()
        assert "subject_repo" not in payload
        assert "subject_commit" not in payload
        assert payload["subject_identity"] == receipt.subject_identity


def test_receipt_refuses_an_unknown_subject_kind():
    class Bogus:
        kind = "email"

    try:
        EvidenceReceipt(subject=Bogus(), artifact_digest="a" * 64, evidence_type="t")
    except EvidenceBindingError as exc:
        assert exc.field == "evidence.subject"
    else:
        raise AssertionError("evidence must bind to a known subject kind")


def test_receipt_refuses_an_empty_digest_or_type():
    subject = IncidentSubject(incident_id="INC-1", state="open", severity="sev1")
    for kwargs in (
        {"artifact_digest": "", "evidence_type": "t"},
        {"artifact_digest": "a" * 64, "evidence_type": "  "},
    ):
        try:
            EvidenceReceipt(subject=subject, **kwargs)
        except (EvidenceBindingError, SubjectRefError):
            pass
        else:
            raise AssertionError(f"{kwargs} must fail")


# ── Subtask 3: capability resolution with separated states ─────────────────


def test_capability_states_are_three_and_distinct():
    assert CAPABILITY_STATES == ("declared", "detected", "runtime-attested")
    assert CAPABILITY_DECLARED != CAPABILITY_DETECTED
    assert CAPABILITY_DETECTED != CAPABILITY_RUNTIME_ATTESTED


def test_declared_only_capability_is_not_usable():
    registry = load_capability_registry(
        {"capabilities": {"code_search": True, "web_fetch": True}},
        profile={"detected": {"code_search": True}},
        source="catalogue",
    )
    declared_only = registry.resolve("web_fetch")
    assert declared_only.state() == CAPABILITY_DECLARED
    assert not declared_only.is_usable()

    detected = registry.resolve("code_search")
    assert detected.state() == CAPABILITY_DETECTED
    assert detected.is_usable()


def test_require_fails_closed_for_a_declared_only_capability():
    registry = load_capability_registry({"capabilities": {"web_fetch": True}})
    try:
        registry.require("web_fetch")
    except CapabilityResolutionError as exc:
        assert exc.field == "capabilities.web_fetch"
    else:
        raise AssertionError("a declared-only capability must be refused")


def test_runtime_attested_is_the_strongest_state():
    registry = load_capability_registry(
        {
            "capabilities": {"sandbox_exec": True},
            "detected": {"sandbox_exec": True},
            "runtime_attested": {"sandbox_exec": True},
        }
    )
    resolution = registry.resolve("sandbox_exec")
    assert resolution.runtime_attested
    assert resolution.state() == CAPABILITY_RUNTIME_ATTESTED
    assert registry.require("sandbox_exec").state() == CAPABILITY_RUNTIME_ATTESTED


def test_absent_capability_fails_closed_never_silently_available():
    registry = load_capability_registry({"capabilities": {}})
    resolution = registry.resolve("never_declared")
    assert resolution.declared is False
    assert resolution.state() == CAPABILITY_DECLARED  # weakest, and unusable
    assert not resolution.is_usable()
    try:
        registry.require("never_declared")
    except CapabilityResolutionError:
        pass
    else:
        raise AssertionError("an absent capability must never resolve as available")


def test_declared_only_lists_exactly_the_unproven_capabilities():
    registry = load_capability_registry(
        {"capabilities": {"a": True, "b": True, "c": True}},
        profile={
            "detected": {"a": True},
            "runtime_attested": {"b": True},
        },
    )
    assert registry.declared_only() == ["c"]


def test_profile_overlays_the_catalogue_for_detected_and_attested():
    registry = load_capability_registry(
        {"capabilities": {"a": True}},
        profile={"runtime_attested": {"a": True}},
    )
    assert registry.resolve("a").state() == CAPABILITY_RUNTIME_ATTESTED


def test_capability_resolution_keeps_each_state_separate_in_its_payload():
    resolution = CapabilityResolution_Like(
        name="x", declared=True, detected=False, runtime_attested=True
    )
    payload = resolution.to_dict()
    assert payload["declared"] is True
    assert payload["detected"] is False
    assert payload["runtime_attested"] is True
    assert payload["state"] == CAPABILITY_RUNTIME_ATTESTED


def CapabilityResolution_Like(**kwargs):
    from skillweave.dispatch.work_contract import CapabilityResolution

    return CapabilityResolution(**kwargs)


def test_capability_module_names_no_provider_harness_or_model():
    source = (
        _src / "skillweave" / "dispatch" / "work_contract.py"
    ).read_text(encoding="utf-8").lower()
    for forbidden in (
        "opencode",
        "deepseek",
        "claude",
        "codex",
        "gemini",
        "openai",
        "anthropic",
    ):
        assert forbidden not in source, f"no {forbidden} name may appear"


# ── Subtask 4: WorkContract authority, scope, actions, budget ──────────────


def test_authority_roles_are_five_and_distinct():
    assert WORK_AUTHORITY_ROLES == (
        "controller",
        "ops",
        "reviewer",
        "observer",
        "integrator",
    )


def _contract(**overrides) -> WorkContract:
    base = dict(
        id="WC-1",
        subject=RepositorySubject(repo="skillweave/skillweave", commit=FULL_SHA),
        authority="ops",
        write_scope=WriteScope(
            kind="paths", allow=["src/skillweave/dispatch/"], deny=["secrets/"]
        ),
        verification=[
            VerificationClause(
                method="unit", evidence_type="test-log", command="python -m pytest"
            )
        ],
        rollback="git revert",
        budget=Budget(max_correction_rounds=2, max_attempts=3),
        methodology="bounded-fanout",
        policy="ops-policy-v1",
        evidence_required=["test-log", "diff"],
    )
    base.update(overrides)
    return WorkContract(**base)


def test_contract_declares_every_required_axis():
    contract = _contract()
    payload = contract.to_dict()
    for key in (
        "id",
        "subject",
        "authority",
        "write_scope",
        "irreversible_actions",
        "verification",
        "rollback",
        "budget",
        "methodology",
        "policy",
        "evidence_required",
    ):
        assert key in payload, f"contract must declare '{key}'"
    assert payload["authority"] == "ops"
    assert payload["methodology"] == "bounded-fanout"
    assert payload["policy"] == "ops-policy-v1"
    assert payload["rollback"] == "git revert"


def test_contract_refuses_an_unknown_authority():
    try:
        _contract(authority="superuser")
    except WorkContractError as exc:
        assert exc.field == "WC-1.authority"
    else:
        raise AssertionError("an unknown authority must fail")


def test_contract_refuses_an_unknown_subject_kind():
    class Bogus:
        kind = "email"

        def to_dict(self):
            return {"kind": "email"}

    try:
        _contract(subject=Bogus())
    except WorkContractError as exc:
        assert exc.field == "WC-1.subject"
    else:
        raise AssertionError("an unknown subject must fail")


def test_write_scope_permits_inside_allow_and_denies_deny():
    scope = WriteScope(
        kind="paths",
        allow=["src/skillweave/dispatch/"],
        deny=["src/skillweave/dispatch/secrets/"],
    )
    assert scope.permits("src/skillweave/dispatch/contracts.py")
    assert not scope.permits("src/skillweave/runtime/authority.py")
    assert not scope.permits("src/skillweave/dispatch/secrets/token.py")


def test_write_scope_is_not_paths_only():
    # A non-Git workflow expresses its scope in its own objects.
    scope = WriteScope(kind="objects", allow=["content:blog"], deny=["content:prod"])
    assert scope.kind == "objects"
    assert scope.to_dict()["allow"] == ["content:blog"]


def test_irreversible_action_kind_must_be_known():
    IrreversibleAction(kind="release", authorization="ops sign-off", rollback="rollback tag")
    try:
        IrreversibleAction(kind="delete_everything")
    except WorkContractError as exc:
        assert exc.field == "irreversible_actions.kind"
    else:
        raise AssertionError("an unknown irreversible kind must fail")


def test_all_declared_irreversible_kinds_construct():
    for kind in sorted(IRREVERSIBLE_KINDS):
        assert IrreversibleAction(kind=kind).kind == kind


def test_contract_refuses_irreversible_action_without_authorization():
    contract = _contract(
        irreversible_actions=[IrreversibleAction(kind="push", rollback="force-with-lease")]
    )
    try:
        contract.assert_authorized()
    except WorkContractError as exc:
        assert "authorization" in str(exc)
    else:
        raise AssertionError("an irreversible action without authorization must fail")


def test_contract_refuses_irreversible_action_without_rollback_note():
    contract = _contract(
        irreversible_actions=[IrreversibleAction(kind="publish", authorization="ops")]
    )
    try:
        contract.assert_authorized()
    except WorkContractError as exc:
        assert exc.field == "WC-1.irreversible_actions.publish.rollback"
    else:
        raise AssertionError("an irreversible action without a rollback note must fail")


def test_contract_accepts_none_rollback_when_stated_explicitly():
    contract = _contract(
        irreversible_actions=[
            IrreversibleAction(
                kind="public_channel",
                authorization="ops",
                rollback="none: a published announcement cannot be unpublished",
            )
        ]
    )
    contract.assert_authorized()  # must not raise


def test_budget_refuses_a_negative_correction_round_or_zero_attempts():
    for kwargs in (
        {"max_correction_rounds": -1, "max_attempts": 1},
        {"max_correction_rounds": 0, "max_attempts": 0},
    ):
        try:
            Budget(**kwargs)
        except WorkContractError:
            pass
        else:
            raise AssertionError(f"{kwargs} must fail")


def test_budget_max_steps_is_explicitly_optional():
    assert Budget().to_dict()["max_steps"] is None
    assert Budget(max_steps=25).to_dict()["max_steps"] == 25


def test_verification_clause_requires_method_and_evidence_type():
    VerificationClause(method="review", evidence_type="review-note")
    for kwargs in ({"method": "", "evidence_type": "x"}, {"method": "x", "evidence_type": ""}):
        try:
            VerificationClause(**kwargs)
        except WorkContractError:
            pass
        else:
            raise AssertionError(f"{kwargs} must fail")


def test_contract_round_trips_through_from_dict():
    original = _contract(
        subject=ContentSubject(channel="blog", content_id="p1", revision="r7"),
        irreversible_actions=[
            IrreversibleAction(kind="publish", authorization="ops", rollback="unpublish")
        ],
    )
    rebuilt = WorkContract.from_dict(original.to_dict())
    assert rebuilt.to_dict() == original.to_dict()
    assert isinstance(rebuilt.subject, ContentSubject)
    assert rebuilt.irreversible_actions[0].kind == "publish"


# ── Subtask 5: exact-brief contract ────────────────────────────────────────


def test_bind_brief_content_addresses_the_exact_submitted_bytes():
    work = b"# brief\n\ndo the thing\n"
    reference = bind_brief(work)
    assert reference.digest == sha256_digest(work)
    assert reference.byte_length == len(work)
    assert reference.exact_bytes() == work


def test_bind_brief_refuses_a_non_bytes_payload():
    for bad in (None, "a string", 42):
        try:
            bind_brief(bad)
        except ExactBriefError:
            pass
        else:
            raise AssertionError(f"bind_brief({bad!r}) must fail")


def test_digest_match_passes_and_mismatch_fails_before_mutation():
    work = b"exact bytes\n"
    reference = bind_brief(work)
    # Identical bytes: the worker and adherence digests agree.
    assert_brief_digests_match(reference.digest, sha256_digest(work))

    # A different brief (e.g. trailing whitespace added in transit) must fail.
    mutated = work + b"\n"
    try:
        assert_brief_digests_match(reference.digest, sha256_digest(mutated))
    except ExactBriefError as exc:
        assert "mismatch" in str(exc)
        assert reference.digest in str(exc)
    else:
        raise AssertionError("a worker/adherence digest mismatch must fail closed")


def test_digest_match_refuses_missing_digests():
    for worker, adherence in (("", "a"), ("a", ""), ("", "")):
        try:
            assert_brief_digests_match(worker, adherence)
        except ExactBriefError:
            pass
        else:
            raise AssertionError("both digests are required")


def test_prepare_brief_binding_agrees_when_both_seams_see_the_same_bytes():
    work = b"same bytes both sides\n"
    binding = prepare_brief_binding(work, adherence_brief=work)
    assert isinstance(binding, BriefBinding)
    assert binding.worker == binding.adherence
    assert binding.worker == sha256_digest(work)


def test_prepare_brief_binding_fails_when_the_adherence_seam_saw_other_bytes():
    work = b"submitted\n"
    other = b"submitted\n\n"
    try:
        prepare_brief_binding(work, adherence_brief=other)
    except ExactBriefError as exc:
        assert "mismatch" in str(exc)
    else:
        raise AssertionError("differing brief bytes must fail before any mutation")


def test_content_addressed_reference_verifies_against_resolved_bytes():
    work = b"immutable brief\n"
    reference = bind_brief_reference(
        sha256_digest(work), byte_length=len(work), content_address="store://briefs/1"
    )
    assert reference.exact_bytes() is None  # reference carries no inline bytes
    assert_brief_matches(reference, work)  # resolves cleanly

    try:
        assert_brief_matches(reference, b"tampered\n")
    except ExactBriefError as exc:
        assert exc.field == "brief.content_address"
    else:
        raise AssertionError("tampered bytes must not match the content address")


def test_bind_brief_reference_refuses_a_non_sha256_digest():
    for bad in ("deadbeef", "z" * 64, ""):
        try:
            bind_brief_reference(bad, byte_length=1)
        except ExactBriefError:
            pass
        else:
            raise AssertionError(f"digest {bad!r} must fail")


def test_conflict_markers_in_a_brief_are_refused():
    assert_brief_has_no_conflict_markers(b"clean brief\n")
    for marker in (b"<<<<<<< HEAD\n", b"=======\n", b">>>>>>> other\n"):
        try:
            assert_brief_has_no_conflict_markers(b"text\n" + marker)
        except ExactBriefError:
            pass
        else:
            raise AssertionError(f"conflict marker {marker!r} must be refused")


# ── Subtask 6: the existing Git dispatch path is preserved ─────────────────


def test_git_lane_produces_a_repository_subject():
    lane = Lane(
        id="lane-ops",
        role="ops",
        repo="skillweave/skillweave",
        base=FULL_SHA,
        execution_model="cold",
        mutating=True,
    )
    subject = lane.to_subject_ref()
    assert isinstance(subject, RepositorySubject)
    assert subject.repo == "skillweave/skillweave"
    assert subject.commit == FULL_SHA


def test_git_lane_with_a_branch_base_has_no_subject_ref():
    lane = Lane(id="lane-ops", role="ops", repo="r/x", base="main", mutating=True)
    assert lane.to_subject_ref() is None


def test_read_only_lane_without_repo_has_no_subject_ref():
    lane = Lane(id="lane-review", role="reviewer", mutating=False)
    assert lane.to_subject_ref() is None


def test_legacy_lane_construction_is_unchanged():
    # The pre-existing Git contract every consumer already relies on.
    lane = Lane(id="l", role="ops", repo="r/x", base=FULL_SHA, execution_model="cold")
    assert lane.criteria_covered() == []
    assert lane.covers_criteria([])
    assert lane.to_dict()["repo"] == "r/x"


# ── Schema: the shipped JSON schema admits the generic contract ───────────


def _schema_validates(schema: dict, instance: dict) -> None:
    """A minimal structural validator (no ``jsonschema`` dependency)."""
    for key in schema.get("required", []):
        assert key in instance, f"missing required key '{key}'"
    props = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        for key in instance:
            assert key in props, f"key '{key}' not allowed by schema"
    for key, value in instance.items():
        spec = props.get(key)
        if spec is None:
            continue
        if spec.get("type") == "object" and isinstance(value, dict):
            _schema_validates(spec, value)
        if "enum" in spec and spec["enum"]:
            assert value in spec["enum"], f"'{key}' value {value!r} not in enum"


def test_schema_file_exists_and_requires_the_core_fields():
    schema = _schema()
    required = set(schema.get("required", []))
    assert {"id", "subject", "authority"} <= required
    assert schema["properties"]["authority"]["enum"] == list(WORK_AUTHORITY_ROLES)
    assert schema["additionalProperties"] is False


def test_schema_subject_enum_matches_the_five_variants():
    schema = _schema()
    subject = schema["$defs"]["subjectRef"]
    assert subject["properties"]["kind"]["enum"] == list(SUBJECT_KINDS)


def test_schema_non_git_subjects_require_their_own_fields_not_git_fields():
    schema = _schema()
    clauses = schema["$defs"]["subjectRef"]["allOf"]
    by_kind = {
        c["if"]["properties"]["kind"]["const"]: c["then"] for c in clauses
    }
    assert set(by_kind) == set(SUBJECT_KINDS)
    assert set(by_kind["repository"]["required"]) == {"repo", "commit"}
    assert set(by_kind["content"]["required"]) == {"channel", "content_id", "revision"}
    assert set(by_kind["configuration"]["required"]) == {
        "config_id",
        "environment",
        "version",
    }
    assert set(by_kind["deployment"]["required"]) == {
        "target",
        "environment",
        "artifact_digest",
    }
    assert set(by_kind["incident"]["required"]) == {
        "incident_id",
        "state",
        "severity",
    }
    for kind in ("content", "configuration", "deployment", "incident"):
        assert "commit" not in by_kind[kind]["required"], (
            f"non-Git subject '{kind}' must not require a Git commit"
        )


def test_schema_brief_digest_is_a_sha256_pattern():
    schema = _schema()
    assert schema["properties"]["brief_digest"]["pattern"] == "^[a-f0-9]{64}$"


def test_schema_admits_a_generic_contract_payload():
    schema = _schema()
    contract = _contract(
        subject=IncidentSubject(incident_id="INC-9", state="open", severity="sev1"),
    )
    payload = contract.to_dict()
    payload["brief_digest"] = sha256_digest(b"brief\n")
    payload["brief_byte_length"] = 6
    _schema_validates(schema, payload)


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
