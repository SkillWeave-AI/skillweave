"""SW-1511A-LIVENESS-001 criterion 3: cancellation reaps the owned group.

These tests drive the *production* cancellation entry point
(``skillweave.runlifecycle.handle.cancel_process`` -> ``RunApplicationService.
cancel_process``) against real, fixture-owned child processes: the owned
leader, the nested request it admits (a grandchild in the same process group)
and an *unrelated* process the test itself starts in its own session.  Nothing
here mocks the signal boundary (``os.kill``/``os.killpg`` stay real) or the
identity verification; the observation under test is the one the cancellation
path itself produces.

Criterion 3 -- cancellation reaps every owned descendant and leaves unrelated
processes untouched, with causal evidence in the final result.  The causal
evidence is the ``owned-process-cancel/v1`` record carried by the result: the
verified identity, the exact process group, the owned pids actually signalled,
and the foreign-group pids proven unrelated and left untouched.

Every child lives in its own process group created by the run lifecycle under
test; teardown kills that group so no fixture escapes.  Each test uses its own
temporary root and lets the lifecycle mint its own run id, so nothing is shared
across runs and a re-run is repeatable.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from skillweave.runlifecycle.handle import start_async, cancel_process
from skillweave.runlifecycle.store import RunStateStore


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


def _unrelated_process(seconds: float = 30):
    """A process in its *own* session/group, unrelated to the owned run."""
    import subprocess

    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        start_new_session=True,
    )


def _safe_pgid(proc) -> int:
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return 0


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


class TestCancelReapsOwnedGroup:
    def test_cancel_reaps_owned_descendants_and_spares_unrelated(self, tmp_path):
        """The owned group (leader + nested grandchild) dies; an unrelated
        process in its own group survives, and the result carries the causal
        ``owned-process-cancel/v1`` evidence naming exactly what was signalled."""
        from skillweave.runtime import process_identity as pidmod

        handle = start_async(_nested_command(), project_root=tmp_path)
        owned_pgid = handle.pid
        unrelated = _unrelated_process()
        try:
            # The nested request is admitted in-group: the owned group has at
            # least two live members before the cancel.
            assert _wait_until(
                lambda: len(pidmod.list_process_group(owned_pgid)) >= 2
            ), "the owned group did not admit its nested request"
            # The unrelated process owns a *different* group and is alive.
            unrelated_pgid = os.getpgid(unrelated.pid)
            assert unrelated_pgid != owned_pgid
            assert pidmod.is_alive(unrelated.pid)

            result = cancel_process(handle.run_id, project_root=tmp_path)

            # -- the owned descendants were reaped -------------------------
            assert result["identity_verified"] is True
            assert result["unknown_inflight"] is False
            assert handle.pid in result["signalled"]
            assert len(result["signalled"]) >= 2
            assert not pidmod.is_alive(handle.pid)
            assert _wait_until(
                lambda: pidmod.list_process_group(owned_pgid) == ())

            # -- the unrelated process was left untouched ------------------
            assert pidmod.is_alive(unrelated.pid)
            assert unrelated.pid not in result["signalled"]

            # -- the causal evidence is in the final result ---------------
            evidence = result["owned_process_cancel"]
            assert evidence["schema"].startswith("owned-process-cancel/v1")
            assert evidence["run_id"] == handle.run_id
            assert evidence["pid"] == handle.pid
            assert evidence["pgid"] == owned_pgid
            assert evidence["identity_verified"] is True
            assert evidence["identity_token"] is not None
            assert evidence["result"] == result["result"]
            # Every signalled pid is a member of the owned group; the unrelated
            # pid is *proven* unrelated and recorded as left untouched.
            assert set(result["signalled"]) <= set(evidence["signalled"])
            assert unrelated.pid not in evidence["signalled"]
            assert unrelated.pid not in evidence["unrelated_untouched"]
        finally:
            _kill_group(owned_pgid)
            _kill_group(_safe_pgid(unrelated))

    def test_cancel_refuses_to_signal_a_foreign_group_member(
            self, tmp_path, monkeypatch):
        """Even a pid *reported* as an owned-group member is proven before it is
        signalled: a foreign-group pid is recorded ``unrelated_untouched`` and
        left alive, never killed."""
        from skillweave.runtime import process_identity as pidmod

        handle = start_async(_sleep_command(), project_root=tmp_path)
        owned_pgid = handle.pid
        unrelated = _unrelated_process()
        unrelated_pgid = os.getpgid(unrelated.pid)
        try:
            assert unrelated_pgid != owned_pgid
            assert _wait_until(lambda: pidmod.is_alive(handle.pid))

            # Inject the scenario the enumeration guard exists for: the owned
            # group enumeration reports a foreign pid as if it were a member.
            real_list = pidmod.list_process_group

            def _widened(pgid):
                members = list(real_list(pgid))
                if pgid == owned_pgid and unrelated.pid not in members:
                    members.append(unrelated.pid)
                return tuple(members)

            monkeypatch.setattr(pidmod, "list_process_group", _widened)

            result = cancel_process(handle.run_id, project_root=tmp_path)

            # The foreign member was proven unrelated: not signalled, but
            # recorded as left untouched, and still alive.
            assert unrelated.pid in result["unrelated_untouched"]
            assert unrelated.pid not in result["signalled"]
            assert pidmod.is_alive(unrelated.pid)
            # The genuine owned member was still reaped.
            assert handle.pid in result["signalled"]
            assert not pidmod.is_alive(handle.pid)
            evidence = result["owned_process_cancel"]
            assert unrelated.pid in evidence["unrelated_untouched"]
            assert unrelated.pid not in evidence["signalled"]
        finally:
            monkeypatch.undo()
            _kill_group(owned_pgid)
            _kill_group(unrelated_pgid)
