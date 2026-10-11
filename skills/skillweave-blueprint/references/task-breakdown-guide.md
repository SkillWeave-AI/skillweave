# Task Breakdown Guide

Adapted from Ralph Loop principles for decomposing PRDs into atomic, AI-executable tasks.

## Core Principles

### 1. Atomic Tasks
Each task must:
- Complete in **one AI iteration** (single context window)
- Result in **working, committable code**
- Have **clear, binary completion criteria**
- Fit within **token/context limits** of target AI

### 2. Dependency Management
- Map dependencies between tasks
- Identify parallel execution opportunities
- Create critical path for sequential work
- Minimize blocking dependencies

### 3. Verification Built-In
- Every task includes verification steps
- Automated tests where possible
- Manual verification steps clearly defined
- Quality gates before task completion

## Task Types & Patterns

### Infrastructure Tasks
**Purpose:** Set up project foundation

**Examples:**
- Initialize repository with README
- Configure build tools (package.json, requirements.txt)
- Set up CI/CD pipeline
- Configure linting and formatting
- Set up testing framework

**Pattern:**
```
Task ID: INFRA-001
Title: Set up project structure
Description: Initialize repository with basic configuration
Acceptance Criteria:
1. Repository exists with README.md
2. package.json/requirements.txt configured with dependencies
3. .gitignore file excludes build artifacts
4. npm run build/pip install works without errors
Type: infrastructure
Priority: critical
Estimated Effort: 2 (small)
Dependencies: none
```

### Database Tasks  
**Purpose:** Set up data storage

**Examples:**
- Create database schema
- Write migrations
- Set up ORM/models
- Create seed data
- Configure backups

**Pattern:**
```
Task ID: DB-001
Title: Create users table
Description: Set up user authentication database schema
Acceptance Criteria:
1. users table exists with columns: id, email, password_hash, created_at
2. Migration runs successfully up and down
3. Model/entity class exists with type definitions
4. Basic CRUD operations work in tests
Type: database
Priority: high
Estimated Effort: 3 (medium)
Dependencies: INFRA-001
```

### API Endpoint Tasks
**Purpose:** Implement REST/GraphQL endpoints

**Examples:**
- Create endpoint with route
- Implement request validation
- Add error handling
- Write integration tests

**Pattern:**
```
Task ID: API-001
Title: Implement POST /users endpoint
Description: Create user registration endpoint
Acceptance Criteria:
1. POST /users accepts email and password
2. Validates email format and password strength
3. Returns 201 with user data on success
4. Returns 400 with errors on validation failure
5. Integration tests pass for all cases
Type: api
Priority: high
Estimated Effort: 4 (medium-large)
Dependencies: DB-001
```

### UI Component Tasks
**Purpose:** Build frontend components

**Examples:**
- Create React/Vue component
- Implement styling
- Add interactivity
- Write component tests

**Pattern:**
```
Task ID: UI-001
Title: Create login form component
Description: Build user login interface
Acceptance Criteria:
1. Component renders email and password fields
2. Form validation shows errors
3. Submit button calls authentication API
4. Loading state during API call
5. Component tests cover 80% of logic
Type: ui
Priority: high
Estimated Effort: 3 (medium)
Dependencies: API-001
```

### Integration Tasks
**Purpose:** Connect systems

**Examples:**
- Integrate third-party API
- Set up webhook handlers
- Configure authentication providers
- Implement file upload to cloud storage

**Pattern:**
```
Task ID: INT-001
Title: Integrate Stripe payment processing
Description: Set up Stripe for payment processing
Acceptance Criteria:
1. Stripe SDK installed and configured
2. Create customer endpoint works
3. Create payment intent endpoint works
4. Webhook handler validates signatures
5. Tests use Stripe test mode
Type: integration
Priority: medium
Estimated Effort: 5 (large)
Dependencies: API-001, INFRA-001
```

### Testing Tasks
**Purpose:** Quality assurance

**Examples:**
- Write unit tests
- Create integration tests
- Set up test data
- Configure test coverage reporting

**Pattern:**
```
Task ID: TEST-001
Title: Add comprehensive authentication tests
Description: Test user registration and login flows
Acceptance Criteria:
1. Unit tests for password validation
2. Integration tests for registration endpoint
3. Integration tests for login endpoint
4. Test edge cases (duplicate email, weak password)
5. Coverage reaches 90% for auth module
Type: testing
Priority: medium
Estimated Effort: 4 (medium-large)
Dependencies: API-001, DB-001
```

## Task Sizing Guidelines

### Small Tasks (1-2 effort points)
- Single file changes
- Configuration updates
- Simple bug fixes
- Documentation updates
- Minor refactoring

**Example:** Add environment variable, update README

### Medium Tasks (3-4 effort points)  
- New endpoint or component
- Database migration
- Integration with simple API
- Test suite for single module

**Example:** Create CRUD endpoint, build form component

### Large Tasks (5-6 effort points)
- Complex feature with multiple components
- Integration with complex API
- Major refactoring
- Performance optimization

**Example:** Implement search with filters, add real-time features

## Dependency Mapping

### Types of Dependencies

#### 1. Technical Dependencies
- Database schema before API endpoint
- API endpoint before UI component
- Infrastructure before application code

