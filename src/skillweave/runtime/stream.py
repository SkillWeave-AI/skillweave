"""Durable per-attempt output stream and collected process facts (SW-1510-STREAM).

This module is the durable evidence channel for a worker attempt. It records two
*distinct* things and never lets the second stand in for the first:

* **The stream** — the per-attempt, per-stream (``stdout``/``stderr``) bytes a
  real child produced, appended under a single monotonic, interleaved cursor with
  an exact sha256 content digest. A user-facing *preview* is a bounded,
  explicitly-truncated decoration over this evidence; it is never the evidence
  itself.
* **The facts** — the observed exit code, the *measured* monotonic duration, and
  the raw result subtype (``exited``/``signaled``/``cancelled``/``timed_out``/
  ``launch_failed``). These are immutable once recorded. An error is never
  coerced to success and a measured duration is never clipped or fabricated.

Persistence
-----------
The store has two explicit, non-interchangeable modes:

* **disk-backed** (``root=<path>``) — bytes, frames, facts and the committed
  cursor are appended and fsynced *as chunks arrive*, so a second process can
  reopen the same root after a normal exit *or* after the collecting process was
  interrupted, and recover the retained bytes, their exact digests, the ordering
  and the raw facts.
* **memory-only** (``root=None``) — explicitly non-durable. ``is_durable`` is
  False and ``persistence_mode`` is ``"memory"``. Nothing is written to disk and
  a second process sees nothing.

A durable store refuses to silently become memory-only: passing ``durable=True``
without a ``root`` is an error. Supported protected execution must use a durable
store (the opt-in native path in :mod:`skillweave.runsvc.service` enforces this).

Identity
--------
Every stream and fact is keyed by the *compound* identity
``(run_id, task_id, attempt)``. The same attempt number in two runs or tasks is a
different stream; a lookup that is ambiguous because more than one identity
shares an attempt number raises :class:`StreamIdentityError` unless the caller
names the run/task.

Bounds and honesty
------------------
A per-stream byte cap bounds retained bytes; a per-stream frame cap bounds
metadata growth. Backpressure (bytes observed after the byte cap) and metadata
truncation (frames omitted after the frame cap) are both recorded, and the exact
digest keeps covering *every observed byte*. Typed incompleteness
(``complete``/``limit_reached``/``interrupted``/``metadata_limit_reached``/
``incomplete``) is data — a bounded prefix is never mistaken for the whole
stream. If a disk write fails the store raises :class:`StreamPersistenceError`
and marks itself degraded rather than claiming a durable ack it did not make.

The module owns no process spawning and no authority. It is a store; the
production spawn/collection path in ``skillweave.core.proc.runner`` feeds it and
the run application service binds its output into the run's evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from skillweave.redaction import scrub_secrets

#: Default number of bytes retained per stream as evidence (1 MiB), matching the
#: runner's historical output ceiling. Retaining is bounded independently of how
#: much the child writes; the exact digest still covers every observed byte.
DEFAULT_RETAIN_LIMIT = 1024 * 1024

#: Default maximum number of frames retained per stream, bounding metadata
#: growth. Once reached, further bytes are still hashed and counted (so the
#: digest stays exact) but no new frame is emitted; the stream is typed
#: ``metadata_limit_reached`` when sealed.
DEFAULT_MAX_FRAMES = 65536

#: Default size of the bounded *closing* excerpt kept per stream (SW-1511A-
#: RESULT-001, finding F-1511A-01). Retention keeps the *head* of a stream and
#: drops the tail, so a child's closing message — the last stream-json
#: ``result`` event, or simply the final line it wrote — is exactly what a
#: retention cap loses. This excerpt is captured at *ingest* time, independently
#: of retention, so the tail survives the cap and can be bound into the attempt's
#: final result. It is a bounded excerpt, never the whole tail: the digest
#: remains the whole-observed-byte evidence.
DEFAULT_CLOSING_EXCERPT_LIMIT = 4 * 1024

#: Event kinds carried by a durable stream frame.
FRAME_CHUNK = "chunk"
FRAME_SEALED = "sealed"
FRAME_FACT = "fact"


class StreamName(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


class PersistenceMode(str, Enum):
    DISK = "disk"
    MEMORY = "memory"


class Incompleteness(str, Enum):
    """How completely a stream's retained evidence represents what was observed.

    ``incomplete`` is the open state before a stream is sealed. ``complete``
    means every observed byte was retained and every frame recorded.
    ``limit_reached`` means the retainer hit its byte cap: the retained prefix
    and the digest are truthful about what was seen, but the retained bytes are
    *not* the whole stream. ``metadata_limit_reached`` means the frame cap was
    hit: bytes are still hashed/counted, but the frame log is a prefix. Both
    ``limit_reached`` and ``metadata_limit_reached`` mean the retained bytes are
    not the whole stream; ``interrupted`` means collection stopped before the
    child's stream was fully drained.
    """

    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    LIMIT_REACHED = "limit_reached"
    METADATA_LIMIT_REACHED = "metadata_limit_reached"
    INTERRUPTED = "interrupted"


class ResultSubtype(str, Enum):
    """The raw, un-coerced way an attempt ended."""

    EXITED = "exited"
    SIGNALED = "signaled"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LAUNCH_FAILED = "launch_failed"
    STORAGE_FAILED = "storage_failed"


class StreamError(Exception):
    """Base error for the durable stream store."""


class StreamFactConflict(StreamError):
    """Raised when an attempt's collected fact would be overwritten.

    Collected facts are immutable: a second, differing ``record_fact`` for the
    same identity is refused rather than silently replacing the recorded exit
    code, duration, or subtype.
    """

    def __init__(self, attempt: int, field_name: str):
        self.attempt = attempt
        self.field_name = field_name
        super().__init__(
            f"attempt {attempt} already has an immutable '{field_name}' fact; "
            "refusing to overwrite a collected fact"
        )


class StreamSealedError(StreamError):
    """Raised when a sealed stream is asked to accept more bytes."""


class StreamIdentityError(StreamError):
    """Raised when a compound identity is ambiguous or mismatched.

    Storage and replay are always scoped to ``(run_id, task_id, attempt)``. A
    bare attempt number that matches more than one identity, or a caller that
    supplies a run/task that disagrees with the begun identity, is refused
    rather than silently resolving to the wrong stream.
    """


class StreamPersistenceError(StreamError):
    """A durable write failed; the store is degraded and reports the failure.

    The store never pretends a write succeeded. On failure it sets
    ``degraded=True`` and ``last_error`` and re-raises, so a collector cannot
    claim durable evidence it did not persist.
    """


@dataclass(frozen=True)
class StreamFrame:
    """One ordered, cursor-addressed unit of durable stream progress.

    Frames from every stream of an attempt share one monotonic cursor, so an
    interleaved stdout/stderr trace is replayed in the order it happened. The
    ``data`` is the *retained* slice of the observed chunk (bounded by the
    stream's retain limit); ``byte_length`` and ``digest`` describe the whole
    observed chunk, so a bounded frame is still exact about what arrived.
    """

    cursor: int
    run_id: str
    task_id: str
    attempt: int
    stream: str
    event_type: str
    data: bytes
    digest: str
    byte_length: int
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "stream": self.stream,
            "event_type": self.event_type,
            "data": self.data.decode("utf-8", errors="replace"),
            "digest": self.digest,
            "byte_length": self.byte_length,
            "at": self.at,
        }


@dataclass(frozen=True)
class StreamSummary:
    """The exact, bounded summary of one attempt's one stream."""

    stream: str
    digest: str
    retained_bytes: int
    retained_digest: str
    observed_bytes: int
    incompleteness: str
    frame_count: int
    cursor_start: int
    cursor_end: int
    backpressure_events: int
    metadata_limited: bool = False

    @property
    def is_complete(self) -> bool:
        return self.incompleteness == Incompleteness.COMPLETE.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "digest": self.digest,
            "retained_bytes": self.retained_bytes,
            "retained_digest": self.retained_digest,
            "observed_bytes": self.observed_bytes,
            "incompleteness": self.incompleteness,
            "frame_count": self.frame_count,
            "cursor_start": self.cursor_start,
            "cursor_end": self.cursor_end,
            "backpressure_events": self.backpressure_events,
            "metadata_limited": self.metadata_limited,
        }


