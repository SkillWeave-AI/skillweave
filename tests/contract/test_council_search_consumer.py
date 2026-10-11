"""Council's search consumer: the formatted-string API, end to end (SW-COUNCIL-003, criterion 5).

Two *real* consumers must keep consuming verified source results and explicit
failure states through Council's repaired Perplexity MCP path:

* the **Council caller** -- :meth:`skillweave.council.search.WebSearch.search`
  is still a *formatted-string* API, driven by the Stage-0 engine call
  (``searcher.search(query)``). It must render a real http(s) source on success
  and a visible failure notice on a non-success -- never a fabricated source;
* the **Discovery research invocation** -- the Studio hook engine's
  ``run_hooks("discovery", "pre", ...)`` runs the external adapter that drives
  *Council's* MCP backend, so Discovery's real source / failure state is proven
  to come from the Council repair, not a synthetic stub.

The same two consumers are then proven on the **installed artifact**: Core is
built from a staged copy, installed into a private ``--target``, and the
consumers are driven in a clean subprocess whose ``sys.path`` holds only the
installed target plus the fixture directory.

No live provider is called.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
FAKE = FIX_C / "fake_mcp_server.py"

for _p in (str(SRC), str(FIX_C)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from skillweave.adapters import SEARCH_OPERATION  # noqa: E402
from skillweave.council.search import SearchConfig, WebSearch  # noqa: E402
from skillweave.council.perplexity import PerplexityMcpBackend  # noqa: E402
from skillweave.discovery import write_discovery_binding  # noqa: E402
from skillweave.studio.hooks.facade import run_hooks  # noqa: E402


def _fake(mode: str) -> list[str]:
    return [sys.executable, str(FAKE), mode]


def _run(coro):
    return asyncio.run(coro)


# ── the Council caller: a formatted-string API over the repaired path ──────


class TestCouncilFormattedStringConsumer:
    def test_search_returns_a_formatted_string_with_a_real_source(self):
        ws = WebSearch()
        cfg = SearchConfig(
            provider="perplexity",
            mcp_command=_fake("normal"),
            full_content_results=0,
            time_range="any",
        )
        text = _run(ws.search("installed council research", cfg))
        assert isinstance(text, str)
        assert "Web Search Results" in text
        assert "https://example.org/cited" in text
        assert "Cited result" in text

    def test_search_reports_an_explicit_failure_state_not_a_source(self):
        ws = WebSearch()
        cfg = SearchConfig(
            provider="perplexity",
            mcp_command=_fake("text-only"),
            full_content_results=0,
            time_range="any",
        )
        text = _run(ws.search("installed council research", cfg))
        assert isinstance(text, str)
        assert "https://" not in text, "a no-results non-success must not fake a source"

    def test_the_engine_stage_zero_drives_the_formatted_string_seam(self):
        """The Stage-0 call ``searcher.search(query)`` is the Council consumer.

        The engine holds a ``WebSearch`` and awaits ``search(query)`` (Stage 0);
        that call must return the formatted string. A minimal stub captures the
        exact call the engine makes, and a real ``WebSearch`` proves the string it
        would return carries a verified source from the repaired path.
        """
        from skillweave.council.engine import CouncilConfig, CouncilEngine

        captured: dict = {}

        class _Reply(str):
            def __new__(cls, text, answering_model):
                obj = super().__new__(cls, text)
                obj.answering_model = answering_model
                obj.served_by = answering_model
                obj.requested_model = answering_model
                obj.provider = "fake"
                return obj

        class _QuietProvider:
            async def query(self, model_id, messages, temperature=0.0):
                return _Reply(f"answer from {model_id}", model_id)

        class _CapturingSearcher:
            async def search(self, query):
                captured["query"] = query
                return ("Web Search Results (time range: any):\n"
                        "Result 1: X | https://s.example.org/1\n  snippet\n")

        engine = CouncilEngine(provider=_QuietProvider(), searcher=_CapturingSearcher())
        cfg = CouncilConfig(models=["m1", "m2"], chairman="m1", mode="quick",
                            total_timeout=2.0)
        result = _run(engine.deliberate("stage zero query", cfg))
        assert captured["query"] == "stage zero query"
        assert "https://s.example.org/1" in result.search_context

        # And the real Council WebSearch returns that same string shape with a
        # verified source from the repaired MCP path.
        ws = WebSearch()
        text = _run(ws.search("q", SearchConfig(provider="perplexity",
                                                mcp_command=_fake("normal"),
                                                full_content_results=0)))
        assert "https://example.org/cited" in text


# ── the Discovery research invocation consumes Council's MCP outcome ───────


def _discovery(project: Path, **binding):
    write_discovery_binding(project, **binding)
    return _run(
        run_hooks("discovery", "pre", project_root=str(project),
                  include_auto_discovered=False)
    )


class TestDiscoveryConsumesCouncilRepair:
    def test_success_consumes_a_real_source_from_council_backend(self, tmp_path):
        result = _discovery(
            tmp_path,
            adapter_id="external-council-mcp-search",
            adapter_factories=["external_council_search:council_mcp_success_factory"],
            query="installed discovery research",
        )
        assert result is not None
        binding, hook_result = result.results[0]
        assert hook_result.status == "pass", hook_result.message
        assert "real source(s)" in hook_result.message
        evidence = Path(hook_result.artifacts[0])
        payload = json.loads(evidence.read_text())
        run = payload["runs"][0]
        assert run["status"] == "success"
        urls = {r["url"] for r in run["results"]}
        assert "https://example.org/cited" in urls, urls
        assert "https://citation.example.net/a" in urls, urls
        assert all(u.startswith("http") for u in urls), urls
        assert run["receipt"]["adapter_id"] == "external-council-mcp-search"
        assert run["receipt"]["self_approving"] is False

    def test_failure_is_an_explicit_state_with_no_source(self, tmp_path):
        result = _discovery(
            tmp_path,
            adapter_id="external-council-mcp-fail",
            adapter_factories=["external_council_search:council_mcp_failure_factory"],
            query="installed discovery research",
        )
        binding, hook_result = result.results[0]
        assert hook_result.status == "fail", hook_result.message
        assert "no_results" in hook_result.message
        evidence = Path(hook_result.artifacts[0])
        payload = json.loads(evidence.read_text())
        run = payload["runs"][0]
        assert run["status"] != "success"
        assert run["result_count"] == 0
        assert run["results"] == []


# ── the same two consumers on the INSTALLED artifact ───────────────────────


@pytest.fixture(scope="session")
def installed_core(tmp_path_factory):
    staging = tmp_path_factory.mktemp("council-core-stage")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, staging / name)
    shutil.copytree(SRC / "skillweave", staging / "src" / "skillweave")
    shutil.copy2(SRC / "skillweave_degraded.py", staging / "src" / "skillweave_degraded.py")

    outdir = tmp_path_factory.mktemp("council-core-wheel")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(outdir)],
        cwd=str(staging), capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise AssertionError(
            f"core wheel build failed ({build.returncode})\nstdout:\n{build.stdout}\n"
            f"stderr:\n{build.stderr}"
        )
    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, [w.name for w in wheels]
    wheel = wheels[0]

    target = tmp_path_factory.mktemp("council-core-installed")
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
         "--no-build-isolation", "--quiet", "--target", str(target), str(wheel)],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        raise AssertionError(
            f"core wheel install failed ({install.returncode})\nstdout:\n{install.stdout}\n"
            f"stderr:\n{install.stderr}"
        )
    return wheel, target


_PROBE = '''
import asyncio, json, sys
from pathlib import Path

target, fixtures, project = sys.argv[1:4]
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
sys.path.insert(0, fixtures)
sys.path.insert(0, target)

import skillweave
from skillweave.council.search import SearchConfig, WebSearch
from skillweave.council.perplexity import PerplexityMcpBackend
from skillweave.discovery import write_discovery_binding
from skillweave.studio.hooks.facade import run_hooks

report = {"skillweave_file": skillweave.__file__}
fake = str(Path(fixtures) / "fake_mcp_server.py")
mcp_cmd = [sys.executable, fake, "normal"]

# -- Council caller: the registry ships Council's own MCP backend ------------
ws = WebSearch()
entry = ws.registry.get("perplexity-search-mcp")
report["council_backend_is_owned"] = type(entry.backend).__name__ == "PerplexityMcpBackend"
report["council_backend_module"] = type(entry.backend).__module__

text = asyncio.run(ws.search(
    "installed council research",
    SearchConfig(provider="perplexity", mcp_command=mcp_cmd,
                 full_content_results=0, time_range="any"),
))
report["council_text"] = text
report["council_has_source"] = "https://example.org/cited" in text

ftext = asyncio.run(ws.search(
    "installed council research",
    SearchConfig(provider="perplexity", mcp_command=[sys.executable, fake, "text-only"],
                 full_content_results=0, time_range="any"),
))
report["council_failure_text"] = ftext
report["council_failure_has_source"] = "https://" in ftext

# -- Discovery invocation: consumes Council's typed outcome ------------------
root = Path(project)
write_discovery_binding(
    root,
    adapter_id="external-council-mcp-search",
    adapter_factories=["external_council_search:council_mcp_success_factory"],
    query="installed discovery research",
)
res = asyncio.run(run_hooks("discovery", "pre", project_root=str(root),
                            include_auto_discovered=False))
binding, hook_result = res.results[0]
report["discovery_module"] = binding.module
report["discovery_status"] = hook_result.status
report["discovery_message"] = hook_result.message
payload = json.loads(Path(hook_result.artifacts[0]).read_text())
run = payload["runs"][0]
report["discovery_evidence_status"] = run["status"]
report["discovery_evidence_urls"] = [r["url"] for r in run["results"]]

write_discovery_binding(
    root,
    adapter_id="external-council-mcp-fail",
    adapter_factories=["external_council_search:council_mcp_failure_factory"],
)
res2 = asyncio.run(run_hooks("discovery", "pre", project_root=str(root),
                             include_auto_discovered=False))
report["discovery_fail_status"] = res2.results[0][1].status
report["discovery_fail_message"] = res2.results[0][1].message

print(json.dumps(report))
'''


def _probe(installed_core, project_dir: Path) -> dict:
    _, target = installed_core
    project_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(target), str(FIX_C), str(project_dir)],
        cwd=str(project_dir), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, (
        f"probe against the installed artifact failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestInstalledConsumers:
    def test_wheel_ships_the_council_mcp_modules(self, installed_core):
        wheel, _ = installed_core
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        shipped = sorted(
            n.rsplit("/", 1)[1] for n in names
            if n.startswith("skillweave/council/") and n.endswith(".py")
        )
        for module in ("mcp_client.py", "perplexity.py", "launcher.py", "search.py"):
            assert module in shipped, f"wheel is missing council/{module}: {shipped}"

    def test_installed_council_caller_renders_a_real_source(self, installed_core, tmp_path):
        report = _probe(installed_core, tmp_path / "c-proj")
        assert str(installed_core[1]) in report["skillweave_file"], report
        assert str(SRC) not in report["skillweave_file"], report
        assert report["council_backend_is_owned"] is True, report
        assert "skillweave.council.perplexity" in report["council_backend_module"], report
        assert report["council_has_source"] is True, report["council_text"]
        assert "Web Search Results" in report["council_text"], report["council_text"]

    def test_installed_council_caller_reports_explicit_failure(self, installed_core, tmp_path):
        report = _probe(installed_core, tmp_path / "c-proj2")
        assert report["council_failure_has_source"] is False, report["council_failure_text"]

    def test_installed_discovery_consumes_the_council_source(self, installed_core, tmp_path):
        report = _probe(installed_core, tmp_path / "c-proj3")
        assert report["discovery_status"] == "pass", report
        assert report["discovery_evidence_status"] == "success", report
        assert "https://example.org/cited" in report["discovery_evidence_urls"], report

    def test_installed_discovery_reports_an_explicit_failure(self, installed_core, tmp_path):
        report = _probe(installed_core, tmp_path / "c-proj4")
        assert report["discovery_fail_status"] == "fail", report
        assert "no_results" in report["discovery_fail_message"], report
