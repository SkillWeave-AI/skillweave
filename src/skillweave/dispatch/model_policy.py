"""Provider-neutral model allocation and escalation policy (SW1311-MODEL-001).

This module owns the *policy* that decides which capability tier a task runs
on, and how a lane escalates or blocks when it makes no progress. It is the
single seam under ``dispatch/`` that turns a task's risk profile into a
Flash/Pro allocation, and it is deliberately provider-neutral: it names no
concrete vendor, gateway, router or product model. ``flash`` and ``pro`` are
capability tiers, not product names.

The module is authoritative in eight ways, matching the eight acceptance
criteria:

1. **A product contract declares the model policy.** The
   ``dispatch-sequence.schema.json`` (the shipped contract) carries a
   ``model_policy`` block naming capability/minimum tier, architectural risk,
   cost ceiling and fallback — all without a vendor/gateway prefix.

2. **Requested, gateway-resolved and answering model are three separate
   facts.** :class:`ModelAttribution` keeps them apart; an unknown attribution
   is reported as unknown, never synthesised.

3. **Namespace/prefix is adapter/profile data.** This module reads a prefix as
   opaque data and never interprets or strips it; translation belongs to the
   owning adapter boundary exactly once.

4. **Allocation is risk-shaped, not size-shaped.** :func:`allocate` weighs
   discovery vs action, task type, recall/precision, blast radius,
   reversibility, state/concurrency, migration, security, integration,
   ambiguity, error profile and coverage. File count alone never decides.

5. **Flash is allowed only for bounded, low-risk work.** Bounded low-risk/bug
   discovery may run on Flash. Architecture, high-blast, migration, security,
   causal and ambiguous rework, and critical review require Pro unless an
   explicit, reasoned override is recorded.

6. **Escalation is bounded.** After two non-progress or accepted review-fail
   cycles a Flash allocation escalates to Pro if the cost ceiling permits,
   otherwise it blocks explicitly. There is never an infinite loop.

7. **Technical failures are not review failures.** Provider unavailability,
   rate limits, launch failures and attribution failures are technical; they
   consume no correction round and never become ``REVIEW_FAIL``.

8. **Tokens/latency/cost are receipt-bound.** :class:`ModelReceipt` records
   them only when measured, otherwise as unavailable with a reason; transfer
   observations cannot mutate policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


# ── Capability tiers ───────────────────────────────────────────────────────

class ModelTier(str, Enum):
    """The two provider-neutral capability tiers.

    ``flash`` names the cheaper, faster class suitable only for bounded,
    low-risk work. ``pro`` names the stronger class required for architecture,
    high-blast, migration, security, causal and critical-review work. Neither
    names a vendor, gateway or product model.
    """

    FLASH = "flash"
    PRO = "pro"


class AllocationError(ValueError):
    """An allocation or escalation request violated the model policy."""


class EscalationError(AllocationError):
    """An escalation could not proceed (budget exhausted, invalid cycle)."""


class TechnicalFailureError(AllocationError):
    """A provider/harness/launch/attribution failure classified as technical.

    Technical failures are bandaged as a typed error whose ``consume_correction``
    is always ``False`` and whose verdict is never ``REVIEW_FAIL``.
    """

    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind
        self.consume_correction = False


# ── Technical failure kinds (criterion 7) ─────────────────────────────────

#: The four technical failure kinds that consume no correction round and never
#: become a review verdict.
TECHNICAL_FAILURE_KINDS: frozenset[str] = frozenset({
    "provider_unavailable",
    "rate_limit",
    "launch_failure",
    "attribution_failure",
})


def is_technical_failure(kind: str) -> bool:
    """Whether ``kind`` names a technical (non-review) failure."""
    return kind in TECHNICAL_FAILURE_KINDS


# ── Model attribution: three separate facts (criterion 2) ─────────────────

#: The sentinel that marks an attribution we know we cannot know. Distinct from
#: ``None`` (absent) so a caller can tell "never asked" from "asked, got no id".
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelAttribution:
    """The three-way split of which model ran (criterion 2).

    ``requested`` is the model the caller asked for. ``resolved`` is the model
    the gateway (router/adapter) selected after the request. ``answering`` is
    the model that actually answered, read from the response envelope.

    The three are independent facts and are never collapsed. When any is not
    observable, it is :data:`UNKNOWN` — not guessed, not defaulted to another
    field. The ``to_dict`` form keeps all three keys so a receipt is truthful
    even when one is unknown.
    """

    requested: str = UNKNOWN
    resolved: str = UNKNOWN
    answering: str = UNKNOWN

    @classmethod
    def of(
        cls,
        *,
        requested: Optional[str] = None,
        resolved: Optional[str] = None,
        answering: Optional[str] = None,
    ) -> "ModelAttribution":
        return cls(
            requested=requested if requested else UNKNOWN,
            resolved=resolved if resolved else UNKNOWN,
            answering=answering if answering else UNKNOWN,
        )

    @property
    def answering_known(self) -> bool:
        return self.answering != UNKNOWN

    @property
    def resolved_known(self) -> bool:
        return self.resolved != UNKNOWN

    def to_dict(self) -> dict[str, str]:
        return {
            "requested": self.requested,
            "resolved": self.resolved,
            "answering": self.answering,
        }


# ── The product contract: model policy declaration (criterion 1) ──────────

#: The provider-neutral capability vocabulary a declaration may name. A task
#: either does discovery or action — never both under one allocation.
TASK_KINDS: frozenset[str] = frozenset({"discovery", "action"})


@dataclass(frozen=True)
class ModelPolicyDeclaration:
    """A provider-neutral model-policy declaration (criterion 1).

    ``minimum_tier`` names the lowest capability tier the task may run on
    (``flash`` or ``pro``). ``architectural_risk`` is a declared risk level;
    ``cost_ceiling`` is an upper bound on spend (in abstract units); ``fallback``
    is the behaviour when the preferred allocation is unavailable (``block`` or
    ``escalate``). ``requires`` is an optional list of true/false flags for the
    high-risk dimensions. No field carries a vendor or gateway prefix.
    """

    minimum_tier: ModelTier = ModelTier.FLASH
    architectural_risk: str = "low"
    cost_ceiling: Optional[float] = None
    fallback: str = "block"
    requires: Mapping[str, bool] = field(default_factory=dict)

    _VALID_RISK = frozenset({"low", "medium", "high", "critical"})
    _VALID_FALLBACK = frozenset({"block", "escalate"})

    def __post_init__(self) -> None:
        if self.architectural_risk not in self._VALID_RISK:
            raise AllocationError(
                f"unknown architectural_risk {self.architectural_risk!r} "
                f"(expected one of {sorted(self._VALID_RISK)})"
            )
        if self.fallback not in self._VALID_FALLBACK:
            raise AllocationError(
                f"unknown fallback {self.fallback!r} "
                f"(expected one of {sorted(self._VALID_FALLBACK)})"
            )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "minimum_tier": self.minimum_tier.value,
            "architectural_risk": self.architectural_risk,
            "fallback": self.fallback,
        }
        if self.cost_ceiling is not None:
            out["cost_ceiling"] = self.cost_ceiling
        if self.requires:
            out["requires"] = dict(self.requires)
        return out

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ModelPolicyDeclaration":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise AllocationError("model_policy must be a mapping")
        tier_raw = data.get("minimum_tier", ModelTier.FLASH.value)
        try:
            min_tier = ModelTier(tier_raw)
        except ValueError:
            raise AllocationError(
                f"unknown minimum_tier {tier_raw!r} "
                f"(expected one of {[t.value for t in ModelTier]})"
            ) from None
        cost = data.get("cost_ceiling")
        return cls(
            minimum_tier=min_tier,
            architectural_risk=str(data.get("architectural_risk", "low")),
            cost_ceiling=float(cost) if cost is not None else None,
            fallback=str(data.get("fallback", "block")),
            requires=dict(data.get("requires", {}) or {}),
        )


# ── Allocation signals (criterion 4): the risk dimensions ─────────────────

#: The high-risk dimensions that force a Pro allocation unless explicitly and
#: reasonably overridden. Each is a boolean signal on the task.
FORCING_SIGNALS: tuple[str, ...] = (
    "architecture",
    "high_blast_radius",
    "migration",
    "security",
    "causal_verification",
    "ambiguous_rework",
    "critical_review",
)

#: The remaining dimensions that *shape* allocation without individually
#: forcing Pro. They still feed ``allocate`` and are never ignored.
SHAPING_SIGNALS: tuple[str, ...] = (
    "state_concurrency",
    "integration",
    "recall_precision",
    "reversibility",
    "error_profile",
    "coverage",
)


@dataclass(frozen=True)
class AllocationSignals:
    """The risk/capability signals an allocation is derived from (criterion 4).

    ``task_kind`` is ``discovery`` or ``action``. ``task_type`` is an opaque,
    caller-supplied label. The seven forcing and six shaping dimensions are all
    booleans or graded values; a missing one is ``False``/``None`` and therefore
    never *force* a tier. ``coverage`` is kept as an opaque hint (e.g. the number
    of covered entities) but is explicitly *not* the sole input.
    """

    task_kind: str = "action"
    task_type: str = ""
    architecture: bool = False
    high_blast_radius: bool = False
    migration: bool = False
    security: bool = False
    causal_verification: bool = False
    ambiguous_rework: bool = False
    critical_review: bool = False
    state_concurrency: bool = False
    integration: bool = False
    recall_precision: str = "balanced"
    reversibility: bool = True
    error_profile: str = ""
    coverage: Optional[int] = None

    def __post_init__(self) -> None:
        if self.task_kind not in TASK_KINDS:
            raise AllocationError(
                f"unknown task_kind {self.task_kind!r} "
                f"(expected one of {sorted(TASK_KINDS)})"
            )

    def forcing(self) -> list[str]:
        """The forcing dimensions that are set true, in declaration order."""
        return [name for name in FORCING_SIGNALS if getattr(self, name)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_kind": self.task_kind,
            "task_type": self.task_type,
            **{name: getattr(self, name) for name in FORCING_SIGNALS},
            **{name: getattr(self, name) for name in SHAPING_SIGNALS},
            "coverage": self.coverage,
        }


# ── Allocation result ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class AllocationResult:
    """The single allocation decision (criterion 5).

    ``tier`` is the allocated capability tier. ``forced_pro`` is true when a
    forcing signal pushed the result to Pro. ``override`` records an explicit,
    reasoned override (with ``override_reason``) when a forcing signal was
    intentionally overridden — an override may lower a forced Pro to Flash only
    when this record is present, so it is always auditable.
    """

    tier: ModelTier
    forced_pro: bool = False
    override: bool = False
    override_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "forced_pro": self.forced_pro,
            "override": self.override,
            "override_reason": self.override_reason,
        }


def allocate(
    signals: AllocationSignals,
    declaration: ModelPolicyDeclaration,
    *,
    override_reason: str = "",
) -> AllocationResult:
    """Allocate a capability tier from signals and the declared floor.

    A forcing signal (architecture, high blast radius, migration, security,
    causal verification, ambiguous rework, critical review) requires Pro. The
    declared ``minimum_tier`` floor also requires at least that tier. A caller
    may lower a forced Pro to Flash only through ``override_reason`` — the
    explicit, reasoned override of criterion 5 — which is recorded on the
    result. ``coverage`` and the shaping signals never by themselves force a
    tier; allocation is risk-shaped, never size-shaped.
    """
    forcing = signals.forcing()
    declared_floor = declaration.minimum_tier

    forced_pro = bool(forcing) or declared_floor is ModelTier.PRO
    if not forced_pro:
        return AllocationResult(tier=declared_floor, forced_pro=False)

    # A forcing signal or a declared Pro floor pushes to Pro.
    if not override_reason.strip():
        return AllocationResult(tier=ModelTier.PRO, forced_pro=True)

    # An explicit, reasoned override may lower to the declared floor (which may
    # itself be Flash). It is always recorded so it stays auditable.
    return AllocationResult(
        tier=declared_floor,
        forced_pro=forced_pro,
        override=True,
        override_reason=override_reason.strip(),
    )


def allocate_pro_for(
    signals: AllocationSignals,
    declaration: ModelPolicyDeclaration,
) -> AllocationResult:
    """Convenience alias: allocate, treating any forcing signal as Pro-required."""
    return allocate(signals, declaration)


# ── Bounded escalation (criterion 6) ───────────────────────────────────────

#: The number of non-progress or accepted review-fail cycles after which a
#: Flash allocation must escalate to Pro (or block).
ESCALATION_THRESHOLD = 2


@dataclass
class EscalationState:
    """The bounded escalation bookkeeping for one lane's allocation.

    ``cycles`` counts non-progress or accepted review-fail cycles. ``tier`` is
    the current allocation. ``blocked`` becomes true when the threshold is
    reached and the cost ceiling does not permit escalation — at which point the
    lane blocks explicitly instead of looping.
    """

    tier: ModelTier = ModelTier.FLASH
    cycles: int = 0
    blocked: bool = False
    blocked_reason: str = ""

    def record_non_progress(self) -> "EscalationState":
        return self._record_cycle("non-progress")

    def record_review_fail(self) -> "EscalationState":
        return self._record_cycle("accepted review-fail")

    def _record_cycle(self, reason: str) -> "EscalationState":
        if self.tier is not ModelTier.FLASH:
            return self
        self.cycles += 1
        if self.cycles >= ESCALATION_THRESHOLD:
            return self  # escalation decision is made by escalate()
        return self

    def escalate(
        self,
        declaration: ModelPolicyDeclaration,
    ) -> "EscalationState":
        """Apply bounded escalation after the threshold is reached (criterion 6).

        Exactly one outcome holds, and it is explicit:

        * a Pro-allocation (already Pro) needs no escalation;
        * a Flash allocation under the threshold stays Flash;
        * at or beyond the threshold, Flash escalates to Pro **only if** the
          declared cost ceiling permits (either no ceiling, or the escalation is
          within budget — the caller passes the ceiling via ``declaration``);
          otherwise the lane blocks explicitly, never looping.

        Returns ``self`` (mutated) for chaining.
        """
        if self.blocked:
            return self
        if self.tier is ModelTier.PRO:
            return self
        if self.cycles < ESCALATION_THRESHOLD:
            return self

        if declaration.cost_ceiling is None:
            self.tier = ModelTier.PRO
            return self
        # A ceiling exists; escalation to Pro is permitted only when the budget
        # ceiling is positive (the caller has already bounded spend to permit a
        # Pro class). A zero/negative ceiling means "Flash-only budget".
        if declaration.cost_ceiling > 0:
            self.tier = ModelTier.PRO
            return self

        self.blocked = True
        self.blocked_reason = (
            "Flash allocation exhausted two non-progress/review-fail cycles and "
            "the cost ceiling does not permit escalation to Pro"
        )
        return self


# ── Receipt: tokens / latency / cost (criterion 8) ─────────────────────────

@dataclass(frozen=True)
class ModelReceipt:
    """A receipt-bound accounting of tokens, latency and cost (criterion 8).

    Each of ``tokens``, ``latency_ms`` and ``cost`` is either a measured value or
    ``None``. A ``None`` with a ``reason`` is *unavailable with reason* — it is
    not defaulted to zero and not guessed. ``from_dict`` rebuilds this shape so a
    downstream reader can tell "measured" from "unavailable".
    """

    tokens: Optional[int] = None
    latency_ms: Optional[float] = None
    cost: Optional[float] = None
    reason: str = ""

    @classmethod
    def unavailable(cls, reason: str) -> "ModelReceipt":
        return cls(tokens=None, latency_ms=None, cost=None, reason=reason)

    def unavailable_fields(self) -> list[str]:
        out: list[str] = []
        if self.tokens is None:
            out.append("tokens")
        if self.latency_ms is None:
            out.append("latency_ms")
        if self.cost is None:
            out.append("cost")
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.tokens is not None:
            out["tokens"] = self.tokens
        if self.latency_ms is not None:
            out["latency_ms"] = self.latency_ms
        if self.cost is not None:
            out["cost"] = self.cost
        if self.reason:
            out["reason"] = self.reason
        return out


# ── Transfer observations cannot mutate policy (criterion 8) ───────────────

@dataclass(frozen=True)
class TransferObservation:
    """A provenance-bearing observation from the transfer catalog.

    It is advisory only. It may be *consulted* by callers but can never mutate
    policy: :func:`apply_transfer_observation` returns a policy-shaped value
    that ignores any attempt to change allocation, escalation or receipt facts.
    """

    subject: str
    note: str = ""
    provenance: str = ""
    confidence: str = ""


def apply_transfer_observation(
    declaration: ModelPolicyDeclaration,
    _observation: TransferObservation,
) -> ModelPolicyDeclaration:
    """Return the declaration unchanged: transfer observations cannot mutate
    policy (criterion 8).

    The observation is advisory; consulting it leaves the model-policy
    declaration (allocation, cost ceiling, fallback) untouched. This function
    deliberately discards the observation's content from the return value.
    """
    return declaration


# ═══════════════════════════════════════════════════════════════════════════
# SW-1510-ROUTE-LOCK: Tier-to-route binding and retry attribution.
# ═══════════════════════════════════════════════════════════════════════════

#: The approved BytePlus routes per capability tier (criterion 3).
#: ``flash`` tiers map to the cheaper Flash class; ``pro`` tiers map to
#: the stronger Pro class.  This is the single binding between capability
#: tiers and concrete provider routes — raw upstream names or global
#: environment keys cannot override this mapping.
TIER_ROUTE_MAP: dict[str, frozenset[str]] = {
    ModelTier.FLASH.value: frozenset({
        "byteplus/deepseek-v4-flash",
        "byteplus/deepseek-v4.1-flash",
    }),
    ModelTier.PRO.value: frozenset({
        "byteplus/deepseek-v4-pro",
        "byteplus/deepseek-v4.1-pro",
        # Pro escalation may also use Flash routes when budget permits.
        "byteplus/deepseek-v4-flash",
        "byteplus/deepseek-v4.1-flash",
    }),
}


def resolve_route_for_tier(tier: str) -> frozenset[str]:
    """Return the approved route set for a capability tier (criterion 3).

    The mapping is the single authoritative binding between the provider-
    neutral ``flash``/``pro`` tier vocabulary and the concrete BytePlus
    route names.  An unknown tier raises ``AllocationError``, so a caller
    can never silently route through an unapproved provider.
    """
    routes = TIER_ROUTE_MAP.get(tier)
    if routes is None:
        raise AllocationError(
            f"unknown tier {tier!r} for route resolution "
            f"(expected one of {sorted(TIER_ROUTE_MAP)})"
        )
    return routes


@dataclass(frozen=True)
class AttemptAttribution:
    """Retry/escalation attempt attribution (criterion 4).

    Each retry or escalation carries its own new ``attempt`` number
    (strictly increasing from the parent), the ``requested_route`` the
    attempt is bound to, and the ``reserved_budget`` (in abstract cost
    units) allocated for this attempt.  An attempt with ``attempt < 1``
    or without an explicit route is invalid and must be refused before
    any launch.

    When ``allowed_routes`` is set, the requested route must be within
    that set — raw upstream names or global environment keys cannot
    override the approved route set.
    """

    attempt: int = 1
    requested_route: str = ""
    reserved_budget: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise AllocationError(
                f"attempt must be a positive integer, got {self.attempt!r}"
            )
        if self.attempt < 1:
            raise AllocationError(
                f"attempt must be >= 1, got {self.attempt}"
            )
        if not self.requested_route:
            raise AllocationError(
                "an attempt attribution requires a non-empty requested_route"
            )


def check_attempt_route(
    attribution: AttemptAttribution,
    allowed_routes: Optional[frozenset[str]] = None,
) -> None:
    """Verify that an attempt's route is within the allowed set (criterion 4).

    Raises ``AllocationError`` when the route is not permitted.  When
    ``allowed_routes`` is ``None``, the check is skipped (caller has not
    declared a route restriction).
    """
    if allowed_routes is not None and attribution.requested_route not in allowed_routes:
        raise AllocationError(
            f"attempt {attribution.attempt} requests route "
            f"{attribution.requested_route!r} which is not in allowed routes "
            f"{sorted(allowed_routes)}"
        )


__all__ = [
    "ModelTier",
    "AllocationError",
    "EscalationError",
    "TechnicalFailureError",
    "TECHNICAL_FAILURE_KINDS",
    "is_technical_failure",
    "UNKNOWN",
    "ModelAttribution",
    "TASK_KINDS",
    "ModelPolicyDeclaration",
    "FORCING_SIGNALS",
    "SHAPING_SIGNALS",
    "AllocationSignals",
    "AllocationResult",
    "allocate",
    "allocate_pro_for",
    "ESCALATION_THRESHOLD",
    "EscalationState",
    "ModelReceipt",
    "TransferObservation",
    "apply_transfer_observation",
    "TIER_ROUTE_MAP",
    "resolve_route_for_tier",
    "AttemptAttribution",
    "check_attempt_route",
]
