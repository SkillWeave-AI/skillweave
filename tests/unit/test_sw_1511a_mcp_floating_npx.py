"""SW-1511A-GATE-001 / F1: an unpinned ``npx`` fetch is refused as floating.

The launcher admitted ``npx -y <pkg>`` as a non-floating explicit command, even
though npx fetches and runs whatever the registry resolves at run time -- the
same hazard the ``git+``/``http(s)`` markers and the unpinned-``uvx`` rule
already refuse. The product's own pinned local ``node <path>`` command must stay
admitted, and a local path containing the substring ``http`` must not be misread
as a URL.

These tests exercise the real resolution and client seams (no network, no
provider, no credential; nothing is ever executed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.council import launcher as L  # noqa: E402

#: The exact pinned local command the product binds for the Perplexity MCP
#: server -- must remain admitted and non-floating.
_PERPLXITY_PINNED = [
    "node",
    "/opt/perplexity-mcp/node_modules/@perplexity-ai/mcp-server/dist/index.js",
]


class TestUnpinnedNpxIsRefused:
    def test_unpinned_scoped_npx_is_floating(self):
        assert (
            L.is_floating_command(["npx", "-y", "@perplexity-ai/mcp-server"]) is True
        )

    def test_unpinned_plain_npx_is_floating(self):
        assert L.is_floating_command(["npx", "-y", "some-mcp-server"]) is True

    @pytest.mark.parametrize(
        "command",
        [
            ["npx", "-y", "@perplexity-ai/mcp-server"],
            ["npx", "some-mcp-server"],
            ["npx.cmd", "-y", "@perplexity-ai/mcp-server"],
        ],
    )
    def test_resolution_refuses_the_unpinned_npx_form(self, command):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(explicit_command=command)
        assert excinfo.value.asset == "explicit_command"


class TestPinnedAndLocalNpxStayAdmitted:
    @pytest.mark.parametrize(
        "command",
        [
            ["npx", "-y", "@perplexity-ai/mcp-server@1.3.0"],
            ["npx", "-y", "some-mcp-server@1.2.3"],
        ],
    )
    def test_exactly_pinned_npx_is_not_floating(self, command):
        assert L.is_floating_command(command) is False
        assert L.resolve_launcher(explicit_command=command).kind == "explicit"

    @pytest.mark.parametrize(
        "command",
        [
            ["npx", "-y", "./node_modules/.bin/mcp-server.js"],
            ["npx", "/opt/mcp/index.js"],
            ["npx", "-y", "file:/opt/mcp/pkg.tgz"],
        ],
    )
    def test_a_local_npx_target_is_not_a_remote_fetch(self, command):
        assert L.is_floating_command(command) is False


class TestProductPinnedPathUnchanged:
    def test_the_pinned_local_node_command_is_admitted(self):
        assert L.is_floating_command(_PERPLXITY_PINNED) is False
        assert L.resolve_launcher(explicit_command=_PERPLXITY_PINNED).kind == "explicit"

    def test_a_local_path_containing_http_is_not_misread(self, tmp_path):
        pinned = tmp_path / "http-mirror" / "pkg.whl"
        pinned.parent.mkdir(parents=True)
        pinned.write_text("")
        assert L.is_floating_command([str(pinned)]) is False


class TestExistingFloatingFormsStillRefused:
    @pytest.mark.parametrize(
        "command",
        [
            ["uvx", "git+https://github.com/example/mcp-server"],
            ["mcp-server", "https://example.org/install"],
            ["uvx", "http://example.org/pkg"],
        ],
    )
    def test_marker_and_uvx_forms_still_refused(self, command):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(explicit_command=command)
        assert excinfo.value.asset == "explicit_command"

    def test_unpinned_uvx_spec_still_floating(self):
        assert (
            L.is_floating_command(
                ["uvx", "some-pkg"], uvx_spec="some-pkg"
            )
            is True
        )


class TestClientRefusalParity:
    """Both stdio clients refuse exactly what the resolver refuses."""

    def test_council_client_refuses_unpinned_npx(self):
        from skillweave.council.mcp_client import McpClientError, McpStdioClient

        with pytest.raises(McpClientError):
            McpStdioClient(["npx", "-y", "@perplexity-ai/mcp-server"])

    def test_council_client_admits_pinned_npx(self):
        from skillweave.council.mcp_client import McpStdioClient

        client = McpStdioClient(["npx", "-y", "@perplexity-ai/mcp-server@1.3.0"])
        assert client is not None

    def test_adapters_client_refuses_unpinned_npx(self):
        from skillweave.adapters.mcp_client import (
            McpClientError,
            McpStdioClient,
        )

        with pytest.raises(McpClientError):
            McpStdioClient(["npx", "-y", "@perplexity-ai/mcp-server"])
