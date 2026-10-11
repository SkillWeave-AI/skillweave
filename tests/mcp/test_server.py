import json
import pytest
from skillweave.mcp.control import mcp_tool_names
from skillweave.mcp.server import mcp, cancel_run, resume_run


def test_mcp_tools_list_only_commands():
    """Ensure that only authorized command tools are registered."""
    # Real FastMCP 1.x has _tool_manager.list_tools() (synchronous).
    # The degraded stub (no mcp package) stores decorated functions in mcp.tools.
    if hasattr(mcp, "_tool_manager"):
        tools = mcp._tool_manager.list_tools()
        registered = sorted(t.name for t in tools)
    elif hasattr(mcp, "tools"):
        # Degraded stub: each entry is the raw function object
        registered = sorted(f.__name__ for f in mcp.tools)
    else:
        registered = []
    assert registered == list(mcp_tool_names()), (
        f"registered tools {registered} do not match authorized set "
        f"{list(mcp_tool_names())}"
    )


def test_mcp_tools_rejects_raw_mutation():
    """Negative control: an extra raw state-mutation tool would be caught."""
    # Demonstrate the assertion has teeth: if a raw mutation tool appeared
    # in the registered set, it would NOT match the canonical set.
    if hasattr(mcp, "_tool_manager"):
        tools = mcp._tool_manager.list_tools()
        registered = sorted(t.name for t in tools)
    elif hasattr(mcp, "tools"):
        registered = sorted(f.__name__ for f in mcp.tools)
    else:
        registered = []
    assert "raw_mutate_db" not in registered
    # Prove the comparison would fail if one were added
    assert registered != sorted(registered + ["raw_mutate_db"])

def test_cancel_run_not_found():
    res = cancel_run("invalid-run", "test", db_path=":memory:")
    assert json.loads(res) == {"error": "Run not found"}

def test_resume_run_not_found():
    res = resume_run("invalid-run", db_path=":memory:")
    assert json.loads(res) == {"error": "Run not found"}
