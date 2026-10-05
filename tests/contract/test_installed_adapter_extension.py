"""Installed-artifact extension test (SW-ADAPT-001, criteria 3 & 8).

The facade's extension promise is only real if it survives packaging. A
``PYTHONPATH=src`` test can register an external adapter while the *source
tree* is importable, which proves nothing about the artifact a consumer
installs. This suite closes that gap the same way the SDK closes it for
schemas: it builds and installs Core into a private ``--target`` directory,
then, in a **clean subprocess whose ``sys.path`` contains only the installed
target plus the external fixture directory (never Core's ``src``)**:

1. imports ``skillweave.adapters`` from the *installed* copy;
2. registers an external synthetic search adapter and a harmless generic echo
   tool -- neither named anywhere in Core -- through the public extension
   registry, with **no Core vendor edit**;
3. discovers both by id and admits/invokes them through the common
   :class:`AdapterFacade`, obtaining a typed outcome and an
   :class:`OperationReceipt`;
4. drives the failure counterexamples -- an unknown adapter id, a malformed
   descriptor payload, and an unapproved adapter -- and confirms each fails
   closed rather than being served by a neighbour.

Equivalence note (criterion 7 boundary): this proves the *local explicit
registration* path on an installed artifact. The Capacium package-format
reader equivalence is covered separately by
``test_adapter_format_readers.py``; both readers register through this same
runtime port.
"""

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "adapter" / "external"

#: Adapter ids that appear *nowhere* in Core. If the installed registry knows
#: them, it is only because the external fixture registered them at runtime.
EXTERNAL_IDS = ("external-echo-tool", "external-synthetic-search")


@pytest.fixture(scope="session")
def installed_core(tmp_path_factory):
    """Build Core from a pristine staged copy, install it, return the target.

    The build runs against a staged copy assembled from the tracked package
    sources, never against ``ROOT`` in place: an in-place build leaves a
    ``build/lib`` tree that setuptools silently reuses, letting a stale
    artifact satisfy the packaging assertions on a dirty developer tree.
    """
    staging = tmp_path_factory.mktemp("core-stage")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, staging / name)
    shutil.copytree(ROOT / "src" / "skillweave", staging / "src" / "skillweave")
    shutil.copy2(ROOT / "src" / "skillweave_degraded.py",
                 staging / "src" / "skillweave_degraded.py")

    outdir = tmp_path_factory.mktemp("core-wheel")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(outdir)],
        cwd=str(staging), capture_output=True, text=True,
    )
    if build.returncode != 0:
        # The build is a hard requirement: skipping on its absence would hide
        # exactly the packaging gap this test exists to catch.
        raise AssertionError(
            f"core wheel build failed ({build.returncode})\nstdout:\n"
            f"{build.stdout}\nstderr:\n{build.stderr}"
        )
    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {[w.name for w in wheels]}"
    wheel = wheels[0]

    target = tmp_path_factory.mktemp("core-installed")
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


#: The probe runs against the install. It is one self-contained program so the
#: import it exercises is exactly the import a consumer makes. ``sys.path`` is
#: rebuilt to the installed target + the external fixtures only -- Core's
#: source tree is deliberately absent, so a source-tree copy cannot satisfy the
#: import by accident.
_PROBE = '''
import asyncio, json, sys

target, fixtures = sys.argv[1], sys.argv[2]
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
sys.path.insert(0, fixtures)
sys.path.insert(0, target)

import skillweave
import skillweave.adapters as A
import external_search_adapter as ext
import external_generic_tool as gen

report = {"skillweave_file": skillweave.__file__}

# The built-in (vendor) registry: it must NOT know the external ids.
builtin = A.build_search_registry()
report["builtin_ids"] = sorted(builtin.ids())

# Register the external adapters through the public extension registry only.
registry = A.AdapterRegistry()
registry.register(ext.external_descriptor(),
                  ext.ExternalSyntheticSearchBackend(),
                  source="external:pkg")
registry.register(gen.generic_descriptor(), gen.EchoToolBackend(),
                  source="external:pkg")
report["registered_ids"] = sorted(registry.ids())

policy = A.EffectivePolicy(
    allowed_adapter_ids=("external-synthetic-search", "external-echo-tool"),
    egress=A.EgressPolicy(allow_network=True,
                          endpoint_allowlist=("external.example.org",)),
)
facade = A.AdapterFacade(registry, policy)

# -- search path: resolve -> admit -> invoke -> typed outcome + receipt ------
outcome, receipt = asyncio.run(facade.invoke(
    category="search",
    operation=A.SEARCH_OPERATION,
    argument={"query": "installed artifact"},
    adapter_id="external-synthetic-search",
    endpoint="https://external.example.org/result",
))
report["search"] = {
    "status": outcome.status,
    "urls": [r.url for r in outcome.results],
    "receipt": receipt.to_dict(),
    "self_approving": receipt.self_approving,
    "is_authority": receipt.is_authority,
}

# -- generic path: same registry, same facade, never a search fallback ------
tool_outcome, tool_receipt = asyncio.run(facade.invoke(
    category="generic",
    operation="tool.echo",
    argument={"value": "hello"},
    adapter_id="external-echo-tool",
))
report["generic"] = {
    "status": tool_outcome.status,
    "payload": tool_outcome.payload,
    "domain_contract": tool_receipt.domain_contract,
}

# -- counterexample 1: an unknown adapter id fails closed -------------------
try:
    asyncio.run(facade.invoke(
        category="search",
        operation=A.SEARCH_OPERATION,
        argument={"query": "x"},
        adapter_id="no-such-adapter",
    ))
    report["unknown_adapter"] = "NOT_REFUSED"
except A.UnknownAdapterError as exc:
    report["unknown_adapter"] = {"type": type(exc).__name__, "asset": exc.asset}

# -- counterexample 2: a malformed descriptor payload fails closed ----------
try:
    A.AdapterDescriptor.from_dict({"id": "broken"})  # no categories/ops/transport
    report["malformed_descriptor"] = "NOT_REFUSED"
except A.AdapterDescriptorError as exc:
    report["malformed_descriptor"] = {"type": type(exc).__name__, "asset": exc.asset}

# -- counterexample 3: an unapproved adapter is never served ----------------
denied = A.AdapterFacade(
    registry,
    A.EffectivePolicy(allowed_adapter_ids=("external-echo-tool",),
                      egress=A.EgressPolicy(allow_network=True,
                                            endpoint_allowlist=("external.example.org",))),
)
try:
    asyncio.run(denied.invoke(
        category="search",
        operation=A.SEARCH_OPERATION,
        argument={"query": "x"},
        adapter_id="external-synthetic-search",
        endpoint="https://external.example.org/result",
    ))
    report["unapproved"] = "NOT_REFUSED"
except A.AdapterError as exc:
    report["unapproved"] = {"type": type(exc).__name__, "asset": exc.asset}

print(json.dumps(report))
'''


