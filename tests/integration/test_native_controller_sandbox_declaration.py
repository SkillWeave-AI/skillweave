"""SW-SCOPE-010 unit C, criterion 2: the owner **sandbox declaration**.

The declaration is durable *owner-authored* state -- ``owner-sandbox.yaml`` in
the planning repository's initiative folder -- whose form the owner decision
(``attempts/SW-SCOPE-010-design-001/owner-decision-sandbox-declaration.md``)
fixes and which is never supplied through any tool, MCP argument or CLI
argument.  The production caller resolves the owner-state *directory* itself,
records the SHA-256 of the bytes it reads, and refuses **by name** for an
unlisted, undeclared, production-looking or changed declaration.

These tests drive the production caller
:func:`skillweave.cli.native_controller.resolve_sandbox_admission` (and the
loader it delegates to) against real declaration bytes in a temporary
owner-state directory.  No provider, gateway, network or CMS call is made, and
no credential is read.  The process-wide state the surface touches (the
``SIGTERM``/``SIGINT`` handlers the lease guard installs, and
``SKILLWEAVE_OWNER_STATE_DIR``) is snapshotted and restored in fixtures, so the
tests are order-independent.

Every test fails on the base ``f959627``: none of the sandbox names, the loader,
the admission caller or the four holds exist there.
"""

from __future__ import annotations

import hashlib
import os
import signal

import pytest

from skillweave.cli import native_controller as nc

#: A declaration file the owner authored: one preview target, explicitly a
#: sandbox (``production: false``), reached through the Elementeer MCP -- exactly
#: the first entry the owner decision names.
_OWNER_DECLARATION = b"""\
# Owner-authored sandbox declaration. Only the owner writes this file.
sandbox_targets:
  - name: fusionaize-preview
    adapter: elementeer-mcp
    match: '^preview\\.'
    production: false
"""


# ── Fixtures (restore process-wide state) ───────────────────────────────────


@pytest.fixture
def restore_signal_handlers():
    """Snapshot and restore the process-wide SIGTERM/SIGINT handlers.

    The controller's lease guard installs handlers for the duration of a drive.
    Restoring them afterwards keeps a later test (or the runner) from inheriting
    a displaced handler regardless of test order.
    """
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


@pytest.fixture
def restore_owner_state_env():
    """Snapshot and restore ``SKILLWEAVE_OWNER_STATE_DIR`` around a test."""
    key = "SKILLWEAVE_OWNER_STATE_DIR"
    saved = os.environ.get(key)
    try:
        yield key
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture
def owner_state(tmp_path, restore_owner_state_env):
    """A temp owner-state directory holding the owner's declaration file."""
    directory = tmp_path / "prds" / "2026-10-15-sw-scope-010-native-controller-wiring"
    directory.mkdir(parents=True)
    (directory / nc.OWNER_SANDBOX_DECLARATION_FILENAME).write_bytes(_OWNER_DECLARATION)
    return directory


# ── 1. Unlisted target -> refused by name ───────────────────────────────────


def test_unlisted_target_is_refused_by_name(owner_state, restore_signal_handlers):
    """A target the declaration does not list is refused under the named hold.

    The declaration lists ``fusionaize-preview``; a target name that is not in it
    is never invented, and nothing is returned for it.  The refusal names
    ``HOLD_SANDBOX_TARGET_UNLISTED`` and carries the digest of the bytes the
    refusal was decided against.
    """
    declaration = nc.load_owner_sandbox_declaration(os.fspath(owner_state))

    with pytest.raises(nc.SandboxDeclarationRefused) as excinfo:
        declaration.resolve_target("fusionaize-production-write")

    assert excinfo.value.hold == nc.HOLD_SANDBOX_TARGET_UNLISTED
    assert excinfo.value.digest == declaration.digest


# ── 2. Undeclared -> refused by name ────────────────────────────────────────


def test_undeclared_declaration_is_refused_by_name(tmp_path, restore_signal_handlers):
    """An absent declaration declares nothing: refused, never a default target.

    ``tmp_path`` contains no ``owner-sandbox.yaml``, so the loader refuses under
    ``HOLD_SANDBOX_UNDECLARED`` rather than assuming an implicit sandbox.
    """
    with pytest.raises(nc.SandboxDeclarationRefused) as excinfo:
        nc.load_owner_sandbox_declaration(os.fspath(tmp_path))

    assert excinfo.value.hold == nc.HOLD_SANDBOX_UNDECLARED


# ── 3. Production-looking target -> refused by name ─────────────────────────


