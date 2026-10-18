"""The unified SkillWeave CLI router.

Besides the delegation subcommands (``dispatch``, ``run``, ``rework``,
``planning-sync``, ``assess``), this module owns the interactive/JSON **entry**
surface (SW-156-ENTRY-003). ``skillweave entry`` resolves a typed intent through the
shared :class:`skillweave.entry.EntryService` contract (SW-156-ENTRY-001) and
answers in one of two registers:

* on a TTY it guides the operator through the contract step by step;
* otherwise (a pipe, or an explicit ``--json``) it emits a single versioned
  JSON envelope on stdout.

Exit semantics
--------------
The entry surface is the one place a caller can tell *why* it did not proceed,
so its codes are distinct and stable. ``help`` is 0 because argparse already
exits 0 for it::

    0  ok               disposition EXECUTE (start/continue/onboard) or RENDER (inspect)
    2  invalid input    unknown action, missing/blank required flag, argparse usage error
    3  hold             disposition GUIDANCE -- actionable, but the operator must act first
    4  escalate         disposition ESCALATE -- the contract refuses and defers to the owner
    5  failure          the contract rejected the typed inputs, or an unexpected error

``2`` is deliberately the same code argparse itself uses for a usage error, so a
wrapper sees one code for "you invoked me wrong". ``3``/``4`` mirror the
contract's own two severity ranks (GUIDANCE/ESCALATE). ``5`` sits outside those
ranks on purpose: a crash is not a contract verdict, and collapsing it into
"hold" would hide a real defect behind a normal outcome. Deviation from
``rework``'s ``1 = user error, 2 = system error`` is intentional -- four
semantics cannot fit two codes, and the entry contract's vocabulary is the
authority on this surface.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from skillweave.cli import run
from skillweave.cli import rework
from skillweave.cli import onboard as onboard_mod
from skillweave.dispatch import cli as dispatch
from skillweave.cli import observe as observe_mod
from skillweave.cli import planning_sync as planning_sync_mod
from skillweave.cli import assess as assess_mod
from skillweave.cli import native_controller as native_controller_mod
from skillweave.cli import support as support_mod
from skillweave.runlifecycle import cli as runlifecycle_cli

# ── Entry exit semantics (SW-156-ENTRY-003) ─────────────────────────────────

#: ``ok``: EXECUTE or RENDER -- the contract permits the request.
EXIT_OK = 0
#: ``invalid input``: unknown action, blank required flag, or argparse usage error.
EXIT_INVALID_INPUT = 2
#: ``hold``: disposition GUIDANCE -- actionable, but the operator must act first.
EXIT_HOLD = 3
#: ``escalate``: disposition ESCALATE -- the contract refuses and defers to the owner.
EXIT_ESCALATE = 4
#: ``failure``: the contract rejected the typed inputs, or an unexpected error.
EXIT_FAILED = 5

#: Disposition -> exit code. Every disposition is mapped; there is no fallback,
#: so a new disposition cannot silently inherit an existing meaning.
_DISPOSITION_EXIT: Mapping[str, int] = MappingProxyType(
    {
        "execute": EXIT_OK,
        "render": EXIT_OK,
        "guidance": EXIT_HOLD,
        "escalate": EXIT_ESCALATE,
    }
)

#: Disposition -> operator-facing status word used in the JSON envelope.
_STATUS_BY_DISPOSITION: Mapping[str, str] = MappingProxyType(
    {
        "execute": "ok",
        "render": "ok",
        "guidance": "hold",
        "escalate": "escalate",
    }
)

#: The entry actions the contract accepts, exposed as argparse choices so an
#: unknown action is a usage error (exit 2) rather than an inferred default.
_ENTRY_ACTIONS = ("start", "continue", "inspect", "onboard")


class _InvalidEntryInput(Exception):
    """The caller's arguments cannot form a typed intent. Maps to exit 2."""


class _EntryFailure(Exception):
    """The contract rejected the typed inputs. Maps to exit 5."""


