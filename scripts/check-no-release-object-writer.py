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
as the URL it is rather than as an opaque ``$URL``. A job- or step-level
``env:`` mapping binds a variable the same way, so ``$RELEASE_API`` set there is
resolved too; only an expression-valued (``${{ ... }}``) or otherwise computed
binding stays opaque.

A folded YAML scalar (``run: >`` / ``run: >-``) joins its physical lines with
spaces, so a method on one line and the URL on the next are one command the
runner executes. That join is YAML's, not the shell's, so a second pass parses
the document and re-checks each step's resolved ``run`` text. PyYAML is
required for that pass; a missing parser is refused (exit 2) rather than
silently skipped, because a check that quietly loses a pass is a check that
quietly reopens a gap.

This is a syntactic guard, not a sandbox. A target assembled at run time from
something the workflow never writes down — the output of another command, a
secret, a script fetched while running, or a computed expression — cannot be
resolved here, and an obfuscated call (base64, an alias) is out of reach.

A raw TCP write that spells the HTTP request as bytes is modelled when the
request line is written down: a ``POST``/``PATCH``/``PUT``/``DELETE`` line
addressing ``/releases`` piped into ``nc``/``ncat``/``netcat``/``socat``/
``openssl s_client``, a socket tool given a port, or a ``/dev/tcp`` redirect is
refused. What stays out of reach is the same class as any other computed
target — a request line or path assembled at run time, or a writer fetched and
executed — and a ``| nc -l`` listener, which cannot reach the forge. These
limits are stated rather than claimed closed, and the ``env:``/folded-scalar/
raw-request-line cases above are *not* among them: the target there is written
down verbatim in the workflow.

``--root`` exists so the very same check can be run against an untouched base
tree (where it must exit non-zero) and against the candidate tree (where it
must exit 0). ``tests/release/test_no_release_object_writer.py`` runs exactly
those two invocations plus a table of write spellings, so the check is red in
CI if it stops catching a spelling it claims to catch.

Exit status: 0 when no covered workflow carries a release-object writer, 1 when
any does, 2 when a required workflow file or the workflow tree is absent, or
when PyYAML is unavailable (the check cannot pass by not running).
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

# The Forgejo/Gitea-native CLI writes the release object with `tea release
# create` (and `tea releases create` — both spellings exist). `tea releases
# list`/`show` are reads and must stay green, so this is a write-verb regex
# rather than a bare marker.
_TEA_WRITE = re.compile(
    r"\btea\s+releases?\s+(?:create|edit|delete|attach)\b", re.IGNORECASE
)
# A language client's release writer, usually called as a dotted path
# (`Github(...).get_repo(...).create_git_release(...)`). The dotted names in
# ``WRITER_MARKERS`` are CamelCase octokit spellings; these snake_case ones
# belong to PyGithub and friends.
_DOTTED_WRITER = re.compile(
    r"\b(?:create_git_release|create_release|edit_release|delete_release"
    r"|upload_release_asset)\s*\("
)

_REST_PATH = re.compile(r"/releases\b")

