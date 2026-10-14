# Native execution & support matrix (SkillWeave 1.5.10)

This document is the **release-facing execution/support matrix** for the
SkillWeave 1.5.10 train. It answers one question for an operator, a reviewer or
an integrating agent:

> Which execution and support surfaces exist, what each one actually does, and
> what are their *honest* limits?

It is deliberately **derived, not narrated**. Every number, name and vocabulary
in it is read from the one authority that owns it in the tree — the rendered
support table in code, the on-disk inventory, the installed distribution, the
MCP tool registry — so this page cannot drift from the product without a
machine check (`tests/unit/test_1510_doc_truth.py`) failing. Where a surface is
*declared* but not *proven*, this page says so; where a fact is *unknown*, it is
labelled unknown rather than promoted.

Three acceptance criteria drive it:

1. the README / version / skill count / substrate map, the CLI howtos and the
   MCP / Assay support rows all **derive from the qualified release manifest**;
2. the **bare limits**, the **detected-versus-live-tested adapters**, the
   **unknown-capacity** rule, the **preview features** and the current
   **plaintext onboarding storage** behaviour are all explicit;
3. the **public examples contain no private paths, no commercial
   implementation, no credentials and no unverifiable always-OSS / future
   capability claims**.

---

## 1. The qualified release manifest this page derives from

There is **no single file named "release manifest"** in the tree. The *qualified
release manifest* is the composed set of the declared, machine-checked release
surfaces that this 1.5.10 checkout actually carries. Each row below names the
file that is its authority and the consumer that projects it into this page:

| Release surface | Authority (source of truth) | How this page derives it |
|-----------------|-----------------------------|--------------------------|
| Product version (candidate) | `pyproject.toml` `[project].version` (the `source_of_truth` in `.version.yaml`) | read directly |
| Distribution-bundle version | `capability.yaml` top-level `version:` (the second `required: true` surface of `.version.yaml`) | read directly |
| Version-surface inventory | `.version.yaml` `locations[]` | enumerated (below) |
| Skill inventory | `skills/*/capability.yaml` (one per skill) | `sorted(...)` over the on-disk dirs |
| Surface support table | `src/skillweave/cli/support.py` (`SUPPORT_TABLE_ID`) | **rendered by import**, not copied |
| Shared run-state interpretation | `Runtime` authorities projected by `support.py` | rendered by import |
| MCP tool set | `src/skillweave/mcp/server.py` `@mcp.tool()` registry | enumerated from the live registry |
| Methodology registry | `src/skillweave/runtime/methodology.py` `DEFAULT_REGISTRY` | enumerated by import |
| Substrate areas | `docs/substrate-map.md` | counted from the canonical table |
| Edition / preview boundary | `src/skillweave/runtime/edition_boundary.py` | constants read by import |

`README.md`, the CLI howtos (`docs/native-assay-execution.md`,
`docs/dispatch-2-report.json`) and the MCP / Assay support rows in this page all
read from exactly these authorities; none of them re-derives a second set.

### The two required version surfaces agree

- candidate (product) — `pyproject.toml`: **1.5.10**
- bundle — `capability.yaml`: **1.5.10**

`.version.yaml` declares `release_policy: decoupled_member_pins`: the *runtime
product* and the *distribution bundle* carry the train version, while every
member artifact (the bundle's `capabilities[]` pins and each skill's own
`capability.yaml`) is **informational and may lag** the bundle. So the 15 member
pins reading `1.3.11` is **not** a drift — it is the declared policy, and
`scripts/check-manifest.py` (not the version gate) owns the pin-equals-member
consistency.

### The released fact is peeled, not `HEAD`

The released fact is **stated by its rule and by the command that derives it**,
not by a printed hash. It is the latest canonical **annotated** tag, identified
by its **peeled** commit:

```sh
git rev-parse '<latest-annotated-tag>^{}'
```

