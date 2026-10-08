"""The generic harness adapter, with the router as decider (SW-158).

Four acceptance criteria, each as a red/green proof:

1. The runner/harness adapter interface is generic and decoupled from
   OpenClaude: ``HarnessAdapter`` is an abstract contract whose own source names
   no concrete harness, and a second plugin with a different identity, a
   different capability set, and a different argv is planned and executed by the
   same generic steps with no core change.
2. SkillWeave passes capability requirements *and* complexity to the router:
   ``AdapterRequest`` carries a ``CapabilityRequirement`` (provider-neutral
   capabilities + the complexity signal) and that exact object reaches
   ``RouterSeam.route`` — proven with a recording seam, not inferred.
3. The router makes the final decision on the model mix and the dispatch
   strategy: swapping only the router changes the decision while the adapter is
   untouched, and the adapter takes the returned ``DispatchStrategy`` verbatim
   (it neither invents nor overrides a model or a strategy).
4. ``OpenClaudeAdapter`` is implemented strictly as a plugin implementing the
   generic interface: it declares its own name/capabilities/argv, is discovered
   through the registry, and refuses by name a decision it cannot honour.

The capability vocabulary is declared on both sides of the one-way
``dispatch -> routing`` dependency; the lockstep assertion here is what keeps
the two copies from drifting. The module also must not pull an optional
subpackage (``runtime``) or ``dispatch`` into the package's eager import
closure, so its imports are scanned rather than assumed.
"""

import ast
import inspect
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.routing import (  # noqa: E402
    HARNESS_CAPABILITIES,
    STRATEGY_CAPABILITIES,
    STRATEGY_INLINE,
    STRATEGY_PARALLEL,
    STRATEGY_SINGLE,
    AdapterRequest,
    CapabilityRequirement,
    DispatchStrategy,
    FaigateRouter,
    HarnessAdapter,
    HarnessAdapterError,
    HarnessAdapterRegistry,
    HarnessPlan,
    OpenClaudeAdapter,
    RouterSeam,
    register_plugins,
)
from skillweave.routing.decide import ComplexityRank, rank_metrics  # noqa: E402

# Harness names the generic core must never branch on. The interface is
# host-neutral; these literals belong only to a plugin's own declaration.
_FORBIDDEN_HARNESS_LITERALS = (
    "openclaude",
    "claude",
    "codex",
    "gemini",
    "opencode",
    "antigravity",
)

_MODULE_PATH = _SRC / "skillweave" / "routing" / "harness_adapter.py"


# ── Criterion 1: the interface is generic and decoupled ────────────────────

def test_interface_is_abstract_with_no_concrete_harness_in_its_source():
    # HarnessAdapter is an ABC that cannot be instantiated without the plugin's
    # own name/capabilities/build_command, and its own source names no concrete
    # harness: the host-specific facts are not in the interface.
    assert inspect.isabstract(HarnessAdapter)
    for method in ("name", "capabilities", "build_command"):
        assert method in HarnessAdapter.__abstractmethods__, (
            f"{method!r} must be an abstract part of the generic interface"
        )
    with pytest.raises(TypeError):
        HarnessAdapter()  # type: ignore[abstract]

    source = inspect.getsource(HarnessAdapter).lower()
    named = [lit for lit in _FORBIDDEN_HARNESS_LITERALS if lit in source]
    assert not named, f"the generic interface names a concrete harness: {named}"


def test_router_seam_names_no_concrete_harness():
    # The router seam is equally host-neutral: it names the decision, not a
    # harness. Faigate is the router, which the seam is allowed to default to.
    source = inspect.getsource(RouterSeam).lower()
    named = [lit for lit in _FORBIDDEN_HARNESS_LITERALS if lit in source]
    assert not named, f"the router seam names a concrete harness: {named}"


