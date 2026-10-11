"""Repository-scoped lane contract (SW-159P-REPO-001).

A multi-repository run has more than one product repository in play. Before
this contract the workspace layer assumed a single global product repository:
one ``GitWorktreeProvider``, one worktree root, one base. That assumption leaks
across repositories — a base SHA from repository B may resolve in repository A
only by accident, and a colliding lane branch name in two repositories can be
conflated.

This module makes the repository the unit of scope. The central contract is
:class:`RepositoryTarget`: a *versioned* record of one lane's repository — its
repository id, canonical root, base ref, resolved full base SHA, controller
branch, and a bounded worktree root. Every mutating lane references exactly one
target.

Two further facts are enforced, in order:

1. **Repo-scoped SHA resolution.** :func:`resolve_ref_in_repository` resolves a
   base or produced-candidate ref *inside the declared repository*. A SHA that
   does not resolve there (because it lives in another repository's object
   store) is refused. :func:`plan_lane` runs this *before* any worktree is
   created, so a cross-repository SHA can never seed a lane.

2. **Derived providers and paths.** :func:`provider_for` and
   :func:`worktree_path_for` derive the ``GitWorktreeProvider`` instance and the
   lane worktree path from the target — never from one global product
   repository. The path is bounded inside the target's ``worktree_root`` and
   outside the primary checkout.

This module is decision-only except where it is explicitly asked to materialise
a worktree (:func:`acquire_lane`), which operates strictly inside one target's
repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from skillweave.routing.workspace import (
    FULL_SHA_PATTERN,
    is_outside_primary_checkout,
)

#: The schema version of the RepositoryTarget contract. Bump only on a breaking
#: shape change; a parser refuses any other value.
REPOSITORY_TARGET_SCHEMA_VERSION = 1

#: The stable schema identifier the contract advertises.
REPOSITORY_TARGET_SCHEMA_ID = "https://skillweave.dev/schemas/repository-target/v1"

#: The exact top-level keys the RepositoryTarget contract permits.
REPOSITORY_TARGET_KEYS = frozenset(
    (
        "schema_version",
        "repository_id",
        "canonical_root",
        "base_ref",
        "base_sha",
        "controller_branch",
        "worktree_root",
    )
)


class RepositoryTargetError(ValueError):
    """A repository target or a repository-scoped operation failed the contract.

    Raised fail-closed, before any consumer acts, with the offending field named
    (when the fault is a field) so the violation is explicit.
    """

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


# ── Validation helpers ──────────────────────────────────────────────────────


def _require_nonempty(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepositoryTargetError(
            f"'{key}' must be a non-empty string, got {value!r}", field=key
        )
    return value


def _require_full_sha(value: Any, key: str) -> str:
    if not isinstance(value, str) or FULL_SHA_PATTERN.fullmatch(value) is None:
        raise RepositoryTargetError(
            f"'{key}' must be a full 40-hex SHA, got {value!r}", field=key
        )
    return value


def _require_repository_id(value: Any) -> str:
    value = _require_nonempty(value, "repository_id")
    if "/" in value or "\\" in value or value in (".", ".."):
        raise RepositoryTargetError(
            f"'repository_id' must be a single path component, got {value!r}",
            field="repository_id",
        )
    return value


def _require_controller_branch(value: Any) -> str:
    value = _require_nonempty(value, "controller_branch")
    if not value.startswith("ops/") or not value.endswith("-controller"):
        raise RepositoryTargetError(
            "'controller_branch' must be an 'ops/<run>-controller' branch, got "
            f"{value!r}",
            field="controller_branch",
        )
    return value


def _require_absolute_path(value: Any, key: str) -> Path:
    value = _require_nonempty(value, key)
    path = Path(value)
    if not path.is_absolute():
        raise RepositoryTargetError(
            f"'{key}' must be an absolute path, got {value!r}", field=key
        )
    return path


def _require_lane(lane: Any) -> str:
    lane = _require_nonempty(lane, "lane")
    if "/" in lane or "\\" in lane or lane in (".", ".."):
        raise RepositoryTargetError(
            f"'lane' must be a single path component, got {lane!r}", field="lane"
        )
    return lane


def _require_known_keys(
    value: Mapping[str, Any], allowed: frozenset, *, prefix: str = ""
) -> None:
    for key in value:
        if key not in allowed:
            field_name = f"{prefix}{key}"
            raise RepositoryTargetError(
                f"disallowed property '{field_name}'", field=field_name
            )


def controller_branch_for(run: str) -> str:
    """Return the repository's controller branch name for a run."""
    run = _require_nonempty(run, "run")
    return f"ops/{run}-controller"


