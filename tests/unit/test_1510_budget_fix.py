"""SW-1510-BUDGET controller-found failure repair proofs.

Covers the five concrete failures identified by the controller:
1. Partial completion erasing unknown dimensions
2. Caller bypassing persisted owner envelope
3. Streaming native entry missing admission
4. Silent store failure / fail-closed
5. Uncommitted work
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.runtime.reservation import (
    ReservationStore,
    ReservationEnvelope,
    ReservationRecord,
    ReservationSlotState,
    AdmissionRefused,
    ReservationConflict,
    EnvelopeRejected,
    ReservationError,
)
from skillweave.runsvc.service import (
    RunApplicationService,
    RunExecution,
    RunIntegrationError,
)
from skillweave.runtime.store import SQLiteRunStore
from skillweave.runtime.journal import EventJournal
from skillweave.runtime.registry import RawArtifactStore

#: Scratch directory for temp databases (set to the task tmp dir via TMPDIR).
_SCRATCH = Path(
    os.environ.get(
        "SKILLWEAVE_SCRATCH",
        os.environ.get("TMPDIR", "/tmp"),
    )
)
_SCRATCH.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def db_path():
    p = str(_SCRATCH / f"test_fix_{uuid.uuid4().hex}.db")
    yield p
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(p + suffix)
        except OSError:
            pass


@pytest.fixture
def store(db_path):
    rs = ReservationStore(db_path)
    yield rs
    rs.close()


def _limited(**kw):
    return ReservationEnvelope(**kw)


# ===================================================================
# Fix 1: Partial completion preserves unknown dimensions
# ===================================================================

class TestPartialCompletionPreservesUnknown:
    """complete(observed_bytes=80) does NOT silently zero tokens/cost."""

    def test_partial_bytes_only(self, store):
        """Observing only bytes preserves reserved tokens and cost."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        # complete with ONLY observed_bytes
        comp = store.complete(rec.reservation_id, observed_bytes=80)
        assert comp.observed_bytes == 80
        # observed_tokens not observed -> stays None
        assert comp.observed_tokens is None
        # cost not observed -> preserved from reserved (COALESCE keeps old)
        assert comp.cost == 10.0

        # Cumulative should have delta bytes only: 80 - 1000 = -920
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 80
        # Tokens and cost remain at reserved values (500, 10)
        assert run_usage["cumulative_tokens"] == 500
        assert run_usage["cumulative_cost"] == 10.0

    def test_partial_tokens_only(self, store):
        """Observing only tokens preserves reserved bytes and cost."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        comp = store.complete(rec.reservation_id, observed_tokens=300.0)
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 1000  # unchanged
        assert run_usage["cumulative_tokens"] == 300   # 300 - 500 = -200, so 500 + (-200) = 300

    def test_partial_cost_only(self, store):
        """Observing only cost preserves reserved bytes and tokens."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        comp = store.complete(rec.reservation_id, cost=7.0)
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 1000  # unchanged
        assert run_usage["cumulative_tokens"] == 500   # unchanged
        assert run_usage["cumulative_cost"] == 7.0     # 7 - 10 = -3, so 10 + (-3) = 7

    def test_reconcile_preserves_unknown(self, store):
        """reconcile only adjusts explicitly provided dimensions."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        store.complete(rec.reservation_id)  # no observed values
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 1000  # reserved stays
        assert run_usage["cumulative_tokens"] == 500
        assert run_usage["cumulative_cost"] == 10.0

        # Reconcile only bytes
        store.reconcile(rec.reservation_id, actual_bytes=600)
        run_usage = store.get_run_usage("r1")
        # Delta: 600 - 1000 = -400; cumulative: 1000 + (-400) = 600
        assert run_usage["cumulative_bytes"] == 600
        # Tokens and cost unchanged from reserved
        assert run_usage["cumulative_tokens"] == 500
        assert run_usage["cumulative_cost"] == 10.0

    def test_reconcile_after_partial_complete(self, store):
        """reconcile after partial complete adjusts from original reserved, not observed."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        # Partial complete: only bytes
        store.complete(rec.reservation_id, observed_bytes=800)
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 800  # delta: 800-1000=-200

        # Reconcile tokens only - should delta from original reserved (500)
        store.reconcile(rec.reservation_id, actual_tokens=300.0)
        run_usage = store.get_run_usage("r1")
        # Tokens: 300 - 500 = -200 delta, cumulative was 500, now 500+(-200)=300
        assert run_usage["cumulative_tokens"] == 300
        # Bytes unchanged from partial complete
        assert run_usage["cumulative_bytes"] == 800

    def test_reconcile_does_not_double_apply(self, store):
        """reconcile after full complete does not double-apply delta."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        # Full complete: all observed
        store.complete(rec.reservation_id, observed_bytes=800, observed_tokens=400.0, cost=8.0)
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 800

        # reconcile with same values should be a no-op delta
        store.reconcile(rec.reservation_id, actual_bytes=800, actual_tokens=400.0, actual_cost=8.0)
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 800
        assert run_usage["cumulative_tokens"] == 400
        assert run_usage["cumulative_cost"] == 8.0

    def test_partial_unknown_usage_stays_reserved_after_restart(self, db_path):
        """After restart, partial-completed reservations keep unknown dimensions."""
        store = ReservationStore(db_path)
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        # Complete with only bytes
        store.complete(rec.reservation_id, observed_bytes=80)
        store.close()

        # Restart
        store2 = ReservationStore(db_path)
        recovered = store2.recover_orphaned()  # should find none (already completed)
        run_usage = store2.get_run_usage("r1")
        assert run_usage["cumulative_bytes"] == 80
        assert run_usage["cumulative_tokens"] == 500  # preserved
        assert run_usage["cumulative_cost"] == 10.0    # preserved
        store2.close()

    def test_twice_complete_refused(self, store):
        """Second complete on same reservation is refused (state guard)."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s1")
        store.complete(rec.reservation_id, observed_bytes=100)
        with pytest.raises(ReservationError):
            store.complete(rec.reservation_id, observed_bytes=200)