def test_a_second_plugin_is_driven_identically_with_no_core_change():
    # The decoupling proof: a brand-new plugin — different name, different
    # capability set, different argv — goes through plan()/execute() unchanged.
    # Nothing generic was edited to make it work.
    class EchoAdapter(HarnessAdapter):
        @property
        def name(self) -> str:
            return "echo"

        def capabilities(self) -> frozenset:
            return frozenset({"in-place"})

        def build_command(self, plan: HarnessPlan):
            return ["echo", *plan.strategy.model_mix]

    router = RecordingRouter(
        DispatchStrategy(
            strategy=STRATEGY_INLINE,
            model_mix=("local-echo",),
            mode="inline",
            router="recording",
        )
    )
    adapter = EchoAdapter()
    request = AdapterRequest(
        run_id="run-echo",
        role="ops",
        requirement=CapabilityRequirement(complexity=0, capabilities=("in-place",)),
    )

    plan = adapter.plan(request, router)
    assert plan.required_capabilities == ("in-place",)
    seen = {}

    def launch(command, received_plan):
        seen["command"] = tuple(command)
        seen["plan"] = received_plan
        return _StubResult(succeeded=True)

    outcome = adapter.execute(plan, launch=launch)
    assert seen["command"] == ("echo", "local-echo")
    assert seen["plan"] is plan
    assert outcome.succeeded is True
    assert outcome.adapter == "echo"


def test_registry_finds_plugins_by_name_and_by_capability():
    adapter = OpenClaudeAdapter()
    echo = _EchoAdapter()
    registry = register_plugins(HarnessAdapterRegistry(), adapter, echo)

    assert registry.names() == ["openclaude", "echo"]
    assert registry.get("echo") is echo
    assert "openclaude" in registry
    assert len(registry) == 2
    # Capability lookup reads the plugins' own declarations, not a core table.
    assert registry.capable_of("installed-skill-digest") == ["openclaude"]
    assert registry.capable_of("in-place") == ["echo"]
    assert registry.capable_of("in-place", "installed-skill-digest") == []


def test_registry_refuses_unknown_and_duplicate_plugins():
    registry = HarnessAdapterRegistry([OpenClaudeAdapter()])
    with pytest.raises(HarnessAdapterError) as exc:
        registry.get("does-not-exist")
    assert exc.value.asset == "does-not-exist"

    with pytest.raises(HarnessAdapterError) as exc:
        registry.register(OpenClaudeAdapter())
    assert exc.value.asset == "openclaude"

    with pytest.raises(HarnessAdapterError):
        registry.register(object())  # type: ignore[arg-type]


def test_generic_module_imports_stay_inside_the_eager_closure():
    # The interface lives in the routing package, whose eager closure must stay
    # free of the optional `runtime` subpackage and must not reach back into
    # `dispatch` (dispatch depends on routing, not the other way round). Scanned
    # from the AST so a future edit cannot quietly break the layering.
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            targets = [node.module or ""]
        for target in targets:
            if target.startswith("skillweave.runtime") or target.startswith(
                "skillweave.dispatch"
            ):
                offenders.append(target)
    assert not offenders, f"harness_adapter imports across a boundary: {offenders}"


# ── Criterion 2: capability requirements + complexity reach the router ─────

def test_request_carries_the_capability_requirement_and_complexity():
    rank = rank_metrics(points=8, criteria=10, depth=5)
    assert isinstance(rank, ComplexityRank)
    requirement = CapabilityRequirement(
        complexity=rank,
        capabilities=("external-process", "status"),
    )
    request = AdapterRequest(
        run_id="run-1",
        role="ops",
        requirement=requirement,
        work=b"do the thing",
        subject_repo="skillweave/skillweave",
        subject_commit="a" * 40,
    )
    assert request.requirement is requirement
    assert request.capabilities == ("external-process", "status")
    assert request.to_dict()["requirement"]["capabilities"] == [
        "external-process",
        "status",
    ]
    assert request.to_dict()["work_bytes"] == len(b"do the thing")


