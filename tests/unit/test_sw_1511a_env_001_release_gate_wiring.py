"""Adversarial tests: the SW-1511A-ENV-001 module must be wired into the gate.

One test per touched criterion. Every test drives the *real production entry
point* -- ``skillweave.github_integration.release_gate.ReleaseReadinessGate`` and
``run_cli`` -- with fakes injected through its public seams, so the gate run is
what builds the declared environment, refuses a session whose collection
dependencies are missing, and writes the ``execution-environment-attestation/v1``
receipt. None of them touches the network, an index, a provider or a credential:
``pip`` is never invoked and the "built" environment is a fake venv whose
``bin/python`` is a shim that replays canned interpreter, SDK and dependency
probes without executing any real installation.

The red proof: on the frozen reviewed candidate these tests fail at the first
call -- ``check_test_environment`` and the ``evaluate``/``run_cli`` keyword seams
do not exist there, so the gate can never build, guard or receipt an environment.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

_EA_MODULE = "skillweave.release.environment_attestation"
_GATE_MODULE = "skillweave.github_integration.release_gate"


def _ea():
    """Import the candidate module, naming the deliverable on the base."""
    return importlib.import_module(_EA_MODULE)


def _gate():
    """Import the production entry point, naming the wiring on the base."""
    return importlib.import_module(_GATE_MODULE)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_sdk_dir() -> Path | None:
    """The sibling SDK checkout, resolved the way the declared input names it."""
    env = os.environ.get("SKILLWEAVE_SDK_DIR")
    if env and (Path(env) / "schema_version.toml").is_file():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "skillweave-sdk"
        if (candidate / "schema_version.toml").is_file():
            return candidate
    return None


def _require_sdk_dir() -> Path:
    sdk_dir = _find_sdk_dir()
    if sdk_dir is None:
        pytest.skip("no sibling skillweave-sdk checkout on this host")
    return sdk_dir


#: The canned observations the fake interpreter replays. It understands exactly
#: the three probe programs the module runs -- interpreter facts, the SDK probe
#: and the dependency probe -- and answers each from these tables, so a test
#: controls the environment without installing anything or touching an index.
_SHIM = r'''#!{real}
import json
import sys

SDK_FOUND = {sdk_found!r}
SDK_VERSION = {sdk_version!r}
SDK_INSTALL_TYPE = {sdk_install_type!r}
DEPENDENCIES = {dependencies!r}
SHORT = {short!r}

# A shebang invocation shifts argv, so recover the ``-c`` program the module
# actually asked for; its source identifies which of the three declared probes
# is running, and the dependency probe is the only one invoked with a JSON
# argument after the program.
argv = sys.argv[1:]
prog = argv[argv.index("-c") + 1] if "-c" in argv else ""
args = argv[argv.index("-c") + 2:] if "-c" in argv else []


def emit(payload):
    print(json.dumps(payload))
    raise SystemExit(0)


if args and prog.count("find_spec") == 1:
    names = json.loads(args[0])
    emit({{n: bool(DEPENDENCIES.get(n)) for n in names}})

if '"short"' in prog:
    emit({{"path": {interpreter!r}, "version": {version!r}, "short": SHORT}})

if '"found"' in prog:
    if not SDK_FOUND:
        emit({{"found": False}})
    emit({{
        "found": True,
        "version": SDK_VERSION,
        "package_path": {package_path!r},
        "dist_info_path": {dist_info_path!r},
        "install_type": SDK_INSTALL_TYPE,
        "pth_links": [],
    }})

sys.stderr.write("fake interpreter: unrecognised probe\n")
raise SystemExit(2)
'''


def _fake_environment(
    root: Path,
    *,
    short: str,
    version: str = "9.9.9",
    sdk_found: bool = True,
    sdk_version: str = "0.3.0",
    sdk_install_type: str = "wheel",
    dependencies: dict[str, bool] | None = None,
) -> Path:
    """A fake built environment: ``bin/python`` replays the canned probes.

    ``dependencies`` maps *import* names to availability; the default provides
    every declared import name, which is the complete-environment shape. A name
    set to ``False`` -- or simply omitted -- is a dependency this environment
    did not provide, i.e. an incomplete environment.
    """
    venv_dir = Path(root)
    venv_dir.mkdir(parents=True, exist_ok=True)
    site = venv_dir / f"lib/python{short}/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    pkg = site / "skillweave_sdk"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text(
        'SCHEMA_VERSION = "%s"\n' % sdk_version, encoding="utf-8"
    )
    (site / f"skillweave_sdk-{sdk_version}.dist-info").mkdir(exist_ok=True)
    (site / "mcp-1.0.0.dist-info").mkdir(exist_ok=True)
    (site / "setuptools-80.0.0.dist-info").mkdir(exist_ok=True)

    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True, exist_ok=True)
    name = "python.exe" if os.name == "nt" else "python"
    shim = bin_dir / name
    shim.write_text(
        _SHIM.format(
            real=sys.executable,
            interpreter=str(shim.resolve()),
            short=short,
            version=version,
            sdk_found=sdk_found,
            sdk_version=sdk_version,
            sdk_install_type=sdk_install_type,
            package_path=str(pkg),
            dist_info_path=str(site / f"skillweave_sdk-{sdk_version}.dist-info"),
            dependencies=dependencies if dependencies is not None else {},
        ),
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return venv_dir


def _python(venv_dir: Path) -> Path:
    return Path(venv_dir) / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )


@pytest.fixture(scope="module", autouse=True)
def _repo_cwd():
    """Every gate run resolves the declared input from the real checkout."""
    previous = Path.cwd()
    os.chdir(_repo_root())
    yield
    os.chdir(previous)


@pytest.fixture(scope="module")
def sdk_dir() -> Path:
    return _require_sdk_dir()


@pytest.fixture
def declared():
    """The real declared input, or a named skip when absent on this host."""
    ea = _ea()
    try:
        return ea.load_declaration(_repo_root() / "profiles" / "test-environments.v1.toml")
    except ea.EnvironmentDeclarationError as exc:  # pragma: no cover - host shape
        pytest.skip(f"declared input not loadable here: {exc}")


@pytest.fixture
def complete_deps(declared) -> dict[str, bool]:
    """Every declared distribution's import name, all provided."""
    return {
        _ea().import_name(d): True for d in _ea().declared_distributions(declared)
    }


