"""A Council-owned, protocol-conformant MCP stdio client (SW-COUNCIL-003).

The addendum's Perplexity MCP defect was a consumer that (a) sent a single bare
``tools/call`` as its very first line, with no lifecycle handshake, and (b)
launched the server from a *floating* git install. This module is Council's own
repair of the *consumer* half, kept inside Council's write scope so the
SW-ADAPT-001 adapter substrate is not rewritten:

* it performs the required lifecycle --
  ``initialize -> notifications/initialized -> tools/list -> tools/call``;
* it **negotiates and records** the protocol version, the server's advertised
  capabilities and its ``serverInfo``;
* it **correlates request ids across interleaved messages**: unsolicited
  notifications (no ``id``) and responses to *other* requests are skipped, and
  every message observed is retained so the correlation is auditable rather than
  asserted;
* a JSON-RPC ``error`` object, a tool result flagged ``isError``, and a closed
  stream each become a *typed* client error -- never a synthesized source;
* the child is **owned**: it is spawned in its own session/process group, so a
  timeout or cancellation tears down *only* that process group and leaves
  sibling sentinels alive;
* stderr is drained continuously and any secret sentinel it carries is
  **redacted** before the text is ever surfaced or logged.

It never constructs a floating ``git+``/URL install: a command carrying a
floating marker is refused before any process is spawned. It calls no live
provider on import.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "FLOATING_MARKERS",
    "McpClientError",
    "McpTimeoutError",
    "McpToolError",
    "NegotiatedSession",
    "redact_text",
    "is_floating_command",
    "McpStdioClient",
]

MCP_PROTOCOL_VERSION = "2025-06-18"

#: Markers that betray a floating install. Checked against the whole command;
#: an explicit ``--from``/``-f`` local mirror hint is honoured by the launcher
#: resolver, which produces the command this client receives.
FLOATING_MARKERS: tuple[str, ...] = ("git+", "http://", "https://")

#: Secret-shaped substrings redacted from any surfaced stderr/log text. The set
#: is intentionally value-shaped (prefixes + long token runs), not a single
#: vendor literal, so an unknown provider's key is still masked.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bpplx-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\bgho_[A-Za-z0-9]{8,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9._-]{6,}"),
)

_REDACTED = "[REDACTED]"


def is_floating_command(command: Sequence[str]) -> bool:
    """Whether a launch command would install floating code."""
    joined = " ".join(str(part) for part in command)
    return any(marker in joined for marker in FLOATING_MARKERS)


def redact_text(
    text: str,
    *,
    secrets: Iterable[str] = (),
    extra_patterns: Iterable[str] = (),
) -> str:
    """Redact known secret values and secret-shaped substrings from ``text``.

    ``secrets`` are exact values (which redact regardless of shape); the built-in
    patterns catch the common ``sk-``/``pplx-``/``Bearer``/``api_key=`` shapes so
    an unknown credential is still masked. ``extra_patterns`` adds caller-supplied
    regexes.
    """
    if not isinstance(text, str):
        return text
    out = text
    for value in secrets:
        value = str(value)
        if value:
            out = out.replace(value, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    for pattern in extra_patterns:
        try:
            out = re.sub(str(pattern), _REDACTED, out)
        except re.error:
            continue
    return out


class McpClientError(RuntimeError):
    """An MCP lifecycle or call error, carrying the JSON-RPC/transport code."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class McpTimeoutError(McpClientError):
    """No matching response arrived within the deadline (a timeout)."""


class McpToolError(McpClientError):
    """A ``tools/call`` result was flagged ``isError`` (or malformed)."""

    def __init__(self, message: str, *, code: Optional[int] = None, is_error: bool = True) -> None:
        super().__init__(message, code=code)
        self.is_error = is_error


@dataclass(frozen=True)
class NegotiatedSession:
    """The recorded result of the ``initialize`` handshake.

    Every field is what the *server* returned, so a later reader can verify the
    negotiated protocol version and the advertised capabilities without holding
    the live client.
    """

    protocol_version: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    server_info: Mapping[str, Any] = field(default_factory=dict)
    instructions: Optional[str] = None
    requested_protocol_version: str = MCP_PROTOCOL_VERSION

    @property
    def version_agreed(self) -> bool:
        """Whether the server agreed to the requested protocol version."""
        return self.protocol_version == self.requested_protocol_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "requested_protocol_version": self.requested_protocol_version,
            "version_agreed": self.version_agreed,
            "capabilities": dict(self.capabilities),
            "server_info": dict(self.server_info),
            "instructions": self.instructions,
        }


