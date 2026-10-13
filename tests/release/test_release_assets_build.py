"""SW-CORE-ZR-026 — the release workflow builds the wheel, sdist and SHA256SUMS.

The Forgejo release workflow used to publish no built distribution: the
``release-assets`` directory was created empty and the engine attached nothing.
CORE-ZR-026 inserts exactly one step, "Build the release assets", immediately
before "Publish the release to every destination", so the published release
carries the wheel, the sdist and a ``SHA256SUMS`` a download can be verified
against with ``sha256sum -c``.

This test does not paraphrase that step. It reads the workflow YAML, extracts
the step's real ``run`` text verbatim and executes it with ``bash`` against a
hermetic synthetic project, so the test fails if the block's behaviour drifts
from what the workflow actually runs.

Structure checks are source-bound: the candidate is compared against the
canonical base commit the change was made from (``BASE_COMMIT``), proving the
diff is one inserted step and that every other step's ``run`` text is
byte-identical to the base. No skip, xfail or silent continue is added here
except one explicit, named skip for when the ``build`` package is unavailable
in the interpreter — stated on the marker below.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_REL = ".forgejo/workflows/forgejo-release.yml"
WORKFLOW_PATH = REPO_ROOT / WORKFLOW_REL
TEST_REL = "tests/release/test_release_assets_build.py"

# The canonical base this change was made from (the clean source checkout the
# task pinned). The structure test is bound to it, not to a moving ``HEAD``, so
# "exactly one inserted step" is a fact about this diff and stays falsifiable.
BASE_COMMIT = "4ff639e60041b2cb3000f56a7c70364706b90920"

BUILD_STEP_NAME = "Build the release assets"
PUBLISH_STEP_NAME = "Publish the release to every destination"

# The single permitted skip: the ``build`` package is unavailable, so the
# extracted block cannot be executed at all.
_BUILD_AVAILABLE = importlib.util.find_spec("build") is not None
_requires_build = pytest.mark.skipif(
    not _BUILD_AVAILABLE,
    reason=(
        "the `build` package is unavailable in this interpreter; the extracted "
        "release-asset block cannot be executed (explicit, named skip — the "
        "only one this suite adds)"
    ),
)


# ── Workflow / git access ──────────────────────────────────────────────────


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
    text = _git("show", f"{BASE_COMMIT}:{WORKFLOW_REL}")
    return yaml.safe_load(text)


def _steps(doc: dict) -> list[dict]:
    return doc["jobs"]["release"]["steps"]


def _build_step() -> dict:
    for step in _steps(_candidate_doc()):
        if step.get("name") == BUILD_STEP_NAME:
            return step
    raise AssertionError(f"workflow has no step named {BUILD_STEP_NAME!r}")


def _step_key(step: dict) -> str:
    return step.get("name") or step.get("uses")


def _run_text(step: dict) -> str:
    return step["run"]


# ── Structure, ordering, blast radius ──────────────────────────────────────


def test_workflow_yaml_parses():
    doc = _candidate_doc()
    assert isinstance(doc, dict)
    assert isinstance(_steps(doc), list)
    assert _steps(doc), "release job declares no steps"


def test_diff_is_exactly_one_step_inserted_before_publish():
    base_names = [s.get("name") for s in _steps(_base_doc())]
    cand_names = [s.get("name") for s in _steps(_candidate_doc())]

    added = [n for n in cand_names if n not in base_names]
    removed = [n for n in base_names if n not in cand_names]

    assert added == [BUILD_STEP_NAME], (
        f"exactly one step must be added; added={added!r}"
    )
    assert removed == [], f"no base step may be removed; removed={removed!r}"
    assert cand_names.count(BUILD_STEP_NAME) == 1, "the new step appears once"

    # It sits immediately before the publish step, so the engine attaches the
    # freshly built assets in the same job.
    i_build = cand_names.index(BUILD_STEP_NAME)
    i_publish = cand_names.index(PUBLISH_STEP_NAME)
    assert i_publish - i_build == 1, (
        f"{BUILD_STEP_NAME!r} must directly precede {PUBLISH_STEP_NAME!r}; "
        f"got index {i_build} vs {i_publish}"
    )


def test_every_other_step_run_text_is_byte_identical_to_base():
    base_steps = _steps(_base_doc())
    cand_steps = _steps(_candidate_doc())

    for base_step in base_steps:
        key = _step_key(base_step)
        matches = [s for s in cand_steps if _step_key(s) == key]
        assert len(matches) == 1, f"step {key!r} is not preserved exactly once"
        # Whole-step equality: the run text, its env, and (for action steps) the
        # ``uses``/``with`` — nothing about a pre-existing step may move.
        assert matches[0] == base_step, (
            f"step {key!r} changed; the diff must touch no other step"
        )

    # Whole-document equality once the one new step is removed: nothing else in
    # the workflow — no job key, no trigger, no comment-adjacent scalar — moved.
    stripped = _candidate_doc()
    stripped["jobs"]["release"]["steps"] = [
        s for s in _steps(stripped) if _step_key(s) != BUILD_STEP_NAME
    ]
    assert stripped == _base_doc(), (
        "the workflow must be the base plus exactly one step and nothing else"
    )


def test_diff_touches_only_the_workflow_and_this_test():
    changed = set(_git("diff", "--name-only", BASE_COMMIT).splitlines())
    changed |= set(_git("ls-files", "--others", "--exclude-standard").splitlines())

    assert changed == {WORKFLOW_REL, TEST_REL}, (
        f"the change must touch only {WORKFLOW_REL!r} and {TEST_REL!r}; "
        f"got {sorted(changed)}"
    )


def test_build_step_declares_the_release_assets_contract():
    run = _run_text(_build_step())
    # A fresh directory, so a stale artifact can never be published.
    assert "rm -rf release-assets" in run
    assert "--outdir release-assets" in run or "--outdir" in run
    # The declared artifact names come from [project], not a product literal.
    assert '"%s-%s.tar.gz" % (name, version)' in run
    assert '"%s-%s-py3-none-any.whl" % (name, version)' in run
    assert '"[project]"' in run, "the distribution name comes from [project]"


def test_build_step_is_reproducible_and_checksummed():
    run = _run_text(_build_step())
    assert "SOURCE_DATE_EPOCH" in run, "the build must pin SOURCE_DATE_EPOCH"
    assert "git log -1 --format=%ct" in run, (
        "SOURCE_DATE_EPOCH must be the tag commit time"
    )
    assert "${TAG_NAME}^{}" in run, "the epoch is read from the tag's commit"
    # The sdist is rewritten to the epoch; setuptools does not do this itself.
    assert "tarfile.GNU_FORMAT" in run and "gzip.compress" in run
    assert "--no-isolation" in run, (
        "the build must run against the pinned toolchain, not an isolated fetch"
    )
    assert "SHA256SUMS" in run and "sha256sum -c SHA256SUMS" in run


def test_build_step_has_a_named_version_refusal():
    run = _run_text(_build_step())
    assert "ReleaseVersionMismatchError" in run, (
        "the negative control must refuse by name, not by a bare exit code"
    )
    assert "shutil.rmtree(dist, ignore_errors=True)" in run, (
        "a refusal must leave no files behind"
    )
    assert "UnexpectedReleaseAssetError" in run, (
        "the directory must hold exactly the declared assets"
    )


# ── Extracted-block execution against a hermetic project ───────────────────


_PYPROJECT = """\
[build-system]
requires = ["setuptools>=77.0.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "exampleproject"
version = "{version}"

[tool.setuptools.packages.find]
where = ["src"]
include = ["exampleproject*"]

[tool.setuptools]
py-modules = ["exampleproject_degraded"]
"""


def _write_block(harness: Path) -> Path:
    script = harness / "build_release_assets.sh"
    script.write_text(_run_text(_build_step()), encoding="utf-8")
    return script


def _make_project(
    tmp_path: Path, version: str = "0.3.0", tag: str | None = None
) -> Path:
    tag = tag or f"v{version}"
    proj = tmp_path / "project"
    (proj / "src" / "exampleproject").mkdir(parents=True)
    (proj / "src" / "exampleproject" / "__init__.py").write_text("VALUE = 1\n")
    (proj / "src" / "exampleproject_degraded.py").write_text("X = 1\n")
    (proj / "pyproject.toml").write_text(_PYPROJECT.format(version=version))
    _git_in(proj, "init", "-q")
    _git_in(proj, "config", "user.email", "test@example.invalid")
    _git_in(proj, "config", "user.name", "test")
    _git_in(proj, "add", ".")
    env = dict(os.environ, GIT_COMMITTER_DATE="2026-01-02T03:04:05Z")
    subprocess.run(
        ["git", "-C", str(proj), "commit", "-q", "-m", "release"],
        check=True, capture_output=True, text=True, env=env,
    )
    _git_in(proj, "tag", "-a", tag, "-m", tag)
    return proj


def _git_in(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def _run_block(tmp_path: Path, proj: Path, tag: str):
    harness = tmp_path / "harness"
    harness.mkdir(exist_ok=True)
    script = _write_block(harness)
    env = dict(os.environ, TAG_NAME=tag)
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(proj), env=env, capture_output=True, text=True,
    )


@_requires_build
def test_block_builds_exactly_the_declared_assets(tmp_path):
    proj = _make_project(tmp_path, "0.3.0")
    proc = _run_block(tmp_path, proj, "v0.3.0")
    assert proc.returncode == 0, (
        f"the extracted block failed (rc={proc.returncode}):\n"
        f"{proc.stdout}\n{proc.stderr}"
    )

    assets = proj / "release-assets"
    assert assets.is_dir(), "the block must create release-assets/"
    listing = sorted(p.name for p in assets.iterdir())
    assert listing == [
        "SHA256SUMS",
        "exampleproject-0.3.0-py3-none-any.whl",
        "exampleproject-0.3.0.tar.gz",
    ], f"exactly the wheel, the sdist and SHA256SUMS; got {listing}"

    # The wheel names the version its manifest declares.
    import zipfile

    with zipfile.ZipFile(assets / "exampleproject-0.3.0-py3-none-any.whl") as zf:
        meta = zf.read("exampleproject-0.3.0.dist-info/METADATA").decode("utf-8")
    version = re.search(r"(?m)^Version:\s*(\S+)\s*$", meta)
    assert version is not None and version.group(1) == "0.3.0", (
        f"wheel metadata version must be 0.3.0; got {version}"
    )

    # The checksum file lists relative names and verifies.
    check = subprocess.run(
        ["sha256sum", "-c", "SHA256SUMS"],
        cwd=str(assets), capture_output=True, text=True,
    )
    assert check.returncode == 0, (
        f"sha256sum -c SHA256SUMS must pass:\n{check.stdout}\n{check.stderr}"
    )
    assert ": OK" in check.stdout
    for line in (assets / "SHA256SUMS").read_text().splitlines():
        assert not line.startswith("/") and "  " in line


@_requires_build
def test_block_two_builds_give_identical_hashes(tmp_path):
    sums = []
    for i in (1, 2):
        run_root = tmp_path / f"run{i}"
        run_root.mkdir()
        proj = _make_project(run_root, "0.3.0")
        proc = _run_block(run_root, proj, "v0.3.0")
        assert proc.returncode == 0, f"run {i} failed:\n{proc.stderr}"
        sums.append((run_root, proj / "release-assets"))
    (r1, a1), (r2, a2) = sums
    assert (a1 / "SHA256SUMS").read_bytes() == (a2 / "SHA256SUMS").read_bytes(), (
        "two builds of the same tag must give byte-identical SHA256SUMS"
    )
    for name in ("exampleproject-0.3.0.tar.gz", "exampleproject-0.3.0-py3-none-any.whl"):
        assert (a1 / name).read_bytes() == (a2 / name).read_bytes(), (
            f"{name} differs between two builds — not reproducible"
        )


@_requires_build
def test_block_refuses_version_mismatch_and_leaves_no_files(tmp_path):
    # The tag names v0.3.0 but pyproject declares 0.3.1 — the negative control.
    proj = _make_project(tmp_path, "0.3.1", tag="v0.3.0")
    # Pre-create the directory with a stale file: a refusal must remove it.
    stale = proj / "release-assets"
    stale.mkdir()
    (stale / "STALE").write_text("stale\n")

    proc = _run_block(tmp_path, proj, "v0.3.0")
    assert proc.returncode != 0, "a tag/version mismatch must refuse"
    assert "ReleaseVersionMismatchError" in proc.stderr, (
        f"the refusal must name itself:\n{proc.stderr}"
    )
    assert not (proj / "release-assets").exists(), (
        "the refusal must leave no files behind"
    )
    assert not getattr(proc, "stdout", "").strip() or "built release assets" not in proc.stdout


def test_only_one_named_skip_exists():
    """Teeth: the only skip this suite carries is the named build-availability
    one, declared on ``_requires_build`` and applied to the three block-executing
    tests. A stray skip / xfail / importorskip / bare ``continue`` would defeat
    the criterion, so the module is parsed with ``ast`` and its decorators, call
    sites and control flow are inspected directly — never a text match that the
    prose above could satisfy."""
    source = (REPO_ROOT / TEST_REL).read_text(encoding="utf-8")
    tree = ast.parse(source)

    decorators: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            decorators.extend(ast.unparse(d) for d in node.decorator_list)
    assert decorators == ["_requires_build"] * 3, (
        f"only the named build-availability skip may gate tests; got {decorators}"
    )

    # No skip/xfail/importorskip invoked in the executable code. ``ast.Unparse``
    # names the callee, so a docstring that merely says the word "xfail" cannot
    # satisfy this — only a real call site can.
    banned_calls = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func)
        in {"pytest.skip", "pytest.importorskip", "pytest.xfail", "pytest.mark.xfail"}
    ]
    assert banned_calls == [], f"no skip/xfail may be invoked; got {banned_calls}"

    assert not any(isinstance(n, ast.Continue) for n in ast.walk(tree)), (
        "no bare `continue` may swallow a case"
    )

    # And the one named skip is genuinely the build-availability gate.
    assert "_requires_build = pytest.mark.skipif(" in source
    assert 'importlib.util.find_spec("build")' in source
