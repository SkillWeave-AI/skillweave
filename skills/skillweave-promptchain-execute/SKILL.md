---
facade: true
experimental: true
name: skillweave-promptchain-execute
type: orchestration
description: Execute SkillWeave sequences with dependency-aware batches, controlled parallelism, retries, evidence, and binary gates.
argument-hint: sequence="[prompt sequence]" inputs="[JSON]" methodology="[rex/ralph/gauntlet/assay]" autonomy_policy="[conservative/medium/unicorn]" session_schedule="[batch/phase/none]" execution_mode="[auto/rex/ralph_attended/ralph_overnight] (legacy adapter]" audience_mode="[auto/humanize/machinize/mixed]" risk_mode="[conservative/medium/unicorn]" (or attach .md/.txt file)
---

# /skillweave-promptchain-execute

**Execute structured prompt sequences with real delivery discipline.**

This skill is the execution engine for SkillWeave. It owns:
- **Lane scheduling and Ralph Loop** — iterative build/verify/advance cycles with binary gates
- **Retry and state management** — automatic retry, state tracking, failure recovery
- **Batch planning** — dependency-aware batch decomposition with safe parallel lanes
- **Build completion handoff to Release** — completed, verified outputs hand off to `skillweave-releasechain`

ReleaseChain receives completed build outputs; it does NOT execute build tasks. Deployment and go-live belong to `skillweave-launch`.

## Mandatory Pre-Flight: SkillWeave Sandboxing

Before generating any output, you MUST verify and enforce the SkillWeave sandbox. This applies to every skill invocation without exception:

### 1. Enforce `.skillweave/` Directory Structure
If `.skillweave/` does not exist in the project root, create it:
```
.skillweave/
.skillweave/tracking-log/
.skillweave/templates/
.skillweave/sequences/
```

### 2. Route All Outputs Into `.skillweave/`
All execution sequences, batch plans, gate logs, and state trackers MUST be saved exclusively within `.skillweave/` or its sub-folders. Never dump artifacts into the repository root.

### 3. Git Isolation
Check `.gitignore` — if `.skillweave/` is not listed, append it. AI-generated execution files are excluded from source control.

### 4. Default Config
If `.skillweave/config.yaml` does not exist, create it with:
```yaml
mode: medium
checklist: true
design_thinking: true
community_knowhow: true
modular_templates: true
```

Proceed with core skill logic only AFTER these four criteria are met.

## Usage

Invoke the skill by its name with arguments. The skill is
host-neutral; no executable prefix is required — route it through any host on
any supported transport (Markdown or MCP).

```text
skillweave-promptchain-execute sequence="[prompt sequence text]" inputs="[JSON inputs]"
```

Or attach a `.md` / `.txt` sequence file and provide:

```text
skillweave-promptchain-execute inputs='{"key":"value"}'
```

## Parameters

- `sequence` (optional if file attached): Prompt sequence text
- `inputs` (required): JSON object with required runtime inputs
- `methodology` (optional): The native execution methodology - `rex`, `ralph`, `gauntlet`, `assay`. **Assay is preserved, never silently substituted by Ralph.** Declared separately from the two fields below.
- `autonomy_policy` (optional): The methodology's own autonomy axis - `conservative`, `medium`, `unicorn` (`unicorn` is Assay's declared default). Separate from `methodology`.
- `session_schedule` (optional): Session scheduling, separate again from both.
- `execution_mode` (optional, **legacy adapter**): `auto`, `rex`, `ralph_attended`, `ralph_overnight`. Accepted only when it agrees with the declared `methodology`; refused (`legacy_would_substitute_methodology`) when it would route execution elsewhere.
- `audience_mode` (optional): `auto`, `humanize`, `machinize`, `mixed`
- `risk_mode` (optional): `conservative`, `medium`, `unicorn` - overrides environment variable and config files

Default:

- `methodology` = the sequence's declared methodology (the legacy `execution_mode` adapter is consulted only when no native methodology is declared)
- `autonomy_policy` = `unicorn` for Assay, empty otherwise (never invented)
- `session_schedule` = the declared `session_boundary`, else `batch`
- `audience_mode = auto`
- `risk_mode = medium` (unless overridden by environment variable or config)

## Core Distinctions

### 1. Sequence Type

Sequence type describes the nature of the work:

- `plan`: strategy, analysis, PRD, architecture, research-led planning
- `build`: code, config, tests, infra, docs tied to implementation
- `mixed`: both plan and build steps are materially present

