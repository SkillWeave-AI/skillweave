"""Authoritative task lifecycle and acceptance ledger (SW-1511A-LEDGER-001).

This module replaces controller-reconstructed counts and stale status files
with a **deterministic projection from versioned receipts**. It is the
storage-side contract named by the task boundary
``task-lifecycle-receipt/v1 with monotone event sequence``: the producer is a
native process service / independent integration gate, the consumer the
scheduler, readiness evaluator, watchdog and Studio.

Three concerns:

1. **Separate axes** (:data:`LEDGER_AXES`): every canonical task exposes
   *separate* ``source``, ``review``, ``integration``, ``live_qualification``,
   ``acceptance`` and ``hold`` states. No axis is inferred from another, and
   no axis ever collapses a live-criterion gap into a process failure.

2. **Monotone, non-overwriting sequence** (:class:`TaskLedger`): every axis of
   every task advances only through strictly increasing receipt sequence
   numbers. A conflicting or stale input (equal-or-lower sequence, or a
   non-authoritative legacy status snapshot) is **rejected** — it can never
   overwrite a newer authoritative state — while remaining **retained as a
   historical observation** so existing evidence is never destroyed.

3. **Canonical 24-task projection** (:func:`project_ledger`): the 1.5.11
   ledger reproduces a row for every one of the 24 canonical task identities
   (even those with no receipt yet) and lets a caller read the open tasks
   *without manual counting*.

Nothing here launches a process, names a provider/model/harness, reads a wall
clock, or touches the network. Receipts carry their own ``observed_at`` and
``sequence``; the ledger is a pure fold over the ordered receipt stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

# ── Contract identity ────────────────────────────────────────────────────────

TASK_LIFECYCLE_RECEIPT_SCHEMA_ID = (
    "task-lifecycle-receipt/v1 (skillweave // SW-1511A-LEDGER-001)"
)
TASK_LIFECYCLE_RECEIPT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_TASK_LIFECYCLE_RECEIPT_VERSIONS: Tuple[str, ...] = ("1.0.0",)

# ── The separate lifecycle axes (criterion 1) ────────────────────────────────

AXIS_SOURCE = "source"
AXIS_REVIEW = "review"
AXIS_INTEGRATION = "integration"
AXIS_LIVE_QUALIFICATION = "live_qualification"
AXIS_ACCEPTANCE = "acceptance"
AXIS_HOLD = "hold"

#: The six separate axes every canonical task exposes. Their order is the
#: projection column order and is part of the contract.
LEDGER_AXES: Tuple[str, ...] = (
    AXIS_SOURCE,
    AXIS_REVIEW,
    AXIS_INTEGRATION,
    AXIS_LIVE_QUALIFICATION,
    AXIS_ACCEPTANCE,
    AXIS_HOLD,
)

#: The closed vocabulary of each axis. A receipt naming a state outside the
#: axis vocabulary is refused (fail closed) rather than silently recorded.
AXIS_STATES: Mapping[str, Tuple[str, ...]] = {
    AXIS_SOURCE: ("absent", "present", "accepted"),
    AXIS_REVIEW: ("absent", "in_review", "review_failed", "review_passed"),
    AXIS_INTEGRATION: ("absent", "pending", "refused", "integrated"),
    AXIS_LIVE_QUALIFICATION: (
        "not_performed",
        "pending",
        "failed",
        "qualified",
        "not_applicable",
    ),
    AXIS_ACCEPTANCE: ("pending", "rejected", "accepted"),
    AXIS_HOLD: ("none", "named_open", "cleared"),
}

#: The default state of each axis before any receipt is folded for a task.
_AXIS_DEFAULT: Mapping[str, str] = {
    AXIS_SOURCE: "absent",
    AXIS_REVIEW: "absent",
    AXIS_INTEGRATION: "absent",
    AXIS_LIVE_QUALIFICATION: "not_performed",
    AXIS_ACCEPTANCE: "pending",
    AXIS_HOLD: "none",
}

# ── The canonical 1.5.11 task identity set (criterion 3) ─────────────────────

#: The 24 canonical SkillWeave 1.5.11 task identities, in PRD order. These are
#: *identities*, not statuses: the ledger reproduces a row for every one of
#: them and refuses a receipt for any task outside the frozen set.
CANONICAL_1511_TASK_IDS: Tuple[str, ...] = (
    "SW-1511-BASE",
    "SW-1511-EVIDENCE",
    "SW-1511-CLOCK",
    "SW-1511-CAPACITY",
    "SW-1511-AVAILABILITY",
    "SW-1511-REPAIR",
    "SW-ORCH-001",
    "SW-1511-REVIEW",
    "SW-1511-RECOVERY",
    "SW-1511-CMS-ENTRY",
    "SW-1511-CMS-EVIDENCE",
    "SW-1511-CMS-TRANSACTION",
    "SW-1511-CMS-FLOWS",
    "SW-1511-GATE",
    "CORE-ZR-022",
    "CORE-ZR-023",
    "CORE-ZR-024",
    "CORE-ZR-025",
    "CORE-ZR-026",
    "CORE-ZR-027",
    "SW-1511-SCHEMA",
    "SW-1511-MCP-PACK",
    "SW-1511-MCP-BOOTSTRAP",
    "SW-1511-SEARCH-ALIGN",
)

#: The eight ``SW-1511A-*`` addendum task identities of the 1.5.11 addendum
#: PRD, in PRD order. These are a *separate* set: the parent 24-task projection
#: and its counts are unchanged, and the addendum is reported **additively**
#: (see :class:`TaskLedgerProjection`) so a reader can tell the two sets apart
#: rather than seeing one merged, inflated task count.
ADDENDUM_1511A_TASK_IDS: Tuple[str, ...] = (
    "SW-1511A-LEDGER-001",
    "SW-1511A-RESULT-001",
    "SW-1511A-LIVENESS-001",
    "SW-1511A-ENV-001",
    "SW-1511A-UPSTREAM-001",
    "SW-1511A-INTEGRATION-001",
    "SW-1511A-HANDOFF-001",
    "SW-1511A-GATE-001",
)

#: The identities the ledger will accept a receipt for: the frozen parent 24
#: plus the addendum 8. Any other id is still refused fail-closed, so the
#: accepted surface grows only by whole, declared sets — never one id at a time.
LEDGER_KNOWN_TASK_IDS: Tuple[str, ...] = (
    CANONICAL_1511_TASK_IDS + ADDENDUM_1511A_TASK_IDS
)


# ── Exceptions (fail closed, never silently folded) ──────────────────────────


class TaskLedgerError(Exception):
    """A task ledger contract violation (raised fail-closed)."""


class UnknownCanonicalTaskError(TaskLedgerError):
    """A receipt named a task outside the frozen canonical identity set."""


class UnknownAxisError(TaskLedgerError):
    """A receipt named an axis or axis state outside the contract vocabulary."""


class NonMonotoneSequenceError(TaskLedgerError):
    """A receipt attempted to establish an authoritative state out of order.

    Raised only when the caller asks the ledger to *apply* a non-advancing
    receipt as authoritative; the default :meth:`TaskLedger.record` path
    rejects-by-return instead and retains the input as an observation.
    """


# ── Receipt / observation records ────────────────────────────────────────────


@dataclass(frozen=True)
class TaskLifecycleReceipt:
    """One versioned authoritative lifecycle receipt (``.../v1``).

    ``sequence`` is monotone *per (task_id, axis)*: a receipt advances the axis
    only when its sequence is strictly greater than the axis' current
    authoritative sequence. ``receipt_id`` is the source-bound evidence
    reference the projection cites; ``observed_at`` is the typed fact the
    producer recorded (never a wall-clock read by the ledger).
    """

    task_id: str
    axis: str
    state: str
    sequence: int
    receipt_id: str = ""
    observed_at: str = ""
    hold_reason: str = ""
    schema_version: str = TASK_LIFECYCLE_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.task_id not in LEDGER_KNOWN_TASK_IDS:
            raise UnknownCanonicalTaskError(
                f"task '{self.task_id}' is not one of the "
                f"{len(CANONICAL_1511_TASK_IDS)} canonical 1.5.11 task identities "
                f"or the {len(ADDENDUM_1511A_TASK_IDS)} SW-1511A addendum identities"
            )
        if self.axis not in AXIS_STATES:
            raise UnknownAxisError(f"unknown lifecycle axis '{self.axis}'")
        if self.state not in AXIS_STATES[self.axis]:
            raise UnknownAxisError(
                f"state '{self.state}' is not in axis '{self.axis}' "
                f"vocabulary {list(AXIS_STATES[self.axis])}"
            )
        if self.schema_version not in SUPPORTED_TASK_LIFECYCLE_RECEIPT_VERSIONS:
            raise TaskLedgerError(
                f"unsupported receipt schema version '{self.schema_version}'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TASK_LIFECYCLE_RECEIPT_SCHEMA_ID,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "axis": self.axis,
            "state": self.state,
            "sequence": self.sequence,
            "receipt_id": self.receipt_id,
            "observed_at": self.observed_at,
            "hold_reason": self.hold_reason,
        }


@dataclass(frozen=True)
class LedgerOutcome:
    """The typed result of folding one receipt or observation.

    ``applied`` is ``True`` only when the input became the new authoritative
    state of its ``(task_id, axis)``. ``retained_as_observation`` is ``True``
    whenever the input was kept in the historical record — including every
    rejected stale/conflicting input, so no evidence is destroyed.
    """

    task_id: str
    axis: str
    sequence: int
    applied: bool
    reason: str = ""
    retained_as_observation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "axis": self.axis,
            "sequence": self.sequence,
            "applied": self.applied,
            "reason": self.reason,
            "retained_as_observation": self.retained_as_observation,
        }


@dataclass(frozen=True)
class LedgerObservation:
    """A retained non-authoritative historical observation.

    Legacy status files and conflicting/stale inputs land here: they are
    visible to an operator as history but can never overwrite a newer
    authoritative state (criterion 2).
    """

    task_id: str
    axis: str
    state: str
    sequence: int
    reason: str
    receipt_id: str = ""
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "axis": self.axis,
            "state": self.state,
            "sequence": self.sequence,
            "reason": self.reason,
            "receipt_id": self.receipt_id,
            "observed_at": self.observed_at,
        }


# ── Projection rows (criterion 1, 3) ─────────────────────────────────────────


@dataclass(frozen=True)
class TaskLedgerRow:
    """One task's projected row: every axis kept separate and explicit."""

    task_id: str
    source: str
    review: str
    integration: str
    live_qualification: str
    acceptance: str
    hold: str
    hold_reason: str
    sequence: int
    receipt_refs: Tuple[str, ...] = ()

    def axis_state(self, axis: str) -> str:
        if axis == AXIS_SOURCE:
            return self.source
        if axis == AXIS_REVIEW:
            return self.review
        if axis == AXIS_INTEGRATION:
            return self.integration
        if axis == AXIS_LIVE_QUALIFICATION:
            return self.live_qualification
        if axis == AXIS_ACCEPTANCE:
            return self.acceptance
        if axis == AXIS_HOLD:
            return self.hold
        raise UnknownAxisError(f"unknown lifecycle axis '{axis}'")

    @property
    def accepted(self) -> bool:
        """True only when the *acceptance* axis is authoritative ``accepted``."""
        return self.acceptance == "accepted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source,
            "review": self.review,
            "integration": self.integration,
            "live_qualification": self.live_qualification,
            "acceptance": self.acceptance,
            "hold": self.hold,
            "hold_reason": self.hold_reason,
            "sequence": self.sequence,
            "receipt_refs": list(self.receipt_refs),
        }


