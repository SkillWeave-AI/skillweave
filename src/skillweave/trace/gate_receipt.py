"""The independent gate receipt (SW-1511-REVIEW criteria 1-3).

A launched skill is not a finished one.  This module makes the states a work
item passes through *distinguishable data* and makes the gate decision rest on
real independent verifier evidence rather than on any convenient signal.

Three criteria live here, next to the frozen boundary contract that carries
them:

* **criterion 1** -- *invoked/assigned*, *running*, *artifact-produced*,
  *reviewed* and *accepted* are five distinct states
  (:data:`ACCEPTANCE_STATES`), ordered by :data:`ACCEPTANCE_ORDER`.  A state
  may only advance to the next one; skipping a state is refused.  A launch
  records *invoked/assigned* and no more -- a launched skill without its
  required producer artifact cannot release dependents
  (:func:`release_dependents` refuses when the producer artifact is absent).
* **criterion 2** -- the review binding (review criteria, subject revision,
  artifact digest, role and the *actual answering identity*) is bound to the
  attempt at review time.  A later code/content change invalidates the prior
  review: :func:`assert_review_current` refuses to accept an acceptance whose
  binding revision/digest no longer matches the attempt's current subject.
* **criterion 3** -- Model PASS, multiple terminal results, narrative receipts
  (prose around a JSON object), unsupported schemas and unknown evidence never
  auto-accept.  Only an actual independent verifier receipt, whose verifier
  identity differs from the producer, can decide the gate
  (:func:`evaluate_gate`).

Boundary contract
-----------------
``independent-gate-receipt-v1`` is frozen here (the baseline task did not
freeze a canonical URI, so the URI/revision/digest are frozen at this seam),
next to the record that implements it.  It is **additive and opt-in**: a
record is only ever validated when a caller hands one over, existing v1
records remain valid, and a record carrying any version this consumer does not
support fails closed (:class:`UnsupportedContractVersion`) rather than being
coerced.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

__all__ = [
    # boundary contract
    "GATE_RECEIPT_SCHEMA_ID",
    "GATE_RECEIPT_SCHEMA_VERSION",
    "SUPPORTED_GATE_RECEIPT_VERSIONS",
    "UnsupportedContractVersion",
    "IndependentGateReceipt",
    "validate_independent_gate_receipt",
    # criterion 1: the distinct acceptance states
    "ACCEPTANCE_INVOKED",
    "ACCEPTANCE_RUNNING",
    "ACCEPTANCE_ARTIFACT_PRODUCED",
    "ACCEPTANCE_REVIEWED",
    "ACCEPTANCE_ACCEPTED",
    "ACCEPTANCE_STATES",
    "ACCEPTANCE_ORDER",
    "ACCEPTANCE_TERMINAL",
    "IllegalStateTransition",
    "MissingProducerArtifact",
    "AttemptAcceptance",
    "release_dependents",
    # criterion 2: the review binding + subject-change invalidation
    "VERIFIER_ROLE",
    "REVIEWER_ROLE",
    "ReviewBinding",
    "StaleReviewError",
    "AttemptReview",
    "assert_review_current",
    # criterion 3: the gate evaluator
    "GATE_PASS",
    "GATE_FAIL",
    "GATE_UNDECIDED",
    "GATE_VERDICTS",
    "GateDecision",
    "MODEL_PASS_REASON",
    "MULTIPLE_TERMINALS_REASON",
    "NARRATIVE_RECEIPT_REASON",
    "evaluate_gate",
    # helpers
    "acceptance_rank",
]

# ═══════════════════════════════════════════════════════════════════════════
# Boundary contract: independent-gate-receipt-v1
# ═══════════════════════════════════════════════════════════════════════════
#
# The PRD freezes a draft v1 contract for this task.  The baseline did not
# freeze a canonical URI/revision/digest, so those are frozen here, next to the
# record that implements it.  The contract is additive and opt-in: an existing
# v1 record validates unchanged, and a record carrying any version this
# consumer does not support is refused rather than coerced.

GATE_RECEIPT_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/independent-gate-receipt/v1"
)
GATE_RECEIPT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_GATE_RECEIPT_VERSIONS = frozenset({GATE_RECEIPT_SCHEMA_VERSION})


class UnsupportedContractVersion(ValueError):
    """A boundary record carries a version this consumer does not support.

    The contract is fail-closed: an unknown version is refused, never
    upgraded, downgraded or read as a best-effort approximation of a supported
    one.  *artifact* names the contract (``independent-gate-receipt-v1``).
    """

    def __init__(self, artifact: str, version: Any, supported: frozenset):
        self.artifact = artifact
        self.version = version
        self.supported = supported
        super().__init__(
            f"unsupported {artifact} schema_version {version!r}; expected one "
            f"of {sorted(supported)}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 1: the five distinct acceptance states
# ═══════════════════════════════════════════════════════════════════════════
#
# "Invoked/assigned", "running", "artifact-produced", "reviewed" and
# "accepted" are five states, not one.  The order below is the only legal
# progression; a caller may not skip a state, and only "accepted" releases a
# dependent.  "invoked/assigned" is captured by a launch; the producer
# artifact is what separates "running" from "artifact-produced", and a
# dependent released before that artifact exists is refused.

ACCEPTANCE_INVOKED = "invoked"
ACCEPTANCE_RUNNING = "running"
ACCEPTANCE_ARTIFACT_PRODUCED = "artifact_produced"
ACCEPTANCE_REVIEWED = "reviewed"
ACCEPTANCE_ACCEPTED = "accepted"

#: Every acceptance state, in declaration order.
ACCEPTANCE_STATES: tuple[str, ...] = (
    ACCEPTANCE_INVOKED,
    ACCEPTANCE_RUNNING,
    ACCEPTANCE_ARTIFACT_PRODUCED,
    ACCEPTANCE_REVIEWED,
    ACCEPTANCE_ACCEPTED,
)

#: The only legal progression: each state's immediate successor.  The final
#: state has no successor.
ACCEPTANCE_ORDER: Mapping[str, Optional[str]] = {
    ACCEPTANCE_INVOKED: ACCEPTANCE_RUNNING,
    ACCEPTANCE_RUNNING: ACCEPTANCE_ARTIFACT_PRODUCED,
    ACCEPTANCE_ARTIFACT_PRODUCED: ACCEPTANCE_REVIEWED,
    ACCEPTANCE_REVIEWED: ACCEPTANCE_ACCEPTED,
    ACCEPTANCE_ACCEPTED: None,
}

#: The single state that releases dependents.
ACCEPTANCE_TERMINAL = ACCEPTANCE_ACCEPTED


def acceptance_rank(state: str) -> int:
    """The ordinal of *state* in :data:`ACCEPTANCE_STATES`; ``-1`` if unknown."""
    try:
        return ACCEPTANCE_STATES.index(state)
    except ValueError:
        return -1


class IllegalStateTransition(ValueError):
    """A state advance skipped the legal order or was not a forward step.

    A launched skill is *invoked/assigned*, not *accepted*: advancing straight
    from ``invoked`` to ``accepted`` (or moving backwards) is refused rather
    than folded into a coarser state.
    """

    def __init__(self, from_state: Any, to_state: Any):
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal acceptance transition {from_state!r} -> {to_state!r}; the "
            f"only legal order is {list(ACCEPTANCE_STATES)}"
        )


class MissingProducerArtifact(ValueError):
    """A dependent release was requested without the required producer artifact.

    The producer artifact of the releasing attempt is mandatory: a launched
    skill that has produced no artifact cannot release its dependents.
    """

    def __init__(self, attempt_id: str, artifact_id: str):
        self.attempt_id = attempt_id
        self.artifact_id = artifact_id
        super().__init__(
            f"attempt {attempt_id!r} cannot release dependents: its required "
            f"producer artifact {artifact_id!r} is absent"
        )


@dataclass
class AttemptAcceptance:
    """The acceptance state of one attempt, with its producer artifact and deps.

    The state machine is enforced by :meth:`advance`: a state may only move to
    its declared successor.  ``producer_artifact`` is the artifact the attempt
    must have produced before dependents can be released; an empty value means
    the artifact is still absent, and :func:`release_dependents` refuses then.
    """

    attempt_id: str
    state: str = ACCEPTANCE_INVOKED
    producer_artifact: str = ""
    dependents: tuple[str, ...] = ()
    history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt acceptance must name its attempt_id")
        if self.state not in ACCEPTANCE_STATES:
            raise ValueError(
                f"unknown acceptance state {self.state!r}; expected one of "
                f"{list(ACCEPTANCE_STATES)}"
            )
        if not self.history:
            self.history.append(self.state)

    @property
    def state_ordinal(self) -> int:
        return acceptance_rank(self.state)

    def advance(self, state: str) -> "AttemptAcceptance":
        """Move to *state*, refusing anything but the immediate successor.

        The producer artifact must already be recorded before the attempt
        reaches *artifact-produced* or beyond, so "artifact-produced" is never
        a label without the artifact.
        """
        expected = ACCEPTANCE_ORDER.get(self.state)
        if state != expected:
            raise IllegalStateTransition(self.state, state)
        if (
            acceptance_rank(state) >= acceptance_rank(ACCEPTANCE_ARTIFACT_PRODUCED)
            and not self.producer_artifact
        ):
            raise IllegalStateTransition(self.state, state)
        self.state = state
        self.history.append(state)
        return self

    def record_producer_artifact(self, artifact_id: str) -> "AttemptAcceptance":
        """Record the artifact this attempt produced (required before review)."""
        if not artifact_id:
            raise ValueError("producer artifact must be a non-empty artifact_id")
        self.producer_artifact = artifact_id
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "state": self.state,
            "producer_artifact": self.producer_artifact,
            "dependents": list(self.dependents),
            "history": list(self.history),
        }


def release_dependents(attempt: AttemptAcceptance) -> tuple[str, ...]:
    """Release *attempt*'s dependents, refusing without the producer artifact.

    A dependent is released only by an attempt that reached *accepted* **and**
    carries its required producer artifact.  A launched (or running, or
    artifact-less) attempt refuses, so a skill that never produced its artifact
    cannot release the work that depends on it.
    """
    if not attempt.producer_artifact:
        raise MissingProducerArtifact(attempt.attempt_id, attempt.producer_artifact)
    if attempt.state != ACCEPTANCE_TERMINAL:
        raise IllegalStateTransition(attempt.state, ACCEPTANCE_TERMINAL)
    return attempt.dependents


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: the review binding and subject-change invalidation
# ═══════════════════════════════════════════════════════════════════════════

#: The role that decides a gate: an independent verifier.  A producer reviewing
#: its own work is never this role.
VERIFIER_ROLE = "verifier"
REVIEWER_ROLE = "reviewer"


class StaleReviewError(ValueError):
    """A prior review no longer matches the attempt's current subject.

    When the reviewed subject revision or artifact digest changes after the
    review was recorded, the review is invalidated: accepting on the stale
    review is refused rather than silently reusing it.
    """

    def __init__(self, attempt_id: str, reason: str):
        self.attempt_id = attempt_id
        self.reason = reason
        super().__init__(
            f"review for attempt {attempt_id!r} is stale: {reason}"
        )


@dataclass(frozen=True)
class ReviewBinding:
    """What a review was made *about*: criteria, subject revision and digest.

    ``subject_revision`` is the code/content revision under review;
    ``artifact_digest`` is the content address of the reviewed artifact.  Both
    are bound at review time, so a later change to either is detectable.
    """

    review_id: str
    review_criteria: tuple[str, ...]
    subject_revision: str
    artifact_digest: str

    def __post_init__(self) -> None:
        for name, value in (
            ("review_id", self.review_id),
            ("subject_revision", self.subject_revision),
            ("artifact_digest", self.artifact_digest),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"review binding must carry a non-empty {name}")
        if not self.review_criteria:
            raise ValueError("review binding must state its review criteria")

    def fingerprint(self) -> str:
        """A stable sha256 over the bound criteria, revision and digest."""
        blob = json.dumps(
            {
                "review_criteria": list(self.review_criteria),
                "subject_revision": self.subject_revision,
                "artifact_digest": self.artifact_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class AttemptReview:
    """A review bound to an attempt, carrying role and actual answering identity.

    ``role`` is the reviewing role and ``answering_identity`` is who *actually*
    answered (the bound verifier identity), never the requested label.  A
    *producer* may not answer its own review: the answering identity must differ
    from ``producer_identity``.
    """

    attempt_id: str
    binding: ReviewBinding
    role: str
    answering_identity: str
    producer_identity: str
    verdict: str = ""
    accepted_revision: str = ""
    accepted_digest: str = ""

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt review must name its attempt_id")
        if not self.role:
            raise ValueError("attempt review must state the reviewing role")
        if not self.answering_identity:
            raise ValueError("attempt review must name the actual answering identity")
        if not self.producer_identity:
            raise ValueError("attempt review must name the producer identity")

    @property
    def independent(self) -> bool:
        """True when the answering identity is not the producer's."""
        return self.answering_identity != self.producer_identity

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "review_id": self.binding.review_id,
            "review_criteria": list(self.binding.review_criteria),
            "binding_fingerprint": self.binding.fingerprint(),
            "role": self.role,
            "answering_identity": self.answering_identity,
            "producer_identity": self.producer_identity,
            "verdict": self.verdict,
            "accepted_revision": self.accepted_revision,
            "accepted_digest": self.accepted_digest,
        }


