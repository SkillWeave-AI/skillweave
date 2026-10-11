"""Contract CI installs the pinned SDK before it runs pytest (SW-CORE-ZR-017).

The cross-repo contract suite imports ``skillweave_sdk`` at module level, so the
``contract-ci`` job must install the SDK it checked out before any step invokes
pytest; otherwise pytest aborts the whole run with a collection error
(``ModuleNotFoundError: No module named 'skillweave_sdk'``).  The real failure
was measured on Forgejo Actions ``contract-ci.yml`` #492 (release branch,
0e17ae6) and #488 (main, 11a6fe7) in the job ``contract-ci``, step
``Run cross-repo contract CI tests``.

This guard parses the workflow YAML and asserts, for the job ``contract-ci``,
that the SDK install step exists and precedes every pytest-invoking step.  Two
negative controls mutate copies (the install step removed; the install step
moved after every pytest step) so the assertion is shown to have teeth.

Hermetic: reads the workflow text only, no network, no mutation of the file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "contract-ci.yml"
JOB = "contract-ci"
SDK_CHECKOUT = "/tmp/skillweave-sdk"
INSTALL_STEP_NAME = "Install the pinned SDK checked out from the published source"

#: A *pytest invocation* is the ``pytest`` module run via ``python -m`` or a
#: bare ``pytest`` command at the start of a shell line.  A step that merely
#: mentions pytest — a ``pip install pytest`` argument or a comment — is not an
#: invocation and must not be treated as one.
_PYTEST_COMMAND = re.compile(r"^(?:python3?(?:\.\d+)?\s+-m\s+)?pytest(?:\s|$)")


def _load_workflow(text: str) -> dict:
    data = yaml.safe_load(text)
    assert isinstance(data, dict), "workflow did not parse to a mapping"
    return data


def _job_steps(workflow: dict, job: str = JOB) -> list[dict]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "workflow has no jobs mapping"
    assert job in jobs, f"workflow has no {job!r} job"
    steps = jobs[job].get("steps")
    assert isinstance(steps, list) and steps, f"job {job!r} has no steps"
    return steps


def _run_invokes_pytest(run: str) -> bool:
    for raw in run.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and _PYTEST_COMMAND.match(line):
            return True
    return False


def _install_step_indices(steps: list[dict]) -> list[int]:
    out = []
    for i, step in enumerate(steps):
        run = step.get("run") or ""
        if "pip install" in run and SDK_CHECKOUT in run:
            out.append(i)
    return out


def _assert_install_precedes_pytest(text: str) -> None:
    """Shared assertion core, also used to prove the controls are not vacuous."""
    steps = _job_steps(_load_workflow(text))

    install_indices = _install_step_indices(steps)
    assert install_indices, (
        f"no step in job {JOB!r} installs the checked-out SDK from "
        f"{SDK_CHECKOUT!r}; the contract tests would fail collection with "
        f"ModuleNotFoundError"
    )
    install_at = install_indices[0]

    pytest_indices = [i for i, s in enumerate(steps) if _run_invokes_pytest(s.get("run") or "")]
    assert pytest_indices, (
        f"job {JOB!r} no longer invokes pytest — the guard would be vacuous"
    )
    for i in pytest_indices:
        assert install_at < i, (
            f"pytest step {steps[i].get('name')!r} at index {i} runs before the "
            f"SDK install step at index {install_at}; the install must come first"
        )


def _remove_install_step(text: str) -> str:
    """Return a copy of the workflow text with the install step deleted."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == f"- name: {INSTALL_STEP_NAME}":
            skipping = True
            continue
        if skipping:
            if re.match(r"^ {6}- ", line) or re.match(r"^ {0,5}\S", line):
                skipping = False
            else:
                continue
        kept.append(line)
    return "".join(kept)


def test_workflow_parses_and_job_exists() -> None:
    assert WORKFLOW.is_file(), f"contract CI workflow missing: {WORKFLOW}"
    _job_steps(_load_workflow(WORKFLOW.read_text(encoding="utf-8")))


def test_sdk_install_step_exists_and_installs_pinned_checkout() -> None:
    steps = _job_steps(_load_workflow(WORKFLOW.read_text(encoding="utf-8")))
    installs = _install_step_indices(steps)
    assert installs, (
        f"no step installs {SDK_CHECKOUT}; the pinned SDK is checked out but "
        f"never installed, which is the exact CI failure this guard repairs"
    )
    run = steps[installs[0]]["run"]
    assert "--no-build-isolation" in run and "--no-deps" in run, (
        "the SDK install must build with the provided setuptools/wheel "
        "(--no-build-isolation) and keep the combination pinned (--no-deps)"
    )


def test_sdk_install_precedes_every_pytest_step() -> None:
    _assert_install_precedes_pytest(WORKFLOW.read_text(encoding="utf-8"))


def test_negative_control_without_install_step_fails() -> None:
    """Removing the install step from a copy must make the assertion fail."""
    mutated = _remove_install_step(WORKFLOW.read_text(encoding="utf-8"))
    assert INSTALL_STEP_NAME not in mutated, (
        "the negative control did not actually remove the install step"
    )
    with pytest.raises(AssertionError):
        _assert_install_precedes_pytest(mutated)


def test_negative_control_install_after_pytest_fails() -> None:
    """Moving the install step after every pytest step must fail the assertion."""
    data = _load_workflow(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"][JOB]["steps"]
    installs = _install_step_indices(steps)
    assert installs, "precondition: the install step must exist to be moved"
    steps.append(steps.pop(installs[0]))  # now after every pytest step
    mutated = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    with pytest.raises(AssertionError):
        _assert_install_precedes_pytest(mutated)
