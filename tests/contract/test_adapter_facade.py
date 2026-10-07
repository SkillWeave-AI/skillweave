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


# ── finding 1: the facade refuses a non-conforming outcome ─────────────────
#
# A backend is *not trusted*. An operation's ``result_contract`` names the typed
# outcome class its result must be an instance of (the binding is owned by the
# registry/descriptor, never a facade branch on a literal). A backend that
# returns a success object of the *wrong* type -- a generic tool's outcome for a
# search contract, or a bare dict/None -- must be turned into a typed,
# non-success failure, never a success receipt that claims a contract it never
# satisfied.


class _FixedBackend(AdapterBackend):
    """Returns exactly what it is handed, whatever type that is."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def invoke(self, operation, argument, context):
        self.calls += 1
        return self.result


class _RaisingBackend(AdapterBackend):
    """Raises from ``invoke`` (an explicit transport outage)."""

    async def invoke(self, operation, argument, context):
        raise RuntimeError("provider is down")


def _search_facade(backend):
    registry = AdapterRegistry()
    registry.register(synthetic_search_descriptor(), backend)
    return AdapterFacade(
        registry, _policy(["synthetic-search"], network=True, endpoints=["external.example.org"])
    )


def test_search_contract_refuses_a_tool_outcome_success():
    """The original counterexample: a tool success must not satisfy search."""
    backend = _FixedBackend(
        ToolOutcome(status="success", tool="acme-echo-tool", payload="no sources at all")
    )
    outcome, receipt = asyncio.run(
        _search_facade(backend).invoke(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
            adapter_id="synthetic-search", argument={"query": "hello"},
            endpoint="https://external.example.org/",
        )
    )
    # The tool outcome is not a conforming search outcome -> typed failure.
    assert isinstance(outcome, SearchOutcome), type(outcome).__name__
    assert not isinstance(outcome, ToolOutcome)
    assert outcome.status == "error"
    assert outcome.error_code == "malformed_outcome"
    assert outcome.results == ()
    assert outcome.succeeded is False
    # The receipt reports the failure, never a success claiming search-outcome/v1.
    assert receipt.domain_contract == "search-outcome/v1"
    assert receipt.status != "success"
    assert receipt.status == "error"
    assert backend.calls == 1


@pytest.mark.parametrize("bad", [None, {"status": "success", "url": "https://x"}, "a string", 42])
def test_search_contract_refuses_dict_none_and_scalar_payloads(bad):
    backend = _FixedBackend(bad)
    outcome, receipt = asyncio.run(
        _search_facade(backend).invoke(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
            adapter_id="synthetic-search", argument={"query": "hello"},
            endpoint="https://external.example.org/",
        )
    )
    assert isinstance(outcome, SearchOutcome)
    assert outcome.status == "error"
    assert outcome.results == ()
    assert receipt.status == "error"


def test_a_conforming_search_failure_is_passed_through_as_a_failure():
    """A real typed outage is conforming: it stays a non-success, not a success."""
    backend = _FixedBackend(
        SearchOutcome(
            query_sha256="e" * 64, requested_provider="synthetic",
            status="unavailable", attempts=(SearchAttempt("synthetic", "http", "unavailable"),),
            error_code="outage",
        )
    )
    outcome, receipt = asyncio.run(
        _search_facade(backend).invoke(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
            adapter_id="synthetic-search", argument={"query": "hello"},
            endpoint="https://external.example.org/",
        )
    )
    assert isinstance(outcome, SearchOutcome)
    assert outcome.status == "unavailable"
    assert receipt.status == "unavailable"


def test_a_backend_that_raises_on_outage_propagates_and_is_never_a_success():
    """An explicit outage raise is surfaced, never quietly turned into success."""
    with pytest.raises(RuntimeError):
        asyncio.run(
            _search_facade(_RaisingBackend()).invoke(
                category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
                adapter_id="synthetic-search", argument={"query": "hello"},
                endpoint="https://external.example.org/",
            )
        )


def test_generic_tool_with_a_custom_contract_still_accepts_a_typed_outcome():
    """A generic tool's own domain contract admits any *typed* outcome."""
    outcome, receipt = asyncio.run(
        AdapterFacade(
            _registry_with_generic_tool(), _policy(["acme-echo-tool"])
        ).invoke(
            category=CATEGORY_GENERIC, operation="tool.echo",
            adapter_id="acme-echo-tool", argument={"value": "hi"},
        )
    )
    assert isinstance(outcome, ToolOutcome)
    assert outcome.status == "success"
    assert receipt.domain_contract == "tool/echo-result/v1"


def test_generic_tool_with_a_custom_contract_refuses_a_bare_dict():
    registry = AdapterRegistry()
    registry.register(generic_tool_descriptor(), _FixedBackend({"echo": "hi"}))
    outcome, receipt = asyncio.run(
        AdapterFacade(registry, _policy(["acme-echo-tool"])).invoke(
            category=CATEGORY_GENERIC, operation="tool.echo",
            adapter_id="acme-echo-tool", argument={"value": "hi"},
        )
    )
    assert isinstance(outcome, ToolOutcome)
    assert outcome.status == "error"
    assert receipt.status == "error"


def _registry_with_generic_tool():
    registry = AdapterRegistry()
    registry.register(generic_tool_descriptor(), CountingToolBackend())
    return registry


# ── finding 2: a receipt can never self-approve ────────────────────────────


