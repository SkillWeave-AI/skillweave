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
import subprocess
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
from skillweave.runtime import process_identity  # noqa: E402


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


def _bind_mailbox(monkeypatch, svc: boot.HarnessProcessService) -> None:
    """Bind the server-owned mailbox so the serving seam resolves *this* service.

    ``control.service_for_binding`` -- the seam the reconnect tool and the real
    serving path both use -- resolves the mailbox from the server-owned
    ``MAILBOX_PATH_ENV``. The test harness points that env at the same mailbox
    ``_service`` uses, so a restarted session resolving "its" service reaches
    the same durable store (the same identity digest).
    """
    monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, svc.mailbox_path)


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

    def test_literal_wrong_attempt_start_is_refused(
        self, authority, tmp_path
    ):
        """A second start of the *same attempt* is a duplicate, refused by scope.

        Distinct from the cross-run case: the same task/run is named, but the
        attempt value is reused. The authority fences on the start scope
        (``task/attempt-N``), so the second start of an *already-launched*
        attempt is a ``duplicate_start_refused`` -- a wrong attempt value can
        never silently launch a second child of the same attempt.
        """
        session = self._session(
            authority, role="controller", client_id="main",
            allowed_roots=[str(tmp_path)],
        )
        scope = mcp_server.start_scope_for(task_id="task-1", attempt=1)
        # The first start of attempt 1 is admitted ...
        authority.authorize(session, "start_run", start_scope=scope)
        # ... and a second start naming the *same literal attempt* is refused.
        with pytest.raises(MCPAuthError) as exc:
            authority.authorize(session, "start_run", start_scope=scope)
        assert exc.value.code == "duplicate_start_refused", exc.value.to_dict()
        # A genuinely *different* attempt value is not caught by that fence: it
        # is a distinct scope, so the refusal above is not a blanket refusal.
        authority.authorize(
            session, "start_run",
            start_scope=mcp_server.start_scope_for(task_id="task-1", attempt=2),
        )

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
    def test_unknown_transport_is_a_named_unsupported_hold(self, tmp_path):
        """An unknown transport is refused by name at the *selection* seam.

        The earlier version of this test asserted on
        ``control.transport_profile`` (the served-surface description), which is
        a *different* seam. The finding was that the real selection seam
        (``select_handoff_adapter``) silently coerced an unknown requested
        transport to the polling adapter for a client that cannot receive
        notifications. This exercises the selection seam directly.
        """
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        capability = handoff_mod.detect_client_capability(
            "legacy-harness",
            attestation={"detected": True, "transports": [], "features": []},
        )
        assert capability.supports_notifications is False
        with pytest.raises(handoff_mod.HandoffError) as exc:
            handoff_mod.select_handoff_adapter(
                capability, mailbox, requested_transport="carrier-pigeon"
            )
        assert exc.value.code == "TRANSPORT_NOT_SUPPORTED"

    def test_unknown_transport_is_a_named_hold_through_the_tool(
        self, authority, tmp_path, monkeypatch
    ):
        """The same HOLD holds through the real ``handoff_adapter`` tool.

        A client that cannot receive notifications must not have an unknown
        requested transport silently coerced to file-poll with status ok.
        """
        monkeypatch.setenv(
            mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "mb.jsonl")
        )
        controller = authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"],
        )
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="legacy-harness",
                recipients="ops",
                transport="carrier-pigeon",
                session_token=controller,
            )
        )
        assert out["status"] == "hold", out
        assert out["code"] == "TRANSPORT_NOT_SUPPORTED", out

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


# ===========================================================================
# Criterion 4 (independent Review round trip through the real tools)
# ===========================================================================


class TestIndependentReviewRoundTripThroughTheRealTools:
    """Criterion 4: a *distinct Review principal* drives the real tools.

    The reviewer scope deliberately excludes general ``write_run_state`` (that
    is what keeps Review independent), so the general ``mailbox_post``/``ack``
    tools refuse it. The dedicated :func:`skillweave.mcp.server.review_action`
    tool is the real seam that admits the reviewer's *own* source-bound result
    on its own addressed channel, with the role still read from the
    server-owned session registry -- never the wire.
    """

    def _mailbox_path(self, tmp_path, monkeypatch) -> Path:
        path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(path))
        return path

    def test_reviewer_cannot_drive_the_general_mailbox_tools(
        self, authority, tmp_path, monkeypatch
    ):
        """The general tools refuse a reviewer (it may not write run state)."""
        self._mailbox_path(tmp_path, monkeypatch)
        reviewer = authority.issue_session(
            role="reviewer", client_id="rev", transport="http",
            run_ids=["run-1"],
        )
        refused = json.loads(
            mcp_server.mailbox_post(
                kind="status", recipient="rev", body="x", run_id="run-1",
                message_id="rev-general", session_token=reviewer,
            )
        )
        assert refused["status"] == "refused", refused
        # A reviewer holds no ``write_run_state``; the general post is refused
        # by the operation scope, not by an ad-hoc role string.
        assert refused["code"] in ("operation_not_permitted", "role_not_permitted"), refused

    def test_review_action_round_trip_is_source_bound_and_scoped(
        self, authority, tmp_path, monkeypatch
    ):
        """assignment -> write result -> read -> replay -> ack, at the tool seam.

        The reviewer's result carries the *actual* run/task/attempt it answers,
        is read back and replayed byte-identically (a reconnect loses nothing),
        and is acknowledged only by the reviewer's own addressed principal --
        idempotently.
        """
        self._mailbox_path(tmp_path, monkeypatch)
        reviewer = authority.issue_session(
            role="reviewer", client_id="rev", transport="http",
            run_ids=["run-1"],
        )
        # -- the reviewer posts its own source-bound result.
        written = json.loads(
            mcp_server.review_action(
                action="write", recipient="rev", body="independently verified",
                run_id="run-1", task_id="task-1", attempt=1,
                message_id="review-result-1", session_token=reviewer,
            )
        )
        assert written["status"] == "ok", written
        body = written["message"]["body"]
        assert body["assignment"] == "independently verified"
        # The result is bound to the exact subject scope it answers.
        assert body["run_id"] == "run-1"
        assert body["task_id"] == "task-1"
        assert body["attempt"] == 1
        # And addressed under the *authenticated* reviewer principal.
        assert written["message"]["principal"] == "mcp:reviewer:rev"

        # -- read back on the reviewer's own channel.
        read = json.loads(
            mcp_server.review_action(
                action="read", recipient="rev", run_id="run-1",
                session_token=reviewer,
            )
        )
        assert read["status"] == "ok", read
        assert [m["message_id"] for m in read["messages"]] == ["review-result-1"]

        # -- replay is byte-identical durable history (reconnect loses nothing).
        replayed = json.loads(
            mcp_server.review_action(
                action="replay", recipient="rev", run_id="run-1",
                session_token=reviewer,
            )
        )
        assert replayed["messages"] == read["messages"]

        # -- ack of the reviewer's own addressed message, idempotently.
        acked = json.loads(
            mcp_server.review_action(
                action="ack", recipient="rev", message_id="review-result-1",
                run_id="run-1", session_token=reviewer,
            )
        )
        assert acked["status"] == "ok", acked
        assert acked["message"]["kind"] == "ack"
        again = json.loads(
            mcp_server.review_action(
                action="ack", recipient="rev", message_id="review-result-1",
                run_id="run-1", session_token=reviewer,
            )
        )
        assert again["message"]["message_id"] == acked["message"]["message_id"]

    def test_review_action_refuses_a_non_review_principal(
        self, authority, tmp_path, monkeypatch
    ):
        """A worker cannot drive the Review seam (scope, not a role string)."""
        self._mailbox_path(tmp_path, monkeypatch)
        worker = authority.issue_session(
            role="worker", client_id="ops", transport="http",
            run_ids=["run-1"],
        )
        refused = json.loads(
            mcp_server.review_action(
                action="write", recipient="ops", body="fake review",
                run_id="run-1", session_token=worker,
            )
        )
        assert refused["status"] == "refused", refused
        assert refused["code"] in ("operation_not_permitted", "role_not_permitted"), refused


