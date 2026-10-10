"""SW-1510-PUBLISH-MANIFEST: Capacium publish-manifest contract.

Asserts the target state for the Capacium Exchange Trusted Publishing
contract. Every capability.yaml (root bundle + 15 skills) must carry:

* owner: SkillWeave-AI
* repository: https://github.com/SkillWeave-AI/skillweave
* homepage: https://github.com/SkillWeave-AI/skillweave
* No occurrence of ``LangeVC`` or ``git.langevc.com`` in those three fields

The root manifest also pins ``replaces`` and ``previous_identities`` as YAML
lists (``["LangeVC/skillweave"]``).

The GitHub Actions workflow ``.github/workflows/release-on-tag.yml`` must
pin ``Capacium/capacium-action-publish`` to the full commit SHA
``7a099697fd3c668bf8737564375b983dc662e118`` (trailing comment ``# v1``),
set ``use_oidc: true``, carry ``id-token: write`` under permissions, and
carry no ``api_token`` key.

This test is written for the *target* state.  On the unchanged base (where
manifests still reference LangeVC / git.langevc.com) every assertion fails.
"""

import os
import re
import sys
from pathlib import Path

import yaml

import pytest

# -- helpers -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SKILL_DIRS = sorted(
    d.name for d in (REPO_ROOT / "skills").iterdir()
    if d.is_dir() and (d / "capability.yaml").is_file()
)

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-on-tag.yml"

EXPECTED_OWNER = "SkillWeave-AI"
EXPECTED_REPOSITORY = "https://github.com/SkillWeave-AI/skillweave"
EXPECTED_HOMEPAGE = "https://github.com/SkillWeave-AI/skillweave"
EXPECTED_SHA = "7a099697fd3c668bf8737564375b983dc662e118"

FORBIDDEN_STRINGS = ("LangeVC", "git.langevc.com")

# -- helpers -----------------------------------------------------------------


