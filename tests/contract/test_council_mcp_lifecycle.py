"""Council's MCP stdio lifecycle against a fake server and a pinned installed server.

SW-COUNCIL-003, criterion 1. The addendum's Perplexity MCP defect was a Council
consumer that sent a single bare ``tools/call`` as its very first line, with no
lifecycle handshake, and launched the server from a floating git install. This
suite pins Council's own repair:

* the Council client performs the required lifecycle --
  ``initialize -> notifications/initialized -> tools/list -> tools/call``;
* it **negotiates and records** the protocol version, the server's advertised
  capabilities and its ``serverInfo`` -- from **two independent servers**: the
  hand-rolled fake *and* a real, pinned, installed artifact fixture;
* it **correlates request ids across interleaved messages**: the fake emits an
  unsolicited notification and a response to a *different id* before every real
  response, so a client that merely reads "the next line" is proven wrong;
* the pinned server advertises an *older* protocol version, so the recording is
  proven to reflect the server, not the client's own assumption;
* the old bare-``tools/call`` defect is reproduced and shown to be refused by
  the same server (``-32002``), so the handshake is load-bearing;
* a closed stream, a JSON-RPC error and an ``isError`` tool result each become a
  typed client error -- never a synthesized source.

Boundary: these fixtures prove *contract conformance*. They do NOT establish
live Perplexity availability; no live provider is ever called.
"""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
FAKE = FIX_C / "fake_mcp_server.py"
PINNED = FIX_C / "pinned"
PINNED_BIN = PINNED / "bin"

from skillweave.council.mcp_client import (  # noqa: E402
    MCP_PROTOCOL_VERSION,
    McpClientError,
    McpStdioClient,
    McpToolError,
    NegotiatedSession,
)
from skillweave.council import launcher as L  # noqa: E402
from skillweave.council.perplexity import (  # noqa: E402
    PERPLEXITY_MCP_TOOL,
    PerplexityMcpBackend,
)
from skillweave.adapters import SEARCH_OPERATION  # noqa: E402


def _fake(mode: str) -> list[str]:
    return [sys.executable, str(FAKE), mode]


def _run(coro):
    return asyncio.run(coro)


def _search_path() -> str:
    return os.pathsep.join(
        [str(PINNED_BIN), "/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"]
    )


def _pinned_registration() -> L.Registration:
    return L.Registration(
        id="sw-council-pinned-mcp",
        launch=("sw-council-pinned-mcp",),
        distribution="sw-council-pinned-mcp",
        version="9.9.9",
        transport="mcp-stdio",
        source="fixture:pinned",
    )


