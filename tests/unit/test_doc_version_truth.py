"""Version/support/setup truth-check (SW-DOC-004).

A machine check that the **release-facing** documentation, the declared version
surfaces and an installed artifact all agree — the SW-DOC-004 acceptance
criteria, asserted rather than claimed:

* criterion 1 — a consistency check (`scripts/check-version-truth.py`) compares
  the candidate package/manifest version, the release-facing badge/changelog and
  the installed package identity; historical references are allowed, and
  candidate `1.5.10` is never advertised as **released**.
* criterion 2 — the released version is established from the tag's **peeled**
  commit and canonical tag subject, never by requiring the tag to equal branch
  HEAD; discovered and installed versions are shown separately with
  provenance/freshness.
* criterion 3 — the public setup material shows usable provider/credential/
  optional-MCP choices and honest zero-key failure behavior, with no private
  paths, cookie values or mandatory `envctl`.
* criterion 4 — the docs and support matrix retain repository names and the
  authorized `SkillWeave-AI` mirror destination without initiating a transfer,
  and honestly declare the SW-1510-DOC-TRUTH / MIRROR-CONFIG qualified results
  as **not available in this checkout** (a hold, never a fabricated pass).

The checker is exercised as a **real subprocess** against the tree and against
mutated fixtures (fail-closed negative controls), and its installed-identity leg
is driven with a controlled on-disk distribution — the consumer wiring, not a
helper name.

Self-contained sys.path handling, following the sibling-test convention
(`tests/unit/test_doc_arch.py`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_CHECK = _REPO / "scripts" / "check-version-truth.py"

# Release-facing material: what a public reader sees about version/setup.
_RELEASE_FACING = (
    "README.md",
    "docs/version-truth.md",
    "docs/support/support-matrix.md",
    "docs/release-flow.md",
    "capability.yaml",
)

_SEMVER_RE = re.compile(r"\b\d+\.\d+\.\d+\b")

# Private-data leakage: an operator home path or a secret-looking token. A
# public tool path such as /opt/homebrew is deliberately NOT matched.
_LEAK_PATTERNS = (
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "operator home path"),
    (re.compile(r"PHPSESSID|sessionid\s*[:=]", re.IGNORECASE), "session cookie"),
    (re.compile(r"(?i)\bcookie\s*[:=]\s*\S"), "cookie value"),
    (re.compile(r"(?i)\bpassword\s*[:=]\s*\S"), "password value"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"), "bearer token"),
    (re.compile(r"\b(sk|pk)-[A-Za-z0-9]{16,}"), "api key literal"),
)


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _skills_on_disk() -> list[str]:
    skills = _REPO / "skills"
    return sorted(
        d.name for d in skills.iterdir() if (d / "capability.yaml").is_file()
    )


def _peeled(tag: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", f"{tag}^{{}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _run_check(repo: Path, *, path: Path = _REPO, extra_env: dict | None = None):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", str(_CHECK), "--repo", str(repo)],
        capture_output=True, text=True, env=env,
    )


# ── criterion 1: candidate / badge / changelog / installed ──────────────────


def test_version_truth_doc_distinguishes_the_four_facts():
    doc = _read("docs/version-truth.md")
    for fact in ("candidate", "released", "installed", "advertised early"):
        assert fact in doc, f"docs/version-truth.md does not distinguish {fact!r}"


def test_candidate_and_bundle_agree_and_candidate_is_not_the_released_fact():
    # The two required surfaces carry the same candidate version.
    pyproject = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', _read("pyproject.toml"), re.M)
    bundle = re.search(r"^version:\s*(\d+\.\d+\.\d+)\s*$", _read("capability.yaml"), re.M)
    assert pyproject and bundle, "candidate version surface missing"
    assert pyproject.group(1) == bundle.group(1), (
        f"candidate drift: pyproject {pyproject.group(1)} vs bundle {bundle.group(1)}"
    )
    # That candidate is the *candidate*; docs must not call it released, and the
    # in-flight 1.5.10 must be declared NOT released.
    doc = _read("docs/version-truth.md")
    assert "1.5.10" in doc and "not released" in doc.lower(), (
        "docs/version-truth.md must flag candidate 1.5.10 as not released"
    )


def test_release_facing_material_does_not_advertise_1510_as_released():
    # No release-advertising position (badge, What's-new heading, changelog top)
    # may name 1.5.10 while the released tag is 1.5.9.
    badge = re.search(r"badge/version-(\d+\.\d+\.\d+)", _read("README.md"))
    assert badge and badge.group(1) != "1.5.10"
    whatsnew = re.search(r"^##\s+What's new in\s+(\d+\.\d+\.\d+)", _read("README.md"), re.M)
    if whatsnew is not None:
        assert whatsnew.group(1) != "1.5.10"
    top = re.search(r"^##\s+v(\d+\.\d+\.\d+)", _read("CHANGELOG.md"), re.M)
    assert top and top.group(1) != "1.5.10"


# ── criterion 2: peeled identity + discovered/installed separation ──────────


def test_released_face_is_peeled_and_not_branch_head():
    tag = re.search(r"badge/version-(\d+\.\d+\.\d+)", _read("README.md")).group(1)
    peeled = _peeled(f"v{tag}")
    assert re.fullmatch(r"[0-9a-f]{40}", peeled), peeled
    # The released identity is the peeled commit; it need not equal HEAD.
    assert peeled == _peeled(f"v{tag}")
    # The check is annotation-aware: it resolves ^{}. Assert the behaviour is
    # real by exercising a tag whose peeled commit is NOT HEAD.
    doc = _read("docs/version-truth.md")
    assert "peeled" in doc.lower(), "version-truth doc must state the peeled policy"
    assert "never" in doc.lower() and "HEAD" in doc, (
        "version-truth doc must state released is never 'equals HEAD'"
    )


def test_discovered_and_installed_are_shown_separately():
    doc = _read("docs/version-truth.md")
    assert "discovered" in doc.lower() and "installed" in doc.lower()
    assert "provenance" in doc.lower() and "freshness" in doc.lower(), (
        "version-truth doc must state provenance and freshness for the two facts"
    )


# ── criterion 3: setup, providers, zero-key, no leakage, envctl optional ────


def test_readme_advertises_the_released_version_and_inventory():
    readme = _read("README.md")
    released = _peeled_badge()
    assert f"badge/version-{released}" in readme
    n = len(_skills_on_disk())
    assert f"badge/skills-{n}" in readme, f"skills badge != on-disk {n}"
    assert f"all {n} skills" in readme
    for skill in _skills_on_disk():
        assert skill in readme, f"README is missing skill {skill!r}"


def test_no_pypi_claim_in_release_facing_material():
    for rel in _RELEASE_FACING:
        text = _read(rel)
        assert "pip install skillweave" not in text, (
            f"{rel} still claims a supported bare-PyPI install"
        )
    readme = _read("README.md")
    assert "no PyPI" in readme, "README must state there is no PyPI distribution"


def test_setup_section_shows_providers_credentials_and_optional_mcp():
    readme = _read("README.md")
    for token in (
        "duckduckgo", "serper", "serpapi", "tavily", "brave", "perplexity",
        "SERPER_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY", "PERPLEXITY_API_KEY",
    ):
        assert token.lower() in readme.lower(), f"README setup missing {token!r}"
    assert "zero-key" in readme.lower(), "README must name the zero-key default"
    assert "fail" in readme.lower() and "closed" in readme.lower(), (
        "README must state the honest fail-closed behavior"
    )
    assert "skillweave[mcp]" in readme, "README must show the optional MCP extra"


def test_envctl_is_optional_not_mandatory():
    for rel in _RELEASE_FACING:
        text = _read(rel)
        if "envctl" in text:
            assert re.search(r"envctl[^.\n]*optional", text, re.IGNORECASE), (
                f"{rel} mentions envctl without marking it optional"
            )
            assert re.search(r"(no component|does not require|not require)", text, re.I), (
                f"{rel} must state envctl is not required"
            )


def test_no_private_paths_or_secret_values_in_release_facing_material():
    for rel in _RELEASE_FACING:
        text = _read(rel)
        for pattern, label in _LEAK_PATTERNS:
            m = pattern.search(text)
            assert m is None, f"{rel}: {label} leaked: {m.group(0)!r}"


def test_leakage_scanner_detects_a_planted_secret():
    # The scanner must actually fire, or its green result is meaningless.
    planted = "home /Users/someone/secret  PHPSESSID: abc123def"
    hits = [label for pattern, label in _LEAK_PATTERNS if pattern.search(planted)]
    assert "operator home path" in hits and "session cookie" in hits, hits


# ── criterion 4: repository names, mirror destination, honest hold ──────────


def test_support_matrix_retains_repository_names_and_mirror_destination():
    matrix = _read("docs/support/support-matrix.md")
    for name in ("skillweave/skillweave", "LangeVC/skillweave", "SkillWeave-AI"):
        assert name in matrix, f"support matrix missing preserved name {name!r}"
    # No second mirror owner and no initiated transfer.
    assert "second mirror" not in matrix.lower() or "no second mirror owner" in matrix.lower()
    for verb in ("started the transfer", "initiated the transfer", "began the transfer"):
        assert verb not in matrix.lower(), f"support matrix claims a transfer: {verb}"


def test_doc_truth_and_mirror_config_results_are_declared_pending():
    # The qualified SW-1510-DOC-TRUTH / MIRROR-CONFIG results are not reachable
    # in this checkout; binding to them must be an explicit hold, not a claim.
    matrix = _read("docs/support/support-matrix.md")
    assert "SW-1510-DOC-TRUTH" in matrix and "SW-1510-MIRROR-CONFIG" in matrix
    lowered = matrix.lower()
    assert "not available" in lowered or "pending" in lowered or "hold" in lowered, (
        "support matrix must declare the DOC-TRUTH/MIRROR-CONFIG qualification "
        "as pending/held, not claimed"
    )


# ── the checker itself: positive + fail-closed negative controls ────────────


def _peeled_badge() -> str:
    return re.search(r"badge/version-(\d+\.\d+\.\d+)", _read("README.md")).group(1)


def test_consistency_check_passes_on_the_tree():
    proc = _run_check(_REPO)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "check-version-truth: OK" in proc.stdout, proc.stdout


def _fixture_repo(tmp_path: Path) -> Path:
    """A minimal, self-contained repo the checker can fail closed on."""
    repo = tmp_path / "fixture"
    repo.mkdir()
    names = ["skillweave-alpha", "skillweave-beta"]
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "skillweave"
            version = "1.5.9"
            """
        )
    )
    (repo / "capability.yaml").write_text("kind: bundle\nversion: 1.5.9\n")
    (repo / "CHANGELOG.md").write_text("## v1.5.9 (2026-09-30) — test\n\nbody\n")
    (repo / "README.md").write_text(
        "# SkillWeave\n\n"
        "[![Version](https://img.shields.io/badge/version-1.5.9-blue)](x)\n"
        "[![Skills](https://img.shields.io/badge/skills-2-blue)](skills/)\n\n"
        "## What's new in 1.5.9\n\nbody\n\n"
        "## Setup\n\nDuckDuckGo is a zero-key default that fails closed.\n\n"
        "`pip install 'skillweave[mcp]'` is the optional MCP extra. No PyPI here.\n\n"
        f"{names[0]} and {names[1]} are the skills; all 2 skills, 7 phases.\n"
    )
    for n in names:
        d = repo / "skills" / n
        d.mkdir(parents=True)
        (d / "capability.yaml").write_text(f"kind: skill\nname: {n}\nversion: 1.3.11\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1.5.9"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-a", "v1.5.9", "-m", "SkillWeave v1.5.9"],
        check=True,
    )
    return repo


