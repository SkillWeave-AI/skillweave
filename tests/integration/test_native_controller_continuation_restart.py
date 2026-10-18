"""SW-SCOPE-010 lane S10-B criterion 3: the recovered controller *reads* the
persisted ``harness-continuation-v1`` decision and continues exactly one
recovered attempt.

Criterion 3: *the continuation decision persisted by the handoff tool is read
by the recovered controller through a reader scoped to its run, and a real
subprocess kill and restart shows exactly one recovered attempt and a strictly
higher fencing token.*

The happy path drives the **real handoff tool** (``handoff_adapter``) to persist
the decision to the run's durable mailbox, then restarts the controller with
``--resume --principal in-process`` over a **real, still-long-lived child
process** recorded as the intent's owned identity.  The restart adopts the
ordering-first in-flight intent (verifying that child against the live OS),
reads back the persisted decision through the run-scoped reader
(:func:`skillweave.mcp.server.read_continuation_decision`), and reports exactly
one recovered intent under a strictly higher fencing token -- it never launches
a second attempt.

Every adversarial case is one named continuation hold: a decision persisted for
another run (``..._foreign_run``), no decision at all (``..._not_persisted``)
and a decision recorded for a different owned attempt (``..._replayed``).  In
each the restart refuses by name and the predecessor's launch intent is never
replayed.

Nothing here contacts a provider, gateway, worker or reviewer: the durable
mailbox is a temporary file, the store is temporary, and the only OS process
observed is a locally spawned child of this test.  Live-run parts (an actual
worker/gateway dispatch) are NOT PERFORMED.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from skillweave.cli import native_controller as nc
from skillweave.dispatch import messaging as messaging
from skillweave.mcp import server as mcp_server
from skillweave.runtime import process_identity
from skillweave.runtime.checkpoint import Checkpoint, EnvironmentFingerprint
from skillweave.runtime.native_process import (
    CHECKPOINT_SCHEMA_ID,
    HARNESS_CONTINUATION_SCHEMA_ID,
    STEP_READY_DISPATCH,
    NativeProcessService,
)
from skillweave.runtime.store import SQLiteRunStore

_RUN_ID = "run-1"
_SCOPE = "run-1/task-a/1"
_INTENT_ID = "intent-live"
#: The CLI-parity principal the handoff tool persists under and the recovered
#: controller reads under when the caller names no authority.
_PRINCIPAL = "in-process"


def _spawn_stable_child() -> subprocess.Popen:
    """A real, long-lived child whose *image* is stable for its whole life.

    ``process_identity.verify`` compares the recorded ``exec_path`` with the
    live one, so the adopted identity has to name a process whose image never
    changes.  A ``sys.executable`` child does not qualify on every host: the
    interpreter can be launched through a shim that ``execve``-replaces itself
    with the real framework image, so a capture taken before the replacement
    records a path the process no longer runs -- a *transient* ``foreign_worker``
    that would make the identity an observation-race flake rather than the
    durable record under test.  ``/bin/sleep`` is a single static image, so the
    capture and the restart's re-observation always agree.
    """
    return subprocess.Popen(["/bin/sleep", "120"])


def _stop(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


# ── Seeding helpers (mirrors tests/integration/test_native_controller_restart_adoption.py) ──


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "store.db")


def _seed_run(store: SQLiteRunStore, run_id: str) -> None:
    store.create_run(run_id)
    record = store.get_run(run_id)
    store.transition(
        run_id, "batch_selection", record.state, record.version, reason="test setup"
    )


def _seed_intent(
    store: SQLiteRunStore,
    *,
    intent_id: str,
    process_identity: dict | None,
    session_id: str = "",
    created_at: str = "2026-01-01T00:00:00+00:00",
    scope: str = _SCOPE,
    run_id: str = _RUN_ID,
) -> None:
    """Record one durable in-flight intent with a deterministic order key."""
    store.record_dispatch_intent(
        run_id=run_id,
        task_id="task-a",
        attempt=1,
        execution_scope=scope,
        step=STEP_READY_DISPATCH,
        state="launched",
        session_id=session_id,
        process_identity=process_identity,
        intent_id=intent_id,
    )
    store._conn.execute(
        "UPDATE dispatch_intents SET created_at = ? WHERE intent_id = ?",
        (created_at, intent_id),
    )
    store._conn.commit()


def _write_checkpoint(store: SQLiteRunStore, run_id: str, scopes: list[str]) -> None:
    """Write the run-keyed transition checkpoint a predecessor left behind."""
    store.save_checkpoint(
        Checkpoint(
            run_id=run_id,
            root_run_id=run_id,
            parent_run_id=None,
            journal_offset=store.last_journal_sequence(run_id),
            environment=EnvironmentFingerprint("", "", "", "", "", {}),
            metadata={
                "schema_id": CHECKPOINT_SCHEMA_ID,
                "in_flight": [[scope, STEP_READY_DISPATCH] for scope in scopes],
            },
        )
    )


def _backdate_lease(store: SQLiteRunStore) -> int:
    """Take the lease, then simulate a crashed controller whose lease expired.

    Returns the (stale) fencing token so the restart's token can be shown to be
    strictly higher.
    """
    holder = NativeProcessService(store, "predecessor")
    holder.acquire(ttl_seconds=1)
    stale_token = int(holder.fencing_token)
    store._conn.execute(
        "UPDATE controller_lease SET lease_until = '2020-01-01T00:00:00', "
        "released_at = NULL WHERE lease_key = 'controller'"
    )
    store._conn.commit()
    return stale_token


def _persist_through_handoff_tool(db: str, mailbox_path: Path, monkeypatch) -> dict:
    """Persist the continuation decision via the **real handoff tool**.

    The tool reads the run's ordering-first *verified* in-flight attempt and the
    client's attested capability, derives the decision and persists it to the
    run's durable mailbox -- exactly the production seam a recovered controller
    then reads back.  No authority is bound, so the direct-caller principal
    (``in-process``) is used, matching the CLI-parity path.
    """
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    return json.loads(
        mcp_server.handoff_adapter(
            client_id="cli", recipients="ops", run_id=_RUN_ID,
            db_path=db, session_token="",
        )
    )


def _post_raw_decision(
    mailbox_path: Path, *, run_id: str, body: dict, message_id: str
) -> None:
    """Post one durable status record carrying a crafted continuation body.

    This writes the durable layer directly (the mailbox), the same record shape
    the tool persists, so the reader's own run-scoping is what the adversarial
    case exercises.
    """
    AgentMailbox, AgentMessage = messaging.AgentMailbox, messaging.AgentMessage
    mailbox = AgentMailbox(str(mailbox_path))
    mailbox.post(
        AgentMessage(
            kind="status",
            sender=_PRINCIPAL,
            recipient=_PRINCIPAL,
            principal=_PRINCIPAL,
            run_id=run_id,
            message_id=message_id,
            body=body,
        )
    )


class _NeverCalledWorker:
    """A launch seam that must never be reached on a restart drive."""

    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, dispatch):
        self.calls.append(dispatch)
        raise AssertionError(
            "a restart replayed the launch intent instead of adopting it"
        )


class _NeverCalledReviewer:
    def review(self, dispatch, execution):
        raise AssertionError("a restart replayed the review instead of adopting")


def _resume(store: SQLiteRunStore, db: str, *, principal: str = _PRINCIPAL):
    """Drive a *restart* and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    worker = _NeverCalledWorker()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = nc.main(
            [
                "--run-id", _RUN_ID, "--task-id", "task-a", "--db-path", db,
                "--resume", "--principal", principal,
            ],
            worker=worker,
            reviewer=_NeverCalledReviewer(),
            store=store,
        )
    assert worker.calls == [], "the restart dispatched a launch intent"
    return code, out.getvalue(), err.getvalue()