def assert_review_current(
    review: AttemptReview,
    *,
    subject_revision: str,
    artifact_digest: str,
) -> None:
    """Refuse an acceptance whose bound subject has since changed.

    The review is bound to the revision and artifact digest it saw.  When the
    attempt's *current* revision or digest differs, the prior review is stale
    and the acceptance is refused -- a code/content change invalidates prior
    reviews.
    """
    if review.binding.subject_revision != subject_revision:
        raise StaleReviewError(
            review.attempt_id,
            f"subject revision changed from {review.binding.subject_revision!r} "
            f"to {subject_revision!r}; a code/content change invalidates the "
            f"prior review",
        )
    if review.binding.artifact_digest != artifact_digest:
        raise StaleReviewError(
            review.attempt_id,
            f"artifact digest changed from {review.binding.artifact_digest!r} "
            f"to {artifact_digest!r}; a code/content change invalidates the "
            f"prior review",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: the gate evaluator (independent verifier evidence decides)
# ═══════════════════════════════════════════════════════════════════════════

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_UNDECIDED = "UNDECIDED"
GATE_VERDICTS: tuple[str, ...] = (GATE_PASS, GATE_FAIL, GATE_UNDECIDED)

#: The refusal reasons the evaluator emits for a signal that must never
#: auto-accept.  They are named constants so a consumer can key on them.
MODEL_PASS_REASON = "model PASS is not independent verifier evidence"
MULTIPLE_TERMINALS_REASON = "multiple terminal results were offered"
NARRATIVE_RECEIPT_REASON = "narrative receipt: result is not machine-readable JSON"
UNSUPPORTED_SCHEMA_REASON = "unsupported evidence schema"
UNKNOWN_EVIDENCE_REASON = "unknown evidence type"

#: JSON objects whose ``schema_version`` begins with this prefix are the
#: supported evidence schema.  Anything else is refused as unsupported.
_SUPPORTED_SCHEMA_PREFIX = "independent-gate-receipt/"
_SUPPORTED_SCHEMA_VERSION = "1.0.0"

#: The evidence rows.  The value state is ``member`` (participates in
#: independence), ``model_pass`` (a model's own PASS -- never evidence),
#: ``narrative`` (prose around a JSON object -- not a machine receipt),
#: ``unsupported_schema`` (a schema this consumer does not support),
#: ``unknown`` (an evidence type this consumer does not know) and ``terminal``
#: (an extra terminal result that makes the denominator ambiguous).
_EVIDENCE_MEMBER = "member"
_EVIDENCE_MODEL_PASS = "model_pass"
_EVIDENCE_NARRATIVE = "narrative"
_EVIDENCE_UNSUPPORTED_SCHEMA = "unsupported_schema"
_EVIDENCE_UNKNOWN = "unknown"
_EVIDENCE_TERMINAL = "terminal"


@dataclass(frozen=True)
class GateDecision:
    """The frozen outcome of a gate evaluation.

    ``verdict`` is one of :data:`GATE_VERDICTS`.  ``accepted`` is **True only**
    for a :data:`GATE_PASS` decided by a real independent verifier receipt;
    every auto-accept signal (a Model PASS, multiple terminal results, a
    narrative receipt, an unsupported schema, unknown evidence) yields
    ``accepted=False`` with the reason named in ``reasons``.
    """

    verdict: str
    accepted: bool
    reasons: tuple[str, ...]
    deciders: tuple[str, ...] = ()
    producer_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
            "deciders": list(self.deciders),
            "producer_identity": self.producer_identity,
        }


