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
from typing import get_type_hints

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import skillweave.runtime.dagscheduler as _dagscheduler
from skillweave.runtime.dagscheduler import (
    ReservationSnapshot,
    RouteCapacitySnapshot,
    Sequence as DagSequence,
    SelectionLedger,
    Task,
    _split_by_max_parallel,
    build_sessions,
)
from skillweave.runtime.native_process import (
    ACCEPTED_GATE_STATUSES,
    ARCHITECT_HEALTHY,
    ARCHITECT_QUOTA_EXHAUSTED,
    CHECKPOINT_SCHEMA_ID,
    CHECKPOINT_SCHEMA_VERSION,
    CONTENT_TXN_SCHEMA_ID,
    CONTENT_TXN_SCHEMA_VERSION,
    DISPATCH_SCHEMA_ID,
    DISPATCH_SCHEMA_VERSION,
    EVIDENCE_VIEW_SCHEMA_ID,
    EVIDENCE_VIEW_SCHEMA_VERSION,
    GATE_ACCEPTED,
    CONTINUATION_CHECKPOINT,
    CONTINUATION_SESSION_RESUME,
    HARNESS_CONTINUATION_SCHEMA_ID,
    HARNESS_CONTINUATION_SCHEMA_VERSION,
    SUPPORTED_HARNESS_CONTINUATION_VERSIONS,
    HARNESS_SESSION_RESUME_CAPABILITY,
    CHILD_DRAIN_SCHEMA_ID,
    CHILD_DRAINED,
    CHILD_DRAIN_REFUSED,
    CHILD_DRAIN_REFUSED_NO_RECORD,
    CHILD_DRAIN_REFUSED_NOT_OWNED,
    CHILD_HEARTBEAT_ONLY,
    CHILD_PROBE,
    USEFUL_SLOT_EXEMPT_STALLED,
    USEFUL_SLOT_FREED,
    USEFUL_SLOT_OCCUPIED,
    OwnedChildDrain,
    plan_child_drain,
    LIMITATION_SESSION_UNKNOWN,
    LIMITATION_UNSUPPORTED_RESUME,
    INTEGRATION_CONTENT_TRANSACTION,
    INTEGRATION_GIT_MERGE,
    INTENT_FAILED,
    INTENT_INTEGRATED,
    INTENT_LAUNCHED,
    INTENT_REVIEWED,
    RESUME_ADOPTED,
    RESUME_DECISION_SCHEMA_ID,
    RESUME_REFUSED_NO_OWNED_ATTEMPT,
    STEPS,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    ContentTransaction,
    ContinuationDecision,
    ControllerLease,
    DispatchIntent,
    DuplicateDispatchError,
    EvidenceDelta,
    HarnessContinuation,
    LeaseNotHeldError,
    LeaseRefusedError,
    NativeProcessCheckpoint,
    NativeProcessService,
    NotControllerError,
    OwnedAttemptDispatch,
    StaleFencingTokenError,
    SubjectMismatchError,
    UnknownSubjectKind,
    UnsupportedContractVersion,
    WrongIntegrationModeError,
    build_evidence_delta,
    evidence_view_fingerprint,
    evidence_view_header,
    integration_mode_for,
    is_repository_subject,
    plan_continuation,
    plan_harness_continuation,
    plan_readiness,
    resolve_integration,
    subject_kind_of,
    validate_content_transaction,
    validate_evidence_delta,
    validate_native_process_checkpoint,
    validate_owned_attempt_dispatch,
)
from skillweave.dispatch.work_contract import (
    ConfigurationSubject,
    ContentSubject,
    DeploymentSubject,
    IncidentSubject,
    RepositorySubject,
)
from skillweave.runtime.journal import EventJournal
from skillweave.runtime.store import NativeProcessService as StoreReexportedService
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
# SW-1511-RECOVERY criterion 1: a recovered controller resumes exactly one
# owned attempt and never replays a pending launch intent.
#
# These exercise the seam a real controller operation reaches: the recovering
# controller takes over the lease through the durable store (the store
# *re-exports* the native process service) and reads its checkpoint.  The
# decision is produced by ``resume()``/``checkpoint()`` -- the same call a
# takeover drives -- not by a helper invoked directly in isolation.
# ═══════════════════════════════════════════════════════════════════════════

def _live_identity_record() -> dict:
    """The recorded identity of a process that is *live right now*.

    Captured from the OS so ``verify`` observes the same facts at resume; no
    provider is probed and no wall-clock race is used -- the process recorded is
    the test interpreter itself, whose identity is stable for the whole test.
    """
    import os as _os
    from skillweave.runtime import process_identity as _pi

    ident = _pi.capture(_os.getpid())
    assert ident is not None, "the current process must be observable"
    return ident.to_dict()


def test_takeover_reaches_the_store_reexported_resume_seam():
    # The recovered controller resumes through the durable store's re-export,
    # not a direct import of the module -- this is the production seam a
    # controller takeover actually reaches.
    assert StoreReexportedService is NativeProcessService
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = StoreReexportedService(store, "controller-a")
        svc.acquire(ttl_seconds=0)  # expired: the successor takes over
        svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        lease, checkpoint = svc.resume(ttl_seconds=3600)
        assert lease.fencing_token == 2
        # The reconciled resume is reached by the real 'resume' operation and
        # carries the criterion-1 decision the checkpoint exists to publish.
        assert checkpoint.resume_decision_event is not None
        assert checkpoint.resume_decision_event["schema_id"] == RESUME_DECISION_SCHEMA_ID
        store.close()


def test_recovered_controller_adopts_one_verified_owned_attempt():
    # Positive path: the predecessor left exactly one in-flight attempt whose
    # recorded process identity still verifies, so the recovered controller
    # ADOPTS it by name -- it resumes that attempt rather than replaying the
    # predecessor's pending launch intent.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)  # expired: forcible takeover by the successor
        intent, _ = first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
            route="provider/model", session_id="sess-1",
        )
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        lease, checkpoint = recovered.resume(ttl_seconds=3600)
        assert lease.fencing_token == 2
        decision = checkpoint.resume_decision_event
        assert decision["decision"] == RESUME_ADOPTED
        assert decision["verified"] is True
        assert decision["intent_id"] == intent.intent_id
        assert decision["execution_scope"] == "run-1/task-a/1"
        # It adopted the *same* one attempt; it did not launch a second one.
        live = reopened.list_dispatch_intents(run_id="run-1", claiming_only=True)
        assert len(live) == 1
        assert live[0].intent_id == intent.intent_id
        reopened.close()


