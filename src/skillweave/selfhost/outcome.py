"""Evidence-backed lane outcome resolution (SW-159P-OUTCOME-001).

The self-hosting runner used to parse ``OPS_READY <sha>`` by regex alone. That
made a lane's *only* way to finish a success token in stdout: a lane that
pushed a valid, verified tip but printed no sentinel had **no outcome at all**
— an old false negative — while the sentinel itself was never validated against
repository state.

This module owns the *typed, versioned* completion contract that replaces it.
A lane resolves to exactly one of four states:

* ``sentinel_confirmed`` — a full 40-hex ``OPS_READY <sha>`` sentinel was
  printed **and** that exact SHA resolves on the declared remote branch, is
  freshly fetched, descends from the pinned base, satisfies repository
  identity and write scope, and carries every required verification receipt.
* ``state_confirmed`` — the sentinel is **absent**, but exit code, freshly
  fetched remote tip, pinned-base ancestry, repository/write-scope identity and
  every required verification receipt independently confirm the pushed
  candidate. This is the recovered false negative; it is never decided by
  arbitrary ``PASS`` text.
* ``failed`` — positive evidence of failure: a signal or non-``exited``
  termination, a non-zero exit, recorded terminal failure evidence, a sentinel
  that does not resolve on the declared remote branch (local-only/unpushed), a
  non-descending commit, a forbidden diff, a failed verification receipt, or a
  wrong repository identity.
* ``inconclusive`` — no way to confirm (and no positive proof of failure):
  missing sentinel with no freshly fetched remote tip, a tip equal to the
  pinned base, a diverged/local-only tip, missing verification receipts, or an
  unknown/absent explicit review verdict.

The resolver is Git-agnostic and provider-neutral: no model, router, harness or
provider identifier appears here. It consumes *observed facts* (a
:class:`RemoteState`, a :class:`DiffEvidence`, verification receipts) rather
than shelling out, so a caller can back each fact with a real probe. Where the
older completion contract is a pure function of a process's exit/output
(``skillweave.runtime.verify``), this resolver adds the repository state that a
success token cannot prove on its own.

Read-only review lanes are handled by :meth:`LaneOutcomeResolver.resolve_review`
under a *distinct* rule: a review can never pass because a commit changed. It
requires an explicit binary verdict (``REVIEW_PASS``/``REVIEW_FAIL``) **and**
zero changes to the reviewed surface.

When the missing-sentinel fallback is used, the outcome carries
:data:`WARNING_MISSING_SENTINEL_RECOVERY`, preserves the stdout/stderr SHA-256
digests for later inspection, and can be emitted as a metadata-only warning
event through :func:`emit_lane_outcome_warning`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from skillweave.dispatch.work_contract import WriteScope, sha256_digest

#: The contract version. Recorded on every outcome so a consumer can tell which
#: revision of the sentinel/state rules produced a verdict.
LANE_OUTCOME_CONTRACT_VERSION = "1"

#: The four and only four lane outcome states.
SENTINEL_CONFIRMED = "sentinel_confirmed"
STATE_CONFIRMED = "state_confirmed"
FAILED = "failed"
INCONCLUSIVE = "inconclusive"

LANE_OUTCOME_STATES: tuple[str, ...] = (
    SENTINEL_CONFIRMED,
    STATE_CONFIRMED,
    FAILED,
    INCONCLUSIVE,
)

#: The warning marker recorded when a lane is confirmed without a sentinel.
WARNING_MISSING_SENTINEL_RECOVERY = "missing_sentinel_recovery"

#: The only explicit binary review verdicts. A review lane passes on nothing
#: less than one of these *plus* zero reviewed-surface changes.
REVIEW_PASS = "REVIEW_PASS"
REVIEW_FAIL = "REVIEW_FAIL"
REVIEW_VERDICTS: tuple[str, ...] = (REVIEW_PASS, REVIEW_FAIL)

#: ``OPS_READY <full-40-hex-SHA>``. The whitespace requirement deliberately
#: excludes the neighbouring ``OPS_READY_FOR_REVIEW`` review token, and the
#: lookahead refuses a 40-hex *prefix* of a longer token, so the sentinel is
#: never an ambiguous partial match.
_SENTINEL_RE = re.compile(r"OPS_READY\s+([0-9a-fA-F]{40})(?![0-9a-fA-F])")

_TERMINAL_FAILURE_EXITED = "exited"


class LaneOutcomeError(ValueError):
    """A lane outcome was malformed, or its evidence contract was violated.

    Raised on construction of the evidence objects (before any resolution), and
    names the offending field via ``field`` so the refusal is attributable.
    """

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


def _is_full_sha(value: Any) -> bool:
    """A full SHA is exactly 40 hexadecimal characters, not a branch name."""
    if not isinstance(value, str) or len(value) != 40:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def parse_sentinel(stdout: Any) -> Optional[str]:
    """Return the full sentinel SHA in ``stdout``, or ``None`` when absent.

    The sentinel is ``OPS_READY <full-40-hex-SHA>``. Only a full 40-hex token
    counts: an arbitrary ``PASS`` substring, a short ref, or a branch name is
    not a sentinel. The result is normalised to lowercase.
    """
    if stdout is None:
        return None
    if isinstance(stdout, (bytes, bytearray)):
        text = bytes(stdout).decode("utf-8", errors="replace")
    else:
        text = str(stdout)
    match = _SENTINEL_RE.search(text)
    return match.group(1).lower() if match else None


# ── Evidence objects ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RemoteState:
    """The freshly fetched state of the declared remote branch.

    ``tip_sha`` is the branch's current remote tip (full 40-hex).
    ``freshly_fetched`` records that the tip was fetched *now*, not read from a
    stale local ref. ``descends_from_base`` records that the tip descends from
    the pinned base (verified ancestry). ``contains`` optionally lists the other
    SHAs reachable on the branch, so a sentinel naming an older pushed commit
    can still resolve.
    """

    repo: str
    branch: str
    tip_sha: str
    freshly_fetched: bool = False
    descends_from_base: bool = False
    contains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.repo, str) or not self.repo.strip():
            raise LaneOutcomeError(
                "'remote.repo' must be a non-empty string", field="remote.repo"
            )
        if not isinstance(self.branch, str) or not self.branch.strip():
            raise LaneOutcomeError(
                "'remote.branch' must be a non-empty string", field="remote.branch"
            )
        if not _is_full_sha(self.tip_sha):
            raise LaneOutcomeError(
                f"'remote.tip_sha' must be a full 40-hex SHA, got {self.tip_sha!r}",
                field="remote.tip_sha",
            )
        for sha in self.contains:
            if not _is_full_sha(sha):
                raise LaneOutcomeError(
                    f"'remote.contains' holds a non-full SHA {sha!r}",
                    field="remote.contains",
                )

    def has(self, sha: str) -> bool:
        """True when ``sha`` resolves on this remote branch (tip or reachable)."""
        return sha == self.tip_sha or sha in self.contains


@dataclass(frozen=True)
class VerificationReceipt:
    """One lane-required verification receipt and its outcome.

    ``receipt_type`` names the check (for example a test suite); ``passed`` is
    its binary result. A receipt that failed can never be read as success.
    """

    receipt_type: str
    passed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.receipt_type, str) or not self.receipt_type.strip():
            raise LaneOutcomeError(
                "'receipt_type' must be a non-empty string",
                field="receipt.receipt_type",
            )


@dataclass(frozen=True)
class DiffEvidence:
    """The paths a lane's candidate changed, for write-scope checking."""

    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for path in self.changed_paths:
            if not isinstance(path, str) or not path.strip():
                raise LaneOutcomeError(
                    "'changed_paths' entries must be non-empty strings",
                    field="diff.changed_paths",
                )


