"""SkillWeave blueprint surface.

The discovery trace graph contract (SW-159-BP-TRACE-001) turns applicable
discovery Markdown into a digest-bound trace graph: every used statement is
recorded with its source path, digest, heading and stable problem ID, and every
problem is linked — through a versioned TraceLink — to an epic, a task, or a
named deferral. Nothing here launches a worker or changes non-discovery
blueprint behavior; the contract is opt-in and standard-library only.

The data-boundary contract (SW-159-BP-CONTRACT-001) requires an exact versioned
data contract whenever a PRD task crosses an architectural boundary (storage,
process, adapter, telemetry, public-api). A prose-only crossing is refused with
a task-specific diagnostic, and a task with no data-boundary change validates
exactly as before. It consumes the integrated WorkContract subject vocabulary by
parity, without importing the dispatch package.
"""

from .data_boundary_contract import (  # noqa: F401
    CONTRACT_REQUIRING,
    NO_DATA_BOUNDARY,
    BOUNDARY_STORAGE,
    BOUNDARY_PROCESS,
    BOUNDARY_ADAPTER,
    BOUNDARY_TELEMETRY,
    BOUNDARY_PUBLIC_API,
    BOUNDARY_KINDS,
    REQUIRED_DATA_CONTRACT_FIELDS,
    WORK_CONTRACT_SUBJECT_KINDS,
    DataBoundaryContractError,
    UnknownBoundaryKindError,
    ProseOnlyBoundaryError,
    UndefinedContractFieldError,
    InvalidDataContractError,
    DataContract,
    normalize_boundary_kind,
    is_contract_requiring,
    classify_task_boundary,
    validate_prd_data_boundaries,
)

from .discovery_trace import (  # noqa: F401
    SCHEMA_VERSION,
    TRACE_LINK_VERSION,
    DISPOSITION_EPIC,
    DISPOSITION_TASK,
    DISPOSITION_DEFERRED,
    DiscoveryTraceError,
    MissingMandatoryProblemError,
    UnlinkedProblemError,
    DigestDriftError,
    UnresolvedConflictError,
    InvalidDeferralError,
    DiscoverySource,
    Deferral,
    TraceLink,
    DiscoveryTraceGraph,
    parse_discovery_markdown,
    detect_conflicts,
    build_trace_graph,
    compute_digest,
    seal,
    canonicalize,
    validate,
    verify_grounding,
    machine_mapping,
    to_machine,
    render_human,
    parse_human_mapping,
    load_schema,
)

__all__ = [
    "CONTRACT_REQUIRING",
    "NO_DATA_BOUNDARY",
    "BOUNDARY_STORAGE",
    "BOUNDARY_PROCESS",
    "BOUNDARY_ADAPTER",
    "BOUNDARY_TELEMETRY",
    "BOUNDARY_PUBLIC_API",
    "BOUNDARY_KINDS",
    "REQUIRED_DATA_CONTRACT_FIELDS",
    "WORK_CONTRACT_SUBJECT_KINDS",
    "DataBoundaryContractError",
    "UnknownBoundaryKindError",
    "ProseOnlyBoundaryError",
    "UndefinedContractFieldError",
    "InvalidDataContractError",
    "DataContract",
    "normalize_boundary_kind",
    "is_contract_requiring",
    "classify_task_boundary",
    "validate_prd_data_boundaries",
    "SCHEMA_VERSION",
    "TRACE_LINK_VERSION",
    "DISPOSITION_EPIC",
    "DISPOSITION_TASK",
    "DISPOSITION_DEFERRED",
    "DiscoveryTraceError",
    "MissingMandatoryProblemError",
    "UnlinkedProblemError",
    "DigestDriftError",
    "UnresolvedConflictError",
    "InvalidDeferralError",
    "DiscoverySource",
    "Deferral",
    "TraceLink",
    "DiscoveryTraceGraph",
    "parse_discovery_markdown",
    "detect_conflicts",
    "build_trace_graph",
    "compute_digest",
    "seal",
    "canonicalize",
    "validate",
    "verify_grounding",
    "machine_mapping",
    "to_machine",
    "render_human",
    "parse_human_mapping",
    "load_schema",
]
