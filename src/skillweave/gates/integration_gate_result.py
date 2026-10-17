"""Cumulative integration-gate result contract (SW-1511A-INTEGRATION-001).

Focused per-file gates missed a packaging surface, a diff-derived test list
silently dropped a file, reviewers mutated their own candidate worktree, and
cross-file process state failed only in the combined run. The integration gate
must therefore be **one cumulative, source-bound decision**, not a collection
of advisory results.

This module owns three concerns:

1. **The cumulative manifest** (:func:`derive_test_manifest`): the test files a
   candidate must run, derived from the *frozen base*, the *candidate diff* and
   the *prior accepted surfaces*. The three sources are unioned, so a change to
   a shared file cannot silently drop the file that a focused, diff-only list
   forgot.

2. **The source-bound result** (:class:`IntegrationGateResult`): source-suite
   and installed-wheel totals, their skip counts, the environment digest and
   the candidate SHA, in the versioned product schema
   ``integration-gate-result/v1`` — replacing the controller-measured interim
   schema.

3. **Read-only review** (:func:`tree_digest`, :func:`disposable_worktree`): a
   digest over the candidate tree proves review did not mutate it; the
   red-proof and destructive checks run inside a disposable worktree that is
   always removed, never in the candidate.

Nothing here launches a process, names a provider/model/harness or reads a
credential: the caller supplies observed facts. A result that omits a required
field, or whose candidate does not match the one under integration, is refused
fail-closed rather than recorded as an undifferentiated pass.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

# ── Contract identity ────────────────────────────────────────────────────────

#: The product schema this module produces, replacing the interim
#: controller-measured ``integration-gate-result.json`` shape.
INTEGRATION_GATE_RESULT_SCHEMA_ID = "integration-gate-result/v1"
INTEGRATION_GATE_RESULT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_INTEGRATION_GATE_RESULT_VERSIONS: Tuple[str, ...] = ("1.0.0",)

#: The unit-suite root the cumulative manifest is drawn from.
UNIT_SUITE_RELATIVE = ("tests", "unit")

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Fields every ``integration-gate-result/v1`` result must carry. A result
#: missing any of them, or claiming another schema, is refused by
#: :func:`verify_result`.
REQUIRED_RESULT_FIELDS: Tuple[str, ...] = (
    "candidate_sha",
    "base_sha",
    "manifest",
    "source_suite",
    "installed_wheel",
    "environment_digest",
    "verdict",
)

#: The two verdicts a result may carry. ``pass`` authorizes final-head
#: integration for the exact candidate; ``refused`` never does.
VERDICT_PASS = "pass"
VERDICT_REFUSED = "refused"

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "integration-gate-result.schema.json"
)


class IntegrationGateError(Exception):
    """An integration-gate result is missing, malformed or not source-bound."""


# ── Suite totals ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SuiteTotals:
    """One suite's observed totals: passes, failures and skips.

    ``total`` is derived, so a caller can never record a total that disagrees
    with the parts. Skips are kept explicit because a skip is exactly the
    selective-ignore signal the cumulative gate exists to catch.
    """

    passed: int = 0
    failed: int = 0
    skipped: int = 0

    def __post_init__(self) -> None:
        for name in ("passed", "failed", "skipped"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise IntegrationGateError(
                    f"suite totals {name} must be a non-negative integer, got {value!r}"
                )

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    def to_dict(self) -> dict[str, int]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "SuiteTotals":
        if not isinstance(mapping, Mapping):
            raise IntegrationGateError(
                f"suite totals must be a mapping, got {mapping!r}"
            )
        passed = int(mapping.get("passed", 0) or 0)
        failed = int(mapping.get("failed", 0) or 0)
        skipped = int(mapping.get("skipped", 0) or 0)
        declared_total = mapping.get("total")
        if declared_total is not None and int(declared_total) != passed + failed + skipped:
            raise IntegrationGateError(
                f"declared total {declared_total} disagrees with the parts "
                f"({passed}+{failed}+{skipped})"
            )
        return cls(passed=passed, failed=failed, skipped=skipped)


# ── Cumulative manifest ──────────────────────────────────────────────────────


def _normalize(relative: str) -> str:
    return str(relative).replace(os.sep, "/").lstrip("./")


def _collect_test_files(root: Path) -> Tuple[str, ...]:
    """Every ``test_*.py`` under ``root/tests``, as POSIX relative paths."""
    tests_dir = Path(root) / "tests"
    if not tests_dir.is_dir():
        return ()
    found = {
        _normalize(str(p.relative_to(root)))
        for p in tests_dir.rglob("test_*.py")
        if p.is_file()
    }
    return tuple(sorted(found))


def _refs_to_tests(ref: str, available: Sequence[str]) -> set[str]:
    """Map one diff/surface reference to the candidate test files it implicates.

    A reference that *is* a test file contributes itself; any other reference
    contributes every test file whose stem shares the reference's stem, so a
    changed module still pulls in its covered test file and no file is dropped
    merely because the diff named a source path.
    """
    normalized = _normalize(ref)
    stem = Path(normalized).stem
    if stem.startswith("test_"):
        return {normalized} if normalized in available else set()
    matches = {
        test
        for test in available
        if Path(test).stem in (stem, f"test_{stem}", f"test_{stem.replace('-', '_')}")
    }
    return matches


def derive_test_manifest(
    *,
    candidate_root: Path | str,
    base_root: Path | str | None = None,
    diff_paths: Iterable[str] = (),
    prior_surfaces: Iterable[str] = (),
) -> Tuple[str, ...]:
    """Derive the cumulative test manifest (criterion 1).

    The manifest is the *union* of three sources, so it is cumulative by
    construction and cannot silently drop a file the way a diff-only list did:

    * the ``test_*.py`` files of the **frozen base** (``base_root``);
    * the test files implicated by the **candidate diff** (``diff_paths``);
    * the test files implicating every **prior accepted surface**
      (``prior_surfaces``).
    """
    candidate = Path(candidate_root)
    available = _collect_test_files(candidate)

    manifest: set[str] = set(available)
    if base_root is not None:
        manifest |= set(_collect_test_files(Path(base_root)))
    for ref in diff_paths:
        manifest |= _refs_to_tests(ref, available)
    for ref in prior_surfaces:
        manifest |= _refs_to_tests(ref, available)

    return tuple(sorted(manifest))


# ── Tree digest (read-only review proof) ─────────────────────────────────────


def tree_digest(
    root: Path | str,
    *,
    exclude: Iterable[str] = (".git", "__pycache__"),
) -> str:
    """A content digest over every file under ``root`` (criterion 2).

    Review is read-only on the candidate. Digesting the candidate tree before
    and after the review and comparing the two digests is the proof: an equal
    digest means the review mutated nothing. Cache directories and VCS
    metadata are excluded, since they are not product surface.
    """
    root_path = Path(root)
    excluded = set(exclude)
    digest = hashlib.sha256()
    if not root_path.exists():
        return digest.hexdigest()
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        if excluded & set(path.relative_to(root_path).parts):
            continue
        digest.update(_normalize(str(path.relative_to(root_path))).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@contextlib.contextmanager
def disposable_worktree(
    *,
    prefix: str = "sw-integration-review-",
    parent: Path | str | None = None,
) -> Iterator[Path]:
    """A disposable worktree for red-proof and destructive checks (criterion 2).

    The directory is created outside the candidate and is *always* removed,
    even when the review raises, so a destructive check can never leave state
    behind or bleed into the candidate it is reviewing.
    """
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent) if parent else None))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── The versioned result ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntegrationGateResult:
    """One ``integration-gate-result/v1`` decision, bound to one candidate.

    ``candidate_sha`` is the single identity every recorded fact binds to:
    a complete-suite result for a *different* SHA can never authorize this
    candidate's integration.
    """

    candidate_sha: str
    base_sha: str
    manifest: Tuple[str, ...]
    source_suite: SuiteTotals
    installed_wheel: SuiteTotals
    environment_digest: str
    verdict: str = VERDICT_REFUSED
    tasks: Tuple[str, ...] = ()
    review_read_only: bool = False
    refusals: Tuple[str, ...] = ()
    schema: str = INTEGRATION_GATE_RESULT_SCHEMA_ID
    schema_version: str = INTEGRATION_GATE_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (("candidate_sha", self.candidate_sha), ("base_sha", self.base_sha)):
            if not isinstance(value, str) or not _FULL_SHA.match(value.strip().lower()):
                raise IntegrationGateError(
                    f"{name} must be a full 40-hex SHA, got {value!r}"
                )
        if self.verdict not in (VERDICT_PASS, VERDICT_REFUSED):
            raise IntegrationGateError(
                f"verdict must be one of {(VERDICT_PASS, VERDICT_REFUSED)}, got {self.verdict!r}"
            )
        if self.schema_version not in SUPPORTED_INTEGRATION_GATE_RESULT_VERSIONS:
            raise IntegrationGateError(
                f"unsupported integration-gate-result version '{self.schema_version}'"
            )
        if self.verdict == VERDICT_PASS and self.refusals:
            raise IntegrationGateError(
                "a passing result cannot carry refusals; a refusal is never a pass"
            )

    @property
    def skipped(self) -> int:
        """Both suites' explicit skip counts, kept separate from passes."""
        return self.source_suite.skipped + self.installed_wheel.skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "candidate_sha": self.candidate_sha,
            "base_sha": self.base_sha,
            "manifest": list(self.manifest),
            "manifest_count": len(self.manifest),
            "source_suite": self.source_suite.to_dict(),
            "installed_wheel": self.installed_wheel.to_dict(),
            "test_totals": {
                "source_suite": self.source_suite.total,
                "installed_wheel": self.installed_wheel.total,
                "combined": self.source_suite.total + self.installed_wheel.total,
            },
            "skips": {
                "source_suite": self.source_suite.skipped,
                "installed_wheel": self.installed_wheel.skipped,
                "combined": self.skipped,
            },
            "environment_digest": self.environment_digest,
            "tasks": list(self.tasks),
            "review_read_only": self.review_read_only,
            "verdict": self.verdict,
            "refusals": list(self.refusals),
        }

    @classmethod
    def from_dict(cls, mapping: Mapping[str, Any]) -> "IntegrationGateResult":
        verify_result(mapping)
        return cls(
            candidate_sha=str(mapping["candidate_sha"]).strip().lower(),
            base_sha=str(mapping["base_sha"]).strip().lower(),
            manifest=tuple(str(m) for m in mapping.get("manifest", ())),
            source_suite=SuiteTotals.from_mapping(mapping["source_suite"]),
            installed_wheel=SuiteTotals.from_mapping(mapping["installed_wheel"]),
            environment_digest=str(mapping["environment_digest"]),
            verdict=str(mapping["verdict"]),
            tasks=tuple(str(t) for t in mapping.get("tasks", ())),
            review_read_only=bool(mapping.get("review_read_only", False)),
            refusals=tuple(str(r) for r in mapping.get("refusals", ())),
            schema=str(mapping.get("schema", INTEGRATION_GATE_RESULT_SCHEMA_ID)),
            schema_version=str(
                mapping.get("schema_version", INTEGRATION_GATE_RESULT_SCHEMA_VERSION)
            ),
        )


