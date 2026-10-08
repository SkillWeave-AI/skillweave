"""Council's Perplexity MCP result normalization (SW-COUNCIL-003, criterion 3).

The addendum's defect was in the *consumer*, not the provider: the shape of a
Perplexity MCP result was not normalized, a ``text``-only synthesis could be
mistaken for a source, and ``isError``/missing-tool/JSON-RPC-error/malformed
payloads were not typed failures. This suite pins Council's repair:

* the ``content`` ``text`` blocks are read as **synthesis**, never a source;
* real sources are read from every shape a Perplexity MCP result uses --
  ``structuredContent.results``, ``structuredContent.citations``, a top-level
  ``citations`` list, and a bare ``results`` list -- each entry either a mapping
  (``url``/``link``/``uri`` + title + snippet) or a plain URL string;
* only ``http(s)`` URLs become sources (a ``javascript:``/``ftp:`` citation is
  dropped, never promoted), and duplicates are collapsed;
* a JSON-RPC error, a closed stream, an ``isError`` tool result, a missing
  search tool, a malformed payload and a synthesis-without-citation each become
  a **typed non-success** with zero results.

No live provider is called: every case drives the hand-rolled fake server.
"""

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
FAKE = FIX_C / "fake_mcp_server.py"

from skillweave.council.perplexity import (  # noqa: E402
    COUNCIL_MCP_ADAPTER_ID,
    PERPLEXITY_MCP_TOOL,
    PERPLEXITY_PROVIDER,
    PerplexityMcpBackend,
    content_text_blocks,
    council_mcp_descriptor,
    normalize_records,
    run_perplexity_mcp,
)
from skillweave.adapters import SEARCH_OPERATION  # noqa: E402


def _fake(mode: str) -> list[str]:
    return [sys.executable, str(FAKE), mode]


# ── pure normalization: every real citation shape ──────────────────────────


class TestNormalizationShapes:
    def test_structured_content_results_and_citations(self):
        result = {
            "structuredContent": {
                "results": [
                    {"title": "A", "url": "https://a.example.org/x", "snippet": "s"},
                ],
                "citations": ["https://b.example.net/y"],
            }
        }
        records = normalize_records(result)
        assert [r.url for r in records] == [
            "https://a.example.org/x",
            "https://b.example.net/y",
        ]
        assert records[0].title == "A"
        assert records[1].title == ""

    def test_top_level_citations_and_results(self):
        result = {
            "results": [{"link": "https://r.example.org/1", "name": "R"}],
            "citations": [{"uri": "https://c.example.org/2", "heading": "C"}],
        }
        records = normalize_records(result)
        assert [r.url for r in records] == [
            "https://r.example.org/1",
            "https://c.example.org/2",
        ]
        assert {r.title for r in records} == {"R", "C"}

    def test_sources_key_is_read_too(self):
        result = {"structuredContent": {"sources": ["https://s.example.org/z"]}}
        records = normalize_records(result)
        assert [r.url for r in records] == ["https://s.example.org/z"]

    def test_non_http_schemes_are_never_promoted(self):
        result = {
            "results": [
                {"url": "javascript:alert(1)"},
                {"url": "ftp://files.example.org/a"},
                {"url": "data:text/plain,hi"},
                {"url": "https://ok.example.org/fine"},
            ]
        }
        records = normalize_records(result)
        assert [r.url for r in records] == ["https://ok.example.org/fine"]

    def test_duplicate_urls_are_collapsed(self):
        result = {
            "results": [{"url": "https://dupe.example.org/a"}],
            "citations": ["https://dupe.example.org/a"],
        }
        assert len(normalize_records(result)) == 1

    def test_every_record_carries_the_provider_and_web_source(self):
        records = normalize_records({"results": ["https://x.example.org/1"]})
        assert records[0].provider == PERPLEXITY_PROVIDER
        assert records[0].source == "web"

    def test_non_container_payload_yields_no_sources(self):
        assert normalize_records({"content": [{"type": "text", "text": "hi"}]}) == []


# ── content text blocks are synthesis, not a source ────────────────────────


class TestSynthesisIsNotASource:
    def test_content_text_blocks_are_joined(self):
        result = {
            "content": [
                {"type": "text", "text": "first"},
                {"type": "image", "data": "..."},
                {"type": "text", "text": "second"},
            ]
        }
        assert content_text_blocks(result) == "first\nsecond"

    def test_a_text_only_payload_has_synthesis_and_no_source(self):
        result = {"content": [{"type": "text", "text": "an answer with no citation"}]}
        assert content_text_blocks(result) == "an answer with no citation"
        assert normalize_records(result) == []