# ── criterion 1: the gate builds the declared environment, complete-or-refuse ─


def test_criterion1_gate_builds_the_declared_environment_and_receipts_it(
    tmp_path, declared, complete_deps
):
    """A real gate run builds the declared env and attests it non-editably.

    The builder seam returns a built environment carrying the pinned SDK, so the
    gate's own ``build_attestation`` observes genuine facts: an installed
    (non-editable) SDK of the declared version, no missing declared dependency,
    and a receipt recording the environment it built. The gate is the caller --
    the module under repair is not exercised directly.
    """
    gate_mod = _gate()
    venv_dir = _fake_environment(
        tmp_path / "py-env",
        short=declared.pythons[0],
        sdk_version=declared.sdk_version,
        dependencies=complete_deps,
    )
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    check, receipt, receipts = gate.check_test_environment(
        declaration=declared,
        environments=[declared.pythons[0]],
        build=lambda minor: venv_dir,
        available=complete_deps,
        session_python=_python(venv_dir),
        sdk_dir=_require_sdk_dir(),
    )

    assert check.id == "test-environment-attestation"
    assert check.required is True
    assert check.passed, check.detail
    assert set(receipts) == {declared.pythons[0]}
    built = receipts[declared.pythons[0]]
    assert built["sdk_version"] == declared.sdk_version
    assert built["sdk_install_type"] == "wheel"
    assert not built["missing_dependencies"]
    assert built["environment_path"] == str(venv_dir.resolve())


def test_criterion1_gate_refuses_an_incomplete_built_environment(
    tmp_path, declared, complete_deps
):
    """A built environment that omits a declared dependency is refused by name.

    The negative half of criterion 1, through the gate: the builder seam returns
    a built environment whose own receipt attests that one declared dependency
    was not provided. The gate must refuse -- naming the dependency and the
    selective-ignore cause -- rather than record a partial environment as
    evidence.
    """
    gate_mod = _gate()
    incomplete = dict(complete_deps)
    incomplete["mcp"] = False
    venv_dir = _fake_environment(
        tmp_path / "py-bare",
        short=declared.pythons[0],
        sdk_version=declared.sdk_version,
        dependencies=incomplete,
    )
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    check, receipt, receipts = gate.check_test_environment(
        declaration=declared,
        environments=[declared.pythons[0]],
        build=lambda minor: venv_dir,
        available=incomplete,
        session_python=_python(venv_dir),
        sdk_dir=_require_sdk_dir(),
    )

    assert check.passed is False
    assert "selective-ignore cause" in check.detail
    assert "mcp" in check.detail
    assert receipt == {}
    assert "mcp" in receipts[declared.pythons[0]]["missing_dependencies"]


