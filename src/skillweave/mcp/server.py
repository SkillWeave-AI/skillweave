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
from typing import Any, Optional, Sequence

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


# ---------------------------------------------------------------------------
# SW-1511-MCP-BOOTSTRAP C6: the log-emission boundary and the restart seam
# ---------------------------------------------------------------------------

#: The environment variable naming the *interactive session* a restart is
#: evaluated against. A restarted main session reconnects under the same
#: identity, so this names the view, never the work.
SESSION_IDENTITY_ENV = "SKILLWEAVE_MCP_SESSION_IDENTITY"

#: The environment variable naming the durable process service a restart must
#: land on. A restart that resolves a *different* service is refused rather
#: than silently re-launched onto an isolated store.
HANDLE_IDENTITY_ENV = "SKILLWEAVE_MCP_CHILD_HANDLE"


def emit_log(message: str, *, environ=None) -> str:
    """The real log-emission boundary: redact *then* fail closed.

    **Every** log line the server emits passes through here, so the credential
    property holds for actual output rather than a caller-supplied blob. A
    worker's provider credential value is redacted before the line is written,
    and the emitted line is then re-scanned -- a credential that still appears
    verbatim (an unusually-shaped value the redactor did not recognise) is a
    hard failure, never a silent leak.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    env = os.environ if environ is None else environ
    redacted = _bootstrap.redact_worker_credentials(message, env)
    _bootstrap.assert_worker_credentials_not_in_logs(redacted, env)
    print(redacted, file=sys.stderr)
    return redacted


def reconnect_session(
    *,
    service: "object | None" = None,
    service_fingerprint: str = "",
    session_identity: str = "",
    handle: "object | None" = None,
    recorded_handle: "object | None" = None,
    environ=None,
) -> dict:
    """The real entry a *restarted* main session goes through when it reconnects.

    Restarting the interactive session must not disturb the single durable
    process service or a child attempt that is mid-flight (quota waiting). This
    is the production seam that **verifies** rather than re-launches:

    * the granted service identity must match ``service_fingerprint`` (the one
      the pre-restart session recorded), so a reconnect that resolves a
      *different* service -- an isolated per-harness controller, a diverged
      store -- is a named ``hold`` (``service_identity_mismatch``), never a
      silent re-admission; and
    * the child attempt is re-verified against the live OS facts through
      :func:`skillweave.mcp.bootstrap.restart_ready_by_service`, so a reconnect
      reports *ready* only when a genuine live child survives the restart.

    ``handle`` is a live child handle when the caller still holds one;
    ``recorded_handle`` is a handle rebuilt from the durable service's recorded
    process identity (:func:`skillweave.mcp.bootstrap.recorded_child_handle`),
    which is what a *restarted* process actually has. Either is re-verified;
    a reconnect with neither reports the child as not-verified (``hold``).

    It never stops the service or the child and never launches a replacement.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    env = os.environ if environ is None else environ
    identity = (session_identity or env.get(SESSION_IDENTITY_ENV, "")).strip()
    fingerprint = (service_fingerprint or env.get(HANDLE_IDENTITY_ENV, "")).strip()
    if service is None:
        return {
            "status": "hold",
            "code": "no_service_bound",
            "session_identity": identity,
            "reason": "no durable process service is bound to this session",
        }
    observed = _bootstrap.service_identity_for(service)
    if fingerprint and fingerprint != observed:
        return {
            "status": "hold",
            "code": "service_identity_mismatch",
            "session_identity": identity,
            "expected_service": fingerprint,
            "observed_service": observed,
            "reason": (
                "the reconnect resolved a different durable process service; "
                "refusing to re-admit the session onto a diverged store"
            ),
        }
    # Re-verify the child against the live OS facts through the same readiness
    # seam a restart relies on (``restart_ready`` -> ``restart_ready_by_service``
    # -> ``child_attempt_alive``).  A live handle is preferred; otherwise the
    # handle rebuilt from the durable service's *recorded* identity is used, so
    # a restarted process re-verifies the recorded child rather than assuming
    # one.  The reconnect never stops the service or the child and never
    # launches a replacement -- it only verifies.
    effective_handle = handle if handle is not None else recorded_handle
    readiness = _bootstrap.restart_ready_by_service(service, effective_handle)
    child_alive = bool(readiness["child_alive"])
    return {
        "status": readiness["status"],
        "code": "" if child_alive else "child_not_verified_alive",
        "session_identity": identity,
        "service_identity": observed,
        "service_reachable": readiness["service_reachable"],
        "child_alive": child_alive,
    }


@mcp.tool()
def restart_session(
    run_id: str = "",
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
    service_fingerprint: str = "",
    session_token: str = "",
) -> str:
    """The entry a *restarted* main session calls to re-admit itself.

    A restart of the interactive session must not disturb the single durable
    process service or a child attempt that is mid-flight (quota waiting). This
    is the registered tool a restarted session goes through: it resolves the
    *same* server-owned durable process service the pre-restart session ran
    against, rebuilds the child handle from the durable run's recorded native
    process identity, and re-verifies -- through
    :func:`skillweave.mcp.reconnect_session` ->
    :func:`skillweave.mcp.bootstrap.restart_ready` -- that the service identity
    is unchanged and the owned child is still verifiably alive. It never stops
    the service or the child and never launches a replacement, so a running
    quota wait and cancellation keep working across the restart.

    A reconnect that resolves a *different* service (an isolated per-harness
    controller, a diverged store), or a recorded child that is no longer
    verifiably alive, is a named ``hold`` -- never a silent re-admission.

    SW-1510-MCP-AUTH: the reconnect is authorized at this boundary; the caller
    may not name a service other than the server-owned one.
    """
    _principal, refusal = gate_operation(
        "restart_session", session_token, db_path=db_path,
    )
    if refusal is not None:
        return refusal
    # The reconnecting session resolves the *server-owned* durable process
    # service from the same inputs the serving seam uses -- a caller cannot
    # name a different one.
    service = control.service_for_binding(
        db_path,
        artifacts_path=artifacts_path,
        stream_root=_default_stream_root(db_path),
        mailbox_path=default_mailbox_path(),
    )
    recorded_handle = None
    if run_id:
        try:
            store = control._get_store(db_path)
            record = store.get_run(run_id)
        except Exception:  # noqa: BLE001 - an unreadable store is not a failure
            record = None
        recorded_handle = _bootstrap_recorded_child_handle(record)
    outcome = reconnect_session(
        service=service,
        service_fingerprint=service_fingerprint,
        session_identity=run_id,
        recorded_handle=recorded_handle,
    )
    return json.dumps(outcome)


