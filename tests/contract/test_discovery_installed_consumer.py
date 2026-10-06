"""The Discovery lifecycle consumer survives packaging (SW-ADAPT-001, AC4/AC8).

A source-tree test can bind Discovery while Core's ``src`` is importable, which
proves nothing about the artifact a consumer installs. This suite builds Core
from a pristine staged copy, installs it into a private ``--target``, and then,
in a **clean subprocess whose ``sys.path`` contains only the installed target
plus the external fixture directory (never Core's ``src``)**:

1. imports ``skillweave.discovery`` from the *installed* copy;
2. materializes the source-owned binding into a throwaway project;
3. drives the real :func:`skillweave.studio.hooks.facade.run_hooks` path at
   ``discovery/pre`` with an external adapter the core has never heard of
   (registered through the public registry, no core edit);
4. confirms the typed success, the evidence artifact and the receipt;
5. confirms the counterexamples (unknown provider, malformed outcome) still
   fail closed on the installed artifact.

No live provider is called.
"""

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "adapter" / "discovery"
SKILL_MANIFEST = ROOT / "skills" / "skillweave-discovery" / "capability.yaml"


@pytest.fixture(scope="session")
def installed_core(tmp_path_factory):
    """Build Core from a pristine staged copy, install it, return the target."""
    staging = tmp_path_factory.mktemp("discovery-core-stage")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, staging / name)
    shutil.copytree(ROOT / "src" / "skillweave", staging / "src" / "skillweave")
    shutil.copy2(
        ROOT / "src" / "skillweave_degraded.py",
        staging / "src" / "skillweave_degraded.py",
    )

    outdir = tmp_path_factory.mktemp("discovery-core-wheel")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(outdir)],
        cwd=str(staging), capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise AssertionError(
            f"core wheel build failed ({build.returncode})\nstdout:\n"
            f"{build.stdout}\nstderr:\n{build.stderr}"
        )
    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {[w.name for w in wheels]}"
    wheel = wheels[0]

    target = tmp_path_factory.mktemp("discovery-core-installed")
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
         "--no-build-isolation", "--quiet", "--target", str(target), str(wheel)],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        raise AssertionError(
            f"core wheel install failed ({install.returncode})\nstdout:\n"
            f"{install.stdout}\nstderr:\n{install.stderr}"
        )
    return wheel, target


_PROBE = '''
import asyncio, json, sys
from pathlib import Path

target, fixtures, project, skill_manifest = sys.argv[1:5]
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
sys.path.insert(0, fixtures)
sys.path.insert(0, target)

import yaml
import skillweave
import skillweave.discovery as D
from skillweave.studio.hooks.facade import run_hooks

report = {"skillweave_file": skillweave.__file__,
          "hook_module": D.DISCOVERY_HOOK_MODULE}

root = Path(project)

# -- the shipped *skill* declaration, materialized by the installed code -----
# This is the AC4/AC8 seam: the Markdown skill's declared boundary is bound to
# the packaged hook, and the installed Core is what turns that declaration into
# a binding the real engine loads.
_manifest = yaml.safe_load(Path(skill_manifest).read_text())
_manifest["studio_binding"] = {
    **_manifest["studio_binding"],
    "config": {
        "provider": "duckduckgo",
        "adapter_id": "external-discovery-search",
        "adapter_factories": [
            "external_discovery_adapter:discovery_success_factory"
        ],
        "query": "installed skill-declared research",
    },
}
D.materialize_skill_binding(_manifest, root)
res_skill = asyncio.run(run_hooks("discovery", "pre", project_root=str(root),
                                  include_auto_discovered=False))
_skill_binding, _skill_result = res_skill.results[0]
report["skill_declared_module"] = _skill_binding.module
report["skill_declared_status"] = _skill_result.status
report["skill_declared_message"] = _skill_result.message
report["skill_declared_has_artifact"] = bool(_skill_result.artifacts) and Path(
    _skill_result.artifacts[0]).exists()

# -- installed external adapter -> typed success through the real engine -----
D.write_discovery_binding(
    root,
    adapter_id="external-discovery-search",
    adapter_factories=["external_discovery_adapter:discovery_success_factory"],
    query="installed discovery research",
)
res = asyncio.run(run_hooks("discovery", "pre", project_root=str(root),
                            include_auto_discovered=False))
binding, hook_result = res.results[0]
report["pass_status"] = hook_result.status
report["pass_message"] = hook_result.message
report["hook_module_ran"] = binding.module
report["artifact_exists"] = bool(hook_result.artifacts) and Path(
    hook_result.artifacts[0]).exists()
payload = json.loads(Path(hook_result.artifacts[0]).read_text())
run = payload["runs"][0]
report["evidence_status"] = run["status"]
report["evidence_url"] = run["results"][0]["url"]
report["receipt_adapter"] = run["receipt"]["adapter_id"]
report["receipt_digest_len"] = len(run["receipt"]["descriptor_digest"])
report["receipt_contract"] = run["receipt"]["domain_contract"]
report["receipt_self_approving"] = run["receipt"]["self_approving"]

# is_authority is a read-only property (never serialized); read it from the
# live receipt returned by the same installed seam.
import external_discovery_adapter as _ext
_descriptor, _backend = _ext.discovery_success_factory()
_live_outcome, _live_receipt = asyncio.run(D.resolve_search_outcome(
    "installed discovery research",
    adapter_id="external-discovery-search",
    extra_adapters=[(_descriptor, _backend, "external:fixture")],
))
report["live_status"] = _live_outcome.status
report["live_receipt_is_authority"] = _live_receipt.is_authority
report["live_receipt_self_approving"] = _live_receipt.self_approving

# -- counterexample: unknown provider -> hook fail --------------------------
D.write_discovery_binding(root, provider="not-a-provider")
res2 = asyncio.run(run_hooks("discovery", "pre", project_root=str(root),
                             include_auto_discovered=False))
report["unknown_status"] = res2.results[0][1].status
report["unknown_message"] = res2.results[0][1].message

# -- counterexample: malformed outcome -> hook fail -------------------------
D.write_discovery_binding(
    root,
    adapter_id="external-discovery-malformed",
    adapter_factories=["external_discovery_adapter:discovery_malformed_factory"],
)
res3 = asyncio.run(run_hooks("discovery", "pre", project_root=str(root),
                             include_auto_discovered=False))
report["malformed_status"] = res3.results[0][1].status
report["malformed_message"] = res3.results[0][1].message

print(json.dumps(report))
'''


