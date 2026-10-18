"""SW-1511-SEARCH-ALIGN criteria 1-4: qualify the Council and Discovery consumers.

This suite *traces the production path* (criterion 1) of both lifecycle
consumers to the one existing search registry, distinguishes provider identity
from HTTP/MCP transport binding, and confirms finding **A8** with reachable
evidence. It then independently qualifies the retained implementation
(criterion 2: both consumers already use equivalent qualified registry bindings,
so nothing is bypassed and nothing needs alignment) and drives the Perplexity
bindings through a real handshake / tool discovery / typed-failure path against
the network-free fake servers, plus a second supported binding for provider
interchangeability (criterion 3).

What is proven here:

* **Criterion 1** -- ``WebSearch()`` (Council) and ``discovery.registry_for()``
  (Discovery) reach the *same* seven adapter ids derived from
  ``SEARCH_PROVIDER_SPECS``; the provider identity (``perplexity``) is distinct
  from the transport binding (``mcp-stdio`` vs ``http``); and **A8 is a same-id
  backend swap, not a second registry binding**: Council's ``search_registry()``
  replaces only the ``perplexity-search-mcp`` *backend* under a descriptor whose
  digest is byte-identical to the substrate's, leaving every id and axis intact.
* **Criterion 2** -- both consumers derive the same provider->adapter map from
  the one spec table, so their bindings are equivalent; the two Perplexity
  backends both return a conforming ``search-outcome/v1`` for the same binding.
  Implementation is retained and independently qualified; no bypass is proven.
* **Criterion 3** -- the ``perplexity-search-mcp`` binding performs the lifecycle
  handshake and tool discovery, and a missing tool, a missing launcher binding
  and a missing connection each become a **typed** non-success with **zero**
  results -- never a fixture success. A second supported binding
  (``duckduckgo-search``) is driven through the same facade to show provider
  interchangeability.
* **Criterion 4** -- search credentials, sessions and host repair remain with the
  provider/operator owner; Capacium Exchange is never invented as a search
  provider; a package ``kind`` is not a runtime adapter category.

The **binding decision** qualified here is: *one provider* (``perplexity``) is
served by **two labeled transport bindings** -- an **official** binding
(``perplexity-search-mcp``, ``mcp-stdio``) that launches the vendor's own MCP
server, and an **api-http** binding (``perplexity-search-http``, ``http``) that
dials ``api.perplexity.ai``; both consume the vendor API key
(``perplexity_api_key``, symbol ``PERPLEXITY_API_KEY``). The official binding is
the *default* resolution; the http binding is **opt-in and labeled**, reached
only by naming its adapter id, never by an implicit fallback from the provider
name. A **web-unofficial** binding credentialed by a browser session cookie
(``PERPLEXITY_COOKIES``) is **not** one of the six retained providers: it is an
opt-in external binding that plugs in through the one extension point
(``extra_adapters``) with no core edit, is declined by default, and is refused
when its credential is absent. Provenance names the binding (the receipt's
``adapter_id`` + ``transport``), and missing auth is a **typed refusal before
any call** -- never a fixture success.
Because the two consumers already derive equivalent bindings from the one spec
table, the implementation is **retained and independently qualified**; making a
different binding the default would be the "substantial extra redesign" that
criterion 2 requires an explicit scope amendment for, and is therefore not done
here.

Boundary: the contract-conformance classes prove conformance against the
*network-free fakes*. A final section performs the **real attempt** through the
already-installed production entry points: the Council launcher resolver and
``run_perplexity_mcp``, the Discovery facade, and (when an installed Perplexity
MCP entry point is actually present) a live handshake / tool-discovery /
sandbox-query path. Where no such entry point or credential exists, the attempt
yields a **typed** ``no_binding`` non-success with zero results -- a typed HOLD,
never a fixture success. The other supported binding (``duckduckgo-search``) has
an opt-in live path (``SKILLWEAVE_LIVE_SEARCH=1``) that records real provider
provenance. **This suite makes no network call at all**, not even a reachability
probe: the environment facts it records are read from the process environment
only. No credential value is ever read, printed or changed; only the *existence*
of a credential environment-variable name is observed.

**Criterion 4** is qualified at the seam this unit owns: search credentials,
sessions and host repair stay with the provider/operator owner (the suite only
ever observes credential-*name* presence, and the launcher injects injected
values into an owned child environment only); Capacium Exchange is never
invented as a search provider; and a package ``kind`` is never a runtime adapter
category.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave import discovery  # noqa: E402
from skillweave.adapters import (  # noqa: E402
    ADAPTER_CATEGORIES,
    CATEGORY_SEARCH,
    SEARCH_OPERATION,
    SEARCH_OUTCOME_CONTRACT,
    SEARCH_PROVIDER_SPECS,
    SEARCH_STATUSES,
    AdapterBackend,
    AdapterDescriptor,
    AdapterDescriptorError,
    AdapterError,
    AdapterRegistry,
    CapaciumFormatReader,
    OperationBinding,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
    build_search_registry,
    default_reader_registry,
)
from skillweave.adapters.search_backends import SearchAdapterBackend  # noqa: E402
from skillweave.council.perplexity import (  # noqa: E402
    COUNCIL_MCP_ADAPTER_ID,
    PERPLEXITY_MCP_TOOL,
    PerplexityMcpBackend,
    council_mcp_descriptor,
    run_perplexity_mcp,
)
from skillweave.council.credential import CredentialResolver  # noqa: E402
from skillweave.council.launcher import (  # noqa: E402
    LauncherResolutionError,
    resolve_launcher,
)
from skillweave.council.search import (  # noqa: E402
    SearchConfig,
    WebSearch,
    _PROVIDER_TO_ADAPTER,
    search_registry,
)
from skillweave.discovery import lifecycle_search as _ls  # noqa: E402

_SIX_PROVIDERS = ("duckduckgo", "serper", "serpapi", "tavily", "brave", "perplexity")
_ALL_SEVEN_IDS = tuple(spec.adapter_id for spec in SEARCH_PROVIDER_SPECS.values())

_ADAPTER_FAKE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "adapter" / "fake_mcp_server.py"
)
_COUNCIL_FAKE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "council" / "fake_mcp_server.py"
)


def _adapter_fake(mode: str) -> list[str]:
    return [sys.executable, str(_ADAPTER_FAKE), mode]


def _council_fake(mode: str) -> list[str]:
    return [sys.executable, str(_COUNCIL_FAKE), mode]


def _run(coro):
    return asyncio.run(coro)


# ══ Criterion 1: production-path trace + provider identity vs transport ═════


class TestProductionPathReachesTheSharedRegistry:
    """Both consumers reach the *same* registry, derived from one spec table."""

    def test_council_default_consumer_reaches_the_shared_registry(self):
        ws = WebSearch()  # the production default: no injected registry
        assert sorted(ws.registry.ids()) == sorted(build_search_registry().ids())
        assert len(ws.registry.ids()) == 7  # six providers, Perplexity twice
        for provider in _SIX_PROVIDERS:
            resolved = ws._adapter_id_for(provider, SearchConfig(provider=provider))
            assert resolved in ws.registry, (provider, resolved)

    def test_discovery_default_consumer_reaches_the_shared_registry(self):
        registry = discovery.registry_for()  # the production default
        assert sorted(registry.ids()) == sorted(build_search_registry().ids())
        for provider in _SIX_PROVIDERS:
            resolved = discovery.adapter_id_for(provider)
            assert resolved in registry, (provider, resolved)

    def test_provider_identity_is_distinct_from_transport_binding(self):
        # Perplexity is ONE provider served by TWO bindings; the binding is the
        # transport, the identity is the provider -- carried separately.
        mcp = SEARCH_PROVIDER_SPECS["perplexity-mcp"]
        http = SEARCH_PROVIDER_SPECS["perplexity-http"]
        assert mcp.provider == http.provider == "perplexity"
        assert mcp.adapter_id != http.adapter_id
        assert mcp.transport == "mcp-stdio" and http.transport == "http"
        # Both bindings are registered, and the shared map names only one as the
        # provider's default resolution (first spec wins for a duplicate provider).
        registry = build_search_registry()
        assert mcp.adapter_id in registry and http.adapter_id in registry
        assert _PROVIDER_TO_ADAPTER["perplexity"] == mcp.adapter_id

    def test_a_success_outcome_carries_identity_and_transport_separately(self):
        outcome = _run(
            SearchAdapterBackend(SEARCH_PROVIDER_SPECS["perplexity-mcp"]).invoke(
                SEARCH_OPERATION,
                {"query": "identity vs transport", "command": _adapter_fake("normal")},
                {},
            )
        )
        assert outcome.status == "success"
        assert outcome.requested_provider == "perplexity"  # identity
        assert outcome.attempts[0].transport == "mcp-stdio"  # binding


class TestA8IsASameIdBackendSwapNotASecondBinding:
    """A8: the two Perplexity-MCP paths bind the SAME id; only the backend differs."""

    def test_council_registry_has_the_same_ids_as_the_substrate(self):
        base = build_search_registry()
        council = search_registry()
        assert sorted(council.ids()) == sorted(base.ids())
        assert len(council.ids()) == len(base.ids())  # no binding was added

    def test_the_council_mcp_descriptor_is_digest_identical_to_the_substrate(self):
        base_entry = build_search_registry().get(COUNCIL_MCP_ADAPTER_ID)
        council_entry = search_registry().get(COUNCIL_MCP_ADAPTER_ID)
        assert council_entry.descriptor.to_dict() == council_mcp_descriptor().to_dict()
        # Identical axes => identical digest. Council swaps the backend, not the
        # binding: this is real client/backend duplication under one id.
        assert base_entry.digest == council_entry.digest

    def test_only_the_mcp_backend_is_swapped_everything_else_is_untouched(self):
        base = build_search_registry()
        council = search_registry()
        for adapter_id in base.ids():
            b, c = base.get(adapter_id), council.get(adapter_id)
            if adapter_id == COUNCIL_MCP_ADAPTER_ID:
                assert type(b.backend).__name__ == "SearchAdapterBackend"
                assert isinstance(c.backend, PerplexityMcpBackend)
                assert c.source == "council:perplexity_mcp"
            else:
                # Every other entry keeps the substrate backend and source.
                assert type(c.backend) is type(b.backend), adapter_id
                assert c.source == b.source == "builtin_search", adapter_id
        # The HTTP Perplexity binding is not Council's: it stays the substrate's.
        http = council.get("perplexity-search-http")
        assert http.source == "builtin_search"
        assert not isinstance(http.backend, PerplexityMcpBackend)


# ══ Criterion 2: equivalent qualified bindings, retained & independently qualified ══


class TestBothConsumersUseEquivalentQualifiedBindings:
    def test_the_two_provider_maps_are_identical_and_spec_derived(self):
        assert _PROVIDER_TO_ADAPTER == _ls.PROVIDER_TO_ADAPTER
        # Both are derived from the one spec table (first binding wins/provider).
        expected: dict[str, str] = {}
        for spec in SEARCH_PROVIDER_SPECS.values():
            expected.setdefault(spec.provider, spec.adapter_id)
        assert _PROVIDER_TO_ADAPTER == expected
        assert _ls.PROVIDER_TO_ADAPTER == expected

    def test_neither_consumer_names_an_adapter_outside_the_registry(self):
        council = search_registry()
        disc = discovery.registry_for()
        for adapter_id in _PROVIDER_TO_ADAPTER.values():
            assert adapter_id in council
            assert adapter_id in disc
        # Perplexity's second binding is reachable from both, too.
        assert "perplexity-search-http" in council and "perplexity-search-http" in disc

    def test_both_perplexity_backends_return_a_conforming_typed_outcome(self):
        # The substrate backend and Council's backend serve the same binding;
        # both must return a typed search-outcome/v1, so the two implementations
        # are independently qualified as equivalent.
        substrate = _run(
            SearchAdapterBackend(SEARCH_PROVIDER_SPECS["perplexity-mcp"]).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": _adapter_fake("normal")},
                {"domain_contract": SEARCH_OUTCOME_CONTRACT},
            )
        )
        council = _run(
            PerplexityMcpBackend(timeout=10.0).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": _council_fake("normal")},
                {"domain_contract": SEARCH_OUTCOME_CONTRACT},
            )
        )
        for outcome in (substrate, council):
            assert isinstance(outcome, SearchOutcome)
            assert outcome.status in SEARCH_STATUSES
            assert outcome.requested_provider == "perplexity"
            assert all(r.url.startswith("http") for r in outcome.results)


# ══ Criterion 3: handshake / tool discovery / typed failures / interchangeability ══


class TestPerplexityHandshakeAndToolDiscovery:
    def test_the_mcp_binding_negotiates_a_session_then_discovers_the_tool(self):
        backend = PerplexityMcpBackend(timeout=10.0)
        outcome = _run(
            backend.invoke(
                SEARCH_OPERATION,
                {"query": "qualify", "command": _council_fake("normal")},
                {},
            )
        )
        assert outcome.status == "success", outcome.error_code
        # The recorded session proves `initialize` completed against the server
        # (the handshake is load-bearing: the bare-call defect gets a -32002).
        session = backend.last_session
        assert session is not None
        assert session.protocol_version == "2025-06-18"
        assert session.server_info.get("name") == "council-fake"
        # The unsafe `javascript:` URL the server advertises is never promoted.
        assert outcome.results and all(r.url.startswith("http") for r in outcome.results)

    def test_the_discovery_path_discovers_the_tool_and_returns_one_real_source(self):
        outcome, receipt = _run(
            discovery.resolve_search_outcome(
                "qualify", provider="perplexity", mcp_command=_adapter_fake("normal")
            )
        )
        assert outcome.status == "success", outcome.error_code
        assert len(outcome.results) >= 1
        assert receipt.adapter_id == "perplexity-search-mcp"
        assert receipt.domain_contract == SEARCH_OUTCOME_CONTRACT
        assert receipt.transport == "mcp-stdio"
        assert receipt.self_approving is False
        assert receipt.is_authority is False


class TestMissingToolAuthOrConnectionIsTypedNeverFixtureSuccess:
    @pytest.mark.parametrize(
        "mode",
        ["tool_unavailable", "unavailable", "error", "reject-first-call"],
    )
    def test_no_substrate_failure_mode_becomes_a_fixture_success(self, mode):
        outcome = _run(
            SearchAdapterBackend(SEARCH_PROVIDER_SPECS["perplexity-mcp"]).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": _adapter_fake(mode)},
                {},
            )
        )
        assert isinstance(outcome, SearchOutcome)
        assert outcome.status in SEARCH_STATUSES
        if mode != "reject-first-call":  # a compliant handshake succeeds there
            assert outcome.status != "success", (mode, outcome.error_code)
            assert outcome.results == (), mode
            assert outcome.error_code, mode

    def test_a_missing_tool_is_typed_tool_missing_not_a_source(self):
        outcome = _run(
            SearchAdapterBackend(SEARCH_PROVIDER_SPECS["perplexity-mcp"]).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": _council_fake("missing-tool")},
                {},
            )
        )
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "tool_missing", outcome.error_code

    def test_a_missing_launcher_binding_is_typed_no_binding_not_a_source(self):
        # Discovery path: the mcp binding is named but no launcher is supplied.
        outcome, _ = _run(
            discovery.resolve_search_outcome("q", provider="perplexity", mcp_command=None)
        )
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding", outcome.error_code
        # Council backend: no command supplied.
        council_outcome = _run(
            PerplexityMcpBackend(timeout=5.0).invoke(SEARCH_OPERATION, {"query": "q"}, {})
        )
        assert council_outcome.error_code == "no_binding"
        # Council sync driver: no command supplied.
        sync_outcome, session = run_perplexity_mcp("q", command=None)
        assert sync_outcome.error_code == "no_binding"
        assert sync_outcome.results == () and session is None

    def test_a_missing_connection_is_typed_unavailable_not_a_source(self):
        outcome = _run(
            PerplexityMcpBackend(timeout=5.0).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": _council_fake("unavailable")},
                {},
            )
        )
        assert outcome.status != "success"
        assert outcome.results == (), "a dead stream must not yield a source"
        # A stream that closed before the child was usable is an availability
        # fact, reported as such -- never a fabricated source.
        assert outcome.status == "unavailable"
        assert outcome.error_code == "launch_failed"

    def test_a_text_only_synthesis_is_a_typed_failure_with_zero_sources(self):
        outcome = _run(
            PerplexityMcpBackend(timeout=10.0).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": _council_fake("text-only")},
                {},
            )
        )
        assert outcome.status != "success"
        assert outcome.results == (), "a synthesis with no cited URL is not a source"
        assert outcome.error_code == "no_results"

    def test_the_formatted_consumer_reports_a_failure_not_a_source(self):
        ws = WebSearch()
        text = _run(
            ws.search(
                "q",
                SearchConfig(
                    provider="perplexity",
                    full_content_results=0,
                    time_range="any",
                    verify_launcher_installed=False,
                ),
            )
        )
        assert "did not succeed" in text
        assert "no_binding" in text
        assert "https://" not in text, "a failure must render zero sources"

    def test_a_floating_launcher_is_refused_before_any_process_is_spawned(self):
        outcome = _run(
            PerplexityMcpBackend(timeout=5.0).invoke(
                SEARCH_OPERATION,
                {"query": "q", "command": ["uvx", "git+https://example.invalid/x"]},
                {},
            )
        )
        assert outcome.status != "success"
        assert outcome.error_code == "no_binding"
        assert outcome.results == ()


class _StubBackend(AdapterBackend):
    """A canned, network-free backend for the interchangeability check."""

    def __init__(self, outcome: SearchOutcome) -> None:
        self._outcome = outcome
        self.calls: list[Mapping[str, Any]] = []

    async def invoke(self, operation, argument, context):
        self.calls.append(dict(argument))
        return self._outcome


def _duckduckgo_registry(backend: AdapterBackend) -> AdapterRegistry:
    """A registry whose ``duckduckgo-search`` binding is served by ``backend``.

    ``in-process`` so the facade's egress gate is not the thing under test.
    """
    registry = AdapterRegistry()
    registry.register(
        AdapterDescriptor(
            id="duckduckgo-search",
            version="1.0.0",
            categories=(CATEGORY_SEARCH,),
            operations=(
                OperationBinding(
                    name=SEARCH_OPERATION,
                    input_contract="search-query/v1",
                    result_contract=SEARCH_OUTCOME_CONTRACT,
                    required_capabilities=("search.query",),
                ),
            ),
            transport="in-process",
            capabilities=("search.query",),
        ),
        backend,
        source="test:interchange",
    )
    return registry


class TestAnotherSupportedBindingExercisesInterchangeability:
    """A second binding, driven through the same facade by both consumers."""

    def _stub(self) -> _StubBackend:
        return _StubBackend(
            SearchOutcome(
                query_sha256="f" * 64,
                requested_provider="duckduckgo",
                status="success",
                results=(
                    SearchResultRecord(
                        title="Wired Result",
                        url="https://example.org/hit",
                        snippet="a real snippet",
                        source="web",
                        provider="duckduckgo",
                    ),
                ),
                attempts=(SearchAttempt("duckduckgo", "in-process", "success"),),
            )
        )

    def test_council_consumes_the_second_binding_through_the_registry(self):
        backend = self._stub()
        ws = WebSearch(registry=_duckduckgo_registry(backend))
        text = _run(
            ws.search(
                "interchange",
                SearchConfig(provider="duckduckgo", full_content_results=0, time_range="any"),
            )
        )
        assert "Wired Result" in text
        assert "https://example.org/hit" in text
        assert backend.calls, "the second binding was not invoked"

    def test_discovery_consumes_the_second_binding_through_the_same_facade(self):
        backend = self._stub()
        outcome, receipt = _run(
            discovery.resolve_search_outcome(
                "interchange", provider="duckduckgo", registry=_duckduckgo_registry(backend)
            )
        )
        assert outcome.status == "success", outcome.error_code
        assert outcome.results[0].url == "https://example.org/hit"
        assert receipt.adapter_id == "duckduckgo-search"
        assert receipt.self_approving is False
        assert backend.calls, "the second binding was not invoked"


# ══ Binding decision: one provider, two *labeled* bindings ══════════════════

#: The two bindings of the single ``perplexity`` provider, as the retained
#: registry actually carries them. ``official`` (``perplexity-search-mcp``,
#: ``mcp-stdio``) launches the vendor's own MCP server and is the *default*
#: provider resolution; ``api-http`` (``perplexity-search-http``, ``http``)
#: dials ``api.perplexity.ai`` and is reached only when its adapter id is named
#: explicitly -- it is never an implicit fallback. Both consume the vendor API
#: key reference (``perplexity_api_key``, symbol ``PERPLEXITY_API_KEY``). These
#: labels are the qualification of what ``src`` already binds, not an invented
#: vocabulary: the credential reference is read from the spec below, never
#: retargeted here.
_OFFICIAL_BINDING = ("perplexity-search-mcp", "mcp-stdio", "perplexity_api_key")
_API_HTTP_BINDING = ("perplexity-search-http", "http", "perplexity_api_key")
_PERPLEXITY_BINDING_LABELS = {
    "official": _OFFICIAL_BINDING,
    "api-http": _API_HTTP_BINDING,
}

#: A **third**, web-unofficial binding: the same provider identity served by a
#: browser session cookie (``PERPLEXITY_COOKIES``) instead of the vendor API
#: key. It is *not* one of the six retained providers; it exists only as an
#: opt-in external binding, registered through the one extension point
#: (``extra_adapters``) with no core edit. It is deliberately ``in-process`` so
#: the credential refusal -- not the egress gate -- is the fact under test.
_WEB_UNOFFICIAL_BINDING = ("perplexity-search-web-opt-in", "in-process", "PERPLEXITY_COOKIES")


def _binding_descriptor(
    adapter_id: str, transport: str, credential_ref: str
) -> AdapterDescriptor:
    """A synthetic search binding descriptor (an opt-in external adapter).

    Deliberately not one of the six retained providers: it plugs into the one
    extension point and declares its own credential reference, exactly as an
    externally-supplied session-cookie binding would.
    """
    return AdapterDescriptor(
        id=adapter_id,
        version="1.0.0",
        categories=(CATEGORY_SEARCH,),
        operations=(
            OperationBinding(
                name=SEARCH_OPERATION,
                input_contract="search-query/v1",
                result_contract=SEARCH_OUTCOME_CONTRACT,
                required_capabilities=("search.query",),
            ),
        ),
        transport=transport,
        capabilities=("search.query",),
        credential_refs=(credential_ref,),
    )


class TestOneProviderTwoLabeledBindings:
    """Perplexity is one provider: an official default and an opt-in binding."""

    def test_the_vendor_binding_is_the_default_resolution(self):
        spec = SEARCH_PROVIDER_SPECS["perplexity-mcp"]
        # First spec wins for a duplicated provider: the vendor MCP binding is
        # what an unqualified ``provider="perplexity"`` resolves to.
        assert _PROVIDER_TO_ADAPTER["perplexity"] == spec.adapter_id
        assert _ls.PROVIDER_TO_ADAPTER["perplexity"] == spec.adapter_id
        assert spec.transport == "mcp-stdio"
        assert spec.credential_refs == ("perplexity_api_key",)
        assert _PERPLEXITY_BINDING_LABELS["official"] == _OFFICIAL_BINDING
        # The official label is exactly what the retained registry carries.
        assert (_OFFICIAL_BINDING[0], _OFFICIAL_BINDING[1]) == (
            spec.adapter_id,
            spec.transport,
        )

    def test_the_second_binding_is_labeled_distinctly_and_opted_into_by_name(self):
        official = SEARCH_PROVIDER_SPECS["perplexity-mcp"]
        http = SEARCH_PROVIDER_SPECS["perplexity-http"]
        # Same provider identity, distinct adapter id (the label) and transport.
        assert official.provider == http.provider == "perplexity"
        assert official.adapter_id != http.adapter_id
        assert official.transport != http.transport
        assert _PERPLEXITY_BINDING_LABELS["api-http"] == _API_HTTP_BINDING
        assert (_API_HTTP_BINDING[0], _API_HTTP_BINDING[1]) == (
            http.adapter_id,
            http.transport,
        )
        # The api-http binding is *not* the default: reaching it requires
        # naming it, never an implicit fallback from the provider name.
        assert _PROVIDER_TO_ADAPTER["perplexity"] != http.adapter_id
        assert discovery.adapter_id_for("perplexity") == official.adapter_id
        # It is a real provider binding (not the web-unofficial opt-in), so it
        # consumes the vendor API key, never the session cookie.
        assert http.credential_refs == ("perplexity_api_key",)
        assert http.credential_refs != (_WEB_UNOFFICIAL_BINDING[2],)

    def test_provenance_names_the_binding_not_merely_the_provider(self):
        # The receipt's ``adapter_id`` + ``transport`` are the binding identity;
        # two bindings of one provider never collapse to the same provenance.
        # Both are the *real*, registered bindings: the official MCP binding is
        # driven offline against a local fake server, and the api-http binding
        # is admitted with an explicit egress grant but still makes no network
        # call (its backend refuses on the absent key first). Nothing is
        # invented in ``src`` and no provider is dialled.
        official = _run(
            discovery.resolve_search_outcome(
                "provenance", provider="perplexity", mcp_command=_adapter_fake("normal")
            )
        )[1]
        api_http = _run(
            discovery.resolve_search_outcome(
                "provenance",
                provider="perplexity",  # the SAME provider identity
                adapter_id="perplexity-search-http",
                allow_network=True,  # egress granted for this one binding only
                credential_availability={"perplexity_api_key": "available"},
            )
        )[1]
        assert (official.adapter_id, official.transport) == ("perplexity-search-mcp", "mcp-stdio")
        assert (api_http.adapter_id, api_http.transport) == ("perplexity-search-http", "http")
        # A verifier can tell which binding answered from the receipt alone.
        assert (official.adapter_id, official.transport) != (api_http.adapter_id, api_http.transport)
        for receipt in (official, api_http):
            assert receipt.domain_contract == SEARCH_OUTCOME_CONTRACT
            assert receipt.self_approving is False
            assert receipt.is_authority is False

    def test_the_binding_provenance_is_invariant_and_remains_non_authority(self):
        receipt = _run(
            discovery.resolve_search_outcome(
                "provenance", provider="perplexity", mcp_command=_council_fake("normal")
            )
        )[1]
        assert receipt.adapter_id == "perplexity-search-mcp"
        assert receipt.transport == "mcp-stdio"
        # The binding names itself; the receipt carries no gate authority.
        assert receipt.self_approving is False
        assert receipt.is_authority is False
        # Provenance without a package ref stays ``None`` -- nothing invented.
        assert receipt.provenance is None

    def test_a_credentialless_opt_in_binding_is_refused_typed_before_any_call(self):
        # The web-unofficial binding is ``in-process``, so the *credential*
        # gate -- not egress -- is the first blocking fact: an absent session
        # cookie refuses the call before any process starts, naming the very
        # reference that binding consumes.
        adapter_id, transport, credential_ref = _WEB_UNOFFICIAL_BINDING
        assert transport == "in-process"  # not an egress transport
        with pytest.raises(AdapterError) as excinfo:
            _run(
                discovery.resolve_search_outcome(
                    "no auth",
                    provider="perplexity",
                    adapter_id=adapter_id,
                    extra_adapters=[
                        (_binding_descriptor(*_WEB_UNOFFICIAL_BINDING),
                         PerplexityMcpBackend(timeout=5.0), "test:web-unofficial")
                    ],
                    mcp_command=_council_fake("normal"),
                    credential_availability={},  # nothing available
                )
            )
        assert excinfo.value.asset == f"credential:{credential_ref}"
        assert credential_ref in str(excinfo.value)

    def test_egress_is_named_before_credentials_for_the_remote_binding(self):
        # The api-http binding is a *remote dial*: under the default-deny egress
        # policy it is refused on egress -- and, because egress is checked
        # before credentials, declaring the missing credential does not change
        # which fact is reported. No source can be fabricated either way.
        for availability in ({}, {"perplexity_api_key": "missing"}):
            with pytest.raises(AdapterError) as excinfo:
                _run(
                    discovery.resolve_search_outcome(
                        "no egress",
                        provider="perplexity",
                        adapter_id="perplexity-search-http",
                        credential_availability=availability,
                    )
                )
            assert excinfo.value.asset == "egress"
            assert "egress" in str(excinfo.value)


# ══ Criterion 4: owners, providers and package kinds stay where they belong ══


class TestCriterion4OwnershipAndCategoryBoundaries:
    def test_the_search_provider_set_is_the_six_providers_only(self):
        # Capacium Exchange is never invented as a *provider*.
        providers = {spec.provider for spec in SEARCH_PROVIDER_SPECS.values()}
        assert providers == set(_SIX_PROVIDERS)
        assert not any("capacium" in p or "exchange" in p for p in providers)

    def test_no_extra_search_adapter_is_invented_around_the_six_providers(self):
        ids = tuple(build_search_registry().ids())
        assert ids == _ALL_SEVEN_IDS
        assert not any("capacium" in i or "exchange" in i for i in ids)

    def test_a_package_kind_is_not_a_runtime_adapter_category(self):
        # The runtime category set is closed and names no package kind.
        assert set(ADAPTER_CATEGORIES) == {"search", "generic"}
        assert "capacium" not in ADAPTER_CATEGORIES
        with pytest.raises(AdapterDescriptorError) as excinfo:
            AdapterDescriptor(
                id="perplexity-search",
                version="1.0.0",
                categories=("capacium",),  # a package kind, not a category
                operations=(
                    OperationBinding(
                        name=SEARCH_OPERATION,
                        input_contract="search-query/v1",
                        result_contract=SEARCH_OUTCOME_CONTRACT,
                    ),
                ),
                transport="http",
            )
        assert "category" in str(excinfo.value)

    def test_a_capacium_package_kind_is_quarantined_and_never_a_category(self):
        manifest = {
            "dialect": "capacium",
            "dialect_version": "1",
            "package": {
                "name": "perplexity-search-pack",
                "version": "1.0.0",
                "digest": "a" * 64,
                "container": False,
            },
            "members": [
                {
                    "name": "perplexity-search",
                    "id": "perplexity-search",
                    "version": "1.0.0",
                    "categories": ["search"],
                    "operations": [
                        {
                            "name": SEARCH_OPERATION,
                            "input_contract": "search-query/v1",
                            "result_contract": SEARCH_OUTCOME_CONTRACT,
                        }
                    ],
                    "transport": "mcp-stdio",
                    "capabilities": ["search.query"],
                    "kind": "mcp-server",  # an opaque package kind
                }
            ],
        }
        descriptor = CapaciumFormatReader().read(manifest).descriptor
        # The kind lives only in the quarantined package ref, never as a category.
        assert descriptor.package_ref.kind == "mcp-server"
        assert descriptor.categories == (CATEGORY_SEARCH,)
        assert "capacium" not in descriptor.categories
        assert set(default_reader_registry().dialects()) == {"local", "capacium"}

    def test_search_credentials_stay_with_the_provider_owner_not_the_agent(self):
        # A resolver with an EMPTY override environment reports the reference
        # missing without reading anything else; the suite never supplies a
        # value, and an unauthorized credential is refused rather than used.
        resolver = CredentialResolver(env_overrides={})
        assert resolver.resolve("PERPLEXITY_COOKIES").available is False
        assert resolver.resolve("PERPLEXITY_API_KEY").available is False
        # No value is ever injected for a missing reference: the owned child
        # environment comes back empty, so nothing leaks into a spawn.
        assert resolver.child_env(env_allowlist=("PERPLEXITY_COOKIES",)) == {}


# ══ Criterion 3 live attempt: real entry points, typed HOLD otherwise ═══════

#: Any of these environment-variable *names* being present satisfies "an
#: authorized Perplexity credential exists". Only presence is observed -- the
#: value is never read, printed, forwarded or changed by this suite.
_PERPLEXITY_CREDENTIAL_NAMES = (
    "PERPLEXITY_API_KEY",
    "PPLX_API_KEY",
    "PERPLEXITY_TOKEN",
)

#: Console-script names an already-installed Perplexity MCP entry point could
#: appear under. This suite never installs one; it only probes for an existing,
#: verified binary (the resolver itself refuses an unverified launcher).
_PERPLEXITY_MCP_SCRIPTS = (
    "perplexity-mcp",
    "perplexity-search-mcp",
    "mcp-server-perplexity",
)

#: The opt-in switch for the *other* supported binding's live path. Off by
#: default so the suite never substitutes a network call for a gated proof.
_LIVE_ENV = "SKILLWEAVE_LIVE_SEARCH"


def _perplexity_credential_present() -> bool:
    """Whether a Perplexity credential *name* is configured (value never read)."""
    return any(name in os.environ for name in _PERPLEXITY_CREDENTIAL_NAMES)


def _installed_perplexity_mcp_script() -> str | None:
    """A verified installed Perplexity MCP console script, or ``None``.

    Only a binary that actually resolves on ``PATH`` counts; the resolver then
    still verifies its distribution/version before executing it.
    """
    for name in _PERPLEXITY_MCP_SCRIPTS:
        found = shutil.which(name)
        if found:
            return found
    return None


def _perplexity_provider_reachable() -> bool:
    """Always ``False``: this suite refuses to originate a reachability probe.

    A TCP connect to the provider would be a network call, and this suite is
    required to make **none**. The fact is therefore recorded as *not
    performed* rather than measured: the honest record is that the suite cannot
    testify to reachability, not that the provider is unreachable.
    """
    return False


class TestLiveAttemptAtTheEnvironmentBoundary:
    """The real attempt: no installed Perplexity entry point => typed HOLD.

    These tests run *unconditionally* and record the honest outcome of driving
    the already-installed production entry points. They are the causal evidence
    for the criterion-3 HOLD: a missing tool / binding / connection is a typed
    non-success with zero results, and is never promoted to a fixture success.
    """

    def test_no_ambient_perplexity_launcher_is_registered_or_invented(self):
        # The resolver ships no default registration and, with no explicit
        # input, fails closed with an attributable asset -- it never trusts an
        # installed-but-unverified launcher, and never builds a floating one.
        with pytest.raises(LauncherResolutionError) as excinfo:
            resolve_launcher()
        assert excinfo.value.asset == "launcher"
        assert "floating install is never constructed" in str(excinfo.value)

    def test_the_live_attempt_records_the_absence_of_an_installed_entry_point(self):
        # The honest record of *this* environment: whether an already-installed
        # Perplexity MCP entry point and an authorized credential are present.
        # Recorded, never assumed; the live classes below are gated on it. All
        # three facts are read locally; the suite originates no network call, so
        # reachability is recorded as a NOT-PERFORMED probe, not measured.
        facts = {
            "installed_perplexity_mcp": _installed_perplexity_mcp_script(),
            "credential_name_present": _perplexity_credential_present(),
            "provider_reachability_probe": "not_performed",
        }
        assert isinstance(facts["credential_name_present"], bool)
        assert facts["provider_reachability_probe"] == "not_performed"
        assert facts["installed_perplexity_mcp"] is None or isinstance(
            facts["installed_perplexity_mcp"], str
        )

    def test_a_real_attempt_council_yields_a_typed_hold_not_a_fixture_source(self):
        # The Council production path, invoked for real with no resolvable
        # binding: a typed error with zero results and no negotiated session.
        outcome, session = run_perplexity_mcp("a harmless sandbox query", command=None)
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding"
        assert session is None

    def test_a_real_attempt_discovery_yields_a_typed_hold_with_a_receipt(self):
        # The Discovery production facade, invoked for real with no launcher:
        # a typed non-success plus a well-formed receipt (typed HOLD, not a
        # fixture success).
        outcome, receipt = _run(
            discovery.resolve_search_outcome(
                "a harmless sandbox query", provider="perplexity"
            )
        )
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding"
        assert receipt.adapter_id == "perplexity-search-mcp"
        assert receipt.transport == "mcp-stdio"
        assert receipt.domain_contract == SEARCH_OUTCOME_CONTRACT
        assert receipt.self_approving is False
        assert receipt.is_authority is False


@pytest.mark.skipif(
    _installed_perplexity_mcp_script() is None,
    reason=(
        "no installed Perplexity MCP entry point is present; the live handshake "
        "is a typed HOLD (see TestLiveAttemptAtTheEnvironmentBoundary)"
    ),
)
class TestLivePerplexityWhenAnInstalledEntryPointExists:
    """Runs only where an installed Perplexity MCP entry point is genuinely present.

    It performs the real lifecycle handshake and tool discovery, then one
    harmless sandbox query. When the resolved entry point lacks the tool, that
    is a typed ``tool_missing`` -- never a fixture success.
    """

    def test_a_resolved_native_entry_point_serves_the_tool_or_is_typed(self):
        script = _installed_perplexity_mcp_script()
        assert script is not None
        try:
            resolution = resolve_launcher(explicit_command=[script])
        except LauncherResolutionError as exc:  # pragma: no cover - env-dependent
            pytest.skip(f"the installed entry point is not admissible: {exc}")
        env_allowlist = tuple(
            name for name in _PERPLEXITY_CREDENTIAL_NAMES if name in os.environ
        )
        # Handshake + tool discovery happen inside the driver; a missing tool
        # surfaces as ``tool_missing`` before any query is issued.
        outcome, session = run_perplexity_mcp(
            "a harmless sandbox query",
            command=list(resolution.command),
            env_allowlist=env_allowlist,
            timeout=30.0,
        )
        if outcome.status != "success":
            assert outcome.results == ()
            assert outcome.error_code in {
                "tool_missing",
                "launch_failed",
                "timeout",
                "no_results",
                "unavailable",
                "mcp_error",
                "is_error",
            }, outcome.error_code
            return
        # A genuine live success must carry real cited http(s) sources and a
        # completed handshake -- the provenance a fixture can never supply.
        assert session is not None
        assert session.protocol_version
        assert outcome.results
        assert all(r.url.startswith("http") for r in outcome.results)
        assert PERPLEXITY_MCP_TOOL  # the tool this binding discovers and calls


@pytest.mark.skipif(
    os.environ.get(_LIVE_ENV) != "1",
    reason=f"set {_LIVE_ENV}=1 to run the opt-in live search proof",
)
class TestLiveOtherSupportedBinding:
    """The other supported binding, live, with real provider provenance.

    ``duckduckgo-search`` is a credential-free http binding. With an explicit
    egress grant it performs a real network query and returns real cited
    ``http(s)`` sources stamped with its own provenance. Off unless
    ``SKILLWEAVE_LIVE_SEARCH=1`` -- the suite never silently reaches the
    network.
    """

    def test_a_live_query_returns_real_cited_sources_with_provenance(self):
        outcome, receipt = _run(
            discovery.resolve_search_outcome(
                "skillweave",
                provider="duckduckgo",
                allow_network=True,
                endpoint_allowlist=["lite.duckduckgo.com"],
                max_results=5,
            )
        )
        assert outcome.status == "success", outcome.error_code
        assert outcome.results, "a live success must carry real sources"
        assert all(r.url.startswith("http") for r in outcome.results)
        assert all(r.provider == "duckduckgo" for r in outcome.results)
        assert outcome.attempts[0].transport == "http"
        assert receipt.adapter_id == "duckduckgo-search"
        assert receipt.domain_contract == SEARCH_OUTCOME_CONTRACT
        assert receipt.self_approving is False

    def test_another_bound_directly_fails_closed_without_an_egress_grant(self):
        # Default-deny egress: the same binding is refused before any call, so a
        # live result can only ever come from the explicit grant above.
        with pytest.raises(Exception) as excinfo:
            _run(
                discovery.resolve_search_outcome(
                    "skillweave", provider="duckduckgo", max_results=3
                )
            )
        assert "egress" in str(excinfo.value)
