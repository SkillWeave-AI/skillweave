"""SW-ORCH-001 criteria 1-3: the native process service.

* criterion 1 -- one native process service drives ready dispatch, independent
  review, repair, re-review, controlled integration and dependent waves over
  durable intents (no external bridge);
* criterion 2 -- durable dispatch intents + verified process/session identity +
  a single controller lease with fencing prevent duplicate launch and
  stale-controller acceptance;
* criterion 3 -- readiness refills actual free capacity subject to
  dependency/write-surface exclusion, and worker count is distinguishable from
  real useful progress.

Every test is deterministic: no provider probe, no wall-clock race (instants
are compared against explicit ``now`` values or by relative ordering), and no
skip/xfail.  The two boundary contracts are checked for backward compatibility
(a v1 record round-trips unchanged) and fail-closed version handling (an
unsupported version is refused, never coerced).
"""

import sys
import tempfile
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.runtime.dagscheduler import (
    ReservationSnapshot,
    RouteCapacitySnapshot,
    Task,
)
from skillweave.runtime.native_process import (
    CHECKPOINT_SCHEMA_ID,
    CHECKPOINT_SCHEMA_VERSION,
    DISPATCH_SCHEMA_ID,
    DISPATCH_SCHEMA_VERSION,
    INTENT_FAILED,
    INTENT_INTEGRATED,
    INTENT_LAUNCHED,
    INTENT_REVIEWED,
    STEPS,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    ControllerLease,
    DispatchIntent,
    DuplicateDispatchError,
    LeaseNotHeldError,
    LeaseRefusedError,
    NativeProcessCheckpoint,
    NativeProcessService,
    NotControllerError,
    OwnedAttemptDispatch,
    StaleFencingTokenError,
    UnsupportedContractVersion,
    plan_readiness,
    validate_native_process_checkpoint,
    validate_owned_attempt_dispatch,
)
from skillweave.runtime.store import SQLiteRunStore
from skillweave.runtime.write_scope import ScopeConflictError


def _store(tmp: str) -> SQLiteRunStore:
    return SQLiteRunStore(db_path=str(Path(tmp) / "store.db"))


def _fresh_route(capacity: int, now: str) -> RouteCapacitySnapshot:
    return RouteCapacitySnapshot(
        available_routes=frozenset(f"provider/model{i}" for i in range(capacity)),
        observed_at=now,
        freshness_seconds=3600.0,
    )


def _fresh_reservation(in_flight: int, now: str) -> ReservationSnapshot:
    return ReservationSnapshot(
        remaining_in_flight=in_flight,
        observed_at=now,
        freshness_seconds=3600.0,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 1: the controlled loop is declared and driven over durable intents
# ═══════════════════════════════════════════════════════════════════════════

def test_loop_declares_the_six_owned_steps_in_order():
    assert STEPS == (
        STEP_READY_DISPATCH,
        STEP_INDEPENDENT_REVIEW,
        STEP_REPAIR,
        STEP_RE_REVIEW,
        STEP_CONTROLLED_INTEGRATION,
        STEP_DEPENDENT_WAVE,
    )


def test_service_drives_review_repair_rereview_integration_dependent_wave():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        intent, _ = svc.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            step=STEP_READY_DISPATCH,
        )
        assert store.get_dispatch_intent(intent.intent_id).state == INTENT_LAUNCHED

        reviewed = svc.record_review(intent.intent_id)
        assert reviewed.state == INTENT_REVIEWED
        assert reviewed.step == STEP_INDEPENDENT_REVIEW

        repaired = svc.record_repair(intent.intent_id)
        assert repaired.step == STEP_REPAIR

        rereviewed = svc.record_re_review(intent.intent_id)
        assert rereviewed.state == INTENT_REVIEWED
        assert rereviewed.step == STEP_RE_REVIEW

        integrated = svc.record_integration(intent.intent_id)
        assert integrated.state == INTENT_INTEGRATED

        wave = svc.record_dependent_wave(intent.intent_id)
        assert wave.step == STEP_DEPENDENT_WAVE
        store.close()


