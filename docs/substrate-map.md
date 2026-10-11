# SkillWeave Substrate Map (`.skillweave/`)

This document defines the authoritative top-level directory and file layout of the `.skillweave/` substrate. It maps each area to its primary **owner skill**, its **lifecycle phase**, its **mutability policy**, and its **operational purpose**.

Every area under `.skillweave/` is machine-checked for documentation completeness and drift prevention via `tests/unit/test_substrate_drift.py`.

---

## 1. Overview and Core Invariants

The `.skillweave/` directory is the **local, generated, git-excluded** state and artifact substrate for SkillWeave projects. It houses planning artifacts, persistent architecture memory, lifecycle configurations, prompt chains, release gates, and execution journals.

It is created in a consuming project by the preflight (`SkillWeavePersistence.ensure_folder_structure`), which makes the folder and its subdirectories on demand. It is **not** shipped: `tests/packaging/test_discovery_installed.py` asserts that the installed distribution contains no `.skillweave/` at all, and `pyproject.toml` packages only `py.typed`.

### Invariants:
1. **Isolated Output Routing**: All AI planning, execution, and validation artifacts reside inside `.skillweave/`—never in arbitrary root-level directories.
2. **Single Ownership**: Every top-level area has exactly one primary owning skill or subsystem responsible for its schema, read/write semantics, and evolution.
3. **Phase Traceability**: Every area is bound to a defined lifecycle phase (Discovery, Blueprint, Design, Build, Release, Launch, Post-Release) or designated as Cross-Cutting/Global.
4. **Drift Enforcement**: Any new file or directory introduced into `.skillweave/` must be registered and documented in this specification; undocumented additions fail CI gates.
5. **Git Exclusion**: `.skillweave/` belongs in `.gitignore` in every project, including this one. What SkillWeave generates into the substrate — PRDs, specifications, sequences, discovery findings, handover records — is intellectual property, and for an open-core product it is the part that is not open. A public repository must never carry it. The private per-org planning repository is the only place substrate content is tracked, and it is tracked there deliberately rather than as a side effect of a tool writing into a working tree.

   `Versioned` in the Mutability column below means *revised across iterations*. It does not mean *tracked in git*.
6. **Reachability is Injection, not Tracking**: A dispatched lane needs the PRD inside its worktree. That is solved by copying or linking it in at dispatch time (`regen-sequence.py`; see the 1.5.1 dispatch initiative), never by committing the substrate so that `git worktree add` happens to carry it along. Tracking the substrate to make it reachable trades an IP boundary for a convenience the dispatcher already provides.

### The substrate rule is taught, not only enforced

Invariants 5 and 6 are not something a user can infer; onboarding must teach them so a new project ends up correctly configured without the operator explaining anything. `onboarding_cli.run_onboarding` now does the following, in order:

1. **States the substrate rule** — what `.skillweave/` is, that it is generated, that direction/durability/disclosure are declared per area and git answers none of them on its own, that a public repository never carries substrate content, and that a dispatched lane receives its PRD by injection (`onboarding_cli.SUBSTRATE_RULE`).
2. **Writes the `.gitignore` exclusion** for a git project — the anchored `/.skillweave/` entry (`onboarding_cli.GITIGNORE_ENTRY`) — without being asked. Absent `.gitignore` is created.
3. **Asks whether a private per-org planning repository exists** and records the answer (`planning_repo_exists`) instead of assuming the substrate is durable where it stands.
4. **Refuses in a public repository** — when the `origin` remote is public (`github.com`) and git is already tracking `.skillweave/` content, onboarding returns `substrate_refused` with a reason, rather than leaving that content tracked. The reason names the IP boundary and the untracking command (`git rm -r --cached .skillweave`).

The git-facing pieces (`_is_git_project`, `_is_public_remote`, `_git_tracked_substrate`, `_ensure_gitignore_exclusion`) live in `onboarding_cli.py` and keep the `GITIGNORE_ENTRY` path consistent with `SkillWeavePersistence._ensure_gitignore`.

### Current deviation from invariant 5

This repository does not yet satisfy invariant 5. `.skillweave/` is tracked here, and 105 files are on the public GitHub mirror as a result.

Invariant 5 cannot simply be applied, because the **test suite** — not the product — reads this repository's own substrate instance as if it were a fixture. Measured by exporting `git archive HEAD` twice, once with and once without `.skillweave/`, and taking the difference against a baseline: **10 tests fail and `tests/test_discovery.py` fails to collect.** Five files carry the whole dependency:

| File | Consumed by | What it actually is |
|---|---|---|
| `.skillweave/phases.yaml` | `test_lifecycle_system.py`, `test_lifecycle_single_source.py` | A generated mirror. `lifecycle.to_yaml()` is the source of truth; the test is named `test_checked_in_yaml_matches_the_generator`. |
| `.skillweave/bundles.yaml` | same | same |
| `.skillweave/config.yaml` | `test_lifecycle_system.py::TestIntegration` | Config the preflight can generate |
| `.skillweave/release/skill-boundaries.yaml` | `test_skill_boundaries.py` | Policy |
| `.skillweave/lib/ideation.py` | `test_discovery.py` (`from ideation import …`) | Project-local library; no `src/skillweave/ideation.py` exists |

