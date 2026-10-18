"""Owned-attempt liveness: nine declared states from real progress evidence.

``SW-1511A-LIVENESS-001`` (``attempt-liveness/v1``).  The telemetry contract's
producer is the *native process watcher*: the supervision path around an owned
attempt records, and keeps for a watcher restart, what the attempt is actually
doing.  A scheduler or watchdog reads those observations and decides; this
module never decides to stop anything.

Two properties are load-bearing and are the reason this module exists rather
than an age or byte counter:

* **Output-byte growth is not progress.**  An attempt's stdout is retained
  under a hard cap (the head is kept, the tail dropped) so ``observed_bytes``
  can grow without bound while the retained prefix is frozen.  Classifying on
  byte growth therefore reports a self-matching wait loop as *working* forever
  and is blind exactly when the stream is capped.  Progress is derived from
  *semantic* signals -- advancing structured events (``nested_events``), tool
  activity, the owned process identity -- and byte counts are carried only as
  context, never as the progress test.
* **A repeated signal is not progress.**  A wait loop that re-emits one
  identical event (the *self-matching* loop) is collapsed to a single progress
  signature: N copies of one event are one event.  A loop whose own output
  matches its progress pattern is therefore classified ``stalled`` instead of
  masquerading as advancing work.

The classification vocabulary is closed and the states are *distinct*:
``working``, ``waiting_for_owner``, ``waiting_for_quota``, ``stalled``,
``provider_error``, ``auth_error``, ``transport_error``, ``cancelled``,
``completed``.  ``unknown`` is the explicit *cannot-classify* value, never a
guess: an unknown metric stays ``None`` (the contract's compatibility rule) and
a state is only named when the evidence supports it.

Availability failures are read from the *declared* typed markers in the
retained stderr evidence (the gateway's own words: ``401``, ``429``,
``502 Bad Gateway``, a transport timeout), so the same statuses the upstream
observation surface distinguishes stay distinct here, and a *repeated* series
of one status accumulates into one causal series (``caused_by`` links each
observation to its predecessor) that a watcher restart continues rather than
resets.

Nothing here signals a process; the module only observes.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

__all__ = [
    "ATTEMPT_LIVENESS_SCHEMA_ID",
    "ATTEMPT_LIVENESS_SCHEMA_VERSION",
    "LIVENESS_STATES",
    "STALL_SAMPLES",
    "LivenessState",
    "LivenessError",
    "LivenessObservation",
    "ProgressEvidence",
    "LivenessWatcher",
    "AttemptLivenessStore",
    "LivenessRecorder",
    "classify",
    "explain",
    "derive_progress_evidence",
    "gather_stream_evidence",
    "liveness_root_for",
    "record_attempt_liveness",
    "read_attempt_liveness",
    "read_attempt_liveness_series",
    "progress_signature",
]

ATTEMPT_LIVENESS_SCHEMA_ID = (
    "attempt-liveness/v1 (skillweave // SW-1511A-LIVENESS-001)"
)
ATTEMPT_LIVENESS_SCHEMA_VERSION = "1.0.0"

#: How many consecutive non-progressing in-flight samples name a stall.  Two is
#: the smallest count that distinguishes "just launched, no output yet" from a
#: loop that has had a full sampling interval to produce something.
STALL_SAMPLES = 2

#: Volatile event keys removed before an event is reduced to a progress
#: signature: a loop that only advances a clock or a counter is *not*
#: progressing, so those fields must not create a new signature.
_VOLATILE_KEYS = frozenset(
    {
        "ts", "time", "at", "timestamp", "elapsed", "elapsed_ms", "duration",
        "duration_ms", "seq", "sequence", "n", "i", "index", "attempt",
        "counter", "retries", "retry", "poll", "loop", "pid", "iteration",
    }
)

#: Declared typed markers for the availability statuses, read from the retained
#: evidence.  Order matters: a definitive authentication failure outranks a
#: retryable provider failure, and a rate limit is a *wait*, not an error.
_AUTH_MARKERS = (
    "401", "403", "unauthorized", "unauthenticated", "invalid_api_key",
    "invalid api key", "authentication", "permission denied",
)
_QUOTA_MARKERS = (
    "429", "rate limit", "rate_limit", "ratelimit", "too many requests",
    "quota", "insufficient_quota", "resource_exhausted", "capacity",
)
_TRANSPORT_MARKERS = (
    "timed out", "timeout", "connection reset", "connection refused",
    "connection aborted", "name or service not known", "nodename nor servname",
    "temporary failure in name resolution", "dns", "unreachable",
    "transport error", "broken pipe", "eof occurred",
)
_PROVIDER_MARKERS = (
    "502", "503", "504", "bad gateway", "service unavailable",
    "gateway timeout", "internal server error", "500",
    "provider error", "upstream error", "upstream_error", "overloaded",
)


class LivenessError(Exception):
    """A liveness observation could not be produced or read back."""


class LivenessState(str, Enum):
    """The closed classification vocabulary (``unknown`` = cannot classify)."""

    WORKING = "working"
    WAITING_FOR_OWNER = "waiting_for_owner"
    WAITING_FOR_QUOTA = "waiting_for_quota"
    STALLED = "stalled"
    PROVIDER_ERROR = "provider_error"
    AUTH_ERROR = "auth_error"
    TRANSPORT_ERROR = "transport_error"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


#: The nine classifiable states, in the contract's declared order.
LIVENESS_STATES: Tuple[str, ...] = (
    LivenessState.WORKING.value,
    LivenessState.WAITING_FOR_OWNER.value,
    LivenessState.WAITING_FOR_QUOTA.value,
    LivenessState.STALLED.value,
    LivenessState.PROVIDER_ERROR.value,
    LivenessState.AUTH_ERROR.value,
    LivenessState.TRANSPORT_ERROR.value,
    LivenessState.CANCELLED.value,
    LivenessState.COMPLETED.value,
)

#: States that are an availability *wait* rather than an error: a legitimate
#: quota wait is preserved as a wait and is never folded into a failure.
WAIT_STATES = frozenset(
    {LivenessState.WAITING_FOR_OWNER.value, LivenessState.WAITING_FOR_QUOTA.value}
)

#: States that name an availability *failure*.
ERROR_STATES = frozenset(
    {
        LivenessState.PROVIDER_ERROR.value,
        LivenessState.AUTH_ERROR.value,
        LivenessState.TRANSPORT_ERROR.value,
    }
)


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def progress_signature(event: Any) -> str:
    """Reduce one structured event to a signature with volatiles removed.

    A loop that re-emits one event with only a changing clock or counter yields
    one signature, so it is one piece of progress, not many.
    """
    if isinstance(event, dict):
        stable = {k: v for k, v in event.items() if k not in _VOLATILE_KEYS}
    else:
        stable = event
    return json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)


def _advancing_signatures(
    events: Sequence[Any], already_seen: Sequence[str] = ()
) -> int:
    """How many event signatures are *new* relative to *already_seen*."""
    seen = set(already_seen)
    advancing = 0
    for event in events:
        signature = progress_signature(event)
        if signature in seen:
            continue
        seen.add(signature)
        advancing += 1
    return advancing


def _is_self_matching(events: Sequence[Any]) -> bool:
    """Whether a batch of events collapses to at most one distinct signature.

    A wait loop that re-emits one event (its only variations are the clock and
    the counter, which :func:`progress_signature` strips) yields a *single*
    distinct signature no matter how many lines it produced, so it is one
    piece of output, not progress.  Two or more distinct signatures is real
    variety and is not self-matching.
    """
    if not events:
        return False
    signatures = {progress_signature(event) for event in events}
    return len(signatures) <= 1


# ---------------------------------------------------------------------------
# Progress evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressEvidence:
    """The typed facts a classification is derived from.

    Every metric is *optional*: an unobservable metric stays ``None`` (the
    contract's compatibility rule -- unknown metrics remain null) and is never
    defaulted to a value that would silently reinterpret the record.  Byte
    counts are carried as context only; ``progress_observed`` is derived from
    semantic signals, never from ``observed_bytes``.
    """

    # -- context: byte counts, never the progress test ---------------------
    observed_bytes: Optional[int] = None
    retained_bytes: Optional[int] = None
    # -- semantic progress -------------------------------------------------
    semantic_events: Optional[int] = None
    advancing_events: Optional[int] = None
    self_matching: Optional[bool] = None
    tool_activity: Optional[int] = None
    # -- owned process identity -------------------------------------------
    owned_identity_verified: Optional[bool] = None
    owned_alive: Optional[bool] = None
    # -- declared wait / failure classification ---------------------------
    wait_class: Optional[str] = None
    failure_class: Optional[str] = None
    # -- recorded terminal outcome (a semantic fact, not an inference) -----
    terminated: Optional[bool] = None
    result_subtype: Optional[str] = None
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    # -- supervision state -------------------------------------------------
    non_progress_samples: int = 0
    detail: str = ""

    @property
    def output_observed(self) -> bool:
        """Whether *anything at all* has been observed (bytes or events)."""
        for value in (
            self.observed_bytes, self.semantic_events, self.tool_activity,
        ):
            if value is not None and value > 0:
                return True
        return bool(self.self_matching)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_bytes": self.observed_bytes,
            "retained_bytes": self.retained_bytes,
            "semantic_events": self.semantic_events,
            "advancing_events": self.advancing_events,
            "self_matching": self.self_matching,
            "tool_activity": self.tool_activity,
            "owned_identity_verified": self.owned_identity_verified,
            "owned_alive": self.owned_alive,
            "wait_class": self.wait_class,
            "failure_class": self.failure_class,
            "terminated": self.terminated,
            "result_subtype": self.result_subtype,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "non_progress_samples": self.non_progress_samples,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProgressEvidence":
        if not isinstance(data, dict):
            return cls()
        return cls(
            observed_bytes=_optional_int(data.get("observed_bytes")),
            retained_bytes=_optional_int(data.get("retained_bytes")),
            semantic_events=_optional_int(data.get("semantic_events")),
            advancing_events=_optional_int(data.get("advancing_events")),
            self_matching=_optional_bool(data.get("self_matching")),
            tool_activity=_optional_int(data.get("tool_activity")),
            owned_identity_verified=_optional_bool(
                data.get("owned_identity_verified")),
            owned_alive=_optional_bool(data.get("owned_alive")),
            wait_class=data.get("wait_class"),
            failure_class=data.get("failure_class"),
            terminated=_optional_bool(data.get("terminated")),
            result_subtype=data.get("result_subtype"),
            exit_code=_optional_int(data.get("exit_code")),
            signal=_optional_int(data.get("signal")),
            non_progress_samples=int(data.get("non_progress_samples") or 0),
            detail=str(data.get("detail") or ""),
        )

    def progress_observed(self) -> Optional[bool]:
        """Whether semantic progress was observed (``None`` when unknowable).

        Tool activity is progress.  A self-matching event series -- every
        retained line carrying the same stable signature, the wait loop's
        signature -- is *not* progress even though its byte count keeps growing,
        so a self-match returns ``False`` outright.  Otherwise the advancing
        count is the test: at least one signature that is new relative to what
        was already seen.
        """
        if self.tool_activity is not None and self.tool_activity > 0:
            return True
        if self.self_matching:
            # The series collapses to one signature: output without progress.
            return False
        if self.advancing_events is not None:
            return self.advancing_events > 0
        if self.semantic_events is not None and self.semantic_events > 0:
            return True
        return None


def classify(evidence: ProgressEvidence) -> LivenessState:
    """Classify one sample.  ``unknown`` when the evidence does not support a state."""
    return _classify(evidence)[0]


def explain(evidence: ProgressEvidence) -> Tuple[LivenessState, str]:
    """Classify one sample and return the state with its declared reason."""
    return _classify(evidence)


def _classify(evidence: ProgressEvidence) -> Tuple[LivenessState, str]:
    # 1. Recorded terminal outcomes are semantic facts and outrank every
    #    inference.  A cancelled/timed-out owned attempt is a cancellation; a
    #    clean exit is completed; a failed exit falls through to the availability
    #    classification so its *reason* is still named.
    subtype = (evidence.result_subtype or "").strip().lower()
    if evidence.terminated:
        if subtype == "cancelled":
            return (
                LivenessState.CANCELLED,
                "the attempt was cancelled while owned",
            )
        if subtype == "timed_out":
            return (
                LivenessState.CANCELLED,
                "the owned attempt was cancelled by its supervisor deadline",
            )
        if evidence.failure_class is None and subtype in ("exited", "completed", ""):
            if evidence.exit_code == 0:
                return (
                    LivenessState.COMPLETED,
                    "the attempt completed: the child exited 0",
                )
        if evidence.failure_class is None and evidence.exit_code == 0:
            return (
                LivenessState.COMPLETED,
                "the attempt completed: the child exited 0",
            )

    # 2. Declared availability statuses, most specific first.  A definitive
    #    authentication failure is not a retryable provider failure.
    failure = (evidence.failure_class or "").strip().lower()
    if failure in ("auth", "authentication", "auth_error"):
        return LivenessState.AUTH_ERROR, "the upstream refused the credential"
    if failure in ("transport", "transport_error", "dns", "network"):
        return (
            LivenessState.TRANSPORT_ERROR,
            "the upstream was unreachable at the transport layer",
        )
    if failure in ("provider", "provider_error", "upstream"):
        return LivenessState.PROVIDER_ERROR, "the upstream answered with an error"

    # 3. A declared wait is preserved as a wait -- a legitimate quota wait is
    #    never reclassified as a stall or a failure.
    wait = (evidence.wait_class or "").strip().lower()
    if wait in ("owner", "waiting_for_owner"):
        return LivenessState.WAITING_FOR_OWNER, "the attempt waits for its owner"
    if wait in ("quota", "waiting_for_quota", "rate_limit"):
        return (
            LivenessState.WAITING_FOR_QUOTA,
            "the attempt waits for quota; the wait is legitimate",
        )

    # 4. Process identity: an owned process that is no longer alive without a
    #    recorded outcome is *unknown* in-flight, never a fabricated stall.  An
    #    identity that cannot be verified is likewise not classifiable.
    if evidence.owned_alive is False and not evidence.terminated:
        return (
            LivenessState.UNKNOWN,
            "the owned process is no longer observable; in-flight state is unknown",
        )
    if evidence.owned_identity_verified is False and evidence.owned_alive is True:
        return (
            LivenessState.UNKNOWN,
            "the recorded process identity no longer matches the live process",
        )

    # 5. Semantic progress is the positive test.
    progress = evidence.progress_observed()
    if progress is True:
        return LivenessState.WORKING, "semantic progress was observed"

    # 6. Output without semantic progress is a stall -- the criterion's point:
    #    byte growth from a capped stream is not progress.
    if evidence.output_observed:
        if evidence.self_matching:
            return (
                LivenessState.STALLED,
                "the attempt re-emits one self-matching event: no advancing "
                "progress, only output (byte growth is not progress)",
            )
        if evidence.advancing_events == 0 or evidence.tool_activity == 0:
            return (
                LivenessState.STALLED,
                "no advancing semantic event and no tool activity was observed; "
                "byte growth is not progress",
            )
        return (
            LivenessState.STALLED,
            "output was observed without semantic progress or tool activity",
        )
    if evidence.non_progress_samples >= STALL_SAMPLES:
        return (
            LivenessState.STALLED,
            f"no progress across {evidence.non_progress_samples} samples",
        )

    # 7. Nothing observable yet: cannot classify.  Never a guess.
    return (
        LivenessState.UNKNOWN,
        "no progress evidence is available yet" if progress is None
        else "the attempt has produced no output yet",
    )


# ---------------------------------------------------------------------------
# Evidence gathering (read-only, from the durable stream + owned identity)
# ---------------------------------------------------------------------------


def _classify_markers(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(wait_class, failure_class)`` from declared marker words.

    Wait classes are returned before failure classes only when a quota marker is
    present *without* an auth marker: a rate limit is a wait, an authentication
    refusal is a failure.
    """
    if not text:
        return None, None
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return None, "auth"
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return "quota", None
    if any(marker in lowered for marker in _TRANSPORT_MARKERS):
        return None, "transport"
    if any(marker in lowered for marker in _PROVIDER_MARKERS):
        return None, "provider"
    return None, None


def _semantic_events(store: Any, *, run_id: str, task_id: str, attempt: int) -> list:
    """Recover structured events from the retained stdout prefix (never bytes)."""
    nested = getattr(store, "nested_events", None)
    if nested is None:
        return []
    try:
        return list(
            nested(attempt, run_id=run_id, task_id=task_id) or []
        )
    except Exception:  # noqa: BLE001 - an observation failure is not a fact
        return []


def _retained_text(store: Any, stream: str, *, run_id: str, task_id: str,
                   attempt: int) -> str:
    try:
        spool = store.spool(attempt, stream, run_id=run_id, task_id=task_id)
        return spool.read_retained().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def gather_stream_evidence(
    store: Any,
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    recorded_identity: Optional[Any] = None,
    pid: Optional[int] = None,
    previous_signatures: Sequence[str] = (),
    previous_observations: Sequence["LivenessObservation"] = (),
) -> ProgressEvidence:
    """Build progress evidence from the attempt's own durable evidence.

    Read-only: the durable stream summaries (byte context), the retained
    stderr's declared markers (availability status) and the retained stdout's
    structured events (semantic progress + tool activity).  The owned process
    identity is probed live when a ``pid`` is supplied.
    """
    stdout_summary = None
    try:
        stdout_summary = store.summary(
            attempt, "stdout", run_id=run_id, task_id=task_id)
    except Exception:  # noqa: BLE001
        stdout_summary = None

    observed = None
    retained = None
    if stdout_summary is not None:
        observed = getattr(stdout_summary, "observed_bytes", None)
        retained = getattr(stdout_summary, "retained_bytes", None)

    events = _semantic_events(store, run_id=run_id, task_id=task_id, attempt=attempt)
    semantic_events = len(events)
    advancing = _advancing_signatures(events, already_seen=previous_signatures)
    # A may-be-series collapses to a single signature when every retained line
    # carries the same stable content: one piece of output, not progress.
    self_matching = _is_self_matching(events)

    tool_activity = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or event.get("type") or "").lower()
        if (
            kind in ("tool", "tool_call", "tool_use", "tool_result", "action")
            or event.get("tool_call")
            or event.get("tool_use")
            or event.get("tool_name")
        ):
            tool_activity += 1

    wait_class = None
    failure_class = None
    for event in events:
        if not isinstance(event, dict):
            continue
        declared = str(event.get("wait") or event.get("wait_class") or "").lower()
        if declared in ("owner", "quota", "rate_limit"):
            wait_class = "owner" if declared == "owner" else "quota"
        declared_failure = str(
            event.get("failure_class") or event.get("error_class") or "").lower()
        if declared_failure in ("auth", "transport", "provider"):
            failure_class = declared_failure

    # Availability statuses are read from the *declared markers* in the retained
    # stderr (the gateway's own words).  This is a bounded scan of evidence
    # already recorded, not a byte count and not a network probe.
    stderr_text = _retained_text(
        store, "stderr", run_id=run_id, task_id=task_id, attempt=attempt)
    marker_wait, marker_failure = _classify_markers(stderr_text)
    if wait_class is None:
        wait_class = marker_wait
    if failure_class is None:
        failure_class = marker_failure

    # A self-matching structured marker series is the repeated-error series: the
    # marker scan above already collapsed it to one classification; the series
    # length is what the watcher records.
    owned_verified = None
    owned_alive = None
    if pid is not None or recorded_identity is not None:
        try:
            import skillweave.runtime.process_identity as pidmod

            recorded = None
            target = pid
            if recorded_identity is not None:
                recorded = pidmod.ProcessIdentity.from_dict(recorded_identity)
                if target is None and recorded is not None:
                    target = recorded.pid
            if target:
                live = pidmod.capture(target)
                owned_alive = bool(live is not None and not live.zombie)
                # The owned *instance* is its pid together with its creation
                # instant, never its resolved executable path: on macOS a
                # framework executable reached through the ``bin/python3.x``
                # symlink re-resolves to ``Python.app/Contents/MacOS/Python``
                # between two observations of the *same* running process, so an
                # exec-path comparison would call a process we launched and
                # still own a foreign worker and refuse to classify it.
                if recorded is not None:
                    owned_verified = bool(
                        live is not None
                        and recorded.pid == target
                        and recorded.start_time == live.start_time
                    )
        except Exception:  # noqa: BLE001 - an observation failure is not a fact
            owned_verified = None
            owned_alive = None

    non_progress = 0
    if not events and not tool_activity:
        for previous in reversed(list(previous_observations)):
            if previous.state in (
                LivenessState.WORKING.value,
                LivenessState.WAITING_FOR_OWNER.value,
                LivenessState.WAITING_FOR_QUOTA.value,
            ):
                break
            non_progress += 1

    return ProgressEvidence(
        observed_bytes=observed,
        retained_bytes=retained,
        semantic_events=semantic_events,
        advancing_events=advancing,
        self_matching=self_matching if events else None,
        tool_activity=tool_activity,
        owned_identity_verified=owned_verified,
        owned_alive=owned_alive,
        wait_class=wait_class,
        failure_class=failure_class,
        non_progress_samples=non_progress,
    )


