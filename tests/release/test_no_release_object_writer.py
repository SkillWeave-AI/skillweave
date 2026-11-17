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

Honest scope: this is a syntactic guard over workflow text, not a sandbox. Two
shapes are outside its reach, and both are pinned as *stated limits* rather
than left to prose: an obfuscated writer (base64, an alias, a script fetched
while running), and a target assembled at run time from something the workflow
never writes down. Literal ``NAME=value`` assignments *are* resolved, so a URL
held in a plain shell variable is still inspected; only a computed one escapes.
``test_an_unresolvable_target_is_a_stated_limit_not_a_passing_check`` asserts
that the second shape passes today, so a change in that fact forces the
docstrings and this module to be corrected instead of the gap being quietly
relabelled as closed.
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
    # ── A body writes without any method token (independent verification
    #    found all of these passing the first version of the check) ──────────
    "curl_data": 'curl -d "tag_name=v1" https://forge/api/v1/repos/o/r/releases',
    "curl_data_binary": (
        "curl --data-binary @body.json https://forge/api/v1/repos/o/r/releases"
    ),
    "curl_upload_file": "curl -T payload.zip https://forge/api/v1/repos/o/r/releases",
    "curl_form": "curl -F file=@x https://forge/api/v1/repos/o/r/releases",
    "wget_post_data": (
        'wget --post-data="tag_name=v1" https://forge/api/v1/repos/o/r/releases'
    ),
    "urlopen_data": (
        "python3 -c \"from urllib.request import urlopen; "
        "urlopen('https://forge/api/v1/repos/o/r/releases', data=b'x')\""
    ),
    # httpie names the method bare, with no flag at all.
    "httpie_method": "http POST https://forge/api/v1/repos/o/r/releases",
    # actions/github-script's generic request carries the method in the path
    # argument; four workflows in this repository already enable that action.
    "github_request_post": (
        'return github.request("POST /repos/o/r/releases", {tag_name: "v1"})'
    ),
    # A URL held in a literal shell variable is resolved before the path test.
    "variable_url_literal": (
        "URL=https://forge/api/v1/repos/o/r/releases\n"
        'curl -X POST "$URL"'
    ),
    # ── Attached/equals flag forms and non-shell clients (round 3). `curl
    #    -d'x'` is how the flag is usually written; the first check only knew
    #    the space-separated form, and `-fsSL` must NOT be mistaken for `-F`. ──
    "curl_data_attached": "curl -d'tag_name=v1' https://forge/api/v1/repos/o/r/releases",
    "curl_data_equals": (
        "curl --data='tag_name=v1' https://forge/api/v1/repos/o/r/releases"
    ),
    "curl_data_binary_equals": (
        "curl --data-binary=@body.json https://forge/api/v1/repos/o/r/releases"
    ),
    "curl_upload_attached": (
        "curl -Tpayload.zip https://forge/api/v1/repos/o/r/releases"
    ),
    "curl_form_attached": "curl -Ff=@x https://forge/api/v1/repos/o/r/releases",
    "curl_json": (
        "curl --json '{\"tag_name\":\"v1\"}' https://forge/api/v1/repos/o/r/releases"
    ),
    "node_fetch": (
        "node -e 'fetch(\"https://forge/api/v1/repos/o/r/releases\","
        "{method:\"POST\"})'"
    ),
    "powershell_irm": (
        "pwsh -Command \"Invoke-RestMethod -Uri "
        "https://forge/api/v1/repos/o/r/releases -Method Post -Body $b\""
    ),
    "xh_method": "xh POST https://forge/api/v1/repos/o/r/releases tag_name=v1",
    # ── Bundled short flags, long flag synonyms and inferred-body clients
    #    (round 4). curl bundles boolean flags with its body flag (`-sSd body`),
    #    every long spelling of a body flag counts (`--data-ascii`,
    #    `--form-string`), gh's `-F` infers POST like `-f`, and httpie/xh infer
    #    a POST from a `name=value` body item with no method word at all. A
    #    pipe ends the curl command, so a downstream `tar -T -` is not read as
    #    an upload. ──────────────────────────────────────────────────────────
    "gh_short_field": "gh api -F tag_name=v1 /repos/o/r/releases",
    "gh_short_field_attached": "gh api -Ftag_name=v1 /repos/o/r/releases",
    "curl_data_ascii": (
        "curl --data-ascii 'tag_name=v1' https://forge/api/v1/repos/o/r/releases"
    ),
    "curl_form_string": (
        "curl --form-string 'tag_name=v1' https://forge/api/v1/repos/o/r/releases"
    ),
    "curl_bundled_short": "curl -sSd 'tag_name=v1' https://forge/api/v1/repos/o/r/releases",
    "curl_d_attached_word": "curl -dx https://forge/api/v1/repos/o/r/releases",
    "curl_d_attached_novalue": "curl -dnoeq https://forge/api/v1/repos/o/r/releases",
    "curl_t_file": "curl -Tpayload.zip https://forge/api/v1/repos/o/r/releases",
    "httpie_body_item": "http https://forge/api/v1/repos/o/r/releases tag_name=v1",
    "xh_json_body_item": (
        "xh --json POST https://forge/api/v1/repos/o/r/releases tag_name=v1"
    ),
}

