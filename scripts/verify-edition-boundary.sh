#!/usr/bin/env bash
# SW-159-EDITION-001 S3 verification harness.
#
# Runs, under `set -eo pipefail`, the nine required fixtures:
#   default-off, consent, deletion, prohibited-content, cohort-threshold,
#   withdrawal, poisoning, tenant-boundary, edition-bypass,
# plus the re-identification, aggregate-service-label, edition-contract, and
# aggregate-artifact fixtures the brief's acceptance surface requires.
#
# Every case is asserted; a single failure aborts the run. No network, no
# service: each fixture is a local, in-memory reproduction.
set -eo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/src"

EVIDENCE_DIR="${EVIDENCE_DIR:-$REPO_ROOT/tests_output/edition-boundary}"
mkdir -p "$EVIDENCE_DIR"
LOG="$EVIDENCE_DIR/s3-verify.log"
: > "$LOG"

log() { echo "$@" | tee -a "$LOG"; }

log "== SW-159-EDITION-001 S3 verification =="
log "repo=$REPO_ROOT"
log "head=$(git rev-parse HEAD)"
log "branch=$(git branch --show-current)"

# The harness itself is the reproduction: exit non-zero on any assertion.
python3 - "$EVIDENCE_DIR" <<'PY' 2>&1 | tee -a "$LOG"
import json
import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from skillweave.runtime.edition_boundary import (
    ABSOLUTE_COHORT_MIN, AGGREGATE_AVAILABLE_LABEL, AGGREGATE_GATES,
    AGGREGATE_SERVICE_GA, AGGREGATE_UNAVAILABLE_LABEL, COMMUNITY, CONTRACTS,
    DEFAULT_COHORT_MIN, ENTERPRISE, EXPORT_FORBIDDEN_KEYS, EXPORT_KIND,
    EXPORT_SCHEMA_VERSION, POISON_MAX_OUTCOME_SHARE, POISON_MAX_REPEAT_SHARE,
    PRO, RE_IDENTIFICATION_FLOOR, AggregateExporter, AggregateServiceStatus,
    AggregateUnavailableError, CohortTooSmallError, ConsentRequiredError,
    ConsentRevokedError, DeletionHandle, Edition, EditionBypassError,
    EditionGuard, ExportBoundaryError, ExportConsent, Feature, PoisoningError,
    ReIdentificationError, TenantBoundaryError, UnknownEditionError,
    build_aggregate, cohort_is_safe, contract_for, detect_poisoning,
    require_capability,
)
from skillweave.runtime.local_telemetry import (
    AssayPolicyName, GateOutcome, LocalTelemetryRecord, ModelIdentity,
    TelemetryPrivacyError, TelemetrySchemaError, VersionPoint,
)

evidence_dir = Path(sys.argv[1])
results = {}

SHA = "a" * 40
MI = ModelIdentity(catalogue_id="faigate/deepseek-v4-pro", tier="pro")
SUBJECT = "team-alpha"


def record(name, payload):
    results[name] = payload
    print(f"  [{name}] {json.dumps(payload, sort_keys=True)}")


def rows(n, *, pass_share=0.5, **overrides):
    """A clean, deterministic cohort of plain aggregate records."""
    out = []
    for i in range(n):
        passing = i < round(n * pass_share)
        row = dict(
            outcome=(GateOutcome.GATE_PASS.value if passing else GateOutcome.HOLD.value),
            starting_budget=10 * (i + 1), turns_used=i, retries=i,
            ast_delta=i, loc_delta=10 * i,
            interval_to_pass=(10 if passing else None),
            interval_to_hold=(None if passing else 5),
            policy=AssayPolicyName.MODERATE.value,
        )
        row.update(overrides)
        out.append(row)
    return out


