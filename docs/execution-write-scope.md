# Execution Write Scope and Isolation (SW-1510-SCOPE)

This document is the operator-facing description of the **execution
enforcement** half of the WorkContract boundary: where a delegated worker may
write, how that is enforced at the real subprocess boundary, what filesystem
isolation the harness actually attests, and how mutation/turn-limit changes are
admitted. It complements `docs/execution-identity-boundary.md` (who a worker
*is*); this document is about what a worker may *touch*.

## Why it exists

A WorkContract carries a normalized write scope, but a scope that is only
lexically validated before launch is not an execution boundary. Two escapes
defeat a purely lexical check:

* a raw `..` segment (`attempt/../../run.db`) that climbs out of the checkout;
* a symlink inside the checkout whose *target* lies outside it
  (`attempt/link -> /etc/passwd`), which a lexical check never follows.

And an in-process role matrix is not isolation: a same-user child can write any
file the host can. The boundary therefore has two halves that must both be
real — an **execution write scope** resolved against the checkout, and a **real
OS isolation window** that seals the protected stores for the lifetime of the
child, failing closed when no such mechanism exists.

## The boundary

Three modules cooperate:

| Module | Role |
|---|---|
| `skillweave.runtime.write_scope` | normalized scope + realpath containment resolution (`ExecutionScope.check`, `resolve_under`, `resolve_real_path`) and the generic object scopes |
| `skillweave.core.proc.runner` | the actual launch boundary: `ScopeEnforcement` authorizes declared writes and `run_process(..., enforcement=...)` attests isolation before spawning |
| `skillweave.runsvc.service` | the six-stage integration path that composes the two at `RunApplicationService.execute` |

At the service seam, a scope-enforced launch requires an **authenticated
worker** (`authority_service` + `worker_token`); naming `declared_writes` or a
`work_contract` without one is a hard `identity` failure, so scope enforcement
can never be silently downgraded to the legacy unprotected path.

For every declared output, before the child is spawned:

1. the target is `realpath`-resolved (symlinks followed);
2. a target under any receipt/store/telemetry root is refused even when it
   would otherwise be inside the checkout;
3. a raw parent-directory traversal is refused before resolution;
4. the normalized `ExecutionScope.check` decides; a target that escapes the
   checkout — traversal, symlinked escape, or an explicit deny — is refused.

A refusal is the typed `ScopeViolation` (see `to_dict()`), raised in the
service before the process exists. The child is launched with the
adapter-wrapped argv inside the same attested window, so every tool — including
Bash children — runs under the same isolation.

## Real isolation and its honest limits

`skillweave.runtime.worker_launcher` supplies the real OS mechanisms:

* `SandboxExecAdapter` — macOS Seatbelt (`sandbox-exec`); denies writes and
  reads and *confines a process to the writable scope*.
* `ImmutableStoreAdapter` — the BSD/Mach immutable flag (`chflags uchg`); the
  kernel rejects create/overwrite/unlink inside a sealed store from any
  process, including a same-user child. It does **not** deny reads, and it seals
  the protected stores but does **not** confine a process to an arbitrary
  outside path; it reports `confines_to_scope = False` honestly.
* `FailClosedAdapter` — used when neither is available; `__enter__` raises.

`ScopeEnforcement.attest()` records the adapter actually in force
(`IsolationAttestation`): the mechanism, its proven capabilities, the concrete
protected roots it seals, and that all spawned tools share the window.
`require_checkout_confinement=True` refuses an adapter that only seals stores
(a typed `ScopeEnforcementUnavailable` / HOLD). When no real adapter is
available the launch fails closed — the runner raises the typed HOLD and the
identity layer refuses the unisolated child — never a quiet unsandboxed run.

## Mutation and turn-limit admission

`skillweave.runtime.assay.AssayEngine`, when given an `actor_resolver`, checks
every mutation and every turn-limit change on four axes before it takes effect:
the **actor** authenticates (a token verified by the resolver, never a
caller-supplied string), the pinned **subject/brief** are unchanged, the
**deadline** has not passed, and the **remaining budget** is not exhausted.

* A negative `max_turns` or `remaining_budget` is always refused.
* `max_turns=None` is the explicit, owner-authorized *no-cumulative-limit*
  envelope; it is honoured and is never replaced by an artificial default.
* A finite envelope's deadline and reservation are enforced.
* `set_turn_limit` records the change with the authenticated actor and a
  `kind` of `extension` or `no_cumulative_limit`.

## Generic object scopes (no fabricated SHAs)

The same contract model carries Git and non-Git subjects. A
content/configuration/deployment/incident scope is validated against the fields
its own kind carries (`ContentSubject` channel/content_id/revision, etc.) —
never by inventing a repository commit. `ExecutionScope.validate_object()`
refuses a missing field and refuses a `repository_commit` that is not a real
full 40-hex commit; `scope_from_work_contract` leaves `repository_commit=None`
for a non-Git subject.

## Operating it

```python
service.execute(
    command,
    run_id=run_id,
    tool=tool,
    model=model,
    subject_repo=repo,
    subject_commit=commit,
    authority_service=authority,     # authenticated worker at ingress
    worker_token=token,
    declared_writes=["src/pkg/mod.py"],   # and/or work_contract=contract
)
```

Refusals to handle:

* `ScopeViolation.to_dict()` — `error: "scope_violation"` with the raw target
  and resolved path (traversal/symlink/protected-store escape).
* `ScopeEnforcementUnavailable.to_hold()` — `kind: "HOLD"`,
  `reason: "isolation_unavailable"` (missing or insufficient isolation).
* `WriteScopeViolation` / `ObjectScopeBindingError` — a raw traversal segment or
  an object scope that does not bind its own subject.

## Limitations

* The adversary is a delegated worker, not a host administrator: a process that
  can read the host's memory or edit the sealed directories before sealing is
  out of scope by design.
* When only the immutable-flag adapter is available (e.g. `sandbox-exec` is
  blocked), write isolation is kernel-enforced against the protected stores but
  the adapter does **not** confine the child to the checkout tree; a caller
  that must guarantee "no writes outside the checkout anywhere" sets
  `require_checkout_confinement=True` and is refused (HOLD) on that host rather
  than given a weaker guarantee.
* Reads of observer signing material are protected by construction (the
  `ObserverSigningVault` keeps the key only in host memory) plus, where
  available, a read-denying sandbox; write-only isolation does not itself deny
  reads.
