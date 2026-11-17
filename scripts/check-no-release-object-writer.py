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
name. Comment text is not executable, so it is removed before the writer test:
a marker that appears only in prose does not trip the check, and a marker that
actually runs cannot hide. The removal follows the shell's own rule rather than
a "line starts with #" guess. A ``#`` that begins a word (start of line, after
whitespace, or after ``;``/``&``/``|``/``(`` etc.) starts a comment that runs to
the end of the *logical* line — so a backslash inside that comment does **not**
continue the line, and the next physical line is a separate statement that the
shell executes. Folding continuations first and then skipping a line that looks
like a comment would therefore hide a real write: a comment ending in a
backslash followed by ``curl -X POST ...`` folds into one line that starts with
``#``. Instead the comment is cut at its ``#`` and the following line is
inspected on its own. ``#`` is not special inside quotes (``'a#b'``, ``"a#b"``),
nor inside a word (``url#frag``), and comments are removed before literal
assignments are collected, so a commented-out ``URL=`` cannot rebind a real one.

A shell write can be spelled many ways, and a check that only knew one of them
would be bypassed by the next author, not defended against. The write test is
therefore a *set of write signals* on a line that also addresses a release
path, not one canonical command:

* **method tokens** — ``-X POST``, ``-XPOST``, ``--request PATCH``,
  ``--method=put``, httpie's bare ``http POST``, and method keywords inside a
  client call (``method="POST"``, ``.request("PUT", ...)``);
* **request bodies** — curl's short ``-d``/``-T``/``-F`` (bare, bundled with
  boolean flags as in ``-sSd``, or attached as in ``-dx``), every long spelling
  of them (``--data`` and its ``-raw``/``-binary``/``-urlencode``/``-ascii``
  variants, ``--form``/``--form-string``, ``--upload-file``, ``--json``), wget
  ``--post-data``/``--post-file``, ``urlopen(..., data=...)``, and httpie/xh's
  inferred body item (``http URL name=value`` is a POST; ``name==value`` is a
  query parameter and is not) — because a write needs no method flag at all
  when a body is present;
* **``gh api`` body forms** — ``-f``/``-F``/``--field``/``--raw-field``/
  ``--input`` (gh infers POST from any of them);
