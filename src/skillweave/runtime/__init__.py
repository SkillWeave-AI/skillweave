import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .store import RunStore, SQLiteRunStore, RunRecord, RunStateModel
from .errors import InvalidTransitionError, VersionConflictError, StoreError
from .journal import EventJournal, JournalEvent, EventType
from .schema.vocabulary import StatusVocabulary, StatusSchema, AmendmentRecord, validate_status, StatusRejectedError
from .authority import (
    Role, RoleAssignment, HumanApproval, DelegationRecord,
    ROLE_CAPABILITY_MATRIX, AuthorityGuard, AuthorityError,
)
from .registry import (
    EvidenceType, EvidenceQualityAxis, EvidenceQuality,
    ArtifactReceipt, EvidenceFinding, EvidenceRegistry,
    MerkleSegment, _compute_merkle_root, _compute_segment_hash,
    RawArtifactStore, ArtifactIntegrityError,
)
from .preflight import (
    SessionEnvelope,
    PreflightResult,
    PreflightFailure,
    FailureClass,
    Retryability,
    classify_failure,
    digest_target,
    failure_for_missing_path,
    failure_for_wrong_language,
    run_preflight,
)
from .semantic_repair import (
    ALLOWED_ACTION,
    ATTEMPT_EVENT_SCHEMA_ID,
    ATTEMPT_EVENT_SCHEMA_VERSION,
    BoundedRepairer,
    ErrorClass,
    HoldReason,
    RepairAction,
    RepairAttempt,
    RepairOutcome,
    UnsupportedAttemptEventVersion,
    UnsupportedErrorClass,
    allowed_action,
    classify_error,
    collect_grounding_evidence,
    detect_surface_failure,
    language_of,
    repair_plan_from_grounding,
    validate_attempt_event,
)
from .handoff import ColdStartBundle, HandoffBroker, HandoffOffer, HandoffError
from .observer import (
    OutputType, ObserverOutput, ObserverState, ObserverLease,
    Detector, ObserverRuntime,
)
from .wireframe import (
    assert_gate_discipline, assert_write_scope, assert_non_polling,
    assert_no_foreign_repos, validate_summary, WireframeError,
)
from . import context
from .checkpoint import (
    EnvironmentFingerprint, Checkpoint, ResumeRevalidationRequired,
    capture_environment, create_checkpoint, validate_resume,
)

# ── Human-coupling gate for irreversible change surfaces ────────────────────

IRREVERSIBLE_SURFACES: frozenset[str] = frozenset({
    "organization",
    "human",
    "finance",
    "legal",
    "public_channel",
})

# Human-coupling levels that already embed human oversight. These levels are
# allowed to modify irreversible surfaces without additional gating because a
# human is already in the loop.
_HUMAN_IN_THE_LOOP: frozenset[str] = frozenset({
    "approval_required",
    "collaborative",
    "human_led",
})


def assert_human_coupling_gate(
    human_coupling: str,
    change_surfaces: list[str],
) -> list[dict[str, str]]:
    """Check that *human_coupling* gates every irreversible *change_surface*.

    Returns a list of violation dicts (one per un-gated irreversible surface)
    or an empty list when all surfaces are properly gated. The check is
    category-independent: it inspects the human-coupling level alone, never
    the category name — a ``build`` profile with ``humanCoupling: autonomous``
    is gated exactly as hard as a ``research`` profile with the same coupling.
    """
    violations: list[dict[str, str]] = []
    if human_coupling in _HUMAN_IN_THE_LOOP:
        return violations
    for surface in change_surfaces:
        if surface in IRREVERSIBLE_SURFACES:
            violations.append({
                "surface": surface,
                "human_coupling": human_coupling,
                "reason": (
                    f"surface '{surface}' is irreversible and requires "
                    f"human coupling 'approval_required', 'collaborative', "
                    f"or 'human_led', got '{human_coupling}'"
                ),
            })
    return violations


