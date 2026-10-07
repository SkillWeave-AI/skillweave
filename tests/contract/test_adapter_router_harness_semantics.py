"""Router/harness decision & admission regression (SW-ADAPT-001, criterion 8).

The adapter facade is additive: it must not disturb the *existing* router and
harness-adapter decision/admission semantics. This module is a focused
regression guard, written against the public ``skillweave.routing`` seam, that
pins the four behaviours the addendum requires to stay intact:

* the exact :class:`AdapterRequest` (capabilities + complexity) reaches the
  router unreconstructed — the adapter composes the port, it does not rewrite
  the decision;
* swapping only the router changes the decision while the adapter is untouched,
  and the adapter takes the returned :class:`DispatchStrategy` verbatim;
* a native-tool plan with no *measured* handshake is refused **before any
  argv is built or any launch happens**, so a self-asserted capability can never
  read as a measurement; and
* on every refusal the injected launch seam is never entered — a protected
  foreign process is never touched by a refused plan.

Scope note: this exercises the Core routing seam directly. The lifecycle
``ProviderRegistry`` / ``search-provider/v1`` / ``model-provider/v1`` contracts
are left intact and are covered by their own suites.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.routing import (  # noqa: E402
    STRATEGY_INLINE,
    STRATEGY_PARALLEL,
    STRATEGY_SINGLE,
    AdapterRequest,
    CapabilityRequirement,
    DispatchStrategy,
    FaigateRouter,
    HarnessAdapter,
    HarnessAdapterError,
    HarnessPlan,
    RouterSeam,
)
from skillweave.routing import harness_adapter as HA  # noqa: E402


class _StubResult:
    def __init__(self, succeeded: bool) -> None:
        self.succeeded = succeeded


class _RecordingRouter(RouterSeam):
    """A router that records the request object it was handed."""

    name = "recording"

    def __init__(self, strategy: DispatchStrategy) -> None:
        self._strategy = strategy
        self.seen: list[AdapterRequest] = []

    def route(self, request: AdapterRequest) -> DispatchStrategy:
        self.seen.append(request)
        return self._strategy


class _Plugin(HarnessAdapter):
    """A minimal generic plugin declared entirely as data."""

    def __init__(self, name: str, capabilities: Sequence[str]) -> None:
        self._name = name
        self._capabilities = frozenset(capabilities)

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def build_command(self, plan: HarnessPlan) -> Sequence[str]:
        return [self._name, *plan.strategy.model_mix]


def _inline(models: Sequence[str], router: str = "recording") -> DispatchStrategy:
    return DispatchStrategy(
        strategy=STRATEGY_INLINE,
        model_mix=tuple(models),
        mode="inline",
        router=router,
    )


# ── the request reaches the router unseen ──────────────────────────────────


def test_request_object_reaches_the_router_unreconstructed():
    strategy = _inline(("local-echo",))
    router = _RecordingRouter(strategy)
    plugin = _Plugin("echo", ("in-place",))
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(complexity=0, capabilities=("in-place",)),
    )
    plugin.plan(request, router)
    assert len(router.seen) == 1
    assert router.seen[0] is request, "the exact request must reach the router"
    assert router.seen[0].requirement is request.requirement


def test_adapter_takes_the_router_decision_verbatim():
    strategy = _inline(("model-a",))
    plugin = _Plugin("echo", ("in-place",))
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(complexity=0, capabilities=("in-place",)),
    )
    plan = plugin.plan(request, _RecordingRouter(strategy))
    assert plan.strategy is strategy, "the decision must be taken verbatim"
    assert plan.required_capabilities == ("in-place",)


def test_swapping_only_the_router_changes_the_decision():
    plugin = _Plugin("echo", ("in-place",))
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(complexity=0, capabilities=("in-place",)),
    )
    small = plugin.plan(request, _RecordingRouter(_inline(("m1",))))
    big = plugin.plan(request, _RecordingRouter(_inline(("m1", "m2", "m3"))))
    # Same adapter, same request; only the router differs.
    assert small.strategy.model_mix == ("m1",)
    assert big.strategy.model_mix == ("m1", "m2", "m3")


# ── a decision the adapter cannot honour is refused by name ────────────────


def test_an_unhonourable_decision_is_refused_by_name():
    # parallel requires external-process + status; this plugin holds only
    # in-place, so plan() must refuse naming the missing capability.
    plugin = _Plugin("echo", ("in-place",))
    parallel = DispatchStrategy(
        strategy=STRATEGY_PARALLEL,
        model_mix=("m1", "m2"),
        mode="full",
        router="recording",
    )
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(complexity=0),
    )
    with pytest.raises(HarnessAdapterError) as exc:
        plugin.plan(request, _RecordingRouter(parallel))
    assert exc.value.asset in ("external-process", "status")


def test_unknown_requirement_capability_falls_closed_at_construction():
    with pytest.raises(HarnessAdapterError):
        CapabilityRequirement(complexity=0, capabilities=("not-a-capability",))


# ── the router casts a tier, never an invented model ───────────────────────


def test_faigate_casts_tier_from_complexity_without_inventing_a_model():
    presets = {
        "quick": {"models": ["q1"]},
        "default": {"models": ["d1", "d2"]},
        "deep": {"models": ["z1", "z2", "z3"]},
    }
    router = FaigateRouter(presets=presets)
    known = {m for preset in presets.values() for m in preset["models"]}

    fast = router.route(_req(complexity=0))
    balanced = router.route(_req(complexity=1))
    deep = router.route(_req(complexity=9))

    assert fast.model_mix == ("q1",) and fast.strategy == STRATEGY_SINGLE
    assert balanced.model_mix == ("d1", "d2") and balanced.strategy == STRATEGY_PARALLEL
    assert deep.model_mix == ("z1", "z2", "z3")
    # No model is invented: every cast member comes from the preset table.
    for decision in (fast, balanced, deep):
        assert set(decision.model_mix) <= known


def _req(complexity: Any) -> AdapterRequest:
    return AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(complexity=complexity),
    )


# ── a native plan with no measured handshake never reaches a launch ────────


class _NativePlugin(HarnessAdapter):
    def __init__(self) -> None:
        self.launched: list[tuple] = []

    @property
    def name(self) -> str:
        return "native-probe"

    def capabilities(self) -> frozenset[str]:
        return frozenset({"native-tool", "in-place"})

    def build_command(self, plan: HarnessPlan) -> Sequence[str]:
        return ["native-probe"]

    def launch_seam(self):
        """A protected foreign process seam: records any entry."""

        def _seam(command, plan):
            self.launched.append(tuple(command))
            return _StubResult(succeeded=True)

        return _seam


def _native_plan() -> tuple[_NativePlugin, HarnessPlan]:
    plugin = _NativePlugin()
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(
            complexity=0, capabilities=("native-tool", "in-place")
        ),
    )
    plan = plugin.plan(request, _RecordingRouter(_inline(("m1",))))
    assert "native-tool" in plan.required_capabilities
    return plugin, plan


def test_native_plan_without_a_handshake_is_refused_before_launch():
    plugin, plan = _native_plan()
    with pytest.raises(HarnessAdapterError):
        plugin.execute(plan, launch=plugin.launch_seam())
    assert plugin.launched == [], "a refused plan must not launch anything"


def test_a_self_asserted_binding_is_not_a_measurement():
    # A well-shaped snapshot whose host_binding names a binary that does not
    # exist on this host is not a measurement: admission refuses, and the
    # protected launch seam is never entered.
    plugin, plan = _native_plan()
    now = datetime.now(timezone.utc)
    snapshot = {
        "schema_version": "1.0.0",
        "measured_at": (now - timedelta(seconds=30)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "subject": plan.adapter,
        "issuer": "observer-1",
        "capabilities": {
            "declared": {},
            "detected": {},
            "host_binding": {
                "binary_path": "/nonexistent/agent",
                "binary_version": "1.0",
                "plugin_path": "",
                "mode": "worker",
                "model": "m1",
                "child_worker_config": {"mode": "worker", "tools": ["Agent", "Skill"]},
                "coordinator_config": {},
            },
        },
    }
    binding = HA.HostBinding(
        binary_path="/nonexistent/agent",
        binary_version="1.0",
        mode="worker",
        model="m1",
        effective_tools=("Agent", "Skill"),
        child_worker_config={"mode": "worker", "tools": ["Agent", "Skill"]},
        coordinator_config={},
    )
    requirements = HA.NativeRequirements(
        skill_name="probe", skill_digest="a" * 64
    )
    with pytest.raises(HarnessAdapterError):
        plugin.execute(
            plan,
            launch=plugin.launch_seam(),
            capability_snapshot=snapshot,
            native_requirements=requirements,
            host_binding=binding,
        )
    assert plugin.launched == [], "an unmeasured handshake must not launch"


def test_admission_refusal_names_the_host_binding_asset():
    plugin, plan = _native_plan()
    now = datetime.now(timezone.utc)
    snapshot = {
        "schema_version": "1.0.0",
        "measured_at": (now - timedelta(seconds=30)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "subject": plan.adapter,
        "issuer": "observer-1",
        "capabilities": {"host_binding": {"binary_path": "/nonexistent/agent"}},
    }
    admission = HA.admit_native_plan(
        plan,
        capability_snapshot=snapshot,
        native_requirements=HA.NativeRequirements(
            skill_name="probe", skill_digest="a" * 64
        ),
        host_binding=HA.HostBinding(binary_path="/nonexistent/agent"),
    )
    assert admission.admitted is False
    assert admission.asset is not None
    assert admission.asset.startswith("host_binding")


def test_non_native_plan_keeps_raw_command_compatibility():
    # A plan that does not require native-tool is admitted on the raw-command
    # path (explicitly *not* native skill execution) and the launch proceeds.
    plugin = _Plugin("echo", ("in-place",))
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=CapabilityRequirement(complexity=0, capabilities=("in-place",)),
    )
    plan = plugin.plan(request, _RecordingRouter(_inline(("m1",))))
    calls: list[tuple] = []

    def launch(command, received_plan):
        calls.append(tuple(command))
        return _StubResult(succeeded=True)

    outcome = plugin.execute(plan, launch=launch)
    assert outcome.succeeded is True
    assert calls == [("echo", "m1")]