def test_recovered_controller_refuses_when_nothing_is_in_flight():
    # Negative path: nothing is in flight (the predecessor had drained its
    # work), so the recovered controller refuses by name instead of replaying
    # any launch intent.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        first.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        # Drain it: a reviewed attempt is terminal and no longer claiming.
        intent_id = store.list_dispatch_intents()[0].intent_id
        first.record_review(intent_id)
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(ttl_seconds=3600)
        decision = checkpoint.resume_decision_event
        assert decision["decision"] == RESUME_REFUSED_NO_OWNED_ATTEMPT
        assert decision["reason"] == "no_in_flight_attempt"
        assert decision["verified"] is False
        assert checkpoint.in_flight == ()
        reopened.close()


def test_recovered_controller_refuses_an_unverifiable_owned_attempt():
    # Negative path: an in-flight attempt whose recorded process identity can
    # no longer be observed (the recorded PID is not live) is NOT adopted --
    # the recovered controller refuses by name; it never re-dispatches (that
    # would be a duplicate launch) and never trusts a stale record.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        # A record whose pid is dead on arrival and whose start_time cannot
        # match any live process.  It is planted through the durable store --
        # the same seam the store-level duplicate test below uses -- so the
        # *read* resume path is what is exercised; the service's own write-time
        # identity guard (SW-1511-CMS-TRANSACTION c1-c3) would, correctly,
        # refuse to record an identity it cannot observe against the live OS.
        store.record_dispatch_intent(
            "run-1", "task-a", 1, "run-1/task-a/1",
            step=STEP_READY_DISPATCH, state=INTENT_LAUNCHED,
            process_identity={
                "pid": 1, "start_time": "0.0", "exec_path": "/nonexistent",
                "argv": ["nonexistent"],
            },
        )
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(ttl_seconds=3600)
        decision = checkpoint.resume_decision_event
        assert decision["decision"] == RESUME_REFUSED_NO_OWNED_ATTEMPT
        assert decision["verified"] is False
        assert decision["reason"]  # a named reason, never a bare False
        # The refusal did NOT replay a launch: still exactly the one intent.
        assert len(reopened.list_dispatch_intents(run_id="run-1")) == 1
        reopened.close()


def test_recovered_controller_never_replays_a_pending_launch_intent():
    # Criterion 1: 'without replaying a pending launch intent'.  After a
    # takeover, an attempt to re-launch the predecessor's scope is refused as a
    # duplicate -- the only admissible disposition of the pending intent is to
    # resume (adopt) it, which the checkpoint decision records.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        first.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(ttl_seconds=3600)
        assert checkpoint.resume_decision_event["decision"] in (
            RESUME_ADOPTED, RESUME_REFUSED_NO_OWNED_ATTEMPT,
        )
        # The replayed launch is refused: the pending intent is resumed, never
        # launched a second time.
        try:
            recovered.dispatch(run_id="run-1", task_id="task-a", attempt=1)
            assert False, "a pending launch intent must not be replayed"
        except DuplicateDispatchError:
            pass
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


def test_v1_checkpoint_without_a_resume_decision_round_trips():
    # Backward compatibility (criterion 1 is additive): a v1 record written
    # *before* the resume decision existed carries no ``resume_decision_event``;
    # it validates unchanged and reports the field as absent (None).
    record = NativeProcessCheckpoint(run_id="run-1", fencing_token=7).to_dict()
    assert record["resume_decision_event"] is None
    validate_native_process_checkpoint(record)
    # And an old record dict without the key at all still validates.
    record.pop("resume_decision_event")
    validate_native_process_checkpoint(record)


def test_resume_decision_with_an_unknown_decision_is_refused():
    # Fail closed: only the two declared decisions are admissible.  Both the
    # record constructor and the validator refuse an undeclared decision.
    event = {
        "schema_id": RESUME_DECISION_SCHEMA_ID,
        "decision": "fabricated_continuity",
        "verified": True,
    }
    try:
        NativeProcessCheckpoint(
            run_id="run-1", fencing_token=1, resume_decision_event=event,
        )
        assert False, "an undeclared resume decision must be refused"
    except ValueError:
        pass
    record = NativeProcessCheckpoint(run_id="run-1", fencing_token=1).to_dict()
    record["resume_decision_event"] = event
    try:
        validate_native_process_checkpoint(record)
        assert False, "an undeclared resume decision must be refused"
    except ValueError:
        pass


def test_resume_adoption_must_name_the_owned_attempt():
    # Fail closed: an *adoption* with no named intent/scope is refused, so a
    # decision can never claim to resume a nameless attempt.
    try:
        NativeProcessCheckpoint(
            run_id="run-1", fencing_token=1,
            resume_decision_event={
                "schema_id": RESUME_DECISION_SCHEMA_ID,
                "decision": RESUME_ADOPTED, "verified": True,
            },
        )
        assert False, "an adoption must name the owned intent it resumes"
    except ValueError:
        pass


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


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 4: the compact changed-state/evidence view
# ═══════════════════════════════════════════════════════════════════════════

def _journal_record(sequence, event_type="state_transition", payload=None):
    return {
        "sequence": sequence,
        "run_id": "run-1",
        "event_type": event_type,
        "payload": payload or {},
        "timestamp": "2026-10-14T00:00:00+00:00",
    }


def test_unchanged_poll_returns_an_empty_delta_with_the_same_cursor():
    records = [_journal_record(1), _journal_record(2)]
    delta = build_evidence_delta(records, "run-1", journal_cursor=2)
    assert delta.new_evidence == ()
    assert delta.journal_cursor == 2  # unchanged: nothing new to read
    assert delta.coverage_complete is True


def test_new_journal_facts_advance_the_cursor_and_are_reported():
    records = [_journal_record(1), _journal_record(2), _journal_record(3)]
    delta = build_evidence_delta(records, "run-1", journal_cursor=1)
    # The changed window moved the cursor from 1 to the newest fact (3); a poll
    # handed back 3 sees nothing further.
    assert delta.journal_cursor == 3
    assert delta.monotonically_increasing_cursor is True
    assert delta.coverage_complete is True
    settled = build_evidence_delta(records, "run-1", journal_cursor=3)
    assert settled.journal_cursor == 3
    assert settled.new_evidence == ()


def test_gate_denominator_is_disjoint_and_process_success_is_never_a_gate():
    # A process's clean exit is not a gate verdict: only explicitly accepted
    # gates are authoritative, everything else stays required.
    view = EvidenceDelta(
        run_id="run-1",
        gates={"G1": GATE_ACCEPTED, "G2": "required", "G3": "pending"},
    )
    assert view.accepted_gates == ("G1",)
    assert set(view.accepted_gates) & set(view.required_gates) == set()
    assert set(view.accepted_gates) | set(view.required_gates) == {"G1", "G2", "G3"}


