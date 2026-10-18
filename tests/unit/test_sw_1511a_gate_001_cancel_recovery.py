"""SW-1511A-GATE-001 repair-b: cancel identity-hint refusal + terminal result.

Defects 5b-i and 5b-ii were found by the live qualification lane and are
repaired inside 1.5.11.  These tests are the *adversarial* proof on the real
production entry points:

* the CLI router ``skillweave.cli.main.main`` (``cancel`` subcommand ->
  ``runlifecycle.cli.main_cancel`` -> ``RunApplicationService.cancel_process``),
  which is the surface that owns the ``--pid`` hint; and
* the MCP tool handler ``skillweave.mcp.server.cancel_run``.

Every test starts a real, fixture-owned child process through the production
``start_async`` (nothing mocks the signal boundary or the identity
verification) under its own per-test temporary root, and reaps the owned
process group on teardown.

5b-i -- a ``--pid`` that differs from the recorded process identity is a
recycled-PID *refusal* (exit 3, like the other identity refusals) and the
process is never signalled.  The case where no identity was recorded is kept:
the refusal is then the *no-record* refusal, still exit 3.

5b-ii -- a repeated cancel of an already-terminal run reports a definite
``already_terminal`` result consistent with the durable lifecycle
(``state``/``lifecycle_state`` = ``killed``, ``unknown_inflight`` false) and
the documented "not running" exit code 3, *distinct* from a never-started
cancel (which refuses with no identity recorded).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from skillweave.runlifecycle.handle import start_async


def _sleep_command(seconds: float = 60) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _recorded_identity_pid(tmp_path: Path, run_id: str) -> int:
    from skillweave.runtime.store import SQLiteRunStore

    store = SQLiteRunStore(str(tmp_path / ".skillweave" / "store.db"))
    try:
        record = store.get_run(run_id)
        if record is None:
            return 0
        identity = (record.metadata or {}).get("process_identity") or {}
        return int(identity.get("pid") or 0)
    finally:
        store.close()


def _is_alive(pid: int) -> bool:
    from skillweave.runtime import process_identity as pidmod

    return pidmod.is_alive(pid)


def _durable_state(tmp_path: Path, run_id: str):
    from skillweave.runtime.store import SQLiteRunStore

    store = SQLiteRunStore(str(tmp_path / ".skillweave" / "store.db"))
    try:
        return store.get_run(run_id)
    finally:
        store.close()


def _start_live(tmp_path: Path):
    """Start a real owned child and wait until its identity is recorded."""
    handle = start_async(_sleep_command(), project_root=tmp_path)
    assert _wait_until(
        lambda: _recorded_identity_pid(tmp_path, handle.run_id) == handle.pid
    ), "the launched process identity was not recorded"
    return handle


# ---------------------------------------------------------------------------
# 5b-i: a mismatched --pid is a recycled-PID refusal; nothing is signalled
# ---------------------------------------------------------------------------

class TestCancelPidMismatchRefused:
    def test_cli_cancel_rejects_a_mismatched_pid_and_signals_nothing(
            self, tmp_path, monkeypatch, capsys):
        """``cancel <run> --pid <wrong>`` exits 3 and the owned process stays
        alive -- the recorded identity is never overridden by the hint."""
        from skillweave.cli.main import main as router_main

        handle = _start_live(tmp_path)
        wrong = handle.pid + 1
        assert wrong != handle.pid

        # Spy the signal boundary: a refusal must not reach it at all.
        import skillweave.runsvc.service as service_mod

        signalled: list[int] = []
        real_kill = os.kill

        def spy_kill(pid, sig):
            signalled.append(pid)
            return real_kill(pid, sig)

        monkeypatch.setattr(service_mod.os, "kill", spy_kill)
        monkeypatch.chdir(tmp_path)
        try:
            rc = router_main(["cancel", handle.run_id, "--pid", str(wrong)])

            assert rc == 3, "a mismatched --pid must be a refusal (exit 3)"
            # Nothing was signalled and the owned process is still alive.
            assert signalled == []
            assert _is_alive(handle.pid)
            # The run was not fabricated terminal by the refused cancel.
            state = _durable_state(tmp_path, handle.run_id).state
            assert state != "killed"
        finally:
            _kill_group(handle.pid)

    def test_cli_cancel_mismatch_diagnostic_names_the_refusal(
            self, tmp_path, monkeypatch, capsys):
        from skillweave.cli.main import main as router_main

        handle = _start_live(tmp_path)
        monkeypatch.chdir(tmp_path)
        try:
            rc = router_main(["cancel", handle.run_id, "--pid", str(handle.pid + 7)])
            err = capsys.readouterr().err
            assert rc == 3
            assert "recycled_pid" in err
            assert _is_alive(handle.pid)
        finally:
            _kill_group(handle.pid)

    def test_cli_cancel_matching_pid_still_cancels(self, tmp_path, monkeypatch, capsys):
        """The refusal is *only* on a mismatch: the recorded PID still cancels."""
        from skillweave.cli.main import main as router_main

        handle = _start_live(tmp_path)
        monkeypatch.chdir(tmp_path)
        try:
            rc = router_main(["cancel", handle.run_id, "--pid", str(handle.pid)])
            assert rc == 0
            payload = json.loads(capsys.readouterr().out)
            assert payload["result"] == "graceful_kill"
            assert payload["identity_verified"] is True
            assert handle.pid in payload["signalled"]
        finally:
            _kill_group(handle.pid)

    def test_cli_cancel_without_recorded_identity_still_refuses(
            self, tmp_path, monkeypatch, capsys):
        """The no-identity case is unchanged: a never-started run refuses (3),
        whether or not a --pid hint is supplied."""
        from skillweave.cli.main import main as router_main
        from skillweave.runtime.store import SQLiteRunStore

        (tmp_path / ".skillweave").mkdir(parents=True, exist_ok=True)
        store = SQLiteRunStore(str(tmp_path / ".skillweave" / "store.db"))
        store.create_run("never-started-001")
        store.close()

        monkeypatch.chdir(tmp_path)
        rc_no_hint = router_main(["cancel", "never-started-001"])
        err_no_hint = capsys.readouterr().err
        assert rc_no_hint == 3
        assert "no_recorded_identity" in err_no_hint

        rc_with_hint = router_main(["cancel", "never-started-001", "--pid", "999999"])
        err_with_hint = capsys.readouterr().err
        assert rc_with_hint == 3
        assert "no_recorded_identity" in err_with_hint

    def test_mcp_cancel_of_a_never_started_run_is_a_refusal(self, tmp_path):
        from skillweave.mcp.server import cancel_run
        from skillweave.runtime.store import SQLiteRunStore

        (tmp_path / ".skillweave").mkdir(parents=True, exist_ok=True)
        db = str(tmp_path / ".skillweave" / "store.db")
        store = SQLiteRunStore(db)
        store.create_run("never-started-002")
        store.close()

        payload = json.loads(cancel_run("never-started-002", db_path=db))
        assert payload.get("code") == "identity_unverified"
        assert payload.get("reason") == "no_recorded_identity"


# ---------------------------------------------------------------------------
# 5b-ii: a repeat cancel reports already_terminal, distinct from never-started
# ---------------------------------------------------------------------------

class TestRepeatCancelReportsAlreadyTerminal:
    def test_cli_repeat_cancel_reports_already_terminal(
            self, tmp_path, monkeypatch, capsys):
        from skillweave.cli.main import main as router_main

        handle = _start_live(tmp_path)
        monkeypatch.chdir(tmp_path)
        try:
            first = router_main(["cancel", handle.run_id])
            assert first == 0
            capsys.readouterr()

            rc = router_main(["cancel", handle.run_id])
            err = capsys.readouterr().err
            assert rc == 3, "an already-terminal cancel must not exit 0"

            # The diagnostic and the durable lifecycle agree.
            record = _durable_state(tmp_path, handle.run_id)
            lifecycle = (record.metadata or {}).get("runlifecycle", {})
            assert lifecycle.get("lifecycle_state") == "killed"
            assert "already terminal" in err
            assert "killed" in err
        finally:
            _kill_group(handle.pid)

    def test_service_repeat_cancel_is_definite_not_unknown(self, tmp_path):
        """The already-terminal result is *definite*: ``unknown_inflight`` is
        false and the state carries the durable lifecycle, never ``unknown``."""
        from skillweave.runlifecycle.handle import cancel_process

        handle = _start_live(tmp_path)
        try:
            cancel_process(handle.run_id, project_root=tmp_path)
            result = cancel_process(handle.run_id, project_root=tmp_path)

            assert result["result"] == "already_terminal"
            assert result["unknown_inflight"] is False
            assert result["state"] == "killed"
            assert result["lifecycle_state"] == "killed"
            assert result["lane_state"] == "advance_or_stop"
            assert result["signalled"] == []
        finally:
            _kill_group(handle.pid)

    def test_already_terminal_is_distinct_from_never_started(
            self, tmp_path, monkeypatch):
        """A terminal run and a never-started run are told apart: the former
        yields an ``already_terminal`` result (exit 3, state reported); the
        latter refuses for a missing identity (exit 3, a typed refusal)."""
        from skillweave.cli.main import main as router_main
        from skillweave.runtime.store import SQLiteRunStore
        import skillweave.runlifecycle.cli as cli_mod

        handle = _start_live(tmp_path)
        (tmp_path / ".skillweave").mkdir(parents=True, exist_ok=True)
        store = SQLiteRunStore(str(tmp_path / ".skillweave" / "store.db"))
        store.create_run("never-started-003")
        store.close()

        real_cancel = cli_mod.cancel_process

        monkeypatch.chdir(tmp_path)
        try:
            first = router_main(["cancel", handle.run_id])
            assert first == 0

            # Terminal repeat: the service returns the definite result, never
            # the never-started refusal (the two paths are told apart).
            terminal = real_cancel(handle.run_id)
            assert terminal["result"] == "already_terminal"
            assert terminal["unknown_inflight"] is False

            # Never-started: a typed refusal, never an already_terminal result.
            from skillweave.runtime.errors import ProcessIdentityError

            with pytest.raises(ProcessIdentityError):
                real_cancel("never-started-003")
        finally:
            _kill_group(handle.pid)

    def test_mcp_repeat_cancel_reports_already_terminal(self, tmp_path):
        from skillweave.mcp.server import cancel_run

        handle = _start_live(tmp_path)
        db = str(tmp_path / ".skillweave" / "store.db")
        try:
            first = json.loads(cancel_run(handle.run_id, db_path=db))
            assert first["status"] == "cancel_requested"
            assert first["identity_verified"] is True

            second = json.loads(cancel_run(handle.run_id, db_path=db))
            assert second["result"] == "already_terminal"
            assert second["unknown_inflight"] is False
            assert second["state"] == "killed"
            assert second["lifecycle_state"] == "killed"
        finally:
            _kill_group(handle.pid)