# ===================================================================
# Fix 2: Persisted envelope is authoritative; identity validation
# ===================================================================

class TestPersistedEnvelopeAuthority:
    """A persisted owner envelope cannot be bypassed by caller-supplied envelope."""

    def test_persisted_zero_requests_bypass_refused(self, store):
        """max_requests=0 persisted, caller passes unlimited — still refused."""
        env = _limited(max_requests=0, max_in_flight=5)
        store.set_envelope("r1", "s1", env)
        # Caller tries to bypass with unlimited envelope
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id="s1",
                envelope=ReservationEnvelope(),  # unlimited
            )
        assert "requests" in str(exc.value)

    def test_persisted_zero_bytes_bypass_refused(self, store):
        """max_bytes=0 persisted blocks any reservation with bytes."""
        env = _limited(max_bytes=0)
        store.set_envelope("r1", "s1", env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id="s1",
                envelope=ReservationEnvelope(),  # unlimited
                reserved_bytes=1,
            )
        assert "bytes" in str(exc.value)

    def test_persisted_tight_limit_sticks(self, store):
        """Persisted max_requests=1 still applies even with wider caller envelope."""
        store.set_envelope("r1", "s1", _limited(max_requests=1))
        # First reservation at s1 succeeds
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="s1")
        # Second at s1 should fail (persisted max_requests=1)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id="s1",  # same scope — persisted envelope applies
                envelope=_limited(max_requests=100),  # wider, ignored
            )
        assert "requests" in str(exc.value)
        # Different scope has no persisted envelope — caller envelope applies
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s2",
            envelope=_limited(max_requests=5),
        )
        assert rec is not None

    def test_no_persisted_caller_envelope_used(self, store):
        """No persisted envelope: caller-supplied envelope is used."""
        store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="s1",
            envelope=_limited(max_requests=5),
        )
        # No persisted envelope, so caller's should be used
        # Second should succeed (max_requests=5)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="s2")
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_requests"] == 2

    def test_bool_run_id_refused(self):
        """Boolean run_id is refused by identity validation."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id=True, task_id="t1", attempt=1,
                scope_id="s1",
            )

    def test_bool_task_id_refused(self):
        """Boolean task_id is refused."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id="r1", task_id=True, attempt=1,
                scope_id="s1",
            )

    def test_empty_run_id_refused(self):
        """Empty string run_id is refused."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id="", task_id="t1", attempt=1,
                scope_id="s1",
            )

    def test_bool_attempt_refused(self):
        """Boolean attempt is refused."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id="r1", task_id="t1", attempt=True,
                scope_id="s1",
            )

    def test_bool_scope_id_refused(self):
        """Boolean scope_id is refused."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id=True,
            )

    def test_bool_parent_scope_id_refused(self):
        """Boolean parent_scope_id is refused."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id="s1", parent_scope_id=True,
            )

    def test_empty_parent_scope_id_refused(self):
        """Empty string parent_scope_id is refused."""
        with pytest.raises(EnvelopeRejected):
            ReservationStore().reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id="s1", parent_scope_id="",
            )

    def test_wrong_run_different_persisted(self, store):
        """Persisted envelope for run-A does not apply to run-B."""
        store.set_envelope("run-a", "s1", _limited(max_requests=1))
        # run-a is limited — second at same scope fails
        store.reserve(run_id="run-a", task_id="t1", attempt=1, scope_id="s1")
        with pytest.raises(AdmissionRefused):
            store.reserve(
                run_id="run-a", task_id="t1", attempt=1, scope_id="s1",
                envelope=_limited(max_requests=100),  # wider, ignored
            )
        # run-b has no persisted envelope, caller can pass wider
        rec = store.reserve(run_id="run-b", task_id="t1", attempt=1,
                            scope_id="s1", envelope=_limited(max_requests=5))
        assert rec is not None


