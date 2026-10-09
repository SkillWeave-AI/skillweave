# Release flow

The single authority for how SkillWeave releases are produced and distributed is
`lvc-ops/docs/release-flow-division.md`. This repository does not restate,
redefine or maintain a local copy of that model.

In one line, the model this repository follows is:

> **`local -> Forgejo (canonical) -> mirror -> GitHub (distribution only)`**

Forgejo is canonical. The ops-engine `ReleaseHandler` produces the release object
on Forgejo on `tag_push` and is the **only** release-object producer for this
repository. The mirror force-pushes the tag ref to GitHub, and GitHub runs
**distribution only** — it never creates the release object, creates the canonical
tag, or rewrites Forgejo's release truth.

## What lives in this repository vs. in the model

| Concern | Where it lives |
|---|---|
| Release-object creation | **generic** — ops-engine `ReleaseHandler`, on Forgejo, on `tag_push`. Not defined here. |
| Version declaration | **business** — this repo's `.version.yaml` |
| Version tooling / tag gate | **generic** — `ops-engine/scripts/version-sync.py` |
| Mirror workflow | **generic** — `unified-mirror.yml`, deployed as `.forgejo/workflows/mirror.yml` |
| Changelog convention | **generic** — shared release policy in ops-engine |
| Release assets (distribution) | **business** — this repo's `.github/workflows/release.yml` (assets only, no release object) |

Anything not named explicitly here follows `release-flow-division.md`, including
its two named exceptions (GitHub-only repositories, and distribution artefacts
the Forgejo Linux host cannot build). Neither exception licenses creating the
release object on GitHub for this repository, which has a Forgejo canonical.

## Distribution boundaries enforced in this repository

- **Forgejo release object** — produced by ops-engine `ReleaseHandler` on `tag_push`.
- **Canonical tag** — created only on Forgejo; nothing on the GitHub side creates
  or moves a tag.
- **GitHub mirror tag** — the mirror force-pushes the tag ref; GitHub must not
  rewrite it.
- **Capacium publication** — triggered by the verified, immutable release/tag
  path, never by a GitHub-created release object.
- **PyPI** — no publication, no claim. There is no operator-approved PyPI product
  and no credentialed PyPI release contract, so no workflow publishes to PyPI and
  no documentation claims a PyPI distribution.

## Version truth

Which version is *released* (the canonical annotated tag, by its peeled commit)
versus *candidate*, *installed* or *advertised early* is stated once in
[docs/version-truth.md](version-truth.md) and checked by
`scripts/check-version-truth.py`. This file names the flow; that one names the
version facts.
