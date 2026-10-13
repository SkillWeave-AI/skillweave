"""The installed ``skillweave-mcp`` console script (CORE-ZR-027 criterion 2/3).

The console script added to ``[project.scripts]`` maps the name ``skillweave-mcp``
to ``skillweave.mcp.server:main``. The wheel's generated shim for that entry
point is, verbatim::

    import sys
    from skillweave.mcp.server import main
    sys.exit(main())

These tests reproduce that shim in a subprocess so the *entry target the wheel
declares* is what runs -- not a re-implementation. Two runs are encoded:

* the **refusal** run -- no ``SKILLWEAVE_MCP_SUPERVISOR`` -- must exit ``3``
  (``skillweave.mcp.server.EXIT_REFUSED``) with one line on stderr naming the
  ``SKILLWEAVE_MCP_SUPERVISOR`` remedy and **no traceback**; and
* the **positive** run -- supervisor set -- must complete a real MCP stdio
  ``initialize`` handshake and ``tools/list`` against the same entry target.

The only skip is the named absent-``mcp`` reason: without the real ``mcp``
package the server cannot serve and neither run is meaningful.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = REPO_ROOT / "src"

SUPERVISOR_ENV = "SKILLWEAVE_MCP_SUPERVISOR"
EXIT_REFUSED = 3

# The wheel's console-script shim for ``skillweave-mcp``, reproduced literally:
# ``from skillweave.mcp.server import main; sys.exit(main())``.
_ENTRY = "import sys; from skillweave.mcp.server import main; sys.exit(main())"


def _has_real_mcp() -> bool:
    """Whether the real ``mcp`` server package is importable on this host."""
    try:
        import mcp.server.fastmcp  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure is "absent"
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _has_real_mcp(), reason="real 'mcp' package not importable on this host"
)


def _env(*, supervisor: str = "") -> dict:
    """A subprocess env with ``PYTHONPATH`` pointing only at the checkout src.

    The supervisor variable is set exactly when ``supervisor`` is non-empty, and
    otherwise *removed* so the refusal path is genuinely exercised.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC)
    if supervisor:
        env[SUPERVISOR_ENV] = supervisor
    else:
        env.pop(SUPERVISOR_ENV, None)
    return env


def _run_entry(*, supervisor: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _ENTRY],
        capture_output=True, text=True, env=_env(supervisor=supervisor),
        cwd=str(REPO_ROOT),
    )


def test_console_script_refuses_without_supervisor():
    """Without ``SKILLWEAVE_MCP_SUPERVISOR`` the entry refuses: exit ``3``, a
    single documented stderr line, and no traceback."""
    proc = _run_entry()

    assert proc.returncode == EXIT_REFUSED, (
        f"expected exit {EXIT_REFUSED}, got {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert "skillweave-mcp refused:" in lines[0], lines[0]
    assert SUPERVISOR_ENV in lines[0], lines[0]


def test_console_script_serves_stdio_with_supervisor():
    """With the supervisor set the same entry completes a real stdio
    ``initialize`` handshake and ``tools/list``."""
    driver = (
        "import asyncio, os, sys\n"
        "from mcp import ClientSession, StdioServerParameters\n"
        "from mcp.client.stdio import stdio_client\n"
        "async def main():\n"
        "    params = StdioServerParameters(\n"
        "        command=sys.executable, args=['-c', " + repr(_ENTRY) + "],\n"
        "        env={**os.environ, " + repr(SUPERVISOR_ENV) + ": 'supervisor:unit'},\n"
        "    )\n"
        "    async with stdio_client(params) as (read, write):\n"
        "        async with ClientSession(read, write) as session:\n"
        "            init = await session.initialize()\n"
        "            tools = await session.list_tools()\n"
        "            print('INIT', init.serverInfo.name)\n"
        "            print('TOOLS', ','.join(sorted(t.name for t in tools.tools)))\n"
        "asyncio.run(main())\n"
    )
    env = _env(supervisor="supervisor:unit")
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INIT SkillWeave" in proc.stdout, proc.stdout
    assert "TOOLS" in proc.stdout, proc.stdout
    tools = next(
        ln[len("TOOLS "):] for ln in proc.stdout.splitlines() if ln.startswith("TOOLS ")
    )
    assert "create_run" in tools, tools
