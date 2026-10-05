# Execution Identity Boundary (SW-1510-IDENTITY)

This document is the operator-facing description of the host-owned
authority/identity boundary that separates **worker**, **verifier**,
**observer**, **reviewer** and **controller** authority in the run path. It
explains what is enforced, where, how to operate it, and its honest
limitations.

## Why it exists

A delegated worker/harness runs untrusted model output. Before SW-1510-IDENTITY,
an approval could be expressed as a *string* inside a record
(`actor="auth_real"`, `role="reviewer"`) and the completion path would accept
it. Nothing tied that string to a real, authenticated principal, and nothing
stopped a worker from writing the terminal receipt or gate state itself.

The SDK (`skillweave_sdk`) freezes the execution wire contracts
(`evidence-receipt-v1`, `run-snapshot-v1`, …) and supplies *pure* authority
verifiers (`verify_evidence_receipt_authority`, …). The SDK documents explicitly
that it **cannot authenticate**: `AuthenticatedServiceContext` is a shape guard,
and the verifier trusts the context it is handed. The runtime that owns the seam
must build that context from a real verification result. That runtime seam is
`skillweave.runtime.authority_service.HostAuthorityService`.

## The trust model

The adversary is a **delegated worker/harness** (and any caller of the protected
API), not a malicious host administrator. A host process that can read the
service's memory or edit its credential registry can defeat any in-process
boundary; that is out of scope by design.

### The six roles

| Role | May do | May NOT do |
|---|---|---|
| worker | execute its attempt, write source/scratch scope | write receipts/gate/authority/observer/telemetry; approve anything |
| reviewer / observer | independently review and attest a receipt | admit gate state; approve itself |
| controller / integrator | admit task/dependency state at a gate | attest a receipt as reviewer |

Worker is deliberately **not** an issuable approving role: the service will
never issue a credential with an approving role to a worker, and a worker
credential carries no approving role at all.

## What is enforced, and where

### 1. Credentials are host-issued, opaque, and verified constant-time

`HostAuthorityService` owns a `CredentialRegistry`. The only way to turn a
bearer token into an authenticated `VerifiedCredential` is
`verify_credential(token)`. The registry stores only a SHA-256 **digest** of
each high-entropy token and compares digests in constant time
(`hmac.compare_digest`). There is **no API that accepts an inert string such as
`auth_real` or `obs_real` as proof of anything** — those strings simply are not
in the registry and fail closed.

`VerifiedCredential` fields come only from the registry lookup, never from
caller-supplied JSON.

### 2. Attempt identities, scope bindings and policy decisions are service-issued

* `issue_attempt_credential(run_id, task_id, attempt, scope)` mints the worker a
  non-approving credential bound to the exact `(run_id, task_id, attempt)` and
  to a canonical **scope digest**. The worker principal is
  `worker:<run>:<task>:<attempt>`.
* `issue_authority_decision(...)` admits a scope-bound decision
  (principal, attempt, brief/scope/policy digests). Only the returned digest is
  presented; the binding facts live in the registry, never in a worker-visible
  record.

### 3. Receipt ingestion is the actual boundary

`ingest_evidence_receipt(receipt, worker_token=..., raw_artifacts=...,
observed_review=...)` is where a wire receipt becomes admissible evidence. In
order:

1. Authenticate the **presenting** credential (a reviewer/observer with
   capability `attest-receipt`, bound to an attempt). A missing/forged/expired/
   revoked token, or a token for a different attempt, fails here.
2. Build the SDK trusted context **strictly** from that verification result and
   call `verify_evidence_receipt_authority` — pure authority, no semantic truth
   yet.
3. Apply the semantic artifact contract a signature cannot supply:
   * the receipt's `run_id`/`task_id`/`attempt` must equal the authenticated
     attempt;
   * the `issuer` must be the authenticated host service principal;
   * the `principal` must not be the worker principal (no self-attestation);
   * the HMAC signature must verify over the exact receipt body;
   * the `artifact_digest` must resolve to real raw bytes in `RawArtifactStore`
     whose content hashes to that digest (a signed PASS with missing or mutated
     bytes fails);
   * an **actually executed, independent** observed review must back the exact
     artifact and attempt (a signed string claiming a review is not enough);
   * `semantic_checks` must be present and all passing;
   * when the host knows a candidate SHA map, the receipt's complete map must
     match it exactly (stale/substituted maps are refused).

`sign_receipt_body` is an HMAC keyed by a **per-service in-memory signing
secret** generated at registry construction (`secrets.token_bytes(32)`). The
secret is never written to a wire record, an issued token, or any disk path, and
it is attached only to a host-verified **review-capable** credential — a worker's
own token verifies through the same path but carries no signing authority
(`signing_secret == b""`). Because the key is neither public nor reachable by a
delegated worker, a worker cannot reproduce a valid signature, and a
worker-produced fake receipt fails its signature step even before the semantic
checks run (the signer fails closed if a credential carries no secret).