None of these is a planning artifact. Disentangling them — generated mirrors and policy into `tests/fixtures/`, `config.yaml` produced by `ensure_folder_structure()` in the test rather than read from disk, `ideation.py` into `src/` or a fixture — is the precondition for adding `.skillweave/` to this repository's `.gitignore`.

`tests/unit/test_substrate_drift.py` is unaffected: it checks only that what is on disk is documented, so an absent substrate leaves it green.

---

## 2. Top-Level Substrate Map (All 27 Canonical Areas)

| # | Area Path | Kind | Owner Skill / Subsystem | Lifecycle Phase | Mutability | Purpose & Responsibilities |
|---|-----------|------|-------------------------|-----------------|------------|----------------------------|
| 1 | `archive/` | Directory | `skillweave-releasechain` / `skillweave-repo-health` | Release / Post-Release | Append-only / Immutable | Historical archive of completed runs, superseded manifests, and previous release snapshots. |
| 2 | `bundles.yaml` | YAML Config | `skillweave-lifecycle` | Global / Setup | Declarative / Config | Definitions of available lifecycle bundles (e.g. `full-lifecycle`, `design-and-build`, `release-and-launch`). |
| 3 | `checklists/` | Directory | `skillweave-promptchain-execute` / `skillweave-releasechain` | Build / Release | Versioned / Dynamic | Operational and release readiness checklists, pre-flight verification items, and gate rubrics. |
| 4 | `cleanup/` | Directory | `skillweave-repo-health` | Post-Release / Global | Generated / Writable | Repository inventory, file classification rules, duplication analysis, and lean-core optimization plans. |
| 5 | `config.yaml` | YAML Config | Core Runtime / `skillweave-lifecycle` | Global / Init | User / Engine Config | Project-level configuration: active bundle, current phase, feature flags, risk modes, lens toggles, git-flow rules. |
| 6 | `design/` | Directory | `skillweave-design` | Design (Phase 3) | Generated / Versioned | Design tokens, UI component specifications, UX principles, wireframes, and design critique reports. |
| 7 | `discovery/` | Directory | `skillweave-discovery` / `skillweave-council` | Discovery (Phase 1) | Iterative / Versioned | User research findings, empathy maps, competitive landscape analysis, problem statements, and council strategy notes. |
| 8 | `handover/` | Directory | `skillweave-lifecycle` / `skillweave-observe` | Cross-Cutting / Global | Append-only / State | Inter-session context handover records, agent shift logs, and cross-workflow continuity state. |
| 9 | `hooks/` | Directory | Core Runtime / `skillweave-promptchain-execute` | Global / Execution | Executable / Config | Lifecycle automation hooks, pre/post step triggers, validation webhooks, and execution event scripts. |
| 10 | `lenses/` | Directory | `skillweave-discovery` / `skillweave-design` | Discovery / Design | Declarative / Config | Domain and cognitive lens specifications (e.g. Design Thinking lens rules, strictness, and principles). |
| 11 | `lib/` | Directory | Core Runtime / Project Domain | Global | Code / Utilities | Local Python helper libraries, assumption scoring functions, ideation utilities, and project-specific extensions. |
| 12 | `licenses/` | Directory | `skillweave-repo-health` / `skillweave-releasechain` | Release / Global | Audit / Reference | Third-party dependency licenses, license compatibility matrices, and compliance audit reports. |
| 13 | `lifecycle/` | Directory | `skillweave-lifecycle` | Global | Engine / State | Lifecycle state machine data, custom phase transition graphs, and dynamic phase configuration. |
| 14 | `manifesto/` | Directory | `skillweave-blueprint` / Core Governance | Global / Inception | Canonical / Locked | Core project manifesto, foundational vision, non-negotiable architectural principles, and ethics guardrails. |
| 15 | `memory/` | Directory | `skillweave-observe` / `skillweave-promptchain-execute` | Global / Continuous | Managed / Versioned | Persistent project knowledge: `architecture.yaml`, `conventions.yaml`, `decisions.yaml`, `open-issues.yaml`, `rules.yaml`. |
| 16 | `onboarding-state.yaml` | YAML State | `skillweave-lifecycle` / CLI | Setup / Onboarding | Ephemeral / State | Interactive onboarding tutorial progress, completed setup milestones, and walkthrough tracking. |
| 17 | `phases.yaml` | YAML Config | `skillweave-lifecycle` | Global / Setup | Declarative / Config | Authoritative lifecycle phase hierarchy (Phases 1-7), skill mappings, phase types (core/recommended/optional). |
| 18 | `planning/` | Directory | `skillweave-blueprint` / `skillweave-lifecycle` | Blueprint / Build | Dynamic / State | File-based Kanban planning tickets (`backlog/`, `doing/`, `done/`) following the beans-pattern with YAML frontmatter. |
| 19 | `prds/` | Directory | `skillweave-blueprint` | Blueprint (Phase 2) | Versioned / Structured | Formal Product Requirements Documents containing structured `prd.json` and narrative markdown per initiative. |
| 20 | `prompts/` | Directory | `skillweave-promptchain-generate` / `skillweave-discovery` | Discovery / Blueprint / Build | Catalog / Templates | Phase-specific prompt templates, role definitions, and system prompt components organized by phase. |
| 21 | `release/` | Directory | `skillweave-releasechain` | Release (Phase 5) | Governed / Policy | Release readiness model (`readiness-model.yaml`), binary gate policies, and skill boundaries (`skill-boundaries.yaml`). |
| 22 | `reports/` | Directory | `skillweave-observe` / `skillweave-launch` | Post-Release / Observability | Generated / Audit | Execution timing analytics, gate attestation receipts, benchmark evaluations, and post-release summaries. |
| 23 | `sequences/` | Directory | `skillweave-promptchain-generate` / `skillweave-promptchain-validate` | Blueprint / Build | Structured / Executable | Machine-executable prompt sequences (`execution-sequences.yaml`), complexity analysis, and agent assignments. |
| 24 | `specs/` | Directory | `skillweave-blueprint` / `skillweave-promptchain-generate` | Blueprint (Phase 2) | Versioned / Technical | Technical specifications, feature backlogs (`backlog.yaml`), API interface definitions, and architectural schemas. |
| 25 | `templates/` | Directory | `skillweave-blueprint` / `skillweave-promptchain-generate` | Blueprint / Setup | Catalog / Scaffolds | Reusable starters for API services, CLI tools, web apps, validation templates, and discovery canvases. |
| 26 | `tracking-log/` | Directory | `skillweave-observe` / `skillweave-promptchain-execute` | Build / Release / Global | Append-only / Journal | Runtime execution logs, Ralph loop iteration records, gate attestation history, and state machine audit trails. |
| 27 | `rework/` | Directory | Core Runtime / CLI (`rework`) | Build / Release | Generated / Writable | Structured rework briefs auto-generated by the `rework` command from failed gate logs — machine-written output, never human-authored input. |

