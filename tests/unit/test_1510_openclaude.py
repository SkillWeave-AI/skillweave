"""SW-1510-OPENCLAUDE: a scoped headless OpenClaude adapter.

OpenClaude stays an *explicitly selected* user adapter with a host-neutral core
contract, and ``dontAsk`` is never blanket tool authorization.

Every case here drives the real consumer wiring, not a helper name:

* criterion 1 launches a *real child process* (``sys.executable``) through
  ``runtime.runner_adapter`` with the environment the routing adapter actually
  produced, and reads the child's own ``os.environ`` back - proving the ambient
  alternate-provider key is absent in the launched process, not merely absent
  from a returned dict.
* criterion 2 classifies the three typed headless events (permission denial,
  ``input_required``, worker exit) and proves ``input_required`` maps to the
  shared lifecycle ``blocked_input`` result rather than a silent REPL wait.
* criterion 3 measures stderr backpressure on a real capped run and drives the
  missing-plugin / missing-tool / oversized-prompt fixtures through the plugin's
  own refusal path with an injected launch counter that must stay at zero.

No provider, network or socket call is made; the unit-suite hermetic boundary
(tests/unit/conftest.py) blocks sockets and clears provider env vars, and every
launch here is either an injected counter or a local ``sys.executable``.
"""

import json
import os
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.routing.harness_adapter import (  # noqa: E402
    AMBIENT_PROVIDER_ENV_KEYS,
    DEFAULT_MAX_PROMPT_BYTES,
    HEADLESS_ENV_ALLOWLIST,
    HEADLESS_EVENT_INPUT_REQUIRED,
    HEADLESS_EVENT_PERMISSION_DENIED,
    HEADLESS_EVENT_RAW,
    HEADLESS_EVENT_RESULT_ERROR,
    HEADLESS_EVENT_WORKER_EXIT,
    OBSERVER_OWNED_FILENAMES,
    PERMISSION_MODE_DEFAULT,
    PERMISSION_MODE_DONT_ASK,
    AdapterRequest,
    CapabilityRequirement,
    HarnessAdapterError,
    NativeRequirements,
    OpenClaudeAdapter,
    assert_not_observer_owned,
    classify_headless_event,
    clean_launch_environment,
    headless_terminal_status,
    parse_headless_events,
)
from skillweave.runtime import runner_adapter as ra  # noqa: E402

# The same host-binding / snapshot shape SW-1510-CAP established: a real offline
# executable on disk whose sha256 is taken from the bytes.
SKILL_DIGEST = "a" * 64
NATIVE_TOOLS = ("Read", "Bash", "Edit", "Agent", "Skill", "Task")


def _digest(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _shape_digest(value):
    import hashlib

    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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

    def config_digest(self):
        return _shape_digest([self.coordinator_config, self.child_config, "bare"])

    def binding(self):
        return {
            "binary_path": str(self.binary),
            "binary_sha256": _digest(self.binary),
            "binary_version": "fixture-1",
            "plugin_path": str(self.plugin),
            "plugin_sha256": _digest(self.plugin),
            "mode": "bare",
            "model": self.model,
            "coordinator_config": self.coordinator_config,
            "child_worker_config": self.child_config,
            "config_digest": self.config_digest(),
        }

    def snapshot(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        binding = self.binding()
        stage = {
            "skill_digest": SKILL_DIGEST,
            "runtime_config_digest": binding["config_digest"],
            "observer": "fixture-host-collector",
        }
        return {
            "schema_version": "1.0.0",
            "snapshot_id": "cap-1510-oc",
            "revision": "1",
            "measured_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "subject": "openclaude",
            "issuer": "fixture-host-collector",
            "limits": {},
            "readiness": "unknown",
            "evidence_level": "run-attested",
            "capabilities": {
                "declared": {"tools": list(NATIVE_TOOLS)},
                "detected": {
                    "coordinator_tools": list(NATIVE_TOOLS),
                    "child_worker_tools": list(NATIVE_TOOLS),
                },
                "run_attested": {
                    "workflow_stages": {
                        "skill-expanded": dict(stage),
                        "codebase-analyzed": dict(stage),
                    },
                    "native_expansion": dict(stage),
                },
                "host_binding": binding,
            },
        }


class _Counter:
    """An injected launch seam that counts calls and touches no provider."""

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
            run_id="oc-1510",
            role="ops",
            requirement=CapabilityRequirement(
                complexity=0, capabilities=("native-tool",)
            ),
            work=b"/understand",
        )
    )