### 4. Protected store writes require a host-issued token/type

`SQLiteRunStore.save_protected_evidence(receipt, ingested)` refuses anything
that is not an `IngestedReceipt` produced by the service, and refuses a receipt
whose digest is not the authenticated one. `transition_with_gate_admission(...)`
requires a `GateAdmission` from a controller/integrator credential and records
the transition with the **admitting principal** as the role — never a
worker-supplied role string. The legacy `save_evidence`/`transition` remain
unchanged for backward compatibility.

### 5. Gate admission is controller-only

`admit_gate(service, controller_token=..., ingested=..., decision=...,
gate_state=...)` requires a controller/integrator credential with capability
`admit-gate`, a decision the service actually issued, and a decision bound to
the **exact** attempt of the ingested receipt. A worker credential can never
reach here.

## Process isolation of the worker

Writing protected stores is also blocked at the OS boundary so a same-user child
process cannot simply write them. An in-memory role matrix, or a `chmod`
against a same-user child (an owner can just chmod it back), is not enough.

`skillweave.runtime.worker_launcher` models two **real** mechanisms and
**fails closed** when neither is available:

* **`SandboxExecAdapter`** — macOS Seatbelt (`sandbox-exec`). Denies both reads
  of observer signing material and writes to every protected store. Selected
  when the host permits Seatbelt; `available()` is a real probe, not a platform
  guess.
* **`ImmutableStoreAdapter`** — the BSD/Mach immutable flag (`chflags uchg`) on
  each protected store for the lifetime of the child. This is kernel-enforced:
  a same-user child's create/overwrite/unlink inside a sealed store fails with
  `EPERM`, independently observable after the child exits. It does **not** deny
  reads, and advertises `denies_reads = False` honestly.

Observer signing material is read-protected **by construction**: it lives only
in an in-memory `ObserverSigningVault` (32-byte key, never written to any path),
so there is no plaintext file a child can open. `read_attempt_paths()` lists the
paths a worker might guess; none exists.

`protect_stores(...)` opens a `StoreProtection` window. If no real adapter is
available, `seal()` raises `IsolationUnavailableError` — the protected native
path **fails closed with an honest capability limitation rather than launching
an unsandboxed worker**. `require_read_isolation=True` additionally refuses an
adapter that does not deny reads.

### Operating it

```python
from skillweave.runtime.worker_launcher import (
    PROTECTED_STORE_NAMES, ProtectedStores, protect_stores,
)

protected = ProtectedStores({name: store_root / name for name in PROTECTED_STORE_NAMES})
protected.ensure()

with protect_stores(protected) as protection:   # raises if no adapter is available
    argv = protection.wrap_command(worker_argv, writable_scope=scope_dir)
    run(argv)                                    # the worker subprocess
# stores are unsealed on exit
```

`protection.denies_writes` / `denies_reads` report the **observed** capabilities
of the chosen adapter. Do not assume read isolation away: assert on it.

## Integrating with the run application service

`RunApplicationService.execute(...)` accepts the host authority seam directly:

```python
service.execute(
    command,
    run_id=run_id, tool=tool, model=model,
    subject_repo=repo, subject_commit=sha,
    authority_service=host_authority,   # authenticate the worker at ingress
    worker_token=worker_token,
)
```

When `authority_service` is supplied, the host authenticates the worker at
ingress: the token must verify against the registry, bind to this exact
`run_id`, and carry no approving role. **Without** an `authority_service` the
path is the unchanged legacy path — except that a protected caller can pass
`require_identity=True`, which makes a missing authority service a hard failure
so the legacy path cannot be reached by accident.

## Backward compatibility

`skillweave.runtime.authority.AuthorityGuard` / `ROLE_CAPABILITY_MATRIX` and
`skillweave.gates.attestation` are preserved **unchanged** as *descriptive
policy helpers*. They remain useful for describing intended policy, but they are
**not identity proof**: an approval with `actor="auth_real"` is an inert data
record and grants the production boundary nothing. Do not treat a passing
`AuthorityGuard` check as authentication.

## Honest limitations

* **Adversary scope.** This boundary defends against a delegated worker/harness
  and callers of the protected API. It does not defend against a host
  administrator who can read the service's memory or script its registry.
* **Read isolation.** On hosts where Seatbelt is unavailable, only *write*
  isolation is enforced at the OS boundary; observer signing material is
  read-protected by being memory-only rather than by a filesystem denial. The
  adapter reports this honestly (`denies_reads = False`) and
  `require_read_isolation=True` fails closed instead of pretending.
* **Fail-closed.** Where no real isolation mechanism exists, the protected path
  refuses to start rather than weakening containment. This is a deliberate
  availability trade: a protected worker that cannot be isolated does not run.
* **File modes.** Directory `chmod` on protected roots is a same-user
  convenience layer only; the isolation guarantee comes from the adapter, never
  from the mode.
