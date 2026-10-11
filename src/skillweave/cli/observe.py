"""Handshake & Observe CLI surface (SW-OBSERVE-001).

Two top-level flags are provided by this module:

``skillweave --dispatch <command ...>``
    Start execution in a **non-blocking subprocess** and immediately return a
    JSON handle so the harness is never blocked on a long-running child.  The
    handle shape is::

        {"pid": <int>, "execution_id": <str>, "log_path": <str>}

    The child writes every output line to a run-log file under
    ``.skillweave/tracking-log/run-<execution_id>.log``.  The harness is free
    to continue as soon as the handle has been received.

``skillweave --observe <execution_id>``
    Open the run-log produced by ``--dispatch`` (or any other mechanism that
    honours the same naming convention) and **stream its contents line-by-line**
    to *stdout* in read-only mode.  No state is mutated: the observer only
    reads.

Design constraints:
- This module calls ``subprocess.Popen`` for ``--dispatch`` and only does
  tail-reading for ``--observe``; no SQLite write, no state machine, no artifact
  persistence touches happen here.
- The child command can be anything (the ``command`` positional remainder).
  Production callers will pass the ``skillweave run ...`` invocation as the
  child, but tests can pass any executable fragment.
- The dispatched child runs under a tiny detached wrapper that appends a
  ``{"event": "run_finished"}`` / ``{"event": "run_error"}`` sentinel to the
  run-log when the command terminates, so ``--observe`` can exit cleanly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from skillweave.cli.support import validate_run_id_or_exit
from skillweave.dispatch.application import generate_run_id

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_LOG_DIR_SEGMENT = Path(".skillweave") / "tracking-log"
_LOG_PREFIX = "run-"

# Detached wrapper executed by ``--dispatch``.  It runs the real command with
# stdout/stderr appended to the run-log, then appends a completion sentinel so
# an observer can detect clean termination without polling a pid.  The wrapper
# is its own session (``start_new_session=True``), so it survives the harness.
_WRAPPER_SRC = (
    "import subprocess, sys, json\n"
    "log_path = sys.argv[1]\n"
    "cmd = sys.argv[2:]\n"
    "with open(log_path, 'a') as log:\n"
    "    try:\n"
    "        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)\n"
    "        rc = proc.returncode\n"
    "    except Exception as exc:\n"
    "        log.write(json.dumps({'event': 'run_error', 'error': str(exc)}) + '\\n')\n"
    "        log.flush()\n"
    "        sys.exit(1)\n"
    "    log.write(json.dumps({'event': 'run_finished', 'returncode': rc}) + '\\n')\n"
    "    log.flush()\n"
)


def _log_path(project_root: Path, execution_id: str) -> Path:
    """Return the canonical path of the run-log for *execution_id*."""
    return project_root / _LOG_DIR_SEGMENT / f"{_LOG_PREFIX}{execution_id}.log"


# ---------------------------------------------------------------------------
# --dispatch
# ---------------------------------------------------------------------------


def build_dispatch_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``--dispatch`` flag."""
    parser = argparse.ArgumentParser(
        prog="skillweave --dispatch",
        description=(
            "Start execution in a non-blocking subprocess and return a JSON "
            "handle immediately. The harness does NOT block after dispatch."
        ),
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Start the command in a non-blocking subprocess.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root (default: current directory). "
             "Run-logs are written to <project-root>/.skillweave/tracking-log/.",
    )
    parser.add_argument(
        "--execution-id",
        default=None,
        help="Explicit execution ID (default: auto-generated UUID hex).",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="The command to run in the background (use -- to separate).",
    )
    return parser


