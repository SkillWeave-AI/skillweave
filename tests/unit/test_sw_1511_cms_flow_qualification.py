"""SW-1511-CMS-FLOWS: the declared CMS paths have a production consumer.

The frozen declaration module (:mod:`skillweave.blueprint.cms_path_qualification`)
shipped with no production caller -- it was imported only by its own test, so
"the five CMS paths exist" held at library level and nowhere else. This test
binds the *consumer* that closes that gap:

* :func:`skillweave.runtime.cms_flow_qualification.qualify_cms_path` takes a
  :class:`~skillweave.blueprint.cms_path_qualification.CmsPath` from
  ``INITIAL_CMS_PATHS`` and produces a
  :class:`~skillweave.blueprint.cms_path_qualification.CmsProcessQualification`
  through the *existing* native controller seam;
* the flow is reachable from the **real production entry point** -- the
  ``skillweave native-controller`` command, via its additive ``--qualify-cms-path``
  flag -- not only from the library.

Every assertion of behaviour is a **fake at the process boundary**: the dummy
:class:`~skillweave.runtime.native_process.RawContentAdapter` over in-memory
content and a declared sandbox binding. No provider, gateway or CMS call is made
and no network is touched.

On the base SHA (bdb1c6c) the module under test does not exist, so this file
fails at import -- the red proof that the consumer is new.
"""

from __future__ import annotations

import json

from skillweave.blueprint.cms_path_qualification import (
    DISPOSITION_NOT_PERFORMED,
    INITIAL_CMS_PATHS,
    PATH_LANDING_PAGE,
    SCHEMA_VERSION,
    CmsPath,
)
from skillweave.cli import native_controller as nc
from skillweave.runtime.cms_flow_qualification import (
    CMS_QUALIFICATION_SANDBOX_TARGET,
    CmsFlowQualificationRefused,
    qualify_cms_path,
)
from skillweave.runtime.store import SQLiteRunStore


def _store(tmp_path) -> SQLiteRunStore:
    return SQLiteRunStore(db_path=str(tmp_path / "store.db"))


def _declared_path() -> CmsPath:
    """The landing-page path, taken from the *frozen* five-path declaration."""
    return INITIAL_CMS_PATHS[1]


# ── the library consumer ────────────────────────────────────────────────────


def test_qualify_cms_path_drives_the_lifecycle_and_reads_it_back(tmp_path):
    """A declared path is driven end-to-end and read back from durable state."""
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, _declared_path(), run_id="run-qual", task_id="task-qual"
        )
        # The path is the *frozen* declaration, not a caller invention.
        assert result.path is _declared_path()
        assert result.path.path_id == PATH_LANDING_PAGE

        # The transaction genuinely ran the reversible lifecycle to its end.
        assert result.final_stage == "restored"

        # The readback and restore verdicts come from the *persisted* record.
        persisted = store.get_cms_transaction(result.run_id, result.txn_id)
        assert persisted is not None
        assert persisted["stage"] == "restored"
        assert persisted["verified"] is True
        assert persisted["restored"] is True

        q = result.qualification
        assert q.artifact == SCHEMA_VERSION
        assert q.readback_verified is True
        assert q.restore_verified is True
        # The producer is *derived* from the owned attempt, never declared here.
        assert q.producer == persisted["producer_identity"]
        assert q.producer.startswith("attempt:")
        # Source-bound pins are the real, persisted hashes.
        assert q.pins["source_digest"] == persisted["baseline_hashes"]["semantic"]
    finally:
        store.close()


def test_qualification_stays_not_performed_and_names_what_it_did_not_measure(tmp_path):
    """Only one of the six required outcomes is measured, so it stays honest.

    A ``performed`` claim requires all six measurable outcomes; this consumer
    measures only ``success``, so it must not forge the rest -- it returns a
    ``not-performed`` record carrying the measured evidence and a named
    limitation, never a prewritten receipt.
    """
    store = _store(tmp_path)
    try:
        result = qualify_cms_path(
            store, "cms-generic-request", run_id="run-np", task_id="task-np"
        )
        q = result.qualification
        assert q.disposition == DISPOSITION_NOT_PERFORMED
        assert q.is_performed is False
        # It genuinely measured the success outcome...
        assert q.measured_outcomes == ("success",)
        # ...and names the five outcomes it did not measure.
        assert q.limitations == (
            "only the 'success' outcome was measured by driving the reversible "
            "lifecycle; 'wait', 'failure', 'cancel', 'resume' and "
            "'publication-refusal' are not measured by this consumer, so the "
            "record stays 'not-performed' by contract",
        )
    finally:
        store.close()


def test_unknown_path_is_refused_before_any_effect(tmp_path):
    """A path outside the frozen five is refused, not silently qualified."""
    store = _store(tmp_path)
    try:
        try:
            qualify_cms_path(store, "cms-does-not-exist", run_id="run-x")
        except CmsFlowQualificationRefused as exc:
            assert "cms-does-not-exist" in str(exc)
        else:  # pragma: no cover - the refusal is the assertion
            raise AssertionError("an undeclared CMS path must be refused")
    finally:
        store.close()


# ── the production entry point ──────────────────────────────────────────────


def test_native_controller_cli_qualifies_a_cms_path(tmp_path, capsys):
    """The flow is reachable from the real command, not only the library."""
    store = _store(tmp_path)
    try:
        code = nc.main(
            [
                "--run-id", "run-cli",
                "--task-id", "task-cli",
                "--qualify-cms-path", PATH_LANDING_PAGE,
            ],
            store=store,
        )
        out = capsys.readouterr().out
    finally:
        store.close()

    assert code == nc.EXIT_OK
    payload = json.loads(out)
    assert payload["path"]["path_id"] == PATH_LANDING_PAGE
    assert payload["final_stage"] == "restored"
    assert payload["qualification"]["artifact"] == SCHEMA_VERSION
    assert payload["qualification"]["disposition"] == DISPOSITION_NOT_PERFORMED
    assert payload["qualification"]["readback_verified"] is True
    assert payload["qualification"]["restore_verified"] is True


def test_native_controller_cli_refuses_an_undeclared_path(tmp_path, capsys):
    """The command maps an undeclared path to a usage refusal (exit 2)."""
    store = _store(tmp_path)
    try:
        code = nc.main(
            [
                "--run-id", "run-bad",
                "--task-id", "task-bad",
                "--qualify-cms-path", "cms-not-declared",
            ],
            store=store,
        )
        err = capsys.readouterr().err
    finally:
        store.close()

    assert code == nc.EXIT_INVALID_INPUT
    assert "cms-not-declared" in err


def test_qualification_sandbox_target_is_a_declared_sandbox():
    """The driven target is an explicit sandbox, never a production surface."""
    assert CMS_QUALIFICATION_SANDBOX_TARGET == "sandbox/ch"
    for marker in ("production", "prod", "live", "real-channel"):
        assert marker not in CMS_QUALIFICATION_SANDBOX_TARGET