* **language clients** — ``requests``/``httpx`` ``.post(`` (any of
  post/patch/put/delete);
* **forge actions and octokit names** — the list in ``WRITER_MARKERS``.

Backslash continuations are folded first, so a method on one physical line and
the URL on the next are inspected as one command. The shell deletes a
backslash-newline with no separator, so a token split across the break
(``relea\\`` then ``ses``) is one token; the fold concatenates the pieces
verbatim for that reason, and inspects a spaced view beside it because the YAML
block indentation gives the ordinary ``curl -X POST \\`` + URL spelling a
separation the shell never sees. A hit in either view is reported. A pipe, ``;`` or ``&&`` ends
a curl segment, so a body flag in one command is not attributed to a
downstream one (``curl … | tar -T -`` is a download, not an upload). Literal ``NAME=value``
assignments in the same workflow are substituted into ``$NAME``/``${NAME}``
references (a few levels deep), so a URL held in a shell variable is inspected
as the URL it is rather than as an opaque ``$URL``.

This is a syntactic guard, not a sandbox. A target assembled at run time from
something the workflow never writes down — the output of another command, a
secret, a script fetched while running — cannot be resolved here, and an
obfuscated call (base64, an alias) is out of reach. That limit is stated
rather than claimed closed.

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
# The method token, wherever it sits in the command: `curl -XPOST`,
# `curl -X POST`, `gh api --method=post`, `curl --request PATCH`, httpie's
# `http POST`, and a method keyword passed to a client.
_HTTP_METHOD = re.compile(
    r"(?:"
    r"(?:-X|--request)\s*=?\s*['\"]?(?:POST|PATCH|PUT|DELETE)\b"
    r"|(?:--method|-method)\s*=?\s*['\"]?(?:POST|PATCH|PUT|DELETE)\b"
    # httpie/xh name the method bare: `http POST url`, `xh PATCH url`, and
    # flag options may sit between the tool and the method: `xh --json POST url`.
    r"|(?:^|\s)(?:https?|xh)(?:\s+--?[A-Za-z0-9_-]+)*\s+(?:POST|PATCH|PUT|DELETE)\b"
    # A method keyword passed to a client: `method="POST"` (Python),
    # `method: "POST"` (JS), `-Method Post` (PowerShell, above).
    r"|\bmethod\s*[:=]\s*['\"]?(?:POST|PATCH|PUT|DELETE)\b"
    r"|\brequest\s*\(\s*['\"](?:POST|PATCH|PUT|DELETE)\b"
    r"|\.(?:post|patch|put|delete)\s*\("
    r")",
    re.IGNORECASE,
)
# A body makes a write without any method token: curl's data/upload/form/json
# flags, wget's post-data, a client's `data=` argument, and httpie/xh's body
# item (`http URL tag_name=v1` is a POST). curl's short body flags are ``-d``,
# ``-T`` and ``-F``; they bundle with boolean flags (``-sSd body``) and take
# their argument either as the next token (``-d body``) or attached (``-d'x'``,
# ``-dx``, ``-dnoeq``, ``-Tfile.zip``, ``-Ff=@x``). The match is case-sensitive
# on purpose: ``-fsSL`` is four lowercase/uppercase boolean flags and contains
# no ``d``/``T``/``F``, so it is not mistaken for a form. The curl segment
# stops at a pipe, `;` or `&&`, so a downstream ``tar -T -`` is not read as an
# upload.
_HTTP_BODY = re.compile(
    r"(?:"
    r"\bcurl\b[^|\n;&]*?(?:"
    r"(?:^|\s)-[A-Za-z]*[dTF](?:\s|\S)"
    r"|--data(?:-(?:raw|binary|urlencode|ascii))?(?:\s|=)"
    r"|--form(?:-string)?(?:\s|=)"
    r"|--upload-file(?:\s|=)"
    r"|--json(?:\s|=)"
    r")"
    r"|\bwget\b[^|\n;&]*?--post-(?:data|file)(?:\s|=)"
    r"|\burlopen\s*\([^\n]*?\bdata\s*="
    # httpie/xh infer a POST from a body item (`name=value`; query params use
    # `==` and are not a body), with optional flag options before the URL.
    r"|(?:^|[\s;&|])(?:https?|xh)(?:\s+--?[A-Za-z0-9_-]+)*\s+"
    r"[^\n]*?\b[A-Za-z_][A-Za-z0-9_-]*=(?!=)"
    r")"
)
# `curl -G` turns a body into a query string: a read, not a write. Only the
# body signal is suppressed; an explicit write method still counts.
_CURL_FORCED_GET = re.compile(r"\bcurl\b[^\n]*?(?:\s-G\s|\s--get\b)", re.IGNORECASE)
# `gh api` infers POST from a body field or an input file and needs no method
# token; only the releases path is interesting here. `-F` is gh's short
# `--field` and switches the request to POST exactly as `-f` does.
_GH_API_BODY = re.compile(
    r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:\s|=|[A-Za-z0-9_][A-Za-z0-9_-]*=)"
)
# Literal shell assignments (`URL=https://...`) and the value they bind, so a
# URL held in a variable is resolved before the path test. Only literal
# right-hand sides are captured; a computed value stays unresolved and is
# reported as a stated limit in the docstring, not silently accepted as safe.
_ASSIGNMENT = re.compile(
    r"(?:^|[\s;&|])(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(?:\"([^\"\n]*)\"|'([^'\n]*)'|(\S+))"
)
_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def literals(text: str) -> dict[str, str]:
    """Every literal shell assignment in ``text``, last binding wins."""
    bound: dict[str, str] = {}
    for match in _ASSIGNMENT.finditer(text):
        name = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        bound[name] = value
    return bound


def resolve(line: str, bound: dict[str, str], depth: int = 4) -> str:
    """Substitute literal variables, a few levels deep; leave the rest as-is."""
    for _ in range(depth):
        new = _VAR_REF.sub(lambda m: bound.get(m.group(1), m.group(0)), line)
        if new == line:
            break
        line = new
    return line


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