class McpStdioClient:
    """A minimal, compliant stdio MCP client with an owned child process.

    Usage::

        client = McpStdioClient(command, env_allowlist=[...], secrets=[...])
        async with client as c:
            tools = await c.list_tools()
            result = await c.call_tool("perplexity_search", {"query": "..."})
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env_allowlist: Optional[Iterable[str]] = None,
        extra_env: Optional[Mapping[str, str]] = None,
        protocol_version: str = MCP_PROTOCOL_VERSION,
        timeout: float = 30.0,
        client_name: str = "skillweave-council",
        client_version: str = "0.1.0",
        secrets: Iterable[str] = (),
    ) -> None:
        if not command:
            raise McpClientError("an MCP stdio client needs a non-empty command")
        if is_floating_command(command):
            raise McpClientError(
                f"refusing a floating install launch: {list(command)!r}"
            )
        self._command = [str(c) for c in command]
        self._env_allowlist = tuple(env_allowlist or ())
        self._extra_env = dict(extra_env or {})
        self._protocol_version = protocol_version
        self._timeout = timeout
        self._client_name = client_name
        self._client_version = client_version
        self._secrets = tuple(str(s) for s in secrets if s)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._session: Optional[NegotiatedSession] = None
        self._tools: Optional[list[dict[str, Any]]] = None
        self._messages: list[Mapping[str, Any]] = []
        self._stderr_lines: list[str] = []
        self._stderr_task: Optional[asyncio.Task] = None
        self._stdout_closed = False

    # -- introspection -----------------------------------------------------

    @property
    def session(self) -> Optional[NegotiatedSession]:
        """The recorded ``initialize`` negotiation (``None`` before start)."""
        return self._session

    @property
    def messages(self) -> tuple[Mapping[str, Any], ...]:
        """Every protocol message observed, in arrival order (correlation audit)."""
        return tuple(self._messages)

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    def stderr_text(self) -> str:
        """Drained stderr, redacted of any secret sentinel."""
        return redact_text("\n".join(self._stderr_lines), secrets=self._secrets)

    # -- lifecycle ---------------------------------------------------------

    def _child_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in self._env_allowlist if k in os.environ}
        env.update(self._extra_env)
        return env

    async def __aenter__(self) -> "McpStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> "McpStdioClient":
        """Spawn the owned child and complete the ``initialize`` handshake."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
            # Own session/process group so teardown touches *only* this child.
            start_new_session=True,
        )
        loop = asyncio.get_running_loop()
        self._stderr_task = loop.create_task(self._drain_stderr())
        await self._initialize()
        return self

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                self._stderr_lines.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        except (asyncio.CancelledError, Exception):
            return

    async def close(self) -> None:
        """Terminate the owned child's process group; never a sibling's."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        await self._terminate_group(proc)
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

    @staticmethod
    async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the owned process group, then SIGKILL if it persists.

        ``start_new_session=True`` gives the child its own pgid, so
        ``killpg`` cannot reach a sibling/server process the caller owns.
        """
        if proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        else:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        else:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    async def cancel(self, request_id: Optional[int] = None) -> None:
        """Send ``notifications/cancelled`` then terminate the owned child."""
        if self._proc is not None and self._proc.stdin is not None:
            try:
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": request_id, "reason": "timeout"},
                    }
                )
            except (McpClientError, Exception):
                pass
        await self.close()

    # -- JSON-RPC ----------------------------------------------------------

    def _request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, message: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpClientError("client is not started")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _read_response(self, request_id: int) -> Mapping[str, Any]:
        """Read until the response matching ``request_id`` arrives.

        Unsolicited notifications (no ``id``) and responses to other/earlier
        requests are **recorded and skipped** -- the correlation a compliant
        client must perform across multiple interleaved messages. A JSON-RPC
        error object raises :class:`McpClientError`; a closed stream raises.
        """
        if self._proc is None or self._proc.stdout is None:
            raise McpClientError("client is not started")
        while True:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=self._timeout
                )
            except asyncio.TimeoutError as exc:
                raise McpTimeoutError(
                    f"no response to request {request_id} within {self._timeout}s",
                    code=-32001,
                ) from exc
            if not line:
                raise McpClientError("server closed the stream before responding")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue  # non-protocol noise on stdout
            if not isinstance(message, Mapping):
                continue
            self._messages.append(message)
            if "id" not in message:
                continue  # notification
            if message.get("id") != request_id:
                continue  # response to a different/earlier request; skip
            if "error" in message:
                err = message["error"]
                raise McpClientError(
                    str(err.get("message") if isinstance(err, Mapping) else err),
                    code=(err.get("code") if isinstance(err, Mapping) else None),
                )
            return message.get("result", {})

    async def _initialize(self) -> NegotiatedSession:
        rid = self._request_id()
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "initialize",
                "params": {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self._client_name,
                        "version": self._client_version,
                    },
                },
            }
        )
        result = await self._read_response(rid)
        result = result if isinstance(result, Mapping) else {}
        self._session = NegotiatedSession(
            protocol_version=str(result.get("protocolVersion", self._protocol_version)),
            capabilities=dict(result.get("capabilities") or {}),
            server_info=dict(result.get("serverInfo") or {}),
            instructions=result.get("instructions"),
            requested_protocol_version=self._protocol_version,
        )
        # Acknowledge initialization; the server may then serve tools/list.
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self._session

    async def negotiate(self) -> NegotiatedSession:
        """The recorded negotiation (re-running the handshake if needed)."""
        if self._session is None:
            return await self._initialize()
        return self._session

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools is not None:
            return list(self._tools)
        rid = self._request_id()
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}})
        result = await self._read_response(rid)
        tools = result.get("tools", []) if isinstance(result, Mapping) else []
        self._tools = [dict(t) for t in tools if isinstance(t, Mapping)]
        return list(self._tools)

    async def tool_names(self) -> list[str]:
        return [str(t.get("name")) for t in await self.list_tools()]

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._tools is not None and name not in {t.get("name") for t in self._tools}:
            raise McpClientError(f"server does not expose tool {name!r}")
        rid = self._request_id()
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments)},
            }
        )
        result = await self._read_response(rid)
        if isinstance(result, Mapping) and result.get("isError") is True:
            detail = _text_of(result) or "tool reported isError"
            raise McpToolError(f"tool {name!r} returned isError: {detail}")
        return result if isinstance(result, Mapping) else {"content": result}


def _text_of(result: Mapping[str, Any]) -> str:
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(b.get("text", "")) for b in blocks
        if isinstance(b, Mapping) and b.get("type") == "text"
    ).strip()
