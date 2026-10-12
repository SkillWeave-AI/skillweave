"""Council-owned Perplexity MCP backend and result normalization (SW-COUNCIL-003).

The SW-ADAPT-001 substrate registers a Perplexity ``mcp-stdio`` binding whose
backend lives in ``skillweave.adapters``. The addendum's defect was in the
*consumer*: the shape of a Perplexity MCP result was not normalized, a
``text``-only synthesis could be mistaken for a source, and ``isError``/missing
tools were not typed failures. This module is Council's repair -- kept in
Council's write scope -- and is wired in by Council's :func:`search_registry`
*in place of* the substrate's MCP backend (same adapter id, same descriptor
axes; only the call path is Council-owned). The adapter package is untouched.

Normalization rules (all fail closed):

* only ``http(s)`` URLs become sources -- a ``javascript:``/data citation is
  dropped, never promoted;
* sources are read from every real shape a Perplexity MCP result uses:
  ``structuredContent.results``, ``structuredContent.citations``, a top-level
  ``citations`` list, and a bare ``results`` list -- each entry either a mapping
  (``url``/``link``/``uri`` + ``title``/``name`` + ``snippet``/``text``) or a
  plain URL string;
* ``content`` ``text`` blocks become **synthesis**, never a retrieved source:
  synthesis without a cited URL is not a source;
* a JSON-RPC error, a closed stream, an ``isError`` tool result, a missing
  ``perplexity_search`` tool and a malformed payload each become a **typed
  non-success** :class:`~skillweave.adapters.SearchOutcome` with zero results.

No live provider is called by this module; it launches only the explicit command
it is handed, and never a floating install.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from skillweave.adapters import (
    CATEGORY_SEARCH,
    SEARCH_OPERATION,
    AdapterBackend,
    AdapterDescriptor,
    OperationBinding,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
)
from skillweave.adapters.outcomes import query_digest

from .launcher import LauncherResolutionError, resolve_launcher
from .mcp_client import (
    McpClientError,
    McpStdioClient,
    McpToolError,
    NegotiatedSession,
)

__all__ = [
    "COUNCIL_MCP_ADAPTER_ID",
    "PERPLEXITY_MCP_TOOL",
    "PERPLEXITY_PROVIDER",
    "PerplexityMcpBackend",
    "council_mcp_descriptor",
    "content_text_blocks",
    "normalize_records",
    "run_perplexity_mcp",
]

COUNCIL_MCP_ADAPTER_ID = "perplexity-search-mcp"
PERPLEXITY_PROVIDER = "perplexity"
PERPLEXITY_MCP_TOOL = "perplexity_search"

def _refused_launcher_detail(
    command: Sequence[str], *, resolved: bool = False
) -> Optional[str]:
    """A refusal reason when a *raw* command would install floating code.

    A command that carries a floating ``git+``/URL marker or an unpinned ``uvx``
    spec is refused by the same resolver the Council/Discovery entry points use.
    Returns ``None`` when the command is admissible.

    ``resolved`` marks a command the *launcher resolver itself produced* (its
    argument carries a ``launcher_kind``): that command was already validated at
    resolution time, so it is not re-judged here. Re-validating a resolved
    ``uvx --from name==version name`` command would misread its bare trailing
    distribution name as an unpinned spec and wrongly refuse a legitimate launch.
    """
    if resolved:
        return None
    try:
        resolve_launcher(explicit_command=list(command))
    except LauncherResolutionError as exc:
        asset = getattr(exc, "asset", None) or "explicit_command"
        return f"launcher refused ({asset}): {exc}"
    return None

_URL_KEYS = ("url", "link", "uri", "source_url")
_TITLE_KEYS = ("title", "name", "heading")
_SNIPPET_KEYS = ("snippet", "text", "description", "summary", "content")


def _is_http(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith("http://") or value.startswith("https://")
    )


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _record_from(entry: Any, provider: str) -> Optional[SearchResultRecord]:
    """One real source from one citation entry, or ``None`` (fail closed)."""
    if _is_http(entry):
        return SearchResultRecord(
            title="", url=str(entry), snippet="", source="web", provider=provider
        )
    if not isinstance(entry, Mapping):
        return None
    url = _first(entry, _URL_KEYS)
    if not _is_http(url):
        return None  # never promote a non-http(s) value to a source
    snippet = _first(entry, _SNIPPET_KEYS)[:500]
    return SearchResultRecord(
        title=_first(entry, _TITLE_KEYS),
        url=url,
        snippet=snippet,
        source="web",
        provider=provider,
    )


def content_text_blocks(result: Mapping[str, Any]) -> str:
    """Join the ``text`` blocks of a tool result (the model's synthesis)."""
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    texts = [
        str(b.get("text", ""))
        for b in blocks
        if isinstance(b, Mapping) and b.get("type") == "text" and b.get("text")
    ]
    return "\n".join(texts).strip()


def _iter_entries(result: Mapping[str, Any]) -> list[Any]:
    """Every citation-shaped entry a Perplexity MCP result may carry."""
    entries: list[Any] = []
    structured = result.get("structuredContent")
    structured = structured if isinstance(structured, Mapping) else {}
    for container in (result, structured):
        for key in ("results", "citations", "sources"):
            value = container.get(key)
            if isinstance(value, list):
                entries.extend(value)
    return entries


def normalize_records(
    result: Mapping[str, Any], *, provider: str = PERPLEXITY_PROVIDER
) -> list[SearchResultRecord]:
    """Normalize any supported Perplexity result shape to typed source records.

    Deduplicates by URL and drops every non-``http(s)`` value, so a bad citation
    can never be counted as a retrieved source.
    """
    seen: set[str] = set()
    records: list[SearchResultRecord] = []
    for entry in _iter_entries(result):
        record = _record_from(entry, provider)
        if record is None or record.url in seen:
            continue
        seen.add(record.url)
        records.append(record)
    return records


def run_perplexity_mcp(
    query: str,
    *,
    command: Optional[Sequence[str]],
    env_allowlist: Sequence[str] = (),
    extra_env: Optional[Mapping[str, str]] = None,
    tool_name: str = PERPLEXITY_MCP_TOOL,
    timeout: float = 30.0,
    protocol_version: str = "2025-06-18",
    secrets: Sequence[str] = (),
) -> tuple[SearchOutcome, Optional[NegotiatedSession]]:
    """Synchronous driver: call the owned MCP child and return the typed outcome.

    Returns ``(outcome, session)`` where ``session`` is the recorded
    ``initialize`` negotiation (``None`` when the child never started, e.g. no
    binding). The ``outcome`` is a fully typed :class:`SearchOutcome`: a
    non-success always carries **no** results and an ``error_code`` naming the
    blocking fact.

    ``secrets`` are extra exact values to redact from any server-originated
    text; the values actually forwarded to the child (allowlisted env +
    ``extra_env``) are redacted regardless, so a server that echoes its own
    credential can never leak it into the outcome.
    """
    sha = query_digest(query)
    if not command:
        return (
            _failure(sha, "no_binding", "no MCP launcher binding supplied for perplexity"),
            None,
        )
    # Defence in depth (criterion 2): a *raw* command that would install floating
    # code (a ``git+``/URL marker or an unpinned ``uvx`` spec) is refused here as
    # a typed ``no_binding`` before any process is spawned, rather than letting
    # the client raise on a floating launch. A resolved command (one the
    # launcher resolver produced) is supplied by the caller and is not re-judged.
    refusal = _refused_launcher_detail(command)
    if refusal is not None:
        return _failure(sha, "no_binding", refusal), None

    client = McpStdioClient(
        command,
        env_allowlist=env_allowlist,
        extra_env=extra_env,
        protocol_version=protocol_version,
        timeout=timeout,
        secrets=secrets,
    )
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_drive(client, query, sha, tool_name))
        finally:
            loop.close()
    raise RuntimeError(
        "run_perplexity_mcp is the synchronous driver; inside a running event "
        "loop use PerplexityMcpBackend.invoke (async) instead"
    )


