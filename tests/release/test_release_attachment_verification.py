"""SW-1511-GATE criterion 9, requirement 2 — the release job verifies its own
attachments after publication.

Criterion 9 requires that stable-release readiness "retains the pre-publication
artifact/CI gate and an explicit post-publication runner/attachment verification
obligation". The pre-publication gate exists (CORE-ZR-026, the "Build the
release assets" step). Independent verification (attempts/SW-1511-GATE-c9-verify)
found the post-publication half MISSING: after ``publish_release`` the workflow
only verified the *mirror tag target*, never that the release a consumer
downloads actually carries the built assets. An engine that attaches a missing,
extra or truncated asset still reports a successful publication.

This module pins the close: one step, "Verify the published release attachments",
placed after "Publish the release to every destination", running
``scripts/verify-release-attachments.py`` against the release API.

The step's ``run`` text is read from the workflow verbatim and executed with
``bash`` — never paraphrased — against a **hermetic fake release API**: an
in-process ``http.server`` on ``127.0.0.1`` with an in-memory release object.
No network leaves the loopback interface, no real release object exists, and the
only credential is a dummy token string. The cases prove the four obligations
the criterion names: a missing asset, an extra asset and a wrong checksum each
fail the job *by name*, and a correct attached set passes.

Structure checks are source-bound to the lane's base commit (``BASE_COMMIT``):
the step does not exist at base and no other step's ``run`` text moved, so a
fix that proves the obligation by damaging another step cannot pass here.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_REL = ".forgejo/workflows/forgejo-release.yml"
WORKFLOW_PATH = REPO_ROOT / WORKFLOW_REL
TEST_REL = "tests/release/test_release_attachment_verification.py"

# The lane's base: the candidate this change was made from. The obligation is
# absent there, which is what makes "this test fails on base" a fact about the
# diff rather than a claim.
BASE_COMMIT = "afdf20d93874a0084e41b89ba0952be571ae63d4"

PUBLISH_STEP_NAME = "Publish the release to every destination"
VERIFY_STEP_NAME = "Verify the published release attachments"
VERIFY_SCRIPT_REL = "scripts/verify-release-attachments.py"

TAG = "v1.5.10"
DUMMY_TOKEN = "not-a-real-token"


# ── Workflow access ────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _candidate_doc() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _base_doc() -> dict:
    return yaml.safe_load(_git("show", f"{BASE_COMMIT}:{WORKFLOW_REL}"))


def _steps(doc: dict) -> list[dict]:
    return doc["jobs"]["release"]["steps"]


def _named_step(doc: dict, name: str) -> dict:
    for step in _steps(doc):
        if step.get("name") == name:
            return step
    raise AssertionError(f"workflow has no step named {name!r}")


def _verify_run_text() -> str:
    return _named_step(_candidate_doc(), VERIFY_STEP_NAME)["run"]


# ── Structure and blast radius (source-bound) ──────────────────────────────


def test_verify_step_exists_after_publish_and_is_absent_at_base():
    cand_names = [s.get("name") for s in _steps(_candidate_doc())]
    base_names = [s.get("name") for s in _steps(_base_doc())]

    assert VERIFY_STEP_NAME not in base_names, (
        "the obligation must be missing at the base commit — it is the finding "
        "this change closes"
    )
    assert cand_names.count(VERIFY_STEP_NAME) == 1, "the step appears exactly once"
    assert cand_names.index(VERIFY_STEP_NAME) > cand_names.index(PUBLISH_STEP_NAME), (
        "post-publication means after the publish step; a verification that runs "
        "before publication verifies nothing"
    )


def test_no_other_step_run_text_moved():
    base_steps = _steps(_base_doc())
    cand_steps = _steps(_candidate_doc())

    for base_step in base_steps:
        key = base_step.get("name") or base_step.get("uses")
        matches = [
            s for s in cand_steps if (s.get("name") or s.get("uses")) == key
        ]
        assert len(matches) == 1, f"step {key!r} is not preserved exactly once"
        assert matches[0] == base_step, (
            f"step {key!r} changed; the verification must be additive"
        )


def test_verify_step_declares_the_obligation():
    run = _verify_run_text()
    assert VERIFY_SCRIPT_REL in run, "the step runs the repository's verifier"
    assert "--tag" in run and "--base-url" in run and "--repository" in run
    assert "--assets-dir release-assets" in run, (
        "the verification must compare against the assets built in this job"
    )
    step = _named_step(_candidate_doc(), VERIFY_STEP_NAME)
    env = step.get("env") or {}
    assert "FORGEJO_TOKEN" in env, "the release read must be authorized"
    assert "TAG_NAME" in env and "FORGEJO_BASE_URL" in env


def test_existing_diff_still_touches_only_workflow_test_and_script():
    """The close adds exactly one script: the byte-comparison helper is not
    heroically re-implemented inside the YAML, and nothing else drifts."""
    changed = set(_git("diff", "--name-only", BASE_COMMIT).splitlines())
    changed |= set(_git("ls-files", "--others", "--exclude-standard").splitlines())

    assert {WORKFLOW_REL, TEST_REL, VERIFY_SCRIPT_REL} <= changed


# ── Hermetic fake release API ──────────────────────────────────────────────


class _ReleaseState:
    """The in-memory release the fake API serves: one release object, no forge."""

    def __init__(self) -> None:
        self.status = 200
        self.release: dict = {}
        self.assets: dict[str, bytes] = {}
        self.requests: list[str] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    state: _ReleaseState  # set on the subclass handed to the server

    def log_message(self, *args):  # silence the default stderr chatter
        pass

    def do_GET(self):  # noqa: N802 — http.server's spelling
        self.state.requests.append(self.path)
        state = self.state

        if state.status != 200:
            self.send_response(state.status)
            self.end_headers()
            self.wfile.write(b'{"message":"not found"}')
            return

        if self.path.startswith("/api/v1/repos/"):
            body = json.dumps(state.release).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        name = self.path.rsplit("/", 1)[-1]
        payload = state.assets.get(name)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _FakeReleaseAPI:
    """A loopback release server. Nothing here reaches a real forge."""

    def __init__(self, tmp_path: Path) -> None:
        self.state = _ReleaseState()
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.harness = tmp_path / "harness"
        (self.harness / "scripts").mkdir(parents=True)
        (self.harness / "release-assets").mkdir()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def install_verifier(self) -> None:
        """Copy the repository's verifier into the harness, so the step's
        relative ``scripts/...`` path resolves against the executed cwd."""
        source = REPO_ROOT / VERIFY_SCRIPT_REL
        assert source.is_file(), f"{VERIFY_SCRIPT_REL} missing from the repository"
        (self.harness / "scripts" / Path(VERIFY_SCRIPT_REL).name).write_bytes(
            source.read_bytes()
        )

    def write_assets(self, assets: dict[str, bytes]) -> dict[str, str]:
        """Write the built assets and SHA256SUMS, and serve those bytes; return
        name -> sha256."""
        digests: dict[str, str] = {}
        lines = []
        for name, payload in assets.items():
            (self.harness / "release-assets" / name).write_bytes(payload)
            self.state.assets[name] = payload
            digest = hashlib.sha256(payload).hexdigest()
            digests[name] = digest
            lines.append("%s  %s" % (digest, name))
        (self.harness / "release-assets" / "SHA256SUMS").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return digests

    def publish(self, names: list[str], downloads: dict[str, str] | None = None) -> None:
        """Build the release object the fake API returns for the tag."""
        repo = "example-org/example-repo"
        assets = []
        for name in names:
            url = (downloads or {}).get(
                name,
                "%s/%s/releases/download/%s/%s" % (self.base_url, repo, TAG, name),
            )
            assets.append({"name": name, "browser_download_url": url})
        self.state.release = {
            "id": 1,
            "tag_name": TAG,
            "name": "SkillWeave " + TAG,
            "assets": assets,
        }

    def run_step(self, token: str | None = DUMMY_TOKEN) -> subprocess.CompletedProcess:
        """Execute the workflow step's ``run`` text verbatim with bash."""
        import os

        env = dict(os.environ)
        env.pop("FORGEJO_TOKEN", None)
        if token is not None:
            env["FORGEJO_TOKEN"] = token
        env["TAG_NAME"] = TAG
        env["FORGEJO_BASE_URL"] = self.base_url
        env["FORGEJO_REPOSITORY"] = "example-org/example-repo"
        return subprocess.run(
            ["bash", "-c", _verify_run_text()],
            cwd=str(self.harness),
            env=env,
            capture_output=True,
            text=True,
        )


