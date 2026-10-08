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
  ordinary asynchronous tools + poll cursors as the fallback.

No ``mcp`` import appears at module import time: the package is optional and its
absence is a *reported state*, never an import-time crash.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

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

#: The explicit diagnostic returned when the ``mcp`` dependency is missing.
#: Phrased as a durable state, not a stack trace: a host can surface it and an
#: operator can act on it.
DEGRADED_REASON = (
    "the 'mcp' package is required to serve MCP but is not importable; "
    "install the MCP dependency to expose a working server — the command "
    "server refuses to serve rather than advertise a non-functional surface"
)

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


def _build_server(fastmcp: Any):
    """Build the real ``FastMCP`` server carrying the six legacy tools."""
    server = fastmcp.FastMCP(SERVER_NAME)
    for spec in legacy_tool_specs():
        server.tool()(spec.handler)
    return server


def serve_legacy_stdio() -> None:
    """Serve the six legacy tools over a real stdio MCP transport.

    This is the *only* way the command server may advertise itself as working.
    With no ``mcp`` package it raises :class:`McpDependencyUnavailable` -- the
    server refuses rather than serving an empty dummy.
    """
    fastmcp = require_mcp()
    _build_server(fastmcp).run()


def serve(transport: str = "stdio", *, host: str = "", port: int = 0) -> None:
    """Serve over ``stdio`` or a scoped Streamable HTTP transport.

    The transport is *honoured*, not described: ``stdio`` runs the stdio server
    and ``http`` runs the scoped Streamable HTTP server (the real
    ``streamable-http`` transport). An unknown transport is refused before any
    server is built, and a missing ``mcp`` package refuses with the explicit
    diagnostic.
    """
    profile = transport_profile(transport, host=host, port=port)
    if not profile.supported:
        raise McpDependencyUnavailable(profile.reason or DEGRADED_REASON)
    fastmcp = require_mcp()
    server = _build_server(fastmcp)
    if profile.scoped:
        settings = getattr(server, "settings", None)
        if settings is not None:
            if host:
                settings.host = host
            if port:
                settings.port = port
        server.run(transport="streamable-http")
    else:
        server.run()


# ---------------------------------------------------------------------------
# Service composition (the single service both CLI and MCP drive)
# ---------------------------------------------------------------------------

def _get_store(db_path: str = ".skillweave/store.db"):
    from skillweave.runtime.store import SQLiteRunStore

    return SQLiteRunStore(db_path)


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
    """Build the same :class:`RunApplicationService` the CLI ingress builds."""
    from skillweave.runtime.journal import EventJournal
    from skillweave.runtime.registry import RawArtifactStore
    from skillweave.runtime.reservation import ReservationStore
    from skillweave.runtime.stream import DurableStreamStore
    from skillweave.runsvc.service import RunApplicationService

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
) -> dict[str, Any]:
    """Execute one native run through the shared service and return its payload.

    This is the single execution ingress the command server's ``create_run``
    delegates to. It mints the run id, honours/creates the owner budget
    envelope, drives ``execute_streaming_native`` and returns the durable
    structured payload (run/state/gate/digest/stream[/assay]).
    """
    from skillweave.dispatch.application import generate_run_id

    svc = get_run_service(
        db_path, artifacts_path, stream_root or default_stream_root(db_path)
    )
    run_id = generate_run_id()
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
        artifact_evidence=artifact_evidence_list(artifact_evidence),
        source_resolver=source_binding(source_root, subject_commit),
        analysis_reader=analysis_binding(analysis_root),
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

    An unsupported ``version`` name is refused by :class:`SnapshotVersionRefused`
    (never coerced). A run whose trusted fields cannot be expressed in the
    frozen version is refused by the service itself
    (``RunSnapshotContractError``).
    """
    _require_snapshot_version(version)
    svc = get_run_service(db_path, artifacts_path)
    snapshot = svc.run_snapshot_v1(run_id)
    if snapshot is None:
        return {"error": "Run not found", "run_id": run_id, "version": version}
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
    versioned record ``run-snapshot-v1`` is returned, refusing an unknown name.
    """
    svc = get_run_service(db_path, artifacts_path)
    if not version:
        snapshot = svc.run_snapshot(run_id)
        if snapshot is None:
            return {"error": "Run not found", "run_id": run_id}
        return snapshot
    _require_snapshot_version(version)
    snapshot = svc.run_snapshot_v1(run_id, version=version)
    if snapshot is None:
        return {"error": "Run not found", "run_id": run_id, "version": version}
    return snapshot


def control_evidence(
    run_id: str,
    *,
    db_path: str = ".skillweave/store.db",
) -> dict[str, Any]:
    """Return a run's ordered journal evidence (the durable record)."""
    from skillweave.runtime.journal import EventJournal

    journal = EventJournal(db_path)
    events = journal.get_events(run_id)
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
    """
    from skillweave.runtime.errors import ProcessIdentityError, StoreError

    svc = get_run_service(db_path, artifacts_path)
    if svc.store.get_run(run_id) is None:
        return {"error": "Run not found", "run_id": run_id}
    try:
        result = svc.cancel_process(run_id, reason=reason)
    except ProcessIdentityError as exc:
        return {
            "error": "process identity not verified; cancellation refused",
            "run_id": run_id,
            "reason": exc.reason,
            "verification": exc.verification,
        }
    except StoreError as exc:
        return {"error": str(exc), "run_id": run_id}
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
    already-launched attempt.
    """
    from skillweave.runtime.errors import StoreError

    svc = get_run_service(db_path, artifacts_path)
    if not command:
        snapshot = svc.resume_run(run_id)
        if snapshot is None:
            return {"error": "Run not found", "run_id": run_id}
        return {
            "status": "reconciled",
            "restarted": False,
            "note": "no command supplied; no new attempt launched",
            **snapshot,
        }
    try:
        result = svc.restart_attempt(
            run_id,
            shlex.split(command),
            idempotency_key=idempotency_key or None,
        )
    except StoreError as exc:
        return {"error": str(exc), "run_id": run_id}
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
    when experimental MCP Tasks are not negotiated for the transport.
    """
    svc = get_run_service(
        db_path, artifacts_path, stream_root or default_stream_root(db_path)
    )
    frames = svc.stream_frames(
        run_id, after_cursor=after_cursor, limit=limit or None
    )
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


__all__ = [
    "CONTROL_NAMESPACE",
    "CONTROL_SURFACE",
    "CONTROL_VERSION",
    "DEGRADED_REASON",
    "LEGACY_TOOLS",
    "MCP_PROTOCOL_VERSION",
    "McpDependencyUnavailable",
    "RUN_SNAPSHOT_VERSION",
    "SERVER_NAME",
    "SUPPORTED_SNAPSHOT_VERSIONS",
    "SUPPORTED_TRANSPORTS",
    "SnapshotVersionRefused",
    "ToolSpec",
    "TransportProfile",
    "artifact_evidence_list",
    "analysis_binding",
    "control_cancel",
    "control_evidence",
    "control_poll",
    "control_resume",
    "control_status",
    "control_tool_specs",
    "create_run_payload",
    "default_stream_root",
    "get_run_service",
    "legacy_tool_specs",
    "mcp_available",
    "negotiate_capabilities",
    "require_mcp",
    "serve",
    "serve_legacy_stdio",
    "server_capabilities",
    "snapshot_v1",
    "source_binding",
    "tool_schema",
    "transport_profile",
    "unavailable_payload",
]