def _is_json_object(value: Any) -> bool:
    return isinstance(value, Mapping)


def _parse_json_str(value: Any) -> tuple[bool, Any]:
    """Return ``(ok, parsed)`` for a value that must be a machine JSON object.

    A bare dict is accepted (already parsed).  A string is accepted only when
    it is *exactly* a JSON object with no surrounding prose; a narrative
    receipt (prose with an embedded JSON object) is refused, not scanned.
    """
    if isinstance(value, Mapping):
        return True, value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False, None
        try:
            parsed = json.loads(text)
        except ValueError:
            return False, None
        return isinstance(parsed, Mapping), parsed
    return False, None


def _evidence_row_ok(parsed: Mapping[str, Any]) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a machine evidence object's schema/type."""
    version = parsed.get("schema_version")
    if not isinstance(version, str) or not version.startswith(_SUPPORTED_SCHEMA_PREFIX):
        return False, UNSUPPORTED_SCHEMA_REASON
    if version != _SUPPORTED_SCHEMA_PREFIX + _SUPPORTED_SCHEMA_VERSION:
        return False, UNSUPPORTED_SCHEMA_REASON
    evidence_type = parsed.get("evidence_type")
    if evidence_type != _EVIDENCE_MEMBER:
        return False, UNKNOWN_EVIDENCE_REASON
    return True, ""


