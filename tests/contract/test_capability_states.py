"""Tools/list and tools/call fixture demonstrations (SW-MCP-001, criterion 3).

Four MCP capability states are demonstrated through fixtures:
  * authenticated      - valid credentials, server exposes tools
  * anonymous/ask-only - server is reachable but requires auth
  * missing/expired    - credentials are absent or expired
  * unavailable        - server/tool is not reachable

Each state maps to a typed non-success or success SearchOutcome with
zero results for non-success, and records the credential availability
in the attempt metadata. No live provider or network is called.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.adapters.outcomes import (  # noqa: E402
    SearchOutcome,
    SearchAttempt,
    SearchResultRecord,
    CredentialReference,
    AVAILABILITY_STATES,
    INJECTION_OWNED_CHILD_ENVIRONMENT,
    query_digest,
)
from skillweave.adapters.descriptor import (  # noqa: E402
    AdapterDescriptor,
    AdapterDescriptorError,
    OperationBinding,
)
from skillweave.adapters.policy import (  # noqa: E402
    EffectivePolicy,
    EgressPolicy,
    AdmissionDecision,
    admit_invocation,
)
from skillweave.council.credential import (  # noqa: E402
    CredentialResolver,
    CredentialResolution,
)


# ── Authenticated state ──────────────────────────────────────────────────────

def test_authenticated_search_outcome_has_sources():
    """A success with valid credentials carries at least one real http(s) source."""
    outcome = SearchOutcome(
        query_sha256=query_digest("latest AI research 2026"),
        requested_provider="perplexity",
        status="success",
        results=(
            SearchResultRecord(
                title="AI Advances",
                url="https://example.org/ai-advances",
                snippet="Recent advances in AI research",
                source="web",
                provider="perplexity",
            ),
        ),
        attempts=(
            SearchAttempt(
                provider="perplexity",
                transport="mcp-stdio",
                status="success",
            ),
        ),
    )
    assert outcome.succeeded is True
    assert len(outcome.results) == 1
    assert outcome.results[0].url.startswith("https://")


def test_authenticated_credential_reference_is_available():
    """An authenticated adapter has its credential reference marked available."""
    ref = CredentialReference(
        reference_id="PERPLEXITY_COOKIES",
        source="env",
        variable_names=("PERPLEXITY_COOKIES",),
        availability="available",
    )
    assert ref.available is True
    assert ref.injection == INJECTION_OWNED_CHILD_ENVIRONMENT
    assert ref.persistent_values is False


# ── Anonymous / ask-only state ──────────────────────────────────────────────

def test_anonymous_ask_only_is_non_success():
    """An anonymous/ask-only state returns a blocked outcome, never success."""
    outcome = SearchOutcome(
        query_sha256=query_digest("test query"),
        requested_provider="perplexity",
        status="blocked",
        results=(),
        attempts=(
            SearchAttempt(
                provider="perplexity",
                transport="mcp-stdio",
                status="blocked",
                error_code="auth_required",
            ),
        ),
        error_code="auth_required",
    )
    assert outcome.succeeded is False
    assert len(outcome.results) == 0
    assert outcome.error_code == "auth_required"


def test_anonymous_ask_only_credential_is_missing():
    """An anonymous adapter has its credential marked missing."""
    ref = CredentialReference(
        reference_id="PERPLEXITY_COOKIES",
        source="env",
        variable_names=("PERPLEXITY_COOKIES",),
        availability="missing",
    )
    assert ref.available is False
    assert ref.availability == "missing"


# ── Missing / expired credential state ──────────────────────────────────────

def test_missing_credential_is_refused_by_policy():
    """A missing credential reference is refused by the admission gate."""
    descriptor = AdapterDescriptor(
        id="perplexity-search-mcp",
        version="1.0.0",
        categories=("search",),
        operations=(
            OperationBinding(
                name="search.query",
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
            ),
        ),
        transport="mcp-stdio",
        credential_refs=("PERPLEXITY_COOKIES",),
    )
    policy = EffectivePolicy(
        allowed_adapter_ids=("perplexity-search-mcp",),
        require_credential_availability=True,
    )
    decision = admit_invocation(
        policy,
        descriptor,
        category="search",
        operation="search.query",
        credential_availability={"PERPLEXITY_COOKIES": "missing"},
    )
    assert decision.admitted is False
    assert "credential" in decision.asset or decision.asset == "credential:PERPLEXITY_COOKIES"


def test_missing_credential_outcome_has_no_sources():
    """A missing-credential outcome is typed as blocked/error with zero results."""
    outcome = SearchOutcome(
        query_sha256=query_digest("test query"),
        requested_provider="perplexity",
        status="blocked",
        results=(),
        attempts=(
            SearchAttempt(
                provider="perplexity",
                transport="mcp-stdio",
                status="blocked",
                error_code="no_credential",
            ),
        ),
        error_code="no_credential",
    )
    assert outcome.succeeded is False
    assert len(outcome.results) == 0


def test_expired_credential_outcome_is_error():
    """An expired credential is typed as an error with zero results."""
    outcome = SearchOutcome(
        query_sha256=query_digest("test query"),
        requested_provider="serper",
        status="error",
        results=(),
        attempts=(
            SearchAttempt(
                provider="serper",
                transport="http",
                status="error",
                error_code="http_401",
            ),
        ),
        error_code="http_401",
    )
    assert outcome.succeeded is False
    assert len(outcome.results) == 0
    assert outcome.error_code == "http_401"


# ── Unavailable tool state ──────────────────────────────────────────────────

def test_unavailable_tool_outcome():
    """An unavailable tool is typed as unavailable with zero results."""
    outcome = SearchOutcome(
        query_sha256=query_digest("test query"),
        requested_provider="perplexity",
        status="unavailable",
        results=(),
        attempts=(
            SearchAttempt(
                provider="perplexity",
                transport="mcp-stdio",
                status="unavailable",
                error_code="tool_missing",
            ),
        ),
        error_code="tool_missing",
    )
    assert outcome.succeeded is False
    assert outcome.status == "unavailable"


def test_unavailable_provider_outcome():
    """An unreachable provider is typed as unavailable."""
    outcome = SearchOutcome(
        query_sha256=query_digest("test query"),
        requested_provider="tavily",
        status="unavailable",
        results=(),
        attempts=(
            SearchAttempt(
                provider="tavily",
                transport="http",
                status="unavailable",
                error_code="network_error",
            ),
        ),
        error_code="network_error",
    )
    assert outcome.succeeded is False
    assert outcome.status == "unavailable"


# ── Policy gate for each state ──────────────────────────────────────────────

def test_policy_admits_authenticated_invocation():
    """An authenticated adapter with available credential passes the gate."""
    descriptor = AdapterDescriptor(
        id="serper-search",
        version="1.0.0",
        categories=("search",),
        operations=(
            OperationBinding(
                name="search.query",
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
            ),
        ),
        transport="http",
        capabilities=("search.query", "network-egress"),
        credential_refs=("SERPER_API_KEY",),
    )
    policy = EffectivePolicy(
        allowed_adapter_ids=("serper-search",),
        egress=EgressPolicy(allow_network=True, endpoint_allowlist=("google.serper.dev",)),
        require_credential_availability=True,
    )
    decision = admit_invocation(
        policy,
        descriptor,
        category="search",
        operation="search.query",
        credential_availability={"SERPER_API_KEY": "available"},
        endpoint="https://google.serper.dev/search",
    )
    assert decision.admitted is True


def test_policy_refuses_not_allowlisted_adapter():
    """An adapter not on the allowlist is refused before any credential check."""
    descriptor = AdapterDescriptor(
        id="unknown-search",
        version="1.0.0",
        categories=("search",),
        operations=(
            OperationBinding(
                name="search.query",
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
            ),
        ),
        transport="http",
    )
    policy = EffectivePolicy(allowed_adapter_ids=("known-adapter",))
    decision = admit_invocation(
        policy, descriptor, category="search", operation="search.query",
    )
    assert decision.admitted is False
    assert decision.asset == "allowlist"


def test_policy_refuses_missing_capability():
    """An adapter lacking a required capability is refused."""
    descriptor = AdapterDescriptor(
        id="no-egress-search",
        version="1.0.0",
        categories=("search",),
        operations=(
            OperationBinding(
                name="search.query",
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
            ),
        ),
        transport="http",
        capabilities=("search.query",),
    )
    policy = EffectivePolicy(
        allowed_adapter_ids=("no-egress-search",),
        egress=EgressPolicy(allow_network=True),
    )
    decision = admit_invocation(
        policy,
        descriptor,
        category="search",
        operation="search.query",
        required_capabilities=("network-egress",),
    )
    assert decision.admitted is False
    assert "capability" in decision.asset


# ── CredentialReference invariants ──────────────────────────────────────────

def test_credential_reference_never_persists_value():
    """persistent_values must be False."""
    ref = CredentialReference(
        reference_id="PERPLEXITY_API_KEY",
        source="env",
        variable_names=("PERPLEXITY_API_KEY",),
        availability="missing",
    )
    assert ref.persistent_values is False


def test_credential_reference_rejects_persistent_values():
    """Construction with persistent_values=True must raise."""
    import re

    with pytest.raises(Exception):
        CredentialReference(
            reference_id="PERPLEXITY_API_KEY",
            source="env",
            variable_names=("PERPLEXITY_API_KEY",),
            availability="available",
            persistent_values=True,
        )


def test_credential_reference_injection_is_owned_child():
    """injection must be owned_child_environment_only."""
    ref = CredentialReference(
        reference_id="PERPLEXITY_COOKIES",
        source="env",
        variable_names=("PERPLEXITY_COOKIES",),
        availability="available",
    )
    assert ref.injection == INJECTION_OWNED_CHILD_ENVIRONMENT


# ── Serialisation ───────────────────────────────────────────────────────────

def test_all_four_states_serialise_to_consistent_json():
    """Each state serialises with the correct shape."""
    states = {
        "authenticated": SearchOutcome(
            query_sha256=query_digest("q1"),
            requested_provider="perplexity",
            status="success",
            results=(
                SearchResultRecord(
                    title="R1", url="https://example.org/1",
                    snippet="S1", source="web", provider="perplexity",
                ),
            ),
            attempts=(SearchAttempt("perplexity", "mcp-stdio", "success"),),
        ),
        "anonymous": SearchOutcome(
            query_sha256=query_digest("q2"),
            requested_provider="perplexity",
            status="blocked",
            results=(),
            attempts=(
                SearchAttempt("perplexity", "mcp-stdio", "blocked", "auth_required"),
            ),
            error_code="auth_required",
        ),
        "missing_credential": SearchOutcome(
            query_sha256=query_digest("q3"),
            requested_provider="perplexity",
            status="blocked",
            results=(),
            attempts=(
                SearchAttempt("perplexity", "mcp-stdio", "blocked", "no_credential"),
            ),
            error_code="no_credential",
        ),
        "unavailable": SearchOutcome(
            query_sha256=query_digest("q4"),
            requested_provider="perplexity",
            status="unavailable",
            results=(),
            attempts=(
                SearchAttempt("perplexity", "mcp-stdio", "unavailable", "tool_missing"),
            ),
            error_code="tool_missing",
        ),
    }

    for name, outcome in states.items():
        d = outcome.to_dict()
        assert "query_sha256" in d
        assert "status" in d
        assert "results" in d
        assert "attempts" in d
        if name == "authenticated":
            assert len(d["results"]) > 0
            assert d["status"] == "success"
        else:
            assert len(d["results"]) == 0
            assert d["status"] != "success"


# ── Descriptor credential_refs validation ───────────────────────────────────

def test_descriptor_credential_ref_must_be_identifier():
    """credential_refs must be plain identifiers, not secret values."""
    with pytest.raises(AdapterDescriptorError) as exc:
        AdapterDescriptor(
            id="test-adapter",
            version="1.0.0",
            categories=("search",),
            operations=(
                OperationBinding(
                    name="search.query",
                    input_contract="search-query/v1",
                    result_contract="search-outcome/v1",
                ),
            ),
            transport="mcp-stdio",
            credential_refs=("sk-live-abc123",),  # looks like a secret value
        )
    assert "credential reference" in str(exc.value)
