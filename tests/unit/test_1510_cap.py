"""SW-1510-CAP: native capability admission at the HarnessAdapter.execute boundary.

These tests exercise the *admission* semantics the frozen ``capability-snapshot-v1``
schema cannot express: a signature (``authenticated=true``) is not a measurement, a
declaration is not a detection, a detection is not a run-attested expansion, and a
coordinator's tools never stand in for the child worker's.

Every case drives the real ``HarnessAdapter.execute`` seam with an injected launch
counter, so "zero work on rejection" is *measured*, not asserted by wording: the
counter must be 0 after a refusal and 1 after the single admitted fixture.

The executable the binding pins is a real, offline file on disk written per-test;
its sha256 is taken from the bytes, so a replacement between planning and execution
is a real digest change, not a fabrication. No provider, network or subprocess call
is made - the launcher is injected and only counts.

Remaining authentication integration (a trusted-principal / OS-isolation IDENTITY +
MCP-AUTH gate) is deliberately **out of scope** here: this module proves capability
measurement and admission, and never treats a snapshot's self-asserted
``authenticated`` flag as a fact.
"""

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.routing.harness_adapter import (  # noqa: E402
    AdapterRequest,
    CapabilityAdmission,
    CapabilityRequirement,
    HarnessAdapterError,
    NativeRequirements,
    OpenClaudeAdapter,
    admit_native_plan,
)


# -- Fixtures: a real offline executable, a pinned plugin, one snapshot shape --

SKILL_DIGEST = "a" * 64
RUNTIME_CONFIG_DIGEST = "b" * 64
WORKER_TOOLS = ("Read", "Bash", "Edit")
NATIVE_TOOLS = ("Read", "Bash", "Edit", "Agent", "Skill", "Task")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Host:
    """A real on-disk fake harness plus the configurations that pin to it."""

    def __init__(self, root):
        self.root = Path(root)
        self.binary = self.root / "fake-worker"
        self.binary.write_text("#!/bin/sh\necho fake-harness-ready\nexit 0\n")
        self.binary.chmod(0o700)
        self.plugin = self.root / "plugin.json"
        self.plugin.write_text('{"name":"fixture-worker","version":"1"}')
        self.model = "fixture-model"
        self.coordinator_config = {
            "mode": "bare", "tools": list(NATIVE_TOOLS), "model": self.model,
        }
        self.child_config = {
            "mode": "bare", "tools": list(NATIVE_TOOLS), "model": self.model,
        }

    def binding(self, *, coordinator=None, child=None, mode="bare", model=None):
        return {
            "binary_path": str(self.binary),
            "binary_sha256": _digest(self.binary),
            "binary_version": "fixture-1",
            "plugin_path": str(self.plugin),
            "plugin_sha256": _digest(self.plugin),
            "mode": mode,
            "model": model or self.model,
            "coordinator_config": self.coordinator_config
            if coordinator is None
            else coordinator,
            "child_worker_config": self.child_config if child is None else child,
        }

    def snapshot(
        self,
        *,
        binding=None,
        version="1.0.0",
        subject="openclaude",
        issuer="fixture-host-collector",
        measured_at=None,
        expires_at=None,
        evidence_level="run-attested",
        run_attested=None,
        child_tools=None,
        declared_tools=None,
    ):
        now = datetime.now(timezone.utc)
        measured = measured_at if measured_at is not None else now.isoformat()
        expires = (
            expires_at
            if expires_at is not None
            else (now + timedelta(minutes=5)).isoformat()
        )
        binding = self.binding() if binding is None else binding
        run_attested = self.good_run_attested() if run_attested is None else run_attested
        return {
            "schema_version": version,
            "snapshot_id": "cap-1510",
            "revision": "1",
            "measured_at": measured,
            "expires_at": expires,
            "subject": subject,
            "issuer": issuer,
            "limits": {},
            "readiness": "unknown",
            "evidence_level": evidence_level,
            "capabilities": {
                "declared": {"tools": list(declared_tools or NATIVE_TOOLS)},
                "detected": {
                    "coordinator_tools": list(NATIVE_TOOLS),
                    "child_worker_tools": list(child_tools or NATIVE_TOOLS),
                },
                "run_attested": run_attested,
                "host_binding": binding,
            },
        }

    def good_run_attested(self):
        """The one shape that carries *bound* expansion plus workflow stages."""
        return {
            "workflow_stages": {
                "skill-expanded": {
                    "skill_digest": SKILL_DIGEST,
                    "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
                    "observer": "fixture-host-collector",
                },
                "codebase-analyzed": {
                    "skill_digest": SKILL_DIGEST,
                    "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
                    "observer": "fixture-host-collector",
                },
            },
            "native_expansion": {
                "skill_digest": SKILL_DIGEST,
                "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
                "observer": "fixture-host-collector",
            },
        }