def test_a_gate_both_accepted_and_required_is_refused():
    # The accepted/required denominators are derived from ``gates`` so they are
    # disjoint by construction, and every gate lands in exactly one set.
    view = EvidenceDelta(
        run_id="run-1",
        gates={"G1": GATE_ACCEPTED, "G2": "required", "G3": "pending"},
    )
    assert set(view.accepted_gates).isdisjoint(view.required_gates)
    assert set(view.accepted_gates) | set(view.required_gates) == {"G1", "G2", "G3"}

    # A tampered record that folds a process outcome into a gate status cannot be
    # reconstructed as a valid view: the boundary refuses the foreign status
    # instead of accepting an ambiguous denominator.
    good = EvidenceDelta(run_id="run-1", gates={"G1": GATE_ACCEPTED}).to_dict()
    try:
        validate_evidence_delta({**good, "gates": {"G1": "merged"}})
        assert False, "a process-derived gate status must be refused"
    except ValueError:
        pass
    # The same boundary refuses a record that names no run.
    try:
        validate_evidence_delta({**good, "run_id": ""})
        assert False, "a view that names no run must be refused"
    except ValueError:
        pass


def test_process_success_cannot_be_folded_into_a_gate_status():
    try:
        EvidenceDelta(run_id="run-1", gates={"G1": "succeeded"})
        assert False, "a process-derived gate status must be refused"
    except ValueError:
        pass
    assert GATE_ACCEPTED in ACCEPTED_GATE_STATUSES


def test_unsupported_evidence_view_version_fails_closed():
    record = EvidenceDelta(run_id="run-1").to_dict()
    record["schema_version"] = "2.0.0"
    try:
        validate_evidence_delta(record)
        assert False, "an unsupported evidence-view version must fail closed"
    except UnsupportedContractVersion:
        pass
    try:
        EvidenceDelta(run_id="run-1", schema_version="9.9.9")
        assert False
    except UnsupportedContractVersion:
        pass


def test_evidence_view_fingerprint_is_cursor_independent():
    a = EvidenceDelta(run_id="run-1", gates={"G1": GATE_ACCEPTED}, journal_cursor=5)
    b = EvidenceDelta(run_id="run-1", gates={"G1": GATE_ACCEPTED}, journal_cursor=9)
    # The cursor a caller hands in is not an observation: the header matches.
    assert evidence_view_fingerprint(a.to_dict()) == evidence_view_fingerprint(b.to_dict())
    header = evidence_view_header(a.to_dict())
    assert "journal_cursor" not in header
    assert header["run_id"] == "run-1"
    # A real change (the gate denominator) changes the fingerprint.
    c = EvidenceDelta(run_id="run-1", gates={"G1": "required"}, journal_cursor=5)
    assert evidence_view_fingerprint(a.to_dict()) != evidence_view_fingerprint(c.to_dict())


def test_quota_exhausted_architect_continues_execution_and_gates():
    decision = plan_continuation(
        ARCHITECT_QUOTA_EXHAUSTED, prior_session_id="sess-1", prior_route="provider/model"
    )
    assert decision.continue_execution is True
    assert decision.gates_continue is True
    assert decision.resume_same_route is True
    assert decision.escalate is False
    assert decision.interrupt_delegated_workers is False
    assert decision.wait is True
    assert "sess-1" in decision.reason


def test_healthy_architect_continues_with_no_wait():
    decision = plan_continuation(ARCHITECT_HEALTHY)
    assert decision.continue_execution is True
    assert decision.gates_continue is True
    assert decision.wait is False
    assert decision.resume_same_route is False


def test_quota_is_not_a_repair_verdict_or_escalation_permission():
    try:
        ContinuationDecision(
            architect_health=ARCHITECT_QUOTA_EXHAUSTED,
            resume_same_route=False,
            wait=True,
        )
        assert False, "a quota-exhausted architect must resume the same route"
    except ValueError:
        pass
    try:
        ContinuationDecision(
            architect_health=ARCHITECT_QUOTA_EXHAUSTED,
            resume_same_route=True,
            escalate=True,
        )
        assert False, "quota must never be an escalation permission"
    except ValueError:
        pass


def test_a_stopped_architect_does_not_interrupt_its_delegated_workers():
    try:
        ContinuationDecision(
            architect_health=ARCHITECT_QUOTA_EXHAUSTED,
            resume_same_route=True,
            interrupt_delegated_workers=True,
        )
        assert False, "a stopped architect must not interrupt its workers"
    except ValueError:
        pass
    try:
        ContinuationDecision(architect_health=ARCHITECT_HEALTHY, interrupt_delegated_workers=True)
        assert False
    except ValueError:
        pass


def test_unknown_architect_health_is_refused():
    for health in ("degraded", "", "quota_exhausted"):
        try:
            plan_continuation(health)
            assert False, f"unknown architect health {health!r} must be refused"
        except ValueError:
            pass


def test_service_evidence_delta_reads_the_durable_journal_after_the_cursor():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        journal = EventJournal(store)
        journal.append("run-1", event_type="state_transition", payload={"gates": {"G1": "accepted"}})
        journal.append("run-1", event_type="batch_complete", payload={"task_id": "task-a"})
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        empty = svc.evidence_delta("run-1", journal_cursor=2)
        assert empty.new_evidence == ()
        assert empty.journal_cursor == 2
        fresh = svc.evidence_delta("run-1", journal_cursor=0)
        assert fresh.journal_cursor == 2
        assert store.last_journal_sequence("run-1") == 2
        store.close()


def test_service_delta_names_new_evidence_from_durable_state():
    # The compact view must name the evidence references it is *newly* aware of
    # through the real service path -- not only when a bare list is handed to
    # ``build_evidence_delta``.  The evidence source is bound to the durable
    # store, so naming it below the journal cursor's timestamp must be refused
    # as stale rather than reported as new.
    from skillweave.runtime.registry import ArtifactReceipt

    def _receipt(artifact_id: str, created_at: str, sha: str) -> ArtifactReceipt:
        return ArtifactReceipt(
            artifact_id=artifact_id,
            # A distinct digest per receipt: ``save_evidence`` is content-
            # addressed, so identical bytes would dedupe to one row.
            sha256=sha,
            schema_version="1",
            producer_command="pytest",
            subject_repo="skillweave",
            subject_commit=_SHA,
            created_at=created_at,
            evidence_type="artifact",
            purpose="criterion-4",
            metadata={"run_id": "run-1"},
        )

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        journal = EventJournal(store)
        journal.append("run-1", event_type="state_transition", payload={"gates": {"G1": "accepted"}})
        # A receipt from before the cursor was first observed is *not* new.
        store.save_evidence(_receipt("evd-old", "2026-01-01T00:00:00+00:00", "b" * 40))
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)

        seen = svc.evidence_delta("run-1", journal_cursor=0, now="2026-01-02T00:00:00+00:00")
        assert seen.new_evidence == ()
        assert seen.coverage_complete is True

        # A receipt created after the cursor's timestamp is a new reference.
        store.save_evidence(_receipt("evd-new", "2026-01-03T00:00:00+00:00", "c" * 40))
        fresh = svc.evidence_delta("run-1", journal_cursor=0, now="2026-01-02T00:00:00+00:00")
        assert fresh.new_evidence == ("evd-new",)
        assert fresh.coverage_complete is True
        store.close()


