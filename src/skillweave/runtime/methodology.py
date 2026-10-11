"""Versioned methodology registry and compatibility matrix (SW-159-METHOD-001).

Defines four methodologies (REX, Ralph, Gauntlet, Assay) without harness or
model identities. Topology, methodology, policy, risk, authority, and checkpoints
are serialized as separate top-level keys. Incompatible combinations are rejected
with named reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ── Enums ────────────────────────────────────────────────────────────────────


class Methodology(str, Enum):
    """Registered methodologies for SkillWeave execution.

    Each value names a distinct approach to structuring and governing work.
    No methodology carries a harness identity, model identity, or provider
    reference — only what the methodology *is*.
    """

    REX = "rex"
    RALPH = "ralph"
    GAUNTLET = "gauntlet"
    ASSAY = "assay"


class TopologyType(str, Enum):
    """Topology types a methodology can coordinate over."""

    SEQUENTIAL = "sequential"
    DAG = "dag"
    PARALLEL_LANES = "parallel_lanes"
    WAVE = "wave"
    HIERARCHICAL = "hierarchical"
    PHASED = "phased"


class PolicyType(str, Enum):
    """Execution policy types that govern how work proceeds."""

    SUPERVISED = "supervised"
    APPROVAL_REQUIRED = "approval_required"
    COLLABORATIVE = "collaborative"
    HUMAN_LED = "human_led"
    AUTONOMOUS = "autonomous"


class RiskLevel(str, Enum):
    """Risk classification for a methodology."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuthorityLevel(str, Enum):
    """Authority requirements for a methodology."""

    NONE = "none"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    HUMAN = "human"


# ── Methodology definition ───────────────────────────────────────────────────