*(Supplementary extensions such as `skills/` and `testing/` are supported for project-local customizations and automated verification caches).*

---

## 3. Detailed Area Specifications

### 3.1 `archive/`
- **Owner**: `skillweave-releasechain` and `skillweave-repo-health`
- **Lifecycle**: Release (Phase 5), Post-Release (Phase 7)
- **Role**: Serves as a deterministic vault for obsolete runs, retired sequences, and superseded manifests. Ensures project history is preserved without polluting active working directories.
- **Key Artifacts**: `manifest.yaml`, dated archive bundles.

### 3.2 `bundles.yaml`
- **Owner**: `skillweave-lifecycle`
- **Lifecycle**: Global / Setup
- **Role**: Defines pre-packaged lifecycle pathways combining selected phases. Allows projects to execute lean subsets (e.g. `discovery-to-blueprint` or `design-and-build`) without enforcing all 7 phases.
- **Key Schema**: List of bundle entries with `id`, `name`, `phases`, and `entry_requires`.

### 3.3 `checklists/`
- **Owner**: `skillweave-promptchain-execute` and `skillweave-releasechain`
- **Lifecycle**: Build (Phase 4), Release (Phase 5)
- **Role**: Maintains machine-checkable and human-verifiable rubrics. Used before triggering binary release gates.
- **Key Artifacts**: `release-readiness.md`, `sample-execution.md`.

### 3.4 `cleanup/`
- **Owner**: `skillweave-repo-health`
- **Lifecycle**: Post-Release (Phase 7) & Global
- **Role**: Houses codebase classification outputs, duplication detection matrices, dead code inventories, and lean-core reduction recommendations.
- **Key Artifacts**: `inventory.yaml`, `classifications.yaml`, `duplications.yaml`, `lean-core.yaml`, `report.md`.

### 3.5 `config.yaml`
- **Owner**: Core Runtime and `skillweave-lifecycle`
- **Lifecycle**: Global / Project Initialization
- **Role**: Central declarative configuration controlling execution behavior, active bundles, lens strictness, risk mode (`conservative`, `medium`, `unicorn`), feature flags, and git flow rules.
- **Key Schema**: `features`, `lifecycle`, `lens`, `mode`, `git_flow`, `release`.

### 3.6 `design/`
- **Owner**: `skillweave-design`
- **Lifecycle**: Design (Phase 3)
- **Role**: Holds UX guidelines, design tokens, interaction flows, accessibility evaluations, and frontend architecture critiques.
- **Key Artifacts**: `tokens.json`, `design-critique.md`, `wireframes.yaml`.

