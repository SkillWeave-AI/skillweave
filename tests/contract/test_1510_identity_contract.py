"""SW-1510-IDENTITY contract tests at the SDK production boundary.

The SDK freezes the execution wire contracts and supplies the *pure* authority
verifiers. It deliberately cannot authenticate: ``AuthenticatedServiceContext``
is a shape guard, and ``verify_evidence_receipt_authority`` trusts a context it
is handed. These tests pin the contract that the Core authority seam must honor
when it calls that SDK boundary:

1. A receipt the host service authenticated is *schema-valid* under the frozen
   ``evidence-receipt-v1`` contract at the exact boundary that admits it.
2. The SDK verifier is invoked with host-built context, and short-circuits
   (context is never even built) when the presented credential is not a real
   authenticated reviewer.
3. The admission binding is exact: the same signed body presented under a
   different attempt, with a different artifact digest, or with a baked-in
   candidate map that does not match the known map, is refused.
4. A host-signed receipt whose text claims PASS but whose bound semantic
   artifact contract fails (missing raw bytes, wrong digest, no executed
   independent review, no semantic checks) is refused even though its signature
   is authentic.
5. Reusing inert ``auth_real`` / ``obs_real`` strings, or forwarding a
   caller-constructed context, does not satisfy the boundary.

Every negative case is a named fixture so the failure mode is traceable.
"""

import hashlib
import hmac
import json
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

import skillweave_sdk.validator as _sdk  # noqa: E402
from skillweave.runtime.authority_service import (  # noqa: E402
    HostAuthorityService,
    IdentityBoundaryError,
    ObservedReview,
    digest_candidate_map,
    sign_receipt_body,
)
from skillweave.runtime.registry import (  # noqa: E402
    RawArtifactStore,
)

_EVIDENCE_SCHEMA_ID = "https://skillweave.dev/schemas/execution/evidence-receipt/v1"


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    import tempfile as _tf

    monkeypatch.setattr(_tf, "tempdir", str(tmp_path), raising=False)
    return tmp_path


def _scope():
    return {"paths": ["/work/out", "/work/scratch"], "mode": "rw"}


def _brief():
    return {"task_id": "task-1", "criteria": ["c1", "c2"]}


def _policy():
    return {"decision": "admit-independent-review"}


def _identity(service, *, run_id="run-1", task_id="task-1", attempt=1):
    scope, brief, policy = _scope(), _brief(), _policy()
    worker_token = service.issue_attempt_credential(
        run_id=run_id, task_id=task_id, attempt=attempt, scope=scope
    )
    reviewer_token = service.registry.issue(
        principal="reviewer-1",
        roles={"reviewer"},
        attempt_key=(run_id, task_id, attempt),
        capability="attest-receipt",
    )
    decision = service.issue_authority_decision(
        principal="reviewer-1",
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        scope=scope,
        brief=brief,
        policy=policy,
    )
    return worker_token, reviewer_token, decision


def _build_receipt(service, reviewer_token, decision, raw, *, body=b"artifact bytes"):
    """Construct the frozen-contract receipt the production seam signs."""
    artifact_digest = raw.put(body)
    reviewer = service.verify_credential(reviewer_token)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt": 1,
        "criterion_id": "c1",
        "subject_digest": hashlib.sha256(b"subject").hexdigest(),
        "artifact_digest": artifact_digest,
        "verifier_version": "1",
        "verdict": "pass",
        "issuer": service.registry.service_principal,
        "principal": "reviewer-1",
        "authority_decision_digest": decision.decision_digest,
        "semantic_checks": [
            {"check_id": "bytes-match", "result": "pass", "evidence_digest": artifact_digest}
        ],
    }
    receipt["signature"] = sign_receipt_body(receipt, reviewer)
    observed = ObservedReview(
        reviewer_principal="reviewer-1",
        attempt_key=("run-1", "task-1", 1),
        artifact_digest=artifact_digest,
        verdict="pass",
        independent=True,
        observed_at="2026-10-05T00:00:00Z",
    )
    return receipt, observed, artifact_digest


def _known_candidate_map():
    return {"task-1": hashlib.sha256(b"candidate").hexdigest()}


# ---------------------------------------------------------------------------
# 1. Schema validity at the admitted boundary
# ---------------------------------------------------------------------------


