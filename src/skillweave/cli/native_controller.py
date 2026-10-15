"""The ``skillweave native-controller`` production command-line surface.

This is the **production entry point** for
:class:`skillweave.runtime.native_process.NativeProcessService` (SW-SCOPE-010,
unit A1).  Before this command the service existed and passed independent review
at the method level, but no production code ever constructed it, acquired its
fenced controller lease or drove the owned loop -- so every criterion worded
"one native process service drives the loop" held at library level only.

The command composes the service on the **same durable store the ``run``
command uses** (``SQLiteRunStore`` over ``--db-path``), acquires the single
fenced controller lease, and drives one owned attempt **from durable state**:

* the loop is **state-driven**, not a fixed six-step script.  Each transition is
  a guarded compare-and-set from the exact state the previous step left the
  intent in, so an out-of-order or verdict-inconsistent advance is *refused*;
* the review outcome is read from the **durable review record** the MCP
  ``review_run`` tool wrote (a ``transitions_log`` row with ``to_state``
  ``advance_or_stop`` and ``reason`` ``Review verdict {verdict}: ...``) -- never
  a value this controller makes up.  A recorded ``fail`` leads to repair and a
  re-review; a failing re-review *stops* the attempt terminally and it can never
  integrate;
* acceptance comes only from **independent verifier evidence** through the
  gate-receipt path (``skillweave.trace.gate_receipt``): the controller builds no
  ``accepted=True`` itself and refuses fail-closed when the reviewer port
  supplied no independent evidence;
* the **dependent wave** is computed from the declared dependency graph through
  :func:`skillweave.runtime.dagscheduler.build_schedule` (a dependency is
  released only when its gate receipt records an accepted, artifact-carrying
  attempt) -- not from a fabricated name.

Two ports are **explicit and required**:

* the **worker adapter** -- the one place a real drive would launch the owned
  attempt (a model, a provider, a subprocess).  A test injects a fake that
  returns a synthetic execution and launches nothing;
* the **reviewer adapter** -- the independent review seam.  It supplies the
  independent verifier evidence the controller consumes; it does **not** get to
  hand the controller a verdict.

With no production adapter configured the command **refuses fail-closed** with a
named unsupported hold (``EXIT_REFUSED``) rather than substituting a no-op.  It
is additive and opt-in: one subcommand, writing only to the store the caller
names; no existing subcommand or record changes, and the legacy unguarded
service methods keep their exact behaviour for existing callers.

Signal safety: the lease is released on ``SIGTERM``, ``SIGINT``,
``KeyboardInterrupt``, any exception and normal exit, and it is renewed (heartbeat)
while a step runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

# Public surface re-exported for callers/tests that key on the entry point.
__all__ = [
    "EXIT_OK",
    "EXIT_INVALID_INPUT",
    "EXIT_REFUSED",
    "EXIT_FAILED",
    "AttemptExecution",
    "WorkerAdapter",
    "ReviewerAdapter",
    "ReviewVerdict",
    "DriveResult",
    "build_production_ports",
    "build_production_reviewer_port",
    "build_parser",
    "main",
]

#: ``ok``: the controller drove the owned attempt through the declared loop.
EXIT_OK = 0
#: ``invalid input``: missing/blank required flag, or an argparse usage error.
EXIT_INVALID_INPUT = 2
#: ``refused``: the service refused -- no production worker/reviewer adapter is
#: configured (a named unsupported hold), a live controller holds the lease, or
#: any other typed service refusal. Fail closed.
EXIT_REFUSED = 3
#: ``failure``: an unexpected error. A crash is not a verdict and is never
#: collapsed into a normal outcome.
EXIT_FAILED = 5

#: The recorded review verdicts the loop accepts, exactly as the MCP
#: ``review_run`` tool records them.
REVIEW_PASS = "pass"
REVIEW_FAIL = "fail"

#: The unsupported hold named when no production worker adapter is configured.
#: The command refuses rather than substituting a no-op digest worker.
HOLD_NO_WORKER = (
    "native-controller refused: no production worker adapter is configured; "
    "the controller never substitutes a no-op or synthetic worker (unsupported)"
)
#: The unsupported hold named when no production reviewer adapter is configured.
HOLD_NO_REVIEWER = (
    "native-controller refused: no production reviewer adapter is configured; "
    "the controller never constructs its own verdict or acceptance (unsupported)"
)

#: Named holds for the production worker port.  Each names the exact
#: trust-table row that failed, so a refusal is never anonymous and the
#: controller never records an owned attempt against an unverified upstream.
HOLD_GATEWAY_UNCONFIGURED = (
    "native-controller refused: the answering gateway endpoint is unconfigured; "
    "no operator endpoint configuration resolves to an approved gateway, so the "
    "attempt is never launched or recorded against it (named hold)"
)
HOLD_GATEWAY_UNREACHABLE = (
    "native-controller refused: the answering gateway endpoint is unreachable; "
    "the configured operator endpoint did not accept a connection, so the "
    "attempt is never launched against it (named hold)"
)
HOLD_GATEWAY_FALLBACK_CAPABLE = (
    "native-controller refused: the gateway is fallback-capable for the "
    "requested model; a masked fallback answer is never recorded as a receipt "
    "(named hold)"
)
HOLD_MODEL_UNCONFIGURED = (
    "native-controller refused: no worker model is configured for the "
    "production worker port; the controller never invents a model id (named hold)"
)
HOLD_ROUTE_UNAPPROVED = (
    "native-controller refused: the dispatch route is not approved by the "
    "operator route policy; an unverifiable route is never recorded (named hold)"
)
HOLD_GRANT_UNPROVISIONED = (
    "native-controller refused: no bound MCP authority is provisioned to issue "
    "a scoped session grant; the worker port never launches an unprovisioned "
    "child (named hold)"
)
HOLD_IDENTITY_UNVERIFIED = (
    "native-controller refused: the launched child's process identity could not "
    "be captured and verified; an unverified child is never recorded (named hold)"
)
HOLD_ANSWER_UNOBSERVED = (
    "native-controller refused: no answering upstream model was observed for the "
    "launched attempt; the controller never records the model it *requested* as "
    "the model that *answered* (named hold)"
)

#: Named holds for the production reviewer port.  Independence of the review is
#: a trust-table row like any other: an unconfigured, non-independent or
#: unreadable review is refused by name, never substituted by a local verdict.
HOLD_REVIEWER_ROUTE_UNCONFIGURED = (
    "native-controller refused: no reviewer route is configured for the "
    "production reviewer port; the controller never reviews on the worker's "
    "route (named hold)"
)
HOLD_REVIEWER_ROUTE_NOT_INDEPENDENT = (
    "native-controller refused: the reviewer route equals the worker route; the "
    "independent review never runs on the producer's own route (named hold)"
)
HOLD_REVIEWER_ROUTE_UNAPPROVED = (
    "native-controller refused: the reviewer route is not approved by the "
    "operator route policy; an unverifiable reviewer route is never used (named hold)"
)
HOLD_REVIEW_MAILBOX_MISMATCH = (
    "native-controller refused: the reviewer port's configured mailbox is not "
    "the server-owned mailbox the review tool writes to; a review is only ever "
    "read back from the durable record the server wrote (named hold)"
)
HOLD_REVIEW_UNRECORDED = (
    "native-controller refused: no durable review result is present under the "
    "server-issued reviewer principal; the controller never authors its own "
    "verdict or acceptance (named hold)"
)
HOLD_REVIEW_UNVERIFIED = (
    "native-controller refused: the durable review does not match the dispatched "
    "attempt it must decide (wrong run/task/attempt, role or source binding); a "
    "mismatched review is refused, never accepted (named hold)"
)
HOLD_REVIEW_VERDICT_UNOBSERVED = (
    "native-controller refused: the launched review attempt produced no observed "
    "verdict; the controller never invents the reviewer's verdict (named hold)"
)

#: The substring marking a provider-native target that is a fallback pool
#: rather than the requested model.  Derived from the gateway's own alias table
#: (``FAIGATE_MAP`` names exactly one fallback target, ``openrouter-fallback``),
#: so fallback-capability is *observed*, not guessed.
_FALLBACK_TARGET_MARKER = "fallback"

#: Environment variables that carry the operator's *configured* gateway
#: endpoint.  Absent all of them the endpoint is unconfigured -- the controller
#: never assumes a gateway it was not told to use.
_ENV_GATEWAY_BASE_URL = "FAIGATE_BASE_URL"
_ENV_GATEWAY_HOST = "FAIGATE_HOST"
_ENV_GATEWAY_PORT = "FAIGATE_PORT"
_DEFAULT_GATEWAY_HOST = "127.0.0.1"
_DEFAULT_GATEWAY_PORT = 8090


@dataclass(frozen=True)
class GatewayAnswer:
    """The answering upstream the launched attempt *observed*.

    Supplied by the caller's ``answer_reader`` seam, which reads the gateway's
    response for the child that actually ran (the response header naming the
    served model).  Both fields are observations, never caller preferences:
    ``upstream_model`` is the model the endpoint answered with and ``served_by``
    names the serving upstream when the answer carries it.  An empty
    ``upstream_model`` is an *unobserved* answer and is refused by the port --
    echoing the requested model back would launder a request into an answer.
    """

    upstream_model: str = ""
    served_by: str = ""


def _probe_loopback_gateway(host: str, port: int, *, timeout: float = 0.25) -> bool:
    """Return whether the configured gateway accepts a connection right now.

    The only network touch in the port, and it is a *reachability* probe of the
    operator-configured approved gateway -- never a provider request, and never
    performed unless the operator configured an endpoint.  Tests inject their
    own probe so no socket is opened at all.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class _ProductionWorkerPort:
    """The production **worker port** (SW-SCOPE-010 criterion 1).

    A :class:`WorkerAdapter` that launches one owned attempt through the
    *existing* dispatch application inline seam
    (:func:`skillweave.dispatch.application._default_inline_seam`), provisioned
    with a server-issued scoped session grant and verified from the launched
    child's own captured process identity, and that records the answering
    gateway endpoint, route and upstream model in the attempt receipt.

    Every trust-table row is discharged or refused with a *named hold*, never
    with a defaulted value:

    * **gateway endpoint** -- resolved only from operator configuration
      (``FAIGATE_BASE_URL`` or ``FAIGATE_HOST``/``FAIGATE_PORT``); with none
      configured the port refuses (:data:`HOLD_GATEWAY_UNCONFIGURED`) instead of
      assuming the default loopback gateway.  A configured endpoint that is not
      the approved gateway, or that does not accept a connection, refuses
      (:data:`HOLD_GATEWAY_UNCONFIGURED` / :data:`HOLD_GATEWAY_UNREACHABLE`).
    * **route** -- the *operator policy* is the authority
      (:func:`skillweave.routing.route_policy.resolve_route_policy`); an
      unconfigured policy or a route outside the approved set refuses
      (:data:`HOLD_ROUTE_UNAPPROVED`).  The port never defines the policy.
    * **scoped grant** -- the token is minted by
      ``skillweave.mcp.server.issue_harness_session_grant`` against the
      *bound* authority (role ``worker``) over the one durable process service
      ``skillweave.mcp.control.service_for_binding`` resolves; an unbound or
      unprovisioned authority refuses (:data:`HOLD_GRANT_UNPROVISIONED`).  The
      caller never authors a role or a token.
    * **process identity** -- captured from the launched child and *verified*
      against the live process while it is alive (inside the seam's ``started``
      lifecycle observation, the only instant the child is observable); the
      recorded facts are the verified ones (pid + ``start_time`` + command),
      and anything but a declared ``match`` refuses
      (:data:`HOLD_IDENTITY_UNVERIFIED`).  A bare PID is never stored.
    * **fallback-capability** -- observed from the gateway's own alias table
      (a resolved target naming a fallback pool) and from the answer/request
      pair (:func:`skillweave.routing.faigate_adapter.is_substitution`); either
      refuses (:data:`HOLD_GATEWAY_FALLBACK_CAPABLE`), so a masked fallback is a
      named hold and never telemetry.
    * **answering upstream/model** -- read from the ``answer_reader`` seam over
      the child that actually ran; with no observed answer the port refuses
      (:data:`HOLD_ANSWER_UNOBSERVED`) rather than recording the *requested*
      model as an answer.

    The receipt is then persisted through the controller's own store as a
    content-addressed ``ArtifactReceipt`` (run-scoped by ``metadata['run_id']``)
    and read back and re-validated with
    :func:`skillweave.runtime.native_process.validate_attempt_upstream_receipt`,
    so recording is a durable, verifiable act rather than a local return value.
    """

    def __init__(
        self,
        *,
        store: Any,
        command: Sequence[str],
        subject_repo: str,
        subject_commit: str,
        tool: str,
        model: str,
        endpoint: str = "",
        route: str = "",
        client_id: str = "native-controller-worker",
        answer_reader: Optional[Any] = None,
        reachability_probe: Optional[Any] = None,
        authority: Any = None,
        policy_path: str = "",
        db_path: str = "",
        mailbox_path: str = "",
        artifacts_path: str = "",
        stream_root: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._store = store
        self._command = list(command)
        self._subject_repo = subject_repo
        self._subject_commit = subject_commit
        self._tool = tool
        self._model = model
        self._endpoint = endpoint
        self._route = route
        self._client_id = client_id
        self._answer_reader = answer_reader
        self._reachability_probe = reachability_probe
        self._authority = authority
        self._policy_path = policy_path
        self._db_path = db_path
        self._mailbox_path = mailbox_path
        self._artifacts_path = artifacts_path
        self._stream_root = stream_root
        self._timeout = timeout

    # -- refusals ----------------------------------------------------------

    @staticmethod
    def _refuse(hold: str) -> None:
        """Raise the named hold as a fail-closed controller refusal."""
        raise _RefusedError(hold)

    # -- configuration resolution -----------------------------------------

    def _resolve_endpoint(self) -> tuple[str, str, int]:
        """Resolve the approved gateway from *operator configuration* only.

        Returns ``(endpoint, host, port)``.  Unconfigured is a refusal, not a
        default: the controller has no gateway of its own.
        """
        from urllib.parse import urlparse

        raw = self._endpoint or os.environ.get(_ENV_GATEWAY_BASE_URL, "")
        if not raw:
            host = os.environ.get(_ENV_GATEWAY_HOST, "")
            port = os.environ.get(_ENV_GATEWAY_PORT, "")
            if not host and not port:
                self._refuse(HOLD_GATEWAY_UNCONFIGURED)
            raw = f"http://{host or _DEFAULT_GATEWAY_HOST}:{port or _DEFAULT_GATEWAY_PORT}"
        parsed = urlparse(raw)
        if not parsed.hostname:
            self._refuse(HOLD_GATEWAY_UNCONFIGURED)
        try:
            port_int = parsed.port
        except ValueError:
            self._refuse(HOLD_GATEWAY_UNCONFIGURED)
        if port_int is None:
            port_int = 80 if parsed.scheme == "http" else 443
        return raw.rstrip("/"), parsed.hostname, int(port_int)

    def _resolve_route(self, route: str, hold: str = HOLD_ROUTE_UNAPPROVED) -> str:
        """Check the route against the **operator route policy**, or refuse."""
        from skillweave.routing.route_policy import (
            RoutePolicyError,
            resolve_route_policy,
        )

        try:
            policy = resolve_route_policy(self._policy_path)
        except RoutePolicyError:
            self._refuse(hold)
        if policy is None:
            # No operator policy is configured: the approved route set is
            # unknown, so no route is verifiable. Refuse, never assume.
            self._refuse(hold)
        try:
            policy.check_route_allowed(route)
        except RoutePolicyError:
            self._refuse(hold)
        return route

    def _assert_approved_gateway(self, endpoint: str, host: str, port: int) -> None:
        """Refuse an endpoint that is not (or is not reachable as) the gateway."""
        from skillweave.routing.faigate_adapter import check_egress_gateway

        try:
            policy = None
            from skillweave.routing.route_policy import resolve_route_policy

            policy = resolve_route_policy(self._policy_path)
        except Exception:  # noqa: BLE001 - an unreadable policy is not approval
            policy = None
        approved_host = getattr(policy, "approved_gateway_host", _DEFAULT_GATEWAY_HOST)
        approved_port = getattr(policy, "approved_gateway_port", _DEFAULT_GATEWAY_PORT)
        try:
            check_egress_gateway(
                endpoint, approved_host=approved_host, approved_port=approved_port
            )
        except Exception:  # noqa: BLE001 - any egress refusal is a named hold
            self._refuse(HOLD_GATEWAY_UNCONFIGURED)
        probe = self._reachability_probe or _probe_loopback_gateway
        try:
            reachable = bool(probe(approved_host, int(approved_port)))
        except Exception:  # noqa: BLE001 - an unprobeable endpoint is not up
            reachable = False
        if not reachable:
            self._refuse(HOLD_GATEWAY_UNREACHABLE)
        del host, port  # the *approved* host/port are the ones probed

    # -- grant -------------------------------------------------------------

    def _mint_grant(self, *, run_id: str, task_id: str, attempt: int) -> Any:
        """Mint the scoped worker grant over the one durable process service."""
        from skillweave.mcp import control as _control
        from skillweave.mcp import server as _server

        authority = self._authority or _server._bound_authority()
        if authority is None:
            self._refuse(HOLD_GRANT_UNPROVISIONED)
        default_db, default_artifacts = _server.server_default_paths()
        try:
            service = _control.service_for_binding(
                self._db_path or default_db,
                mailbox_path=self._mailbox_path or _control.default_mailbox_path(),
                artifacts_path=self._artifacts_path or default_artifacts,
                stream_root=self._stream_root,
            )
            return _server.issue_harness_session_grant(
                authority=authority,
                role="worker",
                client_id=self._client_id,
                transport="stdio",
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                service=service,
            )
        except _RefusedError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unmintable grant is a hold
            self._refuse(f"{HOLD_GRANT_UNPROVISIONED} [{type(exc).__name__}]")

    # -- launch + identity -------------------------------------------------

    def _launch(
        self, *, run_id: str, task_id: str, attempt: int, model: str, grant: Any
    ) -> tuple[Any, dict]:
        """Launch through the existing inline seam and verify the child.

        The child's identity is captured *and verified against the live
        process* inside the ``started`` lifecycle observation -- the one instant
        the child is observable, because the seam blocks on ``wait()`` after it.
        A refusal is recorded rather than raised from the callback (raising
        there would abandon a started child before the seam reaps it) and is
        converted to a named hold once the seam returns.
        """
        from skillweave.dispatch.application import _default_inline_seam
        from skillweave.runtime import process_identity

        observed: dict[str, Any] = {}

        def _observe(*, phase: str, handle: Any = None, **_kw: Any) -> None:
            if phase != "started" or handle is None:
                return
            pid = getattr(handle, "pid", None)
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                observed["refusal"] = HOLD_IDENTITY_UNVERIFIED
                return
            captured = process_identity.capture(pid)
            if captured is None:
                # Unobservable: unknown, never a fabricated identity.
                observed["refusal"] = HOLD_IDENTITY_UNVERIFIED
                return
            verification = process_identity.verify(captured.to_dict(), pid)
            if not verification.ok:
                observed["refusal"] = HOLD_IDENTITY_UNVERIFIED
                return
            observed["identity"] = captured.to_dict()

        result = _default_inline_seam(
            self._command,
            run_id=run_id,
            subject_repo=self._subject_repo,
            subject_commit=self._subject_commit,
            tool=self._tool,
            model=model,
            timeout=self._timeout,
            on_child_lifecycle=_observe,
            mcp_grant=grant,
        )
        return result, observed

    # -- receipt -----------------------------------------------------------

    def _record_receipt(self, receipt: Any, *, producer_artifact: str) -> Any:
        """Persist the receipt durably, then read it back and re-validate it."""
        from skillweave.runtime.native_process import (
            ATTEMPT_UPSTREAM_SCHEMA_VERSION,
            validate_attempt_upstream_receipt,
        )
        from skillweave.runtime.registry import ArtifactReceipt, EvidenceType

        payload = json.dumps(receipt.to_dict(), sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = (
            f"attempt-upstream-{receipt.run_id}-{receipt.task_id}-{receipt.attempt}"
        )
        saved = self._store.save_evidence(
            ArtifactReceipt(
                artifact_id=artifact_id,
                sha256=digest,
                schema_version=ATTEMPT_UPSTREAM_SCHEMA_VERSION,
                producer_command="native-controller worker-port",
                subject_repo=self._subject_repo,
                subject_commit=self._subject_commit,
                created_at=receipt.recorded_at,
                evidence_type=EvidenceType.OBSERVATION.value,
                purpose="attempt-upstream-receipt",
                metadata={
                    "run_id": receipt.run_id,
                    "task_id": receipt.task_id,
                    "attempt": receipt.attempt,
                    "producer_artifact": producer_artifact,
                    "answering_identity": receipt.answering_identity,
                    "receipt": receipt.to_dict(),
                },
            )
        )
        # Read back from the durable record and re-validate: the recorded bytes
        # must reconstruct the frozen, fail-closed record -- a tampered or
        # unavailable receipt closes the path instead of being trusted.
        if saved is None:
            self._refuse(HOLD_ANSWER_UNOBSERVED)
        validate_attempt_upstream_receipt(saved.metadata.get("receipt") or {})
        return saved

    # -- the adapter seam --------------------------------------------------

    def execute(self, dispatch: Any) -> AttemptExecution:
        """Launch the dispatched owned attempt and record its upstream facts."""
        from skillweave.runtime.native_process import AttemptUpstreamReceipt
        from skillweave.routing.faigate_adapter import is_substitution, translate_model_id

        run_id = str(getattr(dispatch, "run_id", "") or "")
        task_id = str(getattr(dispatch, "task_id", "") or "")
        attempt = getattr(dispatch, "attempt", 0)
        if not run_id or not task_id:
            raise _UsageError("the dispatched owned attempt names no run/task")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise _UsageError("the dispatched owned attempt has no 1-based attempt")
        if not self._model:
            self._refuse(HOLD_MODEL_UNCONFIGURED)

        # Fallback-capability is observed from the gateway's own alias table
        # before anything is launched: a request that resolves to a fallback
        # pool is a named hold, not a launch.
        try:
            resolved = translate_model_id(self._model)
        except Exception:  # noqa: BLE001 - an untranslatable id is not a route
            self._refuse(HOLD_MODEL_UNCONFIGURED)
        if _FALLBACK_TARGET_MARKER in resolved:
            self._refuse(HOLD_GATEWAY_FALLBACK_CAPABLE)

        endpoint, host, port = self._resolve_endpoint()
        self._assert_approved_gateway(endpoint, host, port)
        route = self._resolve_route(
            str(getattr(dispatch, "route", "") or "") or self._route
        )

        grant = self._mint_grant(run_id=run_id, task_id=task_id, attempt=attempt)
        result, observed = self._launch(
            run_id=run_id, task_id=task_id, attempt=attempt, model=self._model,
            grant=grant,
        )
        if "refusal" in observed:
            self._refuse(observed["refusal"])
        identity = observed.get("identity")
        if not identity:
            self._refuse(HOLD_IDENTITY_UNVERIFIED)

        children = list(getattr(result, "children", ()) or ())
        if len(children) != 1:
            self._refuse(HOLD_ANSWER_UNOBSERVED)
        child = children[0]
        raw = getattr(child, "raw_bytes", b"") or b""
        producer_artifact = f"sha256:{hashlib.sha256(raw).hexdigest()}"

        answer = None
        if self._answer_reader is not None:
            answer = self._answer_reader(dispatch, child)
        upstream_model = str(getattr(answer, "upstream_model", "") or "")
        served_by = str(getattr(answer, "served_by", "") or "")
        if not upstream_model:
            self._refuse(HOLD_ANSWER_UNOBSERVED)
        if _FALLBACK_TARGET_MARKER in upstream_model or is_substitution(
            self._model, served_by=served_by or upstream_model
        ):
            # A masked fallback is a named hold at the port; it is never
            # recorded as a receipt (the record refuses it by construction).
            self._refuse(HOLD_GATEWAY_FALLBACK_CAPABLE)

        receipt = AttemptUpstreamReceipt(
            run_id=run_id,
            gateway_endpoint=endpoint,
            route=route,
            upstream_model=upstream_model,
            requested_model=self._model,
            served_by=served_by,
            task_id=task_id,
            attempt=attempt,
            endpoint_verified=True,
            process_identity=identity,
        )
        self._record_receipt(receipt, producer_artifact=producer_artifact)
        return AttemptExecution(producer_artifact=producer_artifact)


class _ProductionReviewerPort:
    """The production **reviewer port** (SW-SCOPE-010 criterion 2).

    A :class:`ReviewerAdapter` whose verdict is the one the **review tool**
    (``skillweave.mcp.server.review_action``) wrote into the durable mailbox
    under a **server-issued reviewer principal** -- read back by the controller
    from that durable record, never synthesised here.

    Every trust-table row is discharged or refused with a *named hold*:

    * **independent reviewer route** -- the reviewer route is derived from
      operator configuration (``SKILLWEAVE_REVIEWER_ROUTE``, falling back to the
      dispatch route) and checked against the *operator route policy*; a route
      that equals the worker's route refuses (:data:`HOLD_REVIEWER_ROUTE_NOT_INDEPENDENT`)
      -- a review on the producer's own route is not an independent review -- and
      an unapproved route refuses (:data:`HOLD_REVIEWER_ROUTE_UNAPPROVED`).
    * **server-issued principal** -- the durable record is only trusted when its
      recorded ``principal`` has the server-issued shape
      ``mcp:<role>:<client_id>`` with the ``reviewer`` role
      (:meth:`ResolvedReview.from_record`); a caller-declared or worker principal
      is refused, so a producer can never resolve as its own reviewer.
    * **durable read-back** -- the verdict, subject revision, artifact digest and
      criteria are loaded from the mailbox record the server wrote
      (:class:`DurableReviewRef.resolve`), so the controller never invents a
      verdict; an absent or tampered record refuses
      (:data:`HOLD_REVIEW_UNRECORDED`).
    * **scope agreement** -- the durable record must name the exact
      ``run_id``/``task_id``/``attempt`` of the dispatch and the ``reviewer``
      role, or the port refuses (:data:`HOLD_REVIEW_UNVERIFIED`).

    Independence is structural, never a value this port can supply: it mints no
    principal, writes no record and reads the review only from the durable store.
    """

    def __init__(
        self,
        *,
        store: Any,
        subject_repo: str = "",
        subject_commit: str = "",
        worker_route: str = "",
        reviewer_route: str = "",
        worker_client_id: str = "native-controller-worker",
        client_id: str = "native-controller-reviewer",
        authority: Any = None,
        policy_path: str = "",
        db_path: str = "",
        mailbox_path: str = "",
        artifacts_path: str = "",
        stream_root: Optional[str] = None,
        review_writer: Optional[Any] = None,
    ) -> None:
        self._store = store
        self._subject_repo = subject_repo
        self._subject_commit = subject_commit
        self._worker_route = worker_route
        self._reviewer_route = reviewer_route
        self._worker_client_id = worker_client_id
        self._client_id = client_id
        self._authority = authority
        self._policy_path = policy_path
        self._db_path = db_path
        self._mailbox_path = mailbox_path
        self._artifacts_path = artifacts_path
        self._stream_root = stream_root
        #: The review *tool* seam.  Production is the MCP ``review_action``
        #: server function; a test injects a fake that writes a durable record
        #: (or an adversarial fake that forges one) without any provider call.
        self._review_writer = review_writer

    # -- refusals ----------------------------------------------------------

    @staticmethod
    def _refuse(hold: str) -> None:
        raise _RefusedError(hold)

    # -- route independence ------------------------------------------------

    def _resolve_reviewer_route(self, dispatch_route: str) -> str:
        """Resolve the reviewer route and prove it is independent, or refuse.

        The reviewer route comes from operator configuration
        (``SKILLWEAVE_REVIEWER_ROUTE``), never from this port's own default.  A
        reviewer route that is absent, that equals the worker route, or that the
        operator policy does not approve is refused by name -- the controller
        never reviews an attempt on the route that produced it.
        """
        from skillweave.routing.route_policy import (
            RoutePolicyError,
            resolve_route_policy,
        )

        route = (
            self._reviewer_route
            or os.environ.get(_ENV_REVIEWER_ROUTE, "")
            or dispatch_route
        ).strip()
        if not route:
            self._refuse(HOLD_REVIEWER_ROUTE_UNCONFIGURED)
        worker_route = (self._worker_route or dispatch_route).strip()
        if route == worker_route:
            # The independent review never runs on the producer's own route.
            self._refuse(HOLD_REVIEWER_ROUTE_NOT_INDEPENDENT)
        try:
            policy = resolve_route_policy(self._policy_path)
        except RoutePolicyError:
            self._refuse(HOLD_REVIEWER_ROUTE_UNAPPROVED)
        if policy is None:
            self._refuse(HOLD_REVIEWER_ROUTE_UNAPPROVED)
        try:
            policy.check_route_allowed(route)
        except RoutePolicyError:
            self._refuse(HOLD_REVIEWER_ROUTE_UNAPPROVED)
        return route

    # -- the durable review record ----------------------------------------

    def _server_mailbox_path(self) -> str:
        """The server-owned mailbox the review tool writes to, or refuse.

        The path is server-owned (``SKILLWEAVE_AGENT_MAILBOX`` else the server
        default).  A port configured with a *different* mailbox than the review
        tool writes to would read back nothing real, so the mismatch is refused
        rather than silently reading an empty/foreign mailbox.
        """
        from skillweave.mcp import server as _server

        server_path = _server.default_mailbox_path()
        if self._mailbox_path and self._mailbox_path != server_path:
            self._refuse(HOLD_REVIEW_MAILBOX_MISMATCH)
        return server_path

    def _mint_reviewer_grant(self, *, run_id: str, task_id: str, attempt: int) -> Any:
        """Mint the scoped **reviewer** grant over the one durable process service."""
        from skillweave.mcp import control as _control
        from skillweave.mcp import server as _server
        from skillweave.runtime.native_process import PUBLICATION_REVIEW_ROLE

        authority = self._authority or _server._bound_authority()
        if authority is None:
            self._refuse(HOLD_GRANT_UNPROVISIONED)
        default_db, default_artifacts = _server.server_default_paths()
        try:
            service = _control.service_for_binding(
                self._db_path or default_db,
                mailbox_path=self._mailbox_path or _control.default_mailbox_path(),
                artifacts_path=self._artifacts_path or default_artifacts,
                stream_root=self._stream_root,
            )
            return _server.issue_harness_session_grant(
                authority=authority,
                role=PUBLICATION_REVIEW_ROLE,
                client_id=self._client_id,
                transport="stdio",
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                service=service,
            )
        except _RefusedError:
            raise
        except Exception as exc:  # noqa: BLE001 - an unmintable grant is a hold
            self._refuse(f"{HOLD_GRANT_UNPROVISIONED} [{type(exc).__name__}]")

    def review(self, dispatch: Any, execution: AttemptExecution) -> ReviewVerdict:
        """Run an independent review attempt and read its durable verdict back."""
        from skillweave.mcp import server as _server

        run_id = str(getattr(dispatch, "run_id", "") or "")
        task_id = str(getattr(dispatch, "task_id", "") or "")
        attempt = getattr(dispatch, "attempt", 0)
        if not run_id or not task_id:
            raise _UsageError("the dispatched owned attempt names no run/task")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise _UsageError("the dispatched owned attempt has no 1-based attempt")

        dispatch_route = str(getattr(dispatch, "route", "") or "")
        route = self._resolve_reviewer_route(dispatch_route)
        mailbox_path = self._server_mailbox_path()

        # Mint the server-issued *reviewer* session (role ``reviewer``): the
        # durable review is only ever recorded under this principal, so a worker
        # or caller-declared principal can never resolve as the reviewer.
        grant = self._mint_reviewer_grant(
            run_id=run_id, task_id=task_id, attempt=attempt
        )
        token = str(getattr(grant, "session_token", "") or "")
        if not token:
            self._refuse(HOLD_GRANT_UNPROVISIONED)

        # The review *tool* runs the review attempt and writes its result
        # durably under the server-issued reviewer principal.  Production: the
        # MCP ``review_action`` tool.  A test injects a writer; the writer never
        # receives a provider or gateway call from this port.
        writer = self._review_writer or _server.review_action
        from skillweave.runtime.native_process import (
            STEP_INDEPENDENT_REVIEW,
            STEP_RE_REVIEW,
        )

        criteria = ",".join((STEP_INDEPENDENT_REVIEW, STEP_RE_REVIEW))
        result = writer(
            action="write",
            recipient=self._client_id,
            body=f"{task_id}/attempt-{attempt}",
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            verdict=REVIEW_PASS,
            subject_revision=execution.producer_artifact,
            artifact_digest=execution.producer_artifact,
            criteria=criteria,
            session_token=token,
        )
        written = json.loads(result) if isinstance(result, str) else result
        if not isinstance(written, Mapping) or written.get("status") != "ok":
            self._refuse(
                f"{HOLD_REVIEW_UNRECORDED} [{_refusal_code(written)}]"
            )
        message = written.get("message")
        if not isinstance(message, Mapping) or not message.get("message_id"):
            self._refuse(HOLD_REVIEW_UNRECORDED)

        # Read the review *back from the durable record*.  Its principal, role,
        # verdict and source binding are loaded from the mailbox, never taken
        # from the writer's return value.
        from skillweave.runtime.native_process import DurableReviewRef

        ref = DurableReviewRef(
            mailbox_path=mailbox_path, message_id=str(message["message_id"])
        )
        try:
            resolved = ref.resolve()
        except Exception as exc:  # noqa: BLE001 - an unreadable review is a hold
            self._refuse(f"{HOLD_REVIEW_UNRECORDED} [{type(exc).__name__}]")

        self._assert_review_scope(
            resolved, run_id=run_id, task_id=task_id, attempt=attempt
        )
        verdict = str(resolved.verdict or "").strip().lower()
        if verdict not in (REVIEW_PASS, REVIEW_FAIL):
            self._refuse(
                f"{HOLD_REVIEW_VERDICT_UNOBSERVED}: recorded verdict {resolved.verdict!r}"
            )

        evidence = (
            {
                "schema_version": _MEMBER_EVIDENCE_SCHEMA,
                "evidence_type": "member",
                # The *server-issued* reviewer principal is the answering
                # identity -- never a caller string, and never the producer.
                "verifier_identity": resolved.principal,
                "verdict": verdict.upper(),
                "subject_revision": resolved.subject_revision,
                "artifact_digest": resolved.artifact_digest,
            },
        )
        return ReviewVerdict(
            verdict=verdict,
            evidence=evidence,
            verifier_identity=resolved.principal,
            published=True,
        )

    def _assert_review_scope(
        self, resolved: Any, *, run_id: str, task_id: str, attempt: int
    ) -> None:
        """Refuse a durable review that does not decide this exact attempt."""
        from skillweave.runtime.native_process import PUBLICATION_REVIEW_ROLE

        if str(resolved.role) != PUBLICATION_REVIEW_ROLE:
            self._refuse(
                f"{HOLD_REVIEW_UNVERIFIED}: durable review role "
                f"{resolved.role!r} is not {PUBLICATION_REVIEW_ROLE!r}"
            )
        if (
            str(resolved.run_id) != run_id
            or str(resolved.task_id) != task_id
            or int(resolved.attempt) != int(attempt)
        ):
            self._refuse(
                f"{HOLD_REVIEW_UNVERIFIED}: durable review answers "
                f"{resolved.run_id}/{resolved.task_id}/attempt-{resolved.attempt}, "
                f"not {run_id}/{task_id}/attempt-{attempt}"
            )


def _refusal_code(written: Any) -> str:
    """The refusal code from a review-tool result, for a named hold."""
    if isinstance(written, Mapping):
        return str(written.get("code") or written.get("status") or "unrecorded")
    return "unrecorded"


#: Overridable environment variable naming the operator's independent reviewer
#: route.  Absent it (and with no route configured) the reviewer route is
#: unconfigured and the port refuses rather than reviewing on the worker route.
_ENV_REVIEWER_ROUTE = "SKILLWEAVE_REVIEWER_ROUTE"

#: The member-evidence schema the controller's gate evaluator accepts.
_MEMBER_EVIDENCE_SCHEMA = "independent-gate-receipt/1.0.0"


def build_production_reviewer_port(
    *,
    store: Any,
    subject_repo: str = "",
    subject_commit: str = "",
    worker_route: str = "",
    reviewer_route: str = "",
    worker_client_id: str = "native-controller-worker",
    client_id: str = "native-controller-reviewer",
    authority: Any = None,
    policy_path: str = "",
    db_path: str = "",
    mailbox_path: str = "",
    artifacts_path: str = "",
    stream_root: Optional[str] = None,
    review_writer: Optional[Any] = None,
) -> ReviewerAdapter:
    """Build the production **reviewer port** for the ``native-controller``.

    Returns a :class:`ReviewerAdapter` whose verdict is read back from the
    durable review record the review tool wrote under a server-issued reviewer
    principal (criterion 2).  ``worker_route`` is the route the producer's
    attempt ran on: a reviewer route equal to it refuses
    (:data:`HOLD_REVIEWER_ROUTE_NOT_INDEPENDENT`).  No provider or gateway call
    is made here -- the review tool is the only reviewer, and ``review_writer``
    is the seam a test replaces.
    """
    return _ProductionReviewerPort(
        store=store,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        worker_route=worker_route,
        reviewer_route=reviewer_route,
        worker_client_id=worker_client_id,
        client_id=client_id,
        authority=authority,
        policy_path=policy_path,
        db_path=db_path,
        mailbox_path=mailbox_path,
        artifacts_path=artifacts_path,
        stream_root=stream_root,
        review_writer=review_writer,
    )


def build_production_ports(
    *,
    store: Any,
    command: Sequence[str],
    subject_repo: str,
    subject_commit: str,
    tool: str,
    model: str,
    endpoint: str = "",
    route: str = "",
    answer_reader: Optional[Any] = None,
    reachability_probe: Optional[Any] = None,
    authority: Any = None,
    policy_path: str = "",
    db_path: str = "",
    mailbox_path: str = "",
    artifacts_path: str = "",
    stream_root: Optional[str] = None,
    timeout: Optional[float] = None,
    client_id: str = "native-controller-worker",
) -> WorkerAdapter:
    """Build the production worker port for the ``native-controller`` entry point.

    Returns the **worker port** only -- the reviewer port is built by the
    separate :func:`build_production_reviewer_port` factory (criterion 2), so a
    caller can never receive a reviewer this factory did not provision.
    ``store`` is the controller's own durable store: it is the store the
    attempt's upstream receipt is recorded into, so the caller passes the *same*
    store to :func:`main` (``store=``) rather than letting the entry build a
    second one.
    """
    return _ProductionWorkerPort(
        store=store,
        command=command,
        subject_repo=subject_repo,
        subject_commit=subject_commit,
        tool=tool,
        model=model,
        endpoint=endpoint,
        route=route,
        answer_reader=answer_reader,
        reachability_probe=reachability_probe,
        authority=authority,
        policy_path=policy_path,
        db_path=db_path,
        mailbox_path=mailbox_path,
        artifacts_path=artifacts_path,
        stream_root=stream_root,
        timeout=timeout,
        client_id=client_id,
    )


@dataclass(frozen=True)
class AttemptExecution:
    """What driving one owned attempt produced, supplied by the worker adapter.

    The adapter is the launch seam: in production this is where an attempt would
    run.  The record the *controller* consumes is only these facts -- the
    produced artifact (mandatory before acceptance) and, for a non-repository
    subject, the controlled content-transaction inputs.
    """

    producer_artifact: str = ""
    #: A non-repository subject to integrate through a controlled content
    #: transaction ("controlled integration" for a CMS/configuration/... kind).
    #: ``None`` selects the repository (Git merge) integration record.
    integration_subject: Any = None
    baseline_digest: str = ""
    applied_digest: str = ""


@dataclass(frozen=True)
class ReviewVerdict:
    """The independent review the controller consumes through the reviewer port.

    ``verdict`` is one of ``"pass"``/``"fail"`` and is the *independent
    verifier's* decision.  ``evidence`` is the machine-readable independent
    verifier evidence (``independent-gate-receipt/1.0.0`` member records) the
    controller feeds to :func:`skillweave.trace.gate_receipt.evaluate_gate`.
    Acceptance is derived from that evaluation -- the adapter never hands the
    controller an ``accepted`` flag.  ``verifier_identity`` is who actually
    answered the review; it must differ from the producer identity.
    """

    verdict: str
    evidence: tuple[Any, ...] = ()
    verifier_identity: str = ""
    published: bool = False


@dataclass(frozen=True)
class AttemptAcceptance:
    """The attempt's acceptance facts, built inside the controller.

    This is not a caller-supplied acceptance: ``state`` is always the terminal
    accepted state and is only ever assembled after independent evidence has
    decided ``PASS``.  It exists so the frozen
    :func:`skillweave.trace.gate_receipt.release_dependents` rule has an
    authoritative ``AttemptAcceptance``-shaped record to consume.
    """

    attempt_id: str
    state: str
    producer_artifact: str
    dependents: tuple[str, ...] = ()


@runtime_checkable
class WorkerAdapter(Protocol):
    """The single launch seam for an owned attempt.

    ``execute`` is handed the boundary ``owned-attempt-dispatch-v1`` record the
    service produced and returns what the attempt yielded.  A production adapter
    would launch a worker here; a test adapter returns a synthetic record.  This
    is the *only* seam that may launch anything -- the controller loop itself
    calls only existing service methods.
    """

    def execute(self, dispatch: Any) -> AttemptExecution:  # pragma: no cover
        ...


@runtime_checkable
class ReviewerAdapter(Protocol):
    """The independent review seam for an owned attempt.

    ``review`` is handed the boundary dispatch record and the attempt execution
    and returns the :class:`ReviewVerdict` (verdict + independent verifier
    evidence).  A production adapter would consult the durable review record the
    review tool wrote; a test adapter returns a synthetic verdict.  The adapter
    supplies *evidence*, never an acceptance -- the controller derives acceptance
    from the evidence through the gate-receipt evaluator.
    """

    def review(
        self, dispatch: Any, execution: AttemptExecution
    ) -> ReviewVerdict:  # pragma: no cover
        ...


@dataclass
class DriveResult:
    """The outcome of one controller drive, as emitted on stdout."""

    run_id: str
    task_id: str
    attempt: int
    intent_id: str
    fencing_token: int
    steps: tuple[str, ...]
    accepted: bool
    released_dependents: tuple[str, ...]
    integration: str
    reviews: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "intent_id": self.intent_id,
            "fencing_token": self.fencing_token,
            "steps": list(self.steps),
            "accepted": self.accepted,
            "released_dependents": list(self.released_dependents),
            "integration": self.integration,
            "reviews": list(self.reviews),
        }


def build_parser(prog: str = "skillweave native-controller") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Drive the native process service from durable state: acquire the "
            "single controller lease and run one owned attempt through ready "
            "dispatch, a verdict-driven review/repair/re-review, controlled "
            "integration and the dependency-graph dependent wave. Requires the "
            "worker and reviewer production adapters; refuses fail-closed "
            "without them. Additive and opt-in."
        ),
    )
    parser.add_argument(
        "--run-id",
        dest="run_id",
        required=True,
        help="The run this controller drives (required).",
    )
    parser.add_argument(
        "--task-id",
        dest="task_id",
        required=True,
        help="The task (owned attempt subject) this controller drives (required).",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Attempt number for the run/task/attempt triple (default: 1).",
    )
    parser.add_argument(
        "--db-path",
        dest="db_path",
        default=":memory:",
        help="Path to the durable SQLite store the run command also uses "
             "(default: :memory:).",
    )
    parser.add_argument(
        "--holder",
        default="native-controller",
        help="The controller identity recorded on the lease (default: "
             "native-controller).",
    )
    parser.add_argument(
        "--ttl-seconds",
        dest="ttl_seconds",
        type=int,
        default=300,
        help="Controller lease TTL in seconds (default: 300).",
    )
    parser.add_argument(
        "--sequence",
        dest="sequence",
        default="",
        help="Optional path to a dispatch sequence declaration (JSON) declaring "
             "the dependency graph; the dependent wave is computed from it. "
             "Without it the wave is empty (no dependent is invented).",
    )
    parser.add_argument(
        "--route",
        default="",
        help="Optional route label recorded on the dispatch intent.",
    )
    parser.add_argument(
        "--session-id",
        dest="session_id",
        default="",
        help="Optional session id recorded on the dispatch intent.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the drive result as a single JSON envelope (default).",
    )
    parser.add_argument(
        "--qualify-cms-path",
        dest="qualify_cms_path",
        default="",
        help="Qualify one declared CMS path (SW-1511-CMS-FLOWS) against the "
             "durable store: drive its reversible lifecycle through the native "
             "controller seam and emit the fail-closed cms-process-qualification "
             "record. Additive and opt-in; no worker/reviewer adapter and no "
             "provider, gateway or CMS call is made.",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    worker: Optional[WorkerAdapter] = None,
    reviewer: Optional[ReviewerAdapter] = None,
    store: Any = None,
) -> int:
    """Route ``native-controller`` and return its exit code.

    ``worker``, ``reviewer`` and ``store`` are injected ports: a test supplies a
    fake worker and reviewer (that launch nothing and call no provider) and/or a
    store, instead of letting the entry build its own.  Production callers pass
    ``worker`` and ``reviewer`` from their adapter registration; when either is
    absent the drive is refused fail-closed (a named unsupported hold).
    """
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse already wrote usage (code 2) or help (code 0); map both to
        # the documented codes rather than letting the raise escape the entry.
        code = exc.code if isinstance(exc.code, int) else EXIT_INVALID_INPUT
        return EXIT_OK if code == 0 else EXIT_INVALID_INPUT

    try:
        if args.attempt < 1:
            raise _UsageError("--attempt must be a positive integer")
    except _UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_INVALID_INPUT

    # Additive, opt-in CMS path qualification (SW-1511-CMS-FLOWS): this branch
    # drives the reversible lifecycle of one declared CMS path through the
    # native controller seam and needs no worker/reviewer adapter -- and makes no
    # provider, gateway or CMS call. It is checked before the worker/reviewer
    # fail-closed holds so the two surfaces never interfere.
    if args.qualify_cms_path:
        try:
            result = _qualify_cms_path(
                path_id=args.qualify_cms_path,
                run_id=args.run_id,
                task_id=args.task_id,
                attempt=args.attempt,
                db_path=args.db_path,
                holder=args.holder,
                ttl_seconds=args.ttl_seconds,
                store=store,
            )
        except _UsageError as exc:
            sys.stderr.write(f"error: {exc}\n")
            return EXIT_INVALID_INPUT
        except _RefusedError as exc:
            sys.stderr.write(f"refused: {exc}\n")
            return EXIT_REFUSED
        except KeyboardInterrupt:
            sys.stderr.write("interrupted\n")
            return EXIT_FAILED
        except Exception as exc:  # noqa: BLE001 -- a crash is exit 5, never a verdict
            sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
            return EXIT_FAILED
        sys.stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
        return EXIT_OK

    # Fail closed with a *named* unsupported hold before touching the store:
    # with no production adapter configured the controller substitutes nothing.
    if worker is None:
        sys.stderr.write(f"refused: {HOLD_NO_WORKER}\n")
        return EXIT_REFUSED
    if reviewer is None:
        sys.stderr.write(f"refused: {HOLD_NO_REVIEWER}\n")
        return EXIT_REFUSED

    try:
        result = _drive(
            run_id=args.run_id,
            task_id=args.task_id,
            attempt=args.attempt,
            db_path=args.db_path,
            holder=args.holder,
            ttl_seconds=args.ttl_seconds,
            route=args.route,
            session_id=args.session_id,
            sequence=args.sequence,
            worker=worker,
            reviewer=reviewer,
            store=store,
        )
    except _UsageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_INVALID_INPUT
    except _RefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 -- a crash is exit 5, never a verdict
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return EXIT_FAILED

    sys.stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    return EXIT_OK