# ── RepositoryTarget ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepositoryTarget:
    """The versioned, repository-scoped facts one lane operates on.

    ``repository_id`` is the lane-level identity of one repository (a single
    path component, e.g. ``"skillweave"``). ``canonical_root`` is that
    repository's primary checkout. ``base_ref`` is the ref the lane starts from
    and ``base_sha`` is its resolved full 40-hex commit — resolved *inside this
    repository*. ``controller_branch`` is where completed lanes of this
    repository integrate. ``worktree_root`` is the bounded root under which the
    lane's worktree is placed; it must lie outside the primary checkout so the
    checkout stays clean.
    """

    repository_id: str
    canonical_root: str
    base_ref: str
    base_sha: str
    controller_branch: str
    worktree_root: str
    schema_version: int = REPOSITORY_TARGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REPOSITORY_TARGET_SCHEMA_VERSION:
            raise RepositoryTargetError(
                f"unsupported schema_version {self.schema_version!r}; "
                f"expected {REPOSITORY_TARGET_SCHEMA_VERSION}",
                field="schema_version",
            )
        _require_repository_id(self.repository_id)
        canonical_root = _require_absolute_path(self.canonical_root, "canonical_root")
        _require_nonempty(self.base_ref, "base_ref")
        _require_full_sha(self.base_sha, "base_sha")
        _require_controller_branch(self.controller_branch)
        worktree_root = _require_absolute_path(self.worktree_root, "worktree_root")
        if not is_outside_primary_checkout(str(canonical_root), str(worktree_root)):
            raise RepositoryTargetError(
                "'worktree_root' must lie outside the primary checkout "
                f"({self.canonical_root}); got {self.worktree_root!r}",
                field="worktree_root",
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_id": self.repository_id,
            "canonical_root": self.canonical_root,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "controller_branch": self.controller_branch,
            "worktree_root": self.worktree_root,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepositoryTarget":
        """Parse and fail-closed validate a repository target mapping."""
        if not isinstance(data, Mapping):
            raise RepositoryTargetError("repository target must be a mapping")
        _require_known_keys(data, REPOSITORY_TARGET_KEYS)
        for key in (
            "repository_id",
            "canonical_root",
            "base_ref",
            "base_sha",
            "controller_branch",
            "worktree_root",
        ):
            if key not in data:
                raise RepositoryTargetError(f"missing required '{key}'", field=key)
        return cls(
            repository_id=data["repository_id"],
            canonical_root=data["canonical_root"],
            base_ref=data["base_ref"],
            base_sha=data["base_sha"],
            controller_branch=data["controller_branch"],
            worktree_root=data["worktree_root"],
            schema_version=data.get(
                "schema_version", REPOSITORY_TARGET_SCHEMA_VERSION
            ),
        )


# ── Repository-scoped git resolution ────────────────────────────────────────


def _run_git(cwd: Any, *args: str) -> subprocess.CompletedProcess:
    """Run a git command rooted at ``cwd`` with a deterministic environment."""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
        )
    except OSError as exc:  # pragma: no cover - git is present in tests
        raise RepositoryTargetError(f"git is unavailable: {exc}") from exc


def resolve_ref_in_repository(target: RepositoryTarget, ref: str) -> str:
    """Resolve ``ref`` to a full commit SHA *inside the target repository*.

    The lookup runs in ``target.canonical_root``'s object store, so a SHA that
    does not exist there — most importantly, a SHA produced by a different
    repository — is refused with :class:`RepositoryTargetError`. This is the
    check that must run before any worktree is created.
    """
    _require_nonempty(ref, "ref")
    proc = _run_git(
        target.canonical_root,
        "rev-parse",
        "--verify",
        "--quiet",
        f"{ref}^{{commit}}",
    )
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or FULL_SHA_PATTERN.fullmatch(sha) is None:
        raise RepositoryTargetError(
            f"ref {ref!r} does not resolve to a commit in repository "
            f"{target.repository_id!r} ({target.canonical_root})",
            field="ref",
        )
    # Prove object-store membership explicitly: a commit reachable by
    # ``rev-parse`` is in this repository's store; a foreign SHA is not.
    exists = _run_git(target.canonical_root, "cat-file", "-e", f"{sha}^{{commit}}")
    if exists.returncode != 0:
        raise RepositoryTargetError(
            f"ref {ref!r} resolved to {sha!r} but is not present in repository "
            f"{target.repository_id!r}",
            field="ref",
        )
    return sha


