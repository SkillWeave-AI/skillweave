# Host support matrix

SkillWeave records four distinct, independently evidenced facts about a host
(harness / agent). They are deliberately *not* one axis: a harness can be
installed here, documented there, proven to dispatch on one machine, and used in
production on another. This document names each row, its exact evidence anchor,
and the claim that anchor is and is not allowed to support.

A claim may only cite the anchor in its own column. "Installed" never means
"dispatch-proven", and a documented name is never a production profile.

## The four levels

| Level | Meaning | Evidence anchor | What it is |
|-------|---------|-----------------|------------|
| **installed** | The installer has a target path for this agent and copies/symlinks skills there. | `src/skillweave/installer.py` → `AGENT_CONFIG` (9 targets) and `CAPACIUM_MANAGED_AGENTS` (3 capacium-managed) | A write destination, not a run record. |
| **documented** | A doc names the host and states how its operator would set `SKILLWEAVE_HARNESS`. | `docs/dispatching-from-your-harness.md` (four harness names: `opencode`, `claude-code`, `codex`, `antigravity`) | A declaration mechanism, not a proof. |
| **dispatch-proven** | A real run originated *from* that harness and terminated cleanly, recorded by the seam. | `docs/dispatch-2-report.json` (`proven: true`, `harness: opencode`) | Exactly one run on one machine; per-harness, per-machine. |
| **production** | A deployment profile an operator actually runs. | None shipped. `profiles/example-standard.yaml` is an EXAMPLE to copy/adapt (`name: example-standard`), never auto-loaded. | Operator-owned; ship nothing that claims it. |

## Matrix of hosts

| Host | installed | documented | dispatch-proven | production |
|------|-----------|------------|-----------------|------------|
| `opencode` | yes (capacium-managed) | yes | yes (dispatch 2, one machine) | no published profile |
| `claude-code` | yes (capacium-managed) | yes | no (`UNPROVEN`) | no published profile |
| `codex` | yes (bridge) | yes | no (`UNPROVEN`) | no published profile |
| `antigravity` | yes (bridge) | yes | no (`UNPROVEN`) | no published profile |
| `gemini-cli` | yes (capacium-managed) | no | no | no published profile |
| `openclaw` | yes (bridge) | no | no | no published profile |
| `aider` | yes (bridge) | no | no | no published profile |
| `windsurf` | yes (bridge) | no | no | no published profile |
| `qwen` | yes (bridge) | no | no | no published profile |

Install targets come from `AGENT_CONFIG`; the four documented harness names come
from `docs/dispatching-from-your-harness.md`. The two lists overlap in four names
and diverge elsewhere — that divergence is the point: installation and the
harness-declaration seam are different surfaces.

A dispatch-proven cell is only ever true for the specific harness **and the
specific machine/caller** named in its report. `docs/dispatch-2-report.json`
records `/opt/homebrew/bin/opencode`; that absolute path is one machine's and is
explicitly *not* portable evidence for any other host.

## The OpenCode example is not general syntax

The dispatch that proved `opencode` did so with a specific launch command:

```
opencode run --model faigate/deepseek-v4-pro -
```

That string is the launch command of one profile, on one machine, with one model
id. It is recorded in three places, each disclaiming general status:

- `profiles/example-standard.yaml` — a worked EXAMPLE (`name: example-standard`),
  whose header states the command "assumes `opencode` is on PATH, and the model id
  is the one this example was proven against."
- `docs/dispatch-2-report.json` — an evidence record whose `_note` says it is "a
  verbatim record of one run on one machine, kept as evidence rather than as
  guidance."
- `docs/dispatching-from-your-harness.md` — the `opencode` row states the path and
  model id "are one machine's; yours will differ."

No SkillWeave document presents `opencode run --model … -` as the general way to
launch a role. The general contract is `skillweave.routing.harness`: a profile
declares an arbitrary `launch_command`, and the seam runs whatever that profile
declares. The OpenCode string above is *data* in the example profile, not syntax
of the product.

## Repository names and the mirror destination

Two names are fixed and must not be rewritten by a mirror:

| Role | Name |
|------|------|
| Canonical release repository (Forgejo) | `skillweave/skillweave` |
| Distribution mirror (GitHub) | `LangeVC/skillweave` |
| Authorized mirror destination (org) | `SkillWeave-AI` |

These are the published names; the mirror preserves them (see
`docs/release-flow.md` and `.ops.yaml`). There is **exactly one** mirror owner —
this repository's release workflow — and **no second mirror owner** is
introduced here. The `SkillWeave-AI` destination is recorded, not acted on:
**no transfer is initiated** by this document or by this ticket.

## Composition status of the DOC-TRUTH / MIRROR-CONFIG results

This matrix is intended to bind to the qualified results of the sibling
SW-1510-DOC-TRUTH and SW-1510-MIRROR-CONFIG tasks (and SW-1510-RELEASE). Those
tasks' qualified, composed **results are not available in this checkout**: the
task briefs, the controller ledger and the sibling worktrees are outside this
lane's read scope, and no accepted result artifact for them is present here.

That binding is therefore an explicit **HOLD**, not a claim: the rows above
retain the names and the authorized `SkillWeave-AI` destination verbatim, but
this document does **not** assert that the DOC-TRUTH/MIRROR-CONFIG results have
been qualified or composed. When those qualified results become available they
are folded in by the composing controller (SW-1510-DISC-VERIFY / SW-1510-RELEASE);
nothing here fabricates their content or a passing verification in their place.
