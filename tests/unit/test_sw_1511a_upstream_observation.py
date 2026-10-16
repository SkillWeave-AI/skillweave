"""SW-1511A-UPSTREAM-001 acceptance tests (one adversarial test per criterion).

Criterion 1: admission refuses a provider attempt unless the allowed route,
actual upstream, answering model and credential binding are attestable without
exposing secrets.

Criterion 2: quota/availability observations carry source, observed_at,
freshness, window and status; unknown, rate-limit, exhausted, auth, DNS,
transport and upstream-timeout are distinct and retain their causal series
across watcher restarts.

Criterion 3: a negative test proves an enabled fallback or mismatched upstream
cannot satisfy independent model-diversity or acceptance gates.

No network, provider call or credential is used: every input is a literal.
"""

from __future__ import annotations

import pytest

from skillweave.routing.upstream_observation import (
    AVAILABILITY_STATUSES,
    STATUS_AUTH,
    STATUS_DNS,
    STATUS_EXHAUSTED,
    STATUS_RATE_LIMIT,
    STATUS_TRANSPORT,
    STATUS_UNKNOWN,
    STATUS_UPSTREAM_TIMEOUT,
    AdmissionRefused,
    AvailabilityWatcher,
    ProviderAttemptAttestation,
    QuotaObservation,
    SecretExposureRefused,
    admit_provider_attempt,
    evaluate_acceptance_gate,
    evaluate_model_diversity,
    freshness_of,
)

_ALLOWED = frozenset({"gw/alpha-flash", "gw/beta-pro", "gw/beta-flash"})


def _attestation(**overrides):
    base = dict(
        requested_route="gw/alpha-flash",
        allowed_routes=_ALLOWED,
        actual_upstream="gw",
        answering_model="alpha-flash",
        credential_binding="binding://faigate/route-alpha",
    )
    base.update(overrides)
    return ProviderAttemptAttestation(**base)


# ── Criterion 1 ─────────────────────────────────────────────────────────────


def test_criterion1_admission_refuses_each_unattestable_axis_and_secret_binding():
    """Adversarial: every omitted/masked axis is refused, and no secret leaks.

    The base tree has no admission surface at all, so this test cannot even be
    collected there; with the module present it proves the four axes are
    *required* (never defaulted) and that a secret-shaped credential binding is
    refused rather than recorded.
    """
    # The happy path exists only when all four axes are attestable.
    record = admit_provider_attempt(_attestation())
    assert record["requested_route"] == "gw/alpha-flash"
    assert record["actual_upstream"] == "gw"
    assert record["answering_model"] == "alpha-flash"
    assert record["credential_binding"] == "binding://faigate/route-alpha"
    assert "secret" not in str(record).lower().replace("secret_shape", "")

    # A requested route outside the allowed set is refused (allowed route).
    with pytest.raises(AdmissionRefused) as exc:
        admit_provider_attempt(_attestation(requested_route="elsewhere/alpha-flash"))
    assert exc.value.axis == "allowed_route"

    # A bare model name is not an attestable route (direct-provider bypass).
    with pytest.raises(AdmissionRefused) as exc:
        admit_provider_attempt(
            _attestation(requested_route="alpha-flash", allowed_routes=frozenset({"alpha-flash"}))
        )
    assert exc.value.axis == "allowed_route"

    # The upstream that actually answered must match the requested provider:
    # a fallback that answers from another upstream is not attestable.
    with pytest.raises(AdmissionRefused) as exc:
        admit_provider_attempt(_attestation(actual_upstream="other-upstream"))
    assert exc.value.axis == "actual_upstream"

    # An empty answering model is unattestable and never defaulted.
    with pytest.raises(AdmissionRefused) as exc:
        admit_provider_attempt(_attestation(answering_model="   "))
    assert exc.value.axis == "answering_model"

    # An empty credential binding is unattestable.
    with pytest.raises(AdmissionRefused) as exc:
        admit_provider_attempt(_attestation(credential_binding=""))
    assert exc.value.axis == "credential_binding"

    # A secret-shaped binding is refused outright: attestation must not require
    # exposing the secret itself.
    for secret in ("sk-live-0123456789abcdef", "Bearer eyJhbGciOiJIUzI1NiJ9.payload"):
        with pytest.raises(SecretExposureRefused):
            admit_provider_attempt(_attestation(credential_binding=secret))

    # The refused secret never appears in a raised message (no leak in evidence).
    try:
        admit_provider_attempt(_attestation(credential_binding="sk-live-0123456789abcdef"))
    except SecretExposureRefused as exc:
        assert "sk-live-0123456789abcdef" not in str(exc)


# ── Criterion 2 ─────────────────────────────────────────────────────────────


