"""SkillWeave Core Planning & Decomposition Module (SW-PLAN-001).

Provides:
- Explicit decomposition metadata and decomposition plans.
- Dispatchability assessments and gates for decomposed units.
- Fibonacci point validation and dependency cycle detection.
- Unit eligibility and fail-closed dispatch validation.
- Authority-aware planning ticket handshake (SW-159-BP-TICKET-001).
"""

from .decomposition import (
    ComplexityLevel,
    CriterionCoverageError,
    DecompositionError,
    DecompositionMetadata,
    DecompositionPlan,
    DecompositionStrategy,
    DecompositionUnit,
    DependencyCycleError,
    FIBONACCI_POINTS,
    create_decomposition_plan,
)

from .dispatchability import (
    DispatchabilityAssessment,
    DispatchabilityError,
    DispatchabilityEvaluator,
    DispatchabilityRequirement,
    DispatchabilityStatus,
    evaluate_dispatchability,
    get_dispatchable_units,
    validate_dispatchability,
)

from .planning_handshake import (
    HandshakeError,
    HandshakeEvidence,
    HandshakeResult,
    HandshakeTerminal,
    PlanningBoardInfo,
    create_ticket_on_board,
    detect_planning_repository,
    link_ticket,
    perform_handshake,
)

__all__ = [
    # Decomposition
    "ComplexityLevel",
    "CriterionCoverageError",
    "DecompositionError",
    "DecompositionMetadata",
    "DecompositionPlan",
    "DecompositionStrategy",
    "DecompositionUnit",
    "DependencyCycleError",
    "FIBONACCI_POINTS",
    "create_decomposition_plan",
    # Dispatchability
    "DispatchabilityAssessment",
    "DispatchabilityError",
    "DispatchabilityEvaluator",
    "DispatchabilityRequirement",
    "DispatchabilityStatus",
    "evaluate_dispatchability",
    "get_dispatchable_units",
    "validate_dispatchability",
    # Planning handshake (SW-159-BP-TICKET-001)
    "HandshakeError",
    "HandshakeEvidence",
    "HandshakeResult",
    "HandshakeTerminal",
    "PlanningBoardInfo",
    "create_ticket_on_board",
    "detect_planning_repository",
    "link_ticket",
    "perform_handshake",
]