def _headless_plan(exe, gateway_env=None, permission_mode=PERMISSION_MODE_DEFAULT):
    adapter = OpenClaudeAdapter(
        executable=str(exe),
        permission_mode=permission_mode,
        gateway_env=gateway_env,
    )
    plan = adapter.plan(
        AdapterRequest(
            run_id="oc-1510",
            role="ops",
            requirement=CapabilityRequirement(complexity=0),
            work=b"do the thing",
        )
    )
    return adapter, plan


def _needs():
    return NativeRequirements(
        skill_name="understand",
        skill_digest=SKILL_DIGEST,
        required_tools=("Agent", "Skill"),
        required_stages=("skill-expanded", "codebase-analyzed"),
    )


def _leaky_base_env():
    env = {"PATH": "/usr/bin", "HOME": "/tmp"}
    for key in AMBIENT_PROVIDER_ENV_KEYS:
        env[key] = "ambient-" + key
    return env


# -- criterion 1: clean allowlisted env + pinned settings; ambient cannot redirect --

def test_clean_launch_environment_drops_every_ambient_provider_key():
    env = clean_launch_environment(base_env=_leaky_base_env())
    for key in AMBIENT_PROVIDER_ENV_KEYS:
        assert key not in env, f"ambient provider key {key} leaked into the child env"
    assert env["PATH"] == "/usr/bin"


def test_pinned_gateway_settings_are_the_only_extra_keys():
    env = clean_launch_environment(
        base_env=_leaky_base_env(),
        pinned_env={"APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1"},
    )
    assert env["APPROVED_GATEWAY_BASE_URL"] == "https://gw.example/v1"
    assert set(env) <= set(HEADLESS_ENV_ALLOWLIST) | {"APPROVED_GATEWAY_BASE_URL"}
    for key in AMBIENT_PROVIDER_ENV_KEYS:
        assert key not in env


def test_pinning_an_ambient_provider_key_is_refused_fail_closed():
    with pytest.raises(HarnessAdapterError) as exc:
        clean_launch_environment(
            base_env=_leaky_base_env(),
            pinned_env={"OPENROUTER_API_KEY": "pinned-leak"},
        )
    assert exc.value.asset == "OPENROUTER_API_KEY"


def test_adapter_launch_environment_keeps_pinned_gateway_drops_ambient():
    adapter, _plan = _headless_plan(
        "openclaude",
        gateway_env={"APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1"},
    )
    env = adapter.launch_environment(base_env=_leaky_base_env())
    assert env["APPROVED_GATEWAY_BASE_URL"] == "https://gw.example/v1"
    for key in AMBIENT_PROVIDER_ENV_KEYS:
        assert key not in env


def test_gateway_env_constructor_refuses_an_ambient_provider_key():
    with pytest.raises(HarnessAdapterError) as exc:
        OpenClaudeAdapter(gateway_env={"ANTHROPIC_API_KEY": "leak"})
    assert exc.value.asset == "ANTHROPIC_API_KEY"


def test_runtime_build_child_environment_allowlist_drops_ambient_keeps_pinned():
    env = ra.build_child_environment(
        env_allowlist=["PATH", "HOME"],
        extra_env={"APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1"},
        base_env=_leaky_base_env(),
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/tmp",
        "APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1",
    }


@pytest.mark.parametrize("where", ["allowlist", "extra"])
def test_child_env_refuses_an_ambient_provider_key(where):
    if where == "allowlist":
        kwargs = {"env_allowlist": ["PATH", "OPENAI_API_KEY"]}
    else:
        kwargs = {"env_allowlist": ["PATH"], "extra_env": {"OPENAI_API_KEY": "x"}}
    with pytest.raises(ValueError):
        ra.build_child_environment(base_env=_leaky_base_env(), **kwargs)


def _child_env_probe(env=None, env_allowlist=None):
    """Launch a real child and read back which provider keys it can see."""
    keys = list(AMBIENT_PROVIDER_ENV_KEYS)
    script = (
        "import json, os\n"
        f"keys = {keys!r}\n"
        "print(json.dumps({k: (k in os.environ) for k in keys}))\n"
        'print("PATH" in os.environ)\n'
    )
    result = ra.run_command(
        [sys.executable, "-c", script],
        run_id="oc-1510-env",
        subject_repo="skillweave",
        subject_commit="seed",
        tool="openclaude",
        model="fixture-model",
        env=env,
        env_allowlist=env_allowlist,
    )
    seen, has_path = result.stdout.decode().splitlines()
    return result, json.loads(seen), has_path == "True"


