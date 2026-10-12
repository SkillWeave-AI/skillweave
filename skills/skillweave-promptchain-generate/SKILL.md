---
facade: true
experimental: true
name: skillweave-promptchain-generate
description: Generate standardized SkillWeave prompt sequences from PRD (complexity-aware) or topic/domain. Creates execution plans optimized for REX (simple) or Ralph Loop (standard/complex) workflows.
argument-hint: inputs="[JSON with prd/topic]" mode="[auto/simple/standard/complex]" target="[humanize/machinize/mixed]" risk_mode="[conservative/medium/unicorn]"
---

# /skillweave-promptchain-generate

Generate optimized prompt sequences for execution. Two modes:
1. **PRD-based**: Generate execution sequences from PRD (`prd.json`) with complexity-aware workflow selection (REX vs Ralph Loop)
2. **Topic-based**: Create prompt sequences from topic/domain/goal for general planning

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
All generated sequences, plans, and execution graphs MUST be saved exclusively within `.skillweave/` or its sub-folders. Never dump artifacts into the repository root.

### 3. Git Isolation
Check `.gitignore` — if `.skillweave/` is not listed, append it. AI-generated planning files are excluded from source control.

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

**Usage (PRD-based - Recommended):**
```
skillweave-promptchain-generate inputs='{"prd": "prd.json"}' mode="auto" target="mixed"
```

**Usage (Topic-based):**
```
skillweave-promptchain-generate inputs='{"topic": "Wellness business evaluation", "domain": "wellness", "goal": "Create evaluation framework"}' mode="auto"
```

**Parameters:**
- `inputs` (required): JSON containing either PRD path (`prd`) or topic/domain/goal
- `sequence_type` (optional): Type of sequence - plan (conceptual/strategy), build (development/implementation), mixed (combination) (default: auto-detect)
- `methodology` (optional): Native execution methodology - `rex`, `ralph`, `gauntlet`, `assay`. Declared separately from autonomy policy and session schedule; Assay is never silently substituted by Ralph.
- `autonomy_policy` (optional): The methodology's own autonomy axis - `conservative`, `medium`, `unicorn` (`unicorn` is Assay's declared default). Separate from `methodology`.
- `session_schedule` (optional): Session scheduling, separate again from both. Defaults to the declared `session_boundary` (or `batch`).
- `execution_mode` (optional, **legacy adapter**): Legacy execution profile - rex (simple, fast, attended), ralph_attended (full loop with human checkpoints), ralph_overnight (autonomous overnight execution). Accepted only when it agrees with the declared `methodology`; it is refused (never silently substituted) when it would route execution to a different methodology.
- `target` (optional): Target audience - humanize (human readable), machinize (machine optimized), mixed (default: mixed)
- `risk_mode` (optional): `conservative`, `medium`, `unicorn` - overrides environment variable and config files
- `quality` (optional): Quality level (basic, standard, premium)
- `output_expectations` (optional): Expected output format

**Mode Mapping Table:**

| Sequence Type | Execution Mode | Characteristics | Use Case |
|--------------|----------------|----------------|----------|
| **plan** | **rex** | Conceptual, strategy, business planning • 1-3 tasks • <60 minutes • Quick feedback loop | Business idea validation, market analysis, strategic planning |
| **plan** | **ralph_attended** | Comprehensive planning with human oversight • 4-10 tasks • 1-4 hours • Structured validation | Product requirements, detailed business plans, investment decks |
| **plan** | **ralph_overnight** | Autonomous strategic analysis • 10+ tasks • >4 hours • Deep research | Market research reports, competitive analysis, industry deep dives |
| **build** | **rex** | Simple implementation • 1-3 components • <60 minutes • Lightweight testing | Prototype building, proof of concept, simple feature addition |
| **build** | **ralph_attended** | Full development cycle • 4-10 components • 1-4 hours • Multi-level verification | Feature development, API implementation, UI components |
| **build** | **ralph_overnight** | Complex system implementation • 10+ components • >4 hours • Production-grade gates | System architecture, full-stack applications, complex integrations |
| **mixed** | **rex** | Combined plan/build • 2-5 total tasks • <90 minutes • Integrated flow | MVP development, concept-to-prototype, pilot projects |
| **mixed** | **ralph_attended** | Full product development • 6-15 tasks • 2-8 hours • Phased execution | End-to-end product development, startup launch, product redesign |
| **mixed** | **ralph_overnight** | Enterprise-scale development • 15+ tasks • >8 hours • Autonomous pipeline | Large-scale projects, platform development, organizational transformation |