def sealed(n, *, pass_share=0.5, **overrides):
    """A clean cohort of sealed telemetry records (the production shape)."""
    out = []
    for i in range(n):
        passing = i < round(n * pass_share)
        base = dict(
            version_point=VersionPoint(commit_sha=SHA, branch="feature/sw-159-edition-001"),
            methodology="rex", policy=AssayPolicyName.MODERATE.value,
            model_identity=MI, topology="sequential", risk="low",
            starting_budget=10 * (i + 1), turns_used=i, changes=i % 3,
            retries=i % 2, splits=0, loc_delta=10 * i, ast_delta=i, failures=0,
            interval_to_pass=(10 if passing else None),
            interval_to_hold=(None if passing else 5),
            outcome=(GateOutcome.GATE_PASS.value if passing else GateOutcome.HOLD.value),
        )
        base.update(overrides)
        out.append(LocalTelemetryRecord(**base).seal())
    return out


def available_status():
    return AggregateServiceStatus.evaluate(
        ingestion=True, cohort=True, quality=True, deletion=True,
        tenant_isolation=True,
    )


def exporter():
    return AggregateExporter(available_status())


# ---- 1. default-off: no consent, no export; a grant always has a handle ---
assert ExportConsent(SUBJECT).granted is False
assert ExportConsent(SUBJECT).handle is None
assert ExportConsent(SUBJECT).is_active is False
try:
    ExportConsent(SUBJECT, granted=True)
    raise SystemExit("default-off: granted consent without a handle was accepted")
except ExportBoundaryError:
    pass
try:
    ExportConsent(SUBJECT, schema_version=EXPORT_SCHEMA_VERSION + 1)
    raise SystemExit("default-off: unsupported schema version was accepted")
except ExportBoundaryError:
    pass
try:
    ExportConsent(SUBJECT, min_cohort=ABSOLUTE_COHORT_MIN - 1)
    raise SystemExit("default-off: cohort below the absolute floor was accepted")
except ExportBoundaryError:
    pass
for bad_subject in ("Alice@Example.com", "alice@example.com", "a b", "", "A"):
    try:
        ExportConsent(bad_subject)
        raise SystemExit(f"default-off: raw subject {bad_subject!r} was accepted")
    except ExportBoundaryError:
        pass
assert EXPORT_SCHEMA_VERSION == 1 and EXPORT_KIND == "skillweave.telemetry-export"
record("default_off", {
    "fresh_consent_off": True, "granted_without_handle_refused": True,
    "unsupported_version_refused": True, "sub_floor_cohort_refused": True,
    "raw_subjects_refused": True, "schema_version": EXPORT_SCHEMA_VERSION,
})

