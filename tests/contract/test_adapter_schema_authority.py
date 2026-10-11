"""Core descriptor/outcome versions are pinned to the SDK-owned schema consts.

SW-ADAPT-001 criterion 1 (single ownership): the SDK authors the neutral
adapter contract bytes; Core *consumes* them. A review of commit ``120461b``
observed that Core's ``ADAPTER_DESCRIPTOR_SCHEMA_VERSION = "1.0.0"`` was
asserted nowhere against the SDK-owned schema's ``const`` -- so "the SDK owns
the versioned neutral descriptor" was not machine-checkable from inside Core,
and Core could drift the version unilaterally while every current test stayed
green.

This contract closes that gap. It reads the **SDK-owned** schema ``$id``
prefixes and the version ``const`` values, and pins Core's own contract
constants to them:

* Core's descriptor version must equal the ``const`` in the SDK's
  ``adapter/adapter-descriptor/v1`` schema.
* Every descriptor Core emits (via ``AdapterDescriptor.to_dict``) must validate
  against that SDK schema, binding the version *and* the whole neutral shape.
* Core's search-outcome / credential-reference versions must equal the ``const``
  in the SDK's matching ``adapter/`` schemas.

The SDK is read from the installed ``skillweave_sdk`` package (the authority a
consumer actually imports) and never from a Core-private copy. When the SDK is
not importable the suite skips with a named reason, so a hermetic Core-only
clone behaves identically. No live provider is called.
"""

from __future__ import annotations

import pytest

from skillweave.adapters.descriptor import (
    ADAPTER_DESCRIPTOR_SCHEMA_VERSION,
    AdapterDescriptor,
    OperationBinding,
)
from skillweave.adapters.outcomes import (
    CREDENTIAL_REFERENCE_SCHEMA_VERSION,
    SEARCH_OUTCOME_CONTRACT,
    SEARCH_OUTCOME_SCHEMA_VERSION,
)

sdk_validator = pytest.importorskip(
    "skillweave_sdk.validator",
    reason="installed skillweave-sdk is the contract authority; anchor=validator",
)

_DESCRIPTOR_ID = "https://skillweave.dev/schemas/adapter/adapter-descriptor/v1"
_OUTCOME_ID = "https://skillweave.dev/schemas/adapter/search-outcome/v1"
_CREDENTIAL_ID = "https://skillweave.dev/schemas/adapter/credential-reference/v1"


def _version_const(schema_id: str) -> str:
    """The frozen ``const`` of a schema's ``schema_version`` property."""
    registry = sdk_validator.load_registry()
    assert schema_id in registry, (
        f"the installed SDK does not expose {schema_id!r}; it is not the "
        "adapter-contract SDK this consumer pins"
    )
    schema = registry[schema_id]
    const = schema.get("properties", {}).get("schema_version", {}).get("const")
    assert isinstance(const, str) and const, (
        f"{schema_id} does not freeze a string schema_version const"
    )
    return const


class TestCoreConsumesTheSdkOwnedVersion:
    def test_descriptor_version_is_the_sdk_schema_const(self):
        assert ADAPTER_DESCRIPTOR_SCHEMA_VERSION == _version_const(_DESCRIPTOR_ID), (
            "Core's ADAPTER_DESCRIPTOR_SCHEMA_VERSION drifted from the SDK-owned "
            "adapter-descriptor schema const; the SDK authors the contract version"
        )

    def test_search_outcome_version_is_the_sdk_schema_const(self):
        assert SEARCH_OUTCOME_SCHEMA_VERSION == _version_const(_OUTCOME_ID)

    def test_credential_reference_version_is_the_sdk_schema_const(self):
        assert CREDENTIAL_REFERENCE_SCHEMA_VERSION == _version_const(_CREDENTIAL_ID)

    def test_core_named_search_contract_matches_the_sdk_schema_id(self):
        assert SEARCH_OUTCOME_CONTRACT in _OUTCOME_ID, (
            "Core's SEARCH_OUTCOME_CONTRACT must name the SDK-owned outcome schema"
        )


class TestCoreEmittedDescriptorsValidateAgainstTheSdkSchema:
    def _descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            id="contract-authority-probe",
            version="1.0.0",
            categories=("search",),
            operations=(
                OperationBinding(
                    name="search",
                    input_contract="search-request/v1",
                    result_contract=SEARCH_OUTCOME_CONTRACT,
                ),
            ),
            transport="in-process",
            capabilities=("egress",),
        )

    def test_emitted_descriptor_validates_with_no_errors(self):
        payload = self._descriptor().to_dict()
        errors = sdk_validator.validate(payload, _DESCRIPTOR_ID)
        assert errors == [], (
            "a Core-emitted descriptor failed the SDK-owned schema; Core no "
            f"longer composes the neutral contract: {errors}"
        )

    def test_shipping_only_the_core_version_would_fail_the_sdk_schema(self):
        # A drift guard on the guard: if Core carried a *different* version, the
        # SDK schema would reject it. Proves the validation above is load-bearing
        # rather than vacuously true.
        payload = self._descriptor().to_dict()
        payload["schema_version"] = "9.9.9"
        errors = sdk_validator.validate(payload, _DESCRIPTOR_ID)
        assert errors, "the SDK schema accepted a foreign descriptor version"