def derive_progress_evidence(
    *,
    observed_bytes: Optional[int] = None,
    retained_bytes: Optional[int] = None,
    events: Sequence[Any] = (),
    previous_signatures: Sequence[str] = (),
    tool_activity: Optional[int] = None,
    owned_identity_verified: Optional[bool] = None,
    owned_alive: Optional[bool] = None,
    wait_class: Optional[str] = None,
    failure_class: Optional[str] = None,
    terminated: Optional[bool] = None,
    result_subtype: Optional[str] = None,
    exit_code: Optional[int] = None,
    signal: Optional[int] = None,
    non_progress_samples: int = 0,
) -> ProgressEvidence:
    """Derive progress evidence from explicit semantic inputs.

    This is the *semantic* constructor: the caller supplies structured events
    and declared classes, never a byte count as the progress test.
    """
    advancing = _advancing_signatures(events, already_seen=previous_signatures)
    semantic_events = len(list(events))
    tool = tool_activity
    if tool is None:
        tool = 0
        for event in events:
            if isinstance(event, dict) and (
                event.get("tool_call") or event.get("tool_use")
                or str(event.get("kind") or "").lower() in ("tool", "tool_call")
            ):
                tool += 1
    return ProgressEvidence(
        observed_bytes=observed_bytes,
        retained_bytes=retained_bytes,
        semantic_events=semantic_events,
        advancing_events=advancing,
        self_matching=(True if _is_self_matching(events) else
                       (False if semantic_events else None)),
        tool_activity=tool,
        owned_identity_verified=owned_identity_verified,
        owned_alive=owned_alive,
        wait_class=wait_class,
        failure_class=failure_class,
        terminated=terminated,
        result_subtype=result_subtype,
        exit_code=exit_code,
        signal=signal,
        non_progress_samples=non_progress_samples,
    )


