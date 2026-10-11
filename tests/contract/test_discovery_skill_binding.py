"""The Markdown Discovery skill *declares* its lifecycle consumer (SW-ADAPT-001).

``skills/skillweave-discovery/SKILL.md`` is a prompt pack: Markdown a host runs.
On its own it invokes no provider -- a claim that "Discovery is satisfied through
Council" is not a lifecycle binding, and neither is Markdown that merely
*describes* research. The criterion is that the skill's actual, declared
lifecycle boundary is **bound, in data, to the registry-backed typed-search
consumer** so that the facade the consumer runs is the real
:class:`~skillweave.discovery.lifecycle_hook.DiscoveryResearchHook`.

This suite closes that last gap. The skill manifest
``skills/skillweave-discovery/capability.yaml`` carries a ``studio_binding``
block naming that hook at ``discovery/pre``. This suite proves:

1. the *shipped* skill manifest really declares the source-owned hook (so the
   declaration cannot drift from the code it names);
2. reading the declaration yields the canonical binding descriptor, and
   materializing it writes a ``.skillweave/hooks/discovery-pre.yaml`` the Studio
   hook engine actually loads -- driven through the **real** ``run_hooks`` path;
3. on that materialized binding, a registered external adapter (no core edit)
   yields a typed success + receipt, while the built-in provider fails closed
   (egress denied by default) with **no network call**;
4. the outage / unknown / malformed counterexamples still fail closed *through
   the skill declaration*;
5. the declaration is data with a closed, validated shape: a wrong phase,
   a wrong module, an unknown config key or a smuggled credential value is
   refused, and the block embeds no secret and survives Capacium manifest sync.

No live provider is called.
"""

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "adapter" / "discovery"
for _p in (str(_SRC), str(_FIXTURES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skillweave.adapters import AdapterError  # noqa: E402
from skillweave.discovery import (  # noqa: E402
    DISCOVERY_BINDING_PATH,
    DISCOVERY_HOOK_MODULE,
    SKILL_BINDING_KEY,
    binding_from_skill_manifest,
    discovery_binding_dict,
    materialize_skill_binding,
)
from skillweave.studio.hooks.facade import run_hooks  # noqa: E402

SKILL_MANIFEST = _ROOT / "skills" / "skillweave-discovery" / "capability.yaml"
SKILL_MD = _ROOT / "skills" / "skillweave-discovery" / "SKILL.md"


def _manifest() -> dict:
    return yaml.safe_load(SKILL_MANIFEST.read_text())


def _with_config(config: dict) -> dict:
    """The shipped manifest with a binding-config override (counterexample rig)."""
    manifest = _manifest()
    manifest[SKILL_BINDING_KEY] = {
        **manifest[SKILL_BINDING_KEY],
        "config": config,
    }
    return manifest


def _run_materialized(project_root: Path, manifest: dict):
    materialize_skill_binding(manifest, project_root)
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
    return result.results[0]


# ── the shipped skill really declares the source-owned consumer ─────────────


class TestShippedSkillDeclaresItsConsumer:
    def test_manifest_declares_a_studio_binding(self):
        manifest = _manifest()
        assert SKILL_BINDING_KEY in manifest, manifest.keys()

    def test_declaration_names_the_source_owned_hook_at_discovery_pre(self):
        block = _manifest()[SKILL_BINDING_KEY]
        assert block["phase"] == "discovery"
        assert block["position"] == "pre"
        assert block["module"] == DISCOVERY_HOOK_MODULE
        # The named hook is importable from the shipped package, not invented.
        from skillweave.discovery.lifecycle_hook import DiscoveryResearchHook

        assert DiscoveryResearchHook.__module__ + "." + DiscoveryResearchHook.__name__ == (
            DISCOVERY_HOOK_MODULE
        )

    def test_the_declaration_binds_the_markdown_facades_research_boundary(self):
        """The SKILL.md Phase 2 is 'Research' -- the boundary the binding fills."""
        text = SKILL_MD.read_text()
        assert "Phase 2: Research" in text

    def test_declaration_matches_the_canonical_binding_descriptor(self):
        payload = binding_from_skill_manifest(_manifest())
        assert payload == discovery_binding_dict()

    def test_declaration_embeds_no_credential_value_and_denies_egress(self):
        block = _manifest()[SKILL_BINDING_KEY]
        config = block.get("config", {})
        assert config.get("allow_network", False) is False
        assert "api_key" not in config
        serialized = SKILL_MANIFEST.read_text()
        for token in ("sk-", "xox", "ghp_", "glpat-", "Bearer "):
            assert token not in serialized


# ── the declaration drives the real engine on the installed seam ────────────


class TestSkillBindingDrivesTheRealEngine:
    def test_materialized_binding_loads_and_runs_through_the_real_engine(self, tmp_path):
        result = _run_materialized(tmp_path, _manifest())
        binding, hook_result = _only(result)
        assert binding.type == "python"
        assert binding.module == DISCOVERY_HOOK_MODULE
        # Default duckduckgo is refused by the egress policy *before any call*.
        assert hook_result.status == "fail", hook_result.message
        assert "refused by policy" in hook_result.message
        assert "egress" in hook_result.message

    def test_materialized_path_is_the_engine_owned_location(self, tmp_path):
        target = materialize_skill_binding(_manifest(), tmp_path)
        assert target == tmp_path / DISCOVERY_BINDING_PATH
        assert target.exists()

    def test_declared_binding_carries_an_external_adapter_to_a_typed_success(
        self, tmp_path
    ):
        manifest = _with_config(
            {
                "provider": "duckduckgo",
                "adapter_id": "external-discovery-search",
                "adapter_factories": [
                    "external_discovery_adapter:discovery_success_factory"
                ],
                "query": "problem statement research",
            }
        )
        _, hook_result = _only(_run_materialized(tmp_path, manifest))
        assert hook_result.status == "pass", hook_result.message
        assert hook_result.artifacts, "a pass must carry typed evidence"
        text = Path(hook_result.artifacts[0]).read_text()
        assert '"status": "success"' in text
        assert '"domain_contract": "search-outcome/v1"' in text
        assert '"transport": "in-process"' in text
        assert '"self_approving": false' in text


# ── counterexamples fail closed through the skill declaration ───────────────


class TestSkillBindingCounterexamplesFailClosed:
    def test_provider_outage_is_a_hook_fail(self, tmp_path):
        manifest = _with_config(
            {
                "adapter_id": "external-discovery-outage",
                "adapter_factories": [
                    "external_discovery_adapter:discovery_outage_factory"
                ],
            }
        )
        _, hook_result = _only(_run_materialized(tmp_path, manifest))
        assert hook_result.status == "fail", hook_result.message
        assert "unavailable" in hook_result.message
        assert "provider_down" in hook_result.message

    def test_unknown_provider_is_a_hook_fail_with_no_fallback(self, tmp_path):
        manifest = _with_config({"provider": "not-a-provider"})
        _, hook_result = _only(_run_materialized(tmp_path, manifest))
        assert hook_result.status == "fail"
        assert "unknown provider" in hook_result.message

    def test_malformed_outcome_is_refused(self, tmp_path):
        manifest = _with_config(
            {
                "adapter_id": "external-discovery-malformed",
                "adapter_factories": [
                    "external_discovery_adapter:discovery_malformed_factory"
                ],
            }
        )
        _, hook_result = _only(_run_materialized(tmp_path, manifest))
        assert hook_result.status == "fail"
        assert "malformed" in hook_result.message


# ── the declaration is closed, validated data -- never a script ─────────────


class TestSkillBindingDeclarationIsClosedData:
    def test_missing_block_is_refused(self):
        with pytest.raises(AdapterError) as exc:
            binding_from_skill_manifest({"name": "skillweave-discovery"})
        assert exc.value.asset == SKILL_BINDING_KEY

    def test_non_mapping_block_is_refused(self):
        with pytest.raises(AdapterError):
            binding_from_skill_manifest({SKILL_BINDING_KEY: ["not", "a", "map"]})

    def test_wrong_phase_is_refused(self):
        block = {**_manifest()[SKILL_BINDING_KEY], "phase": "build"}
        with pytest.raises(AdapterError) as exc:
            binding_from_skill_manifest({SKILL_BINDING_KEY: block})
        assert exc.value.asset == f"{SKILL_BINDING_KEY}.phase"

    def test_wrong_position_is_refused(self):
        block = {**_manifest()[SKILL_BINDING_KEY], "position": "post"}
        with pytest.raises(AdapterError) as exc:
            binding_from_skill_manifest({SKILL_BINDING_KEY: block})
        assert exc.value.asset == f"{SKILL_BINDING_KEY}.position"

    def test_wrong_module_is_refused(self):
        block = {
            **_manifest()[SKILL_BINDING_KEY],
            "module": "os.system",
        }
        with pytest.raises(AdapterError) as exc:
            binding_from_skill_manifest({SKILL_BINDING_KEY: block})
        assert exc.value.asset == f"{SKILL_BINDING_KEY}.module"

    def test_unknown_config_key_is_refused(self):
        block = {
            **_manifest()[SKILL_BINDING_KEY],
            "config": {"provider": "duckduckgo", "allow_network": True, "typo_key": 1},
        }
        with pytest.raises(AdapterError) as exc:
            binding_from_skill_manifest({SKILL_BINDING_KEY: block})
        assert exc.value.asset == f"{SKILL_BINDING_KEY}.config"

    def test_smuggled_credential_value_is_refused(self):
        block = {
            **_manifest()[SKILL_BINDING_KEY],
            "config": {"provider": "duckduckgo", "api_key": "sk-live-abc123"},
        }
        with pytest.raises(AdapterError) as exc:
            binding_from_skill_manifest({SKILL_BINDING_KEY: block})
        assert "credential value" in str(exc.value)

    def test_declaration_survives_capacium_manifest_sync(self):
        """The extra key round-trips: it must not read as a manifest drift."""
        from skillweave.github_integration.capability_sync import CapaciumManifestSync

        syncer = CapaciumManifestSync(repo_root=str(_ROOT))
        expected = syncer.normalize_manifest(SKILL_MANIFEST, "1.5.9")
        assert expected.get(SKILL_BINDING_KEY) == _manifest()[SKILL_BINDING_KEY]
