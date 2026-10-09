"""The Discovery lifecycle search seam (SW-ADAPT-001, AC4/AC5/AC8).

This is the executable boundary the Discovery phase was missing. It resolves a
search provider **through the one shared extension registry** -- the same
:class:`~skillweave.adapters.AdapterRegistry` Council's ``WebSearch`` uses -- and
returns a **typed** :class:`~skillweave.adapters.SearchOutcome` and an
:class:`~skillweave.adapters.OperationReceipt`. It is not a second search stack:
it derives its provider->adapter map from :data:`~skillweave.adapters.
SEARCH_PROVIDER_SPECS`, so the registry stays the single source of truth.

Everything fails closed:

* an **unknown provider** raises :class:`UnknownDiscoveryProvider`; it is never
  quietly served by DuckDuckGo;
* a provider whose credential is missing is admitted with ``availability``
  reported as ``missing`` (the facade refuses it *before* the call), or -- for a
  built-in keyed provider, whose credential is the caller's ``api_key`` -- the
  backend returns a typed ``blocked``/``no_credential`` outcome. Either way no
  live call is made;
* egress is denied unless the endpoint is explicitly allowlisted, so the default
  construction makes **no** network call at all.

The default provider is ``duckduckgo`` (zero key), but the *facade policy*
still denies egress by default: a caller that wants a live call must opt in with
``allow_network=True`` and the provider's endpoint. This function itself never
dialect-checks, never installs, and never calls a live provider on import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from skillweave.adapters import (
    SEARCH_OPERATION,
    SEARCH_PROVIDER_SPECS,
    AdapterBackend,
    AdapterDescriptor,
    AdapterFacade,
    AdapterRegistry,
    EffectivePolicy,
    EgressPolicy,
    OperationReceipt,
    SearchOutcome,
    build_search_registry,
)
from skillweave.adapters.search_backends import SearchProviderSpec

__all__ = [
    "DEFAULT_DISCOVERY_PROVIDER",
    "UnknownDiscoveryProvider",
    "PROVIDER_TO_ADAPTER",
    "ADAPTER_SPEC",
    "adapter_id_for",
    "endpoint_for",
    "registry_for",
    "facade_for",
    "resolve_search_outcome",
]

#: The zero-key provider Discovery uses when the binding names none.
DEFAULT_DISCOVERY_PROVIDER = "duckduckgo"

#: provider name -> the adapter id serving it, derived from the one registry.
PROVIDER_TO_ADAPTER: dict[str, str] = {}
#: adapter id -> its provider spec, for endpoint resolution.
ADAPTER_SPEC: dict[str, SearchProviderSpec] = {}
for _spec in SEARCH_PROVIDER_SPECS.values():
    PROVIDER_TO_ADAPTER.setdefault(_spec.provider, _spec.adapter_id)
    ADAPTER_SPEC[_spec.adapter_id] = _spec


class UnknownDiscoveryProvider(ValueError):
    """An unknown Discovery provider name; fails closed (no silent fallback)."""

    def __init__(self, provider: str) -> None:
        known = sorted(PROVIDER_TO_ADAPTER)
        super().__init__(
            f"unknown discovery search provider {provider!r}; "
            f"registered providers: {known}"
        )
        self.provider = provider
        self.known = tuple(known)


def adapter_id_for(
    provider: str,
    *,
    mcp_command: Optional[Sequence[str]] = None,
    adapter_id: Optional[str] = None,
) -> str:
    """Resolve a provider name to its registered adapter id, or fail closed.

    Perplexity is served by two transports; an explicit ``mcp_command`` selects
    the MCP-stdio binding, otherwise the HTTP binding is used.

    When ``adapter_id`` is given it wins: an external adapter the core has never
    heard of is named by its *registered adapter id* (registered at wiring time
    through ``extra_adapters``), so it is invoked through the same facade with no
    core edit. A named id is not validated here -- the registry is the authority
    and refuses an unregistered id at ``get()``.
    """
    if adapter_id is not None and str(adapter_id).strip():
        return str(adapter_id).strip()
    name = provider.strip().lower()
    if not name:
        raise UnknownDiscoveryProvider(provider)
    if name == "perplexity" and mcp_command:
        return "perplexity-search-mcp"
    resolved = PROVIDER_TO_ADAPTER.get(name)
    if resolved is None:
        raise UnknownDiscoveryProvider(name)
    return resolved


def endpoint_for(adapter_id: str) -> Optional[str]:
    """The egress endpoint a provider dials, or ``None`` for a local launch."""
    spec = ADAPTER_SPEC.get(adapter_id)
    if spec is None or spec.endpoint_host is None:
        return None
    return f"https://{spec.endpoint_host}"


def registry_for(
    extra_adapters: Optional[Sequence[tuple[AdapterDescriptor, AdapterBackend, str]]] = None,
    *,
    registry: Optional[AdapterRegistry] = None,
) -> AdapterRegistry:
    """The shared search registry, optionally with external adapters registered.

    ``extra_adapters`` is ``(descriptor, backend, source)`` triples registered at
    wiring time -- an external adapter the core has never heard of needs no core
    edit, exactly as the extension point promises.

    ``registry``, when given, is extended in place and returned rather than
    building a fresh one, so a caller's wiring-time registrations (an installed
    external adapter) are preserved.
    """
    target = registry if registry is not None else build_search_registry()
    if extra_adapters:
        for descriptor, backend, source in extra_adapters:
            if descriptor.id in target:
                continue  # already registered by this caller; idempotent wiring
            target.register(descriptor, backend, source=source)
    return target


def facade_for(
    adapter_id: str,
    *,
    registry: Optional[AdapterRegistry] = None,
    allow_network: bool = False,
    endpoint_allowlist: Optional[Sequence[str]] = None,
    credential_availability: Optional[Mapping[str, str]] = None,
    require_credential_availability: bool = True,
    extra_adapters: Optional[Sequence[tuple[AdapterDescriptor, AdapterBackend, str]]] = None,
) -> AdapterFacade:
    """Build a facade whose policy admits exactly ``adapter_id``.

    Egress is **denied by default**. When ``allow_network`` is set the endpoint
    allowlist must cover the provider's endpoint (defaulting to that endpoint),
    so an allowed call still cannot wander to an unlisted host. An external
    ``in-process`` adapter declares no endpoint and is not an egress transport,
    so it is admitted without any network grant.
    """
    reg = registry_for(extra_adapters, registry=registry)
    endpoint = endpoint_for(adapter_id)
    allow = tuple(endpoint_allowlist) if endpoint_allowlist is not None else (
        (endpoint,) if endpoint else ()
    )
    policy = EffectivePolicy(
        allowed_adapter_ids=(adapter_id,),
        egress=EgressPolicy(allow_network=allow_network, endpoint_allowlist=allow),
        require_credential_availability=require_credential_availability,
    )
    return AdapterFacade(reg, policy)


async def resolve_search_outcome(
    query: str,
    *,
    provider: str = DEFAULT_DISCOVERY_PROVIDER,
    adapter_id: Optional[str] = None,
    registry: Optional[AdapterRegistry] = None,
    allow_network: bool = False,
    endpoint_allowlist: Optional[Sequence[str]] = None,
    credential_availability: Optional[Mapping[str, str]] = None,
    api_key: Optional[str] = None,
    max_results: int = 10,
    mcp_command: Optional[Sequence[str]] = None,
    mcp_env_allowlist: Sequence[str] = (),
    extra_adapters: Optional[Sequence[tuple[AdapterDescriptor, AdapterBackend, str]]] = None,
) -> tuple[SearchOutcome, OperationReceipt]:
    """Resolve -> admit -> invoke Discovery's search; return the typed outcome.

    The returned :class:`SearchOutcome` is provider-neutral and carries at least
    one real http(s) source on success; a non-success carries no sources, so an
    outage is never dressed up as a retrieved source. The receipt binds the
    invocation to the adapter, its descriptor digest and the domain contract and
    carries **no** gate authority.

    ``adapter_id`` overrides ``provider``: an external adapter registered through
    ``extra_adapters``/``registry`` is named by its own id and invoked through the
    same facade.
    """
    resolved_id = adapter_id_for(provider, mcp_command=mcp_command, adapter_id=adapter_id)
    spec = ADAPTER_SPEC.get(resolved_id)

    # ── Derive credential_availability from the spec's credential_refs ─────
    # If the caller supplied an explicit credential_availability it wins;
    # otherwise we derive it from the spec and the api_key argument so that
    # a provider with a credential_ref but no api_key is reported as "missing"
    # before any live call.
    effective_credential_avail = credential_availability
    if effective_credential_avail is None and spec is not None and spec.credential_refs:
        effective_credential_avail = {
            ref: ("available" if api_key else "missing")
            for ref in spec.credential_refs
        }

    facade = facade_for(
        resolved_id,
        registry=registry,
        allow_network=allow_network,
        endpoint_allowlist=endpoint_allowlist,
        credential_availability=effective_credential_avail,
        extra_adapters=extra_adapters,
    )
    argument: dict[str, Any] = {
        "query": query,
        "max_results": max_results,
        "api_key": api_key,
    }
    if spec is not None and spec.transport == "mcp-stdio":
        argument["command"] = list(mcp_command) if mcp_command else None
        argument["env_allowlist"] = tuple(mcp_env_allowlist)
    return await facade.invoke(
        category="search",
        operation=SEARCH_OPERATION,
        argument=argument,
        adapter_id=resolved_id,
        credential_availability=effective_credential_avail,
        endpoint=endpoint_for(resolved_id),
    )