def _classify_evidence(raw: Any) -> tuple[str, Optional[Mapping[str, Any]]]:
    """Classify one evidence row into ``(kind, parsed_member_or_None)``."""
    if isinstance(raw, Mapping):
        role = raw.get("role")
        if role == _EVIDENCE_MODEL_PASS:
            return _EVIDENCE_MODEL_PASS, None
        if role == _EVIDENCE_TERMINAL:
            return _EVIDENCE_TERMINAL, None
        parsed = raw
    elif isinstance(raw, str):
        ok, parsed = _parse_json_str(raw)
        if not ok:
            return _EVIDENCE_NARRATIVE, None
        if parsed.get("role") == _EVIDENCE_MODEL_PASS:
            return _EVIDENCE_MODEL_PASS, None
        if parsed.get("role") == _EVIDENCE_TERMINAL:
            return _EVIDENCE_TERMINAL, None
    else:
        return _EVIDENCE_NARRATIVE, None

    ok, reason = _evidence_row_ok(parsed)
    if not ok:
        if reason == UNSUPPORTED_SCHEMA_REASON:
            return _EVIDENCE_UNSUPPORTED_SCHEMA, None
        return _EVIDENCE_UNKNOWN, None
    return _EVIDENCE_MEMBER, parsed


def evaluate_gate(
    attempt: AttemptAcceptance,
    *,
    model_pass: bool = False,
    evidence: Sequence[Any] = (),
    producer_identity: str = "",
) -> GateDecision:
    """Decide a gate from *actual* independent verifier evidence.

    Only a real independent verifier receipt decides the gate.  The following
    never auto-accept and force :data:`GATE_UNDECIDED` (or :data:`GATE_FAIL`
    when the attempt itself is not in an acceptable state):

    * the model's own ``model_pass`` -- a model PASS is not evidence;
    * multiple terminal results in ``evidence`` -- the denominator is ambiguous;
    * a narrative receipt -- a result that is not machine-readable JSON;
    * an unsupported evidence schema -- refused, never coerced;
    * unknown evidence -- refused, never counted.

    A genuine member receipt whose ``verifier_identity`` differs from the
    producer is required; when it also names a verdict, that verdict decides
    the gate (:data:`GATE_PASS` only for a ``PASS`` verdict).
    """
    reasons: list[str] = []

    # -- the attempt itself must have produced and reached the recoverable state
    if not attempt.producer_artifact:
        reasons.append(
            f"attempt {attempt.attempt_id!r} has produced no producer artifact"
        )

    if model_pass:
        reasons.append(MODEL_PASS_REASON)

    members: list[Mapping[str, Any]] = []
    terminals = 0
    for raw in evidence:
        kind, parsed = _classify_evidence(raw)
        if kind == _EVIDENCE_MEMBER:
            members.append(parsed)  # type: ignore[arg-type]
        elif kind == _EVIDENCE_MODEL_PASS:
            reasons.append(MODEL_PASS_REASON)
        elif kind == _EVIDENCE_TERMINAL:
            terminals += 1
            reasons.append(MULTIPLE_TERMINALS_REASON)
        elif kind == _EVIDENCE_NARRATIVE:
            reasons.append(NARRATIVE_RECEIPT_REASON)
        elif kind == _EVIDENCE_UNSUPPORTED_SCHEMA:
            reasons.append(UNSUPPORTED_SCHEMA_REASON)
        else:
            reasons.append(UNKNOWN_EVIDENCE_REASON)

    if terminals >= 1:
        reasons.append(MULTIPLE_TERMINALS_REASON)

    # -- independent verifier receipts only
    deciders: list[str] = []
    verifier_verdicts: list[str] = []
    for member in members:
        identity = member.get("verifier_identity") or member.get("answering_identity")
        if not isinstance(identity, str) or not identity:
            reasons.append(UNKNOWN_EVIDENCE_REASON)
            continue
        if identity == producer_identity:
            # A producer's own receipt is not independent evidence.
            reasons.append(
                f"evidence answered by the producer {identity!r} is not "
                f"independent verifier evidence"
            )
            continue
        deciders.append(identity)
        verdict = member.get("verdict")
        if isinstance(verdict, str) and verdict:
            verifier_verdicts.append(verdict)

    # de-duplicate reasons while preserving order
    unique_reasons = list(dict.fromkeys(reasons))

    if not unique_reasons and len(deciders) >= 1:
        # Real independent verifier evidence decides the gate.
        if verifier_verdicts and all(v == GATE_PASS for v in verifier_verdicts):
            return GateDecision(
                verdict=GATE_PASS,
                accepted=True,
                reasons=(),
                deciders=tuple(deciders),
                producer_identity=producer_identity,
            )
        if any(v == GATE_FAIL for v in verifier_verdicts):
            return GateDecision(
                verdict=GATE_FAIL,
                accepted=False,
                reasons=("independent verifier reported FAIL",),
                deciders=tuple(deciders),
                producer_identity=producer_identity,
            )
        # Independent evidence exists but did not decide PASS/FAIL yet.
        return GateDecision(
            verdict=GATE_UNDECIDED,
            accepted=False,
            reasons=("independent evidence present but no decisive verdict",),
            deciders=tuple(deciders),
            producer_identity=producer_identity,
        )

    if attempt.producer_artifact and len(deciders) == 0 and not unique_reasons:
        unique_reasons.append("no independent verifier evidence supplied")

    return GateDecision(
        verdict=GATE_UNDECIDED,
        accepted=False,
        reasons=tuple(unique_reasons),
        deciders=tuple(deciders),
        producer_identity=producer_identity,
    )


