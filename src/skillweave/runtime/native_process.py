"""The native process service (SW-ORCH-001 criteria 1-3).

One native process service drives ready dispatch, independent review, repair,
re-review, controlled integration and dependent waves -- no external bridge is
required.  It owns three things the interactive controller used to hold in a
single process's memory:

* **durable dispatch intents** (criterion 2) -- one row per owned attempt,
  with a single *claiming* intent per execution scope; a second intent for the
  same scope is refused, so a duplicate launch never starts a second worker;
* **verified process/session identity** (criterion 2) -- the native process
  identity (:mod:`skillweave.runtime.process_identity`) is captured at launch
  and re-verified before any later action trusts the PID; only a ``match``
  authorises a takeover or signal, a recycled PID or a stale command is
  refused;
* **a single controller lease with fencing** (criterion 2) -- one controller
  at a time, carrying a monotonically increasing fencing token.  A recovering
  controller that takes over the lease reads a *higher* token; any write
  presented with a superseded token is refused, so a stale controller cannot
  be accepted.

Criterion 1's loop is expressed as a declared, ordered set of *steps*
(``STEPS``): ready dispatch, independent review, repair, re-review, controlled
integration and a dependent wave.  Each step is carried by a stored intent, so
the loop is durable and a recovered controller resumes it from the intents
rather than from a live process.

Criterion 3 (dynamic readiness refill) is served by :class:`ReadinessPlan` /
:func:`plan_readiness`: actual free capacity measured from fresh observations
(via ``dagscheduler.measure_max_parallel``) refills the ready set subject to
dependency order and write-surface exclusion, and the *worker count* (intents
launched) is reported separately from *useful progress* (intents that
completed), so a lane that launched and made no progress is never counted as
progress.

Boundary contracts
------------------
Two frozen v1 contracts live here, next to the classes that implement them
(the baseline task did not freeze a canonical URI, so the URI/revision/digest
are frozen at this seam):

* ``native-process-checkpoint-v1`` -- produced by the *controller lease
  holder*, consumed by a *recovering controller* (a ``storage`` boundary).
* ``owned-attempt-dispatch-v1`` -- produced by the *native process service*,
  consumed by a *harness adapter* (a ``process`` boundary).

Both are **additive and opt-in**: a record is only ever written when a caller
hands one to the service, and reading/validating an existing v1 record is
unchanged.  An unsupported version fails closed (:class:`UnsupportedContractVersion`)
and is never repaired into a shape this service happens to support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from . import process_identity
from .dagscheduler import (
    ReservationSnapshot,
    RouteCapacitySnapshot,
    Task,
    build_lanes,
    measure_max_parallel,
)
from .write_scope import WriteScopeClaim

__all__ = [
    # boundary contracts
    "CHECKPOINT_SCHEMA_ID",
    "CHECKPOINT_SCHEMA_VERSION",
    "SUPPORTED_CHECKPOINT_VERSIONS",
    "DISPATCH_SCHEMA_ID",
    "DISPATCH_SCHEMA_VERSION",
    "SUPPORTED_DISPATCH_VERSIONS",
    "UnsupportedContractVersion",
    "NativeProcessCheckpoint",
    "validate_native_process_checkpoint",
    "OwnedAttemptDispatch",
    "validate_owned_attempt_dispatch",
    # controlled loop
    "STEP_READY_DISPATCH",
    "STEP_INDEPENDENT_REVIEW",
    "STEP_REPAIR",
    "STEP_RE_REVIEW",
    "STEP_CONTROLLED_INTEGRATION",
    "STEP_DEPENDENT_WAVE",
    "STEPS",
    "INTENT_LAUNCHED",
    "INTENT_REVIEWING",
    "INTENT_REPAIRING",
    "INTENT_REVIEWED",
    "INTENT_INTEGRATING",
    "INTENT_INTEGRATED",
    "INTENT_FAILED",
    "INTENT_TERMINAL",
    "INTENT_STATES",
    "INTENT_CLAIMING_STATES",
    # lease
    "LEASE_KEY",
    "ControllerLease",
    "LeaseRefusedError",
    "StaleFencingTokenError",
    "LeaseNotHeldError",
    # dispatch intents
    "DispatchIntent",
    "DuplicateDispatchError",
    # readiness
    "ReadinessPlan",
    "plan_readiness",
    # the service
    "NativeProcessService",
    "NotControllerError",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 instant (UTC when naive); ``None`` when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ═══════════════════════════════════════════════════════════════════════════
# Boundary contracts: native-process-checkpoint-v1 and owned-attempt-dispatch-v1
# ═══════════════════════════════════════════════════════════════════════════
#
# The PRD freezes two draft v1 contracts for this task.  The baseline did not
# freeze a canonical URI/revision/digest, so those are frozen here, next to the
# classes that implement them.  Both contracts are additive and opt-in: an
# existing v1 record validates unchanged, and a record carrying any version
# this service does not support is refused rather than coerced.

CHECKPOINT_SCHEMA_ID = (
    "https://skillweave.dev/schemas/storage/native-process-checkpoint/v1"
)
CHECKPOINT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CHECKPOINT_VERSIONS = frozenset({CHECKPOINT_SCHEMA_VERSION})

DISPATCH_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/owned-attempt-dispatch/v1"
)
DISPATCH_SCHEMA_VERSION = "1.0.0"
SUPPORTED_DISPATCH_VERSIONS = frozenset({DISPATCH_SCHEMA_VERSION})


class UnsupportedContractVersion(ValueError):
    """A boundary record carries a version this service does not support.

    The contract is fail-closed: an unknown version is refused, never
    upgraded, downgraded or read as a best-effort approximation of a supported
    one.  *artifact* names the contract (``native-process-checkpoint-v1`` or
    ``owned-attempt-dispatch-v1``).
    """

    def __init__(self, artifact: str, version: Any, supported: frozenset):
        self.artifact = artifact
        self.version = version
        self.supported = supported
        super().__init__(
            f"unsupported {artifact} schema_version {version!r}; expected one "
            f"of {sorted(supported)}"
        )


@dataclass(frozen=True)
class NativeProcessCheckpoint:
    """A ``native-process-checkpoint-v1`` record.

    Produced by the **controller lease holder** for a **recovering
    controller**: it is the durable state a fresh controller needs to resume
    the owned process without the old controller's live memory -- which
    attempts are in flight under which fencing token, and where the journal
    cursor sat.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the checkpoint must name the run and the fencing token it was written
      under (a token-less checkpoint cannot be fenced against);
    * the journal offset is a non-negative integer.
    """

    run_id: str
    fencing_token: int
    journal_offset: int = 0
    in_flight: tuple[tuple[str, str], ...] = ()
    route: str = ""
    session_id: str = ""
    captured_at: str = field(default_factory=_now_iso)
    schema_version: str = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CHECKPOINT_VERSIONS:
            raise UnsupportedContractVersion(
                "native-process-checkpoint-v1", self.schema_version,
                SUPPORTED_CHECKPOINT_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("native-process checkpoint must name its run_id")
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int):
            raise ValueError(
                "native-process checkpoint must carry an integer fencing_token"
            )
        if self.fencing_token < 0:
            raise ValueError(
                "native-process checkpoint fencing_token must be >= 0"
            )
        if isinstance(self.journal_offset, bool) or not isinstance(self.journal_offset, int):
            raise ValueError(
                "native-process checkpoint journal_offset must be an integer"
            )
        if self.journal_offset < 0:
            raise ValueError(
                "native-process checkpoint journal_offset must be >= 0"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CHECKPOINT_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "fencing_token": self.fencing_token,
            "journal_offset": self.journal_offset,
            "in_flight": [list(pair) for pair in self.in_flight],
            "route": self.route,
            "session_id": self.session_id,
            "captured_at": self.captured_at,
        }


def validate_native_process_checkpoint(record: Mapping[str, Any]) -> None:
    """Refuse a checkpoint record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered checkpoint is refused rather than
    trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_CHECKPOINT_VERSIONS:
        raise UnsupportedContractVersion(
            "native-process-checkpoint-v1", version, SUPPORTED_CHECKPOINT_VERSIONS
        )
    if record.get("schema_id") != CHECKPOINT_SCHEMA_ID:
        raise ValueError(
            f"native-process checkpoint schema_id {record.get('schema_id')!r} "
            f"is not {CHECKPOINT_SCHEMA_ID!r}"
        )
    in_flight = record.get("in_flight") or ()
    NativeProcessCheckpoint(
        run_id=record.get("run_id", ""),
        fencing_token=record.get("fencing_token", -1),
        journal_offset=record.get("journal_offset", 0),
        in_flight=tuple(tuple(pair) for pair in in_flight),
        route=record.get("route", ""),
        session_id=record.get("session_id", ""),
        captured_at=record.get("captured_at", ""),
        schema_version=version,
    )