def _bootstrap_recorded_child_handle(record):
    """Rebuild a recorded child handle from a run record (lazy bootstrap import)."""
    from skillweave.mcp import bootstrap as _bootstrap

    return _bootstrap.recorded_child_handle(record)


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
    db_path: str = ".skillweave/store.db",
    session_token: str = "",
    handoff_offer: str = "",
    action: str = "offer",
    handoff_action: str = "",
    handoff_id: str = "",
    reason: str = "",
    principal: str = "",
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

    SW-1511-RECOVERY criterion 2: when the caller names the ``run_id`` it is
    continuing, the response also carries the ``harness-continuation-v1``
    **continuation decision** for that run, derived from the client's
    *attested* capability and the *durably recorded* prior session identity of
    the run's in-flight owned attempt -- actual session resume only when the
    client attested it **and** the single in-flight attempt's recorded identity
    still verifies **and** that attempt recorded a session id; otherwise a named
    ``checkpoint_continuation`` limitation. The prior session id is read from
    the durable dispatch record (never a caller field and never invented), and
    the decision is persisted durably to the run's mailbox, where the reader
    :func:`read_continuation_decision` returns the *same* decision to a
    recovered controller. This is the production seam a recovered controller
    reaches; it never fabricates session-UUID continuity.

    The caller-supplied ``db_path`` is authorized **with** the read (exactly as
    :func:`restart_session` does), so a store outside the authenticated
    principal's bound roots is refused by the authority roots guard *before* the
    tool opens it -- never read and then trusted.

    SW-1511A-HANDOFF-001 criterion 1: when the caller presents a
    ``handoff_offer`` (a JSON ``controller-handoff/v2`` offer carrying the
    destination's ``harness-controller-attestation/v1``), the server *admits*
    the offer through the real
    :mod:`~skillweave.runtime.controller_handoff` state machine and records the
    resulting typed handoff durably on the run.  Admission is fail-closed on the
    four attested axes (attestation present, fresh, configured-route match,
    recipient-principal match) and on the two exactly-once axes (no second live
    handoff for the scope, no controller transfer while a provider-quota wait is
    recorded); every refusal is a named ``status: "hold"`` carrying the hold
    ``code``, never a silent success and never a downgraded transfer.  A handoff
    carrying a provider credential is refused by shape, so the lease transfer
    never carries a credential.

    SW-1511A-HANDOFF-001 finding F3: the state machine's four *transitions* also
    have their production entry point here, driven by the ``action`` argument
    (``offer`` -- the default, unchanged -- ``accept``, ``activate``,
    ``release``, ``reject``).  ``accept``/``activate``/``reject`` may be driven
    only by the **destination principal** the handoff is addressed to, and
    ``release`` only by the **current controller holder**; ``activate`` takes
    the destination's real lease so the generation it acquires is exactly the
    offered generation + 1 (a superseded generation is a named HOLD).  Every
    response carries a ``handoff_ref`` (handoff id, fencing generation,
    destination principal, scope, state) and every refusal is a named
    ``status: "hold"``.  The action is never a caller-declared authority: the
    identity rules are checked against the durable record and the store's live
    lease holder.

    ``offer`` -- whether it is the default, an explicit ``action="offer"`` or a
    ``handoff_offer`` payload -- is **not** a transition and does **not** return
    early: it runs the attested admission and then the transport selection and
    continuation persistence, so its response is the pre-existing combined shape
    (``client_id``, ``contract``, ``adapter``, ``authority_contract``,
    ``controller_handoff``, ``continuation``, ``continuation_persisted``) with
    the ``action``/``handoff_ref`` keys added additively.  Only ``accept``,
    ``activate``, ``release`` and ``reject`` return early with their own
    action-scoped shape.
    """
    gate_principal, refusal = gate_operation(
        "read_run", session_token, run_id=run_id, db_path=db_path,
    )
    if refusal is not None:
        return refusal
    from skillweave.dispatch import messaging as _messaging
    from skillweave.runtime import handoff as _handoff

    try:
        # The actor is this boundary's authenticated principal when an authority
        # is bound; a direct in-process caller with no bound authority names its
        # own principal (exactly as ``mailbox_post`` admits), so the identity
        # rules of a handoff transition remain enforceable there.  The
        # no-authority/no-name path stays the historic ``"in-process"`` actor.
        principal = (
            _message_principal(gate_principal, principal)
            if gate_principal is not None
            else (principal or "in-process")
        )
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)

    # SW-1511A-HANDOFF-001 finding F3: the four *transitions* the state machine
    # owns (accept/activate/release/reject) now have a production entry point at
    # this boundary, driven by the ``action`` argument.  ``offer`` is the
    # default, so every pre-existing call (which passes no ``action``) keeps its
    # exact behaviour -- and so does a call that presents a ``handoff_offer``
    # payload: the offer is an *admission*, not a transition, so it runs the
    # attested admission and then the transport selection and continuation
    # persistence below, returning the same combined shape as before (the new
    # ``action`` and ``handoff_ref`` keys are additive).
    #
    # Only a real transition returns early, with its own action-scoped shape.
    requested_action = (handoff_action or action or "offer").strip()
    if requested_action != "offer":
        module = _controller_handoff_module()
        try:
            record = _drive_controller_handoff_action(
                module,
                action=requested_action,
                handoff_offer=handoff_offer,
                db_path=db_path,
                run_id=run_id,
                handoff_id=handoff_id,
                reason=reason,
                actor=principal,
            )
        except module.ControllerHandoffRefused as exc:
            return json.dumps({"status": "hold", **exc.to_dict()})
        except module.HandoffReleaseRefused as exc:
            return json.dumps({
                "status": "hold",
                "code": module.HANDOFF_HOLD_RELEASE_UNSUPPORTED,
                "reason": exc.reason,
                **exc.context,
            })
        payload = {
            "status": "ok",
            "action": requested_action,
            "controller_handoff": record.to_dict(),
            "handoff_ref": {
                "handoff_id": record.handoff_id,
                "fencing_generation": record.fencing_generation,
                "destination_principal": record.destination_principal,
                "scope": record.scope,
                "state": record.state,
                "resulting_generation": record.resulting_generation,
            },
        }
        return json.dumps(payload)

    # SW-1511A-HANDOFF-001: admit a presented controller-handoff offer *before*
    # transport selection -- an offer that cannot carry the lease is a named
    # HOLD, and a caller never gets a "ok" selection that hides a refused
    # transfer.  Absent an offer this is a no-op, so the pre-existing transport
    # selection is unchanged.
    admitted: dict | None = None
    if handoff_offer:
        try:
            admitted = _admit_controller_handoff_offer(
                handoff_offer, db_path=db_path, run_id=run_id,
            )
        except _controller_handoff_module().ControllerHandoffRefused as exc:
            return json.dumps({"status": "hold", **exc.to_dict()})

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
    payload = {
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
    }
    if admitted is not None:
        # The typed handoff this offer admitted (state ``offered``): the actual
        # durable ``controller-handoff/v2`` record a destination reads.  The
        # offer path keeps the pre-existing combined shape (transport selection
        # + continuation persistence below); ``action`` and ``handoff_ref`` are
        # added *additively* so an offer response is shaped like a transition
        # response without dropping a single historic key.
        payload["controller_handoff"] = admitted
        payload["action"] = requested_action
        payload["handoff_ref"] = {
            "handoff_id": admitted.get("handoff_id"),
            "fencing_generation": admitted.get("fencing_generation"),
            "destination_principal": admitted.get("destination_principal"),
            "scope": admitted.get("scope"),
            "state": admitted.get("state"),
            "resulting_generation": admitted.get("resulting_generation"),
        }
    if run_id:
        # The recovered controller's continuation decision, driven by the
        # *attested* capability (never assumed) and the *durably recorded* prior
        # session of the run's owned attempt (never a caller field): session
        # resume only when the client attested it *and* that attempt recorded a
        # session id; otherwise a named checkpoint continuation that carries no
        # session id forward.  The session id is read from the durable dispatch
        # record -- an unrecorded or unreadable record yields the empty string,
        # so the decision falls closed rather than fabricating an id.
        intent_id, prior_session_id = _recorded_session_for_run(db_path, run_id)
        decision = capability.continuation_decision(
            run_id, prior_session_id=prior_session_id, intent_id=intent_id
        )
        payload["continuation"] = decision.to_dict()
        # Persist the decision where a recovered controller reads it: the run's
        # durable mailbox, addressed under the *authenticated* scoped principal
        # and idempotent by message_id (a re-driven selection appends nothing).
        persisted = _persist_continuation_decision(
            decision, run_id=run_id, principal=principal
        )
        payload["continuation_persisted"] = persisted
    return json.dumps(payload)


def _controller_handoff_module():
    """The controller-handoff authority module (import seam, never global)."""
    from skillweave.runtime import controller_handoff as module

    return module


#: The server-owned evidence key carrying the *conclusion* this boundary reached
#: about a harness's counters.  It is written by the server (never read from the
#: caller's evidence) so the durable record states what was actually verified --
#: and, when nothing could be verified, says so instead of implying a pass.
_HANDOFF_EVIDENCE_VERIFICATION_KEY = "server_verification"


def _replace_attestation(attestation: Any, **changes: Any) -> Any:
    """A copy of *attestation* with *changes* applied (frozen dataclass)."""
    from dataclasses import replace

    return replace(attestation, **changes)


def _handoff_gateway_count(raw: Any) -> Optional[int]:
    """The client's observed gateway request count, or ``None`` when unreadable.

    ``None`` is **not** "zero requests": a missing/non-numeric count is an
    *unverifiable* claim, which is refused by name -- never silently treated as
    a satisfied one.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _verify_handoff_evidence(
    module: Any,
    evidence: dict,
    *,
    observed_route: Optional[str],
    store: Any = None,
    run_id: str = "",
) -> dict:
    """Verify a harness's route claim before it is admitted (F-1511A-01).

    Criterion 3 requires a harness to *prove its configured route with gateway
    counters*.  A bare ``dict`` passthrough proves nothing, so the boundary
    verifies the claim before it is recorded.

    The gate is the **route claim itself**: an attestation that names a
    ``configured_route.route`` is asserting it observed that route, and an
    assertion must be backed by a positive observed count.  An attestation that
    names no route makes no such assertion, so there is nothing to verify and it
    is admitted exactly as before (an additive change -- the pre-existing
    route-less fixtures are untouched).

    For a named route the three refusals are:

    * **zero or absent observed gateway requests** -- the client claims a route
      it made no request on, so it has proven nothing;
    * **an upstream the evidence itself reports as failed** (a non-zero exit
      code) -- the run that claims to prove the route did not succeed;
    * **a route contradicting the route the server observed** -- when the server
      *has* a genuine observation (a recorded dispatch route or an upstream
      receipt), an evidence route naming a different one is refused.

    When no server-side observation exists the counters are still required to be
    positive, and the record carries an explicit ``server_verification``
    conclusion stating that the route could not be cross-checked.  The *live*
    Claude Desktop / OpenCode proof this criterion also asks for remains
    labelled NOT PERFORMED: this boundary never asserts a live qualification it
    did not perform.

    **Monotonicity across observations** (the SW-1511A-HANDOFF-001 accepted
    limitation, resolved here): the observed count for a run/route is a counter,
    so a later observation of the *same* run and route can never be lower than
    the last one.  When a durable prior observation is readable and the present
    count is lower, the claim is refused as a named
    :data:`~skillweave.runtime.controller_handoff.HANDOFF_HOLD_COUNTER_REGRESSION`
    HOLD.  The prior observation is read from, and the admitted count written
    back to, the *durable* run metadata (so it survives a restart); a run/route
    the server has no prior observation for is a first observation and is
    admitted exactly as before.  An **unreadable** stored value fails closed:
    the boundary refuses rather than reading it as absent (fail-open) or
    trusting it.  A different run or route has its own series and never
    interferes.
    """
    claimed_route = str(evidence.get("gateway_route") or "")
    if not claimed_route:
        return {
            "route_claim": None,
            "cross_checked_with_server_record": False,
            "live_qualification": "NOT PERFORMED",
        }
    count = _handoff_gateway_count(evidence.get("gateway_request_count"))
    if count is None or count <= 0:
        raise module.ControllerHandoffRefused(
            module.HANDOFF_HOLD_EVIDENCE_UNVERIFIED,
            f"the attestation claims gateway route {claimed_route!r} but its "
            f"observed gateway request count is {count!r}; a route claim with no "
            f"observed requests proves nothing and is a named HOLD, never "
            f"recorded as a proven route",
            observed_gateway_request_count=count,
            route_claim=claimed_route,
        )
    exit_code = evidence.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        raise module.ControllerHandoffRefused(
            module.HANDOFF_HOLD_EVIDENCE_UNVERIFIED,
            f"the attestation's own evidence records exit_code {exit_code}: the "
            f"run that claims to prove route {claimed_route!r} did not succeed, "
            f"so the counter it reports proved nothing",
            exit_code=exit_code,
            route_claim=claimed_route,
        )
    if observed_route is not None and claimed_route != observed_route:
        raise module.ControllerHandoffRefused(
            module.HANDOFF_HOLD_EVIDENCE_UNVERIFIED,
            f"the attestation's evidence names route {claimed_route!r} but the "
            f"server observed {observed_route!r}; a route claim that "
            f"contradicts the observed route is a named HOLD",
            evidence_route=claimed_route,
            observed_route=observed_route,
        )
    # Monotonicity across observations: a gateway request count advances; a
    # lower one for the same run and route is a contradiction, not a new
    # observation.  The prior value is the server's own durable record for this
    # run/route (never the caller's), captured before this observation is
    # admitted, and an unreadable prior value fails closed by name.
    prior: Optional[int] = None
    if store is not None and run_id:
        try:
            prior = module.read_gateway_counter_observation(store, run_id, claimed_route)
        except module.GatewayCounterObservationError as exc:
            raise module.ControllerHandoffRefused(
                module.HANDOFF_HOLD_COUNTER_REGRESSION,
                f"the durable prior observation for route {claimed_route!r} on "
                f"run {run_id!r} is unreadable ({exc}); the count is refused "
                f"rather than read as absent",
                route_claim=claimed_route,
                run_id=run_id,
            ) from exc
        if prior is not None and count < prior:
            raise module.ControllerHandoffRefused(
                module.HANDOFF_HOLD_COUNTER_REGRESSION,
                f"the attestation reports {count} observed gateway requests for "
                f"route {claimed_route!r}, lower than the {prior} the server last "
                f"observed for that run and route; a gateway counter advances "
                f"across observations, so a lower one is a named HOLD",
                observed_gateway_request_count=count,
                prior_gateway_request_count=prior,
                route_claim=claimed_route,
            )
        module.record_gateway_counter_observation(store, run_id, claimed_route, count)
    return {
        "gateway_request_count_observed": count,
        "prior_gateway_request_count": prior,
        "route_claim": claimed_route,
        "cross_checked_with_server_record": observed_route is not None,
        # The live proof this criterion also asks for is NOT performed here: no
        # real Claude Desktop or OpenCode process was launched and no gateway
        # request was made.  The boundary records the verified claim and labels
        # the live qualification honestly.
        "live_qualification": "NOT PERFORMED",
    }


def _validate_destination_principal(module: Any, principal: str) -> None:
    """Refuse a destination principal the *server* could not have issued (F-1511A-04).

    Criterion 1 asks the lease to move to a **server-issued** destination
    principal.  An `mcp:`-shaped principal is a claim that the MCP authority
    minted it: ``mcp:<role>:<client_id>`` with ``<role>`` drawn from the
    server-owned :data:`~skillweave.runtime.authority.MCP_ROLE_SCOPES` registry.
    A principal in that shape whose role is not a real registry role is a
    caller-declared authority claim and is refused by name -- it is *not*
    admitted, and no server-issued identity is invented for it.

    A principal that is *not* `mcp:`-shaped carries no server-issued claim at
    all: it is the documented in-process / self-named path (exactly as the
    mailbox tools admit), and it is admitted on the same authority the tool was
    already gated on.  The check is therefore conditional on the claim being
    made, never a blanket refusal of the existing contract.
    """
    if not principal.startswith("mcp:"):
        return
    from skillweave.runtime.authority import MCP_ROLE_SCOPES

    parts = principal.split(":")
    role = parts[1] if len(parts) == 3 else ""
    if len(parts) == 3 and role in MCP_ROLE_SCOPES:
        return
    raise module.ControllerHandoffRefused(
        module.HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED,
        f"the destination principal {principal!r} claims to be server-issued "
        f"(an 'mcp:' principal) but its form is not one the MCP authority can "
        f"mint; the server refuses the claim rather than inventing a "
        f"server-issued identity for it",
        destination_principal=principal,
    )


def _endpoint_host_port(endpoint: str) -> Optional[tuple[str, int]]:
    """The ``(host, port)`` of a ``scheme://host:port`` gateway endpoint."""
    if not endpoint:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(endpoint)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.hostname, int(port))