#### 2. Logical Dependencies
- Authentication before protected routes
- Data models before business logic
- Core features before enhancements

#### 3. Resource Dependencies
- Shared libraries/components
- Configuration/setup requirements
- External service availability

### Dependency Graph Example
```
INFRA-001 (Project setup)
    │
    ├── DB-001 (Users table)
    │   │
    │   ├── API-001 (User registration)
    │   │   │
    │   │   ├── UI-001 (Login form)
    │   │   │
    │   │   └── TEST-001 (Auth tests)
    │   │
    │   └── API-002 (User profile)
    │
    └── INFRA-002 (CI/CD setup)
```

## Parallel Execution Opportunities

### Independent Workstreams
```
Workstream A (Authentication):
INFRA-001 → DB-001 → API-001 → UI-001 → TEST-001

Workstream B (Core Features):
INFRA-001 → DB-002 → API-002 → UI-002 → TEST-002
```

### Parallel After Dependencies Met
```
Phase 1 (all required):
INFRA-001, DB-001

Phase 2 (parallel after Phase 1):
API-001, API-002, UI-001

Phase 3 (parallel after respective APIs):
TEST-001 (after API-001), TEST-002 (after API-002)
```

## Task Template for prd.json

```json
{
  "id": "TASK-ID",
  "title": "Descriptive task title",
  "description": "Detailed description of what to implement",
  "acceptanceCriteria": [
    "Binary, testable criterion 1",
    "Binary, testable criterion 2",
    "Binary, testable criterion 3"
  ],
  "priority": "critical|high|medium|low",
  "estimatedEffort": "1|2|3|4|5|6",
  "dependsOn": ["OTHER-TASK-ID", "ANOTHER-TASK-ID"],
  "type": "infrastructure|database|api|ui|integration|testing|documentation",
  "files": ["path/to/file1", "path/to/file2"],
  "verificationSteps": [
    "Run specific test command",
    "Check specific metric",
    "Verify specific behavior"
  ],
  "notes": "Additional context or constraints",
  "passes": false
}
```

## Data-Boundary Contracts

A task that crosses an architectural boundary must declare `boundaries`; a task
with no data-boundary change omits it and validates unchanged. The
contract-requiring kinds are `storage`, `process`, `adapter`, `telemetry` and
`public-api`; any other kind is refused. Each boundary requires a
`data_contract` with all five fields `artifact`, `versioning`, `producer`,
`consumer` and `compatibility` — all non-blank. A prose-only `data_contract`
(a bare string) is refused with a task-specific diagnostic, because a described
crossing is not a defined interface.

```json
"boundaries": [
  {
    "kind": "adapter",
    "data_contract": {
      "artifact": "payment authorization response",
      "versioning": "provider API v2023-10-16",
      "producer": "payment provider",
      "consumer": "checkout service",
      "compatibility": "pinned version; upgrade is a separate migration"
    }
  }
]
```

The optional `subject_kind` names the artifact's subject in the integrated
WorkContract vocabulary (`repository`, `content`, `configuration`,
`deployment`, `incident`), so a boundary speaks the same language as the work
contract that authorizes it.

## Verification Strategy

### Automated Verification
- Unit tests (jest, pytest, etc.)
- Integration tests (API, database)
- E2E tests (Cypress, Playwright)
- Static analysis (type checking, linting)
- Build verification (compilation, bundling)

### Manual Verification
- UI/UX review checklist
- Performance testing steps
- Security review points
- Accessibility compliance checks

### Verification Integration
```
Task completion flow:
1. Implement code changes
2. Run automated tests
3. Fix any failures
4. Run build/compilation
5. Update passes: true if all checks pass
6. Commit changes with descriptive message
```

## Common Pitfalls & Solutions

### 1. Tasks Too Large
**Problem:** Task exceeds context window, AI produces partial work
**Solution:** Split into smaller, focused tasks

### 2. Vague Acceptance Criteria
**Problem:** AI can't determine when task is complete
**Solution:** Make criteria binary and testable

### 3. Circular Dependencies
**Problem:** Deadlock in execution order
**Solution:** Analyze and break dependency cycles

### 4. Missing Dependencies
**Problem:** Task fails because prerequisite not met
**Solution:** Thorough dependency analysis

### 5. Inadequate Verification
**Problem:** Task "passes" but doesn't actually work
**Solution:** Include comprehensive verification steps

## Ralph Loop Integration

### Task Design for Ralph Loop
1. **Single Iteration Completion**: Each task finishes in one loop
2. **Self-Contained**: Task includes all needed context
3. **Verifiable**: Clear pass/fail criteria
4. **Committable**: Results in working, commit-ready code

### Progress Tracking
- Update `progress.txt` after each task
- Document learnings and patterns
- Track time/iterations per task type
- Identify patterns for optimization

### Memory System
- Add to `agents.md` for recurring patterns
- Document project-specific conventions
- Capture integration knowledge
- Record bug patterns and solutions

## SkillWeave Integration

This task breakdown approach enables:

1. **Blueprint Skill**: Creates structured task list from PRD
2. **PromptChain Skill**: Generates execution sequences for tasks
3. **Execute Skill**: Runs tasks with parallel execution
4. **ReleaseChain Skill**: Manages Ralph Loop execution with verification

The atomic, verifiable task design is fundamental to autonomous AI development with predictable outcomes.