def test_production_looking_target_is_refused_by_name(owner_state):
    """A production-looking target is refused under the named hold.

    A declaration entry that names a production-looking surface (the adapter
    names ``prod``) is refused even though it is listed and marked
    ``production: false`` -- the declaration is never a licence to reach
    production.  The refusal names ``HOLD_SANDBOX_PRODUCTION_LOOKING``.
    """
    path = owner_state / nc.OWNER_SANDBOX_DECLARATION_FILENAME
    path.write_bytes(
        b"sandbox_targets:\n"
        b"  - name: cms-write\n"
        b"    adapter: prod-elementeer-mcp\n"
        b"    match: '^cms\\\\.'\n"
        b"    production: false\n"
    )
    declaration = nc.load_owner_sandbox_declaration(os.fspath(owner_state))

    with pytest.raises(nc.SandboxDeclarationRefused) as excinfo:
        declaration.resolve_target("cms-write")

    assert excinfo.value.hold == nc.HOLD_SANDBOX_PRODUCTION_LOOKING


# ── 4. Declaration changed between load and apply -> refused by name ────────


def test_changed_declaration_is_refused_between_load_and_apply(owner_state):
    """Bytes that change between load and apply are refused under the hold.

    The loader records the digest of the bytes it read; when the file is
    rewritten before the admission receipt is produced, the recorded digest no
    longer matches and the target is refused under
    ``HOLD_SANDBOX_DECLARATION_CHANGED`` -- never applied against a declaration
    the owner did not author.
    """
    declaration = nc.load_owner_sandbox_declaration(os.fspath(owner_state))
    recorded = declaration.digest

    path = owner_state / nc.OWNER_SANDBOX_DECLARATION_FILENAME
    path.write_bytes(_OWNER_DECLARATION + b"# rewritten after load\n")

    with pytest.raises(nc.SandboxDeclarationRefused) as excinfo:
        declaration.receipt_for("fusionaize-preview")

    assert excinfo.value.hold == nc.HOLD_SANDBOX_DECLARATION_CHANGED
    assert excinfo.value.digest == recorded


# ── 5. Positive: an admitted target records the owner file's SHA-256 ────────


def test_admitted_target_records_the_declaration_digest(
    owner_state, restore_owner_state_env
):
    """The production caller admits the listed target and records the SHA-256.

    The declaration lists ``fusionaize-preview``; the production caller resolves
    the owner-state directory from its own configuration, admits the target and
    returns a receipt whose digest is the SHA-256 of the exact bytes read.
    """
    os.environ[restore_owner_state_env] = os.fspath(owner_state)
    # project_root is deliberately not the owner-state dir: the caller must
    # resolve the configured directory itself, not take one from the target.
    receipt = nc.resolve_sandbox_admission("fusionaize-preview")

    assert receipt["target"]["name"] == "fusionaize-preview"
    assert receipt["target"]["production"] is False
    assert receipt["declaration_digest"] == hashlib.sha256(_OWNER_DECLARATION).hexdigest()
    assert receipt["declaration_path"].endswith(nc.OWNER_SANDBOX_DECLARATION_FILENAME)


# ── 6. The declaration cannot be supplied through any tool/CLI argument ─────


def test_declaration_cannot_be_supplied_through_any_argument(owner_state):
    """No tool or CLI argument can supply the declaration or its bytes.

    Three independent facts, all from the real source surface:

    * the ``native-controller`` parser exposes **no** sandbox-related option, and
      an unknown ``--sandbox-*`` argument is a usage error (exit 2);
    * the loader takes exactly one parameter -- the owner-state *directory* --
      and appends the pinned file name itself, so it cannot be pointed at
      arbitrary bytes;
    * the production caller takes only the target *name* to admit; passing the
      declaration itself as a keyword argument is a ``TypeError``.
    """
    # (a) No sandbox option exists on the production parser.
    parser = nc.build_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    assert not any("sandbox" in opt for opt in option_strings)
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(
            ["--run-id", "r", "--task-id", "t", "--sandbox-declaration", "/tmp/x.yaml"]
        )
    assert excinfo.value.code == 2

    # (b) The loader cannot be handed a declaration file name.
    import inspect

    params = list(inspect.signature(nc.load_owner_sandbox_declaration).parameters)
    assert params == ["owner_state_dir"]
    with pytest.raises(TypeError):
        nc.load_owner_sandbox_declaration(
            os.fspath(owner_state), filename="/tmp/other.yaml"
        )

    # (c) The production caller cannot be handed declaration bytes.
    with pytest.raises(TypeError):
        nc.resolve_sandbox_admission(
            "fusionaize-preview", sandbox_declaration=_OWNER_DECLARATION
        )