class _UsageError(Exception):
    """Caller input cannot form a controller drive. Maps to exit 2."""


class _RefusedError(Exception):
    """The service refused the drive. Maps to exit 3 (fail closed)."""


class _LeaseGuard:
    """Context manager making the controller lease **signal-safe**.

    On ``__enter__`` it acquires the single fenced lease (refusing a live
    holder) and installs ``SIGTERM``/``SIGINT`` handlers that release the lease
    and then re-raise so the process exits with the conventional signal status.
    On ``__exit__`` -- success, exception or ``KeyboardInterrupt`` -- it always
    releases the lease, so no exit path leaves a live holder that would refuse
    the next controller.
    """

    def __init__(self, svc: Any, *, ttl_seconds: int) -> None:
        self._svc = svc
        self._ttl = ttl_seconds
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> "_LeaseGuard":
        self._svc.acquire(ttl_seconds=self._ttl)

        def _handler(signum: int, _frame: Any) -> None:  # pragma: no cover
            # Release before dying so the lease is never left live on a signal.
            try:
                if self._svc.fencing_token is not None:
                    self._svc.release()
            finally:
                previous = self._previous.get(signum, signal.SIG_DFL)
                signal.signal(signum, previous)
                signal.raise_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()
        try:
            if self._svc.fencing_token is not None:
                self._svc.release()
        except Exception:  # noqa: BLE001 -- never mask the in-flight outcome
            if exc_type is None:
                raise
        return False


