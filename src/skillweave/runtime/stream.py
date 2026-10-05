"""Durable per-attempt output stream and collected process facts (SW-1510-STREAM).

This module is the durable evidence channel for a worker attempt. It records
two *distinct* things and never lets the second stand in for the first:

* **The stream** — the per-attempt, per-stream (``stdout``/``stderr``) bytes a
  real child produced, spooled under a single monotonic, interleaved cursor with
  an exact sha256 content digest. A user-facing *preview* is a bounded,
  explicitly-truncated decoration over this evidence; it is never the evidence
  itself. A preview limit never replaces the raw evidence.
* **The facts** — the observed exit code, the *measured* monotonic duration, and
  the raw result subtype (``exited``/``signaled``/``cancelled``/``timed_out``/
  ``launch_failed``). These are collected facts: immutable once recorded. An
  error is never coerced to success and a measured duration is never clipped or
  fabricated.

Retention is bounded. A per-stream byte cap bounds the bytes *retained* as
evidence; backpressure is the observable pressure applied when a stream reaches
that cap; and the exact digest and observed byte count remain truthful about
everything the child actually wrote. Typed incompleteness (``complete``,
``limit_reached``, ``interrupted``, ``incomplete``) is data — never a silent
prefix mistaken for the whole stream.

The module owns no process spawning and no authority. It is a store; the
production spawn/collection path in ``skillweave.core.proc.runner`` feeds it and
the run application service binds its output into the run's evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

#: Default number of bytes retained per stream as evidence (1 MiB), matching
#: the runner's historical output ceiling. Retaining is bounded independently of
#: how much the child writes; the exact digest still covers every observed byte.
DEFAULT_RETAIN_LIMIT = 1024 * 1024

#: Event kinds carried by a durable stream frame.
FRAME_CHUNK = "chunk"
FRAME_SEALED = "sealed"
FRAME_FACT = "fact"


class StreamName(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"


class Incompleteness(str, Enum):
    """How completely a stream's retained bytes represent what was observed.

    ``incomplete`` is the open state before a stream is sealed. ``complete``
    means every observed byte was retained. ``limit_reached`` means the retainer
    hit its cap: the retained prefix and the digest are truthful about what was
    seen, but the retained bytes are *not* the whole stream. ``interrupted``
    means collection stopped before the child's stream was fully drained (for
    example the child was cancelled or timed out mid-write).
    """

    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    LIMIT_REACHED = "limit_reached"
    INTERRUPTED = "interrupted"


class ResultSubtype(str, Enum):
    """The raw, un-coerced way an attempt ended."""

    EXITED = "exited"
    SIGNALED = "signaled"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LAUNCH_FAILED = "launch_failed"


class StreamError(Exception):
    """Base error for the durable stream store."""


class StreamFactConflict(StreamError):
    """Raised when an attempt's collected fact would be overwritten.

    Collected facts are immutable: a second, differing ``record_fact`` for the
    same attempt is refused rather than silently replacing the recorded exit
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StreamSpool:
    """One stream (stdout or stderr) of one attempt.

    Retains at most ``retain_limit`` bytes as evidence while hashing *every*
    observed byte. When the retainer is at its cap and more bytes arrive, the
    spool records the pressure (``backpressure_events``) and marks itself
    ``limit_reached`` — it keeps counting and hashing so the digest and observed
    count stay truthful, but it does not keep growing.
    """

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        stream: str,
        retain_limit: int = DEFAULT_RETAIN_LIMIT,
    ) -> None:
        if retain_limit < 0:
            raise StreamError("retain_limit must be non-negative")
        self.run_id = run_id
        self.task_id = task_id
        self.attempt = attempt
        self.stream = stream
        self.retain_limit = retain_limit
        self._retained = bytearray()
        self._hasher = hashlib.sha256()
        self._observed = 0
        self._frame_count = 0
        self._cursor_start: Optional[int] = None
        self._cursor_end: int = 0
        self._limited = False
        self._sealed = False
        self._interrupted = False
        self.incompleteness = Incompleteness.INCOMPLETE
        self.backpressure_events = 0

    # -- ingestion ----------------------------------------------------------

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
        """Exact sha256 over every observed byte (not just the retained prefix)."""
        return self._hasher.hexdigest()

    @property
    def retained_digest(self) -> str:
        return hashlib.sha256(bytes(self._retained)).hexdigest()

    @property
    def is_limited(self) -> bool:
        return self._limited

    def ingest(self, chunk: bytes) -> tuple[int, bytes]:
        """Account for an observed ``chunk``.

        Returns ``(frame_delta, retained_slice)``. Every observed byte updates
        the digest; only up to the retain limit is kept. The retained slice is
        what the caller attaches to the ordered frame.
        """
        if self._sealed:
            raise StreamSealedError(
                f"stream '{self.stream}' of attempt {self.attempt} is sealed"
            )
        if not chunk:
            return 0, b""
        self._observed += len(chunk)
        self._hasher.update(chunk)
        room = self.retain_limit - len(self._retained)
        if room > 0:
            retained = bytes(chunk[:room])
            self._retained.extend(retained)
            if len(chunk) > room:
                self._limited = True
        else:
            retained = b""
            self._limited = True
            # The retainer is full and more bytes arrived: this is the point at
            # which pressure is applied. It is observable, not silent.
            self.backpressure_events += 1
        return 1, retained

    def mark_interrupted(self) -> None:
        """Record that collection stopped before the stream was fully drained."""
        self._interrupted = True

    def seal(self) -> str:
        """Close the spool and freeze its typed incompleteness."""
        if self._sealed:
            return self.incompleteness
        self._sealed = True
        if self._interrupted:
            self.incompleteness = Incompleteness.INTERRUPTED
        elif self._limited:
            self.incompleteness = Incompleteness.LIMIT_REACHED
        else:
            self.incompleteness = Incompleteness.COMPLETE
        return self.incompleteness

    def summary(self) -> StreamSummary:
        start = self._cursor_start if self._cursor_start is not None else 0
        return StreamSummary(
            stream=self.stream,
            digest=self.digest,
            retained_bytes=len(self._retained),
            retained_digest=self.retained_digest,
            observed_bytes=self._observed,
            incompleteness=self.incompleteness.value,
            frame_count=self._frame_count,
            cursor_start=start,
            cursor_end=self._cursor_end,
            backpressure_events=self.backpressure_events,
        )

    def read_retained(self) -> bytes:
        return bytes(self._retained)

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
    immutable :class:`AttemptFact` records. Reads are cursor-based, so a
    reconnecting consumer resumes exactly where it left off.
    """

    def __init__(self, retain_limit: int = DEFAULT_RETAIN_LIMIT) -> None:
        if retain_limit < 0:
            raise StreamError("retain_limit must be non-negative")
        self.retain_limit = retain_limit
        self._spools: dict[tuple[int, str], StreamSpool] = {}
        self._facts: dict[int, AttemptFact] = {}
        self._log: list[StreamFrame] = []
        self._cursor = 0

    # -- attempt lifecycle --------------------------------------------------

    def begin_attempt(
        self,
        *,
        run_id: str,
        attempt: int,
        task_id: str = "task-1",
        retain_limit: Optional[int] = None,
    ) -> dict[str, StreamSpool]:
        """Open (or return) the stdout/stderr spools of ``attempt``."""
        limit = self.retain_limit if retain_limit is None else retain_limit
        spools: dict[str, StreamSpool] = {}
        for name in (StreamName.STDOUT, StreamName.STDERR):
            key = (attempt, name.value)
            spool = self._spools.get(key)
            if spool is None:
                spool = StreamSpool(
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    stream=name.value,
                    retain_limit=limit,
                )
                self._spools[key] = spool
            spools[name.value] = spool
        return spools

    def ingest(
        self,
        *,
        attempt: int,
        stream: str,
        chunk: bytes,
        at: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> StreamFrame:
        """Append an observed ``chunk`` and return its ordered frame."""
        key = (attempt, stream)
        spool = self._spools.get(key)
        if spool is None:
            if run_id is None:
                raise StreamError(
                    f"attempt {attempt} stream '{stream}' has not been begun"
                )
            spool = StreamSpool(
                run_id=run_id,
                task_id="task-1",
                attempt=attempt,
                stream=stream,
                retain_limit=self.retain_limit,
            )
            self._spools[key] = spool
        _delta, retained = spool.ingest(chunk)
        self._cursor += 1
        cursor = self._cursor
        frame = StreamFrame(
            cursor=cursor,
            run_id=spool.run_id,
            task_id=spool.task_id,
            attempt=attempt,
            stream=stream,
            event_type=FRAME_CHUNK,
            data=retained,
            digest=hashlib.sha256(bytes(chunk)).hexdigest(),
            byte_length=len(chunk),
            at=at or _now(),
        )
        spool._note_frame(cursor)
        self._log.append(frame)
        return frame

    def seal_attempt(self, attempt: int) -> dict[str, str]:
        """Seal every stream of ``attempt`` and freeze typed incompleteness."""
        result: dict[str, str] = {}
        for name in (StreamName.STDOUT, StreamName.STDERR):
            spool = self._spools.get((attempt, name.value))
            if spool is None:
                continue
            incompleteness = spool.seal()
            result[name.value] = incompleteness
            self._cursor += 1
            frame = StreamFrame(
                cursor=self._cursor,
                run_id=spool.run_id,
                task_id=spool.task_id,
                attempt=attempt,
                stream=name.value,
                event_type=FRAME_SEALED,
                data=b"",
                digest=spool.digest,
                byte_length=spool.observed_bytes,
                at=_now(),
            )
            spool._note_frame(self._cursor)
            self._log.append(frame)
        return result

    def record_fact(self, fact: AttemptFact) -> AttemptFact:
        """Record ``fact`` for its attempt, refusing to overwrite a differing one."""
        existing = self._facts.get(fact.attempt)
        if existing is not None and existing != fact:
            for attr in ("exit_code", "result_subtype", "duration_ns", "signal"):
                if getattr(existing, attr) != getattr(fact, attr):
                    raise StreamFactConflict(fact.attempt, attr)
            raise StreamFactConflict(fact.attempt, "fact")
        self._facts[fact.attempt] = fact
        spool = self._spools.get((fact.attempt, StreamName.STDOUT.value))
        if spool is not None:
            self._cursor += 1
            self._log.append(
                StreamFrame(
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
            )
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
                spool = self._spools.get((attempt, name.value))
                if spool is not None:
                    spool.mark_interrupted()
        self.seal_attempt(attempt)
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
    ) -> list[StreamFrame]:
        """Return frames with ``cursor > after_cursor`` in order (cursor-based).

        This is the reconnect/replay surface: a consumer that stopped at cursor
        N calls again with ``after_cursor=N`` and receives exactly the frames it
        has not yet seen, in order, with no gap and no duplication.
        """
        out = [f for f in self._log if f.cursor > after_cursor]
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

    def summary(self, attempt: int, stream: str) -> StreamSummary:
        spool = self._spools.get((attempt, stream))
        if spool is None:
            raise StreamError(f"no stream '{stream}' for attempt {attempt}")
        return spool.summary()

    def fact(self, attempt: int) -> Optional[AttemptFact]:
        return self._facts.get(attempt)

    def attempts(self) -> list[int]:
        return sorted({a for (a, _s) in self._spools})

    def spool(self, attempt: int, stream: str) -> StreamSpool:
        """Return the spool for ``attempt``/``stream`` (raises if unbegun).

        A public accessor so the production collector never reaches into the
        store's private ``_spools`` map; it is how the collector reads the
        retained evidence bytes and sets a per-stream cap.
        """
        spool = self._spools.get((attempt, stream))
        if spool is None:
            raise StreamError(f"no stream '{stream}' for attempt {attempt}")
        return spool

    def set_retain_limit(self, attempt: int, stream: str, limit: int) -> None:
        """Set a per-stream retention cap (used for a narrower stderr cap)."""
        if limit < 0:
            raise StreamError("retain_limit must be non-negative")
        self.spool(attempt, stream).retain_limit = limit

    # -- preview (never the raw evidence) -----------------------------------

    def preview(
        self,
        *,
        attempt: int,
        stream: str = StreamName.STDOUT.value,
        max_bytes: int = 1024,
    ) -> dict[str, Any]:
        """Return a bounded, explicitly-truncated user preview of one stream.

        The preview is *decoration over evidence*, never the evidence. It always
        carries ``is_full_evidence=False`` and the observed/retained counts, so a
        consumer can never mistake a preview limit for the whole raw stream, and
        can always resolve the raw frames through :meth:`frames`/:meth:`resolve`.
        """
        if max_bytes < 0:
            raise StreamError("max_bytes must be non-negative")
        spool = self._spools.get((attempt, stream))
        if spool is None:
            raise StreamError(f"no stream '{stream}' for attempt {attempt}")
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

    def nested_events(self, attempt: int) -> list[dict[str, Any]]:
        """Parse nested-worker event lines from the attempt's stdout stream.

        A worker that emits structured progress as one JSON object per line
        ("nested events") has those events recovered here as data, in cursor
        order. A line that is not a JSON object is skipped — it stays raw stdout
        evidence and is never invented into an event.
        """
        spool = self._spools.get((attempt, StreamName.STDOUT.value))
        if spool is None:
            return []
        events: list[dict[str, Any]] = []
        text = spool.read_retained().decode("utf-8", errors="replace")
        for line in text.splitlines():
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
    }
    return mapping.get(termination, termination)


__all__ = [
    "AttemptFact",
    "DEFAULT_RETAIN_LIMIT",
    "DurableStreamStore",
    "FRAME_CHUNK",
    "FRAME_FACT",
    "FRAME_SEALED",
    "Incompleteness",
    "ResultSubtype",
    "StreamError",
    "StreamFactConflict",
    "StreamFrame",
    "StreamName",
    "StreamSealedError",
    "StreamSpool",
    "StreamSummary",
]