The released identity is the tag's **peeled** commit, never branch `HEAD` and
never the tag object itself; the tag's own subject line is the canonical
release metadata. Neither a peeled commit nor a `HEAD` hash is printed in this
page — the value is derived from the repository at read time, so it cannot go
stale here. The full rule is in `docs/version-truth.md`, enforced by
`scripts/check-version-truth.py`.

`1.5.10` is this page's **candidate and bundle** version and a **released
version**: its `v1.5.10` tag exists in the repository. The released identity is
the tag's peeled commit, as derived below.

---

## 2. Skill inventory and substrate map

The skill set below is the **exact** `skills/*/capability.yaml` inventory
(**15** skills); `scripts/check-version-truth.py` fails if this list and the
on-disk inventory disagree.

| Skill | Type |
|-------|------|
| `skillweave-blueprint` | plan |
| `skillweave-council` | mixed |
| `skillweave-design` | mixed |
| `skillweave-discovery` | plan |
| `skillweave-entry` | facade |
| `skillweave-launch` | build |
| `skillweave-lifecycle` | plan |
| `skillweave-observe` | plan |
| `skillweave-onboarding` | plan |
| `skillweave-post-release` | mixed |
| `skillweave-promptchain-execute` | build |
| `skillweave-promptchain-generate` | plan |
| `skillweave-promptchain-validate` | plan |
| `skillweave-releasechain` | build |
| `skillweave-repo-health` | plan |

The substrate map (`docs/substrate-map.md`) declares **27** canonical
`.skillweave/` areas, plus the supplementary workspace areas and the durable
`skillweave.config/` tier. `.skillweave/` is generated, git-excluded state —
`tests/packaging/test_discovery_installed.py` asserts the installed
distribution ships **no** `.skillweave/` at all (`pyproject.toml` packages only
`py.typed`).

**Honest deviation (retained, not hidden).** `docs/substrate-map.md` records that
this repository currently *does* track `.skillweave/`, which invariant 5
forbids; five generated-mirror/policy files are read by the test suite as
fixtures. That is an openly recorded deviation, not a claim of compliance.

---

## 3. The versioned surface support table

`skillweave support --json` publishes the versioned support table
`skillweave.support/1`, whose state section is `skillweave.run-state/1` and
which speaks the `run-snapshot-v1` public contract. The rows below are the table
**as rendered from the live module** (`src/skillweave/cli/support.py`):

| Surface | Role | Status | Entry point |
|---------|------|--------|-------------|
| `entry` | decide | supported | `skillweave.cli.main:run_entry` |
| `run` | execute | supported | `skillweave.cli.run:main` |
| `start` | execute | supported | `skillweave.runlifecycle.cli:main_start` |
| `inspect` | operate | supported | `skillweave.runlifecycle.cli:main_inspect` |
| `kill` | operate | supported | `skillweave.runlifecycle.cli:main_kill` |
| `cancel` | operate | supported | `skillweave.runlifecycle.cli:main_cancel` |
| `resume` | operate | supported | `skillweave.runlifecycle.cli:main_resume` |
| `native-controller` | execute | supported | `skillweave.cli.native_controller:main` |
| `mcp` | operate | supported | `skillweave.mcp.server` |
| `dispatch` | legacy | legacy | `skillweave.dispatch.cli:main` |

The interpretation of a state is **shared by construction**: it is *projected*
from the one authority each part already lives in (`RunStateModel` canonical
values and terminal subset, `LEGACY_STATE_ALIASES`, the lifecycle→lane
projection and `STOP_REASONS`), never copied into a second vocabulary.

### The shared run-state vocabulary (15 canonical states)

`preflight`, `batch_selection`, `lane_plan`, `implement`, `verify`,
`review_gate`, `fix_retry`, `integrate`, `advance_or_stop`,
`sandbox_preflight`, `in_progress`, `preflight_complete`,
`blocked_waiting_for_gate`, `review_required`, `failed`.

- **terminal** subset: `advance_or_stop`, `failed`.
- **stop reasons**: `before_gate`, `operator_halt`, `dependency_failed`.
- **lifecycle → lane** projection: `running→preflight`, `succeeded→advance_or_stop`,
  `failed→failed`, `killed→advance_or_stop`, `unknown→unknown`.
