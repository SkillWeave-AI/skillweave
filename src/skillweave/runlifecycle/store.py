"""Structured run state persistence backed by the kernel SQLite store.

Each run's metadata and events are stored in the kernel ``.skillweave/store.db``
using ``SQLiteRunStore`` and ``EventJournal``, so that start, completion,
cancellation and resume share one SQLite/CAS authority.  The old file-based
vocabulary (``RunMetadata``, ``run.json``, ``events.jsonl``) is preserved as
a public compatibility facade; the backing is now the kernel store.

A racing write is detected by SQLite's version-based CAS, not by file rename
atomicity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUNS_DIR = Path(".skillweave") / "runs"
_STORE_DB = ".skillweave" / Path("store.db")

# Safe characters for run IDs (uuid hex + common separators).
_RUN_ID_RE = re.compile(r"^[a-f0-9\-_]+$")

# Mapping from lifecycle states to lane states for kernel storage.
_LIFECYCLE_TO_LANE = {
    "running": "preflight",
    "succeeded": "advance_or_stop",
    "failed": "failed",
    "killed": "advance_or_stop",
    "unknown": "unknown",
}

_LANE_TO_LIFECYCLE = {v: k for k, v in _LIFECYCLE_TO_LANE.items()}
# Multiple lifecycle states can map to the same lane state; reverse only the
# first occurrence.  For reads we keep the raw lifecycle_state in metadata.
_LANE_TO_LIFECYCLE["preflight"] = "running"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RunStateError(RuntimeError):
    """Raised when a run state operation cannot complete."""


class StalePidError(RunStateError):
    """The target PID does not match the recorded PID; kill refused."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RunMetadata:
    """Persistent metadata for one async run.

    Written atomically via the kernel SQLite store with version-based CAS.
    """
    run_id: str
    command: list[str]
    pid: int
    state: str = "running"          # running | succeeded | failed | killed | unknown
    exit_code: Optional[int] = None
    created_at: str = ""
    finished_at: str = ""
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, run_id: str, command: list[str], pid: int) -> "RunMetadata":
        return cls(
            run_id=run_id,
            command=list(command),
            pid=pid,
            created_at=_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunMetadata":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


# ---------------------------------------------------------------------------
# Store (backed by kernel SQLiteRunStore + EventJournal)
# ---------------------------------------------------------------------------

class RunStateStore:
    """Read/write run state via the kernel SQLite store.

    Parameters
    ----------
    project_root:
        Root of the project.  Defaults to CWD.
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self._root = Path(project_root or Path.cwd()).resolve()
        self._runs_dir = self._root / _RUNS_DIR
        self._db_path = str(self._root / _STORE_DB)
        # Ensure the parent directory of the store db exists.
        (self._root / ".skillweave").mkdir(parents=True, exist_ok=True)
        # Lazy-import the kernel store to avoid circular imports.
        from skillweave.runtime.store import SQLiteRunStore, RunRecord
        from skillweave.runtime.journal import EventJournal
        self._kernel = SQLiteRunStore(self._db_path)
        self._journal = EventJournal(self._kernel)
        self._RunRecord = RunRecord

    def _meta_to_record(self, meta: RunMetadata) -> Any:
        """Convert a ``RunMetadata`` to a kernel ``RunRecord``."""
        now = _now()
        lane_state = _LIFECYCLE_TO_LANE.get(meta.state, "unknown")
        merged = dict(meta.metadata or {})
        merged["runlifecycle"] = {
            "command": meta.command,
            "pid": meta.pid,
            "lifecycle_state": meta.state,
            "exit_code": meta.exit_code,
            "finished_at": meta.finished_at,
        }
        return self._RunRecord(
            run_id=meta.run_id,
            root_run_id=meta.run_id,
            parent_run_id=None,
            state=lane_state,
            version=meta.version,
            created_at=meta.created_at or now,
            updated_at=now,
            ended_at=meta.finished_at or None,
            role="ops",
            metadata=merged,
        )

    def _record_to_meta(self, record) -> Optional[RunMetadata]:
        """Convert a kernel ``RunRecord`` back to a ``RunMetadata``."""
        if record is None:
            return None
        lifecycle = record.metadata.get("runlifecycle", {})
        return RunMetadata(
            run_id=record.run_id,
            command=lifecycle.get("command", []),
            pid=lifecycle.get("pid", 0),
            state=lifecycle.get("lifecycle_state",
                                _LANE_TO_LIFECYCLE.get(record.state, "unknown")),
            exit_code=lifecycle.get("exit_code"),
            created_at=record.created_at,
            finished_at=lifecycle.get("finished_at") or record.ended_at or "",
            version=record.version,
            metadata={k: v for k, v in record.metadata.items() if k != "runlifecycle"},
        )

    # -- paths (kept for backward compat, point to old JSON dirs) -----------

    def run_dir(self, run_id: str) -> Path:
        return self._runs_dir / run_id

    def metadata_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    # -- metadata -----------------------------------------------------------

    def write_metadata(self, meta: RunMetadata) -> None:
        """Write *meta* to the kernel SQLite store."""
        record = self._meta_to_record(meta)
        self._kernel.save_run(record)

    def read_metadata(self, run_id: str) -> Optional[RunMetadata]:
        """Return metadata for *run_id*, or ``None`` if absent."""
        record = self._kernel.get_run(run_id)
        if record is None:
            return None
        return self._record_to_meta(record)

    def update_metadata(self, run_id: str, **changes: Any) -> RunMetadata:
        """Update metadata with atomic CAS via the kernel store.

        Raises
        ------
        VersionConflictError
            When the persisted version has changed since the caller's snapshot.
        RunStateError
            When the run does not exist.
        """
        from skillweave.runtime.errors import VersionConflictError
        record = self._kernel.get_run(run_id)
        if record is None:
            raise RunStateError(f"no metadata for run {run_id}")

        expected_version = record.version

        # Extract recognised top-level lifecycle fields.
        state = changes.pop("state", None)
        exit_code = changes.pop("exit_code", None)
        finished_at = changes.pop("finished_at", None)

        # Build the metadata update payload.
        meta_update: dict[str, Any] = {}
        if state is not None:
            meta_update["lifecycle_state"] = state
        if exit_code is not None:
            meta_update["exit_code"] = exit_code
        if finished_at is not None:
            meta_update["finished_at"] = finished_at
        # Pass through any remaining changes as metadata.
        for k, v in changes.items():
            meta_update[k] = v

        if meta_update:
            self._kernel.update_metadata(
                run_id,
                expected_version=expected_version,
                metadata={"runlifecycle": {**record.metadata.get("runlifecycle", {}), **meta_update}},
            )

        return self.read_metadata(run_id)

    # -- events -------------------------------------------------------------

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        """Append one event to the kernel journal."""
        self._journal.append(
            run_id,
            event_type=event.get("event", "lifecycle"),
            payload={k: v for k, v in event.items() if k != "event"},
            idempotency_key=event.get("idempotency_key"),
        )

    def read_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        """Return events with sequence > *after*, in order."""
        kernel_events = self._journal.get_events(run_id, from_sequence=after)
        result = []
        for ke in kernel_events:
            ev = dict(ke.payload)
            ev["event"] = ke.event_type
            ev["sequence"] = ke.sequence
            ev["run_id"] = ke.run_id
            result.append(ev)
        return result

    def latest_event_sequence(self, run_id: str) -> int:
        """Return the highest sequence number in the journal, or 0."""
        return self._journal.get_last_sequence(run_id)

    # -- pid staleness check ------------------------------------------------

    def verify_pid(self, run_id: str, pid: int) -> None:
        """Raise :class:`StalePidError` when *pid* does not match the recorded one.

        This prevents killing a stale/reused PID that happens to have the same
        numeric value as a previous run's PID.  The caller must pass the PID
        they *believe* is the target; the store compares it against the PID
        recorded at start time.
        """
        meta = self.read_metadata(run_id)
        if meta is None:
            raise RunStateError(f"no run found for {run_id}")
        if meta.pid != pid:
            raise StalePidError(
                f"pid mismatch for run {run_id}: recorded pid={meta.pid}, "
                f"provided pid={pid}.  Refusing kill on stale/reused PID."
            )

    # -- listing ------------------------------------------------------------

    def list_runs(self) -> list[str]:
        """Return run IDs of all known runs, newest first."""
        # Read from kernel store.
        records = self._kernel.list_runs(limit=1000)
        return [r.run_id for r in records]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