def test_the_exact_request_reaches_the_router_route_seam():
    # Criterion 2 is about the hand-off, so it is proven at the seam: the object
    # the router receives is the object SkillWeave built, with its requirement
    # intact — the adapter does not rewrite the need before the router sees it.
    router = RecordingRouter(
        DispatchStrategy(
            strategy=STRATEGY_PARALLEL,
            model_mix=("m-a", "m-b"),
            mode="full",
            router="recording",
        )
    )
    adapter = OpenClaudeAdapter()
    request = AdapterRequest(
        run_id="run-hand-off",
        role="ops",
        requirement=CapabilityRequirement(
            complexity="deep", capabilities=("stdin",)
        ),
    )

    adapter.plan(request, router)

    assert router.seen == [request]
    received = router.seen[0]
    assert received.requirement.complexity == "deep"
    assert received.requirement.capabilities == ("stdin",)


def test_unknown_capability_falls_closed_at_construction():
    with pytest.raises(HarnessAdapterError) as exc:
        CapabilityRequirement(complexity=0, capabilities=("telepathy",))
    assert exc.value.asset == "telepathy"


def test_missing_complexity_falls_closed_at_construction():
    # A router asked to decide without a complexity signal would have to invent
    # one, so the gap is refused where it is created.
    with pytest.raises(HarnessAdapterError) as exc:
        CapabilityRequirement(complexity=None)
    assert exc.value.asset == "complexity"


def test_complexity_accepts_rank_tier_and_bare_rank_and_refuses_the_rest():
    from skillweave.routing.harness_adapter import tier_for_complexity

    assert tier_for_complexity(0) == ("fast", None)
    assert tier_for_complexity(1) == ("balanced", None)
    assert tier_for_complexity(7) == ("deep", None)
    assert tier_for_complexity("balanced") == ("balanced", None)

    rank = rank_metrics(points=1, criteria=2, depth=0)
    tier, returned = tier_for_complexity(rank)
    assert (tier, returned) == ("fast", rank)

    for bad in (None, -1, 1.5, "turbo", True):
        with pytest.raises(HarnessAdapterError):
            tier_for_complexity(bad)


# ── Criterion 3: the router decides the model mix and strategy ─────────────

def test_default_router_casts_model_mix_and_strategy_from_complexity():
    router = FaigateRouter()
    fast = router.route(
        _request(complexity=0)
    )
    assert fast.model_mix == ("deepseek-v4-flash",)
    assert fast.strategy == STRATEGY_SINGLE
    assert fast.router == "faigate"

    deep = router.route(_request(complexity=2))
    assert len(deep.model_mix) > 1
    assert deep.strategy == STRATEGY_PARALLEL
    assert deep.mode == "full"
    assert deep.tier == "deep"
    # The mix is the preset's pool, never something the adapter contributed.
    assert deep.chairman in deep.model_mix


def test_swapping_only_the_router_changes_the_decision():
    # The adapter is byte-identical across the two runs; only the seam object
    # differs. Two routers, two decisions — which is exactly the criterion.
    request = _request(complexity=1)
    adapter = OpenClaudeAdapter()

    class SmallRouter(RouterSeam):
        name = "small"

        def route(self, req):
            return DispatchStrategy(
                strategy=STRATEGY_SINGLE,
                model_mix=("tiny-1",),
                mode="quick",
                router=self.name,
            )

    class BigRouter(RouterSeam):
        name = "big"

        def route(self, req):
            return DispatchStrategy(
                strategy=STRATEGY_PARALLEL,
                model_mix=("big-1", "big-2", "big-3"),
                mode="full",
                router=self.name,
            )

    small = adapter.plan(request, SmallRouter())
    big = adapter.plan(request, BigRouter())

    assert small.strategy.model_mix == ("tiny-1",)
    assert big.strategy.model_mix == ("big-1", "big-2", "big-3")
    assert small.strategy.strategy != big.strategy.strategy
    # The plugin's argv follows the router's decision — the model is the
    # decision's primary cast member, never a plugin choice — and the headless
    # transport prefix is fixed (SW-1510-OPENCLAUDE).
    _headless_prefix = [
        "openclaude",
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--permission-mode",
        "default",
    ]
    assert adapter.build_command(small) == [*_headless_prefix, "--model", "tiny-1"]
    assert adapter.build_command(big) == [*_headless_prefix, "--model", "big-1"]


