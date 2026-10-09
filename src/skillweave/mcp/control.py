"""Shared-service MCP control surface (SW-1510-MCP-CONTROL).

This module is the *control* half of the MCP boundary and the single place the
durable, structured read/bind operations live. It exists so the MCP entry does
not re-implement run semantics: every operation here is a thin, versioned
composition over the *same* :class:`~skillweave.runsvc.service.RunApplicationService`
that the CLI drives ("MCP is transport, not the source of durable truth").

Three things the module owns:

* **Honest dependency diagnostic.** A real ``mcp`` package is required to serve
  a working server. :func:`mcp_available` reports whether it is importable and
  :func:`unavailable_payload` / :class:`McpDependencyUnavailable` carry an
  explicit, durable "unavailable" diagnostic. The legacy dummy fallback can no
  longer advertise a working server: serving raises, it does not silently
  no-op.
* **Versioned run-snapshot surface.** :func:`snapshot_v1` composes the frozen
  ``run-snapshot-v1`` through the service and *refuses* an unsupported version
  by name -- it never coerces.
* **Transport/capability negotiation.** :class:`TransportProfile` +
  :func:`negotiate_capabilities` describe stdio and scoped Streamable HTTP, and
  negotiate experimental MCP Tasks only where a profile supports them, with
  ordinary asynchronous tools + poll cursors as the fallback. A scoped HTTP
  bind is validated up front by :func:`require_scoped_bind` (loopback-only
  default, non-loopback refused without explicit opt-in, a scope token
  required) so no server is ever built for a bind that must not be served.

No ``mcp`` import appears at module import time: the package is optional and its
absence is a *reported state*, never an import-time crash.
"""

from __future__ import annotations

import hmac
import json
import os
import shlex
import sqlite3
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

# The service's contract-refusal exceptions. The run service never imports this
# module, so a module-level import here is safe (no cycle). These are the
# refusals the control surface *returns* as structured errors rather than
# letting leak across the tool boundary.
from skillweave.runsvc.service import (
    RunIntegrationError,
    RunSnapshotContractError,
)

#: The MCP protocol version this surface speaks (kept in step with the accepted
#: in-tree stdio client, ``skillweave.council.mcp_client.MCP_PROTOCOL_VERSION``).
MCP_PROTOCOL_VERSION = "2025-06-18"

#: The product name a client sees for the command server.
SERVER_NAME = "SkillWeave"

#: The namespaced control surface identity. The v1 name is a *named* version a
#: client opts into -- there is no silent default.
CONTROL_NAMESPACE = "skillweave"
CONTROL_SURFACE = "control"
CONTROL_VERSION = "skillweave/control@v1"

#: The frozen artifact/record version the control surface composes.
RUN_SNAPSHOT_VERSION = "run-snapshot-v1"

#: The snapshot versions this surface can emit. An unknown name is refused.
SUPPORTED_SNAPSHOT_VERSIONS = frozenset({RUN_SNAPSHOT_VERSION})

#: The six legacy command tools, in registration order. Preserved verbatim for
#: existing consumers; the namespaced surface is *additive* and never renames
#: or removes one of these.
LEGACY_TOOLS: tuple[str, ...] = (
    "create_run",
    "status_run",
    "cancel_run",
    "resume_run",
    "get_evidence",
    "review_run",
)

#: The SW-1510-MCP-AGENTS tools the durable handoff/mailbox surface registers on
#: the *same* production stdio server. Like :data:`LEGACY_TOOLS` they are served
#: only when the real ``mcp`` package is present, so a live ``tools/list``
#: advertises exactly the union of the two sets. Kept here -- the single place
#: the served surface is enumerated -- so a transport assertion names the whole
#: surface from one authority instead of re-listing it per test.
MCP_AGENT_TOOLS: tuple[str, ...] = (
    "mailbox_post",
    "mailbox_read",
    "mailbox_replay",
    "mailbox_ack",
    "agent_capabilities",
)


def mcp_tool_names() -> tuple[str, ...]:
    """The complete served tool surface, sorted -- legacy six + MCP-AGENTS tools.

    Every exact-tool-list assertion over a *real* transport (stdio or scoped
    HTTP) should compare against this rather than a hand-maintained literal, so
    adding a tool to the served surface cannot silently drift from the tests
    that pin it. The result matches the wire form a client sees:
    ``','.join(sorted(t.name for t in tools.tools))``.
    """
    return tuple(sorted(LEGACY_TOOLS + MCP_AGENT_TOOLS))

#: The explicit diagnostic returned when the ``mcp`` dependency is missing.
#: Phrased as a durable state, not a stack trace: a host can surface it and an
#: operator can act on it.
DEGRADED_REASON = (
    "the 'mcp' package is required to serve MCP but is not importable; "
    "install the MCP dependency to expose a working server — the command "
    "server refuses to serve rather than advertise a non-functional surface"
)

#: The default loopback host a scoped HTTP bind uses when none is named.
DEFAULT_HTTP_HOST = "127.0.0.1"

#: The default port a scoped HTTP bind uses when none is named.
DEFAULT_HTTP_PORT = 8765

#: Environment variable carrying the scope token required for a scoped HTTP
#: bind. A client-facing HTTP surface must be scoped to an authenticated
#: caller; there is no anonymous default.
HTTP_AUTH_ENV = "SKILLWEAVE_MCP_HTTP_TOKEN"

#: Hosts treated as loopback. A bind outside this set is refused unless the
#: caller explicitly opts into a remote bind.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: A tool a control client calls. ``async_tool`` marks an ordinary asynchronous
#: tool (the fallback execution shape when experimental Tasks are unavailable).
@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    handler: Callable[..., Any]
    async_tool: bool = True
    poll_cursor: bool = False