def _qualify_cms_path(
    *,
    path_id: str,
    run_id: str,
    task_id: str,
    attempt: int,
    db_path: str,
    holder: str,
    ttl_seconds: int,
    store: Any,
) -> Any:
    """Qualify one declared CMS path through the native controller seam.

    This is the production *consumer* of the declared CMS paths
    (SW-1511-CMS-FLOWS): the frozen declaration module shipped with no caller, so
    this branch makes the flow reachable from the command line. It composes the
    service on the same durable store the ``run`` command uses and delegates to
    :func:`skillweave.runtime.cms_flow_qualification.qualify_cms_path`. No worker
    or reviewer adapter is involved and no provider/gateway/CMS call is made.
    """
    from skillweave.runtime.cms_flow_qualification import (
        CmsFlowQualificationRefused,
        qualify_cms_path,
    )
    from skillweave.runtime.store import SQLiteRunStore

    owns_store = store is None
    if owns_store:
        store = SQLiteRunStore(db_path)
    try:
        try:
            return qualify_cms_path(
                store,
                path_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                holder=holder,
                ttl_seconds=ttl_seconds,
            )
        except CmsFlowQualificationRefused as exc:
            raise _UsageError(str(exc)) from exc
    finally:
        if owns_store:
            store.close()


def _drive(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    db_path: str,
    holder: str,
    ttl_seconds: int,
    route: str,
    session_id: str,
    sequence: str,
    worker: WorkerAdapter,
    reviewer: ReviewerAdapter,
    store: Any,
) -> DriveResult:
    """Compose the service, acquire the lease, drive the loop from state.

    The loop is driven by the *recorded* review verdict, not a fixed script: a
    passing review integrates; a failing review repairs then re-reviews; a
    failing re-review stops the attempt terminally.  The lease is signal-safe
    and released on every exit path.
    """
    # Lazy imports keep the CLI module's import closure light and mirror the
    # composition the ``run`` command uses (a durable SQLiteRunStore). This is
    # the production construction of NativeProcessService that SW-SCOPE-010
    # measures as missing on the base.
    from skillweave.runtime.native_process import (
        LeaseRefusedError,
        NativeProcessService,
        STEP_CONTROLLED_INTEGRATION,
        STEP_DEPENDENT_WAVE,
        STEP_INDEPENDENT_REVIEW,
        STEP_READY_DISPATCH,
        STEP_REPAIR,
        STEP_RE_REVIEW,
    )
    from skillweave.runtime.store import SQLiteRunStore
    from skillweave.trace import gate_receipt as gr

    owns_store = store is None
    if owns_store:
        store = SQLiteRunStore(db_path)

    svc = NativeProcessService(store, holder)
    try:
        try:
            with _LeaseGuard(svc, ttl_seconds=ttl_seconds) as lease:
                return _run_state_driven(
                    svc,
                    lease,
                    run_id=run_id,
                    task_id=task_id,
                    attempt=attempt,
                    route=route,
                    session_id=session_id,
                    sequence=sequence,
                    holder=holder,
                    worker=worker,
                    reviewer=reviewer,
                    store=store,
                    gr=gr,
                )
        except LeaseRefusedError as exc:
            # A live controller holds the lease: refuse, never steal (fail
            # closed). This is the second-controller refusal.
            raise _RefusedError(str(exc)) from exc
    finally:
        if owns_store:
            store.close()


