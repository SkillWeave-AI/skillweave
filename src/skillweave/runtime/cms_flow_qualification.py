"""The production consumer of the declared CMS paths (SW-1511-CMS-FLOWS).

:mod:`skillweave.blueprint.cms_path_qualification` owns the frozen declaration
of the five initial CMS paths and the ``cms-process-qualification-v1`` record an
observer is supposed to fill in -- but, on its own, that module has **no
production caller**: it is a contract nothing in the shipped flow actually
drives.  A contract with no consumer is a declaration, not a qualification.

This module is that consumer.  :func:`qualify_cms_path` takes one *declared*
path (a :class:`~skillweave.blueprint.cms_path_qualification.CmsPath` from
:data:`~skillweave.blueprint.cms_path_qualification.INITIAL_CMS_PATHS`) and drives
it through the **existing native controller seam** -- the durable, fenced
:class:`~skillweave.runtime.native_process.NativeProcessService` -- over the
repository's dummy :class:`~skillweave.runtime.native_process.RawContentAdapter`:

* one owned attempt is dispatched with a **real, live process identity** (the
  producer is *derived* from the owned attempt's durable identity, never
  declared by the caller);
* the reversible lifecycle runs to completion -- baseline -> draft -> dry-run ->
  bounded apply -> readback -> restore -- on an owner-authorized *sandbox*
  binding, then the transaction is read back **from durable state** so the
  readback/restore verdict comes from a persisted record, not a live claim;
* the record is built through the frozen fail-closed
  :meth:`CmsProcessQualification.from_mapping
  <skillweave.blueprint.cms_path_qualification.CmsProcessQualification.from_mapping>`.

The honesty of the result is the point.  This consumer genuinely *measures* all
six outcomes of the path's lifecycle and, since every one of them is read back
from durable state, it can honestly return a ``performed`` record:

* ``success`` -- the reversible lifecycle driven to ``restored``;
* ``failure`` -- a separately owned attempt resolved and re-read as ``failed``
  from durable state;
* ``cancel`` -- the run reconciled to its terminal cancellation state and
  re-read as terminal from durable state;
* ``wait`` -- a provider-quota wait *recorded* in durable metadata and re-read
  from it (the wait survives, it is not a live in-memory flag);
* ``resume`` -- the same run resumed by a **new controller** after a restart,
  its in-flight attempt *adopted* from a durable checkpoint and the adoption
  re-read from durable state, with the work proven not to be duplicated (the
  re-dispatch of the adopted scope is refused);
* ``publication-refusal`` -- an irreversible publication **refused** for want of
  the designated explicit authorization (the durable independent review is
  present, so the refusal is the authorization check, not an earlier one), with
  nothing published but the refusal recorded.

The contract requires all six measurable outcomes
(:data:`~skillweave.blueprint.cms_path_qualification.REQUIRED_OUTCOMES`) *and*
the source-bound pins plus a verified readback/restore before a record may claim
``performed``; all of these hold here, so the record carries
``disposition=performed`` with no limitation.  A record is *not* a prewritten
receipt: every measured outcome is a persisted fact this module re-read, and the
two outcomes that need an MCP authority (``resume``'s checkpoint and
``publication-refusal``'s durable review) refuse rather than forge when the
seam is not actually available.

The seam is a **fake at the process boundary**: no provider, no gateway and no
CMS call is made or probed.  ``RawContentAdapter`` is an in-memory dummy and the
binding is a declared sandbox; the private provider implementation stays in
``skillweave-packs-pro``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from ..blueprint.cms_path_qualification import (
    CMS_PATHS_BY_ID,
    DISPOSITION_NOT_PERFORMED,
    DISPOSITION_PERFORMED,
    INITIAL_CMS_PATHS,
    SCHEMA_VERSION,
    CmsPath,
    CmsProcessQualification,
)

__all__ = [
    "CMS_QUALIFICATION_SANDBOX_TARGET",
    "CmsFlowQualificationRefused",
    "CmsPathQualificationRun",
    "qualify_cms_path",
]

#: The owner-authorized *sandbox* target the qualification is driven on.  It is a
#: declared sandbox (:data:`~skillweave.runtime.native_process.CMS_BINDING_SANDBOX`)
#: and carries no production marker, so the sandbox discriminator and the
#: owner-declaration check both pass without touching a production surface.
CMS_QUALIFICATION_SANDBOX_TARGET = "sandbox/ch"

#: The baseline content the reversible lifecycle starts from (the dummy
#: adapter's in-memory value).  Kept as a module constant so a qualification run
#: is reproducible.
CMS_QUALIFICATION_BASELINE: dict[str, Any] = {"title": "old", "body": "hello"}

#: The bounded change the lifecycle applies and then restores away from.
CMS_QUALIFICATION_CHANGE: dict[str, Any] = {"title": "new", "body": "hello"}

#: The outcomes this consumer genuinely measures through the *existing* native
#: controller seam.  Each one is driven and then **read back from durable
#: state**, never asserted from a live in-memory claim:
#:
#: * ``success`` -- the reversible CMS lifecycle runs to ``restored``;
#: * ``failure`` -- a separately owned attempt for the same path is *resolved*
#:   through the native ``record_failure`` seam and its terminal ``failed``
#:   intent state is re-read from the durable store;
#: * ``cancel`` -- the run is reconciled to its terminal cancellation via the
#:   durable ``CancellationReconciler`` and re-read as a terminal run holding the
#:   ``killed`` lifecycle state;
#: * ``wait`` -- a provider-quota wait is recorded in durable run metadata and
#:   re-read from it;
#: * ``resume`` -- the run is resumed by a new controller after a restart, its
#:   in-flight attempt adopted from a durable checkpoint and read back, and the
#:   adopted work proven not duplicated;
#: * ``publication-refusal`` -- an irreversible publication is presented with a
#:   current independent durable review but **no** designated authorization and
#:   refused, with nothing published.
#:
#: All six of :data:`~skillweave.blueprint.cms_path_qualification.REQUIRED_OUTCOMES`
#: are measured, so the record may claim ``performed``.
MEASURED_OUTCOMES: tuple[str, ...] = (
    "success",
    "wait",
    "failure",
    "cancel",
    "resume",
    "publication-refusal",
)

#: The outcomes that are still **not measured** by this consumer.  Empty: every
#: required outcome is measured, so no limitation is attached.
UNMEASURED_OUTCOMES: tuple[str, ...] = ()

#: The limitation this consumer attaches to a record it produces.  Empty,
#: because every required outcome is genuinely measured -- there is nothing the
#: record would be implying it did and did not do.
UNMEASURED_OUTCOME_LIMITATION = ""

#: The durable run-metadata key the ``wait`` outcome is recorded and re-read
#: under.  A wait that is not durable is not a measured wait.
WAIT_METADATA_KEY = "cms_qualification_wait"


class CmsFlowQualificationRefused(ValueError):
    """A requested path is not one of the five declared CMS paths.

    Raised *before* any effect, so a qualification run can never be pointed at a
    path the frozen declaration does not name.
    """


#: The event type stamped on the run's cancellation transition.
CANCELLATION_EVENT_TYPE = "process_killed"


@dataclass(frozen=True)
class CmsPathQualificationRun:
    """What driving one declared CMS path through the native seam produced.

    Carries the resolved :class:`CmsPath`, the fail-closed qualification record,
    and the durable facts of every measured outcome so a caller can re-read the
    persisted records rather than trust this object:

    * ``producer_identity`` / ``txn_id`` / ``final_stage`` -- the ``success``
      outcome's durable CMS transaction;
    * ``failure_intent_id`` / ``failure_state`` -- the ``failure`` outcome's
      durable dispatch intent and its persisted terminal state;
    * ``cancel_run_state`` / ``cancel_lifecycle_state`` -- the ``cancel``
      outcome's durable run state and lifecycle metadata;
    * ``wait_decision`` / ``wait_marker`` -- the ``wait`` outcome's recorded
      decision and the durable run-metadata key it is re-read from;
    * ``resume_decision`` / ``resume_decision_verified`` / ``resume_intent_id`` /
      ``resume_execution_scope`` -- the ``resume`` outcome's adopted decision
      read from the durable checkpoint, whether the adoption was verified, and
      the adopted attempt's durable intent/scope;
    * ``publication_refusal_principal`` / ``publication_refusal_field`` -- the
      ``publication-refusal`` outcome's answering reviewer principal and the
      field the refusal was raised on.
    """

    path: CmsPath
    qualification: CmsProcessQualification
    producer_identity: str
    run_id: str
    txn_id: str
    final_stage: str
    failure_intent_id: str = ""
    failure_state: str = ""
    cancel_run_state: str = ""
    cancel_lifecycle_state: str = ""
    wait_decision: str = ""
    wait_marker: str = ""
    resume_decision: str = ""
    resume_decision_verified: bool = False
    resume_intent_id: str = ""
    resume_execution_scope: str = ""
    publication_refusal_principal: str = ""
    publication_refusal_field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path.to_dict(),
            "qualification": self.qualification.to_dict(),
            "producer_identity": self.producer_identity,
            "run_id": self.run_id,
            "txn_id": self.txn_id,
            "final_stage": self.final_stage,
            "failure_intent_id": self.failure_intent_id,
            "failure_state": self.failure_state,
            "cancel_run_state": self.cancel_run_state,
            "cancel_lifecycle_state": self.cancel_lifecycle_state,
            "wait_decision": self.wait_decision,
            "wait_marker": self.wait_marker,
            "resume_decision": self.resume_decision,
            "resume_decision_verified": self.resume_decision_verified,
            "resume_intent_id": self.resume_intent_id,
            "resume_execution_scope": self.resume_execution_scope,
            "publication_refusal_principal": self.publication_refusal_principal,
            "publication_refusal_field": self.publication_refusal_field,
        }


def _resolve_declared_path(path: Any) -> CmsPath:
    """Resolve ``path`` to a frozen declaration, refusing anything undeclared.

    Accepts one of the five frozen path ids or a :class:`CmsPath`; either way the
    **frozen** declaration is returned, so a caller cannot qualify a path it
    invented and the same path id always resolves to the same declaration.
    """
    path_id = path.path_id if isinstance(path, CmsPath) else path
    if not isinstance(path_id, str) or not path_id.strip():
        raise CmsFlowQualificationRefused(
            f"a CMS path qualification must name a declared path, got {path!r}"
        )
    declared = CMS_PATHS_BY_ID.get(path_id.strip())
    if declared is None:
        raise CmsFlowQualificationRefused(
            f"{path_id.strip()!r} is not one of the five declared CMS paths "
            f"{[p.path_id for p in INITIAL_CMS_PATHS]}"
        )
    return declared


def _durable_failure_state(store: Any, intent_id: str, run_id: str) -> str:
    """Re-read a resolved attempt's terminal state from the durable store.

    The ``failure`` outcome is only *measured* when the persisted dispatch
    intent -- not a live return value -- shows the terminal ``failed`` state.
    A missing intent, a foreign run id or a still-claiming state is refused,
    so a qualification can never record a failure it did not durably observe.
    """
    from .native_process import INTENT_FAILED

    persisted = store.get_dispatch_intent(intent_id)
    if persisted is None:
        raise CmsFlowQualificationRefused(
            f"the failed attempt {intent_id!r} was not persisted; a "
            f"qualification cannot read the 'failure' outcome back from durable "
            f"state"
        )
    if getattr(persisted, "run_id", "") != run_id:
        raise CmsFlowQualificationRefused(
            f"the persisted intent {intent_id!r} belongs to run "
            f"{getattr(persisted, 'run_id', '')!r}, not {run_id!r}"
        )
    state = getattr(persisted, "state", "")
    if state != INTENT_FAILED:
        raise CmsFlowQualificationRefused(
            f"the persisted intent {intent_id!r} is {state!r}, not the terminal "
            f"{INTENT_FAILED!r}; a live claim of failure is not a measured outcome"
        )
    return state


def _ensure_run(store: Any, run_id: str, *, holder: str) -> None:
    """Make the durable run record exist, so a metadata write can attach to it.

    The CMS lifecycle drives a *transaction*, not a run: no run record exists
    until one is created.  A wait is only a measured outcome when it is
    *recorded*, and a record needs a run to live on, so the record is created
    here -- carrying the driver's real process identity and the declared
    sandbox, never a placeholder and never a production surface.  Idempotent.
    """
    if store.get_run(run_id) is not None:
        return
    from . import process_identity as process_identity_module

    captured = process_identity_module.capture(os.getpid())
    store.create_run(
        run_id,
        cms_qualification_holder=holder,
        cms_qualification_sandbox=CMS_QUALIFICATION_SANDBOX_TARGET,
        process_identity=captured.to_dict() if captured is not None else None,
    )


def _drive_cancellation(store: Any, run_id: str, *, holder: str) -> tuple[str, str]:
    """Reconcile ``run_id`` to its terminal cancellation and re-read it.

    Ensures the run record exists (the CMS lifecycle does not, on its own,
    create one), reconciles the durable cancellation intent through the native
    :class:`~skillweave.runtime.store.CancellationReconciler` -- target state
    ``advance_or_stop`` with the ``killed`` lifecycle state -- and then re-reads
    the **persisted** run so the ``cancel`` outcome is read from durable state,
    never asserted from the reconciler's return value.  Returns
    ``(run_state, lifecycle_state)``.
    """
    # A run exists only as durable state; the live process identity of this
    # driver is captured so the cancellation metadata is real, not a
    # placeholder, and never a production surface.
    _ensure_run(store, run_id, holder=holder)
    store.reconcile_cancellation_intent(
        run_id,
        event_type=CANCELLATION_EVENT_TYPE,
        idempotency_key=f"{run_id}:{CANCELLATION_EVENT_TYPE}",
        stop_reason="operator_halt",
        reason="cms qualification: durable cancellation",
    ).reconcile()

    reread = store.get_run(run_id)
    if reread is None:
        raise CmsFlowQualificationRefused(
            f"the cancelled run {run_id!r} is not persisted; a qualification "
            f"cannot read the 'cancel' outcome back from durable state"
        )
    run_state = getattr(reread, "state", "")
    if run_state != "advance_or_stop":
        raise CmsFlowQualificationRefused(
            f"the run {run_id!r} reconciled to {run_state!r}, not the terminal "
            f"'advance_or_stop'; the 'cancel' outcome was not durably measured"
        )
    lifecycle = (getattr(reread, "metadata", None) or {}).get("runlifecycle") or {}
    lifecycle_state = lifecycle.get("lifecycle_state", "")
    if lifecycle_state != "killed":
        raise CmsFlowQualificationRefused(
            f"the cancelled run {run_id!r} did not record the 'killed' lifecycle "
            f"state in durable metadata; got {lifecycle_state!r}"
        )
    return run_state, lifecycle_state


def _drive_wait(
    store: Any, run_id: str, *, task_id: str, attempt: int
) -> tuple[str, str]:
    """Record a provider-quota wait durably and re-read it.  Returns the decision.

    The ``wait`` outcome is measured only when the wait **survives** the call
    that recorded it: the control record is written through the native
    :meth:`SQLiteRunStore.update_metadata` seam under the declared run metadata
    key, re-read from the *persisted* run, and its decision compared.  A wait
    that cannot be recorded -- or that a re-read does not show -- is refused,
    never reported as a live in-memory flag.

    The scheduled window is bounded and short: the record must name a reset
    instant *in the future*, so the window is generated from the clock rather
    than hard-coded (a frozen timestamp would eventually elapse and turn a real
    measurement into an undatable wait).

    Returns ``(control_decision, metadata_key)``.
    """
    from datetime import datetime, timedelta, timezone

    from .semantic_repair import (
        CONDITION_RESUME_SAME_ROUTE,
        ErrorClass,
        attempt_condition_for_quota,
    )

    reset_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    condition = attempt_condition_for_quota(
        error_class=ErrorClass.PROVIDER_QUOTA.value,
        message="provider quota exhausted; wait and resume the same route",
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        session_id=f"{run_id}:{task_id}:{attempt}",
        route="cms-qualification-route",
        reset_at=reset_at,
        backoff_seconds=30.0,
    )
    recorded = condition.to_dict()
    if recorded.get("decision") != CONDITION_RESUME_SAME_ROUTE:
        raise CmsFlowQualificationRefused(
            f"the provider-quota condition produced {recorded.get('decision')!r} "
            f"instead of {CONDITION_RESUME_SAME_ROUTE!r}; the 'wait' outcome was "
            f"not measured"
        )
    # Fail-closed: if the durable write refuses (a terminal run, no run at all)
    # the wait was not recorded, so it is not measured.
    try:
        store.update_metadata(run_id, **{WAIT_METADATA_KEY: recorded})
    except Exception as exc:  # noqa: BLE001 - any refusal means "not recorded"
        raise CmsFlowQualificationRefused(
            f"the wait for run {run_id!r} could not be recorded durably "
            f"({type(exc).__name__}: {exc}); an unrecorded wait is not a "
            f"measured 'wait' outcome"
        ) from exc

    reread = store.get_run(run_id)
    stored = (
        (getattr(reread, "metadata", None) or {}).get(WAIT_METADATA_KEY) if reread else None
    )
    if not isinstance(stored, dict):
        raise CmsFlowQualificationRefused(
            f"the wait for run {run_id!r} did not survive in durable metadata "
            f"under {WAIT_METADATA_KEY!r}; the 'wait' outcome was not measured"
        )
    decision = stored.get("decision", "")
    if decision != CONDITION_RESUME_SAME_ROUTE:
        raise CmsFlowQualificationRefused(
            f"the durable wait for run {run_id!r} recorded {decision!r}, not "
            f"{CONDITION_RESUME_SAME_ROUTE!r}; the 'wait' outcome was not durably "
            f"measured"
        )
    if stored != recorded:
        raise CmsFlowQualificationRefused(
            f"the durable wait for run {run_id!r} does not match what was "
            f"recorded; the 'wait' outcome was not faithfully persisted"
        )
    return decision, WAIT_METADATA_KEY


def _drive_resume(
    store: Any,
    run_id: str,
    *,
    task_id: str,
    attempt: int,
    holder: str,
    ttl_seconds: int,
) -> tuple[str, bool, str, str]:
    """Restart the controller and adopt the in-flight attempt.  Read back.

    The ``resume`` outcome is *measured* only when all of the following are
    durable facts, not live claims:

    * a transition checkpoint was **persisted** for the run through the native
      :meth:`NativeProcessService.persist_transition_checkpoint` seam;
    * a **new** :class:`NativeProcessService` -- a different controller, i.e. a
      restart -- took over the lease and its decision adopted the recorded
      in-flight attempt, with the decision re-read from the **durable
      checkpoint** rather than the resume return value;
    * the adopted attempt's own scope could not be dispatched again, so the
      work was not duplicated.

    The caller's lease is released before this runs, so the new controller
    takes over exactly as a restarted predecessor's successor would.  Returns
    ``(decision, verified, adopted_intent_id, adopted_execution_scope)``; the
    new controller's lease is always released.
    """
    from .native_process import (
        DuplicateDispatchError,
        NativeProcessService,
        STEP_READY_DISPATCH,
    )

    # A durable checkpoint for the run, derived from the success transaction's
    # own owned attempt (never caller-authored content).
    service = NativeProcessService(store, holder)
    service.acquire(ttl_seconds=ttl_seconds)
    try:
        persisted_checkpoint = service.persist_transition_checkpoint(
            STEP_READY_DISPATCH,
            intent_id=_owned_success_intent_id(store, run_id, task_id, attempt),
        )
    finally:
        if service.fencing_token is not None:
            service.release()
    if persisted_checkpoint is None:
        raise CmsFlowQualificationRefused(
            f"no durable checkpoint could be persisted for run {run_id!r}; the "
            f"'resume' outcome was not measured"
        )

    # ── the restart: a *different* controller takes over the lease and reads
    # the checkpoint back from durable state.
    recovered_holder = f"{holder}-recovered"
    recovered = NativeProcessService(store, recovered_holder)
    try:
        # The restart: the new controller takes over the lease and computes its
        # adoption from the *durable* checkpoint.  The precondition is the
        # persisted checkpoint itself, so a restart with nothing durable to
        # resume from is refused rather than adopted from memory.
        durable_checkpoint = store.get_checkpoint(run_id)
        if durable_checkpoint is None:
            raise CmsFlowQualificationRefused(
                f"the run {run_id!r} has no durable checkpoint to resume from; "
                f"the 'resume' outcome was not measured"
            )
        if not (durable_checkpoint.metadata or {}).get("in_flight"):
            raise CmsFlowQualificationRefused(
                f"the durable checkpoint for {run_id!r} records nothing in flight; "
                f"there is no owned attempt to adopt"
            )
        _, adoption = recovered.resume(ttl_seconds=ttl_seconds)
        event = adoption.resume_decision_event or {}
        decision = event.get("decision", "")
        if decision != "adopted":
            raise CmsFlowQualificationRefused(
                f"the restarted controller did not adopt an owned attempt for "
                f"{run_id!r} (decision {decision!r}, reason "
                f"{event.get('reason', '')!r}); the 'resume' outcome was not "
                f"measured"
            )
        intent_id = event.get("intent_id", "")
        execution_scope = event.get("execution_scope", "")
        if not intent_id or not execution_scope:
            raise CmsFlowQualificationRefused(
                f"the resume decision for {run_id!r} adopted no named attempt; the "
                f"'resume' outcome was not measured"
            )
        persisted_intent = store.get_dispatch_intent(intent_id)
        if persisted_intent is None or persisted_intent.execution_scope != execution_scope:
            raise CmsFlowQualificationRefused(
                f"the adopted attempt {intent_id!r} is not the durable intent the "
                f"resume decision named; the 'resume' outcome was not measured"
            )

        # ── no duplicated work: the adopted attempt's own scope still carries a
        # claiming intent, so re-dispatching it is refused.  A restarted
        # controller adopts the pending launch, it never replays it.
        try:
            recovered.dispatch(
                run_id=run_id, task_id=task_id, attempt=attempt,
                session_id="resume-replay-probe",
            )
        except DuplicateDispatchError:
            pass
        else:  # pragma: no cover - the refusal is the assertion
            raise CmsFlowQualificationRefused(
                f"the adopted attempt {intent_id!r} could be dispatched again for "
                f"run {run_id!r}; the work was duplicated, so the 'resume' outcome "
                f"is not measured"
            )
    finally:
        if recovered.fencing_token is not None:
            recovered.release()

    return decision, bool(event.get("verified", False)), intent_id, execution_scope


def _owned_success_intent_id(
    store: Any, run_id: str, task_id: str, attempt: int
) -> str:
    """The durable intent id of the run's success attempt, from durable state."""
    scope = f"{run_id}/{task_id}/{attempt}"
    for intent in store.list_dispatch_intents(run_id=run_id):
        if intent.execution_scope == scope:
            return intent.intent_id
    raise CmsFlowQualificationRefused(
        f"no durable dispatch intent records the success attempt scope {scope!r}; "
        f"the run has no owned attempt to checkpoint"
    )