@dataclass
class MethodologyDefinition:
    """One methodology entry in the versioned registry.

    Contains **no** harness identity, model identity, or provider reference —
    only what the methodology *is*: its name, supported topologies, supported
    policies, risk level, authority requirement, checkpoint support, and schema
    version.
    """

    methodology: Methodology
    description: str
    supported_topologies: list[TopologyType] = field(default_factory=list)
    supported_policies: list[PolicyType] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    authority: AuthorityLevel = AuthorityLevel.NONE
    checkpoints: bool = False
    version: str = "1.0.0"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict with no nested concerns."""
        return {
            "methodology": self.methodology.value,
            "description": self.description,
            "supported_topologies": [t.value for t in self.supported_topologies],
            "supported_policies": [p.value for p in self.supported_policies],
            "risk": self.risk.value,
            "authority": self.authority.value,
            "checkpoints": self.checkpoints,
            "version": self.version,
        }


# ── Registry ─────────────────────────────────────────────────────────────────


class MethodologyRegistry:
    """Versioned registry of all known methodologies.

    Entries are indexed by :class:`Methodology` enum value. The registry is
    populated with four default entries (REX, Ralph, Gauntlet, Assay) on
    construction. No harness identity, model identity, or provider reference
    is stored.
    """

    def __init__(self) -> None:
        self._entries: dict[Methodology, MethodologyDefinition] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self._entries[Methodology.REX] = MethodologyDefinition(
            methodology=Methodology.REX,
            description="Rapid EXecution — simple, sequential, minimal overhead",
            supported_topologies=[TopologyType.SEQUENTIAL, TopologyType.DAG],
            supported_policies=[PolicyType.SUPERVISED, PolicyType.AUTONOMOUS],
            risk=RiskLevel.LOW,
            authority=AuthorityLevel.REVIEWER,
            checkpoints=False,
            version="1.0.0",
        )
        self._entries[Methodology.RALPH] = MethodologyDefinition(
            methodology=Methodology.RALPH,
            description="Ralph Loop — iterative, review-gated with correction cycles",
            supported_topologies=[TopologyType.DAG, TopologyType.WAVE],
            supported_policies=[
                PolicyType.APPROVAL_REQUIRED,
                PolicyType.COLLABORATIVE,
            ],
            risk=RiskLevel.MEDIUM,
            authority=AuthorityLevel.APPROVER,
            checkpoints=True,
            version="1.0.0",
        )
        self._entries[Methodology.GAUNTLET] = MethodologyDefinition(
            methodology=Methodology.GAUNTLET,
            description=(
                "Gauntlet — multi-model deliberation with council routing"
            ),
            supported_topologies=[TopologyType.PARALLEL_LANES, TopologyType.DAG],
            supported_policies=[PolicyType.COLLABORATIVE, PolicyType.HUMAN_LED],
            risk=RiskLevel.HIGH,
            authority=AuthorityLevel.HUMAN,
            checkpoints=True,
            version="1.0.0",
        )
        self._entries[Methodology.ASSAY] = MethodologyDefinition(
            methodology=Methodology.ASSAY,
            description="Assay — experimental, probe-based exploration",
            supported_topologies=[
                TopologyType.SEQUENTIAL,
                TopologyType.DAG,
                TopologyType.PARALLEL_LANES,
                TopologyType.WAVE,
                TopologyType.HIERARCHICAL,
                TopologyType.PHASED,
            ],
            supported_policies=[PolicyType.SUPERVISED],
            risk=RiskLevel.LOW,
            authority=AuthorityLevel.REVIEWER,
            checkpoints=False,
            version="1.0.0",
        )

    def get(self, methodology: Methodology) -> MethodologyDefinition:
        """Look up a methodology by enum value.

        Raises ``KeyError`` for an unknown methodology.
        """
        if not isinstance(methodology, Methodology):
            raise KeyError(
                f"expected a Methodology enum value, got {type(methodology).__name__}"
            )
        if methodology not in self._entries:
            raise KeyError(f"unknown methodology '{methodology.value}'")
        return self._entries[methodology]

    def list(self) -> list[MethodologyDefinition]:
        """Return all registered methodology definitions."""
        return list(self._entries.values())

    def __contains__(self, methodology: object) -> bool:
        return (
            isinstance(methodology, Methodology)
            and methodology in self._entries
        )


#: Default registry instance. Used by all compatibility checks and migration
#: so that callers do not need to instantiate their own registry.
DEFAULT_REGISTRY = MethodologyRegistry()


# ── Compatibility matrix ─────────────────────────────────────────────────────
#
# The matrix below defines the *known-incompatible* combinations. Each entry
# carries a named reason explaining *why* the combination is refused — never a
# silent coercion or a generic "not supported" message.
#
# Combinations not listed here are checked against the methodology definition's
# supported_topologies and supported_policies arrays.

_INCOMPATIBLE_REASONS: dict[tuple[Methodology, TopologyType, PolicyType], str] = {
    (
        Methodology.REX,
        TopologyType.SEQUENTIAL,
        PolicyType.HUMAN_LED,
    ): (
        "REX is a rapid-execution methodology and does not support human-led "
        "operation; human-led requires iterative deliberation"
    ),
    (
        Methodology.RALPH,
        TopologyType.SEQUENTIAL,
        PolicyType.APPROVAL_REQUIRED,
    ): (
        "Ralph Loop requires iterative review cycles which sequential topology "
        "cannot provide; use DAG or WAVE topology"
    ),
    (
        Methodology.GAUNTLET,
        TopologyType.PARALLEL_LANES,
        PolicyType.AUTONOMOUS,
    ): (
        "Gauntlet requires multi-model deliberation which cannot run "
        "autonomously; use collaborative or human-led policy"
    ),
    (
        Methodology.ASSAY,
        TopologyType.SEQUENTIAL,
        PolicyType.APPROVAL_REQUIRED,
    ): (
        "Assay is experimental and must not block on human approval; "
        "use supervised policy"
    ),
}


def check_compatibility(
    methodology: Methodology,
    topology: TopologyType,
    policy: PolicyType,
) -> Optional[str]:
    """Check if the combination of *methodology*, *topology*, and *policy*
    is compatible.

    Returns ``None`` when the combination is supported, or a **named reason**
    string describing why the combination is incompatible. The caller can
    use the reason for diagnostics or structured error output without parsing
    a generic error message.

    The check proceeds in order:

    1. Known-incompatible combinations (the explicit matrix above).
    2. Topology not in the methodology's supported set.
    3. Policy not in the methodology's supported set.
    """
    reason = _INCOMPATIBLE_REASONS.get((methodology, topology, policy))
    if reason is not None:
        return reason

    definition = DEFAULT_REGISTRY.get(methodology)

    if topology not in definition.supported_topologies:
        return (
            f"methodology '{methodology.value}' does not support topology "
            f"'{topology.value}'; supported topologies: "
            f"{[t.value for t in definition.supported_topologies]}"
        )

    if policy not in definition.supported_policies:
        return (
            f"methodology '{methodology.value}' does not support policy "
            f"'{policy.value}'; supported policies: "
            f"{[p.value for p in definition.supported_policies]}"
        )

    return None


# ── Error type ───────────────────────────────────────────────────────────────


class MethodologyError(ValueError):
    """Raised for incompatible or unknown methodology combinations.

    ``reason`` carries the named reason the combination was rejected, so
    callers can handle structured errors without string parsing.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def assert_compatible(
    methodology: Methodology,
    topology: TopologyType,
    policy: PolicyType,
) -> None:
    """Assert that the combination is compatible.

    Raises :class:`MethodologyError` with a named reason when incompatible.
    """
    reason = check_compatibility(methodology, topology, policy)
    if reason is not None:
        raise MethodologyError(
            f"incompatible combination: methodology={methodology.value}, "
            f"topology={topology.value}, policy={policy.value}",
            reason=reason,
        )


