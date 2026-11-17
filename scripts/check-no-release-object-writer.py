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

curl's short options are read as curl reads them, not as letters in a word: a
cluster such as ``-sSd``, ``-sSX POST``, ``-sSXPOST`` or ``-sG`` is walked left
to right, the first letter that takes an argument (``d``/``T``/``F``/``X``/
``G``/``H``/…) owns the rest of the token or the next word, and later letters
are that argument. So a bundled method or forced-GET is recognised in both its
detached and attached form, while ``-fsSL`` stays four boolean flags;
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
downstream one (``curl … | tar -T -`` is a download, not an upload). Those
separators are read *quote-aware*: a ``;``, ``|``, ``&`` or newline inside a
quoted argument — a header value ``-H 'X: a;b'``, a pretty-printed request
body ``-d '{\\n … }'``, a URL with a query string — is data and neither ends
the command early nor starts a new one, and an option's value is taken as one
word, so ``-H 'X: -G'`` is a header value rather than curl's forced-GET flag.
A command substitution is opaque for the same reason: ``$(...)``, ``<( ... )``
and backticks are evaluated by the shell and handed to curl as one argument, so
``curl -d $(echo '{"tag_name":"v1"}'; true) URL`` sends a POST even though the
outer command has no unquoted ``;``. The substitution's ``;``, ``|``, ``&`` and
newlines belong to the argument, and the outer command is kept intact.
That is what keeps the body flag and the release URL in the same command for
inspection rather than hiding one behind the other. Literal ``NAME=value``
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

A here-document is read the way the shell reads it: the body belongs to the
command whose line carries the ``<<`` operator, so a request line in
``nc host 443 <<'EOF'`` is that command's input rather than a statement of its
own, and ``cat <<'EOF' | nc host 443`` keeps its pipeline whole for the same
reason. A substitution whose result is written down (``$(echo -d)``) is
resolved to that word, so a flag spelled indirectly is still seen; any other
substitution stays opaque.

A raw TCP write that spells the HTTP request as bytes is modelled when the
request line is written down: a ``POST``/``PATCH``/``PUT``/``DELETE`` line
addressing ``/releases`` piped into ``nc``/``ncat``/``netcat``/``socat``/
``openssl s_client``, a socket tool given a port, or a ``/dev/tcp`` redirect is
refused. Sender and listener are told apart by the tool's own arguments, so
``| nc -l`` (clustered or not) cannot reach the forge and is not refused, and a
listener later on a pipeline does not switch off an earlier sender. The request
*target* is what is tested, so a write to another endpoint that merely quotes
``/releases`` in its body is not a release write. ``gh api``'s endpoint is read
from its first bare argument, so a relative endpoint is seen the same as an
absolute one. For ``curl``/``wget`` a target may omit the scheme — ``curl -d …
a.b/x`` sends to ``http://a.b/x`` — so a host-shaped word (a dotted name or
``localhost``, optionally with a port) followed by a path is a target too; the
word must look like a host and the client must be a fetcher, so a relative
path quoted in a body and a non-fetcher's ``host/path`` argument are not
targets. ``socat``'s address may carry options after a comma
(``TCP:host:port,connect-timeout=5``) and ``OPENSSL:host:port`` is the same
outbound sender over TLS; the ``-LISTEN`` option marks a listener in either
spelling.

