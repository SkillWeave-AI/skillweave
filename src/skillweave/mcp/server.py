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

Authority is server-owned (SW-1510-MCP-AUTH)
--------------------------------------------
A reachable local MCP server is **not** proof of authorization. This module is
the MCP *authorization middleware* (:class:`~skillweave.runtime.authority` is the
producer of :data:`~skillweave.runtime.authority.EXECUTION_ENVELOPE_VERSION`
envelopes). Every protected operation resolves an authenticated
:class:`~skillweave.runtime.authority.MCPPrincipal` from a host-minted session
token: the role is read from the server-owned registry, never from a caller
field, so a caller cannot obtain reviewer rights by passing
``role="reviewer"``. Cross-client run access, arbitrary path/command injection,
duplicate starts and replayed approvals are refused with auditable typed
errors. Local stdio carries an *explicit supervisor identity*; scoped HTTP
applies the authorization controls and never forwards a client token to an
unapproved provider.
"""

import json
import os
import sys

from skillweave.mcp import control
from skillweave.runtime.authority import (
    MCPAuthError,
    MCPPrincipal,
    MCPPrincipalAuthority,
    UnauthenticatedPrincipal,
)

try:
    from mcp.server.fastmcp import FastMCP as _RealFastMCP
except ImportError:  # pragma: no cover - exercised on a host without `mcp`
    _RealFastMCP = None


#: The environment variable naming the *explicit* supervisor principal a local
#: stdio client runs under. There is no anonymous default: a stdio session that
#: does not name its supervisor is refused at the authorization seam.
SUPERVISOR_ENV = "SKILLWEAVE_MCP_SUPERVISOR"

#: Environment variable carrying a client's MCP session token (the credential
#: the authority registry minted). An HTTP request presents the same token.
SESSION_TOKEN_ENV = "SKILLWEAVE_MCP_SESSION_TOKEN"

#: Provider routes this middleware will never forward a client credential to,
#: regardless of what an envelope's ``allowed_routes`` claims. A client's MCP
#: session token is authority over *this server*, not a provider bearer token.
NON_FORWARDABLE_ROUTES = frozenset({"passthrough", "echo", "reflect"})

#: The process-wide authority. Bound exactly once by the host process
#: (:func:`bind_authority`); a client cannot bind or replace it.
_AUTHORITY: "MCPPrincipalAuthority | None" = None


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


# ---------------------------------------------------------------------------
# Server-owned authorization middleware
# ---------------------------------------------------------------------------

def bind_authority(authority: MCPPrincipalAuthority) -> MCPPrincipalAuthority:
    """Bind the process-wide MCP authority. Only a live instance is accepted.

    Returns the bound authority so callers can issue sessions against it. A
    look-alike object without ``serve_live`` is refused: revocation of the seam
    is not something a caller can do.
    """
    global _AUTHORITY
    if authority is None or not callable(getattr(authority, "serve_live", None)):
        raise UnauthenticatedPrincipal(
            "bind_authority requires a live MCPPrincipalAuthority"
        )
    _AUTHORITY = authority
    return authority


def get_authority() -> "MCPPrincipalAuthority | None":
    """The bound MCP authority, or ``None`` when the host has not bound one."""
    return _AUTHORITY


def _require_authority() -> MCPPrincipalAuthority:
    authority = _AUTHORITY
    if authority is None or not callable(getattr(authority, "serve_live", None)):
        raise UnauthenticatedPrincipal(
            "no MCP authority is bound to this server; the host process must "
            "call bind_authority(...) before serving protected operations"
        )
    return authority


def supervisor_identity() -> str:
    """The explicit supervisor principal a local stdio session runs under.

    Read from :data:`SUPERVISOR_ENV`. Empty (or whitespace) means the session
    named no supervisor, which the stdio seam refuses -- a local stdio client
    is *not* implicitly trusted.
    """
    return (os.environ.get(SUPERVISOR_ENV, "") or "").strip()


def require_supervisor_identity() -> str:
    """The stdio supervisor principal, or an explicit refusal when unset."""
    identity = supervisor_identity()
    if not identity:
        raise UnauthenticatedPrincipal(
            f"local stdio requires an explicit supervisor identity; set "
            f"{SUPERVISOR_ENV} to name the supervising principal"
        )
    return identity


def session_token_from_env(environ=None) -> str:
    """The presented MCP session token from the environment (never logged)."""
    env = os.environ if environ is None else environ
    return env.get(SESSION_TOKEN_ENV, "") or ""


def server_default_paths() -> tuple:
    """The server's own configured store paths a principal may always use."""
    return (".skillweave/store.db", ".skillweave/artifacts")


def authenticate_operation(
    session_token: str,
    operation: str,
    *,
    requested_role: str = "",
    run_id: str = "",
    db_path: str = "",
    artifacts_path: str = "",
    command: str = "",
    approval_token: str = "",
    start_scope: str = "",
) -> MCPPrincipal:
    """Resolve and authorize the principal for *operation* (the middleware).

    The single entry every protected MCP operation routes through. It (1)
    authenticates the presented token against the server-owned registry, (2)
    **refuses role escalation** before anything else -- the explicit answer to
    ``role="reviewer"`` being demanded by a caller whose principal is not a
    reviewer, and (3) authorizes the operation, failing closed on cross-client
    access, path/command injection, duplicate starts and replayed approvals.
    """
    authority = _require_authority()
    principal = authority.verify_session(session_token)
    # Role escalation is refused FIRST: a caller asking for rights it does not
    # hold is refused even if it would otherwise pass the operation check.
    authority.refuse_role_escalation(principal, requested_role)
    return authority.authorize(
        session_token,
        operation,
        run_id=run_id,
        db_path=db_path,
        artifacts_path=artifacts_path,
        command=command,
        approval_token=approval_token,
        start_scope=start_scope,
        default_paths=server_default_paths(),
    )