def _load_yaml(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _failures(assertions):
    """Collect multiple assertion errors into a single ``AssertionError``.

    Each element of *assertions* is a ``(label, callable)`` pair.  Every
    callable is tried; failures are collected into one message.  If none
    fail the function returns silently.
    """
    errors = []
    for label, fn in assertions:
        try:
            fn()
        except AssertionError as exc:
            errors.append(f"  FAIL {label}: {exc}")
    if errors:
        msg = "Manifest contract violations:\n" + "\n".join(errors)
        raise AssertionError(msg)


# -- root manifest -----------------------------------------------------------


def test_root_manifest_owner():
    doc = _load_yaml(REPO_ROOT / "capability.yaml")
    assert doc.get("owner") == EXPECTED_OWNER, (
        f"root owner expected {EXPECTED_OWNER!r}, got {doc.get('owner')!r}"
    )


def test_root_manifest_repository():
    doc = _load_yaml(REPO_ROOT / "capability.yaml")
    assert doc.get("repository") == EXPECTED_REPOSITORY, (
        f"root repository expected {EXPECTED_REPOSITORY!r}, "
        f"got {doc.get('repository')!r}"
    )


def test_root_manifest_homepage():
    doc = _load_yaml(REPO_ROOT / "capability.yaml")
    assert doc.get("homepage") == EXPECTED_HOMEPAGE, (
        f"root homepage expected {EXPECTED_HOMEPAGE!r}, "
        f"got {doc.get('homepage')!r}"
    )


def test_root_manifest_pins_replaces():
    """The root manifest must declare ``replaces`` as a YAML list ``["LangeVC/skillweave"]``."""
    doc = _load_yaml(REPO_ROOT / "capability.yaml")
    val = doc.get("replaces")
    assert isinstance(val, list), (
        f"root 'replaces' must be a list, got {type(val).__name__}: {val!r}"
    )
    assert val == ["LangeVC/skillweave"], (
        f"root 'replaces' expected ['LangeVC/skillweave'], got {val!r}"
    )


def test_root_manifest_pins_previous_identities():
    """The root manifest must declare ``previous_identities`` as a YAML list ``["LangeVC/skillweave"]``."""
    doc = _load_yaml(REPO_ROOT / "capability.yaml")
    val = doc.get("previous_identities")
    assert isinstance(val, list), (
        f"root 'previous_identities' must be a list, got {type(val).__name__}: {val!r}"
    )
    assert val == ["LangeVC/skillweave"], (
        f"root 'previous_identities' expected ['LangeVC/skillweave'], got {val!r}"
    )


def test_replaces_negative_control_rejects_scalar():
    """Verify the assertion helper would catch a scalar string in ``replaces``.

    A YAML ``replaces: LangeVC/skillweave`` (scalar) must be caught as
    non-list even though the key is present.
    """
    import yaml as _yaml

    bad = _yaml.safe_load("replaces: LangeVC/skillweave\n")
    assert not isinstance(bad.get("replaces"), list), (
        "negative control fixture is not a scalar"
    )
    with pytest.raises(AssertionError):
        assert isinstance(bad["replaces"], list), (
            f"scalar replaces must be rejected, got {bad['replaces']!r}"
        )


def test_previous_identities_negative_control_rejects_scalar():
    """Verify the assertion helper would catch a scalar string in ``previous_identities``."""
    import yaml as _yaml

    bad = _yaml.safe_load("previous_identities: LangeVC/skillweave\n")
    assert not isinstance(bad.get("previous_identities"), list), (
        "negative control fixture is not a scalar"
    )
    with pytest.raises(AssertionError):
        assert isinstance(bad["previous_identities"], list), (
            f"scalar previous_identities must be rejected, got {bad['previous_identities']!r}"
        )


def test_root_manifest_no_langevc():
    doc = _load_yaml(REPO_ROOT / "capability.yaml")
    for field in ("owner", "repository", "homepage"):
        val = doc.get(field, "")
        for forbidden in FORBIDDEN_STRINGS:
            assert forbidden not in str(val), (
                f"root {field} contains {forbidden!r}: {val!r}"
            )


# -- every skill manifest ----------------------------------------------------


def test_each_skill_manifest_owner():
    assertions = []
    for name in SKILL_DIRS:
        doc = _load_yaml(REPO_ROOT / "skills" / name / "capability.yaml")
        actual = doc.get("owner")
        assertions.append((
            f"skills/{name}/capability.yaml owner",
            lambda a=actual: (
                a == EXPECTED_OWNER or
                pytest.fail(
                    f"expected {EXPECTED_OWNER!r}, got {a!r}"
                )
            ),
        ))
    _failures(assertions)


def test_each_skill_manifest_repository():
    assertions = []
    for name in SKILL_DIRS:
        doc = _load_yaml(REPO_ROOT / "skills" / name / "capability.yaml")
        actual = doc.get("repository")
        assertions.append((
            f"skills/{name}/capability.yaml repository",
            lambda a=actual: (
                a == EXPECTED_REPOSITORY or
                pytest.fail(
                    f"expected {EXPECTED_REPOSITORY!r}, got {a!r}"
                )
            ),
        ))
    _failures(assertions)


def test_each_skill_manifest_homepage():
    assertions = []
    for name in SKILL_DIRS:
        doc = _load_yaml(REPO_ROOT / "skills" / name / "capability.yaml")
        actual = doc.get("homepage")
        assertions.append((
            f"skills/{name}/capability.yaml homepage",
            lambda a=actual: (
                a == EXPECTED_HOMEPAGE or
                pytest.fail(
                    f"expected {EXPECTED_HOMEPAGE!r}, got {a!r}"
                )
            ),
        ))
    _failures(assertions)


def test_each_skill_manifest_no_langevc():
    assertions = []
    for name in SKILL_DIRS:
        doc = _load_yaml(REPO_ROOT / "skills" / name / "capability.yaml")
        for field in ("owner", "repository", "homepage"):
            val = doc.get(field, "")
            for forbidden in FORBIDDEN_STRINGS:
                assertions.append((
                    f"skills/{name}/capability.yaml {field}",
                    lambda v=val, f=forbidden, n=name, fd=field: (
                        forbidden not in str(v) or
                        pytest.fail(
                            f"skills/{n}/capability.yaml {fd} "
                            f"contains {f!r}: {v!r}"
                        )
                    ),
                ))
    _failures(assertions)


# -- skill count smoke check -------------------------------------------------


def test_fifteen_skills():
    assert len(SKILL_DIRS) == 15, (
        f"expected 15 skill directories, found {len(SKILL_DIRS)}: {SKILL_DIRS}"
    )


# -- GitHub Actions workflow contract ----------------------------------------


def _workflow_text():
    return WORKFLOW_PATH.read_text()


def test_workflow_exists():
    assert WORKFLOW_PATH.is_file(), (
        f"workflow file not found: {WORKFLOW_PATH}"
    )


def test_workflow_pins_capacium_action_to_full_sha():
    text = _workflow_text()
    # The action line must look like:
    #   uses: Capacium/capacium-action-publish@7a099697fd3c668bf8737564375b983dc662e118  # v1
    pattern = (
        r"uses:\s+Capacium/capacium-action-publish@"
        + re.escape(EXPECTED_SHA)
    )
    assert re.search(pattern, text), (
        f"workflow does not pin Capacium/capacium-action-publish "
        f"to {EXPECTED_SHA}\n---\n{text}\n---"
    )


def test_workflow_has_v1_trailing_comment():
    text = _workflow_text()
    # The comment "# v1" must trail the SHA pin on the same line.
    assert re.search(
        rf"Capacium/capacium-action-publish@{re.escape(EXPECTED_SHA)}\s+#\s*v1\s*$",
        text,
        re.MULTILINE,
    ), (
        f"workflow pin line missing trailing '# v1' comment\n---\n{text}\n---"
    )


def test_workflow_use_oidc_true():
    text = _workflow_text()
    assert re.search(r"use_oidc:\s*true", text), (
        "workflow missing use_oidc: true"
    )


def test_workflow_permissions_id_token_write():
    text = _workflow_text()
    assert re.search(r"id-token:\s*write", text), (
        "workflow missing id-token: write under permissions"
    )


def test_workflow_no_api_token():
    text = _workflow_text()
    # The key "api_token" must not appear (commented or uncommented) in the
    # publish step.  We look for a line that has "api_token:" (even indented).
    assert not re.search(r"^\s+api_token:", text, re.MULTILINE), (
        "workflow contains api_token key (must use OIDC only)"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