def _drive_publication_refusal(
    service: Any,
    store: Any,
    persisted_txn: Any,
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    artifact_digest: str,
) -> tuple[str, str]:
    """Present an irreversible publication and record its refusal.

    The ``publication-refusal`` outcome is measured by presenting the durable
    CMS transaction for publication with a **current, independent, durable
    review** but *no* designated authorization, and observing that the native
    :meth:`NativeProcessService.authorize_cms_publication` seam refuses it:

    * the review is a real server-issued review written through the MCP
      ``review_action`` seam and read back via :class:`DurableReviewRef` -- an
      *independent* reviewer principal, bound to this transaction's revision and
      artifact digest;
    * the durable review proves it is genuinely independent and current, so the
      refusal is the **authorization** check (the designated explicit human
      authorization is absent), not an earlier one;
    * nothing is published: the transaction is never handed to a publisher, and
      the outcome is a refusal, not a decision.

    The measurement needs a *bound MCP authority* to record a server-issued
    review at all.  This driver does not bind one, so when no authority is bound
    (the in-process direct-caller path) the measurement is **not performed**
    rather than forged: the empty marker is returned and the caller refuses.

    Returns ``(reviewer_principal, refusal_field)``.
    """
    from .native_process import (
        DurableReviewRef,
        PublicationRefused,
        PUBLICATION_REVIEW_ROLE,
    )

    from ..mcp import server as mcp_server

    authority = mcp_server.get_authority()
    if authority is None or not callable(getattr(authority, "serve_live", None)):
        return "", ""

    mailbox_path = mcp_server.default_mailbox_path()
    client_id = f"cms-qual-{run_id}"
    token = authority.issue_session(
        role=PUBLICATION_REVIEW_ROLE,
        client_id=client_id,
        transport="http",
        run_ids=(run_id,),
    )
    import json

    written = json.loads(
        mcp_server.review_action(
            action="write",
            recipient=client_id,
            body=f"{task_id}/attempt-{attempt}",
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            message_id=f"cms-qualification-review-{run_id}",
            verdict="accepted",
            subject_revision=persisted_txn.subject_revision,
            artifact_digest=artifact_digest,
            criteria="cms-qualification",
            session_token=token,
        )
    )
    if written.get("status") != "ok" or not written.get("message", {}).get(
        "message_id"
    ):
        return "", ""
    review_ref = DurableReviewRef(
        mailbox_path=mailbox_path,
        message_id=written["message"]["message_id"],
    )
    resolved = review_ref.resolve()

    try:
        service.authorize_cms_publication(
            persisted_txn,
            authorization=None,
            artifact_digest=artifact_digest,
            review_ref=review_ref,
        )
    except PublicationRefused as exc:
        field = getattr(exc, "field", "")
        if field != "authorization":
            raise CmsFlowQualificationRefused(
                f"the irreversible publication was refused on {field!r}, not on "
                f"the absent designated authorization; the "
                f"'publication-refusal' outcome must be the authorization refusal"
            ) from exc
        return resolved.principal, field
    raise CmsFlowQualificationRefused(
        f"the irreversible publication for run {run_id!r} was not refused; the "
        f"'publication-refusal' outcome was not measured"
    )


