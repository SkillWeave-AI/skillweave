"""Assay methodology — experimental, probe-based exploration with autonomy policies.

Append-only states (PIN, HYPOTHESIS, PROBE, MEASURE, CLASSIFY, REMEDIATE,
ESCALATE_OR_SPLIT, PASS, HOLD) with three autonomy policies:

* **Conservative**: approves every mutating batch and external/irreversible
  action; read-only probes run automatically.
* **Moderate**: approves plan/new waves, then self-heals within an approved
  reversible wave.
* **Unicorn**: retries, remediates, reschedules, and resolves capabilities
  inside declared reversible scopes; still holds for irreversible action,
  scope/authority expansion, missing authority, target drift, or budget
  exhaustion.

No model/router identities in this code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── Append-only states ──────────────────────────────────────────────────────

class AssayState(str, Enum):
    """Append-only states for the Assay methodology.

    Each value names a phase in the experimental lifecycle. New states may
    only be *appended* — never removed, renamed, or reordered.
    """

    PIN = "pin"
    HYPOTHESIS = "hypothesis"
    PROBE = "probe"
    MEASURE = "measure"
    CLASSIFY = "classify"
    REMEDIATE = "remediate"
    ESCALATE_OR_SPLIT = "escalate_or_split"
    PASS = "pass"
    HOLD = "hold"

    @classmethod
    def terminal_values(cls) -> frozenset[str]:
        return frozenset({cls.PASS.value, cls.HOLD.value})

    @classmethod
    def is_terminal(cls, value: str) -> bool:
        return value in cls.terminal_values()

    @classmethod
    def legal_transitions(cls, from_state: AssayState | str) -> list[AssayState]:
        """Return the list of legally reachable states from *from_state*."""
        if isinstance(from_state, str):
            from_state = cls(from_state)
        transitions: dict[AssayState, list[AssayState]] = {
            cls.PIN: [cls.HYPOTHESIS],
            cls.HYPOTHESIS: [cls.PROBE],
            cls.PROBE: [cls.MEASURE],
            cls.MEASURE: [cls.CLASSIFY],
            cls.CLASSIFY: [
                cls.REMEDIATE, cls.ESCALATE_OR_SPLIT, cls.PASS, cls.HOLD,
            ],
            cls.REMEDIATE: [cls.PROBE],
            cls.ESCALATE_OR_SPLIT: [cls.HYPOTHESIS, cls.HOLD],
            cls.PASS: [],
            cls.HOLD: [],
        }
        return list(transitions.get(from_state, []))


# ── Action types for policy gating ──────────────────────────────────────────

class ActionType(str, Enum):
    """Actions that policies may gate on."""

    READ_ONLY_PROBE = "read_only_probe"
    MUTATING_BATCH = "mutating_batch"
    EXTERNAL_ACTION = "external_action"
    IRREVERSIBLE_ACTION = "irreversible_action"
    SCOPE_EXPANSION = "scope_expansion"
    AUTHORITY_EXPANSION = "authority_expansion"
    STATE_TRANSITION = "state_transition"
    SPLIT = "split"
    REMEDIATE = "remediate"
    RESCHEDULE = "reschedule"


@dataclass
class ActionRequest:
    """A request to perform an action that may require policy approval."""

    action_type: ActionType
    description: str
    state: AssayState
    target_state: Optional[AssayState] = None
    scope: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyDecision:
    """Result of a policy check."""

    approved: bool
    reason: str = ""
    requires_human: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ── AssayPolicy (abstract base) ─────────────────────────────────────────────

class AssayPolicy:
    """Base class for Assay autonomy policies.

    Subclasses define approval rules for Conservative, Moderate, and Unicorn.
    """

    name: str = "abstract"

    def check(self, action: ActionRequest) -> PolicyDecision:
        """Check whether *action* is approved under this policy."""
        raise NotImplementedError

    def check_transition(
        self,
        from_state: AssayState,
        to_state: AssayState,
    ) -> PolicyDecision:
        """Check whether a state transition is approved."""
        action = ActionRequest(
            action_type=ActionType.STATE_TRANSITION,
            description=(
                f"Transition from {from_state.value} to {to_state.value}"
            ),
            state=from_state,
            target_state=to_state,
        )
        return self.check(action)


# ── Conservative policy ─────────────────────────────────────────────────────

class ConservativePolicy(AssayPolicy):
    """Approves every mutating batch and external/irreversible action.

    Read-only probes run automatically.  Every other action is blocked
    until approval is granted by a higher authority.
    """

    name = "conservative"

    def check(self, action: ActionRequest) -> PolicyDecision:
        if action.action_type == ActionType.READ_ONLY_PROBE:
            return PolicyDecision(
                approved=True,
                reason="Conservative: read-only probes run automatically",
            )

        if action.action_type in (
            ActionType.IRREVERSIBLE_ACTION,
            ActionType.EXTERNAL_ACTION,
        ):
            return PolicyDecision(
                approved=False,
                reason=(
                    f"Conservative policy requires human approval for "
                    f"{action.action_type.value}"
                ),
                requires_human=True,
            )

        return PolicyDecision(
            approved=False,
            reason=(
                f"Conservative policy requires approval for "
                f"{action.action_type.value}"
            ),
        )


# ── Moderate policy ─────────────────────────────────────────────────────────

class ModeratePolicy(AssayPolicy):
    """Approves plan/new waves, then self-heals within an approved reversible wave.

    ``_wave_approved`` tracks whether the current wave has been authorised.
    A new wave (HYPOTHESIS → PROBE) flips it to ``True``.  Within the wave
    all reversible transitions run automatically.  An ESCALATE_OR_SPLIT or
    PASS/HOLD resets it so the next wave must be re-approved.
    """

    name = "moderate"

    def __init__(self) -> None:
        self._wave_approved = False

    def check(self, action: ActionRequest) -> PolicyDecision:
        # ── Plan approval (PIN → HYPOTHESIS) ────────────────────────
        if (
            action.action_type == ActionType.STATE_TRANSITION
            and action.state == AssayState.PIN
            and action.target_state == AssayState.HYPOTHESIS
        ):
            return PolicyDecision(
                approved=True,
                reason="Moderate: initial plan approved",
            )

        # ── Wave approval (HYPOTHESIS → PROBE) ──────────────────────
        if (
            action.action_type == ActionType.STATE_TRANSITION
            and action.state == AssayState.HYPOTHESIS
            and action.target_state == AssayState.PROBE
        ):
            self._wave_approved = True
            return PolicyDecision(
                approved=True,
                reason="Moderate: new wave approved",
            )

        # ── Self-heal within an approved wave ───────────────────────
        if self._wave_approved:
            if action.action_type in (
                ActionType.REMEDIATE,
                ActionType.RESCHEDULE,
            ):
                return PolicyDecision(
                    approved=True,
                    reason="Moderate: self-heals within approved wave",
                )

            if action.action_type == ActionType.READ_ONLY_PROBE:
                return PolicyDecision(
                    approved=True,
                    reason="Moderate: read-only probes run automatically",
                )

            if action.action_type in (
                ActionType.MUTATING_BATCH,
                ActionType.STATE_TRANSITION,
            ):
                # Reversible transitions within the wave are auto-approved.
                if action.target_state in (
                    AssayState.PROBE,
                    AssayState.MEASURE,
                    AssayState.CLASSIFY,
                    AssayState.REMEDIATE,
                ):
                    return PolicyDecision(
                        approved=True,
                        reason=(
                            "Moderate: transitions within approved wave "
                            "are auto-approved"
                        ),
                    )

        # ── Irreversible / external always hold ─────────────────────
        if action.action_type in (
            ActionType.IRREVERSIBLE_ACTION,
            ActionType.EXTERNAL_ACTION,
        ):
            return PolicyDecision(
                approved=False,
                reason=(
                    f"Moderate policy requires human approval for "
                    f"{action.action_type.value}"
                ),
                requires_human=True,
            )

        return PolicyDecision(
            approved=False,
            reason=(
                f"Moderate policy requires approval for "
                f"{action.action_type.value}"
            ),
        )


# ── Unicorn policy ──────────────────────────────────────────────────────────

class UnicornPolicy(AssayPolicy):
    """Retries, remediates, reschedules, and resolves capabilities inside
    declared reversible scopes.

    Still holds for:
    * Irreversible action
    * Scope / authority expansion
    * Missing authority
    * Target drift
    * Budget exhaustion
    """

    name = "unicorn"

    def check(self, action: ActionRequest) -> PolicyDecision:
        # Full autonomy for reversible actions within declared scope.
        if action.action_type in (
            ActionType.READ_ONLY_PROBE,
            ActionType.REMEDIATE,
            ActionType.RESCHEDULE,
            ActionType.MUTATING_BATCH,
        ):
            return PolicyDecision(
                approved=True,
                reason="Unicorn: auto-approved within reversible scope",
            )

        # State transitions within the assay are auto-approved.
        if action.action_type == ActionType.STATE_TRANSITION:
            return PolicyDecision(
                approved=True,
                reason="Unicorn: auto-approves assay state transitions",
            )

        # Splits are auto-approved.
        if action.action_type == ActionType.SPLIT:
            return PolicyDecision(
                approved=True,
                reason="Unicorn: auto-approves splits",
            )

        # ── Explicit hold conditions ────────────────────────────────
        if action.action_type in (
            ActionType.IRREVERSIBLE_ACTION,
            ActionType.EXTERNAL_ACTION,
        ):
            return PolicyDecision(
                approved=False,
                reason=(
                    "Unicorn: holds for irreversible or external action"
                ),
                requires_human=True,
            )

        if action.action_type == ActionType.SCOPE_EXPANSION:
            return PolicyDecision(
                approved=False,
                reason="Unicorn: holds for scope expansion",
                requires_human=True,
            )

        if action.action_type == ActionType.AUTHORITY_EXPANSION:
            return PolicyDecision(
                approved=False,
                reason="Unicorn: holds for authority expansion",
                requires_human=True,
            )

        return PolicyDecision(
            approved=False,
            reason=(
                f"Unicorn: requires evaluation for "
                f"{action.action_type.value}"
            ),
        )


# ── Policy factory ──────────────────────────────────────────────────────────

_POLICY_CLASSES: dict[str, type[AssayPolicy]] = {
    "conservative": ConservativePolicy,
    "moderate": ModeratePolicy,
    "unicorn": UnicornPolicy,
}


def create_policy(name: str) -> AssayPolicy:
    """Create a policy instance by *name*.

    Raises ``ValueError`` for an unknown policy name.
    """
    cls = _POLICY_CLASSES.get(name)
    if cls is None:
        raise ValueError(
            f"unknown assay policy '{name}' "
            f"(expected one of {sorted(_POLICY_CLASSES)})"
        )
    return cls()


def list_policy_names() -> list[str]:
    """Return all registered policy names."""
    return sorted(_POLICY_CLASSES)


# ── Data records ────────────────────────────────────────────────────────────

@dataclass
class AssayHeartbeat:
    """A heartbeat recorded during an assay run."""

    run_id: str
    state: str
    timestamp: str
    turn: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "timestamp": self.timestamp,
            "turn": self.turn,
            "metadata": dict(self.metadata),
        }


@dataclass
class AssayTurnLimit:
    """A recorded turn-limit change.

    ``actor`` is the authenticated actor that authorized the change (empty only
    for a legacy recording on an engine with no admission requirement). An
    owner-authorized no-cumulative-limit envelope records ``max_turns=None``,
    meaning "no fixed cumulative ceiling" — distinct from a number.
    """

    run_id: str
    max_turns: Optional[int]
    current_turn: int
    updated_at: str
    actor: str = ""
    kind: str = "extension"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "max_turns": self.max_turns,
            "current_turn": self.current_turn,
            "updated_at": self.updated_at,
            "actor": self.actor,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class AssayEnvelope:
    """The owner-authorized execution envelope for one assay run.

    ``deadline`` is an ISO instant; when supplied, every admitted mutation and
    turn-limit change is refused at or after it. ``remaining_budget`` is a
    finite reservation; when supplied, advancing turns beyond it is refused.
    Both default to ``None`` = *no cumulative limit*, which is the explicit
    owner-authorized envelope that must not be replaced by an artificial
    default. ``require_actor`` makes an authenticated actor mandatory for every
    mutation and turn-limit change. A negative ``remaining_budget`` is refused.
    """

    deadline: Optional[str] = None
    remaining_budget: Optional[int] = None
    require_actor: bool = False

    def __post_init__(self) -> None:
        if self.remaining_budget is not None:
            if isinstance(self.remaining_budget, bool) or not isinstance(
                self.remaining_budget, int
            ):
                raise AdmissionRefused(
                    "envelope", "remaining_budget must be a non-negative integer"
                )
            if self.remaining_budget < 0:
                raise AdmissionRefused(
                    "envelope",
                    f"remaining_budget must not be negative, got {self.remaining_budget}",
                )
        if self.deadline is not None:
            try:
                datetime.fromisoformat(self.deadline)
            except (TypeError, ValueError):
                raise AdmissionRefused(
                    "envelope",
                    f"deadline must be an ISO timestamp, got {self.deadline!r}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadline": self.deadline,
            "remaining_budget": self.remaining_budget,
            "require_actor": self.require_actor,
        }


@dataclass
class AssaySplit:
    """A recorded split during an assay run."""

    run_id: str
    split_id: str
    from_state: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "split_id": self.split_id,
            "from_state": self.from_state,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


# ── Admission (actor / subject / brief / envelope) ──────────────────────────

class ActorResolver:
    """Resolves a presented token to an authenticated actor, or refuses.

    The engine never trusts a caller-supplied actor *string*; it trusts only a
    credential a resolver independently verifies. A live
    ``HostAuthorityService``-backed resolver is the production implementation;
    ``StaticActorResolver`` is the explicit offline/test seam. A resolver
    returns a non-empty actor string for a live token and raises ``ValueError``
    otherwise.
    """

    def resolve(self, token: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class StaticActorResolver(ActorResolver):
    """An offline resolver over a fixed token -> actor map.

    Deliberately explicit: it binds no identity by naming convention (an
    ``auth_real`` style string is never proof), only a token that appears in
    the map it was constructed with.
    """

    def __init__(self, tokens: dict[str, str]):
        self._tokens = dict(tokens)

    def resolve(self, token: str) -> str:
        actor = self._tokens.get(token)
        if not actor:
            raise ValueError("unrecognized actor token")
        return actor


@dataclass(frozen=True)
class AssayAdmission:
    """The authenticated, pinned facts a mutating assay action is admitted under.

    ``actor_token`` is verified by the engine's resolver — the ``actor`` string
    here is carried only for the record and is never the proof. ``subject`` and
    ``brief`` are pinned content addresses: the action is admitted only while
    the subject and brief still digest to these exact values, so a mid-run swap
    is refused. ``envelope`` carries the owner-authorized deadline and
    reservation.
    """

    actor_token: str
    subject: str
    brief: str
    envelope: AssayEnvelope = field(default_factory=AssayEnvelope)
    actor: str = ""


# ── Assay engine ────────────────────────────────────────────────────────────

class AssayEngine:
    """State machine plus policy enforcement for the Assay methodology.

    No model or router identities appear in this code — only Assay concepts:
    states, policies, turns, heartbeats, splits, and evidence.

    When an ``actor_resolver`` is supplied the engine enforces the admission
    contract on every mutation and every turn-limit change: the actor must
    authenticate, the pinned subject/brief must be unchanged, the envelope
    deadline must not have passed, and the remaining reservation must not be
    exhausted. ``max_turns=None`` is the explicit owner-authorized
    no-cumulative-limit envelope and is *not* replaced by an artificial default.
    A negative turn limit is always refused.
    """

    def __init__(
        self,
        policy: Optional[AssayPolicy] = None,
        policy_name: str = "conservative",
        max_turns: Optional[int] = 10,
        *,
        run_id: str = "",
        actor_resolver: Optional[ActorResolver] = None,
        envelope: Optional[AssayEnvelope] = None,
    ) -> None:
        if policy is None:
            policy = create_policy(policy_name)
        if max_turns is not None and (
            isinstance(max_turns, bool) or not isinstance(max_turns, int)
        ):
            raise AdmissionRefused("turn_limit", "max_turns must be an integer or None")
        if max_turns is not None and max_turns < 0:
            raise AdmissionRefused(
                "turn_limit", f"max_turns must not be negative, got {max_turns}"
            )
        self.policy = policy
        self.max_turns = max_turns
        self.current_turn = 0
        self.run_id = run_id
        self.actor_resolver = actor_resolver
        self.envelope = envelope or AssayEnvelope(require_actor=actor_resolver is not None)
        self.subject_digest: Optional[str] = None
        self.brief_digest: Optional[str] = None
        self._state: AssayState = AssayState.PIN
        self._heartbeats: list[AssayHeartbeat] = []
        self._splits: list[AssaySplit] = []
        self._turn_limit_changes: list[AssayTurnLimit] = []
        self._terminal_evidence: list[dict[str, Any]] = []
        # Record initial heartbeat so there is always a starting record.
        self.heartbeat()

    # ── Admission ───────────────────────────────────────────────────

    def pin(self, *, subject: str, brief: str) -> None:
        """Pin the subject and brief for the run.

        A second pin that differs is refused: the subject/brief are pinned for
        the whole run and are never silently re-bound mid-flight.
        """
        if not subject or not brief:
            raise AdmissionRefused("pin", "subject and brief must be non-empty")
        if self.subject_digest is not None and (
            self.subject_digest != subject or self.brief_digest != brief
        ):
            raise AdmissionRefused(
                "pin", "subject/brief are already pinned for this run"
            )
        self.subject_digest = subject
        self.brief_digest = brief

    def _resolve_actor(self, admission: AssayAdmission) -> str:
        if not admission.actor_token:
            raise AdmissionRefused("actor", "no actor credential presented")
        if self.actor_resolver is None:
            raise AdmissionRefused(
                "actor", "engine has no actor resolver; authenticated mutation is unavailable"
            )
        try:
            return self.actor_resolver.resolve(admission.actor_token)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed refusal
            raise AdmissionRefused("actor", f"actor credential rejected: {exc}") from exc

    def _check_admission(
        self,
        admission: Optional[AssayAdmission],
        *,
        check_budget: bool = True,
    ) -> str:
        """Verify actor, pinned subject/brief, deadline and remaining budget.

        Returns the authenticated actor. Raises :class:`AdmissionRefused` before
        any mutation when any axis fails.
        """
        if admission is None:
            if self.envelope.require_actor or self.actor_resolver is not None:
                raise AdmissionRefused(
                    "admission", "an authenticated admission is required"
                )
            return ""
        actor = self._resolve_actor(admission)
        # Unchanged subject/brief: the pinned values must still hold.
        if self.subject_digest is not None and admission.subject != self.subject_digest:
            raise AdmissionRefused(
                "subject", "admitted subject differs from the pinned subject"
            )
        if self.brief_digest is not None and admission.brief != self.brief_digest:
            raise AdmissionRefused(
                "brief", "admitted brief differs from the pinned brief"
            )
        env = admission.envelope or self.envelope
        if env.deadline is not None:
            if datetime.now(timezone.utc).isoformat() >= env.deadline:
                raise AdmissionRefused(
                    "deadline", f"envelope deadline {env.deadline} has passed"
                )
        if env.remaining_budget is not None:
            if env.remaining_budget < 0:
                raise AdmissionRefused(
                    "budget",
                    f"remaining budget must not be negative, got {env.remaining_budget}",
                )
            if check_budget and self.current_turn >= env.remaining_budget:
                raise AdmissionRefused(
                    "budget",
                    f"remaining budget {env.remaining_budget} is exhausted at "
                    f"turn {self.current_turn}",
                )
        return actor

    # ── State ───────────────────────────────────────────────────────

    @property
    def state(self) -> AssayState:
        return self._state

    def transition(
        self,
        target: AssayState,
        *,
        admission: Optional[AssayAdmission] = None,
    ) -> AssayState:
        """Attempt a state transition, enforcing policy and admission.

        Raises ``ValueError`` for an illegal transition,
        ``PolicyBlockedError`` when the policy blocks it,
        ``PolicyHoldError`` when the policy requires human intervention, and
        ``AdmissionRefused`` when the acting mutation fails actor/subject/brief/
        deadline/budget checks.
        """
        legal = AssayState.legal_transitions(self._state)
        if target not in legal:
            raise InvalidTransitionError(self._state, target)

        self._check_admission(admission)

        decision = self.policy.check_transition(self._state, target)
        if not decision.approved:
            if decision.requires_human:
                raise PolicyHoldError(
                    state=self._state,
                    target=target,
                    reason=decision.reason,
                )
            raise PolicyBlockedError(
                state=self._state,
                target=target,
                reason=decision.reason,
            )

        self._state = target
        self.heartbeat()
        return self._state

    # ── Turns ───────────────────────────────────────────────────────

    def advance_turn(
        self,
        *,
        admission: Optional[AssayAdmission] = None,
    ) -> int:
        """Advance the turn counter, raising on budget exhaustion.

        A mutation of the run's progress: it checks actor, pinned subject/brief,
        deadline and remaining budget first. ``max_turns=None`` means the
        owner-authorized no-cumulative-limit envelope and never blocks here.
        """
        self._check_admission(admission)
        if self.max_turns is not None and self.current_turn >= self.max_turns:
            raise TurnBudgetExhaustedError(
                f"Turn budget exhausted: {self.current_turn}/{self.max_turns}"
            )
        self.current_turn += 1
        return self.current_turn

    def set_turn_limit(
        self,
        max_turns: Optional[int],
        *,
        admission: Optional[AssayAdmission] = None,
    ) -> AssayTurnLimit:
        """Change the maximum turn limit and record the change.

        A negative limit is refused. The change checks actor, pinned subject/
        brief, deadline and remaining budget before it takes effect: an
        unauthorized actor cannot extend (or shrink) the bound. ``max_turns=None``
        records the owner-authorized no-cumulative-limit envelope.
        """
        if max_turns is not None:
            if isinstance(max_turns, bool) or not isinstance(max_turns, int):
                raise AdmissionRefused(
                    "turn_limit", "max_turns must be an integer or None"
                )
            if max_turns < 0:
                raise AdmissionRefused(
                    "turn_limit", f"max_turns must not be negative, got {max_turns}"
                )
        actor = self._check_admission(admission)
        self.max_turns = max_turns
        kind = "no_cumulative_limit" if max_turns is None else "extension"
        limit = AssayTurnLimit(
            run_id=self.run_id,
            max_turns=max_turns,
            current_turn=self.current_turn,
            updated_at=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            kind=kind,
        )
        self._turn_limit_changes.append(limit)
        return limit

    # ── Heartbeats ──────────────────────────────────────────────────

    def heartbeat(self) -> AssayHeartbeat:
        """Record a heartbeat at the current state and turn."""
        hb = AssayHeartbeat(
            run_id="",
            state=self._state.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
            turn=self.current_turn,
        )
        self._heartbeats.append(hb)
        return hb

    # ── Splits ──────────────────────────────────────────────────────

    def record_split(
        self,
        split_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> AssaySplit:
        """Record a split from the current state."""
        split = AssaySplit(
            run_id="",
            split_id=split_id,
            from_state=self._state.value,
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )
        self._splits.append(split)
        return split

    # ── Terminal evidence ───────────────────────────────────────────

    def record_terminal_evidence(self, evidence: dict[str, Any]) -> None:
        """Record terminal evidence.

        Raises ``EvidenceRejectedError`` when the evidence is narrative
        or compaction — these are never valid assay evidence.
        """
        _validate_evidence_not_narrative(evidence)
        self._terminal_evidence.append(evidence)

    # ── Accessors ───────────────────────────────────────────────────

    def get_heartbeats(self) -> list[AssayHeartbeat]:
        return list(self._heartbeats)

    def get_splits(self) -> list[AssaySplit]:
        return list(self._splits)

    def get_turn_limits(self) -> list[AssayTurnLimit]:
        return list(self._turn_limit_changes)

    def get_terminal_evidence(self) -> list[dict[str, Any]]:
        return list(self._terminal_evidence)

    def check_action(self, action: ActionRequest) -> PolicyDecision:
        """Explicit policy check for an arbitrary action."""
        return self.policy.check(action)

    def reset_wave(self) -> None:
        """Reset wave-approval state (used by Moderate after ESCALATE/PASS/HOLD)."""
        if isinstance(self.policy, ModeratePolicy):
            self.policy._wave_approved = False


# ── Evidence validation ─────────────────────────────────────────────────────

# Valid evidence types for Assay (narrative and compaction are excluded).
VALID_ASSAY_EVIDENCE_TYPES: frozenset[str] = frozenset({
    "record", "artifact", "observation", "test", "metric",
    "decision", "runtime_trace",
})

_NARRATIVE_KEYWORDS: frozenset[str] = frozenset({
    "narrative", "compaction", "summary", "recap", "overview",
})


def _validate_evidence_not_narrative(evidence: dict[str, Any]) -> None:
    """Raise ``EvidenceRejectedError`` if *evidence* is narrative/compaction.

    Checks both the ``evidence_type`` field and the ``purpose``/``method``
    text fields for narrative or compaction keywords.
    """
    evidence_type = evidence.get("evidence_type", "")
    purpose = evidence.get("purpose", "")
    method = evidence.get("method", "")

    # Reject by evidence_type.
    if evidence_type and evidence_type not in VALID_ASSAY_EVIDENCE_TYPES:
        raise EvidenceRejectedError(
            f"Evidence type '{evidence_type}' is not valid for Assay; "
            f"expected one of {sorted(VALID_ASSAY_EVIDENCE_TYPES)}",
            evidence=evidence,
        )

    # Reject by keyword in purpose or method.
    for field_name, field_value in [("purpose", purpose), ("method", method)]:
        lowered = field_value.lower()
        for keyword in _NARRATIVE_KEYWORDS:
            if keyword in lowered:
                raise EvidenceRejectedError(
                    f"Narrative/compaction detected in evidence.{field_name}: "
                    f"'{field_value}' contains '{keyword}'",
                    evidence=evidence,
                )


def validate_assay_evidence(evidence: dict[str, Any]) -> None:
    """Public entry point for evidence validation.

    Rejects narrative/compaction.  Raises ``EvidenceRejectedError``.
    """
    _validate_evidence_not_narrative(evidence)


# ── Errors ──────────────────────────────────────────────────────────────────

class AssayError(Exception):
    """Base error for the Assay methodology."""


class InvalidTransitionError(AssayError):
    """Raised for an illegal state transition."""

    def __init__(
        self,
        from_state: AssayState,
        to_state: AssayState,
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition: {from_state.value} → {to_state.value}"
        )


class PolicyBlockedError(AssayError):
    """Raised when a policy blocks a transition or action."""

    def __init__(
        self,
        state: AssayState,
        target: AssayState,
        reason: str,
    ) -> None:
        self.state = state
        self.target = target
        self.reason = reason
        super().__init__(
            f"Policy blocked: {state.value} → {target.value}: {reason}"
        )


class PolicyHoldError(AssayError):
    """Raised when a policy requires human intervention."""

    def __init__(
        self,
        state: AssayState,
        target: AssayState,
        reason: str,
    ) -> None:
        self.state = state
        self.target = target
        self.reason = reason
        super().__init__(
            f"Policy hold: {reason} ({state.value} → {target.value})"
        )


class TurnBudgetExhaustedError(AssayError):
    """Raised when the turn budget is exhausted."""


class AdmissionRefused(AssayError):
    """A mutation or turn-limit change failed actor/subject/brief/envelope checks.

    Raised *before* the mutation. ``axis`` names which check refused (actor,
    subject, brief, deadline, budget, turn_limit, admission, envelope, pin), so
    the refusal is attributable.
    """

    def __init__(self, axis: str, reason: str) -> None:
        self.axis = axis
        self.reason = reason
        super().__init__(f"Assay admission refused ({axis}): {reason}")


class EvidenceRejectedError(AssayError):
    """Raised when evidence is rejected (e.g. narrative/compaction)."""

    def __init__(
        self,
        message: str,
        evidence: dict[str, Any],
    ) -> None:
        self.evidence = evidence
        super().__init__(message)


# ── __all__ ─────────────────────────────────────────────────────────────────

__all__ = [
    "AssayState",
    "ActionType",
    "ActionRequest",
    "PolicyDecision",
    "AssayPolicy",
    "ConservativePolicy",
    "ModeratePolicy",
    "UnicornPolicy",
    "create_policy",
    "list_policy_names",
    "AssayHeartbeat",
    "AssayTurnLimit",
    "AssayEnvelope",
    "AssayAdmission",
    "ActorResolver",
    "StaticActorResolver",
    "AssaySplit",
    "AssayEngine",
    "VALID_ASSAY_EVIDENCE_TYPES",
    "validate_assay_evidence",
    "AssayError",
    "InvalidTransitionError",
    "PolicyBlockedError",
    "PolicyHoldError",
    "TurnBudgetExhaustedError",
    "AdmissionRefused",
    "EvidenceRejectedError",
]