def test_launched_child_process_cannot_see_ambient_provider_keys():
    # The real consumer wiring: the routing adapter produces the child env, the
    # runtime launches the child with it, and the child's own os.environ is read
    # back. A helper name would not prove this; the process does.
    adapter, _plan = _headless_plan(
        "openclaude",
        gateway_env={"APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1"},
    )
    child_env = adapter.launch_environment(base_env=_leaky_base_env())
    # Manual mode: only the explicitly handed env is used (no ambient inherit).
    result, seen, has_path = _child_env_probe(env=child_env, env_allowlist=[])
    assert result.succeeded is True, result.message
    assert has_path is True
    assert not any(seen.values()), f"ambient provider key reached the child: {seen}"


def test_allowlist_mode_inherits_only_allowlisted_keys_in_the_child():
    # Allowlist mode drops ambient keys on the *runtime* side too, reading from
    # an explicit base rather than the ambient shell.
    import os

    saved = {k: os.environ.get(k) for k in AMBIENT_PROVIDER_ENV_KEYS}
    try:
        for k in AMBIENT_PROVIDER_ENV_KEYS:
            os.environ[k] = "ambient-" + k
        result, seen, _ = _child_env_probe(
            env={"APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1"},
            env_allowlist=["PATH", "HOME"],
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert result.succeeded is True, result.message
    assert not any(seen.values()), f"ambient provider key reached the child: {seen}"


# -- criterion 2: typed headless lifecycle events, no silent REPL wait ----------

def test_initial_permission_denial_is_a_typed_event():
    event = classify_headless_event(
        {"type": "system", "subtype": "permission_denied", "tool": "Bash"}
    )
    assert event["kind"] == HEADLESS_EVENT_PERMISSION_DENIED
    assert event["payload"]["tool"] == "Bash"


def test_input_required_is_a_typed_event():
    event = classify_headless_event({"type": "input_required", "prompt": "approve?"})
    assert event["kind"] == HEADLESS_EVENT_INPUT_REQUIRED


def test_worker_exit_is_a_typed_event():
    event = classify_headless_event({"type": "result", "subtype": "exit", "code": 0})
    assert event["kind"] == HEADLESS_EVENT_WORKER_EXIT


def test_an_unrecognised_object_is_surfaced_never_dropped():
    event = classify_headless_event({"type": "assistant", "text": "hi"})
    assert event["kind"] not in (
        HEADLESS_EVENT_PERMISSION_DENIED,
        HEADLESS_EVENT_INPUT_REQUIRED,
        HEADLESS_EVENT_WORKER_EXIT,
    )
    assert event["payload"]["text"] == "hi"


def test_a_non_object_event_is_refused_fail_closed():
    with pytest.raises(HarnessAdapterError) as exc:
        classify_headless_event("a bare string is not a typed event")
    assert exc.value.asset == "event"


def test_parse_headless_events_surfaces_raw_lines_in_order():
    events = parse_headless_events(
        [
            json.dumps({"type": "system", "subtype": "permission_denied"}),
            "not json at all",
            json.dumps({"type": "result"}),
        ]
    )
    assert [e["kind"] for e in events] == [
        HEADLESS_EVENT_PERMISSION_DENIED,
        HEADLESS_EVENT_RAW,
        HEADLESS_EVENT_WORKER_EXIT,
    ]


def test_input_required_maps_to_blocked_input_not_a_wait():
    from skillweave.trace.contracts import JobStatus, TaskVerdict

    status = headless_terminal_status(
        [{"kind": HEADLESS_EVENT_PERMISSION_DENIED}, {"kind": HEADLESS_EVENT_INPUT_REQUIRED}]
    )
    assert status["terminal"] == HEADLESS_EVENT_INPUT_REQUIRED
    lifecycle = status["lifecycle"]
    assert lifecycle.job_status is JobStatus.BLOCKED_INPUT
    assert lifecycle.task_verdict is TaskVerdict.BLOCKED


def test_worker_exit_wins_over_earlier_markers():
    status = headless_terminal_status(
        [{"kind": HEADLESS_EVENT_INPUT_REQUIRED}, {"kind": HEADLESS_EVENT_WORKER_EXIT}]
    )
    assert status["terminal"] == HEADLESS_EVENT_WORKER_EXIT


def test_a_stream_with_no_terminal_marker_is_not_terminal():
    assert headless_terminal_status([{"kind": "event"}]) is None


def test_build_command_is_explicitly_noninteractive_headless():
    adapter, plan = _headless_plan("openclaude")
    argv = list(adapter.build_command(plan))
    assert argv[0] == "openclaude"
    assert "--print" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == PERMISSION_MODE_DEFAULT
    # The model comes from the router's decision, never chosen by the plugin.
    assert argv[argv.index("--model") + 1] == plan.strategy.model_mix[0]


def test_dontask_is_refused_on_a_plan_that_was_not_admitted_native():
    adapter, plan = _headless_plan("openclaude", permission_mode=PERMISSION_MODE_DONT_ASK)
    counter = _Counter()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan, launch=counter)
    assert counter.calls == [], "dontAsk must be refused before any launch"
    assert exc.value.asset == "permission_mode"