def test_service_delta_refuses_to_claim_completeness_without_an_evidence_source():
    # A caller who skips evidence over a record source that binds no evidence
    # store cannot have its completeness proven: the view must report that
    # rather than invent an empty denominator.
    delta = build_evidence_delta(
        [_journal_record(1)], "run-1", journal_cursor=0, since_evidence_index=1
    )
    assert delta.coverage_complete is False
    assert delta.new_evidence == ()


def test_service_records_a_compact_view_for_a_recovering_controller():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        delta = svc.evidence_delta("run-1", journal_cursor=0)
        svc.record_evidence_delta(delta)
        stored = store.get_evidence_delta("run-1")
        assert stored is not None
        assert stored["run_id"] == "run-1"
        assert stored["journal_cursor"] == delta.journal_cursor
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 5: repository and non-repository subjects under one controller
# ═══════════════════════════════════════════════════════════════════════════

_SHA = "a" * 40


def test_subject_kind_is_read_for_every_variant():
    assert subject_kind_of(RepositorySubject(repo="org/repo", commit=_SHA)) == "repository"
    assert subject_kind_of(ContentSubject(channel="c", content_id="e", revision="r")) == "content"
    assert subject_kind_of(
        ConfigurationSubject(config_id="c", environment="e", version="v")
    ) == "configuration"
    assert subject_kind_of(
        DeploymentSubject(target="t", environment="e", artifact_digest="d")
    ) == "deployment"
    assert subject_kind_of(
        IncidentSubject(incident_id="i", state="open", severity="high")
    ) == "incident"


def test_unknown_subject_kind_is_refused_never_inferred_repository():
    class _Bogus:
        kind = "not-a-kind"

    try:
        subject_kind_of(_Bogus())
        assert False, "an unknown subject kind must be refused"
    except UnknownSubjectKind:
        pass
    try:
        subject_kind_of({"kind": "repository?"})
        assert False
    except UnknownSubjectKind:
        pass


def test_repository_and_non_repository_integrate_by_different_modes():
    repo = RepositorySubject(repo="org/repo", commit=_SHA)
    content = ContentSubject(channel="c", content_id="e", revision="r")
    assert is_repository_subject(repo) is True
    assert is_repository_subject(content) is False
    assert integration_mode_for(repo) == INTEGRATION_GIT_MERGE
    assert integration_mode_for(content) == INTEGRATION_CONTENT_TRANSACTION


def test_resolve_integration_refuses_a_wrong_mode_record():
    content = ContentSubject(channel="c", content_id="e", revision="r")
    repo = RepositorySubject(repo="org/repo", commit=_SHA)
    assert resolve_integration(content) == INTEGRATION_CONTENT_TRANSACTION
    # A record that declares the wrong mode for the subject is refused.
    try:
        resolve_integration(content, record={"integration_mode": INTEGRATION_GIT_MERGE})
        assert False, "a content subject must not integrate via git_merge"
    except WrongIntegrationModeError:
        pass
    try:
        resolve_integration(repo, record={"integration_mode": INTEGRATION_CONTENT_TRANSACTION})
        assert False, "a repository subject must not integrate via content_transaction"
    except WrongIntegrationModeError:
        pass
    # A matching mode is fine.
    assert resolve_integration(
        content, record={"integration_mode": INTEGRATION_CONTENT_TRANSACTION}
    ) == INTEGRATION_CONTENT_TRANSACTION


def test_content_transaction_requires_verification_and_bounded_restore():
    base = dict(
        run_id="run-1",
        subject="content:c/e@r",
        subject_kind="content",
        baseline="sha256:baseline",
        applied_digest="sha256:applied",
    )
    try:
        ContentTransaction(**base, verify_readback=False, restore_bounded=True)
        assert False, "an unverified transaction must be refused"
    except ValueError:
        pass
    try:
        ContentTransaction(**base, verify_readback=True, restore_bounded=False)
        assert False, "an unbounded transaction must be refused"
    except ValueError:
        pass


def test_content_transaction_refuses_a_repository_subject():
    try:
        ContentTransaction(
            run_id="run-1",
            subject="repository:org/repo@" + _SHA,
            subject_kind="repository",
            baseline="sha256:b",
            applied_digest="sha256:a",
            verify_readback=True,
            restore_bounded=True,
        )
        assert False, "a repository must integrate through Git, not a content txn"
    except SubjectMismatchError:
        pass


def test_content_transaction_readback_must_reproduce_the_apply():
    base = dict(
        run_id="run-1",
        subject="content:c/e@r",
        subject_kind="content",
        baseline="sha256:b",
        applied_digest="sha256:applied",
        verify_readback=True,
        restore_bounded=True,
    )
    try:
        ContentTransaction(**base, readback_digest="sha256:different")
        assert False, "a readback that contradicts the apply must be refused"
    except ValueError:
        pass
    ok = ContentTransaction(**base, readback_digest="sha256:applied", applied=True)
    assert ok.verified() is True
    assert ok.applied is True


def test_content_transaction_round_trips_and_validates():
    txn = ContentTransaction(
        run_id="run-1",
        subject="content:c/e@r",
        subject_kind="content",
        txn_id="txn-1",
        baseline="sha256:b",
        applied_digest="sha256:a",
        readback_digest="sha256:a",
        applied=True,
        verify_readback=True,
        restore_bounded=True,
    )
    record = txn.to_dict()
    assert record["schema_id"] == CONTENT_TXN_SCHEMA_ID
    assert record["schema_version"] == CONTENT_TXN_SCHEMA_VERSION
    validate_content_transaction(record)


def test_unsupported_content_transaction_version_fails_closed():
    record = ContentTransaction(
        run_id="run-1", subject="content:c/e@r", subject_kind="content",
        baseline="sha256:b", applied_digest="sha256:a",
        verify_readback=True, restore_bounded=True,
    ).to_dict()
    record["schema_version"] = "3.0.0"
    try:
        validate_content_transaction(record)
        assert False, "an unsupported content-transaction version must fail closed"
    except UnsupportedContractVersion:
        pass