async def _drive(
    client: McpStdioClient, query: str, sha: str, tool_name: str
) -> tuple[SearchOutcome, Optional[NegotiatedSession]]:
    """Drive one call and map every outcome to a typed, redacted ``SearchOutcome``.

    Status note: a child that never becomes usable -- an unspawnable command, a
    *cancelled* call, or a stream that closes during the handshake -- is reported
    with ``error_code="launch_failed"`` and, because that is an availability fact
    rather than a malformed payload, surfaces as ``status="unavailable"`` (see
    :func:`_failure`). ``_code`` names the exact blocking fact where one exists.
    """
    import asyncio

    try:
        await client.start()
    except McpClientError as exc:
        # A stream closed / handshake cancelled before the server was usable:
        # the launch never completed, so ``launch_failed`` (=> ``unavailable``).
        return _failure(sha, _code(exc, "launch_failed"), str(exc)), None
    except OSError as exc:  # a child that cannot be spawned
        return _failure(sha, "launch_failed", f"could not launch MCP child: {exc}"), None

    session = client.session
    try:
        names = await client.tool_names()
        if tool_name not in names:
            # A server controls its tool list; redact before it lands in
            # SearchOutcome.synthesis / the persisted evidence.
            return (
                _failure(
                    sha,
                    "tool_missing",
                    f"MCP server does not expose tool {tool_name!r}; "
                    f"tools={client.redact(str(names))}",
                ),
                session,
            )
        result = await client.call_tool(tool_name, {"query": query})
    except McpToolError as exc:
        return _failure(sha, "is_error", f"tool returned isError: {exc}"), session
    except McpClientError as exc:
        return _failure(sha, _code(exc, "mcp_error"), str(exc)), session
    except asyncio.TimeoutError:
        return _failure(sha, "timeout", "MCP call timed out"), session
    finally:
        await client.close()

    records = normalize_records(result)
    # A synthesis is server-originated text persisted as a receipt field; the
    # client-level redactor masks any forwarded credential it may echo.
    synthesis = client.redact(content_text_blocks(result)) or None
    if not records:
        outcome = _failure(
            sha,
            "no_results",
            "Perplexity returned no cited http(s) source",
        )
        # A synthesis with no cited URL is *not* a retrieved source; it is kept
        # only as a non-success synthesis, never promoted to a result.
        return _with_synthesis(outcome, synthesis), session
    return (
        SearchOutcome(
            query_sha256=sha,
            requested_provider=PERPLEXITY_PROVIDER,
            status="success",
            results=tuple(records),
            attempts=(SearchAttempt(PERPLEXITY_PROVIDER, "mcp-stdio", "success"),),
            synthesis=synthesis,
        ),
        session,
    )