@dataclass(frozen=True)
class AttemptFact:
    """The immutable collected facts of one attempt.

    ``duration_ns`` is a *measured* monotonic interval, never a wall-clock
    subtraction and never clipped. ``result_subtype`` is the raw, un-coerced
    shape of the ending.
    """

    run_id: str
    task_id: str
    attempt: int
    exit_code: Optional[int]
    signal: Optional[int]
    result_subtype: str
    termination: str
    duration_ns: int
    started_at: str
    completed_at: str
    pid: int

    @property
    def duration_seconds(self) -> float:
        return self.duration_ns / 1_000_000_000.0

    @property
    def succeeded(self) -> bool:
        return (
            self.result_subtype == ResultSubtype.EXITED.value
            and self.exit_code == 0
            and self.signal is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "result_subtype": self.result_subtype,
            "termination": self.termination,
            "duration_ns": self.duration_ns,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "pid": self.pid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttemptFact":
        return cls(
            run_id=data["run_id"],
            task_id=data["task_id"],
            attempt=int(data["attempt"]),
            exit_code=data.get("exit_code"),
            signal=data.get("signal"),
            result_subtype=data["result_subtype"],
            termination=data["termination"],
            duration_ns=int(data["duration_ns"]),
            started_at=data["started_at"],
            completed_at=data["completed_at"],
            pid=int(data.get("pid", 0)),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity_key(run_id: str, task_id: str, attempt: int) -> tuple[str, str, int]:
    """The compound identity tuple that keys every stream and fact."""
    return (run_id, task_id, int(attempt))


class StreamSpool:
    """One stream (stdout or stderr) of one attempt.

    Retains at most ``retain_limit`` bytes as evidence while hashing *every*
    observed byte. When the retainer is at its cap and more bytes arrive, the
    spool records the pressure (``backpressure_events``) and marks itself
    ``limit_reached`` — it keeps counting and hashing so the digest and observed
    count stay truthful, but it does not keep growing. A separate frame cap
    bounds metadata; its exhaustion is typed ``metadata_limit_reached``.

    A restored spool (reopened from disk) cannot continue a sha256 from its
    persisted hex digest, so a restored unsealed stream is sealed as
    ``interrupted``: honest partial evidence, never a silently resumed stream.
    """

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        stream: str,
        retain_limit: int = DEFAULT_RETAIN_LIMIT,
        max_frames: int = DEFAULT_MAX_FRAMES,
        closing_excerpt_limit: int = DEFAULT_CLOSING_EXCERPT_LIMIT,
    ) -> None:
        if retain_limit < 0:
            raise StreamError("retain_limit must be non-negative")
        if max_frames < 1:
            raise StreamError("max_frames must be >= 1")
        if closing_excerpt_limit < 0:
            raise StreamError("closing_excerpt_limit must be non-negative")
        self.run_id = run_id
        self.task_id = task_id
        self.attempt = attempt
        self.stream = stream
        self.retain_limit = retain_limit
        self.max_frames = max_frames
        self.closing_excerpt_limit = closing_excerpt_limit
        self._retained = bytearray()
        #: The bounded tail kept *independently of retention*. Only the last
        #: ``closing_excerpt_limit`` bytes are ever held, so a child that writes
        #: without bound cannot grow the spool's memory through this buffer.
        self._closing_tail = bytearray()
        self._hasher = hashlib.sha256()
        self._observed = 0
        self._frame_count = 0
        self._cursor_start: Optional[int] = None
        self._cursor_end: int = 0
        self._limited = False
        self._metadata_limited = False
        self._sealed = False
        self._interrupted = False
        self.incompleteness = Incompleteness.INCOMPLETE
        self.backpressure_events = 0
        #: Set when the spool was restored from disk with a persisted (but not
        #: continuable) digest.
        self._restored = False

    # -- ingestion ----------------------------------------------------------

    @property
    def identity(self) -> tuple[str, str, int]:
        return (self.run_id, self.task_id, self.attempt)

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def retained_bytes(self) -> int:
        return len(self._retained)

    @property
    def observed_bytes(self) -> int:
        return self._observed

    @property
    def digest(self) -> str:
        """Exact sha256 over every observed byte (not just the retained prefix).

        A restored spool returns the digest it persisted; the running hasher
        cannot be continued across a process boundary, and a restored spool is
        sealed so its digest is frozen.
        """
        persisted = getattr(self, "_persisted_digest", None)
        if self._restored and persisted is not None:
            return persisted
        return self._hasher.hexdigest()

    @property
    def retained_digest(self) -> str:
        return hashlib.sha256(bytes(self._retained)).hexdigest()

    @property
    def is_limited(self) -> bool:
        return self._limited

    @property
    def metadata_limited(self) -> bool:
        return self._metadata_limited

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def cursor_start(self) -> int:
        return self._cursor_start if self._cursor_start is not None else 0

    @property
    def cursor_end(self) -> int:
        return self._cursor_end

    def ingest(self, chunk: bytes) -> tuple[int, bytes]:
        """Account for an observed ``chunk``.

        Returns ``(frame_delta, retained_slice)``. Every observed byte updates
        the digest; only up to the retain limit is kept. ``frame_delta`` is 0
        when the frame cap has been reached (metadata is bounded): the bytes are
        still hashed and counted, but no frame is emitted.
        """
        if self._sealed:
            raise StreamSealedError(
                f"stream '{self.stream}' of attempt {self.attempt} is sealed"
            )
        if not chunk:
            return 0, b""
        self._observed += len(chunk)
        self._hasher.update(chunk)
        self._note_closing_tail(chunk)
        room = self.retain_limit - len(self._retained)
        if room > 0:
            retained = bytes(chunk[:room])
            self._retained.extend(retained)
            if len(chunk) > room:
                self._limited = True
        else:
            retained = b""
            self._limited = True
        if retained != chunk:
            # More bytes arrived than could be retained: observable pressure.
            self.backpressure_events += 1
        if self._frame_count >= self.max_frames:
            self._metadata_limited = True
            return 0, retained
        return 1, retained

    def _note_closing_tail(self, chunk: bytes) -> None:
        """Keep the last ``closing_excerpt_limit`` observed bytes, at ingest time.

        This is deliberately *outside* retention: the retained prefix is capped
        and keeps the head, so the tail — where a child's closing message lives —
        would otherwise be dropped. Trimming per chunk keeps the buffer bounded
        no matter how much the child writes.
        """
        limit = self.closing_excerpt_limit
        if limit <= 0:
            return
        self._closing_tail.extend(chunk)
        if len(self._closing_tail) > limit:
            del self._closing_tail[: len(self._closing_tail) - limit]

    def closing_tail(self) -> bytes:
        """The bounded trailing excerpt observed so far (b"" if none observed)."""
        return bytes(self._closing_tail)

    def closing_byte_range(self) -> tuple[int, int]:
        """The byte range of the observed stream the closing tail covers.

        ``(start, end)`` are offsets into the *observed* byte sequence, so the
        excerpt's position is exact even though retention dropped everything
        around it. An unobserved stream yields the empty range ``(0, 0)``.
        """
        end = self._observed
        return (max(0, end - len(self._closing_tail)), end)

    def mark_interrupted(self) -> None:
        """Record that collection stopped before the stream was fully drained."""
        self._interrupted = True

    def seal(self) -> str:
        """Close the spool and freeze its typed incompleteness.

        Precedence is ``interrupted`` (collection stopped early) over the limit
        markers: an interrupted stream is partial for a stronger reason.
        """
        if self._sealed:
            return self.incompleteness
        self._sealed = True
        if self._interrupted:
            self.incompleteness = Incompleteness.INTERRUPTED
        elif self._metadata_limited:
            self.incompleteness = Incompleteness.METADATA_LIMIT_REACHED
        elif self._limited:
            self.incompleteness = Incompleteness.LIMIT_REACHED
        else:
            self.incompleteness = Incompleteness.COMPLETE
        return self.incompleteness

    def summary(self) -> StreamSummary:
        return StreamSummary(
            stream=self.stream,
            digest=self.digest,
            retained_bytes=len(self._retained),
            retained_digest=self.retained_digest,
            observed_bytes=self._observed,
            incompleteness=self.incompleteness.value,
            frame_count=self._frame_count,
            cursor_start=self.cursor_start,
            cursor_end=self._cursor_end,
            backpressure_events=self.backpressure_events,
            metadata_limited=self._metadata_limited,
        )

    def read_retained(self) -> bytes:
        return bytes(self._retained)

    # -- persistence state --------------------------------------------------

    def persisted_state(self) -> dict[str, Any]:
        """The serializable state of this spool (digest preserved as hex)."""
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "stream": self.stream,
            "retain_limit": self.retain_limit,
            "max_frames": self.max_frames,
            "closing_excerpt_limit": self.closing_excerpt_limit,
            "observed_bytes": self._observed,
            "digest": self._hasher.hexdigest(),
            "frame_count": self._frame_count,
            "cursor_start": self._cursor_start,
            "cursor_end": self._cursor_end,
            "limited": self._limited,
            "metadata_limited": self._metadata_limited,
            "sealed": self._sealed,
            "interrupted": self._interrupted,
            "incompleteness": self.incompleteness.value,
            "backpressure_events": self.backpressure_events,
        }

    @classmethod
    def restore(
        cls,
        state: dict[str, Any],
        retained: bytes,
        *,
        digest_override: Optional[str] = None,
        closing_tail: bytes = b"",
    ) -> "StreamSpool":
        """Rebuild a spool from persisted state and its retained bytes.

        The running sha256 cannot be continued from a hex digest, so the spool
        records the persisted digest and is marked restored. An unsealed
        restored stream is sealed as ``interrupted`` — collection was not
        completed by the process that owned it.
        """
        spool = cls(
            run_id=state["run_id"],
            task_id=state["task_id"],
            attempt=int(state["attempt"]),
            stream=state["stream"],
            retain_limit=int(state.get("retain_limit", DEFAULT_RETAIN_LIMIT)),
            max_frames=int(state.get("max_frames", DEFAULT_MAX_FRAMES)),
            closing_excerpt_limit=int(
                state.get("closing_excerpt_limit", DEFAULT_CLOSING_EXCERPT_LIMIT)
            ),
        )
        spool._retained = bytearray(retained)
        spool._closing_tail = bytearray(closing_tail)
        spool._observed = int(state.get("observed_bytes", len(retained)))
        spool._frame_count = int(state.get("frame_count", 0))
        spool._cursor_start = state.get("cursor_start")
        spool._cursor_end = int(state.get("cursor_end", 0) or 0)
        spool._limited = bool(state.get("limited", False))
        spool._metadata_limited = bool(state.get("metadata_limited", False))
        spool._interrupted = bool(state.get("interrupted", False))
        spool._sealed = bool(state.get("sealed", False))
        spool._restored = True
        spool._persisted_digest = digest_override or state.get("digest") or hashlib.sha256(retained).hexdigest()
        spool.incompleteness = Incompleteness(state.get("incompleteness", Incompleteness.INCOMPLETE.value))
        spool.backpressure_events = int(state.get("backpressure_events", 0))
        if not spool._sealed:
            # A process that reopens an open stream cannot continue its hash.
            spool.mark_interrupted()
            spool.seal()
        return spool

    # -- internal bookkeeping used by the store -----------------------------

    def _note_frame(self, cursor: int) -> None:
        if self._cursor_start is None:
            self._cursor_start = cursor
        self._cursor_end = cursor
        self._frame_count += 1