def _probe(installed_core, project_dir: Path) -> dict:
    _, target = installed_core
    project_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(target), str(FIXTURES), str(project_dir),
         str(SKILL_MANIFEST)],
        cwd=str(project_dir), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, (
        f"probe against the installed artifact failed\nstdout:\n"
        f"{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestInstalledDiscoveryConsumer:
    def test_wheel_contains_the_discovery_package(self, installed_core):
        wheel, _ = installed_core
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        shipped = sorted(
            n.rsplit("/", 1)[1] for n in names
            if n.startswith("skillweave/discovery/") and n.endswith(".py")
        )
        for module in ("__init__.py", "lifecycle_search.py",
                       "lifecycle_hook.py", "binding.py"):
            assert module in shipped, f"wheel is missing discovery/{module}: {shipped}"

    def test_the_shipped_skill_declaration_drives_the_installed_engine(
        self, installed_core, tmp_path
    ):
        """AC4/AC8: the Markdown skill's declared boundary runs on the install.

        The skill manifest shipped in this checkout is the declaration; the
        *installed* Core is what reads it and drives the registry-backed typed
        search through the real ``run_hooks`` path.
        """
        report = _probe(installed_core, tmp_path / "proj0")
        assert report["skill_declared_module"] == report["hook_module"], report
        assert report["skill_declared_status"] == "pass", report
        assert report["skill_declared_has_artifact"] is True, report

    def test_installed_import_resolves_to_the_target(self, installed_core, tmp_path):
        report = _probe(installed_core, tmp_path / "proj1")
        assert str(installed_core[1]) in report["skillweave_file"], report
        assert str(ROOT / "src") not in report["skillweave_file"], report

    def test_external_adapter_yields_a_typed_success_on_the_installed_artifact(
        self, installed_core, tmp_path
    ):
        report = _probe(installed_core, tmp_path / "proj2")
        assert report["pass_status"] == "pass", report
        assert report["artifact_exists"] is True, report
        assert report["evidence_status"] == "success", report
        assert report["evidence_url"].startswith("https://"), report

    def test_installed_receipt_binds_the_contract_and_carries_no_authority(
        self, installed_core, tmp_path
    ):
        report = _probe(installed_core, tmp_path / "proj3")
        assert report["receipt_adapter"] == "external-discovery-search", report
        assert report["receipt_contract"] == "search-outcome/v1", report
        assert report["receipt_digest_len"] == 64, report
        assert report["receipt_self_approving"] is False, report
        assert report["live_receipt_is_authority"] is False, report
        assert report["live_receipt_self_approving"] is False, report
        assert report["live_status"] == "success", report

    def test_installed_counterexamples_still_fail_closed(self, installed_core, tmp_path):
        report = _probe(installed_core, tmp_path / "proj4")
        assert report["unknown_status"] == "fail", report
        assert "unknown provider" in report["unknown_message"], report
        assert report["malformed_status"] == "fail", report
        assert "malformed" in report["malformed_message"], report