def test_admitted_receipt_is_schema_valid_under_frozen_contract():
    """The exact receipt the seam admits validates against evidence-receipt-v1."""
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _build_receipt(
        service, reviewer_token, decision, raw
    )

    # It is schema-valid *before* ingestion (the wire shape is the frozen one).
    assert _sdk.validate(receipt, _EVIDENCE_SCHEMA_ID) == []

    ingested = service.ingest_evidence_receipt(
        receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
    )
    assert ingested.artifact_digest == artifact_digest


def test_frozen_schema_rejects_a_receipt_without_semantic_checks():
    """The contract itself forbids a checkless receipt; the seam agrees."""
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)
    del receipt["semantic_checks"]
    # Schema fails closed on the missing required array.
    assert _sdk.validate(receipt, _EVIDENCE_SCHEMA_ID)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


# ---------------------------------------------------------------------------
# 2. The SDK production verifier is actually invoked with host context
# ---------------------------------------------------------------------------


def test_sdk_verifier_is_invoked_with_host_built_context(monkeypatch):
    """The seam calls verify_evidence_receipt_authority with a built context."""
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)

    calls = []
    real = _sdk.verify_evidence_receipt_authority

    def spy(receipt_arg, context):
        calls.append(context)
        return real(receipt_arg, context)

    monkeypatch.setattr(
        "skillweave_sdk.validator.verify_evidence_receipt_authority", spy
    )
    # The authority_service module imported the name directly; patch there too.
    import skillweave.runtime.authority_service as _mod

    monkeypatch.setattr(_mod, "verify_evidence_receipt_authority", spy)

    service.ingest_evidence_receipt(
        receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
    )
    assert calls, "the SDK authority verifier must be invoked at the boundary"
    ctx = calls[0]
    assert isinstance(ctx, _sdk.AuthenticatedServiceContext)
    assert ctx.principal == "reviewer-1"
    assert ctx.roles & _sdk.REVIEWER_ROLES


def test_non_reviewer_credential_never_reaches_the_sdk_boundary(monkeypatch):
    """A credential lacking a reviewer role fails before the SDK call."""
    service = HostAuthorityService()
    worker_token, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    # A genuine receipt signed by the reviewer, presented with the worker token.
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)

    called = []
    monkeypatch.setattr(
        "skillweave_sdk.validator.verify_evidence_receipt_authority",
        lambda *a, **k: called.append(1),
    )
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=worker_token, raw_artifacts=raw, observed_review=observed
        )
    assert not called, "an unauthenticated principal must not reach the SDK verifier"


# ---------------------------------------------------------------------------
# 3. Exact attempt / artifact / candidate bindings
# ---------------------------------------------------------------------------


def test_receipt_replayed_under_a_different_attempt_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)
    # The signed body is intact, but it now claims attempt 2.
    receipt["attempt"] = 2
    reviewer = service.verify_credential(reviewer_token)
    receipt["signature"] = sign_receipt_body(receipt, reviewer)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_receipt_with_substituted_artifact_digest_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)
    other_digest = hashlib.sha256(b"different bytes").hexdigest()
    receipt["artifact_digest"] = other_digest
    receipt["semantic_checks"][0]["evidence_digest"] = other_digest
    reviewer = service.verify_credential(reviewer_token)
    receipt["signature"] = sign_receipt_body(receipt, reviewer)
    # Authentically signed, but the digest resolves to nothing the host stored.
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_stale_candidate_map_is_refused_at_the_boundary():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)

    known = _known_candidate_map()
    stale = {"task-1": hashlib.sha256(b"old-candidate").hexdigest()}

    ok = service.ingest_evidence_receipt(
        receipt,
        worker_token=reviewer_token,
        raw_artifacts=raw,
        observed_review=observed,
        candidate_sha_map=known,
        known_candidate_sha_map=known,
    )
    assert ok.artifact_digest == observed.artifact_digest

    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt,
            worker_token=reviewer_token,
            raw_artifacts=raw,
            observed_review=observed,
            candidate_sha_map=stale,
            known_candidate_sha_map=known,
        )


