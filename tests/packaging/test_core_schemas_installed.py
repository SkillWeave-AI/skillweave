"""Installed-artifact proof that the Core runtime schemas ship in the wheel.

``skillweave.gates.integration_gate_result.load_schema()`` (and its four
siblings) is a **runtime** loader: the installed package must be able to read
the schema it speaks. The 1.5.11 candidate resolved the schema as
``Path(__file__).resolve().parents[3] / "schemas" / ...`` — a path that escapes
the installed package to a repository-root ``schemas/`` directory that does not
exist once the wheel is installed, so ``load_schema()`` raised
``FileNotFoundError`` inside the installed distribution.

The fix ships the schemas as ``skillweave/assets/schemas/`` package data and
anchors every loader to package data. This module proves it on the *artifacts*
the release actually publishes: the built wheel and the built sdist must both
carry ``skillweave/assets/schemas/<name>.schema.json``, byte-identical to the
repository ``schemas/`` file, and after installing the wheel into an isolated
target the loaders must resolve from the installed package (not from the
checkout) when launched from an unrelated working directory.

On the frozen base these assertions fail: the wheel carries no
``skillweave/assets/schemas/`` subtree at all and the installed
``load_schema()`` raises ``FileNotFoundError``.
"""

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The Core runtime schemas an installed package loads by name. Each must ride
#: along as package data; the list is explicit so a dropped schema is a failure
#: rather than a silent shrink of the shipped surface.
PACKAGED_SCHEMA_FILENAMES = (
    "assessment-receipt.schema.json",
    "controller-handoff.schema.json",
    "discovery-trace.schema.json",
    "dual-review-attestation.schema.json",
    "harness-controller-attestation.schema.json",
    "integration-gate-result.schema.json",
)

#: Where package data lives inside the built artifacts.
PACKAGE_DATA_DIR = "skillweave/assets/schemas"

#: The schema ids the loaders must return, proving the bytes loaded are the
#: intended contract and not a same-named file from elsewhere.
EXPECTED_SCHEMA_IDS = {
    "integration-gate-result.schema.json": "https://skillweave.dev/schemas/integration-gate-result/v1",
    "dual-review-attestation.schema.json": "https://skillweave.dev/schemas/dual-review-attestation/v1",
    "discovery-trace.schema.json": "https://skillweave.dev/schemas/discovery-trace/v1",
    "assessment-receipt.schema.json": "https://skillweave.dev/schemas/assessment-receipt/v1",
    "controller-handoff.schema.json": "https://skillweave.dev/schemas/controller-handoff/v2",
    "harness-controller-attestation.schema.json": "https://skillweave.dev/schemas/harness-controller-attestation/v1",
}


def _build_artifacts(target_dir: Path) -> "tuple[Path, Path]":
    """Build the wheel and the sdist into ``target_dir`` and return both paths.

    A throwaway builder venv is used so the environment's own site-packages
    cannot leak an editable install into the build; the build itself runs with
    the toolchain's default isolation, exactly as a release build does.
    """
    venv_dir = target_dir / "builder"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    pip = venv_dir / "bin" / "pip"
    subprocess.run(
        [str(pip), "install", "--quiet", "build"],
        check=True,
        capture_output=True,
        text=True,
    )
    out_dir = target_dir / "dist"
    proc = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-m", "build", "--outdir", str(out_dir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted(out_dir.glob("skillweave-*.whl"))
    sdists = sorted(out_dir.glob("skillweave-*.tar.gz"))
    assert len(wheels) == 1, [p.name for p in out_dir.iterdir()]
    assert len(sdists) == 1, [p.name for p in out_dir.iterdir()]
    return wheels[0], sdists[0]


