import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class PreflightError(Exception):
    def __init__(self, reason: str, mismatches: list[dict[str, Any]], code: str = "MISMATCH"):
        self.reason = reason
        self.mismatches = mismatches
        self.code = code
        super().__init__(f"Preflight [{code}]: {reason}")


#: Version of the :class:`PreflightFailure` JSON contract. Consumers must
#: reject unknown versions rather than guess at the shape.
PREFLIGHT_FAILURE_SCHEMA_VERSION = "1.0.0"


class FailureClass(str, Enum):
    """What kind of preflight failure a lane hit.

    Only the first two are auto-repairable from grounding evidence; everything
    else is surfaced for a human decision.
    """

    NONEXISTENT_MODIFIES_PATH = "nonexistent_modifies_path"
    WRONG_LANGUAGE_SOURCE_PATH = "wrong_language_source_path"
    MISSING_FIELD = "missing_field"
    FIELD_MISMATCH = "field_mismatch"
    UNKNOWN = "unknown"


class Retryability(str, Enum):
    """Whether a repair may be attempted without a human in the loop."""

    AUTO_REPAIRABLE = "auto_repairable"
    MANUAL = "manual"
    NON_RETRYABLE = "non_retryable"


#: Capability a repair must hold to apply a given failure class. A repair that
#: needs a capability the acting role lacks is an *authority expansion* and is
#: held, never auto-applied.
AUTO_REPAIR_CAPABILITY = "can_mutate_run_state"