def _valid_receipt():
    return OperationReceipt(
        invocation_id="inv-1",
        adapter_id="synthetic-search",
        adapter_version="2.3.0",
        descriptor_digest="a" * 64,
        operation=SEARCH_OPERATION,
        domain_contract="search-outcome/v1",
        transport="http",
        status="success",
        evidence_digest="b" * 64,
    )


def test_receipt_self_approving_true_is_unrepresentable():
    with pytest.raises(OutcomeError) as exc:
        replace(_valid_receipt(), self_approving=True)
    assert exc.value.asset == "receipt.self_approving"


def test_receipt_loader_refuses_self_approving_true():
    payload = _valid_receipt().to_dict()
    payload["self_approving"] = True
    with pytest.raises(OutcomeError) as exc:
        OperationReceipt.from_dict(payload)
    assert exc.value.asset == "receipt.self_approving"


def test_a_serialized_receipt_never_carries_true():
    constructed = _valid_receipt()
    reloaded = OperationReceipt.from_dict(constructed.to_dict())
    assert constructed.to_dict()["self_approving"] is False
    assert reloaded.to_dict()["self_approving"] is False
    assert reloaded == constructed


# ── finding 3: malicious descriptor fields are refused ─────────────────────


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "/etc/passwd", "a/b", "a\\b", "UPPER", "-leading", "", "x" * 129],
)
def test_malicious_or_malformed_adapter_ids_are_refused(bad_id):
    with pytest.raises(AdapterDescriptorError) as exc:
        AdapterDescriptor(
            id=bad_id, version="1", categories=(CATEGORY_GENERIC,),
            operations=(OperationBinding("op", "in/v1", "out/v1"),),
            transport="in-process",
        )
    assert exc.value.asset == "id"


@pytest.mark.parametrize(
    "bad_transport",
    ['__import__("os").system("true")', "http; rm -rf /", "a b", "", "x" * 129],
)
def test_code_bearing_or_malformed_transports_are_refused(bad_transport):
    with pytest.raises(AdapterDescriptorError) as exc:
        AdapterDescriptor(
            id="ok-id", version="1", categories=(CATEGORY_GENERIC,),
            operations=(OperationBinding("op", "in/v1", "out/v1"),),
            transport=bad_transport,
        )
    assert exc.value.asset == "transport"


@pytest.mark.parametrize(
    "bad_ref",
    ["sk-live-abc123", "sk-proj-DEADBEEF", "secret value", "", "x" * 129],
)
def test_secret_looking_credential_values_are_refused(bad_ref):
    with pytest.raises(AdapterDescriptorError) as exc:
        AdapterDescriptor(
            id="ok-id", version="1", categories=(CATEGORY_GENERIC,),
            operations=(OperationBinding("op", "in/v1", "out/v1"),),
            transport="in-process", credential_refs=(bad_ref,),
        )
    assert exc.value.asset == "credential_refs"


def test_register_dict_refuses_a_path_escaping_id():
    registry = AdapterRegistry()
    with pytest.raises(AdapterDescriptorError) as exc:
        registry.register_dict(
            {
                "schema_version": "1.0.0",
                "id": "../../etc/passwd",
                "version": "1",
                "categories": ["generic"],
                "operations": [{"name": "op", "input_contract": "in/v1", "result_contract": "out/v1"}],
                "transport": "in-process",
            },
            CountingToolBackend(),
        )
    assert exc.value.asset == "id"
    assert registry.ids() == ()


def test_register_dict_refuses_a_code_bearing_transport():
    registry = AdapterRegistry()
    with pytest.raises(AdapterDescriptorError) as exc:
        registry.register_dict(
            {
                "schema_version": "1.0.0",
                "id": "ok-id",
                "version": "1",
                "categories": ["generic"],
                "operations": [{"name": "op", "input_contract": "in/v1", "result_contract": "out/v1"}],
                "transport": '__import__("os").system("true")',
            },
            CountingToolBackend(),
        )
    assert exc.value.asset == "transport"
    assert registry.ids() == ()


def test_register_dict_refuses_a_secret_looking_credential_ref():
    registry = AdapterRegistry()
    with pytest.raises(AdapterDescriptorError) as exc:
        registry.register_dict(
            {
                "schema_version": "1.0.0",
                "id": "ok-id",
                "version": "1",
                "categories": ["generic"],
                "operations": [{"name": "op", "input_contract": "in/v1", "result_contract": "out/v1"}],
                "transport": "in-process",
                "credential_refs": ["sk-live-abc123"],
            },
            CountingToolBackend(),
        )
    assert exc.value.asset == "credential_refs"
    assert registry.ids() == ()


def test_legitimate_descriptor_fields_are_accepted():
    desc = AdapterDescriptor(
        id="perplexity-search-mcp",
        version="1.0.0",
        categories=(CATEGORY_SEARCH,),
        operations=(OperationBinding(SEARCH_OPERATION, "search-query/v1", "search-outcome/v1"),),
        transport="mcp-stdio",
        credential_refs=("search-api-key", "DISCOVERY_SEARCH_KEY"),
    )
    assert desc.id == "perplexity-search-mcp"
    assert desc.transport == "mcp-stdio"
    assert desc.credential_refs == ("search-api-key", "DISCOVERY_SEARCH_KEY")


def test_builtin_search_registry_survives_the_new_validation():
    """Every shipped descriptor must still load, or the validation is too strict."""
    registry = build_search_registry()
    assert len(registry.ids()) == 7