class McpDependencyUnavailable(RuntimeError):
    """The ``mcp`` package is required to serve but cannot be imported."""

    def __init__(self, reason: str = DEGRADED_REASON):
        self.reason = reason
        super().__init__(reason)


class ScopedHttpRefused(RuntimeError):
    """A scoped Streamable HTTP bind was refused before any server was built.

    The refusal is *structural*: a non-loopback bind without explicit remote
    opt-in, or a missing scope token. ``reason`` names the refused bind so a
    caller can act on it without inspecting a stack trace.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ControlStorageError(RuntimeError):
    """A control operation could not reach its durable store.

    Carries a *string* ``reason`` (never the raw sqlite exception object, whose
    ``str`` may embed host filesystem details) so a caller can return a
    structured diagnostic rather than leaking ``sqlite3.OperationalError``.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Dependency honesty
# ---------------------------------------------------------------------------

def mcp_available() -> bool:
    """Whether a real ``mcp`` server package is importable *now*."""
    try:
        import mcp.server.fastmcp  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure is "unavailable"
        return False
    return True


def require_mcp() -> Any:
    """Import and return the real ``mcp.server.fastmcp`` module.

    Raises :class:`McpDependencyUnavailable` (never returns a stand-in) when the
    dependency is missing, so a caller cannot silently proceed with a dummy.
    """
    try:
        import mcp.server.fastmcp as _fastmcp
    except Exception as exc:  # noqa: BLE001
        raise McpDependencyUnavailable(
            f"{DEGRADED_REASON} (import error: {type(exc).__name__}: {exc})"
        ) from exc
    return _fastmcp