# ---- 2. consent: granted through grant(), carries handle + version --------
granted = ExportConsent(SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
assert granted.granted is True and granted.is_active is True
assert granted.handle is not None and len(granted.handle.handle) == 32
assert granted.handle.schema_version == EXPORT_SCHEMA_VERSION
assert granted.handle.created_at == "2026-09-28T00:00:00Z"
again = ExportConsent(SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
later = ExportConsent(SUBJECT).grant(created_at="2026-09-29T00:00:00Z")
assert again.handle.handle == granted.handle.handle
assert later.handle.handle != granted.handle.handle
# preview shows the would-be export and never grants
preview_consent = ExportConsent(SUBJECT)
preview = preview_consent.preview(rows(8))
assert preview["exportable"] is True and preview["granted"] is False
assert preview["schema_version"] == EXPORT_SCHEMA_VERSION
assert preview_consent.is_active is False
bad_preview = ExportConsent(SUBJECT).preview(rows(4))
assert bad_preview["exportable"] is False and "CohortTooSmallError" in bad_preview["reason"]
redacted = ExportConsent(SUBJECT).preview([{"prompt": "hello there friend"}])
assert redacted["exportable"] is False and "TelemetryPrivacyError" in redacted["reason"]
record("consent", {
    "grant_mints_handle": True, "handle_len": len(granted.handle.handle),
    "handle_deterministic": True, "handle_changes_with_time": True,
    "preview_does_not_grant": True, "preview_schema_version": preview["schema_version"],
    "preview_reports_cohort_refusal": True, "preview_reports_content_refusal": True,
})

# ---- 3. deletion: handle is opaque + revocable; unknown handle refused -----
revoked = ExportConsent(SUBJECT).grant(created_at="2026-09-28T00:00:00Z")
snapshot = revoked.handle
revoked.revoke()
assert revoked.granted is False and revoked.revoked is True and revoked.is_active is False
assert revoked.handle.revoked is True and snapshot.revoked is False
try:
    exporter().withdraw("0" * 32)
    raise SystemExit("deletion: an unknown deletion handle was accepted")
except ExportBoundaryError:
    pass
record("deletion", {
    "handle_revocable": True, "handle_survives_revoke": True,
    "unknown_handle_refused": True,
})

# ---- 4. prohibited content: prompts/source/secrets/PII/paths REJECTED -----
for key in ("prompt", "source_code", "api_key", "token", "email", "user_name",
            "content", "path", "raw_events", "stdout"):
    assert key in EXPORT_FORBIDDEN_KEYS, key
    try:
        build_aggregate([{key: "placeholder"}], subject=SUBJECT)
        raise SystemExit(f"prohibited-content: raw key {key!r} was accepted")
    except TelemetryPrivacyError:
        pass
for value in ("wrote /Users/alice/secret.txt",
              "changed src/skillweave/runtime/edition_boundary.py",
              "alice@example.com", "123-45-6789", "10.0.0.1",
              "ghp_16C7e42F292c6912E7710c838347Ae178B4a"):
    try:
        build_aggregate([{"note": value}], subject=SUBJECT)
        raise SystemExit(f"prohibited-content: value {value!r} was accepted")
    except TelemetryPrivacyError:
        pass
# a secret-shaped subject is refused by the value scan, not merely the regex
try:
    build_aggregate(rows(8), subject="skillweaveauthxx")
    raise SystemExit("prohibited-content: secret-shaped subject was accepted")
except TelemetryPrivacyError:
    pass
# rejection precedes the cohort check: a one-record cohort is refused for
# content, never masked and never admitted by a later gate
try:
    build_aggregate([{"prompt": "one two three four"}], subject=SUBJECT)
    raise SystemExit("prohibited-content: content was masked by the cohort check")
except TelemetryPrivacyError:
    pass
# the tenant boundary refuses an unsealed/raw record at contribute() time
exp = exporter()
exp.register(ExportConsent(SUBJECT).grant(created_at="2026-09-28T00:00:00Z"))
try:
    exp.contribute(SUBJECT, [{"prompt": "one two three four"}])
    raise SystemExit("prohibited-content: raw record reached the tenant boundary")
except (TelemetrySchemaError, TelemetryPrivacyError):
    pass
record("prohibited_content", {
    "raw_keys_refused": True, "prohibited_values_refused": True,
    "secret_subject_refused": True, "rejection_precedes_cohort_check": True,
    "raw_record_refused_at_boundary": True,
})

# ---- 5. cohort threshold + re-identification floor ------------------------
assert cohort_is_safe(5) is True and cohort_is_safe(4) is False
assert cohort_is_safe(3) is False  # below the default min of 5
assert cohort_is_safe(3, min_cohort=ABSOLUTE_COHORT_MIN) is True  # == floor
assert cohort_is_safe(4, min_cohort=ABSOLUTE_COHORT_MIN) is True
assert cohort_is_safe(True) is False and cohort_is_safe("8") is False
try:
    build_aggregate(rows(4), subject=SUBJECT)
    raise SystemExit("cohort-threshold: an undersized cohort was exported")
except CohortTooSmallError:
    pass
# a consent cannot lower the threshold past the absolute floor
try:
    ExportConsent(SUBJECT, min_cohort=ABSOLUTE_COHORT_MIN - 1)
    raise SystemExit("cohort-threshold: sub-floor min_cohort was accepted")
except ExportBoundaryError:
    pass
assert RE_IDENTIFICATION_FLOOR == 3 and ABSOLUTE_COHORT_MIN == 3
record("cohort_threshold", {
    "undersized_refused": True, "absolute_floor_holds": True,
    "default_min_cohort": DEFAULT_COHORT_MIN,
    "cohort_is_safe_boundary": {"5": True, "4": False, "3": True},
})

# ---- 6. re-identification: a reported bucket below the floor is refused ----
# 6 pass / 2 hold: the cohort clears the threshold but the smallest reported
# bucket (2) is below the floor (3), so the aggregate re-identifies and is
# refused. This is a *reported-bucket* floor, distinct from the cohort size.
try:
    build_aggregate(rows(8, pass_share=0.75), subject=SUBJECT)
    raise SystemExit("re-identification: a bucket below the floor was reported")
except ReIdentificationError:
    pass
artifact = build_aggregate(rows(8), subject=SUBJECT)  # 4/4: safe
assert artifact["cohort"]["smallest_reported_bucket"] >= RE_IDENTIFICATION_FLOOR
assert artifact["cohort"]["k_anonymous"] is True
record("re_identification", {
    "imbalanced_bucket_refused": True,
    "balanced_smallest_bucket": artifact["cohort"]["smallest_reported_bucket"],
    "floor": RE_IDENTIFICATION_FLOOR,
})

# ---- 7. poisoning: replayed payload / degenerate outcome refused -----------
dup = rows(1, pass_share=1.0)[0]        # one exact payload...
replayed = [dict(dup) for _ in range(6)] + rows(4)  # ...repeated 6/10 times
replay_signals = detect_poisoning(replayed)
assert any(s.kind == "replayed-record" for s in replay_signals), replay_signals
assert not any(s.kind == "degenerate-outcome" for s in replay_signals), replay_signals
degenerate = []
for i in range(10):
    row = dict(rows(1, pass_share=1.0)[0])
    row["starting_budget"] = 100 + i  # distinct payloads, one outcome
    degenerate.append(row)
deg_signals = detect_poisoning(degenerate)
assert any(s.kind == "degenerate-outcome" for s in deg_signals), deg_signals
assert not any(s.kind == "replayed-record" for s in deg_signals), deg_signals
assert detect_poisoning(rows(8)) == []  # a clean cohort carries no signal
try:
    build_aggregate(replayed, subject=SUBJECT)
    raise SystemExit("poisoning: a replayed cohort was exported")
except PoisoningError:
    pass
try:
    build_aggregate(degenerate, subject=SUBJECT)
    raise SystemExit("poisoning: a degenerate cohort was exported")
except PoisoningError:
    pass
record("poisoning", {
    "replayed_record_detected": True, "degenerate_outcome_detected": True,
    "clean_cohort_has_no_signal": True, "export_refused_on_signal": True,
    "max_repeat_share": POISON_MAX_REPEAT_SHARE,
    "max_outcome_share": POISON_MAX_OUTCOME_SHARE,
})

# ---- 8. withdrawal: by handle; another tenant is untouched ----------------
exp = exporter()
alpha = exp.register(ExportConsent("team-alpha").grant(created_at="2026-09-28T00:00:00Z"))
beta = exp.register(ExportConsent("team-beta").grant(created_at="2026-09-28T00:00:00Z"))
assert exp.contribute("team-alpha", sealed(8)) == 8
assert exp.contribute("team-beta", sealed(8)) == 8
assert exp.export("team-alpha")["consent"]["deletion_handle"] == alpha.handle.handle
receipt = exp.withdraw(alpha.handle)
assert receipt["subject"] == "team-alpha" and receipt["revoked"] is True
assert receipt["contributions_removed"] == 8
assert receipt["schema_version"] == EXPORT_SCHEMA_VERSION
# the withdrawn tenant is no longer exportable; the other still is
try:
    exp.export("team-alpha")
    raise SystemExit("withdrawal: the withdrawn tenant was still exportable")
except ConsentRevokedError:
    pass
assert exp.export("team-beta")["consent"]["deletion_handle"] == beta.handle.handle
# withdrawing by the raw handle string is equivalent
exp.withdraw(str(beta.handle.handle))
try:
    exp.export("team-beta")
    raise SystemExit("withdrawal: string-handle withdrawal did not take effect")
except ConsentRevokedError:
    pass
record("withdrawal", {
    "withdraw_by_handle": True, "contributions_removed": 8,
    "withdrawn_tenant_refused": True, "other_tenant_untouched": True,
    "withdraw_by_string_equivalent": True,
})

# ---- 9. tenant boundary: unknown subject, duplicate, no-consent ------------
exp = exporter()
try:
    exp.consent_for("team-ghost")
    raise SystemExit("tenant-boundary: an unknown subject was resolved")
except TenantBoundaryError:
    pass
exp.register(ExportConsent(SUBJECT).grant(created_at="2026-09-28T00:00:00Z"))
try:
    exp.register(ExportConsent(SUBJECT))
    raise SystemExit("tenant-boundary: a duplicate registration was accepted")
except ExportBoundaryError:
    pass
fresh = exporter()
fresh.register(ExportConsent(SUBJECT))  # registered but never granted
try:
    fresh.contribute(SUBJECT, sealed(8))
    raise SystemExit("tenant-boundary: contribution without consent was accepted")
except ConsentRequiredError:
    pass
try:
    fresh.export(SUBJECT)
    raise SystemExit("tenant-boundary: export without consent was accepted")
except ConsentRequiredError:
    pass
unavailable = AggregateExporter(AggregateServiceStatus.evaluate())
unavailable.register(ExportConsent(SUBJECT).grant(created_at="2026-09-28T00:00:00Z"))
unavailable.contribute(SUBJECT, sealed(8))
try:
    unavailable.export(SUBJECT)
    raise SystemExit("tenant-boundary: export while unavailable was accepted")
except AggregateUnavailableError:
    pass
record("tenant_boundary", {
    "unknown_subject_refused": True, "duplicate_registration_refused": True,
    "contribution_without_consent_refused": True,
    "export_without_consent_refused": True,
    "export_while_unavailable_refused": True,
})

# ---- 10. edition bypass: a Community caller cannot reach a Pro feature -----
guard = EditionGuard.of(Edition.COMMUNITY)
for feature in (Feature.BENCHMARK, Feature.BUDGETING, Feature.CAPABILITY_MIX,
                Feature.HOTSPOTS, Feature.TENANT_ISOLATION,
                Feature.AGGREGATE_SERVICE):
    assert guard.allows(feature) is False, feature
    try:
        guard.require(feature)
        raise SystemExit(f"edition-bypass: community granted {feature.value!r}")
    except EditionBypassError:
        pass
assert guard.allows(Feature.COARSE_LOCAL_RECOMMENDATIONS) is True
assert guard.allows(Feature.EXPORT) is True
# an invented edition is refused outright
try:
    EditionGuard.of("platinum")
    raise SystemExit("edition-bypass: an invented edition was accepted")
except UnknownEditionError:
    pass
# the contract is a frozen constant: a caller cannot mutate Pro features in
try:
    guard.contract.hotspots = True  # type: ignore[misc]
    raise SystemExit("edition-bypass: the contract was mutated")
except FrozenInstanceError:
    pass
# the aggregate-service claim is denied in every edition
for contract in CONTRACTS.values():
    assert contract.aggregate_service_claim is AGGREGATE_SERVICE_GA is False
# require_capability fails closed the same way
try:
    require_capability("community", "hotspots")
    raise SystemExit("edition-bypass: require_capability granted a Pro feature")
except EditionBypassError:
    pass
require_capability("pro", "hotspots")  # Pro does grant it
record("edition_bypass", {
    "community_denies_pro_features": True, "unknown_edition_refused": True,
    "contract_frozen": True, "aggregate_service_denied_everywhere": True,
    "require_capability_fails_closed": True,
})

# ---- 11. aggregate service label: preview/unavailable until all gates ------
default = AggregateServiceStatus.evaluate()
assert default.label == AGGREGATE_UNAVAILABLE_LABEL
assert default.available is False
assert set(default.gates) == set(AGGREGATE_GATES)
partial = AggregateServiceStatus.evaluate(ingestion=True, cohort=True)
assert partial.label == AGGREGATE_UNAVAILABLE_LABEL and partial.available is False
assert "quality" in partial.reasons
# a declared cohort gate cannot pass an undersized cohort
contradicted = AggregateServiceStatus.evaluate(
    cohort=True, cohort_size=2, ingestion=True, quality=True, deletion=True,
    tenant_isolation=True)
assert contradicted.gates["cohort"] is False
assert contradicted.label == AGGREGATE_UNAVAILABLE_LABEL
allpass = available_status()
assert allpass.label == AGGREGATE_AVAILABLE_LABEL == "preview"
assert allpass.available is True and allpass.to_dict()["production_claim"] is False
assert AGGREGATE_SERVICE_GA is False
blocked = AggregateServiceStatus.evaluate(
    ingestion=True, cohort=True, quality=True, deletion=True,
    tenant_isolation=True, blocked_reason="tenant-isolation evidence missing")
assert blocked.available is False and blocked.label == AGGREGATE_UNAVAILABLE_LABEL
# availability cannot be claimed directly with a gate unmet: it is exactly the
# conjunction of the five gates, so a forged status cannot open the service
try:
    AggregateServiceStatus(
        label=AGGREGATE_AVAILABLE_LABEL, available=True,
        gates=dict.fromkeys(AGGREGATE_GATES, False), reasons=(), detail="")
    raise SystemExit("aggregate-label: availability was forged with gates unmet")
except ExportBoundaryError:
    pass
record("aggregate_service_label", {
    "default": default.label, "partial": partial.label,
    "all_gates": allpass.label, "contradicted_cohort_gate": False,
    "production_claim": False, "gates": list(AGGREGATE_GATES),
})

# ---- 12. editions: honest Community / Pro / Enterprise contracts -----------
assert COMMUNITY.local_raw_events and COMMUNITY.offline
assert COMMUNITY.coarse_local_recommendations
assert not (COMMUNITY.fixture_backed_benchmark or COMMUNITY.budgeting
            or COMMUNITY.capability_mix or COMMUNITY.hotspots
            or COMMUNITY.tenant_isolation)
assert PRO.fixture_backed_benchmark and PRO.budgeting
assert PRO.capability_mix and PRO.hotspots and PRO.local_raw_events and PRO.offline
assert not PRO.tenant_isolation
assert ENTERPRISE.tenant_isolation and ENTERPRISE.fixture_backed_benchmark
for contract in CONTRACTS.values():
    assert contract.export_opt_in is True and contract.export_default_off is True
    assert contract.local_raw_events is True and contract.offline is True
    assert contract.aggregate_service_claim is False
assert contract_for("community") is COMMUNITY
assert contract_for(Edition.ENTERPRISE) is ENTERPRISE
record("editions", {
    "community": COMMUNITY.to_dict(), "pro": PRO.to_dict(),
    "enterprise": ENTERPRISE.to_dict(), "all_offline_local_optin_default_off": True,
})

# ---- 13. aggregate artifact: coarse only, no raw events, no network --------
artifact = build_aggregate(rows(8), subject=SUBJECT)
assert artifact["kind"] == EXPORT_KIND
assert artifact["schema_version"] == EXPORT_SCHEMA_VERSION
assert artifact["privacy"]["raw_events_exported"] is False
assert artifact["privacy"]["coarse_only"] is True
assert artifact["privacy"]["forbidden_keys_rejected"] is True
assert artifact["offline"] is True
assert artifact["network_export"] is False
assert artifact["production_claim"] is False
assert artifact["cohort"]["size"] == 8 and artifact["cohort"]["k_anonymous"] is True
assert artifact["summary"]["sample_label"] in ("coarse", "adequate")
assert "outcome_counts" in artifact and "recommendation" in artifact
# no forbidden key appears anywhere in the serialised artifact
blob = json.dumps(artifact, sort_keys=True)
assert not any(f'"{k}"' in blob for k in EXPORT_FORBIDDEN_KEYS), blob
record("aggregate_artifact", {
    "kind": artifact["kind"], "schema_version": artifact["schema_version"],
    "raw_events_exported": False, "network_export": False,
    "production_claim": False, "cohort_size": artifact["cohort"]["size"],
    "sample_label": artifact["summary"]["sample_label"],
    "forbidden_key_free": True,
})

(evidence_dir / "s3-results.json").write_text(
    json.dumps(results, indent=2), encoding="utf-8")
print("ALL S3 CASES PASSED")
PY

rc=$?
log "== S3 harness exit: $rc =="
exit $rc
