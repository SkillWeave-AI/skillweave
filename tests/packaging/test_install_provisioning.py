"""Provisioning smoke tests: installed origins, entrypoints and the base/extra boundary.

These tests build the real wheel, create genuinely fresh environments, install
the distribution *without build isolation* (so nothing reaches the network), and
then exercise the install from an unrelated working directory with ``PYTHONPATH``
removed. What they prove could not be proven from the source tree:

* criterion 1 -- a built-wheel install and an editable-dev install both resolve
  ``skillweave`` from their **installed** location (never the checkout's
  ``src``), expose the ``skillweave`` console entrypoint, and run with
  ``PYTHONPATH`` unset.
* criterion 2 -- the base distribution's declared requirements are exactly
  ``PyYAML>=6.0`` + ``skillweave-sdk==0.3.0``; ``jsonschema`` (dev) and ``mcp``
  (optional) are absent from the base install; a missing optional capability
  yields an actionable ``pip install 'skillweave[mcp]'`` diagnostic (exit 1),
  **through the packaged ``python -m skillweave.scripts.check_optional_capability``
  invocation** so the documented command works from a fresh wheel; and *every*
  non-optional runtime module under ``skillweave`` imports in the base install
  (the R1 whole-runtime-surface sweep) so a base-install import break cannot
  recur silently.
* criterion 3 -- the declared ``requires-python >=3.9`` and Python classifiers
  are preserved, so no breaking support change is silently introduced.
* criterion 5 -- the *composed* installed smoke that consumes qualified
  SW-1510-CLI semantics runs the **installed** ``skillweave`` console script
  through the accepted CLI surface and asserts the accepted semantics without
  re-implementing them (support table, async start/inspect/kill roundtrip,
  wrong-pid refusal, invalid-run-id usage error, missing-executable code). On a
  base that predates the SW-1510-CLI integration the composed gate is absent, so
  it is **skipped with an explicit reason** rather than fabricated into a pass.

Source and build artifacts never touch the checkout: the source is copied into
scratch first, and every build/install happens under ``tmp_path``. The media may
not contain PyYAML (a needed base dependency) or the pinned SDK; when a needed
local dependency is genuinely unavailable the affected test is skipped with an
explicit setup-hold reason -- never weakened into a semantic false pass.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Never copy VCS or prior build output into the scratch source copy.
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", ".venv",
)

# Top-level packages a *bare* venv needs to run this suite offline: the build
# backend (so `pip install --no-build-isolation` can build) and the one runtime
# dependency the core imports unconditionally (PyYAML).
_SEED_PACKAGES = ("setuptools", "wheel", "_distutils_hack", "yaml", "_yaml")
_SEED_DISTINFOS = ("setuptools", "wheel", "pyyaml")

# The pinned SDK is a *base* dependency. The runtime-surface sweep needs it in
# the fresh venv -- ``skillweave.runtime.authority_service`` imports
# ``skillweave_sdk.validator.AuthenticatedServiceContext`` -- so ``_seed_bare_venv``
# provides it from a real SDK source: the offline pinned wheel named by
# ``$SKILLWEAVE_SDK_DIR`` (the private dependency material / preflight dir) when
# present, else the running interpreter's own install. A stale or absent copy is
# a setup HOLD at the point of use, never a fabricated pass.
_SDK_DISTINFOS = ("skillweave_sdk", "skillweave-sdk")
_SDK_MODULE = "skillweave_sdk"
_SDK_SENTINEL = "AuthenticatedServiceContext"
# Prebuilt sdist/wheel normalized names (``skillweave-sdk``) are normalized to the
# underscore form; accept both spellings when locating a wheel.
_SDK_WHEEL_GLOBS = ("skillweave_sdk-*.whl", "skillweave-sdk-*.whl")
# This lane runs from the pinched reproducible scratch; the caller pins read-only
# inputs. The default is only a courtesy, tested for existence before use.
_SDK_DIR_ENV = "SKILLWEAVE_SDK_DIR"
_DEFAULT_SDK_DIR = "/Users/andrelange/Documents/repositories/forgejo/skillweave/skillweave-planning/.skillweave/sequences/2026-10-04-sw1510-controller-recovery/preflight/real-baseline-artifacts-0ef3ead374e24f38b4c95759a8903d3b/wheels"


def _imports_sentinel(pkg_dir: Path, module: str, sentinel: str) -> bool:
    """Whether the source file ``<pkg_dir>/<top_level>.py``-adjacent package
    actually defines ``sentinel``.

    A cheap, subprocess-free pre-check: a stale SDK reusing the same version
    string can be detected by scanning source before we trust it.
    """
    top = module.split(".")[0]
    root = pkg_dir / top
    if not root.is_dir():
        return False
    for path in root.rglob("*.py"):
        try:
            if re.search(r"\b%s\b" % re.escape(sentinel), path.read_text(errors="ignore")):
                return True
        except OSError:
            continue
    return False


def _running_sdk_source():
    """The running interpreter's own SDK install, if it provides the sentinel.

    Returns ``(site_dir, distinfo_dir)`` -- the directory that contains the
    ``skillweave_sdk`` package (added to the venv as a ``.pth``) and the existing
    ``.dist-info`` to copy -- or ``None`` when the running interpreter has no
    qualified SDK (missing, stale, or metadata-only).
    """
    import importlib.util

    spec = importlib.util.find_spec(_SDK_MODULE)
    if spec is None or not spec.submodule_search_locations:
        return None
    pkg_dir = Path(next(iter(spec.submodule_search_locations))).parent
    if not _imports_sentinel(pkg_dir, _SDK_MODULE, _SDK_SENTINEL):
        return None
    for info in pkg_dir.glob("*.dist-info"):
        name = info.name.split("-")[0]
        if name in _SDK_DISTINFOS or name.lower() == _SDK_MODULE:
            return pkg_dir, info
    return None


def _sdk_wheel():
    """The offline pinned SDK wheel to provide, or ``None`` when none exists."""
    candidates = []
    env_dir = os.environ.get(_SDK_DIR_ENV)
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(_DEFAULT_SDK_DIR))
    for directory in candidates:
        if not directory.is_dir():
            continue
        for pattern in _SDK_WHEEL_GLOBS:
            found = sorted(directory.glob(pattern))
            if found:
                return found[0]
    return None


def _provide_sdk_source(venv_dir: Path) -> None:
    """Give a fresh venv a qualified ``skillweave_sdk``, or skip with a reason.

    Order: the offline pinned wheel (installed with pip, no index, no deps) is
    preferred; otherwise the running interpreter's own qualified install is
    linked with a ``.pth`` and its ``.dist-info`` copied. Neither path mutates
    ``site-packages`` globally or installs anything shared.
    """
    site = next(venv_dir.glob("lib/python*/site-packages"))

    wheel = _sdk_wheel()
    if wheel is not None:
        proc = subprocess.run(
            [_python(venv_dir), "-m", "pip", "install", "--no-index", "--no-deps",
             "--ignore-installed", str(wheel)],
            capture_output=True, text=True,
            env=_clean_env(venv_dir.parent), cwd=str(venv_dir.parent),
        )
        if proc.returncode != 0:
            pytest.skip(
                "setup HOLD: could not install the pinned SDK wheel "
                f"{wheel.name} into the fresh venv: {proc.stderr.strip()[:200]}"
            )
        return

    running = _running_sdk_source()
    if running is not None:
        pkg_dir, distinfo = running
        (site / "skillweave_sdk.pth").write_text(str(pkg_dir) + "\n")
        shutil.copytree(distinfo, site / distinfo.name, dirs_exist_ok=True)
        return

    pytest.skip(
        "setup HOLD: no qualified skillweave_sdk source is available. Provide the "
        f"pinned SDK wheel (set ${_SDK_DIR_ENV} to a dir containing "
        "skillweave_sdk-*.whl, e.g. the private preflight "
        "real-baseline-artifacts/*/wheels) or run on an interpreter whose own "
        "install exposes skillweave_sdk.validator.AuthenticatedServiceContext. "
        "The runtime-surface sweep needs the pinned SDK and is not run without it."
    )

# The whole-runtime-surface import sweep (R1). It walks every non-optional
# module under the installed ``skillweave`` and imports it, skipping the optional
# ``mcp`` extra and the packaged test modules (which need the dev toolchain).
# ``walk_packages`` silently skips submodules whose *parent* package fails to
# import, so the sweep additionally asserts the count and reports failures as
# JSON for the test to assert on.
_SWEEP_PROBE = r'''
import importlib, json, pkgutil
import skillweave

failures = []
modules = []
for info in pkgutil.walk_packages(skillweave.__path__, "skillweave."):
    name = info.name
    parts = name.split(".")
    if "mcp" in parts or "tests" in parts or parts[-1].startswith("test_"):
        continue
    modules.append(name)
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - the sweep records every failure
        failures.append([name, type(exc).__name__ + ": " + str(exc)[:200]])

print(json.dumps({"modules": len(modules), "names": modules, "failures": failures}))
'''


def _bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _pip(venv_dir: Path) -> Path:
    return _bin_dir(venv_dir) / "pip"


def _python(venv_dir: Path) -> Path:
    return _bin_dir(venv_dir) / "python"


def _script(venv_dir: Path, name: str) -> Path:
    return _bin_dir(venv_dir) / (name + (".exe" if os.name == "nt" else ""))


def _unrelated_cwd(tmp_path: Path) -> Path:
    """A working directory that is neither the checkout nor any install root."""
    cwd = tmp_path / "unrelated-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def _clean_env(tmp_path: Path, home: Path = None) -> dict:
    """Environment with PYTHONPATH removed and temp dirs pinned to the sandbox.

    ``PYTHONPATH`` must be *unset*, not merely empty, so the subprocess cannot
    import the checkout's ``src`` -- only the installed package. ``home``, when
    given, isolates ``$HOME`` for a single test so no shared/user state is read
    or written.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    tmp = tmp_path / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    if home is not None:
        env["HOME"] = str(home)
    return env