def _handoff_observations(
    store: Any, run_id: str
) -> tuple[Optional[tuple[str, int]], Optional[str]]:
    """The gateway facts the *server* genuinely observed for *run_id*.

    Criterion 3's route axis is enforced against an observation, never an
    invention.  Two durable, server-written sources are consulted -- nothing a
    caller can set in the offer:

    * the newest non-empty **dispatch-intent route** the server recorded for the
      run (the route the owned attempt was dispatched on); and
    * the newest **upstream receipt** the server recorded for the run (the
      endpoint host:port the gateway actually answered on, and its route).

    Returns ``(endpoint, route)`` where ``endpoint`` is the observed
    ``(host, port)`` the module's route axis compares against the attestation's
    *configured* gateway, and ``route`` is the observed route string the
    evidence cross-check compares against the evidence's claimed route.  Either
    is ``None`` when the server has no such observation: the axis is then simply
    not enforced, exactly as before -- the boundary never fabricates an
    observation (the owner's *configured* gateway ``127.0.0.1:8090`` pin is a
    configuration, not an observation, and is never used here).
    """
    if not run_id or store is None:
        return (None, None)
    route = ""
    endpoint: Optional[tuple[str, int]] = None
    try:
        for intent in store.list_dispatch_intents(run_id):
            candidate = str(getattr(intent, "route", "") or "")
            if candidate:
                route = candidate
    except Exception:  # pragma: no cover - a store without the intent table
        route = ""
    try:
        for receipt in store.list_evidence(run_id):
            recorded = (getattr(receipt, "metadata", None) or {}).get("receipt")
            if isinstance(recorded, dict):
                candidate = str(recorded.get("route") or "")
                if candidate:
                    route = candidate
                observed = _endpoint_host_port(str(recorded.get("gateway_endpoint") or ""))
                if observed is not None:
                    endpoint = observed
    except Exception:  # pragma: no cover - a store without the evidence table
        pass
    return (endpoint, route or None)


