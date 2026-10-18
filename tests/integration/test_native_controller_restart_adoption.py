"""SW-SCOPE-010 lane S10-B: the restarted controller adopts, never replays.

Criterion 2: *a restarted controller reads the checkpoint and the durable
dispatch intents and adopts only the ordering-first in-flight intent whose
owned identity verifies; otherwise it returns a named checkpoint hold, never
replays a launch intent, and the stale-first plus live case is refused.*

These tests fail on the S10-B base commit (cb5d999): there the controller had
no restart read at all -- ``--resume`` did not exist (argparse exit 2) and no
derived adoption/hold path existed (``AttributeError``), so a second drive over
a live in-flight attempt replayed the launch intent and terminated as a raw
``DuplicateDispatchError`` (exit 5) rather than a *named* refusal.

One adversarial test per refusal.  The process identity is captured from the
running test process (``os.getpid()``), so verification is against the real OS
-- no provider, gateway, worker or reviewer is contacted: the worker/reviewer
ports below raise if they are ever called, which is itself the assertion that a
restart *adopts* rather than *dispatches*.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

from skillweave.cli import native_controller as nc
from skillweave.mcp import server as mcp_server
from skillweave.runtime import process_identity
from skillweave.runtime.checkpoint import Checkpoint, EnvironmentFingerprint
from skillweave.runtime.native_process import (
    CHECKPOINT_SCHEMA_ID,
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


def _seed_intent(
    store: SQLiteRunStore,
    *,
    intent_id: str,
    scope: str,
    process_identity: dict | None,
    created_at: str,
    run_id: str = "run-1",
) -> None:
    """Record one durable in-flight intent with a *deterministic* order key.

    ``created_at`` is pinned so the store's own ``ORDER BY created_at,
    intent_id`` frontier is what the tests exercise -- never a coincidence of
    recording speed.
    """
    store.record_dispatch_intent(
        run_id=run_id,
        task_id="task-a",
        attempt=1,
        execution_scope=scope,
        step=STEP_READY_DISPATCH,
        state="launched",
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


def _live_identity() -> dict:
    live = process_identity.capture(os.getpid())
    assert live is not None, "could not observe this running process"
    return live.to_dict()


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


def _resume(store: SQLiteRunStore, db: str, monkeypatch) -> tuple[int, str]:
    """Drive ``run-1`` as a *restart* and return (exit code, stderr).

    A recovered controller reads the run's *persisted* ``harness-continuation-v1``
    decision (criterion 3), so the restart drive persists it first through the
    **real handoff tool** -- the same production seam -- into a temporary
    mailbox.  The adoption this file exercises is then reached exactly as it is
    in the field: an adoptable in-flight attempt *and* the decision the handoff
    tool recorded for it (a run carrying no persisted decision is the named
    continuation hold of ``test_native_controller_continuation_restart.py``).
    """
    import io
    import contextlib

    monkeypatch.setenv(
        mcp_server.MAILBOX_PATH_ENV, str(Path(db).parent / "mb.jsonl")
    )
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    persisted = json.loads(
        mcp_server.handoff_adapter(
            client_id="cli", recipients="ops", run_id="run-1",
            db_path=db, session_token="",
        )
    )
    assert persisted["continuation_persisted"]["status"] == "persisted", persisted

    err = io.StringIO()
    worker = _NeverCalledWorker()
    with contextlib.redirect_stderr(err):
        code = nc.main(
            [
                "--run-id", "run-1", "--task-id", "task-a", "--db-path", db,
                "--resume",
            ],
            worker=worker,
            reviewer=_NeverCalledReviewer(),
            store=store,
        )
    assert worker.calls == [], "the restart dispatched a launch intent"
    return code, err.getvalue()


# ── The criterion: adoption of the ordering-first verifiable intent ────────

def test_restart_adopts_the_ordering_first_verifiable_in_flight_intent(
    tmp_path, capsys, monkeypatch
):
    """A restarted controller adopts the in-flight attempt it can still verify.

    The run carries exactly one in-flight intent (live identity) and the
    predecessor's written checkpoint; ``--resume`` adopts it: exit 0, the
    adopted intent named in the result, and **no second launch intent**.
    On the base ``--resume`` does not exist (argparse exit 2).
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/1",
        process_identity=_live_identity(), created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    code, _ = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_OK
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["adoption"]["adopted"] is True
    assert result["adoption"]["intent_id"] == "intent-live"
    assert result["adoption"]["execution_scope"] == "run-1/task-a/1"
    assert result["adoption"]["reason"] == "match"
    assert result["adoption"]["owned_identity"]["pid"] == os.getpid()
    assert result["intent_id"] == "intent-live"
    # Adopted, not replayed: the durable intents are exactly the seeded one.
    intents = store.list_dispatch_intents(run_id="run-1")
    assert [i.intent_id for i in intents] == ["intent-live"]
    store.close()