def strip_comment(line: str) -> str:
    """Remove a shell comment from one physical line, quote-aware.

    A ``#`` starts a comment only when it begins a word (line start, after
    whitespace, or after an operator such as ``;``/``&``/``|``/``(``); inside
    quotes or inside a word it is literal. Everything from that ``#`` to the
    end of the physical line goes — including a trailing backslash, which the
    shell does not treat as a continuation once a comment has begun.
    """
    quote = ""
    previous = ""
    escaped = False
    for index, char in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and (index == 0 or previous in " \t;&|()<>"):
            return line[:index]
        previous = char
    return line


def logical_lines(text: str) -> list[tuple[int, str, str]]:
    """Fold backslash continuations into one logical line, kept with its number.

    A method token and the URL it applies to frequently live on different
    physical lines inside a YAML ``run:`` block; folding first means the
    writer test sees the command the shell would actually execute. The number
    reported is the first physical line of the folded command.

    The shell deletes a backslash-newline with **no** separator, so the
    fragments are concatenated verbatim after each physical line is stripped:
    ``relea\\`` followed by ``ses`` is the single token ``releases``, and a
    fold that inserted a space would make the write token and the URL it
    addresses invisible. Two views are therefore returned for every folded
    command. The first joins with nothing and is what the shell executes; the
    second joins with a space because the raw YAML text still carries block
    scalar indentation, which the shell never sees — a continuation that the
    shell joins with no separator can still look separated here. Both views
    are inspected and a hit in either counts: the split token is caught in the
    verbatim view, and the ordinary ``curl -X POST \\`` + URL spelling (whose
    separation came from the indentation) in the spaced one. Over-reporting is
    the safe direction for a gate.

    Comments are stripped *before* folding, one physical line at a time.
    Folding first would let a ``#`` comment absorb the following line — its
    trailing backslash folds into the comment, the result starts with ``#`` and
    is skipped — while the shell ends the comment at the newline and executes
    that following line. A folded command is reported from the physical line
    it begins on.
    """
    folded: list[tuple[int, str, str]] = []
    pending: list[str] = []
    start = 0

    def joined(parts: list[str], separator: str) -> str:
        return separator.join(p for p in parts if p)

    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = strip_comment(raw).strip()
        if not pending:
            start = number
        if stripped.endswith("\\"):
            pending.append(stripped[:-1].strip())
            continue
        pending.append(stripped)
        folded.append((start, joined(pending, ""), joined(pending, " ")))
        pending = []
    if pending:
        folded.append((start, joined(pending, ""), joined(pending, " ")))
    return folded


def findings(text: str, workflow_rel: str) -> list[str]:
    """Every release-object writer found in ``text``, named with its line.

    Each folded command is inspected in both views ``logical_lines`` returns —
    the separator-less one the shell executes and the spaced one that still
    carries the YAML block indentation — and a hit in either is reported once
    for that line.
    """
    found: list[str] = []
    bound = literals("\n".join(strip_comment(line) for line in text.splitlines()))
    for number, verbatim, spaced in logical_lines(text):
        if not verbatim and not spaced:
            continue
        hit = None
        for marker in WRITER_MARKERS:
            if marker in verbatim or marker in spaced:
                hit = "%s:%d %s" % (workflow_rel, number, marker)
                break
        if hit is None:
            http_hit = False
            gh_hit = False
            for line in (verbatim, spaced):
                resolved = resolve(line, bound)
                writes_release = _REST_PATH.search(resolved) is not None
                if not writes_release:
                    continue
                method = _HTTP_METHOD.search(resolved) is not None
                # A curl `-G` turns the body into a query string: it is a read,
                # so the body signal is suppressed there. An explicit write
                # method still counts.
                body = _HTTP_BODY.search(
                    resolved
                ) is not None and not _CURL_FORCED_GET.search(resolved)
                if method or body:
                    http_hit = True
                elif "gh api" in resolved and _GH_API_BODY.search(resolved):
                    gh_hit = True
            if http_hit:
                hit = "%s:%d HTTP write to a release path" % (workflow_rel, number)
            elif gh_hit:
                hit = (
                    "%s:%d gh api body field writes a release path"
                    % (workflow_rel, number)
                )
        if hit is not None:
            found.append(hit)
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
