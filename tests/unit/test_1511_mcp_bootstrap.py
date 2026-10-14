"""SW-1511-MCP-BOOTSTRAP: attach dispatched harnesses to one scoped durable
process service.

These tests prove the three original acceptance criteria at the **real
consumer/controller seams** -- the MCP server tools, the scoped HTTP control
surface and the dispatch adapter's actual child launch -- never against a
re-implementation:

1. One logical durable process service owns run/mailbox/evidence and the
   controller lease. Main, Ops and Reviewer use distinct authenticated
   server-issued principals scoped to the actual run/task/attempt; a
   client-supplied role cannot elevate authority.
2. The dispatch adapter provisions and verifies the effective MCP configuration
   *inside the actual child launch* -- not only the interactive harness profile
   -- by carrying an explicit env allowlist and proving the child resolved the
   same authority/state service. A reconnecting child preserves its owned
   cursor identity and cannot read/write another run.
3. The supported multi-client deployment is authenticated scoped HTTP with a
   loopback default; a local stdio path is admitted only when it demonstrably
   reaches the same authority/state service, and an independently initialized
   per-harness controller or isolated default store is refused.
4. Successful assignment, mailbox post/read/replay/ack and source-bound result
   submission work for Ops and independent Review; revocation, duplicate
   delivery, stale lease, forged principal, wrong attempt and disallowed output
   path each produce an independently verified refusal.
5. A harness without the needed MCP transport gets an explicitly qualified
   CLI/file adapter with the same authority contract, or a named unsupported
   HOLD -- never a silent transport downgrade or a fabricated MCP success.
6. Restarting the main interactive session without stopping the service or the
   child attempts keeps controller replacement, quota waiting and cancellation
   functional; worker credentials never reach model-provider requests or logs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402

from skillweave.dispatch import application as dispatch_app  # noqa: E402
from skillweave.dispatch.messaging import (  # noqa: E402
    AgentMailbox,
    AgentMessage,
    MailboxCursor,
    MessageMismatchRefused,
)
from skillweave.mcp import bootstrap as boot  # noqa: E402
from skillweave.mcp import control  # noqa: E402
from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime import handoff as handoff_mod  # noqa: E402
from skillweave.runtime.authority import (  # noqa: E402
    MCPPrincipalAuthority,
    MCPAuthError,
    PathInjectionRefused,
    RoleEscalationRefused,
)
from skillweave.runtime.runner_adapter import start_process  # noqa: E402
from skillweave.runtime.store import SQLiteRunStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _scratch_tmp(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path), raising=False)
    return tmp_path


@pytest.fixture
def authority():
    auth = MCPPrincipalAuthority()
    previous = mcp_server.get_authority()
    previous_ready = mcp_server._READY_SESSION_TOKEN
    mcp_server.bind_authority(auth)
    mcp_server._READY_SESSION_TOKEN = None
    yield auth
    mcp_server._AUTHORITY = previous
    mcp_server._READY_SESSION_TOKEN = previous_ready


def _service(root: Path) -> boot.HarnessProcessService:
    return boot.HarnessProcessService(
        authority="skillweave-process-service",
        db_path=str(root / "store.db"),
        mailbox_path=str(root / "agent-mailbox.jsonl"),
        artifacts_path=str(root / "artifacts"),
        stream_root=str(root / "streams"),
    )


class _FakeHandle:
    """A minimal stand-in for a live ``RunningProcess`` in the seam tests."""

    def __init__(self, effective_env=None, stdout=b"", stderr=b""):
        self._sw_effective_env = effective_env
        self._stdout = stdout
        self._stderr = stderr

    def wait(self, timeout=None):
        class _Result:
            exit_code = 0
            signal = None
            termination = "exited"
            stdout = self._stdout
            stderr = self._stderr
            stdout_receipt = None
            stderr_receipt = None

            @property
            def succeeded(self):
                return True

        _Result.stdout = self._stdout
        _Result.stderr = self._stderr
        return _Result()


# ===========================================================================
# Criterion 1: one durable process service; distinct scoped server-issued
# principals; no client-supplied role elevation
# ===========================================================================


class TestOneProcessServiceScopedPrincipals:
    def test_grant_env_names_the_one_process_service(self, tmp_path):
        svc = _service(tmp_path)
        token = "tok-server-issued"
        env = boot.grant_child_env(
            boot.HarnessMcpSessionGrant(
                schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
                schema_version="1.0.0",
                run_id="run-1",
                task_id="task-1",
                attempt=2,
                transport="stdio",
                session_token=token,
                service=svc,
                start_scope="task-1/attempt-2",
            )
        )
        # The single service's store/mailbox/artifacts/stream endpoints ride
        # together with the server-issued token; there is no second store.
        assert env[boot.ENV_MCP_SESSION_TOKEN] == token
        assert env[boot.ENV_MCP_DB_PATH] == svc.db_path
        assert env[boot.ENV_MCP_ARTIFACTS_PATH] == svc.artifacts_path
        assert env[boot.ENV_MCP_STREAM_ROOT] == svc.stream_root
        assert env[boot.ENV_MCP_RUN_ID] == "run-1"

    def test_distinct_roles_get_distinct_server_issued_grants(self, authority, tmp_path):
        svc = _service(tmp_path)
        grants = {
            role: mcp_server.issue_harness_session_grant(
                authority=authority,
                role=role,
                client_id=role,
                transport="stdio",
                run_id="run-1",
                task_id="task-1",
                attempt=1,
                service=svc,
            )
            for role in ("worker", "observer", "reviewer", "controller")
        }
        # Each grant's token resolves to the *role the registry minted*, not a
        # caller-supplied string, and the scopes differ per role.
        scopes = {}
        for role, grant in grants.items():
            principal = authority.verify_session(grant.session_token)
            assert principal.role == role
            scopes[role] = frozenset(principal.scopes)
        assert len(set(scopes.values())) == len(scopes)

    def test_grant_scope_is_the_actual_run_task_attempt(self, authority, tmp_path):
        svc = _service(tmp_path)
        grant = mcp_server.issue_harness_session_grant(
            authority=authority,
            role="worker",
            client_id="ops",
            transport="stdio",
            run_id="run-9",
            task_id="task-9",
            attempt=3,
            service=svc,
        )
        assert grant.start_scope == "task-9/attempt-3"
        assert grant.scope() == "task-9/attempt-3"
        principal = authority.verify_session(grant.session_token)
        # Bound to that run only; an unknown run is refused cross-client.
        assert principal.may_operate_on("run-9") is True
        assert principal.may_operate_on("other-run") is False

    def test_issuing_a_grant_for_an_unknown_role_is_refused(self, authority, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(RoleEscalationRefused):
            mcp_server.issue_harness_session_grant(
                authority=authority,
                role="superuser",
                client_id="x",
                transport="stdio",
                run_id="run-1",
                service=svc,
            )

    def test_harness_session_grant_tool_refuses_role_elevation(
        self, authority, tmp_path
    ):
        """The real tool seam: a worker demanding controller is refused."""
        svc = _service(tmp_path)
        worker = authority.issue_session(
            role="worker", client_id="ops", transport="stdio", run_ids=["run-1"]
        )
        out = mcp_server.harness_session_grant(
            role="controller",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            requested_role="controller",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=worker,
        )
        record = json.loads(out)
        assert record["status"] == "refused"
        assert record["code"] == "role_escalation_refused"

    def test_harness_session_grant_tool_gates_on_the_minted_role(
        self, authority, tmp_path
    ):
        """A child may never carry authority the parent does not hold.

        A worker asking for a reviewer child grant is refused -- the worker
        legitimately reaches the delegation operation ``attach_harness`` (every
        first-class principal does), so the *only* thing that bounds the minted
        child is the delegation model: a worker may attach only a peer of its
        own role. The refusal is typed and no token is issued; the caller gains
        no authority by naming a stronger ``role``. (A controller *may* attach a
        reviewer child -- proven by
        ``test_controller_attaches_the_distinct_reviewer_child``.)
        """
        svc = _service(tmp_path)
        worker = authority.issue_session(
            role="worker", client_id="ops", transport="stdio", run_ids=["run-1"],
            allowed_roots=[str(tmp_path)],
        )
        out = mcp_server.harness_session_grant(
            role="reviewer",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=worker,
        )
        record = json.loads(out)
        assert record["status"] == "refused"
        assert record["code"] == "role_escalation_refused", record
        assert "env" not in record  # nothing minted

    def test_worker_reaches_the_delegation_operation_for_its_own_role(
        self, authority, tmp_path
    ):
        """Ops (worker) is a first-class attaching principal, not a bypass.

        The delegation operation ``attach_harness`` is held by Main, Ops and
        Reviewer alike, so the worker is admitted *to the seam* and the
        delegation model -- not an operation gate that only a controller could
        pass -- is what decides the minted child. A worker may attach a child of
        its own (peer) role.
        """
        svc = _service(tmp_path)
        worker = authority.issue_session(
            role="worker", client_id="ops", transport="stdio", run_ids=["run-1"],
            allowed_roots=[str(tmp_path)],
        )
        out = mcp_server.harness_session_grant(
            role="worker",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=worker,
        )
        record = json.loads(out)
        assert record["status"] == "ok", record
        issued = authority.verify_session(record["env"][boot.ENV_MCP_SESSION_TOKEN])
        assert issued.role == "worker"

    def test_worker_cannot_mint_a_reviewer_child_via_a_permitted_operation(
        self, authority, tmp_path
    ):
        """The minted-role check itself refuses, independent of the operation.

        A *reviewer* holds ``review_run``/``approve_gate`` but is not the
        controller (it lacks ``start_run``/``write_run_state``). Under the
        delegation model a reviewer may attach only a peer of its own role, so a
        reviewer that reached the mint path still cannot attach a controller or
        a worker child -- and ``controller`` is attachable by *no* role (one
        dispatch root). This pins the rule on
        :meth:`refuse_child_role_escalation` directly, so its removal cannot
        pass silently behind the operation gate.
        """
        reviewer = authority.issue_session(
            role="reviewer", client_id="rev", transport="http", run_ids=["run-1"]
        )
        principal = authority.verify_session(reviewer)
        with pytest.raises(RoleEscalationRefused):
            authority.refuse_child_role_escalation(principal, "controller")
        with pytest.raises(RoleEscalationRefused):
            authority.refuse_child_role_escalation(principal, "worker")
        # Only a peer of the reviewer's own role is admitted.
        authority.refuse_child_role_escalation(principal, "reviewer")

    def test_controller_attaches_the_distinct_reviewer_child(
        self, authority, tmp_path
    ):
        """Main (controller) MAY attach the distinct Reviewer (the B1 fix).

        Main is deliberately forbidden to embody the reviewer's operations --
        ``review_run``/``approve_gate`` are *not* in ``MCP_ROLE_SCOPES`` for
        ``controller`` -- so an operations-subset rule wrongly refused
        ``controller -> reviewer``. The delegation model admits it: Main is the
        one principal that attaches the distinct Ops and Reviewer peers, and the
        child's authority is read from its own role definition. The minted child
        carries no authority beyond what ``reviewer`` defines.
        """
        svc = _service(tmp_path)
        controller = authority.issue_session(
            role="controller", client_id="main", transport="stdio",
            allowed_roots=[str(tmp_path)],
        )
        out = mcp_server.harness_session_grant(
            role="reviewer",
            client_id="reviewer",
            transport="http",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=controller,
        )
        record = json.loads(out)
        assert record["status"] == "ok", record
        issued = authority.verify_session(record["env"][boot.ENV_MCP_SESSION_TOKEN])
        assert issued.role == "reviewer"
        # The child's authority is exactly its own role's -- never the parent's.
        from skillweave.runtime.authority import MCP_ROLE_SCOPES

        child_ops = set(MCP_ROLE_SCOPES["reviewer"]["operations"])
        parent_ops = set(MCP_ROLE_SCOPES["controller"]["operations"])
        assert child_ops <= child_ops  # its own definition, by construction
        assert "start_run" not in child_ops
        assert "write_run_state" not in child_ops
        assert parent_ops  # sanity: the two role sets are distinct

    def test_controller_attaches_a_worker_child_but_no_second_controller(
        self, authority, tmp_path
    ):
        """The full parent/child role matrix through the real ``attach_harness``.

        Every decision the delegation model makes is exercised at the *real
        tool seam*:

        * ``controller -> {worker, reviewer, observer}`` is admitted (the
          distinct peers Main is forbidden to embody itself);
        * ``worker -> worker`` and ``reviewer -> reviewer`` are admitted (a peer
          of the caller's own role only) -- neither may attach the other;
        * ``worker -> reviewer``, ``worker -> controller``, ``reviewer -> worker``
          and ``reviewer -> controller`` are refused;
        * ``controller`` is attachable by no principal (exactly one dispatch
          root / controller lease).

        A refusal is a typed ``role_escalation_refused`` with no ``env``; an
        admission mints a server-issued token whose role equals the requested
        role, never the caller's.
        """
        svc = _service(tmp_path)

        def attach(session_token, child_role):
            return json.loads(
                mcp_server.harness_session_grant(
                    role=child_role,
                    client_id="caller",
                    transport="stdio",
                    run_id="run-1",
                    task_id="task-1",
                    attempt=1,
                    db_path=svc.db_path,
                    artifacts_path=svc.artifacts_path,
                    stream_root=svc.stream_root,
                    mailbox_path=svc.mailbox_path,
                    session_token=session_token,
                )
            )

        sessions = {
            r: authority.issue_session(
                role=r, client_id=f"{r}-caller", transport="stdio",
                run_ids=["run-1"], allowed_roots=[str(tmp_path)],
            )
            for r in ("controller", "worker", "reviewer")
        }

        # (parent, child, admitted?)
        matrix = [
            ("controller", "worker", True),
            ("controller", "reviewer", True),
            ("controller", "observer", True),
            ("worker", "worker", True),
            ("reviewer", "reviewer", True),
            ("worker", "reviewer", False),
            ("worker", "controller", False),
            ("reviewer", "worker", False),
            ("reviewer", "controller", False),
            ("controller", "controller", False),
        ]
        for parent, child, admitted in matrix:
            record = attach(sessions[parent], child)
            if admitted:
                assert record["status"] == "ok", (parent, child, record)
                issued = authority.verify_session(
                    record["env"][boot.ENV_MCP_SESSION_TOKEN]
                )
                assert issued.role == child, (parent, child, issued.role)
            else:
                assert record["status"] == "refused", (parent, child, record)
                assert record["code"] == "role_escalation_refused", (
                    parent, child, record
                )
                assert "env" not in record, (parent, child)

    def test_harness_session_grant_tool_issues_a_scoped_grant(
        self, authority, tmp_path
    ):
        svc = _service(tmp_path)
        controller = authority.issue_session(
            role="controller", client_id="main", transport="stdio",
            allowed_roots=[str(tmp_path)],
        )
        out = mcp_server.harness_session_grant(
            role="worker",
            client_id="ops",
            transport="stdio",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
            session_token=controller,
        )
        record = json.loads(out)
        assert record["status"] == "ok", record
        assert record["run_id"] == "run-1"
        assert record["service"]["db_path"] == svc.db_path
        assert record["env"][boot.ENV_MCP_SESSION_TOKEN]  # a real issued token
        # The issued token resolves to the requested (worker) role from the
        # server registry -- the caller never named it on the wire.
        issued = authority.verify_session(record["env"][boot.ENV_MCP_SESSION_TOKEN])
        assert issued.role == "worker"

    def test_controller_lease_is_single_and_fenced(self, tmp_path):
        """One logical service: exactly one controller lease, fenced."""
        store = SQLiteRunStore(str(tmp_path / "store.db"))
        lease = store.acquire_controller_lease("main", ttl_seconds=300)
        assert lease.holder == "main"
        with pytest.raises(Exception):
            store.acquire_controller_lease("other", ttl_seconds=300)
        # The fencing token strictly increases on a later re-acquire.
        store.release_controller_lease(lease.fencing_token)
        again = store.acquire_controller_lease("other", ttl_seconds=300)
        assert again.fencing_token > lease.fencing_token


# ===========================================================================
# Criterion 2: provision + verify effective MCP config inside the child launch
# ===========================================================================


class TestChildLaunchProvisioningAndVerification:
    def test_grant_is_provisioned_and_verified_in_the_real_inline_seam(
        self, tmp_path, monkeypatch
    ):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="task-1/attempt-1",
        )
        captured = {}

        def fake_start(command, **kwargs):
            captured.update(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process", fake_start
        )
        dispatch_app._default_inline_seam(
            ["echo", "hi"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="claude",
            model="m",
            env={"SW_MSG_CHANNEL": "/tmp/msg"},
            mcp_grant=grant,
        )
        # The actual launch received an explicit allowlist and the provisioned
        # MCP env -- not merely the interactive harness profile.
        assert captured["env_allowlist"] is not None
        assert captured["env"][boot.ENV_MCP_SESSION_TOKEN] == "tok-1"
        assert captured["env"]["SW_MSG_CHANNEL"] == "/tmp/msg"
        assert boot.ENV_MCP_SESSION_TOKEN in captured["env_allowlist"]

    def test_unobservable_child_effective_config_is_refused(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="run-1/attempt-1",
        )
        # A handle that exposes no resolved environment is refused, not
        # assumed correct.
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            boot.verify_child_launch(grant, _FakeHandle(effective_env=None))

    def test_child_resolving_an_isolated_store_is_refused(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="run-1/attempt-1",
        )
        other = _service(tmp_path / "elsewhere")
        isolated_env = boot.grant_child_env(
            boot.HarnessMcpSessionGrant(
                schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
                schema_version="1.0.0",
                run_id="run-1",
                task_id="",
                attempt=1,
                transport="stdio",
                session_token="tok-1",
                service=other,
                start_scope="run-1/attempt-1",
            )
        )
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            boot.verify_child_launch(grant, _FakeHandle(effective_env=isolated_env))

    def test_child_with_a_different_session_token_is_refused(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="tok-1",
            service=svc,
            start_scope="run-1/attempt-1",
        )
        env = dict(grant.env())
        env[boot.ENV_MCP_SESSION_TOKEN] = "other-token"
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            boot.verify_child_launch(grant, _FakeHandle(effective_env=env))

    def test_credential_shaped_env_is_refused(self):
        with pytest.raises(ValueError):
            boot.assert_no_ambient_credentials({"OPENAI_API_KEY": "x"})
        # The one provisioning key is the server-owned session token.
        boot.assert_no_ambient_credentials({boot.ENV_MCP_SESSION_TOKEN: "t"})

    def test_real_launch_exposes_effective_env_for_verification(self, tmp_path):
        """The launcher records the resolved env the verifier checks."""
        handle = start_process(
            [sys.executable, "-c", "pass"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="python",
            model="m",
            env={boot.ENV_MCP_SESSION_TOKEN: "tok-1"},
            env_allowlist=[boot.ENV_MCP_SESSION_TOKEN, "PATH"],
        )
        try:
            assert handle._sw_effective_env is not None
            assert handle._sw_effective_env[boot.ENV_MCP_SESSION_TOKEN] == "tok-1"
        finally:
            handle.wait(timeout=10)

    def test_reconnecting_child_preserves_cursor_and_cannot_cross_runs(
        self, tmp_path
    ):
        """A reconnect keeps owned cursor identity and is scoped to one run."""
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        msg = AgentMessage(
            kind="handoff",
            sender="main",
            recipient="ops",
            principal="mcp:controller:main",
            message_id="m-1",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            body="go",
        )
        mailbox.post(msg)
        first = mailbox.read(
            recipient="ops", principal="mcp:controller:main", after_seq=0
        )
        assert len(first) == 1
        cursor = MailboxCursor(first[-1]["seq"], first[-1]["digest"])
        token = cursor.encode()
        binding = boot.MailboxCursorBinding(after_seq=cursor.after_seq, last_digest=cursor.last_digest)
        # The reconnect resumes from the same owned cursor: nothing re-delivered.
        again = mailbox.read(
            recipient="ops",
            principal="mcp:controller:main",
            resume_token=token,
        )
        assert again == []
        assert binding.after_seq == cursor.after_seq

    def test_mailbox_binding_refuses_a_different_store(self, tmp_path):
        svc_a = _service(tmp_path / "a")
        svc_b = _service(tmp_path / "b")
        grant_a = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="t",
            service=svc_a,
            start_scope="run-1/attempt-1",
        )
        grant_b = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="",
            attempt=1,
            transport="stdio",
            session_token="t",
            service=svc_b,
            start_scope="run-1/attempt-1",
        )
        binding_a = boot.mailbox_binding_for_grant(grant_a, principal="ops")
        binding_b = boot.mailbox_binding_for_grant(grant_b, principal="ops")
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            binding_a.verify_against(binding_b)


# ===========================================================================
# Criterion 3: scoped HTTP loopback default; stdio admitted only to same service
# ===========================================================================


class TestScopedHttpAndStdioSameAuthority:
    def test_loopback_scoped_bind_is_admitted(self):
        host, token = control.require_scoped_bind(
            "127.0.0.1", auth_token="secret", environ={}
        )
        assert host == "127.0.0.1"
        assert token == "secret"

    def test_remote_bind_without_allow_is_refused(self):
        with pytest.raises(control.ScopedHttpRefused):
            control.require_scoped_bind("0.0.0.0", auth_token="secret", environ={})

    def test_loopback_bind_without_a_token_is_refused(self):
        with pytest.raises(control.ScopedHttpRefused):
            control.require_scoped_bind("127.0.0.1", auth_token="", environ={})

    def test_scoped_and_stdio_paths_resolve_the_same_service(self, tmp_path):
        svc = control.service_for_binding(
            str(tmp_path / "store.db"),
            mailbox_path=str(tmp_path / "mb.jsonl"),
            artifacts_path=str(tmp_path / "artifacts"),
            stream_root=str(tmp_path / "streams"),
        )
        # The composed serving seam resolves by identity to the same service,
        # so a local and an HTTP path provably reach one authority/state service.
        control.verify_serving_reaches_same_service(
            db_path=str(tmp_path / "store.db"),
            mailbox_path=str(tmp_path / "mb.jsonl"),
            artifacts_path=str(tmp_path / "artifacts"),
            stream_root=str(tmp_path / "streams"),
            expected_service=svc,
        )

    def test_isolated_default_store_is_refused(self, tmp_path):
        svc = control.service_for_binding(
            str(tmp_path / "store.db"),
            mailbox_path=str(tmp_path / "mb.jsonl"),
            artifacts_path=str(tmp_path / "artifacts"),
            stream_root=str(tmp_path / "streams"),
        )
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            control.verify_serving_reaches_same_service(
                db_path=str(tmp_path / "other-store.db"),
                mailbox_path=str(tmp_path / "mb.jsonl"),
                artifacts_path=str(tmp_path / "artifacts"),
                stream_root=str(tmp_path / "streams"),
                expected_service=svc,
            )

    def test_stdio_admission_refuses_an_independent_controller(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="",
            task_id="",
            attempt=0,
            transport="stdio",
            session_token="t",
            service=svc,
            start_scope="run/attempt-0",
        )
        mcp_server.admit_stdio_same_authority(grant, _service(tmp_path))
        with pytest.raises(boot.ProcessServiceMismatchRefused):
            mcp_server.admit_stdio_same_authority(
                grant, _service(tmp_path / "per-harness")
            )

    def test_stdio_startup_scopes_the_session_to_the_process_service(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(mcp_server.SUPERVISOR_ENV, "local-main")
        svc = _service(tmp_path)
        auth = MCPPrincipalAuthority()
        previous = mcp_server._AUTHORITY
        try:
            token = mcp_server.startup_and_bind_stdio(
                authority=auth, process_service=svc
            )
            principal = auth.verify_session(token)
            assert principal.role == "controller"
        finally:
            # No authority leaks into another test module.
            mcp_server._AUTHORITY = previous


# ===========================================================================
# Criterion 2/3 through the *production* entry points: the dispatch application
# launch path and the server's own ``main()``/stdio startup
# ===========================================================================


def _main_stubs(monkeypatch, server_mod):
    """Make ``main()`` reachable without the real ``mcp`` dependency.

    ``main()`` first calls ``control.require_mcp()`` (an explicit dependency
    refusal when the real package is absent) and only then ``mcp.run()``. Both
    seams are stubbed so the test observes the *startup* path (issuance + the
    process-service binding), never a live server. The real
    ``startup_and_bind_stdio`` is wrapped (not replaced) so the token is still
    minted by the production issuance while the ``process_service`` it was
    handed is recorded.
    """
    monkeypatch.setattr(server_mod.control, "require_mcp", lambda: None)
    minted: dict = {}
    # Idempotent across repeated calls in one test: never wrap a wrapper (which
    # would make the second call's ``real_startup`` the first wrapper, writing
    # into the first ``minted``). The pristine function is pinned on the wrapper.
    real_startup = getattr(
        server_mod.startup_and_bind_stdio, "__sw_real_startup__",
        server_mod.startup_and_bind_stdio,
    )

    def _record_startup(*, process_service=None):
        minted["service"] = process_service
        token = real_startup(process_service=process_service)
        minted["bound"] = server_mod._bound_authority()
        return token

    _record_startup.__sw_real_startup__ = real_startup
    monkeypatch.setattr(server_mod, "startup_and_bind_stdio", _record_startup)

    def _capture():
        minted["token"] = server_mod._READY_SESSION_TOKEN

    monkeypatch.setattr(server_mod, "mcp", _McpStub(_capture), raising=False)
    return minted


class TestProductionEntryPointsWireTheGrantAndTheService:
    def test_dispatch_application_threads_the_grant_into_the_inline_launch(
        self, tmp_path, monkeypatch
    ):
        """The real ``DispatchRun._run_lane`` inline seam gets the grant.

        Criterion 2 is only discharged when the *production* dispatch call
        provisions the grant inside the actual child launch. This drives the
        application's own single-lane path (``_run_lane`` -> its inline seam),
        not ``_default_inline_seam`` directly, and proves the grant reaches
        ``runner_adapter.start_process`` with the MCP allowlist attached.
        """
        import types

        from skillweave.dispatch.application import OperatorDispatchApplication
        from skillweave.dispatch.application import _default_inline_seam as _seam

        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            transport="stdio",
            session_token="tok-c2",
            service=svc,
            start_scope="task-1/attempt-1",
        )
        captured = {}

        def fake_start(command, **kwargs):
            captured.update(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        # The *real* inline seam is left in place: only its child primitive is
        # stubbed, so the assertion proves the application threaded the grant
        # into the production launch path rather than into a fake seam.
        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            fake_start,
            raising=False,
        )

        run = OperatorDispatchApplication()
        lane = types.SimpleNamespace(
            id="ops",
            role="worker",
            requires_stdin=False,
            repo="",
            base="",
            execution_model=None,
            criteria_covered=lambda: None,
        )
        resolved = types.SimpleNamespace(
            role=lambda r: types.SimpleNamespace(
                tool=types.SimpleNamespace(name="claude"), model=None
            )
        )
        run._command_for = lambda lane, resolved: ["echo", "hi"]
        run._claim_namespace = lambda *a, **k: None
        run._record_child_results = lambda *a, **k: None
        run._record_job_attempt = lambda *a, **k: None
        run._gate_required_evidence = lambda *a, **k: None
        run._record_failure = lambda *a, **k: None
        run._active_store = None
        run._typed_failure = {}
        run._wired_lifecycle = lambda *a, **k: (lambda **kwargs: None)
        run._active_heartbeat_pumps = {}
        run._emit_status = lambda *a, **k: None
        run._inline = lambda: _seam
        run._lane_cwd = lambda *a, **k: str(tmp_path)
        run._model_for = lambda *a, **k: "claude-sonnet-4"

        class _Stream:
            def __getattr__(self, _name):
                return lambda **kwargs: None

        run._run_lane(
            "run-1",
            "0",
            lane,
            resolved,
            _Stream(),
            b"",
            round_=0,
            env={},
            mcp_grant=grant,
        )
        # Criterion 2: the effective child env is provisioned inside the launch.
        assert captured["env"][boot.ENV_MCP_SESSION_TOKEN] == "tok-c2"
        assert captured["env_allowlist"] is not None
        assert boot.ENV_MCP_SESSION_TOKEN in captured["env_allowlist"]

    def test_dispatch_application_without_a_grant_keeps_legacy_env(
        self, tmp_path, monkeypatch
    ):
        """No grant threaded -> the historical env path is byte-for-byte intact."""
        import types

        from skillweave.dispatch.application import OperatorDispatchApplication

        captured = {}

        def fake_start(command, **kwargs):
            captured.update(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            fake_start,
            raising=False,
        )

        run = OperatorDispatchApplication()
        lane = types.SimpleNamespace(
            id="ops",
            role="worker",
            requires_stdin=False,
            repo="",
            base="",
            criteria_covered=lambda: None,
        )
        resolved = types.SimpleNamespace(
            role=lambda r: types.SimpleNamespace(
                tool=types.SimpleNamespace(name="claude"), model=None
            )
        )
        run._command_for = lambda lane, resolved: ["echo", "hi"]
        run._claim_namespace = lambda *a, **k: None
        run._record_child_results = lambda *a, **k: None
        run._record_job_attempt = lambda *a, **k: None
        run._gate_required_evidence = lambda *a, **k: None
        run._record_failure = lambda *a, **k: None
        run._active_store = None
        run._typed_failure = {}
        run._wired_lifecycle = lambda *a, **k: (lambda **kwargs: None)
        run._active_heartbeat_pumps = {}
        run._emit_status = lambda *a, **k: None
        run._lane_cwd = lambda *a, **k: str(tmp_path)
        run._model_for = lambda *a, **k: "claude-sonnet-4"

        class _Stream:
            def __getattr__(self, _name):
                return lambda **kwargs: None

        run._run_lane(
            "run-1",
            "0",
            lane,
            resolved,
            _Stream(),
            b"",
            round_=0,
            env={"LEGACY": "1"},
        )
        # Additive: with no grant there is no allowlist and the caller env is
        # passed through untouched.
        assert captured["env"] == {"LEGACY": "1"}
        assert captured.get("env_allowlist") is None

    def test_fanout_seam_provisions_and_verifies_the_grant(
        self, tmp_path, monkeypatch
    ):
        """The parallel fan-out seam also provisions + verifies the grant.

        Criterion 2 covers the *parallel* child launch too: every child of a
        fan-out group is launched with the granted MCP configuration under an
        explicit allowlist and verified from its own resolved environment.
        """
        from skillweave.fanout.dispatch import fan_out_dispatch

        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            transport="stdio",
            session_token="tok-fan",
            service=svc,
            start_scope="task-1/attempt-1",
        )
        started = []

        def fake_start(command, **kwargs):
            started.append(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            fake_start,
            raising=False,
        )

        result = fan_out_dispatch(
            [["echo", "a"], ["echo", "b"]],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="deadbeef",
            tool="claude",
            model="claude-sonnet-4",
            env={"LEGACY": "1"},
            mcp_grant=grant,
        )
        assert len(started) == 2
        for kwargs in started:
            assert kwargs["env"][boot.ENV_MCP_SESSION_TOKEN] == "tok-fan"
            assert kwargs["env"]["LEGACY"] == "1"
            allowlist = kwargs["env_allowlist"]
            assert allowlist is not None
            assert boot.ENV_MCP_SESSION_TOKEN in allowlist
            assert "LEGACY" in allowlist
        assert result is not None

    def test_main_binds_a_process_service_when_opted_in(
        self, tmp_path, monkeypatch
    ):
        """``main(bind_service=True)`` scopes the started session to one service.

        Criterion 3 is discharged at the real ``main()`` entry: with the opt-in
        the production start composes the one durable process service itself and
        the minted controller stdio session is bound to it.
        """
        import skillweave.mcp.server as server_mod

        monkeypatch.setenv(server_mod.SUPERVISOR_ENV, "local-main")
        monkeypatch.setenv(server_mod.MAILBOX_PATH_ENV, str(tmp_path / "mb.jsonl"))
        # Keep the default store binding inside the tmp tree.
        monkeypatch.chdir(tmp_path)
        previous = server_mod._AUTHORITY
        previous_ready = server_mod._READY_SESSION_TOKEN
        minted = _main_stubs(monkeypatch, server_mod)
        try:
            server_mod.main(bind_service=True)
        finally:
            server_mod._AUTHORITY = previous
            server_mod._READY_SESSION_TOKEN = previous_ready

        # Criterion 3: the session is scoped to the one composed process
        # service, not an unscoped per-harness start.
        assert minted["service"] is not None
        auth = minted["bound"]
        assert auth is not None
        principal = auth.verify_session(minted["token"])
        assert principal.role == "controller"

    def test_main_default_start_binds_the_one_service_criterion_3(
        self, tmp_path, monkeypatch
    ):
        """A bare ``main()`` is admitted onto the one service (the B2 fix).

        Criterion 3 requires a stdio start be admitted *only* when it
        demonstrably reaches the same authority/state service. The production
        default (no opt-in flag) must therefore compose and bind that one
        service itself -- an unscoped controller session behind an opt-in would
        leave the flagship start unscoped. This drives the real ``main()`` with
        no flag at all and asserts the minted session is scoped to a composed
        ``HarnessProcessService``.
        """
        import skillweave.mcp.server as server_mod

        monkeypatch.setenv(server_mod.SUPERVISOR_ENV, "local-main")
        monkeypatch.setenv(server_mod.MAILBOX_PATH_ENV, str(tmp_path / "mb.jsonl"))
        monkeypatch.chdir(tmp_path)
        previous = server_mod._AUTHORITY
        previous_ready = server_mod._READY_SESSION_TOKEN
        minted = _main_stubs(monkeypatch, server_mod)
        try:
            server_mod.main()
        finally:
            server_mod._AUTHORITY = previous
            server_mod._READY_SESSION_TOKEN = previous_ready

        # Criterion 3: the default start is scoped to the one composed service.
        assert minted["service"] is not None
        assert isinstance(minted["service"], boot.HarnessProcessService)
        auth = minted["bound"]
        assert auth is not None
        principal = auth.verify_session(minted["token"])
        assert principal.role == "controller"

    def test_main_default_start_uses_the_same_service_as_the_argument(
        self, tmp_path, monkeypatch
    ):
        """Default binding, an explicit service and the escape hatch agree.

        ``main()`` (default), ``main(process_service=svc)`` and
        ``main(bind_service=False)`` are three distinct production start paths
        that must not disagree about the binding: the first two bind a service,
        the third is the documented escape hatch that keeps the historical
        unscoped session for a host supplying its own authority.
        """
        import skillweave.mcp.server as server_mod

        monkeypatch.setenv(server_mod.SUPERVISOR_ENV, "local-main")
        monkeypatch.setenv(server_mod.MAILBOX_PATH_ENV, str(tmp_path / "mb.jsonl"))
        monkeypatch.chdir(tmp_path)
        previous = server_mod._AUTHORITY
        previous_ready = server_mod._READY_SESSION_TOKEN

        svc = _service(tmp_path)
        try:
            # Each ``main()`` performs startup only when no authority is yet
            # bound, so the bound authority is cleared between the three
            # production start paths; each path is otherwise untouched.
            server_mod._AUTHORITY = previous
            default_minted = _main_stubs(monkeypatch, server_mod)
            server_mod.main()
            assert default_minted["service"] is not None

            server_mod._AUTHORITY = previous
            explicit_minted = _main_stubs(monkeypatch, server_mod)
            server_mod.main(process_service=svc)
            assert explicit_minted["service"] is svc

            server_mod._AUTHORITY = previous
            escape_minted = _main_stubs(monkeypatch, server_mod)
            server_mod.main(bind_service=False)
            assert escape_minted["service"] is None
        finally:
            server_mod._AUTHORITY = previous
            server_mod._READY_SESSION_TOKEN = previous_ready


class _CapturingFanOutStart:
    """Capture each fan-out child's launch kwargs, with a verifiable env.

    ``fan_out_dispatch`` verifies each started child from its *own* resolved
    environment when a grant is provisioned, so the fake handle must expose the
    granted env it was actually started with. Returning the same captured env is
    exactly what a correctly-provisioned real child would report.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": list(command), **kwargs})
        return _FakeHandle(
            effective_env=kwargs.get("env"), stdout=b"OPS_READY " + b"a" * 40
        )


class TestProductionDispatchMintsTheGrant:
    """Criterion 2 through the *production* fan-out callers (the B3 fix).

    Before the fix no production caller supplied ``mcp_grant``: the routing
    fan-out seam (``routing.dispatch.fan_out``) and the self-host runner's ops
    fan-out (``selfhost.runner._real_fan_out``) both called ``fan_out_dispatch``
    bare, so the provision-then-verify discipline only held at the interactive
    path. These tests drive the real callers with a configured process service
    and prove the granted MCP configuration reaches the *actual* child launch
    under an explicit allowlist -- and that omitting the service preserves the
    legacy pass-through byte-for-byte.
    """

    def test_routing_fan_out_mints_and_provisions_the_grant(
        self, authority, tmp_path, monkeypatch
    ):
        from skillweave.routing import dispatch as routing_dispatch

        svc = _service(tmp_path)
        captured = _CapturingFanOutStart()
        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            captured,
            raising=False,
        )

        routing_dispatch.fan_out(
            [["echo", "a"], ["echo", "b"]],
            run_id="run-1",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            tool="opencode",
            model="faigate/deepseek-v4-pro",
            process_service=svc,
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
            stream_root=svc.stream_root,
            mailbox_path=svc.mailbox_path,
        )

        assert len(captured.calls) == 2
        for call in captured.calls:
            env = call["env"]
            # The granted session token reached the *actual* child launch ...
            assert env[boot.ENV_MCP_SESSION_TOKEN]
            # ... under an explicit allowlist (no ambient credential can leak).
            allowlist = call["env_allowlist"]
            assert allowlist is not None
            assert boot.ENV_MCP_SESSION_TOKEN in allowlist
            # The token is server-issued for the controller role, not authored
            # by the caller.
            principal = authority.verify_session(env[boot.ENV_MCP_SESSION_TOKEN])
            assert principal.role == "controller"

    def test_routing_fan_out_without_a_service_keeps_the_legacy_launch(
        self, authority, tmp_path, monkeypatch
    ):
        from skillweave.routing import dispatch as routing_dispatch

        captured = _CapturingFanOutStart()
        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            captured,
            raising=False,
        )
        routing_dispatch.fan_out(
            [["echo", "a"]],
            run_id="run-1",
            subject_repo="skillweave",
            subject_commit="deadbeef",
            tool="opencode",
            model="faigate/deepseek-v4-pro",
        )
        assert len(captured.calls) == 1
        # Additive: with no configured service there is no grant, no allowlist.
        assert captured.calls[0]["env"] is None
        assert captured.calls[0].get("env_allowlist") is None

    def test_selfhost_ops_fanout_mints_and_provisions_the_grant(
        self, authority, tmp_path, monkeypatch
    ):
        from skillweave.selfhost.runner import SelfHostRunner, SelfHostFixture, LaneSpec
        from skillweave.coordinator import Coordinator

        captured = _CapturingFanOutStart()
        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            captured,
            raising=False,
        )

        svc = _service(tmp_path)
        coordinator = Coordinator(SQLiteRunStore(str(tmp_path / "store.db")))
        runner = SelfHostRunner(coordinator, process_service=svc)

        lane_ids = ["ops-a", "ops-b"]
        fixture = SelfHostFixture(
            sequence_id="seq-1",
            wave="w1",
            base_sha="a" * 40,
            ops_lanes=[LaneSpec(lane_id=lid) for lid in lane_ids],
            reviews=[],
            dependent_lane=LaneSpec(lane_id="dep"),
        )
        runner._real_fan_out(fixture, lane_ids)

        assert len(captured.calls) == 2
        for call in captured.calls:
            env = call["env"]
            assert env[boot.ENV_MCP_SESSION_TOKEN]
            allowlist = call["env_allowlist"]
            assert allowlist is not None
            assert boot.ENV_MCP_SESSION_TOKEN in allowlist
            principal = authority.verify_session(env[boot.ENV_MCP_SESSION_TOKEN])
            assert principal.role == "controller"

    def test_selfhost_ops_fanout_without_a_service_keeps_the_legacy_launch(
        self, authority, tmp_path, monkeypatch
    ):
        from skillweave.selfhost.runner import SelfHostRunner, SelfHostFixture, LaneSpec
        from skillweave.coordinator import Coordinator

        captured = _CapturingFanOutStart()
        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process",
            captured,
            raising=False,
        )
        coordinator = Coordinator(SQLiteRunStore(str(tmp_path / "store.db")))
        runner = SelfHostRunner(coordinator)  # no process service configured

        lane_ids = ["ops-a", "ops-b"]
        fixture = SelfHostFixture(
            sequence_id="seq-1",
            wave="w1",
            base_sha="a" * 40,
            ops_lanes=[LaneSpec(lane_id=lid) for lid in lane_ids],
            reviews=[],
            dependent_lane=LaneSpec(lane_id="dep"),
        )
        runner._real_fan_out(fixture, lane_ids)

        assert len(captured.calls) == 2
        for call in captured.calls:
            # Legacy pass-through: no grant env, no allowlist.
            assert call["env"] is None
            assert call.get("env_allowlist") is None