class _Recording(McpStdioClient):
    """Records every *outgoing* request so the lifecycle order is auditable."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: list[dict] = []

    async def _send(self, message):
        self.sent.append(dict(message))
        await super()._send(message)


# ── the lifecycle is performed, in order ───────────────────────────────────


class TestLifecycleHandshake:
    def test_sends_the_full_lifecycle_in_order(self):
        async def scenario():
            client = _Recording(_fake("normal"))
            await client.start()
            try:
                await client.list_tools()
                await client.call_tool(PERPLEXITY_MCP_TOOL, {"query": "q"})
            finally:
                await client.close()
            return client

        client = _run(scenario())
        methods = [m.get("method") for m in client.sent]
        assert methods == [
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ], methods

    def test_records_the_negotiated_version_capabilities_and_server_info(self):
        async def scenario():
            client = McpStdioClient(_fake("normal"))
            await client.start()
            try:
                return client.session
            finally:
                await client.close()

        session = _run(scenario())
        assert isinstance(session, NegotiatedSession)
        assert session.protocol_version == "2025-06-18"
        assert session.version_agreed is True
        assert session.capabilities["tools"]["listChanged"] is True
        assert session.server_info["name"] == "council-fake"
        assert session.server_info["version"] == "2.1.0"
        assert session.instructions == "answer with citations"

    def test_the_initialize_request_names_the_protocol_version_and_client(self):
        async def scenario():
            client = _Recording(_fake("normal"))
            await client.start()
            await client.close()
            return client

        client = _run(scenario())
        init = client.sent[0]
        assert init["params"]["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert init["params"]["clientInfo"]["name"] == "skillweave-council"


# ── request-id correlation across interleaved messages ─────────────────────


class TestRequestIdCorrelation:
    def test_skips_notifications_and_stale_ids_while_correlating(self):
        """The fake emits a notification + a stale-id reply before each real one."""

        async def scenario():
            client = McpStdioClient(_fake("normal"))
            await client.start()
            try:
                await client.list_tools()
                result = await client.call_tool(PERPLEXITY_MCP_TOOL, {"query": "q"})
            finally:
                await client.close()
            return client, result

        client, result = _run(scenario())
        notifications = [m for m in client.messages if "id" not in m]
        stale = [m for m in client.messages if m.get("id") == 999999]
        assert notifications, "the interleaved notifications were observed"
        assert stale, "the stale-id responses were observed and skipped"
        # The real responses matched the client's own sequential request ids.
        matched = {m["id"] for m in client.messages if "id" in m and m["id"] != 999999}
        assert matched == {1, 2, 3}, matched
        # And the *correct* (matching-id) response is what the call returned.
        assert result["structuredContent"]["results"], result

    def test_a_notification_arrives_before_each_real_response(self):
        async def scenario():
            client = McpStdioClient(_fake("normal"))
            await client.start()
            try:
                await client.list_tools()
            finally:
                await client.close()
            return client

        client = _run(scenario())
        seq = [("id" in m) for m in client.messages]
        # At least one notification (no id) precedes a matching response.
        assert False in seq, seq
        assert seq.index(True) > seq.index(False), seq


# ── the old bare-first-call defect is load-bearing ─────────────────────────


class TestOldDefectIsLoadBearing:
    def test_bare_first_call_is_refused_with_not_initialized(self):
        async def scenario():
            proc = await asyncio.create_subprocess_exec(
                *_fake("reject-first-call"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            line = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": PERPLEXITY_MCP_TOOL, "arguments": {"query": "x"}}}
            )
            proc.stdin.write((line + "\n").encode())
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            proc.kill()
            await proc.wait()
            return json.loads(raw.decode())

        reply = _run(scenario())
        assert reply["error"]["code"] == -32002, reply

    def test_the_compliant_client_succeeds_where_the_bare_call_failed(self):
        async def scenario():
            client = McpStdioClient(_fake("reject-first-call"))
            async with client:
                return await client.call_tool(PERPLEXITY_MCP_TOOL, {"query": "x"})

        result = _run(scenario())
        assert result["structuredContent"]["results"], result


# ── the pinned installed server (a real artifact, not the in-file fake) ────


class TestPinnedInstalledServer:
    def test_a_verified_registration_resolves_the_installed_binary(self, monkeypatch):
        monkeypatch.syspath_prepend(str(PINNED))
        importlib.invalidate_caches()
        res = L.resolve_launcher(
            registrations=[_pinned_registration()], search_path=_search_path()
        )
        assert res.kind == "registered", res.provenance
        assert res.command[0] == os.path.realpath(str(PINNED_BIN / "sw-council-pinned-mcp"))
        assert res.provenance["distribution"] == "sw-council-pinned-mcp"
        assert res.provenance["observed_version"] == "9.9.9"

    def test_pinned_server_completes_the_handshake_and_records_its_version(self, monkeypatch):
        monkeypatch.syspath_prepend(str(PINNED))
        importlib.invalidate_caches()
        res = L.resolve_launcher(
            registrations=[_pinned_registration()], search_path=_search_path()
        )
        assert L.is_floating_command(res.command) is False

        async def scenario():
            client = McpStdioClient(list(res.command))
            await client.start()
            try:
                names = await client.tool_names()
                return client.session, names
            finally:
                await client.close()

        session, names = _run(scenario())
        assert names == [PERPLEXITY_MCP_TOOL], names
        # The server advertised an OLDER version: the record reflects the server.
        assert session.protocol_version == "2025-03-26"
        assert session.requested_protocol_version == "2025-06-18"
        assert session.version_agreed is False
        assert session.server_info["name"] == "sw-council-pinned-mcp"

    def test_pinned_server_yields_a_cited_source_via_the_council_backend(self, monkeypatch):
        monkeypatch.syspath_prepend(str(PINNED))
        importlib.invalidate_caches()
        res = L.resolve_launcher(
            registrations=[_pinned_registration()], search_path=_search_path()
        )
        backend = PerplexityMcpBackend()
        outcome = _run(
            backend.invoke(
                SEARCH_OPERATION,
                {"query": "hello", "command": list(res.command)},
                {},
            )
        )
        assert outcome.status == "success", outcome
        assert outcome.attempts[0].transport == "mcp-stdio"
        urls = [r.url for r in outcome.results]
        assert urls and all(u.startswith("http") for u in urls), urls
        assert "https://pinned.example.org/source" in urls
        # The ftp:// citation is never promoted.
        assert "ftp://not-http.example.org/file" not in urls
        # The recorded negotiation survives on the backend for later inspection.
        assert backend.last_session.protocol_version == "2025-03-26"


# ── typed failures, never a synthesized source ─────────────────────────────


class TestTypedFailures:
    def test_a_closed_stream_is_a_typed_client_error(self):
        async def scenario():
            client = McpStdioClient(_fake("unavailable"))
            await client.start()

        with pytest.raises(McpClientError):
            _run(scenario())

    def test_an_iserror_tool_result_is_a_typed_tool_error(self):
        async def scenario():
            client = McpStdioClient(_fake("iserror"))
            async with client:
                await client.list_tools()
                return await client.call_tool(PERPLEXITY_MCP_TOOL, {"query": "q"})

        with pytest.raises(McpToolError) as excinfo:
            _run(scenario())
        assert excinfo.value.is_error is True

    def test_client_refuses_a_tool_the_server_does_not_expose(self):
        async def scenario():
            client = McpStdioClient(_fake("missing-tool"))
            async with client:
                await client.list_tools()
                return await client.call_tool(PERPLEXITY_MCP_TOOL, {"query": "q"})

        with pytest.raises(McpClientError):
            _run(scenario())

    def test_a_jsonrpc_error_surfaces_its_code(self):
        async def scenario():
            client = McpStdioClient(_fake("error"))
            async with client:
                await client.list_tools()
                return await client.call_tool(PERPLEXITY_MCP_TOOL, {"query": "q"})

        with pytest.raises(McpClientError) as excinfo:
            _run(scenario())
        assert excinfo.value.code == -32603
