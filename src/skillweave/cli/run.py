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
        "--artifact-evidence",
        dest="artifact_evidence",
        default=None,
        help=(
            "Path to a JSON file carrying the native Assay artifact proof "
            "graph(s) (one evidence artifact object or a list). Each artifact "
            "must bind real file/blob/analysis evidence for the source named by "
            "--source-root. SW-1510-VERIFY: omitting this on an assay run is "
            "itself a refusal -- the native PASS is withheld, never bypassed."
        ),
    )
    parser.add_argument(
        "--source-root",
        dest="source_root",
        default=None,
        help=(
            "Read-only source tree the artifact coverage is bound to (real "
            "file content digests + listed files). Required to admit an "
            "artifact: a missing source never becomes a PASS."
        ),
    )
    parser.add_argument(
        "--analysis-root",
        dest="analysis_root",
        default=None,
        help=(
            "Directory holding the per-path analysis artifacts referenced by "
            "the proof graph (resolved at <analysis-root>/<path>). Used to "
            "independently recompute each analysis_digest; a missing artifact "
            "is a refusal."
        ),
    )
    parser.add_argument(
        "--route-proof",
        dest="route_proof",
        default=None,
        help=(
            "Path to a route-proof JSON document that declares the approved "
            "gateway, route set, and tier-to-route binding. When supplied, "
            "egress is restricted to the approved routes (default-deny). "
            "When absent, the legacy unlimited path is used."
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


def _load_artifact_evidence(artifact_evidence: Optional[str]) -> Optional[list]:
    """Load the declared artifact proof graph(s) from a JSON file.

    Accepts either a single evidence-artifact object or a list of them. ``None``
    (no ``--artifact-evidence``) is passed through unchanged, so the drill's own
    omission refusal -- not a CLI-side default -- is what withholds the PASS.
    """
    if not artifact_evidence:
        return None
    with open(artifact_evidence, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("--artifact-evidence must be a JSON object or a list of objects")


def _build_source_resolver(source_root: Optional[str], subject_commit: str):
    """A read-only resolver over ``--source-root`` bound to the subject commit.

    Returns ``None`` when no source root is named: the drill then refuses the
    artifact for want of a source binding rather than fabricating one.
    """
    if not source_root:
        return None
    from skillweave.runtime.verify import FilesystemSourceResolver

    return FilesystemSourceResolver(source_root, commit=subject_commit)


def _build_analysis_reader(analysis_root: Optional[str]):
    """Read a coverage path's analysis artifact bytes from ``--analysis-root``.

    ``None`` when no root is given, so the verifier refuses with a raw
    "no analysis artifact is available" reason instead of inventing bytes.
    """
    if not analysis_root:
        return None
    root = os.path.abspath(analysis_root)

    def _reader(path: str):
        candidate = os.path.join(root, path)
        try:
            with open(candidate, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    return _reader


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
    # installed `skillweave run` entry is bound to the budget guard.
    reservation_store = ReservationStore(args.db_path)
    svc = RunApplicationService(
        store, journal, raw_artifacts, stream_store,
        reservation_store=reservation_store,
    )
    run_id = generate_run_id()

    # SW-1510-ROUTE-LOCK: load the route-proof document when supplied.
    # When a route-proof is loaded, the approved routes become the exclusive
    # set of permitted egress routes (default-deny).  When no route-proof is
    # supplied, the legacy unlimited path is unchanged.
    route_proof = None
    if args.route_proof:
        from skillweave.routing.route_proof import RouteProof
        route_proof = RouteProof.load(args.route_proof)

    # SW-1510-BUDGET: Check for an existing owner envelope for this task scope
    # across ALL runs first.  If the host has pre-configured a finite envelope
    # (e.g. max_requests=1), honour it — never overwrite with unlimited.
    # Only create the explicit owner-authorized unlimited envelope when no
    # owner envelope exists for this task scope.
    task_id = args.task_id or run_id
    scope_id = f"{task_id}/attempt-{args.attempt}"
    existing = reservation_store.get_envelope_for_task_scope(task_id)
    if existing is not None:
        owner_env = existing
    else:
        owner_env = ReservationEnvelope()
        # SW-1510-ROUTE-LOCK: when a route-proof is loaded, set allowed_routes
        # on the envelope to the approved route set (default-deny).  An absent
        # route-proof leaves the envelope unlimited (legacy path).
        if route_proof is not None:
            owner_env = ReservationEnvelope(
                allowed_routes=sorted(route_proof.approved_routes),
            )
        reservation_store.set_envelope(run_id, scope_id, owner_env)

    try:
        # The native streaming entry authenticates the worker at ingress and,
        # when a scope is declared, confines the child to the host-mapped attempt
        # checkout before spawning. With no authority service supplied it is the
        # unchanged legacy path (identity optional), but the output is still
        # durably streamed. Public result fields below are preserved verbatim.
        artifact_evidence = _load_artifact_evidence(args.artifact_evidence)
        source_resolver = _build_source_resolver(args.source_root, args.subject_commit)
        analysis_reader = _build_analysis_reader(args.analysis_root)

        execution = svc.execute_streaming_native(
            command=command,
            run_id=run_id,
            tool=args.tool,
            model=args.model,
            subject_repo=args.subject_repo,
            subject_commit=args.subject_commit,
            methodology=args.methodology,
            autonomy_policy=args.autonomy_policy,
            task_id=task_id,
            attempt=args.attempt,
            output_root=args.output_root,
            reservation_envelope=owner_env,
            artifact_evidence=artifact_evidence,
            source_resolver=source_resolver,
            analysis_reader=analysis_reader,
            route_proof=route_proof,
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

        # SW-1510-CLI criterion 3: a native Assay PASS withheld by a *missing
        # source binding* (or any artifact-semantics refusal) is a meaningful
        # non-success, not a bare rc 0. The full result JSON is still emitted so
        # automation can read the phases and the exact refusal reasons, but the
        # exit code is nonzero so a wrapper cannot read a withheld PASS as
        # success. This is deliberately scoped to the Assay methodology: a
        # non-Assay run has no artifact-semantics block and keeps its rc 0.
        assay_block = result_dict.get("assay")
        if isinstance(assay_block, dict) and assay_block.get("outcome") == "hold":
            sys.stdout.write(json.dumps(result_dict, sort_keys=True) + "\n")
            reason = assay_block.get("reason") or "artifact-specific semantics refused"
            sys.stderr.write(
                json.dumps(
                    {
                        "error": f"assay native PASS withheld: {reason}",
                        "run_id": execution["run_id"],
                        "outcome": "hold",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            return 1

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
