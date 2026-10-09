"""Web Search integration for SkillWeave Council.

Six providers are served through one extension point -- the adapter registry in
:mod:`skillweave.adapters` -- not through a chain of ``elif`` branches:

- DuckDuckGo Lite (free, no key, requests-based HTML parsing)
- Serper (Google results via API key)
- SerpApi (Google/Bing/Yahoo via API key)
- Tavily (LLM-optimized search via API key)
- Brave (privacy-focused via API key)
- Perplexity (AI-powered search, via an initialized MCP stdio binding or a
  direct HTTP binding -- two transports for one provider)

:meth:`WebSearch.search` stays a **compatible formatted-string wrapper**: Council
still receives the same context string it always did. Internally it now resolves
the provider through the registry, invokes it as a typed
:class:`~skillweave.adapters.outcomes.SearchOutcome`, and renders that outcome.

There is **no silent fallback**: an unrecognised provider name fails closed and
is reported, rather than being quietly served by DuckDuckGo. Perplexity's MCP
binding is launched only from an explicit command binding supplied by the caller
(``SearchConfig.mcp_command``); the reader never constructs a floating
``git+``/URL install.

Includes: relevance reranking, full content fetching via Jina Reader.
"""

import asyncio
import json
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

from skillweave.adapters import (
    AdapterRegistry,
    SEARCH_OPERATION,
    SEARCH_PROVIDER_SPECS,
    SearchOutcome,
    SearchResultRecord,
    build_search_registry,
)

from .launcher import (
    LauncherResolutionError,
    UvxFallback,
    mcp_argument_from_resolution,
    resolve_launcher,
)
from .perplexity import (
    COUNCIL_MCP_ADAPTER_ID,
    PerplexityMcpBackend,
    council_mcp_descriptor,
)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str       # "web" or "news"
    date: str = ""    # ISO date string from result
    full_content: str = ""


@dataclass
class SearchConfig:
    provider: str = "duckduckgo"      # "duckduckgo" | "serper" | "serpapi" | "tavily" | "brave" | "perplexity"
    time_range: str = "any"
    max_results: int = 10
    full_content_results: int = 3
    hybrid_search: bool = True
    api_key: str | None = None
    #: Explicit MCP stdio launch command for the Perplexity MCP binding. When
    #: unset, the MCP launcher resolver still tries a registered compatible
    #: runtime and then a verified installed binary (never a floating install).
    mcp_command: list[str] | None = None
    #: Pin the launcher resolver to a specific registered runtime id.
    launcher_registration_id: str | None = None
    #: An explicit, pinned uvx fallback (exact distribution/version + network).
    uvx_fallback: "UvxFallback | None" = None
    #: Whether the resolver must verify an installed binary (off in unit use).
    verify_launcher_installed: bool = True
    #: Attribution for an explicit ``mcp_command`` (a free-form provenance map).
    launcher_provenance: dict | None = None
    #: Environment variable names forwarded to the owned MCP child.
    mcp_env_allowlist: tuple[str, ...] = ()


# ── provider -> adapter id, and the reverse, derived from the one registry ──
# These maps are *derived* from SEARCH_PROVIDER_SPECS: the registry is the
# single source of truth for which providers exist, and Council only names one.

_PROVIDER_TO_ADAPTER: dict[str, str] = {}
_ADAPTER_TO_PROVIDER: dict[str, str] = {}
_SPEC_BY_ADAPTER: dict[str, object] = {}
for _key, _spec in SEARCH_PROVIDER_SPECS.items():
    _PROVIDER_TO_ADAPTER.setdefault(_spec.provider, _spec.adapter_id)
    _ADAPTER_TO_PROVIDER[_spec.adapter_id] = _spec.provider
    _SPEC_BY_ADAPTER[_spec.adapter_id] = _spec


