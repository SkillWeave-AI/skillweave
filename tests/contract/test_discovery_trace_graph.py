"""SW-159-BP-TRACE-001 discovery trace graph contract tests.

This suite proves that the discovery trace graph contract
(``skillweave.blueprint.discovery_trace``) and its shipped JSON schema implement
the brief's required result:

1. Every *used* discovery statement is recorded with its source path, the
   digest of the source document, the heading it appeared under and a stable
   problem ID.
2. Problems are linked through the versioned ``TraceLink`` contract to an epic,
   a task, or a **named** deferral; an unnamed deferral is refused.
3. Silent loss of a mandatory problem, a problem with no link, and
   post-grounding digest drift all fail closed.
4. Contradictory discovery statements are surfaced as an unresolved conflict; a
   contradiction that is silently dropped is refused.
5. The machine-readable and human-readable mappings carry identical content, and
   the module's validation agrees with the shipped schema.

The suite is hermetic: no network, no wall clock, no mutation of the working
tree (source re-reads go through an injected in-memory reader).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import jsonschema

from skillweave.blueprint.discovery_trace import (
    DISPOSITION_DEFERRED,
    DISPOSITION_EPIC,
    DISPOSITION_TASK,
    SCHEMA_VERSION,
    TRACE_LINK_VERSION,
    Deferral,
    DigestDriftError,
    DiscoverySource,
    DiscoveryTraceError,
    InvalidDeferralError,
    MissingMandatoryProblemError,
    TraceLink,
    UnlinkedProblemError,
    UnresolvedConflictError,
    build_trace_graph,
    canonicalize,
    compute_digest,
    detect_conflicts,
    load_schema,
    machine_mapping,
    parse_discovery_markdown,
    parse_human_mapping,
    render_human,
    seal,
    to_machine,
    validate,
    verify_grounding,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src"
_MODULE_PATH = _SRC / "skillweave" / "blueprint" / "discovery_trace.py"
_SCHEMA_PATH = _REPO_ROOT / "schemas" / "discovery-trace.schema.json"

_PROBLEMS_MD = """# Discovery notes

## PRB-001: Users cannot find their work
The dashboard has no search and no filters.