@dataclass(frozen=True)
class OwnedAttemptDispatch:
    """An ``owned-attempt-dispatch-v1`` record.

    Produced by the **native process service** for a **harness adapter**: it is
    the typed statement of one dispatched owned attempt -- which run/task/
    attempt it is, on which route/session, under which controller fencing
    token, carrying which verified process identity.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the attempt is 1-based and the identity fields are present;
    * ``fencing_token`` is a non-negative integer;
    * a dispatched record that carries a process identity must carry the
      *verified* identity facts (pid + start_time), never a bare PID.
    """

    run_id: str
    task_id: str
    attempt: int
    fencing_token: int
    route: str = ""
    session_id: str = ""
    process_identity: Optional[dict] = None
    dispatched_at: str = field(default_factory=_now_iso)
    schema_version: str = DISPATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_DISPATCH_VERSIONS:
            raise UnsupportedContractVersion(
                "owned-attempt-dispatch-v1", self.schema_version,
                SUPPORTED_DISPATCH_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("owned-attempt dispatch must name its run_id")
        if not self.task_id:
            raise ValueError("owned-attempt dispatch must name its task_id")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise ValueError("owned-attempt dispatch attempt must be an integer")
        if self.attempt < 1:
            raise ValueError(
                f"owned-attempt dispatch attempt {self.attempt!r} must be >= 1 "
                f"(the attempt number is 1-based)"
            )
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int):
            raise ValueError(
                "owned-attempt dispatch must carry an integer fencing_token"
            )
        if self.fencing_token < 0:
            raise ValueError("owned-attempt dispatch fencing_token must be >= 0")
        # A dispatched record that claims a process identity must carry the
        # *verified* identity, never a bare PID (a PID alone is not identity).
        if self.process_identity is not None:
            if not isinstance(self.process_identity, Mapping):
                raise ValueError(
                    "owned-attempt dispatch process_identity must be a mapping"
                )
            if process_identity.ProcessIdentity.from_dict(self.process_identity) is None:
                raise ValueError(
                    "owned-attempt dispatch process_identity must carry the "
                    "verified facts (pid + start_time), not a bare PID"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": DISPATCH_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "fencing_token": self.fencing_token,
            "route": self.route,
            "session_id": self.session_id,
            "process_identity": self.process_identity,
            "dispatched_at": self.dispatched_at,
        }


