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
| Egress to `api.perplexity.ai:443` | **unreachable** (`socket.gaierror`, DNS does not resolve) |
| Distribution `capacium-mcp` installed | present, version `0.3.0` |
| Console script `/opt/homebrew/bin/capacium-mcp` | **present but unusable** — importing `capacium_mcp.server` raises `ModuleNotFoundError` |

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