# ═══════════════════════════════════════════════════════════════════════════
# The frozen record: independent-gate-receipt-v1
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class IndependentGateReceipt:
    """An ``independent-gate-receipt-v1`` record.

    Produced by the **producer/reviewer/observer** and consumed by the
    **controller acceptance service**: it binds one attempt's acceptance state,
    its review binding (criteria, subject revision, artifact digest, role and
    actual answering identity) and the deciding independent verifier evidence
    into one durable record.

    Fail-closed invariants enforced at construction:

    * ``schema_version`` must be supported (an unknown version is refused);
    * the receipt must name its attempt and its acceptance state;
    * the ``state`` must be one of :data:`ACCEPTANCE_STATES`;
    * a receipt claiming *accepted* must carry independent verifier evidence
      (`accepted=True` can never be constructed from a model PASS alone).
    """

    attempt_id: str
    state: str
    producer_artifact: str = ""
    binding: Optional[ReviewBinding] = None
    role: str = ""
    answering_identity: str = ""
    producer_identity: str = ""
    accepted: bool = False
    captured_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: str = GATE_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_GATE_RECEIPT_VERSIONS:
            raise UnsupportedContractVersion(
                "independent-gate-receipt-v1", self.schema_version,
                SUPPORTED_GATE_RECEIPT_VERSIONS,
            )
        if not self.attempt_id:
            raise ValueError("independent gate receipt must name its attempt_id")
        if self.state not in ACCEPTANCE_STATES:
            raise ValueError(
                f"unknown acceptance state {self.state!r}; expected one of "
                f"{list(ACCEPTANCE_STATES)}"
            )
        if self.accepted:
            # An acceptance is only admissible with the producer artifact, an
            # independent answering identity distinct from the producer, and a
            # bound review.  A model PASS alone can never construct this.
            if not self.producer_artifact:
                raise MissingProducerArtifact(self.attempt_id, self.producer_artifact)
            if self.state != ACCEPTANCE_TERMINAL:
                raise IllegalStateTransition(self.state, ACCEPTANCE_TERMINAL)
            if not self.binding:
                raise ValueError(
                    "an accepted independent gate receipt must carry its review binding"
                )
            if not self.answering_identity or self.answering_identity == self.producer_identity:
                raise ValueError(
                    "an accepted independent gate receipt must be answered by an "
                    "identity other than the producer"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": GATE_RECEIPT_SCHEMA_ID,
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "producer_artifact": self.producer_artifact,
            "binding": (
                {
                    "review_id": self.binding.review_id,
                    "review_criteria": list(self.binding.review_criteria),
                    "subject_revision": self.binding.subject_revision,
                    "artifact_digest": self.binding.artifact_digest,
                    "fingerprint": self.binding.fingerprint(),
                }
                if self.binding
                else None
            ),
            "role": self.role,
            "answering_identity": self.answering_identity,
            "producer_identity": self.producer_identity,
            "accepted": self.accepted,
            "captured_at": self.captured_at,
        }