def test_service_applies_and_verifies_a_content_subject():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        intent, _ = svc.dispatch(
            run_id="run-1", task_id="task-a", attempt=1, step=STEP_READY_DISPATCH
        )
        content = ContentSubject(channel="c", content_id="e", revision="r")
        txn = svc.record_content_integration(
            intent.intent_id,
            content,
            baseline_digest="sha256:base",
            applied_digest="sha256:applied",
        )
        assert txn.verified() is True
        assert txn.subject_kind == "content"
        assert txn.bypass_git_merge is True  # no mandatory Git merge
        # The owned intent advanced to the controlled-integration step.
        advanced = store.get_dispatch_intent(intent.intent_id)
        assert advanced.state == INTENT_INTEGRATED
        assert advanced.step == STEP_CONTROLLED_INTEGRATION
        # And the transaction is durable, readable by a recovering controller.
        stored = store.get_content_transaction("run-1", txn.txn_id)
        assert stored is not None
        assert stored["applied_digest"] == "sha256:applied"
        assert stored["readback_digest"] == "sha256:applied"
        store.close()


def test_service_refuses_to_integrate_a_repository_through_a_content_transaction():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = NativeProcessService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        intent, _ = svc.dispatch(
            run_id="run-1", task_id="task-a", attempt=1, step=STEP_READY_DISPATCH
        )
        repo = RepositorySubject(repo="org/repo", commit=_SHA)
        try:
            svc.record_content_integration(
                intent.intent_id, repo,
                baseline_digest="sha256:b", applied_digest="sha256:a",
            )
            assert False, "a repository must integrate through Git, not a content txn"
        except WrongIntegrationModeError:
            pass
        # The intent is untouched by the refused integration.
        assert store.get_dispatch_intent(intent.intent_id).state == INTENT_LAUNCHED
        store.close()


# ═══════════════════════════════════════════════════════════════════════════
# Interpreter-compatibility regression (the controller-measured correction)
#
# The public scheduling dataclass ``dagscheduler.Sequence`` shares the name of
# ``typing.Sequence``.  Before the fix, the *generic* annotation
# ``lane_groups: Sequence[Sequence[Lane]]`` resolved to the local class and
# raised ``TypeError: type 'Sequence' is not subscriptable`` at import time on
# CPython 3.11/3.12 (which evaluate annotations eagerly).  These tests fail on
# every interpreter if that collision is reintroduced, rather than passing
# silently on 3.14's lazy-annotation semantics.
# ═══════════════════════════════════════════════════════════════════════════

def test_dagscheduler_and_store_imports_bind_cleanly_through_the_runtime_package():
    # ``store`` -> ``native_process`` -> ``dagscheduler``; importing the
    # observer package traverses that same chain the controller observed.
    import skillweave.runtime.observer  # noqa: F401  (import is the assertion)
    import skillweave.runtime.store  # noqa: F401
    assert _dagscheduler.Sequence is DagSequence


def test_generic_annotations_survive_get_type_hints():
    import typing

    main_hints = get_type_hints(_dagscheduler.build_batches)
    assert main_hints["tasks"] == typing.Sequence[Task]

    split_hints = get_type_hints(_split_by_max_parallel)
    assert split_hints["lane_groups"] == typing.Sequence[typing.Sequence[_dagscheduler.Lane]]

    record_hints = get_type_hints(SelectionLedger._record)
    assert record_hints["candidates"] == typing.Sequence[str]
    assert record_hints["rejected_routes"] == typing.Sequence[str]


def test_public_sequence_annotation_names_the_local_dataclass_not_typing():
    hints = get_type_hints(build_sessions)
    assert hints["sequence"] is DagSequence


# ═══════════════════════════════════════════════════════════════════════════
# SW-1511-RECOVERY criterion 2: harness-continuation-v1.
#
# Actual session resume is used *only* for an attested harness capability; an
# unsupported resume reports a named limitation and continues from the durable
# checkpoint -- the predecessor's session UUID is never carried forward as if
# the session were still live.  These exercise the seam a real controller
# takeover reaches: ``NativeProcessService.resume`` (the same call the durable
# store re-exports) produces the ``harness_continuation`` statement, and the
# checkpoint carries it.
# ═══════════════════════════════════════════════════════════════════════════

def test_resume_without_attested_capability_reports_named_checkpoint_continuation():
    # Negative path: the adapter does NOT attest session resume, so the
    # recovered controller continues from the durable checkpoint and reports
    # the limitation by name -- no session UUID is carried forward.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)  # expired: forcible takeover
        first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
            route="provider/model", session_id="sess-live",
        )
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600, resume_capability_attested=False,
        )
        continuation = checkpoint.harness_continuation
        assert continuation is not None
        assert continuation["schema_id"] == HARNESS_CONTINUATION_SCHEMA_ID
        assert continuation["mode"] == CONTINUATION_CHECKPOINT
        assert continuation["session_id"] == ""  # never a fabricated UUID
        assert continuation["limitation"] == LIMITATION_UNSUPPORTED_RESUME
        assert continuation["reason"]  # a named reason, never a bare False
        assert continuation["resume_capability_attested"] is False
        # The checkpoint still adopts the one verified owned attempt (criterion
        # 1) -- the continuation is *how* it continues, not *whether*.
        assert checkpoint.resume_decision_event["decision"] == RESUME_ADOPTED
        reopened.close()


def test_resume_with_attested_capability_resumes_the_actual_session_uuid():
    # Positive path: the adapter attests session resume and the predecessor's
    # session identity is durably recorded, so the recovered controller resumes
    # the *actual* session UUID (the same one), not a fabricated continuation.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        intent, _ = first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
            route="provider/model", session_id="sess-live",
        )
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600, resume_capability_attested=True,
        )
        continuation = checkpoint.harness_continuation
        assert continuation is not None
        assert continuation["mode"] == CONTINUATION_SESSION_RESUME
        assert continuation["resume_capability_attested"] is True
        assert continuation["session_id"] == "sess-live"
        assert continuation["intent_id"] == intent.intent_id
        assert continuation["limitation"] == ""
        reopened.close()


def test_resume_attested_but_no_recorded_session_falls_to_checkpoint():
    # Fail-closed: the adapter attests the capability but no prior session
    # identity was durably recorded, so an actual session resume is NOT
    # fabricated -- the continuation names the limitation and carries no UUID.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
            route="provider/model", session_id="",  # no session recorded
        )
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600, resume_capability_attested=True,
        )
        continuation = checkpoint.harness_continuation
        assert continuation["mode"] == CONTINUATION_CHECKPOINT
        assert continuation["session_id"] == ""
        assert continuation["limitation"] == LIMITATION_SESSION_UNKNOWN
        reopened.close()