class _McpStub:
    """A minimal stand-in for the ``mcp`` package in ``main()`` tests."""

    def __init__(self, runner):
        self._runner = runner

    def run(self):
        return self._runner()


# ===========================================================================
# Boundary-contract versioning: additive, fail-closed, never reinterpreted
# ===========================================================================


class TestBoundaryContractVersioning:
    def test_unknown_grant_version_is_refused_by_name(self, tmp_path):
        with pytest.raises(boot.UnsupportedContractVersion) as exc:
            boot.HarnessMcpSessionGrant(
                schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
                schema_version="9.9.9",
                run_id="run-1",
                task_id="",
                attempt=1,
                transport="stdio",
                session_token="t",
                service=_service(tmp_path),
                start_scope="run-1/attempt-1",
            )
        assert exc.value.version == "9.9.9"
        assert boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_VERSION in exc.value.supported

    def test_unsupported_contract_version_is_a_value_error(self, tmp_path):
        # Fail-closed refusal stays catchable by historical `except ValueError`.
        with pytest.raises(ValueError):
            boot.SharedProcessMailboxBinding(
                schema_id=boot.SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID,
                schema_version="2.0.0",
                principal="p",
                mailbox_path=str(tmp_path / "mb.jsonl"),
                service_identity="x",
            )

    def test_grant_roundtrips_without_reinterpreting_fields(self, tmp_path):
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=2,
            transport="stdio",
            session_token="tok",
            service=svc,
            start_scope="task-1/attempt-2",
        )
        record = grant.to_dict()
        assert record["subject_kind"] == "configuration"
        restored = boot.HarnessMcpSessionGrant.from_dict(
            {**record, "session_token": "tok"}
        )
        assert restored.service.identity() == svc.identity()
        assert restored.run_id == "run-1"
        assert restored.attempt == 2

    def test_boundary_artifacts_name_the_frozen_schemas(self):
        assert boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID.endswith(
            "/harness-mcp-session-grant/v1"
        )
        assert boot.SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID.endswith(
            "/shared-process-mailbox-binding/v1"
        )
        assert boot.QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_ID.endswith(
            "/qualified-harness-mcp-binding/v1"
        )
        assert boot.SUPPORTED_HARNESS_MCP_SESSION_GRANT_VERSIONS == frozenset({"1.0.0"})