# The one shape this syntactic check cannot resolve: the target is assembled at
# run time from something the workflow never writes down. Pinned as an explicit
# *stated limit* so nobody mistakes the absence of a finding for a proof of
# safety. If a later change claims to close it, this test goes red and forces
# the docstrings and this table to be updated honestly.
_STATED_LIMIT_SPELLING = (
    "RFL013_UNRESOLVABLE_URL=https://forge/api/v1/repos/o/r/releases\n"
    'curl -X POST "$RELEASE_URL"'
)


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


def test_an_unresolvable_target_is_a_stated_limit_not_a_passing_check(tmp_path):
    """The check cannot see a URL the workflow never writes down. That is a
    limit, and it is pinned here rather than left to prose: this spelling is
    *not* refused (rc=0). If the check ever learns to refuse it, this test
    fails, the docstrings have to be corrected, and the gap is not quietly
    claimed closed by a comment."""
    root = _synthetic_tree(tmp_path, _STATED_LIMIT_SPELLING)

    proc = _run(root)

    assert proc.returncode == 0, (
        "the unresolvable-target case is expected to pass; if the check now "
        "refuses it, update the docstrings' stated limit and this test\n"
        f"{proc.stderr}"
    )


def test_a_read_of_the_release_path_is_not_refused(tmp_path):
    """The check is about writes. Reading a release (GET /releases) and running
    the verification script are legitimate and must stay green, or the check
    would fight the workflow it protects. The piped-download and forced-GET
    shapes are here because a body flag in the same command line as a
    *downstream* `tar -T -` or a `-G` must not be mistaken for a write."""
    roots = [
        _synthetic_tree(
            tmp_path / "get",
            "curl -fsSL https://forge/api/v1/repos/o/r/releases/tags/v1.0.0",
        ),
        _synthetic_tree(
            tmp_path / "gh-get", "gh api /repos/o/r/releases/tags/v1.0.0"
        ),
        _synthetic_tree(
            tmp_path / "verify",
            "python3 scripts/verify-release-attachments.py --tag v1.0.0",
        ),
        _synthetic_tree(
            tmp_path / "pipe-download",
            "curl -fsSL https://forge/api/v1/repos/o/r/releases/download/v1/a.tar.gz"
            " | tar -xz -T -",
        ),
        _synthetic_tree(
            tmp_path / "forced-get",
            "curl -G --data-urlencode x=y https://forge/api/v1/repos/o/r/releases",
        ),
        _synthetic_tree(
            tmp_path / "httpie-query",
            "http -h https://forge/api/v1/repos/o/r/releases tag==v1",
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