# ── The criterion: a real subprocess kill + restart continues one attempt ───


def test_restart_of_a_killed_controller_reads_the_persisted_decision(
    tmp_path, monkeypatch
):
    """A real long-lived child is the recorded owner; a restart continues it.

    A predecessor controller is killed (its lease left expired), its recorded
    owned attempt is a **real child process of this test** that is still alive,
    and the handoff tool persisted the continuation decision for the run.  The
    restart with ``--resume --principal in-process`` adopts that one in-flight
    intent, reads back the *persisted* decision through the run-scoped reader,
    and shows exactly one recovered intent under a strictly higher fencing
    token.  On the S10-B base commit ``--resume``/``--principal`` do not exist
    (argparse exit 2) and no continuation read exists at all.
    """
    db = _db(tmp_path)
    mailbox_path = tmp_path / "mb.jsonl"
    store = SQLiteRunStore(db)
    _seed_run(store, _RUN_ID)

    # A real, still-live child process is the recorded owned identity -- the
    # restart re-observes the live OS, nothing is fabricated.
    child = _spawn_stable_child()
    try:
        child_identity = process_identity.capture(child.pid)
        assert child_identity is not None, "could not observe the live child"
        _seed_intent(
            store, intent_id=_INTENT_ID,
            process_identity=child_identity.to_dict(), session_id="sess-child",
        )
        _write_checkpoint(store, _RUN_ID, [_SCOPE])

        # The handoff tool derives and persists the continuation decision for
        # the *verified* in-flight attempt, into the run's durable mailbox.
        persisted = _persist_through_handoff_tool(db, mailbox_path, monkeypatch)
        assert persisted["status"] == "ok", persisted
        assert persisted["continuation_persisted"]["status"] == "persisted"
        assert persisted["continuation"]["intent_id"] == _INTENT_ID

        stale_token = _backdate_lease(store)

        code, out, err = _resume(store, db)
        assert code == nc.EXIT_OK, err
        result = json.loads(out.strip().splitlines()[-1])

        # The restart adopted the single in-flight intent and read the *same*
        # persisted decision back through the run-scoped reader.
        assert result["adoption"]["adopted"] is True
        assert result["adoption"]["intent_id"] == _INTENT_ID
        assert result["intent_id"] == _INTENT_ID
        assert result["continuation"]["run_id"] == _RUN_ID
        assert result["continuation"]["intent_id"] == _INTENT_ID
        assert result["fencing_token"] > stale_token

        # Exactly one recovered attempt: the restart wrote no second intent.
        claiming = store.list_dispatch_intents(_RUN_ID, claiming_only=True)
        assert [i.intent_id for i in claiming] == [_INTENT_ID]
    finally:
        _stop(child)
        store.close()


