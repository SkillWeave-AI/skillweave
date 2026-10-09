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

Current values (verified by the check; a manual copy of this table is a
snapshot, not the authority):

| Fact | Value | Provenance |
|------|-------|------------|
| candidate | `1.5.9` | `pyproject.toml` `[project].version`; bundle `capability.yaml` `version:` |
| released | `v1.5.9` → `11a6fe72295d02bdb18ad2eaa0ed34837164e803` | tag object `6ab653d3c61dec4b0f6b313720e8c7c3b7e89c84` peeled with `git rev-parse 'v1.5.9^{}'` |
| installed | `1.5.9` (in this qualification venv); may be **not installed** | `importlib.metadata` |
| advertised early | `1.5.10` (**not released**) | the in-flight addendum target release |

## Released is a peeled commit, never "equals HEAD"

An annotated tag is an object; the commit it *points at* is a different SHA
obtained with `git rev-parse '<tag>^{}'`. A release check must therefore:

1. resolve the tag to its **peeled commit** (`^{}` / `^{commit}`), not the tag
   object and not the branch tip; and
2. bind the released version to **canonical release metadata** (the tag's own
   subject line) rather than to `branch HEAD`.

These are different statements on purpose. In this checkout the peeled commit
of `v1.5.9` (`11a6fe7…`) is **not** the current branch tip (`5ccfb9d…`), and it
is not an ancestor of it either: later commits were composed on top of the
released tag. Requiring `tag == HEAD` would make every one of those legitimate
later commits look like a broken release. The peeled-tag policy says instead:
*the released version is whatever the canonical tag's subject declares, at the
commit the tag peels to.*

The hash IDs above match the canonical `v1.5.9` subject `SkillWeave v1.5.9`. The
tag subject is the canonical release-metadata line; the peeled commit is where
that release actually lives.

### Historical peel evidence

Peeling is how each released version was established, not a head comparison.
Recorded for the recent train (each row is `git rev-parse '<tag>^{}'` plus the
`[project].version` at that commit):

| Tag | Tag object | Peeled commit | `[project].version` at peel |
|-----|-----------|---------------|-----------------------------|
| `v1.5.6` | `ed12a3926b4841eccb9faae57d4f404d2430564a` | `2f60f3c403f925fe5317a2a53fd91c5bf8ec914d` | `1.5.5` |
| `v1.5.7` | `1f14f5aa757ba60c09bd45e21669ffb0fa8ae060` | `6d877a24fe77dda79c2396c0ab1ea1ec79636bba` | `1.5.7` |
| `v1.5.8` | `b94f7191bc6d6685729a0fc0417d4000bfd2608e` | `ba58f8960efedea53e4a4392c04a82677db09038` | `1.5.8` |
| `v1.5.9` | `6ab653d3c61dec4b0f6b313720e8c7c3b7e89c84` | `11a6fe72295d02bdb18ad2eaa0ed34837164e803` | `1.5.9` |

`v1.5.6` peeled to a commit whose `pyproject.toml` still read `1.5.5` — the
declared runtime version can lag the tag by a packaging step. That is exactly
why the *released* fact is the tag/subject, not `pyproject.toml`, and why the
check treats the tag subject as release metadata.

## Discovered vs installed (registry / index)

The *discovered* version is what a registry or index reports as available; the
*installed* version is what is present locally. They are shown **separately**,
each with its provenance and freshness, because "the registry says X" is not a
statement about this machine.

Freshness and provenance rules:

- **installed** carries provenance `importlib.metadata` and, when present, the
  distribution's resolved location. When the distribution is absent the check
  reports **not installed** — it never substitutes the candidate or a registry
  value (no fabricated `1.5.9`).
- **discovered** carries the source it was read from (the run snapshot / index
  the consumer actually queried) and the observation time. It is reported as
  an *observation*, never as a fact about the local install.
- Neither may be silently promoted to *released*. `released` is established
  only by the peeled canonical tag above.
- The run-snapshot contract this surface speaks is pinned by id
  `run-snapshot-v1` (`skillweave.cli.support.RUN_SNAPSHOT_SCHEMA_ID`); the SDK
  owns the schema bytes.

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
   **released** fact, not the candidate and not an old value.
3. **changelog** — the CHANGELOG's top heading must name the current version.
4. **installed identity** — when a distribution is importable, its metadata must
   match the candidate the tree builds; a mismatch is a named
   `installed-version-drift` failure. Absence is reported as `not installed`,
   not as a failure of the tree.
5. **skill inventory** — the README's skill count and the declared
   `skills/*/capability.yaml` inventory must equal the on-disk inventory.

**Historical references are allowed** when they are explicitly historical. A
version token is *historical* when it is not presented as the current release —
for example the wording "1.4.0 scope" in README *Host-Neutral Scope*. The check
does not demand that an old version vanish; it demands that an old version is
not **advertised as current** and that candidate `1.5.10` is not advertised as
**released**. Concretely, the check fails if a version ahead of the tag (e.g.
`1.5.10`) appears in a release-advertising position (badge, "What's new"
heading, changelog top) before it is released.

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