class DurableStreamStore:
    """The durable, cursor-addressed store of per-attempt stream evidence.

    One monotonic cursor orders every frame of every stream of every attempt, so
    an interleaved stdout/stderr trace replays in real order. Attempts carry
    immutable :class:`AttemptFact` records keyed by the compound identity
    ``(run_id, task_id, attempt)``.

    With ``root=<path>`` the store is disk-backed: frames, facts and the
    committed cursor are appended and fsynced as they are produced, and a second
    process can reopen the same root to recover ordered frames, exact digests,
    retained bytes and raw facts — after a normal exit or after the collector was
    interrupted. With ``root=None`` the store is memory-only and explicitly
    non-durable (``is_durable`` is False).
    """

    def __init__(
        self,
        retain_limit: Optional[int] = None,
        *,
        root: Optional[str] = None,
        max_frames: Optional[int] = None,
        durable: Optional[bool] = None,
    ) -> None:
        if durable is None:
            durable = root is not None
        if durable and not root:
            raise StreamPersistenceError(
                "durable=True requires a root path; refusing to silently run "
                "memory-only"
            )
        if retain_limit is not None and retain_limit < 0:
            raise StreamError("retain_limit must be non-negative")
        self.retain_limit = DEFAULT_RETAIN_LIMIT if retain_limit is None else retain_limit
        self.max_frames = DEFAULT_MAX_FRAMES if max_frames is None else max_frames
        self._durable = bool(durable and root)
        self.root: Optional[str] = str(root) if root else None
        self.degraded = False
        self.last_error: Optional[str] = None
        self._spools: dict[tuple[str, str, int], dict[str, StreamSpool]] = {}
        self._facts: dict[tuple[str, str, int], AttemptFact] = {}
        self._log: list[StreamFrame] = []
        self._cursor = 0
        self._frames_path: Optional[Path] = None
        self._facts_path: Optional[Path] = None
        self._cursor_path: Optional[Path] = None
        self._streams_dir: Optional[Path] = None
        if self._durable:
            self._init_disk(Path(root))

    # -- persistence mode ---------------------------------------------------

    @property
    def is_durable(self) -> bool:
        return self._durable

    @property
    def persistence_mode(self) -> str:
        return PersistenceMode.DISK.value if self._durable else PersistenceMode.MEMORY.value

    # -- disk layout and (re)load ------------------------------------------

    @staticmethod
    def _key_hash(run_id: str, task_id: str, attempt: int) -> str:
        raw = f"{run_id}\x00{task_id}\x00{attempt}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:32]

    def _init_disk(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._streams_dir = root / "streams"
        self._streams_dir.mkdir(parents=True, exist_ok=True)
        self._frames_path = root / "frames.jsonl"
        self._facts_path = root / "facts.jsonl"
        self._cursor_path = root / "cursor.json"
        meta_path = root / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                self.retain_limit = int(meta.get("retain_limit", self.retain_limit))
                self.max_frames = int(meta.get("max_frames", self.max_frames))
            except (OSError, ValueError):
                pass
        else:
            self._write_json_atomic(
                meta_path,
                {"retain_limit": self.retain_limit, "max_frames": self.max_frames},
            )
        if not self._frames_path.exists():
            self._frames_path.touch()
        if not self._facts_path.exists():
            self._facts_path.touch()
        self._reload_from_disk()

    def _reload_from_disk(self) -> None:
        """Rebuild in-memory state from the persisted files."""
        assert self._frames_path is not None
        if self._cursor_path is not None and self._cursor_path.exists():
            try:
                self._cursor = int(json.loads(self._cursor_path.read_text()).get("cursor", 0))
            except (OSError, ValueError):
                self._cursor = 0
        assert self._streams_dir is not None
        for state_path in sorted(self._streams_dir.glob("*.json")):
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError):
                continue
            raw_path = state_path.with_suffix(".raw")
            retained = raw_path.read_bytes() if raw_path.exists() else b""
            closing_path = state_path.with_suffix(".closing")
            closing_tail = closing_path.read_bytes() if closing_path.exists() else b""
            spool = StreamSpool.restore(state, retained, closing_tail=closing_tail)
            key = identity_key(spool.run_id, spool.task_id, spool.attempt)
            self._spools.setdefault(key, {})[spool.stream] = spool
        self._log = []
        for line in self._frames_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._log.append(self._frame_from_json(json.loads(line)))
            except (ValueError, KeyError):
                continue
        assert self._facts_path is not None
        for line in self._facts_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                fact = AttemptFact.from_dict(json.loads(line))
            except (ValueError, KeyError):
                continue
            self._facts[identity_key(fact.run_id, fact.task_id, fact.attempt)] = fact
        if self._log:
            self._cursor = max(self._cursor, max(f.cursor for f in self._log))

    @staticmethod
    def _frame_to_json(frame: StreamFrame) -> dict[str, Any]:
        return {
            "cursor": frame.cursor,
            "run_id": frame.run_id,
            "task_id": frame.task_id,
            "attempt": frame.attempt,
            "stream": frame.stream,
            "event_type": frame.event_type,
            "data_b64": base64.b64encode(frame.data).decode("ascii"),
            "digest": frame.digest,
            "byte_length": frame.byte_length,
            "at": frame.at,
        }

    @staticmethod
    def _frame_from_json(obj: dict[str, Any]) -> StreamFrame:
        return StreamFrame(
            cursor=int(obj["cursor"]),
            run_id=obj["run_id"],
            task_id=obj["task_id"],
            attempt=int(obj["attempt"]),
            stream=obj["stream"],
            event_type=obj["event_type"],
            data=base64.b64decode(obj.get("data_b64", "")),
            digest=obj["digest"],
            byte_length=int(obj["byte_length"]),
            at=obj["at"],
        )

    def _fail_persist(self, exc: BaseException) -> None:
        self.degraded = True
        self.last_error = f"{type(exc).__name__}: {exc}"

    def _write_json_atomic(self, path: Path, value: Any) -> None:
        if not self._durable:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
            with os.fdopen(fd, "w") as fh:
                json.dump(value, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            self._fail_persist(exc)
            raise StreamPersistenceError(f"failed to persist {path.name}: {exc}") from exc

    def _write_bytes_atomic(self, path: Path, data: bytes) -> None:
        if not self._durable:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            self._fail_persist(exc)
            raise StreamPersistenceError(f"failed to persist {path.name}: {exc}") from exc

    def _append_json(self, path: Optional[Path], value: Any) -> None:
        if not self._durable or path is None:
            return
        try:
            line = json.dumps(value, sort_keys=True).encode("utf-8") + b"\n"
            with open(path, "ab") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            self._fail_persist(exc)
            raise StreamPersistenceError(f"failed to append {path.name}: {exc}") from exc

    def _commit_cursor(self) -> None:
        if self._cursor_path is not None:
            self._write_json_atomic(self._cursor_path, {"cursor": self._cursor})

    def _persist_spool(self, spool: StreamSpool) -> None:
        if not self._durable or self._streams_dir is None:
            return
        base = self._streams_dir / self._key_hash(
            spool.run_id, f"{spool.task_id}\x00{spool.stream}", spool.attempt
        )
        self._write_bytes_atomic(base.with_suffix(".raw"), spool.read_retained())
        # The closing excerpt is durable beside the spool it belongs to, so a
        # second process recovers the child's tail even though retention dropped
        # it. It is written independently of the retained prefix.
        self._write_bytes_atomic(base.with_suffix(".closing"), spool.closing_tail())
        self._write_json_atomic(base.with_suffix(".json"), spool.persisted_state())

    # -- identity -----------------------------------------------------------

    def _keys(self) -> set[tuple[str, str, int]]:
        return set(self._spools) | set(self._facts)

    def _resolve_key(
        self,
        *,
        run_id: Optional[str],
        task_id: Optional[str],
        attempt: int,
        create: bool,
        default_task: str = "task-1",
    ) -> tuple[str, str, int]:
        """Resolve the compound identity, refusing ambiguity or mismatch."""
        attempt = int(attempt)
        if run_id is None:
            candidates = [k for k in self._keys() if k[2] == attempt]
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise StreamIdentityError(
                    f"attempt {attempt} is ambiguous across identities "
                    f"{sorted(candidates)}; name the run_id and task_id"
                )
            raise StreamError(f"attempt {attempt} has not been begun")
        exact = (run_id, task_id or default_task, attempt)
        if exact in self._keys():
            return exact
        candidates = [k for k in self._keys() if k[0] == run_id and k[2] == attempt]
        if task_id is None:
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise StreamIdentityError(
                    f"attempt {attempt} of run {run_id} is registered under "
                    f"tasks {sorted({k[1] for k in candidates})}; name task_id"
                )
            if create:
                return exact
            raise StreamError(f"attempt {attempt} of run {run_id} has not been begun")
        if candidates:
            raise StreamIdentityError(
                f"attempt {attempt} of run {run_id} is registered under task "
                f"{sorted({k[1] for k in candidates})}, not {task_id!r}; refusing "
                "to reuse one attempt slot for a different task"
            )
        if create:
            return exact
        raise StreamError(f"attempt {attempt} of run {run_id} has not been begun")

    # -- attempt lifecycle --------------------------------------------------

    def begin_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        task_id: str = "task-1",
        retain_limit: Optional[int] = None,
    ) -> dict[str, StreamSpool]:
        """Open (or return) the stdout/stderr spools of ``(run_id, task_id, attempt)``.

        Reusing the same compound identity returns the existing spools. A
        different task_id under the same run/attempt is refused, so one attempt
        slot can never silently mix two tasks.
        """
        key = self._resolve_key(
            run_id=run_id, task_id=task_id, attempt=attempt, create=True
        )
        limit = self.retain_limit if retain_limit is None else retain_limit
        streams = self._spools.setdefault(key, {})
        was_empty = not streams
        for name in (StreamName.STDOUT, StreamName.STDERR):
            if name.value not in streams:
                streams[name.value] = StreamSpool(
                    run_id=key[0],
                    task_id=key[1],
                    attempt=key[2],
                    stream=name.value,
                    retain_limit=limit,
                    max_frames=self.max_frames,
                )
        if was_empty:
            self._persist_identity(key)
        return dict(streams)

    def _persist_identity(self, key: tuple[str, str, int]) -> None:
        # Persist both spools of a freshly begun identity immediately so a
        # second process sees the identity even before any bytes arrive.
        for spool in self._spools.get(key, {}).values():
            self._persist_spool(spool)

    def ingest(
        self,
        *,
        attempt: int,
        stream: str,
        chunk: bytes,
        at: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> StreamFrame:
        """Append an observed ``chunk`` and return its ordered frame.

        The bytes are durable before this returns (when disk-backed). The frame
        cursor is committed as it is assigned, so a live reader in another
        process sees progress as it happens.
        """
        key = self._resolve_key(
            run_id=run_id, task_id=task_id, attempt=attempt, create=run_id is not None
        )
        spool = self._spools.get(key, {}).get(stream)
        if spool is None:
            raise StreamError(f"no stream '{stream}' for identity {key}")
        _delta, retained = spool.ingest(chunk)
        if _delta == 0:
            # Metadata is bounded: the bytes were hashed and counted but no new
            # frame is emitted. Persist the updated counts, return the last frame
            # so the caller's cursor bookkeeping still resolves.
            self._persist_spool(spool)
            existing = [f for f in self._log if f.stream == stream and (f.run_id, f.task_id, f.attempt) == key]
            return existing[-1]
        self._cursor += 1
        frame = StreamFrame(
            cursor=self._cursor,
            run_id=key[0],
            task_id=key[1],
            attempt=key[2],
            stream=stream,
            event_type=FRAME_CHUNK,
            data=retained,
            digest=hashlib.sha256(bytes(chunk)).hexdigest(),
            byte_length=len(chunk),
            at=at or _now(),
        )
        spool._note_frame(self._cursor)
        self._log.append(frame)
        self._append_json(self._frames_path, self._frame_to_json(frame))
        self._persist_spool(spool)
        self._commit_cursor()
        return frame

    def seal_attempt(
        self,
        *,
        attempt: int,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, str]:
        """Seal every stream of an attempt and freeze typed incompleteness."""
        key = self._resolve_key(run_id=run_id, task_id=task_id, attempt=attempt, create=False)
        result: dict[str, str] = {}
        for name in (StreamName.STDOUT, StreamName.STDERR):
            spool = self._spools.get(key, {}).get(name.value)
            if spool is None or spool.sealed:
                if spool is not None:
                    result[name.value] = spool.incompleteness.value
                continue
            incompleteness = spool.seal()
            result[name.value] = incompleteness
            self._cursor += 1
            frame = StreamFrame(
                cursor=self._cursor,
                run_id=key[0],
                task_id=key[1],
                attempt=key[2],
                stream=name.value,
                event_type=FRAME_SEALED,
                data=b"",
                digest=spool.digest,
                byte_length=spool.observed_bytes,
                at=_now(),
            )
            spool._note_frame(self._cursor)
            self._log.append(frame)
            self._append_json(self._frames_path, self._frame_to_json(frame))
            self._persist_spool(spool)
            self._commit_cursor()
        return result

    def record_fact(self, fact: AttemptFact) -> AttemptFact:
        """Record ``fact`` for its identity, refusing to overwrite a differing one."""
        key = identity_key(fact.run_id, fact.task_id, fact.attempt)
        existing = self._facts.get(key)
        if existing is not None and existing != fact:
            for attr in ("exit_code", "result_subtype", "duration_ns", "signal"):
                if getattr(existing, attr) != getattr(fact, attr):
                    raise StreamFactConflict(fact.attempt, attr)
            raise StreamFactConflict(fact.attempt, "fact")
        if existing == fact:
            return existing
        self._facts[key] = fact
        spool = self._spools.get(key, {}).get(StreamName.STDOUT.value)
        if spool is not None:
            self._cursor += 1
            frame = StreamFrame(
                cursor=self._cursor,
                run_id=fact.run_id,
                task_id=fact.task_id,
                attempt=fact.attempt,
                stream="",
                event_type=FRAME_FACT,
                data=b"",
                digest=hashlib.sha256(
                    json.dumps(fact.to_dict(), sort_keys=True).encode()
                ).hexdigest(),
                byte_length=0,
                at=fact.completed_at,
            )
            self._log.append(frame)
            self._append_json(self._frames_path, self._frame_to_json(frame))
            self._commit_cursor()
        self._append_json(self._facts_path, fact.to_dict())
        return fact

    def finish_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        task_id: str,
        exit_code: Optional[int],
        signal: Optional[int],
        termination: str,
        duration_ns: int,
        pid: int,
        started_at: str,
        completed_at: Optional[str] = None,
        interrupted: bool = False,
    ) -> AttemptFact:
        """Seal the streams and record the attempt's immutable collected fact.

        ``result_subtype`` is derived from the raw ``termination`` alone — it is
        never coerced upward to ``exited`` because the exit looked benign.
        """
        if interrupted:
            for name in (StreamName.STDOUT, StreamName.STDERR):
                spool = self._spools.get((run_id, task_id, attempt), {}).get(name.value)
                if spool is not None and not spool.sealed:
                    spool.mark_interrupted()
        self.seal_attempt(attempt=attempt, run_id=run_id, task_id=task_id)
        fact = AttemptFact(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            exit_code=exit_code,
            signal=signal,
            result_subtype=_subtype_for(termination),
            termination=termination,
            duration_ns=duration_ns,
            started_at=started_at,
            completed_at=completed_at or _now(),
            pid=pid,
        )
        return self.record_fact(fact)

    # -- cursor-based reads -------------------------------------------------

    @property
    def cursor(self) -> int:
        """The latest assigned cursor (0 before any frame)."""
        return self._cursor

    def frames(
        self,
        after_cursor: int = 0,
        *,
        limit: Optional[int] = None,
        attempt: Optional[int] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> list[StreamFrame]:
        """Return frames with ``cursor > after_cursor`` in order (cursor-based).

        This is the reconnect/replay surface: a consumer that stopped at cursor
        N calls again with ``after_cursor=N`` and receives exactly the frames it
        has not yet seen, in order, with no gap and no duplication.
        """
        out = [f for f in self._log if f.cursor > after_cursor]
        if run_id is not None:
            out = [f for f in out if f.run_id == run_id]
        if task_id is not None:
            out = [f for f in out if f.task_id == task_id]
        if attempt is not None:
            out = [f for f in out if f.attempt == attempt]
        if limit is not None:
            out = out[:limit]
        return out

    def resolve(self, cursor: int) -> bytes:
        """Return the retained bytes of the frame at ``cursor`` (b"" if none)."""
        for frame in self._log:
            if frame.cursor == cursor:
                return frame.data
        raise StreamError(f"no frame at cursor {cursor}")

    def summary(
        self,
        attempt: int,
        stream: str,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> StreamSummary:
        key = self._resolve_key(run_id=run_id, task_id=task_id, attempt=attempt, create=False)
        spool = self._spools.get(key, {}).get(stream)
        if spool is None:
            raise StreamError(f"no stream '{stream}' for identity {key}")
        return spool.summary()

    def fact(
        self,
        attempt: int,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[AttemptFact]:
        try:
            key = self._resolve_key(run_id=run_id, task_id=task_id, attempt=attempt, create=False)
        except StreamError:
            return None
        return self._facts.get(key)

    def attempts(self, run_id: Optional[str] = None) -> list[int]:
        keys = set(self._spools) | set(self._facts)
        if run_id is not None:
            keys = {k for k in keys if k[0] == run_id}
        return sorted({k[2] for k in keys})

    def identities(self) -> list[tuple[str, str, int]]:
        """Every compound identity the store holds, sorted."""
        return sorted(set(self._spools) | set(self._facts))

    def spool(
        self,
        attempt: int,
        stream: str,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> StreamSpool:
        """Return the spool for an identity/stream (raises if unbegun).

        A public accessor so the production collector never reaches into the
        store's private map; it is how the collector reads retained evidence
        bytes and sets a per-stream cap.
        """
        key = self._resolve_key(run_id=run_id, task_id=task_id, attempt=attempt, create=False)
        spool = self._spools.get(key, {}).get(stream)
        if spool is None:
            raise StreamError(f"no stream '{stream}' for identity {key}")
        return spool

    def set_retain_limit(
        self,
        attempt: int,
        stream: str,
        limit: int,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """Set a per-stream retention cap (used for a narrower stderr cap)."""
        if limit < 0:
            raise StreamError("retain_limit must be non-negative")
        spool = self.spool(attempt, stream, run_id=run_id, task_id=task_id)
        spool.retain_limit = limit
        self._persist_spool(spool)

    # -- the retained closing excerpt (SW-1511A-RESULT-001, F-1511A-01) -----

    def closing_excerpt(
        self,
        attempt: int,
        stream: str,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """The bounded *tail* of one stream, captured independently of retention.

        Retention keeps the head of a stream and drops the tail, so the child's
        closing message is precisely what a cap loses. This excerpt is kept at
        ingest time (see :meth:`StreamSpool._note_closing_tail`), so it survives
        the cap. It is reported as data, never as the evidence:

        * ``observed`` is explicitly ``False`` when no byte was observed at all,
          so a caller can never read a missing tail as an empty message;
        * ``byte_range_start``/``byte_range_end`` locate the excerpt exactly in
          the observed byte sequence (``end`` equals ``observed_bytes``);
        * ``digest`` is the sha256 over *this excerpt's* bytes, so the bound
          record can be checked against the durable excerpt text;
        * secret-shaped content is scrubbed in ``text`` (and the digest is taken
          over that scrubbed text), through the shared
          :mod:`skillweave.redaction` rule set. The spool's durable bytes stay
          raw evidence, exactly as the retained prefix does; it is the excerpt
          *handed out* here that is safe to persist into a human-readable
          record, so a credential the child printed in its closing line never
          leaves this method.
        """
        spool = self.spool(attempt, stream, run_id=run_id, task_id=task_id)
        tail = spool.closing_tail()
        start, end = spool.closing_byte_range()
        observed = bool(tail)
        text = scrub_secrets(tail.decode("utf-8", errors="replace"))
        scrubbed = text.encode("utf-8")
        return {
            "attempt": attempt,
            "stream": stream,
            "observed": observed,
            "text": text,
            "bytes": len(scrubbed),
            "digest": hashlib.sha256(scrubbed).hexdigest() if observed else "",
            "byte_range_start": start,
            "byte_range_end": end,
            "observed_bytes": spool.observed_bytes,
            "note": (
                "tail of the observed stream"
                if observed
                else "no bytes were observed on this stream"
            ),
        }

    # -- preview (never the raw evidence) -----------------------------------

    def preview(
        self,
        *,
        attempt: int,
        stream: str = StreamName.STDOUT.value,
        max_bytes: int = 1024,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return a bounded, explicitly-truncated user preview of one stream.

        The preview is *decoration over evidence*, never the evidence. It always
        carries ``is_full_evidence=False`` and the observed/retained counts, so a
        consumer can never mistake a preview limit for the whole raw stream, and
        can always resolve the raw frames through :meth:`frames`/:meth:`resolve`.
        """
        if max_bytes < 0:
            raise StreamError("max_bytes must be non-negative")
        spool = self.spool(attempt, stream, run_id=run_id, task_id=task_id)
        retained = spool.read_retained()
        prefix = retained[:max_bytes]
        summary = spool.summary()
        truncated = summary.observed_bytes > len(prefix)
        return {
            "attempt": attempt,
            "stream": stream,
            "text": prefix.decode("utf-8", errors="replace"),
            "preview_bytes": len(prefix),
            "retained_bytes": summary.retained_bytes,
            "observed_bytes": summary.observed_bytes,
            "digest": summary.digest,
            "incompleteness": summary.incompleteness,
            "is_truncated": truncated,
            "is_full_evidence": False,
            "evidence_cursor_start": summary.cursor_start,
            "evidence_cursor_end": summary.cursor_end,
        }

    # -- frozen-contract export ---------------------------------------------

    def to_attempt_events(
        self,
        *,
        attempt: int,
        issuer: str,
        principal: str,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Export this attempt's frames as ``attempt-event-v1`` dicts.

        Each event is a collected fact: ``event_id`` names the host-observed
        cursor, ``sequence`` is the monotonic per-run sequence, ``issuer`` and
        ``principal`` name the collecting authority (the schema can express but
        cannot authenticate them), and ``evidence_digest`` is the exact content
        digest of the observed chunk. No model-authored string is trusted: this
        is the collector's own record.
        """
        events: list[dict[str, Any]] = []
        seq = 0
        for frame in self._log:
            if frame.attempt != attempt:
                continue
            if run_id is not None and frame.run_id != run_id:
                continue
            if task_id is not None and frame.task_id != task_id:
                continue
            seq += 1
            rid = run_id or frame.run_id
            events.append(
                {
                    "schema_version": "1.0.0",
                    "run_id": rid,
                    "task_id": task_id or frame.task_id,
                    "attempt": frame.attempt,
                    "event_id": f"{rid}:{frame.cursor}",
                    "sequence": seq,
                    "event_type": frame.event_type,
                    "measured_at": frame.at,
                    "issuer": issuer,
                    "principal": principal,
                    "evidence_digest": frame.digest,
                    "payload": {
                        "stream": frame.stream,
                        "cursor": frame.cursor,
                        "byte_length": frame.byte_length,
                        "retained": len(frame.data),
                    },
                }
            )
        return events

    # -- nested-worker events -----------------------------------------------

    def nested_events(
        self,
        attempt: int,
        *,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        max_bytes: int = 1024 * 1024,
        max_events: int = 4096,
    ) -> list[dict[str, Any]]:
        """Parse nested-worker event lines from the attempt's stdout stream.

        A worker that emits structured progress as one JSON object per line has
        those events recovered here as data, in cursor order. A line that is not
        a JSON object is skipped — it stays raw stdout evidence and is never
        invented into an event. Parsing is bounded (``max_bytes``/``max_events``)
        so it is a streamed scan, not an unbounded duplicate blob.
        """
        try:
            spool = self.spool(attempt, StreamName.STDOUT.value, run_id=run_id, task_id=task_id)
        except StreamError:
            return []
        events: list[dict[str, Any]] = []
        retained = spool.read_retained()[:max_bytes]
        text = retained.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if len(events) >= max_events:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events


def _subtype_for(termination: str) -> str:
    """Map a raw ``termination`` to its result subtype, un-coerced.

    An unrecognised termination is *not* silently mapped to success; it is
    reported verbatim so a consumer sees the real, unexpected shape.
    """
    mapping = {
        "exited": ResultSubtype.EXITED.value,
        "signaled": ResultSubtype.SIGNALED.value,
        "cancelled": ResultSubtype.CANCELLED.value,
        "timed_out": ResultSubtype.TIMED_OUT.value,
        "launch_failed": ResultSubtype.LAUNCH_FAILED.value,
        "storage_failed": ResultSubtype.STORAGE_FAILED.value,
    }
    return mapping.get(termination, termination)


__all__ = [
    "AttemptFact",
    "DEFAULT_CLOSING_EXCERPT_LIMIT",
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_RETAIN_LIMIT",
    "DurableStreamStore",
    "FRAME_CHUNK",
    "FRAME_FACT",
    "FRAME_SEALED",
    "Incompleteness",
    "PersistenceMode",
    "ResultSubtype",
    "StreamError",
    "StreamFactConflict",
    "StreamFrame",
    "StreamIdentityError",
    "StreamName",
    "StreamPersistenceError",
    "StreamSealedError",
    "StreamSpool",
    "StreamSummary",
    "identity_key",
]