# ── Router ──────────────────────────────────────────────────────────────────


def _build_parser(prog: str = "skillweave") -> argparse.ArgumentParser:
    """Build the full router parser. Shared by ``main`` and the tests."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="SkillWeave Multi-agent AI Orchestration",
    )
    subparsers = parser.add_subparsers(title="commands", dest="command")

    # `onboard` subcommand -- operator onboarding
    subparsers.add_parser(
        "onboard",
        help="Run operator onboarding — collect profile, detect phase, persist state",
        parents=[onboard_mod.build_onboard_parser()],
        add_help=False,
    )

    # `entry` subcommand -- interactive / JSON entry through the shared contract
    subparsers.add_parser(
        "entry",
        help="Enter a run interactively, or emit the entry decision as JSON",
        parents=[build_entry_parser()],
        add_help=False,
    )

    # `dispatch` subcommand
    subparsers.add_parser(
        "dispatch",
        help="Execute one wave of a dispatch sequence (experimental)",
        parents=[dispatch.build_parser()],
        add_help=False,
    )

    # `run` subcommand
    subparsers.add_parser(
        "run",
        help="Execute a single authoritative run command",
        parents=[run.build_parser()],
        add_help=False,
    )

    # `rework` subcommand
    subparsers.add_parser(
        "rework",
        help="Generate a structured rework brief from a failed gate log",
        parents=[rework.build_parser()],
        add_help=False,
    )

    # `planning-sync` subcommand
    subparsers.add_parser(
        "planning-sync",
        help="Carry durable substrate areas into the configured planning repository",
        parents=[planning_sync_mod.build_parser()],
        add_help=False,
    )

    # `assess` subcommand -- read-only, tamper-evident assessment receipt
    subparsers.add_parser(
        "assess",
        help="Run a read-only assessment and emit the sealed receipt as JSON",
        parents=[assess_mod.build_parser()],
        add_help=False,
    )

    # `start` subcommand -- async run lifecycle
    subparsers.add_parser(
        "start",
        help="Start a command, optionally as a durable async run",
        parents=[runlifecycle_cli.build_start_parser()],
        add_help=False,
    )

    # `inspect` subcommand -- async run state/events inspection
    subparsers.add_parser(
        "inspect",
        help="Inspect a run's state and events (snapshot or follow)",
        parents=[runlifecycle_cli.build_inspect_parser()],
        add_help=False,
    )

    # `kill` subcommand -- async run kill
    subparsers.add_parser(
        "kill",
        help="Kill a run by its ID and recorded PID",
        parents=[runlifecycle_cli.build_kill_parser()],
        add_help=False,
    )

    # `cancel` subcommand -- identity-verified native cancellation
    subparsers.add_parser(
        "cancel",
        help="Cancel an owned run after verifying its process identity",
        parents=[runlifecycle_cli.build_cancel_parser()],
        add_help=False,
    )

    # `resume` subcommand -- launch a new identified attempt
    subparsers.add_parser(
        "resume",
        help="Resume a run by launching a new identified attempt",
        parents=[runlifecycle_cli.build_resume_parser()],
        add_help=False,
    )

    # `native-controller` subcommand -- the production entry point that
    # constructs the native process service on the durable store and drives one
    # owned attempt (SW-SCOPE-010, unit A). Additive and opt-in.
    subparsers.add_parser(
        "native-controller",
        help="Drive the native process service: acquire the controller lease "
             "and run one owned attempt through the declared six steps",
        parents=[native_controller_mod.build_parser()],
        add_help=False,
    )

    # `support` subcommand -- the versioned support table + shared state
    # interpretation (SW-1510-CLI criterion 2). Read-only.
    subparsers.add_parser(
        "support",
        help="Print the versioned surface support table and shared state interpretation",
        parents=[support_mod.build_parser()],
        add_help=False,
    )

    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    adapter: Optional[Any] = None,
    stdin: Optional[Any] = None,
    stdout: Optional[Any] = None,
) -> int:
    """Route ``argv`` to the matching surface.

    ``adapter``, ``stdin`` and ``stdout`` are injection seams for the entry
    surface: tests supply a crafted adapter and a fake TTY instead of touching
    the filesystem or the real terminal. Production callers pass none of them.
    """
    args_list = list(sys.argv[1:] if argv is None else argv)

    # `--dispatch` flag (Handshake & Observe: non-blocking dispatch)
    if "--dispatch" in args_list:
        return observe_mod.main_dispatch(args_list)

    # `--observe <execution_id>` flag (Handshake & Observe: read-only tailer)
    if "--observe" in args_list:
        return observe_mod.main_observe(args_list)

    parser = _build_parser()
    args = parser.parse_args(args_list)

    if args.command == "onboard":
        return onboard_mod.main(subcommand_argv(args_list))
    elif args.command == "entry":
        return run_entry(
            subcommand_argv(args_list), adapter=adapter, stdin=stdin, stdout=stdout
        )
    elif args.command == "dispatch":
        return dispatch.main(subcommand_argv(args_list))
    elif args.command == "run":
        return run.main(subcommand_argv(args_list))
    elif args.command == "rework":
        return rework.main(subcommand_argv(args_list))
    elif args.command == "planning-sync":
        return planning_sync_mod.main(subcommand_argv(args_list))
    elif args.command == "assess":
        return assess_mod.main(subcommand_argv(args_list))
    elif args.command == "start":
        return runlifecycle_cli.main_start(subcommand_argv(args_list))
    elif args.command == "inspect":
        return runlifecycle_cli.main_inspect(subcommand_argv(args_list))
    elif args.command == "kill":
        return runlifecycle_cli.main_kill(subcommand_argv(args_list))
    elif args.command == "cancel":
        return runlifecycle_cli.main_cancel(subcommand_argv(args_list))
    elif args.command == "resume":
        return runlifecycle_cli.main_resume(subcommand_argv(args_list))
    elif args.command == "native-controller":
        return native_controller_mod.main(subcommand_argv(args_list))
    elif args.command == "support":
        return support_mod.main(subcommand_argv(args_list))
    else:
        parser.print_help()
        return 1


def subcommand_argv(args_list: Sequence[str]) -> list:
    """Return the arguments *after* the subcommand.

    The historical form passed ``args_list[1:]``, which silently produced the
    wrong slice when ``argv`` was supplied explicitly. Deriving the remainder
    from the subcommand's own index is correct for both call styles.
    """
    for index, token in enumerate(args_list):
        if token in ("onboard", "entry", "dispatch", "run", "rework", "planning-sync", "assess", "start", "inspect", "kill", "cancel", "resume", "native-controller", "support"):
            return list(args_list[index + 1:])
    return []


# ── Entry surface ───────────────────────────────────────────────────────────


def build_entry_parser(prog: str = "skillweave entry") -> argparse.ArgumentParser:
    """Build the ``entry`` parser.

    Used both as a subparser parent (``prog="skillweave entry"``, the default)
    and standalone for tests (``prog="skillweave"``), so the two can never
    drift.
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Resolve a typed entry intent through the shared EntryService "
            "contract. On a TTY this guides you; otherwise it emits JSON."
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=_ENTRY_ACTIONS,
        default=None,
        help="Entry action. Omit it on a TTY to be guided interactively.",
    )
    parser.add_argument("--run-id", dest="run_id", help="Run slot (start, continue)")
    parser.add_argument(
        "--from-state",
        dest="from_state",
        help="Already-observed run state to resume from (continue)",
    )
    parser.add_argument(
        "--scope",
        default="workspace",
        help="Inspection scope (inspect; default: workspace)",
    )
    parser.add_argument("--profile", help="Operator profile (onboard)")
    # Observed-state facts. The entry contract's own vocabularies decide what
    # is valid; these flags only supply what the caller already knows, exactly
    # as --skills/--active-skill do. They are what lets a caller reach a
    # non-escalating decision, since omitting a run state is itself a refusal.
    parser.add_argument(
        "--run-state",
        dest="run_state",
        help="Observed run state; must be in the canonical vocabulary",
    )
    parser.add_argument(
        "--phase",
        help="Observed lifecycle phase; must be a canonical phase id",
    )
    parser.add_argument(
        "--onboarding-state",
        dest="onboarding_state",
        help="Observed onboarding state",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Force the JSON envelope even when stdout is a TTY",
    )
    parser.add_argument(
        "--skills",
        action="append",
        default=None,
        metavar="ID",
        help="Override the observed installed skills (repeatable)",
    )
    parser.add_argument(
        "--active-skill",
        dest="active_skill",
        default=None,
        help="Override the observed active skill",
    )
    return parser