def _code(exc: McpClientError, default: str) -> str:
    if isinstance(exc, McpToolError):
        return "is_error"
    if getattr(exc, "code", None) is not None:
        return f"jsonrpc_{exc.code}"
    return default


def _failure(sha: str, code: str, detail: str) -> SearchOutcome:
    status = "unavailable" if code in ("timeout", "no_results", "launch_failed") else "error"
    return SearchOutcome(
        query_sha256=sha,
        requested_provider=PERPLEXITY_PROVIDER,
        status=status,
        results=(),
        attempts=(SearchAttempt(PERPLEXITY_PROVIDER, "mcp-stdio", status, code),),
        error_code=code,
        synthesis=detail,
    )


def _with_synthesis(outcome: SearchOutcome, synthesis: Optional[str]) -> SearchOutcome:
    return SearchOutcome(
        query_sha256=outcome.query_sha256,
        requested_provider=outcome.requested_provider,
        status=outcome.status,
        results=(),
        attempts=outcome.attempts,
        synthesis=synthesis or outcome.synthesis,
        error_code=outcome.error_code,
    )


class PerplexityMcpBackend(AdapterBackend):
    """The Council-owned MCP backend for the ``perplexity-search-mcp`` adapter.

    Wraps :func:`run_perplexity_mcp` so an invocation through the shared facade
    returns a typed :class:`SearchOutcome`; the recorded negotiation is retained
    on :attr:`last_session` for a caller that wants to observe it.
    """

    def __init__(self, *, tool_name: str = PERPLEXITY_MCP_TOOL, timeout: float = 30.0) -> None:
        self._tool_name = tool_name
        self._timeout = timeout
        self.last_session: Optional[NegotiatedSession] = None

    async def invoke(
        self, operation: str, argument: Mapping[str, Any], context: Mapping[str, Any]
    ) -> SearchOutcome:
        query = str(argument.get("query", ""))
        sha = query_digest(query)
        if operation != SEARCH_OPERATION:
            return _failure(sha, "unsupported_operation", f"backend serves only {SEARCH_OPERATION!r}")
        if not query.strip():
            return _failure(sha, "empty_query", "search requires a non-empty query")

        command = argument.get("command")
        if not command:
            # No adapter-owned launcher was resolved (no explicit command, no
            # verified registered runtime, no installed binary, no uvx fallback).
            # This is a typed *non-success*, never a raised client error and
            # never a fabricated source.
            self.last_session = None
            return _failure(
                sha,
                "no_binding",
                "no MCP launcher binding supplied for perplexity",
            )
        # Defence in depth (criterion 2): whatever path supplied this command --
        # Council's own backend, or a Discovery adapter driving it directly --
        # a floating ``git+``/URL install or an unpinned ``uvx`` spec is refused
        # here, before any process is spawned, as a typed ``no_binding``. The
        # command the *resolver* produced (registered/installed/pinned-uvx) was
        # already validated at resolution time and is admissible, so a
        # legitimate launch is unaffected.
        refusal = _refused_launcher_detail(
            command, resolved=("launcher_kind" in argument)
        )
        if refusal is not None:
            self.last_session = None
            return _failure(sha, "no_binding", refusal)

        client = McpStdioClient(
            command,
            env_allowlist=argument.get("env_allowlist", ()),
            extra_env=argument.get("extra_env", {}),
            protocol_version=str(argument.get("protocol_version", "2025-06-18")),
            timeout=self._timeout,
            secrets=argument.get("secrets", ()),
        )
        outcome, session = await _drive(client, query, sha, self._tool_name)
        self.last_session = session
        return outcome


def council_mcp_descriptor() -> AdapterDescriptor:
    """The Council-owned descriptor for the ``perplexity-search-mcp`` adapter.

    Same id and axes as the substrate's binding, so provider resolution, the
    Discovery provider map and the facade are unaffected; only the backend is
    Council's.
    """
    return AdapterDescriptor(
        id=COUNCIL_MCP_ADAPTER_ID,
        version="1.0.0",
        categories=(CATEGORY_SEARCH,),
        operations=(
            OperationBinding(
                name=SEARCH_OPERATION,
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
                required_capabilities=("search.query",),
            ),
        ),
        transport="mcp-stdio",
        capabilities=("search.query",),
    )
