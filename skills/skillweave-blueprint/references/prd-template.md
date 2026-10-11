# PRD (Product Requirements Document) Template

Adapted from Ralph Loop principles for multi-agent AI development. This template ensures structured, testable requirements for autonomous execution.

## Document Structure

### 1. Executive Summary
**Purpose:** High-level overview for stakeholders
**Content:**
- Project name and tagline
- Core problem being solved
- Target market/users
- Key differentiators
- Business objectives (revenue, users, engagement)
- Success definition (what does "done" look like?)

### 2. Problem Statement
**Purpose:** Clear definition of the problem space
**Content:**
- Current situation (pain points, inefficiencies)
- Existing alternatives and their limitations
- Opportunity size and impact
- User frustrations (quote actual user feedback if available)
- Business impact (cost, time, quality)

### 3. Target Users & Personas
**Purpose:** Define who we're building for
**Content:**

**Primary Persona: [Persona Name]**
- Demographics: Age, role, tech proficiency
- Goals: What they want to achieve
- Pain Points: Specific frustrations
- Usage Context: When/how they'll use the product
- Quote: "I wish I could..."

**Secondary Personas:** [List other user types]
**Stakeholders:** [Internal teams, partners, etc.]

### 4. Solution Overview
**Purpose:** High-level description of the solution
**Content:**
- Core value proposition (one sentence)
- Key features and capabilities
- User journey (before/after using solution)
- Technical approach overview
- How it solves the problems from Section 2

### 5. Functional Requirements
**Purpose:** Detailed feature specifications

#### 5.1 Core Features
For each feature:

**Feature: [Feature Name]**
- Description: What it does
- User Benefit: Why users care
- Priority: [Critical/High/Medium/Low]
- Acceptance Criteria:
  1. [Binary, testable criterion 1]
  2. [Binary, testable criterion 2]
  3. [Binary, testable criterion 3]
- Dependencies: [Other features, external services]
- Mockups/Diagrams: [References to design]

#### 5.2 User Stories
Format: "As a [user], I want to [action] so that [benefit]"

**Example:**
- As a project manager, I want to assign tasks to team members so that responsibilities are clear
- Priority: High
- Acceptance: Task assignment interface exists, notifications sent, assignment visible in dashboard

### 6. Non-Functional Requirements
**Purpose:** Quality attributes and constraints

#### Performance
- Response time: < 2 seconds for 95% of requests
- Concurrent users: Support 1000+ simultaneous users
- Load capacity: Handle X requests per second
- Data volume: Support Y records in database

#### Security
- Authentication: OAuth 2.0 / JWT tokens
- Authorization: Role-based access control
- Data protection: Encryption at rest and in transit
- Compliance: GDPR, HIPAA, SOC2 (if applicable)

#### Reliability & Availability
- Uptime: 99.9% availability
- Backup: Daily automated backups
- Recovery: RTO < 1 hour, RPO < 15 minutes
- Monitoring: Real-time alerts for critical failures

#### Scalability
- Horizontal scaling: Support auto-scaling
- Database scaling: Read replicas, sharding strategy
- Cache strategy: Redis/Memcached implementation
- CDN: Static assets via CDN

### 7. Technical Architecture
**Purpose:** Implementation approach

#### Tech Stack
- Frontend: [Framework, language, tools]
- Backend: [Framework, language, tools]
- Database: [Type, specific technology]
- Infrastructure: [Cloud provider, services]
- DevOps: [CI/CD, monitoring, logging]

#### System Architecture
- Diagram: High-level component diagram
- Data Flow: How data moves through the system
- Integration Points: External APIs, services
- Deployment: Environment strategy (dev/staging/prod)

#### Data Model
- Entity Relationship Diagram
- Key tables and relationships
- Data migration strategy
- Backup/restore procedures

#### Data Boundaries
List every crossing of `storage`, `process`, `adapter`, `telemetry` or
`public-api`. Each crossing requires an exact versioned data contract — a prose
description is not a contract. State, per boundary:

- Artifact: what crosses
- Versioning: how it is versioned, and which version is contracted
- Producer: who produces it
- Consumer: who consumes it
- Compatibility: how change is tolerated (compatible / breaking-with-migration)

### 8. Success Metrics (Binary & Testable)
**Purpose:** Clear, measurable success criteria

**Business Metrics:**
- [Metric]: [Target] - [Measurement Method]
- Example: User signups: 1000/month - Analytics dashboard
- Example: Revenue: $10k MRR - Stripe dashboard

**Product Metrics:**
- [Metric]: [Target] - [Measurement Method]
- Example: Feature adoption: 40% of users - Feature flag analytics
- Example: User retention: 70% Week 1 - Cohort analysis

