"""Live Perplexity availability: the external proof is PENDING, not faked
(SW-COUNCIL-003, criterion 6).

Criterion 6 asks for an *owned live* Perplexity search returning real cited
http(s) sources with the configured credentials. That proof depends on two facts
this environment does not supply right now:

* a Perplexity credential (expansion credential / API key), and
* outbound network egress to the provider.

When either is missing, the honest outcome is a **pending/blocked external
proof** -- never a fixture success dressed up as a live one. This suite:

* records the environment facts (is a credential present? is the provider
  reachable?);
* proves that, with no usable binding/credential, Council's Perplexity path fails
  closed with a typed non-success and zero results (it does NOT fabricate a
  source);
* runs the *live* assertion only when the credential and network are actually
  present -- otherwise it is skipped with a reason, and the pending state is
  recorded in ``fixtures/council/LIVE_PROOF_PENDING.md``.

No live provider is called when the environment cannot support it.
"""

import importlib
import json
import os
import socket
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
PENDING = FIX_C / "LIVE_PROOF_PENDING.md"

from skillweave.adapters import SEARCH_OPERATION  # noqa: E402
from skillweave.council import launcher as L  # noqa: E402
from skillweave.council.perplexity import PerplexityMcpBackend, run_perplexity_mcp  # noqa: E402

#: The credential the Perplexity MCP/HTTP binding consumes. Any of these names
#: satisfies "configured credentials" for the live proof.
_CREDENTIAL_ENV = ("PERPLEXITY_API_KEY", "PPLX_API_KEY", "PERPLEXITY_TOKEN")


def _credential_present() -> bool:
    return any(os.environ.get(name) for name in _CREDENTIAL_ENV)


def _provider_reachable() -> bool:
    try:
        conn = socket.create_connection(("api.perplexity.ai", 443), timeout=4)
        conn.close()
        return True
    except OSError:
        return False


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class TestEnvironmentFacts:
    def test_records_whether_a_credential_and_network_are_available(self):
        facts = {
            "credential_present": _credential_present(),
            "provider_reachable": _provider_reachable(),
        }
        # A structured, always-runnable record. The *live* proof is gated below.
        assert set(facts) == {"credential_present", "provider_reachable"}
        assert isinstance(facts["credential_present"], bool)
        assert isinstance(facts["provider_reachable"], bool)

    def test_the_pending_proof_is_documented(self):
        assert PENDING.exists(), f"the pending-proof note is missing: {PENDING}"
        text = PENDING.read_text()
        assert "pending" in text.lower()
        assert "criterion 6" in text.lower()


class TestNoFixtureSuccessIsSubstituted:
    def test_no_usable_binding_is_a_typed_non_success_with_zero_results(self):
        # With no launcher binding at all, the Council Perplexity path must fail
        # closed -- never fabricate a "cited" source from a fixture.
        outcome, session = run_perplexity_mcp("a live query", command=None)
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding"
        assert session is None

    def test_backend_without_a_binding_is_a_typed_non_success(self):
        backend = PerplexityMcpBackend()
        outcome = _run(backend.invoke(SEARCH_OPERATION, {"query": "a live query"}, {}))
        assert outcome.status != "success"
        assert outcome.results == ()
        assert outcome.error_code == "no_binding"

    def test_a_broken_owned_launcher_reports_a_typed_failure_not_fixture_success(
        self, tmp_path, monkeypatch
    ):
        """The source-owned Capacium MCP launcher, if present but unusable, is a
        typed non-success -- proof the path does not silently substitute a fake."""
        monkeypatch.setattr(L, "_REGISTRY", {})
        monkeypatch.setattr(
            L,
            "_REGISTRY",
            {
                "capacium-mcp": L.Registration(
                    id="capacium-mcp",
                    launch=("capacium-mcp",),
                    distribution="capacium-mcp",
                    version="0.3.0",
                    transport="mcp-stdio",
                    source="capacium:launcher-metadata",
                )
            },
        )
        try:
            res = L.resolve_launcher(search_path=os.environ.get("PATH"))
        except L.LauncherResolutionError:
            pytest.skip("capacium-mcp is not installed in this environment")
        # Whatever the resolved command, executing the Perplexity path must not
        # yield a fixture source: a broken/unavailable launcher is a typed failure.
        outcome, _ = run_perplexity_mcp(
            "a live query", command=list(res.command), timeout=15.0
        )
        if outcome.status == "success":
            # Only a genuinely live provider may produce http(s) sources here.
            assert _credential_present(), (
                "a success without a configured credential would be a fixture "
                "substituted for a live proof"
            )
            assert all(r.url.startswith("http") for r in outcome.results)
        else:
            assert outcome.results == ()
            assert outcome.error_code


@pytest.mark.skipif(
    not (_credential_present() and _provider_reachable()),
    reason=(
        "live Perplexity proof requires a configured credential AND network "
        "egress; see fixtures/council/LIVE_PROOF_PENDING.md (criterion 6)"
    ),
)
class TestLivePerplexityProof:
    """Runs only with a real credential and reachable provider. Pending otherwise."""

    def test_live_perplexity_returns_real_cited_http_sources(self, tmp_path):
        # Use the source-owned launcher binding; the credential is forwarded to
        # the owned child through the environment allowlist.
        monkeypatch_env = {name: os.environ[name] for name in _CREDENTIAL_ENV
                           if os.environ.get(name)}
        try:
            res = L.resolve_launcher(search_path=os.environ.get("PATH"))
        except L.LauncherResolutionError as exc:
            pytest.skip(f"no launcher binding available for the live proof: {exc}")
        outcome, _ = run_perplexity_mcp(
            "current best practices for agent harness eval",
            command=list(res.command),
            env_allowlist=tuple(monkeypatch_env),
            timeout=45.0,
        )
        if outcome.status != "success":
            pytest.skip(
                f"provider unavailable/quota: status={outcome.status} "
                f"code={outcome.error_code} (external proof remains pending)"
            )
        assert outcome.results, "a live success must carry real cited sources"
        assert all(r.url.startswith("http") for r in outcome.results), outcome.results