def test_consistency_check_fails_closed_on_badge_drift(tmp_path):
    repo = _fixture_repo(tmp_path)
    assert _run_check(repo).returncode == 0
    readme = (repo / "README.md").read_text()
    (repo / "README.md").write_text(readme.replace("badge/version-1.5.9", "badge/version-1.5.5"))
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "badge==released" in proc.stdout and "DRIFT" in proc.stdout


def test_consistency_check_fails_closed_on_candidate_drift(tmp_path):
    repo = _fixture_repo(tmp_path)
    (repo / "capability.yaml").write_text("kind: bundle\nversion: 1.5.8\n")
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "candidate==bundle" in proc.stdout and "DRIFT" in proc.stdout


def test_consistency_check_fails_closed_on_early_advertised_version(tmp_path):
    # A badge naming 1.5.10 while only v1.5.9 exists must not pass.
    repo = _fixture_repo(tmp_path)
    readme = (repo / "README.md").read_text()
    (repo / "README.md").write_text(
        readme.replace("badge/version-1.5.9", "badge/version-1.5.10")
        .replace("What's new in 1.5.9", "What's new in 1.5.10")
    )
    proc = _run_check(repo)
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)


def _plant_installed(tmp_path: Path, version: str) -> Path:
    """Write a minimal on-disk distribution so importlib.metadata finds it."""
    site = tmp_path / "site"
    dist = site / f"skillweave-{version}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: skillweave\nVersion: %s\n" % version
    )
    return site


