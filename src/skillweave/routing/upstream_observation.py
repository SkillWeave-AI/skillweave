"""Provider upstream / model attestation and quota truth (SW-1511A-UPSTREAM-001).

The task boundary is the ``upstream-observation/v1`` data contract: the
producer is the Faigate adapter and the provider telemetry reader, the consumer
admission, the scheduler, the receipt writer and the review-diversity gate. Its
compatibility rule is explicit: **provider-specific additions are namespaced;
required identity fields never default.**

Three concerns, one per acceptance criterion:

1. **Admission attestation** (:func:`admit_provider_attempt`): a provider
   attempt is refused unless the allowed route, the actual upstream that
   answered, the answering model and the credential binding are all
   *attestable*. Identity fields never default to a placeholder; a missing one
   is a typed :class:`AdmissionRefused` naming the axis. The credential binding
   is a *reference*, never the secret, and a secret-shaped value is refused
   outright (:class:`SecretExposureRefused`) so attestation never requires
   exposing — or recording — a credential.

2. **Quota / availability truth** (:class:`QuotaObservation`,
   :class:`AvailabilityWatcher`): every observation carries ``source``,
   ``observed_at``, ``freshness``, ``window`` and ``status``. ``unknown``,
   ``rate_limit``, ``exhausted``, ``auth``, ``dns``, ``transport`` and
   ``upstream_timeout`` are seven *distinct* statuses, kept as *separate causal
   series* — each observation links to the previous observation of the same
   status (``caused_by``) — and the series are durable: rehydrating a watcher
   from its snapshot continues the incident instead of resetting it.

3. **Fallback / upstream refusal** (:func:`evaluate_model_diversity`,
   :func:`evaluate_acceptance_gate`): an enabled fallback or an upstream that
   does not match the requested provider can never satisfy an independent
   model-diversity or acceptance gate, no matter what the requested route names
   claim. Diversity is counted over the *observed* answering identity
   (upstream + answering model), not over requested route strings.

Nothing here launches a process, performs a provider call, reads a wall clock,
or touches the network. ``observed_at`` is the typed fact a producer recorded;
freshness is *derived* from the two supplied timestamps, never read from the
clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence, Tuple

# ── Contract identity ────────────────────────────────────────────────────────

UPSTREAM_OBSERVATION_SCHEMA_ID = (
    "upstream-observation/v1 (skillweave // SW-1511A-UPSTREAM-001)"
)
UPSTREAM_OBSERVATION_SCHEMA_VERSION = "1.0.0"

# ── Availability status vocabulary (criterion 2) ─────────────────────────────
#
# Seven distinct classes. None is an alias of another: an auth failure is not a
# rate limit, a DNS failure is not a transport failure, and an upstream timeout
# is none of them. Keeping them apart is what makes the causal series causally
# meaningful instead of one undifferentiated "unavailable" bucket.

STATUS_UNKNOWN = "unknown"
STATUS_RATE_LIMIT = "rate_limit"
STATUS_EXHAUSTED = "exhausted"
STATUS_AUTH = "auth"
STATUS_DNS = "dns"
STATUS_TRANSPORT = "transport"
STATUS_UPSTREAM_TIMEOUT = "upstream_timeout"

#: The closed vocabulary, in the order the contract names it.
AVAILABILITY_STATUSES: Tuple[str, ...] = (
    STATUS_UNKNOWN,
    STATUS_RATE_LIMIT,
    STATUS_EXHAUSTED,
    STATUS_AUTH,
    STATUS_DNS,
    STATUS_TRANSPORT,
    STATUS_UPSTREAM_TIMEOUT,
)

#: The freshness vocabulary, derived from explicit timestamps only.
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

#: The observed_after axis for the first observation of a series.
_ROOT_CAUSE = -1


# ── Exceptions (fail closed, never silently folded) ──────────────────────────


class UpstreamObservationError(Exception):
    """An upstream/observation contract violation (raised fail-closed)."""


class AdmissionRefused(UpstreamObservationError):
    """A provider attempt could not be attested and was refused.

    ``axis`` names the single unattestable field so the refusal is auditable
    without the caller guessing which identity was missing.
    """

    def __init__(self, axis: str, reason: str):
        self.axis = axis
        self.reason = reason
        super().__init__(f"provider attempt refused on '{axis}': {reason}")


class SecretExposureRefused(UpstreamObservationError):
    """A credential binding carried secret-shaped material and was refused.

    The offending value is deliberately **not** echoed, so the refusal itself
    cannot leak the secret it rejected.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"credential binding refused: {reason}")


