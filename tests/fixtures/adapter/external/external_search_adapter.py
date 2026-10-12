"""An external search adapter the core has never heard of (SW-ADAPT-001).

It is a *descriptor + backend*, not a Core vendor entry. An installed test
registers it through the public extension registry and invokes it with no Core
vendor edit. It performs no network call: its result is synthetic and clearly
labelled, so a fixture can prove wiring and origin without calling a provider.
"""

from skillweave.adapters import (
    AdapterBackend,
    AdapterDescriptor,
    CATEGORY_SEARCH,
    OperationBinding,
    SEARCH_OPERATION,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
)


class ExternalSyntheticSearchBackend(AdapterBackend):
    def __init__(self, provider="synthetic-external"):
        self.provider = provider

    async def invoke(self, operation, argument, context):
        query = str(argument.get("query", ""))
        if not query.strip():
            return SearchOutcome(
                query_sha256="0" * 64, requested_provider=self.provider,
                status="blocked",
                attempts=(SearchAttempt(self.provider, "http", "blocked", "empty_query"),),
                error_code="empty_query",
            )
        return SearchOutcome(
            query_sha256="1" * 64,
            requested_provider=self.provider,
            status="success",
            results=(
                SearchResultRecord(
                    title=f"external:{query}",
                    url="https://external.example.org/result",
                    snippet="synthetic result from an external adapter",
                    source="web",
                    provider=self.provider,
                ),
            ),
            attempts=(SearchAttempt(self.provider, "http", "success"),),
        )


def external_descriptor():
    return AdapterDescriptor(
        id="external-synthetic-search",
        version="0.4.0",
        title="External synthetic search",
        categories=(CATEGORY_SEARCH,),
        operations=(
            OperationBinding(
                name=SEARCH_OPERATION,
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
                required_capabilities=("search.query",),
            ),
        ),
        transport="http",
        capabilities=("search.query", "network-egress"),
    )
