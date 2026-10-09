"""Composable execution strategies for SkillWeave (SW-160-STRAT-001).

Execution strategies are composable capabilities rather than a single exclusive
enum. Capabilities can be combined freely, and named recipes are projections
over capabilities with no private runtime path.

Both verticals (research-synthesis and software-delivery) execute through the
same strategy contract.
"""

from __future__ import annotations

from enum import Flag, auto
from typing import Any, Optional


# ── Capabilities as bit flags ───────────────────────────────────────────────

class ExecutionCapability(Flag):
    """Composable execution capabilities.

    Capabilities are bit flags so they compose freely::

        FRESH_CONTEXT | INDEPENDENT_REVIEWER  # enables both

    This is the opposite of an exclusive enum: any combination is valid.
    """
    NONE = 0
    FRESH_CONTEXT = auto()          # Start fresh (no warm state carried over)
    INDEPENDENT_REVIEWER = auto()   # Separate reviewer role gates the result
    NAMED_REFERENCE = auto()        # Strategy is addressable by a recipe name
    WARM_START = auto()             # Reuse previously warmed state
    RESUME = auto()                 # Resume from a prior checkpoint
    PERSISTENT_STORE = auto()       # Persist intermediate results to store
    OBSERVER = auto()               # Attach a runtime observer
    RE_REVIEW = auto()              # Brand-new, independent *re*-review
    #: ``RE_REVIEW`` is deliberately distinct from ``FRESH_CONTEXT``: a cold
    #: first-run is fresh (no warm state) but is not a *re*-review.  Marking a
    #: re-review explicitly keeps "fresh" and "brand-new independent re-review"
    #: separable, so the Pro bound (SW-1510-ADAPTIVE criterion 2) can admit a
    #: re-review without also admitting every fresh run.


# ── Strategy contract ──────────────────────────────────────────────────────

