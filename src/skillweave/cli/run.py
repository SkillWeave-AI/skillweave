"""The thin ``skillweave run`` command-line surface.

This module parses arguments and delegates execution to the Run Application Service.
"""

from __future__ import annotations

import argparse
import json
import os
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
        "--stream-root",
        dest="stream_root",
        default=None,
        help=(
            "Directory for the durable per-attempt stream evidence "
            "(frames.jsonl/facts.jsonl/cursor.json). Defaults to "
            "<db dir>/streams so the native entry always persists stream "
            "evidence rather than leaving it in RAM."
        ),
    )
    parser.add_argument(
        "--output-root",
        dest="output_root",
        default=None,
        help=(
            "The trusted attempt checkout/output root the host assigns to this "
            "attempt; scope-enforced launches confine mutations to it. Required "
            "when a declared write scope is supplied."
        ),
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


def _default_stream_root(db_path: str) -> str:
    """The durable stream root used when the caller does not name one.

    A disk-backed run store keeps its stream evidence beside it
    (``<config>/streams``), so the default entry always persists frames/facts/
    cursor rather than running with a memory-only collector. An in-memory
    (``:memory:``) store — the legacy single-shot default — gets a private
    per-process directory so it still produces real durable evidence files
    without ever writing into the current working tree unexpectedly.
    """
    if db_path in ("", ":memory:"):
        import tempfile

        return os.path.join(tempfile.mkdtemp(prefix="skillweave-stream-"), "streams")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    return os.path.join(db_dir, "streams")


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
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
    from skillweave.runsvc.service import RunApplicationService

    store = SQLiteRunStore(args.db_path)
    journal = EventJournal(args.db_path)
    raw_artifacts = RawArtifactStore(args.artifacts_path)

    # The authoritative native entry drives the durable streaming collector, so
    # `skillweave run` persists per-attempt frames/facts/cursor to disk rather
    # than leaving stream evidence in RAM or on the terminal. The default root
    # sits beside the run store, keeping one durable location per run store; a
    # caller can override it explicitly. This is the seam that makes the
    # installed console entry produce real stream evidence (SW-1510-STREAM).
    stream_root = args.stream_root or _default_stream_root(args.db_path)
    stream_store = DurableStreamStore(root=stream_root)

    # SW-1510-BUDGET: wire the atomic reservation store explicitly so the
    # installed `skillweave run` entry is bound to the budget guard.  The owner
    # envelope is set with explicit unlimited (all None = authorized unlimited),
    # so the ingress is not silently unguarded: every run acquires a reservation
    # slot, cumulative usage is tracked, and a finite owner envelope can be
    # pre-configured by the host to cap usage.
    reservation_store = ReservationStore(args.db_path)
    svc = RunApplicationService(
        store, journal, raw_artifacts, stream_store,
        reservation_store=reservation_store,
    )
    run_id = generate_run_id()

    # Set the explicit owner-authorized unlimited envelope so the reservation
    # store has a persisted owner policy.  All-None means "no hard cumulative
    # caps" — the authorized owner-unlimited mode.  A host that wants finite
    # bounds pre-configures the envelope before calling this entry.
    scope_id = f"{args.task_id or run_id}/attempt-{args.attempt}"
    owner_env = ReservationEnvelope()
    reservation_store.set_envelope(run_id, scope_id, owner_env)

    try:
        # The native streaming entry authenticates the worker at ingress and,
        # when a scope is declared, confines the child to the host-mapped attempt
        # checkout before spawning. With no authority service supplied it is the
        # unchanged legacy path (identity optional), but the output is still
        # durably streamed. Public result fields below are preserved verbatim.
        execution = svc.execute_streaming_native(
            command=command,
            run_id=run_id,
            tool=args.tool,
            model=args.model,
            subject_repo=args.subject_repo,
            subject_commit=args.subject_commit,
            methodology=args.methodology,
            autonomy_policy=args.autonomy_policy,
            task_id=args.task_id or run_id,
            attempt=args.attempt,
            output_root=args.output_root,
            reservation_envelope=owner_env,
        )

        result_obj = execution["result"]
        receipt = execution["receipt"]
        result_dict = {
            "run_id": execution["run_id"],
            "state": store.get_run(run_id).state,
            "gate_state": execution["gate_state"],
            "raw_digest": execution["raw_digest"],
            "verification": execution["verification"],
            "methodology": execution["methodology"],
            "stream": {
                "durable": execution.get("durable", True),
                "cursor": stream_store.cursor,
                "stdout_digest": result_obj.stdout_digest,
                "stderr_digest": result_obj.stderr_digest,
            },
        }
        # Preserve the public result-type surface the bounded path returned: the
        # producer receipt object and the exact raw bytes, so a caller that read
        # `execution.receipt`/`execution.raw_bytes` is not broken by the switch.
        result_dict["receipt"] = {
            "artifact_id": receipt.artifact_id,
            "sha256": receipt.sha256,
            "evidence_type": getattr(receipt, "evidence_type", None),
            "purpose": getattr(receipt, "purpose", None),
        }
        if "assay" in execution:
            result_dict["assay"] = execution["assay"]

        if getattr(args, "rework", False) and execution["gate_state"] == "fail":
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
                "run_id": execution["run_id"],
                "gate_state": execution["gate_state"],
                "raw_digest": execution["raw_digest"],
                "verification": execution["verification"],
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
