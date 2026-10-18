"""Bounded semantic preflight repair with automatic redispatch.

A dispatched lane can fail preflight for reasons that are *mechanical* and
grounded in evidence: a ``modifies`` path that does not exist in the target, or
a source path whose language contradicts what the task declares. Those two are
repairable without touching task intent or widening write scope. Everything
else — identity drift, an unbounded loop, a needed capability the acting role
lacks — is HELD, never "fixed".

The repair loop is deliberately strict:

* it edits ONLY the fields named in ``PreflightFailure.implicated_fields``;
* it refuses to run at all if the target digest moved (target drift);
* it stops the second time it sees the same failure fingerprint;
* it stops when the attempt/budget is exhausted;
* it stops when applying the repair would require a capability the role lacks
  (authority expansion);
* every attempt appends a before/after record to the append-only journal.

Nothing here schedules, merges, or releases. It repairs a plan and hands the
repaired plan back for revalidation + redispatch against the *unchanged* target
digest.

Classification + Assay binding (SW-1510-REPAIR)
-----------------------------------------------

A repair is only ever as good as the *class* of failure it answers. Every
failure is therefore classified into one of the :class:`ErrorClass` values
(SW-1511-REPAIR separated the coarse ``auth_quota`` umbrella into distinct
provider-quota, auth-refusal, budget-exhausted, correction-exhausted and
transport-failure classes) and each class has exactly one permitted action
(:data:`ALLOWED_ACTION`): a bounded in-place ``remediate``, a ``reschedule``
against a moved target, an explicit scope-``split``, or a ``hold`` that is
never automated. The classification is not decorative — it decides the action,
and the action is handed to the Assay controller
(:class:`skillweave.runtime.assay.AssayRepairController`) as a
:class:`skillweave.runtime.assay.RepairActionRequest`, where the Assay policy
(Unicorn for permitted reversible decisions, Conservative/Moderate to hold)
and the Assay admission gates decide whether it happens.

Every correction is recorded as an ``attempt-event-v1`` record
(:meth:`RepairAttempt.to_attempt_event`) carrying the failure fingerprint, the
hypothesis, the changed input, the verifier result and a bounded correction
count — appended to, never overwriting, the prior attempts.

Attempt-condition control record (SW-1511-REPAIR criterion 4)
-------------------------------------------------------------

When a provider quota refuses an owned attempt there is no permitted fallback:
substituting another route/credential/provider would raise the acting
*authority*, so it is never inferred.  The one control decision available is to
store the provider's reset/backoff window and *wait*, then resume the **same
owned session on the same route** — never a substituted one.  That decision is
represented by :func:`attempt_condition_for_quota`, emitted as an
``attempt-condition-v1`` record (:meth:`AttemptCondition.to_dict` /
:meth:`AttemptCondition.to_attempt_condition`).  While the attempt waits the run
sits in the non-terminal :data:`~skillweave.runtime.store.RunStateModel.
BLOCKED_WAITING_FOR_GATE` state, so cancellation stays available
(:data:`AttemptCondition.cancellable` is a live fact, not a promise).  A
provider quota the caller cannot place on a future window (no reset, or an
already-elapsed one) is never reported as waiting: the decision fails closed to
a non-waiting HOLD with no stored window.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .preflight import (
    AUTO_REPAIR_CAPABILITY,
    FailureClass,
    PreflightFailure,
    PreflightResult,
    Retryability,
    digest_target,
)

#: The frozen attempt-event contract this module produces corrections under.
#: The exact URI id is the SDK execution contract (see
#: ``tests/unit/test_1510_stream.py``); this module carries the same id/version
#: and refuses any other version rather than guessing at the shape.
ATTEMPT_EVENT_SCHEMA_ID = "https://skillweave.dev/schemas/execution/attempt-event/v1"
ATTEMPT_EVENT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_ATTEMPT_EVENT_VERSIONS = frozenset({ATTEMPT_EVENT_SCHEMA_VERSION})


class UnsupportedAttemptEventVersion(ValueError):
    """A correction event carries a version this consumer does not support."""

    def __init__(self, version: Any):
        self.version = version
        super().__init__(
            f"unsupported attempt-event schema_version {version!r}; expected one "
            f"of {sorted(SUPPORTED_ATTEMPT_EVENT_VERSIONS)}"
        )


class ErrorClass(str, Enum):
    """The distinguishable failure classes a bounded repair must separate.

    The classification is the *first* decision: it selects exactly one allowed
    action (see :data:`ALLOWED_ACTION`) and is what the Assay controller gates.

    SW-1511-REPAIR (criterion 1) splits the coarse ``auth_quota`` umbrella into
    the *distinct* conditions it used to conflate, so a genuine provider quota,
    an authentication refusal, an exhausted local budget envelope, a mechanical
    correction exhaustion and a local tool/transport failure no longer share one
    class (and one control policy). ``auth_quota`` is retained as the legacy
    umbrella value: existing ``attempt-event-v1`` records and the adaptive
    ``backoff`` kind still carry it, so it stays a live member — it is simply no
    longer what live classification produces for those conditions.
    """

    #: LEGACY umbrella (SW-1510): the coarse "auth/quota" bucket. Retained for
    #: backward compatibility with persisted ``attempt-event-v1`` records and
    #: the adaptive ``backoff`` kind. Live classification now emits the more
    #: specific classes below; this value still means HOLD.
    AUTH_QUOTA = "auth_quota"
    #: Provider quota: the provider itself refused with a real quota/rate-limit
    #: condition (429 / usage limit / out of credits). Distinct from an
    #: authentication refusal and from a spent local envelope. With no *permitted*
    #: fallback the action is HOLD; the (separately authorized) fallback that
    #: stores reset/backoff and resumes the owned session on the same route is a
    #: later, gated action — never inferred here.
    PROVIDER_QUOTA = "provider_quota"
    #: Authentication refusal: the acting authority refused (401/403, an
    #: unrecognised credential, a refused admission on the actor axis). The
    #: action is HOLD — never a capability grant, never a silent retry.
    AUTH_REFUSAL = "auth_refusal"
    #: Budget exhausted: the owner-authorized deadline/allowance envelope is
    #: spent. There is no authorized fallback — the action is HOLD.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: Correction exhausted: the *mechanical* bounded repair ran out of its
    #: attempt / correction-round ceiling. This is a local control-policy
    #: exhaustion, distinct from a provider quota or an auth refusal; the action
    #: is HOLD (escalate), and it never widens a budget to keep retrying.
    CORRECTION_EXHAUSTED = "correction_exhausted"
    #: Transport failure: a local tool/transport failure (connection refused/
    #: reset, timeout, DNS, broken pipe, tool-harness crash). A local, not a
    #: provider-quota, condition; the action is HOLD rather than a blind retry.
    TRANSPORT_FAILURE = "transport_failure"
    #: Capability mismatch: the acting role lacks a capability the action needs.
    #: Granting it would be an authority expansion — the action is HOLD.
    CAPABILITY_MISMATCH = "capability_mismatch"
    #: Context pressure: the probe exhausted its working context. The bounded
    #: action is an explicit scope SPLIT, never a silent widening.
    CONTEXT_PRESSURE = "context_pressure"
    #: Malformed artifact: a declared surface is ungrounded/unsupported. The
    #: bounded action is a grounded, 1:1 REMEDIATE.
    MALFORMED_ARTIFACT = "malformed_artifact"
    #: Source drift: the target moved under the diagnosis. The bounded action is
    #: a RESCHEDULE against the *current* digest, never a stale repair.
    SOURCE_DRIFT = "source_drift"
    #: Repeated semantic failure: the same fingerprint recurred, so the repair
    #: did not stick. The action is HOLD (escalate/split), never another retry.
    REPEATED_SEMANTIC_FAILURE = "repeated_semantic_failure"
    #: Unclassified: no typed signal matched, so the failure cannot be shown to
    #: be a *malformed artifact*. The classification fails closed to a HOLD
    #: rather than defaulting to a permissive ``remediate`` (the malformed-
    #: artifact class is reserved for genuinely malformed-artifact evidence).
    UNCLASSIFIED = "unclassified"


class RepairAction(str, Enum):
    """The single permitted action for an error class."""

    REMEDIATE = "remediate"
    RESCHEDULE = "reschedule"
    SPLIT = "split"
    HOLD = "hold"


#: One permitted action per error class. A class whose permitted action is
#: HOLD is never automated: it escalates to a human/controller decision.
#:
#: SW-1511-REPAIR: the five condition classes split out of the coarse
#: ``auth_quota`` umbrella all map to HOLD. None of them is a repair the local
#: loop may take on its own: a real provider quota needs a *permitted* fallback
#: (a later, separately gated action, never inferred here), an auth refusal must
#: not become a silent capability grant, a spent budget must not be widened, a
#: mechanical correction exhaustion must escalate rather than retry forever, and
#: a transient transport failure is not a groundable artifact to remediate. Each
#: is a distinct class so the controller can apply a distinct control policy.
ALLOWED_ACTION: dict[ErrorClass, RepairAction] = {
    ErrorClass.AUTH_QUOTA: RepairAction.HOLD,
    ErrorClass.PROVIDER_QUOTA: RepairAction.HOLD,
    ErrorClass.AUTH_REFUSAL: RepairAction.HOLD,
    ErrorClass.BUDGET_EXHAUSTED: RepairAction.HOLD,
    ErrorClass.CORRECTION_EXHAUSTED: RepairAction.HOLD,
    ErrorClass.TRANSPORT_FAILURE: RepairAction.HOLD,
    ErrorClass.CAPABILITY_MISMATCH: RepairAction.HOLD,
    ErrorClass.CONTEXT_PRESSURE: RepairAction.SPLIT,
    ErrorClass.MALFORMED_ARTIFACT: RepairAction.REMEDIATE,
    ErrorClass.SOURCE_DRIFT: RepairAction.RESCHEDULE,
    ErrorClass.REPEATED_SEMANTIC_FAILURE: RepairAction.HOLD,
    ErrorClass.UNCLASSIFIED: RepairAction.HOLD,
}

#: Alias kept for callers that read the mapping by string key.
ALLOWED_ACTION_BY_VALUE: dict[str, str] = {
    cls.value: action.value for cls, action in ALLOWED_ACTION.items()
}


def allowed_action(error_class: ErrorClass | str) -> str:
    """Return the single permitted action value for *error_class*.

    Raises :class:`UnsupportedErrorClass` for a class outside the vocabulary
    rather than defaulting to a permissive action.
    """
    try:
        cls = ErrorClass(error_class)
    except ValueError as exc:
        raise UnsupportedErrorClass(error_class) from exc
    return ALLOWED_ACTION[cls].value


class UnsupportedErrorClass(ValueError):
    """An error class outside the recognised vocabulary was presented."""

    def __init__(self, error_class: Any):
        self.error_class = error_class
        super().__init__(
            f"unsupported error class {error_class!r}; expected one of "
            f"{sorted(c.value for c in ErrorClass)}"
        )


#: Fields whose divergence is an identity/authority change, never a repairable
#: surface. Mirrors ``preflight._IDENTITY_FIELDS`` (kept explicit here so the
#: classifier is self-contained).
_IDENTITY_FIELDS = frozenset(
    {"product", "remote_repo", "branch", "worktree", "sha", "role"}
)

#: Structured axis names the seam attaches to a refusal, mapped to the class
#: they imply. A named axis is authoritative: it is the *observed* check, not a
#: keyword guess. SW-1511-REPAIR separates the actor/admission/auth axes
#: (authentication refused) from the local budget/deadline/envelope axes (the
#: owner envelope is spent) and the provider quota/rate-limit axes (the provider
#: itself refused), so each gets its own control policy; capability/scope/
#: authority axes are capability mismatches; a drift axis is source drift; and
#: so on.
_AXIS_SIGNALS: dict[str, ErrorClass] = {
    # authentication refused: the acting authority refused the credential.
    "actor": ErrorClass.AUTH_REFUSAL,
    "admission": ErrorClass.AUTH_REFUSAL,
    "auth": ErrorClass.AUTH_REFUSAL,
    # budget exhausted: the owner-authorized envelope/deadline is spent.
    "budget": ErrorClass.BUDGET_EXHAUSTED,
    "deadline": ErrorClass.BUDGET_EXHAUSTED,
    "envelope": ErrorClass.BUDGET_EXHAUSTED,
    "pin": ErrorClass.BUDGET_EXHAUSTED,
    "route": ErrorClass.BUDGET_EXHAUSTED,
    "turn_limit": ErrorClass.BUDGET_EXHAUSTED,
    # provider quota: the provider itself refused with a real quota condition.
    "quota": ErrorClass.PROVIDER_QUOTA,
    "rate_limit": ErrorClass.PROVIDER_QUOTA,
    # correction exhausted: the mechanical correction ceiling was reached.
    "correction": ErrorClass.CORRECTION_EXHAUSTED,
    "attempt": ErrorClass.CORRECTION_EXHAUSTED,
    # transport failure: a local tool/transport failure, not a provider refusal.
    "transport": ErrorClass.TRANSPORT_FAILURE,
    "tool": ErrorClass.TRANSPORT_FAILURE,
    "connection": ErrorClass.TRANSPORT_FAILURE,
    # capability mismatch: the role/route lacks a capability the action needs.
    "authority": ErrorClass.CAPABILITY_MISMATCH,
    "capability": ErrorClass.CAPABILITY_MISMATCH,
    "scope": ErrorClass.CAPABILITY_MISMATCH,
    # context pressure: the probe exhausted its working context.
    "context": ErrorClass.CONTEXT_PRESSURE,
    "context_pressure": ErrorClass.CONTEXT_PRESSURE,
    # source drift: the ground moved under the diagnosis.
    "drift": ErrorClass.SOURCE_DRIFT,
    "target_drift": ErrorClass.SOURCE_DRIFT,
    # repeated semantic failure: the same fingerprint recurred.
    "fingerprint": ErrorClass.REPEATED_SEMANTIC_FAILURE,
    "repeated": ErrorClass.REPEATED_SEMANTIC_FAILURE,
    # malformed artifact: a genuinely ungrounded/unsupported declared surface.
    "malformed": ErrorClass.MALFORMED_ARTIFACT,
    "schema": ErrorClass.MALFORMED_ARTIFACT,
}

#: Hold conditions the classifier maps onto the budget-exhausted class when
#: they arrive as an unlisted axis (kept explicit and broad so an unknown
#: budget-ish axis still fails closed to a HOLD rather than a permissive
#: action). ``auth``/``actor``/``admission`` are authentication refusals and are
#: mapped explicitly in :data:`_AXIS_SIGNALS`; ``quota``/``rate_limit`` are a
#: provider quota and likewise mapped there.
_BUDGET_AXES = frozenset(
    {
        "actor", "deadline", "budget", "envelope", "pin", "admission",
        "turn_limit", "quota", "route", "transport", "tool", "connection",
        "correction", "attempt",
    }
)

#: Ordered typed signals for free-text diagnosis — most specific first. Each
#: entry maps a compiled word-boundary pattern to the *single* class it implies;
#: the FIRST matching entry wins, so a specific signal always outranks a broader
#: one. This is what makes ambiguity deterministic:
#:
#: * a 429 / rate/usage limit / insufficient balance / out of credits is a
#:   *provider quota*, never a malformed artifact, while a 401/403 / unauthorized
#:   / unauthenticated refusal is an *authentication refusal* — two distinct
#:   conditions with distinct control policies (SW-1511-REPAIR);
#: * an exhausted budget/deadline/envelope is a *budget exhaustion*, and an
#:   exhausted correction/attempt ceiling is a *correction exhaustion* — neither
#:   is a provider quota;
#: * a connection refused/reset, timeout, broken pipe or tool-harness crash is a
#:   *transport failure* (local), never a provider quota;
#: * "the requested capability is unavailable on this route" is a capability
#:   mismatch (``capab`` is tested before the merely-incidental ``route``);
#: * a message that matches nothing is left *unclassified* so the caller fails
#:   closed to a HOLD — there is no permissive default.
_SIGNAL_PATTERNS: tuple[tuple[ErrorClass, "re.Pattern[str]"], ...] = (
    (
        ErrorClass.PROVIDER_QUOTA,
        re.compile(
            r"\b(?:quota|rate[ _-]?limit|too many requests|usage limit|"
            r"insufficient[ _a-z]*(?:quota|credit|balance|funds|token)|payments? required|"
            r"out of credits?|no credits?|"
            r"402|429)\b"
        ),
    ),
    (
        ErrorClass.AUTH_REFUSAL,
        re.compile(
            r"\b(?:(?:un)?authori[sz]ed|unauthenticated|no authorized fallback|"
            r"authentication (?:failed|refused|required)|"
            r"invalid (?:credential|api[ _-]?key|token)|"
            r"credential\w* refused|401|403)\b"
        ),
    ),
    (
        ErrorClass.CORRECTION_EXHAUSTED,
        re.compile(
            r"\b(?:correction (?:budget|limit|rounds?|exhausted)|"
            r"attempt(?:s)? (?:exhausted|limit)|max(?:imum)? attempts)\b"
        ),
    ),
    (
        ErrorClass.BUDGET_EXHAUSTED,
        re.compile(
            r"\b(?:budget (?:exhausted|spent|depleted)|out of budget|"
            r"deadline (?:exceeded|passed|reached)|"
            r"envelope (?:exhausted|spent)|allowance (?:exhausted|spent))\b"
        ),
    ),
    (
        ErrorClass.TRANSPORT_FAILURE,
        re.compile(
            r"\b(?:connection (?:refused|reset|closed|aborted)|"
            r"timed? ?out|timeout|broken pipe|no route to host|"
            r"name resolution|dns (?:failure|error)|transport (?:error|failure)|"
            r"tool (?:failure|error|crash|harness))\b"
        ),
    ),
    (
        ErrorClass.CONTEXT_PRESSURE,
        re.compile(
            r"\b(?:context (?:length|window|limit|overflow)|context[_ -]?length|"
            r"token limit|maximum tokens|ran out of context|"
            r"prompt is too long)\b"
        ),
    ),
    (
        ErrorClass.CAPABILITY_MISMATCH,
        re.compile(
            r"\b(?:capab\w*|permission\w*|forbidden|not support\w*|"
            r"unsupported (?:capabilit\w*|tool\w*|feature\w*)|"
            r"not available on this route|authority expansion)\b"
        ),
    ),
    (
        ErrorClass.SOURCE_DRIFT,
        re.compile(r"\b(?:drift\w*|stale|moved|mismatch\w*)\b"),
    ),
    (
        ErrorClass.REPEATED_SEMANTIC_FAILURE,
        re.compile(r"\b(?:fingerprint\w*|repeated\w*|recurred|recurring|recurr\w*)\b"),
    ),
    (
        ErrorClass.MALFORMED_ARTIFACT,
        re.compile(
            r"\b(?:malformed|schema|unsupported version|ungrounded|"
            r"missing field|syntax error)\b"
        ),
    ),
)


def _match_signal(text: str) -> Optional[ErrorClass]:
    """Return the class of the most specific typed signal in *text*, or ``None``.

    ``None`` means *unclassifiable* — deliberately not a malformed artifact, so
    the caller fails closed to a HOLD rather than remediating a failure it could
    not name.
    """
    lowered = text.lower()
    for error_class, pattern in _SIGNAL_PATTERNS:
        if pattern.search(lowered):
            return error_class
    return None


def _classify_exception(error: BaseException) -> Optional[ErrorClass]:
    """Best-effort structured classification of an exception from the seam.

    A named ``axis`` (the *observed* refusal) is authoritative and is consulted
    before free text; the ordered :data:`_SIGNAL_PATTERNS` scan over the
    exception name and text is the deterministic fallback.
    """
    axis = str(getattr(error, "axis", "") or "").strip().lower()
    if axis in _AXIS_SIGNALS:
        return _AXIS_SIGNALS[axis]
    if axis in _BUDGET_AXES:
        return ErrorClass.BUDGET_EXHAUSTED
    text = f"{type(error).__name__} {axis} {error}"
    return _match_signal(text)


def _classify_failure(
    failure: PreflightFailure, prior_fingerprints: Sequence[str]
) -> ErrorClass:
    """Classify a grounded :class:`PreflightFailure`.

    A recurring fingerprint is the repeated-semantic-failure class *first*: the
    same diagnosis recurring means the previous correction did not stick. An
    identity-field mismatch is source drift (the ground moved); the two
    grounding-repairable classes are a malformed artifact (the declared surface
    is ungrounded). Any grounded failure this vocabulary cannot name is
    *unclassified* — it is never defaulted into a permissive ``remediate``.
    """
    if prior_fingerprints and failure.fingerprint in set(prior_fingerprints):
        return ErrorClass.REPEATED_SEMANTIC_FAILURE
    if failure.failure_class in (
        FailureClass.NONEXISTENT_MODIFIES_PATH.value,
        FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value,
    ):
        return ErrorClass.MALFORMED_ARTIFACT
    if failure.failure_class == FailureClass.FIELD_MISMATCH.value and any(
        f in _IDENTITY_FIELDS for f in failure.implicated_fields
    ):
        return ErrorClass.SOURCE_DRIFT
    return ErrorClass.UNCLASSIFIED


def classify_error(
    *,
    failure: Optional[PreflightFailure] = None,
    error: Optional[BaseException] = None,
    message: str = "",
    classified: Optional[PreflightFailure] = None,
    prior_fingerprints: Optional[Sequence[str]] = None,
    budget_exhausted: bool = False,
) -> str:
    """Classify a failure into one of the :class:`ErrorClass` values.

    Inputs are the grounded diagnosis (``failure``), the raised refusal from the
    controller seam (``error``) and the run's prior correction fingerprints. An
    exhausted owner budget is classified as :data:`ErrorClass.BUDGET_EXHAUSTED`
    (distinct from a provider quota and an auth refusal) before anything else:
    it is a local control-policy exhaustion with no authorized fallback.

    A diagnosis that matches no typed signal is returned as
    :data:`ErrorClass.UNCLASSIFIED` — the classification fails closed, because a
    failure that cannot be *named* must never default to a permissive
    ``remediate``. ``classified`` is an accepted alias for ``failure``.
    """
    if classified is not None and failure is None:
        failure = classified
    if budget_exhausted:
        return ErrorClass.BUDGET_EXHAUSTED.value
    prior = list(prior_fingerprints or [])
    failure_class: Optional[ErrorClass] = None
    if failure is not None:
        failure_class = _classify_failure(failure, prior)
        if failure_class is not ErrorClass.MALFORMED_ARTIFACT:
            return failure_class.value
    if error is not None:
        cls = _classify_exception(error)
        if cls is not None:
            return cls.value
    if message:
        cls = _match_signal(message)
        if cls is not None:
            return cls.value
    if failure_class is not None:
        # A malformed-artifact failure with no competing signal, or an
        # unclassifiable one (which fails closed to a HOLD).
        return failure_class.value
    return ErrorClass.UNCLASSIFIED.value


class HoldReason(str, Enum):
    """Why the bounded repair loop refused to continue automatically."""

    REPEATED_FINGERPRINT = "repeated_fingerprint"
    TARGET_DRIFT = "target_drift"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    AUTHORITY_EXPANSION = "authority_expansion"
    NON_REPAIRABLE_CLASS = "non_repairable_class"
    IMPLICATED_FIELD_UNKNOWN = "implicated_field_unknown"
    #: SW-1510-REPAIR: the Assay controller seam held the classified action
    #: (policy requires human approval, admission refused, or a non-terminal
    #: Assay state could not author it). The raw reason is carried alongside.
    CONTROLLER_HELD = "controller_held"


#: Hold reasons that terminate a failure as an *escalate/split* decision rather
#: than a retry opportunity. These map to ``error_class = repeated_semantic_
#: failure`` in the correction record, because the loop must stop.
_ESCALATION_HOLDS = frozenset({
    HoldReason.REPEATED_FINGERPRINT.value,
    HoldReason.BUDGET_EXHAUSTED.value,
    HoldReason.ATTEMPTS_EXHAUSTED.value,
    HoldReason.AUTHORITY_EXPANSION.value,
    HoldReason.NON_REPAIRABLE_CLASS.value,
})


@dataclass(frozen=True)
class RepairAttempt:
    """An append-only record of one repair attempt.

    ``before_digest`` / ``after_digest`` content-address the target plan on
    either side, so the ledger proves exactly what changed. ``dispatch_identity``
    identifies the dispatch that a successful repair was redispatched as.

    SW-1510-REPAIR adds the classified correction evidence required of every
    correction: ``error_class`` (which :class:`ErrorClass`), ``hypothesis`` (the
    falsifiable claim the correction answers), ``verifier_result`` (the outcome
    of revalidating the corrected input) and ``correction_count`` (the bounded
    1-based count of corrections for this fingerprint, so the bound is visible
    in the record itself).
    """

    attempt: int
    failure_fingerprint: str
    failure_class: str
    implicated_fields: list[str]
    before_digest: str
    after_digest: str
    command: str
    exit_code: int
    outcome: str  # "repaired" | "held"
    hold_reason: Optional[str] = None
    dispatch_identity: Optional[str] = None
    #: SW-1510-REPAIR classification + bounded-correction evidence.
    error_class: str = ""
    hypothesis: str = ""
    changed_input: Optional[dict[str, Any]] = None
    verifier_result: str = ""
    correction_count: int = 0
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "failure_fingerprint": self.failure_fingerprint,
            "failure_class": self.failure_class,
            "implicated_fields": self.implicated_fields,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "command": self.command,
            "exit_code": self.exit_code,
            "outcome": self.outcome,
            "hold_reason": self.hold_reason,
            "dispatch_identity": self.dispatch_identity,
            "error_class": self.error_class,
            "hypothesis": self.hypothesis,
            "changed_input": self.changed_input,
            "verifier_result": self.verifier_result,
            "correction_count": self.correction_count,
            "recorded_at": self.recorded_at,
        }

    def to_attempt_event(
        self, *, run_id: str = "", task_id: str = "", principal: str = ""
    ) -> dict[str, Any]:
        """Export this correction as a frozen ``attempt-event-v1`` record.

        The record binds the failure fingerprint, the hypothesis, the changed
        input, the verifier result and the bounded correction count — the five
        facts criterion 2 requires every correction to preserve — under the
        frozen execution contract id, so a consumer can refuse an unsupported
        version rather than guess (see :func:`validate_attempt_event`).
        """
        event_id = f"{run_id or 'repair'}:{self.attempt}:{self.failure_fingerprint[:12]}"
        payload = {
            "error_class": self.error_class,
            "failure_class": self.failure_class,
            "failure_fingerprint": self.failure_fingerprint,
            "hypothesis": self.hypothesis,
            "implicated_fields": list(self.implicated_fields),
            "changed_input": self.changed_input,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "verifier_result": self.verifier_result,
            "outcome": self.outcome,
            "hold_reason": self.hold_reason,
            "correction_count": self.correction_count,
            "dispatch_identity": self.dispatch_identity,
            "command": self.command,
            "exit_code": self.exit_code,
        }
        evidence_digest = digest_target(payload)
        return {
            "schema_id": ATTEMPT_EVENT_SCHEMA_ID,
            "schema_version": ATTEMPT_EVENT_SCHEMA_VERSION,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": self.attempt,
            "event_id": event_id,
            "sequence": self.attempt,
            "event_type": "repair_attempt",
            "measured_at": self.recorded_at,
            "issuer": "repair-classifier",
            "principal": principal,
            "evidence_digest": evidence_digest,
            "payload": payload,
        }


def validate_attempt_event(event: dict[str, Any]) -> None:
    """Refuse an ``attempt-event-v1`` record this consumer cannot support.

    Raises :class:`UnsupportedAttemptEventVersion` for an unknown version (or an
    unknown schema id); raises :class:`ValueError` for a record missing the
    required correction facts, for a ``correction_count`` that is not a positive
    integer, and for an ``error_class`` outside the recognised vocabulary. Never
    repairs the record into a supported shape.
    """
    version = event.get("schema_version")
    if version not in SUPPORTED_ATTEMPT_EVENT_VERSIONS:
        raise UnsupportedAttemptEventVersion(version)
    if event.get("schema_id") != ATTEMPT_EVENT_SCHEMA_ID:
        raise ValueError(
            f"attempt-event schema_id {event.get('schema_id')!r} is not "
            f"{ATTEMPT_EVENT_SCHEMA_ID!r}"
        )
    payload = event.get("payload", {})
    for key in (
        "failure_fingerprint",
        "error_class",
        "hypothesis",
        "changed_input",
        "verifier_result",
        "correction_count",
    ):
        if key not in payload:
            raise ValueError(f"attempt-event payload is missing {key!r}")

    # The bounded correction count is 1-based: it is the number of corrections
    # attempted for this fingerprint, so zero is not a correction and a negative
    # count is meaningless. A bool is rejected even though it is an ``int`` in
    # Python, because ``True`` is not a count.
    count = payload["correction_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(
            f"attempt-event correction_count {count!r} must be a positive "
            f"integer (the bounded correction count is 1-based)"
        )

    # An unrecognised class cannot be routed to a permitted action, so it is
    # refused here rather than defaulted to a permissive one.
    try:
        ErrorClass(payload["error_class"])
    except ValueError as exc:
        raise ValueError(
            f"attempt-event error_class {payload['error_class']!r} is not one "
            f"of {sorted(c.value for c in ErrorClass)}"
        ) from exc


# ═══════════════════════════════════════════════════════════════════════════
# SW-1511-REPAIR criterion 4: the attempt-condition-v1 control record.
# ═══════════════════════════════════════════════════════════════════════════
#
# A provider quota with no *permitted* fallback is not repaired and does not
# switch routes/credentials (that would be an authority expansion).  The one
# control decision is to store the provider's reset/backoff window, wait (the
# non-terminal blocked_waiting_for_gate state, so cancellation stays available)
# and resume the SAME owned session on the SAME route.

#: The frozen attempt-condition contract this module produces control records
#: under.  The baseline (SW-1511-BASE) did not freeze a canonical URI, so the
#: URI/revision/digest are frozen here, next to the class that implements it,
#: and a consumer refuses any other version rather than guessing the shape.
ATTEMPT_CONDITION_SCHEMA_ID = (
    "https://skillweave.dev/schemas/execution/attempt-condition/v1"
)
ATTEMPT_CONDITION_SCHEMA_VERSION = "1.0.0"
SUPPORTED_ATTEMPT_CONDITION_VERSIONS = frozenset({ATTEMPT_CONDITION_SCHEMA_VERSION})

#: The single control decision this contract represents today: store the
#: provider reset window and resume the same owned session on the same route.
#: ``hold`` is the fail-closed alternative (local budget exhaustion, or a
#: provider quota with no storable future window).  It is a closed set so an
#: unknown decision fails closed rather than being executed.
CONDITION_RESUME_SAME_ROUTE = "resume_same_route"
CONDITION_HOLD = "hold"
CONDITION_DECISIONS = (CONDITION_RESUME_SAME_ROUTE, CONDITION_HOLD)


class UnsupportedAttemptConditionVersion(ValueError):
    """An attempt condition carries a version this consumer does not support."""

    def __init__(self, version: Any):
        self.version = version
        super().__init__(
            f"unsupported attempt-condition schema_version {version!r}; expected "
            f"one of {sorted(SUPPORTED_ATTEMPT_CONDITION_VERSIONS)}"
        )


class AttemptConditionKind(str, Enum):
    """The typed condition an attempt is in, control-policy-wise.

    Criterion 4 is deliberately narrower than :class:`ErrorClass`: it covers
    exactly the *provider-quota-with-no-permitted-fallback* condition (store
    reset/backoff, resume the same owned session on the same route) and the
    local, non-provider *budget exhaustion* (store nothing, HOLD — there is no
    reset interval to store).  It must not become a second, drifting error
    vocabulary: any other class is refused rather than mapped onto a wait.
    """

    PROVIDER_QUOTA = "provider_quota"
    BUDGET_EXHAUSTED = "budget_exhausted"


#: The error classes this contract accepts, mapped to their condition kind.
#: Anything outside this closed set is refused (see
#: :func:`attempt_condition_for_quota`) so the contract stays exactly the
#: quota-vs-local-budget distinction criterion 4 governs.
_CONDITION_KINDS: dict[str, AttemptConditionKind] = {
    ErrorClass.PROVIDER_QUOTA.value: AttemptConditionKind.PROVIDER_QUOTA,
    ErrorClass.BUDGET_EXHAUSTED.value: AttemptConditionKind.BUDGET_EXHAUSTED,
    ErrorClass.AUTH_QUOTA.value: AttemptConditionKind.PROVIDER_QUOTA,
}


@dataclass(frozen=True)
class AttemptCondition:
    """An ``attempt-condition-v1`` control record (SW-1511-REPAIR criterion 4).

    The record is the *producer's* (harness/router/repair-classifier) typed
    statement of the control decision for one owned attempt, for the *durable
    controller* (its declared consumer) to persist and act on.  It carries the
    classification, the state the run waits in, whether cancellation is still
    available, the fallback disposition, and — when the decision is to wait —
    the stored reset/backoff window.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be a supported version;
    * the class must be one this contract governs (a provider quota or a local
      budget exhaustion) — never a repair that belongs to another class;
    * a decided *wait* must carry a stored ``reset_at`` in the future (an
      undatable or already-elapsed window is never reported as waiting);
    * a non-wait decision carries no fabricated window;
    * the resume target is the *same* owned session on the *same* route, never
      a substituted one;
    * a waiting attempt is cancellable and waits in the non-terminal
      ``blocked_waiting_for_gate`` state.
    """

    condition: str
    error_class: str
    action: str
    decision: str
    run_id: str = ""
    task_id: str = ""
    attempt: int = 1
    session_id: str = ""
    route: str = ""
    reset_at: Optional[str] = None
    backoff_seconds: Optional[float] = None
    resume_attempt: Optional[int] = None
    resume_route: str = ""
    fallback_permitted: bool = False
    fallback_used: bool = False
    cancellable: bool = True
    run_state: str = "blocked_waiting_for_gate"
    measured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: str = ATTEMPT_CONDITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != ATTEMPT_CONDITION_SCHEMA_VERSION:
            raise UnsupportedAttemptConditionVersion(self.schema_version)
        kind = _CONDITION_KINDS.get(self.error_class)
        if kind is None:
            raise ValueError(
                f"attempt-condition error_class {self.error_class!r} is not one "
                f"this contract governs (expected one of "
                f"{sorted(_CONDITION_KINDS)})"
            )
        if self.condition != kind.value:
            raise ValueError(
                f"attempt-condition condition {self.condition!r} does not match "
                f"the error_class {self.error_class!r} -> {kind.value!r}"
            )
        if self.action != ALLOWED_ACTION[ErrorClass(self.error_class)].value:
            raise ValueError(
                f"attempt-condition action {self.action!r} is not the permitted "
                f"action for {self.error_class!r}"
            )
        if self.decision not in CONDITION_DECISIONS:
            raise ValueError(
                f"attempt-condition decision {self.decision!r} is not one of "
                f"{list(CONDITION_DECISIONS)}"
            )
        # A permitted fallback is never inferred and is only ever named.
        if self.fallback_used:
            raise ValueError(
                "attempt-condition never records a fallback use: substituting a "
                "route/credential/provider is an authority expansion"
            )
        if self.fallback_permitted:
            raise ValueError(
                "attempt-condition only governs the no-permitted-fallback case"
            )

        waiting = self.decision == CONDITION_RESUME_SAME_ROUTE
        if waiting and self.condition != AttemptConditionKind.PROVIDER_QUOTA.value:
            raise ValueError(
                "only a provider-quota condition may resume the same route"
            )
        if waiting:
            if not isinstance(self.reset_at, str) or not self.reset_at.strip():
                raise ValueError(
                    "a provider-quota wait must store the reset/backoff window "
                    "(reset_at); an undated wait is never reported"
                )
            reset = _parse_iso(self.reset_at)
            if reset is None:
                raise ValueError(
                    f"attempt-condition reset_at {self.reset_at!r} is not an ISO "
                    f"8601 timestamp"
                )
            measured = _parse_iso(self.measured_at) or reset
            if reset <= measured:
                raise ValueError(
                    f"attempt-condition reset_at {self.reset_at!r} is not in the "
                    f"future of measured_at {self.measured_at!r}"
                )
            if self.backoff_seconds is None or self.backoff_seconds <= 0:
                raise ValueError(
                    "a provider-quota wait must carry a positive backoff_seconds"
                )
            if not self.route:
                raise ValueError(
                    "a provider-quota wait must name the owned route to resume on"
                )
            if not self.session_id:
                raise ValueError(
                    "a provider-quota wait must name the owned session to resume"
                )
            if self.resume_route != self.route:
                raise ValueError(
                    f"attempt-condition resumes on the SAME route; "
                    f"resume_route {self.resume_route!r} != route {self.route!r}"
                )
            if self.resume_attempt is None or self.resume_attempt != self.attempt:
                raise ValueError(
                    "attempt-condition resumes the SAME owned attempt "
                    "(resume_attempt == attempt)"
                )
            if not self.cancellable:
                raise ValueError(
                    "cancellation remains available while waiting: a waiting "
                    "attempt-condition must be cancellable"
                )
            from .store import RunStateModel

            if self.run_state != RunStateModel.BLOCKED_WAITING_FOR_GATE.value:
                raise ValueError(
                    f"a waiting attempt-condition must sit in "
                    f"{RunStateModel.BLOCKED_WAITING_FOR_GATE.value!r}, not "
                    f"{self.run_state!r}"
                )
            if RunStateModel.is_terminal(self.run_state):
                raise ValueError(
                    "a waiting attempt-condition is never terminal"
                )
        else:
            # Local budget exhaustion: no reset interval exists to store, so a
            # fabricated window would misrepresent a HOLD as a scheduled wait.
            if self.reset_at is not None or self.backoff_seconds is not None:
                raise ValueError(
                    "a local budget exhaustion stores no reset/backoff window"
                )
            if self.resume_attempt is not None or self.resume_route:
                raise ValueError(
                    "a local budget exhaustion does not schedule a same-route "
                    "resume"
                )

    @property
    def waiting(self) -> bool:
        """True only when the stored decision is to wait on a stored window."""
        return self.decision == CONDITION_RESUME_SAME_ROUTE

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": ATTEMPT_CONDITION_SCHEMA_ID,
            "schema_version": self.schema_version,
            "condition": self.condition,
            "error_class": self.error_class,
            "action": self.action,
            "decision": self.decision,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "session_id": self.session_id,
            "route": self.route,
            "reset_at": self.reset_at,
            "backoff_seconds": self.backoff_seconds,
            "resume_attempt": self.resume_attempt,
            "resume_route": self.resume_route,
            "fallback_permitted": self.fallback_permitted,
            "fallback_used": self.fallback_used,
            "cancellable": self.cancellable,
            "run_state": self.run_state,
            "measured_at": self.measured_at,
        }

    def to_attempt_condition(
        self, *, principal: str = "", issuer: str = "repair-classifier"
    ) -> dict[str, Any]:
        """Export this condition as a full ``attempt-condition-v1`` record.

        The envelope carries the frozen schema id/version, the identity
        (run/task/attempt/session/route), the decision, and an
        ``evidence_digest`` over the payload so the durable controller can bind
        the record it acts on.  Validate it with
        :func:`validate_attempt_condition`.
        """
        payload = self.to_dict()
        return {
            "schema_id": ATTEMPT_CONDITION_SCHEMA_ID,
            "schema_version": ATTEMPT_CONDITION_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "session_id": self.session_id,
            "route": self.route,
            "decision": self.decision,
            "condition": self.condition,
            "measured_at": self.measured_at,
            "issuer": issuer,
            "principal": principal,
            "evidence_digest": digest_target(payload),
            "payload": payload,
        }


def validate_attempt_condition(record: Mapping[str, Any]) -> None:
    """Refuse an ``attempt-condition-v1`` record this consumer cannot support.

    Raises :class:`UnsupportedAttemptConditionVersion` for an unknown version
    (or an unknown schema id); raises :class:`ValueError` for a record whose
    declared decision conflicts with the stored window (a wait without a reset,
    a resume onto a substituted route) or whose evidence digest does not bind
    its payload.  Never repairs the record into a supported shape.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_ATTEMPT_CONDITION_VERSIONS:
        raise UnsupportedAttemptConditionVersion(version)
    if record.get("schema_id") != ATTEMPT_CONDITION_SCHEMA_ID:
        raise ValueError(
            f"attempt-condition schema_id {record.get('schema_id')!r} is not "
            f"{ATTEMPT_CONDITION_SCHEMA_ID!r}"
        )
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("attempt-condition payload must be a mapping")

    # Reconstructing the frozen record re-runs every fail-closed invariant, so a
    # tampered decision (a wait without a store, a substituted resume route) is
    # refused here rather than trusted.
    condition = AttemptCondition(
        condition=payload.get("condition", ""),
        error_class=payload.get("error_class", ""),
        action=payload.get("action", ""),
        decision=payload.get("decision", ""),
        run_id=payload.get("run_id", ""),
        task_id=payload.get("task_id", ""),
        attempt=payload.get("attempt", 1),
        session_id=payload.get("session_id", ""),
        route=payload.get("route", ""),
        reset_at=payload.get("reset_at"),
        backoff_seconds=payload.get("backoff_seconds"),
        resume_attempt=payload.get("resume_attempt"),
        resume_route=payload.get("resume_route", ""),
        fallback_permitted=bool(payload.get("fallback_permitted", False)),
        fallback_used=bool(payload.get("fallback_used", False)),
        cancellable=bool(payload.get("cancellable", True)),
        run_state=payload.get("run_state", ""),
        measured_at=payload.get("measured_at", ""),
        schema_version=record["schema_version"],
    )
    if record.get("evidence_digest") != digest_target(condition.to_dict()):
        raise ValueError(
            "attempt-condition evidence_digest does not bind its payload"
        )


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returned timezone-aware (UTC when naive)."""
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


def attempt_condition_for_quota(
    *,
    error_class: Optional[str] = None,
    error: Optional[BaseException] = None,
    message: str = "",
    run_id: str = "",
    task_id: str = "",
    attempt: int = 1,
    session_id: str = "",
    route: str = "",
    fallback_permitted: bool = False,
    reset_at: Optional[str] = None,
    backoff_seconds: Optional[float] = None,
    now: Optional[datetime] = None,
) -> AttemptCondition:
    """The ``attempt-condition-v1`` decision for a refused owned attempt.

    Classifies the presented refusal (via :func:`classify_error`, so the
    SW-1511-REPAIR distinct typed classes are preserved rather than re-derived)
    and returns the control decision the durable controller persists:

    * a **provider quota** with no permitted fallback stores the reset/backoff
      window and *waits*, then resumes the **same owned session on the same
      route** (never a substituted one); cancellation stays available;
    * a **local budget exhaustion** stores nothing and holds.

    Fail-closed: the condition is *waiting only when a real reset window could
    be stored* — a caller that presents ``reset_at`` in the past (or none at
    all) for a provider quota does not receive a fabricated wait; the decision
    degrades to a non-waiting HOLD whose ``reset_at`` is ``None`` (the caller
    must still repair the caller without ever sleeping forever).  Any class
    outside the provider-quota/budget pair is refused: this contract never
    widens into a repair the repair layer does not own.
    """
    classified = (error_class or "").strip()
    if not classified:
        classified = classify_error(error=error, message=message)
    kind = _CONDITION_KINDS.get(classified)
    if kind is None:
        raise ValueError(
            f"attempt-condition does not govern error_class {classified!r}; "
            f"expected one of {sorted(set(_CONDITION_KINDS))}"
        )

    action = ALLOWED_ACTION[ErrorClass(classified)].value
    # The clock the record is measured at, so an injected ``now`` and the
    # recorded ``measured_at`` cannot disagree (a wait is dated against the same
    # reference the caller reasoned with).
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    base = dict(
        condition=kind.value,
        error_class=classified,
        action=action,
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        session_id=session_id,
        route=route,
        fallback_permitted=fallback_permitted,
        measured_at=reference.isoformat(),
    )

    if kind is AttemptConditionKind.BUDGET_EXHAUSTED:
        return AttemptCondition(**base, decision=CONDITION_HOLD)

    # Provider quota: a wait is only recorded when a real, future window is
    # stored.  Otherwise the decision degrades to a non-waiting HOLD (fail
    # closed) rather than fabricating a schedule the provider never gave.
    stored_reset: Optional[str] = None
    stored_backoff: Optional[float] = None
    if reset_at is not None or backoff_seconds is not None:
        reset_dt = _parse_iso(reset_at) if reset_at is not None else None
        if reset_dt is None and backoff_seconds is not None:
            reset_dt = reference + timedelta(seconds=float(backoff_seconds))
        if reset_dt is not None and reset_dt > reference:
            stored_reset = reset_dt.isoformat()
            stored_backoff = (
                float(backoff_seconds)
                if backoff_seconds is not None
                else (reset_dt - reference).total_seconds()
            )

    if stored_reset is None:
        # No storable future window: HOLD, do not claim a wait.  ``reset_at`` /
        # ``backoff_seconds`` are left ``None`` so nothing fabricates a schedule.
        return AttemptCondition(
            **base,
            decision=CONDITION_HOLD,
            resume_attempt=None,
            resume_route="",
        )

    return AttemptCondition(
        **base,
        decision=CONDITION_RESUME_SAME_ROUTE,
        reset_at=stored_reset,
        backoff_seconds=stored_backoff,
        resume_attempt=attempt,
        resume_route=route,
    )


@dataclass
class RepairOutcome:
    """Result of a bounded repair run.

    ``error_class`` is the classified failure (one of :class:`ErrorClass`) and
    ``action_taken`` the single permitted action that was executed for it
    (``remediate``/``reschedule``/``split``/``hold``). ``attempt_event`` is the
    frozen ``attempt-event-v1`` correction record for the deciding attempt.
    """

    repaired: bool
    hold_reason: Optional[str] = None
    attempts: list[RepairAttempt] = field(default_factory=list)
    repaired_plan: Optional[dict[str, Any]] = None
    dispatch_identity: Optional[str] = None
    target_digest: str = ""
    error_class: str = ""
    action_taken: str = ""
    attempt_event: Optional[dict[str, Any]] = None

    @property
    def held(self) -> bool:
        return not self.repaired


#: Candidate-world evidence the repairer is allowed to look at. A path is not
#: invented: only a path that appears in this world may be substituted.
def _offending_path(failure: PreflightFailure) -> Optional[str]:
    """The single declared path the failure is about, from its evidence."""
    for item in failure.evidence:
        if item.get("kind") in ("missing_target", "language_conflict"):
            path = item.get("path")
            if isinstance(path, str) and path:
                return path
    return None


def _candidate_paths(evidence: list[dict[str, Any]]) -> list[str]:
    """Grounded replacement paths, never the offending path itself."""
    paths: list[str] = []
    for item in evidence:
        if item.get("kind") != "grounding_candidate":
            continue
        candidate = item.get("path")
        if isinstance(candidate, str) and candidate:
            paths.append(candidate)
    return paths


#: Legacy alias kept for callers that only need every path mentioned.
def _evidence_paths(evidence: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for item in evidence:
        candidate = item.get("path") or item.get("resolved") or item.get("candidate")
        if isinstance(candidate, str) and candidate:
            paths.append(candidate)
    return paths


def _lane_field(plan: dict[str, Any], field_name: str) -> Optional[list[str]]:
    """Read a lane's list field (``modifies``/``creates``/...) from a task."""
    lane = plan.get("lane")
    if not isinstance(lane, dict):
        return None
    value = lane.get(field_name)
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


