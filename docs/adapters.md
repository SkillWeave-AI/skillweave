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

## The Council-owned Perplexity MCP repair (SW-COUNCIL-003)

The addendum's Perplexity MCP defect lived in the *consumer*, so Council repairs
it **inside Council's own write scope** (`src/skillweave/council/`) rather than
rewriting the SW-ADAPT-001 substrate. Council's `search_registry()` rebuilds the
one registry with the Perplexity `mcp-stdio` binding's backend swapped for
Council's `PerplexityMcpBackend` — *same adapter id, same descriptor axes*, so
provider resolution, the Discovery provider map and the facade are unaffected,
and the `skillweave.adapters` package is untouched. Discovery still resolves its
built-in Perplexity binding from the substrate.

Council-owned modules:

* `skillweave/council/mcp_client.py` — a protocol-conformant stdio client. It
  performs the full lifecycle, **negotiates and records** the protocol version,
  capabilities and `serverInfo`, and **correlates request ids** across
  interleaved notifications/stale responses. It owns its child
  (`start_new_session=True`), so a timeout/cancellation tears down only that
  process group; stderr is drained and any secret sentinel is **redacted**
  before it is surfaced.
* `skillweave/council/perplexity.py` — normalization + the typed driver.
  `normalize_records` reads every real Perplexity result shape
  (`structuredContent.results`/`citations`, top-level `citations`/`results`, or
  a bare URL) and **drops every non-`http(s)` value**; `content` `text` blocks
  are synthesis, *never* a source. A JSON-RPC error, closed stream, `isError`
  result, missing tool, malformed payload, or a synthesis with no cited URL each
  become a **typed non-success with zero results**.
* `skillweave/council/launcher.py` — the launcher resolver, in a fixed
  precedence: explicit `SearchConfig.mcp_command` → registered compatible
  runtime → verified installed binary → uvx fallback (only with an **exact**
  `name==version` pin, an **explicit** network grant and recorded provenance).
  A floating `git+`/URL command or an unpinned `uvx` spec is refused before any
  process is spawned.

**Criterion 2 at the consumer boundary.** The resolver is not the only place a
refused launcher could slip through. `WebSearch._backend_argument` populates
`command` **only** from an accepted resolution — a refused explicit command
leaves no `command` behind — and `PerplexityMcpBackend.invoke` /
`run_perplexity_mcp` re-check a *raw* command, so a floating `git+`/URL install
or an unpinned `uvx` spec becomes a typed `no_binding` that spawns **nothing**,
whatever path supplied it. The check skips a *resolver-produced* command (one
carrying a `launcher_kind`), because its resolved `uvx --from name==version name`
shape is already validated and must not be misread as unpinned.

**Cross-owner hold (Discovered, not fixed here).** Discovery's
`resolve_search_outcome` **defaults** to the SW-ADAPT-001 substrate registry,
whose Perplexity MCP binding (`skillweave.adapters.search_backends.
_fetch_perplexity_mcp`, plus `skillweave.adapters.mcp_client`) lives outside
Council's write scope. On that default path a floating `git+` command is refused
before spawn (as a raised client error rather than Council's typed `no_binding`),
but an **unpinned `uvx` spec is NOT refused** — the substrate spawns it. Closing
that requires SW-ADAPT-001 scope; the criterion-2 regressions drive the function
with Council's owned registry and record the substrate default's behaviour
explicitly (see `tests/contract/test_council_launcher_refusal_consumers.py`).

The Council caller (`WebSearch.search`) stays a **formatted-string** API; the
Discovery research invocation is proven to consume Council's typed outcome (a
real cited source on success, an explicit failure state otherwise). Both are
proven on the **installed** artifact.

**Criterion 6 caveat (live availability).** An owned *live* Perplexity search
that returns real cited sources requires a configured credential and network
egress. Where either is absent the external proof stays **pending/blocked** — no
fixture result is promoted in its place (see
`tests/fixtures/council/LIVE_PROOF_PENDING.md`).

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

## The Discovery lifecycle consumer

The `skillweave-discovery` skill ships as a **prompt** pack: `skills/
skillweave-discovery/SKILL.md` is Markdown a host runs, and a prompt pack does
not, by itself, invoke a search provider. Treating "Discovery goes through
Council" as the lifecycle binding is therefore not admissible: Council's Stage 0
is a *Council* consumer, not a Discovery one, and no Discovery lifecycle
consumer existed in the sources.

The executable Discovery consumer is delivered by a **minimal, source-owned path
addition** (`src/skillweave/discovery/`), bound to the *same* extension registry
Council uses through the one mechanism that actually runs code at a phase
boundary -- the Studio hook engine:

