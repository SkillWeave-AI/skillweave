"""Local empirical telemetry (SW-159-TELEMETRY-001).

A versioned, privacy-safe, **local-only** record of one execution's empirical
shape plus a coarse local recommender over a corpus of such records.

What a record represents (the brief's "required result"):

* **version points** — the commit/branch the execution pinned (:class:`VersionPoint`);
* **methodology / policy** — the methodology and autonomy policy in force
  (bound to :mod:`skillweave.runtime.methodology` and the Assay policy names);
* **starting budget** and turns used;
* **changes, retries, splits** and **LoC/AST deltas** (counts only — never the
  diff or the source);
* **failures** and the **intervals to PASS/HOLD** (:class:`GateOutcome`);
* the **model identity** as a catalogue id *or* a salted digest, plus capability
  and freshness (:class:`ModelIdentity`) — never a raw model name.

Privacy is fail-**closed**: unlike the masking boundary in
:mod:`skillweave.telemetry_intervention`, this contract *rejects* a record that
carries a prompt, source code, a secret, a personal identifier, or a direct
(local absolute / repos-relative) path. Nothing is masked into a record; a
contaminated record is refused outright. Model identity is allowed only in its
catalogue-id or salted-digest form.

Local-only: :class:`TelemetryStore` is disabled by default. Recording requires
an explicit ``enabled=True`` and export requires a separate, explicit
``export_consent=True``; without it there is no export path at all.

The recommender (:func:`recommend`) is deliberately coarse and transparent: it
reports its **sample size** and a **confidence** derived from the correlation
strength and the sample size, and it labels the sample ``insufficient`` /
``coarse`` / ``adequate`` rather than implying precision it does not have. It
uses only the standard library — no numpy, no scipy.

This module changes no Faigate routing and infers no production global service.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from .methodology import Methodology, RiskLevel, TopologyType

# ── Versioning ──────────────────────────────────────────────────────────────

#: The only local-telemetry record schema version that exists.
SCHEMA_VERSION = 1

#: The record kind tag, so a stored blob is self-describing.
TELEMETRY_KIND = "skillweave.local-telemetry"

#: Salt for the model-identity digest. A salted digest is stable within a
#: deployment and reveals nothing about the identity without the salt.
DIGEST_SALT = "skillweave-local-telemetry-v1"

#: Full 40-hex SHA, lowercase only (canonical).
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

#: A catalogue model id (e.g. ``faigate/deepseek-v4-pro``): a slash-joined,
#: lowercase token path with no leading root and no file extension.
_CATALOGUE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")

#: 64-hex sha256 digest.
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

#: ISO-8601-ish freshness stamp (date or date-time, ``Z`` or offset allowed).
_FRESHNESS = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


# ── Autonomy policy names (mirrors the Assay policy set) ───────────────────

class AssayPolicyName(str, Enum):
    """The three autonomy policies a record may declare."""

    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    UNICORN = "unicorn"


class GateOutcome(str, Enum):
    """The terminal outcome a record's interval is measured against.

    ``GATE_PASS`` and ``HOLD`` mirror the Assay terminal states (PASS/HOLD):
    a record carries an interval to exactly one of them.
    """

    GATE_PASS = "GATE_PASS"
    HOLD = "HOLD"


# ── Errors ──────────────────────────────────────────────────────────────────

class TelemetryError(ValueError):
    """Base error for the local-telemetry contract."""


class TelemetryPrivacyError(TelemetryError):
    """A record carried prohibited content (prompt, source, secret, PII, path)."""


class TelemetrySchemaError(TelemetryError):
    """A record violated the versioned schema (type, enum, required key)."""


class TelemetryTamperError(TelemetryError):
    """A record's recomputed digest does not match the digest it carries."""


class TelemetryDisabledError(TelemetryError):
    """Recording was attempted while local telemetry is disabled."""


class ExportConsentError(TelemetryError):
    """Export was attempted without explicit local export consent."""


# ── Privacy: fail-closed rejection of prompts/source/secrets/PII/paths ──────

#: Keys that name a secret or credential. Presence is refused outright.
_PRIVACY_BLOCKED_KEYS = frozenset({
    "api_key", "apikey", "token", "secret", "password", "passwd", "pwd",
    "credential", "credentials", "private_key", "access_key", "client_secret",
    "authorization", "auth", "bearer",
})

#: Keys that name a personal identifier.
_PII_BLOCKED_KEYS = frozenset({
    "user_id", "user_name", "username", "email", "e_mail", "phone",
    "phone_number", "ssn", "address", "ip", "ip_address", "hostname",
    "full_name", "first_name", "last_name", "author",
})

#: Keys that name a prompt or source payload. Presence is refused outright.
_PROMPT_SOURCE_BLOCKED_KEYS = frozenset({
    "prompt", "prompts", "source", "source_code", "code", "snippet", "diff",
    "patch", "content", "text", "body", "message", "messages", "stdout",
    "stderr", "output", "log", "logs", "trace", "transcript", "conversation",
    "raw", "raw_output", "file_contents", "contents",
})

#: Exactly the keys a record may carry. Anything else is refused, so there is no
#: place to smuggle a prompt, a path, or a secret under an unlisted key.
_ALLOWED_RECORD_KEYS = frozenset({
    "schema_version", "kind", "version_point", "methodology", "policy",
    "topology", "risk", "model_identity", "starting_budget", "turns_used",
    "changes", "retries", "splits", "loc_delta", "ast_delta", "failures",
    "interval_to_pass", "interval_to_hold", "outcome", "digest",
})

_ALLOWED_VERSION_POINT_KEYS = frozenset({"commit_sha", "branch", "base_sha"})
_ALLOWED_MODEL_IDENTITY_KEYS = frozenset({
    "catalogue_id", "salted_digest", "tier", "capability", "freshness",
})