def resolve_base(target: RepositoryTarget, ref: Optional[str] = None) -> str:
    """Resolve the lane base ref to a full SHA in the declared repository."""
    return resolve_ref_in_repository(target, ref or target.base_ref)


def resolve_candidate(target: RepositoryTarget, ref: str) -> str:
    """Resolve a produced-candidate ref to a full SHA in the declared repository.

    A candidate SHA that does not resolve in this repository is refused, so a
    candidate can never be recorded against the wrong repository.
    """
    return resolve_ref_in_repository(target, ref)


# ── Derived provider and bounded worktree path ──────────────────────────────


def provider_for(target: RepositoryTarget):
    """Return the ``GitWorktreeProvider`` derived from this target.

    The provider is rooted at the target's ``canonical_root``; it is never a
    single global product-repository provider. Lazy import keeps the workspace
    package import order independent.
    """
    from skillweave.workspace.provider import GitWorktreeProvider

    return GitWorktreeProvider(target.canonical_root)


def worktree_path_for(target: RepositoryTarget, lane: str) -> Path:
    """Return the bounded lane worktree path derived from this target.

    ``<worktree_root>/<lane>``. The result is proven to stay inside the target's
    ``worktree_root`` and outside the primary checkout, so a lane can never
    place a worktree in another repository or dirty the checkout.
    """
    lane = _require_lane(lane)
    root = Path(target.worktree_root)
    path = root / lane
    if path != root and root not in path.parents:
        raise RepositoryTargetError(
            f"lane worktree path {str(path)!r} escapes the bounded worktree root "
            f"{target.worktree_root!r}",
            field="worktree_root",
        )
    if not is_outside_primary_checkout(target.canonical_root, str(path)):
        raise RepositoryTargetError(
            f"lane worktree path {str(path)!r} is inside the primary checkout "
            f"{target.canonical_root!r}",
            field="worktree_root",
        )
    return path


# ── Dispatcher: resolve before worktree creation ────────────────────────────


@dataclass(frozen=True)
class DispatchedLane:
    """A resolved lane dispatch: exactly one repository, one base, one path."""

    repository_id: str
    lane: str
    branch: str
    base_sha: str
    worktree_path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "lane": self.lane,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "worktree_path": self.worktree_path,
        }


def lane_branch(lane: str) -> str:
    """Return the default lane branch name ``ops/SW-159P-<lane>``."""
    return f"ops/SW-159P-{_require_lane(lane)}"


def plan_lane(
    target: RepositoryTarget,
    lane: str,
    *,
    branch: Optional[str] = None,
    base_ref: Optional[str] = None,
) -> DispatchedLane:
    """Resolve a lane's base SHA in its repository *before* worktree creation.

    Raises :class:`RepositoryTargetError` if the base ref does not resolve in
    the declared repository. No worktree is created by this call: it is the
    resolution gate the dispatcher runs first.
    """
    lane = _require_lane(lane)
    base_sha = resolve_base(target, base_ref)
    return DispatchedLane(
        repository_id=target.repository_id,
        lane=lane,
        branch=branch or lane_branch(lane),
        base_sha=base_sha,
        worktree_path=str(worktree_path_for(target, lane)),
    )


def acquire_lane(
    target: RepositoryTarget,
    lane: str,
    *,
    branch: Optional[str] = None,
    base_ref: Optional[str] = None,
    created_at: Optional[str] = None,
):
    """Materialise a lane worktree, resolving the base SHA first.

    The base resolution runs before ``GitWorktreeProvider.acquire``, so a
    cross-repository SHA is refused before any worktree or branch exists.
    Returns the provider's :class:`Workspace`.
    """
    dispatch = plan_lane(target, lane, branch=branch, base_ref=base_ref)
    provider = provider_for(target)
    return provider.acquire(
        dispatch.base_sha,
        dispatch.branch,
        path=dispatch.worktree_path,
        created_at=created_at,
    )


__all__ = [
    "REPOSITORY_TARGET_SCHEMA_VERSION",
    "REPOSITORY_TARGET_SCHEMA_ID",
    "REPOSITORY_TARGET_KEYS",
    "RepositoryTargetError",
    "controller_branch_for",
    "RepositoryTarget",
    "resolve_ref_in_repository",
    "resolve_base",
    "resolve_candidate",
    "provider_for",
    "worktree_path_for",
    "DispatchedLane",
    "lane_branch",
    "plan_lane",
    "acquire_lane",
]
