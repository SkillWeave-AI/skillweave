# Architecture

SkillWeave 1.3.7 — self-hosting, multi-lane runtime.

This document describes the canonical run path and the skill layer as they are
in this release. It is a pointer, not evidence: every claim here is backed by a
machine check (see `tests/unit/test_doc_arch.py`), which scans this file for
stale statements and verifies the diagram matches the call graph.

## Canonical run path

A run flows through one authoritative integration path, never a hand-rolled
sequence of primitive calls:

```
  Run → Journal → Raw Artifact → Receipt → Verification → Gate
```

Each stage is a distinct, addressable record kind, and none is synthesized:

1. **Run** — a persisted `RunRecord` in the CAS-backed run store, with a
   compare-and-set version so two writers on the same version cannot both win.
2. **Journal** — ordered, gap-free journal events bound to the run.
3. **Raw Artifact** — content-addressed worker bytes, resolvable back to exact
   bytes.
4. **Receipt** — the `ArtifactReceipt` bound to the run and the raw digest.
5. **Verification** — a separate verifier's verdict, provenance-bound to the
   subject receipt (never the producer's self-claim).
6. **Gate** — the completion-contract state derived from the verified outcome;
   exit 0 with empty output is `inconclusive`, never a gate pass.

The Run Application Service (`skillweave/runsvc`) is the single seam a caller
drives end to end. It does not import or invoke any simulated executor: the
canonical path runs real subprocesses, and any `simulate_*` placeholder is
quarantined behind `skillweave/legacy`.

## Council model namespace

The Council's `ROUTER_PROFILES` names **provider-native** Faidate roster ids
directly — real `/v1/models` ids, never gateway-qualified and never symbolic seat
aliases. Faigate's live roster self-answers only `deepseek-v4-pro` and
`deepseek-v4-flash`; every other id it serves silently collapses onto
`deepseek-v4-flash`, so the presets name those two ids directly to hold the
`>=2` distinct answering-model gate. The `faigate/` gateway prefix belongs to the
dispatch layer only and is translated exactly once at the adapter boundary
(`translate_model_id`); a prefix in Council profile data is refused before any
provider call. Each seat records requested / resolved / answering model, status,
provider and profile revision — the answering model is read from the response
envelope, never inferred from the request. Fewer than `min_models_required`
distinct answering models is a degraded run, never consensus.

## Multi-lane control plane

The control plane is a set of cooperating written surfaces, each with a single
owner:

* **Coordinator** (`skillweave/coordinator`) — the sole writer of the root DAG
  cursor. Workers and reviewers may read it but cannot mutate it; a fresh
  coordinator resumes the persisted cursor.
* **Workspace** (`skillweave/workspace`) — exclusive worktrees/branches
  materialised from a full base SHA, with attestation and deterministic cleanup.
* **Fan-out** (`skillweave/fanout`) — dependency-ready fan-out: independent
  workers start before any is reaped, so overlap is a measured fact.
* **Review** (`skillweave/review`) — a review child-run starts only after
  push/fetch against a pinned full remote SHA; a SHA mismatch or a write attempt
  blocks it before it starts.
* **Self-hosting** (`skillweave/selfhost`) — SkillWeave drives its own small
  sequence (two ops lanes, two reviews, one dependent lane) with no manual
  worktree or session control.

## Execution flow

```
 Blueprint → Generate → Validate → Execute → ReleaseChain
     ↓          ↓          ↓          ↓          ↓
   PRD      Sequence   Validated   Executed    Production
            with type   sequence    results    ready code
            & mode
```

## Skill layer

Fifteen skills ship as `skillweave-*` packages:

1. `skillweave-blueprint`
2. `skillweave-council`
3. `skillweave-design`
4. `skillweave-discovery`
5. `skillweave-entry`
6. `skillweave-launch`
7. `skillweave-lifecycle`
8. `skillweave-observe`
9. `skillweave-onboarding`
10. `skillweave-post-release`
11. `skillweave-promptchain-execute`
12. `skillweave-promptchain-generate`
13. `skillweave-promptchain-validate`
14. `skillweave-releasechain`
15. `skillweave-repo-health`

## Repository boundary

SkillWeave is four repositories with one owner per concern. This document's
claim is a pointer: the boundary is enforced, not merely described, by
`tests/contract/test_preview_core_pro_boundary.py`.

| Repo | Owns | Licence | Public |
|------|------|---------|--------|
| `skillweave-sdk` | the **contract** — schema bytes and the taxonomy value set | Apache-2.0 | yes |
| `skillweave` | the **execution** — runtime, kernel, engine (this repo) | Apache-2.0 | yes |
| `skillweave-profiles` | the **meaning** — provider-free profile opinion | Apache-2.0 | yes |
| `skillweave-packs-pro` | **commercial opinion** — CMS behaviour and provider mappings | proprietary | no |

The rules the machine checks:

* **SDK owns the contract bytes.** Core consumes a pinned SDK version
  (`skillweave-sdk==0.1.0` in `pyproject.toml`) and never re-authors the
  lifecycle/work-profile/deliverable/evidence/subject schemas or a pack schema
  in its own tree.
* **Base profiles are provider-free.** `skillweave-profiles` encodes no concrete
  provider, model, harness or launch command. Core's own `profiles/` holds a
  worked example, not a shipped provider-free base profile.
* **A private pack cannot weaken Core authority.** Core owns the gate decision
  and the authority matrix; a pack may add stricter gates but cannot override a
  Core evidence verdict, gate decision or authority statement. The gate
  reconciliation result exposes no override/force-pass field, and too little
  evidence is `reconciled=False`, never a pass.
* **Private direct-install metadata is declarative, not state.** A
  `skillweave-packs-pro` manifest declares proprietary `license`,
  `compatibility`, `upgrade`, `support` and `service_tier` identifiers.
  Secrets, billing and entitlement state remain external and appear as no field
  of a verification record.
* **No premature 2.0 marketplace.** A 1.3.12 artefact claims no marketplace
  discovery, verified badge, rev-share, license enforcement or default routing
  cutover; those are deferred to 2.0 and named only on the roadmap as future
  work.

## Agent-agnostic design

* **Capability-based routing**: tasks are assigned by declared capability, never
  by a hard-coded harness name or a name-prefix rule.
* **Read-only review**: the reviewer role is technically read-only; mutation
  attempts are blocked before execution.
* **Model independence is not claimed**: Ops and Review run under one routed
  model; the split is a cost/routing choice, not an independence statement.
