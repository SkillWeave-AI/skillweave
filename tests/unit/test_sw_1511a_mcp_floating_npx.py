"""SW-1511A-GATE-001 / F1: an unpinned npm fetch is refused as floating.

The launcher admitted ``npx -y <pkg>`` as a non-floating explicit command, even
though npx fetches and runs whatever the registry resolves at run time -- the
same hazard the ``git+``/``http(s)`` markers and the unpinned-``uvx`` rule
already refuse. ``pnpm dlx``, ``bunx``/``bun x`` and ``yarn dlx`` are the same
publish-registry fetch at run time and are refused on identical terms. The
product's own pinned local ``node <path>`` command must stay admitted, and a
local path containing the substring ``http`` must not be misread as a URL.

``pnpm exec`` (like ``yarn exec``/``yarn node``/bare ``yarn <script>``) is
deliberately *admitted*: it runs an already-resolved local binary or project
script and never reaches the registry for a package spec, so an unpinned
argument to it names a local program rather than a remote fetch.

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


#: ``(command, unpinned form)`` for each newly-refused npm fetch launcher. The
#: unpinned form is the exact command that reached the HEAD as *admitted*.
_NPM_FETCH_FORMS = [
    ("pnpm dlx", ["pnpm", "dlx", "@perplexity-ai/mcp-server"]),
    ("bunx", ["bunx", "@perplexity-ai/mcp-server"]),
    ("bun x", ["bun", "x", "@perplexity-ai/mcp-server"]),
    ("yarn dlx", ["yarn", "dlx", "@perplexity-ai/mcp-server"]),
]

#: ``(command, pinned form, local form)`` per newly-refused launcher.
_NPM_FETCH_ADMITTED = [
    (
        "pnpm dlx",
        ["pnpm", "dlx", "@perplexity-ai/mcp-server@1.3.0"],
        ["pnpm", "dlx", "./node_modules/.bin/mcp-server.js"],
    ),
    (
        "bunx",
        ["bunx", "@perplexity-ai/mcp-server@1.3.0"],
        ["bunx", "/opt/mcp/index.js"],
    ),
    (
        "bun x",
        ["bun", "x", "@perplexity-ai/mcp-server@1.3.0"],
        ["bun", "x", "file:/opt/mcp/pkg.tgz"],
    ),
    (
        "yarn dlx",
        ["yarn", "dlx", "@perplexity-ai/mcp-server@1.3.0"],
        ["yarn", "dlx", "./node_modules/.bin/mcp-server.js"],
    ),
]


class TestOtherNpmFetchLaunchersRefusedUnpinned:
    """``pnpm dlx``/``bunx``/``bun x``/``yarn dlx`` are the same hazard as npx."""

    @pytest.mark.parametrize(
        "command", [c for _, c in _NPM_FETCH_FORMS], ids=[n for n, _ in _NPM_FETCH_FORMS]
    )
    def test_unpinned_fetch_form_is_floating(self, command):
        assert L.is_floating_command(command) is True

    @pytest.mark.parametrize(
        "command", [c for _, c in _NPM_FETCH_FORMS], ids=[n for n, _ in _NPM_FETCH_FORMS]
    )
    def test_resolution_refuses_the_unpinned_fetch_form(self, command):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(explicit_command=command)
        assert excinfo.value.asset == "explicit_command"

    @pytest.mark.parametrize(
        "command", [c for _, c in _NPM_FETCH_FORMS], ids=[n for n, _ in _NPM_FETCH_FORMS]
    )
    def test_multiple_flags_before_the_spec_are_still_refused(self, command):
        # ``--yes``/``-y`` (and any other option) must not smuggle the spec past
        # the fetch rule.
        flags = command[:1] + command[1:-1] + ["-y", command[-1]]
        assert L.is_floating_command(flags) is True

    @pytest.mark.parametrize(
        "command", [c for _, c in _NPM_FETCH_FORMS], ids=[n for n, _ in _NPM_FETCH_FORMS]
    )
    def test_an_absolute_path_launcher_token_is_still_detected(self, command):
        assert L.is_floating_command(["/usr/local/bin/" + command[0]] + command[1:]) is True


class TestOtherNpmFetchLaunchersAdmitPinnedAndLocal:
    @pytest.mark.parametrize(
        "pinned",
        [p for _, p, _ in _NPM_FETCH_ADMITTED],
        ids=[n for n, _, _ in _NPM_FETCH_ADMITTED],
    )
    def test_exactly_pinned_fetch_form_is_not_floating(self, pinned):
        assert L.is_floating_command(pinned) is False
        assert L.resolve_launcher(explicit_command=pinned).kind == "explicit"

    @pytest.mark.parametrize(
        "local",
        [loc for _, _, loc in _NPM_FETCH_ADMITTED],
        ids=[n for n, _, _ in _NPM_FETCH_ADMITTED],
    )
    def test_a_local_fetch_target_is_not_a_remote_fetch(self, local):
        assert L.is_floating_command(local) is False
        assert L.resolve_launcher(explicit_command=local).kind == "explicit"


class TestNonFetchPackageManagerFormsStayAdmitted:
    """``pnpm exec``/``yarn exec``/``yarn node``/bare scripts never fetch."""

    @pytest.mark.parametrize(
        "command",
        [
            ["pnpm", "exec", "@perplexity-ai/mcp-server"],
            ["pnpm", "install"],
            ["pnpm", "dlx"],
            ["yarn", "exec", "@perplexity-ai/mcp-server"],
            ["yarn", "node", "/opt/mcp/index.js"],
            ["yarn", "install"],
            ["yarn", "@perplexity-ai/mcp-server"],
            ["bun", "run", "dev"],
            ["bun", "install"],
        ],
    )
    def test_a_non_fetch_subcommand_other_than_dlx_is_admitted(self, command):
        assert L.is_floating_command(command) is False

    def test_a_pnpm_dlx_token_outside_first_position_is_not_a_dlx_fetch(self):
        # ``x dlx <pkg>`` -- ``dlx`` is not the subcommand after ``pnpm``, so it
        # is a plain argument and no fetch rule fires.
        assert L.is_floating_command(["pnpm", "x", "dlx", "pkg"]) is False


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

    @pytest.mark.parametrize(
        "command", [c for _, c in _NPM_FETCH_FORMS], ids=[n for n, _ in _NPM_FETCH_FORMS]
    )
    def test_council_client_refuses_every_unpinned_fetch_form(self, command):
        from skillweave.council.mcp_client import McpClientError, McpStdioClient

        with pytest.raises(McpClientError):
            McpStdioClient(command)

    @pytest.mark.parametrize(
        "pinned",
        [p for _, p, _ in _NPM_FETCH_ADMITTED],
        ids=[n for n, _, _ in _NPM_FETCH_ADMITTED],
    )
    def test_council_client_admits_every_pinned_fetch_form(self, pinned):
        from skillweave.council.mcp_client import McpStdioClient

        assert McpStdioClient(pinned) is not None

    @pytest.mark.parametrize(
        "command", [c for _, c in _NPM_FETCH_FORMS], ids=[n for n, _ in _NPM_FETCH_FORMS]
    )
    def test_adapters_client_refuses_every_unpinned_fetch_form(self, command):
        from skillweave.adapters.mcp_client import McpClientError, McpStdioClient

        with pytest.raises(McpClientError):
            McpStdioClient(command)
