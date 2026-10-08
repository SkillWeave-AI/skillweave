"""Integration tests for the interactive/JSON CLI entry (SW-156-ENTRY-003).

These exercise the acceptance criteria of the entry surface on
``skillweave entry``:

1. A bare invocation on a TTY guides the operator through ``EntryService``.
2. A non-interactive invocation returns versioned JSON.
3. ``help``, invalid input, ``hold`` and ``failure`` have distinct exit
   semantics.

The entry surface takes injected ``adapter``/``stdin``/``stdout`` seams, so
these tests drive it in-process without a real terminal, filesystem or
subprocess. Contract-level behaviour is already covered by
``tests/unit/test_entry_service.py``; this module covers only the CLI's
translation of that contract into streams and exit codes.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from skillweave.cli.main import (
    EXIT_ESCALATE,
    EXIT_FAILED,
    EXIT_HOLD,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    build_entry_parser,
    dispatch as dispatch_mod,
    main,
    planning_sync_mod,
    probe_workspace_adapter,
    product_version,
    rework as rework_mod,
    run as run_mod,
    run_entry,
    subcommand_argv,
)
from skillweave.entry import ENTRY_CONTRACT_VERSION, MappingEntryAdapter

# ── Fixtures and helpers ────────────────────────────────────────────────────

#: A coherent, fully-installed state: start/continue/onboard all EXECUTE.
COHERENT_FACTS = {
    "run_state": "preflight",
    "phase": "build",
    "onboarding_state": "complete",
    "installed_skills": ["alpha", "skillweave-observe"],
    "active_skill": "alpha",
}

#: A terminal run state: start/continue ESCALATE (contract refuses to resume).
TERMINAL_FACTS = {
    "run_state": "failed",
    "phase": "release",
    "installed_skills": ["alpha"],
}

#: A legacy uppercase alias: GUIDANCE (hold), actionable but not yet valid.
LEGACY_FACTS = {
    "run_state": "SANDBOX_PREFLIGHT",
    "phase": "build",
    "installed_skills": ["alpha"],
}

#: An empty skill set: GUIDANCE (hold) -- onboard first, then start.
UNINSTALLED_FACTS = {
    "run_state": "preflight",
    "phase": "build",
    "installed_skills": [],
}


class FakeTTY:
    """A scripted terminal: ``isatty`` is true and answers come from a list."""

    def __init__(self, answers=()) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        if not self._answers:
            return ""
        return str(self._answers.pop(0)) + "\n"

    def flush(self) -> None:
        pass

    def write(self, text: str) -> None:  # pragma: no cover - not written to
        self.prompts.append(text)


class NonTTY(io.StringIO):
    """A pipe: ``isatty`` is false, so the surface must emit JSON."""

    def isatty(self) -> bool:
        return False


class CountingAdapter:
    """Wraps an adapter and counts observations, to prove the service ran."""

    def __init__(self, facts: dict) -> None:
        self._inner = MappingEntryAdapter(facts)
        self.observations = 0

    def observe(self):
        self.observations += 1
        return self._inner.observe()


@pytest.fixture
def out():
    return io.StringIO()


@pytest.fixture
def pipe():
    return NonTTY()


@pytest.fixture
def tty():
    return FakeTTY()


def invoke(argv, *, adapter=None, stdin=None, stdout=None):
    """Call the router's entry surface and return ``(exit_code, stdout_text)``."""
    stream = io.StringIO() if stdout is None else stdout
    code = main(["entry", *argv], adapter=adapter, stdin=stdin, stdout=stream)
    return code, stream.getvalue()


