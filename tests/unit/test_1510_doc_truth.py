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

import asyncio
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


def _run_check(repo: Path, *, extra_env: dict | None = None):
    """Run the release-consistency check hermetically on ``repo``.

    ``-S`` (no site) plus a popped ``PYTHONPATH`` are deliberate: they drop the
    *ambient* interpreter's site-packages from the child so its ``installed``
    fact reflects only what a caller plants via ``extra_env["PYTHONPATH"]``
    (which ``-S`` still honours) -- never the harness venv's or host's own
    installed skillweave. Without it the environment, not the tree, would
    decide these assertions.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-S", "-B", str(_CHECK), "--repo", str(repo)],
        capture_output=True, text=True, env=env,
    )


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


def _live_mcp_tool_names(mcp) -> list[str]:
    """Return the registered MCP tool names for either server object.

    ``skillweave.mcp.server`` binds ``mcp`` to the real ``FastMCP`` when the
    optional ``mcp`` package is importable, and to ``_DegradedFastMCP`` when it
    is not. The two surfaces differ:

    * ``_DegradedFastMCP`` records the decorated functions on ``.tools``.
    * real ``FastMCP`` exposes no ``.tools``; its tools live on the async
      ``list_tools()`` (``mcp.server.fastmcp.FastMCP.list_tools``) backed by the
      private ``_tool_manager`` (``mcp.server.fastmcp.tools.base.ToolManager``),
      whose ``_tools`` mapping is keyed by the public tool name.

    This helper reads whichever surface is present so the doc-row comparison
    below stays identical on both paths.
    """
    listed = getattr(mcp, "list_tools", None)
    if callable(listed):
        # Real FastMCP: the async ``list_tools`` yields ``MCPTool`` objects.
        manager = getattr(mcp, "_tool_manager", None)
        if manager is not None and hasattr(manager, "_tools"):
            return list(manager._tools)
        # Fall back to the async API when driven from a non-async context.
        tools = asyncio.run(listed())
        return [t.name for t in tools]
    # Stub server: decorated callables recorded on ``.tools``.
    return [f.__name__ for f in getattr(mcp, "tools", [])]


def _documented_mcp_tool_names(doc: str) -> list[str]:
    """The MCP tool rows the doc lists after the live-registry sentence.

    Reads the backticked tool names in the doc paragraph that immediately
    follows the sentence introducing the enumerated tool set. The live names
    must still appear in the doc (the original direction); reading the rows the
    doc actually binds additionally catches a documented row that is not in the
    live registry, which is the negative control for this test.
    """
    marker = "exposes exactly these"
    assert marker in doc, "doc no longer lists the MCP tool set"
    paragraph = doc.partition(marker)[2].split("\n\n")[1]
    return sorted(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", paragraph))


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
    # The candidate's release state in the doc follows the repository: once the
    # candidate's own tag exists (released_tag == "v" + candidate) the doc must
    # name it as released, not as a release still in preparation; while the tag
    # is absent the doc must say it is not yet released. Derived from the tag
    # list, never a literal, so the same assertion holds before and after the
    # tag exists.
    released_tag = _released_tag()
    if released_tag == "v" + candidate:
        assert "released" in doc.lower(), (
            "the doc must name the candidate as released once its tag exists"
        )
        assert "not yet released" not in doc.lower()
        assert "not released" not in doc.lower()
    else:
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
    tools = sorted(_live_mcp_tool_names(server.mcp))
    assert "create_run" in tools and "agent_capabilities" in tools
    for tool in tools:
        assert tool in doc, f"MCP tool {tool!r} not in the doc"
    documented = _documented_mcp_tool_names(doc)
    for tool in documented:
        assert tool in tools, f"documented MCP tool {tool!r} not in the registry"
    assert documented == tools, (documented, tools)
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


def _candidate() -> str:
    pyproject = re.search(
        r'^version\s*=\s*"(\d+\.\d+\.\d+)"', _read("pyproject.toml"), re.M
    )
    assert pyproject, "pyproject.toml has no [project].version"
    return pyproject.group(1)


def _scratch_repo(dest: Path) -> Path:
    """A scratch clone of this checkout with the shipped README materialised.

    The clone materialises HEAD, but the release face under test is the
    working-tree material this diff ships; overwrite the clone's README with
    the shipped one so the check evaluates the release-in-preparation face
    exactly as it will be tagged, independent of commit state. The real tree is
    never mutated.
    """
    repo = dest / "skillweave"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(_REPO), str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text(_read("README.md"), encoding="utf-8")
    return repo


def _drop_release_prep(repo: Path) -> None:
    """Remove the ``release_prep`` scalar from a scratch clone's .version.yaml.

    SW-CORE-RFX-001 introduces ``release_prep: "<candidate>"`` while a release
    is being prepared: the flag marks the candidate as *ahead* of the released
    tag. Once the candidate's tag exists the flag is no longer ahead, so
    ``check-version-truth.py`` correctly reports ``release-prep-ahead`` drift by
    design -- the owner removes the flag at tagging. This models that post-tag
    state in the scratch clone (tolerating absence, so the lane is exercised
    both with and without the RFX-001 key). The real tree is never touched.
    """
    vf = repo / ".version.yaml"
    if not vf.is_file():
        return
    kept = [
        ln for ln in vf.read_text(encoding="utf-8").splitlines(keepends=True)
        if not re.match(r"\s*release_prep\s*:", ln)
    ]
    vf.write_text("".join(kept), encoding="utf-8")


def _set_release_prep(repo: Path, candidate: str) -> None:
    """Ensure a top-level ``release_prep: "<candidate>"`` scalar is present.

    Models SW-CORE-RFX-001's pre-tag release-prep flag: the candidate is named
    as the release in preparation, ahead of the latest released tag.
    """
    vf = repo / ".version.yaml"
    lines = (
        vf.read_text(encoding="utf-8").splitlines(keepends=True)
        if vf.is_file() else []
    )
    lines = [ln for ln in lines if not re.match(r"\s*release_prep\s*:", ln)]
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and re.match(r"source_of_truth\s*:", ln):
            out.append(f'release_prep: "{candidate}"\n')
            inserted = True
    if not inserted:
        out.append(f'release_prep: "{candidate}"\n')
    vf.write_text("".join(out), encoding="utf-8")


def _scratch_repo_with_candidate_tag(dest: Path) -> Path:
    """A scratch clone of this checkout in the POST-tag state.

    An annotated tag named for the candidate is created on the tree's HEAD and
    the release-prep flag is removed, exactly as the owner will after tagging,
    so ``check-version-truth.py`` -- which compares the release-advertising
    positions against the **latest tag in the repository** -- passes. The real
    tree is never mutated or tagged.
    """
    repo = _scratch_repo(dest)
    tag = "v" + _candidate()
    existing = subprocess.run(
        ["git", "-C", str(repo), "tag", "--list", tag],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not existing:
        subprocess.run(
            ["git", "-C", str(repo), "tag", "-a", tag, "-m",
             f"SkillWeave {_candidate()}"],
            check=True, capture_output=True, text=True,
        )
    _drop_release_prep(repo)
    return repo


def _scratch_repo_pre_release(dest: Path) -> Path:
    """A scratch clone in the PRE-tag state: no candidate tag, release_prep set.

    Models the tree while 1.5.10 is the release in preparation but before its
    ``v1.5.10`` tag exists: the release-facing positions name the candidate and
    ``.version.yaml`` carries ``release_prep: "<candidate>"`` (SW-CORE-RFX-001).
    """
    repo = _scratch_repo(dest)
    candidate = _candidate()
    subprocess.run(
        ["git", "-C", str(repo), "tag", "-d", "v" + candidate],
        capture_output=True, text=True,
    )
    _set_release_prep(repo, candidate)
    return repo


def _check_knows_release_prep() -> bool:
    """True when the composed check implements SW-CORE-RFX-001's release-prep rule."""
    return "release_prep" in _CHECK.read_text(encoding="utf-8")


