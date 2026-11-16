"""RFL-013 — the release-object-writer check is wired in and keeps its teeth.

``scripts/check-no-release-object-writer.py`` is the red proof the lane names:
it must exit non-zero against the base and 0 against the candidate. A check
that nothing runs proves nothing, so this module *runs* it — the same two
invocations, from a pytest process — and then probes the spellings it claims
to catch.

Two things are asserted and cannot be satisfied by prose:

* **Red proof.** The base tree is materialised with ``git archive`` at
  ``BASE_COMMIT`` and the very same script must exit 1 there, naming the
  engine writer lines; against ``REPO_ROOT`` it must exit 0.
* **Teeth.** A table of release-object writes, each of which a reviewer could
  plausibly write, must each make the check exit 1 on a synthetic tree. A
  spelling the script stops catching turns this module red instead of silently
  reopening the gap the check exists to close.

The synthetic trees are tiny: two required workflow files plus one ``run:``
block. Nothing here touches the repository's own workflows or the network.

Honest scope: this is a syntactic guard over workflow text. An obfuscated
writer (base64, an alias, a script fetched at run time) is outside its reach;
that limit is stated in the script's docstring rather than papered over.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_REL = "scripts/check-no-release-object-writer.py"
SCRIPT_PATH = REPO_ROOT / SCRIPT_REL
BASE_COMMIT = "f217f8997753e423deeb116cf48fe7cd34fde454"

CANDIDATE_WORKFLOW_REL = ".forgejo/workflows/forgejo-release.yml"
GITHUB_WORKFLOW_REL = ".github/workflows/release.yml"


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--root", str(root)],
        capture_output=True,
        text=True,
    )


# ── The red proof, at the base and at the candidate ────────────────────────


@pytest.fixture(scope="module")
def base_tree(tmp_path_factory) -> Path:
    """A pristine checkout of the base commit, via ``git archive`` — the same
    tree the red proof names, not a hand-made approximation of it."""
    root = tmp_path_factory.mktemp("base") / "tree"
    root.mkdir()
    archive = subprocess.run(
        ["git", "archive", BASE_COMMIT],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["tar", "-x", "-C", str(root)], input=archive.stdout, check=True
    )
    return root


def test_check_exits_non_zero_at_base(base_tree):
    proc = _run(base_tree)

    assert proc.returncode == 1, (
        f"the check must refuse the base writer; rc={proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "ReleaseObjectWriterError" in proc.stderr
    # The base writer is the engine call, and it is named with its line.
    assert f"{CANDIDATE_WORKFLOW_REL}:641 ReleaseHandler" in proc.stderr, (
        f"the base writer line must be named:\n{proc.stderr}"
    )


def test_check_exits_zero_at_candidate():
    proc = _run(REPO_ROOT)

    assert proc.returncode == 0, (
        f"the candidate must pass; rc={proc.returncode}\n{proc.stderr}"
    )
    assert "ReleaseObjectWriterError" not in proc.stderr


# ── Teeth: every spelling of a release-object write is still caught ────────

_WRITE_SPELLINGS = {
    # The engine call the base carried.
    "engine_publish_release": "ReleaseHandler.publish_release(...)",
    # Forge-action writers.
    "softprops_action": "uses: softprops/action-gh-release@v2",
    # gh CLI, both method spellings and its body-field/input forms.
    "gh_method_equals": "gh api --method=post /repos/o/r/releases",
    "gh_method_space": "gh api --method POST /repos/o/r/releases",
    "gh_field": "gh api -f tag_name=v1 /repos/o/r/releases",
    "gh_input": "gh api --input body.json /repos/o/r/releases",
    "gh_release_create": "gh release create v1.0.0 --notes x",
    # curl, attached method, detached method and a backslash continuation.
    "curl_attached": "curl -XPOST https://forge/api/v1/repos/o/r/releases",
    "curl_detached": "curl -X POST https://forge/api/v1/repos/o/r/releases",
    "curl_request": "curl --request PATCH https://forge/api/v1/repos/o/r/releases",
    "curl_continued": (
        "curl -X POST \\\n            https://forge/api/v1/repos/o/r/releases"
    ),
    # A language-level client.
    "requests_post": (
        'python3 -c "import requests; '
        'requests.post(\'https://forge/api/v1/repos/o/r/releases\')"'
    ),
    # octokit through actions/github-script.
    "octokit": "github.rest.repos.createRelease({owner, repo})",
}


def _synthetic_tree(root: Path, body: str) -> Path:
    (root / ".forgejo" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / CANDIDATE_WORKFLOW_REL).write_text(
        "jobs:\n"
        "  release:\n"
        "    steps:\n"
        "      - name: candidate\n"
        "        run: |\n"
        "          set -euo pipefail\n"
        "          " + body.replace("\n", "\n          ") + "\n",
        encoding="utf-8",
    )
    (root / GITHUB_WORKFLOW_REL).write_text("jobs: {}\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("spelling", sorted(_WRITE_SPELLINGS))
def test_each_write_spelling_is_refused(tmp_path, spelling):
    body = _WRITE_SPELLINGS[spelling]
    root = _synthetic_tree(tmp_path, body)

    proc = _run(root)

    assert proc.returncode == 1, (
        f"{spelling!r} writes a release object but the check passed it; "
        f"rc={proc.returncode}\n{proc.stderr}"
    )
    assert "ReleaseObjectWriterError" in proc.stderr
    assert CANDIDATE_WORKFLOW_REL in proc.stderr, "the file must be named"


def test_a_read_of_the_release_path_is_not_refused(tmp_path):
    """The check is about writes. Reading a release (GET /releases) and running
    the verification script are legitimate and must stay green, or the check
    would fight the workflow it protects."""
    roots = [
        _synthetic_tree(
            tmp_path / "get",
            "curl -sS https://forge/api/v1/repos/o/r/releases/tags/v1.0.0",
        ),
        _synthetic_tree(
            tmp_path / "gh-get", "gh api /repos/o/r/releases/tags/v1.0.0"
        ),
        _synthetic_tree(
            tmp_path / "verify",
            "python3 scripts/verify-release-attachments.py --tag v1.0.0",
        ),
    ]

    for root in roots:
        proc = _run(root)
        assert proc.returncode == 0, (
            f"a release read must not be refused; rc={proc.returncode}\n"
            f"{proc.stderr}"
        )


def test_a_tree_without_the_release_workflow_cannot_pass(tmp_path):
    """Exit 2, not 0: the check cannot pass by being pointed somewhere the
    workflow does not exist."""
    (tmp_path / ".forgejo" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / GITHUB_WORKFLOW_REL).write_text("jobs: {}\n", encoding="utf-8")

    proc = _run(tmp_path)

    assert proc.returncode == 2, (
        f"a missing required workflow must not pass; rc={proc.returncode}\n"
        f"{proc.stderr}"
    )
    assert CANDIDATE_WORKFLOW_REL in proc.stderr


def test_the_check_is_exactly_the_file_it_claims():
    """The test runs the shipped script, not a copy: if the script is removed
    or renamed this module fails rather than skipping."""
    assert SCRIPT_PATH.is_file(), f"{SCRIPT_REL} is absent"
    assert shutil.which("git") is not None