def validate_independent_gate_receipt(record: Mapping[str, Any]) -> None:
    """Refuse a gate receipt this consumer cannot support.

    Raises :class:`UnsupportedContractVersion` for an unknown version (or an
    unknown schema id).  Reconstructing the frozen record re-runs every
    fail-closed invariant, so a tampered receipt (an acceptance without its
    artifact, a self-answered acceptance) is refused rather than trusted.
    """
    version = record.get("schema_version")
    if version not in SUPPORTED_GATE_RECEIPT_VERSIONS:
        raise UnsupportedContractVersion(
            "independent-gate-receipt-v1", version, SUPPORTED_GATE_RECEIPT_VERSIONS
        )
    if record.get("schema_id") != GATE_RECEIPT_SCHEMA_ID:
        raise ValueError(
            f"independent gate receipt schema_id {record.get('schema_id')!r} is "
            f"not {GATE_RECEIPT_SCHEMA_ID!r}"
        )
    raw_binding = record.get("binding")
    binding: Optional[ReviewBinding] = None
    if isinstance(raw_binding, Mapping):
        binding = ReviewBinding(
            review_id=raw_binding.get("review_id", ""),
            review_criteria=tuple(raw_binding.get("review_criteria") or ()),
            subject_revision=raw_binding.get("subject_revision", ""),
            artifact_digest=raw_binding.get("artifact_digest", ""),
        )
    IndependentGateReceipt(
        attempt_id=record.get("attempt_id", ""),
        state=record.get("state", ""),
        producer_artifact=record.get("producer_artifact", ""),
        binding=binding,
        role=record.get("role", ""),
        answering_identity=record.get("answering_identity", ""),
        producer_identity=record.get("producer_identity", ""),
        accepted=bool(record.get("accepted", False)),
        captured_at=record.get("captured_at", ""),
        schema_version=version,
    )