def test_the_adopted_identity_is_the_durable_record_not_a_new_capture(
    tmp_path, capsys, monkeypatch
):
    """The adopted ``owned_identity`` repeats the *durable* record verbatim.

    A restart must not manufacture a fresh identity for the same PID: the
    record it reports is byte-for-byte the one the dispatch intent stored.
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    live = _live_identity()
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/1",
        process_identity=live, created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    code, _ = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_OK
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    recorded = store.get_dispatch_intent("intent-live").process_identity
    assert result["adoption"]["owned_identity"] == recorded
    store.close()


# ── Adversarial: stale-first plus live is refused ──────────────────────────

def _seed_stale_first_then_live(store: SQLiteRunStore, live: dict) -> None:
    """One non-verifying (recycled) intent ordered *first*, one live *second*."""
    recycled = dict(live)
    recycled["start_time"] = "0.0"  # a recycled PID: never the owned attempt
    _seed_intent(
        store, intent_id="intent-stale", scope="run-1/task-a/1",
        process_identity=recycled, created_at="2026-01-01T00:00:00+00:00",
    )
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/2",
        process_identity=live, created_at="2026-01-01T00:00:01+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1", "run-1/task-a/2"])


def test_stale_first_plus_live_is_refused_named(tmp_path, monkeypatch):
    """The ordering-first intent is non-verifying: the restart refuses by name.

    It is never *skipped* to adopt the later live intent -- the frontier order
    decides, and an unverifiable frontier is a named hold, not a silent
    promotion of the second attempt.
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_stale_first_then_live(store, _live_identity())

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_identity_recycled" in err
    # The live second intent was not adopted: no intents were advanced/removed.
    assert [i.intent_id for i in store.list_dispatch_intents(run_id="run-1")] == [
        "intent-stale", "intent-live",
    ]
    store.close()


# ── One adversarial test per named checkpoint hold ─────────────────────────

def test_restart_refuses_when_no_checkpoint_is_written(tmp_path, monkeypatch):
    """No written checkpoint -> ``checkpoint_not_written`` (named hold)."""
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/1",
        process_identity=_live_identity(), created_at="2026-01-01T00:00:00+00:00",
    )

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_not_written" in err
    store.close()


def test_restart_refuses_a_checkpoint_that_does_not_name_the_intent(tmp_path, monkeypatch):
    """The checkpoint names another state -> ``checkpoint_ordering_first_mismatch``."""
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/1",
        process_identity=_live_identity(), created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-b/1"])

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_ordering_first_mismatch" in err
    store.close()


def test_restart_refuses_an_intent_without_recorded_identity(tmp_path, monkeypatch):
    """No recorded owned identity -> ``checkpoint_no_recorded_identity``."""
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-noid", scope="run-1/task-a/1",
        process_identity=None, created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_no_recorded_identity" in err
    store.close()