def search_registry() -> AdapterRegistry:
    """Build the shared search registry (all providers, one extension point).

    The one Council-owned difference from the bare substrate is the Perplexity
    ``mcp-stdio`` binding: Council replaces its backend with its own
    (``PerplexityMcpBackend``), which performs the lifecycle handshake, records
    the negotiated version/capabilities, normalizes every supported Perplexity
    result shape, and turns malformed/``isError``/missing-tool/JSON-RPC-error
    payloads into typed non-success outcomes. The adapter *id* and descriptor
    *axes* are unchanged, so provider resolution, the Discovery provider map and
    the facade are unaffected.
    """
    registry = build_search_registry()
    from skillweave.adapters import AdapterRegistry as _Registry  # local: type only

    # Rebuild the mcp binding only: a fresh registry, same descriptors, with the
    # Perplexity MCP backend swapped for Council's. Everything else is the
    # substrate's own backend, untouched.
    council = _Registry()
    for entry in registry.entries():
        if entry.descriptor.id == COUNCIL_MCP_ADAPTER_ID:
            continue
        council.register(entry.descriptor, entry.backend, source=entry.source)
    council.register(
        council_mcp_descriptor(), PerplexityMcpBackend(), source="council:perplexity_mcp"
    )
    return council


class WebSearch:
    """Multi-provider web search with time-range support."""

    TIME_RANGE_DAYS = {
        "30d": 30,
        "quarter": 90,
        "6mo": 180,
        "1yr": 365,
        "any": None,
    }

    PERPLEXITY_TIME_MAP = {
        "30d": "month",
        "quarter": "month",
        "6mo": "week",     # Perplexity only supports day/week/month/year
        "1yr": "year",
        "any": None,
    }

    def __init__(self, registry: Optional[AdapterRegistry] = None) -> None:
        self._registry = registry if registry is not None else search_registry()

    @property
    def registry(self) -> AdapterRegistry:
        """The extension registry every provider is registered through."""
        return self._registry

    async def search(self, query: str, config: SearchConfig | None = None) -> str:
        """Execute search and return formatted context string for council prompts.

        Formatting rules (criterion 4):
        - Real sources (http/https URLs from a success outcome) are numbered
          ``Result 1``, ``Result 2``, etc.
        - Failure, capability or provenance information is reported *outside*
          the result numbering, as a separate note at the top of the context.
        - A non-success outcome produces *zero* numbered results.
        """
        if config is None:
            config = SearchConfig()
        config = self._apply_defaults(config)

        outcome: Optional[SearchOutcome] = None
        try:
            outcome = await asyncio.wait_for(
                self.search_outcome(query, config),
                timeout=60.0
            )
        except asyncio.TimeoutError:
            return (
                f"Web Search: {config.provider} timed out after 60 seconds.\n"
                "No search results retrieved."
            )

        # ── non-success: report status outside result numbering ───────────
        if outcome.status != "success":
            return self._format_failure(outcome, config)

        results = [_to_search_result(r) for r in outcome.results]

        # Time-range filter (post-hoc for providers without native support)
        if config.time_range != "any":
            results = self._filter_by_time_range(results, config.time_range)

        # Relevance reranking
        results = self._rerank(results, query)

        # Fetch full content for top N
        if config.full_content_results > 0:
            results = await self._fetch_full_content(results[:config.full_content_results]) + results[config.full_content_results:]

        return self._format_context(results, config.time_range)

    # ── registry dispatch ────────────────────────────────────────────────

    def _adapter_id_for(self, provider: str, config: Optional[SearchConfig] = None) -> str:
        """Resolve a provider name to its registered adapter id, or fail.

        No silent fallback: an unknown provider raises rather than being served
        by DuckDuckGo. A provider served by more than one transport (Perplexity
        has an MCP stdio binding and an HTTP binding) is disambiguated by the
        configured binding: an explicit MCP launch command selects the MCP
        transport, otherwise the HTTP binding is used.
        """
        provider = provider.lower()
        if provider == "perplexity" and config is not None and config.mcp_command:
            adapter_id = "perplexity-search-mcp"
        else:
            adapter_id = _PROVIDER_TO_ADAPTER.get(provider)
        if adapter_id is None or adapter_id not in self._registry:
            raise ValueError(
                f"unknown search provider {provider!r}; registered providers: "
                f"{sorted({_ADAPTER_TO_PROVIDER[i] for i in self._registry.ids()})}"
            )
        return adapter_id

    async def search_outcome(self, query: str, config: SearchConfig | None = None) -> SearchOutcome:
        """Return the typed :class:`SearchOutcome` for a query (no formatting).

        This is the seam the registry-backed backend and the Council wrapper
        share, so a caller can obtain a typed result rather than a formatted
        string.
        """
        if config is None:
            config = SearchConfig()
        config = self._apply_defaults(config)
        adapter_id = self._adapter_id_for(config.provider, config)
        entry = self._registry.get(adapter_id)
        argument = self._backend_argument(query, config, adapter_id)
        return await entry.backend.invoke(SEARCH_OPERATION, argument, {})

    # ── failure formatting (criterion 4) ─────────────────────────────────

    def _format_failure(self, outcome: SearchOutcome, config: SearchConfig) -> str:
        """Format a non-success outcome: status outside result numbering.

        Returns a context string with a typed failure header and *zero*
        numbered results.  The typed outcome (status, error_code, attempts)
        is preserved for Discovery/Council consumers.
        """
        parts: list[str] = [
            f"Web Search: {config.provider} search did not succeed.",
            f"Status: {outcome.status}",
        ]
        if outcome.error_code:
            parts.append(f"Code: {outcome.error_code}")
        if outcome.attempts:
            for a in outcome.attempts:
                parts.append(
                    f"Attempt: provider={a.provider} transport={a.transport} "
                    f"status={a.status}"
                    + (f" code={a.error_code}" if a.error_code else "")
                )
        parts.append("No search results retrieved.")
        return "\n".join(parts)

    def _backend_argument(self, query: str, config: SearchConfig, adapter_id: str) -> dict:
        """The argument mapping handed to a search backend."""
        argument: dict = {
            "query": query,
            "max_results": config.max_results,
            "time_range": config.time_range,
            "api_key": config.api_key,
        }
        spec = _SPEC_BY_ADAPTER.get(adapter_id)
        if spec is not None and spec.transport == "mcp-stdio":
            argument["env_allowlist"] = config.mcp_env_allowlist
            # The resolved credential value is redacted from any
            # server-originated text the backend surfaces (defence in depth
            # alongside the allowlisted env values the client reads itself).
            if config.api_key:
                argument["secrets"] = [config.api_key]
            # Resolve the exact launcher (criterion 2). When the caller named an
            # explicit command it wins and no discovery happens; otherwise a
            # registered runtime / verified installed binary is resolved. A
            # failure here yields *no* adapter-owned launcher, so the backend
            # reports a typed ``no_binding`` rather than launching anything.
            #
            # ``command`` is deliberately left unset until the resolver has
            # *accepted* the binding: it is only ever populated by
            # ``mcp_argument_from_resolution`` below. That way a *refused*
            # explicit command (a floating ``git+``/URL install or an unpinned
            # ``uvx`` spec) leaves no command behind, and the backend spawns
            # nothing instead of executing a refused launcher.
            try:
                resolution = resolve_launcher(
                    explicit_command=config.mcp_command,
                    explicit_provenance=config.launcher_provenance,
                    registration_id=config.launcher_registration_id,
                    uvx_fallback=config.uvx_fallback,
                    verify_installed=config.verify_launcher_installed,
                )
            except LauncherResolutionError as exc:
                argument.pop("command", None)
                argument["launcher_provenance"] = {
                    "kind": "unresolved",
                    "source": "launcher_resolver",
                    "detail": str(exc),
                    "asset": getattr(exc, "asset", None),
                }
                argument["launcher_resolution_error"] = getattr(exc, "asset", None) or "launcher"
                return argument
            argument = mcp_argument_from_resolution(resolution, argument)
        return argument

    # ── Helpers ───────────────────────────────────────────────────────

    def _filter_by_time_range(self, results: list[SearchResult], time_range: str) -> list[SearchResult]:
        """Post-hoc date filter for providers without native time range support."""
        days = self.TIME_RANGE_DAYS.get(time_range)
        if days is None:
            return results

        cutoff = datetime.now() - timedelta(days=days)
        filtered = []
        for r in results:
            if r.date:
                try:
                    date_str = r.date.strip()
                    if "ago" in date_str.lower():
                        filtered.append(r)
                        continue
                    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%B %d, %Y"]:
                        try:
                            parsed = datetime.strptime(date_str[:10] if len(date_str) >= 10 else date_str, fmt)
                            if parsed >= cutoff:
                                filtered.append(r)
                            break
                        except ValueError:
                            continue
                except Exception:
                    filtered.append(r)
            else:
                filtered.append(r)
        return filtered

    def _rerank(self, results: list[SearchResult], query: str) -> list[SearchResult]:
        """Relevance reranking by token overlap scoring."""
        query_tokens = set(query.lower().split())

        def score(r: SearchResult) -> float:
            title_tokens = set(r.title.lower().split())
            snippet_tokens = set(r.snippet.lower().split())

            title_overlap = len(title_tokens & query_tokens) / max(len(title_tokens), 1)
            snippet_overlap = len(snippet_tokens & query_tokens) / max(len(snippet_tokens), 1)

            url_bonus = 0.0
            if any(domain in r.url for domain in [".gov", ".edu", ".org", "wikipedia.org", "github.com"]):
                url_bonus = 0.15

            freshness = 0.1 if r.source == "news" else 0.0

            return (title_overlap * 0.4) + (snippet_overlap * 0.35) + url_bonus + freshness

        scored = [(score(r), r) for r in results]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored]

    async def _fetch_full_content(self, results: list[SearchResult]) -> list[SearchResult]:
        """Fetch full article content via Jina Reader."""
        async def fetch_one(r: SearchResult) -> SearchResult:
            if not r.url:
                return r
            try:
                jina_url = f"https://r.jina.ai/{r.url}"
                req = urllib.request.Request(jina_url, headers={"Accept": "text/plain"})
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=25))
                content = resp.read().decode("utf-8", errors="replace")
                if len(content) > 500:
                    r.full_content = content[:2000]
            except Exception:
                pass
            return r

        tasks = [fetch_one(r) for r in results]
        return await asyncio.gather(*tasks)

    def _format_context(self, results: list[SearchResult], time_range: str) -> str:
        """Format search results as context string for council prompts."""
        lines = [f"Web Search Results (time range: {time_range}):\n"]
        for i, r in enumerate(results[:10], 1):
            content = r.full_content if r.full_content else r.snippet
            if len(content) > 500:
                content = content[:500] + "..."
            date_info = f" [{r.date}]" if r.date else ""
            lines.append(f"Result {i}: {r.title} | {r.url}{date_info}\n  {content}\n")
        return "\n".join(lines)

    def _apply_defaults(self, config: SearchConfig) -> SearchConfig:
        """Apply defaults from environment if not set."""
        env_map = {
            "serper": "SERPER_API_KEY",
            "serpapi": "SERPAPI_API_KEY",
            "tavily": "TAVILY_API_KEY",
            "brave": "BRAVE_API_KEY",
            "perplexity": "PERPLEXITY_API_KEY",
        }
        if not config.api_key and config.provider in env_map:
            config.api_key = os.environ.get(env_map[config.provider])
        return config


def _to_search_result(record: SearchResultRecord) -> SearchResult:
    """Turn one typed record into the legacy ``SearchResult`` shape."""
    return SearchResult(
        title=record.title,
        url=record.url,
        snippet=record.snippet,
        source=record.source,
        date=record.date,
    )
