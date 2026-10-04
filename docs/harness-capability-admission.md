# Harness capability admission (native-tool)

This document describes the **native capability admission gate** at the harness
execution boundary: how a plan that needs native skill execution (Skill
expansion plus Agent spawning) is admitted against a *measured* capability
handshake, and why everything else fails closed.

The wire contract is the frozen `capability-snapshot-v1` schema, owned by the
SkillWeave SDK (SW-1510-CONTRACT). This gate **consumes** that schema; it does
not define a second one. What the gate adds is the *admission semantics* the
schema cannot express: a signature is not a measurement, a declaration is not a
detection, and a detection is not a run-attested expansion.

## Where the gate lives

- Implementation: `src/skillweave/routing/harness_adapter.py`
  - `NativeRequirements`, `HostBinding`, `CapabilityAdmission`
  - `admit_native_plan(plan, *, capability_snapshot, native_requirements, host_binding)`
  - `HarnessAdapter.execute(..., capability_snapshot=, native_requirements=, host_binding=)`
- Vocabulary shared (one-way) with dispatch:
  `src/skillweave/dispatch/harness_contract.py`
- The frozen schema:
  `integrations/skillweave-sdk/src/skillweave_sdk/schemas/execution/capability-snapshot-v1.schema.json`

The gate is deliberately generic: it names no concrete harness. Every native
fact it reads is a data record, never a branch in core routing.

## The native-tool execution boundary

`HarnessAdapter.execute` refuses before any work when the plan requires the
`native-tool` capability and the measured handshake is absent, stale,
contradictory or configuration-mismatched. Concretely, `admit_native_plan` runs
**before** `build_command` and before the injected `launch` seam is called, so a
refusal performs **zero work**: no argv is built, no process is spawned, no
provider request is made. The refusal is an attributable `HarnessAdapterError`
carrying the `asset` that decided it.

A plan that does **not** require `native-tool` keeps the ordinary raw-command
compatibility. That path is explicitly *not* advertised as native skill
execution, and admission on it never promotes a declared fact to an attested
fact.

### The three mandatory inputs

A native plan must supply all three; a missing one is a typed refusal, never a
quiet legacy fallback:

| Input | Meaning |
| --- | --- |
| `capability_snapshot` | The measured snapshot (frozen v1 shape). |
| `native_requirements` | What the plan needs: `skill_name`, `skill_digest`, `required_tools`, `required_stages`. |
| `host_binding` | The host's *detected* configuration, reconciled against reality. |

## What the snapshot conveys, and how it is graded

The snapshot's `capabilities` object is read as an open data record with four
distinct parts. The gate keeps them distinct — it never collapses one into
another:

- **`declared`** — what something advertises. Weakest.
- **`detected`** — what was observed to be present (for example a coordinator's
  or a child worker's effective tool list).
- **`run_attested`** — evidence produced by actually running work. Still split
  into *weak* and *bound* facts (below).
- **`host_binding`** — the host configuration the measurement was taken on.

### Weak evidence is kept visible, never promoted

Installation, a model catalog, `/v1/models` visibility, coordinator `system`
`init` tool lists, role strings and copied init events prove that a *surface
exists*. They do **not** prove a skill expanded or a workflow stage ran. The
gate records these facts under `observed` (for example
`weak_evidence_present`) so an audit can see they were consumed — and refuses on
them.

A self-asserted `authenticated=true` flag is likewise never consulted as a fact.
It is retained in `observed` only to keep it visible.

### Bound proof is required for expansion and stages

Native expansion and each required workflow stage need a **bound** observation:
a record produced by the measuring boundary that names

- the `skill_digest` it observed,
- the selected `runtime_config_digest`, and
- an `observer` identity,

under `run_attested.native_expansion` (expansion) or
`run_attested.workflow_stages.<stage>`. A bare string claiming a stage happened,
a `system_init` event, or an `installed`/`models_visible` flag is **not** bound
evidence. An observation bound to a *different* skill digest is not evidence for
this plan's skill.

## The child worker, not the coordinator, is the surface

The plan runs on the selected **child-worker** configuration. Its effective
tool list must hold every required tool plus the workflow tools (`Agent`,
`Skill`). A coordinator's tool list never stands in for the child's: a bare
worker with only `Read`/`Bash`/`Edit` cannot execute an Understand-style plan
requiring `Agent`/`Skill`, however loudly the declaration or the coordinator
advertises them.

Both surfaces stay visible in the admission record (`child_tools`,
`coordinator_tools`, `missing_child_tools`), so a reviewer can see exactly why a
plan was refused.

## `host_binding`: measured, not asserted

The host binding is the host's *detected* configuration: the executable path,
its sha256 digest and version; the plugin path and digest; the coordinator and
child modes and effective tools; the exact model; and a digest of the selected
runtime configuration. The gate reconciles the live binding against both the
snapshot's binding and **reality**:

- Paths, version, mode and model must agree field-for-field.
- The executable's **real bytes** are hashed at admission. A binary replaced
  after planning no longer matches its pin and fails closed.
- The plugin's real bytes are hashed the same way.
- The selected configuration is reconciled by digest and by content: a
  configuration mutated after measurement is a contradiction.

A binding that appears only inside the snapshot, never observed on the host,
fails closed.

## Temporal and shape checks

- **Schema version**: only the frozen `"1.0.0"` is consumed. Anything else fails
  closed; the gate never best-effort parses a future or unspecified wire
  contract.
- **Validity window**: a snapshot missing `measured_at`/`expires_at`, measured
  in the future, already expired, or whose `expires_at` precedes `measured_at`
  fails closed. Unknown capacity is never treated as unlimited.
- **Subject and issuer**: the snapshot must be *about this plan's subject* and
  carry a non-empty issuer. An absent/whitespace issuer is not a measurement;
  a snapshot about a different host is not this plan's.
- **Revision**: retained for audit but **advisory**. A stale snapshot that
  merely bumps `revision` while the measured binary/plugin/config is unchanged
  must not read as a fresh measurement, so `revision` carries no admission
  weight.

## Honest limits: authentication is a separate, mandatory gate

This gate proves **capability measurement and admission**. It does **not**
establish that a snapshot's *issuer* is a trusted principal: `issuer` is checked
for presence and `subject` for agreement, but the gate performs **no
cryptographic verification** and no OS isolation of the measuring boundary.

A model-authored snapshot is therefore **not** trustworthy on this gate's
authority alone. Binding a snapshot to an authenticated measuring service — a
trusted-principal / OS-isolation **IDENTITY + MCP-AUTH** gate — is a separate,
still-mandatory gate. Until that gate exists, treat every snapshot as carrying
*measured claims under an unidentified issuer*, not as authenticated evidence.

## Verification

- `tests/unit/test_1510_cap.py` — the admission semantics: presence, version,
  temporal, subject/issuer, child-vs-coordinator, weak-vs-bound evidence,
  semantic stage mismatch, measured binary/plugin replacement **after** planning,
  and one genuine measured positive fixture. Every case drives the real
  `execute` seam with an injected launch counter, so "zero work on rejection" is
  measured (`launches == 0`), not asserted by wording.
- `tests/contract/test_1510_cap_schema.py` — binds the gate to the frozen SDK
  schema: the declared version equals the schema `const`, a canonical snapshot
  validates, and the gate's fail-closed checks land on the schema's required
  top-level keys.
- `tests/integration/test_harness_adapter.py` — the surrounding adapter suite,
  including the lockstep vocabulary and eager-closure (layering) checks.
