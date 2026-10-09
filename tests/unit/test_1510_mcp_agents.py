"""SW-1510-MCP-AGENTS: durable, harness-neutral handoff and agent mailbox.

Application-level agent communication over MCP, **without assuming MCP is a
reliable peer-to-peer event bus**. The durable mailbox is the source of truth;
notifications are advisory and lossy. These tests prove the three acceptance
criteria at the *actual consumer boundary* -- the callables in
:mod:`skillweave.mcp.server` that an MCP client invokes, the
:class:`~skillweave.dispatch.messaging.AgentMailbox` that stores messages, and
the :class:`~skillweave.runtime.handoff.HandoffBroker` bridge -- never against a
re-implementation:

1. Registration/capability attestation, addressed handoff/ack, ``input_required``
   and status subscription/replay use scoped principal and
   run/task/attempt/message IDs.
2. Reconnect and duplicated deliveries are idempotent; notifications may be
   missed without losing durable work or creating duplicate execution.
3. Codex, Antigravity and OpenClaude capability support is explicitly detected;
   unavailable client features use file/CLI polling adapters with the same
   authority contract.

The frozen ``agent-message-v1`` contract is proven too: the schema version and
canonical digest are pinned, a drifted record is refused by name, and the
produced records round-trip through the pinned contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.dispatch import messaging as messaging  # noqa: E402
from skillweave.dispatch.messaging import (  # noqa: E402
    AGENT_MESSAGE_ARTIFACT,
    AGENT_MESSAGE_NAMESPACE,
    AGENT_MESSAGE_SCHEMA_DIGEST,
    AGENT_MESSAGE_SCHEMA_VERSION,
    AgentMailbox,
    AgentMessage,
    MailboxCursor,
    MailboxHandoffBridge,
    MessageMismatchRefused,
    UnsupportedMessageVersion,
    agent_message_contract_digest,
)
from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime import handoff as runtime_handoff  # noqa: E402
from skillweave.runtime.authority import MCPPrincipalAuthority  # noqa: E402
from skillweave.runtime.handoff import (  # noqa: E402
    ColdStartBundle,
    HandoffBroker,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    """Point tempfile at the scratch basetemp so no probe touches host TMPDIR."""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    # The server-owned mailbox defaults beside the store; give each test its own
    # file so tests never share durable state.
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "agent-mailbox.jsonl"))
    monkeypatch.delenv("SKILLWEAVE_CLIENT_CAPABILITY", raising=False)
    return tmp_path


@pytest.fixture
def authority():
    """A fresh server-owned authority bound onto the MCP server module."""
    auth = MCPPrincipalAuthority()
    previous = mcp_server.get_authority()
    previous_ready = mcp_server._READY_SESSION_TOKEN
    mcp_server.bind_authority(auth)
    mcp_server._READY_SESSION_TOKEN = None
    yield auth
    mcp_server._AUTHORITY = previous
    mcp_server._READY_SESSION_TOKEN = previous_ready


def _child_env() -> dict:
    env = dict(os.environ)
    existing = [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if e]
    if str(_SRC) not in existing:
        existing.insert(0, str(_SRC))
    env["PYTHONPATH"] = os.pathsep.join(existing)
    return env


def _msg(**kw):
    base = dict(kind="handoff", sender="ctrl", recipient="worker-1", principal="mcp:controller:A")
    base.update(kw)
    return AgentMessage(**base)


def _mailbox(tmp_path, name="m.jsonl"):
    return AgentMailbox(str(tmp_path / name))


# ===========================================================================
# Criterion 1: scoped principal + run/task/attempt/message IDs
# ===========================================================================


class TestScopedIdentity:
    def test_contract_identity_is_frozen_and_recomputable(self):
        # The pinned digest is the canonical digest of the declared contract.
        assert agent_message_contract_digest() == AGENT_MESSAGE_SCHEMA_DIGEST
        assert AGENT_MESSAGE_ARTIFACT == "agent-message-v1"
        assert AGENT_MESSAGE_NAMESPACE == "skillweave/agent-message"
        assert AGENT_MESSAGE_SCHEMA_VERSION == "1.0.0"

    def test_a_message_binds_principal_and_run_task_attempt_message_ids(self, tmp_path):
        mb = _mailbox(tmp_path)
        msg = _msg(run_id="run-1", task_id="task-1", attempt=2, message_id="m-1")
        record = mb.post(msg)
        assert record["principal"] == "mcp:controller:A"
        assert record["run_id"] == "run-1" and record["task_id"] == "task-1"
        assert record["attempt"] == 2 and record["message_id"] == "m-1"
        assert record["schema_version"] == AGENT_MESSAGE_SCHEMA_VERSION

    def test_an_anonymous_sender_is_refused(self):
        with pytest.raises(messaging.MessageChannelError):
            AgentMessage(kind="status", sender="x", recipient="y", principal="")

    def test_unknown_kind_is_refused(self):
        with pytest.raises(messaging.MessageChannelError):
            AgentMessage(kind="gossip", sender="x", recipient="y", principal="p")

    def test_an_unknown_schema_version_is_refused_by_name(self, tmp_path):
        mb = _mailbox(tmp_path)
        mb.post(_msg())
        with open(mb.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema_version": "2.0.0", "message_id": "z"}) + "\n")
        with pytest.raises(UnsupportedMessageVersion) as exc:
            mb.read(recipient="worker-1", principal="mcp:controller:A")
        assert exc.value.code == "unsupported_message_version"

    def test_a_record_rejected_as_drifted_names_the_mismatch(self, tmp_path):
        mb = _mailbox(tmp_path)
        mb.post(_msg(message_id="m-drift"))
        # Re-post the same id with a different body: fail closed.
        with pytest.raises(MessageMismatchRefused) as exc:
            mb.post(_msg(message_id="m-drift", body={"tampered": True}))
        assert exc.value.code == "message_id_mismatch"


class TestAddressedHandoffAndAck:
    def test_handoff_bridge_posts_a_durable_addressed_handoff(self, tmp_path):
        mb = _mailbox(tmp_path)
        broker = HandoffBroker()
        bridge = MailboxHandoffBridge(mb)
        cold = ColdStartBundle(
            prd_uri="p", prd_digest="pd", chain_uri="c", chain_digest="cd",
            repo_uri="repo", worktree_path="/w", branch="b",
            target_role="worker-1", sequence_id="seq-1",
        )
        result = bridge.offer(
            broker, from_role="controller", to_role="worker-1", scope="scope-1",
            cold_start=cold, principal="mcp:controller:A", attempt=1,
        )
        handoff_id = result["handoff_id"]
        got = mb.read(recipient="worker-1", principal="mcp:controller:A")
        assert len(got) == 1
        assert got[0]["kind"] == "handoff"
        assert got[0]["message_id"] == handoff_id
        assert got[0]["run_id"] == "seq-1" and got[0]["task_id"] == "scope-1"

    def test_ack_is_addressed_back_to_the_sender(self, tmp_path):
        mb = _mailbox(tmp_path)
        broker = HandoffBroker()
        bridge = MailboxHandoffBridge(mb)
        cold = ColdStartBundle(
            prd_uri="p", prd_digest="pd", chain_uri="c", chain_digest="cd",
            repo_uri="repo", worktree_path="/w", branch="b",
            target_role="worker-1", sequence_id="seq-1",
        )
        offered = bridge.offer(
            broker, from_role="controller", to_role="worker-1", scope="s",
            cold_start=cold, principal="mcp:controller:A",
        )
        acked = bridge.accept(
            broker, offered["handoff_id"], actor="worker-1", product="x",
            repo="repo", prd_digest="pd", chain_digest="cd",
            principal="mcp:controller:A",
        )
        assert acked["handoff"].state == "accepted"
        assert acked["message"]["kind"] == "ack"
        to_sender = mb.read(recipient="controller", principal="mcp:controller:A")
        assert [m["kind"] for m in to_sender] == ["ack"]

    def test_ack_to_a_non_recipient_is_refused(self, tmp_path):
        mb = _mailbox(tmp_path)
        mb.post(_msg())
        # An unknown message cannot be acknowledged at all.
        with pytest.raises(messaging.MessageChannelError):
            mb.ack("does-not-exist", recipient="worker-1", principal="mcp:controller:A")
        # The addressed recipient may ack; a different principal may not.
        posted = mb.read(recipient="worker-1", principal="mcp:controller:A")[0]
        with pytest.raises(MessageMismatchRefused):
            mb.ack(posted["message_id"], recipient="worker-1", principal="mcp:observer:B")

    def test_input_required_and_status_are_first_class_kinds(self, tmp_path):
        mb = _mailbox(tmp_path)
        mb.post(_msg(kind="input_required", body="need decision"))
        mb.post(_msg(kind="status", body="50%"))
        kinds = [m["kind"] for m in mb.read(recipient="worker-1", principal="mcp:controller:A")]
        assert kinds == ["input_required", "status"]


class TestServerToolsAreScoped:
    def test_mailbox_post_uses_the_authenticated_principal(self, authority, tmp_path):
        token = authority.issue_session(
            role="controller", client_id="A", transport="stdio"
        )
        out = json.loads(mcp_server.mailbox_post(
            kind="handoff", recipient="worker-1", run_id="run-1", task_id="task-1",
            attempt=1, session_token=token,
        ))
        assert out["status"] == "posted", out
        assert out["message"]["principal"] == "mcp:controller:A", out

    def test_mailbox_post_refuses_a_principal_mismatch(self, authority):
        token = authority.issue_session(
            role="controller", client_id="A", transport="stdio"
        )
        out = json.loads(mcp_server.mailbox_post(
            kind="status", recipient="worker-1", principal="mcp:controller:B",
            session_token=token,
        ))
        assert out["status"] == "refused"
        assert out["code"] == "principal_mismatch"

    def test_mailbox_read_refuses_without_a_credential(self, authority):
        out = json.loads(mcp_server.mailbox_read(recipient="worker-1"))
        assert out["status"] == "refused" and out["code"] == "unauthenticated"

    def test_mailbox_read_hides_another_principals_messages(self, authority, tmp_path):
        owner = authority.issue_session(role="controller", client_id="A", transport="stdio")
        other = authority.issue_session(role="controller", client_id="B", transport="stdio")
        json.loads(mcp_server.mailbox_post(
            kind="handoff", recipient="shared", body="for-A",
            session_token=owner,
        ))
        read_by_b = json.loads(mcp_server.mailbox_read(
            recipient="shared", session_token=other,
        ))
        assert read_by_b["messages"] == []

    def test_direct_caller_without_bound_authority_may_name_a_principal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcp_server, "_AUTHORITY", None)
        out = json.loads(mcp_server.mailbox_post(
            kind="input_required", recipient="w2", principal="mcp:worker:local",
            body="need input",
        ))
        assert out["status"] == "posted", out
        # ...but a directive with no principal at all is still refused.
        denied = json.loads(mcp_server.mailbox_post(
            kind="status", recipient="w2", body="x",
        ))
        assert denied["code"] == "unauthenticated"


# ===========================================================================
# Criterion 2: idempotent reconnect/duplicate; missed notifications lose nothing
# ===========================================================================


class TestIdempotentReconnect:
    def test_duplicate_delivery_is_idempotent(self, tmp_path):
        mb = _mailbox(tmp_path)
        msg = _msg(message_id="m-dup", run_id="r", attempt=1)
        first = mb.post(msg)
        second = mb.post(msg)
        assert first["seq"] == second["seq"] == 1
        assert len(mb.replay(recipient="worker-1", principal="mcp:controller:A")) == 1

    def test_a_missed_notification_loses_no_durable_work(self, tmp_path):
        mb = _mailbox(tmp_path)
        # The consumer "sees" only the first delivery; messages 2 and 3 arrive
        # while it is disconnected. It resumes from its durable cursor.
        mb.post(_msg(message_id="m1"))
        seen = mb.read(recipient="worker-1", principal="mcp:controller:A")
        token = seen[0]["resume_token"]
        mb.post(_msg(message_id="m2"))
        mb.post(_msg(message_id="m3"))
        resumed = mb.read(
            recipient="worker-1", principal="mcp:controller:A", resume_token=token,
        )
        assert [m["message_id"] for m in resumed] == ["m2", "m3"]

    def test_replay_is_a_pure_function_of_the_durable_log(self, tmp_path):
        mb = _mailbox(tmp_path)
        for i in range(3):
            mb.post(_msg(message_id=f"m{i}"))
        # Two replays from zero are byte-identical: reading consumes nothing.
        first = mb.replay(recipient="worker-1", principal="mcp:controller:A")
        second = mb.replay(recipient="worker-1", principal="mcp:controller:A")
        assert [m["message_id"] for m in first] == [m["message_id"] for m in second]
        # A third read with the cursor from the second replay yields nothing new.
        token = first[-1]["resume_token"]
        assert mb.read(recipient="worker-1", principal="mcp:controller:A", resume_token=token) == []

    def test_duplicate_post_at_the_server_does_not_create_duplicate_execution(self, authority):
        token = authority.issue_session(role="controller", client_id="A", transport="stdio")
        first = json.loads(mcp_server.mailbox_post(
            kind="handoff", recipient="worker-1", message_id="fixed-id",
            run_id="run-1", attempt=1, session_token=token,
        ))
        second = json.loads(mcp_server.mailbox_post(
            kind="handoff", recipient="worker-1", message_id="fixed-id",
            run_id="run-1", attempt=1, session_token=token,
        ))
        assert first["message"]["seq"] == second["message"]["seq"] == 1
        replayed = json.loads(mcp_server.mailbox_replay(
            recipient="worker-1", session_token=token,
        ))
        assert len(replayed["messages"]) == 1

    def test_repeated_ack_is_idempotent(self, authority):
        token = authority.issue_session(role="controller", client_id="A", transport="stdio")
        # Post, then have the *addressed recipient* acknowledge twice. Both
        # principals authenticate as controller A here (single-client test).
        posted = json.loads(mcp_server.mailbox_post(
            kind="handoff", recipient="mcp:controller:A", run_id="run-1",
            attempt=1, session_token=token,
        ))
        mid = posted["message"]["message_id"]
        first = json.loads(mcp_server.mailbox_ack(mid, session_token=token))
        second = json.loads(mcp_server.mailbox_ack(mid, session_token=token))
        assert first["status"] == second["status"] == "acked"
        assert first["message"]["message_id"] == second["message"]["message_id"]
        assert first["message"]["seq"] == second["message"]["seq"]

    def test_a_truncated_mailbox_refuses_a_stale_resume_token(self, tmp_path):
        mb = _mailbox(tmp_path)
        for i in range(3):
            mb.post(_msg(message_id=f"m{i}"))
        # Persist a cursor that pins the very first durable record.
        first = mb.read(recipient="worker-1", principal="mcp:controller:A")
        token = first[0]["resume_token"]
        assert MailboxCursor.decode(token).after_seq == 1
        # The mailbox is truncated from the head: the record the cursor pinned
        # is gone, so resuming would silently skip lost durable work.
        with open(mb.path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        with open(mb.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[1:])
        with pytest.raises(messaging.MessageChannelError) as exc:
            mb.read(
                recipient="worker-1", principal="mcp:controller:A",
                resume_token=token,
            )
        assert "truncated or rewritten" in str(exc.value)

    def test_a_tampered_cursor_token_is_a_typed_refusal(self):
        with pytest.raises(messaging.MessageChannelError):
            MailboxCursor.decode("not-a-real-token")

    def test_cursor_round_trips(self):
        cursor = MailboxCursor(after_seq=7, last_digest="abc")
        assert MailboxCursor.decode(cursor.encode()) == cursor


# ===========================================================================
# Criterion 3: explicit capability detection + polling fallback, same authority
# ===========================================================================


class TestCapabilityDetection:
    def test_the_three_named_clients_are_explicitly_detected(self):
        caps = runtime_handoff.detect_capabilities(
            skill_root_resolver=lambda c: f"/skills/{c}" if c in ("codex", "openclaude") else None,
            config_resolver=lambda c: c == "codex",
        )
        assert set(caps) == {"codex", "antigravity", "openclaude"}
        # Codex: skill root + MCP config -> notification support.
        assert caps["codex"].detected and caps["codex"].supports_notifications
        assert caps["codex"].adapter_kind() == runtime_handoff.TRANSPORT_MCP_NOTIFICATIONS
        # OpenClaude: skill root only, no notification config -> polling.
        assert caps["openclaude"].detected and not caps["openclaude"].supports_notifications
        assert caps["openclaude"].adapter_kind() == runtime_handoff.TRANSPORT_FILE_POLL
        # Antigravity: no positive signal -> detected False, polling.
        assert not caps["antigravity"].detected
        assert caps["antigravity"].adapter_kind() == runtime_handoff.TRANSPORT_FILE_POLL

    def test_an_undetected_client_falls_closed_to_polling(self, tmp_path):
        cap = runtime_handoff.detect_client_capability(
            "mystery-cli", skill_root_resolver=lambda c: None, config_resolver=lambda c: False,
        )
        assert cap.detected is False
        assert cap.adapter_kind() == runtime_handoff.TRANSPORT_FILE_POLL
        # Never silently capable: an unknown client cannot claim notifications.
        assert "mcp-notifications" not in cap.transports

    def test_explicit_attestation_wins_and_omitting_notifications_selects_polling(self):
        cap = runtime_handoff.detect_client_capability(
            "cli", attestation={"transports": ["cli-poll"]}
        )
        assert cap.adapter_kind() == runtime_handoff.TRANSPORT_FILE_POLL
        cap2 = runtime_handoff.detect_client_capability(
            "cli", attestation={"transports": ["mcp-notifications"]}
        )
        assert cap2.adapter_kind() == runtime_handoff.TRANSPORT_MCP_NOTIFICATIONS

    def test_detection_reads_a_positive_file_backed_signal(self, tmp_path, monkeypatch):
        # A real skill root directory (the installer's anchor) is the signal.
        skill_root = tmp_path / "skills"
        skill_root.mkdir()
        cap = runtime_handoff.detect_client_capability(
            "codex",
            skill_root_resolver=lambda c: str(skill_root) if skill_root.exists() else None,
            config_resolver=lambda c: False,
        )
        assert cap.detected is True
        assert "skill_root" in cap.features

    def test_no_skill_root_for_a_codex_client_anchor_is_a_real_union(self):
        # The resolver reads the real installer AGENT_CONFIG; codex is a target.
        names = {t.name for t in __import__("skillweave.installer", fromlist=["AGENT_CONFIG"]).AGENT_CONFIG}
        assert {"codex", "antigravity"} <= names


class TestAdaptersShareAuthority:
    def test_polling_and_notification_adapters_enforce_the_same_authority(self, tmp_path):
        mb = _mailbox(tmp_path)
        polling_cap = runtime_handoff.detect_client_capability(
            "antigravity", skill_root_resolver=lambda c: None, config_resolver=lambda c: False,
        )
        notify_cap = runtime_handoff.detect_client_capability(
            "codex", attestation={"transports": ["mcp-notifications"]},
        )
        polling = runtime_handoff.adapter_for_capability(polling_cap, mb)
        notify = runtime_handoff.adapter_for_capability(notify_cap, mb)
        polling.send(_msg(recipient="worker-x", principal="P", run_id="r"))
        # Both adapters read the same mailbox under the same scoped principal.
        assert len(notify.read(principal="P", recipient="worker-x")) == 1
        # Neither reveals a message to an unrelated principal.
        assert notify.read(principal="Q", recipient="worker-x") == []
        assert polling.read(principal="Q", recipient="worker-x") == []

    def test_a_notification_adapter_refuses_a_client_without_the_capability(self):
        cap = runtime_handoff.detect_client_capability(
            "antigravity", skill_root_resolver=lambda c: None, config_resolver=lambda c: False,
        )
        with pytest.raises(runtime_handoff.HandoffError):
            runtime_handoff.NotificationTransportAdapter(cap, object())

    def test_the_adapter_is_not_an_authority_source(self, tmp_path):
        mb = _mailbox(tmp_path)
        cap = runtime_handoff.detect_client_capability(
            "antigravity", skill_root_resolver=lambda c: None, config_resolver=lambda c: False,
        )
        adapter = runtime_handoff.adapter_for_capability(cap, mb)
        with pytest.raises(runtime_handoff.HandoffError):
            _ = adapter.principal  # must not default to a wildcard principal

    def test_agent_capabilities_tool_reports_contract_and_clients(self, authority):
        token = authority.issue_session(role="controller", client_id="A", transport="stdio")
        out = json.loads(mcp_server.agent_capabilities(session_token=token))
        assert out["status"] == "ok"
        assert set(out["clients"]) == {"codex", "antigravity", "openclaude"}
        assert out["contract"]["artifact"] == AGENT_MESSAGE_ARTIFACT
        assert out["contract"]["schema_digest"] == AGENT_MESSAGE_SCHEMA_DIGEST


# ===========================================================================
# Installed-origin proof (no stand-in module)
# ===========================================================================


def test_consumer_modules_are_the_installed_origin(tmp_path):
    """The mailbox, handoff and MCP server are this checkout's src modules.

    Proven in a fresh interpreter: the wiring asserted above is the *installed*
    product module, and the new tools are genuinely reachable from it.
    """
    probe = (
        "import json\n"
        "from skillweave.dispatch import messaging\n"
        "from skillweave.runtime import handoff\n"
        "from skillweave.mcp import server\n"
        "print(json.dumps({\n"
        "  'messaging_file': messaging.__file__,\n"
        "  'handoff_file': handoff.__file__,\n"
        "  'server_file': server.__file__,\n"
        "  'has_mailbox_post': callable(getattr(server, 'mailbox_post', None)),\n"
        "  'has_mailbox_read': callable(getattr(server, 'mailbox_read', None)),\n"
        "  'has_mailbox_replay': callable(getattr(server, 'mailbox_replay', None)),\n"
        "  'has_mailbox_ack': callable(getattr(server, 'mailbox_ack', None)),\n"
        "  'has_agent_capabilities': callable(getattr(server, 'agent_capabilities', None)),\n"
        "  'digest': messaging.AGENT_MESSAGE_SCHEMA_DIGEST,\n"
        "  'contract_digest': messaging.agent_message_contract_digest(),\n"
        "}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True, text=True, env=_child_env(), cwd=str(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    assert info["messaging_file"].endswith("skillweave/dispatch/messaging.py")
    assert info["handoff_file"].endswith("skillweave/runtime/handoff.py")
    assert info["server_file"].endswith("skillweave/mcp/server.py")
    assert info["has_mailbox_post"] and info["has_mailbox_read"]
    assert info["has_mailbox_replay"] and info["has_mailbox_ack"]
    assert info["has_agent_capabilities"]
    # The digest the record carries is the digest the contract computes.
    assert info["digest"] == info["contract_digest"] == AGENT_MESSAGE_SCHEMA_DIGEST


def test_produced_record_round_trips_through_the_frozen_contract(tmp_path):
    """A record the mailbox wrote decodes back under the pinned contract."""
    mb = _mailbox(tmp_path)
    record = mb.post(_msg(run_id="r", task_id="t", attempt=3, message_id="m"))
    # The digest the record carries matches its own content bytes.
    content = {k: record.get(k) for k in messaging._AGENT_MESSAGE_CONTENT_FIELDS}
    assert messaging._content_digest(content) == record["digest"]
    # And the record decodes through the same validator the reader uses.
    assert messaging._decode_record(record)["message_id"] == "m"