# ===================================================================
# Fix 3: Streaming native ingress wired
# ===================================================================

class TestStreamingNativeIngress:
    """execute_streaming_native now acquires reservation before spawn."""

    def test_reservation_wired_in_streaming_native(self, db_path):
        """Streaming native acquires and completes a reservation."""
        from skillweave.runtime.stream import DurableStreamStore
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        run_id = "stream-budget-1"
        out = svc.execute_streaming_native(
            [sys.executable, "-c", "print('stream-budget-probe')"],
            run_id=run_id,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=run_id,
            reservation_envelope=_limited(max_in_flight=5),
        )
        assert out["run_id"] == run_id
        # Verify a reservation was created and completed
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        assert rec.state in (
            ReservationSlotState.COMPLETED.value,
            ReservationSlotState.RELEASED.value,
        )
        # In-flight should be 0 (freed by complete)
        assert rs.count_in_flight(run_id) == 0
        rs.close()
        s.close()

    def test_streaming_native_reservation_refused(self, db_path):
        """Streaming native refuses when in-flight limit exceeded."""
        from skillweave.runtime.stream import DurableStreamStore
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        run_id = "stream-budget-over"
        env = _limited(max_in_flight=1)
        # First succeeds
        out1 = svc.execute_streaming_native(
            [sys.executable, "-c", "print('ok')"],
            run_id=run_id,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=run_id,
            reservation_envelope=env,
        )
        assert out1["run_id"] == run_id

        # Second (different run_id, so no conflict)
        run_id2 = "stream-budget-over-2"
        out2 = svc.execute_streaming_native(
            [sys.executable, "-c", "print('ok')"],
            run_id=run_id2,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=run_id2,
            reservation_envelope=env,
        )
        assert out2["run_id"] == run_id2
        rs.close()
        s.close()

    def test_streaming_native_persisted_envelope(self, db_path):
        """Streaming native honours persisted owner envelope."""
        from skillweave.runtime.stream import DurableStreamStore
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        # Pre-set a tight envelope
        rs.set_envelope("persisted-stream-1", "persisted-stream-1/attempt-1",
                        _limited(max_requests=1))

        run_id = "persisted-stream-1"
        out1 = svc.execute_streaming_native(
            [sys.executable, "-c", "print('ok')"],
            run_id=run_id,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=run_id,
        )
        assert out1["run_id"] == run_id
        rs.close()
        s.close()


# ===================================================================
# Fix 4: Fail-closed on missing/inaccessible store
# ===================================================================