# ---------------------------------------------------------------------------
# Observation + durable causal series (survives a watcher restart)
# ---------------------------------------------------------------------------


@dataclass
class LivenessObservation:
    """One versioned liveness observation bound into its state's causal series.

    ``sequence`` is the position within *this state's* series and ``caused_by``
    the previous sequence of the same state (``-1`` at the root), so a repeated
    gateway-error series is one growing incident, not a series of new ones.
    """

    run_id: str
    task_id: str
    attempt: int
    state: str
    reason: str
    evidence: ProgressEvidence
    sequence: int = 0
    caused_by: int = -1
    observed_at: str = ""
    schema_version: str = ATTEMPT_LIVENESS_SCHEMA_VERSION
    pid: Optional[int] = None
    process_identity: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.state != LivenessState.UNKNOWN.value and self.state not in LIVENESS_STATES:
            raise LivenessError(
                f"state {self.state!r} is not one of the declared liveness "
                f"states {list(LIVENESS_STATES)}"
            )
        if self.schema_version != ATTEMPT_LIVENESS_SCHEMA_VERSION:
            raise LivenessError(
                f"unsupported liveness schema version {self.schema_version!r}")

    @property
    def identity(self) -> Tuple[str, str, int]:
        return (self.run_id, self.task_id, int(self.attempt))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTEMPT_LIVENESS_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "state": self.state,
            "reason": self.reason,
            "sequence": self.sequence,
            "caused_by": self.caused_by,
            "observed_at": self.observed_at,
            "pid": self.pid,
            "process_identity": self.process_identity,
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LivenessObservation":
        if not isinstance(data, dict):
            raise LivenessError("a liveness observation must be a JSON object")
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            attempt=int(data.get("attempt", 1)),
            state=str(data.get("state", LivenessState.UNKNOWN.value)),
            reason=str(data.get("reason", "")),
            evidence=ProgressEvidence.from_dict(data.get("evidence")),
            sequence=int(data.get("sequence", 0)),
            caused_by=int(data.get("caused_by", -1)),
            observed_at=str(data.get("observed_at", "")),
            schema_version=str(
                data.get("schema_version", ATTEMPT_LIVENESS_SCHEMA_VERSION)),
            pid=_optional_int(data.get("pid")),
            process_identity=data.get("process_identity"),
        )


