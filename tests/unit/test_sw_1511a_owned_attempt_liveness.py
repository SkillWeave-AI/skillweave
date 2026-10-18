"""SW-1511A-LIVENESS-001: owned-attempt liveness driven by the real launch path.

These tests drive the *production* entry point -- ``skillweave.cli.run.main``
(``skillweave run``) -- which launches a real child process through
``RunApplicationService.execute_streaming_native`` and
``skillweave.core.proc.runner.run_process_streaming``.  Nothing here mocks the
signal boundary, the supervision loop or the stream store: the child is a fake
process (no network, no provider, no credential), the store is the same
durable stream store the run writes, and the observation under test is the one
the supervision path itself produces.

Criterion 1 -- liveness classifies the nine declared states from semantic
progress, tool activity and owned process identity, and output-byte growth from
a capped stream is never progress.  Criterion 2 -- the self-matching wait loop,
the capped-stream blindness and the repeated gateway-error series are reproduced
and shown to be detected bounded, with the incident continued (not reset) when
the watcher is rebuilt from its durable snapshot.

Each test uses its own temporary root, so no run id is shared across runs and a
re-run is repeatable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skillweave.runtime import liveness as L


def _run_cli(tmp_path: Path, code: str, extra: list[str] | None = None):
    """Drive the real ``skillweave run`` command on a fake child process.

    The liveness root sits beside the durable stream root (its parent), which is
    what :func:`_read_liveness` reads; using the parent keeps a per-test
    temporary root per attempt with no shared run id.
    """
    import sys

    from skillweave.cli import run as run_cli

    stream_root = tmp_path / "streams"
    db_path = tmp_path / "cli.db"
    artifacts_path = tmp_path / "artifacts"
    argv = [
        "--tool", "opencode",
        "--model", "m",
        "--subject-repo", "repo",
        "--subject-commit", "c0ffee",
        "--task-id", "SW-1511A-LIVENESS-001",
        "--attempt", "1",
        "--db-path", str(db_path),
        "--stream-root", str(stream_root),
        "--artifacts-path", str(artifacts_path),
        *(extra or []),
        "--",
        sys.executable, "-c", code,
    ]
    rc = run_cli.main(argv)
    root = os.path.dirname(os.path.abspath(str(stream_root)))
    return rc, root


def _read_liveness(root: str, run_id: str = "any"):
    """Read the persisted watcher snapshot for the single attempt in *root*."""
    store = L.AttemptLivenessStore(root=root)
    base = Path(root) / "attempt-liveness"
    files = sorted(base.glob("*.watcher.json"))
    assert files, f"no durable liveness snapshot under {base}"
    snapshots = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    return snapshots


def _run_streaming(tmp_path: Path, code: str, *, stdout_retain_limit=None):
    """Drive the real production supervision entry on a fake child process.

    ``run_process_streaming`` is the path ``skillweave run`` uses to launch and
    supervise an attempt; it is where the liveness recorder is wired around the
    launched child.  Driving it directly is what lets a test set a real
    per-stream retention cap (the CLI exposes no such flag) while still running
    the genuine supervision loop, store and recorder -- no mock of the signal
    boundary, the loop or the stream store.
    """
    import sys

    from skillweave.core.proc.runner import run_process_streaming
    from skillweave.runtime.stream import DurableStreamStore

    store = DurableStreamStore(root=str(tmp_path / "streams"))
    return run_process_streaming(
        [sys.executable, "-c", code],
        run_id="run-capped",
        task_id="SW-1511A-LIVENESS-001",
        attempt=1,
        store=store,
        stdout_retain_limit=stdout_retain_limit,
        timeout=30.0,
    )


# ---------------------------------------------------------------------------
# Criterion 1: nine-state classification from semantic progress, never bytes
# ---------------------------------------------------------------------------

class TestLivenessClassificationFromSemanticProgress:
    def test_real_run_records_a_liveness_observation(self, tmp_path):
        code = "print('hello')\nprint('world')"
        rc, root = _run_cli(tmp_path, code)
        assert rc == 0
        snapshots = _read_liveness(root)
        # A completed attempt is a named observation, not silence.
        states = {
            r["state"]
            for snap in snapshots
            for r in (snap.get("order") or [])
        }
        assert "completed" in states

    def test_output_byte_growth_from_a_capped_stream_is_not_progress(self, tmp_path):
        """An unbounded byte stream with one frozen event stays *stalled*.

        The child writes far past a small retain cap so ``observed_bytes``
        grows without bound while the retained prefix is frozen and the single
        structured event never changes.  The classification must read the
        semantic evidence -- one self-matching event -- and call it stalled, not
        ``working``: byte growth from a capped stream is not progress.

        This drives the real supervision entry (``run_process_streaming``), the
        same path ``skillweave run`` uses, because only there is the retain cap
        a real parameter; the child is a fake process (no network, no provider,
        no credential).

        The child is driven by a *byte target*, not a wall-clock window: it
        keeps writing until it has emitted the bytes this test needs, so a
        loaded machine slows the child down but never changes how much it
        writes.  A generous upper time bound is kept only as a safety net
        against a hung child, never as the stopping condition -- a fixed time
        window would make the observed byte count a function of the CPU the
        child happened to get.
        """
        # A self-matching event, the same signature every line, emitted until the
        # byte target is reached while the retained prefix is capped so only its
        # head survives.  The parent's supervision loop samples liveness
        # repeatedly while the child streams, so the stalled classification is
        # observed mid-flight; the safety-net deadline only fires if the child
        # cannot make progress at all.
        wanted = 2_000_000
        code = (
            "import json, sys, time\n"
            "line = json.dumps({'kind':'tick','step':'same'}) + '\\n'\n"
            f"target = {wanted}\n"
            "deadline = time.time() + 60.0\n"
            "written = 0\n"
            "while written < target and time.time() < deadline:\n"
            "    sys.stdout.write(line)\n"
            "    sys.stdout.flush()\n"
            "    written += len(line)\n"
        )
        result = _run_streaming(tmp_path, code, stdout_retain_limit=1024)
        # The cap really bit: far more bytes observed than retained, and the
        # child reached its byte target rather than running out of time.
        assert result.stdout_observed_bytes >= wanted
        assert len(result.stdout_text) <= 1024
        assert len(result.stdout_text) < result.stdout_observed_bytes
        report = result.metadata["liveness"]
        series = report["series"]
        states = {
            record["state"]
            for records in series.values()
            for record in records
        }
        # The stalled classification must appear: a self-matching event is not
        # advancing progress, no matter how many bytes the capped stream grew.
        assert "stalled" in states
        # ... and it is never misread as working: emission without advancing
        # progress is exactly the capped-stream blindness the criterion names.
        assert "working" not in states
        # And the terminal observation is the recorded completed outcome, not a
        # byte-derived guess.
        assert report["state"] == "completed"

    def test_evidence_progress_ignores_observed_bytes(self):
        """The semantic constructor: bytes never flip progress on."""
        capped = L.derive_progress_evidence(
            observed_bytes=10_000_000, retained_bytes=1024,
            events=[{"kind": "log", "msg": "same"}] * 50,
        )
        assert capped.progress_observed() is False
        assert L.classify(capped) is L.LivenessState.STALLED

    def test_tool_activity_is_progress(self):
        evidence = L.derive_progress_evidence(
            events=[{"kind": "tool_call", "name": "read", "path": "a.py"}],
        )
        assert evidence.progress_observed() is True
        assert L.classify(evidence) is L.LivenessState.WORKING

    def test_declared_wait_and_failure_states_are_distinct(self):
        assert L.classify(L.derive_progress_evidence(wait_class="quota")) is (
            L.LivenessState.WAITING_FOR_QUOTA)
        assert L.classify(L.derive_progress_evidence(wait_class="owner")) is (
            L.LivenessState.WAITING_FOR_OWNER)
        assert L.classify(
            L.derive_progress_evidence(failure_class="auth")) is (
            L.LivenessState.AUTH_ERROR)
        assert L.classify(
            L.derive_progress_evidence(failure_class="transport")) is (
            L.LivenessState.TRANSPORT_ERROR)
        assert L.classify(
            L.derive_progress_evidence(failure_class="provider")) is (
            L.LivenessState.PROVIDER_ERROR)

    def test_terminal_states_are_named_not_guessed(self):
        assert L.classify(
            L.derive_progress_evidence(terminated=True, result_subtype="cancelled")
        ) is L.LivenessState.CANCELLED
        assert L.classify(
            L.derive_progress_evidence(
                terminated=True, result_subtype="exited", exit_code=0)
        ) is L.LivenessState.COMPLETED
        # Nothing observed at all is *unknown*, never a guess.
        assert L.classify(L.derive_progress_evidence()) is L.LivenessState.UNKNOWN


# ---------------------------------------------------------------------------
# Criterion 2: regressions reproduce + bounded detection, incident survives restart
# ---------------------------------------------------------------------------

class TestRegressionsBoundedDetection:
    def test_self_matching_wait_loop_is_stalled(self):
        """A loop re-emitting one event (clock-volatile only) is not progress."""
        events = [
            {"kind": "wait", "state": "polling", "ts": i, "seq": i}
            for i in range(1000)
        ]
        evidence = L.derive_progress_evidence(events=events)
        assert evidence.advancing_events == 1
        assert evidence.self_matching is True
        assert L.classify(evidence) is L.LivenessState.STALLED

    def test_capped_stream_blindness_is_reproduced(self):
        """The byte count grows while the retained prefix is frozen."""
        # Same retained prefix, different observed totals: the classification is
        # identical because it never reads the byte count as progress.
        first = L.derive_progress_evidence(
            observed_bytes=4096, retained_bytes=1024,
            events=[{"kind": "tick", "step": "same"}],
        )
        second = L.derive_progress_evidence(
            observed_bytes=40_000_000, retained_bytes=1024,
            events=[{"kind": "tick", "step": "same"}],
        )
        assert L.classify(first) is L.classify(second) is L.LivenessState.STALLED

    def test_repeated_gateway_error_series_is_one_bounded_incident(self):
        """N repeated gateway errors are one growing incident, not N incidents."""
        watcher = L.LivenessWatcher()
        for i in range(7):
            watcher.record(
                L.derive_progress_evidence(failure_class="provider"),
                run_id="run-gw", task_id="t", attempt=1,
            )
        series = watcher.state_series(L.LivenessState.PROVIDER_ERROR.value)
        assert len(series) == 7
        assert watcher.incident_length(
            L.LivenessState.PROVIDER_ERROR.value) == 7
        # The causal chain links each observation to its predecessor, root -1.
        assert series[0].caused_by == -1
        assert [r.caused_by for r in series[1:]] == [0, 1, 2, 3, 4, 5]
        # Distinct states never merge into the same series.
        assert watcher.incident_length(
            L.LivenessState.STALLED.value) == 0

    def test_incident_is_not_reset_on_watcher_restart(self):
        """Rehydrating the watcher from its snapshot continues the incident."""
        watcher = L.LivenessWatcher()
        for i in range(4):
            watcher.record(
                L.derive_progress_evidence(failure_class="provider"),
                run_id="run-restart", task_id="t", attempt=1,
            )
        snapshot = watcher.to_dict()
        rehydrated = L.LivenessWatcher.from_dict(snapshot)
        assert rehydrated.incident_length(
            L.LivenessState.PROVIDER_ERROR.value) == 4
        # A further observation continues the *same* incident from sequence 4.
        observation = rehydrated.record(
            L.derive_progress_evidence(failure_class="provider"),
            run_id="run-restart", task_id="t", attempt=1,
        )
        assert observation.sequence == 4
        assert observation.caused_by == 3
        assert rehydrated.incident_length(
            L.LivenessState.PROVIDER_ERROR.value) == 5

    def test_self_match_memory_survives_restart(self):
        """A restarted watcher keeps the self-match memory of seen signatures."""
        watcher = L.LivenessWatcher()
        events = [{"kind": "wait", "state": "polling", "ts": i} for i in range(3)]
        watcher.remember_signatures([L.progress_signature(e) for e in events])
        rehydrated = L.LivenessWatcher.from_dict(watcher.to_dict())
        # The same loop observed again after restart is still a self-match.
        evidence = L.derive_progress_evidence(
            events=events,
            previous_signatures=rehydrated.previous_signatures,
        )
        assert evidence.self_matching is True
        assert L.classify(evidence) is L.LivenessState.STALLED

    def test_durable_snapshot_round_trips_through_the_store(self, tmp_path):
        store = L.AttemptLivenessStore(root=str(tmp_path))
        watcher = L.LivenessWatcher()
        watcher.record(
            L.derive_progress_evidence(events=[{"kind": "tool_call"}]),
            run_id="r1", task_id="t1", attempt=1,
        )
        watcher.record(
            L.derive_progress_evidence(failure_class="provider"),
            run_id="r1", task_id="t1", attempt=1,
        )
        store.write(watcher, ("r1", "t1", 1))
        # A brand new store object reading the same root rehydrates the incident.
        fresh = L.AttemptLivenessStore(root=str(tmp_path))
        rehydrated = fresh.rehydrate("r1", "t1", 1)
        assert rehydrated.latest_state() == L.LivenessState.PROVIDER_ERROR.value
        assert rehydrated.incident_length("working") == 1
        assert rehydrated.incident_length("provider_error") == 1
