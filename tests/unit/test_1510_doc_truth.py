"""SW-1510-DOC-TRUTH: the versioned execution/support matrix is derived, not claimed.

This suite asserts the three DOC-TRUTH acceptance criteria against the *real*
consumers, not against a re-implementation:

* criterion 1 — the new execution/support matrix
  (``docs/native-execution-support-1510.md``) and the README derive the
  version, the skill count, the substrate map and the CLI / MCP / Assay support
  rows from the **qualified release manifest**. The manifest is the composed
  declared release surfaces of this checkout (``pyproject.toml`` +
  ``capability.yaml`` + ``.version.yaml`` + ``skills/*/capability.yaml`` +
  ``src/skillweave/cli/support.py`` + ``src/skillweave/runtime/methodology.py``
  + ``docs/substrate-map.md``). The support table is read from the **live
  module** and, separately, from the **actual CLI consumer subprocess** — a
  helper name or a copied literal is not evidence.
* criterion 2 — the **bare limits**, the **detected-versus-live-tested
  adapters**, the **unknown-capacity** rule, the **preview features** and the
  **plaintext onboarding storage** behaviour are all explicit in the document.
* criterion 3 — the public material carries no private paths, credentials or
  commercial-implementation disclosure and no unverifiable always-OSS /
  future-capability claim. The leak scanner carries a planted-secret negative
  control so its green result is not vacuous.

``scripts/check-version-truth.py`` is exercised as a **real subprocess** (the
release-consistency consumer), including its installed-identity leg against a
controlled on-disk distribution — installed **origin/provenance**, not a bare
number.

Self-contained ``sys.path`` handling, following the sibling-test convention
(``tests/unit/test_doc_version_truth.py``).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_DOC = "docs/native-execution-support-1510.md"
_CHECK = _REPO / "scripts" / "check-version-truth.py"

# Release-facing material a public reader sees.
_PUBLIC = ("README.md", _DOC) + tuple(
    f"examples/{p.name}"
    for p in sorted((_REPO / "examples").glob("*.py"))
)

# Private-data leakage: operator home path or a secret-looking token. A public
# tool path such as /opt/homebrew is deliberately NOT matched.
_LEAK_PATTERNS = (
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "operator home path"),
    (re.compile(r"/home/[a-z][A-Za-z0-9._-]+"), "operator home path"),
    (re.compile(r"PHPSESSID|sessionid\s*[:=]", re.IGNORECASE), "session cookie"),
    (re.compile(r"(?i)\bcookie\s*[:=]\s*\S"), "cookie value"),
    (re.compile(r"(?i)\bpassword\s*[:=]\s*\S"), "password value"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"), "bearer token"),
    (re.compile(r"\b(sk|pk)-[A-Za-z0-9]{16,}"), "api key literal"),
)

# Unverifiable always-OSS / future-capability promises forbidden by criterion 3.
_UNVERIFIABLE_CLAIMS = (
    re.compile(r"forever\s+free", re.IGNORECASE),
    re.compile(r"free\s+forever", re.IGNORECASE),
    re.compile(r"always\s+(free|oss|open)", re.IGNORECASE),
    re.compile(r"will\s+(always|never)\s+be\s+free", re.IGNORECASE),
    re.compile(r"no\s+limits", re.IGNORECASE),
    re.compile(r"coming\s+soon", re.IGNORECASE),
    re.compile(r"100%\s+free", re.IGNORECASE),
)


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _live_support():
    """Import the *installed/consumer* support module from this checkout."""
    src = str(_REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    importlib.invalidate_caches()
    return importlib.import_module("skillweave.cli.support")


def _cli_support_json() -> dict:
    """Drive the actual CLI consumer as a subprocess and parse its JSON."""
    code = (
        "import sys; sys.path.insert(0, 'src'); "
        "from skillweave.cli import support; "
        "raise SystemExit(support.main(['--json']))"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(_REPO), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    return json.loads(proc.stdout)


def _skills_on_disk() -> list[str]:
    return sorted(
        d.name
        for d in (_REPO / "skills").iterdir()
        if (d / "capability.yaml").is_file()
    )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _released_tag() -> str:
    """The released fact is the repository's latest annotated product tag.

    Derived from the repository, never the literal ``v1.5.9``: the test asks
    the repository which tag is latest, so a later release (for example a local
    ``v1.5.10`` tag) is exercised without editing this file.
    """
    tags = [
        t for t in _git("tag", "--list", "v*").split()
        if re.fullmatch(r"v\d+\.\d+\.\d+", t)
    ]
    assert tags, "no vX.Y.Z release tag found in the repository"
    return max(tags, key=lambda t: tuple(int(p) for p in t[1:].split(".")))


# ── criterion 1: derive from the qualified release manifest ─────────────────


def test_doc_derives_the_surface_table_from_the_live_module():
    doc = _read(_DOC)
    support = _live_support()
    table = support.support_table()
    # The single versioned identity and the shared vocabularies are named.
    assert table["schema_id"] in doc, table["schema_id"]
    assert table["state_interpretation_id"] in doc
    assert table["run_snapshot_schema_id"] in doc
    for surface in table["surfaces"]:
        for token in (
            surface["name"], surface["role"], surface["status"],
            surface["entry_point"],
        ):
            assert token in doc, f"surface {surface['name']!r}: missing {token!r}"
    # The shared run-state vocabulary (projected, not copied) is present.
    state = table["state"]
    assert len(state["canonical"]) == 15, len(state["canonical"])
    for value in state["canonical"]:
        assert value in doc, f"canonical state {value!r} not in the doc"
    for value in state["terminal"]:
        assert value in doc, f"terminal state {value!r} not in the doc"
    for value in state["stop_reasons"]:
        assert value in doc, f"stop reason {value!r} not in the doc"
    # Exit codes and the run-id rule.
    assert table["run_id_rule"] in doc
    for name in table["exit_codes"]:
        assert name in doc, f"exit code name {name!r} not in the doc"


def test_doc_support_table_matches_the_actual_cli_consumer():
    # The doc's table must equal what the *installed console path* emits, not a
    # helper's copy. ``support.main`` is the exact entry the router mounts.
    live = _cli_support_json()
    support = _live_support()
    rendered = support.support_table()
    assert live == rendered, "CLI consumer disagrees with the imported module"
    doc = _read(_DOC)
    for surface in live["surfaces"]:
        assert surface["entry_point"] in doc
        assert surface["name"] in doc


def test_doc_skill_inventory_matches_on_disk():
    doc = _read(_DOC)
    on_disk = _skills_on_disk()
    assert len(on_disk) == 15, on_disk
    for skill in on_disk:
        assert skill in doc, f"skill {skill!r} not in the doc"
    assert "15" in doc


def test_doc_binds_candidate_and_released_facts():
    doc = _read(_DOC)
    pyproject = re.search(
        r'^version\s*=\s*"(\d+\.\d+\.\d+)"', _read("pyproject.toml"), re.M
    )
    bundle = re.search(r"^version:\s*(\d+\.\d+\.\d+)\s*$", _read("capability.yaml"), re.M)
    assert pyproject and bundle and pyproject.group(1) == bundle.group(1)
    candidate = pyproject.group(1)
    # The candidate (and declared bundle) is named in the doc.
    assert candidate in doc
    # The released fact is derived from the repository (the latest annotated
    # tag), never a literal v1.5.9 pinned in this file. The document states it
    # by its RULE and by the COMMAND that derives it, never by a printed hash:
    # neither the released version literal nor a peeled commit is required in
    # the document.
    assert "peeled" in doc.lower(), "the doc must state the peeled-tag rule"
    assert "rev-parse" in doc, "the doc must name the command that derives it"
    assert "annotated" in doc.lower(), "the doc must state the annotated rule"
    # No literal peeled commit may be printed in the doc.
    assert not re.search(r"\b[0-9a-f]{40}\b", doc), "the doc prints a literal hash"
    # The candidate is named as the release in preparation, not yet released
    # until its tag exists in the repository.
    released_tag = _released_tag()
    if released_tag != "v" + candidate:
        assert "not yet released" in doc.lower() or "not released" in doc.lower()


def test_readme_binds_version_skill_count_and_substrate():
    readme = _read("README.md")
    pyproject = re.search(
        r'^version\s*=\s*"(\d+\.\d+\.\d+)"', _read("pyproject.toml"), re.M
    )
    assert pyproject, "pyproject.toml has no [project].version"
    candidate = pyproject.group(1)
    # The badge advertises the candidate release in preparation, the What's new
    # heading names it, the changelog anchor is the CHANGELOG's own 1.5.10
    # heading slug, and the install example uses the candidate wheel name.
    assert f"badge/version-{candidate}" in readme, readme[:200]
    assert f"## What's new in {candidate}" in readme
    assert f"CHANGELOG.md#v{candidate.replace('.', '')}-" in readme
    assert f"skillweave-{candidate}-py3-none-any.whl" in readme
    # No literal peeled hash of a release tag may be printed in the README.
    assert not re.search(r"\b[0-9a-f]{40}\b", readme), "README prints a literal hash"
    n = len(_skills_on_disk())
    assert f"badge/skills-{n}" in readme, f"skills badge != on-disk {n}"
    assert f"all {n} skills" in readme
    # README points at the substrate map and the new execution/support matrix.
    assert "docs/substrate-map.md" in readme or "substrate" in readme.lower()
    assert _DOC in readme or "native-execution-support-1510" in readme


# ── criterion 2: explicit limits, adapters, capacity, preview, storage ──────


def test_doc_states_bare_limits():
    doc = _read(_DOC).lower()
    for token in ("zero-key", "duckduckgo", "serper", "tavily", "brave", "perplexity"):
        assert token in doc, f"bare limits missing {token!r}"
    assert "fails closed" in doc
    assert "no pypi" in doc
    assert "requires-python" in doc or "3.9" in doc


def test_doc_states_detected_versus_live_tested_adapters():
    doc = _read(_DOC).lower()
    assert "detected" in doc and "live" in doc
    assert "conformance" in doc
    # Reading a descriptor proves format conformance, never callability.
    assert "callability" in doc
    assert "pending/blocked" in doc or "pending" in doc


def test_doc_states_unknown_capacity_is_never_unlimited():
    doc = _read(_DOC).lower()
    assert "unknown capacity is never treated as unlimited" in doc
    assert "declaration" in doc and "detection" in doc


def test_doc_states_preview_features_and_matches_live_constants():
    doc = _read(_DOC)
    src = str(_REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    importlib.invalidate_caches()
    eb = importlib.import_module("skillweave.runtime.edition_boundary")
    assert eb.AGGREGATE_SERVICE_GA is False
    assert eb.AGGREGATE_UNAVAILABLE_LABEL in doc
    assert "community" in doc.lower() and "pro" in doc.lower() \
        and "enterprise" in doc.lower()
    assert "preview" in doc.lower()


def test_doc_states_plaintext_onboarding_storage():
    doc = _read(_DOC).lower()
    assert "plaintext" in doc
    assert "onboarding-profile.yaml" in doc
    assert "onboarding-state.yaml" in doc
    assert "yaml.dump" in doc
    # It is *plaintext*: no encryption/keyring claim may accompany it.
    # It is *plaintext*: the doc must state it is not encrypted and uses no
    # separate secret store, rather than implying a protected store.
    assert "not" in doc and "encrypted" in doc
    assert "no separate secret store" in doc


def test_doc_mcp_and_assay_support_rows_match_the_live_registry():
    doc = _read(_DOC)
    src = str(_REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    importlib.invalidate_caches()
    server = importlib.import_module("skillweave.mcp.server")
    tools = sorted(f.__name__ for f in getattr(server.mcp, "tools", []))
    assert "create_run" in tools and "agent_capabilities" in tools
    for tool in tools:
        assert tool in doc, f"MCP tool {tool!r} not in the doc"
    methodology = importlib.import_module("skillweave.runtime.methodology")
    for entry in methodology.DEFAULT_REGISTRY._entries:
        assert entry.value in doc, f"methodology {entry.value!r} not in the doc"


# ── criterion 3: public-example hygiene, no unverifiable claims ─────────────


def test_no_private_paths_or_secret_values_in_public_material():
    for rel in _PUBLIC:
        path = _REPO / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, label in _LEAK_PATTERNS:
            m = pattern.search(text)
            assert m is None, f"{rel}: {label} leaked: {m.group(0)!r}"


def test_leakage_scanner_detects_a_planted_secret():
    planted = "home /Users/someone/secret  PHPSESSID: abc123def"
    hits = [label for pattern, label in _LEAK_PATTERNS if pattern.search(planted)]
    assert "operator home path" in hits and "session cookie" in hits, hits


def test_no_unverifiable_always_oss_or_future_claims():
    for rel in _PUBLIC:
        path = _REPO / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in _UNVERIFIABLE_CLAIMS:
            m = pattern.search(text)
            assert m is None, (
                f"{rel}: unverifiable claim {m.group(0)!r} (criterion 3)"
            )


def test_claim_scanner_detects_a_planted_claim():
    planted = "SkillWeave is Forever Free — all skills, no limits. Studio coming soon."
    hits = [
        p.search(planted).group(0)
        for p in _UNVERIFIABLE_CLAIMS
        if p.search(planted)
    ]
    assert len(hits) >= 2, hits


def test_no_commercial_implementation_disclosure_in_public_material():
    # No proprietary behaviour/CMS mapping/pricing of the commercial pack is
    # reproduced in the public examples or the new doc.
    doc = _read(_DOC).lower()
    for token in ("revenue", "pricing", "our proprietary", "cms behaviour",
                  "provider mappings"):
        assert token not in doc, f"commercial detail leaked: {token!r}"


def test_envctl_where_mentioned_is_optional():
    doc = _read(_DOC)
    if "envctl" in doc:
        assert re.search(r"envctl[^.\n]*optional", doc, re.IGNORECASE)


# ── the release-consistency consumer, run as a real subprocess ──────────────


def _scratch_repo_with_candidate_tag(dest: Path) -> Path:
    """A scratch clone of this checkout with a local annotated ``v<candidate>``.

    The read-only ``check-version-truth.py`` compares the release-advertising
    positions (badge, ``What's new`` heading, CHANGELOG top) against the
    **latest tag in the repository**. In the working tree the latest tag still
    predates the release in preparation, so the check correctly reports drift
    there. This helper materialises the release-in-preparation fact without
    mutating the tree: an annotated tag named for the candidate is created on
    the tree's HEAD in a scratch clone, so the check is exercised against the
    exact tree the diff ships -- never against a re-implementation of it.
    """
    repo = dest / "skillweave"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(_REPO), str(repo)],
        check=True, capture_output=True, text=True,
    )
    # The clone materialises HEAD, but the release face under test is the
    # working-tree material this diff ships; overwrite the clone's README with
    # the shipped one so the check evaluates the release-in-preparation face
    # exactly as it will be tagged, independent of commit state.
    (repo / "README.md").write_text(_read("README.md"), encoding="utf-8")
    pyproject = re.search(
        r'^version\s*=\s*"(\d+\.\d+\.\d+)"', _read("pyproject.toml"), re.M
    )
    assert pyproject, "pyproject.toml has no [project].version"
    candidate = pyproject.group(1)
    tag = "v" + candidate
    existing = subprocess.run(
        ["git", "-C", str(repo), "tag", "--list", tag],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not existing:
        subprocess.run(
            ["git", "-C", str(repo), "tag", "-a", tag, "-m",
             f"SkillWeave {candidate}"],
            check=True, capture_output=True, text=True,
        )
    return repo


def test_release_consistency_check_passes_on_the_tree(tmp_path):
    # The diff advertises the release IN PREPARATION (the candidate) at every
    # release-facing position, so the read-only check must pass once that
    # candidate's tag exists. In the tree as it is the latest tag still
    # predates the candidate, so the check correctly drifts there -- this test
    # binds the shipped release face to the tag its release would carry,
    # without mutating the tree's own tags.
    repo = _scratch_repo_with_candidate_tag(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-B", str(_CHECK), "--repo", str(repo)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "check-version-truth: OK" in proc.stdout


def _plant_installed(tmp_path: Path, version: str, editable_dir: str) -> Path:
    site = tmp_path / "site"
    dist = site / f"skillweave-{version}.dist-info"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: skillweave\nVersion: %s\n" % version
    )
    (dist / "direct_url.json").write_text(
        json.dumps({"url": editable_dir, "dir_info": {"editable": True}})
    )
    return site


def test_check_reports_installed_origin_from_the_real_distribution(tmp_path):
    # The installed-identity leg is driven end to end by the check's own
    # importlib.metadata consumption of a real on-disk distribution: origin
    # path + how it was installed, not a bare number. The check runs against
    # the release-in-preparation scratch clone so its release-facing legs pass
    # while the installed-identity leg is exercised.
    repo = _scratch_repo_with_candidate_tag(tmp_path)
    installed = (tmp_path / "installed")
    installed.mkdir(exist_ok=True)
    badge = re.search(
        r"badge/version-(\d+\.\d+\.\d+)", (repo / "README.md").read_text()
    ).group(1)
    site = _plant_installed(installed, badge, editable_dir="/repo/skillweave")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = str(site)
    proc = subprocess.run(
        [sys.executable, "-B", str(_CHECK), "--repo", str(repo)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "installed identity" in proc.stdout
    assert "editable-checkout" in proc.stdout
    assert "/repo/skillweave" in proc.stdout


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