class ObservationError(UpstreamObservationError):
    """A quota/availability observation is malformed or outside the vocabulary."""


# ── Secret shape (never the value) ───────────────────────────────────────────

#: A binding that *looks like* a secret (an inline bearer token or a
#: provider-style API key) is refused. This is a shape check: it never needs to
#: read the real secret, only to recognise that a value is one.
_SECRET_SHAPES: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^sk-[A-Za-z0-9_-]{8,}$"),
    re.compile(r"^(?:api[_-]?key|token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
)


def looks_like_secret(value: str) -> bool:
    """True when ``value`` has the shape of a secret (bindings must not)."""
    if not isinstance(value, str):
        return False
    return any(pattern.match(value) for pattern in _SECRET_SHAPES)


def _provider_of(route: str) -> str:
    """The provider half of a ``provider/model`` route (``""`` when bare)."""
    if not isinstance(route, str) or "/" not in route:
        return ""
    return route.split("/", 1)[0]


# ── Criterion 1: admission attestation ───────────────────────────────────────


@dataclass(frozen=True)
class ProviderAttemptAttestation:
    """The four attestable axes of a provider attempt, plus fallback facts.

    Every identity field (``requested_route``, ``actual_upstream``,
    ``answering_model``, ``credential_binding``) is required and is never
    defaulted: an attempt that cannot state one of them is not attestable.

    ``fallback_enabled`` / ``fallback_used`` record the observed fallback
    facts. They are *inputs* to admission, not a licence: criterion 3 refuses a
    fallback-served answer from satisfying a diversity or acceptance gate.
    """

    requested_route: str
    allowed_routes: frozenset[str]
    actual_upstream: str
    answering_model: str
    credential_binding: str
    fallback_enabled: bool = False
    fallback_used: bool = False

    def observed_identity(self) -> str:
        """The identity that *actually answered*: upstream + answering model.

        This — not the requested route name — is what diversity is counted
        over, so a fallback or a masked substitution cannot masquerade as a
        distinct model.
        """
        return f"{self.actual_upstream}/{self.answering_model}"


