"""CLI surface for the async run lifecycle.

Provides three subcommands:

- ``skillweave start --async <command>`` — start a durable background run.
- ``skillweave inspect <run-id>`` — snapshot or follow run state/events.
- ``skillweave kill <run-id> <pid>`` — graceful-then-forced kill.
"""

from __future__ import annotations

import argparse
import json
import sys
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
from .store import StalePidError, RunStateError


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

    # ``start_argv`` (not ``command``) so the router's ``dest="command"``
    # survives; see the NOTE in ``build_start_parser``.
    command = list(args.start_argv)
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        parser.error("A command to run is required.")

    if not args.async_run:
        # Without --async, delegate to subprocess.run synchronously
        import subprocess
        result = subprocess.run(command)
        return result.returncode

    # A duplicate ``--run-id`` is a *reservation* refusal, not a crash: the
    # store's PRIMARY KEY rejects the second bootstrap, so ``start_async``
    # raises ``VersionConflictError`` (a ``StoreError``). Surface it as a
    # meaningful error and a nonzero exit rather than an uncaught traceback.
    # ``RunIntegrationError`` covers the async reservation-gate refusal.
    from skillweave.runtime.errors import StoreError
    from skillweave.runsvc.service import RunIntegrationError

    try:
        handle = start_async(
            command=command,
            run_id=args.run_id,
        )
    except (StoreError, RunIntegrationError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(handle.to_dict(), sort_keys=True) + "\n")
    return 0


def main_inspect(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_inspect_parser()
    args = parser.parse_args(argv)

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
    try:
        result = cancel_process(args.run_id, args.pid)
    except ProcessIdentityError as exc:
        sys.stderr.write(f"error: {exc}\n")
        # Verification refusal carries the declared reason (stale/foreign).
        return 3
    except StoreError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def main_resume(argv: Optional[Sequence[str]] = None) -> int:
    from skillweave.runtime.errors import StoreError

    parser = build_resume_parser()
    args = parser.parse_args(argv)
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
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def main_kill(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_kill_parser()
    args = parser.parse_args(argv)

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