#: An explicit secret denomination: a bearer token, a ``key=value``
#: credential, or a PEM block. These are caught by name because their shape is
#: otherwise ordinary prose.
_SECRET_RE = re.compile(
    r"(?i)"
    r"(?:bearer\s+[A-Za-z0-9._\-]{8,})"
    r"|(?:\b(?:api[_-]?key|secret|token|password|passwd|pwd|credential|auth)"
    r"\s*[:=]\s*\S+)"
    r"|(?:-----BEGIN[^-]*(?:PRIVATE|PUBLIC) KEY-----)"
)

#: A run of alphanumeric characters.
_RUN_RE = re.compile(r"[A-Za-z0-9]+")
#: Whether such a run is pure hexadecimal.
_HEX_RE = re.compile(r"[0-9a-fA-F]+")
#: The lower-case hex form (a commit SHA or a salted digest).
_LOWER_HEX_RE = re.compile(r"[0-9a-f]+")
#: A long unbroken run of digits — the shape of a bare account or card number.
_DIGIT_RUN_RE = re.compile(r"\d{12,}")
#: A run of digits broken by separators — the same shape once formatted. A date
#: (``2026-09-28``) or a version (``4.17.21``) carries fewer than ten digits; a
#: real account number carries ten or more.
_GROUPED_RUN_RE = re.compile(r"\d[\d \-.]*\d")
#: Total digits a separator-broken run must carry to be an account number
#: rather than a date or a version.
_GROUPED_DIGIT_MIN = 10

#: An alphanumeric run at least this long is opaque token or base64 material:
#: no telemetry label (branch component, catalogue namespace, tier, signal,
#: methodology) reaches it. The shortest known credential bodies (GitHub, npm,
#: PyPI, Slack, GitLab, Hugging Face, Shopify …) all exceed it.
_TOKEN_RUN_MIN = 16
#: A shorter run is *still* opaque when it bears a digit: every telemetry
#: label is either a pure word or a pure enum, so a digit-bearing
#: alphanumeric of this length is a token body regardless of case or any
#: prefix. This is what catches vendor tokens whose body is under the length
#: floor (``shpat_16c7e42f292c69``, ``SG.16C7e42F292c69``) without naming a
#: single vendor.
_DIGIT_TOKEN_RUN_MIN = 12
#: A git object id is the only lower-case hex telemetry legitimately carries:
#: an abbreviated SHA (7-12 characters, git's own default abbreviation) or a
#: full SHA-1 (40) or SHA-256 (64). Every other run that is hex but not
#: all-digits — including the 13-15 window between an abbreviated and a full
#: object id — is opaque token material (an HMAC, a token payload) and is
#: refused.
_DIGEST_HEX_LENS = frozenset({40, 64})
_SHORT_SHA_LENS = frozenset(range(7, 13))
#: A hex run longer than git's abbreviation ceiling is opaque even when it
#: *looks* like a word (an all-``[a-f]`` run such as ``bbbbbbbbbbbbb``): it is
#: longer than any abbreviated object id, so it is token material unless it is
#: a bare digest (checked before this floor).
_HEX_OPAQUE_MIN = 13
#: A telemetry value is a single token (at most a date and a time); three
#: words is prose, i.e. a prompt. Every field in the schema is an identifier,
#: a label, or a stamp — none has an internal space.
_MAX_WORDS = 2

#: Phrases that betray an instruction/prompt rather than a telemetry value.
_PROMPT_PHRASE_RE = re.compile(
    r"(?i)"
    r"(?:ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|earlier|above|preceding))"
    r"|(?:disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|earlier|above))"
    r"|(?:forget\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|earlier|above))"
    r"|(?:system\s+prompt)"
    r"|(?:you\s+are\s+(?:a\s+helpful|now|an\s+ai))"
    r"|(?:reveal\s+(?:the\s+)?(?:system|hidden|secret))"
    r"|(?:as\s+an\s+ai\s+(?:language\s+)?model)"
    r"|(?:pretend\s+you)"
    r"|(?:no\s+(?:safety|ethical)\s+(?:guidelines|restrictions))"
)


def _is_monotonic_filler(token: str) -> bool:
    """Whether ``token`` is a run of consecutive letters (``abcdefgh``).

    A strictly ascending or descending alphabet run is low-entropy filler —
    the shape of a placeholder key or a test token — and no real label is a
    monotonic sequence. Unlike a blanket "long lowercase run" rule, this never
    touches an ordinary word (``authentication`` is not monotonic).
    """
    if len(token) < 8 or not token.isalpha():
        return False
    lower = token.lower()
    deltas = {ord(b) - ord(a) for a, b in zip(lower, lower[1:])}
    return deltas in ({1}, {-1})


def _looks_like_a_label(token: str) -> bool:
    """Whether a short run has the shape of a telemetry label.

    A label is a word (a branch component, a tier, a signal) or a pure enum;
    a version tag is a word with digits. Two shapes are *not* labels: a
    digit-bearing run as long as a token body, and a monotonic alphabet run
    (``abcdefgh``), which is filler rather than a name. Both are refused
    without naming any vendor.
    """
    if _is_monotonic_filler(token):
        return False
    if not any(ch.isdigit() for ch in token):
        return True
    return len(token) < _DIGIT_TOKEN_RUN_MIN


def _is_bare_digest(token: str, whole: str) -> bool:
    """Whether ``token`` is the whole value and is exactly a digest shape.

    A bare digest is the one identifier long enough to resemble opaque token
    material that the schema nonetheless *requires* (a commit SHA or a salted
    digest), so it is exempted by exact shape — an abbreviated SHA, a full
    40-hex SHA, or a 64-hex digest. A run of pure digits is deliberately *not*
    exempt: it is an account/card-length number, not a digest, and stays
    subject to the digit-run rule even when it is also valid hex.
    """
    if token != whole:
        return False
    if _LOWER_HEX_RE.fullmatch(token) is None:
        return False
    if not any(ch.isalpha() for ch in token):
        return False
    return len(token) in _SHORT_SHA_LENS or len(token) in _DIGEST_HEX_LENS