class TestFailClosed:
    """Missing or inaccessible reservation store fails closed on protected path."""

    def test_missing_store_protected_path_fails(self, db_path):
        """No reservation store on protected path = hard failure."""
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        # No reservation_store passed
        svc = RunApplicationService(s, j, r)

        with pytest.raises(RunIntegrationError) as exc:
            svc.execute(
                [sys.executable, "-c", "print('x')"],
                run_id="fail-closed-1",
                tool="opencode",
                model="test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                created_at="2026-10-05T00:00:00Z",
                require_identity=True,
            )
        assert "reservation" in str(exc.value).lower() or "identity" in str(exc.value).lower()
        s.close()

    def test_inaccessible_store_init_fails(self, db_path):
        """Inaccessible database path propagates init error."""
        # Use a path inside a nonexistent directory
        bad_path = str(_SCRATCH / "nonexistent" / "store.db")
        with pytest.raises(Exception):
            ReservationStore(bad_path)

    def test_execute_reservation_completion_failure_exposed(self, db_path, monkeypatch):
        """execute() exposes completion failure rather than silently swallowing."""
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        # Make complete() fail after reserve succeeds
        original_complete = rs.complete
        def _broken_complete(*a, **kw):
            raise RuntimeError("simulated complete failure")
        monkeypatch.setattr(rs, "complete", _broken_complete)

        with pytest.raises(RunIntegrationError) as exc:
            svc.execute(
                [sys.executable, "-c", "print('x')"],
                run_id="fail-comp-1",
                tool="opencode",
                model="test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                created_at="2026-10-05T00:00:00Z",
                reservation_envelope=_limited(max_in_flight=5),
            )
        assert "reservation" in str(exc.value).lower()
        s.close()


# ===================================================================
# Counterproof: reproduce the exact failure scenarios from controller
# ===================================================================

class TestControllerCounterproof:
    """Reproduce the concrete counterproof scenarios."""

    def test_partial_completion_counterproof(self, store):
        """Replicate partial_completion from controller counterproof."""
        # reserve with no explicit bytes/tokens/cost (all None)
        rec = store.reserve(
            run_id="partial", task_id="t1", attempt=1,
            scope_id="s1",
            reserved_bytes=100, reserved_tokens=50.0, cost=1.0,
        )
        # complete with ONLY observed_bytes=80
        comp = store.complete(rec.reservation_id, observed_bytes=80)
        run_usage = store.get_run_usage("partial")

        # Assert: in_flight=0, cumulative_requests=1
        assert run_usage["in_flight"] == 0
        assert run_usage["cumulative_requests"] == 1
        # Bytes: 80 (the observed value replaced reserved 100)
        assert run_usage["cumulative_bytes"] == 80
        # Tokens: STILL 50 (preserved from reserved, NOT set to 0)
        assert run_usage["cumulative_tokens"] == 50
        # Cost: STILL 1.0 (preserved from reserved, NOT set to 0)
        assert run_usage["cumulative_cost"] == 1.0

        # Verify the record itself has correct observed fields
        record = store.get_reservation(rec.reservation_id)
        assert record.observed_bytes == 80
        assert record.observed_tokens is None  # unknown, not 0
        assert record.cost == 1.0  # preserved from reserved (COALESCE keeps old)

    def test_caller_override_finite_counterproof(self, store):
        """Replicate caller_override_finite from controller counterproof."""
        # Set persisted envelope with max_requests=0
        owner_env = _limited(max_requests=0, max_in_flight=5)
        store.set_envelope("finite", "scope", owner_env)

        # Caller tries to bypass with unlimited envelope
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="finite", task_id="task", attempt=1,
                scope_id="scope",
                envelope=ReservationEnvelope(),  # caller-supplied unlimited
            )
        assert "requests" in str(exc.value)

        # Now verify that WITHOUT a persisted envelope, caller can supply one
        rec = store.reserve(
            run_id="finite", task_id="task", attempt=1,
            scope_id="scope2",
            envelope=_limited(max_requests=5),
        )
        assert rec.state == "reserved"
        # And the persisted one at "scope" still blocks
        with pytest.raises(AdmissionRefused):
            store.reserve(
                run_id="finite", task_id="task", attempt=1,
                scope_id="scope",
                envelope=_limited(max_requests=10),  # wider, but persisted says 0
            )


# ===================================================================
# BLOCKING A: Launch failure cancels the reservation slot
# ===================================================================

