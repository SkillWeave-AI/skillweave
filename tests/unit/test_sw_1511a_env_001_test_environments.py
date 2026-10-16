"""Adversarial tests for SW-1511A-ENV-001 reproducible release-gate environments.

One test per lane criterion, each of which fails on the frozen base
``4773d60f82c4260e0407ff1b5bee532aa8f2da3d`` -- the module and the declared
input do not exist there, so every test dies at the import seam. The module is
imported lazily so the failure names the deliverable instead of erroring during
collection.

No network, no provider call, no credential: ``pip`` runs ``--no-index`` only
and the SDK is the sibling checkout named by ``SKILLWEAVE_SDK_DIR`` (or found
beside this worktree).
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

_MODULE = "skillweave.release.environment_attestation"


def _ea():
    """Import the candidate module, naming the deliverable on the base."""
    return importlib.import_module(_MODULE)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_sdk_dir() -> Path | None:
    """The sibling SDK checkout, resolved the way the declared input names it."""
    env = os.environ.get("SKILLWEAVE_SDK_DIR")
    if env and (Path(env) / "schema_version.toml").is_file():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "skillweave-sdk"
        if (candidate / "schema_version.toml").is_file():
            return candidate
    return None


def _require_sdk_dir() -> Path:
    """The SDK root the guard must attest, or a named skip."""
    sdk_dir = _find_sdk_dir()
    if sdk_dir is None:
        pytest.skip("no sibling skillweave-sdk checkout on this host")
    return sdk_dir


@pytest.fixture(scope="module")
def built_environment(tmp_path_factory):
    """One environment built from the single declared input, always returned.

    The build is the (slow) part shared by the build-requiring criteria, so it
    runs once. It is built with the ``refusals`` out-parameter, so an
    environment this offline host cannot fully complete is still returned with
    the refusal list -- the contract stays complete-or-refuse, and the test
    asserts both halves. Yields
    ``(ea, declaration, venv_dir, sdk_dir, refusals)``.
    """
    ea = _ea()
    declaration = ea.load_declaration()
    minor = "%d.%d" % sys.version_info[:2]
    if minor not in declaration.pythons:
        pytest.skip(f"session interpreter {minor} is not a declared environment")
    sdk_dir = _find_sdk_dir()
    if sdk_dir is None:
        pytest.skip("no sibling skillweave-sdk checkout on this host")
    env_root = tmp_path_factory.mktemp("envs")
    refusals: list[str] = []
    venv_dir = ea.build_environment(
        minor, declaration, env_root, sdk_dir=sdk_dir, refusals=refusals
    )
    return ea, declaration, venv_dir, sdk_dir, refusals


# ── criterion 1: one declared input builds complete non-editable envs ────────


def test_criterion1_declared_input_builds_non_editable_environment(built_environment):
    """One declared input → a non-editable environment, complete-or-refused.

    The single input must name all three supported interpreters and every
    criterion-1 collection dependency, and the built environment must be
    *non-editable*: genuine ``.dist-info`` metadata for the pinned SDK and no
    link back to the checkout source tree. A declared dependency the host
    genuinely cannot provide is refused by name, never narrowed into a partial
    environment silently presented as complete.
    """
    ea, declaration, venv_dir, _, refusals = built_environment

    assert declaration.pythons == ea.SUPPORTED_PYTHONS
    assert set(ea.SUPPORTED_PYTHONS) <= set(declaration.pythons)
    assert declaration.non_editable is True
    for name in ("setuptools", "wheel", "build", "jsonschema", "mcp"):
        assert name in declaration.collection_dependencies, name

    # Non-editable proof: real installed distribution metadata, no source link.
    site = next(venv_dir.glob("lib/python*/site-packages"))
    assert list(site.glob("skillweave_sdk-*.dist-info")), "no SDK .dist-info installed"
    for pattern in ("*.pth", "*.egg-link"):
        for path in site.glob(pattern):
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert str(_repo_root() / "src") not in text, f"editable link in {path.name}"

    # Complete-or-refuse: any dependency this host cannot supply is named, and
    # an empty refusal list means every declared distribution is really present.
    declared = ea.declared_distributions(declaration)
    for reason in refusals:
        assert reason.split(":", 1)[0] in declared, reason
    if not refusals:
        for name in declared:
            normalized = name.lower().replace("-", "_")
            assert list(site.glob(f"{normalized}-*.dist-info")), name


def test_criterion1_incomplete_dependency_is_refused_by_name(tmp_path):
    """Complete-or-refuse: an unprovided declared dependency is named, not dropped.

    This is the negative half of criterion 1. An empty declared wheelhouse
    forces the builder to fall back to the *running interpreter's own*
    distribution for each dependency; any dependency that interpreter cannot
    supply (``mcp`` on this host) must appear in a refusal that names it, and
    must never be silently omitted from a "successful" environment.
    """
    ea = _ea()
    declaration = ea.load_declaration()
    minor = "%d.%d" % sys.version_info[:2]
    if minor not in declaration.pythons:
        pytest.skip(f"session interpreter {minor} is not a declared environment")
    sdk_dir = _find_sdk_dir()
    if sdk_dir is None:
        pytest.skip("no sibling skillweave-sdk checkout on this host")

    empty_wheelhouse = tmp_path / "empty-wheelhouse"
    empty_wheelhouse.mkdir()
    try:
        venv_dir = ea.build_environment(
            minor,
            declaration,
            tmp_path / "envs",
            sdk_dir=sdk_dir,
            wheelhouse=empty_wheelhouse,
        )
    except ea.EnvironmentBuildError as exc:
        assert "refused dependencies" in str(exc)
        assert "mcp" in str(exc)
        return
    # If the host can supply every declared dependency, the environment must be
    # genuinely complete rather than silently partial.
    site = next(venv_dir.glob("lib/python*/site-packages"))
    assert list(site.glob("mcp-*.dist-info")), "environment omits the declared mcp"


# ── criterion 2: the full unit suite, no selective ignore from missing deps ──


def test_criterion2_session_guard_refuses_selective_ignore_cause(built_environment):
    """A missing declared dependency is a named refusal, never a silent skip.

    ``pytest.importorskip`` in the unit suite (for example
    ``pytest.importorskip("mcp")``) turns a missing declared dependency into a
    skip. The guard refuses first and names every dependency that would have
    been skipped -- the *selective-ignore cause*. The positive half then runs
    the guard against the *built* environment, whose interpreter is the session
    interpreter but whose SDK comes from the pinned wheel rather than from
    whatever site-packages the host session happens to expose.
    """
    ea, declaration, venv_dir, sdk_dir, _refusals = built_environment
    import_names = {ea.import_name(d) for d in ea.declared_distributions(declaration)}

    partial = {name: True for name in import_names}
    partial["mcp"] = False
    with pytest.raises(ea.SessionGuardError) as excinfo:
        ea.guard_session(
            declaration, venv_dir=venv_dir, sdk_dir=sdk_dir, available=partial
        )
    message = str(excinfo.value)
    assert "selective-ignore cause" in message
    assert "mcp" in message

    satisfied = {name: True for name in import_names}
    attestation = ea.guard_session(
        declaration,
        venv_dir=venv_dir,
        sdk_dir=sdk_dir,
        available=satisfied,
    )
    assert attestation.python_short == "%d.%d" % sys.version_info[:2]
    assert attestation.sdk_version == declaration.sdk_version


def test_criterion2_declared_python312_session_is_refused_by_name():
    """The declared 3.12 environment names the dependencies that would skip.

    On this host the declared ``python3.12`` cannot import several declared
    dependencies; the guard must refuse it and name them, proving the guard --
    not a silent ``importorskip`` -- is what stands between a partial session
    and the full unit suite.
    """
    ea = _ea()
    declaration = ea.load_declaration()
    other = next((p for p in ea.SUPPORTED_PYTHONS if p != "%d.%d" % sys.version_info[:2]), None)
    assert other is not None
    import shutil

    executable = shutil.which(f"python{other}")
    if executable is None:
        pytest.skip(f"declared interpreter python{other} is not on this host")

    import_names = {ea.import_name(d) for d in ea.declared_distributions(declaration)}
    availability = ea.probe_dependencies(Path(executable), import_names)
    missing = [n for n in import_names if not availability.get(n)]
    if not missing:
        pytest.skip(f"python{other} happens to supply every declared dependency")

    with pytest.raises(ea.SessionGuardError) as excinfo:
        ea.guard_session(declaration, session_python=Path(executable))
    message = str(excinfo.value)
    assert "selective-ignore cause" in message
    for name in missing:
        assert name in message


def test_criterion2_guard_refuses_a_receipt_from_another_session():
    """The guard binds a receipt to the exact session it attests."""
    ea = _ea()
    declaration = ea.load_declaration()
    import_names = {ea.import_name(d) for d in ea.declared_distributions(declaration)}
    satisfied = {name: True for name in import_names}

    foreign = {
        "schema": ea.ENV_ATTESTATION_SCHEMA_ID,
        **{field: "foreign" for field in ea.REQUIRED_RECEIPT_FIELDS},
    }
    with pytest.raises(ea.SessionGuardError) as excinfo:
        ea.guard_session(
            declaration,
            session_python=sys.executable,
            sdk_dir=_require_sdk_dir(),
            available=satisfied,
            receipt=foreign,
        )
    assert "does not match the receipt" in str(excinfo.value)


# ── criterion 3: every receipt names interpreter, SDK, lock digest and dir ───


def test_criterion3_receipt_records_every_required_field(built_environment):
    """A built environment yields a receipt carrying all eight required fields."""
    ea, declaration, venv_dir, sdk_dir, _refusals = built_environment

    receipt = ea.build_attestation(venv_dir, declaration, sdk_dir=sdk_dir).to_dict()
    for field in ea.REQUIRED_RECEIPT_FIELDS:
        assert str(receipt.get(field, "")).strip(), f"receipt omits {field}"
    assert receipt["sdk_version"] == declaration.sdk_version
    assert receipt["sdk_install_type"] in {"wheel", "directory"}
    assert receipt["sdk_dir"] == str(Path(sdk_dir).resolve())
    assert receipt["dependency_lock_digest"] == declaration.dependency_lock_digest
    assert Path(receipt["interpreter_path"]).resolve() == Path(receipt["interpreter_path"])
    ea.verify_attestation(receipt)


def test_criterion3_verify_refuses_incomplete_and_wrong_schema_receipts():
    """An incomplete or foreign receipt is refused, never read as unknown."""
    ea = _ea()

    with pytest.raises(ea.AttestationError):
        ea.verify_attestation(
            {"schema": "something/else", **{f: "x" for f in ea.REQUIRED_RECEIPT_FIELDS}}
        )

    complete = {
        "schema": ea.ENV_ATTESTATION_SCHEMA_ID,
        **{field: "value" for field in ea.REQUIRED_RECEIPT_FIELDS},
    }
    ea.verify_attestation(complete)  # complete receipts pass

    for field in ea.REQUIRED_RECEIPT_FIELDS:
        broken = dict(complete)
        del broken[field]
        with pytest.raises(ea.AttestationError) as excinfo:
            ea.verify_attestation(broken)
        assert field in str(excinfo.value)


def test_criterion3_declaration_pin_change_changes_the_digest(tmp_path):
    """A pin change requires a new attestation: the lock digest must differ.

    The declaration's digest covers every pin, so changing a pin yields a
    different digest -- which is exactly what makes the exact pins immutable
    per the boundary contract. The declaration lives in the checkout, so the
    mutated copy is written to the same ``profiles/`` shape (its repo root is
    the parent of that directory) to exercise only the digest change.
    """
    ea = _ea()
    declaration = ea.load_declaration()
    original = declaration.dependency_lock_digest

    edited = declaration.path.read_text(encoding="utf-8")
    changed = edited.replace(
        'unit_suite = "tests/unit"', 'unit_suite = "tests/unit/acceptance"'
    )
    assert changed != edited, "the declared input no longer names the unit suite"

    profiles = _repo_root() / "profiles"
    altered_path = profiles / "test-environments.v1.tmp.toml"
    altered_path.write_text(changed, encoding="utf-8")
    try:
        altered = ea.load_declaration(altered_path, require_supported_pythons=False)
        assert altered.unit_suite == "tests/unit/acceptance"
        assert altered.dependency_lock_digest != original
    finally:
        altered_path.unlink()
