"""Multi-repository baseline contract (SW-1591-BASE-001).

This module pins the released multi-repository baseline for the SkillWeave
1.5.9.1 contract-authority patch and proves the pinned refs have not moved.
It is the executable half of ``.skillweave/releases/1.5.9.1/baseline.json``:
the JSON is the content-addressed record, this test is the guard that keeps
that record honest against the live repositories.

Scope of proof:

* every pinned subject (SkillWeave main/dev, SDK main/tag, the local SDK
  preview, the two fix diff inputs) is still reachable at exactly the SHA the
  baseline recorded, so nothing upstream moved under the lane;
* the preserved local SDK preview is recorded as protected evidence and is
  confirmed to still be an unpublished local branch (not an ancestor of SDK
  main) — protecting it means *not* consuming it as if it were published;
* the immutability contract on the historical Gate-1312 manifest holds.

Hermetic against local state, tolerant of shared state:

* The SHA-set checks read local refs only (no network, no fetch) so the suite
  is deterministic in an offline sandbox.
* Where the verifying clone simply does not have a remote configured, the
  remote comparison *skips*. It must never fail a green tree merely because a
  sandbox lacks an origin — that would be a false red. When a remote exists,
  a moved ref fails hard.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_PATH = REPO_ROOT / ".skillweave" / "releases" / "1.5.9.1" / "baseline.json"

# Default location of the SDK checkout. Overridable so the guard can run in
# CI or a relocated workspace without editing the test.
DEFAULT_SDK_REPO = Path(
    "/Users/andrelange/Documents/repositories/forgejo/skillweave/skillweave-sdk"
)


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def _rev_parse(repo: Path, ref: str) -> str | None:
    result = _git(repo, "rev-parse", f"{ref}^{{commit}}")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _sdk_repo() -> Path:
    import os

    return Path(os.environ.get("SKILLWEAVE_SDK_REPO", str(DEFAULT_SDK_REPO)))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_PINS = {
    # baseline key -> (local ref name to check against this working repo)
    "skillweave_main": "HEAD",
    "skillweave_dev": "origin/dev",
    "council_fix_input": "fix/council-attributed-response-copy",
    "workflow_fix_input": "fix/contract-ci-invalid-yaml",
}


def test_baseline_file_exists_and_is_valid_json() -> None:
    assert BASELINE_PATH.is_file(), (
        f"{BASELINE_PATH} is missing — the 1.5.9.1 baseline was never recorded"
    )
    baseline = _load_baseline()
    assert baseline["release"] == "1.5.9.1"
    assert baseline["task"] == "SW-1591-BASE-001"
    assert baseline["schema_version"] == 1


def test_pins_present_and_well_formed() -> None:
    """Every subject named in the PRD grounded baseline is pinned to a full SHA."""
    baseline = _load_baseline()
    required = [
        "skillweave_main",
        "skillweave_dev",
        "council_fix_input",
        "workflow_fix_input",
        "sdk_main",
        "sdk_tag_v0_1_0",
        "sdk_local_preview",
    ]
    pins = baseline["pins"]
    for key in required:
        assert key in pins, f"baseline is missing pin {key!r}"
        sha = pins[key]["sha"]
        assert len(sha) == 40, f"pin {key!r} is not a full 40-char SHA: {sha!r}"
        int(sha, 16)  # must be valid hex


def test_core_pins_match_live_refs() -> None:
    """The pinned Core refs still resolve to the recorded SHAs."""
    baseline = _load_baseline()
    for key, ref in _PINS.items():
        expected = baseline["pins"][key]["sha"]
        actual = _rev_parse(REPO_ROOT, ref)
        if actual is None:
            pytest.skip(f"{ref} not available in this clone; cannot verify {key}")
        assert actual == expected, (
            f"ref {ref} moved: baseline pinned {expected}, live is {actual}. "
            f"The released baseline is no longer trustworthy."
        )


def test_skillweave_main_pin_is_the_worktree_ancestor() -> None:
    """The lane's own HEAD must descend from the pinned released main.

    A candidate built on a branch that forked before the release commit would
    silently lose the 1.5.9 baseline.
    """
    baseline = _load_baseline()
    main_sha = baseline["pins"]["skillweave_main"]["sha"]
    result = _git(REPO_ROOT, "merge-base", "--is-ancestor", main_sha, "HEAD")
    if result.returncode not in (0, 1):
        pytest.skip("cannot compute ancestry in this clone")
    assert result.returncode == 0, (
        f"HEAD does not descend from released main {main_sha}; the lane forked "
        f"before the 1.5.9 release commit."
    )


def test_sdk_pins_match_live_refs_when_sdk_present() -> None:
    """SDK main / tag / preview pins still resolve — when the SDK is reachable."""
    baseline = _load_baseline()
    sdk = _sdk_repo()
    if not (sdk / ".git").exists():
        pytest.skip(f"SDK checkout not present at {sdk}")

    checks = {
        "sdk_main": "origin/main",
        "sdk_tag_v0_1_0": "refs/tags/v0.1.0",
        "sdk_local_preview": "ops/SW1312-PROFILE-CONTRACT-001",
    }
    for key, ref in checks.items():
        expected = baseline["pins"][key]["sha"]
        actual = _rev_parse(sdk, ref)
        if actual is None:
            pytest.skip(f"{ref} not available in SDK clone; cannot verify {key}")
        assert actual == expected, (
            f"SDK ref {ref} moved: baseline pinned {expected}, live is {actual}"
        )


def test_local_sdk_preview_is_protected_evidence() -> None:
    """The preview is a preserved local branch, not an ancestor of SDK main.

    The PRD is explicit: the local GLE preview remains protected *source
    evidence* and is not the published contract authority. If it ever became
    an ancestor of SDK main it would silently have been promoted, which is the
    exact failure this baseline exists to detect.
    """
    sdk = _sdk_repo()
    if not (sdk / ".git").exists():
        pytest.skip(f"SDK checkout not present at {sdk}")

    baseline = _load_baseline()
    preview = baseline["protected_local_evidence"]["sdk_local_preview"]
    preview_sha = preview["sha"]
    assert preview["ancestor_of_sdk_main"] is False

    # A pinned preview branch must resolve to the recorded SHA (present, not modified).
    live = _rev_parse(sdk, preview["branch"])
    if live is None:
        pytest.skip(f"preview branch {preview['branch']} not present in SDK clone")
    assert live == preview_sha, (
        f"protected preview branch {preview['branch']} moved: "
        f"recorded {preview_sha}, live {live}"
    )

    # It must NOT be reachable from SDK main.
    sdk_main = baseline["pins"]["sdk_main"]["sha"]
    result = _git(sdk, "merge-base", "--is-ancestor", preview_sha, sdk_main)
    if result.returncode not in (0, 1):
        pytest.skip("cannot compute SDK ancestry in this clone")
    assert result.returncode == 1, (
        f"local preview {preview_sha} is now an ancestor of SDK main {sdk_main}; "
        f"it was promoted without a publication decision."
    )


def test_gate_1312_manifest_is_declared_immutable() -> None:
    """The historical Gate-1312 manifest is recorded as must-not-touch."""
    baseline = _load_baseline()
    manifest = baseline["protected_local_evidence"]["gate_1312_manifest"]
    assert manifest["path"] == "tests/gate_1312/gate-1312-manifest.json"
    assert "immutable" in manifest["modification_policy"]

    on_disk = REPO_ROOT / manifest["path"]
    assert on_disk.is_file(), "the Gate-1312 manifest must exist"
    recorded = json.loads(on_disk.read_text(encoding="utf-8"))
    assert recorded.get("schema_version") == manifest["schema_version"]
    for pkg, sha in manifest["shas"].items():
        assert recorded["shas"][pkg] == sha, (
            f"Gate-1312 manifest pin {pkg} changed from {sha} to "
            f"{recorded['shas'][pkg]} — the historical manifest was mutated."
        )


def test_watched_artifact_digests_unchanged() -> None:
    """Bytes the baseline pinned have not drifted without a re-pin.

    ``contract-ci.yml`` is the *known-red* artifact: this baseline exists
    precisely because it is broken. The digest guard here does not assert it is
    valid — it asserts it has not been silently rewritten. SW-1591-CI-001 is
    expected to repair it and re-pin this entry deliberately.
    """
    baseline = _load_baseline()
    watched = baseline["verification_contract"]["watched_digests"]
    for rel, expected in watched.items():
        path = REPO_ROOT / rel
        assert path.is_file(), f"watched artifact {rel} is missing"
        actual = _sha256(path)
        assert actual == expected, (
            f"watched artifact {rel} changed without re-pinning the baseline: "
            f"expected {expected}, got {actual}. If this change is intentional, "
            f"update verification_contract.watched_digests in baseline.json."
        )


def test_red_conditions_are_recorded_with_digests() -> None:
    """Each reproduced red condition carries a command and a SHA-256 digest.

    This does not re-run the red probes (other lanes own them); it proves the
    baseline *recorded* them reproducibly instead of asserting them in prose.
    """
    baseline = _load_baseline()
    conditions = baseline["red_conditions"]
    assert len(conditions) >= 5, "expected at least five recorded red conditions"
    for cond in conditions:
        assert cond.get("command") or cond.get("commands"), (
            f"red condition {cond['id']} has no command recorded"
        )
        assert re_full_sha_or_digest(cond["output_sha256"]), (
            f"red condition {cond['id']} has a malformed output_sha256"
        )
        # The evidence transcript must actually exist and hash to the recorded digest.
        ev = REPO_ROOT / cond["evidence_file"]
        assert ev.is_file(), f"missing evidence file for {cond['id']}: {ev}"
        if "actionlint_output_sha256" not in cond:
            assert _sha256(ev) == cond["output_sha256"]


def re_full_sha_or_digest(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def test_documented_reproduction_commit_is_released_main() -> None:
    """The baseline states which commit the reds reproduce on, and it is main."""
    baseline = _load_baseline()
    assert baseline["reproduced_on"]["release_ref"] == "refs/heads/main"
    assert (
        baseline["reproduced_on"]["release_commit"]
        == baseline["pins"]["skillweave_main"]["sha"]
    )