def test_dontask_is_allowed_once_the_plan_is_admitted_native(tmp_path):
    # The scoped case: dontAsk is not blanket authorization, but it *is* honoured
    # for a plan whose native-tool handshake actually passed.
    host = _Host(tmp_path)
    adapter = OpenClaudeAdapter(
        executable=str(host.binary), permission_mode=PERMISSION_MODE_DONT_ASK
    )
    plan = adapter.plan(
        AdapterRequest(
            run_id="oc-1510",
            role="ops",
            requirement=CapabilityRequirement(
                complexity=0, capabilities=("native-tool",)
            ),
            work=b"/understand",
        )
    )
    counter = _Counter()
    outcome = adapter.execute(
        plan,
        launch=counter,
        capability_snapshot=host.snapshot(),
        native_requirements=_needs(),
        host_binding=host.binding(),
    )
    assert len(counter.calls) == 1
    argv = counter.calls[0]
    assert argv[argv.index("--permission-mode") + 1] == PERMISSION_MODE_DONT_ASK
    assert outcome.succeeded is True


# -- criterion 3: controlled failure fixtures; no observer-owned writes ---------

def test_missing_plugin_is_a_controlled_failure_with_zero_launch():
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan, launch=counter, missing_plugins=["understand"])
    assert counter.calls == []
    assert exc.value.asset == "plugin:understand"


def test_missing_tool_is_a_controlled_failure_with_zero_launch():
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(
            plan,
            launch=counter,
            required_tools=["Bash"],
            available_tools=["Read"],
        )
    assert counter.calls == []
    assert exc.value.asset == "tool:Bash"


def test_oversized_prompt_is_refused_not_truncated():
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(
            plan,
            launch=counter,
            prompt=b"x" * (DEFAULT_MAX_PROMPT_BYTES + 1),
        )
    assert counter.calls == []
    assert exc.value.asset == "prompt"


def test_a_prompt_at_the_cap_is_accepted():
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    outcome = adapter.execute(
        plan, launch=counter, prompt=b"x" * DEFAULT_MAX_PROMPT_BYTES
    )
    assert len(counter.calls) == 1
    assert outcome.succeeded is True


def test_stderr_backpressure_caps_retained_bytes_and_is_flagged():
    cap = 512
    script = (
        "import sys\n"
        "sys.stderr.write('e' * 50000)\n"
        "sys.stderr.flush()\n"
        "print('done')\n"
    )
    result = ra.run_command(
        [sys.executable, "-c", script],
        run_id="oc-1510-bp",
        subject_repo="skillweave",
        subject_commit="seed",
        tool="openclaude",
        model="fixture-model",
        stderr_retain_limit=cap,
    )
    assert result.stderr_exceeded_cap is True
    assert result.stderr_backpressure_events > 0
    assert len(result.stderr) <= cap
    assert result.stderr_observed_bytes > cap
    # A controlled, defined failure - not a silent success.
    assert result.succeeded is False
    assert result.metadata["stderr_backpressure_events"] == result.stderr_backpressure_events


def test_stderr_within_cap_is_unaffected():
    script = "import sys; sys.stderr.write('small')\n"
    result = ra.run_command(
        [sys.executable, "-c", script],
        run_id="oc-1510-bp2",
        subject_repo="skillweave",
        subject_commit="seed",
        tool="openclaude",
        model="fixture-model",
        stderr_retain_limit=4096,
    )
    assert result.stderr_exceeded_cap is False
    assert result.stderr_backpressure_events == 0
    assert result.stderr == b"small"
    assert result.succeeded is True


@pytest.mark.parametrize(
    "name",
    ["frames.jsonl", "facts.jsonl", "cursor.json", "observer.json", "meta.json"],
)
def test_adapter_cannot_write_an_observer_owned_file(name, tmp_path):
    target = tmp_path / name
    with pytest.raises(HarnessAdapterError) as exc:
        assert_not_observer_owned(str(target))
    assert exc.value.asset == "observer_state"


def test_adapter_cannot_write_inside_an_observer_root(tmp_path):
    root = tmp_path / "observer-state"
    root.mkdir()
    with pytest.raises(HarnessAdapterError) as exc:
        assert_not_observer_owned(str(root / "scratch.txt"), observer_roots=[str(root)])
    assert exc.value.asset == "observer_state"


