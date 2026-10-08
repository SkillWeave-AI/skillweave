"""The six search providers, each an adapter at the same extension point.

Every provider -- DuckDuckGo, Serper, SerpApi, Tavily, Brave and Perplexity --
is registered through the *same* :class:`~skillweave.adapters.registry.
AdapterRegistry` with a neutral descriptor. Nothing in the facade branches on a
provider name: a provider is data plus a backend callable.

Perplexity is registered **twice as alternative transport bindings**:

* ``perplexity-mcp`` -- transport ``mcp-stdio``, invoking the declared MCP tool
  over a compliant, handshake-first stdio client against an explicit (non-
  floating) command;
* ``perplexity-http`` -- transport ``http``, the direct API binding.

The two share the Perplexity provider identity but are distinct adapters with
distinct transports, which is exactly the axis separation the descriptor exists
to express.

Each backend produces a typed :class:`~skillweave.adapters.outcomes.SearchOutcome`
whose ``attempts`` record the transport and how it ended, and whose
``requested_provider`` is always the adapter's own provider -- so an outage is
never dressed up as a retrieved source.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote_plus, unquote

from .descriptor import (
    CATEGORY_SEARCH,
    AdapterDescriptor,
    OperationBinding,
)
from .mcp_client import McpClientError, McpStdioClient
from .outcomes import (
    SEARCH_OUTCOME_CONTRACT,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
    query_digest,
)
from .registry import AdapterBackend, AdapterRegistry

__all__ = [
    "SEARCH_OPERATION",
    "SEARCH_CATEGORY",
    "BASE_SEARCH_CAPABILITIES",
    "EGRESS_CAPABILITY",
    "SEARCH_PROVIDER_SPECS",
    "SearchProviderSpec",
    "SearchAdapterBackend",
    "build_search_registry",
    "PROVIDER_OUTCOME_CONTRACT",
]

SEARCH_CATEGORY = CATEGORY_SEARCH
SEARCH_OPERATION = "search.query"
EGRESS_CAPABILITY = "network-egress"
BASE_SEARCH_CAPABILITIES: tuple[str, ...] = ("search.query",)

#: The SDK-owned domain contract a search operation binds to. It is the single
#: constant defined next to the typed outcome it names; the facade's
#: contract->outcome binding reads the same value, so the two can never drift.
PROVIDER_OUTCOME_CONTRACT = SEARCH_OUTCOME_CONTRACT

_USER_AGENT = "SkillWeave-Adapter/0.1 (https://github.com/LangeVC/skillweave)"


class ProviderCallError(RuntimeError):
    """A provider call ended in a non-success status.

    ``status`` is one of the typed outcome statuses and ``code`` is the
    transport error code (e.g. an HTTP status or a JSON-RPC code).
    """

    def __init__(
        self, message: str, *, status: str = "error", code: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class SearchProviderSpec:
    """The static facts about one search provider.

    ``endpoint_host`` is the egress destination the policy allowlist is checked
    against; it is ``None`` for the in-process MCP stdio binding (the child is
    launched locally, and its own network egress is the server's concern).

    ``credential_refs`` are the credential reference ids the adapter's
    descriptor declares.  The facade checks these against the caller's
    ``credential_availability`` mapping before the call; a missing credential
    for a required ref causes a hard refusal.
    """

    adapter_id: str
    provider: str
    transport: str
    capabilities: tuple[str, ...]
    endpoint_host: Optional[str]
    fetcher: Callable[..., Any]
    credential_refs: tuple[str, ...] = ()


# ── provider fetchers (return typed records; raise ProviderCallError) ──────


def _records_from(items: Sequence[Mapping[str, Any]], provider: str) -> list[SearchResultRecord]:
    out: list[SearchResultRecord] = []
    for r in items:
        url = str(r.get("url", ""))
        if not (url.startswith("http://") or url.startswith("https://")):
            continue  # never promote a non-http(s) value to a source
        out.append(
            SearchResultRecord(
                title=str(r.get("title", "")),
                url=url,
                snippet=str(r.get("snippet", "")),
                source=str(r.get("source", "web")),
                provider=provider,
                date=str(r.get("date", "")),
            )
        )
    return out


def _http_json(
    url: str, *, data: Optional[bytes] = None, headers: Mapping[str, str]
) -> Any:
    req = urllib.request.Request(url, data=data, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - explicit call
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ProviderCallError(
            f"HTTP {exc.code} from {url}",
            status="blocked",
            code=f"http_{exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderCallError(
            f"network error from {url}: {exc.reason}",
            status="unavailable",
            code="network_error",
        ) from exc


async def _in_executor(fn: Callable[[], Any]) -> Any:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn)


def _normalise_tag_quotes(html: str) -> str:
    """Normalise single quotes to double quotes only inside HTML tags.

    This avoids corrupting apostrophes in title or snippet text content.
    """
    def _replace_tag(m: re.Match) -> str:
        return m.group(0).replace("'", '"')
    return re.sub(r"<[^>]+>", _replace_tag, html)


def _ddg_parse(html: str) -> list[SearchResultRecord]:
    """Parse DuckDuckGo Lite HTML into typed search result records.

    Handles multiple DOM variants observed in the Lite page:
    * ``class=\"result-link\"`` or ``class='result-link'``
    * ``href`` before or after ``class`` attribute
    * ``class=\"result-snippet\"`` or ``class='result-snippet'``
    * ``<a>`` inside a ``<td>`` (standard) or bare ``<a>`` in a result row
    * paired by table-row proximity -- never separate-list indexing.

    Returns zero records for empty pages, challenge pages or malformed HTML
    with no parseable results.
    """
    # Normalise quotes to double *only inside HTML tags* so apostrophes in
    # free text (titles, snippets) are never corrupted.
    normalised = _normalise_tag_quotes(html)

    # --- 1. Detect challenge/empty pages early -------------------------------
    _challenge_indicators = (
        "challenge", "captcha", "verify you are human",
        "please confirm", "unusual traffic",
    )
    lower = normalised.lower()
    if any(ind in lower for ind in _challenge_indicators) and "result-link" not in normalised:
        return []

    # --- 2. Extract result rows by scanning for <a class="result-link" --------
    # Each result is wrapped in a <tr> that contains the link anchor and
    # may be followed by a sibling <tr> with the snippet.  We use a
    # pattern that works across attribute order variants:
    #
    #   <a [anything] class="result-link" [anything] href="URL" [anything]>TITLE</a>
    #   <a [anything] href="URL" [anything] class="result-link" [anything]>TITLE</a>
    #
    # Use a lookahead to match both "class" and "href" in any order within the
    # <a> tag, capturing the href and the inner HTML.
    # Then locate the nearest following result-snippet <td> (same or next row).
    LINK_RE = re.compile(
        r'<a\s(?=[^>]*?class="result-link")(?=[^>]*?href="([^"]*)")(?:[^>]*)>(.*?)</a>',
        re.DOTALL,
    )
    out: list[SearchResultRecord] = []
    for m_link in LINK_RE.finditer(normalised):
        href = m_link.group(1).strip()
        title_html = m_link.group(2)
        link_end = m_link.end()

        # Decode uddg redirect
        url = href
        if "uddg=" in url:
            m_uddg = re.search(r"uddg=([^&]+)", url)
            if m_uddg:
                url = unquote(m_uddg.group(1))
        # HTML-unescape the href so &amp; and other entities are decoded.
        url = html_mod.unescape(url)
        if not (url.startswith("http://") or url.startswith("https://")):
            continue

        title = re.sub(r"<[^>]+>", "", title_html).strip()
        # HTML-unescape the title text too.
        title = html_mod.unescape(title)
        if not title:
            continue

        # --- 3. Find the snippet for *this* result only ------------------------
        # Scan from just after this link's </a> until the next <a with
        # class="result-link" (or end of string).  This prevents a row that
        # has no snippet from leaking into the next result's snippet.
        snippet = ""
        after_link = normalised[link_end:]
        next_link_match = re.search(
            r'<a\s(?=[^>]*?class="result-link")',
            after_link,
        )
        if next_link_match:
            snippet_region = after_link[: next_link_match.start()]
        else:
            snippet_region = after_link
        m_snip = re.search(
            r'<td[^>]*?class="result-snippet"[^>]*>(.*?)</td>',
            snippet_region,
            re.DOTALL,
        )
        if m_snip:
            snippet = re.sub(r"<[^>]+>", "", m_snip.group(1)).strip()
            snippet = html_mod.unescape(snippet)

        out.append(
            SearchResultRecord(
                title=title,
                url=url,
                snippet=snippet[:500],
                source="web",
                provider="duckduckgo",
            )
        )
    return out


async def _fetch_duckduckgo(query: str, opts: Mapping[str, Any]) -> list[SearchResultRecord]:
    encoded = quote_plus(query)
    url = f"https://lite.duckduckgo.com/lite/?q={encoded}"

    def _run() -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec
                status_code = resp.status
                if status_code == 429:
                    raise ProviderCallError(
                        "HTTP 429 rate limited by DuckDuckGo Lite",
                        status="blocked", code="http_429",
                    )
                if status_code >= 500:
                    raise ProviderCallError(
                        f"HTTP {status_code} server error from DuckDuckGo Lite",
                        status="unavailable", code=f"http_{status_code}",
                    )
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                # Empty body or whitespace-only is "empty" (no results).
                if not text.strip():
                    return ""
                # HTML challenge pages contain no "result-link" class but may
                # include a challenge form or captcha.  Detect via size +
                # known challenge markers.
                _challenge_markers = (
                    "challenge", "captcha", "verify you are human",
                    "please confirm", "unusual traffic", "h-captcha",
                )
                lower = text.lower()
                if any(m in lower for m in _challenge_markers) and "result-link" not in text:
                    raise ProviderCallError(
                        "DuckDuckGo Lite returned a challenge/captcha page",
                        status="blocked", code="challenge",
                    )
                return text
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 429:
                raise ProviderCallError(
                    "HTTP 429 rate limited", status="blocked", code="http_429",
                ) from exc
            if code >= 500:
                raise ProviderCallError(
                    f"HTTP {code} server error", status="unavailable",
                    code=f"http_{code}",
                ) from exc
            raise ProviderCallError(
                f"HTTP {code}", status="blocked", code=f"http_{code}",
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderCallError(
                f"DuckDuckGo unavailable: {exc.reason}",
                status="unavailable",
                code="network_error",
            ) from exc

    try:
        html = await _in_executor(_run)
    except asyncio.TimeoutError:
        raise ProviderCallError(
            "DuckDuckGo Lite request timed out",
            status="unavailable", code="timeout",
        )

    # An empty string means no results (empty page) -- return zero records.
    if not html.strip():
        return []

    records = _ddg_parse(html)
    # Filter out unsafe URL schemes from parsed results.
    safe: list[SearchResultRecord] = []
    for r in records:
        if r.url.startswith("http://") or r.url.startswith("https://"):
            safe.append(r)
    return safe


async def _fetch_serper(query: str, opts: Mapping[str, Any]) -> list[SearchResultRecord]:
    api_key = opts.get("api_key")
    if not api_key:
        raise ProviderCallError("SERPER_API_KEY not set", status="blocked", code="no_credential")
    body = json.dumps({"q": query, "num": opts.get("max_results", 10)}).encode()
    data = await _in_executor(
        lambda: _http_json(
            "https://google.serper.dev/search",
            data=body,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        )
    )
    return _records_from(
        [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
                "date": r.get("date", ""),
            }
            for r in data.get("organic", [])
        ],
        "serper",
    )


async def _fetch_serpapi(query: str, opts: Mapping[str, Any]) -> list[SearchResultRecord]:
    api_key = opts.get("api_key")
    if not api_key:
        raise ProviderCallError("SERPAPI_API_KEY not set", status="blocked", code="no_credential")
    params = {
        "q": query,
        "api_key": api_key,
        "num": opts.get("max_results", 10),
        "engine": "google",
    }
    qs = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    data = await _in_executor(
        lambda: _http_json(f"https://serpapi.com/search?{qs}", headers={})
    )
    return _records_from(
        [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
                "date": r.get("date", ""),
            }
            for r in data.get("organic_results", [])
        ],
        "serpapi",
    )


async def _fetch_tavily(query: str, opts: Mapping[str, Any]) -> list[SearchResultRecord]:
    api_key = opts.get("api_key")
    if not api_key:
        raise ProviderCallError("TAVILY_API_KEY not set", status="blocked", code="no_credential")
    body = json.dumps(
        {
            "query": query,
            "max_results": opts.get("max_results", 10),
            "search_depth": "advanced",
            "include_raw_content": False,
        }
    ).encode()
    data = await _in_executor(
        lambda: _http_json(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
    )
    return _records_from(
        [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in data.get("results", [])
        ],
        "tavily",
    )


async def _fetch_brave(query: str, opts: Mapping[str, Any]) -> list[SearchResultRecord]:
    api_key = opts.get("api_key")
    if not api_key:
        raise ProviderCallError("BRAVE_API_KEY not set", status="blocked", code="no_credential")
    url = (
        f"https://api.search.brave.com/res/v1/web/search"
        f"?q={quote_plus(query)}&count={opts.get('max_results', 10)}"
    )
    data = await _in_executor(
        lambda: _http_json(
            url,
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
        )
    )
    return _records_from(
        [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
                "date": r.get("page_age", ""),
            }
            for r in data.get("web", {}).get("results", [])
        ],
        "brave",
    )


async def _fetch_perplexity_http(
    query: str, opts: Mapping[str, Any]
) -> tuple[list[SearchResultRecord], Optional[str]]:
    api_key = opts.get("api_key")
    if not api_key:
        raise ProviderCallError(
            "PERPLEXITY_API_KEY not set", status="blocked", code="no_credential"
        )
    body = {
        "model": "sonar-pro",
        "messages": [{"role": "user", "content": query}],
        "max_tokens": 2000,
        "temperature": 0.2,
        "return_related_questions": False,
    }
    data = await _in_executor(
        lambda: _http_json(
            "https://api.perplexity.ai/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
    )
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    citations = data.get("citations", [])
    records = [
        SearchResultRecord(
            title=f"Source {i + 1}",
            url=str(url),
            snippet="Referenced in Perplexity search results",
            source="web",
            provider="perplexity",
        )
        for i, url in enumerate(citations[: opts.get("max_results", 10)])
        if str(url).startswith("http")
    ]
    return records, (content or None)


async def _fetch_perplexity_mcp(
    query: str, opts: Mapping[str, Any]
) -> tuple[list[SearchResultRecord], Optional[str]]:
    command = opts.get("command")
    if not command:
        raise ProviderCallError(
            "no MCP launcher binding supplied for perplexity", status="error", code="no_binding"
        )
    tool = str(opts.get("tool_name", "perplexity_search"))
    client = McpStdioClient(
        command,
        env_allowlist=opts.get("env_allowlist", ()),
        extra_env=opts.get("extra_env", {}),
        protocol_version=str(opts.get("protocol_version", "2025-06-18")),
    )
    try:
        await client.start()
        tools = await client.list_tools()
        if tool not in tools:
            raise ProviderCallError(
                f"MCP server does not expose tool {tool!r}; tools={tools}",
                status="error",
                code="tool_missing",
            )
        result = await client.call_tool(tool, {"query": query})
    except McpClientError as exc:
        raise ProviderCallError(
            f"perplexity MCP call failed: {exc}",
            status="error",
            code=str(exc.code) if exc.code is not None else "mcp_error",
        ) from exc
    finally:
        await client.close()

    structured = result.get("structuredContent", {}) if isinstance(result, Mapping) else {}
    records = _records_from(list(structured.get("results", [])), "perplexity")
    if not records:
        records = [
            SearchResultRecord(
                title=str(c.get("title", f"Source {i + 1}")),
                url=str(c.get("url", "")),
                snippet=str(c.get("snippet", ""))[:500],
                source="web",
                provider="perplexity",
            )
            for i, c in enumerate(structured.get("citations", []))
            if str(c.get("url", "")).startswith("http")
        ]
    synthesis = None
    blocks = result.get("content", []) if isinstance(result, Mapping) else []
    if isinstance(blocks, list):
        texts = [
            b.get("text", "") for b in blocks if isinstance(b, Mapping) and b.get("type") == "text"
        ]
        synthesis = "\n".join(t for t in texts if t) or None
    return records, synthesis


SEARCH_PROVIDER_SPECS: dict[str, SearchProviderSpec] = {
    "duckduckgo": SearchProviderSpec(
        adapter_id="duckduckgo-search",
        provider="duckduckgo",
        transport="http",
        capabilities=BASE_SEARCH_CAPABILITIES + (EGRESS_CAPABILITY,),
        endpoint_host="lite.duckduckgo.com",
        fetcher=_fetch_duckduckgo,
        # DuckDuckGo Lite requires no API key; no credential_ref needed.
    ),
    "serper": SearchProviderSpec(
        adapter_id="serper-search",
        provider="serper",
        transport="http",
        capabilities=BASE_SEARCH_CAPABILITIES + (EGRESS_CAPABILITY,),
        endpoint_host="google.serper.dev",
        fetcher=_fetch_serper,
        credential_refs=("serper_api_key",),
    ),
    "serpapi": SearchProviderSpec(
        adapter_id="serpapi-search",
        provider="serpapi",
        transport="http",
        capabilities=BASE_SEARCH_CAPABILITIES + (EGRESS_CAPABILITY,),
        endpoint_host="serpapi.com",
        fetcher=_fetch_serpapi,
        credential_refs=("serpapi_api_key",),
    ),
    "tavily": SearchProviderSpec(
        adapter_id="tavily-search",
        provider="tavily",
        transport="http",
        capabilities=BASE_SEARCH_CAPABILITIES + (EGRESS_CAPABILITY,),
        endpoint_host="api.tavily.com",
        fetcher=_fetch_tavily,
        credential_refs=("tavily_api_key",),
    ),
    "brave": SearchProviderSpec(
        adapter_id="brave-search",
        provider="brave",
        transport="http",
        capabilities=BASE_SEARCH_CAPABILITIES + (EGRESS_CAPABILITY,),
        endpoint_host="api.search.brave.com",
        fetcher=_fetch_brave,
        credential_refs=("brave_api_key",),
    ),
    "perplexity-mcp": SearchProviderSpec(
        adapter_id="perplexity-search-mcp",
        provider="perplexity",
        transport="mcp-stdio",
        capabilities=BASE_SEARCH_CAPABILITIES,
        endpoint_host=None,
        fetcher=_fetch_perplexity_mcp,
        credential_refs=("perplexity_api_key",),
    ),
    "perplexity-http": SearchProviderSpec(
        adapter_id="perplexity-search-http",
        provider="perplexity",
        transport="http",
        capabilities=BASE_SEARCH_CAPABILITIES + (EGRESS_CAPABILITY,),
        endpoint_host="api.perplexity.ai",
        fetcher=_fetch_perplexity_http,
        credential_refs=("perplexity_api_key",),
    ),
}


class SearchAdapterBackend(AdapterBackend):
    """Turns one provider spec into a typed search outcome."""

    def __init__(self, spec: SearchProviderSpec) -> None:
        self.spec = spec

    async def invoke(
        self, operation: str, argument: Mapping[str, Any], context: Mapping[str, Any]
    ) -> SearchOutcome:
        if operation != SEARCH_OPERATION:
            raise ProviderCallError(
                f"search backend serves only {SEARCH_OPERATION!r}",
                status="error",
                code="unsupported_operation",
            )
        query = str(argument.get("query", ""))
        if not query.strip():
            raise ProviderCallError(
                "search requires a non-empty query", status="error", code="empty_query"
            )
        opts = dict(argument)
        fetched: Any = None
        synthesis: Optional[str] = None
        try:
            fetched = await self.spec.fetcher(query, opts)
        except ProviderCallError as exc:
            return SearchOutcome(
                query_sha256=query_digest(query),
                requested_provider=self.spec.provider,
                status=exc.status,
                attempts=(
                    SearchAttempt(
                        provider=self.spec.provider,
                        transport=self.spec.transport,
                        status=exc.status,
                        error_code=exc.code,
                    ),
                ),
                error_code=exc.code,
            )
        if isinstance(fetched, tuple):
            records, synthesis = fetched
        else:
            records = fetched
        if not records:
            return SearchOutcome(
                query_sha256=query_digest(query),
                requested_provider=self.spec.provider,
                status="unavailable",
                attempts=(
                    SearchAttempt(
                        provider=self.spec.provider,
                        transport=self.spec.transport,
                        status="unavailable",
                        error_code="no_results",
                    ),
                ),
                error_code="no_results",
            )
        return SearchOutcome(
            query_sha256=query_digest(query),
            requested_provider=self.spec.provider,
            status="success",
            results=tuple(records),
            attempts=(
                SearchAttempt(
                    provider=self.spec.provider,
                    transport=self.spec.transport,
                    status="success",
                    error_code=None,
                ),
            ),
            synthesis=synthesis,
        )


def build_search_registry(
    registry: Optional[AdapterRegistry] = None,
    *,
    provider_keys: Optional[Sequence[str]] = None,
) -> AdapterRegistry:
    """Register every search provider through the same extension point.

    Default registers all seven bindings (six providers; Perplexity has an MCP
    and an HTTP binding). Idempotent per registry: re-registering the same id
    raises, as a duplicate id must.
    """
    registry = registry if registry is not None else AdapterRegistry()
    keys = tuple(provider_keys) if provider_keys is not None else tuple(SEARCH_PROVIDER_SPECS)
    for key in keys:
        spec = SEARCH_PROVIDER_SPECS[key]
        descriptor = AdapterDescriptor(
            id=spec.adapter_id,
            version="1.0.0",
            categories=(SEARCH_CATEGORY,),
            operations=(
                OperationBinding(
                    name=SEARCH_OPERATION,
                    input_contract="search-query/v1",
                    result_contract=PROVIDER_OUTCOME_CONTRACT,
                    required_capabilities=tuple(spec.capabilities),
                ),
            ),
            transport=spec.transport,
            capabilities=tuple(spec.capabilities),
            # Only declare credential_refs on the descriptor for egress
            # transports.  Non-egress transports (e.g. mcp-stdio) handle
            # credentials at the backend level and must not be gated here,
            # which would alter substrate default behaviour.
            credential_refs=(
                spec.credential_refs
                if spec.transport in ("http", "https", "http+json", "grpc")
                else ()
            ),
        )
        registry.register(descriptor, SearchAdapterBackend(spec), source="builtin_search")
    return registry