def test_fencing_token_is_strictly_higher_than_the_killed_controller(
    tmp_path, monkeypatch
):
    """The recovered controller's token strictly exceeds the predecessor's.

    The token the restart reports (and the persisted checkpoint records) is
    strictly greater than the token the killed controller last held, so a stale
    controller's action can never be mistaken for the recovered one's.
    """
    db = _db(tmp_path)
    mailbox_path = tmp_path / "mb.jsonl"
    store = SQLiteRunStore(db)
    _seed_run(store, _RUN_ID)

    child = _spawn_stable_child()
    try:
        child_identity = process_identity.capture(child.pid)
        assert child_identity is not None
        _seed_intent(
            store, intent_id=_INTENT_ID,
            process_identity=child_identity.to_dict(),
        )
        _write_checkpoint(store, _RUN_ID, [_SCOPE])
        _persist_through_handoff_tool(db, mailbox_path, monkeypatch)
        stale_token = _backdate_lease(store)

        code, out, err = _resume(store, db)
        assert code == nc.EXIT_OK, err
        result = json.loads(out.strip().splitlines()[-1])
        assert result["fencing_token"] == store.current_fencing_token()
        assert result["fencing_token"] > stale_token
    finally:
        _stop(child)
        store.close()


# ── Adversarial: one named continuation hold per refusal ───────────────────


