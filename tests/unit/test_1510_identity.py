"""SW-1510-IDENTITY unit tests: real worker/verifier/observer/review authority.

These prove, at the actual API boundary:

* only genuine host-issued credential tokens authenticate (no ``auth_real`` /
  ``obs_real`` strings, no caller-made context);
* forged/replayed/expired/revoked worker tokens fail before any mutation;
* a semantically bogus but authentically signed receipt still fails its bound
  artifact contract;
* the protected receipt/gate store boundaries refuse worker-shaped calls;
* the legacy pure-policy helpers keep their historical behavior without being
  promoted to trusted authority.
"""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.runtime.authority_service import (  # noqa: E402
    GATE_CAPABLE_ROLES,
    HostAuthorityService,
    IdentityBoundaryError,
    ObservedReview,
    admit_gate,
    digest_candidate_map,
    sign_receipt_body,
)
from skillweave.runtime.registry import (  # noqa: E402
    ArtifactReceipt,
    EvidenceQuality,
    EvidenceType,
    RawArtifactStore,
)
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    """Point tempfile at the scratch basetemp so no probe touches host TMPDIR."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    return tmp_path


def _service():
    return HostAuthorityService()


def _scope():
    return {"paths": ["/work/out", "/work/scratch"], "mode": "rw"}


def _brief():
    return {"task_id": "task-1", "criteria": ["c1", "c2"]}


def _policy():
    return {"decision": "admit-independent-review"}


def _issue(service, run_id="run-1", task_id="task-1", attempt=1):
    scope = _scope()
    brief = _brief()
    policy = _policy()
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


def _signed_receipt(service, reviewer_token, decision, raw, body=b"artifact bytes"):
    artifact_digest = raw.put(body)
    reviewer = service.verify_credential(reviewer_token)
    subject_digest = hashlib.sha256(b"subject").hexdigest()
    receipt = {
        "schema_version": "1.0.0",
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt": 1,
        "criterion_id": "c1",
        "subject_digest": subject_digest,
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


# ---------------------------------------------------------------------------
# 1. Genuine separate authenticated principals
# ---------------------------------------------------------------------------


def test_genuine_principals_authenticate_and_produce_a_positive_decision():
    service = _service()
    worker_token, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _signed_receipt(
        service, reviewer_token, decision, raw
    )

    ingested = service.ingest_evidence_receipt(
        receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
    )
    assert ingested.reviewer_principal == "reviewer-1"
    assert ingested.artifact_digest == artifact_digest

    controller_token = service.registry.issue(
        principal="controller-1",
        roles={"controller"},
        attempt_key=("run-1", "task-1", 1),
        capability="admit-gate",
    )
    admission = admit_gate(
        service, controller_token=controller_token, ingested=ingested,
        decision=decision, gate_state="pass",
    )
    assert admission.admitted_by == "controller-1"
    assert admission.gate_state == "pass"
    assert admission.receipt_artifact_digest == artifact_digest

    # The worker credential is a real, distinct principal that cannot approve.
    worker = service.verify_credential(worker_token)
    assert worker.principal.startswith("worker:")
    assert not (worker.roles & {"reviewer", "observer", "controller", "integrator"})
    assert GATE_CAPABLE_ROLES.isdisjoint(worker.roles)


def test_worker_credential_cannot_attest_its_own_attempt():
    service = _service()
    worker_token, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=worker_token, raw_artifacts=raw, observed_review=observed
        )


# ---------------------------------------------------------------------------
# 2. auth_real / obs_real strings, forged signature, replay, expired, revoked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("string_token", ["auth_real", "obs_real", "reviewer", "service"])
def test_caller_strings_are_not_credentials(string_token):
    service = _service()
    _issue(service)
    with pytest.raises(IdentityBoundaryError):
        service.verify_credential(string_token)


def test_caller_created_context_is_not_accepted_by_the_production_boundary():
    # A caller can build the SDK's shape-check context; the production boundary
    # never consults it. Only a host-verified credential is accepted.
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    from skillweave_sdk.validator import AuthenticatedServiceContext, verify_evidence_receipt_authority

    forged_context = AuthenticatedServiceContext(
        principal="reviewer-1", roles=frozenset({"reviewer"}),
        service_principal=service.registry.service_principal,
        issued_handle_ids=frozenset(), authority_decision_digests=frozenset(
            {decision.decision_digest}
        ),
    )
    # The SDK's shape check passes for the caller-made context...
    verify_evidence_receipt_authority(receipt, forged_context)
    # ...but the production ingestion still requires a real credential token.
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token="auth_real", raw_artifacts=raw, observed_review=observed
        )


def test_forged_signature_fails_even_for_authentic_reviewer():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    receipt["signature"] = "deadbeef" * 8
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_worker_signed_fake_receipt_cannot_pass():
    # A worker signs a receipt with its own (non-service) knowledge: the HMAC
    # key is the host service secret, so a worker cannot produce a valid one.
    service = _service()
    worker_token, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    receipt["signature"] = "0" * 64  # worker guess
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_expired_credential_is_rejected_before_mutation(tmp_path):
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    service.registry.expire(reviewer_token)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_revoked_credential_is_rejected():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    service.registry.revoke_token(reviewer_token)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_revoked_worker_principal_revokes_all_tokens():
    service = _service()
    worker_token, _, _ = _issue(service)
    assert service.verify_credential(worker_token).principal.startswith("worker:")
    service.registry.revoke_principal("worker:run-1:task-1:1")
    with pytest.raises(IdentityBoundaryError):
        service.verify_credential(worker_token)


def test_replayed_token_from_other_attempt_is_rejected():
    service = _service()
    _, reviewer_token_a, decision_a = _issue(service)
    # A second attempt issues its own reviewer credential and decision.
    _, reviewer_token_b, decision_b = _issue(service, run_id="run-2", task_id="task-2")
    raw = RawArtifactStore()
    receipt_b, observed_b, _ = _signed_receipt(
        service, reviewer_token_b, decision_b, raw
    )
    # Rewriting attempt A's receipt fields onto B fails the exact-attempt bind.
    replayed = dict(receipt_b)
    replayed["run_id"] = "run-1"
    replayed["task_id"] = "task-1"
    replayed["signature"] = sign_receipt_body(
        replayed, service.verify_credential(reviewer_token_b)
    )
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            replayed, worker_token=reviewer_token_b, raw_artifacts=raw, observed_review=observed_b
        )


# ---------------------------------------------------------------------------
# 3. Authentically signed but semantically bogus receipts
# ---------------------------------------------------------------------------


def test_authentically_signed_receipt_with_missing_raw_bytes_fails():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _signed_receipt(service, reviewer_token, decision, raw)
    raw.delete(artifact_digest)  # the raw artifact is gone
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_authentically_signed_receipt_with_wrong_digest_fails():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _signed_receipt(service, reviewer_token, decision, raw)
    raw.mock_mutate(artifact_digest, b"tampered bytes")  # bytes no longer match
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_signed_pass_without_observed_review_is_not_a_pass():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, _, _ = _signed_receipt(service, reviewer_token, decision, raw)
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=None
        )


def test_signed_receipt_with_self_review_is_rejected():
    service = _service()
    worker_token, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, _, artifact_digest = _signed_receipt(service, reviewer_token, decision, raw)
    self_review = ObservedReview.__new__(ObservedReview)  # bypass post-init for the fixture
    object.__setattr__(self_review, "reviewer_principal", "worker:run-1:task-1:1")
    object.__setattr__(self_review, "attempt_key", ("run-1", "task-1", 1))
    object.__setattr__(self_review, "artifact_digest", artifact_digest)
    object.__setattr__(self_review, "verdict", "pass")
    object.__setattr__(self_review, "independent", True)
    object.__setattr__(self_review, "observed_at", "now")
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=self_review
        )


def test_signed_receipt_without_semantic_checks_is_rejected():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    receipt["semantic_checks"] = []
    receipt["signature"] = sign_receipt_body(
        receipt, service.verify_credential(reviewer_token)
    )
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
        )


def test_stale_candidate_sha_map_is_rejected():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, _ = _signed_receipt(service, reviewer_token, decision, raw)
    known = {"task-1": "a" * 40}
    stale = {"task-1": "b" * 40}
    with pytest.raises(IdentityBoundaryError):
        service.ingest_evidence_receipt(
            receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed,
            candidate_sha_map=stale, known_candidate_sha_map=known,
        )
    # The exact matching map is admitted.
    ok = service.ingest_evidence_receipt(
        receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed,
        candidate_sha_map=dict(known), known_candidate_sha_map=dict(known),
    )
    assert ok.artifact_digest == receipt["artifact_digest"]
    assert digest_candidate_map(known) == digest_candidate_map(dict(known))


# ---------------------------------------------------------------------------
# 4. Protected receipt / gate store boundaries
# ---------------------------------------------------------------------------


def _receipt(artifact_id="a1", sha="a" * 64):
    return ArtifactReceipt(
        artifact_id=artifact_id, sha256=sha, schema_version="1", producer_command="cmd",
        subject_repo="r", subject_commit="c", created_at="t",
        evidence_type=EvidenceType.ARTIFACT.value, purpose="p", method="m",
        system_source="s", quality=EvidenceQuality(),
    )


def test_protected_store_refuses_worker_shaped_receipt_write():
    store = SQLiteRunStore()
    with pytest.raises(IdentityBoundaryError):
        store.save_protected_evidence(_receipt(), ingested=object())
    # Nothing was persisted by the refused protected write.
    assert store.get_evidence("a1") is None


def test_protected_store_refuses_unissued_gate_admission():
    store = SQLiteRunStore()
    store.create_run("run-1")
    with pytest.raises(IdentityBoundaryError):
        store.transition_with_gate_admission(
            "run-1", "implement", "preflight", 1, admission={"run_id": "run-1"}
        )


def test_protected_store_admits_a_host_ingested_receipt():
    service = _service()
    _, reviewer_token, decision = _issue(service)
    raw = RawArtifactStore()
    receipt, observed, artifact_digest = _signed_receipt(service, reviewer_token, decision, raw)
    ingested = service.ingest_evidence_receipt(
        receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
    )
    store = SQLiteRunStore()
    saved = store.save_protected_evidence(
        _receipt(artifact_id="attested-receipt-1", sha=artifact_digest), ingested
    )
    assert saved.sha256 == artifact_digest
    assert store.get_evidence("attested-receipt-1") is not None


# ---------------------------------------------------------------------------
# 5. Legacy pure-policy compatibility (not promoted to trusted authority)
# ---------------------------------------------------------------------------


def test_legacy_authority_guard_still_approves_caller_strings_but_is_not_authority():
    # Historical behavior is preserved: AuthorityGuard is descriptive policy.
    from skillweave.runtime.authority import AuthorityGuard, ROLE_CAPABILITY_MATRIX

    guard = AuthorityGuard()
    approval = guard.approve(actor="auth_real", role="reviewer", scope="s", policy_digest="d")
    assert approval.actor == "auth_real"
    assert guard.validate_approval(approval, approving_role="reviewer") is True
    # ...and it grants the production boundary nothing: the same string is not
    # an authenticated credential.
    assert "reviewer" in ROLE_CAPABILITY_MATRIX
    service = _service()
    with pytest.raises(IdentityBoundaryError):
        service.verify_credential("auth_real")


def test_legacy_save_evidence_is_unchanged():
    store = SQLiteRunStore()
    store.save_evidence(_receipt())
    assert store.get_evidence("a1") is not None


# ---------------------------------------------------------------------------
# 6. Real isolated worker process: write/read denial at the OS boundary
# ---------------------------------------------------------------------------


def _protected_stores(base: Path):
    from skillweave.runtime.worker_launcher import PROTECTED_STORE_NAMES, ProtectedStores

    protected = ProtectedStores({name: base / name for name in PROTECTED_STORE_NAMES})
    protected.ensure()
    return protected


def test_real_worker_child_cannot_write_protected_stores(tmp_path):
    from skillweave.runtime.worker_launcher import (
        IsolationUnavailableError,
        select_adapter,
    )

    adapter = select_adapter()
    if not adapter.available():
        pytest.skip("no real store-isolation adapter on this host")
    protected = _protected_stores(tmp_path)
    scope = tmp_path / "worker_scope"
    scope.mkdir()
    gate_root = protected.roots["gate_state"]
    (gate_root / "receipt.json").write_text("original")

    child = (
        "import sys, json, pathlib\n"
        "g = pathlib.Path(sys.argv[1])\n"
        "res = {}\n"
        "for label, fn in ((\"overwrite\", lambda: (g/'receipt.json').write_text('PWNED')),\n"
        "                  (\"create\", lambda: (g/'new.json').write_text('x')),\n"
        "                  (\"unlink\", lambda: (g/'receipt.json').unlink())):\n"
        "    try:\n"
        "        fn(); res[label] = 'wrote'\n"
        "    except OSError as e:\n"
        "        res[label] = 'denied:%s' % e.errno\n"
        "print(json.dumps(res))\n"
    )
    from skillweave.runtime.worker_launcher import protect_stores

    with protect_stores(protected, adapter=adapter) as protection:
        proc = subprocess.run(
            [sys.executable, "-c", child, str(gate_root)],
            capture_output=True, text=True,
        )
        assert protection.denies_writes is True
    import json

    result = json.loads(proc.stdout.strip())
    assert all(str(v).startswith("denied") for v in result.values()), result
    assert (gate_root / "receipt.json").read_text() == "original"


def test_observer_signing_material_is_not_on_any_disk_path(tmp_path):
    from skillweave.runtime.worker_launcher import ObserverSigningVault

    vault = ObserverSigningVault()
    signing_root = tmp_path / "observer_signing"
    signing_root.mkdir()
    for probe in vault.read_attempt_paths(signing_root):
        assert not probe.exists()
    # The key is in memory only; signing is deterministic for the same vault.
    assert vault.sign(b"m") == vault.sign(b"m")
    assert vault.key_ref.startswith("vault:")


def test_fail_closed_adapter_refuses_to_launch_or_seal(tmp_path):
    from skillweave.runtime.worker_launcher import (
        FailClosedAdapter,
        IsolationUnavailableError,
        protect_stores,
    )

    protected = _protected_stores(tmp_path)
    with pytest.raises(IsolationUnavailableError):
        with protect_stores(protected, adapter=FailClosedAdapter(reason="test")):
            pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