def test_criterion2_statuses_are_distinct_and_causal_series_survive_restart():
    """Adversarial: the seven statuses never collapse, and a restart keeps chains.

    On the base tree there is no observation contract, so the seven classes and
    the restart-continuity guarantee cannot be observed at all; here the same
    failure repeated across a watcher restart stays one growing causal series
    instead of resetting the incident.
    """
    assert len(set(AVAILABILITY_STATUSES)) == len(AVAILABILITY_STATUSES)
    assert {
        STATUS_UNKNOWN,
        STATUS_RATE_LIMIT,
        STATUS_EXHAUSTED,
        STATUS_AUTH,
        STATUS_DNS,
        STATUS_TRANSPORT,
        STATUS_UPSTREAM_TIMEOUT,
    } == set(AVAILABILITY_STATUSES)

    def observation(status, second):
        return QuotaObservation(
            source="faigate/telemetry",
            observed_at=f"2026-10-16T10:00:{second:02d}+00:00",
            freshness="fresh",
            window="60s",
            status=status,
        )

    # An incomplete observation is refused: every carrier field is required.
    for missing in ("source", "observed_at", "window"):
        kwargs = dict(
            source="faigate/telemetry",
            observed_at="2026-10-16T10:00:00+00:00",
            freshness="fresh",
            window="60s",
            status=STATUS_RATE_LIMIT,
        )
        kwargs[missing] = ""
        with pytest.raises(Exception):
            QuotaObservation(**kwargs)
    with pytest.raises(Exception):
        QuotaObservation(
            source="s",
            observed_at="2026-10-16T10:00:00+00:00",
            freshness="fresh",
            window="60s",
            status="429",
        )

    before = AvailabilityWatcher()
    for second in (0, 1, 2):
        before.observe(observation(STATUS_RATE_LIMIT, second))

    # Distinct statuses form distinct series — a DNS blip never merges into the
    # rate-limit incident.
    before.observe(observation(STATUS_DNS, 3))
    assert [r.sequence for r in before.causal_series(STATUS_RATE_LIMIT)] == [0, 1, 2]
    assert [r.sequence for r in before.causal_series(STATUS_DNS)] == [0]
    assert len(before.series_statuses()) == 2

    # Restart: a durable snapshot is replayed, then the same class is observed
    # again — the causal series continues, it does not reset.
    snapshot = before.to_dict()
    restarted = AvailabilityWatcher.from_dict(snapshot)
    restarted.observe(observation(STATUS_RATE_LIMIT, 4))
    restarted.observe(observation(STATUS_RATE_LIMIT, 5))

    chain = restarted.causal_series(STATUS_RATE_LIMIT)
    assert [r.sequence for r in chain] == [0, 1, 2, 3, 4]
    assert [r.caused_by for r in chain] == [-1, 0, 1, 2, 3]
    assert [r.observation.status for r in chain] == [STATUS_RATE_LIMIT] * 5
    assert restarted.to_dict()["schema"] == before.to_dict()["schema"]

    # Freshness is derived from explicit timestamps, never read from a clock.
    assert freshness_of("2026-10-16T10:00:00+00:00", "2026-10-16T10:00:30+00:00", 60) == "fresh"
    assert freshness_of("2026-10-16T10:00:00+00:00", "2026-10-16T10:05:00+00:00", 60) == "stale"
    assert freshness_of("not-a-timestamp", "2026-10-16T10:05:00+00:00", 60) == "unknown"


# ── Criterion 3 ─────────────────────────────────────────────────────────────


def test_criterion3_negative_enabled_fallback_or_mismatched_upstream_never_passes():
    """Adversarial negative: fallback and upstream mismatch cannot fake diversity.

    Two attempts name *different* requested routes, so a route-name-only check
    would report diversity. The same actual upstream answered both — one via an
    enabled fallback, one through a mismatched upstream — so both the
    model-diversity gate and the acceptance gate must refuse.
    """
    honest_a = _attestation(
        requested_route="gw/beta-pro", actual_upstream="gw", answering_model="beta-pro"
    )
    honest_b = _attestation(
        requested_route="gw/alpha-flash", actual_upstream="gw", answering_model="alpha-flash"
    )
    assert evaluate_model_diversity([honest_a, honest_b], required_distinct=2).satisfied

    # Enabled fallback: the requested route is a different name, but the answer
    # came from the fallback upstream, so diversity is not independent.
    fallback = _attestation(
        requested_route="gw/alpha-flash",
        actual_upstream="gw",
        answering_model="alpha-flash",
        fallback_enabled=True,
        fallback_used=True,
    )
    diversity = evaluate_model_diversity([honest_a, fallback], required_distinct=2)
    assert not diversity.satisfied
    assert diversity.fallback_detected
    assert not evaluate_acceptance_gate([honest_a, fallback], required_distinct=2).satisfied

    # Mismatched upstream: the attested upstream is not the requested provider.
    mismatch = _attestation(
        requested_route="gw/alpha-flash",
        actual_upstream="mirror-gw",
        answering_model="alpha-flash",
    )
    diversity = evaluate_model_diversity([honest_a, mismatch], required_distinct=2)
    assert not diversity.satisfied
    assert diversity.upstream_mismatches
    assert not evaluate_acceptance_gate([honest_a, mismatch], required_distinct=2).satisfied

    # Masked substitution: two distinct route names, one answering identity.
    masked_a = _attestation(
        requested_route="gw/beta-pro", actual_upstream="gw", answering_model="alpha-flash"
    )
    diversity = evaluate_model_diversity([masked_a, honest_b], required_distinct=2)
    assert diversity.distinct_identities == 1
    assert not diversity.satisfied
    assert not evaluate_acceptance_gate([masked_a, honest_b], required_distinct=2).satisfied
