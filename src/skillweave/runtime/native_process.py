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

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
import hashlib
import json

from . import process_identity
from .dagscheduler import (
    ReservationSnapshot,
    RouteCapacitySnapshot,
    Task,
    build_lanes,
    measure_max_parallel,
)
from .write_scope import WriteScopeClaim
from ..trace.gate_receipt import (
    ACCEPTANCE_ACCEPTED,
    ACCEPTANCE_STATES,
    IndependentGateReceipt,
    MissingProducerArtifact,
    collect_package_review,
    release_dependents,
)

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
    # criterion 4: the compact changed-state/evidence view
    "EVIDENCE_VIEW_SCHEMA_ID",
    "EVIDENCE_VIEW_SCHEMA_VERSION",
    "SUPPORTED_EVIDENCE_VIEW_VERSIONS",
    "ACCEPTED_GATE_STATUSES",
    "EvidenceDelta",
    "validate_evidence_delta",
    "build_evidence_delta",
    "evidence_view_fingerprint",
    "evidence_view_header",
    "ARCHITECT_HEALTHY",
    "ARCHITECT_QUOTA_EXHAUSTED",
    "ContinuationDecision",
    "plan_continuation",
    # criterion 5: repository and non-repository subjects
    "CONTENT_TXN_SCHEMA_ID",
    "CONTENT_TXN_SCHEMA_VERSION",
    "SUPPORTED_CONTENT_TXN_VERSIONS",
    "INTEGRATION_GIT_MERGE",
    "INTEGRATION_CONTENT_TRANSACTION",
    "NON_REPOSITORY_INTEGRATION_KINDS",
    "ContentTransaction",
    "validate_content_transaction",
    "subject_kind_of",
    "is_repository_subject",
    "integration_mode_for",
    "resolve_integration",
    "UnknownSubjectKind",
    "SubjectMismatchError",
    "WrongIntegrationModeError",
    # controlled loop
    "STEP_READY_DISPATCH",
    "STEP_INDEPENDENT_REVIEW",
    "STEP_REPAIR",
    "STEP_RE_REVIEW",
    "STEP_CONTROLLED_INTEGRATION",
    "STEP_DEPENDENT_WAVE",
    # SW-1511-REVIEW criteria 1 and 3: controller acceptance consumer seam
    "record_acceptance",
    "release_accepted_dependents",
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
# Criterion 4: the compact changed-state/evidence view
# ═══════════════════════════════════════════════════════════════════════════
#
# ``evidence-view-v1`` is the compact view (the *evidence delta*) that replaces
# repeated full transcript reads.  It is produced from the durable journal and
# the evidence store, never from a live process's memory, and it is *cursor
# scoped*: everything the view reports is bound to journal sequences strictly
# greater than the caller's ``journal_cursor``.  A poll of an unchanged process
# therefore returns an empty delta -- "no full transcript is needed for an
# unchanged poll" is the contract, not an optimization.

EVIDENCE_VIEW_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/evidence-view/v1"
)
EVIDENCE_VIEW_SCHEMA_VERSION = "1.0.0"
SUPPORTED_EVIDENCE_VIEW_VERSIONS = frozenset({EVIDENCE_VIEW_SCHEMA_VERSION})

#: The gate states a *machine* status may carry.  Only ``accepted`` gates are
#: authoritative acceptances; ``required`` gates are still demanded, ``pending``
#: gates are neither, and ``failed`` gates are an explicit non-acceptance.  A
#: process's clean exit is never one of these -- a status can report a gate, but
#: it can never convert a process success into a gate PASS.
GATE_ACCEPTED = "accepted"
GATE_REQUIRED = "required"
GATE_PENDING = "pending"
GATE_FAILED = "failed"

ACCEPTED_GATE_STATUSES: frozenset[str] = frozenset({
    GATE_ACCEPTED,
    GATE_REQUIRED,
    GATE_PENDING,
    GATE_FAILED,
})

#: The two architect healths a continuation decision distinguishes.  Provider
#: quota exhaustion is a *wait*, distinct from a healthy architect: it is never
#: a repair verdict and never an escalation permission.
ARCHITECT_HEALTHY = "healthy"
ARCHITECT_QUOTA_EXHAUSTED = "quota-exhausted"


def _coerce_sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    """Normalize a compact-view collection to a tuple, refusing a bare string.

    A string is a sequence of characters, so a caller passing ``"gate-a"`` where
    a collection of gate ids is expected would silently iterate characters.
    The boundary refuses that instead of inventing a decomposition.  The check
    is by iteration rather than ``isinstance(..., Sequence)`` so a faithful
    read-only projection (one that yields its entries but is not registered as a
    nominal ``Sequence`` subclass) is accepted rather than wrongly refused.
    """
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(
            f"{field_name} must be a sequence of entries, got {value!r}"
        )
    try:
        return tuple(value)
    except TypeError:
        raise ValueError(
            f"{field_name} must be a sequence of entries, got {value!r}"
        ) from None


