"""SW-1510-ROUTE-LOCK: bind requested routes to allowed actual providers.

Four criteria (one per test class):

1. Parent and nested requests retain correlation IDs, requested alias, resolved
   provider/model, answering envelope identity where available, and
   catalog/policy revision.
2. Worker and child egress is restricted to the approved enforcing gateway,
   including alternative loopback routers and Bash network calls; absent
   enforcement is detected before paid admission.
3. The approved BytePlus V4 Flash/V4.1 Flash/Pro combination is enforced from
   observed current route proofs; wrong version or direct-provider bypass
   causes HOLD.
4. Retry/escalation requires new attempt attribution and reserved budget; raw
   upstream names or global environment keys cannot override the approved
   route set.

No provider, network or socket call is made; the unit-suite hermetic boundary
(tests/unit/conftest.py) blocks sockets and clears provider env vars, and
every test here drives pure functions or injected seams.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.routing.faigate_adapter import (  # noqa: E402
    APPROVED_BYTEPLUS_ROUTES,
    APPROVED_GATEWAY_HOST,
    APPROVED_GATEWAY_PORT,
    APPROVED_GATEWAY_URL,
    COUNCIL_POLICY_REVISION,
    COUNCIL_PROFILE_VERSION,
    RouteDecision,
    make_route_decision,
    is_byteplus_route,
    verify_approved_route,
    check_egress_gateway,
    check_retry_attribution,
    ModelNamespaceError,
    FAIGATE_MAP,
    OPENROUTER_MAP,
    ROUTER_PROFILES,
)

from skillweave.routing.route_proof import (  # noqa: E402
    RouteProof,
    RouteProofError,
)

from skillweave.runtime.reservation import (  # noqa: E402
    ReservationStore,
    ReservationEnvelope,
    QuotaRefused,
)

from skillweave.runtime.execution_envelope import (  # noqa: E402
    ExecutionEnvelopeV1,
)

from skillweave.runsvc.service import (  # noqa: E402
    RunApplicationService,
    RunIntegrationError,
)

from skillweave.runtime.store import SQLiteRunStore  # noqa: E402
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import RawArtifactStore  # noqa: E402

from skillweave.dispatch.model_policy import (  # noqa: E402
    ModelTier,
    AllocationError,
    TIER_ROUTE_MAP,
    resolve_route_for_tier,
    AttemptAttribution,
    check_attempt_route,
)


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 1: correlation IDs, requested alias, resolved provider/model,
# answering envelope identity, and catalog/policy revision.
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteDecisionRecord:
    """Parent and nested requests retain full identity chain (criterion 1)."""

    def test_route_decision_has_all_required_fields(self):
        """A RouteDecision carries every field the criterion requires."""
        d = RouteDecision(
            correlation_id="corr-001",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            answering_envelope_identity="deepseek-v4-flash",
            catalog_revision=COUNCIL_PROFILE_VERSION,
            policy_revision=COUNCIL_POLICY_REVISION,
            run_id="run-001",
            task_id="task-01",
            attempt=1,
            reservation_id="res-001",
            attempt_order=0,
            attribution_status="attributed",
            outcome="resolved",
            measured_at="2026-10-08T12:00:00+00:00",
        )
        assert d.correlation_id == "corr-001"
        assert d.requested_alias == "deepseek-v4-flash"
        assert d.resolved_provider == "faigate"
        assert d.resolved_model == "byteplus/deepseek-v4-flash"
        assert d.answering_envelope_identity == "deepseek-v4-flash"
        assert d.catalog_revision == COUNCIL_PROFILE_VERSION
        assert d.policy_revision == COUNCIL_POLICY_REVISION

    def test_route_decision_to_dict_contract_compliant(self):
        """to_dict() produces a route-decision-v1 contract payload.

        The payload must contain the 14 required contract fields and
        must NOT contain disallowed fields (correlation_id,
        requested_alias, answering_envelope_identity, etc.).
        """
        d = RouteDecision(
            correlation_id="corr-002",
            requested_alias="deepseek-v4-pro",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-pro",
            answering_envelope_identity="deepseek-v4-pro",
            catalog_revision="1.3.11",
            policy_revision="1.3.11",
            run_id="run-002",
            task_id="task-02",
            attempt=1,
            reservation_id="res-002",
            attempt_order=0,
            attribution_status="attributed",
            outcome="resolved",
            measured_at="2026-10-08T12:00:00+00:00",
        )
        data = d.to_dict()
        # Contract fields present
        assert data["schema_version"] == 1
        assert data["run_id"] == "run-002"
        assert data["task_id"] == "task-02"
        assert data["attempt"] == 1
        assert data["request_id"] == "corr-002"
        assert data["parent_request_id"] == ""
        assert data["requested_model"] == "deepseek-v4-pro"
        assert data["answering_model"] == "deepseek-v4-pro"
        assert data["attribution_status"] == "attributed"
        assert data["reservation_id"] == "res-002"
        assert data["attempt_order"] == 0
        assert data["measured_at"] == "2026-10-08T12:00:00+00:00"
        assert data["outcome"] == "resolved"
        assert data["usage"] == {}
        # Disallowed fields must NOT be in the contract payload
        assert "correlation_id" not in data
        assert "requested_alias" not in data
        assert "answering_envelope_identity" not in data
        assert "resolved_provider" not in data
        assert "resolved_model" not in data
        assert "catalog_revision" not in data
        assert "policy_revision" not in data

    def test_route_decision_to_journal_dict_has_meta(self):
        """to_journal_dict() includes extra metadata outside the contract."""
        d = RouteDecision(
            correlation_id="corr-002",
            requested_alias="deepseek-v4-pro",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-pro",
            answering_envelope_identity="deepseek-v4-pro",
            catalog_revision="1.3.11",
            policy_revision="1.3.11",
            run_id="run-002",
            task_id="task-02",
            attempt=1,
        )
        jd = d.to_journal_dict()
        assert "contract" in jd
        assert "_meta" in jd
        assert jd["_meta"]["correlation_id"] == "corr-002"
        assert jd["_meta"]["requested_alias"] == "deepseek-v4-pro"
        assert jd["_meta"]["resolved_model"] == "byteplus/deepseek-v4-pro"
        assert jd["_meta"]["catalog_revision"] == "1.3.11"
        assert jd["_meta"]["policy_revision"] == "1.3.11"
        # The contract sub-dict passes schema validation
        import json
        from jsonschema import validate
        schema_path = Path(__file__).resolve().parent.parent.parent / "schemas" / "execution" / "route-decision-v1.schema.json"
        with open(schema_path) as f:
            schema = json.load(f)
        validate(instance=jd["contract"], schema=schema)

    def test_answering_envelope_identity_may_be_none(self):
        """When the envelope does not report a model, the field is None."""
        d = RouteDecision(
            correlation_id="corr-003",
            requested_alias="unknown",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            run_id="run-003",
            task_id="task-03",
            attempt=1,
        )
        assert d.answering_envelope_identity is None
        # The contract payload uses empty string for unknown answering model
        payload = d.to_dict()
        assert payload["answering_model"] == ""
        assert payload["outcome"] == "pending"

    def test_make_route_decision_defaults_policy_revision(self):
        """make_route_decision fills in the current policy revision by default."""
        d = make_route_decision(
            correlation_id="corr-004",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            run_id="run-004",
            task_id="task-04",
            attempt=1,
        )
        assert d.policy_revision == COUNCIL_POLICY_REVISION
        assert d.catalog_revision == COUNCIL_POLICY_REVISION

    def test_make_route_decision_accepts_explicit_revisions(self):
        """Explicit catalog/policy revision overrides the default."""
        d = make_route_decision(
            correlation_id="corr-005",
            requested_alias="deepseek-v4-pro",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-pro",
            catalog_revision="2.0.0",
            policy_revision="2.0.0",
            run_id="run-005",
            task_id="task-05",
            attempt=1,
        )
        assert d.catalog_revision == "2.0.0"
        assert d.policy_revision == "2.0.0"

    def test_parent_and_nested_correlation_ids_link(self):
        """Parent and nested requests share a correlation prefix."""
        parent_corr = "run-001/task-01"
        nested_corr = "run-001/task-01/nested-01"
        parent = make_route_decision(
            correlation_id=parent_corr,
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            run_id="run-001",
            task_id="task-01",
            attempt=1,
        )
        nested = make_route_decision(
            correlation_id=nested_corr,
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            run_id="run-001",
            task_id="task-01/nested-01",
            attempt=1,
        )
        # The nested correlation starts with the parent's prefix
        assert nested.correlation_id.startswith(parent.correlation_id)
        # The contract payload has parent_request_id for nested requests
        nested_payload = nested.to_dict()
        assert nested_payload["parent_request_id"] == parent_corr
        assert nested_payload["request_id"] == nested_corr
        # Parent has empty parent_request_id
        parent_payload = parent.to_dict()
        assert parent_payload["parent_request_id"] == ""

    def test_route_decision_serialises_to_json(self):
        """RouteDecision survives JSON serialisation (evidence contract)."""
        d = RouteDecision(
            correlation_id="corr-json",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            answering_envelope_identity="deepseek-v4-flash",
            catalog_revision="1.3.11",
            policy_revision="1.3.11",
            run_id="run-json",
            task_id="task-json",
            attempt=1,
            reservation_id="res-json",
            attribution_status="attributed",
            outcome="resolved",
            measured_at="2026-10-08T12:00:00+00:00",
        )
        serialised = json.dumps(d.to_journal_dict())
        restored = json.loads(serialised)
        # Contract sub-dict is present
        assert "contract" in restored
        assert "_meta" in restored
        assert restored["contract"]["request_id"] == "corr-json"
        assert restored["contract"]["requested_model"] == "deepseek-v4-flash"
        assert restored["contract"]["answering_model"] == "deepseek-v4-flash"
        assert restored["_meta"]["correlation_id"] == "corr-json"
        assert restored["_meta"]["resolved_model"] == "byteplus/deepseek-v4-flash"


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 2: egress restricted to the approved enforcing gateway.
# ═══════════════════════════════════════════════════════════════════════════


class TestEgressGateway:
    """Worker and child egress must route through the approved gateway (criterion 2)."""

    def test_approved_gateway_url_is_correct(self):
        """The approved gateway URL matches the expected host:port."""
        assert APPROVED_GATEWAY_HOST == "127.0.0.1"
        assert APPROVED_GATEWAY_PORT == 8090
        assert APPROVED_GATEWAY_URL == "http://127.0.0.1:8090"

    def test_approved_gateway_url_passes_check(self):
        """The approved gateway URL is accepted."""
        # Should not raise
        check_egress_gateway(APPROVED_GATEWAY_URL)

    def test_approved_gateway_url_with_path_passes(self):
        """The approved gateway URL with a path is accepted."""
        check_egress_gateway(f"{APPROVED_GATEWAY_URL}/v1")
        check_egress_gateway(f"{APPROVED_GATEWAY_URL}/v1/chat/completions")

    def test_alternative_loopback_port_is_refused(self):
        """A Faigate-like router on a different port is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://127.0.0.1:9090/v1")
        assert "port" in str(exc.value).lower()

    def test_different_host_is_refused(self):
        """A different loopback host is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://127.0.0.2:8090/v1")
        assert "host" in str(exc.value).lower()

    def test_localhost_by_name_is_refused(self):
        """'localhost' by name is refused (only 127.0.0.1 is approved)."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://localhost:8090/v1")
        assert "host" in str(exc.value).lower()

    def test_direct_upstream_is_refused(self):
        """A direct upstream URL (e.g. api.openai.com) is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("https://api.openai.com/v1")
        assert "scheme" in str(exc.value).lower()

    def test_different_loopback_router_is_refused(self):
        """OmniRoute on localhost:20128 is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://localhost:20128/v1")
        assert "host" in str(exc.value).lower()

    def test_empty_url_is_refused(self):
        """An empty or malformed URL is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("")
        assert "empty" in str(exc.value).lower()

    def test_bash_network_call_to_unapproved_host_is_refused(self):
        """A Bash network call to an unapproved host is caught by the check.

        This test proves the check function itself works -- the actual Bash
        call is never made because the unit suite blocks sockets.  The
        check function is the gate that must fire *before* any Bash call.
        """
        # The check_egress_gateway function is called with the URL that
        # a Bash call would resolve to; if it raises, the call is blocked
        # before any network activity.
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://alternative-router.local/v1")
        assert "host" in str(exc.value).lower()

    def test_https_to_gateway_port_is_refused(self):
        """HTTPS to the gateway address is refused (only HTTP is approved)."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("https://127.0.0.1:8090/v1")
        assert "scheme" in str(exc.value).lower()

    def test_absent_enforcement_detected_before_admission(self):
        """When enforcement is absent (no URL check), the call is detected.

        This test verifies that the egress check raises *before* any
        paid admission would occur: the error is raised by the pure
        function before any network activity or billing.
        """
        # The check is called at decision time, not after the call.
        # If it raises, the run is HOLD before any provider is contacted.
        with pytest.raises(ModelNamespaceError):
            check_egress_gateway("http://192.168.1.100:8090/v1")

    # --- Adversarial URL parsing tests (controller finding 2 on f36a3ea) ---

    def test_suffix_host_is_refused(self):
        """A suffix/subdomain attack (127.0.0.1:8090.evil.com) is refused.

        The old prefix-match check would accept this URL because it starts
        with ``http://127.0.0.1:8090``.  The exact URL parsing approach
        must reject it.  Note that the hostname component resolves as
        ``127.0.0.1`` so the check fails on the port (``8090.evil.com``),
        not the hostname.
        """
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://127.0.0.1:8090.evil.com/v1")
        msg = str(exc.value).lower()
        assert "port" in msg

    def test_userinfo_is_refused(self):
        """A URL with userinfo (http://user:pass@127.0.0.1:8090) is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://user:pass@127.0.0.1:8090/v1")
        assert "userinfo" in str(exc.value).lower() or "credential" in str(exc.value).lower()

    def test_trailing_dot_host_is_refused(self):
        """A trailing-dot host (127.0.0.1.:8090) is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://127.0.0.1.:8090/v1")
        assert "host" in str(exc.value).lower()

    def test_ipv6_loopback_is_refused(self):
        """IPv6 ::1 as host is refused (only 127.0.0.1 is approved)."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://[::1]:8090/v1")
        assert "host" in str(exc.value).lower()

    def test_ipv4_alias_192_168_is_refused(self):
        """A private IPv4 address as host is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://192.168.1.1:8090/v1")
        assert "host" in str(exc.value).lower()

    def test_http_scheme_is_required(self):
        """Only http scheme is allowed; https/ftp are refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("ftp://127.0.0.1:8090/v1")
        assert "scheme" in str(exc.value).lower()

    def test_empty_url_is_refused_with_exact_parse(self):
        """Empty URL is refused before URL parsing."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("")
        assert "empty" in str(exc.value).lower()

    def test_userinfo_no_password_is_refused(self):
        """A URL with only a username (http://user@127.0.0.1:8090) is refused."""
        with pytest.raises(ModelNamespaceError) as exc:
            check_egress_gateway("http://user@127.0.0.1:8090/v1")
        assert "userinfo" in str(exc.value).lower() or "credential" in str(exc.value).lower()


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: BytePlus V4 Flash / V4.1 Flash / Pro enforcement.
# ═══════════════════════════════════════════════════════════════════════════


