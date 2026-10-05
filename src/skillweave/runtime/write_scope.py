"""Write-scope arbitration: claim and release, not just validate.

``preflight.validate_write_scope`` checks the write scope of a SINGLE envelope.
It does not arbitrate between two lanes that want the same paths. Two workers
can therefore be let loose on the same files even though each was validated
correctly on its own.

This module turns validation into a locking primitive: a run can CLAIM a write
scope before touching it and RELEASE it when done. A second claim on an
overlapping scope is rejected while the first is held. Overlap is determined
over resolved absolute paths (the same resolution ``004`` uses), never over raw
string prefixes.

It deliberately does NOT schedule: no dependency resolution, no batch building,
no ordering. Claim, conflict, release — nothing else. Ordering is 010's job.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from typing import Any, Mapping, Optional, Sequence


class ScopeConflictError(Exception):
    """Raised when a claim overlaps an already-held write scope."""

    def __init__(self, run_id: str, holder_run_id: str, overlapping_path: str):
        self.run_id = run_id
        self.holder_run_id = holder_run_id
        self.overlapping_path = overlapping_path
        super().__init__(
            f"Write-scope conflict for run '{run_id}': path "
            f"'{overlapping_path}' is already held by run '{holder_run_id}'"
        )


def resolve_scope_path(raw_path: str) -> str:
    """Resolve a single scope string to an absolute directory path.

    Mirrors ``SessionEnvelope.validate_write_scope``: a trailing ``**`` marks a
    recursive scope and is stripped; everything else is resolved with
    ``os.path.abspath`` (lexical resolution, not ``realpath`` — same as 004).
    The root ``/`` is represented by ``os.sep``.
    """
    cleaned = raw_path.replace("**", "").rstrip("/")
    if cleaned == "":
        return os.sep
    return os.path.abspath(cleaned)


def paths_overlap(resolved_a: str, resolved_b: str) -> bool:
    """Return True when two resolved scope paths overlap.

    Two paths overlap when one is equal to the other or one is an ancestor of
    the other (with a separator boundary, so ``/a/foobar`` and ``/a/foo`` do not
    overlap). The filesystem root overlaps everything below it.
    """
    if resolved_a == os.sep or resolved_b == os.sep:
        return True
    if resolved_a == resolved_b:
        return True
    if resolved_a.startswith(resolved_b + os.sep):
        return True
    if resolved_b.startswith(resolved_a + os.sep):
        return True
    return False


@dataclass
class WriteScopeClaim:
    claim_id: str
    run_id: str
    resolved_path: str
    created_at: str
    released_at: Optional[str] = None
    lease_until: Optional[str] = None
    heartbeat_at: Optional[str] = None

    @property
    def held(self) -> bool:
        return self.released_at is None

    def is_expired(self, now: str) -> bool:
        """A held claim is expired when its lease has a deadline in the past."""
        if self.released_at is not None or self.lease_until is None:
            return False
        return now >= self.lease_until

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "run_id": self.run_id,
            "resolved_path": self.resolved_path,
            "created_at": self.created_at,
            "released_at": self.released_at,
            "lease_until": self.lease_until,
            "heartbeat_at": self.heartbeat_at,
        }


def default_lease_until(now: str, ttl_seconds: int = 300) -> str:
    """Compute a lease deadline ``ttl_seconds`` after ``now`` (ISO timestamps)."""
    from datetime import datetime, timedelta
    dt = datetime.fromisoformat(now)
    return (dt + timedelta(seconds=ttl_seconds)).isoformat()


class WriteSetConflictError(Exception):
    """Raised when a worker's declared write-set overlaps another worker's.

    The conflict is detected BEFORE the worker starts, so overlapping scopes
    block startup rather than being discovered mid-flight."""
    def __init__(self, worker_id: str, conflicting_worker: str, overlapping_path: str):
        self.worker_id = worker_id
        self.conflicting_worker = conflicting_worker
        self.overlapping_path = overlapping_path
        super().__init__(
            f"write-set conflict: worker '{worker_id}' path '{overlapping_path}' "
            f"overlaps worker '{conflicting_worker}'"
        )


class WriteSetManager:
    """Declared write-sets with a conflict matrix, checked before worker start.

    A worker declares the paths it will write. If any declared path overlaps a
    path already held by a different in-flight worker, startup is blocked with
    a :class:`WriteSetConflictError`. This is the pre-start write-set lock:
    nothing runs with an overlapping scope, instead of failing mid-flight.
    """

    def __init__(self):
        self._declared: dict[str, list[str]] = {}

    def declare(self, worker_id: str, scope_paths: list[str]) -> None:
        resolved = [resolve_scope_path(p) for p in scope_paths]
        for other_id, other_paths in self._declared.items():
            if other_id == worker_id:
                continue
            for other in other_paths:
                for new_path in resolved:
                    if paths_overlap(new_path, other):
                        raise WriteSetConflictError(worker_id, other_id, new_path)
        # MERGE into the worker's existing set: a second declaration must not
        # silently drop previously declared paths. Union, never replace.
        existing = self._declared.get(worker_id, [])
        self._declared[worker_id] = list(dict.fromkeys(existing + resolved))

    def release(self, worker_id: str) -> None:
        self._declared.pop(worker_id, None)

    def conflicts_with(self, worker_id: str, scope_paths: list[str]) -> list[str]:
        """Return the worker ids that would conflict, without mutating state."""
        resolved = [resolve_scope_path(p) for p in scope_paths]
        conflicts = []
        for other_id, other_paths in self._declared.items():
            if other_id == worker_id:
                continue
            for other in other_paths:
                for new_path in resolved:
                    if paths_overlap(new_path, other):
                        conflicts.append(other_id)
                        break
                else:
                    continue
                break
        return conflicts


# ── Execution write-scope enforcement (SW-1510-SCOPE) ───────────────────────
#
# ``resolve_scope_path`` above is deliberately *lexical* (it mirrors ``004``).
# That is correct for conflict detection between two lanes but insufficient as
# an execution boundary: a lexical check accepts ``attempt/../run.db`` and never
# notices that ``attempt/link`` is a symlink to ``/etc/passwd``. The primitives
# below add the two things an execution boundary needs:
#
# * :func:`resolve_real_path` — an ``os.path.realpath`` resolution that follows
#   symlinks, so a symlinked escape is resolved to its real target.
# * :func:`resolve_under` — a *containment* resolution: a target is resolved
#   against its checkout root, ``..`` and symlinks are followed, and the result
#   must stay inside the checkout. It returns the resolved real path (so an
#   identical symlink target can be compared), or ``None`` when the target
#   escapes.
#
# And an :class:`ExecutionScope` that turns a normalized write scope into a
# *checked* boundary at the run/service seam: :meth:`ExecutionScope.check`
# returns a path when it is a legitimate inside-checkout write and ``None``
# otherwise. The caller (the runner) fails closed on ``None`` — the scope is
# enforced against the *resolved* target, not the raw string an author typed.

#: Execution-scope kinds. ``paths`` and ``objects`` are the two the generic
#: contract already carries; the four non-Git kinds below add the object scopes
#: a content/configuration/deployment/incident work contract needs, so an
#: object publication is validated against its own field and never forced to
#: name a repository path.
EXECUTION_SCOPE_KIND_PATHS = "paths"
EXECUTION_SCOPE_KIND_OBJECTS = "objects"
EXECUTION_SCOPE_KIND_CONTENT = "content"
EXECUTION_SCOPE_KIND_CONFIGURATION = "configuration"
EXECUTION_SCOPE_KIND_DEPLOYMENT = "deployment"
EXECUTION_SCOPE_KIND_INCIDENT = "incident"

EXECUTION_SCOPE_KINDS: frozenset[str] = frozenset({
    EXECUTION_SCOPE_KIND_PATHS,
    EXECUTION_SCOPE_KIND_OBJECTS,
    EXECUTION_SCOPE_KIND_CONTENT,
    EXECUTION_SCOPE_KIND_CONFIGURATION,
    EXECUTION_SCOPE_KIND_DEPLOYMENT,
    EXECUTION_SCOPE_KIND_INCIDENT,
})


def _is_full_sha(value: Any) -> bool:
    """A full 40-hex commit SHA (a commit id, never a branch or digest)."""
    if not isinstance(value, str) or len(value) != 40:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _reject_traversal(raw: str, field_name: str = "path") -> None:
    """Refuse a raw path that names a parent-directory traversal segment.

    A lexical preflight alone is insufficient (criterion 2), but a raw ``..``
    segment is unambiguous intent and is refused *before* resolution. A
    contained ``..`` (``sub/../file``) is still acceptable; only a segment that
    actually climbs is refused, and the resolved-containment check below is the
    authoritative boundary regardless.
    """
    if not isinstance(raw, str) or not raw:
        raise WriteScopeViolation(
            f"{field_name} must be a non-empty string", field=field_name
        )
    parts = raw.replace("\\", "/").split("/")
    depth = 0
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if depth == 0:
                raise WriteScopeViolation(
                    f"{field_name} {raw!r} contains a parent-directory traversal "
                    f"escape (..) and is refused before resolution",
                    field=field_name,
                )
            depth -= 1
        else:
            depth += 1


def resolve_real_path(path: str) -> str:
    """Resolve *path* to an absolute real path, following symlinks.

    Unlike :func:`resolve_scope_path` this follows every symlink segment, so a
    symlink whose target lies outside the checkout resolves to that outside
    target rather than to the link's location.
    """
    return os.path.realpath(os.path.abspath(path))


def resolve_under(root: str, target: str) -> Optional[str]:
    """Resolve *target* under *root*, returning it only when it stays inside.

    ``target`` may be absolute or relative to *root*. Both ``root`` and the
    joined target are fully realpath-resolved (symlinks followed), so:

    * ``root/sub/file`` resolves inside and is returned;
    * ``root/../outside`` escapes and returns ``None``;
    * ``root/link`` where ``link -> /etc/passwd`` resolves to ``/etc/passwd``
      and returns ``None``.

    Returns the resolved real path (so a caller can compare it to another
    resolved target), or ``None`` when the target escapes *root*.
    """
    if not isinstance(root, str) or not root:
        raise WriteScopeViolation("checkout root must be a non-empty string", field="root")
    if not isinstance(target, str) or not target:
        raise WriteScopeViolation("target must be a non-empty string", field="target")
    real_root = os.path.realpath(os.path.abspath(root))
    if os.path.isabs(target):
        joined = target
    else:
        joined = os.path.join(real_root, target)
    real_target = os.path.realpath(os.path.abspath(joined))
    if real_target == real_root:
        return real_target
    if real_target.startswith(real_root + os.sep):
        return real_target
    return None


def is_inside_root(root: str, target: str) -> bool:
    """True when *target* (symlinks followed) is *root* or below it."""
    return resolve_under(root, target) is not None


class WriteScopeViolation(Exception):
    """A requested write lies outside the enforced execution scope.

    Raised *before* any mutation, naming the raw target, the resolved target
    (when one was computed) and the scope it violated, so the refusal is
    attributable rather than a bare NO.
    """

    def __init__(
        self,
        message: str,
        *,
        field: Optional[str] = None,
        target: str = "",
        resolved: Optional[str] = None,
        kind: str = EXECUTION_SCOPE_KIND_PATHS,
    ):
        super().__init__(message)
        self.field = field
        self.target = target
        self.resolved = resolved
        self.kind = kind

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "write_scope_violation",
            "reason": str(self),
            "field": self.field,
            "target": self.target,
            "resolved": self.resolved,
            "kind": self.kind,
        }


class ObjectScopeBindingError(WriteScopeViolation):
    """An object work contract's write scope does not bind its own subject.

    A content/configuration/deployment/incident subject must be named by the
    fields its own kind carries. The scope is never satisfied by inventing a
    repository commit — a fabricated 40-hex id is refused, not silently used.
    """


#: Fields a non-Git object subject carries, keyed by scope kind. These are the
#: *generic object model* fields; nothing here is a repository SHA.
_OBJECT_SCOPE_FIELDS: dict[str, tuple[str, ...]] = {
    EXECUTION_SCOPE_KIND_CONTENT: ("channel", "content_id", "revision"),
    EXECUTION_SCOPE_KIND_CONFIGURATION: ("config_id", "environment", "version"),
    EXECUTION_SCOPE_KIND_DEPLOYMENT: ("target", "environment", "artifact_digest"),
    EXECUTION_SCOPE_KIND_INCIDENT: ("incident_id", "state", "severity"),
}


@dataclass
class ExecutionScope:
    """A normalized write scope, enforced as a real execution boundary.

    ``kind`` is one of :data:`EXECUTION_SCOPE_KINDS`. For ``paths``/``objects``
    the scope carries a ``checkout_root``; a path write is admitted only when
    it resolves inside that root (symlinks followed, traversal refused) and is
    not under an explicit ``deny``. The object kinds carry the generic subject
    object and a ``deny``/``allow`` field policy, and are validated without
    ever synthesizing a repository commit id.
    """

    kind: str = EXECUTION_SCOPE_KIND_PATHS
    checkout_root: Optional[str] = None
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    subject: Optional[Mapping[str, Any]] = None
    repository_commit: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind not in EXECUTION_SCOPE_KINDS:
            raise WriteScopeViolation(
                f"unknown execution scope kind {self.kind!r}; expected one of "
                f"{sorted(EXECUTION_SCOPE_KINDS)}",
                field="write_scope.kind",
                kind=str(self.kind),
            )
        if self.kind in (
            EXECUTION_SCOPE_KIND_PATHS,
            EXECUTION_SCOPE_KIND_OBJECTS,
        ):
            if not self.checkout_root:
                raise WriteScopeViolation(
                    f"execution scope kind {self.kind!r} requires a checkout_root",
                    field="write_scope.checkout_root",
                    kind=self.kind,
                )
        else:
            if not isinstance(self.subject, Mapping):
                raise WriteScopeViolation(
                    f"execution scope kind {self.kind!r} requires an object subject",
                    field="write_scope.subject",
                    kind=self.kind,
                )

    # -- paths / objects ----------------------------------------------------

    @property
    def resolved_root(self) -> str:
        if not self.checkout_root:
            raise WriteScopeViolation(
                "this execution scope has no checkout root",
                field="write_scope.checkout_root",
                kind=self.kind,
            )
        return os.path.realpath(os.path.abspath(self.checkout_root))

    def _denied(self, resolved: str) -> bool:
        for d in self.deny:
            real_d = os.path.realpath(
                os.path.abspath(os.path.join(self.resolved_root, d))
            )
            if resolved == real_d or resolved.startswith(real_d + os.sep):
                return True
        return False

    def check(self, target: str) -> Optional[str]:
        """Return the resolved in-scope path for *target*, or ``None`` to refuse.

        The target is joined to the checkout root (or taken absolute) and fully
        realpath-resolved. A traversal segment that escapes, a symlink whose
        target lies outside the checkout, and an explicit ``deny`` all return
        ``None``. An ``allow`` list, when present and non-wildcard, further
        bounds the admitted paths.
        """
        _reject_traversal(target, "write_scope.target")
        resolved = resolve_under(self.resolved_root, target)
        if resolved is None:
            return None
        if self._denied(resolved):
            return None
        if self.allow:
            admissible = False
            for a in self.allow:
                real_a = os.path.realpath(
                    os.path.abspath(os.path.join(self.resolved_root, a))
                )
                if resolved == real_a or resolved.startswith(real_a + os.sep):
                    admissible = True
                    break
            if not admissible:
                return None
        return resolved

    def permits(self, target: str) -> bool:
        """Boolean form of :meth:`check` (True when the write is admitted)."""
        return self.check(target) is not None

    # -- object scopes ------------------------------------------------------

    def object_fields(self) -> tuple[str, ...]:
        """The subject fields this object scope must bind, by kind."""
        return _OBJECT_SCOPE_FIELDS.get(self.kind, ())

    def validate_object(self) -> dict[str, Any]:
        """Validate the object subject against its own kind, without a SHA.

        A missing/empty required field is refused. A ``repository_commit``
        supplied for a non-Git object kind is refused *unless* it is a real
        40-hex commit — an invented id is the exact fabrication this criterion
        forbids; a kind that legitimately names no commit simply omits it.
        """
        if self.kind in (
            EXECUTION_SCOPE_KIND_PATHS,
            EXECUTION_SCOPE_KIND_OBJECTS,
        ):
            raise WriteScopeViolation(
                f"execution scope kind {self.kind!r} is not an object scope",
                field="write_scope.kind",
                kind=self.kind,
            )
        subject = self.subject or {}
        missing = [
            f for f in self.object_fields()
            if not isinstance(subject.get(f), str) or not subject.get(f).strip()
        ]
        if missing:
            raise ObjectScopeBindingError(
                f"{self.kind} scope subject is missing field(s) {missing}; "
                f"a non-Git object is named by its own fields, never by a "
                f"repository commit",
                field=f"write_scope.subject.{self.kind}",
                kind=self.kind,
            )
        if self.repository_commit is not None and not _is_full_sha(
            self.repository_commit
        ):
            raise ObjectScopeBindingError(
                f"{self.kind} scope carries a fabricated repository commit "
                f"{self.repository_commit!r}; refuse to invent a SHA for a "
                f"non-Git object",
                field="write_scope.repository_commit",
                kind=self.kind,
            )
        return {
            "kind": self.kind,
            "bindings": {f: str(subject[f]) for f in self.object_fields()},
            "repository_commit": self.repository_commit,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "checkout_root": self.checkout_root,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "subject": dict(self.subject) if self.subject else None,
            "repository_commit": self.repository_commit,
        }


def scope_from_work_contract(contract: Any, *, checkout_root: Optional[str] = None) -> ExecutionScope:
    """Build an :class:`ExecutionScope` from a ``WorkContract`` (or its dict).

    A ``paths``/``objects`` write scope needs a ``checkout_root`` (the attempt
    checkout), which the contract data does not itself carry; the caller passes
    the real root it will enforce. A non-Git object scope is built from the
    work contract's discriminated subject, so a content/configuration/
    deployment/incident contract is enforced against the fields its own kind
    carries. The contract's subject is never converted into a repository commit:
    for a non-Git kind ``repository_commit`` stays ``None``.
    """
    # Lazy import: ``dispatch`` is a sibling top-level package, not an optional
    # subpackage, and importing it here keeps ``import skillweave`` free of a
    # hard dependency on the dispatch layer.
    from skillweave.dispatch.work_contract import (
        SUBJECT_KINDS,
        SUBJECT_REPOSITORY,
    )

    if hasattr(contract, "write_scope"):
        scope = contract.write_scope
        subject = contract.subject
        kind = getattr(scope, "kind", "paths")
        allow = list(getattr(scope, "allow", []) or [])
        deny = list(getattr(scope, "deny", []) or [])
    elif isinstance(contract, Mapping):
        scope = contract.get("write_scope") or {}
        subject = contract.get("subject") or {}
        kind = str(scope.get("kind", "paths"))
        allow = list(scope.get("allow") or [])
        deny = list(scope.get("deny") or [])

        def _subject_kind(obj: Any) -> Optional[str]:
            return obj.get("kind") if isinstance(obj, Mapping) else getattr(obj, "kind", None)

        subj_kind = _subject_kind(subject)
        if subj_kind == SUBJECT_REPOSITORY:
            subject = None
            repository_commit = contract.get("subject", {}).get("commit") if isinstance(
                contract.get("subject"), Mapping
            ) else None
        else:
            repository_commit = None
    else:
        raise WriteScopeViolation(
            "contract must be a WorkContract or a mapping", field="contract"
        )

    # Normalize a subject object to a plain mapping for the object scope.
    subject_map: Optional[dict[str, Any]] = None
    subj_kind = getattr(subject, "kind", None) if subject is not None else None
    if subj_kind in SUBJECT_KINDS and subj_kind != SUBJECT_REPOSITORY:
        subject_map = subject.to_dict() if hasattr(subject, "to_dict") else dict(subject)
        kind = subj_kind
    elif isinstance(subject, Mapping) and subject.get("kind") in SUBJECT_KINDS:
        if subject.get("kind") != SUBJECT_REPOSITORY:
            subject_map = dict(subject)
            kind = subject["kind"]

    repository_commit = None
    if getattr(subject, "kind", None) == SUBJECT_REPOSITORY:
        repository_commit = subject.commit  # a real Git subject, not fabricated

    if kind in (EXECUTION_SCOPE_KIND_PATHS, EXECUTION_SCOPE_KIND_OBJECTS):
        return ExecutionScope(
            kind=kind,
            checkout_root=checkout_root,
            allow=allow,
            deny=deny,
        )
    return ExecutionScope(
        kind=kind,
        subject=subject_map,
        allow=allow,
        deny=deny,
        repository_commit=repository_commit,
    )
