"""SW-1511A-GATE-001 (criterion 3): the ledger admits the addendum identities.

Criterion 3 requires the ledger to report the **final Source, Integration and
Live-Qualification counts** for the 1.5.11 addendum and to produce one
release-readiness verdict. The 1.5.11 ledger as first written knew only the 24
parent ``SW-1511`` identities, so it refused every ``SW-1511A-*`` receipt with
``UnknownCanonicalTaskError`` and could not count the addendum at all.

The fix admits the eight ``SW-1511A-*`` identities as an explicit *addendum
set* (:data:`ADDENDUM_1511A_TASK_IDS`), projected **additively**: the parent
24-task projection and its counts are unchanged, and the addendum is reported
separately so a reader can tell the two sets apart.

This module is the adversarial proof, exercised on the *real* acceptance
receipts the addendum published: the three fixture files under
``tests/fixtures/sw_1511a_receipts/`` are byte-copies of
``.../2026-10-16-sw1511a/receipts/SW-1511A-*.acceptance.json``. On the frozen
base every assertion below fails, because ingesting any addendum receipt raises
``UnknownCanonicalTaskError`` before a single addendum row can be projected.
"""

import json
import sys
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

#: The eight addendum identities, exactly the PRD's set for SW-1511A.
_EXPECTED_ADDENDUM_IDS = (
    "SW-1511A-LEDGER-001",
    "SW-1511A-RESULT-001",
    "SW-1511A-LIVENESS-001",
    "SW-1511A-ENV-001",
    "SW-1511A-UPSTREAM-001",
    "SW-1511A-INTEGRATION-001",
    "SW-1511A-HANDOFF-001",
    "SW-1511A-GATE-001",
)

#: The shipped acceptance receipts under test (real files, not synthesised).
_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "sw_1511a_receipts"
)
_FIXTURE_NAMES = (
    "SW-1511A-LEDGER-001.acceptance.json",
    "SW-1511A-RESULT-001.acceptance.json",
    "SW-1511A-ENV-001.acceptance.json",
)


def _import_ledger():
    from skillweave.trace import task_ledger

    return task_ledger


def _fixture_receipts():
    """Load the fixture acceptance receipts as ``(task_id, document)`` pairs."""
    loaded = []
    for name in _FIXTURE_NAMES:
        payload = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
        loaded.append((payload["task"], payload))
    return loaded


def test_addendum_identities_are_declared_as_a_separate_set():
    """The eight addendum ids are known, in PRD order, *without* widening the
    parent 24-task set or its pinned count."""
    tl = _import_ledger()

    assert tuple(tl.ADDENDUM_1511A_TASK_IDS) == _EXPECTED_ADDENDUM_IDS
    # The parent 24 remain exactly 24: the addendum is additive, not merged.
    assert len(tl.CANONICAL_1511_TASK_IDS) == 24
    assert tuple(tl.LEDGER_KNOWN_TASK_IDS) == (
        tuple(tl.CANONICAL_1511_TASK_IDS) + _EXPECTED_ADDENDUM_IDS
    )
    # No addendum id leaked into the parent set.
    assert set(_EXPECTED_ADDENDUM_IDS).isdisjoint(tl.CANONICAL_1511_TASK_IDS)

    # A foreign identity is still refused fail-closed: the surface grew by a
    # declared set, never id-by-id.
    with pytest.raises(tl.UnknownCanonicalTaskError):
        tl.TaskLifecycleReceipt(
            task_id="SW-1511A-NOT-A-TASK",
            axis=tl.AXIS_SOURCE,
            state="present",
            sequence=1,
        )


def test_addendum_acceptance_receipts_ingest_and_project_additively():
    """The real addendum acceptance receipts ingest through both entry points
    and project an addendum row each, with parent counts unchanged."""
    tl = _import_ledger()
    ledger = tl.TaskLedger()

    fixtures = _fixture_receipts()
    assert len(fixtures) == 3

    # An *authoritative* receipt per fixture task: the acceptance axis reaching
    # ``accepted`` is what the ledger counts (state ``ACCEPTED-SOURCE`` is a
    # source-acceptance observation, so it is ingested as history via the
    # snapshot path in addition to the authoritative receipt).
    for index, (task_id, _doc) in enumerate(fixtures, start=1):
        outcome = ledger.record(
            tl.TaskLifecycleReceipt(
                task_id=task_id,
                axis=tl.AXIS_ACCEPTANCE,
                state="accepted",
                sequence=1,
                receipt_id=f"accept:{task_id}",
                observed_at="2026-10-16T00:00:00+00:00",
            )
        )
        assert outcome.applied is True, (task_id, outcome.reason)

        snapshot = ledger.ingest_status_snapshot(
            task_id=task_id,
            axis=tl.AXIS_SOURCE,
            state="present",
            receipt_id=f"receipt-file:{task_id}",
            observed_at="2026-10-16T00:00:00+00:00",
        )
        assert snapshot.applied is False
        assert snapshot.retained_as_observation is True

    projection = ledger.project()

    # The parent 24 are untouched by addendum activity.
    assert len(projection.rows) == 24
    assert projection.accepted_count == 0
    assert projection.open_count == 24

    # The addendum is reported additively, in PRD order, with its own counts.
    assert projection.addendum_task_ids() == _EXPECTED_ADDENDUM_IDS
    assert len(projection.addendum_rows) == 8
    assert projection.addendum_accepted_count == 3
    assert projection.addendum_open_count == 5

    for task_id, _doc in fixtures:
        row = projection.row(task_id)
        assert row.acceptance == "accepted"
        # The snapshot is non-authoritative, so it never lands on the source
        # axis; it survives only as history (criterion 2, unchanged by the fix).
        assert row.source == "absent"
        assert row.receipt_refs == (f"accept:{task_id}",)

    # A task the addendum has not accepted yet stays open and uncounted.
    assert projection.is_open("SW-1511A-GATE-001") is True
    assert projection.accepted_count + projection.addendum_accepted_count == 3

    # The addendum counts survive JSON serialization alongside the parent keys.
    payload = json.loads(json.dumps(projection.to_dict()))
    assert payload["canonical_task_count"] == 24
    assert payload["addendum_task_count"] == 8
    assert payload["addendum_accepted_count"] == 3
    assert payload["addendum_open_count"] == 5
    assert len(payload["rows"]) == 24
    assert len(payload["addendum_rows"]) == 8
    assert payload["open_task_ids"] == list(projection.open_task_ids())