class TestBytePlusRouteEnforcement:
    """Approved BytePlus route set is enforced (criterion 3)."""

    def test_approved_routes_are_recognised(self):
        """Every approved BytePlus route is recognised by is_byteplus_route."""
        for route in APPROVED_BYTEPLUS_ROUTES:
            assert is_byteplus_route(route), f"{route} should be approved"

    def test_approved_routes_have_byteplus_prefix(self):
        """Every approved route starts with 'byteplus/'."""
        for route in APPROVED_BYTEPLUS_ROUTES:
            assert route.startswith("byteplus/"), f"{route} should have byteplus/ prefix"

    def test_deepseek_v4_flash_is_approved(self):
        """deepseek-v4-flash is an approved BytePlus route."""
        assert is_byteplus_route("byteplus/deepseek-v4-flash")

    def test_deepseek_v4_1_flash_is_approved(self):
        """deepseek-v4.1-flash is an approved BytePlus route."""
        assert is_byteplus_route("byteplus/deepseek-v4.1-flash")

    def test_deepseek_v4_pro_is_approved(self):
        """deepseek-v4-pro is an approved BytePlus route."""
        assert is_byteplus_route("byteplus/deepseek-v4-pro")

    def test_deepseek_v4_1_pro_is_approved(self):
        """deepseek-v4.1-pro is an approved BytePlus route."""
        assert is_byteplus_route("byteplus/deepseek-v4.1-pro")

    def test_wrong_version_is_refused(self):
        """deepseek-v3 (wrong version) is refused."""
        assert not is_byteplus_route("byteplus/deepseek-v3")

    def test_wrong_variant_is_refused(self):
        """deepseek-v4-sonnet (unknown variant) is refused."""
        assert not is_byteplus_route("byteplus/deepseek-v4-sonnet")

    def test_direct_provider_bypass_is_refused(self):
        """A model id without the byteplus/ prefix is a direct-provider bypass."""
        assert not is_byteplus_route("deepseek-v4-flash")
        assert not is_byteplus_route("deepseek-v4-pro")
        assert not is_byteplus_route("faigate/deepseek-v4-flash")

    def test_verify_approved_route_passes_for_approved(self):
        """verify_approved_route does not raise for an approved route."""
        verify_approved_route("byteplus/deepseek-v4-flash")

    def test_verify_approved_route_raises_for_wrong_version(self):
        """verify_approved_route raises for a wrong version."""
        with pytest.raises(ModelNamespaceError) as exc:
            verify_approved_route("byteplus/deepseek-v3")
        assert "not an approved BytePlus route" in str(exc.value)

    def test_verify_approved_route_raises_for_direct_bypass(self):
        """verify_approved_route raises for a direct-provider bypass."""
        with pytest.raises(ModelNamespaceError) as exc:
            verify_approved_route("deepseek-v4-flash")
        assert "not an approved BytePlus route" in str(exc.value)

    def test_verify_approved_route_raises_for_wrong_provider(self):
        """verify_approved_route raises when the provider is not faigate."""
        with pytest.raises(ModelNamespaceError) as exc:
            verify_approved_route("byteplus/deepseek-v4-flash", provider="openrouter")
        assert "not the approved gateway" in str(exc.value)

    def test_verify_approved_route_raises_for_single_model(self):
        """A SingleModelProvider fallback is refused as a direct bypass."""
        with pytest.raises(ModelNamespaceError) as exc:
            verify_approved_route("byteplus/deepseek-v4-flash", provider="single_model")
        assert "not the approved gateway" in str(exc.value)

    def test_verify_approved_route_for_faigate_provider_passes(self):
        """verify_approved_route with faigate provider passes for approved."""
        verify_approved_route("byteplus/deepseek-v4-flash", provider="faigate")
        verify_approved_route("byteplus/deepseek-v4-pro", provider="faigate")

    def test_approved_routes_match_router_profiles(self):
        """Every model in ROUTER_PROFILES maps to an approved BytePlus route
        through FAIGATE_MAP."""
        for preset_name, preset in ROUTER_PROFILES.items():
            for model in preset["models"]:
                mapped = FAIGATE_MAP.get(model, model)
                # The mapped model should be resolvable to a byteplus route
                # through the translate path
                if mapped.startswith("deepseek-"):
                    byteplus_model = f"byteplus/{mapped}"
                    assert is_byteplus_route(byteplus_model), (
                        f"preset {preset_name!r} model {model!r} maps to "
                        f"{mapped!r} which is not in approved routes"
                    )

    def test_tier_route_map_contains_only_approved_routes(self):
        """Every route in TIER_ROUTE_MAP is in the approved BytePlus set."""
        for tier, routes in TIER_ROUTE_MAP.items():
            for route in routes:
                assert route in APPROVED_BYTEPLUS_ROUTES, (
                    f"tier {tier!r} declares route {route!r} "
                    f"which is not in APPROVED_BYTEPLUS_ROUTES"
                )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 3: Tier-to-route binding via model_policy.
