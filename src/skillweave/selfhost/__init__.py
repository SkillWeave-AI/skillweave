"""Internal self-hosting entry (SW-SELFHOST-001).

SkillWeave drives its own small Ops/Review sequence with no manual worktree or
session control. ``SelfHostRunner`` is that entry: it takes a declarative
``SelfHostFixture`` (two Ops lanes, two reviews, one dependent lane) and runs it
through the canonical path — real subprocesses via the fan-out/run-service seam,
coordinator-governed root cursor, and pinned-SHA-gated reviews — producing
nothing a human had to hand-wire.

The runner performs *no* manual worktree/session control: worktrees come from
``workspace.GitWorktreeProvider``, the root cursor from ``coordinator.Coordinator``,
reviews from ``review.ReviewGate``, and the actual execution from the real
subprocess seam. The fixture is data; the runner wires it.
"""

from .runner import (
    SelfHostRunner,
    SelfHostResult,
    SelfHostFixture,
    LaneSpec,
    ReviewSpec,
)
from .outcome import (
    LANE_OUTCOME_CONTRACT_VERSION,
    LANE_OUTCOME_STATES,
    SENTINEL_CONFIRMED,
    STATE_CONFIRMED,
    FAILED,
    INCONCLUSIVE,
    WARNING_MISSING_SENTINEL_RECOVERY,
    REVIEW_PASS,
    REVIEW_FAIL,
    REVIEW_VERDICTS,
    DiffEvidence,
    LaneEvidence,
    LaneOutcome,
    LaneOutcomeError,
    LaneOutcomeResolver,
    RemoteState,
    VerificationReceipt,
    emit_lane_outcome_warning,
    parse_sentinel,
)

__all__ = [
    "SelfHostRunner",
    "SelfHostResult",
    "SelfHostFixture",
    "LaneSpec",
    "ReviewSpec",
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
    "DiffEvidence",
    "LaneEvidence",
    "LaneOutcome",
    "LaneOutcomeError",
    "LaneOutcomeResolver",
    "RemoteState",
    "VerificationReceipt",
    "emit_lane_outcome_warning",
    "parse_sentinel",
]