### 2. Methodology, Autonomy Policy and Schedule

The execution intent is **three separate fields**, never one conflated mode
string (this mirrors `runtime/snapshot.py`, where `methodology`,
`autonomy_policy` and `session_schedule` are distinct):

- `methodology` describes how the work is carried out. It is one of `rex`,
  `ralph`, `gauntlet`, `assay`. **Assay is a first-class methodology and is
  never silently substituted by Ralph** — the declared value is preserved end
  to end.
- `autonomy_policy` is the methodology's own autonomy axis (`conservative`,
  `medium`, `unicorn`); `unicorn` is Assay's declared default. It is not a
  methodology.
- `session_schedule` describes where a session may stop and resume.

### 2a. Execution Mode (legacy adapter)

`execution_mode` (`rex`, `ralph_attended`, `ralph_overnight`) is retained as an
**explicit legacy adapter** over the methodology axis:

- `rex` → methodology `rex`
- `ralph_attended` → methodology `ralph`
- `ralph_overnight` → methodology `ralph`

The adapter is **fail-closed**: a legacy `execution_mode` (or PRD
`suggested_workflow`) that denotes a *different* methodology than the sequence
declares is refused with a `legacy_would_substitute_methodology` error, and an
unknown value is refused rather than coerced. A legacy mode therefore never
silently routes execution to Ralph.

`mixed` is **not** a methodology or an execution mode. It is only a
sequence-type classification.

## Mode Resolution

When no native `methodology` is declared, resolve the legacy `execution_mode`
(`auto` = choose as follows); when a native `methodology` is declared it is
authoritative and this legacy resolution applies only where it agrees:

### Use `rex` when:

- there are 1-3 steps
- total work is small
- dependencies are mostly linear
- there is no meaningful public/private or release boundary risk
- verification is lightweight

### Use `ralph_attended` when:

- there are 4+ steps
- the work mutates a repository
- code, tests, or docs are all involved
- binary gates are required
- public/private or product-tier boundaries are involved
- multiple safe parallel lanes exist

### Use `ralph_overnight` when:

- the sequence is already decomposed into explicit batches
- batch gates are well-defined
- retries and carry-forward logic are already specified
- long autonomous execution is acceptable

Rule of thumb:

- default to `ralph_attended` for real product development unless the task is obviously tiny

## Next Level Features

SkillWeave Next Level provides advanced capabilities that can enhance prompt chain execution. These features are controlled by `.skillweave/config.yaml` and can be accessed via the `SkillWeaveNextLevel` class.

### Risk Mode Integration

SkillWeave v0.5.5 introduces a hierarchical override system for risk mode. The effective risk mode is determined by the following precedence order (highest to lowest):

1. **CLI parameter**: `risk_mode="conservative/medium/unicorn"` (if provided)
2. **Environment variable**: `SKILLWEAVE_RISK_MODE` (if set)
3. **Project config**: `.skillweave/config.yaml` `mode` setting
4. **Global config**: `~/.skillweave/config.yaml` `mode` setting
5. **Default**: `medium`

Use the `RiskModeResolver` class from `skillweave.risk_mode_resolver` to resolve the effective risk mode programmatically.

**Command-line utilities:**
- `skillweave-risk-mode` - shows effective risk mode given current context. Use `skillweave-risk-mode --cli-risk-mode=conservative --verbose` to see precedence resolution.
- `skillweave-interactive-mode` - interactive risk mode selection with project analysis and persistence options (temporary, project config, global config).

Adjust execution behavior according to the effective risk mode:
- **Conservative**: Extra validation, explicit approvals, strict safety checks
- **Medium**: Balanced approach with standard validation
- **Unicorn**: Optimistic assumptions, minimal confirmations, maximum speed

### Checklist-Based Execution
If `checklist: true` is set in the config, the skill will:
- Parse markdown checklists (`- [ ]` and `- [x]`) from sequence inputs
- Track checklist item completion across batches using `.skillweave/tracking-log/`
- Loop until all checklist items are marked complete
- Provide progress reports and remaining items

### Design-Thinking Lens  
If `design_thinking: true` is set in the config, apply these cognitive ergonomics principles to outputs:
1. **Value ≥ Noise**: Ensure every execution output provides clear user value
2. **Scan Before Read**: Structure batch reports for quick scanning with clear headings
3. **Hierarchy of Needs**: Address functional needs before advanced features
4. **Progressive Disclosure**: Reveal complexity gradually as needed
5. **Recognition Over Recall**: Use consistent patterns and familiar formats
6. **Error Tolerance**: Design for mistakes with clear recovery paths