# ── the driver: typed outcomes for every server mode ───────────────────────


def _drive(mode: str, *, timeout: float = 30.0):
    return run_perplexity_mcp("a query", command=_fake(mode), timeout=timeout)


class TestDriverOutcomes:
    def test_normal_mode_is_a_success_with_real_sources_and_synthesis(self):
        outcome, session = _drive("normal")
        assert outcome.status == "success"
        assert outcome.requested_provider == PERPLEXITY_PROVIDER
        urls = [r.url for r in outcome.results]
        assert "https://example.org/cited" in urls
        assert "https://citation.example.net/a" in urls
        # The javascript: citation the server returns is dropped.
        assert all(u.startswith("http") for u in urls), urls
        assert outcome.attempts[0].transport == "mcp-stdio"
        assert outcome.synthesis == "synthesized answer"
        assert session is not None

    def test_no_binding_is_a_typed_non_success_with_no_results(self):
        outcome, session = run_perplexity_mcp("q", command=None)
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding"
        assert session is None

    def test_a_closed_stream_is_a_typed_non_success(self):
        outcome, _ = _drive("unavailable")
        assert outcome.status == "unavailable"
        assert outcome.results == ()
        assert outcome.error_code == "launch_failed"

    def test_a_jsonrpc_error_is_a_typed_non_success(self):
        outcome, _ = _drive("error")
        assert outcome.status == "error"
        assert outcome.results == ()
        assert outcome.error_code == "jsonrpc_-32603"

    def test_a_missing_search_tool_is_a_typed_non_success(self):
        outcome, _ = _drive("missing-tool")
        assert outcome.status == "error"
        assert outcome.results == ()
        assert outcome.error_code == "tool_missing"

    def test_an_iserror_tool_result_is_a_typed_non_success(self):
        outcome, _ = _drive("iserror")
        assert outcome.status == "error"
        assert outcome.results == ()
        assert outcome.error_code == "is_error"

    def test_a_malformed_payload_is_a_typed_non_success(self):
        outcome, _ = _drive("malformed", timeout=0.6)
        assert outcome.status == "error"
        assert outcome.results == ()
        assert outcome.error_code, outcome

    def test_synthesis_without_a_cited_url_is_not_a_retrieved_source(self):
        outcome, _ = _drive("text-only")
        assert outcome.status == "unavailable"
        assert outcome.error_code == "no_results"
        assert outcome.results == (), "a synthesis is never promoted to a source"
        # The synthesis is retained as a non-success note, not as a result.
        assert outcome.synthesis == "A synthesized answer with no citation."


# ── the Council-owned adapter binding ──────────────────────────────────────


class TestCouncilAdapterBinding:
    def test_descriptor_keeps_the_substrate_id_and_axes(self):
        desc = council_mcp_descriptor()
        assert desc.id == COUNCIL_MCP_ADAPTER_ID == "perplexity-search-mcp"
        assert desc.transport == "mcp-stdio"
        assert desc.capabilities == ("search.query",)
        binding = desc.operation(SEARCH_OPERATION)
        assert binding.result_contract == "search-outcome/v1"

    def test_backend_returns_a_typed_outcome_for_a_real_server(self):
        backend = PerplexityMcpBackend()

        async def run():
            return await backend.invoke(
                SEARCH_OPERATION, {"query": "hello", "command": _fake("normal")}, {}
            )

        outcome = asyncio.run(run())
        assert outcome.status == "success"
        assert outcome.results
        assert backend.last_session is not None
        assert backend.last_session.server_info["name"] == "council-fake"

    def test_backend_without_a_command_is_a_typed_no_binding(self):
        backend = PerplexityMcpBackend()

        async def run():
            return await backend.invoke(SEARCH_OPERATION, {"query": "hello"}, {})

        outcome = asyncio.run(run())
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding"

    def test_backend_rejects_a_non_search_operation_and_empty_query(self):
        backend = PerplexityMcpBackend()

        async def run(op, arg):
            return await backend.invoke(op, arg, {})

        bad_op = asyncio.run(run("tool.echo", {"query": "x"}))
        assert bad_op.error_code == "unsupported_operation"
        empty = asyncio.run(run(SEARCH_OPERATION, {"query": "   "}))
        assert empty.error_code == "empty_query"