# ── Reversibility is a property of the surface, not of the category ─────────
#
# The coupling a run needs is derived from what a run can actually touch and
# whether touching it can be undone. The product or category label ("build",
# "research", ...) never enters the derivation: a ``build`` profile that
# declares ``public_channel`` is treated exactly like a ``research`` profile
# that declares ``public_channel``. ``derive_human_coupling`` is the single
# source of truth for this mapping, and ``assert_human_coupling_gate`` /
# ``authorize_mutation`` are its enforcement seams.
REVERSIBILITY_BY_SURFACE: dict[str, str] = {
    # Reversible surfaces: no human authority is required to mutate them.
    "code": "reversible",
    "configuration": "reversible",
    "infrastructure": "reversible",
    "documents": "reversible",
    "knowledge": "reversible",
    "data": "reversible",
    # An external system is mutated through a revocable/compensatable call, so
    # it is reversible for gating purposes; the coupling it needs is still
    # derived from the other surfaces it is combined with.
    "external_system": "reversible",
    # Irreversible surfaces: a mutation cannot be recalled once it lands.
    "organization": "irreversible",
    "human": "irreversible",
    "finance": "irreversible",
    "legal": "irreversible",
    "public_channel": "irreversible",
}

#: Coupling level assigned when every affected surface is reversible.
_REVERSIBLE_COUPLING = "supervised"


def surface_reversibility(surface: str) -> str:
    """Return ``"irreversible"`` or ``"reversible"`` for *surface*.

    An unknown surface is treated as irreversible — an unrecognised mutation
    target fails closed rather than silently being allowed.
    """
    return REVERSIBILITY_BY_SURFACE.get(surface, "irreversible")


def derive_human_coupling(change_surfaces: list[str]) -> str:
    """Derive the required human coupling from the affected surfaces.

    The result depends only on *change_surfaces* and their reversibility —
    never on a category, product, or profile label. Any irreversible surface
    raises the requirement to ``approval_required``; an all-reversible surface
    set needs only ``supervised``.
    """
    if any(surface_reversibility(s) == "irreversible" for s in change_surfaces):
        return "approval_required"
    return _REVERSIBLE_COUPLING


# ── Profile selection: explainable, with rejected alternatives ──────────────
#
# A selection is only acceptable when it reports *why*: the matched evidence,
# a confidence, and the alternatives it rejected. Below a threshold a selection
# must not proceed silently — it either asks a bounded set of clarification
# questions (when the ambiguity is resolvable) or returns a typed hold (when it
# is not). This is deliberately data-first: the caller supplies the signals and
# the registry of candidates; the runtime owns the arbitration and the honesty.

#: Confidence below which a selection may not proceed unchallenged.
SELECTION_CONFIDENCE_THRESHOLD = 0.70

#: Hard ceiling on clarification questions. Bounded means bounded — a
#: low-confidence selection can never spray an unbounded question list.
MAX_CLARIFICATION_QUESTIONS = 3