**Backward Compatibility:** The `mode` parameter is still supported as an alias
for the **legacy** `execution_mode` adapter, and the `execution_mode` /
`suggested_workflow` vocabularies are explicit adapters over the native
`methodology`:

- `mode="simple"` → `execution_mode="rex"` → methodology `rex`
- `mode="standard"` → `execution_mode="ralph_attended"` → methodology `ralph`
- `mode="complex"` → `execution_mode="ralph_overnight"` → methodology `ralph`
- `mode="auto"` → Auto-detect both sequence_type and execution_mode

The adapter **fails closed**: an unknown value, or a legacy
`execution_mode`/`suggested_workflow` that denotes a *different* methodology
than the sequence declares, is refused with
`legacy_would_substitute_methodology` / `unknown_legacy_*` — execution is never
silently re-routed to Ralph. A legacy `mode`/`execution_mode` alias is therefore
only usable where it agrees with the declared `methodology`; new work declares
`methodology` directly.

**PRD Input Example:**
```
skillweave-promptchain-generate inputs='{"prd": "generated/prd.json"}' mode="auto"
```

**Topic Input Example:**
```
skillweave-promptchain-generate inputs='{"topic": "Market research for AI tools", "domain": "saas", "goal": "Competitive analysis"}'

**Output (PRD-based):**
When generating from PRD, creates optimized execution sequences:
- `execution-sequences.yaml` - Structured execution plan with dependency graph
- `agent-assignments.json` - Task-to-capability mapping (agent-agnostic)
- `dependency-graph.dot` - Visual dependency graph
- `complexity-analysis.md` - Detailed complexity assessment
- `workflow-recommendation.md` - REX vs Ralph Loop recommendation with rationale

**Output (Topic-based):**
When generating from topic/domain, creates standard prompt sequence:
- Complete prompt sequence document with:
  - Metadata, Objective, Success Criteria
  - Assumptions, Usage Notes
  - Inputs Required, Outputs Required
  - Sequence Steps, Final Assembly
  - Validation Rules, Failure Handling
  - Final Deliverable Format

## PRD-based Sequence Generation

### Complexity-Aware Workflow Selection (Two-Axis Model)

PromptChain analyzes the PRD's declared `sequence.methodology` / `autonomy_policy`
/ `session_schedule` (preserving all three separately) and its tasks using a
two-axis model:
- **Sequence Type**: `plan` (conceptual/strategy), `build` (development/implementation), or `mixed`
- **Execution Mode** (legacy adapter): `rex` (simple/fast), `ralph_attended` (human-checkpointed), `ralph_overnight` (autonomous)

A PRD that carries a native `methodology` declaration keeps it: the generated
sequence emits `methodology`, `autonomy_policy` and `session_schedule` as three
separate fields (plus the `execution_envelope` block). The legacy
`execution_recommendation.suggested_workflow` is consulted only as an explicit
compatibility adapter — it is refused if it disagrees with the declared
methodology.

Based on complexity analysis, generates optimal execution sequences:

#### Execution Modes (Based on Task Complexity):

1. **REX Mode (`execution_mode="rex"`)**: For 1-3 tasks, <60 minutes
   - **Workflow**: Plan → Implement → Review → Done
   - **Parallelization**: Limited parallel lanes, simple dependencies
   - **Memory**: Basic progress tracking (`progress-simple.txt`)
   - **Verification**: Lightweight checks (type checking, basic tests)
   - **Use**: Quick prototypes, simple features, concept validation

2. **Ralph Loop Attended Mode (`execution_mode="ralph_attended"`)**: For 4-10 tasks, 1-4 hours
   - **Workflow**: Full Ralph Loop with human checkpoints at integration gates
   - **Parallelization**: Multiple sidecar lanes with synchronization points
   - **Memory**: Structured progress tracking (`progress-structured.yaml`)
   - **Verification**: Multi-level verification (code, functional, system)
   - **Use**: Feature development, product requirements, detailed planning

3. **Ralph Loop Overnight Mode (`execution_mode="ralph_overnight"`)**: For 10+ tasks, >4 hours
   - **Workflow**: Fully autonomous Ralph Loop execution with automated gates
   - **Parallelization**: Maximal parallelization with critical path management
   - **Memory**: Advanced memory system (`agents-enhanced.md`)
   - **Verification**: Production-grade quality gates and integration tests
   - **Use**: System architecture, large-scale projects, enterprise development

#### Sequence Types (Based on Content):

- **Plan Sequences**: Conceptual work, strategy, business planning, research
- **Build Sequences**: Development, implementation, coding, technical work  
- **Mixed Sequences**: Combination of plan and build phases in integrated workflow

### Automatic Analysis Process

1. **Load PRD**: Read and validate `prd.json` against schema
2. **Analyze Complexity**: Evaluate `execution_recommendation` or calculate if missing
3. **Build Dependency Graph**: Analyze task dependencies for parallel opportunities
4. **Generate Sequences**: Create optimized execution sequences based on mode
5. **Map Capabilities**: Convert task types to required capabilities (not specific agents)
6. **Output Planning**: Structure outputs for target audience (humanize/machinize/mixed)

### Example PRD Analysis

For a PRD with `execution_recommendation.mode: "simple"`:
```yaml
execution_sequence:
  mode: "simple"
  workflow: "rex-simple"
  steps:
    - id: "plan"
      type: "analysis"
      task: "Analyze requirements and create implementation plan"
      
    - id: "implement"
      type: "execution"
      task: "Implement solution based on plan"
      depends_on: ["plan"]
      
    - id: "review"
      type: "verification"
      task: "Review implementation and verify acceptance criteria"
      depends_on: ["implement"]
      
    - id: "complete"
      type: "finalization"
      task: "Finalize and deliver solution"
      depends_on: ["review"]
