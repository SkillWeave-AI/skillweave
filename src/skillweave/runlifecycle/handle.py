"""Async run lifecycle: start, inspect, and kill durable background runs.

Routes through ``RunApplicationService`` and the kernel SQLite store so that
start, completion, cancellation and resume share one SQLite/CAS authority.
The public API (``RunHandle``, ``start_async``, ``kill_run``, ``inspect_run``)
is preserved as a compatibility facade.
"""

from __future__ import annotations

import json
import os
import signal as _signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO

from .store import RunMetadata, RunStateStore, StalePidError, RunStateError

# ---------------------------------------------------------------------------
# RunHandle — the versioned, durable handle returned by ``start_async``.
# ---------------------------------------------------------------------------

@dataclass
class RunHandle:
    """Versioned handle for an async run.  Survives launcher exit.

    Attributes
    ----------
    run_id:
        Unique run identifier (uuid hex).
    pid:
        Process ID of the detached child.
    command:
        The command that was started.
    state:
        Current run state (running, succeeded, failed, killed, unknown).
    created_at:
        ISO-8601 timestamp of when the run was started.
    version:
        Monotonically increasing version counter.
    """
    run_id: str
    pid: int
    command: list[str]
    state: str = "running"
    created_at: str = ""
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunHandle":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


# ---------------------------------------------------------------------------
# InspectMode — snapshot or follow
# ---------------------------------------------------------------------------

class InspectMode:
    SNAPSHOT = "snapshot"
    FOLLOW = "follow"


# ---------------------------------------------------------------------------
# Detached wrapper script — writes completion through the kernel store
# ---------------------------------------------------------------------------

