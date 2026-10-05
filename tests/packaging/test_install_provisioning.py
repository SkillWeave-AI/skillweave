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
  ``PyYAML>=6.0`` + ``skillweave-sdk==0.2.0``; ``jsonschema`` (dev) and ``mcp``
  (optional) are absent from the base install; and a missing optional capability
  yields an actionable ``pip install 'skillweave[mcp]'`` diagnostic (exit 1).
* criterion 3 -- the declared ``requires-python >=3.9`` and Python classifiers
  are preserved, so no breaking support change is silently introduced.
* criterion 5 -- the *composed* installed smoke that consumes qualified
  SW-1510-CLI semantics is explicitly **HELD**; this suite asserts the hold is
  recorded rather than fabricating a composed pass.

Source and build artifacts never touch the checkout: the source is copied into
scratch first, and every build/install happens under ``tmp_path``. The media may
not contain PyYAML (a needed base dependency); when a needed local dependency is
genuinely unavailable the affected test is skipped with an explicit setup-hold
reason -- never weakened into a semantic false pass.
"""

import json
import os
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


def _clean_env(tmp_path: Path) -> dict:
    """Environment with PYTHONPATH removed and temp dirs pinned to the sandbox.

    ``PYTHONPATH`` must be *unset*, not merely empty, so the subprocess cannot
    import the checkout's ``src`` -- only the installed package.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    tmp = tmp_path / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    return env


def _run(cmd, *, cwd, env):
    return subprocess.run(
        [str(part) for part in cmd], cwd=str(cwd), env=env,
        capture_output=True, text=True,
    )


def _seed_bare_venv(venv_dir: Path) -> None:
    """Make a bare venv able to build offline by copying named packages in.

    Offline environments cannot let pip resolve the build backend, and a
    ``--system-site-packages`` venv would leak ambient packages (including a
    possibly-installed ``mcp``) into the very base install we are proving is
    minimal. Seeding by copy keeps the venv's ``sys.path`` closed.
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


def _make_venv(tmp_path: Path, name: str) -> Path:
    """A bare venv, seeded so an offline no-isolation install can build."""
    venv_dir = tmp_path / name
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    _seed_bare_venv(venv_dir)
    return venv_dir


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
    assert base == ["PyYAML>=6.0", "skillweave-sdk==0.2.0"], base
    assert "Requires-Python: >=3.9" in metadata

    extras = {line[len("Provides-Extra: "):] for line in metadata.splitlines()
              if line.startswith("Provides-Extra: ")}
    assert {"dev", "mcp"} <= extras, extras

    # mcp and jsonschema are optional, never base.
    assert any(r.startswith("mcp") and 'extra == "mcp"' in r for r in requires), requires
    assert all("jsonschema" not in r for r in base), base

    ep_lines = {line.strip() for line in entry_points.splitlines()}
    assert "skillweave = skillweave.cli.main:main" in ep_lines, ep_lines


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
    assert base == {"PyYAML>=6.0", "skillweave-sdk==0.2.0"}, base


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


# ── criterion 2: actionable optional-capability diagnostic ──────────────────


def test_missing_optional_capability_reports_actionable_hint(tmp_path, wheel):
    """criterion 2: with no MCP SDK the diagnostic exits 1 and names the exact
    ``pip install 'skillweave[mcp]'`` remedy -- degradation is not silent."""
    venv_dir = _make_venv(tmp_path, "diag")
    _install_wheel(venv_dir, wheel, tmp_path)
    env = _clean_env(tmp_path)
    cwd = _unrelated_cwd(tmp_path)

    script = REPO_ROOT / "scripts" / "check-optional-capability.py"
    proc = _run([_python(venv_dir), str(script)], cwd=cwd, env=env)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)

    report = json.loads(proc.stdout)
    entry = report["capabilities"]["mcp"]
    assert entry["status"] == "missing"
    assert entry["available"] is False
    assert entry["install_hint"] == "pip install 'skillweave[mcp]'"
    assert entry["module_path"] is None
    assert report["missing"] == ["mcp"]

    # The hint is the *declared extra*, so `pip install` would in fact resolve.
    with zipfile.ZipFile(wheel) as zf:
        metadata = zf.read(next(n for n in zf.namelist() if n.endswith("METADATA"))).decode()
    assert "Provides-Extra: mcp" in metadata
    assert 'extra == "mcp"' in metadata

    unknown = _run([_python(venv_dir), str(script), "not-a-capability"], cwd=cwd, env=env)
    assert unknown.returncode == 2, unknown.returncode


# ── criterion 5: the composed gate is HELD, not faked ───────────────────────


def test_composed_cli_smoke_is_explicitly_held():
    """criterion 5: provisioning is repaired, but the *composed* installed smoke
    that consumes qualified SW-1510-CLI semantics is a held gate. This asserts
    the hold is documented -- it must never be reported as PASS here."""
    doc = (REPO_ROOT / "docs" / "provisioning.md").read_text()
    assert "Held composition gate" in doc
    assert "SW-1510-CLI" in doc
    assert "HELD" in doc or "HOLD" in doc