One shape is deliberately over-reported: a socket sender whose payload is read
from a *file* (``openssl s_client ... < payload.http``). The bytes are not
written down, so classifying the command as a read would be a guess; a gate
reports it instead. What stays out of reach is the same class as any other
computed target — a request line or path assembled at run time, or a writer
fetched and executed. These limits are stated rather than claimed closed, and
the ``env:``/folded-scalar/raw-request-line cases above are *not* among them:
the target there is written down verbatim in the workflow.

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
# A bash `/dev/tcp` redirect is the fourth transport. It is inspected at file
# scope as well as per line, because an fd can be opened on one line
# (`exec 3<>/dev/tcp/host/443`) and written on the next (`printf ... >&3`).
_DEV_TCP = re.compile(r"/dev/(?:tcp|udp)/")
_SOCKET_TOOLS = frozenset(("nc", "ncat", "netcat", "socat"))

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
# item (`http URL tag_name=v1` is a POST). curl's *short* options are not
# tested by a letter-class regex and cannot be: a cluster such as ``-sSd``,
# ``-dx`` or ``-sSX POST`` is read in curl's own order, where the first letter
# that takes an argument owns the rest of the token, and every later letter is
# that argument rather than a flag. A pattern like ``-[A-Za-z]*[dTF]`` both
# misses the detached form (``-sSX POST``) and misreads an attached value that
# happens to contain ``d``/``T``/``F``. ``_curl_signals`` walks the option
# table instead. The curl segment stops at a pipe, `;` or `&&`, so a
# downstream ``tar -T -`` is not read as an upload.
# A quoted span is opaque to an operator: a `;`, `|`, `&` or newline inside it
# is data, not shell syntax, and a word that merely *looks* like a flag
# (`-H 'X-Note: -G'`) is a value. Every scan that walked to "the end of the
# command" with `[^|;&]*` was quote-blind, so a header value or request body
# containing one of those characters ended the segment early and hid the body
# flag and the URL from the check while the shell executed a real POST. These
# fragments step over a whole quoted span (newlines included) while still
# stopping at an unquoted operator, and the plain-character alternative
# excludes quotes so an unmatched-looking `'` cannot be walked through one
# character at a time.
#
# A command substitution is opaque to the outer command for the same reason a
# quoted span is: the shell runs ``$(...)`` and hands its result to curl as one
# argument, so a ``;``, ``|`` or newline *inside* it is part of the argument and
# not a command boundary. ``curl -d $(echo '{"tag_name":"v1"}'; true) URL``
# sends a POST even though the outer line has no unquoted ``;``. The
# substitution alternatives therefore sit beside the quoted ones, before the
# plain-character alternative can walk into them.
_SUBSTITUTION = r"(?:\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`|[<>]\((?:[^()]|\([^()]*\))*\))"
_QUOTED_SPAN = (
    r"(?:'[^']*'"
    r"|\"(?:\\.|[^\"\\])*\""
    r"|" + _SUBSTITUTION +
    r"|[^|;&'\"\n])"
)
_HTTP_BODY = re.compile(
    r"(?:"
    r"\bcurl\b" + _QUOTED_SPAN + r"*?(?:"
    r"--data(?:-(?:raw|binary|urlencode|ascii))?(?:\s|=)"
    r"|--form(?:-string)?(?:\s|=)"
    r"|--upload-file(?:\s|=)"
    r"|--json(?:\s|=)"
    r")"
    # GNU wget's `--body-data`/`--body-file` are documented aliases of
    # `--post-data`/`--post-file` and send the same POST.
    r"|\bwget\b" + _QUOTED_SPAN + r"*?--(?:post|body)-(?:data|file)(?:\s|=)"
    r"|\burlopen\s*\([^)]*?\bdata\s*="
    # httpie/xh infer a POST from a body item, with optional flag options
    # before the URL. A body item is `name=value` (a query parameter is
    # `name==value` and is not a body), raw JSON `name:=value` (httpie/xh
    # default to `application/json`), or a file field `name@path`. A bare
    # `=` must not match the `==` query form, hence the lookahead.
    r"|(?:^|[\s;&|])(?:https?|xh)(?:\s+--?[A-Za-z0-9_-]+)*\s+"
    + _QUOTED_SPAN + r"*?\b[A-Za-z_][A-Za-z0-9_-]*(?:=(?!=)|:=|@)"
    r")"
)
# An explicit *read* method: the body signal must not turn it into a write.
# `gh api --method GET .../releases -f per_page=100` is documented usage for a
# filtered read, not a release write. Only the body signal is suppressed; an
# explicit write method still counts. curl's own read spellings (``-G``,
# ``--get``, ``-X GET``) are read by ``_curl_signals`` from the option table
# instead, because — unlike ``gh api --method`` — they can be bundled and their
# arguments can look like flags. The GET must be inside the client's own
# segment: a GET for one URL must not mask a body-only POST after `;`, `&&`,
# `|` or a newline on the same line.
_READ_METHOD = re.compile(
    r"\bgh(?:\s+api)\b" + _QUOTED_SPAN + r"*?"
    r"(?:-X|--request|--method|-method)\s*=?\s*['\"]?GET\b",
    re.IGNORECASE,
)
# A client invocation starts at the command's head or after `;`, `&&`/`||`, a
# pipe or a subshell `(`. The read-method suppression only applies when the
# command carries one invocation: with two, the GET may belong to the first and
# the body signal to the second (`curl -X GET ...; curl -d ... .../releases`),
# so suppressing command-wide would hide a write behind an unrelated read. The
# scan runs on an operator view split by ``_command_segments``, so a `;` or `|`
# inside a quoted argument does not start a new command and a quoted `-G` does
# not appear as a flag.
_CLIENT_START = re.compile(
    r"(?:^|[;|&(\n])\s*(?:curl|wget|gh|https?|xh)\b"
)
# One curl invocation: from the command word to the end of the segment, which
# ``_command_segments`` already cut at the unquoted pipes, `;` and `&&`. The
# quoted-span fragment keeps a ``;`` or ``|`` inside a header value or body
# from ending the segment early.
_CURL_SEGMENT = re.compile(r"\bcurl\b" + _QUOTED_SPAN + r"*")
# curl's short options that take an argument (``-d``, ``-T``, ``-F``, ``-X``,
# ``-H``, ``-o``, ...). The first such letter in a cluster owns the rest of the
# token as its argument, so later letters are data, not flags. The write and
# body letters are in this set, which is what makes ``-sSd`` a body and
# ``-sSXPOST`` a write while ``-fsSL`` is four booleans.
_CURL_SHORT_WITH_ARG = frozenset(
    "AbcCdDeEFHKmoPQrtTuUwxXyYz"
)
# The long options whose value is the next word when no `=` joins them. Their
# value is data too: ``curl --data -G url`` posts the string ``-G``.
_CURL_LONG_WITH_ARG = frozenset(
    (
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--data-ascii",
        "--form",
        "--form-string",
        "--upload-file",
        "--json",
        "--request",
        "--header",
        "--output",
        "--user",
        "--url",
    )
)
_WRITE_VERBS = frozenset(("POST", "PATCH", "PUT", "DELETE"))
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