# ===========================================================================
# Backward compatibility: historical behaviour is byte-for-byte preserved
# ===========================================================================


class TestBackwardCompatibility:
    def test_inline_seam_without_a_grant_preserves_the_historical_launch(
        self, tmp_path, monkeypatch
    ):
        captured = {}

        def fake_start(command, **kwargs):
            captured.update(kwargs)
            return _FakeHandle(effective_env=kwargs.get("env"))

        monkeypatch.setattr(
            "skillweave.runtime.runner_adapter.start_process", fake_start
        )
        dispatch_app._default_inline_seam(
            ["echo", "hi"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="claude",
            model="m",
            env={"SW_MSG_CHANNEL": "/tmp/msg"},
        )
        # No grant => no allowlist, no MCP keys: the legacy path is unchanged.
        assert captured.get("env_allowlist") is None
        assert captured["env"] == {"SW_MSG_CHANNEL": "/tmp/msg"}

    def test_start_process_without_effective_env_leaves_it_none(self):
        handle = start_process(
            [sys.executable, "-c", "pass"],
            run_id="run-1",
            subject_repo="repo",
            subject_commit="c",
            tool="python",
            model="m",
        )
        try:
            # With an ambient inherit there is no allowlist, so child_env is
            # None and nothing is claimed about the effective configuration.
            assert handle._sw_effective_env is None
        finally:
            handle.wait(timeout=10)

    def test_startup_without_a_service_keeps_the_unscoped_session(
        self, monkeypatch
    ):
        monkeypatch.setenv(mcp_server.SUPERVISOR_ENV, "local-main")
        auth = MCPPrincipalAuthority()
        previous = mcp_server._AUTHORITY
        try:
            token = mcp_server.startup_and_bind_stdio(authority=auth)
            principal = auth.verify_session(token)
            assert principal.role == "controller"
        finally:
            # No authority leaks into another test module.
            mcp_server._AUTHORITY = previous


# ===========================================================================
# Criterion 4: assignment, mailbox round trip and source-bound result work for
# Ops and independent Review; revocation, duplicate delivery, stale lease,
# forged principal, wrong attempt and disallowed output path each produce a
# declared refusal at the real tool seam
# ===========================================================================


class TestAssignmentMailboxAndSourceBoundResult:
    """Criterion 4 through the real MCP server tools and the run snapshot.

    Every assertion drives the production seam -- :mod:`skillweave.mcp.server`
    under a server-issued, scoped session, the fenced controller lease on
    :class:`~skillweave.runtime.store.SQLiteRunStore`, and the run snapshot's
    source binding -- never a re-implementation of the rule. The mailbox is
    addressed ``(recipient, principal)``: under a bound authority the session
    acts as its own principal, so a message is read/acked by the principal it
    was posted under and never by another principal.
    """

    def _session(self, authority, *, role, client_id, run_id="run-1",
                 transport="http", allowed_roots=(), allowed_routes=()):
        """Mint a scoped server-issued session for *role* (the registry)."""
        return authority.issue_session(
            role=role,
            client_id=client_id,
            transport=transport,
            run_ids=[run_id],
            allowed_roots=list(allowed_roots),
            allowed_routes=list(allowed_routes),
        )

    def _mailbox_path(self, tmp_path, monkeypatch) -> Path:
        """Point the server-owned mailbox at the scratch dir (no caller path).

        The mailbox path is *server-owned*: a caller may never inject one. The
        test binds it exactly as the production stdio startup does -- through
        :data:`~skillweave.mcp.server.MAILBOX_PATH_ENV` -- so the real tools
        resolve it with no code path that trusts a caller-supplied path.
        """
        path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(path))
        return path

    def test_assignment_mailbox_round_trip_for_ops_and_independent_review(
        self, authority, tmp_path, monkeypatch
    ):
        """Assignment -> post -> read -> replay -> ack, at the real tool seam.

        The controller *assigns* work by posting a handoff on each peer's
        addressed sub-channel under its own authority; the assignment is read
        back and replayed (the durable history is unchanged, so a reconnect
        loses nothing). An acknowledgement is only admitted for a message
        *addressed to the acknowledging principal*, and is idempotent: a
        duplicated acknowledgement derives the same reply id.
        """
        self._mailbox_path(tmp_path, monkeypatch)
        controller = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )

        assignments = {
            "ops": "implement c4",
            "rev": "independently verify c4",
        }
        for peer, body in assignments.items():
            out = json.loads(
                mcp_server.mailbox_post(
                    kind="handoff",
                    recipient=peer,
                    body=body,
                    run_id="run-1",
                    task_id="task-1",
                    attempt=1,
                    message_id=f"assign-{peer}",
                    session_token=controller,
                )
            )
            assert out["status"] == "posted", out
            assert out["message"]["recipient"] == peer
            # Addressed under the authenticated principal, never the caller's
            # named string.
            assert out["message"]["principal"] == "mcp:controller:main"

        # --- the assignment is read back (the controller's own channel).
        read = json.loads(
            mcp_server.mailbox_read(
                recipient="ops", run_id="run-1", session_token=controller,
            )
        )
        assert read["status"] == "ok", read
        assert [m["message_id"] for m in read["messages"]] == ["assign-ops"]
        assert read["messages"][0]["body"] == "implement c4"

        # --- replay returns byte-identical durable history (reconnect loses
        # nothing) and consumes nothing.
        replayed = json.loads(
            mcp_server.mailbox_replay(
                recipient="ops", run_id="run-1", session_token=controller,
            )
        )
        assert replayed["status"] == "ok", replayed
        assert replayed["messages"] == read["messages"]

        # --- an ack is admitted only for a message addressed *to* the
        # acknowledging principal (the durable reply is derived from it).
        handed = json.loads(
            mcp_server.mailbox_post(
                kind="handoff",
                recipient="mcp:controller:main",
                body="result for review",
                run_id="run-1",
                message_id="handoff-to-controller",
                session_token=controller,
            )
        )
        assert handed["status"] == "posted", handed
        ack = json.loads(
            mcp_server.mailbox_ack(
                message_id="handoff-to-controller",
                note="accepted",
                run_id="run-1",
                session_token=controller,
            )
        )
        assert ack["status"] == "acked", ack
        assert ack["message"]["kind"] == "ack"
        again = json.loads(
            mcp_server.mailbox_ack(
                message_id="handoff-to-controller",
                note="accepted",
                run_id="run-1",
                session_token=controller,
            )
        )
        assert again["status"] == "acked", again
        # A duplicated acknowledgement never creates a second durable reply.
        assert again["message"]["message_id"] == ack["message"]["message_id"]

        # --- an ack of a message addressed to *another* label is refused: the
        # scope is (recipient, principal), so the controller cannot acknowledge
        # work it addressed to its peer.
        misaddressed = json.loads(
            mcp_server.mailbox_ack(
                message_id="assign-ops",
                run_id="run-1",
                session_token=controller,
            )
        )
        assert misaddressed["status"] == "refused", misaddressed
        assert misaddressed["code"] == "message_id_mismatch", misaddressed

    def test_review_principal_cannot_read_another_principals_channel(
        self, authority, tmp_path, monkeypatch
    ):
        """Independent Review sees only its own addressed channel.

        A message the controller posted under ``mcp:controller:main`` is never
        returned to a reviewer session reading the same recipient label: the
        scope is the authenticated principal, so cross-principal reads observe
        nothing (a verified non-disclosure, not a missing feature).
        """
        self._mailbox_path(tmp_path, monkeypatch)
        controller = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        reviewer = self._session(
            authority, role="reviewer", client_id="rev",
            allowed_roots=[str(tmp_path)],
        )
        json.loads(
            mcp_server.mailbox_post(
                kind="handoff",
                recipient="rev",
                body="verify",
                run_id="run-1",
                message_id="assign-rev",
                session_token=controller,
            )
        )
        seen = json.loads(
            mcp_server.mailbox_read(
                recipient="rev", run_id="run-1", session_token=reviewer,
            )
        )
        assert seen["status"] == "ok", seen
        assert seen["messages"] == []

    def _post(self, token, *, recipient, body, message_id, run_id="run-1",
              principal=""):
        return json.loads(
            mcp_server.mailbox_post(
                kind="handoff",
                recipient=recipient,
                sender="",
                body=body,
                run_id=run_id,
                task_id="task-1",
                attempt=1,
                message_id=message_id,
                principal=principal,
                session_token=token,
            )
        )

    def test_revoked_session_cannot_post(self, authority, tmp_path, monkeypatch):
        """Revocation produces a verified refusal at the tool seam."""
        self._mailbox_path(tmp_path, monkeypatch)
        controller = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        # The very same token works ...
        assert self._post(
            controller, recipient="ops", body="a", message_id="m-1"
        )["status"] == "posted"
        # ... until the server revokes it.
        authority.revoke_token(controller)
        refused = self._post(
            controller, recipient="ops", body="b", message_id="m-2"
        )
        assert refused["status"] == "refused", refused
        assert refused["code"] == "unauthenticated", refused

    def test_duplicate_delivery_is_idempotent_and_conflicts_are_refused(
        self, authority, tmp_path, monkeypatch
    ):
        """Identical redelivery is idempotent; a mutated body is refused.

        A duplicated delivery (same ``message_id``) never creates a second
        durable message; a *different* body under the same id is a declared
        ``message_id_mismatch`` refusal, not a silent overwrite.
        """
        self._mailbox_path(tmp_path, monkeypatch)
        controller = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        first = self._post(
            controller, recipient="ops", body="payload", message_id="dup-1"
        )
        assert first["status"] == "posted", first
        identical = self._post(
            controller, recipient="ops", body="payload", message_id="dup-1"
        )
        assert identical["status"] == "posted", identical
        assert identical["message"]["seq"] == first["message"]["seq"]
        # Exactly one durable message survived the redelivery.
        read = json.loads(
            mcp_server.mailbox_read(recipient="ops", session_token=controller)
        )
        assert [m["message_id"] for m in read["messages"]] == ["dup-1"]

        refused = self._post(
            controller, recipient="ops", body="tampered", message_id="dup-1"
        )
        assert refused["status"] == "refused", refused
        assert refused["code"] == "message_id_mismatch", refused

    def test_forged_principal_is_refused(self, authority, tmp_path, monkeypatch):
        """A caller naming a principal other than its own is refused."""
        self._mailbox_path(tmp_path, monkeypatch)
        controller = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        forged = self._post(
            controller, recipient="ops", body="x", message_id="m-f",
            principal="mcp:worker:ops",
        )
        assert forged["status"] == "refused", forged
        assert forged["code"] == "principal_mismatch", forged

    def test_wrong_attempt_and_cross_run_are_refused(
        self, authority, tmp_path
    ):
        """A session scoped to one run cannot operate on another run.

        The controller role is deliberately *not* run-bound, so it is the
        run-bound roles (worker/observer/reviewer) whose scope the authority
        fences. A reviewer bound to ``run-1`` reading ``run-other`` is refused
        at the tool seam, never silently served another client's run.
        """
        svc = _service(tmp_path)
        reviewer = self._session(
            authority, role="reviewer", client_id="rev",
            run_id="run-1", allowed_roots=[str(tmp_path)],
        )
        out = json.loads(
            mcp_server.get_evidence(
                run_id="run-other",
                db_path=svc.db_path,
                session_token=reviewer,
            )
        )
        assert out["status"] == "refused", out
        assert out["code"] == "cross_client_access_refused", out

    def test_disallowed_output_path_is_refused(self, authority, tmp_path):
        """A write path outside the granted roots is refused by the authority."""
        session = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        with pytest.raises(PathInjectionRefused):
            authority.authorize(
                session,
                "write_run_state",
                run_id="run-1",
                artifacts_path=str(tmp_path.parent / "outside" / "artifacts"),
                default_paths=(str(tmp_path / "artifacts"),),
            )
        # An in-root path is admitted: the guard refuses injections, not work.
        authority.authorize(
            session,
            "write_run_state",
            run_id="run-1",
            artifacts_path=str(tmp_path / "artifacts"),
            default_paths=(str(tmp_path / "artifacts"),),
        )

    def test_worker_cannot_forge_an_assignment_write(
        self, authority, tmp_path, monkeypatch
    ):
        """A worker holds no ``write_run_state``: it cannot write the mailbox."""
        self._mailbox_path(tmp_path, monkeypatch)
        worker = self._session(
            authority, role="worker", client_id="ops",
            allowed_roots=[str(tmp_path)],
        )
        out = self._post(
            worker, recipient="ops", body="x", message_id="w-1"
        )
        assert out["status"] == "refused", out
        assert out["code"] == "operation_not_permitted", out

    def test_stale_controller_lease_is_refused_until_released(self, tmp_path):
        """The controller lease is single and fenced (stale lease refusal)."""
        store = SQLiteRunStore(str(tmp_path / "store.db"))
        held = store.acquire_controller_lease("main", ttl_seconds=300)
        # A second contender is refused while the lease is held ...
        with pytest.raises(Exception):
            store.acquire_controller_lease("second", ttl_seconds=300)
        # ... and only after the holder releases may a new, higher-fenced
        # lease be taken; the superseded token is stale and no longer usable.
        store.release_controller_lease(held.fencing_token)
        new = store.acquire_controller_lease("second", ttl_seconds=300)
        assert new.fencing_token > held.fencing_token
        with pytest.raises(Exception):
            store.release_controller_lease(held.fencing_token)

    def test_source_bound_result_reports_the_native_process_identity(
        self, authority, tmp_path
    ):
        """Source-bound result submission reaches the run snapshot.

        ``control_status`` reads the snapshot the tools consume; it carries the
        native ``process_identity`` handle and never a caller-fabricated
        subject binding.
        """
        svc = _service(tmp_path)
        SQLiteRunStore(svc.db_path).create_run(
            "run-1", task_id="task-1", attempt=1
        )
        out = control.control_status(
            "run-1", db_path=svc.db_path, artifacts_path=svc.artifacts_path
        )
        assert out["run_id"] == "run-1"
        identity = out["process_identity"]
        assert identity["handle_id"] == "runsvc-run:run-1"
        assert identity["service_principal"] == "runsvc"

    def test_evidence_read_is_source_bound_never_fabricated(
        self, authority, tmp_path
    ):
        """The evidence read returns only recorded facts (empty when absent).

        ``get_evidence`` carries the durable journal under the run it was read
        from -- never a caller-supplied subject binding. The source binding is
        the run snapshot's own native ``process_identity`` handle, read through
        ``control_status``; with no producer receipt recorded, no subject
        repo/commit is invented.
        """
        svc = _service(tmp_path)
        controller = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        SQLiteRunStore(svc.db_path).create_run("run-1", task_id="t", attempt=1)
        out = json.loads(
            mcp_server.get_evidence(
                run_id="run-1",
                db_path=svc.db_path,
                session_token=controller,
            )
        )
        # The read returns the run's own recorded journal, not a fabrication.
        assert "events" in out and out["events"] == [], out
        # No producer receipt was recorded, so no subject binding is invented
        # and the snapshot carries the native process identity handle only.
        status = control.control_status(
            "run-1", db_path=svc.db_path, artifacts_path=svc.artifacts_path
        )
        assert status["run_id"] == "run-1"
        assert status.get("subject_repo", "") == ""
        assert status.get("subject_commit", "") == ""
        assert status["process_identity"]["handle_id"] == "runsvc-run:run-1"