**Technical Metrics:**
- [Metric]: [Target] - [Measurement Method]
- Example: Page load time: < 2s - Web vitals monitoring
- Example: API error rate: < 0.1% - Error tracking

### 9. Scope & Constraints
**Purpose:** Define boundaries to prevent scope creep

#### In Scope (Phase 1)
- [Feature/Component] - Why it's included
- [Feature/Component] - Why it's included
- [Feature/Component] - Why it's included

#### In Scope (Future Phases)
- [Feature/Component] - Planned for later
- [Feature/Component] - Planned for later

#### Out of Scope
- [Feature/Component] - Why it's excluded
- [Feature/Component] - Why it's excluded
- [Feature/Component] - Why it's excluded

#### Constraints
- Timeline: [Start date] - [End date]
- Budget: $[Amount] or [Person-months]
- Team size: [Number of developers/roles]
- Technical: Legacy system compatibility, etc.

### 10. Timeline & Milestones
**Purpose:** Phased delivery plan

#### Phase 1: Foundation (Weeks 1-4)
- Milestone: Project setup complete
- Deliverables: Architecture, basic infrastructure
- Dependencies: Team assembled, tools approved

#### Phase 2: Core Features (Weeks 5-8)
- Milestone: MVP feature complete
- Deliverables: Core functionality, basic UI
- Dependencies: Phase 1 complete, design finalized

#### Phase 3: Polish & Scale (Weeks 9-12)
- Milestone: Production ready
- Deliverables: Performance optimization, monitoring
- Dependencies: User testing feedback, security review

#### Phase 4: Launch & Iterate (Week 13+)
- Milestone: Successful launch
- Deliverables: User onboarding, analytics
- Dependencies: Marketing prepared, support trained

### 11. Resource Requirements
**Purpose:** What's needed to build and operate

#### Development Team
- Roles: [Backend, Frontend, DevOps, etc.]
- Skills: [Specific technologies, domain expertise]
- Allocation: [Full-time, part-time, contractors]

#### Infrastructure
- Cloud Services: [AWS/Azure/GCP services]
- Third-party Services: [SaaS tools, APIs]
- Development Tools: [IDEs, collaboration tools]

#### Operational
- Monitoring: [Tools for observability]
- Support: [Customer support channels]
- Documentation: [Internal/external docs]

### 12. Assumptions & Dependencies
**Purpose:** Identify risks and prerequisites

#### Key Assumptions
1. [Assumption] - Impact if wrong
2. [Assumption] - Impact if wrong
3. [Assumption] - Impact if wrong

#### External Dependencies
- [Dependency] - Owner, timeline, risk level
- [Dependency] - Owner, timeline, risk level

#### Risks & Mitigation
- [Risk] - Probability, Impact, Mitigation strategy
- [Risk] - Probability, Impact, Mitigation strategy

## Ralph Loop Adaptations

### Binary Criteria Design
Every acceptance criterion must be:
1. **Testable**: Can be verified with automated tests
2. **Binary**: Clear pass/fail condition
3. **Measurable**: Quantitative where possible
4. **Independent**: Doesn't depend on subjective judgment

**Bad Examples:**
- "The UI should look good" (subjective)
- "Users should find it intuitive" (vague)

**Good Examples:**
- "All buttons have hover states (CSS :hover)"
- "Form validation shows errors within 500ms of submission"
- "API returns 200 status code for valid requests"

### Task Decomposition Principles
1. **Atomic**: Each task completes in one AI iteration
2. **Context-Fitting**: Task fits within LLM context window
3. **Verifiable**: Clear completion criteria
4. **Independent**: Minimal dependencies on other tasks
5. **Committable**: Results in a working, committable change

### Memory System Integration
**Short-term (progress.txt):**
- Iteration-by-iteration tracking
- What worked/what didn't
- Technical decisions and rationale

**Long-term (agents.md):**
- Project-specific patterns
- Code standards and conventions
- Integration knowledge
- Bug patterns and solutions

## Usage Notes for AI Execution

1. **Start with Discovery**: Use interview questions to fill template
2. **Validate Assumptions**: Challenge each assumption
3. **Prioritize Ruthlessly**: Use MoSCoW (Must, Should, Could, Won't)
4. **Define "Done"**: Clear completion criteria for each phase
5. **Plan for Iteration**: Assume requirements will evolve

## Integration with SkillWeave

This PRD template is designed for:
1. **Blueprint Skill**: Creates structured PRD from interview
2. **PromptChain Skill**: Generates execution sequences from PRD
3. **ReleaseChain Skill**: Executes tasks with Ralph Loop principles

The structured format enables autonomous AI development while maintaining human oversight and clear success criteria.