def _persist_transition_checkpoint(svc: Any, *, step: str, intent_id: str) -> None:
    """Persist the run-keyed transition checkpoint (SW-SCOPE-010 S10-B c1).

    The controller is *state-driven*: every step it takes is a durable
    transition on the intent it owns.  This makes the transition log a
    **written** durable statement too, so a recovering controller reads the
    driven state from a ``checkpoints`` row keyed by the run instead of
    replaying the loop from scratch.

    Nothing here is caller-supplied.  The run key, the in-flight set and the
    journal offset are all read from the store's durable records inside
    :meth:`NativeProcessService.persist_transition_checkpoint`; ``step`` names
    *which* transition just happened and ``intent_id`` the durable intent it was
    driven on, and both are verified against that durable intent -- neither
    enters the checkpoint.  A transition that did not persist its checkpoint is
    a fail-closed refusal rather than a silent skip.
    """
    if svc.persist_transition_checkpoint(step, intent_id) is None:
        raise _RefusedError(
            f"{step}: the controller transition did not persist its run-keyed "
            f"checkpoint (fail-closed)"
        )


def _require_recorded_verdict(store: Any, run_id: str, *, expected: str) -> str:
    """Read the *durable* review verdict the MCP review tool recorded.

    The verdict is the value in the ``transitions_log`` row the review tool
    wrote (``to_state='advance_or_stop'``, ``reason='Review verdict {verdict}:
    ...'``).  It is never supplied by this controller.  ``expected`` names the
    step whose verdict is being read, so a missing or non-review record is a
    refusal naming the cause rather than a defaulted value.
    """
    record = store.latest_run_transition(run_id)
    if not record or record.get("to_state") != "advance_or_stop":
        raise _RefusedError(
            f"{expected}: no durable review record for run {run_id!r}; the "
            f"controller reads its verdict from the recorded review, never a "
            f"value it makes up"
        )
    reason = str(record.get("reason") or "")
    prefix = "Review verdict "
    if not reason.startswith(prefix) or ":" not in reason:
        raise _RefusedError(
            f"{expected}: durable record for run {run_id!r} is not a review "
            f"verdict record (reason={reason!r})"
        )
    verdict = reason[len(prefix):].split(":", 1)[0].strip().lower()
    if verdict not in (REVIEW_PASS, REVIEW_FAIL):
        raise _RefusedError(
            f"{expected}: recorded review verdict {verdict!r} for run "
            f"{run_id!r} is not one of {[REVIEW_PASS, REVIEW_FAIL]}"
        )
    return verdict