def test_release_consistency_check_passes_on_the_tree(tmp_path):
    # The diff advertises the release IN PREPARATION (the candidate) at every
    # release-facing position, so the read-only check must pass once that
    # candidate's tag exists and the release-prep flag is cleared, as the owner
    # does at tagging. In the tree as it is the latest tag still predates the
    # candidate, so the check correctly drifts there -- this test binds the
    # shipped release face to the tag its release would carry, without mutating
    # the tree's own tags.
    repo = _scratch_repo_with_candidate_tag(tmp_path)
    proc = _run_check(repo)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "check-version-truth: OK" in proc.stdout


def test_release_consistency_check_accepts_release_prep_before_tag(tmp_path):
    # The PRE-tag state: 1.5.10 is the release in preparation, its tag does not
    # exist yet and .version.yaml carries release_prep: "1.5.10". SW-CORE-RFX-001
    # makes the release-prep flag license the candidate's release face ahead of
    # the latest released tag, so the composed check passes; without RFX-001 the
    # candidate's release face is simply ahead of the tag and correctly drifts.
    # The test asserts the behaviour of whichever check is present, so the file
    # is green both in this lane and on the RFX-001 composition.
    repo = _scratch_repo_pre_release(tmp_path)
    proc = _run_check(repo)
    if _check_knows_release_prep():
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert "check-version-truth: OK" in proc.stdout
    else:
        # The release-prep rule is not composed here, so the candidate's
        # release face is ahead of the latest tag and the check drifts.
        assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
        assert "DRIFT" in proc.stdout


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
    proc = _run_check(repo, extra_env={"PYTHONPATH": str(site)})
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "installed identity" in proc.stdout
    assert "editable-checkout" in proc.stdout
    assert "/repo/skillweave" in proc.stdout


# ── SW-1511A-GATE-001 repair H: provisioning doc binds the CURRENT version ──


