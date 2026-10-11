"""Export and edition boundary (SW-159-EDITION-001).

This module defines the *boundary* between a local, privacy-safe telemetry
corpus (SW-159-TELEMETRY-001, :mod:`skillweave.runtime.local_telemetry`) and
anything that would leave the machine, and it states the Community / Pro /
Enterprise edition contracts honestly.

What the brief requires, and where it lives here:

* **Export is off by default** and requires *previewable, revocable* consent
  carrying a **deletion handle** and a **schema version** —
  :class:`ExportConsent`, :class:`DeletionHandle`.
* **Reject** prompts, source, secrets, paths, raw identifiers, and undersized
  cohorts — :func:`build_aggregate` (fail-closed via
  :func:`skillweave.runtime.local_telemetry.scan_for_prohibited_content` plus
  :data:`EXPORT_FORBIDDEN_KEYS`) and :func:`cohort_is_safe`.
* **Community retains local raw events and transparent coarse recommendations
  offline** — :data:`COMMUNITY`, and the coarse local recommender
  (:func:`skillweave.runtime.local_telemetry.recommend`).
* **Pro/Enterprise contracts define fixture-backed benchmarks, budgeting,
  capability-based mix, and hotspots** — :data:`PRO`, :data:`ENTERPRISE`.
* **Label the aggregate service ``preview/unavailable``** until ingestion,
  cohort, quality, deletion, and tenant-isolation gates pass —
  :class:`AggregateServiceStatus`.
* **Cover re-identification, poisoning, withdrawal, and edition bypass
  adversarially** — :func:`detect_poisoning`,
  :class:`AggregateExporter.withdraw`, :class:`EditionGuard`, and the
  re-identification floor in :func:`build_aggregate`.

It creates **no** network ingestion, **no** billing, **no** release, and
**no** production-service claim. The one thing this module can build is a
coarse, aggregated artifact in memory; :data:`AGGREGATE_SERVICE_GA` is pinned
``False`` and the aggregate status can never label the service ``available``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from .local_telemetry import (
    ADEQUATE_SAMPLE,
    COARSE_MIN_SAMPLE,
    GateOutcome,
    TelemetryError,
    TelemetryPrivacyError,
    recommend,
    scan_for_prohibited_content,
    validate_record,
)

# ── Versioning ──────────────────────────────────────────────────────────────

#: The aggregate-export artifact schema version. A consumer must pin it.
EXPORT_SCHEMA_VERSION = 1

#: The aggregate-export artifact kind tag.
EXPORT_KIND = "skillweave.telemetry-export"

#: Salt for the deletion-handle and series-token derivations. Handles and
#: tokens are one-way within a deployment; nothing but the subject can be
#: recovered from one, and nothing can be forged across deployments.
BOUNDARY_SALT = "skillweave-edition-boundary-v1"


# ── Cohort thresholds (k-anonymity) ────────────────────────────────────────

#: The default minimum cohort size a consent declares. Exporting any cohort
#: smaller than this is refused.
DEFAULT_COHORT_MIN = 5

#: No consent can lower the cohort threshold below this absolute floor.
ABSOLUTE_COHORT_MIN = 3

#: The smallest count any *reported* bucket may carry. A cohort that would
#: report a bucket of one or two records re-identifies those records, so the
#: aggregate is refused rather than published.
RE_IDENTIFICATION_FLOOR = 3


# ── Poisoning thresholds ───────────────────────────────────────────────────

#: A single repeated payload accounting for more than this share of a cohort is
#: a replay, not a distribution.
POISON_MAX_REPEAT_SHARE = 0.5

#: A single outcome accounting for more than this share of a cohort is
#: degenerate — the aggregate carries no discriminating signal.
POISON_MAX_OUTCOME_SHARE = 0.98


# ── The aggregate service is not a service yet ─────────────────────────────

#: The five gates the brief requires before the aggregate service may be
#: labelled anything but ``preview/unavailable``.
AGGREGATE_GATES: tuple[str, ...] = (
    "ingestion",
    "cohort",
    "quality",
    "deletion",
    "tenant_isolation",
)

#: The label while any gate is unmet.
AGGREGATE_UNAVAILABLE_LABEL = "preview/unavailable"

#: The label once every gate is met. Deliberately still ``preview`` — this is
#: never a general-availability claim.
AGGREGATE_AVAILABLE_LABEL = "preview"

#: Pinned ``False``: this release ships no production aggregate service.
AGGREGATE_SERVICE_GA = False


# ── Raw-identifier / export keys ───────────────────────────────────────────

#: Keys that must never appear in an exported cohort record. A superset of the
#: local-telemetry blocked keys, extended with the raw-payload and path names a
#: prompt/source/diff export would carry. Presence is **refused**, not masked.
EXPORT_FORBIDDEN_KEYS = frozenset({
    # raw payloads
    "raw", "raw_events", "raw_output", "raw_record", "raw_data",
    # prompts / source
    "prompt", "prompts", "prompt_text", "source", "source_code", "code",
    "snippet", "diff", "patch", "content", "text", "body", "message",
    "messages", "stdout", "stderr", "output", "log", "logs", "trace",
    "transcript", "conversation", "file_contents", "contents",
    # paths
    "path", "file_path", "filepath", "file", "dir", "directory", "repo_path",
    # raw identifiers
    "user_id", "user_name", "username", "email", "e_mail", "phone",
    "phone_number", "ssn", "address", "ip", "ip_address", "hostname",
    "full_name", "first_name", "last_name", "author",
    # secrets / credentials
    "api_key", "apikey", "token", "secret", "password", "passwd", "pwd",
    "credential", "credentials", "private_key", "access_key", "client_secret",
    "authorization", "auth", "bearer",
})

#: A pseudonymous subject label: a short, lower-case, separator-joined token.
#: Deliberately not an email, a UUID, or a personal name.
_SUBJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# ── Errors ──────────────────────────────────────────────────────────────────

class EditionError(TelemetryError):
    """Base error for the export/edition boundary."""


class ExportBoundaryError(EditionError):
    """Base error for an export-boundary refusal."""


class ConsentRequiredError(ExportBoundaryError):
    """An export was attempted without an active, granted consent."""


class ConsentRevokedError(ExportBoundaryError):
    """An export was attempted after the consent was revoked."""


class CohortTooSmallError(ExportBoundaryError):
    """The cohort is below the declared (or absolute) threshold."""


class ReIdentificationError(ExportBoundaryError):
    """A reported bucket is too small; the cohort would re-identify records."""


class PoisoningError(ExportBoundaryError):
    """The cohort is not representative (replayed payloads / degenerate outcome)."""


class AggregateUnavailableError(ExportBoundaryError):
    """An export was attempted while the aggregate service gates are unmet."""


class TenantBoundaryError(EditionError):
    """A tenant-scoped operation crossed a tenant boundary or is unknown."""


class EditionBypassError(EditionError):
    """A feature was requested that the declared edition does not grant."""


class UnknownEditionError(EditionError):
    """An unknown edition name was declared."""


# ── Opaque tokens ──────────────────────────────────────────────────────────

def _seal_token(*parts: str) -> str:
    """Return a one-way salted token over ``parts``."""
    message = "|".join(parts).encode("utf-8")
    return hmac.new(
        BOUNDARY_SALT.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def deletion_handle_for(subject: str, created_at: str) -> str:
    """Derive the opaque deletion handle for ``subject`` at ``created_at``."""
    return _seal_token("deletion", subject, created_at)[:32]


def series_token(subject: str, record: Mapping[str, Any]) -> str:
    """Derive the opaque per-record series token used only for *counting*."""
    return _seal_token("series", subject, _canonical(record))


def _validate_subject(subject: Any) -> str:
    """A subject must be a short pseudonym, never a raw identifier."""
    if not isinstance(subject, str) or not _SUBJECT_RE.match(subject):
        raise ExportBoundaryError(
            f"subject must be a short pseudonymous label "
            f"({'^[a-z0-9][a-z0-9._-]{0,63}$'}), got {subject!r}"
        )
    # Even a well-shaped label must not itself be an email / digit run / token.
    scan_for_prohibited_content({"subject": subject})
    return subject


# ── Cohort thresholds ──────────────────────────────────────────────────────

def cohort_is_safe(size: int, min_cohort: int = DEFAULT_COHORT_MIN) -> bool:
    """Whether ``size`` clears the cohort threshold and the absolute floor."""
    if isinstance(size, bool) or not isinstance(size, int):
        return False
    return size >= max(int(min_cohort), ABSOLUTE_COHORT_MIN)


def reported_bucket_floor(artifact: Mapping[str, Any]) -> int:
    """The smallest count among the artifact's reported outcome buckets."""
    counts = artifact.get("outcome_counts") or {}
    values = [int(v) for v in counts.values()]
    return min(values) if values else 0


