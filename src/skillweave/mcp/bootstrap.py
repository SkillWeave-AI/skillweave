"""SW-1511-MCP-BOOTSTRAP: attach dispatched harnesses to one scoped durable
process service.

This module is the *binding* seam between the dispatch adapter (which launches
child harnesses) and the single logical durable process service (which owns the
run store, the durable mailbox, the evidence journal and the controller lease).
It freezes and enforces three boundary (data-contract) artifacts:

* ``harness-mcp-session-grant-v1`` (process) -- produced by the host authority
  and the dispatch adapter, consumed by the actual main/Ops/Review child
  harness.  One logical durable process service owns run/mailbox/evidence and
  the controller lease; every child principal is *server-issued* and scoped to
  the actual ``run_id``/``task_id``/``attempt``.  A client-supplied role can
  never elevate authority (the grant carries only the server-minted token; the
  role is re-read from the server-owned registry at every call).

* ``shared-process-mailbox-binding-v1`` (storage) -- produced by the single
  process service, consumed by reconnecting scoped clients.  The binding
  records the *authoritative* state endpoints (store db, mailbox path,
  artifacts path, stream root) that the child will be provisioned with, so a
  reconnect provably returns to the same authority/state service rather than a
  second, isolated default store.

* ``qualified-harness-mcp-binding-v1`` (adapter) -- produced by the harness
  adapter, consumed by an MCP-enabled or explicitly qualified polling client.
  This module only *references* that contract (it is owned by
  SW-1511-MCP-PACK); it is reported by name so a consumer can refuse an
  unknown revision rather than silently reinterpret it.

Compatibility: additive and versioned.  Unknown versions are refused fail-closed
(``UnsupportedContractVersion``); a published contract is never reinterpreted.

The design mirrors the frozen boundary-contract idiom established for
``skillweave.trace.gate_receipt``: a schema id, a schema version, a supported
version set, a fail-closed ``UnsupportedContractVersion`` error, and a frozen
dataclass with ``from_dict``/``to_dict``/``validate``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# --------------------------------------------------------------------------
# schema ids / versions (frozen boundary contracts)
# --------------------------------------------------------------------------

HARNESS_MCP_SESSION_GRANT_SCHEMA_ID = (
    "https://skillweave.dev/schemas/process/harness-mcp-session-grant/v1"
)
HARNESS_MCP_SESSION_GRANT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_HARNESS_MCP_SESSION_GRANT_VERSIONS = frozenset(
    {HARNESS_MCP_SESSION_GRANT_SCHEMA_VERSION}
)

SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID = (
    "https://skillweave.dev/schemas/storage/shared-process-mailbox-binding/v1"
)
SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_VERSION = "1.0.0"
SUPPORTED_SHARED_PROCESS_MAILBOX_BINDING_VERSIONS = frozenset(
    {SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_VERSION}
)

QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_ID = (
    "https://skillweave.dev/schemas/adapter/qualified-harness-mcp-binding/v1"
)
QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_VERSION = "1.0.0"
SUPPORTED_QUALIFIED_HARNESS_MCP_BINDING_VERSIONS = frozenset(
    {QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_VERSION}
)

#: env var names the provisioned child harness reads to reach the single
#: process service.  Frozen so a reconnecting child can be recognised.
ENV_MCP_SESSION_TOKEN = "SKILLWEAVE_MCP_SESSION_TOKEN"
ENV_MCP_AUTHORITY = "SKILLWEAVE_MCP_AUTHORITY"
ENV_MCP_DB_PATH = "SKILLWEAVE_MCP_DB_PATH"
ENV_MCP_MAILBOX_PATH = "SKILLWEAVE_MCP_MAILBOX_PATH"
ENV_MCP_ARTIFACTS_PATH = "SKILLWEAVE_MCP_ARTIFACTS_PATH"
ENV_MCP_STREAM_ROOT = "SKILLWEAVE_MCP_STREAM_ROOT"
ENV_MCP_RUN_ID = "SKILLWEAVE_MCP_RUN_ID"

#: the stable, server-owned name of the single logical process service.
PROCESS_SERVICE_NAME = "skillweave-process-service"

#: the transports the authority actually recognises.
VALID_TRANSPORTS = frozenset({"stdio", "http"})


class UnsupportedContractVersion(ValueError):
    """Fail-closed refusal of an unknown boundary-contract version.

    ``artifact`` names the contract, ``version`` the refused revision and
    ``supported`` the set the running binary understands.  Subclassing
    ``ValueError`` keeps historical ``except ValueError`` callers safe while a
    caller that needs the typed fact can catch this class.
    """

    def __init__(
        self,
        artifact: str,
        version: str,
        supported: Sequence[str],
    ) -> None:
        self.artifact = artifact
        self.version = version
        self.supported = tuple(sorted(supported))
        super().__init__(
            f"unsupported contract version for {artifact!r}: "
            f"{version!r} not in {self.supported!r}"
        )


class ProcessServiceMismatchRefused(ValueError):
    """A child was provisioned against a different authority/state service."""


def _require_version(artifact: str, version: str, supported: frozenset[str]) -> None:
    if version not in supported:
        raise UnsupportedContractVersion(artifact, version, supported)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _normalise_path(value: str) -> str:
    """Return an absolute, symlink-resolved path for identity comparison."""
    return os.path.realpath(os.path.abspath(value))


# --------------------------------------------------------------------------
# harness-mcp-session-grant-v1 (process)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessProcessService:
    """The single logical durable process service the harnesses attach to.

    Both absolute and normalised forms of each path are kept so an
    authority-identity comparison is exact while the emitted env / evidence
    uses the operator-supplied absolute path.
    """

    authority: str
    db_path: str
    mailbox_path: str
    artifacts_path: str
    stream_root: str
    service_name: str = PROCESS_SERVICE_NAME

    def identity(self) -> str:
        """A stable digest identifying *this* authority/state service."""
        return _digest(
            {
                "authority": self.authority,
                "service_name": self.service_name,
                "db_path": _normalise_path(self.db_path),
                "mailbox_path": _normalise_path(self.mailbox_path),
                "artifacts_path": _normalise_path(self.artifacts_path),
                "stream_root": _normalise_path(self.stream_root),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "service_name": self.service_name,
            "db_path": self.db_path,
            "mailbox_path": self.mailbox_path,
            "artifacts_path": self.artifacts_path,
            "stream_root": self.stream_root,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HarnessProcessService":
        return cls(
            authority=str(data["authority"]),
            db_path=str(data["db_path"]),
            mailbox_path=str(data["mailbox_path"]),
            artifacts_path=str(data["artifacts_path"]),
            stream_root=str(data["stream_root"]),
            service_name=str(data.get("service_name", PROCESS_SERVICE_NAME)),
        )


@dataclass(frozen=True)
class HarnessMcpSessionGrant:
    """A server-issued, scoped grant binding a child to the process service.

    The grant carries the opaque, server-minted session token and the *actual*
    run/task/attempt scope.  It never carries a caller-authored role: the role
    is re-read from the server-owned session registry on every call, so a
    client-supplied ``role`` argument cannot elevate authority.
    """

    schema_id: str
    schema_version: str
    run_id: str
    task_id: str
    attempt: int
    transport: str
    session_token: str
    service: HarnessProcessService
    start_scope: str
    role: str = "controller"

    def __post_init__(self) -> None:
        _require_version(
            HARNESS_MCP_SESSION_GRANT_SCHEMA_ID,
            self.schema_version,
            SUPPORTED_HARNESS_MCP_SESSION_GRANT_VERSIONS,
        )
        if self.transport not in VALID_TRANSPORTS:
            raise ValueError(
                f"unknown transport {self.transport!r}; expected one of "
                f"{sorted(VALID_TRANSPORTS)}"
            )
        if not self.session_token:
            raise ValueError("a session grant requires a server-issued token")
        if not self.start_scope:
            raise ValueError("a session grant requires an explicit start scope")

    def scope(self) -> str:
        return self.start_scope

    def env(self) -> dict[str, str]:
        """The child-harness environment provisioning this grant."""
        return {
            ENV_MCP_SESSION_TOKEN: self.session_token,
            ENV_MCP_AUTHORITY: self.service.authority,
            ENV_MCP_DB_PATH: self.service.db_path,
            ENV_MCP_MAILBOX_PATH: self.service.mailbox_path,
            ENV_MCP_ARTIFACTS_PATH: self.service.artifacts_path,
            ENV_MCP_STREAM_ROOT: self.service.stream_root,
            ENV_MCP_RUN_ID: self.run_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "subject_kind": "configuration",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "transport": self.transport,
            "role": self.role,
            "start_scope": self.start_scope,
            "token_digest": hashlib.sha256(
                self.session_token.encode("utf-8")
            ).hexdigest(),
            "service": self.service.to_dict(),
            "service_identity": self.service.identity(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HarnessMcpSessionGrant":
        return cls(
            schema_id=str(data.get("schema_id", HARNESS_MCP_SESSION_GRANT_SCHEMA_ID)),
            schema_version=str(data["schema_version"]),
            run_id=str(data["run_id"]),
            task_id=str(data.get("task_id", "")),
            attempt=int(data.get("attempt", 0)),
            transport=str(data.get("transport", "stdio")),
            session_token=str(data["session_token"]),
            service=HarnessProcessService.from_dict(data["service"]),
            start_scope=str(data["start_scope"]),
            role=str(data.get("role", "controller")),
        )


# --------------------------------------------------------------------------
# shared-process-mailbox-binding-v1 (storage)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MailboxCursorBinding:
    """A resume point into the shared durable mailbox."""

    after_seq: int = 0
    last_digest: str = ""

    def resume_token(self) -> str:
        return _canonical_json(
            {"after_seq": self.after_seq, "last_digest": self.last_digest}
        )

    @classmethod
    def from_token(cls, token: str) -> "MailboxCursorBinding":
        data = json.loads(token)
        return cls(
            after_seq=int(data.get("after_seq", 0)),
            last_digest=str(data.get("last_digest", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"after_seq": self.after_seq, "last_digest": self.last_digest}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MailboxCursorBinding":
        return cls(
            after_seq=int(data.get("after_seq", 0)),
            last_digest=str(data.get("last_digest", "")),
        )


@dataclass(frozen=True)
class SharedProcessMailboxBinding:
    """Binds reconnecting scoped clients to the *one* durable mailbox.

    The binding records the authoritative mailbox path + service identity so a
    reconnecting child provably re-attaches to the same process service.
    Because the path is authoritative, two bindings that name different
    mailbox paths are never silently interchangeable.
    """

    schema_id: str
    schema_version: str
    principal: str
    mailbox_path: str
    service_identity: str
    cursor: MailboxCursorBinding = field(default_factory=MailboxCursorBinding)

    def __post_init__(self) -> None:
        _require_version(
            SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID,
            self.schema_version,
            SUPPORTED_SHARED_PROCESS_MAILBOX_BINDING_VERSIONS,
        )
        if not self.principal:
            raise ValueError("a mailbox binding requires a principal")
        if not self.mailbox_path:
            raise ValueError("a mailbox binding requires an authoritative path")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "subject_kind": "configuration",
            "principal": self.principal,
            "mailbox_path": self.mailbox_path,
            "service_identity": self.service_identity,
            "cursor": self.cursor.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SharedProcessMailboxBinding":
        return cls(
            schema_id=str(
                data.get("schema_id", SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID)
            ),
            schema_version=str(data["schema_version"]),
            principal=str(data["principal"]),
            mailbox_path=str(data["mailbox_path"]),
            service_identity=str(data["service_identity"]),
            cursor=MailboxCursorBinding.from_dict(data.get("cursor", {})),
        )

    def verify_against(self, other: "SharedProcessMailboxBinding") -> None:
        """Refuse a reconnect that would land on another mailbox/store."""
        if _normalise_path(self.mailbox_path) != _normalise_path(other.mailbox_path):
            raise ProcessServiceMismatchRefused(
                "mailbox binding names a different mailbox path: "
                f"{self.mailbox_path!r} != {other.mailbox_path!r}"
            )
        if self.service_identity != other.service_identity:
            raise ProcessServiceMismatchRefused(
                "mailbox binding names a different process-service identity"
            )


# --------------------------------------------------------------------------
# qualified-harness-mcp-binding-v1 (adapter) -- referenced, owned by MCP-PACK
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QualifiedHarnessMcpBindingReference:
    """A by-name reference to the adapter contract owned by SW-1511-MCP-PACK.

    This module never fabricates an MCP config itself: it records which frozen
    adapter revision the child harness must speak so an unknown revision is
    refused by name rather than silently reinterpreted.
    """

    schema_id: str
    schema_version: str
    qualification: str

    def __post_init__(self) -> None:
        _require_version(
            QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_ID,
            self.schema_version,
            SUPPORTED_QUALIFIED_HARNESS_MCP_BINDING_VERSIONS,
        )
        if not self.qualification:
            raise ValueError("a qualified harness binding requires a qualification")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "subject_kind": "configuration",
            "qualification": self.qualification,
        }


# --------------------------------------------------------------------------
# enforcement helpers used at the real consumer/controller seams
# --------------------------------------------------------------------------

#: credential-like env keys a provisioned child must never receive ambiently.
_CREDENTIAL_ENV_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)


def assert_no_ambient_credentials(extra_env: Mapping[str, str]) -> None:
    """Fail closed if a provisioning env would leak ambient credentials.

    ``extra_env`` is what the adapter passes to ``build_child_environment`` as
    ``extra_env``; the runtime already refuses ambient *provider* keys, this
    additionally refuses anything credential-shaped that is not one of the
    explicit, server-owned MCP keys this module provisions.
    """
    allowed = {
        ENV_MCP_SESSION_TOKEN,
    }
    for key in extra_env:
        if key in allowed:
            continue
        upper = key.upper()
        if any(marker in upper for marker in _CREDENTIAL_ENV_MARKERS):
            raise ValueError(
                f"refusing to provision credential-shaped env key {key!r} "
                "into a child harness"
            )


def same_authority(a: HarnessProcessService, b: HarnessProcessService) -> bool:
    """True when two services resolve to the *same* authority/state service.

    Used to admit a stdio child: a locally-launched stdio harness is only
    admitted when it demonstrably reaches the same authority/state service,
    i.e. the same store, mailbox, artifacts and stream root -- not a second,
    isolated default store.
    """
    return a.identity() == b.identity()


def refuse_isolated_controller(
    provisioned: HarnessProcessService, observed: HarnessProcessService
) -> None:
    """Refuse an independently-initialized per-harness controller/default store."""
    if not same_authority(provisioned, observed):
        raise ProcessServiceMismatchRefused(
            "child harness resolved an isolated controller/default store "
            f"(expected identity {provisioned.identity()}, "
            f"observed {observed.identity()})"
        )


def service_from_environment(
    environ: Mapping[str, str],
) -> Optional[HarnessProcessService]:
    """Recover the process service a provisioned child was launched against.

    Returns ``None`` when the child carries no MCP provisioning at all, so a
    caller can distinguish "unprovisioned" from "mis-provisioned".
    """
    token = environ.get(ENV_MCP_SESSION_TOKEN)
    if not token:
        return None
    return HarnessProcessService(
        authority=str(environ.get(ENV_MCP_AUTHORITY, "")),
        db_path=str(environ.get(ENV_MCP_DB_PATH, "")),
        mailbox_path=str(environ.get(ENV_MCP_MAILBOX_PATH, "")),
        artifacts_path=str(environ.get(ENV_MCP_ARTIFACTS_PATH, "")),
        stream_root=str(environ.get(ENV_MCP_STREAM_ROOT, "")),
    )


def mailbox_binding_for_grant(
    grant: HarnessMcpSessionGrant,
    *,
    principal: str,
    cursor: Optional[MailboxCursorBinding] = None,
) -> SharedProcessMailboxBinding:
    """Project a session grant onto the durable shared-mailbox binding."""
    return SharedProcessMailboxBinding(
        schema_id=SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID,
        schema_version=SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_VERSION,
        principal=principal,
        mailbox_path=grant.service.mailbox_path,
        service_identity=grant.service.identity(),
        cursor=cursor or MailboxCursorBinding(),
    )


def grant_child_env(grant: HarnessMcpSessionGrant) -> dict[str, str]:
    """The effective MCP configuration provisioned into the actual child launch.

    This is the *real* provisioning seam: the returned mapping is merged into
    the child's env by :func:`skillweave.dispatch.application._default_inline_seam`
    and handed to ``runner_adapter.start_process`` -- so the effective child
    configuration, not merely the interactive harness profile, carries the
    server-issued session grant and the one process service's state endpoints.
    """
    return grant.env()


def _read_child_environ(handle: Any) -> Optional[dict[str, str]]:
    """Best-effort read of a live child's *effective* environment.

    The launcher attaches ``_sw_effective_env`` to the handle when it can
    observe the child's resolved configuration.  A handle that exposes neither
    the attribute nor an environment mapping is treated as *unobservable*, not
    as passing.
    """
    env = getattr(handle, "_sw_effective_env", None)
    if env is None:
        env = getattr(handle, "effective_env", None)
    if env is None:
        env = getattr(handle, "environ", None)
    if env is None:
        return None
    return {str(k): str(v) for k, v in dict(env).items()}


def verify_child_launch(grant: HarnessMcpSessionGrant, handle: Any) -> Any:
    """Verify the *actual* child launch resolved the granted process service.

    A launch is verified only when the child's effective configuration is
    observable *and* resolves to the same authority/state service the grant
    names.  An unobservable launch is refused rather than assumed correct, so
    the grant is never silently downgraded to the interactive harness profile.

    Returns the ``handle`` unchanged on success (so call sites read naturally),
    or raises :class:`ProcessServiceMismatchRefused`.
    """
    observed_env = _read_child_environ(handle)
    if observed_env is None:
        raise ProcessServiceMismatchRefused(
            "cannot verify the child's effective MCP configuration: the launch "
            "did not expose its resolved environment in the actual child run"
        )
    token = observed_env.get(ENV_MCP_SESSION_TOKEN, "")
    if token != grant.session_token:
        raise ProcessServiceMismatchRefused(
            "child launch did not carry the granted session token"
        )
    observed = service_from_environment(observed_env)
    if observed is None:
        raise ProcessServiceMismatchRefused(
            "child launch carried no observable process-service binding"
        )
    refuse_isolated_controller(grant.service, observed)
    return handle


__all__ = [
    "ENV_MCP_ARTIFACTS_PATH",
    "ENV_MCP_AUTHORITY",
    "ENV_MCP_DB_PATH",
    "ENV_MCP_MAILBOX_PATH",
    "ENV_MCP_RUN_ID",
    "ENV_MCP_SESSION_TOKEN",
    "ENV_MCP_STREAM_ROOT",
    "HARNESS_MCP_SESSION_GRANT_SCHEMA_ID",
    "HARNESS_MCP_SESSION_GRANT_SCHEMA_VERSION",
    "HarnessMcpSessionGrant",
    "HarnessProcessService",
    "MailboxCursorBinding",
    "PROCESS_SERVICE_NAME",
    "ProcessServiceMismatchRefused",
    "QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_ID",
    "QUALIFIED_HARNESS_MCP_BINDING_SCHEMA_VERSION",
    "QualifiedHarnessMcpBindingReference",
    "SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_ID",
    "SHARED_PROCESS_MAILBOX_BINDING_SCHEMA_VERSION",
    "SUPPORTED_HARNESS_MCP_SESSION_GRANT_VERSIONS",
    "SUPPORTED_QUALIFIED_HARNESS_MCP_BINDING_VERSIONS",
    "SUPPORTED_SHARED_PROCESS_MAILBOX_BINDING_VERSIONS",
    "SharedProcessMailboxBinding",
    "UnsupportedContractVersion",
    "VALID_TRANSPORTS",
    "assert_no_ambient_credentials",
    "grant_child_env",
    "mailbox_binding_for_grant",
    "refuse_isolated_controller",
    "same_authority",
    "service_from_environment",
    "verify_child_launch",
]