def _admit_controller_handoff_offer(
    handoff_offer: str, *, db_path: str, run_id: str
) -> dict:
    """Admit a presented ``controller-handoff/v2`` offer through the real machine.

    This is the production caller of
    :mod:`skillweave.runtime.controller_handoff` for criterion 1: the server
    parses the caller's offer, admits it (four attested axes + the exactly-once
    axes) and records the typed handoff durably on the run.  Every refusal
    propagates as a :class:`~skillweave.runtime.controller_handoff.
    ControllerHandoffRefused` carrying its named hold -- this function never
    silently downgrades an offer into a success.

    The offer's ``fencing_generation`` is **re-read from the durable controller
    lease** through the run store (never taken from the caller's blob): a caller
    cannot *assert* the generation its offer carries, it must be the generation
    the lease actually holds.

    The **observed route** handed to the admission is computed here from the
    server's own durable records (:func:`_observed_handoff_route`), so the
    route-match axis is enforced at the real boundary whenever the server has an
    observation to compare against -- it is no longer hardcoded away (review
    finding F-1511A-02).
    """
    module = _controller_handoff_module()
    try:
        offer = json.loads(handoff_offer) if isinstance(handoff_offer, str) else handoff_offer
    except (TypeError, ValueError) as exc:
        raise module.ControllerHandoffRefused(
            module.HANDOFF_HOLD_ATTESTATION_ABSENT,
            f"the presented handoff offer is not readable JSON: {exc}",
        ) from exc
    if not isinstance(offer, dict):
        raise module.ControllerHandoffRefused(
            module.HANDOFF_HOLD_ATTESTATION_ABSENT,
            "the presented handoff offer is not a controller-handoff/v2 object",
        )

    attestation_raw = offer.get("destination_attestation")
    attestation = None
    if isinstance(attestation_raw, dict):
        route = attestation_raw.get("configured_route") or {}
        caps = attestation_raw.get("capabilities") or {}
        # The attestation's per-harness *evidence* (gateway counters observed,
        # exit code, artifact) and its MCP config anchors are carried through
        # admission verbatim: criterion 3 proves each harness's configured route
        # with counters, so the record must keep what the harness actually
        # observed.  The module's own ``HandoffAttestation.from_dict`` reads the
        # same two fields, so a record admitted here and a record re-read from
        # its durable form carry identical evidence.
        attestation = module.HandoffAttestation(
            client_id=str(attestation_raw.get("client_id") or ""),
            gateway_host=str(route.get("gateway_host") or ""),
            gateway_port=int(route.get("gateway_port") or 0),
            recipient_principal=str(attestation_raw.get("recipient_principal") or ""),
            notifications=bool(caps.get("notifications")),
            session_resume=bool(caps.get("session_resume")),
            can_accept_controller_handoff=bool(
                caps.get("can_accept_controller_handoff", True)
            ),
            adapter_kind=str(attestation_raw.get("adapter_kind") or ""),
            route=str(route.get("route") or ""),
            alias=str(route.get("alias") or ""),
            credential_binding=str(route.get("credential_binding") or ""),
            observed_at=str(attestation_raw.get("observed_at") or ""),
            max_age_seconds=int(attestation_raw.get("max_age_seconds") or 0),
            mcp_config_anchors=tuple(caps.get("mcp_config_anchors") or ()),
            evidence=dict(attestation_raw.get("evidence") or {}),
        )

    store = _get_store(db_path)

    # F-1511A-01: the evidence is *verified* before it is admitted, never
    # passed through.  The verification's own conclusion is written back under
    # a server-owned key so the durable record states what was checked (and
    # that the live qualification was NOT PERFORMED) rather than implying a
    # proof the server did not perform.
    observed_endpoint, observed_route = _handoff_observations(store, run_id)
    if attestation is not None:
        verification = _verify_handoff_evidence(
            module,
            attestation.evidence,
            observed_route=observed_route,
            store=store,
            run_id=run_id,
        )
        attestation = _replace_attestation(
            attestation,
            evidence={**attestation.evidence, _HANDOFF_EVIDENCE_VERIFICATION_KEY: verification},
        )

    destination_principal = str(offer.get("destination_principal") or "")
    # F-1511A-04: an `mcp:`-shaped destination principal claims the MCP
    # authority issued it; the boundary refuses a claim in that shape whose role
    # is not a real server-owned role, instead of inventing an issuance source.
    _validate_destination_principal(module, destination_principal)

    record = module.offer_handoff(
        from_holder=str(offer.get("from_holder") or ""),
        destination_principal=destination_principal,
        scope=str(offer.get("scope") or run_id or ""),
        destination_attestation=attestation,
        # The observed route is the gateway endpoint the *server* observed, read
        # from its own durable records (F-1511A-02).  Absent a genuine
        # observation the route-match axis is not enforced -- never by inventing
        # a route (the owner's configured pin is a configuration, not an
        # observation).
        observed_route=observed_endpoint,
        credential_binding=str(offer.get("credential_binding") or ""),
        handoff_id=str(offer.get("handoff_id") or ""),
    )

    if run_id:
        # Re-read the offering controller's generation from the *durable* lease:
        # a caller-supplied generation is never trusted.
        try:
            generation = int(store.current_fencing_token())
        except Exception:  # pragma: no cover - a store without the lease table
            generation = int(offer.get("fencing_generation") or 1)
        if generation >= 1:
            record = module.bind_offered_generation(record, generation)

        # Exactly-once: a second live handoff for the scope is refused, and a
        # controller transfer during a recorded provider-quota wait is refused.
        held = module.live_handoff_scope_hold(
            store, record.scope, exclude_run_id=run_id
        )
        if held is not None:
            raise module.ControllerHandoffRefused(
                module.HANDOFF_HOLD_LIVE_HANDOFF,
                f"the scope {record.scope!r} is already carried live by run "
                f"{held!r}; a second live handoff would be a second continuator",
                scope=record.scope,
                held_run_id=held,
            )
        wait = module.quota_wait_hold(store, run_id)
        if wait is not None:
            raise module.ControllerHandoffRefused(
                module.HANDOFF_HOLD_QUOTA_WAIT,
                f"run {run_id!r} has a recorded provider-quota wait ({wait!r}): "
                f"it is a live continuation of the same owned attempt, so a "
                f"controller transfer now would be a second continuation",
                run_id=run_id,
                wait_decision=wait,
            )
        record = module.record_handoff(store, record, run_id=run_id)
    return record.to_dict()


