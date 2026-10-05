---
facade: true
experimental: true
name: skillweave-blueprint
description: Create structured PRD (Product Requirements Document) through guided interview. Adapts Ralph Loop concepts for multi-agent AI development. Creates blueprint for promptchain and releasechain workflows.
argument-hint: idea="[your project idea]" domain="[domain]" risk_mode="[conservative/medium/unicorn]" (optional parameters)
---

# /skillweave-blueprint

**Structured product development from idea to blueprint.**  
Create a complete PRD (Product Requirements Document) through guided interview, adapting Ralph Loop concepts for multi-agent AI development.

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
All generated planning documents, PRDs (`prd.json`), task breakdowns, and execution sequences MUST be saved exclusively within `.skillweave/` or its sub-folders. Never dump artifacts into the repository root or `/prd/` or `/docs/`.

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

```
skillweave-blueprint idea="[your project idea]" domain="[domain]"
```
**Or start interactive interview:**
```
skillweave-blueprint
```

**Parameters:**
- `idea` (optional): Initial project idea or concept
- `domain` (optional): Domain context (e.g., saas, mobile, web, enterprise)
- `complexity` (optional): Complexity level (simple, medium, complex)
- `output_format` (optional): Output format (json, markdown, both)
- `risk_mode` (optional): `conservative`, `medium`, `unicorn` - overrides environment variable and config files

**Example:**
```
skillweave-blueprint idea="AI-powered task management tool" domain="saas"
```

## Mode Configuration

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

### Mode-Specific Behavior

**Conservative Mode** (maximum safety):
- Require explicit approval for all assumptions before proceeding
- Validate all inputs with strict rules (e.g., check for completeness, realism)
- Generate detailed documentation with extensive rationale
- Suggest conservative, battle-tested technology choices
- Add extra validation steps and confirmations

**Medium Mode** (balanced, default):
- Standard validation and approval process
- Generate comprehensive documentation
- Suggest balanced technology choices

**Unicorn Mode** (maximum creativity):
- Minimal validation (accept optimistic assumptions)
- Make optimistic assumptions when information is missing
- Generate lightweight, concise documentation
- Suggest cutting-edge, innovative technology choices
- Skip unnecessary confirmations to speed up process

Adjust your interview questions, validation rigor, documentation depth, and technology suggestions according to the effective risk mode.

## Next Level Features

SkillWeave Next Level provides advanced capabilities that can enhance the blueprint process. These features are controlled by `.skillweave/config.yaml` and can be accessed via the `SkillWeaveNextLevel` class.

### Checklist-Based Execution
If `checklist: true` is set in the config, the skill will:
- Parse markdown checklists (`- [ ]` and `- [x]`) from input or attached files
- Track checklist item completion across sessions using `.skillweave/tracking-log/`
- Loop until all checklist items are marked complete
- Provide progress reports and remaining items

### Design-Thinking Lens  
If `design_thinking: true` is set in the config, apply these cognitive ergonomics principles:
1. **Value ≥ Noise**: Ensure every output provides clear user value
2. **Scan Before Read**: Structure content for quick scanning with clear headings
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
- Load and combine templates from `.skillweave/templates/`
- Use template inheritance for consistent documentation
- Generate custom PRD sections from reusable components

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
    current_skill="skillweave-blueprint",  # This skill's name
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

## Interactive PRD Creation Process

### Phase 1: Discovery Interview
The skill conducts a structured interview to understand your project:

**Mode Adaptation:** Based on the configured mode, adjust the level of validation, detail, and approval requirements:
- **Conservative:** For each answer, validate completeness and realism, ask for explicit confirmation of assumptions.
- **Medium:** Standard validation, ask for clarification when needed.
- **Unicorn:** Accept optimistic assumptions, focus on speed over exhaustive validation.

1. **Project Vision & Goals**
   - What problem are you solving?
   - Who are the target users?
   - What's the core value proposition?
   - What makes this unique?

