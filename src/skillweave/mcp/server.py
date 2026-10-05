import json
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

def _get_run_svc(db_path=".skillweave/store.db", artifacts_path=".skillweave/artifacts"):
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runsvc.service import RunApplicationService
    store = _get_store(db_path)
    journal = EventJournal(db_path)
    raw_artifacts = RawArtifactStore(artifacts_path)
    return RunApplicationService(store, journal, raw_artifacts)

@mcp.tool()
def create_run(tool: str, model: str, subject_repo: str, subject_commit: str, command: str, db_path: str = ".skillweave/store.db", artifacts_path: str = ".skillweave/artifacts", methodology: str = "ralph", autonomy_policy: str = "", task_id: str = "", attempt: int = 1) -> str:
    """Create and execute a new SkillWeave run.

    ``methodology`` is the declared methodology binding (default ``ralph``).
    ``assay`` runs the native Assay drill and is never silently downgraded.
    """
    svc = _get_run_svc(db_path, artifacts_path)
    run_id = generate_run_id()

    cmd_list = shlex.split(command)
    execution = svc.execute(
        command=cmd_list,
        run_id=run_id,
        tool=tool,
        model=model,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        methodology=methodology,
        autonomy_policy=autonomy_policy,
        task_id=task_id or None,
        attempt=attempt,
    )

    payload = {
        "run_id": execution.run.run_id,
        "state": execution.run.state,
        "gate_state": execution.gate_state,
        "raw_digest": execution.raw_digest,
        "methodology": execution.methodology,
    }
    if execution.assay is not None:
        payload["assay"] = execution.assay
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
