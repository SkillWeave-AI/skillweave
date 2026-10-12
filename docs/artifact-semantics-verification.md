# Artifact-specific semantics before an Assay PASS (SW-1510-VERIFY)

This document is the operator-facing description of the artifact-semantics
boundary that gates an Assay PASS. It explains the separation of facts, the
proof-graph contract, the deterministic independent-host recipe, the actual
installed proof boundary, and the minimal candidate-only completion handoff.
It records honest limitations.

## The three facts, kept separate

A gate is reconciled only when the evidence backing it is real. Three facts
that used to collapse into one "it exited 0" are now distinct:

* **Process completion** -- the probe exited cleanly with non-empty output.
  Owned by `CompletionContract` / `Verifier` in
  `src/skillweave/runtime/verify.py`; unchanged by this lane.
* **Artifact integrity** -- the artifact's bytes hash to the digest it
  claims. Owned by the self-content digest (`artifact_sha256`).
* **Semantic verification** -- the artifact actually *analyzed* the source it
  claims. Owned by `ArtifactEvidenceVerifier`; this is the boundary added
  here.

A fourth fact, human/controller acceptance, remains a separate authority and
is never inferred from the three above.

## Where it is consumed

`reconcile_gate(...)` in `src/skillweave/runtime/gate_reconciliation.py` is the
gate consumer. As soon as a caller supplies artifact evidence
(`artifact_evidence=`) or asks to bind coverage to real source
(`source_resolver=`), artifact-semantic verification becomes mandatory: a
missing, empty, phantom, README-only, unsupported, or substituted proof can no
longer reconcile a gate to `True`.

The legacy call signature `reconcile_gate(gate_name, registry, observer,
authority)` with **no** artifact evidence keeps the explicit pre-SW-1510 path
through a legacy adapter -- those flows never asserted analyzed-source
semantics. `ReconciliationResult` deliberately carries no override/force
field: a refusal is folded into `authority_statement` (raw diagnostics
preserved verbatim) plus the `evidence_weight` label
`artifact_evidence_refused`.

## The proof-graph contract (`evidence-receipt-v1`)

An artifact **is** an evidence receipt. The SDK schema authority
(`https://skillweave.dev/schemas/evidence/v1`, `additionalProperties: false`)
stays pinned and read-only; the analysis proof graph is namespaced under
`metadata.artifact_verifier` (a free-form `metadata` object), so this module
never re-authors a schema.

```jsonc
{
  "artifact_id": "art-1",
  "sha256": "<self-content digest, sha256 field excluded>",
  "schema_version": "1",
  "producer_command": "analyze",
  "subject_repo": "skillweave",
  "subject_commit": "<40-hex commit>",
  "created_at": "2026-10-05T00:00:00Z",
  "evidence_type": "artifact",
  "purpose": "artifact analysis proof",
  "metadata": {
    "artifact_verifier": {
      "version": "evidence-receipt-v1",
      "analyzed": true,
      "coverage": [
        {
          "path": "pkg/module.py",
          "disposition": "analyzed",
          "content_sha256": "<sha256 of the real file bytes>",
          "analysis_digest": "<sha256 of the real analysis artifact>"
        }
      ],
      "exclusions": [
        {
          "path": "README.md",
          "justification": "documentation only, not executable source",
          "bounded": "narrative paths (*.md)"
        }
      ]
    }
  }
}
```

An unsupported `version` is refused (`SUPPORTED_ARTIFACT_VERSIONS`). The
understanding of the graph is:

* **Analyzed dispositions** (`ANALYZED_DISPOSITIONS`): `analyzed`, `included`,
  `reviewed`, `modified`, `inspected`. Anything else is unsupported and fails
  closed. The `analyzed` flag must be the boolean `True` -- a narrative string
  is not a flag.