- an unknown raw value is reported `recognized: false` — **never coerced to a
  default**.

### The single run-id rule

Every async `start` / `inspect` / `kill` / `cancel` / `resume` id must match:

```
[A-Za-z0-9][A-Za-z0-9._-]{0,127}
```

This rejects traversal/injection shapes (`../x`, `a b`, `x/y`, empty, `.`,
`..`, a 300-char id) while the auto-generated uuid hex always satisfies it.

### Documented exit codes

| Outcome | Code |
|---------|------|
| `ok` | 0 |
| `error` | 1 |
| `invalid_input` | 2 |
| `not_running` | 3 |
| `identity_refused` | 3 |
| `not_executable` | 126 |
| `command_not_found` | 127 |

---

## 4. CLI howtos

### Run a command as a durable async run

```bash
skillweave start -- make test      # launch; prints the run id
skillweave inspect <run-id>        # snapshot or --follow
skillweave kill <run-id>           # kill by id + recorded pid
```

`start`/`inspect`/`kill`/`cancel`/`resume` are mounted by `skillweave.cli.main`
and each reaches its own parser (the top-level `start` REMAINDER positional no
longer collides with the router `dest="command"`).

### Run an Assay probe (native execution)

```bash
skillweave run \
    --tool opencode --model <model> \
    --subject-repo <repo> --subject-commit <sha> \
    --methodology assay --autonomy-policy unicorn \
    --task-id <task> --attempt 1 \
    --db-path .skillweave/store.db --artifacts-path .skillweave/artifacts \
    -- python3 -c "print('probe output')"
```

The command after `--` is the **probe**: a real, bounded subprocess. Assay
journals `pin → hypothesis → probe → measure → classify → pass/hold`; **probe
facts are not a verdict** and model text is never terminal authority. Full
detail: `docs/native-assay-execution.md`.

### Interpret one state, or print the table

```bash
skillweave support --state running --json   # one interpretation
skillweave support --json                    # the whole table
```

### Dispatch from your harness (declared, not guessed)

The dispatch seam records *which* harness executed a run from exactly one
signal, `SKILLWEAVE_HARNESS`. Unset ⇒ the run records a `DETECTED` harness with
an empty name, never mistaken for a declaration. The four canonical names are
`opencode`, `claude-code`, `codex`, `antigravity`
(`docs/dispatching-from-your-harness.md`).

---

## 5. MCP and Assay support rows

### MCP is transport, not the source of durable truth

MCP is an **optional extra** (`skillweave[mcp]`), never a base dependency; the
base install runs without it and reports an actionable hint when it is
requested while absent (`python -m skillweave.scripts.check_optional_capability
mcp`). The server exposes exactly these **19** tools (read from the live
`@mcp.tool()` registry in `src/skillweave/mcp/server.py`):

`create_run`, `status_run`, `cancel_run`, `resume_run`, `restart_session`,
`get_evidence`, `review_run`, `mailbox_post`, `mailbox_read`, `mailbox_replay`,
`mailbox_ack`, `agent_capabilities`, `harness_session_grant`, `handoff_adapter`,
`read_continuation_decision`, `review_action`, `register_child_harness_capability`,
`qualified_harness_continuation`, `recover_owned_attempt`.

The six control tools keep their names/signatures and delegate to
`skillweave.mcp.control`, which composes the *same* `RunApplicationService` the
CLI drives. The control surface emits the frozen `run-snapshot-v1` record,
returns structured refusals (never raised exceptions), and — for the scoped
Streamable HTTP transport — requires a scope token enforced on **every**
request. Serving the real server needs the real `mcp` package; without it the
placeholder server reports `can_serve == False` and refuses rather than
no-op'ing.

**Stub vs live transport proof (honest).** The serving-contract assertions run
against an injected fake FastMCP module and are **not** a live transport proof;
the live initialize/list proofs **skip, naming the missing package**, when the
real `mcp` package is not importable. On a host without `mcp` the live paths are
*unproven*, not silently passed.

