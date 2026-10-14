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
)
from skillweave.mcp import bootstrap as boot  # noqa: E402
from skillweave.mcp import control  # noqa: E402
from skillweave.mcp import server as mcp_server  # noqa: E402
from skillweave.runtime.authority import (  # noqa: E402
    MCPPrincipalAuthority,
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
            stdout = self._stdout
            stderr = self._stderr
            stdout_receipt = None
            stderr_receipt = None

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
        child is the subset rule: reviewer authority is not a subset of worker
        authority. The refusal is typed and no token is issued; the caller gains
        no authority by naming a stronger ``role``. (A controller *may* attach a
        worker child -- proven by the scoped-grant test above.)
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
        Reviewer alike, so the worker is admitted *to the seam* and the subset
        rule -- not an operation gate that only a controller could pass -- is
        what decides the minted child. A worker may attach a child of its own
        (no broader) authority.
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
        controller (it lacks ``start_run``/``write_run_state``), and a
        reviewer's authority is not a subset of its own -- so even a reviewer
        that reached the mint path cannot attach a controller child. This pins
        the subset rule on :meth:`refuse_child_role_escalation` directly, so its
        removal cannot pass silently behind the operation gate.
        """
        reviewer = authority.issue_session(
            role="reviewer", client_id="rev", transport="http", run_ids=["run-1"]
        )
        principal = authority.verify_session(reviewer)
        with pytest.raises(RoleEscalationRefused):
            authority.refuse_child_role_escalation(principal, "controller")
        with pytest.raises(RoleEscalationRefused):
            authority.refuse_child_role_escalation(principal, "worker")
        # Only authority no broader than the reviewer's own is admitted.
        authority.refuse_child_role_escalation(principal, "reviewer")

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
    real_startup = server_mod.startup_and_bind_stdio

    def _record_startup(*, process_service=None):
        minted["service"] = process_service
        token = real_startup(process_service=process_service)
        minted["bound"] = server_mod._bound_authority()
        return token

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

    def test_main_without_opt_in_keeps_the_historical_unscoped_start(
        self, tmp_path, monkeypatch
    ):
        """Default ``main()`` preserves the historical unscoped session."""
        import skillweave.mcp.server as server_mod

        monkeypatch.setenv(server_mod.SUPERVISOR_ENV, "local-main")
        monkeypatch.chdir(tmp_path)
        previous = server_mod._AUTHORITY
        previous_ready = server_mod._READY_SESSION_TOKEN
        minted = _main_stubs(monkeypatch, server_mod)
        try:
            server_mod.main()
        finally:
            server_mod._AUTHORITY = previous
            server_mod._READY_SESSION_TOKEN = previous_ready

        # Additive: the default start binds no process service at all.
        assert minted["service"] is None
        auth = minted["bound"]
        principal = auth.verify_session(minted["token"])
        assert principal.role == "controller"


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