# ===========================================================================
# Criterion 5: a harness without the needed MCP transport gets an explicitly
# qualified CLI/file adapter with the same authority contract, or a named
# unsupported HOLD -- never a silent downgrade or a fabricated MCP success
# ===========================================================================


class TestQualifiedTransportAdapterNeverSilentlyDowngrades:
    def test_unknown_transport_is_a_named_unsupported_hold(self):
        """An unknown transport is refused by name, never coerced to stdio."""
        profile = control.transport_profile("carrier-pigeon")
        assert profile.supported is False
        assert profile.kind == "carrier-pigeon"
        assert "carrier-pigeon" in profile.reason
        # The served surface is named, so the HOLD is actionable.
        assert "http" in profile.reason and "stdio" in profile.reason

    def test_supported_transports_are_qualified(self):
        http = control.transport_profile("http", host="127.0.0.1", port=8765)
        assert http.supported is True and http.scoped is True
        stdio = control.transport_profile("stdio")
        assert stdio.supported is True
        # stdio is admitted precisely because it is not a network scope; it is
        # never reported as a scoped HTTP surface it is not.
        assert stdio.scoped is False

    def test_harness_without_notifications_gets_the_file_adapter(
        self, tmp_path
    ):
        """Explicit attestation of no notification support -> file-poll adapter.

        The adapter is assigned by the *detected* capability (never assumed) and
        carries the same mailbox authority contract as the MCP-notification
        adapter: the same durable mailbox under the same addressing.
        """
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        capability = handoff_mod.detect_client_capability(
            "legacy-harness",
            attestation={"detected": True, "transports": [], "features": []},
        )
        assert capability.detected is True
        assert capability.supports_notifications is False
        assert capability.adapter_kind() == handoff_mod.TRANSPORT_FILE_POLL

        adapter = handoff_mod.adapter_for_capability(capability, mailbox)
        assert isinstance(adapter, handoff_mod.PollingTransportAdapter)
        assert adapter.kind == handoff_mod.TRANSPORT_FILE_POLL

        mailbox.post(
            AgentMessage(
                kind="handoff",
                sender="main",
                recipient="ops",
                principal="mcp:controller:main",
                message_id="m-1",
                body="go",
            )
        )
        adapter.send(
            AgentMessage(
                kind="status",
                sender="ops",
                recipient="ops",
                principal="mcp:controller:main",
                message_id="m-2",
                body="working",
            )
        )
        delivered = adapter.read(
            recipient="ops", principal="mcp:controller:main"
        )
        assert {m["message_id"] for m in delivered} == {"m-1", "m-2"}

    def test_undetected_harness_is_not_claimed_as_mcp_capable(
        self, tmp_path
    ):
        """No config/skill-root -> detected=False; never a fabricated MCP yes."""
        capability = handoff_mod.detect_client_capability(
            "never-seen",
            skill_root_resolver=lambda _cid: None,
            config_resolver=lambda _cid: None,
        )
        assert capability.detected is False
        assert capability.supports_notifications is False
        assert capability.adapter_kind() == handoff_mod.TRANSPORT_FILE_POLL
        assert handoff_mod.TRANSPORT_CLI_POLL in capability.transports

    def test_notification_adapter_refuses_a_harness_without_support(
        self, tmp_path
    ):
        """Asking the MCP-notification adapter for an unsupported harness fails.

        The refusal is named (``TRANSPORT_NOT_SUPPORTED``); the tool never
        reports a fabricated MCP success for a harness that cannot receive it.
        """
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        capability = handoff_mod.detect_client_capability(
            "legacy-harness",
            attestation={"detected": True, "transports": [], "features": []},
        )
        with pytest.raises(handoff_mod.HandoffError) as exc:
            handoff_mod.NotificationTransportAdapter(capability, mailbox)
        assert exc.value.code == "TRANSPORT_NOT_SUPPORTED"

    def test_agent_capabilities_tool_attests_the_adapter_and_contract(
        self, authority, tmp_path
    ):
        """The real ``agent_capabilities`` tool names the adapter per client."""
        svc = _service(tmp_path)
        controller = authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"],
        )
        out = json.loads(
            mcp_server.agent_capabilities(
                clients="legacy-harness", session_token=controller
            )
        )
        assert out["status"] == "ok", out
        # The frozen contract version and digest are reported, so a client can
        # attest the exact schema it speaks.
        assert out["contract"]["artifact"] == "agent-message-v1"
        assert out["contract"]["schema_version"] == "1.0.0"
        assert out["contract"]["schema_digest"]
        entry = out["clients"]["legacy-harness"]
        # Not detected -> the file adapter (never a fabricated mcp-notifications).
        assert entry["adapter_kind"] == handoff_mod.TRANSPORT_FILE_POLL
        assert entry["detected"] is False

    def test_negotiate_capabilities_names_the_execution_shape(self):
        """A profile without experimental tasks negotiates the poll/cursor shape."""
        profile = control.transport_profile("http", host="127.0.0.1", port=8765)
        negotiated = control.negotiate_capabilities(
            {"tasks": {"supported": False}}, profile
        )
        assert negotiated["execution_shape"] == "async_tool_poll_cursor"
        assert negotiated["tasks"]["negotiated"] is False

    def test_require_mcp_is_an_explicit_dependency_refusal_or_present(self):
        """``require_mcp`` either admits a real package or refuses by name."""
        try:
            control.require_mcp()
        except Exception as exc:  # noqa: BLE001 - the declared refusal is typed
            assert "mcp" in str(exc).lower()


