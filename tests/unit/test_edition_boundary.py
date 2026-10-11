"""Unit tests for the export and edition boundary (SW-159-EDITION-001).

Covers the brief's acceptance surface:

- default-off: export is off unless a consent is explicitly granted; a granted
  consent always carries a deletion handle and a schema version;
- previewable / revocable consent: preview shows the would-be export without
  granting, revoke withdraws, and the deletion handle survives the revoke;
- prohibited content: prompts, source, secrets, personal identifiers, and
  direct paths are REJECTED at the export boundary — not masked;
- cohort threshold: an undersized cohort is refused, and no consent can lower
  the threshold past the absolute floor;
- re-identification: a reported bucket below the floor refuses the aggregate;
- poisoning: a replayed payload or a degenerate outcome refuses the aggregate;
- withdrawal: a subject withdraws by deletion handle; another tenant is
  untouched;
- tenant boundary: an unknown subject, a duplicate registration, and a
  contribution without consent are each refused;
- edition contracts: Community keeps raw events local with a coarse offline
  recommender; Pro/Enterprise define fixture-backed benchmarks, budgeting,
  capability-based mix, and hotspots; no edition claims the aggregate service;
- edition bypass: a Community caller cannot reach a Pro feature, an invented
  edition is refused, and the aggregate-service claim is denied everywhere;
- aggregate service label: pinned ``preview/unavailable`` until every gate
  passes, and never GA.

Runs as a pytest module or as a standalone script (``_run_all``).
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest

from skillweave.runtime.edition_boundary import (
    ABSOLUTE_COHORT_MIN,
    AGGREGATE_AVAILABLE_LABEL,
    AGGREGATE_GATES,
    AGGREGATE_SERVICE_GA,
    AGGREGATE_UNAVAILABLE_LABEL,
    COMMUNITY,
    CONTRACTS,
    DEFAULT_COHORT_MIN,
    ENTERPRISE,
    EXPORT_KIND,
    EXPORT_SCHEMA_VERSION,
    PRO,
    RE_IDENTIFICATION_FLOOR,
    AggregateExporter,
    AggregateServiceStatus,
    AggregateUnavailableError,
    CohortTooSmallError,
    ConsentRequiredError,
    ConsentRevokedError,
    DeletionHandle,
    Edition,
    EditionBypassError,
    EditionContract,
    EditionGuard,
    ExportBoundaryError,
    ExportConsent,
    Feature,
    PoisoningError,
    ReIdentificationError,
    TenantBoundaryError,
    UnknownEditionError,
    build_aggregate,
    cohort_is_safe,
    contract_for,
    detect_poisoning,
    reported_bucket_floor,
    require_capability,
)
from skillweave.runtime.local_telemetry import (
    AssayPolicyName,
    GateOutcome,
    LocalTelemetryRecord,
    ModelIdentity,
    TelemetryPrivacyError,
    VersionPoint,
)

_SHA = "a" * 40
_MI = ModelIdentity(catalogue_id="faigate/deepseek-v4-pro", tier="pro")
_SUBJECT = "team-alpha"


def _rows(n, *, pass_share=0.5, **overrides):
    """A clean, deterministic cohort of plain aggregate records."""
    rows = []
    for i in range(n):
        passing = i < round(n * pass_share)
        row = dict(
            outcome=(GateOutcome.GATE_PASS.value if passing else GateOutcome.HOLD.value),
            starting_budget=10 * (i + 1),
            turns_used=i,
            retries=i,
            ast_delta=i,
            loc_delta=10 * i,
            interval_to_pass=(10 if passing else None),
            interval_to_hold=(None if passing else 5),
            policy=AssayPolicyName.MODERATE.value,
        )
        row.update(overrides)
        rows.append(row)
    return rows


def _sealed(n, *, pass_share=0.5, **overrides):
    """A clean cohort of sealed telemetry records (the production shape)."""
    rows = []
    for i in range(n):
        passing = i < round(n * pass_share)
        base = dict(
            version_point=VersionPoint(
                commit_sha=_SHA, branch="feature/sw-159-edition-001"
            ),
            methodology="rex",
            policy=AssayPolicyName.MODERATE.value,
            model_identity=_MI,
            topology="sequential",
            risk="low",
            starting_budget=10 * (i + 1),
            turns_used=i,
            changes=i % 3,
            retries=i % 2,
            splits=0,
            loc_delta=10 * i,
            ast_delta=i,
            failures=0,
            interval_to_pass=(10 if passing else None),
            interval_to_hold=(None if passing else 5),
            outcome=(GateOutcome.GATE_PASS.value if passing else GateOutcome.HOLD.value),
        )
        base.update(overrides)
        rows.append(LocalTelemetryRecord(**base).seal())
    return rows


def _available_status():
    return AggregateServiceStatus.evaluate(
        ingestion=True, cohort=True, quality=True, deletion=True,
        tenant_isolation=True,
    )


def _exporter():
    return AggregateExporter(_available_status())


# ---------------------------------------------------------------------------
# Default-off: export requires an explicit, granted consent
# ---------------------------------------------------------------------------

class TestExportOffByDefault:
    def test_fresh_consent_is_off(self):
        consent = ExportConsent(_SUBJECT)
        assert consent.granted is False
        assert consent.revoked is False
        assert consent.is_active is False
        assert consent.handle is None

    def test_granted_consent_without_a_handle_is_refused(self):
        with pytest.raises(ExportBoundaryError):
            ExportConsent(_SUBJECT, granted=True)

    def test_unsupported_schema_version_is_refused(self):
        with pytest.raises(ExportBoundaryError):
            ExportConsent(_SUBJECT, schema_version=EXPORT_SCHEMA_VERSION + 1)

    def test_cohort_below_the_absolute_floor_is_refused(self):
        with pytest.raises(ExportBoundaryError):
            ExportConsent(_SUBJECT, min_cohort=ABSOLUTE_COHORT_MIN - 1)

    def test_raw_identifier_is_not_a_valid_subject(self):
        for subject in ("Alice@Example.com", "alice@example.com", "a b", "", "A"):
            with pytest.raises(ExportBoundaryError):
                ExportConsent(subject)

    def test_export_schema_version_is_pinned(self):
        assert EXPORT_SCHEMA_VERSION == 1
        assert EXPORT_KIND == "skillweave.telemetry-export"


# ---------------------------------------------------------------------------
# Consent: granted through grant(), carries a handle + version
# ---------------------------------------------------------------------------

class TestConsentLifecycle:
    def test_grant_mints_a_deletion_handle_and_records_the_version(self):
        consent = ExportConsent(_SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
        assert consent.granted is True and consent.is_active is True
        assert consent.handle is not None
        assert consent.handle.schema_version == EXPORT_SCHEMA_VERSION
        assert consent.handle.created_at == "2026-09-28T00:00:00Z"
        assert len(consent.handle.handle) == 32

    def test_handle_is_deterministic_for_subject_and_time(self):
        a = ExportConsent(_SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
        b = ExportConsent(_SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
        assert a.handle.handle == b.handle.handle

    def test_handle_changes_with_the_grant_time(self):
        a = ExportConsent(_SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
        b = ExportConsent(_SUBJECT).grant(created_at="2026-09-29T00:00:00Z")
        assert a.handle.handle != b.handle.handle

    def test_revoke_withdraws_and_retires_the_handle(self):
        consent = ExportConsent(_SUBJECT).grant()
        handle = consent.handle
        consent.revoke()
        assert consent.granted is False and consent.revoked is True
        assert consent.is_active is False
        assert handle.revoked is False  # the earlier reference is a snapshot
        assert consent.handle.revoked is True

    def test_to_dict_exposes_the_deletion_handle_and_version(self):
        consent = ExportConsent(_SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
        payload = consent.to_dict()
        assert payload["deletion_handle"] == consent.handle.handle
        assert payload["schema_version"] == EXPORT_SCHEMA_VERSION
        assert payload["granted"] is True and payload["revoked"] is False

    def test_deletion_handle_dict_round_trips_its_declared_fields(self):
        handle = DeletionHandle(
            subject=_SUBJECT, handle="h" * 32, schema_version=EXPORT_SCHEMA_VERSION,
            created_at="2026-09-28T00:00:00Z",
        )
        assert set(handle.to_dict()) == {
            "subject", "handle", "schema_version", "created_at", "revoked",
        }
        assert handle.revoke().revoked is True


# ---------------------------------------------------------------------------
# Preview: read-only, shows the would-be export without granting
# ---------------------------------------------------------------------------

class TestPreview:
    def test_preview_shows_the_export_but_does_not_grant(self):
        consent = ExportConsent(_SUBJECT)
        preview = consent.preview(_rows(8))
        assert preview["exportable"] is True
        assert preview["granted"] is False and preview["revoked"] is False
        assert consent.is_active is False  # preview never mutates consent

    def test_preview_reports_why_an_unsafe_cohort_is_not_exportable(self):
        preview = ExportConsent(_SUBJECT).preview(_rows(4))
        assert preview["exportable"] is False
        assert "CohortTooSmallError" in preview["reason"]

    def test_preview_reports_a_prohibited_content_refusal(self):
        preview = ExportConsent(_SUBJECT).preview([{"prompt": "hello there friend"}])
        assert preview["exportable"] is False
        assert "TelemetryPrivacyError" in preview["reason"]

    def test_preview_carries_the_schema_version(self):
        preview = ExportConsent(_SUBJECT).preview(_rows(8))
        assert preview["schema_version"] == EXPORT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Prohibited content: reject prompts/source/secrets/PII/paths
# ---------------------------------------------------------------------------

class TestProhibitedContentAtExport:
    def test_raw_identifier_key_is_rejected(self):
        for key in ("prompt", "source_code", "api_key", "token", "email",
                    "user_name", "content", "path", "raw_events", "stdout"):
            with pytest.raises(TelemetryPrivacyError):
                build_aggregate([{key: "placeholder"}], subject=_SUBJECT)

    def test_prohibited_value_is_rejected_even_under_an_allowed_key(self):
        for value in ("wrote /Users/alice/secret.txt",
                      "changed src/skillweave/runtime/edition_boundary.py",
                      "alice@example.com", "123-45-6789", "10.0.0.1",
                      "ghp_16C7e42F292c6912E7710c838347Ae178B4a"):
            with pytest.raises(TelemetryPrivacyError):
                build_aggregate([{"note": value}], subject=_SUBJECT)

    def test_a_secret_in_the_subject_is_rejected(self):
        # A subject that is shaped like a short label but carries a 16-char
        # opaque run is refused by the value scan, not merely the shape regex.
        with pytest.raises(TelemetryPrivacyError):
            build_aggregate(_rows(8), subject="skillweaveauthxx")

    def test_rejection_precedes_the_cohort_check(self):
        # A one-record cohort carrying prohibited content is refused for the
        # content, never masked and never admitted by a later gate.
        with pytest.raises(TelemetryPrivacyError):
            build_aggregate([{"prompt": "one two three four"}], subject=_SUBJECT)

    def test_clean_cohort_passes_the_boundary(self):
        artifact = build_aggregate(_rows(8), subject=_SUBJECT)
        assert artifact["privacy"]["forbidden_keys_rejected"] is True


# ---------------------------------------------------------------------------
# Cohort threshold + re-identification floor
# ---------------------------------------------------------------------------

class TestCohortThreshold:
    def test_undersized_cohort_is_refused(self):
        with pytest.raises(CohortTooSmallError):
            build_aggregate(_rows(DEFAULT_COHORT_MIN - 1), subject=_SUBJECT)

    def test_cohort_is_safe_at_the_threshold_and_not_below(self):
        assert cohort_is_safe(DEFAULT_COHORT_MIN) is True
        assert cohort_is_safe(DEFAULT_COHORT_MIN - 1) is False

    def test_no_consent_can_lower_the_floor_below_absolute(self):
        with pytest.raises(ExportBoundaryError):
            ExportConsent(_SUBJECT, min_cohort=ABSOLUTE_COHORT_MIN - 1)
        # Even at the absolute floor, a two-record cohort is unsafe.
        assert cohort_is_safe(ABSOLUTE_COHORT_MIN - 1, min_cohort=ABSOLUTE_COHORT_MIN) is False

    def test_non_integer_sizes_are_not_safe(self):
        assert cohort_is_safe(True) is False
        assert cohort_is_safe("5") is False

    def test_a_consent_raised_threshold_is_honoured(self):
        with pytest.raises(CohortTooSmallError):
            build_aggregate(_rows(8), subject=_SUBJECT, min_cohort=20)


class TestReIdentification:
    def test_a_bucket_below_the_floor_refuses_the_aggregate(self):
        # 6 pass / 2 hold over 8 records: the cohort clears the threshold but
        # a reported bucket of two re-identifies those records.
        with pytest.raises(ReIdentificationError):
            build_aggregate(_rows(8, pass_share=0.75), subject=_SUBJECT)

    def test_every_reported_bucket_at_the_floor_is_exportable(self):
        artifact = build_aggregate(_rows(6), subject=_SUBJECT)
        assert artifact["cohort"]["smallest_reported_bucket"] == RE_IDENTIFICATION_FLOOR
        assert artifact["cohort"]["k_anonymous"] is True

    def test_reported_bucket_floor_reads_the_smallest_bucket(self):
        artifact = build_aggregate(_rows(8), subject=_SUBJECT)
        assert reported_bucket_floor(artifact) == min(
            artifact["outcome_counts"].values()
        )

    def test_the_floor_is_at_least_three(self):
        assert RE_IDENTIFICATION_FLOOR >= 3


# ---------------------------------------------------------------------------
# Poisoning: replayed payloads and degenerate outcomes
# ---------------------------------------------------------------------------

class TestPoisoning:
    def test_replayed_payload_is_detected(self):
        rows = _rows(8)
        rows[1:8] = [dict(rows[0]) for _ in range(7)]
        signals = detect_poisoning(rows)
        assert any(s.kind == "replayed-record" for s in signals)

    def test_degenerate_outcome_is_detected(self):
        rows = _rows(100)
        for row in rows:
            row["outcome"] = GateOutcome.GATE_PASS.value
        signals = detect_poisoning(rows)
        assert any(s.kind == "degenerate-outcome" for s in signals)

    def test_clean_cohort_has_no_signal(self):
        assert detect_poisoning(_rows(8)) == []

    def test_a_poisoned_cohort_is_refused_by_the_boundary(self):
        rows = _rows(8)
        rows[1:8] = [dict(rows[0]) for _ in range(7)]
        with pytest.raises(PoisoningError):
            build_aggregate(rows, subject=_SUBJECT)

    def test_a_degenerate_cohort_is_refused_by_the_boundary(self):
        rows = _rows(100)
        for row in rows:
            row["outcome"] = GateOutcome.GATE_PASS.value
        with pytest.raises(PoisoningError):
            build_aggregate(rows, subject=_SUBJECT)

    def test_empty_cohort_is_not_signalled_as_poisoned(self):
        assert detect_poisoning([]) == []


# ---------------------------------------------------------------------------
# The aggregate artifact: coarse, offline, no raw record
# ---------------------------------------------------------------------------

class TestAggregateArtifact:
    def test_artifact_declares_offline_and_no_production_claim(self):
        artifact = build_aggregate(_rows(8), subject=_SUBJECT)
        assert artifact["kind"] == EXPORT_KIND
        assert artifact["schema_version"] == EXPORT_SCHEMA_VERSION
        assert artifact["offline"] is True
        assert artifact["network_export"] is False
        assert artifact["production_claim"] is False
        assert artifact["privacy"]["raw_events_exported"] is False
        assert artifact["provenance"]["ingestion"] == "none"

    def test_artifact_is_coarse_not_a_raw_record_dump(self):
        artifact = build_aggregate(_rows(8), subject=_SUBJECT)
        encoded = json.dumps(artifact)
        for leak in ("/Users", "prompt", "source_code", "api_key", "email"):
            assert leak not in encoded
        assert artifact["privacy"]["coarse_only"] is True

    def test_artifact_reports_counts_and_a_banded_rate(self):
        artifact = build_aggregate(_rows(8), subject=_SUBJECT)
        assert sum(artifact["outcome_counts"].values()) == 8
        assert "-" in artifact["summary"]["pass_rate_band"]
        assert artifact["summary"]["sample_size"] == 8
        assert artifact["summary"]["sample_label"] == "adequate"

    def test_artifact_carries_a_coarse_recommendation(self):
        artifact = build_aggregate(_rows(8), subject=_SUBJECT)
        assert "confidence" in artifact["recommendation"]
        assert 0.0 <= artifact["recommendation"]["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# Aggregate service: preview/unavailable until every gate passes
# ---------------------------------------------------------------------------

class TestAggregateServiceStatus:
    def test_default_is_preview_unavailable(self):
        status = AggregateServiceStatus.evaluate()
        assert status.label == AGGREGATE_UNAVAILABLE_LABEL
        assert status.available is False
        assert set(status.gates) == set(AGGREGATE_GATES)
        assert status.to_dict()["production_claim"] is AGGREGATE_SERVICE_GA

    def test_every_gate_open_is_still_only_preview(self):
        status = _available_status()
        assert status.available is True
        assert status.label == AGGREGATE_AVAILABLE_LABEL == "preview"
        assert status.label != "available"

    def test_the_service_is_never_ga(self):
        assert AGGREGATE_SERVICE_GA is False
        assert all(
            not AggregateServiceStatus.evaluate(
                ingestion=g, cohort=g, quality=g, deletion=g, tenant_isolation=g
            ).to_dict()["production_claim"]
            for g in (False, True)
        )

    def test_an_undersized_cohort_cannot_satisfy_the_cohort_gate(self):
        status = AggregateServiceStatus.evaluate(
            cohort=True, cohort_size=DEFAULT_COHORT_MIN - 1,
        )
        assert status.gates["cohort"] is False
        assert status.available is False
        assert any("cohort" in reason for reason in status.reasons)

    def test_a_blocking_reason_keeps_the_service_unavailable(self):
        status = AggregateServiceStatus.evaluate(
            ingestion=True, cohort=True, quality=True, deletion=True,
            tenant_isolation=True, blocked_reason="tenant-isolation audit open",
        )
        assert status.available is False
        assert status.label == AGGREGATE_UNAVAILABLE_LABEL

    def test_an_inconsistent_status_label_is_refused(self):
        with pytest.raises(ExportBoundaryError):
            AggregateServiceStatus(
                label="available", available=True, gates={}, reasons=(), detail="",
            )
        with pytest.raises(ExportBoundaryError):
            AggregateServiceStatus(
                label=AGGREGATE_UNAVAILABLE_LABEL, available=True,
                gates={}, reasons=(), detail="",
            )

    def test_availability_cannot_be_claimed_with_a_gate_unmet(self):
        # A directly-constructed status must not be able to assert availability
        # while any gate is closed: availability requires every gate to pass.
        with pytest.raises(ExportBoundaryError):
            AggregateServiceStatus(
                label=AGGREGATE_AVAILABLE_LABEL, available=True,
                gates=dict.fromkeys(AGGREGATE_GATES, False),
                reasons=(), detail="",
            )
        # and the gate mapping must be exactly the five named gates
        with pytest.raises(ExportBoundaryError):
            AggregateServiceStatus(
                label=AGGREGATE_AVAILABLE_LABEL, available=True,
                gates={gate: True for gate in AGGREGATE_GATES[:4]},
                reasons=(), detail="",
            )
        # a fully-open, exactly-named gate set is the only available shape
        assert _available_status().available is True
        # all-pass does NOT force availability: a blocked reason still holds it
        blocked = AggregateServiceStatus.evaluate(
            ingestion=True, cohort=True, quality=True, deletion=True,
            tenant_isolation=True, blocked_reason="audit open",
        )
        assert blocked.available is False
        assert all(blocked.gates.values())

    def test_the_gate_set_is_pinned(self):
        assert AGGREGATE_GATES == (
            "ingestion", "cohort", "quality", "deletion", "tenant_isolation",
        )


# ---------------------------------------------------------------------------
# Edition contracts: Community / Pro / Enterprise
# ---------------------------------------------------------------------------

class TestEditionContracts:
    def test_all_editions_keep_events_local_offline_and_export_opt_in(self):
        for contract in CONTRACTS.values():
            assert contract.local_raw_events is True
            assert contract.offline is True
            assert contract.export_opt_in is True
            assert contract.export_default_off is True
            assert contract.aggregate_service_claim is AGGREGATE_SERVICE_GA

    def test_community_has_coarse_local_recommendations_only(self):
        assert COMMUNITY.coarse_local_recommendations is True
        assert COMMUNITY.fixture_backed_benchmark is False
        assert COMMUNITY.budgeting is False
        assert COMMUNITY.capability_mix is False
        assert COMMUNITY.hotspots is False
        assert COMMUNITY.tenant_isolation is False

    def test_pro_defines_benchmarks_budgeting_mix_and_hotspots(self):
        assert PRO.fixture_backed_benchmark is True
        assert PRO.budgeting is True
        assert PRO.capability_mix is True
        assert PRO.hotspots is True
        assert PRO.tenant_isolation is False

    def test_enterprise_adds_tenant_isolation_on_top_of_pro(self):
        assert ENTERPRISE.tenant_isolation is True
        assert ENTERPRISE.fixture_backed_benchmark is True
        assert ENTERPRISE.budgeting is True
        assert ENTERPRISE.capability_mix is True
        assert ENTERPRISE.hotspots is True

    def test_contract_lookup_is_fail_closed(self):
        assert contract_for(Edition.COMMUNITY) is COMMUNITY
        assert contract_for("pro") is PRO
        assert contract_for("enterprise") is ENTERPRISE
        with pytest.raises(UnknownEditionError):
            contract_for("community-plus")

    def test_contract_to_dict_is_complete(self):
        payload = COMMUNITY.to_dict()
        assert payload["edition"] == "community"
        assert set(payload) == {
            "edition", "local_raw_events", "offline", "export_opt_in",
            "export_default_off", "coarse_local_recommendations",
            "fixture_backed_benchmark", "budgeting", "capability_mix",
            "hotspots", "tenant_isolation", "aggregate_service_claim",
        }

    def test_community_offline_recommender_is_the_coarse_one(self):
        from skillweave.runtime.local_telemetry import recommend
        rec = recommend(_sealed(8))
        assert rec.sample_label in ("insufficient", "coarse", "adequate")
        assert COMMUNITY.coarse_local_recommendations is True


# ---------------------------------------------------------------------------
# Edition bypass: a lower edition cannot reach a higher edition's feature
# ---------------------------------------------------------------------------

class TestEditionBypass:
    def test_community_cannot_reach_a_pro_feature(self):
        for feature in (Feature.BENCHMARK, Feature.BUDGETING,
                        Feature.CAPABILITY_MIX, Feature.HOTSPOTS):
            with pytest.raises(EditionBypassError):
                require_capability("community", feature)

    def test_pro_cannot_reach_enterprise_tenant_isolation(self):
        with pytest.raises(EditionBypassError):
            require_capability("pro", Feature.TENANT_ISOLATION)

    def test_capabilities_are_granted_where_declared(self):
        require_capability("community", Feature.COARSE_LOCAL_RECOMMENDATIONS)
        require_capability("community", Feature.EXPORT)
        for feature in (Feature.BENCHMARK, Feature.BUDGETING,
                        Feature.CAPABILITY_MIX, Feature.HOTSPOTS,
                        Feature.EXPORT):
            require_capability("pro", feature)
        for feature in (Feature.BENCHMARK, Feature.BUDGETING,
                        Feature.CAPABILITY_MIX, Feature.HOTSPOTS,
                        Feature.EXPORT, Feature.TENANT_ISOLATION):
            require_capability("enterprise", feature)

    def test_no_edition_grants_the_aggregate_service(self):
        for edition in ("community", "pro", "enterprise"):
            with pytest.raises(EditionBypassError):
                require_capability(edition, Feature.AGGREGATE_SERVICE)

    def test_an_invented_feature_is_refused(self):
        with pytest.raises(EditionBypassError):
            EditionGuard.of("community").allows("turbo-mode")

    def test_an_invented_edition_is_refused(self):
        with pytest.raises(UnknownEditionError):
            EditionGuard.of("community-plus")

    def test_the_contract_is_frozen_against_mutation(self):
        with pytest.raises(FrozenInstanceError):
            COMMUNITY.hotspots = True
        # A guard built before the attempt still denies the feature.
        assert EditionGuard.of("community").allows(Feature.HOTSPOTS) is False

    def test_guard_reports_every_feature_it_gates(self):
        payload = EditionGuard.of("enterprise").to_dict()
        assert payload["edition"] == "enterprise"
        assert set(payload["features"]) == {f.value for f in Feature}
        assert all(payload["features"].values()) is False  # aggregate denied

    def test_allow_and_require_agree(self):
        guard = EditionGuard.of("pro")
        assert guard.allows(Feature.HOTSPOTS) is True
        guard.require(Feature.HOTSPOTS)
        assert guard.allows(Feature.TENANT_ISOLATION) is False


# ---------------------------------------------------------------------------
# Tenant boundary: a scoped exporter refuses to cross subjects
# ---------------------------------------------------------------------------

class TestTenantBoundary:
    def test_an_unknown_subject_is_refused(self):
        with pytest.raises(TenantBoundaryError):
            _exporter().consent_for("ghost")

    def test_a_duplicate_registration_is_refused(self):
        exporter = _exporter()
        exporter.register(ExportConsent("team-alpha"))
        with pytest.raises(ExportBoundaryError):
            exporter.register(ExportConsent("team-alpha"))

    def test_a_contribution_without_consent_is_refused(self):
        exporter = _exporter()
        exporter.register(ExportConsent("team-alpha"))
        with pytest.raises(ConsentRequiredError):
            exporter.contribute("team-alpha", _sealed(8))

    def test_a_contribution_after_revoke_is_refused(self):
        exporter = _exporter()
        consent = exporter.register(ExportConsent("team-alpha"))
        consent.grant()
        consent.revoke()
        with pytest.raises(ConsentRevokedError):
            exporter.contribute("team-alpha", _sealed(8))

    def test_export_while_the_service_is_unavailable_is_refused(self):
        exporter = AggregateExporter(AggregateServiceStatus.evaluate())
        consent = exporter.register(ExportConsent("team-alpha"))
        consent.grant()
        exporter.contribute("team-alpha", _sealed(8))
        with pytest.raises(AggregateUnavailableError):
            exporter.export("team-alpha")

    def test_a_contribution_from_one_tenant_never_appears_in_another(self):
        exporter = _exporter()
        a = exporter.register(ExportConsent("team-alpha")).grant()
        exporter.register(ExportConsent("team-beta")).grant()
        exporter.contribute("team-alpha", _sealed(8))
        artifact = exporter.export("team-alpha")
        assert artifact["cohort"]["size"] == 8
        assert artifact["consent"]["subject"] == "team-alpha"
        assert a.handle.handle == artifact["consent"]["deletion_handle"]


# ---------------------------------------------------------------------------
# Withdrawal: by deletion handle, without touching another tenant
# ---------------------------------------------------------------------------

class TestWithdrawal:
    def test_withdraw_revokes_the_consent_and_drops_contributions(self):
        exporter = _exporter()
        consent = exporter.register(ExportConsent("team-alpha")).grant()
        exporter.contribute("team-alpha", _sealed(8))
        receipt = exporter.withdraw(consent.handle)
        assert receipt["revoked"] is True
        assert receipt["contributions_removed"] == 8
        assert receipt["schema_version"] == EXPORT_SCHEMA_VERSION
        assert consent.revoked is True
        with pytest.raises(ConsentRevokedError):
            exporter.export("team-alpha")

    def test_withdrawal_by_handle_string_is_equivalent(self):
        exporter = _exporter()
        consent = exporter.register(ExportConsent("team-alpha")).grant()
        exporter.contribute("team-alpha", _sealed(8))
        receipt = exporter.withdraw(consent.handle.handle)
        assert receipt["subject"] == "team-alpha"

    def test_withdrawal_leaves_another_tenant_exportable(self):
        exporter = _exporter()
        alpha = exporter.register(ExportConsent("team-alpha")).grant()
        exporter.register(ExportConsent("team-beta")).grant()
        exporter.contribute("team-alpha", _sealed(8))
        exporter.contribute("team-beta", _sealed(8))
        exporter.withdraw(alpha.handle)
        beta = exporter.export("team-beta")
        assert beta["consent"]["subject"] == "team-beta"
        assert beta["cohort"]["size"] == 8

    def test_an_unknown_deletion_handle_is_refused(self):
        with pytest.raises(ExportBoundaryError):
            _exporter().withdraw("0" * 32)

    def test_withdrawal_receipt_names_the_subject_and_handle(self):
        exporter = _exporter()
        consent = exporter.register(ExportConsent("team-alpha")).grant()
        exporter.contribute("team-alpha", _sealed(8))
        receipt = exporter.withdraw(consent.handle)
        assert receipt["subject"] == "team-alpha"
        assert receipt["handle"] == consent.handle.handle


# ---------------------------------------------------------------------------
# Exporter preview: read-only, no gate required
# ---------------------------------------------------------------------------

class TestExporterPreview:
    def test_preview_works_while_the_service_is_unavailable(self):
        exporter = AggregateExporter(AggregateServiceStatus.evaluate())
        exporter.register(ExportConsent("team-alpha"))
        preview = exporter.preview("team-alpha")
        assert preview["exportable"] is False  # no contributions yet
        assert preview["granted"] is False

    def test_preview_of_a_clean_cohort_is_exportable_without_granting(self):
        exporter = _exporter()
        consent = exporter.register(ExportConsent("team-alpha")).grant()
        exporter.contribute("team-alpha", _sealed(8))
        preview = exporter.preview("team-alpha")
        assert preview["exportable"] is True
        assert preview["granted"] is True
        assert consent.is_active is True


# ---------------------------------------------------------------------------
# Standalone runner (bash -eo pipefail compatible)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    """Run all test functions directly (no pytest dependency)."""
    tests = []
    for module_member in list(globals().values()):
        if isinstance(module_member, type) and issubclass(module_member, object):
            for attr_name in dir(module_member):
                if attr_name.startswith("test_") and callable(getattr(module_member, attr_name)):
                    tests.append((module_member, attr_name))

    failed = 0
    for cls, test_name in sorted(tests, key=lambda x: (x[0].__name__, x[1])):
        test_fn = getattr(cls, test_name)
        instance = cls()
        try:
            if hasattr(instance, "setup_method"):
                instance.setup_method()
            test_fn(instance)
            print(f"PASS {cls.__name__}.{test_name}")
        except Exception as e:
            if type(e).__name__ == "Skipped":
                print(f"SKIP {cls.__name__}.{test_name}")
                continue
            failed += 1
            print(f"FAIL {cls.__name__}.{test_name}: {type(e).__name__}: {e}")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
