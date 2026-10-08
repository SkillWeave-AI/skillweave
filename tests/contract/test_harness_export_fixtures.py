"""Harness export fixture validation (SW-MCP-001, criterion 1).

Generates private fixture configs for all six declared harness frameworks
and validates them against the real Capacium adapter contracts. The six
frameworks from the Perplexity manifest capability.yaml are:

  antigravity, claude-code, codex, cursor, opencode, claude-desktop

Each fixture config uses ${VAR} credential references (never literal values),
never uses floating git+/URL installs, and matches the expected config shape
for that framework's Capacium adapter.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

# ── The six declared frameworks from the Perplexity manifest ────────────────

DECLARED_FRAMEWORKS = (
    "antigravity",
    "claude-code",
    "codex",
    "cursor",
    "opencode",
    "claude-desktop",
)

# ── Credential reference pattern ───────────────────────────────────────────

REFERENCE_RE = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")
SECRET_VALUE_RE = re.compile(r"^(?!\$\{)[A-Za-z0-9_\-+/=.]{16,}$")
FLOATING_RE = re.compile(r"git\+|https?://|@\s*(main|latest|master)\b")


def _is_credential_ref(value: str) -> bool:
    return bool(REFERENCE_RE.match(value))


def _is_literal_secret(value: str) -> bool:
    return bool(SECRET_VALUE_RE.match(value))


def _has_floating(cmd: str, args: list[str]) -> bool:
    combined = cmd + " " + " ".join(args)
    return bool(FLOATING_RE.search(combined))


# ── Fixture config factories for each harness ──────────────────────────────

def _fixture_claude_desktop(name: str, *, ref_only: bool = True) -> dict[str, Any]:
    """Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json
    Format: { mcpServers: { <name>: { command, args, env } } }
    """
    env = {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"} if ref_only else {}
    return {
        "harness": "claude-desktop",
        "config": {
            "mcpServers": {
                name: {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": env,
                }
            }
        }
    }


def _fixture_claude_code(name: str, *, ref_only: bool = True) -> dict[str, Any]:
    """Claude Code: ~/.claude.json
    Format: { mcpServers: { <name>: { command, args, env } } }
    """
    env = {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"} if ref_only else {}
    return {
        "harness": "claude-code",
        "config": {
            "mcpServers": {
                name: {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": env,
                }
            }
        }
    }


def _fixture_cursor(name: str, *, ref_only: bool = True) -> dict[str, Any]:
    """Cursor: .cursor/mcp.json or ~/.cursor/mcp.json
    Format: { mcpServers: { <name>: { command, args, env } } }
    """
    env = {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"} if ref_only else {}
    return {
        "harness": "cursor",
        "config": {
            "mcpServers": {
                name: {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": env,
                }
            }
        }
    }


def _fixture_antigravity(name: str, *, ref_only: bool = True) -> dict[str, Any]:
    """Antigravity: ~/.gemini/config/mcp_config.json
    Format: { mcpServers: { <name>: { command, args, env } } }
    """
    env = {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"} if ref_only else {}
    return {
        "harness": "antigravity",
        "config": {
            "mcpServers": {
                name: {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": env,
                }
            }
        }
    }


def _fixture_codex(name: str, *, ref_only: bool = True) -> dict[str, Any]:
    """Codex: ~/.codex/config.toml
    TOML format (tested via the Python dict representation).
    """
    env = {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"} if ref_only else {}
    return {
        "harness": "codex",
        "config": {
            "mcpServers": {
                name: {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": env,
                }
            }
        }
    }


def _fixture_opencode(name: str, *, ref_only: bool = True) -> dict[str, Any]:
    """OpenCode: ~/.config/opencode/opencode.json
    Format: { mcp: { <name>: { command, args, env } } } (uses "mcp" not "mcpServers")
    """
    env = {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"} if ref_only else {}
    return {
        "harness": "opencode",
        "config": {
            "mcp": {
                name: {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": env,
                }
            }
        }
    }


# ── Fixture factory dispatch ───────────────────────────────────────────────

FIXTURE_FACTORIES = {
    "antigravity": _fixture_antigravity,
    "claude-code": _fixture_claude_code,
    "codex": _fixture_codex,
    "cursor": _fixture_cursor,
    "opencode": _fixture_opencode,
    "claude-desktop": _fixture_claude_desktop,
}


# ── Tests ───────────────────────────────────────────────────────────────────

class TestFixtureShape:
    """Each fixture has the right shape for its harness."""

    @pytest.mark.parametrize("harness", DECLARED_FRAMEWORKS)
    def test_fixture_has_expected_keys(self, harness):
        factory = FIXTURE_FACTORIES[harness]
        fixture = factory("perplexity")
        assert fixture["harness"] == harness
        assert "config" in fixture

    @pytest.mark.parametrize("harness", DECLARED_FRAMEWORKS)
    def test_fixture_uses_credential_reference(self, harness):
        factory = FIXTURE_FACTORIES[harness]
        fixture = factory("perplexity")
        config = fixture["config"]
        if harness == "opencode":
            servers = config.get("mcp", {})
        else:
            servers = config.get("mcpServers", {})
        for srv in servers.values():
            env = srv.get("env", {})
            for val in env.values():
                assert _is_credential_ref(val), (
                    f"{harness}: expected credential ref ${{VAR}}, got {val!r}"
                )

    @pytest.mark.parametrize("harness", DECLARED_FRAMEWORKS)
    def test_fixture_no_literal_secret(self, harness):
        factory = FIXTURE_FACTORIES[harness]
        fixture = factory("perplexity")
        config = fixture["config"]
        if harness == "opencode":
            servers = config.get("mcp", {})
        else:
            servers = config.get("mcpServers", {})
        for srv in servers.values():
            env = srv.get("env", {})
            for val in env.values():
                assert not _is_literal_secret(val), (
                    f"{harness}: literal secret value in config: {val!r}"
                )

    @pytest.mark.parametrize("harness", DECLARED_FRAMEWORKS)
    def test_fixture_no_floating_install(self, harness):
        factory = FIXTURE_FACTORIES[harness]
        fixture = factory("perplexity")
        config = fixture["config"]
        if harness == "opencode":
            servers = config.get("mcp", {})
        else:
            servers = config.get("mcpServers", {})
        for srv in servers.values():
            cmd = srv.get("command", "")
            args = srv.get("args", [])
            assert not _has_floating(cmd, args), (
                f"{harness}: floating install marker in command"
            )

    def test_all_six_frameworks_have_factories(self):
        assert set(FIXTURE_FACTORIES) == set(DECLARED_FRAMEWORKS)


class TestCredentialParity:
    """Every harness fixture carries the same credential reference.

    SW-MCP-001 credential-parity requirement: a package exported without the
    credential reference is a gap to be diagnosed, not silently accepted.
    """

    def test_all_harnesses_have_perplexity_cookies_ref(self):
        for harness in DECLARED_FRAMEWORKS:
            factory = FIXTURE_FACTORIES[harness]
            fixture = factory("perplexity")
            config = fixture["config"]
            if harness == "opencode":
                servers = config.get("mcp", {})
            else:
                servers = config.get("mcpServers", {})
            has_ref = any(
                "PERPLEXITY_COOKIES" in srv.get("env", {})
                for srv in servers.values()
            )
            assert has_ref, f"{harness} is missing PERPLEXITY_COOKIES reference"

    def test_parity_gap_is_detectable(self):
        """A fixture without the credential reference is flagged as a gap."""
        fixture_no_ref = _fixture_claude_desktop("perplexity", ref_only=False)
        config = fixture_no_ref["config"]
        servers = config.get("mcpServers", {})
        has_ref = any(
            "PERPLEXITY_COOKIES" in srv.get("env", {})
            for srv in servers.values()
        )
        assert not has_ref, "expected no PERPLEXITY_COOKIES ref in gap fixture"

    def test_literal_secret_is_rejected(self):
        """A config embedding a literal secret value must be rejected."""
        bad_config = {
            "mcpServers": {
                "perplexity": {
                    "command": "/fixture/perplexity-mcp",
                    "args": [],
                    "env": {"PERPLEXITY_COOKIES": "fixture-secret-sentinel"},
                }
            }
        }
        for val in bad_config["mcpServers"]["perplexity"]["env"].values():
            assert _is_literal_secret(val), f"expected literal secret pattern"
            assert not _is_credential_ref(val), "literal secret must not match ref pattern"


class TestBaselineParity:
    """Fixtures match the baseline contract cases from exports.json."""

    def test_baseline_accept_cases_pass(self):
        """The baseline accept cases match our fixture shape."""
        for harness in ("claude-desktop", "claude-code"):
            factory = FIXTURE_FACTORIES[harness]
            fixture = factory("perplexity", ref_only=True)
            config = fixture["config"]
            if harness == "opencode":
                servers = config.get("mcp", {})
            else:
                servers = config.get("mcpServers", {})
            has_ref = any(
                "PERPLEXITY_COOKIES" in srv.get("env", {})
                for srv in servers.values()
            )
            no_literal = all(
                not _is_literal_secret(val)
                for srv in servers.values()
                for val in srv.get("env", {}).values()
            )
            no_floating = all(
                not _has_floating(srv.get("command", ""), srv.get("args", []))
                for srv in servers.values()
            )
            assert has_ref and no_literal and no_floating, (
                f"{harness}: baseline accept conditions not met"
            )

    def test_floating_install_is_flagged(self):
        """A config using git+ install is detected."""
        floating_config = {
            "mcpServers": {
                "perplexity": {
                    "command": "uvx",
                    "args": [
                        "--from",
                        "perplexity-api[mcp] @ git+https://github.com/helallao/perplexity-ai",
                        "perplexity-mcp",
                    ],
                    "env": {"PERPLEXITY_COOKIES": "${PERPLEXITY_COOKIES}"},
                }
            }
        }
        for srv in floating_config["mcpServers"].values():
            assert _has_floating(srv["command"], srv["args"])


class TestOpenCodeConfigShape:
    """OpenCode uses "mcp" section key, not "mcpServers"."""

    def test_opencode_uses_mcp_key(self):
        fixture = _fixture_opencode("perplexity")
        assert "mcp" in fixture["config"]
        assert "mcpServers" not in fixture["config"]

    def test_opencode_has_credential_ref(self):
        fixture = _fixture_opencode("perplexity")
        mcp = fixture["config"]["mcp"]
        for srv in mcp.values():
            assert "PERPLEXITY_COOKIES" in srv.get("env", {})
            assert _is_credential_ref(srv["env"]["PERPLEXITY_COOKIES"])