* **Coverage binding.** A coverage entry must bind *both* the real file
  content (`content_sha256` equal to the resolver's bytes) *and* a
  non-placeholder analysis artifact (`analysis_digest`), or be an explicit,
  bounded, independently-justified exclusion.
* **Exclusions / N/A** (`EXCLUSION_DISPOSITIONS`): `not_applicable`,
  `excluded`, `skipped`, `ignored`. Each requires a real justification
  (`_PLACEHOLDER_JUSTIFICATIONS` such as `n/a`, `todo`, `-` are refused), a
  bound scope that actually *covers* the excluded path, **and** a reason that
  is independently checkable against the real source name and bytes.
* **Independent exclusion-reason authority.** Matching the bound to the path
  does not establish the *reason*, and the reason is never established by the
  justification's *wording* either -- the outcome for a file must not depend on
  which keyword the author typed. The authority is the real source **name and
  bytes** (`_independent_reason_check` -> `_is_documentation`):
  * A **generated / vendored / third-party / codegen** claim asserts a
    provenance the source cannot confirm, so it is **unsupported** and refused
    (`_GENERATED_REASON_KEYWORDS`) regardless of the bound or the bytes.
  * Otherwise the file is admitted as a bounded exclusion only when it is
    independently established as **documentation** from the real source:
    * no real bytes, a binary body, or bytes that cannot be decoded -> nothing
      can be established, so it fails closed;
    * a **code name** (a recognised source suffix, `_CODE_SUFFIXES`) is
      executable by name and fails closed even when its body happens not to
      trip a byte marker;
    * a **text body** is classified by its own bytes (`_classify_text_body` +
      `_CODE_LINE_PATTERNS`). A body carrying a recognised code line -- a
      shebang, an import/declaration, an assignment, a shell builtin or common
      executable, a pipeline, SQL, an embedded `<script>`/`<?php`, a call, a
      block close -- is **code** and is **contradicted** and refused, even
      under a documentation suffix. A prose body (a README, a markdown
      overview, a plain-text licence, notes) is documentation.
  * Consequences: `settings.py` containing `LIMIT = 7` excluded as "narrative
    documentation only" is refused; so is a no-suffix `settings` with
    `LIMIT = 7`, a no-suffix `run` with `rm -rf "$WORK"`, an
    `engine_copy.txt` containing a `def`, a `migrate.sql` with `DROP TABLE`, a
    `page.html` with a script fetch, a `Makefile` piping `curl` to `sh`, a
    `.md` whose bytes are a shell script, and code in other languages without a
    code suffix -- none of these is admitted on the strength of its name or the
    author's wording. Conversely a genuinely narrative `OVERVIEW.md` with the
    justification "Project overview text for readers" -- no "documentation"
    keyword at all -- is admitted, and a plain-text licence is admitted.
  * When the resolver cannot return the real source bytes for the excluded
    path (no resolver, missing file, non-bytes), nothing can be independently
    established, so the exclusion is refused rather than trusted.
* **Refused outright:** a missing resolver; an unresolvable file; a coverage
  entry whose only evidence is a `git ls-tree` / `git ls-files` enumeration (a
  line of output is not an analysis); a **README-only** graph over a tree with
  real files (`NARRATIVE_PATH_MARKERS`); a **phantom** covered path not present
  in the source; a **substituted** commit/content field; an empty/partial graph
  over a populated source; a **contradicted**, **unsupported** or
  **unavailable** exclusion reason.

A refusal is never repaired by schema fixup or a placeholder coverage entry,
and a timeout or missing input never becomes a PASS.

## Deterministic independent-host recipe

This is the exact procedure proven by
`tests/unit/test_1510_verify.py::test_installed_wheel_rejects_falsified_and_admits_genuine`.
It runs the **installed** package -- a fresh wheel's own interpreter, with the
source tree off `sys.path` -- so the real installed native consumer, not a
source-tree shim, renders the verdict.

```console
$ WORK=<empty scratch dir>            # never the checkout
$ cp -R src "$WORK/src"
$ cp pyproject.toml README.md LICENSE "$WORK/"
$ python3 -m pip wheel --no-deps --no-index --no-build-isolation \
      -w "$WORK/dist" "$WORK"
$ python3 -m venv "$WORK/venv"
$ "$WORK/venv/bin/pip" install --no-deps --no-index "$WORK"/dist/skillweave-*.whl

# The offline install cannot fetch PyYAML; stage the ambient copy without
# reaching the network or the source tree, then neutralize PYTHONPATH.
$ SITE=$("$WORK/venv/bin/python" -c \
      'import site,sys;print([p for p in sys.path if p.endswith("site-packages")][-1])')
$ cp -R "$(python3 -c 'import yaml,pathlib;print(pathlib.Path(yaml.__file__).resolve().parent)')" "$SITE/yaml"

$ PYTHONPATH= TMPDIR="$WORK/tmp" "$WORK/venv/bin/python" -c \
    'import json,sys
from skillweave.runtime.verify import ArtifactEvidenceVerifier, FilesystemSourceResolver
art, root, commit, amap = sys.argv[1:5]
artifact = json.load(open(art)); amap = json.load(open(amap))
resolver = FilesystemSourceResolver(root, commit=commit)
reader = lambda p: amap[p].encode() if p in amap else None
r = ArtifactEvidenceVerifier(expected_commit=commit).verify(
        artifact, resolver=resolver, analysis_reader=reader)
print(json.dumps(r.to_dict(), sort_keys=True))' \
    "$WORK/artifact.json" "$WORK/src-tree" "<40-hex-commit>" "$WORK/amap.json"
```

A genuine artifact yields `admitted: true`, `gate_state: "pass"`,
`analyzed_files` exactly the non-narrative files, and **no** reasons. A
falsified artifact (real digests but substituted content) yields
`admitted: false`, `gate_state: "fail"`, and a raw
`substituted source field` diagnostic. A README-only graph over the same
populated tree is refused.

## Actual installed proof boundary

What is actually exercised end to end today (and what is not):

* **In scope / proven:** the installed wheel's interpreter imports the
  installed `skillweave.runtime.verify`, honors `expected_commit`, and refuses
  a falsified artifact while admitting a genuine one -- raw reasons and exact
  source/commit pins preserved. `PYTHONPATH` is popped so the installed
  distribution, not the checkout, answers.
* **Gate consumer:** `reconcile_gate` demands artifact semantics whenever
  evidence or a source resolver is carried, and folds the refusal into
  `authority_statement`. This is proven at the source-tree consumer in the
  suite; the installed-wheel proof covers the verifier it delegates to.
* **Out of scope here:** the pinned SDK is imported, never copied; a missing
  or unusable schema authority fails closed to the structural contract rather
  than inventing a schema. SDK/WorkContract schema authority is read-only and
  unchanged by this lane.

## Minimal candidate-only completion handoff recipe

This is the minimal recipe for an autonomous
**probe → cold/criticalPro → sameUUID repair/re-review** loop. It confers
**no** dependency-admission and **no** integration authority; only the root of
the repo qualifies the run and installs the candidate.

1. **Probe (candidate build).** Build the wheel from a clean copy as above.
   The candidate is identified by the same UUID across the whole loop; nothing
   downstream may mint a new identity.
2. **Cold / criticalPro review.** In a *fresh* venv (cold), install the
   candidate and run the verifier on behalf of a criticalPro reviewer:
   genuine admitted, falsified refused. Record the raw verdict, the exact
   source pins, and the candidate UUID.
3. **sameUUID repair.** If refused, repair the proof graph in place -- same
   candidate UUID, same source/commit pins -- never by editing digests to
   match wrong bytes and never by adding a placeholder coverage entry.
4. **Re-review.** Rebuild, reinstall into a fresh cold venv, re-verify. The
   loop terminates only when the genuine artifact is admitted and the
   completion contract independently reports PASS.
5. **Handoff, not admission.** The candidate is handed off with its UUID,
   proofs, and verdicts. Dependency admission and integration remain the
   controller's authority; the root qualifies the candidate and installs it.

## Limitations

* The verifier proves the analysis proof graph is *internally* real against a
  supplied resolver; it does not authenticate the resolver's host. A host that
  fabricates both the source tree and the analysis bytes is out of scope by
  design (the boundary is against a delegated worker, not a malicious host
  administrator).
* `reconcile_gate`'s legacy path (no evidence carried) intentionally keeps the
  pre-SW-1510 behavior; flows that predate artifact semantics are not
  retroactively re-gated.
* The handoff recipe is a procedure; it grants no new service authority and
  adds no parallel gate schema.
