# Adapters, the extension registry, and the search/tool facade

SkillWeave 1.5.10 — SW-ADAPT-001.

This document is a pointer, not evidence: every claim here is backed by a
machine check under `tests/contract/` (see the *Conformance* section for the
exact suite). The adapter facade is **additive**: it creates no `WorkContract`,
holds no gate authority, and leaves the existing `search-provider/v1` /
`model-provider/v1` contracts and the lifecycle `ProviderRegistry` intact.

## The one idea: a neutral descriptor behind one extension point

An **adapter** is a neutral descriptor plus a backend callable. The descriptor
names the runtime category it registers under, the operations it serves, the
transport it is invoked through, the capabilities it holds, and the credential
references it needs. Nothing in the facade branches on an adapter id, a
transport name or a package kind: it reads the descriptor as *data*, applies
the effective policy, invokes the backend, and returns a typed outcome plus an
`OperationReceipt`.

Everything is wired through a single extension point,
`skillweave.adapters.AdapterRegistry`:

```python
from skillweave.adapters import (
    AdapterRegistry, AdapterFacade, EffectivePolicy, EgressPolicy,
    SEARCH_OPERATION,
)

registry = AdapterRegistry()
registry.register(my_descriptor, MyBackend(), source="my-package")   # no core edit
facade = AdapterFacade(
    registry,
    EffectivePolicy(
        allowed_adapter_ids=("my-adapter",),
        egress=EgressPolicy(allow_network=True, endpoint_allowlist=("api.example.com",)),
    ),
)
outcome, receipt = await facade.invoke(
    category="search",
    operation=SEARCH_OPERATION,
    argument={"query": "..."},
    adapter_id="my-adapter",
    endpoint="https://api.example.com/v1/search",
)
```

A caller can also resolve by `category + operation + capabilities` without
naming an id, **provided exactly one** registered adapter matches; zero matches
is an unknown adapter and more than one is ambiguous — both fail closed. There
is **no implicit fallback**: a generic adapter is never used to satisfy a search
operation (or vice versa).

## The four independent axes

Four facts about an adapter are independent and must never be collapsed into a
single "kind" taxonomy:

| Axis | Where it lives | Closed? |
|------|----------------|---------|
| **runtime category** (`search`, `generic`) | `AdapterDescriptor.categories` | **Closed** — an unknown category fails closed |
| **invocation transport** (`http`, `mcp-stdio`, `in-process`, …) | `AdapterDescriptor.transport` | Open string — a new transport needs no contract change |
| **package format / kind** (`capacium` kind `mcp-server`, or any format's opaque kind) | `PackageRef.kind`, produced by a *format reader* | Open opaque string — the facade **never** branches on it |
| **distribution / provisioning** (how the bytes arrive) | Not in the descriptor at all | Reading a descriptor proves format conformance, **never** callability |

The last row is the important one: reading a manifest or a descriptor is not a
promise that the adapter can be called or that it was distributed. That is a
separate, explicitly-gated step.

## Swapping a search provider

The six built-in providers (DuckDuckGo, Serper, SerpApi, Tavily, Brave and
Perplexity) are each registered through the **same** `build_search_registry()`
extension point. Swapping a provider is a data change, not a code branch:

```python
from skillweave.council.search import SearchConfig, WebSearch

# Zero-key default: DuckDuckGo needs no credential at all.
ws = WebSearch()
ctx = await ws.search("latest on adaptive harnesses")           # SearchConfig()

# A keyed provider is selected by name; the key comes from the environment.
ctx = await ws.search("...", SearchConfig(provider="brave"))
```

`WebSearch.search()` remains a **compatible formatted-string wrapper** — Council
receives exactly the context string it always did. Callers that want the typed
result use `WebSearch.search_outcome()`, which returns a `SearchOutcome`.

An unrecognised provider name **fails closed** (raises) rather than being
quietly served by DuckDuckGo.

## Perplexity: two transports, one provider

Perplexity is registered **twice as alternative transport bindings**:

* `perplexity-search-mcp` — transport `mcp-stdio`, invoked over a compliant,
  handshake-first stdio client;
* `perplexity-search-http` — transport `http`, the direct API binding.

The two share the Perplexity identity but are distinct adapters with distinct
transports. An explicit MCP launch command in `SearchConfig.mcp_command`
selects the MCP transport; otherwise the HTTP binding is used.

The MCP client performs the required lifecycle —
`initialize → notifications/initialized → tools/list → tools/call` — because a
compliant server answers a bare first `tools/call` with `-32002`
(server not initialized). A launch that would install floating code (`git+`,
`http://`, `https://`) is refused before any process is spawned.

**Availability caveat.** The MCP lifecycle fixtures prove *contract
conformance against a fake server*. They do **not** establish live Perplexity
availability, and no live provider is ever called by the tests.

## Adding a local tool (zero format)

A local tool that speaks no package format is registered directly; its
`package_ref` stays `None`:

```python
from skillweave.adapters import (
    AdapterBackend, AdapterDescriptor, CATEGORY_GENERIC,
    OperationBinding, ToolOutcome,
)

class EchoBackend(AdapterBackend):
    async def invoke(self, operation, argument, context):
        return ToolOutcome(status="success", tool=operation, payload={"echo": argument.get("value")})

descriptor = AdapterDescriptor(
    id="my-echo-tool", version="0.1.0", categories=(CATEGORY_GENERIC,),
    operations=(OperationBinding(name="tool.echo",
                                 input_contract="tool/echo-request/v1",
                                 result_contract="tool/echo-result/v1"),),
    transport="in-process",
)
registry.register(descriptor, EchoBackend(), source="my-package")
```

## Connector-pack / package-format readers (Capacium, optional)

A **package-format reader** turns a package manifest into the *same* neutral
`AdapterDescriptor` the facade already understands. Two readers ship, and both
register through the **same** runtime port:

* `LocalRegistrationReader` — the zero-format path above (`dialect="local"`);
* `CapaciumFormatReader` — the optional, **versioned** Capacium package-format
  reader (`dialect="capacium"`, `dialect_version="1"`).

The Capacium reader reuses the package's identity/entrypoint/interface metadata
and quarantines the format's `kind` inside `package_ref` as an opaque string.
Capacium's CLI or service is **never** invoked; the manifest is read from bytes.

```python
from skillweave.adapters.format_readers import CapaciumFormatReader

reader = CapaciumFormatReader()
binding = reader.read(manifest, admitted_members=("search-server",))
binding.register_into(registry, backend, source="capacium:pkgname")

# Extension seam: a new format registers its own reader here.
from skillweave.adapters.format_readers import ReaderRegistry
readers = ReaderRegistry(); readers.register(MyFormatReader())
```

Unsupported dialects, unsupported dialect versions, a container with no
`member_ref`, and an unadmitted member all **fail explicitly** — never a
best-effort parse.

## Package-author vs consumer responsibilities

| Responsibility | Package author | Consumer |
|----------------|----------------|----------|
| Declare identity, entrypoint, interface metadata | ✅ | |
| Pin the format dialect + version | ✅ | |
| Produce the neutral descriptor via a reader | | ✅ (with the author's manifest) |
| Choose the admitted members | | ✅ |
| Set the allowlist / egress / credential policy | | ✅ |
| Decide whether to install or load anything | | ✅ (explicitly, and only after policy admission) |

Reading a manifest proves **format conformance only**. Callability, distribution
and credential availability are separate, explicitly-gated facts.

## Admission: fail-closed before any call

`admit_invocation` runs a fixed, ordered set of checks so the *first blocking
fact* is named, and each refusal is attributable via `asset`:

1. the adapter id is on the **explicit allowlist** (empty allowlist approves
   nothing);
2. the adapter **serves** the requested category + operation;
3. every required **capability** is declared by the adapter;
4. every required **credential reference** is observed `available` (a missing
   credential is refused, never simulated), and the value is injected only into
   an owned child environment;
5. an out-of-process transport targets an **allowlisted endpoint** under the
   egress policy (default: deny).

A missing capability, an unapproved adapter, a missing credential and a denied
egress are four *distinct* refusals, not one generic NO. Discovery and import
perform **no** network install, **no** provider call and **no** arbitrary
executable loading.

## Receipts bind, and carry no authority

Every invocation returns an `OperationReceipt` binding the invocation id, the
adapter id/version and descriptor digest, the operation, the domain contract,
the transport, the provenance, the terminal status and an evidence digest. A
search that reports `success` must carry at least one **real http(s) source
URL**; an outage, an error, a cancellation or an unavailable status is reported
as such and never dressed up as a retrieved source. A receipt is a *record of an
execution*: `self_approving` is always `False` and `is_authority` is always
`False`. Receipts cannot self-approve.

## Conformance

| Criterion | Suite |
|-----------|-------|
| Neutral descriptor + registry + facade + policy | `tests/contract/test_adapter_facade.py` |
| Capacium reader equivalence, same runtime port | `tests/contract/test_adapter_format_readers.py` |
| MCP lifecycle; old bare-`tools/call` defect is load-bearing | `tests/contract/test_adapter_mcp_lifecycle.py` |
| Installed artifact: external adapters register with no vendor edit | `tests/contract/test_installed_adapter_extension.py` |
| Council is a real registry consumer | `tests/contract/test_adapter_council_search.py` |
| Router/harness decision & admission semantics preserved | `tests/contract/test_adapter_router_harness_semantics.py` |
| SDK adapter schema contract set | `skillweave-sdk/tests/contract/test_adapter_contract_set.py` |

## Not in scope (follow-ups)

The following remain **follow-ups** and are explicitly *not* delivered here:

* full vendor pack extraction;
* advanced Studio graph editing;
* adaptive cross-harness control.