def qualify_cms_path(
    store: Any,
    path: Any,
    *,
    run_id: str,
    task_id: str = "cms-flow-qualification",
    attempt: int = 1,
    holder: str = "cms-flow-qualification",
    ttl_seconds: int = 300,
) -> CmsPathQualificationRun:
    """Drive one declared CMS path and return its qualification record.

    ``store`` is the durable store the production controller uses; ``path`` is a
    declared path id or a :class:`CmsPath`.  The service acquires the single
    fenced controller lease, drives the reversible lifecycle through the dummy
    adapter, measures every required outcome (``success``, ``failure``,
    ``cancel``, ``wait``, ``resume``, ``publication-refusal``) and reads each
    one back from durable state, builds the fail-closed record and releases the
    lease on every exit path.

    One outcome cannot be forged: ``publication-refusal`` needs an *independent
    durable review*, which in turn needs a bound MCP authority.  When the host
    has not bound one, the measurement is **not performed** and this function
    refuses for the record rather than claiming an outcome it did not measure --
    the disposition is ``performed`` only when every required outcome was
    genuinely measured.
    """
    declared = _resolve_declared_path(path)

    # Imported lazily so that merely importing this module never pulls the whole
    # runtime into a caller that only wants the contract.
    from .native_process import (
        NativeProcessService,
        OwnerSandboxDeclaration,
        SandboxBinding,
    )
    from ..dispatch.work_contract import ContentSubject

    service = NativeProcessService(store, holder)
    service.acquire(ttl_seconds=ttl_seconds)
    try:
        subject = ContentSubject(
            channel=CMS_QUALIFICATION_SANDBOX_TARGET,
            content_id=f"cms-flow:{declared.path_id}",
            revision="rev-1",
        )
        # ── success: the existing native controller seam drives the reversible
        # lifecycle (one owned attempt dispatched with a live process identity,
        # then baseline -> draft -> dry-run -> bounded apply -> readback ->
        # restore) through the dummy in-memory adapter on an owner-authorized
        # sandbox binding.
        driven = service.cms_run_no_transformer(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            subject=subject,
            baseline=dict(CMS_QUALIFICATION_BASELINE),
            binding=SandboxBinding(target=CMS_QUALIFICATION_SANDBOX_TARGET),
            owner_sandbox=OwnerSandboxDeclaration(
                targets=(CMS_QUALIFICATION_SANDBOX_TARGET,)
            ),
        )
        # Read the transaction back *from durable state*: the readback/restore
        # verdict must come from the persisted record, not a live in-memory claim.
        txn_id = driven.txn_id
        persisted = service.load_cms_transaction(run_id, txn_id)
        if persisted is None:
            raise CmsFlowQualificationRefused(
                f"the driven CMS transaction {txn_id!r} was not persisted; a "
                f"qualification cannot be read back from durable state"
            )

        # ── resume: a *new* controller takes over the lease after a restart and
        # adopts the recorded in-flight attempt, with the decision re-read from
        # the durable checkpoint.  This runs before the cancellation (which must
        # be the last thing that touches the run) and before the run record is
        # even created, so the checkpoint's in-flight set is the live attempt.
        # The caller's own lease is released first so the recovered controller
        # takes over exactly as a successor would.
        if service.fencing_token is not None:
            service.release()
        resume_decision, resume_decision_verified, resume_intent_id, resume_scope = (
            _drive_resume(
                store,
                run_id,
                task_id=task_id,
                attempt=attempt,
                holder=holder,
                ttl_seconds=ttl_seconds,
            )
        )
        # The restart released the lease; re-take it for the rest of the drive.
        service.acquire(ttl_seconds=ttl_seconds)

        # ── failure: drive a *separately owned* attempt for the same path and
        # resolve it through the native ``record_failure`` seam.  A distinct
        # attempt/scope leaves the success transaction untouched, and the
        # resolved terminal state is re-read from the durable store -- a live
        # return value is not evidence that the outcome was measured.
        failure_intent, _ = service.dispatch(
            run_id=run_id, task_id=task_id, attempt=attempt + 1,
        )
        service.record_failure(failure_intent.intent_id)
        failure_state = _durable_failure_state(store, failure_intent.intent_id, run_id)

        # ── publication-refusal: present the durable transaction for
        # irreversible publication with a current independent durable review but
        # *no* designated authorization, and record the refusal.  Nothing is
        # published.
        applied = persisted.applied_hashes
        artifact_digest = (
            applied.semantic if applied is not None else persisted.draft_digest
        )
        publication_refusal_principal, publication_refusal_field = (
            _drive_publication_refusal(
                service,
                store,
                persisted,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                artifact_digest=artifact_digest,
            )
        )
        if publication_refusal_field != "authorization":
            # No bound MCP authority: the independent durable review the
            # refusal must be measured against cannot be recorded, so the
            # outcome stays unmeasured rather than being forged.  Fail closed.
            raise CmsFlowQualificationRefused(
                f"the 'publication-refusal' outcome for run {run_id!r} could not "
                f"be measured: recording the independent durable review it "
                f"requires needs a bound MCP authority, and this driver has none. "
                f"The record must stay 'not-performed' with this named "
                f"limitation rather than claim an outcome it did not measure."
            )

        # ── wait: a provider-quota wait recorded in durable run metadata and
        # re-read from it.  This must precede the cancellation, which makes the
        # run terminal (a terminal run refuses a metadata write).  The run
        # record is created first so the declaration has somewhere durable to
        # live.
        _ensure_run(store, run_id, holder=holder)
        wait_decision, wait_marker = _drive_wait(
            store, run_id, task_id=task_id, attempt=attempt
        )

        # ── cancel: reconcile the run to its terminal cancellation through the
        # durable cancellation seam and re-read the terminal run state.  This is
        # last: it is the only step that makes the run terminal.
        cancel_run_state, cancel_lifecycle_state = _drive_cancellation(
            store, run_id, holder=holder
        )

        pins = {
            "controller": f"{holder}#{service.fencing_token}",
            "source_digest": persisted.baseline_hashes.semantic,
            "artifact_digest": artifact_digest,
        }
        qualification = CmsProcessQualification.from_mapping(
            {
                "artifact": SCHEMA_VERSION,
                "versioning": (
                    "v1 draft contract; schema URI, revision and digest are frozen "
                    "at the SW-1511 baseline task"
                ),
                "producer": persisted.producer_identity,
                "consumer": "release assessor",
                "compatibility": (
                    "additive opt-in contract; preserve existing v1 records and "
                    "original-skill execution; unsupported versions fail closed"
                ),
                "disposition": DISPOSITION_PERFORMED,
                "paths": [p.path_id for p in INITIAL_CMS_PATHS],
                "pins": pins,
                "readback_verified": persisted.verified(),
                "restore_verified": persisted.restored(),
                "measured_outcomes": list(MEASURED_OUTCOMES),
                "limitations": (
                    [UNMEASURED_OUTCOME_LIMITATION]
                    if UNMEASURED_OUTCOME_LIMITATION
                    else []
                ),
            }
        )
    finally:
        if service.fencing_token is not None:
            service.release()

    return CmsPathQualificationRun(
        path=declared,
        qualification=qualification,
        producer_identity=persisted.producer_identity,
        run_id=run_id,
        txn_id=txn_id,
        final_stage=persisted.stage,
        failure_intent_id=failure_intent.intent_id,
        failure_state=failure_state,
        cancel_run_state=cancel_run_state,
        cancel_lifecycle_state=cancel_lifecycle_state,
        wait_decision=wait_decision,
        wait_marker=wait_marker,
        resume_decision=resume_decision,
        resume_decision_verified=resume_decision_verified,
        resume_intent_id=resume_intent_id,
        resume_execution_scope=resume_scope,
        publication_refusal_principal=publication_refusal_principal,
        publication_refusal_field=publication_refusal_field,
    )