```

For a PRD with `execution_recommendation.mode: "standard"`:
```yaml
execution_sequence:
  mode: "standard"
  workflow: "ralph-loop-attended"
  checkpoint_interval: 5
  steps:
    - phase: "initialization"
      tasks: ["INFRA-001", "DB-001"]
      parallel: false
      
    - phase: "core-development"
      tasks: ["API-001", "UI-001", "FEAT-001"]
      parallel: true
      depends_on: ["initialization"]
      
    - phase: "testing"
      tasks: ["TEST-001", "TEST-002"]
      parallel: true
      depends_on: ["core-development"]
      
    - phase: "finalization"
      tasks: ["DOC-001", "DEPLOY-001"]
      parallel: false
      depends_on: ["testing"]
```

## Next Level Features

SkillWeave Next Level provides advanced capabilities that can enhance prompt chain generation. These features are controlled by `.skillweave/config.yaml` and can be accessed via the `SkillWeaveNextLevel` class.

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

Adjust generation behavior according to the effective risk mode:
- **Conservative**: Extra validation, explicit approvals, strict safety checks, detailed documentation
- **Medium**: Balanced approach with standard validation
- **Unicorn**: Optimistic assumptions, minimal confirmations, maximum speed, concise outputs


### Checklist-Based Execution
If `checklist: true` is set in the config, the skill will:
- Parse markdown checklists (`- [ ]` and `- [x]`) from input PRDs or attached files
- Track checklist item completion across sequence generation steps using `.skillweave/tracking-log/`
- Loop until all checklist items are marked complete
- Provide progress reports and remaining items

### Design-Thinking Lens  
If `design_thinking: true` is set in the config, apply these cognitive ergonomics principles to generated sequences:
1. **Value ≥ Noise**: Ensure every sequence step provides clear user value
2. **Scan Before Read**: Structure sequences for quick scanning with clear headings
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
- Load and combine templates from `.skillweave/templates/` for sequence generation
- Use template inheritance for consistent sequence structures
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

Adjust your generation based on enabled features to provide enhanced results while maintaining backward compatibility.


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
    current_skill="skillweave-promptchain-generate",  # This skill's name
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

## Topic-based Sequence Format

For topic/domain/goal inputs, the generated prompt sequence follows this structure:

1. **Metadata**: Sequence ID, version, created date, mode (plan/build/mixed)
2. **Objective**: Clear goal and success definition
3. **Success Criteria**: Binary, testable success metrics
4. **Assumptions**: Key assumptions and validation methods
5. **Usage Notes**: How to execute the sequence
6. **Inputs Required**: Required inputs with validation
7. **Outputs Required**: Expected outputs with format specifications
8. **Sequence Steps**: Step-by-step execution plan with `depends_on` arrays
9. **Final Assembly**: How to combine step outputs into final deliverable
10. **Validation Rules**: Rules for validating each step and overall sequence
11. **Failure Handling**: Recovery procedures for failed steps
12. **Final Deliverable Format**: Format and structure of final output

## Integration with SkillWeave Workflow

PromptChain Generate is a key component in the complete SkillWeave development flow:

### Complete Workflow: Blueprint → PromptChain → ReleaseChain

1. **Blueprint Skill** (`skillweave-blueprint`):
   - Creates structured PRD with `execution_recommendation`
   - Output: `prd.json`, `prd.md`, memory system templates

2. **PromptChain Generate** (`skillweave-promptchain-generate`):
   - Analyzes PRD complexity and generates optimized execution sequences
   - Output: `execution-sequences.yaml`, `agent-assignments.json`

3. **PromptChain Execute** (`skillweave-promptchain-execute`):
   - Executes sequences with parallel execution and dependency analysis
   - For build components: Offers to invoke ReleaseChain automatically

4. **ReleaseChain** (`skillweave-releasechain`):
   - Executes PRD tasks with Ralph Loop (or REX-style for simple tasks)
   - Uses capability-based agent routing (agent-agnostic)
   - Output: Completed project with memory system updates

### Example Complete Workflow

```bash
# Step 1: Create blueprint from idea
skillweave-blueprint idea="AI meeting notes summarizer" domain="saas"

