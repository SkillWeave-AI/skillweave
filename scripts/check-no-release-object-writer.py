#!/usr/bin/env python3
"""RFL-013 — no release workflow may write the release object.

Under the Forgejo-first release model the ops-engine ``ReleaseHandler`` is the
only release-object producer. Every workflow in this repository is therefore
covered, not just the two canonical release files: a gate or an artifact job
that quietly took the release-object duty back would be just as wrong as the
release workflow itself.

The two files below must exist — the check cannot pass by being pointed at a
tree where the release workflows are missing. Everything else found under
``.forgejo/workflows`` and ``.github/workflows`` is scanned as well, so a new
workflow is covered the moment it is added:

* ``.forgejo/workflows/forgejo-release.yml`` — the canonical workflow on the
  Forgejo host.
* ``.github/workflows/release.yml`` — the GitHub-side distribution workflow
  (Homebrew asset job), which must never take the release-object duty back.

This check reads the workflow text of the tree it is pointed at and refuses by
name. A comment line is not executable text and is skipped; every other logical
line is inspected, so a marker that appears only in prose does not trip the
check and a marker that actually runs cannot hide.

A shell write can be spelled many ways, and a check that only knew one of them
would be bypassed by the next author, not defended against. The write test is
therefore a *set* of spellings on the same logical line: ``-X POST``, ``-XPOST``,
``--method POST``, ``--method=post`` (case-insensitively), ``--request PATCH``,
a ``requests``/``httpx``-style ``.post(`` call (any of post/patch/put/delete),
``gh api``'s body field (``-f``/``--field``/``--raw-field``) and ``--input``.
Backslash continuations are folded first, so a method on one physical line and
the URL on the next are inspected as one command. This is a syntactic guard,
not a sandbox: an obfuscated command (base64, an alias, a downloaded script)
is out of its reach, and it is reported as such rather than claimed closed.

``--root`` exists so the very same check can be run against an untouched base
tree (where it must exit non-zero) and against the candidate tree (where it
must exit 0). ``tests/release/test_no_release_object_writer.py`` runs exactly
those two invocations plus a table of write spellings, so the check is red in
CI if it stops catching a spelling it claims to catch.

Exit status: 0 when no covered workflow carries a release-object writer, 1 when
any does, 2 when a required workflow file or the workflow tree is absent (the
check cannot pass by not running).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

WORKFLOW_DIRS = (
    ".forgejo/workflows",
    ".github/workflows",
)
REQUIRED_WORKFLOWS_REL = (
    ".forgejo/workflows/forgejo-release.yml",
    ".github/workflows/release.yml",
)
WORKFLOW_REL = REQUIRED_WORKFLOWS_REL[0]

# Every expression that writes a release object. Engine calls are listed beside
# the forge-level writers because in this repository the only writer is the
# engine call: a check that scanned for REST calls alone would pass a workflow
# that still lets the engine create the object. The octokit names are listed
# because several workflows already use ``actions/github-script`` — a
# ``github.rest.repos.createRelease`` there writes the object just as surely as
# a dedicated action does.
WRITER_MARKERS = (
    "ReleaseHandler",
    "publish_release",
    "ops_engine.modules.release",
    "softprops/action-gh-release",
    "actions/create-release",
    "ncipollo/release-action",
    "marvinpinto/action-automatic-releases",
    "release-drafter/release-drafter",
    "goreleaser/goreleaser-action",
    "crazy-max/ghaction-github-release",
    "svenstaro/upload-release-action",
    "createRelease",
    "updateRelease",
    "deleteRelease",
    "uploadReleaseAsset",
    "gh release create",
    "gh release edit",
    "gh release delete",
    "gh release upload",
)

_REST_PATH = re.compile(r"/releases\b")
# Every spelling of "this HTTP call writes" on one logical line. The method is
# matched wherever it sits in the command: `curl -XPOST`, `curl -X POST`,
# `gh api --method=post`, `gh api --method POST`, `curl --request PATCH`.
_HTTP_WRITE = re.compile(
    r"(?:"
    r"(?:-X|--request)\s*=?\s*(?:POST|PATCH|PUT|DELETE)\b"
    r"|--method\s*=?\s*(?:POST|PATCH|PUT|DELETE)\b"
    r"|\.(?:post|patch|put|delete)\s*\("
    r")",
    re.IGNORECASE,
)
# `gh api` infers POST from a body field or an input file and needs no method
# token; only the releases path is interesting here.
_GH_API_BODY = re.compile(
    r"(?:^|\s)(?:-f|--field|--raw-field|--input)(?:\s|=)"
)


def workflow_files(root: Path) -> list[str]:
    """Every workflow file under the covered directories, sorted, as rel paths."""
    found: list[str] = []
    for directory in WORKFLOW_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for pattern in ("*.yml", "*.yaml"):
            found.extend(
                str(path.relative_to(root)) for path in sorted(base.glob(pattern))
            )
    return sorted(set(found))


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Fold backslash continuations into one logical line, kept with its number.

    A method token and the URL it applies to frequently live on different
    physical lines inside a YAML ``run:`` block; folding first means the
    writer test sees the command the shell would actually execute. The number
    reported is the first physical line of the folded command.
    """
    folded: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.rstrip()
        if not pending:
            start = number
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        pending.append(stripped.strip())
        folded.append((start, " ".join(p for p in pending if p)))
        pending = []
    if pending:
        folded.append((start, " ".join(p for p in pending if p)))
    return folded


def findings(text: str, workflow_rel: str) -> list[str]:
    """Every release-object writer found in ``text``, named with its line."""
    found: list[str] = []
    for number, line in logical_lines(text):
        if not line or line.startswith("#"):
            continue
        for marker in WRITER_MARKERS:
            if marker in line:
                found.append("%s:%d %s" % (workflow_rel, number, marker))
                break
        if _REST_PATH.search(line) and _HTTP_WRITE.search(line):
            found.append("%s:%d HTTP write to /releases" % (workflow_rel, number))
        elif "gh api" in line and _REST_PATH.search(line) and _GH_API_BODY.search(line):
            found.append(
                "%s:%d gh api body field writes /releases" % (workflow_rel, number)
            )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Refuse a workflow that writes a release object (RFL-013)."
        )
    )
    parser.add_argument(
        "--root",
        default=".",
        help="the tree to inspect (default: the current directory)",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    missing = [rel for rel in REQUIRED_WORKFLOWS_REL if not (root / rel).is_file()]
    if missing:
        for rel in missing:
            print(
                "::error::ReleaseObjectWriterError: %s is absent under %s; the "
                "release workflow cannot be checked, so the check does not pass."
                % (rel, args.root),
                file=sys.stderr,
            )
        return 2

    covered = workflow_files(root)
    found: list[str] = []
    for rel in covered:
        text = (root / rel).read_text(encoding="utf-8")
        found.extend(findings(text, rel))

    if found:
        for item in found:
            print(
                "::error::ReleaseObjectWriterError: %s. The ops-engine "
                "ReleaseHandler is the only release-object producer; the "
                "workflow must not create, re-assert or edit the object." % item,
                file=sys.stderr,
            )
        return 1

    for rel in covered:
        print(
            "release-object writer check passed for %s: no create, re-assert or "
            "edit call in %s" % ((root / rel), rel)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
