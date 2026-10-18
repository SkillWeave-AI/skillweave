# Criterion 6 — live Perplexity availability: PENDING (external proof)

Status: **pending / blocked at the environment boundary. Not a PASS.**

SW-COUNCIL-003 criterion 6 asks for an *owned live* Perplexity search that
returns real cited HTTP(S) sources with the configured credentials. That is an
**external proof**: it can only be produced where a Perplexity credential and
outbound network egress both exist. In this acceptance state neither is present,
so the honest outcome is pending — never a fixture success presented as a live
one.

## Observed environment facts (recorded, not assumed)

| Fact | Observation |
|------|-------------|
| Perplexity credential (`PERPLEXITY_API_KEY` / `PPLX_API_KEY` / `PERPLEXITY_TOKEN`) | **absent** |
| Egress to `api.perplexity.ai:443` | reachable at capture time; irrelevant while no credential is present |
| Distribution `capacium-mcp` installed | **absent** — `importlib.metadata.version("capacium-mcp")` raises `PackageNotFoundError` |
| Console script `/opt/homebrew/bin/capacium-mcp` | present on `PATH`, but its distribution metadata is absent **and** `import capacium_mcp.server` raises `ModuleNotFoundError` (unusable) |

The two facts above are why CORE-ZR-024 **drops the default launcher
registration**: an installed console script can exist while its distribution is
absent and its module unimportable, so a default launcher would resolve to a
command that cannot serve `perplexity_search`. The resolver now ships **no**
default registration and refuses before any query with a typed `tool_missing`
(see `launcher.py` / `perplexity.py`). The live proof below is still gated on a
credential that does not exist here.

## What the suites *do* prove (in the meantime)

- the MCP lifecycle, result normalization, launcher precedence and owned-child
  teardown are pinned by contract tests against network-free fixtures
  (`test_council_mcp_lifecycle.py`, `test_council_perplexity_normalization.py`,
  `test_council_launcher_resolution.py`, `test_council_mcp_teardown.py`);
- the Council caller and the Discovery invocation are proven to consume *real*
  source results and *explicit* failure states, including on the **installed**
  artifact (`test_council_search_consumer.py`);
- with no usable binding or credential, the Perplexity path fails **closed** with
  a typed non-success and zero results — it never fabricates a source.

## What remains, to close criterion 6

1. supply an owner-authorized Perplexity credential in the owned child's
   environment allowlist (`SearchConfig.mcp_env_allowlist`);
2. provide an admissible, verified Perplexity MCP launcher (or an explicit
   `SearchConfig.mcp_command`) for an environment with egress to the provider;
3. run `tests/contract/test_council_live_availability.py::TestLivePerplexityProof`
   where both are present; it asserts real `http(s)` cited sources on success and
   records an explicit unavailable/quota status otherwise.

Until then this criterion is **pending**, and no fixture result is promoted in
its place.