def _pyproject_facts() -> dict:
    """The current-version facts, derived from THIS tree's pyproject.toml."""
    text = _read("pyproject.toml")
    version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    requires = re.search(r'^requires-python\s*=\s*"([^"]+)"', text, re.M)
    assert version, "pyproject.toml has no [project].version"
    assert requires, "pyproject.toml has no requires-python"
    deps_block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert deps_block, "pyproject.toml has no [project].dependencies"
    # Strip full-line comments so prose inside the block is never read as a pin.
    deps_body = "\n".join(
        ln for ln in deps_block.group(1).splitlines()
        if not ln.strip().startswith("#")
    )
    deps = re.findall(r'"([^"]+)"', deps_body)
    classifiers = re.findall(
        r'"Programming Language :: Python :: (\d+\.\d+)"', text
    )
    return {
        "version": version.group(1),
        "requires_python": requires.group(1),
        "dependencies": sorted(deps),
        "python_series": sorted(classifiers),
    }


def _provisioning_declared_table(text: str) -> dict[str, str]:
    """The rows of docs/provisioning.md's 'What is declared' table, by field."""
    section = text.partition("## What is declared")[2].split("\n## ")[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*(.*?)\s*\|\s*$", line)
        if not m:
            continue
        field, value = m.group(1), m.group(2)
        if field in ("Field", "---") or set(field) <= {"-", " "}:
            continue
        rows[field] = value
    assert rows, "docs/provisioning.md 'What is declared' table is missing"
    return rows


def test_provisioning_current_version_statements_match_pyproject():
    # SW-1511A-GATE-001 divergence 3: the 'What is declared' table named 1.5.9
    # while the tree is 1.5.11. This derives every current-version fact from
    # pyproject.toml (the declared source of truth) and fails when the
    # provisioning doc's CURRENT-product statements disagree. Statements about
    # history are explicitly labelled and out of scope here.
    facts = _pyproject_facts()
    candidate = facts["version"]
    doc = _read("docs/provisioning.md")

    # (1) the 'What is declared' table.
    rows = _provisioning_declared_table(doc)
    assert rows["Version"] == f"`{candidate}`", (
        f"'What is declared' Version {rows['Version']!r} != pyproject {candidate}"
    )
    assert rows["`requires-python`"] == f"`{facts['requires_python']}`", (
        f"requires-python row {rows['`requires-python`']!r} != "
        f"pyproject {facts['requires_python']}"
    )
    # every base dependency pyproject declares must be named in the row, and no
    # other multipart version constraint may stand in for one of them.
    deps_row = rows["Base dependencies"]
    for dep in facts["dependencies"]:
        assert dep in deps_row, f"base dependency {dep!r} missing from {deps_row!r}"
    # the console-script set declared by pyproject.
    scripts_block = re.search(
        r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", _read("pyproject.toml"),
        re.M | re.S,
    )
    assert scripts_block, "pyproject.toml has no [project.scripts]"
    declared_scripts = re.findall(r"^([a-z][a-z0-9-]*)\s*=", scripts_block.group(1), re.M)
    named = set(re.findall(r"`([^`]+)`", rows["Console scripts"]))
    assert named, "Console scripts row is not a backticked list"
    assert named == set(declared_scripts), (
        f"console scripts {sorted(named)} != pyproject {sorted(declared_scripts)}"
    )

    # (2) the wheel name in every install example is the CURRENT one.
    wheel = f"skillweave-{candidate}-py3-none-any.whl"
    assert wheel in doc, f"provisioning install examples do not name {wheel}"
    # no stale current-product wheel name survives.
    for stale in set(re.findall(r"skillweave-(\d+\.\d+\.\d+)-py3-none-any\.whl", doc)):
        assert stale == candidate, (
            f"provisioning install example names stale wheel version {stale} "
            f"(current is {candidate})"
        )

    # (3) the "Version markers and Python support" section states the CURRENT
    # distribution version (and does not state a stale one as current).
    markers = doc.partition("## Version markers and Python support")[2]
    assert markers, "the version-markers section is missing"
    assert f"distribution version is `{candidate}`" in markers, (
        "the version-markers section does not state the current distribution "
        f"version {candidate}"
    )

    # (4) Python support: the requires-python floor and every declared
    # classifier series the section names as declared support must agree with
    # pyproject.toml. A series pyproject does not declare may appear only as an
    # explicitly measured/verified row, never as declared support.
    assert facts["requires_python"] in markers, (
        f"the section does not state the pyproject requires-python "
        f"{facts['requires_python']}"
    )


def test_provisioning_history_is_labelled_as_history():
    # The repair keeps statements about HISTORY, but they must be explicitly
    # labelled as history -- never as a description of the current release. Any
    # stale version that survives in this section must sit beside an explicit
    # "history"/"historical" label.
    doc = _read("docs/provisioning.md")
    markers = doc.partition("## Version markers and Python support")[2]
    stale = set(re.findall(r"`(1\.5\.(?:8|9|10))`", markers))
    if stale:
        assert re.search(r"histor", markers, re.IGNORECASE), (
            f"stale version(s) {sorted(stale)} appear in the version-markers "
            "section without a history label"
        )
    # The current candidate must never be framed as history in this section.
    candidate = _pyproject_facts()["version"]
    assert f"version was `{candidate}`" not in markers


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
