"""SW-1511A-GATE-001 repair F2 -- the offer response shape is unchanged.

The independent review of repair F (finding R1) found a backward-compatibility
regression: with the F3 dispatch guard in place the tool returned *early* for
any call presenting a ``handoff_offer``, so the offer path stopped returning the
transport-selection fields it returned with ``controller_handoff`` on the base
``a70769c`` (``client_id``, ``contract``, ``adapter``, ``authority_contract``,
``continuation``, ``continuation_persisted``) and also skipped the transport
selection and the continuation persistence for that path.

An *offer* -- the default with no ``action``, an explicit ``action="offer"``, or
a ``handoff_offer`` payload -- is an admission, not a transition.  Only
``accept``/``activate``/``release``/``reject`` return early with their own
action-scoped shape.

This file proves it against the **base** the regression report names: the test
materialises a real ``git worktree`` at ``a70769c52c891894e86d5636ed93dd872e64e296``,
drives the *same* offer payload through the **real MCP handler**
(``skillweave.mcp.server.handoff_adapter`` -- never a module-level call) on that
base, then drives it through this head, and asserts the head's response still
carries every base key with the same meaning.  On the pre-repair head the F3
guard returned early, so the comparison fails there and passes here.

Every test uses a per-test temporary store and mailbox, and the base worktree is
created once and removed at the end, so a re-run is a re-run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skillweave.mcp import server as mcp_server  # noqa: E402

#: The base the review finding R1 names: the last head before repair F.
BASE_SHA = "a70769c52c891894e86d5636ed93dd872e64e296"

#: Every key the pre-F offer path returned alongside ``controller_handoff``.
BASE_OFFER_KEYS = (
    "status",
    "client_id",
    "contract",
    "adapter",
    "authority_contract",
    "continuation",
    "continuation_persisted",
)

#: The keys repair F2 adds additively -- present in addition, never instead.
ADDITIVE_KEYS = ("action", "handoff_ref")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _offer(**over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": "dest-principal",
        "scope": "run-1",
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": {
            "schema": "harness-controller-attestation/v1",
            "client_id": "claude-desktop",
            "configured_route": {"gateway_host": "127.0.0.1", "gateway_port": 8090},
            "recipient_principal": "dest-principal",
            "capabilities": {"session_resume": True, "notifications": True},
        },
    }
    base.update(over)
    return base


def _call(**kw) -> dict:
    """Call the **real** ``handoff_adapter`` tool (never a module-level call)."""
    kw.setdefault("client_id", "claude-desktop")
    kw.setdefault("recipients", "ops")
    kw.setdefault("session_token", "")
    return json.loads(mcp_server.handoff_adapter(**kw))


@pytest.fixture
def base_response(tmp_path_factory):
    """The base's real offer response, captured from a worktree at ``BASE_SHA``.

    The worktree runs the *base* sources and the *base* tool signature (which
    has no ``action`` argument), so the captured response is the pre-repair
    behaviour the finding measured -- not a re-assertion of the head.
    """
    repo = _repo_root()
    root = tmp_path_factory.mktemp("base-worktree")
    worktree = root / "wt"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), BASE_SHA],
        cwd=repo, check=True, capture_output=True,
    )
    try:
        db = str(root / "base-store.db")
        runner = tmp_path_factory.mktemp("base-run") / "drive.py"
        runner.write_text(
            "import json, os, sys\n"
            "sys.path.insert(0, os.environ['BASE_SRC'])\n"
            "from skillweave.runtime.store import SQLiteRunStore\n"
            "from skillweave.mcp import server as mcp\n"
            "store = SQLiteRunStore(os.environ['BASE_DB'])\n"
            "store.create_run('run-1', task_id='t', attempt=1)\n"
            "assert store.acquire_controller_lease('ops').fencing_token == 1\n"
            "payload = json.loads(mcp.handoff_adapter(\n"
            "    client_id='claude-desktop', recipients='ops', run_id='run-1',\n"
            "    db_path=os.environ['BASE_DB'], session_token='',\n"
            "    handoff_offer=os.environ['BASE_OFFER'],\n"
            "))\n"
            "print(json.dumps(payload))\n"
        )
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(worktree / "src")
        env["BASE_SRC"] = str(worktree / "src")
        env["BASE_DB"] = db
        env["BASE_OFFER"] = json.dumps(_offer())
        env[mcp_server.MAILBOX_PATH_ENV] = str(root / "base-mailbox")
        proc = subprocess.run(
            [sys.executable, str(runner)],
            cwd=str(worktree), env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        yield json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo, check=False, capture_output=True,
        )
        shutil.rmtree(root, ignore_errors=True)


def _head_response(tmp_path, monkeypatch, *, fresh_store=True, **call_kw) -> dict:
    """This head's real offer response, on a fresh per-call store by default.

    ``fresh_store=False`` reuses the store the caller passes as ``db_path``, so
    a test can drive several calls against one durable run.
    """
    import uuid

    from skillweave.runtime.store import SQLiteRunStore

    if fresh_store:
        db = str(tmp_path / f"store-{uuid.uuid4().hex[:8]}.db")
        store = SQLiteRunStore(db)
        store.create_run("run-1", task_id="t", attempt=1)
        assert store.acquire_controller_lease("ops").fencing_token == 1
        store.close()
        call_kw["db_path"] = db
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    call_kw.setdefault("run_id", "run-1")
    return _call(**call_kw)


def test_offer_payload_keeps_every_base_key(base_response, tmp_path, monkeypatch):
    """A ``handoff_offer`` payload returns the base key set, unchanged values."""
    head = _head_response(
        tmp_path, monkeypatch, handoff_offer=json.dumps(_offer())
    )
    for key in BASE_OFFER_KEYS:
        assert key in head, f"base key {key!r} missing from the offer response"
        assert key in base_response
    for key in ADDITIVE_KEYS:
        assert key in head
    assert head["status"] == base_response["status"] == "ok"
    assert head["controller_handoff"]["state"] == "offered"
    assert head["controller_handoff"]["handoff_id"] == (
        base_response["controller_handoff"]["handoff_id"]
    )
    # The transport selection ran for the offer path, exactly as on the base.
    assert head["client_id"] == base_response["client_id"]
    assert head["adapter"] == base_response["adapter"]
    assert head["contract"] == base_response["contract"]
    assert head["authority_contract"] == base_response["authority_contract"]


def test_explicit_action_offer_matches_the_base_offer_path(
    base_response, tmp_path, monkeypatch
):
    """``action="offer"`` is the same admission, not a transition."""
    head = _head_response(
        tmp_path, monkeypatch,
        action="offer", handoff_offer=json.dumps(_offer()),
    )
    for key in BASE_OFFER_KEYS:
        assert key in head, f"base key {key!r} missing for action='offer'"
    assert head["status"] == "ok"
    assert head["action"] == "offer"
    assert head["adapter"] == base_response["adapter"]
    assert head["controller_handoff"]["state"] == "offered"


def test_no_action_without_offer_is_the_historic_transport_path(
    tmp_path, monkeypatch
):
    """A call with neither an action nor an offer is the pre-existing path."""
    head = _head_response(tmp_path, monkeypatch)
    for key in BASE_OFFER_KEYS:
        assert key in head, f"base key {key!r} missing from the historic path"
    assert head["status"] == "ok"
    # No offer was presented, so there is no handoff -- exactly as on the base.
    assert "controller_handoff" not in head
    assert "action" not in head


def test_the_offer_continuation_is_decided_and_persisted(
    base_response, tmp_path, monkeypatch
):
    """The offer path keeps the continuation decision *and* persists it."""
    head = _head_response(
        tmp_path, monkeypatch, handoff_offer=json.dumps(_offer())
    )
    assert head["continuation"] == base_response["continuation"]
    assert head["continuation_persisted"] == base_response["continuation_persisted"]
    assert head["continuation_persisted"]["status"] == "persisted"

    # The decision is durably readable through the production reader under the
    # same (in-process, self-named) principal the offer persisted it under --
    # exactly as the integration restart test reads it back.
    decision = json.loads(
        mcp_server.read_continuation_decision(
            run_id="run-1", principal="in-process", session_token=""
        )
    )
    assert decision["status"] == "ok"
    assert decision["found"] is True
    assert decision["decision"] == head["continuation"]


def test_a_transition_still_returns_its_own_action_scoped_shape(
    tmp_path, monkeypatch
):
    """Only the four real transitions return early, with their own shape."""
    import uuid

    from skillweave.runtime.store import SQLiteRunStore

    db = str(tmp_path / f"store-{uuid.uuid4().hex[:8]}.db")
    store = SQLiteRunStore(db)
    store.create_run("run-1", task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    store.close()
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    _call(run_id="run-1", db_path=db, handoff_offer=json.dumps(_offer()))

    accepted = _call(
        run_id="run-1", db_path=db,
        action="accept", principal="dest-principal", handoff_id="ho-1",
    )
    assert set(accepted) == {
        "status", "action", "controller_handoff", "handoff_ref",
    }
    assert accepted["action"] == "accept"
    assert accepted["controller_handoff"]["state"] == "accepted"


def test_the_offer_admission_counter_is_not_moved(tmp_path, monkeypatch):
    """D3 monotonicity: the offer still admits through the same boundary.

    The repair changes only *when the guard returns*; the admission (and the
    counter verification it reaches) is untouched, so a first observation is
    admitted and a regressed second observation is still refused by name.
    """
    import uuid

    from skillweave.runtime import controller_handoff as ch
    from skillweave.runtime.store import SQLiteRunStore

    db = str(tmp_path / f"store-{uuid.uuid4().hex[:8]}.db")
    store = SQLiteRunStore(db)
    store.create_run("run-1", task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    store.close()

    offer = _offer(
        destination_attestation={
            "schema": "harness-controller-attestation/v1",
            "client_id": "claude-desktop",
            "configured_route": {
                "gateway_host": "127.0.0.1", "gateway_port": 8090,
                "route": "gw-route",
            },
            "recipient_principal": "dest-principal",
            "capabilities": {"session_resume": True, "notifications": True},
            "evidence": {"gateway_route": "gw-route", "gateway_request_count": 5},
        },
    )
    first = _head_response(
        tmp_path, monkeypatch, fresh_store=False, db_path=db,
        handoff_offer=json.dumps(offer),
    )
    assert first["status"] == "ok"

    regressed = dict(offer, handoff_id="ho-2")
    regressed["destination_attestation"] = dict(
        offer["destination_attestation"],
        evidence={"gateway_route": "gw-route", "gateway_request_count": 4},
    )
    second = _head_response(
        tmp_path, monkeypatch, fresh_store=False, db_path=db,
        handoff_offer=json.dumps(regressed),
    )
    assert second["status"] == "hold"
    assert second["code"] == ch.HANDOFF_HOLD_COUNTER_REGRESSION