def _drive_controller_handoff_action(
    module: Any,
    *,
    action: str,
    handoff_offer: str,
    db_path: str,
    run_id: str,
    handoff_id: str,
    reason: str,
    actor: str,
) -> Any:
    """Drive one handoff action at the real MCP boundary (finding F3).

    ``offer`` (the default) keeps the pre-existing attested admission and
    returns the *offered* record; the four transitions are delegated to
    :func:`skillweave.runtime.controller_handoff.drive_handoff`, whose ``actor``
    is the **authenticated/attributed principal** this boundary resolved --
    never a caller field.  The destination/original-controller identity rules
    are enforced inside that driver against the durable record and the store's
    live lease holder, so this boundary never re-implements a move.

    ``activate`` supplies the destination's **real** lease acquisition (the
    durable store's own ``acquire_controller_lease`` under the destination
    principal), so the exactly-once generation arithmetic is arbitrated by the
    store: no activation without a durably acquired generation ``offered + 1``.
    ``release`` yields the lease the *current holder* actually holds -- the
    holder over the offered generation, or the generation the destination
    acquired on activation -- so a stale generation is never released.

    Returns the durable record the transition reached; every refusal propagates
    as the named :class:`ControllerHandoffRefused` (or the module's
    :class:`HandoffReleaseRefused`) -- never a silent success.
    """
    if action == module.HANDOFF_ACTION_OFFER:
        return module.ControllerHandoffRecord.from_dict(
            _admit_controller_handoff_offer(handoff_offer, db_path=db_path, run_id=run_id)
        )
    store = _get_store(db_path)

    def _acquire():
        # The destination's own real acquisition, under the principal the
        # server-issued handoff is addressed to.
        return store.acquire_controller_lease(actor or "controller")

    release_lease = None
    if action == module.HANDOFF_ACTION_RELEASE:
        held = module.read_handoff(store, run_id)
        if held is not None:
            # The generation to yield: the destination's acquired generation
            # once active, else the offering holder's generation still held.
            generation = (
                held.resulting_generation
                if held.resulting_generation is not None
                else held.fencing_generation
            )

            def release_lease() -> None:
                store.release_controller_lease(int(generation))

    return module.drive_handoff(
        store,
        action=action,
        run_id=run_id,
        handoff_id=handoff_id,
        reason=reason,
        actor=actor,
        acquire=_acquire,
        release=release_lease,
    )


def _recorded_session_for_run(db_path: str, run_id: str) -> tuple[str, str]:
    """The *ordering-first* in-flight attempt's verified ``(intent_id, session_id)``.

    Reads the durable dispatch record for ``run_id`` -- the same record a launch
    wrote -- so a session resume is bound to an *observed* session, never a
    caller-supplied or invented one.  The selection is the native controller's
    own: :meth:`NativeProcessService._resume_decision` adopts the
    **ordering-first** in-flight intent (``in_flight[0]``, the frontier's
    ``created_at``/``intent_id`` order) when its recorded identity still
    verifies through :meth:`NativeProcessService.verify_owned_attempt`, and
    otherwise **refuses** -- it never skips the first intent to adopt a later
    one.  A first intent whose identity no longer verifies (recycled pid, stale
    command, foreign worker, unobservable) therefore yields a fail-closed
    ``("", "")`` and the named checkpoint, exactly as the native decision
    refuses with ``refused_no_owned_attempt`` rather than silently continuing a
    different attempt.  A store that cannot be opened is likewise a fail-closed
    ``("", "")``.
    """
    store = None
    try:
        store = _get_store(db_path)
        # Only the claiming frontier carries a resumable owned attempt; a
        # reviewed/integrated/failed intent is a superseded record, not the one
        # a recovered controller continues.
        intents = store.list_dispatch_intents(run_id, claiming_only=True)
    except Exception:
        return "", ""
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    if not intents:
        return "", ""
    # Identical to the native decision: adopt only the ordering-first in-flight
    # intent when its recorded identity still verifies; refuse rather than skip.
    first = intents[0]
    if _intent_identity_verified(first):
        return first.intent_id, first.session_id
    return "", ""