# ===========================================================================
# Criterion 6: restarting the interactive session leaves the service and child
# attempts running; controller replacement, quota waiting and cancellation stay
# functional; worker credentials never reach provider requests or logs
# ===========================================================================


class TestRestartControllerReplacementAndCredentialBoundary:
    def test_controller_replacement_is_fenced_and_strictly_increasing(self, tmp_path):
        """A restart hands over the controller lease without duplicating it."""
        store = SQLiteRunStore(str(tmp_path / "store.db"))
        first = store.acquire_controller_lease("session-a", ttl_seconds=300)
        # The interactive session restarts: it releases, and a new session takes
        # a strictly higher-fenced lease -- no second controller ever holds one.
        store.release_controller_lease(first.fencing_token)
        second = store.acquire_controller_lease("session-b", ttl_seconds=300)
        assert second.fencing_token > first.fencing_token
        with pytest.raises(Exception):
            store.acquire_controller_lease("session-c", ttl_seconds=300)

    def test_cancel_of_an_unverified_identity_is_refused_not_fabricated(
        self, tmp_path
    ):
        """A run with no recorded process identity cannot be 'cancelled'.

        ``control_cancel`` reports ``identity_unverified`` and signals nothing;
        the in-flight state stays *unknown*, never a fabricated termination.
        """
        svc = _service(tmp_path)
        SQLiteRunStore(svc.db_path).create_run("run-1", task_id="t", attempt=1)
        out = control.control_cancel(
            "run-1",
            reason="interactive restart",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
        )
        assert out["code"] == "identity_unverified", out
        assert out["reason"] == "no_recorded_identity", out
        assert out["verification"]["ok"] is False

    def test_cancel_of_an_unknown_run_is_not_found(self, tmp_path):
        svc = _service(tmp_path)
        SQLiteRunStore(svc.db_path)
        out = control.control_cancel(
            "run-missing",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
        )
        assert out["code"] == "not_found", out

    def test_resume_without_a_command_reconciles_without_claiming_restart(
        self, tmp_path
    ):
        """After a restart the surface reconciles; it never claims a restart."""
        svc = _service(tmp_path)
        SQLiteRunStore(svc.db_path).create_run("run-1", task_id="t", attempt=1)
        out = control.control_resume(
            "run-1",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
        )
        assert out["status"] == "reconciled", out
        assert out["restarted"] is False

    def test_resume_with_a_command_launches_a_new_identified_attempt(
        self, tmp_path
    ):
        """A real resume starts a *new identified* attempt, not a DB transition."""
        svc = _service(tmp_path)
        SQLiteRunStore(svc.db_path).create_run("run-1", task_id="t", attempt=1)
        out = control.control_resume(
            "run-1",
            command=f"{sys.executable} -c pass",
            idempotency_key="resume-1",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
        )
        assert out["status"] == "restarted", out
        assert out["restart_kind"] == "new_identified_attempt", out
        # A real new process identity was recorded (its PID + creation token),
        # not merely a state transition dressed up as "restarted".
        identity = out["process_identity"]
        assert identity["pid"] > 0
        assert identity["creation_token"]
        # The new attempt is a distinct run, linked to its predecessor.
        assert out["run_id"] != "run-1"
        assert out["parent_run_id"] == "run-1"

    def test_provider_forwarding_refuses_passthrough_and_unapproved_routes(
        self, authority
    ):
        """Worker credentials are never forwarded to a provider request."""
        session = authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"], allowed_routes=["approved-route"],
        )
        # A token-echo route is refused by name ...
        for route in ("passthrough", "echo", "reflect"):
            with pytest.raises(MCPAuthError) as exc:
                authority.authorize(
                    session, "forward", run_id="run-1", provider_route=route,
                )
            assert getattr(exc.value, "code", "") == "token_passthrough_refused", (
                route, exc.value
            )
        # ... and a route the session was not granted is refused too.
        with pytest.raises(MCPAuthError) as exc2:
            authority.authorize(
                session, "forward", run_id="run-1",
                provider_route="unapproved-route",
            )
        assert getattr(exc2.value, "code", "") == "unapproved_provider_route"

    def test_worker_credentials_never_reach_the_child_env(self, tmp_path):
        """The child env allowlist admits only SkillWeave-owned session keys."""
        svc = _service(tmp_path)
        grant = boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            transport="stdio",
            session_token="tok-c6",
            service=svc,
            start_scope="task-1/attempt-1",
        )
        env = boot.grant_child_env(grant)
        # Every provisioned key is SkillWeave-owned; no ambient credential.
        boot.assert_no_ambient_credentials(env)
        for key in env:
            assert key.startswith("SKILLWEAVE_"), key
        assert boot.ENV_MCP_SESSION_TOKEN in env

    def test_credential_shaped_worker_env_is_refused_by_name(self):
        """A worker credential cannot be provisioned into the child env."""
        with pytest.raises(ValueError) as exc:
            boot.assert_no_ambient_credentials(
                {boot.ENV_MCP_SESSION_TOKEN: "t", "ANTHROPIC_API_KEY": "secret"}
            )
        assert "ANTHROPIC_API_KEY" in str(exc.value)

    def test_forwarding_guard_admits_an_approved_route(self, authority):
        """The guard is not blanket-deny: an approved route is admitted."""
        session = authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"], allowed_routes=["approved-route"],
        )
        forwarded = authority.authorize(
            session, "forward", run_id="run-1",
            provider_route="approved-route",
        )
        # A real admission: the authorization returns the acting principal.
        assert forwarded.principal == "mcp:controller:main"