def run_entry(
    argv: Optional[Sequence[str]] = None,
    *,
    adapter: Optional[Any] = None,
    stdin: Optional[Any] = None,
    stdout: Optional[Any] = None,
) -> int:
    """Run the ``entry`` surface and return its exit code."""
    in_stream = sys.stdin if stdin is None else stdin
    out_stream = sys.stdout if stdout is None else stdout

    parser = build_entry_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse already printed help (code 0) or the usage error (code 2).
        code = exc.code if isinstance(exc.code, int) else EXIT_INVALID_INPUT
        return EXIT_OK if code == 0 else EXIT_INVALID_INPUT

    interactive = not args.as_json and _is_tty(in_stream)

    try:
        entry_adapter = adapter if adapter is not None else probe_workspace_adapter(args)
        if interactive:
            intent = _collect_intent_interactively(entry_adapter, in_stream, out_stream)
        else:
            intent = _intent_from_args(args)
        decision = _dispatch(intent, entry_adapter)
    except _InvalidEntryInput as exc:
        return _emit_invalid_input(str(exc), out_stream, interactive)
    except _EntryFailure as exc:
        _emit_failure(str(exc), out_stream)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 -- an unexpected error is exit 5, never a hold
        _emit_failure(f"{type(exc).__name__}: {exc}", out_stream)
        return EXIT_FAILED

    if interactive:
        _render_decision(decision, out_stream)
    else:
        _write_json(out_stream, _decision_payload(decision))

    code = _DISPOSITION_EXIT[decision.disposition.value]
    if interactive:
        if code == EXIT_HOLD:
            out_stream.write("\nNo action taken. Address the guidance above, then re-run.\n")
        elif code == EXIT_ESCALATE:
            out_stream.write("\nEscalated: this needs the run owner, not this session.\n")
    return code