# ── Poisoning detection ────────────────────────────────────────────────────

@dataclass(frozen=True)
class PoisoningSignal:
    """One reason a cohort is not trustworthy as an aggregate."""

    kind: str
    share: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "share": self.share, "detail": self.detail}


def detect_poisoning(
    records: Sequence[Mapping[str, Any]],
    *,
    max_repeat_share: float = POISON_MAX_REPEAT_SHARE,
    max_outcome_share: float = POISON_MAX_OUTCOME_SHARE,
) -> list[PoisoningSignal]:
    """Return the poisoning signals a cohort carries (empty when clean).

    Two adversarial shapes are detected, neither of which a legitimate cohort
    produces:

    * **replayed-record** — a single payload repeated in more than
      ``max_repeat_share`` of the cohort. A poisoning campaign floods with one
      observation; a real distribution does not repeat an exact payload.
    * **degenerate-outcome** — one outcome in more than ``max_outcome_share``
      of the cohort. An aggregate that is ~100% one outcome carries no signal.
    """
    rows = [r for r in records if isinstance(r, Mapping)]
    n = len(rows)
    if n == 0:
        return []

    signals: list[PoisoningSignal] = []

    payloads = Counter(_canonical(r) for r in rows)
    _payload, repeated = payloads.most_common(1)[0]
    repeat_share = repeated / n
    if repeat_share > max_repeat_share:
        signals.append(PoisoningSignal(
            "replayed-record", round(repeat_share, 4),
            f"a single repeated payload accounts for {repeated}/{n} records",
        ))

    outcomes = Counter(str(r.get("outcome")) for r in rows)
    top_outcome, top_count = outcomes.most_common(1)[0]
    outcome_share = top_count / n
    if outcome_share > max_outcome_share:
        signals.append(PoisoningSignal(
            "degenerate-outcome", round(outcome_share, 4),
            f"outcome {top_outcome!r} accounts for {top_count}/{n} records",
        ))

    return signals


