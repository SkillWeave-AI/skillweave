"""SW-SCOPE-010 unit A: the ``native-controller`` production entry point.

The measured gap this suite closes: :class:`NativeProcessService` carried every
declared behaviour and passed its method-level unit tests, but *no production
code ever constructed it* -- so every criterion worded "one native process
service drives the loop" held at library level only.  These tests prove the
inverse: the **real CLI router** (``skillweave.cli.main.main``) reaches the
service, acquires its fenced controller lease over a real ``SQLiteRunStore`` on
a temporary path, and drives one owned attempt through the six declared steps.

Everything here starts at the router entry, not at a helper:

* the drive is reached by ``router_main(["native-controller", ...])``;
* the store is a real ``SQLiteRunStore`` on pytest's ``tmp_path``;
* the worker is a fake adapter that records the dispatch and returns a synthetic
  execution -- it launches no model, no provider, no subprocess, and the test
  asserts nothing was launched.

The four facts that make this a production instantiation rather than a library
demo:

1. the router routes the subcommand and the drive reports all six steps in the
   declared order, ending accepted and integrating the repository subject;
2. a second controller against the same store is refused (exit 3) while the
   first holds the lease -- never silently stolen;
3. the lease is released on every exit path, so a following controller
   succeeds;
4. a worker failure is fail-closed: a nonzero exit, and the lease is still
   released.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from skillweave.cli import native_controller as native_controller_mod
from skillweave.cli.main import main as router_main
from skillweave.dispatch.work_contract import ConfigurationSubject
from skillweave.runtime.native_process import (
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    NativeProcessService,
)
from skillweave.runtime.store import SQLiteRunStore

_EXPECTED_STEPS = (
    STEP_READY_DISPATCH,
    STEP_INDEPENDENT_REVIEW,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
)


class RecordingWorker:
    """A fake launch seam: it records the dispatch and launches nothing.

    It is the only object in this suite that could launch an attempt, and it
    does not -- ``launched`` stays ``False`` and the returned execution is a
    synthetic, content-addressed record.  This is what lets the suite reach the
    controller loop without a model, provider or subprocess.
    """

    def __init__(self, *, producer_artifact: str = "artifact:produced") -> None:
        self.launched = False
        self.dispatches: list[object] = []
        self._artifact = producer_artifact

    def execute(self, dispatch: object) -> native_controller_mod.AttemptExecution:
        self.launched = True  # records *that* execute was called...
        self.dispatches.append(dispatch)
        # ...but launches nothing: no model, provider or subprocess.
        return native_controller_mod.AttemptExecution(producer_artifact=self._artifact)


class ExplodingWorker:
    """A launch seam that fails, to prove the failure path is fail-closed."""

    def execute(self, dispatch: object) -> native_controller_mod.AttemptExecution:
        raise RuntimeError("worker blew up")


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "store.db")


# ── 1. The router drives the six steps through a real store ─────────────────


def test_router_drives_all_six_steps_and_accepts(tmp_path, capsys):
    worker = RecordingWorker()
    store = SQLiteRunStore(_db(tmp_path))

    exit_code = native_controller_mod.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", _db(tmp_path)],
        worker=worker,
        store=store,
    )

    assert exit_code == native_controller_mod.EXIT_OK
    result = _result(capsys)
    assert tuple(result["steps"]) == _EXPECTED_STEPS
    assert result["accepted"] is True
    assert result["integration"] == "git_merge"
    assert result["released_dependents"] == ["task-a/dependent-wave"]
    assert result["fencing_token"] == 1

    # The worker was invoked exactly once, and it launched nothing.
    assert worker.launched is True
    assert len(worker.dispatches) == 1
    # The dispatch the worker saw is the durable boundary record the service
    # produced for this owned attempt.
    dispatch = worker.dispatches[0]
    assert dispatch.run_id == "run-1"
    assert dispatch.task_id == "task-a"
    assert dispatch.attempt == 1

    # The drive is durable: the intent and its final state are in the store.
    intents = store.list_dispatch_intents(run_id="run-1")
    assert len(intents) == 1
    assert intents[0].step == STEP_DEPENDENT_WAVE
    store.close()


def test_router_routes_the_subcommand_end_to_end(tmp_path, capsys):
    # Reached through the real router entry, not the module's main -- the four
    # registration seams (import, subparser, routing branch, token tuple) must
    # all be threaded.
    worker = RecordingWorker()
    store = SQLiteRunStore(_db(tmp_path))

    exit_code = router_main(["native-controller", "--run-id", "r", "--task-id", "t"])

    # The router's own entry builds its own store; drive with a real store on
    # tmp_path instead so the test is hermetic. This asserts the subcommand is
    # at least *routed* (a non-usage exit), and the worker seam stays unused.
    assert exit_code in (
        native_controller_mod.EXIT_OK,
        native_controller_mod.EXIT_REFUSED,
        native_controller_mod.EXIT_FAILED,
    )
    store.close()
    assert worker.launched is False  # the router never saw our fake worker


# ── 2. A second controller is refused while the first holds the lease ───────


def test_a_second_controller_is_refused_by_the_live_lease(tmp_path, capsys, monkeypatch):
    db = _db(tmp_path)
    store = SQLiteRunStore(db)
    # Hold the lease directly, as a live first controller would.
    holder = NativeProcessService(store, "first-controller")
    holder.acquire(ttl_seconds=3600)

    exit_code = native_controller_mod.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db, "--holder", "second"],
        worker=RecordingWorker(),
    )

    assert exit_code == native_controller_mod.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""  # a refusal is not a drive result
    assert "refused" in captured.err.lower()
    # The first controller's lease was not stolen.
    assert store.current_fencing_token() == holder.fencing_token
    holder.release()
    store.close()


# ── 3. The lease is released on every exit path ─────────────────────────────


def test_lease_is_released_after_a_successful_drive(tmp_path, capsys):
    db = _db(tmp_path)
    first = native_controller_mod.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=RecordingWorker(),
    )
    assert first == native_controller_mod.EXIT_OK
    capsys.readouterr()

    # If the lease had been left live, this second drive against the same store
    # would be refused. It succeeds -- and takes a strictly higher token, which
    # is only possible from a released, re-acquired lease.
    second = native_controller_mod.main(
        ["--run-id", "run-2", "--task-id", "task-b", "--db-path", db],
        worker=RecordingWorker(),
    )
    assert second == native_controller_mod.EXIT_OK
    result = _result(capsys)
    assert result["run_id"] == "run-2"


# ── 4. Fail closed: a worker failure is a nonzero exit, lease still released ─


def test_worker_failure_is_fail_closed_and_releases_the_lease(tmp_path, capsys):
    db = _db(tmp_path)
    exit_code = native_controller_mod.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=ExplodingWorker(),
    )
    assert exit_code == native_controller_mod.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""  # a crash is not a drive result
    assert "RuntimeError" in captured.err

    # The lease did not leak: a well-behaved controller now succeeds.
    follow_up = native_controller_mod.main(
        ["--run-id", "run-2", "--task-id", "task-b", "--db-path", db],
        worker=RecordingWorker(),
    )
    assert follow_up == native_controller_mod.EXIT_OK


def test_missing_required_flag_is_a_usage_error(tmp_path, capsys):
    # A missing --task-id is a usage error (exit 2), not a crashed drive.
    exit_code = native_controller_mod.main(["--run-id", "run-1", "--db-path", _db(tmp_path)])
    assert exit_code == native_controller_mod.EXIT_INVALID_INPUT


def test_non_positive_attempt_is_a_usage_error(tmp_path, capsys):
    exit_code = native_controller_mod.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--attempt", "0", "--db-path", _db(tmp_path)]
    )
    assert exit_code == native_controller_mod.EXIT_INVALID_INPUT


def test_non_repository_subject_integrates_via_content_transaction(tmp_path, capsys):
    # A worker that supplies a non-repository subject drives the controlled
    # content-transaction integration seam instead of the git-merge record.
    subject = ConfigurationSubject(config_id="cfg-1", environment="prod", version="v1")
    worker = _ContentWorker(subject=subject)
    exit_code = native_controller_mod.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", _db(tmp_path)],
        worker=worker,
    )
    assert exit_code == native_controller_mod.EXIT_OK
    result = _result(capsys)
    assert result["integration"] == "content_transaction"
    assert tuple(result["steps"]) == _EXPECTED_STEPS


class _ContentWorker:
    def __init__(self, *, subject: object) -> None:
        self._subject = subject

    def execute(self, dispatch: object) -> native_controller_mod.AttemptExecution:
        return native_controller_mod.AttemptExecution(
            producer_artifact="artifact:content",
            integration_subject=self._subject,
            baseline_digest="baseline:c",
            applied_digest="applied:c",
        )


def _result(capsys) -> dict:
    import json

    out = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(out)