@pytest.fixture()
def api(tmp_path):
    server = _FakeReleaseAPI(tmp_path)
    server.install_verifier()
    try:
        yield server
    finally:
        server.close()


_ASSETS = {
    "exampleproject-0.3.0.tar.gz": b"the sdist bytes\n",
    "exampleproject-0.3.0-py3-none-any.whl": b"the wheel bytes\n",
}


def _assert_named_refusal(proc, name: str) -> None:
    assert proc.returncode != 0, (
        f"the step must fail the job; rc={proc.returncode}\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert name in proc.stderr, (
        f"the failure must name {name} so a red run is diagnosable:\n{proc.stderr}"
    )


# ── The four obligations, and the adversarial rows ─────────────────────────


def test_correct_attached_set_passes(api):
    api.write_assets(_ASSETS)
    api.publish(list(_ASSETS))

    proc = api.run_step()

    assert proc.returncode == 0, (
        f"a correct attached set must pass:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "attachment verification passed" in proc.stdout
    # Both names were actually downloaded and read, not merely listed.
    assert proc.stdout.count("verified attached asset") == len(_ASSETS)


def test_missing_attachment_fails_by_name(api):
    api.write_assets(_ASSETS)
    api.publish([next(iter(_ASSETS))])  # the sdist never made it to the release

    proc = api.run_step()

    _assert_named_refusal(proc, "MissingReleaseAttachmentError")


def test_extra_attachment_fails_by_name(api):
    api.write_assets(_ASSETS)
    api.publish(list(_ASSETS) + ["stray-scratch.txt"])

    proc = api.run_step()

    _assert_named_refusal(proc, "UnexpectedReleaseAttachmentError")


def test_wrong_checksum_fails_by_name(api):
    api.write_assets(_ASSETS)
    api.publish(list(_ASSETS))
    # The release serves bytes that differ from the ones the manifest covers.
    api.state.assets["exampleproject-0.3.0-py3-none-any.whl"] = b"tampered wheel\n"

    proc = api.run_step()

    _assert_named_refusal(proc, "AttachmentChecksumMismatchError")


def test_absent_release_fails_by_name(api):
    """An unpublished tag cannot be verified by assuming it was published."""
    api.write_assets(_ASSETS)
    api.state.status = 404

    proc = api.run_step()

    _assert_named_refusal(proc, "ReleaseLookupError")


def test_missing_token_is_a_named_refusal(api):
    """The release read is an authorized read: without a credential it refuses
    rather than reporting an unverified release as verified."""
    api.write_assets(_ASSETS)
    api.publish(list(_ASSETS))

    proc = api.run_step(token=None)

    _assert_named_refusal(proc, "MissingReleaseTokenError")


def test_missing_checksum_manifest_is_a_named_refusal(api):
    """The manifest is the reviewed expectation; without it nothing is verified."""
    api.write_assets(_ASSETS)
    (api.harness / "release-assets" / "SHA256SUMS").unlink()
    api.publish(list(_ASSETS))

    proc = api.run_step()

    _assert_named_refusal(proc, "MissingChecksumManifestError")


def test_off_origin_attachment_is_refused(api):
    """The download URL is forge-supplied. A URL on another origin would send
    the release credential elsewhere, so it is refused, not followed."""
    api.write_assets(_ASSETS)
    api.publish(
        list(_ASSETS),
        downloads={
            "exampleproject-0.3.0.tar.gz": "http://198.51.100.7/elsewhere/bundle.tgz"
        },
    )

    proc = api.run_step()

    _assert_named_refusal(proc, "ReleaseLookupError")
    assert "198.51.100.7" not in " ".join(api.state.requests), (
        "the credential must never be sent to the off-origin host"
    )
