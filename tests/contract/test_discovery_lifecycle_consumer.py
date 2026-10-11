"""Discovery has a real, installed lifecycle consumer (SW-ADAPT-001, AC4/AC8).

``skillweave-discovery`` is a *prompt* skill: ``skills/skillweave-discovery/
SKILL.md`` is Markdown. A prompt pack does not, by itself, invoke a search
provider, so a claim that "Discovery is satisfied through Council" is not a
lifecycle binding. The only mechanism that runs code at a lifecycle phase
boundary is the Studio hook engine, so this suite binds Discovery to the *same*
extension registry Council uses -- via a ``type=python`` hook executed by the
**real** :func:`skillweave.studio.hooks.facade.run_hooks` path.

Each test materializes the source-owned binding into a throwaway project, adds
an external adapter factory the core has never heard of, and then drives the
real ``run_hooks("discovery", "pre", ...)`` call. No live provider is called:
the external fixture is ``in-process`` and synthetic, and the built-in provider
is refused by the egress policy before any call.

Counterexamples (each must fail closed, not fall through):
* a provider **outage** -> a typed non-success -> hook ``fail``;
* an **unknown** provider -> ``UnknownDiscoveryProvider`` -> hook ``fail``;
* a **malformed** outcome (backend returns the wrong type) -> hook ``fail``;
* an **unapproved** adapter (not allowlisted) -> policy refusal -> ``fail``;
* **egress denied** by default -> policy refusal -> ``fail``, no call made;
* a **missing credential** -> policy refusal before the call -> ``fail``.
"""

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "adapter" / "discovery"
for _p in (str(_SRC), str(_FIXTURES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skillweave.adapters import SearchOutcome, OutcomeError  # noqa: E402
from skillweave.discovery import (  # noqa: E402
    DiscoveryResearchHook,
    UnknownDiscoveryProvider,
    adapter_id_for,
    discovery_binding_dict,
    write_discovery_binding,
)
from skillweave.studio.hooks.facade import run_hooks  # noqa: E402
from skillweave.studio.hooks.models import Phase, PhaseContext, Position  # noqa: E402


def _run(project_root: Path, **binding_kwargs):
    write_discovery_binding(project_root, **binding_kwargs)
    return asyncio.run(
        run_hooks(
            "discovery",
            "pre",
            project_root=str(project_root),
            include_auto_discovered=False,
        )
    )


def _only(result):
    assert result is not None, "run_hooks returned None -- the binding did not load"
    assert len(result.results) == 1, result.to_dict()
    binding, hook_result = result.results[0]
    return binding, hook_result


# ── the consumer is bound and reaches the registry ─────────────────────────


class TestDiscoveryLifecycleConsumerIsWired:
    def test_binding_loads_and_the_hook_runs_through_the_real_engine(self, tmp_path):
        result = _run(
            tmp_path,
            provider="duckduckgo",
            query="problem statement research",
        )
        binding, hook_result = _only(result)
        assert binding.type == "python"
        assert binding.module.endswith("DiscoveryResearchHook")
        assert hook_result.status == "fail"  # egress denied by default, see below
        assert "refused by policy" in hook_result.message

    def test_external_adapter_factory_returns_a_typed_success(self, tmp_path):
        result = _run(
            tmp_path,
            adapter_id="external-discovery-search",
            adapter_factories=[
                "external_discovery_adapter:discovery_success_factory"
            ],
            query="problem statement research",
        )
        _, hook_result = _only(result)
        assert hook_result.status == "pass", hook_result.message
        assert "1 real source(s)" in hook_result.message
        # The typed record was persisted as hook evidence, with a receipt.
        assert hook_result.artifacts, "a pass must carry the typed evidence artifact"
        evidence = Path(hook_result.artifacts[0])
        assert evidence.exists()
        text = evidence.read_text()
        assert "\"status\": \"success\"" in text
        assert "https://external.example.org/discovery" in text
        # The receipt records the execution and carries no gate authority.
        assert "\"self_approving\": false" in text
        assert "\"domain_contract\": \"search-outcome/v1\"" in text
        assert "\"transport\": \"in-process\"" in text

    def test_hook_declines_phases_other_than_discovery_pre(self):
        hook = DiscoveryResearchHook()
        wrong_phase = PhaseContext(phase=Phase.BUILD, position=Position.PRE)
        wrong_position = PhaseContext(phase=Phase.DISCOVERY, position=Position.POST)
        assert hook.should_run(wrong_phase) is False
        assert hook.should_run(wrong_position) is False
        assert hook.should_run(
            PhaseContext(phase=Phase.DISCOVERY, position=Position.PRE)
        ) is True


# ── counterexamples: every failure fails closed ────────────────────────────


class TestDiscoveryConsumerCounterexamples:
    def test_provider_outage_is_a_typed_non_success_and_a_hook_fail(self, tmp_path):
        result = _run(
            tmp_path,
            adapter_id="external-discovery-outage",
            adapter_factories=[
                "external_discovery_adapter:discovery_outage_factory"
            ],
        )
        _, hook_result = _only(result)
        assert hook_result.status == "fail", hook_result.message
        assert "unavailable" in hook_result.message
        assert "provider_down" in hook_result.message

    def test_unknown_provider_fails_closed(self, tmp_path):
        result = _run(tmp_path, provider="not-a-provider")
        _, hook_result = _only(result)
        assert hook_result.status == "fail"
        assert "unknown provider" in hook_result.message

    def test_malformed_outcome_is_refused(self, tmp_path):
        result = _run(
            tmp_path,
            adapter_id="external-discovery-malformed",
            adapter_factories=[
                "external_discovery_adapter:discovery_malformed_factory"
            ],
        )
        _, hook_result = _only(result)
        assert hook_result.status == "fail"
        assert "malformed" in hook_result.message

    def test_egress_is_denied_by_default_with_no_call(self, tmp_path):
        result = _run(tmp_path, provider="tavily")
        _, hook_result = _only(result)
        assert hook_result.status == "fail"
        assert "refused by policy" in hook_result.message
        assert "egress" in hook_result.message

    def test_missing_credential_is_refused_before_the_call(self, tmp_path):
        result = _run(
            tmp_path,
            adapter_id="external-discovery-credentialed",
            adapter_factories=["external_discovery_adapter:credentialed_factory"],
            credential_availability={"DISCOVERY_SEARCH_KEY": "missing"},
        )
        _, hook_result = _only(result)
        assert hook_result.status == "fail"
        assert "refused by policy" in hook_result.message
        assert "credential:DISCOVERY_SEARCH_KEY" in hook_result.message


# ── unit-level guards for the seam itself ──────────────────────────────────


class TestSeamFailClosed:
    def test_unknown_provider_raises(self):
        with pytest.raises(UnknownDiscoveryProvider):
            adapter_id_for("bogus-provider")

    def test_unknown_external_adapter_id_is_refused_by_the_registry(self):
        from skillweave.adapters import UnknownAdapterError
        from skillweave.discovery import resolve_search_outcome

        with pytest.raises(UnknownAdapterError):
            asyncio.run(
                resolve_search_outcome("q", adapter_id="never-registered")
            )

    def test_malformed_search_outcome_payload_fails_closed(self):
        with pytest.raises(OutcomeError):
            SearchOutcome.from_dict(
                {
                    "query_sha256": "d" * 64,
                    "requested_provider": "x",
                    "status": "success",
                    "results": [{"url": "javascript:alert(1)"}],
                }
            )

    def test_binding_descriptor_is_cost_free_by_default(self):
        cfg = discovery_binding_dict()["hooks"][0]["config"]
        assert cfg["allow_network"] is False
        assert "api_key" not in cfg
