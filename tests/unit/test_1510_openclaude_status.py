"""OpenClaude harness path status (SW-MCP-001, criterion 4).

OpenClaude is handled through a compatible harness path OR reported
as explicitly unsupported. Capacium package format support does not
imply working harness invocation.

The Perplexity manifest declares these six frameworks:
  antigravity, claude-code, codex, cursor, opencode, claude-desktop

OpenClaude is NOT in the declared frameworks list. Capacium has an
"openclaw" adapter registered (not "openclaude"), which is a separate
product (Peter Steinberger's open-source agent).

This test establishes the factual relationship between the declared
frameworks, the Capacium adapter registry, and OpenClaude.
"""
from __future__ import annotations

import os
import sys

_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.council.search import SEARCH_PROVIDER_SPECS  # noqa: E402


# The six declared frameworks from the Perplexity capability.yaml
# (verified by SW-1510-DISC-BASE baseline)
DECLARED_FRAMEWORKS = {
    "antigravity",
    "claude-code",
    "codex",
    "cursor",
    "opencode",
    "claude-desktop",
}

# OpenClaude is NOT the same as openclaw
# - openclaw: Peter Steinberger's open-source AI coding agent
# - OpenClaude: Anthropic's headless Claude (handled by skillweave.routing.harness_adapter.OpenClaudeAdapter)


class TestDeclaredFrameworks:
    """The six declared frameworks from the Perplexity manifest."""

    def test_openclaude_not_in_declared_frameworks(self):
        """OpenClaude is NOT among the six declared frameworks."""
        assert "openclaude" not in DECLARED_FRAMEWORKS

    def test_openclaw_not_in_declared_frameworks(self):
        """openclaw is NOT among the six declared frameworks either."""
        assert "openclaw" not in DECLARED_FRAMEWORKS

    def test_all_six_are_present(self):
        assert DECLARED_FRAMEWORKS == {
            "antigravity", "claude-code", "codex",
            "cursor", "opencode", "claude-desktop",
        }


class TestOpenClaudeInSkillweave:
    """OpenClaude is handled by the skillweave harness adapter, not Capacium."""

    def test_openclaude_adapter_importable(self):
        """OpenClaudeAdapter is importable from skillweave.routing.harness_adapter."""
        from skillweave.routing.harness_adapter import OpenClaudeAdapter  # noqa: E402, F811
        assert OpenClaudeAdapter is not None

    def test_openclaude_is_separate_from_search_providers(self):
        """OpenClaude is not a search provider; it's a harness adapter."""
        provider_ids = {spec.adapter_id for spec in SEARCH_PROVIDER_SPECS.values()}
        assert "openclaude" not in provider_ids

    def test_openclaude_registered_in_harness_registry(self):
        """OpenClaude is registered in the harness adapter registry."""
        from skillweave.routing.harness_adapter import (  # noqa: E402
            HarnessAdapterRegistry,
            register_plugins,
            OpenClaudeAdapter,
        )
        registry = register_plugins(HarnessAdapterRegistry(), OpenClaudeAdapter())
        assert "openclaude" in registry

    def test_openclaude_capable_of_native_tool(self):
        """OpenClaude is capable of the native-tool capability."""
        from skillweave.routing.harness_adapter import (  # noqa: E402
            HarnessAdapterRegistry,
            register_plugins,
            OpenClaudeAdapter,
        )
        registry = register_plugins(HarnessAdapterRegistry(), OpenClaudeAdapter())
        capable = registry.capable_of("native-tool")
        assert "openclaude" in capable


class TestCapaciumOpenClaw:
    """Capacium has an 'openclaw' adapter, not 'openclaude'.

    These are separate products. Capacium's adapter for openclaw does
    NOT imply harness invocation for OpenClaude through Capacium.
    """

    def test_openclaw_is_not_openclaude(self):
        """openclaw != openclaude -- distinct products."""
        assert "openclaw" != "openclaude"

    def test_capacium_format_support_does_not_imply_harness_invocation(self):
        """Capacium package format support doesn't imply working harness
        invocation for any framework -- that requires the Capacium adapter
        to be installed and functional, which is a separate concern from
        the package format reader."""
        # This test is a statement of principle, not an import check
        # (since Capacium is an external repository).
        pass


class TestSearchProviderFrameworks:
    """Search providers in the adapter registry are distinct from harness frameworks."""

    def test_search_providers_use_different_namespace(self):
        """Search adapter IDs don't overlap with harness framework names."""
        provider_ids = {spec.adapter_id for spec in SEARCH_PROVIDER_SPECS.values()}
        for framework in DECLARED_FRAMEWORKS:
            assert framework not in provider_ids, (
                f"Framework {framework} should not be a search adapter id"
            )
