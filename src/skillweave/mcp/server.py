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

@mcp.tool()
def create_run(tool: str, model: str, subject_repo: str, subject_commit: str, command: str, db_path: str = ".skillweave/store.db", artifacts_path: str = ".skillweave/artifacts", methodology: str = "ralph", autonomy_policy: str = "", task_id: str = "", attempt: int = 1, output_root: str = "", stream_root: str = "") -> str:
    """Create and execute a new SkillWeave run.

    ``methodology`` is the declared methodology binding (default ``ralph``).
    ``assay`` runs the native Assay drill and is never silently downgraded.

    The run executes through the durable streaming entry: its per-attempt
    frames/facts/cursor are persisted under ``stream_root`` (default beside the
    run store). ``output_root`` names the trusted attempt checkout the host
    assigns when a scope-enforced launch is requested.
    """
    svc = _get_run_svc(
        db_path, artifacts_path, stream_root or _default_stream_root(db_path)
    )
    run_id = generate_run_id()
    # SW-1510-BUDGET: Check for an existing owner envelope for this task scope
    # across ALL runs first.  If the host has pre-configured a finite envelope,
    # honour it — never overwrite with unlimited.  Only create the explicit
    # owner-authorized unlimited envelope when no owner envelope exists.
    from skillweave.runtime.reservation import ReservationEnvelope as _ReservationEnvelope
    effective_task_id = task_id or run_id
    scope_id = f"{effective_task_id}/attempt-{attempt}"
    existing = svc.reservation_store.get_envelope_for_task_scope(effective_task_id)
    if existing is not None:
        owner_env = existing
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
def cancel_run(run_id: str, reason: str, db_path: str = ".skillweave/store.db") -> str:
    """Cancel a run. Follows proper transition path."""
    store = _get_store(db_path)
    record = store.get_run(run_id)
    if not record:
        return json.dumps({"error": "Run not found"})
    
    store.transition(
        run_id,
        "cancelled",
        expected_state=record.state,
        expected_version=record.version,
        reason=f"User cancelled: {reason}",
        role="ops",
        stop_reason="cancelled_by_user"
    )
    return json.dumps({"status": "cancelled", "run_id": run_id})

@mcp.tool()
def resume_run(run_id: str, db_path: str = ".skillweave/store.db") -> str:
    """Resume a stopped or fix_retry run. Follows proper transition path."""
    store = _get_store(db_path)
    record = store.get_run(run_id)
    if not record:
        return json.dumps({"error": "Run not found"})

    store.transition(
        run_id,
        "implement",
        expected_state=record.state,
        expected_version=record.version,
        reason="Resumed by user",
        role="ops"
    )
    return json.dumps({"status": "resumed", "run_id": run_id})

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
