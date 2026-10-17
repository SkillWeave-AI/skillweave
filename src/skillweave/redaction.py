"""The single secret-scrubbing seam shared by the stream and trace layers.

SW-1511A-RESULT-001 (finding F-1511A-01) preserves the child's closing message
independently of stream retention. That message is child-authored text, so it
can carry a credential the child printed. Wherever that text is handed to a
human-readable artifact -- the stream store's excerpt read-out and the
final-result store's durable sibling and record -- it is scrubbed, so a
credential the child printed never lands in the record or beside it. (The
stream store's own spool stays raw evidence, exactly as its retained prefix
does.)

Those two ends live in different subpackages and neither may import the other
(``skillweave.trace`` keeps no import onto ``skillweave.runtime``, and the
runtime keeps no import onto the trace layer). This module is the shared,
dependency-light source of truth both import, so a secret is masked by exactly
one rule set rather than two copies that can drift.

The denominations restate those already named by
:mod:`skillweave.telemetry_intervention` (bearer tokens, ``key=value``
credential pairs, PEM private-key blocks). That module cannot be reused here:
it imports ``skillweave.dispatch``, which this layer must stay clear of.
"""

from __future__ import annotations

import re

__all__ = ["SECRET_MASK", "scrub_secrets", "contains_secret"]

#: The mask written in place of a secret-shaped value.
SECRET_MASK = "[REDACTED_SECRET]"

#: The secret denominations scrubbed from a captured message. Case-insensitive:
#: a secret denomination is a secret regardless of the case the child used.
#:
#: SW-1511A-HANDOFF-001 finding F-1511A-03: an ``env``-file line such as
#: ``PROVIDER_API_KEY=abc123`` is a credential the child can print, and a bare
#: ``sk-...`` prefix is a provider key with no ``bearer``/``key=`` keyword at
#: all. The keyword alternative therefore admits an **identifier-character
#: prefix/suffix** (``PROVIDER_API_KEY``, ``MY_TOKEN``, ``AUTH_TOKEN``) -- the
#: word-boundary form the rule carried before let the ``_`` before the keyword
#: suppress the match, so the env-file line was never masked. A ``sk-``
#: alternative covers the provider-style key a harness can echo verbatim.
#: Deliberately *not* a general ``[:=]\S+`` rule: that would mask every
#: ``scheme://host`` reference (``gateway://127.0.0.1:8090``), which is a
#: non-secret binding, not a credential.
SECRET_RE = re.compile(
    r"(?i)"
    r"(?:bearer\s+[A-Za-z0-9._\-]{8,})"
    r"|(?<![\w.\-])[A-Za-z0-9_\-]*"
    r"(?:api[_-]?key|secret|token|password|passwd|pwd|credential|auth)"
    r"[A-Za-z0-9_\-]*\s*[:=]\s*\S+"
    r"|(?:\bsk-[A-Za-z0-9_\-]{8,})"
    r"|(?:-----BEGIN[^-]*PRIVATE KEY-----)"
)


def scrub_secrets(text: str) -> str:
    """Return ``text`` with every secret-shaped denomination masked.

    Clean text passes through verbatim; only the unsafe shapes are rewritten.
    """
    if not text:
        return text
    return SECRET_RE.sub(SECRET_MASK, text)


def contains_secret(text: str) -> bool:
    """Return whether ``text`` still carries an unmasked secret denomination."""
    return bool(text) and SECRET_RE.search(text) is not None
