"""The native process service (SW-ORCH-001 criteria 1-3).

One native process service drives ready dispatch, independent review, repair,
re-review, controlled integration and dependent waves -- no external bridge is
required.  It owns three things the interactive controller used to hold in a
single process's memory:

* **durable dispatch intents** (criterion 2) -- one row per owned attempt,
  with a single *claiming* intent per execution scope; a second intent for the
  same scope is refused, so a duplicate launch never starts a second worker;
* **verified process/session identity** (criterion 2) -- the native process
  identity (:mod:`skillweave.runtime.process_identity`) is captured at launch
  and re-verified before any later action trusts the PID; only a ``match``
  authorises a takeover or signal, a recycled PID or a stale command is
  refused;
* **a single controller lease with fencing** (criterion 2) -- one controller
  at a time, carrying a monotonically increasing fencing token.  A recovering
  controller that takes over the lease reads a *higher* token; any write
  presented with a superseded token is refused, so a stale controller cannot
  be accepted.

Criterion 1's loop is expressed as a declared, ordered set of *steps*
(``STEPS``): ready dispatch, independent review, repair, re-review, controlled
integration and a dependent wave.  Each step is carried by a stored intent, so
the loop is durable and a recovered controller resumes it from the intents
rather than from a live process.

Criterion 3 (dynamic readiness refill) is served by :class:`ReadinessPlan` /
:func:`plan_readiness`: actual free capacity measured from fresh observations
(via ``dagscheduler.measure_max_parallel``) refills the ready set subject to
dependency order and write-surface exclusion, and the *worker count* (intents
launched) is reported separately from *useful progress* (intents that
completed), so a lane that launched and made no progress is never counted as
progress.

Boundary contracts
------------------
Two frozen v1 contracts live here, next to the classes that implement them
(the baseline task did not freeze a canonical URI, so the URI/revision/digest
are frozen at this seam):

* ``native-process-checkpoint-v1`` -- produced by the *controller lease
  holder*, consumed by a *recovering controller* (a ``storage`` boundary).
* ``owned-attempt-dispatch-v1`` -- produced by the *native process service*,
  consumed by a *harness adapter* (a ``process`` boundary).

Both are **additive and opt-in**: a record is only ever written when a caller
hands one to the service, and reading/validating an existing v1 record is
unchanged.  An unsupported version fails closed (:class:`UnsupportedContractVersion`)
and is never repaired into a shape this service happens to support.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
import hashlib
import json
import os
import signal

from . import process_identity
from . import process_identity as process_identity_module
from .dagscheduler import (
    ReservationSnapshot,
    RouteCapacitySnapshot,
    Task,
    build_lanes,
    measure_max_parallel,
)
from .write_scope import WriteScopeClaim
from ..trace.gate_receipt import (
    ACCEPTANCE_ACCEPTED,
    ACCEPTANCE_STATES,
    AttemptReview,
    IndependentGateReceipt,
    MissingProducerArtifact,
    ReviewBinding,
    StaleReviewError,
    assert_review_current,
    collect_package_review,
    release_dependents,
)

__all__ = [
    # boundary contracts
    "CHECKPOINT_SCHEMA_ID",
    "CHECKPOINT_SCHEMA_VERSION",
    "SUPPORTED_CHECKPOINT_VERSIONS",
    "RESUME_DECISION_SCHEMA_ID",
    "RESUME_ADOPTED",
    "RESUME_REFUSED_NO_OWNED_ATTEMPT",
    # criterion 2: harness-continuation-v1
    "HARNESS_CONTINUATION_SCHEMA_ID",
    "HARNESS_CONTINUATION_SCHEMA_VERSION",
    "SUPPORTED_HARNESS_CONTINUATION_VERSIONS",
    "HARNESS_SESSION_RESUME_CAPABILITY",
    "CONTINUATION_SESSION_RESUME",
    "CONTINUATION_CHECKPOINT",
    "LIMITATION_UNSUPPORTED_RESUME",
    "LIMITATION_NO_ATTESTED_CAPABILITY",
    "LIMITATION_SESSION_UNKNOWN",
    "HarnessContinuation",
    "plan_harness_continuation",
    # criterion 3: owned-child drain + useful-slot occupancy
    "CHILD_DRAIN_SCHEMA_ID",
    "CHILD_DRAIN_SCHEMA_VERSION",
    "SUPPORTED_CHILD_DRAIN_VERSIONS",
    "CHILD_DRAINED",
    "CHILD_DRAIN_REFUSED",
    "CHILD_DRAIN_REFUSED_NOT_OWNED",
    "CHILD_DRAIN_REFUSED_NO_RECORD",
    "CHILD_PROBE",
    "CHILD_HEARTBEAT_ONLY",
    "USEFUL_SLOT_OCCUPIED",
    "USEFUL_SLOT_EXEMPT_STALLED",
    "USEFUL_SLOT_FREED",
    "OwnedChildDrain",
    "plan_child_drain",
    "DISPATCH_SCHEMA_ID",
    "DISPATCH_SCHEMA_VERSION",
    "SUPPORTED_DISPATCH_VERSIONS",
    "UnsupportedContractVersion",
    "NativeProcessCheckpoint",
    "validate_native_process_checkpoint",
    "OwnedAttemptDispatch",
    "validate_owned_attempt_dispatch",
    # criterion 4: the compact changed-state/evidence view
    "EVIDENCE_VIEW_SCHEMA_ID",
    "EVIDENCE_VIEW_SCHEMA_VERSION",
    "SUPPORTED_EVIDENCE_VIEW_VERSIONS",
    "ACCEPTED_GATE_STATUSES",
    "EvidenceDelta",
    "validate_evidence_delta",
    "build_evidence_delta",
    "evidence_view_fingerprint",
    "evidence_view_header",
    "ARCHITECT_HEALTHY",
    "ARCHITECT_QUOTA_EXHAUSTED",
    "ContinuationDecision",
    "plan_continuation",
    # criterion 5: repository and non-repository subjects
    "CONTENT_TXN_SCHEMA_ID",
    "CONTENT_TXN_SCHEMA_VERSION",
    "SUPPORTED_CONTENT_TXN_VERSIONS",
    "INTEGRATION_GIT_MERGE",
    "INTEGRATION_CONTENT_TRANSACTION",
    "NON_REPOSITORY_INTEGRATION_KINDS",
    "ContentTransaction",
    "validate_content_transaction",
    "subject_kind_of",
    "is_repository_subject",
    "integration_mode_for",
    "resolve_integration",
    "UnknownSubjectKind",
    "SubjectMismatchError",
    "WrongIntegrationModeError",
    # SW-1511-CMS-TRANSACTION criteria 1-3: the neutral CMS capability contract,
    # the managed transaction owner and the publication authorization binding
    "CMS_TXN_SCHEMA_ID",
    "CMS_TXN_SCHEMA_VERSION",
    "SUPPORTED_CMS_TXN_VERSIONS",
    "CMS_CAPABILITIES",
    "CMS_STAGES",
    "CMS_STAGE_BASELINE",
    "CMS_STAGE_DRAFTED",
    "CMS_STAGE_DRY_RUN",
    "CMS_STAGE_APPLIED",
    "CMS_STAGE_READBACK",
    "CMS_STAGE_RESTORED",
    "CMS_BINDING_SANDBOX",
    "CMS_BINDING_PRODUCTION",
    "CMS_BINDING_KINDS",
    "CmsCapabilityContract",
    "validate_cms_capability_contract",
    "CmsRawHashes",
    "RawContentAdapter",
    "CmsTransaction",
    "validate_cms_transaction",
    "CmsCapabilityError",
    "CmsStageError",
    "SandboxBinding",
    "CmsBindingRefused",
    "resolve_cms_sandbox_binding",
    "CMS_PRODUCTION_TARGET_MARKERS",
    "OwnerSandboxDeclaration",
    "resolve_owner_sandbox_target",
    "PUBLICATION_ACTION_KIND",
    "PUBLICATION_REVIEW_ROLE",
    "DURABLE_REVIEW_MESSAGE_KIND",
    "DurableReviewRef",
    "ResolvedReview",
    "PublicationAuthorization",
    "PublicationDecision",
    "PublicationRefused",
    "authorize_publication",
    # criterion 4: no-text-transformer CMS run + external HOLD
    "NO_TXT_TXN_STAGE_COMPLETED",
    "EXTERNAL_HOLD_PROVIDER_NOT_HERE",
    "ExternalHold",
    # criterion 5: candidate-bound milestone receipt
    "MILESTONE_RECEIPT_SCHEMA_ID",
    "MILESTONE_RECEIPT_SCHEMA_VERSION",
    "SUPPORTED_MILESTONE_RECEIPT_VERSIONS",
    "MilestoneReceiptRefused",
    "CmsMilestoneReceipt",
    "validate_cms_milestone_receipt",
    "require_cms_milestone_receipt",
    # controlled loop
    "STEP_READY_DISPATCH",
    "STEP_INDEPENDENT_REVIEW",
    "STEP_REPAIR",
    "STEP_RE_REVIEW",
    "STEP_CONTROLLED_INTEGRATION",
    "STEP_DEPENDENT_WAVE",
    # SW-1511-REVIEW criteria 1 and 3: controller acceptance consumer seam
    "record_acceptance",
    "release_accepted_dependents",
    "STEPS",
    "INTENT_LAUNCHED",
    "INTENT_REVIEWING",
    "INTENT_REPAIRING",
    "INTENT_REVIEWED",
    "INTENT_INTEGRATING",
    "INTENT_INTEGRATED",
    "INTENT_FAILED",
    "INTENT_TERMINAL",
    "INTENT_STATES",
    "INTENT_CLAIMING_STATES",
    # verdict-driven guarded loop (SW-SCOPE-010 unit A1)
    "REVIEW_PASS",
    "REVIEW_FAIL",
    "REVIEW_VERDICTS",
    # lease
    "LEASE_KEY",
    "ControllerLease",
    "LeaseRefusedError",
    "StaleFencingTokenError",
    "LeaseNotHeldError",
    # dispatch intents
    "DispatchIntent",
    "DuplicateDispatchError",
    "IntentStateConflictError",
    # readiness
    "ReadinessPlan",
    "plan_readiness",
    # the service
    "NativeProcessService",
    "NotControllerError",
    # attempt upstream telemetry (additive)
    "ATTEMPT_UPSTREAM_SCHEMA_ID",
    "ATTEMPT_UPSTREAM_SCHEMA_VERSION",
    "SUPPORTED_ATTEMPT_UPSTREAM_VERSIONS",
    "AttemptUpstreamRefused",
    "AttemptUpstreamReceipt",
    "validate_attempt_upstream_receipt",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 instant (UTC when naive); ``None`` when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ═══════════════════════════════════════════════════════════════════════════
# Boundary contracts: native-process-checkpoint-v1 and owned-attempt-dispatch-v1
# ═══════════════════════════════════════════════════════════════════════════
#
# The PRD freezes two draft v1 contracts for this task.  The baseline did not
# freeze a canonical URI/revision/digest, so those are frozen here, next to the
# classes that implement them.  Both contracts are additive and opt-in: an
# existing v1 record validates unchanged, and a record carrying any version
# this service does not support is refused rather than coerced.

CHECKPOINT_SCHEMA_ID = (
    "https://skillweave.dev/schemas/storage/native-process-checkpoint/v1"
)
CHECKPOINT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CHECKPOINT_VERSIONS = frozenset({CHECKPOINT_SCHEMA_VERSION})

# SW-1511-RECOVERY criterion 1: the resume decision a recovered controller
# records when it adopts (or refuses to adopt) in-flight work.  Additive to the
# v1 checkpoint: an existing v1 record without it round-trips unchanged.
RESUME_DECISION_SCHEMA_ID = (
    "https://skillweave.dev/schemas/storage/native-process-resume-decision/v1"
)

# SW-1511-RECOVERY criterion 2: the harness-continuation contract.  The PRD
# declares ``harness-continuation-v1`` for the **harness adapter** producer and
# the **native process recovery** consumer; its URI/revision/digest were not
# frozen at the baseline, so they are frozen here, next to the decision that
# implements them.  The contract is *opt-in* and *fail-closed*: an adapter that
# does not attest a session-resume capability is reported as a named limitation
# (a checkpoint continuation), never granted fabricated session-UUID continuity.
HARNESS_CONTINUATION_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/harness-continuation/v1"
)
HARNESS_CONTINUATION_SCHEMA_VERSION = "1.0.0"
SUPPORTED_HARNESS_CONTINUATION_VERSIONS = frozenset(
    {HARNESS_CONTINUATION_SCHEMA_VERSION}
)

#: The single capability an adapter must attest before this controller uses an
#: *actual* session resume.  An adapter that does not attest it falls closed to
#: a checkpoint continuation -- the recovered controller resumes owned work
#: from durable state rather than pretending the predecessor's session UUID is
#: still live.
HARNESS_SESSION_RESUME_CAPABILITY = "session-resume"

#: The two continuation modes.  ``SESSION_RESUME`` is admissible *only* when the
#: adapter attested the capability; otherwise the mode is
#: ``CHECKPOINT_CONTINUATION``, which names the limitation instead of carrying a
#: session id forward.
CONTINUATION_SESSION_RESUME = "session_resume"
CONTINUATION_CHECKPOINT = "checkpoint_continuation"
_CONTINUATION_MODES: frozenset[str] = frozenset(
    {CONTINUATION_SESSION_RESUME, CONTINUATION_CHECKPOINT}
)

#: The named limitations a checkpoint continuation reports.  A limitation is a
#: machine-readable reason the actual session was not resumed, so a consumer
#: never mistakes a checkpoint continuation for UUID continuity.
LIMITATION_UNSUPPORTED_RESUME = "unsupported_session_resume_capability"
LIMITATION_NO_ATTESTED_CAPABILITY = "no_attested_capability"
LIMITATION_SESSION_UNKNOWN = "session_identity_unknown"

DISPATCH_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/owned-attempt-dispatch/v1"
)
DISPATCH_SCHEMA_VERSION = "1.0.0"
SUPPORTED_DISPATCH_VERSIONS = frozenset({DISPATCH_SCHEMA_VERSION})

# SW-1511-RECOVERY criterion 3: the owned-child drain report.  The native
# process recovery consumer drains a *background probe child* by its **owned
# identity** -- a child whose recorded identity still verifies as the attempt
# the controller launched -- and reports a heartbeat-only stalled child as a
# **useful-slot exemption** so it neither suppresses a completed review nor
# occupies a useful slot.  Additive to the v1 checkpoint: an existing v1 record
# carries none and round-trips unchanged.
CHILD_DRAIN_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/owned-child-drain/v1"
)
CHILD_DRAIN_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CHILD_DRAIN_VERSIONS = frozenset({CHILD_DRAIN_SCHEMA_VERSION})

#: The typed outcomes of draining one background probe child.  Each is named so
#: a consumer never mistakes a refused drain for a performed one.
CHILD_DRAINED = "drained"
CHILD_DRAIN_REFUSED = "refused"
#: The identity-verification outcomes that refuse a drain.  A child whose
#: recorded identity cannot be re-verified as the owned attempt is *never*
#: signalled: a recycled PID, a stale command, a foreign worker or an
#: unobservable process is reported distinctly.
CHILD_DRAIN_REFUSED_NOT_OWNED = "not_owned_identity"
CHILD_DRAIN_REFUSED_NO_RECORD = "no_recorded_identity"
_CHILD_DRAIN_OUTCOMES: frozenset[str] = frozenset(
    {CHILD_DRAINED, CHILD_DRAIN_REFUSED}
)

#: The kinds of child the drain distinguishes.  A **probe** child is a
#: background tool the attempt launched (a concurrency/search probe): it is a
#: *useful slot* only while it is genuinely progressing.  A **heartbeat-only**
#: child emits liveness traffic but no useful progress -- it is a *stall*, not
#: a worker, so it is excluded from useful-slot occupancy.
CHILD_PROBE = "probe"
CHILD_HEARTBEAT_ONLY = "heartbeat_only"

#: The measured useful-slot occupancy states a refill reasons about.
USEFUL_SLOT_OCCUPIED = "occupied"
USEFUL_SLOT_EXEMPT_STALLED = "exempt_stalled"
USEFUL_SLOT_FREED = "freed"


class UnsupportedContractVersion(ValueError):
    """A boundary record carries a version this service does not support.

    The contract is fail-closed: an unknown version is refused, never
    upgraded, downgraded or read as a best-effort approximation of a supported
    one.  *artifact* names the contract (``native-process-checkpoint-v1`` or
    ``owned-attempt-dispatch-v1``).
    """

    def __init__(self, artifact: str, version: Any, supported: frozenset):
        self.artifact = artifact
        self.version = version
        self.supported = supported
        super().__init__(
            f"unsupported {artifact} schema_version {version!r}; expected one "
            f"of {sorted(supported)}"
        )


@dataclass(frozen=True)
class NativeProcessCheckpoint:
    """A ``native-process-checkpoint-v1`` record.

    Produced by the **controller lease holder** for a **recovering
    controller**: it is the durable state a fresh controller needs to resume
    the owned process without the old controller's live memory -- which
    attempts are in flight under which fencing token, and where the journal
    cursor sat.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the checkpoint must name the run and the fencing token it was written
      under (a token-less checkpoint cannot be fenced against);
    * the journal offset is a non-negative integer.
    """

    run_id: str
    fencing_token: int
    journal_offset: int = 0
    in_flight: tuple[tuple[str, str], ...] = ()
    route: str = ""
    session_id: str = ""
    captured_at: str = field(default_factory=_now_iso)
    # Optional, additive (v1-compatible): a resume statement the recovering
    # controller publishes when it does NOT admit the predecessor's in-flight
    # intent -- the durable decision a continuing controller reads so it adopts
    # (or refuses) the attempt instead of re-dispatching it.  ``None`` for a
    # checkpoint that carries no continuation statement, so an existing v1
    # record round-trips unchanged.
    resume_decision_event: Optional[dict] = None
    # Optional, additive (v1-compatible, criterion 2): the ``harness-
    # continuation-v1`` decision the recovering controller used for the owned
    # attempt -- an actual session resume *only* when the adapter attested the
    # capability, otherwise a named-limitation checkpoint continuation that
    # carries no session UUID.  ``None`` for a checkpoint that was not produced
    # through the resume path, so an existing v1 record round-trips unchanged.
    harness_continuation: Optional[dict] = None
    # Optional, additive (v1-compatible, criterion 3): the owned-child drain
    # report the recovering controller publishes -- which background probe
    # children it drained by owned identity, which it refused to signal, and
    # which heartbeat-only stalls it exempted from useful-slot occupancy.
    # ``None`` for a checkpoint produced without draining children, so an
    # existing v1 record round-trips unchanged.
    child_drain_report: Optional[dict] = None
    schema_version: str = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CHECKPOINT_VERSIONS:
            raise UnsupportedContractVersion(
                "native-process-checkpoint-v1", self.schema_version,
                SUPPORTED_CHECKPOINT_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("native-process checkpoint must name its run_id")
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int):
            raise ValueError(
                "native-process checkpoint must carry an integer fencing_token"
            )
        if self.fencing_token < 0:
            raise ValueError(
                "native-process checkpoint fencing_token must be >= 0"
            )
        if isinstance(self.journal_offset, bool) or not isinstance(self.journal_offset, int):
            raise ValueError(
                "native-process checkpoint journal_offset must be an integer"
            )
        if self.journal_offset < 0:
            raise ValueError(
                "native-process checkpoint journal_offset must be >= 0"
            )
        if self.resume_decision_event is not None:
            event = self.resume_decision_event
            if not isinstance(event, Mapping):
                raise ValueError(
                    "native-process checkpoint resume_decision_event must be a "
                    "mapping when present"
                )
            decision = event.get("decision")
            if decision not in _RESUME_DECISIONS:
                raise ValueError(
                    f"native-process checkpoint resume decision {decision!r} is "
                    f"not one of {sorted(_RESUME_DECISIONS)}"
                )
            # An admission is a durable claim that the predecessor's intent is
            # still the owned attempt of this run; it must name it.
            if decision == RESUME_ADOPTED:
                if not event.get("intent_id"):
                    raise ValueError(
                        "a resume adoption must name the owned intent it resumes"
                    )
                if not event.get("execution_scope"):
                    raise ValueError(
                        "a resume adoption must name the execution scope it "
                        "resumes"
                    )
        if self.harness_continuation is not None:
            continuation = self.harness_continuation
            if not isinstance(continuation, Mapping):
                raise ValueError(
                    "native-process checkpoint harness_continuation must be a "
                    "mapping when present"
                )
            # Re-run the continuation contract's own fail-closed invariants so a
            # checkpoint cannot carry a fabricated session resume (a session id
            # with no attested capability) or a session id on a checkpoint
            # continuation.
            HarnessContinuation(
                run_id=continuation.get("run_id", self.run_id),
                mode=continuation.get("mode", ""),
                resume_capability_attested=bool(
                    continuation.get("resume_capability_attested", False)
                ),
                session_id=continuation.get("session_id", ""),
                intent_id=continuation.get("intent_id", ""),
                limitation=continuation.get("limitation", ""),
                reason=continuation.get("reason", ""),
            )
        if self.child_drain_report is not None:
            report = self.child_drain_report
            if not isinstance(report, Mapping):
                raise ValueError(
                    "native-process checkpoint child_drain_report must be a "
                    "mapping when present"
                )
            # The report re-runs the drain's own fail-closed invariants: every
            # child outcome is a declared one, and a *drained* child must have
            # carried a verified owned identity (a drain without a verified
            # identity would be a signal to an un-owned process).
            for child in report.get("children", ()):
                outcome = child.get("outcome")
                if outcome not in _CHILD_DRAIN_OUTCOMES:
                    raise ValueError(
                        f"child drain outcome {outcome!r} is not one of "
                        f"{sorted(_CHILD_DRAIN_OUTCOMES)}"
                    )
                if outcome == CHILD_DRAINED and not child.get("verified"):
                    raise ValueError(
                        "a drained child must have been verified as the owned "
                        "identity; refusing a drain with no verified identity"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CHECKPOINT_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "fencing_token": self.fencing_token,
            "journal_offset": self.journal_offset,
            "in_flight": [list(pair) for pair in self.in_flight],
            "route": self.route,
            "session_id": self.session_id,
            "captured_at": self.captured_at,
            "resume_decision_event": (
                None if self.resume_decision_event is None
                else dict(self.resume_decision_event)
            ),
            "harness_continuation": (
                None if self.harness_continuation is None
                else dict(self.harness_continuation)
            ),
            "child_drain_report": (
                None if self.child_drain_report is None
                else dict(self.child_drain_report)
            ),
        }


@dataclass(frozen=True)
class RestartAdoption:
    """The restart decision a *restarted* controller reads (criterion 2).

    ``checkpoint`` is the run-keyed transition checkpoint the predecessor
    wrote; ``intent`` is the *ordering-first* in-flight dispatch intent -- the
    first in the store's deterministic ``(created_at, intent_id)`` order, read
    from the durable intents themselves, never from an index the caller
    supplies.  ``adopted`` is true only when that intent's recorded owned
    identity still verifies against the live OS, in which case
    ``owned_identity`` repeats the *durable* identity that was verified -- a
    restart never manufactures one.  When nothing can be adopted, ``hold``
    names the exact refusal (a named checkpoint hold) and ``reason`` the
    declared verification reason, and the predecessor's launch intent is never
    replayed.
    """

    run_id: str
    checkpoint: Any
    intent: Optional["DispatchIntent"]
    adopted: bool
    hold: str = ""
    reason: str = ""
    owned_identity: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "intent_id": "" if self.intent is None else self.intent.intent_id,
            "execution_scope": (
                "" if self.intent is None else self.intent.execution_scope
            ),
            "adopted": self.adopted,
            "hold": self.hold,
            "reason": self.reason,
            "owned_identity": self.owned_identity,
        }


def validate_native_process_checkpoint(record: Mapping[str, Any]) -> None:
    """Refuse a checkpoint record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered checkpoint is refused rather than
    trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_CHECKPOINT_VERSIONS:
        raise UnsupportedContractVersion(
            "native-process-checkpoint-v1", version, SUPPORTED_CHECKPOINT_VERSIONS
        )
    if record.get("schema_id") != CHECKPOINT_SCHEMA_ID:
        raise ValueError(
            f"native-process checkpoint schema_id {record.get('schema_id')!r} "
            f"is not {CHECKPOINT_SCHEMA_ID!r}"
        )
    in_flight = record.get("in_flight") or ()
    NativeProcessCheckpoint(
        run_id=record.get("run_id", ""),
        fencing_token=record.get("fencing_token", -1),
        journal_offset=record.get("journal_offset", 0),
        in_flight=tuple(tuple(pair) for pair in in_flight),
        route=record.get("route", ""),
        session_id=record.get("session_id", ""),
        captured_at=record.get("captured_at", ""),
        resume_decision_event=record.get("resume_decision_event"),
        harness_continuation=record.get("harness_continuation"),
        child_drain_report=record.get("child_drain_report"),
        schema_version=version,
    )


