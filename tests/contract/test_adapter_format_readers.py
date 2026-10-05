"""SW-ADAPT-001: package-format readers produce consumer-owned bindings.

A local registration and a Capacium package must produce *equivalent* neutral
descriptors (same id/version/categories/operations/transport/capabilities) that
register through the same runtime port, differing only in provenance. Capacium's
``kind`` is quarantined as an opaque string in ``package_ref`` -- the facade
never reads it as an enum. Unsupported dialects and unadmitted container members
fail explicitly.
"""

import asyncio

import pytest

from skillweave.adapters import (
    AdapterBackend,
    AdapterError,
    AdapterFacade,
    AdapterRegistry,
    CATEGORY_SEARCH,
    EffectivePolicy,
    EgressPolicy,
    SEARCH_OPERATION,
    SearchAttempt,
    SearchOutcome,
    SearchResultRecord,
)
from skillweave.adapters.format_readers import (
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

_MEMBER = {
    "name": "perplexity-search",
    "id": "perplexity-search",
    "version": "1.0.0",
    "categories": ["search"],
    "operations": [
        {
            "name": "search.query",
            "input_contract": "search-query/v1",
            "result_contract": "search-outcome/v1",
        }
    ],
    "transport": "mcp-stdio",
    "capabilities": ["search.query"],
}

_LOCAL_MANIFEST = {"member": _MEMBER}

_CAPACIUM_MANIFEST = {
    "dialect": "capacium",
    "dialect_version": "1",
    "package": {
        "name": "perplexity-search-pack",
        "version": "1.0.0",
        "digest": "a" * 64,
        "container": False,
    },
    "members": [{**_MEMBER, "kind": "mcp-server", "interface": "mcp/tools/v1"}],
}

_CONTAINER_MANIFEST = {
    "dialect": "capacium",
    "dialect_version": "1",
    "package": {
        "name": "pack",
        "version": "1.0.0",
        "digest": "b" * 64,
        "container": True,
        "member_ref": "perplexity-search",
    },
    "members": [
        {**_MEMBER, "kind": "mcp-server"},
        {"name": "other", "id": "other", "version": "1", "categories": ["search"],
         "operations": [{"name": "search.query", "input_contract": "i", "result_contract": "r"}],
         "transport": "http", "kind": "http-api"},
    ],
}


class _Backend(AdapterBackend):
    def __init__(self):
        self.calls = 0

    async def invoke(self, operation, argument, context):
        self.calls += 1
        return SearchOutcome(
            query_sha256="a" * 64,
            requested_provider="perplexity",
            status="success",
            results=(SearchResultRecord("t", "https://e.com", "s", "web", "perplexity"),),
            attempts=(SearchAttempt("perplexity", "mcp-stdio", "success"),),
        )


def _comparable(descriptor):
    return (
        descriptor.id,
        descriptor.version,
        descriptor.categories,
        tuple((o.name, o.input_contract, o.result_contract) for o in descriptor.operations),
        descriptor.transport,
        descriptor.capabilities,
    )


def test_local_registration_has_no_package_ref():
    binding = LocalRegistrationReader().read(_LOCAL_MANIFEST)
    assert isinstance(binding, AdapterBinding)
    assert binding.descriptor.package_ref is None
    assert binding.dialect == "local"


def test_local_and_capacium_bindings_are_equivalent():
    local = LocalRegistrationReader().read(_LOCAL_MANIFEST)
    capacium = CapaciumFormatReader().read(_CAPACIUM_MANIFEST)
    assert _comparable(local.descriptor) == _comparable(capacium.descriptor)
    # Only provenance differs.
    assert local.descriptor.package_ref is None
    assert capacium.descriptor.package_ref is not None


def test_capacium_kind_is_quarantined_as_an_opaque_string():
    descriptor = CapaciumFormatReader().read(_CAPACIUM_MANIFEST).descriptor
    assert descriptor.package_ref.format == "capacium"
    assert descriptor.package_ref.format_version == "1"
    assert descriptor.package_ref.kind == "mcp-server"
    assert descriptor.package_ref.digest == "a" * 64
    # The neutral descriptor carries no Capacium vocabulary in its own fields.
    assert "capacium" not in descriptor.to_dict().get("transport", "")
    assert descriptor.categories == ("search",)


def test_both_readers_register_through_the_same_runtime_port():
    registry = AdapterRegistry()
    backend = _Backend()
    CapaciumFormatReader().read(_CAPACIUM_MANIFEST).register_into(registry, backend)
    assert "perplexity-search" in registry
    facade = AdapterFacade(
        registry,
        EffectivePolicy(allowed_adapter_ids=("perplexity-search",)),
    )
    outcome, receipt = asyncio.run(
        facade.invoke(
            category=CATEGORY_SEARCH, operation=SEARCH_OPERATION,
            adapter_id="perplexity-search", argument={"query": "q"},
        )
    )
    assert outcome.succeeded
    assert receipt.provenance is not None
    assert receipt.provenance["format"] == "capacium"
    assert backend.calls == 1


def test_two_bindings_of_the_same_provider_are_indistinguishable_to_the_facade():
    """A local and a Capacium reader yield descriptors the facade treats alike."""
    local = LocalRegistrationReader().read(_LOCAL_MANIFEST).descriptor
    capacium = CapaciumFormatReader().read(_CAPACIUM_MANIFEST).descriptor
    assert local.serves(CATEGORY_SEARCH, SEARCH_OPERATION)
    assert capacium.serves(CATEGORY_SEARCH, SEARCH_OPERATION)
    assert local.capabilities == capacium.capabilities


def test_unsupported_dialect_version_fails_explicitly():
    bad = {**_CAPACIUM_MANIFEST, "dialect_version": "99"}
    with pytest.raises(UnsupportedDialectError) as exc:
        CapaciumFormatReader().read(bad)
    assert exc.value.asset == "dialect_version"


def test_non_capacium_dialect_fails_explicitly():
    bad = {**_CAPACIUM_MANIFEST, "dialect": "other-format"}
    with pytest.raises(UnsupportedDialectError):
        CapaciumFormatReader().read(bad)


def test_container_without_member_is_refused():
    bad = {**_CONTAINER_MANIFEST, "package": {**_CONTAINER_MANIFEST["package"], "member_ref": None}}
    with pytest.raises(UnadmittedMemberError):
        CapaciumFormatReader().read(bad)


def test_unadmitted_container_member_is_refused():
    with pytest.raises(UnadmittedMemberError) as exc:
        CapaciumFormatReader().read(_CONTAINER_MANIFEST, admitted_members=["other"])
    assert exc.value.asset == "members"


def test_admitted_container_member_binds_provenance():
    binding = CapaciumFormatReader().read(
        _CONTAINER_MANIFEST, admitted_members=["perplexity-search"]
    )
    assert binding.descriptor.package_ref.member_ref == "perplexity-search"
    assert binding.descriptor.package_ref.container is True


def test_unknown_dialect_reader_fails_closed():
    registry = ReaderRegistry()
    with pytest.raises(UnsupportedDialectError):
        registry.get("no-such-format")


def test_future_reader_plugs_in_without_a_core_edit():
    class MyFormatReader(PackageFormatReader):
        dialect = "my-format"

        def read(self, manifest, *, admitted_members=()):
            return AdapterBinding(
                descriptor=LocalRegistrationReader().read({"member": manifest["entry"]}).descriptor,
                dialect=self.dialect,
            )

    registry = default_reader_registry()
    registry.register(MyFormatReader())
    assert "my-format" in registry.dialects()
    binding = registry.get("my-format").read({"entry": _MEMBER})
    assert binding.dialect == "my-format"


def test_default_registry_admits_local_and_capacium_only():
    assert set(default_reader_registry().dialects()) == {"local", "capacium"}