def test_resume_without_a_verified_owned_attempt_never_resumes_the_old_session():
    # Negative path: nothing verifiable is in flight, so the recovered
    # controller refuses the adoption (criterion 1) AND must not resume the
    # predecessor's session -- even with the capability attested, an
    # un-adopted attempt has no owned session to continue.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        # A recorded identity that can no longer be observed: not adoptable.
        # Planted through the durable store -- the same seam the store-level
        # duplicate test below uses -- so the *read* resume path (and its
        # refusal to carry a session forward) is what is exercised; the
        # service's write-time identity guard (SW-1511-CMS-TRANSACTION c1-c3)
        # would, correctly, refuse to record an unobservable identity.
        store.record_dispatch_intent(
            "run-1", "task-a", 1, "run-1/task-a/1",
            step=STEP_READY_DISPATCH, state=INTENT_LAUNCHED,
            route="provider/model", session_id="sess-stale",
            process_identity={
                "pid": 1, "start_time": "0.0", "exec_path": "/nonexistent",
                "argv": ["nonexistent"],
            },
        )
        store.close()

        reopened = _store(tmp)
        recovered = NativeProcessService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600, resume_capability_attested=True,
        )
        assert checkpoint.resume_decision_event["decision"] == (
            RESUME_REFUSED_NO_OWNED_ATTEMPT
        )
        continuation = checkpoint.harness_continuation
        assert continuation["mode"] == CONTINUATION_CHECKPOINT
        assert continuation["session_id"] == ""
        assert continuation["limitation"] == LIMITATION_SESSION_UNKNOWN
        reopened.close()


# The constructor/planner fail-closed invariants are exercised directly below
# (``test_harness_continuation_*`` and ``test_plan_harness_continuation_*``),
# each pinning one invariant so removing that guard fails exactly its test;
# the two tests here add the *durable* v1 round-trip and the reconstruction
# guard.


# ── criterion 2 fail-closed invariants (one test per guard) ─────────────────

def test_harness_continuation_refuses_an_unsupported_schema_version():
    # Invariant 1: an unknown schema version is refused, never approximated.
    assert HARNESS_CONTINUATION_SCHEMA_VERSION in (
        SUPPORTED_HARNESS_CONTINUATION_VERSIONS
    )
    try:
        HarnessContinuation(
            run_id="run-1", mode=CONTINUATION_CHECKPOINT,
            limitation=LIMITATION_UNSUPPORTED_RESUME,
            schema_version="9.9.9",
        )
        assert False, "an unsupported schema version must be refused"
    except UnsupportedContractVersion:
        pass


def test_harness_continuation_requires_a_run_id():
    # Invariant 2: a continuation must name its run.
    try:
        HarnessContinuation(run_id="", mode=CONTINUATION_CHECKPOINT,
                            limitation=LIMITATION_UNSUPPORTED_RESUME)
        assert False, "a continuation without a run_id must be refused"
    except ValueError:
        pass


def test_harness_continuation_refuses_an_unknown_mode():
    # Invariant 3: the mode vocabulary is closed.
    try:
        HarnessContinuation(run_id="run-1", mode="teleport",
                            limitation=LIMITATION_UNSUPPORTED_RESUME)
        assert False, "an unknown continuation mode must be refused"
    except ValueError:
        pass


def test_session_resume_requires_the_attested_capability():
    # Invariant 4 (the core "never fabricate UUID continuity" guard): a session
    # resume without the attestation is refused at construction.
    try:
        HarnessContinuation(
            run_id="run-1", mode=CONTINUATION_SESSION_RESUME,
            resume_capability_attested=False, session_id="sess-x",
        )
        assert False, "a session resume without attestation must be refused"
    except ValueError:
        pass


def test_session_resume_must_name_the_prior_session():
    # Invariant 5: an attested session resume with no session id is refused.
    try:
        HarnessContinuation(
            run_id="run-1", mode=CONTINUATION_SESSION_RESUME,
            resume_capability_attested=True, session_id="",
        )
        assert False, "a session resume must name the prior session"
    except ValueError:
        pass


def test_checkpoint_continuation_must_name_a_limitation():
    # Invariant 6: a checkpoint continuation reports a named limitation.
    try:
        HarnessContinuation(
            run_id="run-1", mode=CONTINUATION_CHECKPOINT, limitation="",
        )
        assert False, "a checkpoint continuation must name its limitation"
    except ValueError:
        pass


def test_checkpoint_continuation_must_not_carry_a_session_id():
    # Invariant 7 (the guard that previously had no covering test): a checkpoint
    # continuation that carries a session id forward is refused -- that would be
    # fabricated UUID continuity.
    try:
        HarnessContinuation(
            run_id="run-1", mode=CONTINUATION_CHECKPOINT,
            limitation=LIMITATION_UNSUPPORTED_RESUME, session_id="sess-smuggled",
        )
        assert False, "a checkpoint continuation must not carry a session id"
    except ValueError:
        pass


def test_plan_harness_continuation_resumes_only_attested_with_a_session():
    # Planner purity, positive: attested + a recorded session resumes that id.
    decision = plan_harness_continuation(
        "run-1", resume_capability_attested=True, prior_session_id="sess-live",
        intent_id="intent-1",
    )
    assert decision.mode == CONTINUATION_SESSION_RESUME
    assert decision.session_id == "sess-live"
    assert decision.intent_id == "intent-1"
    assert decision.limitation == ""


def test_plan_harness_continuation_unattested_names_unsupported_resume():
    # Planner purity, negative: no attestation -> checkpoint, unsupported name.
    decision = plan_harness_continuation(
        "run-1", resume_capability_attested=False, prior_session_id="sess-live",
    )
    assert decision.mode == CONTINUATION_CHECKPOINT
    assert decision.session_id == ""
    assert decision.limitation == LIMITATION_UNSUPPORTED_RESUME


def test_plan_harness_continuation_attested_but_no_session_names_unknown():
    # Planner purity, fail-closed: attested but no recorded session -> checkpoint
    # with the session-identity-unknown limitation, never a fabricated id.
    decision = plan_harness_continuation(
        "run-1", resume_capability_attested=True, prior_session_id="",
    )
    assert decision.mode == CONTINUATION_CHECKPOINT
    assert decision.session_id == ""
    assert decision.limitation == LIMITATION_SESSION_UNKNOWN


# ── criterion 2 backward compatibility ─────────────────────────────────────