def _finite_non_negative(value: Any) -> bool:
    """True when *value* is a real, finite, non-negative number."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0.0
    )


@dataclass
class SelectionEvidence:
    """One signal that matched (or failed to match) a candidate profile."""

    signal: str
    value: str
    matched: bool
    weight: float = 1.0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "value": self.value,
            "matched": self.matched,
            "weight": self.weight,
            "detail": self.detail,
        }


@dataclass
class ProfileCandidate:
    """A profile that could be selected, with the evidence for and against it."""

    profile_id: str
    category: str
    change_surfaces: list[str]
    matched: list[SelectionEvidence] = field(default_factory=list)
    unmatched: list[SelectionEvidence] = field(default_factory=list)

    @property
    def evidence(self) -> list[SelectionEvidence]:
        return self.matched + self.unmatched

    @property
    def weight(self) -> float:
        # A negative or non-finite weight is not usable as evidence: ignore it
        # rather than let it invert or inflate the ranking.
        return sum(
            e.weight
            for e in self.matched
            if _finite_non_negative(e.weight)
        )

    @property
    def confidence(self) -> float:
        total = sum(
            e.weight for e in self.evidence if _finite_non_negative(e.weight)
        )
        if not math.isfinite(total) or total <= 0.0:
            return 0.0
        share = self.weight / total
        if not math.isfinite(share):
            return 0.0
        # Confidence is a probability: clamp it into [0, 1] so no pathological
        # weight can push a selection over the threshold.
        return min(1.0, max(0.0, share))

    def rejection_reason(self, winner_id: str) -> str:
        if self.profile_id == winner_id:
            return ""
        if not self.matched:
            return (
                f"candidate '{self.profile_id}' matched no selection evidence; "
                f"'{winner_id}' matched more"
            )
        return (
            f"candidate '{self.profile_id}' carried {self.weight:.2f} matched "
            f"weight, less than the selected '{winner_id}'"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "category": self.category,
            "change_surfaces": list(self.change_surfaces),
            "matched": [e.to_dict() for e in self.matched],
            "unmatched": [e.to_dict() for e in self.unmatched],
            "weight": self.weight,
            "confidence": self.confidence,
        }


@dataclass
class ClarificationQuestion:
    """A bounded question asked when a low-confidence selection is resolvable."""

    question_id: str
    prompt: str
    options: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "prompt": self.prompt,
            "options": list(self.options),
            "reason": self.reason,
        }


@dataclass
class TypedHold:
    """A typed, non-proceeding outcome: the runtime refused to select.

    A hold is explicit and machine-readable, never a silent default. It names
    the competing profiles and the evidence gap, so a caller can resolve it and
    re-run selection.
    """

    hold_reason: str
    confidence: float
    tied_profiles: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    questions: list[ClarificationQuestion] = field(default_factory=list)

    @property
    def is_hold(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "TypedHold",
            "hold_reason": self.hold_reason,
            "confidence": self.confidence,
            "tied_profiles": list(self.tied_profiles),
            "missing_evidence": list(self.missing_evidence),
            "questions": [q.to_dict() for q in self.questions],
        }


@dataclass
class ProfileSelection:
    """The explainable result of selecting a profile over its alternatives.

    Carries matched evidence, a confidence, and the rejected alternatives.
    ``selected`` is False and ``hold`` is set when the runtime would not
    proceed — either because the confidence fell below the threshold or because
    two candidates carried identical evidence and could not be separated.
    """

    selected: bool
    profile_id: Optional[str] = None
    category: Optional[str] = None
    confidence: float = 0.0
    matched_evidence: list[SelectionEvidence] = field(default_factory=list)
    rejected: list[ProfileCandidate] = field(default_factory=list)
    clarification_questions: list[ClarificationQuestion] = field(default_factory=list)
    hold: Optional[TypedHold] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "profile_id": self.profile_id,
            "category": self.category,
            "confidence": self.confidence,
            "matched_evidence": [e.to_dict() for e in self.matched_evidence],
            "rejected": [c.to_dict() for c in self.rejected],
            "clarification_questions": [
                q.to_dict() for q in self.clarification_questions
            ],
            "hold": self.hold.to_dict() if self.hold is not None else None,
        }


def _ask_clarification(
    candidates: list[ProfileCandidate],
) -> list[ClarificationQuestion]:
    """Build at most ``MAX_CLARIFICATION_QUESTIONS`` questions for *candidates*.

    The questions target the evidence gap between the leading candidates: each
    question asks which signal actually applies, so answering them resolves the
    ambiguity rather than reopening it.
    """
    questions: list[ClarificationQuestion] = []
    seen: set[str] = set()
    for candidate in candidates:
        for signal in {e.signal for e in candidate.matched}:
            if signal in seen:
                continue
            seen.add(signal)
            others = [
                c for c in candidates if c.profile_id != candidate.profile_id
            ]
            options = [candidate.profile_id] + [c.profile_id for c in others]
            questions.append(
                ClarificationQuestion(
                    question_id=f"q-{signal}",
                    prompt=(
                        f"Signal '{signal}' matched more than one candidate. "
                        f"Which profile should this run use?"
                    ),
                    options=options,
                    reason=(
                        f"'{signal}' is shared by {', '.join(options)} and "
                        f"does not separate them at "
                        f"{SELECTION_CONFIDENCE_THRESHOLD:.2f} confidence"
                    ),
                )
            )
            if len(questions) >= MAX_CLARIFICATION_QUESTIONS:
                return questions
    if not questions:
        questions.append(
            ClarificationQuestion(
                question_id="q-target",
                prompt=(
                    "No candidate profile matched strongly enough. What kind of "
                    "work is this run meant to do?"
                ),
                options=[c.profile_id for c in candidates],
                reason="selection confidence fell below the threshold",
            )
        )
    return questions[:MAX_CLARIFICATION_QUESTIONS]


def select_profile(
    candidates: list[ProfileCandidate],
    threshold: float = SELECTION_CONFIDENCE_THRESHOLD,
) -> ProfileSelection:
    """Select the best-matching profile and explain the decision.

    Reports matched evidence and confidence for the winner, and the rejected
    alternatives with the reason each lost. When the winner's confidence is
    below *threshold*, or when the top two candidates carry identical weight,
    nothing is selected: the result asks a bounded set of clarification
    questions when the tie is resolvable and otherwise returns a typed hold.
    """
    if not candidates:
        raise ValueError("select_profile requires at least one candidate")

    ranked = sorted(candidates, key=lambda c: (-c.weight, c.profile_id))
    winner = ranked[0]
    confidence = winner.confidence
    tied = [
        c for c in ranked[1:]
        if c.weight == winner.weight and c.weight > 0.0
    ]

    if tied:
        tied_profiles = [winner.profile_id] + [c.profile_id for c in tied]
        questions = _ask_clarification([winner] + tied)
        hold = TypedHold(
            hold_reason=(
                "selection is ambiguous: "
                + ", ".join(tied_profiles)
                + " carry identical matched evidence"
            ),
            confidence=confidence,
            tied_profiles=tied_profiles,
            missing_evidence=sorted({e.signal for e in winner.evidence}),
            questions=questions,
        )
        return ProfileSelection(
            selected=False,
            category=winner.category,
            confidence=confidence,
            matched_evidence=list(winner.matched),
            rejected=ranked[1:],
            clarification_questions=questions,
            hold=hold,
        )

    if confidence < threshold:
        questions = _ask_clarification([winner] + ranked[1:])
        hold = TypedHold(
            hold_reason=(
                f"selection confidence {confidence:.2f} is below the "
                f"threshold {threshold:.2f}"
            ),
            confidence=confidence,
            tied_profiles=[winner.profile_id],
            missing_evidence=[e.signal for e in winner.unmatched],
            questions=questions,
        )
        return ProfileSelection(
            selected=False,
            category=winner.category,
            confidence=confidence,
            matched_evidence=list(winner.matched),
            rejected=ranked[1:],
            clarification_questions=questions,
            hold=hold,
        )

    return ProfileSelection(
        selected=True,
        profile_id=winner.profile_id,
        category=winner.category,
        confidence=confidence,
        matched_evidence=list(winner.matched),
        rejected=ranked[1:],
        clarification_questions=[],
        hold=None,
    )


# ── Mutation authorization for irreversible surfaces ────────────────────────
#
# Reversibility, not the category, decides whether a mutation may proceed. A
# mutation that touches an irreversible surface is refused unless explicit
# human authority (a ``HumanApproval`` with decision ``approved``) is presented
# for the mutation's scope. The refusal happens *before* anything is written,
# so the surface is never touched speculatively.

#: Decisions that constitute explicit human authority.
_AUTHORIZING_DECISIONS: frozenset[str] = frozenset({"approved", "approve"})


def irreversible_surfaces_for(change_surfaces: list[str]) -> list[str]:
    """Return the subset of *change_surfaces* that are irreversible."""
    return [s for s in change_surfaces if surface_reversibility(s) == "irreversible"]


def authorize_mutation(
    actor: str,
    change_surface: str,
    approval: Optional[Any] = None,
    *,
    scope: str = "",
    mutation: str = "mutate",
) -> dict[str, Any]:
    """Authorize a mutation of *change_surface*, refusing irreversible ones.

    Args:
        actor: The actor attempting the mutation.
        change_surface: The surface the mutation would touch.
        approval: Explicit human authority. Expected to be an object exposing
            ``decision`` (or a mapping with a ``"decision"`` key) and a
            ``scope``; ``HumanApproval`` from ``runtime.authority`` is the
            canonical instance.
        scope: The scope the mutation applies to. Must be covered by the
            approval's scope when the surface is irreversible.
        mutation: A label for the mutation, used in the result.

    Returns:
        A dict describing the decision: ``{"authorized": bool, "surface": ...,
        "irreversibility": ..., "actor": ..., "mutation": ..., "reason": ...,
        "approval": ...}``. On an irreversible surface without authority the
        result is ``authorized=False`` and no mutation may proceed.

    Raises:
        ValueError: If *actor* or *change_surface* is empty.
    """
    if not actor:
        raise ValueError("authorize_mutation requires a non-empty actor")
    if not change_surface:
        raise ValueError("authorize_mutation requires a non-empty change_surface")

    irreversibility = surface_reversibility(change_surface)

    def _decision_of(a: Any) -> str:
        if a is None:
            return ""
        if isinstance(a, dict):
            return str(a.get("decision", ""))
        return str(getattr(a, "decision", ""))

    def _scope_of(a: Any) -> str:
        if a is None:
            return ""
        if isinstance(a, dict):
            return str(a.get("scope", ""))
        return str(getattr(a, "scope", ""))

    def _result(authorized: bool, reason: str) -> dict[str, Any]:
        return {
            "authorized": authorized,
            "surface": change_surface,
            "irreversibility": irreversibility,
            "actor": actor,
            "mutation": mutation,
            "scope": scope,
            "reason": reason,
            "approval": getattr(approval, "to_dict", lambda: approval)()
            if approval is not None else None,
        }

    if irreversibility == "reversible":
        return _result(True, f"surface '{change_surface}' is reversible; no human authority required")

    decision = _decision_of(approval)
    if decision not in _AUTHORIZING_DECISIONS:
        return _result(
            False,
            (
                f"surface '{change_surface}' is irreversible and requires "
                f"explicit human authority before mutation; got "
                f"{'no approval' if approval is None else f'decision {decision!r}'}"
            ),
        )

    approval_scope = _scope_of(approval)
    if scope and approval_scope and approval_scope != scope:
        return _result(
            False,
            (
                f"human authority for '{approval_scope}' does not cover the "
                f"mutation scope '{scope}'"
            ),
        )

    return _result(
        True,
        f"human authority presented for irreversible surface '{change_surface}'",
    )


from .planning_sync import (
    PlanningSyncBackingStore,
    SyncReport,
    discover_planning_repo,
    discover_planning_root,
    runtime_has_git,
    resolve_runtime_store,
    classify_runtime,
)
from .preflight import PreflightInterceptor, PreflightError as PreflightGateError
from .substrate import (
    BackingStore,
    GitBackingStore,
    LocalOnlyBackingStore,
    ResolvedArea,
    UnclassifiedAreaError,
    resolve_store,
    classify_area,
    classify_substrate,
)

__all__ = [
    "RunStore",
    "SQLiteRunStore",
    "RunRecord",
    "RunStateModel",
    "InvalidTransitionError",
    "VersionConflictError",
    "StoreError",
    "EventJournal",
    "JournalEvent",
    "EventType",
    "StatusVocabulary",
    "StatusSchema",
    "AmendmentRecord",
    "validate_status",
    "StatusRejectedError",
    "Role",
    "RoleAssignment",
    "HumanApproval",
    "DelegationRecord",
    "ROLE_CAPABILITY_MATRIX",
    "AuthorityGuard",
    "AuthorityError",
    "EvidenceType",
    "EvidenceQualityAxis",
    "EvidenceQuality",
    "ArtifactReceipt",
    "EvidenceFinding",
    "EvidenceRegistry",
    "MerkleSegment",
    "_compute_merkle_root",
    "_compute_segment_hash",
    "RawArtifactStore",
    "ArtifactIntegrityError",
    "SessionEnvelope",
    "PreflightResult",
    "run_preflight",
    "ColdStartBundle",
    "HandoffBroker",
    "HandoffOffer",
    "HandoffError",
    "OutputType",
    "ObserverOutput",
    "ObserverState",
    "ObserverLease",
    "Detector",
    "ObserverRuntime",
    "assert_gate_discipline",
    "assert_write_scope",
    "assert_non_polling",
    "assert_no_foreign_repos",
    "validate_summary",
    "WireframeError",
    "BackingStore",
    "GitBackingStore",
    "LocalOnlyBackingStore",
    "ResolvedArea",
    "UnclassifiedAreaError",
    "resolve_store",
    "classify_area",
    "classify_substrate",
    "PlanningSyncBackingStore",
    "SyncReport",
    "discover_planning_repo",
    "discover_planning_root",
    "runtime_has_git",
    "resolve_runtime_store",
    "classify_runtime",
    "IRREVERSIBLE_SURFACES",
    "assert_human_coupling_gate",
    "REVERSIBILITY_BY_SURFACE",
    "surface_reversibility",
    "derive_human_coupling",
    "irreversible_surfaces_for",
    "SELECTION_CONFIDENCE_THRESHOLD",
    "MAX_CLARIFICATION_QUESTIONS",
    "SelectionEvidence",
    "ProfileCandidate",
    "ClarificationQuestion",
    "TypedHold",
    "ProfileSelection",
    "select_profile",
    "authorize_mutation",
]
