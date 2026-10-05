"""Verification and completion contract (SW-VERIFY-001, SW-EVIDENCE-QUALITY-001).

A dispatch result's raw output never *self-assesses* as high quality. Evidence
starts ``unassessed`` and can only be promoted to an assessed grade by a
separate verifier whose receipt carries its own provenance (its own
``artifact_id``, producer identity, and a back-reference to the subject
receipt it assessed). A completion contract additionally decides gate state:
exit 0 with empty or wrong output is ``inconclusive`` or ``failed``, never a
gate PASS.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional, Sequence


#: The only quality grade a producer may self-declare. Anything higher is the
#: exclusive output of a separate verifier.
UNASSESSED = "unassessed"


class GateState(str):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass
class CompletionContract:
    """Decide the gate state of one dispatch outcome, fail-closed on emptiness.

    ``exit_code == 0`` with empty output (or output that fails a provided
    check) yields ``inconclusive``, never ``pass``. A non-zero exit or signal
    yields ``fail``. A clean exit with non-empty, check-passing output yields
    ``pass``.
    """

    def evaluate(
        self,
        *,
        exit_code: Optional[int],
        signal: Optional[int],
        termination: str,
        stdout: bytes,
        check_output: Optional[Any] = None,
    ) -> str:
        if termination != "exited" or signal is not None:
            return GateState.FAIL
        if exit_code != 0:
            return GateState.FAIL
        if not stdout or not stdout.strip():
            return GateState.INCONCLUSIVE
        if check_output is not None and not check_output(stdout):
            return GateState.INCONCLUSIVE
        return GateState.PASS


@dataclass
class VerificationResult:
    """A separate verifier's independent assessment of a subject receipt.

    ``subject_artifact_id`` provenance-binds this verdict to the exact receipt
    it assessed; the verifier receipt itself is a distinct artifact bearing the
    verifier's own identity and digest, never the producer's self-claim.
    """

    subject_artifact_id: str
    grade: str
    gate_state: str
    reasons: list[str] = field(default_factory=list)
    verified_by: str = "verifier"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_receipt(self, *, digest: str) -> dict[str, Any]:
        return {
            "artifact_id": f"verify-{self.subject_artifact_id}",
            "sha256": digest,
            "subject_artifact_id": self.subject_artifact_id,
            "grade": self.grade,
            "gate_state": self.gate_state,
            "verified_by": self.verified_by,
            "created_at": self.created_at,
            "reasons": self.reasons,
        }


class Verifier:
    """Assess a subject receipt independently of its producer.

    The verifier decides the grade (``unassessed`` -> ``assessed``/``high`` or a
    negative grade) and the gate state. It never trusts the subject's
    self-declared quality: grade derives from the verifier's own call to the
    completion contract, and the result is a separate, provenance-bound receipt.
    """

    def assess(
        self,
        subject_artifact_id: str,
        *,
        exit_code: Optional[int],
        signal: Optional[int],
        termination: str,
        stdout: bytes,
        check_output: Optional[Any] = None,
    ) -> VerificationResult:
        contract = CompletionContract()
        gate_state = contract.evaluate(
            exit_code=exit_code,
            signal=signal,
            termination=termination,
            stdout=stdout,
            check_output=check_output,
        )
        reasons: list[str] = []
        if gate_state == GateState.PASS:
            grade = "high"
        elif gate_state == GateState.INCONCLUSIVE:
            grade = "inconclusive"
            reasons.append("exit 0 but empty or failing output")
        else:
            grade = "failed"
            if signal is not None:
                reasons.append(f"terminated by signal {signal}")
            elif termination != "exited":
                reasons.append(f"termination={termination}")
            else:
                reasons.append(f"non-zero exit {exit_code}")

        return VerificationResult(
            subject_artifact_id=subject_artifact_id,
            grade=grade,
            gate_state=gate_state,
            reasons=reasons,
        )


def evaluate_empty_state(*, num_runs: int, num_artifacts: int) -> dict[str, Any]:
    """Deterministically grade an empty run/evidence state.

    An empty state (no runs, no artifacts) is a reproducible negative outcome:
    it yields ``inconclusive`` — never a gate PASS — and the same result on
    every call, with no ad-hoc or non-reproducible special status. A
    non-empty state is graded ``insufficient`` until the completion contract
    sees actual evidence.
    """
    if num_runs == 0 and num_artifacts == 0:
        return {
            "gate_state": GateState.INCONCLUSIVE,
            "reason": "empty run/evidence state",
        }
    return {
        "gate_state": GateState.INCONCLUSIVE,
        "reason": "insufficient evidence",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Artifact-specific semantics (SW-1510-VERIFY)
#
# Process completion (a clean exit), artifact integrity (bytes hash to the
# claimed digest) and semantic verification (the artifact actually *analyzed*
# the source it claims) are three separate facts. This section owns the third:
# it refuses an artifact whose proof of analysis is empty, narrative-only,
# README-only over populated sources, an unsupported ``analyzed`` flag, a
# fake/phantom coverage list, an unjustified ``N/A``, or a substituted source
# field -- and never repairs a missing source, timeout or partial graph into a
# PASS.
#
# An artifact *is* an evidence receipt (pinned SDK shape: see
# ``https://skillweave.dev/schemas/evidence/v1``); the analysis proof graph is
# namespaced under ``metadata.artifact_verifier`` so the SDK schema authority
# stays pinned and read-only and this module never re-authors it.
# ═══════════════════════════════════════════════════════════════════════════

#: The namespaced artifact-semantics contract version this verifier accepts.
ARTIFACT_VERIFIER_VERSION = "evidence-receipt-v1"

#: Every version string the artifact-semantics surface accepts. An artifact
#: naming any other version is refused (a new namespaced surface refuses
#: unsupported versions rather than guessing).
SUPPORTED_ARTIFACT_VERSIONS: frozenset[str] = frozenset({
    "1", "v1", "evidence-receipt-v1", "evidence_receipt_v1",
})

#: The SDK evidence schema ``$id`` this verifier validates the artifact shape
#: against when the pinned SDK is importable.
EVIDENCE_SCHEMA_ID = "https://skillweave.dev/schemas/evidence/v1"

#: Dispositions that mean the file was actually analyzed. A disposition
#: outside this set and outside :data:`EXCLUSION_DISPOSITIONS` is unsupported
#: and fails closed.
ANALYZED_DISPOSITIONS: frozenset[str] = frozenset({
    "analyzed", "included", "reviewed", "modified", "inspected",
})

#: Dispositions that remove a file from coverage -- each requires an explicit,
#: bounded, independently-justified exclusion.
EXCLUSION_DISPOSITIONS: frozenset[str] = frozenset({
    "not_applicable", "excluded", "skipped", "ignored",
})

#: Justification strings that are placeholders rather than a justification.
_PLACEHOLDER_JUSTIFICATIONS: frozenset[str] = frozenset({
    "", "n/a", "na", "none", "null", "tbd", "todo", "unknown", "-",
    "not applicable", "no", "nil", "fixme",
})

#: Paths whose *sole* coverage cannot stand in for a populated source tree.
#: A proof that only covers a README/CHANGELOG over a tree with real files is
#: narrative-only by construction.
NARRATIVE_PATH_MARKERS: tuple[str, ...] = (
    "readme", "changelog", "changelog.md", "notice", "license",
    "contributing", "code_of_conduct", "copying", "authors",
)
_NARRATIVE_SUFFIXES: tuple[str, ...] = (".md", ".rst", ".txt", ".adoc", ".markdown")

#: Command prefixes that only *enumerate* files. A coverage entry whose only
#: evidence is a git ls-tree line has not analyzed the file.
_GIT_ENUMERATION_PREFIXES: tuple[str, ...] = (
    "git ls-tree", "git ls-files", "ls-tree", "ls-files",
)

_SHA256_HEX_LEN = 64
FULL_SHA_HEX_LEN = 40


class ArtifactVerificationError(Exception):
    """Base error for artifact-semantics verification."""


class EvidenceVersionRefused(ArtifactVerificationError):
    """The artifact names an unsupported evidence-receipt version."""


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(ch in "0123456789abcdef" for ch in value)
    )


def _looks_like_digest(value: Any) -> bool:
    """A hex digest of a plausible length (not an arbitrary label)."""
    return (
        isinstance(value, str)
        and len(value) >= 32
        and all(ch in "0123456789abcdefABCDEF" for ch in value)
    )


#: Keywords that introduce a *predicate* bounding scope (a family of paths)
#: rather than naming one literal path. They are matched against the excluded
#: path so a predicate cannot be claimed for an unrelated file.
_NARRATIVE_BOUND_KEYWORDS: tuple[str, ...] = (
    "narrative", "documentation", "doc-only", "docs only", "doc only",
)
_GENERATED_BOUND_KEYWORDS: tuple[str, ...] = (
    "generated", "vendored", "third-party", "third party", "third_party",
)
_GENERATED_PATH_MARKERS: tuple[str, ...] = (
    "generated", "vendor", "vendored", "third_party", "third-party",
    "thirdparty", "external",
)


def _is_narrative_path_name(path: str) -> bool:
    lowered = PurePosixPath(path).name.lower()
    if any(marker in lowered for marker in NARRATIVE_PATH_MARKERS):
        return True
    return PurePosixPath(path).suffix.lower() in _NARRATIVE_SUFFIXES


def _bound_covers(bounded: Any, path: str) -> bool:
    """Whether the *bounded* scope actually covers the excluded *path*.

    An exclusion's bound must be resolvable to a matcher the excluded path
    satisfies -- a literal path, a prefix directory, a glob/family, or a
    predicate descriptor (narrative / generated / vendored). A bound that
    merely *names some other file* does not justify excluding this one: a
    false bound is refused (``the claimed bound does not cover this path``),
    so an exclusion cannot be made to pass by writing any non-empty string.
    """
    import fnmatch

    if isinstance(bounded, (list, tuple)):
        return any(_bound_covers(b, path) for b in bounded)
    if not isinstance(bounded, str):
        return False
    bound = bounded.strip()
    if not bound:
        return False
    low = bound.lower()

    # Predicate descriptors: the bound names a *family*; the path must belong.
    if any(k in low for k in _NARRATIVE_BOUND_KEYWORDS):
        return _is_narrative_path_name(path)
    if any(k in low for k in _GENERATED_BOUND_KEYWORDS):
        parts = {part.lower() for part in PurePosixPath(path).parts}
        return bool(parts & set(_GENERATED_PATH_MARKERS)) or any(
            marker in path.lower() for marker in _GENERATED_PATH_MARKERS
        )

    # A literal path, a prefix directory, or a glob family.
    stripped = bound.rstrip("/")
    if path == stripped or path.startswith(stripped + "/"):
        return True
    if fnmatch.fnmatchcase(path, bound) or fnmatch.fnmatchcase(path, bound + "/*"):
        return True
    if "/" not in bound and fnmatch.fnmatchcase(PurePosixPath(path).name, bound):
        return True
    # A bare filename bound (e.g. "README.md") may name the path by basename.
    if "/" not in bound and PurePosixPath(bound).name == PurePosixPath(path).name:
        return True
    return False


def canonical_artifact_bytes(artifact: Any, *, exclude: Sequence[str] = ("sha256",)) -> bytes:
    """Canonical bytes over *artifact* with *exclude* keys removed.

    Sorted keys, compact separators -- so the same semantic artifact always
    hashes to the same digest regardless of key order or whitespace.
    """
    body = {k: v for k, v in artifact.items() if k not in set(exclude)}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def artifact_sha256(artifact: Any) -> str:
    """The self-content digest of an artifact (its ``sha256`` field excluded)."""
    return hashlib.sha256(canonical_artifact_bytes(artifact)).hexdigest()


class SourceResolver:
    """Read-only source access used to bind coverage to real files.

    A resolver reports the exact commit it is bound to, the complete set of
    files under the source root, and the sha256 of a file's content. It never
    writes. Implementations may be filesystem- or VCS-backed.
    """

    def commit(self) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def list_files(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def content_sha256(self, path: str) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError


class FilesystemSourceResolver(SourceResolver):
    """A read-only resolver over a real directory tree.

    ``list_files`` returns every regular file (relative POSIX path) under
    *root*, skipping VCS and cache directories. ``content_sha256`` reads the
    file and hashes its bytes; a missing file yields ``None`` rather than a
    fabricated digest. ``commit`` is the caller-declared pinned commit (a
    filesystem tree has no intrinsic commit), or ``None`` when unpinned.
    """

    _SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache"})

    def __init__(self, root: Any, *, commit: Optional[str] = None) -> None:
        self.root = Path(root).resolve()
        self._commit = commit

    def commit(self) -> Optional[str]:
        return self._commit

    def list_files(self) -> list[str]:
        if not self.root.is_dir():
            return []
        found: list[str] = []
        for entry in sorted(self.root.rglob("*")):
            if not entry.is_file():
                continue
            rel = entry.relative_to(self.root)
            if any(part in self._SKIP_DIRS for part in rel.parts):
                continue
            found.append(rel.as_posix())
        return found

    def content_sha256(self, path: str) -> Optional[str]:
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return hashlib.sha256(candidate.read_bytes()).hexdigest()


class GitSourceResolver(SourceResolver):
    """A read-only resolver over a real git worktree.

    Commit and file list come from read-only git plumbing (``rev-parse``,
    ``ls-tree -r``); ``content_sha256`` reads the working-tree file bytes. It
    is the same enumeration ``git ls-tree`` performs -- and deliberately *not*
    analysis: it supplies the expected file set and content addresses, while
    the coverage entry must still bind an analysis artifact for admission.
    """

    def __init__(self, root: Any, *, commit: Optional[str] = None) -> None:
        self.root = Path(root).resolve()
        self._commit = commit
        self._files: Optional[list[str]] = None

    def _git(self, *args: str) -> Optional[str]:
        import subprocess

        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def commit(self) -> Optional[str]:
        if self._commit is not None:
            return self._commit
        out = self._git("rev-parse", "HEAD")
        return out.strip() if out else None

    def list_files(self) -> list[str]:
        if self._files is None:
            out = self._git("ls-tree", "-r", "--name-only", "HEAD")
            self._files = [
                line for line in (out or "").splitlines() if line.strip()
            ]
        return list(self._files)

    def content_sha256(self, path: str) -> Optional[str]:
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return hashlib.sha256(candidate.read_bytes()).hexdigest()


@dataclass
class ArtifactVerificationResult:
    """The outcome of artifact-semantics verification.

    ``admitted`` is the only field a gate may read to decide PASS; every other
    field is the evidence (or the exact refusal reason) behind it. A refusal is
    never a PASS and is never repaired into one.
    """

    admitted: bool
    gate_state: str
    reasons: list[str] = field(default_factory=list)
    analyzed_files: list[str] = field(default_factory=list)
    exclusions: dict[str, str] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)
    phantom: list[str] = field(default_factory=list)
    artifact_digest: str = ""
    schema_authority: str = ""
    produced: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "gate_state": self.gate_state,
            "reasons": list(self.reasons),
            "analyzed_files": list(self.analyzed_files),
            "exclusions": dict(self.exclusions),
            "uncovered": list(self.uncovered),
            "phantom": list(self.phantom),
            "artifact_digest": self.artifact_digest,
            "schema_authority": self.schema_authority,
            "produced": self.produced,
            "created_at": self.created_at,
        }

    def to_receipt(self, *, digest: str) -> dict[str, Any]:
        """A separate, provenance-bound verifier receipt for this verdict."""
        return {
            "artifact_id": f"artifact-verify-{self.artifact_digest[:16]}",
            "sha256": digest,
            "subject_artifact_id": self.artifact_digest,
            "gate_state": self.gate_state,
            "verified_by": "artifact-evidence-verifier",
            "created_at": self.created_at,
            **self.to_dict(),
        }


def _refuse(reasons: Sequence[str]) -> ArtifactVerificationResult:
    return ArtifactVerificationResult(
        admitted=False,
        gate_state=GateState.FAIL,
        reasons=list(reasons),
        produced=False,
    )


class ArtifactEvidenceVerifier:
    """Decide whether an artifact's proof of analysis is real.

    The verifier is fail-closed on every axis the acceptance criteria name:

    * an **unsupported version**, a missing/mismatched self-digest, or an
      artifact that is not a valid evidence receipt (validated against the
      pinned SDK schema when importable) is refused;
    * an **unsupported ``analyzed`` flag** (not the boolean ``True``) is
      refused -- a narrative string is not a flag;
    * a **source coverage** entry must bind *both* the real file content
      (``content_sha256`` equal to the resolver's bytes) *and* a non-placeholder
      **analysis artifact** (``analysis_digest``), or be an explicit, bounded,
      independently-justified exclusion (``N/A``);
    * a **phantom** covered path (not present in the source), a **substituted**
      commit, a **partial/empty** graph over a populated source, and a
      **README-only** graph over a tree with real files are each refused;
    * a missing resolver, an unresolvable file and a coverage entry whose only
      evidence is a ``git ls-tree`` enumeration are all refused -- **missing
      source never becomes a PASS**.
    """

    def __init__(
        self,
        *,
        expected_commit: Optional[str] = None,
        subject_repo: Optional[str] = None,
        schema_validator: Optional[Callable[[Any], list[str]]] = None,
        declared_files: Optional[Sequence[str]] = None,
        require_commit: bool = False,
    ) -> None:
        self.expected_commit = expected_commit
        self.subject_repo = subject_repo
        self._schema_validator = schema_validator
        self._declared_files = list(declared_files) if declared_files is not None else None
        self.require_commit = require_commit

    # -- schema authority ---------------------------------------------------

    def _validate_shape(self, artifact: Any) -> tuple[bool, list[str], str]:
        """Validate the artifact as an evidence receipt.

        Uses the pinned SDK schema validator when one is available (imported,
        never copied), else the structural contract. Returns
        ``(ok, errors, authority_label)``.
        """
        validator = self._schema_validator
        authority = "injected"
        if validator is None:
            try:
                from skillweave_sdk.validator import validate as sdk_validate

                validator = lambda payload: sdk_validate(payload, EVIDENCE_SCHEMA_ID)  # noqa: E731
                authority = "sdk"
            except Exception:  # noqa: BLE001 - SDK absent: use the structural contract
                validator = None
                authority = "structural"

        if validator is not None:
            try:
                errors = list(validator(artifact))
            except Exception as exc:  # noqa: BLE001 - an unusable authority fails closed
                return False, [f"schema authority could not validate: {exc}"], authority
            return (not errors), [f"evidence schema: {e}" for e in errors], authority

        # Structural fallback: the evidence-receipt shape the SDK schema pins.
        errors = []
        if not isinstance(artifact, dict):
            errors.append("artifact must be a mapping")
        else:
            for key in (
                "artifact_id", "sha256", "schema_version", "producer_command",
                "subject_repo", "subject_commit", "created_at", "evidence_type",
                "purpose",
            ):
                if key not in artifact:
                    errors.append(f"missing required property {key!r}")
            if not _is_hex(artifact.get("sha256"), _SHA256_HEX_LEN):
                errors.append("sha256 is not a 64-hex content digest")
        return (not errors), errors, authority

    # -- proof-graph accessors ---------------------------------------------

    @staticmethod
    def _proof_block(artifact: Any) -> Any:
        if not isinstance(artifact, dict):
            return None
        metadata = artifact.get("metadata")
        if not isinstance(metadata, dict):
            return None
        for key in ("artifact_verifier", "evidence_graph", "artifact_semantics"):
            block = metadata.get(key)
            if isinstance(block, dict):
                return block
        return None

    @staticmethod
    def _coverage_entries(block: Any) -> list[Any]:
        if not isinstance(block, dict):
            return []
        for key in ("coverage", "sources", "analyzed_files", "entries"):
            value = block.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                # A path -> binding mapping: lift to entries carrying ``path``.
                lifted = []
                for path, binding in value.items():
                    if isinstance(binding, dict):
                        lifted.append({"path": path, **binding})
                    else:
                        lifted.append({"path": path, "disposition": binding})
                return lifted
        return []

    # -- the verdict --------------------------------------------------------

    def verify(
        self,
        artifact: Any,
        *,
        resolver: Optional[SourceResolver] = None,
        analysis_reader: Optional[Callable[[str], Optional[bytes]]] = None,
    ) -> ArtifactVerificationResult:
        """Return the artifact-semantics verdict for *artifact*.

        ``resolver`` binds coverage to the real source; ``analysis_reader``
        returns the bytes of the analysis artifact a coverage entry claims, so
        the placeholder ``analysis_digest`` can be independently recomputed.
        Both are required for admission: without them the file/blob/analysis
        evidence cannot be confirmed and the result is a refusal.
        """
        if not isinstance(artifact, dict):
            return _refuse(["artifact is not a mapping"])

        ok, shape_errors, authority = self._validate_shape(artifact)
        if not ok:
            result = _refuse(shape_errors)
            result.schema_authority = authority
            return result

        block = self._proof_block(artifact)
        if block is None:
            result = _refuse(
                [
                    "artifact carries no analysis proof graph "
                    "(metadata.artifact_verifier is empty or missing)"
                ]
            )
            result.schema_authority = authority
            return result

        # Version gate: an unsupported version is refused before any analysis.
        version = block.get("version", block.get("schema_version"))
        if version is not None and str(version) not in SUPPORTED_ARTIFACT_VERSIONS:
            result = _refuse([f"unsupported artifact-evidence version {version!r}"])
            result.schema_authority = authority
            return result

        # Self-digest: the artifact must hash to its own claimed sha256.
        claimed_digest = artifact.get("sha256")
        computed_digest = artifact_sha256(artifact)
        if not _is_hex(claimed_digest, _SHA256_HEX_LEN) or claimed_digest != computed_digest:
            result = _refuse(
                [
                    "artifact sha256 does not match its canonical content "
                    f"(claimed {claimed_digest!r}, computed {computed_digest})"
                ]
            )
            result.schema_authority = authority
            return result

        result = ArtifactVerificationResult(
            admitted=False,
            gate_state=GateState.FAIL,
            artifact_digest=claimed_digest,
            schema_authority=authority,
            produced=True,
        )

        # Source binding: a resolver is mandatory; a missing source is a refusal.
        if resolver is None:
            result.reasons.append(
                "no source resolver: file/blob/analysis evidence cannot be bound"
            )
            return result

        # Commit binding: a substituted commit is refused.
        artifact_commit = artifact.get("subject_commit")
        if artifact_commit is not None:
            resolver_commit = resolver.commit()
            if not _is_hex(artifact_commit, FULL_SHA_HEX_LEN):
                result.reasons.append(
                    f"subject_commit {artifact_commit!r} is not a 40-hex SHA"
                )
                return result
            if resolver_commit is None:
                result.reasons.append(
                    "artifact binds a subject commit but the source reports none"
                )
                return result
            if resolver_commit != artifact_commit:
                result.reasons.append(
                    f"subject_commit {artifact_commit!r} does not match the "
                    f"resolved source commit {resolver_commit!r}"
                )
                return result
        elif self.require_commit:
            result.reasons.append("artifact binds no subject commit")
            return result
        if self.expected_commit is not None and artifact.get("subject_commit") != self.expected_commit:
            result.reasons.append(
                f"artifact subject_commit is not the pinned {self.expected_commit!r}"
            )
            return result

        # The analyzed flag must be the boolean True. A story is not a flag.
        analyzed = block.get("analyzed", block.get("analysis_performed"))
        if analyzed is not True:
            result.reasons.append(
                f"unsupported analyzed flag {analyzed!r}: artifact does not "
                "declare a real analysis"
            )
            return result

        # Expected file set: the declared scope if bounded, else the resolver's.
        expected = (
            list(self._declared_files)
            if self._declared_files is not None
            else resolver.list_files()
        )
        expected_set = set(expected)
        if not expected_set:
            result.reasons.append("source resolves to no files to analyze")
            return result

        covered_paths: set[str] = set()

        # A declared coverage/analysis entry whose ``path`` names a file the
        # source does not contain is phantom coverage -- a missing input. It is
        # recorded and refused *after* every entry is inspected, so the raw
        # diagnostic names the exact file rather than a generic shape refusal.
        def is_phantom(path: Any) -> bool:
            return isinstance(path, str) and bool(path) and path not in expected_set

        # Namespaced exclusions block (bounded, justified) plus per-entry N/A.
        # A block exclusion removes its path from coverage, so the path counts
        # as covered -- otherwise it would be double-reported as uncovered.
        block_exclusions = block.get("exclusions")
        if isinstance(block_exclusions, list):
            for entry in block_exclusions:
                if not isinstance(entry, dict):
                    result.reasons.append("exclusion entry is not a mapping")
                    return result
                path = entry.get("path")
                ok, reason = self._justify_exclusion(
                    entry, path if isinstance(path, str) else ""
                )
                if not ok:
                    result.reasons.append(f"exclusion {path!r}: {reason}")
                    return result
                result.exclusions[str(path)] = reason
                covered_paths.add(str(path))

        entries = self._coverage_entries(block)
        if not entries:
            result.reasons.append(
                "proof graph carries no source coverage: an empty graph over a "
                "populated source is not a PASS"
            )
            return result

        for entry in entries:
            if not isinstance(entry, dict):
                result.reasons.append("coverage entry is not a mapping")
                return result
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                result.reasons.append(f"coverage entry has no path: {entry!r}")
                return result
            # A git ls-tree line is an enumeration, not a path.
            if any(path.startswith(p) for p in _GIT_ENUMERATION_PREFIXES) or "\t" in path:
                result.reasons.append(
                    f"coverage entry {path!r} is a git ls-tree line, not a file "
                    "binding"
                )
                return result
            if is_phantom(path):
                # A declared path the source does not contain: the file does not
                # resolve, so no content/analysis digest can be confirmed. Record
                # it (still validating its justification) and refuse by name.
                disposition = str(entry.get("disposition", "analyzed"))
                if disposition in EXCLUSION_DISPOSITIONS:
                    ok, reason = self._justify_exclusion(entry, path)
                    if not ok:
                        result.reasons.append(f"N/A {path!r}: {reason}")
                        return result
                    result.exclusions[path] = reason
                elif disposition not in ANALYZED_DISPOSITIONS:
                    result.reasons.append(
                        f"unsupported analyzed disposition {disposition!r} for {path!r}"
                    )
                    return result
                covered_paths.add(path)
                continue
            disposition = str(entry.get("disposition", "analyzed"))
            if disposition in EXCLUSION_DISPOSITIONS:
                ok, reason = self._justify_exclusion(entry, path)
                if not ok:
                    result.reasons.append(f"N/A {path!r}: {reason}")
                    return result
                result.exclusions[path] = reason
                covered_paths.add(path)
                continue
            if disposition not in ANALYZED_DISPOSITIONS:
                result.reasons.append(
                    f"unsupported analyzed disposition {disposition!r} for {path!r}"
                )
                return result
            ok, reason = self._verify_analysis_entry(
                path, entry, resolver=resolver, analysis_reader=analysis_reader
            )
            if not ok:
                result.reasons.append(f"{path!r}: {reason}")
                return result
            result.analyzed_files.append(path)
            covered_paths.add(path)

        # Phantom coverage: a covered path that is not in the source.
        result.phantom = sorted(p for p in covered_paths if p not in expected_set)
        # Uncovered: an expected file with neither analysis nor a justified N/A.
        result.uncovered = sorted(p for p in expected_set if p not in covered_paths)

        if result.phantom:
            result.reasons.append(
                "coverage claims files absent from the source: "
                + ", ".join(result.phantom)
            )
            return result
        if result.uncovered:
            result.reasons.append(
                "source coverage leaves "
                f"{len(result.uncovered)} file(s) neither analyzed nor justified: "
                + ", ".join(result.uncovered[:8])
            )
            return result

        # README-only / narrative-only over a populated tree.
        populated = [p for p in expected_set if not self._is_narrative_path(p)]
        if populated:
            analyzed_populated = [p for p in result.analyzed_files if p in set(populated)]
            if not analyzed_populated:
                result.reasons.append(
                    "proof covers only narrative paths over a populated source "
                    "(README-only graph)"
                )
                return result

        result.admitted = True
        result.gate_state = GateState.PASS
        return result

    # -- entry helpers ------------------------------------------------------

    def _verify_analysis_entry(
        self,
        path: str,
        entry: Any,
        *,
        resolver: SourceResolver,
        analysis_reader: Optional[Callable[[str], Optional[bytes]]],
    ) -> tuple[bool, str]:
        """Whether a single analyzed-file entry binds real evidence."""
        content = entry.get("content_sha256", entry.get("sha256"))
        if not _looks_like_digest(content):
            return False, (
                "analysis entry has no file/blob content digest "
                "(content_sha256/sha256)"
            )
        actual = resolver.content_sha256(path)
        if actual is None:
            return False, "source file does not resolve (missing input)"
        if actual != content:
            return False, (
                "declared content_sha256 does not match the resolved source "
                "bytes (substituted source field)"
            )

        analysis_digest = entry.get("analysis_digest", entry.get("analysis_sha256"))
        if not _looks_like_digest(analysis_digest):
            return False, (
                "analysis entry binds no analysis_digest: enumerating files is "
                "not analyzing them"
            )
        if analysis_reader is None:
            return False, "no analysis artifact is available to verify the analysis_digest"
        analysis_bytes = analysis_reader(path)
        if analysis_bytes is None:
            return False, "claimed analysis artifact does not resolve"
        if hashlib.sha256(analysis_bytes).hexdigest() != analysis_digest:
            return False, "analysis_digest does not match the analysis artifact bytes"
        return True, ""

    def _justify_exclusion(self, entry: Any, path: str) -> tuple[bool, str]:
        """Whether an exclusion/N-A entry is explicit, bounded and justified.

        ``path`` is the file being excluded: the claimed *bounded* scope must
        actually cover it. A bound naming some *other* file (a ``README.md``
        bound used to exclude an executable ``settings.py``) is a false bound
        and is refused -- the justification must be consistent with what is
        being excluded, not merely non-empty.
        """
        justification = entry.get("justification", entry.get("reason"))
        bounded = entry.get("bounded", entry.get("scope"))
        if not isinstance(justification, str) or not justification.strip():
            return False, "no justification for excluding this file"
        if justification.strip().lower() in _PLACEHOLDER_JUSTIFICATIONS:
            return False, f"justification {justification!r} is a placeholder"
        if bounded is None:
            return False, "justification is not bounded (no scope/exclusion named)"
        if isinstance(bounded, str) and not bounded.strip():
            return False, "bounding scope is empty"
        if isinstance(bounded, (list, tuple)) and not bounded:
            return False, "bounding scope is empty"
        if not isinstance(bounded, (str, list, tuple)):
            return False, "bounding scope must be a string or list"
        if not _bound_covers(bounded, path):
            return False, (
                f"bounding scope {bounded!r} does not cover the excluded path "
                f"{path!r}"
            )
        return True, justification.strip()

    @staticmethod
    def _is_narrative_path(path: str) -> bool:
        return _is_narrative_path_name(path)


def verify_artifact_evidence(
    artifact: Any,
    *,
    resolver: Optional[SourceResolver] = None,
    analysis_reader: Optional[Callable[[str], Optional[bytes]]] = None,
) -> ArtifactVerificationResult:
    """Convenience wrapper: verify one artifact's analysis proof graph."""
    return ArtifactEvidenceVerifier().verify(
        artifact, resolver=resolver, analysis_reader=analysis_reader
    )
