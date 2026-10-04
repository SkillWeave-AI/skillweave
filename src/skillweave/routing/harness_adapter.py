"""The generic runner/harness adapter interface, with the router as decider (SW-158).

Four things live here, and the boundary between them is the point:

1. **The adapter interface is generic.** :class:`HarnessAdapter` is an abstract
   contract an adapter plugin implements — its name, the capabilities it holds,
   how it turns a decision into a launch command, and how it hands the work to a
   launcher seam. The interface itself names *no* concrete harness: every
   host-specific fact (identity, capability set, argv) belongs to a plugin
   subclass. The core never branches on an adapter name; it reads capability
   data. An adapter that cannot honour a decision is refused by name, never
   silently downgraded.

2. **SkillWeave states what it needs, not what to run.** :class:`AdapterRequest`
   carries a :class:`CapabilityRequirement` — the provider-neutral capabilities
   the run requires *plus* the complexity signal (a
   :class:`~skillweave.routing.decide.ComplexityRank`, a bare rank, or a tier
   name). The capabilities are validated against
   :data:`HARNESS_CAPABILITIES` and an unknown one falls closed at construction,
   so a typo can never read as "no requirement".

3. **The router owns the final decision.** :class:`RouterSeam` is the seam the
   core hands the requirement to. The router — :class:`FaigateRouter` is the
   default implementation, but any seam will do — decides the *model mix* and
   the *dispatch strategy* and returns them as a :class:`DispatchStrategy`. The
   adapter does not choose models and does not choose the strategy; it accepts
   the router's decision or refuses it. A different router therefore produces a
   different decision with no change to any adapter.

4. **A plugin implements the interface.** :class:`OpenClaudeAdapter` is one
   such plugin: it declares its own identity, its own capability set, and its
   own transport argv, and does nothing the generic interface does not sanction.
   :class:`HarnessAdapterRegistry` finds plugins by their declared name and by
   capability, so the composition root wires a plugin in without the core ever
   naming it.

Layering note
-------------

The capability vocabulary is declared here rather than imported from
``skillweave.dispatch.harness_contract`` because ``dispatch`` depends on
``routing`` and not the other way round; a backwards import would be a cycle.
The two vocabularies are the same provider-neutral contract, and the integration
test asserts they stay in lockstep, so the duplication cannot drift silently.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .decide import ComplexityRank
from .profile import (
    TIER_BALANCED,
    TIER_DEEP,
    TIER_FAST,
    VALID_TIERS,
    tier_to_router,
)


# ── The provider-neutral capability vocabulary ─────────────────────────────

#: The capability names an adapter may declare and a run may require. The core
#: reads these as data; a name outside this tuple is refused rather than
#: treated as satisfied. Kept in lockstep with
#: ``skillweave.dispatch.harness_contract.CAPABILITIES`` (asserted by the
#: integration test) — the two are one contract, declared on each side of the
#: one-way ``dispatch -> routing`` dependency.
HARNESS_CAPABILITIES: tuple[str, ...] = (
    "native-tool",
    "external-process",
    "in-place",
    "stdin",
    "status",
    "cancel",
    "state-namespace",
    "installed-skill-digest",
)

#: The capability a plan must state for the native-tool admission gate to arm.
#: A plan that does not require native-tool keeps the ordinary raw-command
#: compatibility (see :meth:`HarnessAdapter.execute`): the gate refuses only
#: when native execution is *claimed as required*, never silently.
NATIVE_TOOL_CAPABILITY = "native-tool"


# ── The dispatch strategies the router may choose ──────────────────────────

STRATEGY_INLINE = "inline"
STRATEGY_SINGLE = "single"
STRATEGY_PARALLEL = "parallel"

#: What each strategy requires of the adapter that will honour it. The router
#: chooses the strategy; the adapter's capability set is then reconciled against
#: this table before any launch. Adding a strategy edits data; it never adds a
#: branch to an adapter.
STRATEGY_CAPABILITIES: dict[str, tuple[str, ...]] = {
    STRATEGY_INLINE: ("in-place",),
    STRATEGY_SINGLE: ("external-process", "stdin"),
    STRATEGY_PARALLEL: ("external-process", "status"),
}


class HarnessAdapterError(ValueError):
    """A requirement, router decision, plan, or adapter capability is invalid.

    Raised before any launch. ``asset`` names the field that failed (a
    capability, an adapter, a complexity value) whenever one can be named, so
    the refusal is attributable rather than a bare NO.
    """

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


# ── What SkillWeave asks for ───────────────────────────────────────────────

#: The rank axis ``decide`` consumes: 0 -> fast, 1 -> balanced, >=2 -> deep.
#: The conversion is declared here once, as the same ordered tuple, so the
#: complexity signal the router reads is the one measure ``decide`` already
#: uses — never a second, disagreeing scale.
_RANK_TO_TIER = (TIER_FAST, TIER_BALANCED, TIER_DEEP)


def tier_for_complexity(
    complexity: Any,
) -> tuple[str, Optional[ComplexityRank]]:
    """Resolve the complexity signal onto the tier axis the router casts from.

    Accepts exactly the three forms ``decide`` accepts, and refuses anything
    else by name:

    * a :class:`~skillweave.routing.decide.ComplexityRank` — its raw metrics are
      kept and returned alongside the derived tier, so the record can name which
      input produced which tier;
    * a bare non-negative integer rank (0 -> fast, 1 -> balanced, >=2 -> deep);
    * a tier name (``fast``/``balanced``/``deep``).

    Returns ``(tier, rank_or_none)``: the rank is non-``None`` exactly when raw
    metrics were converted here. A ``None``, negative, or unrecognised value
    falls closed with :class:`HarnessAdapterError`.
    """
    if isinstance(complexity, ComplexityRank):
        return _tier_from_rank(complexity.rank), complexity
    if isinstance(complexity, str):
        if complexity not in VALID_TIERS:
            raise HarnessAdapterError(
                f"unknown complexity '{complexity}' "
                f"(expected one of {sorted(VALID_TIERS)})",
                asset=complexity,
            )
        return complexity, None
    if isinstance(complexity, int) and not isinstance(complexity, bool):
        if complexity < 0:
            raise HarnessAdapterError(
                f"complexity rank must be non-negative, got {complexity}",
                asset="complexity",
            )
        return _tier_from_rank(complexity), None
    raise HarnessAdapterError(
        "complexity must be a ComplexityRank, a tier name, or a non-negative "
        f"integer, got {complexity!r}",
        asset="complexity",
    )


def _tier_from_rank(rank: int) -> str:
    """Map a non-negative rank onto the named tier axis (shared tail)."""
    return _RANK_TO_TIER[min(rank, len(_RANK_TO_TIER) - 1)]


@dataclass(frozen=True)
class CapabilityRequirement:
    """What a run needs of an adapter, plus the complexity that drives routing.

    ``capabilities`` is the provider-neutral subset the run requires; an empty
    tuple means the run requires nothing specific. ``complexity`` is the
    declared signal the router casts from — it is required, because a router
    asked to decide without one would have to invent it. An unknown capability
    name, or a missing complexity, is refused here (falls closed) rather than
    surfacing later as a satisfied requirement.
    """

    complexity: Any
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.complexity is None:
            raise HarnessAdapterError(
                "a capability requirement needs a complexity signal to route on",
                asset="complexity",
            )
        caps = tuple(self.capabilities)
        if isinstance(self.capabilities, str) or not isinstance(caps, tuple):
            raise HarnessAdapterError(
                "requirement capabilities must be a tuple of capability names",
                asset="capabilities",
            )
        for cap in caps:
            if cap not in HARNESS_CAPABILITIES:
                raise HarnessAdapterError(
                    f"unknown capability '{cap}' "
                    f"(expected one of {list(HARNESS_CAPABILITIES)})",
                    asset=cap,
                )
        object.__setattr__(self, "capabilities", caps)

    @property
    def required(self) -> frozenset[str]:
        return frozenset(self.capabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": list(self.capabilities),
            "complexity": (
                self.complexity.to_dict()
                if isinstance(self.complexity, ComplexityRank)
                else self.complexity
            ),
        }


@dataclass(frozen=True)
class AdapterRequest:
    """One run's hand-off to the adapter: what it needs, and the work itself.

    This is the object SkillWeave passes to the router. It carries the
    :class:`CapabilityRequirement` (capabilities + complexity) verbatim and the
    exact work bytes, so the router decides from the stated need and the adapter
    later receives the decision alongside the same request — never a
    reconstructed one.
    """

    run_id: str
    role: str
    requirement: CapabilityRequirement
    work: bytes = b""
    subject_repo: str = ""
    subject_commit: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.requirement, CapabilityRequirement):
            raise HarnessAdapterError(
                "an adapter request requires a CapabilityRequirement",
                asset="requirement",
            )
        if not isinstance(self.work, (bytes, bytearray)):
            raise HarnessAdapterError(
                "adapter request work must be bytes",
                asset="work",
            )
        object.__setattr__(self, "work", bytes(self.work))

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self.requirement.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "role": self.role,
            "requirement": self.requirement.to_dict(),
            "work_bytes": len(self.work),
            "subject_repo": self.subject_repo,
            "subject_commit": self.subject_commit,
        }


# ── What the router decides ────────────────────────────────────────────────

@dataclass(frozen=True)
class DispatchStrategy:
    """The router's final decision: which models run, and how they are driven.

    ``model_mix`` is the concrete model pool the router cast; ``strategy`` is
    one of :data:`STRATEGY_CAPABILITIES` (inline, single, parallel); ``mode`` is
    the router's own stage mode; ``router`` names the router that decided. The
    adapter neither fills nor overrides these fields — it accepts them or
    refuses the plan. Every field is validated at construction so a decision
    with no models, an unknown strategy, or an unnamed router never reaches a
    launch.
    """

    strategy: str
    model_mix: tuple[str, ...]
    mode: str
    router: str
    chairman: Optional[str] = None
    tier: Optional[str] = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGY_CAPABILITIES:
            raise HarnessAdapterError(
                f"unknown dispatch strategy '{self.strategy}' "
                f"(expected one of {sorted(STRATEGY_CAPABILITIES)})",
                asset="strategy",
            )
        models = tuple(str(m) for m in self.model_mix)
        if not models or any(not m.strip() for m in models):
            raise HarnessAdapterError(
                "a dispatch decision must name at least one non-empty model",
                asset="model_mix",
            )
        if not isinstance(self.router, str) or not self.router.strip():
            raise HarnessAdapterError(
                "a dispatch decision must name the router that made it",
                asset="router",
            )
        object.__setattr__(self, "model_mix", models)

    def required_capabilities(self) -> tuple[str, ...]:
        """The capabilities the router's chosen strategy demands of an adapter."""
        return STRATEGY_CAPABILITIES[self.strategy]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "model_mix": list(self.model_mix),
            "mode": self.mode,
            "router": self.router,
            "chairman": self.chairman,
            "tier": self.tier,
            "rationale": self.rationale,
        }