# ── Legacy migration ─────────────────────────────────────────────────────────
#
# The mapping from the legacy ``execution_model`` string enum to the new
# Methodology enum is deterministic. Each legacy value maps to exactly one
# methodology; unrecognised values raise a MethodologyError with an actionable
# message.

_LEGACY_TO_METHODOLOGY: dict[str, Methodology] = {
    "cold": Methodology.REX,
    "warm": Methodology.RALPH,
    "resume": Methodology.RALPH,
}


def migrate_legacy_method(execution_model: str) -> Methodology:
    """Migrate a legacy ``execution_model`` string to a :class:`Methodology`.

    The mapping is deterministic:

    * ``"cold"`` → :attr:`Methodology.REX`
    * ``"warm"`` → :attr:`Methodology.RALPH`
    * ``"resume"`` → :attr:`Methodology.RALPH`

    Raises :class:`MethodologyError` for unrecognised values — no silent
    default, no coercion to a "best guess".
    """
    methodology = _LEGACY_TO_METHODOLOGY.get(execution_model)
    if methodology is None:
        raise MethodologyError(
            f"cannot migrate legacy execution_model '{execution_model}': "
            f"no deterministic mapping exists; "
            f"expected one of {sorted(_LEGACY_TO_METHODOLOGY)}",
            reason=f"unknown_legacy_value:{execution_model}",
        )
    return methodology


# ── Separate serialization ───────────────────────────────────────────────────


def serialize_methodology_state(
    methodology: Methodology,
    topology: TopologyType,
    policy: PolicyType,
    risk: Optional[RiskLevel] = None,
    authority: Optional[AuthorityLevel] = None,
    checkpoints: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Serialize topology, methodology, policy, risk, authority, and checkpoints
    as separate top-level keys.

    Each concern is independently addressable — no key nests another concern.
    When ``risk`` or ``authority`` is not provided, the value is derived from
    the methodology's definition.
    """
    definition = DEFAULT_REGISTRY.get(methodology)
    return {
        "topology": topology.value,
        "methodology": methodology.value,
        "policy": policy.value,
        "risk": (risk or definition.risk).value,
        "authority": (authority or definition.authority).value,
        "checkpoints": checkpoints or [],
    }


__all__ = [
    "Methodology",
    "TopologyType",
    "PolicyType",
    "RiskLevel",
    "AuthorityLevel",
    "MethodologyDefinition",
    "MethodologyRegistry",
    "DEFAULT_REGISTRY",
    "check_compatibility",
    "assert_compatible",
    "MethodologyError",
    "migrate_legacy_method",
    "serialize_methodology_state",
]
