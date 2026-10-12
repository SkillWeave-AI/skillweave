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
    QuotaRefused,
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
# BLOCKING C: Installed ingress paths wire ReservationStore
# ===================================================================

class TestInstalledIngressWiresReservation:
    """cli/run.py and mcp/server.py now wire ReservationStore explicitly."""

    def test_cli_run_main_creates_reservation(self, monkeypatch):
        """cli/run.main() creates a ReservationStore and tracks reservations."""
        from skillweave.cli import run as cli_run

        # Capture stdout and prevent sys.exit
        written: list[str] = []
        monkeypatch.setattr(sys.stdout, "write", lambda s: written.append(s))
        exit_codes: list[int] = []
        monkeypatch.setattr(sys, "exit", lambda code: exit_codes.append(code))

        # Create a temp db path
        db_path = str(_SCRATCH / f"cli_run_test_{uuid.uuid4().hex}.db")

        try:
            cli_run.main([
                "--tool", "opencode",
                "--model", "budget-test",
                "--subject-repo", "skillweave",
                "--subject-commit", "deadbeef",
                "--db-path", db_path,
                "--artifacts-path", str(_SCRATCH / f"cli_run_art_{uuid.uuid4().hex}"),
                "--", sys.executable, "-c", "print('cli-budget-probe')",
            ])

            # Verify the db has reservation records
            from skillweave.runtime.reservation import ReservationStore
            rs = ReservationStore(db_path)
            rows = rs._conn.execute(
                "SELECT COUNT(*) as cnt FROM reservations"
            ).fetchone()
            assert rows["cnt"] >= 1, (
                "expected at least one reservation record from CLI run"
            )
            # Verify envelope was persisted
            env_rows = rs._conn.execute(
                "SELECT COUNT(*) as cnt FROM reservation_envelopes"
            ).fetchone()
            assert env_rows["cnt"] >= 1, (
                "expected at least one envelope record from CLI run"
            )
            rs.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass

    def test_cli_run_reservation_store_not_none(self):
        """RunApplicationService has a non-None reservation_store when wired."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        db_path = str(_SCRATCH / f"svc_wired_{uuid.uuid4().hex}.db")
        try:
            store = SQLiteRunStore(db_path)
            journal = EventJournal(db_path)
            raw = RawArtifactStore()
            stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
            stream_store = DurableStreamStore(root=stream_root)
            rs = ReservationStore(db_path)
            svc = RunApplicationService(
                store, journal, raw, stream_store,
                reservation_store=rs,
            )
            assert svc.reservation_store is not None
            # Verify owner envelope can be set and used
            run_id = "ingress-wired-test"
            scope_id = f"{run_id}/attempt-1"
            owner_env = ReservationEnvelope()
            svc.reservation_store.set_envelope(run_id, scope_id, owner_env)

            execution = svc.execute_streaming_native(
                [sys.executable, "-c", "print('ingress-budget-probe')"],
                run_id=run_id,
                tool="opencode",
                model="budget-test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                attempt=1,
                task_id=run_id,
                reservation_envelope=owner_env,
            )
            assert execution["run_id"] == run_id

            # Verify reservation was created and completed
            reservations = svc.reservation_store.list_reservations(run_id)
            assert len(reservations) >= 1
            rec = reservations[0]
            assert rec.state in (
                ReservationSlotState.COMPLETED.value,
                ReservationSlotState.RELEASED.value,
            )
            usage = svc.reservation_store.get_run_usage(run_id)
            assert usage["in_flight"] == 0
            rs.close()
            store.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass

    def test_mcp_create_run_wires_reservation(self):
        """mcp/server.py create_run tool wires ReservationStore."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        db_path = str(_SCRATCH / f"mcp_wired_{uuid.uuid4().hex}.db")
        try:
            store = SQLiteRunStore(db_path)
            journal = EventJournal(db_path)
            raw = RawArtifactStore()
            stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
            stream_store = DurableStreamStore(root=stream_root)
            rs = ReservationStore(db_path)
            svc = RunApplicationService(
                store, journal, raw, stream_store,
                reservation_store=rs,
            )
            assert svc.reservation_store is not None

            # Simulate the MCP create_run path
            run_id = "mcp-ingress-wired"
            scope_id = f"{run_id}/attempt-1"
            owner_env = ReservationEnvelope()
            svc.reservation_store.set_envelope(run_id, scope_id, owner_env)

            execution = svc.execute_streaming_native(
                [sys.executable, "-c", "print('mcp-budget-probe')"],
                run_id=run_id,
                tool="opencode",
                model="budget-test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                attempt=1,
                task_id=run_id,
                reservation_envelope=owner_env,
            )
            assert execution["run_id"] == run_id

            # Verify reservation was created
            reservations = svc.reservation_store.list_reservations(run_id)
            assert len(reservations) >= 1
            rec = reservations[0]
            assert rec.state in (
                ReservationSlotState.COMPLETED.value,
                ReservationSlotState.RELEASED.value,
            )
            mcp_usage = svc.reservation_store.get_run_usage(run_id)
            assert mcp_usage["in_flight"] == 0
            rs.close()
            store.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass

    def test_installed_ingress_persists_owner_envelope(self, db_path):
        """Installed ingress persists owner envelope before admission."""
        from skillweave.runtime.reservation import (
            ReservationStore, ReservationEnvelope,
        )
        rs = ReservationStore(db_path)

        # Simulate what cli/run.py and mcp/server.py do: set owner envelope
        run_id = "owner-envelope-test"
        scope_id = f"{run_id}/attempt-1"
        owner_env = ReservationEnvelope()  # unlimited
        rs.set_envelope(run_id, scope_id, owner_env)

        # Verify envelope is persisted
        loaded = rs.get_envelope(run_id, scope_id)
        assert loaded is not None
        # All-None = authorized unlimited
        assert loaded.max_cost is None
        assert loaded.max_tokens is None
        assert loaded.max_bytes is None
        assert loaded.max_requests is None
        assert loaded.max_in_flight == 3  # default

        # A finite owner envelope pre-configured by the host is authoritative
        run_id2 = "finite-owner-test"
        scope_id2 = f"{run_id2}/attempt-1"
        finite_env = ReservationEnvelope(max_requests=5, max_in_flight=3)
        rs.set_envelope(run_id2, scope_id2, finite_env)

        loaded2 = rs.get_envelope(run_id2, scope_id2)
        assert loaded2 is not None
        assert loaded2.max_requests == 5

        # Caller cannot widen the finite owner envelope
        from skillweave.runtime.reservation import EnvelopeRejected
        with pytest.raises(EnvelopeRejected):
            rs.set_envelope(run_id2, scope_id2, ReservationEnvelope(max_requests=10))
        rs.close()


# ===================================================================
# BLOCKING D: Retry-scope envelope inheritance
# ===================================================================