def _dispatch(intent: Any, adapter: Any) -> Any:
    """Resolve ``intent`` through the shared contract."""
    from skillweave.entry import EntryService

    try:
        return EntryService().dispatch(intent, adapter)
    except (TypeError, ValueError) as exc:
        raise _EntryFailure(str(exc)) from exc


def _intent_from_args(args: argparse.Namespace) -> Any:
    """Build the typed intent named by ``args``, or refuse."""
    from skillweave.entry import ContinueIntent, InspectIntent, OnboardIntent, StartIntent

    action = args.action
    if action is None:
        raise _InvalidEntryInput(
            "no entry action given; choose one of: " + ", ".join(_ENTRY_ACTIONS)
        )
    if action == "start":
        return StartIntent.of(_required(args.run_id, "--run-id", action))
    if action == "continue":
        return ContinueIntent.of(
            _required(args.run_id, "--run-id", action),
            _required(args.from_state, "--from-state", action),
        )
    if action == "inspect":
        return InspectIntent.of((args.scope or "").strip() or "workspace")
    if action == "onboard":
        return OnboardIntent.of(_required(args.profile, "--profile", action))
    raise _InvalidEntryInput(
        f"unknown entry action {action!r}; choose one of: " + ", ".join(_ENTRY_ACTIONS)
    )


