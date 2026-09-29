## v1.5.9 (2026-09-30) — Contract Authority, Work Contracts & Methodology Registry

SkillWeave v1.5.9 delivers the Contract Authority framework, cross-repo contract CI, work contracts, methodology registry, bounded repair, assay methodology, and infrastructure fixes for the release pipeline.

### Contract Authority
- **Cross-Repo Contract CI**: Fail-closed contract validation against the published SDK 0.2.0 schema set, with a drift counterproof job that proves one-byte schema drift turns the gate red.
- **Core → SDK 0.2.0 Migration**: Core now validates against the published `skillweave-sdk` contract set instead of inline schemas.
- **Council AttributedResponse**: `copy()`, `asdict()` and `pickle` now survive round-trips without losing attribution metadata.
- **Skill Inventory Fixes**: Resolved 6 inventory test failures caused by the contract migration.

### Features
- **Work Contracts**: Discriminated `SubjectRef` variants (repository, content, configuration, deployment, incident) with `WorkContract` binding authority, write scope, irreversible actions, verification, rollback, and budget. Exact-brief contract via SHA-256 digest.
- **Methodology Registry**: Versioned registry for REX, Ralph, Gauntlet, and Assay methodologies with compatibility matrix. Legacy `execution_model` values migrate deterministically.
- **Assay Methodology**: Append-only state machine (PIN → HYPOTHESIS → PROBE → MEASURE → CLASSIFY → REMEDIATE → PASS/HOLD) with conservative, moderate, and unicorn autonomy policies.
- **Bounded Preflight Repair**: Versioned `PreflightFailure` contract with grounded repair engine. Repairs are 1:1 substitutions preserving write scope; repeated fingerprints, target drift, and authority expansion trigger holds.
- **Blueprint Data Contracts**: PRDs crossing storage, process, adapter, telemetry or public-API boundaries now require exact versioned data contracts instead of prose.
- **Discovery Trace Graph**: Digest-bound trace from discovery Markdown to epics/tasks/deferrals. Silent loss of mandatory problems, missing links, and post-grounding digest drift all fail closed.
- **Grounding Manifest**: Bounded, deterministic, read-only scanner producing digest-bearing manifests with language detection, symbol citations, and gap tracking.
- **Blueprint Ticket Handshake**: Authority-aware planning ticket handshake with four terminal states (linked, created, not_applicable, needs_authority) and duplicate-title prevention.
- **CMS Operate Profile**: Provider-neutral CMS Ops profile with Blueprint-always contracts, generic roles, and conditional skip-reasons.
- **Edition Contracts**: Consent boundary export and Community/Pro contract separation.
- **Telemetry**: Privacy-safe local empirical telemetry with shape-based path detection (not vendor-prefix denylist), disabled and local-only by default.
- **Repository-Scoped Lane Contracts**: `RepositoryTarget` binding for lane contracts.
- **Durable Async Run Lifecycle**: `start`/`inspect`/`kill` with durable state.
- **Evidence-Backed Lane Outcome Resolution**: Lane outcomes resolved from evidence, not stdout.

### Infrastructure
- **Release Pipeline**: Flattened `release_title_policy` fields to `.version.yaml` root level for `ops-engine` parser compatibility. Updated `TITLE_GATE_PY` regex to match root-level keys.


## v1.5.8 (2026-09-27) — Generic Routing, Messaging & Validation Expansion

SkillWeave v1.5.8 completes the Generic Lifecycle execution capability. It introduces a modular routing adapter, dedicated worker messaging channels, deterministic blocker slicing, and massive vertical and horizontal testing expansions for both Software Delivery and Research Synthesis pipelines.

### Highlights
- **Generic Harness Adapter**: Replaced hardcoded OpenClaude references with a `HarnessAdapter` interface and an agnostic `AdapterRequest` contract, paving the way for multi-harness routing (e.g. Faigate / OmniRoute).
- **Worker Direct Messaging**: Agents now emit structured `DirectMessage` records via a dedicated file-based JSONL channel (`SW_MSG_CHANNEL`), decoupling worker signals from fragile stdout log scraping.
- **S3-Gate Validation Slicing**: Blocker parsing now fails closed if `(lane_id, domain)` matches multiple records identically, preventing runaway spawn loops when blockers are ambiguous.
- **Contract Reality Check**: Delivered a 20-criterion alignment map mapping the PRD requirements to their implementations and testing coverage.
- **Extensive Validation Profiles**: Added exhaustive Golden Scenarios for the generic lifecycle, fully modeled research and software delivery profiles, and breadth integration tests.

## v1.5.7 (2026-09-27) — Lifecycle Closeout and Remediation

SkillWeave v1.5.7 introduces the fully integrated Lifecycle Closeout system, bringing robust transactionality, tamper-evident telemetry, and durable retrospective persistence to the pipeline.

### Highlights
- **Transactional Closeout**: Workspaces are now removed via an idempotent, fsynced journal. Duplicate mutations are prevented, and crashes during the mutation window are handled gracefully.
- **Tamper-Evident Telemetry**: Privacy validations now cover both keys and values, proactively redacting secrets and absolute paths from the telemetry stream.
- **Durable Retrospectives**: Retrospective synchronization is now fully atomic (`mkstemp` -> `os.replace`), ensuring no torn reads or data loss during sync.
- **Strict Evidence Boundaries**: Missing or incomplete evidence now strictly forces a hold instead of silently closing out.
- **Remediation Slicing**: Introduced a new `byteplus-deepseek-pro` escalation pathway to slice multi-domain failures into disjoint micro-lanes and repair blocking gates autonomously.

# SkillWeave Changelog
## v1.5.6 (2026-09-26) — Universal Entry and Onboarding Ecosystem

SkillWeave introduces a universal CLI entry facade and a comprehensive operator onboarding ecosystem. These additions provide a unified interface to start, continue, inspect, or onboard, while securely persisting operator profiles and state contexts. 

### Highlights
- **Universal CLI Router**: `skillweave start`, `continue`, `inspect`, and `onboard` subcommands are now fully routed and validated through the shared `EntryService` contract.
- **Onboarding Profile Generation**: Captures and persists operator intent, role, and autonomy boundaries in durable, encrypted YAML profiles.
- **Strict Integration Testing**: New harnesses test end-to-end integration across entry facades, adapters, profile verification, and CLI router parity.
- **Product-Neutral Release Gating**: Forgejo release workflows are now entirely decoupled from hardcoded product titles; `release_title_policy` is loaded dynamically from `.version.yaml`.

## v1.5.5 (2026-09-25) — Workspaces and dispatches you can inspect before they act

SkillWeave can now prepare multi-repository workspaces and operator dispatches
without hiding the ownership, lifecycle, or exact acceptance criteria behind
them. The release makes those boundaries inspectable before an ops worker is
allowed to write, and rejects incomplete or ambiguous contracts instead of
guessing.