def test_criterion1_gate_refuses_a_builder_that_refuses_a_dependency(tmp_path, declared):
    """A builder that refuses a dependency is a named gate refusal.

    The builder's own complete-or-refuse contract (``EnvironmentBuildError``)
    must surface as a failed required gate check naming the refused dependency,
    never as a passing gate with no environment.
    """
    ea = _ea()
    gate_mod = _gate()
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    def refusing_builder(minor: str):
        raise ea.EnvironmentBuildError(
            f"incomplete environment for py{minor}; refused dependencies: "
            "mcp: no wheel in the declared wheelhouse"
        )

    check, receipt, receipts = gate.check_test_environment(
        declaration=declared,
        environments=[declared.pythons[0]],
        build=refusing_builder,
    )

    assert check.passed is False
    assert "mcp" in check.detail
    assert receipt == {}
    assert receipts == {}


# ── criterion 2: the gate refuses a partial session, never a silent skip ──────


def test_criterion2_gate_refuses_a_session_missing_a_collection_dependency(
    tmp_path, declared, complete_deps
):
    """The gate refuses the unit-suite session whose declared deps are missing.

    The refusal is the *selective-ignore cause* criterion 2 forbids: a session
    that cannot import a declared collection dependency must be refused by the
    gate, naming it, because ``pytest.importorskip`` would otherwise turn the
    missing dependency into a silent skip of the unit suite.
    """
    gate_mod = _gate()
    venv_dir = _fake_environment(
        tmp_path / "py-env2", short=declared.pythons[0], dependencies=complete_deps
    )
    partial = dict(complete_deps)
    partial["mcp"] = False
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    check, receipt, receipts = gate.check_test_environment(
        declaration=declared,
        environments=[declared.pythons[0]],
        build=lambda minor: venv_dir,
        available=partial,
        session_python=_python(venv_dir),
        sdk_dir=_require_sdk_dir(),
    )

    assert check.passed is False
    assert "selective-ignore cause" in check.detail
    assert "mcp" in check.detail
    assert receipt == {}
    assert receipts  # the built environment was still recorded as evidence


def test_criterion2_gate_evaluate_refuses_the_session_and_carries_no_receipt(
    tmp_path, declared, complete_deps
):
    """A full ``evaluate`` run refuses the partial session and emits no receipt.

    The end-to-end gate verdict, not the seam: ``evaluate`` with the environment
    seam active must mark the run unreleasable and leave no attestation receipt
    to be adopted as evidence of a session that was never guarded.
    """
    gate_mod = _gate()
    venv_dir = _fake_environment(
        tmp_path / "py-env3", short=declared.pythons[0], dependencies=complete_deps
    )
    partial = dict(complete_deps)
    partial["jsonschema"] = False
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    result = gate.evaluate(
        current_version="0.0.0",
        latest_tag=None,
        environments=[declared.pythons[0]],
        build=lambda minor: venv_dir,
        available=partial,
        session_python=_python(venv_dir),
        sdk_dir=_require_sdk_dir(),
    )

    env_checks = [c for c in result.checks if c.id == "test-environment-attestation"]
    assert len(env_checks) == 1
    assert env_checks[0].passed is False
    assert "selective-ignore cause" in env_checks[0].detail
    assert "jsonschema" in env_checks[0].detail
    assert result.can_release is False
    assert result.environment_attestation == {}


# ── criterion 3: the gate run writes the receipt with the eight fields ────────


