import json
import os
import shlex
import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    class FastMCP:
        def __init__(self, name):
            self.name = name
            self.tools = []
        def tool(self):
            def decorator(func):
                self.tools.append(func)
                return func
            return decorator

from skillweave.dispatch.application import generate_run_id

mcp = FastMCP("SkillWeave")

def _get_store(db_path=".skillweave/store.db"):
    from skillweave.runtime.store import SQLiteRunStore
    return SQLiteRunStore(db_path)

def _default_stream_root(db_path):
    """The durable stream root used when the caller does not name one.

    Keep the stream evidence for a disk-backed run store beside it
    (``<db dir>/streams``); an in-memory store gets a private per-process
    directory. Either way the MCP entry persists real frames/facts/cursor
    rather than a memory-only collector.
    """
    if db_path in ("", ":memory:"):
        import tempfile
        return os.path.join(tempfile.mkdtemp(prefix="skillweave-stream-"), "streams")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    return os.path.join(db_dir, "streams")

def _get_run_svc(db_path=".skillweave/store.db", artifacts_path=".skillweave/artifacts", stream_root=None):
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.runtime.reservation import ReservationStore
    from skillweave.runsvc.service import RunApplicationService
    store = _get_store(db_path)
    journal = EventJournal(db_path)
    raw_artifacts = RawArtifactStore(artifacts_path)
    # The MCP surface drives the same durable streaming collector as the CLI, so
    # the two native entries persist identical per-attempt stream evidence. A
    # read-only surface (status/replay) that names no stream root gets the
    # memory-only store and therefore creates no files as a side effect.
    stream_store = DurableStreamStore(root=stream_root) if stream_root else None
    # SW-1510-BUDGET: wire the atomic reservation store explicitly so the MCP
    # entry is bound to the budget guard.  Every MCP create_run acquires a
    # reservation slot; cumulative usage is tracked.
    reservation_store = ReservationStore(db_path)
    return RunApplicationService(
        store, journal, raw_artifacts, stream_store,
        reservation_store=reservation_store,
    )

def _artifact_evidence_list(artifact_evidence: str):
    """Parse the MCP-supplied artifact proof graph(s); ``None`` when absent."""
    if not artifact_evidence:
        return None
    payload = json.loads(artifact_evidence)
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("artifact_evidence must be a JSON object or a list of objects")


def _source_binding(source_root: str, subject_commit: str):
    """A read-only source resolver over ``source_root`` (``None`` when absent)."""
    if not source_root:
        return None
    from skillweave.runtime.verify import FilesystemSourceResolver
    return FilesystemSourceResolver(source_root, commit=subject_commit)


