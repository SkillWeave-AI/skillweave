"""SW-1510-ADAPTIVE: bounded adaptive scheduling and role-based model policy.

Three criteria, proven through the *actual consumer wiring* and again through a
*fresh installed wheel's own interpreter* with the source tree off ``sys.path``
(so a passing helper name or a source-tree shim cannot stand in for the binding).

Criterion 1 -- a ready lane is admitted by dependency, write-surface exclusion,
  *fresh* route capability/capacity and *remaining* reservations; the maximum
  parallelism is *measured* from those fresh observations, not fixed at 27.
  A refused lane is reported (``Schedule.refusals``), never silently dropped,
  and a stale observation is refused rather than assumed fresh.

Criterion 2 -- V4 Flash / V4.1 Flash are eligible only for a workload
  capability that is *attested* (proved by a real run); Pro is bounded to
  complex/special analysis or a brand-new, independent re-review.  The
  re-review is the composable ``RE_REVIEW | FRESH_CONTEXT | INDEPENDENT_REVIEWER``
  capability set from :mod:`skillweave.runtime.strategy` -- a cold first-run is
  fresh but is *not* a re-review.

Criterion 3 -- every selection/escalation/split/backoff records its cause,
  candidate set, rejected routes, policy/catalog revision, budget delta and
  outcome; the split/backoff records are emitted as the frozen
  ``attempt-event-v1`` and handed to the real Assay consumer, and a selection is
  exportable as ``route-decision-v1`` through the real adapter.  Quality
  feedback never auto-accepts a graph.

Every consumer named here is the real one: the route families are derived from
the ``tier_routes`` of the owner-approved ``route-proof-v1`` document (the
accepted ROUTE-LOCK / FG-CAPACITY pin) -- never a product-name literal in the
dispatch source, the attempt event is validated and recorded by
``AssayRepairController.record_correction``, and the route-decision payload is
built by ``faigate_adapter.make_route_decision``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.dispatch.model_policy import (  # noqa: E402
    FLASH_ROUTE_FAMILY,
    PRO_ROUTE_FAMILY,
    AllocationResult,
    ModelTier,
    ProBasis,
    WorkloadCapabilityGrant,
    evaluate_route_eligibility,
    pro_basis_from_signals,
    pro_basis_from_strategy,
    route_families_from_tier_routes,
)
from skillweave.routing.route_proof import RouteProof  # noqa: E402
from skillweave.runtime.assay import (  # noqa: E402
    AssayAdmission,
    AssayEngine,
    AssayRepairController,
    StaticActorResolver,
)
from skillweave.runtime.dagscheduler import (  # noqa: E402
    KIND_BACKOFF,
    KIND_ESCALATION,
    KIND_SELECTION,
    KIND_SPLIT,
    KIND_TO_ERROR_CLASS,
    EXECUTION_MODE_INLINE,
    QualityFeedback,
    ReservationSnapshot,
    RouteCapacitySnapshot,
    AdaptiveDecisionRecord,
    Schedule,
    SelectionLedger,
    Sequence,
    StaleObservationError,
    Task,
    build_schedule,
    measure_max_parallel,
    acceptance_from_quality_feedback,
    REFUSAL_RESERVATION,
    REFUSAL_ROUTE,
)
from skillweave.runtime.semantic_repair import validate_attempt_event  # noqa: E402
from skillweave.runtime.strategy import (  # noqa: E402
    RECIPE_COLD,
    RECIPE_RE_REVIEW,
    ExecutionCapability,
    compose_strategy,
)

#: The owner-approved route proof (route-proof-v1). Its ``tier_routes`` are the
#: accepted ROUTE-LOCK / FG-CAPACITY pin; the eligibility families must be
#: exactly the model halves of these bindings, not hand-written product names.
_ROUTE_PROOF_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "route-proof-byteplus-v1.json"

_NOW = "2026-10-09T00:00:00+00:00"


def _proof() -> RouteProof:
    return RouteProof.load(str(_ROUTE_PROOF_FIXTURE))


def _approved_routes() -> list[str]:
    return sorted(_proof().approved_routes)


def _families() -> dict:
    """The families the accepted policy yields, as the production resolver does."""
    return route_families_from_tier_routes(_proof().tier_routes)


def _fresh_routes(routes=None, *, seconds=60.0):
    rs = frozenset(routes if routes is not None else _approved_routes())
    return RouteCapacitySnapshot(
        available_routes=rs, observed_at=_NOW, freshness_seconds=seconds
    )


def _fresh_reservations(*, remaining_cost=100.0, remaining_in_flight=4, seconds=60.0):
    return ReservationSnapshot(
        remaining_cost=remaining_cost,
        remaining_in_flight=remaining_in_flight,
        observed_at=_NOW,
        freshness_seconds=seconds,
    )


def _lane_ids(schedule):
    return [[lane.task_id for lane in sb.lanes] for sb in schedule.batches]


# ===========================================================================
# Criterion 1 -- adaptive admission + measured (not 27) parallelism
# ===========================================================================


def test_max_parallel_is_measured_from_fresh_observations_not_fixed_at_27():
    # Two fresh routes and three remaining in-flight slots -> measured min = 2.
    # It is neither the literal 27 the criterion forbids nor a fixed default.
    rc = _fresh_routes(["byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"])
    res = _fresh_reservations(remaining_in_flight=3)
    measured = measure_max_parallel(rc, res, now=_NOW)
    assert measured == 2

    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", write_scope=["/src/a/**"]),
        Task(id="b", write_scope=["/src/b/**"]),
        Task(id="c", write_scope=["/src/c/**"]),
        Task(id="d", write_scope=["/src/d/**"]),
    ])
    sched = build_schedule(seq, route_capacity=rc, reservations=res, now=_NOW)
    assert sched.measured_max_parallel == 2
    # The measured cap actually split the fan-out, deterministically by id.
    assert _lane_ids(sched) == [["a", "b"], ["c", "d"]]


def test_dagscheduler_source_carries_no_fixed_27_parallelism_literal():
    # A "measured rather than fixed at 27" claim is only real if the module does
    # not hard-code 27 anywhere as a parallelism value.
    source = (_SRC / "skillweave" / "runtime" / "dagscheduler.py").read_text()
    assert "27" not in source


def test_measure_refuses_when_no_fresh_observation_exists():
    with pytest.raises(StaleObservationError):
        measure_max_parallel(None, None, now=_NOW)


def test_ready_lane_refused_when_its_route_is_not_in_the_fresh_available_set():
    # ``b`` declares a route that is not in the fresh available set -> refused,
    # named in Schedule.refusals, never silently dropped.
    rc = _fresh_routes(["byteplus/deepseek-v4-flash"])
    res = _fresh_reservations()
    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", route="byteplus/deepseek-v4-flash", write_scope=["/src/a/**"]),
        Task(id="b", route="byteplus/deepseek-v4-pro", write_scope=["/src/b/**"]),
    ])
    sched = build_schedule(seq, route_capacity=rc, reservations=res, now=_NOW)
    assert [r.task_id for r in sched.refusals] == ["b"]
    assert sched.refusals[0].dimension == REFUSAL_ROUTE
    assert "byteplus/deepseek-v4-pro" in sched.refusals[0].reason
    assert _lane_ids(sched) == [["a"]]


def test_ready_lane_refused_when_remaining_reservations_are_exhausted():
    # An explicit cap keeps the parallel dimension out of the way, so the
    # reservation dimension is what refuses: zero remaining in-flight slots.
    rc = _fresh_routes()
    res = _fresh_reservations(remaining_in_flight=0)
    seq = Sequence(session_boundary="c", tasks=[Task(id="a", write_scope=["/src/a/**"])])
    sched = build_schedule(seq, max_parallel=1, route_capacity=rc, reservations=res, now=_NOW)
    assert [r.task_id for r in sched.refusals] == ["a"]
    assert sched.refusals[0].dimension == REFUSAL_RESERVATION
    assert sched.batches == []


def test_ready_lane_refused_when_route_cost_exceeds_remaining_budget():
    rc = _fresh_routes()
    res = _fresh_reservations(remaining_cost=0.5, remaining_in_flight=4)
    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", route="byteplus/deepseek-v4-pro", write_scope=["/src/a/**"]),
    ])
    sched = build_schedule(
        seq, route_capacity=rc, reservations=res, now=_NOW,
        route_cost={"byteplus/deepseek-v4-pro": 2.0},
    )
    assert [r.task_id for r in sched.refusals] == ["a"]
    assert sched.refusals[0].dimension == REFUSAL_RESERVATION
    assert "exceeds remaining budget" in sched.refusals[0].reason


def test_stale_route_observation_is_refused_not_assumed_fresh():
    stale = RouteCapacitySnapshot(
        available_routes=frozenset(_approved_routes()),
        observed_at="2020-01-01T00:00:00+00:00",
        freshness_seconds=60.0,
    )
    assert stale.is_fresh(_NOW) is False
    res = _fresh_reservations()
    seq = Sequence(session_boundary="c", tasks=[Task(id="a")])
    with pytest.raises(StaleObservationError) as ei:
        build_schedule(seq, route_capacity=stale, reservations=res, now=_NOW)
    assert ei.value.kind == "route-capacity"


def test_stale_reservation_observation_is_refused_not_assumed_fresh():
    rc = _fresh_routes()
    stale = ReservationSnapshot(
        remaining_cost=100.0, remaining_in_flight=4,
        observed_at="2020-01-01T00:00:00+00:00", freshness_seconds=60.0,
    )
    seq = Sequence(session_boundary="c", tasks=[Task(id="a")])
    with pytest.raises(StaleObservationError) as ei:
        build_schedule(seq, route_capacity=rc, reservations=stale, now=_NOW)
    assert ei.value.kind == "reservation"


def test_the_four_admission_dimensions_are_all_honoured_together():
    # dependency: b depends on the gated a, whose gate failed -> b stays blocked.
    # write-surface: c and d share a scope -> both inline, never fanned out.
    # route: e's route is not fresh-available -> refused.
    # reservation: x's route cost exceeds the remaining budget -> refused.
    rc = _fresh_routes(["byteplus/deepseek-v4-flash"])
    res = _fresh_reservations(remaining_cost=1.0, remaining_in_flight=3)
    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", gate="g"),
        Task(id="b", depends_on=["a"]),
        Task(id="c", write_scope=["/src/shared/**"]),
        Task(id="d", write_scope=["/src/shared/**"]),
        Task(id="e", route="byteplus/deepseek-v4-pro"),
        Task(id="x", route="byteplus/deepseek-v4-flash"),
    ])
    sched = build_schedule(
        seq, gate_results={"g": False}, route_capacity=rc, reservations=res, now=_NOW,
        route_cost={"byteplus/deepseek-v4-flash": 5.0},
    )
    # dependency dimension:
    assert sched.blocked == ["b"]
    # route + reservation dimensions, reported not dropped:
    refused = {r.task_id: r.dimension for r in sched.refusals}
    assert refused == {"e": REFUSAL_ROUTE, "x": REFUSAL_RESERVATION}
    # write-surface dimension: c and d are emitted inline (one lane per batch).
    modes = {lane.task_id: lane.execution_mode
             for sb in sched.batches for lane in sb.lanes}
    assert modes.get("c") == EXECUTION_MODE_INLINE
    assert modes.get("d") == EXECUTION_MODE_INLINE
    assert _lane_ids(sched) == [["a"], ["c"], ["d"]]


def test_a_task_with_no_declared_route_is_skipped_by_the_route_dimension():
    # The route dimension only refuses a lane that *declares* a route; a lane
    # that declares none is admitted on the other dimensions.
    rc = _fresh_routes(["byteplus/deepseek-v4-flash"])
    res = _fresh_reservations()
    seq = Sequence(session_boundary="c", tasks=[Task(id="a", write_scope=["/src/a/**"])])
    sched = build_schedule(seq, route_capacity=rc, reservations=res, now=_NOW)
    assert sched.refusals == []
    assert _lane_ids(sched) == [["a"]]


def test_measured_parallelism_of_zero_refuses_every_lane_not_crashes():
    # No fresh route capacity and no in-flight headroom -> measured 0. The
    # schedule admits nothing but still *reports* each lane refused, rather than
    # raising from a zero-width split.
    rc = _fresh_routes([])
    res = _fresh_reservations(remaining_in_flight=0)
    assert measure_max_parallel(rc, res, now=_NOW) == 0
    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", write_scope=["/src/a/**"]),
        Task(id="b", write_scope=["/src/b/**"]),
    ])
    sched = build_schedule(seq, route_capacity=rc, reservations=res, now=_NOW)
    assert sched.measured_max_parallel == 0
    assert sched.batches == []
    assert {r.task_id for r in sched.refusals} == {"a", "b"}
    assert all(r.dimension == "measured-parallelism" for r in sched.refusals)


def test_legacy_dispatch4_behaviour_is_preserved_without_observations():
    # No observations -> not adaptive: no refusals, measured stays None, and the
    # dispatch-4 max_parallel/gating semantics are exactly as before.
    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", write_scope=["/src/a/**"]),
        Task(id="b", write_scope=["/src/b/**"]),
    ])
    sched = build_schedule(seq, max_parallel=1)
    assert _lane_ids(sched) == [["a"], ["b"]]
    assert sched.refusals == []
    assert sched.measured_max_parallel is None


# ===========================================================================
# Criterion 2 -- role-based flash/pro eligibility
# ===========================================================================


def _flash_attested():
    return WorkloadCapabilityGrant(workload="bounded-bug-discovery", attested=True)


def _flash_unattested():
    return WorkloadCapabilityGrant(workload="bounded-bug-discovery", attested=False)


def _model_half(route: str) -> str:
    return route.split("/", 1)[1]


def test_route_families_are_derived_from_the_accepted_route_proof():
    # The eligibility families are not hand-written product names: they are the
    # ``model`` halves of the owner-approved route-proof tier bindings. Every
    # approved route lands in a family; the families are disjoint and unite to
    # the accepted approved set; and ``pro`` is exactly the Pro tier routes that
    # are not also Flash tier routes.
    proof = _proof()
    fam = route_families_from_tier_routes(proof.tier_routes)
    flash, pro = fam["flash"], fam["pro"]
    assert flash.isdisjoint(pro)
    assert flash | pro == {_model_half(r) for r in proof.approved_routes}
    assert flash == {
        _model_half(r) for r in proof.resolve_allowed_routes_for_tier("flash")
    }
    assert pro == {
        _model_half(r) for r in proof.resolve_allowed_routes_for_tier("pro")
    } - flash


def test_configured_families_come_from_the_route_proof_not_literals():
    # With no owner route-proof configured the module families fail closed
    # (empty); configured, they are exactly the accepted proof's model halves.
    # This proves the dispatch module carries no hard-coded route family.
    env = _clean_env()
    env.pop("SKILLWEAVE_ROUTE_PROOF", None)
    code = (
        "import json,sys;sys.path.insert(0,{src!r});"
        "from skillweave.dispatch import model_policy as mp;"
        "print(json.dumps({{'flash':sorted(mp.FLASH_ROUTE_FAMILY),"
        "'pro':sorted(mp.PRO_ROUTE_FAMILY)}}))"
    ).format(src=str(_SRC))

    def _families_from(env_):
        proc = subprocess.run([sys.executable, "-B", "-c", code], env=env_,
                              capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)

    assert _families_from(env) == {"flash": [], "pro": []}
    fam = _families()
    configured = _families_from(dict(env, SKILLWEAVE_ROUTE_PROOF=str(_ROUTE_PROOF_FIXTURE)))
    assert configured == {"flash": sorted(fam["flash"]), "pro": sorted(fam["pro"])}


def test_flash_routes_are_eligible_only_for_an_attested_workload():
    cands = sorted(_approved_routes())
    alloc = AllocationResult(tier=ModelTier.FLASH)
    fam = _families()

    ok = evaluate_route_eligibility(
        alloc, cands, workload=_flash_attested(), route_families=fam
    )
    assert ok.eligible == {
        "byteplus/deepseek-v4-flash", "byteplus/deepseek-v4.1-flash"
    }
    # The pro routes on a flash allocation are refused with the stable reason.
    assert set(ok.rejected) == {
        "byteplus/deepseek-v4-pro", "byteplus/deepseek-v4.1-pro"
    }
    assert all(v == "pro-route is not admitted on a flash allocation"
               for v in ok.rejected.values())

    bad = evaluate_route_eligibility(
        alloc, cands, workload=_flash_unattested(), route_families=fam
    )
    assert bad.eligible == frozenset()
    for route in ("byteplus/deepseek-v4-flash", "byteplus/deepseek-v4.1-flash"):
        assert bad.rejected[route] == "flash-route requires an attested workload capability"


def test_pro_route_is_refused_without_a_pro_basis():
    cands = list(_approved_routes())
    alloc = AllocationResult(tier=ModelTier.PRO)
    # No basis -> pro routes refused; the attested flash routes stay eligible.
    elig = evaluate_route_eligibility(
        alloc, cands, workload=_flash_attested(), pro_basis=ProBasis(),
        route_families=_families(),
    )
    assert elig.eligible == {
        "byteplus/deepseek-v4-flash", "byteplus/deepseek-v4.1-flash"
    }
    for route in ("byteplus/deepseek-v4-pro", "byteplus/deepseek-v4.1-pro"):
        assert elig.rejected[route] == (
            "pro-route requires complex/special analysis or a fresh independent re-review"
        )


def test_pro_route_is_admitted_for_complex_or_special_analysis():
    cands = list(_approved_routes())
    alloc = AllocationResult(tier=ModelTier.PRO)
    basis = ProBasis(complexity=True)
    elig = evaluate_route_eligibility(
        alloc, cands, workload=_flash_attested(), pro_basis=basis,
        route_families=_families(),
    )
    assert {"byteplus/deepseek-v4-pro", "byteplus/deepseek-v4.1-pro"} <= elig.eligible
    assert "complex_or_special_analysis" in elig.reasons


def test_pro_route_is_admitted_for_a_fresh_independent_re_review_via_the_strategy():
    # The re-review is the composable capability set from the real strategy
    # module -- not a hand-written flag.
    assert RECIPE_RE_REVIEW.is_fresh_independent_re_review is True
    basis = pro_basis_from_strategy(RECIPE_RE_REVIEW)
    assert basis.permits_pro() is True
    assert basis.reasons() == ["fresh_independent_re_review"]

    # And it is composable ad hoc, not only via a named recipe.
    ad_hoc = compose_strategy(
        ExecutionCapability.RE_REVIEW,
        ExecutionCapability.FRESH_CONTEXT,
        ExecutionCapability.INDEPENDENT_REVIEWER,
    )
    assert pro_basis_from_strategy(ad_hoc).permits_pro() is True

    alloc = AllocationResult(tier=ModelTier.PRO)
    elig = evaluate_route_eligibility(
        alloc, list(_approved_routes()),
        workload=_flash_attested(), pro_basis=basis, route_families=_families(),
    )
    assert {"byteplus/deepseek-v4-pro", "byteplus/deepseek-v4.1-pro"} <= elig.eligible
    assert "fresh_independent_re_review" in elig.reasons


def test_a_cold_first_run_is_fresh_but_is_not_a_re_review():
    # The collision that would let any fresh run claim Pro: a cold run is fresh
    # and independently reviewed, but it is NOT a re-review, so it confers no
    # Pro basis.
    assert RECIPE_COLD.has_fresh_context and RECIPE_COLD.has_independent_reviewer
    assert RECIPE_COLD.is_fresh_independent_re_review is False
    assert pro_basis_from_strategy(RECIPE_COLD).permits_pro() is False


def test_pro_basis_from_signals_maps_critical_review_to_a_re_review():
    from skillweave.dispatch.model_policy import AllocationSignals

    complex_basis = pro_basis_from_signals(AllocationSignals(architecture=True))
    assert complex_basis.permits_pro() and complex_basis.reasons() == [
        "complex_or_special_analysis"
    ]
    review_basis = pro_basis_from_signals(AllocationSignals(critical_review=True))
    assert review_basis.permits_pro() and review_basis.reasons() == [
        "fresh_independent_re_review"
    ]
    plain = pro_basis_from_signals(AllocationSignals())
    assert plain.permits_pro() is False


def test_unknown_or_bare_routes_are_refused():
    alloc = AllocationResult(tier=ModelTier.FLASH)
    elig = evaluate_route_eligibility(
        alloc, ["deepseek-v4-flash", "somevendor/some-model"],
        workload=_flash_attested(), route_families=_families(),
    )
    assert elig.eligible == frozenset()
    assert elig.rejected["deepseek-v4-flash"] == (
        "route is not a recognized flash/pro route family"
    )


# ===========================================================================
# Criterion 3 -- every decision records its cause
# ===========================================================================


def test_adaptive_admission_records_a_selection_with_every_required_field():
    rc = _fresh_routes(["byteplus/deepseek-v4-flash"])
    res = _fresh_reservations()
    seq = Sequence(session_boundary="c", tasks=[
        Task(id="a", route="byteplus/deepseek-v4-flash", write_scope=["/src/a/**"]),
        Task(id="b", route="byteplus/deepseek-v4-pro", write_scope=["/src/b/**"]),
    ])
    sched = build_schedule(seq, route_capacity=rc, reservations=res, now=_NOW)
    assert sched.ledger is not None
    records = sched.ledger.records
    assert len(records) == 1
    rec = records[0]
    assert rec.kind == KIND_SELECTION
    assert rec.cause  # why the decision was taken
    assert "a" in rec.candidates  # candidates (the admitted set)
    assert "b" in rec.rejected_routes  # rejected routes, named
    assert rec.outcome  # what happened
    assert rec.budget_delta == 0.0
    d = rec.to_dict()
    for key in ("kind", "cause", "candidates", "rejected_routes",
                "policy_revision", "catalog_revision", "budget_delta", "outcome"):
        assert key in d


def test_split_decision_emits_a_frozen_attempt_event_to_the_real_assay_consumer():
    ledger = SelectionLedger(policy_revision="rev-9", catalog_revision="cat-3")
    rec = ledger.record_split(
        cause="context pressure on lane-a",
        candidates=["lane-a", "lane-b"],
        rejected_routes=["byteplus/deepseek-v4-pro"],
        budget_delta=0.25,
        outcome="split into two bounded lanes",
        subject="byteplus/deepseek-v4-flash",
    )
    event = rec.to_attempt_event(run_id="run-1", task_id="lane-a", principal="worker-1")

    # The frozen contract version is taken from the accepted repair module, not
    # re-declared here.
    from skillweave.runtime import semantic_repair as sr

    assert event["schema_id"] == sr.ATTEMPT_EVENT_SCHEMA_ID
    assert event["schema_version"] == sr.ATTEMPT_EVENT_SCHEMA_VERSION
    assert event["payload"]["error_class"] == "context_pressure"
    assert event["payload"]["correction_count"] == 1
    validate_attempt_event(event)  # the real validator accepts it

    # The real Assay consumer records it (validation is delegated by the consumer).
    resolver = StaticActorResolver({"good": "worker-1"})
    engine = AssayEngine(policy_name="unicorn", max_turns=10, run_id="run-1",
                         actor_resolver=resolver)
    engine.pin(subject="subj-1", brief="brief-1")
    admission = AssayAdmission(actor_token="good", subject=engine.subject_digest,
                               brief=engine.brief_digest, envelope=engine.envelope)
    controller = AssayRepairController(engine=engine, admission=admission)
    recorded = controller.record_correction(event)
    assert recorded is event
    assert len(controller.corrections) == 1


def test_backoff_decision_maps_to_the_auth_quota_class():
    assert KIND_TO_ERROR_CLASS[KIND_SPLIT] == "context_pressure"
    assert KIND_TO_ERROR_CLASS[KIND_BACKOFF] == "auth_quota"
    rec = SelectionLedger().record_backoff(
        cause="rate limit hit", outcome="backed off", subject="byteplus/deepseek-v4-pro"
    )
    event = rec.to_attempt_event(run_id="run-1", task_id="lane-a")
    validate_attempt_event(event)
    assert event["payload"]["error_class"] == "auth_quota"


def test_non_correction_kinds_have_no_attempt_event_form():
    for kind in (KIND_SELECTION, KIND_ESCALATION):
        rec = AdaptiveDecisionRecord(kind=kind, cause="x", outcome="y")
        with pytest.raises(ValueError):
            rec.to_attempt_event()


def test_decision_exports_a_route_decision_v1_payload_through_the_real_adapter():
    rec = SelectionLedger(policy_revision="rev-9", catalog_revision="cat-3").record_selection(
        cause="pick a route",
        candidates=["byteplus/deepseek-v4-pro", "byteplus/deepseek-v4-flash"],
        rejected_routes=["byteplus/deepseek-v4.1-pro"],
        subject="byteplus/deepseek-v4-pro",
        outcome="selected",
    )
    payload = rec.to_route_decision(run_id="run-1", task_id="lane-a", attempt=2,
                                    reservation_id="res-7")
    # The adapter's real route-decision-v1 payload, carrying the ledger revisions
    # and keeping requested/resolved provider+model as separate facts.
    assert payload["schema_version"] == "1.0.0"
    assert payload["request_id"] == "run-1/lane-a"
    assert payload["requested_model"] == "byteplus/deepseek-v4-pro"
    assert payload["resolved_provider"] == "byteplus"
    assert payload["resolved_model"] == "deepseek-v4-pro"
    assert payload["catalog_revision"] == "cat-3"
    assert payload["policy_revision"] == "rev-9"
    assert payload["attempt"] == 2
    assert payload["reservation_id"] == "res-7"


def test_quality_feedback_never_auto_accepts_a_graph():
    for passed in (True, False):
        decision = acceptance_from_quality_feedback(
            QualityFeedback(signal="reviewer", passed=passed, score=1.0)
        )
        assert decision.accepted is False
        assert decision.requires_gate is True
        assert "advisory" in decision.reason


def test_ledger_records_carry_the_policy_and_catalog_revision_they_were_made_under():
    ledger = SelectionLedger(policy_revision="rev-9", catalog_revision="cat-3")
    ledger.record_escalation(cause="two non-progress cycles", outcome="escalated to pro")
    assert ledger.records[0].policy_revision == "rev-9"
    assert ledger.records[0].catalog_revision == "cat-3"
    dumped = ledger.to_dict()
    assert dumped["policy_revision"] == "rev-9"
    assert dumped["catalog_revision"] == "cat-3"
    assert len(dumped["records"]) == 1


# ===========================================================================
# Installed-origin proof: a fresh wheel's own interpreter, source off sys.path
# ===========================================================================

#: The driver runs under the *installed* wheel's interpreter with the source
#: tree off ``sys.path`` and a cwd that carries no ``skillweave``.  It exercises
#: all three criteria through the installed modules and prints a JSON verdict.
_INSTALLED_DRIVER = r'''
import json
import sys

import skillweave.runtime.dagscheduler as ds
import skillweave.runtime.strategy as st
import skillweave.dispatch.model_policy as mp
from skillweave.runtime.assay import (
    AssayAdmission, AssayEngine, AssayRepairController, StaticActorResolver,
)
from skillweave.runtime.semantic_repair import validate_attempt_event

NOW = "2026-10-09T00:00:00+00:00"

# --- Criterion 1: adaptive admission + measured parallelism -----------------
rc = ds.RouteCapacitySnapshot(
    available_routes=frozenset({"byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"}),
    observed_at=NOW, freshness_seconds=60.0,
)
res = ds.ReservationSnapshot(remaining_cost=100.0, remaining_in_flight=3,
                             observed_at=NOW, freshness_seconds=60.0)
measured = ds.measure_max_parallel(rc, res, now=NOW)
seq = ds.Sequence(session_boundary="c", tasks=[
    ds.Task(id="a", route="byteplus/deepseek-v4-flash", write_scope=["/src/a/**"]),
    ds.Task(id="b", route="byteplus/deepseek-v4.1-pro", write_scope=["/src/b/**"]),
    ds.Task(id="c", write_scope=["/src/c/**"]),
])
sched = ds.build_schedule(seq, route_capacity=rc, reservations=res, now=NOW)
lane_ids = [[l.task_id for l in sb.lanes] for sb in sched.batches]
refused = {r.task_id: r.dimension for r in sched.refusals}

# --- Criterion 2: role-based eligibility ------------------------------------
alloc_flash = mp.AllocationResult(tier=mp.ModelTier.FLASH)
alloc_pro = mp.AllocationResult(tier=mp.ModelTier.PRO)
attested = mp.WorkloadCapabilityGrant(workload="bounded-bug-discovery", attested=True)
unattested = mp.WorkloadCapabilityGrant(workload="bounded-bug-discovery", attested=False)
cands = sorted(rc.available_routes)
elig_attested = mp.evaluate_route_eligibility(alloc_flash, cands, workload=attested)
elig_unattested = mp.evaluate_route_eligibility(alloc_flash, cands, workload=unattested)
basis = mp.pro_basis_from_strategy(st.RECIPE_RE_REVIEW)
elig_pro = mp.evaluate_route_eligibility(alloc_pro, cands, workload=attested, pro_basis=basis)
cold_basis = mp.pro_basis_from_strategy(st.RECIPE_COLD)

# --- Criterion 3: recorded decisions + real attempt-event consumer -----------
ledger = ds.SelectionLedger(policy_revision="rev-9", catalog_revision="cat-3")
rec = ledger.record_split(cause="context pressure", candidates=["a", "b"],
                          rejected_routes=["byteplus/deepseek-v4-pro"],
                          budget_delta=0.25, outcome="split",
                          subject="byteplus/deepseek-v4-flash")
event = rec.to_attempt_event(run_id="run-1", task_id="a", principal="worker-1")
validate_attempt_event(event)
resolver = StaticActorResolver({"good": "worker-1"})
engine = AssayEngine(policy_name="unicorn", max_turns=10, run_id="run-1",
                     actor_resolver=resolver)
engine.pin(subject="subj-1", brief="brief-1")
adm = AssayAdmission(actor_token="good", subject=engine.subject_digest,
                     brief=engine.brief_digest, envelope=engine.envelope)
controller = AssayRepairController(engine=engine, admission=adm)
controller.record_correction(event)
rd = rec.to_route_decision(run_id="run-1", task_id="a", attempt=2, reservation_id="res-7")
accept = ds.acceptance_from_quality_feedback(
    ds.QualityFeedback(signal="reviewer", passed=True, score=1.0))

print(json.dumps({
    "dagscheduler_file": ds.__file__,
    "strategy_file": st.__file__,
    "model_policy_file": mp.__file__,
    "measured_max_parallel": measured,
    "schedule_measured": sched.measured_max_parallel,
    "lane_ids": lane_ids,
    "refused": refused,
    "elig_flash_attested": sorted(elig_attested.eligible),
    "elig_flash_unattested": sorted(elig_unattested.eligible),
    "elig_pro_rereview": sorted(elig_pro.eligible),
    "families_flash": sorted(mp.FLASH_ROUTE_FAMILY),
    "families_pro": sorted(mp.PRO_ROUTE_FAMILY),
    "pro_reasons": list(elig_pro.reasons),
    "cold_permits_pro": cold_basis.permits_pro(),
    "attempt_event_error_class": event["payload"]["error_class"],
    "attempt_event_schema_id": event["schema_id"],
    "route_decision_catalog": rd["catalog_revision"],
    "corrections_recorded": len(controller.corrections),
    "accept_accepted": accept.accepted,
    "accept_requires_gate": accept.requires_gate,
}, sort_keys=True))
'''


def _clean_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _venv_site(venv_dir):
    matches = sorted(venv_dir.glob("lib/python*/site-packages"))
    assert matches, f"no venv site-packages under {venv_dir}"
    return matches[0]


def _build_wheel(work):
    (work / "src").parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_SRC, work / "src")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(_REPO_ROOT / name, work / name)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index",
         "--no-build-isolation", "-w", str(work / "dist"), str(work)],
        cwd=str(work), capture_output=True, text=True, env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted((work / "dist").glob("skillweave-*.whl"))
    assert wheels, f"no wheel built in {work / 'dist'}"
    return wheels[-1]


def _install_into_fresh_venv(work, wheel):
    venv_dir = work / "venv"
    env = _clean_env()
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True,
                   capture_output=True, text=True, env=env)
    site = _venv_site(venv_dir)
    subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "--no-deps", "--no-index",
         str(wheel)],
        check=True, capture_output=True, text=True, env=env,
    )
    # The offline install cannot fetch PyYAML; bundle the ambient copy so the
    # installed package imports without reaching the network or the source tree.
    import yaml  # noqa: PLC0415 - intentional late import

    ambient_yaml = Path(yaml.__file__).resolve().parent
    shutil.copytree(ambient_yaml, site / "yaml")
    extra = ambient_yaml.parent / "_yaml"
    if extra.exists():
        shutil.copytree(extra, site / "_yaml")
    return venv_dir


def _run_installed(venv_dir, tmp_path):
    driver = tmp_path / "driver.py"
    driver.write_text(_INSTALLED_DRIVER, encoding="utf-8")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = _clean_env()
    env["TMPDIR"] = str(tmp_path / "tmp")
    # Point the installed policy at the owner's route proof so the *installed*
    # wheel derives its families from owner data -- never from a source-tree
    # literal and never from a helper name.
    env["SKILLWEAVE_ROUTE_PROOF"] = str(_ROUTE_PROOF_FIXTURE)
    proc = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-B", str(driver)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_installed_wheel_binds_all_three_criteria(tmp_path):
    pytest.importorskip("yaml")

    work = tmp_path / "build"
    work.mkdir()
    wheel = _build_wheel(work)
    venv_dir = _install_into_fresh_venv(work, wheel)
    result = _run_installed(venv_dir, tmp_path)

    # The *installed* wheel -- not the source tree -- provided the modules.
    assert str(venv_dir) in result["dagscheduler_file"]
    assert str(venv_dir) in result["strategy_file"]
    assert str(venv_dir) in result["model_policy_file"]

    # Criterion 1 in the installed wheel.
    assert result["measured_max_parallel"] == 2
    assert result["schedule_measured"] == 2
    assert result["lane_ids"] == [["a", "c"]]
    assert result["refused"] == {"b": "route-capability"}

    # Criterion 2 in the installed wheel.
    assert result["elig_flash_attested"] == ["byteplus/deepseek-v4-flash"]
    assert result["elig_flash_unattested"] == []
    assert set(result["elig_pro_rereview"]) == {
        "byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"
    }
    assert result["pro_reasons"] == ["fresh_independent_re_review"]
    assert result["cold_permits_pro"] is False
    # The installed wheel derived its families from the owner route proof.
    assert result["families_flash"] == sorted(_families()["flash"])
    assert result["families_pro"] == sorted(_families()["pro"])

    # Criterion 3 in the installed wheel.
    assert result["attempt_event_error_class"] == "context_pressure"
    assert result["attempt_event_schema_id"].endswith("attempt-event/v1")
    assert result["route_decision_catalog"] == "cat-3"
    assert result["corrections_recorded"] == 1
    assert result["accept_accepted"] is False
    assert result["accept_requires_gate"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