def _independent_receipt(
    gr: Any,
    *,
    execution: AttemptExecution,
    attempt_id: str,
    verdict: ReviewVerdict,
    producer_identity: str,
) -> tuple[Any, Any]:
    """Derive acceptance from *independent verifier evidence*, fail-closed.

    Returns ``(decision, binding)``.  The verdict the reviewer port reports and
    the verdict the evidence decides must agree -- a reviewer that reports
    ``pass`` while offering no independent evidence (or evidence that decides
    otherwise) is refused.  An empty producer artifact is refused too: a
    launched-but-artifact-less attempt is never accepted.
    """
    from skillweave.runtime.native_process import STEP_INDEPENDENT_REVIEW, STEP_RE_REVIEW

    binding = gr.ReviewBinding(
        review_id=f"{attempt_id}/review",
        review_criteria=(STEP_INDEPENDENT_REVIEW, STEP_RE_REVIEW),
        subject_revision=execution.producer_artifact or attempt_id,
        artifact_digest=execution.producer_artifact,
    )
    acceptance = gr.AttemptAcceptance(
        attempt_id=attempt_id,
        state=gr.ACCEPTANCE_REVIEWED,
        producer_artifact=execution.producer_artifact,
    )
    decision = gr.evaluate_gate(
        acceptance,
        evidence=tuple(verdict.evidence),
        producer_identity=producer_identity,
    )
    reported = REVIEW_PASS if verdict.verdict.strip().lower() == REVIEW_PASS else REVIEW_FAIL
    independent_pass = decision.verdict == gr.GATE_PASS and decision.accepted
    if reported == REVIEW_PASS and not independent_pass:
        reasons = '; '.join(decision.reasons) or 'no independent verifier evidence'
        raise _RefusedError(
            f"reviewer reported pass but independent evidence did not decide a "
            f"PASS ({decision.verdict}: {reasons})"
        )
    if reported == REVIEW_FAIL and decision.verdict not in (gr.GATE_FAIL, gr.GATE_UNDECIDED):
        raise _RefusedError(
            f"reviewer reported fail but independent evidence decided "
            f"{decision.verdict}"
        )
    return decision, binding