def _run(cmd, *, cwd, env):
    return subprocess.run(
        [str(part) for part in cmd], cwd=str(cwd), env=env,
        capture_output=True, text=True,
    )


def _isolated_home(tmp_path: Path, name: str) -> Path:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _seed_bare_venv(venv_dir: Path) -> None:
    """Make a bare venv able to build offline by copying named packages in.

    Offline environments cannot let pip resolve the build backend, and a
    ``--system-site-packages`` venv would leak ambient packages (including a
    possibly-installed ``mcp``) into the very base install we are proving is
    minimal. Seeding by copy keeps the venv's ``sys.path`` closed. The pinned
    SDK is provided by ``_provide_sdk_source`` (offline wheel, else the running
    interpreter's qualified install) because it is a *base* dependency the
    runtime-surface sweep must import; a missing source is a setup HOLD.
    """
    purelib = Path(sysconfig.get_paths()["purelib"])
    site = next(venv_dir.glob("lib/python*/site-packages"))
    missing = []
    for name in _SEED_PACKAGES:
        src = purelib / name
        if src.exists():
            shutil.copytree(src, site / name, dirs_exist_ok=True)
        else:
            missing.append(name)
    for distinfo in purelib.glob("*.dist-info"):
        if distinfo.name.split("-")[0].lower() in _SEED_DISTINFOS:
            shutil.copytree(distinfo, site / distinfo.name, dirs_exist_ok=True)
    pth = purelib / "distutils-precedence.pth"
    if pth.exists():
        shutil.copy(pth, site / pth.name)
    if missing:
        pytest.skip(
            "setup HOLD: cannot seed a bare venv; the running interpreter lacks "
            f"{missing!r}. PyYAML is a declared base dependency and is required "
            "to prove a real base install. Install it and re-run."
        )
    _provide_sdk_source(venv_dir)


