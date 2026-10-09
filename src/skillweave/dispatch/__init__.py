"""Operator dispatch surface (contract, profile resolution, and live events).

Exports the sequence/event contract, the practice task validation, and the
typed live event stream. The worker-launch mechanics are not part of this
lane: the dispatcher itself is SW138-DISPATCH-001, which consumes these
contracts and feeds the event stream.
"""

from .contracts import (  # noqa: F401
    ContractError,
    SequenceBoundaryError,
    LaneValidationError,
    PracticeTaskError,
    ProfileReference,
    CriterionGroup,
    Lane,
    SequenceDeclaration,
    EventType,
    ProcessStatus,
    TaskStatus,
    DispatchEvent,
    SESSION_BOUNDARY_BATCH,
    FIBONACCI_POINTS,
    load_sequence,
    validate_mutating_lane,
    validate_for_dispatch,
    validate_practice_task,
)

# Authoritative profile resolution (SW138-PROFILE-001): the single seam that
# turns an explicit profile path and required roles into a launchable intent.
from .profile_resolution import (  # noqa: F401
    ProfileResolutionError,
    ResolvedModel,
    ResolvedRole,
    ResolvedDispatch,
    resolve_limits,
    resolve_dispatch_profile,
)

from .events import (  # noqa: F401
    DispatchEventStream,
    HeartbeatMonitor,
    EventStreamError,
)

# The experimental application seam and its CLI (SW138-DISPATCH-001). These
# consume the contract/profile/stream exported above and drive a wave through
# the shared fan-out / run / workspace services.
# Worker-to-controller messaging channel (SW-158-RETRO-003).
from .messaging import (  # noqa: F401
    ENV_MESSAGE_CHANNEL,
    MessageChannelError,
    DirectMessage,
    MessageChannel,
    # SW-1510-MCP-AGENTS: the durable, harness-neutral agent mailbox
    AGENT_MESSAGE_ARTIFACT,
    AGENT_MESSAGE_SCHEMA_VERSION,
    AGENT_MESSAGE_NAMESPACE,
    AGENT_MESSAGE_SCHEMA_DIGEST,
    AGENT_MESSAGE_CONTRACT,
    AGENT_MESSAGE_KINDS,
    agent_message_contract_digest,
    UnsupportedMessageVersion,
    MessageMismatchRefused,
    AgentMessage,
    MailboxCursor,
    AgentMailbox,
    MailboxHandoffBridge,
)

from .application import (  # noqa: F401
    EXECUTION_MODELS,
    HALT_REQUIRES_OPERATOR,
    ExecutionModelError,
    OperatorDispatchError,
    ProfileLocationError,
    WorkspaceMismatchError,
    TopologyGateError,
    TopologyGateInput,
    TopologyEnforcement,
    DispatchReport,
    LanePlan,
    DispatchRun,
    GitWorkspaceSeam,
    WorkspaceSeam,
    OperatorDispatchApplication,
    enforce_execution_model,
    derive_topology_manifests,
    enforce_topology,
    generate_run_id,
)

__all__ = [
    "ContractError",
    "SequenceBoundaryError",
    "LaneValidationError",
    "PracticeTaskError",
    "ProfileReference",
    "CriterionGroup",
    "Lane",
    "SequenceDeclaration",
    "EventType",
    "ProcessStatus",
    "TaskStatus",
    "DispatchEvent",
    "SESSION_BOUNDARY_BATCH",
    "FIBONACCI_POINTS",
    "load_sequence",
    "validate_mutating_lane",
    "validate_for_dispatch",
    "validate_practice_task",
    "ProfileResolutionError",
    "ResolvedModel",
    "ResolvedRole",
    "ResolvedDispatch",
    "resolve_limits",
    "resolve_dispatch_profile",
    "DispatchEventStream",
    "HeartbeatMonitor",
    "EventStreamError",
    "EXECUTION_MODELS",
    "HALT_REQUIRES_OPERATOR",
    "ExecutionModelError",
    "OperatorDispatchError",
    "ProfileLocationError",
    "WorkspaceMismatchError",
    "TopologyGateError",
    "TopologyGateInput",
    "TopologyEnforcement",
    "DispatchReport",
    "LanePlan",
    "DispatchRun",
    "GitWorkspaceSeam",
    "WorkspaceSeam",
    "OperatorDispatchApplication",
    "enforce_execution_model",
    "derive_topology_manifests",
    "enforce_topology",
    "generate_run_id",
    # SW-158-RETRO-003
    "ENV_MESSAGE_CHANNEL",
    "MessageChannelError",
    "DirectMessage",
    "MessageChannel",
    # SW-1510-MCP-AGENTS: the durable agent mailbox (agent-message-v1)
    "AGENT_MESSAGE_ARTIFACT",
    "AGENT_MESSAGE_SCHEMA_VERSION",
    "AGENT_MESSAGE_NAMESPACE",
    "AGENT_MESSAGE_SCHEMA_DIGEST",
    "AGENT_MESSAGE_CONTRACT",
    "AGENT_MESSAGE_KINDS",
    "agent_message_contract_digest",
    "UnsupportedMessageVersion",
    "MessageMismatchRefused",
    "AgentMessage",
    "MailboxCursor",
    "AgentMailbox",
    "MailboxHandoffBridge",
]