@dataclass(frozen=True)
class TaskLedgerProjection:
    """The deterministic projection of the ledger over the task identity sets.

    ``rows`` are the frozen parent 24 canonical tasks and their counts
    (:attr:`accepted_count`, :attr:`open_count`, :meth:`open_task_ids`) are
    over exactly those rows — the parent contract is unchanged. The eight
    ``SW-1511A-*`` addendum rows travel separately in ``addendum_rows`` and are
    reported **additively** (:attr:`addendum_accepted_count`,
    :attr:`addendum_open_count`), so the addendum is visible without inflating
    or re-keying the parent 24.
    """

    rows: Tuple[TaskLedgerRow, ...]
    observations: Tuple[LedgerObservation, ...] = ()
    rejected: Tuple[LedgerOutcome, ...] = ()
    addendum_rows: Tuple[TaskLedgerRow, ...] = ()

    @property
    def task_ids(self) -> Tuple[str, ...]:
        return tuple(r.task_id for r in self.rows)

    def row(self, task_id: str) -> TaskLedgerRow:
        for r in (*self.rows, *self.addendum_rows):
            if r.task_id == task_id:
                return r
        raise UnknownCanonicalTaskError(f"no ledger row for task '{task_id}'")

    def open_task_ids(self) -> Tuple[str, ...]:
        """The parent task ids whose acceptance axis is not ``accepted``.

        Derived, never hand-counted: the caller reads this instead of
        reconstructing progress from stale status files.
        """
        return tuple(r.task_id for r in self.rows if not r.accepted)

    def addendum_task_ids(self) -> Tuple[str, ...]:
        """The addendum task ids projected, in addendum PRD order."""
        return tuple(r.task_id for r in self.addendum_rows)

    def addendum_open_task_ids(self) -> Tuple[str, ...]:
        """The addendum task ids whose acceptance axis is not ``accepted``."""
        return tuple(r.task_id for r in self.addendum_rows if not r.accepted)

    def is_open(self, task_id: str) -> bool:
        return not self.row(task_id).accepted

    @property
    def accepted_count(self) -> int:
        return sum(1 for r in self.rows if r.accepted)

    @property
    def open_count(self) -> int:
        return len(self.rows) - self.accepted_count

    @property
    def addendum_accepted_count(self) -> int:
        return sum(1 for r in self.addendum_rows if r.accepted)

    @property
    def addendum_open_count(self) -> int:
        return len(self.addendum_rows) - self.addendum_accepted_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TASK_LIFECYCLE_RECEIPT_SCHEMA_ID,
            "schema_version": TASK_LIFECYCLE_RECEIPT_SCHEMA_VERSION,
            "canonical_task_count": len(CANONICAL_1511_TASK_IDS),
            "rows": [r.to_dict() for r in self.rows],
            "observations": [o.to_dict() for o in self.observations],
            "rejected": [o.to_dict() for o in self.rejected],
            "accepted_count": self.accepted_count,
            "open_count": self.open_count,
            "open_task_ids": list(self.open_task_ids()),
            "addendum_task_count": len(ADDENDUM_1511A_TASK_IDS),
            "addendum_rows": [r.to_dict() for r in self.addendum_rows],
            "addendum_accepted_count": self.addendum_accepted_count,
            "addendum_open_count": self.addendum_open_count,
            "addendum_open_task_ids": list(self.addendum_open_task_ids()),
        }