def _make_venv(tmp_path: Path, name: str) -> Path:
    """A bare venv, seeded so an offline no-isolation install can build."""
    venv_dir = tmp_path / name
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    _seed_bare_venv(venv_dir)
    return venv_dir


def _venv_sdk_qualified(venv_dir: Path, tmp_path: Path) -> bool:
    """Whether the *fresh venv* can import the pinned SDK's runtime symbol.

    The runtime sweep imports ``AuthenticatedServiceContext`` from
    ``skillweave_sdk.validator``; the check must run against the venv that will
    run the sweep, not the running interpreter (the earlier bug). A stale or
    missing copy in the venv is a setup HOLD.
    """
    proc = _run(
        [_python(venv_dir), "-c",
         "from skillweave_sdk.validator import AuthenticatedServiceContext"],
        cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path),
    )
    return proc.returncode == 0


@pytest.fixture(scope="module")
def source_copy(tmp_path_factory) -> Path:
    """The checkout copied into scratch, so builds never write into the repo."""
    dest = tmp_path_factory.mktemp("source") / "skillweave"
    shutil.copytree(REPO_ROOT, dest, ignore=_COPY_IGNORE)
    return dest


@pytest.fixture(scope="module")
def wheel(tmp_path_factory, source_copy) -> Path:
    """Build the real wheel from the scratch copy (no isolation, no network)."""
    outdir = tmp_path_factory.mktemp("dist")
    env = _clean_env(tmp_path_factory.mktemp("build-tmp"))
    proc = _run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-build-isolation", "--no-deps", "--no-index",
            "-w", outdir, source_copy,
        ],
        cwd=tmp_path_factory.mktemp("build-cwd"),
        env=env,
    )
    assert proc.returncode == 0, f"wheel build failed:\n{proc.stdout}\n{proc.stderr}"
    built = list(outdir.glob("skillweave-*.whl"))
    assert built, f"no wheel produced in {outdir}"
    return built[0]


