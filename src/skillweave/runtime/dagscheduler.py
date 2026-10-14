"""DagScheduler: resolve a task graph into ordered batches.

``write_scope`` (009) provides claim and release for write scopes but no
ordering. This module takes a task graph, resolves ``depends_on`` into
topologically ordered batches, and rejects cycles explicitly. It starts no
processes — that is ``runner_adapter`` (011).

Dispatch 1 scope (SW-135-010, criteria 1 and 5):
  * a graph of tasks with ``depends_on`` becomes an ordered list of batches;
  * a cyclic graph is an error, never silently dropped;
  * the result is deterministic: the same graph yields the same batches in
    the same order, regardless of input ordering.

Dispatch 2 scope (SW-135-010, criteria 2 and 9):
  * two lanes with overlapping write scopes never share a parallel batch —
    proven through the claim machinery from 009 (``paths_overlap`` over
    resolved paths), not a fresh check;
  * every lane is emitted with ``execution_mode`` ``fan-out`` or ``inline``,
    derived from write-scope overlap via the claim registry, never from a
    hand-written flag.

Dispatch 3 scope (SW-135-010, criteria 7, 8 and 10):
  * every emitted batch is a session boundary and carries the marker
    explicitly, so a consumer never has to infer where one session ends;
  * a sequence that does not declare ``session_boundary`` is refused, never
    defaulted (inventing a boundary is the defect);
  * red proof: the missing boundary is rejected with a message naming the
    missing key, and overlapping lanes are never emitted as fan-out.

Dispatch 4 adds ``max_parallel`` and dependent-gating.

SW-1510-ADAPTIVE (adaptive scheduling + measured parallelism, criteria 1 and 3)
-------------------------------------------------------------------------------
Dispatch 5 makes admission *adaptive*:

* a ready lane is admitted by dependency, write-surface exclusion, **fresh**
  route capability/capacity and **remaining** reservations — four observed
  dimensions, none of them invented;
* the maximum parallelism is *measured* from those fresh observations rather
  than fixed at a magic number;
* a refused lane is reported (``Schedule.refusals``), never silently dropped;
* every selection/escalation/split/backoff is recorded with its cause, candidate
  set, rejected routes, policy/catalog revision, budget delta and outcome (see
  :class:`SelectionLedger`).

The module still starts no process and knows no runner: admission and
parallelism are decided here declaratively, from observed data handed in.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Sequence as TypingSequence

# ``class Sequence`` below is a public scheduling primitive (exported in
# ``__all__``); it deliberately shares the name of ``typing.Sequence``. The
# typing alias is therefore imported as ``TypingSequence`` and every *generic
# annotation* uses that alias, so ``TypingSequence[TypingSequence[Lane]]`` can
# never resolve to the local class and raise
# ``TypeError: type 'Sequence' is not subscriptable`` at import time.

from .write_scope import resolve_scope_path, paths_overlap, WriteScopeClaim
from ..trace.gate_receipt import (
    ACCEPTANCE_TERMINAL,
    MissingProducerArtifact,
    UnsupportedContractVersion,
    validate_independent_gate_receipt,
)


class CyclicGraphError(Exception):
    """Raised when the task graph contains a dependency cycle.

    ``unresolved`` names every task that can never be scheduled because it
    participates in (or depends on) a cycle. Reporting them is the point: a
    cycle must surface as an error, not vanish into a missing task.
    """

    def __init__(self, unresolved: TypingSequence[str]):
        self.unresolved = sorted(unresolved)
        super().__init__(
            "Task graph contains a dependency cycle involving: "
            + ", ".join(self.unresolved)
        )


class UnknownDependencyError(Exception):
    """Raised when a task depends on a task that is not in the graph."""

    def __init__(self, task_id: str, missing: str):
        self.task_id = task_id
        self.missing = missing
        super().__init__(
            f"Task '{task_id}' depends on '{missing}', which is not in the graph"
        )


@dataclass
class Task:
    id: str
    depends_on: List[str] = field(default_factory=list)
    write_scope: List[str] = field(default_factory=list)
    gate: Optional[str] = None
    #: The approved route this lane may run on (a ``provider/model`` pair), or
    #: ``None`` when the lane declares no route.  Admission (criterion 1) checks
    #: a declared route against the *fresh* available route set; a task with no
    #: declared route is skipped by the route dimension, never refused for it.
    route: Optional[str] = None


@dataclass
class Batch:
    index: int
    tasks: List[Task]

    @property
    def task_ids(self) -> List[str]:
        return [t.id for t in self.tasks]


EXECUTION_MODE_FANOUT = "fan-out"
EXECUTION_MODE_INLINE = "inline"


@dataclass
class Lane:
    """A task annotated with the execution mode derived from write scope.

    ``execution_mode`` is ``fan-out`` when the lane may run in parallel with
    the other lanes of its batch (its write scope is disjoint from theirs and
    from every held claim); ``inline`` when its scope overlaps another lane or
    a held claim, forcing it to run alone.
    """

    task: Task
    execution_mode: str

    @property
    def task_id(self) -> str:
        return self.task.id


def build_batches(tasks: TypingSequence[Task]) -> List[Batch]:
    """Resolve ``tasks`` into an ordered list of batches.

    A batch is the earliest layer a task can belong to: batch 0 holds tasks
    with no dependencies, batch 1 holds tasks whose dependencies are all in
    earlier batches, and so on.

    Determinism: tasks are considered in id order at every step, so the same
    graph yields the same batches in the same order regardless of the input
    ordering.
    """
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        seen = set()
        dupes = sorted({i for i in ids if i in seen or seen.add(i)})
        raise ValueError(f"Duplicate task ids: {dupes}")

    by_id = {t.id: t for t in tasks}

    dependents = {t.id: [] for t in tasks}
    unresolved_count = {t.id: 0 for t in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep not in by_id:
                raise UnknownDependencyError(task.id, dep)
            dependents[dep].append(task.id)
            unresolved_count[task.id] += 1

    ready = sorted(t.id for t in tasks if unresolved_count[t.id] == 0)

    batches: List[Batch] = []
    scheduled: List[str] = []

    while ready:
        batches.append(Batch(index=len(batches), tasks=[by_id[tid] for tid in ready]))
        scheduled.extend(ready)
        next_ready = []
        for tid in ready:
            for dependent in dependents[tid]:
                unresolved_count[dependent] -= 1
                if unresolved_count[dependent] == 0:
                    next_ready.append(dependent)
        ready = sorted(next_ready)

    if len(scheduled) != len(tasks):
        remaining = sorted(t.id for t in tasks if t.id not in set(scheduled))
        raise CyclicGraphError(remaining)

    return batches


def _resolved_scopes(task: Task) -> List[str]:
    return [resolve_scope_path(p) for p in task.write_scope]


def _scope_conflicts(
    scopes: TypingSequence[str],
    others: TypingSequence[str],
) -> bool:
    """True when any path in ``scopes`` overlaps any path in ``others``.

    Overlap is decided by ``write_scope.paths_overlap`` (009) over resolved
    absolute paths — never by a hand-written string comparison. This is the
    same arbitration primitive the claim registry uses.
    """
    for own in scopes:
        for other in others:
            if paths_overlap(own, other):
                return True
    return False


def build_lanes(
    tasks: TypingSequence[Task],
    held_claims: tuple[WriteScopeClaim, ...] = (),
) -> List[List[Lane]]:
    """Build write-scope-aware lanes into parallel batches.

    Extends ``build_batches`` with two guarantees from 009's claim machinery:

    * two lanes whose write scopes overlap are never placed in the same batch;
    * every lane is emitted with ``execution_mode`` ``fan-out`` (disjoint from
      all siblings and every held claim) or ``inline`` (overlaps a sibling or a
      held claim, so it must run alone).

    ``held_claims`` are the claims already held by other runs, as returned by
    ``store.list_write_scope_claims``. A lane overlapping a held claim is
    forced ``inline`` so it cannot fan out on top of a scope owned elsewhere.
    """
    batches = build_batches(tasks)
    held_scopes = [c.resolved_path for c in held_claims] if held_claims else []

    result: List[List[Lane]] = []
    for batch in batches:
        ordered = sorted(batch.tasks, key=lambda t: t.id)
        scopes = {t.id: _resolved_scopes(t) for t in ordered}

        inline_ids: List[str] = []
        fanout_ids: List[str] = []
        for task in ordered:
            own = scopes[task.id]
            siblings = [
                p for other in ordered if other.id != task.id
                for p in scopes[other.id]
            ]
            overlaps_sibling = _scope_conflicts(own, siblings)
            overlaps_held = _scope_conflicts(own, held_scopes)
            if overlaps_sibling or overlaps_held:
                inline_ids.append(task.id)
            else:
                fanout_ids.append(task.id)

        by_id = {t.id: t for t in ordered}

        # Fan-out lanes are mutually disjoint by construction and may share one
        # batch; each inline lane gets its own batch so no two overlapping
        # write scopes ever sit in the same batch. Id order keeps it
        # deterministic.
        group: List[Lane] = [Lane(task=by_id[tid], execution_mode=EXECUTION_MODE_FANOUT)
                              for tid in fanout_ids]
        if group:
            result.append(group)
        for tid in inline_ids:
            result.append([Lane(task=by_id[tid], execution_mode=EXECUTION_MODE_INLINE)])

    return result


class MissingSessionBoundaryError(Exception):
    """Raised when a sequence does not declare ``session_boundary``.

    This error exists so a boundary is never invented. Defaulting a
    boundary is the defect, not a convenience — a caller that forgets the key
    must be stopped, not silently given a wrong session split.
    """

    def __init__(self):
        super().__init__(
            "Sequence does not declare the required key 'session_boundary'; "
            "refusing rather than inventing a session boundary"
        )


@dataclass
class Sequence:
    """A run to schedule: the tasks plus the session boundary they belong to.

    ``session_boundary`` is required. A missing boundary is refused by
    ``build_sessions`` (``MissingSessionBoundaryError``), never defaulted.
    """

    tasks: List[Task] = field(default_factory=list)
    session_boundary: Optional[str] = None


@dataclass
class SessionBatch:
    """A batch that IS a session boundary.

    ``session_boundary`` is carried explicitly on the batch so a consumer
    reads the marker instead of inferring where one session ends and the next
    begins.
    """

    index: int
    lanes: List[Lane]
    session_boundary: str

    @property
    def task_ids(self) -> List[str]:
        return [lane.task_id for lane in self.lanes]


def build_sessions(
    sequence: Sequence,
    held_claims: tuple[WriteScopeClaim, ...] = (),
) -> List[SessionBatch]:
    """Build session boundary batches from a declared ``Sequence``.

    Each emitted batch is a session boundary and carries ``session_boundary``
    explicitly (criterion 7). Write-scope arbitration from ``build_lanes`` is
    preserved end to end, so a batch whose lanes overlap in write scope is
    never emitted as fan-out (criterion 10).

    A sequence whose ``session_boundary`` is missing or empty is refused with
    ``MissingSessionBoundaryError`` naming the key (criteria 8 and 10) — a
    boundary is never invented.
    """
    if not sequence.session_boundary:
        raise MissingSessionBoundaryError()

    lane_groups = build_lanes(sequence.tasks, held_claims=held_claims)

    sessions: List[SessionBatch] = []
    for index, group in enumerate(lane_groups):
        sessions.append(SessionBatch(
            index=index,
            lanes=group,
            session_boundary=sequence.session_boundary,
        ))
    return sessions


@dataclass
class Schedule:
    """A gated, parallel-capped session schedule.

    ``batches`` are the session boundary batches that may run, in order.
    ``blocked`` names every task (sorted) that was never released because one
    of its dependencies carried a gate that did not pass — transitively, so a
    task downstream of a blocked task is itself blocked. Blocked tasks are
    reported, never silently dropped (same philosophy as ``CyclicGraphError``).

    ``refusals`` names every ready lane that was *not* admitted by adaptive
    admission (SW-1510-ADAPTIVE criterion 1) with the dimension that refused it
    and the reason — reported, never silently dropped.  ``measured_max_parallel``
    is the parallelism measured from the fresh observations (``None`` when the
    caller fixed it or supplied no observations).
    """

    batches: List[SessionBatch]
    blocked: List[str]
    refusals: List["LaneRefusal"] = field(default_factory=list)
    measured_max_parallel: Optional[int] = None
    ledger: Optional["SelectionLedger"] = None


# ── SW-1510-ADAPTIVE (criterion 1): fresh observations ─────────────────────
#
# Admission is decided from *observed* dimensions that are handed in.  Each
# observation carries an ``observed_at`` instant and a ``freshness_seconds``
# window; an observation older than its window is stale and is refused, never
# assumed to still hold.  Nothing here is a literal: the route set and the
# reservation headroom come from the caller (the accepted FG-CAPACITY /
# FG-ADMISSION producers), not from a constant in this module.

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(observed_at: str, now: str) -> Optional[float]:
    """Age of an observation in seconds, or ``None`` when it cannot be parsed.

    An unparseable instant is treated as *not fresh* by the caller, so a
    malformed timestamp can never be read as a valid fresh observation.
    """
    try:
        then = datetime.fromisoformat(observed_at)
        reference = datetime.fromisoformat(now)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return (reference - then).total_seconds()


def _is_fresh(observed_at: str, freshness_seconds: float, now: str) -> bool:
    age = _age_seconds(observed_at, now)
    if age is None:
        return False
    return age <= freshness_seconds


class StaleObservationError(Exception):
    """Raised when a required observation is older than its freshness window.

    A stale route-capacity or reservation observation is refused rather than
    read as if it still held: inventing fresh capacity from a stale snapshot is
    the defect this error prevents.
    """

    def __init__(self, kind: str, observed_at: str, freshness_seconds: float):
        self.kind = kind
        self.observed_at = observed_at
        self.freshness_seconds = freshness_seconds
        super().__init__(
            f"{kind} observation from {observed_at!r} is stale "
            f"(freshness window {freshness_seconds}s)"
        )


@dataclass(frozen=True)
class RouteCapacitySnapshot:
    """A fresh observation of which provider routes currently admit work.

    ``available_routes`` are the ``provider/model`` routes observed as usable
    right now (from the owner-approved set, minus routes currently saturated).
    ``capacity`` is an optional map of route -> remaining concurrent capacity.
    ``observed_at`` + ``freshness_seconds`` bound how long the observation may
    be trusted; a construction with ``available_routes`` empty is an explicit
    "none available", not a missing value.
    """

    available_routes: frozenset[str] = field(default_factory=frozenset)
    capacity: Mapping[str, int] = field(default_factory=dict)
    observed_at: str = ""
    freshness_seconds: float = 0.0

    def is_fresh(self, now: Optional[str] = None) -> bool:
        return _is_fresh(self.observed_at, self.freshness_seconds, now or _now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_routes": sorted(self.available_routes),
            "capacity": dict(self.capacity),
            "observed_at": self.observed_at,
            "freshness_seconds": self.freshness_seconds,
        }


@dataclass(frozen=True)
class ReservationSnapshot:
    """A fresh observation of remaining reservation headroom.

    ``remaining_cost`` is the owner budget still available (``None`` = no hard
    cumulative limit — the explicit unlimited mode).  ``remaining_in_flight`` is
    how many in-flight slots remain.  Both are *remaining*, not totals.
    ``observed_at`` + ``freshness_seconds`` bound the observation.
    """

    remaining_cost: Optional[float] = None
    remaining_in_flight: int = 0
    observed_at: str = ""
    freshness_seconds: float = 0.0

    def is_fresh(self, now: Optional[str] = None) -> bool:
        return _is_fresh(self.observed_at, self.freshness_seconds, now or _now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "remaining_cost": self.remaining_cost,
            "remaining_in_flight": self.remaining_in_flight,
            "observed_at": self.observed_at,
            "freshness_seconds": self.freshness_seconds,
        }


#: Stable refusal-dimension codes, so a consumer reads a code not prose.
#: (The dependency and write-surface dimensions are admitted elsewhere: a task
#: held back by an unmet dependency is reported in ``Schedule.blocked`` and a
#: lane whose write scope overlaps a sibling or a held claim is admitted as an
#: ``inline`` lane that cannot fan out — neither is a route/reservation refusal.)
REFUSAL_ROUTE = "route-capability"
REFUSAL_RESERVATION = "reservation"
REFUSAL_ROUTE_STALE = "route-capability-stale"
REFUSAL_RESERVATION_STALE = "reservation-stale"
#: The measured parallelism is the number of lanes that may fan out at once.  A
#: fresh measurement of ``0`` admits no fan-out lane at all: every ready lane is
#: refused on this dimension rather than silently producing an empty schedule.
REFUSAL_PARALLELISM = "measured-parallelism"


@dataclass(frozen=True)
class LaneRefusal:
    """A ready lane that adaptive admission refused (criterion 1).

    ``dimension`` is one of the stable ``REFUSAL_*`` codes and ``reason`` is the
    human-readable cause.  A refusal is always reported, never dropped.
    """

    task_id: str
    dimension: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "dimension": self.dimension,
            "reason": self.reason,
        }


def measure_max_parallel(
    route_snapshot: Optional[RouteCapacitySnapshot],
    reservation_snapshot: Optional[ReservationSnapshot],
    *,
    now: Optional[str] = None,
) -> int:
    """Measure the maximum parallelism from fresh observations (criterion 1).

    The result is *measured*, never a fixed literal: it is the minimum of

    * the number of currently available routes (route capacity), and
    * the remaining in-flight reservation headroom.

    An observation older than its freshness window is refused with
    :class:`StaleObservationError` rather than read as still-true; a call with
    no fresh observation at all is refused too — parallelism that cannot be
    measured is never guessed.
    """
    reference = now or _now_iso()

    if route_snapshot is not None and not route_snapshot.is_fresh(reference):
        raise StaleObservationError(
            "route-capacity", route_snapshot.observed_at,
            route_snapshot.freshness_seconds,
        )
    if reservation_snapshot is not None and not reservation_snapshot.is_fresh(reference):
        raise StaleObservationError(
            "reservation", reservation_snapshot.observed_at,
            reservation_snapshot.freshness_seconds,
        )
    if route_snapshot is None and reservation_snapshot is None:
        raise StaleObservationError("route-capacity+reservation", "", 0.0)

    dimensions: List[int] = []
    if route_snapshot is not None:
        dimensions.append(len(route_snapshot.available_routes))
    if reservation_snapshot is not None:
        dimensions.append(max(0, reservation_snapshot.remaining_in_flight))
    return min(dimensions)


def _admit_by_budget(
    task: Task,
    reservation_snapshot: Optional[ReservationSnapshot],
    route_cost: Mapping[str, float],
) -> Optional[str]:
    """Return a refusal reason when a lane cannot be admitted by reservation.

    A lane is admitted only when the remaining reservation headroom is
    positive and, when both a route cost and a finite remaining budget are
    observed, the lane's cost fits.  ``remaining_cost is None`` is the explicit
    unlimited mode and never refuses.
    """
    if reservation_snapshot is None:
        return "no fresh reservation observation"
    if reservation_snapshot.remaining_in_flight <= 0:
        return (
            "no remaining in-flight reservation slots "
            f"(remaining_in_flight={reservation_snapshot.remaining_in_flight})"
        )
    if reservation_snapshot.remaining_cost is not None and task.route is not None:
        cost = route_cost.get(task.route)
        if cost is not None and cost > reservation_snapshot.remaining_cost:
            return (
                f"route cost {cost} exceeds remaining budget "
                f"{reservation_snapshot.remaining_cost}"
            )
    return None


def _split_by_max_parallel(
    lane_groups: TypingSequence[TypingSequence[Lane]],
    max_parallel: int,
) -> List[List[Lane]]:
    """Cap the number of lanes per group at ``max_parallel``.

    Only fan-out groups can hold more than one lane; inline groups hold exactly
    one and are left untouched. ``max_parallel`` is guaranteed >= 1 by the
    caller. Id order is preserved, so the split is deterministic.
    """
    split: List[List[Lane]] = []
    for group in lane_groups:
        group = list(group)
        if len(group) <= max_parallel:
            split.append(group)
            continue
        for start in range(0, len(group), max_parallel):
            split.append(group[start:start + max_parallel])
    return split


def _gate_receipt_releases(
    gate: str,
    receipt: Any,
) -> bool:
    """Decide whether one gate's ``independent-gate-receipt-v1`` releases deps.

    SW-1511-REVIEW criteria 1 and 3: the controller acceptance service's
    consumer seam.  A gate is released only when its bound receipt records an
    **accepted** attempt — the sole state that may release dependents — whose
    required producer artifact is present.  This is the same rule
    :func:`skillweave.trace.gate_receipt.release_dependents` enforces, applied
    to the durable record a consumer hands in.

    It is **fail-closed**: a receipt whose ``state`` is not the terminal
    acceptance state, or that carries *accepted* without its producer artifact,
    releases nothing (and a *launched* artifact-less attempt in particular
    cannot release its dependents).  An unsupported contract version is
    *refused* (:class:`UnsupportedContractVersion`), never coerced to v1.  A
    non-mapping payload is refused as an invalid receipt.
    """
    if not isinstance(receipt, Mapping):
        raise ValueError(
            f"gate {gate!r} carries a gate receipt that is not a mapping; "
            f"expected an independent-gate-receipt-v1 record"
        )
    # Fail-closed on any version this consumer does not support.
    validate_independent_gate_receipt(receipt)
    state = receipt.get("state")
    if state != ACCEPTANCE_TERMINAL:
        # Not yet accepted: the dependent is not released.
        return False
    if not receipt.get("accepted"):
        return False
    if not receipt.get("producer_artifact"):
        # An accepted state without its producer artifact is refused outright,
        # mirroring release_dependents (a launched skill that never produced
        # its artifact cannot release dependents).
        raise MissingProducerArtifact(
            str(receipt.get("attempt_id", "")), str(receipt.get("producer_artifact", ""))
        )
    return True


def build_schedule(
    sequence: Sequence,
    gate_results: Optional[Mapping[str, bool]] = None,
    max_parallel: Optional[int] = None,
    held_claims: tuple[WriteScopeClaim, ...] = (),
    *,
    route_capacity: Optional[RouteCapacitySnapshot] = None,
    reservations: Optional[ReservationSnapshot] = None,
    route_cost: Optional[Mapping[str, float]] = None,
    gate_receipts: Optional[Mapping[str, Any]] = None,
    now: Optional[str] = None,
) -> Schedule:
    """Build a gated, parallel-capped schedule from a ``Sequence``.

    Extends ``build_sessions`` with dispatch-4 guarantees:

    * ``max_parallel`` caps how many lanes share one fan-out batch (criterion
      3). When unset there is no cap; a value < 1 is refused.
    * a lane whose ``gate`` did not pass (missing or ``False`` in
      ``gate_results``) does NOT release its dependents (criterion 4). Blocked
      tasks — and anything downstream of them — are returned in
      ``Schedule.blocked`` rather than vanishing from the plan.

    SW-1511-REVIEW (criteria 1 and 3) adds the *controller acceptance* consumer
    seam, additively and opt-in: an optional ``gate_receipts`` mapping from a
    dependency task's ``gate`` to its ``independent-gate-receipt-v1`` record.  A
    gate backed by a receipt is released **only** when that receipt records the
    terminal *accepted* state with its required producer artifact present; a
    launched/running (or artifact-less) attempt therefore cannot release its
    dependents.  When ``gate_receipts`` is omitted, ``gate_results`` is
    consulted exactly as before and existing behaviour (and every v1 record) is
    unchanged.  An unsupported receipt version fails closed
    (:class:`~skillweave.trace.gate_receipt.UnsupportedContractVersion`), never
    coerced.

    SW-1510-ADAPTIVE (criterion 1) makes admission *adaptive*.  When fresh
    ``route_capacity`` and/or ``reservations`` observations are supplied:

    * a ready lane is admitted only when its declared route is in the fresh
      available route set and the fresh reservation headroom admits it; a
      refused lane is reported in ``Schedule.refusals`` with its dimension;
    * ``max_parallel``, when left unset, is *measured* from the fresh
      observations (``measure_max_parallel``) rather than fixed at a literal.

    When neither observation is supplied the legacy dispatch-4 behaviour is
    preserved exactly (no lane refused, ``max_parallel`` used as given).

    The scheduler still starts no process and knows no runner (criterion 6):
    gating, admission and parallelism are decided here declaratively, never by
    executing work.
    """
    if not sequence.session_boundary:
        raise MissingSessionBoundaryError()
    if max_parallel is not None and max_parallel < 1:
        raise ValueError(
            f"max_parallel must be >= 1 or None, got {max_parallel}"
        )
    reference = now or _now_iso()
    costs = dict(route_cost) if route_cost else {}
    ledger = SelectionLedger()
    adaptive = route_capacity is not None or reservations is not None

    measured: Optional[int] = None
    if adaptive and max_parallel is None:
        measured = measure_max_parallel(route_capacity, reservations, now=reference)
        max_parallel = measured

    results = dict(gate_results) if gate_results else {}
    receipts = dict(gate_receipts) if gate_receipts else {}

    # Layering, cycle/dupe/unknown-dep validation, and determinism come from
    # build_batches. Gating is a release condition layered on top: a dependent
    # is released only when every dependency is scheduled AND its gate passed.
    batches = build_batches(sequence.tasks)
    by_id = {t.id: t for t in sequence.tasks}

    blocked: set = set()
    for batch in batches:
        for task in batch.tasks:
            if task.id in blocked:
                continue
            for dep in task.depends_on:
                dep_task = by_id[dep]
                dep_blocked = dep in blocked
                gate = dep_task.gate
                if gate is not None and gate in receipts:
                    # SW-1511-REVIEW criteria 1/3: the controller acceptance
                    # consumer seam.  The receipt decides the release; a
                    # launched/artifact-less or non-accepted attempt does not.
                    gate_failed = not _gate_receipt_releases(gate, receipts[gate])
                else:
                    gate_failed = gate is not None and not results.get(gate, False)
                if dep_blocked or gate_failed:
                    blocked.add(task.id)
                    break

    unblocked = [t for t in sequence.tasks if t.id not in blocked]

    # SW-1510-ADAPTIVE criterion 1: adaptive admission.  A ready lane is
    # admitted against fresh route capability and remaining reservations; a
    # refused lane is reported, never dropped.  Dependency and write-surface
    # exclusion are already honoured: a dep-blocked task is in ``blocked`` and
    # an overlapping lane is admitted as ``inline`` (it cannot fan out).
    refusals: List[LaneRefusal] = []
    if adaptive and route_capacity is not None and not route_capacity.is_fresh(reference):
        raise StaleObservationError(
            "route-capacity", route_capacity.observed_at,
            route_capacity.freshness_seconds,
        )
    if adaptive and reservations is not None and not reservations.is_fresh(reference):
        raise StaleObservationError(
            "reservation", reservations.observed_at, reservations.freshness_seconds,
        )
    if adaptive:
        admitted: List[Task] = []
        for task in unblocked:
            # A measured parallelism of 0 admits no lane at all: the fresh
            # observation says there is no capacity to run anything, so every
            # ready lane is refused on this dimension rather than the schedule
            # silently collapsing to nothing.
            if max_parallel is not None and max_parallel < 1:
                refusals.append(LaneRefusal(
                    task.id, REFUSAL_PARALLELISM,
                    "measured parallelism is 0: no fresh route capacity or "
                    "in-flight reservation headroom to admit a lane",
                ))
                continue
            route_reason: Optional[str] = None
            if task.route is not None and route_capacity is not None:
                if task.route not in route_capacity.available_routes:
                    route_reason = (
                        f"route {task.route!r} is not in the fresh available "
                        f"route set {sorted(route_capacity.available_routes)}"
                    )
            if route_reason is not None:
                refusals.append(LaneRefusal(task.id, REFUSAL_ROUTE, route_reason))
                continue
            budget_reason = _admit_by_budget(task, reservations, costs)
            if budget_reason is not None:
                refusals.append(
                    LaneRefusal(task.id, REFUSAL_RESERVATION, budget_reason)
                )
                continue
            admitted.append(task)
        unblocked = admitted

    lane_groups = build_lanes(unblocked, held_claims=held_claims)
    if max_parallel is not None:
        lane_groups = _split_by_max_parallel(lane_groups, max_parallel)

    session_batches = [
        SessionBatch(index=i, lanes=list(group),
                     session_boundary=sequence.session_boundary)
        for i, group in enumerate(lane_groups)
    ]

    # Criterion 3: the admission decision is itself a recorded selection,
    # carrying the candidate set, the refused lanes (as rejected routes), the
    # measured parallelism and the outcome.
    if adaptive:
        ledger.record_selection(
            cause=(
                "adaptive admission: dependency + write-surface + fresh route "
                "capability/capacity + remaining reservations"
            ),
            candidates=sorted(t.id for t in unblocked),
            rejected_routes=sorted(r.task_id for r in refusals),
            budget_delta=0.0,
            outcome=(
                f"measured_max_parallel={measured}; "
                f"admitted={len(unblocked)}; refused={len(refusals)}"
            ),
        )

    return Schedule(
        batches=session_batches,
        blocked=sorted(blocked),
        refusals=refusals,
        measured_max_parallel=measured,
        ledger=ledger,
    )


# ═══════════════════════════════════════════════════════════════════════════
# SW-1510-ADAPTIVE (criterion 3): the decision ledger.
# ═══════════════════════════════════════════════════════════════════════════
#
# Every selection, escalation, split and backoff records its *cause*, the
# *candidate set* it chose from, the *rejected routes*, the *policy/catalog
# revision*, the *budget delta* and the *outcome*.  A quality-feedback signal
# never auto-accepts a graph: the only acceptance it can produce is one that
# still requires an independent gate.

KIND_SELECTION = "selection"
KIND_ESCALATION = "escalation"
KIND_SPLIT = "split"
KIND_BACKOFF = "backoff"

#: The two kinds that correspond to a bounded correction, and the error class
#: (from ``semantic_repair.ErrorClass``) whose permitted action they are.
KIND_TO_ERROR_CLASS: dict[str, str] = {
    KIND_SPLIT: "context_pressure",
    KIND_BACKOFF: "auth_quota",
}


@dataclass(frozen=True)
class AdaptiveDecisionRecord:
    """One recorded selection/escalation/split/backoff decision (criterion 3).

    Every field criterion 3 requires is present and explicit: ``cause`` (why the
    decision was taken), ``candidates`` (the candidate set), ``rejected_routes``
    (what was refused and named), ``policy_revision``/``catalog_revision`` (the
    revisions the decision was made under), ``budget_delta`` (the spend the
    decision incurs) and ``outcome`` (what happened).
    """

    kind: str
    cause: str
    candidates: tuple[str, ...] = ()
    rejected_routes: tuple[str, ...] = ()
    policy_revision: str = ""
    catalog_revision: str = ""
    budget_delta: float = 0.0
    outcome: str = ""
    subject: str = ""
    recorded_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cause": self.cause,
            "candidates": list(self.candidates),
            "rejected_routes": list(self.rejected_routes),
            "policy_revision": self.policy_revision,
            "catalog_revision": self.catalog_revision,
            "budget_delta": self.budget_delta,
            "outcome": self.outcome,
            "subject": self.subject,
            "recorded_at": self.recorded_at,
        }

    def to_attempt_event(
        self, *, run_id: str = "", task_id: str = "", principal: str = ""
    ) -> dict[str, Any]:
        """Export a split/backoff decision as a frozen ``attempt-event-v1``.

        The producer here is the adaptive scheduler; the consumer is the Assay
        journal (``AssayRepairController.record_correction``).  The schema id,
        version and the evidence digest are taken from the accepted repair
        module (:mod:`skillweave.runtime.semantic_repair`) so no schema is
        re-declared.  A kind that is not a bounded correction (selection,
        escalation) has no attempt-event form and is refused.
        """
        from . import semantic_repair as _sr

        error_class = KIND_TO_ERROR_CLASS.get(self.kind)
        if error_class is None:
            raise ValueError(
                f"kind {self.kind!r} is not a bounded correction and has no "
                f"attempt-event form (only {sorted(KIND_TO_ERROR_CLASS)} do)"
            )
        payload = {
            "error_class": error_class,
            "failure_class": self.kind,
            "failure_fingerprint": _digest({
                "kind": self.kind, "cause": self.cause, "subject": self.subject,
            }),
            "hypothesis": self.cause,
            "implicated_fields": list(self.candidates),
            "changed_input": {
                "candidates": list(self.candidates),
                "rejected_routes": list(self.rejected_routes),
            },
            "before_digest": _digest({"subject": self.subject}),
            "after_digest": _digest({
                "subject": self.subject, "outcome": self.outcome,
            }),
            "verifier_result": f"outcome:{self.outcome}",
            "outcome": self.outcome,
            "hold_reason": None,
            "correction_count": 1,
            "dispatch_identity": None,
            "command": f"adaptive:{self.kind}",
            "exit_code": 0,
        }
        return {
            "schema_id": _sr.ATTEMPT_EVENT_SCHEMA_ID,
            "schema_version": _sr.ATTEMPT_EVENT_SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": 1,
            "event_id": f"{run_id or 'adaptive'}:{self.kind}:{self.recorded_at}",
            "sequence": 1,
            "event_type": f"adaptive_{self.kind}",
            "measured_at": self.recorded_at,
            "issuer": "adaptive-scheduler",
            "principal": principal,
            "evidence_digest": _digest(payload),
            "payload": payload,
        }

    def to_route_decision(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        attempt: int = 1,
        reservation_id: str = "",
    ) -> dict[str, Any]:
        """Export this decision as a ``route-decision-v1`` contract payload.

        The payload is built by :func:`skillweave.routing.faigate_adapter.
        make_route_decision` (imported lazily so the scheduler keeps no import
        of the adapter), so the schema is not re-declared here.  The requested
        model is the first eligible candidate; the resolved provider/model are
        the decision subject when it is a ``provider/model`` route.
        """
        from skillweave.routing.faigate_adapter import make_route_decision

        requested = self.candidates[0] if self.candidates else ""
        provider, _, model = (self.subject or "").partition("/")
        decision = make_route_decision(
            correlation_id=f"{run_id or 'adaptive'}/{task_id or self.kind}",
            requested_alias=requested,
            resolved_provider=provider,
            resolved_model=model if provider else "",
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            reservation_id=reservation_id,
            attempt_order=list(self.candidates),
            attribution_status="pending",
            outcome=self.outcome or "pending",
            measured_at=self.recorded_at,
        )
        decision.catalog_revision = self.catalog_revision
        decision.policy_revision = self.policy_revision
        return decision.to_dict()


def _digest(value: Any) -> str:
    """A stable content digest over a canonical JSON encoding."""
    import hashlib
    import json

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SelectionLedger:
    """An append-only ledger of adaptive decisions (criterion 3).

    Each ``record_*`` records a decision with its cause, candidate set, rejected
    routes, policy/catalog revision, budget delta and outcome.  The ledger never
    mutates or drops a record — a consumer reads the whole chain or refuses it.
    """

    def __init__(
        self,
        *,
        policy_revision: str = "",
        catalog_revision: str = "",
    ) -> None:
        self.policy_revision = policy_revision
        self.catalog_revision = catalog_revision
        self.records: List[AdaptiveDecisionRecord] = []

    def _record(
        self,
        kind: str,
        *,
        cause: str,
        candidates: TypingSequence[str] = (),
        rejected_routes: TypingSequence[str] = (),
        budget_delta: float = 0.0,
        outcome: str = "",
        subject: str = "",
        policy_revision: Optional[str] = None,
        catalog_revision: Optional[str] = None,
    ) -> AdaptiveDecisionRecord:
        record = AdaptiveDecisionRecord(
            kind=kind,
            cause=cause,
            candidates=tuple(candidates),
            rejected_routes=tuple(rejected_routes),
            policy_revision=(
                self.policy_revision if policy_revision is None else policy_revision
            ),
            catalog_revision=(
                self.catalog_revision if catalog_revision is None else catalog_revision
            ),
            budget_delta=budget_delta,
            outcome=outcome,
            subject=subject,
        )
        self.records.append(record)
        return record

    def record_selection(self, **kw: Any) -> AdaptiveDecisionRecord:
        return self._record(KIND_SELECTION, **kw)

    def record_escalation(self, **kw: Any) -> AdaptiveDecisionRecord:
        return self._record(KIND_ESCALATION, **kw)

    def record_split(self, **kw: Any) -> AdaptiveDecisionRecord:
        return self._record(KIND_SPLIT, **kw)

    def record_backoff(self, **kw: Any) -> AdaptiveDecisionRecord:
        return self._record(KIND_BACKOFF, **kw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_revision": self.policy_revision,
            "catalog_revision": self.catalog_revision,
            "records": [r.to_dict() for r in self.records],
        }


@dataclass(frozen=True)
class QualityFeedback:
    """A quality signal observed for a graph/run (criterion 3).

    Advisory only: it may inform a later human/independent gate, but it can never
    accept a graph by itself.  ``passed``/``score`` are observations, not a
    verdict.
    """

    signal: str
    passed: bool = False
    score: float = 0.0
    provenance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "passed": self.passed,
            "score": self.score,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class AcceptanceDecision:
    """The (non-)acceptance derived from quality feedback (criterion 3)."""

    accepted: bool
    requires_gate: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "requires_gate": self.requires_gate,
            "reason": self.reason,
        }


def acceptance_from_quality_feedback(
    feedback: QualityFeedback,
) -> AcceptanceDecision:
    """Quality feedback never auto-accepts a graph (criterion 3).

    Whatever the signal says — pass or fail — the result is *not* an automatic
    acceptance: ``accepted`` is always ``False`` and ``requires_gate`` is always
    ``True``.  A passing quality signal still has to clear an independent gate;
    it is evidence, never a verdict.
    """
    return AcceptanceDecision(
        accepted=False,
        requires_gate=True,
        reason=(
            f"quality feedback signal {feedback.signal!r} "
            f"(passed={feedback.passed}) is advisory; an independent gate must "
            f"still accept the graph"
        ),
    )


__all__ = [
    "Task",
    "Batch",
    "Lane",
    "EXECUTION_MODE_FANOUT",
    "EXECUTION_MODE_INLINE",
    "CyclicGraphError",
    "UnknownDependencyError",
    "MissingSessionBoundaryError",
    "StaleObservationError",
    "Sequence",
    "SessionBatch",
    "Schedule",
    "RouteCapacitySnapshot",
    "ReservationSnapshot",
    "LaneRefusal",
    "measure_max_parallel",
    "build_batches",
    "build_lanes",
    "build_sessions",
    "build_schedule",
    "AdaptiveDecisionRecord",
    "SelectionLedger",
    "QualityFeedback",
    "AcceptanceDecision",
    "acceptance_from_quality_feedback",
    "KIND_SELECTION",
    "KIND_ESCALATION",
    "KIND_SPLIT",
    "KIND_BACKOFF",
    "KIND_TO_ERROR_CLASS",
    "REFUSAL_ROUTE",
    "REFUSAL_RESERVATION",
    "REFUSAL_ROUTE_STALE",
    "REFUSAL_RESERVATION_STALE",
    "REFUSAL_PARALLELISM",
]