def _looks_like_secret_shape(text: str) -> bool:
    """Whether ``text`` has the shape of a secret or an encoded blob.

    Telemetry values are short, structured identifiers. A denylist of known
    credential prefixes is never complete, so the boundary is stated as a
    *shape* rule instead: any opaque alphanumeric run too long to be a label,
    any hex run of token length that is not exactly a commit SHA or a salted
    digest, and any account-length digit run are refused. This admits every
    credential format — present and future — without naming any of them.

    A value that is *nothing but* a legitimate digest (a SHA or a salted
    digest) is returned clean before the digit-run rule, because the digit
    substrings of a digest are part of the digest, not an account number.
    """
    stripped = text.strip()
    if _is_bare_digest(stripped, stripped):
        return False
    for run in _RUN_RE.finditer(text):
        token = run.group(0)
        if _HEX_RE.fullmatch(token):
            if _is_bare_digest(token, text):
                continue
            if not _looks_like_a_label(token):
                return True
            if len(token) >= _HEX_OPAQUE_MIN:
                return True
        elif len(token) >= _TOKEN_RUN_MIN or not _looks_like_a_label(token):
            return True
    if _DIGIT_RUN_RE.search(text):
        return True
    for grouped in _GROUPED_RUN_RE.finditer(text):
        if sum(ch.isdigit() for ch in grouped.group(0)) >= _GROUPED_DIGIT_MIN:
            return True
    return False

#: An email address (a personal identifier).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: A US Social Security Number (a personal identifier), in the hyphenated or
#: dotted form (``123-45-6789``, ``123.45.6789``). The 3-2-4 grouping is what
#: distinguishes it from a version or date stamp (``2026.09.28`` is 4-2-2).
_SSN_RE = re.compile(r"\b\d{3}[-.]\d{2}[-.]\d{4}\b")

#: An IPv4 address (a personal identifier / host).
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

#: An IPv6 address (a personal identifier / host), in the full, compressed, or
#: mixed form. Written as the canonical grammar — a run of eight groups, or a
#: ``::`` elision at any position — so it admits ``2001:db8::1`` and
#: ``2001:db8:0:0:0:0:0:1`` while leaving a clock time such as ``00:00:00``
#: (three groups, no ``::``) untouched: no branch of this pattern matches a
#: bare three-group colon run.
_IPV6_RE = re.compile(
    r"(?i)"
    r"(?<![0-9a-f:])"
    r"(?:"
    r"(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,7}:"
    r"|(?:[0-9a-f]{1,4}:){1,6}:[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,5}(?::[0-9a-f]{1,4}){1,2}"
    r"|(?:[0-9a-f]{1,4}:){1,4}(?::[0-9a-f]{1,4}){1,3}"
    r"|(?:[0-9a-f]{1,4}:){1,3}(?::[0-9a-f]{1,4}){1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,2}(?::[0-9a-f]{1,4}){1,5}"
    r"|[0-9a-f]{1,4}:(?::[0-9a-f]{1,4}){1,6}"
    r"|:(?:(?::[0-9a-f]{1,4}){1,7}|:)"
    r")"
    r"(?![0-9a-f:])"
)

#: A phone number: an international (``+``-prefixed) number or a parenthesized
#: area-code form. Deliberately not a bare hyphenated digit run, which would
#: false-positive on an ISO date such as a ``freshness`` stamp.
_PHONE_RE = re.compile(
    r"(?:\+\d[\d\s().\-]{6,}\d|\(\d{3}\)\s?\d{3}[\s.\-]?\d{4})"
)

#: Unambiguous filesystem top-level names. Deliberately *not* generic words
#: (``app``, ``code``, ``data``, ``work``): those occur as ordinary branch
#: segments, while these name a system root in any context.
_ROOTED_SEGMENT = (
    r"(?:Users|home|root|private|Volumes|Applications|Library|"
    r"mnt|srv|opt|usr|etc|var|proc|sys|media|sbin|boot)"
)

#: The boundary a path segment starts at. A path never begins mid-word, so the
#: character before the leading ``/`` (or a tree head) must be a non-word
#: character — but *any* non-word character, not a hand-picked few: a comma, a
#: semicolon, or a bracket must not be able to smuggle a path past the check.
#: The two lookbehinds additionally cover a camelCase-joined prefix
#: (``aW/Users/...``) without opening the door to a lower-case branch slash
#: (``feature/run/tests``), which the root gate below must treat as a name.
_PATH_BOUNDARY = r"(?:^|(?<=[^A-Za-z0-9])|(?<=[a-z][A-Z]))"

#: A rooted system path appearing anywhere in the value. Two shapes qualify:
#: (a) a *leading* slash — at the start of the value or after any non-word
#: character — followed by at least one more segment separator, the absolute
#: form ``/Users/alice`` / ``/zzz/secret/file`` / after a comma:
#: ``a,/Users/alice/secret.txt``. A branch name has no leading slash (every
#: ``/`` in ``feature/SW-159/add-telemetry`` follows a word character), so it
#: is untouched; and (b) a *recognised* system root as a whole segment, which
#: catches a root glued to a lower-case prefix (``ab/Users/alice``) even when
#: its slash is not leading. Shape (b) keys on whole segments (``/Users/``,
#: never ``/Users`` inside ``/Userspace``), so a branch such as
#: ``feature/help`` is untouched. No punctuation can glue a root past (a).
_ROOTED_PATH_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9]))/(?:[A-Za-z0-9._~\-]+/)+"
    r"|/" + _ROOTED_SEGMENT + r"(?:/|$)"
    # A ``~``-rooted home reference, including the ``~user`` form: a ``~`` that
    # starts a value or follows a non-word character and is immediately
    # followed by a separator (``~/documents``) or by a user name and then a
    # separator (``~alice/secret``, ``~root/.ssh/id_rsa``). A ``~`` glued to a
    # preceding word (``a~/b``) is not a home reference and is left to the
    # other rules; the leading boundary is what keeps an ordinary label clean.
    r"|(?:^|(?<=[^A-Za-z0-9._\-]))~[A-Za-z0-9._\-]*[\\/]"
)