def test_loop_state_survives_a_controller_restart_without_live_memory():
    # Criterion 1: the loop is durable, not held in a process's memory.  A NEW
    # service instance reading the same store sees the same intents.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)  # already-expired lease, for takeover
        intent, _ = first.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        first.record_repair(intent.intent_id)
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        lease, checkpoint = recovered.resume(ttl_seconds=3600)
        intents = reopened.list_dispatch_intents(run_id="run-1")
        assert len(intents) == 1
        assert intents[0].state == "repairing"
        assert intents[0].step == STEP_REPAIR
        # The recovered controller reads a strictly higher fencing token than
        # the controller it replaced, and resumes from the durable journal
        # cursor (no events were written, so the offset is the journal origin).
        assert lease.fencing_token == 2
        assert checkpoint.fencing_token == 2
        assert checkpoint.journal_offset == 0
        assert checkpoint.in_flight == (("run-1/task-a/1", STEP_REPAIR),)
        reopened.close()


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2a: durable dispatch intents prevent duplicate launch
# ═══════════════════════════════════════════════════════════════════════════

def test_a_second_claiming_intent_for_a_scope_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        try:
            svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
            assert False, "a duplicate launch of the same scope must be refused"
        except DuplicateDispatchError:
            pass
        store.close()


def test_scope_is_reusable_once_the_attempt_is_terminal():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        intent, _ = svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        svc.record_review(intent.intent_id)  # terminal
        # A terminal attempt no longer claims the scope; a fresh dispatch is
        # admitted (a repair/retry is not a duplicate of a finished attempt).
        second, _ = svc.dispatch(run_id="run-1", task_id="task-a", attempt=2)
        assert second.state == INTENT_LAUNCHED
        store.close()


def test_failed_attempt_is_resolved_and_frees_its_scope():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        intent, _ = svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        failed = svc.record_failure(intent.intent_id)
        assert failed.state == INTENT_FAILED
        # A resolved (failed) attempt no longer claims the scope, so a retry
        # on the same scope is admitted rather than refused as a duplicate.
        second, _ = svc.dispatch(run_id="run-1", task_id="task-a", attempt=2)
        assert second.state == INTENT_LAUNCHED
        store.close()


def test_store_partial_index_refuses_a_duplicate_scope_atomically():
    # Even bypassing the service, the storage layer itself refuses a second
    # claiming intent for the same scope (the partial unique index).
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.record_dispatch_intent(
            "run-1", "task-a", 1, "run-1/task-a/1",
            step=STEP_READY_DISPATCH, state=INTENT_LAUNCHED,
        )
        try:
            store.record_dispatch_intent(
                "run-1", "task-a", 1, "run-1/task-a/1",
                step=STEP_READY_DISPATCH, state=INTENT_LAUNCHED,
            )
            assert False, "the store must refuse a duplicate claiming scope"
        except DuplicateDispatchError:
            pass
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2b: verified process/session identity
# ═══════════════════════════════════════════════════════════════════════════

def test_dispatch_record_requires_verified_identity_not_a_bare_pid():
    try:
        OwnedAttemptDispatch(
            run_id="run-1", task_id="task-a", attempt=1, fencing_token=1,
            process_identity={"pid": 4242},
        )
        assert False, "a bare PID must not pass as a verified identity"
    except ValueError:
        pass


def test_in_flight_identity_is_reverified_against_the_os():
    import os

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        from skillweave.runtime import process_identity

        live = process_identity.capture(os.getpid())
        assert live is not None, "the test process must be observable"
        intent, _ = svc.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=live.to_dict(),
        )
        verification = svc.verify_owned_attempt(intent)
        assert verification.ok is True
        assert verification.reason == "match"

        # A recorded identity whose start_time no longer matches is a recycled
        # PID: it is refused, not trusted.
        forged = live.to_dict()
        forged["start_time"] = "0.0"
        stale_intent = DispatchIntent(
            intent_id="forged", run_id="run-1", task_id="task-a", attempt=1,
            execution_scope="run-1/task-a/1", step=STEP_READY_DISPATCH,
            state=INTENT_REVIEWED, fencing_token=1, process_identity=forged,
        )
        recycled = svc.verify_owned_attempt(stale_intent)
        assert recycled.ok is False
        assert recycled.reason == "recycled_pid"
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2c: a single controller lease with fencing
# ═══════════════════════════════════════════════════════════════════════════

def test_a_live_controller_lease_is_never_stolen():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.acquire_controller_lease("controller-a", ttl_seconds=3600)
        try:
            store.acquire_controller_lease("controller-b", ttl_seconds=3600)
            assert False, "a live controller lease must not be stolen"
        except LeaseRefusedError:
            pass
        assert store.get_controller_lease().holder == "controller-a"
        store.close()


def test_takeover_after_expiry_reads_a_strictly_higher_fencing_token():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = store.acquire_controller_lease("controller-a", ttl_seconds=0)
        assert first.fencing_token == 1
        # ttl_seconds=0 makes the lease already expired at acquisition, so a
        # second acquirer takes it over -- and must receive a higher token.
        second = store.acquire_controller_lease("controller-b", ttl_seconds=3600)
        assert second.fencing_token == 2
        assert second.fencing_token > first.fencing_token
        assert store.get_controller_lease().holder == "controller-b"
        store.close()