def test_v1_checkpoint_without_a_continuation_round_trips_unchanged():
    # Additive contract: a v1 checkpoint produced *without* a harness
    # continuation carries ``harness_continuation: None`` and round-trips
    # through validate unchanged -- an existing v1 record is never disturbed.
    record = {
        "schema_id": "https://skillweave.dev/schemas/storage/"
                     "native-process-checkpoint/v1",
        "schema_version": "1.0.0",
        "run_id": "run-legacy",
        "fencing_token": 3,
        "journal_offset": 0,
        "in_flight": [["run-legacy/task-a/1", "ready_dispatch"]],
        "route": "",
        "session_id": "",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "resume_decision_event": None,
    }
    validate_native_process_checkpoint(record)
    # The reconstructed record defaults the continuation to None.
    reconstructed = NativeProcessCheckpoint(
        run_id="run-legacy", fencing_token=3, journal_offset=0,
        in_flight=(("run-legacy/task-a/1", "ready_dispatch"),),
    )
    assert reconstructed.harness_continuation is None
    assert reconstructed.to_dict()["harness_continuation"] is None


def test_a_checkpoint_cannot_smuggle_a_fabricated_session_resume():
    # A checkpoint carrying a continuation that claims a session resume without
    # the attestation is refused when the record is reconstructed.
    try:
        NativeProcessCheckpoint(
            run_id="run-1", fencing_token=1, journal_offset=0,
            harness_continuation={
                "mode": CONTINUATION_SESSION_RESUME,
                "resume_capability_attested": False,
                "session_id": "sess-fabricated",
            },
        )
        assert False, "a checkpoint must not carry a fabricated session resume"
    except ValueError:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# SW-1511-RECOVERY criterion 3: background probe children are drained by owned
# identity; heartbeat-only stalled children do not indefinitely suppress a
# completed review or occupy useful slots.
#
# These exercise the seam a real controller operation reaches: the recovered
# controller takes over through the durable store's re-exported service and
# calls ``resume``/``drain_background_probe_children`` -- the same calls a
# takeover drives.  The identity verification is the *real* one
# (``process_identity.verify`` re-observing the OS), not a stub; a drained
# child's recorded identity is a live process the test actually owns.
# ═══════════════════════════════════════════════════════════════════════════


def _live_child(identifier: str = "probe-1", **extra) -> dict:
    """A background child whose recorded identity is a live owned process.

    Recorded from the OS (this test interpreter) so the real
    ``process_identity.verify`` observes a ``match`` -- the positive owned-
    identity path.  ``scope``/``child_id`` name the child distinctly.
    """
    child = {
        "child_id": identifier,
        "scope": f"run-1/child/{identifier}",
        "recorded_identity": _live_identity_record(),
        "kind": CHILD_PROBE,
        "progressing": True,
    }
    child.update(extra)
    return child


def test_drained_by_owned_identity_only_when_the_child_verifies():
    # Positive path: a background probe child whose recorded identity still
    # re-verifies as the owned process is drained by owned identity -- the
    # drain is reached through the resumed controller's real drain seam.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
        )
        store.close()

        reopened = _store(tmp)
        recovered = StoreReexportedService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600,
            background_children=[_live_child("probe-1")],
        )
        report = checkpoint.child_drain_report
        assert report is not None
        assert report["schema_id"] == CHILD_DRAIN_SCHEMA_ID
        assert len(report["children"]) == 1
        child = report["children"][0]
        assert child["outcome"] == CHILD_DRAINED
        assert child["verified"] is True
        assert child["useful_slot"] == USEFUL_SLOT_FREED
        assert report["occupied_slots"] == 0
        reopened.close()


def test_drain_refuses_a_child_whose_identity_no_longer_verifies():
    # Negative path: the child's recorded identity is a dead/foreign process,
    # so the real OS verification does NOT return ``match`` -- the drain is
    # refused by name and the child is never reported drained.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
        )
        store.close()

        reopened = _store(tmp)
        recovered = StoreReexportedService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600,
            background_children=[{
                "child_id": "probe-stale",
                "scope": "run-1/child/probe-stale",
                "recorded_identity": {
                    "pid": 1, "start_time": "0.0",
                    "exec_path": "/nonexistent", "argv": ["nonexistent"],
                },
                "kind": CHILD_PROBE,
            }],
        )
        child = checkpoint.child_drain_report["children"][0]
        assert child["outcome"] == CHILD_DRAIN_REFUSED
        assert child["verified"] is False
        assert child["drain_refused_reason"]  # a named reason, never a bare False
        # A refused probe we did not end still occupies its useful slot.
        assert child["useful_slot"] == USEFUL_SLOT_OCCUPIED
        reopened.close()


def test_no_recorded_identity_refuses_the_drain_distinctly():
    # Fail-closed: a child with no recorded identity at all is refused with the
    # distinct ``no_recorded_identity`` reason -- the drain never invents an
    # owned identity to signal.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = StoreReexportedService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        report = svc.drain_background_probe_children(
            [{"child_id": "probe-norec", "scope": "run-1/child/probe-norec"}]
        )
        child = report.children[0]
        assert child["outcome"] == CHILD_DRAIN_REFUSED
        assert child["refusal"] == CHILD_DRAIN_REFUSED_NO_RECORD
        assert child["verified"] is False
        store.close()


def test_heartbeat_only_stalled_child_is_exempt_from_useful_slots():
    # Criterion 3: a heartbeat-only stalled child emits liveness traffic but no
    # useful progress.  It is a stall, not a worker: it holds NO useful slot,
    # and it is refused a drain (a heartbeat is not proof of owned identity).
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = StoreReexportedService(store, "controller-a")
        svc.acquire(ttl_seconds=3600)
        svc.dispatch(run_id="run-1", task_id="task-a", attempt=1)
        report = svc.drain_background_probe_children(
            [{
                "child_id": "probe-stall",
                "scope": "run-1/child/probe-stall",
                "kind": CHILD_HEARTBEAT_ONLY,
                "heartbeat_only": True,
                # Even with a live identity, a heartbeat-only child is a stall:
                # its heartbeat is not proof it still *is* the owned attempt.
                "recorded_identity": _live_identity_record(),
            }],
            review_completed=True,
        )
        child = report.children[0]
        assert child["outcome"] == CHILD_DRAIN_REFUSED
        assert child["kind"] == CHILD_HEARTBEAT_ONLY
        assert child["useful_slot"] == USEFUL_SLOT_EXEMPT_STALLED
        assert child["verified"] is False
        # The stalled child holds no useful slot: useful occupancy measured
        # separately from heartbeat traffic is zero.
        assert report.occupied_slots == 0
        store.close()