@dataclass(frozen=True)
class OwnedAttemptDispatch:
    """An ``owned-attempt-dispatch-v1`` record.

    Produced by the **native process service** for a **harness adapter**: it is
    the typed statement of one dispatched owned attempt -- which run/task/
    attempt it is, on which route/session, under which controller fencing
    token, carrying which verified process identity.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the attempt is 1-based and the identity fields are present;
    * ``fencing_token`` is a non-negative integer;
    * a dispatched record that carries a process identity must carry the
      *verified* identity facts (pid + start_time), never a bare PID.
    """

    run_id: str
    task_id: str
    attempt: int
    fencing_token: int
    route: str = ""
    session_id: str = ""
    process_identity: Optional[dict] = None
    dispatched_at: str = field(default_factory=_now_iso)
    schema_version: str = DISPATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_DISPATCH_VERSIONS:
            raise UnsupportedContractVersion(
                "owned-attempt-dispatch-v1", self.schema_version,
                SUPPORTED_DISPATCH_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("owned-attempt dispatch must name its run_id")
        if not self.task_id:
            raise ValueError("owned-attempt dispatch must name its task_id")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise ValueError("owned-attempt dispatch attempt must be an integer")
        if self.attempt < 1:
            raise ValueError(
                f"owned-attempt dispatch attempt {self.attempt!r} must be >= 1 "
                f"(the attempt number is 1-based)"
            )
        if isinstance(self.fencing_token, bool) or not isinstance(self.fencing_token, int):
            raise ValueError(
                "owned-attempt dispatch must carry an integer fencing_token"
            )
        if self.fencing_token < 0:
            raise ValueError("owned-attempt dispatch fencing_token must be >= 0")
        # A dispatched record that claims a process identity must carry the
        # *verified* identity, never a bare PID (a PID alone is not identity).
        if self.process_identity is not None:
            if not isinstance(self.process_identity, Mapping):
                raise ValueError(
                    "owned-attempt dispatch process_identity must be a mapping"
                )
            if process_identity.ProcessIdentity.from_dict(self.process_identity) is None:
                raise ValueError(
                    "owned-attempt dispatch process_identity must carry the "
                    "verified facts (pid + start_time), not a bare PID"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": DISPATCH_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "fencing_token": self.fencing_token,
            "route": self.route,
            "session_id": self.session_id,
            "process_identity": self.process_identity,
            "dispatched_at": self.dispatched_at,
        }


def validate_owned_attempt_dispatch(record: Mapping[str, Any]) -> None:
    """Refuse a dispatch record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id); reconstructs the frozen record so a tampered dispatch
    (a bare PID posing as identity, an attempt of 0) is refused.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_DISPATCH_VERSIONS:
        raise UnsupportedContractVersion(
            "owned-attempt-dispatch-v1", version, SUPPORTED_DISPATCH_VERSIONS
        )
    if record.get("schema_id") != DISPATCH_SCHEMA_ID:
        raise ValueError(
            f"owned-attempt dispatch schema_id {record.get('schema_id')!r} is "
            f"not {DISPATCH_SCHEMA_ID!r}"
        )
    OwnedAttemptDispatch(
        run_id=record.get("run_id", ""),
        task_id=record.get("task_id", ""),
        attempt=record.get("attempt", 0),
        fencing_token=record.get("fencing_token", -1),
        route=record.get("route", ""),
        session_id=record.get("session_id", ""),
        process_identity=record.get("process_identity"),
        dispatched_at=record.get("dispatched_at", ""),
        schema_version=version,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 1: the controlled loop
# ═══════════════════════════════════════════════════════════════════════════
#
# The native process service drives the whole loop.  The steps are declared in
# order so the loop is data, not implicit control flow in a live process: a
# recovering controller reads the stored intents and knows exactly where the
# process is.  No step is an external bridge.

STEP_READY_DISPATCH = "ready_dispatch"
STEP_INDEPENDENT_REVIEW = "independent_review"
STEP_REPAIR = "repair"
STEP_RE_REVIEW = "re_review"
STEP_CONTROLLED_INTEGRATION = "controlled_integration"
STEP_DEPENDENT_WAVE = "dependent_wave"

#: The ordered loop one native process service drives (criterion 1).  Every
#: step is carried by a durable intent; the order is the declaration of what
#: "beyond the interactive controller" means here.
STEPS: tuple[str, ...] = (
    STEP_READY_DISPATCH,
    STEP_INDEPENDENT_REVIEW,
    STEP_REPAIR,
    STEP_RE_REVIEW,
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
)

# Intent lifecycle states.  ``LAUNCHED``, ``REVIEWING``, ``REPAIRING`` and
# ``INTEGRATING`` are the *claiming* states: an attempt sitting in one of them
# is in flight, so a second intent for the same execution scope would be a
# duplicate launch and is refused.
INTENT_LAUNCHED = "launched"
INTENT_REVIEWING = "reviewing"
INTENT_REPAIRING = "repairing"
INTENT_REVIEWED = "reviewed"
INTENT_INTEGRATING = "integrating"
INTENT_INTEGRATED = "integrated"
INTENT_FAILED = "failed"

INTENT_STATES: frozenset[str] = frozenset({
    INTENT_LAUNCHED,
    INTENT_REVIEWING,
    INTENT_REPAIRING,
    INTENT_REVIEWED,
    INTENT_INTEGRATING,
    INTENT_INTEGRATED,
    INTENT_FAILED,
})

INTENT_CLAIMING_STATES: frozenset[str] = frozenset({
    INTENT_LAUNCHED,
    INTENT_REVIEWING,
    INTENT_REPAIRING,
    INTENT_INTEGRATING,
})

#: Intent states that count as *real useful progress* (criterion 3).  A worker
#: that launched and has made no progress is not progress; only a reviewed,
#: integrated or explicitly-failed (resolved) attempt counts.
INTENT_TERMINAL: frozenset[str] = frozenset({
    INTENT_REVIEWED,
    INTENT_INTEGRATED,
    INTENT_FAILED,
})

# Resume decisions a recovering controller publishes in its checkpoint
# (SW-1511-RECOVERY criterion 1).  A resumed controller *adopts* exactly the
# one in-flight attempt it can still verify as owned -- it never re-dispatches
# the predecessor's pending intent, because dispatch is refused for an already
# claiming scope.  When it cannot adopt (no verifiable owned attempt), it
# *refuses* by name rather than replaying a launch intent.
RESUME_ADOPTED = "adopted"
RESUME_REFUSED_NO_OWNED_ATTEMPT = "refused_no_owned_attempt"
_RESUME_DECISIONS: frozenset[str] = frozenset({
    RESUME_ADOPTED,
    RESUME_REFUSED_NO_OWNED_ATTEMPT,
})

#: The named holds a *restart* publishes when it cannot adopt the ordering-first
#: in-flight intent (SW-SCOPE-010 S10-B criterion 2).  Each names the exact
#: durable statement that failed, so a restart is never an anonymous refusal and
#: never a silent replay of the predecessor's launch intent.  The identity holds
#: are keyed by the declared :mod:`skillweave.runtime.process_identity` reason.
RESTART_HOLD_CHECKPOINT_UNWRITTEN = "checkpoint_not_written"
RESTART_HOLD_ORDERING_FIRST_MISMATCH = "checkpoint_ordering_first_mismatch"
RESTART_HOLD_NO_RECORDED_IDENTITY = "checkpoint_no_recorded_identity"
RESTART_HOLD_IDENTITY_RECYCLED = "checkpoint_identity_recycled"
RESTART_HOLD_IDENTITY_STALE_COMMAND = "checkpoint_identity_stale_command"
RESTART_HOLD_IDENTITY_FOREIGN = "checkpoint_identity_foreign"
RESTART_HOLD_IDENTITY_NOT_ALIVE = "checkpoint_identity_not_alive"
RESTART_HOLD_IDENTITY_UNVERIFIED = "checkpoint_identity_unverified"
RESTART_HOLD_BY_REASON: Mapping[str, str] = {
    "no_recorded_identity": RESTART_HOLD_NO_RECORDED_IDENTITY,
    "recycled_pid": RESTART_HOLD_IDENTITY_RECYCLED,
    "stale_command": RESTART_HOLD_IDENTITY_STALE_COMMAND,
    "foreign_worker": RESTART_HOLD_IDENTITY_FOREIGN,
    "not_alive": RESTART_HOLD_IDENTITY_NOT_ALIVE,
}

#: The only review verdicts the loop accepts.  A verdict is the durable review
#: record's outcome (``pass``/``fail``); anything else is refused rather than
#: coerced, so an unknown verdict can never be mistaken for a pass.
REVIEW_PASS = "pass"
REVIEW_FAIL = "fail"
REVIEW_VERDICTS: frozenset[str] = frozenset({REVIEW_PASS, REVIEW_FAIL})


def _review_target_state(verdict: str, *, failing: str = INTENT_REPAIRING) -> str:
    """Map a recorded review *verdict* to the intent state it advances to.

    A ``pass`` always advances to :data:`INTENT_REVIEWED` (ready to integrate).
    A ``fail`` advances to *failing* -- :data:`INTENT_REPAIRING` for a first
    review (a repair round must follow) or :data:`INTENT_FAILED` for a
    re-review (the attempt stops, terminal, and can never integrate).  Any
    value outside :data:`REVIEW_VERDICTS` is refused: the loop is driven by the
    recorded verdict, so an unrecognised value must never be treated as a pass.
    """
    normalized = verdict.strip().lower() if isinstance(verdict, str) else verdict
    if normalized == REVIEW_PASS:
        return INTENT_REVIEWED
    if normalized == REVIEW_FAIL:
        return failing
    raise ValueError(
        f"{verdict!r} is not a review verdict; expected one of "
        f"{sorted(REVIEW_VERDICTS)}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: the single controller lease with fencing
# ═══════════════════════════════════════════════════════════════════════════

class LeaseRefusedError(Exception):
    """A controller lease was refused (another live controller holds it).

    A live lease is never silently stolen: the refusing caller must wait for
    the holder's lease to expire or be released, then take over and receive a
    strictly higher fencing token.
    """

    def __init__(self, holder: str, lease_until: Optional[str]):
        self.holder = holder
        self.lease_until = lease_until
        super().__init__(
            f"controller lease is held by {holder!r} until {lease_until!r}; "
            f"a live lease is never silently stolen"
        )


class StaleFencingTokenError(Exception):
    """A write presented a fencing token that has been superseded.

    The durable effect of a stale controller is refused: its token is lower
    than the token currently held, so it is no longer the controller.
    """

    def __init__(self, presented: int, current: int):
        self.presented = presented
        self.current = current
        super().__init__(
            f"stale controller fencing token {presented}: the current token is "
            f"{current}; a superseded controller's write is refused"
        )


class LeaseNotHeldError(Exception):
    """A write presented a token for a lease that is not currently held."""

    def __init__(self, presented: int, holder: Optional[str]):
        self.presented = presented
        self.holder = holder
        super().__init__(
            f"fencing token {presented} does not correspond to a held lease "
            f"(current holder: {holder!r})"
        )


class DuplicateDispatchError(Exception):
    """A second *claiming* intent was offered for an execution scope.

    A scope already carries a live owned attempt, so a second launch would be
    a duplicate worker.  The duplicate is refused; the caller must observe the
    existing intent instead.
    """

    def __init__(self, execution_scope: str, intent_id: str, run_id: str):
        self.execution_scope = execution_scope
        self.intent_id = intent_id
        self.run_id = run_id
        super().__init__(
            f"execution scope {execution_scope!r} is already claimed by intent "
            f"{intent_id!r} (run {run_id!r}); a duplicate launch is refused"
        )


class IntentStateConflictError(Exception):
    """A guarded intent advance was refused: the intent was not in the expected state.

    The state-driven loop advances an intent only from the exact state the
    previous step left it in (``advance_dispatch_intent_from``).  An intent that
    is missing, already advanced, or sitting in a state inconsistent with the
    step about to run is refused rather than silently overwritten -- so an
    out-of-order or verdict-inconsistent advance cannot corrupt the loop.
    """

    def __init__(self, intent_id: str, expected_state: str, actual_state: Optional[str]):
        self.intent_id = intent_id
        self.expected_state = expected_state
        self.actual_state = actual_state
        super().__init__(
            f"dispatch intent {intent_id!r} is not in expected state "
            f"{expected_state!r} (actual: {actual_state!r}); the guarded "
            f"advance is refused"
        )


#: The single lease lane.  There is exactly one controller lease per run store;
#: the key is stated (not implied) so the invariant "one controller" is data.
LEASE_KEY = "controller"


@dataclass(frozen=True)
class ControllerLease:
    """The single controller lease and its fencing token.

    Exactly one controller holds the lease at a time.  The ``fencing_token``
    strictly increases on every acquisition, so a recovering controller that
    takes over reads a higher token than the controller it replaced; any write
    presented with an older token is refused (:class:`StaleFencingTokenError`).
    This is what prevents *stale-controller acceptance*.
    """

    holder: str
    fencing_token: int
    lease_until: Optional[str] = None
    acquired_at: str = ""
    released_at: Optional[str] = None

    @property
    def held(self) -> bool:
        return self.released_at is None

    def is_expired(self, now: Optional[str] = None) -> bool:
        """A held lease is expired when its deadline is in the past."""
        if self.released_at is not None or self.lease_until is None:
            return False
        reference = now or _now_iso()
        until = _parse_iso(self.lease_until)
        current = _parse_iso(reference)
        if until is None or current is None:
            # An undatable deadline is treated as expired (fail closed): the
            # caller cannot prove the lease is live, so it is not.
            return True
        return current >= until

    def to_dict(self) -> dict[str, Any]:
        return {
            "holder": self.holder,
            "fencing_token": self.fencing_token,
            "lease_until": self.lease_until,
            "acquired_at": self.acquired_at,
            "released_at": self.released_at,
            "held": self.held,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: durable dispatch intents
# ═══════════════════════════════════════════════════════════════════════════
#
# One row per owned attempt.  The exit from a *claiming* state (launched ->
# reviewing -> repairing -> ... ) releases the execution scope, so the scope
# is claimed for as long as an owned attempt is live and no longer.  A second
# claiming intent for the same scope is refused (:class:`DuplicateDispatchError`
# at the service, a partial unique index at the store), so a duplicate launch
# cannot start a second worker.

@dataclass(frozen=True)
class DispatchIntent:
    """A durable record of one owned attempt's dispatch lifecycle.

    ``execution_scope`` is the modelled key the duplicate-launch guard is
    anchored to (the caller's stable "which work is this" identity, e.g.
    ``run_id/task_id/attempt``).  ``state`` is one of :data:`INTENT_STATES`;
    the intent *claims* its scope only while it sits in a
    :data:`INTENT_CLAIMING_STATES` state.  ``fencing_token`` records which
    controller held the lease when the attempt was dispatched, so a recovered
    intent is not silently re-owned by a stale controller.
    """

    intent_id: str
    run_id: str
    task_id: str
    attempt: int
    execution_scope: str
    step: str
    state: str
    fencing_token: int
    route: str = ""
    session_id: str = ""
    process_identity: Optional[dict] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @property
    def claiming(self) -> bool:
        return self.state in INTENT_CLAIMING_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "execution_scope": self.execution_scope,
            "step": self.step,
            "state": self.state,
            "fencing_token": self.fencing_token,
            "route": self.route,
            "session_id": self.session_id,
            "process_identity": self.process_identity,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: dynamic readiness refill
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ReadinessPlan:
    """The readiness decision for one refill of ready lanes (criterion 3).

    ``admitted`` are the ready task ids admitted against actual free capacity
    this refill; ``refused`` maps a ready task id to the dimension that
    refused it (reusing the ``dagscheduler`` ``REFUSAL_*`` codes).  A task
    held back by an unmet dependency is not present in ``admitted`` (dependency
    excitation is honoured by :func:`plan_readiness`), and a lane whose write
    scope overlaps an already-admitted lane is admitted *inline* (it cannot fan
    out) -- both exclusions are the *dependency/write-surface exclusion* the
    criterion names.

    ``capacity_available`` is the measured maximum parallelism for this refill
    (``None`` when no fresh observation was supplied, i.e. an unconditional
    refill).  ``worker_count`` is how many lanes were admitted (workers
    started), ``useful_progress`` is how many already-completed attempts that
    run has produced -- deliberately *not* a function of ``worker_count``, so a
    refill that starts workers but makes no progress is distinguishable from
    one that does.
    """

    admitted: tuple[str, ...]
    refused: Mapping[str, str]
    capacity_available: Optional[int]
    worker_count: int
    useful_progress: int
    inline: tuple[str, ...] = ()
    observed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": list(self.admitted),
            "refused": dict(self.refused),
            "capacity_available": self.capacity_available,
            "worker_count": self.worker_count,
            "useful_progress": self.useful_progress,
            "inline": list(self.inline),
            "observed_at": self.observed_at,
        }


def plan_readiness(
    tasks: Sequence[Task],
    *,
    done: Sequence[str],
    route_capacity: Optional[RouteCapacitySnapshot] = None,
    reservations: Optional[ReservationSnapshot] = None,
    held_claims: Sequence[WriteScopeClaim] = (),
    now: Optional[str] = None,
) -> ReadinessPlan:
    """Refill readiness from *actual* free capacity (SW-ORCH-001 criterion 3).

    Engages exactly when fresh capacity observations are supplied --
    ``route_capacity`` and/or ``reservations``.  When neither is given this
    returns an empty plan with ``capacity_available=None``: refill is opt-in,
    so a caller that does not supply observations is never silently given
    invented capacity.

    The refill is subject to:

    * **dependency exclusion** -- a task is *ready* only when every task it
      depends on is in ``done``; a not-ready task is never admitted (it is a
      dependency refusal, not a capacity one, and is reported under
      ``REFUSAL_DEPENDENCY``);
    * **write-surface exclusion** -- two ready lanes whose write scopes overlap
      are never fanned out together (``dagscheduler.build_lanes``); the
      overlapping lane is admitted *inline* and reported in ``inline``;
    * **actual free capacity** -- the number of admitted fan-out lanes is
      capped at the *measured* maximum parallelism
      (``dagscheduler.measure_max_parallel`` over the fresh observations), not
      a fixed literal.  A measured capacity of 0 admits no lane and reports
      every ready lane as refused on the parallelism dimension.
    """
    reference = now or _now_iso()
    capacity: Optional[int] = None
    if route_capacity is not None or reservations is not None:
        capacity = measure_max_parallel(route_capacity, reservations, now=reference)

    done_set = set(done)
    refused: dict[str, str] = {}

    ready: list[Task] = []
    for task in tasks:
        if task.id in done_set:
            # Already-completed work is never re-admitted: a completed task is
            # progress that has happened, not a worker to start again.
            continue
        unmet = [d for d in task.depends_on if d not in done_set]
        if unmet:
            refused[task.id] = f"dependency:{','.join(sorted(unmet))}"
            continue
        ready.append(task)

    # No fresh observation -> no refill (opt-in): report readiness but admit
    # nothing, and never invent capacity.
    if capacity is None:
        return ReadinessPlan(
            admitted=(),
            refused=refused,
            capacity_available=None,
            worker_count=0,
            useful_progress=len(done_set),
            inline=(),
            observed_at=reference,
        )

    if capacity < 1:
        for task in ready:
            refused[task.id] = "measured-parallelism"
        return ReadinessPlan(
            admitted=(),
            refused=refused,
            capacity_available=capacity,
            worker_count=0,
            useful_progress=len(done_set),
            inline=(),
            observed_at=reference,
        )

    # Write-surface exclusion.  A task is *ready* only when every dependency is
    # already done, so the ready set is a mutually-independent frontier; its
    # dependencies are satisfied and no longer part of the graph this refill
    # arbitrates.  Strip them before asking ``build_lanes`` to group the
    # frontier by write scope (otherwise a satisfied dependency would be an
    # unknown node in the lane graph).
    frontier = [
        Task(
            id=t.id, depends_on=[], write_scope=list(t.write_scope),
            gate=t.gate, route=t.route,
        )
        for t in ready
    ]
    lane_groups = build_lanes(frontier, held_claims=tuple(held_claims))
    ordered_lanes = [lane for group in lane_groups for lane in group]

    admitted_ids: list[str] = []
    inline_ids: list[str] = []
    fanout_count = 0
    for lane in ordered_lanes:
        tid = lane.task_id
        if lane.execution_mode == "inline":
            # A lane whose scope overlaps another ready lane or a held claim
            # may still run, but inline: it cannot fan out on top of an owned
            # scope, and it is counted against nothing (it runs alone).
            if len(admitted_ids) >= capacity:
                refused[tid] = "measured-parallelism"
                continue
            admitted_ids.append(tid)
            inline_ids.append(tid)
            continue
        if fanout_count >= capacity:
            refused[tid] = "measured-parallelism"
            continue
        fanout_count += 1
        admitted_ids.append(tid)

    return ReadinessPlan(
        admitted=tuple(sorted(admitted_ids)),
        refused=refused,
        capacity_available=capacity,
        worker_count=len(admitted_ids),
        useful_progress=len(done_set),
        inline=tuple(sorted(inline_ids)),
        observed_at=reference,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 4: the compact changed-state/evidence view
# ═══════════════════════════════════════════════════════════════════════════
#
# ``evidence-view-v1`` is the compact view (the *evidence delta*) that replaces
# repeated full transcript reads.  It is produced from the durable journal and
# the evidence store, never from a live process's memory, and it is *cursor
# scoped*: everything the view reports is bound to journal sequences strictly
# greater than the caller's ``journal_cursor``.  A poll of an unchanged process
# therefore returns an empty delta -- "no full transcript is needed for an
# unchanged poll" is the contract, not an optimization.

EVIDENCE_VIEW_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/evidence-view/v1"
)
EVIDENCE_VIEW_SCHEMA_VERSION = "1.0.0"
SUPPORTED_EVIDENCE_VIEW_VERSIONS = frozenset({EVIDENCE_VIEW_SCHEMA_VERSION})

#: The gate states a *machine* status may carry.  Only ``accepted`` gates are
#: authoritative acceptances; ``required`` gates are still demanded, ``pending``
#: gates are neither, and ``failed`` gates are an explicit non-acceptance.  A
#: process's clean exit is never one of these -- a status can report a gate, but
#: it can never convert a process success into a gate PASS.
GATE_ACCEPTED = "accepted"
GATE_REQUIRED = "required"
GATE_PENDING = "pending"
GATE_FAILED = "failed"

ACCEPTED_GATE_STATUSES: frozenset[str] = frozenset({
    GATE_ACCEPTED,
    GATE_REQUIRED,
    GATE_PENDING,
    GATE_FAILED,
})

#: The two architect healths a continuation decision distinguishes.  Provider
#: quota exhaustion is a *wait*, distinct from a healthy architect: it is never
#: a repair verdict and never an escalation permission.
ARCHITECT_HEALTHY = "healthy"
ARCHITECT_QUOTA_EXHAUSTED = "quota-exhausted"


def _coerce_sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    """Normalize a compact-view collection to a tuple, refusing a bare string.

    A string is a sequence of characters, so a caller passing ``"gate-a"`` where
    a collection of gate ids is expected would silently iterate characters.
    The boundary refuses that instead of inventing a decomposition.  The check
    is by iteration rather than ``isinstance(..., Sequence)`` so a faithful
    read-only projection (one that yields its entries but is not registered as a
    nominal ``Sequence`` subclass) is accepted rather than wrongly refused.
    """
    if isinstance(value, (str, bytes, Mapping)):
        raise ValueError(
            f"{field_name} must be a sequence of entries, got {value!r}"
        )
    try:
        return tuple(value)
    except TypeError:
        raise ValueError(
            f"{field_name} must be a sequence of entries, got {value!r}"
        ) from None


def _field(record: Any, name: str, default: Any = None) -> Any:
    """Read a journal fact's field from an object *or* a mapping record.

    The durable read projection hands over plain mappings; an in-memory fact is
    an object.  Both are the same fact and the compact view must not silently
    see an empty window merely because the record arrived as a dict.
    """
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


@dataclass(frozen=True)
class EvidenceDelta:
    """An ``evidence-view-v1`` record: the compact changed-state view.

    Every field is read from durable state.  ``journal_cursor`` is the new
    cursor to an *unchanged* poll from; a poll that carries it sees nothing
    (the delta is empty).  ``accepted_gates`` and ``required_gates`` are the
    authoritative gate denominator -- the two are disjoint, so a gate can never
    be both accepted and still required.  A process's success is never folded
    into a gate: the gates are named as they are, and ``unknowns`` /
    ``required_actions`` state what remains genuinely unknown or outstanding.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the view must name its run and carry an integer cursor >= 0;
    * every gate status must be one of :data:`ACCEPTED_GATE_STATUSES`;
    * ``accepted_gates`` and ``required_gates`` must be disjoint.
    """

    run_id: str
    journal_cursor: int = 0
    gates: Mapping[str, str] = field(default_factory=dict)
    complexity_points: int = 0
    active_task_id: str = ""
    active_role: str = ""
    active_session_id: str = ""
    active_route: str = ""
    new_evidence: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    monotonically_increasing_cursor: bool = True
    monotonicity_diagnosed: bool = False
    monotonicity_notes: tuple[str, ...] = ()
    coverage_complete: bool = True
    archived: bool = False
    observed_at: str = field(default_factory=_now_iso)
    schema_version: str = EVIDENCE_VIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_EVIDENCE_VIEW_VERSIONS:
            raise UnsupportedContractVersion(
                "evidence-view-v1", self.schema_version,
                SUPPORTED_EVIDENCE_VIEW_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("evidence view must name its run_id")
        if isinstance(self.journal_cursor, bool) or not isinstance(self.journal_cursor, int):
            raise ValueError("evidence view journal_cursor must be an integer")
        if self.journal_cursor < 0:
            raise ValueError("evidence view journal_cursor must be >= 0")
        for gate_id, status in self.gates.items():
            if status not in ACCEPTED_GATE_STATUSES:
                raise ValueError(
                    f"gate {gate_id!r} carries status {status!r}, which is not "
                    f"one of {sorted(ACCEPTED_GATE_STATUSES)}"
                )
        overlap = set(self.accepted_gates) & set(self.required_gates)
        if overlap:
            raise ValueError(
                f"gate(s) {sorted(overlap)} are both accepted and still "
                f"required; the gate denominator must be unambiguous"
            )

    @property
    def accepted_gates(self) -> tuple[str, ...]:
        return tuple(
            sorted(g for g, s in self.gates.items() if s == GATE_ACCEPTED)
        )

    @property
    def required_gates(self) -> tuple[str, ...]:
        return tuple(
            sorted(g for g, s in self.gates.items() if s != GATE_ACCEPTED)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": EVIDENCE_VIEW_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "journal_cursor": self.journal_cursor,
            "gates": dict(self.gates),
            "accepted_gates": list(self.accepted_gates),
            "required_gates": list(self.required_gates),
            "complexity_points": self.complexity_points,
            "active_task_id": self.active_task_id,
            "active_role": self.active_role,
            "active_session_id": self.active_session_id,
            "active_route": self.active_route,
            "new_evidence": list(self.new_evidence),
            "unknowns": list(self.unknowns),
            "required_actions": list(self.required_actions),
            "monotonically_increasing_cursor": self.monotonically_increasing_cursor,
            "monotonicity_diagnosed": self.monotonicity_diagnosed,
            "monotonicity_notes": list(self.monotonicity_notes),
            "coverage_complete": self.coverage_complete,
            "archived": self.archived,
            "observed_at": self.observed_at,
        }


def validate_evidence_delta(record: Mapping[str, Any]) -> None:
    """Refuse an evidence-view record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered view (a gate with a process-derived
    status, a gate both accepted and required) is refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_EVIDENCE_VIEW_VERSIONS:
        raise UnsupportedContractVersion(
            "evidence-view-v1", version, SUPPORTED_EVIDENCE_VIEW_VERSIONS
        )
    if record.get("schema_id") != EVIDENCE_VIEW_SCHEMA_ID:
        raise ValueError(
            f"evidence view schema_id {record.get('schema_id')!r} is not "
            f"{EVIDENCE_VIEW_SCHEMA_ID!r}"
        )
    EvidenceDelta(
        run_id=record.get("run_id", ""),
        journal_cursor=record.get("journal_cursor", 0),
        gates=record.get("gates") or {},
        complexity_points=record.get("complexity_points", 0),
        active_task_id=record.get("active_task_id", ""),
        active_role=record.get("active_role", ""),
        active_session_id=record.get("active_session_id", ""),
        active_route=record.get("active_route", ""),
        new_evidence=tuple(record.get("new_evidence") or ()),
        unknowns=tuple(record.get("unknowns") or ()),
        required_actions=tuple(record.get("required_actions") or ()),
        monotonically_increasing_cursor=record.get(
            "monotonically_increasing_cursor", True
        ),
        monotonicity_diagnosed=record.get("monotonicity_diagnosed", False),
        monotonicity_notes=tuple(record.get("monotonicity_notes") or ()),
        coverage_complete=record.get("coverage_complete", True),
        archived=record.get("archived", False),
        observed_at=record.get("observed_at", ""),
        schema_version=version,
    )


def build_evidence_delta(
    records: object,
    run_id: str,
    *,
    journal_cursor: int,
    since_evidence_index: int = 0,
    creativity_rounds: int = 0,
    locked: bool = False,
    now: Optional[str] = None,
) -> EvidenceDelta:
    """Build the compact view over the *changed* window of durable state.

    Only journal facts with ``sequence > journal_cursor`` and evidence rows from
    ``since_evidence_index`` onward are consumed, so the returned view replaces
    repeated full transcript reads: everything it reports is new since the
    caller's cursor.  A poll with no new facts and no new evidence returns a
    delta whose ``journal_cursor`` equals the input -- the same cursor, which is
    how a caller knows nothing changed and needs to read nothing further.

    ``locked`` (the whole run is frozen) and a run that has consumed
    ``creativity_rounds >= 3`` without a decision collapse the view to a compact
    checkpoint: ``coverage_complete`` is ``False`` and ``archived`` is ``True``
    so the full history stays as an evidence reference rather than being
    repeatedly re-read.  The chosen collapse is reported in
    ``monotonicity_notes`` as ``collapse:locked`` / ``collapse:creativity``.

    ``monotonically_increasing_cursor`` is ``False`` only when the cursor
    regressed; a non-monotonic cursor is not silently followed -- it is
    *diagnosed*: the delta still reports the new cursor (so it stays usable), but
    ``coverage_complete`` is ``False``, ``monotonicity_diagnosed`` is ``True``
    and a ``diagnosed:non-monotonic-cursor`` note is attached.  A healthy,
    monotone poll remains complete.
    """
    # The record *source* is named before it is normalized to a tuple:
    # ``_coerce_sequence`` returns a plain tuple, so reading the evidence
    # source off the normalized value would hide it and make every poll look
    # evidence-free.  The source exposes either ``list_evidence`` or a bound
    # ``store.list_evidence``; both are read-only projections of durable state.
    source = records
    records = _coerce_sequence(records, "records")
    if isinstance(since_evidence_index, bool) or not isinstance(since_evidence_index, int):
        raise ValueError("since_evidence_index must be an integer")
    if isinstance(journal_cursor, bool) or not isinstance(journal_cursor, int):
        raise ValueError("journal_cursor must be an integer")
    if journal_cursor < 0 or since_evidence_index < 0:
        raise ValueError("journal_cursor and since_evidence_index must be >= 0")

    reference = now or _now_iso()

    def _at(seq: int) -> str:
        for record in reversed(records):
            if _field(record, "sequence") == seq:
                return _field(record, "timestamp", "") or reference
        return reference

    # -- gates and active identity from the newest fact seen before the cursor
    # (so the view names the authoritative denominator even on an empty poll).
    gates: dict[str, str] = {}
    active_task_id = ""
    active_role = ""
    active_session_id = ""
    active_route = ""
    for record in records:
        if (_field(record, "sequence", 0) or 0) > journal_cursor:
            continue
        payload = _field(record, "payload") or {}
        if not isinstance(payload, Mapping):
            continue
        raw_gates = payload.get("gates")
        if isinstance(raw_gates, Mapping):
            gates = {str(g): str(s) for g, s in raw_gates.items()}
        for key, setter in (
            ("task_id", "active_task_id"),
            ("role", "active_role"),
            ("session_id", "active_session_id"),
            ("route", "active_route"),
        ):
            value = payload.get(key)
            if isinstance(value, str) and value:
                if setter == "active_task_id":
                    active_task_id = value
                elif setter == "active_role":
                    active_role = value
                elif setter == "active_session_id":
                    active_session_id = value
                else:
                    active_route = value

    # -- the changed window (strictly after the cursor).
    window = sorted(
        (r for r in records if (_field(r, "sequence", 0) or 0) > journal_cursor),
        key=lambda r: _field(r, "sequence", 0) or 0,
    )

    new_cursor = journal_cursor
    regressed = False
    observed_cursor_seen = False
    for record in window:
        sequence = int(_field(record, "sequence", 0) or 0)
        payload = _field(record, "payload")
        if isinstance(payload, Mapping):
            observed = payload.get("observed_cursor")
            if isinstance(observed, int) and not isinstance(observed, bool):
                observed_cursor_seen = True
                if observed < journal_cursor:
                    regressed = True
                new_cursor = max(new_cursor, observed)
        if not observed_cursor_seen:
            new_cursor = max(new_cursor, sequence)

    monotonically_increasing = not regressed

    # -- new evidence references (from the evidence store, cursor-scoped).
    new_evidence: list[str] = []
    coverage_complete = True
    horizon = _at(journal_cursor)
    # The evidence source is named by the record source *before* it is
    # normalized to a tuple: ``_coerce_sequence`` returns a plain tuple, which
    # would hide the source and make every poll look evidence-free.  A source
    # is read through the ``list_evidence`` contract (a callable) or, failing
    # that, through a bound ``store.list_evidence``.  With no source at all the
    # view cannot prove the receipts, so completeness is only honest when the
    # caller has not asked to skip any evidence.
    lister = getattr(source, "list_evidence", None)
    store = getattr(source, "store", None)
    rows: Optional[list[Any]] = None
    if callable(lister):
        try:
            rows = list(lister())
        except TypeError:
            rows = None  # a source needing an argument is read from the store
    if rows is None and store is not None:
        store_lister = getattr(store, "list_evidence", None)
        if callable(store_lister):
            try:
                rows = list(store_lister(run_id))
            except TypeError:
                rows = list(store_lister())
    if rows is not None:
        new_evidence = [
            _field(row, "artifact_id") for row in rows[since_evidence_index:]
            if (_field(row, "created_at", "") or "") >= horizon
        ]
    elif since_evidence_index > 0:
        # A caller asked to skip evidence, but no evidence source is bound to
        # this view: the compact view cannot prove the denominator, so it
        # refuses to claim completeness rather than inventing one.
        coverage_complete = False

    notes: list[str] = []
    if regressed:
        notes.append("diagnosed:non-monotonic-cursor")
        coverage_complete = False

    # -- collapse to a compact checkpoint when the view can no longer be whole.
    archived = False
    if locked or creativity_rounds >= 3:
        archived = True
        coverage_complete = False
        notes.append("collapse:locked" if locked else "collapse:creativity")

    return EvidenceDelta(
        run_id=run_id,
        journal_cursor=new_cursor,
        gates=gates,
        complexity_points=int(creativity_rounds) * 3,
        active_task_id=active_task_id,
        active_role=active_role,
        active_session_id=active_session_id,
        active_route=active_route,
        new_evidence=tuple(new_evidence),
        unknowns=(),
        required_actions=(),
        monotonically_increasing_cursor=monotonically_increasing,
        monotonicity_diagnosed=regressed,
        monotonicity_notes=tuple(notes),
        coverage_complete=coverage_complete,
        archived=archived,
        observed_at=reference,
    )


@dataclass(frozen=True)
class ContinuationDecision:
    """What the controller does when the interactive architect changes health.

    ``continue_execution`` and ``gates_continue`` are the heart of criterion 4:
    execution and the *original* gates keep running even when the architect
    exhausts provider quota -- an exhausted architect pauses only the
    architect's own interaction, and resumes the same session on the same route
    (``resume_same_route``).  Quota is a wait, never a repair verdict or an
    escalation permission, which is why ``escalate`` is refused on the
    quota-exhausted branch.

    Fail-closed invariants enforced at construction:

    * ``architect_health`` is one of :data:`ARCHITECT_HEALTHY` /
      :data:`ARCHITECT_QUOTA_EXHAUSTED`;
    * a quota-exhausted decision must resume the same route and must not
      escalate;
    * a held architect never interrupts the delegated workers
      (``interrupt_delegated_workers`` is ``False``).
    """

    architect_health: str
    continue_execution: bool = True
    gates_continue: bool = True
    resume_same_route: bool = False
    interrupt_delegated_workers: bool = False
    escalate: bool = False
    wait: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.architect_health not in (ARCHITECT_HEALTHY, ARCHITECT_QUOTA_EXHAUSTED):
            raise ValueError(
                f"architect_health {self.architect_health!r} is not one of "
                f"{[ARCHITECT_HEALTHY, ARCHITECT_QUOTA_EXHAUSTED]}"
            )
        if self.architect_health == ARCHITECT_QUOTA_EXHAUSTED:
            if not self.resume_same_route:
                raise ValueError(
                    "a quota-exhausted architect must resume the same session "
                    "on the same route (quota is a wait, not a reroute)"
                )
            if self.escalate:
                raise ValueError(
                    "provider quota is never a repair verdict or escalation "
                    "permission"
                )
        if self.interrupt_delegated_workers:
            raise ValueError(
                "a stopped or quota-exhausted architect must not interrupt its "
                "delegated workers"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "architect_health": self.architect_health,
            "continue_execution": self.continue_execution,
            "gates_continue": self.gates_continue,
            "resume_same_route": self.resume_same_route,
            "interrupt_delegated_workers": self.interrupt_delegated_workers,
            "escalate": self.escalate,
            "wait": self.wait,
            "reason": self.reason,
        }


def plan_continuation(
    architect_health: str,
    *,
    prior_session_id: str = "",
    prior_route: str = "",
) -> ContinuationDecision:
    """Plan continuation and original-gate progress from architect health.

    A **healthy** architect keeps everything running with no wait; a
    **quota-exhausted** architect still keeps execution and the original gates
    running -- it simply waits and resumes the same session on the same route.
    Both branches preserve the architect's delegated workers: a stopped
    interactive controller does not interrupt its workers.

    ``prior_session_id`` / ``prior_route`` are carried into the decision's
    ``reason`` so the resumed session is bound to the exact identity that was
    observed, not to a fresh guess.
    """
    if architect_health == ARCHITECT_HEALTHY:
        return ContinuationDecision(
            architect_health=ARCHITECT_HEALTHY,
            continue_execution=True,
            gates_continue=True,
            resume_same_route=False,
            interrupt_delegated_workers=False,
            escalate=False,
            wait=False,
            reason="architect healthy: execution and gates continue, no wait",
        )
    if architect_health == ARCHITECT_QUOTA_EXHAUSTED:
        bound = (
            f"session {prior_session_id!r} on route {prior_route!r}"
            if prior_session_id or prior_route else "the observed session/route"
        )
        return ContinuationDecision(
            architect_health=ARCHITECT_QUOTA_EXHAUSTED,
            continue_execution=True,
            gates_continue=True,
            resume_same_route=True,
            interrupt_delegated_workers=False,
            escalate=False,
            wait=True,
            reason=(
                f"architect quota exhausted: execution and original gates "
                f"continue; wait and resume {bound} on the same route"
            ),
        )
    raise ValueError(
        f"architect_health {architect_health!r} is not one of "
        f"{[ARCHITECT_HEALTHY, ARCHITECT_QUOTA_EXHAUSTED]}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: harness-continuation-v1 (attested session resume)
# ═══════════════════════════════════════════════════════════════════════════
#
# A recovered controller wants to continue an owned attempt.  The *only* way it
# may resume the predecessor's actual provider session is when the harness
# adapter attests the capability to do so.  Otherwise it continues from the
# durable checkpoint and reports the limitation *by name* -- it never carries
# the predecessor's session UUID forward as if the session were still live.
#
# The decision is a pure function of the *attested* capability: the caller (a
# recovery seam that holds the adapter) passes whether the adapter attested the
# capability and the predecessor's recorded session identity.  A decision that
# claims a session resume without the attestation is refused at construction,
# so a fabricated UUID can never masquerade as continuity.

@dataclass(frozen=True)
class HarnessContinuation:
    """A ``harness-continuation-v1`` record: how a recovered controller continues.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported (an unknown version is refused, never
      approximated);
    * ``mode`` is one of :data:`CONTINUATION_SESSION_RESUME` /
      :data:`CONTINUATION_CHECKPOINT`;
    * a **session resume** is admissible only when the adapter attested the
      session-resume capability *and* a prior session identity is present --
      otherwise it is refused (this is the "never fabricated UUID continuity"
      invariant);
    * a **checkpoint continuation** carries a named ``limitation`` and never a
      ``session_id``: the durable checkpoint is the continuation, so no session
      UUID is carried forward.
    """

    run_id: str
    mode: str
    resume_capability_attested: bool = False
    session_id: str = ""
    intent_id: str = ""
    limitation: str = ""
    reason: str = ""
    schema_version: str = HARNESS_CONTINUATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_HARNESS_CONTINUATION_VERSIONS:
            raise UnsupportedContractVersion(
                "harness-continuation-v1", self.schema_version,
                SUPPORTED_HARNESS_CONTINUATION_VERSIONS,
            )
        if not self.run_id:
            raise ValueError(
                "a harness continuation must name its run_id"
            )
        if self.mode not in _CONTINUATION_MODES:
            raise ValueError(
                f"harness continuation mode {self.mode!r} is not one of "
                f"{sorted(_CONTINUATION_MODES)}"
            )
        if self.mode == CONTINUATION_SESSION_RESUME:
            if not self.resume_capability_attested:
                raise ValueError(
                    "a session resume requires an attested "
                    f"{HARNESS_SESSION_RESUME_CAPABILITY!r} capability; "
                    "refusing to fabricate session-UUID continuity"
                )
            if not self.session_id:
                raise ValueError(
                    "a session resume must name the prior session it resumes"
                )
        else:  # CONTINUATION_CHECKPOINT
            if not self.limitation:
                raise ValueError(
                    "a checkpoint continuation must name the limitation it "
                    "reports"
                )
            if self.session_id:
                raise ValueError(
                    "a checkpoint continuation must not carry a session id "
                    "forward (that would be fabricated UUID continuity)"
                )

    @property
    def resumes_session(self) -> bool:
        """True only for a genuine, capability-attested session resume."""
        return self.mode == CONTINUATION_SESSION_RESUME

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": HARNESS_CONTINUATION_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "mode": self.mode,
            "resume_capability_attested": bool(self.resume_capability_attested),
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "limitation": self.limitation,
            "reason": self.reason,
        }


def plan_harness_continuation(
    run_id: str,
    *,
    resume_capability_attested: bool,
    prior_session_id: str = "",
    intent_id: str = "",
) -> HarnessContinuation:
    """Decide how a recovered controller continues an owned attempt (criterion 2).

    Actual session resume is used *only* for an attested harness capability:
    when ``resume_capability_attested`` is true and a prior session identity is
    known, the continuation mode is :data:`CONTINUATION_SESSION_RESUME`.
    Otherwise -- the adapter does not attest the capability, or no prior session
    is known -- the mode is :data:`CONTINUATION_CHECKPOINT` and the record names
    the *limitation*, carrying no session id.  A caller cannot get a session
    resume without the attestation: the refusal is enforced in
    :class:`HarnessContinuation`'s constructor.
    """
    if resume_capability_attested and prior_session_id:
        return HarnessContinuation(
            run_id=run_id,
            mode=CONTINUATION_SESSION_RESUME,
            resume_capability_attested=True,
            session_id=prior_session_id,
            intent_id=intent_id,
            reason=(
                f"adapter attested {HARNESS_SESSION_RESUME_CAPABILITY!r}: "
                f"resume session {prior_session_id!r}"
            ),
        )
    if not resume_capability_attested:
        limitation = LIMITATION_UNSUPPORTED_RESUME
        reason = (
            f"adapter did not attest {HARNESS_SESSION_RESUME_CAPABILITY!r}: "
            "continue from the durable checkpoint, not the prior session"
        )
    else:
        limitation = LIMITATION_SESSION_UNKNOWN
        reason = (
            "adapter attests session resume but no prior session identity was "
            "recorded: continue from the durable checkpoint"
        )
    return HarnessContinuation(
        run_id=run_id,
        mode=CONTINUATION_CHECKPOINT,
        resume_capability_attested=bool(resume_capability_attested),
        session_id="",  # a checkpoint continuation never carries a session id
        intent_id=intent_id,
        limitation=limitation,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: owned-child drain + useful-slot occupancy
# ═══════════════════════════════════════════════════════════════════════════
#
# A recovered controller resumes the owned attempt -- and the attempt may have
# left **background probe children** behind: the concurrency/search probes it
# launched, each with its own recorded process identity.  Two distinct facts
# must not be conflated:
#
#   * a child that is **still the owned attempt's process** (its recorded
#     identity re-verifies as a ``match``) is drained -- ended by *owned
#     identity*, so a recycled PID, a stale command or a foreign worker is
#     reported as a refusal, never signalled;
#   * a child that emits **heartbeat traffic but no useful progress** is a
#     *stall*, not a worker.  It must not be counted as occupying a useful
#     slot, and it must not indefinitely suppress a *completed* review.
#
# The drain report is a pure function of the caller's verification of each
# child's recorded identity against the OS: the caller (the service that holds
# the lease) passes the observed facts, and the planner classifies each child
# distinctly.  A child whose identity cannot be verified is never reported as
# drained.

@dataclass(frozen=True)
class _OwnedChildFact:
    """One background child's recorded identity and observed drain verification.

    ``verification`` is the :class:`~skillweave.runtime.process_identity.
    IdentityVerification` the caller obtained by re-observing the recorded
    identity against the live OS -- the planner never observes the OS itself.
    """

    child_id: str
    scope: str
    recorded_identity: Optional[dict]
    verification: Any
    kind: str = CHILD_PROBE
    progressing: bool = False
    heartbeat_only: bool = False


@dataclass(frozen=True)
class OwnedChildDrain:
    """An ``owned-child-drain-v1`` report over one run's background children.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported (an unknown version is refused);
    * every child outcome is a declared one (:data:`CHILD_DRAINED` or
      :data:`CHILD_DRAIN_REFUSED`);
    * a **drained** child carries ``verified`` true -- a drain without a
      re-verified owned identity would be a signal to an un-owned process;
    * a **heartbeat-only** child is exempted from useful-slot occupancy and is
      never reported as occupying one.
    """

    run_id: str
    children: tuple[dict, ...] = ()
    captured_at: str = field(default_factory=_now_iso)
    schema_version: str = CHILD_DRAIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CHILD_DRAIN_VERSIONS:
            raise UnsupportedContractVersion(
                "owned-child-drain-v1", self.schema_version,
                SUPPORTED_CHILD_DRAIN_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("an owned-child drain must name its run_id")
        for child in self.children:
            if not isinstance(child, Mapping):
                raise ValueError(
                    "each owned-child drain entry must be a mapping"
                )
            outcome = child.get("outcome")
            if outcome not in _CHILD_DRAIN_OUTCOMES:
                raise ValueError(
                    f"child drain outcome {outcome!r} is not one of "
                    f"{sorted(_CHILD_DRAIN_OUTCOMES)}"
                )
            if outcome == CHILD_DRAINED and not child.get("verified"):
                raise ValueError(
                    "a drained child must carry a verified owned identity; "
                    "refusing to report a drain that was never verified"
                )
            if child.get("kind") == CHILD_HEARTBEAT_ONLY:
                if child.get("useful_slot") == USEFUL_SLOT_OCCUPIED:
                    raise ValueError(
                        "a heartbeat-only stalled child must not occupy a "
                        "useful slot"
                    )

    @property
    def drained(self) -> tuple[str, ...]:
        """The child ids drained by owned identity."""
        return tuple(
            c.get("child_id", "") for c in self.children
            if c.get("outcome") == CHILD_DRAINED
        )

    @property
    def refused(self) -> tuple[str, ...]:
        """The child ids a drain was refused for (never signalled)."""
        return tuple(
            c.get("child_id", "") for c in self.children
            if c.get("outcome") == CHILD_DRAIN_REFUSED
        )

    @property
    def occupied_slots(self) -> int:
        """The useful slots the surviving children still occupy.

        Measured from the report, not from heartbeat traffic: a child whose
        useful-slot state is :data:`USEFUL_SLOT_OCCUPIED` occupies a slot; a
        freed or exempted (heartbeat-only stalled) child does not.
        """
        return sum(
            1 for c in self.children
            if c.get("useful_slot") == USEFUL_SLOT_OCCUPIED
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CHILD_DRAIN_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "children": [dict(c) for c in self.children],
            "occupied_slots": self.occupied_slots,
            "captured_at": self.captured_at,
        }


def _child_drain_entry(fact: _OwnedChildFact) -> dict:
    """Classify one background child into its durable drain entry.

    The classification never signals anything; it reports what a drain *would*
    do, gated on the caller's identity verification:

    * a :data:`CHILD_HEARTBEAT_ONLY` child is a stall -- it is exempted from
      useful-slot occupancy (``USEFUL_SLOT_EXEMPT_STALLED``) and a drain is
      refused, because a heartbeat is liveness traffic, not proof the process
      still *is* the owned attempt;
    * an unverified identity (no record, recycled, stale command, foreign or
      not alive) refuses the drain distinctly;
    * only a ``match`` verification of a genuinely progressing probe yields a
      :data:`CHILD_DRAINED` entry with ``verified`` true.
    """
    verification = fact.verification
    verified = bool(getattr(verification, "ok", False)) and (
        getattr(verification, "reason", "") == process_identity.REASON_MATCH
    )
    reason = getattr(verification, "reason", "identity_unverified")

    if fact.kind == CHILD_HEARTBEAT_ONLY or fact.heartbeat_only:
        # A heartbeat-only stalled child: exempt from useful-slot occupancy.
        return {
            "child_id": fact.child_id,
            "scope": fact.scope,
            "kind": CHILD_HEARTBEAT_ONLY,
            "outcome": CHILD_DRAIN_REFUSED,
            "drain_refused_reason": "heartbeat_only_stall_not_useful",
            "verified": False,
            "useful_slot": USEFUL_SLOT_EXEMPT_STALLED,
            "progress": False,
        }

    if not verified:
        refusal = (
            CHILD_DRAIN_REFUSED_NO_RECORD
            if reason == process_identity.REASON_NO_RECORD
            else CHILD_DRAIN_REFUSED_NOT_OWNED
        )
        return {
            "child_id": fact.child_id,
            "scope": fact.scope,
            "kind": CHILD_PROBE,
            "outcome": CHILD_DRAIN_REFUSED,
            "drain_refused_reason": reason,
            "refusal": refusal,
            "verified": False,
            # A refused probe is still a live useful worker (we did not end it),
            # so it keeps occupying its slot: freeing it would over-admit.
            "useful_slot": USEFUL_SLOT_OCCUPIED,
            "progress": bool(fact.progressing),
        }

    return {
        "child_id": fact.child_id,
        "scope": fact.scope,
        "kind": CHILD_PROBE,
        "outcome": CHILD_DRAINED,
        "drain_refused_reason": "",
        "refusal": "",
        "verified": True,
        "useful_slot": USEFUL_SLOT_FREED,
        "progress": bool(fact.progressing),
    }


def plan_child_drain(
    run_id: str,
    children: Sequence[_OwnedChildFact],
    *,
    captured_at: str = "",
) -> OwnedChildDrain:
    """Classify each background child into a drain report (criterion 3).

    A background probe child is drained **by owned identity**: the caller
    supplies each child's recorded identity *and* the verification it obtained
    by re-observing the live OS, and only a ``match`` verification is reported
    as :data:`CHILD_DRAINED`.  A heartbeat-only stalled child is exempted from
    useful-slot occupancy and refused a drain, so it neither suppresses a
    completed review nor holds a useful slot.
    """
    entries = tuple(_child_drain_entry(fact) for fact in children)
    return OwnedChildDrain(
        run_id=run_id,
        children=entries,
        captured_at=captured_at or _now_iso(),
    )


def _child_field(child: Any, name: str, default: Any) -> Any:
    """Read one field from a background child (mapping or object), never raise.

    The service accepts a child as a mapping or as a duck-typed object, so the
    same call site reads either.  A missing field yields *default* -- the drain
    never invents a fact the caller did not supply.
    """
    if isinstance(child, Mapping):
        return child.get(name, default)
    return getattr(child, name, default)


def _assert_no_stall_occupies_slot(report: "OwnedChildDrain") -> None:
    """Refuse a report where a heartbeat-only stall holds a useful slot.

    This is the fail-closed check for a *completed* review: when a review has
    completed, no heartbeat-only stalled child may be reported as occupying a
    useful slot -- such a stall suppresses nothing and occupies nothing.
    """
    for child in report.children:
        if (
            child.get("kind") == CHILD_HEARTBEAT_ONLY
            and child.get("useful_slot") == USEFUL_SLOT_OCCUPIED
        ):
            raise ValueError(
                "a completed review must not be suppressed by a heartbeat-only "
                "stalled child occupying a useful slot"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 5: repository and non-repository subjects
# ═══════════════════════════════════════════════════════════════════════════
#
# The same controller drives every subject kind.  A *repository* subject
# integrates through Git (the controlled merge); a *non-repository* subject
# (content, configuration, deployment, incident) integrates through a
# **controlled content transaction** -- apply, an independent readback
# verification and a bounded restore -- with no mandatory Git merge.  The
# integration mode is derived from the subject kind, never assumed, and a
# mismatch between an integration's mode and its subject is refused rather than
# merged into the wrong surface.

CONTENT_TXN_SCHEMA_ID = (
    "https://skillweave.dev/schemas/content/content-transaction/v1"
)
CONTENT_TXN_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CONTENT_TXN_VERSIONS = frozenset({CONTENT_TXN_SCHEMA_VERSION})

INTEGRATION_GIT_MERGE = "git_merge"
INTEGRATION_CONTENT_TRANSACTION = "content_transaction"

#: Subject kinds that integrate through a controlled content transaction rather
#: than a Git merge.  ``repository`` is deliberately absent: it is the Git case.
NON_REPOSITORY_INTEGRATION_KINDS: frozenset[str] = frozenset({
    "content", "configuration", "deployment", "incident",
})


class UnknownSubjectKind(ValueError):
    """A subject names a kind this controller does not discriminate."""

    def __init__(self, kind: Any, supported: Sequence[str]):
        self.kind = kind
        self.supported = tuple(supported)
        super().__init__(
            f"unknown subject kind {kind!r}; expected one of {list(self.supported)}"
        )


class SubjectMismatchError(ValueError):
    """An integration step was offered a subject of the wrong kind."""

    def __init__(self, message: str, *, field: Optional[str] = None):
        super().__init__(message)
        self.field = field


def subject_kind_of(subject: Any) -> str:
    """The discriminated ``kind`` of a subject, or fail closed for an unknown.

    The subject vocabulary is the generic contract's
    :data:`skillweave.dispatch.work_contract.SUBJECT_KINDS`; an object whose
    ``kind`` is outside it (or missing) is refused, never inferred to be a
    repository.
    """
    from skillweave.dispatch.work_contract import SUBJECT_KINDS

    kind = getattr(subject, "kind", None)
    if kind is None and isinstance(subject, Mapping):
        kind = subject.get("kind")
    if kind not in SUBJECT_KINDS:
        raise UnknownSubjectKind(kind, SUBJECT_KINDS)
    return kind


def is_repository_subject(subject: Any) -> bool:
    """True when *subject* is the repository (Git) variant.

    A non-repository subject never carries a repository commit, so this is the
    single discriminator that decides whether a controlled merge or a
    controlled content transaction is the correct integration.
    """
    return subject_kind_of(subject) == "repository"


def integration_mode_for(subject: Any) -> str:
    """The integration mode the subject kind requires (git merge vs content txn).

    ``repository`` integrates through Git; every non-repository kind integrates
    through a controlled content transaction.  The mode is a function of the
    kind, so it cannot be supplied (and silently mismatched) by a caller.
    """
    return (
        INTEGRATION_GIT_MERGE
        if is_repository_subject(subject)
        else INTEGRATION_CONTENT_TRANSACTION
    )


@dataclass(frozen=True)
class ContentTransaction:
    """A ``content-transaction-v1`` record: a controlled content apply/verify.

    This is CMS integration for a non-repository subject: a bounded set of
    changes is applied against a fixed ``baseline``, the result is *verified by
    an independent readback* (the ``readback_digest`` must equal the
    ``applied_digest``), and a bounded ``restore`` point is retained so the
    transaction can be rolled back without a Git merge.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the transaction must name its run, subject and baseline;
    * ``subject_kind`` must be a non-repository kind (a repository integrates
      through Git, never a content transaction);
    * ``verify_readback`` and ``restore_bounded`` are mandatory for a controlled
      content transaction -- an unverified or unbounded apply is not one;
    * a verified transaction must actually have a readback that matches what was
      applied (``readback_digest == applied_digest``), so a "verified" record
      that contradicts its own apply is refused.
    """

    run_id: str
    subject: str
    baseline: str
    applied_digest: str
    subject_kind: str = ""
    txn_id: str = ""
    readback_digest: str = ""
    dry_run_digest: str = ""
    applied: bool = False
    verify_readback: bool = False
    restore_bounded: bool = False
    bypass_git_merge: bool = True
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = CONTENT_TXN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CONTENT_TXN_VERSIONS:
            raise UnsupportedContractVersion(
                "content-transaction-v1", self.schema_version,
                SUPPORTED_CONTENT_TXN_VERSIONS,
            )
        for name, value in (
            ("run_id", self.run_id),
            ("subject", self.subject),
            ("baseline", self.baseline),
            ("applied_digest", self.applied_digest),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"content transaction must carry a non-empty {name}"
                )
        # A content transaction is the non-repository integration: a repository
        # subject must integrate through Git, so a transaction that names a
        # repository (or an unknown kind) is the wrong integration surface.
        if self.subject_kind not in NON_REPOSITORY_INTEGRATION_KINDS:
            raise SubjectMismatchError(
                f"a content transaction only integrates non-repository "
                f"subjects {sorted(NON_REPOSITORY_INTEGRATION_KINDS)}, got "
                f"{self.subject_kind!r}; a repository integrates through "
                f"{INTEGRATION_GIT_MERGE}",
                field="subject_kind",
            )
        if not self.verify_readback:
            raise ValueError(
                "a controlled content transaction must verify by independent "
                "readback (verify_readback=True)"
            )
        if not self.restore_bounded:
            raise ValueError(
                "a controlled content transaction must retain a bounded restore "
                "point (restore_bounded=True)"
            )
        if self.readback_digest and self.readback_digest != self.applied_digest:
            raise ValueError(
                f"content transaction readback digest {self.readback_digest!r} "
                f"does not match applied digest {self.applied_digest!r}; a "
                f"verified apply must be reproducible"
            )

    def verified(self) -> bool:
        """True when the readback confirms the applied content."""
        return bool(self.readback_digest) and self.readback_digest == self.applied_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CONTENT_TXN_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "txn_id": self.txn_id,
            "baseline": self.baseline,
            "dry_run_digest": self.dry_run_digest,
            "applied_digest": self.applied_digest,
            "readback_digest": self.readback_digest,
            "applied": self.applied,
            "verify_readback": self.verify_readback,
            "restore_bounded": self.restore_bounded,
            "bypass_git_merge": self.bypass_git_merge,
            "created_at": self.created_at,
        }


def validate_content_transaction(record: Mapping[str, Any]) -> None:
    """Refuse a content-transaction record this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered transaction (unverified, unbounded, or
    a readback that contradicts its apply) is refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_CONTENT_TXN_VERSIONS:
        raise UnsupportedContractVersion(
            "content-transaction-v1", version, SUPPORTED_CONTENT_TXN_VERSIONS
        )
    if record.get("schema_id") != CONTENT_TXN_SCHEMA_ID:
        raise ValueError(
            f"content transaction schema_id {record.get('schema_id')!r} is not "
            f"{CONTENT_TXN_SCHEMA_ID!r}"
        )
    ContentTransaction(
        run_id=record.get("run_id", ""),
        subject=record.get("subject", ""),
        baseline=record.get("baseline", ""),
        applied_digest=record.get("applied_digest", ""),
        subject_kind=record.get("subject_kind", ""),
        txn_id=record.get("txn_id", ""),
        readback_digest=record.get("readback_digest", ""),
        dry_run_digest=record.get("dry_run_digest", ""),
        applied=record.get("applied", False),
        verify_readback=record.get("verify_readback", False),
        restore_bounded=record.get("restore_bounded", False),
        bypass_git_merge=record.get("bypass_git_merge", True),
        created_at=record.get("created_at", ""),
        schema_version=version,
    )


class WrongIntegrationModeError(ValueError):
    """An integration was attempted in a mode its subject kind forbids.

    A repository subject integrates through Git; a non-repository subject
    integrates through a controlled content transaction.  Presenting the wrong
    pair (a repository to a content transaction, or a Git content record to a
    non-repository subject) is refused rather than merged into the wrong
    surface.
    """

    def __init__(self, subject_kind: Any, mode: str):
        self.subject_kind = subject_kind
        self.mode = mode
        super().__init__(
            f"subject kind {subject_kind!r} cannot integrate via {mode!r}"
        )


def resolve_integration(subject: Any, *, record: Optional[Mapping[str, Any]] = None) -> str:
    """Return the integration mode for *subject*, refusing a wrong-mode record.

    The mode is derived from the subject kind.  When an integration *record* is
    also supplied it is checked against that mode: a ``content_transaction``
    record for a repository subject (or a ``git_merge`` record for a
    non-repository subject) is refused, so the same controller dispatches both
    subject kinds without letting a record silently pick the wrong surface.
    """
    mode = integration_mode_for(subject)
    if record is not None:
        declared = record.get("integration_mode") or record.get("mode")
        if declared is not None and declared != mode:
            raise WrongIntegrationModeError(subject_kind_of(subject), mode)
    return mode


# ═══════════════════════════════════════════════════════════════════════════
# The neutral CMS capability contract and its transaction owner
# (SW-1511-CMS-TRANSACTION criteria 1-3)
# ═══════════════════════════════════════════════════════════════════════════
#
# ``cms-transaction-receipt-v1`` is the *neutral* boundary between a
# **capability-bound CMS adapter** (a producer) and the **generic lifecycle
# transaction owner** (this controller).  "Neutral" is load-bearing: the
# contract names the *capabilities* an adapter must provide -- read a baseline,
# draft a change, dry-run it, apply it, read it back and restore it -- and never
# a provider, a channel SDK or a transformer.  One owner drives every CMS
# subject kind through this same contract, exactly as one controller drives every
# repository subject through Git.
#
# The contract is **additive and opt-in**: an existing ``content-transaction-v1``
# record validates unchanged, an adapter that does not declare the capability
# contract is simply not usable for a *managed* transaction, and an unsupported
# version fails closed (:class:`UnsupportedContractVersion`) rather than being
# repaired into a shape this owner happens to support.

CMS_TXN_SCHEMA_ID = (
    "https://skillweave.dev/schemas/content/cms-transaction-receipt/v1"
)
CMS_TXN_SCHEMA_VERSION = "1.0.0"
SUPPORTED_CMS_TXN_VERSIONS = frozenset({CMS_TXN_SCHEMA_VERSION})

# ── criterion 4: no-text-transformer CMS run + external HOLD ───────────────

#: The completed stages a no-text-transformer run reports: the dummy adapter
#: drives the full reversible lifecycle baseline → dry_run → readback → restored.
NO_TXT_TXN_STAGE_COMPLETED = "restored"

#: A named external HOLD: credentials or live egress are missing and the private
#: provider implementation stays in skillweave-packs-pro, never in this
#: repository.  The caller receives this as a typed refusal, never a silent pass
#: or a fabricated success.
EXTERNAL_HOLD_PROVIDER_NOT_HERE = (
    "external_hold: private CMS provider implementation and live-egress "
    "credentials stay in skillweave-packs-pro; this repository carries only "
    "the dummy sandbox adapter (RawContentAdapter).  No live provider is "
    "probed and no credential/e-gress path is opened."
)

# ── criterion 5: candidate-bound milestone receipt ─────────────────────────

MILESTONE_RECEIPT_SCHEMA_ID = (
    "https://skillweave.dev/schemas/content/cms-milestone-receipt/v1"
)
MILESTONE_RECEIPT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_MILESTONE_RECEIPT_VERSIONS = frozenset({MILESTONE_RECEIPT_SCHEMA_VERSION})

#: The neutral capabilities a CMS adapter must provide to drive a managed
#: transaction.  Each is a verb on the adapter, never a provider named by the
#: contract.  ``restore`` is mandatory: a transaction whose baseline cannot be
#: put back is not a *reversible* transaction.
CMS_CAPABILITIES: tuple[str, ...] = (
    "read_baseline",
    "draft",
    "dry_run",
    "apply",
    "readback",
    "restore",
)

#: The ordered lifecycle stages a managed CMS transaction passes through.  The
#: owner refuses a transaction that skips a stage or runs one out of order.
CMS_STAGE_BASELINE = "baseline"
CMS_STAGE_DRAFTED = "drafted"
CMS_STAGE_DRY_RUN = "dry_run"
CMS_STAGE_APPLIED = "applied"
CMS_STAGE_READBACK = "readback"
CMS_STAGE_RESTORED = "restored"
CMS_STAGES: tuple[str, ...] = (
    CMS_STAGE_BASELINE,
    CMS_STAGE_DRAFTED,
    CMS_STAGE_DRY_RUN,
    CMS_STAGE_APPLIED,
    CMS_STAGE_READBACK,
    CMS_STAGE_RESTORED,
)

#: The only binding kind a managed transaction may be driven through.  This is
#: a structural *discriminator*, not a naming convention: a binding must
#: explicitly declare ``sandbox`` and any other declaration (an undeclared or
#: unknown kind, or a ``production``-declared binding) is refused fail-closed --
#: the owner never touches production content.
CMS_BINDING_SANDBOX = "sandbox"
CMS_BINDING_PRODUCTION = "production"
CMS_BINDING_KINDS: tuple[str, ...] = (CMS_BINDING_SANDBOX, CMS_BINDING_PRODUCTION)


class CmsCapabilityError(ValueError):
    """A CMS adapter is missing a capability the managed contract requires.

    Raised when an adapter cannot provide one of :data:`CMS_CAPABILITIES`; the
    owner refuses to drive a managed transaction through an adapter that cannot
    read a baseline, dry-run, apply, read back or restore.
    """

    def __init__(self, missing: Sequence[str], adapter: str = ""):
        self.missing = tuple(missing)
        self.adapter = adapter
        super().__init__(
            f"CMS adapter {adapter or '<unnamed>'!r} is missing required "
            f"capabilities {list(self.missing)}; a managed transaction needs "
            f"all of {list(CMS_CAPABILITIES)}"
        )


class CmsStageError(ValueError):
    """A transaction was driven through the lifecycle out of order."""

    def __init__(self, expected: str, got: str):
        self.expected = expected
        self.got = got
        super().__init__(
            f"CMS transaction stage {got!r} is not reachable from {expected!r}; "
            f"the managed lifecycle is {list(CMS_STAGES)}"
        )


class ExternalHold(ValueError):
    """A named external HOLD: a required resource is not in this repository.

    Raised when credentials, a live provider or live egress are required but the
    private implementation stays in skillweave-packs-pro.  This is a *typed
    refusal*, never a silent pass or a fabricated success: the caller receives
    the named hold reason verbatim.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class CmsCapabilityContract:
    """The neutral capability contract a CMS adapter binds to.

    This is the contract *identity*: it names the artifact, its frozen version
    and the exact capability set it requires -- nothing provider-specific.  An
    adapter is usable by the managed owner only when it declares this contract
    and actually provides every capability in it.
    """

    adapter: str
    capabilities: tuple[str, ...] = CMS_CAPABILITIES
    schema_version: str = CMS_TXN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CMS_TXN_VERSIONS:
            raise UnsupportedContractVersion(
                "cms-transaction-receipt-v1", self.schema_version,
                SUPPORTED_CMS_TXN_VERSIONS,
            )
        if not isinstance(self.adapter, str) or not self.adapter.strip():
            raise ValueError("a CMS capability contract must name its adapter")
        missing = tuple(
            cap for cap in CMS_CAPABILITIES if cap not in self.capabilities
        )
        if missing:
            raise CmsCapabilityError(missing, self.adapter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CMS_TXN_SCHEMA_ID,
            "schema_version": self.schema_version,
            "adapter": self.adapter,
            "capabilities": list(self.capabilities),
        }


def validate_cms_capability_contract(record: Mapping[str, Any]) -> None:
    """Refuse a capability contract this owner cannot support.

    An unknown version (or a record whose schema id is not the CMS receipt's)
    fails closed with :class:`UnsupportedContractVersion`; a contract missing a
    required capability is refused by reconstruction.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_CMS_TXN_VERSIONS:
        raise UnsupportedContractVersion(
            "cms-transaction-receipt-v1", version, SUPPORTED_CMS_TXN_VERSIONS
        )
    if record.get("schema_id") != CMS_TXN_SCHEMA_ID:
        raise ValueError(
            f"CMS capability contract schema_id {record.get('schema_id')!r} is "
            f"not {CMS_TXN_SCHEMA_ID!r}"
        )
    CmsCapabilityContract(
        adapter=record.get("adapter", ""),
        capabilities=tuple(record.get("capabilities", ())),
        schema_version=version,
    )


@dataclass(frozen=True)
class CmsRawHashes:
    """The raw semantic and render hashes of a CMS artifact.

    Restoration is established *by these hashes*, not by a claim: a restore is
    verified only when the raw semantic and render hashes read back after the
    restore equal the baseline's.  Two independently-derived hashes are carried
    so a "restored" claim that only matches on one dimension (a render that
    looks right while the semantic content drifted, or vice versa) is refused.
    """

    semantic: str
    render: str

    def __post_init__(self) -> None:
        for name, value in (("semantic", self.semantic), ("render", self.render)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"CMS raw hashes must carry a non-empty {name} hash"
                )

    def matches(self, other: "CmsRawHashes") -> bool:
        """True when both dimensions reproduce the *other* hashes exactly."""
        return self.semantic == other.semantic and self.render == other.render

    def to_dict(self) -> dict[str, Any]:
        return {"semantic": self.semantic, "render": self.render}


def _hash_cms_content(content: Any) -> CmsRawHashes:
    """A deterministic raw semantic/render hash pair for CMS content.

    The neutral owner never inspects a provider's content shape: it hashes a
    canonical JSON encoding of whatever the adapter hands back, deriving the
    *semantic* hash from the content itself and the *render* hash from the
    semantic hash (so a render that is not anchored to the semantic content is
    impossible).  This is the deterministic stub the dummy adapter binds to; a
    real pack adapter would supply provider-derived raw bytes.
    """
    semantic_blob = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    semantic = "sha256:" + hashlib.sha256(semantic_blob.encode("utf-8")).hexdigest()
    render = "sha256:" + hashlib.sha256(
        ("render:" + semantic).encode("utf-8")
    ).hexdigest()
    return CmsRawHashes(semantic=semantic, render=render)


class RawContentAdapter:
    """A capability-bound *dummy* CMS adapter over raw in-memory content.

    This is the dummy adapter criterion 3 names: it declares the neutral
    :class:`CmsCapabilityContract` and implements every capability against a
    plain in-memory value, so apply / readback / restore can be reproduced
    without a provider, a transformer or live egress.  It exposes only verbs the
    contract names -- no provider, no channel and no transformer.
    """

    contract: CmsCapabilityContract

    def __init__(self, *, adapter: str = "raw-dummy") -> None:
        self.contract = CmsCapabilityContract(adapter=adapter)
        self._current: Any = None
        self._restore_point: Any = None

    # -- capabilities -----------------------------------------------------

    def read_baseline(self, content: Any) -> CmsRawHashes:
        """Read the baseline the transaction starts from."""
        self._current = content
        self._restore_point = content
        return _hash_cms_content(content)

    def draft(self, content: Any, change: Any) -> Any:
        """Draft the changed content (still not applied)."""
        return change

    def dry_run(self, content: Any) -> CmsRawHashes:
        """Hash the drafted content without applying it."""
        return _hash_cms_content(content)

    def apply(self, content: Any) -> CmsRawHashes:
        """Apply the drafted content; retain the bounded restore point."""
        self._restore_point = self._current
        self._current = content
        return _hash_cms_content(content)

    def readback(self) -> CmsRawHashes:
        """Read the applied content back from the target surface."""
        return _hash_cms_content(self._current)

    def restore(self) -> CmsRawHashes:
        """Restore the bounded baseline; hash what is now present."""
        self._current = self._restore_point
        return _hash_cms_content(self._current)


@dataclass(frozen=True)
class CmsTransaction:
    """A ``cms-transaction-receipt-v1`` record: a managed CMS transaction.

    Driven by one transaction owner through the ordered lifecycle
    (:data:`CMS_STAGES`) -- baseline, draft, dry-run, bounded apply, readback and
    restore -- against a neutral capability contract.  Fail-closed invariants:

    * ``schema_version`` must be supported;
    * the transaction names its run, subject revision, adapter and baseline;
    * ``capabilities`` must include every capability in
      :data:`CMS_CAPABILITIES` (an adapter that cannot restore is not managed);
    * a ``restored`` transaction must present raw semantic *and* render hashes
      equal to the baseline's -- a restore that only matches on one dimension,
      or that claims a restore without the hashes, is refused.
    """

    run_id: str
    subject: str
    subject_kind: str
    subject_revision: str
    adapter: str
    baseline_hashes: CmsRawHashes
    txn_id: str = ""
    stage: str = CMS_STAGE_BASELINE
    draft_digest: str = ""
    dry_run_hashes: Optional[CmsRawHashes] = None
    applied_hashes: Optional[CmsRawHashes] = None
    readback_hashes: Optional[CmsRawHashes] = None
    restored_hashes: Optional[CmsRawHashes] = None
    capabilities: tuple[str, ...] = CMS_CAPABILITIES
    producer_identity: str = ""
    #: The owned attempt's dispatch intent this transaction is bound to (the
    #: durable source of the producer identity).  A transaction with no bound
    #: intent has no verifiable producer and is refused for publication.
    intent_id: str = ""
    #: The *owner-authorized sandbox target* the transaction was opened on.  It
    #: is recorded so a recovering owner can re-verify (against the owner's
    #: declaration) that it is resuming on the sandbox it opened, never on a
    #: production surface reached through a mis-declared binding.
    sandbox_target: str = ""
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = CMS_TXN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_CMS_TXN_VERSIONS:
            raise UnsupportedContractVersion(
                "cms-transaction-receipt-v1", self.schema_version,
                SUPPORTED_CMS_TXN_VERSIONS,
            )
        for name, value in (
            ("run_id", self.run_id),
            ("subject", self.subject),
            ("subject_revision", self.subject_revision),
            ("adapter", self.adapter),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"a CMS transaction must carry a non-empty {name}"
                )
        if self.subject_kind not in NON_REPOSITORY_INTEGRATION_KINDS:
            raise SubjectMismatchError(
                f"a CMS transaction only manages non-repository subjects "
                f"{sorted(NON_REPOSITORY_INTEGRATION_KINDS)}, got "
                f"{self.subject_kind!r}; a repository integrates through "
                f"{INTEGRATION_GIT_MERGE}",
                field="subject_kind",
            )
        if not isinstance(self.baseline_hashes, CmsRawHashes):
            raise ValueError(
                "a CMS transaction must carry baseline raw semantic/render hashes"
            )
        missing = tuple(c for c in CMS_CAPABILITIES if c not in self.capabilities)
        if missing:
            raise CmsCapabilityError(missing, self.adapter)
        if self.stage not in CMS_STAGES:
            raise ValueError(
                f"CMS transaction stage {self.stage!r} is not one of "
                f"{list(CMS_STAGES)}"
            )
        # A readback must reproduce what was applied; a contradictory readback
        # is refused rather than recorded as a verified apply.
        if self.applied_hashes is not None and self.readback_hashes is not None:
            if not self.readback_hashes.matches(self.applied_hashes):
                raise ValueError(
                    "CMS transaction readback does not reproduce the applied "
                    "content; a verified apply must be reproducible"
                )
        # A restore is established by raw hashes matching the baseline's.
        if self.restored_hashes is not None:
            if not self.restored_hashes.matches(self.baseline_hashes):
                raise ValueError(
                    "CMS transaction restore hashes do not reproduce the "
                    "baseline; restoration is not established"
                )
            if self.stage != CMS_STAGE_RESTORED:
                raise ValueError(
                    "CMS transaction carries restored hashes but is not at the "
                    f"{CMS_STAGE_RESTORED!r} stage"
                )
        # A recorded sandbox target must not name a production surface: a
        # transaction that recorded a production-looking target is refused
        # rather than trusted (fail closed), so a mis-declared binding that
        # reached a production target cannot be persisted or replayed.
        if self.sandbox_target and _looks_like_production(self.sandbox_target):
            raise CmsBindingRefused(
                CMS_BINDING_SANDBOX, target=self.sandbox_target
            )

    def verified(self) -> bool:
        """True when the readback reproduces the applied content."""
        if self.applied_hashes is None or self.readback_hashes is None:
            return False
        return self.readback_hashes.matches(self.applied_hashes)

    def restored(self) -> bool:
        """True when the restore reproduces the baseline raw hashes."""
        if self.restored_hashes is None:
            return False
        return self.restored_hashes.matches(self.baseline_hashes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": CMS_TXN_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "subject_revision": self.subject_revision,
            "adapter": self.adapter,
            "txn_id": self.txn_id,
            "stage": self.stage,
            "baseline_hashes": self.baseline_hashes.to_dict(),
            "draft_digest": self.draft_digest,
            "dry_run_hashes": (
                None if self.dry_run_hashes is None else self.dry_run_hashes.to_dict()
            ),
            "applied_hashes": (
                None if self.applied_hashes is None else self.applied_hashes.to_dict()
            ),
            "readback_hashes": (
                None if self.readback_hashes is None else self.readback_hashes.to_dict()
            ),
            "restored_hashes": (
                None if self.restored_hashes is None else self.restored_hashes.to_dict()
            ),
            "capabilities": list(self.capabilities),
            "producer_identity": self.producer_identity,
            "intent_id": self.intent_id,
            "sandbox_target": self.sandbox_target,
            "verified": self.verified(),
            "restored": self.restored(),
            "created_at": self.created_at,
        }


class CmsBindingRefused(ValueError):
    """A managed transaction was offered a binding that is not an authorized sandbox.

    The managed CMS owner only ever drives a transaction through an *explicitly
    sandbox-declared* binding.  An undeclared/unknown binding, or a binding that
    declares itself ``production``, is refused fail-closed rather than touched,
    so "this is only a sandbox" is enforced structurally instead of by naming.
    """

    def __init__(self, kind: Any, *, target: str = ""):
        self.kind = kind
        self.target = target
        super().__init__(
            f"CMS binding kind {kind!r} is not the declared "
            f"{CMS_BINDING_SANDBOX!r} sandbox binding"
            + (f" for {target!r}" if target else "")
            + f"; the owner refuses any undeclared, unknown or "
            f"{CMS_BINDING_PRODUCTION!r}-declared binding"
        )


@dataclass(frozen=True)
class SandboxBinding:
    """The one owner-authorized *sandbox* binding a managed transaction runs on.

    The discriminator is structural: ``kind`` must be explicitly the declared
    :data:`CMS_BINDING_SANDBOX`.  An empty/unknown ``kind`` (an undeclared
    binding) or a :data:`CMS_BINDING_PRODUCTION` binding is refused at
    construction, so no caller can reach a production surface by an implicit
    default and the sandbox is not merely a naming convention.  ``target`` names
    the concrete sandbox the owner authorizes.
    """

    target: str
    kind: str = CMS_BINDING_SANDBOX

    def __post_init__(self) -> None:
        if self.kind != CMS_BINDING_SANDBOX:
            raise CmsBindingRefused(self.kind, target=self.target)
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("a sandbox binding must name its target")
        # A target that names a production surface is refused even when the
        # binding declares ``kind == "sandbox"``: the declared kind is not the
        # whole check, so ``SandboxBinding(target="production/real-channel")``
        # cannot smuggle a production surface through the sandbox discriminator.
        if _looks_like_production(self.target):
            raise CmsBindingRefused(self.kind, target=self.target)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target}


def resolve_cms_sandbox_binding(binding: Any) -> str:
    """Resolve the owner-authorized sandbox target, refusing everything else.

    This is the structural discriminator that keeps the managed CMS owner on a
    declared sandbox.  Only a :class:`SandboxBinding` (``kind == "sandbox"``)
    resolves; a ``None``/undeclared binding, a mapping whose ``kind`` is missing
    or unknown, or a ``production``-declared binding is refused fail-closed with
    :class:`CmsBindingRefused`.  The sandbox is therefore declared structurally,
    not by leaving the binding implicit.
    """
    if isinstance(binding, SandboxBinding):
        if binding.kind != CMS_BINDING_SANDBOX:
            raise CmsBindingRefused(binding.kind, target=binding.target)
        return resolve_owner_sandbox_target(binding.target)
    if isinstance(binding, Mapping):
        kind = binding.get("kind", "")
        if kind != CMS_BINDING_SANDBOX:
            raise CmsBindingRefused(kind, target=str(binding.get("target", "")))
        target = binding.get("target", "")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("a sandbox binding must name its target")
        return resolve_owner_sandbox_target(target)
    raise CmsBindingRefused(binding)


#: Substrings that mark a binding target as *production-looking* even when it
#: dresses itself up as a sandbox.  A target may not merely *say* "sandbox": the
#: literal phrase must not also name a production surface, so
#: ``"production/real-channel"`` and ``"prod/sandbox"`` are refused rather than
#: accepted on the strength of the declared ``kind`` alone.
CMS_PRODUCTION_TARGET_MARKERS: tuple[str, ...] = ("production", "prod", "live", "real-channel")


class OwnerSandboxDeclaration:
    """The owner's declaration of exactly which sandbox targets are authorized.

    The binding discriminator (:func:`resolve_cms_sandbox_binding`) proves a
    binding *declares itself* a sandbox; this is the second, independent half:
    the resolved target must be **one of the targets the owner actually
    authorized**.  A binding that names an unlisted target, or any target that
    looks like a production surface (:data:`CMS_PRODUCTION_TARGET_MARKERS`), is
    refused fail-closed -- so "kind == sandbox" is not sufficient to reach a
    surface the owner never declared.  An empty declaration authorizes nothing
    (fail closed), rather than everything.
    """

    __slots__ = ("targets",)

    def __init__(self, targets: Any = ()):
        resolved: list[str] = []
        for target in targets:
            if not isinstance(target, str) or not target.strip():
                raise ValueError(
                    "an owner sandbox declaration must name non-empty targets"
                )
            normalized = target.strip()
            if _looks_like_production(normalized):
                raise CmsBindingRefused(
                    CMS_BINDING_SANDBOX, target=normalized
                )
            resolved.append(normalized)
        self.targets: tuple[str, ...] = tuple(resolved)

    def authorizes(self, target: str) -> bool:
        """True only for a listed, non-production-looking target."""
        if not isinstance(target, str) or not target.strip():
            return False
        candidate = target.strip()
        if _looks_like_production(candidate):
            return False
        return candidate in self.targets

    def to_dict(self) -> dict[str, Any]:
        return {"targets": list(self.targets)}

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, OwnerSandboxDeclaration):
            return self.targets == other.targets
        return NotImplemented

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"OwnerSandboxDeclaration(targets={list(self.targets)!r})"


def _looks_like_production(target: str) -> bool:
    """True when *target* names a production-looking surface (case-insensitive)."""
    lowered = target.lower()
    return any(marker in lowered for marker in CMS_PRODUCTION_TARGET_MARKERS)


def resolve_owner_sandbox_target(
    target: str, *, authorized: Any = None
) -> str:
    """Resolve a *recorded* sandbox target against the owner's declaration.

    Fail-closed, in order: the target must be non-empty; it must not look like a
    production surface; and (when an ``authorized`` declaration is given) it must
    be one of the owner's declared targets.  A production-looking or unlisted
    target raises :class:`CmsBindingRefused`, so a target recorded by an earlier
    binding cannot be replayed onto an unauthorized surface by a recovering
    owner.
    """
    if not isinstance(target, str) or not target.strip():
        raise CmsBindingRefused(CMS_BINDING_SANDBOX, target="")
    normalized = target.strip()
    if _looks_like_production(normalized):
        raise CmsBindingRefused(CMS_BINDING_SANDBOX, target=normalized)
    if authorized is not None:
        if not isinstance(authorized, OwnerSandboxDeclaration):
            authorized = OwnerSandboxDeclaration(authorized)
        if not authorized.authorizes(normalized):
            raise CmsBindingRefused(CMS_BINDING_SANDBOX, target=normalized)
    return normalized


def _require_owner_authorized_sandbox(target: str, declaration: Any) -> str:
    """Resolve *target* as one the owner **positively named** in *declaration*.

    This is the allow-list half of the sandbox discriminator, wired at the real
    call sites: the target must appear in an actual :class:`OwnerSandboxDeclaration`
    the owner supplied (or a mapping/list the owner supplied, coerced to one).
    An *absent* declaration authorizes nothing -- unlike
    :func:`resolve_owner_sandbox_target`, a missing declaration is refused here
    rather than skipping the check -- so a caller cannot reach a surface by
    omitting the declaration.  The production-marker denylist stays an
    *additional* check (a target may not be declared even if the owner lists it),
    but the primary decision is the positive, owner-named allow-list.
    """
    if declaration is None:
        raise CmsBindingRefused(CMS_BINDING_SANDBOX, target=target)
    if isinstance(declaration, OwnerSandboxDeclaration):
        authorized = declaration
    else:
        authorized = OwnerSandboxDeclaration(declaration)
    return resolve_owner_sandbox_target(target, authorized=authorized)


def _hashes_from(record: Optional[Mapping[str, Any]]) -> Optional[CmsRawHashes]:
    if record is None:
        return None
    return CmsRawHashes(semantic=record.get("semantic", ""), render=record.get("render", ""))


def validate_cms_transaction(record: Mapping[str, Any]) -> None:
    """Refuse a CMS transaction record this owner cannot support.

    An unknown version or schema id fails closed; reconstructing the frozen
    record re-runs every fail-closed invariant, so a tampered transaction (a
    contradictory readback or a restore that does not reproduce the baseline) is
    refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_CMS_TXN_VERSIONS:
        raise UnsupportedContractVersion(
            "cms-transaction-receipt-v1", version, SUPPORTED_CMS_TXN_VERSIONS
        )
    if record.get("schema_id") != CMS_TXN_SCHEMA_ID:
        raise ValueError(
            f"CMS transaction schema_id {record.get('schema_id')!r} is not "
            f"{CMS_TXN_SCHEMA_ID!r}"
        )
    baseline = _hashes_from(record.get("baseline_hashes"))
    CmsTransaction(
        run_id=record.get("run_id", ""),
        subject=record.get("subject", ""),
        subject_kind=record.get("subject_kind", ""),
        subject_revision=record.get("subject_revision", ""),
        adapter=record.get("adapter", ""),
        baseline_hashes=baseline,
        txn_id=record.get("txn_id", ""),
        stage=record.get("stage", CMS_STAGE_BASELINE),
        draft_digest=record.get("draft_digest", ""),
        dry_run_hashes=_hashes_from(record.get("dry_run_hashes")),
        applied_hashes=_hashes_from(record.get("applied_hashes")),
        readback_hashes=_hashes_from(record.get("readback_hashes")),
        restored_hashes=_hashes_from(record.get("restored_hashes")),
        capabilities=tuple(record.get("capabilities", ())),
        producer_identity=record.get("producer_identity", ""),
        intent_id=record.get("intent_id", ""),
        sandbox_target=record.get("sandbox_target", ""),
        created_at=record.get("created_at", ""),
        schema_version=version,
    )


# -- Publication authorization (criterion 2) ---------------------------------
#
# A CMS publication is an *irreversible* action on a *content* subject.  It is
# authorized only when the designated explicit authorization binds to the exact
# subject revision that is being published, and the autonomy policy governing
# the run permits the action.  A Unicorn policy auto-approves reversible work
# but *holds* for an irreversible action or a scope expansion, so Unicorn can
# never authorize an irreversible publication or widen the write scope -- that
# refusal is the same :class:`~skillweave.runtime.assay.PolicyDecision` the assay
# policy produces, surfaced here rather than re-implemented.

PUBLICATION_ACTION_KIND = "publish"


class PublicationRefused(ValueError):
    """An irreversible publication was not authorized for its exact subject."""

    def __init__(self, reason: str, *, field: str = ""):
        self.field = field
        super().__init__(reason)


@dataclass(frozen=True)
class PublicationAuthorization:
    """The designated explicit authorization to publish one subject revision.

    ``subject_revision`` and ``artifact_digest`` bind the authorization to the
    *exact* revision it authorizes: an authorization for a different revision
    (or a different artifact digest) is refused, so a change to the content
    invalidates the authorization rather than publishing an unreviewed revision.
    ``authorizer`` must be a designated authority distinct from the producer --
    a self-authored authorization is refused.
    """

    authorizer: str
    subject_revision: str
    artifact_digest: str
    authority: str = ""
    designated: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("authorizer", self.authorizer),
            ("subject_revision", self.subject_revision),
            ("artifact_digest", self.artifact_digest),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"a publication authorization must carry a non-empty {name}"
                )

    def binds(self, *, subject_revision: str, artifact_digest: str) -> bool:
        """True when the authorization names exactly this revision/digest."""
        return (
            self.subject_revision == subject_revision
            and self.artifact_digest == artifact_digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorizer": self.authorizer,
            "subject_revision": self.subject_revision,
            "artifact_digest": self.artifact_digest,
            "authority": self.authority,
            "designated": self.designated,
        }


@dataclass(frozen=True)
class PublicationDecision:
    """The outcome of authorizing a CMS publication.

    ``approved`` is True only when a designated explicit authorization bound to
    the exact revision *and* the governing autonomy policy approved the
    irreversible action.  ``policy_reason`` records the policy's reason, so a
    refusal is auditable rather than silent.
    """

    approved: bool
    reason: str
    policy_reason: str = ""
    requires_human: bool = False


# ── criterion 5: candidate-bound milestone receipt ─────────────────────────


class MilestoneReceiptRefused(ValueError):
    """A candidate-bound milestone receipt was missing, changed, stale or foreign.

    Raised when ``require_cms_milestone_receipt`` cannot find a receipt bound to
    the installed candidate, or when the receipt's digest does not match the
    stored candidate -- the operation is refused fail-closed, never skipped.
    """

    def __init__(self, reason: str, *, found: str = "", expected: str = ""):
        self.reason = reason
        self.found = found
        self.expected = expected
        super().__init__(reason)


@dataclass(frozen=True)
class CmsMilestoneReceipt:
    """A candidate-bound milestone receipt stored *before* any runtime CMS apply.

    The receipt binds the installed candidate (its commit SHA) to the Assay
    proofs that qualified it: the CLI proof and the Operator-MCP proof.  It is
    stored durably and must be *present and unchanged* before any
    runtime-dependent CMS apply proceeds -- a missing, changed, stale or foreign
    receipt is refused fail-closed.

    ``candidate_sha`` is the exact commit of the installed candidate.
    ``cli_assay_proof`` and ``mcp_assay_proof`` are the digest-bound evidence
    the native Assay recorded for the CLI and Operator-MCP qualification runs.
    ``digest`` is the content digest that binds the receipt to its payload.
    """

    candidate_sha: str
    cli_assay_proof: str
    mcp_assay_proof: str
    stored_at: str = ""
    schema_version: str = MILESTONE_RECEIPT_SCHEMA_VERSION
    digest: str = ""

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_MILESTONE_RECEIPT_VERSIONS:
            raise UnsupportedContractVersion(
                "cms-milestone-receipt-v1", self.schema_version,
                SUPPORTED_MILESTONE_RECEIPT_VERSIONS,
            )
        for name in ("candidate_sha", "cli_assay_proof", "mcp_assay_proof"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"a CMS milestone receipt must carry a non-empty {name}"
                )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_id": MILESTONE_RECEIPT_SCHEMA_ID,
            "schema_version": self.schema_version,
            "candidate_sha": self.candidate_sha,
            "cli_assay_proof": self.cli_assay_proof,
            "mcp_assay_proof": self.mcp_assay_proof,
            "stored_at": self.stored_at,
        }

    def to_dict(self) -> dict[str, Any]:
        data = self.payload()
        data["digest"] = self.digest
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def _milestone_receipt_digest(payload: dict[str, Any]) -> str:
    """A content digest binding a milestone receipt to its payload."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_cms_milestone_receipt(record: Mapping[str, Any]) -> None:
    """Validate a ``cms-milestone-receipt-v1`` record; fail-closed on mismatch."""
    if record.get("schema_id") != MILESTONE_RECEIPT_SCHEMA_ID:
        raise ValueError(
            f"CMS milestone receipt schema_id {record.get('schema_id')!r} is "
            f"not {MILESTONE_RECEIPT_SCHEMA_ID!r}"
        )
    CmsMilestoneReceipt(
        candidate_sha=record.get("candidate_sha", ""),
        cli_assay_proof=record.get("cli_assay_proof", ""),
        mcp_assay_proof=record.get("mcp_assay_proof", ""),
        stored_at=record.get("stored_at", ""),
        schema_version=record.get("schema_version", MILESTONE_RECEIPT_SCHEMA_VERSION),
    )


def require_cms_milestone_receipt(
    *,
    store: Any,
    candidate_sha: str,
) -> CmsMilestoneReceipt:
    """Require an exact candidate-bound milestone receipt; refuse otherwise.

    The receipt must be present in the durable store, must bind to exactly
    ``candidate_sha``, and its content digest must match the stored record --
    a missing, changed, stale or foreign receipt is refused fail-closed.

    Returns the validated receipt when present and unchanged.
    """
    record = getattr(store, "get_cms_milestone_receipt", lambda c: None)(
        candidate_sha
    )
    if record is None:
        raise MilestoneReceiptRefused(
            f"no CMS milestone receipt found for candidate {candidate_sha!r}; "
            f"a runtime-dependent CMS apply requires an exact, unchanged "
            f"receipt bound to the installed candidate",
            expected=candidate_sha,
        )
    validate_cms_milestone_receipt(record)
    stored_digest = record.get("digest", "")
    receipt = CmsMilestoneReceipt(
        candidate_sha=record.get("candidate_sha", ""),
        cli_assay_proof=record.get("cli_assay_proof", ""),
        mcp_assay_proof=record.get("mcp_assay_proof", ""),
        stored_at=record.get("stored_at", ""),
        schema_version=record.get(
            "schema_version", MILESTONE_RECEIPT_SCHEMA_VERSION
        ),
    )
    expected_digest = _milestone_receipt_digest(receipt.payload())
    if stored_digest != expected_digest:
        raise MilestoneReceiptRefused(
            f"CMS milestone receipt for candidate {candidate_sha!r} has been "
            f"changed: stored digest {stored_digest!r} does not match the "
            f"payload digest {expected_digest!r}; a changed receipt is refused",
            found=stored_digest,
            expected=expected_digest,
        )
    return receipt


#: The MCP reviewing role that may decide a publication gate.  A publication
#: review is only ever admitted from a durable ``review_action`` record whose
#: *server-issued* principal carries this role -- the MCP role that owns
#: ``review_run`` (:data:`skillweave.runtime.authority.MCP_ROLE_SCOPES`).  A
#: producer answering its own publication review is never independent, so no
#: other role is admitted; a record with no server-stamped role is refused,
#: never defaulted to this value.
PUBLICATION_REVIEW_ROLE = "reviewer"

#: The durable kind of the MCP ``review_action`` result record
#: (:func:`skillweave.mcp.server.review_action` posts a ``status`` message whose
#: body is bound to the review's run/task/attempt).  A publication review must
#: be *this* record, loaded from the durable mailbox -- never a caller-created
#: object.
DURABLE_REVIEW_MESSAGE_KIND = "status"


@dataclass(frozen=True)
class DurableReviewRef:
    """A *reference* to a durable review record, not a review.

    The reference is only a locator -- the mailbox it lives in and the id of the
    record posted by the MCP ``review_action`` tool.  Everything that decides
    the review is *loaded from that durable record* (:meth:`resolve`), so a
    caller can point at a review but cannot fabricate its independence, its role
    or the attempt it answered: those are derived from the server-recorded
    ``principal`` and the recorded ``run_id``/``task_id``/``attempt``.
    """

    mailbox_path: str
    message_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.mailbox_path, str) or not self.mailbox_path.strip():
            raise ValueError("a durable review reference must name its mailbox")
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise ValueError("a durable review reference must name a message id")

    def resolve(self) -> "ResolvedReview":
        """Load the referenced durable record and project a :class:`ResolvedReview`.

        Fail-closed: an absent record, a record that is not a review result and a
        record whose bytes do not match its declared digest are all refused by
        name (:class:`PublicationRefused`), so a dangling or tampered reference
        never silently resolves to a review.
        """
        from skillweave.dispatch import messaging as _messaging

        mailbox = _messaging.AgentMailbox(self.mailbox_path)
        record = mailbox.get(self.message_id)
        if record is None:
            raise PublicationRefused(
                f"durable review {self.message_id!r} is not in mailbox "
                f"{self.mailbox_path!r}; a review must be a durable, server-"
                f"recorded result",
                field="review",
            )
        return ResolvedReview.from_record(record)


def _server_issued_principal_role(principal: str) -> Optional[str]:
    """The MCP role of a *server-issued* principal, or ``None``.

    A principal the MCP authority mints has the exact shape
    ``mcp:<role>:<client_id>`` where ``<role>`` is one of
    :data:`~skillweave.runtime.authority.MCP_ROLE_SCOPES` (the server-owned
    registry) and ``<client_id>`` is a non-empty token.  Anything else -- a
    caller-declared principal string, an unbound-authority principal, or a
    ``role`` that is not a real registry role -- is *not* server-issued and
    returns ``None``.  This is the structural test that keeps a
    caller-declared principal/role from resolving as an independent review.
    """
    if not isinstance(principal, str):
        return None
    parts = principal.split(":")
    if len(parts) != 3:
        return None
    prefix, role, client_id = parts
    if prefix != "mcp" or not role or not client_id:
        return None
    from skillweave.runtime.authority import MCP_ROLE_SCOPES

    if role not in MCP_ROLE_SCOPES:
        return None
    return role


@dataclass(frozen=True)
class ResolvedReview:
    """A review projected from its *durable* record.

    Every field is *loaded* from the record the MCP server wrote, never supplied
    by a caller: ``principal`` is the server-issued principal the review was
    recorded under, ``role`` is the reviewing role the server stamped from that
    session, and ``run_id``/``task_id``/``attempt`` are the scope the review
    answered.  The review's own ``verdict`` and the exact ``subject_revision``/
    ``artifact_digest``/``criteria`` it reviewed are read from the durable body
    too -- the publication binds to *those* facts, never to a value a caller
    supplies at authorization time.  Independence is decided against the
    server-recorded ``principal``, so a producer that names itself the reviewer
    is refused even though it controls its own arguments.
    """

    message_id: str
    principal: str
    role: str
    run_id: str
    task_id: str
    attempt: int
    subject: str
    digest: str
    verdict: str
    subject_revision: str
    artifact_digest: str
    criteria: tuple[str, ...]

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ResolvedReview":
        """Project a durable ``review_action`` result record, or refuse it.

        A record that is not a ``status`` result whose body names the review's
        run/task/attempt is refused by name: only a genuine review result carries
        the durable scope a publication must bind to.  The principal must be the
        *server-issued* principal shape (``mcp:<role>:<client_id>``) and the
        body must carry the reviewing role the server stamped -- an unbound
        authority or a caller-declared principal/role is refused, never
        resolving as an independent review.  The review's verdict, subject
        revision, artifact digest and criteria are read from the *body* (which
        the record digest covers), so a later caller cannot substitute them.
        """
        kind = str(record.get("kind") or "")
        if kind != DURABLE_REVIEW_MESSAGE_KIND:
            raise PublicationRefused(
                f"durable record {record.get('message_id')!r} is a {kind!r} "
                f"message, not a review result; a publication review must be the "
                f"server-recorded review result",
                field="review",
            )
        body = record.get("body")
        if not isinstance(body, Mapping):
            raise PublicationRefused(
                f"durable review {record.get('message_id')!r} carries no recorded "
                f"body to bind",
                field="review",
            )
        principal = str(record.get("principal") or "").strip()
        if not principal:
            raise PublicationRefused(
                f"durable review {record.get('message_id')!r} was recorded under "
                f"no principal; there is no accountable reviewer",
                field="review",
            )
        # The reviewing role is stamped by the server from the *server-issued*
        # session.  A record whose body carries no role (an unbound-authority
        # write, or a caller that declared its own role) is refused: there is no
        # authenticated reviewer role to trust.
        role = str(body.get("role") or "").strip()
        if not role:
            raise PublicationRefused(
                f"durable review {record.get('message_id')!r} carries no "
                f"server-stamped reviewing role; a caller-declared role is not an "
                f"independent review",
                field="role",
            )
        # The principal must be the *server-issued* shape and must agree with
        # the stamped role -- a caller-declared principal (or a status record
        # posted through the unbound general mailbox) is not server-issued and
        # cannot resolve as an independent review.
        issued_role = _server_issued_principal_role(principal)
        if issued_role is None or issued_role != role:
            raise PublicationRefused(
                f"durable review {record.get('message_id')!r} was recorded under "
                f"principal {principal!r}, which is not a server-issued "
                f"principal matching the stamped role {role!r}; a caller-declared "
                f"principal/role is not an independent review",
                field="principal",
            )
        raw_criteria = body.get("criteria")
        if isinstance(raw_criteria, (list, tuple)):
            criteria = tuple(str(c) for c in raw_criteria if str(c).strip())
        elif isinstance(raw_criteria, str) and raw_criteria.strip():
            criteria = tuple(
                part.strip() for part in raw_criteria.split(",") if part.strip()
            )
        else:
            criteria = ()
        return cls(
            message_id=str(record.get("message_id") or ""),
            principal=principal,
            role=role,
            run_id=str(record.get("run_id") or body.get("run_id") or ""),
            task_id=str(record.get("task_id") or body.get("task_id") or ""),
            attempt=int(record.get("attempt") or body.get("attempt") or 0),
            subject=str(body.get("assignment") or ""),
            digest=str(record.get("digest") or ""),
            verdict=str(body.get("verdict") or "").strip(),
            subject_revision=str(body.get("subject_revision") or "").strip(),
            artifact_digest=str(body.get("artifact_digest") or "").strip(),
            criteria=criteria,
        )


def _durable_producer_identity(intent: Any) -> str:
    """The producer identity *derived from and verified against* an owned attempt.

    The producer of a managed CMS transaction is the identity the dispatch
    intent recorded for the owned attempt, never a string the caller declares at
    :meth:`NativeProcessService.open_cms_transaction`.  The identity is a
    fingerprint of the recorded :class:`~skillweave.runtime.process_identity.ProcessIdentity`
    -- its immutable ``creation_token`` (PID + start instant + image), which a
    caller cannot forge and which binds the producer to the exact owned process.

    The recorded identity must also be **verified against live OS facts** with
    :func:`skillweave.runtime.process_identity.verify`: an identity that merely
    *names* a PID (a fabricated dict, a recycled PID, a stale command or a
    foreign process) is not the owned attempt's producer and yields the empty
    string, so the transaction is refused fail-closed rather than trusting a
    caller-supplied identity that was never alive under this owner.
    """
    identity = getattr(intent, "process_identity", None)
    if not isinstance(identity, Mapping):
        return ""
    record = process_identity.ProcessIdentity.from_dict(dict(identity))
    if record is None:
        return ""
    verification = process_identity.verify(dict(identity), record.pid)
    if not verification.ok:
        return ""
    return f"attempt:{record.creation_token}"


def _require_current_independent_review(
    review: Optional[AttemptReview],
    *,
    producer_identity: str,
    subject_revision: str,
    artifact_digest: str,
    expected_review_criteria: Sequence[str] = (),
) -> None:
    """Refuse a publication whose independent review is missing or unusable.

    Fail-closed, in order: a review must be present and have a supported form
    (an :class:`~skillweave.trace.gate_receipt.AttemptReview` whose role is a
    real reviewing role); it must be *independent* (not answered by the
    producer); it must be *current* for the exact revision/digest (a stale
    review is refused); and it must cover every ``expected_review_criteria``.
    This is the production caller of
    :func:`~skillweave.trace.gate_receipt.assert_review_current`, so the
    review-binds-to-exact-revision half is proven through the real publication
    path rather than on a helper.
    """
    if review is None:
        raise PublicationRefused(
            "irreversible CMS publication requires a current independent review "
            "of the exact subject revision; none was presented",
            field="review",
        )
    if not isinstance(review, AttemptReview):
        raise PublicationRefused(
            "the publication review is not a supported independent review form",
            field="review",
        )
    binding = review.binding
    if binding is None:
        raise PublicationRefused(
            "the publication review carries no binding to a subject revision",
            field="review",
        )
    if review.role != PUBLICATION_REVIEW_ROLE:
        raise PublicationRefused(
            f"publication review role {review.role!r} is not the independent "
            f"reviewing role {PUBLICATION_REVIEW_ROLE!r}",
            field="review",
        )
    # The review's *own* verdict must be the accepting verdict.  A review that
    # reached REPAIR/HOLD (or carries no verdict yet) does not authorize an
    # irreversible publication -- the accepting verdict is read from the review,
    # never assumed.
    if review.verdict != ACCEPTANCE_ACCEPTED:
        raise PublicationRefused(
            f"the publication review reached verdict "
            f"{review.verdict or '<missing>'!r}, not {ACCEPTANCE_ACCEPTED!r}; only "
            f"an accepting independent review authorizes irreversible publication",
            field="review",
        )
    if review.answering_identity == producer_identity:
        raise PublicationRefused(
            f"the publication review was answered by the producer "
            f"{producer_identity!r}; a producer cannot review its own "
            f"publication",
            field="review",
        )
    if not review.independent:
        raise PublicationRefused(
            "the publication review is not independent of the producer",
            field="review",
        )
    # The review must be current for the exact revision *and* digest.
    try:
        assert_review_current(
            review, subject_revision=subject_revision, artifact_digest=artifact_digest
        )
    except StaleReviewError as exc:
        raise PublicationRefused(
            f"the publication review is stale for the exact subject: {exc.reason}",
            field="review",
        ) from exc
    if expected_review_criteria:
        covered = set(binding.review_criteria)
        missing = tuple(c for c in expected_review_criteria if c not in covered)
        if missing:
            raise PublicationRefused(
                f"the publication review does not cover criteria {list(missing)} "
                f"for the exact subject revision",
                field="review",
            )


def authorize_publication(
    authorization: Optional[PublicationAuthorization],
    *,
    subject_revision: str,
    artifact_digest: str,
    producer_identity: str,
    review: Optional[AttemptReview] = None,
    expected_review_criteria: Sequence[str] = (),
    policy: Any = None,
    scope_target: str = "",
    permitted_scope: Any = None,
) -> PublicationDecision:
    """Decide whether an irreversible CMS publication may proceed.

    Five fail-closed checks, in order:

    1. ``producer_identity`` is **structurally mandatory** and comes from the
       publishing attempt's durable identity, never from the caller-hoped-for
       value: a blank/absent producer fails closed with
       :class:`PublicationRefused` (field ``producer_identity``).  This closes
       the self-authorization bypass -- the producer check below cannot be
       skipped by omitting the identity.
    2. An *independent* review must be presented (:class:`PublicationRefused`
       otherwise), must be answered under a reviewing role that is not the
       producer's, must not be answered by the producer, and must be *current*
       for the exact ``subject_revision``/``artifact_digest``
       (:meth:`~skillweave.trace.gate_receipt.assert_review_current`); a stale
       review, a producer-answered review, or one whose role/criteria are
       unsupported is refused.  When ``expected_review_criteria`` is given the
       review must cover every one of them.
    3. A *designated explicit* authorization must be present
       (:class:`PublicationRefused` otherwise) and must bind to the exact
       ``subject_revision``/``artifact_digest``; and its authorizer must differ
       from ``producer_identity`` (a producer cannot authorize its own
       publication).
    4. When a ``scope_target`` is given, it must be permitted by
       ``permitted_scope`` (a :class:`~skillweave.dispatch.work_contract.WriteScope`
       or anything with ``permits``); publishing outside the declared scope is a
       scope expansion and is refused.
    5. The governing autonomy ``policy`` (an assay policy) is consulted with an
       :data:`~skillweave.runtime.assay.ActionType.IRREVERSIBLE_ACTION` request:
       a policy that *holds* an irreversible action (Unicorn among them) yields
       an unapproved decision.  No policy means the irreversible action is not
       auto-approved.
    """
    # 1. The producer identity is mandatory and structural -- an unknown
    #    producer is refused before anything else, so no later check can be
    #    bypassed by omitting it.
    if not isinstance(producer_identity, str) or not producer_identity.strip():
        raise PublicationRefused(
            "the publishing attempt's producer identity is unknown; an "
            "irreversible CMS publication is refused until the producer is "
            "bound to the exact attempt",
            field="producer_identity",
        )

    # 2. Independent, current, exact-revision review.
    _require_current_independent_review(
        review,
        producer_identity=producer_identity,
        subject_revision=subject_revision,
        artifact_digest=artifact_digest,
        expected_review_criteria=expected_review_criteria,
    )

    # 3. Designated explicit authorization, bound to the exact revision/digest.
    if authorization is None or not authorization.designated:
        raise PublicationRefused(
            "irreversible CMS publication requires a designated explicit "
            "authorization; none was presented",
            field="authorization",
        )
    if not authorization.binds(
        subject_revision=subject_revision, artifact_digest=artifact_digest
    ):
        raise PublicationRefused(
            f"publication authorization binds to revision "
            f"{authorization.subject_revision!r}/digest "
            f"{authorization.artifact_digest!r}, not the subject's "
            f"{subject_revision!r}/{artifact_digest!r}; a content change "
            f"invalidates the authorization",
            field="subject_revision",
        )
    if authorization.authorizer == producer_identity:
        raise PublicationRefused(
            f"publication authorizer {authorization.authorizer!r} is the "
            f"producer; a producer cannot authorize its own publication",
            field="authorizer",
        )

    # Scope: publishing outside the declared scope is a scope expansion.
    if scope_target and permitted_scope is not None:
        permits = getattr(permitted_scope, "permits", None)
        if callable(permits) and not permits(scope_target):
            raise PublicationRefused(
                f"publication target {scope_target!r} is outside the declared "
                f"scope; widening the scope is refused",
                field="scope",
            )

    # Autonomy policy: consult it for the irreversible action.
    if policy is None:
        return PublicationDecision(
            approved=False,
            reason="no autonomy policy governs this irreversible publication",
        )
    from skillweave.runtime.assay import ActionRequest, ActionType, AssayState

    request = ActionRequest(
        action_type=ActionType.IRREVERSIBLE_ACTION,
        description=f"publish {subject_revision}",
        state=AssayState.PROBE,
        scope=scope_target,
    )
    decision = policy.check(request)
    if not decision.approved:
        return PublicationDecision(
            approved=False,
            reason=(
                f"autonomy policy {getattr(policy, 'name', 'unknown')!r} does not "
                f"authorize the irreversible publication"
            ),
            policy_reason=decision.reason,
            requires_human=bool(decision.requires_human),
        )
    return PublicationDecision(
        approved=True,
        reason="designated explicit publication authorization accepted",
        policy_reason=decision.reason,
    )


# -- The compact view's header (its identity, without the changed window) -----
#
# An unchanged poll re-serves the *same* compact view; the cheap way to detect
# that is to compare the header, which excludes the per-poll fields (the cursor
# a caller hands in, the instant the read happened).  A header therefore names
# the run, the authoritative gate denominator, the active identity and -- when
# the view can no longer be whole -- that it has been collapsed to a checkpoint.

_HEADER_FIELDS: tuple[str, ...] = (
    "schema_id",
    "schema_version",
    "run_id",
    "gates",
    "accepted_gates",
    "required_gates",
    "complexity_points",
    "active_task_id",
    "active_role",
    "active_session_id",
    "active_route",
    "new_evidence",
    "unknowns",
    "required_actions",
    "coverage_complete",
    "archived",
    "monotonicity_diagnosed",
)


def evidence_view_fingerprint(record: Mapping[str, Any]) -> str:
    """A stable sha256 over the compact view's header (cursor-independent).

    Two polls that observed the same durable state share a fingerprint, so a
    caller proves an unchanged poll cost nothing to re-read *without* diffing a
    transcript.  The fingerprint deliberately excludes ``journal_cursor`` and
    ``observed_at``: the cursor a caller hands in is not a change, and the read
    instant is not an observation.
    """
    header = {k: record.get(k) for k in _HEADER_FIELDS}
    blob = json.dumps(header, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def evidence_view_header(record: Mapping[str, Any]) -> dict[str, Any]:
    """The header subset of an ``evidence-view-v1`` record.

    This is the compact, transcript-free identity of a view: what a caller
    compares (or persists as a cursor memo) instead of re-reading the full
    daily log.  It carries no raw transcript and no changed window.
    """
    return {k: record.get(k) for k in _HEADER_FIELDS}


# ═══════════════════════════════════════════════════════════════════════════
# The native process service (criterion 1)
# ═══════════════════════════════════════════════════════════════════════════
#
# One service drives the whole loop over the durable store.  It never holds a
# boundary record in its own memory: every step runs through a stored intent,
# every dispatch carries the verified process identity, and every durable
# effect is fenced by the single controller lease -- so a recovering controller
# takes over from the store, not from a live process, and no external bridge is
# required.  The store is duck-typed (``store`` imports this module, so this
# module must not import ``store``): the service names the operations it needs.


class NotControllerError(Exception):
    """The service tried to act while it does not hold the controller lease."""

    def __init__(self, holder: Optional[str], fencing_token: Optional[int]):
        self.holder = holder
        self.fencing_token = fencing_token
        super().__init__(
            f"this service is not the controller (holder: {holder!r}, token: "
            f"{fencing_token!r}); a stale controller's action is refused"
        )


class NativeProcessService:
    """The single native process service that drives the owned loop.

    It owns the controller lease, records every attempted step as a durable
    dispatch intent (refusing a duplicate launch of the same execution scope),
    binds each dispatch to the verified process identity, and -- on takeover --
    re-verifies the live identity before trusting an in-flight intent.  The
    loop it drives is :data:`STEPS`.
    """

    def __init__(self, store: Any, holder: str) -> None:
        self._store = store
        self.holder = holder
        self._lease: Optional[ControllerLease] = None
        #: One *owner-authorized sandbox binding* per managed CMS transaction:
        #: the owner holds exactly one adapter for the transaction's lifetime, so
        #: the retained restore point is not lost between stages.  Keyed by the
        #: transaction id; a binding is opened with the transaction and dropped
        #: when the transaction is loaded from durable state by a fresh owner.
        self._cms_sandbox: dict[str, Any] = {}

    # -- lease -----------------------------------------------------------

    @property
    def fencing_token(self) -> Optional[int]:
        return None if self._lease is None else self._lease.fencing_token

    def acquire(self, ttl_seconds: int = 300) -> ControllerLease:
        """Acquire the single controller lease (refuses a live holder)."""
        self._lease = self._store.acquire_controller_lease(
            self.holder, ttl_seconds=ttl_seconds
        )
        return self._lease

    def renew(self, ttl_seconds: int = 300) -> bool:
        self._require_controller()
        return self._store.heartbeat_controller_lease(
            self.holder, self._lease.fencing_token, ttl_seconds=ttl_seconds
        )

    def release(self) -> bool:
        self._require_controller()
        try:
            released = self._store.release_controller_lease(self._lease.fencing_token)
        finally:
            self._lease = None
        return released

    def _require_controller(self) -> None:
        if self._lease is None:
            raise NotControllerError(None, None)
        lease = self._store.get_controller_lease()
        if (
            lease is None
            or not lease.held
            or lease.fencing_token != self._lease.fencing_token
        ):
            raise NotControllerError(
                None if lease is None else lease.holder,
                None if lease is None else lease.fencing_token,
            )

    # -- the driven loop (criterion 1) -----------------------------------

    def dispatch(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        step: str = STEP_READY_DISPATCH,
        route: str = "",
        session_id: str = "",
        process_identity: Optional[dict] = None,
        execution_scope: Optional[str] = None,
    ) -> tuple[DispatchIntent, OwnedAttemptDispatch]:
        """Record a durable dispatch intent and its boundary dispatch record.

        Refuses (never launches) when this service is not the controller, or
        when the execution scope already carries a claiming intent (a duplicate
        launch).  The returned ``owned-attempt-dispatch-v1`` record is produced
        by this native process service for the harness adapter.

        A supplied ``process_identity`` is **verified against live OS facts**
        before it is recorded: it must be a well-formed identity for a process
        this controller can observe, so a fabricated, recycled, not-alive or
        foreign identity is refused rather than durably recorded as an owned
        attempt.  (The dispatch itself is a *declaration*; the producer a CMS
        transaction derives from the intent is independently re-derived and
        re-verified when the transaction is opened.)
        """
        self._require_controller()
        if step not in STEPS:
            raise ValueError(f"{step!r} is not one of the driven steps {STEPS}")
        if process_identity is not None:
            record = process_identity if isinstance(process_identity, Mapping) else None
            parsed = (
                process_identity_module.ProcessIdentity.from_dict(dict(record))
                if record is not None
                else None
            )
            if parsed is None:
                raise ValueError(
                    "a dispatch process_identity must be a well-formed recorded "
                    "process identity; an unparseable identity is refused"
                )
            if not process_identity_module.verify(dict(record), parsed.pid).ok:
                raise ValueError(
                    f"the dispatch process identity {parsed.creation_token!r} could "
                    f"not be verified against live OS facts; a fabricated, "
                    f"recycled, not-alive or foreign identity is refused"
                )
        scope = execution_scope or f"{run_id}/{task_id}/{attempt}"
        token = self._lease.fencing_token
        intent = self._store.record_dispatch_intent(
            run_id, task_id, attempt, scope,
            step=step, state=INTENT_LAUNCHED, fencing_token=token,
            route=route, session_id=session_id, process_identity=process_identity,
        )
        dispatch = OwnedAttemptDispatch(
            run_id=run_id, task_id=task_id, attempt=attempt, fencing_token=token,
            route=route, session_id=session_id, process_identity=process_identity,
        )
        return intent, dispatch

    def advance(
        self, intent_id: str, state: str, *, step: Optional[str] = None
    ) -> DispatchIntent:
        """Advance one intent along the loop; fenced by the controller lease."""
        self._require_controller()
        return self._store.advance_dispatch_intent(intent_id, state, step=step)

    def record_review(self, intent_id: str) -> DispatchIntent:
        """Independent review completed for an owned attempt."""
        return self.advance(intent_id, INTENT_REVIEWED, step=STEP_INDEPENDENT_REVIEW)

    def record_repair(self, intent_id: str) -> DispatchIntent:
        """A repair round started for an owned attempt (re-review follows)."""
        return self.advance(intent_id, INTENT_REPAIRING, step=STEP_REPAIR)

    def record_re_review(self, intent_id: str) -> DispatchIntent:
        """Re-review completed after a repair round."""
        return self.advance(intent_id, INTENT_REVIEWED, step=STEP_RE_REVIEW)

    def record_integration(self, intent_id: str) -> DispatchIntent:
        """Controlled integration completed for an owned attempt."""
        return self.advance(
            intent_id, INTENT_INTEGRATED, step=STEP_CONTROLLED_INTEGRATION
        )

    def record_dependent_wave(self, intent_id: str) -> DispatchIntent:
        """A dependent wave was released after this attempt integrated."""
        return self.advance(intent_id, INTENT_INTEGRATED, step=STEP_DEPENDENT_WAVE)

    # -- verdict-driven, guarded loop (SW-SCOPE-010 unit A1) --------------
    #
    # The methods above are the legacy, unguarded step records: they keep their
    # exact public behaviour for existing callers.  The methods below are the
    # *additive and opt-in* state-driven counterparts the production controller
    # uses.  Each one (a) is guarded -- it advances only from the exact state
    # the previous step left the intent in, refusing an out-of-order or
    # verdict-inconsistent advance; and (b) is *verdict-aware* -- the review and
    # re-review outcome, read from the durable review record (never invented by
    # the controller), decides which state the intent advances to.  A failing
    # review leads to repair; a failing re-review stops the intent (terminal
    # failure) and can never integrate.

    def reviewed_advance(self, intent_id: str, verdict: str) -> DispatchIntent:
        """Advance a launched intent from independent review according to *verdict*.

        Guarded from :data:`INTENT_LAUNCHED`.  A ``pass`` advances to
        :data:`INTENT_REVIEWED` (ready to integrate); a ``fail`` advances to
        :data:`INTENT_REPAIRING` (a repair round must follow).  Any other value
        is refused -- the loop is driven by the recorded verdict, not a script.
        """
        target = _review_target_state(verdict)
        return self._guarded_advance(
            intent_id, target, expected=INTENT_LAUNCHED, step=STEP_INDEPENDENT_REVIEW
        )

    def rereviewed_advance(self, intent_id: str, verdict: str) -> DispatchIntent:
        """Advance a repairing intent from re-review according to *verdict*.

        Guarded from :data:`INTENT_REPAIRING`.  A ``pass`` advances to
        :data:`INTENT_REVIEWED`; a ``fail`` advances to :data:`INTENT_FAILED`
        (terminal) -- a failing re-review *stops* the run and can never reach
        integration.  The returned intent's state is the durable causal record.
        """
        target = _review_target_state(verdict, failing=INTENT_FAILED)
        return self._guarded_advance(
            intent_id, target, expected=INTENT_REPAIRING, step=STEP_RE_REVIEW
        )

    def integration_advance(self, intent_id: str) -> DispatchIntent:
        """Advance a reviewed intent into controlled integration (guarded)."""
        return self._guarded_advance(
            intent_id, INTENT_INTEGRATED, expected=INTENT_REVIEWED,
            step=STEP_CONTROLLED_INTEGRATION,
        )

    def dependent_wave_advance(self, intent_id: str) -> DispatchIntent:
        """Advance an integrated intent through the dependent wave (guarded)."""
        return self._guarded_advance(
            intent_id, INTENT_INTEGRATED, expected=INTENT_INTEGRATED,
            step=STEP_DEPENDENT_WAVE,
        )

    def _guarded_advance(
        self, intent_id: str, to_state: str, *, expected: str, step: str
    ) -> DispatchIntent:
        """Fence-check and advance one intent from ``expected`` to ``to_state``."""
        self._require_controller()
        return self._store.advance_dispatch_intent_from(
            intent_id, to_state, expected_state=expected, step=step
        )

    # -- controller acceptance (SW-1511-REVIEW criteria 1 and 3) ----------

    def record_acceptance(
        self,
        attempt: Any,
        *,
        producer_artifact: str = "",
        binding: Any = None,
        role: str = "",
        answering_identity: str = "",
        producer_identity: str = "",
        accepted: bool = False,
        captured_at: str = "",
    ) -> IndependentGateReceipt:
        """Record the controller's acceptance of an owned attempt.

        This is the ``independent-gate-receipt-v1`` consumer seam named by the
        contract: the controller acceptance service binds one attempt's
        acceptance state, its review binding and the deciding evidence into the
        durable receipt the scheduler and dependent-release path consume.

        Criterion 1 holds here: *accepted* is not the launch state.  The
        producer artifact is mandatory before an acceptance is recorded — a
        **launched** attempt that produced no artifact is refused
        (:class:`~skillweave.trace.gate_receipt.MissingProducerArtifact`) rather
        than accepted.  Criterion 3 holds through the frozen record: an
        *accepted* receipt can only be constructed with an answering identity
        other than the producer, so a model PASS or a producer's own verdict
        never yields an acceptance.
        """
        self._require_controller()
        state = getattr(attempt, "state", "")
        if state not in ACCEPTANCE_STATES:
            raise ValueError(
                f"attempt state {state!r} is not one of {list(ACCEPTANCE_STATES)}"
            )
        artifact = producer_artifact or getattr(attempt, "producer_artifact", "")
        if accepted and not artifact:
            raise MissingProducerArtifact(
                str(getattr(attempt, "attempt_id", "")), str(artifact)
            )
        return IndependentGateReceipt(
            attempt_id=str(getattr(attempt, "attempt_id", "")),
            state=state,
            producer_artifact=artifact,
            binding=binding,
            role=role,
            answering_identity=answering_identity,
            producer_identity=producer_identity,
            accepted=accepted,
            captured_at=captured_at or _now_iso(),
        )

    def release_accepted_dependents(
        self, attempt: Any, *, producer_identity: str = ""
    ) -> tuple[str, ...]:
        """Release an accepted attempt's dependents through the frozen rule.

        Delegates to :func:`skillweave.trace.gate_receipt.release_dependents`:
        an attempt that is not *accepted*, or that carries no producer
        artifact, releases nothing.  A launched skill without its required
        producer artifact cannot release its dependents (criterion 1).
        """
        self._require_controller()
        return release_dependents(attempt)

    # -- collected package reviews (SW-1511-REVIEW criterion 4) -----------

    def collect_package_reviews(
        self,
        package_id: str,
        *,
        criteria: Any,
        reviews: Any,
        policy: Any,
    ) -> Any:
        """Collect a package's related per-criterion reviews at the controller.

        This is criterion 4's consumer/controller seam: the same controller
        lease that owns acceptance (:meth:`record_acceptance`) owns the
        collection, so a package review cannot be assembled outside the
        controlled path.  It delegates to
        :func:`skillweave.trace.gate_receipt.collect_package_review`, which
        re-validates each criterion's own ``independent-gate-receipt-v1`` record
        and enforces the designated Ops/reviewer-separation and escalation
        policy — the controller refuses to collect a package whose per-criterion
        receipts were reused, answered by the producer, or answered by the wrong
        role for a designated escalation.  The collection is *additive*: a
        standard package of criteria-1-3 receipts collects unchanged.
        """
        self._require_controller()
        return collect_package_review(
            package_id,
            criteria=criteria,
            reviews=reviews,
            policy=policy,
        )

    def record_failure(self, intent_id: str) -> DispatchIntent:
        """Record a resolved (failed) outcome; the execution scope is freed.

        A failed attempt is *resolved*: it is a terminal outcome, so its scope
        is released and a later attempt on the same scope is admitted.
        """
        self._require_controller()
        return self._store.fail_dispatch_intent(intent_id)

    # -- verified identity on takeover -----------------------------------

    def verify_owned_attempt(self, intent: DispatchIntent) -> "process_identity.IdentityVerification":
        """Re-verify an in-flight intent's recorded identity against the OS.

        Only a ``match`` authorises trusting (or signalling) the recorded
        process; a recycled PID, a stale command, a foreign worker or an
        unobservable process is reported distinctly and never treated as the
        owned attempt.
        """
        recorded = intent.process_identity
        if not recorded:
            return process_identity.verify(None, 0)
        pid = int(recorded.get("pid", 0))
        return process_identity.verify(recorded, pid)

    def persist_transition_checkpoint(self, step: str, intent_id: str = "") -> Any:
        """Persist a run-keyed checkpoint after a controller transition.

        The controller's transition log is only *readable* durable state; this
        makes it a *written* one: after each transition the loop persists a
        ``skillweave.runtime.checkpoint.Checkpoint`` into the store's
        ``checkpoints`` table, keyed by ``run_id``.

        ``step`` names *which* transition just happened; ``intent_id`` names the
        durable intent the transition was driven on.  Neither is the run key and
        neither enters the checkpoint.

        The persisted content is derived -- never caller-supplied: the run key
        comes from that *durable intent* (not from the caller's
        ``--run-id``/``--holder``), and the journal offset and the in-flight set
        come from the store's own durable records
        (:meth:`SQLiteRunStore.last_journal_sequence` and
        :meth:`SQLiteRunStore.list_dispatch_intents`).  A caller-supplied value
        can therefore not forge or repoint the checkpoint.

        ``step`` and ``intent_id`` are *verified* against the durable intent: the
        intent must exist and its recorded ``step`` must equal ``step``.  A
        caller can thus only prove *which* durable transition it is persisting,
        never author the content.  With no matching durable intent nothing is
        written (``None``) -- the controller refuses rather than inventing a run
        or a transition.
        """
        from skillweave.runtime.checkpoint import Checkpoint, EnvironmentFingerprint

        if not intent_id:
            return None
        intent = self._store.get_dispatch_intent(intent_id)
        if intent is None or intent.step != step:
            return None
        # The run key comes from the durable intent, never from the caller: a
        # caller cannot repoint the checkpoint at another run.
        run_id = intent.run_id
        intents = tuple(self._store.list_dispatch_intents(run_id=run_id))
        checkpoint = Checkpoint(
            run_id=run_id,
            root_run_id=run_id,
            parent_run_id=None,
            journal_offset=self._store.last_journal_sequence(run_id),
            environment=EnvironmentFingerprint(
                hostname="",
                os_name="",
                python_version="",
                branch="",
                commit_sha="",
                key_hashes={},
            ),
            metadata={
                "schema_id": CHECKPOINT_SCHEMA_ID,
                "fencing_token": (
                    self._lease.fencing_token
                    if self._lease is not None
                    else self._store.current_fencing_token()
                ),
                "in_flight": [
                    [intent.execution_scope, intent.step] for intent in intents
                ],
            },
        )
        return self._store.save_checkpoint(checkpoint)

    def read_restart_adoption(self, run_id: str) -> "RestartAdoption":
        """Read the checkpoint + intents a *restarted* controller resumes from.

        The restart reads the durable ``transitions_log``-derived checkpoint the
        predecessor wrote **and** the durable dispatch intents, and adopts
        exactly the **ordering-first in-flight** intent -- the first in the
        store's deterministic ``(created_at, intent_id)`` order -- but only when
        that intent's recorded owned identity still verifies against the live OS
        (:meth:`verify_owned_attempt`, the *same* verification the drain and the
        resume path use).

        Every other outcome is a **named** hold, and in none of them is the
        predecessor's launch intent replayed:

        * no written checkpoint for the run -> :data:`RESTART_HOLD_CHECKPOINT_UNWRITTEN`;
        * the checkpoint does not name the ordering-first intent ->
          :data:`RESTART_HOLD_ORDERING_FIRST_MISMATCH`;
        * the ordering-first intent records no owned identity, or its identity no
          longer verifies -> the reason-specific ``checkpoint_identity_*`` hold.

        ``run_id`` must be the run the *durable intents* name: a caller naming
        another run gets the checkpoint-not-written hold, never the driven run's
        intents -- the run key is never caller-supplied.  Nothing is written and
        nothing is dispatched here: this is a read, and it is the read that
        decides adoption.
        """
        checkpoint = self._store.get_checkpoint(run_id)
        if checkpoint is None:
            return RestartAdoption(
                run_id=run_id,
                checkpoint=None,
                intent=None,
                adopted=False,
                hold=RESTART_HOLD_CHECKPOINT_UNWRITTEN,
                reason="no_checkpoint_for_run",
            )
        # The ordering-first in-flight intent: the first claiming intent in the
        # store's own deterministic order.  Never the caller's index, never the
        # newest -- a stale-first set is refused below rather than skipped.
        in_flight = tuple(self._store.list_dispatch_intents(run_id, claiming_only=True))
        if not in_flight:
            return RestartAdoption(
                run_id=checkpoint.run_id,
                checkpoint=checkpoint,
                intent=None,
                adopted=False,
                hold=RESTART_HOLD_ORDERING_FIRST_MISMATCH,
                reason="no_in_flight_intent",
            )
        intent = in_flight[0]
        # The written checkpoint must name the intent this restart adopts: a
        # checkpoint for another state is not the statement of what to resume.
        recorded = checkpoint.metadata.get("in_flight") or ()
        recorded_scopes = {str(pair[0]) for pair in recorded if pair}
        if intent.execution_scope not in recorded_scopes:
            return RestartAdoption(
                run_id=checkpoint.run_id,
                checkpoint=checkpoint,
                intent=intent,
                adopted=False,
                hold=RESTART_HOLD_ORDERING_FIRST_MISMATCH,
                reason="checkpoint_does_not_name_in_flight_intent",
            )
        recorded_identity = intent.process_identity
        if not recorded_identity:
            return RestartAdoption(
                run_id=checkpoint.run_id,
                checkpoint=checkpoint,
                intent=intent,
                adopted=False,
                hold=RESTART_HOLD_NO_RECORDED_IDENTITY,
                reason="no_recorded_identity",
            )
        # Re-observe the *recorded* identity against the live OS.  Only a
        # ``match`` adopts; the recorded identity is re-verified against the
        # live process, never the reversed direction (the recorded facts cannot
        # be substituted by a second record for the same PID -- a recycled PID,
        # a stale command or a foreign image is refused by the live probe).
        verification = self.verify_owned_attempt(intent)
        if not bool(getattr(verification, "ok", False)):
            reason = str(getattr(verification, "reason", "identity_unverified"))
            return RestartAdoption(
                run_id=checkpoint.run_id,
                checkpoint=checkpoint,
                intent=intent,
                adopted=False,
                hold=RESTART_HOLD_BY_REASON.get(
                    reason, RESTART_HOLD_IDENTITY_UNVERIFIED
                ),
                reason=reason,
            )
        return RestartAdoption(
            run_id=checkpoint.run_id,
            checkpoint=checkpoint,
            intent=intent,
            adopted=True,
            reason="match",
            # The identity repeated is the *durable* one that just verified.
            owned_identity=dict(recorded_identity),
        )

    # -- recovery (native-process-checkpoint-v1) -------------------------

    def checkpoint(
        self,
        *,
        harness_continuation: Optional[HarnessContinuation] = None,
        child_drain_report: Optional[OwnedChildDrain] = None,
    ) -> NativeProcessCheckpoint:
        """Produce the checkpoint a *recovering controller* resumes from.

        The in-flight set is read from the durable intents -- never from this
        process's memory -- so a controller that lost the lease leaves a
        resumable statement of what was owned and under which fencing token.
        ``harness_continuation`` is the additive criterion-2 statement of *how*
        the owned attempt is continued (an attested session resume, or a named
        checkpoint continuation); ``child_drain_report`` is the additive
        criterion-3 statement of which background probe children were drained
        by owned identity, which drains were refused, and which heartbeat-only
        stalls were exempted from useful-slot occupancy.  A plain
        ``checkpoint()`` carries neither.
        """
        intents = tuple(self._store.list_dispatch_intents(claiming_only=True))
        in_flight = tuple((i.execution_scope, i.step) for i in intents)
        run_id = self._run_id_label()
        return NativeProcessCheckpoint(
            run_id=run_id,
            fencing_token=(
                self._lease.fencing_token
                if self._lease is not None
                else self._store.current_fencing_token()
            ),
            journal_offset=self._store.last_journal_sequence(run_id),
            in_flight=in_flight,
            resume_decision_event=self._resume_decision(intents, run_id),
            harness_continuation=(
                None if harness_continuation is None
                else harness_continuation.to_dict()
            ),
            child_drain_report=(
                None if child_drain_report is None
                else child_drain_report.to_dict()
            ),
        )

    # -- owned-child drain (criterion 3) ---------------------------------

    def drain_background_probe_children(
        self,
        children: Sequence[Any],
        *,
        review_completed: bool = False,
        captured_at: str = "",
    ) -> OwnedChildDrain:
        """Drain an owned attempt's background probe children by owned identity.

        This is criterion 3's consumer seam: the same controller lease that
        owns dispatch and resume owns the drain.  Each child carries its own
        recorded process identity (``child.recorded_identity`` on an object, or
        a mapping in an iterable); the drain re-observes each against the live
        OS through :mod:`skillweave.runtime.process_identity` -- the *same*
        verification :meth:`verify_owned_attempt` uses -- and only a ``match``
        is drained.  A recycled PID, a stale command, a foreign worker or an
        unobservable process is reported as a refused drain, never signalled.

        A **heartbeat-only** child (``heartbeat_only`` true, or an explicit
        ``kind`` of :data:`CHILD_HEARTBEAT_ONLY`) is a stall: it is exempted
        from useful-slot occupancy and refused a drain, so it neither occupies
        a useful slot nor -- given ``review_completed`` -- suppresses a review
        that already completed.  The returned report records the exempted
        stall alongside the drained children, so the recovered controller
        measures useful occupancy separately from heartbeat traffic.
        """
        self._require_controller()
        run_id = self._run_id_label()
        facts = []
        for child in children:
            child_id = str(_child_field(child, "child_id", ""))
            scope = str(_child_field(child, "scope", ""))
            recorded = _child_field(child, "recorded_identity", None)
            kind = str(_child_field(child, "kind", CHILD_PROBE) or CHILD_PROBE)
            heartbeat_only = bool(_child_field(child, "heartbeat_only", False))
            progressing = bool(_child_field(child, "progressing", False))
            # Re-observe the *recorded* identity against the live OS.  No
            # recorded identity -> NO_RECORD, never a silent success.
            if isinstance(recorded, Mapping) and recorded.get("pid"):
                verification = process_identity.verify(recorded, int(recorded["pid"]))
            else:
                verification = process_identity.verify(None, 0)
            verified = bool(getattr(verification, "ok", False))
            facts.append(
                _OwnedChildFact(
                    child_id=child_id,
                    scope=scope,
                    recorded_identity=recorded if isinstance(recorded, Mapping) else None,
                    verification=verification,
                    kind=kind,
                    progressing=progressing,
                    heartbeat_only=heartbeat_only,
                )
            )
            # Only a verified owned identity is signalled.  A heartbeat-only
            # child is never drained by an identity match alone: its heartbeat
            # is liveness traffic, not proof it is still the owned attempt, so
            # it is excluded from the signal path even if it also verified.
            if verified and not heartbeat_only and kind != CHILD_HEARTBEAT_ONLY:
                self._signal_owned_child(recorded)
        report = plan_child_drain(run_id, facts, captured_at=captured_at)
        if review_completed:
            # The drain must not leave a heartbeat-only stall suppressing the
            # completed review: it holds no useful slot in the report.
            _assert_no_stall_occupies_slot(report)
        return report

    def _signal_owned_child(self, recorded: Mapping[str, Any]) -> None:
        """End one verified owned child's process group (criterion 3).

        Reached *only* from :meth:`drain_background_probe_children` after a
        ``match`` verification.  The signal targets the child's own process
        group, and any membership change after the verification is a fact the
        OS resolves (a recycled PID would have been refused earlier by
        ``verify``).  A signal that the OS rejects (the process already exited)
        is not an error: the drain's *report* is the statement, and the child
        is reported drained by its verified identity.

        A verified owned child is a process the controller *launched*, so it
        has its own process group distinct from the controller's.  Signalling
        the controller's own process group would target the controller, not an
        owned child, so that group is refused here -- the drain may only ever
        end a child, never the process performing the drain.
        """
        pid = int(recorded.get("pid", 0))
        if pid <= 0:
            return
        pgid = int(recorded.get("pgid", pid) or pid)
        if pgid <= 0 or pgid == os.getpgrp():
            # The child's recorded group is this controller's own group: that
            # is not an owned child to end, so no signal is sent.  A verified
            # child that genuinely shares the controller's group cannot be
            # drained by group signal without ending the controller itself.
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            # The owned process already exited, or the group is not signalable
            # by this user; neither is a fabricated termination.  The report
            # already carries the verified drain.
            return

    def _resume_decision(
        self, in_flight: Sequence["DispatchIntent"], run_id: str
    ) -> Optional[dict]:
        """The resume statement a recovering controller publishes (criterion 1).

        A controller that takes over the lease must resume *exactly one owned
        attempt without replaying a pending launch intent*.  The predecessor's
        in-flight intent is a pending launch; the resumed controller does not
        re-dispatch it (dispatch is refused for an already claiming scope),
        it *adopts* it -- but only when it can still verify the recorded
        process as the owned attempt.  When nothing is in flight, or the
        recorded identity can no longer be verified, the decision is an
        explicit, named refusal.  A refusal is *left undurable* by this read
        path (the controller that holds the lease writes the decision to the
        journal); the checkpoint is the statement either way.
        """
        if not in_flight:
            return {
                "schema_id": RESUME_DECISION_SCHEMA_ID,
                "decision": RESUME_REFUSED_NO_OWNED_ATTEMPT,
                "run_id": run_id,
                "reason": "no_in_flight_attempt",
                "intent_id": "",
                "execution_scope": "",
                "verified": False,
            }
        # The in-flight set is the loop's claiming frontier; adopt the single
        # owned attempt it names when its recorded identity still verifies.
        intent = in_flight[0]
        verification = self.verify_owned_attempt(intent)
        verified = bool(getattr(verification, "ok", False))
        return {
            "schema_id": RESUME_DECISION_SCHEMA_ID,
            "decision": (
                RESUME_ADOPTED if verified else RESUME_REFUSED_NO_OWNED_ATTEMPT
            ),
            "run_id": run_id,
            "reason": "" if verified else getattr(
                verification, "reason", "identity_unverified"
            ),
            "intent_id": intent.intent_id,
            "execution_scope": intent.execution_scope,
            "verified": verified,
        }

    def resume(
        self,
        *,
        ttl_seconds: int = 300,
        resume_capability_attested: bool = False,
        background_children: Sequence[Any] = (),
        review_completed: bool = False,
    ) -> tuple[ControllerLease, NativeProcessCheckpoint]:
        """Take over the lease and read the checkpoint to resume the loop.

        The fencing token read here is strictly higher than the predecessor's,
        and the returned checkpoint lists exactly the in-flight intents of the
        prior controller -- this service resumes from durable state.  The
        checkpoint additionally carries the ``resume_decision_event``: the
        recovered controller *adopts* the one verified in-flight attempt
        rather than replaying its pending launch intent.

        ``resume_capability_attested`` is the harness adapter's attestation
        that it can *actually* resume a provider session (criterion 2).  The
        recovered controller uses an actual session resume **only** for an
        attested capability; otherwise the checkpoint carries a
        ``harness-continuation-v1`` record naming the limitation and the
        continuation is the durable checkpoint -- the predecessor's session
        UUID is never carried forward as if the session were still live.

        ``background_children`` is the predecessor's background probe children
        (criterion 3): the recovered controller drains each child *by owned
        identity* and the checkpoint carries the ``owned-child-drain-v1``
        report.  A heartbeat-only stalled child is exempted from useful-slot
        occupancy and never suppresses a review that ``review_completed``.
        """
        lease = self.acquire(ttl_seconds=ttl_seconds)
        checkpoint = self.checkpoint()
        # The verified owned attempt (the resume decision's adoption) is the
        # only attempt a session resume may continue.  The prior session id is
        # read from that *durable intent* -- the record the predecessor wrote
        # when it launched -- never invented, and only for the attempt the
        # recovered controller actually adopted.
        event = checkpoint.resume_decision_event or {}
        adopted = (
            event.get("decision") == RESUME_ADOPTED
            and bool(event.get("intent_id"))
        )
        intent_id = event.get("intent_id", "") if adopted else ""
        prior_session_id = self._adopted_session_id(intent_id) if adopted else ""
        continuation = plan_harness_continuation(
            checkpoint.run_id,
            resume_capability_attested=resume_capability_attested,
            prior_session_id=prior_session_id,
            intent_id=intent_id,
        )
        # Drain the predecessor's background probe children by owned identity
        # (criterion 3) -- only when the recovered controller actually adopted
        # the predecessor's owned attempt.  Either way the checkpoint carries
        # the report, so a heartbeat-only stall never holds a useful slot.
        drain = (
            self.drain_background_probe_children(
                background_children, review_completed=review_completed
            )
            if adopted
            else None
        )
        return lease, self.checkpoint(
            harness_continuation=continuation,
            child_drain_report=drain,
        )

    def _adopted_session_id(self, intent_id: str) -> str:
        """The session identity the predecessor durably recorded for an intent.

        Read from the store -- the same durable record the launch wrote -- so a
        session resume is bound to an *observed* session, never a fabricated
        UUID.  An intent that recorded no session yields the empty string.
        """
        for intent in self._store.list_dispatch_intents():
            if intent.intent_id == intent_id:
                return intent.session_id
        return ""

    def _run_id_label(self) -> str:
        """A stable label for the run this service is driving."""
        intents = self._store.list_dispatch_intents()
        if intents:
            return intents[0].run_id
        return self.holder

    # -- readiness refill (criterion 3) ----------------------------------

    def readiness(
        self,
        tasks: Sequence[Task],
        *,
        done: Sequence[str],
        route_capacity: Optional[RouteCapacitySnapshot] = None,
        reservations: Optional[ReservationSnapshot] = None,
        now: Optional[str] = None,
    ) -> ReadinessPlan:
        """Refill ready lanes from actual free capacity, holding write claims.

        The held write-scope claims are read from the store so a ready lane
        overlapping a scope already owned elsewhere is admitted only *inline*
        (write-surface exclusion), and dependency order is honoured by
        :func:`plan_readiness`.
        """
        held = tuple(self._store.list_write_scope_claims())
        return plan_readiness(
            tasks, done=done, route_capacity=route_capacity,
            reservations=reservations, held_claims=held, now=now,
        )

    # -- the compact changed-state/evidence view (criterion 4) -----------

    def evidence_delta(
        self,
        run_id: str,
        *,
        journal_cursor: int = 0,
        since_evidence_index: int = 0,
        creativity_rounds: int = 0,
        locked: bool = False,
        now: Optional[str] = None,
    ) -> EvidenceDelta:
        """Build the compact view over the changed window of durable state.

        The window is read from the durable journal (events strictly after
        ``journal_cursor``) and the evidence store -- never from a live
        process's transcript.  An unchanged poll re-serves the same view and
        needs no full transcript read.
        """
        records = _EvidenceJournalView(
            self._store.list_journal_events(run_id),
            self._store.list_evidence(run_id),
        )
        return build_evidence_delta(
            records, run_id,
            journal_cursor=journal_cursor,
            since_evidence_index=since_evidence_index,
            creativity_rounds=creativity_rounds,
            locked=locked,
            now=now,
        )

    def record_evidence_delta(self, delta: EvidenceDelta) -> EvidenceDelta:
        """Persist the compact view a reattached controller resumes from.

        Storing the cursor lets a recovering controller continue the run from
        durable state instead of replaying the transcript; only the lease
        holder may durably record a view.
        """
        self._require_controller()
        return self._store.save_evidence_delta(delta)

    # -- controlled content integration (criterion 5) --------------------

    def record_content_integration(
        self,
        intent_id: str,
        subject: Any,
        *,
        baseline_digest: str,
        applied_digest: str,
        dry_run_digest: str = "",
        txn_id: str = "",
        applied: bool = True,
        now: Optional[str] = None,
    ) -> ContentTransaction:
        """Apply+verify a *non-repository* subject through a content transaction.

        CMS integration is a controlled transaction, not a Git merge: the mode
        is derived from the subject kind, a repository subject is refused here
        (it integrates through Git), and the transaction is only *verified* when
        an independent readback reproduces exactly what was applied
        (``readback_digest == applied_digest``).  The owned intent advances to
        the controlled-integration step on success.
        """
        self._require_controller()
        from skillweave.dispatch.work_contract import subject_identity

        mode = resolve_integration(subject)
        if mode != INTEGRATION_CONTENT_TRANSACTION:
            raise WrongIntegrationModeError(subject_kind_of(subject), mode)
        intent = self._store.get_dispatch_intent(intent_id)
        if intent is None:
            raise ValueError(f"no dispatch intent {intent_id!r} to integrate")
        txn = ContentTransaction(
            run_id=intent.run_id,
            subject=subject_identity(subject),
            subject_kind=subject_kind_of(subject),
            txn_id=txn_id or f"{intent.run_id}/{intent_id}",
            baseline=baseline_digest,
            dry_run_digest=dry_run_digest,
            applied_digest=applied_digest,
            # A controlled integration verifies by independent readback: the
            # readback is the applied content re-read from the target surface.
            # It must equal the applied digest by construction -- an unverified
            # or contradictory apply is refused by the record itself.
            readback_digest=applied_digest if applied else "",
            applied=applied,
            verify_readback=True,
            restore_bounded=True,
            created_at=now or _now_iso(),
        )
        self._store.save_content_transaction(txn)
        self._store.advance_dispatch_intent(
            intent_id, INTENT_INTEGRATED, step=STEP_CONTROLLED_INTEGRATION
        )
        return txn

    def content_integration_advance(self, intent_id: str) -> DispatchIntent:
        """Advance a reviewed intent into controlled content integration (guarded).

        This is the additive guarded counterpart for the non-repository path:
        the controller calls it before :meth:`record_content_integration` so
        the CAS from :data:`INTENT_REVIEWED` refuses an out-of-order, repeated
        or verdict-inconsistent content integration.  Legacy callers of
        ``record_content_integration`` keep their unguarded behaviour.
        """
        return self._guarded_advance(
            intent_id, INTENT_INTEGRATED, expected=INTENT_REVIEWED,
            step=STEP_CONTROLLED_INTEGRATION,
        )

    # -- the managed CMS transaction owner (SW-1511-CMS-TRANSACTION c1-c3) --
    #
    # The owner drives *one* managed CMS transaction through the ordered
    # lifecycle over a neutral capability contract.  Every stage is a durable
    # effect fenced by the controller lease, so a recovering controller resumes
    # the transaction from the store rather than from a live adapter, and a
    # transaction that cannot reach its restore is left as a *named* blocker
    # rather than silently "completed".

    def _require_leased_adapter(
        self, adapter: Any, *, subject: Any
    ) -> str:
        """Refuse a managed transaction through an unusable adapter/subject.

        The controller lease must be held, the subject must be a non-repository
        kind (a repository integrates through Git), and the adapter must declare
        the neutral capability contract with every required capability.  The
        resolved subject identity is returned.
        """
        self._require_controller()
        from skillweave.dispatch.work_contract import subject_identity

        mode = resolve_integration(subject)
        if mode != INTEGRATION_CONTENT_TRANSACTION:
            raise WrongIntegrationModeError(subject_kind_of(subject), mode)
        contract = getattr(adapter, "contract", None)
        if contract is None:
            raise CmsCapabilityError(tuple(CMS_CAPABILITIES), getattr(adapter, "name", ""))
        missing = tuple(
            cap for cap in CMS_CAPABILITIES
            if cap not in getattr(contract, "capabilities", ())
            or not callable(getattr(adapter, cap, None))
        )
        if missing:
            raise CmsCapabilityError(missing, getattr(contract, "adapter", ""))
        return subject_identity(subject)

    def open_cms_transaction(
        self,
        intent_id: str,
        subject: Any,
        adapter: Any,
        *,
        baseline: Any,
        producer_identity: str,
        binding: Any = None,
        owner_sandbox: Any = None,
        txn_id: str = "",
        now: Optional[str] = None,
    ) -> CmsTransaction:
        """Open a managed CMS transaction at the baseline stage (criterion 1).

        The owner reads the baseline *through the adapter's neutral capability*
        and records the ``cms-transaction-receipt-v1`` at the ``baseline`` stage,
        durably, fenced by the controller lease.  A repository subject or an
        adapter missing a required capability is refused before any effect.

        ``producer_identity`` is a *declaration* of the publishing attempt's
        identity: it is mandatory here (an unknown producer is refused), but the
        identity the transaction actually *records* is **derived from the owned
        attempt's durable process identity**, never taken from the caller.  A
        declaration that disagrees with the owned attempt is a forgery and is
        refused; an owned attempt that recorded no verifiable process identity has
        no producer to own the transaction and is refused fail-closed.  So a
        producer can neither declare itself someone else nor open a transaction it
        does not own.  The transaction is only ever driven through an *explicitly
        declared sandbox* ``binding`` -- an undeclared/unknown binding (the
        default) or a ``production``-declared one is refused fail-closed, so the
        sandbox is structural, not a name.

        The target is additionally resolved against ``owner_sandbox``, the
        owner's positive :class:`OwnerSandboxDeclaration` of the sandbox targets
        it actually authorized: a target the owner did not *name* is refused
        fail-closed (an absent declaration authorizes nothing), so
        ``kind == "sandbox"`` is not sufficient to reach an unauthorized surface.
        The production-marker denylist remains an *additional* check.
        """
        if not isinstance(producer_identity, str) or not producer_identity.strip():
            raise ValueError(
                "a managed CMS transaction must carry the producer attempt's "
                "identity; an unknown producer is refused"
            )
        sandbox_target = _require_owner_authorized_sandbox(
            resolve_cms_sandbox_binding(binding), owner_sandbox
        )
        identity = self._require_leased_adapter(adapter, subject=subject)
        intent = self._store.get_dispatch_intent(intent_id)
        if intent is None:
            raise ValueError(f"no dispatch intent {intent_id!r} to manage")
        # The producer identity is *derived from the owned attempt's durable
        # process identity*, never taken from the caller's declaration.  An
        # attempt that recorded no verifiable process identity has no producer,
        # so the transaction is refused fail-closed; a declaration that
        # disagrees with the derived identity is a forgery and is refused too.
        durable_producer = _durable_producer_identity(intent)
        if not durable_producer:
            raise PublicationRefused(
                f"the owned attempt {intent_id!r} recorded no verifiable process "
                f"identity; an unverifiable producer is refused",
                field="producer_identity",
            )
        if producer_identity != durable_producer:
            raise PublicationRefused(
                f"the declared producer {producer_identity!r} is not the producer "
                f"the owned attempt {intent_id!r} recorded "
                f"({durable_producer!r}); an unverifiable producer is refused",
                field="producer_identity",
            )
        baseline_hashes = adapter.read_baseline(baseline)
        txn = CmsTransaction(
            run_id=intent.run_id,
            subject=identity,
            subject_kind=subject_kind_of(subject),
            subject_revision=getattr(subject, "revision", ""),
            adapter=adapter.contract.adapter,
            txn_id=txn_id or f"{intent.run_id}/{intent_id}",
            baseline_hashes=baseline_hashes,
            capabilities=tuple(adapter.contract.capabilities),
            producer_identity=durable_producer,
            intent_id=intent.intent_id,
            sandbox_target=sandbox_target,
            created_at=now or _now_iso(),
        )
        self._store.save_cms_transaction(txn)
        # Bind the *one* owner-authorized sandbox adapter to the transaction, so
        # every later stage drives the same retained restore point.
        self._cms_sandbox[txn.txn_id] = adapter
        return txn

    def draft_cms_transaction(
        self, txn: CmsTransaction, *, change: Any,
        now: Optional[str] = None,
    ) -> CmsTransaction:
        """Draft a change and dry-run it (criterion 1).

        The drafted content is dry-run *before* any apply: the dry-run hashes are
        recorded (never the applied hashes), so a caller proves the transaction
        can run without mutating the target.  Only the ``baseline -> drafted ->
        dry_run`` transition is accepted.  The change is drafted against the
        *bound sandbox adapter's* retained content, so no target is touched.
        """
        self._require_controller()
        if txn.stage != CMS_STAGE_BASELINE:
            raise CmsStageError(CMS_STAGE_BASELINE, txn.stage)
        adapter = self._sandbox_adapter(txn)
        draft_content = adapter.draft(adapter.readback(), change)
        dry_run_hashes = adapter.dry_run(draft_content)
        advanced = replace(
            txn,
            stage=CMS_STAGE_DRY_RUN,
            draft_digest=dry_run_hashes.semantic,
            dry_run_hashes=dry_run_hashes,
            created_at=now or txn.created_at,
        )
        self._store.save_cms_transaction(advanced, upsert=True)
        return advanced

    def apply_cms_transaction(
        self, txn: CmsTransaction, *, change: Any,
        now: Optional[str] = None,
    ) -> CmsTransaction:
        """Apply the drafted change and verify it by independent readback.

        The apply is the *bounded* mutation: the bound adapter retains the
        baseline as the restore point, the change is applied, and the readback
        must reproduce the applied hashes (a contradictory readback is refused by
        the frozen record).  Only the ``dry_run -> applied -> readback``
        transition is accepted, so an un-dry-run apply is refused.
        """
        self._require_controller()
        if txn.stage != CMS_STAGE_DRY_RUN:
            raise CmsStageError(CMS_STAGE_DRY_RUN, txn.stage)
        adapter = self._sandbox_adapter(txn)
        draft_content = adapter.draft(adapter.readback(), change)
        applied_hashes = adapter.apply(draft_content)
        readback_hashes = adapter.readback()
        advanced = replace(
            txn,
            stage=CMS_STAGE_READBACK,
            applied_hashes=applied_hashes,
            readback_hashes=readback_hashes,
            created_at=now or txn.created_at,
        )
        self._store.save_cms_transaction(advanced, upsert=True)
        return advanced

    def restore_cms_transaction(
        self, txn: CmsTransaction, *, now: Optional[str] = None
    ) -> tuple[CmsTransaction, str]:
        """Restore the baseline and establish restoration by raw hashes.

        Returns ``(txn, blocker)``.  When the restore reproduces the baseline's
        raw semantic *and* render hashes the transaction advances to the
        ``restored`` stage and the blocker is empty.  When it does not, the
        transaction is left at its prior stage and a *named* blocker is returned
        -- restoration is never claimed without the hashes proving it.  Only the
        ``readback`` stage may be restored from.
        """
        self._require_controller()
        if txn.stage != CMS_STAGE_READBACK:
            raise CmsStageError(CMS_STAGE_READBACK, txn.stage)
        adapter = self._sandbox_adapter(txn)
        restored_hashes = adapter.restore()
        if not restored_hashes.matches(txn.baseline_hashes):
            blocker = (
                "restore_not_established: restored raw hashes "
                f"semantic={restored_hashes.semantic!r} render="
                f"{restored_hashes.render!r} do not reproduce the baseline "
                f"semantic={txn.baseline_hashes.semantic!r} render="
                f"{txn.baseline_hashes.render!r}"
            )
            return txn, blocker
        advanced = replace(
            txn,
            stage=CMS_STAGE_RESTORED,
            restored_hashes=restored_hashes,
            created_at=now or txn.created_at,
        )
        self._store.save_cms_transaction(advanced, upsert=True)
        return advanced, ""

    def cms_run_no_transformer(
        self,
        *,
        run_id: str,
        task_id: str = "cms-no-txt",
        attempt: int = 1,
        subject: Any = None,
        baseline: Any = None,
        owner_sandbox: Any = None,
        binding: Any = None,
    ) -> CmsTransaction:
        """Run a complete no-text-transformer CMS transaction (criterion 4).

        Opens, drafts, applies, readback-verifies and restores a CMS transaction
        through the dummy :class:`RawContentAdapter` -- no provider, no
        transformer, no live egress.  Returns the restored transaction.

        When credentials or a live provider would be required, the method raises
        :class:`ExternalHold` rather than fabricating a success: the private
        provider implementation stays in skillweave-packs-pro and this
        repository carries only the dummy sandbox adapter.  The caller receives
        ``EXTERNAL_HOLD_PROVIDER_NOT_HERE`` as a typed refusal.
        """
        from skillweave.dispatch.work_contract import ContentSubject

        self._require_controller()
        subject = subject or ContentSubject(
            channel="sandbox/ch", content_id="e1", revision="rev-1"
        )
        baseline = baseline or {"title": "old", "body": "hello"}
        binding = binding or SandboxBinding(target="sandbox/ch")
        owner_sandbox = owner_sandbox or OwnerSandboxDeclaration(
            targets=("sandbox/ch",)
        )
        captured = process_identity_module.capture(os.getpid())
        intent, _ = self.dispatch(
            run_id=run_id, task_id=task_id, attempt=attempt,
            step=STEP_READY_DISPATCH,
            process_identity=captured.to_dict() if captured else None,
        )
        producer = _durable_producer_identity(intent)
        if not producer:
            raise ExternalHold(EXTERNAL_HOLD_PROVIDER_NOT_HERE)
        txn = self.open_cms_transaction(
            intent.intent_id, subject, RawContentAdapter(),
            baseline=baseline, producer_identity=producer,
            binding=binding, owner_sandbox=owner_sandbox,
        )
        txn = self.draft_cms_transaction(
            txn, change={"title": "new", "body": "hello"}
        )
        txn = self.apply_cms_transaction(
            txn, change={"title": "new", "body": "hello"}
        )
        txn, blocker = self.restore_cms_transaction(txn)
        if blocker:
            raise ExternalHold(
                f"external_hold: restore blocker {blocker!r}; "
                f"no live provider is probed"
            )
        return txn

    def load_cms_transaction(self, run_id: str, txn_id: str) -> Optional[CmsTransaction]:
        """Load a persisted managed CMS transaction (a recovering controller).

        A recovering controller reads the transaction from durable state rather
        than re-driving it; an unsupported persisted version fails closed.
        """
        record = self._store.get_cms_transaction(run_id, txn_id)
        if record is None:
            return None
        validate_cms_transaction(record)
        return CmsTransaction(
            run_id=record.get("run_id", ""),
            subject=record.get("subject", ""),
            subject_kind=record.get("subject_kind", ""),
            subject_revision=record.get("subject_revision", ""),
            adapter=record.get("adapter", ""),
            baseline_hashes=_hashes_from(record.get("baseline_hashes")),
            txn_id=record.get("txn_id", ""),
            stage=record.get("stage", CMS_STAGE_BASELINE),
            draft_digest=record.get("draft_digest", ""),
            dry_run_hashes=_hashes_from(record.get("dry_run_hashes")),
            applied_hashes=_hashes_from(record.get("applied_hashes")),
            readback_hashes=_hashes_from(record.get("readback_hashes")),
            restored_hashes=_hashes_from(record.get("restored_hashes")),
            capabilities=tuple(record.get("capabilities", ())),
            producer_identity=record.get("producer_identity", ""),
            intent_id=record.get("intent_id", ""),
            sandbox_target=record.get("sandbox_target", ""),
            created_at=record.get("created_at", ""),
            schema_version=record.get("schema_version", CMS_TXN_SCHEMA_VERSION),
        )

    def bind_cms_sandbox(
        self,
        txn: CmsTransaction,
        adapter: Any,
        *,
        binding: Any = None,
        owner_sandbox: Any = None,
    ) -> None:
        """Re-bind the one owner-authorized sandbox adapter to a *recovered* txn.

        A controller that loaded a transaction from durable state (a takeover)
        re-binds its single sandbox adapter here so the transaction can resume
        its lifecycle.  The adapter must declare the same capabilities as the
        transaction recorded; a mismatched adapter is refused rather than driven.
        An explicit sandbox ``binding`` may be supplied and is resolved through
        the same structural discriminator as :meth:`open_cms_transaction`, so a
        ``production``-declared or unknown binding is refused here too.

        The recorded target and any fresh binding are additionally resolved
        against the owner's positive ``owner_sandbox`` declaration: a target the
        owner did not name is refused fail-closed (an absent declaration
        authorizes nothing), so a takeover cannot re-point the restore point at
        an unauthorized surface the owner never declared.
        """
        self._require_controller()
        # A recovered transaction carries the sandbox target it was opened
        # against; re-validate it (never trust a legacy/production-looking
        # recorded target) and, when a fresh binding is given, require that it
        # names the *same* target, so a takeover cannot re-point the restore
        # point at an undeclared or production-looking surface.
        recorded_target = _require_owner_authorized_sandbox(
            txn.sandbox_target, owner_sandbox
        )
        if binding is not None:
            bound_target = _require_owner_authorized_sandbox(
                resolve_cms_sandbox_binding(binding), owner_sandbox
            )
            if bound_target != recorded_target:
                raise CmsBindingRefused(
                    CMS_BINDING_SANDBOX, target=bound_target
                )
        missing = tuple(
            cap for cap in txn.capabilities
            if not callable(getattr(adapter, cap, None))
        )
        if missing:
            raise CmsCapabilityError(missing, txn.adapter)
        self._cms_sandbox[txn.txn_id] = adapter

    def _sandbox_adapter(self, txn: CmsTransaction) -> Any:
        """The one owner-authorized sandbox adapter bound to *txn*.

        The owner holds exactly one adapter per transaction, so the retained
        restore point survives across stages.  A transaction with no bound
        adapter (a recovered transaction that was never re-bound) is refused
        rather than silently driven through a stateless stub.
        """
        adapter = self._cms_sandbox.get(txn.txn_id)
        if adapter is None:
            raise CmsCapabilityError(
                tuple(CMS_CAPABILITIES),
                f"{txn.adapter} (no sandbox binding for {txn.txn_id!r}; "
                f"re-bind with bind_cms_sandbox before resuming)",
            )
        return adapter

    # -- authorization trust model (SW-1511-CMS-TRANSACTION c2) --
    #
    # Every input the CMS authorization path consumes, who supplies it, and how
    # it is derived or verified from durable, server-owned facts.  No
    # authorization decision may depend on a value the caller can freely choose:
    # each caller-supplied argument below is either *re-derived* from durable
    # state or *compared* against a durable fact, never trusted as-is.
    #
    #   input                      | caller-supplied? | derived/verified from
    #   ---------------------------+------------------+-------------------------------------------
    #   producer_identity          | declared         | derived from the owned intent's durable
    #                              |                  |   process_identity via process_identity.verify
    #                              |                  |   (alive + image + start_time match); a
    #                              |                  |   mismatched/blank declaration is refused
    #   txn.subject_revision       | no (recorded)    | saved on the transaction at open time; the
    #                              |                  |   review is compared against it
    #   artifact_digest            | declared         | compared against the *review's recorded*
    #                              |                  |   artifact_digest; a mismatch refuses
    #   review verdict             | no (recorded)    | read from the durable review body; only
    #                              |                  |   ACCEPTANCE_ACCEPTED authorizes
    #   review subject_revision    | no (recorded)    | read from the durable body; must equal the
    #                              |                  |   transaction's revision
    #   review artifact_digest     | no (recorded)    | read from the durable body; must equal the
    #                              |                  |   asserted digest
    #   review principal           | no (recorded)    | server-issued shape mcp:<role>:<client_id>
    #                              |                  |   with role in MCP_ROLE_SCOPES; must match
    #                              |                  |   the server-stamped body role
    #   review role                | no (recorded)    | server-stamped from the issuing session;
    #                              |                  |   must be the independent reviewer role
    #   review run/task/attempt    | no (recorded)    | compared against the owned intent's
    #                              |                  |   run/task/attempt
    #   authorization (PublicationAuthorization) | declared | must be designated and bind to the
    #                              |                  |   exact revision/digest; authorizer !=
    #                              |                  |   producer
    #   scope_target/permitted_scope | declared       | scope expansion refused via permitted_scope
    #   sandbox target (open/bind) | declared         | resolved against the owner's positive
    #                              |                  |   OwnerSandboxDeclaration allow-list AND the
    #                              |                  |   production-marker denylist; an unlisted or
    #                              |                  |   absent-declaration target refuses
    #
    # `open_cms_transaction` / `bind_cms_sandbox` consume: producer_identity
    # (declared -> verified/derived as above) and the sandbox target (declared ->
    # owner allow-list + denylist).  The durable review loading
    # (`ResolvedReview.from_record`, `_require_review_answers_transaction`,
    # `_durable_attempt_review`) consumes only recorded facts: message_id,
    # principal, role, run/task/attempt, and the review's verdict/revision/digest/
    # criteria -- all read from the digest-covered durable body, never from a
    # caller argument.
    def authorize_cms_publication(
        self,
        txn: CmsTransaction,
        *,
        authorization: Optional[PublicationAuthorization],
        artifact_digest: str,
        review_ref: Optional[DurableReviewRef] = None,
        expected_review_criteria: Sequence[str] = (),
        policy: Any = None,
        scope_target: str = "",
        permitted_scope: Any = None,
    ) -> PublicationDecision:
        """Authorize an irreversible CMS publication at the controller (criterion 2).

        The producer identity is **derived from the transaction** (its recorded
        ``producer_identity``, which :meth:`open_cms_transaction` derived from the
        owned attempt's durable record), never accepted from the caller, so a
        producer can never publish by omitting or blanking the argument -- an
        unknown producer on the transaction refuses the publication fail-closed.
        The review is presented as a **reference to a durable record**
        (:class:`DurableReviewRef`), never as a caller-constructed object: the
        reference is *loaded from the durable mailbox* and its independence is
        decided against the server-recorded ``principal`` and the recorded
        ``run_id``/``task_id``/``attempt``, so a caller cannot mint an
        ``AttemptReview`` claiming independence.  The designated explicit
        authorization must bind to the transaction's *exact* subject revision and
        the given artifact digest, and the governing autonomy policy is consulted
        for the irreversible action, so a Unicorn policy holds rather than
        authorizing an irreversible publication; a target outside the declared
        scope is refused as a scope expansion.  The decision is returned for the
        caller to record; nothing is published here.
        """
        self._require_controller()
        if review_ref is None:
            raise PublicationRefused(
                "irreversible CMS publication requires a durable, server-recorded "
                "independent review; a caller-constructed review object is not "
                "accepted",
                field="review",
            )
        if not isinstance(review_ref, DurableReviewRef):
            raise PublicationRefused(
                "the publication review reference is not a durable review "
                "reference",
                field="review",
            )
        resolved = review_ref.resolve()
        # The review must answer *this* transaction's owned attempt, not merely
        # any attempt: a review recorded for another run/task/attempt cannot
        # authorize this publication.
        self._require_review_answers_transaction(resolved, txn)
        review = self._durable_attempt_review(
            resolved, txn, artifact_digest=artifact_digest,
            expected_review_criteria=expected_review_criteria,
        )
        return authorize_publication(
            authorization,
            subject_revision=txn.subject_revision,
            artifact_digest=artifact_digest,
            producer_identity=txn.producer_identity,
            review=review,
            expected_review_criteria=expected_review_criteria,
            policy=policy,
            scope_target=scope_target,
            permitted_scope=permitted_scope,
        )

    def _require_review_answers_transaction(
        self, resolved: ResolvedReview, txn: CmsTransaction
    ) -> None:
        """Refuse a durable review that does not answer the transaction's attempt."""
        intent = self._store.get_dispatch_intent(txn.intent_id) if txn.intent_id else None
        if intent is None:
            raise PublicationRefused(
                f"the transaction carries no owned attempt to bind a review to",
                field="review",
            )
        if (
            resolved.run_id != intent.run_id
            or resolved.task_id != intent.task_id
            or resolved.attempt != intent.attempt
        ):
            raise PublicationRefused(
                f"the durable review answers attempt "
                f"{resolved.run_id}/{resolved.task_id}/{resolved.attempt}, not the "
                f"transaction's owned attempt "
                f"{intent.run_id}/{intent.task_id}/{intent.attempt}",
                field="review",
            )

    def _durable_attempt_review(
        self,
        resolved: ResolvedReview,
        txn: CmsTransaction,
        *,
        artifact_digest: str,
        expected_review_criteria: Sequence[str] = (),
    ) -> AttemptReview:
        """Project a loaded durable review into the review the publication checks.

        The answering identity is the *server-recorded principal* the durable
        record was posted under (never a caller string), and the producer is the
        transaction's derived producer, so independence
        (:attr:`AttemptReview.independent`) is decided from durable facts.  The
        binding carries the *exact* subject revision and artifact digest the
        publication asserts, so the currentness check
        (:func:`~skillweave.trace.gate_receipt.assert_review_current`) can only
        pass for the revision/digest under publication.  The review's recorded
        subject is carried as its criteria, so an ``expected_review_criteria``
        request that the durable review does not name is refused.
        """
        # Every fact this review is checked against is taken FROM the durable
        # record (``resolved``); none is copied from the caller's asserted
        # ``artifact_digest`` or the transaction.  A review recorded for another
        # revision or a different digest therefore fails the currentness check
        # rather than being re-stamped with the caller's values.
        recorded_revision = resolved.subject_revision
        recorded_digest = resolved.artifact_digest
        if not recorded_revision:
            raise PublicationRefused(
                "the durable review names no subject revision to bind to",
                field="review",
            )
        if not recorded_digest:
            raise PublicationRefused(
                "the durable review names no reviewed artifact digest to bind to",
                field="review",
            )
        # Requirement 2: the review's *own* verdict decides.  Only an accepting
        # verdict counts; REPAIR, HOLD, an unknown token or a missing verdict
        # refuse -- never re-stamped as accepted.
        if resolved.verdict != ACCEPTANCE_ACCEPTED:
            raise PublicationRefused(
                f"the durable review reached verdict {resolved.verdict or '<missing>'!r}, "
                f"not {ACCEPTANCE_ACCEPTED!r}; only an accepting independent review "
                f"authorizes irreversible publication",
                field="review",
            )
        # The review must bind the *exact* subject revision and artifact digest the
        # publication asserts (the caller's asserted digest is compared, never
        # written into the binding), so a review for another revision or an
        # unreviewed digest cannot authorize this publication.
        if recorded_revision != txn.subject_revision:
            raise PublicationRefused(
                f"the durable review is bound to subject revision "
                f"{recorded_revision!r}, not the transaction's "
                f"{txn.subject_revision!r}",
                field="review",
            )
        if recorded_digest != artifact_digest:
            raise PublicationRefused(
                f"the durable review is bound to artifact digest "
                f"{recorded_digest!r}, not the asserted {artifact_digest!r}",
                field="review",
            )
        criteria = tuple(resolved.criteria) or tuple(expected_review_criteria)
        binding = ReviewBinding(
            review_id=resolved.message_id,
            review_criteria=criteria,
            subject_revision=recorded_revision,
            artifact_digest=recorded_digest,
        )
        return AttemptReview(
            attempt_id=f"{resolved.run_id}/{resolved.task_id}/{resolved.attempt}",
            binding=binding,
            role=resolved.role or PUBLICATION_REVIEW_ROLE,
            answering_identity=resolved.principal,
            producer_identity=txn.producer_identity,
            verdict=resolved.verdict,
            accepted_revision=recorded_revision,
            accepted_digest=recorded_digest,
        )


# ---------------------------------------------------------------------------
# attempt upstream telemetry (additive)
# ---------------------------------------------------------------------------
#
# A production worker port launches an owned attempt and must record what the
# gateway actually *answered*: the answering gateway endpoint, the route and the
# upstream model.  This is that record.  It is deliberately fail-closed about
# its own content -- it cannot exist without the answering endpoint/route/model,
# it cannot exist without a verified endpoint, and it can never be a receipt of
# a substituted (fallback-masked) answer.  A substituted answer is a *named hold
# at the port*, never telemetry: recording one here would launder a fallback
# into evidence.

ATTEMPT_UPSTREAM_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/attempt-upstream-receipt/v1"
)
ATTEMPT_UPSTREAM_SCHEMA_VERSION = "1.0.0"
SUPPORTED_ATTEMPT_UPSTREAM_VERSIONS = frozenset({ATTEMPT_UPSTREAM_SCHEMA_VERSION})


class AttemptUpstreamRefused(ValueError):
    """An owned attempt's gateway answer could not be recorded as trustworthy.

    *hold* names the declared refusal -- an unconfigured, unreachable or
    fallback-capable endpoint, an unverifiable route, an unverified process
    identity or an unprovisioned scoped grant -- so the caller records a
    *named hold* rather than an anonymous failure.  The attempt is never
    recorded against an unverified upstream.
    """

    def __init__(self, reason: str, *, hold: str = "", field: str = ""):
        self.hold = hold
        self.field = field
        super().__init__(reason)


@dataclass(frozen=True)
class AttemptUpstreamReceipt:
    """An ``attempt-upstream-receipt-v1`` record: the answering upstream facts.

    Every field is read from the observed launch and the gateway answer; nothing
    it records is caller-chosen.  ``answering_identity`` names the endpoint +
    route + upstream model so the receipt cannot be satisfied by naming an
    upstream it did not observe.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported;
    * the receipt must name its run and carry an integer attempt >= 0;
    * the answering gateway endpoint, route and upstream model are all present
      (an unrecorded field is never "verified");
    * the endpoint must be marked verified;
    * a substituted or fallback-capable answer must not be recorded at all (it
      is a named hold, not a receipt);
    * a carried process identity must be the verified facts (pid +
      start_time), never a bare PID.
    """

    run_id: str
    gateway_endpoint: str = ""
    route: str = ""
    upstream_model: str = ""
    requested_model: str = ""
    served_by: str = ""
    task_id: str = ""
    attempt: int = 0
    substituted: bool = False
    fallback_capable: bool = False
    endpoint_verified: bool = False
    process_identity: Optional[dict] = None
    recorded_at: str = field(default_factory=_now_iso)
    schema_version: str = ATTEMPT_UPSTREAM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_ATTEMPT_UPSTREAM_VERSIONS:
            raise UnsupportedContractVersion(
                "attempt-upstream-receipt-v1", self.schema_version,
                SUPPORTED_ATTEMPT_UPSTREAM_VERSIONS,
            )
        if not self.run_id:
            raise ValueError("attempt upstream receipt must name its run_id")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise ValueError("attempt upstream receipt attempt must be an integer")
        if self.attempt < 0:
            raise ValueError("attempt upstream receipt attempt must be >= 0")
        for name, value in (
            ("gateway_endpoint", self.gateway_endpoint),
            ("route", self.route),
            ("upstream_model", self.upstream_model),
        ):
            if not value:
                raise ValueError(
                    f"attempt upstream receipt must record the answering "
                    f"{name}; an unrecorded {name} is not verifiable and is "
                    f"never recorded as a receipt"
                )
        if not self.endpoint_verified:
            raise ValueError(
                "attempt upstream receipt must carry a verified gateway "
                "endpoint; an unverified endpoint is a named hold, not a receipt"
            )
        if self.substituted:
            raise ValueError(
                "attempt upstream receipt must not record a substituted answer; "
                "a fallback-masked answer is refused at the port, never recorded"
            )
        if self.fallback_capable:
            raise ValueError(
                "attempt upstream receipt must not record a fallback-capable "
                "endpoint; a fallback-capable endpoint is a named hold"
            )
        if self.process_identity is not None:
            if not isinstance(self.process_identity, Mapping):
                raise ValueError(
                    "attempt upstream receipt process_identity must be a mapping"
                )
            if process_identity.ProcessIdentity.from_dict(self.process_identity) is None:
                raise ValueError(
                    "attempt upstream receipt process_identity must carry the "
                    "verified facts (pid + start_time), not a bare PID"
                )

    @property
    def answering_identity(self) -> str:
        """The recorded answering upstream identity (endpoint/route/model)."""
        return f"{self.gateway_endpoint}|{self.route}|{self.upstream_model}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": ATTEMPT_UPSTREAM_SCHEMA_ID,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "gateway_endpoint": self.gateway_endpoint,
            "route": self.route,
            "upstream_model": self.upstream_model,
            "requested_model": self.requested_model,
            "served_by": self.served_by,
            "substituted": self.substituted,
            "fallback_capable": self.fallback_capable,
            "endpoint_verified": self.endpoint_verified,
            "process_identity": self.process_identity,
            "answering_identity": self.answering_identity,
            "recorded_at": self.recorded_at,
        }