_WRAPPER_SRC = (
    "import subprocess, sys, json, os, signal\n"
    "run_id = sys.argv[1]\n"
    "db_path = sys.argv[2]\n"
    "cmd = sys.argv[3:]\n"
    "from skillweave.runtime.store import SQLiteRunStore\n"
    "from skillweave.runtime.journal import EventJournal\n"
    "from skillweave.runtime.errors import VersionConflictError\n"
    "store = SQLiteRunStore(db_path)\n"
    "journal = EventJournal(db_path)\n"
    "journal.append(run_id, event_type='started',\n"
    "    payload={'command': cmd})\n"
    "try:\n"
    "    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)\n"
    "    rc = proc.returncode\n"
    "    stdout_text = proc.stdout.decode('utf-8', errors='replace') if proc.stdout else ''\n"
    "    try:\n"
    "        current = store.get_run(run_id)\n"
    "        if current and current.state == 'preflight':\n"
    "            store.transition(\n"
    "                run_id, 'batch_selection',\n"
    "                expected_state=current.state,\n"
    "                expected_version=current.version,\n"
    "                reason='wrapper: advance',\n"
    "            )\n"
    "            current = store.get_run(run_id)\n"
    "        # Set lifecycle metadata BEFORE terminal transition (can't update terminal records).\n"
    "        lifecycle = dict(current.metadata.get('runlifecycle', {}))\n"
    "        lifecycle.update({'lifecycle_state': 'succeeded' if rc == 0 else 'failed', 'exit_code': rc})\n"
    "        store.update_metadata(run_id,\n"
    "            expected_version=current.version,\n"
    "            metadata={'runlifecycle': lifecycle})\n"
    "        current = store.get_run(run_id)\n"
    "        store.transition(\n"
    "            run_id, 'advance_or_stop',\n"
    "            expected_state=current.state,\n"
    "            expected_version=current.version,\n"
    "            reason='wrapper: process completed',\n"
    "        )\n"
    "    except VersionConflictError:\n"
    "        pass\n"
    "    journal.append(run_id, event_type='finished',\n"
    "        payload={'returncode': rc, 'stdout_preview': stdout_text[:1024]})\n"
    "except Exception as exc:\n"
    "    try:\n"
    "        current = store.get_run(run_id)\n"
    "        if current and current.state == 'preflight':\n"
    "            store.transition(\n"
    "                run_id, 'batch_selection',\n"
    "                expected_state=current.state,\n"
    "                expected_version=current.version,\n"
    "                reason='wrapper: advance',\n"
    "            )\n"
    "            current = store.get_run(run_id)\n"
    "        lifecycle = dict(current.metadata.get('runlifecycle', {}))\n"
    "        lifecycle.update({'lifecycle_state': 'failed', 'exit_code': -1})\n"
    "        store.update_metadata(run_id,\n"
    "            expected_version=current.version,\n"
    "            metadata={'runlifecycle': lifecycle})\n"
    "        current = store.get_run(run_id)\n"
    "        store.transition(\n"
    "            run_id, 'advance_or_stop',\n"
    "            expected_state=current.state,\n"
    "            expected_version=current.version,\n"
    "            reason='wrapper: error: ' + str(exc),\n"
    "        )\n"
    "    except Exception:\n"
    "        pass\n"
    "    journal.append(run_id, event_type='error',\n"
    "        payload={'error': str(exc)})\n"
    "finally:\n"
    "    store.close()\n"
    "    journal.close()\n"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_async(
    command: list[str],
    *,
    project_root: Optional[Path] = None,
    run_id: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> RunHandle:
    """Start *command* as a detached background process.

    Routes through ``RunApplicationService.start_async`` so the run is
    registered in the kernel SQLite store before launch, and the detached
    child writes completion through the same store.  The service owns
    native start, completion, cancellation, and reattachment/resume
    decisions.

    The child runs in its own session (process group) so it survives the
    launcher's exit.  A structured RunHandle is returned immediately.

    Parameters
    ----------
    command:
        The command and arguments to execute.
    project_root:
        Project root (default: CWD).  State is written relative to this.
    run_id:
        Explicit run ID (default: auto-generated uuid hex).
    cwd:
        Working directory for the child (default: project_root).

    Returns
    -------
    RunHandle
        The versioned handle for the newly started run.
    """
    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runsvc.service import RunApplicationService

    root = Path(project_root or Path.cwd()).resolve()
    child_cwd = str(cwd or root)

    db_path = str(root / ".skillweave" / "store.db")
    (root / ".skillweave").mkdir(parents=True, exist_ok=True)

    kernel_store = SQLiteRunStore(db_path)
    journal = EventJournal(kernel_store)
    raw_artifacts = RawArtifactStore()
    service = RunApplicationService(kernel_store, journal, raw_artifacts)

    # Build env with PYTHONPATH for the child.
    src_path = str(Path(__file__).resolve().parent.parent.parent)
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_path}:{existing}" if existing else src_path

    result = service.start_async(
        command,
        run_id=run_id,
        cwd=Path(child_cwd),
        project_root=root,
        _env=env,
    )

    return RunHandle(
        run_id=result["run_id"],
        pid=result["pid"],
        command=result["command"],
        state="running",
        created_at=result["created_at"],
        version=result["version"],
    )


def inspect_run(
    run_id: str,
    *,
    project_root: Optional[Path] = None,
    mode: str = InspectMode.SNAPSHOT,
    poll_interval: float = 0.25,
    timeout: Optional[float] = None,
    out: Optional[TextIO] = None,
) -> int:
    """Inspect a run's state and events.

    Parameters
    ----------
    run_id:
        The run ID to inspect.
    project_root:
        Project root (default: CWD).
    mode:
        ``"snapshot"`` dumps current state and exits.  ``"follow"`` tails
        events until the run finishes or *timeout* expires.
    poll_interval:
        Seconds between poll cycles in follow mode.
    timeout:
        Maximum seconds to wait in follow mode.
    out:
        Output stream (default: sys.stdout).

    Returns
    -------
    0 on success, 1 if the run is not found.
    """
    if out is None:
        out = sys.stdout

    store = RunStateStore(project_root)
    meta = store.read_metadata(run_id)
    if meta is None:
        out.write(json.dumps({"error": f"run not found: {run_id}"}) + "\n")
        return 1

    # Snapshot: emit current state
    snapshot = {
        "run_id": meta.run_id,
        "pid": meta.pid,
        "command": meta.command,
        "state": meta.state,
        "exit_code": meta.exit_code,
        "created_at": meta.created_at,
        "finished_at": meta.finished_at,
        "version": meta.version,
    }

    events = store.read_events(run_id)
    lane_states: dict[str, Any] = {}
    for ev in events:
        lane_id = ev.get("lane_id") or ev.get("run_id", run_id)
        if ev.get("event") in ("finished", "error"):
            lane_states[lane_id] = "terminal"
        elif ev.get("event") == "started":
            lane_states[lane_id] = "running"

    snapshot["lane_states"] = lane_states
    snapshot["event_count"] = len(events)

    if mode == InspectMode.SNAPSHOT:
        out.write(json.dumps(snapshot, sort_keys=True) + "\n")
        return 0

    # Follow mode: stream events until terminal
    last_seq = 0
    start_time = time.monotonic()

    # Dump snapshot first
    out.write(json.dumps(snapshot, sort_keys=True) + "\n")
    out.flush()

    while True:
        new_events = store.read_events(run_id, after=last_seq)
        for ev in new_events:
            out.write(json.dumps(ev, sort_keys=True) + "\n")
            out.flush()
            seq = ev.get("sequence", 0)
            if seq > last_seq:
                last_seq = seq
            if ev.get("event") in ("finished", "error"):
                return 0

        if timeout is not None and (time.monotonic() - start_time) >= timeout:
            out.write(json.dumps({"event": "timeout", "run_id": run_id}) + "\n")
            return 1

        # Re-read metadata to detect terminal state written by the wrapper
        current_meta = store.read_metadata(run_id)
        if current_meta and current_meta.state != "running":
            # Drain remaining events
            remaining = store.read_events(run_id, after=last_seq)
            for ev in remaining:
                out.write(json.dumps(ev, sort_keys=True) + "\n")
                out.flush()
            return 0

        time.sleep(poll_interval)


def kill_run(
    run_id: str,
    pid: int,
    *,
    project_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Kill a run by its ID and recorded PID.

    Uses the kernel-backed ``RunStateStore`` for CAS-guarded terminal
    transitions.  Graceful (SIGTERM) first, then forced (SIGKILL) after
    a short grace period.  Verifies the *pid* matches the recorded PID
    to prevent killing a stale/reused process.

    Parameters
    ----------
    run_id:
        The run ID to kill.
    pid:
        The PID the caller believes is the target.  Must match the recorded
        PID.
    project_root:
        Project root (default: CWD).

    Returns
    -------
    dict with ``run_id``, ``pid``, ``state``, ``result``.

    Raises
    ------
    StalePidError
        When *pid* does not match the recorded PID.
    RunStateError
        When the run is not found or already terminal.
    """
    store = RunStateStore(project_root)

    # Verify the PID matches the recorded one
    store.verify_pid(run_id, pid)

    meta = store.read_metadata(run_id)
    if meta is None:
        raise RunStateError(f"no run found: {run_id}")

    if meta.state != "running":
        return {
            "run_id": run_id,
            "pid": pid,
            "state": meta.state,
            "result": "already_terminal",
        }

    # Graceful kill (SIGTERM) to the process group
    killed_graceful = False
    try:
        os.killpg(pid, _signal.SIGTERM)
        killed_graceful = True
        # Give it a short grace period
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                break
    except (ProcessLookupError, PermissionError):
        pass

    # Force kill (SIGKILL) to the process group.
    # Note: macOS os.kill(pid, 0) can raise PermissionError for zombies or
    # cross-session processes, so we skip the existence check and attempt
    # SIGKILL directly.
    try:
        os.killpg(pid, _signal.SIGKILL)
        result = "forced_kill"
    except ProcessLookupError:
        result = "graceful_kill" if killed_graceful else "already_dead"
    except PermissionError:
        # macOS: os.killpg may also fail with EPERM for zombies.  Fall back
        # to killing just the tracked PID.
        try:
            os.kill(pid, _signal.SIGKILL)
            result = "forced_kill"
        except (ProcessLookupError, PermissionError):
            result = "graceful_kill" if killed_graceful else "already_dead"

    # Use the kernel-backed store for CAS-guarded terminal transition.
    # Drive the kernel lane state to terminal so the wrapper cannot
    # resurrect the run after cancellation.
    store.update_metadata(run_id, expected_version=meta.version, state="killed", finished_at=_now())

    store.append_event(run_id, {
        "event": "killed",
        "run_id": run_id,
        "pid": pid,
        "result": result,
    })

    return {
        "run_id": run_id,
        "pid": pid,
        "state": "killed",
        "result": result,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