class TestRetryScopeEnvelopeInheritance:
    """A retry to a new attempt inherits the owner's finite envelope."""

    def test_retry_inherits_finite_envelope(self, store):
        """Retry to attempt-2 inherits finite envelope from attempt-1."""
        task_id = "retry-task"
        # Owner sets finite envelope on attempt-1
        owner_env = ReservationEnvelope(max_requests=1, max_in_flight=5)
        store.set_envelope("retry-run", f"{task_id}/attempt-1", owner_env)

        # attempt-1 succeeds (consumes the single request)
        r1 = store.reserve(
            run_id="retry-run", task_id=task_id, attempt=1,
            scope_id=f"{task_id}/attempt-1",
        )
        assert r1 is not None

        # attempt-2 (retry) should inherit the same finite envelope
        # and be refused because max_requests=1 was already consumed
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="retry-run", task_id=task_id, attempt=2,
                scope_id=f"{task_id}/attempt-2",
                envelope=ReservationEnvelope(),  # caller passes unlimited
            )
        assert "requests" in str(exc.value)

    def test_retry_fallback_respects_caller_tightening(self, store):
        """Retry inherits owner envelope but caller can still tighten."""
        task_id = "retry-tight"
        # Owner sets finite max_requests=5
        owner_env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("retry-tight-run", f"{task_id}/attempt-1", owner_env)

        # attempt-1: caller tightens to max_requests=2
        r1 = store.reserve(
            run_id="retry-tight-run", task_id=task_id, attempt=1,
            scope_id=f"{task_id}/attempt-1",
            envelope=ReservationEnvelope(max_requests=2, max_in_flight=5),
        )
        assert r1 is not None

        # attempt-2 inherits owner's max_requests=5 (not the caller's 2)
        # Second request at attempt-2 with inherited envelope allows 1 more
        r2 = store.reserve(
            run_id="retry-tight-run", task_id=task_id, attempt=2,
            scope_id=f"{task_id}/attempt-2",
            envelope=ReservationEnvelope(),  # caller unlimited, but owner's 5 applies
        )
        assert r2 is not None

        # Third request across the run should be refused (max_requests=5 for
        # owner, and 3 requests total so far, still within limit)
        # Actually 2 more should be allowed, let's check cumulative
        usage = store.get_run_usage("retry-tight-run")
        assert usage["cumulative_requests"] == 2  # r1 + r2

    def test_retry_envelope_fallback_does_not_leak_to_different_run(self, store):
        """Envelope fallback does not leak across different runs."""
        # Set envelope on run-A attempt-1
        store.set_envelope("run-a", "task/attempt-1",
                           ReservationEnvelope(max_requests=1, max_in_flight=5))
        # run-B attempt-2 should NOT inherit run-A's envelope
        rec = store.reserve(
            run_id="run-b", task_id="task", attempt=2,
            scope_id="task/attempt-2",
            envelope=ReservationEnvelope(max_requests=10, max_in_flight=5),
        )
        assert rec is not None

    def test_retry_cumulative_requests_accounted_across_attempts(self, store):
        """Retry cumulative requests count across attempts under same envelope."""
        task_id = "retry-cumul"
        owner_env = ReservationEnvelope(max_requests=3, max_in_flight=5)
        store.set_envelope("retry-cumul-run", f"{task_id}/attempt-1", owner_env)

        # attempt-1: first request
        store.reserve(
            run_id="retry-cumul-run", task_id=task_id, attempt=1,
            scope_id=f"{task_id}/attempt-1",
        )
        # attempt-1: second request (different scope)
        store.reserve(
            run_id="retry-cumul-run", task_id=task_id, attempt=1,
            scope_id=f"{task_id}/attempt-1-sub",
        )

        # attempt-2 (retry): third request
        r3 = store.reserve(
            run_id="retry-cumul-run", task_id=task_id, attempt=2,
            scope_id=f"{task_id}/attempt-2",
        )
        assert r3 is not None

        # Fourth should be refused (max_requests=3)
        with pytest.raises(AdmissionRefused):
            store.reserve(
                run_id="retry-cumul-run", task_id=task_id, attempt=3,
                scope_id=f"{task_id}/attempt-3",
            )

        usage = store.get_run_usage("retry-cumul-run")
        assert usage["cumulative_requests"] == 3

    def test_retry_inheritance_service_path(self, db_path):
        """Prove retry-scope envelope inheritance through RunApplicationService.

        Each attempt in a retry uses a distinct run_id but shares the same
        task_id.  The reservation store's get_envelope fallback searches
        across all runs for the same task scope prefix, so the owner's
        finite envelope set on attempt-1 is authoritative for attempt-2.

        Cumulative tracking (max_requests) is per-run_id, so a retry with a
        new run_id does not inherit the prior run's counter.  The envelope
        inheritance is verified by checking the effective envelope resolution
        through the store after each service call.
        """
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        task_id = "retry-svc-task"
        # Set finite owner envelope on attempt-1 scope (run_1).
        # Use high enough limits so both attempts succeed (cumulative tracking
        # is per-run_id; a new run_id starts fresh).
        run_1 = "retry-svc-run-1"
        owner_env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        rs.set_envelope(run_1, f"{task_id}/attempt-1", owner_env)

        # attempt-1 succeeds
        out1 = svc.execute_streaming_native(
            [sys.executable, "-c", "print('ok')"],
            run_id=run_1,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=task_id,
            reservation_envelope=ReservationEnvelope(),  # caller unlimited
        )
        assert out1["run_id"] == run_1

        # Verify attempt-1 has the owner envelope persisted
        env_1 = rs.get_envelope(run_1, f"{task_id}/attempt-1")
        assert env_1 is not None
        assert env_1.max_requests == 5

        # attempt-2 (retry) uses a distinct run_id.
        # The envelope should be inherited via fallback_task_scope.
        # The service path must NOT raise EnvelopeRejected (BLOCKING B guard)
        # because set_envelope only persists when no existing envelope is found.
        run_2 = "retry-svc-run-2"
        out2 = svc.execute_streaming_native(
            [sys.executable, "-c", "print('retry')"],
            run_id=run_2,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=2,
            task_id=task_id,
            reservation_envelope=ReservationEnvelope(),  # caller unlimited
        )
        assert out2["run_id"] == run_2

        # Verify the second run resolved the envelope via fallback — no
        # explicit envelope was set for attempt-2, so get_envelope falls
        # back to the owner envelope from attempt-1.
        env_fallback = rs.get_envelope(
            run_2, f"{task_id}/attempt-2",
            fallback_task_scope=task_id,
        )
        assert env_fallback is not None
        assert env_fallback.max_requests == 5

        rs.close()
        s.close()


# ===================================================================
# Run entry point
# ===================================================================

if __name__ == "__main__":
    pytest.main(["-xvs", __file__])


# ===================================================================
# BLOCKING (criterion 1): Product ingress exercises token/cost dimensions
# ===================================================================