def validate_attempt_upstream_receipt(record: Mapping[str, Any]) -> None:
    """Refuse an attempt upstream receipt this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered receipt (a missing answering field, a
    substituted answer, a fallback-capable endpoint, a bare PID posing as
    identity) is refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_ATTEMPT_UPSTREAM_VERSIONS:
        raise UnsupportedContractVersion(
            "attempt-upstream-receipt-v1", version,
            SUPPORTED_ATTEMPT_UPSTREAM_VERSIONS,
        )
    if record.get("schema_id") != ATTEMPT_UPSTREAM_SCHEMA_ID:
        raise ValueError(
            f"attempt upstream receipt schema_id {record.get('schema_id')!r} is "
            f"not {ATTEMPT_UPSTREAM_SCHEMA_ID!r}"
        )
    AttemptUpstreamReceipt(
        run_id=record.get("run_id", ""),
        gateway_endpoint=record.get("gateway_endpoint", ""),
        route=record.get("route", ""),
        upstream_model=record.get("upstream_model", ""),
        requested_model=record.get("requested_model", ""),
        served_by=record.get("served_by", ""),
        task_id=record.get("task_id", ""),
        attempt=record.get("attempt", 0),
        substituted=record.get("substituted", False),
        fallback_capable=record.get("fallback_capable", False),
        endpoint_verified=record.get("endpoint_verified", False),
        process_identity=record.get("process_identity"),
        recorded_at=record.get("recorded_at", ""),
        schema_version=version,
    )


class _EvidenceJournalView(Sequence):
    """The record source :func:`build_evidence_delta` reads.

    A read-only :class:`collections.abc.Sequence` over one run's durable journal
    facts, plus ``list_evidence`` (the run's receipts, so the view can name the
    new evidence references) and ``store`` (entity loading for any receipt the
    view needs).  All three are read-only projections of durable state.
    """

    __slots__ = ("_records", "store")

    def __init__(self, records: Sequence[Any], evidence: Sequence[Any]) -> None:
        self._records = tuple(records)
        # Receipts may arrive as objects (``ArtifactReceipt``) or as durable
        # read projections (mappings); both name the same artifact.
        self.store = dict((_field(row, "artifact_id"), row) for row in evidence)

    def __iter__(self):
        return iter(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __getitem__(self, index):
        return self._records[index]

    def list_evidence(self, *args, **kwargs):
        return list(self.store.values())
