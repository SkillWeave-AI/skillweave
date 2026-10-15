"""SW-SCOPE-010 lane S10-B: the controller's transition checkpoint.

Criterion: *on every transition the controller persists a checkpoint keyed by
run, derived from durable dispatch intents and the journal sequence, never from
caller-supplied values.*

These tests fail on the S10-B base commit (4659ae1): there the controller drove
the loop and left the intent transitions durable, but **nothing ever wrote a
``checkpoints`` row**, so a recovering controller had no run-keyed statement of
the driven state.  Every assertion below is about that written, derived record.

No MCP provider, gateway, worker or reviewer is contacted: the worker/reviewer
ports are injected fakes and the drive runs fully in-process.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from skillweave.cli import native_controller as nc
from skillweave.runtime.native_process import (
    CHECKPOINT_SCHEMA_ID,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
    NativeProcessService,
)
from skillweave.runtime.store import SQLiteRunStore

_PASS_EVIDENCE = (
    {
        "schema_version": "independent-gate-receipt/1.0.0",
        "evidence_type": "member",
        "verifier_identity": "reviewer-1",
        "verdict": "PASS",
    },
)


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "store.db")


def _seed_run(store: SQLiteRunStore, run_id: str) -> None:
    store.create_run(run_id)
    record = store.get_run(run_id)
    store.transition(
        run_id, "batch_selection", record.state, record.version, reason="test setup"
    )


def _seed_review_record(store: SQLiteRunStore, run_id: str, verdict: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    store._conn.execute(
        "INSERT INTO transitions_log "
        "(run_id, from_state, to_state, reason, version, timestamp, role) "
        "VALUES (?, 'batch_selection', 'advance_or_stop', ?, 1, ?, 'reviewer')",
        (run_id, f"Review verdict {verdict}: ok", now),
    )
    store._conn.commit()


class _Worker:
    def execute(self, dispatch):
        return nc.AttemptExecution(producer_artifact="artifact:produced")


class _Reviewer:
    def __init__(self, store, run_id):
        self._store = store
        self._run_id = run_id

    def review(self, dispatch, execution):
        return nc.ReviewVerdict(
            verdict="pass",
            evidence=_PASS_EVIDENCE,
            verifier_identity="reviewer-1",
        )


def _drive(tmp_path, capsys) -> tuple[SQLiteRunStore, str]:
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=_Worker(),
        reviewer=_Reviewer(store, "run-1"),
        store=store,
    )
    assert code == nc.EXIT_OK
    capsys.readouterr()
    return store, db


# ── The criterion ──────────────────────────────────────────────────────────

def test_every_transition_persists_a_checkpoint_keyed_by_run(tmp_path, capsys):
    """After the drive a checkpoint exists under the run the intents name.

    Fails on the base: no ``checkpoints`` row is ever written, so
    ``get_checkpoint`` returns ``None``.
    """
    store, _ = _drive(tmp_path, capsys)
    checkpoint = store.get_checkpoint("run-1")
    assert checkpoint is not None, (
        "the controller transitioned the intent but persisted no run-keyed "
        "checkpoint"
    )
    # The key is the run, and the run is the one the *durable intents* name.
    intents = store.list_dispatch_intents(run_id="run-1")
    assert intents, "the drive recorded no durable intent"
    assert checkpoint.run_id == intents[0].run_id == "run-1"
    assert checkpoint.metadata["schema_id"] == CHECKPOINT_SCHEMA_ID
    store.close()


def test_checkpoint_journal_offset_is_derived_from_the_store(tmp_path, capsys):
    """The persisted offset is the store's own journal cursor, not an argument."""
    store, _ = _drive(tmp_path, capsys)
    checkpoint = store.get_checkpoint("run-1")
    assert checkpoint is not None
    assert checkpoint.journal_offset == store.last_journal_sequence("run-1")
    store.close()


def test_checkpoint_records_the_last_transition_step(tmp_path, capsys):
    """The checkpoint names which transition last occurred (the dependent wave)."""
    store, _ = _drive(tmp_path, capsys)
    checkpoint = store.get_checkpoint("run-1")
    assert checkpoint is not None
    in_flight = checkpoint.metadata["in_flight"]
    assert in_flight, "the checkpoint records no in-flight intent state"
    scope, step = in_flight[0]
    assert step == STEP_DEPENDENT_WAVE
    assert scope == "run-1/task-a/1"
    store.close()


# ── Adversarial: never caller-supplied ─────────────────────────────────────

def test_a_caller_value_cannot_repoint_the_checkpoint(tmp_path, capsys):
    """A caller-named run cannot create or capture a checkpoint.

    The run key is *derived* from the service's durable intents.  Two
    directions, both fail-closed:

    * a service whose durable records name no run persists nothing under the
      caller's label;
    * a service sharing the driven store persists under the run its *intents*
      name (``run-1``), never under the caller-named run.

    On the base this is an ``AttributeError`` (no derived write path exists).
    """
    # No durable intents at all: the caller's label is not a run key.
    empty = SQLiteRunStore(_db(tmp_path))
    written = NativeProcessService(
        empty, "caller-named-run"
    ).persist_transition_checkpoint(STEP_READY_DISPATCH)
    assert written is None
    assert empty.get_checkpoint("caller-named-run") is None
    empty.close()

    # Sharing the driven store: the write lands under the *durable* run.
    store, _ = _drive(tmp_path, capsys)
    NativeProcessService(store, "caller-named-run").persist_transition_checkpoint(
        STEP_READY_DISPATCH
    )
    assert store.get_checkpoint("caller-named-run") is None
    assert store.get_checkpoint("run-1") is not None
    store.close()


def test_recovery_reads_the_written_checkpoint_without_replaying(tmp_path, capsys):
    """A fresh controller reads the driven state from the written checkpoint.

    The written row is the durable statement a recovering controller resumes
    from: it names the run, the store's journal cursor and the intent state the
    transitions left behind -- so resume reads a *written* record rather than
    replaying the loop.  On the base no such durable transition statement
    exists (``get_checkpoint`` returns ``None``).
    """
    store, db = _drive(tmp_path, capsys)
    recovered = SQLiteRunStore(db)
    # The recovering controller reads the durable statement, not a replay.
    from_written = recovered.get_checkpoint("run-1")
    assert from_written is not None
    assert from_written.journal_offset == recovered.last_journal_sequence("run-1")
    assert any(
        scope == "run-1/task-a/1" for scope, _ in from_written.metadata["in_flight"]
    )
    # And its own recovery checkpoint agrees on the run and the cursor.
    state = NativeProcessService(recovered, "recovering-controller").checkpoint()
    assert state.run_id == "run-1"
    assert state.journal_offset == recovered.last_journal_sequence("run-1")
    recovered.close()
    store.close()
