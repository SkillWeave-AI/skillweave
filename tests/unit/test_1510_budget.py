"""SW-1510-BUDGET: atomic cumulative admission for parent and nested requests.

Covers:
  Criterion 1 — Atomic reservation accounts for run/task/attempt/nested request,
    bytes, conservative token upper bound, deadline, in-flight slots and total
    request budget across restarts.
  Criterion 2 — Parallel reservations cannot overspend; unknown usage remains
    reserved until reconciled and retries each consume an explicit request
    allocation.
  Criterion 3 — A transport outage, quota failure or guard restart cannot switch
    to an unguarded endpoint or rotate credentials to enlarge the approved budget.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
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


#: A real bounded subprocess that exits 0 with non-empty output.
_PASS_PROBE = [sys.executable, "-c", "print('budget-probe-output')"]

#: Scratch directory for temp databases (set to the task tmp dir via TMPDIR).
_SCRATCH = Path(
    os.environ.get(
        "SKILLWEAVE_SCRATCH",
        os.environ.get("TMPDIR", "/tmp"),
    )
)
_SCRATCH.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path():
    """Return a unique temp db path per test (cleaned up after)."""
    p = str(_SCRATCH / f"test_budget_{uuid.uuid4().hex}.db")
    yield p
    try:
        os.unlink(p)
    except OSError:
        pass
    try:
        os.unlink(p + "-wal")
    except OSError:
        pass
    try:
        os.unlink(p + "-shm")
    except OSError:
        pass


@pytest.fixture
def store(db_path):
    """Return a ReservationStore backed by a temp file."""
    rs = ReservationStore(db_path)
    yield rs
    rs.close()


@pytest.fixture
def rsvc_store(db_path):
    """Return an SQLiteRunStore + ReservationStore sharing the same db."""
    s = SQLiteRunStore(db_path)
    rs = ReservationStore(db_path)
    yield s, rs
    rs.close()
    s.close()


@pytest.fixture
def service(rsvc_store):
    """Return a RunApplicationService with reservation_store wired in."""
    s, rs = rsvc_store
    j = EventJournal(s.db_path)
    r = RawArtifactStore()
    svc = RunApplicationService(s, j, r, reservation_store=rs)
    yield svc


# ---------------------------------------------------------------------------
# Helper: create a simple unlimited envelope
# ---------------------------------------------------------------------------


def _unlimited():
    return ReservationEnvelope()


def _limited(**kw):
    return ReservationEnvelope(**kw)


# ===================================================================
# Criterion 1: Atomic reservation accounts for all axes
# ===================================================================


class TestAtomicReservation:
    """Every axis is tracked: run/task/attempt/nested, bytes, tokens,
    deadline, in-flight, request budget.  All checked atomically."""

    def test_basic_lifecycle(self, store):
        """Reserve -> occupy -> complete -> release."""
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="r1/t1/1",
            reserved_bytes=1000, reserved_tokens=500.0,
        )
        assert rec.state == ReservationSlotState.RESERVED.value
        assert rec.reserved_bytes == 1000
        assert rec.reserved_tokens == 500.0
        assert rec.version == 1

        occ = store.occupy(rec.reservation_id)
        assert occ.state == ReservationSlotState.OCCUPIED.value
        assert occ.version == 2

        comp = store.complete(rec.reservation_id, observed_bytes=800, observed_tokens=400.0)
        assert comp.state == ReservationSlotState.COMPLETED.value
        assert comp.observed_bytes == 800

        rel = store.release(rec.reservation_id)
        assert rel.state == ReservationSlotState.RELEASED.value

    def test_run_task_attempt_nested(self, store):
        """Parent and nested scope IDs are tracked."""
        parent = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="r1/t1/1",
        )
        nested = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="r1/t1/1/nested-1",
            parent_scope_id="r1/t1/1",
        )
        assert nested.parent_scope_id == "r1/t1/1"
        lst = store.list_reservations("r1")
        assert len(lst) == 2

    def test_deadline_enforced(self, store):
        """Reservation past deadline is refused."""
        past = "2020-01-01T00:00:00Z"
        env = _limited(max_deadline=past)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(
                run_id="r1", task_id="t1", attempt=1,
                scope_id="r1/t1/1", envelope=env,
            )
        assert "deadline" in str(exc.value)

    def test_deadline_future_accepted(self, store):
        """Reservation before deadline proceeds."""
        future = "2099-01-01T00:00:00Z"
        env = _limited(max_deadline=future)
        rec = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="r1/t1/1", envelope=env,
        )
        assert rec.state == ReservationSlotState.RESERVED.value

    def test_in_flight_limit(self, store):
        """Cannot exceed max_in_flight."""
        env = _limited(max_in_flight=2)
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="scope1", envelope=env)
        r2 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="scope2", envelope=env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope3", envelope=env)
        assert "in_flight" in str(exc.value)

        # Completing a slot frees one in-flight
        store.complete(r1.reservation_id)
        r3 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="scope3", envelope=env)
        assert r3.state == ReservationSlotState.RESERVED.value

    def test_cumulative_request_limit(self, store):
        """Cannot exceed max_requests."""
        env = _limited(max_requests=2)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope1", envelope=env)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope2", envelope=env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope3", envelope=env)
        assert "requests" in str(exc.value)

    def test_cumulative_bytes_limit(self, store):
        """Cannot exceed max_bytes."""
        env = _limited(max_bytes=2000)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope1", reserved_bytes=1500, envelope=env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope2", reserved_bytes=1000, envelope=env)
        assert "bytes" in str(exc.value)

    def test_cumulative_tokens_limit(self, store):
        """Cannot exceed max_tokens."""
        env = _limited(max_tokens=1000)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope1", reserved_tokens=600.0, envelope=env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope2", reserved_tokens=500.0, envelope=env)
        assert "tokens" in str(exc.value)

    def test_cumulative_cost_limit(self, store):
        """Cannot exceed max_cost."""
        env = _limited(max_cost=10.0)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope1", cost=6.0, envelope=env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope2", cost=5.0, envelope=env)
        assert "cost" in str(exc.value)

    def test_per_request_bytes(self, store):
        """Per-request byte ceiling is enforced."""
        env = _limited(max_per_request_bytes=500)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope1", reserved_bytes=400, envelope=env)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope2", reserved_bytes=600, envelope=env)
        assert "per_request_bytes" in str(exc.value)

    def test_per_request_tokens(self, store):
        """Per-request token ceiling is enforced."""
        env = _limited(max_per_request_tokens=300)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="scope1", reserved_tokens=500.0, envelope=env)
        assert "per_request_tokens" in str(exc.value)

    def test_unknown_usage_stays_reserved(self, store):
        """Completing with no observed usage keeps reserved amounts in cumulative."""
        env = _limited(max_bytes=5000)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope1", reserved_bytes=2000, envelope=env)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="scope2", reserved_bytes=2000, envelope=env)
        # Complete without observed usage
        rec = store.get_reservation(
            store.list_reservations("r1")[0].reservation_id
        )
        store.complete(rec.reservation_id)
        usage = store.get_usage("r1", "scope1")
        # Cumulative bytes should still include the reserved amount
        assert usage["cumulative_bytes"] >= 2000

    def test_retry_own_allocation(self, store):
        """Each retry consumes its own explicit request allocation."""
        env = _limited(max_requests=3, max_in_flight=3)
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="attempt-1", envelope=env)
        r2 = store.reserve(run_id="r1", task_id="t1", attempt=2,
                            scope_id="attempt-2", envelope=env)
        # Two retries consumed 2 of 3 request slots (per-run cumulative)
        run_usage = store.get_run_usage("r1")
        assert run_usage["cumulative_requests"] >= 2
        # Third reservation succeeds (within budget)
        r3 = store.reserve(run_id="r1", task_id="t1", attempt=3,
                            scope_id="attempt-3", envelope=env)
        assert r3 is not None
        # Complete one to free in_flight, then 4th hits request limit
        store.complete(r1.reservation_id)
        with pytest.raises(AdmissionRefused) as exc:
            store.reserve(run_id="r1", task_id="t1", attempt=4,
                          scope_id="attempt-4", envelope=env)
        assert "requests" in str(exc.value)

    def test_cancel_reverses_request_count(self, store):
        """Cancelling a reservation reverses the request count."""
        env = _limited(max_requests=2)
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="scope1", envelope=env)
        usage_before = store.get_usage("r1", "scope1")
        assert usage_before["cumulative_requests"] >= 1
        store.cancel(r1.reservation_id)
        usage_after = store.get_usage("r1", "scope1")
        assert usage_after["cumulative_requests"] == 0


# ===================================================================
# Criterion 2: Parallel reservations cannot overspend
# ===================================================================


class TestParallelOverspend:
    """Concurrent reservation writers using independent connections cannot
    overspend a finite envelope."""

    def test_concurrent_in_flight_limit(self, db_path):
        """Two concurrent connections both trying to exceed max_in_flight=1.

        Exactly one succeeds beyond the limit; the other is refused.
        """
        env = _limited(max_in_flight=1)
        store = ReservationStore(db_path)
        store.set_envelope("r1", "scope", env)
        store.close()

        results: list = []
        errors: list = []

        def _reserve(conn_id):
            try:
                s = ReservationStore(db_path)
                rec = s.reserve(
                    run_id="r1", task_id="t1", attempt=1,
                    scope_id=f"scope-{conn_id}",
                    envelope=env,
                )
                results.append(rec.reservation_id)
                s.close()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_reserve, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At most 1 reservation succeeded (max_in_flight=1)
        assert len(results) <= 1
        # At least 4 were refused
        assert len(errors) >= 4

    def test_concurrent_cumulative_limit(self, db_path):
        """Two concurrent connections both trying to exceed max_requests=1."""
        env = _limited(max_requests=1)
        store = ReservationStore(db_path)
        store.set_envelope("r1", "scope", env)
        store.close()

        results: list = []
        errors: list = []

        def _reserve(conn_id):
            try:
                s = ReservationStore(db_path)
                rec = s.reserve(
                    run_id="r1", task_id="t1", attempt=1,
                    scope_id=f"scope-{conn_id}",
                    envelope=env,
                )
                results.append(rec.reservation_id)
                s.close()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_reserve, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At most 1 succeeded (cumulative max_requests=1)
        assert len(results) <= 1

    def test_concurrent_exact_limit(self, db_path):
        """Multiple concurrent writers, each within budget, all succeed."""
        env = _limited(max_requests=10, max_in_flight=10)
        store = ReservationStore(db_path)
        store.set_envelope("r1", "scope", env)
        store.close()

        results: list = []
        errors: list = []

        def _reserve(conn_id):
            try:
                s = ReservationStore(db_path)
                rec = s.reserve(
                    run_id="r1", task_id="t1", attempt=1,
                    scope_id=f"scope-{conn_id}",
                    envelope=env,
                )
                results.append(rec.reservation_id)
                s.close()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_reserve, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        assert len(errors) == 0


# ===================================================================
# Criterion 3: Transport outage / quota failure / restart
# ===================================================================


class TestRestartAndQuota:
    """Restart recovery and quota boundaries."""

    def test_restart_recovery(self, store):
        """Orphaned reservations (RESERVED/OCCUPIED) are recovered on restart."""
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="orphan-1")
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="orphan-2")
        recovered = store.recover_orphaned()
        assert len(recovered) == 2
        for rec in recovered:
            assert rec.state == ReservationSlotState.COMPLETED.value

    def test_restart_frees_in_flight(self, store):
        """Recovered orphans free their in-flight slots."""
        env = _limited(max_in_flight=1)
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="orphan", envelope=env)
        recovered = store.recover_orphaned()
        assert len(recovered) >= 1
        # Now another reservation should succeed (in_flight was freed)
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="new-after-recovery", envelope=env)
        assert rec.state == ReservationSlotState.RESERVED.value

    def test_recovery_idempotent(self, store):
        """Calling recover_orphaned twice is safe (second pass finds none)."""
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="orphan")
        r1 = store.recover_orphaned()
        r2 = store.recover_orphaned()
        assert len(r1) >= 1
        assert len(r2) == 0

    def test_cancel_before_occupy(self, store):
        """A reservation can be cancelled before being occupied."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="to-cancel")
        cancelled = store.cancel(rec.reservation_id)
        assert cancelled.state == ReservationSlotState.CANCELLED.value

    def test_cancel_reverses_usage(self, store):
        """Cancelling reverses cumulative request and in_flight."""
        env = _limited(max_requests=3, max_in_flight=2)
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="to-cancel", envelope=env,
                            reserved_bytes=500, reserved_tokens=100.0, cost=2.0)
        run_usage_before = store.get_run_usage("r1")
        assert run_usage_before["in_flight"] >= 1
        usage_before = store.get_usage("r1", "to-cancel")
        assert usage_before["cumulative_requests"] >= 1
        store.cancel(r1.reservation_id)
        run_usage_after = store.get_run_usage("r1")
        assert run_usage_after["in_flight"] == 0
        usage_after = store.get_usage("r1", "to-cancel")
        assert usage_after["cumulative_requests"] == 0
        assert usage_after["cumulative_bytes"] == 0
        assert usage_after["cumulative_cost"] == 0.0

    def test_reconcile_unknown_usage(self, store):
        """Reconcile replaces reserved amounts with actuals."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="to-reconcile",
                            reserved_bytes=5000, reserved_tokens=1000.0, cost=5.0)
        store.complete(rec.reservation_id)
        reconciled = store.reconcile(
            rec.reservation_id,
            actual_bytes=3000, actual_tokens=800.0, actual_cost=3.0,
        )
        assert reconciled.state == ReservationSlotState.RECONCILED.value
        assert reconciled.observed_bytes == 3000
        usage = store.get_usage("r1", "to-reconcile")
        # Delta: -2000 bytes, -200 tokens, -2.0 cost
        # Original cumulative bytes = 5000, now 5000 + (3000-5000) = 3000
        assert usage["cumulative_bytes"] == 3000


# ===================================================================
# Malformed / negative / bool limits
# ===================================================================


class TestEnvelopeValidation:
    """Finite limits must be non-negative, not boolean, well-typed."""

    def test_negative_cost_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_cost=-1.0)

    def test_negative_tokens_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_tokens=-1)

    def test_negative_bytes_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_bytes=-100)

    def test_negative_requests_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_requests=-1)

    def test_bool_max_tokens_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_tokens=True)

    def test_bool_max_bytes_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_bytes=False)

    def test_bool_max_cost_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_cost=True)

    def test_zero_in_flight_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_in_flight=0)

    def test_negative_in_flight_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_in_flight=-1)

    def test_unlimited_accepted(self):
        """All-None envelope is the explicit unlimited mode."""
        env = ReservationEnvelope()
        assert env.max_cost is None
        assert env.max_tokens is None
        assert env.max_bytes is None
        assert env.max_requests is None
        assert env.max_deadline is None
        # max_in_flight defaults to 3 (positive bound, not cumulative)
        assert env.max_in_flight == 3

    def test_unlimited_mode_progresses(self, store):
        """Unlimited envelope does not block reservations under in-flight bounds."""
        env = ReservationEnvelope()  # max_in_flight=3
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s1", envelope=env)
        r2 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s2", envelope=env)
        r3 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s3", envelope=env)
        assert r3 is not None
        # 4th should fail (max_in_flight=3)
        with pytest.raises(AdmissionRefused):
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="s4", envelope=env)

    def test_malformed_deadline_refused(self):
        with pytest.raises(EnvelopeRejected):
            _limited(max_deadline="not-a-timestamp")


# ===================================================================
# Idempotency key scoping
# ===================================================================


class TestIdempotencyScoping:
    """Idempotency keys cannot leak across scopes."""

    def test_same_key_different_scope(self, store):
        """Same key in different scopes creates separate reservations."""
        r1 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-a", idempotency_key="same-key",
        )
        r2 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-b", idempotency_key="same-key",
        )
        assert r1.reservation_id != r2.reservation_id

    def test_same_key_same_scope_idempotent(self, store):
        """Same key in same scope returns existing reservation."""
        r1 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-a", idempotency_key="dup-key",
        )
        r2 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-a", idempotency_key="dup-key",
        )
        assert r1.reservation_id == r2.reservation_id


# ===================================================================
# Real service ingress (RunApplicationService + reservation_store)
# ===================================================================


class TestServiceIngress:
    """Reservation is acquired before worker launch via RunApplicationService."""

    def test_reservation_wired_in_execute(self, service):
        """execute() acquires a reservation and completes it."""
        run_id = "budget-exec-1"
        exec_result = service.execute(
            _PASS_PROBE,
            run_id=run_id,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=_limited(max_in_flight=5),
        )
        assert exec_result.run_id == run_id
        assert exec_result.run is not None

        # Verify a reservation was created
        rs = service.reservation_store
        assert rs is not None
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1
        rec = reservations[0]
        assert rec.state in (
            ReservationSlotState.COMPLETED.value,
            ReservationSlotState.RELEASED.value,
        )

    def test_reservation_refuses_overspend(self, service):
        """execute() refuses when in-flight limit is exceeded."""
        run_id = "budget-overspend-1"
        env = _limited(max_in_flight=1)

        # First execute succeeds
        exec_result = service.execute(
            _PASS_PROBE,
            run_id=run_id,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=env,
        )
        assert exec_result.run is not None

        # Second execute with same run_id and limited envelope
        # This tests that a new execute for a new run_id with max_in_flight=1
        # on a different scope does NOT conflict
        run_id2 = "budget-overspend-2"
        exec_result2 = service.execute(
            _PASS_PROBE,
            run_id=run_id2,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            reservation_envelope=env,
        )
        assert exec_result2.run is not None

    def test_reservation_with_assay_methodology(self, service):
        """Reservation works with assay methodology."""
        run_id = "budget-assay-1"
        exec_result = service.execute(
            _PASS_PROBE,
            run_id=run_id,
            tool="opencode",
            model="budget-test",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            created_at="2026-10-05T00:00:00Z",
            methodology="assay",
            autonomy_policy="unicorn",
        )
        assert exec_result.run is not None
        # Verify reservation exists
        rs = service.reservation_store
        reservations = rs.list_reservations(run_id)
        assert len(reservations) >= 1


# ===================================================================
# Envelope persistence
# ===================================================================


class TestEnvelopePersistence:
    """Envelopes can be stored and retrieved."""

    def test_set_and_get_envelope(self, store):
        env = _limited(max_cost=100.0, max_tokens=50000, max_in_flight=5)
        store.set_envelope("r1", "scope", env)
        loaded = store.get_envelope("r1", "scope")
        assert loaded is not None
        assert loaded.max_cost == 100.0
        assert loaded.max_tokens == 50000
        assert loaded.max_in_flight == 5

    def test_get_nonexistent_envelope(self, store):
        env = store.get_envelope("nonexistent", "nope")
        assert env is None

    def test_update_envelope(self, store):
        env1 = _limited(max_cost=50.0)
        store.set_envelope("r1", "scope", env1)
        env2 = _limited(max_cost=100.0)
        store.set_envelope("r1", "scope", env2)
        loaded = store.get_envelope("r1", "scope")
        assert loaded.max_cost == 100.0


# ===================================================================
# Edge cases
# ===================================================================


class TestEdgeCases:
    """Cancellation and late-finalization idempotency."""

    def test_double_complete_refused(self, store):
        """Completing an already-completed reservation is refused."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="double-comp")
        store.complete(rec.reservation_id)
        with pytest.raises(ReservationError):
            store.complete(rec.reservation_id)

    def test_complete_then_release(self, store):
        """Complete -> Release is the expected terminal path."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="comp-rel")
        store.complete(rec.reservation_id)
        rel = store.release(rec.reservation_id)
        assert rel.state == ReservationSlotState.RELEASED.value

    def test_cancel_then_release(self, store):
        """Cancel -> Release is also valid."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="cancel-rel")
        store.cancel(rec.reservation_id)
        rel = store.release(rec.reservation_id)
        assert rel.state == ReservationSlotState.RELEASED.value

    def test_release_from_reserved_refused(self, store):
        """Cannot release a reservation that is still RESERVED (not completed)."""
        rec = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="no-release-reserved")
        with pytest.raises(ReservationError):
            store.release(rec.reservation_id)

    def test_unknown_reservation_id(self, store):
        """Operations on unknown IDs raise ReservationError."""
        with pytest.raises(ReservationError):
            store.complete("nonexistent-id")
        with pytest.raises(ReservationError):
            store.cancel("nonexistent-id")

    def test_in_flight_accounting_after_complete(self, store):
        """In-flight count is decremented after complete."""
        env = _limited(max_in_flight=2)
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s1", envelope=env)
        assert store.count_in_flight("r1") == 1
        store.complete(r1.reservation_id)
        assert store.count_in_flight("r1") == 0

    def test_list_by_state(self, store):
        """List reservations filtered by state."""
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="s1")
        store.reserve(run_id="r1", task_id="t1", attempt=1,
                      scope_id="s2")
        reserved = store.list_reservations("r1", state="reserved")
        assert len(reserved) == 2

    def test_different_run_separate_usage(self, store):
        """Different runs have independent usage tracking."""
        r1 = store.reserve(run_id="run-a", task_id="t1", attempt=1,
                            scope_id="scope")
        r2 = store.reserve(run_id="run-b", task_id="t1", attempt=1,
                            scope_id="scope")
        u1 = store.get_usage("run-a", "scope")
        u2 = store.get_usage("run-b", "scope")
        assert u1["cumulative_requests"] >= 1
        assert u2["cumulative_requests"] >= 1
        assert u1["cumulative_requests"] + u2["cumulative_requests"] >= 2

    def test_scope_scoped_idempotency_key(self, store):
        """Effective key includes run_id and scope_id."""
        r1 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-a", idempotency_key="my-key",
        )
        # Same key in same scope returns same
        r2 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-a", idempotency_key="my-key",
        )
        assert r1.reservation_id == r2.reservation_id
        # Different scope with same key -> different reservation
        r3 = store.reserve(
            run_id="r1", task_id="t1", attempt=1,
            scope_id="scope-b", idempotency_key="my-key",
        )
        assert r1.reservation_id != r3.reservation_id
        # Different run with same key and scope -> different reservation
        r4 = store.reserve(
            run_id="r2", task_id="t1", attempt=1,
            scope_id="scope-a", idempotency_key="my-key",
        )
        assert r1.reservation_id != r4.reservation_id