2. **Functional Requirements**
   - Core features and functionality
   - User workflows and interactions
   - Integration requirements
   - Data and storage needs

3. **Technical Considerations**
   - Preferred tech stack (if any)
   - Performance requirements
   - Scalability needs
   - Security considerations
   - **Technology suggestions based on mode:**
     - *Conservative:* Suggest battle-tested, stable technologies with strong community support
     - *Medium:* Suggest balanced mix of mature and modern technologies
     - *Unicorn:* Suggest cutting-edge, innovative technologies that provide competitive advantage

4. **Success Criteria**
   - How will you measure success?
   - What are the key metrics?
   - What's the timeline?
   - What are the constraints?

### Phase 2: PRD Generation
Based on interview responses, creates a structured PRD with:

**Documentation Depth based on Mode:**
- **Conservative:** Include extensive rationale, detailed acceptance criteria, thorough risk analysis, comprehensive appendices.
- **Medium:** Include standard sections with clear acceptance criteria and reasonable detail.
- **Unicorn:** Keep documentation concise, focus on essential information, skip lengthy explanations.

```
# Product Requirements Document

## 1. Executive Summary
- Project overview and vision
- Business objectives
- Key differentiators

## 2. Problem Statement
- Problem being solved
- Current alternatives and their limitations
- Opportunity size and impact

## 3. Target Users & Personas
- Primary user personas
- Secondary stakeholders
- User needs and pain points

## 4. Solution Overview
- Core solution description
- Key features and capabilities
- User journey mapping

## 5. Functional Requirements
### 5.1 Core Features
- [Feature 1]: Description, acceptance criteria
- [Feature 2]: Description, acceptance criteria

### 5.2 User Stories
- As a [user], I want to [action] so that [benefit]
- Priority: [High/Medium/Low]

## 6. Non-Functional Requirements
- Performance requirements (response times, load capacity)
- Security requirements (authentication, data protection)
- Scalability requirements (growth projections)
- Compliance requirements (regulations, standards)

## 7. Technical Architecture
- Proposed tech stack
- System architecture diagram
- Data model overview
- Integration points
- Data boundaries: every crossing of storage, process, adapter, telemetry or public-api
  requires an exact versioned data contract (artifact, versioning, producer,
  consumer, compatibility). Prose alone is not a contract.

## 8. Success Metrics (Binary & Testable)
- [Metric 1]: Target value, measurement method
- [Metric 2]: Target value, measurement method

## 9. Scope & Constraints
### In Scope
- What WILL be built (Phase 1)
- What WILL be built (Future phases)

### Out of Scope
- What WILL NOT be built
- Explicit limitations

## 10. Timeline & Milestones
- Development phases
- Key deliverables
- Dependencies and risks

## 11. Resource Requirements
- Development resources
- Infrastructure needs
- Third-party services

## 12. Assumptions & Dependencies
- Key assumptions
- External dependencies
- Risk factors and mitigation
```

### Phase 3: Task Breakdown (prd.json)
Creates a task list in Ralph Loop format for execution:

