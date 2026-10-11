import json
import pytest
from skillweave.mcp.control import mcp_tool_names
from skillweave.mcp.server import mcp, cancel_run, resume_run


def _assert_registry_matches(registered):
    """Assert that *registered* (sorted list of tool names) equals the authorized set."""
    assert registered == list(mcp_tool_names()), (
        f"registered tools {registered} do not match authorized set "
        f"{list(mcp_tool_names())}"
    )


def _collect_registered():
    """Return sorted tool names from the server, regardless of FastMCP or degraded stub."""
    if hasattr(mcp, "_tool_manager"):
        tools = mcp._tool_manager.list_tools()
        return sorted(t.name for t in tools)
    if hasattr(mcp, "tools"):
        # Degraded stub: each entry is the raw function object
        return sorted(f.__name__ for f in mcp.tools)
    return []


def test_mcp_tools_list_only_commands():
    """Ensure that only authorized command tools are registered."""
    _assert_registry_matches(_collect_registered())


def test_mcp_tools_rejects_raw_mutation():
    """Negative control: an extra raw state-mutation tool would be caught."""
    # Build a contaminated registry that includes a synthetic raw-mutation tool.
    # _assert_registry_matches MUST raise AssertionError; if it were weakened
    # to a no-op the pytest.raises block would fail (no exception raised).
    contaminated = list(mcp_tool_names()) + ["raw_mutate_db"]
    with pytest.raises(AssertionError):
        _assert_registry_matches(contaminated)

def test_cancel_run_not_found():
    res = cancel_run("invalid-run", "test", db_path=":memory:")
    assert json.loads(res) == {"error": "Run not found"}

def test_resume_run_not_found():
    res = resume_run("invalid-run", db_path=":memory:")
    assert json.loads(res) == {"error": "Run not found"}
