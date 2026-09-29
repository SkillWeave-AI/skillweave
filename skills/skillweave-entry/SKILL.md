---
facade: true
experimental: true
name: skillweave-entry
description: "Entry-point facade for SkillWeave — start, continue, inspect, or onboard."
argument-hint: command="[start|continue|inspect|onboard]" run_id="[id]"
---

# /skillweave-entry

> Canonical metadata is English. User-facing artifacts follow the output language setting.

**Entry-point facade for the SkillWeave runtime.**  
This skill resolves entry intents through `EntryService.dispatch()` — the single
stateless seam that reads adapter-observed facts and returns a typed
`Decision` with disposition, digests, and operator-facing guidance.

Lifecycle decisions (install, upgrade, remove) are delegated to `EntryService`;
this skill does not reimplement them in prose.

## Interactive Entry

When invoked without a `command` argument, present at most three primary options:

| # | Option | Intent |
|---|--------|--------|
| 1 | **Start a new run** | `StartIntent` |
| 2 | **Continue an existing run** | `ContinueIntent` |
| 3 | **Inspect workspace state** | `InspectIntent` |

Onboarding (`OnboardIntent`) is available as a secondary option when no profile
has been completed.

## Usage

```
skillweave command="start"    run_id="my-run"              # Start a new run
skillweave command="continue" run_id="my-run"              # Continue an existing run
skillweave command="inspect"  scope="workspace"            # Inspect current state
skillweave command="onboard"  profile="operator"           # Run operator onboarding
```

## Harness Adapters

Two adapter implementations produce the same semantic result from identical
facts:

- `MappingEntryAdapter` — reads from a `Mapping` (dict, decoded payload)
- `ObjectEntryAdapter` — reads from an object's attributes (run record, model)

Use whichever matches the caller's data shape. Both canonicalise through
`EntryState` and produce identical digests for identical facts.

## Entry Contract

All lifecycle decisions flow through `EntryService.dispatch(intent, adapter)`:

1. Construct a typed intent (`StartIntent`, `ContinueIntent`, `InspectIntent`,
   `OnboardIntent`).
2. Wrap your data source in an `EntryAdapter`.
3. Call `dispatch()` — the returned `Decision` names the disposition:
   - **EXECUTE** — proceed with the requested action
   - **RENDER** — display state without action (inspect only)
   - **GUIDANCE** — state has minor contradictions; follow the guidance text
   - **ESCALATE** — state is incoherent; escalate to the run owner

## Testing

Verify the adapter equivalence:

```bash
python -m pytest tests/integration/test_skillweave_entry_adapters.py -v --tb=short
```