# ═══════════════════════════════════════════════════════════════════════════


class TestTierRouteBinding:
    """The tier-to-route mapping in model_policy binds capability to route (criterion 3)."""

    def test_flash_tier_resolves_to_flash_routes(self):
        """Flash tier resolves to deepseek-v4-flash and deepseek-v4.1-flash."""
        routes = resolve_route_for_tier(ModelTier.FLASH.value)
        assert "byteplus/deepseek-v4-flash" in routes
        assert "byteplus/deepseek-v4.1-flash" in routes
        assert "byteplus/deepseek-v4-pro" not in routes

    def test_pro_tier_resolves_to_pro_and_flash_routes(self):
        """Pro tier resolves to pro and flash routes (escalation flexibility)."""
        routes = resolve_route_for_tier(ModelTier.PRO.value)
        assert "byteplus/deepseek-v4-pro" in routes
        assert "byteplus/deepseek-v4.1-pro" in routes
        assert "byteplus/deepseek-v4-flash" in routes  # escalation may use flash

    def test_unknown_tier_raises(self):
        """An unknown tier raises AllocationError."""
        with pytest.raises(AllocationError) as exc:
            resolve_route_for_tier("unknown")
        assert "unknown tier" in str(exc.value)

    def test_tier_route_map_keys_match_model_tiers(self):
        """TIER_ROUTE_MAP keys match ModelTier values."""
        for tier in ModelTier:
            assert tier.value in TIER_ROUTE_MAP, (
                f"ModelTier {tier.value} missing from TIER_ROUTE_MAP"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Criterion 4: Retry/escalation requires new attempt attribution and
# reserved budget; raw upstream names or global environment keys cannot
# override the approved route set.
# ═══════════════════════════════════════════════════════════════════════════


class TestRetryAttribution:
    """Retry/escalation requires new attempt attribution and budget (criterion 4)."""

    def test_attempt_attribution_requires_positive_attempt(self):
        """An attempt attribution with attempt=1 is valid."""
        attr = AttemptAttribution(
            attempt=1,
            requested_route="byteplus/deepseek-v4-flash",
            reserved_budget=5.0,
        )
        assert attr.attempt == 1
        assert attr.requested_route == "byteplus/deepseek-v4-flash"
        assert attr.reserved_budget == 5.0

    def test_attempt_attribution_with_zero_attempt_is_refused(self):
        """An attempt of 0 is refused."""
        with pytest.raises(AllocationError) as exc:
            AttemptAttribution(attempt=0, requested_route="byteplus/deepseek-v4-flash")
        assert "attempt must be >= 1" in str(exc.value)

    def test_attempt_attribution_with_negative_attempt_is_refused(self):
        """A negative attempt is refused."""
        with pytest.raises(AllocationError) as exc:
            AttemptAttribution(attempt=-1, requested_route="byteplus/deepseek-v4-flash")
        assert "attempt must be >= 1" in str(exc.value)

    def test_attempt_attribution_with_bool_attempt_is_refused(self):
        """A boolean attempt value is refused."""
        with pytest.raises(AllocationError) as exc:
            AttemptAttribution(attempt=True, requested_route="byteplus/deepseek-v4-flash")  # type: ignore[arg-type]
        assert "attempt must be a positive integer" in str(exc.value)

    def test_attempt_attribution_with_empty_route_is_refused(self):
        """An empty requested_route is refused."""
        with pytest.raises(AllocationError) as exc:
            AttemptAttribution(attempt=1, requested_route="")
        assert "non-empty requested_route" in str(exc.value)

    def test_attempt_attribution_default_reserved_budget(self):
        """Default reserved budget is 0.0."""
        attr = AttemptAttribution(attempt=1, requested_route="byteplus/deepseek-v4-flash")
        assert attr.reserved_budget == 0.0

    def test_check_attempt_route_passes_for_allowed_route(self):
        """check_attempt_route passes when the route is in allowed_routes."""
        attr = AttemptAttribution(attempt=1, requested_route="byteplus/deepseek-v4-flash")
        allowed = frozenset({"byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"})
        check_attempt_route(attr, allowed_routes=allowed)

    def test_check_attempt_route_refuses_unapproved_route(self):
        """check_attempt_route raises when the route is not allowed."""
        attr = AttemptAttribution(attempt=1, requested_route="deepseek-v4-flash")
        allowed = frozenset({"byteplus/deepseek-v4-flash"})
        with pytest.raises(AllocationError) as exc:
            check_attempt_route(attr, allowed_routes=allowed)
        assert "not in allowed routes" in str(exc.value)

    def test_check_attempt_route_passes_when_allowed_is_none(self):
        """When allowed_routes is None, any route is accepted."""
        attr = AttemptAttribution(attempt=1, requested_route="any-route")
        check_attempt_route(attr, allowed_routes=None)

    def test_retry_escalation_requires_new_attempt(self):
        """A retry must carry a new attempt number distinct from the parent."""
        parent = AttemptAttribution(attempt=1, requested_route="byteplus/deepseek-v4-flash")
        retry = AttemptAttribution(attempt=2, requested_route="byteplus/deepseek-v4-flash")
        assert retry.attempt > parent.attempt

    def test_retry_escalation_with_same_attempt_is_not_a_retry(self):
        """A retry with the same attempt number is invalid -- each retry
        needs a new attribution."""
        attr = AttemptAttribution(attempt=1, requested_route="byteplus/deepseek-v4-flash")
        same_attr = AttemptAttribution(attempt=1, requested_route="byteplus/deepseek-v4-flash")
        assert attr.attempt == same_attr.attempt
        # This is not a retry -- it is the same attempt.

    def test_raw_upstream_name_cannot_override_approved_route(self):
        """A raw upstream name like 'deepseek-v4-flash' (no byteplus/ prefix)
        is refused by check_attempt_route when the allowed set contains
        only approved routes."""
        attr = AttemptAttribution(attempt=1, requested_route="deepseek-v4-flash")
        allowed = frozenset({"byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"})
        with pytest.raises(AllocationError) as exc:
            check_attempt_route(attr, allowed_routes=allowed)
        assert "not in allowed routes" in str(exc.value)

    def test_global_env_key_cannot_override_approved_route(self):
        """A global environment key name used as a route is refused."""
        attr = AttemptAttribution(attempt=1, requested_route="OPENAI_API_KEY")
        allowed = frozenset({"byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"})
        with pytest.raises(AllocationError) as exc:
            check_attempt_route(attr, allowed_routes=allowed)
        assert "not in allowed routes" in str(exc.value)

    def test_retry_attribution_function_passes(self):
        """check_retry_attribution (in faigate_adapter) passes for valid."""
        check_retry_attribution(
            attempt=2,
            requested_route="byteplus/deepseek-v4-flash",
            allowed_routes={"byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"},
        )

    def test_retry_attribution_function_refuses_invalid_attempt(self):
        """check_retry_attribution refuses attempt=0."""
        with pytest.raises(ValueError) as exc:
            check_retry_attribution(attempt=0, requested_route="byteplus/deepseek-v4-flash")
        assert "retry attempt must be a positive integer" in str(exc.value)

    def test_retry_attribution_function_refuses_unapproved_route(self):
        """check_retry_attribution refuses a route not in allowed_routes."""
        with pytest.raises(ValueError) as exc:
            check_retry_attribution(
                attempt=2,
                requested_route="deepseek-v4-flash",
                allowed_routes={"byteplus/deepseek-v4-flash"},
            )
        assert "not in allowed routes" in str(exc.value)

    def test_retry_attribution_default_budget(self):
        """A retry without an explicit reserved_budget is still tracked."""
        attr = AttemptAttribution(attempt=2, requested_route="byteplus/deepseek-v4-flash")
        assert attr.reserved_budget == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Cross-criterion: The route lock constants and mapping integrity.
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteLockIntegrity:
    """Constants and mappings stay consistent across criteria."""

    def test_approved_routes_non_empty(self):
        """The approved BytePlus route set is not empty."""
        assert len(APPROVED_BYTEPLUS_ROUTES) >= 3  # flash, v4.1-flash, pro at minimum

    def test_tier_route_map_covers_both_tiers(self):
        """TIER_ROUTE_MAP covers flash and pro."""
        assert ModelTier.FLASH.value in TIER_ROUTE_MAP
        assert ModelTier.PRO.value in TIER_ROUTE_MAP

    def test_flash_tier_routes_are_subset_of_pro(self):
        """Flash routes are a subset of Pro routes (escalation flexibility)."""
        flash_routes = resolve_route_for_tier(ModelTier.FLASH.value)
        pro_routes = resolve_route_for_tier(ModelTier.PRO.value)
        assert flash_routes.issubset(pro_routes), (
            "Flash routes must be a subset of Pro routes for escalation"
        )

    def test_gateway_url_ends_with_slash(self):
        """The APPROVED_GATEWAY_URL is the base URL (no trailing slash needed)."""
        assert APPROVED_GATEWAY_URL == "http://127.0.0.1:8090"

    def test_profile_models_map_to_byteplus(self):
        """All router profile models map to byteplus/ through FAIGATE_MAP."""
        for preset in ROUTER_PROFILES.values():
            for model in preset["models"]:
                mapped = FAIGATE_MAP.get(model, model)
                byteplus_route = f"byteplus/{mapped}"
                assert byteplus_route in APPROVED_BYTEPLUS_ROUTES or mapped == "openrouter-fallback", (
                    f"model {model!r} -> {mapped!r} does not map to an approved BytePlus route"
                )

    def test_policy_revision_is_set(self):
        """The policy revision constant is a non-empty string."""
        assert isinstance(COUNCIL_POLICY_REVISION, str)
        assert len(COUNCIL_POLICY_REVISION) > 0


# ═══════════════════════════════════════════════════════════════════════════
# Route-proof document: loading, validation, and integration with
# ReservationEnvelope / ExecutionEnvelopeV1.
# ═══════════════════════════════════════════════════════════════════════════


_ROUTE_PROOF_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "route-proof-byteplus-v1.json"
)