def test_missing_candidate_map_when_host_knows_one_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt,
            worker_token=reviewer_token,
            raw_artifacts=raw,
            observed_review=observed,
            known_candidate_sha_map=_known_candidate_map(),
        )
    assert digest_candidate_map(_known_candidate_map()) == digest_candidate_map(
        dict(_known_candidate_map())
    )


# ---------------------------------------------------------------------------
# 4. Authentic signature, failed semantic artifact contract
# ---------------------------------------------------------------------------


def test_host_signed_pass_receipt_with_missing_raw_bytes_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _build_receipt(service, reviewer_token, decision, raw)
    raw.delete(artifact_digest)  # the bound bytes are gone
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_host_signed_pass_receipt_with_mutated_bytes_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _build_receipt(service, reviewer_token, decision, raw)
    raw.mock_mutate(artifact_digest, b"tampered")  # bytes no longer hash to the digest
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_host_signed_pass_receipt_without_executed_review_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, _, _ = _build_receipt(service, reviewer_token, decision, raw)
    # No observed review execution backs the signed PASS claim.
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=None
        )


def test_host_signed_receipt_with_nonpassing_semantic_check_is_refused():
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)
    receipt["semantic_checks"][0]["result"] = "fail"
    reviewer = service.verify_credential(reviewer_token)
    receipt["signature"] = sign_receipt_body(receipt, reviewer)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


# ---------------------------------------------------------------------------
# 5. Inert strings / caller context do not authenticate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("claim", ["auth_real", "obs_real", "reviewer", "run-service"])
def test_inert_role_strings_are_not_credentials(claim):
    service = HostAuthorityService()
    raw = RawArtifactStore()
    _identity(service)
    with pytest.raises(IdentityBoundaryError):
        service.verify_credential(claim)


def test_caller_constructed_context_is_not_accepted_at_the_boundary():
    """A caller-made context never substitutes for host authentication."""
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)

    # A forged context that passes the SDK's own shape check...
    forged = _sdk.AuthenticatedServiceContext(
        principal="reviewer-1",
        roles=frozenset({"reviewer"}),
        service_principal=service.registry.service_principal,
        authority_decision_digests=frozenset({decision.decision_digest}),
    )
    _sdk.verify_evidence_receipt_authority(receipt, forged)  # SDK cannot tell

    # ...still cannot be fed to the production seam; it takes a *token* and
    # authenticates it against the registry, where the caller's claim is unknown.
    with pytest.raises(IdentityBoundaryError):
        service.verify_credential("reviewer-1")
    # The real token is the only thing that works.
    assert service.verify_credential(reviewer_token).principal == "reviewer-1"


# ---------------------------------------------------------------------------
# 6. The signing key is a host secret, not a guessable public string
# ---------------------------------------------------------------------------


def test_signature_over_public_strings_is_not_the_host_signature():
    """A worker cannot reproduce the signature by hashing public strings.

    The old derivation keyed the HMAC with ``service_principal + "\\x00" +
    principal`` — both of which appear in the wire receipt and so are known to a
    delegated worker. A receipt signed that way must be refused by the seam.
    """
    service = HostAuthorityService()
    _, reviewer_token, decision = _identity(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _build_receipt(service, reviewer_token, decision, raw)

    body = {k: v for k, v in sorted(receipt.items()) if k != "signature"}
    material = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    public_key = (
        f"{service.registry.service_principal}\x00{receipt['principal']}".encode("utf-8")
    )
    receipt["signature"] = hmac.new(public_key, material, hashlib.sha256).hexdigest()

    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_worker_credential_carries_no_signing_authority():
    """Only a review-capable credential carries the host receipt-signing secret.

    A worker's own token verifies through the same path; if it carried the
    secret, the worker could sign a receipt the host would accept.
    """
    service = HostAuthorityService()
    worker_token, reviewer_token, _ = _identity(service)
    worker = service.verify_credential(worker_token)
    reviewer = service.verify_credential(reviewer_token)
    assert worker.signing_secret == b"", "a worker credential must not carry signing authority"
    assert reviewer.signing_secret, "a reviewer credential must carry signing authority"
    # The secret is never placed on a wire record or a serialized representation.
    assert "signing_secret" not in repr(worker)
    assert reviewer.signing_secret.hex() not in repr(reviewer)


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in _tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