```json
{
  "projectName": "Project Name",
  "version": "1.0",
  "status": "draft",
  "tasks": [
    {
      "id": "ARCH-001",
      "title": "Set up project structure",
      "description": "Initialize repository, configure build tools, set up CI/CD",
      "acceptanceCriteria": [
        "Repository exists with README",
        "Package.json/requirements.txt configured",
        "Build script runs without errors",
        "CI pipeline passes"
      ],
      "priority": "high",
      "estimatedEffort": "2",
      "dependsOn": [],
      "type": "infrastructure",
      "passes": false
    },
    {
      "id": "FEAT-001",
      "title": "Implement user authentication",
      "description": "Create signup, login, password reset functionality",
      "acceptanceCriteria": [
        "Users can register with email/password",
        "Users can login and receive JWT token",
        "Password reset flow works",
        "Tests cover 90% of auth logic"
      ],
      "priority": "high",
      "estimatedEffort": "5",
      "dependsOn": ["ARCH-001"],
      "type": "feature",
      "passes": false
    },
    {
      "id": "DATA-001",
      "title": "Persist user sessions to the session store",
      "description": "Crosses the storage boundary: writes session records to the session store and reads them back at request time.",
      "acceptanceCriteria": [
        "Session record written on login and read on the next request",
        "Old session records remain readable after a schema addition"
      ],
      "priority": "high",
      "estimatedEffort": "5",
      "dependsOn": ["ARCH-001"],
      "type": "feature",
      "passes": false,
      "boundaries": [
        {
          "kind": "storage",
          "data_contract": {
            "artifact": "session record",
            "versioning": "schema v2; v1 records remain readable",
            "producer": "auth service",
            "consumer": "request middleware",
            "compatibility": "backward compatible: new optional fields only",
            "subject_kind": "configuration"
          }
        }
      ]
    }
  ]
}
```

### Data-boundary contract (required for any boundary crossing)

A task that crosses an architectural boundary MUST declare a `boundaries`
array; a task with no data-boundary change omits it and validates unchanged.

- **Contract-requiring kinds:** `storage`, `process`, `adapter`, `telemetry`,
  `public-api`. Any other kind is refused — a boundary is never guessed.
- **Every boundary requires a `data_contract`** carrying all five fields:
  `artifact`, `versioning`, `producer`, `consumer`, `compatibility`. A missing
  or blank field is an *undefined required field* and fails with a
  task-specific diagnostic.
- **Prose is not a contract.** A boundary whose `data_contract` is a bare
  string (e.g. `"data_contract": "the session store"`) is refused.
- `subject_kind` is optional and must use the WorkContract subject vocabulary
  (`repository`, `content`, `configuration`, `deployment`, `incident`).

Validation: `skillweave.blueprint.data_boundary_contract.validate_prd_data_boundaries(prd)`.

### Phase 4: Memory System Setup
Creates memory files for Ralph Loop execution:

**progress.txt template:**
```
## Project: [Project Name]
## Created: [Date]
## Status: Planning

### Iteration History
[Will be populated during execution]

### Key Decisions
[Will track architectural and implementation decisions]

### Learnings & Patterns
[Will accumulate knowledge across iterations]
```

**agents.md template:**
```
# Project Patterns & Guidelines

## Architecture Patterns
- [Patterns discovered during development]

## Code Standards
- [Coding conventions for this project]

## Gotchas & Solutions
- [Common issues and their solutions]

## Integration Notes
- [External service integration details]
```

## Output Options

The blueprint skill can generate:

1. **Complete PRD Package** (Recommended)
   - `prd.md` - Full PRD document
   - `prd.json` - Task list for execution
   - `progress.txt` - Progress tracking template
   - `agents.md` - Knowledge base template
   - `README.md` - Project overview

2. **PRD Only** - Just the PRD document
3. **Task List Only** - Just the prd.json for execution
4. **Custom Selection** - Choose specific components

## Integration with SkillWeave Workflow

The blueprint creates the foundation for the complete SkillWeave development flow:

```
Blueprint → PromptChain → ReleaseChain
```

1. **Blueprint** creates structured PRD and task list
2. **PromptChain** uses PRD to generate execution sequences
3. **ReleaseChain** executes tasks with Ralph Loop principles

## Ralph Loop Adaptations

This blueprint skill adapts key Ralph Loop concepts:

### 1. **Binary Success Criteria**
- All acceptance criteria must be testable (pass/fail)
- No subjective criteria like "looks good" or "feels right"
- Every requirement has a verification method

### 2. **Atomic Task Design**
- Each task completes in one AI iteration
- Tasks fit within context window limits
- Clear dependencies between tasks
- Natural stopping points for verification

### 3. **Memory Architecture**
- Short-term memory (`progress.txt`) for iteration tracking
- Long-term memory (`agents.md`) for accumulated knowledge
- Structured format for AI readability