### 3.7 `discovery/`
- **Owner**: `skillweave-discovery` and `skillweave-council`
- **Lifecycle**: Discovery (Phase 1)
- **Role**: Stores problem validation evidence, stakeholder interviews, competitor matrices, opportunity canvases, and multi-model council strategy syntheses.
- **Key Artifacts**: `*-council-strategy.md`, `opportunity-canvas.md`, `user-interviews.md`.

### 3.8 `handover/`
- **Owner**: `skillweave-lifecycle` and `skillweave-observe`
- **Lifecycle**: Cross-Cutting / Global
- **Role**: Manages continuity between autonomous agent sessions, developer shifts, and cross-lane handoffs.
- **Key Artifacts**: `README.md`, `session-handover-*.md`.

### 3.9 `hooks/`
- **Owner**: Core Runtime and `skillweave-promptchain-execute`
- **Lifecycle**: Global / Execution
- **Role**: Houses lifecycle event hooks (e.g. `pre-execute`, `post-execute`, `on-gate-failure`) for custom automation and integration.
- **Key Artifacts**: Executable scripts and YAML hook declarations.

### 3.10 `lenses/`
- **Owner**: `skillweave-discovery` and `skillweave-design`
- **Lifecycle**: Discovery (Phase 1), Design (Phase 3)
- **Role**: Configures cognitive and domain lenses applied during planning and ideation.
- **Key Artifacts**: `design-thinking.yaml`, custom lens profiles.

### 3.11 `lib/`
- **Owner**: Core Runtime / Project Domain
- **Lifecycle**: Global
- **Role**: Contains project-local Python helper libraries and domain modules (e.g. assumption prioritization models, ideation ranking logic) consumed by prompt chains.
- **Key Artifacts**: `assumptions.py`, `ideation.py`.

### 3.12 `licenses/`
- **Owner**: `skillweave-repo-health` and `skillweave-releasechain`
- **Lifecycle**: Release (Phase 5) & Global
- **Role**: Maintains dependency license inventories, third-party attribution notices, and legal compliance audits.
- **Key Artifacts**: `license-audit.json`, `attribution.md`.

### 3.13 `lifecycle/`
- **Owner**: `skillweave-lifecycle`
- **Lifecycle**: Global
- **Role**: Holds dynamic lifecycle state machine definitions, custom phase transitions, and lifecycle hook configurations.
- **Key Artifacts**: `state-machine.yaml`, `custom-phases.yaml`.

### 3.14 `manifesto/`
- **Owner**: `skillweave-blueprint` / Core Governance
- **Lifecycle**: Global / Inception
- **Role**: Defines the fundamental mission, core architectural values, non-functional requirements, and ethical guardrails of the project.
- **Key Artifacts**: `README.md`, `principles.yaml`.

### 3.15 `memory/`
- **Owner**: `skillweave-observe` and `skillweave-promptchain-execute`
- **Lifecycle**: Global / Continuous
- **Role**: Central repository of persistent architectural knowledge, conventions, active decisions, and unsolved issues.
- **Key Artifacts**: `architecture.yaml`, `conventions.yaml`, `decisions.yaml`, `open-issues.yaml`, `rules.yaml`.

### 3.16 `onboarding-state.yaml`
- **Owner**: `skillweave-lifecycle` / CLI
- **Lifecycle**: Setup / Onboarding
- **Role**: Tracks user progress through interactive CLI onboarding, completed tutorials, and active learning paths.
- **Key Schema**: `current_step`, `completed_tutorials`, `last_updated`.

### 3.17 `phases.yaml`
- **Owner**: `skillweave-lifecycle`
- **Lifecycle**: Global / Setup
- **Role**: The authoritative single source of truth for the 7 canonical lifecycle phases, their numeric order, associated skills, required capabilities, and phase strictness (`core`, `recommended`, `optional`).
- **Key Schema**: `phases` list with `id`, `order`, `skills`, `capabilities`, `phase_type`, and `global_skills`.

### 3.18 `planning/`
- **Owner**: `skillweave-blueprint` and `skillweave-lifecycle`
- **Lifecycle**: Blueprint (Phase 2), Build (Phase 4)
- **Role**: File-based Kanban planning board using the beans-pattern. Contains ticket files in `backlog/`, `doing/`, and `done/` with YAML frontmatter.
- **Key Artifacts**: `backlog/*.md`, `doing/*.md`, `done/*.md`.

### 3.19 `prds/`
- **Owner**: `skillweave-blueprint`
- **Lifecycle**: Blueprint (Phase 2)
- **Role**: Stores formal Product Requirements Documents. Each initiative directory contains structured `prd.json` and narrative `prd.md`.
- **Key Artifacts**: `initiative-*/prd.json`, `initiative-*/prd.md`, `v*/*-prd.md`.

### 3.20 `prompts/`
- **Owner**: `skillweave-promptchain-generate` and `skillweave-discovery`
- **Lifecycle**: Discovery (Phase 1), Blueprint (Phase 2), Build (Phase 4)
- **Role**: Catalog of phase-tailored prompt templates, persona cards, and structured extraction instructions.
- **Key Artifacts**: `discovery/*.md`, `design/*.md`, `build/*.md`.