def _required(value: Optional[str], flag: str, action: str) -> str:
    text = (value or "").strip()
    if not text:
        raise _InvalidEntryInput(f"{flag} is required to {action}")
    return text


def _collect_intent_interactively(adapter: Any, in_stream: Any, out_stream: Any) -> Any:
    """Guide the operator to a typed intent. Raises ``_InvalidEntryInput`` on bad input."""
    from skillweave.entry import ContinueIntent, InspectIntent, OnboardIntent, StartIntent

    state = adapter.observe()
    out_stream.write("SkillWeave entry\n")
    out_stream.write(f"  run state : {state.run_state or '(none)'}\n")
    out_stream.write(f"  phase     : {state.phase or '(none)'}\n")
    out_stream.write(f"  skills    : {', '.join(state.installed_skills) or 'none'}\n")
    out_stream.write(f"  active    : {state.active_skill or '(none)'}\n\n")

    action = _prompt_choice(
        "What do you want to do?", _ENTRY_ACTIONS, in_stream, out_stream
    )
    if action == "start":
        return StartIntent.of(_prompt_text("Run slot (--run-id):", in_stream, out_stream))
    if action == "continue":
        run_id = _prompt_text("Run slot (--run-id):", in_stream, out_stream)
        from_state = _prompt_text(
            "Resume from run state (--from-state):", in_stream, out_stream
        )
        return ContinueIntent.of(run_id, from_state)
    if action == "inspect":
        scope = _prompt_text(
            "Inspection scope [workspace]:", in_stream, out_stream, allow_default="workspace"
        )
        return InspectIntent.of(scope or "workspace")
    return OnboardIntent.of(_prompt_text("Operator profile (--profile):", in_stream, out_stream))


def _prompt_choice(prompt: str, choices: Sequence[str], in_stream: Any, out_stream: Any) -> str:
    out_stream.write(f"{prompt} [{'/'.join(choices)}]\n")
    answer = _read_answer(in_stream, out_stream)
    if answer not in choices:
        raise _InvalidEntryInput(
            f"{answer!r} is not one of: " + ", ".join(choices)
        )
    return answer


def _prompt_text(
    prompt: str,
    in_stream: Any,
    out_stream: Any,
    *,
    allow_default: Optional[str] = None,
) -> str:
    out_stream.write(f"  {prompt}\n")
    answer = _read_answer(in_stream, out_stream)
    if not answer and allow_default is not None:
        return allow_default
    if not answer:
        raise _InvalidEntryInput(f"a value is required for {prompt!r}")
    return answer


def _read_answer(in_stream: Any, out_stream: Any) -> str:
    out_stream.write("> ")
    out_stream.flush()
    line = in_stream.readline()
    if not line:
        raise _InvalidEntryInput("input ended before an answer was given")
    return line.strip()


# ── Workspace probe ─────────────────────────────────────────────────────────


