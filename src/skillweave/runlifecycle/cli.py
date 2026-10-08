"""CLI surface for the async run lifecycle.

Provides three subcommands:

- ``skillweave start --async <command>`` — start a durable background run.
- ``skillweave inspect <run-id>`` — snapshot or follow run state/events.
- ``skillweave kill <run-id> <pid>`` — graceful-then-forced kill.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional, Sequence

from .handle import (
    start_async,
    inspect_run,
    kill_run,
    cancel_process,
    restart_attempt,
    InspectMode,
)
from .store import StalePidError, RunStateError, RunMetadata, RunStateStore

# The run-id rule and the documented exit-code table are published once in the
# shared support table (SW-1510-CLI criterion 2) so every async surface applies
# the *same* rule and the same codes rather than each inventing one.
from skillweave.cli.support import (
    EXIT_CODES,
    validate_run_id_or_exit,
)


def _project_root() -> Path:
    return Path.cwd()


def _command_launch_error(command: Sequence[str]) -> Optional[tuple[int, str]]:
    """Return ``(exit_code, message)`` when *command* cannot be launched.

    The pre-check mirrors what the shell does and what a naive ``subprocess``
    call would discover only *after* a fork: a non-existent executable raises a
    raw ``FileNotFoundError`` (POSIX 127) and a present-but-not-executable file
    raises ``PermissionError`` (POSIX 126). Both are returned here so the
    surfaces can report a meaningful message and a distinct documented code
    instead of a Python traceback. ``shutil.which`` resolves a bare name
    through ``PATH`` and checks the executable bit on a path.
    """
    if not command:
        return EXIT_CODES["command_not_found"], "no command given"
    argv0 = command[0]
    if shutil.which(argv0) is not None:
        return None
    if os.path.exists(argv0):
        return EXIT_CODES["not_executable"], f"command not executable: {argv0}"
    return EXIT_CODES["command_not_found"], f"command not found: {argv0}"


def _record_launch_failure(
    run_id: Optional[str], command: Sequence[str], message: str
) -> str:
    """Record a durable ``failed`` run for a launch that never happened.

    A caller who asked for ``--async`` must not be left with a silent
    ``running`` record (or no record) when the command cannot be launched: the
    run is recorded ``failed`` with the reason, so ``inspect`` tells the truth.
    Recording is best-effort -- a store failure never masks the launch error.
    """
    rid = run_id or uuid.uuid4().hex
    try:
        store = RunStateStore(_project_root())
        store.write_metadata(RunMetadata.create(rid, list(command), 0))
        store.update_metadata(
            rid,
            state="failed",
            exit_code=EXIT_CODES["command_not_found"],
            reason=message,
        )
    except Exception:  # noqa: BLE001 - recording is best-effort
        pass
    return rid


def build_start_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillweave start",
        description="Start a command, optionally as a durable background run.",
    )
    parser.add_argument(
        "--async",
        action="store_true",
        dest="async_run",
        help="Start the command in a non-blocking detached subprocess.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Explicit run ID (default: auto-generated uuid hex).",
    )
    # NOTE: the REMAINDER positional is deliberately *not* named ``command``.
    # ``skillweave.cli.main`` builds the router with
    # ``add_subparsers(dest="command")`` and mounts this parser as a *parent*;
    # a positional also named ``command`` overwrites the router's subcommand
    # value with the remainder list (e.g. ``['--', 'echo', 'hi']``), so
    # ``args.command == "start"`` could never be true and the installed
    # ``skillweave start`` entry printed top-level help instead of dispatching
    # here. The name mirrors ``cli.run``'s ``run_argv`` for the same reason.
    parser.add_argument(
        "start_argv",
        nargs=argparse.REMAINDER,
        help="The command to run (use -- to separate).",
    )
    return parser


def build_inspect_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillweave inspect",
        description="Inspect a run's state and events.  Supports snapshot or follow mode.",
    )
    parser.add_argument(
        "run_id",
        help="The run ID to inspect (returned by `skillweave start --async`).",
    )
    parser.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Keep tailing events until the run finishes (like tail -f).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Seconds between poll cycles in follow mode (default: 0.25).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Maximum seconds to wait in follow mode (default: no timeout).",
    )
    return parser


def build_kill_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillweave kill",
        description="Kill a run by its ID and recorded PID.  "
                     "Idempotent: graceful (SIGTERM) then forced (SIGKILL).",
    )
    parser.add_argument(
        "run_id",
        help="The run ID to kill.",
    )
    parser.add_argument(
        "pid",
        type=int,
        help="The PID to kill.  Must match the recorded PID.",
    )
    return parser


def main_start(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_start_parser()
    args = parser.parse_args(argv)

    # A run id supplied by the caller must satisfy the shared rule *before* it
    # reaches the store: an unvalidated id admitted '../x', 'a b', 'x/y', '',
    # '..' and 300-char values, which is a path/token hazard and a meaningless
    # downstream error. An invalid id is a usage error (exit 2).
    if args.run_id is not None:
        validate_run_id_or_exit(args.run_id, parser)

    # ``start_argv`` (not ``command``) so the router's ``dest="command"``
    # survives; see the NOTE in ``build_start_parser``.
    command = list(args.start_argv)
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        parser.error("A command to run is required.")

    # A command the OS cannot launch is reported as a meaningful message and a
    # distinct documented code (127 not-found / 126 not-executable) rather than
    # a raw ``FileNotFoundError`` traceback from a fork. Checked for both the
    # synchronous and the async path so neither leaks a traceback.
    launch_error = _command_launch_error(command)

    if not args.async_run:
        # Without --async, delegate to subprocess.run synchronously.
        if launch_error is not None:
            code, message = launch_error
            sys.stderr.write(f"error: {message}\n")
            return code
        import subprocess
        result = subprocess.run(command)
        return result.returncode

    if launch_error is not None:
        # --async: record the failure durably so a run the caller asked to
        # start is never a silent ``running`` record (or no record at all),
        # then report the meaningful error.
        code, message = launch_error
        rid = _record_launch_failure(args.run_id, command, message)
        sys.stdout.write(json.dumps(
            {"run_id": rid, "error": message, "state": "failed"},
            sort_keys=True) + "\n")
        sys.stderr.write(f"error: {message}\n")
        return code

    # A duplicate ``--run-id`` is a *reservation* refusal, not a crash: the
    # store's PRIMARY KEY rejects the second bootstrap, so ``start_async``
    # raises ``VersionConflictError`` (a ``StoreError``). Surface it as a
    # meaningful error and a nonzero exit rather than an uncaught traceback or
    # a bare "Version conflict ... expected 1, got 6".
    # ``RunIntegrationError`` covers the async reservation-gate refusal.
    from skillweave.runtime.errors import StoreError, VersionConflictError
    from skillweave.runsvc.service import RunIntegrationError

    try:
        handle = start_async(
            command=command,
            run_id=args.run_id,
        )
    except VersionConflictError:
        sys.stderr.write(f"error: run already exists: {args.run_id}\n")
        return EXIT_CODES["error"]
    except RunIntegrationError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_CODES["error"]
    except StoreError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_CODES["error"]
    sys.stdout.write(json.dumps(handle.to_dict(), sort_keys=True) + "\n")
    return 0


def main_inspect(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_inspect_parser()
    args = parser.parse_args(argv)
    validate_run_id_or_exit(args.run_id, parser)

    mode = InspectMode.FOLLOW if args.follow else InspectMode.SNAPSHOT

    return inspect_run(
        run_id=args.run_id,
        mode=mode,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )


def build_cancel_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillweave cancel",
        description="Cancel an owned run after proving process identity. "
                    "Refuses a recycled PID, a stale command or a foreign "
                    "worker; an unobservable process is reported as unknown.",
    )
    parser.add_argument("run_id", help="The run ID to cancel.")
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Optional PID the caller believes is the target (defaults to "
             "the recorded identity).",
    )
    parser.add_argument(
        "--reason",
        default="operator_halt",
        help="Cancellation reason recorded on the acknowledgement event.",
    )
    return parser


def build_resume_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillweave resume",
        description="Resume a run by launching a NEW identified attempt.",
    )
    parser.add_argument("run_id", help="The run ID to resume.")
    # NOTE: ``dest="resume_command"`` (not the bare ``command``) so the flag
    # cannot share an attribute name with the router's
    # ``add_subparsers(dest="command")``. A shared dest let ``--command``
    # overwrite the router's subcommand value, so the installed
    # ``skillweave resume <run-id>`` entry printed top-level help instead of
    # resuming whenever (and only when) ``--command`` was supplied.
    parser.add_argument(
        "--command",
        dest="resume_command",
        default=None,
        help="Command for the new attempt (defaults to the recorded command).",
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Idempotency key for the restart (defaults to restart:<run_id>).",
    )
    return parser


def main_cancel(argv: Optional[Sequence[str]] = None) -> int:
    from skillweave.runtime.errors import ProcessIdentityError, StoreError

    parser = build_cancel_parser()
    args = parser.parse_args(argv)
    validate_run_id_or_exit(args.run_id, parser)
    try:
        result = cancel_process(args.run_id, args.pid)
    except ProcessIdentityError as exc:
        sys.stderr.write(f"error: {exc}\n")
        # Verification refusal carries the declared reason (stale/foreign).
        return EXIT_CODES["identity_refused"]
    except StoreError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_CODES["error"]
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def main_resume(argv: Optional[Sequence[str]] = None) -> int:
    from skillweave.runtime.errors import StoreError

    parser = build_resume_parser()
    args = parser.parse_args(argv)
    validate_run_id_or_exit(args.run_id, parser)
    command = None
    if args.resume_command:
        import shlex
        command = shlex.split(args.resume_command)
    try:
        result = restart_attempt(
            args.run_id, command, idempotency_key=args.idempotency_key
        )
    except (StoreError, RunStateError) as exc:
        # ``RunStateError`` covers "no run found" and "no recorded command";
        # ``StoreError`` covers the terminal-restart refusal and CAS conflicts.
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_CODES["error"]
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def main_kill(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_kill_parser()
    args = parser.parse_args(argv)
    validate_run_id_or_exit(args.run_id, parser)

    try:
        result = kill_run(
            run_id=args.run_id,
            pid=args.pid,
        )
    except StalePidError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except RunStateError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    # A "killed-running inconsistency": the caller asked to kill a run they
    # believe is live, but the observed facts disagree -- the run is already
    # terminal (``already_terminal``) or was recorded ``running`` while its
    # process is already gone (``already_dead``). The library ``kill_run``
    # stays idempotent for automation retries; the CLI surface must not let the
    # inconsistency pass as an unqualified success, so it emits a meaningful
    # diagnostic and a distinct nonzero code (3) rather than 0.
    outcome = result.get("result")
    if outcome in ("already_terminal", "already_dead"):
        sys.stderr.write(
            f"error: run {result.get('run_id')} is not running "
            f"(state={result.get('state')}, outcome={outcome}); "
            "nothing was signalled\n"
        )
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 3

    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0