### Community Know-How
If `community_knowhow: true` is set, the skill will:
- Extract patterns from `.skillweave/tracking-log/` across projects
- Provide repository cleanup recommendations based on common issues
- Suggest optimizations and best practices from community patterns

### Modular Templates
If `modular_templates: true` is set, the skill can:
- Load and combine templates from `.skillweave/templates/` for batch planning
- Use template inheritance for consistent execution reports
- Generate custom sequence sections from reusable components

### Using Next Level Features
```python
from skillweave.next_level import SkillWeaveNextLevel

# Initialize with project root
next_level = SkillWeaveNextLevel("/path/to/project")

# Check feature availability
if next_level.is_checklist_enabled():
    checklist = next_level.parse_checklist(markdown_content)
    # Track progress, loop until completion

if next_level.is_design_thinking_enabled():
    lens = next_level.get_design_thinking_lens()
    lens.apply_to_output(your_content)

# Access other features similarly
```

Adjust your execution based on enabled features to provide enhanced results while maintaining backward compatibility.


## Intelligent Guidance (v0.5.5)

SkillWeave v0.5.5 introduces intelligent prompt analysis and onboarding flows 
that help ensure you're using the right skill with the right parameters.

### How It Works

When this skill is invoked, you should first use the `SkillIntegrationHelper` 
to analyze the user's prompt and validate the request:

```python
from skillweave.intelligent_detection import integrate_with_skill
import os

# Determine project root (current directory or parent containing .skillweave/)
project_root = os.getcwd()
if not os.path.exists(os.path.join(project_root, ".skillweave")):
    # Try parent directory
    parent = os.path.dirname(project_root)
    if os.path.exists(os.path.join(parent, ".skillweave")):
        project_root = parent

result = integrate_with_skill(
    user_prompt=user_prompt,  # The original user prompt
    current_skill="skillweave-promptchain-execute",  # This skill's name
    project_root=project_root
)
```

### Handling the Result

The `integrate_with_skill` function returns a dictionary with an `action` key:

1. **`action: "proceed"`** - Skill selection is appropriate, parameters are valid
   - Continue with normal skill execution
   - Use `result["validated_parameters"]` for parameter values
   - Apply `result["mode_override"]` if present (risk mode from CLI/env)

2. **`action: "gather_parameters"`** - Missing or invalid parameters detected
   - Show `result["missing_parameters"]` to the user
   - Ask for each missing parameter using `result["parameter_prompts"]`
   - Use interactive Q&A to gather all required information
   - After gathering, re-run `integrate_with_skill` with updated parameters

3. **`action: "switch_skill"`** - Different skill might be more appropriate
   - Consider switching to `result["recommended_skill"]`
   - Show explanation: `result["switch_reason"]`
   - Ask user for confirmation before switching
   - If confirmed, load the recommended skill instead

4. **`action: "onboarding_flow"`** - User needs guided onboarding
   - Follow the interactive onboarding flow
   - Use `result["onboarding_steps"]` for guidance
   - Gather information step by step
   - Complete onboarding before skill execution

### Benefits

- **Skill Validation**: Ensures this skill is appropriate for the task
- **Parameter Completeness**: Checks all required parameters are provided
- **Intelligent Routing**: Suggests better-suited skills when applicable
- **Guided Onboarding**: Helps new users through step-by-step setup
- **Learning System**: Improves recommendations based on user feedback

### Integration with Existing Features

The intelligent guidance system works alongside existing Next Level features:
- Respects risk mode overrides from CLI, environment, or config
- Uses the same project root and configuration
- Integrates with checklist tracking and design thinking
- Maintains backward compatibility

### Example Workflow

```python
# 1. Analyze user prompt
result = integrate_with_skill(user_prompt, "skillweave-blueprint", project_root)

# 2. Handle result
if result["action"] == "proceed":
    # Extract validated parameters
    params = result["validated_parameters"]
    # Apply risk mode override if present
    if "mode_override" in result:
        set_risk_mode(result["mode_override"])
    # Execute skill with validated parameters
    execute_skill(params)
    
elif result["action"] == "gather_parameters":
    # Interactive parameter gathering
    for param in result["missing_parameters"]:
        prompt = result["parameter_prompts"].get(param, f"Enter value for {param}:")
        value = ask_user(prompt)
        # Update parameters and re-validate
        # (In practice, you'd collect all then re-validate)
        
elif result["action"] == "switch_skill":
    # Suggest skill switch
    if confirm_switch(result["recommended_skill"], result["switch_reason"]):
        load_skill(result["recommended_skill"])
```

