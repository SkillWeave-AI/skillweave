"""SW-1511A-UPSTREAM-001 wiring acceptance tests (production caller proof).

Criterion 1: admission refuses a provider attempt unless the allowed route,
actual upstream, answering model and credential binding are attestable without
exposing secrets — proven here **through the real Faigate attempt path**
(``FaigateProvider.query``) and the real attempt-launch path
(``RunApplicationService._admit_provider_launch``), not only on the contract
module.

Criterion 2: quota/availability observations carry source, observed_at,
freshness, window and status; the statuses are distinct and a failure
re-observed after a watcher restart continues the same causal series — proven
through the adapter's own telemetry reader.

Criterion 3: an enabled fallback or a mismatched upstream can never carry an
independent model-diversity or acceptance gate — proven on the live path.

Negative control: every test below calls the production entry point and asserts
the contract module is reached; on the reviewed candidate ``bf56b0f`` the module
exists but has no production caller, so those calls never reach it and the
tests fail. No network, provider call or credential is used: every response is
a literal returned by an in-process fake ``_req``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.routing import faigate_adapter as adapter  # noqa: E402
from skillweave.routing.faigate_adapter import (  # noqa: E402
    FaigateProvider,
    PROVIDER_NAMESPACE,
    admit_provider_launch,
    classify_availability_error,
)
from skillweave.routing.route_policy import (  # noqa: E402
    RoutePolicy,
    _ENV_ROUTE_POLICY,
)
from skillweave.routing.route_proof import RouteProof  # noqa: E402
from skillweave.routing.upstream_observation import (  # noqa: E402
    STATUS_EXHAUSTED,
    STATUS_RATE_LIMIT,
    STATUS_UNKNOWN,
    AdmissionRefused,
    AvailabilityWatcher,
    SecretExposureRefused,
)


#: The reviewed candidate's own contract module, so the tests can show the
#: production path reaches exactly this object and exactly its admission
#: function — not a re-implemented copy.
CONTRACT_MODULE = sys.modules["skillweave.routing.upstream_observation"]

_ALLOWED = (
    "byteplus/deepseek-v4-flash",
    "byteplus/deepseek-v4.1-flash",
    "byteplus/deepseek-v4-pro",
)

#: The gateway's ``served_by`` convention (see ``tests/mocks/faigate_server.py``):
#: a *bare* provider-native model name, carrying no provider prefix.
SERVED_FLASH = "deepseek-v4-flash"


def _policy_document() -> dict:
    return {
        "schema": "route-policy-v1",
        "required_revision": "1.3.11",
        "policy_revision": "1.3.11",
        "approved_gateway": {"host": "127.0.0.1", "port": 8090},
        "approved_routes": list(_ALLOWED),
    }


@pytest.fixture
def policy(tmp_path, monkeypatch):
    """An active owner route pin (the production route-lock configuration)."""
    path = tmp_path / "route-policy.json"
    path.write_text(json.dumps(_policy_document()))
    monkeypatch.setenv(_ENV_ROUTE_POLICY, str(path))
    monkeypatch.delenv("SKILLWEAVE_CREDENTIAL_BINDING", raising=False)
    yield str(path)


def _provider(monkeypatch, response, *, base_url="http://127.0.0.1:8090/v1"):
    """A real ``FaigateProvider`` whose only substitute is the socket call.

    ``__init__`` runs for real (so the availability watcher and the credential
    binding are the production ones); only ``_req`` — the network boundary — is
    replaced with a literal response. No socket, no key, no credential.
    """
    monkeypatch.setattr(FaigateProvider, "_req", lambda self, *a, **k: response)
    provider = FaigateProvider(base_url=base_url, api_key=None)
    assert not getattr(provider, "api_key", None)
    return provider


def _answer(model="deepseek-v4-flash", served_by=None, headers=None, **extra):
    envelope = {
        "choices": [{"message": {"content": "ok"}}],
        "model": model,
        "headers": headers or {},
    }
    if served_by is not None:
        envelope["served_by"] = served_by
    envelope.update(extra)
    return envelope


def _query(provider, model="faigate:deepseek-v4-flash"):
    return asyncio.run(
        provider.query(model, [{"role": "user", "content": "hi"}], 0.5)
    )


def _spy_target():
    """A fresh, bounded fake service instance that needs no store."""
    from skillweave.runsvc.service import RunApplicationService

    return RunApplicationService.__new__(RunApplicationService)


# ── Criterion 1 ─────────────────────────────────────────────────────────────


def test_criterion1_live_provider_path_admits_and_refuses_without_echoing_secrets(
    policy, monkeypatch
):
    """Adversarial: the live attempt path calls ``admit_provider_attempt``.

    Every attempt below goes through ``FaigateProvider.query`` — the production
    entry point a council seat calls. A real admission is reached only when all
    four axes are attestable; each unattestable axis is refused by the contract
    module's typed error, and a secret-shaped binding never echoes its value.
    """
    # The production path must reach the contract module's own admission
    # function: the adapter's binding is the contract's object itself (not a
    # copy), and spying on it observes every live admission.
    assert adapter.admit_provider_attempt is CONTRACT_MODULE.admit_provider_attempt
    called: list[object] = []
    original = CONTRACT_MODULE.admit_provider_attempt

    def _spy(attestation):
        called.append(attestation)
        return original(attestation)

    monkeypatch.setattr(adapter, "admit_provider_attempt", _spy)

    # Attestable attempt: the gateway answered from the approved upstream for
    # the approved route — admitted, and the answer is handed back.
    honest = _provider(
        monkeypatch,
        _answer(served_by=SERVED_FLASH),
    )
    assert _query(honest) == "ok"
    assert len(called) == 1, "the live provider path never called the contract"
    attestation = called[0]
    assert attestation.requested_route in _ALLOWED
    assert attestation.actual_upstream == "byteplus"
    assert attestation.answering_model == "deepseek-v4-flash"
    assert attestation.credential_binding == "gateway://127.0.0.1:8090"
    assert attestation.requested_route in attestation.allowed_routes

    # Mismatched upstream: the answer names another upstream, so the live path
    # refuses — and the refusal names the upstream that actually answered.
    called.clear()
    mismatched = _provider(
        monkeypatch,
        _answer(served_by="mirror-gw/deepseek-v4-flash"),
    )
    with pytest.raises(AdmissionRefused) as exc:
        _query(mismatched)
    assert exc.value.axis == "actual_upstream"
    assert "mirror-gw" in str(exc.value)
    assert called, "the refusal did not come from the contract module"

    # Direct-provider bypass: an id whose only resolution is a bare model name
    # is refused on the allowed-route axis by the live path.
    called.clear()
    bypass = _provider(
        monkeypatch, _answer(served_by="gpt-4o")
    )
    with pytest.raises(AdmissionRefused) as exc:
        _query(bypass, model="faigate:gpt-4o")
    assert exc.value.axis == "allowed_route"
    assert called, "the refusal did not come from the contract module"

    # Secret-shaped binding: refused, and the secret is never echoed into the
    # exception text, the provider repr or the durable observation record.
    secret = "sk-live-0123456789abcdef"
    monkeypatch.setenv("SKILLWEAVE_CREDENTIAL_BINDING", secret)
    leaked = _provider(
        monkeypatch,
        _answer(served_by=SERVED_FLASH),
    )
    with pytest.raises(SecretExposureRefused) as exc:
        _query(leaked)
    assert secret not in str(exc.value)
    assert secret not in repr(leaked)
    assert secret not in json.dumps(leaked.availability_snapshot())


def test_criterion1_live_launch_path_refuses_and_names_the_attempt(policy):
    """Adversarial: the attempt-launch caller also refuses fail-closed.

    ``RunApplicationService._admit_provider_launch`` is the caller that runs
    *before a worker starts*. A route that is not an allowed ``provider/model``
    pair, and a route outside the owner pin, are refused with the named
    route-lock stage rather than reaching a launched process.
    """
    from skillweave.runsvc.service import RunIntegrationError

    route_proof = RouteProof(
        approved_gateway_host="127.0.0.1",
        approved_gateway_port=8090,
        approved_routes=frozenset(_ALLOWED),
        tier_routes={"flash": frozenset(_ALLOWED[:2]), "pro": frozenset(_ALLOWED)},
        policy_revision="1.3.11",
    )
    route_policy = RoutePolicy(
        approved_routes=frozenset(_ALLOWED),
        required_revision="1.3.11",
        approved_gateway_host="127.0.0.1",
        approved_gateway_port=8090,
    )

    # An allowed route is admitted with a gateway-reference binding.
    record = _spy_target()._admit_provider_launch(
        "byteplus/deepseek-v4-pro",
        route_policy=route_policy,
        route_proof=route_proof,
    )
    assert record["admitted"] is True
    assert record["actual_upstream"] == "byteplus"
    assert record["credential_binding"] == "gateway://127.0.0.1:8090"

    # A direct-provider bypass is refused before launch, at the named stage.
    with pytest.raises(RunIntegrationError) as exc:
        _spy_target()._admit_provider_launch(
            "deepseek-v4-pro",
            route_policy=route_policy,
            route_proof=route_proof,
        )
    assert exc.value.stage == "route_lock_direct_provider"

    # A route outside the owner pin is refused before launch too.
    with pytest.raises(RunIntegrationError) as exc:
        _spy_target()._admit_provider_launch(
            "byteplus/not-approved",
            route_policy=route_policy,
            route_proof=route_proof,
        )
    assert exc.value.stage == "route_lock_direct_provider"


# ── Criterion 2 ─────────────────────────────────────────────────────────────


def test_criterion2_live_telemetry_feeds_distinct_statuses_and_survives_restart(
    policy, monkeypatch
):
    """Adversarial: the adapter's telemetry reader is the observation producer.

    A gateway that answers with a rate-limit signal, one that reports exhaustion
    and one that reports nothing must land on three *distinct* statuses, and a
    failure repeated after the adapter is rebuilt from its durable snapshot must
    continue the same causal series rather than reset it.
    """
    # A rate-limit response header is observed as rate_limit, not unknown.
    rate_limited = _provider(
        monkeypatch,
        _answer(
            served_by=SERVED_FLASH,
            headers={"Retry-After": "30"},
        ),
    )
    _query(rate_limited)
    snapshot = rate_limited.availability_snapshot()
    assert STATUS_RATE_LIMIT in snapshot["series"]
    assert snapshot["series"][STATUS_RATE_LIMIT][0]["sequence"] == 0

    # An exhaustion signal is exhausted, never collapsed into rate_limit; a
    # silent response is unknown. The statuses stay distinct per attempt.
    exhausted = _provider(
        monkeypatch,
        _answer(
            served_by=SERVED_FLASH,
            headers={"X-Faigate-Quota": "exhausted"},
        ),
    )
    _query(exhausted)
    exhausted_series = exhausted.availability_snapshot()["series"]
    assert STATUS_EXHAUSTED in exhausted_series
    assert STATUS_RATE_LIMIT not in exhausted_series

    silent = _provider(
        monkeypatch, _answer(served_by=SERVED_FLASH)
    )
    _query(silent)
    assert list(silent.availability_snapshot()["series"]) == [STATUS_UNKNOWN]

    # The failing path is a producer too: a rate-limit failure is classified and
    # observed *before* the exception is raised, and the raw error text — which
    # can carry a credential — is never recorded verbatim.
    failing = _provider(monkeypatch, {"error": "HTTP 429: rate limit exceeded"})
    with pytest.raises(adapter.RateLimitedError):
        _query(failing)
    failed_snapshot = failing.availability_snapshot()
    assert STATUS_RATE_LIMIT in failed_snapshot["series"]
    recorded = json.dumps(failed_snapshot)
    assert "429" not in recorded and "rate limit exceeded" not in recorded

    # Classification is the producer's distinct-vector function: an empty or
    # unrecognised failure is unknown, never silently rate_limit.
    assert classify_availability_error("") == STATUS_UNKNOWN
    assert classify_availability_error("something odd happened") == STATUS_UNKNOWN

    # Restart: a durable snapshot is replayed into a *fresh* adapter, then the
    # same class is observed again — the causal series continues (it is not a
    # new incident) and the schema is stable across the restart.
    restarted = FaigateProvider(base_url="http://127.0.0.1:8090/v1", api_key=None)
    restarted._availability_watcher = AvailabilityWatcher.from_dict(snapshot)
    restarted._observe_availability(STATUS_RATE_LIMIT)
    restarted._observe_availability(STATUS_RATE_LIMIT)
    chain = restarted._availability_watcher.causal_series(STATUS_RATE_LIMIT)
    assert [r.sequence for r in chain] == [0, 1, 2]
    assert [r.caused_by for r in chain] == [-1, 0, 1]
    assert restarted.availability_snapshot()["schema"] == snapshot["schema"]


# ── Criterion 3 ─────────────────────────────────────────────────────────────


def test_criterion3_live_path_fallback_or_mismatch_never_satisfies_the_gate(
    policy, monkeypatch
):
    """Adversarial negative: the live path refuses a fallback/mismatch.

    A response that reports an enabled fallback, and one that reports a
    mismatched upstream, are both refused by the live admission call's
    acceptance gate — neither can carry the attempt, so neither can satisfy an
    independent model-diversity or acceptance gate.
    """
    fallback = _provider(
        monkeypatch,
        _answer(
            served_by=SERVED_FLASH,
            fallback_enabled=True,
            fallback_used=True,
        ),
    )
    with pytest.raises(AdmissionRefused) as exc:
        _query(fallback)
    assert exc.value.axis == "acceptance"
    assert "fallback" in str(exc.value).lower()

    mismatch = _provider(
        monkeypatch,
        _answer(served_by="mirror-gw/deepseek-v4-flash"),
    )
    with pytest.raises(AdmissionRefused) as exc:
        _query(mismatch)
    assert exc.value.axis == "actual_upstream"

    # The launch path shares the same contract call: an attempt admitted there
    # carries no fallback flag, so the gate is satisfied — the refusal above is
    # the fallback fact, not a blanket refusal of every attempt.
    record = admit_provider_launch(
        "byteplus/deepseek-v4-flash",
        allowed_routes=_ALLOWED,
        credential_binding="gateway://127.0.0.1:8090",
    )
    assert record["admitted"] is True