@dataclass
class _Series:
    records: list = field(default_factory=list)


class LivenessWatcher:
    """Fold liveness observations into per-state causal series.

    The same state observed repeatedly accumulates into **one** growing series
    (``caused_by`` links each observation to its predecessor), which is the
    bounded incident: repeated gateway errors are one incident whose length is
    the count of observations, not a new incident per sample. Distinct states
    are distinct series and never merge.  :meth:`to_dict` / :meth:`from_dict`
    make the series durable, so a watcher *restart* continues the incident
    rather than resetting it, and :attr:`previous_signatures` additionally
    carries the self-match memory so a restarted watcher does not read a wait
    loop's repeated event as fresh progress.
    """

    def __init__(self) -> None:
        self._series: dict[str, list] = {}
        #: Every observation in the order it was recorded, across states.  The
        #: per-state series carry the causal chain; this flat history is what a
        #: sampling supervisor reads to count consecutive non-progress samples.
        self._history: list = []
        self._signatures: list = []
        self.attempt: Optional[Tuple[str, str, int]] = None

    # -- observation -------------------------------------------------------
    def observe(self, observation: LivenessObservation) -> LivenessObservation:
        """Record one observation, appending it to its state's causal series."""
        chain = self._series.setdefault(observation.state, [])
        observation.caused_by = chain[-1].sequence if chain else -1
        observation.sequence = len(chain)
        chain.append(observation)
        self._history.append(observation)
        self.attempt = observation.identity
        return observation

    def record(self, evidence: ProgressEvidence, *, run_id: str, task_id: str,
               attempt: int, observed_at: str = "",
               pid: Optional[int] = None,
               process_identity: Optional[dict[str, Any]] = None,
               ) -> LivenessObservation:
        """Classify *evidence* and append the observation to its series."""
        state, reason = explain(evidence)
        return self.observe(
            LivenessObservation(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                state=state.value,
                reason=reason,
                evidence=evidence,
                observed_at=observed_at,
                pid=pid,
                process_identity=process_identity,
            )
        )

    # -- series access -----------------------------------------------------
    def state_series(self, state: str) -> Tuple[LivenessObservation, ...]:
        """The causal series of one state (empty when never observed)."""
        return tuple(self._series.get(state, ()))

    def score_states(self) -> Tuple[str, ...]:
        """The states that have a live causal series, in vocabulary order."""
        return tuple(s for s in LIVENESS_STATES if self._series.get(s))

    def incident_length(self, state: str) -> int:
        """The bounded length of one state's incident (0 when never observed)."""
        return len(self._series.get(state, ()))

    def latest(self) -> Optional[LivenessObservation]:
        """The most recently *recorded* observation, or ``None``.

        Ordering comes from the flat chronological history, never from the
        per-state ``sequence`` (which is only meaningful *within* one state's
        series, so comparing sequences across states would misreport the
        latest state).
        """
        if self._history:
            return self._history[-1]
        # Fallback for a snapshot written before the history was recorded.
        newest = None
        for chain in self._series.values():
            if not chain:
                continue
            candidate = chain[-1]
            if newest is None or (candidate.sequence, candidate.state) > (
                    newest.sequence, newest.state):
                newest = candidate
        return newest

    def latest_state(self) -> Optional[str]:
        newest = self.latest()
        return newest.state if newest is not None else None

    def history(self) -> Tuple[LivenessObservation, ...]:
        """Every observation in the order recorded, across all states."""
        return tuple(self._history)

    @property
    def previous_signatures(self) -> Tuple[str, ...]:
        """Every progress signature already seen (the self-match memory)."""
        return tuple(self._signatures)

    def remember_signatures(self, signatures: Sequence[str]) -> None:
        for signature in signatures:
            if signature not in self._signatures:
                self._signatures.append(signature)

    # -- durability --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """A durable snapshot: replaying it restores every causal series."""
        return {
            "schema": ATTEMPT_LIVENESS_SCHEMA_ID,
            "schema_version": ATTEMPT_LIVENESS_SCHEMA_VERSION,
            "attempt": list(self.attempt) if self.attempt else None,
            "signatures": list(self._signatures),
            "series": {
                state: [r.to_dict() for r in records]
                for state, records in self._series.items()
            },
            "order": [r.to_dict() for r in self._history],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LivenessWatcher":
        """Rehydrate a watcher from a snapshot, continuing its series."""
        watcher = cls()
        if not isinstance(data, dict):
            return watcher
        for state, records in (data.get("series") or {}).items():
            watcher._series[state] = [
                LivenessObservation.from_dict(r) for r in records
            ]
        watcher._signatures = list(data.get("signatures") or [])
        watcher._history = [
            LivenessObservation.from_dict(r) for r in (data.get("order") or [])
        ]
        attempt = data.get("attempt")
        if isinstance(attempt, (list, tuple)) and len(attempt) == 3:
            watcher.attempt = (str(attempt[0]), str(attempt[1]), int(attempt[2]))
        return watcher


# ---------------------------------------------------------------------------
# Durable store
# ---------------------------------------------------------------------------


def liveness_root_for(store: Any) -> Optional[str]:
    """Derive the durable liveness root from a stream store, or ``None``.

    It sits beside the durable stream root (the same convention the
    attempt-final-result store uses), so a liveness observation outlives the
    retention-capped stream bytes.  A memory-only store yields ``None`` and the
    recorder stays memory-only rather than inventing a path.
    """
    root = getattr(store, "root", None)
    if not root:
        return None
    return os.path.dirname(os.path.abspath(str(root))) or "."


@dataclass
class AttemptLivenessStore:
    """Durable, atomic writer/reader of one attempt's liveness incident.

    ``root`` is a directory; each attempt's snapshot is written to
    ``<root>/attempt-liveness/<key>.watcher.json`` via an atomic replace and
    fsynced, so a watcher restart rehydrates the incident from disk. ``root=None``
    is explicitly memory-only.
    """

    root: Optional[str] = None
    _snapshots: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.root:
            self._dir().mkdir(parents=True, exist_ok=True)

    @property
    def is_durable(self) -> bool:
        return bool(self.root)

    def _dir(self) -> Path:
        return Path(str(self.root)) / "attempt-liveness"

    def _key(self, identity: Tuple[str, str, int]) -> str:
        raw = f"{identity[0]}\x00{identity[1]}\x00{identity[2]}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:32]

    def _path(self, identity: Tuple[str, str, int]) -> Path:
        return self._dir() / f"{self._key(identity)}.watcher.json"

    def write(self, watcher: LivenessWatcher, identity: Tuple[str, str, int]) -> None:
        """Persist one attempt's watcher snapshot (atomic, fsynced)."""
        snapshot = watcher.to_dict()
        self._snapshots[identity] = snapshot
        if not self.root:
            return
        path = self._path(identity)
        tmp = path.with_suffix(".json.tmp")
        data = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode(
            "utf-8")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def read(self, run_id: str, task_id: str, attempt: int) -> Optional[dict]:
        """Read a persisted snapshot, or ``None`` when none exists."""
        identity = (run_id, task_id, int(attempt))
        in_memory = self._snapshots.get(identity)
        if in_memory is not None:
            return in_memory
        if not self.root:
            return None
        path = self._path(identity)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def rehydrate(self, run_id: str, task_id: str, attempt: int) -> LivenessWatcher:
        """Rehydrate the attempt's watcher, continuing its incident."""
        snapshot = self.read(run_id, task_id, attempt)
        return LivenessWatcher.from_dict(snapshot) if snapshot else LivenessWatcher()


# ---------------------------------------------------------------------------
# The production recorder: sampling inside the real supervision path
# ---------------------------------------------------------------------------


class LivenessRecorder:
    """Record an owned attempt's liveness from inside its supervision loop.

    The recorder is constructed by the real supervision path
    (:func:`skillweave.core.proc.runner.run_process_streaming`) around the child
    it launched, samples the attempt's own durable evidence at a bounded rate,
    and appends the terminal observation when the attempt ends.  It is
    memory-only when no durable root can be derived, and every observation
    failure is swallowed: liveness evidence is additive and must never change
    the outcome of the attempt it observes.
    """

    def __init__(
        self,
        store: Any,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        pid: Optional[int] = None,
        process_identity: Optional[dict[str, Any]] = None,
        root: Optional[str] = None,
        interval: float = 0.25,
        max_samples: int = 64,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.task_id = task_id
        self.attempt = int(attempt)
        self.pid = pid
        self.process_identity = process_identity
        self.root = root if root is not None else liveness_root_for(store)
        self.store_ = AttemptLivenessStore(root=self.root)
        self.interval = float(interval)
        self.max_samples = int(max_samples)
        self._last_sample = 0.0
        self._samples = 0
        try:
            self.watcher = self.store_.rehydrate(run_id, task_id, self.attempt)
        except Exception:  # noqa: BLE001
            self.watcher = LivenessWatcher()

    @classmethod
    def for_store(cls, store: Any, *, run_id: str, task_id: str, attempt: int,
                  pid: Optional[int] = None) -> "LivenessRecorder":
        """Build a recorder for a launched attempt, capturing its identity."""
        identity: Optional[dict[str, Any]] = None
        if pid:
            try:
                import skillweave.runtime.process_identity as pidmod

                captured = pidmod.capture(pid)
                identity = captured.to_dict() if captured is not None else None
            except Exception:  # noqa: BLE001
                identity = None
        return cls(
            store, run_id=run_id, task_id=task_id, attempt=attempt, pid=pid,
            process_identity=identity,
        )

    # -- sampling ----------------------------------------------------------
    def sample(self, *, force: bool = False) -> Optional[LivenessObservation]:
        """Record one in-flight observation, rate-limited and bounded."""
        if self._samples >= self.max_samples:
            return None
        now = time.monotonic()
        if not force and (now - self._last_sample) < self.interval:
            return None
        self._last_sample = now
        self._samples += 1
        try:
            evidence = gather_stream_evidence(
                self.store,
                run_id=self.run_id,
                task_id=self.task_id,
                attempt=self.attempt,
                recorded_identity=self.process_identity,
                pid=self.pid,
                previous_signatures=self.watcher.previous_signatures,
                previous_observations=self.watcher.history(),
            )
            observation = self.watcher.record(
                evidence,
                run_id=self.run_id,
                task_id=self.task_id,
                attempt=self.attempt,
                pid=self.pid,
                process_identity=self.process_identity,
            )
            self._remember(evidence)
            self.store_.write(self.watcher, observation.identity)
            return observation
        except Exception:  # noqa: BLE001 - liveness is additive evidence
            return None

    def finish(self, fact: Any = None) -> Optional[LivenessObservation]:
        """Record the terminal observation for the attempt from its typed fact."""
        try:
            subtype = None
            exit_code = None
            sig = None
            if fact is not None:
                subtype = getattr(fact, "result_subtype", None)
                termination = getattr(fact, "termination", None)
                subtype = subtype or termination
                exit_code = getattr(fact, "exit_code", None)
                sig = getattr(fact, "signal", None)
            evidence = gather_stream_evidence(
                self.store,
                run_id=self.run_id,
                task_id=self.task_id,
                attempt=self.attempt,
                recorded_identity=None,
                pid=None,
                previous_signatures=self.watcher.previous_signatures,
            )
            terminal = ProgressEvidence(
                observed_bytes=evidence.observed_bytes,
                retained_bytes=evidence.retained_bytes,
                semantic_events=evidence.semantic_events,
                advancing_events=evidence.advancing_events,
                self_matching=evidence.self_matching,
                tool_activity=evidence.tool_activity,
                owned_identity_verified=evidence.owned_identity_verified,
                owned_alive=False,
                wait_class=evidence.wait_class,
                failure_class=evidence.failure_class,
                terminated=True,
                result_subtype=subtype,
                exit_code=exit_code,
                signal=sig,
            )
            observation = self.watcher.record(
                terminal,
                run_id=self.run_id,
                task_id=self.task_id,
                attempt=self.attempt,
                pid=self.pid,
                process_identity=self.process_identity,
            )
            self._remember(evidence)
            self.store_.write(self.watcher, observation.identity)
            return observation
        except Exception:  # noqa: BLE001 - liveness is additive evidence
            return None

    def _remember(self, evidence: ProgressEvidence) -> None:
        try:
            events = _semantic_events(
                self.store, run_id=self.run_id, task_id=self.task_id,
                attempt=self.attempt)
            self.watcher.remember_signatures(
                [progress_signature(e) for e in events])
        except Exception:  # noqa: BLE001
            pass

    # -- reporting ---------------------------------------------------------
    def report(self) -> dict[str, Any]:
        """The attempt's liveness report: the latest state + its incident series."""
        latest = self.watcher.latest()
        return {
            "schema": ATTEMPT_LIVENESS_SCHEMA_ID,
            "schema_version": ATTEMPT_LIVENESS_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "root": self.root,
            "durable": self.store_.is_durable,
            "state": latest.state if latest is not None else None,
            "reason": latest.reason if latest is not None else "",
            "samples": self._samples,
            "incidents": {
                state: self.watcher.incident_length(state)
                for state in self.watcher.score_states()
            },
            "series": {
                state: [r.to_dict() for r in self.watcher.state_series(state)]
                for state in self.watcher.score_states()
            },
        }


# ---------------------------------------------------------------------------
# Module-level convenience (the durable record API for callers/tests)
# ---------------------------------------------------------------------------


def record_attempt_liveness(
    store: Any,
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    pid: Optional[int] = None,
    process_identity: Optional[dict[str, Any]] = None,
    root: Optional[str] = None,
) -> dict[str, Any]:
    """Record one attempt's liveness from its own evidence and return the report."""
    recorder = LivenessRecorder(
        store, run_id=run_id, task_id=task_id, attempt=attempt, pid=pid,
        process_identity=process_identity, root=root,
    )
    recorder.sample(force=True)
    return recorder.report()


def read_attempt_liveness(
    root: str, run_id: str, task_id: str, attempt: int
) -> Optional[dict]:
    """Read an attempt's persisted liveness report (latest state + incidents)."""
    store = AttemptLivenessStore(root=root)
    snapshot = store.read(run_id, task_id, attempt)
    if snapshot is None:
        return None
    watcher = LivenessWatcher.from_dict(snapshot)
    latest = watcher.latest()
    return {
        "schema": ATTEMPT_LIVENESS_SCHEMA_ID,
        "schema_version": ATTEMPT_LIVENESS_SCHEMA_VERSION,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": int(attempt),
        "state": latest.state if latest is not None else None,
        "reason": latest.reason if latest is not None else "",
        "incidents": {
            state: watcher.incident_length(state)
            for state in watcher.score_states()
        },
        "watcher": snapshot,
    }


def read_attempt_liveness_series(
    root: str, run_id: str, task_id: str, attempt: int, state: str
) -> Tuple[dict, ...]:
    """Read one state's persisted causal series for an attempt."""
    store = AttemptLivenessStore(root=root)
    snapshot = store.read(run_id, task_id, attempt)
    if snapshot is None:
        return ()
    watcher = LivenessWatcher.from_dict(snapshot)
    return tuple(r.to_dict() for r in watcher.state_series(state))