@dataclass
class LaneEvidence:
    """Every fact needed to resolve one lane's outcome.

    All fields are observed values, not claims: the process result, the fetched
    remote state, the candidate's diff, the lane's verification receipts, and
    any terminal failure evidence the run recorded.
    """

    exit_code: Optional[int] = None
    termination: str = _TERMINAL_FAILURE_EXITED
    signal: Optional[int] = None
    stdout: bytes = b""
    stderr: bytes = b""
    remote: Optional[RemoteState] = None
    diff: DiffEvidence = field(default_factory=DiffEvidence)
    receipts: tuple[VerificationReceipt, ...] = ()
    failure_evidence: tuple[str, ...] = ()


# ── The outcome record ──────────────────────────────────────────────────────


@dataclass
class LaneOutcome:
    """The versioned verdict for one lane, with every evidence source recorded.

    ``sha`` is the confirmed candidate SHA (``None`` when unconfirmed).
    ``evidence_sources`` names every input that fed the decision, so the verdict
    is reviewable rather than a bare state. ``warning`` is set when a non-primary
    path (the missing-sentinel fallback) was used, and the stdout/stderr digests
    are preserved alongside it for later inspection.
    """

    state: str
    version: str = LANE_OUTCOME_CONTRACT_VERSION
    sha: Optional[str] = None
    reasons: list[str] = field(default_factory=list)
    evidence_sources: list[str] = field(default_factory=list)
    warning: Optional[str] = None
    stdout_digest: str = ""
    stderr_digest: str = ""
    review_verdict: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.state not in LANE_OUTCOME_STATES:
            raise LaneOutcomeError(
                f"unknown lane outcome state {self.state!r}; expected one of "
                f"{list(LANE_OUTCOME_STATES)}",
                field="outcome.state",
            )

    @property
    def confirmed(self) -> bool:
        """True only for the two positively-confirmed states."""
        return self.state in (SENTINEL_CONFIRMED, STATE_CONFIRMED)

    def warning_event(self) -> Optional[dict[str, Any]]:
        """The metadata-only warning payload for a fallback-confirmed outcome.

        Returns ``None`` when no warning applies. The payload names the warning,
        the confirmed state and version, and the preserved stdout/stderr digests
        — never the raw bytes.
        """
        if self.warning is None:
            return None
        return {
            "warning": self.warning,
            "lane_outcome": self.state,
            "lane_outcome_contract_version": self.version,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "version": self.version,
            "sha": self.sha,
            "confirmed": self.confirmed,
            "reasons": list(self.reasons),
            "evidence_sources": list(self.evidence_sources),
            "warning": self.warning,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "review_verdict": self.review_verdict,
            "created_at": self.created_at,
        }