### 4. **Feedback Loops**
- Built-in verification steps for each task
- Automated testing requirements
- Quality gates before task completion

## Complexity Assessment & Execution Recommendation

Based on analysis of your PRD tasks, the blueprint provides recommendations for execution strategy:

### Assessment Criteria
1. **Task Count**: Number of tasks in PRD
2. **Estimated Duration**: Sum of `estimated_minutes` across tasks
3. **Dependency Complexity**: Depth of dependency graph, cycles
4. **Agent Diversity**: Number of different agent types required
5. **Risk Level**: Based on risk assessment in PRD
6. **Task Type Variety**: Different categories (infrastructure, API, UI, etc.)

### Recommended Execution Strategies

#### 1. **Simple Mode (REX-style)**
- **When**: 1-3 tasks, <60 minutes total, simple dependencies
- **Workflow**: Plan → Implement → Review → Done
- **Best for**: Quick fixes, small features, prototype validation
- **Tools**: Direct execution with simple feedback loop

#### 2. **Standard Mode (Ralph Loop Attended)**
- **When**: 4-10 tasks, 1-4 hours total, moderate complexity
- **Workflow**: Ralph Loop with human checkpoints
- **Best for**: Feature development, moderate refactoring
- **Tools**: Full Ralph Loop with progress tracking and memory

#### 3. **Complex Mode (Ralph Loop Overnight)**
- **When**: 10+ tasks, >4 hours, complex dependencies
- **Workflow**: Fully autonomous Ralph Loop execution
- **Best for**: Major features, system overhauls, new projects
- **Tools**: Overnight execution with comprehensive verification

### Native Methodology Declaration

The blueprint declares the execution **methodology**, its **autonomy policy** and
its **session schedule** as three *separate* fields on the PRD's `sequence`
block — never as one conflated mode string. This is the native surface the
promptchain-generate, promptchain-validate, promptchain-execute and regen
producer preserve end to end:

```json
"sequence": {
  "sequence_id": "my-project",
  "sequence_type": "build",
  "methodology": "assay",
  "autonomy_policy": "unicorn",
  "session_schedule": "batch"
}
```

- `methodology` is one of `rex`, `ralph`, `gauntlet`, `assay`. **Assay is never
  silently substituted by Ralph** — the value the blueprint declares is the
  value every downstream stage preserves.
- `autonomy_policy` is the methodology's own policy axis (`conservative`,
  `medium`, `unicorn`), carried separately from the methodology. `unicorn` is
  Assay's declared default; it is not a methodology.
- `session_schedule` is separate again from both.

### Automatic Recommendation (explicit legacy compatibility)

The blueprint may additionally include an `execution_recommendation` in the
generated `prd.json`. This is **legacy compatibility only**: its
`suggested_workflow` is an *adapter* over the legacy vocabulary, and it must be
**consistent with the declared methodology or the producer refuses it**. A
`suggested_workflow` that would route execution to a *different* methodology
than the sequence declares is a hard error (`suggested_workflow_would_substitute_methodology`)
— the compatibility path never silently rewrites a different methodology into
the run.

```json
"execution_recommendation": {
  "mode": "standard",
  "reason": "5 tasks, 2.5 hours total, moderate dependencies",
  "suggested_workflow": "ralph-loop-attended",
  "estimated_iterations": 8,
  "parallel_opportunities": 2
}
```

The legacy `suggested_workflow` vocabulary and the methodology it denotes:

| `suggested_workflow` | denotes methodology |
|----------------------|---------------------|
| `rex-simple`         | `rex`               |
| `ralph-loop-attended`| `ralph`             |
| `ralph-loop-overnight`| `ralph`            |
| `direct-execution`   | `rex`               |

An unrecognised `suggested_workflow` is refused rather than coerced, and a
workflow denoting a different methodology than the declared one is refused with
`legacy_would_substitute_methodology`. Omitting `execution_recommendation`
entirely is valid: compatibility is explicit and never a silent default.