def validate_owned_attempt_dispatch(record: Mapping[str, Any]) -> None:
    """Refuse a dispatch record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id); reconstructs the frozen record so a tampered dispatch
    (a bare PID posing as identity, an attempt of 0) is refused.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_DISPATCH_VERSIONS:
        raise UnsupportedContractVersion(
            "owned-attempt-dispatch-v1", version, SUPPORTED_DISPATCH_VERSIONS
        )
    if record.get("schema_id") != DISPATCH_SCHEMA_ID:
        raise ValueError(
            f"owned-attempt dispatch schema_id {record.get('schema_id')!r} is "
            f"not {DISPATCH_SCHEMA_ID!r}"
        )
    OwnedAttemptDispatch(
        run_id=record.get("run_id", ""),
        task_id=record.get("task_id", ""),
        attempt=record.get("attempt", 0),
        fencing_token=record.get("fencing_token", -1),
        route=record.get("route", ""),
        session_id=record.get("session_id", ""),
        process_identity=record.get("process_identity"),
        dispatched_at=record.get("dispatched_at", ""),
        schema_version=version,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 1: the controlled loop
# ═══════════════════════════════════════════════════════════════════════════
#
# The native process service drives the whole loop.  The steps are declared in
# order so the loop is data, not implicit control flow in a live process: a
# recovering controller reads the stored intents and knows exactly where the
# process is.  No step is an external bridge.

STEP_READY_DISPATCH = "ready_dispatch"
STEP_INDEPENDENT_REVIEW = "independent_review"
STEP_REPAIR = "repair"
STEP_RE_REVIEW = "re_review"
STEP_CONTROLLED_INTEGRATION = "controlled_integration"
STEP_DEPENDENT_WAVE = "dependent_wave"

#: The ordered loop one native process service drives (criterion 1).  Every
#: step is carried by a durable intent; the order is the declaration of what
#: "beyond the interactive controller" means here.
STEPS: tuple[str, ...] = (
    STEP_READY_DISPATCH,
    STEP_INDEPENDENT_REVIEW,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
)

# Intent lifecycle states.  ``LAUNCHED``, ``REVIEWING``, ``REPAIRING`` and
# ``INTEGRATING`` are the *claiming* states: an attempt sitting in one of them
# is in flight, so a second intent for the same execution scope would be a
# duplicate launch and is refused.
INTENT_LAUNCHED = "launched"
INTENT_REVIEWING = "reviewing"
INTENT_REPAIRING = "repairing"
INTENT_REVIEWED = "reviewed"
INTENT_INTEGRATING = "integrating"
INTENT_INTEGRATED = "integrated"
INTENT_FAILED = "failed"

INTENT_STATES: frozenset[str] = frozenset({
    INTENT_LAUNCHED,
    INTENT_REVIEWING,
    INTENT_REPAIRING,
    INTENT_REVIEWED,
    INTENT_INTEGRATING,
    INTENT_INTEGRATED,
    INTENT_FAILED,
})

INTENT_CLAIMING_STATES: frozenset[str] = frozenset({
    INTENT_LAUNCHED,
    INTENT_REVIEWING,
    INTENT_REPAIRING,
    INTENT_INTEGRATING,
})

#: Intent states that count as *real useful progress* (criterion 3).  A worker
#: that launched and has made no progress is not progress; only a reviewed,
#: integrated or explicitly-failed (resolved) attempt counts.
INTENT_TERMINAL: frozenset[str] = frozenset({
    INTENT_REVIEWED,
    INTENT_INTEGRATED,
    INTENT_FAILED,
})


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: the single controller lease with fencing
# ═══════════════════════════════════════════════════════════════════════════

class LeaseRefusedError(Exception):
    """A controller lease was refused (another live controller holds it).

    A live lease is never silently stolen: the refusing caller must wait for
    the holder's lease to expire or be released, then take over and receive a
    strictly higher fencing token.
    """

    def __init__(self, holder: str, lease_until: Optional[str]):
        self.holder = holder
        self.lease_until = lease_until
        super().__init__(
            f"controller lease is held by {holder!r} until {lease_until!r}; "
            f"a live lease is never silently stolen"
        )


class StaleFencingTokenError(Exception):
    """A write presented a fencing token that has been superseded.

    The durable effect of a stale controller is refused: its token is lower
    than the token currently held, so it is no longer the controller.
    """

    def __init__(self, presented: int, current: int):
        self.presented = presented
        self.current = current
        super().__init__(
            f"stale controller fencing token {presented}: the current token is "
            f"{current}; a superseded controller's write is refused"
        )


class LeaseNotHeldError(Exception):
    """A write presented a token for a lease that is not currently held."""

    def __init__(self, presented: int, holder: Optional[str]):
        self.presented = presented
        self.holder = holder
        super().__init__(
            f"fencing token {presented} does not correspond to a held lease "
            f"(current holder: {holder!r})"
        )


class DuplicateDispatchError(Exception):
    """A second *claiming* intent was offered for an execution scope.

    A scope already carries a live owned attempt, so a second launch would be
    a duplicate worker.  The duplicate is refused; the caller must observe the
    existing intent instead.
    """

    def __init__(self, execution_scope: str, intent_id: str, run_id: str):
        self.execution_scope = execution_scope
        self.intent_id = intent_id
        self.run_id = run_id
        super().__init__(
            f"execution scope {execution_scope!r} is already claimed by intent "
            f"{intent_id!r} (run {run_id!r}); a duplicate launch is refused"
        )


#: The single lease lane.  There is exactly one controller lease per run store;
#: the key is stated (not implied) so the invariant "one controller" is data.
LEASE_KEY = "controller"


@dataclass(frozen=True)
class ControllerLease:
    """The single controller lease and its fencing token.

    Exactly one controller holds the lease at a time.  The ``fencing_token``
    strictly increases on every acquisition, so a recovering controller that
    takes over reads a higher token than the controller it replaced; any write
    presented with an older token is refused (:class:`StaleFencingTokenError`).
    This is what prevents *stale-controller acceptance*.
    """

    holder: str
    fencing_token: int
    lease_until: Optional[str] = None
    acquired_at: str = ""
    released_at: Optional[str] = None

    @property
    def held(self) -> bool:
        return self.released_at is None

    def is_expired(self, now: Optional[str] = None) -> bool:
        """A held lease is expired when its deadline is in the past."""
        if self.released_at is not None or self.lease_until is None:
            return False
        reference = now or _now_iso()
        until = _parse_iso(self.lease_until)
        current = _parse_iso(reference)
        if until is None or current is None:
            # An undatable deadline is treated as expired (fail closed): the
            # caller cannot prove the lease is live, so it is not.
            return True
        return current >= until

    def to_dict(self) -> dict[str, Any]:
        return {
            "holder": self.holder,
            "fencing_token": self.fencing_token,
            "lease_until": self.lease_until,
            "acquired_at": self.acquired_at,
            "released_at": self.released_at,
            "held": self.held,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: durable dispatch intents
# ═══════════════════════════════════════════════════════════════════════════
#
# One row per owned attempt.  The exit from a *claiming* state (launched ->
# reviewing -> repairing -> ... ) releases the execution scope, so the scope
# is claimed for as long as an owned attempt is live and no longer.  A second
# claiming intent for the same scope is refused (:class:`DuplicateDispatchError`
# at the service, a partial unique index at the store), so a duplicate launch
# cannot start a second worker.

@dataclass(frozen=True)
class DispatchIntent:
    """A durable record of one owned attempt's dispatch lifecycle.

    ``execution_scope`` is the modelled key the duplicate-launch guard is
    anchored to (the caller's stable "which work is this" identity, e.g.
    ``run_id/task_id/attempt``).  ``state`` is one of :data:`INTENT_STATES`;
    the intent *claims* its scope only while it sits in a
    :data:`INTENT_CLAIMING_STATES` state.  ``fencing_token`` records which
    controller held the lease when the attempt was dispatched, so a recovered
    intent is not silently re-owned by a stale controller.
    """

    intent_id: str
    run_id: str
    task_id: str
    attempt: int
    execution_scope: str
    step: str
    state: str
    fencing_token: int
    route: str = ""
    session_id: str = ""
    process_identity: Optional[dict] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @property
    def claiming(self) -> bool:
        return self.state in INTENT_CLAIMING_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "execution_scope": self.execution_scope,
            "step": self.step,
            "state": self.state,
            "fencing_token": self.fencing_token,
            "route": self.route,
            "session_id": self.session_id,
            "process_identity": self.process_identity,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: dynamic readiness refill
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ReadinessPlan:
    """The readiness decision for one refill of ready lanes (criterion 3).

    ``admitted`` are the ready task ids admitted against actual free capacity
    this refill; ``refused`` maps a ready task id to the dimension that
    refused it (reusing the ``dagscheduler`` ``REFUSAL_*`` codes).  A task
    held back by an unmet dependency is not present in ``admitted`` (dependency
    excitation is honoured by :func:`plan_readiness`), and a lane whose write
    scope overlaps an already-admitted lane is admitted *inline* (it cannot fan
    out) -- both exclusions are the *dependency/write-surface exclusion* the
    criterion names.

    ``capacity_available`` is the measured maximum parallelism for this refill
    (``None`` when no fresh observation was supplied, i.e. an unconditional
    refill).  ``worker_count`` is how many lanes were admitted (workers
    started), ``useful_progress`` is how many already-completed attempts that
    run has produced -- deliberately *not* a function of ``worker_count``, so a
    refill that starts workers but makes no progress is distinguishable from
    one that does.
    """

    admitted: tuple[str, ...]
    refused: Mapping[str, str]
    capacity_available: Optional[int]
    worker_count: int
    useful_progress: int
    inline: tuple[str, ...] = ()
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": list(self.admitted),
            "refused": dict(self.refused),
            "capacity_available": self.capacity_available,
            "worker_count": self.worker_count,
            "useful_progress": self.useful_progress,
            "inline": list(self.inline),
            "observed_at": self.observed_at,
        }


def plan_readiness(
    tasks: Sequence[Task],
    *,
    done: Sequence[str],
    route_capacity: Optional[RouteCapacitySnapshot] = None,
    reservations: Optional[ReservationSnapshot] = None,
    held_claims: Sequence[WriteScopeClaim] = (),
    now: Optional[str] = None,
) -> ReadinessPlan:
    """Refill readiness from *actual* free capacity (SW-ORCH-001 criterion 3).

    Engages exactly when fresh capacity observations are supplied --
    ``route_capacity`` and/or ``reservations``.  When neither is given this
    returns an empty plan with ``capacity_available=None``: refill is opt-in,
    so a caller that does not supply observations is never silently given
    invented capacity.

    The refill is subject to:

    * **dependency exclusion** -- a task is *ready* only when every task it
      depends on is in ``done``; a not-ready task is never admitted (it is a
      dependency refusal, not a capacity one, and is reported under
      ``REFUSAL_DEPENDENCY``);
    * **write-surface exclusion** -- two ready lanes whose write scopes overlap
      are never fanned out together (``dagscheduler.build_lanes``); the
      overlapping lane is admitted *inline* and reported in ``inline``;
    * **actual free capacity** -- the number of admitted fan-out lanes is
      capped at the *measured* maximum parallelism
      (``dagscheduler.measure_max_parallel`` over the fresh observations), not
      a fixed literal.  A measured capacity of 0 admits no lane and reports
      every ready lane as refused on the parallelism dimension.
    """
    reference = now or _now_iso()
    capacity: Optional[int] = None
    if route_capacity is not None or reservations is not None:
        capacity = measure_max_parallel(route_capacity, reservations, now=reference)

    done_set = set(done)
    refused: dict[str, str] = {}

    ready: list[Task] = []
    for task in tasks:
        if task.id in done_set:
            # Already-completed work is never re-admitted: a completed task is
            # progress that has happened, not a worker to start again.
            continue
        unmet = [d for d in task.depends_on if d not in done_set]
        if unmet:
            refused[task.id] = f"dependency:{','.join(sorted(unmet))}"
            continue
        ready.append(task)

    # No fresh observation -> no refill (opt-in): report readiness but admit
    # nothing, and never invent capacity.
    if capacity is None:
        return ReadinessPlan(
            admitted=(),
            refused=refused,
            capacity_available=None,
            worker_count=0,
            useful_progress=len(done_set),
            inline=(),
            observed_at=reference,
        )

    if capacity < 1:
        for task in ready:
            refused[task.id] = "measured-parallelism"
        return ReadinessPlan(
            admitted=(),
            refused=refused,
            capacity_available=capacity,
            worker_count=0,
            useful_progress=len(done_set),
            inline=(),
            observed_at=reference,
        )

    # Write-surface exclusion.  A task is *ready* only when every dependency is
    # already done, so the ready set is a mutually-independent frontier; its
    # dependencies are satisfied and no longer part of the graph this refill
    # arbitrates.  Strip them before asking ``build_lanes`` to group the
    # frontier by write scope (otherwise a satisfied dependency would be an
    # unknown node in the lane graph).
    frontier = [
        Task(
            id=t.id, depends_on=[], write_scope=list(t.write_scope),
            gate=t.gate, route=t.route,
        )
        for t in ready
    ]
    lane_groups = build_lanes(frontier, held_claims=tuple(held_claims))
    ordered_lanes = [lane for group in lane_groups for lane in group]

    admitted_ids: list[str] = []
    inline_ids: list[str] = []
    fanout_count = 0
    for lane in ordered_lanes:
        tid = lane.task_id
        if lane.execution_mode == "inline":
            # A lane whose scope overlaps another ready lane or a held claim
            # may still run, but inline: it cannot fan out on top of an owned
            # scope, and it is counted against nothing (it runs alone).
            if len(admitted_ids) >= capacity:
                refused[tid] = "measured-parallelism"
                continue
            admitted_ids.append(tid)
            inline_ids.append(tid)
            continue
        if fanout_count >= capacity:
            refused[tid] = "measured-parallelism"
            continue
        fanout_count += 1
        admitted_ids.append(tid)

    return ReadinessPlan(
        admitted=tuple(sorted(admitted_ids)),
        refused=refused,
        capacity_available=capacity,
        worker_count=len(admitted_ids),
        useful_progress=len(done_set),
        inline=tuple(sorted(inline_ids)),
        observed_at=reference,
    )


# ═══════════════════════════════════════════════════════════════════════════
# The native process service (criterion 1)
# ═══════════════════════════════════════════════════════════════════════════
#
# One service drives the whole loop over the durable store.  It never holds a
# boundary record in its own memory: every step runs through a stored intent,
# every dispatch carries the verified process identity, and every durable
# effect is fenced by the single controller lease -- so a recovering controller
# takes over from the store, not from a live process, and no external bridge is
# required.  The store is duck-typed (``store`` imports this module, so this
# module must not import ``store``): the service names the operations it needs.


class NotControllerError(Exception):
    """The service tried to act while it does not hold the controller lease."""

    def __init__(self, holder: Optional[str], fencing_token: Optional[int]):
        self.holder = holder
        self.fencing_token = fencing_token
        super().__init__(
            f"this service is not the controller (holder: {holder!r}, token: "
            f"{fencing_token!r}); a stale controller's action is refused"
        )


class NativeProcessService:
    """The single native process service that drives the owned loop.

    It owns the controller lease, records every attempted step as a durable
    dispatch intent (refusing a duplicate launch of the same execution scope),
    binds each dispatch to the verified process identity, and -- on takeover --
    re-verifies the live identity before trusting an in-flight intent.  The
    loop it drives is :data:`STEPS`.
    """

    def __init__(self, store: Any, holder: str) -> None:
        self._store = store
        self.holder = holder
        self._lease: Optional[ControllerLease] = None

    # -- lease -----------------------------------------------------------

    @property
    def fencing_token(self) -> Optional[int]:
        return None if self._lease is None else self._lease.fencing_token

    def acquire(self, ttl_seconds: int = 300) -> ControllerLease:
        """Acquire the single controller lease (refuses a live holder)."""
        self._lease = self._store.acquire_controller_lease(
            self.holder, ttl_seconds=ttl_seconds
        )
        return self._lease

    def renew(self, ttl_seconds: int = 300) -> bool:
        self._require_controller()
        return self._store.heartbeat_controller_lease(
            self.holder, self._lease.fencing_token, ttl_seconds=ttl_seconds
        )

    def release(self) -> bool:
        self._require_controller()
        try:
            released = self._store.release_controller_lease(self._lease.fencing_token)
        finally:
            self._lease = None
        return released

    def _require_controller(self) -> None:
        if self._lease is None:
            raise NotControllerError(None, None)
        lease = self._store.get_controller_lease()
        if (
            lease is None
            or not lease.held
            or lease.fencing_token != self._lease.fencing_token
        ):
            raise NotControllerError(
                None if lease is None else lease.holder,
                None if lease is None else lease.fencing_token,
            )

    # -- the driven loop (criterion 1) -----------------------------------

    def dispatch(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        step: str = STEP_READY_DISPATCH,
        route: str = "",
        session_id: str = "",
        process_identity: Optional[dict] = None,
        execution_scope: Optional[str] = None,
    ) -> tuple[DispatchIntent, OwnedAttemptDispatch]:
        """Record a durable dispatch intent and its boundary dispatch record.

        Refuses (never launches) when this service is not the controller, or
        when the execution scope already carries a claiming intent (a duplicate
        launch).  The returned ``owned-attempt-dispatch-v1`` record is produced
        by this native process service for the harness adapter.
        """
        self._require_controller()
        if step not in STEPS:
            raise ValueError(f"{step!r} is not one of the driven steps {STEPS}")
        scope = execution_scope or f"{run_id}/{task_id}/{attempt}"
        token = self._lease.fencing_token
        intent = self._store.record_dispatch_intent(
            run_id, task_id, attempt, scope,
            step=step, state=INTENT_LAUNCHED, fencing_token=token,
            route=route, session_id=session_id, process_identity=process_identity,
        )
        dispatch = OwnedAttemptDispatch(
            run_id=run_id, task_id=task_id, attempt=attempt, fencing_token=token,
            route=route, session_id=session_id, process_identity=process_identity,
        )
        return intent, dispatch

    def advance(
        self, intent_id: str, state: str, *, step: Optional[str] = None
    ) -> DispatchIntent:
        """Advance one intent along the loop; fenced by the controller lease."""
        self._require_controller()
        return self._store.advance_dispatch_intent(intent_id, state, step=step)

    def record_review(self, intent_id: str) -> DispatchIntent:
        """Independent review completed for an owned attempt."""
        return self.advance(intent_id, INTENT_REVIEWED, step=STEP_INDEPENDENT_REVIEW)

    def record_repair(self, intent_id: str) -> DispatchIntent:
        """A repair round started for an owned attempt (re-review follows)."""
        return self.advance(intent_id, INTENT_REPAIRING, step=STEP_REPAIR)

    def record_re_review(self, intent_id: str) -> DispatchIntent:
        """Re-review completed after a repair round."""
        return self.advance(intent_id, INTENT_REVIEWED, step=STEP_RE_REVIEW)

    def record_integration(self, intent_id: str) -> DispatchIntent:
        """Controlled integration completed for an owned attempt."""
        return self.advance(
            intent_id, INTENT_INTEGRATED, step=STEP_CONTROLLED_INTEGRATION
        )

    def record_dependent_wave(self, intent_id: str) -> DispatchIntent:
        """A dependent wave was released after this attempt integrated."""
        return self.advance(intent_id, INTENT_INTEGRATED, step=STEP_DEPENDENT_WAVE)

    def record_failure(self, intent_id: str) -> DispatchIntent:
        """Record a resolved (failed) outcome; the execution scope is freed.

        A failed attempt is *resolved*: it is a terminal outcome, so its scope
        is released and a later attempt on the same scope is admitted.
        """
        self._require_controller()
        return self._store.fail_dispatch_intent(intent_id)

    # -- verified identity on takeover -----------------------------------

    def verify_owned_attempt(self, intent: DispatchIntent) -> "process_identity.IdentityVerification":
        """Re-verify an in-flight intent's recorded identity against the OS.

        Only a ``match`` authorises trusting (or signalling) the recorded
        process; a recycled PID, a stale command, a foreign worker or an
        unobservable process is reported distinctly and never treated as the
        owned attempt.
        """
        recorded = intent.process_identity
        if not recorded:
            return process_identity.verify(None, 0)
        pid = int(recorded.get("pid", 0))
        return process_identity.verify(recorded, pid)

    # -- recovery (native-process-checkpoint-v1) -------------------------

    def checkpoint(self) -> NativeProcessCheckpoint:
        """Produce the checkpoint a *recovering controller* resumes from.

        The in-flight set is read from the durable intents -- never from this
        process's memory -- so a controller that lost the lease leaves a
        resumable statement of what was owned and under which fencing token.
        """
        in_flight = tuple(
            (i.execution_scope, i.step)
            for i in self._store.list_dispatch_intents(claiming_only=True)
        )
        run_id = self._run_id_label()
        return NativeProcessCheckpoint(
            run_id=run_id,
            fencing_token=(
                self._lease.fencing_token
                if self._lease is not None
                else self._store.current_fencing_token()
            ),
            journal_offset=self._store.last_journal_sequence(run_id),
            in_flight=in_flight,
        )

    def resume(self, *, ttl_seconds: int = 300) -> tuple[ControllerLease, NativeProcessCheckpoint]:
        """Take over the lease and read the checkpoint to resume the loop.

        The fencing token read here is strictly higher than the predecessor's,
        and the returned checkpoint lists exactly the in-flight intents of the
        prior controller -- this service resumes from durable state.
        """
        lease = self.acquire(ttl_seconds=ttl_seconds)
        return lease, self.checkpoint()

    def _run_id_label(self) -> str:
        """A stable label for the run this service is driving."""
        intents = self._store.list_dispatch_intents()
        if intents:
            return intents[0].run_id
        return self.holder

    # -- readiness refill (criterion 3) ----------------------------------

    def readiness(
        self,
        tasks: Sequence[Task],
        *,
        done: Sequence[str],
        route_capacity: Optional[RouteCapacitySnapshot] = None,
        reservations: Optional[ReservationSnapshot] = None,
        now: Optional[str] = None,
    ) -> ReadinessPlan:
        """Refill ready lanes from actual free capacity, holding write claims.

        The held write-scope claims are read from the store so a ready lane
        overlapping a scope already owned elsewhere is admitted only *inline*
        (write-surface exclusion), and dependency order is honoured by
        :func:`plan_readiness`.
        """
        held = tuple(self._store.list_write_scope_claims())
        return plan_readiness(
            tasks, done=done, route_capacity=route_capacity,
            reservations=reservations, held_claims=held, now=now,
        )