def _is_auto_repairable(failure: PreflightFailure) -> bool:
    return failure.retryability == Retryability.AUTO_REPAIRABLE.value


def _apply_repair(
    plan: dict[str, Any],
    failure: PreflightFailure,
    evidence: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Apply a bounded edit to ONE implicated field of a plan.

    Returns a NEW plan (never mutates the input) or ``None`` when the failure
    is not one this engine knows how to repair. Only the offending ENTRY inside
    the fields named in ``failure.implicated_fields`` is swapped for grounded
    evidence; every other entry, and every other field — task intent,
    acceptance criteria, write scope — is copied through untouched.
    """
    if failure.failure_class not in (
        FailureClass.NONEXISTENT_MODIFIES_PATH.value,
        FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value,
    ):
        return None

    offending = _offending_path(failure)
    if not offending:
        return None
    replacement = _candidate_paths(evidence)
    if not replacement:
        return None
    # A repair is a 1:1 substitution: the single offending entry is swapped for
    # the single best grounded candidate. Substituting *all* candidates (or any
    # number other than one) would widen the declared write set, which the
    # bounded repair must never do.
    substitute = replacement[0]

    for lane_field in failure.implicated_fields:
        declared = _lane_field(plan, lane_field)
        if declared is None:
            continue
        new_field: list[str] = []
        for path in declared:
            new_field.append(substitute if path == offending else path)
        # Dropping duplicates keeps the write set from growing if the candidate
        # was already declared elsewhere in the same field.
        new_field = list(dict.fromkeys(new_field))
        if new_field == declared:
            continue
        repaired = dict(plan)
        lane = dict(plan.get("lane", {}))
        lane[lane_field] = new_field
        repaired["lane"] = lane
        return repaired
    return None


class BoundedRepairer:
    """Repair + revalidate + redispatch, bounded by fingerprint/budget/authority.

    ``authorize`` is a predicate ``(role, capability, failure) -> bool`` that
    answers whether the acting role may apply this repair. The default is
    deliberately conservative: every repair requires ``can_mutate_run_state``,
    so a read-only role can never cause a repair to be applied.
    """

    def __init__(
        self,
        *,
        max_attempts: Optional[int] = 1,
        max_correction_rounds: int = 0,
        authorize: Optional[Callable[[str, str, PreflightFailure], bool]] = None,
        journal: Any = None,
        journal_run_id: str = "repair",
        controller: Any = None,
        scope: str = "",
    ):
        # SW-1511-REPAIR (criterion 2): the default remains a finite attempt
        # ceiling, but an *explicitly authorized unconstrained* run may pass
        # ``max_attempts=None`` to declare "no hard cumulative attempt ceiling".
        # This does NOT disable the finite owner envelope: the Assay deadline/
        # budget (``AssayEnvelope``) is enforced independently at the admission
        # seam, and a progress-free loop still holds on a repeated fingerprint.
        # A bool is refused even though it is an ``int`` in Python.
        if max_attempts is not None and (
            isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be None or an int >= 1")
        if max_correction_rounds < 0:
            raise ValueError("max_correction_rounds must be >= 0")
        self.max_attempts = max_attempts
        self.max_correction_rounds = max_correction_rounds
        self._authorize = authorize
        self._journal = journal
        self._journal_run_id = journal_run_id
        #: The bound Assay controller (``AssayRepairController``) that gates the
        #: classified action. ``None`` is the unit-level repair path, where the
        #: local bounded checks still apply but no Assay policy is consulted.
        self._controller = controller
        self._scope = scope
        self._role = ""
        self.attempts: list[RepairAttempt] = []

    def _authorized(self, role: str, capability: str, failure: PreflightFailure) -> bool:
        if self._authorize is None:
            return False
        return bool(self._authorize(role, capability, failure))

    def _record(self, attempt: RepairAttempt) -> None:
        self.attempts.append(attempt)
        if self._journal is not None:
            self._journal.append(
                run_id=self._journal_run_id,
                event_type="semantic_repair_attempt",
                payload=attempt.to_dict(),
                idempotency_key=(
                    f"repair:{attempt.attempt}:{attempt.failure_fingerprint}"
                ),
            )

    def seed_prior_attempts(self, payloads: list[dict[str, Any]]) -> None:
        """Load earlier attempts for this run into the loop's memory.

        The repairer is deliberately short-lived; the append-only journal is
        the durable ledger. Seeding from it makes the repeated-fingerprint and
        attempt/budget holds survive across calls — otherwise a caller that
        builds a fresh repairer per iteration could repair and re-dispatch the
        same failure forever.
        """
        fields = set(RepairAttempt.__dataclass_fields__)
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            known = {k: v for k, v in payload.items() if k in fields}
            try:
                self.attempts.append(RepairAttempt(**known))
            except TypeError:
                continue

    def _correction_count(self, fingerprint: str) -> int:
        """The bounded 1-based count of corrections for *fingerprint*.

        Reads the prior attempts (durably seeded from the journal) so the
        count is real evidence of how many times this failure has been
        corrected — not a per-call constant.
        """
        return sum(
            1 for a in self.attempts if a.failure_fingerprint == fingerprint
        ) + 1

    def _hypothesis(self, failure: PreflightFailure, error_class: str) -> str:
        """The falsifiable claim a correction for this class answers."""
        fields = ",".join(sorted(failure.implicated_fields))
        action = ALLOWED_ACTION[ErrorClass(error_class)].value
        return (
            f"{error_class}: the {failure.failure_class!r} failure on "
            f"[{fields}] is corrected by a {action} of bounded width and "
            f"re-passes preflight against the unchanged target digest without "
            f"widening write scope or altering acceptance criteria"
        )

    def _controller_decision(
        self,
        *,
        error_class: str,
        requested_action: str,
        description: str,
        failure: Optional[PreflightFailure],
    ) -> Optional[dict[str, Any]]:
        """Route a classified action through the Assay controller, if bound.

        Returns the controller's decision dict, or ``None`` when no controller
        is bound (the unit-level repair path). The controller — not this
        repairer — decides whether the action is permitted under the autonomy
        policy and the admission gates.
        """
        if self._controller is None:
            return None
        from .assay import RepairActionRequest  # local: avoid an import cycle

        request = RepairActionRequest(
            error_class=error_class,
            action=requested_action,
            description=description,
            scope=self._scope,
            metadata={
                "failure_fingerprint": failure.fingerprint if failure else "",
                "failure_class": failure.failure_class if failure else "",
                "role": self._role,
            },
        )
        decision = self._controller.act(request)
        return {
            "approved": decision.approved,
            "reason": decision.reason,
            "requires_human": decision.requires_human,
        }

    def _finalize_hold(
        self,
        *,
        failure: PreflightFailure,
        hold_reason: str,
        error_class: str,
        plan_digest: str,
        current_target_digest: str,
        command: str,
        after_digest: Optional[str] = None,
        verifier_result: str = "held",
        changed_input: Optional[dict[str, Any]] = None,
        run_id: str = "",
        task_id: str = "",
    ) -> RepairOutcome:
        """Record a held correction with its full classified evidence."""
        action = ALLOWED_ACTION[ErrorClass(error_class)].value
        attempt = RepairAttempt(
            attempt=len(self.attempts) + 1,
            failure_fingerprint=failure.fingerprint,
            failure_class=failure.failure_class,
            implicated_fields=list(failure.implicated_fields),
            before_digest=plan_digest,
            after_digest=after_digest or plan_digest,
            command=command,
            exit_code=1,
            outcome="held",
            hold_reason=hold_reason,
            error_class=error_class,
            hypothesis=self._hypothesis(failure, error_class),
            changed_input=changed_input,
            verifier_result=verifier_result,
            correction_count=self._correction_count(failure.fingerprint),
        )
        self._record(attempt)
        return RepairOutcome(
            repaired=False,
            hold_reason=hold_reason,
            attempts=list(self.attempts),
            target_digest=current_target_digest,
            error_class=error_class,
            action_taken=action,
            attempt_event=attempt.to_attempt_event(run_id=run_id, task_id=task_id),
        )

    def repair(
        self,
        plan: dict[str, Any],
        failure: Optional[PreflightFailure],
        *,
        role: str,
        current_target_digest: str,
        evidence: Optional[list[dict[str, Any]]] = None,
        command: str = "promptchain-validate",
        revalidate: Optional[Callable[[dict[str, Any]], PreflightResult]] = None,
        redispatch: Optional[Callable[[dict[str, Any]], str]] = None,
    ) -> RepairOutcome:
        """Attempt a bounded repair of ``plan`` for ``failure``.

        ``current_target_digest`` is the digest of the target as it is NOW. If
        it differs from ``failure.target_digest`` the loop holds: repairing
        against a moved target could apply a stale fix.

        Every return path carries the classified ``error_class`` and the single
        permitted ``action_taken``, and records an ``attempt-event-v1``
        correction with the fingerprint, hypothesis, changed input, verifier
        result and bounded correction count.
        """
        evidence = evidence or (failure.evidence if failure else [])
        plan_digest = digest_target(plan)
        target = failure.target_digest if failure else current_target_digest
        self._role = role

        if failure is None:
            return RepairOutcome(
                repaired=True, repaired_plan=plan, target_digest=current_target_digest
            )

        prior_fingerprints = [a.failure_fingerprint for a in self.attempts]

        # --- target drift: the ground moved under the diagnosis -------------
        if current_target_digest != failure.target_digest:
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.TARGET_DRIFT.value,
                error_class=ErrorClass.SOURCE_DRIFT.value,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                verifier_result="target_digest_moved",
            )

        # --- repeated fingerprint: we already tried this exact fix ----------
        if failure.fingerprint in prior_fingerprints:
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.REPEATED_FINGERPRINT.value,
                error_class=ErrorClass.REPEATED_SEMANTIC_FAILURE.value,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                verifier_result="repeated_fingerprint",
            )

        # --- attempts / correction-round budget ------------------------------
        # SW-1511-REPAIR: a local control-policy exhaustion is *not* a provider
        # quota. Running out of the mechanical attempt ceiling is the
        # correction-exhausted class (escalate); only the tighter
        # ``max_correction_rounds`` cap is the budget-exhausted class. Neither
        # widens a budget to keep retrying, and neither is an ``auth_quota``.
        attempt_limit = self.max_attempts
        limit_reason = HoldReason.ATTEMPTS_EXHAUSTED.value
        limit_class = ErrorClass.CORRECTION_EXHAUSTED.value
        if self.max_correction_rounds > 0 and (
            attempt_limit is None or self.max_correction_rounds < attempt_limit
        ):
            attempt_limit = self.max_correction_rounds
            limit_reason = HoldReason.BUDGET_EXHAUSTED.value
            limit_class = ErrorClass.BUDGET_EXHAUSTED.value
        if attempt_limit is not None and len(self.attempts) >= attempt_limit:
            return self._finalize_hold(
                failure=failure,
                hold_reason=limit_reason,
                error_class=limit_class,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                verifier_result="budget_exhausted",
            )

        # --- authority expansion ---------------------------------------------
        required = failure.authority_requirement or AUTO_REPAIR_CAPABILITY
        if not self._authorized(role, required, failure):
            decision = self._controller_decision(
                error_class=ErrorClass.CAPABILITY_MISMATCH.value,
                requested_action=RepairAction.HOLD.value,
                description=(
                    f"role {role!r} lacks capability {required!r} to correct "
                    f"{failure.failure_class!r}"
                ),
                failure=failure,
            )
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.AUTHORITY_EXPANSION.value,
                error_class=ErrorClass.CAPABILITY_MISMATCH.value,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                verifier_result=(
                    f"capability {required!r} not held" if decision is None
                    else f"capability {required!r} not held; controller={decision['reason']}"
                ),
            )

        # --- non-repairable failure class ------------------------------------
        if not _is_auto_repairable(failure):
            error_class = classify_error(
                failure=failure, prior_fingerprints=prior_fingerprints
            )
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.NON_REPAIRABLE_CLASS.value,
                error_class=error_class,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                verifier_result="not_auto_repairable",
            )

        # --- the actual bounded edit -----------------------------------------
        repaired_plan = _apply_repair(plan, failure, evidence)
        if repaired_plan is None:
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.IMPLICATED_FIELD_UNKNOWN.value,
                error_class=ErrorClass.MALFORMED_ARTIFACT.value,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                verifier_result="no_grounded_substitution_available",
            )

        after_digest = digest_target(repaired_plan)
        changed_input = self._changed_input(plan, repaired_plan, failure)

        # --- revalidate against the UNCHANGED target digest ------------------
        revalidated: Optional[PreflightResult] = None
        if revalidate is not None:
            revalidated = revalidate(repaired_plan)
        if revalidated is not None and not revalidated.passed:
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.NON_REPAIRABLE_CLASS.value,
                error_class=ErrorClass.MALFORMED_ARTIFACT.value,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                after_digest=after_digest,
                verifier_result="revalidation_failed",
                changed_input=changed_input,
            )

        # --- the Assay controller gate ---------------------------------------
        # A permitted reversible remediation is published to the Assay
        # controller, where the autonomy policy (Unicorn) and the admission
        # gates decide whether it may run. A controller hold is final: the
        # bounded correction is recorded but not redispatched.
        decision = self._controller_decision(
            error_class=ErrorClass.MALFORMED_ARTIFACT.value,
            requested_action=RepairAction.REMEDIATE.value,
            description=(
                f"bounded 1:1 remediation of {failure.failure_class!r} on "
                f"{sorted(failure.implicated_fields)}"
            ),
            failure=failure,
        )
        if decision is not None and not decision["approved"]:
            return self._finalize_hold(
                failure=failure,
                hold_reason=HoldReason.CONTROLLER_HELD.value,
                error_class=ErrorClass.MALFORMED_ARTIFACT.value,
                plan_digest=plan_digest,
                current_target_digest=current_target_digest,
                command=command,
                after_digest=after_digest,
                verifier_result=f"controller held: {decision['reason']}",
                changed_input=changed_input,
            )

        dispatch_identity = redispatch(repaired_plan) if redispatch else None
        attempt = RepairAttempt(
            attempt=len(self.attempts) + 1,
            failure_fingerprint=failure.fingerprint,
            failure_class=failure.failure_class,
            implicated_fields=list(failure.implicated_fields),
            before_digest=plan_digest,
            after_digest=after_digest,
            command=command,
            exit_code=0,
            outcome="repaired",
            dispatch_identity=dispatch_identity,
            error_class=ErrorClass.MALFORMED_ARTIFACT.value,
            hypothesis=self._hypothesis(
                failure, ErrorClass.MALFORMED_ARTIFACT.value
            ),
            changed_input=changed_input,
            verifier_result=(
                "revalidation_passed" if revalidated is not None
                else "revalidation_not_requested"
            ),
            correction_count=self._correction_count(failure.fingerprint),
        )
        self._record(attempt)
        return RepairOutcome(
            repaired=True,
            attempts=list(self.attempts),
            repaired_plan=repaired_plan,
            dispatch_identity=dispatch_identity,
            target_digest=target,
            error_class=ErrorClass.MALFORMED_ARTIFACT.value,
            action_taken=RepairAction.REMEDIATE.value,
            attempt_event=attempt.to_attempt_event(
                run_id=self._journal_run_id
            ),
        )

    @staticmethod
    def _changed_input(
        before: dict[str, Any],
        after: dict[str, Any],
        failure: PreflightFailure,
    ) -> dict[str, Any]:
        """The exact lane-field substitution a correction applied.

        Only the implicated lane fields are diffed, so the record proves the
        bounded width of the change (task intent, acceptance criteria and every
        other field are provably untouched).
        """
        changed: dict[str, Any] = {}
        before_lane = before.get("lane", {})
        after_lane = after.get("lane", {})
        for field_name in failure.implicated_fields:
            b = before_lane.get(field_name) if isinstance(before_lane, dict) else None
            a = after_lane.get(field_name) if isinstance(after_lane, dict) else None
            if a != b:
                changed[field_name] = {"from": b, "to": a}
        return changed


# ---------------------------------------------------------------------------
# Grounding evidence for the two auto-repairs
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__"})

#: Fields a plan may declare a language in.
_LANGUAGE_FIELDS = ("language", "source_language")


def language_of(path: str) -> Optional[str]:
    """Return the language a source path belongs to, or ``None`` if unknown."""
    from ..grounding.scanner import GroundingScanner

    suffix = Path(path).suffix.lower()
    return GroundingScanner.LANG_EXTENSIONS.get(suffix)


def collect_grounding_evidence(
    repo_root: Any, *, max_files: int = 2000
) -> list[str]:
    """Bounded, read-only list of repository-relative file paths.

    This is the candidate world a repair may draw from: a replacement path must
    already exist here, it is never invented.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            rel = Path(dirpath, name).relative_to(root).as_posix()
            found.append(rel)
            if len(found) >= max_files:
                return found
    return found


def _declared_language(task: dict[str, Any]) -> Optional[str]:
    lane = task.get("lane")
    sources = [lane] if isinstance(lane, dict) else []
    sources.append(task)
    for source in sources:
        for field_name in _LANGUAGE_FIELDS:
            value = source.get(field_name) if isinstance(source, dict) else None
            if isinstance(value, str) and value:
                return value
    return None


def _candidates_for_missing(
    path: str, evidence: list[str], *, limit: int = 1
) -> list[str]:
    """Grounded replacements for a missing path, strongest match first.

    A same-basename file anywhere in the repo wins; then a same-suffix file in
    the same directory. Never returns the missing path itself.
    """
    target = Path(path)
    same_name = [p for p in evidence if Path(p).name == target.name and p != path]
    same_dir = [
        p
        for p in evidence
        if Path(p).parent == target.parent
        and Path(p).suffix == target.suffix
        and p != path
    ]
    ordered = list(dict.fromkeys(same_name + same_dir))
    return ordered[:limit]


def _candidates_for_language(
    path: str, declared_language: str, evidence: list[str], *, limit: int = 1
) -> list[str]:
    """Grounded same-stem files whose language matches what the task declares."""
    stem = Path(path).stem
    parent = Path(path).parent
    same_stem = [
        p
        for p in evidence
        if Path(p).stem == stem and language_of(p) == declared_language
    ]
    same_dir = [
        p
        for p in evidence
        if Path(p).parent == parent and language_of(p) == declared_language
    ]
    ordered = list(dict.fromkeys(same_stem + same_dir))
    return ordered[:limit]


def detect_surface_failure(
    task: dict[str, Any],
    repo_root: Any,
    *,
    target_digest: str,
    evidence: Optional[list[str]] = None,
) -> Optional[PreflightFailure]:
    """Diagnose a task's lane surfaces against real grounding evidence.

    Returns a :class:`PreflightFailure` for the first grounded, repairable
    problem — a nonexistent ``modifies`` path or a wrong-language source path —
    or ``None`` when the surfaces are consistent with the repository.
    """
    lane = task.get("lane")
    if not isinstance(lane, dict):
        return None
    root = Path(repo_root)
    world = evidence if evidence is not None else collect_grounding_evidence(root)
    task_id = str(task.get("id", "<no-id>"))

    # 1. wrong-language source path (declared language contradicts the path)
    declared = _declared_language(task)
    if declared:
        for field_name in ("modifies", "creates"):
            for raw in _lane_field(task, field_name) or []:
                actual = language_of(raw)
                if actual is None or actual == declared:
                    continue
                candidates = _candidates_for_language(raw, declared, world)
                grounding = [
                    {
                        "kind": "grounding_candidate",
                        "path": cand,
                        "language": language_of(cand),
                        "source": "grounding_scan",
                    }
                    for cand in candidates
                ]
                return PreflightFailure(
                    failure_class=FailureClass.WRONG_LANGUAGE_SOURCE_PATH.value,
                    target_digest=target_digest,
                    evidence=[
                        {
                            "kind": "language_conflict",
                            "path": raw,
                            "task_id": task_id,
                            "expected_language": declared,
                            "actual_language": actual,
                        },
                        *grounding,
                    ],
                    implicated_fields=[field_name],
                    retryability=Retryability.AUTO_REPAIRABLE.value,
                    authority_requirement=AUTO_REPAIR_CAPABILITY,
                    detail=(
                        f"task {task_id!r} expects {declared!r} but {raw!r} is "
                        f"{actual!r}"
                    ),
                )

    # 2. nonexistent modifies path
    for raw in _lane_field(task, "modifies") or []:
        if (root / raw).is_file():
            continue
        candidates = _candidates_for_missing(raw, world)
        grounding = [
            {"kind": "grounding_candidate", "path": cand, "source": "grounding_scan"}
            for cand in candidates
        ]
        return PreflightFailure(
            failure_class=FailureClass.NONEXISTENT_MODIFIES_PATH.value,
            target_digest=target_digest,
            evidence=[
                {
                    "kind": "missing_target",
                    "path": raw,
                    "task_id": task_id,
                    "detail": f"resolved={(root / raw).as_posix()}",
                },
                *grounding,
            ],
            implicated_fields=["modifies"],
            retryability=Retryability.AUTO_REPAIRABLE.value,
            authority_requirement=AUTO_REPAIR_CAPABILITY,
            detail=f"task {task_id!r} modifies missing path {raw!r}",
        )

    return None


def _journaled_attempts(journal: Any, run_id: str) -> list[dict[str, Any]]:
    """Attempt payloads already recorded for this run, oldest first.

    The journal is the durable ledger; a fresh :class:`BoundedRepairer` is
    short-lived. Reading the ledger back is what keeps the repeated-fingerprint
    and attempt/budget holds meaningful across calls to
    :func:`repair_plan_from_grounding`.
    """
    if journal is None:
        return []
    try:
        events = journal.get_events(run_id)
    except Exception:
        return []
    payloads: list[dict[str, Any]] = []
    for event in events:
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def repair_plan_from_grounding(
    plan: dict[str, Any],
    repo_root: Any,
    *,
    role: str,
    current_target_digest: str,
    authorize: Optional[Callable[[str, str, PreflightFailure], bool]] = None,
    revalidate: Optional[Callable[[dict[str, Any]], PreflightResult]] = None,
    redispatch: Optional[Callable[[dict[str, Any]], str]] = None,
    journal: Any = None,
    journal_run_id: str = "repair",
    max_attempts: Optional[int] = 1,
    max_correction_rounds: int = 0,
    controller: Any = None,
    scope: str = "",
    error: Optional[BaseException] = None,
    budget_exhausted: bool = False,
    classify_only: bool = False,
) -> RepairOutcome:
    """Diagnose + classify + repair a task plan from grounding evidence.

    This is the integration seam: it grounds the failure from the repository,
    classifies it into the :class:`ErrorClass` values, then runs
    :class:`BoundedRepairer` with the allowed action for that class. Prior
    attempts recorded in the journal for this ``journal_run_id`` seed the loop,
    so a failure already seen is held rather than repaired (and re-dispatched)
    again.

    When no *grounded surface* failure is detected the call still classifies any
    ``error``/``budget_exhausted`` signal the caller presents (an auth/quota
    refusal, a context-pressure signal, ...), and for a non-``remediate`` class
    returns the classified HOLD/SPLIT/RESCHEDULE action — a passthrough failure
    must never be silently redispatched as if it were a successful repair. Set
    ``classify_only`` to *always* return the classified action without applying
    a remediation (the classification seam used by the Assay controller).
    """
    target = current_target_digest
    failure = detect_surface_failure(plan, repo_root, target_digest=target)
    repairer = BoundedRepairer(
        max_attempts=max_attempts,
        max_correction_rounds=max_correction_rounds,
        authorize=authorize,
        journal=journal,
        journal_run_id=journal_run_id,
        controller=controller,
        scope=scope,
    )
    repairer.seed_prior_attempts(_journaled_attempts(journal, journal_run_id))
    evidence = failure.evidence if failure else []
    prior_fingerprints = [a.failure_fingerprint for a in repairer.attempts]

    # --- classification-only seam (the Assay controller's classify call) -----
    # Nothing is applied; the caller receives the classified action and must
    # route it through :class:`AssayRepairController` before it can happen.
    if classify_only:
        drifted = failure is not None and failure.target_digest != current_target_digest
        error_class = classify_error(
            failure=failure,
            error=error,
            prior_fingerprints=prior_fingerprints,
            budget_exhausted=budget_exhausted,
        )
        if drifted:
            error_class = ErrorClass.SOURCE_DRIFT.value
        action = ALLOWED_ACTION[ErrorClass(error_class)].value
        hold_reason = (
            None if action == RepairAction.REMEDIATE.value
            else _hold_reason_for_class(error_class)
        )
        return RepairOutcome(
            repaired=False,
            hold_reason=hold_reason,
            attempts=list(repairer.attempts),
            target_digest=current_target_digest,
            error_class=error_class,
            action_taken=action,
        )

    # --- an exhausted budget or a presented refusal outranks a surface repair -
    # An auth/quota condition has no authorized fallback: it is classified and
    # held even when a groundable surface failure is also present.
    if budget_exhausted or (failure is None and error is not None):
        error_class = classify_error(
            failure=failure,
            error=error,
            prior_fingerprints=prior_fingerprints,
            budget_exhausted=budget_exhausted,
        )
        action = ALLOWED_ACTION[ErrorClass(error_class)].value
        return RepairOutcome(
            repaired=False,
            hold_reason=_hold_reason_for_class(error_class),
            attempts=list(repairer.attempts),
            target_digest=current_target_digest,
            error_class=error_class,
            action_taken=action,
        )

    if failure is None:
        # No grounded surface failure was detected. A call with a presented
        # refusal or an exhausted budget was already held above; a call with
        # *nothing* to classify (no surface failure, no error, no recognisable
        # signal) must also fail closed to a HOLD rather than be treated as a
        # successful repair — an unclassifiable failure is never a PASS.
        error_class = classify_error(prior_fingerprints=prior_fingerprints)
        if error_class != ErrorClass.MALFORMED_ARTIFACT.value:
            return RepairOutcome(
                repaired=False,
                hold_reason=_hold_reason_for_class(error_class),
                attempts=list(repairer.attempts),
                target_digest=current_target_digest,
                error_class=error_class,
                action_taken=ALLOWED_ACTION[ErrorClass(error_class)].value,
            )
        return RepairOutcome(
            repaired=True,
            repaired_plan=plan,
            target_digest=current_target_digest,
        )

    return repairer.repair(
        plan,
        failure,
        role=role,
        current_target_digest=current_target_digest,
        evidence=evidence,
        revalidate=revalidate,
        redispatch=redispatch,
    )


def _hold_reason_for_class(error_class: str) -> str:
    """The local hold reason a classified action maps to when not remediated.

    SW-1511-REPAIR: the five condition classes split out of the coarse
    ``auth_quota`` umbrella keep their *own* hold reason so a caller can tell an
    authentication refusal, a provider quota, a spent envelope, a mechanical
    correction exhaustion and a local transport failure apart — each is a HOLD,
    but for a different, causally-evidenced reason.
    """
    if error_class == ErrorClass.AUTH_QUOTA.value:
        return HoldReason.BUDGET_EXHAUSTED.value
    if error_class == ErrorClass.BUDGET_EXHAUSTED.value:
        return HoldReason.BUDGET_EXHAUSTED.value
    if error_class == ErrorClass.CORRECTION_EXHAUSTED.value:
        return HoldReason.ATTEMPTS_EXHAUSTED.value
    if error_class == ErrorClass.PROVIDER_QUOTA.value:
        return HoldReason.BUDGET_EXHAUSTED.value
    if error_class == ErrorClass.AUTH_REFUSAL.value:
        return HoldReason.AUTHORITY_EXPANSION.value
    if error_class == ErrorClass.TRANSPORT_FAILURE.value:
        return HoldReason.NON_REPAIRABLE_CLASS.value
    if error_class == ErrorClass.CAPABILITY_MISMATCH.value:
        return HoldReason.AUTHORITY_EXPANSION.value
    if error_class == ErrorClass.SOURCE_DRIFT.value:
        return HoldReason.TARGET_DRIFT.value
    if error_class == ErrorClass.REPEATED_SEMANTIC_FAILURE.value:
        return HoldReason.REPEATED_FINGERPRINT.value
    if error_class == ErrorClass.CONTEXT_PRESSURE.value:
        return HoldReason.CONTROLLER_HELD.value
    # malformed_artifact is only reached here when its permitted remediation was
    # not applied; unclassified fails closed to the same non-repairable hold.
    return HoldReason.NON_REPAIRABLE_CLASS.value
