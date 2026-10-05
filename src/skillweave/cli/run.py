"""The thin ``skillweave run`` command-line surface.

This module parses arguments and delegates execution to the Run Application Service.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from skillweave.dispatch.application import generate_run_id

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillweave run",
        description="Execute a single authoritative run command.",
    )
    parser.add_argument(
        "--tool",
        required=True,
        help="The tool being executed",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="The model identifier",
    )
    parser.add_argument(
        "--subject-repo",
        required=True,
        help="The repository the run operates on",
    )
    parser.add_argument(
        "--subject-commit",
        required=True,
        help="The exact commit SHA the run operates on",
    )
    parser.add_argument(
        "--db-path",
        default=":memory:",
        help="Path to the SQLite store (default: :memory:)",
    )
    parser.add_argument(
        "--artifacts-path",
        default=".skillweave/artifacts",
        help="Path to the raw artifact store directory",
    )
    parser.add_argument(
        "--lane",
        help="The lane identifier",
    )
    parser.add_argument(
        "--methodology",
        default="ralph",
        choices=["ralph", "rex", "gauntlet", "assay"],
        help=(
            "Declared methodology binding (default: ralph). 'assay' runs the "
            "native Assay drill and is never silently downgraded to ralph."
        ),
    )
    parser.add_argument(
        "--autonomy-policy",
        dest="autonomy_policy",
        default="",
        help=(
            "Assay autonomy policy (conservative|moderate|unicorn); "
            "empty uses the default (unicorn)"
        ),
    )
    parser.add_argument(
        "--task-id",
        dest="task_id",
        default=None,
        help="Task identifier for the run/task/attempt triple (default: run id)",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Attempt number for the run/task/attempt triple (default: 1)",
    )
    parser.add_argument(
        "--rework",
        action="store_true",
        help="Draft rework briefs from failed gates",
    )
    parser.add_argument(
        "run_argv",
        nargs=argparse.REMAINDER,
        help="The actual command to run (use -- to separate)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # NOTE: the REMAINDER positional is deliberately *not* named ``command``.
    # ``skillweave.cli.main`` builds the router with
    # ``add_subparsers(dest="command")`` and mounts this parser as a *parent*;
    # a positional also named ``command`` would overwrite the router's
    # subcommand value with ``[]``, so ``args.command == "run"`` could never be
    # true and the installed ``skillweave run`` entry would print help instead
    # of dispatching here.
    command = args.run_argv
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        parser.error("A command to run is required.")

    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runsvc.service import RunApplicationService

    store = SQLiteRunStore(args.db_path)
    journal = EventJournal(args.db_path)
    raw_artifacts = RawArtifactStore(args.artifacts_path)

    svc = RunApplicationService(store, journal, raw_artifacts)
    run_id = generate_run_id()

    try:
        execution = svc.execute(
            command=command,
            run_id=run_id,
            tool=args.tool,
            model=args.model,
            subject_repo=args.subject_repo,
            subject_commit=args.subject_commit,
            methodology=args.methodology,
            autonomy_policy=args.autonomy_policy,
            task_id=args.task_id,
            attempt=args.attempt,
        )

        result_dict = {
            "run_id": execution.run.run_id,
            "state": execution.run.state,
            "gate_state": execution.gate_state,
            "raw_digest": execution.raw_digest,
            "verification": execution.verification,
            "methodology": execution.methodology,
        }
        if execution.assay is not None:
            result_dict["assay"] = execution.assay

        if getattr(args, "rework", False) and execution.gate_state == "fail":
            # Unify on the structured brief artifact. run.py delegates to the
            # same writer as `skillweave rework` so exactly one brief (.md)
            # exists rather than a divergent inline string.
            from skillweave.rework import ReworkBriefWriter, ReworkError

            lane = getattr(args, "lane", "") or ""
            brief_path: Optional[str] = None
            if lane:
                try:
                    writer = ReworkBriefWriter()
                    brief_path = str(writer.write(lane))
                except ReworkError as exc:
                    brief_path = f"<not written: {exc}>"
            result_dict["rework_brief"] = {
                "lane": lane,
                "run_id": execution.run.run_id,
                "gate_state": execution.gate_state,
                "raw_digest": execution.raw_digest,
                "verification": execution.verification,
                "brief_path": brief_path,
            }

        sys.stdout.write(json.dumps(result_dict, sort_keys=True) + "\n")
        return 0

    except Exception as exc:
        error_dict = {
            "error": str(exc),
            "run_id": run_id,
        }
        sys.stderr.write(json.dumps(error_dict, sort_keys=True) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