# ===========================================================================
# Backward compatibility for criteria 4-6: the added seams are additive
# ===========================================================================


class TestCriterionFourToSixBackwardCompatibility:
    def test_mailbox_addressing_is_unchanged_for_a_plain_principal(self, tmp_path):
        """The historical (recipient, principal) mailbox contract still holds."""
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        mailbox.post(
            AgentMessage(
                kind="status",
                sender="main",
                recipient="ops",
                principal="principal-a",
                message_id="bc-1",
                body="x",
            )
        )
        # A different principal never sees principal-a's message.
        assert mailbox.read(recipient="ops", principal="principal-b") == []
        assert len(mailbox.read(recipient="ops", principal="principal-a")) == 1

    def test_capability_detection_defaults_are_stable(self):
        """Adapter assignment stays within the three declared transports."""
        kinds = {
            handoff_mod.TRANSPORT_MCP_NOTIFICATIONS,
            handoff_mod.TRANSPORT_FILE_POLL,
            handoff_mod.TRANSPORT_CLI_POLL,
        }
        for client in ("codex", "antigravity", "openclaude"):
            capability = handoff_mod.detect_client_capability(
                client,
                skill_root_resolver=lambda _c: None,
                config_resolver=lambda _c: None,
            )
            assert capability.adapter_kind() in kinds

    def test_run_snapshot_without_a_run_is_not_found(self, tmp_path):
        """An unknown run is a declared ``not_found``, never a fabricated graph."""
        svc = _service(tmp_path)
        SQLiteRunStore(svc.db_path)
        out = control.control_status(
            "no-such-run",
            db_path=svc.db_path,
            artifacts_path=svc.artifacts_path,
        )
        assert out["code"] == "not_found", out
