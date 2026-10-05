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
    parser.add_argument(
        "command",
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

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        parser.error("A command to run is required.")

    if not args.async_run:
        # Without --async, delegate to subprocess.run synchronously
        import subprocess
        result = subprocess.run(command)
        return result.returncode

    handle = start_async(
        command=command,
        run_id=args.run_id,
    )
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
    parser.add_argument(
        "--command",
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
    if args.command:
        import shlex
        command = shlex.split(args.command)
    try:
        result = restart_attempt(
            args.run_id, command, idempotency_key=args.idempotency_key
        )
    except StoreError as exc:
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
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    except StalePidError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except RunStateError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
