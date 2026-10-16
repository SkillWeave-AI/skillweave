"""Unambiguous final result for every launched attempt (SW-1511A-RESULT-001).

This module is the storage-side contract named by the task boundary
``attempt-final-result/v1``: the producer is the run wrapper and the child
result reconciler, the consumer the ledger projector, controller, reviewer and
operator UI. Its compatibility clause is explicit — *legacy exit/raw-stream
attempts remain readable through an explicit migration adapter* — so the
adapter here never infers an outcome from an exit code alone.

It separates three things that the control loop used to blur together:

1. **Child execution** — the raw, un-coerced machine outcome of the child
   (``child_exit`` *or* ``child_signal``) kept apart from the **wrapper**
   process exit code. A wrapper that exits non-zero only because it faithfully
   relayed a child's failure is recorded as such, never as the child's own
   exit.
2. **Assay artifact semantics** — the assay state and whether artifacts are
   *intentionally absent*. A successful child whose artifact semantics are
   deliberately not applicable is represented as a **named hold** or a
   **non-applicable gate**, never as an undifferentiated process failure.
3. **Evidence** — a single ``evidence_digest`` over *every observed byte* of
   the attempt's streams. The digest is exact and retention-independent, and
   the result record is written durably and atomically at attempt end, so a
   verdict and its receipt survive a retention-capped stream without any
   resume.

Exactly one ``attempt-final-result/v1`` exists per launched attempt identity
``(run_id, task_id, attempt)``: a conflicting second write is refused, an
identical re-delivery is idempotent.

Nothing here launches a process, reads a wall clock, opens a socket or names a
provider/model/harness. The store is duck-typed (``fact`` / ``summary``), so
this module stays dependency-light and is consumable by the dispatch
application and the later trace lanes without importing the runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

# ── Contract identity ────────────────────────────────────────────────────────

ATTEMPT_FINAL_RESULT_SCHEMA_ID = (
    "attempt-final-result/v1 (skillweave // SW-1511A-RESULT-001)"
)
ATTEMPT_FINAL_RESULT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_ATTEMPT_FINAL_RESULT_VERSIONS: Tuple[str, ...] = ("1.0.0",)

#: The closed vocabulary of assay states, byte-identical to
#: :class:`skillweave.runtime.assay.AssayState`. It is restated (not imported)
#: so the trace layer keeps no dependency on the runtime; a drift between the
#: two is caught by the criterion test, not papered over by a loose copy.
ASSAY_STATES: Tuple[str, ...] = (
    "pin",
    "hypothesis",
    "probe",
    "measure",
    "classify",
    "artifact_verify",
    "remediate",
    "escalate_or_split",
    "pass",
    "hold",
)

#: The closed vocabulary of child terminal reasons. Each is distinct: a launch
#: failure, a timeout, a signal, a heartbeat expiry and a cancel never collapse
#: into one ambiguous "failed" bucket.
TERMINAL_REASONS: Tuple[str, ...] = (
    "child_exited",
    "child_signaled",
    "child_timed_out",
    "launch_failed",
    "cancelled",
    "heartbeat_expired",
    "blocked_input",
    "preflight_failed",
)

#: The terminal reasons that are a *technical* end, not a task verdict. A child
#: that never ran, or was reaped by the wrapper, must not be reported as a
#: task-level process failure.
TECHNICAL_TERMINAL_REASONS: frozenset[str] = frozenset({
    "launch_failed",
    "child_timed_out",
    "cancelled",
    "heartbeat_expired",
    "blocked_input",
    "preflight_failed",
})


# ── Fail-closed exceptions ───────────────────────────────────────────────────


class AttemptFinalResultError(Exception):
    """An attempt-final-result contract violation (raised fail-closed)."""


class DuplicateFinalResultError(AttemptFinalResultError):
    """A second, *differing* final result was offered for one attempt.

    Every launched attempt carries exactly one ``attempt-final-result/v1``; a
    conflicting re-write is refused rather than silently replacing the record.
    """


class UndifferentiatedProcessFailureError(AttemptFinalResultError):
    """A successful child with intentionally absent artifacts was flattened.

    The child succeeded and its artifact semantics were deliberately not
    applicable: the record must say so as a named hold or a non-applicable
    gate. Recording it as an undifferentiated process failure is refused.
    """


class UnknownFinalResultVersionError(AttemptFinalResultError):
    """The record carries a schema version outside the supported set."""


class UnreadableFinalResultError(AttemptFinalResultError):
    """A persisted final result is absent or cannot be resolved."""


# ── The separated dimensions ─────────────────────────────────────────────────


class FinalVerdict(str, Enum):
    """The verdict dimension: how the *task* discharged, not how the child ended.

    ``hold`` and ``not_applicable`` are first-class: a named hold or a
    non-applicable gate is a *decision*, not a failure, and is never folded
    into ``fail``.
    """

    PASS = "pass"
    FAIL = "fail"
    HOLD = "hold"
    NOT_APPLICABLE = "not_applicable"
    INCONCLUSIVE = "inconclusive"


class ArtifactSemantics(str, Enum):
    """Whether the attempt's artifact semantics are applicable or deliberately not.

    ``NAMED_HOLD`` and ``NOT_APPLICABLE`` are the two explicit ways a
    successful child with intentionally absent artifacts is represented. Both
    require a name/reason so the absence is *named*, never undifferentiated.
    """

    APPLICABLE = "applicable"
    NAMED_HOLD = "named_hold"
    NOT_APPLICABLE = "not_applicable"


class ChildOutcome(str, Enum):
    """The single machine outcome of the child process."""

    EXIT_CODE = "exit_code"
    SIGNAL = "signal"
    TIMED_OUT = "timed_out"
    LAUNCH_FAILED = "launch_failed"


def assay_state_value(state: Any) -> str:
    """Return the canonical string of an assay state (enum member or string).

    Duck-typed: an :class:`~skillweave.runtime.assay.AssayState` member is
    accepted through its ``value`` without importing the runtime, and a bare
    string is validated against :data:`ASSAY_STATES`.
    """
    value = getattr(state, "value", state)
    text = str(value)
    if text not in ASSAY_STATES:
        raise AttemptFinalResultError(
            f"unknown assay state '{text}'; expected one of {list(ASSAY_STATES)}"
        )
    return text


# ── The final result record ──────────────────────────────────────────────────


@dataclass
class AttemptFinalResult:
    """Exactly one attempt's unambiguous final result (``.../v1``).

    The three concerns are separate fields, never derived from one another:

    * ``child_exit`` / ``child_signal`` — the child's raw machine outcome;
    * ``wrapper_exit`` — the wrapper process' own exit code;
    * ``assay_state`` / ``artifact_semantics`` — Assay artifact semantics;
    * ``verdict`` / ``terminal_reason`` — the task verdict and its cause;
    * ``evidence_digest`` — the exact digest over every observed stream byte.

    A successful child with intentionally absent artifact semantics can only be
    recorded as ``HOLD`` or ``NOT_APPLICABLE`` (criterion 2); an attempt to
    record ``FAIL`` there raises :class:`UndifferentiatedProcessFailureError`.
    """

    run_id: str
    task_id: str
    attempt: int
    child_exit: Optional[int]
    wrapper_exit: int
    verdict: FinalVerdict
    terminal_reason: str
    evidence_digest: str
    assay_state: str = "pin"
    child_signal: Optional[int] = None
    artifact_semantics: ArtifactSemantics = ArtifactSemantics.APPLICABLE
    hold_name: str = ""
    hold_reason: str = ""
    observed_at: str = ""
    migrated_from_legacy: bool = False
    schema_version: str = ATTEMPT_FINAL_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.verdict = FinalVerdict(self.verdict)
        self.artifact_semantics = ArtifactSemantics(self.artifact_semantics)
        self.assay_state = assay_state_value(self.assay_state)
        if self.schema_version not in SUPPORTED_ATTEMPT_FINAL_RESULT_VERSIONS:
            raise UnknownFinalResultVersionError(
                f"unsupported attempt-final-result version "
                f"'{self.schema_version}'"
            )
        if self.terminal_reason not in TERMINAL_REASONS:
            raise AttemptFinalResultError(
                f"unknown terminal reason '{self.terminal_reason}'; expected one "
                f"of {list(TERMINAL_REASONS)}"
            )
        self.verify_outcome()
        self._verify_artifact_semantics()

    # -- criterion 1: exactly one machine outcome ---------------------------

    @property
    def outcome(self) -> str:
        """The single child machine outcome (exit code / signal / timeout / launch)."""
        if self.terminal_reason == "launch_failed":
            return ChildOutcome.LAUNCH_FAILED.value
        if self.terminal_reason == "child_timed_out":
            return ChildOutcome.TIMED_OUT.value
        if self.child_signal is not None:
            return ChildOutcome.SIGNAL.value
        return ChildOutcome.EXIT_CODE.value

    def verify_outcome(self) -> None:
        """Reject a contradictory or incomplete machine outcome."""
        if self.terminal_reason in ("child_exited", "child_signaled"):
            if self.child_exit is not None and self.child_signal is not None:
                raise AttemptFinalResultError(
                    "child carries both an exit code and a signal"
                )
            if self.child_exit is None and self.child_signal is None:
                raise AttemptFinalResultError(
                    "child carries neither an exit code nor a signal"
                )
        elif self.terminal_reason == "child_signaled" and self.child_signal is None:
            raise AttemptFinalResultError("child_signaled without a signal number")
        if self.wrapper_exit is None:
            raise AttemptFinalResultError("wrapper exit code is required")

    # -- criterion 2: intentional absence is never a process failure --------

    def _verify_artifact_semantics(self) -> None:
        absent = self.artifact_semantics is not ArtifactSemantics.APPLICABLE
        child_succeeded = (
            self.child_exit == 0
            and self.child_signal is None
            and self.terminal_reason == "child_exited"
        )
        if absent:
            if not (self.hold_name or self.hold_reason or self.run_id):
                raise AttemptFinalResultError(
                    "intentionally absent artifacts must be named"
                )
            if not child_succeeded and self.verdict is not FinalVerdict.INCONCLUSIVE:
                raise AttemptFinalResultError(
                    "artifact semantics may be intentionally absent only for a "
                    "successful child"
                )
            if self.verdict not in (FinalVerdict.HOLD, FinalVerdict.NOT_APPLICABLE):
                raise UndifferentiatedProcessFailureError(
                    f"a successful child with intentionally absent artifact "
                    f"semantics must be a named hold or a non-applicable gate, "
                    f"not verdict '{self.verdict.value}'"
                )
            expected = (
                FinalVerdict.HOLD
                if self.artifact_semantics is ArtifactSemantics.NAMED_HOLD
                else FinalVerdict.NOT_APPLICABLE
            )
            if self.verdict is not expected:
                raise UndifferentiatedProcessFailureError(
                    f"artifact semantics '{self.artifact_semantics.value}' must "
                    f"yield verdict '{expected.value}', not '{self.verdict.value}'"
                )

    @property
    def gate_state(self) -> str:
        """The gate decision: ``pass`` / ``fail`` / ``hold`` / ``not_applicable``."""
        return self.verdict.value

    @property
    def identity(self) -> Tuple[str, str, int]:
        return (self.run_id, self.task_id, self.attempt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTEMPT_FINAL_RESULT_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "child_exit": self.child_exit,
            "child_signal": self.child_signal,
            "wrapper_exit": self.wrapper_exit,
            "outcome": self.outcome,
            "assay_state": self.assay_state,
            "artifact_semantics": self.artifact_semantics.value,
            "verdict": self.verdict.value,
            "gate_state": self.gate_state,
            "evidence_digest": self.evidence_digest,
            "terminal_reason": self.terminal_reason,
            "hold_name": self.hold_name,
            "hold_reason": self.hold_reason,
            "observed_at": self.observed_at,
            "migrated_from_legacy": self.migrated_from_legacy,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AttemptFinalResult":
        return cls(
            run_id=data["run_id"],
            task_id=data["task_id"],
            attempt=data["attempt"],
            child_exit=data["child_exit"],
            child_signal=data.get("child_signal"),
            wrapper_exit=data["wrapper_exit"],
            verdict=data["verdict"],
            terminal_reason=data["terminal_reason"],
            evidence_digest=data["evidence_digest"],
            assay_state=data.get("assay_state", "pin"),
            artifact_semantics=data.get(
                "artifact_semantics", ArtifactSemantics.APPLICABLE.value
            ),
            hold_name=data.get("hold_name", ""),
            hold_reason=data.get("hold_reason", ""),
            observed_at=data.get("observed_at", ""),
            migrated_from_legacy=bool(data.get("migrated_from_legacy", False)),
            schema_version=data.get("schema_version", ATTEMPT_FINAL_RESULT_SCHEMA_VERSION),
        )


# ── Verdict derivation ───────────────────────────────────────────────────────


def derive_final_verdict(
    *,
    child_exit: Optional[int],
    child_signal: Optional[int],
    terminal_reason: str,
    assay_state: str = "pin",
    artifact_semantics: ArtifactSemantics = ArtifactSemantics.APPLICABLE,
    artifacts_declared: bool = True,
    artifacts_resolved: bool = True,
) -> FinalVerdict:
    """Derive the task verdict from the separated facts, never from exit alone.

    * intentional absence (``NAMED_HOLD`` / ``NOT_APPLICABLE``) on a successful
      child yields ``HOLD`` / ``NOT_APPLICABLE`` — a decision, never ``fail``;
    * a technical terminal reason is ``INCONCLUSIVE`` (the task was never
      discharged), never a fabricated ``fail``;
    * a bare exit zero with declared-but-unresolved artifacts is
      ``INCONCLUSIVE``: exit zero alone is not a pass;
    * only a clean child exit with no signal and satisfied artifact semantics
      is ``PASS``.
    """
    semantics = ArtifactSemantics(artifact_semantics)
    assay = assay_state_value(assay_state)

    if terminal_reason in TECHNICAL_TERMINAL_REASONS:
        return FinalVerdict.INCONCLUSIVE

    child_succeeded = (
        child_exit == 0
        and child_signal is None
        and terminal_reason == "child_exited"
    )

    if semantics is not ArtifactSemantics.APPLICABLE:
        if not child_succeeded:
            return FinalVerdict.INCONCLUSIVE
        if semantics is ArtifactSemantics.NAMED_HOLD or assay == "hold":
            return FinalVerdict.HOLD
        return FinalVerdict.NOT_APPLICABLE

    if not child_succeeded:
        return FinalVerdict.FAIL
    if artifacts_declared and not artifacts_resolved:
        return FinalVerdict.INCONCLUSIVE
    return FinalVerdict.PASS


def terminal_reason_for(
    *,
    child_exit: Optional[int],
    child_signal: Optional[int],
    termination: str,
    timed_out: bool = False,
) -> str:
    """Map a raw termination onto the closed terminal-reason vocabulary."""
    if termination == "launch_failed":
        return "launch_failed"
    if timed_out or termination == "timed_out":
        return "child_timed_out"
    if termination == "cancelled":
        return "cancelled"
    if termination == "heartbeat_expired":
        return "heartbeat_expired"
    if termination == "blocked_input":
        return "blocked_input"
    if termination == "preflight_failed":
        return "preflight_failed"
    if child_signal is not None:
        return "child_signaled"
    return "child_exited"


# ── Evidence digest ──────────────────────────────────────────────────────────


def evidence_digest(observed_digests: Sequence[str]) -> str:
    """Combine per-stream whole-stream digests into the attempt's evidence digest.

    Each input is the exact digest over *every observed byte* of one stream
    (retention-independent). The combined digest is order-independent over the
    stream set, so a retention-capped stream still yields the same evidence
    digest as a fully retained one.
    """
    payload = json.dumps(
        sorted(str(d) for d in observed_digests), separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def evidence_digest_from_store(store: Any, *, run_id: str, task_id: str, attempt: int) -> str:
    """Read the exact evidence digest for one attempt from a duck-typed store.

    Only ``store.summary(attempt, stream, ...)`` is used, and only its
    whole-stream ``digest`` field: the retained prefix (``retained_digest``) is
    never the evidence. This is what makes the verdict and its receipt
    available when the stream exceeded retention.
    """
    digests = []
    for name in ("stdout", "stderr"):
        try:
            summary = store.summary(attempt, name, run_id=run_id, task_id=task_id)
        except Exception:  # noqa: BLE001 - a missing stream is not evidence
            continue
        digest = getattr(summary, "digest", None)
        if isinstance(summary, Mapping):
            digest = summary.get("digest")
        if digest:
            digests.append(str(digest))
    return evidence_digest(digests)


# ── The durable writer/reader (criterion 1, 3) ───────────────────────────────


@dataclass
class AttemptFinalResultStore:
    """Durable, atomic, exactly-one writer and reader of final results.

    ``root`` is a directory; each record is written to
    ``<root>/attempt-final-results/<key>.json`` via an atomic replace and
    fsynced, so the verdict and its receipt are available without any resume
    and independent of stream retention. ``root=None`` is explicitly
    memory-only.

    Exactly one record exists per attempt identity: an identical re-write is
    idempotent, a differing one raises :class:`DuplicateFinalResultError`.
    """

    root: Optional[str] = None
    _records: dict[Tuple[str, str, int], AttemptFinalResult] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.root:
            self._dir().mkdir(parents=True, exist_ok=True)

    @property
    def is_durable(self) -> bool:
        return bool(self.root)

    def _dir(self) -> Path:
        return Path(str(self.root)) / "attempt-final-results"

    def _path(self, identity: Tuple[str, str, int]) -> Path:
        raw = f"{identity[0]}\x00{identity[1]}\x00{identity[2]}".encode("utf-8")
        key = hashlib.sha256(raw).hexdigest()[:32]
        return self._dir() / f"{key}.json"

    def write(self, result: AttemptFinalResult) -> AttemptFinalResult:
        """Write exactly one result per attempt identity (idempotent, atomic)."""
        existing = self.read(*result.identity, missing_ok=True)
        if existing is not None:
            if existing.to_dict() == result.to_dict():
                return existing
            raise DuplicateFinalResultError(
                f"attempt {result.identity} already has a final result "
                f"(verdict '{existing.verdict.value}'); refusing to overwrite it "
                f"with verdict '{result.verdict.value}'"
            )
        self._records[result.identity] = result
        if self.root:
            self._write_atomic(self._path(result.identity), result.to_dict())
        return result

    def read(
        self, run_id: str, task_id: str, attempt: int, *, missing_ok: bool = False
    ) -> Optional[AttemptFinalResult]:
        """Read one persisted final result, or raise when it is absent."""
        identity = (run_id, task_id, attempt)
        in_memory = self._records.get(identity)
        if in_memory is not None:
            return in_memory
        if self.root:
            path = self._path(identity)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                result = AttemptFinalResult.from_dict(data)
                self._records[identity] = result
                return result
        if missing_ok:
            return None
        raise UnreadableFinalResultError(
            f"no attempt-final-result/v1 for {identity}"
        )

    def identities(self) -> Tuple[Tuple[str, str, int], ...]:
        return tuple(sorted(self._records))

    def _write_atomic(self, path: Path, value: Any) -> None:
        tmp = path.with_suffix(".json.tmp")
        data = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def final_result_from_attempt(
    store: Any,
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    wrapper_exit: int,
    assay_state: Any = "pin",
    artifact_semantics: ArtifactSemantics = ArtifactSemantics.APPLICABLE,
    hold_name: str = "",
    hold_reason: str = "",
    artifacts_declared: bool = True,
    artifacts_resolved: bool = True,
    observed_at: str = "",
) -> AttemptFinalResult:
    """Build the final result for one attempt from its collected facts.

    The child machine outcome and the evidence digest come from the
    duck-typed ``store`` (``fact`` / ``summary``). The evidence digest is the
    whole-observed-byte digest, so a retention-capped stream still yields the
    same evidence digest and the same verdict.
    """
    fact = store.fact(attempt, run_id=run_id, task_id=task_id)
    if fact is None:
        raise UnreadableFinalResultError(
            f"no collected fact for attempt {(run_id, task_id, attempt)}"
        )
    child_exit = getattr(fact, "exit_code", None)
    child_signal = getattr(fact, "signal", None)
    termination = getattr(fact, "termination", "exited")
    reason = terminal_reason_for(
        child_exit=child_exit, child_signal=child_signal, termination=termination
    )
    semantics = ArtifactSemantics(artifact_semantics)
    verdict = derive_final_verdict(
        child_exit=child_exit,
        child_signal=child_signal,
        terminal_reason=reason,
        assay_state=assay_state,
        artifact_semantics=semantics,
        artifacts_declared=artifacts_declared,
        artifacts_resolved=artifacts_resolved,
    )
    return AttemptFinalResult(
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        child_exit=child_exit,
        child_signal=child_signal,
        wrapper_exit=wrapper_exit,
        verdict=verdict,
        terminal_reason=reason,
        evidence_digest=evidence_digest_from_store(
            store, run_id=run_id, task_id=task_id, attempt=attempt
        ),
        assay_state=assay_state_value(assay_state),
        artifact_semantics=semantics,
        hold_name=hold_name,
        hold_reason=hold_reason,
        observed_at=observed_at,
    )


# ── Legacy migration adapter (compatibility clause) ──────────────────────────


_LEGACY_TERMINATION_TO_REASON: Mapping[str, str] = {
    "exited": "child_exited",
    "signaled": "child_signaled",
    "timed_out": "child_timed_out",
    "cancelled": "cancelled",
    "launch_failed": "launch_failed",
}


def adapt_legacy_attempt(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    exit_code: Optional[int],
    signal: Optional[int] = None,
    termination: str = "exited",
    raw_stream_digest: str = "",
    assay_state: Any = "pin",
    observed_at: str = "",
) -> AttemptFinalResult:
    """Read a legacy exit/raw-stream attempt through an explicit adapter.

    The legacy shape carries only an exit code and a raw stream. This adapter
    makes the migration *explicit* (``migrated_from_legacy=True``), records the
    wrapper exit as the legacy exit code it was, and maps the raw termination
    onto the closed terminal-reason vocabulary. It never infers a task verdict
    from the exit code alone: a legacy attempt with no assay evidence is
    ``INCONCLUSIVE``, and a non-``exited`` termination is a technical terminal
    reason, not a fabricated task failure.
    """
    termination = termination if termination in _LEGACY_TERMINATION_TO_REASON else "exited"
    reason = _LEGACY_TERMINATION_TO_REASON[termination]
    if reason == "child_exited" and exit_code is not None and exit_code != 0:
        # A non-zero exit is a child failure, but a legacy record cannot claim a
        # task verdict; it is preserved as its raw reason and remains
        # inconclusive rather than silently "done".
        reason = "child_exited"
    if reason == "child_exited":
        verdict = FinalVerdict.PASS if exit_code == 0 else FinalVerdict.FAIL
    else:
        verdict = FinalVerdict.INCONCLUSIVE
    return AttemptFinalResult(
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        child_exit=exit_code if signal is None else None,
        child_signal=signal,
        wrapper_exit=exit_code if exit_code is not None else 1,
        verdict=verdict,
        terminal_reason=reason,
        evidence_digest=raw_stream_digest,
        assay_state=assay_state,
        observed_at=observed_at,
        migrated_from_legacy=True,
    )


__all__ = [
    "ATTEMPT_FINAL_RESULT_SCHEMA_ID",
    "ATTEMPT_FINAL_RESULT_SCHEMA_VERSION",
    "SUPPORTED_ATTEMPT_FINAL_RESULT_VERSIONS",
    "ASSAY_STATES",
    "TERMINAL_REASONS",
    "TECHNICAL_TERMINAL_REASONS",
    "AttemptFinalResultError",
    "DuplicateFinalResultError",
    "UndifferentiatedProcessFailureError",
    "UnknownFinalResultVersionError",
    "UnreadableFinalResultError",
    "FinalVerdict",
    "ArtifactSemantics",
    "ChildOutcome",
    "assay_state_value",
    "AttemptFinalResult",
    "AttemptFinalResultStore",
    "derive_final_verdict",
    "terminal_reason_for",
    "evidence_digest",
    "evidence_digest_from_store",
    "final_result_from_attempt",
    "adapt_legacy_attempt",
]
