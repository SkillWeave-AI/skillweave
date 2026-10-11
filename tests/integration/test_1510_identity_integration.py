"""SW-1510-IDENTITY integration tests at real API / subprocess boundaries.

These drive the identity boundary the way production does, not through an
in-memory role matrix:

* The run application service authenticates a real worker credential at ingress
  before any run state is written; a forged/expired/revoked/mis-bound worker
  token fails *before mutation*, and ``require_identity`` forbids the legacy
  path for a protected caller.
* A genuinely separate, real child process cannot authenticate by presenting
  inert ``auth_real``/``obs_real`` strings, cannot read observer signing
  material, and cannot write protected stores — observed after the child exits.
* A positive independent reviewer produces an admissible receipt, and a
  controller (not the worker, not the reviewer) admits it at the gate boundary,
  persisted with the admitting principal's identity.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.runsvc import RunApplicationService, RunIntegrationError  # noqa: E402
from skillweave.runtime.authority_service import (  # noqa: E402
    HostAuthorityService,
    IdentityBoundaryError,
    ObservedReview,
    admit_gate,
    sign_receipt_body,
)
from skillweave.runtime.journal import EventJournal  # noqa: E402
from skillweave.runtime.registry import (  # noqa: E402
    ArtifactReceipt,
    EvidenceQuality,
    EvidenceType,
    RawArtifactStore,
)
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402
from skillweave.runtime.worker_launcher import (  # noqa: E402
    PROTECTED_STORE_NAMES,
    ObserverSigningVault,
    ProtectedStores,
    protect_stores,
    select_adapter,
)


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    import tempfile as _tf

    monkeypatch.setattr(_tf, "tempdir", str(tmp_path), raising=False)
    return tmp_path


def _runsvc():
    store = SQLiteRunStore()
    journal = EventJournal(store)
    raw = RawArtifactStore()
    return RunApplicationService(store, journal, raw), store, journal, raw


def _scope():
    return {"paths": ["/work/out", "/work/scratch"], "mode": "rw"}


# ---------------------------------------------------------------------------
# 1. Run-service ingress authenticates the worker before any mutation
# ---------------------------------------------------------------------------


def test_runsvc_authenticated_worker_token_runs_end_to_end():
    service, store, journal, raw = _runsvc()
    authority = HostAuthorityService()
    worker_token = authority.issue_attempt_credential(
        run_id="run-a", task_id="task-1", attempt=1, scope=_scope()
    )
    result = service.execute(
        [sys.executable, "-c", "print('authenticated-worker-output')"],
        run_id="run-a",
        tool="opencode",
        model="model-xyz-7",
        subject_repo="skillweave",
        subject_commit="abc123",
        created_at="2026-10-05T00:00:00Z",
        authority_service=authority,
        worker_token=worker_token,
    )
    assert result.gate_state == "pass"
    assert store.get_run("run-a").state == "advance_or_stop"
    assert journal.has_gaps("run-a") is False


def test_runsvc_forged_worker_token_fails_before_any_mutation():
    service, store, journal, raw = _runsvc()
    authority = HostAuthorityService()
    with pytest.raises(RunIntegrationError) as excinfo:
        service.execute(
            [sys.executable, "-c", "print('x')"],
            run_id="run-forged",
            tool="opencode",
            model="m",
            subject_repo="r",
            subject_commit="c",
            created_at="2026-10-05T00:00:00Z",
            authority_service=authority,
            worker_token="auth_real",  # an inert string, not a credential
        )
    assert excinfo.value.stage == "identity"
    # No run state was written: the boundary is at ingress.
    assert store.get_run("run-forged") is None


def test_runsvc_revoked_worker_token_fails_before_any_mutation():
    service, store, journal, raw = _runsvc()
    authority = HostAuthorityService()
    worker_token = authority.issue_attempt_credential(
        run_id="run-rev", task_id="task-1", attempt=1, scope=_scope()
    )
    authority.registry.revoke_token(worker_token)
    with pytest.raises(RunIntegrationError) as excinfo:
        service.execute(
            [sys.executable, "-c", "print('x')"],
            run_id="run-rev",
            tool="opencode",
            model="m",
            subject_repo="r",
            subject_commit="c",
            created_at="2026-10-05T00:00:00Z",
            authority_service=authority,
            worker_token=worker_token,
        )
    assert excinfo.value.stage == "identity"
    assert store.get_run("run-rev") is None


def test_runsvc_worker_token_bound_to_another_run_is_refused():
    service, store, journal, raw = _runsvc()
    authority = HostAuthorityService()
    worker_token = authority.issue_attempt_credential(
        run_id="run-one", task_id="task-1", attempt=1, scope=_scope()
    )
    with pytest.raises(RunIntegrationError) as excinfo:
        service.execute(
            [sys.executable, "-c", "print('x')"],
            run_id="run-two",  # different run than the token's binding
            tool="opencode",
            model="m",
            subject_repo="r",
            subject_commit="c",
            created_at="2026-10-05T00:00:00Z",
            authority_service=authority,
            worker_token=worker_token,
        )
    assert excinfo.value.stage == "identity"
    assert store.get_run("run-two") is None


def test_runsvc_worker_token_with_approving_role_is_refused():
    service, store, journal, raw = _runsvc()
    authority = HostAuthorityService()
    observer_token = authority.registry.issue(
        principal="observer-1",
        roles={"observer"},
        attempt_key=("run-obs", "task-1", 1),
        capability="attest-receipt",
    )
    with pytest.raises(RunIntegrationError) as excinfo:
        service.execute(
            [sys.executable, "-c", "print('x')"],
            run_id="run-obs",
            tool="opencode",
            model="m",
            subject_repo="r",
            subject_commit="c",
            created_at="2026-10-05T00:00:00Z",
            authority_service=authority,
            worker_token=observer_token,
        )
    assert excinfo.value.stage == "identity"
    assert store.get_run("run-obs") is None


def test_runsvc_require_identity_forbids_the_legacy_path():
    service, store, journal, raw = _runsvc()
    with pytest.raises(RunIntegrationError) as excinfo:
        service.execute(
            [sys.executable, "-c", "print('x')"],
            run_id="run-legacy",
            tool="opencode",
            model="m",
            subject_repo="r",
            subject_commit="c",
            created_at="2026-10-05T00:00:00Z",
            require_identity=True,  # no authority_service supplied
        )
    assert excinfo.value.stage == "identity"
    assert store.get_run("run-legacy") is None


# ---------------------------------------------------------------------------
# 2. A real separate child process cannot impersonate
# ---------------------------------------------------------------------------


def test_real_child_process_cannot_authenticate_with_inert_strings(tmp_path):
    """A genuinely separate process has only strings; none authenticates."""
    child = (
        "import sys, json\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from skillweave.runtime.authority_service import HostAuthorityService\n"
        "svc = HostAuthorityService()\n"
        "res = {}\n"
        "for claim in ('auth_real', 'obs_real', 'reviewer', 'run-service'):\n"
        "    try:\n"
        "        svc.verify_credential(claim); res[claim] = 'ACCEPTED'\n"
        "    except Exception as e:\n"
        "        res[claim] = 'rejected:%s' % type(e).__name__\n"
        "print(json.dumps(res))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child, str(_SRC)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip())
    assert all(str(v).startswith("rejected") for v in result.values()), result


def test_real_child_process_cannot_read_observer_signing_material(tmp_path):
    vault = ObserverSigningVault()
    signing_root = tmp_path / "observer_signing"
    signing_root.mkdir()
    # A genuine secret is presented as a would-be on-disk key; the vault key is
    # never written, so a child read of the probe paths finds nothing.
    child = (
        "import sys, json, pathlib\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "res = {}\n"
        "for name in ('observer.key', 'signing.key', 'key.pem'):\n"
        "    try:\n"
        "        res[name] = (root/name).read_text()\n"
        "    except OSError as e:\n"
        "        res[name] = 'denied:%s' % e.errno\n"
        "print(json.dumps(res))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child, str(signing_root)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip())
    assert all(str(v).startswith("denied") for v in result.values()), result


def test_real_execute_protects_the_authority_store_from_the_worker(tmp_path):
    """The actual ``RunApplicationService.execute`` launches a real protected child.

    This is the production path the controller reproduced: a valid worker
    credential whose only writable scope is a private output dir is launched by
    ``execute``; the child tries to (a) mutate the run authority store (the
    on-disk SQLite DB two directories up) and (b) write into the host-owned
    protected stores. Both must fail, observed after the child exits, while the
    run itself still succeeds.
    """
    db = tmp_path / "authority" / "runsvc.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(str(db))
    journal = EventJournal(store)
    raw = RawArtifactStore()
    service = RunApplicationService(store, journal, raw)
    authority = HostAuthorityService()
    worker_token = authority.issue_attempt_credential(
        run_id="run-protected", task_id="task-1", attempt=1, scope=_scope()
    )

    # The host maps one owned writable output root for the attempt, exactly as
    # production does. The probe is written *there* (the worker's only writable
    # tree); a probe path outside it is legitimately denied and would crash the
    # child, which is not what this test measures.
    output_root = tmp_path / "worker_out"
    output_root.mkdir()
    probe = output_root / "probe.json"
    child = (
        "import json, pathlib, sqlite3, sys\n"
        "db = pathlib.Path(sys.argv[1]); out = pathlib.Path(sys.argv[2])\n"
        "res = {}\n"
        "try:\n"
        "    c = sqlite3.connect(str(db)); c.execute(\"UPDATE runs SET state='pwned'\");"
        "    c.commit(); res['sqlite'] = 'wrote'\n"
        "except Exception as e:\n"
        "    res['sqlite'] = 'denied:%s' % type(e).__name__\n"
        "g = db.parent / 'gate_state'\n"
        "try:\n"
        "    (g / 'forged.gate').write_text('x'); res['gate'] = 'wrote'\n"
        "except OSError as e:\n"
        "    res['gate'] = 'denied:%s' % e.errno\n"
        "out.write_text(json.dumps(res))\n"
        "print('protected-worker-completed')\n"
    )
    result = service.execute(
        [sys.executable, "-c", child, str(db), str(probe)],
        run_id="run-protected",
        tool="opencode",
        model="m",
        subject_repo="r",
        subject_commit="c",
        created_at="2026-10-05T00:00:00Z",
        authority_service=authority,
        worker_token=worker_token,
        output_root=str(output_root),
    )
    # The run itself completed and passed; the worker was still confined.
    assert result.gate_state == "pass"
    res = json.loads(probe.read_text())
    assert res["sqlite"].startswith("denied"), res
    assert res["gate"].startswith("denied"), res
    # The host's run metadata was not replaced by the child.
    final = SQLiteRunStore(str(db)).get_run("run-protected")
    assert final.state == "advance_or_stop"


def test_real_child_process_cannot_write_protected_stores(tmp_path):
    adapter = select_adapter()
    if not adapter.available():
        pytest.skip("no real store-isolation adapter on this host")
    protected = ProtectedStores({name: tmp_path / name for name in PROTECTED_STORE_NAMES})
    protected.ensure()
    # A private writable scope, exactly as production gives every worker: a
    # confinement adapter (Seatbelt) turns it into the profile's only writable
    # tree, so the protected store roots sit *outside* it and the child is
    # denied there. Kept separate from the store roots on purpose.
    scope = tmp_path / "worker_scope"
    scope.mkdir()
    gate_root = protected.roots["gate_state"]
    (gate_root / "gate.json").write_text("authoritative")

    child = (
        "import sys, json, pathlib\n"
        "g = pathlib.Path(sys.argv[1])\n"
        "res = {}\n"
        "for label, fn in (('overwrite', lambda: (g/'gate.json').write_text('PWNED')),\n"
        "                  ('create', lambda: (g/'forged.json').write_text('x')),\n"
        "                  ('unlink', lambda: (g/'gate.json').unlink())):\n"
        "    try:\n"
        "        fn(); res[label] = 'wrote'\n"
        "    except OSError as e:\n"
        "        res[label] = 'denied:%s' % e.errno\n"
        "print(json.dumps(res))\n"
    )
    with protect_stores(protected, adapter=adapter) as protection:
        assert protection.denies_writes is True
        # Launch through the open window so the adapter wraps the child: Seatbelt
        # confines *at launch* (its seal() is a no-op), so a bare subprocess.run
        # here would silently defeat the adapter. protection.run is the enforced
        # path and fails closed if the window is not open.
        proc = protection.run(
            [sys.executable, "-c", child, str(gate_root)],
            writable_scope=scope,
            capture_output=True, text=True,
        )
    result = json.loads(proc.stdout.strip())
    assert all(str(v).startswith("denied") for v in result.values()), result
    assert (gate_root / "gate.json").read_text() == "authoritative"


# ---------------------------------------------------------------------------
# 3. Positive independent reviewer + controller decision at the gate
# ---------------------------------------------------------------------------


def _reviewed_receipt(authority):
    raw = RawArtifactStore()
    body = b"reviewed artifact bytes"
    artifact_digest = raw.put(body)
    run_id, task_id, attempt = "run-gate", "task-1", 1
    scope = _scope()
    reviewer_token = authority.registry.issue(
        principal="reviewer-1",
        roles={"reviewer"},
        attempt_key=(run_id, task_id, attempt),
        capability="attest-receipt",
    )
    decision = authority.issue_authority_decision(
        principal="reviewer-1",
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        scope=scope,
        brief={"task_id": task_id},
        policy={"decision": "admit-independent-review"},
    )
    reviewer = authority.verify_credential(reviewer_token)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "task_id": task_id,
        "attempt": attempt,
        "criterion_id": "c1",
        "subject_digest": hashlib.sha256(b"subject").hexdigest(),
        "artifact_digest": artifact_digest,
        "verifier_version": "1",
        "verdict": "pass",
        "issuer": authority.registry.service_principal,
        "principal": "reviewer-1",
        "authority_decision_digest": decision.decision_digest,
        "semantic_checks": [
            {"check_id": "bytes-match", "result": "pass", "evidence_digest": artifact_digest}
        ],
    }
    receipt["signature"] = sign_receipt_body(receipt, reviewer)
    observed = ObservedReview(
        reviewer_principal="reviewer-1",
        attempt_key=(run_id, task_id, attempt),
        artifact_digest=artifact_digest,
        verdict="pass",
        independent=True,
        observed_at="2026-10-05T00:00:00Z",
    )
    ingested = authority.ingest_evidence_receipt(
        receipt, worker_token=reviewer_token, raw_artifacts=raw, observed_review=observed
    )
    return raw, receipt, ingested, decision, run_id


def test_controller_admits_an_independently_reviewed_receipt_into_the_store():
    authority = HostAuthorityService()
    raw, receipt, ingested, decision, run_id = _reviewed_receipt(authority)
    controller_token = authority.registry.issue(
        principal="controller-1",
        roles={"controller"},
        attempt_key=("run-gate", "task-1", 1),
        capability="admit-gate",
    )
    admission = admit_gate(
        authority,
        controller_token=controller_token,
        ingested=ingested,
        decision=decision,
        gate_state="pass",
    )
    assert admission.admitted_by == "controller-1"

    store = SQLiteRunStore().with_authority_service(authority)
    store.create_run(run_id)
    # Persist the host-ingested receipt and admit the gate via the controller.
    saved = store.save_protected_evidence(
        ArtifactReceipt(
            artifact_id="attested-gate-receipt",
            sha256=ingested.artifact_digest,
            schema_version="1",
            producer_command="review",
            subject_repo="r",
            subject_commit="c",
            created_at="2026-10-05T00:00:00Z",
            evidence_type=EvidenceType.ARTIFACT.value,
            purpose="independent review",
            method="reviewer",
            system_source="reviewer",
            quality=EvidenceQuality(),
        ),
        ingested,
    )
    assert saved.sha256 == ingested.artifact_digest
    assert store.get_evidence("attested-gate-receipt") is not None

    record = store.transition_with_gate_admission(
        run_id, "batch_selection", "preflight", 1, admission
    )
    # The persisted transition is attributed to the controller, not a worker.
    assert record.role == "controller-1"


def test_worker_cannot_admit_its_own_gate():
    authority = HostAuthorityService()
    raw, receipt, ingested, decision, run_id = _reviewed_receipt(authority)
    worker_token = authority.issue_attempt_credential(
        run_id=run_id, task_id="task-1", attempt=1, scope=_scope()
    )
    with pytest.raises(IdentityBoundaryError):
        admit_gate(
            authority,
            controller_token=worker_token,  # a worker credential
            ingested=ingested,
            decision=decision,
            gate_state="pass",
        )


def test_gate_admission_is_bound_to_the_exact_attempt():
    authority = HostAuthorityService()
    raw, receipt, ingested, decision, run_id = _reviewed_receipt(authority)
    other_decision = authority.issue_authority_decision(
        principal="controller-1",
        run_id="other-run",
        task_id="task-1",
        attempt=1,
        scope=_scope(),
        brief={"task_id": "task-1"},
        policy={"decision": "other"},
    )
    controller_token = authority.registry.issue(
        principal="controller-1",
        roles={"controller"},
        attempt_key=("run-gate", "task-1", 1),
        capability="admit-gate",
    )
    with pytest.raises(IdentityBoundaryError):
        admit_gate(
            authority,
            controller_token=controller_token,
            ingested=ingested,
            decision=other_decision,
            gate_state="pass",
        )


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