def _install_wheel(venv_dir: Path, wheel_path: Path, tmp_path: Path) -> None:
    proc = _run(
        [
            _pip(venv_dir), "install", "--no-index", "--no-deps",
            "--ignore-installed", str(wheel_path),
        ],
        cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path),
    )
    assert proc.returncode == 0, f"wheel install failed:\n{proc.stdout}\n{proc.stderr}"


def _install_editable(venv_dir: Path, source: Path, tmp_path: Path) -> None:
    proc = _run(
        [
            _pip(venv_dir), "install", "--no-index", "--no-deps",
            "--no-build-isolation", "--ignore-installed", "-e", str(source),
        ],
        cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path),
    )
    assert proc.returncode == 0, f"editable install failed:\n{proc.stdout}\n{proc.stderr}"


def _origin(venv_dir: Path, tmp_path: Path) -> str:
    proc = _run(
        [
            _python(venv_dir), "-c",
            "import json, pathlib, skillweave; "
            "print(json.dumps(str(pathlib.Path(skillweave.__file__).resolve())))",
        ],
        cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _cli(venv_dir: Path, args, *, cwd, env):
    """Run the *installed* ``skillweave`` console script."""
    return _run([_script(venv_dir, "skillweave")] + list(args), cwd=cwd, env=env)


# ── criterion 1 + 2 + 3: declared wheel metadata ────────────────────────────


def test_declared_wheel_metadata_scopes_deps_and_keeps_python_support(wheel):
    """The built wheel declares exactly the base deps, the console entrypoints,
    and keeps >=3.9 support with mcp/jsonschema scoped behind extras."""
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'requires-python = ">=3.9"' in text
    assert '"Programming Language :: Python :: 3.9"' in text

    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        metadata = zf.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
        entry_points = zf.read(
            next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
        ).decode()

    requires = [line[len("Requires-Dist: "):] for line in metadata.splitlines()
                if line.startswith("Requires-Dist: ")]
    base = sorted(r for r in requires if "extra ==" not in r)
    assert base == ["PyYAML>=6.0", "skillweave-sdk==0.3.0"], base
    assert "Requires-Python: >=3.9" in metadata

    extras = {line[len("Provides-Extra: "):] for line in metadata.splitlines()
              if line.startswith("Provides-Extra: ")}
    assert {"dev", "mcp"} <= extras, extras

    # mcp and jsonschema are optional, never base.
    assert any(r.startswith("mcp") and 'extra == "mcp"' in r for r in requires), requires
    assert all("jsonschema" not in r for r in base), base

    ep_lines = {line.strip() for line in entry_points.splitlines()}
    assert "skillweave = skillweave.cli.main:main" in ep_lines, ep_lines
    assert "skillweave-mcp = skillweave.mcp.server:main" in ep_lines, ep_lines


def test_base_entry_targets_import_without_optional_capabilities(tmp_path, wheel):
    """criterion 1 + 2: every declared console-script target imports against a
    base environment that has no MCP SDK and no dev tooling."""
    venv_dir = _make_venv(tmp_path, "base-scripts")
    _install_wheel(venv_dir, wheel, tmp_path)
    env = _clean_env(tmp_path)
    probe = (
        "import importlib, importlib.util as u, json\n"
        "targets = {\n"
        "  'skillweave': ('skillweave.cli.main', 'main'),\n"
        "  'skillweave-mcp': ('skillweave.mcp.server', 'main'),\n"
        "  'skillweave-install': ('skillweave.installer', 'main'),\n"
        "  'skillweave-risk-mode': ('skillweave.scripts.resolve_risk_mode', 'main'),\n"
        "  'skillweave-interactive-mode': ('skillweave.scripts.interactive_mode', 'main'),\n"
        "}\n"
        "assert u.find_spec('mcp') is None, 'base env must not have the MCP SDK'\n"
        "assert u.find_spec('jsonschema') is None, 'base env must not have dev-only jsonschema'\n"
        "import importlib as _i\n"
        "import skillweave\n"
        "_i.import_module('skillweave')\n"
        "import sys as _s\n"
        "assert 'jsonschema' not in _s.modules and 'mcp' not in _s.modules\n"
        "out = {}\n"
        "for script, (mod, attr) in targets.items():\n"
        "    m = importlib.import_module(mod)\n"
        "    assert callable(getattr(m, attr, None)), script\n"
        "    out[script] = mod\n"
        "print(json.dumps(out))\n"
    )
    proc = _run([_python(venv_dir), "-c", probe], cwd=_unrelated_cwd(tmp_path), env=env)
    assert proc.returncode == 0, proc.stderr


def test_base_install_whole_runtime_surface_imports_cleanly(tmp_path, wheel):
    """criterion 2 (R1): every non-optional module under the *installed*
    ``skillweave`` imports in the base install (no MCP SDK, no dev extras).

    The cold review found that a stale base/SDK could hide an import break
    (``skillweave_sdk.validator.AuthenticatedServiceContext``); sweeping the
    whole runtime surface makes such a break fail loudly instead of silently.
    The pinned SDK is provided to the fresh venv by ``_seed_bare_venv`` (offline
    wheel, else the running interpreter's install); only when *no* source exists
    does ``_provide_sdk_source`` skip with an explicit setup-HOLD reason. When a
    source exists the sweep runs for real -- it is never skipped or narrowed.
    """
    venv_dir = _make_venv(tmp_path, "sweep")
    _install_wheel(venv_dir, wheel, tmp_path)
    env = _clean_env(tmp_path)
    cwd = _unrelated_cwd(tmp_path)

    # The SDK must actually be importable in THIS venv, not merely on the
    # running interpreter (the earlier bug let a stale/absent venv SDK through).
    assert _venv_sdk_qualified(venv_dir, tmp_path), (
        "the fresh venv cannot import "
        "skillweave_sdk.validator.AuthenticatedServiceContext; the sweep would "
        "be meaningless"
    )

    proc = _run([_python(venv_dir), "-c", _SWEEP_PROBE], cwd=cwd, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["modules"] > 0, report
    assert report["failures"] == [], report["failures"]
    # The exact module the cold review flagged must have been swept and clean --
    # it imports the pinned SDK, so its presence proves the venv SDK is real.
    assert "skillweave.runtime.authority_service" in report["names"], (
        "the sweep did not reach skillweave.runtime.authority_service: "
        + repr(sorted(report["names"]))
    )


# ── criterion 1: installed origins, entrypoint, no PYTHONPATH ───────────────


def test_wheel_install_runs_from_installed_location_without_pythonpath(tmp_path, wheel):
    """criterion 1: a built-wheel install resolves skillweave from the venv's
    site-packages (not the checkout) from an unrelated cwd with PYTHONPATH
    unset, exposes the console script, and reports the pinned requirements."""
    venv_dir = _make_venv(tmp_path, "wheel")
    _install_wheel(venv_dir, wheel, tmp_path)

    origin = _origin(venv_dir, tmp_path)
    assert str(venv_dir) in origin, origin
    assert "site-packages" in origin, origin
    assert str(REPO_ROOT / "src") not in origin, origin

    help_proc = _run([_script(venv_dir, "skillweave"), "--help"],
                     cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path))
    assert help_proc.returncode == 0, help_proc.stderr
    assert "usage: skillweave" in help_proc.stdout

    meta_proc = _run([_python(venv_dir), "-c",
                      "import importlib.metadata as m; print('\\n'.join(m.requires('skillweave')))"],
                     cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path))
    assert meta_proc.returncode == 0, meta_proc.stderr
    base = {line for line in meta_proc.stdout.splitlines() if "extra ==" not in line}
    assert base == {"PyYAML>=6.0", "skillweave-sdk==0.3.0"}, base