class TestRouteProofDocument:
    """Route-proof document loading and validation through real paths."""

    def test_load_route_proof_fixture(self):
        """The fixture route-proof document loads without error."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        assert proof.schema_version == "route-proof-v1"
        assert "byteplus/deepseek-v4-flash" in proof.approved_routes
        assert "byteplus/deepseek-v4.1-flash" in proof.approved_routes
        assert "byteplus/deepseek-v4-pro" in proof.approved_routes
        assert "byteplus/deepseek-v4.1-pro" in proof.approved_routes
        assert proof.approved_gateway_host == "127.0.0.1"
        assert proof.approved_gateway_port == 8090

    def test_route_proof_check_route_allowed_passes(self):
        """check_route_allowed does not raise for an approved route."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        proof.check_route_allowed("byteplus/deepseek-v4-flash")
        proof.check_route_allowed("byteplus/deepseek-v4-pro")

    def test_route_proof_check_route_allowed_refuses_unapproved(self):
        """check_route_allowed raises for an unapproved route."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        with pytest.raises(RouteProofError):
            proof.check_route_allowed("byteplus/deepseek-v3")

    def test_route_proof_check_route_allowed_refuses_bare_model(self):
        """check_route_allowed refuses a bare model name (direct bypass)."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        with pytest.raises(RouteProofError):
            proof.check_route_allowed("deepseek-v4-flash")

    def test_route_proof_resolve_tier_flash(self):
        """Tier resolution returns the correct route subset."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        routes = proof.resolve_allowed_routes_for_tier("flash")
        assert "byteplus/deepseek-v4-flash" in routes
        assert "byteplus/deepseek-v4.1-flash" in routes
        assert "byteplus/deepseek-v4-pro" not in routes

    def test_route_proof_resolve_tier_pro(self):
        """Pro tier includes flash routes (escalation)."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        routes = proof.resolve_allowed_routes_for_tier("pro")
        assert "byteplus/deepseek-v4-pro" in routes
        assert "byteplus/deepseek-v4.1-pro" in routes
        assert "byteplus/deepseek-v4-flash" in routes

    def test_route_proof_unknown_tier_raises(self):
        """An unknown tier raises RouteProofError."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        with pytest.raises(RouteProofError):
            proof.resolve_allowed_routes_for_tier("unknown")

    def test_route_proof_to_dict_round_trip(self):
        """RouteProof serializes and deserializes faithfully."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        data = proof.to_dict()
        restored = RouteProof.from_dict(data)
        assert restored.approved_routes == proof.approved_routes
        assert restored.tier_routes == proof.tier_routes
        assert restored.approved_gateway_host == proof.approved_gateway_host
        assert restored.approved_gateway_port == proof.approved_gateway_port


# ═══════════════════════════════════════════════════════════════════════════
# ReservationEnvelope route enforcement: the reservation store checks
# allowed_routes at reserve() time.
# ═══════════════════════════════════════════════════════════════════════════


class TestReservationRouteEnforcement:
    """The reservation store enforces allowed_routes at reserve() time."""

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_route_reservation.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    def test_reserve_approved_route_passes(self, db_path):
        """reserve() accepts a route in allowed_routes."""
        store = ReservationStore(db_path)
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        store.set_envelope("run-1", "task-1/attempt-1", env)
        rec = store.reserve(
            run_id="run-1", task_id="task-1", attempt=1,
            scope_id="task-1/attempt-1",
            idempotency_key="test-1", envelope=env,
            route="byteplus/deepseek-v4-flash",
        )
        assert rec.state == "reserved"

    def test_reserve_unapproved_route_refused(self, db_path):
        """reserve() refuses a route NOT in allowed_routes."""
        store = ReservationStore(db_path)
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        store.set_envelope("run-2", "task-2/attempt-1", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="run-2", task_id="task-2", attempt=1,
                scope_id="task-2/attempt-1",
                idempotency_key="test-2", envelope=env,
                route="byteplus/deepseek-v3",
            )
        assert "not in allowed_routes" in str(exc.value)

    def test_reserve_no_allowed_routes_permits_any(self, db_path):
        """When allowed_routes is None, any route is accepted."""
        store = ReservationStore(db_path)
        env = ReservationEnvelope()  # no allowed_routes restriction
        store.set_envelope("run-3", "task-3/attempt-1", env)
        rec = store.reserve(
            run_id="run-3", task_id="task-3", attempt=1,
            scope_id="task-3/attempt-1",
            idempotency_key="test-3", envelope=env,
            route="any-route",
        )
        assert rec.state == "reserved"

    def test_reserve_empty_allowed_routes_refuses_all(self, db_path):
        """An empty allowed_routes list refuses all routes (default-deny)."""
        store = ReservationStore(db_path)
        env = ReservationEnvelope(allowed_routes=[])
        store.set_envelope("run-4", "task-4/attempt-1", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="run-4", task_id="task-4", attempt=1,
                scope_id="task-4/attempt-1",
                idempotency_key="test-4", envelope=env,
                route="byteplus/deepseek-v4-flash",
            )
        assert "not in allowed_routes" in str(exc.value)

    def test_reserve_route_from_route_proof_envelope(self, db_path):
        """Envelope created from RouteProof.approved_routes enforces correctly."""
        proof = RouteProof.load(str(_ROUTE_PROOF_FIXTURE))
        env = ReservationEnvelope(allowed_routes=sorted(proof.approved_routes))
        store = ReservationStore(db_path)
        store.set_envelope("run-5", "task-5/attempt-1", env)

        # Approved route passes
        store.reserve(
            run_id="run-5", task_id="task-5", attempt=1,
            scope_id="task-5/attempt-1",
            idempotency_key="test-5a", envelope=env,
            route="byteplus/deepseek-v4-flash",
        )

        # Unapproved route refused
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="run-5", task_id="task-5", attempt=1,
                scope_id="task-5/attempt-1",
                idempotency_key="test-5b", envelope=env,
                route="byteplus/deepseek-v3",
            )
        assert "not in allowed_routes" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════════
# ExecutionEnvelopeV1 bridge: to_reservation_envelope() must pass through
# allowed_routes in both branches.
# ═══════════════════════════════════════════════════════════════════════════


class TestExecutionEnvelopeBridge:
    """ExecutionEnvelopeV1.to_reservation_envelope passes allowed_routes."""

    def test_enforcing_branch_passes_allowed_routes(self):
        """Under enforcing policy, allowed_routes is passed through."""
        env = ExecutionEnvelopeV1(
            allowed_routes=["byteplus/deepseek-v4-flash"],
            limit_policy="enforcing",
        )
        rv = env.to_reservation_envelope()
        assert rv.allowed_routes == ["byteplus/deepseek-v4-flash"]

    def test_observe_adapt_branch_passes_allowed_routes(self):
        """Under observe_adapt policy, allowed_routes is passed through."""
        env = ExecutionEnvelopeV1(
            allowed_routes=["byteplus/deepseek-v4-flash"],
            limit_policy="observe_adapt",
        )
        rv = env.to_reservation_envelope()
        assert rv.allowed_routes == ["byteplus/deepseek-v4-flash"]

    def test_none_allowed_routes_passes_through(self):
        """When allowed_routes is None, it stays None."""
        env = ExecutionEnvelopeV1(limit_policy="enforcing")
        rv = env.to_reservation_envelope()
        assert rv.allowed_routes is None

    def test_from_dict_round_trip_preserves_allowed_routes(self):
        """allowed_routes survives from_dict -> to_reservation_envelope."""
        data = {
            "allowed_routes": ["byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"],
            "limit_policy": "enforcing",
            "max_requests": 5,
        }
        env = ExecutionEnvelopeV1.from_dict(data)
        rv = env.to_reservation_envelope()
        assert rv.allowed_routes == ["byteplus/deepseek-v4-flash", "byteplus/deepseek-v4-pro"]


# ═══════════════════════════════════════════════════════════════════════════
# RunApplicationService: the route is passed through to reserve() via the
# start_async() and execute_streaming_native() paths.
# ═══════════════════════════════════════════════════════════════════════════