# ── The router seam ────────────────────────────────────────────────────────

class RouterSeam(ABC):
    """The seam that owns the model-mix and dispatch-strategy decision.

    The core hands a :class:`AdapterRequest` to :meth:`route` and uses whatever
    :class:`DispatchStrategy` comes back, unchanged. ``name`` identifies the
    deciding router in the record. Any object exposing ``name`` and ``route``
    satisfies the seam; :class:`FaigateRouter` is the default implementation.
    """

    name: str

    @abstractmethod
    def route(self, request: AdapterRequest) -> DispatchStrategy:
        """Decide the model mix and dispatch strategy for ``request``."""


class FaigateRouter(RouterSeam):
    """The default router: cast the tier from complexity against the presets.

    The tier is derived from the request's complexity signal through the shared
    :func:`tier_for_complexity` step, then mapped onto a router preset and a
    stage mode through the existing ``tier_to_router`` table. The preset's model
    pool *is* the model mix — this never invents a model — and the strategy is
    parallel when the router casts more than one model, single otherwise.

    ``presets`` is injectable so a caller (and the tests) can route against a
    hermetic table without touching the live ``ROUTER_PROFILES``.
    """

    name = "faigate"

    def __init__(self, presets: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        if presets is None:
            from .faigate_adapter import ROUTER_PROFILES

            presets = ROUTER_PROFILES
        if not isinstance(presets, Mapping) or not presets:
            raise HarnessAdapterError(
                "faigate routing needs a non-empty preset table",
                asset="presets",
            )
        self._presets = {str(k): dict(v) for k, v in presets.items()}

    def route(self, request: AdapterRequest) -> DispatchStrategy:
        tier, rank = tier_for_complexity(request.requirement.complexity)
        preset_name, mode = tier_to_router(tier)
        preset = self._presets.get(preset_name)
        if preset is None:
            raise HarnessAdapterError(
                f"faigate has no preset '{preset_name}' for tier '{tier}'",
                asset=preset_name,
            )
        models = tuple(str(m) for m in (preset.get("models") or ()))
        if not models:
            raise HarnessAdapterError(
                f"faigate preset '{preset_name}' casts no models",
                asset=preset_name,
            )
        strategy = STRATEGY_PARALLEL if len(models) > 1 else STRATEGY_SINGLE
        cast = f" ({len(models)} models)" if rank is None else ""
        return DispatchStrategy(
            strategy=strategy,
            model_mix=models,
            mode=mode,
            router=self.name,
            chairman=preset.get("chairman"),
            tier=tier,
            rationale=(
                f"tier '{tier}'{cast} -> preset '{preset_name}', mode '{mode}', "
                f"strategy '{strategy}'"
            ),
        )


# ── The generic adapter interface ──────────────────────────────────────────

@dataclass(frozen=True)
class HarnessPlan:
    """A decision the adapter has accepted: the router's strategy plus the
    capability set the adapter must hold to honour it.

    ``required_capabilities`` is the union of what the run asked for and what
    the router's strategy demands. A plan exists only when the adapter holds
    every one of them, so a plan is always executable by the adapter that
    produced it.
    """

    adapter: str
    request: AdapterRequest
    strategy: DispatchStrategy
    required_capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "request": self.request.to_dict(),
            "strategy": self.strategy.to_dict(),
            "required_capabilities": list(self.required_capabilities),
        }