def test_criterion3_gate_receipt_carries_every_required_field(
    tmp_path, declared, complete_deps
):
    """The gate's receipt records all eight criterion-3 fields, verified.

    ``verify_attestation`` -- part of the module under repair -- is exercised on
    the receipt the *gate* produced, the same object a downstream consumer
    (reviewer, acceptance ledger) reads.
    """
    ea = _ea()
    gate_mod = _gate()
    venv_dir = _fake_environment(
        tmp_path / "py-env4",
        short=declared.pythons[0],
        sdk_version=declared.sdk_version,
        dependencies=complete_deps,
    )
    sdk_dir = _require_sdk_dir()
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    check, receipt, _ = gate.check_test_environment(
        declaration=declared,
        environments=[declared.pythons[0]],
        build=lambda minor: venv_dir,
        available=complete_deps,
        session_python=_python(venv_dir),
        sdk_dir=sdk_dir,
    )

    assert check.passed, check.detail
    for field in ea.REQUIRED_RECEIPT_FIELDS:
        assert str(receipt.get(field, "")).strip(), f"gate receipt omits {field}"
    assert receipt["sdk_version"] == declared.sdk_version
    assert receipt["dependency_lock_digest"] == declared.dependency_lock_digest
    assert receipt["sdk_dir"] == str(Path(sdk_dir).resolve())
    ea.verify_attestation(receipt)


def test_criterion3_run_cli_writes_the_receipt_into_release_gate_data(
    tmp_path, declared, complete_deps
):
    """The production CLI entry writes the receipt into ``release-gate-data.json``.

    This is the exact entry the release workflow invokes
    (``python -c "from ...release_gate import run_cli; run_cli()"``): its output
    artifact must carry the eight-field ``environment_attestation`` receipt. The
    gate's own artifact write is asserted, not a private helper.
    """
    ea = _ea()
    gate_mod = _gate()
    venv_dir = _fake_environment(
        tmp_path / "py-env5",
        short=declared.pythons[0],
        sdk_version=declared.sdk_version,
        dependencies=complete_deps,
    )
    work = tmp_path / "work"
    work.mkdir()
    previous = Path.cwd()
    os.chdir(work)
    os.environ["SKILLWEAVE_SDK_DIR"] = str(_require_sdk_dir())
    try:
        rc = gate_mod.run_cli(
            ["--release-version", "0.0.0"],
            environments=[declared.pythons[0]],
            build=lambda minor: venv_dir,
            available=complete_deps,
            session_python=_python(venv_dir),
        )
    finally:
        os.chdir(previous)
        os.environ.pop("SKILLWEAVE_SDK_DIR", None)

    assert rc == 0
    data = json.loads((work / "release-gate-data.json").read_text(encoding="utf-8"))
    receipt = data["environment_attestation"]
    for field in ea.REQUIRED_RECEIPT_FIELDS:
        assert str(receipt.get(field, "")).strip(), f"artifact omits {field}"
    assert receipt["python_short"] == declared.pythons[0]
    ea.verify_attestation(receipt)
    assert (work / "release-gate-report.md").is_file()


def test_criterion3_gate_refuses_a_receipt_from_another_session(
    tmp_path, declared, complete_deps
):
    """The gate refuses to run the suite against a foreign session receipt.

    The receipt binding is a criterion-3 property of the *gate*: a caller that
    supplies a receipt whose dependency-lock digest belongs to a different
    session must be refused, so the gate never adopts an attestation that is not
    its own.
    """
    gate_mod = _gate()
    venv_dir = _fake_environment(
        tmp_path / "py-env6", short=declared.pythons[0], dependencies=complete_deps
    )
    session_python = _python(venv_dir)
    foreign = {
        "schema": _ea().ENV_ATTESTATION_SCHEMA_ID,
        "interpreter_path": str(session_python.resolve()),
        "interpreter_version": "9.9.9",
        "python_short": declared.pythons[0],
        "sdk_version": declared.sdk_version,
        "sdk_origin": "foreign",
        "sdk_install_type": "wheel",
        "dependency_lock_digest": "0" * 64,
        "sdk_dir": str(Path(_require_sdk_dir()).resolve()),
    }
    gate = gate_mod.ReleaseReadinessGate(repo_root=str(_repo_root()))

    check, receipt, _ = gate.check_test_environment(
        declaration=declared,
        environments=[declared.pythons[0]],
        build=lambda minor: venv_dir,
        available=complete_deps,
        session_python=session_python,
        sdk_dir=_require_sdk_dir(),
        receipt=foreign,
    )

    assert check.passed is False
    assert "does not match the receipt" in check.detail
    assert "dependency_lock_digest" in check.detail
    assert receipt == {}
