"""Discovery trace graph contract (SW-159-BP-TRACE-001).

Blueprint ingests applicable discovery Markdown and emits a **digest-bound
trace graph**: every *used* discovery statement is recorded with its source
path, the digest of the source it came from, the heading it was read under and
a stable problem ID; every problem is linked — through a versioned
:data:`TRACE_LINK_VERSION` contract — to an epic, a task, or a *named*
deferral.

The contract fails closed in four directions:

* **silent loss of a mandatory problem** — a problem declared mandatory that
  no longer appears in the ingested sources, or that is linked to nothing,
  raises :class:`MissingMandatoryProblemError` / :class:`UnlinkedProblemError`;
* **post-grounding digest drift** — :func:`verify_grounding` re-ingests the
  sources and raises :class:`DigestDriftError` when a source or statement
  digest no longer matches the record taken at grounding time, and
  :func:`canonicalize` raises the same error when the graph's own content no
  longer hashes to the digest it carries;
* **undisclosed contradictory discovery statements** — two sources asserting
  the same problem ID with different content are surfaced in ``conflicts`` as
  an unresolved conflict; a graph that drops such a conflict silently instead
  of recording it, or that records a conflict no contradiction backs, raises
  :class:`UnresolvedConflictError`;
* **an unnamed deferral** — a link whose disposition is ``deferred`` must name
  its deferral (name, version, owner, rationale, target) or it raises
  :class:`InvalidDeferralError`.

It emits both a machine-readable mapping (:func:`machine_mapping` /
:func:`to_machine`) and a human-readable rendering (:func:`render_human`)
whose content is identical — :func:`parse_human_mapping` reconstructs exactly
the machine mapping from the human text, and the shipped test asserts the two
agree.

The module is deliberately dependency-light: it imports only the standard
library (``hashlib``, ``json``, ``re``, ``dataclasses``, ``pathlib``,
``typing``) so the contract can be imported and enforced without importing the
full ``skillweave`` runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

#: The only trace-graph document schema version that exists.
SCHEMA_VERSION = 1

#: The versioned TraceLink contract. A future, incompatible link shape bumps
#: this integer; a graph carrying any other value is refused, so an old
#: consumer can never read a new link shape as if it were the one it knows.
TRACE_LINK_VERSION = 1

#: The permitted link dispositions: to an epic, to a task, or a named deferral.
DISPOSITION_EPIC = "epic"
DISPOSITION_TASK = "task"
DISPOSITION_DEFERRED = "deferred"
_DISPOSITIONS = frozenset({DISPOSITION_EPIC, DISPOSITION_TASK, DISPOSITION_DEFERRED})

#: The three permitted deferral-version forms are any non-empty string; the
#: deferral is *named* only when name, version, owner and rationale are all set.

#: Canonical lowercase sha256 hex.
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

#: A stable problem ID: an uppercase prefix, a hyphen, digits (``PRB-001``).
_PROBLEM_ID = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

#: A discovery heading that declares a problem: ``## PRB-001: <title>``.
_HEADING = re.compile(r"^#{2,6}\s+(?P<id>[A-Z][A-Z0-9]*-\d+)\s*:\s*(?P<title>\S.*?)\s*$")

#: Any markdown heading line (statement boundary).
_ANY_HEADING = re.compile(r"^#{1,6}\s")

#: Core keys a graph must carry (``digest`` is derived from these).
_CORE_KEYS = (
    "schema_version",
    "trace_link_version",
    "grounding",
    "sources",
    "links",
    "conflicts",
    "mandatory_problems",
)

#: Every key a graph may carry. Anything else is rejected.
_TOP_LEVEL_KEYS = frozenset(_CORE_KEYS) | {"digest"}

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "discovery-trace.schema.json"


# ── Exception hierarchy ──────────────────────────────────────────────────────


class DiscoveryTraceError(ValueError):
    """A discovery trace graph is missing, malformed or self-contradictory."""


class MissingMandatoryProblemError(DiscoveryTraceError):
    """A mandatory problem was dropped from the ingested sources (silent loss)."""


class UnlinkedProblemError(DiscoveryTraceError):
    """A recorded problem has no link to an epic, a task, or a named deferral."""


class DigestDriftError(DiscoveryTraceError):
    """A source/statement no longer hashes to its grounded digest, or the graph
    does not hash to the digest it carries."""


class UnresolvedConflictError(DiscoveryTraceError):
    """Conflicting discovery statements were not surfaced (or a recorded
    conflict is not backed by an actual contradiction)."""


class InvalidDeferralError(DiscoveryTraceError):
    """A deferred link does not name its deferral completely."""


# ── Canonical hashing ────────────────────────────────────────────────────────


def _sha256_hex(data: Any) -> str:
    if isinstance(data, bytes):
        payload = data
    elif isinstance(data, str):
        payload = data.encode("utf-8")
    else:
        payload = _canonical_json(data).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, ASCII-safe.

    List order is preserved, so the order of ``grounding``, ``sources``,
    ``links``, ``conflicts`` and ``mandatory_problems`` is part of the digested
    identity.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_digest(graph: Mapping[str, Any]) -> str:
    """Return the sha256 digest of ``graph`` with any ``digest`` key excluded."""
    payload = {k: v for k, v in graph.items() if k != "digest"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ── Normalisation helpers ────────────────────────────────────────────────────


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DiscoveryTraceError(f"{label} must be a mapping, got {value!r}")
    return value


def _require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DiscoveryTraceError(f"{label} must be a non-empty string, got {value!r}")
    return value


def _check_unknown_keys(mapping: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted((k for k in mapping if k not in allowed), key=repr)
    if unknown:
        raise DiscoveryTraceError(
            f"{label} carries unknown key(s) {unknown}; only {sorted(allowed)} are allowed"
        )


def _normalize_sha256(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256.match(value):
        raise DiscoveryTraceError(
            f"{field_name} is not a canonical lowercase sha256 hex digest: {value!r}"
        )
    return value


def _normalize_problem_id(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _PROBLEM_ID.match(value):
        raise DiscoveryTraceError(
            f"{field_name} is not a stable problem ID (PREFIX-NNN): {value!r}"
        )
    return value


# ── Input records ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiscoverySource:
    """One used discovery statement, bound to its source and identity.

    ``path`` and ``sha256`` identify the source document the statement was read
    from; ``heading`` is the heading it appeared under; ``problem_id`` is the
    stable identifier the statement asserts; ``statement`` is the exact body
    text (digested, not stored in the sealed graph).
    """

    path: str
    sha256: str
    heading: str
    problem_id: str
    statement: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": _require_nonempty_str(self.path, "source.path"),
            "sha256": _normalize_sha256(self.sha256, field_name="source.sha256"),
            "heading": _require_nonempty_str(self.heading, "source.heading"),
            "problem_id": _normalize_problem_id(self.problem_id, field_name="source.problem_id"),
            "statement_sha256": _sha256_hex(self.statement),
        }


@dataclass(frozen=True)
class Deferral:
    """A *named* deferral: who owns it, why, and against which version/target."""

    name: str
    version: str
    owner: str
    rationale: str
    target: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": _require_nonempty_str(self.name, "deferral.name"),
            "version": _require_nonempty_str(self.version, "deferral.version"),
            "owner": _require_nonempty_str(self.owner, "deferral.owner"),
            "rationale": _require_nonempty_str(self.rationale, "deferral.rationale"),
            "target": _require_nonempty_str(self.target, "deferral.target"),
        }


@dataclass(frozen=True)
class TraceLink:
    """A versioned link from a problem to an epic/task or a named deferral.

    ``trace_link_version`` is carried on every link so a consumer can refuse a
    link shape it does not know rather than misread it.
    """

    problem_id: str
    disposition: str
    target: Optional[str] = None
    deferral: Optional[Deferral] = None
    trace_link_version: int = TRACE_LINK_VERSION

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "problem_id": _normalize_problem_id(self.problem_id, field_name="link.problem_id"),
            "disposition": self.disposition,
            "trace_link_version": self.trace_link_version,
        }
        if self.disposition == DISPOSITION_DEFERRED:
            if self.deferral is None:
                raise InvalidDeferralError(
                    f"link for '{self.problem_id}' is deferred but names no deferral"
                )
            document["deferral"] = self.deferral.to_dict()
        else:
            document["target"] = _require_nonempty_str(
                self.target, f"link.target for '{self.problem_id}'"
            )
        return document


# ── Ingestion ────────────────────────────────────────────────────────────────


def _normalize_statement(lines: Sequence[str]) -> str:
    text = "\n".join(line.rstrip() for line in lines)
    return text.strip("\n").strip()


def parse_discovery_markdown(
    text: str,
    *,
    source_path: str,
    problem_ids: Optional[Iterable[str]] = None,
) -> list[DiscoverySource]:
    """Parse applicable discovery Markdown into used-statement records.

    A statement is declared by a heading of the form ``## PRB-001: <title>``;
    its body runs to the next heading of any level. Each record binds the
    source path, the sha256 of the whole document as read, the heading, the
    stable problem ID and the exact statement text.

    ``problem_ids``, when given, selects *only* those problem IDs (the
    statements the blueprint actually used); any requested ID absent from the
    document is simply not returned — the caller's mandatory-coverage check
    turns that absence into a fail-closed error.
    """
    document_sha = _sha256_hex(text)
    wanted = set(problem_ids) if problem_ids is not None else None
    lines = text.splitlines()
    records: list[DiscoverySource] = []
    current_id: Optional[str] = None
    current_heading = ""
    body: list[str] = []

    def flush() -> None:
        if current_id is None:
            return
        if wanted is not None and current_id not in wanted:
            return
        statement = _normalize_statement(body)
        if not statement:
            return
        records.append(
            DiscoverySource(
                path=source_path,
                sha256=document_sha,
                heading=current_heading,
                problem_id=current_id,
                statement=statement,
            )
        )

    for line in lines:
        match = _HEADING.match(line)
        if match:
            flush()
            current_id = match.group("id")
            current_heading = line.lstrip("#").strip()
            body = []
            continue
        if _ANY_HEADING.match(line):
            flush()
            current_id = None
            current_heading = ""
            body = []
            continue
        if current_id is not None:
            body.append(line)
    flush()
    return records


# ── Normalisation of the graph document ──────────────────────────────────────


def _normalize_grounding(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DiscoveryTraceError(f"grounding must be a list, got {value!r}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, entry in enumerate(value):
        item = _require_mapping(entry, f"grounding[{idx}]")
        _check_unknown_keys(item, frozenset({"path", "sha256"}), f"grounding[{idx}]")
        for required in ("path", "sha256"):
            if required not in item:
                raise DiscoveryTraceError(f"grounding[{idx}] is missing '{required}'")
        path = _require_nonempty_str(item["path"], f"grounding[{idx}].path")
        if path in seen:
            raise DiscoveryTraceError(f"duplicate grounding path: {path!r}")
        seen.add(path)
        normalized.append(
            {
                "path": path,
                "sha256": _normalize_sha256(item["sha256"], field_name=f"grounding[{idx}].sha256"),
            }
        )
    return normalized


def _normalize_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DiscoveryTraceError(f"sources must be a list, got {value!r}")
    if not value:
        raise DiscoveryTraceError("a trace graph must record at least one used discovery statement")
    normalized: list[dict[str, Any]] = []
    for idx, entry in enumerate(value):
        item = _require_mapping(entry, f"sources[{idx}]")
        _check_unknown_keys(
            item,
            frozenset({"path", "sha256", "heading", "problem_id", "statement_sha256"}),
            f"sources[{idx}]",
        )
        for required in ("path", "sha256", "heading", "problem_id", "statement_sha256"):
            if required not in item:
                raise DiscoveryTraceError(f"sources[{idx}] is missing '{required}'")
        normalized.append(
            {
                "path": _require_nonempty_str(item["path"], f"sources[{idx}].path"),
                "sha256": _normalize_sha256(item["sha256"], field_name=f"sources[{idx}].sha256"),
                "heading": _require_nonempty_str(item["heading"], f"sources[{idx}].heading"),
                "problem_id": _normalize_problem_id(
                    item["problem_id"], field_name=f"sources[{idx}].problem_id"
                ),
                "statement_sha256": _normalize_sha256(
                    item["statement_sha256"], field_name=f"sources[{idx}].statement_sha256"
                ),
            }
        )
    return normalized


def _normalize_link(entry: Any, idx: int) -> dict[str, Any]:
    item = _require_mapping(entry, f"links[{idx}]")
    _check_unknown_keys(
        item,
        frozenset({"problem_id", "disposition", "target", "deferral", "trace_link_version"}),
        f"links[{idx}]",
    )
    for required in ("problem_id", "disposition", "trace_link_version"):
        if required not in item:
            raise DiscoveryTraceError(f"links[{idx}] is missing '{required}'")
    version = item["trace_link_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != TRACE_LINK_VERSION:
        raise DiscoveryTraceError(
            f"links[{idx}].trace_link_version must be {TRACE_LINK_VERSION}, got {version!r}"
        )
    disposition = item["disposition"]
    if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
        raise DiscoveryTraceError(
            f"links[{idx}].disposition must be one of {sorted(_DISPOSITIONS)}, got {disposition!r}"
        )
    normalized: dict[str, Any] = {
        "problem_id": _normalize_problem_id(item["problem_id"], field_name=f"links[{idx}].problem_id"),
        "disposition": disposition,
        "trace_link_version": TRACE_LINK_VERSION,
    }
    if disposition == DISPOSITION_DEFERRED:
        if "deferral" not in item:
            raise InvalidDeferralError(
                f"links[{idx}] for '{normalized['problem_id']}' is deferred but names no deferral"
            )
        deferral = _require_mapping(item["deferral"], f"links[{idx}].deferral")
        _check_unknown_keys(
            deferral,
            frozenset({"name", "version", "owner", "rationale", "target"}),
            f"links[{idx}].deferral",
        )
        for required in ("name", "version", "owner", "rationale", "target"):
            if required not in deferral:
                raise InvalidDeferralError(
                    f"links[{idx}].deferral is missing '{required}'"
                )
            _require_nonempty_str(deferral[required], f"links[{idx}].deferral.{required}")
        normalized["deferral"] = {
            key: deferral[key] for key in ("name", "version", "owner", "rationale", "target")
        }
    else:
        if "target" not in item:
            raise UnlinkedProblemError(
                f"links[{idx}] for '{normalized['problem_id']}' has no target"
            )
        normalized["target"] = _require_nonempty_str(item["target"], f"links[{idx}].target")
    return normalized


def _normalize_links(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DiscoveryTraceError(f"links must be a list, got {value!r}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, entry in enumerate(value):
        link = _normalize_link(entry, idx)
        if link["problem_id"] in seen:
            raise DiscoveryTraceError(f"duplicate link for problem {link['problem_id']!r}")
        seen.add(link["problem_id"])
        normalized.append(link)
    return normalized


def _normalize_conflicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DiscoveryTraceError(f"conflicts must be a list, got {value!r}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, entry in enumerate(value):
        item = _require_mapping(entry, f"conflicts[{idx}]")
        _check_unknown_keys(
            item, frozenset({"problem_id", "sources", "resolved"}), f"conflicts[{idx}]"
        )
        for required in ("problem_id", "sources", "resolved"):
            if required not in item:
                raise DiscoveryTraceError(f"conflicts[{idx}] is missing '{required}'")
        problem_id = _normalize_problem_id(
            item["problem_id"], field_name=f"conflicts[{idx}].problem_id"
        )
        if problem_id in seen:
            raise DiscoveryTraceError(f"duplicate conflict for problem {problem_id!r}")
        seen.add(problem_id)
        sources = item["sources"]
        if not isinstance(sources, list) or len(sources) < 2:
            raise DiscoveryTraceError(
                f"conflicts[{idx}].sources must list at least two source paths"
            )
        resolved = item["resolved"]
        if not isinstance(resolved, bool):
            raise DiscoveryTraceError(f"conflicts[{idx}].resolved must be a boolean")
        if resolved:
            raise UnresolvedConflictError(
                f"conflict for '{problem_id}' claims to be resolved: the trace "
                f"surface records conflicting statements, it does not adjudicate them"
            )
        normalized.append(
            {
                "problem_id": problem_id,
                "sources": [_require_nonempty_str(s, f"conflicts[{idx}].sources") for s in sources],
                "resolved": False,
            }
        )
    return normalized


def _normalize_mandatory(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise DiscoveryTraceError(f"mandatory_problems must be a list, got {value!r}")
    normalized: list[str] = []
    seen: set[str] = set()
    for idx, pid in enumerate(value):
        problem_id = _normalize_problem_id(pid, field_name=f"mandatory_problems[{idx}]")
        if problem_id in seen:
            raise DiscoveryTraceError(f"duplicate mandatory problem: {problem_id!r}")
        seen.add(problem_id)
        normalized.append(problem_id)
    return normalized


def _normalize_core(graph: Any) -> dict[str, Any]:
    """Validate and canonicalize every digested field, excluding ``digest``."""
    if not isinstance(graph, Mapping):
        raise DiscoveryTraceError(f"trace graph must be a mapping, got {graph!r}")
    _check_unknown_keys(graph, _TOP_LEVEL_KEYS, "trace graph")
    for required in _CORE_KEYS:
        if required not in graph:
            raise DiscoveryTraceError(f"trace graph is missing '{required}'")

    version = graph["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        raise DiscoveryTraceError(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")

    link_version = graph["trace_link_version"]
    if (
        not isinstance(link_version, int)
        or isinstance(link_version, bool)
        or link_version != TRACE_LINK_VERSION
    ):
        raise DiscoveryTraceError(
            f"trace_link_version must be {TRACE_LINK_VERSION}, got {link_version!r}"
        )

    grounding = _normalize_grounding(graph["grounding"])
    sources = _normalize_sources(graph["sources"])
    links = _normalize_links(graph["links"])
    conflicts = _normalize_conflicts(graph["conflicts"])
    mandatory = _normalize_mandatory(graph["mandatory_problems"])

    _check_graph_consistency(sources, links, conflicts, mandatory, grounding)

    return {
        "schema_version": SCHEMA_VERSION,
        "trace_link_version": TRACE_LINK_VERSION,
        "grounding": grounding,
        "sources": sources,
        "links": links,
        "conflicts": conflicts,
        "mandatory_problems": mandatory,
    }


def _find_conflicts(sources: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Group source records by problem ID, keeping only contradictory groups."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for source in sources:
        grouped.setdefault(source["problem_id"], []).append(source)
    conflicting: dict[str, list[Mapping[str, Any]]] = {}
    for problem_id, group in grouped.items():
        digests = {s["statement_sha256"] for s in group}
        if len(digests) > 1:
            conflicting[problem_id] = group
    return conflicting


def _check_graph_consistency(
    sources: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
    conflicts: Sequence[Mapping[str, Any]],
    mandatory: Sequence[str],
    grounding: Sequence[Mapping[str, Any]],
) -> None:
    """Fail closed on silent loss, missing links and unresolved conflicts."""
    source_ids = {s["problem_id"] for s in sources}
    link_ids = {link["problem_id"] for link in links}
    conflict_ids = {c["problem_id"] for c in conflicts}
    grounded_paths = {g["path"]: g["sha256"] for g in grounding}

    # Every source must be grounded to the document it was read from.
    for source in sources:
        if grounded_paths.get(source["path"]) != source["sha256"]:
            raise DigestDriftError(
                f"source '{source['path']}' for problem '{source['problem_id']}' is not "
                f"grounded to that document digest"
            )

    # Silent loss: a mandatory problem dropped from the sources.
    for problem_id in mandatory:
        if problem_id not in source_ids:
            raise MissingMandatoryProblemError(
                f"mandatory problem {problem_id!r} was dropped: it is recorded as mandatory "
                f"but no used discovery statement declares it"
            )

    # Every recorded problem must be linked to an epic/task or a named deferral.
    for problem_id in sorted(source_ids):
        if problem_id not in link_ids:
            qualifier = "mandatory " if problem_id in mandatory else ""
            raise UnlinkedProblemError(
                f"{qualifier}problem {problem_id!r} has no link to an epic, a task, or a "
                f"named deferral"
            )

    # Every link must point at a recorded problem.
    for link in links:
        if link["problem_id"] not in source_ids:
            raise UnlinkedProblemError(
                f"link for {link['problem_id']!r} references a problem with no used statement"
            )

    # Contradictory sources must be surfaced, and may not be silently dropped:
    # the recorded conflicts are exactly the contradictions the sources imply.
    detected = _find_conflicts(sources)
    for problem_id, group in detected.items():
        if problem_id not in conflict_ids:
            raise UnresolvedConflictError(
                f"conflicting discovery statements for problem {problem_id!r} "
                f"({', '.join(sorted(s['path'] for s in group))}) are not surfaced as a conflict"
            )
    for problem_id in conflict_ids - set(detected):
        raise UnresolvedConflictError(
            f"conflict for problem {problem_id!r} is recorded but no contradiction between "
            f"its sources is present"
        )
    # A recorded conflict must cite exactly the source paths that contradict:
    # a phantom, missing or extra path would misattribute which documents
    # disagree, so the conflict is refused rather than emitted as a wrong
    # citation.
    recorded_paths = {
        c["problem_id"]: set(c["sources"]) for c in conflicts
    }
    for problem_id, group in detected.items():
        expected = {s["path"] for s in group}
        if recorded_paths[problem_id] != expected:
            raise UnresolvedConflictError(
                f"conflict for problem {problem_id!r} cites "
                f"{sorted(recorded_paths[problem_id])} but the contradicting sources are "
                f"{sorted(expected)}"
            )


# ── Building and sealing ─────────────────────────────────────────────────────


def detect_conflicts(sources: Sequence[DiscoverySourceOrDict]) -> list[dict[str, Any]]:
    """Surface contradictory discovery statements as unresolved conflicts."""
    normalized = [
        s.to_dict() if isinstance(s, DiscoverySource) else dict(s) for s in sources
    ]
    conflicts: list[dict[str, Any]] = []
    found = _find_conflicts(normalized)
    for problem_id in sorted(found):
        group = found[problem_id]
        conflicts.append(
            {
                "problem_id": problem_id,
                "sources": sorted({s["path"] for s in group}),
                "resolved": False,
            }
        )
    return conflicts


def build_trace_graph(
    sources: Sequence[DiscoverySourceOrDict],
    links: Sequence[TraceLinkOrDict],
    *,
    mandatory_problems: Sequence[str] = (),
    conflicts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a sealed, digest-bound trace graph from ingested sources and links.

    Grounding is derived from the sources (one entry per distinct source path,
    carrying the document digest read at ingestion). Conflicts are detected
    from contradictory statements unless the caller supplies an explicit set —
    but a supplied set is still checked to match the contradictions present, so
    a contradiction can never be silently omitted.
    """
    source_dicts = [
        s.to_dict() if isinstance(s, DiscoverySource) else dict(s) for s in sources
    ]
    link_dicts = [
        l.to_dict() if isinstance(l, TraceLink) else dict(l) for l in links
    ]

    grounding: dict[str, str] = {}
    for source in source_dicts:
        grounding.setdefault(source["path"], source["sha256"])

    if conflicts is None:
        conflict_dicts = detect_conflicts(source_dicts)
    else:
        conflict_dicts = [dict(c) for c in conflicts]

    core = {
        "schema_version": SCHEMA_VERSION,
        "trace_link_version": TRACE_LINK_VERSION,
        "grounding": [{"path": p, "sha256": d} for p, d in sorted(grounding.items())],
        "sources": source_dicts,
        "links": link_dicts,
        "conflicts": conflict_dicts,
        "mandatory_problems": list(mandatory_problems),
    }
    canonical = _normalize_core(core)
    canonical["digest"] = compute_digest(canonical)
    return canonical


