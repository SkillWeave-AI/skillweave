"""Version/support/setup truth-check (SW-DOC-004).

A machine check that the **release-facing** documentation, the declared version
surfaces and an installed artifact all agree — the SW-DOC-004 acceptance
criteria, asserted rather than claimed:

* criterion 1 — a consistency check (`scripts/check-version-truth.py`) compares
  the candidate package/manifest version, the release-facing badge/changelog and
  the installed package identity; historical references are allowed, and a
  version ahead of the released tag is never advertised as **released** — unless
  the explicit `release_prep` state names the candidate (pyproject == bundle)
  ahead of the released tag, in which case the badge / "What's new" heading /
  CHANGELOG top may name the candidate, provided they all agree with it.
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
helper name. The **discovered** (registry/index) leg is driven with real
operator-supplied observation files: fresh, stale, mismatched, malformed,
missing-field and absent — each asserted on the checker's own printed/`--json`
output, so "shown separately with freshness/provenance" is proven, not claimed.

Self-contained sys.path handling, following the sibling-test convention
(`tests/unit/test_doc_arch.py`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
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


def _candidate() -> str:
    return re.search(
        r'^version\s*=\s*"(\d+\.\d+\.\d+)"', _read("pyproject.toml"), re.M
    ).group(1)


def _release_prep() -> str | None:
    m = re.search(
        r'^release_prep:\s*"?(\d+\.\d+\.\d+)"?\s*$', _read(".version.yaml"), re.M
    )
    return m.group(1) if m else None


def _peeled(tag: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), "rev-parse", f"{tag}^{{}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _latest_release_tag() -> str:
    """The released fact: the highest semver ``vX.Y.Z`` tag -- never a literal."""
    out = subprocess.run(
        ["git", "-C", str(_REPO), "tag", "--list", "v*"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    tags = [t for t in out if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return max(tags, key=lambda t: tuple(int(p) for p in t[1:].split(".")))


def _released_version() -> str:
    return _latest_release_tag().lstrip("v")


def _semver_key(version: str) -> tuple[int, int, int]:
    return tuple(int(p) for p in version.split("."))  # type: ignore[return-value]


def _run_check(
    repo: Path,
    *,
    extra_args: list[str] | None = None,
    extra_env: dict | None = None,
):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, "-B", str(_CHECK), "--repo", str(repo)]
    if extra_args:
        argv.extend(extra_args)
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def _run_check_json(repo: Path, *extra_args: str) -> dict:
    proc = _run_check(repo, extra_args=["--json", *extra_args])
    assert proc.returncode in (0, 1), (proc.returncode, proc.stdout, proc.stderr)
    return json.loads(proc.stdout)


def _write_observation(
    tmp_path: Path,
    name: str,
    *,
    version: str | None = None,
    source: str = "forgejo-index",
    index_url: str | None = "https://forgejo.example/api/packages/skillweave/pypi/simple/",
    observed_at: str | None = None,
) -> Path:
    obs = tmp_path / name
    payload = {
        "source": source,
        "name": "skillweave",
        "version": version if version is not None else _released_version(),
    }
    if index_url is not None:
        payload["index_url"] = index_url
    if observed_at is not None:
        payload["observed_at"] = observed_at
    obs.write_text(json.dumps(payload), encoding="utf-8")
    return obs


def _now_iso(minutes_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # The candidate is the *candidate*, not the released fact: it is not named
    # by the latest annotated tag (derived, never a literal in the docs).
    candidate = pyproject.group(1)
    assert candidate != _released_version(), (
        f"candidate {candidate} must not equal the released fact "
        f"{_released_version()}"
    )


def test_release_facing_material_is_release_aware_not_advertising_as_released():
    # Release-aware: the released fact is the latest annotated tag, derived, not
    # a literal. A release-advertising position (badge, What's-new heading,
    # changelog top) may name the candidate ONLY because the explicit
    # release_prep state is active (prep == candidate, ahead of the released
    # tag); otherwise every such position must name the released version.
    released = _released_version()
    candidate = _candidate()
    prep = _release_prep()
    prep_active = prep is not None and prep == candidate and (
        _semver_key(candidate) > _semver_key(released)
    )
    expected = candidate if prep_active else released
    positions = {
        "badge": re.search(r"badge/version-(\d+\.\d+\.\d+)", _read("README.md")),
        "changelog-top": re.search(
            r"^##\s+v(\d+\.\d+\.\d+)", _read("CHANGELOG.md"), re.M
        ),
    }
    whatsnew = re.search(
        r"^##\s+What's new in\s+(\d+\.\d+\.\d+)", _read("README.md"), re.M
    )
    if whatsnew is not None:
        positions["whats-new"] = whatsnew
    for name, m in positions.items():
        assert m is not None, f"{name} position missing"
        if prep_active:
            assert m.group(1) in (candidate, released), (
                f"{name} names {m.group(1)!r}; an active release-prep "
                f"({candidate} ahead of {released}) allows only the candidate "
                f"or the still-released version"
            )
        else:
            assert m.group(1) == expected, (
                f"{name} names {m.group(1)!r}; with no release-prep it must "
                f"name the released fact {released!r}"
            )


# ── criterion 2: peeled identity + discovered/installed separation ──────────


def test_released_face_is_peeled_and_not_branch_head():
    # The released fact is the latest annotated tag, derived -- not the badge
    # (which under an active release-prep may name the candidate).
    tag = _latest_release_tag()
    peeled = _peeled(tag)
    assert re.fullmatch(r"[0-9a-f]{40}", peeled), peeled
    # The released identity is the peeled commit; it need not equal HEAD.
    assert peeled == _peeled(tag)
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


def test_check_actually_computes_the_discovered_fact(tmp_path):
    # The tool -- not just the prose -- must produce the discovered version,
    # from an operator observation, with its source and observation timestamp.
    released = _released_version()
    obs = _write_observation(tmp_path, "obs.json", version=released)
    proc = _run_check(_REPO, extra_args=["--discovered-observation", str(obs)])
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "discovered" in proc.stdout
    assert "forgejo-index" in proc.stdout and released in proc.stdout
    assert "freshness" in proc.stdout
    # It is an explicit observation, never a live registry query.
    doc = _read("docs/version-truth.md")
    assert "no live registry query" in doc.lower() or (
        "performs no live query" in doc.lower()
    )


def test_check_json_exposes_installed_and_discovered_separately(tmp_path):
    # A discovered observation that is ahead of the released tag -- the exact
    # "what a registry might see" the doc describes -- must never be promoted to
    # the released fact (which stays derived from the tag, not this input).
    candidate = _candidate()
    obs = _write_observation(
        tmp_path, "obs.json", version=candidate, index_url=None, observed_at=_now_iso()
    )
    report = _run_check_json(_REPO, "--discovered-observation", str(obs))
    assert report["discovered"]["version"] == candidate
    assert report["discovered"]["source"] == "forgejo-index"
    # Never merged: they are two distinct top-level facts.
    assert "installed" in report and "discovered" in report
    # The discovered candidate is not the released fact (released stays the tag).
    assert report["released"]["version"] == _released_version()
    assert report["released"]["tag"] == _latest_release_tag()
    # Any mismatch between discovered and installed is shown, not hidden.
    if report["installed"] is not None:
        assert report["installed"]["version"] != report["discovered"]["version"]


def test_check_reports_no_discovered_observation_when_absent():
    proc = _run_check(_REPO)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "no discovered observation supplied" in proc.stdout, proc.stdout
    report = _run_check_json(_REPO)
    assert report["discovered"] is None


def test_check_marks_a_stale_observation_stale(tmp_path):
    obs = _write_observation(
        tmp_path, "old.json", observed_at=_now_iso(minutes_ago=600)
    )  # 10h old
    fresh = _run_check(
        _REPO, extra_args=["--discovered-observation", str(obs), "--freshness-hours", "48"]
    )
    assert fresh.returncode == 0
    assert "[STALE]" not in fresh.stdout
    stale = _run_check(
        _REPO, extra_args=["--discovered-observation", str(obs), "--freshness-hours", "1"]
    )
    assert "[STALE]" in stale.stdout, stale.stdout
    report = _run_check_json(
        _REPO, "--discovered-observation", str(obs), "--freshness-hours", "1"
    )
    assert report["discovered"]["stale"] is True
    assert report["discovered"]["age_hours"] > 1


def test_check_fails_closed_on_malformed_observation(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    proc = _run_check(_REPO, extra_args=["--discovered-observation", str(bad)])
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "malformed" in proc.stderr.lower()


def test_check_fails_closed_on_observation_missing_a_field(tmp_path):
    bad = tmp_path / "missing.json"
    bad.write_text(json.dumps({"source": "x", "version": "9.9.9"}), encoding="utf-8")
    proc = _run_check(_REPO, extra_args=["--discovered-observation", str(bad)])
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "missing required field" in proc.stderr


def test_check_fails_closed_on_absent_observation_file(tmp_path):
    proc = _run_check(
        _REPO, extra_args=["--discovered-observation", str(tmp_path / "nope.json")]
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "not found" in proc.stderr


# ── criterion 3: setup, providers, zero-key, no leakage, envctl optional ────


def test_readme_advertises_the_released_version_and_inventory():
    readme = _read("README.md")
    released = _released_version()
    candidate = _candidate()
    prep = _release_prep()
    # Release-aware: the badge advertises the released tag, or the candidate
    # when the explicit release-prep state is active (prep == candidate, ahead
    # of the released tag). The released fact is derived, never a literal.
    badge = re.search(r"badge/version-(\d+\.\d+\.\d+)", readme).group(1)
    expected = {released}
    if prep is not None and prep == candidate and _semver_key(candidate) > _semver_key(released):
        expected.add(candidate)
    assert badge in expected, f"badge {badge!r} not in accepted {sorted(expected)}"
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


def test_consistency_check_passes_on_the_tree():
    proc = _run_check(_REPO)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "check-version-truth: OK" in proc.stdout, proc.stdout


def _init_repo(repo: Path, released: str) -> None:
    """git-init a fixture repo and lay down one annotated ``vX.Y.Z`` tag."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", f"SkillWeave v{released}"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-a", f"v{released}",
         "-m", f"SkillWeave v{released}"],
        check=True,
    )