def _install_wheel(target_dir: Path, wheel: Path) -> Path:
    """Install ``wheel`` into an isolated target dir; return the site root."""
    venv_dir = target_dir / "builder"
    pip = venv_dir / "bin" / "pip"
    site = target_dir / "site"
    subprocess.run(
        [str(pip), "install", "--no-deps", "--target", str(site), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (site / "skillweave").is_dir(), f"installed skillweave missing under {site}"
    return site


def _build_and_install(target_dir: Path) -> Path:
    wheel, _sdist = _build_artifacts(target_dir)
    return _install_wheel(target_dir, wheel)


_DRIVER_IMPORTS = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "import skillweave\n"
    "from skillweave.gates import integration_gate_result as igr\n"
    "from skillweave.blueprint.discovery_trace import load_schema as load_discovery\n"
    "from skillweave.gates.attestation import load_schema as load_attestation\n"
    "from skillweave.assessment_contracts import load_schema as load_assessment\n"
    "from skillweave.runtime.controller_handoff import load_handoff_schema\n"
)


def _run_driver(target_dir: Path, foreign_cwd: Path, body: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(target_dir / "site")
    env["SW_INSTALLED_SITE"] = str(target_dir / "site")
    return subprocess.run(
        [sys.executable, "-c", _DRIVER_IMPORTS + body],
        cwd=str(foreign_cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_core_schemas_ship_in_wheel_and_sdist(tmp_path):
    """Both published artifacts carry every Core runtime schema as package data,
    byte-identical to the repository ``schemas/`` file it is generated from."""
    wheel, sdist = _build_artifacts(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
        for filename in PACKAGED_SCHEMA_FILENAMES:
            member = f"{PACKAGE_DATA_DIR}/{filename}"
            assert member in wheel_names, (
                f"{filename} is not packaged in the wheel; the runtime loader "
                f"cannot resolve it inside an installed distribution"
            )
            assert archive.read(member) == (REPO_ROOT / "schemas" / filename).read_bytes(), (
                f"packaged {filename} in the wheel drifted from schemas/{filename}"
            )

    with tarfile.open(sdist) as archive:
        sdist_names = set(archive.getnames())
        for filename in PACKAGED_SCHEMA_FILENAMES:
            matches = [n for n in sdist_names if n.endswith(f"{PACKAGE_DATA_DIR}/{filename}")]
            assert matches, f"{filename} is not packaged in the sdist: {PACKAGE_DATA_DIR}/{filename}"
            extracted = archive.extractfile(matches[0])
            assert extracted is not None
            assert extracted.read() == (REPO_ROOT / "schemas" / filename).read_bytes(), (
                f"packaged {filename} in the sdist drifted from schemas/{filename}"
            )


def test_installed_loaders_resolve_schemas_from_the_package(tmp_path):
    """After installing the wheel, every Core runtime loader resolves its schema
    from inside the installed package, from an unrelated working directory."""
    _build_and_install(tmp_path)
    site = (tmp_path / "site").resolve()
    src_pkg = (REPO_ROOT / "src" / "skillweave").resolve()

    foreign_cwd = tmp_path / "work"
    foreign_cwd.mkdir()

    expected_ids = EXPECTED_SCHEMA_IDS
    body = (
        "installed = Path(skillweave.__file__).resolve()\n"
        f"assert installed != Path({str(src_pkg)!r}) / '__init__.py', installed\n"
        f"assert str(installed).startswith({str(site)!r}), installed\n"
        "docs = {\n"
        "    'integration-gate-result.schema.json': igr.load_schema(),\n"
        "    'dual-review-attestation.schema.json': load_attestation(),\n"
        "    'discovery-trace.schema.json': load_discovery(),\n"
        "    'assessment-receipt.schema.json': load_assessment(),\n"
        "    'controller-handoff.schema.json': load_handoff_schema('handoff'),\n"
        "    'harness-controller-attestation.schema.json': load_handoff_schema('attestation'),\n"
        "}\n"
        f"expected_ids = {expected_ids!r}\n"
        "for name, doc in docs.items():\n"
        "    assert doc['$id'] == expected_ids[name], (name, doc.get('$id'))\n"
        "packaged = Path(skillweave.__file__).resolve().parent / 'assets' / 'schemas'\n"
        "for name in expected_ids:\n"
        "    assert (packaged / name).is_file(), (packaged / name)\n"
    )
    proc = _run_driver(tmp_path, foreign_cwd, body)
    assert proc.returncode == 0, proc.stderr


def test_installed_loader_fails_closed_naming_the_packaged_path(tmp_path):
    """A packaging regression is explicit: with the packaged schema removed, the
    loader raises ``FileNotFoundError`` naming the resolved packaged path rather
    than silently resolving another checkout or a caller-relative directory."""
    _build_and_install(tmp_path)

    foreign_cwd = tmp_path / "work"
    foreign_cwd.mkdir()

    body = (
        "target = Path(os.environ['SW_INSTALLED_SITE']) / 'skillweave' / 'assets' / 'schemas' / 'integration-gate-result.schema.json'\n"
        "target.unlink()\n"
        "try:\n"
        "    igr.load_schema()\n"
        "    raise SystemExit('expected FileNotFoundError')\n"
        "except FileNotFoundError as exc:\n"
        "    assert str(target) in str(exc), (str(exc), str(target))\n"
    )
    proc = _run_driver(tmp_path, foreign_cwd, body)
    assert proc.returncode == 0, proc.stderr