def test_editable_install_resolves_back_to_its_source_tree(tmp_path, source_copy):
    """criterion 1: an editable-dev install imports from its source tree (not a
    site-packages copy) from an unrelated cwd with PYTHONPATH unset, and its
    console script runs -- the dev loop is genuinely wired, not a stale copy."""
    venv_dir = _make_venv(tmp_path, "editable")
    _install_editable(venv_dir, source_copy, tmp_path)

    origin = _origin(venv_dir, tmp_path)
    expected = (source_copy / "src" / "skillweave" / "__init__.py").resolve()
    assert Path(origin) == expected, (origin, str(expected))
    assert "site-packages" not in origin, origin

    help_proc = _run([_script(venv_dir, "skillweave"), "--help"],
                     cwd=_unrelated_cwd(tmp_path), env=_clean_env(tmp_path))
    assert help_proc.returncode == 0, help_proc.stderr
    assert "usage: skillweave" in help_proc.stdout


# ── criterion 2: actionable optional-capability diagnostic (R2) ─────────────


def test_missing_optional_capability_reports_actionable_hint(tmp_path, wheel):
    """criterion 2 (R2): with no MCP SDK the *packaged* diagnostic runs from the
    fresh install via ``python -m skillweave.scripts.check_optional_capability``,
    exits 1 and names the exact ``pip install 'skillweave[mcp]'`` remedy -- and
    the source-tree wrapper reports the identical remedy. Degradation is not
    silent."""
    venv_dir = _make_venv(tmp_path, "diag")
    _install_wheel(venv_dir, wheel, tmp_path)
    env = _clean_env(tmp_path)
    cwd = _unrelated_cwd(tmp_path)

    packaged = _run(
        [_python(venv_dir), "-m", "skillweave.scripts.check_optional_capability", "mcp"],
        cwd=cwd, env=env,
    )
    assert packaged.returncode == 1, (packaged.returncode, packaged.stdout, packaged.stderr)
    report = json.loads(packaged.stdout)
    entry = report["capabilities"]["mcp"]
    assert entry["status"] == "missing"
    assert entry["available"] is False
    assert entry["install_hint"] == "pip install 'skillweave[mcp]'"
    assert entry["module_path"] is None
    assert report["missing"] == ["mcp"]

    unknown = _run(
        [_python(venv_dir), "-m", "skillweave.scripts.check_optional_capability", "not-a-capability"],
        cwd=cwd, env=env,
    )
    assert unknown.returncode == 2, unknown.returncode

    # The source-tree wrapper delegates to the same packaged module.
    wrapper = REPO_ROOT / "scripts" / "check-optional-capability.py"
    wrapped = _run([_python(venv_dir), str(wrapper)], cwd=cwd, env=env)
    assert wrapped.returncode == 1, (wrapped.returncode, wrapped.stdout, wrapped.stderr)
    assert json.loads(wrapped.stdout)["capabilities"]["mcp"]["install_hint"] == \
        "pip install 'skillweave[mcp]'"

    # The hint is the *declared extra*, so `pip install` would in fact resolve.
    with zipfile.ZipFile(wheel) as zf:
        metadata = zf.read(next(n for n in zf.namelist() if n.endswith("METADATA"))).decode()
    assert "Provides-Extra: mcp" in metadata
    assert 'extra == "mcp"' in metadata


