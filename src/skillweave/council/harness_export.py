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

Write behaviour follows the Capacium adapter contract pattern:
read existing -> setdefault(section) -> inject/replace named entry ->
write. Unrelated top-level keys and sibling server entries are
preserved. Only the entry with the same name is replaced.
"""

from __future__ import annotations

import json
import os
import tempfile
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
    "_section_key_for_harness",
    "_build_entry_for_harness",
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


def _section_key_for_harness(harness: str) -> str:
    """Return the config section key for the given harness."""
    if harness == "codex":
        return "mcp_servers"
    if harness == "opencode":
        return "mcp"
    return "mcpServers"


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning {} if missing or unparseable."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file, returning {} if missing or unparseable."""
    if not path.exists():
        return {}
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (ImportError, Exception):
        return {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON file (write to temp, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_toml(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a TOML file (write to temp, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".toml", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            _write_toml_level(f, data, "")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_harness_config(
    harness: str,
    name: str = "perplexity",
    output_dir: str | os.PathLike[str] = ".",
    command: str = "/fixture/perplexity-mcp",
) -> Path:
    """Write a harness config to a private scratch directory.

    Follows the Capacium adapter contract pattern: read existing file
    (if present), setdefault the section key, inject/replace only the
    named entry, and write back. Unrelated top-level keys and sibling
    server entries are preserved. Only the entry with the same *name*
    is replaced.

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
    section_key = _section_key_for_harness(harness)
    entry = _build_entry_for_harness(harness, name, command)

    is_toml = harness == "codex"
    suffix = ".toml" if is_toml else ".json"
    path = output_dir / f"{harness}{suffix}"

    # Read existing config ({} if missing)
    if is_toml:
        config = _read_toml(path)
    else:
        config = _read_json(path)

    # setdefault the section key, then inject/replace the named entry
    servers = config.setdefault(section_key, {})
    servers[name] = entry

    # Atomic write
    if is_toml:
        _atomic_write_toml(path, config)
    else:
        _atomic_write_json(path, config)

    return path


def _build_entry_for_harness(
    harness: str, name: str, command: str
) -> Any:
    """Build the server entry dict for a given harness."""
    if harness == "opencode":
        return {
            "type": "local",
            "command": [command],
            "enabled": True,
            "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
        }
    return {
        "command": command,
        "args": [],
        "env": {"PERPLEXITY_COOKIES": CREDENTIAL_REF},
    }


# ── TOML writer (lightweight, no external dependency)


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
            f.write(f"{key} = {_json.dumps(str(value), ensure_ascii=False)}\n")