def _seed_adoptable(tmp_path, monkeypatch, mailbox_path: Path):
    """Seed an adoptable in-flight attempt with a live child identity.

    Returns ``(store, db)``; the caller owns terminating the process.
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, _RUN_ID)
    child = _spawn_stable_child()
    child_identity = process_identity.capture(child.pid)
    assert child_identity is not None
    _seed_intent(store, intent_id=_INTENT_ID, process_identity=child_identity.to_dict())
    _write_checkpoint(store, _RUN_ID, [_SCOPE])
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    return store, db, child


def test_restart_refuses_a_foreign_run_decision_named(tmp_path, monkeypatch):
    """A persisted decision naming another run -> ``..._foreign_run``.

    The durable *record* belongs to this run (so the reader returns it), but the
    decision body names a different run: it is a foreign statement, never
    relabelled as this run's, and the restart refuses by name.
    """
    mailbox_path = tmp_path / "mb.jsonl"
    store, db, child = _seed_adoptable(tmp_path, monkeypatch, mailbox_path)
    try:
        _post_raw_decision(
            mailbox_path,
            run_id=_RUN_ID,  # the record is this run's...
            body={
                "schema_id": HARNESS_CONTINUATION_SCHEMA_ID,
                "schema_version": "1.0.0",
                "run_id": "some-other-run",  # ...but the decision is not
                "mode": "checkpoint_continuation",
                "intent_id": _INTENT_ID,
            },
            message_id="harness-continuation-foreign",
        )
        code, _, err = _resume(store, db)
        assert code == nc.EXIT_REFUSED
        assert err == f"refused: {nc.HOLD_CONTINUATION_FOREIGN_RUN}\n"
        # Nothing was replayed: the durable intents are exactly the seeded one.
        assert [i.intent_id for i in store.list_dispatch_intents(_RUN_ID)] == [
            _INTENT_ID
        ]
    finally:
        _stop(child)
        store.close()


def test_restart_refuses_when_no_decision_is_persisted(tmp_path, monkeypatch):
    """An empty mailbox -> ``..._not_persisted`` (named hold)."""
    mailbox_path = tmp_path / "mb.jsonl"
    store, db, child = _seed_adoptable(tmp_path, monkeypatch, mailbox_path)
    try:
        code, _, err = _resume(store, db)
        assert code == nc.EXIT_REFUSED
        assert err == f"refused: {nc.HOLD_CONTINUATION_NOT_PERSISTED}\n"
        assert [i.intent_id for i in store.list_dispatch_intents(_RUN_ID)] == [
            _INTENT_ID
        ]
    finally:
        _stop(child)
        store.close()


def test_restart_reads_nothing_from_another_runs_decision(tmp_path, monkeypatch):
    """The reader is scoped to *this* run: another run's decision is invisible.

    The mailbox holds a well-formed decision, but it belongs to ``other-run``.
    Read for this run it is *not* found (the reader filters by the record's own
    durable run id), so the restart reports the named ``not_persisted`` hold
    rather than continuing from a foreign run's decision.
    """
    mailbox_path = tmp_path / "mb.jsonl"
    store, db, child = _seed_adoptable(tmp_path, monkeypatch, mailbox_path)
    try:
        _post_raw_decision(
            mailbox_path,
            run_id="other-run",
            body={
                "schema_id": HARNESS_CONTINUATION_SCHEMA_ID,
                "schema_version": "1.0.0",
                "run_id": "other-run",
                "mode": "checkpoint_continuation",
                "intent_id": "intent-other-run",
            },
            message_id="harness-continuation-other-run",
        )
        code, _, err = _resume(store, db)
        assert code == nc.EXIT_REFUSED
        assert err == f"refused: {nc.HOLD_CONTINUATION_NOT_PERSISTED}\n"
    finally:
        _stop(child)
        store.close()


def test_restart_refuses_a_decision_for_a_different_attempt(tmp_path, monkeypatch):
    """A decision naming another owned attempt -> ``..._replayed``.

    The decision is for this run but names a *different* attempt than the
    ordering-first in-flight intent being adopted: continuing from it would
    replay a predecessor attempt's continuation, so it is a named hold.
    """
    mailbox_path = tmp_path / "mb.jsonl"
    store, db, child = _seed_adoptable(tmp_path, monkeypatch, mailbox_path)
    try:
        _post_raw_decision(
            mailbox_path,
            run_id=_RUN_ID,
            body={
                "schema_id": HARNESS_CONTINUATION_SCHEMA_ID,
                "schema_version": "1.0.0",
                "run_id": _RUN_ID,
                "mode": "checkpoint_continuation",
                "intent_id": "intent-some-other-attempt",
            },
            message_id="harness-continuation-replayed",
        )
        code, _, err = _resume(store, db)
        assert code == nc.EXIT_REFUSED
        assert err == f"refused: {nc.HOLD_CONTINUATION_REPLAYED}\n"
        assert [i.intent_id for i in store.list_dispatch_intents(_RUN_ID)] == [
            _INTENT_ID
        ]
    finally:
        _stop(child)
        store.close()