## PRB-002: Onboarding is slow
Setup takes four steps and two of them are redundant.
"""

_DISCOVERY_PATH = "docs/discovery/problems.md"


def _source_records(text: str = _PROBLEMS_MD, path: str = _DISCOVERY_PATH):
    return parse_discovery_markdown(text, source_path=path)


def _links():
    return [
        TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1"),
        TraceLink(
            "PRB-002",
            DISPOSITION_DEFERRED,
            deferral=Deferral(
                name="onboarding-simplification",
                version="1.6.0",
                owner="ops",
                rationale="not scheduled for the current epic",
                target="EPIC-9",
            ),
        ),
    ]


def _graph(**overrides):
    """A minimal valid sealed graph, with optional per-call overrides."""
    kwargs = {
        "mandatory_problems": ["PRB-001"],
    }
    kwargs.update(overrides)
    return build_trace_graph(_source_records(), _links(), **kwargs)


def _schema_validator():
    return jsonschema.Draft202012Validator(json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))


def _schema_errors(doc):
    return list(_schema_validator().iter_errors(doc))


# ── Criterion 1: every used statement carries path/digest/heading/problem ID ──


def test_every_used_statement_records_source_path_digest_heading_and_problem_id():
    records = _source_records()
    assert [r.problem_id for r in records] == ["PRB-001", "PRB-002"]
    for record in records:
        assert record.path == _DISCOVERY_PATH
        assert len(record.sha256) == 64
        assert record.heading.startswith(record.problem_id)
        assert record.statement
    # The document digest is shared across the statements read from one document.
    assert len({r.sha256 for r in records}) == 1


def test_selecting_only_used_problem_ids_keeps_the_rest_out():
    records = _source_records()
    selected = parse_discovery_markdown(
        _PROBLEMS_MD, source_path=_DISCOVERY_PATH, problem_ids=["PRB-002"]
    )
    assert [r.problem_id for r in selected] == ["PRB-002"]
    # Statement bodies are digested exactly; whitespace at the edges is trimmed.
    assert records[0].statement == "The dashboard has no search and no filters."


def test_graph_records_path_digest_heading_and_problem_id_for_every_source():
    graph = _graph()
    payload = canonicalize(graph).payload
    for source in payload["sources"]:
        assert source["path"] == _DISCOVERY_PATH
        assert source["heading"].startswith(source["problem_id"])
        assert len(source["sha256"]) == 64
        assert len(source["statement_sha256"]) == 64
    grounded = {g["path"]: g["sha256"] for g in payload["grounding"]}
    assert grounded[_DISCOVERY_PATH] == payload["sources"][0]["sha256"]


# ── Criterion 2: versioned TraceLink to epic/task/named deferral ─────────────


def test_trace_link_is_versioned_on_every_link():
    graph = _graph()
    payload = canonicalize(graph).payload
    assert payload["trace_link_version"] == TRACE_LINK_VERSION
    assert payload["schema_version"] == SCHEMA_VERSION
    for link in payload["links"]:
        assert link["trace_link_version"] == TRACE_LINK_VERSION


def test_link_to_epic_and_task_and_named_deferral_are_representable():
    sources = _source_records()
    sources.append(
        DiscoverySource(
            path=_DISCOVERY_PATH,
            sha256=sources[0].sha256,
            heading="PRB-003: Metrics are absent",
            problem_id="PRB-003",
            statement="There is no usage metric.",
        )
    )
    links = _links() + [TraceLink("PRB-003", DISPOSITION_EPIC, target="EPIC-3")]
    graph = build_trace_graph(sources, links, mandatory_problems=["PRB-001", "PRB-003"])
    by_id = {l["problem_id"]: l for l in canonicalize(graph).payload["links"]}
    assert by_id["PRB-001"]["disposition"] == DISPOSITION_TASK
    assert by_id["PRB-003"]["disposition"] == DISPOSITION_EPIC
    assert by_id["PRB-002"]["disposition"] == DISPOSITION_DEFERRED
    assert by_id["PRB-002"]["deferral"]["version"] == "1.6.0"


def test_unnamed_deferral_is_refused():
    link = TraceLink("PRB-002", DISPOSITION_DEFERRED, deferral=None)
    with pytest.raises(InvalidDeferralError):
        link.to_dict()


def test_incomplete_or_unknown_link_version_is_refused():
    graph = _graph()
    graph["links"][0]["trace_link_version"] = 2
    graph["digest"] = compute_digest(graph)
    with pytest.raises(DiscoveryTraceError):
        canonicalize(graph)
    graph = _graph()
    graph["trace_link_version"] = True
    graph["digest"] = compute_digest(graph)
    with pytest.raises(DiscoveryTraceError):
        canonicalize(graph)


# ── Criterion 3: silent loss and digest drift fail closed ────────────────────


def test_mandatory_problem_missing_from_sources_fails_closed():
    with pytest.raises(MissingMandatoryProblemError):
        build_trace_graph(_source_records(), _links(), mandatory_problems=["PRB-404"])


def test_mandatory_problem_may_not_be_silently_dropped_after_the_fact():
    graph = _graph()
    # Drop the very statement a mandatory problem depends on, then re-digest so
    # only the consistency check — not the digest — can catch it.
    graph["sources"] = [s for s in graph["sources"] if s["problem_id"] != "PRB-001"]
    graph["links"] = [l for l in graph["links"] if l["problem_id"] != "PRB-001"]
    graph["digest"] = compute_digest(graph)
    with pytest.raises(MissingMandatoryProblemError):
        canonicalize(graph)


def test_problem_with_no_link_fails_closed():
    graph = _graph()
    graph["links"] = [l for l in graph["links"] if l["problem_id"] != "PRB-001"]
    graph["digest"] = compute_digest(graph)
    with pytest.raises(UnlinkedProblemError):
        canonicalize(graph)


def test_link_to_an_unrecorded_problem_fails_closed():
    with pytest.raises(UnlinkedProblemError):
        build_trace_graph(
            _source_records(),
            _links() + [TraceLink("PRB-777", DISPOSITION_TASK, target="TASK-9")],
            mandatory_problems=["PRB-001"],
        )


def test_source_not_grounded_to_its_document_digest_fails_closed():
    graph = _graph()
    graph["grounding"][0]["sha256"] = "b" * 64
    graph["digest"] = compute_digest(graph)
    with pytest.raises(DigestDriftError):
        canonicalize(graph)


def test_verify_grounding_passes_when_sources_are_unchanged():
    graph = _graph()
    verify_grounding(graph, lambda path: _PROBLEMS_MD)


def test_verify_grounding_detects_post_grounding_document_drift():
    graph = _graph()
    drifted = _PROBLEMS_MD.replace("no search", "a working search")
    with pytest.raises(DigestDriftError) as exc:
        verify_grounding(graph, lambda path: drifted)
    assert "drift" in str(exc.value)


def test_verify_grounding_detects_statement_drift_within_a_grounded_document():
    graph = _graph()
    # A same-length edit keeps the document digest stable only if the document
    # digest is spoofed; re-ingestion of the statement still catches it.
    edited = _PROBLEMS_MD.replace("no filters", "no zoomers")
    edited = edited.ljust(len(_PROBLEMS_MD))[: len(_PROBLEMS_MD)]
    with pytest.raises(DigestDriftError):
        verify_grounding(graph, lambda path: edited)


def test_verify_grounding_refuses_an_unresolvable_source():
    graph = _graph()

    def missing(path):
        raise FileNotFoundError(path)

    with pytest.raises(DigestDriftError):
        verify_grounding(graph, missing)


def test_post_hoc_edit_of_a_digested_field_is_detected():
    graph = _graph()
    graph["sources"][0]["heading"] = "PRB-001: Tampered heading"
    with pytest.raises(DigestDriftError):
        canonicalize(graph)


# ── Criterion 4: conflicting statements surface as unresolved conflicts ──────


def _contradictory_pair():
    """Two sources asserting PRB-001 with different statements, from two paths."""
    a = DiscoverySource(_DISCOVERY_PATH, "a" * 64, "PRB-001: A", "PRB-001", "statement one")
    b = DiscoverySource("other.md", "b" * 64, "PRB-001: B", "PRB-001", "statement two")
    return a, b


def test_conflicting_statements_are_surfaced_as_unresolved():
    a, b = _contradictory_pair()
    graph = build_trace_graph(
        [a, b], [TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1")]
    )
    conflicts = canonicalize(graph).payload["conflicts"]
    assert conflicts == [
        {"problem_id": "PRB-001", "sources": [_DISCOVERY_PATH, "other.md"], "resolved": False}
    ]


def test_conflict_detection_reports_both_contributing_sources():
    a, b = _contradictory_pair()
    conflicts = detect_conflicts([a, b])
    assert len(conflicts) == 1
    assert conflicts[0]["problem_id"] == "PRB-001"
    assert conflicts[0]["resolved"] is False
    assert conflicts[0]["sources"] == [_DISCOVERY_PATH, "other.md"]


def test_silently_dropping_a_detected_conflict_fails_closed():
    # Build a graph whose sources contradict, then re-validate it with the
    # conflict quietly removed: the source evidence still contradicts, so the
    # undisclosed conflict is refused even though the digest is recomputed.
    a, b = _contradictory_pair()
    core = {
        "schema_version": SCHEMA_VERSION,
        "trace_link_version": TRACE_LINK_VERSION,
        "grounding": [
            {"path": _DISCOVERY_PATH, "sha256": "a" * 64},
            {"path": "other.md", "sha256": "b" * 64},
        ],
        "sources": [a.to_dict(), b.to_dict()],
        "links": [TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1").to_dict()],
        "conflicts": [],
        "mandatory_problems": [],
    }
    core["digest"] = compute_digest(core)
    with pytest.raises(UnresolvedConflictError):
        canonicalize(core)


def test_recording_a_conflict_with_no_contradiction_fails_closed():
    graph = _graph()
    graph["conflicts"] = [
        {"problem_id": "PRB-001", "sources": ["a.md", "b.md"], "resolved": False}
    ]
    graph["digest"] = compute_digest(graph)
    with pytest.raises(UnresolvedConflictError):
        canonicalize(graph)


def test_conflict_citing_a_phantom_source_path_fails_closed():
    # The conflict is real (both sources contradict for PRB-001), but the
    # recorded citation names a path that contributes nothing: the emitted
    # mapping would misattribute which documents disagree.
    a, b = _contradictory_pair()
    core = {
        "schema_version": SCHEMA_VERSION,
        "trace_link_version": TRACE_LINK_VERSION,
        "grounding": [
            {"path": _DISCOVERY_PATH, "sha256": "a" * 64},
            {"path": "other.md", "sha256": "b" * 64},
        ],
        "sources": [a.to_dict(), b.to_dict()],
        "links": [TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1").to_dict()],
        "conflicts": [
            {"problem_id": "PRB-001", "sources": ["other.md", "zzz.md"], "resolved": False}
        ],
        "mandatory_problems": [],
    }
    core["digest"] = compute_digest(core)
    with pytest.raises(UnresolvedConflictError):
        canonicalize(core)


def test_a_resolved_conflict_claim_is_refused():
    a, b = _contradictory_pair()
    core = {
        "schema_version": SCHEMA_VERSION,
        "trace_link_version": TRACE_LINK_VERSION,
        "grounding": [
            {"path": _DISCOVERY_PATH, "sha256": "a" * 64},
            {"path": "other.md", "sha256": "b" * 64},
        ],
        "sources": [a.to_dict(), b.to_dict()],
        "links": [TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1").to_dict()],
        "conflicts": [
            {"problem_id": "PRB-001", "sources": ["other.md", _DISCOVERY_PATH], "resolved": True}
        ],
        "mandatory_problems": [],
    }
    core["digest"] = compute_digest(core)
    with pytest.raises(UnresolvedConflictError):
        canonicalize(core)


def test_non_contradictory_sources_do_not_create_a_conflict():
    graph = _graph()
    assert canonicalize(graph).payload["conflicts"] == []


# ── Criterion 5: machine and human mappings carry identical content ──────────


def test_machine_and_human_mappings_are_identical():
    graph = _graph()
    parsed = parse_human_mapping(render_human(graph))
    assert parsed == machine_mapping(graph)


def test_machine_and_human_mappings_are_identical_with_a_conflict():
    a, b = _contradictory_pair()
    graph = build_trace_graph(
        [a, b], [TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1")]
    )
    assert parse_human_mapping(render_human(graph)) == machine_mapping(graph)


def test_human_mapping_matches_machine_json():
    graph = _graph()
    assert json.loads(to_machine(graph)) == parse_human_mapping(render_human(graph))


def test_human_mapping_states_each_disposition_and_target():
    lines = render_human(_graph()).splitlines()
    assert "problem PRB-001 -> task TASK-1" in lines
    assert "problem PRB-002 -> deferred onboarding-simplification@1.6.0" in lines


def test_human_mapping_renders_an_unresolved_conflict_line():
    a, b = _contradictory_pair()
    graph = build_trace_graph(
        [a, b], [TraceLink("PRB-001", DISPOSITION_TASK, target="TASK-1")]
    )
    lines = render_human(graph).splitlines()
    assert f"conflict PRB-001 unresolved {_DISCOVERY_PATH} other.md" in lines


def test_render_human_round_trips_through_parse_human_mapping():
    graph = _graph()
    text = render_human(graph)
    assert parse_human_mapping(text) == machine_mapping(graph)
    # Rendering is deterministic and idempotent over the sealed graph.
    assert render_human(canonicalize(graph).to_dict()) == text


# ── Sealing, determinism and schema agreement ────────────────────────────────


def test_seal_is_idempotent_and_round_trips():
    first = seal(_graph())
    second = seal(first)
    assert first == second
    assert canonicalize(first).to_dict() == first


def test_validate_is_canonicalize():
    assert validate(_graph()) == canonicalize(_graph())


def test_graph_digest_is_order_sensitive_and_content_bound():
    graph = _graph()
    assert graph["digest"] == compute_digest(graph)
    reordered = _graph()
    reordered["sources"] = list(reversed(reordered["sources"]))
    assert compute_digest(reordered) != graph["digest"]


def test_digest_is_recomputed_from_payload_excluding_itself():
    graph = _graph()
    assert compute_digest(graph) == compute_digest(
        {k: v for k, v in graph.items() if k != "digest"}
    )


def test_schema_required_fields_and_shape():
    schema = load_schema()
    assert schema["$id"].endswith("discovery-trace/v1")
    assert set(schema["required"]) == {
        "schema_version",
        "trace_link_version",
        "grounding",
        "sources",
        "links",
        "conflicts",
        "mandatory_problems",
        "digest",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["links"]["items"]["properties"]["disposition"]["enum"] == [
        "epic",
        "task",
        "deferred",
    ]
    assert (
        schema["properties"]["sources"]["items"]["properties"]["problem_id"]["pattern"]
        == "^[A-Z][A-Z0-9]*-[0-9]+$"
    )
    assert schema["properties"]["digest"]["pattern"] == "^[a-f0-9]{64}$"


def test_schema_validates_sealed_graph_and_rejects_unknown_key():
    assert _schema_errors(_graph()) == []
    bad = _graph()
    bad["smuggled"] = True
    assert _schema_errors(bad) != []


def test_schema_rejects_deferred_link_without_a_deferral():
    bad = _graph()
    bad["links"][1].pop("deferral")
    assert _schema_errors(bad) != []


def test_schema_rejects_conflict_with_a_single_source():
    bad = _graph()
    bad["conflicts"] = [{"problem_id": "PRB-001", "sources": ["a.md"], "resolved": False}]
    assert _schema_errors(bad) != []


def test_module_and_schema_agree_on_rejected_shapes():
    cases = []
    bad = _graph()
    bad["links"][0]["disposition"] = "sprint"
    cases.append(bad)
    bad = _graph()
    bad["sources"][0]["statement_sha256"] = "Z" * 64
    cases.append(bad)
    bad = _graph()
    bad["mandatory_problems"] = ["prb-001"]
    cases.append(bad)
    for case in cases:
        with pytest.raises(DiscoveryTraceError):
            canonicalize(case)
        assert _schema_errors(case) != [], case


# ── Fail-closed validation of malformed shapes ───────────────────────────────


@pytest.mark.parametrize(
    "mutate,label",
    [
        (lambda g: g.pop("sources"), "missing_sources"),
        (lambda g: g.update(schema_version=2), "wrong_schema_version"),
        (lambda g: g.update(schema_version=True), "boolean_schema_version"),
        (lambda g: g.update(trace_link_version=2), "wrong_trace_link_version"),
        (lambda g: g["sources"][0].update(problem_id="lower-1"), "malformed_problem_id"),
        (lambda g: g["sources"][0].update(sha256="nothex"), "malformed_source_digest"),
        (lambda g: g["sources"][0].update(sha256="A" * 64), "mixed_case_source_digest"),
        (lambda g: g["sources"][0].pop("heading"), "missing_heading"),
        (lambda g: g["links"][0].update(disposition="sprint"), "bad_disposition"),
        (lambda g: g["links"][1]["deferral"].pop("owner"), "deferral_without_owner"),
        (lambda g: g["grounding"][0].update(sha256="nothex"), "malformed_grounding_digest"),
        (lambda g: g.update(extra="x"), "unknown_top_level_key"),
    ],
)
def test_named_negative_fixture_fails_closed(mutate, label):
    bad = _graph()
    mutate(bad)
    with pytest.raises(DiscoveryTraceError):
        seal(bad)


def test_non_mapping_graph_fails_closed():
    with pytest.raises(DiscoveryTraceError):
        seal([1, 2, 3])


def test_graph_with_no_sources_fails_closed():
    bad = _graph()
    bad["sources"] = []
    with pytest.raises(DiscoveryTraceError):
        seal(bad)


def test_duplicate_link_for_a_problem_fails_closed():
    bad = _graph()
    bad["links"].append(dict(bad["links"][0]))
    with pytest.raises(DiscoveryTraceError):
        seal(bad)


def test_duplicate_grounding_path_fails_closed():
    bad = _graph()
    bad["grounding"].append(dict(bad["grounding"][0]))
    with pytest.raises(DiscoveryTraceError):
        seal(bad)


def test_missing_digest_is_an_error_not_a_tamper():
    bad = _graph()
    bad.pop("digest")
    with pytest.raises(DiscoveryTraceError):
        canonicalize(bad)


def test_post_hoc_mutation_of_the_digest_is_detected():
    bad = _graph()
    bad["digest"] = "b" * 64
    with pytest.raises(DigestDriftError):
        canonicalize(bad)


# ── Import hygiene: standard library only, no runtime required ───────────────


def test_module_import_closure_is_standard_library_only():
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    non_stdlib = sorted(imported - set(sys.stdlib_module_names) - {"__future__"})
    assert non_stdlib == [], f"contract module imports non-stdlib modules: {non_stdlib}"


def test_module_loads_in_isolation_without_skillweave_runtime():
    probe = f'''
import importlib.util as u, json, sys
before = set(sys.modules)
spec = u.spec_from_file_location("skillweave_discovery_trace", {str(_MODULE_PATH)!r})
module = u.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
md = {json.dumps(_PROBLEMS_MD)}
records = module.parse_discovery_markdown(md, source_path={json.dumps(_DISCOVERY_PATH)})
links = [
    module.TraceLink("PRB-001", module.DISPOSITION_TASK, target="TASK-1"),
    module.TraceLink("PRB-002", module.DISPOSITION_EPIC, target="EPIC-2"),
]
graph = module.build_trace_graph(records, links, mandatory_problems=["PRB-001"])
checked = module.canonicalize(graph)
newly_loaded = sorted(set(sys.modules) - before)
third_party = [n for n in newly_loaded if n.split(".")[0] not in sys.stdlib_module_names
               and not n.startswith("skillweave_discovery_trace")]
print(json.dumps({{"third_party": third_party,
                  "skillweave_loaded": any(n == "skillweave" or n.startswith("skillweave.") for n in newly_loaded),
                  "links": len(checked.payload["links"]),
                  "digest_len": len(graph["digest"])}}))
'''
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_SRC)},
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["third_party"] == [], report["third_party"]
    assert report["skillweave_loaded"] is False
    assert report["links"] == 2
    assert report["digest_len"] == 64