class ExecutionStrategy:
    """An execution strategy defined by its composable capabilities.

    This is the contract that both verticals (research-synthesis and
    software-delivery) execute through.  Named recipes are projections over
    capabilities and contain no private runtime path — only capability flags
    and an optional human label.

    Parameters
    ----------
    capabilities : ExecutionCapability
        The bitwise combination of active capabilities.
    recipe_name : str or None
        An optional human-readable label (e.g. ``"cold"``, ``"warm"``).
        When set, the strategy is *addressable by name* through the recipe
        registry; when ``None`` the strategy is an ad-hoc composition.
    """

    def __init__(
        self,
        capabilities: ExecutionCapability = ExecutionCapability.NONE,
        recipe_name: Optional[str] = None,
    ) -> None:
        self._capabilities = capabilities
        self._recipe_name = recipe_name

    # ── Read-only properties ───────────────────────────────────────────

    @property
    def capabilities(self) -> ExecutionCapability:
        """The raw bitmask of active capabilities."""
        return self._capabilities

    @property
    def recipe_name(self) -> Optional[str]:
        """The optional named-recipe label, or ``None`` for ad-hoc strategies."""
        return self._recipe_name

    # ── Convenience checks for individual capabilities ──────────────────

    @property
    def has_fresh_context(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.FRESH_CONTEXT)

    @property
    def has_independent_reviewer(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.INDEPENDENT_REVIEWER)

    @property
    def has_named_reference(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.NAMED_REFERENCE)

    @property
    def has_warm_start(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.WARM_START)

    @property
    def has_resume(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.RESUME)

    @property
    def has_persistent_store(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.PERSISTENT_STORE)

    @property
    def has_observer(self) -> bool:
        return bool(self._capabilities & ExecutionCapability.OBSERVER)

    @property
    def has_re_review(self) -> bool:
        """True when the strategy is marked as a brand-new independent
        *re*-review (the ``RE_REVIEW`` capability)."""
        return bool(self._capabilities & ExecutionCapability.RE_REVIEW)

    @property
    def is_fresh_independent_re_review(self) -> bool:
        """True when the strategy is a *fresh independent re-review*.

        SW-1510-ADAPTIVE (criterion 2): a Pro allocation is permitted for a
        brand-new, independent re-review — and only that.  A re-review is
        marked ``RE_REVIEW`` (a brand-new re-review, not a first cold run),
        carries no warm state over (FRESH_CONTEXT) and is gated by a separate
        reviewer role (INDEPENDENT_REVIEWER).  All three must be present.

        The ``RE_REVIEW`` marking is what separates a re-review from an
        ordinary cold first-run: both are fresh and independently reviewed, so
        freshness and independence alone cannot tell them apart.
        """
        return (
            self.has_re_review
            and self.has_fresh_context
            and self.has_independent_reviewer
        )

    # ── Composition helpers ─────────────────────────────────────────────

    def with_capability(self, capability: ExecutionCapability) -> ExecutionStrategy:
        """Return a new strategy with *capability* added."""
        return ExecutionStrategy(
            capabilities=self._capabilities | capability,
            recipe_name=self._recipe_name,
        )

    def without_capability(self, capability: ExecutionCapability) -> ExecutionStrategy:
        """Return a new strategy with *capability* removed."""
        return ExecutionStrategy(
            capabilities=self._capabilities & ~capability,
            recipe_name=self._recipe_name,
        )

    def has_all(self, *capabilities: ExecutionCapability) -> bool:
        """Return ``True`` when *every* listed capability is present."""
        mask = ExecutionCapability.NONE
        for cap in capabilities:
            mask |= cap
        return (self._capabilities & mask) == mask

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (capability names + optional recipe name)."""
        return {
            "capabilities": [
                c.name for c in ExecutionCapability
                if c and (self._capabilities & c)
            ],
            "recipe_name": self._recipe_name,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ExecutionStrategy):
            return NotImplemented
        return (
            self._capabilities == other._capabilities
            and self._recipe_name == other._recipe_name
        )

    def __hash__(self) -> int:
        return hash((self._capabilities, self._recipe_name))

    def __repr__(self) -> str:
        names = [
            c.name for c in ExecutionCapability
            if c and (self._capabilities & c)
        ]
        label = f" recipe={self._recipe_name!r}" if self._recipe_name else ""
        return f"ExecutionStrategy({names}{label})"


# ── Factory helpers ─────────────────────────────────────────────────────────

def from_capabilities(
    *capabilities: ExecutionCapability,
    recipe_name: Optional[str] = None,
) -> ExecutionStrategy:
    """Build a strategy from one or more capabilities."""
    combined = ExecutionCapability.NONE
    for cap in capabilities:
        combined |= cap
    return ExecutionStrategy(capabilities=combined, recipe_name=recipe_name)


# ── Named recipes (projections over capabilities) ───────────────────────────
#
# Each recipe is a projection over capabilities and contains **no private
# runtime path** — only capability flags and a human label.  A recipe never
# embeds a file path, a store URI, or any other runtime-specific address.

RECIPE_COLD = from_capabilities(
    ExecutionCapability.FRESH_CONTEXT,
    ExecutionCapability.INDEPENDENT_REVIEWER,
    ExecutionCapability.NAMED_REFERENCE,
    recipe_name="cold",
)

RECIPE_WARM = from_capabilities(
    ExecutionCapability.INDEPENDENT_REVIEWER,
    ExecutionCapability.NAMED_REFERENCE,
    ExecutionCapability.WARM_START,
    recipe_name="warm",
)

RECIPE_RESUME = from_capabilities(
    ExecutionCapability.RESUME,
    ExecutionCapability.INDEPENDENT_REVIEWER,
    ExecutionCapability.NAMED_REFERENCE,
    recipe_name="resume",
)

RECIPE_MINIMAL = from_capabilities(
    ExecutionCapability.FRESH_CONTEXT,
    recipe_name="minimal",
)

#: A brand-new, independent re-review: marked ``RE_REVIEW`` (so it is a
#: *re*-review and not a first cold run), with no warm state carried over
#: (FRESH_CONTEXT) and gated by a separate reviewer role (INDEPENDENT_REVIEWER).
#: This is the capability projection SW-1510-ADAPTIVE criterion 2 names as one
#: of the only two reasons a Pro allocation is permitted.
RECIPE_RE_REVIEW = from_capabilities(
    ExecutionCapability.RE_REVIEW,
    ExecutionCapability.FRESH_CONTEXT,
    ExecutionCapability.INDEPENDENT_REVIEWER,
    ExecutionCapability.NAMED_REFERENCE,
    recipe_name="re-review",
)

RECIPE_FULL = from_capabilities(
    ExecutionCapability.FRESH_CONTEXT,
    ExecutionCapability.INDEPENDENT_REVIEWER,
    ExecutionCapability.NAMED_REFERENCE,
    ExecutionCapability.WARM_START,
    ExecutionCapability.RESUME,
    ExecutionCapability.PERSISTENT_STORE,
    ExecutionCapability.OBSERVER,
    recipe_name="full",
)


# ── Recipe registry ─────────────────────────────────────────────────────────
#
# All named recipes indexed by name.  Every entry is a pure capability
# projection — no runtime path, no store reference, no file location.
#
# The registry is the addressable-by-name set; it is deliberately left as the
# established five.  ``RECIPE_RE_REVIEW`` above is a canonical *capability
# projection* (the re-review composition) exported by symbol rather than
# registered by name, so the re-review marking rides on the composable
# ``RE_REVIEW`` capability rather than a new registry entry.

NAMED_RECIPES: dict[str, ExecutionStrategy] = {
    "cold": RECIPE_COLD,
    "warm": RECIPE_WARM,
    "resume": RECIPE_RESUME,
    "minimal": RECIPE_MINIMAL,
    "full": RECIPE_FULL,
}


def resolve_recipe(name: str) -> ExecutionStrategy:
    """Resolve *name* to its capability projection.

    Raises ``ValueError`` for an unknown recipe name.
    """
    if name not in NAMED_RECIPES:
        raise ValueError(
            f"unknown recipe '{name}' "
            f"(expected one of {sorted(NAMED_RECIPES)})"
        )
    return NAMED_RECIPES[name]


def compose_strategy(
    *capabilities: ExecutionCapability,
    recipe_name: Optional[str] = None,
) -> ExecutionStrategy:
    """Compose a strategy from arbitrary capabilities.

    Unlike ``resolve_recipe``, this allows ad-hoc combinations not limited
    to predefined names — the central proof that strategies are composable
    capabilities rather than an exclusive enum.
    """
    return from_capabilities(*capabilities, recipe_name=recipe_name)


# ── Backward-compatible mapping ─────────────────────────────────────────────
#
# Bridges the legacy ``execution_model`` string enum (``cold``/``warm``/``resume``)
# to the composable-capability world.

EXECUTION_MODEL_TO_CAPABILITIES: dict[str, ExecutionCapability] = {
    "cold": (
        ExecutionCapability.FRESH_CONTEXT
        | ExecutionCapability.INDEPENDENT_REVIEWER
        | ExecutionCapability.NAMED_REFERENCE
    ),
    "warm": (
        ExecutionCapability.INDEPENDENT_REVIEWER
        | ExecutionCapability.NAMED_REFERENCE
        | ExecutionCapability.WARM_START
    ),
    "resume": (
        ExecutionCapability.RESUME
        | ExecutionCapability.INDEPENDENT_REVIEWER
        | ExecutionCapability.NAMED_REFERENCE
    ),
}


def from_execution_model(model: str) -> ExecutionStrategy:
    """Convert a legacy ``execution_model`` string to a composable strategy.

    Raises ``ValueError`` for an unknown model name.
    """
    if model not in EXECUTION_MODEL_TO_CAPABILITIES:
        raise ValueError(
            f"unknown execution_model '{model}' "
            f"(expected one of {sorted(EXECUTION_MODEL_TO_CAPABILITIES)})"
        )
    caps = EXECUTION_MODEL_TO_CAPABILITIES[model]
    return ExecutionStrategy(capabilities=caps, recipe_name=model)


__all__ = [
    "ExecutionCapability",
    "ExecutionStrategy",
    "from_capabilities",
    "RECIPE_COLD",
    "RECIPE_WARM",
    "RECIPE_RESUME",
    "RECIPE_MINIMAL",
    "RECIPE_FULL",
    "RECIPE_RE_REVIEW",
    "NAMED_RECIPES",
    "resolve_recipe",
    "compose_strategy",
    "from_execution_model",
    "EXECUTION_MODEL_TO_CAPABILITIES",
]