# Step 2: Generate execution sequences from PRD
skillweave-promptchain-generate inputs='{"prd": "generated/prd.json"}' mode="auto"

# Step 3: Execute sequences (or skip to ReleaseChain)
skillweave-promptchain-execute sequence="execution-sequences.yaml" inputs='{"prd": "generated/prd.json"}'

# Step 4: Execute development pipeline (if build components)
skillweave-releasechain inputs='{"prd": "generated/prd.json", "sequences": "execution-sequences.yaml"}' mode="attended"
```

### Agent-Agnostic Design

Like all SkillWeave skills, PromptChain Generate is **agent-agnostic**:
- Uses **capability-based routing** instead of specific agent names
- Maps task types to required capabilities (`code_generation`, `planning`, `testing`, etc.)
- Compatible with any AI coding agent on any supported transport (Markdown or MCP)
- Runtime agent discovery and capability matching

## Recommended companion files

Use these files if present:
- `references/format-spec.md` - Prompt sequence format specification
- `references/execution-rules.md` - Execution and parallelization rules
- `references/validation-rules.md` - Validation and failure handling rules
- `references/complexity-analysis.md` - PRD complexity assessment guide
- `assets/prompt-sequence.schema.json` - JSON schema for prompt sequences
- `assets/workflow-context.schema.json` - Schema for workflow context data

## Effective-profile chain derivation (SW1312-CHAIN-001)

When an explicit effective-profile artifact is supplied, generation is driven by
that **immutable snapshot**, not by the legacy topic/PRD path. The snapshot is
the resolver's output (`skillweave.profiles.effective`); this skill consumes it
as a read-only input and never re-resolves a profile.

**Invocation (profile path):**

```
skillweave-promptchain-generate profile="path/to/effective-profile.snapshot.json"
```

**How the chain is derived** — the resolver exposes a resolved content mapping and
preview-only runtime dimensions. Generation reads the profile's *data*, never its
name:

1. **Ordered steps** — one step per phase in the snapshot's declared `phases`,
   in order, each carrying the phase, a role, the skill set, capabilities, gate
   and evidence contracts.
2. **Skills** — the skill set for the profile's `primaryCategory` (a data-driven
   category→skill map, e.g. `build` → blueprint/design/generate/validate/execute/
   releasechain, `research` → discovery/generate/validate/execute/observe), or
   the snapshot's declared `skills` when present.
3. **Capabilities / roles** — from the snapshot's declared `capabilities` and
   `roles`; the fixed separation `ops` / `reviewer` / `observer` holds.
4. **Gates / evidence** — derived from the profile's `control.risk` (high/critical
   adds a `separate_cold_review` gate and cold-review + replay evidence).
5. **Handoffs** — one per phase boundary (`handoff:<prev>-><next>`), each an
   identity-bearing transfer carrying the four profile identity fields
   (`profile_id`, `profile_version`, `sdk_digest`, `effective_digest`) so the
   snapshot that produced the chain rides on every phase transfer, never as a
   bare string.
6. **Dispatch topology** — one governed lane per phase (disjoint `write_scope`
   under the phase, `depends_on` the prior phase) plus a final integration lane;
   each lane manifest carries a `provenance` block with the four identity fields.

**Fail-explicit preview boundary:** preview-only runtime dimensions (`phases`,
`kernel_stage`, `topology`, `control`, `human_coupling`, `change_surfaces`,
`autonomy_bounds`, `skills`, `capabilities`) are carried as *declarations* only.
Requesting their execution fails before dispatch with an actionable message; the
generator never falls back silently to the legacy plan/build/mixed path.

**Backward compatibility:** with no explicit profile, the legacy topic/PRD path
above is unchanged.