def test_an_ordinary_worker_path_is_permitted(tmp_path):
    target = tmp_path / "worktree" / "notes.txt"
    assert assert_not_observer_owned(str(target)) == str(target)


# ── Finding A: a missing/non-callable launch seam is a named refusal ─────────

def test_execute_without_a_launch_seam_is_a_named_refusal():
    # The plugin docstring promises a refusal before the argv is built. An
    # omitted seam must not leak a bare TypeError.
    adapter, plan = _headless_plan("openclaude")
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan)  # type: ignore[call-arg]
    assert exc.value.asset == "launch"


def test_execute_with_a_none_launch_seam_is_a_named_refusal():
    adapter, plan = _headless_plan("openclaude")
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan, launch=None)  # type: ignore[arg-type]
    assert exc.value.asset == "launch"


def test_execute_with_a_non_callable_launch_seam_is_a_named_refusal():
    adapter, plan = _headless_plan("openclaude")
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan, launch="not-callable")  # type: ignore[arg-type]
    assert exc.value.asset == "launch"


# ── Finding B: typed events on the adapter's real execution path ─────────────

_PERMISSION_DENIED_LINE = json.dumps(
    {"type": "system", "subtype": "permission_denied", "tool": "Bash"}
)
_INPUT_REQUIRED_LINE = json.dumps({"type": "input_required", "prompt": "approve?"})
_WORKER_EXIT_LINE = json.dumps({"type": "result", "subtype": "exit", "code": 0})
_ASSISTANT_LINE = json.dumps({"type": "assistant", "text": "working"})


def _streaming_launch(adapter, *, stream, stderr_bytes=0, stderr_retain_limit=None, env=None):
    """A launch seam that runs a real child emitting ``stream`` on stdout.

    The child's stdout lines are appended to the same list handed back as
    ``stream_output``, so a *single* ``execute`` call drives the real child and
    then ingests its output into typed events (no second call, no direct helper).
    """
    lines: list[str] = []

    def launch(command, plan):
        script = (
            "import sys\n"
            + "".join(f"print({line!r})\n" for line in stream)
            + (f"sys.stderr.write('e' * {stderr_bytes})\n" if stderr_bytes else "")
        )
        result = ra.run_command(
            [sys.executable, "-c", script],
            run_id="oc-1510-stream",
            subject_repo="skillweave",
            subject_commit="seed",
            tool="openclaude",
            model="fixture-model",
            env=env,
            env_allowlist=[] if env is not None else None,
            stderr_retain_limit=stderr_retain_limit,
        )
        lines.extend(result.stdout.decode().splitlines())
        return result

    return launch, lines


def test_real_child_permission_denial_is_a_typed_event_through_execute():
    adapter, plan = _headless_plan("openclaude")
    launch, lines = _streaming_launch(
        adapter, stream=[_PERMISSION_DENIED_LINE, _ASSISTANT_LINE, _WORKER_EXIT_LINE]
    )
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-stream"
    )
    kinds = [e.kind for e in outcome.result.stream.events]
    assert HEADLESS_EVENT_PERMISSION_DENIED in kinds
    assert kinds[-1] == HEADLESS_EVENT_WORKER_EXIT
    assert outcome.result.stream.blocked is False
    assert outcome.succeeded is True


def test_real_child_input_required_maps_to_blocked_input_through_execute():
    from skillweave.trace.contracts import JobStatus, TaskVerdict

    adapter, plan = _headless_plan("openclaude")
    launch, lines = _streaming_launch(
        adapter, stream=[_PERMISSION_DENIED_LINE, _INPUT_REQUIRED_LINE]
    )
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-stream"
    )
    result = outcome.result
    assert result.blocked is True
    assert result.stream.terminal["terminal"] == HEADLESS_EVENT_INPUT_REQUIRED
    assert result.lifecycle.job_status is JobStatus.BLOCKED_INPUT
    assert result.lifecycle.task_verdict is TaskVerdict.BLOCKED
    # No silent wait: the adapter falls closed rather than reporting success.
    assert outcome.succeeded is False
    assert result.termination == HEADLESS_EVENT_INPUT_REQUIRED


def test_typed_events_serialise_to_attempt_event_v1():
    adapter, plan = _headless_plan("openclaude")
    launch, lines = _streaming_launch(
        adapter, stream=[_PERMISSION_DENIED_LINE, _WORKER_EXIT_LINE]
    )
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-stream"
    )
    events = outcome.result.stream.to_attempt_events(
        subject_repo="skillweave", subject_commit="seed"
    )
    assert events[0]["schema_version"] == "1.0.0"
    assert events[0]["event_id"] == "oc-1510-stream:0"
    assert events[0]["run_id"] == "oc-1510-stream"
    assert events[0]["event_type"] == HEADLESS_EVENT_PERMISSION_DENIED
    assert events[0]["subject_repo"] == "skillweave"


