"""Core-side consumers of the SDK-owned lifecycle contracts (SW-160-GLE).

The Generic Lifecycle Extension splits one contract into four repositories
(``docs/architecture.md``, "Repository boundary"). The SDK owns the contract
*bytes*; this module is the Core-side consumer that loads contract IDs,
validation and vocabulary from the installed SDK API rather than from
hard-coded unpublished bytes.

* :class:`CategoryRegistry` -- the rebaselined category vocabulary. The eleven
  closed categories now include ``learn``. The registry loads the vocabulary
  from the installed ``skillweave_sdk.validator`` registry and never carries a
  private copy. Unknown categories fail closed.

* :class:`ProviderRegistry` -- the SDK extension point. A model or search
  provider is registered at wiring time with opaque, consumer-supplied
  ``hostFrameworkIdentifier`` / ``catalogueIdentifier`` values. The registry
  holds no enumeration of concrete providers, so adding one requires no
  contract change and no Core edit.

* the effective-profile snapshot contract -- Core re-exports the immutable,
  content-addressed :class:`~skillweave.profiles.effective.EffectiveProfileSnapshot`
  so a lifecycle consumer pins a run's profile through one seam.

* :class:`DenyOverridesPolicy` -- gate aggregation where a ``DENY`` is final.
  A later ``ALLOW`` can never overwrite it; ``SKIP`` and ``ABSTAIN`` are not
  consent, so a gate is only ``ALLOW`` when every decision is an explicit
  ``ALLOW``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional, Sequence, Union

import skillweave_sdk
import skillweave_sdk.validator as _sdk_validator

# The immutable, content-addressed effective-profile snapshot is owned by
# ``skillweave.profiles.effective``; the lifecycle contract surface re-exports
# it so a lifecycle consumer has one place to pin a run's profile.
from skillweave.profiles.effective import (
    ConflictError,
    EffectiveProfileError,
    EffectiveProfileSnapshot,
    PreviewExecutionError,
    ProfileSource,
    SchemaBindingError,
    SOURCE_KINDS,
    canonical_json_bytes,
    content_digest,
    resolve_effective_profile,
)

__all__ = [
    # contract set
    "CONTRACT_SET_NAME",
    "CONTRACT_SET_VERSION",
    "contracts_dir",
    "load_contract_lock",
    "ContractNotAvailableError",
    "ContractDriftError",
    # category taxonomy registry
    "CATEGORY_PREFIX",
    "DIMENSION_PREFIXES",
    "QualifiedCategoryRef",
    "CategoryRegistry",
    "tCategoryRegistry",
    "UnknownCategoryError",
    # provider extension point
    "PROVIDER_KINDS",
    "ProviderBinding",
    "ProviderRegistry",
    "UnknownProviderKindError",
    "UnknownProviderError",
    # effective profile snapshot contract
    "EffectiveProfileError",
    "SchemaBindingError",
    "ConflictError",
    "PreviewExecutionError",
    "EffectiveProfileSnapshot",
    "ProfileSource",
    "SOURCE_KINDS",
    "resolve_snapshot",
    "snapshot_digest",
    "verify_snapshot_digest",
    # gate aggregation
    "ALLOW",
    "DENY",
    "SKIP",
    "ABSTAIN",
    "DECISIONS",
    "GateDecision",
    "AggregatedGateResult",
    "DenyOverridesPolicy",
    "GateDecisionLog",
    "UnknownDecisionError",
]

# ── Contract set location ──────────────────────────────────────────────────

CONTRACT_SET_NAME = "skillweave-lifecycle-contracts"
CONTRACT_SET_VERSION = "1.0.0"
_CONTRACTS_DIRNAME = Path("schemas") / "lifecycle-contracts"


class ContractNotAvailableError(RuntimeError):
    """The pinned contract set could not be read.

    Core consumes the contract bytes; it never invents a vocabulary. When the
    contract directory is absent the registry refuses to start rather than
    falling back to a private copy.
    """


class ContractDriftError(RuntimeError):
    """The lock's vocabulary and the taxonomy schema's enums disagree."""


def contracts_dir(directory: Optional[Union[str, Path]] = None) -> Path:
    """Return the pinned contract directory (repo-relative by default).

    ``contracts.py`` lives at ``src/skillweave/lifecycle/contracts.py``, so the
    repository root is four parents up. An explicit ``directory`` overrides the
    lookup, which lets a consumer point at a pinned checkout.
    """
    if directory is not None:
        return Path(directory)
    return Path(__file__).resolve().parents[3] / _CONTRACTS_DIRNAME


def load_contract_lock(directory: Optional[Union[str, Path]] = None) -> dict:
    """Read the consumer receipt from the pinned contract set.

    The receipt records which SDK version was consumed and the vocabulary
    snapshot at consumption time. It does not embed schema bytes — the SDK
    is contract authority and owns the schemas.
    """
    path = contracts_dir(directory) / "contract-lock.json"
    if not path.is_file():
        raise ContractNotAvailableError(
            f"consumer receipt not found at {path}; Core consumes the "
            f"pinned SDK artifact and does not carry a private vocabulary copy"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ── Category taxonomy registry (GLE-003, rebaselined) ──────────────────────

#: Reference prefixes, one namespace per closed dimension. An explicit prefix
#: keeps ``kernel:learn`` distinguishable from ``category:learn`` even when an
#: id is legal in more than one namespace.
CATEGORY_PREFIX = "category"

DIMENSION_PREFIXES: dict[str, str] = {
    "category": "categories",
    "kernel": "kernelStages",
    "topology": "topologies",
    "human_coupling": "humanCoupling",
    "change_surface": "changeSurfaces",
}

# Accepted spellings for each prefix (the schema uses camelCase for the last
# two dimensions; both spellings resolve to the same namespace).
_PREFIX_ALIASES: dict[str, str] = {
    "category": "category",
    "kernel": "kernel",
    "topology": "topology",
    "human_coupling": "human_coupling",
    "humancoupling": "human_coupling",
    "change_surface": "change_surface",
    "changesurface": "change_surface",
}


class UnknownCategoryError(ValueError):
    """A category (or qualified reference id) is outside the closed vocabulary."""


@dataclass(frozen=True)
class QualifiedCategoryRef:
    """A namespaced vocabulary reference: ``category:learn`` / ``kernel:K6``."""

    kind: str
    id: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class CategoryRegistry:
    """The rebaselined, closed category vocabulary.

    The eleven categories -- ``research``, ``decide``, ``design``, ``author``,
    ``facilitate``, ``build``, ``transform``, ``assure``, ``operate``,
    ``publish`` and ``learn`` -- are loaded from the pinned contract set. The
    vocabulary is closed: an unknown category is refused, never coerced onto a
    nearest match.
    """

    categories: tuple[str, ...]
    kernel_stages: tuple[str, ...] = ()
    topologies: tuple[str, ...] = ()
    human_coupling: tuple[str, ...] = ()
    change_surfaces: tuple[str, ...] = ()
    contract_version: str = CONTRACT_SET_VERSION

    def __post_init__(self) -> None:
        for name, values in (
            ("categories", self.categories),
            ("kernel_stages", self.kernel_stages),
            ("topologies", self.topologies),
            ("human_coupling", self.human_coupling),
            ("change_surfaces", self.change_surfaces),
        ):
            if len(set(values)) != len(values):
                raise ContractDriftError(f"{name} contains duplicates: {values!r}")

    # -- construction ------------------------------------------------------

    @classmethod
    def from_contract_set(
        cls, directory: Optional[Union[str, Path]] = None
    ) -> "CategoryRegistry":
        """Load the registry from the installed SDK taxonomy schema.

        The SDK is contract authority; the category-taxonomy schema in the
        installed ``skillweave-sdk`` is the single source of truth for the
        closed vocabulary. Core does not carry a private copy.
        """
        reg = _sdk_validator.load_registry()

        # Locate the category-taxonomy schema in the SDK registry.
        taxonomy_schema = None
        for key, schema in reg.items():
            if "category-taxonomy" in key:
                taxonomy_schema = schema
                break

        if taxonomy_schema is None:
            raise ContractNotAvailableError(
                "SDK registry does not contain a category-taxonomy schema; "
                "the installed skillweave-sdk is not a lifecycle-contracts SDK"
            )

        defs = taxonomy_schema.get("$defs", {})

        return cls(
            categories=tuple(defs.get("category", {}).get("enum", [])),
            kernel_stages=tuple(defs.get("kernelStage", {}).get("enum", [])),
            topologies=tuple(defs.get("topology", {}).get("enum", [])),
            human_coupling=tuple(defs.get("humanCoupling", {}).get("enum", [])),
            change_surfaces=tuple(defs.get("changeSurface", {}).get("enum", [])),
            contract_version=CONTRACT_SET_VERSION,
        )

    # -- the rebaselined default -------------------------------------------

    _default: ClassVar[Optional["CategoryRegistry"]] = None

    @classmethod
    def rebaselined(cls) -> "CategoryRegistry":
        """The rebaselined registry (cached), loaded from the pinned contract set.

        ``learn`` is one of the eleven categories: the GLE rebaseline added it
        to the vocabulary the registry serves.
        """
        if cls._default is None:
            cls._default = cls.from_contract_set()
        return cls._default

    # -- lookups -----------------------------------------------------------

    def is_registered(self, category: str) -> bool:
        return category in self.categories

    def require(self, category: str) -> str:
        """Return ``category`` if registered, else fail closed."""
        if category not in self.categories:
            raise UnknownCategoryError(
                f"unknown category {category!r}; "
                f"registered: {list(self.categories)}"
            )
        return category

    def dimension(self, prefix: str) -> tuple[str, ...]:
        """The closed values of one namespaced dimension."""
        canonical = _PREFIX_ALIASES.get(prefix.lower())
        if canonical is None:
            raise UnknownCategoryError(
                f"unknown reference prefix {prefix!r}; "
                f"expected one of {sorted(DIMENSION_PREFIXES)}"
            )
        return {
            "category": self.categories,
            "kernel": self.kernel_stages,
            "topology": self.topologies,
            "human_coupling": self.human_coupling,
            "change_surface": self.change_surfaces,
        }[canonical]

    def parse_reference(self, reference: str) -> QualifiedCategoryRef:
        """Split ``kind:id`` into a :class:`QualifiedCategoryRef`, no validation."""
        if ":" not in reference:
            raise UnknownCategoryError(
                f"reference {reference!r} is not qualified; expected 'kind:id'"
            )
        prefix, _, identifier = reference.partition(":")
        canonical = _PREFIX_ALIASES.get(prefix.lower())
        if canonical is None:
            raise UnknownCategoryError(
                f"unknown reference prefix {prefix!r}; "
                f"expected one of {sorted(DIMENSION_PREFIXES)}"
            )
        return QualifiedCategoryRef(kind=canonical, id=identifier)

    def resolve_reference(self, reference: str) -> QualifiedCategoryRef:
        """Parse and validate a qualified reference against its own namespace.

        ``category:learn`` resolves (``learn`` is a category); ``kernel:learn``
        fails closed (the kernel stages are ``K0``..``K6``), which is exactly
        the separation the qualified form exists to make explicit.
        """
        ref = self.parse_reference(reference)
        if ref.id not in self.dimension(ref.kind):
            raise UnknownCategoryError(
                f"{ref} is not in the {ref.kind} vocabulary "
                f"{list(self.dimension(ref.kind))}"
            )
        return ref


# Backwards-friendly alias: the registry *is* the taxonomy registry.
tCategoryRegistry = CategoryRegistry


# ── Provider extension point (GLE-001 / GLE-003) ───────────────────────────

#: The provider contract kinds the SDK owns. This enumerates *contract shapes*,
#: not providers: no concrete provider name appears here or anywhere below.
PROVIDER_KINDS = ("model-provider", "search-provider")


class UnknownProviderKindError(ValueError):
    """A binding names a provider contract kind the SDK does not own."""


class UnknownProviderError(KeyError):
    """No provider is registered under the requested id."""


@dataclass(frozen=True)
class ProviderBinding:
    """One provider wired at the SDK extension point.

    The identifiers are opaque and consumer-supplied. The class never inspects
    them, never matches them against a vendor list, and never requires a
    particular provider to exist -- so a new provider is added purely by
    registering a binding.
    """

    kind: str
    id: str
    host_framework_identifier: str
    catalogue_identifier: str
    extras: Mapping[str, Any] = field(default_factory=dict)

    def to_contract(self, *, contract_version: str = CONTRACT_SET_VERSION) -> dict[str, Any]:
        """The provider-contract instance this binding declares.

        Emits exactly the fields the SDK ``model-provider`` /
        ``search-provider`` schemas require; ``extras`` carries optional,
        schema-declared fields (e.g. ``onFailure``, ``tier``, ``capabilities``).
        """
        payload: dict[str, Any] = {
            "contractVersion": contract_version,
            "id": self.id,
            "hostFrameworkIdentifier": self.host_framework_identifier,
            "catalogueIdentifier": self.catalogue_identifier,
        }
        for key, value in self.extras.items():
            payload.setdefault(key, value)
        return payload


class ProviderRegistry:
    """The SDK provider extension point: wiring-time registration, no enumeration.

    A registry starts empty. A consumer registers a provider with the opaque
    identifiers its host framework and catalogue use; nothing in Core needs to
    know the provider exists, and no Core list has to be edited to add one.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, ProviderBinding] = {}

    def register(self, binding: ProviderBinding) -> ProviderBinding:
        if binding.kind not in PROVIDER_KINDS:
            raise UnknownProviderKindError(
                f"unknown provider kind {binding.kind!r}; expected one of {PROVIDER_KINDS}"
            )
        if not binding.id:
            raise ValueError("a provider binding requires a non-empty id")
        if not binding.host_framework_identifier or not binding.catalogue_identifier:
            raise ValueError(
                f"provider {binding.id!r} requires opaque host and catalogue identifiers"
            )
        self._bindings[binding.id] = binding
        return binding

    def register_provider(
        self,
        *,
        kind: str,
        id: str,
        host_framework_identifier: str,
        catalogue_identifier: str,
        **extras: Any,
    ) -> ProviderBinding:
        """Register a provider by keyword; the extension-point entry point."""
        return self.register(
            ProviderBinding(
                kind=kind,
                id=id,
                host_framework_identifier=host_framework_identifier,
                catalogue_identifier=catalogue_identifier,
                extras=dict(extras),
            )
        )

    def get(self, provider_id: str) -> Optional[ProviderBinding]:
        return self._bindings.get(provider_id)

    def resolve(self, provider_id: str) -> ProviderBinding:
        binding = self._bindings.get(provider_id)
        if binding is None:
            raise UnknownProviderError(
                f"no provider registered under {provider_id!r}; "
                f"registered: {sorted(self._bindings)}"
            )
        return binding

    def ids(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    def of_kind(self, kind: str) -> tuple[ProviderBinding, ...]:
        return tuple(b for b in self._bindings.values() if b.kind == kind)

    def __len__(self) -> int:
        return len(self._bindings)


# ── Effective profile snapshot contract (GLE-011) ──────────────────────────


def resolve_snapshot(
    sources: Sequence[Mapping[str, Any]],
    *,
    schema_set: Optional[Sequence[tuple[str, str]]] = None,
) -> EffectiveProfileSnapshot:
    """Resolve ordered profile sources into one immutable snapshot.

    Thin lifecycle-side entry point over
    :func:`skillweave.profiles.effective.resolve_effective_profile`; the
    snapshot it returns is content-addressed and frozen.
    """
    return resolve_effective_profile(sources, schema_set=schema_set)


def snapshot_digest(snapshot: EffectiveProfileSnapshot) -> str:
    """The content digest of a snapshot's resolved profile."""
    return content_digest(snapshot.resolved)


def verify_snapshot_digest(snapshot: EffectiveProfileSnapshot, expected: str) -> bool:
    """True when ``snapshot`` still hashes to ``expected`` (content addressing)."""
    return snapshot.digest == expected and snapshot.canonical_bytes == canonical_json_bytes(
        snapshot.resolved
    )


# ── Gate aggregation: deny-overrides (GLE-006) ─────────────────────────────

ALLOW = "ALLOW"
DENY = "DENY"
SKIP = "SKIP"
ABSTAIN = "ABSTAIN"

#: Every legal decision. The policy is closed over this set.
DECISIONS = (ALLOW, DENY, SKIP, ABSTAIN)

#: A gate is only allowed when every decision is an explicit ALLOW. These are
#: the decisions that must never be read as consent.
_NON_CONSENT = (SKIP, ABSTAIN)


class UnknownDecisionError(ValueError):
    """A decision outside the closed ALLOW/DENY/SKIP/ABSTAIN set."""


@dataclass(frozen=True)
class GateDecision:
    """One evaluator's verdict on one gate."""

    gate: str
    decision: str
    actor: str = ""
    reason: str = ""
    evaluation_level: str = "node_completed"

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise UnknownDecisionError(
                f"unknown decision {self.decision!r}; expected one of {DECISIONS}"
            )


@dataclass(frozen=True)
class AggregatedGateResult:
    """The deny-overrides outcome for one gate."""

    gate: str
    decision: str
    reason: str
    deciders: tuple[str, ...] = ()
    vetoed_by: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.decision == ALLOW


@dataclass(frozen=True)
class DenyOverridesPolicy:
    """Aggregate evaluator decisions where a ``DENY`` is final.

    A single ``DENY`` makes the gate ``DENY`` regardless of how many later
    ``ALLOW`` decisions arrive: the outcome is order-independent, so no later
    positive evaluator can overwrite an unresolved critical fail. ``SKIP`` and
    ``ABSTAIN`` are not consent, so a gate is only ``ALLOW`` when every
    decision is an explicit ``ALLOW``.
    """

    def aggregate(
        self, gate: str, decisions: Sequence[GateDecision]
    ) -> AggregatedGateResult:
        if not decisions:
            return AggregatedGateResult(
                gate=gate,
                decision=DENY,
                reason="no evaluator decision recorded; deny by default",
            )

        for decision in decisions:
            if decision.gate != gate:
                raise ValueError(
                    f"decision for gate {decision.gate!r} aggregated under {gate!r}"
                )

        denied = [d for d in decisions if d.decision == DENY]
        if denied:
            return AggregatedGateResult(
                gate=gate,
                decision=DENY,
                reason="; ".join(d.reason or f"{d.actor or 'evaluator'} denied" for d in denied),
                deciders=tuple(d.actor for d in decisions),
                vetoed_by=tuple(d.actor for d in denied),
            )

        non_consent = [d for d in decisions if d.decision in _NON_CONSENT]
        if non_consent:
            return AggregatedGateResult(
                gate=gate,
                decision=DENY,
                reason=(
                    "no consent from "
                    + ", ".join(
                        f"{d.actor or 'evaluator'} ({d.decision})" for d in non_consent
                    )
                ),
                deciders=tuple(d.actor for d in decisions),
            )

        return AggregatedGateResult(
            gate=gate,
            decision=ALLOW,
            reason=f"gate '{gate}' allowed by unanimous decision",
            deciders=tuple(d.actor for d in decisions),
        )


class GateDecisionLog:
    """An append-only decision log whose aggregate latches a ``DENY``.

    Recording a later ``ALLOW`` after a ``DENY`` cannot flip the gate back:
    :meth:`outcome` re-aggregates under deny-overrides on every read.
    """

    def __init__(self, gate: str, policy: Optional[DenyOverridesPolicy] = None) -> None:
        self.gate = gate
        self._policy = policy or DenyOverridesPolicy()
        self._decisions: list[GateDecision] = []

    def record(self, decision: GateDecision) -> AggregatedGateResult:
        if decision.gate != self.gate:
            raise ValueError(
                f"decision for gate {decision.gate!r} recorded in log for {self.gate!r}"
            )
        self._decisions.append(decision)
        return self.outcome()

    @property
    def decisions(self) -> tuple[GateDecision, ...]:
        return tuple(self._decisions)

    def outcome(self) -> AggregatedGateResult:
        return self._policy.aggregate(self.gate, self._decisions)
