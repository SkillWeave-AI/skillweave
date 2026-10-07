"""Provider-neutral adapter facade, registry and search/tool backends.

This package is additive: it introduces a neutral, SDK-owned adapter
descriptor contract and an explicit extension registry that Core resolves
through. It creates no WorkContract and holds no gate-authority. Existing
``search-provider/v1`` / ``model-provider/v1`` contracts and the lifecycle
``ProviderRegistry`` stay intact; adapters compose those domain ports rather
than replacing them.
"""

from .descriptor import (
    ADAPTER_CATEGORIES,
    ADAPTER_DESCRIPTOR_SCHEMA_VERSION,
    CATEGORY_GENERIC,
    CATEGORY_SEARCH,
    AdapterDescriptor,
    AdapterDescriptorError,
    OperationBinding,
    PackageRef,
)
from .format_readers import (
    CAPACIUM_DIALECT,
    CAPACIUM_DIALECT_VERSION,
    LOCAL_DIALECT,
    AdapterBinding,
    CapaciumFormatReader,
    FormatReaderError,
    LocalRegistrationReader,
    PackageFormatReader,
    ReaderRegistry,
    UnadmittedMemberError,
    UnsupportedDialectError,
    default_reader_registry,
)
from .mcp_client import MCP_PROTOCOL_VERSION, McpClientError, McpStdioClient
from .outcomes import (
    AVAILABILITY_STATES,
    CREDENTIAL_REFERENCE_SCHEMA_VERSION,
    INJECTION_OWNED_CHILD_ENVIRONMENT,
    SEARCH_OUTCOME_CONTRACT,
    SEARCH_OUTCOME_SCHEMA_VERSION,
    SEARCH_STATUSES,
    CredentialReference,
    OperationReceipt,
    OutcomeError,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
    ToolOutcome,
)
from .policy import (
    AdmissionDecision,
    EffectivePolicy,
    EgressPolicy,
    PolicyError,
    admit_invocation,
)
from .registry import (
    AdapterBackend,
    AdapterEntry,
    AdapterError,
    AdapterFacade,
    AdapterRegistry,
    Resolution,
    UnknownAdapterError,
    UnsupportedVersionError,
)
from .search_backends import (
    BASE_SEARCH_CAPABILITIES,
    EGRESS_CAPABILITY,
    PROVIDER_OUTCOME_CONTRACT,
    SEARCH_OPERATION,
    SEARCH_PROVIDER_SPECS,
    SearchAdapterBackend,
    SearchProviderSpec,
    build_search_registry,
)

__all__ = [
    "ADAPTER_DESCRIPTOR_SCHEMA_VERSION",
    "ADAPTER_CATEGORIES",
    "CATEGORY_SEARCH",
    "CATEGORY_GENERIC",
    "AdapterDescriptor",
    "AdapterDescriptorError",
    "OperationBinding",
    "PackageRef",
    "MCP_PROTOCOL_VERSION",
    "McpClientError",
    "McpStdioClient",
    # Package-format readers (Capacium optional standard / delivery only).
    "FormatReaderError",
    "UnsupportedDialectError",
    "UnadmittedMemberError",
    "AdapterBinding",
    "PackageFormatReader",
    "LocalRegistrationReader",
    "CapaciumFormatReader",
    "ReaderRegistry",
    "default_reader_registry",
    "LOCAL_DIALECT",
    "CAPACIUM_DIALECT",
    "CAPACIUM_DIALECT_VERSION",
    "CREDENTIAL_REFERENCE_SCHEMA_VERSION",
    "SEARCH_OUTCOME_SCHEMA_VERSION",
    "SEARCH_OUTCOME_CONTRACT",
    "SEARCH_STATUSES",
    "AVAILABILITY_STATES",
    "INJECTION_OWNED_CHILD_ENVIRONMENT",
    "OutcomeError",
    "CredentialReference",
    "SearchResultRecord",
    "SearchAttempt",
    "SearchOutcome",
    "OperationReceipt",
    "ToolOutcome",
    "PolicyError",
    "EgressPolicy",
    "EffectivePolicy",
    "AdmissionDecision",
    "admit_invocation",
    "AdapterError",
    "UnknownAdapterError",
    "UnsupportedVersionError",
    "AdapterBackend",
    "AdapterEntry",
    "AdapterRegistry",
    "Resolution",
    "AdapterFacade",
    "SEARCH_OPERATION",
    "BASE_SEARCH_CAPABILITIES",
    "EGRESS_CAPABILITY",
    "PROVIDER_OUTCOME_CONTRACT",
    "SEARCH_PROVIDER_SPECS",
    "SearchProviderSpec",
    "SearchAdapterBackend",
    "build_search_registry",
]
