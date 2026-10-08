"""SkillWeave command server (the six legacy tools) over MCP.

The durable semantics live in the shared service (``skillweave.runsvc``); this
module is the *transport* the command tools are bound to. Each tool delegates to
:mod:`skillweave.mcp.control`, which composes the same
:class:`~skillweave.runsvc.service.RunApplicationService` the CLI drives, so the
CLI and MCP cannot disagree about what ran.

Dependency honesty (SW-1510-MCP-CONTROL criterion 2)
----------------------------------------------------
A working server needs the real ``mcp`` package. When it is absent this module
still imports (so the tool functions remain importable/callable for in-process
callers), but the server cannot advertise itself as working: the placeholder
``mcp`` object carries an explicit degradation reason, exposes ``can_serve ==
False``, and its ``run`` raises :class:`McpDependencyUnavailable`. Running this
module as ``__main__`` with no ``mcp`` package exits non-zero with that
diagnostic instead of silently no-op'ing.
"""

import json
import sys

from skillweave.mcp import control

try:
    from mcp.server.fastmcp import FastMCP as _RealFastMCP
except ImportError:  # pragma: no cover - exercised on a host without `mcp`
    _RealFastMCP = None


class _DegradedFastMCP:
    """A tracked stand-in used *only* when the ``mcp`` package is missing.

    It records the decorated tool functions (so the module's tool registry stays
    inspectable) but is explicitly *not* a server: it reports a degradation
    reason and refuses to run.
    """

    can_serve = False
    degradation_reason = control.DEGRADED_REASON

    def __init__(self, name):
        self.name = name
        self.tools = []

    def tool(self):
        def decorator(func):
            self.tools.append(func)
            return func

        return decorator

    def run(self, *args, **kwargs):
        raise control.McpDependencyUnavailable()


if _RealFastMCP is not None:
    mcp = _RealFastMCP(control.SERVER_NAME)
else:
    mcp = _DegradedFastMCP(control.SERVER_NAME)


def _get_store(db_path=".skillweave/store.db"):
    return control._get_store(db_path)


def _default_stream_root(db_path):
    return control.default_stream_root(db_path)


def _get_run_svc(db_path=".skillweave/store.db", artifacts_path=".skillweave/artifacts", stream_root=None):
    return control.get_run_service(db_path, artifacts_path, stream_root)


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
    payload = control.create_run_payload(
        route_proof=route_proof,
        tool=tool,
        model=model,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        command=command,
        db_path=db_path,
        artifacts_path=artifacts_path,
        methodology=methodology,
        autonomy_policy=autonomy_policy,
        task_id=task_id,
        attempt=attempt,
        output_root=output_root,
        stream_root=stream_root,
        artifact_evidence=artifact_evidence,
        source_root=source_root,
        analysis_root=analysis_root,
    )
    return json.dumps(payload)


@mcp.tool()
def status_run(run_id: str, db_path: str = ".skillweave/store.db") -> str:
    """Get the status of a specific run.

    Includes the methodology/attempt and (for Assay) the ordered phases and
    terminal outcome, read from the same snapshot the CLI and replay use.
    """
    result = control.control_status(run_id, db_path=db_path)
    if result.get("error") == "Run not found":
        return json.dumps({"error": "Run not found"})
    return json.dumps(result)


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
    result = control.control_cancel(run_id, reason=reason, db_path=db_path)
    if result.get("error") == "Run not found":
        return json.dumps({"error": "Run not found"})
    return json.dumps(result)


@mcp.tool()
def resume_run(run_id: str, db_path: str = ".skillweave/store.db", command: str = "") -> str:
    """Resume a run by launching a NEW identified attempt.

    With a *command*, this launches a real new attempt (new process, new
    recorded identity) through the native service — it never reports
    "restarted" from a database transition alone. Without a command it
    reconciles stored intents and returns the run's *actual* state with
    ``restarted: false``.
    """
    result = control.control_resume(run_id, command=command, db_path=db_path)
    if result.get("error") == "Run not found":
        return json.dumps({"error": "Run not found"})
    return json.dumps(result)


@mcp.tool()
def get_evidence(run_id: str, db_path: str = ".skillweave/store.db") -> str:
    """Get evidence/journal for a run."""
    result = control.control_evidence(run_id, db_path=db_path)
    if "error" in result:
        # A storage refusal is a structured record, not a key error.
        return json.dumps(result)
    return json.dumps({"events": result["events"]})


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
        stop_reason=stop_reason,
    )
    return json.dumps({"status": "reviewed", "verdict": verdict, "run_id": run_id})


def main() -> None:
    """Serve the command server, or refuse with an explicit unavailable state.

    This is the only entry the module exposes as a *server*. With no ``mcp``
    package it raises :class:`~skillweave.mcp.control.McpDependencyUnavailable`
    (surfaced as a non-zero exit by ``__main__``) rather than silently no-op'ing.
    """
    control.require_mcp()  # raises the explicit diagnostic when absent
    runner = getattr(mcp, "run", None)
    if not callable(runner):
        raise control.McpDependencyUnavailable(control.DEGRADED_REASON)
    runner()


if __name__ == "__main__":
    try:
        main()
    except control.McpDependencyUnavailable as exc:
        print(f"skillweave-mcp unavailable: {exc.reason}", file=sys.stderr)
        raise SystemExit(1)