### Assay methodology

Assay is a **declared methodology binding**: naming it either executes as Assay
or is refused with a named reason — **never silently downgraded to Ralph**. All
four surfaces (CLI, MCP, `RunApplicationService`, replay) are thin wrappers over
one implementation. Autonomy policies: `unicorn` (default), `moderate`,
`conservative`. A self-declared pass is impossible: PASS comes only from the
completion-contract gate.

Declared methodology bindings (from `runtime/methodology.py` `DEFAULT_REGISTRY`):

| Methodology | Supported topologies | Supported policies |
|-------------|----------------------|--------------------|
| `rex` | sequential, dag | supervised, autonomous |
| `ralph` | dag, wave | approval_required, collaborative |
| `gauntlet` | parallel_lanes, dag | collaborative, human_led |
| `assay` | sequential, dag, parallel_lanes, wave, hierarchical, phased | supervised |

An **unknown methodology is refused** at the parser (`--methodology` choices)
or, for direct service calls, at the `methodology` stage before any run state is
written.

---

## 6. Bare limits, adapters, unknown capacity, preview features

This section is the explicit statement criterion 2 asks for. Each item names the
limit, the authority, and what the limit is **not**.

### 6.1 Bare limits

- **Search credentials are bare.** Of the six built-in search providers,
  **DuckDuckGo Lite is the zero-key default** (no API key, no account); the
  keyed providers (Serper, SerpApi, Tavily, Brave, Perplexity) read their key
  from the environment (`SERPER_API_KEY`, `SERPAPI_API_KEY`, `TAVILY_API_KEY`,
  `BRAVE_API_KEY`, `PERPLEXITY_API_KEY`). **No key is required to *install*
  SkillWeave.** If a *keyed* provider is selected and its credential is absent,
  the call **fails closed** with an explicit `<ENV_VAR> not set` /
  missing-credential refusal — it is never silently served by DuckDuckGo. An
  unrecognised provider name likewise fails closed.
- **Install is source/Capacium only.** There is **no PyPI distribution**: a bare
  `pip install skillweave` is *not* a supported path and no workflow publishes
  there. Supported install surfaces are Capacium, a built wheel, and an editable
  source checkout. `requires-python >= 3.9`; the *verified-host* interpreter in
  this environment is 3.14 (declared-only rows: 3.10–3.13 where no interpreter
  was available). A fully offline/hermetic **base** install additionally needs
  `PyYAML` and the pinned `skillweave-sdk` wheel in the local wheelhouse; where
  they are absent and network installs are disallowed, the install is an
  explicit **setup HOLD**, not a fabricated pass.
- **No cumulative limit is not a limit.** An owner-authorized
  `max_turns=None` is the explicit *no-cumulative-limit* envelope and is honoured
  (never replaced by an artificial default); a negative `max_turns` or
  `remaining_budget` is always refused. Turn-limit changes are admitted on four
  axes (authenticated actor, unchanged subject/brief, deadline, budget) and are
  recorded with a `kind` of `extension` or `no_cumulative_limit`.
- **Isolation is honestly bounded.** Write isolation is real
  (`sandbox-exec` Seatbelt / the BSD immutable flag), but the immutable-flag
  adapter does **not** confine a process to the checkout and does **not** deny
  reads — it reports `confines_to_scope = False`. A caller needing "no writes
  outside the checkout anywhere" sets `require_checkout_confinement=True` and is
  refused (HOLD) on a host that cannot guarantee it, rather than handed a weaker
  guarantee.

### 6.2 Detected versus live-tested adapters

The adapter facade keeps four facts independent and never collapses them into a
single "kind":

- **runtime category** (`search`, `generic`) — *closed*; an unknown category
  fails closed;
