# Version truth

This document is the release-facing **version-truth** surface for SkillWeave
(SW-DOC-004). It distinguishes four facts that are routinely collapsed into one
number, states the policy that keeps them honest, and names the machine check
that fails closed when a release-facing claim drifts from the tree or from an
installed artifact.

It is a pointer, not evidence: every current value below is *derived* from the
tree (or from an installed distribution) by `scripts/check-version-truth.py`,
and asserted by `tests/unit/test_doc_version_truth.py`. Nothing here is a second
inventory to maintain by hand.

## The four facts

| Fact | Where it lives | What it means |
|------|----------------|---------------|
| **candidate** | `[project].version` in `pyproject.toml` (`.version.yaml` `source_of_truth`) and the distribution-bundle `capability.yaml` | the version the checked-out tree would build; the single declarative authority |
| **released** | the latest canonical **annotated** product tag, identified by its **peeled commit** | what has actually shipped from the canonical Forgejo repository |
| **installed** | `importlib.metadata.version("skillweave")` in the running interpreter | the version of the distribution that is actually present locally (may be *absent*) |
| **advertised early** | a version named in docs/README before it ships | work in progress; must never be shown as *released* |

Current values are **derived, never written here**: the check prints the
candidate, the released tag and its peeled commit, the installed identity and
the discovered observation on every run, and the `--json` output carries the
same values. No literal version or commit hash for the *released* fact is
recorded in this document — a copy here would be a snapshot that silently rots,
and a hard-coded hash would contradict the policy that the released fact is
*resolved* from the tag. Read the checker's output, or the table it prints, for
the numbers that hold right now.

| Fact | Where it is derived from | What it means |
|------|--------------------------|---------------|
| candidate | `pyproject.toml` `[project].version`; bundle `capability.yaml` `version:` | the version the checked-out tree would build |
| released | `git tag --list 'v*'` → highest semver → `git rev-parse '<tag>^{}'` | what the canonical tag's peeled commit declares |
| installed | `importlib.metadata` in the running interpreter | what is present locally (may be **not installed**) |
| advertised early | the badge / "What's new" heading / CHANGELOG top | a candidate named before it ships; never shown as *released* |

## Released is a peeled commit, never "equals HEAD"

An annotated tag is an object; the commit it *points at* is a different SHA
obtained with `git rev-parse '<tag>^{}'`. A release check must therefore:

1. resolve the tag to its **peeled commit** (`^{}` / `^{commit}`), not the tag
   object and not the branch tip; and
2. bind the released version to **canonical release metadata** (the tag's own
   subject line) rather than to `branch HEAD`.

These are different statements on purpose. The latest release tag's peeled
commit is generally **not** the current branch tip, and need not be an ancestor
of it: later commits are legitimately composed on top of the released tag.
Requiring `tag == HEAD` would make every one of those later commits look like a
broken release. The peeled-tag policy says instead: *the released version is
whatever the canonical tag's subject declares, at the commit the tag peels to.*
The canonical tag subject is the release-metadata line (e.g. `SkillWeave
vX.Y.Z`); the peeled commit is where that release actually lives. Both are
*resolved at check time*, never copied into this document.

The current candidate, released tag and peeled commit are printed by
`scripts/check-version-truth.py` (and exposed in `--json`); this document
deliberately holds no literal of them.

## Discovered vs installed (registry / index)

The *discovered* version is what a registry or index reports as available; the
*installed* version is what is present locally. They are **computed and shown
separately** — by `scripts/check-version-truth.py`, in both its text and its
`--json` output — each with its provenance and freshness, because "the registry
says X" is not a statement about this machine. The two are never merged and
never compared as if equal; a difference between them is *shown*, not hidden
(and is not itself a release-claim drift).

**Installed identity** (provenance `importlib.metadata`): the running
interpreter's distribution is read for its version, distribution name, resolved
origin path and how it was installed (`site-packages`, `editable-checkout`
(`direct_url.json` `dir_info.editable`), `local-vcs`, or `unknown`). This is the
same identity `skillweave.cli.support.product_version()` consumes. When the
distribution is absent the check reports **not installed** — it never
substitutes the candidate or a registry value (no fabricated version).

**Discovered observation** (an explicit registry/index observation, supplied by
the operator):

- `--discovered-observation <file.json>` reads a small JSON object
  `{"source": ..., "index_url": ..., "name": "skillweave", "version": "X.Y.Z",
  "observed_at": "<ISO-8601>"}`. `index_url` and `observed_at` are optional; the
  rest are required.
- **This tool performs no live registry query.** It does not open a socket, does
  not contact an index and does not resolve `index_url`; it *reports the
  observation it was handed*. The observation's `source`, `index_url` and
  observation time are printed alongside the version so the reader can weigh it.
- Age is reported against a freshness threshold (`--freshness-hours`, default
  `48`). An observation older than the threshold is marked **STALE** — never
  silently trusted — and `age_hours > freshness_hours` is exposed in `--json`.
- When **no** observation is supplied the check prints
  `no discovered observation supplied` (and `--json` reports
  `"discovered": null`); it never invents a value.
- A malformed or unreadable observation file is a **setup error** (exit 2): a
  bad observation is never silently treated as "no observation".
