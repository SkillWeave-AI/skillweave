"""Discovery <-> SDK contract integration on installed artifacts (SW-ADAPT-001).

This is the cross-repository check the focused reviews asked for: it proves
SDK-owned schema authority and the Discovery consumer *from installed artifacts*,
not from a source tree and not from a source-fixture digest.

Two independent wheels are built from pristine staged copies and each installed
into its own private ``--target``:

* **Core** (``skillweave``) -- provides ``skillweave.adapters`` and
  ``skillweave.discovery`` and the hook engine;
* **SDK** (``skillweave-sdk``) -- owns the neutral ``adapter/`` schema bytes.

A clean subprocess then imports ``skillweave`` from the Core target and
``skillweave_sdk`` from the SDK target (never from ``src`` or site-packages) and
proves:

1. the installed SDK's canonical digest equals the pinned value, computed over
   the *installed* resource bytes;
2. the installed SDK registry carries the three neutral ``adapter/`` schemas
   alongside the unchanged lifecycle ``search-provider`` / ``model-provider``
   contracts and the frozen ``execution/`` set;
3. a descriptor and a typed search outcome **produced by the installed Core**
   validate against the **installed SDK** schema (Core composes the SDK contract
   rather than carrying its own copy);
4. the real ``run_hooks`` Discovery path yields a typed success whose receipt
   binds the computed descriptor digest, the contract and the transport, and
   carries no authority (``self_approving`` and ``is_authority`` both false);
5. the counterexamples (unknown provider, malformed outcome, outage) still fail
   closed on the installed artifacts.

The SDK checkout is read-only input, resolved from ``SKILLWEAVE_SDK_DIR`` or the
sibling ``../skillweave-sdk``. When it is absent the suite skips with a named
reason, so a hermetic clone behaves identically. No live provider is called.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "adapter" / "discovery"

_SDK_DIR_ENV = "SKILLWEAVE_SDK_DIR"
_EXPECTED_SCHEMA_DIGEST = "3dd247f604017bac4ff37ff4f3e9b46c5d1bc48fc8ef225de88ec4b6d14d2e64"


def _resolve_sdk() -> Path:
    env = os.environ.get(_SDK_DIR_ENV)
    candidates = [Path(env)] if env else []
    candidates.append(ROOT.parent / "skillweave-sdk")
    for cand in candidates:
        if (cand / "schema_version.toml").is_file():
            return cand
    pytest.skip(
        "skillweave-sdk is not available: set SKILLWEAVE_SDK_DIR or expose a "
        "sibling ../skillweave-sdk checkout (anchor=schema_version.toml)"
    )


def _pip_install(wheel: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
         "--no-build-isolation", "--quiet", "--target", str(target), str(wheel)],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        raise AssertionError(
            f"install failed for {wheel.name}\nstdout:\n{install.stdout}\n"
            f"stderr:\n{install.stderr}"
        )
    return target


@pytest.fixture(scope="session")
def installed_core(tmp_path_factory):
    staging = tmp_path_factory.mktemp("core-stage")
    # Core ships two top-level packages under src/.
    stage_src = staging / "src"
    stage_src.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, staging / name)
    shutil.copytree(ROOT / "src" / "skillweave", stage_src / "skillweave")
    degraded = ROOT / "src" / "skillweave_degraded.py"
    if degraded.is_file():
        shutil.copy2(degraded, stage_src / "skillweave_degraded.py")
    wheel_dir = tmp_path_factory.mktemp("core-wheel")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(wheel_dir)],
        cwd=str(staging), capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise AssertionError(f"core wheel build failed\n{build.stdout}\n{build.stderr}")
    wheel = sorted(wheel_dir.glob("skillweave-*.whl"))[0]
    target = _pip_install(wheel, tmp_path_factory.mktemp("core-installed"))
    return wheel, target


@pytest.fixture(scope="session")
def installed_sdk(tmp_path_factory):
    sdk_root = _resolve_sdk()
    staging = tmp_path_factory.mktemp("sdk-stage")
    for name in ("pyproject.toml", "README.md", "LICENSE", "schema_version.toml"):
        src = sdk_root / name
        if src.is_file():
            shutil.copy2(src, staging / name)
    shutil.copytree(
        sdk_root / "src" / "skillweave_sdk", staging / "src" / "skillweave_sdk"
    )
    wheel_dir = tmp_path_factory.mktemp("sdk-wheel")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(wheel_dir)],
        cwd=str(staging), capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise AssertionError(f"sdk wheel build failed\n{build.stdout}\n{build.stderr}")
    wheel = sorted(wheel_dir.glob("skillweave_sdk-*.whl"))[0]
    target = _pip_install(wheel, tmp_path_factory.mktemp("sdk-installed"))
    return wheel, target


_PROBE = r'''
import asyncio, json, sys
from pathlib import Path

core_target, sdk_target, fixtures, project = sys.argv[1:5]
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
sys.path.insert(0, fixtures)
sys.path.insert(0, core_target)
sys.path.insert(0, sdk_target)

import skillweave
import skillweave.discovery as D
from skillweave.studio.hooks.facade import run_hooks

import skillweave_sdk
import skillweave_sdk.validator as V
from importlib.resources import files

report = {
    "skillweave_file": skillweave.__file__,
    "skillweave_sdk_file": skillweave_sdk.__file__,
}

# -- installed SDK authority, computed over installed bytes ------------------
schemas = files("skillweave_sdk") / "schemas"
report["sdk_schemas_dir"] = str(schemas)
report["installed_digest"] = V.canonical_digest(schemas)
report["expected_digest"] = V.EXPECTED_SCHEMA_DIGEST
registry = V.load_registry(schemas)
report["adapter_ids"] = sorted(i for i in registry if "/adapter/" in i)
report["has_search_provider"] = (
    "https://skillweave.dev/schemas/lifecycle/search-provider/v1" in registry)
report["has_model_provider"] = (
    "https://skillweave.dev/schemas/lifecycle/model-provider/v1" in registry)
report["execution_count"] = sum(1 for i in registry if "/execution/" in i)

DESCRIPTOR = "https://skillweave.dev/schemas/adapter/adapter-descriptor/v1"
OUTCOME = "https://skillweave.dev/schemas/adapter/search-outcome/v1"

# -- Core-produced descriptor validates against the SDK schema ---------------
import external_discovery_adapter as ext
descriptor, backend = ext.discovery_success_factory()
descriptor_dict = descriptor.to_dict()
report["descriptor_errors"] = V.validate(descriptor_dict, DESCRIPTOR, schemas)

# -- real run_hooks Discovery path on installed artifacts --------------------
root = Path(project)
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
report["artifact_exists"] = bool(hook_result.artifacts) and Path(
    hook_result.artifacts[0]).exists()
payload = json.loads(Path(hook_result.artifacts[0]).read_text())
run = payload["runs"][0]
report["evidence_status"] = run["status"]
report["evidence_url"] = run["results"][0]["url"]
receipt = run["receipt"]
report["receipt_adapter"] = receipt["adapter_id"]
report["receipt_contract"] = receipt["domain_contract"]
report["receipt_transport"] = receipt["transport"]
report["receipt_self_approving"] = receipt["self_approving"]
report["receipt_is_authority"] = receipt["is_authority"]

# The receipt's descriptor digest must equal the digest of the descriptor the
# installed Core actually registered (recomputed from the SDK-neutral shape).
from skillweave.adapters.outcomes import descriptor_digest as dd
report["receipt_digest"] = receipt["descriptor_digest"]
report["recomputed_digest"] = dd(descriptor_dict)

# -- the live typed outcome validates against the SDK schema -----------------
live_outcome, live_receipt = asyncio.run(D.resolve_search_outcome(
    "installed discovery research",
    adapter_id="external-discovery-search",
    extra_adapters=[(descriptor, backend, "external:fixture")],
))
report["outcome_errors"] = V.validate(
    live_outcome.to_dict() | {"requested_provider": live_outcome.requested_provider,
                              "schema_version": live_outcome.schema_version},
    OUTCOME, schemas)
report["live_receipt_is_authority"] = live_receipt.is_authority
report["live_status"] = live_outcome.status

# -- counterexamples ---------------------------------------------------------
D.write_discovery_binding(root, provider="not-a-provider")
report["unknown_message"] = asyncio.run(run_hooks(
    "discovery", "pre", project_root=str(root), include_auto_discovered=False
)).results[0][1].message
D.write_discovery_binding(
    root,
    adapter_id="external-discovery-malformed",
    adapter_factories=["external_discovery_adapter:discovery_malformed_factory"],
)
report["malformed_message"] = asyncio.run(run_hooks(
    "discovery", "pre", project_root=str(root), include_auto_discovered=False
)).results[0][1].message
D.write_discovery_binding(
    root,
    adapter_id="external-discovery-outage",
    adapter_factories=["external_discovery_adapter:discovery_outage_factory"],
)
report["outage_message"] = asyncio.run(run_hooks(
    "discovery", "pre", project_root=str(root), include_auto_discovered=False
)).results[0][1].message

print(json.dumps(report))
'''


def _probe(installed_core, installed_sdk, project_dir: Path) -> dict:
    _, core_target = installed_core
    _, sdk_target = installed_sdk
    project_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(core_target), str(sdk_target),
         str(FIXTURES), str(project_dir)],
        cwd=str(project_dir), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, (
        f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestInstalledSdkAuthority:
    def test_both_imports_resolve_to_their_installed_targets(
        self, installed_core, installed_sdk, tmp_path
    ):
        r = _probe(installed_core, installed_sdk, tmp_path / "a")
        assert str(installed_core[1]) in r["skillweave_file"], r
        assert str(installed_sdk[1]) in r["skillweave_sdk_file"], r
        assert str(ROOT / "src") not in r["skillweave_file"], r

    def test_installed_sdk_digest_and_registry_keep_all_contract_groups(
        self, installed_core, installed_sdk, tmp_path
    ):
        r = _probe(installed_core, installed_sdk, tmp_path / "b")
        assert r["installed_digest"] == _EXPECTED_SCHEMA_DIGEST, r
        assert len(r["adapter_ids"]) == 3, r
        assert r["has_search_provider"] is True, r
        assert r["has_model_provider"] is True, r
        assert r["execution_count"] == 11, r

    def test_core_descriptor_and_outcome_validate_against_installed_sdk_schema(
        self, installed_core, installed_sdk, tmp_path
    ):
        r = _probe(installed_core, installed_sdk, tmp_path / "c")
        assert r["descriptor_errors"] == [], r
        assert r["outcome_errors"] == [], r

    def test_receipt_binds_digest_contract_transport_and_no_authority(
        self, installed_core, installed_sdk, tmp_path
    ):
        r = _probe(installed_core, installed_sdk, tmp_path / "d")
        assert r["pass_status"] == "pass", r
        assert r["receipt_adapter"] == "external-discovery-search", r
        assert r["receipt_contract"] == "search-outcome/v1", r
        assert r["receipt_transport"] == "in-process", r
        assert r["receipt_digest"] == r["recomputed_digest"], r
        assert r["receipt_self_approving"] is False, r
        assert r["receipt_is_authority"] is False, r
        assert r["live_receipt_is_authority"] is False, r

    def test_installed_counterexamples_fail_closed(
        self, installed_core, installed_sdk, tmp_path
    ):
        r = _probe(installed_core, installed_sdk, tmp_path / "e")
        assert "unknown provider" in r["unknown_message"], r
        assert "malformed" in r["malformed_message"], r
        assert "unavailable" in r["outage_message"] or "did not succeed" in r["outage_message"], r
