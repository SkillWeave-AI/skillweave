"""A harmless generic tool the core has never heard of (SW-ADAPT-001).

It echoes its argument: no filesystem, no network, no executable loading. It
exists to prove a ``generic``-category adapter registers and invokes through the
same extension point as a search adapter, and is never used as an implicit
search fallback.
"""

from skillweave.adapters import (
    AdapterBackend,
    AdapterDescriptor,
    CATEGORY_GENERIC,
    OperationBinding,
    ToolOutcome,
)


class EchoToolBackend(AdapterBackend):
    async def invoke(self, operation, argument, context):
        return ToolOutcome(status="success", tool=operation, payload={"echo": argument.get("value")})


def generic_descriptor():
    return AdapterDescriptor(
        id="external-echo-tool",
        version="0.1.0",
        categories=(CATEGORY_GENERIC,),
        operations=(
            OperationBinding(
                name="tool.echo",
                input_contract="tool/echo-request/v1",
                result_contract="tool/echo-result/v1",
            ),
        ),
        transport="in-process",
    )