def test_restart_refuses_a_stale_recorded_command(tmp_path, monkeypatch):
    """Same PID and creation instant, different argv -> ``..._stale_command``."""
    live = _live_identity()
    if not live.get("argv"):
        pytest.skip("this platform exposes no observable argv to falsify")
    forged = dict(live)
    forged["argv"] = ["/bin/not-the-recorded-command", "--forged"]

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-stale-cmd", scope="run-1/task-a/1",
        process_identity=forged, created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_identity_stale_command" in err
    store.close()


def test_restart_refuses_a_foreign_recorded_image(tmp_path, monkeypatch):
    """Same PID and instant, different exec image -> ``..._identity_foreign``."""
    forged = dict(_live_identity())
    forged["exec_path"] = "/bin/definitely-not-the-recorded-image"

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-foreign", scope="run-1/task-a/1",
        process_identity=forged, created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_identity_foreign" in err
    store.close()


def test_restart_refuses_a_no_longer_observable_process(tmp_path, monkeypatch):
    """The recorded PID is gone -> ``checkpoint_identity_not_alive``.

    The recorded process is a real one that has already exited and been reaped
    (``subprocess`` of ``true``), so the observation genuinely fails against the
    live OS -- an *unknown* in-flight state, reported by name.
    """
    proc = subprocess.Popen(["true"])
    dead_pid = proc.pid
    proc.wait()

    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-dead", scope="run-1/task-a/1",
        process_identity={
            "pid": dead_pid, "ppid": 1, "pgid": dead_pid,
            "start_time": "1234.567", "exec_path": "/usr/bin/true",
            "argv": ["true"],
        },
        created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    code, err = _resume(store, db, monkeypatch)
    assert code == nc.EXIT_REFUSED
    assert "checkpoint_identity_not_alive" in err
    store.close()


# ── Adversarial: never a replay without --resume ───────────────────────────

def test_drive_without_resume_refuses_rather_than_replaying(tmp_path):
    """A live in-flight attempt and no ``--resume`` -> named refusal, no replay.

    The drive must not dispatch over the in-flight scope: on the base that
    reached the store as a raw duplicate launch (exit 5); here it is a *named*
    refusal (exit 3) and no launch happens.
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_review_record(store, "run-1", "pass")
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/1",
        process_identity=_live_identity(), created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    import contextlib
    import io

    err = io.StringIO()
    worker = _NeverCalledWorker()
    with contextlib.redirect_stderr(err):
        code = nc.main(
            ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
            worker=worker,
            reviewer=_NeverCalledReviewer(),
            store=store,
        )
    assert code == nc.EXIT_REFUSED
    assert worker.calls == []
    assert "already carries a live in-flight owned attempt" in err.getvalue()
    assert [i.intent_id for i in store.list_dispatch_intents(run_id="run-1")] == [
        "intent-live"
    ]
    store.close()


# ── Adversarial: the run key is never caller-supplied ──────────────────────

def test_a_caller_named_run_cannot_adopt_another_runs_intent(tmp_path):
    """A caller-named run reads nothing another run's intents hold.

    The durable intent belongs to ``run-1``; a service *named* by the caller
    (``caller-named-run``) reads no checkpoint for that label and reports the
    named hold instead of adopting ``run-1``'s in-flight attempt.
    """
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    _seed_run(store, "run-1")
    _seed_intent(
        store, intent_id="intent-live", scope="run-1/task-a/1",
        process_identity=_live_identity(), created_at="2026-01-01T00:00:00+00:00",
    )
    _write_checkpoint(store, "run-1", ["run-1/task-a/1"])

    adoption = NativeProcessService(
        store, "caller-named-run"
    ).read_restart_adoption("caller-named-run")
    assert adoption.adopted is False
    assert adoption.hold == "checkpoint_not_written"
    # And the driven run is untouched by the misnamed read.
    assert store.get_checkpoint("run-1") is not None
    assert store.get_checkpoint("caller-named-run") is None
    store.close()