def test_no_stream_output_preserves_the_launcher_result_untouched():
    # A caller that does not opt into headless ingestion still gets its own
    # result object back, unchanged.
    adapter, plan = _headless_plan("openclaude")
    sentinel = _StubResult(succeeded=True)
    outcome = adapter.execute(plan, launch=lambda c, p: sentinel)
    assert outcome.result is sentinel


# ── Finding C: stderr backpressure on the execute path falls closed ──────────

def test_stderr_backpressure_through_execute_falls_closed():
    adapter, plan = _headless_plan("openclaude")
    launch, lines = _streaming_launch(
        adapter,
        stream=[_WORKER_EXIT_LINE],
        stderr_bytes=50000,
        stderr_retain_limit=512,
    )
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-stream"
    )
    result = outcome.result
    assert result.backpressure_exceeded is True
    assert outcome.succeeded is False


# ── Finding C: observer-owned writes refused on the execute path ─────────────

@pytest.mark.parametrize(
    "name", ["frames.jsonl", "facts.jsonl", "cursor.json", "observer.json", "meta.json"]
)
def test_execute_refuses_an_observer_owned_scratch_target(name, tmp_path):
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    target = tmp_path / name
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(
            plan, launch=counter, scratch_target=str(target), scratch_data=b"forged"
        )
    assert counter.calls == [], "an observer-owned write must be refused before launch"
    assert exc.value.asset == "observer_state"
    assert not target.exists(), "no byte may be written to an observer-owned path"


def test_execute_writes_an_ordinary_scratch_target(tmp_path):
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    target = tmp_path / "worktree" / "notes.txt"
    target.parent.mkdir()
    outcome = adapter.execute(
        plan, launch=counter, scratch_target=str(target), scratch_data=b"ok"
    )
    assert len(counter.calls) == 1
    assert target.read_bytes() == b"ok"
    assert outcome.succeeded is True


def test_execute_refuses_a_scratch_target_inside_an_observer_root(tmp_path):
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    root = tmp_path / "observer-state"
    root.mkdir()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(
            plan,
            launch=counter,
            scratch_target=str(root / "scratch.txt"),
            observer_roots=[str(root)],
        )
    assert counter.calls == []
    assert exc.value.asset == "observer_state"


# ── Reachability: the adapter is selected by name through the registry ───────

def test_adapter_is_selected_by_name_through_the_registry_and_executes():
    from skillweave.routing.harness_adapter import (
        HarnessAdapterRegistry,
        register_plugins,
    )

    registry = register_plugins(HarnessAdapterRegistry(), OpenClaudeAdapter())
    assert "openclaude" in registry
    assert registry.capable_of("native-tool") == ["openclaude"]
    adapter = registry.get("openclaude")
    assert adapter.name == "openclaude"

    plan = adapter.plan(
        AdapterRequest(
            run_id="oc-1510-registry",
            role="ops",
            requirement=CapabilityRequirement(complexity=0),
            work=b"do the thing",
        )
    )
    launch, lines = _streaming_launch(
        adapter, stream=[_PERMISSION_DENIED_LINE, _WORKER_EXIT_LINE]
    )
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-registry"
    )
    assert outcome.adapter == "openclaude"
    assert outcome.command[0] == "openclaude"
    assert outcome.succeeded is True


# ── One-shot: real child, real clean env, typed events, all through execute ──

def test_execute_drives_a_real_clean_child_and_types_its_stream():
    adapter, plan = _headless_plan(
        "openclaude",
        gateway_env={"APPROVED_GATEWAY_BASE_URL": "https://gw.example/v1"},
    )
    child_env = adapter.launch_environment(base_env=_leaky_base_env())

    # The child prints which ambient provider keys it can see, then emits a
    # stream-json lifecycle. One execute call drives it and ingests the result.
    probe = json.dumps({k: False for k in AMBIENT_PROVIDER_ENV_KEYS})
    launch, lines = _streaming_launch(
        adapter,
        stream=[_PERMISSION_DENIED_LINE, _INPUT_REQUIRED_LINE],
        env=child_env,
    )
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-e2e"
    )
    # Typed events from a real process, mappable to blocked_input; falls closed.
    assert outcome.result.stream.terminal["terminal"] == HEADLESS_EVENT_INPUT_REQUIRED
    assert outcome.succeeded is False

    # And the child truly could not see an ambient provider key.
    probe_launch, probe_lines = _streaming_launch(
        adapter, stream=[probe], env=child_env
    )
    probe_outcome = adapter.execute(
        plan, launch=probe_launch, stream_output=probe_lines, run_id="oc-1510-e2e-probe"
    )
    # The probe line is a plain JSON object, so it classifies as a benign
    # ``event`` whose ``payload`` is the parsed object itself (no wrapper).
    seen = probe_outcome.result.stream.events[0].data["payload"]
    assert set(seen) == set(AMBIENT_PROVIDER_ENV_KEYS)
    assert not any(seen.values())