def authorize_provider_forwarding(envelope, route: str) -> None:
    """Refuse forwarding a client credential to an unapproved provider route.

    An ``execution-envelope-v1`` may name ``allowed_routes``; a route outside
    that set -- or a route that only exists to echo/reflect a credential -- is
    a refusal. The client's MCP session token is authority over this server,
    never a provider bearer token, so it is not passed through.
    """
    from skillweave.runtime.authority import MCPAuthError as _MCPAuthError

    allowed = (envelope or {}).get("allowed_routes") or []
    if route in NON_FORWARDABLE_ROUTES:
        raise _MCPAuthError(
            "token_passthrough_refused",
            f"refusing to forward a client credential to route {route!r}",
            action="forward",
            extra={"route": route},
        )
    if allowed and route not in allowed:
        raise _MCPAuthError(
            "unapproved_provider_route",
            f"route {route!r} is not in the envelope's allowed_routes",
            action="forward",
            extra={"route": route, "allowed_routes": list(allowed)},
        )


def _get_store(db_path=".skillweave/store.db"):
    return control._get_store(db_path)


def _default_stream_root(db_path):
    return control.default_stream_root(db_path)


def _get_run_svc(db_path=".skillweave/store.db", artifacts_path=".skillweave/artifacts", stream_root=None):
    return control.get_run_service(db_path, artifacts_path, stream_root)


def _auth_error_payload(exc: MCPAuthError) -> str:
    """A structured, auditable refusal -- never a boolean or bare string."""
    return json.dumps({"status": "refused", **exc.to_dict()})



@mcp.tool()
def create_run(tool: str, model: str, subject_repo: str, subject_commit: str, command: str, db_path: str = ".skillweave/store.db", artifacts_path: str = ".skillweave/artifacts", methodology: str = "ralph", autonomy_policy: str = "", task_id: str = "", attempt: int = 1, output_root: str = "", stream_root: str = "", artifact_evidence: str = "", source_root: str = "", analysis_root: str = "") -> str:
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
def review_run(run_id: str, verdict: str, comment: str, db_path: str = ".skillweave/store.db", role: str = "", session_token: str = "", approval_token: str = "", artifacts_path: str = ".skillweave/artifacts") -> str:
    """Review a run to advance its gate, as an *authenticated* reviewer.

    Authority is server-owned: the reviewer role is read from the host-minted
    session credential, never from the ``role`` argument. A caller that passes
    ``role="reviewer"`` while its authenticated principal is not a reviewer is
    refused (:class:`RoleEscalationRefused`) rather than granted reviewer
    effect. A worker principal cannot review at all; a reviewer/observer may
    review only a run it is bound to (cross-client access is refused); a
    replayed ``approval_token`` is refused. Every refusal is an auditable typed
    record.

    On success the persisted transition records the *authenticated* principal's
    role -- never the caller-supplied string -- so the durable record carries
    who actually reviewed.
    """
    token = session_token or session_token_from_env()
    try:
        principal = authenticate_operation(
            token,
            "review_run",
            requested_role=role,
            run_id=run_id,
            db_path=db_path,
            artifacts_path=artifacts_path,
            approval_token=approval_token,
        )
    except MCPAuthError as exc:
        return _auth_error_payload(exc)

    if verdict.lower() not in ("pass", "fail"):
        return json.dumps({
            "status": "refused",
            "error": f"unknown review verdict {verdict!r}; expected 'pass' or 'fail'",
            "code": "invalid_verdict",
            "run_id": run_id,
        })

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
        role=principal.role,
        stop_reason=stop_reason,
    )
    return json.dumps({
        "status": "reviewed",
        "verdict": verdict,
        "run_id": run_id,
        "reviewer": principal.principal,
        "role": principal.role,
    })


def main() -> None:
    """Serve the command server, or refuse with an explicit unavailable state.

    This is the only entry the module exposes as a *server*. With no ``mcp``
    package it raises :class:`~skillweave.mcp.control.McpDependencyUnavailable`
    (surfaced as a non-zero exit by ``__main__``) rather than silently no-op'ing.

    A local stdio session must run under an *explicit supervisor identity*
    (:data:`SUPERVISOR_ENV`) and the host must have bound an MCP authority: a
    local stdio client is not implicitly trusted and a reachable server is not
    proof of authorization. Both are enforced before the runner is invoked.
    """
    control.require_mcp()  # raises the explicit diagnostic when absent
    require_supervisor_identity()  # local stdio needs a named supervisor
    _require_authority()  # a reachable server is not proof of authorization
    runner = getattr(mcp, "run", None)
    if not callable(runner):
        raise control.McpDependencyUnavailable(control.DEGRADED_REASON)
    runner()


if __name__ == "__main__":
    try:
        main()
    except MCPAuthError as exc:
        print(f"skillweave-mcp refused: {exc.message}", file=sys.stderr)
        raise SystemExit(1)
    except control.McpDependencyUnavailable as exc:
        print(f"skillweave-mcp unavailable: {exc.reason}", file=sys.stderr)
        raise SystemExit(1)