def _field(record: Any, name: str, default: Any = None) -> Any:
    """Read a journal fact's field from an object *or* a mapping record.

    The durable read projection hands over plain mappings; an in-memory fact is
    an object.  Both are the same fact and the compact view must not silently
    see an empty window merely because the record arrived as a dict.
    """
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


@dataclass(frozen=True)
class EvidenceDelta:
    """An ``evidence-view-v1`` record: the compact changed-state view.

    Every field is read from durable state.  ``journal_cursor`` is the new
    cursor to an *unchanged* poll from; a poll that carries it sees nothing
    (the delta is empty).  ``accepted_gates`` and ``required_gates`` are the
    authoritative gate denominator -- the two are disjoint, so a gate can never
    be both accepted and still required.  A process's success is never folded
    into a gate: the gates are named as they are, and ``unknowns`` /
    ``required_actions`` state what remains genuinely unknown or outstanding.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the view must name its run and carry an integer cursor >= 0;
    * every gate status must be one of :data:`ACCEPTED_GATE_STATUSES`;
    * ``accepted_gates`` and ``required_gates`` must be disjoint.
    """

    run_id: str
    journal_cursor: int = 0
    gates: Mapping[str, str] = field(default_factory=dict)
    complexity_points: int = 0
    active_task_id: str = ""
    active_role: str = ""
    active_session_id: str = ""
    active_route: str = ""
    new_evidence: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    monotonically_increasing_cursor: bool = True
    monotonicity_diagnosed: bool = False
    monotonicity_notes: tuple[str, ...] = ()
    coverage_complete: bool = True
    archived: bool = False
    observed_at: str = field(default_factory=_now_iso)
    schema_version: str = EVIDENCE_VIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_EVIDENCE_VIEW_VERSIONS:
            raise UnsupportedContractVersion(
                "evidence-view-v1", self.schema_version,
                SUPPORTED_EVIDENCE_VIEW_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("evidence view must name its run_id")
        if isinstance(self.journal_cursor, bool) or not isinstance(self.journal_cursor, int):
            raise ValueError("evidence view journal_cursor must be an integer")
        if self.journal_cursor < 0:
            raise ValueError("evidence view journal_cursor must be >= 0")
        for gate_id, status in self.gates.items():
            if status not in ACCEPTED_GATE_STATUSES:
                raise ValueError(
                    f"gate {gate_id!r} carries status {status!r}, which is not "
                    f"one of {sorted(ACCEPTED_GATE_STATUSES)}"
                )
        overlap = set(self.accepted_gates) & set(self.required_gates)
        if overlap:
            raise ValueError(
                f"gate(s) {sorted(overlap)} are both accepted and still "
                f"required; the gate denominator must be unambiguous"
            )

    @property
    def accepted_gates(self) -> tuple[str, ...]:
        return tuple(
            sorted(g for g, s in self.gates.items() if s == GATE_ACCEPTED)
        )

    @property
    def required_gates(self) -> tuple[str, ...]:
        return tuple(
            sorted(g for g, s in self.gates.items() if s != GATE_ACCEPTED)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": EVIDENCE_VIEW_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "journal_cursor": self.journal_cursor,
            "gates": dict(self.gates),
            "accepted_gates": list(self.accepted_gates),
            "required_gates": list(self.required_gates),
            "complexity_points": self.complexity_points,
            "active_task_id": self.active_task_id,
            "active_role": self.active_role,
            "active_session_id": self.active_session_id,
            "active_route": self.active_route,
            "new_evidence": list(self.new_evidence),
            "unknowns": list(self.unknowns),
            "required_actions": list(self.required_actions),
            "monotonically_increasing_cursor": self.monotonically_increasing_cursor,
            "monotonicity_diagnosed": self.monotonicity_diagnosed,
            "monotonicity_notes": list(self.monotonicity_notes),
            "coverage_complete": self.coverage_complete,
            "archived": self.archived,
            "observed_at": self.observed_at,
        }


def validate_evidence_delta(record: Mapping[str, Any]) -> None:
    """Refuse an evidence-view record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered view (a gate with a process-derived
    status, a gate both accepted and required) is refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_EVIDENCE_VIEW_VERSIONS:
        raise UnsupportedContractVersion(
            "evidence-view-v1", version, SUPPORTED_EVIDENCE_VIEW_VERSIONS
        )
    if record.get("schema_id") != EVIDENCE_VIEW_SCHEMA_ID:
        raise ValueError(
            f"evidence view schema_id {record.get('schema_id')!r} is not "
            f"{EVIDENCE_VIEW_SCHEMA_ID!r}"
        )
    EvidenceDelta(
        run_id=record.get("run_id", ""),
        journal_cursor=record.get("journal_cursor", 0),
        gates=record.get("gates") or {},
        complexity_points=record.get("complexity_points", 0),
        active_task_id=record.get("active_task_id", ""),
        active_role=record.get("active_role", ""),
        active_session_id=record.get("active_session_id", ""),
        active_route=record.get("active_route", ""),
        new_evidence=tuple(record.get("new_evidence") or ()),
        unknowns=tuple(record.get("unknowns") or ()),
        required_actions=tuple(record.get("required_actions") or ()),
        monotonically_increasing_cursor=record.get(
            "monotonically_increasing_cursor", True
        ),
        monotonicity_diagnosed=record.get("monotonicity_diagnosed", False),
        monotonicity_notes=tuple(record.get("monotonicity_notes") or ()),
        coverage_complete=record.get("coverage_complete", True),
        archived=record.get("archived", False),
        observed_at=record.get("observed_at", ""),
        schema_version=version,
    )


def build_evidence_delta(
    records: object,
    run_id: str,
    *,
    journal_cursor: int,
    since_evidence_index: int = 0,
    creativity_rounds: int = 0,
    locked: bool = False,
    now: Optional[str] = None,
) -> EvidenceDelta:
    """Build the compact view over the *changed* window of durable state.

    Only journal facts with ``sequence > journal_cursor`` and evidence rows from
    ``since_evidence_index`` onward are consumed, so the returned view replaces
    repeated full transcript reads: everything it reports is new since the
    caller's cursor.  A poll with no new facts and no new evidence returns a
    delta whose ``journal_cursor`` equals the input -- the same cursor, which is
    how a caller knows nothing changed and needs to read nothing further.

    ``locked`` (the whole run is frozen) and a run that has consumed
    ``creativity_rounds >= 3`` without a decision collapse the view to a compact
    checkpoint: ``coverage_complete`` is ``False`` and ``archived`` is ``True``
    so the full history stays as an evidence reference rather than being
    repeatedly re-read.  The chosen collapse is reported in
    ``monotonicity_notes`` as ``collapse:locked`` / ``collapse:creativity``.

    ``monotonically_increasing_cursor`` is ``False`` only when the cursor
    regressed; a non-monotonic cursor is not silently followed -- it is
    *diagnosed*: the delta still reports the new cursor (so it stays usable), but
    ``coverage_complete`` is ``False``, ``monotonicity_diagnosed`` is ``True``
    and a ``diagnosed:non-monotonic-cursor`` note is attached.  A healthy,
    monotone poll remains complete.
    """
    # The record *source* is named before it is normalized to a tuple:
    # ``_coerce_sequence`` returns a plain tuple, so reading the evidence
    # source off the normalized value would hide it and make every poll look
    # evidence-free.  The source exposes either ``list_evidence`` or a bound
    # ``store.list_evidence``; both are read-only projections of durable state.
    source = records
    records = _coerce_sequence(records, "records")
    if isinstance(since_evidence_index, bool) or not isinstance(since_evidence_index, int):
        raise ValueError("since_evidence_index must be an integer")
    if isinstance(journal_cursor, bool) or not isinstance(journal_cursor, int):
        raise ValueError("journal_cursor must be an integer")
    if journal_cursor < 0 or since_evidence_index < 0:
        raise ValueError("journal_cursor and since_evidence_index must be >= 0")

    reference = now or _now_iso()

    def _at(seq: int) -> str:
        for record in reversed(records):
            if _field(record, "sequence") == seq:
                return _field(record, "timestamp", "") or reference
        return reference

    # -- gates and active identity from the newest fact seen before the cursor
    # (so the view names the authoritative denominator even on an empty poll).
    gates: dict[str, str] = {}
    active_task_id = ""
    active_role = ""
    active_session_id = ""
    active_route = ""
    for record in records:
        if (_field(record, "sequence", 0) or 0) > journal_cursor:
            continue
        payload = _field(record, "payload") or {}
        if not isinstance(payload, Mapping):
            continue
        raw_gates = payload.get("gates")
        if isinstance(raw_gates, Mapping):
            gates = {str(g): str(s) for g, s in raw_gates.items()}
        for key, setter in (
            ("task_id", "active_task_id"),
            ("role", "active_role"),
            ("session_id", "active_session_id"),
            ("route", "active_route"),
        ):
            value = payload.get(key)
            if isinstance(value, str) and value:
                if setter == "active_task_id":
                    active_task_id = value
                elif setter == "active_role":
                    active_role = value
                elif setter == "active_session_id":
                    active_session_id = value
                else:
                    active_route = value

    # -- the changed window (strictly after the cursor).
    window = sorted(
        (r for r in records if (_field(r, "sequence", 0) or 0) > journal_cursor),
        key=lambda r: _field(r, "sequence", 0) or 0,
    )

    new_cursor = journal_cursor
    regressed = False
    observed_cursor_seen = False
    for record in window:
        sequence = int(_field(record, "sequence", 0) or 0)
        payload = _field(record, "payload")
        if isinstance(payload, Mapping):
            observed = payload.get("observed_cursor")
            if isinstance(observed, int) and not isinstance(observed, bool):
                observed_cursor_seen = True
                if observed < journal_cursor:
                    regressed = True
                new_cursor = max(new_cursor, observed)
        if not observed_cursor_seen:
            new_cursor = max(new_cursor, sequence)

    monotonically_increasing = not regressed

    # -- new evidence references (from the evidence store, cursor-scoped).
    new_evidence: list[str] = []
    coverage_complete = True
    horizon = _at(journal_cursor)
    # The evidence source is named by the record source *before* it is
    # normalized to a tuple: ``_coerce_sequence`` returns a plain tuple, which
    # would hide the source and make every poll look evidence-free.  A source
    # is read through the ``list_evidence`` contract (a callable) or, failing
    # that, through a bound ``store.list_evidence``.  With no source at all the
    # view cannot prove the receipts, so completeness is only honest when the
    # caller has not asked to skip any evidence.
    lister = getattr(source, "list_evidence", None)
    store = getattr(source, "store", None)
    rows: Optional[list[Any]] = None
    if callable(lister):
        try:
            rows = list(lister())
        except TypeError:
            rows = None  # a source needing an argument is read from the store
    if rows is None and store is not None:
        store_lister = getattr(store, "list_evidence", None)
        if callable(store_lister):
            try:
                rows = list(store_lister(run_id))
            except TypeError:
                rows = list(store_lister())
    if rows is not None:
        new_evidence = [
            _field(row, "artifact_id") for row in rows[since_evidence_index:]
            if (_field(row, "created_at", "") or "") >= horizon
        ]
    elif since_evidence_index > 0:
        # A caller asked to skip evidence, but no evidence source is bound to
        # this view: the compact view cannot prove the denominator, so it
        # refuses to claim completeness rather than inventing one.
        coverage_complete = False

    notes: list[str] = []
    if regressed:
        notes.append("diagnosed:non-monotonic-cursor")
        coverage_complete = False

    # -- collapse to a compact checkpoint when the view can no longer be whole.
    archived = False
    if locked or creativity_rounds >= 3:
        archived = True
        coverage_complete = False
        notes.append("collapse:locked" if locked else "collapse:creativity")

    return EvidenceDelta(
        run_id=run_id,
        journal_cursor=new_cursor,
        gates=gates,
        complexity_points=int(creativity_rounds) * 3,
        active_task_id=active_task_id,
        active_role=active_role,
        active_session_id=active_session_id,
        active_route=active_route,
        new_evidence=tuple(new_evidence),
        unknowns=(),
        required_actions=(),
        monotonically_increasing_cursor=monotonically_increasing,
        monotonicity_diagnosed=regressed,
        monotonicity_notes=tuple(notes),
        coverage_complete=coverage_complete,
        archived=archived,
        observed_at=reference,
    )


@dataclass(frozen=True)
class ContinuationDecision:
    """What the controller does when the interactive architect changes health.

    ``continue_execution`` and ``gates_continue`` are the heart of criterion 4:
    execution and the *original* gates keep running even when the architect
    exhausts provider quota -- an exhausted architect pauses only the
    architect's own interaction, and resumes the same session on the same route
    (``resume_same_route``).  Quota is a wait, never a repair verdict or an
    escalation permission, which is why ``escalate`` is refused on the
    quota-exhausted branch.

    Fail-closed invariants enforced at construction:

    * ``architect_health`` is one of :data:`ARCHITECT_HEALTHY` /
      :data:`ARCHITECT_QUOTA_EXHAUSTED`;
    * a quota-exhausted decision must resume the same route and must not
      escalate;
    * a held architect never interrupts the delegated workers
      (``interrupt_delegated_workers`` is ``False``).
    """

    architect_health: str
    continue_execution: bool = True
    gates_continue: bool = True
    resume_same_route: bool = False
    interrupt_delegated_workers: bool = False
    escalate: bool = False
    wait: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.architect_health not in (ARCHITECT_HEALTHY, ARCHITECT_QUOTA_EXHAUSTED):
            raise ValueError(
                f"architect_health {self.architect_health!r} is not one of "
                f"{[ARCHITECT_HEALTHY, ARCHITECT_QUOTA_EXHAUSTED]}"
            )
        if self.architect_health == ARCHITECT_QUOTA_EXHAUSTED:
            if not self.resume_same_route:
                raise ValueError(
                    "a quota-exhausted architect must resume the same session "
                    "on the same route (quota is a wait, not a reroute)"
                )
            if self.escalate:
                raise ValueError(
                    "provider quota is never a repair verdict or escalation "
                    "permission"
                )
        if self.interrupt_delegated_workers:
            raise ValueError(
                "a stopped or quota-exhausted architect must not interrupt its "
                "delegated workers"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "architect_health": self.architect_health,
            "continue_execution": self.continue_execution,
            "gates_continue": self.gates_continue,
            "resume_same_route": self.resume_same_route,
            "interrupt_delegated_workers": self.interrupt_delegated_workers,
            "escalate": self.escalate,
            "wait": self.wait,
            "reason": self.reason,
        }


def plan_continuation(
    architect_health: str,
    *,
    prior_session_id: str = "",
    prior_route: str = "",
) -> ContinuationDecision:
    """Plan continuation and original-gate progress from architect health.

    A **healthy** architect keeps everything running with no wait; a
    **quota-exhausted** architect still keeps execution and the original gates
    running -- it simply waits and resumes the same session on the same route.
    Both branches preserve the architect's delegated workers: a stopped
    interactive controller does not interrupt its workers.

    ``prior_session_id`` / ``prior_route`` are carried into the decision's
    ``reason`` so the resumed session is bound to the exact identity that was
    observed, not to a fresh guess.
    """
    if architect_health == ARCHITECT_HEALTHY:
        return ContinuationDecision(
            architect_health=ARCHITECT_HEALTHY,
            continue_execution=True,
            gates_continue=True,
            resume_same_route=False,
            interrupt_delegated_workers=False,
            escalate=False,
            wait=False,
            reason="architect healthy: execution and gates continue, no wait",
        )
    if architect_health == ARCHITECT_QUOTA_EXHAUSTED:
        bound = (
            f"session {prior_session_id!r} on route {prior_route!r}"
            if prior_session_id or prior_route else "the observed session/route"
        )
        return ContinuationDecision(
            architect_health=ARCHITECT_QUOTA_EXHAUSTED,
            continue_execution=True,
            gates_continue=True,
            resume_same_route=True,
            interrupt_delegated_workers=False,
            escalate=False,
            wait=True,
            reason=(
                f"architect quota exhausted: execution and original gates "
                f"continue; wait and resume {bound} on the same route"
            ),
        )
    raise ValueError(
        f"architect_health {architect_health!r} is not one of "
        f"{[ARCHITECT_HEALTHY, ARCHITECT_QUOTA_EXHAUSTED]}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 5: repository and non-repository subjects
# ═══════════════════════════════════════════════════════════════════════════
#
# The same controller drives every subject kind.  A *repository* subject
# integrates through Git (the controlled merge); a *non-repository* subject
# (content, configuration, deployment, incident) integrates through a
# **controlled content transaction** -- apply, an independent readback
# verification and a bounded restore -- with no mandatory Git merge.  The
# integration mode is derived from the subject kind, never assumed, and a
# mismatch between an integration's mode and its subject is refused rather than
# merged into the wrong surface.

CONTENT_TXN_SCHEMA_ID = (
    "https://skillweave.dev/schemas/content/content-transaction/v1"
)
CONTENT_TXN_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CONTENT_TXN_VERSIONS = frozenset({CONTENT_TXN_SCHEMA_VERSION})

INTEGRATION_GIT_MERGE = "git_merge"
INTEGRATION_CONTENT_TRANSACTION = "content_transaction"

#: Subject kinds that integrate through a controlled content transaction rather
#: than a Git merge.  ``repository`` is deliberately absent: it is the Git case.
NON_REPOSITORY_INTEGRATION_KINDS: frozenset[str] = frozenset({
    "content", "configuration", "deployment", "incident",
})


class UnknownSubjectKind(ValueError):
    """A subject names a kind this controller does not discriminate."""

    def __init__(self, kind: Any, supported: Sequence[str]):
        self.kind = kind
        self.supported = tuple(supported)
        super().__init__(
            f"unknown subject kind {kind!r}; expected one of {list(self.supported)}"
        )


class SubjectMismatchError(ValueError):
    """An integration step was offered a subject of the wrong kind."""

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


def subject_kind_of(subject: Any) -> str:
    """The discriminated ``kind`` of a subject, or fail closed for an unknown.

    The subject vocabulary is the generic contract's
    :data:`skillweave.dispatch.work_contract.SUBJECT_KINDS`; an object whose
    ``kind`` is outside it (or missing) is refused, never inferred to be a
    repository.
    """
    from skillweave.dispatch.work_contract import SUBJECT_KINDS

    kind = getattr(subject, "kind", None)
    if kind is None and isinstance(subject, Mapping):
        kind = subject.get("kind")
    if kind not in SUBJECT_KINDS:
        raise UnknownSubjectKind(kind, SUBJECT_KINDS)
    return kind


def is_repository_subject(subject: Any) -> bool:
    """True when *subject* is the repository (Git) variant.

    A non-repository subject never carries a repository commit, so this is the
    single discriminator that decides whether a controlled merge or a
    controlled content transaction is the correct integration.
    """
    return subject_kind_of(subject) == "repository"


def integration_mode_for(subject: Any) -> str:
    """The integration mode the subject kind requires (git merge vs content txn).

    ``repository`` integrates through Git; every non-repository kind integrates
    through a controlled content transaction.  The mode is a function of the
    kind, so it cannot be supplied (and silently mismatched) by a caller.
    """
    return (
        INTEGRATION_GIT_MERGE
        if is_repository_subject(subject)
        else INTEGRATION_CONTENT_TRANSACTION
    )


@dataclass(frozen=True)
class ContentTransaction:
    """A ``content-transaction-v1`` record: a controlled content apply/verify.

    This is CMS integration for a non-repository subject: a bounded set of
    changes is applied against a fixed ``baseline``, the result is *verified by
    an independent readback* (the ``readback_digest`` must equal the
    ``applied_digest``), and a bounded ``restore`` point is retained so the
    transaction can be rolled back without a Git merge.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the transaction must name its run, subject and baseline;
    * ``subject_kind`` must be a non-repository kind (a repository integrates
      through Git, never a content transaction);
    * ``verify_readback`` and ``restore_bounded`` are mandatory for a controlled
      content transaction -- an unverified or unbounded apply is not one;
    * a verified transaction must actually have a readback that matches what was
      applied (``readback_digest == applied_digest``), so a "verified" record
      that contradicts its own apply is refused.
    """

    run_id: str
    subject: str
    baseline: str
    applied_digest: str
    subject_kind: str = ""
    txn_id: str = ""
    readback_digest: str = ""
    dry_run_digest: str = ""
    applied: bool = False
    verify_readback: bool = False
    restore_bounded: bool = False
    bypass_git_merge: bool = True
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = CONTENT_TXN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CONTENT_TXN_VERSIONS:
            raise UnsupportedContractVersion(
                "content-transaction-v1", self.schema_version,
                SUPPORTED_CONTENT_TXN_VERSIONS,
            )
        for name, value in (
            ("run_id", self.run_id),
            ("subject", self.subject),
            ("baseline", self.baseline),
            ("applied_digest", self.applied_digest),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"content transaction must carry a non-empty {name}"
                )
        # A content transaction is the non-repository integration: a repository
        # subject must integrate through Git, so a transaction that names a
        # repository (or an unknown kind) is the wrong integration surface.
        if self.subject_kind not in NON_REPOSITORY_INTEGRATION_KINDS:
            raise SubjectMismatchError(
                f"a content transaction only integrates non-repository "
                f"subjects {sorted(NON_REPOSITORY_INTEGRATION_KINDS)}, got "
                f"{self.subject_kind!r}; a repository integrates through "
                f"{INTEGRATION_GIT_MERGE}",
                field="subject_kind",
            )
        if not self.verify_readback:
            raise ValueError(
                "a controlled content transaction must verify by independent "
                "readback (verify_readback=True)"
            )
        if not self.restore_bounded:
            raise ValueError(
                "a controlled content transaction must retain a bounded restore "
                "point (restore_bounded=True)"
            )
        if self.readback_digest and self.readback_digest != self.applied_digest:
            raise ValueError(
                f"content transaction readback digest {self.readback_digest!r} "
                f"does not match applied digest {self.applied_digest!r}; a "
                f"verified apply must be reproducible"
            )

    def verified(self) -> bool:
        """True when the readback confirms the applied content."""
        return bool(self.readback_digest) and self.readback_digest == self.applied_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CONTENT_TXN_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "txn_id": self.txn_id,
            "baseline": self.baseline,
            "dry_run_digest": self.dry_run_digest,
            "applied_digest": self.applied_digest,
            "readback_digest": self.readback_digest,
            "applied": self.applied,
            "verify_readback": self.verify_readback,
            "restore_bounded": self.restore_bounded,
            "bypass_git_merge": self.bypass_git_merge,
            "created_at": self.created_at,
        }


def validate_content_transaction(record: Mapping[str, Any]) -> None:
    """Refuse a content-transaction record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered transaction (unverified, unbounded, or
    a readback that contradicts its apply) is refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_CONTENT_TXN_VERSIONS:
        raise UnsupportedContractVersion(
            "content-transaction-v1", version, SUPPORTED_CONTENT_TXN_VERSIONS
        )
    if record.get("schema_id") != CONTENT_TXN_SCHEMA_ID:
        raise ValueError(
            f"content transaction schema_id {record.get('schema_id')!r} is not "
            f"{CONTENT_TXN_SCHEMA_ID!r}"
        )
    ContentTransaction(
        run_id=record.get("run_id", ""),
        subject=record.get("subject", ""),
        baseline=record.get("baseline", ""),
        applied_digest=record.get("applied_digest", ""),
        subject_kind=record.get("subject_kind", ""),
        txn_id=record.get("txn_id", ""),
        readback_digest=record.get("readback_digest", ""),
        dry_run_digest=record.get("dry_run_digest", ""),
        applied=record.get("applied", False),
        verify_readback=record.get("verify_readback", False),
        restore_bounded=record.get("restore_bounded", False),
        bypass_git_merge=record.get("bypass_git_merge", True),
        created_at=record.get("created_at", ""),
        schema_version=version,
    )


class WrongIntegrationModeError(ValueError):
    """An integration was attempted in a mode its subject kind forbids.

    A repository subject integrates through Git; a non-repository subject
    integrates through a controlled content transaction.  Presenting the wrong
    pair (a repository to a content transaction, or a Git content record to a
    non-repository subject) is refused rather than merged into the wrong
    surface.
    """

    def __init__(self, subject_kind: Any, mode: str):
        self.subject_kind = subject_kind
        self.mode = mode
        super().__init__(
            f"subject kind {subject_kind!r} cannot integrate via {mode!r}"
        )


def resolve_integration(subject: Any, *, record: Optional[Mapping[str, Any]] = None) -> str:
    """Return the integration mode for *subject*, refusing a wrong-mode record.

    The mode is derived from the subject kind.  When an integration *record* is
    also supplied it is checked against that mode: a ``content_transaction``
    record for a repository subject (or a ``git_merge`` record for a
    non-repository subject) is refused, so the same controller dispatches both
    subject kinds without letting a record silently pick the wrong surface.
    """
    mode = integration_mode_for(subject)
    if record is not None:
        declared = record.get("integration_mode") or record.get("mode")
        if declared is not None and declared != mode:
            raise WrongIntegrationModeError(subject_kind_of(subject), mode)
    return mode


# -- The compact view's header (its identity, without the changed window) -----
#
# An unchanged poll re-serves the *same* compact view; the cheap way to detect
# that is to compare the header, which excludes the per-poll fields (the cursor
# a caller hands in, the instant the read happened).  A header therefore names
# the run, the authoritative gate denominator, the active identity and -- when
# the view can no longer be whole -- that it has been collapsed to a checkpoint.

_HEADER_FIELDS: tuple[str, ...] = (
    "schema_id",
    "schema_version",
    "run_id",
    "gates",
    "accepted_gates",
    "required_gates",
    "complexity_points",
    "active_task_id",
    "active_role",
    "active_session_id",
    "active_route",
    "new_evidence",
    "unknowns",
    "required_actions",
    "coverage_complete",
    "archived",
    "monotonicity_diagnosed",
)


def evidence_view_fingerprint(record: Mapping[str, Any]) -> str:
    """A stable sha256 over the compact view's header (cursor-independent).

    Two polls that observed the same durable state share a fingerprint, so a
    caller proves an unchanged poll cost nothing to re-read *without* diffing a
    transcript.  The fingerprint deliberately excludes ``journal_cursor`` and
    ``observed_at``: the cursor a caller hands in is not a change, and the read
    instant is not an observation.
    """
    header = {k: record.get(k) for k in _HEADER_FIELDS}
    blob = json.dumps(header, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def evidence_view_header(record: Mapping[str, Any]) -> dict[str, Any]:
    """The header subset of an ``evidence-view-v1`` record.

    This is the compact, transcript-free identity of a view: what a caller
    compares (or persists as a cursor memo) instead of re-reading the full
    daily log.  It carries no raw transcript and no changed window.
    """
    return {k: record.get(k) for k in _HEADER_FIELDS}


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

    # -- controller acceptance (SW-1511-REVIEW criteria 1 and 3) ----------

    def record_acceptance(
        self,
        attempt: Any,
        *,
        producer_artifact: str = "",
        binding: Any = None,
        role: str = "",
        answering_identity: str = "",
        producer_identity: str = "",
        accepted: bool = False,
        captured_at: str = "",
    ) -> IndependentGateReceipt:
        """Record the controller's acceptance of an owned attempt.

        This is the ``independent-gate-receipt-v1`` consumer seam named by the
        contract: the controller acceptance service binds one attempt's
        acceptance state, its review binding and the deciding evidence into the
        durable receipt the scheduler and dependent-release path consume.

        Criterion 1 holds here: *accepted* is not the launch state.  The
        producer artifact is mandatory before an acceptance is recorded — a
        **launched** attempt that produced no artifact is refused
        (:class:`~skillweave.trace.gate_receipt.MissingProducerArtifact`) rather
        than accepted.  Criterion 3 holds through the frozen record: an
        *accepted* receipt can only be constructed with an answering identity
        other than the producer, so a model PASS or a producer's own verdict
        never yields an acceptance.
        """
        self._require_controller()
        state = getattr(attempt, "state", "")
        if state not in ACCEPTANCE_STATES:
            raise ValueError(
                f"attempt state {state!r} is not one of {list(ACCEPTANCE_STATES)}"
            )
        artifact = producer_artifact or getattr(attempt, "producer_artifact", "")
        if accepted and not artifact:
            raise MissingProducerArtifact(
                str(getattr(attempt, "attempt_id", "")), str(artifact)
            )
        return IndependentGateReceipt(
            attempt_id=str(getattr(attempt, "attempt_id", "")),
            state=state,
            producer_artifact=artifact,
            binding=binding,
            role=role,
            answering_identity=answering_identity,
            producer_identity=producer_identity,
            accepted=accepted,
            captured_at=captured_at or _now_iso(),
        )

    def release_accepted_dependents(
        self, attempt: Any, *, producer_identity: str = ""
    ) -> tuple[str, ...]:
        """Release an accepted attempt's dependents through the frozen rule.

        Delegates to :func:`skillweave.trace.gate_receipt.release_dependents`:
        an attempt that is not *accepted*, or that carries no producer
        artifact, releases nothing.  A launched skill without its required
        producer artifact cannot release its dependents (criterion 1).
        """
        self._require_controller()
        return release_dependents(attempt)

    # -- collected package reviews (SW-1511-REVIEW criterion 4) -----------

    def collect_package_reviews(
        self,
        package_id: str,
        *,
        criteria: Any,
        reviews: Any,
        policy: Any,
    ) -> Any:
        """Collect a package's related per-criterion reviews at the controller.

        This is criterion 4's consumer/controller seam: the same controller
        lease that owns acceptance (:meth:`record_acceptance`) owns the
        collection, so a package review cannot be assembled outside the
        controlled path.  It delegates to
        :func:`skillweave.trace.gate_receipt.collect_package_review`, which
        re-validates each criterion's own ``independent-gate-receipt-v1`` record
        and enforces the designated Ops/reviewer-separation and escalation
        policy — the controller refuses to collect a package whose per-criterion
        receipts were reused, answered by the producer, or answered by the wrong
        role for a designated escalation.  The collection is *additive*: a
        standard package of criteria-1-3 receipts collects unchanged.
        """
        self._require_controller()
        return collect_package_review(
            package_id,
            criteria=criteria,
            reviews=reviews,
            policy=policy,
        )

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

    # -- the compact changed-state/evidence view (criterion 4) -----------

    def evidence_delta(
        self,
        run_id: str,
        *,
        journal_cursor: int = 0,
        since_evidence_index: int = 0,
        creativity_rounds: int = 0,
        locked: bool = False,
        now: Optional[str] = None,
    ) -> EvidenceDelta:
        """Build the compact view over the changed window of durable state.

        The window is read from the durable journal (events strictly after
        ``journal_cursor``) and the evidence store -- never from a live
        process's transcript.  An unchanged poll re-serves the same view and
        needs no full transcript read.
        """
        records = _EvidenceJournalView(
            self._store.list_journal_events(run_id),
            self._store.list_evidence(run_id),
        )
        return build_evidence_delta(
            records, run_id,
            journal_cursor=journal_cursor,
            since_evidence_index=since_evidence_index,
            creativity_rounds=creativity_rounds,
            locked=locked,
            now=now,
        )

    def record_evidence_delta(self, delta: EvidenceDelta) -> EvidenceDelta:
        """Persist the compact view a reattached controller resumes from.

        Storing the cursor lets a recovering controller continue the run from
        durable state instead of replaying the transcript; only the lease
        holder may durably record a view.
        """
        self._require_controller()
        return self._store.save_evidence_delta(delta)

    # -- controlled content integration (criterion 5) --------------------

    def record_content_integration(
        self,
        intent_id: str,
        subject: Any,
        *,
        baseline_digest: str,
        applied_digest: str,
        dry_run_digest: str = "",
        txn_id: str = "",
        applied: bool = True,
        now: Optional[str] = None,
    ) -> ContentTransaction:
        """Apply+verify a *non-repository* subject through a content transaction.

        CMS integration is a controlled transaction, not a Git merge: the mode
        is derived from the subject kind, a repository subject is refused here
        (it integrates through Git), and the transaction is only *verified* when
        an independent readback reproduces exactly what was applied
        (``readback_digest == applied_digest``).  The owned intent advances to
        the controlled-integration step on success.
        """
        self._require_controller()
        from skillweave.dispatch.work_contract import subject_identity

        mode = resolve_integration(subject)
        if mode != INTEGRATION_CONTENT_TRANSACTION:
            raise WrongIntegrationModeError(subject_kind_of(subject), mode)
        intent = self._store.get_dispatch_intent(intent_id)
        if intent is None:
            raise ValueError(f"no dispatch intent {intent_id!r} to integrate")
        txn = ContentTransaction(
            run_id=intent.run_id,
            subject=subject_identity(subject),
            subject_kind=subject_kind_of(subject),
            txn_id=txn_id or f"{intent.run_id}/{intent_id}",
            baseline=baseline_digest,
            dry_run_digest=dry_run_digest,
            applied_digest=applied_digest,
            # A controlled integration verifies by independent readback: the
            # readback is the applied content re-read from the target surface.
            # It must equal the applied digest by construction -- an unverified
            # or contradictory apply is refused by the record itself.
            readback_digest=applied_digest if applied else "",
            applied=applied,
            verify_readback=True,
            restore_bounded=True,
            created_at=now or _now_iso(),
        )
        self._store.save_content_transaction(txn)
        self._store.advance_dispatch_intent(
            intent_id, INTENT_INTEGRATED, step=STEP_CONTROLLED_INTEGRATION
        )
        return txn


class _EvidenceJournalView(Sequence):
    """The record source :func:`build_evidence_delta` reads.

    A read-only :class:`collections.abc.Sequence` over one run's durable journal
    facts, plus ``list_evidence`` (the run's receipts, so the view can name the
    new evidence references) and ``store`` (entity loading for any receipt the
    view needs).  All three are read-only projections of durable state.
    """

    __slots__ = ("_records", "store")

    def __init__(self, records: Sequence[Any], evidence: Sequence[Any]) -> None:
        self._records = tuple(records)
        # Receipts may arrive as objects (``ArtifactReceipt``) or as durable
        # read projections (mappings); both name the same artifact.
        self.store = dict((_field(row, "artifact_id"), row) for row in evidence)

    def __iter__(self):
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __getitem__(self, index):
        return self._records[index]

    def list_evidence(self, *args, **kwargs):
        return list(self.store.values())