Always use intelligent guidance when executing this skill to provide the best 
user experience and ensure successful outcomes.

## Git Flow Convention

When executing `build` or `mixed` sequences in a git repository, SkillWeave enforces a minimum branching discipline. This prevents work from landing directly on protected branches and ensures a reviewable integration path.

### Branch Model

| Branch | Purpose | Protected |
|--------|---------|-----------|
| `main` | Release-ready, tagged with `vX.Y.Z` | Yes — no direct commits |
| `dev` | Integration branch, CI must be green | Yes — only via PR |
| `feature/<id>-<slug>` | New functionality, branched from `dev` | No |
| `fix/<id>-<slug>` | Bug fixes, branched from `dev` | No |
| `chore/<slug>` | Maintenance, docs, CI changes | No |

### Preflight Detection (before any build work)

Before creating a branch or committing, check the repository state:

1. **Does `dev` exist?** If not, recommend creating it from `main` and explain why.
2. **Is the current branch `main`?** Warn the user and recommend branching to `dev` or a feature branch. Never commit build work directly to `main`.
3. **Is there an existing branch for this task?** Search for open branches matching the task ID or slug. If found, offer to continue on that branch instead of creating a new one.
4. **Is `dev` up to date with `main`?** If `dev` is behind `main`, recommend rebasing or merging `main` into `dev` before branching.

If the user explicitly opts out (e.g., single-branch workflow), respect their choice but log the decision in `.skillweave/tracking-log/`.

### Branch Creation

When starting build work:

1. Determine branch type from the task/PRD context:
   - PRD tasks with new features → `feature/<ticket-id>-<slug>`
   - Bug fixes or patches → `fix/<ticket-id>-<slug>`
   - Maintenance, docs, CI → `chore/<slug>`
2. Branch from `dev` (not from `main`)
3. Log the branch name in `.skillweave/tracking-log/` for continuity across sessions

### Merge Flow

The expected merge path for build work:

```
feature/FEAT-001-auth  →  PR to dev  →  PR to main  →  tag vX.Y.Z
```

- **Feature branch → `dev`**: Created by `promptchain-execute` or `releasechain` after build gates pass. Requires tests green.
- **`dev` → `main`**: Created by `releasechain` as the release PR. Requires all integration tests green, changelog updated, version bumped.
- **Tag on `main`**: Created by `releasechain` or `skillweave-launch` after merge to `main`.

### Configuration

The git flow can be configured in `.skillweave/config.yaml`:

```yaml
git_flow:
  enabled: true
  branches:
    production: main
    integration: dev
  branch_prefix:
    feature: feature/
    fix: fix/
    chore: chore/
  require_pr: true
  auto_create_dev: false
```

If `git_flow.enabled` is `false` or missing, skip branch enforcement but still warn when committing directly to `main`.

## Execution Process

### Phase 1: Preflight

1. Detect sequence type: `plan`, `build`, or `mixed`
2. Validate required inputs
3. Parse dependencies
4. Identify:
   - critical path
   - safe parallel lanes
   - review gates
   - handoff boundary to releasechain/skillweave-launch (see `.skillweave/release/skill-boundaries.yaml`)
5. Resolve execution mode
6. Resolve effective risk mode using hierarchical override system (CLI parameter > environment variable > project config > global config > default)
7. **Evaluate git flow state** (see Git Flow Convention above):
   - Detect branch model (`dev` exists? current branch? open feature branches?)
   - Create or switch to appropriate work branch
   - Warn if working directly on `main` or `dev`
8. Determine whether the sequence should run:
   - as one batch
   - as multiple batches
   - with sidecar subagents

If the sequence is malformed, underspecified, or contradicts the real repository baseline, stop and produce a baseline-alignment report before executing.

### Phase 2: Batch Planning

Before implementation, convert the sequence into executable batches.

For each batch, define:

- `batch_id`
- `goal`
- `included_steps`
- `critical_path_step`
- `parallel_lanes`
- `write_surfaces`
- `verification_commands`
- `review_gate`
- `completion_contract`
- `next_batch_if_pass`
- `fallback_if_fail`

Do not execute a multi-step build sequence without first producing an explicit batch plan.

### Phase 3: Ralph Loop State Machine

When execution mode is `ralph_attended` or `ralph_overnight`, use this loop:

1. `Preflight`
2. `Batch Selection`
3. `Lane Plan`
4. `Implement`
5. `Verify`
6. `Review Gate`
7. `Fix / Retry`
8. `Integrate`
9. `Advance or Stop`

Only advance when the current batch passes its binary gate.

## Parallelization Rules

Parallelism is allowed only when it is safe.

### Safe parallel lanes

Parallelize steps when they have:

- disjoint write scopes
- no unresolved dependency between them
- no shared ownership of fragile contract surfaces

Good sidecar lanes:

- read-only research
- documentation drafting
- isolated tests
- release notes
- audit and verification passes

### Keep local on the critical path

Keep the following local unless the sequence explicitly isolates them:

- product-tier registries
- tool index / tool registration
- shared contracts used across many modules
- release and export manifests
- other single-owner integration surfaces

### Subagent policy

Use subagents only for:

- independent sidecar lanes
- isolated implementation with disjoint write scope
- verification or review that does not block immediate local context-building

Do not delegate the immediate blocking task if the next local action depends on it.

## Required Build-Step Fields

For build-oriented execution, each implementation step should define or be normalized into:

- `id`
- `title`
- `depends_on`
- `required_capabilities`
- `write_scope`
- `verification`
- `integration_gate`
- `retry_budget`
- `handoff_contract`

If these are missing, infer them conservatively. If they cannot be inferred safely, stop and emit a sequence-hardening recommendation.

## Gate Policy

All meaningful completion decisions must be binary.

Allowed completion signals:

- tests passed
- verifier passed
- build succeeded
- review explicitly returned `continue`
- required artifact exists and matches the contract

Not sufficient on their own:

- "looks good"
- "seems fine"
- "mostly done"
- "probably works"

If verification is inconclusive, mark the batch `inconclusive`, explain why, and do not silently advance.

## Audience Handling

After execution, outputs may be shaped for:

- `humanize`: explanation-first deliverables
- `machinize`: structured machine-readable outputs
- `mixed`: both

Do not ask about audience mode if the sequence or context already makes it obvious.

## Output Contract

### For each batch, return:

- `batch_id`
- `steps_completed`
- `files_changed`
- `commands_run`
- `verification_results`
- `review_result`
- `blockers`
- `known_limitations`
- `next_executable_batch`

### For plan-oriented outputs, return:

- decision summary
- assumptions
- unresolved questions
- artifacts produced
- recommended next execution package

### For build-oriented outputs, return:

- implementation summary
- exact verification steps
- pass/fail/inconclusive status
- next slice recommendation

## Failure Handling

If a batch fails:

1. identify whether the failure is:
   - implementation
   - verification
   - environment
   - sequence-definition
2. apply the retry budget if appropriate
3. prefer narrow fixes over broad redesign
4. if still blocked, stop and emit:
   - blocker summary
   - affected batch
   - safe rollback or pause point
   - next required user or system action

Do not continue into the next batch after a failed gate.

## Handoff to ReleaseChain / Launch

This skill is an **orchestration substrate**. It does NOT handle release-specific logic.

When build work completes successfully and outputs are in a reviewable state,
offer the appropriate downstream skill:

| Downstream need | Skill |
|-----------------|-------|
| Release (version, changelog, publish) | `skillweave-releasechain` |
| Launch (deploy, communicate, go-live) | `skillweave-launch` |

### Conditions

Hand off to releasechain when:
- Build-oriented work completed successfully
- Outputs are in a reviewable state
- The next logical step is review, test hardening, commit, release, or PR flow

Do not automatically transfer partial or inconclusive build batches.

### Skill Boundaries

For complete role definitions, see:
- `.skillweave/release/skill-boundaries.yaml` — formal boundary specs
- `skills/skillweave-releasechain/SKILL.md` — releasechain skill doc
- `skills/skillweave-launch/SKILL.md` — launch skill

## Agent-Agnostic Execution

This skill is agent-agnostic.

It should prefer:

- capability-based routing
- lane-based task assignment
- central ownership of critical-path integration
- fallback to available agents when the preferred capability runner is unavailable

When multiple agents exist, assign by:

- capability fit
- write-scope safety
- dependency position
- verification needs

Do not encode vendor-specific assumptions into the execution logic.

## Recommended Companion Files

Use these files if present:

- `references/execution-rules.md`
- `references/format-spec.md`
- `references/sequence-type-detection.md`
- `references/parallel-execution.md`
- `references/ralph-loop-state-machine.md`
- `references/build-step-normalization.md`
- `references/gate-policy.md`