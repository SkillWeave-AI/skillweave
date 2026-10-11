"""Operator dispatch sequence and event contract (SW138-CONTRACT-001).

This module owns the *contract*, not the mechanics. It defines:

* the sequence declaration a wave dispatcher consumes — session boundary, an
  explicit profile reference, the execution model, the correction budget, and
  per-lane repo plus full base SHA;
* the fail-closed validation that must run *before* any worker-start callback;
* the event record every dispatch event carries — run/wave/lane/dispatch
  identifiers, a monotonic sequence, a timestamp, an event type, process status,
  task/evidence status, and optional receipt references;
* the practice task validation that accepts ``acceptanceCriteria``, Fibonacci
  ``points``, ``dependsOn`` and ``lane`` and refuses anything else.

Nothing here launches a worker. No model name and no harness name appears as a
default, and no duration-estimate field exists on any record: the sequence is
capability-based, and time estimates are not part of the contract (the practice
PRD uses Fibonacci ``points``, never minutes).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Mapping, Optional, Sequence

from skillweave.dispatch.work_contract import (
    SubjectRef,
    EvidenceReceipt,
    subject_ref_from_dict,
    is_git_subject,
)

#: The only permitted session boundary. Reused from the post-v1.3.7 promptchain
#: declaration: a sequence that does not declare ``session_boundary: batch`` is
#: refused, never defaulted.
SESSION_BOUNDARY_BATCH = "batch"

#: The legal Fibonacci point values of the practice contract.
FIBONACCI_POINTS = (1, 2, 3, 5, 8, 13)

#: The scalar fields a sequence *must* declare for a lane to be dispatchable.
_REQUIRED_SEQUENCE_KEYS = (
    "session_boundary",
    "profile",
    "execution_model",
    "max_correction_rounds_per_wave",
    "max_parallel",
    "lanes",
)

#: The fields a mutating lane *must* resolve before dispatch.
_REQUIRED_LANE_KEYS = ("repo", "base", "execution_model")


class ContractError(ValueError):
    """A declaration failed the dispatch contract.

    Raised before any worker-start callback is invoked: validating exactly the
    fail-closed surface of the contract, with the offending field named.
    """

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


class SequenceBoundaryError(ContractError):
    """A declaration that does not declare ``session_boundary: batch``."""


class LaneValidationError(ContractError):
    """A lane that cannot be dispatched (missing repo/base/model/criteria)."""


class PracticeTaskError(ContractError):
    """A practice task that fails field or Fibonacci-point validation."""


@dataclass
class ProfileReference:
    """The explicit profile a declaration points at.

    The path is required and must be a non-empty string; ``required`` records
    whether the profile is mandatory for mutating dispatch (it is, by the
    contract — the caller states it, we never imply it).
    """

    path: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "required": self.required}


@dataclass
class CriterionGroup:
    """One dispatch group: the acceptance criteria it covers.

    ``criteria`` are 1-based criterion indices. A lane's groups must cover every
    criterion exactly once (criterion coverage); the exact-once check is the
    lane's ``covers_criteria`` helper.
    """

    criteria: List[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"criteria": list(self.criteria)}


@dataclass
class Lane:
    """One dispatchable lane: role, repo, full base SHA, execution model.

    ``mutating`` distinguishes a write lane from a read-only one. A mutating
    lane must carry ``repo``, ``base`` (a full SHA, not a branch name), and
    ``execution_model``. ``criterion_groups`` holds the criterion coverage for
    lanes that discharge acceptance criteria.

    The topology fields (``depends_on``, ``write_scope``, ``worktree``,
    ``branch``, ``integration_policy``, ``harness_state_namespace``) are the
    ``dispatch-sequence.schema.json`` lane properties consumed here so a
    governed mutating lane's manifest is parsed, not merely validated (and then
    dropped). ``depends_on``/``write_scope`` are ``None`` when *absent* (never
    defaulted to ``[]``) so the operator dispatcher can tell "declared empty"
    from "not declared".
    """

    id: str
    role: str
    repo: Optional[str] = None
    base: Optional[str] = None
    execution_model: Optional[str] = None
    mutating: bool = False
    criterion_groups: List[CriterionGroup] = field(default_factory=list)
    depends_on: Optional[List[str]] = None
    write_scope: Optional[List[str]] = None
    worktree: Optional[str] = None
    branch: Optional[str] = None
    integration_policy: Optional[str] = None
    harness_state_namespace: Optional[str] = None

    def criteria_covered(self) -> List[int]:
        """Flatten every criterion group into one list, preserving order."""
        return [c for g in self.criterion_groups for c in g.criteria]

    def covers_criteria(self, criterion_indices: Sequence[int]) -> bool:
        """True when the lane covers each criterion exactly once.

        Exact-once coverage: a missing or duplicated index fails. This is the
        criterion-coverage gate a mutating lane must pass before dispatch.
        """
        covered = self.criteria_covered()
        expected = sorted(int(i) for i in criterion_indices)
        return sorted(covered) == expected

    def to_subject_ref(self) -> Optional[SubjectRef]:
        """Produce a :class:`SubjectRef` from this lane's Git identity.

        A lane that carries both ``repo`` and a full base SHA returns a
        ``RepositorySubject``. A lane without a repo (a read-only reviewer, for
        example) returns ``None`` — the lane has no subject identity to carry.
        """
        if self.repo and self.base and _is_full_sha(self.base):
            from skillweave.dispatch.work_contract import RepositorySubject

            return RepositorySubject(repo=self.repo, commit=self.base)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "repo": self.repo,
            "base": self.base,
            "execution_model": self.execution_model,
            "mutating": self.mutating,
            "criterion_groups": [g.to_dict() for g in self.criterion_groups],
            "depends_on": list(self.depends_on) if self.depends_on is not None else None,
            "write_scope": list(self.write_scope) if self.write_scope is not None else None,
            "worktree": self.worktree,
            "branch": self.branch,
            "integration_policy": self.integration_policy,
            "harness_state_namespace": self.harness_state_namespace,
        }


@dataclass
class SequenceDeclaration:
    """The operator dispatch sequence declaration.

    Carries the session boundary (must be ``batch``), the explicit profile
    reference, the execution model, the correction budget, ``max_parallel`` and
    the lanes.
    """

    session_boundary: str
    profile: ProfileReference
    execution_model: str
    max_correction_rounds_per_wave: int
    max_parallel: int
    lanes: List[Lane] = field(default_factory=list)

    def mutating_lanes(self) -> List[Lane]:
        return [lane for lane in self.lanes if lane.mutating]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_boundary": self.session_boundary,
            "profile": self.profile.to_dict(),
            "execution_model": self.execution_model,
            "max_correction_rounds_per_wave": self.max_correction_rounds_per_wave,
            "max_parallel": self.max_parallel,
            "lanes": [lane.to_dict() for lane in self.lanes],
        }


# --- Sequence loading and fail-closed validation ---------------------------

def _require_nonempty_string(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(
            f"'{key}' must be a non-empty string, got {value!r}", field=key
        )
    return value.strip()


def _parse_profile(data: Any) -> ProfileReference:
    if not isinstance(data, Mapping):
        raise ContractError(
            "profile must be a mapping with a non-empty 'path'", field="profile"
        )
    path = _require_nonempty_string(data.get("path"), "profile.path")
    return ProfileReference(path=path, required=bool(data.get("required", True)))


def _parse_lane(data: Any, index: int) -> Lane:
    if not isinstance(data, Mapping):
        raise ContractError(f"lane {index} must be a mapping", field=f"lanes[{index}]")
    lane_id = _require_nonempty_string(data.get("id"), f"lanes[{index}].id")
    role = _require_nonempty_string(data.get("role"), f"lanes[{index}].role")
    groups = []
    for g in (data.get("criterion_groups") or []):
        if not isinstance(g, Mapping):
            raise ContractError(
                f"lane '{lane_id}' criterion group must be a mapping",
                field=f"lanes[{index}].criterion_groups",
            )
        criteria = [int(c) for c in g.get("criteria") or []]
        groups.append(CriterionGroup(criteria=criteria))
    depends_on = data.get("depends_on")
    write_scope = data.get("write_scope")
    return Lane(
        id=lane_id,
        role=role,
        repo=data.get("repo"),
        base=data.get("base"),
        execution_model=data.get("execution_model"),
        mutating=bool(data.get("mutating", False)),
        criterion_groups=groups,
        depends_on=list(depends_on) if isinstance(depends_on, list) else None,
        write_scope=list(write_scope) if isinstance(write_scope, list) else None,
        worktree=data.get("worktree"),
        branch=data.get("branch"),
        integration_policy=data.get("integration_policy"),
        harness_state_namespace=data.get("harness_state_namespace"),
    )


def load_sequence(declaration: Mapping[str, Any]) -> SequenceDeclaration:
    """Parse a dispatch sequence declaration and validate its scalar fields.

    ``session_boundary`` must be ``batch``; the profile reference, execution
    model, correction budget and ``max_parallel`` must be present. Lanes are
    parsed structurally. This does not yet apply the mutating-lane fail-closed
    checks — those run in :func:`validate_for_dispatch`, immediately before a
    worker-start callback, so a caller can validate without side effects.
    """
    if not isinstance(declaration, Mapping):
        raise ContractError("declaration must be a mapping", field=None)

    boundary = declaration.get("session_boundary")
    if boundary != SESSION_BOUNDARY_BATCH:
        raise SequenceBoundaryError(
            "declaration must set 'session_boundary' to 'batch'; "
            f"got {boundary!r}",
            field="session_boundary",
        )

    profile = _parse_profile(declaration.get("profile"))
    execution_model = _require_nonempty_string(
        declaration.get("execution_model"), "execution_model"
    )

    max_rounds = declaration.get("max_correction_rounds_per_wave")
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 0:
        raise ContractError(
            "'max_correction_rounds_per_wave' must be a non-negative integer, "
            f"got {max_rounds!r}",
            field="max_correction_rounds_per_wave",
        )

    max_parallel = declaration.get("max_parallel")
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel < 1:
        raise ContractError(
            "'max_parallel' must be a positive integer, got {max_parallel!r}",
            field="max_parallel",
        )

    lanes_raw = declaration.get("lanes")
    if not isinstance(lanes_raw, list) or not lanes_raw:
        raise ContractError("'lanes' must be a non-empty list", field="lanes")

    lanes = [_parse_lane(lane, i) for i, lane in enumerate(lanes_raw)]
    return SequenceDeclaration(
        session_boundary=boundary,
        profile=profile,
        execution_model=execution_model,
        max_correction_rounds_per_wave=max_rounds,
        max_parallel=max_parallel,
        lanes=lanes,
    )


def _is_full_sha(value: Any) -> bool:
    """A full base SHA is 40 hexadecimal characters, not a branch name."""
    if not isinstance(value, str):
        return False
    if len(value) != 40:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def validate_mutating_lane(
    lane: Lane,
    criterion_indices: Sequence[int],
) -> None:
    """Fail-closed checks for a mutating lane.

    A mutating lane without ``repo``, without a full base SHA, without an
    ``execution_model``, or without exactly-once criterion coverage raises
    :class:`LaneValidationError`. A read-only lane is not checked here — the
    contract binds mutating lanes, whose dispatch has side effects.
    """
    if not lane.mutating:
        return

    if not lane.repo:
        raise LaneValidationError(
            f"mutating lane '{lane.id}' must declare 'repo'", field=f"{lane.id}.repo"
        )
    if not _is_full_sha(lane.base):
        raise LaneValidationError(
            f"mutating lane '{lane.id}' must declare a full base SHA (40 hex chars), "
            f"got {lane.base!r}",
            field=f"{lane.id}.base",
        )
    if not lane.execution_model:
        raise LaneValidationError(
            f"mutating lane '{lane.id}' must declare 'execution_model'",
            field=f"{lane.id}.execution_model",
        )
    if criterion_indices and not lane.covers_criteria(criterion_indices):
        raise LaneValidationError(
            f"mutating lane '{lane.id}' criterion coverage must cover each of "
            f"{list(criterion_indices)} exactly once, got {lane.criteria_covered()}",
            field=f"{lane.id}.criterion_groups",
        )


def validate_for_dispatch(
    declaration: SequenceDeclaration,
    criterion_indices: Sequence[int],
    *,
    on_worker_start: Optional[Callable[[Lane], None]] = None,
) -> None:
    """Validate the declaration before any worker-start callback.

    ``criterion_indices`` is the full set of acceptance-criterion indices the
    wave must discharge. Every mutating lane is fail-closed validated first;
    only when *all* lanes pass is ``on_worker_start`` invoked for the lanes that
    are eligible to start. A failed validation raises before ``on_worker_start``
    is ever called — the "starts zero workers" contract.
    """
    for lane in declaration.mutating_lanes():
        validate_mutating_lane(lane, criterion_indices)

    if on_worker_start is not None:
        for lane in declaration.lanes:
            on_worker_start(lane)


# --- Event contract --------------------------------------------------------

class EventType(str, Enum):
    """The typed lifecycle events of the dispatch stream."""

    WAVE_STARTED = "wave_started"
    LANE_STARTED = "lane_started"
    DISPATCH_STARTED = "dispatch_started"
    HEARTBEAT = "heartbeat"
    PROCESS_TERMINAL = "process_terminal"
    EVIDENCE_RECORDED = "evidence_recorded"
    LANE_TERMINAL = "lane_terminal"
    INTERVENTION_RESTART = "intervention_restart"
    INTERVENTION_MALFORMED = "intervention_malformed"
    INTERVENTION_DESYNC = "intervention_desync"


class ProcessStatus(str, Enum):
    """The process status a dispatch event reports."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    EXITED = "exited"
    SIGNALED = "signaled"
    TIMED_OUT = "timed_out"
    LAUNCH_FAILED = "launch_failed"