# A raw-socket writer sends its HTTP request as *data*, not as a command line:
# ``printf 'POST /api/v1/repos/o/r/releases HTTP/1.1\r\n' | nc forge 443``, the
# same payload through ``openssl s_client``, or a ``/dev/tcp`` redirect. The
# release path therefore does not appear beside a method flag; instead a
# request line (``POST <path>/releases HTTP/1.1``) appears and a transport
# supplies the socket. A quoted request line on its own is inert data piped to
# nothing, so a `|` into a socket tool, a socket tool with a port, or a
# `/dev/tcp` redirect is required as well.
_RAW_REQUEST_LINE = re.compile(
    r"\b(?:POST|PATCH|PUT|DELETE)\s+\S*/releases\b", re.IGNORECASE
)
_RAW_SOCKET = re.compile(
    r"(?:"
    r"\|\s*(?:nc|ncat|netcat|socat|openssl\s+s_client)\b"
    r"|\b(?:nc|ncat|netcat|socat)\b[^|\n;&]*\s\d{2,5}\b"
    r"|/dev/(?:tcp|udp)/"
    r")"
)
# `nc -l` is a listener, not a sender: it cannot reach the forge, so data piped
# into it is not a release write. Suppress the raw-socket signal for that form
# so a debug listener piping a documented request line is not a false positive.
_RAW_SOCKET_LISTENER = re.compile(
    r"\|\s*(?:nc|ncat|netcat)\b[^|\n;&]*\s(?:-l|--listen)\b"
)

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
    # GNU wget's `--body-data`/`--body-file` are documented aliases of
    # `--post-data`/`--post-file` and send the same POST.
    r"|\bwget\b[^|\n;&]*?--(?:post|body)-(?:data|file)(?:\s|=)"
    r"|\burlopen\s*\([^\n]*?\bdata\s*="
    # httpie/xh infer a POST from a body item, with optional flag options
    # before the URL. A body item is `name=value` (a query parameter is
    # `name==value` and is not a body), raw JSON `name:=value` (httpie/xh
    # default to `application/json`), or a file field `name@path`. A bare
    # `=` must not match the `==` query form, hence the lookahead.
    r"|(?:^|[\s;&|])(?:https?|xh)(?:\s+--?[A-Za-z0-9_-]+)*\s+"
    r"[^\n]*?\b[A-Za-z_][A-Za-z0-9_-]*(?:=(?!=)|:=|@)"
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
# One key/value line inside an ``env:`` block (`RELEASE_API: https://...`).
_ENV_KEY = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


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


def env_literals(text: str) -> dict[str, str]:
    """Literal values bound by a job- or step-level ``env:`` mapping.

    ``env:`` binds a variable the same way a shell assignment does:
    ``env:\\n  RELEASE_API: https://.../releases`` makes ``$RELEASE_API``
    resolve at run time. Only a literal scalar inside an ``env:`` block is
    captured; an expression (``${{ ... }}``), a block scalar or a computed value
    stays unresolved, exactly as a computed shell value does, and the stated
    limit still covers it.
    """
    bound: dict[str, str] = {}
    env_indent: int | None = None
    for raw in text.splitlines():
        line = strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if env_indent is not None:
            if indent > env_indent:
                match = _ENV_KEY.match(line)
                if match:
                    value = match.group(2).strip()
                    if (
                        value
                        and not value.startswith(("${{", "|", ">", "&", "*"))
                        and "${{" not in value
                    ):
                        bound[match.group(1)] = value.strip("\"'")
                continue
            env_indent = None
        if line.strip() == "env:":
            env_indent = indent
    return bound


def rendered_runs(text: str) -> list[tuple[str, str]]:
    """The ``run`` text of every step, *as the runner executes it*.

    A YAML folded scalar (``run: >`` / ``run: >-``) joins its physical lines
    with spaces, so a method on one line and the URL on the next become one
    command the runner runs. The backslash fold in ``logical_lines`` cannot see
    that, so this pass parses the document and re-checks each step's resolved
    ``run`` text. A document PyYAML rejects yields nothing here and the
    textual passes still run; a *missing* PyYAML is refused by ``main`` rather
    than silently skipping this pass. The step's name (or index) is reported
    beside each hit.
    """
    import yaml  # noqa: PLC0415 — required; ``main`` refuses without it

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []

    runs: list[tuple[str, str]] = []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if isinstance(run, str) and run.strip():
                label = step.get("name") or "step %d" % index
                runs.append((str(label), run))
    return runs


def _marker_hit(line: str) -> str | None:
    """The writer marker on a command, if any — name, CLI verb or client call."""
    for marker in WRITER_MARKERS:
        if marker in line:
            return marker
    match = _TEA_WRITE.search(line)
    if match:
        return match.group(0).strip()
    match = _DOTTED_WRITER.search(line)
    if match:
        return match.group(0).rstrip("( ").strip()
    return None