* `skillweave.discovery.lifecycle_search` -- the seam. It derives its provider
  map from `SEARCH_PROVIDER_SPECS` (the registry stays the single source of
  truth), resolves -> admits -> invokes through `AdapterFacade`, and returns a
  **typed** `SearchOutcome` plus an `OperationReceipt`. An unknown provider
  raises; a `None`/in-process adapter is named by its own registered id.
* `skillweave.discovery.lifecycle_hook.DiscoveryResearchHook` -- a
  `type=python` `HookAdapter` bound at `discovery/pre` (the free-tier
  `pre_discovery` injection point). It reads `ctx.hook_config`, drives the seam,
  and returns a hook `pass`/`fail`. It sets **no** `gate_override`: a hook is not
  gate authority.
* `skillweave.discovery.binding` -- the source-owned binding descriptor a
  consumer materializes into `.skillweave/hooks/discovery-pre.yaml`, plus the
  reader (`binding_from_skill_manifest` / `materialize_skill_binding`) that
  turns the *skill's own declaration* into that descriptor. It embeds no
  credential value and denies egress by default.

The binding is data, not a script in a foreign home: it names the packaged class
`skillweave.discovery.lifecycle_hook.DiscoveryResearchHook`, so the engine
imports it straight from the installed wheel.

### The exact criterion-driven path addition

The prompt skill itself now **declares** its lifecycle consumer. Its manifest
`skills/skillweave-discovery/capability.yaml` carries a single data field:

```yaml
studio_binding:
  phase: discovery
  position: pre
  module: skillweave.discovery.lifecycle_hook.DiscoveryResearchHook
  name: skillweave-discovery-research
  config:
    provider: duckduckgo
    allow_network: false
```

This is the whole addition, and it is deliberately a *declaration*, not a
mechanism:

* it binds the Markdown facade's **Phase 2 (Research)** boundary to the packaged
  hook — so the skill's declared boundary is what the consumer materializes, not
  a binding invented outside the skill;
* it is **data**, never a host command: the value names the source-owned class,
  which the engine imports from the installed wheel. No executable is embedded,
  so the shipped `SKILL.md` stays host-neutral;
* the reader is conservative and **fails closed** — a block naming a different
  phase/position/module, an unknown config key, or a smuggled credential value
  is refused (`AdapterError` with an attributable `asset`), and the produced
  descriptor is re-validated through the engine's own `BindingConfig` schema
  before it is written;
* it **preserves the declared boundaries**: it creates no `WorkContract`, holds
  no gate authority, replaces no SDK contract, and (like the bare descriptor)
  denies egress and embeds no credential value. The Capacium `kind`/delivery
  vocabulary never appears here — the block is runtime category + transport
  binding only, independent of any package format.

The declaration round-trips through `CapaciumManifestSync` (unknown top-level
keys are preserved), so it does not read as a manifest drift, and it needs no
native CLI/process/store change: `materialize_skill_binding(skill_manifest,
project_root)` writes `.skillweave/hooks/discovery-pre.yaml`, the exact path the
Studio loader already reads.

Everything fails closed -- each is an asserted counterexample, not a claim:

* a provider **outage** is a typed non-success, reported as a hook `fail`;
* an **unknown** provider raises and is reported (no DuckDuckGo fallback);
* a **malformed** outcome (a backend returning the wrong type) is refused;
* an **unapproved** adapter, a **denied egress** (the default), and a
  **missing credential** are three distinct pre-invocation refusals.

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
| Discovery lifecycle consumer (real `run_hooks` path; outage/unknown/malformed) | `tests/contract/test_discovery_lifecycle_consumer.py` |
| Markdown Discovery skill **declares** its consumer; declaration drives the engine + is closed data | `tests/contract/test_discovery_skill_binding.py` |
| Discovery consumer on the **installed** artifact (incl. the skill declaration) | `tests/contract/test_discovery_installed_consumer.py` |
| Router/harness decision & admission semantics preserved | `tests/contract/test_adapter_router_harness_semantics.py` |
| Core descriptor/outcome versions pinned to the SDK-owned schema `const` | `tests/contract/test_adapter_schema_authority.py` |
| SDK adapter schema contract set | `skillweave-sdk/tests/contract/test_adapter_contract_set.py` |

## Not in scope (follow-ups)

The following remain **follow-ups** and are explicitly *not* delivered here:

* full vendor pack extraction;
* advanced Studio graph editing;
* adaptive cross-harness control.
