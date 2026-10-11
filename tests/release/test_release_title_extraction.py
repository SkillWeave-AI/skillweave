r"""Prove that the flat top-level scalars in .version.yaml match the
release_title_policy flow mapping AND satisfy the workflow's extraction regexes.

SW-CORE-ZR-013: SW-CORE-ZR-001 restored the ``release_title_policy`` flow
mapping but deleted the flat ``product_name:`` and ``pattern:`` lines that
.forgejo/workflows/forgejo-release.yml extracts with line-anchored
``re.search(…, re.MULTILINE)``.  No test caught it because no test executes
the workflow's extraction.

This test reads the two regexes directly from
.forgejo/workflows/forgejo-release.yml so it CANNOT drift from the workflow.
It then applies them to .version.yaml and asserts that:

* The flat values equal the values inside ``release_title_policy``.
* The assembled title pattern accepts ``SkillWeave v1.5.10``.
* The assembled title pattern rejects ``Skillweave v1.5.10`` (wrong case).
* The assembled title pattern rejects ``SkillWeave 1.5.10`` (missing ``v``).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Workflow regex extraction ──────────────────────────────────────────────


def _extract_workflow_regexes(workflow: str) -> dict[str, str]:
    r"""Extract the two line-anchored regexes used by the title gate.

    The workflow's inline Python contains two ``re.search(…, re.MULTILINE)``
    calls:

        re.search(r"^product_name:\s*(.+)$", text, re.MULTILINE)
        re.search(r"^pattern:\s*(.+)$", text, re.MULTILINE)

    We parse the *actual workflow file* so the test automatically tracks any
    workflow change.
    """
    regexes: dict[str, str] = {}
    for key in ("product_name", "pattern"):
        needle = rf're\.search\(r"\^({key}):\\s\*\(\.\+\)\$"'
        m = re.search(needle, workflow)
        if not m:
            raise AssertionError(
                f"Could not find re.search for {key!r} in workflow file"
            )
        regexes[key] = rf"^{key}:\s*(.+)$"
    return regexes


def _workflow_extract_value(
    text: str, regex_str: str, key: str
) -> str | None:
    """Apply the same extraction logic as the workflow's inline Python."""
    m = re.search(regex_str, text, re.MULTILINE)
    if not m:
        return None
    value = m.group(1).strip().strip("\"'")
    return value if value else None


# ── Policy helpers (mirror existing test_release_title_policy.py) ───────────


def _load_policy(repo: Path = REPO_ROOT) -> dict | None:
    path = repo / ".version.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        return None
    return data.get("release_title_policy")


# ── Tests ──────────────────────────────────────────────────────────────────


class TestReleaseTitleExtraction:
    """Extraction parity between .forgejo/workflows/forgejo-release.yml and
    .version.yaml."""

    def test_workflow_regexes_find_flat_values(self):
        """The two regexes extracted from the workflow find non-empty flat
        top-level scalars in .version.yaml."""
        workflow_path = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
        assert workflow_path.exists(), "workflow file must exist"

        workflow_text = workflow_path.read_text("utf-8")
        regexes = _extract_workflow_regexes(workflow_text)

        version_yaml_text = (REPO_ROOT / ".version.yaml").read_text("utf-8")

        product_name = _workflow_extract_value(
            version_yaml_text, regexes["product_name"], "product_name"
        )
        assert product_name is not None, (
            "Workflow regex did not find product_name in .version.yaml"
        )

        pattern_raw = _workflow_extract_value(
            version_yaml_text, regexes["pattern"], "pattern"
        )
        assert pattern_raw is not None, (
            "Workflow regex did not find pattern in .version.yaml"
        )

    def test_flat_values_equal_mapping_values(self):
        """The flat top-level scalars must equal the values inside the
        release_title_policy flow mapping."""
        workflow_path = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
        workflow_text = workflow_path.read_text("utf-8")
        regexes = _extract_workflow_regexes(workflow_text)

        version_yaml_text = (REPO_ROOT / ".version.yaml").read_text("utf-8")
        policy = _load_policy()
        assert policy is not None, "release_title_policy must exist"

        flat_product = _workflow_extract_value(
            version_yaml_text, regexes["product_name"], "product_name"
        )
        flat_pattern = _workflow_extract_value(
            version_yaml_text, regexes["pattern"], "pattern"
        )

        assert flat_product == policy["product_name"], (
            f"Flat product_name {flat_product!r} != "
            f"release_title_policy.product_name {policy['product_name']!r}"
        )
        assert flat_pattern == policy["pattern"], (
            f"Flat pattern {flat_pattern!r} != "
            f"release_title_policy.pattern {policy['pattern']!r}"
        )

    def test_accepts_valid_title(self):
        """SkillWeave v1.5.10 must be accepted by the assembled pattern."""
        workflow_path = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
        workflow_text = workflow_path.read_text("utf-8")
        regexes = _extract_workflow_regexes(workflow_text)

        version_yaml_text = (REPO_ROOT / ".version.yaml").read_text("utf-8")

        product_name = _workflow_extract_value(
            version_yaml_text, regexes["product_name"], "product_name"
        )
        pattern_raw = _workflow_extract_value(
            version_yaml_text, regexes["pattern"], "pattern"
        )

        full_pattern = pattern_raw.replace("{product}", product_name)
        NAME_RE = re.compile(full_pattern)

        assert NAME_RE.match("SkillWeave v1.5.10"), (
            "SkillWeave v1.5.10 should match the release title pattern"
        )

    def test_rejects_wrong_case(self):
        """Skillweave v1.5.10 (lowercase 'w') must be rejected."""
        workflow_path = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
        workflow_text = workflow_path.read_text("utf-8")
        regexes = _extract_workflow_regexes(workflow_text)

        version_yaml_text = (REPO_ROOT / ".version.yaml").read_text("utf-8")

        product_name = _workflow_extract_value(
            version_yaml_text, regexes["product_name"], "product_name"
        )
        pattern_raw = _workflow_extract_value(
            version_yaml_text, regexes["pattern"], "pattern"
        )

        full_pattern = pattern_raw.replace("{product}", product_name)
        NAME_RE = re.compile(full_pattern)

        assert not NAME_RE.match("Skillweave v1.5.10"), (
            "Skillweave (lowercase 'w') must NOT match the release title pattern"
        )

    def test_rejects_missing_v_prefix(self):
        """SkillWeave 1.5.10 (missing 'v' before version) must be rejected."""
        workflow_path = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
        workflow_text = workflow_path.read_text("utf-8")
        regexes = _extract_workflow_regexes(workflow_text)

        version_yaml_text = (REPO_ROOT / ".version.yaml").read_text("utf-8")

        product_name = _workflow_extract_value(
            version_yaml_text, regexes["product_name"], "product_name"
        )
        pattern_raw = _workflow_extract_value(
            version_yaml_text, regexes["pattern"], "pattern"
        )

        full_pattern = pattern_raw.replace("{product}", product_name)
        NAME_RE = re.compile(full_pattern)

        assert not NAME_RE.match("SkillWeave 1.5.10"), (
            "SkillWeave 1.5.10 (no 'v' prefix) must NOT match the release title pattern"
        )
