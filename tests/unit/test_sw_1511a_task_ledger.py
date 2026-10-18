"""SW-1511A-LEDGER-001: the authoritative task lifecycle and acceptance ledger.

This file is the adversarial proof for the three lane criteria. Each criterion
has a dedicated ``test_criterionN_*`` function that is written to *fail* on
the frozen base: the base has no ``skillweave.trace.task_ledger`` module at
all, so every criterion test reaches the ledger through an explicit
``_import_ledger()`` that raises (never skips, never xfails) when the contract
is absent.

* criterion 1 -- every canonical task exposes *separate* source, review,
  integration, live-qualification, acceptance and hold states derived from
  receipts with monotone per-axis sequence numbers;
* criterion 2 -- conflicting or stale status inputs are rejected *or* retained
  as historical observations and can never overwrite a newer authoritative
  state;
* criterion 3 -- the 1.5.11 ledger reproduces all 24 task states and identifies
  ``SW-1511-CMS-FLOWS`` and ``SW-1511-GATE`` as open without manual counting.

Determinism is a property under test, not an assumption: the whole stream is
replayed from an empty ledger and the two projections must be byte-equal.
Nothing here launches a process, reads a wall clock, opens a socket or touches
a provider credential.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

def _import_ledger():
    """Import the ledger contract, or fail the test on the frozen base.

    The base SHA this task is measured against has no ledger module: the
    resulting :class:`ModuleNotFoundError` is the red proof. It is deliberately
    *not* converted to a skip or an xfail.
    """
    from skillweave.trace import task_ledger

    return task_ledger


#: A standalone legacy status-file snapshot. It names the same task twice and
#: carries a state the ledger authoritatively holds as newer, lower down the
#: stream. It has no monotone sequence, so it has no authority.
LEGACY_STATUS_SNAPSHOT = {
    "generated_by": "controller-status-writer (pre-ledger)",
    "tasks": [
        {"id": "SW-1511-CMS-FLOWS", "status": "passes", "detail": "stale replay"},
        {"id": "SW-1511-GATE", "status": "passes", "detail": "conflicting rewrite"},
    ],
}


def _ordered_stream():
    """The ordered authoritative receipt stream used by every criterion test.

    Sequence is monotone *per (task_id, axis)* and advances by one each time,
    exactly as ``TaskLedger.next_sequence`` prescribes. ``timestamp``/``ref``
    are typed facts the producer recorded; the ledger reads neither a clock nor
    a file.
    """
    tl = _import_ledger()
    return [
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_SOURCE,
            state="present",
            sequence=1,
            receipt_id="r-base-source",
            observed_at="2026-10-16T09:00:00+00:00",
        ),
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_REVIEW,
            state="review_passed",
            sequence=2,
            receipt_id="r-base-review",
            observed_at="2026-10-16T09:05:00+00:00",
        ),
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_INTEGRATION,
            state="integrated",
            sequence=3,
            receipt_id="r-base-integration",
            observed_at="2026-10-16T09:10:00+00:00",
        ),
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_ACCEPTANCE,
            state="accepted",
            sequence=4,
            receipt_id="r-base-acceptance",
            observed_at="2026-10-16T09:15:00+00:00",
        ),
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_LIVE_QUALIFICATION,
            state="not_applicable",
            sequence=5,
            receipt_id="r-base-live-na",
            observed_at="2026-10-16T09:20:00+00:00",
        ),
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-CMS-FLOWS",
            axis=tl.AXIS_SOURCE,
            state="present",
            sequence=1,
            receipt_id="r-flows-source",
            observed_at="2026-10-16T09:25:00+00:00",
        ),
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-GATE",
            axis=tl.AXIS_HOLD,
            state="named_open",
            sequence=1,
            receipt_id="r-gate-hold",
            observed_at="2026-10-16T09:30:00+00:00",
            hold_reason=(
                "CMS publication prerequisites unavailable; remains a named HOLD"
            ),
        ),
    ]


def _replay(stream):
    tl = _import_ledger()
    ledger = tl.TaskLedger()
    for receipt in stream:
        ledger.record(receipt)
    return ledger


# ── criterion 1 ──────────────────────────────────────────────────────────────


def test_criterion1_axes_are_separate_and_monotone_per_axis():
    """Criterion 1: separate axes, each advanced by its own monotone sequence.

    Red on the base: there is no ledger module, so no axis can be read at all.
    """
    tl = _import_ledger()

    # The six axes are declared separately and in contract order.
    assert tl.LEDGER_AXES == (
        "source",
        "review",
        "integration",
        "live_qualification",
        "acceptance",
        "hold",
    )
    assert len(set(tl.LEDGER_AXES)) == 6, "an axis must not alias another"

    stream = _ordered_stream()
    ledger = _replay(stream)

    # Each receipt advanced exactly its own (task, axis) coordinate: the axes
    # are genuinely independent, not derived from one another.
    assert ledger.sequence_of("SW-1511-BASE", tl.AXIS_SOURCE) == 1
    assert ledger.sequence_of("SW-1511-BASE", tl.AXIS_REVIEW) == 2
    assert ledger.sequence_of("SW-1511-BASE", tl.AXIS_INTEGRATION) == 3
    assert ledger.sequence_of("SW-1511-BASE", tl.AXIS_ACCEPTANCE) == 4
    assert ledger.sequence_of("SW-1511-BASE", tl.AXIS_LIVE_QUALIFICATION) == 5
    # No receipt ever named this axis: it stays unset, so nothing was inferred.
    assert ledger.sequence_of("SW-1511-BASE", tl.AXIS_HOLD) == -1

    row = ledger.project().row("SW-1511-BASE")
    assert row.source == "present"
    assert row.review == "review_passed"
    assert row.integration == "integrated"
    assert row.acceptance == "accepted"
    # A live-criterion gap is *named*, never collapsed into a process failure.
    assert row.live_qualification == "not_applicable"
    assert row.hold == "none"

    # Every axis state is in its own closed vocabulary: nothing leaked across.
    for axis in tl.LEDGER_AXES:
        assert row.axis_state(axis) in tl.AXIS_STATES[axis]

    # The hold axis is independent of acceptance: GATE is held open while its
    # acceptance axis is untouched, and its reason survives.
    gate = ledger.project().row("SW-1511-GATE")
    assert gate.hold == "named_open"
    assert gate.acceptance == "pending"
    assert "HOLD" in gate.hold_reason

    # A state outside an axis' vocabulary is refused at construction, so an
    # illegal crossing (a live state on the review axis) can never be folded.
    with pytest.raises(tl.UnknownAxisError):
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_REVIEW,
            state="qualified",
            sequence=6,
        )
    with pytest.raises(tl.UnknownAxisError):
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis="deployment",
            state="present",
            sequence=6,
        )

    # The v1 receipt round-trips with its schema identity and version, so a
    # later reader can tell which contract wrote it.
    envelope = stream[0].to_dict()
    assert envelope["schema"] == tl.TASK_LIFECYCLE_RECEIPT_SCHEMA_ID
    assert envelope["schema_version"] == tl.TASK_LIFECYCLE_RECEIPT_SCHEMA_VERSION
    assert envelope["axis"] == "source" and envelope["sequence"] == 1
    # Backward compatibility is explicit, not assumed: only declared versions
    # are accepted, and an unknown one is refused rather than coerced.
    assert (
        tl.TASK_LIFECYCLE_RECEIPT_SCHEMA_VERSION
        in tl.SUPPORTED_TASK_LIFECYCLE_RECEIPT_VERSIONS
    )
    with pytest.raises(tl.TaskLedgerError):
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-BASE",
            axis=tl.AXIS_SOURCE,
            state="present",
            sequence=6,
            schema_version="2.0.0",
        )

    # Each axis owns its own monotone counter: the next sequence a producer may
    # use is the axis' current sequence plus one, per axis.
    assert ledger.next_sequence("SW-1511-BASE", tl.AXIS_REVIEW) == 3
    assert ledger.next_sequence("SW-1511-CMS-FLOWS", tl.AXIS_SOURCE) == 2
    assert ledger.next_sequence("SW-1511-GATE", tl.AXIS_HOLD) == 2

    # Replay from an empty ledger is deterministic: identical ordered input,
    # identical projection.
    again = _replay(stream)
    assert again.project().to_dict() == ledger.project().to_dict()


# ── criterion 2 ──────────────────────────────────────────────────────────────


def test_criterion2_conflicting_and_stale_inputs_cannot_overwrite_newer_state():
    """Criterion 2: reject-or-retain, never overwrite a newer authoritative state.

    Red on the base: no ledger exists to reject or retain anything.
    """
    tl = _import_ledger()
    stream = _ordered_stream()
    ledger = _replay(stream)

    authoritative_before = ledger.project().row("SW-1511-CMS-FLOWS")
    assert authoritative_before.acceptance == "pending"

    # (a) A *stale* replay: the same axis at a sequence the ledger has passed.
    stale = tl.TaskLifecycleReceipt(
        task_id="SW-1511-CMS-FLOWS",
        axis=tl.AXIS_SOURCE,
        state="accepted",
        sequence=0,
        receipt_id="r-flows-stale",
    )
    outcome = ledger.record(stale)
    assert outcome.applied is False
    assert "stale" in outcome.reason
    assert outcome.retained_as_observation is True

    # (b) A *conflicting* rewrite: same sequence, a different state.
    conflict = tl.TaskLifecycleReceipt(
        task_id="SW-1511-CMS-FLOWS",
        axis=tl.AXIS_SOURCE,
        state="absent",
        sequence=1,
        receipt_id="r-flows-conflict",
    )
    outcome = ledger.record(conflict)
    assert outcome.applied is False
    assert "conflict" in outcome.reason
    assert outcome.retained_as_observation is True

    # (c) A legacy status file carries no monotone sequence and therefore no
    # authority: it is retained as history and never applied.
    snapshot = LEGACY_STATUS_SNAPSHOT["tasks"][0]
    outcome = ledger.ingest_status_snapshot(
        task_id=snapshot["id"],
        axis=tl.AXIS_SOURCE,
        state="absent",
        receipt_id="controller-status-writer",
        observed_at="2026-10-16T08:00:00+00:00",
    )
    assert outcome.applied is False
    assert outcome.retained_as_observation is True

    # The authoritative state is untouched by all three inputs ...
    after = ledger.project().row("SW-1511-CMS-FLOWS")
    assert after.source == "present"
    assert after.to_dict() == authoritative_before.to_dict()

    # ... and re-reading it (in any later order) still yields it.
    ledger.record(
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-CMS-FLOWS",
            axis=tl.AXIS_SOURCE,
            state="present",
            sequence=1,
            receipt_id="r-flows-source",
        )
    )
    assert ledger.project().row("SW-1511-CMS-FLOWS").source == "present"

    # ... and every rejected input survives as a historical observation.
    retained = {o.receipt_id: o for o in ledger.observations()}
    assert retained["r-flows-stale"].reason.startswith("stale")
    assert retained["r-flows-conflict"].reason.startswith("conflict")
    assert "r-flows-stale" in retained
    assert "r-flows-conflict" in retained
    assert len(ledger.rejected()) == 3

    # A receipt that does not advance strictly is never silently absorbed: the
    # strict path fails closed with a typed error.
    with pytest.raises(tl.NonMonotoneSequenceError):
        ledger.apply(stale)

    # A conflicting rewrite of a *newer* authoritative task is likewise refused.
    base_conflict = tl.TaskLifecycleReceipt(
        task_id="SW-1511-BASE",
        axis=tl.AXIS_ACCEPTANCE,
        state="rejected",
        sequence=4,
        receipt_id="r-base-conflict",
    )
    assert ledger.record(base_conflict).applied is False
    assert ledger.project().row("SW-1511-BASE").acceptance == "accepted"

    # Invariants: an applied receipt is never also reported as retained, and
    # nothing was applied twice at the same coordinate.
    outcomes = []
    for receipt in stream:
        outcomes.append(ledger.record(receipt))
    for out in outcomes:
        if out.applied:
            assert out.retained_as_observation is False

    # The criterion-2 invariant, stated directly: the authoritative rows are a
    # pure function of the ordered stream. A ledger fed only that stream
    # projects byte-identical rows, so no rejected or stale input left a trace
    # in authoritative state — it survived only as observation history.
    only_authoritative = _replay(stream)
    assert only_authoritative.project().rows == ledger.project().rows

    # The same authoritative receipt re-delivered is an idempotent no-op: it is
    # neither applied a second time nor recorded as a new observation.
    redelivery = ledger.record(stream[1])
    assert redelivery.applied is False
    assert "idempotent" in redelivery.reason
    assert redelivery.retained_as_observation is False

    # A status snapshot naming a task that never received a receipt still cannot
    # manufacture acceptance: the acceptance axis keeps its default.
    ledger.ingest_status_snapshot(
        task_id="SW-1511-GATE", axis=tl.AXIS_ACCEPTANCE, state="accepted"
    )
    assert ledger.project().row("SW-1511-GATE").acceptance == "pending"

    # ... and offered against a newer authoritative acceptance it is retained as
    # history while the newer authoritative state stands.
    snapshot_after = ledger.ingest_status_snapshot(
        task_id="SW-1511-BASE", axis=tl.AXIS_ACCEPTANCE, state="rejected"
    )
    assert snapshot_after.applied is False
    assert ledger.project().row("SW-1511-BASE").acceptance == "accepted"


# ── criterion 3 ──────────────────────────────────────────────────────────────


def test_criterion3_reproduces_all_24_task_states_and_names_open_tasks():
    """Criterion 3: all 24 states reproduced; CMS-FLOWS and GATE open, uncounted.

    Red on the base: no projection exists, so no task state can be reproduced.
    """
    tl = _import_ledger()
    projection = _replay(_ordered_stream()).project()

    # (a) Every canonical identity is reproduced, in PRD order.
    assert len(tl.CANONICAL_1511_TASK_IDS) == 24
    assert projection.task_ids == tuple(tl.CANONICAL_1511_TASK_IDS)
    assert len(set(projection.task_ids)) == 24

    # (b) The ledger *derives* openness; the caller never counts by hand.
    open_ids = projection.open_task_ids()
    assert "SW-1511-CMS-FLOWS" in open_ids
    assert "SW-1511-GATE" in open_ids
    assert projection.is_open("SW-1511-CMS-FLOWS") is True
    assert projection.is_open("SW-1511-GATE") is True
    assert projection.open_count == len(open_ids) == 23
    assert projection.accepted_count == 1
    assert projection.accepted_count + projection.open_count == 24

    # A task is open unless its *acceptance* axis is authoritatively accepted:
    # a source-present or review-passed task is still open.
    assert projection.row("SW-1511-CMS-FLOWS").source == "present"
    assert projection.is_open("SW-1511-CMS-FLOWS") is True

    # (c) The identity set is the literal frozen 1.5.11 canonical 24, in PRD
    # order. Spelling it out keeps a drift between the ledger and the frozen
    # plan a hard failure rather than a silent divergence.
    assert tuple(tl.CANONICAL_1511_TASK_IDS) == (
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

    # (d) The projection is JSON-serializable evidence (no clock, no handle).
    payload = json.loads(json.dumps(projection.to_dict()))
    assert payload["canonical_task_count"] == 24
    assert len(payload["rows"]) == 24
    assert payload["open_task_ids"] == list(open_ids)
    assert all(row["task_id"] for row in payload["rows"])

    # A receipt for a task outside the frozen identity set is refused, so the
    # 24-task surface cannot silently grow.
    with pytest.raises(tl.UnknownCanonicalTaskError):
        tl.TaskLifecycleReceipt(
            task_id="SW-1511-NOT-A-CANONICAL-TASK",
            axis=tl.AXIS_SOURCE,
            state="present",
            sequence=1,
        )

    # A ledger rebuilt from the whole ordered stream via the public constructor
    # helper projects identically, so the projection is reconstructible from
    # persisted receipts alone (no live state, no manual reconstruction).
    rebuilt = tl.ledger_from_receipts(_ordered_stream()).project()
    assert rebuilt.to_dict() == projection.to_dict()
    assert tl.project_ledger(tl.ledger_from_receipts(_ordered_stream())).to_dict() == (
        projection.to_dict()
    )

    # The barrel re-exports the same contract objects: ``skillweave.trace`` is
    # the documented consumer import path and must name identical classes.
    barrel = importlib.import_module("skillweave.trace")
    assert barrel.TaskLedger is tl.TaskLedger
    assert barrel.CANONICAL_1511_TASK_IDS is tl.CANONICAL_1511_TASK_IDS