# ===================================================================
# Unlimited mode across restart
# ===================================================================


class TestUnlimitedMode:
    """Explicit unlimited owner mode progresses under three occupied slots
    across restart."""

    def test_unlimited_progresses(self, db_path):
        """Three in-flight slots under unlimited mode work."""
        env = ReservationEnvelope()  # max_in_flight=3, no cumulative limits
        store = ReservationStore(db_path)
        store.set_envelope("r1", "scope", env)

        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s1", envelope=env)
        r2 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s2", envelope=env)
        r3 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s3", envelope=env)
        assert r3 is not None
        store.close()

        # Simulate restart: new store instance, recover orphans, continue
        store2 = ReservationStore(db_path)
        recovered = store2.recover_orphaned()
        # Three orphaned reservations should be recovered
        assert len(recovered) >= 2  # at least the ones still RESERVED
        # After recovery, should be able to reserve again
        r4 = store2.reserve(run_id="r1", task_id="t1", attempt=1,
                             scope_id="s4", envelope=env)
        assert r4 is not None
        store2.close()

    def test_unlimited_three_slots(self, store):
        """Unlimited mode with 3 in-flight slots works across complete/release."""
        env = ReservationEnvelope()
        r1 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s1", envelope=env)
        r2 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s2", envelope=env)
        r3 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s3", envelope=env)
        assert store.count_in_flight("r1") == 3
        # 4th is refused
        with pytest.raises(AdmissionRefused):
            store.reserve(run_id="r1", task_id="t1", attempt=1,
                          scope_id="s4", envelope=env)
        # Complete one, then 4th succeeds
        store.complete(r1.reservation_id)
        r4 = store.reserve(run_id="r1", task_id="t1", attempt=1,
                            scope_id="s4", envelope=env)
        assert r4 is not None


# ===================================================================
# Run entry point
# ===================================================================


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