def _strip_line_carry(line: str, quote: str) -> tuple[str, str]:
    """Strip one physical line's comment, carrying the quote state in and out.

    A ``#`` starts a comment only when it begins a word (line start, after
    whitespace, or after an operator such as ``;``/``&``/``|``/``(``); inside
    quotes or inside a word it is literal. The *opening* quote may sit on an
    earlier physical line: a newline inside a quote is just a character of the
    quoted word, so the quote state is passed from line to line. Everything
    from a comment's ``#`` to the end of the physical line goes — including a
    trailing backslash, which the shell does not treat as a continuation once
    a comment has begun.
    """
    out: list[str] = []
    escaped = False
    previous = ""
    for char in line:
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            previous = char
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            previous = char
            continue
        if char == "#" and (not out or previous in " \t;&|()<>"):
            break
        out.append(char)
        previous = char
    return "".join(out), quote


def strip_comment(line: str) -> str:
    """Remove a shell comment from one physical line, quote-aware."""
    return _strip_line_carry(line, "")[0]


def strip_comments(text: str) -> str:
    """Remove shell comments from a block, carrying quote state across lines.

    A newline inside a quoted string is not a line end to the shell, so a ``#``
    on the next physical line is still inside the quote and is literal. The
    per-line ``strip_comment`` cannot see that; this one can, which is what
    keeps a multi-line quoted request body intact for inspection.
    """
    quote = ""
    kept: list[str] = []
    for line in text.split("\n"):
        stripped, quote = _strip_line_carry(line, quote)
        kept.append(stripped)
    return "\n".join(kept)


def _heredoc_operator(text: str, index: int) -> tuple[int, str, bool] | None:
    """Parse a ``<<``/``<<-`` redirection operator starting at ``index``.

    Returns ``(delimiter_start, delimiter, strip_tabs)`` — the index just past
    the operator and any ``-``, the delimiter word with its quoting removed,
    and whether the ``<<-`` form strips leading tabs. ``None`` when the
    characters at ``index`` are not a here-document operator (``<<<`` is a
    here-*string*, whose payload is written on the command itself, and ``<<``
    inside a longer token such as ``2<<3`` is arithmetic).
    """
    if not text.startswith("<<", index) or text.startswith("<<<", index):
        return None
    cursor = index + 2
    strip_tabs = False
    if cursor < len(text) and text[cursor] == "-":
        strip_tabs = True
        cursor += 1
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    start = cursor
    delimiter: list[str] = []
    quote = ""
    while cursor < len(text):
        char = text[cursor]
        if quote:
            if char == quote:
                quote = ""
            else:
                delimiter.append(char)
        elif char in ("'", '"'):
            quote = char
        elif char.isspace() or char in ";&|<>()":
            break
        elif char == "\\" and cursor + 1 < len(text):
            cursor += 1
            delimiter.append(text[cursor])
        else:
            delimiter.append(char)
        cursor += 1
    word = "".join(delimiter)
    if not word:
        return None
    return start, word, strip_tabs


def _heredoc_body_end(
    text: str, start: int, delimiter: str, strip_tabs: bool
) -> tuple[int, int]:
    """Locate a here-document body that begins at ``start``.

    Returns ``(body_end, next_line_start)``: the index where the body text
    stops (the delimiter line) and the index of the line after the delimiter,
    which is where the command that owns the here-document continues. The
    shell collects the body from the lines *after* the whole command line, so
    the caller passes the end of that line as ``start``. When the delimiter is
    never found, the end of the text is returned.
    """
    cursor = start
    while cursor <= len(text):
        newline = text.find("\n", cursor)
        line_end = len(text) if newline == -1 else newline
        line = text[cursor:line_end]
        if strip_tabs:
            line = line.lstrip("\t")
        if line == delimiter:
            return cursor, len(text) if newline == -1 else newline + 1
        if newline == -1:
            return len(text), len(text)
        cursor = newline + 1
    return len(text), len(text)


def _heredocs_in(text: str) -> list[tuple[str, bool]]:
    """Every here-document operator on one command line, in order.

    Quote-aware: ``echo 'a << b'`` opens nothing. ``<<<`` is a here-*string*
    and is skipped, exactly as ``_heredoc_operator`` does.
    """
    ops: list[tuple[str, bool]] = []
    index = 0
    quote = ""
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == "<":
            parsed = _heredoc_operator(text, index)
            if parsed is not None:
                ops.append((parsed[1], parsed[2]))
                index += 2
                continue
        index += 1
    return ops