### 3.21 `release/`
- **Owner**: `skillweave-releasechain`
- **Lifecycle**: Release (Phase 5)
- **Role**: Governs release verification criteria, gate policies, and skill boundary constraints.
- **Key Artifacts**: `readiness-model.yaml`, `skill-boundaries.yaml`.

### 3.22 `reports/`
- **Owner**: `skillweave-observe` and `skillweave-launch`
- **Lifecycle**: Post-Release (Phase 7), Launch (Phase 6), Observability
- **Role**: Stores generated execution summaries, timing benchmarks, gate receipts, and retrospective syntheses.
- **Key Artifacts**: `execution-report-*.json`, `benchmark-summary.md`.

### 3.23 `sequences/`
- **Owner**: `skillweave-promptchain-generate` and `skillweave-promptchain-validate`
- **Lifecycle**: Blueprint (Phase 2), Build (Phase 4)
- **Role**: Houses machine-executable prompt sequences, complexity assessments, and role assignment matrices.
- **Key Artifacts**: `initiative-*/execution-sequences.yaml`, `initiative-*/complexity-analysis.md`, `initiative-*/agent-assignments.json`.

### 3.24 `specs/`
- **Owner**: `skillweave-blueprint` and `skillweave-promptchain-generate`
- **Lifecycle**: Blueprint (Phase 2)
- **Role**: Stores technical interface specifications, task backlogs, and subsystem contract definitions.
- **Key Artifacts**: `backlog.yaml`, `README.md`, `api-spec.yaml`.

### 3.25 `templates/`
- **Owner**: `skillweave-blueprint` and `skillweave-promptchain-generate`
- **Lifecycle**: Blueprint (Phase 2) / Setup
- **Role**: Reusable starter templates for project scaffolding (APIs, CLI tools, web apps, validation sheets, discovery tools).
- **Key Artifacts**: `api_service_starter.yaml`, `cli_tool_starter.yaml`, `web_app_starter.yaml`, `prompt-sequence-template.md`.

### 3.26 `tracking-log/`
- **Owner**: `skillweave-observe` and `skillweave-promptchain-execute`
- **Lifecycle**: Build (Phase 4), Release (Phase 5), Global
- **Role**: Authoritative chronological execution log and state store. Records Ralph Loop iterations, prompt inventory, gate validation records, and run receipts.
- **Key Artifacts**: `iterations.yaml`, `execution-state.json`, `status.yaml`.

### 3.27 `rework/`
- **Owner**: Core Runtime / CLI (`rework`)
- **Lifecycle**: Build (Phase 4), Release (Phase 5)
- **Role**: Stores structured rework briefs auto-generated by the `rework` command from failed gate logs. This is generated output — machine-written briefs, never human-authored input — consistent with `tracking-log/` and `reports/`.
- **Key Artifacts**: `<lane_id>-<timestamp>.md`.

---

## 4. Lifecycle Flow and Substrate Progression

The 7 lifecycle phases interact with `.skillweave/` areas in strict sequential order while cross-cutting areas maintain continuous governance:

```
Phase 1: Discovery  ──► .skillweave/discovery/, lenses/, prompts/discovery/
      │
Phase 2: Blueprint  ──► .skillweave/prds/, specs/, sequences/, planning/, templates/
      │
Phase 3: Design     ──► .skillweave/design/, lenses/
      │
Phase 4: Build      ──► .skillweave/sequences/, checklists/, tracking-log/
      │
Phase 5: Release    ──► .skillweave/release/, checklists/, archive/, licenses/
      │
Phase 6: Launch     ──► .skillweave/reports/
      │
Phase 7: Post-Rel   ──► .skillweave/cleanup/, archive/, reports/
```

### Cross-Cutting Substrates (Active in All Phases):
- **Configuration & Phasing**: `.skillweave/config.yaml`, `phases.yaml`, `bundles.yaml`, `onboarding-state.yaml`
- **Knowledge & Memory**: `.skillweave/memory/`, `manifesto/`, `handover/`, `hooks/`, `lib/`

---

## 5. Supplementary & Workspace Substrate Areas

The following areas under `.skillweave/` represent supplementary runtime artifacts, project-local extensions, and caching structures:

### 5.1 `bundles/`
- **Owner**: `skillweave-lifecycle`
- **Lifecycle**: Global / Setup
- **Role**: Directory containing unpacked or project-specific lifecycle bundle definition overrides and custom bundle manifests.
- **Key Artifacts**: Custom bundle configuration files and descriptors.

### 5.2 `docs/`
- **Owner**: `skillweave-lifecycle` / Documentation Subsystem
- **Lifecycle**: Global / Setup
- **Role**: Project-level substrate documentation, onboarding guidelines, and reference materials.
- **Key Artifacts**: `getting-started.md`, architecture summaries.