@dataclass(frozen=True)
class HarnessOutcome:
    """What an executed plan produced: the command, and the launcher's result.

    ``succeeded`` reflects the launcher's own verdict. When the result cannot
    state one, the outcome reads as not-succeeded (falls closed) rather than
    claiming a success nobody reported.
    """

    adapter: str
    plan: HarnessPlan
    command: tuple[str, ...]
    result: Any = None

    @property
    def succeeded(self) -> bool:
        return bool(getattr(self.result, "succeeded", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "command": list(self.command),
            "strategy": self.plan.strategy.to_dict(),
            "succeeded": self.succeeded,
        }


class HarnessAdapter(ABC):
    """The generic runner/harness adapter interface. No harness is named here.

    An adapter plugin declares its own ``name`` and its own ``capabilities()``
    as data; the interface provides the two shared steps:

    * :meth:`plan` hands the request to the router, takes the router's decision
      verbatim, and reconciles it against the adapter's declared capabilities.
      A missing capability — whether the run required it or the router's
      strategy demands it — refuses the plan by name, before any launch.
    * :meth:`execute` asks the plugin for its own transport argv
      (:meth:`build_command`) and passes the work to the injected ``launch``
      seam, returning a :class:`HarnessOutcome`.

    Nothing in this class branches on an adapter name, a model id, or a
    strategy name beyond the declared :data:`STRATEGY_CAPABILITIES` table.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The plugin's own identity, opaque to the generic core."""

    @abstractmethod
    def capabilities(self) -> frozenset[str]:
        """The capability names this plugin holds, declared as data."""

    @abstractmethod
    def build_command(self, plan: HarnessPlan) -> Sequence[str]:
        """The plugin's own transport argv for an accepted plan.

        This is the only host-specific knowledge in the class: it belongs to the
        plugin, never to the interface.
        """

    def supports(self, capability: str) -> bool:
        """Whether this plugin holds ``capability``.

        Falls closed: an unknown or absent capability is ``False``, never a
        benefit of the doubt.
        """
        return capability in self.capabilities()

    def plan(
        self,
        request: AdapterRequest,
        router: Optional[RouterSeam] = None,
    ) -> HarnessPlan:
        """Route ``request``, then accept or refuse the router's decision.

        The router decides; the adapter only reconciles. ``router`` defaults to
        :class:`FaigateRouter`, so a caller states its need and nothing else.
        A router that cannot route, a decision the adapter cannot honour, or a
        requirement the adapter does not meet all fall closed with
        :class:`HarnessAdapterError` naming the asset.
        """
        if not isinstance(request, AdapterRequest):
            raise HarnessAdapterError(
                "plan() requires an AdapterRequest",
                asset="request",
            )
        decider = FaigateRouter() if router is None else router
        if not callable(getattr(decider, "route", None)):
            raise HarnessAdapterError(
                "the router seam must expose route(request)",
                asset="router",
            )

        decision = decider.route(request)
        if not isinstance(decision, DispatchStrategy):
            raise HarnessAdapterError(
                "the router must return a DispatchStrategy",
                asset="router",
            )

        needed = tuple(
            dict.fromkeys(
                tuple(request.requirement.capabilities)
                + tuple(decision.required_capabilities())
            )
        )
        missing = [cap for cap in needed if not self.supports(cap)]
        if missing:
            raise HarnessAdapterError(
                f"adapter '{self.name}' cannot honour the decision from router "
                f"'{decision.router}': missing capability {missing}",
                asset=missing[0],
            )
        return HarnessPlan(
            adapter=self.name,
            request=request,
            strategy=decision,
            required_capabilities=needed,
        )

    def execute(
        self,
        plan: HarnessPlan,
        *,
        launch: Callable[[Sequence[str], HarnessPlan], Any],
        capability_snapshot: Any = None,
        native_requirements: Any = None,
        host_binding: Any = None,
    ) -> HarnessOutcome:
        """Hand an accepted plan to the ``launch`` seam and record the outcome.

        ``launch`` is injected: the interface owns the hand-off, not the
        process. A non-callable seam is refused before the plugin's argv is
        even built, so a missing launcher is never mistaken for a run.

        When ``plan`` requires :data:`NATIVE_TOOL_CAPABILITY`, the measured
        capability handshake must be supplied through ``capability_snapshot``,
        ``native_requirements`` and ``host_binding`` and must admit the plan
        (:func:`admit_native_plan`) **before** any argv is built or any launch
        happens. An absent, stale, contradictory or configuration-mismatched
        handshake raises :class:`HarnessAdapterError` here — never a quiet
        legacy fallback. A plan that does not require native-tool keeps the
        ordinary raw-command compatibility, which is explicitly *not* native
        skill execution.
        """
        if not isinstance(plan, HarnessPlan):
            raise HarnessAdapterError(
                "execute() requires a HarnessPlan",
                asset="plan",
            )
        if not callable(launch):
            raise HarnessAdapterError(
                "execute() requires a callable launch seam",
                asset="launch",
            )
        admission = admit_native_plan(
            plan,
            capability_snapshot=capability_snapshot,
            native_requirements=native_requirements,
            host_binding=host_binding,
        )
        if not admission.admitted:
            raise HarnessAdapterError(
                "native-tool admission refused before launch: " + admission.reason,
                asset=admission.asset,
            )
        command = tuple(str(part) for part in self.build_command(plan))
        if not command:
            raise HarnessAdapterError(
                f"adapter '{self.name}' built an empty command",
                asset="command",
            )
        result = launch(list(command), plan)
        return HarnessOutcome(
            adapter=self.name,
            plan=plan,
            command=command,
            result=result,
        )


# ── Native capability admission (SW-1510-CAP) ──────────────────────────────
#
# The native-tool execution boundary. A plan that requires native-tool must be
# admitted against a *measured* capability snapshot and a *detected* host
# binding before any argv is built or any launch happens. This gate is
# deliberately generic — it names no concrete harness — and it is data-driven:
# every native fact it reads (child tools/mode, coordinator config, host binding)
# is an observation record, never a branch in core.
#
# The measurement and the pinning are the point; what is *consumed* here is the
# frozen ``capability-snapshot-v1`` contract (SDK), whose ``capabilities`` object
# this gate reads as an open data record carrying distinct ``declared``,
# ``detected`` and ``run_attested`` facts plus the host binding. The snapshot's
# top-level required shape is enforced by the SDK schema, not duplicated here;
# what this module adds is the *admission* semantics the schema cannot express:
# a signature (``authenticated=true``) is not a measurement, a declaration is
# not a detection, and a detection is not a run-attested expansion.

#: The frozen snapshot schema version this gate consumes. A snapshot naming any
#: other version fails closed — the gate never best-effort parses a future or
#: unspecified wire contract.
CAPABILITY_SNAPSHOT_SCHEMA_VERSION = "1.0.0"

#: The snapshot revision field is retained for audit but is **unused** for
#: admission: a stale snapshot that merely bumps ``revision`` while the measured
#: binary/plugin/config is unchanged must not read as a fresh measurement. The
#: binding is reconciled from *observations*, so the revision carries no weight.
CAPABILITY_SNAPSHOT_REVISION_IS_ADVISORY = True

#: The tools an Understand-style workflow needs of a bare child worker: skill
#: expansion plus agent spawning. Required-tool admission is checked against the
#: *child worker* configuration, never the coordinator's.
WORKFLOW_STAGE_TOOLS: tuple[str, ...] = ("Agent", "Skill")

#: Evidence keys inside ``capabilities.run_attested`` that this gate treats as
#: *weak* facts. Installation, /v1/models visibility and system/init events
#: prove a surface exists; they do not prove a skill expanded or a stage ran.
WEAK_RUN_ATTESTED_KEYS: tuple[str, ...] = (
    "installed",
    "installation",
    "models_visible",
    "model_catalog",
    "system_init",
    "init",
)

#: Evidence keys that carry *bound* native expansion / workflow-stage work: an
#: observation produced by the measuring boundary that names the skill digest
#: and the selected runtime configuration it was observed under. A stdout line
#: merely *claiming* a stage happened is not this.
BOUND_STAGE_KEYS = ("native_expansion", "workflow_stages")
_BOUND_STAGE_REQUIRED_KEYS = ("skill_digest", "runtime_config_digest", "observer")


def _as_mapping(value: Any) -> dict[str, Any]:
    """Coerce a mapping-like to a plain dict, or ``{}`` for anything else."""
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_instant(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 instant into an aware UTC datetime, or ``None``.

    A naive timestamp is treated as UTC; an unparseable or absent one fails
    closed (``None``) rather than defaulting to "now".
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _shape_digest(value: Any) -> Optional[str]:
    """A stable digest of a JSON-shaped value, or ``None`` if not hashable.

    Used to compare the *configuration* a binding pinned at plan time against
    what the host currently observes. A non-JSON value fails closed.
    """
    try:
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_digest(path: Any) -> Optional[str]:
    """The sha256 of a file's bytes, or ``None`` when it is absent/unreadable."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None


def _declared_tools(snapshot: Mapping[str, Any]) -> set[str]:
    caps = _as_mapping(_as_mapping(snapshot.get("capabilities")).get("declared"))
    tools = caps.get("tools")
    return {str(t) for t in tools} if isinstance(tools, (list, tuple, set)) else set()


def _detected_coordinator_tools(snapshot: Mapping[str, Any]) -> set[str]:
    detected = _as_mapping(_as_mapping(snapshot.get("capabilities")).get("detected"))
    tools = detected.get("coordinator_tools")
    return {str(t) for t in tools} if isinstance(tools, (list, tuple, set)) else set()


def _detected_child_tools(snapshot: Mapping[str, Any]) -> set[str]:
    detected = _as_mapping(_as_mapping(snapshot.get("capabilities")).get("detected"))
    tools = detected.get("child_worker_tools")
    return {str(t) for t in tools} if isinstance(tools, (list, tuple, set)) else set()


def _snapshot_host_binding(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return _as_mapping(_as_mapping(snapshot.get("capabilities")).get("host_binding"))


def _run_attested(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return _as_mapping(_as_mapping(snapshot.get("capabilities")).get("run_attested"))


class NativeRequirements:
    """What a native-tool plan requires of the host: skill, tools and stages.

    ``skill_name`` / ``skill_digest`` pin the installed skill the plan expands.
    ``required_tools`` are the native tools (e.g. ``Agent``/``Skill``) the
    selected worker mode must actually hold. ``required_stages`` are the
    workflow stages for which *bound* run-attested evidence must exist. The
    object is validated at construction so a caller cannot state an empty
    requirement that would trivially pass.
    """

    __slots__ = ("skill_name", "skill_digest", "required_tools", "required_stages")

    def __init__(
        self,
        *,
        skill_name: str,
        skill_digest: str,
        required_tools: Iterable[str] = (),
        required_stages: Iterable[str] = (),
    ) -> None:
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise HarnessAdapterError(
                "native requirements must name the skill being expanded",
                asset="native_requirements.skill_name",
            )
        if not isinstance(skill_digest, str) or not skill_digest.strip():
            raise HarnessAdapterError(
                "native requirements must pin the skill digest",
                asset="native_requirements.skill_digest",
            )
        tools = tuple(str(t) for t in required_tools)
        stages = tuple(str(s) for s in required_stages)
        if not tools and not stages and not skill_digest:
            raise HarnessAdapterError(
                "native requirements must require at least one tool or stage",
                asset="native_requirements",
            )
        self.skill_name = skill_name
        self.skill_digest = skill_digest
        self.required_tools = tools
        self.required_stages = stages

    @classmethod
    def from_mapping(cls, data: Any) -> "NativeRequirements":
        """Build from a mapping (``skill_name``/``skill_digest``/... keys)."""
        if not isinstance(data, Mapping):
            raise HarnessAdapterError(
                "native requirements must be a mapping",
                asset="native_requirements",
            )
        return cls(
            skill_name=str(data.get("skill_name", "")),
            skill_digest=str(data.get("skill_digest", "")),
            required_tools=data.get("required_tools", ()) or (),
            required_stages=data.get("required_stages", ()) or (),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_digest": self.skill_digest,
            "required_tools": list(self.required_tools),
            "required_stages": list(self.required_stages),
        }


class HostBinding:
    """The host's *detected* configuration that a native plan binds to.

    Every field is a real observation of the host: the executable's path and
    digest, its version, the plugin's path and digest, the coordinator/child
    mode and effective tools, the exact model, and a digest of the selected
    runtime configuration. ``to_dict`` is the canonical binding record the
    snapshot's ``capabilities.host_binding`` is reconciled against; the field
    set is the contract between the two.
    """

    __slots__ = (
        "binary_path",
        "binary_sha256",
        "binary_version",
        "plugin_path",
        "plugin_sha256",
        "mode",
        "effective_tools",
        "model",
        "coordinator_config",
        "child_worker_config",
        "config_digest",
        "measured_at",
    )

    def __init__(
        self,
        *,
        binary_path: str = "",
        binary_sha256: str = "",
        binary_version: str = "",
        plugin_path: str = "",
        plugin_sha256: str = "",
        mode: str = "",
        effective_tools: Iterable[str] = (),
        model: str = "",
        coordinator_config: Any = None,
        child_worker_config: Any = None,
        config_digest: str = "",
    ) -> None:
        self.binary_path = str(binary_path or "")
        self.binary_sha256 = str(binary_sha256 or "")
        self.binary_version = str(binary_version or "")
        self.plugin_path = str(plugin_path or "")
        self.plugin_sha256 = str(plugin_sha256 or "")
        self.mode = str(mode or "")
        self.effective_tools = tuple(str(t) for t in effective_tools)
        self.model = str(model or "")
        self.coordinator_config = (
            _as_mapping(coordinator_config)
            if isinstance(coordinator_config, Mapping)
            else coordinator_config
        )
        self.child_worker_config = (
            _as_mapping(child_worker_config)
            if isinstance(child_worker_config, Mapping)
            else child_worker_config
        )
        if config_digest:
            self.config_digest = str(config_digest)
        else:
            self.config_digest = (
                _shape_digest(
                    [self.coordinator_config, self.child_worker_config, self.mode]
                )
                or ""
            )
        self.measured_at: Optional[datetime] = None

    @classmethod
    def from_mapping(cls, data: Any) -> "HostBinding":
        if not isinstance(data, Mapping):
            raise HarnessAdapterError(
                "host binding must be a mapping",
                asset="host_binding",
            )
        return cls(
            binary_path=data.get("binary_path", ""),
            binary_sha256=data.get("binary_sha256", ""),
            binary_version=data.get("binary_version", ""),
            plugin_path=data.get("plugin_path", ""),
            plugin_sha256=data.get("plugin_sha256", ""),
            mode=data.get("mode", ""),
            effective_tools=data.get("effective_tools", ()) or (),
            model=data.get("model", ""),
            coordinator_config=data.get("coordinator_config"),
            child_worker_config=data.get("child_worker_config"),
            config_digest=data.get("config_digest", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "binary_path": self.binary_path,
            "binary_sha256": self.binary_sha256,
            "binary_version": self.binary_version,
            "plugin_path": self.plugin_path,
            "plugin_sha256": self.plugin_sha256,
            "mode": self.mode,
            "effective_tools": list(self.effective_tools),
            "model": self.model,
            "coordinator_config": self.coordinator_config,
            "child_worker_config": self.child_worker_config,
            "config_digest": self.config_digest,
        }

    def remeasured(self) -> "HostBinding":
        """Return a copy flagged as measured against the host *now*.

        The copy is re-observed against reality by the admission gate (which
        reads the binary/plugin bytes and the current configuration directly),
        so a binary/plugin/config replaced after planning is caught. The origin
        object stays byte-identical for the caller that pinned it.
        """
        measured = HostBinding(
            binary_path=self.binary_path,
            binary_sha256=self.binary_sha256,
            binary_version=self.binary_version,
            plugin_path=self.plugin_path,
            plugin_sha256=self.plugin_sha256,
            mode=self.mode,
            effective_tools=self.effective_tools,
            model=self.model,
            coordinator_config=self.coordinator_config,
            child_worker_config=self.child_worker_config,
            config_digest=self.config_digest,
        )
        object.__setattr__(measured, "measured_at", datetime.now(timezone.utc))
        return measured


def _binding_from_config(config: Any, *, key: str) -> dict[str, Any]:
    """Read the coordinator/child configuration record out of the host binding.

    The host binding carries ``coordinator_config`` and ``child_worker_config``
    as objects with at least ``mode`` and ``tools``. Anything else fails closed
    (treated as absent) rather than guessed.
    """
    record = _as_mapping(config)
    return record


class CapabilityAdmission:
    """The native-tool admission decision, recorded for audit.

    ``admitted`` is the gate's verdict; ``reason``/``asset`` name the fact that
    decided it, so a refusal is attributable. ``observed`` holds the reconciled
    facts (declared/detected distinctions, weak evidence retained, bound stage
    proof) so a later audit can see exactly what was consumed — and what was
    *kept visible* rather than silently discarded.
    """

    __slots__ = ("admitted", "reason", "asset", "observed")

    def __init__(
        self,
        *,
        admitted: bool,
        reason: str,
        asset: Optional[str] = None,
        observed: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.admitted = admitted
        self.reason = reason
        self.asset = asset
        self.observed = dict(observed or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "asset": self.asset,
            "observed": dict(self.observed),
        }


def _looks_like_digest(value: Any) -> bool:
    """Whether ``value`` is a hex digest string (not an arbitrary label)."""
    if not isinstance(value, str) or len(value) < 32:
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in value)


def _bound_stage_present(
    run_attested: Mapping[str, Any],
    stage: str,
    *,
    skill_digest: str,
    runtime_config_digest: str,
) -> tuple[bool, Optional[str]]:
    """Whether *bound* evidence exists for workflow ``stage``.

    Bound evidence lives under ``run_attested['workflow_stages']`` (or
    ``run_attested['native_expansion']`` for the expansion stage) as a record
    that binds, with *typed* fields:

    - the ``skill_digest`` (a hex digest) the observation expanded, equal to
      the plan's required skill digest;
    - the selected ``runtime_config_digest`` (a hex digest) the observation ran
      under, equal to the *actual* selected configuration digest — a proof that
      merely asserts a stage happened under some other runtime is not evidence
      for this run; and
    - a non-empty ``observer`` identity string.

    A bare string claiming a stage happened, a ``system_init`` event, an
    ``installed``/``models_visible`` flag, or a label that is not shaped like a
    digest is *not* bound evidence. Returns ``(present, reason_or_none)``.
    """
    stages = _as_mapping(run_attested.get("workflow_stages"))
    proof = stages.get(stage)
    if proof is None and stage in ("skill-expanded", "expand", "expansion"):
        proof = run_attested.get("native_expansion")
    if not isinstance(proof, Mapping):
        return False, f"stage {stage!r} has no bound observation"
    for required in _BOUND_STAGE_REQUIRED_KEYS:
        value = proof.get(required)
        if not isinstance(value, str) or not value.strip():
            return False, (
                f"stage {stage!r} observation lacks bound {required!r}"
            )
    if not _looks_like_digest(proof.get("skill_digest")):
        return False, (
            f"stage {stage!r} skill_digest {proof.get('skill_digest')!r} is not "
            "a digest"
        )
    if not _looks_like_digest(proof.get("runtime_config_digest")):
        return False, (
            f"stage {stage!r} runtime_config_digest "
            f"{proof.get('runtime_config_digest')!r} is not a digest"
        )
    if str(proof.get("skill_digest")) != skill_digest:
        return False, (
            f"stage {stage!r} binds skill digest {proof.get('skill_digest')!r}, "
            f"not the required {skill_digest!r}"
        )
    if not _looks_like_digest(runtime_config_digest):
        return False, "no selected runtime configuration digest to bind against"
    if str(proof.get("runtime_config_digest")) != runtime_config_digest:
        # The observation ran under a *different* runtime configuration than the
        # one this plan selected: the proof does not bind this run.
        return False, (
            f"stage {stage!r} binds runtime config "
            f"{proof.get('runtime_config_digest')!r}, not the selected "
            f"{runtime_config_digest!r}"
        )
    return True, None


def _reconcile_host_binding(
    *,
    binding: HostBinding,
    snapshot: Mapping[str, Any],
    native: NativeRequirements,
) -> tuple[Optional[str], dict[str, Any]]:
    """Reconcile the detected host binding against the snapshot and reality.

    Returns ``(refusal_asset, observed)``; ``refusal_asset`` is ``None`` when
    the binding reconciles. This is where a binary replaced after planning, a
    plugin mismatch, a mode/tools/provider mismatch, or a self-asserted binding
    (present only inside the snapshot, not observed on the host) fails closed.
    """
    observed: dict[str, Any] = {}

    pinned = binding.to_dict()
    snap_binding = _snapshot_host_binding(snapshot)
    observed["pinned_binding"] = pinned
    observed["snapshot_binding"] = snap_binding

    # A binding must be *observed on the host*, not merely asserted inside the
    # snapshot. If the snapshot's binding is absent, refuses here.
    if not snap_binding:
        return "host_binding", observed

    # The snapshot's binding describes the host the measurement was taken on;
    # the live binding must match it field-for-field. A snapshot that disagrees
    # with what is observed now is stale/contradictory and fails closed.
    for field in ("binary_path", "plugin_path", "binary_version", "mode", "model"):
        if str(snap_binding.get(field, "")) != str(pinned.get(field, "")):
            observed["mismatch"] = field
            return f"host_binding.{field}", observed

    # Binary digest: both the snapshot's pin AND the real file bytes must agree.
    live_digest = _file_digest(binding.binary_path)
    observed["live_binary_sha256"] = live_digest
    if live_digest is None:
        return "host_binding.binary_path", observed
    for source in ("binary_sha256", "plugin_sha256"):
        snap_value = str(snap_binding.get(source, ""))
        pinned_value = str(pinned.get(source, ""))
        if snap_value and pinned_value and snap_value != pinned_value:
            observed["mismatch"] = source
            return f"host_binding.{source}", observed
    if pinned.get("binary_sha256") and pinned["binary_sha256"] != live_digest:
        # The binary was replaced AFTER planning: the pinned handshake no longer
        # describes the executable about to run.
        observed["mismatch"] = "binary_sha256_replaced"
        return "host_binding.binary_sha256", observed
    if pinned.get("plugin_path"):
        live_plugin = _file_digest(pinned["plugin_path"])
        observed["live_plugin_sha256"] = live_plugin
        if pinned.get("plugin_sha256") and live_plugin != pinned["plugin_sha256"]:
            observed["mismatch"] = "plugin_sha256_replaced"
            return "host_binding.plugin_sha256", observed

    # Configuration: the selected coordinator/child configuration the measurement
    # was taken on must be the same shape the plan will run on now. When both
    # sides carry a digest they must agree, and when they carry the configuration
    # itself the contents must be equal — a mutated config is a contradiction and
    # fails closed rather than silently re-admitting.
    snap_config_digest = str(snap_binding.get("config_digest", "") or "")
    pinned_config_digest = str(pinned.get("config_digest", "") or "")
    observed["config_digest"] = pinned_config_digest
    observed["snapshot_config_digest"] = snap_config_digest
    if snap_config_digest and pinned_config_digest and snap_config_digest != pinned_config_digest:
        observed["mismatch"] = "config_digest"
        return "host_binding.config_digest", observed
    snap_child_digest = _shape_digest(
        _binding_from_config(snap_binding.get("child_worker_config"), key="child")
    )
    live_child_digest = _shape_digest(
        _binding_from_config(pinned.get("child_worker_config"), key="child")
    )
    if snap_child_digest is None or live_child_digest is None:
        # An unserialisable configuration cannot be reconciled: fail closed
        # rather than compare two ``None`` digests and let a mutation pass.
        observed["mismatch"] = "child_worker_config.unhashable"
        return "host_binding.child_worker_config", observed
    if snap_child_digest != live_child_digest:
        observed["mismatch"] = "child_worker_config"
        return "host_binding.child_worker_config", observed
    snap_coord_digest = _shape_digest(
        _binding_from_config(snap_binding.get("coordinator_config"), key="coord")
    )
    live_coord_digest = _shape_digest(
        _binding_from_config(pinned.get("coordinator_config"), key="coord")
    )
    if snap_coord_digest is None or live_coord_digest is None:
        observed["mismatch"] = "coordinator_config.unhashable"
        return "host_binding.coordinator_config", observed
    if snap_coord_digest != live_coord_digest:
        observed["mismatch"] = "coordinator_config"
        return "host_binding.coordinator_config", observed

    # The child-worker configuration is the surface a native plan actually
    # runs on. Its mode must be a worker mode and its tools must hold every
    # required tool — a coordinator's tools never stand in for the child's.
    child = _binding_from_config(pinned.get("child_worker_config"), key="child")
    coordinator = _binding_from_config(pinned.get("coordinator_config"), key="coord")
    observed["child_worker_config"] = child
    # The coordinator's surface is kept visible in every refusal, so an audit
    # can see that a coordinator could hold a tool the child does not.
    coordinator_tools = _tool_set(coordinator.get("tools"))
    observed["coordinator_tools"] = sorted(coordinator_tools)
    child_mode = str(child.get("mode", ""))
    observed["child_mode"] = child_mode
    if not child_mode.strip():
        return "host_binding.child_worker_config.mode", observed
    # The mode name alone proves nothing — the effective tool list is the
    # evidence, checked below.
    child_tools = _tool_set(child.get("tools"))
    observed["child_tools"] = sorted(child_tools)
    declared = _declared_tools(snapshot)
    detected_child = _detected_child_tools(snapshot)
    observed["declared_child_tools"] = sorted(declared)
    observed["detected_child_tools"] = sorted(detected_child)
    required = {str(t) for t in native.required_tools} | set(WORKFLOW_STAGE_TOOLS)
    missing = sorted(required - child_tools)
    if missing:
        observed["missing_child_tools"] = missing
        return f"host_binding.child_worker_config.tools:{missing[0]}", observed

    # The coordinator cannot supply a tool the child lacks: coordinator support
    # is not child support. (Sufficient native expansion needs a bound
    # observation, never a declaration or a coordinator init list — checked by
    # the caller.)
    required_native = {str(t) for t in native.required_tools} - child_tools
    if required_native and required_native <= coordinator_tools:
        return "host_binding.child_worker_config.tools", observed

    return None, observed


def _tool_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(t) for t in value}
    return set()


def _snapshot_temporal(snapshot: Mapping[str, Any]) -> tuple[Optional[str], dict[str, Any]]:
    """Validate the snapshot's validity window; return ``(asset, observed)``."""
    observed: dict[str, Any] = {}
    measured_at = _parse_instant(snapshot.get("measured_at"))
    expires_at = _parse_instant(snapshot.get("expires_at"))
    observed["measured_at"] = snapshot.get("measured_at")
    observed["expires_at"] = snapshot.get("expires_at")
    if measured_at is None:
        return "measured_at", observed
    if expires_at is None:
        return "expires_at", observed
    now = datetime.now(timezone.utc)
    if measured_at > now + timedelta(seconds=60):
        # A snapshot measured in the future is not a measurement.
        return "measured_at.future", observed
    if expires_at <= now:
        return "expires_at.expired", observed
    if expires_at <= measured_at:
        return "expires_at", observed
    return None, observed


def admit_native_plan(
    plan: HarnessPlan,
    *,
    capability_snapshot: Any = None,
    native_requirements: Any = None,
    host_binding: Any = None,
    measured_at: Optional[datetime] = None,
) -> CapabilityAdmission:
    """Admit (or refuse) a native-tool plan against a measured handshake.

    Called at the native-tool execution boundary *before* ``build_command`` /
    launch. When the plan requires :data:`NATIVE_TOOL_CAPABILITY`, all three of
    ``capability_snapshot``, ``native_requirements`` and ``host_binding`` must
    be present or the call refuses with :class:`HarnessAdapterError` (never a
    quiet legacy fallback). The refusal is returned as an unadmitted
    :class:`CapabilityAdmission`, and the caller raises it — so the refusal
    happens with zero launch work.

    Non-native plans return an admission with ``admitted=True`` and a reason
    stating raw-command compatibility; the caller must not read that as native
    skill execution.
    """
    required = set(plan.required_capabilities)
    if NATIVE_TOOL_CAPABILITY not in required:
        return CapabilityAdmission(
            admitted=True,
            reason="plan does not require native-tool; raw-command path",
            observed={"required_capabilities": sorted(required)},
        )

    # ── Presence: all three inputs are mandatory for a native plan ──────────
    missing = [
        name
        for name, value in (
            ("capability_snapshot", capability_snapshot),
            ("native_requirements", native_requirements),
            ("host_binding", host_binding),
        )
        if value is None
    ]
    if missing:
        return CapabilityAdmission(
            admitted=False,
            reason=(
                "native-tool plan requires a measured handshake; missing "
                + ", ".join(missing)
            ),
            asset=missing[0],
        )

    if not isinstance(capability_snapshot, Mapping):
        return CapabilityAdmission(
            admitted=False,
            reason="capability snapshot must be a mapping",
            asset="capability_snapshot",
        )
    snapshot = capability_snapshot
    native = (
        native_requirements
        if isinstance(native_requirements, NativeRequirements)
        else NativeRequirements.from_mapping(native_requirements)
    )
    binding = (
        host_binding
        if isinstance(host_binding, HostBinding)
        else HostBinding.from_mapping(host_binding)
    )
    observed: dict[str, Any] = {"native_requirements": native.to_dict()}

    # ── Schema version: consume the frozen v1, refuse anything else ─────────
    version = snapshot.get("schema_version")
    if version != CAPABILITY_SNAPSHOT_SCHEMA_VERSION:
        return CapabilityAdmission(
            admitted=False,
            reason=(
                f"unsupported capability snapshot version {version!r}; expected "
                f"{CAPABILITY_SNAPSHOT_SCHEMA_VERSION!r}"
            ),
            asset="schema_version",
            observed=observed,
        )

    # ── Temporal validity: missing/future/expired fails closed ──────────────
    temporal_asset, temporal_observed = _snapshot_temporal(snapshot)
    observed.update(temporal_observed)
    if temporal_asset is not None:
        return CapabilityAdmission(
            admitted=False,
            reason=f"capability snapshot is not valid now ({temporal_asset})",
            asset=temporal_asset,
            observed=observed,
        )

    observed["revision_advisory"] = CAPABILITY_SNAPSHOT_REVISION_IS_ADVISORY

    # ── Subject and issuer: about this host, from a measuring issuer ────────
    # The snapshot must be *about the subject this plan is for* and carry a
    # non-empty issuer. The issuer is not cryptographic authentication (that is
    # the separate IDENTITY gate) but an absent/whitespace issuer is not a
    # measurement and a snapshot about a different host is not this plan's.
    subject = snapshot.get("subject")
    observed["subject"] = subject
    if not isinstance(subject, str) or not subject.strip():
        return CapabilityAdmission(
            admitted=False,
            reason="capability snapshot has no subject",
            asset="subject",
            observed=observed,
        )
    if subject != plan.adapter:
        return CapabilityAdmission(
            admitted=False,
            reason=(
                f"capability snapshot subject {subject!r} is not this plan's "
                f"subject {plan.adapter!r}"
            ),
            asset="subject",
            observed=observed,
        )
    issuer = snapshot.get("issuer")
    observed["issuer"] = issuer
    if not isinstance(issuer, str) or not issuer.strip():
        return CapabilityAdmission(
            admitted=False,
            reason="capability snapshot has no issuer",
            asset="issuer",
            observed=observed,
        )

    # ── The measurement is not a model claim ────────────────────────────────
    # `authenticated=true` is self-reported and is never consulted as a fact;
    # the measuring boundary's `observer` plus real host observations carry the
    # weight. The flag is retained in `observed` only to keep it visible.
    attested_flag = _as_mapping(_as_mapping(snapshot.get("capabilities")).get("detected"))
    observed["authenticated_flag_seen"] = (
        attested_flag.get("authenticated") is True
        or _as_mapping(snapshot).get("authenticated") is True
    )

    # ── Host binding reconciliation (detected, re-measured at admission) ─────
    refusal_asset, binding_observed = _reconcile_host_binding(
        binding=binding, snapshot=snapshot, native=native
    )
    observed.update(binding_observed)
    if refusal_asset is not None:
        return CapabilityAdmission(
            admitted=False,
            reason=f"host binding does not reconcile: {refusal_asset}",
            asset=refusal_asset,
            observed=observed,
        )

    # ── Required tools: the *measured* CHILD worker must hold them ──────────
    # The config a binding *claims* is not the measurement. If the snapshot
    # carries a detected child-tool observation it is authoritative and must
    # hold every required tool; a config that merely asserts Agent/Skill while
    # the detected child worker holds only Read/Bash cannot admit the plan.
    child_tools = _tool_set(
        _as_mapping(binding.child_worker_config).get("tools")
    )
    detected_child_tools = _detected_child_tools(snapshot)
    observed["detected_child_tools"] = sorted(detected_child_tools)
    required_tools = {str(t) for t in native.required_tools} | set(WORKFLOW_STAGE_TOOLS)
    absent = sorted(t for t in required_tools if t not in child_tools)
    measured_absent = sorted(t for t in required_tools if t not in detected_child_tools)
    if absent or measured_absent:
        blockers = absent or measured_absent
        # Declared tools (or a coordinator's list) never satisfy this, and a
        # configuration claim cannot override the measured child tools.
        return CapabilityAdmission(
            admitted=False,
            reason=(
                "selected child worker mode lacks required native tool(s) "
                f"{blockers}; declared/coordinator/config claims cannot satisfy "
                "them"
            ),
            asset=f"native_requirements.required_tools:{blockers[0]}",
            observed=observed,
        )

    # ── Run-attested proof: bound expansion + each required stage ───────────
    run_attested = _run_attested(snapshot)
    observed["weak_evidence_present"] = sorted(
        key for key in WEAK_RUN_ATTESTED_KEYS if key in run_attested
    )
    observed["evidence_level"] = snapshot.get("evidence_level")
    # The *actual* selected configuration digest the proofs must bind. Prefer an
    # explicit snapshot binding digest, then the live binding's digest (computed
    # from the configuration shape when not stated).
    selected_config_digest = str(
        _snapshot_host_binding(snapshot).get("config_digest", "")
        or binding.config_digest
        or ""
    )
    observed["selected_config_digest"] = selected_config_digest

    expansion_ok, expansion_reason = _bound_stage_present(
        run_attested,
        "skill-expanded",
        skill_digest=native.skill_digest,
        runtime_config_digest=selected_config_digest,
    )
    if not expansion_ok:
        return CapabilityAdmission(
            admitted=False,
            reason=(
                "no bound native skill-expansion evidence: "
                + str(expansion_reason)
            ),
            asset="capabilities.run_attested.native_expansion",
            observed=observed,
        )

    for stage in native.required_stages:
        if stage in ("skill-expanded", "expand", "expansion"):
            continue
        stage_ok, stage_reason = _bound_stage_present(
            run_attested,
            str(stage),
            skill_digest=native.skill_digest,
            runtime_config_digest=selected_config_digest,
        )
        if not stage_ok:
            return CapabilityAdmission(
                admitted=False,
                reason=f"required stage not bound: {stage_reason}",
                asset=f"capabilities.run_attested.workflow_stages.{stage}",
                observed=observed,
            )

    observed["admitted_skill_digest"] = native.skill_digest
    return CapabilityAdmission(
        admitted=True,
        reason=(
            "native-tool plan admitted: measured handshake reconciles, child "
            "worker holds required tools, bound expansion and stages observed"
        ),
        observed=observed,
    )


# ── The plugin registry ────────────────────────────────────────────────────

class HarnessAdapterRegistry:
    """A name-and-capability index of adapter plugins.

    Wiring code registers plugins; consumers look them up by the plugin's own
    declared name or by the capabilities they must hold. The registry never
    names a plugin itself — a duplicate name is refused, an unknown name is
    refused by name, and capability lookup reads the plugins' own declarations.
    """

    def __init__(self, adapters: Iterable[HarnessAdapter] = ()) -> None:
        self._by_name: dict[str, HarnessAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: HarnessAdapter) -> None:
        if not isinstance(adapter, HarnessAdapter):
            raise HarnessAdapterError(
                "only a HarnessAdapter plugin can be registered",
                asset="adapter",
            )
        name = adapter.name
        if not isinstance(name, str) or not name.strip():
            raise HarnessAdapterError(
                "a plugin must declare a non-empty name",
                asset="name",
            )
        if name in self._by_name:
            raise HarnessAdapterError(
                f"adapter '{name}' is already registered",
                asset=name,
            )
        self._by_name[name] = adapter

    def get(self, name: str) -> HarnessAdapter:
        adapter = self._by_name.get(name)
        if adapter is None:
            raise HarnessAdapterError(
                f"no adapter plugin registered as '{name}'",
                asset=name,
            )
        return adapter

    def names(self) -> list[str]:
        return list(self._by_name)

    def capable_of(self, *capabilities: str) -> list[str]:
        """Names of registered plugins holding every named capability."""
        return [
            name
            for name, adapter in self._by_name.items()
            if all(adapter.supports(cap) for cap in capabilities)
        ]

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


def register_plugins(
    registry: HarnessAdapterRegistry, *adapters: HarnessAdapter
) -> HarnessAdapterRegistry:
    """Register ``adapters`` on ``registry`` and return it (composition seam).

    A convenience for the wiring step: the registry stays the index, and no
    generic core code needs to know which plugins exist.
    """
    for adapter in adapters:
        registry.register(adapter)
    return registry


# ── A plugin: the OpenClaude harness ───────────────────────────────────────

class OpenClaudeAdapter(HarnessAdapter):
    """The OpenClaude harness, implemented strictly as a plugin (SW-158).

    Everything OpenClaude-specific about this class is *here*, in the plugin:
    the identity it reports, the capabilities it declares, and the transport
    argv it builds. The generic interface above it is untouched — a second
    plugin with a different name, a different capability set, and a different
    argv is registered and driven identically, with no change to any core code.

    ``capabilities`` and ``executable`` are injectable so a caller can declare
    the plugin's real, machine-observed surface without editing the class.
    """

    #: The plugin's own identity. Opaque to the generic core: the interface
    #: treats it as data, so nothing in the core branches on it.
    NAME = "openclaude"

    #: The capabilities this plugin holds, declared as data. Absent from this
    #: set means the plugin does not claim it, and a decision that needs it is
    #: refused by :meth:`HarnessAdapter.plan`.
    DEFAULT_CAPABILITIES: frozenset[str] = frozenset(
        {
            "native-tool",
            "external-process",
            "stdin",
            "status",
            "cancel",
            "state-namespace",
            "installed-skill-digest",
        }
    )

    def __init__(
        self,
        executable: str = "openclaude",
        capabilities: Optional[Iterable[str]] = None,
    ) -> None:
        if not isinstance(executable, str) or not executable.strip():
            raise HarnessAdapterError(
                "the OpenClaude plugin needs a non-empty executable",
                asset="executable",
            )
        self._executable = executable.strip()
        declared = (
            self.DEFAULT_CAPABILITIES
            if capabilities is None
            else frozenset(capabilities)
        )
        for cap in declared:
            if cap not in HARNESS_CAPABILITIES:
                raise HarnessAdapterError(
                    f"unknown capability '{cap}' "
                    f"(expected one of {list(HARNESS_CAPABILITIES)})",
                    asset=cap,
                )
        self._capabilities = frozenset(declared)

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def executable(self) -> str:
        return self._executable

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def build_command(self, plan: HarnessPlan) -> Sequence[str]:
        """The plugin's transport argv: its executable, and the router's model.

        The model is the router's primary cast member from
        ``plan.strategy.model_mix`` — read from the decision, never chosen here.
        """
        command = [self._executable]
        if plan.strategy.model_mix:
            command += ["--model", plan.strategy.model_mix[0]]
        return command


__all__ = [
    "HARNESS_CAPABILITIES",
    "NATIVE_TOOL_CAPABILITY",
    "CAPABILITY_SNAPSHOT_SCHEMA_VERSION",
    "CAPABILITY_SNAPSHOT_REVISION_IS_ADVISORY",
    "WORKFLOW_STAGE_TOOLS",
    "WEAK_RUN_ATTESTED_KEYS",
    "BOUND_STAGE_KEYS",
    "STRATEGY_INLINE",
    "STRATEGY_SINGLE",
    "STRATEGY_PARALLEL",
    "STRATEGY_CAPABILITIES",
    "HarnessAdapterError",
    "tier_for_complexity",
    "CapabilityRequirement",
    "AdapterRequest",
    "DispatchStrategy",
    "RouterSeam",
    "FaigateRouter",
    "HarnessPlan",
    "HarnessOutcome",
    "HarnessAdapter",
    "NativeRequirements",
    "HostBinding",
    "CapabilityAdmission",
    "admit_native_plan",
    "HarnessAdapterRegistry",
    "register_plugins",
    "OpenClaudeAdapter",
]