class _Counter:
    """A launch seam that counts calls and never touches a provider."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, plan):
        self.calls.append(tuple(argv))
        return _StubResult(succeeded=True)


class _StubResult:
    def __init__(self, succeeded):
        self.succeeded = succeeded


def _native_plan(exe):
    adapter = OpenClaudeAdapter(executable=str(exe))
    return adapter, adapter.plan(
        AdapterRequest(
            run_id="cap-1510",
            role="ops",
            requirement=CapabilityRequirement(complexity=0, capabilities=("native-tool",)),
            work=b"/understand",
        )
    )


def _needs(*stages):
    return NativeRequirements(
        skill_name="understand",
        skill_digest=SKILL_DIGEST,
        required_tools=("Agent", "Skill"),
        required_stages=tuple(stages) or ("skill-expanded", "codebase-analyzed"),
    )


def _refused(**kwargs):
    """Run a native plan and assert it is refused with ZERO launches."""
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter, plan = _native_plan(host.binary)
        counter = _Counter()
        with pytest.raises(HarnessAdapterError) as exc:
            adapter.execute(plan, launch=counter, **kwargs)
        assert counter.calls == [], "a refusal must not reach the launch seam"
        assert exc.value.asset, "a refusal must name the fact that decided it"
        return exc.value


# -- Presence: a native plan cannot launch without a measured handshake -------

def test_native_plan_refused_without_any_handshake():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter, plan = _native_plan(host.binary)
        counter = _Counter()
        with pytest.raises(HarnessAdapterError) as exc:
            adapter.execute(plan, launch=counter)
        assert counter.calls == []
        assert exc.value.asset in (
            "capability_snapshot", "native_requirements", "host_binding"
        )


@pytest.mark.parametrize(
    "omit",
    ["capability_snapshot", "native_requirements", "host_binding"],
)
def test_native_plan_refused_when_one_of_the_three_inputs_is_missing(omit):
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter, plan = _native_plan(host.binary)
        counter = _Counter()
        args = {
            "capability_snapshot": host.snapshot(),
            "native_requirements": _needs(),
            "host_binding": host.binding(),
        }
        args.pop(omit)
        with pytest.raises(HarnessAdapterError) as exc:
            adapter.execute(plan, launch=counter, **args)
        assert counter.calls == []
        assert exc.value.asset == omit


def test_raw_command_plan_keeps_explicit_compatibility_and_launches():
    # A plan that does NOT require native-tool keeps the ordinary raw-command
    # path: no handshake needed, and it launches exactly once. This path is
    # explicitly NOT advertised as native skill execution.
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter = OpenClaudeAdapter(executable=str(host.binary))
        plan = adapter.plan(
            AdapterRequest(
                run_id="raw-1510",
                role="ops",
                requirement=CapabilityRequirement(complexity=0),
            )
        )
        assert "native-tool" not in plan.required_capabilities
        counter = _Counter()
        outcome = adapter.execute(plan, launch=counter)
        assert len(counter.calls) == 1
        assert outcome.succeeded is True


# -- Version and validity window ---------------------------------------------

def test_unknown_schema_version_cannot_admit_native_work():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(version="99.0.0"),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert exc.asset == "schema_version"


def test_expired_snapshot_cannot_admit_native_work():
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(
                measured_at=(now - timedelta(minutes=10)).isoformat(),
                expires_at=(now - timedelta(minutes=1)).isoformat(),
            ),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert exc.asset == "expires_at.expired"


def test_missing_validity_timestamps_fail_closed():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        snap = host.snapshot()
        snap.pop("measured_at")
        exc = _refused(
            capability_snapshot=snap,
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert exc.asset == "measured_at"


def test_future_measured_at_is_not_a_measurement():
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(
                measured_at=(now + timedelta(hours=1)).isoformat(),
                expires_at=(now + timedelta(hours=2)).isoformat(),
            ),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert exc.asset == "measured_at.future"


# -- Subject / issuer --------------------------------------------------------

def test_snapshot_about_another_subject_is_not_this_plan():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(subject="some-other-harness"),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert exc.asset == "subject"


def test_snapshot_without_an_issuer_is_not_a_measurement():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(issuer="   "),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert exc.asset == "issuer"


# -- Child worker vs coordinator ---------------------------------------------

def test_declared_tools_do_not_promote_a_proof():
    # The declaration loudly advertises Agent/Skill, but the selected child
    # worker holds only Read/Bash/Edit: a bare worker cannot run Understand.
    bare_child = {"mode": "bare", "tools": list(WORKER_TOOLS), "model": "fixture-model"}
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        binding = host.binding(child=bare_child)
        exc = _refused(
            capability_snapshot=host.snapshot(
                binding=binding, child_tools=list(WORKER_TOOLS)
            ),
            native_requirements=_needs(),
            host_binding=binding,
        )
        assert "child_worker_config.tools" in exc.asset


def test_coordinator_tools_cannot_stand_in_for_the_child():
    # Coordinator advertises Agent/Skill; the child worker (the surface a native
    # plan actually runs on) does not. The refusal keeps both visible.
    bare_child = {"mode": "bare", "tools": list(WORKER_TOOLS), "model": "fixture-model"}
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        binding = host.binding(child=bare_child)
        admission = admit_native_plan(
            _native_plan(host.binary)[1],
            capability_snapshot=host.snapshot(binding=binding, child_tools=list(WORKER_TOOLS)),
            native_requirements=_needs(),
            host_binding=binding,
        )
        assert admission.admitted is False
        # Both surfaces stay visible: the coordinator holds Agent/Skill, the
        # child does not, and only the child's list can admit the plan.
        assert "Agent" in admission.observed["coordinator_tools"]
        assert admission.observed["child_tools"] == sorted(WORKER_TOOLS)
        assert admission.observed["missing_child_tools"]


# -- Weak evidence: installation / init / catalog are not expansion ----------

def test_init_installation_and_model_catalog_do_not_prove_expansion():
    weak = {
        "installed": True,
        "installation": {"path": "/opt/fixture"},
        "models_visible": True,
        "model_catalog": ["fixture-model"],
        "system_init": {"tools": ["Agent", "Skill"]},
        "init": {"tools": ["Agent", "Skill"]},
    }
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        admission = admit_native_plan(
            _native_plan(host.binary)[1],
            capability_snapshot=host.snapshot(run_attested=weak),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert admission.admitted is False
        # The weak facts stay visible; they were consumed, not silently dropped.
        assert admission.observed["weak_evidence_present"]
        assert "native_expansion" in admission.asset


def test_self_asserted_authenticated_flag_is_never_a_measurement():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        snap = host.snapshot(
            run_attested={"authenticated": True, "installed": True}
        )
        snap["capabilities"]["detected"]["authenticated"] = True
        admission = admit_native_plan(
            _native_plan(host.binary)[1],
            capability_snapshot=snap,
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert admission.admitted is False
        assert admission.observed["authenticated_flag_seen"] is True  # seen, not trusted


# -- Semantic stage mismatch -------------------------------------------------

def test_expansion_bound_to_a_different_skill_digest_is_refused():
    # The expansion observation is real and bound, but it binds a *different*
    # skill digest than the plan requires: bound evidence for some other skill
    # is not evidence for this one.
    run_attested = {
        "workflow_stages": {},
        "native_expansion": {
            "skill_digest": "c" * 64,
            "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
            "observer": "fixture-host-collector",
        },
    }
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(run_attested=run_attested),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert "native_expansion" in exc.asset


def test_a_stdout_claim_without_binding_is_not_stage_proof():
    # A bare string claiming a stage happened carries no digest, no runtime
    # config and no observer identity: it is not bound evidence. Expansion is
    # valid here, so the refusal must come from the claimed workflow stage.
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(
                run_attested={
                    "workflow_stages": {"codebase-analyzed": "yes it analyzed"},
                    "native_expansion": {
                        "skill_digest": SKILL_DIGEST,
                        "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
                        "observer": "fixture-host-collector",
                    },
                }
            ),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert "codebase-analyzed" in exc.asset


def test_required_stage_missing_from_bound_evidence_is_refused():
    run_attested = {
        "native_expansion": {
            "skill_digest": SKILL_DIGEST,
            "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
            "observer": "fixture-host-collector",
        },
        "workflow_stages": {
            "skill-expanded": {
                "skill_digest": SKILL_DIGEST,
                "runtime_config_digest": RUNTIME_CONFIG_DIGEST,
                "observer": "fixture-host-collector",
            },
        },
    }
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        exc = _refused(
            capability_snapshot=host.snapshot(run_attested=run_attested),
            native_requirements=_needs("skill-expanded", "codebase-analyzed"),
            host_binding=host.binding(),
        )
        assert "codebase-analyzed" in exc.asset


# -- Measured replacement at execution: the pin no longer describes reality --

def test_binary_replaced_after_planning_invalidates_the_pin():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter, plan = _native_plan(host.binary)
        snapshot = host.snapshot()
        binding = host.binding()  # pinned against the ORIGINAL bytes
        # Replace the executable AFTER the handshake was pinned.
        host.binary.write_text("#!/bin/sh\nexit 42\n")
        host.binary.chmod(0o700)
        counter = _Counter()
        with pytest.raises(HarnessAdapterError) as exc:
            adapter.execute(
                plan, launch=counter,
                capability_snapshot=snapshot,
                native_requirements=_needs(),
                host_binding=binding,
            )
        assert counter.calls == []
        assert exc.value.asset == "host_binding.binary_sha256"


def test_plugin_replaced_after_planning_invalidates_the_pin():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter, plan = _native_plan(host.binary)
        snapshot = host.snapshot()
        binding = host.binding()
        host.plugin.write_text('{"name":"fixture-worker","version":"2"}')
        counter = _Counter()
        with pytest.raises(HarnessAdapterError) as exc:
            adapter.execute(
                plan, launch=counter,
                capability_snapshot=snapshot,
                native_requirements=_needs(),
                host_binding=binding,
            )
        assert counter.calls == []
        assert exc.value.asset == "host_binding.plugin_sha256"


def test_child_configuration_mutated_after_measurement_is_refused():
    # The snapshot was taken on a child config without Agent/Skill, but the live
    # binding claims a wider one: a contradiction fails closed.
    narrow = {"mode": "bare", "tools": list(WORKER_TOOLS), "model": "fixture-model"}
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        snap = host.snapshot(child_tools=list(WORKER_TOOLS))
        snap["capabilities"]["host_binding"] = host.binding(child=narrow)
        counter = _Counter()
        with pytest.raises(HarnessAdapterError) as exc:
            _native_plan(host.binary)[0].execute(
                _native_plan(host.binary)[1], launch=counter,
                capability_snapshot=snap,
                native_requirements=_needs(),
                host_binding=host.binding(),  # live, wider child config
            )
        assert counter.calls == []
        assert "child_worker_config" in exc.value.asset


# -- The genuine measured positive fixture -----------------------------------

def test_a_genuine_measured_handshake_is_admitted_and_launches_once():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        adapter, plan = _native_plan(host.binary)
        counter = _Counter()
        outcome = adapter.execute(
            plan, launch=counter,
            capability_snapshot=host.snapshot(),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert len(counter.calls) == 1
        assert counter.calls[0][0] == str(host.binary)
        assert outcome.succeeded is True
        assert "native-tool" in plan.required_capabilities


def test_a_revision_bump_alone_does_not_change_the_verdict():
    # The revision field is advisory: the same measured content admits whether
    # revision is 1 or 7, because admission reads *observations*, not a counter.
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        plan = _native_plan(host.binary)[1]
        for revision in ("1", "7"):
            snap = host.snapshot()
            snap["revision"] = revision
            admission = admit_native_plan(
                plan,
                capability_snapshot=snap,
                native_requirements=_needs(),
                host_binding=host.binding(),
            )
            assert admission.admitted is True, revision
            assert admission.observed["revision_advisory"] is True


def test_admission_is_an_attributable_object():
    with tempfile.TemporaryDirectory() as area:
        host = _Host(area)
        admission = admit_native_plan(
            _native_plan(host.binary)[1],
            capability_snapshot=host.snapshot(),
            native_requirements=_needs(),
            host_binding=host.binding(),
        )
        assert isinstance(admission, CapabilityAdmission)
        record = admission.to_dict()
        assert record["admitted"] is True
        assert record["observed"]["admitted_skill_digest"] == SKILL_DIGEST
        # The audit record is JSON-serialisable: no live objects leak into it.
        json.dumps(record)


def test_native_requirements_from_mapping_round_trips():
    requirements = NativeRequirements.from_mapping(
        {
            "skill_name": "understand",
            "skill_digest": SKILL_DIGEST,
            "required_tools": ["Agent", "Skill"],
            "required_stages": ["skill-expanded", "codebase-analyzed"],
        }
    )
    assert requirements.skill_name == "understand"
    assert "Agent" in requirements.required_tools
    assert "codebase-analyzed" in requirements.required_stages
    assert requirements.to_dict()["skill_digest"] == SKILL_DIGEST