### 5.3 `observation/`
- **Owner**: `skillweave-observe`
- **Lifecycle**: Build (Phase 4) / Observability
- **Role**: Active telemetry collection, structured event streaming logs, and execution metric traces.
- **Key Artifacts**: `events/*.jsonl`, streaming traces.

### 5.4 `phases/`
- **Owner**: `skillweave-lifecycle`
- **Lifecycle**: Global / Setup
- **Role**: Directory containing custom, domain-specific phase definition schemas and extension manifests.
- **Key Artifacts**: Extended phase descriptors and lifecycle transitions.

### 5.5 `schemas/`
- **Owner**: Core Runtime / `skillweave-promptchain-execute`
- **Lifecycle**: Build (Phase 4), Release (Phase 5), Global
- **Role**: JSON schema specifications and validation contracts ensuring phase gate conformance and artifact structural integrity.
- **Key Artifacts**: `blueprint-ready.schema.json`, `build-complete.schema.json`, `deployed.schema.json`, `handover.schema.json`, `launch-ready.schema.json`.

  **Canonical location**: the five gate schemas named above are durably tracked in the planning repository at `.skillweave/planning/schemas/` in `skillweave/skillweave-planning` (allowlisted and committed there under SW152-016). Any byte-identical copy under this repository's own `.skillweave/schemas/` is a git-excluded local mirror, not a source of truth. Sync direction is planning (canonical) → here (mirror); never edited here, not durable.

### 5.6 `testing/`
- **Owner**: `skillweave-promptchain-execute` / Verification Subsystem
- **Lifecycle**: Build (Phase 4) / Verification
- **Role**: Automated verification configurations, temporary test execution workspaces, and gate validation results cache.
- **Key Artifacts**: `test-config.yaml`, `results/`.

### 5.7 `wizard/`
- **Owner**: `skillweave-lifecycle` / CLI
- **Lifecycle**: Setup / Inception
- **Role**: Interactive onboarding wizard scaffolds, project initialization state, and guided setup workflows.
- **Key Artifacts**: Wizard step state and scaffolding templates.

### 5.8 `catalogue.yaml`
- **Owner**: Core Runtime / Model & Harness Catalogue Subsystem
- **Lifecycle**: Global / Setup
- **Role**: Declares the model-and-harness catalogue (runtime CLI, harness statuses, model capabilities, role defaults, contract index), the single source of truth for role-to-model resolution and the `!= ops` separation-of-duties guard.
- **Key Artifacts**: `catalogue.yaml`.

### 5.9 `releases/`
- **Owner**: `skillweave-releasechain`
- **Lifecycle**: Release (Phase 5)
- **Role**: Stores baseline manifests, evidence files, and release artefacts for Contract Authority patch releases. Content is per-release-version subdirectories.
- **Key Artifacts**: `baseline.json`, `evidence/*.out`.

---

## 5a. Durable Input Tier (`skillweave.config/`)

The **`skillweave.config/`** directory is the single durable *input* tier. It is **not** part of the `.skillweave/` substrate: it lives in the consumer's repository root, is meant to be tracked and hand-edited, and is **never** git-excluded by preflight. It carries a leading-less name (no dot) to avoid colliding with the `skillweave` Python package and with a consumer's own `config/` directory.

| Tier | Path | Kind | Git treatment | Purpose |
|------|------|------|---------------|---------|
| Tier 1 (shipped default) | `skillweave/assets/catalogue.yaml` | Shipped deliverable | tracked in this repo | The default catalogue that ships with SkillWeave. |
| Tier 2 (durable override) | `skillweave.config/catalogue.yaml` | Durable input | tracked in consumer repo, never git-excluded | A team's tuned model roster. Preflight seeds it from the tier-1 deliverable on first run and never overwrites it afterwards. |

- **Owner**: Core Runtime / Model & Harness Catalogue Subsystem + preflight (`SkillWeavePersistence`).
- **Lifecycle**: Global / Setup.
- **Seed semantics**: `SkillWeavePersistence.ensure_folder_structure()` copies `skillweave/assets/catalogue.yaml` into `skillweave.config/catalogue.yaml` only when the target does not already exist — a fresh clone starts from the shipped default, and a tuned roster is preserved across preflights and clones.
- **Read semantics**: `core.catalogue._default_path()` prefers `skillweave.config/catalogue.yaml` when present, falling back to the shipped `skillweave/assets/catalogue.yaml`. It must never read a catalogue from `.skillweave/`.
- **Key Artifacts**: `catalogue.yaml`.

---

## 6. Core Subsystem & Runtime Architecture Layout (SW-140)

SkillWeave core runtime and execution components are structured under `src/skillweave/`:

### 6.1 `src/skillweave/api/`
- **Owner**: Core Runtime / API Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Programmatic entry points and service interfaces exposing execution capabilities, pipeline triggers, and lifecycle operations.
- **Key Artifacts**: `api/run.py`, `api/__init__.py`.