def seal(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``graph`` and return it sealed with a matching digest."""
    canonical = _normalize_core(graph)
    canonical["digest"] = compute_digest(canonical)
    return canonical


@dataclass(frozen=True)
class DiscoveryTraceGraph:
    """The canonicalized, validated, digest-bearing discovery trace graph."""

    payload: Mapping[str, Any]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        document = dict(self.payload)
        document["digest"] = self.digest
        return document

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def canonicalize(graph: Mapping[str, Any]) -> DiscoveryTraceGraph:
    """Validate a sealed trace graph, fail-closed, and verify its digest.

    Raises :class:`DiscoveryTraceError` on any malformed or self-contradictory
    field (or a dropped mandatory problem / unresolved conflict), and
    :class:`DigestDriftError` when the graph does not hash to the digest it
    carries.
    """
    canonical = _normalize_core(graph)

    if "digest" not in graph:
        raise DiscoveryTraceError("trace graph is missing 'digest'")
    provided = _normalize_sha256(graph["digest"], field_name="digest")
    expected = compute_digest(canonical)
    if provided != expected:
        raise DigestDriftError(
            f"digest drift: graph carries {provided} but its content hashes to {expected}"
        )

    return DiscoveryTraceGraph(payload=canonical, digest=expected)


def validate(graph: Mapping[str, Any]) -> DiscoveryTraceGraph:
    """Validate the trace graph (alias of :func:`canonicalize`)."""
    return canonicalize(graph)


# ── Post-grounding drift verification ────────────────────────────────────────


def verify_grounding(
    graph: Mapping[str, Any],
    read_source: Callable[[str], Any],
) -> None:
    """Re-ingest the grounded sources and refuse any post-grounding drift.

    ``read_source(path)`` returns the current bytes/str of a source document.
    For every grounding entry the document digest is recomputed and compared;
    a mismatch raises :class:`DigestDriftError`, naming the path and both
    digests. The per-statement digests are recomputed from the re-ingested
    Markdown as well, so editing a statement without changing the document
    length cannot pass.
    """
    canonical = canonicalize(graph)
    payload = canonical.payload
    recorded = {s["problem_id"]: s for s in payload["sources"]}
    for entry in payload["grounding"]:
        path = entry["path"]
        try:
            raw = read_source(path)
        except Exception as exc:  # noqa: BLE001
            raise DigestDriftError(
                f"grounded source {path!r} can no longer be resolved: {exc}"
            ) from exc
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        current = _sha256_hex(text)
        if current != entry["sha256"]:
            raise DigestDriftError(
                f"post-grounding drift in {path!r}: grounded {entry['sha256'][:12]}, "
                f"now {current[:12]}"
            )
        by_id = {s.problem_id: s for s in parse_discovery_markdown(text, source_path=path)}
        for problem_id, source in recorded.items():
            if source["path"] != path:
                continue
            fresh = by_id.get(problem_id)
            if fresh is None:
                raise DigestDriftError(
                    f"post-grounding drift: problem {problem_id!r} is no longer "
                    f"present in {path!r}"
                )
            if _sha256_hex(fresh.statement) != source["statement_sha256"]:
                raise DigestDriftError(
                    f"post-grounding drift in statement {problem_id!r} of {path!r}"
                )
    # The graph itself must still hash to the digest it carries.
    expected = compute_digest(payload)
    if expected != canonical.digest:
        raise DigestDriftError("trace graph digest no longer matches its content")


# ── Machine- and human-readable mappings ─────────────────────────────────────


def _conflict_entries(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The unresolved-conflict entries of a sealed graph, in stable order."""
    entries: list[dict[str, Any]] = []
    for conflict in graph["conflicts"]:
        entries.append(
            {
                "problem_id": conflict["problem_id"],
                "sources": sorted(conflict["sources"]),
                "resolved": False,
            }
        )
    return sorted(entries, key=lambda c: c["problem_id"])


def machine_mapping(graph: Mapping[str, Any]) -> dict[str, Any]:
    """The machine-readable mapping: problem→link rows plus conflict rows.

    ``links`` maps each problem to its disposition and target in stable
    problem-ID order; ``conflicts`` lists the surfaced contradictions in stable
    order. The human rendering carries exactly this content, so the two are
    interchangeable.
    """
    canonical = canonicalize(graph)
    links: dict[str, dict[str, str]] = {}
    for link in canonical.payload["links"]:
        if link["disposition"] == DISPOSITION_DEFERRED:
            deferral = link["deferral"]
            links[link["problem_id"]] = {
                "disposition": DISPOSITION_DEFERRED,
                "target": f"{deferral['name']}@{deferral['version']}",
            }
        else:
            links[link["problem_id"]] = {
                "disposition": link["disposition"],
                "target": link["target"],
            }
    return {
        "links": {pid: links[pid] for pid in sorted(links)},
        "conflicts": _conflict_entries(canonical.payload),
    }


def to_machine(graph: Mapping[str, Any]) -> str:
    """The machine-readable mapping as canonical JSON."""
    return _canonical_json(machine_mapping(graph))


def render_human(graph: Mapping[str, Any]) -> str:
    """Render the same mapping as deterministic human-readable lines.

    The grammar is strict so a reader (or :func:`parse_human_mapping`) sees
    exactly the machine mapping, including the surfaced conflicts:

    * ``problem PRB-001 -> task TASK-1``
    * ``problem PRB-002 -> epic EPIC-1``
    * ``problem PRB-003 -> deferred hardening@1.3.13``
    * ``conflict PRB-004 unresolved docs/discovery/a.md docs/discovery/b.md``
    """
    canonical = canonicalize(graph)
    mapping = machine_mapping(canonical.to_dict())
    lines: list[str] = []
    for problem_id, entry in mapping["links"].items():
        lines.append(f"problem {problem_id} -> {entry['disposition']} {entry['target']}")
    for conflict in mapping["conflicts"]:
        state = "resolved" if conflict["resolved"] else "unresolved"
        lines.append(f"conflict {conflict['problem_id']} {state} " + " ".join(conflict["sources"]))
    return "\n".join(lines) + ("\n" if lines else "")


_HUMAN_LINE = re.compile(
    r"^problem\s+(?P<id>[A-Z][A-Z0-9]*-\d+)\s+->\s+(?P<kind>epic|task|deferred)\s+(?P<target>\S.*)$"
)
_HUMAN_CONFLICT = re.compile(
    r"^conflict\s+(?P<id>[A-Z][A-Z0-9]*-\d+)\s+(?P<state>resolved|unresolved)(?:\s+(?P<sources>\S.*))?$"
)


def parse_human_mapping(text: str) -> dict[str, Any]:
    """Reconstruct the machine mapping from :func:`render_human` output.

    Returns the identical ``{"links": ..., "conflicts": ...}`` structure
    :func:`machine_mapping` returns, so a test can assert the two renderings
    carry identical content; a human-only extra row would make them diverge.
    """
    links: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, Any]] = []
    for idx, line in enumerate(text.splitlines()):
        if not line:
            continue
        conflict_match = _HUMAN_CONFLICT.match(line)
        if conflict_match:
            sources = (conflict_match.group("sources") or "").split()
            conflicts.append(
                {
                    "problem_id": conflict_match.group("id"),
                    "sources": sorted(sources),
                    "resolved": conflict_match.group("state") == "resolved",
                }
            )
            continue
        match = _HUMAN_LINE.match(line)
        if not match:
            raise DiscoveryTraceError(f"unrecognised human mapping line {idx}: {line!r}")
        problem_id = match.group("id")
        if problem_id in links:
            raise DiscoveryTraceError(f"duplicate human mapping line for {problem_id!r}")
        links[problem_id] = {
            "disposition": match.group("kind"),
            "target": match.group("target"),
        }
    return {
        "links": {pid: links[pid] for pid in sorted(links)},
        "conflicts": sorted(conflicts, key=lambda c: c["problem_id"]),
    }


def load_schema() -> dict[str, Any]:
    """Return the shipped discovery-trace JSON schema, verbatim."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


#: Forward references for the public builder signatures.
DiscoverySourceOrDict = Any
TraceLinkOrDict = Any

__all__ = [
    "SCHEMA_VERSION",
    "TRACE_LINK_VERSION",
    "DISPOSITION_EPIC",
    "DISPOSITION_TASK",
    "DISPOSITION_DEFERRED",
    "DiscoveryTraceError",
    "MissingMandatoryProblemError",
    "UnlinkedProblemError",
    "DigestDriftError",
    "UnresolvedConflictError",
    "InvalidDeferralError",
    "DiscoverySource",
    "Deferral",
    "TraceLink",
    "DiscoveryTraceGraph",
    "parse_discovery_markdown",
    "detect_conflicts",
    "build_trace_graph",
    "compute_digest",
    "seal",
    "canonicalize",
    "validate",
    "verify_grounding",
    "machine_mapping",
    "to_machine",
    "render_human",
    "parse_human_mapping",
    "load_schema",
]
