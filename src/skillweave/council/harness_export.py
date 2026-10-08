"""Production harness config generator (SW-MCP-001, criterion 1).

Generates private fixture configs for all six declared Perplexity manifest
frameworks, matching their real Capacium adapter config contracts. Output
is written to a caller-specified private scratch directory; no real host
configs are ever touched.

Framework config formats (verified against Capacium adapter source):

  ==============  ===========  ===========================================
  Framework       Section key  Entry shape
  ==============  ===========  ===========================================
  antigravity     mcpServers   {command, args, env}
  claude-code     mcpServers   {command, args, env}
  codex           mcp_servers  {command, args, env} (TOML format)
  cursor          mcpServers   {command, args, env}
  opencode        mcp          {"type":"local","command":[],"enabled":true}
  claude-desktop  mcpServers   {command, args, env}
  ==============  ===========  ===========================================
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "DECLARED_FRAMEWORKS",
    "CONFIG_BUILDERS",
    "generate_harness_config",
    "generate_all_harness_configs",
    "write_harness_config",
    "build_antigravity_config",
    "build_claude_code_config",
    "build_codex_config",
    "build_cursor_config",
    "build_opencode_config",
    "build_claude_desktop_config",
]

# ── The six declared frameworks from the Perplexity manifest capability.yaml

DECLARED_FRAMEWORKS: tuple[str, ...] = (
    "antigravity",
    "claude-code",
    "codex",
    "cursor",
    "opencode",
    "claude-desktop",
)

# ── Credential reference pattern

CREDENTIAL_REF = "${PERPLEXITY_COOKIES}"


# ── Config builders


def build_claude_desktop_config(
    name: str, command: str = "/fixture/perplexity-mcp"
) -> dict[str, Any]:
    """Claude Desktop: ~/Library/Application Support/Claude/claude_desktop_config.json

    Uses ``mcpServers`` section key with ``{command, args, env}`` entries.
    Matches ClaudeDesktopAdapter which uses ``McpConfigPatcher.inject_json_mcp_server()``
    with ``mcp_section_key="mcpServers"``.
    """
    return {
        "mcpServers": {
            name: {
                "command": command,
                "args": [],
                "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
            }
        }
    }


def build_claude_code_config(
    name: str, command: str = "/fixture/perplexity-mcp"
) -> dict[str, Any]:
    """Claude Code: ~/.claude.json

    Uses ``mcpServers`` section key with ``{command, args, env}`` entries.
    Matches ClaudeCodeAdapter which uses ``inject_json_mcp_server(mcp_section_key="mcpServers")``.
    """
    return {
        "mcpServers": {
            name: {
                "command": command,
                "args": [],
                "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
            }
        }
    }


def build_codex_config(
    name: str, command: str = "/fixture/perplexity-mcp"
) -> dict[str, Any]:
    """Codex: ~/.codex/config.toml

    Uses TOML ``mcp_servers`` section key (NOT ``mcpServers``).
    Matches CodexAdapter which uses ``config.setdefault("mcp_servers", {})``
    and writes TOML via ``McpConfigPatcher.write_toml()``.
    """
    return {
        "mcp_servers": {
            name: {
                "command": command,
                "args": [],
                "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
            }
        }
    }


def build_cursor_config(
    name: str, command: str = "/fixture/perplexity-mcp"
) -> dict[str, Any]:
    """Cursor: .cursor/mcp.json or ~/.cursor/mcp.json

    Uses ``mcpServers`` section key with ``{command, args, env}`` entries.
    Matches CursorAdapter which uses ``inject_json_mcp_server(mcp_section_key="mcpServers")``.
    """
    return {
        "mcpServers": {
            name: {
                "command": command,
                "args": [],
                "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
            }
        }
    }


def build_opencode_config(
    name: str, command: str = "/fixture/perplexity-mcp"
) -> dict[str, Any]:
    """OpenCode: ~/.config/opencode/opencode.json

    Uses ``mcp`` section key (NOT ``mcpServers``). Local stdio servers use
    ``{"type":"local","command":["cmd","..."],"enabled":true}`` shape.
    Matches ``McpConfigPatcher.build_opencode_mcp_entry()`` and
    ``OpenCodeAdapter.install_mcp_server()`` which calls
    ``config.setdefault("mcp", {})``.
    """
    return {
        "mcp": {
            name: {
                "type": "local",
                "command": [command],
                "enabled": True,
                "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
            }
        }
    }


def build_antigravity_config(
    name: str, command: str = "/fixture/perplexity-mcp"
) -> dict[str, Any]:
    """Antigravity: ~/.gemini/config/mcp_config.json

    Uses ``mcpServers`` section key with ``{command, args, env}`` entries.
    Matches AntigravityAdapter which uses ``inject_json_mcp_server(mcp_section_key="mcpServers")``.
    """
    return {
        "mcpServers": {
            name: {
                "command": command,
                "args": [],
                "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
            }
        }
    }


# ── Builder dispatch

CONFIG_BUILDERS: dict[str, Any] = {
    "antigravity": build_antigravity_config,
    "claude-code": build_claude_code_config,
    "codex": build_codex_config,
    "cursor": build_cursor_config,
    "opencode": build_opencode_config,
    "claude-desktop": build_claude_desktop_config,
}


# ── Generation API


def generate_harness_config(
    harness: str,
    name: str = "perplexity",
    command: str = "/fixture/perplexity-mcp",
) -> dict[str, Any]:
    """Generate a harness config dict for the given framework.

    Args:
        harness: Framework name (one of ``DECLARED_FRAMEWORKS``).
        name: MCP server entry name (e.g. ``"perplexity"``).
        command: The MCP server command path.

    Returns:
        A dict representing the harness config with the correct section key
        and entry shape for that framework.

    Raises:
        ValueError: If *harness* is not a known framework.
    """
    builder = CONFIG_BUILDERS.get(harness)
    if builder is None:
        raise ValueError(
            f"Unknown harness: {harness!r}; "
            f"known: {sorted(CONFIG_BUILDERS)}"
        )
    return builder(name, command)


def generate_all_harness_configs(
    name: str = "perplexity",
    command: str = "/fixture/perplexity-mcp",
) -> dict[str, dict[str, Any]]:
    """Generate configs for all six declared frameworks.

    Returns a dict mapping harness name to its config dict.
    """
    return {
        h: generate_harness_config(h, name, command)
        for h in DECLARED_FRAMEWORKS
    }


def write_harness_config(
    harness: str,
    name: str = "perplexity",
    output_dir: str | os.PathLike[str] = ".",
    command: str = "/fixture/perplexity-mcp",
) -> Path:
    """Write a harness config to a private scratch directory.

    Never writes to real host config paths -- only to the specified
    *output_dir*. The output format is JSON for all frameworks except
    ``codex`` which uses TOML.

    Args:
        harness: Framework name.
        name: MCP server entry name.
        output_dir: Scratch directory path (created if missing).
        command: MCP server command path.

    Returns:
        The path to the written config file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = generate_harness_config(harness, name, command)

    if harness == "codex":
        path = output_dir / f"{harness}.toml"
        _write_toml_simple(path, config)
    else:
        path = output_dir / f"{harness}.json"
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
    return path


# ── TOML writer (lightweight, no external dependency)


def _write_toml_simple(path: Path, data: dict[str, Any]) -> None:
    """Write a simple nested-dict structure as TOML.

    Handles the nesting depth needed for Codex configs:
      [mcp_servers]
      [mcp_servers.<name>]
      command = "..."
      args = []
      [mcp_servers.<name>.env]
      KEY = "value"
    """
    with open(path, "w") as f:
        _write_toml_level(f, data, "")


def _write_toml_level(
    f: Any, data: dict[str, Any], prefix: str
) -> None:
    import json as _json

    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if prefix:
                f.write(f"[{full_key}]\n")
            else:
                f.write(f"[{key}]\n")
            _write_toml_level(f, value, full_key)
        elif isinstance(value, bool):
            f.write(f"{key} = {'true' if value else 'false'}\n")
        elif isinstance(value, (int, float)):
            f.write(f"{key} = {value}\n")
        elif isinstance(value, list):
            f.write(f"{key} = {_json.dumps(value)}\n")
        else:
            f.write(f"{key} = {_json.dumps(str(value))}\n")