def _substitution_end(text: str, index: int) -> int:
    """End index (exclusive) of a substitution starting at ``index``, else -1.

    ``$(...)``, ``<( ... )``/``>( ... )`` and backticks are parsed to their own
    closing delimiter, quote-aware and with nested ``(``/``)`` counted, so the
    characters inside — including ``;``, ``|``, ``&`` and newlines — are not
    mistaken for syntax of the command that contains the substitution.
    """
    if text.startswith("$(", index) or text.startswith("<(", index) or text.startswith(">(", index):
        opening = index + 1
        depth = 0
        quote = ""
        cursor = opening
        while cursor < len(text):
            char = text[cursor]
            if quote:
                if char == "\\" and quote == '"' and cursor + 1 < len(text):
                    cursor += 2
                    continue
                if char == quote:
                    quote = ""
                cursor += 1
                continue
            if char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return cursor + 1
            cursor += 1
        return -1
    if text.startswith("`", index):
        cursor = index + 1
        while cursor < len(text):
            if text[cursor] == "\\" and cursor + 1 < len(text):
                cursor += 2
                continue
            if text[cursor] == "`":
                return cursor + 1
            cursor += 1
        return -1
    return -1


def _split_by_pipe_real(text: str) -> list[str]:
    """Split ``text`` on the ``|`` characters the shell itself sees.

    Quote-aware and substitution-aware: ``-H 'X: a|b'`` and
    ``$(a | b)`` carry no pipeline break. ``||`` and ``|&`` are command
    separators too and split the same way, at the operator's start.
    """
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(text):
                index += 1
                current.append(text[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        if char in "$`<>":
            end = _substitution_end(text, index)
            if end != -1:
                current.append(text[index:end])
                index = end
                continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            index += 1
            if index < len(text) and text[index] in "|&":
                index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _command_segments(text: str) -> list[str]:
    """Split a shell block into commands, quote-aware.

    A ``;``, ``|``, ``&`` or newline ends a command only when it is unquoted:
    the operators inside ``-d '{"a":"b;c"}'`` or ``-H 'X: a|b'`` belong to the
    argument, not to the shell. A quoted newline is part of the argument too,
    so a pretty-printed request body stays attached to the command that sends
    it. A backslash-newline is deleted with no separator, exactly as the shell
    does it. Each returned segment is one command, which is what makes a body
    flag in one command attributable to that command alone.

    A here-document is read the way the shell reads it: the body is the block
    of lines after the command line, and it is the *input* of that command, so
    it is appended to the command that opens it rather than parsed as commands
    of its own. The pipeline that reads it is kept whole for the same reason —
    ``cat <<'EOF' | nc host 443`` puts the request line in the body and the
    transport in the pipeline, and they only describe one write together.
    """
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(text):
                index += 1
                current.append(text[index])
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        if char in ("$", "`", "<", ">"):
            end = _substitution_end(text, index)
            if end != -1:
                current.append(text[index:end])
                index = end
                continue
        if char == "\\" and index + 1 < len(text):
            if text[index + 1] == "\n":
                index += 2
                continue
            current.append(char)
            current.append(text[index + 1])
            index += 2
            continue
        if char == "\n":
            line = "".join(current)
            bodies: list[str] = []
            next_line = index + 1
            for delimiter, strip_tabs in _heredocs_in(line):
                body_end, next_line = _heredoc_body_end(
                    text, next_line, delimiter, strip_tabs
                )
                bodies.append(text[index + 1:body_end])
            if bodies and "|" in line:
                current = [line, "\n", "\n".join(bodies)]
                index = next_line
                continue
            if bodies:
                segment = (line + "\n" + "\n".join(bodies)).strip()
                if segment:
                    segments.append(segment)
                current = []
                index = next_line
                continue
        if char in ";|&\n":
            # A command line that opens a here-document keeps its pipeline
            # together: the body is the stdin of the last command in it, and
            # `cat <<'EOF' | nc host 443` only describes one write as a whole.
            if _heredocs_in("".join(current)):
                current.append(char)
                index += 1
                continue
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _literal_substitution(span: str) -> str | None:
    """The literal result of a substitution that is just ``echo WORD``.

    ``$(echo -d)`` is evaluated by the shell and handed to curl as the word
    ``-d``, so a flag can be written where no flag is visible. Only a single
    ``echo`` of one quote-free, expansion-free word is modelled; anything
    else stays opaque, and an unknown result is not silently read as a read.
    """
    inner = span
    if inner.startswith("$(") and inner.endswith(")"):
        inner = inner[2:-1]
    elif inner.startswith("`") and inner.endswith("`"):
        inner = inner[1:-1]
    else:
        return None
    words = inner.split()
    if len(words) != 2 or words[0] != "echo":
        return None
    value = words[1].strip("\"'")
    if not value or any(char in value for char in "$`\\<>|&;()"):
        return None
    return value


def _shell_words(text: str) -> list[str]:
    """The words of one command, quote-aware, with quote characters removed.

    A quoted span is one word even when it contains whitespace or an operator:
    ``-H 'X-Note: -G'`` yields the two words ``-H`` and ``X-Note: -G``, so the
    header value cannot look like a curl flag. An attached quoted value
    (``-d'{...}'``) stays one word with the flag prefix.
    """
    words: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if quote:
            if char == "\\" and quote == '"' and index < len(text):
                current.append(text[index])
                index += 1
            elif char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char in ("$", "`", "<", ">"):
            end = _substitution_end(text, index - 1)
            if end != -1:
                span = text[index - 1:end]
                literal = _literal_substitution(span)
                current.append(literal if literal is not None else span)
                index = end
                continue
        if char.isspace():
            if current:
                words.append("".join(current))
                current = []
            continue
        if char == "\\" and index < len(text):
            current.append(text[index])
            index += 1
            continue
        current.append(char)
    if current:
        words.append("".join(current))
    return words


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

    The verbatim view keeps trailing whitespace: a backslash is a continuation
    only when it is the last *character* of the physical line. The spaced view
    strips, so the YAML indentation yields the ordinary separated spelling.

    Comments are stripped *before* folding, one physical line at a time, with
    the quote state carried across lines **and across continuations** exactly
    as the shell carries it. Folding first would let a ``#`` comment absorb the
    following line — its trailing backslash folds into the comment, the result
    starts with ``#`` and is skipped — while the shell ends the comment at the
    newline and executes that following line. Carrying the quote state is what
    keeps a ``#`` on the line after a still-open quote literal instead of
    mistaken for a comment. A folded command is reported from the physical
    line it begins on.
    """
    folded: list[tuple[int, str, str]] = []
    pending: list[tuple[str, str]] = []
    start = 0

    def joined(parts: list[tuple[str, str]], which: int) -> str:
        return "".join(p[which] for p in parts if p[which])

    quote = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped, quote = _strip_line_carry(raw, quote)
        verbatim = stripped.rstrip()
        spaced = stripped.strip()
        if not pending:
            start = number
        if verbatim.endswith("\\"):
            pending.append((verbatim[:-1], spaced[:-1]))
            continue
        pending.append((verbatim, spaced))
        folded.append((start, joined(pending, 0), joined(pending, 1)))
        pending = []
    if pending:
        folded.append((start, joined(pending, 0), joined(pending, 1)))
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


_SHELL_TOKEN = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|[^\s]+")
# A request *target* is an absolute URL or a path of at least two segments.
# Searching for ``/releases`` anywhere in the command would read a request
# *body* that merely mentions it — `-d '{"body":"see /releases"}'` — as
# addressing a release path, which is a false positive on every issue/comment
# write that quotes the endpoint.
_URL_SPAN = re.compile(r"(?:https?://|//)[^\s'\"`]+")
_PATH_SPAN = re.compile(
    r"(?:^|[\s'\"`=:(])(/[A-Za-z0-9._~%\-]+(?:/[A-Za-z0-9._~%\-]+)+)"
    r"(?=[\s'\"`),;]|$)"
)
# curl and wget both accept a URL with the scheme left off: `curl -d … a.b/x`
# sends to `http://a.b/x`. The host must look like a host (a dotted name or
# `localhost`, optionally with a port) so a relative path merely quoted in a
# request body — `see repos/o/r/releases` — is not read as a target.
_BARE_URL_SPAN = re.compile(
    r"(?:^|[\s'\"`=:(])"
    r"((?:localhost|(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+)"
    r"(?::\d{1,5})?/[^\s'\"`),;]+)"
)
# The clients that accept a scheme-less URL as a target.
_FETCHER = re.compile(r"(?:^|\s)(?:curl|wget)(?:\s|$)")
# The request target of a raw HTTP request line is the first token after the
# method. Reading the target rather than the whole line keeps a body or a
# comment that mentions ``/releases`` from satisfying the path test. Any method
# is captured: a visible ``GET`` request line says the command is a read. A
# method at the start of a quoted payload counts, so the tokens are split on
# quote characters as well as whitespace.
_REQUEST_LINE = re.compile(
    r"(?:^|[\s'\"`])(GET|HEAD|OPTIONS|POST|PATCH|PUT|DELETE)\s+(\S+)",
    re.IGNORECASE,
)
_QUOTED_TOKEN = re.compile(
    r"'([^'\n]*)'|\"([^\"\n]*)\"|([^\s'\"]+)"
)
# A command assembled as an argv list — ``subprocess.run(["gh","release",
# "create","v1"])`` — spells the CLI verb with JSON punctuation between the
# words, so the plain ``gh release create`` marker never occurs. Only the
# punctuation between tokens is normalised; quoted strings stay words.
_ARGV_PUNCTUATION = re.compile(r"[\[\](),]")
_GH_RELEASE_SPLIT = re.compile(
    r"\bgh\s+release\s+(?:create|edit|delete|upload)\b"
)
# socat's address forms: `TCP:host:port` sends, `TCP-LISTEN:port` listens. The
# address may carry options after a comma (`TCP:h:443,connect-timeout=5`),
# and `OPENSSL:host:port` is the same sender wrapped in TLS.
_SOCAT_SEND = re.compile(
    r"^(?:TCP|UDP)(?:4|6)?:[^\s,:]*:\d{1,5}(?:,[^\s]*)?$"
    r"|^OPENSSL(?:[46])?:[^\s,:]*:\d{1,5}(?:,[^\s]*)?$"
)
_SOCAT_LISTEN = re.compile(r"-LISTEN", re.IGNORECASE)
_HOST_PORT = re.compile(r"^[^\s:]+:\d{1,5}$")
_SOCKET_SHORT_WITH_ARG = frozenset("bcDeEimMoOqstTvVwXx")
_SOCKET_LONG_WITH_ARG = frozenset(
    ("--exec", "--idle-timeout", "--interval", "--max-conns", "--output",
     "--source", "--source-port", "--tcp", "--timeout", "--udp")
)


def _shell_tokens(line: str) -> list[str]:
    """The words of ``line``, with quote characters removed.

    A quoted payload (``printf 'POST /releases HTTP/1.1'``) is split on its
    own whitespace, so the request line inside it becomes the words a reader
    sees. Every word ends up bare.
    """
    tokens: list[str] = []
    for match in _QUOTED_TOKEN.finditer(line):
        part = next(g for g in match.groups() if g is not None)
        tokens.extend(part.split())
    return tokens


def _release_spans(line: str) -> list[str]:
    """Every URL or multi-segment path on ``line`` that addresses ``/releases``."""
    spans = [s for s in _URL_SPAN.findall(line) if _REST_PATH.search(s)]
    spans.extend(s for s in _PATH_SPAN.findall(line) if _REST_PATH.search(s))
    # A scheme-less host/path is a target too, but only worth naming when the
    # line is a fetcher's: `docker pull a.b/c` is not an HTTP request, and the
    # write signal is required separately anyway.
    if _FETCHER.search(" ".join(_shell_tokens(line))):
        spans.extend(s for s in _BARE_URL_SPAN.findall(line) if _REST_PATH.search(s))
    return spans


def _release_targets(line: str) -> bool:
    """Whether an argument of ``line`` is a URL/path addressing ``/releases``."""
    return bool(_release_spans(line))


# ``gh api`` takes its endpoint as the first bare argument after the subcommand,
# and an endpoint may be spelled relative (``repos/o/r/releases``) just as often
# as absolute. Reading the endpoint argument — rather than any path-looking word
# on the line — keeps a field value that quotes the path out of the target test.
_GH_API_OPTION_WITH_VALUE = frozenset((
    "-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header",
    "-q", "--jq", "-p", "--preview", "--input", "-t", "--template",
    "--hostname", "--cache",
))


def _gh_api_endpoint(line: str) -> str | None:
    tokens = _shell_tokens(line)
    for index, token in enumerate(tokens):
        if token.rsplit("/", 1)[-1] != "gh" or tokens[index + 1:index + 2] != ["api"]:
            continue
        skip = False
        for arg in tokens[index + 2:]:
            if skip:
                skip = False
                continue
            if arg.startswith("-"):
                option = arg.split("=", 1)[0]
                if "=" not in arg and option in _GH_API_OPTION_WITH_VALUE:
                    skip = True
                continue
            return arg
    return None


def _gh_api_addresses_release(line: str) -> bool:
    endpoint = _gh_api_endpoint(line)
    return endpoint is not None and _REST_PATH.search(endpoint) is not None


def _request_target(line: str) -> str | None:
    """The target of a raw HTTP request line on ``line``, if there is one."""
    text = " ".join(_shell_tokens(line))
    match = _REQUEST_LINE.search(text)
    if match:
        return match.group(2)
    return None


def _raw_request_is_write(line: str) -> bool:
    """A raw request line carries a write method, not a read one.

    ``printf 'GET ...' | nc h 443`` sends a *read* over the socket; only a
    ``POST``/``PATCH``/``PUT``/``DELETE`` request line is a release write.
    """
    text = " ".join(_shell_tokens(line))
    match = _REQUEST_LINE.search(text)
    return bool(match) and match.group(1).upper() in _WRITE_VERBS


def _socket_send(line: str) -> bool:
    """Whether ``line`` hands data to a socket *sender*.

    A pipe into ``nc``/``ncat``/``netcat``/``socat`` with a host and a port, a
    socket tool addressed with ``host:port``, ``openssl s_client -connect``, or
    a ``/dev/tcp`` redirect. A listener (``nc -l``, ``nc -lp``, ``nc -lk``,
    ``socat TCP-LISTEN:``) receives and cannot reach the forge, so it is not a
    sender.
    """
    if _DEV_TCP.search(line):
        return True
    for segment in _split_by_pipe_real(line):
        tokens = [t.strip("\"'") for t in _shell_tokens(segment)]
        if not tokens:
            continue
        name = tokens[0].rsplit("/", 1)[-1]
        args = tokens[1:]
        if name == "openssl":
            if (
                "s_client" in args
                and any(
                    a == "-connect" or a.startswith("-connect=") for a in args
                )
            ):
                return True
            continue
        if name not in _SOCKET_TOOLS:
            continue
        if _socket_address_sends(args):
            return True
    return False


def _socket_address_sends(args: list[str]) -> bool:
    """Whether a socket tool's arguments name a remote peer to send to."""
    listen = False
    host = False
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if _SOCAT_LISTEN.search(token):
            listen = True
            continue
        if token in ("-l", "--listen") or token.startswith("--listen="):
            listen = True
            continue
        if token.startswith("--"):
            option = token.split("=", 1)[0]
            if option in _SOCKET_LONG_WITH_ARG and "=" not in token:
                index += 1
            continue
        if token.startswith("-") and token != "-":
            rest = token[1:]
            for offset, char in enumerate(rest):
                if char == "l":
                    listen = True
                    continue
                if char in _SOCKET_SHORT_WITH_ARG:
                    # The rest of the cluster is this option's value.
                    if offset + 1 == len(rest):
                        index += 1
                    break
            continue
        if _SOCAT_SEND.match(token):
            return not listen
        if _HOST_PORT.match(token):
            return not listen
        if token.isdigit() and host:
            return not listen
        host = True
    return False


# A stdin redirect from a file (`openssl s_client ... < payload.http`). The
# `3<>/dev/tcp/...` fd-open is not a redirect of stdin, `<<<` is a here-string
# whose payload is written on the command line, `<&` duplicates an fd, and
# `<<` is a here-document whose body is parsed by ``_heredocs_in`` and kept
# with the command that reads it — none of those names a file this pattern
# should report.
_FILE_REDIRECT = re.compile(r"(?<!<)<(?!<|>|&)\s*([^\s;&|<>]+)")


def _opaque_raw_write(*views: str) -> str | None:
    """A socket sender fed a payload from a file, so its content is invisible.

    A request line written down in the workflow is classified exactly (a
    ``GET`` is a read, a ``POST`` is a write). A payload read from a *file* is
    not written down: the workflow names a path whose bytes the check cannot
    read. Certifying such a command as a read would be a guess, so it is
    reported — the same "over-reporting is the safe direction for a gate"
    rule the folded-line views follow. ``/dev/null`` is empty and exempt.
    """
    for view in views:
        if _request_target(view) is not None or not _socket_send(view):
            continue
        for match in _FILE_REDIRECT.finditer(view):
            path = match.group(1)
            if path != "/dev/null":
                return path
    return None


def _raw_request_target(*views: str) -> str | None:
    """A raw request target that a socket transport on the same command sends.

    The request line and the transport may sit on different physical lines of
    one folded command; both views carry the whole command, so any view that
    has a target and a sender names the write.
    """
    for view in views:
        target = _request_target(view)
        if (
            target is not None
            and _release_targets(target)
            and _raw_request_is_write(view)
            and _socket_send(view)
        ):
            return target
    return None


def _marker_hit(line: str) -> str | None:
    """The writer marker on a command, if any — name, CLI verb or client call."""
    for marker in WRITER_MARKERS:
        if marker in line:
            return marker
    # A command name quoted as a word (`"$CMD" release create`) or spelled as
    # an argv list puts punctuation between the verb's words; normalise only
    # those separators so the marker is still seen.
    spaced = _ARGV_PUNCTUATION.sub(" ", line.replace('"', " ").replace("'", " "))
    match = _GH_RELEASE_SPLIT.search(spaced)
    if match:
        return match.group(0).strip()
    match = _TEA_WRITE.search(line)
    if match:
        return match.group(0).strip()
    match = _DOTTED_WRITER.search(line)
    if match:
        return match.group(0).rstrip("( ").strip()
    return None


def _curl_signals(line: str) -> tuple[bool, bool, bool]:
    """``(method_write, body_write, read)`` from curl's short-option clusters.

    curl reads a cluster such as ``-sSX`` left to right: a boolean letter is a
    flag, the first letter that takes an argument owns the rest of the token
    and — if the token ends there — the next word as its value. So ``-sSd`` is
    a body, ``-sSXPOST`` and ``-sSX POST`` name a method, ``-sSXGET`` is a
    read, and ``-fsSL`` is four booleans. A letter class cannot model this:
    ``-[A-Za-z]*[dTF]`` misses the detached ``-sSX POST`` and matches an
    attached value that merely contains ``d``/``T``/``F``.

    The value of an option is data, never another option: ``curl -d -G url``
    posts the string ``-G`` and ``curl --data -X POST url`` posts the string
    ``-X POST``, so a value word is consumed and not scanned. The words are
    taken quote-aware, so a header value that spells ``-X GET`` or ``-G`` is
    one word (the value of ``-H``) and never a flag of its own.
    """
    method_write = body_write = read = False
    for segment in _CURL_SEGMENT.findall(line):
        words = _shell_words(segment)
        index = 0
        while index < len(words):
            token = words[index]
            index += 1
            if token.startswith("--"):
                option, has_value = token.split("=", 1)[0], "=" in token
                if option == "--get":
                    read = True
                if not has_value and option in _CURL_LONG_WITH_ARG:
                    # The next word is this option's value — data, not flags.
                    index += 1
                continue
            if not token.startswith("-") or token == "-":
                continue
            rest = token[1:]
            while rest:
                char = rest[0]
                rest = rest[1:]
                if char == "G":
                    read = True
                    continue
                if char in _CURL_SHORT_WITH_ARG:
                    if char in "dTF":
                        body_write = True
                    elif char == "X":
                        value = rest
                        if not value:
                            value = words[index] if index < len(words) else ""
                            index += 1
                        if value.upper() in _WRITE_VERBS:
                            method_write = True
                        elif value.upper() == "GET":
                            read = True
                        break
                    # The value of this option is the rest of the cluster or,
                    # when the cluster ends here, the next word. Either way the
                    # value is data and is not scanned for flags.
                    if not rest:
                        index += 1
                    break
    return method_write, body_write, read


def _read_suppresses(line: str) -> bool:
    """Whether an explicit GET may switch off the body signal for ``line``.

    Only for a command carrying a single client invocation: the body signal and
    the GET must belong to the same command for a GET to explain the body away.
    ``line`` is one segment, so a `;` or `|` inside a quoted argument cannot
    make one command look like two.
    """
    return (
        (_READ_METHOD.search(line) is not None or _curl_signals(line)[2])
        and len(_CLIENT_START.findall(line)) <= 1
    )


def _http_kind(line: str, *extra_views: str) -> str | None:
    """Classify a resolved command that addresses a release path.

    Returns ``"http"`` for a method token or request body, ``"gh"`` for a
    ``gh api`` body field, and ``None`` for a read or an unrelated line.

    ``line`` is split into commands quote-aware and each is classified on its
    own: a ``;``, ``|``, ``&`` or newline inside a quoted header value or
    request body is data, so it neither ends the command early (hiding the body
    flag and URL behind it) nor starts a new one (letting an unrelated read
    explain a write away).

    ``extra_views`` are other renderings of the same command (the folded
    command's verbatim and spaced views). A raw request line and its socket
    transport may be split between them, so the raw-socket test sees every
    view; every other test runs on the commands of ``line``, the view being
    classified.
    """
    if _opaque_raw_write(line, *extra_views) is not None:
        # A socket sender fed a payload this check cannot read. Whether it is a
        # write is unknowable, so it is reported rather than certified a read.
        return "http"
    if not (
        _release_targets(line)
        or _gh_api_addresses_release(line)
        or any(
            _release_targets(view) or _gh_api_addresses_release(view)
            for view in extra_views
        )
        or _raw_request_target(line, *extra_views) is not None
    ):
        return None
    # A raw-socket write: the HTTP request line is data, and a socket sender
    # carries it. `printf 'POST .../releases ...' | nc h 443`, the same line
    # through `openssl s_client -connect`, or a `/dev/tcp` redirect.
    if _raw_request_target(line, *extra_views) is not None:
        return "http"
    gh_body = False
    for command in _command_segments(line):
        if not (
            _release_targets(command) or _gh_api_addresses_release(command)
        ):
            continue
        method_write, body_write, _ = _curl_signals(command)
        if (
            _HTTP_METHOD.search(command) is not None
            or method_write
            # A curl `-G` turns the body into a query string: it is a read, so
            # the body signal is suppressed there. An explicit write method
            # still counts.
            or (
                (_HTTP_BODY.search(command) is not None or body_write)
                and not _read_suppresses(command)
            )
        ):
            return "http"
        if "gh api" in command and _GH_API_BODY.search(command):
            # `gh api --method GET .../releases -f per_page=100` turns the
            # fields into a query string: a read. An explicit write method
            # still counts.
            if not _read_suppresses(command):
                gh_body = True
    return "gh" if gh_body else None


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
    bound = literals(strip_comments(text))
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
        resolved = (resolve(verbatim, bound), resolve(spaced, bound))
        # A literal variable can hold the command name as well as the URL
        # (`CMD=gh; "$CMD" release create v1`), so the marker is tested after
        # substitution too.
        marker = _marker_hit(verbatim) or _marker_hit(spaced)
        if marker is None:
            marker = _marker_hit(resolved[0]) or _marker_hit(resolved[1])
        if marker is not None:
            hit = "%s:%d %s" % (workflow_rel, number, marker)
        else:
            kinds = set()
            # Both views of the one folded command are passed so a request
            # line and its socket transport split across them are seen as one
            # write. A raw request line is compared against its *masked* view
            # so a quoted `;`/`|`/`&`/newline cannot split the transport from
            # the request line it carries; the body/method scans on a segment
            # are quote-aware through ``_command_segments``.
            for line, *others in (resolved, resolved[::-1]):
                kind = _http_kind(line, *others)
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
        whole = strip_comments(run)
        # A `/dev/tcp` fd can be opened on one line of a step's block and
        # written on the next. The request line and the transport are then in
        # different commands, so the whole step block is the second view: a
        # request line in a block whose any line opens a socket is a write.
        block_writes = bool(_DEV_TCP.search(whole)) and bool(
            _raw_request_target(whole)
        )
        for command in _command_segments(whole):
            command = resolve(command, bound)
            marker = _marker_hit(command)
            kind = _http_kind(command)
            if (
                kind is None
                and block_writes
                and _raw_request_is_write(whole)
                and _request_target(command) is not None
                and _release_targets(" ".join(_shell_tokens(command)))
            ):
                kind = "http"
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