def _non_empty(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def admit_provider_attempt(
    attestation: ProviderAttemptAttestation,
) -> dict[str, Any]:
    """Attest and admit one provider attempt, or refuse it (criterion 1).

    Refuses, in order, a requested route that is not an allowed
    ``provider/model`` pair, an actual upstream that does not match the
    requested provider, an absent answering model, an absent credential
    binding, and a secret-shaped credential binding. On success returns the
    machine-readable attestation record — which cites the binding *reference*,
    never a secret.
    """
    # 1. Allowed route: a bare model name is a direct-provider bypass and is
    #    refused even when it appears in the allowed set.
    route = attestation.requested_route
    if _provider_of(route) == "":
        raise AdmissionRefused(
            "allowed_route",
            f"requested route {route!r} is not a provider/model pair; a bare "
            "model name is a direct-provider bypass",
        )
    if route not in attestation.allowed_routes:
        raise AdmissionRefused(
            "allowed_route",
            f"requested route {route!r} is not in the allowed set "
            f"{sorted(attestation.allowed_routes)}",
        )

    # 2. Actual upstream: the upstream that answered must be the requested
    #    provider. A mismatched upstream means a fallback or a substitution
    #    answered under the requested route name.
    requested_provider = _provider_of(route)
    if not _non_empty(attestation.actual_upstream):
        raise AdmissionRefused("actual_upstream", "no actual upstream was observed")
    if attestation.actual_upstream != requested_provider:
        raise AdmissionRefused(
            "actual_upstream",
            f"the answer came from upstream {attestation.actual_upstream!r} but "
            f"route {route!r} requests provider {requested_provider!r}",
        )

    # 3. Answering model: an unreported model is unattestable, never defaulted.
    if not _non_empty(attestation.answering_model):
        raise AdmissionRefused(
            "answering_model",
            "no answering model was reported; an unreported model is not "
            "attestable and is never defaulted",
        )

    # 4. Credential binding: required, and a *reference* — a secret-shaped
    #    value is refused so admission never needs the secret.
    binding = attestation.credential_binding
    if not _non_empty(binding):
        raise AdmissionRefused(
            "credential_binding", "no credential binding was attested"
        )
    if looks_like_secret(binding):
        raise SecretExposureRefused(
            "the binding is secret-shaped; attestation requires a reference to "
            "the credential binding, never the secret itself"
        )

    return {
        "schema": UPSTREAM_OBSERVATION_SCHEMA_ID,
        "schema_version": UPSTREAM_OBSERVATION_SCHEMA_VERSION,
        "requested_route": route,
        "allowed_routes": sorted(attestation.allowed_routes),
        "actual_upstream": attestation.actual_upstream,
        "answering_model": attestation.answering_model,
        "credential_binding": binding,
        "fallback_enabled": attestation.fallback_enabled,
        "fallback_used": attestation.fallback_used,
        "admitted": True,
    }


# ── Criterion 2: quota and availability truth ────────────────────────────────


def _parse_timestamp(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning ``None`` when it is not one."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def freshness_of(observed_at: str, now: str, window_seconds: int) -> str:
    """Derive freshness from two explicit timestamps (never a clock read).

    Returns ``fresh`` when ``now - observed_at`` is within ``window_seconds``,
    ``stale`` when it is older, and ``unknown`` when either timestamp cannot be
    parsed — an unparseable observation is never assumed fresh.
    """
    observed = _parse_timestamp(observed_at)
    at = _parse_timestamp(now)
    if observed is None or at is None:
        return FRESHNESS_UNKNOWN
    age = (at - observed).total_seconds()
    return FRESHNESS_FRESH if age <= window_seconds else FRESHNESS_STALE


@dataclass(frozen=True)
class QuotaObservation:
    """One versioned quota/availability observation (criterion 2).

    All five carrier fields are required and validated: ``source`` (where the
    observation came from), ``observed_at`` (when the producer observed it),
    ``freshness`` (fresh/stale/unknown), ``window`` (the quota window the
    status applies to) and ``status`` (one of :data:`AVAILABILITY_STATUSES`).
    A missing carrier is refused rather than defaulted.
    """

    source: str
    observed_at: str
    freshness: str
    window: str
    status: str
    detail: str = ""
    schema_version: str = UPSTREAM_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("source", self.source),
            ("observed_at", self.observed_at),
            ("freshness", self.freshness),
            ("window", self.window),
            ("status", self.status),
        ):
            if not _non_empty(value):
                raise ObservationError(
                    f"observation requires a non-empty {name!r}; every carrier "
                    "field is part of the contract and is never defaulted"
                )
        if self.freshness not in (
            FRESHNESS_FRESH,
            FRESHNESS_STALE,
            FRESHNESS_UNKNOWN,
        ):
            raise ObservationError(
                f"unknown freshness {self.freshness!r}; expected one of "
                f"{[FRESHNESS_FRESH, FRESHNESS_STALE, FRESHNESS_UNKNOWN]}"
            )
        if self.status not in AVAILABILITY_STATUSES:
            raise ObservationError(
                f"status {self.status!r} is not one of the distinct "
                f"availability statuses {list(AVAILABILITY_STATUSES)}"
            )
        if self.schema_version != UPSTREAM_OBSERVATION_SCHEMA_VERSION:
            raise ObservationError(
                f"unsupported observation schema version {self.schema_version!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": UPSTREAM_OBSERVATION_SCHEMA_ID,
            "schema_version": self.schema_version,
            "source": self.source,
            "observed_at": self.observed_at,
            "freshness": self.freshness,
            "window": self.window,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuotaObservation":
        return cls(
            source=data.get("source", ""),
            observed_at=data.get("observed_at", ""),
            freshness=data.get("freshness", ""),
            window=data.get("window", ""),
            status=data.get("status", ""),
            detail=data.get("detail", ""),
            schema_version=data.get(
                "schema_version", UPSTREAM_OBSERVATION_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class AvailabilityRecord:
    """One observation bound into its status' causal series (criterion 2).

    ``sequence`` is the position within *this status'* series (the first
    observation of a status is ``0``) and ``caused_by`` is the previous
    sequence of the same status (``-1`` at the root). A status therefore keeps
    its own monotone chain, and unrelated statuses never share a chain.
    """

    observation: QuotaObservation
    sequence: int
    caused_by: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation.to_dict(),
            "sequence": self.sequence,
            "caused_by": self.caused_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AvailabilityRecord":
        return cls(
            observation=QuotaObservation.from_dict(data.get("observation", {})),
            sequence=int(data.get("sequence", 0)),
            caused_by=int(data.get("caused_by", _ROOT_CAUSE)),
        )


class AvailabilityWatcher:
    """Fold availability observations into per-status causal series.

    The same status observed repeatedly accumulates into **one** growing series
    (``caused_by`` links each observation to its predecessor). Distinct statuses
    are distinct series and never merge. :meth:`to_dict` / :meth:`from_dict`
    make the series durable, so a watcher *restart* continues the incident
    rather than resetting it to a single fresh observation.
    """

    def __init__(self) -> None:
        self._series: dict[str, list[AvailabilityRecord]] = {}

    def observe(self, observation: QuotaObservation) -> AvailabilityRecord:
        """Record one observation, appending it to its status' causal series."""
        chain = self._series.setdefault(observation.status, [])
        caused_by = chain[-1].sequence if chain else _ROOT_CAUSE
        record = AvailabilityRecord(
            observation=observation,
            sequence=len(chain),
            caused_by=caused_by,
        )
        chain.append(record)
        return record

    def causal_series(self, status: str) -> Tuple[AvailabilityRecord, ...]:
        """The causal series of one status (empty when never observed)."""
        return tuple(self._series.get(status, ()))

    def series_statuses(self) -> Tuple[str, ...]:
        """The statuses that have a live causal series, in vocabulary order."""
        return tuple(s for s in AVAILABILITY_STATUSES if self._series.get(s))

    def to_dict(self) -> dict[str, Any]:
        """A durable snapshot: replaying it restores every causal series."""
        return {
            "schema": UPSTREAM_OBSERVATION_SCHEMA_ID,
            "schema_version": UPSTREAM_OBSERVATION_SCHEMA_VERSION,
            "series": {
                status: [r.to_dict() for r in records]
                for status, records in self._series.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AvailabilityWatcher":
        """Rehydrate a watcher from a snapshot, continuing its series."""
        watcher = cls()
        for status, records in data.get("series", {}).items():
            watcher._series[status] = [
                AvailabilityRecord.from_dict(r) for r in records
            ]
        return watcher


# ── Criterion 3: fallback / upstream refusal ─────────────────────────────────


@dataclass(frozen=True)
class DiversityVerdict:
    """The result of an independent model-diversity evaluation (criterion 3)."""

    satisfied: bool
    distinct_identities: int
    required_distinct: int
    fallback_detected: bool = False
    upstream_mismatches: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "distinct_identities": self.distinct_identities,
            "required_distinct": self.required_distinct,
            "fallback_detected": self.fallback_detected,
            "upstream_mismatches": [list(m) for m in self.upstream_mismatches],
        }


def _upstream_mismatches(
    attestations: Sequence[ProviderAttemptAttestation],
) -> Tuple[Tuple[str, str], ...]:
    out: list[Tuple[str, str]] = []
    for a in attestations:
        requested_provider = _provider_of(a.requested_route)
        if a.actual_upstream != requested_provider:
            out.append((a.requested_route, a.actual_upstream))
    return tuple(out)


def evaluate_model_diversity(
    attestations: Sequence[ProviderAttemptAttestation],
    required_distinct: int,
) -> DiversityVerdict:
    """Evaluate independent model diversity over *observed* identities.

    Diversity is counted over the answering identity (actual upstream +
    answering model), never over requested route names. The verdict is
    ``satisfied`` only when the distinct observed identities reach
    ``required_distinct`` **and** no attempt used an enabled fallback **and** no
    attempt's upstream mismatched its requested provider — so a fallback or a
    mismatched upstream can never satisfy the gate (criterion 3).
    """
    identities = {a.observed_identity() for a in attestations}
    fallback_detected = any(a.fallback_enabled or a.fallback_used for a in attestations)
    mismatches = _upstream_mismatches(attestations)
    distinct = len(identities)
    satisfied = (
        distinct >= required_distinct
        and not fallback_detected
        and not mismatches
    )
    return DiversityVerdict(
        satisfied=satisfied,
        distinct_identities=distinct,
        required_distinct=required_distinct,
        fallback_detected=fallback_detected,
        upstream_mismatches=mismatches,
    )


@dataclass(frozen=True)
class AcceptanceVerdict:
    """An acceptance gate over a set of attempts (criterion 3).

    An acceptance gate requires an attestable route for **every** attempt (the
    same admission rule as criterion 1) *and* a satisfied independent
    diversity verdict. An attempt that admission refuses fails the gate;
    a fallback or mismatched upstream can therefore never carry an acceptance
    gate on its own.
    """

    satisfied: bool
    diversity: DiversityVerdict
    refused: Tuple[Tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "diversity": self.diversity.to_dict(),
            "refused": [list(r) for r in self.refused],
        }


def evaluate_acceptance_gate(
    attestations: Sequence[ProviderAttemptAttestation],
    required_distinct: int,
) -> AcceptanceVerdict:
    """Evaluate an acceptance gate: per-attempt admission *and* diversity."""
    refused: list[Tuple[str, str]] = []
    for a in attestations:
        try:
            admit_provider_attempt(a)
        except AdmissionRefused as exc:
            refused.append((a.requested_route, exc.axis))
        except SecretExposureRefused as exc:
            refused.append((a.requested_route, "credential_binding_secret"))
    diversity = evaluate_model_diversity(attestations, required_distinct)
    satisfied = not refused and diversity.satisfied
    return AcceptanceVerdict(
        satisfied=satisfied,
        diversity=diversity,
        refused=tuple(refused),
    )


__all__ = [
    "UPSTREAM_OBSERVATION_SCHEMA_ID",
    "UPSTREAM_OBSERVATION_SCHEMA_VERSION",
    "STATUS_UNKNOWN",
    "STATUS_RATE_LIMIT",
    "STATUS_EXHAUSTED",
    "STATUS_AUTH",
    "STATUS_DNS",
    "STATUS_TRANSPORT",
    "STATUS_UPSTREAM_TIMEOUT",
    "AVAILABILITY_STATUSES",
    "FRESHNESS_FRESH",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "UpstreamObservationError",
    "AdmissionRefused",
    "SecretExposureRefused",
    "ObservationError",
    "looks_like_secret",
    "ProviderAttemptAttestation",
    "admit_provider_attempt",
    "freshness_of",
    "QuotaObservation",
    "AvailabilityRecord",
    "AvailabilityWatcher",
    "DiversityVerdict",
    "evaluate_model_diversity",
    "AcceptanceVerdict",
    "evaluate_acceptance_gate",
]