#: A path-leading separator: a ``/`` or ``\`` at the start of the value, or
#: after a character that *cannot* occur immediately before a git-ref segment
#: separator. The separator itself and ``.`` ``-`` ``_`` are ref-legal here
#: (``feature/SW-159/add-telemetry``, ``dependabot/npm_and_yarn/x``,
#: ``feature/x./y``), so they are excluded and a branch name stays clean; every
#: other leading context — a comma, semicolon, bracket, space, or the start of
#: the value — introduces an absolute path whatever follows it: the
#: multi-segment ``/etc/passwd``, the single-segment ``/secret.txt`` /
#: ``/.env`` / ``/x``, the doubled ``a//secret.txt``, and the same forms
#: embedded after punctuation or a space (``a,/secret.txt``,
#: ``wrote /secret.txt``). This closes the single-segment form that shape (a)
#: of :data:`_ROOTED_PATH_RE` — which requires a second separator — otherwise
#: admits. A traversal (``../../etc``) is *not* caught here (its separator
#: follows ``.``); :data:`_TRAVERSAL_RE` owns that shape alone.
_LEADING_SEPARATOR_RE = re.compile(r"(?:^|(?<=[^A-Za-z0-9._\-]))[\\/]")

#: A ``..`` traversal segment (``../../etc``, ``..\..\windows``). A bare ``..``
#: is only a traversal when a segment *follows* it — a ``..`` that ends the
#: value (the branch ``feature/..``) names nothing and is admitted.
_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/][^\s\"']+)+")

#: A repository-relative source path (``src/...``, ``tests/...``, ...). The
#: brief rejects "direct repo paths"; this catches the relative form (the
#: slash-absolute, drive-letter, and UNC forms are owned by
#: :data:`_LEADING_SEPARATOR_RE`; the ``~``-rooted form by
#: :data:`_ROOTED_PATH_RE`). A path is recognised by
#: its *shape* — a known source-tree head followed by at least one segment, or
#: any tail ending in a source-file extension — not by an arbitrary
#: ``head/label`` pair, which is also the shape of an ordinary branch name.
#: The heads are repo tree names (``src``, ``tests``, ``neutrality`` …), not
#: branch namespaces (``feature``, ``docs``, ``chore``, ``fix``), so
#: ``feature/sw-159-telemetry-001`` stays clean while ``neutrality/x`` does not.
_REPO_PATH_RE = re.compile(
    _PATH_BOUNDARY + r"(?:src|tests|scripts|schemas|examples|planning|"
    r"profiles|skills|references|pack|packs|fixtures|skillweave|neutrality)/"
    r"(?:[A-Za-z0-9._\-]+/)*[A-Za-z0-9._\-]+"
    r"|" + _PATH_BOUNDARY + r"(?:[A-Za-z0-9._\-]+/)+[A-Za-z0-9._\-]*"
    r"\.(?:py|md|json|ya?ml|toml|cfg|ini|sh|txt|rst)\b"
)

#: Markers that reveal inline source code or a prompt block. Statement
#: punctuation (``;``, braces, a spaced assignment) is code, not a field
#: value: no scheduler id, signal, or branch name carries it.
_SOURCE_MARKERS = (
    "def ", "class ", "import ", "from ", "function ", "pub fn ",
    "```", "#!/", "<?", "{\n", ");", "=>",
    ";", "{", "}", " = ", "== ",
)

#: A string longer than this is treated as prose/prompt, not a field value.
_MAX_VALUE_LEN = 400

#: Prohibited-content key groups, with the refusal label each reports.
_BLOCKED_KEY_GROUPS = (
    (_PRIVACY_BLOCKED_KEYS, "secret/credential"),
    (_PII_BLOCKED_KEYS, "personal identifier"),
    (_PROMPT_SOURCE_BLOCKED_KEYS, "prompt/source"),
)


def _looks_like_source_or_prompt(text: str) -> bool:
    """Whether ``text`` is prose or inline source rather than a field value.

    Telemetry values are short structured identifiers; a sentence, an
    instruction, or a code fragment is not. Prose is therefore refused by its
    *shape* (word count), which catches novel phrasings a phrase denylist would
    miss, and the phrase list catches terse injections that are not long enough
    to read as prose.
    """
    if len(text) > _MAX_VALUE_LEN:
        return True
    if "\n" in text or "\r" in text:
        return True
    if _PROMPT_PHRASE_RE.search(text):
        return True
    if len(text.split()) > _MAX_WORDS:
        return True
    return any(marker in text for marker in _SOURCE_MARKERS)


def _contains_path(text: str) -> bool:
    """Whether ``text`` carries an absolute, home-rooted, or repo path."""
    return bool(
        _LEADING_SEPARATOR_RE.search(text)
        or _TRAVERSAL_RE.search(text)
        or _ROOTED_PATH_RE.search(text)
        or _REPO_PATH_RE.search(text)
    )


def _assert_clean_value(path: str, value: str) -> None:
    """Refuse ``value`` if it carries prohibited content. Fail closed."""
    if _SECRET_RE.search(value) or _looks_like_secret_shape(value):
        raise TelemetryPrivacyError(
            f"telemetry field '{path}' carries secret material"
        )
    if _EMAIL_RE.search(value):
        raise TelemetryPrivacyError(
            f"telemetry field '{path}' carries a personal identifier (email)"
        )
    if (
        _SSN_RE.search(value)
        or _IPV4_RE.search(value)
        or _IPV6_RE.search(value)
        or _PHONE_RE.search(value)
    ):
        raise TelemetryPrivacyError(
            f"telemetry field '{path}' carries a personal identifier"
        )
    if _contains_path(value):
        raise TelemetryPrivacyError(
            f"telemetry field '{path}' carries a direct path"
        )
    if _looks_like_source_or_prompt(value):
        raise TelemetryPrivacyError(
            f"telemetry field '{path}' carries prompt or source content"
        )


