"""CORE-ZR-023: real protocol proofs for both product server factories."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_DRIVER = r'''
import asyncio, importlib.metadata, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    mode = sys.argv[1]
    code = {
        "stdio": "from skillweave.mcp.server import main; main()",
        "control": "from mcp.server import fastmcp; from skillweave.mcp import control; control._build_server(fastmcp).run()",
        "library": "from mcp.server.fastmcp import FastMCP; FastMCP('SkillWeave').run()",
        "removed-hook": "from skillweave.mcp import control; control._set_package_version = lambda server: server; from skillweave.mcp.server import main; main()",
    }[mode]
    env = dict(os.environ)
    env["SKILLWEAVE_MCP_SUPERVISOR"] = "supervisor:package-version-test"
    params = StdioServerParameters(command=sys.executable, args=["-c", code], env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            init = await client.initialize()
            print(json.dumps({"initialize": init.model_dump(mode="json"),
                              "package_version": importlib.metadata.version("skillweave"),
                              "library_version": importlib.metadata.version("mcp")}), flush=True)

asyncio.run(main())
'''


def _initialize(mode, tmp_path):
    driver = tmp_path / "version_client.py"
    driver.write_text(_DRIVER)
    env = dict(os.environ)
    source = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH")]))
    proc = subprocess.run(
        [sys.executable, str(driver), mode], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=45,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("mode", ["stdio", "control"])
def test_real_initialize_reports_installed_package_version(mode, tmp_path):
    proof = _initialize(mode, tmp_path)
    info = proof["initialize"]["serverInfo"]
    assert info["name"] == "SkillWeave"
    assert info["version"] == proof["package_version"], proof


@pytest.mark.parametrize("mode", ["library", "removed-hook"])
def test_library_default_and_removed_version_hook_are_negative_controls(mode, tmp_path):
    proof = _initialize(mode, tmp_path)
    info = proof["initialize"]["serverInfo"]
    assert proof["library_version"] != proof["package_version"], proof
    assert info["version"] == proof["library_version"], proof
    with pytest.raises(AssertionError):
        assert info["version"] == proof["package_version"]