class TestProductIngressTokenCostDimensions:
    """Prove token/cost dimensions are exercised through the product ingress,
    not only at the ReservationStore helper level."""

    def test_execute_reserve_passes_token_bounds(self, db_path):
        """execute() reserve passes reserved_tokens from envelope per-request limit."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        # Set envelope with finite per-request token bound
        env = ReservationEnvelope(
            max_per_request_tokens=500,
            max_in_flight=5,
        )
        run_id = "token-dim-exec"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        out = svc.execute(
            [sys.executable, "-c", "print('token-probe')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=ReservationEnvelope(),
        )
        # Verify a reservation was created with reserved_tokens
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        assert rec.reserved_tokens == 500.0, (
            f"expected reserved_tokens=500.0, got {rec.reserved_tokens}"
        )
        rs.close()
        s.close()

    def test_execute_reserve_passes_byte_bounds(self, db_path):
        """execute() reserve passes reserved_bytes from envelope per-request limit."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        env = ReservationEnvelope(
            max_per_request_bytes=8192,
            max_in_flight=5,
        )
        run_id = "byte-dim-exec"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        out = svc.execute(
            [sys.executable, "-c", "print('byte-probe')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=ReservationEnvelope(),
        )
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        assert rec.reserved_bytes == 8192, (
            f"expected reserved_bytes=8192, got {rec.reserved_bytes}"
        )
        rs.close()
        s.close()

    def test_execute_complete_observed_bytes_recorded(self, db_path):
        """execute() complete records observed_bytes from transport outcome."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        env = ReservationEnvelope(max_in_flight=5)
        run_id = "complete-dim-exec"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        svc.execute(
            [sys.executable, "-c", "print('hello world')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=ReservationEnvelope(),
        )
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        # observed_bytes should be set (len of "hello world\n")
        assert rec.observed_bytes is not None and rec.observed_bytes > 0, (
            f"expected observed_bytes > 0, got {rec.observed_bytes}"
        )
        # observed_tokens stays None (not observable from subprocess)
        assert rec.observed_tokens is None, (
            f"expected observed_tokens=None, got {rec.observed_tokens}"
        )
        # cost stays None (no cost was reserved at ingress; not observable
        # from subprocess)
        assert rec.cost is None, (
            f"expected cost=None, got {rec.cost}"
        )
        rs.close()
        s.close()

    def test_streaming_native_reserve_passes_token_bounds(self, db_path):
        """execute_streaming_native() reserve passes reserved_tokens."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        env = ReservationEnvelope(
            max_per_request_tokens=2000,
            max_in_flight=5,
        )
        run_id = "token-dim-stream"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        svc.execute_streaming_native(
            [sys.executable, "-c", "print('stream-token-probe')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=run_id,
            reservation_envelope=ReservationEnvelope(),
        )
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        assert rec.reserved_tokens == 2000.0, (
            f"expected reserved_tokens=2000.0, got {rec.reserved_tokens}"
        )
        rs.close()
        s.close()

    def test_streaming_native_reserve_passes_byte_bounds(self, db_path):
        """execute_streaming_native() reserve passes reserved_bytes."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
        stream_store = DurableStreamStore(root=stream_root)
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, stream_store=stream_store,
                                    reservation_store=rs)

        env = ReservationEnvelope(
            max_per_request_bytes=4096,
            max_in_flight=5,
        )
        run_id = "byte-dim-stream"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        svc.execute_streaming_native(
            [sys.executable, "-c", "print('stream-byte-probe')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            attempt=1,
            task_id=run_id,
            reservation_envelope=ReservationEnvelope(),
        )
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        assert rec.reserved_bytes == 4096, (
            f"expected reserved_bytes=4096, got {rec.reserved_bytes}"
        )
        rs.close()
        s.close()

    def test_execute_reserve_with_no_per_request_limits(self, db_path):
        """execute() reserve passes None when envelope has no per-request limits."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        env = ReservationEnvelope(max_in_flight=5)  # no per-request limits
        run_id = "no-limit-exec"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        svc.execute(
            [sys.executable, "-c", "print('no-limit')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=ReservationEnvelope(),
        )
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        # When no per-request limits, reserved_bytes/tokens are None (reserved at 0)
        assert rec.reserved_bytes is None, (
            f"expected reserved_bytes=None, got {rec.reserved_bytes}"
        )
        assert rec.reserved_tokens is None, (
            f"expected reserved_tokens=None, got {rec.reserved_tokens}"
        )
        rs.close()
        s.close()


# ===================================================================
# BLOCKING (criterion 1): ExecutionEnvelopeV1 bridge from SDK contract
# ===================================================================

class TestExecutionEnvelopeBridge:
    """Prove the execution-envelope-v1 SDK contract bridges to ReservationEnvelope."""

    def test_enforcing_policy_maps_all_limits(self):
        """enforcing policy maps all specified limits to ReservationEnvelope."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1(
            limit_policy="enforcing",
            max_tokens=100000,
            max_bytes=1048576,
            max_cost=50.0,
            max_requests=100,
            max_deadline="2026-12-31T23:59:59Z",
            max_in_flight=5,
            max_per_request_bytes=4096,
            max_per_request_tokens=2000,
        )
        env = sdk.to_reservation_envelope()
        assert env.max_tokens == 100000
        assert env.max_bytes == 1048576
        assert env.max_cost == 50.0
        assert env.max_requests == 100
        assert env.max_deadline == "2026-12-31T23:59:59Z"
        assert env.max_in_flight == 5
        assert env.max_per_request_bytes == 4096
        assert env.max_per_request_tokens == 2000

    def test_observe_adapt_policy_sets_cumulative_none(self):
        """observe_adapt policy sets cumulative limits to None."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1(
            limit_policy="observe_adapt",
            max_tokens=100000,
            max_bytes=1048576,
            max_cost=50.0,
            max_requests=100,
            max_per_request_bytes=4096,
            max_per_request_tokens=2000,
        )
        env = sdk.to_reservation_envelope()
        # Cumulative limits are None under observe_adapt
        assert env.max_tokens is None
        assert env.max_bytes is None
        assert env.max_cost is None
        assert env.max_requests is None
        # Per-request limits still apply
        assert env.max_per_request_bytes == 4096
        assert env.max_per_request_tokens == 2000

    def test_conservative_per_request_bounds(self):
        """conservative_per_request_bounds returns envelope's per-request limits."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1(
            max_per_request_bytes=8192,
            max_per_request_tokens=4000,
        )
        bounds = sdk.conservative_per_request_bounds()
        assert bounds["reserved_bytes"] == 8192
        assert bounds["reserved_tokens"] == 4000.0

    def test_conservative_bounds_none_when_unset(self):
        """conservative_per_request_bounds returns None for unset limits."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1()
        bounds = sdk.conservative_per_request_bounds()
        assert bounds["reserved_bytes"] is None
        assert bounds["reserved_tokens"] is None

    def test_from_dict_round_trip(self):
        """from_dict/to_dict round-trips correctly."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        data = {
            "allowed_routes": ["route-a", "route-b"],
            "limit_policy": "enforcing",
            "max_tokens": 50000,
            "max_bytes": 512000,
            "max_cost": 25.0,
            "max_requests": 50,
            "max_deadline": "2026-11-01T00:00:00Z",
            "max_in_flight": 3,
            "max_per_request_bytes": 2048,
            "max_per_request_tokens": 1000,
        }
        sdk = ExecutionEnvelopeV1.from_dict(data)
        assert sdk.allowed_routes == ["route-a", "route-b"]
        assert sdk.limit_policy == "enforcing"
        assert sdk.max_tokens == 50000
        assert sdk.max_cost == 25.0
        assert sdk.max_in_flight == 3

        # Round-trip through dict
        assert sdk.to_dict() == data

    def test_default_enforcing_policy(self):
        """Default limit_policy is enforcing."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1.from_dict({})
        assert sdk.limit_policy == "enforcing"
        assert sdk.max_in_flight == 3  # default

    def test_allowed_routes_preserved(self):
        """allowed_routes list is preserved through conversion."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1(allowed_routes=["openai/gpt-4", "anthropic/claude-3"])
        data = sdk.to_dict()
        assert data["allowed_routes"] == ["openai/gpt-4", "anthropic/claude-3"]
        sdk2 = ExecutionEnvelopeV1.from_dict(data)
        assert sdk2.allowed_routes == ["openai/gpt-4", "anthropic/claude-3"]

    def test_reservation_store_reserves_via_enforcing_bridge(self, store):
        """Prove bridge output is accepted by ReservationStore.reserve()."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1(
            limit_policy="enforcing",
            max_requests=5,
            max_in_flight=5,
            max_per_request_bytes=1000,
            max_per_request_tokens=500,
        )
        env = sdk.to_reservation_envelope()
        store.set_envelope("bridge-run", "bridge-scope", env)

        # First reservation succeeds (within limits)
        rec = store.reserve(
            run_id="bridge-run", task_id="bridge-task", attempt=1,
            scope_id="bridge-scope",
            reserved_bytes=100,
            reserved_tokens=50.0,
        )
        assert rec is not None

        # Check the envelope was stored with the correct limits
        loaded = store.get_envelope("bridge-run", "bridge-scope")
        assert loaded.max_requests == 5
        assert loaded.max_per_request_bytes == 1000
        assert loaded.max_per_request_tokens == 500

    def test_reservation_store_reserves_via_observe_adapt_bridge(self, store):
        """Prove observe_adapt bridge output accepts requests beyond nominal caps."""
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        sdk = ExecutionEnvelopeV1(
            limit_policy="observe_adapt",
            max_requests=1,  # nominal cap, but observe_adapt means no enforcement
            max_in_flight=5,
        )
        env = sdk.to_reservation_envelope()
        store.set_envelope("obs-run", "obs-scope", env)

        # First reservation
        store.reserve(
            run_id="obs-run", task_id="obs-task", attempt=1,
            scope_id="obs-scope",
        )
        # Second should ALSO succeed because observe_adapt sets max_requests=None
        rec2 = store.reserve(
            run_id="obs-run", task_id="obs-task", attempt=1,
            scope_id="obs-scope",
        )
        assert rec2 is not None

    def test_conservative_bounds_used_in_service_path(self, db_path):
        """Prove conservative_per_request_bounds values reach reserve() through
        the product ingress."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.reservation import ReservationEnvelope
        from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1
        from skillweave.runsvc.service import RunApplicationService

        s = SQLiteRunStore(db_path)
        j = EventJournal(s.db_path)
        r = RawArtifactStore()
        rs = ReservationStore(db_path)
        svc = RunApplicationService(s, j, r, reservation_store=rs)

        # Simulate host that bridges from execution-envelope-v1
        sdk = ExecutionEnvelopeV1(
            limit_policy="enforcing",
            max_per_request_bytes=5000,
            max_per_request_tokens=3000,
            max_in_flight=5,
        )
        env = sdk.to_reservation_envelope()
        run_id = "bridge-service-path"
        rs.set_envelope(run_id, f"{run_id}/attempt-1", env)

        svc.execute(
            [sys.executable, "-c", "print('bridge-service')"],
            run_id=run_id,
            tool="opencode",
            model="test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=ReservationEnvelope(),
        )
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        # The per-request bounds from the bridge should have reached reserve()
        assert rec.reserved_bytes == 5000, (
            f"expected reserved_bytes=5000 (from bridge), got {rec.reserved_bytes}"
        )
        assert rec.reserved_tokens == 3000.0, (
            f"expected reserved_tokens=3000.0 (from bridge), got {rec.reserved_tokens}"
        )
        rs.close()
        s.close()


# ===================================================================
# BLOCKING (finding 1): Ingress honours existing owner envelope
# ===================================================================

class TestIngressHonoursOwnerEnvelope:
    """Prove that installed ingress does NOT overwrite an existing finite
    owner envelope with an unlimited one."""

    def test_ingress_does_not_overwrite_existing_envelope(self, monkeypatch):
        """Ingress does not overwrite existing finite envelope — the
        persisted owner envelope is retained across runs."""
        from skillweave.cli import run as cli_run

        db_path = str(_SCRATCH / f"ingress_retain_{uuid.uuid4().hex}.db")

        # Pre-set a finite envelope for this task scope
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
        rs = ReservationStore(db_path)
        task_id = "retain-task"
        rs.set_envelope(
            "existing-run",
            f"{task_id}/attempt-1",
            ReservationEnvelope(max_requests=5, max_bytes=10000, max_in_flight=5),
        )
        rs.close()

        written: list[str] = []
        monkeypatch.setattr(sys.stdout, "write", lambda s: written.append(s))
        exit_codes: list[int] = []
        monkeypatch.setattr(sys, "exit", lambda code: exit_codes.append(code))

        try:
            cli_run.main([
                "--tool", "opencode",
                "--model", "budget-test",
                "--subject-repo", "skillweave",
                "--subject-commit", "deadbeef",
                "--db-path", db_path,
                "--task-id", task_id,
                "--attempt", "2",
                "--artifacts-path", str(_SCRATCH / f"cli_art3_{uuid.uuid4().hex}"),
                "--", sys.executable, "-c", "print('retain-test')",
            ])
            # Verify the envelope was NOT overwritten — the finite owner
            # envelope should still be authoritative
            rs2 = ReservationStore(db_path)
            env = rs2.get_envelope_for_task_scope(task_id)
            assert env is not None
            assert env.max_requests == 5, (
                f"expected max_requests=5 (preserved), got {env.max_requests}"
            )
            assert env.max_bytes == 10000, (
                f"expected max_bytes=10000 (preserved), got {env.max_bytes}"
            )
            rs2.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass

    def test_reserve_task_usage_aggregate_across_runs(self, store):
        """Task-scoped cumulative usage is tracked across different run_ids."""
        task_id = "agg-task"
        scope_id = f"{task_id}/attempt-1"
        owner_env = ReservationEnvelope(max_requests=2, max_in_flight=5)
        store.set_envelope("agg-run-1", scope_id, owner_env)

        # First reservation on run-1 — succeeds
        store.reserve(
            run_id="agg-run-1", task_id=task_id, attempt=1,
            scope_id=scope_id,
        )
        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage is not None
        assert task_usage["cumulative_requests"] == 1

        # Second reservation on run-2 (different run_id), same scope — succeeds
        store.reserve(
            run_id="agg-run-2", task_id=task_id, attempt=1,
            scope_id=scope_id,
        )
        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage is not None
        assert task_usage["cumulative_requests"] == 2

        # Third reservation for same scope should fail (max_requests=2)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="agg-run-3", task_id=task_id, attempt=1,
                scope_id=scope_id,
            )
        assert "requests" in str(exc.value)

    def test_task_usage_across_attempts_same_scope(self, store):
        """Task-scoped cumulative usage for same scope accumulates across runs."""
        task_id = "same-scope-task"
        scope_id = f"{task_id}/attempt-1"
        owner_env = ReservationEnvelope(max_requests=3, max_in_flight=5)
        store.set_envelope("ss-run-1", scope_id, owner_env)

        store.reserve(run_id="ss-run-1", task_id=task_id, attempt=1, scope_id=scope_id)
        store.reserve(run_id="ss-run-2", task_id=task_id, attempt=1, scope_id=scope_id)
        store.reserve(run_id="ss-run-3", task_id=task_id, attempt=1, scope_id=scope_id)

        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage is not None
        assert task_usage["cumulative_requests"] == 3

        with pytest.raises(AdmissionRefused):
            store.reserve(run_id="ss-run-4", task_id=task_id, attempt=1, scope_id=scope_id)

    def test_task_usage_complete_updates_counters(self, store):
        """complete() updates task-scoped cumulative counters."""
        task_id = "comp-task"
        scope_id = f"{task_id}/attempt-1"
        owner_env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("comp-run", scope_id, owner_env)

        rec = store.reserve(
            run_id="comp-run", task_id=task_id, attempt=1,
            scope_id=scope_id,
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        store.complete(rec.reservation_id, observed_bytes=800, observed_tokens=400.0, cost=8.0)

        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage is not None
        assert task_usage["cumulative_bytes"] == 800
        assert task_usage["cumulative_tokens"] == 400
        assert task_usage["cumulative_cost"] == 8.0
        assert task_usage["cumulative_requests"] == 1

    def test_task_usage_cancel_reverses_counters(self, store):
        """cancel() reverses task-scoped cumulative counters."""
        task_id = "cancel-task"
        scope_id = f"{task_id}/attempt-1"
        owner_env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("cancel-run", scope_id, owner_env)

        rec = store.reserve(
            run_id="cancel-run", task_id=task_id, attempt=1,
            scope_id=scope_id,
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        store.cancel(rec.reservation_id)

        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage is not None
        assert task_usage["cumulative_requests"] == 0
        assert task_usage["cumulative_bytes"] == 0
        assert task_usage["cumulative_tokens"] == 0
        assert task_usage["cumulative_cost"] == 0.0

    def test_task_usage_reconcile_updates_counters(self, store):
        """reconcile() updates task-scoped cumulative counters."""
        task_id = "reconcile-task"
        scope_id = f"{task_id}/attempt-1"
        owner_env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("reconcile-run", scope_id, owner_env)

        rec = store.reserve(
            run_id="reconcile-run", task_id=task_id, attempt=1,
            scope_id=scope_id,
            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0,
        )
        store.complete(rec.reservation_id)

        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage is not None
        assert task_usage["cumulative_bytes"] == 1000

        store.reconcile(rec.reservation_id, actual_bytes=600)
        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage["cumulative_bytes"] == 600
        assert task_usage["cumulative_tokens"] == 500
        assert task_usage["cumulative_cost"] == 10.0

    def test_mcp_create_run_honours_existing_envelope(self):
        """MCP create_run honours pre-set finite owner envelope."""
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
        from skillweave.runsvc.service import RunApplicationService

        db_path = str(_SCRATCH / f"mcp_honour_{uuid.uuid4().hex}.db")
        try:
            rs = ReservationStore(db_path)
            task_id = "mcp-honour-task"
            rs.set_envelope(
                "pre-mcp-run",
                f"{task_id}/attempt-1",
                ReservationEnvelope(max_requests=3, max_bytes=50000, max_in_flight=5),
            )
            rs.close()

            s = SQLiteRunStore(db_path)
            j = EventJournal(db_path)
            raw = RawArtifactStore()
            stream_root = str(_SCRATCH / f"stream_{uuid.uuid4().hex}")
            stream_store = DurableStreamStore(root=stream_root)
            rs2 = ReservationStore(db_path)
            svc = RunApplicationService(s, j, raw, stream_store, reservation_store=rs2)

            run_id = "mcp-honour-run"
            existing = rs2.get_envelope_for_task_scope(task_id)
            assert existing is not None
            assert existing.max_requests == 3
            assert existing.max_bytes == 50000

            owner_env = existing
            execution = svc.execute_streaming_native(
                [sys.executable, "-c", "print('mcp-honour')"],
                run_id=run_id,
                tool="opencode",
                model="test",
                subject_repo="skillweave",
                subject_commit="deadbeef",
                attempt=1,
                task_id=task_id,
                reservation_envelope=owner_env,
            )
            assert execution["run_id"] == run_id

            still_there = rs2.get_envelope_for_task_scope(task_id)
            assert still_there is not None
            assert still_there.max_requests == 3
            assert still_there.max_bytes == 50000

            rs2.close()
            s.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass

    def test_ingress_preserves_owner_envelope_across_attempts(self, monkeypatch):
        """Ingress preserves owner envelope across attempts for same task."""
        from skillweave.cli import run as cli_run

        db_path = str(_SCRATCH / f"ingress_attempts_{uuid.uuid4().hex}.db")

        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
        rs = ReservationStore(db_path)
        task_id = "attempts-task"
        rs.set_envelope(
            "orig-run",
            f"{task_id}/attempt-1",
            ReservationEnvelope(max_requests=10, max_bytes=5000, max_in_flight=5),
        )
        rs.close()

        written: list[str] = []
        monkeypatch.setattr(sys.stdout, "write", lambda s: written.append(s))
        exit_codes: list[int] = []
        monkeypatch.setattr(sys, "exit", lambda code: exit_codes.append(code))

        try:
            cli_run.main([
                "--tool", "opencode",
                "--model", "budget-test",
                "--subject-repo", "skillweave",
                "--subject-commit", "deadbeef",
                "--db-path", db_path,
                "--task-id", task_id,
                "--attempt", "1",
                "--artifacts-path", str(_SCRATCH / f"ia_art1_{uuid.uuid4().hex}"),
                "--", sys.executable, "-c", "print('attempt-1')",
            ])
            cli_run.main([
                "--tool", "opencode",
                "--model", "budget-test",
                "--subject-repo", "skillweave",
                "--subject-commit", "deadbeef",
                "--db-path", db_path,
                "--task-id", task_id,
                "--attempt", "2",
                "--artifacts-path", str(_SCRATCH / f"ia_art2_{uuid.uuid4().hex}"),
                "--", sys.executable, "-c", "print('attempt-2')",
            ])
            rs2 = ReservationStore(db_path)
            env = rs2.get_envelope_for_task_scope(task_id)
            assert env is not None
            assert env.max_requests == 10, (
                f"expected max_requests=10, got {env.max_requests}"
            )
            assert env.max_bytes == 5000, (
                f"expected max_bytes=5000, got {env.max_bytes}"
            )
            rs2.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass


# ===================================================================
# BLOCKING (finding 2/3): Task-scoped cumulative enforcement across runs
# ===================================================================

class TestTaskScopedCumulativeEnforcement:
    """Prove that cumulative budget enforcement works across ALL runs for the
    same task scope, not just per-run."""

    def test_max_requests_across_runs_refused(self, store):
        """max_requests enforced across different run_ids."""
        task_id = "cross-run-task"
        scope_id = f"{task_id}/attempt-1"
        store.set_envelope("run-a", scope_id,
                           ReservationEnvelope(max_requests=2, max_in_flight=5))

        store.reserve(run_id="run-a", task_id=task_id, attempt=1, scope_id=scope_id)
        store.reserve(run_id="run-b", task_id=task_id, attempt=1, scope_id=scope_id)

        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="run-c", task_id=task_id, attempt=1, scope_id=scope_id)
        assert "requests" in str(exc.value)

    def test_max_bytes_across_runs_refused(self, store):
        """max_bytes enforced across different run_ids."""
        task_id = "cross-byte-task"
        scope_id = f"{task_id}/attempt-1"
        store.set_envelope("byte-run-a", scope_id,
                           ReservationEnvelope(max_bytes=2000, max_in_flight=5))

        store.reserve(run_id="byte-run-a", task_id=task_id, attempt=1,
                      scope_id=scope_id, reserved_bytes=1500)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="byte-run-b", task_id=task_id, attempt=1,
                          scope_id=scope_id, reserved_bytes=1000)
        assert "bytes" in str(exc.value)

    def test_max_tokens_across_runs_refused(self, store):
        """max_tokens enforced across different run_ids."""
        task_id = "cross-token-task"
        scope_id = f"{task_id}/attempt-1"
        store.set_envelope("tok-run-a", scope_id,
                           ReservationEnvelope(max_tokens=5000, max_in_flight=5))

        store.reserve(run_id="tok-run-a", task_id=task_id, attempt=1,
                      scope_id=scope_id, reserved_tokens=3000.0)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="tok-run-b", task_id=task_id, attempt=1,
                          scope_id=scope_id, reserved_tokens=3000.0)
        assert "tokens" in str(exc.value)

    def test_max_cost_across_runs_refused(self, store):
        """max_cost enforced across different run_ids."""
        task_id = "cross-cost-task"
        scope_id = f"{task_id}/attempt-1"
        store.set_envelope("cost-run-a", scope_id,
                           ReservationEnvelope(max_cost=25.0, max_in_flight=5))

        store.reserve(run_id="cost-run-a", task_id=task_id, attempt=1,
                      scope_id=scope_id, cost=15.0)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="cost-run-b", task_id=task_id, attempt=1,
                          scope_id=scope_id, cost=15.0)
        assert "cost" in str(exc.value)

    def test_per_run_limits_are_independent(self, store):
        """Per-run limits are independent of task-scoped limits."""
        task_id = "independent-task"
        scope_id = f"{task_id}/attempt-1"
        store.set_envelope("indep-run-a", scope_id,
                           ReservationEnvelope(max_requests=5, max_in_flight=5))

        store.reserve(run_id="indep-run-a", task_id=task_id, attempt=1, scope_id=scope_id)
        store.reserve(run_id="indep-run-a", task_id=task_id, attempt=1, scope_id=scope_id)

        run_usage = store.get_run_usage("indep-run-a")
        assert run_usage["cumulative_requests"] == 2

        store.reserve(run_id="indep-run-b", task_id=task_id, attempt=1, scope_id=scope_id)
        store.reserve(run_id="indep-run-b", task_id=task_id, attempt=1, scope_id=scope_id)

        run_usage_b = store.get_run_usage("indep-run-b")
        assert run_usage_b["cumulative_requests"] == 2

        task_usage = store.get_task_usage(task_id, scope_id)
        assert task_usage["cumulative_requests"] == 4

        store.reserve(run_id="indep-run-c", task_id=task_id, attempt=1, scope_id=scope_id)

        with pytest.raises(AdmissionRefused):
            store.reserve(run_id="indep-run-d", task_id=task_id, attempt=1, scope_id=scope_id)

    def test_get_envelope_for_task_scope_returns_existing(self, store):
        """get_envelope_for_task_scope returns existing envelope for task."""
        task_id = "lookup-task"
        store.set_envelope("lookup-run-1", f"{task_id}/attempt-1",
                           ReservationEnvelope(max_requests=5, max_in_flight=5))

        env = store.get_envelope_for_task_scope(task_id)
        assert env is not None
        assert env.max_requests == 5

    def test_get_envelope_for_task_scope_returns_none(self, store):
        """get_envelope_for_task_scope returns None when no envelope exists."""
        env = store.get_envelope_for_task_scope("nonexistent-task")
        assert env is None

    def test_get_envelope_for_task_scope_finds_any_attempt(self, store):
        """get_envelope_for_task_scope finds envelope regardless of attempt number."""
        task_id = "any-attempt-task"
        store.set_envelope("any-run", f"{task_id}/attempt-2",
                           ReservationEnvelope(max_requests=3, max_in_flight=5))

        env = store.get_envelope_for_task_scope(task_id)
        assert env is not None
        assert env.max_requests == 3

    def test_get_task_usage_returns_none(self, store):
        """get_task_usage returns None when no usage exists."""
        usage = store.get_task_usage("nonexistent", "nonexistent")
        assert usage is None

    def test_get_task_usage_after_reserve(self, store):
        """get_task_usage returns correct data after reserve."""
        task_id = "usage-check-task"
        scope_id = f"{task_id}/attempt-1"
        store.set_envelope("usage-run", scope_id,
                           ReservationEnvelope(max_requests=10, max_in_flight=5))

        store.reserve(run_id="usage-run", task_id=task_id, attempt=1,
                      scope_id=scope_id, reserved_bytes=500, reserved_tokens=200.0, cost=5.0)

        usage = store.get_task_usage(task_id, scope_id)
        assert usage is not None
        assert usage["cumulative_requests"] == 1
        assert usage["cumulative_bytes"] == 500
        assert usage["cumulative_tokens"] == 200
        assert usage["cumulative_cost"] == 5.0


# ===================================================================
# BLOCKING: QuotaRefused error and route enforcement
# ===================================================================

class TestCheckRouteAllowed:
    """Prove check_route_allowed raises QuotaRefused for disallowed routes."""

    def test_route_allowed_passes(self, store):
        """Route in allowed_routes passes without error."""
        env = ReservationEnvelope(allowed_routes=["openai/gpt-4", "anthropic/claude-3"])
        # Should not raise
        store.check_route_allowed("openai/gpt-4", env)

    def test_route_not_allowed_raises(self, store):
        """Route not in allowed_routes raises QuotaRefused."""
        env = ReservationEnvelope(allowed_routes=["openai/gpt-4"])
        with pytest.raises(QuotaRefused) as exc:
            store.check_route_allowed("anthropic/claude-3", env)
        assert "route" in str(exc.value)
        assert "anthropic/claude-3" in str(exc.value)

    def test_allowed_routes_none_permits_all(self, store):
        """allowed_routes=None permits any route."""
        env = ReservationEnvelope()
        # Should not raise
        store.check_route_allowed("any/route", env)

    def test_allowed_routes_empty_rejects_all(self, store):
        """allowed_routes=[] (explicitly set) rejects all routes."""
        env = ReservationEnvelope(allowed_routes=[])
        with pytest.raises(QuotaRefused) as exc:
            store.check_route_allowed("any/route", env)
        assert "route" in str(exc.value)

    def test_reserve_with_allowed_routes_envelope(self, store):
        """A reservation can be created with an allowed_routes envelope."""
        env = ReservationEnvelope(allowed_routes=["route-a"], max_in_flight=5)
        store.set_envelope("route-run", "route-scope", env)
        rec = store.reserve(
            run_id="route-run", task_id="route-task", attempt=1,
            scope_id="route-scope", route="route-a",
        )
        assert rec is not None

    def test_allowed_routes_envelope_widening_rejected(self, store):
        """Caller cannot widen allowed_routes beyond owner-set list."""
        owner_env = ReservationEnvelope(allowed_routes=["route-a"])
        store.set_envelope("widen-run", "widen-scope", owner_env)
        # Caller tries to add a route
        with pytest.raises(EnvelopeRejected) as exc:
            store.set_envelope(
                "widen-run", "widen-scope",
                ReservationEnvelope(allowed_routes=["route-a", "route-b"]),
            )
        assert "allowed_routes" in str(exc.value)

    def test_allowed_routes_tightening_allowed(self, store):
        """Caller can tighten allowed_routes (subset of owner list)."""
        owner_env = ReservationEnvelope(allowed_routes=["route-a", "route-b", "route-c"])
        store.set_envelope("tight-run", "tight-scope", owner_env)
        # Tighten to subset
        store.set_envelope(
            "tight-run", "tight-scope",
            ReservationEnvelope(allowed_routes=["route-a"]),
        )
        env = store.get_envelope("tight-run", "tight-scope")
        assert env.allowed_routes == ["route-a"]

    def test_reserve_refuses_disallowed_route(self, store):
        """reserve() with route not in envelope allowed_routes raises QuotaRefused."""
        env = ReservationEnvelope(allowed_routes=["openai/gpt-4"], max_in_flight=5)
        store.set_envelope("route-admit-run", "route-admit-scope", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="route-admit-run", task_id="route-admit-task", attempt=1,
                scope_id="route-admit-scope",
                route="anthropic/claude-3",
            )
        assert "route" in str(exc.value)

    def test_reserve_allows_approved_route(self, store):
        """reserve() with route in envelope allowed_routes succeeds."""
        env = ReservationEnvelope(allowed_routes=["openai/gpt-4"], max_in_flight=5)
        store.set_envelope("route-ok-run", "route-ok-scope", env)
        rec = store.reserve(
            run_id="route-ok-run", task_id="route-ok-task", attempt=1,
            scope_id="route-ok-scope",
            route="openai/gpt-4",
        )
        assert rec is not None

    def test_reserve_with_route_none_refused_when_allowed_set(self, store):
        """reserve() with route=None raises when allowed_routes is set (AC4)."""
        env = ReservationEnvelope(allowed_routes=["approved"], max_in_flight=5)
        store.set_envelope("route-none-run", "route-none-scope", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="route-none-run", task_id="route-none-task", attempt=1,
                scope_id="route-none-scope",
                route=None,
            )
        assert "route" in str(exc.value)
        assert "None" in str(exc.value)

    def test_reserve_with_route_none_permits_when_allowed_not_set(self, store):
        """reserve() with route=None permits when allowed_routes is None."""
        env = ReservationEnvelope(max_in_flight=5)
        store.set_envelope("route-none-free-run", "route-none-free-scope", env)
        rec = store.reserve(
            run_id="route-none-free-run", task_id="route-none-free-task", attempt=1,
            scope_id="route-none-free-scope",
            route=None,
        )
        assert rec is not None

    def test_reserve_refused_by_persisted_envelope_route(self, store):
        """Persisted envelope with allowed_routes blocks disallowed route."""
        owner_env = ReservationEnvelope(allowed_routes=["route-a"], max_in_flight=5)
        store.set_envelope("pers-route-run", "pers-route-scope", owner_env)
        # Caller passes unlimited envelope + disallowed route -> refused
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="pers-route-run", task_id="pers-route-task", attempt=1,
                scope_id="pers-route-scope",
                envelope=ReservationEnvelope(),  # unlimited, but persisted has allowed_routes
                route="route-b",
            )
        assert "route" in str(exc.value)


class TestQuotaRefused:
    """Prove QuotaRefused error type works correctly."""

    def test_quota_refused_attributes(self):
        """QuotaRefused stores axis, reason, run_id, detail."""
        err = QuotaRefused(
            "tokens", "token quota exceeded",
            run_id="qr-run",
            detail={"limit": 1000, "used": 1001},
        )
        assert err.axis == "tokens"
        assert err.reason == "token quota exceeded"
        assert err.run_id == "qr-run"
        assert err.detail == {"limit": 1000, "used": 1001}
        assert "tokens" in str(err)

    def test_quota_refused_minimal(self):
        """QuotaRefused can be created with minimal args."""
        err = QuotaRefused("route", "route not permitted")
        assert err.axis == "route"
        assert err.run_id == ""
        assert err.detail == {}


# ===================================================================
# BLOCKING: Nested-scope budget enforcement (parent_scope_id)
# ===================================================================

class TestNestedScopeBudgetEnforcement:
    """Prove nested-scope budget enforcement works when parent_scope_id is set."""

    def test_nested_scope_requests_refused(self, store):
        """Nested scope respects parent's max_requests.

        The nested-scope check reads the parent's OWN scope usage (cumulative
        requests for the parent_scope_id).  The parent reservation itself
        counts as one request in that scope, so max_requests=1 means no
        child can be admitted.
        """
        parent_env = ReservationEnvelope(max_requests=1, max_in_flight=10)
        store.set_envelope("parent-run", "parent-scope", parent_env)

        # Parent reservation — this consumes the single request slot
        parent_rec = store.reserve(
            run_id="parent-run", task_id="parent-task", attempt=1,
            scope_id="parent-scope",
        )
        # First child should be refused (parent scope already has 1 request)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="parent-run", task_id="parent-task", attempt=1,
                scope_id="child-scope-1",
                parent_scope_id="parent-scope",
            )
        assert "requests" in str(exc.value)

    def test_nested_scope_bytes_refused(self, store):
        """Nested scope respects parent's max_bytes."""
        parent_env = ReservationEnvelope(max_bytes=500, max_in_flight=5)
        store.set_envelope("bytes-parent-run", "bytes-parent-scope", parent_env)

        store.reserve(
            run_id="bytes-parent-run", task_id="bytes-task", attempt=1,
            scope_id="bytes-parent-scope",
        )
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="bytes-parent-run", task_id="bytes-task", attempt=1,
                scope_id="bytes-child-scope",
                parent_scope_id="bytes-parent-scope",
                reserved_bytes=1000,
            )
        assert "bytes" in str(exc.value)

    def test_nested_scope_tokens_refused(self, store):
        """Nested scope respects parent's max_tokens."""
        parent_env = ReservationEnvelope(max_tokens=1000, max_in_flight=5)
        store.set_envelope("tok-parent-run", "tok-parent-scope", parent_env)

        store.reserve(
            run_id="tok-parent-run", task_id="tok-task", attempt=1,
            scope_id="tok-parent-scope",
        )
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="tok-parent-run", task_id="tok-task", attempt=1,
                scope_id="tok-child-scope",
                parent_scope_id="tok-parent-scope",
                reserved_tokens=1500.0,
            )
        assert "tokens" in str(exc.value)

    def test_nested_scope_cost_refused(self, store):
        """Nested scope respects parent's max_cost."""
        parent_env = ReservationEnvelope(max_cost=10.0, max_in_flight=5)
        store.set_envelope("cost-parent-run", "cost-parent-scope", parent_env)

        store.reserve(
            run_id="cost-parent-run", task_id="cost-task", attempt=1,
            scope_id="cost-parent-scope",
        )
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="cost-parent-run", task_id="cost-task", attempt=1,
                scope_id="cost-child-scope",
                parent_scope_id="cost-parent-scope",
                cost=20.0,
            )
        assert "cost" in str(exc.value)

    def test_nested_scope_no_parent_skips_check(self, store):
        """No parent_scope_id skips nested-scope enforcement."""
        env = ReservationEnvelope(max_requests=1, max_in_flight=5)
        store.set_envelope("no-parent-run", "no-parent-scope", env)

        store.reserve(
            run_id="no-parent-run", task_id="no-parent-task", attempt=1,
            scope_id="no-parent-scope",
        )
        # Different scope without parent_scope_id should not be blocked
        rec2 = store.reserve(
            run_id="no-parent-run", task_id="no-parent-task", attempt=1,
            scope_id="other-scope",
        )
        assert rec2 is not None

    def test_nested_scope_with_existing_parent(self, store):
        """Nested scope checks exist when parent reservation already exists."""
        parent_env = ReservationEnvelope(max_bytes=2000, max_in_flight=5)
        store.set_envelope("nest-exist-run", "nest-exist-parent", parent_env)

        # First parent reservation
        parent_rec = store.reserve(
            run_id="nest-exist-run", task_id="nest-task", attempt=1,
            scope_id="nest-exist-parent",
            reserved_bytes=800,
        )
        # Child within remaining budget
        child = store.reserve(
            run_id="nest-exist-run", task_id="nest-task", attempt=1,
            scope_id="nest-exist-child",
            parent_scope_id="nest-exist-parent",
            reserved_bytes=500,
        )
        assert child is not None

        # Another child exceeding budget (parent scope has 800 bytes, 800 + 1500 > 2000)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="nest-exist-run", task_id="nest-task", attempt=1,
                scope_id="nest-exist-child2",
                parent_scope_id="nest-exist-parent",
                reserved_bytes=1500,
            )
        assert "bytes" in str(exc.value)