def test_adapter_takes_the_router_decision_verbatim():
    decision = DispatchStrategy(
        strategy=STRATEGY_PARALLEL,
        model_mix=("x-1", "x-2"),
        mode="full",
        router="recording",
        tier="deep",
    )
    plan = OpenClaudeAdapter().plan(_request(complexity=2), RecordingRouter(decision))
    assert plan.strategy is decision
    assert plan.strategy.model_mix == decision.model_mix


def test_router_decision_is_validated_not_coerced():
    # An empty model pool or an unknown strategy is refused where it is made,
    # so a malformed decision never becomes a launch.
    for bad in (
        dict(strategy=STRATEGY_SINGLE, model_mix=(), mode="quick", router="r"),
        dict(strategy="magic", model_mix=("m",), mode="quick", router="r"),
        dict(strategy=STRATEGY_SINGLE, model_mix=("m",), mode="quick", router="  "),
    ):
        with pytest.raises(HarnessAdapterError):
            DispatchStrategy(**bad)


def test_strategy_capability_table_is_the_single_source():
    # The strategy each router may choose maps to the capabilities it demands
    # through declared data; the adapter reads that data instead of branching.
    assert STRATEGY_CAPABILITIES[STRATEGY_INLINE] == ("in-place",)
    assert STRATEGY_CAPABILITIES[STRATEGY_SINGLE] == ("external-process", "stdin")
    assert STRATEGY_CAPABILITIES[STRATEGY_PARALLEL] == (
        "external-process",
        "status",
    )
    decision = DispatchStrategy(
        strategy=STRATEGY_SINGLE,
        model_mix=("m",),
        mode="quick",
        router="r",
    )
    assert decision.required_capabilities() == ("external-process", "stdin")


# ── Criterion 4: OpenClaudeAdapter is strictly a plugin ────────────────────

def test_openclaude_is_a_plugin_implementing_the_generic_interface():
    adapter = OpenClaudeAdapter()
    assert isinstance(adapter, HarnessAdapter)
    assert adapter.name == "openclaude"
    # Its capabilities are its own declaration, drawn from the shared
    # vocabulary — and it makes no claim to in-place, which is not its surface.
    assert adapter.capabilities() == OpenClaudeAdapter.DEFAULT_CAPABILITIES
    assert adapter.capabilities() <= frozenset(HARNESS_CAPABILITIES)
    assert adapter.supports("external-process") is True
    assert adapter.supports("in-place") is False


def test_openclaude_refuses_a_decision_it_cannot_honour_by_name():
    # An in-place strategy demands a capability OpenClaude does not hold; the
    # plugin refuses by naming the missing capability rather than downgrading.
    in_place_router = RecordingRouter(
        DispatchStrategy(
            strategy=STRATEGY_INLINE,
            model_mix=("local",),
            mode="inline",
            router="recording",
        )
    )
    with pytest.raises(HarnessAdapterError) as exc:
        OpenClaudeAdapter().plan(_request(complexity=0), in_place_router)
    assert exc.value.asset == "in-place"
    assert "openclaude" in str(exc.value)


def test_openclaude_refuses_a_run_requirement_it_lacks():
    # The refusal also covers what the *run* required, not only what the
    # strategy demanded: the union is reconciled before any launch.
    request = AdapterRequest(
        run_id="run-in-place",
        role="observer",
        requirement=CapabilityRequirement(complexity=0, capabilities=("in-place",)),
    )
    with pytest.raises(HarnessAdapterError) as exc:
        OpenClaudeAdapter().plan(request, FaigateRouter())
    assert exc.value.asset == "in-place"


def test_openclaude_executes_through_the_injected_launch_seam():
    adapter = OpenClaudeAdapter(executable="openclaude-beta")
    plan = adapter.plan(_request(complexity=0))
    seen = {}

    def launch(command, received_plan):
        seen["command"] = tuple(command)
        seen["plan"] = received_plan
        return _StubResult(succeeded=True)

    outcome = adapter.execute(plan, launch=launch)
    assert seen["command"][0] == "openclaude-beta"
    assert seen["plan"] is plan
    assert outcome.command == seen["command"]
    assert outcome.succeeded is True
    assert outcome.to_dict()["adapter"] == "openclaude"


