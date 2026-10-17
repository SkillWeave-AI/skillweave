"""SW-1511A-GATE-001 / F-1511A-04: an inline-seam launch refusal is typed.

The fan-out path already degrades a child that cannot be spawned to a typed
``launch_failed`` outcome (``fanout/dispatch.py``). The single-lane *inline*
seam -- the one a serialized/INLINE lane travels via ``_run_lane`` ->
``_default_inline_seam`` -> ``runner_adapter.start_process`` -> ``Popen`` -- did
not guard its start, so a lane whose ``launch_command`` exists but is not
executable raised ``PermissionError`` straight out of ``dispatch()``: the whole
wave aborted with no ``DispatchRun`` and no durable record.

These tests run the REAL dispatch entry point (``OperatorDispatchApplication.
dispatch``) with only the workspace seam faked (no git, no worktree) and a real
subprocess launch, so the refusal is the actual OS ``PermissionError`` -- never
a mocked exception. No network, provider or credential is touched.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import yaml  # noqa: E402

from skillweave.dispatch.application import (  # noqa: E402
    OperatorDispatchApplication,
    ProvisionedWorkspace,
    WorkspaceSeam,
)


class _NoopWorkspace(WorkspaceSeam):
    """In-memory workspace seam: no git, no worktree, attests the lane base."""

    def provision(self, lane, run_id):
        return ProvisionedWorkspace(base_sha=lane.base or "", path=None)

    def release(self, lane, run_id):
        return None


def _write_fixture(tmp_path: Path, launch_command: str) -> tuple[str, str]:
    """Write a minimal one-lane sequence + profile and return their paths.

    The lane is ``mutating`` so it is provisioned and run (and, at
    ``max_parallel=1``, an ``inline``-group lane); its ``on_model_failure`` is
    ``skip`` so the wave itself must NOT halt on the typed failure.
    """
    base = "a" * 40
    prof = tmp_path / "profile.yaml"
    prof.write_text(
        yaml.safe_dump(
            {
                "name": "f1511a04-fixture",
                "tier": "balanced",
                "limits": {
                    "timeout": 10.0,
                    "max_retries": 0,
                    "min_models_required": 1,
                    "on_model_failure": "skip",
                    "max_correction_rounds_per_wave": 0,
                },
                "roles": {
                    "ops": {
                        "model": "faigate/dispatch-fixture-model",
                        "tool": {
                            "name": "worker",
                            "launch_command": launch_command,
                            "args": [],
                        },
                        "capabilities": {"can_mutate_run_state": True},
                    },
                },
            }
        )
    )
    seq = tmp_path / "sequence.yaml"
    seq.write_text(
        yaml.safe_dump(
            {
                "session_boundary": "batch",
                "profile": {"path": str(prof), "required": True},
                "execution_model": "cold",
                "max_correction_rounds_per_wave": 0,
                "max_parallel": 1,
                "lanes": [
                    {
                        "id": "lane-a",
                        "role": "ops",
                        "repo": "skillweave/repo-a",
                        "base": base,
                        "execution_model": "cold",
                        "mutating": True,
                        "depends_on": [],
                        "write_scope": ["skillweave/repo-a/**"],
                        "worktree": str(tmp_path / "wt-lane-a"),
                        "branch": "branch-lane-a",
                        "integration_policy": "independent",
                        "criterion_groups": [{"criteria": [1]}],
                    }
                ],
            }
        )
    )
    return str(seq), str(prof)


def _run(seq: str, prof: str) -> tuple[object, list[dict]]:
    sink = io.StringIO()
    app = OperatorDispatchApplication(workspace_seam=_NoopWorkspace())
    run = app.dispatch(seq, prof, wave="0", sink=sink)
    events = [json.loads(ln) for ln in sink.getvalue().splitlines() if ln.strip()]
    return run, events


def test_non_executable_launch_command_is_a_typed_launch_failed_outcome(tmp_path):
    """A ``+x``-less launch command yields a typed outcome, never a bare raise.

    This is the F-1511A-04 regression: pre-repair ``dispatch()`` propagates the
    child's ``PermissionError`` and never returns a ``DispatchRun``.
    """
    script = tmp_path / "worker.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, stat.S_IRUSR | stat.S_IWUSR)  # 0600 -- exists, not executable
    assert not os.access(str(script), os.X_OK)

    seq, prof = _write_fixture(tmp_path, str(script))

    # The refusal must be *typed and observable* on this seam, exactly as the
    # fan-out path already makes it -- so this call must return, not raise.
    run, events = _run(seq, prof)

    # A durable machine record exists for the refused child.
    assert len(run.results) == 1
    record = run.results[0]
    assert record["lane_id"] == "lane-a"
    assert record["outcome"] == "launch_failed"
    assert record["termination"] == "launch_failed"
    assert record["exit_code"] is None
    assert record["signal"] is None
    # Empty stdout/stderr receipts are still emitted, bound to the child run id.
    assert record["stdout"]["byte_length"] == 0
    assert record["stderr"]["byte_length"] == 0

    # The failure is recorded once, with its own outcome and a real message.
    assert len(run.failures) == 1
    assert run.failures[0]["outcome"] == "launch_failed"
    assert "PermissionError" in run.failures[0]["detail"]

    # The typed lifecycle is complete and visible on the stream: the lane went
    # through dispatch_started -> exactly one process_terminal (launch_failed)
    # -> evidence_recorded -> lane_terminal. The wave is not silently aborted.
    types = [e["event_type"] for e in events]
    assert types.index("dispatch_started") < types.index("process_terminal")
    assert types.index("process_terminal") < types.index("lane_terminal")
    terminals = [e for e in events if e["event_type"] == "process_terminal"]
    assert len(terminals) == 1
    assert terminals[0]["process_status"] == "launch_failed"

    # ``skip`` policy: the typed failure is recorded but the wave does not halt.
    assert run.halted is False
    assert run.halt_reason is None


def test_inline_launch_failed_outcome_matches_the_fanout_shape(tmp_path):
    """The inline typed result is byte-for-byte the fan-out's typed shape.

    Both seams must classify a never-spawned child identically (same outcome,
    same empty-receipt references, same terminal mapping), so a consumer never
    has to branch on which seam produced it. Proven directly against the
    canonical fan-out constructor, not a re-derivation.
    """
    from skillweave.fanout.dispatch import _launch_failed_result, _resolve_outcome

    script = tmp_path / "worker.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, stat.S_IRUSR | stat.S_IWUSR)

    seq, prof = _write_fixture(tmp_path, str(script))
    run, _ = _run(seq, prof)
    inline_record = run.results[0]

    canonical = _launch_failed_result(
        [str(script)],
        run_id="run-0",
        repo="skillweave/repo-a",
        commit="a" * 40,
        tool="worker",
        model="faigate/dispatch-fixture-model",
        created_at="2026-01-01T00:00:00+00:00",
        message="PermissionError: denied",
    )
    assert _resolve_outcome(canonical) == "launch_failed"
    assert inline_record["outcome"] == _resolve_outcome(canonical)
    assert inline_record["termination"] == canonical.termination
    assert inline_record["exit_code"] == canonical.exit_code
    assert inline_record["signal"] == canonical.signal


def test_startable_launch_command_still_runs_and_succeeds(tmp_path):
    """The guard is a refusal *path*, not a behaviour change: a valid command
    still launches, waits and is recorded as a plain success.
    """
    script = tmp_path / "worker.sh"
    script.write_text("#!/bin/sh\necho ok\n")
    os.chmod(script, 0o755)

    seq, prof = _write_fixture(tmp_path, str(script))
    run, events = _run(seq, prof)

    assert run.halted is False
    assert len(run.results) == 1
    record = run.results[0]
    assert record["outcome"] == "exit_code"
    assert record["exit_code"] == 0
    assert run.failures == []
    terminals = [e for e in events if e["event_type"] == "process_terminal"]
    assert len(terminals) == 1
    assert terminals[0]["process_status"] == "exited"


def test_lifecycle_observation_sees_start_and_terminal_for_a_refused_child(tmp_path):
    """A never-started child still reports both lifecycle observations.

    The fan-out path emits ``started`` (handle ``None``) then ``terminal`` for a
    launch failure so the passive heartbeat/observer cadence is bounded. The
    inline seam must mirror that: a ``started`` observation fires even though no
    process exists, and a ``terminal`` one closes it.
    """
    from skillweave.dispatch.application import _default_inline_seam

    script = tmp_path / "worker.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, stat.S_IRUSR | stat.S_IWUSR)

    seen: list[tuple[str, object]] = []

    def _observe(*, child_key, dispatch_id, phase, handle):
        seen.append((phase, handle))

    result = _default_inline_seam(
        [str(script)],
        run_id="run-x",
        subject_repo="skillweave/repo-a",
        subject_commit="a" * 40,
        tool="worker",
        model="faigate/dispatch-fixture-model",
        on_child_lifecycle=_observe,
    )

    assert [phase for phase, _ in seen] == ["started", "terminal"]
    assert all(handle is None for _, handle in seen)
    assert len(result.children) == 1
    assert result.children[0].outcome == "launch_failed"
