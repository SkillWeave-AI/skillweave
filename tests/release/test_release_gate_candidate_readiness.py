"""SW-1511A-GATE-001: the release gate green on the real 1.5.11 candidate.

This is the *production-caller* proof: it runs ``ReleaseReadinessGate`` -- the
same entry point the release workflow, the verdict producer and ``run_cli`` use
-- on the *real* repository root, and asserts the candidate has no failing
*required* check (``can_release``). It fails on the pre-repair head, where the
gate reported three required failures:

* ``changelog-updated`` -- the check's regex did not accept the project's real
  ``## vX.Y.Z`` heading and fell through to "No version entries found";
* ``release-version-capabilities-match`` -- the check forced every member
  ``skills/*/capability.yaml`` to equal the release version, contradicting the
  ``decoupled_member_pins`` release policy the tree declares; and
* ``capacium-manifests`` -- the sync tool shipped ``LangeVC`` identity defaults
  that fought the migrated ``SkillWeave-AI`` tree.

Each failing check is paired with a negative control that mutates an ephemeral
copy of the tree and shows the *same* check still fails on a genuine defect --
so a green result here is not vacuous.

Hermetic: reads the real tree, writes only to a pytest ``tmp_path`` copy. The
version is derived from ``pyproject.toml``, never a literal.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from skillweave.github_integration.release_gate import ReleaseReadinessGate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_VERSION_RE = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', re.MULTILINE)


def _candidate_version() -> str:
    match = _VERSION_RE.search((REPO_ROOT / "pyproject.toml").read_text())
    assert match, "pyproject.toml has no version"
    return match.group(1)


def _copy_tree(dest: Path) -> Path:
    """Copy the version-bearing surfaces into a scratch root.

    A full copy is unnecessary and slow; the gate reads only pyproject.toml,
    CHANGELOG.md, capability.yaml, skills/*/capability.yaml, README.md, LICENSE
    and tests/. Copying exactly those keeps the negative controls hermetic.
    """
    root = dest / "repo"
    root.mkdir(parents=True)
    for name in ("pyproject.toml", "CHANGELOG.md", "capability.yaml", "README.md", "LICENSE"):
        shutil.copy2(REPO_ROOT / name, root / name)
    shutil.copytree(REPO_ROOT / "skills", root / "skills")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (root / "src").mkdir()
    return root


def test_real_candidate_has_no_failing_required_check():
    gate = ReleaseReadinessGate(repo_root=str(REPO_ROOT))
    result = gate.evaluate(current_version=_candidate_version())
    failed_required = [c.id for c in result.required_failed]
    assert failed_required == [], (
        f"release gate reports failing required checks on the real candidate: "
        f"{failed_required} -> can_release={result.can_release}"
    )
    assert result.can_release is True


def test_real_candidate_required_checks_named_green():
    """The three formerly-failing checks are present *and* passing."""
    gate = ReleaseReadinessGate(repo_root=str(REPO_ROOT))
    result = gate.evaluate(current_version=_candidate_version())
    by_id = {c.id: c for c in result.checks}
    for check_id in (
        "changelog-updated",
        "release-version-capabilities-match",
        "capacium-manifests",
    ):
        assert check_id in by_id, f"required check {check_id!r} missing from the gate"
        assert by_id[check_id].required is True
        assert by_id[check_id].passed is True, by_id[check_id].detail


# ─── negative controls: the checks are not vacuously green ──────────────────


def test_negative_control_changelog_missing_entry_fails(tmp_path):
    root = _copy_tree(tmp_path)
    # Remove every version heading: the check must go red on a real defect.
    (root / "CHANGELOG.md").write_text("# Changelog\n\nNo entries yet.\n")
    gate = ReleaseReadinessGate(repo_root=str(root))
    check = gate.check_changelog(_candidate_version())
    assert check.passed is False


def test_negative_control_member_pin_mismatch_fails(tmp_path):
    root = _copy_tree(tmp_path)
    member = root / "skills" / "skillweave-blueprint" / "capability.yaml"
    version = _candidate_version()
    assert "version: 1.3" in member.read_text(), "fixture expects a lagging member version"
    member.write_text(member.read_text().replace("version: 1.3", "version: 9.9", 1))
    gate = ReleaseReadinessGate(repo_root=str(root))
    check = gate.check_release_version_matches_capabilities(version)
    assert check.passed is False
    assert "skillweave-blueprint/capability.yaml" in check.detail


def test_negative_control_manifest_drift_fails(tmp_path):
    root = _copy_tree(tmp_path)
    bundle = root / "capability.yaml"
    # Drift the owner back to the retired identity: the sync tool must flag it.
    bundle.write_text(bundle.read_text().replace("owner: SkillWeave-AI", "owner: LangeVC", 1))
    gate = ReleaseReadinessGate(repo_root=str(root))
    check = gate.check_capacium_manifests()
    assert check.passed is False
    assert "out of sync" in check.detail


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