# ── criterion 5: composed installed CLI smoke (SW-1510-CLI semantics) ───────

_COMPOSED_SKIP = (
    "composed gate not present on this base: the built wheel exposes no "
    "skillweave.cli.support surface (it predates the SW-1510-CLI integration). "
    "Run this suite on the SW-1510-CLI-composed head to exercise the composed "
    "installed smoke."
)


@pytest.fixture(scope="module")
def composed_install(tmp_path_factory, wheel) -> Path:
    """A fresh wheel install that carries the SW-1510-CLI surface, else skip.

    The composed gate must run against the *installed* console script, not the
    checkout. When the built wheel has no ``skillweave.cli.support`` module the
    SW-1510-CLI semantics are simply not present on this base, so the gate is
    skipped with an explicit reason -- never reported as a fabricated pass.
    """
    tmp_path = tmp_path_factory.mktemp("composed")
    venv_dir = _make_venv(tmp_path, "venv")
    _install_wheel(venv_dir, wheel, tmp_path)
    env = _clean_env(tmp_path)
    cwd = _unrelated_cwd(tmp_path)
    probe = _run(
        [_python(venv_dir), "-c",
         "import importlib.util as u; "
         "print(u.find_spec('skillweave.cli.support') is not None)"],
        cwd=cwd, env=env,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "True":
        pytest.skip(_COMPOSED_SKIP)
    return venv_dir


def _composed_case(composed_install: Path, tmp_path: Path, name: str):
    """One isolated case: its own HOME and project dir, PYTHONPATH unset."""
    home = _isolated_home(tmp_path, name + "-home")
    proj = tmp_path / (name + "-proj")
    proj.mkdir(parents=True, exist_ok=True)
    env = _clean_env(tmp_path, home=home)
    return composed_install, proj, env


def _sleep_command(venv_dir: Path):
    """A harmless, portable command the async run can hold open briefly."""
    return [str(_python(venv_dir)), "-c", "import time; time.sleep(30)"]


def test_composed_support_table_lists_the_accepted_surfaces(composed_install, tmp_path):
    """criterion 5: the installed CLI ``--help`` exposes the async surfaces and
    the installed ``skillweave support`` prints the versioned
    ``skillweave.support/1`` table naming entry/run/start/inspect/kill/cancel/
    resume/mcp/dispatch -- read from the table, not re-derived here."""
    venv_dir, proj, env = _composed_case(composed_install, tmp_path, "support")

    help_proc = _cli(venv_dir, ["--help"], cwd=proj, env=env)
    assert help_proc.returncode == 0, help_proc.stderr
    for surface in ("support", "start", "inspect", "kill"):
        assert surface in help_proc.stdout, help_proc.stdout

    table_proc = _cli(venv_dir, ["support", "--json"], cwd=proj, env=env)
    assert table_proc.returncode == 0, table_proc.stderr
    table = json.loads(table_proc.stdout)
    assert table["schema_id"] == "skillweave.support/1", table.get("schema_id")
    names = {row["name"] for row in table["surfaces"]}
    assert names == {
        "entry", "run", "start", "inspect", "kill", "cancel", "resume",
        "mcp", "dispatch",
    }, names
    # The shared rule and exit table are the accepted ones, published once.
    assert table["run_id_rule"] == r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    assert table["exit_codes"]["invalid_input"] == 2
    assert table["exit_codes"]["command_not_found"] == 127


def test_composed_async_start_inspect_kill_roundtrip(composed_install, tmp_path):
    """criterion 5: an async run starts, inspects consistently, refuses a wrong
    PID (exit 2) and accepts the recorded PID, and the killed run projects to
    the ``advance_or_stop`` lane -- the accepted lifecycle semantics, observed
    through the installed console script."""
    venv_dir, proj, env = _composed_case(composed_install, tmp_path, "roundtrip")
    run_id = "composed0001"

    start = _cli(venv_dir, ["start", "--async", "--run-id", run_id, "--",
                            *_sleep_command(venv_dir)], cwd=proj, env=env)
    assert start.returncode == 0, start.stderr
    started = json.loads(start.stdout)
    assert started["run_id"] == run_id
    assert started["state"] == "running"
    pid = started["pid"]
    assert isinstance(pid, int) and pid > 0

    insp1 = _cli(venv_dir, ["inspect", run_id], cwd=proj, env=env)
    assert insp1.returncode == 0, insp1.stderr
    snap1 = json.loads(insp1.stdout)
    assert snap1["state"] == "running"
    assert snap1["lane_states"] == {run_id: "preflight"}, snap1["lane_states"]
    assert snap1["lane_state"] == "preflight"

    wrong = _cli(venv_dir, ["kill", run_id, str(pid + 1)], cwd=proj, env=env)
    assert wrong.returncode == 2, (wrong.returncode, wrong.stdout, wrong.stderr)
    assert "Traceback" not in wrong.stderr

    right = _cli(venv_dir, ["kill", run_id, str(pid)], cwd=proj, env=env)
    assert right.returncode == 0, (right.returncode, right.stdout, right.stderr)
    assert json.loads(right.stdout)["state"] == "killed"

    insp2 = _cli(venv_dir, ["inspect", run_id], cwd=proj, env=env)
    assert insp2.returncode == 0, insp2.stderr
    snap2 = json.loads(insp2.stdout)
    assert snap2["state"] == "killed"
    assert snap2["lane_states"] == {run_id: "advance_or_stop"}, snap2["lane_states"]
    assert snap2["lane_state"] == "advance_or_stop"


def test_composed_invalid_run_id_is_a_usage_error(composed_install, tmp_path):
    """criterion 5: an invalid run id (path traversal) is refused with exit 2 on
    ``start``/``inspect``/``kill`` -- the shared run-id rule, not a per-surface
    ad-hoc check, and never a raw traceback."""
    venv_dir, proj, env = _composed_case(composed_install, tmp_path, "badid")

    cases = [
        ["start", "--async", "--run-id", "../x", "--", *_sleep_command(venv_dir)],
        ["inspect", "../x"],
        ["kill", "../x", "1"],
    ]
    for argv in cases:
        proc = _cli(venv_dir, argv, cwd=proj, env=env)
        assert proc.returncode == 2, (argv, proc.returncode, proc.stdout, proc.stderr)
        assert "invalid run id" in proc.stderr, proc.stderr
        assert "Traceback" not in proc.stderr


def test_composed_missing_executable_is_reported_as_127(composed_install, tmp_path):
    """criterion 5: a command that cannot be launched is reported with the
    documented ``command_not_found`` code (127) and a message, with no Python
    traceback and no silent ``running`` record."""
    venv_dir, proj, env = _composed_case(composed_install, tmp_path, "nobin")
    missing = str(tmp_path / "definitely-not-a-real-binary-xyz")

    proc = _cli(venv_dir, ["start", "--async", "--run-id", "nobin0001", "--", missing],
                cwd=proj, env=env)
    assert proc.returncode == 127, (proc.returncode, proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["state"] == "failed"
    assert "command not found" in payload["error"]