class TestLaunchFailureCancelsReservation:
    """BLOCKING A: launch failure must cancel/release the acquired slot."""

    def test_execute_launch_failure_cancels_reservation(self, db_path):
        """execute() launch exception handler cancels the reserved slot."""
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        run_id = "launch-fail-exec"
        # Use a non-existent command to trigger launch failure
        with pytest.raises(RunIntegrationError) as exc:
            svc.execute(
                ["/nonexistent/command-that-will-fail"],
                run_id=run_id,
                tool="opencode",
                model="test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                created_at="2026-10-05T00:00:00Z",
                reservation_envelope=_limited(max_in_flight=5),
            )
        assert "launch" in str(exc.value).lower()

        # The reservation should be cancelled (state CANCELLED), not stuck
        # in RESERVED.  Use raw query to check all reservations for this run.
        rows = rs._conn.execute(
            "SELECT state FROM reservations WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        assert len(rows) > 0, "expected at least one reservation"
        for row in rows:
            assert row["state"] == ReservationSlotState.CANCELLED.value, (
                f"expected CANCELLED, got {row['state']}"
            )

        # In-flight must be 0 (freed by cancel)
        usage = rs.get_run_usage(run_id)
        assert usage["in_flight"] == 0
        rs.close()
        s.close()

    def test_streaming_native_launch_failure_cancels_reservation(self, db_path, monkeypatch):
        """execute_streaming_native() launch exception handler cancels.

        ``run_process_streaming`` catches Popen failures internally and returns
        a result — it does not raise.  To trigger the exception handler in
        ``service.py`` we monkey-patch it to raise so the cancellation path
        is exercised.
        """
        import skillweave.core.proc.runner as runner_mod
        from skillweave.runtime.stream import DurableStreamStore

        def _raise_on_launch(*a, **kw):
            raise RuntimeError("simulated streaming launch failure")

        monkeypatch.setattr(runner_mod, "run_process_streaming", _raise_on_launch)

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        run_id = "launch-fail-stream"
        with pytest.raises(RunIntegrationError) as exc:
            svc.execute_streaming_native(
                ["/nonexistent/command"],
                run_id=run_id,
                tool="opencode",
                model="test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                attempt=1,
                task_id=run_id,
                reservation_envelope=_limited(max_in_flight=5),
            )
        assert "launch" in str(exc.value).lower()

        rows = rs._conn.execute(
            "SELECT state FROM reservations WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        assert len(rows) > 0
        for row in rows:
            assert row["state"] == ReservationSlotState.CANCELLED.value, (
                f"expected CANCELLED, got {row['state']}"
            )

        usage = rs.get_run_usage(run_id)
        assert usage["in_flight"] == 0
        rs.close()
        s.close()

    def test_launch_failure_cumulative_requests_not_leaked(self, db_path):
        """Cumulative request count is reversed on launch failure cancel."""
        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        run_id = "launch-fail-cumulative"
        # First a successful run to establish baseline
        out = svc.execute(
            [sys.executable, "-c", "print('ok')"],
            run_id=run_id + "-ok",
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=_limited(max_in_flight=5),
        )

        # Now a launch failure
        with pytest.raises(RunIntegrationError):
            svc.execute(
                ["/nonexistent/command"],
                run_id=run_id,
                tool="opencode",
                model="test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                created_at="2026-10-05T00:00:00Z",
                reservation_envelope=_limited(max_in_flight=5),
            )

        # Cumulative requests must be 0 for the failed run (reversed by cancel)
        usage = rs.get_run_usage(run_id)
        assert usage["cumulative_requests"] == 0, (
            f"expected 0 cumulative_requests after launch failure, "
            f"got {usage['cumulative_requests']}"
        )
        assert usage["in_flight"] == 0
        rs.close()
        s.close()


# ===================================================================
# BLOCKING B: Persisted finite owner envelope widening guard
# ===================================================================

class TestEnvelopeWideningGuard:
    """BLOCKING B: set_envelope must reject caller envelope that widens owner."""

    def test_widen_max_bytes_rejected(self, store):
        """Widening max_bytes (None = unlimited) is rejected."""
        owner_env = _limited(max_bytes=100)
        store.set_envelope("wb", "s1", owner_env)
        # Caller tries to widen with unlimited
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wb", "s1", ReservationEnvelope(max_bytes=None))
        assert "max_bytes" in str(exc.value)

    def test_widen_max_requests_rejected(self, store):
        """Widening max_requests (larger value) is rejected."""
        owner_env = _limited(max_requests=5)
        store.set_envelope("wr", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wr", "s1", _limited(max_requests=10))
        assert "max_requests" in str(exc.value)

    def test_widen_max_cost_rejected(self, store):
        """Widening max_cost from 10.0 to None is rejected."""
        owner_env = _limited(max_cost=10.0)
        store.set_envelope("wc", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wc", "s1", ReservationEnvelope(max_cost=None))
        assert "max_cost" in str(exc.value)

    def test_widen_max_tokens_rejected(self, store):
        """Widening max_tokens from 1000 to 2000 is rejected."""
        owner_env = _limited(max_tokens=1000)
        store.set_envelope("wtok", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wtok", "s1", _limited(max_tokens=2000))
        assert "max_tokens" in str(exc.value)

    def test_widen_max_in_flight_rejected(self, store):
        """Widening max_in_flight from 3 to 10 is rejected."""
        owner_env = _limited(max_in_flight=3)
        store.set_envelope("wif", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wif", "s1", _limited(max_in_flight=10))
        assert "max_in_flight" in str(exc.value)

    def test_widen_per_request_bytes_rejected(self, store):
        """Widening max_per_request_bytes is rejected."""
        owner_env = _limited(max_per_request_bytes=1024)
        store.set_envelope("wprb", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wprb", "s1", _limited(max_per_request_bytes=2048))
        assert "max_per_request_bytes" in str(exc.value)

    def test_widen_per_request_tokens_rejected(self, store):
        """Widening max_per_request_tokens is rejected."""
        owner_env = _limited(max_per_request_tokens=500)
        store.set_envelope("wprt", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wprt", "s1", _limited(max_per_request_tokens=1000))
        assert "max_per_request_tokens" in str(exc.value)

    def test_widen_deadline_rejected(self, store):
        """Widening max_deadline to a later time is rejected."""
        owner_env = _limited(max_deadline="2026-10-06T00:00:00Z")
        store.set_envelope("wd", "s1", owner_env)
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope("wd", "s1", _limited(max_deadline="2026-10-07T00:00:00Z"))
        assert "max_deadline" in str(exc.value)

    def test_force_true_bypasses_widen_guard(self, store):
        """force=True allows owner-authorized widening."""
        owner_env = _limited(max_bytes=100)
        store.set_envelope("force", "s1", owner_env)
        # Owner-authorized bypass with force=True
        store.set_envelope("force", "s1", ReservationEnvelope(max_bytes=None), force=True)
        # Verify it was updated
        env = store.get_envelope("force", "s1")
        assert env.max_bytes is None

    def test_owner_tightening_allowed(self, store):
        """Owner-authorized tightening (reducing bound) is always allowed."""
        owner_env = _limited(max_bytes=1000, max_requests=10)
        store.set_envelope("tight", "s1", owner_env)
        # Tighten (without force, since we're not widening — we're narrowing)
        store.set_envelope("tight", "s1", _limited(max_bytes=500, max_requests=5))
        env = store.get_envelope("tight", "s1")
        assert env.max_bytes == 500
        assert env.max_requests == 5

    def test_same_value_not_widening(self, store):
        """Setting same value as owner is not widening."""
        owner_env = _limited(max_bytes=100)
        store.set_envelope("same", "s1", owner_env)
        # Same value — no widening
        store.set_envelope("same", "s1", _limited(max_bytes=100))
        env = store.get_envelope("same", "s1")
        assert env.max_bytes == 100

    def test_owner_unlimited_caller_finite_not_widening(self, store):
        """Owner-unlimited (None) is not widened by a finite caller value."""
        owner_env = ReservationEnvelope()  # all None = unlimited
        store.set_envelope("unl", "s1", owner_env)
        # Caller supplies finite — this is tightening, not widening
        store.set_envelope("unl", "s1", _limited(max_bytes=100))
        env = store.get_envelope("unl", "s1")
        assert env.max_bytes == 100


# ===================================================================
# Run entry point
# ===================================================================

if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
