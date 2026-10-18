"""SW-1511A-HANDOFF-001 finding F-1511A-03 -- the shared secret rule set.

Review-001 (C3) found the shared scrubber
(:mod:`skillweave.redaction`, the single seam the trace layer and the stream
layer both import) did not mask an ``env``-file line such as
``PROVIDER_API_KEY=abc123``: the keyword alternative was word-boundary anchored,
so the ``_`` before ``KEY`` suppressed the match, and a bare provider key
(``sk-...``) with no ``bearer``/``key=`` keyword was missed entirely.  Two
handoff modules then consulted that same rule set for their credential-binding
admission, so the miss propagated there.

This file pins the **shared** rule set and the two consumers that delegate to
it.  On the base the rule set carries the anchored form and neither the
env-file line nor the bare ``sk-`` key is masked, so every masking assertion
below fails there.

No network, no provider, no credential: every value is a synthetic string.
"""

from __future__ import annotations

import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.redaction import (  # noqa: E402
    SECRET_MASK,
    contains_secret,
    scrub_secrets,
)
from skillweave.routing import upstream_observation as uo  # noqa: E402
from skillweave.runtime import controller_handoff as ch  # noqa: E402

#: The denominations the shared rule set must mask -- an ``env``-file line, a
#: keyworded pair, a bare provider key, a bearer token and a PEM block.
SECRETS = [
    "PROVIDER_API_KEY=abc123",
    "MY_TOKEN=zzz",
    "AUTH_TOKEN: hunter2",
    "sk-abcdefghijklmnop",
    "Authorization: Bearer abcdefghijklmnop",
    "-----BEGIN RSA PRIVATE KEY-----",
]

#: Non-secret bindings that must pass through: a gateway endpoint is a
#: *configuration reference*, not a credential, and masking it would break the
#: very handoff route the release carries.
NON_SECRETS = [
    "gateway://127.0.0.1:8090",
    "binding://faigate/route-alpha",
    "faigate/deepseek-v4",
]


# ── the shared seam ─────────────────────────────────────────────────────────


def test_the_shared_rule_masks_each_secret_denomination():
    for secret in SECRETS:
        assert contains_secret(secret), f"{secret!r} must be recognised"
        scrubbed = scrub_secrets(f"child said: {secret}")
        assert secret not in scrubbed
        assert SECRET_MASK in scrubbed


def test_the_shared_rule_leaves_a_non_secret_binding_untouched():
    for safe in NON_SECRETS:
        assert not contains_secret(safe), f"{safe!r} is a binding, not a secret"
        assert scrub_secrets(safe) == safe


# ── the two consumers that delegate to the shared rule ──────────────────────


def test_the_handoff_binding_check_refuses_the_env_file_line():
    """The credential-binding admission consults the shared rule, so an
    env-file line is refused rather than carried on a lease transfer."""
    assert ch.looks_like_secret("PROVIDER_API_KEY=abc123") is True
    assert ch.looks_like_secret("sk-abcdefghijklmnop") is True
    # The reference-shaped binding the release actually carries stays admitted.
    assert ch.looks_like_secret("gateway://127.0.0.1:8090") is False
    assert ch.looks_like_secret("binding://faigate/route-alpha") is False


def test_the_upstream_observation_check_refuses_the_env_file_line():
    """The drift copy in the routing layer delegates to the same rule set."""
    assert uo.looks_like_secret("PROVIDER_API_KEY=abc123") is True
    assert uo.looks_like_secret("sk-abcdefghijklmnop") is True
    assert uo.looks_like_secret("gateway://127.0.0.1:8090") is False
    assert uo.looks_like_secret("binding://faigate/route-alpha") is False
