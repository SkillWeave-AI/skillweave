"""Cursor-based progress frames and durable run snapshots (SW-1510-STREAM).

This module is the read side of the durable stream. It turns a run's collected
stream evidence and lifecycle facts into two shapes:

* **Progress frames** — an ordered, cursor-based view of what has happened so
  far (an interleaved trace of stream chunks, attempt facts, and the run's own
  state transitions), read with ``after_cursor``. Reconnecting returns exactly
  the frames not yet seen: ordered, gap-free, no duplication. A *preview* is
  always a bounded decoration and never replaces the raw stream evidence.
* **Run snapshots** — a ``run-snapshot-v1`` conforming record of the run at a
  committed event cursor. ``methodology``, ``autonomy_policy`` and
  ``session_schedule`` are three distinct fields and are never conflated into
  one mode string; ``process_identity`` is a native-handle object issued by the
  trusted run service, not a model-authored string.

Methodology (Assay) and autonomy policy (Unicorn) enter only as *data* on the
snapshot — this module composes them by name; it owns no controller and makes
no gate decision. The gate state it records is derived by the completion
contract elsewhere and passed in.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .stream import (
    AttemptFact,
    DurableStreamStore,
    FRAME_CHUNK,
    FRAME_FACT,
    Incompleteness,
    StreamFrame,
    StreamName,
)

#: The autonomy policy whose name maps to the ``unicorn`` autonomy enum.
#: The Assay autonomy policies are data here: a policy name is carried, not
#: interpreted, so no second authority is introduced.
ASSAY_TO_AUTONOMY = {
    "conservative": "conservative",
    "moderate": "medium",
    "medium": "medium",
    "unicorn": "unicorn",
}

#: Methodologies expressible in a ``run-snapshot-v1`` (the schema's enum).
SNAPSHOT_METHODOLOGIES = frozenset({"assay", "rex", "ralph", "gauntlet"})


class SnapshotError(Exception):
    """A snapshot could not be built without fabricating a field."""


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProgressFrame:
    """One ordered, cursor-addressed progress unit for a reconnecting reader.

    A progress frame is either stream evidence (``stream`` set, ``bytes`` are
    the retained slice) or a fact/lifecycle marker. It never carries raw bytes
    as a substitute for the fact it reports: ``byte_length`` and ``digest``
    describe what was observed even when the retained ``bytes`` are bounded.
    """

    cursor: int
    kind: str
    run_id: str
    attempt: Optional[int]
    stream: Optional[str]
    digest: str
    byte_length: int
    retained: int
    data: bytes
    at: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "kind": self.kind,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "stream": self.stream,
            "digest": self.digest,
            "byte_length": self.byte_length,
            "retained": self.retained,
            "data": self.data.decode("utf-8", errors="replace"),
            "at": self.at,
            "detail": self.detail,
        }


def progress_frames(
    store: DurableStreamStore,
    run_id: str,
    *,
    after_cursor: int = 0,
    limit: Optional[int] = None,
) -> list[ProgressFrame]:
    """Return ordered progress frames for *run_id* with cursor > ``after_cursor``.

    This is the reconnect/replay surface over a run: a consumer that stopped at
    cursor N resumes exactly there. Frames are ordered by their single monotonic
    cursor (the interleaving of stdout, stderr, and attempt facts), so the trace
    is replayed in the order it actually happened.
    """
    frames: list[ProgressFrame] = []
    for frame in store.frames(after_cursor):
        if frame.run_id != run_id:
            continue
        frames.append(_to_progress(frame))
    if limit is not None:
        frames = frames[:limit]
    return frames


def _to_progress(frame: StreamFrame) -> ProgressFrame:
    detail: dict[str, Any] = {}
    if frame.event_type == FRAME_CHUNK:
        detail = {"message": "stream chunk"}
    elif frame.event_type == FRAME_FACT:
        detail = {"message": "attempt fact recorded"}
    elif frame.event_type == "sealed":
        detail = {"message": "stream sealed", "incompleteness_final": True}
    return ProgressFrame(
        cursor=frame.cursor,
        kind=frame.event_type,
        run_id=frame.run_id,
        attempt=frame.attempt,
        stream=frame.stream or None,
        digest=frame.digest,
        byte_length=frame.byte_length,
        retained=len(frame.data),
        data=frame.data,
        at=frame.at,
        detail=detail,
    )


def preview_frames(
    store: DurableStreamStore,
    run_id: str,
    *,
    after_cursor: int = 0,
    max_bytes: int = 1024,
) -> dict[str, Any]:
    """Return a bounded *preview* over a run's progress, never the raw evidence.

    The preview carries at most ``max_bytes`` of retained stream text and is
    always marked ``is_full_evidence=False``; the raw frames remain resolvable
    through :func:`progress_frames`. A preview limit therefore never replaces
    raw evidence.
    """
    frames = progress_frames(store, run_id, after_cursor=after_cursor)
    retained_total = 0
    observed_total = 0
    preview_text: list[str] = []
    for frame in frames:
        retained_total += frame.retained
        # Only chunk frames carry newly observed bytes; a sealed frame's
        # ``byte_length`` is a summary of the stream, not extra bytes, and must
        # not be double-counted here.
        if frame.kind == FRAME_CHUNK:
            observed_total += frame.byte_length
        if sum(len(t) for t in preview_text) < max_bytes and frame.data:
            preview_text.append(frame.data.decode("utf-8", errors="replace"))
    text = "".join(preview_text)[:max_bytes]
    return {
        "run_id": run_id,
        "after_cursor": after_cursor,
        "latest_cursor": frames[-1].cursor if frames else after_cursor,
        "frame_count": len(frames),
        "retained_bytes": retained_total,
        "observed_bytes": observed_total,
        "text": text,
        "is_truncated": observed_total > len(text.encode("utf-8", errors="replace")),
        "is_full_evidence": False,
    }


@dataclass
class RunSnapshotInputs:
    """The trusted inputs a run snapshot is built from.

    ``process_identity`` and ``authority_decision_digest`` are supplied by the
    trusted run service seam — they are never read from the wire instance. The
    three mode fields (``methodology``, ``autonomy_policy``, ``session_schedule``)
    are distinct and required.
    """

    run_id: str
    task_id: str
    state: str
    gate_state: str
    methodology: str
    autonomy_policy: str
    session_schedule: str
    subject_digest: str
    brief_digest: str
    envelope_digest: str
    process_identity: Mapping[str, str]
    authority_decision_digest: str
    attempt: int = 1
    version: int = 1
    event_cursor: int = 0
    schema_version: str = "1.0.0"


def _require_digest(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise SnapshotError(f"{name} must be a 64-char lowercase hex digest")
    return value


def _require_handle(handle: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(handle, Mapping):
        raise SnapshotError("process_identity must be a native handle object")
    handle_id = handle.get("handle_id")
    service_principal = handle.get("service_principal")
    if not handle_id or not service_principal:
        raise SnapshotError(
            "process_identity must carry handle_id and service_principal; "
            "a model-authored string is not a native handle"
        )
    if set(handle.keys()) - {"handle_id", "service_principal"}:
        raise SnapshotError("process_identity carries unknown handle fields")
    return {"handle_id": str(handle_id), "service_principal": str(service_principal)}


def build_run_snapshot(
    inputs: RunSnapshotInputs,
    *,
    store: Optional[DurableStreamStore] = None,
) -> dict[str, Any]:
    """Build a ``run-snapshot-v1`` dict from trusted inputs.

    Every digest, the native handle, and the three distinct mode fields are
    validated rather than defaulted: a missing or malformed trusted field is a
    :class:`SnapshotError`, never a fabricated value. When *store* is supplied
    and the caller left ``event_cursor`` at 0, the store's current cursor is
    used, so the snapshot is pinned to a real committed cursor.
    """
    if inputs.methodology not in SNAPSHOT_METHODOLOGIES:
        raise SnapshotError(
            f"methodology {inputs.methodology!r} is not one of "
            f"{sorted(SNAPSHOT_METHODOLOGIES)}"
        )
    if inputs.autonomy_policy not in ("conservative", "medium", "unicorn"):
        raise SnapshotError(
            f"autonomy_policy {inputs.autonomy_policy!r} is not one of "
            f"['conservative', 'medium', 'unicorn']"
        )
    if not inputs.session_schedule:
        raise SnapshotError("session_schedule must be a non-empty string")
    if not inputs.state:
        raise SnapshotError("state must be a non-empty string")
    if not inputs.gate_state:
        raise SnapshotError("gate_state must be a non-empty string")
    if inputs.attempt < 1:
        raise SnapshotError("attempt must be >= 1")
    if inputs.version < 1:
        raise SnapshotError("version must be >= 1")

    event_cursor = inputs.event_cursor
    if store is not None and event_cursor == 0:
        event_cursor = store.cursor

    return {
        "schema_version": inputs.schema_version,
        "run_id": inputs.run_id,
        "version": inputs.version,
        "task_id": inputs.task_id,
        "attempt": inputs.attempt,
        "state": inputs.state,
        "gate_state": inputs.gate_state,
        "methodology": inputs.methodology,
        "autonomy_policy": inputs.autonomy_policy,
        "session_schedule": inputs.session_schedule,
        "subject_digest": _require_digest(inputs.subject_digest, "subject_digest"),
        "brief_digest": _require_digest(inputs.brief_digest, "brief_digest"),
        "envelope_digest": _require_digest(
            inputs.envelope_digest, "envelope_digest"
        ),
        "process_identity": _require_handle(inputs.process_identity),
        "event_cursor": event_cursor,
        "authority_decision_digest": _require_digest(
            inputs.authority_decision_digest, "authority_decision_digest"
        ),
    }


def snapshot_inputs_from_run(
    *,
    run_id: str,
    task_id: str,
    state: str,
    gate_state: str,
    methodology: str,
    autonomy_policy: str,
    session_schedule: str,
    subject_digest: str,
    brief_digest: str,
    envelope_digest: str,
    process_identity: Mapping[str, str],
    authority_decision_digest: str,
    attempt: int = 1,
    version: int = 1,
    event_cursor: int = 0,
) -> RunSnapshotInputs:
    """Convenience constructor mirroring the frozen snapshot's field names."""
    return RunSnapshotInputs(
        run_id=run_id,
        task_id=task_id,
        state=state,
        gate_state=gate_state,
        methodology=methodology,
        autonomy_policy=autonomy_policy,
        session_schedule=session_schedule,
        subject_digest=subject_digest,
        brief_digest=brief_digest,
        envelope_digest=envelope_digest,
        process_identity=process_identity,
        authority_decision_digest=authority_decision_digest,
        attempt=attempt,
        version=version,
        event_cursor=event_cursor,
    )


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """The canonical digest of a snapshot (its identity, not a signature)."""
    return _canonical_digest(dict(snapshot))


__all__ = [
    "ASSAY_TO_AUTONOMY",
    "ProgressFrame",
    "RunSnapshotInputs",
    "SNAPSHOT_METHODOLOGIES",
    "SnapshotError",
    "build_run_snapshot",
    "preview_frames",
    "progress_frames",
    "snapshot_digest",
    "snapshot_inputs_from_run",
]