def _probe(installed_core) -> dict:
    _, target = installed_core
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(target), str(FIXTURES)],
        cwd=str(target), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, (
        f"probe against the installed artifact failed\nstdout:\n"
        f"{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestInstalledArtifactCarriesTheFacade:
    def test_wheel_contains_the_adapters_package(self, installed_core):
        wheel, _ = installed_core
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        adapters = [n for n in names
                    if n.startswith("skillweave/adapters/") and n.endswith(".py")]
        shipped = sorted(n.rsplit("/", 1)[1] for n in adapters)
        for module in ("__init__.py", "descriptor.py", "outcomes.py",
                       "policy.py", "registry.py", "search_backends.py",
                       "mcp_client.py", "format_readers.py"):
            assert module in shipped, f"wheel is missing adapters/{module}: {shipped}"

    def test_installed_import_resolves_to_the_target(self, installed_core):
        _, target = installed_core
        report = _probe(installed_core)
        assert str(target) in report["skillweave_file"], report
        assert str(ROOT / "src") not in report["skillweave_file"], report


class TestExternalAdaptersRegisterWithoutVendorEdit:
    def test_builtin_registry_does_not_know_the_external_ids(self, installed_core):
        report = _probe(installed_core)
        for adapter_id in EXTERNAL_IDS:
            assert adapter_id not in report["builtin_ids"], report

    def test_external_ids_register_through_the_public_registry(self, installed_core):
        report = _probe(installed_core)
        for adapter_id in EXTERNAL_IDS:
            assert adapter_id in report["registered_ids"], report


class TestExternalAdaptersAreResolvableAndInvocable:
    def test_external_search_returns_a_typed_success_with_a_real_url(
        self, installed_core
    ):
        search = _probe(installed_core)["search"]
        assert search["status"] == "success", search
        assert search["urls"], "an empty-success search must not be reported"
        assert all(u.startswith("http") for u in search["urls"]), search

    def test_external_search_receipt_binds_and_carries_no_authority(
        self, installed_core
    ):
        search = _probe(installed_core)["search"]
        receipt = search["receipt"]
        assert receipt["adapter_id"] == "external-synthetic-search"
        assert receipt["operation"] == "search.query"
        assert receipt["domain_contract"] == "search-outcome/v1"
        assert len(receipt["descriptor_digest"]) == 64, receipt
        assert search["self_approving"] is False
        assert search["is_authority"] is False

    def test_generic_tool_invokes_through_the_same_facade(self, installed_core):
        generic = _probe(installed_core)["generic"]
        assert generic["status"] == "success", generic
        assert generic["payload"] == {"echo": "hello"}, generic


class TestInstalledFailureCounterexamplesFailClosed:
    def test_unknown_adapter_id_is_refused(self, installed_core):
        refusal = _probe(installed_core)["unknown_adapter"]
        assert refusal != "NOT_REFUSED", "an unknown id must not be served"
        assert refusal["type"] == "UnknownAdapterError", refusal

    def test_malformed_descriptor_payload_is_refused(self, installed_core):
        refusal = _probe(installed_core)["malformed_descriptor"]
        assert refusal != "NOT_REFUSED", "a malformed payload must not load"
        assert refusal["type"] == "AdapterDescriptorError", refusal

    def test_unapproved_adapter_is_refused(self, installed_core):
        refusal = _probe(installed_core)["unapproved"]
        assert refusal != "NOT_REFUSED", "an unapproved adapter must not run"
        assert refusal["asset"] == "allowlist", refusal