def test_a_superseded_controller_write_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        stale = store.acquire_controller_lease("controller-a", ttl_seconds=0)
        store.acquire_controller_lease("controller-b", ttl_seconds=3600)
        # The stale controller presents its (now superseded) token.
        try:
            store.assert_controller(stale.fencing_token)
            assert False, "a stale controller's write must be refused"
        except StaleFencingTokenError:
            pass
        # Its heartbeat is refused too.
        try:
            store.heartbeat_controller_lease("controller-a", stale.fencing_token)
            assert False, "a stale controller must not renew the lease"
        except (StaleFencingTokenError, LeaseNotHeldError):
            pass
        store.close()


def test_no_lease_held_write_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        try:
            store.assert_controller(1)
            assert False, "a write with no held lease must be refused"
        except LeaseNotHeldError:
            pass
        store.close()


def test_released_lease_can_be_reacquired_with_a_higher_token():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        held = store.acquire_controller_lease("controller-a", ttl_seconds=3600)
        assert store.release_controller_lease(held.fencing_token) is True
        again = store.acquire_controller_lease("controller-a", ttl_seconds=3600)
        assert again.fencing_token > held.fencing_token
        store.close()


def test_service_refuses_a_stale_controller_action():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        stale = NativeProcessService(store, "controller-a")
        stale.acquire(ttl_seconds=0)  # expired at once
        svc = NativeProcessService(store, "controller-b")
        svc.acquire(ttl_seconds=3600)
        # controller-a's service no longer holds the lease; its dispatch is
        # refused before any durable effect.
        try:
            stale.dispatch(run_id="run-1", task_id="task-a", attempt=1)
            assert False, "a stale controller must not dispatch"
        except NotControllerError:
            pass
        assert store.list_dispatch_intents() == []
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: dynamic readiness refill
# ═══════════════════════════════════════════════════════════════════════════

def test_readiness_admits_only_up_to_measured_free_capacity():
    now = "2026-10-14T00:00:00+00:00"
    tasks = [Task(id=f"t{i}", write_scope=[f"src/lane{i}"]) for i in range(5)]
    plan = plan_readiness(
        tasks, done=[],
        route_capacity=_fresh_route(3, now),
        reservations=_fresh_reservation(10, now),
        now=now,
    )
    assert plan.capacity_available == 3
    assert plan.worker_count == 3
    assert len(plan.admitted) == 3
    assert set(plan.refused.values()) == {"measured-parallelism"}


def test_readiness_honours_dependency_exclusion():
    now = "2026-10-14T00:00:00+00:00"
    tasks = [
        Task(id="a", write_scope=["src/lane-a"]),
        Task(id="b", depends_on=["a"], write_scope=["src/lane-b"]),
    ]
    plan = plan_readiness(
        tasks, done=[],
        route_capacity=_fresh_route(5, now),
        reservations=_fresh_reservation(5, now),
        now=now,
    )
    assert plan.admitted == ("a",)
    assert "b" in plan.refused
    assert plan.refused["b"].startswith("dependency:")

    # Once the dependency is done, b is admitted.
    plan2 = plan_readiness(
        tasks, done=["a"],
        route_capacity=_fresh_route(5, now),
        reservations=_fresh_reservation(5, now),
        now=now,
    )
    assert plan2.admitted == ("b",)


def test_readiness_honours_write_surface_exclusion():
    now = "2026-10-14T00:00:00+00:00"
    tasks = [
        Task(id="a", write_scope=["src/shared"]),
        Task(id="b", write_scope=["src/shared/nested"]),  # overlaps a
        Task(id="c", write_scope=["src/other"]),
    ]
    plan = plan_readiness(
        tasks, done=[],
        route_capacity=_fresh_route(5, now),
        reservations=_fresh_reservation(5, now),
        now=now,
    )
    # a and b overlap in write scope, so neither may fan out: each is admitted
    # only inline (it runs alone).  The disjoint lane c still fans out.
    assert plan.inline == ("a", "b")
    assert "c" in plan.admitted
    assert set(plan.admitted) == {"a", "b", "c"}