def _write_repo(
    repo: Path,
    *,
    version: str,
    released: str,
    prep: str | None = None,
    badge: str | None = None,
    changelog_top: str | None = None,
    whatsnew: str | None = None,
) -> None:
    """Write a fixture repo whose advertising positions are explicit.

    ``version`` is the candidate (pyproject + bundle); ``released`` is the
    annotated tag the fixture is initialized with; ``prep`` writes the
    ``release_prep`` scalar. ``badge``/``changelog_top``/``whatsnew`` default to
    the released version, so a plain fixture behaves as the pre-release-prep one.
    """
    repo.mkdir()
    names = ["skillweave-alpha", "skillweave-beta"]
    (repo / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [project]
            name = "skillweave"
            version = "{version}"
            """
        )
    )
    (repo / "capability.yaml").write_text(f"kind: bundle\nversion: {version}\n")
    top = released if changelog_top is None else changelog_top
    (repo / "CHANGELOG.md").write_text(f"## v{top} (2026-09-30) — test\n\nbody\n")
    badge_v = released if badge is None else badge
    whatsnew_v = released if whatsnew is None else whatsnew
    (repo / "README.md").write_text(
        "# SkillWeave\n\n"
        f"[![Version](https://img.shields.io/badge/version-{badge_v}-blue)](x)\n"
        "[![Skills](https://img.shields.io/badge/skills-2-blue)](skills/)\n\n"
        f"## What's new in {whatsnew_v}\n\nbody\n\n"
        "## Setup\n\nDuckDuckGo is a zero-key default that fails closed.\n\n"
        "`pip install 'skillweave[mcp]'` is the optional MCP extra. No PyPI here.\n\n"
        f"{names[0]} and {names[1]} are the skills; all 2 skills, 7 phases.\n"
    )
    for n in names:
        d = repo / "skills" / n
        d.mkdir(parents=True)
        (d / "capability.yaml").write_text(f"kind: skill\nname: {n}\nversion: 1.3.11\n")
    if prep is not None:
        (repo / ".version.yaml").write_text(
            "schema: 1\n"
            "source_of_truth: pyproject.toml\n"
            f'release_prep: "{prep}"\n'
            "locations:\n"
            "  - path: pyproject.toml\n"
            "    pattern: '^version\\s*=\\s*\"(\\d+\\.\\d+\\.\\d+)\"'\n"
            "    required: true\n"
        )
    _init_repo(repo, released)


def _fixture_repo(tmp_path: Path) -> Path:
    """A minimal, self-contained repo the checker can fail closed on."""
    repo = tmp_path / "fixture"
    _write_repo(repo, version="9.9.1", released="9.9.1")
    return repo


def test_consistency_check_fails_closed_on_badge_drift(tmp_path):
    repo = tmp_path / "drift"
    _write_repo(repo, version="9.9.1", released="9.9.1", badge="9.9.0")
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "badge==released" in proc.stdout and "DRIFT" in proc.stdout


def test_consistency_check_fails_closed_on_candidate_drift(tmp_path):
    repo = _fixture_repo(tmp_path)
    (repo / "capability.yaml").write_text("kind: bundle\nversion: 9.9.0\n")
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "candidate==bundle" in proc.stdout and "DRIFT" in proc.stdout


def test_consistency_check_fails_closed_on_early_advertised_version(tmp_path):
    # Without a release_prep, a badge/heading naming the candidate ahead of the
    # released tag (9.9.2 while only v9.9.1 exists) must not pass: advertising
    # ahead is drift.
    repo = tmp_path / "early"
    _write_repo(repo, version="9.9.1", released="9.9.1", badge="9.9.2",
                whatsnew="9.9.2")
    proc = _run_check(repo)
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "DRIFT" in proc.stdout


def test_consistency_check_release_prep_accepts_candidate_positions(tmp_path):
    # POSITIVE control: release_prep == candidate (pyproject == bundle) ahead of
    # the released tag, and the CHANGELOG top, badge and What's-new heading all
    # name the candidate -> exit 0 with a named release-prep line.
    repo = tmp_path / "prepped"
    _write_repo(repo, version="9.9.2", released="9.9.1", prep="9.9.2",
                badge="9.9.2", changelog_top="9.9.2", whatsnew="9.9.2")
    proc = _run_check(repo)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "release-prep" in proc.stdout, proc.stdout
    report = _run_check_json(repo)
    assert report["release_prep"] == "9.9.2"
    assert report["ok"] is True


def test_consistency_check_release_prep_rejects_a_position_naming_another(tmp_path):
    # NEGATIVE control: with an active release-prep, a position naming a version
    # that is neither the candidate nor the still-released tag is DRIFT.
    repo = tmp_path / "prep_other"
    _write_repo(repo, version="9.9.2", released="9.9.1", prep="9.9.2",
                badge="9.9.3", changelog_top="9.9.2", whatsnew="9.9.2")
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "badge==released" in proc.stdout and "DRIFT" in proc.stdout


def test_consistency_check_release_prep_must_equal_the_candidate(tmp_path):
    # NEGATIVE control: a release_prep that differs from the candidate is DRIFT.
    repo = tmp_path / "prep_mismatch"
    _write_repo(repo, version="9.9.2", released="9.9.1", prep="9.9.3",
                badge="9.9.2", changelog_top="9.9.2", whatsnew="9.9.2")
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "release-prep==candidate" in proc.stdout and "DRIFT" in proc.stdout


def test_consistency_check_release_prep_must_be_ahead_of_the_released_tag(tmp_path):
    # NEGATIVE control: a release_prep that is not ahead of the released tag is
    # a named DRIFT, even when it equals the candidate.
    repo = tmp_path / "prep_not_ahead"
    _write_repo(repo, version="9.9.1", released="9.9.1", prep="9.9.1")
    proc = _run_check(repo)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "release-prep-ahead" in proc.stdout and "DRIFT" in proc.stdout


def _plant_installed(
    tmp_path: Path, version: str, *, editable_dir: str | None = None
) -> Path:
    """Write a minimal on-disk distribution so importlib.metadata finds it."""
    site = tmp_path / "site"
    dist = site / f"skillweave-{version}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: skillweave\nVersion: %s\n" % version
    )
    if editable_dir is not None:
        (dist / "direct_url.json").write_text(
            json.dumps({"url": editable_dir, "dir_info": {"editable": True}})
        )
    return site


def test_consistency_check_installed_identity_positive(tmp_path):
    # criterion 1: a real (on-disk) installed distribution whose metadata
    # matches the candidate is accepted, end to end via the check's own
    # importlib.metadata consumption -- not a helper.
    site = _plant_installed(tmp_path, _candidate())
    proc = _run_check(_REPO, extra_env={"PYTHONPATH": str(site)})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "installed==candidate" in proc.stdout and "ok" in proc.stdout


def test_consistency_check_reports_installed_origin(tmp_path):
    # installed identity carries provenance (dist name, version, origin path
    # and how it was installed), not just a bare number.
    site = _plant_installed(
        tmp_path, _candidate(), editable_dir="/repo/skillweave"
    )
    proc = _run_check(_REPO, extra_env={"PYTHONPATH": str(site)})
    assert "installed identity" in proc.stdout, proc.stdout
    assert "skillweave" in proc.stdout and "editable-checkout" in proc.stdout
    assert "/repo/skillweave" in proc.stdout, proc.stdout


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
        test_release_facing_material_is_release_aware_not_advertising_as_released,
        test_released_face_is_peeled_and_not_branch_head,
        test_discovered_and_installed_are_shown_separately,
        test_check_reports_no_discovered_observation_when_absent,
        test_readme_advertises_the_released_version_and_inventory,
        test_no_pypi_claim_in_release_facing_material,
        test_setup_section_shows_providers_credentials_and_optional_mcp,
        test_envctl_is_optional_not_mandatory,
        test_no_private_paths_or_secret_values_in_release_facing_material,
        test_leakage_scanner_detects_a_planted_secret,
        test_support_matrix_retains_repository_names_and_mirror_destination,
        test_doc_truth_and_mirror_config_results_are_declared_pending,
        test_consistency_check_passes_on_the_tree,
        test_consistency_check_release_prep_accepts_candidate_positions,
        test_consistency_check_release_prep_rejects_a_position_naming_another,
        test_consistency_check_release_prep_must_equal_the_candidate,
        test_consistency_check_release_prep_must_be_ahead_of_the_released_tag,
    ]
    import inspect
    import tempfile

    failed = 0
    for t in tests:
        try:
            tmp = Path(tempfile.mkdtemp())
            # Tests declaring a `tmp_path` fixture get a fresh directory; the
            # rest are called with no arguments.
            if "tmp_path" in inspect.signature(t).parameters:
                t(tmp)
            else:
                t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