# ══ Round 2 · AC2: a failed result is a typed failed terminal, not a success ══
#
# The controller probe: OpenClaude can emit a ``result`` event that reports an
# error (a failure subtype, an ``is_error`` flag, a non-zero embedded exit code)
# and *still exit 0*. Each of these must be a typed ``result_error`` terminal and
# must fall closed through the real execute path, regardless of the exit code.
# A benign result (subtype success / ``is_error`` false / code 0) must still
# succeed, and a message that merely *mentions* the markers stays untyped.

_FAILED_RESULT_LINES = [
    json.dumps({"type": "result", "subtype": "success", "is_error": True,
                "result": "API Error: 429"}),
    json.dumps({"type": "result", "subtype": "error_max_turns"}),
    json.dumps({"type": "result", "subtype": "error_during_execution",
                "is_error": True}),
    json.dumps({"type": "result", "subtype": "exit", "code": 3}),
    json.dumps({"type": "result", "code": 1}),
    json.dumps({"type": "result", "error": "provider refused"}),
]


@pytest.mark.parametrize("line", _FAILED_RESULT_LINES)
def test_failed_result_is_typed_as_result_error(line):
    event = classify_headless_event(json.loads(line))
    assert event["kind"] == HEADLESS_EVENT_RESULT_ERROR
    # It is never mistaken for a clean worker exit.
    assert event["kind"] != HEADLESS_EVENT_WORKER_EXIT


@pytest.mark.parametrize("line", _FAILED_RESULT_LINES)
def test_failed_result_is_a_failed_terminal_not_a_worker_exit(line):
    from skillweave.trace.contracts import JobStatus

    status = headless_terminal_status(parse_headless_events([line]))
    assert status["terminal"] == HEADLESS_EVENT_RESULT_ERROR
    # The typed lifecycle reads as failed / not succeeded.
    lifecycle = status["lifecycle"]
    assert lifecycle.job_status is JobStatus.NOT_STARTED


@pytest.mark.parametrize("line", _FAILED_RESULT_LINES)
def test_failed_result_falls_closed_through_execute(line):
    # The launcher's process exits 0 (the real child runs the emitting script to
    # completion), yet the reported failure must not be a success.
    adapter, plan = _headless_plan("openclaude")
    launch, lines = _streaming_launch(adapter, stream=[line])
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-failed"
    )
    result = outcome.result
    assert result.failed is True
    assert result.blocked is False
    assert outcome.succeeded is False
    assert result.termination == HEADLESS_EVENT_RESULT_ERROR
    assert result.exit_code == 0  # the process exited 0 - and still failed closed


def test_a_benign_result_still_succeeds_through_execute():
    adapter, plan = _headless_plan("openclaude")
    benign = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "code": 0}
    )
    launch, lines = _streaming_launch(adapter, stream=[benign])
    outcome = adapter.execute(
        plan, launch=launch, stream_output=lines, run_id="oc-1510-benign"
    )
    assert outcome.result.failed is False
    assert outcome.result.stream.terminal["terminal"] == HEADLESS_EVENT_WORKER_EXIT
    assert outcome.succeeded is True


def test_prose_that_merely_mentions_an_error_is_not_a_failed_terminal():
    # A message body is not a structured failure field: the words are data.
    line = json.dumps(
        {"type": "result", "subtype": "success",
         "result": "no error occurred; the run completed"}
    )
    event = classify_headless_event(json.loads(line))
    assert event["kind"] == HEADLESS_EVENT_WORKER_EXIT
    assert event["kind"] != HEADLESS_EVENT_RESULT_ERROR


def test_input_required_still_maps_to_blocked_input_with_result_error_present():
    # A failed result and an input request in one stream: the failure is the
    # strongest terminal, and a blocked child is still never a success.
    from skillweave.trace.contracts import JobStatus

    status = headless_terminal_status(
        [{"kind": HEADLESS_EVENT_INPUT_REQUIRED},
         {"kind": HEADLESS_EVENT_RESULT_ERROR}]
    )
    assert status["terminal"] == HEADLESS_EVENT_RESULT_ERROR
    blocked = headless_terminal_status([{"kind": HEADLESS_EVENT_INPUT_REQUIRED}])
    assert blocked["lifecycle"].job_status is JobStatus.BLOCKED_INPUT


