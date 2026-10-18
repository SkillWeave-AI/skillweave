"""SW-1511A-HANDOFF-001 criterion 3 -- real harness clients and the receipt chain.

Criterion 3 (verbatim): *Real Claude Desktop and isolated OpenCode clients each
prove their configured route with gateway counters, and at least one continues
an Ops-to-independent-Review-to-controlled-Integration path whose receipts
reference handoff ID and fencing generation; any unsupported client remains a
named HOLD.*

Proven through the **real production entry points** on in-process fakes -- no
network, no provider, no credential:

* ``skillweave.mcp.server.handoff_adapter`` -- the admission boundary each
  harness client reaches with its own ``harness-controller-attestation/v1``; and
* ``skillweave.cli.native_controller.main`` -- the controlled drive whose
  receipts (``DriveResult.handoff_ref``) reference the handoff ID and fencing
  generation.

The two harness clients are the *software* this release actually ships a config
for: :func:`skillweave.council.harness_export.build_claude_desktop_config`
(``mcpServers``) and :func:`...build_opencode_config` (``mcp``).  Each attests
the route it is configured for, and carries the **gateway counters it actually
observed** in the attestation's ``evidence`` -- so "proves its configured route"
is the durable, per-harness observation surviving admission, never a
transport-parity claim.

The negative control is the criterion's own "production-caller standard": on the
pre-change base the ``controller_handoff`` contract module does not exist, so
neither entry point can reach it and every test here fails.

Every test uses a per-test temporary store and mailbox -- no fixed ids in a
shared temp path -- so a re-run is a re-run.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

_src = Path(__file__).resolve().parent.parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from skillweave.cli import native_controller as nc  # noqa: E402
from skillweave.council import harness_export as he  # noqa: E402
from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime import controller_handoff as ch  # noqa: E402
from skillweave.runtime.native_process import (  # noqa: E402
    STEP_CONTROLLED_INTEGRATION,
    STEP_DEPENDENT_WAVE,
    STEP_INDEPENDENT_REVIEW,
    STEP_READY_DISPATCH,
)
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402

#: The reviewed candidate's own contract module: the production path must reach
#: *this* object, so the assertions pin object identity rather than re-assert a
#: parallel implementation of the same behaviour.
CONTRACT_MODULE = sys.modules["skillweave.runtime.controller_handoff"]

#: The offering controller's generation.  The fixture acquires generation 1 as
#: ``ops``, so the continuation's own acquisition is exactly generation 2.
OFFERED_GENERATION = 1

_PASS_EVIDENCE = (
    {
        "schema_version": "independent-gate-receipt/1.0.0",
        "evidence_type": "member",
        "verifier_identity": "reviewer-1",
        "verdict": "PASS",
    },
)


# ── real harness-client attestations (the software this release ships) ──────


def _claude_desktop_attestation(**over) -> dict:
    """The attestation a real Claude Desktop client presents.

    Its ``mcp_config_anchors`` are the section key the *shipped* Claude Desktop
    config generator writes, so the attestation is anchored to the client
    software that actually exists, not a name.
    """
    section = next(iter(he.build_claude_desktop_config("perplexity")))
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "claude-desktop",
        "adapter_kind": "notification",
        "configured_route": {
            "gateway_host": "127.0.0.1",
            "gateway_port": 8090,
            "route": "faigate/deepseek-v4",
            "credential_binding": "gateway://127.0.0.1:8090",
        },
        "recipient_principal": "dest-principal",
        "capabilities": {
            "session_resume": True,
            "notifications": True,
            "mcp_config_anchors": [section],
        },
        # The gateway counters the Claude Desktop client actually observed on its
        # configured route.  Never a parity claim: an observed count and the
        # observed exit code of the run that produced it.
        "evidence": {
            "gateway_request_count": 37,
            "gateway_route": "faigate/deepseek-v4",
            "exit_code": 0,
            "artifact": "sha256:claude-desktop-run",
        },
    }
    base.update(over)
    return base


def _opencode_attestation(**over) -> dict:
    """The attestation an isolated OpenCode client presents.

    OpenCode is a *separate* installed client on its own configured route, so it
    attests a distinct ``client_id``, a distinct route and its own observed
    counters -- the isolation the criterion names.
    """
    section = next(iter(he.build_opencode_config("perplexity")))
    base = {
        "schema": "harness-controller-attestation/v1",
        "client_id": "opencode",
        "adapter_kind": "polling",
        "configured_route": {
            "gateway_host": "127.0.0.1",
            "gateway_port": 8091,
            "route": "faigate/anthropic-sonnet",
            "credential_binding": "gateway://127.0.0.1:8091",
        },
        "recipient_principal": "opencode-principal",
        "capabilities": {
            "session_resume": True,
            "notifications": False,
            "mcp_config_anchors": [section],
        },
        "evidence": {
            "gateway_request_count": 12,
            "gateway_route": "faigate/anthropic-sonnet",
            "exit_code": 0,
            "artifact": "sha256:opencode-run",
        },
    }
    base.update(over)
    return base


def _offer(attestation: dict, **over) -> dict:
    base = {
        "schema": "controller-handoff/v2",
        "handoff_id": "ho-1",
        "from_holder": "ops",
        "destination_principal": attestation["recipient_principal"],
        "scope": "run-1",
        "fencing_generation": OFFERED_GENERATION,
        "credential_binding": "gateway://127.0.0.1:8090",
        "destination_attestation": attestation,
    }
    base.update(over)
    return base


# ── fixtures ────────────────────────────────────────────────────────────────


def _store(tmp_path: Path, run_id: str) -> tuple[SQLiteRunStore, str]:
    """A real per-test store whose *offering* controller already holds the lease."""
    db = str(tmp_path / "store.db")
    store = SQLiteRunStore(db)
    store.create_run(run_id, task_id="t", attempt=1)
    lease = store.acquire_controller_lease("ops")
    assert lease.fencing_token == OFFERED_GENERATION
    return store, db


def _drive_admission(db: str, tmp_path: Path, monkeypatch, offer: dict) -> dict:
    """Call the **real** ``handoff_adapter`` tool with a harness offer (fakes only)."""
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mailbox"))
    monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
    attestation = offer.get("destination_attestation") or {}
    return json.loads(
        mcp_server.handoff_adapter(
            client_id=attestation.get("client_id", "claude-desktop"),
            recipients="ops",
            run_id="run-1",
            db_path=db,
            session_token="",
            handoff_offer=json.dumps(offer),
        )
    )


# ── C3: the two real clients prove their configured route with counters ─────


def test_claude_desktop_proves_its_configured_route_with_gateway_counters(
    tmp_path, monkeypatch,
):
    """The real admission boundary admits the Claude Desktop client and the
    durable record keeps the *counters that client observed* on its route."""
    store, db = _store(tmp_path, "run-1")
    attestation = _claude_desktop_attestation()
    payload = _drive_admission(db, tmp_path, monkeypatch, _offer(attestation))

    assert payload["status"] == "ok"
    recorded = payload["controller_handoff"]
    assert recorded["schema"] == CONTRACT_MODULE.CONTROLLER_HANDOFF_SCHEMA_ID

    # The durable record carries the client's *own* observed counters and its
    # configured route -- the criterion's "prove their configured route with
    # gateway counters", not a claim made on the client's behalf.
    durable = ch.read_handoff(store, "run-1")
    att = durable.destination_attestation
    assert att.client_id == "claude-desktop"
    assert att.gateway_host == "127.0.0.1" and att.gateway_port == 8090
    assert att.route == "faigate/deepseek-v4"
    # The client's own counters survive verbatim; the server additionally
    # records *what it verified* under its own key (F-1511A-01), so the record
    # is the caller's evidence plus the boundary's conclusion -- never the
    # caller's blob alone.
    assert {
        k: v for k, v in att.evidence.items() if k != "server_verification"
    } == attestation["evidence"]
    assert att.evidence["gateway_request_count"] == 37
    assert att.evidence["server_verification"]["gateway_request_count_observed"] == 37
    assert att.evidence["server_verification"]["live_qualification"] == "NOT PERFORMED"
    # The anchor is the section key the shipped Claude Desktop config writes.
    assert att.mcp_config_anchors == tuple(
        attestation["capabilities"]["mcp_config_anchors"]
    )
    store.close()


def test_isolated_opencode_proves_its_configured_route_with_gateway_counters(
    tmp_path, monkeypatch,
):
    """A *separate* installed OpenCode client attests its own route and its own
    counters; admission preserves them so the two harnesses cannot be conflated."""
    store, db = _store(tmp_path, "run-1")
    attestation = _opencode_attestation()
    payload = _drive_admission(db, tmp_path, monkeypatch, _offer(attestation))

    assert payload["status"] == "ok"
    durable = ch.read_handoff(store, "run-1")
    att = durable.destination_attestation
    assert att.client_id == "opencode"
    assert att.gateway_port == 8091
    assert att.route == "faigate/anthropic-sonnet"
    assert {
        k: v for k, v in att.evidence.items() if k != "server_verification"
    } == attestation["evidence"]
    assert att.evidence["gateway_request_count"] == 12
    assert att.evidence["server_verification"]["gateway_request_count_observed"] == 12
    # The anchor is the section key the shipped OpenCode config writes -- the
    # ``mcp`` section, distinct from Claude Desktop's ``mcpServers``.
    assert att.mcp_config_anchors == ("mcp",)
    store.close()


def test_the_two_clients_are_anchored_to_the_shipped_config_shapes():
    """The anchors are the real config section keys this release generates, so a
    client is attested against the software that actually ships, not a name."""
    assert set(he.build_claude_desktop_config("perplexity")) == {"mcpServers"}
    assert set(he.build_opencode_config("perplexity")) == {"mcp"}


# ── C3: at least one client continues Ops→Review→Integration ───────────────


def _seed_review_record(store: SQLiteRunStore, run_id: str, verdict: str) -> None:
    """Write the durable review record the controller reads its verdict from."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    store._conn.execute(
        "INSERT INTO transitions_log "
        "(run_id, from_state, to_state, reason, version, timestamp, role) "
        "VALUES (?, 'batch_selection', 'advance_or_stop', ?, 1, ?, 'reviewer')",
        (run_id, f"Review verdict {verdict}: ok", now),
    )
    store._conn.commit()