### 6.2 `src/skillweave/cli/`
- **Owner**: Core Runtime / CLI Subsystem
- **Lifecycle**: Cross-Cutting / Tooling
- **Role**: Command-line interface orchestration, argument parsing, interactive terminal commands, and command routing.
- **Key Artifacts**: `cli/main.py`, `cli/run.py`, `cli/__init__.py`.

### 6.3 `src/skillweave/core/proc/`
- **Owner**: Core Runtime / Process Subsystem
- **Lifecycle**: Build (Phase 4) / Execution
- **Role**: Low-level process execution management, async subprocess runners, isolation boundaries, and streaming process output capture.
- **Key Artifacts**: `core/proc/runner.py`, `core/proc/__init__.py`.

### 6.4 `src/skillweave/core/observer/`
- **Owner**: Core Runtime / Observer Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Read-only tracking of execution events, maintaining persistent lease and journal offset, preventing unauthorized state mutation.
- **Key Artifacts**: `observer.py`, `__init__.py`.

### 6.5 `src/skillweave/core/context/`
- **Owner**: Core Runtime / Context Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Context check-pointing, block-level provenance, and token threshold limits (`no_new_task`, `checkpoint`, `stop`).
- **Key Artifacts**: `checkpoint.py`, `config.py`, `limits.py`, `manager.py`.
### 6.6 `src/skillweave/core/recovery/`
- **Owner**: Core Runtime / Recovery Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Reconstructs execution state (DAG, Claims, Gate) from the ReadOnlyObserver after crashes (Orphan, Worker, Coordinator).
- **Key Artifacts**: `manager.py`, `__init__.py`.
### 6.7 `src/skillweave/core/policy/`
- **Owner**: Core Runtime / Policy Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Maintains persistent policy for execution retries, backoff schedules, budget tracking, and idempotent compensation.
- **Key Artifacts**: `policy.py`, `__init__.py`.
### 6.8 `src/skillweave/core/workspace/remote/`
- **Owner**: Core Runtime / Workspace Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Provides Sandbox/Remote Workspace environments honoring the core Workspace contract, without introducing external orchestration truth.
- **Key Artifacts**: `provider.py`, `__init__.py`.

### 6.9 `src/skillweave/core/operator/`
- **Owner**: Core Runtime / Operator Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Delegated autonomous operator agent capable of running reversible, in-scope decisions while strictly escalating irreversible actions.
- **Key Artifacts**: `operator_agent.py`, `__init__.py`.

### 6.10 `src/skillweave/core/catalogue/`
- **Owner**: Core Runtime / Model & Harness Catalogue Subsystem
- **Lifecycle**: Execution / Runtime
- **Role**: Parses the model & harness catalogue (`config/catalogue.yaml`) and resolves roles to models, honouring the `!= ops` separation-of-duties constraint and `cost_index` preference.
- **Key Artifacts**: `__init__.py`.

---

## 7. Classification by Direction, Durability and Disclosure

Every canonical area additionally carries three axes, each orthogonal to owner, phase and mutability:

- **Direction** — whether the content flows *into* a run (consumed as authored input) or *out of* a run (produced as generated output).
- **Durability** — whether the content is *authored* (written/maintained by humans or shipped as an asset, persistent by intent) or *generated* (machine-produced and recreated/overwritten each cycle).
- **Disclosure** — whether the content is *public* (safe for a public mirror under invariant 5: policy, config, templates, code) or *private* (IP that must stay out of the public mirror: PRDs, discovery findings, plans, handover records).

`pending` means the axis is resolved by an operator decision, not by this document. See section 8.

| Area | Direction | Durability | Disclosure | Reason |
|------|-----------|------------|------------|--------|
| `archive/` | output | generated | private | Historical snapshots of completed runs are machine-written record and project IP. |
| `bundles.yaml` | input | authored | public | Declarative bundle definitions are authored configuration. |
| `checklists/` | pending | pending | pending | See section 8 — authored rubric vs. generated checklist. |
| `cleanup/` | output | generated | private | Inventory and classification outputs are generated per run. |
| `config.yaml` | input | authored | public | Project configuration is authored by the user, not generated. |
| `design/` | output | generated | private | Design tokens and critique reports are produced by the design phase. |
| `discovery/` | output | generated | private | Research findings and council syntheses are generated investigation output. |
| `handover/` | output | generated | private | Session hand-over records are written state between runs. |
| `hooks/` | pending | pending | pending | See section 8 — declarative hook config vs. generated event state. |
| `lenses/` | input | authored | public | Lens specifications are authored policy consumed during planning. |
| `lib/` | input | authored | pending | Helper code is authored; its disclosure hinges on the feature question in section 8. |
| `licenses/` | output | generated | public | Third-party attribution and compliance audits must be disclosed, not hidden. |
| `lifecycle/` | pending | pending | pending | See section 8 — state-machine definition vs. dynamic transition state. |
| `manifesto/` | input | authored | public | Foundational principles are the canonical, shared, non-negotiable vision. |
| `memory/` | output | generated | private | Persistent project knowledge is accumulated machine-assisted output. |
| `onboarding-state.yaml` | output | generated | private | Interactive onboarding progress is a user's own state. |
| `phases.yaml` | input | authored | public | The authoritative phase hierarchy is authored configuration. |
| `planning/` | output | generated | private | Kanban tickets are generated/edited planning state. |
| `prds/` | output | generated | private | Product requirements documents are the core planning IP. |
| `prompts/` | input | authored | public | Phase prompt templates are authored catalogue assets. |
| `release/` | pending | pending | pending | See section 8 — governed policy vs. generated readiness state. |
| `reports/` | output | generated | private | Execution and audit summaries are generated output. |
| `sequences/` | output | generated | private | Machine-executable sequences are generated planning output. |
| `specs/` | output | generated | private | Technical specs and backlogs are authored planning artefacts, IP by default. |
| `templates/` | input | authored | public | Starter scaffolds are authored assets. |
| `tracking-log/` | output | generated | private | Runtime journals are machine-written audit state. |
| `rework/` | output | generated | private | Rework briefs are machine-written output, never human-authored input. |

