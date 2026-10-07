"""PEP 639 license metadata for the skillweave distribution (SW-LIC-001).

Every assertion here is made against a *built artifact* opened from outside
the source checkout -- the wheel and sdist are built into a scratch dir and
then read back with ``zipfile``/``tarfile``. Nothing is asserted against the
in-tree ``pyproject.toml`` alone, because a declared field proves nothing
about what an installer or a license scanner actually receives.

Criterion 1 (SW-LIC-001): canonical Apache text comparison + local license
scanner result; any normalization preserves every license term and the
copyright/NOTICE content.
Criterion 2: SPDX ``license`` + ``license-files`` on a setuptools floor that
supports them (>= 77.0.0); candidate wheel and sdist build successfully.
Criterion 3: built METADATA ``License-Expression`` and packaged license
files identify Apache-2.0 with no contradictory private/commercial term.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CANONICAL = FIXTURES / "apache-2.0-canonical.txt"

# Point Python's tempfile at the caller-provided private scratch, so no
# build output ever lands in /tmp or in the source checkout.
_scratch = os.environ.get("SW_LICENSE_SCRATCH")
if _scratch:
    _scratch_path = Path(_scratch)
    _scratch_path.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(_scratch_path)

# The nine numbered sections plus the appendix. A license scanner keys on
# these; a truncated or reworded body loses one and stops matching.
REQUIRED_SECTIONS = (
    "1. Definitions.",
    "2. Grant of Copyright License.",
    "3. Grant of Patent License.",
    "4. Redistribution.",
    "5. Submission of Contributions.",
    "6. Trademarks.",
    "7. Disclaimer of Warranty.",
    "8. Limitation of Liability.",
    "9. Accepting Warranty or Additional Liability.",
    "END OF TERMS AND CONDITIONS",
    "APPENDIX: How to apply the Apache License to your work.",
)

# Terms that would contradict an Apache-2.0 grant if they shipped in the
# metadata or the license files. Checked case-insensitively.
CONTRADICTORY_TOKENS = (
    "proprietary",
    "all rights reserved",
    "non-commercial",
    "noncommercial",
    "commercial license",
    "evaluation only",
    "license-ref-",
)

NOTICE_REQUIRED = ("SkillWeave", "Copyright 2026 LangeVC.com")


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    """Build wheel + sdist offline in a scratch dir; yield the paths.

    Uses ``--no-isolation`` so the build resolves ``setuptools`` from the
    running interpreter (>= 77.0.0 required by pyproject). An isolated
    build would need network to fetch the floor, which this lane forbids.
    """
    import setuptools

    floor = tuple(int(p) for p in setuptools.__version__.split(".")[:2])
    if floor < (77, 0):
        pytest.skip(
            "setuptools %s cannot honour PEP 639 license/license-files; "
            "need >= 77.0.0" % setuptools.__version__
        )

    out = tmp_path_factory.mktemp("dist")
    build_root = tmp_path_factory.mktemp("src") / "tree"
    # Copy the tree so the build never writes into the checkout.
    shutil.copytree(
        REPO_ROOT,
        build_root,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info"),
    )
    env = dict(os.environ)
    env["HOME"] = str(tmp_path_factory.mktemp("home"))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "build", "--no-isolation",
         "--wheel", "--sdist", "--outdir", str(out), str(build_root)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        "offline build failed:\nSTDOUT:\n%s\nSTDERR:\n%s"
        % (result.stdout[-4000:], result.stderr[-4000:])
    )
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    assert wheels, "no wheel produced"
    assert sdists, "no sdist produced"
    return {"wheel": wheels[0], "sdist": sdists[0], "stdout": result.stdout}


def _wheel_metadata(whl):
    with zipfile.ZipFile(whl) as z:
        name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        return z.read(name).decode("utf-8")


def _wheel_license_bytes(whl, filename):
    with zipfile.ZipFile(whl) as z:
        hits = [n for n in z.namelist()
                if n.startswith("skillweave") and n.endswith("dist-info/licenses/" + filename)]
        assert hits, "%s not packaged in the wheel" % filename
        return z.read(hits[0])


@pytest.fixture(scope="module")
def installed(artifacts, tmp_path_factory):
    """Install the BUILT wheel into a target dir and return that tree.

    Installing the wheel (not the source tree) needs no build backend and no
    network, so it works offline and proves the *installed origin* -- the
    METADATA and license files a consumer actually receives on ``sys.path``
    -- not just the bytes inside the wheel zip.
    """
    target = tmp_path_factory.mktemp("installed")
    env = dict(os.environ)
    env["HOME"] = str(tmp_path_factory.mktemp("installhome"))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pip", "install", "--no-deps",
         "--no-index", "--no-cache-dir", "--target", str(target),
         str(artifacts["wheel"])],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, (
        "offline wheel install failed:\n%s\n%s"
        % (result.stdout[-3000:], result.stderr[-3000:])
    )
    dist_infos = sorted(target.glob("skillweave-*.dist-info"))
    assert dist_infos, "installed skillweave dist-info missing in %s" % target
    return {"root": target, "dist_info": dist_infos[0]}


# --- Criterion 1: canonical comparison + local license metadata scan ------

def test_license_matches_canonical_apache_text():
    """The shipped LICENSE equals canonical Apache-2.0 with only the two
    documented normalizations applied: the leading blank line of the
    upstream body is dropped and the copyright placeholder is filled in."""
    canonical = CANONICAL.read_text(encoding="utf-8")
    expected = canonical[1:] if canonical.startswith("\n") else canonical
    assert expected.count("Copyright [yyyy] [name of copyright owner]") == 1
    expected = expected.replace(
        "Copyright [yyyy] [name of copyright owner]",
        "Copyright 2026 LangeVC.com",
        1,
    )
    actual = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert actual == expected, "LICENSE diverges from canonical Apache-2.0"

def test_license_retains_every_section_and_copyright():
    body = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert section in body, "canonical section missing: %r" % section
    assert body.rstrip().endswith("limitations under the License.")
    assert "Copyright 2026 LangeVC.com" in body

def test_local_license_scanner_identifies_apache(spdx_available):
    """Local license scanner result.

    The offline environment carries no third-party scanner (scancode /
    askalono / licensee / reuse are all absent -- see the recorded probe).
    The available local detection engine is the PyPA ``packaging`` SPDX
    implementation; it normalizes the declared expression against the SPDX
    license list and rejects anything not on it, which is exactly the
    ``License-Expression`` check PEP 639 requires. The Apache-2.0 body is
    additionally matched structurally above.
    """
    canonicalize = spdx_available.canonicalize
    invalid = spdx_available.invalid
    # Positive control: a registered SPDX id round-trips.
    assert canonicalize("Apache-2.0") == "Apache-2.0"
    # Negative control: the scanner must *reject* a syntactically valid but
    # unregistered license id, proving it is a real validator against the
    # SPDX list and not a permissive pass-through.
    with pytest.raises(invalid):
        canonicalize("NotARealSpdxLicense-1.0")
    # Guard against a false negative control: `LicenseRef-*` is a documented
    # escape hatch for *custom* licenses and is accepted by design, so it is
    # deliberately NOT used as the rejection sample above.
    assert canonicalize("LicenseRef-NotOnTheSpdxList") == "LicenseRef-NotOnTheSpdxList"

def test_scanner_probe_reproducible_and_hash_pinned():
    """Criterion 1: the offline scanner-availability probe is a real,
    reproducible artifact. Run it here and prove its result line and hash
    match the recorded evidence, so the 'no scanner present' hold is
    verifiable rather than asserted."""
    probe = Path(__file__).resolve().parent / "probe_scanners.py"
    assert probe.is_file(), "probe script missing: %s" % probe
    result = subprocess.run(
        [sys.executable, "-B", str(probe)],
        capture_output=True, text=True, check=True,
    )
    assert "SCANNER_AVAILABLE=" in result.stdout, result.stdout
    record = json.loads(
        (Path(__file__).resolve().parent / "external-license-evidence.json")
        .read_text("utf-8")
    )
    recorded = record["scanner_probe"]["result_line"]
    assert recorded in result.stdout, (recorded, result.stdout[-1000:])
    digest = hashlib.sha256(probe.read_bytes()).hexdigest()
    assert digest == record["scanner_probe"]["script_sha256"], digest


def test_notice_present_and_attributive():
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    for token in NOTICE_REQUIRED:
        assert token in notice, "NOTICE missing %r" % token


# --- Criterion 2: build succeeds on a PEP 639 backend ---------------------

def test_declared_config_uses_pep639_fields():
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    assert data["project"]["license"] == "Apache-2.0"
    files = data["project"]["license-files"]
    assert "LICENSE" in files and "NOTICE" in files
    requires = data["build-system"]["requires"]
    floor = next(r for r in requires if r.startswith("setuptools"))
    m = re.search(r"setuptools>=([0-9]+)\.([0-9]+)", floor)
    assert m, "setuptools floor not declared: %r" % floor
    assert (int(m.group(1)), int(m.group(2))) >= (77, 0)
    # PEP 639 forbids a License:: classifier beside an SPDX license field.
    assert not any(
        c.startswith("License ::") for c in data["project"]["classifiers"]
    )

def test_wheel_and_sdist_build(artifacts):
    assert artifacts["wheel"].suffix == ".whl"
    assert artifacts["sdist"].name.endswith(".tar.gz")
    assert "Successfully built" in artifacts["stdout"]


# --- Criterion 3: inspect BUILT artifacts from outside the checkout -------

def test_wheel_metadata_declares_spdx_expression(artifacts, spdx_available):
    from packaging.metadata import Metadata

    meta = Metadata.from_email(_wheel_metadata(artifacts["wheel"]), validate=True)
    assert meta.metadata_version == "2.4"
    assert meta.license_expression == "Apache-2.0"
    assert spdx_available.canonicalize(meta.license_expression) == "Apache-2.0"
    assert set(meta.license_files or ()) == {"LICENSE", "NOTICE"}
    # The legacy free-text field and the deprecated classifier must be gone.
    assert meta.license is None
    assert not [c for c in (meta.classifiers or []) if str(c).startswith("License ::")]

def test_wheel_packages_canonical_license_and_notice(artifacts):
    src_license = (REPO_ROOT / "LICENSE").read_bytes()
    src_notice = (REPO_ROOT / "NOTICE").read_bytes()
    assert _wheel_license_bytes(artifacts["wheel"], "LICENSE") == src_license
    assert _wheel_license_bytes(artifacts["wheel"], "NOTICE") == src_notice

def test_sdist_packages_canonical_license_and_notice(artifacts):
    with tarfile.open(artifacts["sdist"]) as tf:
        names = tf.getnames()
        lic = next(n for n in names if n.endswith("/LICENSE"))
        notice = next(n for n in names if n.endswith("/NOTICE"))
        pkginfo = next(n for n in names if n.endswith("/PKG-INFO"))
        assert tf.extractfile(lic).read() == (REPO_ROOT / "LICENSE").read_bytes()
        assert tf.extractfile(notice).read() == (REPO_ROOT / "NOTICE").read_bytes()
        body = tf.extractfile(pkginfo).read().decode("utf-8")
    assert "License-Expression: Apache-2.0" in body
    assert "License-File: LICENSE" in body
    assert "License-File: NOTICE" in body

def test_installed_distribution_identifies_apache_from_outside_checkout(installed, spdx_available):
    """Criterion 3, consumer wiring: read the METADATA and license files from
    an *installed* tree, outside the checkout, exactly as an installer/scanner
    sees them."""
    from packaging.metadata import Metadata

    dist_info = installed["dist_info"]
    raw = (dist_info / "METADATA").read_text("utf-8")
    meta = Metadata.from_email(raw, validate=True)
    assert meta.metadata_version == "2.4"
    assert meta.license_expression == "Apache-2.0"
    assert spdx_available.canonicalize(meta.license_expression) == "Apache-2.0"
    assert set(meta.license_files or ()) == {"LICENSE", "NOTICE"}
    # Installed origin: the license files sit beside the installed METADATA.
    assert (dist_info / "licenses" / "LICENSE").read_bytes() == (REPO_ROOT / "LICENSE").read_bytes()
    assert (dist_info / "licenses" / "NOTICE").read_bytes() == (REPO_ROOT / "NOTICE").read_bytes()
    # The installed package is importable from the foreign target, and this
    # METADATA is the real thing on sys.path, not read from the checkout.
    assert "@" not in str(installed["root"].resolve())
    assert (installed["root"] / "skillweave" / "__init__.py").is_file()


def test_no_contradictory_restriction_in_artifacts(artifacts):
    """Both artifacts identify Apache-2.0 and introduce no private or
    commercial restriction contradicting it."""
    with tarfile.open(artifacts["sdist"]) as tf:
        sdist_blob = b"".join(
            tf.extractfile(n).read()
            for n in tf.getnames()
            if n.endswith(("/PKG-INFO", "/LICENSE", "/NOTICE"))
        )
    blob = (_wheel_metadata(artifacts["wheel"]).encode("utf-8")
            + _wheel_license_bytes(artifacts["wheel"], "LICENSE")
            + _wheel_license_bytes(artifacts["wheel"], "NOTICE")
            + sdist_blob).decode("utf-8", "replace").lower()
    found = [t for t in CONTRADICTORY_TOKENS if t in blob]
    assert not found, "contradictory terms present: %r" % found


# --- Criterion 4: NOASSERTION stays unverified external evidence ----------

def test_external_evidence_recorded_and_unverified():
    record = json.loads(
        (Path(__file__).resolve().parent / "external-license-evidence.json")
        .read_text("utf-8")
    )
    assert record["reported_observation"] == "NOASSERTION"
    assert record["status"] == "unverified_external"
    assert record["mirrored_revision_checked"] is False
    assert record["action_taken"] == "none"
    assert record["transfer_or_push_or_retag"] is False
    assert record["cause_status"] == "not_determined"
    assert record["carry_to"] == "mirror/publication checkpoint"
    # No publication/transfer method was used to "test" detection.
    assert record["forbidden_test_methods_not_used"]
    # Pinned to the accepted pre-change base revision (immutable), so this
    # assertion is stable across the candidate commit that adds this file.
    base = record["base_revision"]
    resolved = subprocess.run(
        ["git", "rev-parse", base], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert resolved == base
    # The base revision is in the history of the revision under test (true
    # both before and after this lane's bounded commit).
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, "HEAD"], cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    assert ancestor.returncode == 0, "base %s is not an ancestor of HEAD" % base


class SpdxScanner:
    """The offline SPDX validator actually available in this lane.

    ``packaging.licenses`` is the PyPA SPDX engine. ``canonicalize``
    normalizes a license expression against the SPDX list and raises
    ``invalid`` for any id that is not registered (``LicenseRef-*`` custom
    refs are documented escape hatches and are accepted by design).
    """

    def __init__(self, canonicalize, invalid):
        self.canonicalize = canonicalize
        self.invalid = invalid


@pytest.fixture(scope="module")
def spdx_available():
    from packaging.licenses import (
        InvalidLicenseExpression,
        canonicalize_license_expression,
    )
    return SpdxScanner(canonicalize_license_expression, InvalidLicenseExpression)