def unavailable_payload(**extra: Any) -> dict[str, Any]:
    """A structured, durable unavailable diagnostic (no fabricated success)."""
    payload = {
        "status": "unavailable",
        "available": False,
        "reason": DEGRADED_REASON,
        "namespace": CONTROL_NAMESPACE,
        "surface": CONTROL_SURFACE,
        "version": CONTROL_VERSION,
        "legacy_tools_preserved": list(LEGACY_TOOLS),
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Scoped HTTP bind contract
# ---------------------------------------------------------------------------

def require_scoped_bind(
    host: str,
    *,
    auth_token: str = "",
    allow_remote: bool = False,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[str, str]:
    """Validate a scoped Streamable HTTP bind *before* any server is built.

    Returns the ``(effective_host, token)`` a scoped bind would use, or raises
    :class:`ScopedHttpRefused` -- a structural refusal, so no server is ever
    built for a bind that must not be served:

    * the host defaults to :data:`DEFAULT_HTTP_HOST` (loopback), so the *relied
      upon* default is a loopback bind, not a fail-open wildcard;
    * a non-loopback host is refused unless *allow_remote* is explicitly set;
    * a scope token is required (the ``auth_token`` argument or
      :data:`HTTP_AUTH_ENV`). A token is never echoed in the refusal reason.
    """
    env = os.environ if environ is None else environ
    effective_host = host or DEFAULT_HTTP_HOST
    if effective_host not in LOOPBACK_HOSTS and not allow_remote:
        raise ScopedHttpRefused(
            f"refusing to bind scoped HTTP to non-loopback host "
            f"{effective_host!r} without explicit remote opt-in "
            f"(allow_remote=True)"
        )
    token = auth_token or env.get(HTTP_AUTH_ENV, "")
    if not token:
        raise ScopedHttpRefused(
            f"refusing to serve scoped HTTP without a scope token; pass "
            f"auth_token or set {HTTP_AUTH_ENV}"
        )
    return effective_host, token


# ---------------------------------------------------------------------------
# Per-request scoped HTTP enforcement (SW-1510-MCP-AUTH criterion 3)
# ---------------------------------------------------------------------------
#
# ``require_scoped_bind`` validates the *bind* (loopback, token present) before
# any server is built. That is necessary but **not sufficient**: a token checked
# only at startup does not authenticate a *request*. This section enforces the
# scope token on **every** HTTP request, before any MCP handling, and binds the
# authenticated principal into the per-operation authority so the per-tool
# gating sees an authenticated principal over HTTP (not only over stdio).

#: The WWW-Authenticate challenge returned with a 401. It names no token and
#: echoes nothing the caller sent.
WWW_AUTHENTICATE = "Bearer"

#: The refusal record body for a failed HTTP authentication. It is a constant:
#: it never carries the presented token, the expected token or any host detail.
HTTP_UNAUTHENTICATED_BODY = b'{"error":"unauthenticated","code":"http_unauthenticated"}'


class HttpRequestUnauthorized(Exception):
    """An HTTP request failed the per-request scope-token check.

    Raised by the request guard so the transport returns ``401`` with a
    ``WWW-Authenticate`` challenge and no MCP handling runs. ``reason`` is a
    short, token-free diagnostic for the operator's own logs.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def strip_bearer_scheme(header: str) -> str:
    """The token from an ``Authorization`` header value (any case), else ``""``.

    ``Authorization: Bearer <token>`` is the scheme this surface issues. A bare
    token (no scheme) is accepted for operator-facing clients that send one; an
    absent header yields ``""``. It never raises on a malformed header -- a
    malformed credential is simply an absent one.
    """
    value = (header or "").strip()
    if not value:
        return ""
    if " " in value:
        scheme, _, rest = value.partition(" ")
        if scheme.lower() == "bearer":
            return rest.strip()
        # An unrecognized scheme is not a credential this surface issued.
        return ""
    return value


def bearer_token_from_headers(headers: Mapping[str, Any]) -> str:
    """The bearer token from a request's headers (case-insensitive lookup)."""
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    value = ""
    if callable(getter):
        # Starlette/ASGI headers are case-insensitive; a plain dict is not.
        value = headers.get("authorization") or headers.get("Authorization") or ""
    return strip_bearer_scheme(str(value))


def tokens_match(presented: str, expected: str) -> bool:
    """Constant-time token comparison (never short-circuits on length)."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)


def check_http_authorization(headers: Mapping[str, Any], token: str) -> str:
    """Return the request's bearer token, or refuse with a token-free 401.

    The per-request half of the scoped HTTP contract: a missing or wrong token
    is refused *before* any MCP handling. Constant-time comparison; the
    presented value is never logged or echoed.
    """
    presented = bearer_token_from_headers(headers)
    if not presented:
        raise HttpRequestUnauthorized("missing bearer token")
    if not tokens_match(presented, token):
        raise HttpRequestUnauthorized("invalid bearer token")
    return presented


#: The session authority a served HTTP request resolves its principal against.
#: Bound once at serve time (``bind_http_authority``); the request middleware
#: verifies the presented token and binds the principal for the call. Process-
#: local, never the wire.
_HTTP_AUTHORITY: Any = None


def bind_http_authority(authority: Any) -> Any:
    """Bind the authority the HTTP request middleware authenticates against."""
    global _HTTP_AUTHORITY
    _HTTP_AUTHORITY = authority
    return authority


def http_authority() -> Any:
    """The authority bound for the HTTP serving path, or ``None``."""
    return _HTTP_AUTHORITY


#: The authenticated principal for the *current* request. The per-request
#: middleware sets it; a protected HTTP tool call reads it. A ``ContextVar`` so
#: concurrent requests never see each other's principal. Reset to ``None``
#: between requests, so one request can never inherit another's authority.
_CURRENT_HTTP_PRINCIPAL: "ContextVar[Any]" = ContextVar(
    "skillweave_http_principal", default=None
)

#: The presented bearer token for the *current* request. A protected HTTP tool
#: call resolves its principal from this (the client presents the token on the
#: Authorization header, not in each tool's arguments). Reset between requests
#: so a token never leaks across requests; the value is never logged.
_CURRENT_HTTP_TOKEN: "ContextVar[str]" = ContextVar(
    "skillweave_http_token", default=""
)


def current_http_principal() -> Any:
    """The authenticated principal of the current HTTP request, or ``None``."""
    return _CURRENT_HTTP_PRINCIPAL.get()


def current_http_token() -> str:
    """The bearer token of the current HTTP request (empty outside a request)."""
    return _CURRENT_HTTP_TOKEN.get()


def _load_asgi_http_types():
    """The Starlette request/response types the HTTP middleware uses.

    Imported lazily: ``starlette`` ships with the optional ``mcp`` dependency,
    so this module still imports (and its dependency diagnostic still works)
    where ``starlette`` is absent. ``None`` when unavailable.
    """
    try:
        from starlette.requests import Request as _Request
        from starlette.responses import PlainTextResponse as _Resp
    except Exception:  # noqa: BLE001 - absence is a reported state, not a crash
        return None, None
    return _Request, _Resp


def _make_http_auth_middleware(token: str, *, authority: Any = None):
    """Build the ASGI middleware enforcing *token* on every request.

    The middleware runs ahead of the MCP app: it authenticates each request,
    binds the resolved principal for the call (when an *authority* is bound),
    and only then hands the request to the MCP app. A failure returns ``401``
    with ``WWW-Authenticate: Bearer`` and a constant body -- no MCP handler is
    reached and no token is echoed.
    """
    request_cls, response_cls = _load_asgi_http_types()

    if request_cls is None:
        # Without starlette there is no HTTP app to wrap; the caller must have
        # the same dependency, so this is only reached on a broken install.
        raise McpDependencyUnavailable(
            "scoped HTTP serving requires the 'starlette' dependency "
            "(installed with 'mcp')"
        )

    class _ScopedHttpAuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") != "http":
                await self.app(scope, receive, send)
                return
            request = request_cls(scope)
            try:
                presented = check_http_authorization(request.headers, token)
            except HttpRequestUnauthorized as exc:
                # No token in the log line; the reason names only the failure.
                print(
                    f"skillweave-mcp http refused: {exc.reason}", file=sys.stderr
                )
                response = response_cls(
                    HTTP_UNAUTHENTICATED_BODY.decode("utf-8"),
                    status_code=401,
                    headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                )
                await response(scope, receive, send)
                return
            auth = authority if authority is not None else _HTTP_AUTHORITY
            # When an authority is bound, resolve the principal the presented
            # token names and attach it for the call; a token that passed the
            # comparison but is unknown to the registry is refused. With no
            # authority bound (direct in-process serving) the token check above
            # still governs admission and no per-request principal is attached.
            principal = None
            if auth is not None:
                try:
                    principal = auth.verify_session(presented)
                except Exception:  # noqa: BLE001 - any failure is unauth
                    response = response_cls(
                        HTTP_UNAUTHENTICATED_BODY.decode("utf-8"),
                        status_code=401,
                        headers={"WWW-Authenticate": WWW_AUTHENTICATE},
                    )
                    await response(scope, receive, send)
                    return
            reset_p = _CURRENT_HTTP_PRINCIPAL.set(principal)
            reset_t = _CURRENT_HTTP_TOKEN.set(presented)
            try:
                await self.app(scope, receive, send)
            finally:
                _CURRENT_HTTP_PRINCIPAL.reset(reset_p)
                _CURRENT_HTTP_TOKEN.reset(reset_t)

    return _ScopedHttpAuthMiddleware


def _load_fastmcp() -> Any:
    """Return the ``mcp.server.fastmcp`` module used to build servers.

    This is the single *injectable seam* for the serving path: a test replaces
    it with a fake FastMCP module to pin exactly *what* :func:`serve` would run
    without a live ``mcp`` install (see ``tests/unit/test_1510_mcp_control.py``).
    The default is the real dependency, so production and the stub tests share
    one code path instead of the test asserting against a re-implementation.
    """
    return require_mcp()


def _build_server(fastmcp: Any):
    """Build the real ``FastMCP`` server carrying the complete served surface.

    The scoped HTTP transport registers exactly what the production stdio
    server (``skillweave.mcp.server``) registers: the six legacy command tools
    *plus* the SW-1510-MCP-AGENTS durable handoff/mailbox tools. Both are
    resolved lazily from the same module, so a transport cannot advertise a
    surface the other lacks -- and every tool carries the module's own
    ``gate_operation`` authorization gate, over the same host-bound authority.
    """
    server = fastmcp.FastMCP(SERVER_NAME)
    for spec in (*legacy_tool_specs(), *mcp_agent_tool_specs()):
        server.tool()(spec.handler)
    return server


def _apply_scoped_settings(server: Any, host: str, port: int) -> None:
    """Bind the scoped host/port onto a server's settings when it exposes them."""
    settings = getattr(server, "settings", None)
    if settings is not None:
        settings.host = host
        if port:
            settings.port = port


def serve_legacy_stdio() -> None:
    """Serve the six legacy tools over a real stdio MCP transport.

    This is the *only* way the command server may advertise itself as working.
    With no ``mcp`` package it raises :class:`McpDependencyUnavailable` -- the
    server refuses rather than serving an empty dummy.
    """
    fastmcp = _load_fastmcp()
    _build_server(fastmcp).run()


def serve(
    transport: str = "stdio",
    *,
    host: str = "",
    port: int = 0,
    auth_token: str = "",
    allow_remote: bool = False,
    fastmcp_factory: Optional[Callable[[], Any]] = None,
) -> None:
    """Serve over ``stdio`` or a *scoped* Streamable HTTP transport.

    The transport is *honoured*, not described: ``stdio`` runs the stdio server
    and ``http`` runs the scoped Streamable HTTP server (the real
    ``streamable-http`` transport). An unknown transport is refused before any
    server is built, and a missing ``mcp`` package refuses with the explicit
    diagnostic.

    The scoped HTTP bind is validated **before any server is built or run**
    (:func:`require_scoped_bind`): it defaults to loopback, refuses a
    non-loopback host unless *allow_remote* is set, and requires a scope token
    (``auth_token`` or :data:`HTTP_AUTH_ENV`). *fastmcp_factory* overrides the
    FastMCP module loader (the injectable seam); production leaves it unset.
    """
    profile = transport_profile(transport, host=host, port=port)
    if not profile.supported:
        raise McpDependencyUnavailable(profile.reason or DEGRADED_REASON)
    loader = fastmcp_factory or _load_fastmcp
    if profile.scoped:
        effective_host, token = require_scoped_bind(
            host, auth_token=auth_token, allow_remote=allow_remote
        )
        effective_port = port or DEFAULT_HTTP_PORT
        # The scope token authenticates *requests*, not merely the bind. Bind
        # it into a live session so the request guard can attach an
        # authenticated principal to each call (the per-tool gating then sees
        # a principal over HTTP, exactly as it does over stdio).
        _bind_http_scope_principal(token, effective_host, effective_port)
        server = _build_server(loader())
        _apply_scoped_settings(server, effective_host, effective_port)
        _install_http_request_guard(server, token)
        server.run(transport="streamable-http")
        return
    _build_server(loader()).run()


def _bind_http_scope_principal(token: str, host: str, port: int) -> Any:
    """Register *token* as the live HTTP-serving principal on a bound authority.

    The request middleware needs a session the presented token resolves to. The
    server module owns the process authority (:func:`skillweave.mcp.server.
    bind_authority`); when it is bound we register the scope token there so the
    authenticated principal carries the controller role and its scoped
    operations. When no authority is bound (direct in-process serving) this is
    a no-op -- the request guard still enforces the token, but there is no
    per-operation principal to attach, and the tool-level gate remains the
    unbound direct-caller path. Returns the authority, or ``None``.
    """
    try:
        from skillweave.mcp import server as _server
    except Exception:  # noqa: BLE001 - never fatal to serving
        return None
    authority = _server.get_authority()
    if authority is None:
        return None
    authority.register_scope_token(
        token,
        role="controller",
        client_id=f"http:{host}:{port}",
        transport="http",
        allowed_routes=["skillweave-local"],
    )
    bind_http_authority(authority)
    return authority


def _install_http_request_guard(server: Any, token: str) -> None:
    """Install the per-request bearer guard on *server*'s HTTP surface.

    FastMCP exposes ``streamable_http_app()`` (a Starlette app) and a
    ``settings`` object carrying middleware hooks. The most portable seam that
    does **not** reimplement the transport is to wrap the built ASGI app: we
    expose a guarded ``streamable_http_app`` on the server so the MCP stack
    builds its app through us. Where the server exposes no such hook (the fake
    test seam), the wrap is recorded on the server for the contract to observe.
    """
    middleware = _make_http_auth_middleware(token, authority=http_authority())

    original = getattr(server, "streamable_http_app", None)
    if callable(original):
        def guarded(*args, **kwargs):
            app = original(*args, **kwargs)
            return middleware(app)

        try:
            server.streamable_http_app = guarded
        except Exception:  # noqa: BLE001 - read-only attr; fall through
            pass
    # Record the guard so the serving contract is observable even on a server
    # whose app is built internally (and for the injected-fake seam).
    try:
        server.http_auth_middleware = middleware
    except Exception:  # noqa: BLE001
        pass


def serve_scoped_http(
    *,
    host: str = "",
    port: int = 0,
    auth_token: str = "",
    allow_remote: bool = False,
    fastmcp_factory: Optional[Callable[[], Any]] = None,
) -> None:
    """Serve the scoped Streamable HTTP transport (loopback + scope token)."""
    serve(
        "http",
        host=host,
        port=port,
        auth_token=auth_token,
        allow_remote=allow_remote,
        fastmcp_factory=fastmcp_factory,
    )


# ---------------------------------------------------------------------------
# Service composition (the single service both CLI and MCP drive)
# ---------------------------------------------------------------------------

def _ensure_parent_dir(path: str) -> None:
    """Create the parent directory of a file-backed path (no-op for ``:memory:``).

    The CLI creates this directory at ingress; the control surface must too, or
    a first control call in a fresh project fails to open the run store. A
    directory-creating failure is surfaced as :class:`ControlStorageError` (a
    durable string reason), never a raw ``OSError``.
    """
    if not path or path == ":memory:":
        return
    parent = os.path.dirname(os.path.abspath(path))
    if not parent:
        return
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise ControlStorageError(
            f"cannot create storage directory {parent!r}: {type(exc).__name__}"
        ) from exc


def _get_store(db_path: str = ".skillweave/store.db"):
    from skillweave.runtime.store import SQLiteRunStore

    _ensure_parent_dir(db_path)
    try:
        return SQLiteRunStore(db_path)
    except sqlite3.OperationalError as exc:
        raise ControlStorageError(
            f"cannot open run store {db_path!r}: {type(exc).__name__}"
        ) from exc


def default_stream_root(db_path: str) -> str:
    """The durable stream root used when the caller does not name one."""
    if db_path in ("", ":memory:"):
        import tempfile

        return os.path.join(tempfile.mkdtemp(prefix="skillweave-stream-"), "streams")
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    return os.path.join(db_dir, "streams")


def get_run_service(
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
    stream_root: Optional[str] = None,
):
    """Build the same :class:`RunApplicationService` the CLI ingress builds.

    The run store's parent directory is created up front, mirroring the CLI
    ingress, so a first control call in a fresh project succeeds instead of
    failing to open the database. The directory-backed stores do not need this:
    :class:`DurableStreamStore` creates its own root, and the raw artifact store
    is memory-backed, so neither is pre-created here (a read must not have the
    side effect of creating an artifacts directory on disk). A store that still
    cannot be opened is surfaced as :class:`ControlStorageError`.
    """
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.reservation import ReservationStore
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.runsvc.service import RunApplicationService

    _ensure_parent_dir(db_path)

    store = _get_store(db_path)
    journal = EventJournal(db_path)
    raw_artifacts = RawArtifactStore(artifacts_path)
    stream_store = DurableStreamStore(root=stream_root) if stream_root else None
    reservation_store = ReservationStore(db_path)
    return RunApplicationService(
        store,
        journal,
        raw_artifacts,
        stream_store,
        reservation_store=reservation_store,
    )


def artifact_evidence_list(artifact_evidence: str):
    """Parse the artifact proof graph(s); ``None`` when absent."""
    if not artifact_evidence:
        return None
    payload = json.loads(artifact_evidence)
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("artifact_evidence must be a JSON object or a list of objects")


def source_binding(source_root: str, subject_commit: str):
    if not source_root:
        return None
    from skillweave.runtime.verify import FilesystemSourceResolver

    return FilesystemSourceResolver(source_root, commit=subject_commit)


def analysis_binding(analysis_root: str):
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


# ---------------------------------------------------------------------------
# Structured results: contract refusals are returned, not raised
# ---------------------------------------------------------------------------

def structured_error(
    code: str,
    message: str,
    *,
    run_id: str = "",
    version: str = "",
    supported: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """A durable, structured error record a control tool can *return*.

    Every control operation returns either a result or one of these: an
    exception is never leaked across the tool boundary. ``code`` is a stable
    machine-readable token; ``supported`` names the versions a refusal would
    accept.
    """
    payload: dict[str, Any] = {
        "error": message,
        "code": code,
        "run_id": run_id,
        "version": version,
        "supported": list(supported) if supported is not None else [],
    }
    return payload


def _snapshot_error_payload(
    run_id: str,
    *,
    version: str = "",
    exc: Optional[Exception] = None,
) -> dict[str, Any]:
    """Map a snapshot contract refusal onto the structured error record."""
    if isinstance(exc, SnapshotVersionRefused):
        return structured_error(
            "unsupported_version",
            f"unsupported run-snapshot version {version!r}",
            run_id=run_id,
            version=version,
            supported=sorted(SUPPORTED_SNAPSHOT_VERSIONS),
        )
    reason = getattr(exc, "reason", None) or "refused"
    return structured_error(
        "snapshot_unavailable",
        str(reason),
        run_id=run_id,
        version=version or RUN_SNAPSHOT_VERSION,
        supported=sorted(SUPPORTED_SNAPSHOT_VERSIONS),
    )


def _storage_error_payload(exc: Exception, **extra: Any) -> dict[str, Any]:
    reason = getattr(exc, "reason", None) or "storage unavailable"
    return structured_error("storage_unavailable", str(reason), **extra)


def _contract_guard(
    run_id: str, *, version: str = "", exc: Exception
) -> dict[str, Any]:
    """Route a service-refusal exception to the right structured payload."""
    if isinstance(exc, ControlStorageError):
        return _storage_error_payload(exc, run_id=run_id)
    return _snapshot_error_payload(run_id, version=version, exc=exc)


def _not_found(run_id: str, *, version: str = "") -> dict[str, Any]:
    """The durable not-found record (message preserved for legacy callers)."""
    return structured_error("not_found", "Run not found", run_id=run_id, version=version)


def _open_service(
    db_path: str,
    artifacts_path: str,
    stream_root: Optional[str] = None,
):
    """Build the shared service, mapping storage failures to a typed error."""
    try:
        return get_run_service(db_path, artifacts_path, stream_root)
    except ControlStorageError:
        raise
    except sqlite3.OperationalError as exc:
        raise ControlStorageError(
            f"cannot open run service for {db_path!r}: {type(exc).__name__}"
        ) from exc


def create_run_payload(
    *,
    tool: str,
    model: str,
    subject_repo: str,
    subject_commit: str,
    command: str,
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
    methodology: str = "ralph",
    autonomy_policy: str = "",
    task_id: str = "",
    attempt: int = 1,
    output_root: str = "",
    stream_root: str = "",
    artifact_evidence: str = "",
    source_root: str = "",
    analysis_root: str = "",
    route_proof: str = "",
) -> dict[str, Any]:
    """Execute one native run through the shared service and return its payload.

    This is the single execution ingress the command server's ``create_run``
    delegates to. It mints the run id, honours/creates the owner budget
    envelope, drives ``execute_streaming_native`` and returns the durable
    structured payload (run/state/gate/digest/stream[/assay]).

    First-run behaviour: the run store's parent directory is created here (the
    durable stream store creates its own root), so a run started from a fresh
    project cwd *creates* its store rather than failing with
    ``unable to open database file``. A store/contract failure is *returned* as
    a structured error dict, never raised across the tool boundary.
    """
    from skillweave.dispatch.application import generate_run_id

    try:
        svc = _open_service(
            db_path, artifacts_path, stream_root or default_stream_root(db_path)
        )
    except (ControlStorageError, RunSnapshotContractError, RunIntegrationError) as exc:
        return _contract_guard("", exc=exc)
    run_id = generate_run_id()
    from skillweave.runtime.reservation import ReservationEnvelope as _ReservationEnvelope

    # SW-1510-ROUTE-LOCK: load the route-proof document when supplied.
    # A malformed route proof produces a structured route_lock error
    # (exit nonzero, no traceback).
    route_proof_obj = None
    if route_proof:
        from skillweave.routing.route_proof import RouteProof as _RouteProof, RouteProofError
        try:
            route_proof_obj = _RouteProof.load(route_proof)
        except RouteProofError as exc:
            return _contract_guard(
                "",
                exc=RunIntegrationError(
                    "route_lock",
                    f"malformed route proof: {exc}",
                ),
            )

    try:
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
            artifact_evidence=artifact_evidence_list(artifact_evidence),
            source_resolver=source_binding(source_root, subject_commit),
            analysis_reader=analysis_binding(analysis_root),
            route_proof=route_proof_obj,
        )
    except (ControlStorageError, RunSnapshotContractError, RunIntegrationError) as exc:
        return _contract_guard("", exc=exc)

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
    return payload


# ---------------------------------------------------------------------------
# Versioned control operations
# ---------------------------------------------------------------------------

def _require_snapshot_version(version: str) -> None:
    if version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise SnapshotVersionRefused(version)


class SnapshotVersionRefused(RuntimeError):
    """The client named an unsupported run-snapshot version."""

    def __init__(self, version: str):
        self.version = version
        super().__init__(
            f"unsupported run-snapshot version {version!r}; this surface emits "
            f"one of {sorted(SUPPORTED_SNAPSHOT_VERSIONS)}"
        )


def snapshot_v1(
    run_id: str,
    *,
    version: str = RUN_SNAPSHOT_VERSION,
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
) -> dict[str, Any]:
    """Compose the frozen ``run-snapshot-v1`` for ``run_id``.

    A contract refusal -- an unsupported ``version`` name, or a run whose
    trusted fields cannot be expressed in the frozen version -- is *returned* as
    a structured error dict, never raised across the tool boundary.
    """
    try:
        _require_snapshot_version(version)
        svc = _open_service(db_path, artifacts_path)
        snapshot = svc.run_snapshot_v1(run_id, version=version)
    except (
        SnapshotVersionRefused,
        ControlStorageError,
        RunSnapshotContractError,
    ) as exc:
        return _contract_guard(run_id, version=version, exc=exc)
    if snapshot is None:
        return _not_found(run_id, version=version)
    return snapshot


def control_status(
    run_id: str,
    *,
    version: str = "",
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
) -> dict[str, Any]:
    """Read a run's state.

    With no ``version`` the durable shared snapshot (``run_snapshot``) is
    returned unchanged (legacy shape). With an explicit ``version`` the frozen
    versioned record ``run-snapshot-v1`` is returned; an unknown name or an
    unrepresentable run is *returned* as a structured error dict, not raised.
    """
    try:
        svc = _open_service(db_path, artifacts_path)
        if not version:
            snapshot = svc.run_snapshot(run_id)
            if snapshot is None:
                return _not_found(run_id)
            return snapshot
        _require_snapshot_version(version)
        snapshot = svc.run_snapshot_v1(run_id, version=version)
    except (
        SnapshotVersionRefused,
        ControlStorageError,
        RunSnapshotContractError,
    ) as exc:
        return _contract_guard(run_id, version=version, exc=exc)
    if snapshot is None:
        return _not_found(run_id, version=version)
    return snapshot


def control_evidence(
    run_id: str,
    *,
    db_path: str = ".skillweave/store.db",
) -> dict[str, Any]:
    """Return a run's ordered journal evidence (the durable record)."""
    from skillweave.runtime.journal import EventJournal

    try:
        _ensure_parent_dir(db_path)
        journal = EventJournal(db_path)
        events = journal.get_events(run_id)
    except (ControlStorageError, sqlite3.OperationalError) as exc:
        if isinstance(exc, ControlStorageError):
            return _storage_error_payload(exc, run_id=run_id)
        return _storage_error_payload(
            ControlStorageError(
                f"cannot open run journal {db_path!r}: {type(exc).__name__}"
            ),
            run_id=run_id,
        )
    try:
        events = [e.to_dict() if hasattr(e, "to_dict") else e for e in events]
    except Exception:  # noqa: BLE001
        pass
    return {"run_id": run_id, "events": events}


def control_cancel(
    run_id: str,
    *,
    reason: str = "operator_halt",
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
) -> dict[str, Any]:
    """Cancel through the native lifecycle entry (identity-checked).

    A refusal (recycled PID / stale command / foreign worker) is returned as a
    structured refusal carrying the verification reason; nothing is signalled.
    A storage/service contract refusal is likewise returned, not raised.
    """
    from skillweave.runtime.errors import ProcessIdentityError, StoreError

    try:
        svc = _open_service(db_path, artifacts_path)
        if svc.store.get_run(run_id) is None:
            return _not_found(run_id)
        result = svc.cancel_process(run_id, reason=reason)
    except ProcessIdentityError as exc:
        return {
            "error": "process identity not verified; cancellation refused",
            "code": "identity_unverified",
            "run_id": run_id,
            "version": "",
            "supported": [],
            "reason": exc.reason,
            "verification": exc.verification,
        }
    except (ControlStorageError, RunSnapshotContractError, RunIntegrationError) as exc:
        return _contract_guard(run_id, exc=exc)
    except StoreError as exc:
        return structured_error("store_error", str(exc), run_id=run_id)
    return {"status": "cancel_requested", **result}


def control_resume(
    run_id: str,
    *,
    command: str = "",
    idempotency_key: str = "",
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
) -> dict[str, Any]:
    """Resume a run.

    With no *command* this reconciles stored intents and reports the run's
    actual state with ``restarted: false`` (a transition alone never claims a
    restart). With a *command* it launches a real NEW identified attempt through
    the native service, preserving *idempotency_key* so a replay returns the
    already-launched attempt. Contract/storage refusals are returned, not
    raised.
    """
    from skillweave.runtime.errors import StoreError

    try:
        svc = _open_service(db_path, artifacts_path)
        if not command:
            snapshot = svc.resume_run(run_id)
            if snapshot is None:
                return _not_found(run_id)
            return {
                "status": "reconciled",
                "restarted": False,
                "note": "no command supplied; no new attempt launched",
                **snapshot,
            }
        result = svc.restart_attempt(
            run_id,
            shlex.split(command),
            idempotency_key=idempotency_key or None,
        )
    except (ControlStorageError, RunSnapshotContractError, RunIntegrationError) as exc:
        return _contract_guard(run_id, exc=exc)
    except StoreError as exc:
        return structured_error("store_error", str(exc), run_id=run_id)
    return {
        "status": "restarted",
        "restart_kind": "new_identified_attempt",
        **result,
    }


def control_poll(
    run_id: str,
    *,
    after_cursor: int = 0,
    limit: int = 0,
    db_path: str = ".skillweave/store.db",
    artifacts_path: str = ".skillweave/artifacts",
    stream_root: str = "",
) -> dict[str, Any]:
    """Poll a run's ordered progress frames from ``after_cursor``.

    This is the *fallback* async shape -- a cursor a client advances -- used
    when experimental MCP Tasks are not negotiated for the transport. A storage
    refusal is returned as a structured error, not raised.
    """
    try:
        svc = _open_service(
            db_path, artifacts_path, stream_root or default_stream_root(db_path)
        )
        frames = svc.stream_frames(
            run_id, after_cursor=after_cursor, limit=limit or None
        )
    except (ControlStorageError, RunSnapshotContractError, RunIntegrationError) as exc:
        return _contract_guard(run_id, exc=exc)
    latest = frames[-1]["cursor"] if frames else after_cursor
    return {
        "run_id": run_id,
        "after_cursor": after_cursor,
        "latest_cursor": latest,
        "frame_count": len(frames),
        "frames": frames,
        "execution_shape": "async_tool_poll_cursor",
    }


# ---------------------------------------------------------------------------
# Transport + capability negotiation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TransportProfile:
    """A describe-able transport the control surface can serve over."""

    kind: str
    protocol_version: str = MCP_PROTOCOL_VERSION
    supported: bool = True
    scoped: bool = False
    experimental_tasks: bool = False
    url: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "kind": self.kind,
            "protocol_version": self.protocol_version,
            "supported": self.supported,
            "scoped": self.scoped,
            "experimental_tasks": self.experimental_tasks,
            "url": self.url,
        }
        if self.reason:
            out["reason"] = self.reason
        return out


#: The transports this surface knows how to describe. ``stdio`` is always
#: available; ``http`` is the scoped Streamable HTTP transport.
SUPPORTED_TRANSPORTS = frozenset({"stdio", "http"})


def transport_profile(kind: str, *, host: str = "", port: int = 0) -> TransportProfile:
    """Describe the transport *kind* the control surface would serve over.

    An unknown transport kind is *unsupported* (``supported=False`` with a
    reason) rather than silently mapped to stdio -- a client cannot be handed a
    transport it did not ask for.
    """
    if kind not in SUPPORTED_TRANSPORTS:
        return TransportProfile(
            kind=kind,
            supported=False,
            reason=(
                f"unsupported transport {kind!r}; this surface serves "
                f"{sorted(SUPPORTED_TRANSPORTS)}"
            ),
        )
    scoped = kind == "http"
    url = f"http://{host or '127.0.0.1'}:{port or 0}/mcp" if scoped else ""
    return TransportProfile(
        kind=kind,
        supported=True,
        scoped=scoped,
        experimental_tasks=scoped,
        url=url,
    )


def server_capabilities(profile: TransportProfile) -> dict[str, Any]:
    """The MCP capabilities the surface advertises for *profile*.

    ``tools`` is always advertised. Experimental ``tasks`` is advertised *only*
    where the profile supports it (the scoped HTTP transport).
    """
    caps: dict[str, Any] = {"tools": {}}
    if profile.experimental_tasks:
        caps["experimental"] = {"tasks": {}}
    return caps


def negotiate_capabilities(
    client_capabilities: Mapping[str, Any],
    profile: TransportProfile,
) -> dict[str, Any]:
    """Negotiate capabilities from a client's request and the profile.

    Experimental MCP Tasks are negotiated **only** when *both* the profile
    supports them and the client requested them; otherwise the negotiated
    execution shape is the ordinary asynchronous tool + poll-cursor fallback.
    The returned record never claims a capability the transport cannot serve.
    """
    requested = client_capabilities or {}
    requested_tasks = bool(
        (requested.get("experimental") or {}).get("tasks") is not None
    )
    tasks_negotiated = bool(profile.experimental_tasks and requested_tasks)
    return {
        "protocol_version": profile.protocol_version,
        "transport": profile.kind,
        "scoped": profile.scoped,
        "tools": True,
        "tasks": {
            "supported": profile.experimental_tasks,
            "requested": requested_tasks,
            "negotiated": tasks_negotiated,
            "experimental": True,
        },
        "execution_shape": (
            "experimental_tasks" if tasks_negotiated else "async_tool_poll_cursor"
        ),
    }


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """A namespaced tool descriptor (name is prefixed with the namespace)."""
    return {
        "name": f"{CONTROL_NAMESPACE}/{spec.name}",
        "version": spec.version,
        "description": spec.description,
        "input_schema": {"type": "object", "additionalProperties": True},
        "annotations": {
            "async": spec.async_tool,
            "poll_cursor": spec.poll_cursor,
        },
    }


def control_tool_specs() -> list[ToolSpec]:
    """The namespaced, versioned control tools this surface exposes."""
    return [
        ToolSpec(
            name="status",
            version=CONTROL_VERSION,
            description="Read a run's durable state (optionally as run-snapshot-v1).",
            handler=control_status,
        ),
        ToolSpec(
            name="evidence",
            version=CONTROL_VERSION,
            description="Read a run's ordered journal evidence.",
            handler=control_evidence,
        ),
        ToolSpec(
            name="cancel",
            version=CONTROL_VERSION,
            description="Cancel a run through the identity-checked native lifecycle.",
            handler=control_cancel,
        ),
        ToolSpec(
            name="resume",
            version=CONTROL_VERSION,
            description="Resume a run (new identified attempt or reconcile).",
            handler=control_resume,
        ),
        ToolSpec(
            name="poll",
            version=CONTROL_VERSION,
            description="Poll a run's ordered progress frames (fallback async shape).",
            handler=control_poll,
            poll_cursor=True,
        ),
    ]


def legacy_tool_specs() -> list[ToolSpec]:
    """The six legacy command tools (delegating to the server wrappers).

    The handlers are resolved lazily from ``skillweave.mcp.server`` so the
    legacy JSON-string contract is preserved exactly and this module imports
    cleanly whether or not the server module is loaded.
    """
    from skillweave.mcp import server as _server

    names = {
        "create_run": _server.create_run,
        "status_run": _server.status_run,
        "cancel_run": _server.cancel_run,
        "resume_run": _server.resume_run,
        "get_evidence": _server.get_evidence,
        "review_run": _server.review_run,
    }
    return [
        ToolSpec(name=n, version="legacy", description=n, handler=names[n])
        for n in LEGACY_TOOLS
    ]


def mcp_agent_tool_specs() -> list[ToolSpec]:
    """The SW-1510-MCP-AGENTS tools (the same module handlers stdio registers).

    Resolved lazily from ``skillweave.mcp.server`` exactly as
    :func:`legacy_tool_specs` is, so the scoped HTTP server serves the identical
    durable handoff/mailbox surface -- gated by each tool's own
    ``gate_operation`` -- without this module eagerly importing the optional
    authority subpackage or re-declaring the handlers.
    """
    from skillweave.mcp import server as _server

    names = {
        "mailbox_post": _server.mailbox_post,
        "mailbox_read": _server.mailbox_read,
        "mailbox_replay": _server.mailbox_replay,
        "mailbox_ack": _server.mailbox_ack,
        "agent_capabilities": _server.agent_capabilities,
    }
    return [
        ToolSpec(name=n, version="agents", description=n, handler=names[n])
        for n in MCP_AGENT_TOOLS
    ]


__all__ = [
    "CONTROL_NAMESPACE",
    "CONTROL_SURFACE",
    "CONTROL_VERSION",
    "ControlStorageError",
    "DEFAULT_HTTP_HOST",
    "DEFAULT_HTTP_PORT",
    "DEGRADED_REASON",
    "HTTP_AUTH_ENV",
    "HTTP_UNAUTHENTICATED_BODY",
    "HttpRequestUnauthorized",
    "LEGACY_TOOLS",
    "LOOPBACK_HOSTS",
    "MCP_AGENT_TOOLS",
    "MCP_PROTOCOL_VERSION",
    "McpDependencyUnavailable",
    "RUN_SNAPSHOT_VERSION",
    "SERVER_NAME",
    "SUPPORTED_SNAPSHOT_VERSIONS",
    "SUPPORTED_TRANSPORTS",
    "ScopedHttpRefused",
    "SnapshotVersionRefused",
    "ToolSpec",
    "TransportProfile",
    "WWW_AUTHENTICATE",
    "artifact_evidence_list",
    "analysis_binding",
    "bearer_token_from_headers",
    "bind_http_authority",
    "check_http_authorization",
    "control_cancel",
    "control_evidence",
    "control_poll",
    "control_resume",
    "control_status",
    "control_tool_specs",
    "create_run_payload",
    "current_http_principal",
    "current_http_token",
    "default_stream_root",
    "get_run_service",
    "http_authority",
    "legacy_tool_specs",
    "mcp_agent_tool_specs",
    "mcp_available",
    "mcp_tool_names",
    "negotiate_capabilities",
    "require_mcp",
    "require_scoped_bind",
    "serve",
    "serve_legacy_stdio",
    "serve_scoped_http",
    "server_capabilities",
    "snapshot_v1",
    "source_binding",
    "strip_bearer_scheme",
    "structured_error",
    "tokens_match",
    "tool_schema",
    "transport_profile",
    "unavailable_payload",
]
