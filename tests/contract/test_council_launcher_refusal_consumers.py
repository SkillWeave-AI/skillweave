"""The criterion-2 launcher refusal at the *consumer* boundary (SW-COUNCIL-003).

The controller's criterion-2 finding: ``WebSearch._backend_argument`` set
``argument['command'] = config.mcp_command`` **before** ``resolve_launcher``, and
its ``except LauncherResolutionError`` branch returned the argument with
``command`` still set -- so a *refused* explicit ``mcp_command`` (a floating
``git+``/URL install or an unpinned ``uvx`` spec) was still spawned by
``WebSearch.search``.

This suite drives the *real consumers* and proves that a refused launcher is a
typed ``no_binding`` that spawns **nothing**:

* ``WebSearch.search`` -- the formatted-string Council API;
* ``WebSearch._backend_argument`` -- the exact seam of the reported defect (no
  ``command`` survives a refusal);
* the Discovery research invocation -- ``resolve_search_outcome`` driven with
  Council's owned registry (the way Council's repaired Perplexity MCP path is
  consumed), and the external adapter that drives ``PerplexityMcpBackend``
  through the Studio hook facade;
* ``PerplexityMcpBackend.invoke`` directly (defence in depth).

"Spawns nothing" is *observed*, not asserted: the refused command names a
harmless fake ``uvx`` (``COUNCIL_REFUSAL_FAKE_LAUNCHER``) that appends its argv
to ``COUNCIL_REFUSAL_ARGV_LOG``. A non-empty log is the observable proof that a
process was launched; an absent/empty log is the proof none was.

Honest boundary: Discovery's ``resolve_search_outcome`` *defaults* to the
SW-ADAPT-001 substrate registry, whose Perplexity MCP binding lives in
``skillweave.adapters.search_backends`` -- outside Council's write scope. This
suite therefore drives that function with Council's owned registry. The
substrate default's own refusal behaviour is recorded in the final test as a
cross-owner hold, not silently passed.

No live provider is called.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
FAKE_UVX = FIX_C / "fake_bin" / "uvx"

for _p in (str(SRC), str(FIX_C)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skillweave.council import launcher as L  # noqa: E402
from skillweave.council.perplexity import (  # noqa: E402
    PerplexityMcpBackend,
    _refused_launcher_detail,
    run_perplexity_mcp,
)
from skillweave.council.search import (  # noqa: E402
    SearchConfig,
    WebSearch,
    search_registry,
)
from skillweave.discovery import (  # noqa: E402
    resolve_search_outcome,
    write_discovery_binding,
)
from skillweave.studio.hooks.facade import run_hooks  # noqa: E402

#: A floating git+ install and a bare (unpinned) uvx spec -- the two refusals.
FLOATING = "git+https://example.invalid/unpinned-mcp"
UNPINNED_PKG = "some-unpinned-pkg"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def refusal_env(tmp_path, monkeypatch):
    """A private argv log the fake launcher would write to, if ever spawned."""
    log = tmp_path / "refusal-argv.log"
    monkeypatch.setenv("COUNCIL_REFUSAL_FAKE_LAUNCHER", str(FAKE_UVX))
    monkeypatch.setenv("COUNCIL_REFUSAL_ARGV_LOG", str(log))
    return log


def _assert_nothing_spawned(log: Path):
    spawned = log.read_text() if log.exists() else ""
    assert spawned == "", f"a refused launcher was spawned: {spawned!r}"


def _floating_cmd():
    return [str(FAKE_UVX), FLOATING]


def _unpinned_uvx_cmd():
    return [str(FAKE_UVX), UNPINNED_PKG]


# ── the WebSearch consumer: a refused launcher is a typed non-success ─────


class TestWebSearchConsumerRefusal:
    @pytest.mark.parametrize("command", [_floating_cmd, _unpinned_uvx_cmd])
    def test_search_refuses_and_spawns_nothing(self, refusal_env, command):
        ws = WebSearch()
        cfg = SearchConfig(
            provider="perplexity",
            mcp_command=command(),
            full_content_results=0,
            time_range="any",
        )
        text = _run(ws.search("refusal probe", cfg))
        assert isinstance(text, str)
        # The refusal surfaces as a typed no_binding, never a fabricated source.
        assert "no_binding" in text, text
        assert "https://" not in text, text
        _assert_nothing_spawned(refusal_env)

    @pytest.mark.parametrize("command", [_floating_cmd, _unpinned_uvx_cmd])
    def test_backend_argument_drops_the_refused_command(self, command):
        """The exact seam of the reported defect: no ``command`` survives."""
        ws = WebSearch()
        cfg = SearchConfig(provider="perplexity", mcp_command=command())
        arg = ws._backend_argument("q", cfg, "perplexity-search-mcp")
        assert not arg.get("command"), arg
        assert arg["launcher_provenance"]["kind"] == "unresolved"
        assert arg["launcher_resolution_error"] == "explicit_command"

    def test_a_resolved_uvx_command_is_not_refused(self, refusal_env):
        """The guard must not over-refuse a *resolver-produced* uvx command.

        A resolved ``uvx --from name==version name`` command was validated at
        resolution time; re-judging it here would misread the bare trailing
        distribution name as an unpinned spec and wrongly refuse a legitimate
        launch. ``_refused_launcher_detail`` must therefore skip a resolved
        command (marked by ``launcher_kind``) while still refusing the raw one.
        """
        resolved = ("uvx", "--from", "sw-council-pinned==9.9.9", "sw-council-pinned")
        assert _refused_launcher_detail(resolved, resolved=True) is None
        # The same command, unmarked, is refused (it *looks* floating raw).
        assert _refused_launcher_detail(resolved) is not None

    def test_the_real_resolver_output_is_executable(self):
        """A real ``resolve_launcher`` output is admissible to the backend guard."""
        res = L.resolve_launcher(explicit_command=["/bin/echo", "ok"])
        assert _refused_launcher_detail(list(res.command)) is None


# ── the Council backend directly (defence in depth) ───────────────────────


class TestBackendRefusal:
    @pytest.mark.parametrize("command", [_floating_cmd, _unpinned_uvx_cmd])
    def test_invoke_refuses_a_raw_command(self, refusal_env, command):
        backend = PerplexityMcpBackend(timeout=2.0)
        outcome = _run(
            backend.invoke(
                "search.query",
                {"query": "q", "command": command()},
                {},
            )
        )
        assert outcome.status != "success"
        assert outcome.error_code == "no_binding"
        assert outcome.results == ()
        _assert_nothing_spawned(refusal_env)

    @pytest.mark.parametrize("command", [_floating_cmd, _unpinned_uvx_cmd])
    def test_driver_refuses_a_raw_command(self, refusal_env, command):
        outcome, session = run_perplexity_mcp("q", command=command())
        assert outcome.status != "success"
        assert outcome.error_code == "no_binding"
        assert outcome.results == ()
        assert session is None
        _assert_nothing_spawned(refusal_env)


# ── the Discovery consumer: resolve_search_outcome over the Council registry ─


class TestDiscoveryResolveSearchOutcomeRefusal:
    @pytest.mark.parametrize("command", [_floating_cmd, _unpinned_uvx_cmd])
    def test_resolve_search_outcome_refuses_with_council_registry(
        self, refusal_env, command
    ):
        outcome, receipt = _run(
            resolve_search_outcome(
                "refusal probe",
                provider="perplexity",
                mcp_command=command(),
                registry=search_registry(),
            )
        )
        assert outcome.status != "success", outcome.status
        assert outcome.error_code == "no_binding", outcome.error_code
        assert outcome.results == ()
        assert receipt.status != "success"
        _assert_nothing_spawned(refusal_env)


# ── the Discovery consumer adapter through the Studio hook facade ──────────


class TestDiscoveryHookConsumerRefusal:
    def test_refusal_adapter_fails_the_hook_and_spawns_nothing(self, refusal_env, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        write_discovery_binding(
            project,
            adapter_id="external-council-mcp-refusal",
            adapter_factories=["external_council_search:council_mcp_refusal_factory"],
            query="refusal probe",
        )
        result = _run(
            run_hooks(
                "discovery",
                "pre",
                project_root=str(project),
                include_auto_discovered=False,
            )
        )
        assert result is not None
        _binding, hook_result = result.results[0]
        assert hook_result.status == "fail", hook_result.message
        assert "no_binding" in hook_result.message, hook_result.message
        _assert_nothing_spawned(refusal_env)

    def test_refusal_adapter_directly_returns_a_typed_no_binding(self, refusal_env):
        from external_council_search import council_mcp_refusal_factory

        descriptor, backend = council_mcp_refusal_factory()
        outcome = _run(
            backend.invoke("search.query", {"query": "q"}, {})
        )
        assert outcome.error_code == "no_binding", outcome.error_code
        assert outcome.results == ()
        _assert_nothing_spawned(refusal_env)


# ── honest cross-owner hold (not a silent pass) ───────────────────────────


class TestSubstrateDefaultHold:
    def test_substrate_default_path_is_out_of_council_scope(self, refusal_env):
        """Discovery's *default* registry is the SW-ADAPT-001 substrate.

        With no Council registry supplied, ``resolve_search_outcome``'s Perplexity
        MCP binding is ``skillweave.adapters.search_backends._fetch_perplexity_mcp``
        -- outside Council's write scope. This test records the observed boundary
        honestly rather than claiming a fix there:

        * a floating ``git+`` command does not spawn a process (the substrate
          client raises before ``start()``), but it surfaces as a raised
          ``McpClientError`` rather than a typed ``no_binding`` outcome;
        * an *unpinned uvx* spec is **not** refused by the substrate path -- the
          fake launcher is actually spawned.

        That gap needs SW-ADAPT-001 scope (``adapters/search_backends.py`` or
        ``adapters/mcp_client.py``) and is raised as a cross-owner hold.
        """
        from skillweave.adapters.mcp_client import McpClientError

        # The substrate filters the child environment to its allowlist, so the
        # argv-log var reaches the launched child only when forwarded explicitly.
        allow = ("COUNCIL_REFUSAL_ARGV_LOG",)

        # Floating: refused before spawn -- raised, and nothing launched.
        with pytest.raises(McpClientError):
            _run(
                resolve_search_outcome(
                    "q",
                    provider="perplexity",
                    mcp_command=_floating_cmd(),
                    mcp_env_allowlist=allow,
                )
            )
        _assert_nothing_spawned(refusal_env)

        # Unpinned uvx: the substrate path does NOT refuse it -- the launcher is
        # actually spawned, and the outcome is a generic error, never Council's
        # typed ``no_binding``. This is the held gap (out of Council's scope).
        outcome, _ = _run(
            resolve_search_outcome(
                "q",
                provider="perplexity",
                mcp_command=_unpinned_uvx_cmd(),
                mcp_env_allowlist=allow,
            )
        )
        assert outcome.status != "success"
        assert outcome.error_code != "no_binding", (
            "Council's typed no_binding is not reachable on the substrate default "
            "path; if it becomes reachable, revisit this hold"
        )
        spawned = refusal_env.read_text() if refusal_env.exists() else ""
        assert spawned != "", (
            "expected the substrate path to spawn the unpinned uvx launcher "
            "(the documented cross-owner hold); if this changed, revisit the hold"
        )