# ── The ledger (criterion 1, 2) ──────────────────────────────────────────────


class TaskLedger:
    """Fold versioned receipts into an authoritative, monotone ledger.

    Authoritative state is per ``(task_id, axis)``. A receipt advances an axis
    only when its ``sequence`` is strictly greater than the axis' current
    sequence; equal-or-lower sequences and non-authoritative legacy snapshots
    are rejected-by-return and retained as :class:`LedgerObservation` history.
    """

    def __init__(self) -> None:
        self._authoritative: dict[Tuple[str, str], TaskLifecycleReceipt] = {}
        self._observations: List[LedgerObservation] = []
        self._rejected: List[LedgerOutcome] = []

    # -- reads ---------------------------------------------------------------

    def authoritative(self, task_id: str, axis: str) -> Optional[TaskLifecycleReceipt]:
        return self._authoritative.get((task_id, axis))

    def sequence_of(self, task_id: str, axis: str) -> int:
        """The current authoritative sequence of an axis (``-1`` when unset)."""
        receipt = self._authoritative.get((task_id, axis))
        return receipt.sequence if receipt is not None else -1

    def next_sequence(self, task_id: str, axis: str) -> int:
        """The next strictly-increasing sequence a producer may use."""
        return self.sequence_of(task_id, axis) + 1

    def observations(self) -> Tuple[LedgerObservation, ...]:
        return tuple(self._observations)

    def rejected(self) -> Tuple[LedgerOutcome, ...]:
        return tuple(self._rejected)

    # -- writes --------------------------------------------------------------

    def record(self, receipt: TaskLifecycleReceipt) -> LedgerOutcome:
        """Fold one authoritative receipt, advancing or rejecting it.

        The receipt's identity, axis and state are validated by
        :class:`TaskLifecycleReceipt`. A receipt whose sequence does not
        strictly exceed the axis' current sequence is **not** applied: a
        same-sequence *conflicting* state and a straight *stale* replay are
        both recorded as history and reported as ``applied=False``.
        """
        current = self._authoritative.get((receipt.task_id, receipt.axis))
        if current is not None:
            if receipt.sequence < current.sequence:
                return self._reject(
                    receipt,
                    reason=(
                        f"stale: sequence {receipt.sequence} < authoritative "
                        f"{current.sequence} on {receipt.axis}"
                    ),
                )
            if receipt.sequence == current.sequence:
                if receipt.state == current.state:
                    return LedgerOutcome(
                        task_id=receipt.task_id,
                        axis=receipt.axis,
                        sequence=receipt.sequence,
                        applied=False,
                        reason="idempotent re-delivery of an identical receipt",
                        retained_as_observation=False,
                    )
                return self._reject(
                    receipt,
                    reason=(
                        f"conflict: sequence {receipt.sequence} already held "
                        f"state '{current.state}' on {receipt.axis}"
                    ),
                )

        self._authoritative[(receipt.task_id, receipt.axis)] = receipt
        return LedgerOutcome(
            task_id=receipt.task_id,
            axis=receipt.axis,
            sequence=receipt.sequence,
            applied=True,
            reason="",
            retained_as_observation=False,
        )

    def apply(self, receipt: TaskLifecycleReceipt) -> TaskLifecycleReceipt:
        """Apply a receipt or fail closed with :class:`NonMonotoneSequenceError`.

        The strict variant: a caller that must not tolerate a non-advancing
        receipt (a producer asserting it owns the newest state) gets a typed
        refusal instead of a silent reject-by-return.
        """
        outcome = self.record(receipt)
        if not outcome.applied:
            raise NonMonotoneSequenceError(
                f"receipt for {receipt.task_id}/{receipt.axis} at sequence "
                f"{receipt.sequence} was not applied: {outcome.reason}"
            )
        return self._authoritative[(receipt.task_id, receipt.axis)]

    def ingest_status_snapshot(
        self,
        *,
        task_id: str,
        axis: str,
        state: str,
        receipt_id: str = "",
        observed_at: str = "",
    ) -> LedgerOutcome:
        """Ingest a legacy/stale status-file snapshot as history only.

        A status file carries no monotone sequence and therefore carries no
        authority: it is always retained as an observation and never applied,
        so it can never overwrite a newer authoritative state.
        """
        if task_id not in LEDGER_KNOWN_TASK_IDS:
            raise UnknownCanonicalTaskError(
                f"status snapshot names non-canonical task '{task_id}'"
            )
        if axis not in AXIS_STATES:
            raise UnknownAxisError(f"unknown lifecycle axis '{axis}'")
        return self._reject(
            state=state,
            task_id=task_id,
            axis=axis,
            sequence=self.sequence_of(task_id, axis),
            receipt_id=receipt_id,
            observed_at=observed_at,
            reason=(
                "non-authoritative status snapshot: no monotone sequence, "
                "retained as historical observation only"
            ),
        )

    # -- internals -----------------------------------------------------------

    def _reject(
        self,
        receipt: Optional[TaskLifecycleReceipt] = None,
        *,
        state: str = "",
        task_id: str = "",
        axis: str = "",
        sequence: int = 0,
        receipt_id: str = "",
        observed_at: str = "",
        reason: str,
    ) -> LedgerOutcome:
        if receipt is not None:
            task_id = receipt.task_id
            axis = receipt.axis
            state = receipt.state
            sequence = receipt.sequence
            receipt_id = receipt.receipt_id
            observed_at = receipt.observed_at
        observation = LedgerObservation(
            task_id=task_id,
            axis=axis,
            state=state,
            sequence=sequence,
            reason=reason,
            receipt_id=receipt_id,
            observed_at=observed_at,
        )
        self._observations.append(observation)
        outcome = LedgerOutcome(
            task_id=task_id,
            axis=axis,
            sequence=sequence,
            applied=False,
            reason=reason,
            retained_as_observation=True,
        )
        self._rejected.append(outcome)
        return outcome

    # -- projection ----------------------------------------------------------

    def _project_rows(
        self, task_ids: Sequence[str]
    ) -> Tuple[TaskLedgerRow, ...]:
        rows: List[TaskLedgerRow] = []
        for task_id in task_ids:
            states = dict(_AXIS_DEFAULT)
            hold_reason = ""
            refs: List[str] = []
            highest = -1
            for axis in LEDGER_AXES:
                receipt = self._authoritative.get((task_id, axis))
                if receipt is None:
                    continue
                states[axis] = receipt.state
                if receipt.sequence > highest:
                    highest = receipt.sequence
                if receipt.receipt_id and receipt.receipt_id not in refs:
                    refs.append(receipt.receipt_id)
            hold_receipt = self._authoritative.get((task_id, AXIS_HOLD))
            if hold_receipt is not None:
                hold_reason = hold_receipt.hold_reason
            rows.append(
                TaskLedgerRow(
                    task_id=task_id,
                    source=states[AXIS_SOURCE],
                    review=states[AXIS_REVIEW],
                    integration=states[AXIS_INTEGRATION],
                    live_qualification=states[AXIS_LIVE_QUALIFICATION],
                    acceptance=states[AXIS_ACCEPTANCE],
                    hold=states[AXIS_HOLD],
                    hold_reason=hold_reason,
                    sequence=highest,
                    receipt_refs=tuple(refs),
                )
            )
        return tuple(rows)

    def project(self) -> TaskLedgerProjection:
        """Project the ledger over the canonical 24 + addendum 8 sets
        (criterion 3).

        Every canonical task identity gets exactly one row — including tasks
        with no receipt yet, which project their axis defaults — so the ledger
        *reproduces all 24 task states* deterministically and in PRD order. The
        eight ``SW-1511A-*`` addendum identities are projected separately into
        ``addendum_rows`` and reported additively; they never enter ``rows`` and
        so never alter the parent 24-task counts.
        """
        return TaskLedgerProjection(
            rows=self._project_rows(CANONICAL_1511_TASK_IDS),
            observations=tuple(self._observations),
            rejected=tuple(self._rejected),
            addendum_rows=self._project_rows(ADDENDUM_1511A_TASK_IDS),
        )


