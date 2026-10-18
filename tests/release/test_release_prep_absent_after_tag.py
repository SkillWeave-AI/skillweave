"""SW-CORE-ZR-018: ``release_prep`` is absent once the candidate is released.

``.version.yaml`` carries a flat top-level scalar ``release_prep: "X.Y.Z"``
(SW-CORE-RFX-001) that, while the candidate is strictly ahead of the latest
annotated product tag, licenses the release-advertising positions (README
version badge, "What's new" heading, CHANGELOG top) to name the still-unreleased
candidate. It exists only on a release-preparation commit: the moment the
candidate's own ``vX.Y.Z`` tag is created the flag is *no longer ahead* and
``scripts/check-version-truth.py`` reports the named ``release-prep-ahead``
DRIFT by design.

This module proves the post-tag contract:

* the shipped tree -- whose candidate equals the latest annotated tag -- omits
  ``release_prep`` and passes the read-only check cleanly; and
* re-introducing the flag on that same tree is *fails closed*: the check exits
  non-zero and names ``release-prep-ahead`` / ``DRIFT``.

State-aware: while the candidate is still a *release in preparation* (its own
``vX.Y.Z`` tag does not yet exist), ``release_prep`` is present by design and the
post-tag contract is exercised on an ephemeral scratch clone that models the
tagged tree (the candidate tag created, the flag dropped) -- never on the real
tree, which is never tagged. The branch is derived from the repository's tags,
never a literal version, so the file is green both before and after the owner
creates the tag.

The negative control mutates an ephemeral scratch clone (``git clone`` into the
pytest ``tmp_path``), never the real tree, and the candidate/tag are derived
from the repository -- never a literal version or hash -- so the assertion holds
for whichever release train the tree carries.

Hermetic: reads the tree, tags nothing (the scratch clone is tagged, not the
real tree), pushes nothing, needs no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_CHECK = _REPO / "scripts" / "check-version-truth.py"

_SEMVER = r"\d+\.\d+\.\d+"
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"(' + _SEMVER + r')"', re.MULTILINE)
_BUNDLE_VERSION_RE = re.compile(r"^version:\s*(" + _SEMVER + r")\s*$", re.MULTILINE)
_RELEASE_PREP_RE = re.compile(r'^release_prep:\s*"?' + "(" + _SEMVER + r')"?\s*$', re.MULTILINE)
_TAG_RE = re.compile(r"v" + _SEMVER)


# -- derivation helpers (never a literal version or hash) --------------------


def _read(repo: Path, rel: str) -> str:
    return (repo / rel).read_text(encoding="utf-8")


def _candidate(repo: Path) -> str:
    m = _PYPROJECT_VERSION_RE.search(_read(repo, "pyproject.toml"))
    assert m, "pyproject.toml has no [project].version"
    return m.group(1)


def _bundle(repo: Path) -> str:
    m = _BUNDLE_VERSION_RE.search(_read(repo, "capability.yaml"))
    assert m, "capability.yaml has no top-level version:"
    return m.group(1)


def _release_prep(repo: Path) -> str | None:
    m = _RELEASE_PREP_RE.search(_read(repo, ".version.yaml"))
    return m.group(1) if m else None


def _latest_release_tag(repo: Path) -> str:
    """The released fact: the highest semver ``vX.Y.Z`` tag -- never a literal."""
    out = subprocess.run(
        ["git", "-C", str(repo), "tag", "--list", "v*"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    tags = [t for t in out if _TAG_RE.fullmatch(t)]
    assert tags, "no vX.Y.Z release tag found in the repository"
    return max(tags, key=lambda t: tuple(int(p) for p in t[1:].split(".")))


def _set_release_prep(repo: Path, version: str) -> None:
    """Re-introduce a top-level ``release_prep: "<version>"`` scalar.

    Inserted after ``source_of_truth:`` (the flat-scalar region the version-sync
    parser accepts) so the mutated clone is representable, matching how the
    owner would set the flag on a release-preparation commit.
    """
    vf = repo / ".version.yaml"
    lines = [
        ln for ln in vf.read_text(encoding="utf-8").splitlines(keepends=True)
        if not re.match(r"\s*release_prep\s*:", ln)
    ]
    out: list[str] = []
    inserted = False
    for ln in lines:
        out.append(ln)
        if not inserted and re.match(r"source_of_truth\s*:", ln):
            out.append(f'release_prep: "{version}"\n')
            inserted = True
    assert inserted, ".version.yaml has no source_of_truth: line to anchor release_prep"
    vf.write_text("".join(out), encoding="utf-8")


def _scratch_clone(dest: Path) -> Path:
    """An ephemeral clone of this checkout carrying its tags.

    ``git clone`` copies the annotated product tags. The real tree is never
    mutated or tagged.
    """
    repo = dest / "skillweave"
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(_REPO), str(repo)],
        check=True, capture_output=True, text=True,
    )
    return repo


def _drop_release_prep(repo: Path) -> None:
    """Remove the top-level ``release_prep`` scalar, as the owner does at tagging."""
    vf = repo / ".version.yaml"
    kept = [
        ln for ln in vf.read_text(encoding="utf-8").splitlines(keepends=True)
        if not re.match(r"\s*release_prep\s*:", ln)
    ]
    vf.write_text("".join(kept), encoding="utf-8")


def _post_tag_clone(dest: Path) -> Path:
    """An ephemeral clone modelling the POST-tag tree.

    The candidate's own annotated ``vX.Y.Z`` tag is created on the clone's HEAD
    and the ``release_prep`` flag is dropped, exactly as the owner does once the
    tag exists -- so the clone is a faithful post-tag tree regardless of whether
    the real tree is still a release in preparation. The real tree is never
    mutated or tagged.
    """
    repo = _scratch_clone(dest)
    candidate = _candidate(repo)
    tag = "v" + candidate
    exists = subprocess.run(
        ["git", "-C", str(repo), "tag", "--list", tag],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not exists:
        subprocess.run(
            ["git", "-C", str(repo), "tag", "-a", tag, "-m",
             f"SkillWeave {candidate}"],
            check=True, capture_output=True, text=True,
        )
    _drop_release_prep(repo)
    return repo


def _run_check(repo: Path, *extra: str):
    # ``-S`` (no site) plus a popped PYTHONPATH are deliberate: they drop the
    # *ambient* interpreter's site-packages from the child so its ``installed``
    # fact is reported as "not installed" rather than as whatever skillweave
    # some host venv happens to carry -- the environment, not the tree, would
    # otherwise decide these assertions.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-S", "-B", str(_CHECK), "--repo", str(repo), *extra],
        capture_output=True, text=True, env=env,
    )


# -- the post-tag contract ---------------------------------------------------


def test_shipped_tree_omits_release_prep_after_its_tag_exists(tmp_path):
    # State-aware. Once the candidate's own tag exists the SHIPPED tree is the
    # released tree: it carries no release_prep and the check passes cleanly.
    # While the candidate is a release in preparation (its tag does not yet
    # exist) the post-tag contract is modelled on an ephemeral post-tag clone,
    # because the real tree legitimately still carries the flag. Derived from
    # the repository's tags, never a literal.
    tag = _latest_release_tag(_REPO)
    candidate = _candidate(_REPO)
    if tag == "v" + candidate:
        tree = _REPO
        assert _release_prep(_REPO) is None, (
            "release_prep must be absent once the candidate's tag exists"
        )
        assert not re.search(
            r"^release_prep\s*:", _read(_REPO, ".version.yaml"), re.M
        ), "a top-level release_prep key must not exist in the released tree"
    else:
        # Release in preparation: the post-tag tree is modelled on a clone.
        tree = _post_tag_clone(tmp_path)
        assert _latest_release_tag(tree) == "v" + _candidate(tree)
        assert _release_prep(tree) is None
        assert not re.search(
            r"^release_prep\s*:", _read(tree, ".version.yaml"), re.M
        ), "the post-tag clone must carry no release_prep key"
    # The composed read-only check agrees: released tree, no drift.
    proc = _run_check(tree)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "check-version-truth: OK" in proc.stdout
    assert "DRIFT" not in proc.stdout
    assert "release-prep-ahead" not in proc.stdout


def test_reintroducing_release_prep_after_the_tag_fails_closed(tmp_path):
    # The composed check must implement the released-tree rule; guard loudly
    # rather than skip, so a composition that dropped it fails here, not in
    # silence.
    assert "release-prep-ahead" in _CHECK.read_text(encoding="utf-8"), (
        "the composed check does not implement the release-prep-ahead rule"
    )

    repo = _post_tag_clone(tmp_path)
    tag = _latest_release_tag(repo)
    candidate = _candidate(repo)
    assert tag == "v" + candidate, (
        f"the scratch clone must be a post-tag tree; latest tag {tag} != "
        f"candidate v{candidate}"
    )
    # Baseline: the clone is a faithful post-tag tree, clean before mutation.
    assert _release_prep(repo) is None
    baseline = _run_check(repo)
    assert baseline.returncode == 0, (
        baseline.returncode, baseline.stdout, baseline.stderr
    )

    # NEGATIVE CONTROL: re-add the flag while the candidate EQUALS the tag.
    _set_release_prep(repo, candidate)
    assert _release_prep(repo) == candidate

    proc = _run_check(repo, "--json")
    assert proc.returncode == 1, (
        "a release_prep present while the candidate equals its own tag must be "
        f"a DRIFT, got rc={proc.returncode}: {proc.stdout} {proc.stderr}"
    )
    report = json.loads(proc.stdout)
    assert report["release_prep"] == candidate
    assert report["ok"] is False
    labels = {c["label"] for c in report["checks"]}
    assert "release-prep-ahead" in labels, labels
    assert any(
        c["label"] == "release-prep-ahead" and not c["ok"] for c in report["checks"]
    )

    # The human-readable path names the drift too.
    text = _run_check(repo)
    assert text.returncode == 1
    assert "DRIFT" in text.stdout
    assert "release-prep-ahead" in text.stdout


if __name__ == "__main__":  # pragma: no cover
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