def test_readiness_distinguishes_worker_count_from_useful_progress():
    now = "2026-10-14T00:00:00+00:00"
    tasks = [
        Task(id="done1", write_scope=["src/d1"]),
        Task(id="done2", write_scope=["src/d2"]),
        Task(id="ready", write_scope=["src/r"]),
    ]
    plan = plan_readiness(
        tasks, done=["done1", "done2"],
        route_capacity=_fresh_route(5, now),
        reservations=_fresh_reservation(5, now),
        now=now,
    )
    # Two attempts already completed (useful progress) while only one worker is
    # (re)started -- the two numbers are independent, not the same count.
    assert plan.worker_count == 1
    assert plan.useful_progress == 2
    assert plan.admitted == ("ready",)


def test_readiness_is_opt_in_and_invents_no_capacity():
    tasks = [Task(id="t", write_scope=["src/lane"])]
    plan = plan_readiness(tasks, done=[])
    assert plan.capacity_available is None
    assert plan.worker_count == 0
    assert plan.admitted == ()


def test_measured_zero_capacity_admits_nothing():
    now = "2026-10-14T00:00:00+00:00"
    tasks = [Task(id="t", write_scope=["src/lane"])]
    plan = plan_readiness(
        tasks, done=[],
        route_capacity=_fresh_route(0, now),
        reservations=_fresh_reservation(5, now),
        now=now,
    )
    assert plan.capacity_available == 0
    assert plan.admitted == ()
    assert plan.refused["t"] == "measured-parallelism"


def test_service_readiness_holds_existing_write_claims():
    now = "2026-10-14T00:00:00+00:00"
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.claim_write_scope("other-run", ["src/owned"])
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        tasks = [
            Task(id="conflict", write_scope=["src/owned/inner"]),
            Task(id="free", write_scope=["src/free"]),
        ]
        plan = svc.readiness(
            tasks, done=[],
            route_capacity=_fresh_route(5, now),
            reservations=_fresh_reservation(5, now),
            now=now,
        )
        # The lane overlapping the scope held by another run cannot fan out.
        assert "conflict" in plan.inline
        assert "free" in plan.admitted
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Boundary contracts: backward compatibility + fail-closed version handling
# ═══════════════════════════════════════════════════════════════════════════

def test_existing_v1_checkpoint_round_trips_unchanged():
    record = NativeProcessCheckpoint(
        run_id="run-1", fencing_token=7, journal_offset=42,
        in_flight=(("run-1/task-a/1", STEP_READY_DISPATCH),),
        route="provider/model", session_id="sess-1",
    ).to_dict()
    assert record["schema_id"] == CHECKPOINT_SCHEMA_ID
    assert record["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    # A v1 consumer accepts the existing record unchanged (additive/opt-in).
    validate_native_process_checkpoint(record)


def test_existing_v1_dispatch_round_trips_unchanged():
    record = OwnedAttemptDispatch(
        run_id="run-1", task_id="task-a", attempt=1, fencing_token=3,
        route="provider/model", session_id="sess-1",
        process_identity={"pid": 1, "start_time": "0.0", "argv": []},
    ).to_dict()
    assert record["schema_id"] == DISPATCH_SCHEMA_ID
    assert record["schema_version"] == DISPATCH_SCHEMA_VERSION
    validate_owned_attempt_dispatch(record)


def test_unsupported_checkpoint_version_fails_closed():
    record = NativeProcessCheckpoint(run_id="run-1", fencing_token=1).to_dict()
    record["schema_version"] = "2.0.0"
    try:
        validate_native_process_checkpoint(record)
        assert False, "an unsupported checkpoint version must fail closed"
    except UnsupportedContractVersion:
        pass
    # And constructing the record itself fails closed.
    try:
        NativeProcessCheckpoint(run_id="run-1", fencing_token=1, schema_version="2.0.0")
        assert False
    except UnsupportedContractVersion:
        pass


def test_unsupported_dispatch_version_fails_closed():
    record = OwnedAttemptDispatch(
        run_id="run-1", task_id="task-a", attempt=1, fencing_token=1
    ).to_dict()
    record["schema_version"] = "9.9.9"
    try:
        validate_owned_attempt_dispatch(record)
        assert False, "an unsupported dispatch version must fail closed"
    except UnsupportedContractVersion:
        pass


def test_unknown_schema_id_is_refused():
    record = NativeProcessCheckpoint(run_id="run-1", fencing_token=1).to_dict()
    record["schema_id"] = "https://skillweave.dev/schemas/storage/other/v1"
    try:
        validate_native_process_checkpoint(record)
        assert False, "an unknown schema_id must be refused"
    except ValueError:
        pass


def test_checkpoint_requires_a_fencing_token():
    try:
        NativeProcessCheckpoint(run_id="run-1", fencing_token=-1)
        assert False, "a negative fencing token must be refused"
    except ValueError:
        pass


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    failures = 0
    for t in _tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