### REX vs Ralph Loop Decision Guide

| Aspect | REX (Simple) | Ralph Loop (Standard/Complex) |
|--------|--------------|-------------------------------|
| **Task Count** | 1-3 tasks | 4+ tasks |
| **Duration** | <1 hour | >1 hour |
| **Dependencies** | Simple, linear | Complex, branching |
| **Risk** | Low | Medium/High |
| **Learning Value** | Low (one-off) | High (accumulates knowledge) |
| **Setup Overhead** | Minimal | Moderate (memory system) |
| **Best For** | Quick wins, proofs of concept | Production features, systems |

**Rule of thumb**: When in doubt, start with REX for speed, switch to Ralph Loop when complexity emerges. Either way, record the choice as an explicit
`methodology` declaration — not as a workflow string a producer might re-route.

## Usage Examples

### Example 1: Complete SaaS Application
```
skillweave-blueprint idea="AI-powered content planning platform for marketers" domain="saas"
```

### Example 2: Internal Tool
```
skillweave-blueprint idea="Employee onboarding automation system" domain="enterprise"
```

### Example 3: Mobile App
```
skillweave-blueprint idea="Fitness tracking app with social features" domain="mobile"
```

## Next Steps

After creating the blueprint:

1. **Review the PRD** - Ensure it captures your vision
2. **Adjust task priorities** - Reorder based on dependencies
3. **Set up your repository** - Initialize with the generated files
4. **Run PromptChain** - Generate execution sequences from the PRD
5. **Execute with ReleaseChain** - Start autonomous development

## Agent-Agnostic Design

SkillWeave Blueprint is designed to be **agent-agnostic** – it works with any AI coding agent that reads Markdown or speaks MCP, across all supported transports.

### How It Works

1. **Capability-Based Assignment**: Instead of specifying concrete agents, the blueprint defines **required capabilities**:
   - `planning`: Strategic thinking, architecture design
   - `code_generation`: Writing and modifying code
   - `testing`: Creating and running tests
   - `review`: Code review and quality assessment
   - `research`: Information gathering and analysis
   - `automation`: Scripting and workflow automation
   - `infrastructure`: System setup and configuration

2. **Runtime Agent Mapping**: During execution, the SkillWeave runtime maps these capabilities to available agents based on:
   - Agent registry and capability declarations
   - Historical performance data
   - Current availability and load
   - User preferences and configurations

3. **Flexible Configuration**: Assignment is capability-based by default (`target_agent: any`). Pin a concrete host only via an explicit, user-supplied adapter — never as a built-in default:
   ```json
   {
     "target_agent": "any",  // Recommended: let the runtime map capabilities to an available agent
     "required_capabilities": ["code_generation", "testing"]
   }
   ```

### Benefits

- **Future-Proof**: Works with new agents as they emerge
- **Flexible**: Can use different agents for different tasks
- **Resilient**: Falls back to available agents if preferred ones are unavailable
- **Optimized**: Routes tasks to best-suited agents automatically

### Integration with SkillWeave Runtime

The blueprint generates PRDs that work with the SkillWeave agent orchestration layer, which handles:
- Agent discovery and capability registration
- Task routing based on capabilities
- Load balancing across multiple agents
- Fallback strategies and error recovery

This ensures your development plans remain executable regardless of which specific AI coding agents you have access to.

## Companion Files

This skill works with:
- `references/prd-template.md` - PRD structure template
- `references/task-breakdown-guide.md` - Task decomposition guidelines
- `references/ralph-loop-adaptation.md` - Ralph Loop principles for multi-agent
- `references/complexity-assessment.md` - Complexity scoring and execution recommendations
- `assets/prd.schema.json` - JSON schema for prd.json

---

**The blueprint skill turns vague ideas into executable development plans, bridging the gap between concept and implementation with AI-assisted structured planning.**