# ── The resolver ────────────────────────────────────────────────────────────


class LaneOutcomeResolver:
    """Resolve one lane's outcome from its declared identity and observed facts.

    Constructed per lane with the lane's declared repository, remote branch and
    pinned base, plus the write scope that bounds its diff and (optionally) the
    verification receipt types the lane must produce and an ancestry oracle.

    ``is_ancestor(ancestor, descendant)`` is an optional callable that verifies
    pinned-base ancestry for a sentinel naming a non-tip commit (for example a
    real ``git merge-base --is-ancestor`` probe). When it is not supplied, a
    non-tip sentinel cannot have its ancestry proven and resolves
    ``inconclusive`` rather than being guessed at.
    """

    VERSION = LANE_OUTCOME_CONTRACT_VERSION

    def __init__(
        self,
        *,
        repo: str,
        remote_branch: str,
        pinned_base: str,
        write_scope: Optional[WriteScope] = None,
        required_receipts: Sequence[str] = (),
        is_ancestor: Optional[Callable[[str, str], bool]] = None,
    ):
        if not isinstance(repo, str) or not repo.strip():
            raise LaneOutcomeError(
                "'repo' must be a non-empty string", field="repo"
            )
        if not isinstance(remote_branch, str) or not remote_branch.strip():
            raise LaneOutcomeError(
                "'remote_branch' must be a non-empty string", field="remote_branch"
            )
        if not _is_full_sha(pinned_base):
            raise LaneOutcomeError(
                f"'pinned_base' must be a full 40-hex SHA, got {pinned_base!r}",
                field="pinned_base",
            )
        self.repo = repo
        self.remote_branch = remote_branch
        self.pinned_base = pinned_base
        self.write_scope = write_scope if write_scope is not None else WriteScope()
        self.required_receipt_types = tuple(str(r) for r in required_receipts)
        self.is_ancestor = is_ancestor

    # -- helpers ------------------------------------------------------------

    def _forbidden_paths(self, diff: DiffEvidence) -> list[str]:
        return [p for p in diff.changed_paths if not self.write_scope.permits(p)]

    def _receipt_state(
        self, receipts: Sequence[VerificationReceipt]
    ) -> tuple[str, list[str]]:
        """Classify the lane's receipts: pass, failed, or inconclusive (missing).

        A required receipt that is *present and failed* is positive failure
        evidence. A required receipt that is *absent* is inconclusive — never
        silently treated as passing.
        """
        by_type = {r.receipt_type: r for r in receipts}
        failed = [
            t for t in self.required_receipt_types
            if t in by_type and not by_type[t].passed
        ]
        if failed:
            return FAILED, [f"verification receipt failed: {t}" for t in failed]
        missing = [t for t in self.required_receipt_types if t not in by_type]
        if missing:
            return INCONCLUSIVE, [f"missing required verification receipt: {t}" for t in missing]
        return SENTINEL_CONFIRMED, []  # sentinel_confirmed is the "all good" sentinel here

    @staticmethod
    def _digests(evidence: LaneEvidence) -> tuple[str, str]:
        return (
            sha256_digest(evidence.stdout or b""),
            sha256_digest(evidence.stderr or b""),
        )

    def _result(
        self,
        state: str,
        *,
        sha: Optional[str],
        reasons: Sequence[str],
        sources: Sequence[str],
        stdout_digest: str,
        stderr_digest: str,
        warning: Optional[str] = None,
        review_verdict: Optional[str] = None,
    ) -> LaneOutcome:
        return LaneOutcome(
            state=state,
            version=self.VERSION,
            sha=sha,
            reasons=list(reasons),
            evidence_sources=list(sources),
            warning=warning,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            review_verdict=review_verdict,
        )

    # -- ops lanes ----------------------------------------------------------

    def resolve(self, evidence: LaneEvidence) -> LaneOutcome:
        """Resolve an ops lane's outcome from its evidence."""
        stdout_digest, stderr_digest = self._digests(evidence)
        sentinel = parse_sentinel(evidence.stdout)
        sources: list[str] = ["process.exit_code", "process.termination", "process.stdout"]
        sources.append("sentinel" if sentinel is not None else "fallback.missing_sentinel")

        # Positive terminal failure evidence first: a signalled/aborted process,
        # a non-zero exit, or a recorded terminal failure can never confirm.
        if evidence.termination != _TERMINAL_FAILURE_EXITED or evidence.signal is not None:
            return self._result(
                FAILED, sha=None,
                reasons=[f"termination={evidence.termination} signal={evidence.signal}"],
                sources=sources + ["process.signal"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if evidence.exit_code != 0:
            return self._result(
                FAILED, sha=None,
                reasons=[f"non-zero exit {evidence.exit_code}"],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if evidence.failure_evidence:
            return self._result(
                FAILED, sha=None,
                reasons=[f"terminal failure evidence: {list(evidence.failure_evidence)}"],
                sources=sources + ["failure_evidence"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )

        if sentinel is not None:
            return self._resolve_sentinel(
                sentinel, evidence, sources, stdout_digest, stderr_digest
            )
        return self._resolve_fallback(
            evidence, sources, stdout_digest, stderr_digest
        )

    def _resolve_sentinel(
        self,
        sentinel: str,
        evidence: LaneEvidence,
        sources: list[str],
        stdout_digest: str,
        stderr_digest: str,
    ) -> LaneOutcome:
        sources = sources + ["remote"]
        remote = evidence.remote
        if remote is None:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=["sentinel present but no remote state was supplied to verify it"],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if remote.repo != self.repo:
            return self._result(
                FAILED, sha=None,
                reasons=[
                    f"wrong repository identity: sentinel repo {remote.repo!r} != "
                    f"declared {self.repo!r}"
                ],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if remote.branch != self.remote_branch:
            return self._result(
                FAILED, sha=None,
                reasons=[
                    f"wrong remote branch: {remote.branch!r} != declared "
                    f"{self.remote_branch!r}"
                ],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if not remote.freshly_fetched:
            return self._result(
                FAILED, sha=None,
                reasons=["declared remote branch was not freshly fetched; sentinel is unverifiable"],
                sources=sources + ["remote.freshness"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if not remote.has(sentinel):
            return self._result(
                FAILED, sha=None,
                reasons=[
                    f"sentinel {sentinel} does not resolve on declared remote branch "
                    f"{self.remote_branch!r} (local-only or unpushed commit)"
                ],
                sources=sources + ["remote.resolution"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if sentinel == remote.tip_sha:
            descends = remote.descends_from_base
        elif self.is_ancestor is not None:
            descends = bool(self.is_ancestor(self.pinned_base, sentinel))
        else:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=[
                    f"sentinel {sentinel} is not the fetched tip and its ancestry "
                    "cannot be verified"
                ],
                sources=sources + ["ancestry"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if not descends:
            return self._result(
                FAILED, sha=None,
                reasons=[f"sentinel {sentinel} does not descend from pinned base {self.pinned_base}"],
                sources=sources + ["ancestry"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        forbidden = self._forbidden_paths(evidence.diff)
        if forbidden:
            return self._result(
                FAILED, sha=None,
                reasons=[f"forbidden diff outside write scope: {forbidden}"],
                sources=sources + ["write_scope"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        receipt_state, receipt_reasons = self._receipt_state(evidence.receipts)
        if receipt_state == FAILED:
            return self._result(
                FAILED, sha=None, reasons=receipt_reasons,
                sources=sources + ["write_scope", "verification_receipts"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if receipt_state == INCONCLUSIVE:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=receipt_reasons + ["cannot confirm the sentinel without every receipt"],
                sources=sources + ["write_scope", "verification_receipts"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        return self._result(
            SENTINEL_CONFIRMED, sha=sentinel,
            reasons=["sentinel resolves on declared remote branch and descends from pinned base"],
            sources=sources + ["remote.resolution", "ancestry", "write_scope", "verification_receipts"],
            stdout_digest=stdout_digest, stderr_digest=stderr_digest,
        )

    def _resolve_fallback(
        self,
        evidence: LaneEvidence,
        sources: list[str],
        stdout_digest: str,
        stderr_digest: str,
    ) -> LaneOutcome:
        """Missing-sentinel recovery: confirm only from independent state facts."""
        sources = sources + ["remote"]
        remote = evidence.remote
        if remote is None:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=["missing sentinel and no remote state to confirm a candidate"],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if remote.repo != self.repo:
            return self._result(
                FAILED, sha=None,
                reasons=[
                    f"wrong repository identity: {remote.repo!r} != declared {self.repo!r}"
                ],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if remote.branch != self.remote_branch:
            return self._result(
                FAILED, sha=None,
                reasons=[
                    f"wrong remote branch: {remote.branch!r} != declared "
                    f"{self.remote_branch!r}"
                ],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if not remote.freshly_fetched:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=["remote branch was not freshly fetched; cannot confirm a pushed candidate"],
                sources=sources + ["remote.freshness"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if remote.tip_sha == self.pinned_base:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=["remote tip equals the pinned base; no pushed candidate (local-only or unpushed)"],
                sources=sources + ["remote.tip"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if not remote.descends_from_base:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=[
                    f"remote tip {remote.tip_sha} does not descend from pinned base "
                    f"{self.pinned_base} (diverged or local-only)"
                ],
                sources=sources + ["ancestry"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        forbidden = self._forbidden_paths(evidence.diff)
        if forbidden:
            return self._result(
                FAILED, sha=None,
                reasons=[f"forbidden diff outside write scope: {forbidden}"],
                sources=sources + ["ancestry", "write_scope"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        receipt_state, receipt_reasons = self._receipt_state(evidence.receipts)
        if receipt_state == FAILED:
            return self._result(
                FAILED, sha=None, reasons=receipt_reasons,
                sources=sources + ["ancestry", "write_scope", "verification_receipts"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if receipt_state == INCONCLUSIVE:
            return self._result(
                INCONCLUSIVE, sha=None, reasons=receipt_reasons,
                sources=sources + ["ancestry", "write_scope", "verification_receipts"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        return self._result(
            STATE_CONFIRMED, sha=remote.tip_sha,
            reasons=[
                "missing sentinel recovered from exit 0 + freshly fetched remote tip "
                "descending from pinned base, allowed diff, and every verification receipt"
            ],
            sources=sources + ["remote.tip", "ancestry", "write_scope", "verification_receipts"],
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            warning=WARNING_MISSING_SENTINEL_RECOVERY,
        )

    # -- read-only review lanes ---------------------------------------------

    def resolve_review(
        self,
        evidence: LaneEvidence,
        *,
        verdict: Optional[str],
        changed_paths: Sequence[str] = (),
    ) -> LaneOutcome:
        """Resolve a read-only review lane under the distinct review rule.

        A review can never pass because a commit changed: any change to the
        reviewed surface is a failure regardless of verdict. A pass additionally
        requires an *explicit* binary verdict (``REVIEW_PASS``). The verdict is
        never inferred from stdout text, so an arbitrary ``REVIEW_PASS``
        substring is not a verdict and leaves the outcome ``inconclusive``.
        """
        stdout_digest, stderr_digest = self._digests(evidence)
        changes = tuple(changed_paths) if changed_paths else tuple(evidence.diff.changed_paths)
        sources: list[str] = ["process.exit_code", "process.termination", "review.verdict"]

        if evidence.termination != _TERMINAL_FAILURE_EXITED or evidence.signal is not None:
            return self._result(
                FAILED, sha=None,
                reasons=[f"termination={evidence.termination} signal={evidence.signal}"],
                sources=sources + ["process.signal"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if evidence.exit_code != 0:
            return self._result(
                FAILED, sha=None, reasons=[f"non-zero exit {evidence.exit_code}"],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if changes:
            return self._result(
                FAILED, sha=None,
                reasons=[
                    f"read-only review changed the reviewed surface: {list(changes)}; "
                    "a changed commit never proves review PASS"
                ],
                sources=sources + ["review.reviewed_surface"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
                review_verdict=verdict,
            )
        if verdict is None:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=["no explicit binary review verdict supplied"],
                sources=sources, stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if verdict not in REVIEW_VERDICTS:
            return self._result(
                INCONCLUSIVE, sha=None,
                reasons=[
                    f"unknown review verdict {verdict!r}; only REVIEW_PASS/REVIEW_FAIL "
                    "are explicit binary verdicts"
                ],
                sources=sources + ["review.verdict"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            )
        if verdict == REVIEW_FAIL:
            return self._result(
                FAILED, sha=None, reasons=["reviewer verdict REVIEW_FAIL"],
                sources=sources + ["review.verdict"],
                stdout_digest=stdout_digest, stderr_digest=stderr_digest,
                review_verdict=verdict,
            )
        return self._result(
            SENTINEL_CONFIRMED, sha=None,
            reasons=["explicit REVIEW_PASS verdict with zero reviewed-surface changes"],
            sources=sources + ["review.verdict", "review.reviewed_surface"],
            stdout_digest=stdout_digest, stderr_digest=stderr_digest,
            review_verdict=verdict,
        )


# ── Warning emission (metadata-only) ────────────────────────────────────────


def emit_lane_outcome_warning(
    stream: Any,
    *,
    wave: str,
    lane_id: str,
    dispatch_id: str,
    outcome: LaneOutcome,
) -> Optional[Any]:
    """Emit a metadata-only warning event when the fallback confirmed a lane.

    The event carries the warning marker, the confirmed state and contract
    version, and the preserved stdout/stderr **digests** — never the raw bytes,
    which the event stream refuses outright. Returns ``None`` when the outcome
    carries no warning. The dispatch contract's event vocabulary has no generic
    ``warning`` type, so the warning rides on ``evidence_recorded`` with
    ``evidence_status='warning'`` and a ``warning`` payload key; this keeps the
    stream's metadata-only guarantee intact without widening the dispatch
    surface.
    """
    payload = outcome.warning_event()
    if payload is None:
        return None
    from skillweave.dispatch.contracts import EventType, ProcessStatus, TaskStatus

    return stream.emit(
        wave=wave,
        lane_id=lane_id,
        dispatch_id=dispatch_id,
        event_type=EventType.EVIDENCE_RECORDED,
        process_status=ProcessStatus.EXITED,
        task_status=TaskStatus.DONE,
        evidence_status="warning",
        receipt_refs=[f"lane-outcome:{outcome.state}"],
        payload=payload,
    )


__all__ = [
    "LANE_OUTCOME_CONTRACT_VERSION",
    "LANE_OUTCOME_STATES",
    "SENTINEL_CONFIRMED",
    "STATE_CONFIRMED",
    "FAILED",
    "INCONCLUSIVE",
    "WARNING_MISSING_SENTINEL_RECOVERY",
    "REVIEW_PASS",
    "REVIEW_FAIL",
    "REVIEW_VERDICTS",
    "LaneOutcomeError",
    "LaneOutcome",
    "LaneEvidence",
    "RemoteState",
    "VerificationReceipt",
    "DiffEvidence",
    "LaneOutcomeResolver",
    "parse_sentinel",
    "emit_lane_outcome_warning",
]