def dispatch_bg(
    command: Sequence[str],
    project_root: Path,
    execution_id: str,
) -> dict:
    """Start *command* as a detached subprocess, return the JSON handle dict.

    The child process is started with ``Popen`` fully decoupled from the
    harness: ``stdin``, ``stdout``, and ``stderr`` are detached (output is
    redirected to the run-log).  The function returns immediately after the
    child has been forked -- it does **not** wait for the child to finish.

    Args:
        command: The command line to execute (list of strings).
        project_root: Root directory of the project.
        execution_id: The unique identifier for this execution.

    Returns:
        A dict with ``pid``, ``execution_id``, and ``log_path`` keys.
    """
    log_file = _log_path(project_root, execution_id)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "w") as log_fh:
        # Write a header line so observers can detect the file immediately.
        log_fh.write(
            json.dumps(
                {
                    "event": "dispatch_started",
                    "execution_id": execution_id,
                    "command": list(command),
                    "log_path": str(log_file),
                }
            )
            + "\n"
        )
        log_fh.flush()

    proc = subprocess.Popen(
        [sys.executable, "-c", _WRAPPER_SRC, str(log_file), *list(command)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        # Detach the child from the current process group so it keeps
        # running even if the parent exits.
        start_new_session=True,
        cwd=str(project_root),
    )

    return {
        "pid": proc.pid,
        "execution_id": execution_id,
        "log_path": str(log_file),
    }


def main_dispatch(argv: Optional[Sequence[str]] = None) -> int:
    """Entry-point for the ``--dispatch`` flag.

    Parses arguments, forks the child, and writes the JSON handle to stdout.
    Exits with code 0 on success, 1 if no command was provided.
    """
    parser = build_dispatch_parser()
    args = parser.parse_args(argv)

    command: list[str] = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        parser.error("A command to run in the background is required.")

    # An explicit execution id obeys the same run-id rule the async lifecycle
    # surfaces apply (shared support table, SW-1510-CLI criterion 2/3): an
    # invalid id is a usage error (exit 2), never a filesystem path hazard.
    if args.execution_id is not None:
        validate_run_id_or_exit(args.execution_id, parser)

    execution_id = args.execution_id or generate_run_id()
    project_root = Path(args.project_root).resolve()

    handle = dispatch_bg(
        command=command,
        project_root=project_root,
        execution_id=execution_id,
    )

    sys.stdout.write(json.dumps(handle, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# --observe
# ---------------------------------------------------------------------------


def build_observe_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``--observe`` flag."""
    parser = argparse.ArgumentParser(
        prog="skillweave --observe",
        description=(
            "Stream the run-log for an execution to stdout in read-only mode. "
            "No state is mutated."
        ),
    )
    parser.add_argument(
        "--observe",
        metavar="EXECUTION_ID",
        required=True,
        help="The execution ID to observe (returned by --dispatch).",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root (default: current directory).",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Keep tailing the log until the run finishes (like tail -f). "
             "Without this flag, existing content is dumped and the command "
             "exits immediately.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Seconds between poll cycles when --follow is active (default: 0.25).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Maximum seconds to wait in --follow mode before giving up "
             "(default: no timeout).",
    )
    return parser


def _is_sentinel_line(raw_line: str) -> bool:
    """Return True when *raw_line* marks the end of the run.

    The sentinel is a JSON object with ``"event": "run_finished"`` or
    ``"event": "run_error"``.  Plain text log lines are never sentinels.
    """
    stripped = raw_line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        obj = json.loads(stripped)
        return obj.get("event") in ("run_finished", "run_error")
    except (json.JSONDecodeError, AttributeError):
        return False


def observe(
    execution_id: str,
    project_root: Path,
    *,
    follow: bool = False,
    poll_interval: float = 0.25,
    timeout: Optional[float] = None,
    out=None,
) -> int:
    """Stream the run-log for *execution_id* to *out* (default: sys.stdout).

    This function is **read-only**: it never writes to any file or database.

    Args:
        execution_id: The execution ID whose log should be streamed.
        project_root: Root directory of the project.
        follow: If True, keep tailing until a sentinel or *timeout*.
        poll_interval: Seconds between reads in follow mode.
        timeout: Maximum seconds to wait in follow mode.
        out: File-like object to write to (defaults to sys.stdout).

    Returns:
        0 on success, 1 if the log file does not exist.
    """
    if out is None:
        out = sys.stdout

    log_file = _log_path(project_root, execution_id)

    if not log_file.exists():
        sys.stderr.write(
            f"observe: no run-log found for execution_id={execution_id!r}\n"
            f"  expected: {log_file}\n"
        )
        return 1

    start = time.monotonic()

    with open(log_file, "r") as fh:
        # --- Pass 1: dump all content already in the file ------------------
        for line in fh:
            out.write(line)
            if not line.endswith("\n"):
                out.write("\n")
            if hasattr(out, "flush"):
                out.flush()
            if _is_sentinel_line(line):
                return 0

        if not follow:
            return 0

        # --- Pass 2: follow mode (tail -f style) ----------------------------
        while True:
            line = fh.readline()
            if line:
                out.write(line)
                if not line.endswith("\n"):
                    out.write("\n")
                if hasattr(out, "flush"):
                    out.flush()
                if _is_sentinel_line(line):
                    return 0
            else:
                if timeout is not None and (time.monotonic() - start) >= timeout:
                    sys.stderr.write(
                        f"observe: timeout after {timeout}s "
                        f"(execution_id={execution_id!r})\n"
                    )
                    return 1
                time.sleep(poll_interval)

    return 0  # pragma: no cover – unreachable, satisfies type checkers


def main_observe(argv: Optional[Sequence[str]] = None) -> int:
    """Entry-point for the ``--observe`` flag."""
    parser = build_observe_parser()
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()

    return observe(
        execution_id=args.observe,
        project_root=project_root,
        follow=args.follow,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )


# ---------------------------------------------------------------------------
# Standalone entry-point (python -m ...)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main_observe())