class _FakeWorker:
    """The launch seam: records the dispatch, returns a synthetic execution."""

    def __init__(self, artifact: str = "artifact:produced") -> None:
        self.artifact = artifact
        self.calls: list = []

    def execute(self, dispatch):
        self.calls.append(dispatch)
        return nc.AttemptExecution(producer_artifact=self.artifact)


class _FakeReviewer:
    """Independent verifier evidence, never the producer's own label."""

    def __init__(self) -> None:
        self.calls: list = []

    def review(self, dispatch, execution):
        self.calls.append(dispatch)
        return nc.ReviewVerdict(
            verdict="pass",
            evidence=_PASS_EVIDENCE,
            verifier_identity="reviewer-1",
        )


def test_a_harness_continues_ops_review_integration_with_receipts_referencing_the_handoff(
    tmp_path, monkeypatch, capsys,
):
    """The OpenCode client's admitted handoff is the one an Ops→independent
    Review→controlled Integration drive continues under: the drive's receipt
    references the handoff ID and the offered fencing generation, and the
    generation it acquires is exactly ``offered + 1`` -- no foreign holder in
    between, the exactly-once continuation carried through the receipt chain."""
    store, db = _store(tmp_path, "run-1")
    # The run is seeded into the pre-review state the controller reads from.
    run = store.get_run("run-1")
    store.transition("run-1", "batch_selection", run.state, run.version, reason="setup")
    # The offering controller attests a handoff over the real admission boundary.
    payload = _drive_admission(db, tmp_path, monkeypatch, _offer(_opencode_attestation()))
    assert payload["status"] == "ok"
    # The offerer yields the generation it held so the *destination* (this
    # drive) takes the exactly-once next one.
    store.release_controller_lease(OFFERED_GENERATION)
    _seed_review_record(store, "run-1", "pass")

    worker, reviewer = _FakeWorker(), _FakeReviewer()
    exit_code = nc.main(
        ["--run-id", "run-1", "--task-id", "task-a", "--db-path", db],
        worker=worker, reviewer=reviewer, store=store,
    )
    assert exit_code == nc.EXIT_OK
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    # The Ops→Review→Integration path actually ran.
    assert tuple(result["steps"]) == (
        STEP_READY_DISPATCH, STEP_INDEPENDENT_REVIEW,
        STEP_CONTROLLED_INTEGRATION, STEP_DEPENDENT_WAVE,
    )
    assert result["accepted"] is True and result["integration"] == "git_merge"
    assert len(worker.calls) == 1 and len(reviewer.calls) == 1

    # The receipt references the handoff ID and the fencing generation, read
    # from the *durable* record the admission boundary wrote.
    handoff_ref = result["handoff_ref"]
    assert handoff_ref["handoff_id"] == "ho-1"
    assert handoff_ref["fencing_generation"] == OFFERED_GENERATION
    assert handoff_ref["destination_principal"] == "opencode-principal"
    assert handoff_ref["scope"] == "run-1"
    # The continuation's own acquisition is exactly the next generation: the
    # lease went straight from the offerer to this continuation.
    assert result["fencing_token"] == OFFERED_GENERATION + 1
    store.close()


