# Criterion 5 — DuckDuckGo Lite live canary: PENDING (external proof)

Status: **pending / blocked at the environment boundary. Not a PASS.**

SW-COUNCIL-004 criterion 5 asks for an *owned zero-key live canary* on the
default supported path that returns a real source when reachable. That is an
**external proof**: it can only be produced where network egress to
`lite.duckduckgo.com:443` exists. In this environment it does not, so the
honest outcome is pending — never a fixture result presented as a live one.

## Observed environment facts

| Fact | Observation |
|------|-------------|
| Egress to `lite.duckduckgo.com:443` | **unreachable** (`socket.gaierror` / connection timeout) |
| DuckDuckGo credential required | **none** (zero-key provider) |
| Configurable API key | **not required** — DuckDuckGo Lite is free, no key |

## What the suites *do* prove (in the meantime)

- the DuckDuckGo Lite HTML parser correctly parses all DOM variants, paired
  titles/URLs/snippets, and excludes unsafe URL schemes
  (`test_council_004_duckduckgo_lite.py::TestCriterion1_LiteParserVariants`);
- empty pages, HTML challenges, HTTP 429/5xx, timeout and unsafe URL schemes
  produce distinct empty/blocked/error outcomes with zero retrieved evidence
  records (`TestCriterion2_ErrorStates`);
- fallback selection uses explicit allowlist/configuration
  (`TestCriterion3_ConfiguredFallback`);
- formatted legacy context contains real sources only under result numbering
  (`TestCriterion4_FormattedContext`);
- the live canary is truthfully skipped when the endpoint is unreachable, and
  the reachability fact is recorded
  (`TestCriterion5_LiveCanary`).

## What remains, to close criterion 5

1. run in an environment with outbound network egress to
   `lite.duckduckgo.com:443`;
2. run
   `tests/contract/test_council_004_duckduckgo_lite.py::TestCriterion5_LiveCanary::test_live_duckduckgo_returns_a_real_source`
   where egress is present; it asserts real `http(s)` sources on success and
   records an explicit unavailable/quota status otherwise.

Until then this criterion is **pending**, and no fixture result is promoted in
its place.
