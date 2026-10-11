# SkillWeave

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.5.10-blue)](https://github.com/SkillWeave-AI/skillweave/releases/tag/v1.5.10)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![Skills](https://img.shields.io/badge/skills-15-blue)](skills/)
[![Status](https://img.shields.io/badge/status-production--stable-green)](https://github.com/SkillWeave-AI/skillweave)
[![Capacium](https://img.shields.io/badge/Capacium-Install%20via%20cap-0B1020?style=flat-square&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjIwMCA1MCAyNTAgNTAwIj48cGF0aCBmaWxsPSIjRjdGQUZDIiBkPSJNMzA4LjgzLDU5MC40N2wtMzYuMDItMzYuMjQtMjExLjMyLS4wNC0uMDItMjExLjczLTMzLjExLTMzLjk3LDMzLjEtMzIuODMuMDYtMjE1Ljc0LDIxMy43NC0uMDQsMzMuNDQtMzIuOTcsMzIuNzIsMzIuOTUsMjE0LjAxLjA2LjA5LDIxNS42MiwzMi44NSwzMi43Ni0zMi45OCwzMy4xMS4wNywyMTIuNzQtMjEwLjQuMTItMzYuMjMsMzYuMjJaTTMwOS4wNiw1NTQuMzhsNzUuNTktNzYuODQsMTM5LjgzLTE0MS4zMSwyNy43MS0yOC4xNi05MC42NS05MC43Mi0xNTMuMjItMTUzLjE0LTEyMS41MiwxMjAuNTQtOTQuOTMsOTUuNDYtMjguMTIsMjguNDQsMTA0Ljc1LDEwNS41LDc0LjA4LDczLjY2LDY2LjQ3LDY2LjU3Wk0yMTcuMjYsMTE4LjQ0bDMyLjE0LTMyLjAyLTE2Mi41MS0uMDMuMDQsMTYyLjQ4TDIxNy4yNiwxMTguNDRaTTUyOS44OSw4Ni4zNmwtMTYyLjY1LjA2LDE2Mi42MSwxNjIuNDguMDQtMTYyLjUzWk0yMTIuNTksNDk0LjQ4bC04MC41NS04MS44Ny00NS4wMi00NS43Mi0uMTEsMTYyLjA2LDE1OS40Ny0uMDUtMzMuNzktMzQuNDJaTTM2OC4zNSw1MjguOTRoMTYxLjUzcy0uMDUtMTYyLjA0LS4wNS0xYy4wNGwtNTUuMSw1NC45OS0xMDYuMzgsMTA3LjA1WiIvPjxwYXRoIGZpbGw9IiNGOEZBRkMiIGQ9Ik0zMDguODIsNDgwLjA3bC03OS43Ny00Ny43My02Ny4xMi00MC4wNy0uMDItMTcwLjczLDE0Ni43Mi04NC4yNSw2NC44MywzNi44NSw4Mi45Miw0Ny4xNS4wMiwxNzEuNDgtMTQ3LjU5LDg3LjNaTTMyNi41OSwyMzEuNTRjMTYuMDgsNC4zNiwyOS43MywxMy4zOCw0MC40NywyNi4xNmw0OS41OS0yOC40OS0xMDcuNzgtNjIuODEtMTA3LjE3LDYyLjQzLDQ5Ljc1LDI4LjU1YzE4LjI2LTIyLjY2LDQ2Ljk5LTMyLjg0LDc1LjE1LTI1Ljg0Wk0yOTUuODIsMzg2LjQ2Yy00Ny41OS0xMC4yMS03NC45Ni02MC44My01Ny4xOC0xMDYuMzlsLTUwLjktMjkuNjEtLjA4LDEyOC40LDEwOC4wNiw2Mi4zMi4xLTU0LjdWTTMyMS45NSwzODYuNmwuMjgsNTQuNjQsMTA3LjktNjIuMjktLjA1LTEyOC4yNC00OS43NCwyOS4yN2MxMy43MSwzNS41OS41OSw3NS40My0zMS4zOSw5NS43My04LjMyLDUuNzEtMTcuMTgsOC44Ni0yNi45OSwxMC44OFoiLz48L3N2Zz4%3D&labelColor=0B1020&logoColor=F7FAFC)](https://github.com/Capacium/capacium)

**Multi-agent AI skill orchestration — 15 skills, 7 lifecycle phases, any AI coding agent.**

SkillWeave orchestrates AI-assisted product development from idea to post-release. It works with Claude Code, Codex, Gemini CLI, Cursor, Windsurf, OpenCode, and any agent that reads Markdown or speaks MCP.

---

## What's new in 1.5.10

- **Native execution surfaces**: an identity-verified lifecycle store and
  durable execution stream, native Assay, and Scope, Skills, Verify, Cancel and
  Budget enforcement through the authoritative run path.
- **Route-lock and MCP control/authority**: the `route-decision-v1` payload with
  egress confinement, a server-owned MCP authority that gates every tool, and a
  durable harness-neutral agent mailbox served over both transports.
- **Trustworthy publishing**: one honest publish identity carried to both
  forges, with the Core/SDK contracts qualified against the installed wheel.

See the [1.5.10 changelog](CHANGELOG.md#v1510-2026-10-11--native-execution-route-lock-mcp-control--trustworthy-publishing)
for the full release contract. `1.5.10` is the release in preparation — **not
yet released** until its `v1.5.10` tag exists. Earlier release notes remain in
the [changelog](CHANGELOG.md) as history.

---

## Getting Started

### For Power Users

Use meta-commands directly:

```
/skillweave                    # Where am I? What's next?
/skillweave plan               # View Kanban board
/skillweave build              # Start/continue building
/skillweave test               # Run test pyramid
/skillweave council topic="…"  # Get multi-model opinion
/skillweave report             # Generate release report
```

Or go straight to individual skills:

```
/skillweave-blueprint idea="Your SaaS idea"
/skillweave-promptchain-generate inputs='{"prd": ".skillweave/prds/v1.0/prd.json"}'
/skillweave-releasechain inputs='{"prd": ".skillweave/prds/v1.0/prd.json"}' mode="attended"
```

### For Indie Hackers

Start with `/skillweave` to see where your project stands. The navigator detects your phase automatically and recommends the next step.

**New project:**
1. `/skillweave-blueprint idea="Your idea"` — Creates PRD
2. `/skillweave-promptchain-generate inputs='{"prd": ".skillweave/prds/v1.0/prd.json"}'` — Generates execution plan
3. `/skillweave build` — Executes the plan with testing gates

**Existing project:**
1. `/skillweave` — See current phase
2. Follow the recommendation

### For Non-Technical Users

Just type:

```
/skillweave start
```

The wizard asks 5 simple questions and starts the right tool for you.

---

## Install

SkillWeave is distributed from source and via **Capacium**. There is **no PyPI
distribution** for this repository (see [docs/release-flow.md](docs/release-flow.md)):
installing SkillWeave from PyPI is not a supported install path, and no workflow
publishes it there.

Install paths, all exercised by the packaging suite
(`tests/packaging/test_install_provisioning.py`):

### Via Capacium (recommended)

Works with Claude Desktop, Claude Code, Codex, Gemini CLI, Cursor, Windsurf, OpenCode, and more.

```bash
brew install capacium/tap/capacium
cap install skillweave
```

### From a built wheel

```bash
python -m pip install ./skillweave-1.5.10-py3-none-any.whl
```

`skillweave` declares `requires-python >=3.9`; Python 3.9 is the supported
floor. See [docs/provisioning.md](docs/provisioning.md) for the tested-Python
matrix and the full offline/venv/`uv`/`pipx` flows.

### From a source checkout (editable, for development)

```bash
git clone https://github.com/SkillWeave-AI/skillweave.git
cd skillweave
pip install -e .
```

### Optional: MCP server

The Model Context Protocol server is an **optional extra**, never a base
dependency. The base install runs without it and reports an actionable hint if
the server is requested while absent:

```bash
python -m skillweave.scripts.check_optional_capability mcp
# -> missing; remedy: pip install 'skillweave[mcp]'
```

Add it explicitly when you want the real server:

```bash
pip install 'skillweave[mcp]'
```

### Providers, credentials and the zero-key default

Search is served through six providers, one of which needs **no credential**:

- **DuckDuckGo Lite** is the **zero-key default** — no API key, no account.
- **Serper / SerpApi / Tavily / Brave / Perplexity** are keyed providers,
  selected by name. Each reads its key from the environment:
  `SERPER_API_KEY`, `SERPAPI_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`,
  `PERPLEXITY_API_KEY`. No key is ever required to *install* SkillWeave.

Honest zero-key failure behavior: if a **keyed** provider is selected and its
credential is absent, the call **fails closed** with an explicit
`<ENV_VAR> not set` / missing-credential refusal — it is never silently served
by DuckDuckGo. An unrecognised provider name likewise fails closed.

`envctl` is optional convenience (if present); SkillWeave performs no component
and does not require it.

---

## Architecture

```
Layer 0 — Wizard          /skillweave start         (5 questions, routes to skill)
Layer 1 — Meta-Commands   /skillweave plan|build|…  (7 commands, maps to skills)
Layer 2 — Direct Skills   /skillweave-blueprint …   (15 skills, full control)
```

### 7-Phase Lifecycle

```
Discovery → Blueprint → Design → Build → Release → Launch → Post-Release
```

| Phase | Skills | What it does |
|-------|--------|-------------|
| **Discovery** | discovery, council | Problem definition, user research, empathy mapping |
| **Blueprint** | blueprint, generate, validate | PRD creation, execution planning |
| **Design** | design | Design-Thinking Lens, token extraction, evaluation |
| **Build** | execute, releasechain, observe | Ralph Loop execution with parallel lanes |
| **Release** | releasechain, observe | Release gates, versioning, code review |
| **Launch** | launch | Pre-launch checklist, deployment, metrics |
| **Post-Release** | post-release, repo-health | Retrospective, feedback, iteration |

**Global skills** (available in any phase): lifecycle, repo-health, observe, council

### 15 Skills

The set below is the exact `skills/*/capability.yaml` inventory; the
machine check in `scripts/check-version-truth.py` fails if this list and the
on-disk inventory drift apart.

| Skill | Type | Description |
|-------|------|-------------|
| `skillweave-entry` | facade | Entry-point facade — start, continue, inspect or onboard via the shared EntryService |
| `skillweave-onboarding` | plan | Operator onboarding — role, purpose, autonomy, risk boundary; durable profile |
| `skillweave-lifecycle` | plan | Phase navigation, planning board, testing, wizard, reports |
| `skillweave-discovery` | plan | Problem exploration, user research, 11 prompts |
| `skillweave-blueprint` | plan | Structured PRD creation with complexity assessment |
| `skillweave-design` | mixed | Design-Thinking Lens, brief analysis, token extraction |
| `skillweave-promptchain-generate` | plan | Execution sequence generation from PRD |
| `skillweave-promptchain-validate` | plan | Sequence validation for completeness |
| `skillweave-promptchain-execute` | build | Ralph Loop state machine, batch execution |
| `skillweave-releasechain` | build | Release pipeline with binary gates |
| `skillweave-launch` | build | Deployment, announcement, health checks, metrics |
| `skillweave-post-release` | mixed | Retrospective, feedback collection, iteration planning |
| `skillweave-repo-health` | plan | Inventory scan, dedup, archive, hygiene report |
| `skillweave-observe` | plan | Execution reports, events, memory (read-only) |
| `skillweave-council` | mixed | Multi-model LLM deliberation with web search |

### LLM Council

Convene multiple AI models for research, code review, or architecture decisions:

- **3 stages**: Independent opinions → anonymous peer review → chairman synthesis
- **Profiles**: quick (1 model), default (2), deep (2), expert (2) — provider-native Faidata roster ids
- **Web search**: DuckDuckGo (free), Serper, Tavily, Brave, SerpApi, Perplexity
- **Output**: Markdown or structured JSON with consensus score
- **Truthful attribution**: provider-native model ids; each seat records
  requested/resolved/answering model, status, provider and profile revision. The
  answering model is read from the response envelope, never inferred from the
  request, and fewer than the minimum distinct answering models is a *degraded*
  run — never reported as consensus.

### Adapters & extension registry

Add or swap a search provider, register a local tool, or read a package-format
manifest through **one** neutral extension point, with no vendor edit:

- **Provider swap** is a data change; the zero-key DuckDuckGo default needs no
  credential, and a keyed provider is selected by name.
- **Local tools** register directly (no package format); **Capacium** is an
  optional, versioned package-format reader whose `kind` stays opaque.
- **Fail-closed** admission: an unapproved adapter, a denied egress, a missing
  credential and an unknown provider are distinct, attributable refusals.

See **[docs/adapters.md](docs/adapters.md)** for the consumer-facing guide
(provider swap, zero-key defaults, local tool and connector-pack examples,
package-author vs consumer responsibilities, and the conformance matrix). Every
claim there is backed by a machine check under `tests/contract/`.

### Testing

5-level test pyramid with automatic gate decisions:

```
Level 5: Evidence/Groundedness  (are claims backed by code?)
Level 4: Acceptance             (do acceptance criteria pass?)
Level 3: E2E Smoke              (does the system start and respond?)
Level 2: Unit                   (do individual functions work?)
Level 1: Lint                   (is the code clean?)
```

Gate decisions: **PROMOTE** (all pass) | **HOLD** (minor failures, suggest fix) | **ROLLBACK** (critical failures, block)

### Planning

File-based Kanban with YAML frontmatter tickets:

```
.skillweave/planning/
├── backlog/     # Tickets waiting
├── doing/       # In progress
├── done/        # Completed
└── BOARD.md     # Auto-generated board view
```

Create tickets, move between states, seed from PRD — all via `/skillweave plan`.

---

## Configuration

All SkillWeave data lives in `.skillweave/` (git-ignored):

```
.skillweave/
├── config.yaml          # Risk mode, tier, feature flags
├── planning/            # Kanban tickets (backlog/doing/done)
├── prds/                # Product requirement documents
├── sequences/           # Execution sequences
├── testing/             # Test config + results
├── handover/            # Skill-to-skill transition signals
├── reports/             # Release reports (yyyy-mm-dd-topic.md)
├── discovery/           # Discovery phase artifacts
├── schemas/             # JSON schemas for validation
└── templates/           # Report/doc templates
```

The **27 canonical `.skillweave/` areas**, their single owning skill/subsystem,
lifecycle phase and mutability are specified in
**[docs/substrate-map.md](docs/substrate-map.md)**; the execution/support matrix
in [docs/native-execution-support-1510.md](docs/native-execution-support-1510.md)
summarises the substrate count and the one open deviation it records.

### Risk Modes

| Mode | Behavior |
|------|----------|
| **conservative** | Extra validation, explicit approvals, strict checks |
| **medium** | Balanced automation with safety checks (default) |
| **unicorn** | Maximum automation, optimistic assumptions |

Override hierarchy: CLI parameter > env (`SKILLWEAVE_RISK_MODE`) > project config > global config > default.

---

## Distribution

SkillWeave ships in two formats depending on agent capabilities:

| Format | Agents | How it works |
|--------|--------|-------------|
| **SKILL.md** (Markdown skills) | Claude Code, Codex, Gemini CLI, Qwen Code | Agent reads skill instructions as context |
| **MCP Server** | Claude Desktop (via Capacium), Cursor, Windsurf, Zed, OpenCode | Agent calls SkillWeave tools programmatically |

Both formats use the same Python core (`src/skillweave/`). SKILL.md is the universal baseline; MCP provides richer tool-based interaction for agents that support it.

### Host-Neutral Scope

Shipped skills are host-neutral by contract: they neither invoke a specific host
binary nor default task assignment to a concrete host or model. Task routing is
capability-based (`target_agent: any`); any retained host-specific invocation
lives under `references/adapters/<host>.md` as a non-binding adapter example.

Stable, host-neutral Command/Intent semantics and full cross-transport
(Command ⇄ MCP/Intent) parity were scoped as **1.4.0 scope**. (Historical note:
that line was written against the 1.4.0 train; this README does not restate a
current-release status for it.)

---

## Support and limits

This section states what the shipped engine **does** and the limits it carries;
it makes no future-capability or always-free promise.

The **execution/support matrix** — the versioned surface support table
(`skillweave.support/1`), the shared run-state interpretation
(`skillweave.run-state/1`), the CLI/MCP/Assay rows, and the explicit bare
limits, detected-versus-live-tested adapters, unknown-capacity rule, preview
features and plaintext onboarding storage behaviour — is
**[docs/native-execution-support-1510.md](docs/native-execution-support-1510.md)**.
Every number and name there is derived from the release tree and asserted by
`tests/unit/test_1510_doc_truth.py`.

The 15 skills (all 15 skills) and 7 phases above are the ones this repository
ships — the exact `skills/*/capability.yaml` inventory. The local engine keeps
its raw events on the machine and works offline; the aggregate telemetry service
is labelled **`preview/unavailable`** and this release ships **no** production
service for it (`AGGREGATE_SERVICE_GA` is pinned `False`). Edition contracts
exist for **Community / Pro / Enterprise**; `EditionGuard` grants only what an
edition's declared contract allows and refuses an unknown edition. Expected
limits — a keyless zero-key search default, a source/Capacium-only install (no
PyPI), a capability handshake where **unknown capacity is never treated as
unlimited**, and onboarding state written as **plaintext** YAML — are all
itemised in the matrix above.

---

## Version truth

Version labels on this page distinguish four facts, which are **not** one
number. The declared, machine-checked inventory is
[docs/version-truth.md](docs/version-truth.md), verified by
`scripts/check-version-truth.py`:

| Fact | Meaning | Current value |
|------|---------|---------------|
| **candidate** | `[project].version` in `pyproject.toml` (the source of truth) and the bundle `capability.yaml` | `1.5.10` |
| **released** | the latest canonical **annotated** product tag, by its **peeled commit** (`git rev-parse '<tag>^{}'`) | the tag rule above, printed in full by [docs/version-truth.md](docs/version-truth.md) |
| **installed** | the distribution metadata of a local install (`importlib.metadata`) | `1.5.10` (may be absent) |
| **advertised early** | a version named but not yet released | `1.5.10` (the release in preparation; **not yet released** until its tag exists) |

The version badge above advertises the candidate `1.5.10`, the release **in
preparation** — it is deliberately **not** presented as released until its
`v1.5.10` tag exists. The released fact, whenever it is needed, is derived by
the peeled-tag rule from the repository rather than printed here. Historical
versions (for example the `1.4.0` note in *Host-Neutral Scope*) appear only as
explicitly historical statements. See
[docs/version-truth.md](docs/version-truth.md) for the full rule, the
peeled-tag policy and the provenance of each figure.

---

## License

Apache 2.0 — Copyright 2026 LangeVC.com. See [LICENSE](LICENSE).