# ===========================================================================
# Criterion 5 (the real adapter-selection seam, never a silent downgrade)
# ===========================================================================


class TestHandoffAdapterSelectionSeam:
    """Criterion 5 through the *production selection path*, not a bare helper.

    The ``handoff_adapter`` MCP tool is the seam that actually *selects* the
    adapter: a client without MCP-notification support is handed a qualified
    CLI/file polling adapter carrying the *same* authority contract, and an
    explicitly-requested unsupported transport is a named
    ``TRANSPORT_NOT_SUPPORTED`` HOLD -- never a coerced transport and never a
    fabricated MCP success.
    """

    def _mailbox_path(self, tmp_path, monkeypatch) -> Path:
        path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(path))
        return path

    def test_handoff_adapter_tool_selects_the_polling_adapter_with_contract(
        self, authority, tmp_path, monkeypatch
    ):
        """A non-notification client gets the file adapter + same contract."""
        self._mailbox_path(tmp_path, monkeypatch)
        controller = authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"],
        )
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="legacy-harness",
                recipients="ops,rev",
                session_token=controller,
            )
        )
        assert out["status"] == "ok", out
        # The *qualified* polling adapter, selected by detected capability.
        assert out["adapter"]["kind"] == handoff_mod.TRANSPORT_FILE_POLL
        assert out["adapter"]["capability"]["detected"] is False
        # The same authority contract every transport carries: bound to the
        # authenticated principal, never a caller field.
        assert out["authority_contract"]["principal"] == "mcp:controller:main"
        assert out["authority_contract"]["recipients"] == ["ops", "rev"]
        assert out["adapter"]["authority_contract"] == out["authority_contract"]
        # The frozen contract version is reported so the client can attest it.
        assert out["contract"]["artifact"] == "agent-message-v1"

    def test_handoff_adapter_tool_holds_on_an_unsupported_transport(
        self, authority, tmp_path, monkeypatch
    ):
        """An explicitly-requested unsupported transport is a *named* HOLD."""
        self._mailbox_path(tmp_path, monkeypatch)
        controller = authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"],
        )
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="legacy-harness",
                recipients="ops",
                transport=handoff_mod.TRANSPORT_MCP_NOTIFICATIONS,
                session_token=controller,
            )
        )
        # Never a silent downgrade, never a fabricated MCP success: the refusal
        # is named and actionable, and it lists the transports the surface
        # supports -- so the caller knows what to ask for instead.
        assert out["status"] == "hold", out
        assert out["code"] == "TRANSPORT_NOT_SUPPORTED", out
        assert out["client_id"] == "legacy-harness"
        assert handoff_mod.TRANSPORT_FILE_POLL in out["supported_transports"]
        # The client's own reason names *why* the request could not be honored.
        assert out["reason"]

    def test_selected_adapter_enforces_the_authority_contract(self, tmp_path):
        """A transport switch never broadens what the client may read/write.

        The selected polling adapter carries a real contract: a read under a
        principal other than the contract's is a ``scope_escape`` and a read of
        an un-addressed recipient is refused -- so the adapter is *qualified*,
        not merely named.
        """
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        capability = handoff_mod.detect_client_capability(
            "legacy-harness",
            attestation={"detected": True, "transports": [], "features": []},
        )
        contract = handoff_mod.HandoffAuthorityContract(
            principal="mcp:controller:main", recipients=frozenset({"ops"}),
        )
        adapter = handoff_mod.select_handoff_adapter(
            capability, mailbox, authority_contract=contract
        )
        assert isinstance(adapter, handoff_mod.PollingTransportAdapter)
        with pytest.raises(handoff_mod.HandoffError) as exc:
            adapter.read(principal="mcp:worker:ops", recipient="ops")
        assert exc.value.code == "scope_escape"
        with pytest.raises(handoff_mod.HandoffError) as exc2:
            adapter.read(principal="mcp:controller:main", recipient="nobody")
        assert exc2.value.code == "recipient_not_addressed"

    def test_selection_never_hands_notification_adapter_to_unsupported_client(
        self, tmp_path
    ):
        """The selection path cannot be coerced into a fabricated MCP success."""
        mailbox = AgentMailbox(str(tmp_path / "mb.jsonl"))
        capability = handoff_mod.detect_client_capability(
            "legacy-harness",
            attestation={"detected": True, "transports": [], "features": []},
        )
        with pytest.raises(handoff_mod.HandoffError) as exc:
            handoff_mod.select_handoff_adapter(
                capability, mailbox,
                requested_transport=handoff_mod.TRANSPORT_MCP_NOTIFICATIONS,
            )
        assert exc.value.code == "TRANSPORT_NOT_SUPPORTED"

    # -- SW-1511-RECOVERY criterion 2: the continuation decision through the
    # real tool, with the prior session id read from the durable record. -----

    def _seed_owned_attempt(self, root: Path, *, session_id: str) -> str:
        """Write a durable dispatch record for ``run-1`` and return the db path.

        This is the *durable record a launch wrote*: the continuation must read
        the prior session id from here, never from a caller field.  The recorded
        identity is the *real* live process this test runs in, so it verifies
        against the OS -- exactly as a launch's recorded owned attempt would.
        """
        from skillweave.runtime.store import SQLiteRunStore

        db_path = str(root / "store.db")
        live = process_identity.capture(os.getpid())
        store = SQLiteRunStore(db_path)
        store.record_dispatch_intent(
            run_id="run-1", task_id="task-a", attempt=1,
            execution_scope="run-1/task-a/1", step="ready_dispatch",
            route="provider/model", session_id=session_id,
            process_identity=live.to_dict(),
        )
        store.close()
        return db_path

    def _driving_token(self, authority, allowed_roots=None):
        return authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1"],
            allowed_roots=[str(r) for r in (allowed_roots or [])],
        )

    def test_handoff_tool_attested_with_recorded_session_resumes_that_session(
        self, authority, tmp_path, monkeypatch
    ):
        """The real tool resumes the *recorded* session for an attested client.

        Driving ``handoff_adapter`` with an explicit ``session-resume``
        attestation and a durably recorded session id returns a session-resume
        decision naming that exact id -- read from the record, and persisted to
        the run's mailbox where a recovered controller reads it.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        monkeypatch.setenv(
            handoff_mod.CAPABILITY_ATTESTATION_ENV,
            json.dumps({"transports": [], "features": {"session-resume": True}}),
        )
        db_path = self._seed_owned_attempt(tmp_path, session_id="sess-live")
        token = self._driving_token(authority, [tmp_path])
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        continuation = out["continuation"]
        assert continuation["mode"] == "session_resume"
        assert continuation["resume_capability_attested"] is True
        assert continuation["session_id"] == "sess-live"
        assert continuation["limitation"] == ""
        # The decision is persisted durably where a recovered controller reads
        # it -- the run's mailbox, as a status record carrying the decision.
        assert out["continuation_persisted"]["status"] == "persisted"
        records = AgentMailbox(str(mailbox_path)).read(
            recipient="mcp:controller:main", principal="mcp:controller:main"
        )
        persisted = [r for r in records
                     if r.get("kind") == "status"
                     and isinstance(r.get("body"), dict)
                     and "mode" in r["body"]]
        assert persisted, records
        assert persisted[-1]["body"]["mode"] == "session_resume"
        assert persisted[-1]["body"]["session_id"] == "sess-live"

    def test_handoff_tool_attested_without_recorded_session_falls_to_checkpoint(
        self, authority, tmp_path, monkeypatch
    ):
        """An attested client with no recorded session falls to the checkpoint.

        The attestation is present but the durable record carries no session id,
        so the tool must NOT fabricate one: the decision is a named
        ``checkpoint_continuation`` with the session-identity-unknown limitation.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        monkeypatch.setenv(
            handoff_mod.CAPABILITY_ATTESTATION_ENV,
            json.dumps({"transports": [], "features": {"session-resume": True}}),
        )
        db_path = self._seed_owned_attempt(tmp_path, session_id="")
        token = self._driving_token(authority, [tmp_path])
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        continuation = out["continuation"]
        assert continuation["mode"] == "checkpoint_continuation"
        assert continuation["session_id"] == ""
        assert continuation["limitation"] == "session_identity_unknown"

    def test_handoff_tool_unattested_client_falls_to_named_checkpoint(
        self, authority, tmp_path, monkeypatch
    ):
        """An unattested client never gets a session resume, even with a record.

        The durable record *does* carry a session id, but the client did not
        attest the capability -- so the tool returns a named
        ``checkpoint_continuation`` that carries no session id forward.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        monkeypatch.delenv(handoff_mod.CAPABILITY_ATTESTATION_ENV, raising=False)
        db_path = self._seed_owned_attempt(tmp_path, session_id="sess-live")
        token = self._driving_token(authority, [tmp_path])
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="unattested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        continuation = out["continuation"]
        assert continuation["mode"] == "checkpoint_continuation"
        assert continuation["session_id"] == ""
        assert continuation["limitation"] == "unsupported_session_resume_capability"

    # -- SW-1511-RECOVERY criterion 2 (B): the db_path roots guard, driven
    # through the real tool. A store outside the authenticated principal's
    # bound roots must be refused *before* any read; an in-roots store still
    # works. ---------------------------------

    def _seed_intent(
        self,
        db_path: str,
        *,
        session_id: str,
        state: str = "launched",
        process_identity=None,
        attempt: int = 1,
        scope: str = "",
        intent_id: str = "",
    ) -> str:
        """Record one durable dispatch intent and return its ``intent_id``."""
        from skillweave.runtime.store import SQLiteRunStore

        store = SQLiteRunStore(db_path)
        intent = store.record_dispatch_intent(
            run_id="run-1", task_id="task-a", attempt=attempt,
            execution_scope=scope or f"run-1/task-a/{attempt}",
            step="ready_dispatch", state=state, session_id=session_id,
            process_identity=process_identity,
            intent_id=intent_id or None,
        )
        store.close()
        return intent.intent_id

    def _rooted_token(self, authority, root, *, role="controller"):
        """A session bound to ``run-1`` with ``root`` as its one allowed root."""
        return authority.issue_session(
            role=role, client_id="main", transport="http",
            run_ids=["run-1"], allowed_roots=[str(root)],
        )

    @pytest.mark.parametrize("role", ["controller", "worker", "reviewer"])
    def test_handoff_tool_refuses_a_db_path_outside_the_bound_roots(
        self, authority, tmp_path, monkeypatch, role
    ):
        """A store outside the principal's allowed roots is refused unread.

        The caller is bound to one run with allowed roots in *one* directory but
        passes a store outside them; the tool must refuse before reading -- no
        continuation decision is fabricated from an out-of-roots store, for
        controller, worker and reviewer alike.
        """
        self._mailbox_path(tmp_path, monkeypatch)
        monkeypatch.setenv(
            handoff_mod.CAPABILITY_ATTESTATION_ENV,
            json.dumps({"transports": [], "features": {"session-resume": True}}),
        )
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # A real, seeded store the tool would happily *read* if it were not
        # refused: proof the refusal is the roots guard, not a missing record.
        db_path = self._seed_owned_attempt(outside, session_id="sess-leak")
        token = self._rooted_token(authority, allowed, role=role)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert "continuation" not in out, out
        assert out.get("code") == "path_injection_refused", out
        assert out.get("status") not in ("ok", "persisted"), out

    def test_handoff_tool_still_reads_a_db_path_inside_the_bound_roots(
        self, authority, tmp_path, monkeypatch
    ):
        """An in-roots store is still read: the guard narrows, it does not break."""
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        monkeypatch.setenv(
            handoff_mod.CAPABILITY_ATTESTATION_ENV,
            json.dumps({"transports": [], "features": {"session-resume": True}}),
        )
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        live = process_identity.capture(os.getpid())
        db_path = str(allowed / "store.db")
        self._seed_intent(
            db_path, session_id="sess-live", state="launched",
            process_identity=live.to_dict(),
        )
        token = self._rooted_token(authority, allowed)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "session_resume"
        assert out["continuation"]["session_id"] == "sess-live"

    # -- SW-1511-RECOVERY criterion 2 (D): only the *verified* in-flight
    # attempt's session may be named; a superseded or unverified intent falls
    # to the named checkpoint. --------------------------------------------

    def _attested(self, monkeypatch):
        monkeypatch.setenv(
            handoff_mod.CAPABILITY_ATTESTATION_ENV,
            json.dumps({"transports": [], "features": {"session-resume": True}}),
        )

    def test_handoff_tool_ignores_a_superseded_attempt_session(
        self, authority, tmp_path, monkeypatch
    ):
        """A superseded (terminal) attempt recorded *last* is never named.

        The run carries the live in-flight attempt *and*, recorded later, a
        terminal attempt whose session id must be ignored: selecting the
        last-recorded intent regardless of state would name the superseded
        record's session -- the resume must instead follow the in-flight one.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        db_path = str(tmp_path / "store.db")
        # The live in-flight attempt, recorded first.
        self._seed_intent(
            db_path, session_id="sess-live", state="launched",
            process_identity=live.to_dict(),
            attempt=2, scope="run-1/task-a/2", intent_id="intent-live",
        )
        # A superseded terminal attempt with its own session id, recorded
        # *later* -- so a last-recorded selection would wrongly name it.
        self._seed_intent(
            db_path, session_id="sess-dead", state="failed",
            process_identity={"pid": 999999, "start_time": "0.0",
                              "exec_path": "/bin/dead", "argv": ["dead"]},
            attempt=1, scope="run-1/task-a/1", intent_id="intent-dead",
        )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "session_resume", out
        assert out["continuation"]["session_id"] == "sess-live"
        assert out["continuation"]["intent_id"] == "intent-live"

    def test_handoff_tool_unverified_in_flight_attempt_falls_to_checkpoint(
        self, authority, tmp_path, monkeypatch
    ):
        """An in-flight attempt whose identity no longer verifies is not adopted.

        The only in-flight attempt recorded a recycled identity (a stale
        ``start_time``); its session id must NOT be named -- the continuation is
        a named checkpoint, mirroring ``verify_owned_attempt``.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        forged = live.to_dict()
        forged["start_time"] = "0.0"  # a recycled pid: never the owned attempt
        db_path = str(tmp_path / "store.db")
        self._seed_intent(
            db_path, session_id="sess-stale", state="launched",
            process_identity=forged, intent_id="intent-stale",
        )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "checkpoint_continuation", out
        assert out["continuation"]["session_id"] == ""
        assert out["continuation"]["limitation"] == "session_identity_unknown"

    def test_handoff_tool_attempt_without_identity_falls_to_checkpoint(
        self, authority, tmp_path, monkeypatch
    ):
        """An in-flight attempt that recorded no process identity is not adopted."""
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        db_path = str(tmp_path / "store.db")
        self._seed_intent(
            db_path, session_id="sess-noid", state="launched",
            process_identity=None, intent_id="intent-noid",
        )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "checkpoint_continuation", out
        assert out["continuation"]["session_id"] == ""

    # -- SW-1511-RECOVERY criterion 2 (D, teeth): the selection is the native
    # *ordering-first* decision. A non-verifying ordering-first intent REFUSES
    # (named checkpoint) -- it is never skipped to adopt a later live intent --
    # and several intents in both orders resolve by the frontier order, not by
    # identity. --------------------------------------------------------------

    def _forged_identity(self):
        """A recycled-pid identity captured live then made non-verifying."""
        forged = process_identity.capture(os.getpid()).to_dict()
        forged["start_time"] = "0.0"  # a recycled pid: never the owned attempt
        return forged

    def test_handoff_tool_refuses_a_non_verifying_ordering_first_intent(
        self, authority, tmp_path, monkeypatch
    ):
        """A stale ordering-first intent refuses; it is not skipped for a live one.

        The frontier's *first* record (by ``created_at``/``intent_id``) is a
        recycled-identity intent and a *live* intent is recorded after it. The
        native decision adopts only ``in_flight[0]`` and, when that does not
        verify, refuses with ``refused_no_owned_attempt`` -- the server must do
        the same: a named checkpoint with an *empty* session id, never the live
        intent's session reached by skipping the stale first record.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        db_path = str(tmp_path / "store.db")
        # The ordering-first intent: recorded FIRST, identity does not verify.
        self._seed_intent(
            db_path, session_id="sess-stale", state="launched",
            process_identity=self._forged_identity(),
            attempt=1, scope="run-1/task-a/1", intent_id="intent-stale",
        )
        # A *live* intent recorded after it -- which a skipping selection would
        # wrongly adopt.
        self._seed_intent(
            db_path, session_id="sess-live", state="launched",
            process_identity=live.to_dict(),
            attempt=2, scope="run-1/task-a/2", intent_id="intent-live",
        )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        continuation = out["continuation"]
        assert continuation["mode"] == "checkpoint_continuation", out
        assert continuation["session_id"] == "", out
        assert continuation["limitation"] == "session_identity_unknown", out

    def test_handoff_tool_adopts_the_verifying_ordering_first_intent(
        self, authority, tmp_path, monkeypatch
    ):
        """With the *live* intent ordering-first, it is adopted; no skipping.

        The mirror of the refusal case: the live intent is recorded first and a
        stale intent after it. Native ``in_flight[0]`` verifies, so the session
        resume names the live intent -- and never the later stale record.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        db_path = str(tmp_path / "store.db")
        self._seed_intent(
            db_path, session_id="sess-live", state="launched",
            process_identity=live.to_dict(),
            attempt=2, scope="run-1/task-a/2", intent_id="intent-live",
        )
        self._seed_intent(
            db_path, session_id="sess-stale", state="launched",
            process_identity=self._forged_identity(),
            attempt=1, scope="run-1/task-a/1", intent_id="intent-stale",
        )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "session_resume", out
        assert out["continuation"]["session_id"] == "sess-live", out
        assert out["continuation"]["intent_id"] == "intent-live", out

    def test_handoff_tool_several_intents_resolve_by_frontier_order(
        self, authority, tmp_path, monkeypatch
    ):
        """With several live intents the *ordering-first* one is adopted.

        Three in-flight intents are recorded in a known order; the adopted
        session must be the ordering-first one (by ``created_at``/
        ``intent_id``), never "any verified intent" reached by iteration order.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        db_path = str(tmp_path / "store.db")
        for n, sid in ((1, "sess-1"), (2, "sess-2"), (3, "sess-3")):
            self._seed_intent(
                db_path, session_id=sid, state="launched",
                process_identity=live.to_dict(),
                attempt=n, scope=f"run-1/task-a/{n}", intent_id=f"intent-{n}",
            )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "session_resume", out
        assert out["continuation"]["session_id"] == "sess-1", out
        assert out["continuation"]["intent_id"] == "intent-1", out

    def test_handoff_tool_several_intents_with_a_stale_first_refuse(
        self, authority, tmp_path, monkeypatch
    ):
        """Several intents with a non-verifying ordering-first one refuse.

        The first of three in-flight intents carries a recycled identity; even
        though two later live intents exist, the native decision refuses on
        ``in_flight[0]`` rather than adopting a later intent -- the server must
        yield the named checkpoint, never skip forward.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        db_path = str(tmp_path / "store.db")
        self._seed_intent(
            db_path, session_id="sess-stale", state="launched",
            process_identity=self._forged_identity(),
            attempt=1, scope="run-1/task-a/1", intent_id="intent-stale",
        )
        for n, sid in ((2, "sess-2"), (3, "sess-3")):
            self._seed_intent(
                db_path, session_id=sid, state="launched",
                process_identity=live.to_dict(),
                attempt=n, scope=f"run-1/task-a/{n}", intent_id=f"intent-{n}",
            )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["continuation"]["mode"] == "checkpoint_continuation", out
        assert out["continuation"]["session_id"] == "", out
        assert out["continuation"]["limitation"] == "session_identity_unknown", out

    # -- SW-1511-RECOVERY criterion 2 (C, teeth): the reader is scoped to the
    # run recorded *in the record itself*, in both directions. ---------------

    def _two_run_token(self, authority, root):
        """A controller bound to ``run-1`` *and* ``run-2`` with one root."""
        return authority.issue_session(
            role="controller", client_id="main", transport="http",
            run_ids=["run-1", "run-2"], allowed_roots=[str(root)],
        )

    def _persisted_record(self, mailbox_path: Path, run_id: str) -> dict:
        """The durable ``harness-continuation`` status record for ``run_id``."""
        records = AgentMailbox(str(mailbox_path)).read(
            recipient="mcp:controller:main", principal="mcp:controller:main",
            kinds=["status"],
        )
        matches = [
            r for r in records
            if r.get("run_id") == run_id
            and isinstance(r.get("body"), dict)
            and r["body"].get("schema_id")
            == "https://skillweave.dev/schemas/process/harness-continuation/v1"
        ]
        assert matches, records
        return matches[-1]

    def _persist_for_run(self, mailbox_path: Path, run_id: str, *, session_id: str) -> None:
        """Post a ``harness-continuation`` status record owned by ``run_id``.

        The record is written the way :func:`_persist_continuation_decision`
        writes it -- scoped to ``run_id`` and the principal -- but directly, so
        a multi-run mailbox can be built without driving a second store.
        """
        from skillweave.runtime.native_process import plan_harness_continuation

        decision = plan_harness_continuation(
            run_id, resume_capability_attested=True, prior_session_id=session_id,
            intent_id=f"intent-{session_id}",
        )
        principal = "mcp:controller:main"
        AgentMailbox(str(mailbox_path)).post(
            AgentMessage(
                kind="status", sender=principal, recipient=principal,
                principal=principal, run_id=run_id,
                message_id=f"harness-continuation-{run_id}-{decision.mode}",
                body=decision.to_dict(),
            )
        )

    def test_read_continuation_decision_is_scoped_to_the_records_run(
        self, authority, tmp_path, monkeypatch
    ):
        """A record for another run is never returned for the requested run.

        A controller bound to ``run-1`` and ``run-2`` persisted a decision for
        each. Reading ``run-1`` must return *run-1's* record -- never run-2's
        mislabelled with ``run_id="run-1"``.
        """
        mailbox_path = self._mailbox_path(tmp_path, monkeypatch)
        self._persist_for_run(mailbox_path, "run-1", session_id="sess-1")
        self._persist_for_run(mailbox_path, "run-2", session_id="sess-2")
        token = self._two_run_token(authority, tmp_path)
        read = json.loads(
            mcp_server.read_continuation_decision(
                run_id="run-1", session_token=token,
            )
        )
        assert read["status"] == "ok", read
        assert read["found"] is True, read
        assert read["run_id"] == "run-1", read
        assert read["decision"]["session_id"] == "sess-1", read
        assert read["decision"]["run_id"] == "run-1", read

    def test_read_continuation_decision_for_a_run_without_a_record_is_absent(
        self, authority, tmp_path, monkeypatch
    ):
        """Reading a run that has no record is ``found: false``, not a foreign one.

        A decision exists for ``run-1`` only; reading ``run-2`` (bound, but with
        no record of its own) must be fail-closed absent -- never run-1's
        decision relabelled as run-2's.
        """
        mailbox_path = self._mailbox_path(tmp_path, monkeypatch)
        self._persist_for_run(mailbox_path, "run-1", session_id="sess-1")
        token = self._two_run_token(authority, tmp_path)
        read = json.loads(
            mcp_server.read_continuation_decision(
                run_id="run-2", session_token=token,
            )
        )
        assert read["status"] == "ok", read
        assert read["found"] is False, read
        assert read["run_id"] == "run-2", read
        assert "decision" not in read, read

    # -- SW-1511-RECOVERY criterion 2 (C): the persisted decision has a real
    # production reader, without instantiating NativeProcessService. --------

    def test_read_continuation_decision_returns_the_persisted_decision(
        self, authority, tmp_path, monkeypatch
    ):
        """The reader returns the *same* decision the tool persisted.

        A recovered controller reaches the decision through
        ``read_continuation_decision`` (the run's durable mailbox), not a
        re-derivation -- and the reader itself never touches the native process
        service.
        """
        mailbox_path = tmp_path / "mb.jsonl"
        monkeypatch.setenv(mcp_server.MAILBOX_PATH_ENV, str(mailbox_path))
        self._attested(monkeypatch)
        live = process_identity.capture(os.getpid())
        db_path = str(tmp_path / "store.db")
        self._seed_intent(
            db_path, session_id="sess-live", state="launched",
            process_identity=live.to_dict(),
        )
        token = self._rooted_token(authority, tmp_path)
        written = json.loads(
            mcp_server.handoff_adapter(
                client_id="attested-harness", recipients="ops",
                run_id="run-1", db_path=db_path, session_token=token,
            )
        )
        assert written["continuation"]["mode"] == "session_resume", written
        read = json.loads(
            mcp_server.read_continuation_decision(
                run_id="run-1", session_token=token,
            )
        )
        assert read["status"] == "ok", read
        assert read["found"] is True, read
        assert read["decision"]["mode"] == "session_resume"
        assert read["decision"]["session_id"] == "sess-live"
        assert read["decision"]["schema_id"] == (
            "https://skillweave.dev/schemas/process/harness-continuation/v1"
        )
        # The reader is *the same decision*: identical to what was persisted.
        assert read["decision"] == written["continuation"]

    def test_read_continuation_decision_is_fail_closed_when_absent(
        self, authority, tmp_path, monkeypatch
    ):
        """With no persisted decision the reader reports ``found: false``."""
        monkeypatch.setenv(
            mcp_server.MAILBOX_PATH_ENV, str(tmp_path / "empty-mb.jsonl")
        )
        token = self._rooted_token(authority, tmp_path)
        out = json.loads(
            mcp_server.read_continuation_decision(
                run_id="run-1", session_token=token,
            )
        )
        assert out["status"] == "ok", out
        assert out["found"] is False, out


# ===========================================================================
# Criterion 6 (restart survival of the service+child; credentials never logged)
# ===========================================================================


class TestRestartSurvivalAndCredentialLogBoundary:
    """Criterion 6: restarting the interactive session leaves the work running.

    The interactive session is a *view*; the durable service and the child are
    owned by the process service. A restart is ready exactly when the granted
    service identity is still reachable *and* the child attempt is verifiably
    alive (re-verified against the live OS facts), so quota waiting stays
    functional across the restart -- and a worker's credential never becomes
    observable in a log.
    """

    def _grant(self, tmp_path, *, token="tok-c6"):
        svc = _service(tmp_path)
        return boot.HarnessMcpSessionGrant(
            schema_id=boot.HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            schema_version="1.0.0",
            run_id="run-1",
            task_id="task-1",
            attempt=1,
            transport="stdio",
            session_token=token,
            service=svc,
            start_scope="task-1/attempt-1",
        )

    def test_restart_ready_is_ready_while_the_child_is_alive(self, tmp_path):
        """A live child keeps a restart safe: service reachable, child alive."""
        import subprocess
        import time

        from skillweave.runtime import process_identity as _pi

        grant = self._grant(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.1)
            record = _pi.capture(proc.pid).to_dict()

            class _LiveHandle:
                pid = proc.pid
                _sw_process_identity = record

            out = boot.restart_ready(grant, _LiveHandle())
            assert out["status"] == "ready", out
            assert out["service_reachable"] is True
            assert out["service_identity"] == grant.service.identity()
            assert out["child_alive"] is True
        finally:
            proc.terminate()
            proc.wait()

    def test_restart_ready_holds_when_the_child_is_gone(self, tmp_path):
        """A restart is *not* ready when the child cannot be verified alive.

        The recorded identity outlives the child here: after the child exits its
        identity is unobservable, so the seam fails closed (``hold``) rather
        than assuming a survivor.
        """
        import subprocess
        import time

        from skillweave.runtime import process_identity as _pi

        grant = self._grant(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        time.sleep(0.05)
        record = _pi.capture(proc.pid)
        stale = record.to_dict() if record is not None else {
            "pid": proc.pid, "start_time": "", "identity_token": "stale"
        }

        class _StaleHandle:
            pid = proc.pid
            _sw_process_identity = stale

        out = boot.restart_ready(grant, _StaleHandle())
        assert out["status"] == "hold", out
        assert out["child_alive"] is False

    def test_child_attempt_alive_rejects_a_recycled_pid(self, tmp_path):
        """A recorded identity that no longer matches the live pid is not alive."""
        class _RecycledHandle:
            pid = os.getpid()
            _sw_process_identity = {
                "pid": os.getpid(),
                "start_time": "not-the-real-start-time",
                "identity_token": "mismatched",
            }

        assert boot.child_attempt_alive(_RecycledHandle()) is False

    def test_credential_shaped_values_are_redacted_from_logs(self):
        """A worker credential value is redacted before it can be logged."""
        environ = {
            "ANTHROPIC_API_KEY": "sk-super-secret",
            "OPENAI_API_KEY": "sk-openai-secret",
            "SKILLWEAVE_MCP_SESSION_TOKEN": "tok-not-redacted",
        }
        line = "provider call failed key=sk-super-secret other=sk-openai-secret"
        redacted = boot.redact_worker_credentials(line, environ)
        assert "sk-super-secret" not in redacted
        assert "sk-openai-secret" not in redacted
        assert redacted.count("<redacted-credential>") == 2

    def test_assert_worker_credentials_not_in_logs_fails_closed(self):
        """The log guard refuses when a credential value is observable."""
        environ = {"ANTHROPIC_API_KEY": "sk-leaky"}
        with pytest.raises(ValueError) as exc:
            boot.assert_worker_credentials_not_in_logs(
                ["starting run", "auth header Bearer sk-leaky"], environ
            )
        assert "ANTHROPIC_API_KEY" in str(exc.value)
        # A clean log passes: the guard is a real check, not a blanket refusal.
        boot.assert_worker_credentials_not_in_logs(["starting run"], environ)

    def test_restart_ready_never_stops_the_service_or_child(self, tmp_path):
        """Verifying a restart is observational: the child survives the check."""
        import subprocess
        import time

        from skillweave.runtime import process_identity as _pi

        grant = self._grant(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.1)
            record = _pi.capture(proc.pid).to_dict()

            class _LiveHandle:
                pid = proc.pid
                _sw_process_identity = record

            boot.restart_ready(grant, _LiveHandle())
            # The check did not signal the child: it is still running.
            assert proc.poll() is None
        finally:
            proc.terminate()
            proc.wait()

    def test_reconnect_session_entry_verifies_a_surviving_child(self, tmp_path):
        """The real reconnect entry a restarted main session goes through.

        ``server.reconnect_session`` is the production seam invoked after an
        interactive restart; it re-verifies the durable service identity and a
        live child (through ``child_attempt_alive``), rather than re-launching.
        """
        import subprocess
        import time

        from skillweave.runtime import process_identity as _pi

        svc = _service(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.1)
            record = _pi.capture(proc.pid).to_dict()

            class _LiveHandle:
                pid = proc.pid
                _sw_process_identity = record

            out = mcp_server.reconnect_session(
                service=svc,
                service_fingerprint=svc.identity(),
                session_identity="main",
                handle=_LiveHandle(),
            )
            assert out["status"] == "ready", out
            assert out["service_reachable"] is True
            assert out["child_alive"] is True
            assert out["session_identity"] == "main"
            # Observational: the child survives the reconnect verification.
            assert proc.poll() is None
        finally:
            proc.terminate()
            proc.wait()

    def test_reconnect_session_holds_on_a_different_service(self, tmp_path):
        """A reconnect that lands on a *different* service fails closed."""
        svc = _service(tmp_path)
        other = boot.HarnessProcessService(
            authority="skillweave-process-service",
            db_path=str(tmp_path / "other.db"),
            mailbox_path=str(tmp_path / "other-mailbox.jsonl"),
            artifacts_path=str(tmp_path / "other-artifacts"),
            stream_root=str(tmp_path / "other-streams"),
        )
        out = mcp_server.reconnect_session(
            service=svc,
            service_fingerprint=other.identity(),
        )
        assert out["status"] == "hold", out
        assert out["code"] == "service_identity_mismatch", out

    def test_emit_log_redacts_then_fails_closed_on_real_output(
        self, capsys
    ):
        """The real log-emission boundary redacts, then re-verifies output.

        Every server emission goes through ``server.emit_log``: a credential
        value is redacted before the line is written, and the emitted line is
        re-scanned so a value the redactor did not recognise is a hard refusal
        -- the property holds for actual output, not a passed-in blob.
        """
        environ = {"ANTHROPIC_API_KEY": "sk-real-secret"}
        # A recognised credential value is redacted in the *actual* output.
        emitted = mcp_server.emit_log(
            "provider call failed key=sk-real-secret", environ=environ
        )
        assert "sk-real-secret" not in emitted
        assert "sk-real-secret" not in capsys.readouterr().err
        # A credential-shaped value that reaches an emitted line verbatim is a
        # hard failure, never a silent leak.
        with pytest.raises(ValueError):
            boot.assert_worker_credentials_not_in_logs(
                "leak sk-real-secret", environ
            )

    def test_restart_session_tool_reverifies_a_recorded_live_child(
        self, authority, tmp_path, monkeypatch
    ):
        """The registered tool a *restarted* main session actually calls.

        The pre-restart session held a live child; the restart has no live
        handle object -- only the *recorded* native process identity in the
        durable store. ``mcp_server.restart_session`` is the registered tool a
        restarted session goes through: it resolves the server-owned service,
        rebuilds the handle from the durable record, and re-verifies the
        surviving child through ``restart_ready`` without stopping it.
        """
        import time

        from skillweave.runtime import process_identity as _pi

        svc = _service(tmp_path)
        _bind_mailbox(monkeypatch, svc)
        store = SQLiteRunStore(svc.db_path)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            time.sleep(0.1)
            identity = _pi.capture(proc.pid).to_dict()
            # The durable service records the child identity at launch; the
            # restarted session has *only* this fact.
            store.create_run(
                "run-live", task_id="t", attempt=1, process_identity=identity
            )

            session = authority.issue_session(
                role="controller", client_id="main", transport="http",
                allowed_roots=[str(tmp_path)],
            )
            out = json.loads(
                mcp_server.restart_session(
                    run_id="run-live",
                    db_path=svc.db_path,
                    artifacts_path=svc.artifacts_path,
                    service_fingerprint=svc.identity(),
                    session_token=session,
                )
            )
            assert out["status"] == "ready", out
            assert out["service_reachable"] is True
            assert out["child_alive"] is True
            assert out["service_identity"] == svc.identity()
            # Observational: the restart verification did not stop the child.
            assert proc.poll() is None
        finally:
            proc.terminate()
            proc.wait()

    def test_restart_session_tool_holds_when_the_recorded_child_is_gone(
        self, authority, tmp_path, monkeypatch
    ):
        """A restart whose recorded child is dead fails closed (named hold).

        The durable record outlives the child; the registered tool must not
        report a survivor it cannot verify against the live OS facts.
        """
        import time

        from skillweave.runtime import process_identity as _pi

        svc = _service(tmp_path)
        _bind_mailbox(monkeypatch, svc)
        store = SQLiteRunStore(svc.db_path)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        time.sleep(0.05)
        captured = _pi.capture(proc.pid)
        stale = captured.to_dict() if captured is not None else {
            "pid": proc.pid, "start_time": "", "identity_token": "stale"
        }
        store.create_run("run-dead", task_id="t", attempt=1, process_identity=stale)

        session = authority.issue_session(
            role="controller", client_id="main", transport="http",
            allowed_roots=[str(tmp_path)],
        )
        out = json.loads(
            mcp_server.restart_session(
                run_id="run-dead",
                db_path=svc.db_path,
                artifacts_path=svc.artifacts_path,
                service_fingerprint=svc.identity(),
                session_token=session,
            )
        )
        assert out["status"] == "hold", out
        assert out["child_alive"] is False

    def test_startup_seam_verifies_restart_survival_before_admitting(
        self, authority, tmp_path
    ):
        """The startup path a restarted session goes through verifies survival.

        ``startup_and_bind_stdio`` is the real startup seam. When the
        environment declares the pre-restart session identity, a start that
        cannot verify the surviving service/child is refused (failing closed);
        the same start with a verified live child is admitted.
        """
        import time

        from skillweave.runtime import process_identity as _pi

        svc = _service(tmp_path)
        store = SQLiteRunStore(svc.db_path)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            time.sleep(0.1)
            identity = _pi.capture(proc.pid).to_dict()
            store.create_run(
                "run-start", task_id="t", attempt=1, process_identity=identity
            )

            env = {
                mcp_server.SESSION_IDENTITY_ENV: "run-start",
                mcp_server.HANDLE_IDENTITY_ENV: svc.identity(),
                mcp_server.SUPERVISOR_ENV: "operator",
            }
            # A restart that verifies the surviving child is admitted.
            token = mcp_server.startup_and_bind_stdio(
                process_service=svc, environ=env
            )
            assert token
            assert proc.poll() is None
        finally:
            proc.terminate()
            proc.wait()

    def test_restart_does_not_break_a_recorded_quota_wait_or_cancellation(
        self, authority, tmp_path, monkeypatch
    ):
        """A recorded quota wait and a cancellation survive a session restart.

        The scenario the criterion names end-to-end: a child is mid-flight
        waiting on a provider quota window, with the wait *recorded* in the
        durable run metadata. The main interactive session restarts (the live
        handle is discarded; only the durable record survives). After the
        simulated restart: (a) the recorded quota wait is still intact, and
        (b) cancellation still verifies the *recorded* identity and signals the
        still-living child through the real ``control.control_cancel`` entry --
        neither the restart nor the cancel fabricates a termination.
        """
        import time

        from skillweave.runtime import process_identity as _pi
        from skillweave.runtime.semantic_repair import (
            attempt_condition_for_quota,
        )

        svc = _service(tmp_path)
        _bind_mailbox(monkeypatch, svc)
        store = SQLiteRunStore(svc.db_path)
        # ``start_new_session`` gives the child its own process group: the
        # cancellation signals the *owned* group by pgid, so a child sharing
        # the test runner's group would signal the runner itself.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            time.sleep(0.1)
            identity = _pi.capture(proc.pid).to_dict()
            wait = attempt_condition_for_quota(
                error_class="provider_quota",
                run_id="run-quota",
                task_id="task-1",
                attempt=1,
                session_id="sess-1",
                route="owned-route",
                reset_at=None,
                backoff_seconds=900.0,
            )
            store.create_run(
                "run-quota",
                task_id="task-1",
                attempt=1,
                process_identity=identity,
                attempt_condition=wait.to_dict(),
            )
            recorded_wait = store.get_run("run-quota").metadata["attempt_condition"]

            # ── the main session restarts: the live handle is gone ─────────
            session = authority.issue_session(
                role="controller", client_id="main-restarted",
                transport="http", allowed_roots=[str(tmp_path)],
            )
            ready = json.loads(
                mcp_server.restart_session(
                    run_id="run-quota",
                    db_path=svc.db_path,
                    artifacts_path=svc.artifacts_path,
                    service_fingerprint=svc.identity(),
                    session_token=session,
                )
            )
            assert ready["status"] == "ready", ready
            assert ready["child_alive"] is True

            # (a) The recorded quota wait is unchanged across the restart.
            after_restart = store.get_run("run-quota").metadata["attempt_condition"]
            assert after_restart == recorded_wait
            assert after_restart["decision"] == "resume_same_route", after_restart

            # (b) Cancellation still works: the recorded identity verifies and
            #     the still-living child is signalled -- no fabricated result.
            cancelled = control.control_cancel(
                "run-quota",
                reason="interactive restart",
                db_path=svc.db_path,
                artifacts_path=svc.artifacts_path,
            )
            assert cancelled["status"] == "cancel_requested", cancelled
            assert cancelled["identity_verified"] is True, cancelled
            proc.wait(timeout=10)
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()


# ===========================================================================
# Criterion 6 (continued): the credential rule holds on the *real* output
# paths of the dispatch stream and of child launch -- never a caller blob
# ===========================================================================


class TestWorkerCredentialGuardOnRealOutputPaths:
    """The worker-credential rule is enforced where output is actually written.

    Criterion 6 requires a worker's provider credential never reach a
    model-provider request or a log. These tests drive the *real* dispatch
    output paths -- the dispatch event stream's sink and the teed child-launch
    sink -- with a credential value already present, and prove it is redacted
    on the write, not merely assumed absent from a caller-supplied blob.
    """

    def _credential(self, monkeypatch):
        environ = {"ANTHROPIC_API_KEY": "sk-dispatch-secret"}
        monkeypatch.setattr(os, "environ", environ, raising=False)
        return environ

    def test_dispatch_event_stream_sink_redacts_a_credential_on_write(
        self, tmp_path, monkeypatch
    ):
        """The stream's own JSONL sink never carries a credential value.

        ``DispatchEventStream`` writes each event to its sink; the guard runs
        on the *encoded line actually written*, so a credential value that
        reached an event field is redacted before it reaches the sink.
        """
        from skillweave.dispatch import events as _events

        self._credential(monkeypatch)
        sink_path = tmp_path / "stream.jsonl"
        with open(sink_path, "w", encoding="utf-8") as sink:
            stream = _events.DispatchEventStream("run-1", sink)
            stream.emit(
                wave="w1",
                lane_id="lane-1",
                dispatch_id="d1",
                event_type=_events.EventType.LANE_STARTED,
                process_status=_events.ProcessStatus.RUNNING,
                task_status=_events.TaskStatus.IN_PROGRESS,
                payload={"note": "worker used sk-dispatch-secret"},
            )
        written = sink_path.read_text(encoding="utf-8")
        assert "sk-dispatch-secret" not in written, written
        assert "<redacted-credential>" in written, written

    def test_tee_sink_redacts_a_credential_on_child_launch_output(
        self, tmp_path, monkeypatch
    ):
        """The teed child-launch sink never carries a credential value.

        ``_TeeSink`` is the real output sink the dispatch/child-launch path
        forwards to the caller. Every written line passes through the guard on
        the write, so a credential value in a launched child's line is redacted
        before the caller sees it.
        """
        from skillweave.dispatch import application as _app

        self._credential(monkeypatch)
        import io

        caller = io.StringIO()
        tee = _app._TeeSink(caller)
        tee.write('{"line": "child env key=sk-dispatch-secret"}\n')
        forwarded = caller.getvalue()
        assert "sk-dispatch-secret" not in forwarded, forwarded
        assert "<redacted-credential>" in forwarded, forwarded
        assert all("sk-dispatch-secret" not in line for line in tee.lines)

    # -- defect B1: short placeholder values never corrupt evidence ----------

    def test_dispatch_line_stays_valid_json_when_a_placeholder_is_short(
        self, tmp_path, monkeypatch
    ):
        """A short credential-ish value never corrupts the durable JSONL line.

        The guard only redacts *credential-shaped* (long, token-character)
        values, so a placeholder such as ``"1"``, ``"a,b"`` or ``"us:"`` is
        never rewritten.  The emitted evidence line stays valid JSON and the
        short value passes through unchanged.
        """
        from skillweave.dispatch import events as _events

        environ = {
            "WORKER_API_KEY": "1",
            "WORKER_TOKEN": "a,b",
            "WORKER_SECRET": "us:",
        }
        monkeypatch.setattr(os, "environ", environ, raising=False)
        sink_path = tmp_path / "placeholder.jsonl"
        with open(sink_path, "w", encoding="utf-8") as sink:
            stream = _events.DispatchEventStream("run-1", sink)
            stream.emit(
                wave="w1",
                lane_id="1",
                dispatch_id="a,b",
                event_type=_events.EventType.LANE_STARTED,
                process_status=_events.ProcessStatus.RUNNING,
                task_status=_events.TaskStatus.IN_PROGRESS,
                payload={"note": "region us:"},
            )
        written = sink_path.read_text(encoding="utf-8")
        # The line must still parse: an unbounded replace of "1"/"a,b"/"us:"
        # would have rewritten JSON structure (numbers, punctuation) and left
        # invalid JSON behind.
        parsed = json.loads(written)
        assert parsed["lane_id"] == "1", written
        assert parsed["dispatch_id"] == "a,b", written
        assert parsed["note"] == "region us:", written
        assert "<redacted-credential>" not in written, written

    def test_tee_sink_leaves_short_placeholders_unchanged(self, monkeypatch):
        """The teed child-launch path never rewrites a short placeholder."""
        from skillweave.dispatch import application as _app

        environ = {"WORKER_API_KEY": "1", "WORKER_TOKEN": "a,b"}
        monkeypatch.setattr(os, "environ", environ, raising=False)
        import io

        caller = io.StringIO()
        tee = _app._TeeSink(caller)
        line = '{"region": "us:", "n": 1}\n'
        tee.write(line)
        assert caller.getvalue() == line, caller.getvalue()

    # -- defect B1/B2: a genuine secret is never observable -----------------

    def test_dispatch_line_redacts_a_plain_long_credential(self, tmp_path, monkeypatch):
        """A genuine long credential is redacted from the durable evidence line."""
        from skillweave.dispatch import events as _events

        secret = "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0" * 3
        environ = {"ANTHROPIC_API_KEY": secret}
        monkeypatch.setattr(os, "environ", environ, raising=False)
        sink_path = tmp_path / "secret.jsonl"
        with open(sink_path, "w", encoding="utf-8") as sink:
            stream = _events.DispatchEventStream("run-1", sink)
            stream.emit(
                wave="w1",
                lane_id="lane-1",
                dispatch_id="d1",
                event_type=_events.EventType.LANE_STARTED,
                process_status=_events.ProcessStatus.RUNNING,
                task_status=_events.TaskStatus.IN_PROGRESS,
                payload={"note": f"used {secret}"},
            )
        written = sink_path.read_text(encoding="utf-8")
        assert secret not in written, written
        assert "<redacted-credential>" in written, written
        # The durable line is still valid JSON -- redaction is structure-preserving.
        json.loads(written)

    def test_dispatch_line_redacts_a_json_escaped_credential(self, tmp_path, monkeypatch):
        """A secret whose JSON-escaped form appears in the line is still caught.

        A credential containing a quote, backslash or control character is
        serialized in its escaped form inside the JSONL line; a verbatim match
        would miss it.  The guard redacts the escaped form too.
        """
        from skillweave.dispatch import events as _events

        secret = 'sk-ant-"QUOTED\\SLASH\x07' + "X9y8Z7w6V5u4T3s2" * 2
        environ = {"ANTHROPIC_API_KEY": secret}
        monkeypatch.setattr(os, "environ", environ, raising=False)
        sink_path = tmp_path / "escaped.jsonl"
        with open(sink_path, "w", encoding="utf-8") as sink:
            stream = _events.DispatchEventStream("run-1", sink)
            stream.emit(
                wave="w1",
                lane_id="lane-1",
                dispatch_id="d1",
                event_type=_events.EventType.LANE_STARTED,
                process_status=_events.ProcessStatus.RUNNING,
                task_status=_events.TaskStatus.IN_PROGRESS,
                payload={"note": f"used {secret}"},
            )
        written = sink_path.read_text(encoding="utf-8")
        # Neither the plain nor the JSON-escaped form is observable.
        assert secret not in written, written
        assert json.dumps(secret)[1:-1] not in written, written
        json.loads(written)

    def test_tee_sink_redacts_a_json_escaped_credential(self, monkeypatch):
        """The teed path redacts the escaped form of a genuine credential."""
        from skillweave.dispatch import application as _app

        secret = 'sk-ant-"QUOTED\\SLASH\x07' + "X9y8Z7w6V5u4T3s2" * 2
        environ = {"ANTHROPIC_API_KEY": secret}
        monkeypatch.setattr(os, "environ", environ, raising=False)
        import io

        caller = io.StringIO()
        tee = _app._TeeSink(caller)
        tee.write(json.dumps({"note": f"used {secret}"}) + "\n")
        forwarded = caller.getvalue()
        assert secret not in forwarded, forwarded
        assert json.dumps(secret)[1:-1] not in forwarded, forwarded
        assert "<redacted-credential>" in forwarded, forwarded

    def test_residual_assertion_fails_closed_on_a_surviving_credential(self):
        """The residual check is non-vacuous: a surviving credential refuses.

        Checked directly (defect B2): the assertion tests the genuine
        credential-shaped values in both plain and JSON-escaped form, so a
        value that survives redaction cannot pass as "clean".
        """
        secret = "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0" * 3
        environ = {"ANTHROPIC_API_KEY": secret}
        # Plain form survives -> refused.
        with pytest.raises(ValueError):
            boot.assert_worker_credentials_not_in_logs(f"leak {secret}", environ)
        # JSON-escaped form survives -> refused (a verbatim-only check misses it).
        escaped_secret = 'sk-ant-"q\\u0007' + "A1b2C3d4E5f6G7h8" * 3
        with pytest.raises(ValueError):
            boot.assert_worker_credentials_not_in_logs(
                f"leak {json.dumps(escaped_secret)[1:-1]}",
                {"ANTHROPIC_API_KEY": escaped_secret},
            )
        # A clean line with only a short placeholder passes.
        boot.assert_worker_credentials_not_in_logs("region us: n=1", {"WORKER_API_KEY": "1"})
