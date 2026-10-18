"""A compliant MCP stdio JSON-RPC client (SW-ADAPT-001).

The addendum's Perplexity MCP consumer defect was that it sent a single
``tools/call`` as the very first line, with no lifecycle handshake, and launched
the server from a *floating* git install. A compliant server answers such a call
with ``-32002`` (server not initialized). This client performs the required
lifecycle:

    initialize  ->  notifications/initialized  ->  tools/list  ->  tools/call

The launch command is supplied by the caller from an explicit binding -- this
client never constructs a floating ``git+``/URL install. The child is an *owned*
process: it receives only the allowlisted environment plus the credential
variables of the declared reference, and the credential value is never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "McpClientError",
    "McpStdioClient",
]

MCP_PROTOCOL_VERSION = "2025-06-18"


class McpClientError(RuntimeError):
    """An MCP lifecycle or call error, carrying the JSON-RPC error code."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


def _assert_not_floating(command: Sequence[str]) -> None:
    """Refuse a launch command that would install floating dependencies.

    Uses the canonical launcher detector so this client refuses the same forms
    the resolver does -- the ``git+``/``http(s)`` markers plus an unpinned
    ``uvx``/``npx`` package spec -- not the markers alone.
    """
    from skillweave.council.launcher import is_floating_command as _canonical

    if _canonical(command):
        raise McpClientError(
            f"refusing a floating install launch: {list(command)!r}"
        )


class McpStdioClient:
    """A minimal, compliant stdio MCP client.

    Usage::

        client = McpStdioClient(command, env_allowlist=[...], extra_env={...})
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
        client_name: str = "skillweave-adapters",
        client_version: str = "0.1.0",
    ) -> None:
        if not command:
            raise McpClientError("an MCP stdio client needs a non-empty command")
        _assert_not_floating(command)
        self._command = [str(c) for c in command]
        self._env_allowlist = tuple(env_allowlist or ())
        self._extra_env = dict(extra_env or {})
        self._protocol_version = protocol_version
        self._timeout = timeout
        self._client_name = client_name
        self._client_version = client_version
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._tools: Optional[list[str]] = None

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

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._child_env(),
        )
        await self._initialize()

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()

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
        """Read lines until the response matching ``request_id`` arrives.

        Unsolicited notifications (no ``id``) are skipped, as a compliant client
        must. A JSON-RPC error object is raised as :class:`McpClientError`.
        """
        if self._proc is None or self._proc.stdout is None:
            raise McpClientError("client is not started")
        while True:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=self._timeout
            )
            if not line:
                raise McpClientError("server closed the stream before responding")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue  # non-protocol noise on stdout
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

    async def _initialize(self) -> Mapping[str, Any]:
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
        # Acknowledge initialization; the server may then serve tools/list.
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def list_tools(self) -> list[str]:
        if self._tools is not None:
            return list(self._tools)
        rid = self._request_id()
        await self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}})
        result = await self._read_response(rid)
        tools = result.get("tools", []) if isinstance(result, Mapping) else []
        self._tools = [str(t.get("name")) for t in tools if isinstance(t, Mapping)]
        return list(self._tools)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._tools is not None and name not in self._tools:
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
        return await self._read_response(rid)
