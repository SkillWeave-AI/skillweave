"""External adapters for the Discovery lifecycle-consumer test (SW-ADAPT-001).

These are descriptors + backends the core has never heard of. A Discovery binding
names one through ``adapter_factories`` (``module:callable``), so Discovery
research reaches it through the *public* extension registry with **no core edit**.
None performs a network call: a scenario returns a synthetic, clearly-labelled
outcome, so wiring and origin are proven without ever calling a provider.

Scenarios:

* :func:`discovery_success_factory` -- a typed ``success`` with one real https
  source;
* :func:`discovery_outage_factory` -- a typed ``unavailable`` (an outage, no
  source);
* :func:`discovery_malformed_factory` -- a backend that returns a bare ``dict``
  rather than a typed outcome, so a backend lying about its result type is
  refused as ``malformed``;
* :func:`credentialed_factory` -- a descriptor that declares a credential
  reference, for the pre-invocation credential gate.
"""

from skillweave.adapters import (
    CATEGORY_SEARCH,
    SEARCH_OPERATION,
    AdapterBackend,
    AdapterDescriptor,
    OperationBinding,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
)

_SUCCESS_ID = "external-discovery-search"
_OUTAGE_ID = "external-discovery-outage"
_MALFORMED_ID = "external-discovery-malformed"
_CRED_ID = "external-discovery-credentialed"


def _descriptor(adapter_id, *, capabilities=("search.query",), credential_refs=()):
    return AdapterDescriptor(
        id=adapter_id,
        version="0.5.0",
        title=f"External {adapter_id}",
        categories=(CATEGORY_SEARCH,),
        operations=(
            OperationBinding(
                name=SEARCH_OPERATION,
                input_contract="search-query/v1",
                result_contract="search-outcome/v1",
                required_capabilities=tuple(capabilities),
            ),
        ),
        transport="in-process",
        capabilities=tuple(capabilities),
        credential_refs=tuple(credential_refs),
    )


class _SuccessBackend(AdapterBackend):
    async def invoke(self, operation, argument, context):
        query = str(argument.get("query", ""))
        return SearchOutcome(
            query_sha256="a" * 64,
            requested_provider="synthetic-external",
            status="success",
            results=(
                SearchResultRecord(
                    title=f"external:{query}",
                    url="https://external.example.org/discovery",
                    snippet="synthetic result from an external discovery adapter",
                    source="web",
                    provider="synthetic-external",
                ),
            ),
            attempts=(SearchAttempt("synthetic-external", "in-process", "success"),),
        )


class _OutageBackend(AdapterBackend):
    async def invoke(self, operation, argument, context):
        query = str(argument.get("query", ""))
        return SearchOutcome(
            query_sha256="b" * 64,
            requested_provider="synthetic-external",
            status="unavailable",
            attempts=(
                SearchAttempt(
                    "synthetic-external", "in-process", "unavailable", "provider_down"
                ),
            ),
            error_code="provider_down",
        )


class _MalformedBackend(AdapterBackend):
    async def invoke(self, operation, argument, context):
        # A backend that returns the wrong type must be refused, never trusted.
        return {"status": "success", "results": [{"url": "not-a-real-source"}]}


class _CredentialedBackend(AdapterBackend):
    async def invoke(self, operation, argument, context):
        return SearchOutcome(
            query_sha256="c" * 64,
            requested_provider="synthetic-external-credentialed",
            status="unavailable",
            attempts=(
                SearchAttempt(
                    "synthetic-external-credentialed",
                    "in-process",
                    "unavailable",
                    "no_credential",
                ),
            ),
            error_code="no_credential",
        )


def discovery_success_factory():
    return (_descriptor(_SUCCESS_ID), _SuccessBackend())


def discovery_outage_factory():
    return (_descriptor(_OUTAGE_ID), _OutageBackend())


def discovery_malformed_factory():
    return (_descriptor(_MALFORMED_ID), _MalformedBackend())


def credentialed_factory():
    return (
        _descriptor(
            _CRED_ID,
            capabilities=("search.query",),
            credential_refs=("DISCOVERY_SEARCH_KEY",),
        ),
        _CredentialedBackend(),
    )