def probe_workspace_adapter(args: argparse.Namespace, cwd: Optional[Path] = None) -> Any:
    """Probe the workspace read-only and return an adapter over the facts.

    Installed skills are read from the ``skills/`` directory. The remaining
    facts are supplied by flags, because this probe cannot infer them: it opens
    no store and reads no run record. Any fact left unsupplied is simply absent,
    which the contract treats as unobserved rather than defaulting -- so a bare
    invocation with no run state escalates rather than guessing one.

    Crucially, the probe invents no "unknown" value for a fact it never read:
    an absent key and a dropped fact are the same thing to the contract.
    """
    from skillweave.entry import MappingEntryAdapter

    root = Path.cwd() if cwd is None else Path(cwd)
    facts: dict[str, Any] = {}

    skills_dir = root / "skills"
    try:
        installed = sorted(
            entry.name for entry in skills_dir.iterdir() if entry.is_dir()
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        installed = []
    facts["installed_skills"] = tuple(installed)

    if args.skills is not None:
        facts["installed_skills"] = tuple(args.skills)
    if args.active_skill is not None:
        facts["active_skill"] = args.active_skill
    if args.run_state is not None:
        facts["run_state"] = args.run_state
    if args.phase is not None:
        facts["phase"] = args.phase
    if args.onboarding_state is not None:
        facts["onboarding_state"] = args.onboarding_state

    return MappingEntryAdapter(facts)


# ── Output ──────────────────────────────────────────────────────────────────


def _decision_payload(decision: Any) -> dict:
    """Build the versioned JSON envelope for a contract decision."""
    from skillweave.entry import ENTRY_CONTRACT_VERSION

    payload = dict(decision.to_payload())
    payload.update(
        {
            "schema_version": ENTRY_CONTRACT_VERSION,
            "product_version": product_version(),
            "intent_kind": decision.intent.kind.value,
            "status": _STATUS_BY_DISPOSITION[decision.disposition.value],
            "decision_digest": decision.digest,
            "state": decision.state.to_payload(),
            "exit_code": _DISPOSITION_EXIT[decision.disposition.value],
        }
    )
    return payload


def _emit_invalid_input(message: str, out_stream: Any, interactive: bool) -> int:
    if interactive:
        out_stream.write(f"\nInvalid input: {message}\n")
    else:
        _write_json(
            out_stream,
            {
                "schema_version": _contract_version(),
                "product_version": product_version(),
                "status": "invalid_input",
                "error": message,
                "exit_code": EXIT_INVALID_INPUT,
            },
        )
    return EXIT_INVALID_INPUT


def _emit_failure(message: str, out_stream: Any) -> None:
    # Diagnostics go to stderr so stdout stays a single parseable JSON document.
    envelope = {
        "schema_version": _contract_version(),
        "product_version": product_version(),
        "status": "failed",
        "error": message,
        "exit_code": EXIT_FAILED,
    }
    try:
        sys.stderr.write(json.dumps(envelope, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 -- a broken stream must not mask the failure
        pass


def _render_decision(decision: Any, out_stream: Any) -> None:
    status = _STATUS_BY_DISPOSITION[decision.disposition.value]
    out_stream.write(f"Result: {status} ({decision.disposition.value})\n")
    for reason in decision.reasons:
        out_stream.write(f"  - {reason.code}: {reason.detail}\n")
    if decision.guidance:
        out_stream.write(f"\n{decision.guidance}\n")
    out_stream.write(f"\nstate digest  : {decision.state_digest}\n")
    out_stream.write(f"intent digest : {decision.intent_digest}\n")


def _write_json(out_stream: Any, payload: Mapping[str, Any]) -> None:
    out_stream.write(json.dumps(payload, sort_keys=True) + "\n")


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _contract_version() -> str:
    from skillweave.entry import ENTRY_CONTRACT_VERSION

    return ENTRY_CONTRACT_VERSION


def product_version() -> str:
    """Return SkillWeave's product version.

    Order: the installed distribution's metadata, then the repo's
    ``pyproject.toml`` [project].version. ``pyproject.toml`` is the declared
    source of truth; ``importlib.metadata`` is tried first because an installed
    console-script is not guaranteed to sit inside the source tree.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return str(version("skillweave"))
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 -- metadata is best-effort
        pass

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        found = _read_project_version(candidate)
        if found:
            return found
    return "unknown"


def _read_project_version(path: Path) -> Optional[str]:
    try:
        import tomllib  # Python 3.11+

        with path.open("rb") as handle:
            data = tomllib.load(handle)
        value = data.get("project", {}).get("version")
        return str(value) if value else None
    except ModuleNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        return None

    # Python 3.9/3.10 fallback: read the [project] section's version line only.
    try:
        section = ""
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped
                    continue
                if section == "[project]" and stripped.startswith("version"):
                    _, _, raw = stripped.partition("=")
                    return raw.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


if __name__ == "__main__":
    sys.exit(main())