def _http_kind(line: str) -> str | None:
    """Classify a resolved command that addresses a release path.

    Returns ``"http"`` for a method token or request body, ``"gh"`` for a
    ``gh api`` body field, and ``None`` for a read or an unrelated line.
    """
    if _REST_PATH.search(line) is None:
        return None
    # A raw-socket write: the HTTP request line is data, and a socket tool or a
    # /dev/tcp redirect carries it. `printf 'POST .../releases ...' | nc h 443`.
    if (
        _RAW_REQUEST_LINE.search(line)
        and _RAW_SOCKET.search(line)
        and not _RAW_SOCKET_LISTENER.search(line)
    ):
        return "http"
    if (
        _HTTP_METHOD.search(line) is not None
        # A curl `-G` turns the body into a query string: it is a read, so the
        # body signal is suppressed there. An explicit write method still counts.
        or (
            _HTTP_BODY.search(line) is not None
            and not _CURL_FORCED_GET.search(line)
        )
    ):
        return "http"
    if "gh api" in line and _GH_API_BODY.search(line):
        return "gh"
    return None


def findings(text: str, workflow_rel: str) -> list[str]:
    """Every release-object writer found in ``text``, named with its line.

    Each folded command is inspected in both views ``logical_lines`` returns —
    the separator-less one the shell executes and the spaced one that still
    carries the YAML block indentation — and a hit in either is reported once
    for that line. A second pass parses the YAML and re-checks every step's
    resolved ``run`` text, so a folded scalar (whose line joins are YAML's, not
    the shell's) is inspected as the single command the runner executes. The
    second pass reports a hit by the step that carries it; the textual pass
    cannot map a rendered command back to a physical line, so a hit found only
    there is named by step rather than by line.
    """
    found: list[str] = []
    bound = literals("\n".join(strip_comment(line) for line in text.splitlines()))
    # A step/job ``env:`` mapping binds a URL just as a shell assignment does.
    # Shell assignments win (they are later at run time), so ``env`` fills only
    # names the shell left unbound.
    for name, value in env_literals(text).items():
        bound.setdefault(name, value)

    seen: set[str] = set()
    for number, verbatim, spaced in logical_lines(text):
        if not verbatim and not spaced:
            continue
        hit = None
        marker = _marker_hit(verbatim) or _marker_hit(spaced)
        if marker is not None:
            hit = "%s:%d %s" % (workflow_rel, number, marker)
        else:
            kinds = set()
            for line in (verbatim, spaced):
                kind = _http_kind(resolve(line, bound))
                if kind:
                    kinds.add(kind)
            if "http" in kinds:
                hit = "%s:%d HTTP write to a release path" % (workflow_rel, number)
            elif "gh" in kinds:
                hit = (
                    "%s:%d gh api body field writes a release path"
                    % (workflow_rel, number)
                )
        if hit is not None and hit not in seen:
            seen.add(hit)
            found.append(hit)

    for label, run in rendered_runs(text):
        resolved = "\n".join(strip_comment(l) for l in run.splitlines())
        for command in resolved.splitlines():
            command = resolve(command, bound)
            marker = _marker_hit(command)
            kind = _http_kind(command)
            if marker is not None:
                hit = "%s: step %r %s" % (workflow_rel, label, marker)
            elif kind == "http":
                hit = "%s: step %r HTTP write to a release path" % (
                    workflow_rel,
                    label,
                )
            elif kind == "gh":
                hit = "%s: step %r gh api body field writes a release path" % (
                    workflow_rel,
                    label,
                )
            else:
                continue
            if hit not in seen:
                seen.add(hit)
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

    try:
        import yaml  # noqa: F401, PLC0415 — the YAML pass needs a real parser
    except ImportError:
        print(
            "::error::ReleaseObjectWriterError: PyYAML is unavailable, so the "
            "folded-scalar pass cannot run and the check does not pass. Install "
            "PyYAML (the release workflow's test job already does).",
            file=sys.stderr,
        )
        return 2

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
