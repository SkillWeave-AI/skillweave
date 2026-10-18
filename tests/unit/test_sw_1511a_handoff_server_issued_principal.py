"""SW-1511A-HANDOFF-001 finding F-1511A-04 -- a server-issued principal claim.

Criterion 1 asks the lease to move to a **server-issued** destination principal.
Review-001 (C3) found the boundary trusted the offer's self-claim: any principal
string was admitted, so "server-issued" proved nothing.

This file is the adversarial proof that the **real admission boundary**
(``skillweave.mcp.server.handoff_adapter``) now refuses a principal that *claims*
to be server-issued (the ``mcp:<role>:<client_id>`` shape the MCP authority
mints) but whose role is not a role the server actually owns -- on in-process
fakes, no network, no provider, no credential.

The check is conditional on the claim being made, so the documented in-process /
self-named path (a principal that is *not* ``mcp:``-shaped) is admitted exactly
as before -- the positive control proves the refusal is a *claim* check, not a
blanket refusal of the existing contract.  On the pre-repair head nothing checks
the principal, so the refusal test fails there; here the same real tool call
returns the named HOLD, and no server-issued identity is ever invented.

Every test uses a per-test temporary store and mailbox, so a re-run is a re-run.
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
from skillweave.runtime.authority import MCP_ROLE_SCOPES  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    store.acquire_controller_lease("ops")
    return store, db


def _attestation(recipient_principal: str) -> dict:
    return {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "configured_route": {
            "gateway_host": "127.0.0.1",
            "gateway_port": 8090,
            "route": "faigate/deepseek-v4",
            "credential_binding": "gateway://127.0.0.1:8090",
        },
        "recipient_principal": recipient_principal,
        "capabilities": {"session_resume": True, "notifications": True},
        "evidence": {"exit_code": 0},
    }


def _offer(destination_principal: str, **over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": destination_principal,
        "scope": "run-1",
        "fencing_generation": 1,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": _attestation(destination_principal),
    }
    base.update(over)
    return base


def _drive(db, tmp_path, monkeypatch, offer) -> dict:
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    return json.loads(
        mcp_server.handoff_adapter(
            client_id="claude-desktop",
            recipients="ops",
            run_id="run-1",
            db_path=db,
            session_token="",
            handoff_offer=json.dumps(offer),
        )
    )


# ── the refusal (fails on the pre-repair head, which trusts the self-claim) ─


def test_an_mcp_shaped_principal_with_a_non_registry_role_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """``mcp:bogus:xyz`` claims the MCP authority issued it; ``bogus`` is not a
    role the authority owns, so the claim is refused by name."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer("mcp:bogus:xyz"))

    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED
    assert ch.read_handoff(store, "run-1") is None  # nothing was recorded
    store.close()


def test_an_mcp_shaped_principal_missing_the_client_id_is_a_named_hold(
    tmp_path, monkeypatch,
):
    """``mcp:worker`` is the right prefix but the wrong arity: the authority
    never mints a two-part principal, so it is refused too."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer("mcp:worker"))

    assert payload["status"] == "hold"
    assert payload["code"] == ch.HANDOFF_HOLD_PRINCIPAL_NOT_SERVER_ISSUED
    store.close()


# ── the positive controls: the refusal is a *claim* check ──────────────────


def test_a_wellformed_server_role_principal_is_admitted(tmp_path, monkeypatch):
    """A principal in the authority's own shape and role is admitted -- the
    check refuses a false claim, not the contract itself."""
    assert "controller" in MCP_ROLE_SCOPES
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer("mcp:controller:client-1"))

    assert payload["status"] == "ok"
    record = ch.read_handoff(store, "run-1")
    assert record is not None
    assert record.destination_principal == "mcp:controller:client-1"
    store.close()


def test_a_non_mcp_principal_makes_no_claim_and_is_admitted(tmp_path, monkeypatch):
    """The documented in-process / self-named path carries no server-issued
    claim, so it is admitted exactly as before -- the change is additive."""
    store, db = _store(tmp_path, "run-1")
    payload = _drive(db, tmp_path, monkeypatch, _offer("dest-principal"))

    assert payload["status"] == "ok"
    store.close()