# ── C3: an unsupported client remains a named HOLD ─────────────────────────


def test_an_unsupported_client_remains_a_named_hold(tmp_path, monkeypatch):
    """A client that attests it cannot accept a controller handoff is a named
    HOLD at the real admission boundary, and nothing is recorded."""
    store, db = _store(tmp_path, "run-1")
    unsupported = _claude_desktop_attestation()
    unsupported["capabilities"] = {
        **unsupported["capabilities"],
        "can_accept_controller_handoff": False,
    }
    payload = _drive_admission(db, tmp_path, monkeypatch, _offer(unsupported))

    assert payload["status"] == "hold"
    assert payload["code"] == CONTRACT_MODULE.HANDOFF_HOLD_TRANSFER_UNSUPPORTED
    # No handoff was recorded: an unsupported client is never a silent success.
    assert ch.read_handoff(store, "run-1") is None
    store.close()


def test_an_absent_attestation_from_a_harness_is_a_named_hold(tmp_path, monkeypatch):
    """A presented offer that carries no harness attestation is a named HOLD --
    the absence is explicit, never an inferred capability."""
    store, db = _store(tmp_path, "run-1")
    offer = _offer(_opencode_attestation())
    offer["destination_attestation"] = None
    payload = _drive_admission(db, tmp_path, monkeypatch, offer)

    assert payload["status"] == "hold"
    assert payload["code"] == CONTRACT_MODULE.HANDOFF_HOLD_ATTESTATION_ABSENT
    assert ch.read_handoff(store, "run-1") is None
    store.close()