def project_ledger(ledger: TaskLedger) -> TaskLedgerProjection:
    """Project ``ledger`` over the canonical 24-task set (criterion 3)."""
    return ledger.project()


def ledger_from_receipts(receipts: Iterable[TaskLifecycleReceipt]) -> TaskLedger:
    """Fold an ordered receipt stream into a fresh :class:`TaskLedger`."""
    ledger = TaskLedger()
    for receipt in receipts:
        ledger.record(receipt)
    return ledger


__all__ = [
    "TASK_LIFECYCLE_RECEIPT_SCHEMA_ID",
    "TASK_LIFECYCLE_RECEIPT_SCHEMA_VERSION",
    "SUPPORTED_TASK_LIFECYCLE_RECEIPT_VERSIONS",
    "AXIS_SOURCE",
    "AXIS_REVIEW",
    "AXIS_INTEGRATION",
    "AXIS_LIVE_QUALIFICATION",
    "AXIS_ACCEPTANCE",
    "AXIS_HOLD",
    "LEDGER_AXES",
    "AXIS_STATES",
    "CANONICAL_1511_TASK_IDS",
    "ADDENDUM_1511A_TASK_IDS",
    "LEDGER_KNOWN_TASK_IDS",
    "TaskLedgerError",
    "UnknownCanonicalTaskError",
    "UnknownAxisError",
    "NonMonotoneSequenceError",
    "TaskLifecycleReceipt",
    "LedgerOutcome",
    "LedgerObservation",
    "TaskLedgerRow",
    "TaskLedgerProjection",
    "TaskLedger",
    "project_ledger",
    "ledger_from_receipts",
]