def last_json(text: str) -> dict:
    """Parse the last line of ``text`` as JSON (stdout may hold prompts first)."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    assert lines, "expected JSON on stdout, got nothing"
    return json.loads(lines[-1])


# ── AC3: the codes are distinct ─────────────────────────────────────────────


def test_exit_codes_are_pairwise_distinct():
    """Guards the whole acceptance criterion: no two semantics share a code."""
    codes = [EXIT_OK, EXIT_INVALID_INPUT, EXIT_HOLD, EXIT_ESCALATE, EXIT_FAILED]
    assert len(set(codes)) == len(codes), f"exit codes collide: {codes}"


def test_exit_codes_have_the_documented_absolute_values():
    """Pin the absolute codes, not just their mutual distinctness.

    Distinctness alone would pass if every code shifted together (hold 3->7,
    escalate 4->8), silently breaking any caller or script that checked the
    documented number. The mapping in ``cli/main.py``'s docstring is the
    contract, so assert it literally.
    """
    assert (EXIT_OK, EXIT_INVALID_INPUT, EXIT_HOLD, EXIT_ESCALATE, EXIT_FAILED) == (
        0,
        2,
        3,
        4,
        5,
    )


def test_help_confidence_does_not_reuse_other_codes():
    """``help`` is 0 (argparse's own), and must not collide with any failure."""
    assert EXIT_OK == 0
    for code in (EXIT_INVALID_INPUT, EXIT_HOLD, EXIT_ESCALATE, EXIT_FAILED):
        assert code != EXIT_OK


# ── AC2: non-interactive returns versioned JSON ─────────────────────────────


def test_non_tty_invocation_returns_versioned_json(pipe):
    code, text = invoke(
        ["inspect"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe
    )
    assert code == EXIT_OK
    payload = last_json(text)
    assert payload["schema_version"] == ENTRY_CONTRACT_VERSION
    assert payload["product_version"] and payload["product_version"] != "unknown"
    assert payload["status"] == "ok"
    assert payload["exit_code"] == EXIT_OK


def test_json_envelope_carries_the_decision_evidence(pipe):
    code, text = invoke(
        ["start", "--run-id", "run-1"],
        adapter=MappingEntryAdapter(COHERENT_FACTS),
        stdin=pipe,
    )
    assert code == EXIT_OK
    payload = last_json(text)
    assert payload["disposition"] == "execute"
    assert payload["intent_kind"] == "start"
    assert payload["state"]["run_id"] == ""
    assert payload["state"]["run_state"] == "preflight"
    assert payload["state_digest"] and payload["intent_digest"]
    assert payload["decision_digest"]
    assert payload["reasons"] == []


def test_json_mode_forces_json_on_a_tty(tty):
    """``--json`` overrides the TTY default, so scripts get a parseable envelope."""
    code, text = invoke(
        ["inspect", "--json"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=tty
    )
    assert code == EXIT_OK
    payload = last_json(text)
    assert payload["schema_version"] == ENTRY_CONTRACT_VERSION
    # Nothing was read from the terminal: the guide never ran.
    assert "What do you want to do?" not in text


def test_bare_non_tty_without_json_flag_still_emits_json(pipe):
    """A pipe with no flags is non-interactive: it must answer in JSON, not hang."""
    code, text = invoke([], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    # No action can be inferred from a pipe (the contract forbids a guess).
    assert code == EXIT_INVALID_INPUT
    payload = last_json(text)
    assert payload["status"] == "invalid_input"
    assert payload["schema_version"] == ENTRY_CONTRACT_VERSION


def test_stdout_is_exactly_one_json_document(pipe):
    code, text = invoke(
        ["inspect"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe
    )
    assert code == EXIT_OK
    assert len(text.strip().splitlines()) == 1
    json.loads(text.strip())


def test_envelope_version_matches_contract_and_product():
    assert ENTRY_CONTRACT_VERSION == "skillweave.entry/1"
    assert product_version() == _pyproject_version()


# ── AC1: a bare TTY invocation guides through EntryService ──────────────────


def test_bare_tty_invocation_guides_the_operator():
    adapter = CountingAdapter(COHERENT_FACTS)
    tty = FakeTTY(["inspect", ""])
    code, text = invoke([], adapter=adapter, stdin=tty)
    assert code == EXIT_OK
    # The guide showed observed facts, prompted, and rendered the decision.
    assert "run state : preflight" in text
    assert "What do you want to do?" in text
    assert f"[{'/'.join(['start', 'continue', 'inspect', 'onboard'])}]" in text
    assert "Result: ok (render)" in text
    # It went *through* the contract rather than deciding by itself.
    assert adapter.observations >= 1


def test_tty_guide_collects_a_start_intent():
    adapter = CountingAdapter(COHERENT_FACTS)
    tty = FakeTTY(["start", "run-42"])
    code, text = invoke([], adapter=adapter, stdin=tty)
    assert code == EXIT_OK
    assert "Run slot" in text
    assert "Result: ok (execute)" in text


def test_tty_guide_collects_a_continue_intent():
    tty = FakeTTY(["continue", "run-7", "verify"])
    code, text = invoke([], adapter=CountingAdapter(COHERENT_FACTS), stdin=tty)
    assert code == EXIT_OK
    assert "Resume from run state" in text


def test_tty_guide_collects_an_onboard_intent():
    """A completed onboarding is ESCALATE: the guide still collects the profile."""
    facts = dict(COHERENT_FACTS, onboarding_state="complete")
    tty = FakeTTY(["onboard", "lean"])
    code, text = invoke([], adapter=CountingAdapter(facts), stdin=tty)
    assert code == EXIT_ESCALATE
    assert "Operator profile" in text
    assert "ONBOARD_ALREADY_COMPLETE" in text


def test_tty_guide_onboards_when_not_yet_complete():
    facts = dict(COHERENT_FACTS, onboarding_state="initial")
    tty = FakeTTY(["onboard", "lean"])
    code, text = invoke([], adapter=CountingAdapter(facts), stdin=tty)
    assert code == EXIT_OK
    assert "Operator profile" in text
    assert "Result: ok (execute)" in text


def test_tty_inspect_scope_defaults_without_typing_a_value():
    """An empty answer to a defaulted prompt must not be treated as invalid."""
    tty = FakeTTY(["inspect", ""])
    code, _ = invoke([], adapter=CountingAdapter(COHERENT_FACTS), stdin=tty)
    assert code == EXIT_OK


# ── AC3: invalid input ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["start"],  # --run-id missing
        ["continue", "--run-id", "r"],  # --from-state missing
        ["onboard"],  # --profile missing
        ["start", "--run-id", "   "],  # present but blank
    ],
)
def test_missing_required_flag_is_invalid_input(argv, pipe):
    code, text = invoke(argv, adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    assert code == EXIT_INVALID_INPUT
    payload = last_json(text)
    assert payload["status"] == "invalid_input"
    assert payload["error"]


def test_no_action_on_a_pipe_is_invalid_input(pipe):
    code, text = invoke(["--json"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    assert code == EXIT_INVALID_INPUT
    assert last_json(text)["status"] == "invalid_input"


def test_unknown_action_is_invalid_input():
    """argparse rejects an unknown choice with its own usage error (code 2)."""
    code = run_entry(
        ["frobnicate"],
        adapter=MappingEntryAdapter(COHERENT_FACTS),
        stdin=NonTTY(),
        stdout=io.StringIO(),
    )
    assert code == EXIT_INVALID_INPUT


def test_unknown_action_never_infers_a_start():
    """The contract forbids guessing an intent, so a bad action never dispatches."""
    adapter = CountingAdapter(COHERENT_FACTS)
    code = run_entry(
        ["frobnicate"], adapter=adapter, stdin=NonTTY(), stdout=io.StringIO()
    )
    assert code == EXIT_INVALID_INPUT
    assert adapter.observations == 0


def test_non_tty_invalid_input_envelope_is_json(pipe):
    code, text = invoke(["start"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    assert code == EXIT_INVALID_INPUT
    json.loads(text.strip())
    assert last_json(text)["exit_code"] == EXIT_INVALID_INPUT


def test_tty_bad_choice_is_invalid_input():
    tty = FakeTTY(["frobnicate"])
    code, text = invoke([], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=tty)
    assert code == EXIT_INVALID_INPUT
    assert "Invalid input" in text
    assert "frobnicate" in text


def test_tty_eof_is_invalid_input_not_a_hang():
    tty = FakeTTY([])  # readline returns "" immediately
    code, text = invoke([], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=tty)
    assert code == EXIT_INVALID_INPUT
    assert "Invalid input" in text


# ── AC3: hold vs escalate ───────────────────────────────────────────────────


def test_legacy_run_state_is_hold(pipe):
    code, text = invoke(
        ["start", "--run-id", "r"], adapter=MappingEntryAdapter(LEGACY_FACTS), stdin=pipe
    )
    assert code == EXIT_HOLD
    payload = last_json(text)
    assert payload["status"] == "hold"
    assert payload["disposition"] == "guidance"
    assert [r["code"] for r in payload["reasons"]] == ["LEGACY_RUN_STATE"]
    assert payload["guidance"]  # a hold is never silent
    assert payload["exit_code"] == EXIT_HOLD


def test_missing_skills_is_hold(pipe):
    code, text = invoke(
        ["start", "--run-id", "r"],
        adapter=MappingEntryAdapter(UNINSTALLED_FACTS),
        stdin=pipe,
    )
    assert code == EXIT_HOLD
    assert last_json(text)["status"] == "hold"


def test_terminal_run_start_escalates(pipe):
    code, text = invoke(
        ["start", "--run-id", "r"], adapter=MappingEntryAdapter(TERMINAL_FACTS), stdin=pipe
    )
    assert code == EXIT_ESCALATE
    payload = last_json(text)
    assert payload["status"] == "escalate"
    assert payload["disposition"] == "escalate"
    assert payload["exit_code"] == EXIT_ESCALATE
    assert any(r["code"] == "TERMINAL_RUN_START" for r in payload["reasons"])


def test_terminal_run_continue_escalates(pipe):
    code, text = invoke(
        ["continue", "--run-id", "r", "--from-state", "failed"],
        adapter=MappingEntryAdapter(TERMINAL_FACTS),
        stdin=pipe,
    )
    assert code == EXIT_ESCALATE
    assert last_json(text)["status"] == "escalate"


def test_hold_and_escalate_are_distinct_codes(pipe):
    """The two non-EXECUTE severities must not collapse to one code."""
    hold, _ = invoke(
        ["start", "--run-id", "r"], adapter=MappingEntryAdapter(LEGACY_FACTS), stdin=pipe
    )
    escalate, _ = invoke(
        ["start", "--run-id", "r"], adapter=MappingEntryAdapter(TERMINAL_FACTS), stdin=pipe
    )
    assert hold == EXIT_HOLD
    assert escalate == EXIT_ESCALATE
    assert hold != escalate


def test_hold_is_rendered_with_guidance_on_a_tty():
    tty = FakeTTY(["start", "r"])
    code, text = invoke([], adapter=MappingEntryAdapter(LEGACY_FACTS), stdin=tty)
    assert code == EXIT_HOLD
    assert "Result: hold (guidance)" in text
    assert "LEGACY_RUN_STATE" in text
    assert "migrate the record" in text  # the contract's own guidance text
    assert "No action taken" in text


def test_escalate_is_rendered_as_an_escalation_on_a_tty():
    tty = FakeTTY(["start", "r"])
    code, text = invoke([], adapter=MappingEntryAdapter(TERMINAL_FACTS), stdin=tty)
    assert code == EXIT_ESCALATE
    assert "Result: escalate (escalate)" in text
    assert "Escalated" in text


# ── AC3: failure ────────────────────────────────────────────────────────────


class ExplodingAdapter:
    """An adapter whose observation raises: the unexpected-error path."""

    def observe(self):
        raise RuntimeError("probe exploded")


def test_adapter_error_is_failure(pipe, capsys):
    code, _ = invoke(["inspect"], adapter=ExplodingAdapter(), stdin=pipe)
    assert code == EXIT_FAILED
    captured = capsys.readouterr()
    envelope = json.loads(captured.err.strip())
    assert envelope["status"] == "failed"
    assert envelope["exit_code"] == EXIT_FAILED
    assert "probe exploded" in envelope["error"]


def test_failure_never_shares_a_code_with_hold_or_escalate():
    assert EXIT_FAILED not in (EXIT_HOLD, EXIT_ESCALATE, EXIT_INVALID_INPUT, EXIT_OK)


def test_contract_rejection_is_failure_not_a_crash(pipe, capsys, monkeypatch):
    """A ValueError from the contract's own guards maps to exit 5, not a traceback."""
    from skillweave.entry import EntryService

    def reject(self, intent, adapter):
        raise ValueError("escalate decision requires at least one reason")

    monkeypatch.setattr(EntryService, "dispatch", reject)
    code, _ = invoke(
        ["inspect"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe
    )
    assert code == EXIT_FAILED
    envelope = json.loads(capsys.readouterr().err.strip())
    assert envelope["status"] == "failed"
    assert "requires at least one reason" in envelope["error"]


def test_failure_envelope_stays_off_stdout(pipe, capsys):
    """stdout must remain a valid stream for callers parsing JSON."""
    code, text = invoke(["inspect"], adapter=ExplodingAdapter(), stdin=pipe)
    assert code == EXIT_FAILED
    assert text == ""
    assert json.loads(capsys.readouterr().err.strip())["status"] == "failed"


# ── Determinism ─────────────────────────────────────────────────────────────


def test_same_facts_produce_identical_digests(pipe):
    """Digests are pure functions of observed facts, never of the host."""
    _, first = invoke(["inspect"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    _, second = invoke(["inspect"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    assert last_json(first)["state_digest"] == last_json(second)["state_digest"]
    assert last_json(first)["decision_digest"] == last_json(second)["decision_digest"]


def test_skill_order_does_not_change_the_digest(pipe):
    reordered = dict(COHERENT_FACTS, installed_skills=["skillweave-observe", "alpha"])
    _, first = invoke(["inspect"], adapter=MappingEntryAdapter(COHERENT_FACTS), stdin=pipe)
    _, second = invoke(["inspect"], adapter=MappingEntryAdapter(reordered), stdin=pipe)
    assert last_json(first)["state_digest"] == last_json(second)["state_digest"]


# ── Workspace probe ─────────────────────────────────────────────────────────


def test_probe_observes_the_skills_directory(tmp_path):
    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    (tmp_path / "skills" / "beta").mkdir()
    (tmp_path / "skills" / "not-a-dir").write_text("x")
    adapter = probe_workspace_adapter(build_entry_parser().parse_args([]), cwd=tmp_path)
    assert adapter.observe().installed_skills == ("alpha", "beta")


def test_probe_tolerates_a_missing_skills_directory(tmp_path):
    adapter = probe_workspace_adapter(build_entry_parser().parse_args([]), cwd=tmp_path)
    assert adapter.observe().installed_skills == ()


def test_probe_honours_skill_and_active_skill_overrides(tmp_path):
    args = build_entry_parser().parse_args(
        ["inspect", "--skills", "x", "--skills", "y", "--active-skill", "y"]
    )
    state = probe_workspace_adapter(args, cwd=tmp_path).observe()
    assert state.installed_skills == ("x", "y")
    assert state.active_skill == "y"


def test_probe_does_not_invent_run_state_or_phase(tmp_path):
    """Facts this probe cannot read are absent, never a fabricated default."""
    state = probe_workspace_adapter(build_entry_parser().parse_args([]), cwd=tmp_path).observe()
    assert state.run_state == ""
    assert state.phase == ""


def test_probe_accepts_supplied_state_facts(tmp_path):
    args = build_entry_parser().parse_args(
        ["inspect", "--run-state", "preflight", "--phase", "build", "--onboarding-state", "initial"]
    )
    state = probe_workspace_adapter(args, cwd=tmp_path).observe()
    assert state.run_state == "preflight"
    assert state.phase == "build"
    assert state.onboarding_state == "initial"


def test_probe_without_a_run_state_escalates_rather_than_guessing(tmp_path, monkeypatch, pipe):
    """A bare repo has no observable run state, so the contract refuses."""
    monkeypatch.chdir(tmp_path)
    code, text = invoke(["start", "--run-id", "r"], stdin=pipe)
    assert code == EXIT_ESCALATE
    payload = last_json(text)
    codes = {r["code"] for r in payload["reasons"]}
    assert "UNKNOWN_RUN_STATE" in codes
    assert "NO_INSTALLED_SKILLS" in codes


def test_probe_end_to_end_reaches_hold_without_an_injected_adapter(tmp_path, monkeypatch, pipe):
    """Real filesystem, no adapter injection: a supplied state still holds."""
    monkeypatch.chdir(tmp_path)
    args = ["start", "--run-id", "r", "--run-state", "preflight", "--phase", "build"]
    code, text = invoke(args, stdin=pipe)
    assert code == EXIT_HOLD
    assert any(r["code"] == "NO_INSTALLED_SKILLS" for r in last_json(text)["reasons"])


# ── Router regression ───────────────────────────────────────────────────────


def test_router_still_registers_every_subcommand():
    parser = _router_parser()
    # Every mounted surface must stay registered: the delegation/entry
    # surfaces plus the async run lifecycle (start/inspect/kill) and the
    # identity-verified cancel/resume pair. ``support`` is the SW-1510-CLI
    # criterion-2 published surface (the versioned support table + shared
    # state interpretation). A REMAINDER positional or an option dest that
    # collides with ``dest="command"`` (see ``test_router_delegates_to_run``)
    # would drop one of these from the router *and* break the installed
    # top-level dispatch.
    assert set(parser.choices) == {
        "entry",
        "dispatch",
        "run",
        "rework",
        "planning-sync",
        "onboard",
        "assess",
        "start",
        "inspect",
        "kill",
        "cancel",
        "resume",
        "support",
    }


def test_bare_router_invocation_prints_help_and_returns_one(capsys):
    assert main([]) == 1
    assert "commands:" in capsys.readouterr().out


def test_router_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == EXIT_OK


def test_entry_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["entry", "--help"])
    assert exc.value.code == EXIT_OK
    assert "--json" in capsys.readouterr().out


def test_unknown_subcommand_exits_with_the_usage_code():
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == EXIT_INVALID_INPUT


def test_subcommand_argv_slices_after_the_subcommand():
    assert subcommand_argv(["entry", "inspect", "--json"]) == ["inspect", "--json"]
    # A token equal to a subcommand name *as a value* must not be mistaken for it.
    assert subcommand_argv(["run", "--tool", "run"]) == ["--tool", "run"]


def _spy(monkeypatch, calls):
    """Patch each delegating surface's ``main`` with a distinct recorder.

    Each recorder records under its own name, so a mutant that sends a
    subcommand to the *wrong* delegate (or to none) is caught, not just a
    mutant that drops arguments.
    """

    def recorder_for(name):
        def recorder(argv):
            calls.append((name, list(argv)))
            return 42

        return recorder

    for name, module in (
        ("run", run_mod),
        ("rework", rework_mod),
        ("dispatch", dispatch_mod),
        ("planning-sync", planning_sync_mod),
    ):
        monkeypatch.setattr(module, "main", recorder_for(name))


@pytest.mark.parametrize(
    "name, argv, expected",
    [
        ("rework", ["rework", "--lane", "SW-156"], ["--lane", "SW-156"]),
        ("planning-sync", ["planning-sync", "--area", "core"], ["--area", "core"]),
        (
            "dispatch",
            ["dispatch", "--sequence", "seq", "--profile", "p", "--wave", "2"],
            ["--sequence", "seq", "--profile", "p", "--wave", "2"],
        ),
    ],
)
def test_router_delegates_post_subcommand_argv(monkeypatch, name, argv, expected):
    """Each surface must receive *its* post-name arguments, and no other.

    Locks down both directions of a slicing bug: the subcommand token itself
    must be dropped, while tokens that merely *equal* a subcommand name as a
    value survive (see ``test_subcommand_argv_slices_after_the_subcommand``).
    """
    calls: list[tuple[str, list[str]]] = []
    _spy(monkeypatch, calls)
    assert main(argv) == 42
    assert calls == [(name, expected)]


def test_router_delegates_to_run(monkeypatch):
    """The router must delegate ``run`` to ``cli.run.main`` with its own argv.

    The REMAINDER positional in ``run.build_parser()`` is named ``run_argv``
    (not ``command``) precisely so it cannot overwrite the router's
    ``add_subparsers(dest="command")`` value -- otherwise the installed
    ``skillweave run`` entry never reached the run surface. Marked as a plain
    test now that the fix has flipped the former strict xfail to XPASS.
    """
    calls: list[tuple[str, list[str]]] = []
    _spy(monkeypatch, calls)
    argv = [
        "run",
        "--tool",
        "run",
        "--model",
        "m",
        "--subject-repo",
        "r",
        "--subject-commit",
        "c",
    ]
    assert main(argv) == 42
    assert calls == [("run", argv[1:])]


def test_router_never_reaches_a_delegate_for_an_unknown_subcommand(monkeypatch, capsys):
    """A guard proving the spies above are wired to the right call sites."""
    calls: list[tuple[str, list[str]]] = []
    _spy(monkeypatch, calls)
    with pytest.raises(SystemExit):
        main(["frobnicate"])
    assert calls == []
    assert "invalid choice" in capsys.readouterr().err


def test_product_version_matches_the_declared_release():
    """The envelope's ``product_version`` must name this checkout's version.

    Reading it back from ``pyproject.toml`` (rather than hardcoding) keeps the
    test honest across releases, while the literal pin records what the
    published envelope said when the contract was written.
    """
    assert product_version() == _pyproject_version() == "1.5.9"


def _router_parser():
    """Return the router parser, reaching its subparser choices."""
    from skillweave.cli.main import _build_parser

    parser = _build_parser()
    return parser._subparsers._group_actions[0]


def _pyproject_version() -> str:
    """Read the declared product version independently of the CLI's own reader."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])
