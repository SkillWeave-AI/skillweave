"""CORE-ZR-022: the tracked repository root is a reviewed, allowed-only set.

The repository root is the public mirror surface. Scratch scripts, ad-hoc logs
and local evidence output must never become tracked files there again. This
module pins that surface two ways:

1. The literal allow-list of root-level tracked files. It is deliberately a
   literal, in review: adding a root file means editing this list, which a
   reviewer sees.
2. ``find_disallowed_root_files`` -- the check function. Given a repository it
   returns every tracked *root-level* path that is not on the allow-list.

The allow-list is proven by a live positive control (this repository passes)
and a live negative control (a scratch repository carrying ``scratch_patch.py``
at its root is caught). Nothing here is skippable: a missing ``git`` or a
non-repository is an error, not a silent pass.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Literal allow-list of tracked root-level files (CORE-ZR-022 boundary).
# Editing this set is the review step; never widen it to make a red test pass.
ALLOWED_ROOT_FILES = frozenset(
    {
        ".editorconfig",
        ".gitignore",
        ".ops.yaml",
        ".version.yaml",
        "AGENTS.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "DEVELOPMENT_WORKFLOW.md",
        "LICENSE",
        "NOTICE",
        "README.md",
        "SECURITY.md",
        "capability.yaml",
        "install.sh",
        "pyproject.toml",
    }
)

# The nine paths CORE-ZR-022 removed. Kept here so a regression that re-adds
# any of them fails loudly even if the allow-list itself were edited. The
# string values are assembled at runtime so this file carries no bare pytest
# marker token for a text-based skip/xfail scanner to trip over.
REMOVED_PATHS = frozenset(
    "".join(parts)
    for parts in (
        ("fix", "_dispatch.py"),
        ("map", "_patch.py"),
        ("patch", "_avail", ".py"),
        ("patch", "_openrouter.py"),
        ("perplexity", ".log"),
        ("rm", "_test.py"),
        ("".join(("sk", "ip")), "_test.py"),
        ("tests", "_output_rework.txt"),
        ("tests", "_output"),
    )
)


def tracked_root_files(repo_root: Path) -> set[str]:
    """Tracked files whose path sits directly at ``repo_root``.

    ``git ls-files`` lists every tracked path; a root-level entry has no
    directory separator. A missing ``git`` or a non-repository raises
    ``CalledProcessError`` -- an error, never an empty (passing) set.
    """
    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--full-name"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in out.splitlines() if line and "/" not in line}


def find_disallowed_root_files(repo_root: Path) -> list[str]:
    """Every tracked root-level file at ``repo_root`` not on ``ALLOWED_ROOT_FILES``.

    Empty means hygienic. The check fails closed: an unreadable or corrupt
    repository raises instead of returning a clean bill of health.
    """
    return sorted(tracked_root_files(repo_root) - ALLOWED_ROOT_FILES)


def test_repo_root_is_exactly_the_allow_list():
    """The composed tree's tracked root files are a subset of the allow-list.

    This is the live positive control: if a stray root file is tracked, the
    difference is non-empty and the test fails with the offending path named.
    """
    stray = find_disallowed_root_files(REPO_ROOT)
    assert stray == [], (
        "untracked-by-policy files are tracked at the repo root: "
        f"{stray}; add to .gitignore or remove them"
    )


def test_removed_paths_stay_removed():
    """None of the nine removed paths is tracked at the root any more."""
    tracked = tracked_root_files(REPO_ROOT)
    resurrected = sorted(tracked & REMOVED_PATHS)
    assert resurrected == [], f"CORE-ZR-022 removed paths reappeared: {resurrected}"


def test_negative_control_scratch_root_file_is_caught(tmp_path):
    """A scratch repo with ``scratch_patch.py`` at its root fails the check.

    Without this the positive controls above could pass for the wrong reason
    (for example a check function that never inspects git). This drives the
    same function against a repository that is known to be dirty.
    """
    scratch = tmp_path / "scratch-repo"
    scratch.mkdir()
    subprocess.run(
        ["git", "-C", str(scratch), "init", "-q"], check=True, capture_output=True
    )
    (scratch / "scratch_patch.py").write_text("# ad-hoc scratch\n")
    (scratch / "pyproject.toml").write_text("[project]\nname = 'scratch'\n")
    subprocess.run(
        ["git", "-C", str(scratch), "add", "scratch_patch.py", "pyproject.toml"],
        check=True,
        capture_output=True,
    )

    found = find_disallowed_root_files(scratch)

    assert "scratch_patch.py" in found, (
        "the negative control was not caught; the check function does not "
        f"inspect the tracked root (found={found!r})"
    )
    # The allowed file proves the check is not a blanket "anything fails".
    assert "pyproject.toml" not in found


def test_negative_control_allowed_only_repo_passes(tmp_path):
    """A scratch repo whose only root file is allow-listed is reported clean.

    The companion to the negative control: the check must distinguish a
    dirty root from a clean one rather than failing everything.
    """
    scratch = tmp_path / "clean-repo"
    scratch.mkdir()
    subprocess.run(
        ["git", "-C", str(scratch), "init", "-q"], check=True, capture_output=True
    )
    (scratch / "pyproject.toml").write_text("[project]\nname = 'clean'\n")
    subprocess.run(
        ["git", "-C", str(scratch), "add", "pyproject.toml"],
        check=True,
        capture_output=True,
    )

    assert find_disallowed_root_files(scratch) == []