def _analysis_binding(analysis_root: str):
    """Read coverage-path analysis bytes from ``analysis_root`` (``None``)."""
    if not analysis_root:
        return None
    root = os.path.abspath(analysis_root)

    def _reader(path):
        try:
            with open(os.path.join(root, path), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    return _reader


@mcp.tool()
def create_run(tool: str, model: str, subject_repo: str, subject_commit: str, command: str, db_path: str = ".skillweave/store.db", artifacts_path: str = ".skillweave/artifacts", methodology: str = "ralph", autonomy_policy: str = "", task_id: str = "", attempt: int = 1, output_root: str = "", stream_root: str = "", artifact_evidence: str = "", source_root: str = "", analysis_root: str = "", route_proof: str = "") -> str:
    """Create and execute a new SkillWeave run.

    ``methodology`` is the declared methodology binding (default ``ralph``).
    ``assay`` runs the native Assay drill and is never silently downgraded.

    The run executes through the durable streaming entry: its per-attempt
    frames/facts/cursor are persisted under ``stream_root`` (default beside the
    run store). ``output_root`` names the trusted attempt checkout the host
    assigns when a scope-enforced launch is requested.

    SW-1510-VERIFY: ``artifact_evidence`` is the JSON proof graph the native
    Assay PASS is verified against, ``source_root`` the read-only source it is
    bound to and ``analysis_root`` the per-path analysis artifacts. Omitting
    them on an assay run is itself a refusal -- the native PASS is withheld,
    never bypassed by omission.
    """
    svc = _get_run_svc(
        db_path, artifacts_path, stream_root or _default_stream_root(db_path)
    )
    run_id = generate_run_id()
    # SW-1510-ROUTE-LOCK: load the route-proof document when supplied.
    # When a route-proof is loaded, the approved routes become the exclusive
    # set of permitted egress routes (default-deny).
    route_proof_obj = None
    if route_proof:
        from skillweave.routing.route_proof import RouteProof as _RouteProof
        route_proof_obj = _RouteProof.load(route_proof)

    from skillweave.runtime.reservation import ReservationEnvelope as _ReservationEnvelope
    effective_task_id = task_id or run_id
    scope_id = f"{effective_task_id}/attempt-{attempt}"
    existing = svc.reservation_store.get_envelope_for_task_scope(effective_task_id)
    if existing is not None:
        owner_env = existing
    else:
        # SW-1510-ROUTE-LOCK: when a route-proof is loaded, set allowed_routes
        # on the envelope to the approved route set (default-deny).
        if route_proof_obj is not None:
            owner_env = _ReservationEnvelope(
                allowed_routes=sorted(route_proof_obj.approved_routes),
            )
        else:
            owner_env = _ReservationEnvelope()
        svc.reservation_store.set_envelope(run_id, scope_id, owner_env)

    cmd_list = shlex.split(command)
    execution = svc.execute_streaming_native(
        command=cmd_list,
        run_id=run_id,
        tool=tool,
        model=model,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        methodology=methodology,
        autonomy_policy=autonomy_policy,
        task_id=effective_task_id,
        attempt=attempt,
        output_root=output_root or None,
        reservation_envelope=owner_env,
        artifact_evidence=_artifact_evidence_list(artifact_evidence),
        source_resolver=_source_binding(source_root, subject_commit),
        analysis_reader=_analysis_binding(analysis_root),
    )

    result_obj = execution["result"]
    payload = {
        "run_id": execution["run_id"],
        "state": svc.store.get_run(run_id).state,
        "gate_state": execution["gate_state"],
        "raw_digest": execution["raw_digest"],
        "methodology": execution["methodology"],
        "stream": {
            "durable": execution.get("durable", True),
            "cursor": svc.stream_store.cursor,
            "stdout_digest": result_obj.stdout_digest,
            "stderr_digest": result_obj.stderr_digest,
        },
    }
    if "assay" in execution:
        payload["assay"] = execution["assay"]
    return json.dumps(payload)

@mcp.tool()
def status_run(run_id: str, db_path: str = ".skillweave/store.db") -> str:
    """Get the status of a specific run.

    Includes the methodology/attempt and (for Assay) the ordered phases and
    terminal outcome, read from the same snapshot the CLI and replay use.
    """
    svc = _get_run_svc(db_path)
    snapshot = svc.run_snapshot(run_id)
    if snapshot is None:
        return json.dumps({"error": "Run not found"})
    return json.dumps(snapshot)

@mcp.tool()
def cancel_run(run_id: str, reason: str = "operator_halt", db_path: str = ".skillweave/store.db") -> str:
    """Cancel a run through the native lifecycle entry.

    Never a bare database transition: the service re-verifies the recorded
    process identity against the live OS facts before signalling, stops the
    owned process group, and reports the real outcome. A recycled PID, a
    stale command or a foreign worker is refused (nothing is signalled) and
    an unobservable process is reported as ``unknown_inflight`` — the tool
    never claims a termination it did not perform.
    """
    from skillweave.runtime.errors import ProcessIdentityError, StoreError

    svc = _get_run_svc(db_path)
    if svc.store.get_run(run_id) is None:
        return json.dumps({"error": "Run not found"})
    try:
        result = svc.cancel_process(run_id, reason=reason)
    except ProcessIdentityError as exc:
        return json.dumps({
            "error": "process identity not verified; cancellation refused",
            "run_id": run_id,
            "reason": exc.reason,
            "verification": exc.verification,
        })
    except StoreError as exc:
        return json.dumps({"error": str(exc), "run_id": run_id})
    return json.dumps({"status": "cancel_requested", **result})

@mcp.tool()
def resume_run(run_id: str, db_path: str = ".skillweave/store.db", command: str = "") -> str:
    """Resume a run by launching a NEW identified attempt.

    With a *command*, this launches a real new attempt (new process, new
    recorded identity) through the native service — it never reports
    "restarted" from a database transition alone. Without a command it
    reconciles stored intents and returns the run's *actual* state with
    ``restarted: false``.
    """
    from skillweave.runtime.errors import StoreError

    svc = _get_run_svc(db_path)
    if not command:
        snapshot = svc.resume_run(run_id)
        if snapshot is None:
            return json.dumps({"error": "Run not found"})
        return json.dumps({
            "status": "reconciled",
            "restarted": False,
            "note": "no command supplied; no new attempt launched",
            **snapshot,
        })

    cmd_list = shlex.split(command)
    try:
        result = svc.restart_attempt(run_id, cmd_list)
    except StoreError as exc:
        return json.dumps({"error": str(exc), "run_id": run_id})
    return json.dumps({
        "status": "restarted",
        "restart_kind": "new_identified_attempt",
        **result,
    })

@mcp.tool()
def get_evidence(run_id: str, db_path: str = ".skillweave/store.db") -> str:
    """Get evidence/journal for a run."""
    from skillweave.runtime.journal import EventJournal
    journal = EventJournal(db_path)
    events = journal.get_events(run_id)
    # Ensure events are dicts if they are dataclasses
    try:
        events = [e.to_dict() if hasattr(e, "to_dict") else e for e in events]
    except Exception:
        pass
    return json.dumps({"events": events})

@mcp.tool()
def review_run(run_id: str, verdict: str, comment: str, db_path: str = ".skillweave/store.db") -> str:
    """Review a run to advance its gate. Follows proper transition path."""
    store = _get_store(db_path)
    record = store.get_run(run_id)
    if not record:
        return json.dumps({"error": "Run not found"})
    
    target_state = "advance_or_stop"
    stop_reason = None
    if verdict.lower() != "pass":
        stop_reason = f"rejected: {verdict}"

    store.transition(
        run_id,
        target_state,
        expected_state=record.state,
        expected_version=record.version,
        reason=f"Review verdict {verdict}: {comment}",
        role="reviewer",
        stop_reason=stop_reason
    )
    return json.dumps({"status": "reviewed", "verdict": verdict, "run_id": run_id})

if __name__ == "__main__":
    if hasattr(mcp, "run"):
        mcp.run()