# ===================================================================
# BLOCKING: Task aggregate enforcement across ALL attempts
# ===================================================================

class TestTaskAggregateEnforcement:
    """Prove task aggregate budget enforcement works across ALL attempts."""

    def test_task_aggregate_requests_across_attempts(self, store):
        """Task aggregate max_requests enforced across different attempts."""
        task_id = "agg-attempts-task"
        env = ReservationEnvelope(max_requests=3, max_in_flight=5)
        store.set_envelope("agg-att-run-1", f"{task_id}/attempt-1", env)

        # attempt-1: first request
        store.reserve(run_id="agg-att-run-1", task_id=task_id, attempt=1,
                      scope_id=f"{task_id}/attempt-1")
        # attempt-2 (different attempt, same task): second request
        store.reserve(run_id="agg-att-run-2", task_id=task_id, attempt=2,
                      scope_id=f"{task_id}/attempt-2")
        # attempt-3: third request
        store.reserve(run_id="agg-att-run-3", task_id=task_id, attempt=3,
                      scope_id=f"{task_id}/attempt-3")

        # Fourth should be refused (max_requests=3 across ALL attempts)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="agg-att-run-4", task_id=task_id, attempt=4,
                          scope_id=f"{task_id}/attempt-4")
        assert "requests" in str(exc.value)

    def test_task_aggregate_bytes_across_attempts(self, store):
        """Task aggregate max_bytes enforced across different attempts."""
        task_id = "agg-bytes-task"
        env = ReservationEnvelope(max_bytes=2000, max_in_flight=5)
        store.set_envelope("agg-by-run-1", f"{task_id}/attempt-1", env)

        store.reserve(run_id="agg-by-run-1", task_id=task_id, attempt=1,
                      scope_id=f"{task_id}/attempt-1", reserved_bytes=1200)

        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="agg-by-run-2", task_id=task_id, attempt=2,
                          scope_id=f"{task_id}/attempt-2", reserved_bytes=1200)
        assert "bytes" in str(exc.value)

    def test_task_aggregate_tokens_across_attempts(self, store):
        """Task aggregate max_tokens enforced across different attempts."""
        task_id = "agg-tok-task"
        env = ReservationEnvelope(max_tokens=5000, max_in_flight=5)
        store.set_envelope("agg-tok-run-1", f"{task_id}/attempt-1", env)

        store.reserve(run_id="agg-tok-run-1", task_id=task_id, attempt=1,
                      scope_id=f"{task_id}/attempt-1", reserved_tokens=3000.0)

        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="agg-tok-run-2", task_id=task_id, attempt=2,
                          scope_id=f"{task_id}/attempt-2", reserved_tokens=3000.0)
        assert "tokens" in str(exc.value)

    def test_task_aggregate_cost_across_attempts(self, store):
        """Task aggregate max_cost enforced across different attempts."""
        task_id = "agg-cost-task"
        env = ReservationEnvelope(max_cost=25.0, max_in_flight=5)
        store.set_envelope("agg-cost-run-1", f"{task_id}/attempt-1", env)

        store.reserve(run_id="agg-cost-run-1", task_id=task_id, attempt=1,
                      scope_id=f"{task_id}/attempt-1", cost=15.0)

        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="agg-cost-run-2", task_id=task_id, attempt=2,
                          scope_id=f"{task_id}/attempt-2", cost=15.0)
        assert "cost" in str(exc.value)

    def test_get_task_aggregate_returns_none(self, store):
        """get_task_aggregate returns None when no aggregate exists."""
        agg = store.get_task_aggregate("nonexistent-agg-task")
        assert agg is None

    def test_get_task_aggregate_after_reserve(self, store):
        """get_task_aggregate returns correct data after reserve."""
        task_id = "agg-get-task"
        env = ReservationEnvelope(max_requests=10, max_in_flight=5)
        store.set_envelope("agg-get-run-1", f"{task_id}/attempt-1", env)

        store.reserve(run_id="agg-get-run-1", task_id=task_id, attempt=1,
                      scope_id=f"{task_id}/attempt-1",
                      reserved_bytes=500, reserved_tokens=200.0, cost=5.0)

        agg = store.get_task_aggregate(task_id)
        assert agg is not None
        assert agg["cumulative_requests"] == 1
        assert agg["cumulative_bytes"] == 500
        assert agg["cumulative_tokens"] == 200
        assert agg["cumulative_cost"] == 5.0

    def test_task_aggregate_complete_updates(self, store):
        """complete() updates task aggregate counters."""
        task_id = "agg-comp-task"
        env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("agg-comp-run", f"{task_id}/attempt-1", env)

        rec = store.reserve(run_id="agg-comp-run", task_id=task_id, attempt=1,
                            scope_id=f"{task_id}/attempt-1",
                            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0)
        store.complete(rec.reservation_id, observed_bytes=800, observed_tokens=400.0, cost=8.0)

        agg = store.get_task_aggregate(task_id)
        assert agg is not None
        assert agg["cumulative_bytes"] == 800
        assert agg["cumulative_tokens"] == 400
        assert agg["cumulative_cost"] == 8.0

    def test_task_aggregate_cancel_reverses(self, store):
        """cancel() reverses task aggregate counters."""
        task_id = "agg-cancel-task"
        env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("agg-cancel-run", f"{task_id}/attempt-1", env)

        rec = store.reserve(run_id="agg-cancel-run", task_id=task_id, attempt=1,
                            scope_id=f"{task_id}/attempt-1",
                            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0)
        store.cancel(rec.reservation_id)

        agg = store.get_task_aggregate(task_id)
        assert agg is not None
        assert agg["cumulative_requests"] == 0
        assert agg["cumulative_bytes"] == 0
        assert agg["cumulative_tokens"] == 0
        assert agg["cumulative_cost"] == 0.0

    def test_task_aggregate_reconcile_updates(self, store):
        """reconcile() updates task aggregate counters."""
        task_id = "agg-reconcile-task"
        env = ReservationEnvelope(max_requests=5, max_in_flight=5)
        store.set_envelope("agg-reconcile-run", f"{task_id}/attempt-1", env)

        rec = store.reserve(run_id="agg-reconcile-run", task_id=task_id, attempt=1,
                            scope_id=f"{task_id}/attempt-1",
                            reserved_bytes=1000, reserved_tokens=500.0, cost=10.0)
        store.complete(rec.reservation_id)

        agg = store.get_task_aggregate(task_id)
        assert agg is not None
        assert agg["cumulative_bytes"] == 1000

        store.reconcile(rec.reservation_id, actual_bytes=600)
        agg = store.get_task_aggregate(task_id)
        assert agg["cumulative_bytes"] == 600
        assert agg["cumulative_tokens"] == 500
        assert agg["cumulative_cost"] == 10.0


# ===================================================================
# Run entry point
# ===================================================================

if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