def _intent_identity_verified(intent) -> bool:
    """Whether *intent*'s recorded process identity still verifies on the OS.

    Mirrors :meth:`NativeProcessService.verify_owned_attempt` without
    instantiating the service: an intent that recorded no identity, or whose
    recorded facts no longer match the live process, is not the owned attempt.
    """
    from skillweave.runtime import process_identity

    recorded = getattr(intent, "process_identity", None)
    if not recorded:
        return False
    try:
        pid = int(recorded.get("pid", 0))
    except (AttributeError, TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return bool(process_identity.verify(recorded, pid).ok)


def _persist_continuation_decision(decision, *, run_id: str, principal: str) -> dict:
    """Persist a continuation decision to the run's durable mailbox.

    The decision is posted as a ``status`` ``agent-message-v1`` record scoped to
    ``run_id`` and the authenticated ``principal``, with a deterministic
    ``message_id`` so a re-driven selection re-posts the *same* record (a
    duplicate delivery is idempotent, never a second decision).  The durable
    record is where a recovered controller reads the decision; a persisted
    record is never a fabricated success -- the returned mapping reports the
    real outcome.
    """
    _messaging, mailbox = _open_mailbox()
    message_id = f"harness-continuation-{run_id}-{decision.mode}"
    message = _messaging.AgentMessage(
        kind="status",
        sender=principal,
        recipient=principal,
        principal=principal,
        run_id=run_id,
        message_id=message_id,
        body=decision.to_dict(),
    )
    try:
        record = mailbox.post(message)
    except _messaging.MessageChannelError as exc:
        return {"status": "refused", **exc.to_dict()}
    return {"status": "persisted", "message_id": record.get("message_id"), "seq": record.get("seq")}


@mcp.tool()
def read_continuation_decision(
    run_id: str = "",
    principal: str = "",
    session_token: str = "",
) -> str:
    """Read back the persisted ``harness-continuation-v1`` decision for a run.

    This is the **production reader** a recovered controller uses to reach the
    *same* continuation decision :func:`handoff_adapter` persisted: the durable
    ``status`` ``agent-message-v1`` record addressed to the authenticated
    scoped principal, carrying the decision body. It reads the server-owned
    mailbox only (never instantiating the native process service) and is
    authorized at this boundary under ``read_run``, scoped to the authenticated
    principal -- a caller cannot read another principal's decision.

    Returns ``found: true`` with the stored decision when one is present, and a
    fail-closed ``found: false`` (never a fabricated decision) when no decision
    has been persisted for the run or the read is refused.

    The read is scoped to ``run_id`` by the run recorded **in the record
    itself**, not by the caller argument alone: a mailbox read filters by
    recipient and principal only, so a caller bound to several runs would
    otherwise receive the newest decision across all of them mislabelled with
    the requested run.  A record that belongs to a different run is never
    returned and is never relabelled with the requested ``run_id`` -- when no
    record of that run exists the read is ``found: false``.
    """
    gate_principal, refusal = gate_operation("read_run", session_token, run_id=run_id)
    if refusal is not None:
        return refusal
    _messaging, mailbox = _open_mailbox()
    try:
        scope_principal = _message_principal(gate_principal, principal)
        messages = mailbox.read(
            recipient=scope_principal, principal=scope_principal, kinds=["status"],
        )
    except _messaging.MessageChannelError as exc:
        return json.dumps({"status": "refused", "found": False, **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    for record in reversed(messages):
        # The run on the durable record is authoritative; the caller's run_id
        # only ever narrows the search, it never relabels a foreign record.
        if record.get("run_id") != run_id:
            continue
        body = record.get("body")
        if (
            isinstance(body, dict)
            and body.get("schema_id") == _continuation_schema_id()
        ):
            return json.dumps({
                "status": "ok",
                "found": True,
                "run_id": record.get("run_id"),
                "message_id": record.get("message_id"),
                "seq": record.get("seq"),
                "decision": body,
            })
    return json.dumps({
        "status": "ok", "found": False, "run_id": run_id,
        "message": "no harness-continuation decision is persisted for this run",
    })


def _continuation_schema_id() -> str:
    """The frozen ``harness-continuation-v1`` schema id (imported lazily)."""
    from skillweave.runtime.native_process import HARNESS_CONTINUATION_SCHEMA_ID

    return HARNESS_CONTINUATION_SCHEMA_ID


# ---------------------------------------------------------------------------
# SW-1511-RECOVERY criterion 6: one recovered owned attempt, mailbox by cursor
# ---------------------------------------------------------------------------

#: The durable status message_id marking that a run's one recovery has been
#: recorded. Deterministic so a second recovery of the same run collides here
#: (a duplicate dispatch is detected) and a re-driven recovery is idempotent.
def _recovery_message_id(run_id: str) -> str:
    return f"owned-attempt-recovery-{run_id}"


def _verified_owned_attempt_for_run(db_path: str, run_id: str):
    """The *ordering-first, claiming-only* verified intent for ``run_id``.

    Returns ``(intent, ordered)`` where ``ordered`` is the run's claiming
    frontier in the store's own ``created_at``/``intent_id`` order, and
    ``intent`` is the frontier attempt only when its recorded process identity
    still verifies on the OS -- otherwise ``None``. This is the *selection* the
    native controller makes (never a skip to a later attempt); the recovery
    planner only shapes it. The store's own filter is what scopes the read to
    the record's own run -- a caller cannot widen it.
    """
    store = None
    try:
        store = _get_store(db_path)
        ordered = list(store.list_dispatch_intents(run_id, claiming_only=True))
    except Exception:  # noqa: BLE001 - an unreadable store verifies nothing
        return None, []
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
    if not ordered:
        return None, []
    first = ordered[0]
    if _intent_identity_verified(first):
        return first, ordered
    return None, ordered


def _recovery_already_recorded(*, run_id: str, principal: str) -> bool:
    """Whether this run's one recovery was already durably recorded.

    The recovered attempt is *adopted* from the run's durable claiming frontier
    (the store record already exists -- a recovery never mints a second
    attempt), and the deterministic recovery marker is posted to the run's
    durable mailbox. A second recovery of the same run therefore cannot record
    a second owned attempt. Reading is pure -- it never consumes a record.
    """
    _messaging, mailbox = _open_mailbox()
    try:
        records = mailbox.read(recipient=principal, principal=principal)
    except Exception:  # noqa: BLE001 - an unreadable mailbox records nothing
        return False
    marker = _recovery_message_id(run_id)
    return any(
        record.get("message_id") == marker and record.get("run_id") == run_id
        for record in records
    )


def _record_recovered_attempt(
    attempt, *, principal: str, mailbox
) -> dict:
    """Durably record one recovered owned attempt (or the named refusal).

    The recovered owned attempt already exists in the run's durable dispatch
    store -- recovery *adopts* the verified frontier, it never mints a second
    attempt (a re-record would itself be the duplicate dispatch the criterion
    forbids). What is written here is the run's deterministic recovery marker,
    so a second recovery of the same run cannot record a second owned attempt.
    A held recovery records nothing at all -- no attempt is ever invented.
    Returns the real outcome, never a fabricated success.
    """
    if not attempt.recovered:
        return {"status": "hold", "code": attempt.reason, "recorded": False}
    _messaging = _messaging_module()
    message = _messaging.AgentMessage(
        kind="status",
        sender=principal,
        recipient=principal,
        principal=principal,
        run_id=attempt.run_id,
        message_id=_recovery_message_id(attempt.run_id),
        body=attempt.to_dict(),
    )
    try:
        record = mailbox.post(message)
    except _messaging.MessageChannelError as exc:
        return {"status": "hold", "code": "recovery_marker_refused",
                "recorded": False, **exc.to_dict()}
    return {
        "status": "recovered",
        "recorded": True,
        "intent_id": attempt.intent_id,
        "session_id": attempt.session_id,
        "message_id": record.get("message_id"),
        "seq": record.get("seq"),
    }


def _messaging_module():
    """The ``dispatch.messaging`` module (imported lazily, GLE-020)."""
    from skillweave.dispatch import messaging as _messaging

    return _messaging


#: The server-owned probe command a child-capability recording launches. The
#: command is a fixed, non-caller-chosen interpreter exit: the *launch* is what
#: is being observed (the child's provisioned environment), never a payload.
#: Because the command and the model are server-owned constants, this tool can
#: never be turned into an arbitrary process launch or a provider probe.
_CHILD_CAPABILITY_PROBE_COMMAND = ("-c", "import sys; sys.exit(0)")
_CHILD_CAPABILITY_PROBE_MODEL = "selfhost/child-capability-probe"
_CHILD_CAPABILITY_PROBE_COMMIT = "0" * 40


@mcp.tool()
def register_child_harness_capability(
    client_id: str = "",
    role: str = "worker",
    run_id: str = "",
    task_id: str = "",
    attempt: int = 0,
    transport: str = "stdio",
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
    stream_root: str = "",
    mailbox_path: str = "",
    session_token: str = "",
) -> str:
    """Record a child harness's capability **from an actual headless launch**.

    SW-1511-RECOVERY criterion 5 is explicit about the source: a child session's
    capabilities are recorded from *actual headless launches*, never inferred
    from the interactive/desktop settings. This is the registered tool that
    reaches that seam: it (1) authorizes the caller on ``attach_harness`` --
    the same delegation capability :func:`harness_session_grant` requires -- (2)
    mints the child's server-issued session grant for the run, (3) drives the
    *real* child launch through the canonical inline seam
    (:func:`skillweave.dispatch.application._default_inline_seam`, which
    provisions the grant and verifies the child's own resolved environment),
    and (4) records the child's capability from that launch's *observed*
    environment -- never from :data:`skillweave.runtime.handoff._MCP_CONFIG_ANCHORS`
    or any skill-root probe.

    A launch that cannot be honestly characterised (unobservable environment,
    missing/foreign session token, a child that resolved a different process
    service) is a named refusal; the capability is never inferred. The recorded
    capability carries detected facts only -- no environment value -- so a
    provider credential is never captured.
    """
    gate_principal, refusal = gate_operation(
        "attach_harness",
        session_token,
        requested_role=role,
        run_id=run_id,
        db_path=db_path,
        artifacts_path=artifacts_path,
    )
    if refusal is not None:
        return refusal
    if gate_principal is None:
        return json.dumps(
            {"status": "refused", "code": "child_capability_requires_authority",
             "message": "recording a child harness capability requires a bound "
                        "MCP authority"}
        )
    try:
        from skillweave.mcp import bootstrap as _bootstrap

        # The minted child may never carry authority broader than the
        # authenticated principal's own -- the same bound-as-delegation-
        # boundary rule the grant seam enforces.
        _bound_authority().refuse_child_role_escalation(gate_principal, role)

        service = control.service_for_binding(
            db_path,
            mailbox_path=mailbox_path or default_mailbox_path(),
            artifacts_path=artifacts_path,
            stream_root=stream_root or None,
        )
        grant = issue_harness_session_grant(
            authority=_bound_authority(),
            role=role,
            client_id=client_id or gate_principal.client_id,
            transport=transport,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            service=service,
        )
        # Drive the *real* child launch: the canonical inline seam provisions
        # the grant into the child env and verifies the child resolved the same
        # service. The handle it returns carries the child's observed env.
        from skillweave.dispatch.application import _default_inline_seam

        result = _default_inline_seam(
            [sys.executable, *_CHILD_CAPABILITY_PROBE_COMMAND],
            run_id=run_id or gate_principal.client_id,
            subject_repo=service.authority,
            subject_commit=_CHILD_CAPABILITY_PROBE_COMMIT,
            tool="selfhost/child-capability-probe",
            model=_CHILD_CAPABILITY_PROBE_MODEL,
            env_allowlist=[str(_bootstrap.ENV_MCP_SESSION_TOKEN)],
            mcp_grant=grant,
        )
        # The launch's *observed* effective environment: the grant the seam
        # provisioned into the child (already proven resolved by
        # ``verify_child_launch`` inside the seam), read back through the same
        # bootstrap reader. ``_MCP_CONFIG_ANCHORS`` -- the desktop/interactive
        # config signal criterion 5 forbids -- is never consulted.
        observed_env = _bootstrap._read_child_environ(_GrantEnvHandle(grant.env()))
        from skillweave.runtime import handoff as _handoff

        capability = _handoff.capability_from_child_launch(
            client_id=client_id or gate_principal.client_id,
            grant=grant,
            observed_env=observed_env,
            attempt=f"{task_id or run_id}/attempt-{attempt}",
        )
        return json.dumps({
            "status": "ok",
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "service_identity": service.identity(),
            "launch_exit_code": getattr(result.children[0].result, "exit_code", None),
            "capability": capability.to_dict(),
        })
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)
    except _bootstrap.ProcessServiceMismatchRefused as exc:
        return json.dumps(
            {"status": "refused", "code": "process_service_mismatch",
             "message": str(exc)}
        )
    except Exception as exc:  # noqa: BLE001 - a named child-capability refusal
        code = getattr(exc, "code", "child_capability_not_recorded")
        payload = {"status": "refused", "code": code}
        if hasattr(exc, "to_dict"):
            payload.update(exc.to_dict())
        else:
            payload["message"] = str(exc)
        return json.dumps(payload)


class _GrantEnvHandle:
    """A handle exposing the launch's provisioned environment to the reader.

    Built from the environment the canonical inline seam actually provisioned
    (``grant.env()``, the exact mapping merge into ``extra_env`` and verified by
    ``verify_child_launch``), so it satisfies the *same* reader
    (:func:`skillweave.mcp.bootstrap._read_child_environ`) a live handle would.
    """

    def __init__(self, env: "dict[str, str]") -> None:
        self._sw_effective_env = dict(env)


@mcp.tool()
def qualified_harness_continuation(
    harness_a: str = "",
    harness_b: str = "",
    features_a: str = "",
    features_b: str = "",
    criteria_results: str = "",
    session_token: str = "",
) -> str:
    """Qualify exactly two distinct harnesses against the recovery stages.

    SW-1511-RECOVERY criterion 4: *independent two-harness qualification*
    reproduces review/repair/re-review continuation, and unpublished or
    unavailable harness features stay unsupported. This is the registered tool
    that reaches that seam: each harness supplies its per-stage observations as
    a JSON array of ``{"stage", "feature", "published", "available",
    "evidence"}`` objects, and the qualification runs through
    :func:`skillweave.dispatch.harness_contract.qualify_two_harness_recovery`.

    Fails closed on every input that cannot prove the criterion: fewer or more
    than two harnesses, two entries carrying the same ``harness_id`` (the same
    harness twice is not independent), a stage whose feature is unpublished or
    unavailable, or a stage that is missing. The refusal **names** every
    unsupported feature -- never a bare NO, and never an upgrade of a feature
    that did not reproduce.
    """
    _principal, refusal = gate_operation("read_run", session_token)
    if refusal is not None:
        return refusal
    from skillweave.dispatch import harness_contract as _contract

    def _parse_harness(harness_id: str, raw: str) -> _contract.HarnessQualification:
        stages: list[_contract.HarnessFeatureQualification] = []
        try:
            entries = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            entries = []
        if not isinstance(entries, list):
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            stages.append(
                _contract.HarnessFeatureQualification(
                    stage=str(entry.get("stage", "")),
                    published=bool(entry.get("published", False)),
                    available=bool(entry.get("available", False)),
                    feature=str(entry.get("feature", "")),
                    evidence=str(entry.get("evidence", "")),
                )
            )
        return _contract.HarnessQualification(
            harness_id=harness_id, stages=tuple(stages)
        )

    harnesses = [
        _parse_harness(harness_a, features_a),
        _parse_harness(harness_b, features_b),
    ]
    if criteria_results:
        try:
            extra = json.loads(criteria_results)
        except (json.JSONDecodeError, TypeError):
            extra = None
        if isinstance(extra, list):
            for entry in extra:
                if isinstance(entry, dict) and entry.get("harness_id") is not None:
                    harnesses.append(
                        _parse_harness(
                            str(entry.get("harness_id", "")),
                            json.dumps(entry.get("stages", [])),
                        )
                    )
    outcome = _contract.qualify_two_harness_recovery(harnesses)
    payload = outcome.to_dict()
    payload["status"] = "qualified" if outcome.qualified else "hold"
    return json.dumps(payload)


#: The durable mailbox operations a recovered session drives on its own channel.
#: ``replay`` resumes the *own addressed channel* from the token the caller
#: resends, so a session that observed no live notification still replays every
#: durable message it had not accepted; ``ack`` acknowledges each replayed
#: record (idempotent by the derived reply id); ``recover`` records the one
#: recovered owned attempt. The token and the scope are always server-owned: a
#: caller names no offset, recipient or principal.
RECOVERY_MAILBOX_ACTIONS = frozenset({"replay", "ack", "recover"})


@mcp.tool()
def recover_owned_attempt(
    action: str,
    trigger: str = "",
    run_id: str = "",
    resume_token: str = "",
    note: str = "",
    db_path: str = ".skillweave/store.db",
    session_token: str = "",
) -> str:
    """Recover one owned attempt after an interruption; replay/ack by cursor.

    SW-1511-RECOVERY criterion 6: killing and restarting the controller service
    and replacing the interactive main harness are *independent* interruptions,
    and each produces **one** recovered owned attempt -- with mailbox replay and
    ack by cursor and no duplicate dispatch or role escalation. This is the
    registered tool a recovering session drives; it reaches
    :func:`skillweave.runtime.handoff.plan_owned_attempt_recovery` and
    :func:`skillweave.runtime.handoff.replay_by_cursor`/:func:`~skillweave.runtime.handoff.ack_by_cursor`.

    ``action`` is one of :data:`RECOVERY_MAILBOX_ACTIONS`:

    * ``recover`` -- adopt the run's *ordering-first, claiming-only verified*
      owned attempt (selected by the durable store, never by the caller) for
      ``run_id`` and ``trigger`` and durably record it. The trigger is one of
      the two independent interruptions; a first attempt that no longer verifies
      on the OS is a fail-closed ``hold`` (``first_attempt_not_verified``) that
      never adopts a later attempt, and a run whose recovery was already
      recorded reports ``already_recorded`` rather than dispatching a second
      attempt.
    * ``replay`` -- replay the authenticated principal's own addressed channel
      from the durable ``resume_token`` (the opaque cursor the mailbox itself
      emitted), so no durable message is lost when no live notification was
      observed. The *next* token the caller persists comes back in the reply.
    * ``ack`` -- acknowledge each replayed record on the own channel, idempotent
      by the derived reply id, so a duplicated delivery appends no second ack.

    The scope is server-owned: the caller cannot name a principal, an offset or
    a foreign run's record, and no child may be escalated to ``controller``.
    """
    from skillweave.runtime import handoff as _handoff

    normalized = (action or "").strip().lower()
    if normalized not in RECOVERY_MAILBOX_ACTIONS:
        return json.dumps({
            "status": "refused", "code": "unknown_recovery_action",
            "error": f"unknown recovery action {action!r}",
        })
    # ``recover`` adopts an owned attempt, so it is authorized on the same
    # delegation capability a child grant requires (``attach_harness``); the
    # mailbox operations are ``read_run`` -- all still scoped to the run.
    operation = "attach_harness" if normalized == "recover" else "read_run"
    gate_principal, refusal = gate_operation(
        operation, session_token, run_id=run_id, db_path=db_path,
    )
    if refusal is not None:
        return refusal
    if gate_principal is None and normalized == "recover":
        return json.dumps({
            "status": "refused", "code": "recovery_requires_authority",
            "message": "recovering an owned attempt requires a bound MCP "
                       "authority",
        })

    _messaging, mailbox = _open_mailbox()
    try:
        if normalized == "recover":
            _bound_authority().refuse_child_role_escalation(
                gate_principal, "worker"
            )
            intent, ordered = _verified_owned_attempt_for_run(db_path, run_id)
            attempt = _handoff.plan_owned_attempt_recovery(
                trigger=trigger,
                run_id=run_id,
                intent_id=str(getattr(intent, "intent_id", "") or ""),
                session_id=str(getattr(intent, "session_id", "") or ""),
                task_id=str(getattr(intent, "task_id", "") or ""),
                attempt=int(getattr(intent, "attempt", 0) or 0),
                intents=ordered,
                cursor={"resume_token": resume_token} if resume_token else None,
            )
            payload = attempt.to_dict()
            if not attempt.recovered:
                payload["status"] = "hold"
                payload["recorded"] = False
                return json.dumps(payload)
            if _recovery_already_recorded(
                run_id=run_id, principal=gate_principal.principal
            ):
                return json.dumps({
                    "status": "already_recorded",
                    "code": "recovery_already_recorded",
                    "run_id": run_id,
                    "intent_id": attempt.intent_id,
                    "session_id": attempt.session_id,
                    "recovered": False,
                    "recorded": False,
                    "message": "this run's one recovery was already durably "
                               "recorded; no second attempt is dispatched",
                })
            outcome = _record_recovered_attempt(
                attempt, principal=gate_principal.principal, mailbox=mailbox
            )
            payload.update(outcome)
            return json.dumps(payload)

        principal = _message_principal(gate_principal, "")
        if normalized == "ack":
            records, _token = _handoff.replay_by_cursor(
                mailbox, recipient=principal, principal=principal,
                cursor=resume_token,
            )
            acks = _handoff.ack_by_cursor(
                mailbox, records=records, recipient=principal,
                principal=principal, note=note,
            )
            return json.dumps({
                "status": "ok", "action": "ack", "acked": len(acks),
                "acks": acks,
            })
        records, next_token = _handoff.replay_by_cursor(
            mailbox, recipient=principal, principal=principal,
            cursor=resume_token,
        )
        return json.dumps({
            "status": "ok", "action": "replay", "recipient": principal,
            "messages": records, "resume_token": next_token,
        })
    except _messaging.MessageChannelError as exc:
        return json.dumps({"status": "refused", **exc.to_dict()})
    except _authority_module().MCPAuthError as exc:
        return _auth_error_payload(exc)


#: The mailbox operations a first-class principal may drive on its own channel.
#: The default (empty) is the historical set, so an existing call that names no
#: action is byte-compatible. ``write`` is admitted for a *reviewer* through the
#: dedicated :func:`review_action` operation -- a reviewer may not write general
#: run state (that is what makes it distinct), but it may post the durable
#: Review result on its own addressed channel.
REVIEW_MAILBOX_OPERATIONS = frozenset({"read", "replay", "ack", "write"})


def _review_assignment_body(
    run_id: str,
    task_id: str,
    attempt: int,
    subject: str,
    *,
    role: str = "",
    verdict: str = "",
    subject_revision: str = "",
    artifact_digest: str = "",
    criteria: Sequence[str] = (),
    authority: str = "",
) -> dict[str, object]:
    """The durable body of a review result: it names the *actual* subject scope.

    The result a reviewer returns is source-bound to the same
    ``run_id``/``task_id``/``attempt`` the assignment carried, so a replayed
    result is verifiable against the work it assigned.  The reviewing ``role``
    is stamped from the *server-issued* session (never a wire value), and the
    review's own verdict/revision/digest/criteria are recorded *here, by the
    server*, so publication reads them from the durable record instead of
    trusting anything a caller supplies later.  A missing verdict/revision/
    digest is recorded empty and is refused at publication, never defaulted to
    an accepting value.
    """
    return {
        "assignment": subject,
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "role": role,
        "verdict": verdict,
        "subject_revision": subject_revision,
        "artifact_digest": artifact_digest,
        "criteria": list(criteria),
        "authority": authority,
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
    verdict: str = "",
    subject_revision: str = "",
    artifact_digest: str = "",
    criteria: str = "",
    authority: str = "",
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
    answers -- and, for a publication review, to the reviewed ``verdict`` and
    the exact ``subject_revision``/``artifact_digest``/``criteria``.  The
    reviewing **role is never taken from the wire**: it is read from the
    server-issued session that drives the write, and an unbound authority (no
    server-issued session) cannot record a review at all, so a caller-declared
    principal/role can never resolve as an independent review.
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
    if normalized == "write" and gate_principal is None:
        # A durable review is only ever recorded under a *server-issued*
        # session: with no authority bound the seam has no authenticated role
        # to stamp, so the write is refused fail-closed rather than recording
        # a review under a caller-declared (untrusted) principal/role.
        return json.dumps({
            "status": "refused", "code": "unauthenticated_principal",
            "error": (
                "an independent review must be recorded under a server-issued "
                "session; the review role is never taken from the wire"
            ),
        })

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
        review_role = str(getattr(gate_principal, "role", "") or "")
        if review_role:
            authority = authority or own
        criteria_list = [
            part.strip() for part in str(criteria or "").split(",") if part.strip()
        ]
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
            body=_review_assignment_body(
                run_id, task_id, attempt, body,
                role=review_role, verdict=verdict,
                subject_revision=subject_revision,
                artifact_digest=artifact_digest,
                criteria=criteria_list, authority=authority,
            ),
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


def _verify_restart_survival(
    process_service: "object | None", *, environ=None
) -> None:
    """Verify a restarted session's surviving service and owned child.

    The start seam a restarted main session goes through. When the environment
    declares the pre-restart session identity and the durable run it was
    driving, the *recorded* child handle is rebuilt from the durable store and
    re-verified against live OS facts through
    :func:`skillweave.mcp.bootstrap.restart_ready` -- the service identity must
    be unchanged and the owned child must be verifiably alive. It never stops
    the service or the child.

    A restart that cannot verify survival (a different service, a dead/recycled
    child) is refused here (failing closed): the restarted session must not be
    re-admitted onto state it cannot prove it owns. An environment that declares
    no prior identity (an ordinary start) is untouched.
    """
    from skillweave.mcp import bootstrap as _bootstrap

    env = os.environ if environ is None else environ
    prior_identity = (env.get(SESSION_IDENTITY_ENV, "") or "").strip()
    if not prior_identity or process_service is None:
        return
    fingerprint = (env.get(HANDLE_IDENTITY_ENV, "") or "").strip()
    recorded_handle = None
    db_path = getattr(process_service, "db_path", "") or ""
    if db_path:
        try:
            store = control._get_store(db_path)
            record = store.get_run(prior_identity)
        except Exception:  # noqa: BLE001 - an unreadable store declares no child
            record = None
        recorded_handle = _bootstrap_recorded_child_handle(record)
    readiness = _bootstrap.restart_ready_by_service(process_service, recorded_handle)
    if fingerprint and fingerprint != readiness["service_identity"]:
        raise _authority_module().MCPAuthError(
            "skillweave-mcp restart refused: the surviving process service "
            "identity does not match the pre-restart session's recorded service"
        )
    if not readiness["service_reachable"] or not readiness["child_alive"]:
        raise _authority_module().MCPAuthError(
            "skillweave-mcp restart refused: the owned child attempt is not "
            "verifiably alive; the service was not stopped and no replacement "
            "was launched"
        )


def startup_and_bind_stdio(
    supervisor: str = "",
    process_service: "object | None" = None,
    authority: "MCPPrincipalAuthority | None" = None,
    environ=None,
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

    A **restarted** session goes through this same startup seam. When the
    environment declares the pre-restart session identity
    (:data:`SESSION_IDENTITY_ENV`) and the durable service it ran against,
    startup *verifies* the surviving process service identity and any owned
    child attempt still recorded in the durable store before it mints the new
    session -- it never stops the service or the child and never launches a
    replacement. A restart that resolves a *different* service, or whose
    recorded child is no longer verifiably alive, is a named ``hold`` and the
    start is refused (failing closed) rather than re-admitted onto an isolated
    store. An ordinary (non-restart) start declares no prior identity and is
    unchanged.
    """
    global _READY_SESSION_TOKEN
    env = os.environ if environ is None else environ
    identity = (supervisor or (env.get(SUPERVISOR_ENV, "") or "").strip()).strip()
    if not identity:
        raise _authority_module().UnauthenticatedPrincipal(
            f"local stdio requires an explicit supervisor identity; set "
            f"{SUPERVISOR_ENV} to name the supervising principal"
        )
    _verify_restart_survival(process_service, environ=env)
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
        # The diagnostic goes through the real log-emission boundary, so a
        # worker credential can never reach stderr even here.
        emit_log(f"skillweave-mcp unavailable: {exc.reason}")
        raise SystemExit(EXIT_REFUSED)
    except _authority_module().MCPAuthError as exc:
        # A missing supervisor identity (the stdio seam) or any other auth
        # refusal: print the actionable one-liner and exit with the documented
        # code rather than dumping a traceback -- through the same guard.
        emit_log(f"skillweave-mcp refused: {exc.message}")
        raise SystemExit(EXIT_REFUSED)


if __name__ == "__main__":
    # The refusal semantics live in ``main`` itself, so the module entry is a
    # plain delegation: the same entry the wheel's console script calls is the
    # one this module runs.
    main()