- Neither the installed nor the discovered fact may be silently promoted to
  *released*. `released` is established only by the peeled canonical tag above.
- The run-snapshot contract this surface speaks is pinned by id
  `run-snapshot-v1` (`skillweave.cli.support.RUN_SNAPSHOT_SCHEMA_ID`); the SDK
  owns the schema bytes. The discovered-observation file is the operator-facing,
  tool-local carrier of that same "what a registry observed" idea; it is not the
  run snapshot itself and confers no authority.

### Forgejo-first release provenance

The release model is Forgejo-first (see `docs/release-flow.md`):
`local -> Forgejo (canonical) -> mirror -> GitHub (distribution only)`. The
canonical release object and the canonical tag are Forgejo-side; the GitHub side
is a distribution mirror only. The four release identities (Forgejo release
object, canonical tag, mirror tag, distribution receipt) are bound to **one
immutable commit** by `verify_release_identity` in
`tests/release/test_forgejo_first_release_contract.py`, which fails closed on
zero or duplicate Forgejo release objects.

## What the check enforces (and what "historical" allows)

`scripts/check-version-truth.py` compares:

1. **candidate** — `pyproject.toml` and the bundle `capability.yaml` must agree
   (the two `required: true` surfaces in `.version.yaml`); member pins and
   `skills/*/capability.yaml` are informational and may lag
   (`release_policy: decoupled_member_pins`).
2. **release-facing badge** — the README version badge must advertise the
   **released** fact, not an old value and not an unaccepted candidate.
3. **changelog** — the CHANGELOG's top heading must name an accepted version.
4. **installed identity** — when a distribution is importable, its metadata must
   match the candidate the tree builds; a mismatch is a named
   `installed-version-drift` failure. Absence is reported as `not installed`,
   not as a failure of the tree.
5. **skill inventory** — the README's skill count and the declared
   `skills/*/capability.yaml` inventory must equal the on-disk inventory.

In addition to those checks, the tool **reports** (it does not gate on) the
`installed` identity and the operator-supplied `discovered` observation as
separate lines, each with its provenance and freshness, and mirrors all of the
above in its `--json` output (`candidate`, `bundle`, `release_prep`,
`released`, `installed`, `discovered`, `skills`, `checks`, `ok`). Absence is
reported honestly: a missing install prints `not installed`, a missing
observation prints `no discovered observation supplied` — neither is a drift,
and neither is ever fabricated.

**Historical references are allowed** when they are explicitly historical. A
version token is *historical* when it is not presented as the current release —
for example the wording "1.4.0 scope" in README *Host-Neutral Scope*. The check
does not demand that an old version vanish; it demands that an old version is
not **advertised as current**.

## The release-prep exception

A release commit must carry the new CHANGELOG section, README badge and
"What's new in X" heading **before the release tag exists** — at that instant
the only version those positions can name is the still-*unreleased* candidate.
Under the plain rule that is exactly the drift the check exists to catch, so an
**explicit, reviewable release-preparation state** is declared instead:

- a flat scalar `release_prep: "X.Y.Z"` in `.version.yaml`. It is a flat
  top-level key on purpose: the ops-engine version-sync parser accepts only
  flat scalar keys and the `locations` list, so the automatic bump never
  rewrites it and the declaration stays representable (a nested map would be
  unparseable).

The rule the checker applies:

- The release-prep state is **active** only when `release_prep` equals the
  **candidate** (`pyproject.toml` == bundle `capability.yaml`) **and** the
  candidate is **ahead of** the released tag. When active, the badge, the
  "What's new" heading and the CHANGELOG top may name the candidate — provided
  they all agree with it. The tool reports a named `release-prep` line and
  exits 0.
- With the state active, any of those positions naming something **other than
  the candidate or the still-released version** is **DRIFT**.
- A `release_prep` that **differs from the candidate** is a named
  `release-prep==candidate` **DRIFT**.
- A `release_prep` that is **not ahead of the released tag** is a named
  `release-prep-ahead` **DRIFT**.
- **Without** `release_prep`, nothing changes: a version ahead of the released
  tag appearing in a release-advertising position (badge, "What's new" heading,
  changelog top) is **DRIFT**.

The flag is removed (or stops being ahead) once the tag exists, at which point
the ordinary rule resumes and the released version becomes the released fact.
In the released tree it is therefore **absent**: the version the release-facing
positions name is a released version whose tag exists, not an early-advertised
one, and `release_prep` reappears only on the next release-preparation commit.

## Distribution note

SkillWeave has **no PyPI distribution** for this repository
(`docs/release-flow.md`): no workflow publishes to PyPI and no documentation
claims one. The setup paths that are actually supported and tested are the
Capacium install, a built wheel, and an editable source install — see the
README *Install* section and `docs/provisioning.md`.

## See also

- `scripts/check-version-truth.py` — the release-facing consistency check.
- `tests/unit/test_doc_version_truth.py` — the speaking-test that asserts this
  document, the README and the tree agree.
- `scripts/check-manifest.py` — bundle pin == member file (a different
  question: a declaration agreeing with what it declares).
- `docs/release-flow.md` — the Forgejo-first distribution and authority model.
- `docs/provisioning.md` — tested Python versions and install flows.