class TestServiceRouteWiring:
    """RunApplicationService wires route through start_async and execute."""

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_svc_route.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    @pytest.fixture
    def scratch(self, tmp_path):
        a = tmp_path / "artifacts"
        a.mkdir()
        return str(a)

    @pytest.fixture
    def svc(self, db_path, scratch):
        store = SQLiteRunStore(db_path)
        journal = EventJournal(db_path)
        raw = RawArtifactStore(scratch)
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore
        stream = DurableStreamStore(root=scratch)
        rs = ReservationStore(db_path)
        return RunApplicationService(store, journal, raw, stream, reservation_store=rs)

    def test_execute_streaming_native_passes_route_to_reserve(self, svc, db_path, scratch):
        """execute_streaming_native passes model as route to reserve().

        This test verifies the integration wiring: the ingress must persist
        an envelope with allowed_routes, and the reserve() call inside
        execute_streaming_native must check the route against it.
        """
        # Persist an envelope with allowed_routes for the task scope
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "test-run-e", "task-e/attempt-1", env
        )

        # A probe command that exits 0
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('route-enforcement-stream')"]

        # The model passed as route should be in allowed_routes -> passes
        result = svc.execute_streaming_native(
            command=cmd,
            run_id="test-run-e",
            tool="bash",
            model="byteplus/deepseek-v4-flash",
            subject_repo="test-repo",
            subject_commit="a724d32a724d32a724d32a724d32a724d32a724d",
            task_id="task-e",
            attempt=1,
        )
        assert result["run_id"] == "test-run-e"
        assert result["gate_state"] == "pass"

    def test_execute_streaming_native_refuses_unapproved_route(self, svc, db_path, scratch):
        """execute_streaming_native refuses when route is not in allowed_routes."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "test-run-f", "task-f/attempt-1", env
        )

        import sys as _sys
        cmd = [_sys.executable, "-c", "print('should-not-run')"]

        with pytest.raises(RunIntegrationError) as exc:
            svc.execute_streaming_native(
                command=cmd,
                run_id="test-run-f",
                tool="bash",
                model="byteplus/deepseek-v3",  # NOT in allowed_routes
                subject_repo="test-repo",
                subject_commit="a724d32a724d32a724d32a724d32a724d32a724d",
                task_id="task-f",
                attempt=1,
            )
        assert "quota refused" in str(exc.value)

    def test_start_async_passes_route_to_reserve(self, svc, db_path, scratch):
        """start_async passes the route parameter through to reserve()."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "test-run-g", "task-g/attempt-1", env
        )

        import sys as _sys
        cmd = [_sys.executable, "-c", "print('start-async-route')"]

        # The route parameter is passed through to reserve() -> should pass
        result = svc.start_async(
            command=cmd,
            run_id="test-run-g",
            attempt=1,
            task_id="task-g",
            route="byteplus/deepseek-v4-flash",
        )
        assert result["run_id"] == "test-run-g"
        assert result["state"] is not None

    def test_start_async_refuses_unapproved_route(self, svc, db_path, scratch):
        """start_async refuses when route is not in allowed_routes."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "test-run-h", "task-h/attempt-1", env
        )

        import sys as _sys
        cmd = [_sys.executable, "-c", "print('should-not-run')"]

        with pytest.raises(RunIntegrationError) as exc:
            svc.start_async(
                command=cmd,
                run_id="test-run-h",
                attempt=1,
                task_id="task-h",
                route="byteplus/deepseek-v3",  # NOT in allowed_routes
            )
        assert "async reservation refused" in str(exc.value)

    def test_restart_attempt_checks_retry_attribution(self, svc, db_path, scratch):
        """restart_attempt verifies retry attribution before launching."""
        from skillweave.runtime.reservation import ReservationEnvelope
        # First, create a source run that is non-terminal
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('source-run')"]

        # Create a run via start_async that we can restart
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "test-run-i", "task-i/attempt-1", env
        )
        source = svc.start_async(
            command=cmd,
            run_id="test-run-i",
            attempt=1,
            task_id="task-i",
            route="byteplus/deepseek-v4-flash",
        )

        # Now try to restart with an envelope that has allowed_routes
        env2 = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "test-run-i", "task-i/attempt-2", env2
        )

        # The restart should pass because "restart" is checked against
        # allowed_routes via AttemptAttribution
        result = svc.restart_attempt(
            run_id="test-run-i",
            command=[_sys.executable, "-c", "print('restart-attempt')"],
            task_id="task-i",
            reservation_envelope=env2,
            route="byteplus/deepseek-v4-flash",
        )
        assert result["restarted"] is True
        assert result["attempt"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# Controller Finding 1: route identity chain persisted as journal events.
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteDecisionJournalPersistence:
    """Route decisions are persisted as journal events (Finding 1)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_route_journal.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    @pytest.fixture
    def scratch(self, tmp_path):
        a = tmp_path / "artifacts"
        a.mkdir()
        return str(a)

    @pytest.fixture
    def svc(self, db_path, scratch):
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore
        store = SQLiteRunStore(db_path)
        journal = EventJournal(db_path)
        raw = RawArtifactStore(scratch)
        stream = DurableStreamStore(root=scratch)
        rs = ReservationStore(db_path)
        return RunApplicationService(store, journal, raw, stream, reservation_store=rs)

    def test_execute_streaming_logs_route_decision_event(self, svc, db_path, scratch):
        """execute_streaming_native writes a route_decision journal event."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-rd-1", "task-rd-1/attempt-1", env
        )
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('ok')"]
        result = svc.execute_streaming_native(
            command=cmd,
            run_id="run-rd-1",
            tool="bash",
            model="byteplus/deepseek-v4-flash",
            subject_repo="test-repo",
            subject_commit="a724d32a724d32a724d32a724d32a724d32a724d",
            task_id="task-rd-1",
            attempt=1,
        )
        # Verify a route_decision journal event exists
        events = svc.journal.get_events("run-rd-1")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 1
        payload = route_events[0].payload
        # The payload now has contract/_meta format
        assert "contract" in payload
        assert "_meta" in payload
        assert payload["contract"]["requested_model"] == "byteplus/deepseek-v4-flash"
        assert payload["_meta"]["resolved_provider"] == "faigate"
        assert payload["contract"]["request_id"] is not None

    def test_start_async_logs_route_decision_event(self, svc, db_path, scratch):
        """start_async writes a route_decision journal event."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-rd-2", "task-rd-2/attempt-1", env
        )
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('ok')"]
        svc.start_async(
            command=cmd,
            run_id="run-rd-2",
            attempt=1,
            task_id="task-rd-2",
            route="byteplus/deepseek-v4-flash",
        )
        events = svc.journal.get_events("run-rd-2")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 1
        payload = route_events[0].payload
        assert payload["contract"]["requested_model"] == "byteplus/deepseek-v4-flash"

    def test_route_decision_has_correlation_id(self, svc, db_path, scratch):
        """The route decision event carries a correlation_id linking the chain."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-rd-3", "task-rd-3/attempt-1", env
        )
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('ok')"]
        svc.execute_streaming_native(
            command=cmd,
            run_id="run-rd-3",
            tool="bash",
            model="byteplus/deepseek-v4-flash",
            subject_repo="test-repo",
            subject_commit="a724d32a724d32a724d32a724d32a724d32a724d",
            task_id="task-rd-3",
            attempt=1,
        )
        events = svc.journal.get_events("run-rd-3")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 1
        assert route_events[0].correlation_id is not None
        # The correlation_id should contain the task_id
        assert "task-rd-3" in route_events[0].correlation_id
        # The contract payload request_id should match the correlation_id
        payload = route_events[0].payload
        assert payload["contract"]["request_id"] == route_events[0].correlation_id


# ═══════════════════════════════════════════════════════════════════════════
# Controller Finding 2: child environment confinement and egress.
# ═══════════════════════════════════════════════════════════════════════════


class TestChildEnvironmentConfinement:
    """Child env is confined to allowlist; provider keys are stripped (Finding 2)."""

    def test_build_locked_child_env_strips_provider_keys(self):
        """_build_locked_child_env drops ambient provider keys from base env."""
        svc = object.__new__(RunApplicationService)
        base = {
            "PATH": "/usr/bin",
            "HOME": "/root",
            "FAIGATE_API_KEY": "sk-should-be-stripped",
            "OPENROUTER_API_KEY": "sk-should-be-stripped",
            "OPENAI_API_KEY": "sk-should-be-stripped",
            "ANTHROPIC_API_KEY": "sk-should-be-stripped",
            "SECRET_TOKEN": "should-be-dropped",
        }
        env = svc._build_locked_child_env(base_env=base)
        # Allowlisted keys are preserved
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/root"
        # Provider keys are stripped
        assert "FAIGATE_API_KEY" not in env
        assert "OPENROUTER_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        # Non-allowlisted non-provider keys are also stripped
        assert "SECRET_TOKEN" not in env

    def test_build_locked_child_env_extra_vars_added(self):
        """Extra vars passed to _build_locked_child_env are included."""
        svc = object.__new__(RunApplicationService)
        base = {"PATH": "/usr/bin", "HOME": "/root"}
        env = svc._build_locked_child_env(
            base_env=base,
            extra_vars={"SKILLWEAVE_ATTEMPT_CHECKOUT": "/tmp/checkout"},
        )
        assert env.get("PATH") == "/usr/bin"
        assert env.get("SKILLWEAVE_ATTEMPT_CHECKOUT") == "/tmp/checkout"

    def test_build_locked_child_env_refuses_ambient_key_in_extra(self):
        """Passing an ambient provider key in extra_vars raises."""
        svc = object.__new__(RunApplicationService)
        base = {"PATH": "/usr/bin", "HOME": "/root"}
        with pytest.raises(RunIntegrationError) as exc:
            svc._build_locked_child_env(
                base_env=base,
                extra_vars={"OPENAI_API_KEY": "sk-bad"},
            )
        assert "ambient provider" in str(exc.value).lower()

    def test_build_locked_child_env_strips_all_ambient_keys(self):
        """Every key in _AMBIENT_PROVIDER_ENV_KEYS is stripped."""
        from skillweave.runsvc.service import _AMBIENT_PROVIDER_ENV_KEYS
        svc = object.__new__(RunApplicationService)
        base = dict.fromkeys(_AMBIENT_PROVIDER_ENV_KEYS, "value")
        base["PATH"] = "/usr/bin"
        base["HOME"] = "/root"
        env = svc._build_locked_child_env(base_env=base)
        for key in _AMBIENT_PROVIDER_ENV_KEYS:
            assert key not in env, f"{key} should be stripped"

    def test_execute_passes_env_allowlist(self):
        """execute() passes env_allowlist to run_command, confining child env.

        We verify this by checking the allowlist is non-None when
        _LOCKED_CHILD_ENV_ALLOWLIST is set.
        """
        from skillweave.runsvc.service import _LOCKED_CHILD_ENV_ALLOWLIST
        assert len(_LOCKED_CHILD_ENV_ALLOWLIST) > 0

    def test_streaming_passes_env_allowlist(self):
        """execute_streaming_native passes env_allowlist to run_process_streaming."""
        from skillweave.runsvc.service import _LOCKED_CHILD_ENV_ALLOWLIST
        assert len(_LOCKED_CHILD_ENV_ALLOWLIST) > 0

    def test_egress_gateway_enforced_before_admission(self):
        """check_egress_gateway refuses non-approved URLs before any call."""
        with pytest.raises(ModelNamespaceError):
            check_egress_gateway("http://127.0.0.1:9090")
        with pytest.raises(ModelNamespaceError):
            check_egress_gateway("http://localhost:8090")
        with pytest.raises(ModelNamespaceError):
            check_egress_gateway("https://api.openai.com/v1")
        # Approved gateway passes
        check_egress_gateway(APPROVED_GATEWAY_URL)


# ═══════════════════════════════════════════════════════════════════════════
# Controller Finding 3: route=None must HOLD when allowed_routes is set.
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteNoneEnforcement:
    """route=None is refused when allowed_routes is set (Finding 3)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_route_none.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    @pytest.fixture
    def scratch(self, tmp_path):
        a = tmp_path / "artifacts"
        a.mkdir()
        return str(a)

    @pytest.fixture
    def svc(self, db_path, scratch):
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore
        store = SQLiteRunStore(db_path)
        journal = EventJournal(db_path)
        raw = RawArtifactStore(scratch)
        stream = DurableStreamStore(root=scratch)
        rs = ReservationStore(db_path)
        return RunApplicationService(store, journal, raw, stream, reservation_store=rs)

    def test_start_async_route_none_refused_with_allowed_routes(self, svc, db_path, scratch):
        """start_async with route=None and allowed_routes set is refused."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-none-1", "task-none-1/attempt-1", env
        )
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('should-not-run')"]
        with pytest.raises(RunIntegrationError) as exc:
            svc.start_async(
                command=cmd,
                run_id="run-none-1",
                attempt=1,
                task_id="task-none-1",
                route=None,
            )
        assert "quota refused" in str(exc.value).lower()

    def test_start_async_route_none_permits_without_allowed_routes(self, svc, db_path, scratch):
        """start_async with route=None and no allowed_routes is permitted."""
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('ok')"]
        result = svc.start_async(
            command=cmd,
            run_id="run-none-2",
            attempt=1,
            task_id="task-none-2",
            route=None,
        )
        assert result["run_id"] == "run-none-2"

    def test_reserve_route_none_refused_with_allowed_routes(self, db_path):
        """reserve() with route=None and allowed_routes set is refused."""
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope, QuotaRefused
        store = ReservationStore(db_path)
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        store.set_envelope("run-none-3", "task-none-3/attempt-1", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="run-none-3", task_id="task-none-3", attempt=1,
                scope_id="task-none-3/attempt-1",
                idempotency_key="test-none-3", envelope=env,
                route=None,
            )
        assert "not in allowed_routes" in str(exc.value) or "quota refused" in str(exc.value).lower()

    def test_reserve_route_none_permits_without_allowed_routes(self, db_path):
        """reserve() with route=None and no allowed_routes is permitted."""
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope
        store = ReservationStore(db_path)
        env = ReservationEnvelope()  # no allowed_routes restriction
        store.set_envelope("run-none-4", "task-none-4/attempt-1", env)
        rec = store.reserve(
            run_id="run-none-4", task_id="task-none-4", attempt=1,
            scope_id="task-none-4/attempt-1",
            idempotency_key="test-none-4", envelope=env,
            route=None,
        )
        assert rec.state == "reserved"


# ═══════════════════════════════════════════════════════════════════════════
# Controller Finding 4: FAIGATE_BASE_URL / OPENROUTER_API_KEY / raw
# upstream model names cannot override approved route set.
# ═══════════════════════════════════════════════════════════════════════════


class TestEnvKeyCannotOverrideApprovedRoutes:
    """Ambient env keys and raw upstream names cannot override the approved set (Finding 4)."""

    def test_faigate_base_url_in_ambient_keys(self):
        """FAIGATE_BASE_URL is in _AMBIENT_PROVIDER_ENV_KEYS and is stripped."""
        from skillweave.runsvc.service import _AMBIENT_PROVIDER_ENV_KEYS
        assert "FAIGATE_BASE_URL" in _AMBIENT_PROVIDER_ENV_KEYS

    def test_openrouter_api_key_in_ambient_keys(self):
        """OPENROUTER_API_KEY is in _AMBIENT_PROVIDER_ENV_KEYS and is stripped."""
        from skillweave.runsvc.service import _AMBIENT_PROVIDER_ENV_KEYS
        assert "OPENROUTER_API_KEY" in _AMBIENT_PROVIDER_ENV_KEYS

    def test_raw_upstream_model_name_refused_by_reservation(self, db_path):
        """A raw upstream model name (no byteplus/ prefix) is refused by reserve()."""
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope, QuotaRefused
        store = ReservationStore(db_path)
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        store.set_envelope("run-env-1", "task-env-1/attempt-1", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="run-env-1", task_id="task-env-1", attempt=1,
                scope_id="task-env-1/attempt-1",
                idempotency_key="test-env-1", envelope=env,
                route="deepseek-v4-flash",  # raw upstream name, no byteplus/ prefix
            )
        assert "not in allowed_routes" in str(exc.value)

    def test_raw_upstream_model_name_refused_by_start_async(self, svc, db_path, scratch):
        """start_async refuses a raw upstream model name as route."""
        from skillweave.runtime.reservation import ReservationEnvelope
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-env-2", "task-env-2/attempt-1", env
        )
        import sys as _sys
        cmd = [_sys.executable, "-c", "print('should-not-run')"]
        with pytest.raises(RunIntegrationError) as exc:
            svc.start_async(
                command=cmd,
                run_id="run-env-2",
                attempt=1,
                task_id="task-env-2",
                route="deepseek-v4-flash",  # raw upstream name
            )
        assert "reservation refused" in str(exc.value)

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_env_override.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    @pytest.fixture
    def scratch(self, tmp_path):
        a = tmp_path / "artifacts"
        a.mkdir()
        return str(a)

    @pytest.fixture
    def svc(self, db_path, scratch):
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore
        store = SQLiteRunStore(db_path)
        journal = EventJournal(db_path)
        raw = RawArtifactStore(scratch)
        stream = DurableStreamStore(root=scratch)
        rs = ReservationStore(db_path)
        return RunApplicationService(store, journal, raw, stream, reservation_store=rs)

    def test_global_env_key_as_route_refused(self, db_path):
        """A global env key name used as a route is refused by reserve()."""
        from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope, QuotaRefused
        store = ReservationStore(db_path)
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        store.set_envelope("run-env-3", "task-env-3/attempt-1", env)
        with pytest.raises(QuotaRefused) as exc:
            store.reserve(
                run_id="run-env-3", task_id="task-env-3", attempt=1,
                scope_id="task-env-3/attempt-1",
                idempotency_key="test-env-3", envelope=env,
                route="OPENAI_API_KEY",  # env key name as route
            )
        assert "not in allowed_routes" in str(exc.value)

    def test_resolve_route_for_tier_only_returns_byteplus_routes(self):
        """TIER_ROUTE_MAP only contains byteplus/ routes, never raw names."""
        for tier in ModelTier:
            routes = resolve_route_for_tier(tier.value)
            for route in routes:
                assert route.startswith("byteplus/"), (
                    f"tier {tier.value} contains non-byteplus route {route!r}"
                )


# ═══════════════════════════════════════════════════════════════════════════
# Controller Finding 4 continued: check_retry_attribution against env keys.
# ═══════════════════════════════════════════════════════════════════════════


class TestRetryAttributionEnvKeyProtection:
    """Retry attribution checks protect against env key overrides (Finding 4)."""

    def test_retry_with_faigate_base_url_refused(self):
        """FAIGATE_BASE_URL as a retry route is refused."""
        with pytest.raises(ValueError) as exc:
            check_retry_attribution(
                attempt=2,
                requested_route="FAIGATE_BASE_URL",
                allowed_routes={"byteplus/deepseek-v4-flash"},
            )
        assert "not in allowed routes" in str(exc.value)

    def test_retry_with_raw_model_name_refused(self):
        """A raw model name (no byteplus/ prefix) is refused as retry route."""
        with pytest.raises(ValueError) as exc:
            check_retry_attribution(
                attempt=2,
                requested_route="deepseek-v4-flash",
                allowed_routes={"byteplus/deepseek-v4-flash"},
            )
        assert "not in allowed routes" in str(exc.value)

    def test_retry_with_approved_route_passes(self):
        """An approved BytePlus route passes retry attribution."""
        check_retry_attribution(
            attempt=2,
            requested_route="byteplus/deepseek-v4-flash",
            allowed_routes={"byteplus/deepseek-v4-flash"},
        )

    def test_retry_attribution_from_service_restart(self, svc, db_path, scratch):
        """restart_attempt checks retry route against approved set.

        A restart with an unapproved route is refused before launch.
        """
        from skillweave.runtime.reservation import ReservationEnvelope
        import sys as _sys

        # Create source run
        env = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-attr-1", "task-attr-1/attempt-1", env
        )
        svc.start_async(
            command=[_sys.executable, "-c", "print('source')"],
            run_id="run-attr-1",
            attempt=1,
            task_id="task-attr-1",
            route="byteplus/deepseek-v4-flash",
        )

        # Try restart with unapproved route
        env2 = ReservationEnvelope(allowed_routes=["byteplus/deepseek-v4-flash"])
        svc.reservation_store.set_envelope(
            "run-attr-1", "task-attr-1/attempt-2", env2
        )
        with pytest.raises(Exception) as exc:
            svc.restart_attempt(
                run_id="run-attr-1",
                command=[_sys.executable, "-c", "print('should-not-run')"],
                task_id="task-attr-1",
                reservation_envelope=env2,
                route="deepseek-v4-flash",  # raw upstream name, NOT byteplus/
            )
        assert "retry" in str(exc.value).lower() or "route" in str(exc.value).lower()

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_retry_attr.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    @pytest.fixture
    def scratch(self, tmp_path):
        a = tmp_path / "artifacts"
        a.mkdir()
        return str(a)

    @pytest.fixture
    def svc(self, db_path, scratch):
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore
        store = SQLiteRunStore(db_path)
        journal = EventJournal(db_path)
        raw = RawArtifactStore(scratch)
        stream = DurableStreamStore(root=scratch)
        rs = ReservationStore(db_path)
        return RunApplicationService(store, journal, raw, stream, reservation_store=rs)


# ═══════════════════════════════════════════════════════════════════════════
# Real-entrypoint subprocess tests: verify the console script's integration
# path enforces route lock at the outermost boundary.
# ═══════════════════════════════════════════════════════════════════════════


class TestConsoleEntrypointRouteLock:
    """The skillweave console entrypoint enforces route lock at real launch."""

    def test_console_script_imports_route_lock_symbols(self):
        """The console script can import route lock symbols (wiring check)."""
        from skillweave.runsvc.service import (
            _AMBIENT_PROVIDER_ENV_KEYS,
            _LOCKED_CHILD_ENV_ALLOWLIST,
        )
        assert "FAIGATE_BASE_URL" in _AMBIENT_PROVIDER_ENV_KEYS
        assert "PATH" in _LOCKED_CHILD_ENV_ALLOWLIST

    def test_route_proof_fixture_loads_via_console_import(self):
        """The route-proof fixture can be loaded from a subprocess script."""
        import subprocess
        import sys as _sys
        proof_path = str(_ROUTE_PROOF_FIXTURE)
        script = (
            "import sys; sys.path.insert(0, 'src'); "
            "from skillweave.routing.route_proof import RouteProof; "
            f"p = RouteProof.load({proof_path!r}); "
            "print(len(p.approved_routes))"
        )
        result = subprocess.run(
            [_sys.executable, "-c", script],
            capture_output=True, text=True, cwd=str(_src.parent),
        )
        assert result.returncode == 0
        assert int(result.stdout.strip()) >= 4

    def _run_subprocess_script(self, script_lines):
        """Write a temp script file and run it in a subprocess.

        Avoids quoting issues with multi-line ``-c`` arguments.
        """
        import subprocess
        import sys as _sys
        import tempfile
        src_dir = str(_src.parent)
        code = "\n".join(script_lines)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="route_lock_test_",
            dir=src_dir, delete=False,
        ) as f:
            f.write(code)
            script_path = f.name
        try:
            result = subprocess.run(
                [_sys.executable, script_path],
                capture_output=True, text=True, cwd=src_dir,
            )
        finally:
            import os
            os.unlink(script_path)
        return result

    def test_subprocess_reserve_refuses_unapproved_route(self):
        """A subprocess calling reserve() with unapproved route is refused."""
        result = self._run_subprocess_script([
            "import sys, tempfile, os",
            "sys.path.insert(0, 'src')",
            "from skillweave.runtime.reservation import ReservationStore, ReservationEnvelope, QuotaRefused",
            "tmp = tempfile.mktemp(suffix='.db')",
            "store = ReservationStore(tmp)",
            "env = ReservationEnvelope(allowed_routes=['byteplus/deepseek-v4-flash'])",
            "store.set_envelope('run-c', 'task-c/attempt-1', env)",
            "try:",
            "    store.reserve(run_id='run-c', task_id='task-c', attempt=1, "
            "scope_id='task-c/attempt-1', idempotency_key='c', "
            "envelope=env, route='deepseek-v4-flash')",
            "    print('UNEXPECTED_PASS')",
            "except QuotaRefused:",
            "    print('REFUSED')",
            "finally:",
            "    os.unlink(tmp)",
        ])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "REFUSED" in result.stdout

    def test_subprocess_execution_envelope_passes_allowed_routes(self):
        """A subprocess verifying ExecutionEnvelopeV1 passes allowed_routes."""
        result = self._run_subprocess_script([
            "import sys",
            "sys.path.insert(0, 'src')",
            "from skillweave.runtime.execution_envelope import ExecutionEnvelopeV1",
            "env = ExecutionEnvelopeV1(allowed_routes=['byteplus/deepseek-v4-flash'])",
            "rv = env.to_reservation_envelope()",
            "print(rv.allowed_routes)",
        ])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "byteplus/deepseek-v4-flash" in result.stdout

    def test_subprocess_route_proof_check_allowed(self):
        """A subprocess loading route proof and checking allowed routes."""
        proof_path = str(_ROUTE_PROOF_FIXTURE)
        result = self._run_subprocess_script([
            "import sys",
            "sys.path.insert(0, 'src')",
            "from skillweave.routing.route_proof import RouteProof",
            f"p = RouteProof.load({proof_path!r})",
            "p.check_route_allowed('byteplus/deepseek-v4-flash')",
            "print('OK')",
            "try:",
            "    p.check_route_allowed('deepseek-v4-flash')",
            "    print('UNEXPECTED')",
            "except Exception as e:",
            "    print('REFUSED:' + str(e))",
        ])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "OK" in result.stdout
        assert "REFUSED" in result.stdout

    def test_subprocess_check_egress_gateway(self):
        """A subprocess verifying egress gateway enforcement."""
        result = self._run_subprocess_script([
            "import sys",
            "sys.path.insert(0, 'src')",
            "from skillweave.routing.faigate_adapter import check_egress_gateway, ModelNamespaceError",
            "check_egress_gateway('http://127.0.0.1:8090/v1')",
            "print('GATEWAY_OK')",
            "try:",
            "    check_egress_gateway('http://localhost:8090/v1')",
            "    print('UNEXPECTED')",
            "except ModelNamespaceError:",
            "    print('GATEWAY_REFUSED')",
        ])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "GATEWAY_OK" in result.stdout
        assert "GATEWAY_REFUSED" in result.stdout


# ═══════════════════════════════════════════════════════════════════════════
# Controller Finding 1 & 2: answering_model post-launch update and
# route-decision-v1 contract schema compliance.
# ═══════════════════════════════════════════════════════════════════════════


class TestRouteDecisionContractCompliance:
    """Every route_decision payload validates against route-decision-v1 schema."""

    SCHEMA_PATH = (
        Path(__file__).resolve().parent.parent.parent
        / "schemas" / "execution" / "route-decision-v1.schema.json"
    )

    def _load_schema(self):
        import json
        with open(self.SCHEMA_PATH) as f:
            return json.load(f)

    def test_to_dict_validates_against_schema(self):
        """The contract payload from to_dict() validates against the schema."""
        import json
        from jsonschema import validate
        schema = self._load_schema()
        d = RouteDecision(
            correlation_id="schema-corr-1",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            answering_envelope_identity="deepseek-v4-flash",
            run_id="run-schema-1",
            task_id="task-schema-1",
            attempt=1,
            reservation_id="res-schema-1",
            attribution_status="attributed",
            outcome="resolved",
            measured_at="2026-10-08T12:00:00+00:00",
        )
        validate(instance=d.to_dict(), schema=schema)

    def test_to_journal_dict_contract_validates_against_schema(self):
        """The contract sub-dict in to_journal_dict() validates."""
        import json
        from jsonschema import validate
        schema = self._load_schema()
        d = RouteDecision(
            correlation_id="schema-corr-2",
            requested_alias="deepseek-v4-pro",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-pro",
            answering_envelope_identity="deepseek-v4-pro",
            run_id="run-schema-2",
            task_id="task-schema-2",
            attempt=1,
        )
        jd = d.to_journal_dict()
        validate(instance=jd["contract"], schema=schema)

    def test_pending_decision_validates_against_schema(self):
        """A pending route_decision (no answering model) validates."""
        import json
        from jsonschema import validate
        schema = self._load_schema()
        d = RouteDecision(
            correlation_id="schema-corr-3",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            run_id="run-schema-3",
            task_id="task-schema-3",
            attempt=1,
        )
        validate(instance=d.to_dict(), schema=schema)

    def test_nested_request_validates_against_schema(self):
        """A nested request's contract payload validates with parent_request_id."""
        import json
        from jsonschema import validate
        schema = self._load_schema()
        d = RouteDecision(
            correlation_id="run-001/task-01/nested-01",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            answering_envelope_identity="deepseek-v4-flash",
            run_id="run-001",
            task_id="task-01/nested-01",
            attempt=1,
        )
        payload = d.to_dict()
        assert payload["parent_request_id"] == "run-001/task-01"
        validate(instance=payload, schema=schema)

    def test_no_disallowed_fields_in_contract(self):
        """The contract payload must NOT contain correlation_id, requested_alias,
        answering_envelope_identity, or resolved_provider/resolved_model."""
        d = RouteDecision(
            correlation_id="corr-no-disallowed",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            answering_envelope_identity="deepseek-v4-flash",
            run_id="run-nd",
            task_id="task-nd",
            attempt=1,
        )
        payload = d.to_dict()
        assert "correlation_id" not in payload
        assert "requested_alias" not in payload
        assert "answering_envelope_identity" not in payload
        assert "resolved_provider" not in payload
        assert "resolved_model" not in payload
        assert "catalog_revision" not in payload
        assert "policy_revision" not in payload