def digest_bytes(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes (the target identity primitive)."""
    return hashlib.sha256(data).hexdigest()


def digest_target(target: Any) -> str:
    """Content-address a preflight target.

    Accepts bytes (hashed directly) or any JSON-serialisable object (hashed
    over its canonical, key-sorted JSON so equal values produce equal digests).
    """
    if isinstance(target, bytes):
        return digest_bytes(target)
    if isinstance(target, str):
        return digest_bytes(target.encode("utf-8"))
    canonical = json.dumps(target, sort_keys=True, separators=(",", ":"), default=str)
    return digest_bytes(canonical.encode("utf-8"))


@dataclass(frozen=True)
class PreflightFailure:
    """Versioned, machine-readable preflight failure.

    Carries everything a bounded repair needs to decide whether it may act and
    what it may touch: the failure ``class``, the ``target_digest`` it was
    observed against, the ``evidence`` that grounds the diagnosis, the
    ``implicated_fields`` a repair is limited to, whether it is ``retryable``,
    and the ``authority_requirement`` needed to apply it.
    """

    failure_class: str
    target_digest: str
    evidence: list[dict[str, Any]]
    implicated_fields: list[str]
    retryability: str
    authority_requirement: str
    detail: str = ""
    schema_version: str = PREFLIGHT_FAILURE_SCHEMA_VERSION
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self):
        if self.schema_version != PREFLIGHT_FAILURE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported PreflightFailure schema_version "
                f"{self.schema_version!r}; expected "
                f"{PREFLIGHT_FAILURE_SCHEMA_VERSION!r}"
            )

    @property
    def fingerprint(self) -> str:
        """Stable identity of this failure, ignoring volatile fields.

        Two occurrences of the same class against the same target with the same
        implicated fields share a fingerprint. This is the repeated-fingerprint
        hold key: seeing it twice means the repair did not stick and the loop
        must stop instead of thrashing.
        """
        canonical = {
            "failure_class": self.failure_class,
            "target_digest": self.target_digest,
            "implicated_fields": sorted(self.implicated_fields),
            "evidence_kinds": sorted(
                str(item.get("kind", "")) for item in self.evidence
            ),
        }
        return digest_target(canonical)

    @property
    def is_auto_repairable(self) -> bool:
        return self.retryability == Retryability.AUTO_REPAIRABLE.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "failure_class": self.failure_class,
            "target_digest": self.target_digest,
            "evidence": self.evidence,
            "implicated_fields": self.implicated_fields,
            "retryability": self.retryability,
            "authority_requirement": self.authority_requirement,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PreflightFailure":
        return cls(
            failure_class=data["failure_class"],
            target_digest=data["target_digest"],
            evidence=list(data.get("evidence", [])),
            implicated_fields=list(data.get("implicated_fields", [])),
            retryability=data["retryability"],
            authority_requirement=data["authority_requirement"],
            detail=data.get("detail", ""),
            schema_version=data.get("schema_version", PREFLIGHT_FAILURE_SCHEMA_VERSION),
            observed_at=data.get(
                "observed_at", datetime.now(timezone.utc).isoformat()
            ),
        )


@dataclass
class SessionEnvelope:
    product: str
    remote_repo: str
    worktree: str
    branch: str
    role: str
    prd_digest: str
    chain_digest: str
    allowed_write_scopes: list[str]
    state_vocabulary: list[str]
    forbidden_transitions: list[str]
    pin_sha: str = ""
    pinned_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "product": self.product,
            "remote_repo": self.remote_repo,
            "worktree": self.worktree,
            "branch": self.branch,
            "role": self.role,
            "prd_digest": self.prd_digest,
            "chain_digest": self.chain_digest,
            "allowed_write_scopes": self.allowed_write_scopes,
            "state_vocabulary": self.state_vocabulary,
            "forbidden_transitions": self.forbidden_transitions,
            "pin_sha": self.pin_sha,
            "pinned_at": self.pinned_at,
        }

    def validate_product(self, expected_product: str) -> bool:
        return self.product == expected_product

    def validate_repo(self, actual_remote: str) -> bool:
        return self.remote_repo == actual_remote

    def validate_write_scope(self, target_path: str) -> bool:
        if not self.allowed_write_scopes:
            return False
        resolved_target = os.path.abspath(target_path)
        for scope in self.allowed_write_scopes:
            resolved_scope = os.path.abspath(scope.replace("**", "").rstrip("/"))
            if resolved_scope == os.sep:
                return True
            if resolved_target.startswith(resolved_scope + os.sep) or resolved_target == resolved_scope:
                return True
        return False

    def is_read_only_operation(self, action: str) -> bool:
        read_only_prefixes = ("get_", "list_", "read_", "search_", "diagnose_", "check_")
        # Mutating verbs always win over any read-looking prefix. In particular
        # ``analyze_and_delete`` (or any ``*_delete``/``*_write``/``*_mutate``)
        # must NEVER be released as read-only merely because it also matches a
        # benign prefix. The name does not decide authorization.
        mutating_markers = ("delete", "write", "mutate", "commit", "push", "merge", "release", "purge", "truncate")
        lowered = action.lower()
        if any(m in lowered for m in mutating_markers):
            return False
        return any(action.startswith(p) for p in read_only_prefixes)

    def mutation_requires_capability(self, action: str) -> bool:
        """Return True when ``action`` is a destructive/mutating operation that
        must fail closed unless an explicit capability grants it.

        ``analyze_and_delete`` is the canonical case: the ``analyze_`` prefix
        looks read-only, but the ``_delete`` suffix is destructive. It is never
        released by name; only an explicit ``allowed_actions`` grant passes.
        """
        lowered = action.lower()
        return "_delete" in lowered or lowered.endswith("delete") or lowered in ("purge", "truncate")



@dataclass
class PreflightResult:
    passed: bool
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    failures: list[PreflightFailure] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        return {
            "passed": self.passed,
            "mismatches": self.mismatches,
            "warnings": self.warnings,
            "failures": [f.to_dict() for f in self.failures],
            "checked_at": self.checked_at,
        }


#: Envelope-mismatch fields that concern identity. These are NEVER repaired
#: automatically: changing the repo, branch, SHA, product, worktree, or role is
#: exactly the authority expansion the repair loop must refuse.
_IDENTITY_FIELDS = frozenset(
    {"product", "remote_repo", "branch", "worktree", "sha", "role"}
)


def classify_failure(
    mismatch: dict[str, Any],
    *,
    target_digest: str,
    evidence: Optional[list[dict[str, Any]]] = None,
) -> PreflightFailure:
    """Turn a preflight mismatch dict into a versioned :class:`PreflightFailure`.

    Identity-field mismatches are ``non_retryable``; a missing required field is
    ``manual``. Grounding-repairable classes are produced by
    :func:`failure_for_missing_path` / :func:`failure_for_wrong_language`
    instead, which carry the evidence a bounded repair consumes.
    """
    field = mismatch.get("field", "")
    if field in _IDENTITY_FIELDS:
        return PreflightFailure(
            failure_class=FailureClass.FIELD_MISMATCH.value,
            target_digest=target_digest,
            evidence=evidence or [
                {
                    "kind": "mismatch",
                    "field": field,
                    "expected": mismatch.get("expected"),
                    "actual": mismatch.get("actual"),
                }
            ],
            implicated_fields=[field],
            retryability=Retryability.NON_RETRYABLE.value,
            authority_requirement=AUTO_REPAIR_CAPABILITY,
            detail=f"identity field {field!r} diverged; not auto-repairable",
        )
    return PreflightFailure(
        failure_class=FailureClass.MISSING_FIELD.value,
        target_digest=target_digest,
        evidence=evidence or [
            {
                "kind": "mismatch",
                "field": field,
                "expected": mismatch.get("expected"),
                "actual": mismatch.get("actual"),
            }
        ],
        implicated_fields=[field],
        retryability=Retryability.MANUAL.value,
        authority_requirement=AUTO_REPAIR_CAPABILITY,
        detail=f"field {field!r} is missing or unusable",
    )


def failure_for_missing_path(
    *, path: str, task_id: str, lane_field: str, target_digest: str,
    evidence: Optional[list[dict[str, Any]]] = None,
) -> PreflightFailure:
    """A ``modifies``/``creates`` path that does not exist in the target."""
    return PreflightFailure(
        failure_class=FailureClass.NONEXISTENT_MODIFIES_PATH.value,
        target_digest=target_digest,
        evidence=evidence or [
            {"kind": "missing_target", "path": path, "task_id": task_id}
        ],
        implicated_fields=[lane_field],
        retryability=Retryability.AUTO_REPAIRABLE.value,
        authority_requirement=AUTO_REPAIR_CAPABILITY,
        detail=f"task {task_id!r} declares missing path {path!r} in {lane_field}",
    )


def failure_for_wrong_language(
    *, path: str, task_id: str, lane_field: str, expected_language: str,
    actual_language: str, target_digest: str,
    evidence: Optional[list[dict[str, Any]]] = None,
) -> PreflightFailure:
    """A source path whose language contradicts the task's declared language."""
    return PreflightFailure(
        failure_class=FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value,
        target_digest=target_digest,
        evidence=evidence or [
            {
                "kind": "language_conflict",
                "path": path,
                "task_id": task_id,
                "expected_language": expected_language,
                "actual_language": actual_language,
            }
        ],
        implicated_fields=[lane_field],
        retryability=Retryability.AUTO_REPAIRABLE.value,
        authority_requirement=AUTO_REPAIR_CAPABILITY,
        detail=(
            f"task {task_id!r} expects {expected_language!r} but path {path!r} "
            f"is {actual_language!r}"
        ),
    )


def run_preflight(
    envelope: SessionEnvelope,
    actual_repo: str,
    actual_branch: str,
    actual_product: Optional[str] = None,
    actual_worktree: Optional[str] = None,
    actual_sha: Optional[str] = None,
    actual_role: Optional[str] = None,
    actual_scope: Optional[str] = None,
    failures: Optional[list[PreflightFailure]] = None,
) -> PreflightResult:
    mismatches = []
    warnings = []
    envelope_digest = digest_target(envelope.to_dict())

    required_string_fields = (
        "product",
        "remote_repo",
        "worktree",
        "branch",
        "role",
        "prd_digest",
        "chain_digest",
    )
    required_list_fields = ("allowed_write_scopes", "state_vocabulary", "forbidden_transitions")

    for field_name in required_string_fields:
        if not getattr(envelope, field_name, None):
            mismatches.append({
                "field": field_name,
                "expected": "non-empty",
                "actual": getattr(envelope, field_name, None),
            })

    for field_name in required_list_fields:
        if getattr(envelope, field_name, None) is None:
            mismatches.append({
                "field": field_name,
                "expected": "list",
                "actual": None,
            })

    if actual_product and not envelope.validate_product(actual_product):
        mismatches.append({
            "field": "product",
            "expected": envelope.product,
            "actual": actual_product,
        })

    if not envelope.validate_repo(actual_repo):
        mismatches.append({
            "field": "remote_repo",
            "expected": envelope.remote_repo,
            "actual": actual_repo,
        })

    if envelope.branch and actual_branch and envelope.branch != actual_branch:
        mismatches.append({
            "field": "branch",
            "expected": envelope.branch,
            "actual": actual_branch,
        })

    # Worktree / SHA / role are compared when the caller supplies the actual
    # value; a mismatch is release-blocking, exactly like repo and branch.
    if actual_worktree is not None and envelope.worktree and actual_worktree != envelope.worktree:
        mismatches.append({
            "field": "worktree",
            "expected": envelope.worktree,
            "actual": actual_worktree,
        })

    if actual_sha is not None and envelope.pin_sha and actual_sha != envelope.pin_sha:
        mismatches.append({
            "field": "sha",
            "expected": envelope.pin_sha,
            "actual": actual_sha,
        })

    if actual_role is not None and envelope.role and actual_role != envelope.role:
        mismatches.append({
            "field": "role",
            "expected": envelope.role,
            "actual": actual_role,
        })

    if actual_scope is not None and not envelope.validate_write_scope(actual_scope):
        mismatches.append({
            "field": "scope",
            "expected": "within allowed_write_scopes",
            "actual": actual_scope,
        })

    if mismatches:
        classified = [
            classify_failure(m, target_digest=envelope_digest) for m in mismatches
        ]
        return PreflightResult(
            passed=False,
            mismatches=mismatches,
            warnings=warnings,
            failures=classified,
        )

    return PreflightResult(passed=True, warnings=warnings, failures=list(failures or []))


InterceptedCallable = Any


class PreflightInterceptor:
    """
    Fail-closed interceptor. Wraps a mutating callable with a preflight
    gate. If preflight fails, the callable is never invoked and
    PreflightError is raised.
    """

    def __init__(self, envelope: SessionEnvelope, repo: str, branch: str, product: Optional[str] = None):
        self._envelope = envelope
        self._repo = repo
        self._branch = branch
        self._product = product
        self._passed = False
        self._result: Optional[PreflightResult] = None

    @property
    def passed(self) -> bool:
        if self._result is None:
            self._result = run_preflight(
                self._envelope,
                actual_repo=self._repo,
                actual_branch=self._branch,
                actual_product=self._product,
            )
            self._passed = self._result.passed
        return self._passed

    @property
    def result(self) -> PreflightResult:
        if self._result is None:
            _ = self.passed
        return self._result

    def guard(self, callable_fn: InterceptedCallable, *args: Any, **kwargs: Any) -> Any:
        if not self.passed:
            raise PreflightError(
                reason=f"Preflight failed: {len(self.result.mismatches)} mismatches",
                mismatches=self.result.mismatches,
                code="INTERCEPTOR_BLOCKED",
            )
        return callable_fn(*args, **kwargs)