The two trees implied by Direction:

- **Input tree** (authored, flows in): `bundles.yaml`, `config.yaml`, `lenses/`, `lib/`, `manifesto/`, `phases.yaml`, `prompts/`, `templates/`.
- **Output tree** (generated, flows out): `archive/`, `cleanup/`, `design/`, `discovery/`, `handover/`, `licenses/`, `memory/`, `onboarding-state.yaml`, `planning/`, `prds/`, `reports/`, `rework/`, `sequences/`, `specs/`, `tracking-log/`.
- **Pending** (operator-reserved, see section 8): `checklists/`, `hooks/`, `lifecycle/`, `release/`.

---

## 8. Pending Operator Decisions

The four areas below each have a reading as *authored policy* and a reading as *generated state*. This document records both readings and the evidence, and deliberately does **not** choose. The operator decides; the implementing/testing lane must not assume one. The same holds for whether `lib/ideation.py` and `lib/assumptions.py` are product features.

### 8.1 `checklists/`
- **Authored-policy reading**: a rubric is a policy — release-readiness and execution checklists encode what must pass before a gate, authored once and re-read each run.
- **Generated-state reading**: `src/skillweave/release/checklist.py` ("Generates markdown checklists from readiness assessment results") and `src/skillweave/execution_checklist.py` (mkdirs and writes `.skillweave/checklists/*`) both *produce* files into this directory each run.
- **Evidence leans**: generated — the two in-tree writers both treat it as output; no in-tree writer reads it as authored input.

### 8.2 `release/`
- **Authored-policy reading**: `skill-boundaries.yaml` (tracked in the fixture) and `readiness-model.yaml` are governed policy; the map's mutability column says "Governed / Policy".
- **Generated-state reading**: `src/skillweave/release/readiness.py` writes `.skillweave/release/security-review.md` (generated review status).
- **Evidence leans**: mixed — the boundaries/model are authored policy, while `security-review.md` is generated; the policy subset dominates by volume but the generated subset is non-zero.

### 8.3 `lifecycle/`
- **Authored-policy reading**: `state-machine.yaml` and `custom-phases.yaml` are authored definitions of the state machine.
- **Generated-state reading**: the map's mutability column says "Engine / State" and "dynamic phase configuration", implying transition state is written during operation.
- **Evidence leans**: inconclusive — no `src/` writer targets `.skillweave/lifecycle/` directly; the nearest generated artefact is `phases.yaml` (a separate area).

### 8.4 `hooks/`
- **Authored-policy reading**: `src/skillweave/studio/hooks/binding/loader.py` reads `.skillweave/hooks/<phase>-<position>.yaml` as declarative hook declarations (authored by the project).
- **Generated-state reading**: `src/skillweave/studio/hooks/discovery/registry.py` writes `.skillweave/hooks/.dismissed.json` (generated dismissal state).
- **Evidence leans**: mixed — declared hooks are authored input, `.dismissed.json` is generated state.

### 8.5 `lib/ideation.py` and `lib/assumptions.py` — product features or project extensions?
- **Product-feature reading**: `tests/test_discovery.py` imports `IdeationSession` and `AssumptionTracker` and exercises them as if they were product surface; if they are features they belong in `src/skillweave/` and in **no** `.skillweave/` asset tier, and would ship (public).
- **Project-extension reading**: nothing under `src/` imports them; the packaged distribution deliberately excludes them (`tests/packaging/test_discovery_installed.py`); the map calls `lib/` "project-specific extensions"; today they exist only under `tests/fixtures/substrate-root/.skillweave/lib/`.
- **Evidence leans**: currently fixture/extension — they ship nowhere and `tests/fixtures/substrate-root/README.md` explicitly flags the "shipped assets" question as open. But the suite's framing and the absence of any `src/` home are exactly why the operator must call it, not the lane.
