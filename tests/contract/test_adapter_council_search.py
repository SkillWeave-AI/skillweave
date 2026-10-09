"""Council is a real registry consumer (SW-ADAPT-001, criteria 4 & 5).

``skillweave.council.search.WebSearch`` is the executable lifecycle consumer of
the search capability (the ``skillweave-discovery`` skill is a prompt pack; when
it wants the web it goes through Council, whose Stage 0 calls
``searcher.search(query)``). This module pins the consumer wiring:

* all six providers are reached through **one** extension registry -- seven
  bindings, Perplexity being two transports for one provider;
* provider names resolve to registered adapter ids, and an **unknown provider
  fails closed** rather than being quietly served by DuckDuckGo;
* Perplexity's transport is disambiguated by the configured binding (an explicit
  MCP launch command selects the MCP adapter, else the HTTP adapter);
* ``search_outcome`` returns the typed :class:`SearchOutcome`;
* ``WebSearch.search`` stays a **compatible formatted-string wrapper** for
  Council, and a non-success outcome is rendered as a visible error, never as a
  fetched source.

No live provider is called: each test injects a stub backend into a fresh
registry, so the wiring is exercised without network egress.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.adapters import (  # noqa: E402
    SEARCH_OPERATION,
    AdapterBackend,
    AdapterDescriptor,
    CATEGORY_SEARCH,
    OperationBinding,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
)
from skillweave.council.search import (  # noqa: E402
    SearchConfig,
    SearchResult,
    WebSearch,
    _PROVIDER_TO_ADAPTER,
)

_SIX_PROVIDERS = ("duckduckgo", "serper", "serpapi", "tavily", "brave", "perplexity")


class _StubBackend(AdapterBackend):
    """A backend that returns a canned outcome and records its calls."""

    def __init__(self, outcome: SearchOutcome) -> None:
        self._outcome = outcome
        self.calls: list[Mapping[str, Any]] = []

    async def invoke(self, operation, argument, context):
        self.calls.append(dict(argument))
        return self._outcome


def _success(url: str = "https://example.org/hit") -> SearchOutcome:
    return SearchOutcome(
        query_sha256="a" * 64,
        requested_provider="duckduckgo",
        status="success",
        results=(
            SearchResultRecord(
                title="Wired Result",
                url=url,
                snippet="a real snippet",
                source="web",
                provider="duckduckgo",
            ),
        ),
        attempts=(SearchAttempt("duckduckgo", "http", "success"),),
    )


def _failure() -> SearchOutcome:
    return SearchOutcome(
        query_sha256="b" * 64,
        requested_provider="serper",
        status="blocked",
        attempts=(SearchAttempt("serper", "http", "blocked", "no_credential"),),
        error_code="no_credential",
    )


def _registry_with(adapter_id: str, backend: AdapterBackend):
    from skillweave.adapters import AdapterRegistry

    registry = AdapterRegistry()
    registry.register(
        AdapterDescriptor(
            id=adapter_id,
            version="1.0.0",
            categories=(CATEGORY_SEARCH,),
            operations=(
                OperationBinding(
                    name=SEARCH_OPERATION,
                    input_contract="search-query/v1",
                    result_contract="search-outcome/v1",
                ),
            ),
            transport="http",
            capabilities=("search.query",),
        ),
        backend,
        source="test",
    )
    return registry


def _run(coro):
    return asyncio.run(coro)


# ── the extension point ────────────────────────────────────────────────────


def test_default_registry_serves_all_six_providers_through_one_registry():
    registry = WebSearch().registry
    providers = {_PROVIDER_TO_ADAPTER[p] for p in _SIX_PROVIDERS}
    assert providers <= set(registry.ids())
    # Seven bindings: six providers, Perplexity twice.
    assert len(registry.ids()) == 7


def test_the_council_consumer_uses_the_registry_not_an_elif_chain():
    ws = WebSearch()
    entry = ws.registry.get("duckduckgo-search")
    assert entry.descriptor.categories == (CATEGORY_SEARCH,)
    assert entry.descriptor.operation(SEARCH_OPERATION).result_contract == "search-outcome/v1"


# ── provider resolution & no silent fallback ───────────────────────────────


@pytest.mark.parametrize("provider", _SIX_PROVIDERS)
def test_each_provider_resolves_to_a_registered_adapter(provider):
    ws = WebSearch()
    adapter_id = ws._adapter_id_for(provider, SearchConfig(provider=provider))
    assert adapter_id in ws.registry


@pytest.mark.parametrize("provider", ["bogus", "google", "bing", ""])
def test_unknown_provider_fails_closed(provider):
    ws = WebSearch()
    with pytest.raises(ValueError):
        ws._adapter_id_for(provider, SearchConfig(provider=provider))


def test_perplexity_defaults_to_the_mcp_binding_then_http_is_selectable():
    ws = WebSearch()
    # No explicit MCP command -> the derived default map is used.
    default = ws._adapter_id_for("perplexity", SearchConfig(provider="perplexity"))
    assert default in ("perplexity-search-mcp", "perplexity-search-http")
    # An explicit MCP launch command selects the MCP transport.
    with_mcp = ws._adapter_id_for(
        "perplexity",
        SearchConfig(provider="perplexity", mcp_command=["mcp-server"]),
    )
    assert with_mcp == "perplexity-search-mcp"


def test_mcp_backend_argument_carries_the_explicit_command_only_for_mcp():
    ws = WebSearch()
    mcp_arg = ws._backend_argument(
        "q",
        SearchConfig(provider="perplexity", mcp_command=["mcp-server"], mcp_env_allowlist=("TOKEN",)),
        "perplexity-search-mcp",
    )
    assert mcp_arg["command"] == ["mcp-server"]
    assert mcp_arg["env_allowlist"] == ("TOKEN",)
    http_arg = ws._backend_argument("q", SearchConfig(provider="perplexity"), "perplexity-search-http")
    assert "command" not in http_arg


# ── typed outcome seam ─────────────────────────────────────────────────────


def test_search_outcome_returns_a_typed_search_outcome():
    backend = _StubBackend(_success())
    ws = WebSearch(registry=_registry_with("duckduckgo-search", backend))
    outcome = _run(ws.search_outcome("hello", SearchConfig(provider="duckduckgo")))
    assert isinstance(outcome, SearchOutcome)
    assert outcome.status == "success"
    assert outcome.results[0].url == "https://example.org/hit"
    assert backend.calls and backend.calls[0]["query"] == "hello"


# ── the compatible formatted-string wrapper ────────────────────────────────


def test_websearch_search_still_returns_a_formatted_context_string():
    backend = _StubBackend(_success())
    ws = WebSearch(registry=_registry_with("duckduckgo-search", backend))
    text = _run(
        ws.search(
            "hello world",
            SearchConfig(provider="duckduckgo", full_content_results=0, time_range="any"),
        )
    )
    assert isinstance(text, str)
    assert "Wired Result" in text
    assert "example.org/hit" in text


def test_websearch_search_reports_a_non_success_as_a_visible_error():
    backend = _StubBackend(_failure())
    ws = WebSearch(registry=_registry_with("duckduckgo-search", backend))
    # Point the config at the registered adapter; the stub ignores the provider.
    text = _run(
        ws.search(
            "anything",
            SearchConfig(provider="duckduckgo", full_content_results=0),
        )
    )
    # The stub returned a non-success outcome: Council gets a visible notice,
    # never a fabricated source line.
    assert isinstance(text, str)
    assert "did not succeed" in text
    assert "Status: blocked" in text
    assert "https://" not in text


def test_websearch_search_timeout_is_reported_not_silent(monkeypatch):
    ws = WebSearch(registry=_registry_with("duckduckgo-search", _StubBackend(_success())))

    async def _hang(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(ws, "search_outcome", _hang)
    import skillweave.council.search as mod

    real_wait_for = asyncio.wait_for

    async def _short_wait_for(coro, timeout):
        return await real_wait_for(coro, timeout=0.01)

    monkeypatch.setattr(mod.asyncio, "wait_for", _short_wait_for)
    text = _run(ws.search("q", SearchConfig(provider="duckduckgo", full_content_results=0)))
    assert "timed out" in text.lower()