### Highlights
- Workspace manifests are validated strictly, provisioned from exact base
  revisions, and released or cleaned up only with explicit ownership receipts.
  Foreign, dirty, or currently active worktrees are refused rather than removed.
- A read-only multi-repository inventory exposes worktree state before cleanup,
  including the reason an entry cannot be touched.
- Operator dispatches now have a preview and observer surface before ops starts,
  criterion-aware events, and consistent heartbeat and terminal semantics for
  both inline and fan-out execution.
- Dispatch contracts require every requested acceptance criterion exactly once.
  Missing, duplicate, empty, or out-of-range criterion groups fail closed, while
  valid non-empty regrouping remains supported.

### Reliability
- Workspace lifecycle, cleanup receipts, observer previews, profile effects,
  event streams, and exact-once dispatch criteria are covered by unit,
  integration, contract, and release-gate tests.
- The reviewed release candidate passed the complete 1.5.5 product and planning
  gates before entering the release branch.

### Upgrade
`pip install --upgrade skillweave`. Existing profiles remain compatible; use the
new preview and workspace inventory surfaces to inspect generated work before an
operator dispatch begins.

**Full changelog:** [CHANGELOG.md](https://github.com/LangeVC/skillweave/blob/v1.5.5/CHANGELOG.md) · **Diff:** [v1.5.4...v1.5.5](https://github.com/LangeVC/skillweave/compare/v1.5.4...v1.5.5)


## v1.5.4 (2026-09-10) — A Capacium publication that actually authenticates

The path that publishes a tagged release to the Capacium Exchange was wired for
Trusted Publishing but never asked for a token, so every tag since v1.3.12
reached the CLI with an empty credential and refused. This release turns that
path on. No runtime or skill code changes.

### Fixed
- The Capacium publication step now requests a short-lived publish token through
  OIDC (`id-token: write` plus `use_oidc: true`) instead of arriving with an
  empty `CAPACIUM_API_TOKEN`. No long-lived Capacium secret is stored in the
  repository; the Exchange derives the publisher owner from the OIDC claims and
  issues a token scoped to exactly that owner and repository.

### Changed
- The version-sync helper fetched by the release gate is pinned to ops-engine
  `v3.4.2`, the same release the release workflow installs as its engine, so the
  repository names one ops-engine version rather than two. The helper is
  byte-identical across `v3.0.0`, `v3.4.0` and `v3.4.2`, so the gate's verdict is
  unchanged.
- The `check-manifest` diagnostic output is English, matching the rest of the
  repository's tracked files. Only strings moved; its exit-code contract is
  unchanged.

### Upgrade
`pip install --upgrade skillweave`. `src/` and `skills/` are identical to 1.5.3,
so nothing you call changes.

**Full changelog:** [CHANGELOG.md](https://github.com/LangeVC/skillweave/blob/v1.5.4/CHANGELOG.md) · **Diff:** [v1.5.3...v1.5.4](https://github.com/LangeVC/skillweave/compare/v1.5.3...v1.5.4)


## v1.5.3 (2026-09-08) — Releases that reach both forges, and a history that is whole

Every release from 1.3.12 to 1.5.2 was tagged on both forges while only Forgejo
carried a release object, so the public "latest release" stood at 1.3.11 for nine
days, and the notes that did exist were raw commit dumps. This release replaces
the machinery that produced that and repairs the history it damaged. No runtime
or skill code changes.

### Highlights
- A release is published to every declared forge, or the run goes red. There is no
  longer a path where one forge succeeds quietly and the other is skipped.
- The release body is the changelog entry for the tag. A version with no entry is
  refused, instead of falling back to commit subjects.
- The changelog carries all 41 released versions again, each with a date.

### Added
- `.ops.yaml` declares the release destinations and this repository's tracker
  prefixes. No forge repository, API host or CI variable survives as a literal in
  the workflow layer.
- Three gates that run before any release object exists on either forge: the
  release title, the presence of a changelog entry, and whether that entry still
  addresses an internal tracker rather than a reader.
- The canonical tag is pushed onto the mirror before publication, and both tags
  are peeled and compared after it, so the release API cannot invent a tag at the
  mirror's default-branch head.

### Changed
- Publication is handed to the ops-engine release handler, pinned to 3.4.2 and
  version-gated after install. The workflow no longer reimplements it.
- The mirror destination is read from repository variables with a double match and
  an existence and permission preflight, instead of being assumed reachable.

### Fixed
- Release notes are no longer a commit-log dump assembled with `printf`, which
  left every line separator as a literal backslash-n: the 1.5.2 body was 4356
  characters rendering as four lines.
- The release history deleted at the 1.4.0 bump is restored — 597 lines, covering
  everything at or below 1.3.13.
- Six tagged versions that never carried a changelog entry have one.
- `pytest-asyncio` is declared. Without it a clean environment runs the async
  suites as unknown marks and 32 tests fail without the code being at fault.

### Upgrade
`pip install --upgrade skillweave`. `src/` and `skills/` are identical to 1.5.2,
so nothing you call changes. Installing from source also needs the sibling SDK
checkout installed first; the dependency on it has never resolved from an index.

**Full changelog:** [CHANGELOG.md](https://github.com/LangeVC/skillweave/blob/v1.5.3/CHANGELOG.md) · **Diff:** [v1.5.2...v1.5.3](https://github.com/LangeVC/skillweave/compare/v1.5.2...v1.5.3)


## v1.5.2 (2026-09-02)

### Substrate
- **Three-tier model**: package defaults (shipped under `skillweave/assets/`), durable input under `skillweave.config/`, and generated output under `.skillweave/`, which is git-excluded. A team's tuned catalogue lives in `skillweave.config/` and survives upgrades; generated state never ships and never tracks.
- **Generic substrate contract**: `direction` (input/output), `durability` (authored/generated, with `ephemeral` as a first-class value), and `disclosure` (public/private) declared per area, plus a backing-store seam that resolves an area's store from its declaration rather than from a `.git` directory. Durable areas in a non-git workspace sync through the configured org planning repository and are reported at-risk (not silently accepted) when no store is reachable.

### Mirror
- **Single mirror workflow**: the visibility-gate, the release-tag version-gate, and the mirror job are one workflow ordered by `needs`, with a destination guard that proves the GitHub destination is reachable before any push instead of failing inside one.

### Version topology
- **`decoupled_member_pins`**: the bundle and the runtime carry the train version; the `capabilities[]` member pins and the 13 skill capability files are informational and may lag the bundle together. A bundle bump can be packaging alone, so a plain bump moves only the two required locations (`pyproject.toml` and `capability.yaml`) and leaves the pins and member files where they are. The pin never diverges from its own member file — that consistency is owned by `scripts/check-manifest.py`, not by the release gate.

### Testing
- **Hermetic suite**: tests that depended on untracked local state (sibling checkouts, the git-excluded `.skillweave/` tree) now skip with an explicit reason when that state is absent, so a fresh `git archive` clone reproduces a green suite.

### Onboarding
- **Substrate rule taught**: onboarding states the substrate rule, writes the anchored `/.skillweave/` gitignore exclusion, records the private planning-repository answer, and refuses to leave substrate content tracked when the origin remote is public.


## v1.5.0 (2026-09-01)

### Features (Production Agent-Native)
- **Crash Recovery**: Orphan-, Worker-, and Coordinator-Crash-Recovery that reconstructs execution DAGs from persistent context without brittle transcript parsing.
- **Context Limits & Checkpointing**: Configurable token profiles (e.g., 120k `no_new_task`, 150k `checkpoint`, 170k `stop`) to bound AI execution budgets safely.
- **Operator Agent Escalation**: `operator_agent` with strict deterministic decision scopes. Irreversible choices automatically escalate before mutating state.
- **Persistent Policy**: Idempotent compensation, backoff, and retry budgets ensuring a crash loop doesn't double-charge tokens.
- **Remote Workspace Sandbox**: Distributed execution honoring the precise `skillweave-workspace` contract without leaking orchestration state.
- **Soak & Langlauf**: Added extensive multi-hour degradation limits and `DoubleStartGuard` process protection.


## v1.4.0 (2026-08-31)

### Features
- **Public Control Plane**: Complete separation of integration tests and host-neutral API paths.
- **MCP Guard Rails**: Full enforcement of negative authority limits in the MCP server.
- **Skill Portability**: All 13 skills audited and refactored for host-neutral Command/Intent contracts (experimental).
- **Harness Provenance**: Hermetic Fakes and OpenCode-DeepSeek proofs with append-only receipts.
- **Faigate Diversity**: Expanded ops, review, and comparison routing profiles with Anti-Masking detection.
- **Dispatchability**: Explicit execution queues, topological phase batching, and metadata validation.

### Deprecations
- **SW-DEPR-001**: `skillweave.executor` is deprecated. Test doubles moved to `skillweave.legacy.test_double`.

---

## v1.3.13 (2026-08-31) — Dynamic Routing and Anti-Masking

- **Dynamic Routing Policy Engine:** Added support for capability-based routing and scoring (DR-002, DR-004).
- **Anti-Masking:** Faigate adapter now parses `served_by` headers to detect and flag silent fallbacks (DR-003).
- **Council Deduplication:** The council engine drops duplicated models and triggers the Routing Policy Engine for dynamic replacement (DR-005, DR-006).

## v1.3.12 (2026-08-30) — Forgejo-first release provenance

1.3.11 shipped with an ambiguity at the release seam: the visible GitHub release
object was created by a GitHub-side workflow, not delivered by the mirror from
the Forgejo canonical. This release resolves that ambiguity before the release
line is cut again.

### Release artifact policy

The release line follows one artifact policy across three surfaces, and every
future release is verified against it by the Forgejo-first release contract:

- **Runtime** — `pyproject.toml` carries the one runtime version; it is the
  declared `source_of_truth` and the version a release tag derives from.
- **Bundle** — `capability.yaml` carries the same version as the runtime; its
  member `capabilities[]` pins are a manifest, not a separate version line.
- **Skill capabilities** — each `skills/skillweave-*/capability.yaml` carries
  the same version as the runtime under the lockstep policy.

A release is one immutable commit: the Forgejo release object, the canonical tag
target, the GitHub mirror tag and the distribution receipt must all refer to the
same commit. Zero Forgejo release objects, or more than one, fail the contract.

### Release path reconciled with the org model

- A single release-object producer remains: the ops-engine `ReleaseHandler` on
  Forgejo `tag_push`. No SkillWeave GitHub workflow creates a release object.
- GitHub-side workflows are distribution-only after mirror input. They cannot
  create or move the canonical tag, alter Forgejo release truth, or publish from
  an unverified branch.
- Capacium publication is triggered by the verified immutable release/tag path.
- The dormant PyPI job is removed; there is no PyPI distribution claim.

### Changelog history restored

The 1.3.8, 1.3.9 and 1.3.10 entries were reconstructed from the immutable tags,
not backfilled from memory, and are listed below.

## v1.3.11 (2026-08-30) — Dispatch operations, and a gate that can fail

1.3.10 closed the Council namespace. This release closes the operational gap
underneath it: structured jobs, strict review, harness adherence, model
allocation, observation, and a transfer catalogue, proven by a gate that
holds itself to the same standard it applies to the code.

### Dispatch operations

- **Append-only receipts and typed terminals.** Multi-round receipts preserve
  prior bytes and keep process, task, evidence and gate outcomes separate. A
  zero exit code no longer implies a passing gate.
- **Collision-safe topology and integration eligibility.** Missing commits,
  detached HEAD, dirty non-allowlisted state, a stale base, an omitted sibling
  and a SHA changed after review each fail integration closed.
- **Strict review loop.** A REVIEW_FAIL produces finding dispositions, a bounded
  correction, controller verification and a fresh cold REVIEW_PASS before any
  dependent work starts.
- **Four-harness adherence profiles** with strict digest, bypass and
  role-authority checks. Real proof status stays per harness and per machine;
  no stable transport-parity claim is made before 1.4.
- **Provider-neutral model policy** covering complexity, risk, cost and
  escalation without a hardcoded dispatch-provider default.
- **Semantic observation** with deterministic live and replay projections and
  read-only observer authority.
- **Transfer catalogue.** Dispatch learnings are stored with resolvable
  provenance, observed scope and contradictions. Retrieval is advisory and
  changes no policy, profile, topology, state or gate.

### Routing

- **Minimal adapter mapping for Faigate and OpenRouter.** Council profiles keep
  provider-agnostic names; `translate_model_id` remains the single translation
  point and the alias tables are its provider backend, applied exactly once
  after the namespace is resolved. The dynamic registry that replaces this
  mapping is planned for 1.3.13.

### Release hygiene

- `jsonschema` is declared in the `dev` extra, so a clean environment collects
  the contract tests instead of failing on import.
- The discovery tracking-log tests read tracked fixtures instead of relative
  git-ignored paths, so the full suite is green in every worktree rather than
  carrying two standing failures.

### The gate

`SW-GATE-1311` proves `DISPATCH_OPERATIONS_PASS` across thirteen criteria, with
two independent reviewers of different model classes inspecting one immutable
subject. Criteria that a test process cannot observe are controller-attested,
and the attestation now binds the candidate SHA: a dual pass recorded against
any other subject fails closed inside the repository, not only in tooling.

## v1.3.10 (2026-08-28) — Council profiles revert the provider prefix

Reconstructed from the immutable `v1.3.10` tag. A version-only release: the
`faigate/` model-id prefix introduced in 1.3.9 was reverted, because the council
provider talks to Faigate natively and must not carry the prefix. Runtime,
bundle and all thirteen skill capabilities move to 1.3.10 in step.

## v1.3.9 (2026-08-28) — Council profiles carry the Faigate prefix

Reconstructed from the immutable `v1.3.9` tag. Council profile model ids were
prefixed with `faigate/` (`faigate/gpt-4o`, `faigate/deepseek-v4-pro`, and the
chairman ids likewise) across the four council tiers. Runtime, bundle and all
thirteen skill capabilities move to 1.3.9 in step.

## v1.3.8 (2026-08-28) — Lazy runtime imports and capability sync

Reconstructed from the immutable `v1.3.8` tag. Top-level runtime imports in the
routing and fanout layers were made lazy so the engine core imports without
`runtime/` present: `ProcessResult`, `ArtifactReceipt`, `EvidenceType` and
`ObserverRuntime` resolve on first use rather than at module import. Runtime,
bundle and all thirteen skill capabilities move to 1.3.8 in step.

## v1.3.7 (2026-08-22) — Self-hosting comes home, and the gate holds it to its word

1.3.6 closed the dispatch seam and taught the council to verify who answered.
This release closes a different gap: the pipeline that ships SkillWeave could
not ship itself. The multi-lane self-hosting work makes the release line
reproducible inside its own machinery, and the ready-to-release amendment ties
the version and CI net together so a declared release is what the gate checks.

### Self-hosting multi-lane

- **The release line runs its own lanes.** The self-hosting gate fixture
  (`tests/gate_b06/test_self_hosting_multilane_gate.py`) drives parallel,
  conflict, SHA, review, and coordinator-kill fixtures under `bash -eo
  pipefail`, with the five W3-L1 hermetic unit suites, and emits
  `SELF_HOSTING_MULTI_LANE_PASS` only when every fixture holds.

### Ready-to-release amendment

- **GLE-020 lazy-surface restoration.** Optional runtime surfaces resolve on
  first access rather than eager import, so the engine's core imports without
  `runtime/` present.
- **Frameworks anchor derivation.** The capability frameworks list derives
  from the installer's declared agent targets, never re-typed in
  `capability_sync.py`.
- **Version and CI net.** `.version.yaml` declares the version locations; the
  release readiness gate enforces every declared manifest carries the release
  version, and the changelog is written by hand, not rewritten by a bump.

### Notes

All fourteen manifests (root plus thirteen skills) move to 1.3.7 in step. The
prose changelog remains a hand-written history and is not an auto-bumped
location.

### The core imports without `runtime`

Recorded 2026-08-13, before this release was cut. It is the consumer-facing
detail behind the lazy-surface restoration named above.


`import skillweave` used to fail with `ModuleNotFoundError` as soon as `runtime/`
was physically absent: `__init__.py` imported `.execution` and `.observation`
eagerly, and those pulled in `runtime` through `state_machine.py`,
`gate_policy.py` and `event_logger.py`. A consumer embedding the engine could
therefore embed nothing less than all of it.

Resolved through PEP 562: fourteen runtime-reachable names resolve on first
access. `OPTIONAL_SUBPACKAGES = ("runtime",)` is declared explicitly rather than
left implicit.

**A behaviour change that can affect consumers.** The public API is unchanged
character for character — `__all__` still carries the same 50 names, and
`from skillweave import *` yields the same set. What goes away is the eager
binding of the submodule names:

```python
hasattr(skillweave, "execution")              # True  -> False
hasattr(skillweave, "observation")            # True  -> False
hasattr(skillweave, "execution_integration")  # True  -> False
dir(skillweave)                               # 16 entries fewer

import skillweave.execution                   # unchanged
from skillweave.execution.batch_planner import BatchPlanner   # unchanged
```

That lies outside the `__all__` contract and is nowhere attribute-chained inside
this repository. Anyone using `hasattr` for feature detection should switch to
`importlib.util.find_spec("skillweave.execution")`.

## v1.3.6 (2026-08-18) — The seam closes, and the council learns who answered

1.3.5 shipped every part of a dispatch except the seam that joins them: a
profile could name a target tool and its launch command, and nothing started
it. This release closes that seam, and a second one nobody had looked for —
whether the model that answered is the model you asked for.

### The seam

- **`launch_command` is read and the tool is started.** A role with a
  `ToolSpec` is launched, the work is handed over on standard input, and the
  result comes back as an `ArtifactReceipt` rather than as free text. The
  adapter branches on no tool name.
- **A role without a `ToolSpec` runs in place and is recorded as such.**
  Staying in the current harness is a declared configuration, distinguishable
  afterwards from a dispatch that silently did not happen.
- **A profile ships as data**, `profiles/example-standard.yaml`, as a worked
  example to copy and adapt rather than a default that loads itself.
- **A timeout can be set and is reported as itself.** `dispatch()` and
  `launch_from_role()` take one, defaulting to `DEFAULT_DISPATCH_TIMEOUT`
  (900s). The record keeps *declared* apart from *terminated*.
- **The executing harness is declared, never guessed.** `determine_harness`
  reads `SKILLWEAVE_HARNESS`; absent data never reads as a caller's statement.
  Harness and profile stay separate.

### The council

Faigate substitutes silently: an unknown model id returns HTTP 200, a
well-formed completion and no error field, answered by a different model. All
nine ids in `ROUTER_PROFILES` were substituted when measured — `gemini-pro`
included, which *is* in `/v1/models`. A four-seat run had three seats answered
by one model, the chairman among them, and stage 2 had that model rank its own
three anonymised answers.

- The answering model is read from the response envelope, never inferred.
- `min_models_required` counts **distinct** answering models.
- A substitution is surfaced per seat; a self-ranked stage 2 is visible in the
  record; the chairman's own substitution is recorded.

### Repairs

- **Availability was checked against the council's casting**, so a profile
  pinning a served model was refused with a false claim.
- **A hardcoded ten-second socket timeout** made `timeout_per_model` inert.
- **A degraded council named who, never why.**
- **An empty completion counted as an answer**; one guard now covers all four
  providers.
- **`dispatch` was both a module and a re-exported function.** The module keeps
  its name; the function is `dispatch_role`.

### Compatibility

The thirteen bundled skills stay at 1.3.0 — none changed. Two behaviours move
toward the documented contract: an empty completion raises, and a collapsed
council fails its minimum. Code importing `dispatch` from the package root must
use `dispatch_role`.

## v1.3.5 (2026-08-17) — Contracts worth trusting, and a dispatcher on top

Ten runtime contracts existed since 1.3.0 but did not hold what they promised.
Each defect below was proven by a test that fails against 1.3.0 and passes
here; where no such test was constructible, the criterion says so instead of
inventing one.

### Repairs

- **Compare-and-swap detects conflicts again.** The guard read
  `connection.total_changes`, which is cumulative per connection and therefore
  never differs — every stale write reported success. Now `rowcount`.
- **Journal sequences no longer collide.** Allocation and INSERT are one
  `BEGIN IMMEDIATE` transaction. Eight writers appending 200 entries persisted
  33 before; all of them now.
- **AuthorityGuard fails closed.** An unknown action returned True. It returns
  False, and an undeclared role holds no capability at all.
- **Preflight validates the whole envelope** against resolved paths, not a
  prefix of the string.
- **Checkpoint, handoff and evidence survive the process.** They lived in
  memory; they live in the same store as runs and transitions.
- **The state vocabulary has one truth.** `STOPPED_BEFORE_B06` was in the enum
  and not in the schema.
- **`busy_timeout` is set explicitly**, with a stated reason. The value does not
  change behaviour — `sqlite3.connect` already defaults to 5000. It stops a
  concurrency-critical property from depending on a library default nobody chose.
- **`executor` says what it is.** `execute_step` never executed anything;
  it is `simulate_step`.

### Arbitration and dispatch

- **Write scopes are claimed and released**, not merely checked, and the claim
  is persistent. Overlap is decided on resolved absolute paths.
- **DagScheduler** turns a dependency graph into batches, rejects cycles by
  name, enforces `max_parallel`, and refuses to release the dependents of a
  failed gate. It starts no process and knows no runner.
- **RunnerAdapter** starts real processes, binds their output to the run as
  `ArtifactReceipt` evidence rather than free text, distinguishes exit code
  from signal, kills the whole process group on cancel, and treats a worker
  that dies without a result as a failure with a message.
- **A batch bounds a session.** Both emit that boundary explicitly and refuse a
  sequence that does not declare one.

### Routing — leaving the model behind

This is the part that makes SkillWeave usable from a model-bound harness. A run
can be orchestrated from Codex, Claude Desktop, Claude Code or Antigravity and
executed elsewhere — primarily OpenCode — with the model resolved through
Faigate rather than inherited from wherever the operator happened to sit.

- **RoutingProfile** is declarable data: a model per role, a tier Faigate
  resolves, limits, and an optional target tool. Roles are data too, with
  `observer` wired to the existing runtime observer. A role holding both
  `can_mutate_run_state` and `can_approve_gate` is refused at load — that
  combination is self-approval.
- **The Faigate adapter left `council/`** for a shared routing surface; the
  council consumes it through a re-export shim. `ROUTER_PROFILES` is subsumed,
  not duplicated.
- **Three tier vocabularies were reconciled** with the mapping written down.
  `expert` deliberately stays a router profile and not a tier: it varies which
  models run, not how much work is done.
- **Three routing modes**: `pin` decides nothing, `auto` derives the tier from
  the complexity promptchain-generate already computes, and `hybrid` lets auto
  decide within declared bounds. A bound that moved the decision is recorded as
  an adjustment, never as the original decision.
- **The record separates requested from resolved** — tier, mode, the input that
  drove it, every adjustment, and what Faigate actually returned.
- **Dispatch is optional and per role.** A role without a target tool runs where
  it is. Staying inside the current harness is a first-class configuration.

### Notes

The thirteen member versions in `capability.yaml` stay at 1.3.0. No skill
changed in this release; only the runtime and the new routing layer did, and the
bundle version moves because its composition did.

## v1.3.0 (2026-08-12) — Runtime Foundation

Der dokumentierte Lifecycle war bis hierher nicht durchsetzbar: `executor.py`
war laut eigenem Kommentar eine Simulation, die State Machine lag im
Arbeitsspeicher, der Event Logger verschluckte I/O-Fehler, die Gate Policy
kannte keine Rollen. Diese Version schliesst genau diese Luecke.

- Autoritative Run State Machine mit Persistenz
- Schreibvalidierung des Statusvokabulars; ein Wert ausserhalb des Enums wird
  abgewiesen statt geschrieben
- Append-only Event Journal mit Persist-before-Ack
- Rollen- und Transition-Autoritaet; Selbstfreigabe wird strukturell verhindert
- Session Envelope und Preflight gegen produktfremde Prompts
- Immutable Artifact und Evidence Registry
- Typed Handoff Broker mit `cold_start_bundle`
- Checkpoint und Resume mit Revalidierung der Umgebung
- Observer Runtime mit Offset und Lease
- Cross-Run Gate Reconciliation
- Verifizierbare Kontextbeschaffung; Prosa wird als Kontextquelle abgewiesen
- Conformance-Readiness des Kerndatenmodells
- Neun Golden Negative Fixtures, jede aus einem real aufgetretenen Fehler der
  Welle CP-OPT-2026-08-05-W1 abgeleitet

Der Degradationsdetektor liegt als eigenstaendiges Modul `skillweave_degraded`
neben dem Package, damit er erreichbar bleibt, wenn `skillweave.runtime` fehlt.
Sein `fallback_version` bleibt bewusst auf `v1.2.0` — das ist die letzte
Version ohne Runtime.

Bekannte Einschraenkung: Der GNF-Gegenbeweis dokumentiert die Abwesenheit des
Schutzes auf `v1.2.0`, demonstriert aber nicht, dass die Fixtures den Defekt
fangen. Verfolgt als `SW-SCOPE-003`.

## v1.2.0 (2026-08-11) — Branch Topology Consolidation

Consolidates three previously unintegrated development lines onto a single baseline.
Wave `CP-OPT-2026-08-05-W1`, session S02, iteration I00. Independently reviewed and
reproduced by the cross-product review authority (`SKILLWEAVE_TOPOLOGY: AUTHORIZED`,
`S02_RELEASECHAIN_AUTHORIZED: true`).

Merge order: `EN-FIRST 50c3012` → `G0C bc1bd41` → `G2A 3c24cb1`, zero pairwise conflicts.

- **FEATURE**: SW-N-G2A neutrality adapters — ProcessDefinition, EventGrammar, EVR and compiler contracts, executable and test-covered, with no Capacium or Elementeer imports.
- **CHANGED**: Canonical skill catalog is now EN-first. All 13 skill descriptions rewritten in English; boundary declarations corrected.
- **CHANGED**: Skill count 14 → 13. The legacy `skills/launch/SKILL.md` relic is removed; `skills/skillweave-launch/` remains the canonical launch skill.
- **CHANGED**: Domain corrected from `skillweave.dev` to `skillweave.xyz` across licensing and endpoint configuration.
- **FIXED**: SW-G0C release dead code removed from `launch/deployment.py`.
- **FIXED**: SW-N-G2A R2 process-pack handoff — both schema entries carried an empty `""` key instead of `"$id"`. Repaired to canonical URIs, digests recomputed, non-recursive manifest and receipt reissued, independently verified (`4ef1cf67`).
- **ADDED**: `tests/test_skill_catalog.py` and `tests/test_skill_boundaries.py`.

### Known issues

`BACKLOG-ENV-001` — pre-existing environmental test failures in `test_backlog_sync`,
`test_council` and `test_integration`. Fail-set is identical before and after this
consolidation; no regressions were introduced. Not addressed in this release.

### Not in this release

The runtime remains a planning and contract surface. `executor.py` is still a
simulation; the state machine is in-memory and the event logger writes local files
best-effort. Runtime integrity (authoritative run state, event journal, role
authority, typed handoffs, checkpoints, evidence registry, observer cursor) is the
subject of the next PRD, not of this consolidation.

## v1.1.0 (2026-05-26) — Studio Hook Binding Engine

- **FEATURE**: Hook binding engine with 4 execution types (Python HookAdapter, shell, SKILL.md injection, Capacium capability)
- **FEATURE**: YAML binding config with 3-source resolution (project > user > auto-discovered), dedup by capability+phase+position, priority sorting
- **FEATURE**: Execution chain with 4 failure modes (block, warn, ignore, retry), safe condition evaluator, timeout enforcement
- **FEATURE**: Auto-discovery of hook bindings from Capacium capability triggers (CloudEvents mapping)
- **FEATURE**: JWT license validation — HMAC-SHA256, offline-first, 7-day grace period, 14-day trial support
- **FEATURE**: Tier gate enforcement — Free tier bypass for `pre_discovery` (mentoring hooks), Studio required for all other hook points
- **FEATURE**: CLI with 6 commands: `hooks list`, `bind`, `unbind`, `test`, `discover`, `help`
- **FEATURE**: 2 reference capabilities — `ci-gate` (post_test HookAdapter) + `lean-startup` SKILL.md (pre_discovery)
- **TESTS**: 195 tests across hooks, binding, engine, discovery, licensing, CLI, and reference modules

## v1.0.2 (2026-05-21)

- **FIX**: Capacium manifests now stay synchronized across the root bundle and all individual SkillWeave skill manifests.
- **FEATURE**: Release gates now block releases when the requested release/tag version differs from any `capability.yaml` version.
- **FEATURE**: Published GitHub releases trigger the Capacium Exchange publish workflow.

## v1.0.1 (2026-05-17)

- **FEATURE**: Git Flow Convention — skills enforce minimum branching discipline (feature/fix/chore branches, dev → main merge path, preflight detection)
- **FEATURE**: Forgejo → GitHub mirror workflow (`.forgejo/workflows/mirror.yml`) — auto-push to GitHub on main push
- **IMPROVEMENT**: promptchain-execute Preflight Phase 1 now includes git flow state evaluation (Step 7)
- **IMPROVEMENT**: releasechain Pipeline Stage 5 expanded with branch model, merge flow enforcement, and config schema
- **IMPROVEMENT**: launch Pre-Launch-Checklist includes git flow check (dev → main merge path verified)
- **IMPROVEMENT**: Skill SKILL.md files synced between user-level and repo-level copies

## v1.0.0 (2026-05-17) — Forever Free
- **RELEASE**: SkillWeave v1.0.0 "Forever Free" — production-stable AI skill orchestration
- **FEATURE**: 5-level test pyramid (Lint → Unit → E2E Smoke → Acceptance → Evidence/Groundedness) with 3-state gate (PROMOTE/HOLD/ROLLBACK)
- **FEATURE**: File-based planning system (beans-pattern) — directory-as-state Kanban with YAML frontmatter tickets in `.skillweave/planning/{backlog,doing,done}/`
- **FEATURE**: Lifecycle Navigator with automatic phase detection from `.skillweave/` artifacts and intelligent next-step recommendations
- **FEATURE**: Progressive Disclosure architecture — Layer 0 (Wizard, 5 questions), Layer 1 (7 Meta-Commands), Layer 2 (13 Direct Skills)
- **FEATURE**: Handover signal validation with JSON schemas for skill-to-skill phase transitions
- **FEATURE**: Release report generation with date-stamped markdown templates
- **FEATURE**: Free/Studio tier boundary enforcement via `.skillweave/config.yaml`
- **IMPROVEMENT**: Council stability — structured JSON peer review, CouncilDegradedError, 60s/180s timeouts, graceful degradation with partial results
- **FIX**: Council Faigate port mismatch (8092 → 8090) causing OpenCode instability
- **FIX**: `normalize_manifest` always uses `pyproject.toml` version for skill manifests
- **IMPROVEMENT**: SerpApi and Perplexity MCP search provider support
- **IMPROVEMENT**: Google CSE provider removed (API deprecated by Google)
- **DOCS**: Unified getting-started guide for 3 personas (Power User, Indie Hacker, Non-Technical)
- **DOCS**: Complete reference docs for plan commands, testing flow, navigator detection, meta-commands, wizard flow
- **LICENSE**: Copyright updated to LangeVC.com, Apache 2.0 confirmed

## v0.8.5 (2026-05-08) — Search providers consolidated

Reconstructed from the immutable `v0.8.5` tag.

### Added
- SerpApi search provider (`SERPAPI_API_KEY`).
- Perplexity MCP support, falling back to the API key when no cookie jar is present.

### Changed
- The `FAIGNITE_*` environment variables are named `FAIGATE_*`, completing the provider rename.

### Removed
- The Google CSE provider. Google deprecated the API it used.

### Fixed
- Manifest normalisation uses the `pyproject.toml` version for skill manifests instead of preserving a stale one.


## v0.8.3 (2026-05-04) — More providers, fewer round trips

Reconstructed from the immutable `v0.8.3` tag.

### Added
- OmniRoute and 9router as council providers, kept separate from Faigate rather than folded into it.
- Perplexity as a search provider.

### Fixed
- Faigate availability is checked with one `GET /models` instead of one call per model.
- Faigate reads its token from the local token file, and its base URL points at the local gateway.
- The DuckDuckGo parser matches the result link directly, so search works without an external package.


## v0.8.2 (2026-04-30) — Capacium validation compatibility

Reconstructed from the immutable `v0.8.2` tag. Rolls up 0.8.1.

### Added
- Capacium validate auto-patch and manifest sync (0.8.1).

### Removed
- The `opencode-command` framework, from the default framework set, the bundle and every sub-skill. It failed Capacium validation.


## v0.8.0 (2026-04-30) — Council, multi-provider routing, and the Capacium bundle

Reconstructed from the immutable `v0.8.0` tag.

### Added
- `skillweave-council` — the LLM council skill, with web search and lifecycle integration.
- A root `capability.yaml` of kind `bundle`, which is what the Capacium Exchange indexes.
- A Capacium publish step on the release workflow, submitting to the Exchange on release.
- Multi-provider council routing: Faigate, OpenRouter, Kilo, Claw and LlmAI are detected, with a single-model fallback.

### Changed
- `FaigniteProvider` is `FaigateProvider`. The old name was a branding error.

### Fixed
- Capacium sync respects a skill's own version instead of forcing the `pyproject.toml` version onto it.
- The bundle manifest and the installer's skill list both carry `skillweave-council`.


## v0.7.0 (2026-04-27)
- **FEATURE**: 6 new agent-facing skills after promptchain pattern (SKILL.md + capability.yaml + sequence_type)
  - `skillweave-lifecycle` (plan): Bundle-Navigator, Phasen-Status, Entry-Point-Detection
  - `skillweave-discovery` (plan): Problemdefinition, User Research, Empathy Mapping, 11 Prompts
  - `skillweave-design` (mixed): Design-Thinking-Lens, Briefanalyse, Token-Extraktion, Evaluation
  - `skillweave-launch` (build): Pre-Launch-Check, Deployment-Koordination, Announce, Metrics-Vergleich
  - `skillweave-post-release` (mixed): Retrospektive, Feedback-Sammlung, Iterationsplanung
  - `skillweave-repo-health` (plan): Inventory-Scan, 5-Class-Klassifikation, Archive, Dedup, Hygiene-Report
  - `skillweave-observe` (plan): Execution Reports, Timing, Events, Memory — Read-Only Observability
- **FEATURE**: Launch infrastructure (src/skillweave/launch/)
  - `deployment.py`: GitHub Actions workflow_dispatch-Trigger, Health-Check, Rollback-Plan
  - `announce.py`: Release Notes Generator aus CHANGELOG.md, Multi-Channel-Formatting
  - `metrics.py`: Pre/Post-Launch-Metriken, Snapshot-Vergleich mit Delta-Report
- **FEATURE**: Post-Release infrastructure (src/skillweave/post_release/)
  - `retrospective.py`: Strukturierte Retro-Vorlage (Went Well / To Improve / Action Items P1-P3)
  - `feedback.py`: GitHub Issues-Feedback-Sammlung, Kategorisierung (bug/feature/improvement/question)
  - `iteration.py`: Backlog-Generator aus Retro + Feedback, Prioritäts-Scoring
- **FEATURE**: Repo-Health infrastructure (src/skillweave/repo_health/)
  - `scanner.py`: Inventory-Scan mit Typ, Größe, last_modified
  - `classifier.py`: 5-category rules-based classification (Active Core / Consolidation / Legacy / Deprecated / Needs Review)
  - `dedup.py`: MD5-Exact + Fuzzy-Content-Duplikatserkennung
  - `archive.py`: Move + Restore mit JSON-Manifest, dry_run-Pflicht
  - `report.py`: Hygiene-Score (A-F) mit konkreten Empfehlungen
- **FEATURE**: phases.yaml — alle 7 Phasen mit Skills + promptchain_types + global_skills-Sektion
- **FEATURE**: bundles.yaml — 5 Bundles mit sequence_types_used + entry_requires + estimated_effort
- **FEATURE**: Installer auf 13 Skills aktualisiert (12 SkillWeave + frontend-design)
- **FEATURE**: Design-Skill references: 5 UX-Prinzipien, 6 Workshop-Regeln, Token-Format, Elementify-Integration
- **FEATURE**: Lifecycle-Skill references: 7 Phasen mit Entry/Exit-Conditions, 5 Bundles mit Recommendation-Matrix
- **IMPROVEMENT**: Alle neuen Skills haben sequence_type (plan/mixed/build) für promptchain-generate-Kompatibilität
- **IMPROVEMENT**: Discovery-Skill mit 11 Prompts in 4 Gruppen (Empathy, Research, Framing, Output)
- **IMPROVEMENT**: Legacy prompt-chain aus Installer-Tabelle entfernt (nie existiert, kein Effekt)
- **IMPROVEMENT**: Release-Naming-Convention in auto-tag-release.yml enforced (SkillWeave vX.Y.Z)

## v0.6.0 (2026-04-27)
- **FEATURE**: 7-phase lifecycle model with entry/exit conditions and bundle system (Initiative 01)
- **FEATURE**: Discovery prompt library (11 prompts) and Design Thinking Lens (Initiative 02)
- **FEATURE**: Release readiness assessment, premature invocation detection, Launch skill separation (Initiative 03)
- **FEATURE**: Repo cleanup, dead code removal, archive with restore manifest (Initiative 04)
- **FEATURE**: Execution system with Ralph Loop state machine, batch planner, retry, gate policy, observation layer, execution memory, sidecar manager (Initiative 05)
- **FEATURE**: GitHub Action workflows (11 workflows), GitHub integration layer, auto-tag, auto-changelog, auto-issue, auto-PR, auto-docs-sync, release readiness gate (Initiative 06)
- **FEATURE**: Phase-aware onboarding flow with state persistence
- **FEATURE**: Phase boundary enforcement (soft) with violation logging
- **FEATURE**: Ideation module (quantity-first, wild ideas, separate evaluation)
- **FEATURE**: Assumption tracking with risk scoring and validation status
- **FEATURE**: Iteration quality framework with evidence-driven revision
- **FEATURE**: Release workflow with 5 gated sequential steps
- **FEATURE**: Launch skill placeholder with separate lifecycle phase
- **FEATURE**: Capability-based GitHub Actions (workflow-inventory, auto-tag, auto-release, auto-changelog, auto-issue, auto-PR, auto-docs-sync, release-readiness-gate, integration-test, auto-release-notes)
- **IMPROVEMENT**: promptchain-execute redefined as orchestration substrate (release logic moved to releasechain)
- **IMPROVEMENT**: All 510 tests passing across full system

## v0.5.6 (2026-04-26)
- **FEATURE**: Capacium badge and install section in README
- **FEATURE**: Content boundary enforcement for release artifacts (AGENTS.md + prerelease.yml)
- **IMPROVEMENT**: Version bump to 0.5.6 with updated documentation

## v0.5.5 (2026-04-26) — Scoring and validation fixes

Reconstructed from the immutable `v0.5.5` tag.

### Fixed
- Confidence scoring, parameter validation, and the test expectations that had drifted from both.


## v0.5.1 (2026-04-23) — Cleanup release

Reconstructed from the immutable `v0.5.1` tag. It finalises the 0.5.0 changes and adds no capability of its own.


## v0.5.0 (2026-04-21)
- **RELEASE**: SkillWeave Next Level Features v0.5.0
- **FEATURE**: Three risk modes (conservative, medium, unicorn) across all skills
- **FEATURE**: .skillweave folder structure with config, tracking, manifesto
- **FEATURE**: Persistent state manager with session recovery
- **FEATURE**: Configuration manager with mode interpretation
- **FEATURE**: GitHub Issues integration with Planning Poker estimation
- **FEATURE**: Backlog synchronization with .skillweave tracking
- **FEATURE**: Optional checklist execution with markdown checkbox tracking
- **FEATURE**: Optional Design-Thinking Lens for UI/UX decisions
- **FEATURE**: Enhanced capability-based routing with dynamic agent detection
- **FEATURE**: Modular templates foundation with example templates
- **FEATURE**: Community know-how prototype for pattern extraction
- **FEATURE**: Comprehensive testing suite with 100+ tests
- **FEATURE**: Updated documentation with configuration guide and examples
- **FEATURE**: Capacium packaging for all skills - each skill ships as a self-contained capability.yaml for cap install
- **IMPROVEMENT**: All Next Level features fully tested and integrated

## v0.4.4 (2026-04-10)
- **FEATURE**: Execute Skill v2 – Ralph Loop state machine with binary gates, batch planning, and safe parallelization
- **FEATURE**: Two-axis model for generate – separates `sequence_type` (plan/build/mixed) from `execution_mode` (rex/ralph_attended/ralph_overnight)
- **FEATURE**: Enhanced validate – checks parallelization readiness and separation of critical path vs sidecar lanes
- **NEW**: Three reference files for execute – `ralph-loop-state-machine.md`, `build-step-normalization.md`, `gate-policy.md`
- **IMPROVEMENT**: Execute now enforces write-scope ownership, safe parallel lanes, and binary completion signals
- **IMPROVEMENT**: Generate includes mode mapping table with 9 combinations (plan/build/mixed × rex/ralph_attended/ralph_overnight)
- **IMPROVEMENT**: Validate now identifies single-owner surfaces and integration gate requirements

## v0.4.3 (2026-04-09)
- **FIX**: Installed `skillweave-blueprint` for supported agents so Codex and related tools see the full five-skill SkillWeave set
- **FIX**: Cleaned up legacy SkillWeave duplicates under `~/.agents/skills` to prevent duplicate promptchain entries with stale descriptions
- **FIX**: Source/Target directory separation – installer now copies skills from development repo to `~/.skillweave` installation directory
- **FIX**: Removed duplicate git repository from `~/.skillweave` (target directory should be plain folder, not a git repo)
- **FIX**: Added warning when target directory is a git repository with cleanup instructions
- **NEW**: `update-local-skills.sh` script for developers to sync changes from development repo to installation
- **IMPROVEMENT**: Installer automatically copies skills from source to target when run from development repository
- **IMPROVEMENT**: Better logging and dry-run output for copying operations

## v0.4.2 (2026-04-09)
- **FEATURE**: Interactive installer with agent selection modes (`--interactive`, `--uninstall`, `--update`, `--troubleshoot`)
- **FIX**: Correct agent paths for Gemini CLI (`~/.gemini/skills`) and Qwen Code (`~/.qwen/skills`)
- **FIX**: Prevent creating directories for non-existent agents (no "file-leichen")
- **IMPROVEMENT**: Smart agent detection with numeric selection, `all`, or `none` options
- **DOCS**: Updated README with interactive installation section and corrected agent paths

## v0.4.1 (2026-04-09)
- **DOCS**: Complete README overhaul with "Product development flow on steroids" branding
- **DOCS**: Added DEVELOPMENT_WORKFLOW.md with standardized release process
- **DOCS**: Release conventions and "SkillWeave vX.Y.Z" naming standardization
- **IMPROVEMENT**: ASCII workflow diagram and enhanced architecture documentation
- **IMPROVEMENT**: Repository structure updates and clearer onboarding guidance

## v0.4.0 (2026-04-09)
- **FEATURE**: Blueprint Skill (`/skillweave-blueprint`) for structured PRD creation
- **FEATURE**: Enhanced Execute Skill with parallel execution and dependency analysis
- **FEATURE**: Enhanced ReleaseChain Skill with dual-mode (REX/Ralph Loop) execution
- **ARCHITECTURE**: Parallel execution core with dependency graph analysis
- **ARCHITECTURE**: Capability-based agent-agnostic design for any AI coding agent
- **DOCS**: Full workflow example and comprehensive testing suite
- **PERFORMANCE**: Optimizations for large projects (50+ tasks)

## v0.3.5 (2026-04-07)
- **FEATURE**: Intelligent `skillweave-promptchain-execute` with plan/build/mixed mode detection
- **NEW**: `skillweave-releasechain` development pipeline skill
- Execute skill now detects sequence type and adapts outputs accordingly
- Post-execution options: target audience (humanize/machinize/mixed)
- Build components can trigger automated development pipeline
- Releasechain handles: review, testing, iteration, commit, push, PR, release, changelog
- Better integration between execute and releasechain skills

## v0.3.4 (2026-04-07)
- **IMPROVEMENT**: Enhanced `skillweave-promptchain-validate` with better output handling
- Added user prompts for output format: Validation Report, Improved sequence only, Both separate files
- Added sequence type detection: plan mode, build mode, mixed
- Improved sequence must be complete (no placeholder references)
- Output format adaptation based on sequence type
- Better documentation of validation interaction process

## v0.3.3 (2026-04-07)
- **FEATURE**: Attachment detection for `skillweave-promptchain-execute` and `skillweave-promptchain-validate`
- Skills now accept prompt sequences as .md/.txt file attachments
- Updated descriptions, usage examples, and parameters
- Added attachment detection logic description
- Clarified that generate uses parameters only (no attachments)

## v0.3.2 (2026-04-07)
- **FIX**: YAML parsing in `skillweave-promptchain-execute` argument-hint
- Changed `inputs='{\"key\": \"value\"}'` to `inputs=\"[JSON]\"` to avoid YAML parser errors
- Fixes Codex and other agent compatibility issues

## v0.3.1 (2026-04-07)
- **FIX**: Multi-agent installer with correct paths and formats for all agents
- **FIX**: Opencode installation as single `.md` files to `~/.config/opencode/commands/`
- **FIX**: Claude Code/Codex/Antigravity paths to correct locations (`~/.claude/skills/`, `~/.codex/skills/`, `~/.antigravity/skills/`)
- **FIX**: Different installation formats per agent type (single files vs directories)
- **FIX**: Updated README with correct manual installation instructions
- **IMPROVEMENT**: Better error handling and logging in installer script

## v0.3.0 (2026-04-07)
- separate skill directories for each command: `skillweave-promptchain-generate`, `-validate`, `-execute`
- initial multi-agent installer script (had incorrect paths for some agents)
- license changed from MIT to Apache 2.0
- initial README updates for multi-agent installation

## v0.2.0 (2026-04-06)
- new `skillweave-promptchain` skill with direct `/skillweave-*` commands
- `/skillweave-promptchain-generate`, `-validate`, `-execute` commands
- improved README with new installation instructions
- updated multi-agent quickstart section

## v0.1.0 (2026-04-06)
- initial repository structure
- first `prompt-chain` skill
- initial docs
- initial schemas
- initial parser, validator, orchestrator, executor
- first examples and templates
