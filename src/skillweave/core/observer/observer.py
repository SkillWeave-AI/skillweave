import os
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import fcntl

@dataclass(frozen=True)
class Lease:
    id: str
    owner: str

@dataclass(frozen=True)
class ObserverState:
    lease: Optional[Lease]
    journal_offset: int

class ReadOnlyError(Exception):
    pass

class ReadOnlyObserver:
    """
    A persistent, read-only observer that supports Leases and Journal-Offsets.
    Mutation of state is technically denied (raises ReadOnlyError).

    Beyond journal offsets, the observer is the reconnect/replay read seam for
    a run's durable stream evidence (SW-1510-STREAM): it returns ordered,
    cursor-based progress and never mutates the store it reads. A truncated
    preview it may return is always marked as a preview — it never replaces the
    raw durable evidence.
    """

    def __init__(self, storage_path: str):
        self._storage_path = storage_path
        self._state = self._load_state()

    def _load_state(self) -> ObserverState:
        if not os.path.exists(self._storage_path):
            return ObserverState(lease=None, journal_offset=0)
            
        with open(self._storage_path, 'r') as f:
            try:
                data = json.load(f)
                lease_data = data.get('lease')
                lease = Lease(**lease_data) if lease_data else None
                return ObserverState(
                    lease=lease,
                    journal_offset=data.get('journal_offset', 0)
                )
            except json.JSONDecodeError:
                return ObserverState(lease=None, journal_offset=0)

    @property
    def state(self) -> ObserverState:
        return self._state

    @property
    def current_offset(self) -> int:
        return self._state.journal_offset

    @property
    def current_lease(self) -> Optional[Lease]:
        return self._state.lease

    def mutate_state(self, *args, **kwargs):
        raise ReadOnlyError("Mutation of state is technically denied in read-only observer mode.")

    def update_offset(self, offset: int):
        raise ReadOnlyError("Cannot mutate offset directly on ReadOnlyObserver.")

    # -- durable stream reconnect/replay (SW-1510-STREAM) -------------------

    def read_progress(
        self,
        run_id: str,
        stream_store: Any,
        *,
        after_cursor: Optional[int] = None,
        limit: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read ordered, cursor-based durable stream progress for ``run_id``.

        This is the reconnect seam: a consumer that stopped at cursor N resumes
        exactly there and receives the frames it has not seen, in order, with no
        gap and no duplication. When ``after_cursor`` is omitted the observer
        resumes from its own persisted ``journal_offset``. Reading is pure: the
        observer never advances its offset and never mutates ``stream_store``.

        The returned ``frames`` are the durable evidence itself (interleaved
        stdout/stderr/facts); this call never substitutes a preview for it.
        """
        if stream_store is None:
            return {
                "run_id": run_id,
                "after_cursor": self._state.journal_offset if after_cursor is None else after_cursor,
                "latest_cursor": self._state.journal_offset if after_cursor is None else after_cursor,
                "frames": [],
                "is_full_evidence": True,
                "status": "no durable stream store configured",
            }
        cursor = self._state.journal_offset if after_cursor is None else after_cursor
        frames = stream_store.frames(cursor, limit=limit, run_id=run_id, task_id=task_id)
        latest = frames[-1].cursor if frames else cursor
        return {
            "run_id": run_id,
            "after_cursor": cursor,
            "latest_cursor": latest,
            "frame_count": len(frames),
            "frames": [f.to_dict() for f in frames],
            "is_full_evidence": True,
        }

    def read_preview(
        self,
        run_id: str,
        stream_store: Any,
        *,
        after_cursor: Optional[int] = None,
        max_bytes: int = 1024,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return a bounded preview of progress — never the raw evidence.

        The result is always marked ``is_full_evidence=False`` so a consumer can
        never mistake the preview limit for the whole stream; the raw frames
        remain available through :meth:`read_progress`.
        """
        if stream_store is None:
            return {
                "run_id": run_id,
                "text": "",
                "is_full_evidence": False,
                "status": "no durable stream store configured",
            }
        cursor = self._state.journal_offset if after_cursor is None else after_cursor
        frames = stream_store.frames(cursor, run_id=run_id, task_id=task_id)
        text = ""
        observed = 0
        for f in frames:
            observed += f.byte_length
            if len(text) < max_bytes and f.data:
                text += f.data.decode("utf-8", errors="replace")
        text = text[:max_bytes]
        return {
            "run_id": run_id,
            "after_cursor": cursor,
            "latest_cursor": frames[-1].cursor if frames else cursor,
            "text": text,
            "observed_bytes": observed,
            "is_truncated": observed > len(text.encode("utf-8", errors="replace")),
            "is_full_evidence": False,
        }

    def read_attempt_fact(
        self,
        run_id: str,
        stream_store: Any,
        attempt: int,
        *,
        task_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Read one attempt's immutable collected fact (read-only)."""
        if stream_store is None:
            return None
        fact = stream_store.fact(attempt, run_id=run_id, task_id=task_id)
        if fact is None:
            return None
        return fact.to_dict()

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ('_storage_path', '_state'):
            super().__setattr__(name, value)
        else:
            raise ReadOnlyError(f"Cannot mutate attribute '{name}' on ReadOnlyObserver.")