- **invocation transport** (`http`, `mcp-stdio`, `in-process`, …) — open;
- **package format / kind** (e.g. Capacium's opaque `kind`) — open, and the
  facade **never branches on it**;
- **distribution / provisioning** — *not in the descriptor at all*: **reading a
  manifest or a descriptor proves format conformance, never callability.**

The MCP-lifecycle fixtures prove **contract conformance against a fake
server**; they do **not** establish **live** provider availability, and **no
live provider is ever called by the tests**. A live, cited Perplexity search
requires a configured credential and network egress; where either is absent the
external proof stays **pending/blocked** (a fixture result is never promoted in
its place). This is the detected-versus-live-tested distinction, stated
explicitly.

### 6.3 Unknown capacity is never unlimited

At the native-tool boundary, a plan that requires native skill execution is
admitted only against a **measured** capability handshake. A snapshot missing
`measured_at`/`expires_at`, measured in the future, already expired, or whose
window is inverted **fails closed**. A declaration is **not** a detection, and a
detection is **not** a run-attested expansion: **unknown capacity is never treated as unlimited** (`docs/harness-capability-admission.md`).

### 6.4 Preview features

- The **aggregate (telemetry) service is labelled `preview/unavailable`** until
  all five gates (ingestion, cohort, quality, deletion, tenant_isolation) pass —
  and even then it is labelled `preview`, **never** `available`/GA.
  `AGGREGATE_SERVICE_GA` is pinned `False`: this release ships **no** production
  aggregate service.
- The three edition contracts are **Community / Pro / Enterprise**. Every
  edition keeps raw events **local** and works **offline**; export is opt-in and
  **off by default** everywhere. Community adds a coarse local recommender; Pro
  adds fixture-backed benchmarks, budgeting, a capability-based mix and
  hotspots; Enterprise adds tenant isolation. `EditionGuard` gates a feature
  request on the declared contract and refuses an unknown/invented edition —
  a Community caller cannot mutate the frozen contract into granting Pro
  features.

### 6.5 Onboarding storage is plaintext today

The onboarding surface writes **plaintext YAML**; the stored file is not
encrypted and no separate secret store is used:

- the durable authored profile —
  `skillweave.config/onboarding-profile.yaml` — is written with
  `yaml.dump(..., default_flow_style=False, sort_keys=False)`
  (`src/skillweave/onboarding/service.py`, `_save_durable_profile`);
- the generated state — `.skillweave/onboarding-state.yaml` — is written with
  `yaml.dump(state, f, default_flow_style=False)`
  (`src/skillweave/onboarding_cli.py`, `_save_state`, read back with
  `yaml.safe_load`).

This is stated as the **current** behaviour; it is not a security guarantee and
carries no credential. An operator who would not write these values to plaintext
should not run onboarding with them.

---

## 7. Public-example hygiene (criterion 3)

The public examples and docs are scanned for private-data and unverifiable-claim
patterns:

- **No private operator paths, session cookies, bearer tokens, or `sk-`/`pk-`
  API-key literals** in `README.md`, the docs or `examples/`. A public tool path
  such as `/opt/homebrew` is *not* matched (it is not private data).
- **No bare-PyPI install claim** anywhere in the release-facing material.
- **`envctl`, where mentioned, is marked optional** and is not required.
- **No unverifiable always-OSS / future-capability promises.** Where the product
  is described, it is described as what it *does* now (skills, phases, the local
  engine), not with an always-open-source guarantee or a not-yet-shipped feature
  commitment. `docs/architecture.md` names the `skillweave-packs-pro` repository
  as **proprietary and not public** — a boundary statement, not a commercial
  implementation disclosure; no commercial behaviour, CMS mapping or provider
  implementation is reproduced here.

These three properties are asserted by `tests/unit/test_1510_doc_truth.py`,
which renders the live support table and scans the release-facing material; the
leak scanner has a planted-secret negative control so a green result is not
vacuous.

---

## 8. Hold

This page binds to the **facts present in this checkout**. It does **not**
assert that the sibling SW-1510-DOC-TRUTH / SW-1510-MIRROR-CONFIG qualified
results have been composed: those qualified result artifacts are outside this
lane's read scope, so folding them in is an explicit **HOLD** for the composing
controller (SW-1510-DISC-VERIFY / SW-1510-RELEASE). Nothing here fabricates
their content or a passing verification in their place.
