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
from typing import Sequence

from skillweave.mcp import control

# GLE-020: ``skillweave.runtime`` is an *optional* subpackage, so this module
# must not top-level import it (that would drag it into the eager closure). The
# authority types the middleware uses are resolved lazily via PEP 562 -- the
# same deferral ``skillweave/__init__.py`` uses -- so importing this module
# never forces the optional subpackage while ``server.MCPAuthError`` (and the
# other names) remain reachable exactly as before.
_AUTHORITY_NAMES = frozenset({
    "EXECUTION_ENVELOPE_MCP_EXTENSION",
    "MCPAuthError",
    "MCPPrincipal",
    "MCPPrincipalAuthority",
    "UnauthenticatedPrincipal",
})


def __getattr__(name: str):
    if name in _AUTHORITY_NAMES:
        from skillweave.runtime import authority as _authority

        return getattr(_authority, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


try:
    from mcp.server.fastmcp import FastMCP as _RealFastMCP
except ImportError:  # pragma: no cover - exercised on a host without `mcp`
    _RealFastMCP = None


#: The environment variable naming the *explicit* supervisor principal a local
#: stdio client runs under. There is no anonymous default: a stdio session that
#: does not name its supervisor is refused at the authorization seam.
SUPERVISOR_ENV = "SKILLWEAVE_MCP_SUPERVISOR"

#: The documented non-zero exit code this module uses when it refuses to serve:
#: the ``mcp`` dependency is absent, or a local stdio session named no explicit
#: supervisor identity. The one-line diagnostic goes to stderr; the process
#: exits with this code instead of raising a traceback. Operators may script
#: against it.
EXIT_REFUSED = 3

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

#: The session token the *production* stdio startup minted for this process
#: (:func:`startup_and_bind_stdio`). It is host memory, never the wire: a
#: stdio client's protected calls resolve to this principal when it presents
#: no explicit credential. An HTTP client still presents its own token.
_READY_SESSION_TOKEN: "str | None" = None


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
    mcp = control._set_package_version(_RealFastMCP(control.SERVER_NAME))
else:
    mcp = _DegradedFastMCP(control.SERVER_NAME)


# ---------------------------------------------------------------------------
# Server-owned authorization middleware
# ---------------------------------------------------------------------------

def bind_authority(authority: "MCPPrincipalAuthority") -> "MCPPrincipalAuthority":
    """Bind the process-wide MCP authority. Only a live instance is accepted.

    Returns the bound authority so callers can issue sessions against it. A
    look-alike object without ``serve_live`` is refused: revocation of the seam
    is not something a caller can do.
    """
    global _AUTHORITY
    if authority is None or not callable(getattr(authority, "serve_live", None)):
        raise _authority_module().UnauthenticatedPrincipal(
            "bind_authority requires a live MCPPrincipalAuthority"
        )
    _AUTHORITY = authority
    return authority


def get_authority() -> "MCPPrincipalAuthority | None":
    """The bound MCP authority, or ``None`` when the host has not bound one."""
    return _AUTHORITY


def _authority_module():
    """The optional ``skillweave.runtime.authority`` module (lazy, GLE-020)."""
    from skillweave.runtime import authority as _authority

    return _authority


def _bound_authority() -> "MCPPrincipalAuthority | None":
    """The live authority bound to this process, or ``None``.

    A bound object qualifies only when it carries ``serve_live``; a look-alike
    without it is treated as *not bound* rather than trusted.
    """
    authority = _AUTHORITY
    if authority is None or not callable(getattr(authority, "serve_live", None)):
        return None
    return authority


def _current_http_principal():
    """The authenticated principal of the current HTTP request, or ``None``.

    The HTTP request guard (:mod:`skillweave.mcp.control`) sets this per
    request; over HTTP a protected tool call resolves its principal here, so
    the per-tool gating sees the same authenticated principal it does over
    stdio. Resolved lazily so importing this module never forces the optional
    subpackage.
    """
    try:
        return control.current_http_principal()
    except Exception:  # noqa: BLE001 - absence is "no HTTP principal"
        return None


def _require_authority() -> "MCPPrincipalAuthority":
    authority = _bound_authority()
    if authority is None:
        raise _authority_module().UnauthenticatedPrincipal(
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
        raise _authority_module().UnauthenticatedPrincipal(
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


def start_scope_for(*, task_id: str, attempt: int, run_id: str = "") -> str:
    """The start scope a create/start operation is bound to.

    Mirrors the shared service's own scope id
    (``control.create_run_payload``: ``f"{task_id or run_id}/attempt-{attempt}"``)
    so the authorization seam refuses a *duplicate start of the same attempt*
    at the real tool even though this surface mints its own run id.
    """
    effective = task_id or run_id
    return f"{effective}/attempt-{attempt}"


def issue_harness_session_grant(
    *,
    authority: "MCPPrincipalAuthority",
    role: str,
    client_id: str,
    transport: str,
    run_id: str,
    service: "object",
    task_id: str = "",
    attempt: int = 0,
    allowed_roots: Sequence[str] = (),
    allowed_argv0: Sequence[str] = (),
    allowed_routes: Sequence[str] = (),
    ttl_seconds: "int | None" = None,
) -> "object":
    """Issue a ``harness-mcp-session-grant-v1`` bound to run/task/attempt.

    The **server** mints the token and the caller supplies no role string that
    is trusted -- ``role`` is validated against the server-owned registry by
    :meth:`MCPPrincipalAuthority.issue_session` (an unknown role is refused).
    The returned grant is the one the dispatch adapter provisions into the
    *actual* child launch, so a child reaches this same authority/state service
    rather than an isolated harness profile.

    ``service`` is the :class:`skillweave.mcp.bootstrap.HarnessProcessService`
    describing the one logical durable process service; it is recorded by
    identity so a reconnect or a stdio admission can be refused when it lands
    on a different store.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    token = authority.issue_session(
        role=role,
        client_id=client_id,
        transport=transport,
        run_ids=(run_id,) if run_id else (),
        allowed_roots=tuple(allowed_roots),
        allowed_argv0=tuple(allowed_argv0),
        allowed_routes=tuple(allowed_routes),
        ttl_seconds=ttl_seconds,
    )
    return _bootstrap.HarnessMcpSessionGrant(
        schema_id=_bootstrap.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
        schema_version=_bootstrap.HARNESS_MCP_SESSION_GRANT_SCHEMA_VERSION,
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        transport=transport,
        session_token=token,
        service=service,
        start_scope=start_scope_for(task_id=task_id, attempt=attempt, run_id=run_id),
        role=role,
    )


def admit_stdio_same_authority(
    grant: "object", local_service: "object"
) -> None:
    """Admit a local stdio harness only when it reaches the same service.

    Criterion 3: a stdio path is admitted only if it demonstrably reaches the
    same authority/state service. An independently-initialized per-harness
    controller or an isolated default store is refused by identity, so a stdio
    harness can never operate on a second, silently-diverging store.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    _bootstrap.refuse_isolated_controller(grant.service, local_service)


def resolve_session_token(presented: str = "") -> str:
    """The session credential a protected call runs under.

    A client may present a token in the call, in :data:`SESSION_TOKEN_ENV`, or
    -- over HTTP -- on the request's ``Authorization`` header, which the
    request guard resolves for the call. When none is present the token the
    *production* stdio startup minted (:data:`_READY_SESSION_TOKEN`) resolves
    the host-owned supervisor principal; a local stdio client cannot mint it,
    only the host binding did.
    """
    return (
        presented
        or session_token_from_env()
        or _http_request_token()
        or (_READY_SESSION_TOKEN or "")
    )


def _http_request_token() -> str:
    """The current HTTP request's bearer token, or ``""`` outside a request."""
    try:
        return control.current_http_token()
    except Exception:  # noqa: BLE001 - absence is "no HTTP credential"
        return ""


def gate_operation(
    operation: str,
    presented_token: str = "",
    *,
    requested_role: str = "",
    run_id: str = "",
    db_path: str = "",
    artifacts_path: str = "",
    command: str = "",
    approval_token: str = "",
    start_scope: str = "",
    provider_route: str = "",
):
    """Authorize *operation* at the tool boundary (the controller's gate seam).

    Returns ``(principal, None)`` when the call is admitted -- the caller's
    ``db_path``/``artifacts_path``/``command`` have been proven either to be the
    server default or inside the authenticated principal's bound scope -- and
    ``(None, refusal_json)`` when it is refused, with the auditable typed error
    serialised for the tool to return.

    When the host has bound **no** authority the seam returns ``(None, None)``:
    that is the accepted in-process/direct-caller path used by the CLI parity
    tests, and it is why a reachable server is only *gated* once the host binds
    an authority. Once an authority *is* bound, a caller that presents no
    resolvable credential is refused for **every** tool, not only review.
    """
    authority = _bound_authority()
    if authority is None:
        return None, None
    token = resolve_session_token(presented_token)
    try:
        principal = authenticate_operation(
            token,
            operation,
            requested_role=requested_role,
            run_id=run_id,
            db_path=db_path,
            artifacts_path=artifacts_path,
            command=command,
            approval_token=approval_token,
            start_scope=start_scope,
            provider_route=provider_route,
        )
    except _authority_module().MCPAuthError as exc:
        return None, _auth_error_payload(exc)
    return principal, None


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
    provider_route: str = "",
) -> "MCPPrincipal":
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
    # Over HTTP the request guard already authenticated the bearer and attached
    # the principal for *this* token; reuse it so the per-tool gate authorizes
    # the same principal. ``authorize`` only honors it when it matches the
    # token's own session, so this can never widen authority.
    override = _current_http_principal()
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
        provider_route=provider_route,
        default_paths=server_default_paths(),
        principal_override=override,
    )


def authorize_provider_forwarding(
    envelope, route: str, *, session_token: str = ""
) -> None:
    """Refuse forwarding a client credential to an unapproved provider route.

    An ``execution-envelope-v1`` may name ``allowed_routes``; a route outside
    that set -- or a route that only exists to echo/reflect a credential -- is
    a refusal. The client's MCP session token is authority over *this server*,
    never a provider bearer token, so it is never passed through.

    When the caller presents a session token **and** the host has bound an
    authority, the decision is delegated to that authority
    (:meth:`MCPPrincipalAuthority.authorize` with ``provider_route``), so the
    route set is the authenticated principal's bound routes rather than an
    envelope field a caller could write. The envelope's own ``allowed_routes``
    remain the fallback for an unbound, direct in-process caller.
    """
    authority = _AUTHORITY
    token = resolve_session_token(session_token)
    if token and authority is not None and callable(
        getattr(authority, "serve_live", None)
    ):
        principal = authority.verify_session(token)
        authority.authorize(
            token, "forward",
            provider_route=route,
            default_paths=server_default_paths(),
        )
        facts = principal.to_envelope().get("scope", {}).get(
            _authority_module().EXECUTION_ENVELOPE_MCP_EXTENSION, {}
        )
        if facts.get("allowed_routes") and route not in facts["allowed_routes"]:
            raise _authority_module().MCPAuthError(
                "unapproved_provider_route",
                f"route {route!r} is not in the principal's approved routes",
                role=principal.role,
                action="forward",
                extra={"route": route,
                       "allowed_routes": list(facts["allowed_routes"])},
            )
        return

    # Direct in-process caller (no bound session): the envelope is the policy.
    allowed = (envelope or {}).get("allowed_routes") or []
    if route in NON_FORWARDABLE_ROUTES:
        raise _authority_module().MCPAuthError(
            "token_passthrough_refused",
            f"refusing to forward a client credential to route {route!r}",
            action="forward",
            extra={"route": route},
        )
    if allowed and route not in allowed:
        raise _authority_module().MCPAuthError(
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


def _auth_error_payload(exc) -> str:
    """A structured, auditable refusal -- never a boolean or bare string."""
    return json.dumps({"status": "refused", **exc.to_dict()})



@mcp.tool()
def create_run(tool: str, model: str, subject_repo: str, subject_commit: str, command: str, db_path: str = ".skillweave/store.db", artifacts_path: str = ".skillweave/artifacts", methodology: str = "ralph", autonomy_policy: str = "", task_id: str = "", attempt: int = 1, output_root: str = "", stream_root: str = "", artifact_evidence: str = "", source_root: str = "", analysis_root: str = "", session_token: str = "", route_proof: str = "") -> str:
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

    SW-1510-MCP-AUTH: the caller-supplied ``db_path``/``artifacts_path``/
    ``command`` are authorized at this boundary, never trusted; and the start
    scope (``task_id``/``attempt``) is refused as a duplicate at the authority
    seam, so a second start of the same attempt cannot launch.
    """
    start_scope = (
        start_scope_for(task_id=task_id, attempt=attempt) if task_id else ""
    )
    principal, refusal = gate_operation(
        "start_run",
        session_token,
        run_id="",
        db_path=db_path,
        artifacts_path=artifacts_path,
        command=command,
        start_scope=start_scope,
    )
    if refusal is not None:
        return refusal
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
def status_run(run_id: str, db_path: str = ".skillweave/store.db", session_token: str = "") -> str:
    """Get the status of a specific run.

    Includes the methodology/attempt and (for Assay) the ordered phases and
    terminal outcome, read from the same snapshot the CLI and replay use.

    SW-1510-MCP-AUTH: the read is authorized at this boundary; a cross-client
    run id is refused with an auditable typed error.
    """
    _principal, refusal = gate_operation(
        "read_run", session_token, run_id=run_id, db_path=db_path,
    )
    if refusal is not None:
        return refusal
    result = control.control_status(run_id, db_path=db_path)
    if result.get("error") == "Run not found":
        return json.dumps({"error": "Run not found"})
    return json.dumps(result)


@mcp.tool()
def cancel_run(run_id: str, reason: str = "operator_halt", db_path: str = ".skillweave/store.db", session_token: str = "") -> str:
    """Cancel a run through the native lifecycle entry.

    Never a bare database transition: the service re-verifies the recorded
    process identity against the live OS facts before signalling, stops the
    owned process group, and reports the real outcome. A recycled PID, a
    stale command or a foreign worker is refused (nothing is signalled) and
    an unobservable process is reported as ``unknown_inflight`` — the tool
    never claims a termination it did not perform.

    SW-1510-MCP-AUTH: the cancel is authorized at this boundary; a
    cross-client run id is refused with an auditable typed error.
    """
    _principal, refusal = gate_operation(
        "cancel_run", session_token, run_id=run_id, db_path=db_path,
    )
    if refusal is not None:
        return refusal
    result = control.control_cancel(run_id, reason=reason, db_path=db_path)
    if result.get("error") == "Run not found":
        return json.dumps({"error": "Run not found"})
    return json.dumps(result)


@mcp.tool()
def resume_run(run_id: str, db_path: str = ".skillweave/store.db", command: str = "", session_token: str = "") -> str:
    """Resume a run by launching a NEW identified attempt.

    With a *command*, this launches a real new attempt (new process, new
    recorded identity) through the native service — it never reports
    "restarted" from a database transition alone. Without a command it
    reconciles stored intents and returns the run's *actual* state with
    ``restarted: false``.

    SW-1510-MCP-AUTH: the resume (and any caller-supplied command) is
    authorized at this boundary; a cross-client run id or an arbitrary command
    is refused with an auditable typed error.
    """
    _principal, refusal = gate_operation(
        "resume_run", session_token, run_id=run_id, db_path=db_path,
        command=command,
    )
    if refusal is not None:
        return refusal
    result = control.control_resume(run_id, command=command, db_path=db_path)
    if result.get("error") == "Run not found":
        return json.dumps({"error": "Run not found"})
    return json.dumps(result)


@mcp.tool()
def get_evidence(run_id: str, db_path: str = ".skillweave/store.db", session_token: str = "") -> str:
    """Get evidence/journal for a run.

    SW-1510-MCP-AUTH: the read is authorized at this boundary; a cross-client
    run id is refused with an auditable typed error.
    """
    _principal, refusal = gate_operation(
        "read_run", session_token, run_id=run_id, db_path=db_path,
    )
    if refusal is not None:
        return refusal
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
    except _authority_module().MCPAuthError as exc:
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


# ---------------------------------------------------------------------------
# SW-1510-MCP-AGENTS: durable, harness-neutral handoff + agent mailbox tools
# ---------------------------------------------------------------------------
#
# These tools provide *application-level* agent communication over MCP without
# assuming MCP is a reliable peer-to-peer event bus. Notifications are advisory;
# the durable mailbox (``skillweave.dispatch.messaging``) is the source of
# truth, so a client that misses a notification recovers the same durable work
# by cursor. Every tool resolves a **server-owned scoped principal** (never a
# caller field) and every message carries run/task/attempt/message IDs.

#: The environment variable naming the durable mailbox file. It defaults beside
#: the server store so the mailbox path is server-owned -- a caller cannot
#: point an agent tool at an arbitrary path.
MAILBOX_PATH_ENV = "SKILLWEAVE_AGENT_MAILBOX"

#: The server's own default mailbox path (a caller may never inject another).
MAILBOX_DEFAULT_PATH = ".skillweave/agent-mailbox.jsonl"


def default_mailbox_path() -> str:
    """The server-owned durable mailbox path (env override, else the default)."""
    return os.environ.get(MAILBOX_PATH_ENV, "") or MAILBOX_DEFAULT_PATH


def _open_mailbox():
    """The durable mailbox and its module (imported lazily, GLE-020)."""
    from skillweave.dispatch import messaging as _messaging

    return _messaging, _messaging.AgentMailbox(default_mailbox_path())


def _message_principal(gate_principal, explicit_principal: str) -> str:
    """Resolve the scoped principal a message is authored/received under.

    Authority is server-owned: when an authority is bound the authenticated
    principal is authoritative, and an explicit principal that differs is a
    refusal (exactly as a role mismatch is refused) -- a caller cannot widen or
    redirect the scope by naming another principal. Only the in-process
    direct-caller path (no bound authority, the CLI-parity seam) may name its
    own principal.
    """
    if gate_principal is not None:
        if explicit_principal and explicit_principal != gate_principal.principal:
            raise _authority_module().MCPAuthError(
                "principal_mismatch",
                f"authenticated principal {gate_principal.principal!r} cannot "
                f"act as {explicit_principal!r}",
                role=gate_principal.role,
                action="messaging",
                extra={
                    "requested_principal": explicit_principal,
                    "actual_principal": gate_principal.principal,
                },
            )
        return gate_principal.principal
    if explicit_principal:
        return explicit_principal
    raise _authority_module().UnauthenticatedPrincipal(
        "agent messaging requires a scoped principal"
    )


@mcp.tool()
def mailbox_post(kind: str, recipient: str, sender: str = "", body: str = "", run_id: str = "", task_id: str = "", attempt: int = 0, in_reply_to: str = "", correlation_id: str = "", message_id: str = "", principal: str = "", session_token: str = "") -> str:
    """Post one durable, addressed ``agent-message-v1`` message.

    ``kind`` is one of ``handoff``, ``ack``, ``input_required`` or ``status``.
    The message is addressed to ``recipient`` under the *authenticated* scoped
    principal and stamped with ``run_id``/``task_id``/``attempt``/``message_id``.

    SW-1510-MCP-AGENTS: the write is authorized at this boundary (duplicate
    deliveries are idempotent by ``message_id``); the mailbox path is
    server-owned, never a caller path.
    """
    gate_principal, refusal = gate_operation(
        "write_run_state", session_token, run_id=run_id, db_path="",
    )
    if refusal is not None:
        return refusal
    _messaging, mailbox = _open_mailbox()
    try:
        scope_principal = _message_principal(gate_principal, principal)
        try:
            payload = json.loads(body) if body.strip().startswith(("{", "[")) else body
        except (json.JSONDecodeError, TypeError):
            payload = body
        kwargs = {"message_id": message_id} if message_id else {}
        message = _messaging.AgentMessage(
            kind=kind,
            sender=(sender or scope_principal),
            recipient=recipient,
            principal=scope_principal,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            in_reply_to=in_reply_to,
            correlation_id=correlation_id,
            body=payload,
            **kwargs,
        )
        record = mailbox.post(message)
    except _messaging.MessageChannelError as exc:
        return json.dumps({"status": "refused", **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    return json.dumps({"status": "posted", "message": record})


@mcp.tool()
def mailbox_read(recipient: str = "", resume_token: str = "", after_seq: int = 0, kinds: str = "", run_id: str = "", principal: str = "", session_token: str = "") -> str:
    """Read durable messages addressed to ``recipient`` from a resume cursor.

    This is the *subscription* read: it is pure (it consumes nothing), it
    carries a ``resume_token`` the caller persists, and it returns every message
    it has not yet accepted -- so a *missed* notification loses no durable work
    and a re-read is idempotent. ``kinds`` is an optional comma-separated filter
    (for example ``input_required,status``) so a caller may subscribe to just
    the input/status stream.

    SW-1510-MCP-AGENTS: the read is authorized at this boundary and scoped to
    the authenticated principal; a message addressed to another principal is
    never returned.
    """
    gate_principal, refusal = gate_operation(
        "read_run", session_token, run_id=run_id,
    )
    if refusal is not None:
        return refusal
    _messaging, mailbox = _open_mailbox()
    try:
        scope_principal = _message_principal(gate_principal, principal)
        wanted = [k.strip() for k in kinds.split(",") if k.strip()] or None
        messages = mailbox.read(
            recipient=(recipient or scope_principal),
            principal=scope_principal,
            resume_token=resume_token,
            after_seq=after_seq,
            kinds=wanted,
        )
    except _messaging.MessageChannelError as exc:
        return json.dumps({"status": "refused", **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    resume = messages[-1]["resume_token"] if messages else resume_token
    return json.dumps(
        {"status": "ok", "recipient": recipient or scope_principal,
         "messages": messages, "resume_token": resume}
    )


@mcp.tool()
def mailbox_replay(recipient: str = "", after_seq: int = 0, run_id: str = "", principal: str = "", session_token: str = "") -> str:
    """Replay the full durable history addressed to ``recipient``.

    The reconnect seam: regardless of which notifications a client observed, the
    same durable records are returned in append order. Reading never mutates or
    consumes the mailbox.

    SW-1510-MCP-AGENTS: the replay is authorized at this boundary and scoped to
    the authenticated principal.
    """
    gate_principal, refusal = gate_operation(
        "read_run", session_token, run_id=run_id,
    )
    if refusal is not None:
        return refusal
    _messaging, mailbox = _open_mailbox()
    try:
        scope_principal = _message_principal(gate_principal, principal)
        messages = mailbox.replay(
            recipient=(recipient or scope_principal),
            principal=scope_principal,
            after_seq=after_seq,
        )
    except _messaging.MessageChannelError as exc:
        return json.dumps({"status": "refused", **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    return json.dumps(
        {"status": "ok", "recipient": recipient or scope_principal,
         "messages": messages}
    )


@mcp.tool()
def mailbox_ack(message_id: str, note: str = "", run_id: str = "", principal: str = "", session_token: str = "") -> str:
    """Acknowledge a handoff by posting its durable ``ack`` reply.

    Only the message's *addressed recipient under its scoped principal* may
    acknowledge it; any other caller is refused. A repeated acknowledgement is
    idempotent (the reply id is derived from the original), so a duplicated
    delivery never creates a second acknowledgement.

    SW-1510-MCP-AGENTS: the ack is authorized at this boundary.
    """
    gate_principal, refusal = gate_operation(
        "write_run_state", session_token, run_id=run_id,
    )
    if refusal is not None:
        return refusal
    _messaging, mailbox = _open_mailbox()
    try:
        scope_principal = _message_principal(gate_principal, principal)
        record = mailbox.ack(
            message_id, recipient=scope_principal, principal=scope_principal,
            note=note,
        )
    except _messaging.MessageChannelError as exc:
        return json.dumps({"status": "refused", **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    return json.dumps({"status": "acked", "message": record})


@mcp.tool()
def harness_session_grant(
    role: str = "controller",
    client_id: str = "",
    transport: str = "stdio",
    run_id: str = "",
    task_id: str = "",
    attempt: int = 0,
    requested_role: str = "",
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
    stream_root: str = "",
    mailbox_path: str = "",
    session_token: str = "",
) -> str:
    """Mint a ``harness-mcp-session-grant-v1`` bound to run/task/attempt.

    This is the **real consumer/controller seam** for criterion 1: the caller is
    authorized first on the *delegation* operation (``attach_harness``), so the
    three first-class principals -- Main (``controller``), Ops (``worker``) and
    Reviewer (``reviewer``) -- each reach this seam under their own
    server-issued identity, and the resulting grant carries a *server-issued*
    token scoped to the actual run. The authority the grant mints is ``role``;
    the gate refuses a client asking for a stronger role than its authenticated
    principal holds (never granted), whether it names that role directly or via
    ``requested_role``. A principal may only attach a child under the explicit
    delegation model: Main may attach the distinct Ops (``worker``) and Reviewer
    (``reviewer``) principals it is *forbidden to embody itself*, every other
    role may attach only a peer of its own role, and no one attaches a second
    ``controller``. The child's authority is read from its own role definition,
    so it never exceeds what that role carries.

    The grant is bound to the one logical durable process service composed from
    ``db_path``/``mailbox_path``/``artifacts_path``/``stream_root``; a caller
    whose binding resolves to a different store is refused, so a child can never
    attach to an isolated per-harness controller.
    """
    gate_principal, refusal = gate_operation(
        "attach_harness",
        session_token,
        requested_role=requested_role,
        run_id=run_id,
        db_path=db_path,
        artifacts_path=artifacts_path,
    )
    if refusal is not None:
        return refusal
    if gate_principal is None:
        # In-process/direct-caller path (no authority bound, the CLI-parity
        # seam): there is no server registry to mint against, so the seam is
        # explicitly unavailable rather than silently serving an unscoped grant.
        return json.dumps(
            {"status": "refused", "code": "harness_grant_requires_authority",
             "message": "harness session grants require a bound MCP authority"}
        )
    try:
        from skillweave.mcp import bootstrap as _bootstrap

        # The *minted* authority is gated too: a child may never carry authority
        # broader than the authenticated principal's own (criterion 1). This is
        # load-bearing for every first-class principal because the operation
        # above already admitted Main/Ops/Reviewer (a stronger role demand is
        # refused by ``refuse_role_escalation``), so the delegation model -- not
        # an unreachable short-circuit -- is what bounds the minted child.
        _bound_authority().refuse_child_role_escalation(gate_principal, role)

        service = control.service_for_binding(
            db_path,
            mailbox_path=mailbox_path or default_mailbox_path(),
            artifacts_path=artifacts_path,
            stream_root=stream_root or None,
        )
        principal = gate_principal
        authority = _bound_authority()
        grant = issue_harness_session_grant(
            authority=authority,
            role=role,
            client_id=client_id or principal.client_id,
            transport=transport,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            service=service,
        )
        payload = grant.to_dict()
        payload["status"] = "ok"
        payload["env"] = grant.env()
        return json.dumps(payload)
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    except _bootstrap.ProcessServiceMismatchRefused as exc:
        return json.dumps(
            {"status": "refused", "code": "process_service_mismatch",
             "message": str(exc)}
        )


@mcp.tool()
def agent_capabilities(clients: str = "", session_token: str = "") -> str:
    """Detect, per client, which transports it supports and the adapter chosen.

    This is the **registration/capability attestation** seam: a client's support
    for MCP notifications is *explicitly detected* (positive config/skill-root
    presence or an operator attestation), never assumed. A client without
    notification support -- including one that was never detected -- is assigned
    the file/CLI polling adapter, which carries the **same authority contract**.
    The frozen ``agent-message-v1`` contract version and digest are reported so
    a client can attest the exact schema it speaks.

    SW-1510-MCP-AGENTS: authorized at this boundary; scoped to the authenticated
    principal.
    """
    _principal, refusal = gate_operation("read_run", session_token)
    if refusal is not None:
        return refusal
    from skillweave.dispatch import messaging as _messaging
    from skillweave.runtime import handoff as _handoff

    ids = [c.strip() for c in clients.split(",") if c.strip()] or None
    detected = _handoff.detect_capabilities(ids)
    return json.dumps({
        "status": "ok",
        "contract": {
            "artifact": _messaging.AGENT_MESSAGE_ARTIFACT,
            "schema_version": _messaging.AGENT_MESSAGE_SCHEMA_VERSION,
            "namespace": _messaging.AGENT_MESSAGE_NAMESPACE,
            "schema_digest": _messaging.AGENT_MESSAGE_SCHEMA_DIGEST,
        },
        "clients": {cid: cap.to_dict() for cid, cap in detected.items()},
    })


@mcp.tool()
def handoff_adapter(
    client_id: str = "",
    recipients: str = "",
    transport: str = "",
    run_id: str = "",
    session_token: str = "",
) -> str:
    """Select and return the qualified transport adapter for *client_id*.

    This is the **real production selection path** for criterion 5 (the seam
    ``agent_capabilities`` reports *about* but does not itself select): a caller
    asks for a harness's handoff transport and the server (1) authorizes the
    read at this boundary, (2) detects the client's capability (explicit
    attestation or a positive, file-backed presence signal -- never assumed),
    and (3) selects the adapter the *capability* dictates through a real call
    into :func:`~skillweave.runtime.handoff.select_handoff_adapter`. A client
    without MCP-notification support gets the qualified file/CLI polling
    adapter; an *explicitly requested* transport that the client does not
    support, or one that is not a supported transport at all, is a named
    ``TRANSPORT_NOT_SUPPORTED`` **HOLD** -- never a silent downgrade and never a
    fabricated MCP success.

    The returned adapter carries the **same authority contract** as every other
    transport: a server-owned :class:`~skillweave.runtime.handoff.
    HandoffAuthorityContract` bound to the authenticated principal and the
    addressed recipients the caller named. Under a bound authority the principal
    is the authenticated one (never a caller field); a direct in-process caller
    with no bound authority names its own principal, exactly as the mailbox
    tools do.
    """
    gate_principal, refusal = gate_operation("read_run", session_token, run_id=run_id)
    if refusal is not None:
        return refusal
    from skillweave.dispatch import messaging as _messaging
    from skillweave.runtime import handoff as _handoff

    try:
        principal = (
            _message_principal(gate_principal, "") if gate_principal is not None
            else "in-process"
        )
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)

    requested = [r.strip() for r in recipients.split(",") if r.strip()]
    if not requested:
        requested = [client_id or principal]
    contract = _handoff.HandoffAuthorityContract(
        principal=principal, recipients=frozenset(requested)
    )
    capability = _handoff.detect_client_capability(client_id or principal)
    try:
        adapter = _handoff.select_handoff_adapter(
            capability,
            _messaging.AgentMailbox(default_mailbox_path()),
            authority_contract=contract,
            requested_transport=transport,
        )
    except _handoff.HandoffError as exc:
        # A named, actionable HOLD: the caller is told *why* and what is
        # supported, never handed a downgraded transport.
        return json.dumps({
            "status": "hold",
            "code": exc.code,
            "error": str(exc),
            "client_id": capability.client_id,
            "reason": capability.reason,
            "supported_transports": sorted(
                _handoff.SUPPORTED_HANDOFF_TRANSPORTS
            ),
        })
    return json.dumps({
        "status": "ok",
        "client_id": capability.client_id,
        "contract": {
            "artifact": _messaging.AGENT_MESSAGE_ARTIFACT,
            "schema_version": _messaging.AGENT_MESSAGE_SCHEMA_VERSION,
            "namespace": _messaging.AGENT_MESSAGE_NAMESPACE,
            "schema_digest": _messaging.AGENT_MESSAGE_SCHEMA_DIGEST,
        },
        "adapter": adapter.to_dict(),
        "authority_contract": contract.to_dict(),
    })


#: The mailbox operations a first-class principal may drive on its own channel.
#: The default (empty) is the historical set, so an existing call that names no
#: action is byte-compatible. ``write`` is admitted for a *reviewer* through the
#: dedicated :func:`review_action` operation -- a reviewer may not write general
#: run state (that is what makes it distinct), but it may post the durable
#: Review result on its own addressed channel.
REVIEW_MAILBOX_OPERATIONS = frozenset({"read", "replay", "ack", "write"})


def _review_assignment_body(run_id: str, task_id: str, attempt: int, subject: str) -> dict[str, object]:
    """The durable body of an assignment: it names the *actual* subject scope.

    The result a reviewer returns is then source-bound to the same
    ``run_id``/``task_id``/``attempt`` the assignment carried, so a replayed
    assignment is verifiable against the work it assigned.
    """
    return {
        "assignment": subject,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
    }


@mcp.tool()
def review_action(
    action: str,
    recipient: str = "",
    message_id: str = "",
    body: str = "",
    run_id: str = "",
    task_id: str = "",
    attempt: int = 0,
    resume_token: str = "",
    in_reply_to: str = "",
    correlation_id: str = "",
    session_token: str = "",
) -> str:
    """Drive the independent-Review round trip over the real durable tools.

    This is the criterion-4 seam that lets a **distinct Review principal** --
    not only Main -- take part in the assignment round trip. A reviewer's scope
    deliberately excludes general ``write_run_state`` (that is what keeps Review
    independent), so the general ``mailbox_post``/``mailbox_ack`` tools refuse
    it. Here the authority-bearing operation is the *review* itself:
    ``action`` is one of ``read``/``replay``/``ack`` (admitted under
    ``review_run``) or ``write`` (the reviewer's own source-bound result,
    admitted under ``review_run`` -- never ``write_run_state``). The role is
    still read from the server-owned session registry, never the wire, and a
    non-review-capable principal is refused by that same operation check.

    A ``read``/``replay``/``ack`` is always on the authenticated principal's own
    addressed channel; a ``write`` is a durable ``status`` result whose body is
    bound to the actual ``run_id``/``task_id``/``attempt`` of the assignment it
    answers.
    """
    from skillweave.dispatch import messaging as _messaging
    from skillweave.runtime import handoff as _handoff

    normalized = (action or "").strip().lower()
    if normalized == "write":
        operation = "review_run"
    elif normalized in ("read", "replay", "ack", "result"):
        operation = "review_run"
    else:
        return json.dumps({
            "status": "refused", "code": "unknown_action",
            "error": f"unknown review action {action!r}",
        })
    # Every action is authorized on the authenticated reviewer's ``review_run``
    # scope: a non-reviewer principal is refused here (operation_not_permitted),
    # so this seam never widens a worker/observer's authority.
    gate_principal, refusal = gate_operation(operation, session_token, run_id=run_id)
    if refusal is not None:
        return refusal

    _messaging_mod, mailbox = _open_mailbox()
    try:
        principal = _message_principal(gate_principal, "")
        own = principal
        if normalized in ("read", "result"):
            messages = mailbox.read(
                recipient=(recipient or own), principal=own,
                resume_token=resume_token,
            )
            return json.dumps({
                "status": "ok", "action": "read",
                "recipient": recipient or own, "messages": messages,
            })
        if normalized == "replay":
            messages = mailbox.replay(
                recipient=(recipient or own), principal=own,
            )
            return json.dumps({
                "status": "ok", "action": "replay",
                "recipient": recipient or own, "messages": messages,
            })
        if normalized == "ack":
            record = mailbox.ack(
                message_id, recipient=(recipient or own), principal=own,
                note=body,
            )
            return json.dumps({
                "status": "ok", "action": "ack", "message": record,
            })
        # normalized == "write": the reviewer's own source-bound result.
        kwargs = {"message_id": message_id} if message_id else {}
        message = _messaging.AgentMessage(
            kind="status",
            sender=own,
            recipient=recipient or own,
            principal=own,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            in_reply_to=in_reply_to,
            correlation_id=correlation_id,
            body=_review_assignment_body(run_id, task_id, attempt, body),
            **kwargs,
        )
        record = mailbox.post(message)
        return json.dumps({
            "status": "ok", "action": "write", "message": record,
        })
    except _messaging_mod.MessageChannelError as exc:
        return json.dumps({"status": "refused", **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)


def startup_and_bind_stdio(
    supervisor: str = "",
    process_service: "object | None" = None,
    authority: "MCPPrincipalAuthority | None" = None,
) -> str:
    """Production issuance point: bind the authority and mint the stdio session.

    This is the only *production* caller of
    :meth:`MCPPrincipalAuthority.issue_session`. A local stdio start names its
    supervisor (:data:`SUPERVISOR_ENV`); this binds one process authority
    (:func:`bind_authority`) and mints exactly one ``controller``-role stdio
    session for that local operator. The minted token is held only in host
    memory (:data:`_READY_SESSION_TOKEN`) so the protected tools resolve the
    local operator without a wire credential -- it is never written to the
    environment or logged, and no client can mint it.

    ``process_service`` (optional) is the
    :class:`skillweave.mcp.bootstrap.HarnessProcessService` this stdio session
    operates against. When supplied, the minted session is a
    ``harness-mcp-session-grant-v1`` scoped to that one durable process service,
    so a local stdio path is admitted only when it demonstrably reaches the same
    authority/state service (criterion 3). Omitting it preserves the historical
    unscoped ``controller`` session exactly.

    An HTTP deployment does not use this path: the host binds its own authority
    and each HTTP client presents its own issued token.
    """
    global _READY_SESSION_TOKEN
    identity = (supervisor or require_supervisor_identity()).strip()
    if not identity:
        raise _authority_module().UnauthenticatedPrincipal(
            f"local stdio requires an explicit supervisor identity; set "
            f"{SUPERVISOR_ENV} to name the supervising principal"
        )
    auth = authority if authority is not None else _authority_module().MCPPrincipalAuthority()
    bind_authority(auth)
    if process_service is not None:
        grant = issue_harness_session_grant(
            authority=auth,
            role="controller",
            client_id=identity,
            transport="stdio",
            run_id="",
            service=process_service,
        )
        token = grant.session_token
    else:
        token = auth.issue_session(
            role="controller", client_id=identity, transport="stdio"
        )
    _READY_SESSION_TOKEN = token
    return _READY_SESSION_TOKEN


def main(
    *,
    process_service: "object | None" = None,
    bind_service: "bool | None" = None,
) -> None:
    """Serve the command server, or refuse with an explicit unavailable state.

    This is the only entry the module exposes as a *server*. With no ``mcp``
    package it refuses with the :class:`~skillweave.mcp.control.McpDependencyUnavailable`
    diagnostic -- a one-line stderr message and :data:`EXIT_REFUSED` -- rather
    than silently no-op'ing.

    A local stdio session must run under an *explicit supervisor identity*
    (:data:`SUPERVISOR_ENV`) and the host must have bound an MCP authority: a
    local stdio client is not implicitly trusted and a reachable server is not
    proof of authorization. Both are enforced before the runner is invoked;
    when the host has not already bound an authority, startup performs the
    production issuance itself (:func:`startup_and_bind_stdio`).

    ``process_service`` (optional) is the
    :class:`skillweave.mcp.bootstrap.HarnessProcessService` the started stdio
    session operates against; when supplied, the minted controller session is
    scoped to it (criterion 3).

    ``bind_service`` controls the **default** production start (criterion 3
    requires a stdio start be admitted *only* when it demonstrably reaches the
    same authority/state service):

    * ``None`` (the default): the production start composes the one durable
      process service itself from the server-owned binding
      (``SKILLWEAVE_AGENT_MAILBOX`` + :func:`default_mailbox_path`) and passes
      it to the startup seam, so a real ``python -m skillweave.mcp.server`` /
      ``skillweave-mcp`` start is admitted onto the same authority/state
      service rather than an unscoped session -- the criterion holds without a
      caller passsing an opt-in flag.
    * ``False``: the documented escape hatch for an in-process caller that
      provides its *own* authority and deliberately wants the historical
      unscoped controller session; it is not a production start.
    * ``True``: same as ``None`` (kept so an explicit opt-in reads naturally).

    A host that already bound an authority is untouched: this only governs the
    startup issuance the server performs for itself.
    """
    # A *documented* non-zero exit for either refusal: the server never starts
    # without its dependency or an explicit, authenticated supervisor identity.
    # ``EXIT_REFUSED`` is the single code an operator scripts against. The
    # conversion lives here, in the entry the *wheel* actually calls, so the
    # refusal is a one-line stderr diagnostic and the documented exit code
    # whether the server is launched by name (``skillweave-mcp``) or as the
    # module (``python -m skillweave.mcp.server``); in-process callers of
    # ``main()`` observe the same single conversion.
    try:
        control.require_mcp()  # raises the explicit diagnostic when absent
        require_supervisor_identity()  # local stdio needs a named supervisor
        if _bound_authority() is None:
            # The host process did not bind an authority: perform the production
            # issuance at startup, so a reachable server is authorized rather
            # than trusted for merely being reachable.
            if process_service is None and bind_service is not False:
                # Criterion 3: the default production start composes the one
                # durable process service from the server-owned inputs and
                # scopes the minted controller stdio session to it, so a bare
                # ``main()`` is admitted onto the same authority/state service.
                # ``bind_service=False`` is the documented escape hatch that
                # keeps the historical unscoped session for a caller supplying
                # its own authority.
                default_db, default_artifacts = server_default_paths()
                process_service = control.service_for_binding(
                    default_db,
                    mailbox_path=default_mailbox_path(),
                    artifacts_path=default_artifacts,
                )
            startup_and_bind_stdio(process_service=process_service)
        runner = getattr(mcp, "run", None)
        if not callable(runner):
            raise control.McpDependencyUnavailable(control.DEGRADED_REASON)
        runner()
    except control.McpDependencyUnavailable as exc:
        # Checked first so the dependency-refusal path never has to import the
        # optional authority subpackage. One line, token-free, no traceback.
        print(f"skillweave-mcp unavailable: {exc.reason}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)
    except _authority_module().MCPAuthError as exc:
        # A missing supervisor identity (the stdio seam) or any other auth
        # refusal: print the actionable one-liner and exit with the documented
        # code rather than dumping a traceback.
        print(f"skillweave-mcp refused: {exc.message}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


if __name__ == "__main__":
    # The refusal semantics live in ``main`` itself, so the module entry is a
    # plain delegation: the same entry the wheel's console script calls is the
    # one this module runs.
    main()
