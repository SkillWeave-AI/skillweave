"""SW-1510-CANCEL: native cancel + resume process-lifecycle semantics.

This module proves the three original SW-1510-CANCEL criteria against real,
fixture-owned child processes (never a mocked signal boundary):

1. Cancellation checks owned process identity, stops the process group and its
   admitted nested requests, and records an acknowledgement plus any unknown
   in-flight state.
2. A recycled PID, a stale command or a foreign worker cannot be killed
   (nothing is signalled on refusal).  Only test-owned process groups are ever
   signalled.
3. Resume creates a new identified attempt with CAS/idempotency checks; the MCP
   and CLI surfaces never claim restarted execution from a database transition
   alone.

Every child process here is created by the run lifecycle under test and lives in
its own process group; teardown kills that group so no fixture escapes.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from skillweave.runlifecycle.handle import (
    start_async,
    cancel_process,
    restart_attempt,
)
from skillweave.runlifecycle.store import RunStateStore, RunStateError
from skillweave.runtime.errors import ProcessIdentityError, StoreError


def _sleep_command(seconds: float = 30) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _nested_command(seconds: float = 30) -> list[str]:
    """A child that admits a nested request (a grandchild) in its own group."""
    return [
        sys.executable,
        "-c",
        (
            "import subprocess, time, sys;"
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})']);"
            "print('NESTED', p.pid, flush=True);"
            f"time.sleep({seconds})"
        ),
    ]


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


def _store(tmp_path) -> RunStateStore:
    return RunStateStore(tmp_path)


def _recorded_pid(tmp_path, run_id: str) -> int:
    meta = _store(tmp_path).read_metadata(run_id)
    return meta.pid if meta is not None else 0


def _tamper_identity(tmp_path, run_id: str, **changes) -> None:
    """Overwrite the recorded native identity on the run record (test-only)."""
    store = _store(tmp_path)
    record = store._kernel.get_run(run_id)
    assert record is not None
    identity = dict((record.metadata or {}).get("process_identity") or {})
    identity.update(changes)
    store._kernel.update_metadata(
        run_id,
        expected_version=record.version,
        metadata={"process_identity": identity},
    )


# ---------------------------------------------------------------------------
# Criterion 1: identity-checked cancellation, group + nested stop, ack, unknown
# ---------------------------------------------------------------------------

class TestCancelOwnedIdentity:
    def test_cancel_stops_owned_group_and_admitted_nested_request(self, tmp_path):
        handle = start_async(_nested_command(), project_root=tmp_path)
        pgid = handle.pid
        try:
            # Let the wrapper admit its nested request (grandchild) in-group.
            ident = _store(tmp_path)
            meta = ident.read_metadata(handle.run_id)
            assert meta is not None and meta.pid == handle.pid

            from skillweave.runtime import process_identity as pidmod

            assert _wait_until(lambda: len(pidmod.list_process_group(pgid)) >= 2)

            result = cancel_process(handle.run_id, project_root=tmp_path)

            assert result["identity_verified"] is True
            assert result["unknown_inflight"] is False
            assert result["state"] == "killed"
            assert handle.pid in result["signalled"]
            # The admitted nested request was signalled too, in the same group.
            assert len(result["signalled"]) >= 2
            assert not pidmod.is_alive(handle.pid)
            assert _wait_until(lambda: pidmod.list_process_group(pgid) == ())
        finally:
            _kill_group(pgid)

    def test_cancel_records_acknowledgement_event(self, tmp_path):
        handle = start_async(_sleep_command(), project_root=tmp_path)
        try:
            result = cancel_process(handle.run_id, project_root=tmp_path)
            events = _store(tmp_path).read_events(handle.run_id)
            acks = [e for e in events if e.get("event") == "cancellation_ack"]
            assert acks, "a cancellation acknowledgment event must be recorded"
            ack = acks[-1]
            assert ack["identity_verified"] is True
            assert ack["result"] == result["result"]
            assert ack["pid"] == handle.pid
        finally:
            _kill_group(handle.pid)

    def test_cancel_unobservable_process_reports_unknown_not_terminated(self, tmp_path):
        """A recorded process that can no longer be observed stays *unknown*."""
        handle = start_async(_sleep_command(), project_root=tmp_path)
        pgid = handle.pid
        try:
            # Point the recorded identity at a PID that is not observable, via
            # a controlled, known-dead PID (no foreign process is touched).
            import subprocess

            done = subprocess.Popen([sys.executable, "-c", "pass"])
            done.wait()
            _tamper_identity(tmp_path, handle.run_id, pid=done.pid)
            # The recorded numeric PID and the identity PID must agree.
            store = _store(tmp_path)
            record = store._kernel.get_run(handle.run_id)
            store._kernel.update_metadata(
                handle.run_id,
                expected_version=record.version,
                metadata={
                    "runlifecycle": {
                        **record.metadata.get("runlifecycle", {}),
                        "pid": done.pid,
                    }
                },
            )

            result = cancel_process(handle.run_id, project_root=tmp_path)

            assert result["unknown_inflight"] is True
            assert result["state"] == "unknown"
            assert result["identity_verified"] is False
            assert result["signalled"] == []
            # The owned child was never signalled by the unknown path.
            from skillweave.runtime import process_identity as pidmod

            assert pidmod.is_alive(handle.pid)
            # The run is not fabricated terminal on an unobservable process.
            assert _store(tmp_path).read_metadata(handle.run_id).state != "killed"
        finally:
            _kill_group(pgid)


# ---------------------------------------------------------------------------
# Criterion 2: recycled PID / stale command / foreign worker refused
# ---------------------------------------------------------------------------

class TestCancelRefusals:
    def _live_handle(self, tmp_path):
        handle = start_async(_sleep_command(), project_root=tmp_path)
        assert _wait_until(
            lambda: _recorded_pid(tmp_path, handle.run_id) == handle.pid
        )
        return handle

    def test_refuses_recycled_pid(self, tmp_path):
        handle = self._live_handle(tmp_path)
        try:
            _tamper_identity(tmp_path, handle.run_id, start_time="1.000000")
            with pytest.raises(ProcessIdentityError) as exc:
                cancel_process(handle.run_id, project_root=tmp_path)
            assert exc.value.reason == "recycled_pid"
        finally:
            _kill_group(handle.pid)

    def test_refuses_stale_command(self, tmp_path):
        handle = self._live_handle(tmp_path)
        try:
            _tamper_identity(
                tmp_path, handle.run_id, argv=["/bin/not-the-command"]
            )
            with pytest.raises(ProcessIdentityError) as exc:
                cancel_process(handle.run_id, project_root=tmp_path)
            assert exc.value.reason == "stale_command"
        finally:
            _kill_group(handle.pid)

    def test_refuses_foreign_worker(self, tmp_path):
        handle = self._live_handle(tmp_path)
        try:
            _tamper_identity(tmp_path, handle.run_id, exec_path="/bin/sh")
            with pytest.raises(ProcessIdentityError) as exc:
                cancel_process(handle.run_id, project_root=tmp_path)
            assert exc.value.reason == "foreign_worker"
        finally:
            _kill_group(handle.pid)

    def test_refusal_signals_nothing(self, tmp_path, monkeypatch):
        handle = self._live_handle(tmp_path)
        import skillweave.runsvc.service as service_mod

        signalled: list[int] = []
        real_kill = os.kill
        real_killpg = os.killpg

        def spy_kill(pid, sig):
            signalled.append(pid)
            return real_kill(pid, sig)

        def spy_killpg(pgid, sig):
            signalled.append(pgid)
            return real_killpg(pgid, sig)

        monkeypatch.setattr(service_mod.os, "kill", spy_kill)
        monkeypatch.setattr(service_mod.os, "killpg", spy_killpg)
        try:
            _tamper_identity(tmp_path, handle.run_id, exec_path="/bin/sh")
            with pytest.raises(ProcessIdentityError):
                cancel_process(handle.run_id, project_root=tmp_path)
            assert signalled == []
        finally:
            _kill_group(handle.pid)


# ---------------------------------------------------------------------------
# Criterion 3: resume launches a NEW identified attempt (CAS/idempotency)
# ---------------------------------------------------------------------------

class TestResumeLaunchesNewAttempt:
    def test_resume_creates_new_identified_attempt(self, tmp_path):
        source = start_async(_sleep_command(), project_root=tmp_path)
        new_pid = None
        try:
            result = restart_attempt(
                source.run_id,
                _sleep_command(),
                project_root=tmp_path,
                idempotency_key="resume-key-1",
            )
            assert result["restarted"] is True
            assert result["idempotent_replay"] is False
            assert result["run_id"] != source.run_id
            assert result["parent_run_id"] == source.run_id
            assert result["attempt"] == 2
            new_pid = result["pid"]
            assert new_pid != source.pid
            # A real, distinct process was launched and recorded.
            from skillweave.runtime import process_identity as pidmod

            assert pidmod.is_alive(new_pid)
            meta = _store(tmp_path).read_metadata(result["run_id"])
            assert meta.pid == new_pid
            assert (meta.metadata.get("lineage") or {}).get("parent_run_id") == source.run_id
            assert (meta.metadata.get("lineage") or {}).get("attempt") == 2
        finally:
            _kill_group(source.pid)
            if new_pid:
                _kill_group(new_pid)

    def test_resume_is_idempotent_on_key(self, tmp_path):
        source = start_async(_sleep_command(), project_root=tmp_path)
        first = None
        try:
            first = restart_attempt(
                source.run_id,
                _sleep_command(),
                project_root=tmp_path,
                idempotency_key="resume-key-2",
            )
            second = restart_attempt(
                source.run_id,
                _sleep_command(),
                project_root=tmp_path,
                idempotency_key="resume-key-2",
            )
            assert second["run_id"] == first["run_id"]
            assert second["idempotent_replay"] is True
            assert second["pid"] == first["pid"]
        finally:
            _kill_group(source.pid)
            if first:
                _kill_group(first["pid"])

    def test_resume_refuses_terminal_run(self, tmp_path):
        handle = start_async(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            project_root=tmp_path,
        )
        pgid = handle.pid
        try:
            store = _store(tmp_path)
            assert _wait_until(
                lambda: (store.read_metadata(handle.run_id) or None) is not None
                and store.read_metadata(handle.run_id).state in ("succeeded", "failed")
            )
            with pytest.raises(StoreError):
                restart_attempt(
                    handle.run_id, _sleep_command(), project_root=tmp_path
                )
        finally:
            _kill_group(pgid)


# ---------------------------------------------------------------------------
# Criterion 3: MCP surface never claims a restart from a transition alone
# ---------------------------------------------------------------------------

class TestMcpSurface:
    def _db(self, tmp_path) -> str:
        return str(tmp_path / ".skillweave" / "store.db")

    def test_mcp_resume_without_command_does_not_claim_restarted(self, tmp_path):
        from skillweave.mcp.server import resume_run

        handle = start_async(_sleep_command(), project_root=tmp_path)
        try:
            raw = resume_run(handle.run_id, db_path=self._db(tmp_path))
            payload = json.loads(raw)
            assert payload["status"] == "reconciled"
            assert payload["restarted"] is False
            assert payload["run_id"] == handle.run_id
        finally:
            _kill_group(handle.pid)

    def test_mcp_resume_with_command_launches_new_attempt(self, tmp_path):
        from skillweave.mcp.server import resume_run

        source = start_async(_sleep_command(), project_root=tmp_path)
        new_pid = None
        try:
            raw = resume_run(
                source.run_id,
                db_path=self._db(tmp_path),
                command=" ".join(_sleep_command()),
            )
            payload = json.loads(raw)
            assert payload["status"] == "restarted"
            assert payload["restart_kind"] == "new_identified_attempt"
            assert payload["restarted"] is True
            assert payload["run_id"] != source.run_id
            new_pid = payload["pid"]
            assert new_pid != source.pid
        finally:
            _kill_group(source.pid)
            if new_pid:
                _kill_group(new_pid)

    def test_mcp_cancel_reports_refusal_not_fabricated_kill(self, tmp_path):
        from skillweave.mcp.server import cancel_run

        handle = start_async(_sleep_command(), project_root=tmp_path)
        try:
            assert _wait_until(
                lambda: _recorded_pid(tmp_path, handle.run_id) == handle.pid
            )
            _tamper_identity(tmp_path, handle.run_id, exec_path="/bin/sh")
            payload = json.loads(
                cancel_run(handle.run_id, db_path=self._db(tmp_path))
            )
            assert "error" in payload
            assert payload["reason"] == "foreign_worker"
            # The run is not reported as killed by a refused cancellation.
            assert _store(tmp_path).read_metadata(handle.run_id).state != "killed"
        finally:
            _kill_group(handle.pid)


# ---------------------------------------------------------------------------
# CLI surface wiring (parser + exit codes), no foreign process touched
# ---------------------------------------------------------------------------

class TestCliSurface:
    def test_cli_cancel_parser_has_native_semantics(self):
        from skillweave.runlifecycle.cli import build_cancel_parser

        parser = build_cancel_parser()
        args = parser.parse_args(["run-1", "--pid", "123", "--reason", "halt"])
        assert args.run_id == "run-1"
        assert args.pid == 123
        assert args.reason == "halt"

    def test_cli_resume_parser_has_idempotency_key(self):
        from skillweave.runlifecycle.cli import build_resume_parser

        parser = build_resume_parser()
        args = parser.parse_args(["run-1", "--command", "echo hi", "--idempotency-key", "k1"])
        assert args.run_id == "run-1"
        assert args.command == "echo hi"
        assert args.idempotency_key == "k1"

    def test_cli_cancel_reports_refusal_exit_code(self, tmp_path, monkeypatch, capsys):
        import skillweave.runlifecycle.cli as cli_mod
        from skillweave.runtime.errors import ProcessIdentityError

        def _boom(run_id, pid):
            raise ProcessIdentityError(run_id, pid, "foreign_worker")

        monkeypatch.setattr(cli_mod, "cancel_process", _boom)
        rc = cli_mod.main_cancel(["run-x", "--pid", "1"])
        assert rc == 3
        assert "foreign_worker" in capsys.readouterr().err
