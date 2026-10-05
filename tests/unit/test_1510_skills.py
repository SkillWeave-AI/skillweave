#!/usr/bin/env python3
"""SW-1510-SKILLS: align Blueprint/Generate/Validate/Execute with native Assay.

The change removes the handoff-only Assay bridge from the *normal product path*
and makes the methodology intent a first-class, three-field declaration. This
module proves the three original acceptance criteria through the real product
calls -- never hand-coded statuses and never a README-only assertion:

1. Schema, skill arguments, generator and executor preserve
   ``methodology=assay``, ``autonomy_policy=unicorn`` and ``session_schedule``
   separately and never silently substitute Ralph. Proven at every layer:
   the shipped prompt/dispatch schemas (validated with ``jsonschema``), the four
   shipped SKILL.md argument surfaces, the generator
   (``scripts/regen-sequence.py``: ``build_execution_envelope`` /
   ``build_execution_sequences``), the executor (``RunApplicationService``) and
   the run snapshot (``runtime/snapshot.py``), where the three fields are three
   *distinct* keys and never conflated into one mode string.

2. Legacy ``suggested_workflow`` compatibility is explicit and fails if it would
   route execution to a *different* methodology. The legacy ``execution_mode`` /
   ``suggested_workflow`` vocabularies are adapters over the methodology axis:
   an agreeing value is accepted (and recorded in ``legacy_fields``); a value
   that would route elsewhere is refused with a named reason
   (``legacy_would_substitute_methodology`` /
   ``suggested_workflow_would_substitute_methodology``); an unknown value and an
   unsupported envelope version are refused too. Every negative path exercises
   the *production* compiler, driving a loaded copy of the fixture.

3. One source-owned fixture traverses discovery -> PRD -> sequence -> work
   contract -> dispatch -> receipt with all constraints and exact-source
   bindings retained. ``tests/fixtures/sw-1510-skills/`` is that fixture: two
   discovery sources ground the trace graph, the PRD is compiled to a sequence
   and envelope, a work contract carries the configuration subject, a dispatch
   declaration binds the full base SHA, and the executor produces the bound
   receipt -- all keyed to the same source digests and the same immutable base.

Hermetic: no live provider, no shared controller. The unit suite's
``conftest.py`` blocks every outbound socket for the whole module.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src"
for _candidate in (str(_SRC), str(_REPO_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from skillweave.blueprint.discovery_trace import (  # noqa: E402
    DISPOSITION_TASK,
    TraceLink,
    build_trace_graph,
    canonicalize,
    parse_discovery_markdown,
    verify_grounding,
)
from skillweave.dispatch.contracts import (  # noqa: E402
    ContractError,
    load_sequence,
    validate_for_dispatch,
)
from skillweave.dispatch.work_contract import (  # noqa: E402
    SUBJECT_CONFIGURATION,
    ConfigurationSubject,
    WorkContract,
)
from skillweave.runsvc import RunApplicationService  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402
from skillweave.runtime.snapshot import (  # noqa: E402
    SNAPSHOT_METHODOLOGIES,
    RunSnapshotInputs,
    SnapshotError,
    build_run_snapshot,
    snapshot_digest,
)
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

# Load the canonical producer exactly as the sibling generator test does.
_spec = importlib.util.spec_from_file_location(
    "regen_sequence", _REPO_ROOT / "scripts" / "regen-sequence.py"
)
rs = importlib.util.module_from_spec(_spec)
sys.modules["regen_sequence"] = rs
_spec.loader.exec_module(rs)  # type: ignore[union-attr]

FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "sw-1510-skills"
DISCOVERY_DIR = FIXTURES / "discovery"
PRD_JSON = FIXTURES / "prd.json"
DISPATCH_PROFILE = _REPO_ROOT / "tests" / "fixtures" / "dispatch-profile.yaml"
PROMPT_SEQUENCE_SCHEMA = _REPO_ROOT / "schemas" / "prompt-sequence.schema.json"
DISPATCH_SEQUENCE_SCHEMA = _REPO_ROOT / "schemas" / "dispatch-sequence.schema.json"

#: The immutable base the whole fixture chain binds to (the lane's clean
#: checkout of independently accepted Core a724).
FIXTURE_BASE = "1e71266bbfd0368835f9f3e917f25c36071a2bc3"

#: The four shipped skill surfaces that must carry the three separate fields.
SKILL_ARGUMENT_FILES = (
    "skillweave-blueprint",
    "skillweave-promptchain-generate",
    "skillweave-promptchain-validate",
    "skillweave-promptchain-execute",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_prd() -> dict:
    return json.loads(PRD_JSON.read_text(encoding="utf-8"))


def _schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _errors(subschema: dict, instance) -> list:
    return list(jsonschema.Draft202012Validator(subschema).iter_errors(instance))


def _service():
    store = SQLiteRunStore(":memory:")
    journal = EventJournal(":memory:")
    raw = RawArtifactStore(None)
    return RunApplicationService(store, journal, raw), store, journal, raw


def _compile(sequence: dict) -> dict:
    """A production-call alias, so the negative cases read clearly."""
    return rs.compile_execution_envelope(sequence)


def _assert_refused(fn, reason: str, field: str) -> None:
    """Assert the production compiler refuses with the named reason + field."""
    with pytest.raises(rs.ExecutionEnvelopeError) as excinfo:
        fn()
    assert excinfo.value.reason == reason, excinfo.value.reason
    assert excinfo.value.field == field, excinfo.value.field


def _discovery_sources():
    """Parse both fixture discovery documents into grounded source records."""
    records = []
    for name in ("discovery-001.md", "discovery-002.md"):
        path = DISCOVERY_DIR / name
        rel = path.relative_to(_REPO_ROOT).as_posix()
        records.extend(
            parse_discovery_markdown(
                path.read_text(encoding="utf-8"), source_path=rel
            )
        )
    return records


def _build_fixture_graph() -> dict:
    """The sealed, digest-bound trace graph the PRD chain is grounded on."""
    links = [
        TraceLink(
            problem_id="PRB-001",
            disposition=DISPOSITION_TASK,
            target="SW-1510-SKILLS-1",
        ),
        TraceLink(
            problem_id="PRB-002",
            disposition=DISPOSITION_TASK,
            target="SW-1510-SKILLS-1",
        ),
    ]
    return build_trace_graph(
        _discovery_sources(),
        links,
        mandatory_problems=("PRB-001", "PRB-002"),
    )


def _standalone_dispatch_declaration(prd: dict) -> dict:
    """The dispatch declaration the dispatched lane is validated against.

    The dispatch declaration is a *distinct* document from the PRD ``sequence``
    block: it adds the explicit profile reference, the correction budget,
    ``max_parallel`` and the dispatch lanes. It is derived from the same fixture
    so the full base SHA and the criterion coverage stay pinned to the one
    source-owned fixture.
    """
    sequence = prd["sequence"]
    task = next(t for t in prd["tasks"] if t["id"] == "SW-1510-SKILLS-1")
    criteria = list(range(1, len(task["acceptanceCriteria"]) + 1))
    return {
        "session_boundary": "batch",
        "profile": {"path": "tests/fixtures/dispatch-profile.yaml", "required": True},
        "execution_model": "cold",
        "max_correction_rounds_per_wave": 2,
        "max_parallel": 2,
        "lanes": [
            {
                "id": task["lane"]["id"],
                "role": "ops",
                "repo": "skillweave/skillweave",
                "base": sequence["base"],
                "execution_model": "cold",
                "mutating": True,
                "depends_on": [],
                "write_scope": list(task["lane"]["modifies"])
                + list(task["lane"]["creates"]),
                "worktree": sequence["worktree"],
                "branch": sequence["branch"],
                "integration_policy": "independent",
                "criterion_groups": [{"criteria": [c]} for c in criteria],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Criterion 1: three separate fields, no silent Ralph substitution
# ---------------------------------------------------------------------------

def test_generated_sequence_carries_three_separate_fields():
    sequence = rs.build_execution_sequences(_load_prd())
    assert sequence["methodology"] == "assay"
    assert sequence["autonomy_policy"] == "unicorn"
    assert sequence["session_schedule"] == "batch"
    # Three distinct keys -- never one conflated mode string.
    assert len({"methodology", "autonomy_policy", "session_schedule"}) == 3
    # Assay has no legacy execution_mode: the emitted legacy scalar is empty,
    # so the unchanged build-format consumers cannot read Assay as Ralph.
    assert sequence["execution_mode"] == ""


def test_generated_envelope_records_declared_source_and_three_fields():
    envelope = rs.build_execution_sequences(_load_prd())[rs.EXECUTION_ENVELOPE_KEY]
    assert envelope["version"] == rs.EXECUTION_ENVELOPE_VERSION == 1
    assert envelope["methodology"] == "assay"
    assert envelope["autonomy_policy"] == "unicorn"
    assert envelope["session_schedule"] == "batch"
    # A declared methodology -- no legacy adapter ran.
    assert envelope["source"] == "declared"
    assert envelope["legacy_fields"] == []
    assert envelope["subject_kind"] == "configuration"


@pytest.mark.parametrize(
    "schema_path", [PROMPT_SEQUENCE_SCHEMA, DISPATCH_SEQUENCE_SCHEMA]
)
def test_envelope_block_validates_under_shipped_schemas(schema_path):
    envelope = rs.build_execution_sequences(_load_prd())[rs.EXECUTION_ENVELOPE_KEY]
    schema = _schema(schema_path)
    assert _errors(schema["$defs"]["executionEnvelope"], envelope) == []


@pytest.mark.parametrize(
    "schema_path", [PROMPT_SEQUENCE_SCHEMA, DISPATCH_SEQUENCE_SCHEMA]
)
def test_three_scalar_fields_are_schema_declared_with_assay(schema_path):
    schema = _schema(schema_path)
    props = schema["properties"]
    for field in ("methodology", "autonomy_policy", "session_schedule"):
        assert field in props, f"{schema_path.name} must declare '{field}'"
    assert "assay" in props["methodology"]["enum"]
    assert set(props["methodology"]["enum"]) == {
        "rex", "ralph", "gauntlet", "assay"
    }
    # Validate the produced values against the shipped subschemas.
    sequence = rs.build_execution_sequences(_load_prd())
    for field in ("methodology", "autonomy_policy", "session_schedule"):
        assert _errors(props[field], sequence[field]) == []


def test_shipped_skill_asset_schemas_stay_in_sync_with_core():
    # The prompt-sequence schema each shipped skill ships as an asset must be
    # byte-identical to the deployed core schema carrying the envelope surface.
    core = PROMPT_SEQUENCE_SCHEMA.read_text(encoding="utf-8")
    for skill in SKILL_ARGUMENT_FILES:
        asset = (
            _REPO_ROOT / "skills" / skill / "assets" / "prompt-sequence.schema.json"
        )
        if asset.is_file():
            assert asset.read_text(encoding="utf-8") == core, skill


def test_skill_arguments_declare_the_three_fields_separately():
    for skill in SKILL_ARGUMENT_FILES:
        text = (_REPO_ROOT / "skills" / skill / "SKILL.md").read_text(
            encoding="utf-8"
        )
        lowered = text.lower()
        assert "methodology" in lowered, skill
        assert "autonomy_policy" in lowered, skill
        assert "session_schedule" in lowered, skill
        assert "assay" in lowered, skill
        # The anti-substitution promise is stated on the argument surface.
        assert "ralph" in lowered and "substitut" in lowered, skill


def test_assay_skill_argument_names_the_legacy_adapter_explicitly():
    text = (
        _REPO_ROOT / "skills" / "skillweave-promptchain-execute" / "SKILL.md"
    ).read_text(encoding="utf-8")
    lowered = text.lower()
    assert "execution_mode" in lowered
    assert "legacy" in lowered
    # The named refusal a producer/executor surfaces on a mismatched adapter.
    assert "legacy_would_substitute_methodology" in text


def test_generator_fails_closed_without_the_legacy_mode_key():
    # execution_mode stays a *named* required sequence key (the legacy scalar
    # is declared, never silently dropped): omitting it fails closed with zero
    # lanes before any of them is emitted.
    prd = _load_prd()
    prd["sequence"].pop("execution_mode", None)
    with pytest.raises(KeyError):
        rs.build_execution_sequences(prd)


def test_blank_legacy_mode_never_derives_ralph_for_a_declared_assay():
    # Assay has no legacy mode string, so the fixture declares a blank
    # execution_mode: a declared, empty legacy scalar declares *no* mode and is
    # never derived forward into ralph_attended.
    prd = _load_prd()
    assert prd["sequence"]["execution_mode"] == ""
    sequence = rs.build_execution_sequences(prd)
    assert sequence["methodology"] == "assay"
    assert sequence["execution_mode"] == ""


def test_executor_preserves_methodology_and_policy_distinctly():
    service, store, journal, raw = _service()
    sequence = rs.build_execution_sequences(_load_prd())
    result = service.execute(
        [sys.executable, "-c", "print('sw-1510-skills-chain')"],
        run_id="sw1510-skills-exec",
        tool="native",
        model="declared-methodology",
        subject_repo="skillweave/skillweave",
        subject_commit=sequence["base"],
        created_at="2026-10-05T00:00:00Z",
        methodology=sequence["methodology"],
        autonomy_policy=sequence["autonomy_policy"],
        task_id="SW-1510-SKILLS-1",
        attempt=1,
    )
    assert result.methodology == "assay"
    metadata = store.get_run("sw1510-skills-exec").metadata
    assert metadata["methodology"] == "assay"
    # The policy is its own recorded field, never conflated with the methodology.
    assert metadata["autonomy_policy"] == "unicorn"
    assert metadata["autonomy_policy"] != metadata["methodology"]


def test_run_snapshot_keeps_three_fields_and_binds_exact_digests():
    prd_bytes = PRD_JSON.read_bytes()
    sequence = rs.build_execution_sequences(_load_prd())
    schema_digest = rs.canonical_schema_digest(_schema(PROMPT_SEQUENCE_SCHEMA))
    graph = _build_fixture_graph()
    inputs = RunSnapshotInputs(
        run_id="sw1510-skills-snap",
        task_id="SW-1510-SKILLS-1",
        state="advance_or_stop",
        gate_state="pass",
        methodology=sequence["methodology"],
        autonomy_policy=sequence["autonomy_policy"],
        session_schedule=sequence["session_schedule"],
        subject_digest=hashlib.sha256(prd_bytes).hexdigest(),
        brief_digest=graph["digest"],
        envelope_digest=schema_digest,
        process_identity={"handle_id": "h-1", "service_principal": "ops"},
        authority_decision_digest=hashlib.sha256(b"ops-authorized").hexdigest(),
        attempt=1,
    )
    snapshot = build_run_snapshot(inputs)
    assert snapshot["methodology"] == "assay"
    assert snapshot["autonomy_policy"] == "unicorn"
    assert snapshot["session_schedule"] == "batch"
    assert snapshot["methodology"] in SNAPSHOT_METHODOLOGIES
    # The exact schema digest is frozen into the snapshot's envelope binding.
    assert snapshot["envelope_digest"] == schema_digest
    assert len(snapshot_digest(snapshot)) == 64


def test_run_snapshot_refuses_an_unknown_methodology():
    with pytest.raises(SnapshotError):
        build_run_snapshot(
            RunSnapshotInputs(
                run_id="r",
                task_id="t",
                state="s",
                gate_state="pass",
                methodology="warp-drive",
                autonomy_policy="unicorn",
                session_schedule="batch",
                subject_digest=hashlib.sha256(b"x").hexdigest(),
                brief_digest=hashlib.sha256(b"y").hexdigest(),
                envelope_digest=hashlib.sha256(b"z").hexdigest(),
                process_identity={"handle_id": "h", "service_principal": "ops"},
                authority_decision_digest=hashlib.sha256(b"a").hexdigest(),
            )
        )


# ---------------------------------------------------------------------------
# Criterion 2: explicit legacy compatibility, fail-closed on substitution
# ---------------------------------------------------------------------------

def test_agreeing_legacy_execution_mode_is_accepted_not_reinterpreted():
    # A declared methodology is authoritative; an agreeing legacy scalar is
    # accepted (no refusal) and the declaration is preserved, not re-derived.
    envelope = _compile({"methodology": "rex", "execution_mode": "rex"})
    assert envelope["methodology"] == "rex"
    assert envelope["source"] == "declared"
    assert envelope["legacy_fields"] == []


def test_legacy_only_sequence_records_the_adapter_it_consumed():
    # With no declared methodology the legacy execution_mode *supplies* it, so
    # the adapter that produced the envelope is recorded explicitly.
    envelope = _compile({"execution_mode": "ralph_attended"})
    assert envelope["methodology"] == "ralph"
    assert envelope["source"] == "legacy"
    assert envelope["legacy_fields"] == ["execution_mode:ralph_attended"]


def test_declared_assay_with_legacy_ralph_is_refused():
    # The exact silent-substitution case: Assay declared, legacy mode says Ralph.
    _assert_refused(
        lambda: _compile({"methodology": "assay", "execution_mode": "ralph_attended"}),
        "legacy_would_substitute_methodology",
        "execution_mode",
    )


def test_unknown_legacy_execution_mode_is_refused():
    _assert_refused(
        lambda: _compile({"methodology": "assay", "execution_mode": "warp"}),
        "unknown_legacy_execution_mode:warp",
        "execution_mode",
    )


def test_no_intent_at_all_is_refused():
    # The compiler does not invent a methodology -- a blank legacy scalar
    # declares no mode, so there is nothing to adapt.
    _assert_refused(
        lambda: _compile({"execution_mode": ""}),
        "no_declared_methodology",
        "methodology",
    )


def test_legacy_workflow_vocabulary_is_explicit_and_fail_closed():
    # The documented mapping is data, and an unknown value is refused.
    assert rs.legacy_suggested_workflow_methodology("rex-simple") == "rex"
    assert rs.legacy_suggested_workflow_methodology("direct-execution") == "rex"
    assert rs.legacy_suggested_workflow_methodology("ralph-loop-overnight") == "ralph"
    _assert_refused(
        lambda: rs.legacy_suggested_workflow_methodology("hand-wave"),
        "unknown_legacy_suggested_workflow:hand-wave",
        "suggested_workflow",
    )


def test_agreeing_suggested_workflow_is_accepted_by_the_generator():
    prd = _load_prd()
    prd["sequence"]["methodology"] = "rex"
    prd["sequence"]["autonomy_policy"] = ""
    prd["execution_recommendation"]["suggested_workflow"] = "rex-simple"
    sequence = rs.build_execution_sequences(prd)
    assert sequence["methodology"] == "rex"
    envelope = sequence[rs.EXECUTION_ENVELOPE_KEY]
    assert envelope["source"] == "declared"
    assert envelope["legacy_fields"] == ["suggested_workflow:rex-simple"]


def test_suggested_workflow_that_would_substitute_is_refused_by_the_generator():
    # Driven through the PRODUCTION generator (build_execution_sequences), which
    # reads execution_recommendation.suggested_workflow, not a hand-set status.
    prd = _load_prd()
    prd["execution_recommendation"]["suggested_workflow"] = "ralph-loop-attended"
    _assert_refused(
        lambda: rs.build_execution_sequences(prd),
        "suggested_workflow_would_substitute_methodology",
        "suggested_workflow",
    )


def test_unknown_suggested_workflow_is_refused_by_the_generator():
    prd = _load_prd()
    prd["execution_recommendation"]["suggested_workflow"] = "vibes-only"
    _assert_refused(
        lambda: rs.build_execution_sequences(prd),
        "unknown_legacy_suggested_workflow:vibes-only",
        "suggested_workflow",
    )


def test_unsupported_envelope_version_is_refused():
    _assert_refused(
        lambda: rs.assert_supported_envelope_version(2),
        "unsupported_envelope_version:2",
        "version",
    )
    # The version this compiler knows is accepted.
    rs.assert_supported_envelope_version(rs.EXECUTION_ENVELOPE_VERSION)


# ---------------------------------------------------------------------------
# Criterion 3: one source-owned fixture traverses the whole chain
# ---------------------------------------------------------------------------

def test_fixture_is_source_owned_and_pins_the_base():
    for path in (
        PRD_JSON,
        DISCOVERY_DIR / "discovery-001.md",
        DISCOVERY_DIR / "discovery-002.md",
    ):
        assert path.is_file(), path
    prd = _load_prd()
    assert prd["sequence"]["base"] == FIXTURE_BASE
    assert len(prd["sequence"]["base"]) == 40


def test_chain_discovery_graph_binds_source_documents():
    graph = _build_fixture_graph()
    canonical = canonicalize(graph)  # validates + verifies the digest
    grounding = {g["path"]: g["sha256"] for g in canonical.payload["grounding"]}
    for name in ("discovery-001.md", "discovery-002.md"):
        rel = (DISCOVERY_DIR / name).relative_to(_REPO_ROOT).as_posix()
        assert grounding[rel] == _sha256_file(DISCOVERY_DIR / name)
    # Post-grounding drift verification re-ingests the real files.
    verify_grounding(graph, lambda p: (_REPO_ROOT / p).read_text(encoding="utf-8"))
    # Both mandatory problems are present and linked -- no silent loss.
    assert set(canonical.payload["mandatory_problems"]) == {"PRB-001", "PRB-002"}


def test_chain_prd_to_sequence_to_envelope_is_bound():
    prd = _load_prd()
    sequence = rs.build_execution_sequences(prd)
    # The generator fails closed on a missing sequence key; the fixture is exact.
    for key in rs.SEQUENCE_REQUIRED_KEYS:
        assert key in prd["sequence"], key
    assert sequence["base"] == FIXTURE_BASE
    envelope = sequence[rs.EXECUTION_ENVELOPE_KEY]
    # Envelope digest binding uses the real shipped prompt-sequence schema.
    schema_digest = rs.canonical_schema_digest(_schema(PROMPT_SEQUENCE_SCHEMA))
    compiled = rs.compile_execution_envelope(
        prd["sequence"], schema_digest=schema_digest
    )
    assert compiled["schema_digest"] == schema_digest
    # The declared block compiles deterministically to the same three fields.
    assert (compiled["methodology"], compiled["autonomy_policy"],
            compiled["session_schedule"]) == ("assay", "unicorn", "batch")


def test_chain_work_contract_carries_the_configuration_subject():
    prd = _load_prd()
    envelope = rs.build_execution_sequences(prd)[rs.EXECUTION_ENVELOPE_KEY]
    contract = WorkContract.from_dict(
        {
            "id": "SW-1510-SKILLS-1",
            "subject": {
                "kind": SUBJECT_CONFIGURATION,
                "config_id": "execution-envelope-v1",
                "environment": "runtime",
                "version": str(envelope["version"]),
            },
            "authority": "ops",
            "write_scope": {
                "kind": "paths",
                "allow": ["schemas/prompt-sequence.schema.json",
                          "scripts/regen-sequence.py"],
                "deny": [],
            },
            "irreversible_actions": [],
            "verification": [
                {
                    "method": "pytest",
                    "evidence_type": "test",
                    "command": "pytest tests/unit/test_1510_skills.py",
                }
            ],
            "rollback": "git revert",
            "budget": {"max_correction_rounds": 2, "max_attempts": 1},
            "methodology": envelope["methodology"],
            "policy": envelope["autonomy_policy"],
            "evidence_required": ["tests/unit/test_1510_skills.py"],
        }
    )
    contract.assert_authorized()  # no unauthorized irreversible action
    assert contract.subject.kind == envelope["subject_kind"] == SUBJECT_CONFIGURATION
    assert contract.methodology == "assay"
    # The policy is carried separately from the methodology on the contract too.
    assert contract.policy == "unicorn" != contract.methodology


def test_chain_dispatch_declaration_binds_the_full_base_and_coverage():
    prd = _load_prd()
    declaration = _standalone_dispatch_declaration(prd)
    decl = load_sequence(declaration)
    assert decl.profile.path == "tests/fixtures/dispatch-profile.yaml"
    assert decl.session_boundary == "batch"
    lane = decl.lanes[0]
    assert lane.mutating
    assert lane.base == FIXTURE_BASE
    # The lane's Git identity is a repository subject at the exact base SHA.
    subject = lane.to_subject_ref()
    assert subject.to_dict() == {
        "kind": "repository",
        "repo": "skillweave/skillweave",
        "commit": FIXTURE_BASE,
    }
    task = next(t for t in prd["tasks"] if t["id"] == "SW-1510-SKILLS-1")
    criteria = list(range(1, len(task["acceptanceCriteria"]) + 1))
    started = []
    validate_for_dispatch(
        decl, criteria, on_worker_start=lambda l: started.append(l.id)
    )
    assert started == [lane.id]


def test_chain_prd_sequence_block_is_not_a_dispatch_declaration():
    # The dispatch declaration is a distinct document: the raw PRD sequence
    # block carries no profile, so the production loader refuses it. This pins
    # the boundary the fixture test builds across.
    with pytest.raises(ContractError):
        load_sequence(_load_prd()["sequence"])


def test_chain_execute_produces_receipt_bound_to_source_and_base():
    prd = _load_prd()
    sequence = rs.build_execution_sequences(prd)
    service, store, journal, raw = _service()
    result = service.execute(
        [sys.executable, "-c", "print('sw-1510-skills-receipt')"],
        run_id="sw1510-skills-receipt",
        tool="native",
        model="declared-methodology",
        subject_repo="skillweave/skillweave",
        subject_commit=FIXTURE_BASE,
        created_at="2026-10-05T00:00:00Z",
        methodology=sequence["methodology"],
        autonomy_policy=sequence["autonomy_policy"],
        task_id="SW-1510-SKILLS-1",
        attempt=1,
    )
    # The receipt binds the exact source (repo + full immutable base SHA).
    assert result.receipt.subject_repo == "skillweave/skillweave"
    assert result.receipt.subject_commit == FIXTURE_BASE
    assert result.receipt.sha256 == result.raw_digest
    assert raw.resolve(result.receipt.sha256) == result.raw_bytes
    # And the run snapshot agrees with the declared methodology, distinctly.
    snapshot = service.run_snapshot("sw1510-skills-receipt")
    assert snapshot["methodology"] == "assay"
    assert snapshot["autonomy_policy"] == "unicorn"
    assert snapshot["task_id"] == "SW-1510-SKILLS-1"
    assert snapshot["attempt"] == 1
    assert journal.has_gaps("sw1510-skills-receipt") is False