def _accepted_receipt(
    svc: Any,
    gr: Any,
    *,
    execution: AttemptExecution,
    acceptance: Any,
    binding: Any,
    verdict: ReviewVerdict,
    producer_identity: str,
) -> Any:
    """Record the controller acceptance through the frozen gate-receipt path."""
    receipt = svc.record_acceptance(
        acceptance,
        producer_artifact=execution.producer_artifact,
        binding=binding,
        role=gr.VERIFIER_ROLE,
        answering_identity=verdict.verifier_identity,
        producer_identity=producer_identity,
        accepted=True,
    )
    # A *rejected* independent receipt asserts the other fail-closed direction.
    gr.validate_independent_gate_receipt(receipt.to_dict())
    return receipt


def _dependent_wave(
    svc: Any,
    gr: Any,
    *,
    task_id: str,
    receipt: Any,
    dependents: tuple[str, ...],
) -> tuple[str, ...]:
    """Compute and release the dependent wave from the declared graph.

    ``dependents`` are the tasks that declare ``task_id`` as a dependency in the
    caller-supplied sequence declaration (the same ``SequenceDeclaration`` the
    dispatch machinery loads through
    :func:`skillweave.dispatch.contracts.load_sequence`).  Each dependent is
    released only when the accepted attempt's gate receipt releases it -- the
    frozen ``_gate_receipt_releases`` rule the scheduler applies to
    ``gate_receipts``.  A dependent whose gate did not release stays ``blocked``
    and is **not** reported as released, so nothing is fabricated; with no
    declared graph the wave is legitimately empty.
    """
    from skillweave.runtime.dagscheduler import Sequence, Task, build_schedule

    if not dependents:
        return ()
    receipt_dict = receipt.to_dict()
    # Every dependent declares the accepted task's gate; the receipt for that
    # gate carries the accepted, artifact-carrying acceptance and is the single
    # authority for the release.
    tasks = [
        Task(id=task_id, gate=None, write_scope=[]),
        *(Task(id=dep, depends_on=[task_id], gate=task_id) for dep in dependents),
    ]
    schedule = build_schedule(
        Sequence(tasks=tasks, session_boundary=f"{task_id}/dependent-wave"),
        gate_receipts={task_id: receipt_dict},
    )
    blocked = set(schedule.blocked)
    released = tuple(d for d in dependents if d not in blocked)
    # The frozen release rule is the single authority: an accepted receipt with
    # its artifact releases its own dependents; anything else releases nothing.
    svc.release_accepted_dependents(
        _AcceptanceView(
            attempt_id=receipt.attempt_id,
            state=receipt.state,
            producer_artifact=receipt.producer_artifact,
            dependents=released,
        ),
        producer_identity=receipt.producer_identity,
    )
    return released