# ── The aggregate artifact ─────────────────────────────────────────────────

#: Coarse pass-rate bands. A rate is reported as a band, never as a precise
#: number, so a tiny cohort cannot be fingerprinted by its exact rate.
_RATE_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.25, "0.00-0.25"),
    (0.25, 0.50, "0.25-0.50"),
    (0.50, 0.75, "0.50-0.75"),
    (0.75, 0.90, "0.75-0.90"),
    (0.90, 1.01, "0.90-1.00"),
)


def _pass_rate_band(rate: float) -> str:
    for lo, hi, label in _RATE_BANDS:
        if lo <= rate < hi:
            return label
    return _RATE_BANDS[-1][2]


def _sample_label(n: int) -> str:
    if n < COARSE_MIN_SAMPLE:
        return "insufficient"
    if n < ADEQUATE_SAMPLE:
        return "coarse"
    return "adequate"


def _reject_raw_keys(row: Mapping[str, Any], index: int) -> None:
    for key in row:
        if str(key).lower() in EXPORT_FORBIDDEN_KEYS:
            raise TelemetryPrivacyError(
                f"export refused: record[{index}] carries raw identifier key "
                f"{key!r}"
            )


def build_aggregate(
    records: Sequence[Mapping[str, Any]],
    *,
    subject: str,
    min_cohort: int = DEFAULT_COHORT_MIN,
) -> dict[str, Any]:
    """Build a coarse, privacy-safe aggregate over ``records``.

    Fail-closed, in order:

    1. every record is refused if it carries a raw-identifier key
       (:data:`EXPORT_FORBIDDEN_KEYS`) or prohibited content in a value
       (:func:`skillweave.runtime.local_telemetry.scan_for_prohibited_content`);
    2. the cohort must clear :func:`cohort_is_safe`;
    3. the cohort must be free of :func:`detect_poisoning` signals;
    4. no *reported* bucket may fall below :data:`RE_IDENTIFICATION_FLOOR`.

    The result carries counts, bands, and a coarse recommendation — never a raw
    record, a path, an identifier, or a raw event.
    """
    _validate_subject(subject)
    rows = [dict(r) for r in records if isinstance(r, Mapping)]

    for index, row in enumerate(rows):
        _reject_raw_keys(row, index)
        scan_for_prohibited_content(row)

    n = len(rows)
    threshold = max(int(min_cohort), ABSOLUTE_COHORT_MIN)
    if not cohort_is_safe(n, min_cohort):
        raise CohortTooSmallError(
            f"cohort of {n} is below the threshold {threshold}; "
            f"no aggregate is exported"
        )

    signals = detect_poisoning(rows)
    if signals:
        raise PoisoningError(
            "cohort is not representative: "
            + "; ".join(signal.detail for signal in signals)
        )

    outcome_counts = {outcome.value: 0 for outcome in GateOutcome}
    for row in rows:
        value = str(row.get("outcome"))
        if value in outcome_counts:
            outcome_counts[value] += 1
    smallest = min(outcome_counts.values())
    if smallest < RE_IDENTIFICATION_FLOOR:
        raise ReIdentificationError(
            f"a reported bucket holds {smallest} record(s), below the "
            f"re-identification floor {RE_IDENTIFICATION_FLOOR}; "
            f"the aggregate is refused"
        )

    passes = outcome_counts[GateOutcome.GATE_PASS.value]
    pass_rate = passes / n if n else 0.0

    return {
        "kind": EXPORT_KIND,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "cohort": {
            "size": n,
            "min_cohort": threshold,
            "re_identification_floor": RE_IDENTIFICATION_FLOOR,
            "smallest_reported_bucket": smallest,
            "k_anonymous": True,
        },
        "outcome_counts": outcome_counts,
        "summary": {
            "sample_size": n,
            "sample_label": _sample_label(n),
            "pass_rate_band": _pass_rate_band(pass_rate),
            "distinct_series": len({series_token(subject, row) for row in rows}),
        },
        "recommendation": recommend(rows).to_dict(),
        "privacy": {
            "raw_events_exported": False,
            "forbidden_keys_rejected": True,
            "coarse_only": True,
            "re_identification_floor": RE_IDENTIFICATION_FLOOR,
        },
        "provenance": {
            "subject": subject,
            "series_digest": _seal_token("series", subject)[:32],
            "collected_offline": True,
            "ingestion": "none",
        },
        "offline": True,
        "network_export": False,
        "production_claim": False,
    }