def test_heartbeat_only_stall_does_not_suppress_a_completed_review():
    # Criterion 3: a completed review is not indefinitely suppressed by a
    # heartbeat-only stalled child.  With the review completed the drain runs
    # through the real `resume` seam and returns a report in which the stall
    # holds no useful slot -- the completed review is released, not withheld.
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        first = NativeProcessService(store, "controller-a")
        first.acquire(ttl_seconds=0)
        intent, _ = first.dispatch(
            run_id="run-1", task_id="task-a", attempt=1,
            process_identity=_live_identity_record(),
        )
        store.close()

        reopened = _store(tmp)
        recovered = StoreReexportedService(reopened, "controller-b")
        _, checkpoint = recovered.resume(
            ttl_seconds=3600,
            background_children=[
                # One genuinely owned probe child, drained by identity ...
                _live_child("probe-owned"),
                # ... and one heartbeat-only stall, which must not occupy a
                # useful slot nor suppress the completed review.
                {
                    "child_id": "probe-stall",
                    "scope": "run-1/child/probe-stall",
                    "kind": CHILD_HEARTBEAT_ONLY,
                    "heartbeat_only": True,
                    "recorded_identity": _live_identity_record(),
                },
            ],
            review_completed=True,
        )
        report = checkpoint.child_drain_report
        assert report is not None
        stalled = [c for c in report["children"]
                   if c["kind"] == CHILD_HEARTBEAT_ONLY][0]
        assert stalled["useful_slot"] == USEFUL_SLOT_EXEMPT_STALLED
        # Exactly one useful slot is occupied by neither child: the drained
        # owned probe freed its slot, the stall holds none.
        assert report["occupied_slots"] == 0
        # The two distinct facts are separate: the owned probe is drained, the
        # stall is not.
        owned = [c for c in report["children"]
                 if c["child_id"] == "probe-owned"][0]
        assert owned["outcome"] == CHILD_DRAINED
        assert stalled["outcome"] == CHILD_DRAIN_REFUSED
        reopened.close()


def test_plan_child_drain_is_a_pure_classifier_over_verification():
    # The planner is a pure function of the caller's verification: a ``match``
    # drains, anything else refuses; a heartbeat-only child is exempted.
    from skillweave.runtime.process_identity import IdentityVerification

    matched = IdentityVerification(ok=True, reason="match", recorded_pid=123)
    refused = IdentityVerification(ok=False, reason="recycled_pid", recorded_pid=123)
    report = plan_child_drain(
        "run-pure",
        [
            _Fact("probe-ok", "s1", {"pid": 123}, matched),
            _Fact("probe-bad", "s2", {"pid": 123}, refused),
            _Fact("probe-stall", "s3", {"pid": 123}, matched,
                  kind=CHILD_HEARTBEAT_ONLY, heartbeat_only=True),
        ],
    )
    assert isinstance(report, OwnedChildDrain)
    by_id = {c["child_id"]: c for c in report.children}
    assert by_id["probe-ok"]["outcome"] == CHILD_DRAINED
    assert by_id["probe-ok"]["verified"] is True
    assert by_id["probe-bad"]["outcome"] == CHILD_DRAIN_REFUSED
    assert by_id["probe-bad"]["drain_refused_reason"] == "recycled_pid"
    assert by_id["probe-stall"]["useful_slot"] == USEFUL_SLOT_EXEMPT_STALLED
    assert report.drained == ("probe-ok",)
    assert report.refused == ("probe-bad", "probe-stall")
    assert report.occupied_slots == 1  # only the refused live probe


class _Fact:
    """A minimal ``_OwnedChildFact``-shaped value for the pure planner test."""

    def __init__(self, child_id, scope, recorded_identity, verification,
                 kind=CHILD_PROBE, progressing=False, heartbeat_only=False):
        self.child_id = child_id
        self.scope = scope
        self.recorded_identity = recorded_identity
        self.verification = verification
        self.kind = kind
        self.progressing = progressing
        self.heartbeat_only = heartbeat_only


def test_a_drain_report_cannot_claim_a_drain_without_verification():
    # Fail-closed: a report whose child is marked ``drained`` but NOT verified
    # is refused at construction -- a drain without a re-verified owned identity
    # would be a signal to an un-owned process.
    try:
        OwnedChildDrain(
            run_id="run-1",
            children=(
                {"child_id": "probe-x", "outcome": CHILD_DRAINED,
                 "verified": False, "kind": CHILD_PROBE,
                 "useful_slot": USEFUL_SLOT_FREED},
            ),
        )
        assert False, "a drained child with no verified identity must be refused"
    except ValueError:
        pass


def test_a_drain_report_cannot_report_a_stall_occupying_a_useful_slot():
    # Fail-closed: a heartbeat-only child reported as occupying a useful slot
    # is refused -- the stall must never hold a useful slot.
    try:
        OwnedChildDrain(
            run_id="run-1",
            children=(
                {"child_id": "probe-stall", "kind": CHILD_HEARTBEAT_ONLY,
                 "outcome": CHILD_DRAIN_REFUSED, "verified": False,
                 "useful_slot": USEFUL_SLOT_OCCUPIED},
            ),
        )
        assert False, "a heartbeat-only child must not occupy a useful slot"
    except ValueError:
        pass


def test_unsupported_child_drain_version_fails_closed():
    try:
        OwnedChildDrain(run_id="run-1", schema_version="2.0.0")
        assert False, "an unsupported child-drain version must be refused"
    except UnsupportedContractVersion:
        pass


# ── criterion 3 backward compatibility ─────────────────────────────────────

def test_v1_checkpoint_without_a_drain_report_round_trips_unchanged():
    # Additive contract: a v1 checkpoint produced without draining children
    # carries ``child_drain_report: None`` and round-trips unchanged.
    record = {
        "schema_id": "https://skillweave.dev/schemas/storage/"
                     "native-process-checkpoint/v1",
        "schema_version": "1.0.0",
        "run_id": "run-legacy",
        "fencing_token": 3,
        "journal_offset": 0,
        "in_flight": [["run-legacy/task-a/1", "ready_dispatch"]],
        "route": "",
        "session_id": "",
        "captured_at": "2026-01-01T00:00:00+00:00",
        "resume_decision_event": None,
    }
    validate_native_process_checkpoint(record)
    reconstructed = NativeProcessCheckpoint(
        run_id="run-legacy", fencing_token=3, journal_offset=0,
        in_flight=(("run-legacy/task-a/1", "ready_dispatch"),),
    )
    assert reconstructed.child_drain_report is None
    assert reconstructed.to_dict()["child_drain_report"] is None


def test_a_checkpoint_cannot_smuggle_a_drain_without_verification():
    # A checkpoint carrying a drain report that claims a drain without a
    # verified identity is refused when the record is reconstructed.
    try:
        NativeProcessCheckpoint(
            run_id="run-1", fencing_token=1, journal_offset=0,
            child_drain_report={
                "schema_id": CHILD_DRAIN_SCHEMA_ID,
                "run_id": "run-1",
                "children": [
                    {"child_id": "probe-x", "outcome": CHILD_DRAINED,
                     "verified": False, "kind": CHILD_PROBE},
                ],
            },
        )
        assert False, "a checkpoint must not carry an unverified drain"
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