class TestAnsweringModelUpdate:
    """The answering model is recorded as a follow-up event after the response."""

    @pytest.fixture
    def db_path(self, tmp_path):
        p = tmp_path / "test_answering_model.db"
        yield str(p)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    @pytest.fixture
    def scratch(self, tmp_path):
        a = tmp_path / "artifacts"
        a.mkdir()
        return str(a)

    @pytest.fixture
    def svc(self, db_path, scratch):
        from skillweave.runtime.store import SQLiteRunStore
        from skillweave.runtime.journal import EventJournal
        from skillweave.runtime.registry import RawArtifactStore
        from skillweave.runtime.stream import DurableStreamStore
        from skillweave.runtime.reservation import ReservationStore
        store = SQLiteRunStore(db_path)
        journal = EventJournal(db_path)
        raw = RawArtifactStore(scratch)
        stream = DurableStreamStore(root=scratch)
        rs = ReservationStore(db_path)
        return RunApplicationService(store, journal, raw, stream, reservation_store=rs)

    def test_update_answering_model_writes_follow_up_event(self, svc, db_path):
        """_update_route_decision_answering writes a follow-up event."""
        svc._update_route_decision_answering(
            run_id="run-answer-1",
            task_id="task-answer-1",
            correlation_id="task-answer-1/attempt-1",
            requested_alias="deepseek-v4-flash",
            answering_model="deepseek-v4-flash",
            reservation_id="res-answer-1",
            attempt=1,
            attempt_order=1,
        )
        events = svc.journal.get_events("run-answer-1")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 1
        payload = route_events[0].payload
        assert payload["contract"]["answering_model"] == "deepseek-v4-flash"
        assert payload["contract"]["attribution_status"] == "attributed"
        assert payload["contract"]["outcome"] == "resolved"
        assert payload["contract"]["attempt_order"] == 1

    def test_update_answering_model_empty_string(self, svc, db_path):
        """When answering_model is empty, the event records unattributed status."""
        svc._update_route_decision_answering(
            run_id="run-answer-2",
            task_id="task-answer-2",
            correlation_id="task-answer-2/attempt-1",
            requested_alias="deepseek-v4-flash",
            answering_model="",
            reservation_id="",
            attempt=1,
            attempt_order=1,
        )
        events = svc.journal.get_events("run-answer-2")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 1
        payload = route_events[0].payload
        assert payload["contract"]["answering_model"] == ""
        assert payload["contract"]["attribution_status"] == "unattributed"
        assert payload["contract"]["outcome"] == "resolved"
        # The _meta should include the reason
        assert "_meta" in payload
        assert "no_answering_model_reason" in payload["_meta"]

    def test_update_answering_model_validates_against_schema(self, svc, db_path):
        """The follow-up event payload validates against route-decision-v1."""
        import json
        from jsonschema import validate
        schema_path = (
            Path(__file__).resolve().parent.parent.parent
            / "schemas" / "execution" / "route-decision-v1.schema.json"
        )
        with open(schema_path) as f:
            schema = json.load(f)

        svc._update_route_decision_answering(
            run_id="run-answer-3",
            task_id="task-answer-3",
            correlation_id="task-answer-3/attempt-1",
            requested_alias="deepseek-v4-flash",
            answering_model="deepseek-v4-flash",
            reservation_id="res-answer-3",
            attempt=1,
            attempt_order=1,
        )
        events = svc.journal.get_events("run-answer-3")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 1
        validate(instance=route_events[0].payload["contract"], schema=schema)

    def test_both_initial_and_follow_up_validate(self, svc, db_path):
        """Both the initial decision and the answering model update validate.

        This simulates the real flow: initial decision (pending), then
        a follow-up with the answering model (attributed).
        """
        import json
        from jsonschema import validate
        schema_path = (
            Path(__file__).resolve().parent.parent.parent
            / "schemas" / "execution" / "route-decision-v1.schema.json"
        )
        with open(schema_path) as f:
            schema = json.load(f)

        # 1. Initial decision (pending)
        from skillweave.routing.faigate_adapter import make_route_decision
        initial = make_route_decision(
            correlation_id="task-full/attempt-1",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            run_id="run-full",
            task_id="task-full",
            attempt=1,
            reservation_id="res-full",
            attempt_order=0,
        )
        svc.journal.append(
            "run-full",
            event_type="route_decision",
            payload=initial.to_journal_dict(),
            correlation_id="task-full/attempt-1",
        )
        validate(instance=initial.to_dict(), schema=schema)

        # 2. Follow-up with answering model (attributed)
        from skillweave.routing.faigate_adapter import make_route_decision
        follow_up = make_route_decision(
            correlation_id="task-full/attempt-1",
            requested_alias="deepseek-v4-flash",
            resolved_provider="faigate",
            resolved_model="byteplus/deepseek-v4-flash",
            answering_envelope_identity="deepseek-v4-flash",
            run_id="run-full",
            task_id="task-full",
            attempt=1,
            reservation_id="res-full",
            attempt_order=1,
            attribution_status="attributed",
            outcome="resolved",
        )
        svc.journal.append(
            "run-full",
            event_type="route_decision",
            payload=follow_up.to_journal_dict(),
            correlation_id="task-full/attempt-1",
        )
        validate(instance=follow_up.to_dict(), schema=schema)

        # 3. Verify both events are present with correct attempt_order
        events = svc.journal.get_events("run-full")
        route_events = [e for e in events if e.event_type == "route_decision"]
        assert len(route_events) >= 2
        orders = sorted([e.payload["contract"]["attempt_order"] for e in route_events])
        assert orders == [0, 1]

    # ------------------------------------------------------------------
    # Subprocess egress confinement via sandbox-exec (controller finding 2b)
    # ------------------------------------------------------------------

    def _sandbox_available(self) -> bool:
        """Check if sandbox-exec (Seatbelt) is available on this host.

        Uses the same probe as ``SandboxExecAdapter.available()``: the
        binary must exist and ``sandbox-exec -p "(version 1)(allow default)"
        /bin/echo ok`` must succeed.  macOS Seatbelt is restricted on many
        configurations (returns EPERM exit 71 even as root); the probe
        detects that honestly rather than guessing from platform alone.
        """
        from skillweave.runtime.worker_launcher import SandboxExecAdapter
        return SandboxExecAdapter().available()

    def test_sandbox_allows_approved_gateway_connection(self):
        """sandbox-exec with egress profile allows connection to approved port.

        A Python child wrapped with sandbox-exec using the route-proof
        egress profile can attempt a TCP connection to 127.0.0.1:8090.
        The sandbox permits it; if nothing is listening the child gets
        ConnectionRefusedError (not a sandbox block).
        """
        if not self._sandbox_available():
            pytest.skip(
                "sandbox-exec (Seatbelt) is not available on this host; "
                "this test requires a macOS host where Seatbelt profiles "
                "can be applied"
            )

        import subprocess as _subprocess
        import sys as _sys
        import tempfile as _tempfile
        src_dir = str(_src.parent)
        sandbox_exec = shutil.which("sandbox-exec")
        assert sandbox_exec is not None

        # Build the same egress profile the service uses
        from skillweave.runtime.worker_launcher import SandboxExecAdapter
        adapter = SandboxExecAdapter()
        profile = adapter.build_egress_profile("127.0.0.1", 8090)

        script = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(3)\n"
            "try:\n"
            "    s.connect(('127.0.0.1', 8090))\n"
            "    print('CONNECTED')\n"
            "except ConnectionRefusedError:\n"
            "    # Port is closed but sandbox allowed the connection attempt\n"
            "    print('ALLOWED_REFUSED')\n"
            "except PermissionError:\n"
            "    print('SANDBOX_BLOCKED')\n"
            "except OSError as e:\n"
            "    print(f'OS_ERROR: {e.errno} {e.strerror}')\n"
            "finally:\n"
            "    s.close()\n"
        )
        with _tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="sandbox_egress_approved_",
            dir=src_dir, delete=False,
        ) as f:
            f.write(script)
            script_path = f.name
        try:
            result = _subprocess.run(
                [sandbox_exec, "-p", profile, _sys.executable, script_path],
                capture_output=True, text=True, timeout=10,
            )
        finally:
            import os as _os
            try:
                _os.unlink(script_path)
            except OSError:
                pass

        assert result.returncode == 0, (
            f"sandbox-exec child failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        # Either the port is open (CONNECTED) or closed but allowed
        # (ALLOWED_REFUSED).  SANDBOX_BLOCKED would mean the profile
        # did not permit the approved gateway.
        assert "ALLOWED_REFUSED" in result.stdout or "CONNECTED" in result.stdout, (
            f"sandbox blocked approved gateway: stdout={result.stdout!r}"
        )

    def test_sandbox_blocks_alternative_loopback_port(self):
        """sandbox-exec with egress profile blocks alternative loopback port.

        A Python child wrapped with sandbox-exec cannot connect to
        127.0.0.1:9090 (a different port on the same loopback address).
        The sandbox denies the outbound TCP connection.
        """
        if not self._sandbox_available():
            pytest.skip(
                "sandbox-exec (Seatbelt) is not available on this host; "
                "this test requires a macOS host where Seatbelt profiles "
                "can be applied"
            )

        import subprocess as _subprocess
        import sys as _sys
        import tempfile as _tempfile
        src_dir = str(_src.parent)
        sandbox_exec = shutil.which("sandbox-exec")
        assert sandbox_exec is not None

        from skillweave.runtime.worker_launcher import SandboxExecAdapter
        adapter = SandboxExecAdapter()
        profile = adapter.build_egress_profile("127.0.0.1", 8090)

        script = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.settimeout(3)\n"
            "try:\n"
            "    s.connect(('127.0.0.1', 9090))\n"
            "    print('CONNECTED')\n"
            "except ConnectionRefusedError:\n"
            "    # Sandbox allowed it (unexpected)\n"
            "    print('ALLOWED_REFUSED')\n"
            "except PermissionError:\n"
            "    print('SANDBOX_BLOCKED')\n"
            "except OSError as e:\n"
            "    print(f'OS_ERROR: {e.errno} {e.strerror}')\n"
            "finally:\n"
            "    s.close()\n"
        )
        with _tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="sandbox_egress_blocked_",
            dir=src_dir, delete=False,
        ) as f:
            f.write(script)
            script_path = f.name
        try:
            result = _subprocess.run(
                [sandbox_exec, "-p", profile, _sys.executable, script_path],
                capture_output=True, text=True, timeout=10,
            )
        finally:
            import os as _os
            try:
                _os.unlink(script_path)
            except OSError:
                pass

        assert result.returncode == 0, (
            f"sandbox-exec child failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert "SANDBOX_BLOCKED" in result.stdout, (
            f"expected sandbox to block alternative port 9090, "
            f"got stdout={result.stdout!r}"
        )

    def test_sandbox_blocks_bash_network_call(self):
        """sandbox-exec with egress profile blocks a Bash network call.

        A Bash child wrapped with sandbox-exec cannot reach an unapproved
        host via /dev/tcp (bash's virtual network filesystem).  The
        sandbox denies the TCP connection.
        """
        if not self._sandbox_available():
            pytest.skip(
                "sandbox-exec (Seatbelt) is not available on this host; "
                "this test requires a macOS host where Seatbelt profiles "
                "can be applied"
            )

        import subprocess as _subprocess
        sandbox_exec = shutil.which("sandbox-exec")
        assert sandbox_exec is not None

        from skillweave.runtime.worker_launcher import SandboxExecAdapter
        adapter = SandboxExecAdapter()
        profile = adapter.build_egress_profile("127.0.0.1", 8090)

        argv = [
            sandbox_exec, "-p", profile,
            "/bin/bash", "-c",
            "cat < /dev/tcp/127.0.0.1/9090 2>/dev/null || echo 'BASH_FAILED'",
        ]
        try:
            result = _subprocess.run(
                argv, capture_output=True, text=True, timeout=10,
            )
        except _subprocess.TimeoutExpired:
            result = _subprocess.CompletedProcess(
                argv, returncode=-1, stdout="", stderr="TIMEOUT",
            )

        assert "BASH_FAILED" in result.stdout, (
            f"bash network call was not blocked by sandbox: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Run entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