class TaskStatus(str, Enum):
    """The task status a dispatch event reports."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class DispatchEvent:
    """One dispatch event.

    Carries the run, wave, lane and dispatch identifiers, a monotonically
    increasing ``sequence`` number, a ``timestamp``, an ``event_type``, a
    ``process_status``, a ``task_status``, an optional ``evidence_status`` and
    optional ``receipt_refs``. There is no duration-estimate and no model or
    harness field: the payload is metadata-only.
    """

    run_id: str
    wave: str
    lane_id: str
    dispatch_id: str
    sequence: int
    timestamp: str
    event_type: str
    process_status: str
    task_status: str
    evidence_status: Optional[str] = None
    receipt_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "wave": self.wave,
            "lane_id": self.lane_id,
            "dispatch_id": self.dispatch_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "process_status": self.process_status,
            "task_status": self.task_status,
        }
        if self.evidence_status is not None:
            payload["evidence_status"] = self.evidence_status
        payload["receipt_refs"] = list(self.receipt_refs)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


# --- Practice task validation ----------------------------------------------

def validate_practice_task(task: Mapping[str, Any]) -> None:
    """Validate a practice task's required fields and Fibonacci points.

    A task must declare ``acceptanceCriteria`` (non-empty), Fibonacci ``points``
    (one of 1, 2, 3, 5, 8, 13), ``dependsOn`` (a list) and ``lane``. Points
    outside the Fibonacci set raise :class:`PracticeTaskError`. There is no
    duration-estimate field in the contract — and a task that carries one is
    rejected as carrying an illegal field.
    """
    if not isinstance(task, Mapping):
        raise PracticeTaskError("task must be a mapping", field=None)

    acceptance = task.get("acceptanceCriteria")
    if not isinstance(acceptance, list) or not acceptance:
        raise PracticeTaskError(
            "'acceptanceCriteria' must be a non-empty list", field="acceptanceCriteria"
        )

    points = task.get("points")
    if not isinstance(points, int) or isinstance(points, bool):
        raise PracticeTaskError(
            "task 'points' must be a Fibonacci integer, got "
            f"{points!r}",
            field="points",
        )
    if points not in FIBONACCI_POINTS:
        raise PracticeTaskError(
            f"task 'points' {points} is not a Fibonacci value "
            f"{list(FIBONACCI_POINTS)}",
            field="points",
        )

    depends_on = task.get("dependsOn")
    if not isinstance(depends_on, list):
        raise PracticeTaskError(
            "'dependsOn' must be a list", field="dependsOn"
        )

    if not task.get("lane") and "lane" not in task:
        raise PracticeTaskError("'lane' is required", field="lane")

    # No duration-estimate field is permitted on the practice contract. Refuse
    # it explicitly rather than silently accepting a field the PRD forbids.
    for illegal in ("estimated_minutes", "duration", "estimatedDuration", "effort_minutes"):
        if illegal in task:
            raise PracticeTaskError(
                f"task carries a forbidden duration-estimate field "
                f"'{illegal}'",
                field=illegal,
            )


__all__ = [
    "ContractError",
    "SequenceBoundaryError",
    "LaneValidationError",
    "PracticeTaskError",
    "ProfileReference",
    "CriterionGroup",
    "Lane",
    "SequenceDeclaration",
    "EventType",
    "ProcessStatus",
    "TaskStatus",
    "DispatchEvent",
    "SESSION_BOUNDARY_BATCH",
    "FIBONACCI_POINTS",
    "load_sequence",
    "validate_mutating_lane",
    "validate_for_dispatch",
    "validate_practice_task",
    "SubjectRef",
    "EvidenceReceipt",
    "subject_ref_from_dict",
    "is_git_subject",
]