def test_outcome_falls_closed_when_the_launcher_has_no_verdict():
    outcome = OpenClaudeAdapter().execute(
        OpenClaudeAdapter().plan(_request(complexity=0)),
        launch=lambda command, plan: object(),
    )
    assert outcome.succeeded is False


def test_execute_refuses_a_non_callable_launch_seam():
    adapter = OpenClaudeAdapter()
    plan = adapter.plan(_request(complexity=0))
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan, launch=None)  # type: ignore[arg-type]
    assert exc.value.asset == "launch"


def test_openclaude_rejects_an_unknown_capability_declaration():
    with pytest.raises(HarnessAdapterError) as exc:
        OpenClaudeAdapter(capabilities=["telepathy"])
    assert exc.value.asset == "telepathy"


def test_plugin_identity_is_opaque_to_the_generic_steps():
    # The generic plan()/execute() carry the plugin's own name through without
    # the core ever branching on it: the same calls run for two plugins. Each
    # plugin is paired with a strategy it can honour, so only the identity
    # differs across the two runs.
    echo = _EchoAdapter()
    openclaude = OpenClaudeAdapter()

    for adapter, router in ((echo, _inline_router()), (openclaude, FaigateRouter())):
        plan = adapter.plan(_request(complexity=0), router)
        assert plan.adapter == adapter.name
        outcome = adapter.execute(plan, launch=lambda c, p: _StubResult(True))
        assert outcome.adapter == adapter.name


# ── Cross-cutting: one vocabulary, one export surface ─────────────────────

def test_capability_vocabulary_is_in_lockstep_with_the_dispatch_contract():
    # dispatch depends on routing (one way), so the provider-neutral capability
    # vocabulary is declared on both sides. This assertion is the seam that
    # keeps the two copies from drifting silently.
    from skillweave.dispatch.harness_contract import CAPABILITIES

    assert HARNESS_CAPABILITIES == CAPABILITIES


def test_the_interface_names_are_exported_from_the_routing_package():
    import skillweave.routing as routing

    for name in (
        "HarnessAdapter",
        "HarnessAdapterError",
        "HarnessAdapterRegistry",
        "HarnessPlan",
        "HarnessOutcome",
        "AdapterRequest",
        "CapabilityRequirement",
        "DispatchStrategy",
        "RouterSeam",
        "FaigateRouter",
        "OpenClaudeAdapter",
        "HARNESS_CAPABILITIES",
        "STRATEGY_CAPABILITIES",
        "STRATEGY_INLINE",
        "STRATEGY_SINGLE",
        "STRATEGY_PARALLEL",
        "tier_for_complexity",
        "register_plugins",
    ):
        assert name in routing.__all__, f"{name!r} missing from routing.__all__"
        assert hasattr(routing, name), f"{name!r} is not importable from routing"


# ── Helpers ────────────────────────────────────────────────────────────────

class _StubResult:
    """A launcher result carrying only the verdict the interface reads."""

    def __init__(self, succeeded):
        self.succeeded = succeeded


class RecordingRouter(RouterSeam):
    """A seam that records the request it was handed and returns a fixed plan."""

    name = "recording"

    def __init__(self, decision):
        self.decision = decision
        self.seen = []

    def route(self, request):
        self.seen.append(request)
        return self.decision


class _EchoAdapter(HarnessAdapter):
    """A minimal second plugin: distinct identity, capability, and argv."""

    @property
    def name(self):
        return "echo"

    def capabilities(self):
        return frozenset({"in-place"})

    def build_command(self, plan):
        return ["echo", *plan.strategy.model_mix]


def _request(complexity):
    return AdapterRequest(
        run_id="run-x",
        role="ops",
        requirement=CapabilityRequirement(complexity=complexity),
    )


def _inline_router():
    return RecordingRouter(
        DispatchStrategy(
            strategy=STRATEGY_INLINE,
            model_mix=("local",),
            mode="inline",
            router="recording",
        )
    )


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