# ── Deletion handle + consent ──────────────────────────────────────────────

@dataclass(frozen=True)
class DeletionHandle:
    """An opaque, revocable handle for withdrawing a subject's contribution.

    The handle is a salted one-way token over the subject and the grant time;
    it lets a subject withdraw without the exporter ever holding a raw
    identifier. It carries the consent ``schema_version`` so a withdrawal is
    interpretable against the contract it was granted under.
    """

    subject: str
    handle: str
    schema_version: int
    created_at: str
    revoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "handle": self.handle,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "revoked": self.revoked,
        }

    def revoke(self) -> "DeletionHandle":
        return replace(self, revoked=True)


@dataclass
class ExportConsent:
    """Previewable, revocable export consent. **Off by default.**

    A freshly constructed consent is ``granted=False``; nothing may be
    exported under it. :meth:`grant` records the grant time, mints the
    :class:`DeletionHandle`, and is the only path to ``granted=True`` — so a
    granted consent *always* carries a deletion handle and a schema version
    (constructing one ``granted=True`` without a handle is refused).
    :meth:`revoke` withdraws it; :meth:`preview` shows what a grant would
    export without granting anything.
    """

    subject: str
    granted: bool = False
    revoked: bool = False
    schema_version: int = EXPORT_SCHEMA_VERSION
    min_cohort: int = DEFAULT_COHORT_MIN
    created_at: str = ""
    handle: Optional[DeletionHandle] = None

    def __post_init__(self) -> None:
        _validate_subject(self.subject)
        if self.schema_version != EXPORT_SCHEMA_VERSION:
            raise ExportBoundaryError(
                f"unsupported export schema_version {self.schema_version!r} "
                f"(expected {EXPORT_SCHEMA_VERSION})"
            )
        if self.min_cohort < ABSOLUTE_COHORT_MIN:
            raise ExportBoundaryError(
                f"min_cohort {self.min_cohort} is below the absolute floor "
                f"{ABSOLUTE_COHORT_MIN}"
            )
        if self.revoked:
            self.granted = False
        if self.granted and self.handle is None:
            raise ExportBoundaryError(
                "a granted consent must carry a deletion handle; "
                "grant it through ExportConsent.grant()"
            )

    @property
    def is_active(self) -> bool:
        return self.granted and not self.revoked

    def grant(self, *, created_at: Optional[str] = None) -> "ExportConsent":
        """Grant consent, mint the deletion handle, and return ``self``."""
        when = created_at or _utc_now()
        self.created_at = when
        self.handle = DeletionHandle(
            subject=self.subject,
            handle=deletion_handle_for(self.subject, when),
            schema_version=self.schema_version,
            created_at=when,
        )
        self.revoked = False
        self.granted = True
        return self

    def revoke(self) -> "ExportConsent":
        """Revoke consent and return ``self``. The handle stays resolvable."""
        self.granted = False
        self.revoked = True
        if self.handle is not None:
            self.handle = self.handle.revoke()
        return self

    def preview(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Show what a grant would export. Read-only; never mutates consent."""
        try:
            artifact = build_aggregate(
                records, subject=self.subject, min_cohort=self.min_cohort
            )
        except TelemetryError as exc:
            return {
                "exportable": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "subject": self.subject,
                "granted": self.granted,
                "revoked": self.revoked,
                "schema_version": self.schema_version,
            }
        artifact["exportable"] = True
        artifact["granted"] = self.granted
        artifact["revoked"] = self.revoked
        return artifact

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "granted": self.granted,
            "revoked": self.revoked,
            "schema_version": self.schema_version,
            "min_cohort": self.min_cohort,
            "created_at": self.created_at,
            "deletion_handle": self.handle.handle if self.handle else None,
        }


# ── The aggregate service status ───────────────────────────────────────────

@dataclass(frozen=True)
class AggregateServiceStatus:
    """The honest label for the aggregate service.

    Until **all** of :data:`AGGREGATE_GATES` pass, the label is
    :data:`AGGREGATE_UNAVAILABLE_LABEL` (``preview/unavailable``). Once they
    pass it is :data:`AGGREGATE_AVAILABLE_LABEL` (``preview``) — never
    ``available`` or GA, because this release ships no production service.
    """

    label: str
    available: bool
    gates: Mapping[str, bool]
    reasons: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        if self.label not in (
            AGGREGATE_UNAVAILABLE_LABEL, AGGREGATE_AVAILABLE_LABEL
        ):
            raise ExportBoundaryError(f"invalid aggregate label {self.label!r}")
        if self.available and self.label != AGGREGATE_AVAILABLE_LABEL:
            raise ExportBoundaryError(
                "an available status must carry the preview label"
            )
        if not self.available and self.label != AGGREGATE_UNAVAILABLE_LABEL:
            raise ExportBoundaryError(
                "an unavailable status must carry the preview/unavailable label"
            )
        # ``available`` requires every gate to pass; a directly-constructed
        # status cannot claim availability with a gate unmet (the label alone
        # is not the claim). The converse is intentionally not required: every
        # gate may pass while a ``blocked_reason`` still holds the service
        # unavailable, so all-pass does not by itself imply available.
        if self.available and not all(self.gates.values()):
            raise ExportBoundaryError(
                "an available status must have every aggregate gate passing"
            )
        if set(self.gates) != set(AGGREGATE_GATES):
            raise ExportBoundaryError(
                f"aggregate gates must be exactly {AGGREGATE_GATES!r}, "
                f"got {tuple(self.gates)!r}"
            )

    @classmethod
    def evaluate(
        cls,
        *,
        cohort_size: int = 0,
        min_cohort: int = DEFAULT_COHORT_MIN,
        ingestion: bool = False,
        cohort: bool = False,
        quality: bool = False,
        deletion: bool = False,
        tenant_isolation: bool = False,
        blocked_reason: str = "",
    ) -> "AggregateServiceStatus":
        """Evaluate the five gates and return the honest label.

        ``cohort`` is reconciled with ``cohort_size``: a declared cohort gate
        cannot pass an undersized cohort.
        """
        gates = {
            "ingestion": bool(ingestion),
            "cohort": bool(cohort),
            "quality": bool(quality),
            "deletion": bool(deletion),
            "tenant_isolation": bool(tenant_isolation),
        }
        reasons: list[str] = []
        if cohort_size and not cohort_is_safe(cohort_size, min_cohort):
            if gates["cohort"]:
                gates["cohort"] = False
                reasons.append(
                    f"cohort gate contradicts a cohort of {cohort_size} "
                    f"(threshold {max(int(min_cohort), ABSOLUTE_COHORT_MIN)})"
                )
        reasons.extend(name for name, ok in gates.items() if not ok)
        if blocked_reason:
            reasons.append(blocked_reason)
        available = all(gates.values()) and not blocked_reason
        label = (
            AGGREGATE_AVAILABLE_LABEL if available
            else AGGREGATE_UNAVAILABLE_LABEL
        )
        return cls(
            label=label,
            available=available,
            gates=gates,
            reasons=tuple(reasons),
            detail=(
                "every aggregate gate passes; still preview, not GA"
                if available
                else "aggregate service is preview/unavailable: "
                     + ", ".join(reasons)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "available": self.available,
            "gates": dict(self.gates),
            "reasons": list(self.reasons),
            "production_claim": AGGREGATE_SERVICE_GA,
            "detail": self.detail,
        }


# ── Editions ────────────────────────────────────────────────────────────────

class Edition(str, Enum):
    """The three editions an honest contract exists for."""

    COMMUNITY = "community"
    PRO = "pro"
    ENTERPRISE = "enterprise"


@dataclass(frozen=True)
class EditionContract:
    """The declared, honest capability contract of one edition.

    Every edition keeps its raw events **local** and works **offline**; export
    is opt-in and **off by default** everywhere. Community adds a coarse local
    recommender. Pro adds fixture-backed benchmarks, budgeting, a
    capability-based mix, and hotspots. Enterprise adds tenant isolation. No
    edition claims the aggregate service.
    """

    edition: Edition
    local_raw_events: bool
    offline: bool
    export_opt_in: bool
    export_default_off: bool
    coarse_local_recommendations: bool
    fixture_backed_benchmark: bool
    budgeting: bool
    capability_mix: bool
    hotspots: bool
    tenant_isolation: bool
    aggregate_service_claim: bool = AGGREGATE_SERVICE_GA

    def to_dict(self) -> dict[str, Any]:
        return {
            "edition": self.edition.value,
            "local_raw_events": self.local_raw_events,
            "offline": self.offline,
            "export_opt_in": self.export_opt_in,
            "export_default_off": self.export_default_off,
            "coarse_local_recommendations": self.coarse_local_recommendations,
            "fixture_backed_benchmark": self.fixture_backed_benchmark,
            "budgeting": self.budgeting,
            "capability_mix": self.capability_mix,
            "hotspots": self.hotspots,
            "tenant_isolation": self.tenant_isolation,
            "aggregate_service_claim": self.aggregate_service_claim,
        }


COMMUNITY = EditionContract(
    edition=Edition.COMMUNITY,
    local_raw_events=True,
    offline=True,
    export_opt_in=True,
    export_default_off=True,
    coarse_local_recommendations=True,
    fixture_backed_benchmark=False,
    budgeting=False,
    capability_mix=False,
    hotspots=False,
    tenant_isolation=False,
)

PRO = EditionContract(
    edition=Edition.PRO,
    local_raw_events=True,
    offline=True,
    export_opt_in=True,
    export_default_off=True,
    coarse_local_recommendations=True,
    fixture_backed_benchmark=True,
    budgeting=True,
    capability_mix=True,
    hotspots=True,
    tenant_isolation=False,
)

ENTERPRISE = EditionContract(
    edition=Edition.ENTERPRISE,
    local_raw_events=True,
    offline=True,
    export_opt_in=True,
    export_default_off=True,
    coarse_local_recommendations=True,
    fixture_backed_benchmark=True,
    budgeting=True,
    capability_mix=True,
    hotspots=True,
    tenant_isolation=True,
)

CONTRACTS: dict[Edition, EditionContract] = {
    Edition.COMMUNITY: COMMUNITY,
    Edition.PRO: PRO,
    Edition.ENTERPRISE: ENTERPRISE,
}


def contract_for(edition: "Edition | str") -> EditionContract:
    """Resolve an edition (by enum or name) to its contract, fail-closed."""
    if isinstance(edition, Edition):
        return CONTRACTS[edition]
    try:
        resolved = Edition(str(edition))
    except ValueError:
        raise UnknownEditionError(f"unknown edition {edition!r}") from None
    return CONTRACTS[resolved]


class Feature(str, Enum):
    """A capability an edition may or may not grant."""

    COARSE_LOCAL_RECOMMENDATIONS = "coarse_local_recommendations"
    BENCHMARK = "benchmark"
    BUDGETING = "budgeting"
    CAPABILITY_MIX = "capability_mix"
    HOTSPOTS = "hotspots"
    EXPORT = "export"
    AGGREGATE_SERVICE = "aggregate_service"
    TENANT_ISOLATION = "tenant_isolation"


_FEATURE_ATTR: dict[Feature, str] = {
    Feature.COARSE_LOCAL_RECOMMENDATIONS: "coarse_local_recommendations",
    Feature.BENCHMARK: "fixture_backed_benchmark",
    Feature.BUDGETING: "budgeting",
    Feature.CAPABILITY_MIX: "capability_mix",
    Feature.HOTSPOTS: "hotspots",
    Feature.EXPORT: "export_opt_in",
    Feature.AGGREGATE_SERVICE: "aggregate_service_claim",
    Feature.TENANT_ISOLATION: "tenant_isolation",
}


@dataclass(frozen=True)
class EditionGuard:
    """Gate a feature request on the declared edition's contract.

    The contract is a frozen constant, so a Community caller cannot mutate it
    into granting Pro features; an unknown or invented edition name is refused
    outright (:class:`UnknownEditionError`).
    """

    contract: EditionContract

    @classmethod
    def of(cls, edition: "Edition | str") -> "EditionGuard":
        return cls(contract_for(edition))

    def allows(self, feature: "Feature | str") -> bool:
        attr = _FEATURE_ATTR.get(_as_feature(feature))
        if attr is None:
            raise EditionBypassError(f"unknown feature {feature!r}")
        return bool(getattr(self.contract, attr))

    def require(self, feature: "Feature | str") -> None:
        """Raise :class:`EditionBypassError` unless the edition grants it."""
        if not self.allows(feature):
            raise EditionBypassError(
                f"edition {self.contract.edition.value!r} does not grant "
                f"'{_as_feature(feature).value}'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "edition": self.contract.edition.value,
            "features": {
                feature.value: self.allows(feature) for feature in Feature
            },
        }


def _as_feature(feature: "Feature | str") -> Feature:
    if isinstance(feature, Feature):
        return feature
    try:
        return Feature(str(feature))
    except ValueError:
        raise EditionBypassError(f"unknown feature {feature!r}") from None


def require_capability(edition: "Edition | str", feature: "Feature | str") -> None:
    """Convenience: require ``feature`` under ``edition``, fail-closed."""
    EditionGuard.of(edition).require(feature)


# ── The in-memory aggregate exporter ───────────────────────────────────────

@dataclass
class AggregateExporter:
    """An in-memory, tenant-scoped aggregate exporter. Never touches a network.

    It holds per-subject consents and contributions, builds a coarse aggregate
    only when the service gates pass *and* the subject's consent is active, and
    supports withdrawal by deletion handle. Cross-tenant reads are refused.
    """

    status: AggregateServiceStatus
    _consents: dict[str, ExportConsent] = field(default_factory=dict)
    _contributions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def register(self, consent: ExportConsent) -> ExportConsent:
        """Register a subject's consent (initially off unless granted)."""
        if consent.subject in self._consents:
            raise ExportBoundaryError(
                f"a consent for {consent.subject!r} is already registered"
            )
        self._consents[consent.subject] = consent
        self._contributions.setdefault(consent.subject, [])
        return consent

    def consent_for(self, subject: str) -> ExportConsent:
        """Return ``subject``'s consent, or refuse an unknown subject."""
        try:
            return self._consents[subject]
        except KeyError:
            raise TenantBoundaryError(f"unknown subject {subject!r}") from None

    def contribute(
        self, subject: str, records: Sequence[Mapping[str, Any]]
    ) -> int:
        """Add sealed telemetry records for ``subject`` under an active consent."""
        consent = self.consent_for(subject)
        if consent.revoked:
            raise ConsentRevokedError(
                f"consent for {subject!r} is revoked; contribution refused"
            )
        if not consent.granted:
            raise ConsentRequiredError(
                f"consent for {subject!r} is not granted; contribution refused"
            )
        rows: list[dict[str, Any]] = []
        for record in records:
            validate_record(record)
            rows.append(dict(record))
        self._contributions[subject].extend(rows)
        return len(rows)

    def preview(self, subject: str) -> dict[str, Any]:
        """Preview ``subject``'s would-be export. Read-only; no gate required."""
        consent = self.consent_for(subject)
        return consent.preview(self._contributions[subject])

    def export(self, subject: str) -> dict[str, Any]:
        """Export ``subject``'s aggregate, if every gate and consent hold."""
        consent = self.consent_for(subject)
        if not self.status.available:
            raise AggregateUnavailableError(
                f"aggregate service is {self.status.label}: "
                f"{', '.join(self.status.reasons)}"
            )
        if consent.revoked:
            raise ConsentRevokedError(
                f"consent for {subject!r} is revoked; export refused"
            )
        if not consent.granted:
            raise ConsentRequiredError(
                f"consent for {subject!r} is not granted; export refused"
            )
        if consent.handle is None:
            raise ExportBoundaryError(
                f"consent for {subject!r} has no deletion handle; export refused"
            )
        artifact = build_aggregate(
            self._contributions[subject],
            subject=subject,
            min_cohort=consent.min_cohort,
        )
        artifact["consent"] = {
            "subject": consent.subject,
            "granted": True,
            "revoked": False,
            "schema_version": consent.schema_version,
            "deletion_handle": consent.handle.handle,
            "created_at": consent.created_at,
        }
        return artifact

    def withdraw(self, handle: "DeletionHandle | str") -> dict[str, Any]:
        """Withdraw a subject's contribution by deletion handle.

        Revokes the matching consent and drops that subject's contributions.
        Another tenant's contributions are untouched.
        """
        wanted = handle.handle if isinstance(handle, DeletionHandle) else str(handle)
        for subject, consent in self._consents.items():
            if consent.handle is not None and consent.handle.handle == wanted:
                consent.revoke()
                removed = len(self._contributions.get(subject, []))
                self._contributions[subject] = []
                return {
                    "subject": subject,
                    "handle": wanted,
                    "revoked": True,
                    "contributions_removed": removed,
                    "schema_version": consent.schema_version,
                }
        raise ExportBoundaryError(f"unknown deletion handle {wanted!r}")


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "EXPORT_KIND",
    "BOUNDARY_SALT",
    "DEFAULT_COHORT_MIN",
    "ABSOLUTE_COHORT_MIN",
    "RE_IDENTIFICATION_FLOOR",
    "POISON_MAX_REPEAT_SHARE",
    "POISON_MAX_OUTCOME_SHARE",
    "AGGREGATE_GATES",
    "AGGREGATE_UNAVAILABLE_LABEL",
    "AGGREGATE_AVAILABLE_LABEL",
    "AGGREGATE_SERVICE_GA",
    "EXPORT_FORBIDDEN_KEYS",
    "EditionError",
    "ExportBoundaryError",
    "ConsentRequiredError",
    "ConsentRevokedError",
    "CohortTooSmallError",
    "ReIdentificationError",
    "PoisoningError",
    "AggregateUnavailableError",
    "TenantBoundaryError",
    "EditionBypassError",
    "UnknownEditionError",
    "deletion_handle_for",
    "series_token",
    "cohort_is_safe",
    "reported_bucket_floor",
    "PoisoningSignal",
    "detect_poisoning",
    "build_aggregate",
    "DeletionHandle",
    "ExportConsent",
    "AggregateServiceStatus",
    "Edition",
    "EditionContract",
    "COMMUNITY",
    "PRO",
    "ENTERPRISE",
    "CONTRACTS",
    "contract_for",
    "Feature",
    "EditionGuard",
    "require_capability",
    "AggregateExporter",
]