def _scan_value(path: str, value: Any) -> None:
    """Walk ``value`` and refuse any prohibited string anywhere within it."""
    if isinstance(value, str):
        _assert_clean_value(path, value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            key_str = str(key)
            for blocked, label in _BLOCKED_KEY_GROUPS:
                if key_str.lower() in blocked:
                    raise TelemetryPrivacyError(
                        f"telemetry field '{path}.{key_str}' names a {label}"
                    )
            _scan_value(f"{path}.{key_str}", item)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_value(f"{path}[{index}]", item)


def scan_for_prohibited_content(mapping: Mapping[str, Any]) -> None:
    """Refuse a record that carries prompts, source, secrets, PII, or paths.

    Public entry point for the privacy boundary. Raises
    :class:`TelemetryPrivacyError` on the first violation.
    """
    for key, value in mapping.items():
        key_str = str(key)
        for blocked, label in _BLOCKED_KEY_GROUPS:
            if key_str.lower() in blocked:
                raise TelemetryPrivacyError(
                    f"telemetry key '{key_str}' names a {label}"
                )
        _scan_value(key_str, value)


# ── Identity ────────────────────────────────────────────────────────────────

def salted_digest(value: str, *, salt: str = DIGEST_SALT) -> str:
    """Return a deterministic salted sha256 digest of ``value``.

    Used to represent a model identity when no catalogue id is available: the
    digest is stable within a deployment and leaks nothing without the salt.
    """
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VersionPoint:
    """The pinned revision an execution ran against."""

    commit_sha: str
    branch: str = ""
    base_sha: str = ""

    def __post_init__(self) -> None:
        if not _FULL_SHA.match(self.commit_sha or ""):
            raise TelemetrySchemaError(
                f"version_point.commit_sha must be a full 40-hex sha, "
                f"got {self.commit_sha!r}"
            )
        if self.base_sha and not _FULL_SHA.match(self.base_sha):
            raise TelemetrySchemaError(
                f"version_point.base_sha must be a full 40-hex sha, "
                f"got {self.base_sha!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "branch": self.branch,
            "base_sha": self.base_sha,
        }


@dataclass(frozen=True)
class ModelIdentity:
    """A privacy-safe model identity: catalogue id **or** salted digest.

    At least one of ``catalogue_id`` / ``salted_digest`` must be present. The
    ``tier`` is a capability tier (``flash``/``pro``), ``capability`` an opaque
    capability label, and ``freshness`` the stamp at which the identity was
    resolved. A raw model name is never accepted.
    """

    catalogue_id: str = ""
    salted_digest: str = ""
    tier: str = "flash"
    capability: str = ""
    freshness: str = ""

    def __post_init__(self) -> None:
        if not self.catalogue_id and not self.salted_digest:
            raise TelemetrySchemaError(
                "model_identity needs a catalogue_id or a salted_digest"
            )
        if self.catalogue_id and not _CATALOGUE_ID.match(self.catalogue_id):
            raise TelemetrySchemaError(
                f"model_identity.catalogue_id is not a catalogue id: "
                f"{self.catalogue_id!r}"
            )
        if self.salted_digest and not _SHA256.match(self.salted_digest):
            raise TelemetrySchemaError(
                f"model_identity.salted_digest must be 64-hex sha256, "
                f"got {self.salted_digest!r}"
            )
        if self.tier not in ("flash", "pro"):
            raise TelemetrySchemaError(
                f"model_identity.tier must be 'flash' or 'pro', got {self.tier!r}"
            )
        if self.freshness and not _FRESHNESS.match(self.freshness):
            raise TelemetrySchemaError(
                f"model_identity.freshness is not an ISO timestamp: "
                f"{self.freshness!r}"
            )

    @classmethod
    def from_digest(
        cls, raw_identity: str, *, tier: str = "flash",
        capability: str = "", freshness: str = "",
    ) -> "ModelIdentity":
        """Build an identity from a raw name via a salted digest."""
        return cls(
            salted_digest=salted_digest(raw_identity),
            tier=tier,
            capability=capability,
            freshness=freshness,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalogue_id": self.catalogue_id,
            "salted_digest": self.salted_digest,
            "tier": self.tier,
            "capability": self.capability,
            "freshness": self.freshness,
        }


# ── The record ──────────────────────────────────────────────────────────────

def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_digest(record: Mapping[str, Any]) -> str:
    """Return the sha256 digest of ``record`` with any ``digest`` key excluded."""
    payload = {k: v for k, v in record.items() if k != "digest"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TelemetrySchemaError(f"{label} must be a non-negative integer")
    return value


@dataclass
class LocalTelemetryRecord:
    """One versioned, privacy-safe local telemetry observation.

    Counts and deltas only: ``changes``/``retries``/``splits``/``failures`` are
    integers, ``loc_delta``/``ast_delta`` are integer deltas (a negative
    ``ast_delta`` records an AST regression, e.g. a parse failure). The interval
    to the terminal outcome is recorded in ``interval_to_pass`` (for
    ``GATE_PASS``) or ``interval_to_hold`` (for ``HOLD``).
    """

    version_point: VersionPoint
    methodology: str
    policy: str
    model_identity: ModelIdentity
    topology: str = TopologyType.SEQUENTIAL.value
    risk: str = RiskLevel.LOW.value
    starting_budget: int = 0
    turns_used: int = 0
    changes: int = 0
    retries: int = 0
    splits: int = 0
    loc_delta: int = 0
    ast_delta: int = 0
    failures: int = 0
    interval_to_pass: Optional[int] = None
    interval_to_hold: Optional[int] = None
    outcome: str = GateOutcome.HOLD.value
    schema_version: int = SCHEMA_VERSION
    kind: str = TELEMETRY_KIND

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise TelemetrySchemaError(
                f"unsupported schema_version {self.schema_version!r} "
                f"(expected {SCHEMA_VERSION})"
            )
        if self.kind != TELEMETRY_KIND:
            raise TelemetrySchemaError(f"unknown telemetry kind {self.kind!r}")
        if self.methodology not in {m.value for m in Methodology}:
            raise TelemetrySchemaError(
                f"unknown methodology {self.methodology!r}"
            )
        if self.policy not in {p.value for p in AssayPolicyName}:
            raise TelemetrySchemaError(f"unknown policy {self.policy!r}")
        if self.topology not in {t.value for t in TopologyType}:
            raise TelemetrySchemaError(f"unknown topology {self.topology!r}")
        if self.risk not in {r.value for r in RiskLevel}:
            raise TelemetrySchemaError(f"unknown risk {self.risk!r}")
        if self.outcome not in {o.value for o in GateOutcome}:
            raise TelemetrySchemaError(f"unknown outcome {self.outcome!r}")
        for name in (
            "starting_budget", "turns_used", "changes", "retries", "splits",
            "loc_delta", "ast_delta", "failures",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TelemetrySchemaError(f"{name} must be an integer")
            if value < 0 and name not in ("loc_delta", "ast_delta"):
                raise TelemetrySchemaError(f"{name} must not be negative")
        for name in ("interval_to_pass", "interval_to_hold"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise TelemetrySchemaError(
                    f"{name} must be a non-negative integer or absent"
                )
        if self.outcome == GateOutcome.GATE_PASS.value and self.interval_to_pass is None:
            raise TelemetrySchemaError(
                "a GATE_PASS outcome must record interval_to_pass"
            )
        if self.outcome == GateOutcome.HOLD.value and self.interval_to_hold is None:
            raise TelemetrySchemaError(
                "a HOLD outcome must record interval_to_hold"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "version_point": self.version_point.to_dict(),
            "methodology": self.methodology,
            "policy": self.policy,
            "topology": self.topology,
            "risk": self.risk,
            "model_identity": self.model_identity.to_dict(),
            "starting_budget": self.starting_budget,
            "turns_used": self.turns_used,
            "changes": self.changes,
            "retries": self.retries,
            "splits": self.splits,
            "loc_delta": self.loc_delta,
            "ast_delta": self.ast_delta,
            "failures": self.failures,
            "interval_to_pass": self.interval_to_pass,
            "interval_to_hold": self.interval_to_hold,
            "outcome": self.outcome,
        }

    def to_dict(self, *, seal: bool = True) -> dict[str, Any]:
        """Return the record as a dict, privacy-scanned and (by default) sealed."""
        payload = self._payload()
        scan_for_prohibited_content(payload)
        if seal:
            payload = dict(payload)
            payload["digest"] = compute_digest(payload)
        return payload

    def seal(self) -> dict[str, Any]:
        """Return the sealed dict form of this record."""
        return self.to_dict(seal=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LocalTelemetryRecord":
        """Rebuild and validate a record from its dict form."""
        validate_record(data)
        vp = data["version_point"]
        mi = data["model_identity"]
        return cls(
            version_point=VersionPoint(
                commit_sha=vp["commit_sha"],
                branch=vp.get("branch", ""),
                base_sha=vp.get("base_sha", ""),
            ),
            methodology=data["methodology"],
            policy=data["policy"],
            model_identity=ModelIdentity(
                catalogue_id=mi.get("catalogue_id", ""),
                salted_digest=mi.get("salted_digest", ""),
                tier=mi.get("tier", "flash"),
                capability=mi.get("capability", ""),
                freshness=mi.get("freshness", ""),
            ),
            topology=data.get("topology", TopologyType.SEQUENTIAL.value),
            risk=data.get("risk", RiskLevel.LOW.value),
            starting_budget=data.get("starting_budget", 0),
            turns_used=data.get("turns_used", 0),
            changes=data.get("changes", 0),
            retries=data.get("retries", 0),
            splits=data.get("splits", 0),
            loc_delta=data.get("loc_delta", 0),
            ast_delta=data.get("ast_delta", 0),
            failures=data.get("failures", 0),
            interval_to_pass=data.get("interval_to_pass"),
            interval_to_hold=data.get("interval_to_hold"),
            outcome=data.get("outcome", GateOutcome.HOLD.value),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            kind=data.get("kind", TELEMETRY_KIND),
        )


# ── Schema validation (fail-closed, stdlib only) ───────────────────────────

def _require_plain_int(value: Any, label: str, *, allow_negative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelemetrySchemaError(f"{label} must be an integer")
    if not allow_negative and value < 0:
        raise TelemetrySchemaError(f"{label} must not be negative")


def validate_record(record: Mapping[str, Any]) -> None:
    """Validate a record dict against the versioned schema. Fail closed.

    Checks: only allowed keys, every required field's presence *and* value
    (each required key has a downstream type/enum/shape test, so consigning
    presence to the same place the value is judged keeps the two from drifting
    apart), enum values, integer fields, model-identity resolution,
    version-point sha shape, the outcome/interval pairing, the privacy
    boundary, and the content digest.
    """
    if not isinstance(record, Mapping):
        raise TelemetrySchemaError("record must be a mapping")

    unknown = sorted(k for k in record if k not in _ALLOWED_RECORD_KEYS)
    if unknown:
        raise TelemetrySchemaError(
            f"record carries unknown key(s) {unknown}; "
            f"only {sorted(_ALLOWED_RECORD_KEYS)} are allowed"
        )

    if record.get("schema_version") != SCHEMA_VERSION:
        raise TelemetrySchemaError(
            f"unsupported schema_version {record.get('schema_version')!r}"
        )
    if record.get("kind") != TELEMETRY_KIND:
        raise TelemetrySchemaError(f"unknown telemetry kind {record.get('kind')!r}")

    if record.get("methodology") not in {m.value for m in Methodology}:
        raise TelemetrySchemaError(f"unknown methodology {record.get('methodology')!r}")
    if record.get("policy") not in {p.value for p in AssayPolicyName}:
        raise TelemetrySchemaError(f"unknown policy {record.get('policy')!r}")
    if record.get("topology") not in {t.value for t in TopologyType}:
        raise TelemetrySchemaError(f"unknown topology {record.get('topology')!r}")
    if record.get("risk") not in {r.value for r in RiskLevel}:
        raise TelemetrySchemaError(f"unknown risk {record.get('risk')!r}")
    if record.get("outcome") not in {o.value for o in GateOutcome}:
        raise TelemetrySchemaError(f"unknown outcome {record.get('outcome')!r}")

    vp = record.get("version_point")
    if not isinstance(vp, Mapping):
        raise TelemetrySchemaError("version_point must be a mapping")
    vp_unknown = sorted(k for k in vp if k not in _ALLOWED_VERSION_POINT_KEYS)
    if vp_unknown:
        raise TelemetrySchemaError(f"version_point carries unknown key(s) {vp_unknown}")
    if not _FULL_SHA.match(str(vp.get("commit_sha", ""))):
        raise TelemetrySchemaError("version_point.commit_sha must be a full 40-hex sha")

    mi = record.get("model_identity")
    if not isinstance(mi, Mapping):
        raise TelemetrySchemaError("model_identity must be a mapping")
    mi_unknown = sorted(k for k in mi if k not in _ALLOWED_MODEL_IDENTITY_KEYS)
    if mi_unknown:
        raise TelemetrySchemaError(f"model_identity carries unknown key(s) {mi_unknown}")
    if not mi.get("catalogue_id") and not mi.get("salted_digest"):
        raise TelemetrySchemaError(
            "model_identity needs a catalogue_id or a salted_digest"
        )

    for name in (
        "starting_budget", "turns_used", "changes", "retries", "splits",
        "failures",
    ):
        _require_plain_int(record.get(name), name)
    for name in ("loc_delta", "ast_delta"):
        _require_plain_int(record.get(name), name, allow_negative=True)
    for name in ("interval_to_pass", "interval_to_hold"):
        value = record.get(name)
        if value is not None:
            _require_plain_int(value, name)

    if record.get("outcome") == GateOutcome.GATE_PASS.value and record.get("interval_to_pass") is None:
        raise TelemetrySchemaError("a GATE_PASS outcome must record interval_to_pass")
    if record.get("outcome") == GateOutcome.HOLD.value and record.get("interval_to_hold") is None:
        raise TelemetrySchemaError("a HOLD outcome must record interval_to_hold")

    scan_for_prohibited_content(record)

    carried = record.get("digest")
    if carried is None:
        raise TelemetrySchemaError("record is not sealed (missing digest)")
    if carried != compute_digest(record):
        raise TelemetryTamperError("record digest does not match its payload")


# ── Local-only store with explicit consent gates ───────────────────────────

@dataclass
class TelemetryStore:
    """A local-only telemetry store, disabled until explicitly enabled.

    * Recording requires ``enabled=True``; the default store is disabled and
      :meth:`record` raises :class:`TelemetryDisabledError`.
    * Export requires ``export_consent=True``; without it :meth:`export` raises
      :class:`ExportConsentError` — there is no export path at all.

    The store keeps records in memory only. It never writes to a network, a
    remote service, or a shared export location of its own accord.
    """

    enabled: bool = False
    export_consent: bool = False
    _records: list[dict[str, Any]] = field(default_factory=list)

    def record(self, entry: LocalTelemetryRecord) -> dict[str, Any]:
        """Record one entry, if local telemetry is enabled."""
        if not self.enabled:
            raise TelemetryDisabledError(
                "local telemetry is disabled; enable it explicitly to record"
            )
        sealed = entry.seal()
        self._records.append(sealed)
        return sealed

    @property
    def is_enabled(self) -> bool:
        return self.enabled

    @property
    def has_export_consent(self) -> bool:
        return self.export_consent

    def records(self) -> list[dict[str, Any]]:
        """Return a copy of the recorded entries."""
        return [dict(r) for r in self._records]

    def __len__(self) -> int:
        return len(self._records)

    def export(self) -> list[dict[str, Any]]:
        """Export recorded entries — only with explicit export consent."""
        if not self.export_consent:
            raise ExportConsentError(
                "export requires explicit export consent; none was granted"
            )
        return self.records()

    def recommend(self, *, metric: str = "outcome", factor: str = "") -> "Recommendation":
        """Return a coarse recommendation over the recorded corpus."""
        return recommend(self._records, metric=metric, factor=factor or None)


# ── Coarse local recommender ───────────────────────────────────────────────

#: Below this many samples a correlation is labelled ``insufficient``.
COARSE_MIN_SAMPLE = 4

#: At or above this many samples the label becomes ``adequate``.
ADEQUATE_SAMPLE = 8

#: The candidate numeric factors the recommender correlates against an outcome.
CANDIDATE_FACTORS: tuple[str, ...] = (
    "starting_budget", "turns_used", "changes", "retries", "splits",
    "loc_delta", "ast_delta", "failures",
)


def _sample_label(n: int) -> str:
    if n < COARSE_MIN_SAMPLE:
        return "insufficient"
    if n < ADEQUATE_SAMPLE:
        return "coarse"
    return "adequate"


def pearson(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    """Return the Pearson correlation of ``pairs``, or ``None`` if undefined.

    ``None`` is returned when there are fewer than two pairs or either series
    has zero variance — never a fabricated 0.0.
    """
    n = len(pairs)
    if n < 2:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / ((sxx ** 0.5) * (syy ** 0.5))


def _outcome_series(records: Sequence[Mapping[str, Any]]) -> list[float]:
    return [
        1.0 if r.get("outcome") == GateOutcome.GATE_PASS.value else 0.0
        for r in records
    ]


def _confidence(r: Optional[float], n: int) -> float:
    """Coarse confidence from correlation strength and sample size.

    ``|r|`` scaled by how close ``n`` is to :data:`ADEQUATE_SAMPLE`, clipped to
    ``[0, 1]``. Transparent and deliberately conservative.
    """
    if r is None or n < 2:
        return 0.0
    sample_factor = min(1.0, n / ADEQUATE_SAMPLE)
    return round(min(1.0, abs(r)) * sample_factor, 3)


@dataclass(frozen=True)
class Recommendation:
    """A coarse, transparent recommendation with its evidence."""

    metric: str
    factor: str
    direction: str
    correlation: Optional[float]
    sample_size: int
    sample_label: str
    confidence: float
    pass_rate: float
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "factor": self.factor,
            "direction": self.direction,
            "correlation": self.correlation,
            "sample_size": self.sample_size,
            "sample_label": self.sample_label,
            "confidence": self.confidence,
            "pass_rate": self.pass_rate,
            "detail": self.detail,
        }


def _direction(r: Optional[float]) -> str:
    if r is None:
        return "none"
    if r >= 0.3:
        return "positive"
    if r <= -0.3:
        return "negative"
    return "flat"


def recommend(
    records: Sequence[Mapping[str, Any]],
    *,
    metric: str = "outcome",
    factor: Optional[str] = None,
) -> Recommendation:
    """Return a coarse recommendation over ``records``.

    With ``metric="outcome"`` the recommender correlates each candidate factor
    against the binary pass outcome and returns the factor with the strongest
    absolute correlation, with its sample size, sample label, and confidence.
    With ``metric="interval_to_pass"`` it correlates factors against the
    interval to ``GATE_PASS``. A ``factor`` may be named to score just that one.

    This is a *local, coarse* recommender: it reports its sample size and
    confidence rather than asserting a causal claim.
    """
    rows = [r for r in records if isinstance(r, Mapping)]
    n = len(rows)
    pass_rate = (
        sum(_outcome_series(rows)) / n if n else 0.0
    )

    if metric == "interval_to_pass":
        target = "interval_to_pass"
    else:
        target = "outcome"

    factors = (factor,) if factor else CANDIDATE_FACTORS
    best: Optional[tuple[float, str, Optional[float], int]] = None
    for name in factors:
        pairs: list[tuple[float, float]] = []
        for r in rows:
            x = r.get(name)
            if not isinstance(x, (int, float)) or isinstance(x, bool):
                continue
            if target == "outcome":
                y: Optional[float] = (
                    1.0 if r.get("outcome") == GateOutcome.GATE_PASS.value else 0.0
                )
            else:
                y = r.get("interval_to_pass")
                if not isinstance(y, (int, float)) or isinstance(y, bool):
                    continue
            pairs.append((float(x), float(y)))
        r_value = pearson(pairs)
        if r_value is None:
            continue
        score = abs(r_value)
        if best is None or score > best[0]:
            best = (score, name, r_value, len(pairs))

    if best is None:
        return Recommendation(
            metric=metric, factor=factor or "", direction="none",
            correlation=None, sample_size=0, sample_label="insufficient",
            confidence=0.0, pass_rate=round(pass_rate, 4),
            detail="no factor had enough varying, numeric observations",
        )

    _score, name, r_value, used = best
    label = _sample_label(used)
    return Recommendation(
        metric=metric, factor=name, direction=_direction(r_value),
        correlation=round(r_value, 4) if r_value is not None else None,
        sample_size=used, sample_label=label,
        confidence=_confidence(r_value, used), pass_rate=round(pass_rate, 4),
        detail=(
            f"factor '{name}' correlates {_direction(r_value)} "
            f"(r={round(r_value, 4)}) with '{metric}' over {used} samples "
            f"({label}); overall pass rate {round(pass_rate, 4)}"
        ),
    )


def summarise(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a coarse local summary of a corpus of telemetry records."""
    rows = [r for r in records if isinstance(r, Mapping)]
    n = len(rows)
    passes = sum(_outcome_series(rows))
    intervals = [
        r["interval_to_pass"] for r in rows
        if isinstance(r.get("interval_to_pass"), int)
        and not isinstance(r.get("interval_to_pass"), bool)
    ]
    return {
        "sample_size": n,
        "sample_label": _sample_label(n),
        "pass_count": int(passes),
        "pass_rate": round(passes / n, 4) if n else 0.0,
        "mean_interval_to_pass": (
            round(sum(intervals) / len(intervals), 4) if intervals else None
        ),
        "recommendation": recommend(rows).to_dict(),
    }


__all__ = [
    "SCHEMA_VERSION",
    "TELEMETRY_KIND",
    "DIGEST_SALT",
    "AssayPolicyName",
    "GateOutcome",
    "TelemetryError",
    "TelemetryPrivacyError",
    "TelemetrySchemaError",
    "TelemetryTamperError",
    "TelemetryDisabledError",
    "ExportConsentError",
    "salted_digest",
    "VersionPoint",
    "ModelIdentity",
    "LocalTelemetryRecord",
    "compute_digest",
    "scan_for_prohibited_content",
    "validate_record",
    "TelemetryStore",
    "COARSE_MIN_SAMPLE",
    "ADEQUATE_SAMPLE",
    "CANDIDATE_FACTORS",
    "pearson",
    "Recommendation",
    "recommend",
    "summarise",
]