def verify_result(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse an incomplete or foreign integration-gate result, fail-closed.

    A result missing any required field, or carrying another ``schema``, raises
    :class:`IntegrationGateError` naming the defect; it never degrades into an
    anonymous pass.
    """
    if not isinstance(mapping, Mapping):
        raise IntegrationGateError(
            f"an {INTEGRATION_GATE_RESULT_SCHEMA_ID} result is a mapping, got "
            f"{type(mapping).__name__}"
        )
    schema = mapping.get("schema")
    if schema != INTEGRATION_GATE_RESULT_SCHEMA_ID:
        raise IntegrationGateError(
            f"result declares schema {schema!r}, expected {INTEGRATION_GATE_RESULT_SCHEMA_ID!r}"
        )
    absent = [f for f in REQUIRED_RESULT_FIELDS if not str(mapping.get(f, "")).strip()]
    if absent:
        raise IntegrationGateError(
            f"result is missing required field(s) {absent}; every "
            f"{INTEGRATION_GATE_RESULT_SCHEMA_ID} result must record {list(REQUIRED_RESULT_FIELDS)}"
        )
    verdict = mapping.get("verdict")
    if verdict not in (VERDICT_PASS, VERDICT_REFUSED):
        raise IntegrationGateError(f"result verdict {verdict!r} is not a known verdict")
    return dict(mapping)


def write_result(path: Path | str, result: IntegrationGateResult) -> Path:
    """Persist one result as canonical JSON; returns the written path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def read_result(path: Path | str) -> IntegrationGateResult:
    """Read and validate a persisted result, fail-closed on a foreign schema."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return IntegrationGateResult.from_dict(payload)


def load_schema() -> dict[str, Any]:
    """Return the shipped ``integration-gate-result/v1`` JSON schema, verbatim."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


__all__ = [
    "INTEGRATION_GATE_RESULT_SCHEMA_ID",
    "INTEGRATION_GATE_RESULT_SCHEMA_VERSION",
    "SUPPORTED_INTEGRATION_GATE_RESULT_VERSIONS",
    "REQUIRED_RESULT_FIELDS",
    "VERDICT_PASS",
    "VERDICT_REFUSED",
    "UNIT_SUITE_RELATIVE",
    "IntegrationGateError",
    "SuiteTotals",
    "IntegrationGateResult",
    "derive_test_manifest",
    "tree_digest",
    "disposable_worktree",
    "verify_result",
    "write_result",
    "read_result",
    "load_schema",
]
