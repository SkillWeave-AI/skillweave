#!/usr/bin/env python3
"""RFL-013 — no release workflow may write the release object.

Under the Forgejo-first release model the ops-engine ``ReleaseHandler`` is the
only release-object producer. Both release workflows in this repository keep
their gates and their artifact jobs, but neither may create, re-assert or edit
a release object itself — neither through a forge REST write, through a
release-creating action, nor through a call into the engine:

* ``.forgejo/workflows/forgejo-release.yml`` — the canonical workflow on the
  Forgejo host.
* ``.github/workflows/release.yml`` — the GitHub-side distribution workflow
  (Homebrew asset job), which must never take the release-object duty back.

This check reads the workflow text of the tree it is pointed at and refuses by
name. A comment line is not executable text and is skipped; every other line is
inspected, so a marker that appears only in prose does not trip the check and a
marker that actually runs cannot hide. ``--root`` exists so the very same check
can be run against an untouched base tree (where it must exit non-zero) and
against the candidate tree (where it must exit 0).

Exit status: 0 when every covered workflow carries no release-object writer, 1
when any does, 2 when a covered workflow file is absent (the check cannot pass
by not running).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

WORKFLOWS_REL = (
    ".forgejo/workflows/forgejo-release.yml",
    ".github/workflows/release.yml",
)
WORKFLOW_REL = WORKFLOWS_REL[0]

# Every expression that writes a release object. Engine calls are listed beside
# the forge-level writers because in this repository the only writer is the
# engine call: a check that scanned for REST calls alone would pass a workflow
# that still lets the engine create the object.
WRITER_MARKERS = (
    "ReleaseHandler",
    "publish_release",
    "ops_engine.modules.release",
    "softprops/action-gh-release",
    "actions/create-release",
    "gh release create",
    "gh release edit",
)

_REST_WRITE_METHOD = re.compile(r"\b(?:POST|PATCH|PUT)\b")


def findings(text: str, workflow_rel: str = WORKFLOW_REL) -> list[str]:
    """Every release-object writer found in ``text``, named with its line."""
    found: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for marker in WRITER_MARKERS:
            if marker in line:
                found.append("%s:%d %s" % (workflow_rel, number, marker))
        if "/releases" in line and _REST_WRITE_METHOD.search(line):
            found.append("%s:%d REST write to /releases" % (workflow_rel, number))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Refuse a canonical release workflow that writes a release object "
            "(RFL-013)."
        )
    )
    parser.add_argument(
        "--root",
        default=".",
        help="the tree to inspect (default: the current directory)",
    )
    args = parser.parse_args(argv)

    missing = [
        rel for rel in WORKFLOWS_REL if not (Path(args.root) / rel).is_file()
    ]
    if missing:
        for rel in missing:
            print(
                "::error::ReleaseObjectWriterError: %s is absent under %s; the "
                "release workflow cannot be checked, so the check does not pass."
                % (rel, args.root),
                file=sys.stderr,
            )
        return 2

    found: list[str] = []
    for rel in WORKFLOWS_REL:
        text = (Path(args.root) / rel).read_text(encoding="utf-8")
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

    for rel in WORKFLOWS_REL:
        print(
            "release-object writer check passed for %s: no create, re-assert or "
            "edit call in %s" % ((Path(args.root) / rel), rel)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
