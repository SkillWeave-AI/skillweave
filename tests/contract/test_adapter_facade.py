"""SW-ADAPT-001: the common adapter facade, registry, policy and outcomes.

These tests exercise the *actual consumer wiring*: they register adapters the
core vendor list has never heard of (an external synthetic search adapter and a
harmless generic tool), resolve them through the one extension point, admit them
under an effective policy, invoke them and inspect the typed outcome and the
common receipt. They also prove the negative paths: an unapproved adapter, a
missing capability, a missing credential and a denied egress are four distinct
refusals -- and none of them performs a call.
"""

import asyncio
from dataclasses import dataclass, replace

import pytest

from skillweave.adapters import (
    AdapterBackend,
    AdapterDescriptor,
    AdapterError,
    AdapterFacade,
    AdapterRegistry,
    CATEGORY_GENERIC,
    CATEGORY_SEARCH,
    EffectivePolicy,
    EgressPolicy,
    OperationBinding,
    OperationReceipt,
    OutcomeError,
    PolicyError,
    SEARCH_OPERATION,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
    ToolOutcome,
    UnknownAdapterError,
    UnsupportedVersionError,
    admit_invocation,
    build_search_registry,
)
from skillweave.adapters import search_backends
from skillweave.adapters.descriptor import AdapterDescriptorError


# ── test doubles: external adapters the core has never heard of ────────────


class CountingSearchBackend(AdapterBackend):
    """An external search adapter returning a typed success and counting calls."""

    def __init__(self, records=None, status="success"):
        self.records = list(records or [
            SearchResultRecord(
                title="External result",
                url="https://external.example.org/a",
                snippet="synthetic",
                source="web",
                provider="synthetic",
            )
        ])
        self.status = status
        self.calls = 0

    async def invoke(self, operation, argument, context):
        self.calls += 1
        if self.status == "success":
            return SearchOutcome(
                query_sha256="a" * 64,
                requested_provider="synthetic",
                status="success",
                results=tuple(self.records),
                attempts=(SearchAttempt("synthetic", "http", "success"),),
            )
        return SearchOutcome(
            query_sha256="a" * 64,
            requested_provider="synthetic",
            status=self.status,
            attempts=(SearchAttempt("synthetic", "http", self.status, "outage"),),
            error_code="outage",
        )


class CountingToolBackend(AdapterBackend):
    """A harmless generic tool: returns its own opaque payload."""

    def __init__(self):
        self.calls = 0

    async def invoke(self, operation, argument, context):
        self.calls += 1
        return ToolOutcome(status="success", tool=operation, payload={"echo": argument.get("value")})


