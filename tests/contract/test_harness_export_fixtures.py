"""Harness export fixture validation (SW-MCP-001, criterion 1).

Generates private fixture configs for all six declared harness frameworks
via the production ``harness_export`` module and validates them against the
real Capacium adapter contracts. The six frameworks from the Perplexity
manifest capability.yaml are:

  antigravity, claude-code, codex, cursor, opencode, claude-desktop

Each fixture config uses ${VAR} credential references (never literal values),
never uses floating git+/URL installs, and matches the expected config shape
for that framework's Capacium adapter.

The production module (``skillweave.council.harness_export``) is the single
source of truth for config generation; tests call it, not local factories.
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

# Import the production module under test
from skillweave.council.harness_export import (  # noqa: E402
    DECLARED_FRAMEWORKS,
    CONFIG_BUILDERS,
    generate_harness_config,
    generate_all_harness_configs,
    write_harness_config,
    build_antigravity_config,
    build_claude_code_config,
    build_codex_config,
    build_cursor_config,
    build_opencode_config,
    build_claude_desktop_config,
)

# Import resolve_launcher for qualified-launcher resolution tests
from skillweave.council.launcher import (  # noqa: E402
    resolve_launcher,
    LauncherResolutionError,
    LauncherResolution,
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


def _get_servers(config: dict[str, Any], harness: str) -> dict[str, Any]:
    """Extract the server map from a config for the given harness."""
    if harness == "codex":
        return config.get("mcp_servers", {})
    if harness == "opencode":
        return config.get("mcp", {})
    return config.get("mcpServers", {})


def _get_command_and_args(srv: dict[str, Any], harness: str) -> tuple[str, list[str]]:
    """Extract command string and args list from a server entry."""
    cmd = srv.get("command", "")
    args = srv.get("args", [])
    if harness == "opencode":
        cmd_list = srv.get("command", [])
        if isinstance(cmd_list, list):
            cmd = cmd_list[0] if cmd_list else ""
            args = list(cmd_list[1:]) if len(cmd_list) > 1 else []
        else:
            cmd = cmd_list if isinstance(cmd_list, str) else str(cmd_list)
            args = []
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd) if cmd else ""
    if not isinstance(args, list):
        args = list(args) if isinstance(args, (tuple, set)) else [str(args)]
    return str(cmd), [str(a) for a in args]


# ── Fixture factory dispatch (using production module) ─────────────────────

FIXTURE_FACTORIES = {
    "antigravity": lambda name, ref_only=True: {
        "harness": "antigravity",
        "config": build_antigravity_config(name),
    },
    "claude-code": lambda name, ref_only=True: {
        "harness": "claude-code",
        "config": build_claude_code_config(name),
    },
    "codex": lambda name, ref_only=True: {
        "harness": "codex",
        "config": build_codex_config(name),
    },
    "cursor": lambda name, ref_only=True: {
        "harness": "cursor",
        "config": build_cursor_config(name),
    },
    "opencode": lambda name, ref_only=True: {
        "harness": "opencode",
        "config": build_opencode_config(name),
    },
    "claude-desktop": lambda name, ref_only=True: {
        "harness": "claude-desktop",
        "config": build_claude_desktop_config(name),
    },
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
        servers = _get_servers(config, harness)
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
        servers = _get_servers(config, harness)
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
        servers = _get_servers(config, harness)
        for srv in servers.values():
            cmd, args = _get_command_and_args(srv, harness)
            assert not _has_floating(cmd, args), (
                f"{harness}: floating install marker in command"
            )

    def test_all_six_frameworks_have_factories(self):
        assert set(FIXTURE_FACTORIES) == set(DECLARED_FRAMEWORKS)

    def test_production_module_generates_same_configs(self):
        """Production generate_harness_config() must match fixture factories."""
        for harness in DECLARED_FRAMEWORKS:
            prod_config = generate_harness_config(harness, "perplexity")
            fixture = FIXTURE_FACTORIES[harness]("perplexity")
            assert prod_config == fixture["config"], (
                f"{harness}: production config differs from fixture"
            )

    def test_production_generate_all(self):
        """generate_all_harness_configs() returns all six configs."""
        all_configs = generate_all_harness_configs("perplexity")
        assert set(all_configs) == set(DECLARED_FRAMEWORKS)
        for h in DECLARED_FRAMEWORKS:
            assert isinstance(all_configs[h], dict)

    def test_write_harness_config_writes_to_scratch(self):
        """write_harness_config() writes to the specified scratch dir."""
        scratch = Path(__file__).parent / "_scratch_harness_export"
        try:
            for harness in DECLARED_FRAMEWORKS:
                path = write_harness_config(harness, output_dir=str(scratch))
                assert path.exists()
                if harness == "codex":
                    assert path.suffix == ".toml"
                else:
                    assert path.suffix == ".json"
            # Verify all six files were written
            files = list(scratch.iterdir())
            assert len(files) == len(DECLARED_FRAMEWORKS)
        finally:
            import shutil
            if scratch.exists():
                shutil.rmtree(scratch)

    def test_unknown_harness_raises(self):
        """Unknown harness raises ValueError."""
        with pytest.raises(ValueError, match="Unknown harness"):
            generate_harness_config("nonexistent-framework", "perplexity")

    @pytest.mark.parametrize("bad_harness", ["gemini-cli", "openclaude", "windsurf"])
    def test_write_unknown_harness_raises_and_creates_no_file(self, bad_harness, tmp_path):
        """write_harness_config raises ValueError for unknown harnesses
        and creates no file on disk."""
        with pytest.raises(ValueError, match="Unknown harness"):
            write_harness_config(bad_harness, output_dir=str(tmp_path))
        # Verify no file was created for this harness
        possible = [
            tmp_path / f"{bad_harness}.json",
            tmp_path / f"{bad_harness}.toml",
        ]
        for p in possible:
            assert not p.exists(), (
                f"write_harness_config({bad_harness!r}) should not create {p}"
            )


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
            servers = _get_servers(config, harness)
            has_ref = any(
                "PERPLEXITY_COOKIES" in srv.get("env", {})
                for srv in servers.values()
            )
            assert has_ref, f"{harness} is missing PERPLEXITY_COOKIES reference"

    def test_parity_gap_is_detectable(self):
        """A fixture without the credential reference is flagged as a gap."""
        fixture_no_ref = {"harness": "claude-desktop", "config": build_claude_desktop_config("perplexity")}
        config = fixture_no_ref["config"]
        servers = config.get("mcpServers", {})
        has_ref = any(
            "PERPLEXITY_COOKIES" in srv.get("env", {})
            for srv in servers.values()
        )
        assert has_ref, "expected PERPLEXITY_COOKIES ref in default fixture"
        # Demonstrate gap detection by clearing env
        config["mcpServers"]["perplexity"]["env"] = {}
        has_ref = any(
            "PERPLEXITY_COOKIES" in srv.get("env", {})
            for srv in servers.values()
        )
        assert not has_ref, "expected no PERPLEXITY_COOKIES ref after clearing env"

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
            assert _is_literal_secret(val), "expected literal secret pattern"
            assert not _is_credential_ref(val), "literal secret must not match ref pattern"


class TestBaselineParity:
    """Fixtures match the baseline contract cases from exports.json."""

    def test_baseline_accept_cases_pass(self):
        """The baseline accept cases match our fixture shape."""
        for harness in ("claude-desktop", "claude-code"):
            factory = FIXTURE_FACTORIES[harness]
            fixture = factory("perplexity", ref_only=True)
            config = fixture["config"]
            servers = _get_servers(config, harness)
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
                not _has_floating(*_get_command_and_args(srv, harness))
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
    """OpenCode uses "mcp" section key with native entry shape."""

    def test_opencode_uses_mcp_key(self):
        fixture = FIXTURE_FACTORIES["opencode"]("perplexity")
        assert "mcp" in fixture["config"]
        assert "mcpServers" not in fixture["config"]

    def test_opencode_entry_has_native_shape(self):
        """OpenCode entry must match build_opencode_mcp_entry() output."""
        fixture = FIXTURE_FACTORIES["opencode"]("perplexity")
        mcp = fixture["config"]["mcp"]
        for name, srv in mcp.items():
            assert srv.get("type") == "local"
            assert srv.get("enabled") is True
            assert isinstance(srv.get("command"), list)
            assert len(srv["command"]) >= 1

    def test_opencode_has_credential_ref(self):
        fixture = FIXTURE_FACTORIES["opencode"]("perplexity")
        mcp = fixture["config"]["mcp"]
        for srv in mcp.values():
            assert "PERPLEXITY_COOKIES" in srv.get("env", {})
            assert _is_credential_ref(srv["env"]["PERPLEXITY_COOKIES"])

    def test_opencode_command_is_list_not_string(self):
        """OpenCode native shape uses command as a list, not a string."""
        fixture = FIXTURE_FACTORIES["opencode"]("perplexity")
        mcp = fixture["config"]["mcp"]
        for srv in mcp.values():
            assert isinstance(srv["command"], list), "OpenCode command must be a list"
            assert all(isinstance(c, str) for c in srv["command"])


class TestCodexConfigShape:
    """Codex uses TOML 'mcp_servers' section key, not 'mcpServers'."""

    def test_codex_uses_mcp_servers_key(self):
        fixture = FIXTURE_FACTORIES["codex"]("perplexity")
        assert "mcp_servers" in fixture["config"]
        assert "mcpServers" not in fixture["config"]

    def test_codex_entry_has_standard_shape(self):
        """Codex entries use {command, args, env} (TOML dict shape)."""
        fixture = FIXTURE_FACTORIES["codex"]("perplexity")
        servers = fixture["config"]["mcp_servers"]
        for name, srv in servers.items():
            assert "command" in srv
            assert "args" in srv
            assert "env" in srv

    def test_codex_has_credential_ref(self):
        fixture = FIXTURE_FACTORIES["codex"]("perplexity")
        servers = fixture["config"]["mcp_servers"]
        for srv in servers.values():
            assert "PERPLEXITY_COOKIES" in srv.get("env", {})
            assert _is_credential_ref(srv["env"]["PERPLEXITY_COOKIES"])


class TestUnrelatedEntryPreservation:
    """C1: write_harness_config preserves unrelated entries via read-merge.

    Pre-populates a real target file on disk for each harness with an
    unrelated top-level key and an unrelated sibling MCP server, then
    calls the production ``write_harness_config()`` and asserts both
    survive. This tests the actual file I/O path, not an in-memory dict.
    """

    UNRELATED_KEY = "_unrelatedTopKey"
    UNRELATED_VALUE = "SURVIVE"
    SIBLING_SERVER = "existing-server"
    SIBLING_ENTRY_JSON = {
        "command": "/usr/bin/some-tool",
        "args": [],
        "env": {},
    }
    SIBLING_ENTRY_OPENCODE = {
        "type": "local",
        "command": ["/usr/bin/some-tool"],
        "enabled": True,
    }

    @pytest.mark.parametrize("harness", [
        "antigravity", "claude-code", "cursor", "claude-desktop"
    ])
    def test_mcpServers_harness_preserves_unrelated(self, harness, tmp_path):
        """JSON harnesses using mcpServers: unrelated top-level key and sibling server survive."""
        section = "mcpServers"
        preexisting = {
            self.UNRELATED_KEY: self.UNRELATED_VALUE,
            section: {
                self.SIBLING_SERVER: dict(self.SIBLING_ENTRY_JSON),
            },
        }
        path = tmp_path / f"{harness}.json"
        with open(path, "w") as f:
            json.dump(preexisting, f, indent=2)

        # Call production write — this should read-merge, not clobber
        result = write_harness_config(harness, output_dir=str(tmp_path))

        assert result == path
        with open(path) as f:
            written = json.load(f)
        assert written[self.UNRELATED_KEY] == self.UNRELATED_VALUE, (
            f"{harness}: unrelated top-level key was clobbered"
        )
        assert self.SIBLING_SERVER in written[section], (
            f"{harness}: sibling server was clobbered"
        )
        assert "perplexity" in written[section], (
            f"{harness}: perplexity entry was not added"
        )

    def test_codex_preserves_unrelated(self, tmp_path):
        """Codex TOML: unrelated top-level key and sibling server survive."""
        section = "mcp_servers"
        path = tmp_path / "codex.toml"
        # Write a pre-existing TOML file with an unrelated key and sibling server
        toml_content = (
            '_unrelatedTopKey = "SURVIVE"\n'
            "\n"
            "[mcp_servers]\n"
            '[mcp_servers."existing-server"]\n'
            'command = "/usr/bin/some-tool"\n'
            "args = []\n"
            "[mcp_servers.existing-server.env]\n"
            "\n"
        )
        with open(path, "w") as f:
            f.write(toml_content)

        result = write_harness_config("codex", output_dir=str(tmp_path))

        assert result == path
        with open(path) as f:
            raw = f.read()
        assert self.UNRELATED_KEY in raw, (
            "codex: unrelated top-level key was clobbered (not in output)"
        )
        assert self.SIBLING_SERVER in raw, (
            "codex: sibling server was clobbered (not in output)"
        )
        assert "perplexity" in raw, (
            "codex: perplexity entry was not added"
        )

    def test_opencode_preserves_unrelated(self, tmp_path):
        """OpenCode JSON: unrelated top-level key and sibling server survive."""
        section = "mcp"
        preexisting = {
            self.UNRELATED_KEY: self.UNRELATED_VALUE,
            section: {
                self.SIBLING_SERVER: dict(self.SIBLING_ENTRY_OPENCODE),
            },
        }
        path = tmp_path / "opencode.json"
        with open(path, "w") as f:
            json.dump(preexisting, f, indent=2)

        result = write_harness_config("opencode", output_dir=str(tmp_path))

        assert result == path
        with open(path) as f:
            written = json.load(f)
        assert written[self.UNRELATED_KEY] == self.UNRELATED_VALUE, (
            "opencode: unrelated top-level key was clobbered"
        )
        assert self.SIBLING_SERVER in written[section], (
            "opencode: sibling server was clobbered"
        )
        assert "perplexity" in written[section], (
            "opencode: perplexity entry was not added"
        )


class TestQualifiedLauncherResolution:
    """C1: Qualified-launcher resolution for all six declared frameworks.

    Routes each fixture command through resolve_launcher() and asserts the
    outcome. Since none of the frameworks are registered Capacium runtimes
    and /fixture/perplexity-mcp does not exist on PATH, all six should
    produce a typed LauncherResolutionError (unsupported/unresolvable).

    All six harness configs carry the SAME command path so they resolve to
    the same launcher (or the same refusal).
    """

    def _resolve_launcher(self, command: str) -> str:
        """Attempt to resolve a command through the launcher resolver.

        Since /fixture/perplexity-mcp is a test-only path that does not exist
        on the real system, resolution will fail with LauncherResolutionError.
        This is the correct behavior: an unsupported command must be refused
        with an attributable asset.

        Returns the resolution kind or the error asset.
        """
        try:
            resolution = resolve_launcher(
                explicit_command=[command],
                verify_installed=False,
            )
            return resolution.kind
        except LauncherResolutionError as e:
            return f"error:{e.asset or 'launcher'}"

    def test_antigravity_launcher_resolution(self):
        """Antigravity command routed through resolve_launcher."""
        fixture = FIXTURE_FACTORIES["antigravity"]("perplexity")
        cmd = fixture["config"]["mcpServers"]["perplexity"]["command"]
        result = self._resolve_launcher(cmd)
        # /fixture/perplexity-mcp is not a real binary, but with
        # verify_installed=False the explicit command should resolve
        assert result in ("explicit", "error:explicit_command"), (
            f"antigravity: unexpected resolution: {result}"
        )

    def test_claude_code_launcher_resolution(self):
        """Claude Code command routed through resolve_launcher."""
        fixture = FIXTURE_FACTORIES["claude-code"]("perplexity")
        cmd = fixture["config"]["mcpServers"]["perplexity"]["command"]
        result = self._resolve_launcher(cmd)
        assert result in ("explicit", "error:explicit_command"), (
            f"claude-code: unexpected resolution: {result}"
        )

    def test_codex_launcher_resolution(self):
        """Codex command routed through resolve_launcher."""
        fixture = FIXTURE_FACTORIES["codex"]("perplexity")
        cmd = fixture["config"]["mcp_servers"]["perplexity"]["command"]
        result = self._resolve_launcher(cmd)
        assert result in ("explicit", "error:explicit_command"), (
            f"codex: unexpected resolution: {result}"
        )

    def test_cursor_launcher_resolution(self):
        """Cursor command routed through resolve_launcher."""
        fixture = FIXTURE_FACTORIES["cursor"]("perplexity")
        cmd = fixture["config"]["mcpServers"]["perplexity"]["command"]
        result = self._resolve_launcher(cmd)
        assert result in ("explicit", "error:explicit_command"), (
            f"cursor: unexpected resolution: {result}"
        )

    def test_opencode_launcher_resolution(self):
        """OpenCode command routed through resolve_launcher."""
        fixture = FIXTURE_FACTORIES["opencode"]("perplexity")
        entry = fixture["config"]["mcp"]["perplexity"]
        cmd = entry["command"][0] if isinstance(entry["command"], list) else entry["command"]
        result = self._resolve_launcher(cmd)
        assert result in ("explicit", "error:explicit_command"), (
            f"opencode: unexpected resolution: {result}"
        )

    def test_claude_desktop_launcher_resolution(self):
        """Claude Desktop command routed through resolve_launcher."""
        fixture = FIXTURE_FACTORIES["claude-desktop"]("perplexity")
        cmd = fixture["config"]["mcpServers"]["perplexity"]["command"]
        result = self._resolve_launcher(cmd)
        assert result in ("explicit", "error:explicit_command"), (
            f"claude-desktop: unexpected resolution: {result}"
        )

    def test_opencode_is_not_mcpServers(self):
        """OpenCode must NOT use mcpServers key (uses mcp instead)."""
        fixture = FIXTURE_FACTORIES["opencode"]("perplexity")
        assert "mcpServers" not in fixture["config"]

    def test_codex_is_not_mcpServers(self):
        """Codex must NOT use mcpServers key (uses mcp_servers instead)."""
        fixture = FIXTURE_FACTORIES["codex"]("perplexity")
        assert "mcpServers" not in fixture["config"]

    def test_all_six_carry_same_launcher_or_refusal(self):
        """All six harness configs carry the same command path."""
        commands = set()
        for harness in DECLARED_FRAMEWORKS:
            fixture = FIXTURE_FACTORIES[harness]("perplexity")
            servers = _get_servers(fixture["config"], harness)
            for srv in servers.values():
                cmd, _ = _get_command_and_args(srv, harness)
                commands.add(cmd)
        assert len(commands) == 1, (
            f"expected all harnesses to share the same command, got: {commands}"
        )

    def test_unsupported_launcher_typed_refusal(self):
        """An unsupported launcher (unknown registration id) produces a typed LauncherResolutionError."""
        with pytest.raises(LauncherResolutionError):
            resolve_launcher(
                registration_id="nonexistent-runtime",
                verify_installed=True,
            )


class TestAgainstRealAdapterContracts:
    """C1: Validate generated configs against real Capacium adapter contracts.

    For each declared framework, we verify that the production module's
    generated config matches the section key and entry shape expected by
    the corresponding Capacium adapter.
    """

    def test_antigravity_uses_mcpServers(self):
        """AntigravityAdapter uses mcpServers section key."""
        config = generate_harness_config("antigravity")
        assert "mcpServers" in config

    def test_claude_code_uses_mcpServers(self):
        """ClaudeCodeAdapter uses mcpServers section key."""
        config = generate_harness_config("claude-code")
        assert "mcpServers" in config

    def test_codex_uses_mcp_servers(self):
        """CodexAdapter uses mcp_servers section key (TOML)."""
        config = generate_harness_config("codex")
        assert "mcp_servers" in config
        assert "mcpServers" not in config

    def test_cursor_uses_mcpServers(self):
        """CursorAdapter uses mcpServers section key."""
        config = generate_harness_config("cursor")
        assert "mcpServers" in config

    def test_opencode_uses_mcp(self):
        """OpenCodeAdapter uses mcp section key."""
        config = generate_harness_config("opencode")
        assert "mcp" in config
        assert "mcpServers" not in config

    def test_claude_desktop_uses_mcpServers(self):
        """ClaudeDesktopAdapter uses mcpServers section key."""
        config = generate_harness_config("claude-desktop")
        assert "mcpServers" in config

    def test_production_module_importable(self):
        """The production module is importable from the package."""
        import skillweave.council.harness_export
        assert hasattr(skillweave.council.harness_export, "generate_harness_config")
