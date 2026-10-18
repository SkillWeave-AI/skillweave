"""SW-1511A-GATE-001 repair G -- validate the issued principal *as it is issued*.

The live HANDOFF-C3 run (finding F5) found that the server's **own** principal
was refused as a handoff destination whenever the supervisor identity contained
colons.  :func:`skillweave.mcp.server.startup_and_bind_stdio` mints the stdio
session by concatenation -- ``f"mcp:{role}:{client_id}"`` where ``client_id`` is
the *verbatim* supervisor identity -- so the real operator identity
``supervisor:andrelange:claude-code`` mints
``mcp:controller:supervisor:andrelange:claude-code``.  The pre-repair
``_validate_destination_principal`` admitted only a three-part split
(``len(parts) == 3``), so it mis-refused that minted value with
``PRINCIPAL_NOT_SERVER_ISSUED`` and the four transitions were unreachable for
the server's own bound session.

The repair parses exactly the minting grammar (``split(":", 2)``: the role is
the segment after ``mcp:`` and must be in
:data:`~skillweave.runtime.authority.MCP_ROLE_SCOPES`; the remainder is the
supervisor identity, which may itself contain colons and must be non-empty and
free of whitespace/control characters), and **prefers an exact comparison** with
the principal the server actually issued for its bound session over any shape
matching.

This file is the adversarial proof, driven entirely through:
  * the **real stdio-bound authority** --
    :func:`skillweave.mcp.server.startup_and_bind_stdio` mints the session and
    :data:`_READY_SESSION_TOKEN` resolves it (no hand-written token, no direct
    module call for the identity); and
  * the **real MCP handler** --
    :func:`skillweave.mcp.server.handoff_adapter` (the production boundary),
    on in-process fakes only: no network, no provider, no credential.

On the pre-repair head the colon-bearing case (the lifecycle test and the
identity-over-shape test) fails, because the three-part split refuses the
server's own minted principal; here the same real tool calls admit it and the
lease moves.

Every test uses a per-test temporary root (its own store and mailbox), so a
re-run is a re-run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime import controller_handoff as ch  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

#: The real live operator identity called out by the F5 finding: it contains two
#: colons, so the pre-repair three-part split mis-refused the minted principal.
COLON_SUPERVISOR = "supervisor:andrelange:claude-code"

#: The default store path the server itself always allows a principal to use, so
#: the tool's own root guard admits it without the fixture having to widen roots.
DEFAULT_DB_REL = ".skillweave/store.db"


# ── the real stdio-bound authority ──────────────────────────────────────────


def _bind_server(tmp_path: Path, monkeypatch, supervisor: str) -> tuple[str, str]:
    """Bind the *real* stdio authority for *supervisor* and return its issued pair.

    Returns ``(minted_principal, db_abs)``.  The session is minted by the
    production seam (:func:`startup_and_bind_stdio`) and its token is held in
    host memory (:data:`_READY_SESSION_TOKEN`), exactly as the live server
    holds it -- no token is hand-written and no principal is asserted by the
    test.
    """
    monkeypatch.delenv(mcp_server.SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    monkeypatch.setattr(mcp_server, "_READY_SESSION_TOKEN", None)
    # The server's own configured store path is the default, resolved against
    # the CWD, so binding the test to its own temporary root is enough.
    monkeypatch.chdir(tmp_path)

    token = mcp_server.startup_and_bind_stdio(supervisor=supervisor)
    authority = mcp_server._bound_authority()
    assert authority is not None
    minted = authority.verify_session(token).principal
    assert mcp_server.resolve_session_token("") == token

    db_abs = str(tmp_path / DEFAULT_DB_REL)
    Path(db_abs).parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(db_abs)
    store.create_run("run-1", task_id="t", attempt=1)
    assert store.acquire_controller_lease("ops").fencing_token == 1
    store.close()
    return minted, db_abs


def _call(monkeypatch, db_abs: str, **kw) -> dict:
    """Call the **real** ``handoff_adapter`` tool (fakes only).

    No ``principal`` is ever passed for a transition: the actor must be the
    boundary's own authenticated identity, never a caller field.
    """
    kw.setdefault("client_id", "claude-desktop")
    kw.setdefault("recipients", "ops")
    kw.setdefault("run_id", "run-1")
    kw.setdefault("db_path", db_abs)
    return json.loads(
        mcp_server.handoff_adapter(session_token="", **kw)
    )


def _offer(destination_principal: str, **over) -> dict:
    """A ``controller-handoff/v2`` offer addressed to *destination_principal*.

    The harness attestation's ``recipient_principal`` is the same value, so the
    offer passes every attested axis and the *only* thing under test is the
    server-issued-principal claim.
    """
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": destination_principal,
        "scope": "run-1",
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": {
            "schema": "harness-controller-attestation/v1",
            "client_id": "claude-desktop",
            "configured_route": {"gateway_host": "127.0.0.1", "gateway_port": 8090},
            "recipient_principal": destination_principal,
            "capabilities": {"session_resume": True, "notifications": True},
        },
    }
    base.update(over)
    return base


def _offer_call(monkeypatch, db_abs: str, destination_principal: str, **over) -> dict:
    return _call(
        monkeypatch, db_abs,
        handoff_offer=json.dumps(_offer(destination_principal, **over)),
    )


def _release_offered_generation(db_abs: str, generation: int) -> None:
    """Yield a generation directly through the store (the offerer's own release)."""
    store = SQLiteRunStore(db_abs)
    try:
        assert store.release_controller_lease(generation) is True
    finally:
        store.close()


# ── (a) the finding: a colon-bearing supervisor identity self-handoff ───────


def test_the_servers_own_colon_bearing_principal_passes_the_whole_lifecycle(
    tmp_path, monkeypatch,
):
    """offered -> accepted -> active -> released for the server's *own* minted
    principal, every step through the real handler under the real bound
    authority.  This is the case the pre-repair split refused."""
    minted, db_abs = _bind_server(tmp_path, monkeypatch, COLON_SUPERVISOR)
    # The minting is concatenation: the verbatim supervisor identity, colons and
    # all, is the trailing segment.  If this assertion ever changes the grammar
    # test below is measuring the wrong thing.
    assert minted == f"mcp:controller:{COLON_SUPERVISOR}"

    offered = _offer_call(monkeypatch, db_abs, minted)
    assert offered["status"] == "ok", offered
    assert offered["controller_handoff"]["state"] == ch.HANDOFF_STATE_OFFERED
    assert offered["controller_handoff"]["destination_principal"] == minted

    accepted = _call(monkeypatch, db_abs, action="accept", handoff_id="ho-1")
    assert accepted["status"] == "ok", accepted
    assert accepted["controller_handoff"]["state"] == ch.HANDOFF_STATE_ACCEPTED

    # The offerer yields generation 1 so the destination's own acquisition is
    # the exactly-once generation 2.
    _release_offered_generation(db_abs, 1)
    active = _call(monkeypatch, db_abs, action="activate", handoff_id="ho-1")
    assert active["status"] == "ok", active
    assert active["controller_handoff"]["state"] == ch.HANDOFF_STATE_ACTIVE
    assert active["controller_handoff"]["resulting_generation"] == 2

    released = _call(monkeypatch, db_abs, action="release", handoff_id="ho-1")
    assert released["status"] == "ok", released
    assert released["controller_handoff"]["state"] == ch.HANDOFF_STATE_RELEASED

    store = SQLiteRunStore(db_abs)
    try:
        assert store.get_controller_lease().holder == minted
        assert store.get_controller_lease().released_at is not None
    finally:
        store.close()


# ── (b) the colon-free identity still passes ────────────────────────────────


def test_a_colon_free_identity_still_passes_the_whole_lifecycle(
    tmp_path, monkeypatch,
):
    """The documented colon-free identity is unchanged by the repair."""
    minted, db_abs = _bind_server(tmp_path, monkeypatch, "opencode")
    assert minted == "mcp:controller:opencode"

    offered = _offer_call(monkeypatch, db_abs, minted)
    assert offered["status"] == "ok", offered
    accepted = _call(monkeypatch, db_abs, action="accept", handoff_id="ho-1")
    assert accepted["status"] == "ok", accepted
    _release_offered_generation(db_abs, 1)
    active = _call(monkeypatch, db_abs, action="activate", handoff_id="ho-1")
    assert active["status"] == "ok", active
    assert active["controller_handoff"]["resulting_generation"] == 2


# ── the refusals the repair must keep ───────────────────────────────────────


def test_an_unknown_role_with_colons_is_refused(tmp_path, monkeypatch):
    """``mcp:badrole:x:y`` names a role the registry does not own."""
    _, db_abs = _bind_server(tmp_path, monkeypatch, COLON_SUPERVISOR)
    payload = _offer_call(monkeypatch, db_abs, "mcp:badrole:x:y")
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED


def test_an_empty_supervisor_remainder_is_refused(tmp_path, monkeypatch):
    """``mcp:controller:`` has the right role but no supervisor identity."""
    _, db_abs = _bind_server(tmp_path, monkeypatch, COLON_SUPERVISOR)
    payload = _offer_call(monkeypatch, db_abs, "mcp:controller:")
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED


def test_a_supervisor_segment_with_whitespace_is_refused(tmp_path, monkeypatch):
    """A claimed principal whose supervisor segment carries a newline is refused
    (it is not the value the server issued, and it is not a shape it could mint
    unknowingly)."""
    _, db_abs = _bind_server(tmp_path, monkeypatch, COLON_SUPERVISOR)
    payload = _offer_call(monkeypatch, db_abs, "mcp:controller:sup\nevil")
    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED


# ── (f) a non-``mcp:`` principal behaves as before ──────────────────────────


def test_a_non_mcp_principal_is_admitted_as_before(tmp_path, monkeypatch):
    """A principal that makes no server-issued claim is admitted exactly as
    before -- the repair is additive to the documented in-process contract."""
    _, db_abs = _bind_server(tmp_path, monkeypatch, COLON_SUPERVISOR)
    payload = _offer_call(monkeypatch, db_abs, "dest-principal")
    assert payload["status"] == "ok", payload
    assert payload["controller_handoff"]["destination_principal"] == "dest-principal"


# ── exact comparison is preferred over shape matching ───────────────────────


def test_the_issued_value_is_admitted_by_identity_over_the_shape_grammar(
    tmp_path, monkeypatch,
):
    """The boundary admits the value it *actually issued* even when that value
    would not pass the shape grammar.

    A supervisor identity carrying an interior space mints a principal whose
    supervisor segment has whitespace -- the shape check would refuse it -- yet
    the server issued exactly that value for its bound session, so the exact
    comparison admits it.  This is the "prefer an exact comparison with the
    issued value" half of the repair; the shape check is the fallback for
    principals the server cannot compare.
    """
    minted, db_abs = _bind_server(tmp_path, monkeypatch, "supervisor andrelange")
    assert minted == "mcp:controller:supervisor andrelange"
    # The shape grammar alone would refuse this segment (whitespace)...
    assert mcp_server._supervisor_segment_is_wellformed("supervisor andrelange") is False

    payload = _offer_call(monkeypatch, db_abs, minted)
    assert payload["status"] == "ok", payload
    assert payload["controller_handoff"]["destination_principal"] == minted