def synthetic_search_descriptor(adapter_id="synthetic-search"):
    return AdapterDescriptor(
        id=adapter_id,
        version="2.3.0",
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


def generic_tool_descriptor(adapter_id="acme-echo-tool"):
    return AdapterDescriptor(
        id=adapter_id,
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


def _policy(ids, *, network=False, endpoints=(), require_credential=True):
    return EffectivePolicy(
        allowed_adapter_ids=tuple(ids),
        egress=EgressPolicy(allow_network=network, endpoint_allowlist=tuple(endpoints)),
        require_credential_availability=require_credential,
    )


# ── registration & resolution ──────────────────────────────────────────────


def test_external_adapters_register_without_a_vendor_edit():
    registry = AdapterRegistry()
    registry.register(synthetic_search_descriptor(), CountingSearchBackend(), source="external")
    registry.register(generic_tool_descriptor(), CountingToolBackend(), source="external")
    assert set(registry.ids()) == {"synthetic-search", "acme-echo-tool"}


def test_duplicate_id_is_refused():
    registry = AdapterRegistry()
    registry.register(synthetic_search_descriptor(), CountingSearchBackend())
    with pytest.raises(AdapterError) as exc:
        registry.register(synthetic_search_descriptor(), CountingSearchBackend())
    assert exc.value.asset == "id"


def test_unregistered_id_is_refused_by_name():
    registry = AdapterRegistry()
    registry.register(synthetic_search_descriptor(), CountingSearchBackend())
    with pytest.raises(UnknownAdapterError) as exc:
        registry.get("not-registered")
    assert "not-registered" in str(exc.value)


def test_unsupported_descriptor_version_is_refused():
    with pytest.raises(AdapterDescriptorError) as exc:
        replace(synthetic_search_descriptor(), schema_version="9.0.0")
    assert exc.value.asset == "schema_version"


def test_unknown_category_fails_closed():
    with pytest.raises(AdapterDescriptorError) as exc:
        AdapterDescriptor(
            id="x", version="1", categories=("vector-db",),
            operations=(OperationBinding("op", "in/v1", "out/v1"),),
            transport="http",
        )
    assert exc.value.asset == "categories"


# ── the facade: resolve → admit → invoke → typed receipt ───────────────────


def test_search_success_carries_a_real_source_and_a_receipt():
    registry = AdapterRegistry()
    backend = CountingSearchBackend()
    registry.register(synthetic_search_descriptor(), backend)
    facade = AdapterFacade(
        registry, _policy(["synthetic-search"], network=True, endpoints=["external.example.org"])
    )
    outcome, receipt = asyncio.run(
        facade.invoke(
            category=CATEGORY_SEARCH,
            operation=SEARCH_OPERATION,
            adapter_id="synthetic-search",
            argument={"query": "hello"},
            endpoint="https://external.example.org/",
        )
    )
    assert isinstance(outcome, SearchOutcome) and outcome.succeeded
    assert all(r.url.startswith("http") for r in outcome.results)
    assert isinstance(receipt, OperationReceipt)
    assert receipt.adapter_id == "synthetic-search"
    assert receipt.adapter_version == "2.3.0"
    assert receipt.domain_contract == "search-outcome/v1"
    assert len(receipt.descriptor_digest) == 64
    assert receipt.self_approving is False
    assert receipt.is_authority is False
    assert backend.calls == 1


def test_search_outage_is_reported_not_dressed_as_a_source():
    registry = AdapterRegistry()
    registry.register(synthetic_search_descriptor(), CountingSearchBackend(status="unavailable"))
    facade = AdapterFacade(
        registry, _policy(["synthetic-search"], network=True, endpoints=["external.example.org"])
    )
    outcome, receipt = asyncio.run(
        facade.invoke(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
            adapter_id="synthetic-search", argument={"query": "hello"},
            endpoint="https://external.example.org/",
        )
    )
    assert outcome.status == "unavailable"
    assert outcome.results == ()
    assert receipt.status == "unavailable"


def test_generic_tool_is_never_an_implicit_search_fallback():
    """Without naming an id, a search request is not served by the generic tool."""
    registry = AdapterRegistry()
    registry.register(generic_tool_descriptor(), CountingToolBackend())
    facade = AdapterFacade(registry, _policy(["acme-echo-tool"]))
    with pytest.raises(UnknownAdapterError):
        facade.resolve(category=CATEGORY_SEARCH, operation=SEARCH_OPERATION)


def test_unregistered_named_adapter_fails_closed():
    registry = AdapterRegistry()
    facade = AdapterFacade(registry, _policy(["synthetic-search"]))
    with pytest.raises(UnknownAdapterError):
        facade.resolve(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION, adapter_id="synthetic-search"
        )


def test_ambiguous_resolution_requires_an_explicit_id():
    registry = AdapterRegistry()
    registry.register(synthetic_search_descriptor("synthetic-a"), CountingSearchBackend())
    registry.register(synthetic_search_descriptor("synthetic-b"), CountingSearchBackend())
    facade = AdapterFacade(registry, _policy(["synthetic-a", "synthetic-b"]))
    with pytest.raises(AdapterError) as exc:
        facade.resolve(category=CATEGORY_SEARCH, operation=SEARCH_OPERATION)
    assert "ambiguous" in str(exc.value)


# ── policy negatives: four distinct refusals, no call ──────────────────────


def test_unapproved_adapter_is_refused_before_any_call():
    registry = AdapterRegistry()
    backend = CountingSearchBackend()
    registry.register(synthetic_search_descriptor(), backend)
    facade = AdapterFacade(registry, _policy([], network=True, endpoints=["external.example.org"]))
    with pytest.raises(AdapterError) as exc:
        facade.resolve(category=CATEGORY_SEARCH, operation=SEARCH_OPERATION, adapter_id="synthetic-search")
    assert exc.value.asset == "allowlist"
    assert backend.calls == 0


def test_missing_capability_is_a_distinct_refusal():
    registry = AdapterRegistry()
    backend = CountingSearchBackend()
    registry.register(synthetic_search_descriptor(), backend)
    facade = AdapterFacade(
        registry, _policy(["synthetic-search"], network=True, endpoints=["external.example.org"])
    )
    with pytest.raises(AdapterError) as exc:
        facade.resolve(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION, adapter_id="synthetic-search",
            required_capabilities=("telepathy",),
        )
    assert exc.value.asset == "capability:telepathy"
    assert backend.calls == 0


def test_missing_credential_is_refused_and_never_simulated():
    desc = replace(synthetic_search_descriptor(), credential_refs=("search-api-key",))
    decision = admit_invocation(
        _policy(["synthetic-search"], network=True, endpoints=["external.example.org"]),
        desc,
        category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
        credential_availability={"search-api-key": "missing"},
        endpoint="https://external.example.org/",
    )
    assert decision.admitted is False
    assert decision.asset == "credential:search-api-key"


def test_denied_egress_is_a_distinct_refusal():
    registry = AdapterRegistry()
    backend = CountingSearchBackend()
    registry.register(synthetic_search_descriptor(), backend)
    facade = AdapterFacade(registry, _policy(["synthetic-search"], network=False))
    with pytest.raises(AdapterError) as exc:
        facade.resolve(category=CATEGORY_SEARCH, operation=SEARCH_OPERATION, adapter_id="synthetic-search")
    assert exc.value.asset == "egress"
    assert backend.calls == 0


def test_egress_allowlist_is_fail_closed_for_a_foreign_host():
    decision = admit_invocation(
        _policy(["synthetic-search"], network=True, endpoints=["allowed.example.org"]),
        synthetic_search_descriptor(),
        category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
        endpoint="https://evil.example.net/",
    )
    assert decision.admitted is False
    assert decision.asset == "egress"


def test_empty_allowlist_approves_nothing():
    with pytest.raises(PolicyError):
        _policy(["a", "a"])


# ── six providers through the SAME extension point ─────────────────────────


def test_all_six_providers_register_through_one_registry():
    registry = build_search_registry()
    providers = {search_backends.SEARCH_PROVIDER_SPECS[k].provider for k in search_backends.SEARCH_PROVIDER_SPECS}
    assert providers == {"duckduckgo", "serper", "serpapi", "tavily", "brave", "perplexity"}
    resolved = registry.resolved_for(CATEGORY_SEARCH, SEARCH_OPERATION, ("search.query",))
    assert len(resolved) == 7  # six providers; Perplexity is two transports
    assert all(e.descriptor.serves(CATEGORY_SEARCH, SEARCH_OPERATION) for e in resolved)
    assert all(e.source == "builtin_search" for e in resolved)


def test_perplexity_has_two_alternative_transport_bindings():
    registry = build_search_registry()
    mcp = registry.get("perplexity-search-mcp")
    http = registry.get("perplexity-search-http")
    assert mcp.descriptor.transport == "mcp-stdio"
    assert http.descriptor.transport == "http"
    # same provider, same category+operation: transport is an independent axis
    assert mcp.descriptor.serves(CATEGORY_SEARCH, SEARCH_OPERATION)
    assert http.descriptor.serves(CATEGORY_SEARCH, SEARCH_OPERATION)


# ── typed outcome invariants (fail closed) ─────────────────────────────────


def test_success_without_sources_is_rejected():
    with pytest.raises(OutcomeError) as exc:
        SearchOutcome(query_sha256="b" * 64, requested_provider="x", status="success")
    assert exc.value.asset == "results"


def test_non_success_with_results_is_rejected():
    with pytest.raises(OutcomeError):
        SearchOutcome(
            query_sha256="b" * 64, requested_provider="x", status="blocked",
            results=(SearchResultRecord("t", "https://e.com", "s", "web", "x"),),
        )


def test_unsafe_scheme_is_never_promoted_to_a_source():
    with pytest.raises(OutcomeError) as exc:
        SearchResultRecord(title="t", url="javascript:alert(1)", snippet="s", source="web", provider="x")
    assert exc.value.asset == "results.url"


def test_malformed_payload_fails_closed():
    with pytest.raises(OutcomeError):
        SearchOutcome.from_dict({"status": "success", "results": [{"url": "ftp://x"}]})


def test_fallback_is_visibly_attributed():
    outcome = SearchOutcome.from_dict({
        "schema_version": "1.0.0",
        "query_sha256": "c" * 64,
        "requested_provider": "perplexity",
        "status": "success",
        "results": [{"title": "t", "url": "https://e.com", "snippet": "s", "source": "web", "provider": "perplexity"}],
        "attempts": [
            {"provider": "perplexity", "transport": "mcp-stdio", "status": "unavailable", "error_code": "mcp_error"},
            {"provider": "perplexity", "transport": "http", "status": "success", "error_code": None},
        ],
    })
    assert [a.transport for a in outcome.attempts] == ["mcp-stdio", "http"]
    assert outcome.attempts[0].status == "unavailable"


def test_unknown_major_version_is_rejected():
    with pytest.raises(OutcomeError):
        SearchOutcome.from_dict({
            "schema_version": "2.0.0", "query_sha256": "d" * 64,
            "requested_provider": "x", "status": "error", "results": [], "attempts": [],
        })