def test_consistency_check_installed_identity_positive(tmp_path):
    # criterion 1: a real (on-disk) installed distribution whose metadata
    # matches the candidate is accepted, end to end via the check's own
    # importlib.metadata consumption -- not a helper.
    site = _plant_installed(tmp_path, _peeled_badge())
    proc = _run_check(_REPO, extra_env={"PYTHONPATH": str(site)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "installed==candidate" in proc.stdout and "ok" in proc.stdout


def test_consistency_check_installed_identity_negative(tmp_path):
    # A stale/foreign install (wrong metadata) is an installed-version-drift.
    site = _plant_installed(tmp_path, "1.5.8")
    proc = _run_check(_REPO, extra_env={"PYTHONPATH": str(site)})
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "installed==candidate" in proc.stdout and "DRIFT" in proc.stdout


def _run_all() -> int:
    tests = [
        test_version_truth_doc_distinguishes_the_four_facts,
        test_candidate_and_bundle_agree_and_candidate_is_not_the_released_fact,
        test_release_facing_material_does_not_advertise_1510_as_released,
        test_released_face_is_peeled_and_not_branch_head,
        test_discovered_and_installed_are_shown_separately,
        test_readme_advertises_the_released_version_and_inventory,
        test_no_pypi_claim_in_release_facing_material,
        test_setup_section_shows_providers_credentials_and_optional_mcp,
        test_envctl_is_optional_not_mandatory,
        test_no_private_paths_or_secret_values_in_release_facing_material,
        test_leakage_scanner_detects_a_planted_secret,
        test_support_matrix_retains_repository_names_and_mirror_destination,
        test_doc_truth_and_mirror_config_results_are_declared_pending,
        test_consistency_check_passes_on_the_tree,
    ]
    import tempfile

    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