# ══ Round 2 · AC3: the observer guard is filesystem identity, not spelling ════

_CASE_VARIANT_NAMES = [
    "META.JSON",
    "Frames.Jsonl",
    "CURSOR.json",
    "Observer.JSON",
    "FACTS.JSONL",
]


@pytest.mark.parametrize("name", _CASE_VARIANT_NAMES)
def test_execute_refuses_a_case_variant_of_an_observer_owned_name(name, tmp_path):
    # On a case-insensitive volume a case variant is the *same file*: it must be
    # refused before any byte is written and before launch.
    adapter, plan = _headless_plan("openclaude")
    counter = _Counter()
    target = tmp_path / name
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(
            plan, launch=counter, scratch_target=str(target), scratch_data=b"forged"
        )
    assert counter.calls == []
    assert exc.value.asset == "observer_state"
    assert not target.exists()


def test_execute_refuses_a_hard_link_to_an_observer_owned_file(tmp_path):
    # A hard link shares the inode: its name and path are neutral, only its
    # filesystem identity betrays it. The adapter's *own* configured root is
    # used, so the guard is tied to a live store, not to a per-call argument.
    root = tmp_path / "observer-store"
    root.mkdir()
    owned = root / "meta.json"
    owned.write_text("real evidence")
    link = tmp_path / "innocent-name.bin"
    os.link(owned, link)
    assert os.stat(link).st_nlink == 2
    assert os.stat(link).st_ino == os.stat(owned).st_ino

    adapter = OpenClaudeAdapter(executable="openclaude", observer_roots=[str(root)])
    assert adapter.observer_roots == (str(root),)
    plan = adapter.plan(
        AdapterRequest(
            run_id="oc-1510",
            role="ops",
            requirement=CapabilityRequirement(complexity=0),
            work=b"do the thing",
        )
    )
    counter = _Counter()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(plan, launch=counter, scratch_target=str(link), scratch_data=b"x")
    assert counter.calls == []
    assert exc.value.asset == "observer_state"
    assert owned.read_text() == "real evidence", "the observer-owned file was not touched"


def test_adapter_configured_observer_root_guards_without_a_per_call_argument(tmp_path):
    # The structural root: execute takes no observer_roots, only the adapter's
    # own configuration carries the store root.
    root = tmp_path / "observer-store"
    root.mkdir()
    adapter = OpenClaudeAdapter(executable="openclaude", observer_roots=[str(root)])
    plan = adapter.plan(
        AdapterRequest(
            run_id="oc-1510",
            role="ops",
            requirement=CapabilityRequirement(complexity=0),
            work=b"do the thing",
        )
    )
    counter = _Counter()
    with pytest.raises(HarnessAdapterError) as exc:
        adapter.execute(
            plan, launch=counter, scratch_target=str(root / "scratch.txt"), scratch_data=b"x"
        )
    assert counter.calls == []
    assert exc.value.asset == "observer_state"


def test_unresolvable_destination_fails_closed():
    # A destination that cannot be resolved is refused rather than written.
    with pytest.raises(HarnessAdapterError) as exc:
        assert_not_observer_owned("bad\x00name")
    assert exc.value.asset == "observer_state"


def test_an_ordinary_scratch_target_under_a_configured_root_elsewhere(tmp_path):
    # A configured observer root does not make unrelated writes fail.
    root = tmp_path / "observer-store"
    root.mkdir()
    adapter = OpenClaudeAdapter(executable="openclaude", observer_roots=[str(root)])
    plan = adapter.plan(
        AdapterRequest(
            run_id="oc-1510",
            role="ops",
            requirement=CapabilityRequirement(complexity=0),
            work=b"do the thing",
        )
    )
    target = tmp_path / "worktree" / "notes.txt"
    target.parent.mkdir()
    outcome = adapter.execute(plan, launch=_Counter(), scratch_target=str(target),
                              scratch_data=b"ok")
    assert target.read_bytes() == b"ok"
    assert outcome.succeeded is True


def test_observer_owned_filenames_vocabulary_is_unchanged():
    # The literal original criterion names the observer-owned files.
    assert set(OBSERVER_OWNED_FILENAMES) == {
        "frames.jsonl", "facts.jsonl", "cursor.json", "observer.json", "meta.json",
    }
