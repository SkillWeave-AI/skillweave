"""External adapters that drive *Council's* MCP backend (SW-COUNCIL-003, criterion 5).

These are descriptors + backends the core has never heard of. A Discovery binding
names one through ``adapter_factories`` (``module:callable``), so Discovery
research reaches it through the *public* extension registry with no core edit.

Unlike the SW-ADAPT-001 fixtures, these do not return a synthetic outcome: they
call Council's own :class:`skillweave.council.perplexity.PerplexityMcpBackend`
against the network-free fake MCP server and return whatever *typed* outcome it
produces. That is what makes criterion 5 real -- the installed consumer is proven
to consume the Council repair, not a stub.

* :func:`council_mcp_success_factory` -- drives the ``normal`` fake server: a real
  cited http(s) source;
* :func:`council_mcp_failure_factory` -- drives the ``text-only`` fake server: a
  synthesis with no cited URL, so a typed ``no_results`` non-success with zero
  sources (an explicit failure state, never a fabricated source).

No live provider is ever called.
"""

import sys
from pathlib import Path

from skillweave.adapters import (
    CATEGORY_SEARCH,
    SEARCH_OPERATION,
    AdapterBackend,
    AdapterDescriptor,
    OperationBinding,
)
from skillweave.council.perplexity import PerplexityMcpBackend

FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"

_SUCCESS_ID = "external-council-mcp-search"
_FAILURE_ID = "external-council-mcp-fail"


def _descriptor(adapter_id: str) -> AdapterDescriptor:
    return AdapterDescriptor(
        id=adapter_id,
        version="1.0.0",
        title=f"External {adapter_id}",
        categories=(CATEGORY_SEARCH,),
        operations=(
            OperationBinding(
                name=SEARCH_OPERATION,
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
                required_capabilities=("search.query",),
            ),
        ),
        transport="in-process",
        capabilities=("search.query",),
    )


class _CouncilMcpBackend(AdapterBackend):
    """Drives Council's MCP backend against the fake server for one mode."""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    async def invoke(self, operation, argument, context):
        command = [sys.executable, str(FAKE_SERVER), self._mode]
        backend = PerplexityMcpBackend(timeout=10.0)
        return await backend.invoke(
            operation,
            {"query": argument.get("query", ""), "command": command},
            context,
        )


def council_mcp_success_factory():
    return _descriptor(_SUCCESS_ID), _CouncilMcpBackend("normal")


def council_mcp_failure_factory():
    return _descriptor(_FAILURE_ID), _CouncilMcpBackend("text-only")
