"""MCP stdio lifecycle contract tests (SW-ADAPT-001, criteria 4 & 8).

The addendum's Perplexity MCP defect was a consumer that sent a single bare
``tools/call`` as its very first line, with no lifecycle handshake. A compliant
server answers that with ``-32002`` (server not initialized). These tests pin
the corrected client against a hand-rolled, network-free fake server:

* the compliant client performs
  ``initialize -> notifications/initialized -> tools/list -> tools/call``;
* the *old defect* -- a bare first ``tools/call`` -- is reproduced and shown to
  be refused by the same server, so the handshake is load-bearing, not ceremony;
* an MCP outage or a JSON-RPC error becomes a **typed non-success outcome**
  with zero results, never a synthesized "source";
* a floating launch (``git+`` / URL install) is refused before any process is
  spawned.

Boundary: this proves *contract conformance against a fake server*. It can NOT
establish live Perplexity availability -- no live provider is ever called -- and
these tests make no such claim.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from skillweave.adapters import (
    SEARCH_OPERATION,
    SEARCH_PROVIDER_SPECS,
    McpClientError,
    McpStdioClient,
    SearchAdapterBackend,
)

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "adapter" / "fake_mcp_server.py"


def _launch(mode: str) -> list[str]:
    return [sys.executable, str(SERVER), mode]


def _mcp_backend():
    return SearchAdapterBackend(SEARCH_PROVIDER_SPECS["perplexity-mcp"])


def _run(coro):
    return asyncio.run(coro)


async def _bare_first_call(mode: str) -> dict:
    """Send a raw ``tools/call`` as the first line, as the old defect did."""
    proc = await asyncio.create_subprocess_exec(
        *_launch(mode),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "perplexity_search", "arguments": {"query": "x"}}}
    )
    proc.stdin.write((line + "\n").encode())
    await proc.stdin.drain()
    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    proc.kill()
    await proc.wait()
    return json.loads(raw.decode())


class TestCompliantLifecycle:
    def test_client_completes_the_handshake_and_lists_tools(self):
        async def scenario():
            client = McpStdioClient(_launch("normal"))
            await client.start()
            try:
                return await client.list_tools()
            finally:
                await client.close()

        tools = _run(scenario())
        assert tools == ["perplexity_search"], tools

    def test_client_calls_a_tool_and_gets_structured_content(self):
        async def scenario():
            client = McpStdioClient(_launch("normal"))
            async with client:
                return await client.call_tool("perplexity_search", {"query": "x"})

        result = _run(scenario())
        assert result["structuredContent"]["results"], result

    def test_client_refuses_a_tool_the_server_does_not_expose(self):
        async def scenario():
            client = McpStdioClient(_launch("normal"))
            async with client:
                await client.list_tools()
                await client.call_tool("missing_tool", {})

        with pytest.raises(McpClientError):
            _run(scenario())


class TestTheOldDefectIsLoadBearing:
    def test_bare_first_call_is_refused_with_not_initialized(self):
        reply = _run(_bare_first_call("reject-first-call"))
        assert "error" in reply, reply
        assert reply["error"]["code"] == -32002, reply

    def test_the_compliant_client_succeeds_where_the_bare_call_failed(self):
        # Same server, same tool: only the handshake differs.
        async def scenario():
            client = McpStdioClient(_launch("reject-first-call"))
            async with client:
                return await client.call_tool("perplexity_search", {"query": "x"})

        result = _run(scenario())
        assert result["structuredContent"]["results"], result


class TestMcpBackendProducesTypedOutcomes:
    def test_mcp_transport_success_is_a_typed_search_outcome(self):
        backend = _mcp_backend()
        outcome = _run(
            backend.invoke(
                SEARCH_OPERATION,
                {"query": "hello", "command": _launch("normal")},
                {},
            )
        )
        assert outcome.status == "success", outcome
        assert outcome.requested_provider == "perplexity"
        assert outcome.results, "a success must carry results"
        assert outcome.attempts[0].transport == "mcp-stdio"
        # The unsafe 'javascript:' URL the server returns is never promoted.
        assert all(r.url.startswith("http") for r in outcome.results), outcome.results

    def test_mcp_outage_is_a_non_success_outcome_not_a_source(self):
        backend = _mcp_backend()
        outcome = _run(
            backend.invoke(
                SEARCH_OPERATION,
                {"query": "hello", "command": _launch("unavailable")},
                {},
            )
        )
        assert outcome.status != "success", outcome
        assert outcome.results == (), "an outage must not yield a source"
        assert outcome.attempts[0].status != "success"
        assert outcome.error_code, outcome

    def test_mcp_jsonrpc_error_is_reported_not_simulated(self):
        backend = _mcp_backend()
        outcome = _run(
            backend.invoke(
                SEARCH_OPERATION,
                {"query": "hello", "command": _launch("error")},
                {},
            )
        )
        assert outcome.status != "success", outcome
        assert outcome.results == (), outcome

    def test_missing_launcher_binding_is_refused(self):
        backend = _mcp_backend()
        outcome = _run(backend.invoke(SEARCH_OPERATION, {"query": "hello"}, {}))
        assert outcome.status != "success", outcome
        assert outcome.error_code == "no_binding", outcome


class TestFloatingLaunchIsRefused:
    @pytest.mark.parametrize(
        "command",
        [
            ["npx", "-y", "git+https://github.com/example/mcp-server"],
            ["mcp-server", "https://example.org/install"],
            ["uvx", "http://example.org/pkg"],
        ],
    )
    def test_client_constructor_refuses_a_floating_install(self, command):
        with pytest.raises(McpClientError):
            McpStdioClient(command)

    def test_a_local_compliant_command_is_accepted(self):
        # A local interpreter + file is not a floating install.
        McpStdioClient(_launch("normal"))