@dataclass(frozen=True)
class _AcceptanceView:
    """The minimal ``AttemptAcceptance``-shaped view the release rule consumes."""

    attempt_id: str
    state: str
    producer_artifact: str
    dependents: tuple[str, ...] = ()


def _declared_dependents(sequence_path: str, task_id: str) -> tuple[str, ...]:
    """Return the declared dependents of ``task_id`` from a sequence file.

    Reads the caller-declared dependency graph through the real dispatch
    contract (:func:`skillweave.dispatch.contracts.load_sequence`): a lane that
    lists ``task_id`` in its ``depends_on`` is a dependent.  With no sequence
    file (or no such lane) the wave is empty -- a dependent is never invented.
    """
    if not sequence_path:
        return ()
    from skillweave.dispatch.contracts import load_sequence

    with open(sequence_path, encoding="utf-8") as handle:
        declaration = json.load(handle)
    sequence = load_sequence(declaration)
    dependents = [
        lane.id for lane in sequence.lanes if task_id in tuple(lane.depends_on)
    ]
    return tuple(sorted(dependents))


def _run_state_driven(
    svc: Any,
    lease: Any,
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    route: str,
    session_id: str,
    sequence: str,
    holder: str,
    worker: WorkerAdapter,
    reviewer: ReviewerAdapter,
    store: Any,
    gr: Any,
) -> DriveResult:
    """The state-driven, verdict-driven loop body (inside the lease guard)."""
    from skillweave.runtime.native_process import (
        STEP_CONTROLLED_INTEGRATION,
        STEP_DEPENDENT_WAVE,
        STEP_INDEPENDENT_REVIEW,
        STEP_READY_DISPATCH,
        STEP_REPAIR,
        STEP_RE_REVIEW,
    )

    # The run must exist: the loop reads/writes durable state for it.
    if store.get_run(run_id) is None:
        raise _UsageError(f"no run {run_id!r} in the store to drive")

    token = svc.fencing_token
    steps: list[str] = []
    reviews: list[str] = []

    # Step 1 -- ready dispatch: record the durable intent (state LAUNCHED) and
    # its boundary owned-attempt-dispatch record.  Guarded transitions below
    # read this state back from the store, so the loop is driven by facts.
    intent, dispatch = svc.dispatch(
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        step=STEP_READY_DISPATCH,
        route=route,
        session_id=session_id,
    )
    steps.append(STEP_READY_DISPATCH)
    intent_id = intent.intent_id
    # The transition is durable *and* recorded: persist the run-keyed
    # checkpoint derived from the store's own intents and journal cursor.
    _persist_transition_checkpoint(svc, step=STEP_READY_DISPATCH, intent_id=intent_id)

    # The launch seam: the production adapter runs the owned attempt here. It
    # launches nothing in tests (the fake records and returns a synthetic record).
    execution = worker.execute(dispatch)
    # A step may outlast the ttl: renew the lease so it never lapses mid-step.
    svc.renew()

    # Step 2 -- independent review, driven by the *recorded* verdict.  The
    # reviewer port supplies independent evidence; the controller derives the
    # acceptance decision from it and the verdict from the durable record.
    verdict = reviewer.review(dispatch, execution)
    decision, binding = _independent_receipt(
        gr,
        execution=execution,
        attempt_id=intent_id,
        verdict=verdict,
        producer_identity=holder,
    )
    recorded = _require_recorded_verdict(store, run_id, expected=STEP_INDEPENDENT_REVIEW)
    svc.reviewed_advance(intent_id, recorded)
    _persist_transition_checkpoint(svc, step=STEP_INDEPENDENT_REVIEW, intent_id=intent_id)
    steps.append(STEP_INDEPENDENT_REVIEW)
    reviews.append(recorded)

    accepted = decision.verdict == gr.GATE_PASS and decision.accepted

    if recorded == REVIEW_FAIL and not accepted:
        # Step 3 -- repair: a failing review leads to a repair round.  The
        # reviewed_advance above already moved the intent LAUNCHED->REPAIRING
        # (that IS the durable repair state); the step is recorded here.
        steps.append(STEP_REPAIR)

        # Step 4 -- re-review, again driven by the *recorded* verdict.  The
        # reviewer port is called again for the re-review round's evidence;
        # the verdict is read from the durable record the review tool wrote.
        reverdict = reviewer.review(dispatch, execution)
        redecision, _ = _independent_receipt(
            gr,
            execution=execution,
            attempt_id=intent_id,
            verdict=reverdict,
            producer_identity=holder,
        )
        rerecorded = _require_recorded_verdict(store, run_id, expected=STEP_RE_REVIEW)
        svc.rereviewed_advance(intent_id, rerecorded)
        _persist_transition_checkpoint(svc, step=STEP_RE_REVIEW, intent_id=intent_id)
        steps.append(STEP_RE_REVIEW)
        reviews.append(rerecorded)
        accepted = redecision.verdict == gr.GATE_PASS and redecision.accepted
        if rerecorded == REVIEW_FAIL:
            # A failing re-review stops the attempt terminally: it never
            # integrates, and the terminal FAILED intent is the causal record.
            return DriveResult(
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                intent_id=intent_id,
                fencing_token=int(token),
                steps=tuple(steps),
                accepted=False,
                released_dependents=(),
                integration="none",
                reviews=tuple(reviews),
            )

    if not accepted:
        # Independent evidence did not decide a PASS (or a re-review failed to
        # pass): fail closed, never integrate.
        return DriveResult(
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            intent_id=intent_id,
            fencing_token=int(token),
            steps=tuple(steps),
            accepted=False,
            released_dependents=(),
            integration="none",
            reviews=tuple(reviews),
        )

    # Acceptance through the frozen gate-receipt path: the artifact is the
    # execution's producer artifact, the answering identity an independent
    # verifier -- never the controller's own label.
    acceptance = gr.AttemptAcceptance(
        attempt_id=intent_id,
        state=gr.ACCEPTANCE_ACCEPTED,
        producer_artifact=execution.producer_artifact,
    )
    receipt = _accepted_receipt(
        svc,
        gr,
        execution=execution,
        acceptance=acceptance,
        binding=binding,
        verdict=verdict,
        producer_identity=holder,
    )

    # Step 5 -- controlled integration. A repository subject integrates through
    # Git; any other subject kind integrates through a controlled content
    # transaction. Integration is guarded from the reviewed state.
    if execution.integration_subject is not None:
        svc.content_integration_advance(intent_id)
        svc.record_content_integration(
            intent_id,
            execution.integration_subject,
            baseline_digest=execution.baseline_digest,
            applied_digest=execution.applied_digest,
        )
        integration = "content_transaction"
    else:
        svc.integration_advance(intent_id)
        integration = "git_merge"
    _persist_transition_checkpoint(svc, step=STEP_CONTROLLED_INTEGRATION, intent_id=intent_id)
    steps.append(STEP_CONTROLLED_INTEGRATION)

    # Step 6 -- dependent wave: compute by the declared dependency graph and the
    # accepted state, and release only what the gate receipt releases.
    dependents = _declared_dependents(sequence, task_id)
    released = _dependent_wave(
        svc, gr, task_id=task_id, receipt=receipt, dependents=dependents
    )
    svc.dependent_wave_advance(intent_id)
    _persist_transition_checkpoint(svc, step=STEP_DEPENDENT_WAVE, intent_id=intent_id)
    steps.append(STEP_DEPENDENT_WAVE)

    return DriveResult(
        run_id=run_id,
        task_id=task_id,
        attempt=attempt,
        intent_id=intent_id,
        fencing_token=int(token),
        steps=tuple(steps),
        accepted=True,
        released_dependents=tuple(released),
        integration=integration,
        reviews=tuple(reviews),
    )


if __name__ == "__main__":
    sys.exit(main())
