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
import re
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
    "HOLD_SUBJECT_KIND_UNKNOWN",
    "HOLD_SANDBOX_TARGET_UNLISTED",
    "HOLD_SANDBOX_UNDECLARED",
    "HOLD_SANDBOX_PRODUCTION_LOOKING",
    "HOLD_SANDBOX_DECLARATION_CHANGED",
    "OWNER_SANDBOX_DECLARATION_FILENAME",
    "SandboxDeclarationRefused",
    "OwnerSandboxTarget",
    "OwnerSandboxDeclaration",
    "load_owner_sandbox_declaration",
    "resolve_sandbox_admission",
    "HOLD_CMS_PUBLICATION_NO_DURABLE_REVIEW",
    "HOLD_CMS_PUBLICATION_PRODUCER_UNVERIFIED",
    "HOLD_CMS_PUBLICATION_UNAUTHORIZED",
    "HOLD_CMS_PUBLICATION_CREDENTIAL_MISSING",
    "HOLD_CMS_PUBLICATION_EGRESS_UNPERMITTED",
    "CMS_PUBLICATION_SANDBOX_TARGET",
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

#: Named holds for the controller *restart* (SW-SCOPE-010 S10-B c2).  A
#: restarted controller reads the written checkpoint and the durable dispatch
#: intents and adopts exactly the ordering-first in-flight intent whose recorded
#: owned identity still verifies.  Every other outcome is a *named* checkpoint
#: hold -- and in none of them is the predecessor's launch intent replayed.
HOLD_CHECKPOINT_UNWRITTEN = (
    "native-controller refused: no run-keyed transition checkpoint is written "
    "for the run, so a restart has no durable statement to resume from; the "
    "predecessor's launch intent is never replayed (named checkpoint hold)"
)
HOLD_CHECKPOINT_INTENT_MISMATCH = (
    "native-controller refused: the written checkpoint does not name the "
    "ordering-first in-flight dispatch intent, so it is not the statement of "
    "the state the restart must adopt; the launch intent is never replayed "
    "(named checkpoint hold)"
)
HOLD_CHECKPOINT_NO_OWNED_IDENTITY = (
    "native-controller refused: the ordering-first in-flight dispatch intent "
    "carries no recorded owned process identity, so it cannot be adopted as the "
    "owned attempt; the launch intent is never replayed (named checkpoint hold)"
)
HOLD_CHECKPOINT_IDENTITY_UNVERIFIED = (
    "native-controller refused: the ordering-first in-flight dispatch intent's "
    "recorded owned identity no longer verifies against the live OS, so the "
    "restart adopts nothing rather than re-owning an unverified attempt; the "
    "launch intent is never replayed (named checkpoint hold)"
)
HOLD_CHECKPOINT_IDENTITY_RECYCLED = (
    "native-controller refused: the ordering-first in-flight dispatch intent's "
    "recorded PID has been recycled -- the recorded creation instant no longer "
    "matches the live process -- so the restart adopts nothing; the launch "
    "intent is never replayed (named checkpoint hold)"
)
HOLD_CHECKPOINT_IDENTITY_STALE = (
    "native-controller refused: the ordering-first in-flight dispatch intent's "
    "recorded command no longer matches the live process argument vector, so "
    "the restart adopts nothing; the launch intent is never replayed (named "
    "checkpoint hold)"
)
HOLD_CHECKPOINT_IDENTITY_FOREIGN = (
    "native-controller refused: the ordering-first in-flight dispatch intent's "
    "recorded image is not the image the PID now runs, so the restart adopts "
    "nothing; the launch intent is never replayed (named checkpoint hold)"
)
HOLD_CHECKPOINT_IDENTITY_NOT_ALIVE = (
    "native-controller refused: the ordering-first in-flight dispatch intent's "
    "recorded process is no longer observable, so its state is unknown -- not "
    "confirmed terminated -- and the restart adopts nothing; the launch intent "
    "is never replayed (named checkpoint hold)"
)

#: The named checkpoint hold each re-verification failure maps to, keyed by the
#: declared :mod:`skillweave.runtime.process_identity` reason.  A fresh
#: controller never re-owns an attempt it cannot still verify.
_CHECKPOINT_HOLD_BY_REASON = {
    "no_recorded_identity": HOLD_CHECKPOINT_NO_OWNED_IDENTITY,
    "recycled_pid": HOLD_CHECKPOINT_IDENTITY_RECYCLED,
    "stale_command": HOLD_CHECKPOINT_IDENTITY_STALE,
    "foreign_worker": HOLD_CHECKPOINT_IDENTITY_FOREIGN,
    "not_alive": HOLD_CHECKPOINT_IDENTITY_NOT_ALIVE,
}

#: The named hold when a run already carries a live in-flight owned attempt and
#: this drive was *not* asked to resume it.  Re-dispatching the scope would
#: replay the predecessor's launch intent, which the controller never does.
HOLD_CHECKPOINT_ALREADY_IN_FLIGHT = (
    "native-controller refused: the run already carries a live in-flight owned "
    "attempt, so this drive would replay the predecessor's launch intent; re-run "
    "with resume to adopt the ordering-first in-flight attempt (named checkpoint "
    "hold)"
)

#: Named holds for the recovered controller's **continuation read**
#: (SW-SCOPE-010 S10-B criterion 3).  A recovered controller reaches the
#: ``harness-continuation-v1`` decision the handoff tool persisted through the
#: run-scoped reader :func:`skillweave.mcp.server.read_continuation_decision`
#: (the durable mailbox), never by re-deriving it.  Every outcome other than a
#: decision recorded for *this* run is a *named* continuation hold, and in none
#: of them is another run's decision relabelled or the launch intent replayed.
HOLD_CONTINUATION_UNREADABLE = (
    "native-controller refused: the harness-continuation decision persisted for "
    "this run could not be read under the recovered controller's scope, so the "
    "restart has no durable continuation statement to continue from; the "
    "predecessor's launch intent is never replayed (named continuation hold)"
)
HOLD_CONTINUATION_NOT_PERSISTED = (
    "native-controller refused: no harness-continuation decision is persisted "
    "for this run under the recovered controller's scope, so there is no "
    "durable continuation statement to read back; another run's decision is "
    "never relabelled as this run's and the launch intent is never replayed "
    "(named continuation hold)"
)
HOLD_CONTINUATION_FOREIGN_RUN = (
    "native-controller refused: the harness-continuation decision read back "
    "names a different run than the one being recovered, so it is not the "
    "continuation statement of this run; a foreign run's decision is never "
    "relabelled (named continuation hold)"
)
HOLD_CONTINUATION_REPLAYED = (
    "native-controller refused: the harness-continuation decision persisted for "
    "this run names a different owned attempt than the ordering-first in-flight "
    "intent being adopted, so continuing from it would replay a predecessor "
    "attempt's continuation; a decision for another attempt is never replayed "
    "as this attempt's (named continuation hold)"
)

#: Named hold for the controlled-integration **subject-kind discrimination**
#: (SW-SCOPE-010 S10-C criterion 1).  The integration surface is a function of
#: the subject *kind*, never of the mere presence of a subject: ``repository``
#: integrates through Git, every non-repository kind integrates through the
#: guarded content transaction, and a subject whose kind this controller does
#: not discriminate is refused **by name** -- never silently inferred to be a
#: repository (which would send it through Git) and never coerced onto the
#: content surface.  The refusal happens *before* any durable mutation, so an
#: undiscriminated subject leaves no partial integration behind.
HOLD_SUBJECT_KIND_UNKNOWN = (
    "native-controller refused: the integration subject names a kind this "
    "controller does not discriminate, so no integration surface can be "
    "selected for it; a repository subject integrates through Git, a "
    "non-repository subject through the guarded content transaction, and an "
    "unknown kind is never inferred to be either (named hold)"
)

#: The four names an owner **sandbox declaration** is refused under
#: (SW-SCOPE-010 S10-C criterion 2).  The declaration is durable *owner-authored*
#: state -- ``owner-sandbox.yaml`` in the planning repository -- that the
#: production caller reads from a path it resolves itself.  It is never supplied
#: through any tool, MCP argument or CLI argument, so a target can only be
#: admitted by a declaration the owner actually wrote; every other outcome is a
#: *named* hold and nothing is applied.
HOLD_SANDBOX_TARGET_UNLISTED = (
    "native-controller refused: the sandbox target is not listed in the owner "
    "sandbox declaration, so the tool never invents a target the owner did not "
    "declare (named sandbox hold)"
)
HOLD_SANDBOX_UNDECLARED = (
    "native-controller refused: no owner sandbox declaration exists at the "
    "durable owner-authored path, so the controller never assumes an implicit "
    "or default sandbox target (named sandbox hold)"
)
HOLD_SANDBOX_PRODUCTION_LOOKING = (
    "native-controller refused: the sandbox target names a production-looking "
    "surface (a production, live or main-domain marker); the declaration is "
    "never a licence to reach production (named sandbox hold)"
)
HOLD_SANDBOX_DECLARATION_CHANGED = (
    "native-controller refused: the owner sandbox declaration changed between "
    "load and apply -- its recorded SHA-256 no longer matches the file bytes -- "
    "so the target is never applied against a declaration the owner did not "
    "author (named sandbox hold)"
)

#: Whole-segment markers of a *production-looking* sandbox target.  The owner
#: decision names ``prod``, ``live`` and the main domain.  A marker is matched as
#: a delimited segment (``prod-eu``, ``live.example.dev``, ``main``), never as a
#: bare substring, so an innocuous name such as ``product-preview`` or a hostname
#: containing ``domain`` is not misread as production -- the refusal is a fact
#: about the declaration, not a guess about a deployment.
_SANDBOX_PRODUCTION_MARKERS = frozenset({"prod", "production", "live", "main"})

#: The owner-authored declaration file name the owner decision pins.  The
#: *directory* is the production caller's own durable state location, resolved by
#: the caller -- never an argument.
OWNER_SANDBOX_DECLARATION_FILENAME = "owner-sandbox.yaml"

#: The owner-authorized *sandbox* target an irreversible CMS publication is
#: authorized on (SW-SCOPE-010 S10-C criterion 3).  It is the same declared
#: sandbox the CMS lifecycle is driven on, carries no production marker, and is
#: the only target this controller is allowed to touch -- the publication path
#: never contacts a remote CMS, so nothing production is published.
CMS_PUBLICATION_SANDBOX_TARGET = "sandbox/ch"

#: Named holds for the irreversible CMS publication **authorization**
#: (SW-SCOPE-010 S10-C criterion 3).  Publication through the controller
#: requires the durable independent review and the verified producer facts, a
#: designated human authorization, and a provisioned credential and permitted
#: egress.  Each refusal is named -- following the existing holds in this module
#: -- so an unauthorized or unprovisioned publication is never anonymous and
#: never silently collapsed into a normal outcome.
HOLD_CMS_PUBLICATION_NO_DURABLE_REVIEW = (
    "native-controller refused: the irreversible CMS publication was presented "
    "no durable, server-recorded independent review binding the transaction's "
    "exact subject revision and artifact digest; a caller-constructed review is "
    "never accepted, so the publication is refused fail-closed (named hold)"
)
HOLD_CMS_PUBLICATION_PRODUCER_UNVERIFIED = (
    "native-controller refused: the publishing attempt's producer identity does "
    "not match the identity the controller derived from the owned attempt's "
    "durable record; a forged or unverified producer can never authorize its own "
    "irreversible publication (named hold)"
)
HOLD_CMS_PUBLICATION_UNAUTHORIZED = (
    "native-controller refused: irreversible CMS publication requires the "
    "designated human authorization bound to the exact subject revision and "
    "artifact digest, and the governing autonomy policy must approve the "
    "irreversible action; without them the publication is refused fail-closed "
    "(named hold)"
)
HOLD_CMS_PUBLICATION_CREDENTIAL_MISSING = (
    "native-controller refused: no credential reference for the publication is "
    "observed available under the effective admission policy; a missing "
    "credential is refused by name, never simulated, and no unprovisioned "
    "publication is attempted (named hold)"
)
HOLD_CMS_PUBLICATION_EGRESS_UNPERMITTED = (
    "native-controller refused: the publication endpoint cannot egress under the "
    "effective admission policy; egress is default-deny, so an unpermitted "
    "endpoint is refused by name and no remote CMS is contacted (named hold)"
)

#: The principal a recovered controller reads its continuation decision under
#: when the caller names none.  It mirrors the handoff tool's own direct-caller
#: principal (``in-process``, the documented CLI-parity seam of the messaging
#: tools), so a decision the tool persisted with no bound authority is readable
#: by the controller with no extra input.  A principal is never invented: a
#: caller that persists under an authenticated principal names it explicitly.
_CONTINUATION_DEFAULT_PRINCIPAL = "in-process"

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

#: The environment variable describing the *admission* facts of an irreversible
#: CMS publication (SW-SCOPE-010 S10-C criterion 3): the egress endpoint and
#: allowlist, and the credential references with their observed availability.
#: It is one JSON object so the controller reads the operator's provisioned
#: environment rather than inventing a credential or an endpoint: absent the
#: variable the publication is an in-process action with no credential
#: requirement.  Shape::
#:
#:   {"endpoint": "https://cms.example/prod", "allow_network": true,
#:    "endpoint_allowlist": ["cms.example"],
#:    "credential_refs": ["cms_token"],
#:    "credential_availability": {"cms_token": "available"}}
#:
#: A missing credential reference or an endpoint that cannot egress is refused
#: *by name* -- never simulated, never probed on the network.
_ENV_CMS_PUBLICATION_ADMISSION = "SKILLWEAVE_CMS_PUBLICATION_ADMISSION"

#: The publication adapter's descriptor id and operation, gated through the
#: common adapter admission policy.  The publication is a *single* declared
#: adapter/operation so the credential and egress gates have a name to attribute
#: their refusal to.
_CMS_PUBLICATION_ADAPTER_ID = "cms-publication"
_CMS_PUBLICATION_OPERATION = "publish"

#: The autonomy-policy names the publication branch understands.  ``none`` (the
#: default) means *no* policy governs the irreversible action, so the
#: publication is never auto-approved; ``owner`` is the designated publication
#: authority that approves only the irreversible publication.  The shipped
#: policies (conservative/moderate/unicorn) all *hold* an irreversible action.
_PUBLICATION_POLICY_NONE = "none"
_PUBLICATION_POLICY_OWNER = "owner"

#: The environment variable naming the autonomy policy that governs the
#: irreversible publication.  Absent (or ``none``) means *no* policy governs it,
#: so the publication is never auto-approved; ``owner`` is the designated
#: publication authority.  An unknown name is a caller error (exit 2), never an
#: implicit approval.
_ENV_CMS_PUBLICATION_POLICY = "SKILLWEAVE_CMS_PUBLICATION_POLICY"


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
    #: On a restart drive that adopted an ordering-first in-flight attempt, the
    #: derived statement of *what* was adopted (run, intent, owned identity).
    #: ``None`` on a normal fresh drive: the field records a read, never a
    #: caller-supplied claim.
    adoption: Optional[dict[str, Any]] = None
    #: On a restart drive that adopted an in-flight attempt, the
    #: ``harness-continuation-v1`` decision read back from the run's durable
    #: mailbox through the run-scoped reader: the *same* decision the handoff
    #: tool persisted, verbatim as the reader returned it.  ``None`` on a fresh
    #: drive -- the controller never derives or invents the decision.
    continuation: Optional[dict[str, Any]] = None

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
            "adoption": self.adoption,
            "continuation": self.continuation,
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
        "--resume",
        action="store_true",
        dest="resume",
        help="Restart the controller: read the run-keyed checkpoint and the "
             "durable dispatch intents and adopt the ordering-first in-flight "
             "intent whose recorded owned identity verifies, instead of "
             "replaying the predecessor's launch intent. Without a verifiable "
             "owned attempt the restart refuses with a named checkpoint hold.",
    )
    parser.add_argument(
        "--principal",
        default=_CONTINUATION_DEFAULT_PRINCIPAL,
        help="The scoped principal the recovered controller reads the run's "
             "persisted harness-continuation decision under (default: "
             "in-process, the CLI-parity principal the handoff tool writes it "
             "as). A foreign principal's mailbox is never read and no principal "
             "is invented.",
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
    parser.add_argument(
        "--authorize-cms-publication",
        dest="authorize_cms_publication",
        default="",
        help="Authorize the irreversible publication of one managed CMS "
             "transaction (SW-SCOPE-010 S10-C criterion 3): load the transaction "
             "and the durable independent review, verify the producer identity "
             "against the owned attempt's record, require the designated human "
             "authorization, then decide under the autonomy policy. Additive and "
             "opt-in; refuses by name when the review, producer, authorization, "
             "credential or egress is missing. No remote CMS is contacted.",
    )
    parser.add_argument(
        "--cms-txn-id",
        dest="cms_txn_id",
        default="",
        help="The managed CMS transaction id to authorize for publication "
             "(default: <run-id>/<task-id>).",
    )
    parser.add_argument(
        "--cms-artifact-digest",
        dest="cms_artifact_digest",
        default="",
        help="The artifact digest the publication asserts; the durable review "
             "and the designated authorization must both bind to it.",
    )
    parser.add_argument(
        "--cms-review-mailbox",
        dest="cms_review_mailbox",
        default="",
        help="Path to the durable mailbox holding the independent publication "
             "review (defaults to the server-owned mailbox path).",
    )
    parser.add_argument(
        "--cms-review-message-id",
        dest="cms_review_message_id",
        default="",
        help="The durable review message id to load as the independent review; "
             "a missing reference is refused, never fabricated.",
    )
    parser.add_argument(
        "--cms-publication-authorizer",
        dest="cms_publication_authorizer",
        default="",
        help="The designated human authorizer for the publication; must differ "
             "from the derived producer identity.",
    )
    parser.add_argument(
        "--cms-publication-authority",
        dest="cms_publication_authority",
        default="",
        help="Optional authority label recorded on the designated publication "
             "authorization.",
    )
    parser.add_argument(
        "--cms-binding",
        dest="cms_binding",
        default="",
        help="Optional authorized CMS sandbox target the publication is bound "
             "to (default: the declared sandbox target; a production-looking "
             "target is refused).",
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

    # Additive, opt-in irreversible CMS publication *authorization*
    # (SW-SCOPE-010 S10-C criterion 3): this branch loads the managed
    # transaction and the durable independent review, verifies the producer
    # against the owned attempt's record, requires the designated human
    # authorization and consults the autonomy policy -- and contacts no remote
    # CMS. It is checked before the worker/reviewer fail-closed holds so the
    # surfaces never interfere.
    if args.authorize_cms_publication:
        try:
            result = _authorize_cms_publication(
                run_id=args.run_id,
                task_id=args.task_id,
                attempt=args.attempt,
                db_path=args.db_path,
                holder=args.holder,
                ttl_seconds=args.ttl_seconds,
                txn_id=args.cms_txn_id,
                artifact_digest=args.cms_artifact_digest,
                review_mailbox=args.cms_review_mailbox,
                review_message_id=args.cms_review_message_id,
                authorizer=args.cms_publication_authorizer,
                authority=args.cms_publication_authority,
                binding_target=args.cms_binding,
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
            resume=args.resume,
            principal=args.principal,
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


# ── irreversible CMS publication authorization (SW-SCOPE-010 S10-C crit. 3) ──


@dataclass(frozen=True)
class _OwnerPublicationPolicy:
    """The designated publication authority, named ``owner``.

    It approves *only* the irreversible publication action, so the owner holds
    the explicit publication grant while every shipped autonomy policy
    (conservative/moderate/unicorn) *holds* an irreversible action.  This is a
    policy *name* the operator selects; it is never an implicit approval.
    """

    name: str = _PUBLICATION_POLICY_OWNER

    def check(self, action: Any) -> Any:
        from skillweave.runtime.assay import ActionType, PolicyDecision

        if getattr(action, "action_type", None) == ActionType.IRREVERSIBLE_ACTION:
            return PolicyDecision(
                approved=True, reason="owner approved the irreversible publication"
            )
        return PolicyDecision(
            approved=False, reason="the owner approves only the publication"
        )


def _resolve_publication_policy(name: str) -> Any:
    """Resolve the autonomy policy *name* governing the publication, or refuse.

    ``none``/empty means *no* policy governs the irreversible action (never an
    auto-approval); ``owner`` is the designated publication authority; any other
    name is a caller error (:class:`_UsageError`, exit 2).  The shipped policies
    are reachable by name too, so ``conservative``/``moderate``/``unicorn`` hold
    rather than publish.
    """
    normalized = (name or "").strip().lower()
    if not normalized or normalized == _PUBLICATION_POLICY_NONE:
        return None
    if normalized == _PUBLICATION_POLICY_OWNER:
        return _OwnerPublicationPolicy()
    from skillweave.runtime.assay import create_policy

    try:
        return create_policy(normalized)
    except (ValueError, KeyError) as exc:
        raise _UsageError(
            f"unknown CMS publication autonomy policy {name!r}; expected one of "
            f"{_PUBLICATION_POLICY_NONE!r}, {_PUBLICATION_POLICY_OWNER!r} or a "
            f"shipped assay policy name"
        ) from exc


def _publication_admission(
    *, binding_target: str, admission_json: str, held_adapter: bool
) -> None:
    """Gate the publication through the common adapter admission policy.

    The endpoint, the egress decision and the credential references are read
    from the operator's ``_ENV_CMS_PUBLICATION_ADMISSION`` JSON -- the controller
    never invents an endpoint or a credential.  Egress is checked *before*
    credentials (default-deny), so an unpermitted endpoint is refused by name
    whether or not a credential is also missing; a missing credential reference
    is then refused by name.  Nothing here contacts the network: it is a pure
    decision over the declared facts.

    ``held_adapter`` admits a *local* publication (no out-of-process transport),
    where the egress gate does not apply and only the declared credential
    references are required.
    """
    from skillweave.adapters.descriptor import AdapterDescriptor, OperationBinding
    from skillweave.adapters.policy import (
        EffectivePolicy,
        EgressPolicy,
        admit_invocation,
    )

    facts: Mapping[str, Any] = {}
    if admission_json.strip():
        try:
            parsed = json.loads(admission_json)
        except ValueError as exc:
            raise _UsageError(
                f"{_ENV_CMS_PUBLICATION_ADMISSION} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise _UsageError(
                f"{_ENV_CMS_PUBLICATION_ADMISSION} must be a JSON object"
            )
        facts = parsed

    endpoint = str(facts.get("endpoint") or "").strip()
    allow_network = bool(facts.get("allow_network", False))
    allowlist = tuple(str(h) for h in (facts.get("endpoint_allowlist") or ()))
    credential_refs = tuple(str(r) for r in (facts.get("credential_refs") or ()))
    reported = {
        str(k): str(v)
        for k, v in dict(facts.get("credential_availability") or {}).items()
    }

    policy = EffectivePolicy(
        allowed_adapter_ids=(_CMS_PUBLICATION_ADAPTER_ID,),
        egress=EgressPolicy(
            allow_network=allow_network, endpoint_allowlist=allowlist
        ),
    )

    serving = "held" if held_adapter else "remote"
    operations = (
        OperationBinding(
            name=_CMS_PUBLICATION_OPERATION,
            input_contract=f"cms-publication-{serving}/v1",
            result_contract=f"cms-publication-{serving}/v1",
        ),
    )
    if held_adapter:
        descriptor = AdapterDescriptor(
            id=_CMS_PUBLICATION_ADAPTER_ID,
            version="1.0.0",
            categories=("generic",),
            operations=operations,
            transport="local",
            credential_refs=credential_refs,
        )
    else:
        descriptor = AdapterDescriptor(
            id=_CMS_PUBLICATION_ADAPTER_ID,
            version="1.0.0",
            categories=("generic",),
            operations=operations,
            transport="https",
            credential_refs=credential_refs,
        )

    decision = admit_invocation(
        policy,
        descriptor,
        category="generic",
        operation=_CMS_PUBLICATION_OPERATION,
        credential_availability=reported,
        endpoint=endpoint,
    )
    if decision.admitted:
        return
    asset = decision.asset or ""
    if asset == "egress":
        raise _RefusedError(HOLD_CMS_PUBLICATION_EGRESS_UNPERMITTED)
    if asset.startswith("credential:"):
        raise _RefusedError(HOLD_CMS_PUBLICATION_CREDENTIAL_MISSING)
    # Any other unadmitted asset (allowlist/operation/capability) is a
    # caller-input problem, not an external hold.
    raise _UsageError(decision.reason)


@dataclass(frozen=True)
class _PublicationAuthorizationResult:
    """What authorizing one CMS publication for publication produced.

    A decision, never a publication: the record reports whether the irreversible
    publication was authorized and, if not, *why*.  No remote CMS is contacted
    and the transaction is left untouched.
    """

    run_id: str
    txn_id: str
    subject_revision: str
    artifact_digest: str
    producer_identity: str
    approved: bool
    reason: str
    policy_reason: str = ""
    requires_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "txn_id": self.txn_id,
            "subject_revision": self.subject_revision,
            "artifact_digest": self.artifact_digest,
            "producer_identity": self.producer_identity,
            "approved": self.approved,
            "reason": self.reason,
            "policy_reason": self.policy_reason,
            "requires_human": self.requires_human,
        }


def _mailbox_path() -> str:
    """The server-owned durable mailbox the review tool writes to."""
    from skillweave.mcp import server as _server

    return _server.default_mailbox_path()


def _authorize_cms_publication(
    *,
    run_id: str,
    task_id: str,
    attempt: int,
    db_path: str,
    holder: str,
    ttl_seconds: int,
    txn_id: str,
    artifact_digest: str,
    review_mailbox: str,
    review_message_id: str,
    authorizer: str,
    authority: str,
    binding_target: str,
    store: Any,
) -> _PublicationAuthorizationResult:
    """Authorize an irreversible CMS publication through the native controller.

    Every fact the decision rests on is read from durable state, never taken
    from the caller's hope: the transaction (and its *derived* producer) is
    loaded from the store, the independent review is loaded *by reference* from
    the durable mailbox, and the credential/egress facts come from the
    operator's declared admission environment.  The five refusals each fail
    closed with a *named hold*:

    * no durable, server-recorded review binds the exact revision/digest;
    * the publishing attempt's producer does not match the owned attempt's
      durable record (a forged producer);
    * no designated human authorization (or a non-binding/self-authorization);
    * a credential reference is not observed available;
    * the endpoint cannot egress under the effective policy.

    No provider, gateway or CMS call is made or probed, and nothing is published
    here -- the decision is returned for the caller to record.
    """
    from skillweave.runtime.native_process import (
        ExternalHold,
        NativeProcessService,
        PublicationAuthorization,
        PublicationRefused,
    )
    from skillweave.runtime.store import SQLiteRunStore

    owns_store = store is None
    if owns_store:
        store = SQLiteRunStore(db_path)
    try:
        service = NativeProcessService(store, holder)
        with _LeaseGuard(service, ttl_seconds=ttl_seconds):
            resolved_txn_id = txn_id.strip() or f"{run_id}/{task_id}"
            txn = service.load_cms_transaction(run_id, resolved_txn_id)
            if txn is None:
                # The durable review cannot bind a transaction that is not there.
                raise _RefusedError(HOLD_CMS_PUBLICATION_NO_DURABLE_REVIEW)

            # The publishing attempt's producer must be the identity the
            # controller derived from the owned attempt's *durable* record -- the
            # exact value ``open_cms_transaction`` derived and live-verified when
            # the transaction was opened.  A blanked or forged producer (an
            # identity the owned attempt does not verify to) is refused by name
            # *before* the review is resolved, so the durable producer fact is
            # checked, never trusted.
            from skillweave.runtime.native_process import _durable_producer_identity

            intent = (
                store.get_dispatch_intent(txn.intent_id) if txn.intent_id else None
            )
            verified_producer = (
                _durable_producer_identity(intent) if intent is not None else ""
            )
            if not verified_producer or txn.producer_identity != verified_producer:
                raise _RefusedError(HOLD_CMS_PUBLICATION_PRODUCER_UNVERIFIED)

            path = review_mailbox.strip() or _mailbox_path()
            message_id = (review_message_id or "").strip()
            if not message_id:
                # No review reference at all: the publication presents no durable
                # independent review, so it is refused by that name.
                raise _RefusedError(HOLD_CMS_PUBLICATION_NO_DURABLE_REVIEW)

            # The egress/credential admission gate runs BEFORE the service call so
            # a missing credential or an unpermitted endpoint is the *named*
            # refusal, never simulated and never attempted.
            held_adapter = not binding_target.strip()
            _publication_admission(
                binding_target=binding_target,
                admission_json=os.environ.get(_ENV_CMS_PUBLICATION_ADMISSION, ""),
                held_adapter=held_adapter,
            )

            policy = _resolve_publication_policy(
                os.environ.get(_ENV_CMS_PUBLICATION_POLICY, "")
            )
            if not (authorizer or "").strip():
                # No designated human authorization was presented at all: refuse
                # by name rather than constructing a non-binding authorization.
                raise _RefusedError(HOLD_CMS_PUBLICATION_UNAUTHORIZED)
            authorization = PublicationAuthorization(
                authorizer=authorizer,
                subject_revision=txn.subject_revision,
                artifact_digest=artifact_digest,
                authority=authority,
            )
            from skillweave.runtime.native_process import DurableReviewRef

            try:
                decision = service.authorize_cms_publication(
                    txn,
                    authorization=authorization,
                    artifact_digest=artifact_digest,
                    review_ref=DurableReviewRef(
                        mailbox_path=path, message_id=message_id
                    ),
                    policy=policy,
                )
            except PublicationRefused as exc:
                raise _RefusedError(_publication_hold_for_field(exc.field)) from exc
            except ExternalHold as exc:
                # A missing external prerequisite is a *named external hold*.
                raise _RefusedError(str(exc)) from exc

            result = _PublicationAuthorizationResult(
                run_id=run_id,
                txn_id=resolved_txn_id,
                subject_revision=txn.subject_revision,
                artifact_digest=artifact_digest,
                producer_identity=txn.producer_identity,
                approved=bool(decision.approved),
                reason=decision.reason,
                policy_reason=getattr(decision, "policy_reason", ""),
                requires_human=bool(getattr(decision, "requires_human", False)),
            )
        if not result.approved:
            # An unapproved irreversible publication is refused by name (exit 3),
            # never silently reported as authorized.
            raise _RefusedError(
                f"{HOLD_CMS_PUBLICATION_UNAUTHORIZED} (policy: {result.policy_reason or result.reason})"
            )
        return result
    finally:
        if owns_store:
            store.close()


def _publication_hold_for_field(field: str) -> str:
    """Map a :class:`PublicationRefused` field to its named controller hold."""
    if field == "review":
        return HOLD_CMS_PUBLICATION_NO_DURABLE_REVIEW
    if field == "producer_identity":
        return HOLD_CMS_PUBLICATION_PRODUCER_UNVERIFIED
    # authorization / authorizer / subject_revision / scope: the designated
    # human authorization is missing, non-binding, self-authored or widening.
    return HOLD_CMS_PUBLICATION_UNAUTHORIZED


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
    resume: bool,
    principal: str,
    worker: WorkerAdapter,
    reviewer: ReviewerAdapter,
    store: Any,
) -> DriveResult:
    """Compose the service, acquire the lease, drive the loop from state.

    The loop is driven by the *recorded* review verdict, not a fixed script: a
    passing review integrates; a failing review repairs then re-reviews; a
    failing re-review stops the attempt terminally.  The lease is signal-safe
    and released on every exit path.

    With ``resume`` the drive is a **restart**: before dispatching anything it
    reads the run-keyed checkpoint and the durable dispatch intents and adopts
    the ordering-first in-flight intent whose recorded owned identity verifies
    (a named checkpoint hold otherwise).  With no ``resume`` but a live
    in-flight attempt already present the drive refuses with a named hold rather
    than replaying the predecessor's launch intent.
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
                    resume=resume,
                    principal=principal,
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


def _read_continuation_decision(
    *, run_id: str, principal: str, adopted_intent_id: str
) -> dict[str, Any]:
    """Read back the run's persisted ``harness-continuation-v1`` decision.

    The recovered controller does **not** derive or invent a continuation
    statement: it reads the *same* decision the handoff tool persisted, through
    the run-scoped reader :func:`skillweave.mcp.server.read_continuation_decision`
    (the reader is scoped by the record's *own* durable ``run_id``, so another
    run's decision can never be relabelled as this run's).

    Four named holds replace a silent continuation:

    * ``HOLD_CONTINUATION_UNREADABLE`` -- the reader itself refused (no mailbox,
      no scoped principal, an unauthenticated read), or its reply is not a
      well-formed ok payload.
    * ``HOLD_CONTINUATION_FOREIGN_RUN`` -- the reader returned a decision whose
      own ``run_id`` is not the run being recovered.
    * ``HOLD_CONTINUATION_NOT_PERSISTED`` -- no decision is persisted for this
      run (``found`` is false).
    * ``HOLD_CONTINUATION_REPLAYED`` -- a decision *is* persisted for this run
      but it names a different owned attempt than the ordering-first in-flight
      intent ``adopted_intent_id`` this restart is resuming.  Replaying a
      predecessor attempt's continuation as the current attempt's would be
      exactly the retroactive mislabelling criterion 3 forbids, so it is a
      named hold -- never a silent continuation.

    The decision is returned verbatim as the reader produced it -- never an
    empty or synthetic decision, and the predecessor's launch intent is never
    replayed in its place.
    """
    from skillweave.mcp.server import read_continuation_decision

    effective = principal or _CONTINUATION_DEFAULT_PRINCIPAL
    payload = read_continuation_decision(
        run_id=run_id, principal=effective, session_token=""
    )
    try:
        raw = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError):
        raise _RefusedError(HOLD_CONTINUATION_UNREADABLE) from None
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        raise _RefusedError(HOLD_CONTINUATION_UNREADABLE)
    if not raw.get("found"):
        raise _RefusedError(HOLD_CONTINUATION_NOT_PERSISTED)
    # The record's own durable run id is what scopes the read: a decision that
    # names a different run is a foreign statement, never this run's.
    if raw.get("run_id") != run_id:
        raise _RefusedError(HOLD_CONTINUATION_FOREIGN_RUN)
    decision = raw.get("decision")
    if not isinstance(decision, dict) or not decision:
        raise _RefusedError(HOLD_CONTINUATION_NOT_PERSISTED)
    if decision.get("run_id") not in (None, run_id):
        raise _RefusedError(HOLD_CONTINUATION_FOREIGN_RUN)
    # Bind the decision to the *adopted* attempt: a decision recorded for a
    # different intent is a predecessor attempt's continuation and is never
    # replayed as this attempt's.  A decision carrying no intent id is the
    # adapter's own plan (the handoff tool records the session it saw, not
    # always an intent) and is not itself a replay.
    recorded_intent = decision.get("intent_id")
    if recorded_intent and recorded_intent != adopted_intent_id:
        raise _RefusedError(HOLD_CONTINUATION_REPLAYED)
    return decision


def _adopt_in_flight_attempt(
    svc: Any, store: Any, *, run_id: str, resume: bool
) -> Any:
    """Read the checkpoint + intents and adopt the ordering-first attempt.

    Returns a :class:`~skillweave.runtime.native_process.RestartAdoption` when
    the run already carries an in-flight owned attempt, or ``None`` when the run
    has no in-flight intent (a fresh dispatch is admitted normally).

    The read is always performed -- a drive never dispatches over an existing
    in-flight attempt -- and the *derivation* is the durable one: the run key,
    the ordering-first intent and the owned identity all come from the store's
    records, never from the caller's flags.  With ``resume`` a verifiable
    adoption proceeds; an unverifiable one is a **named checkpoint hold** and
    the predecessor's launch intent is never replayed.  Without ``resume`` a
    live in-flight attempt is refused by name rather than replayed.
    """
    in_flight = tuple(store.list_dispatch_intents(run_id, claiming_only=True))
    if not in_flight:
        return None
    adoption = svc.read_restart_adoption(run_id)
    if not adoption.adopted:
        raise _RefusedError(
            f"{adoption.hold}: "
            f"{_CHECKPOINT_HOLD_BY_REASON.get(adoption.reason, HOLD_CHECKPOINT_UNWRITTEN)}"
        )
    if not resume:
        raise _RefusedError(HOLD_CHECKPOINT_ALREADY_IN_FLIGHT)
    return adoption


def _integration_mode_for_subject(subject: Any) -> str:
    """The controlled-integration surface this attempt's subject kind requires.

    ``None`` (the attempt declared no explicit subject) keeps the legacy
    behaviour byte-identical: the attempt integrates through the Git merge
    record.  An explicit subject is discriminated **by kind** through the
    service's own :func:`~skillweave.runtime.native_process.integration_mode_for`,
    so the surface is never chosen by the caller: the ``repository`` kind
    integrates through Git, every non-repository kind through the guarded
    content transaction.  A subject whose kind this controller does not
    discriminate is refused **by name** (:data:`HOLD_SUBJECT_KIND_UNKNOWN`)
    before any durable mutation -- it is never inferred to be a repository and
    never coerced onto the content surface.
    """
    from skillweave.runtime.native_process import (
        INTEGRATION_GIT_MERGE,
        UnknownSubjectKind,
        integration_mode_for,
    )

    if subject is None:
        return INTEGRATION_GIT_MERGE
    try:
        return integration_mode_for(subject)
    except UnknownSubjectKind as exc:
        raise _RefusedError(f"{HOLD_SUBJECT_KIND_UNKNOWN}: {exc}") from exc


# ── Owner sandbox declaration (SW-SCOPE-010 S10-C criterion 2) ─────────────
#
# Additive and opt-in, and deliberately **outside the argv surface**.  The owner
# decision (`owner-decision-sandbox-declaration.md`, decided 2026-10-16) fixes
# the form: a list of allowed sandbox targets in `owner-sandbox.yaml` under the
# planning repository's initiative folder, never supplied through any tool, MCP
# argument or CLI argument.  The production caller resolves the owner-state
# *directory* itself; below, the loader appends only the pinned file name,
# records the SHA-256 of the bytes it read, and refuses by name when the target
# is unlisted, the declaration is absent, the target looks production, or the
# bytes changed between load and apply.


class SandboxDeclarationRefused(Exception):
    """The owner sandbox declaration does not admit this target.

    ``hold`` is the named hold (one of the ``HOLD_SANDBOX_*`` names) the refusal
    reports, so a caller never has to parse prose to learn why nothing was
    applied.  The refusal carries the digest of the declaration it was decided
    against, so a rejected apply is bound to the exact bytes that rejected it.
    """

    def __init__(self, hold: str, *, digest: str = "") -> None:
        super().__init__(hold)
        self.hold = hold
        self.digest = digest


@dataclass(frozen=True)
class OwnerSandboxTarget:
    """One allowed sandbox target, exactly as the owner declared it."""

    name: str
    adapter: str
    match: str
    production: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "adapter": self.adapter,
            "match": self.match,
            "production": self.production,
        }


def _is_production_looking(*fields: str) -> bool:
    """Whether any of ``fields`` names a production-looking surface.

    Each field is split on the delimiters a target name or adapter uses
    (``-``, ``_``, ``.``, ``/``, ``:``, ``@``, whitespace) and a marker matches
    only a whole segment, so ``prod-eu`` and ``live.example.dev`` refuse while
    ``product-preview`` does not.  The comparison is case-insensitive.
    """
    for field in fields:
        if not field:
            continue
        for segment in re.split(r"[-_./:@\s]+", field.lower()):
            if segment in _SANDBOX_PRODUCTION_MARKERS:
                return True
    return False


def _sandbox_target_from(entry: Any) -> OwnerSandboxTarget:
    """Build one :class:`OwnerSandboxTarget` from a declaration entry.

    A malformed entry declares nothing: it is refused under
    :data:`HOLD_SANDBOX_UNDECLARED` rather than read as a half-populated target.
    """
    if not isinstance(entry, Mapping):
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED)
    adapter = entry.get("adapter", "")
    match = entry.get("match", "")
    if not isinstance(adapter, str) or not isinstance(match, str):
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED)
    # The owner decision requires an *explicit* `production: false`: an omitted
    # or non-False value is not a declaration that the target is a sandbox, so
    # it is refused rather than defaulted.
    production = entry.get("production")
    if production is not False:
        raise SandboxDeclarationRefused(HOLD_SANDBOX_PRODUCTION_LOOKING)
    return OwnerSandboxTarget(
        name=name.strip(), adapter=adapter, match=match, production=False
    )


@dataclass(frozen=True)
class OwnerSandboxDeclaration:
    """A loaded owner sandbox declaration and the digest of the bytes read.

    ``declaration_path`` is the path the **loader** resolved from the caller's
    owner-state directory (never an argument), ``digest`` the SHA-256 of the
    exact bytes read, and ``targets`` the declared allow-list.  Every admission
    records the digest, so a later apply is bound to the declaration it was
    decided against.
    """

    declaration_path: str
    digest: str
    targets: tuple[OwnerSandboxTarget, ...]

    def resolve_target(
        self, target_name: str, *, adapter: str = ""
    ) -> OwnerSandboxTarget:
        """Admit ``target_name`` (optionally pinned to ``adapter``), or refuse.

        Refusals are named, never silent and never skipped:

        * a name (or adapter) the declaration does not list is refused under
          :data:`HOLD_SANDBOX_TARGET_UNLISTED` -- the tool never invents a
          target the owner did not declare.  The listing check comes first: an
          unlisted target is simply not declared, so it is never reported as a
          production surface the declaration licenses;
        * a *listed* target whose name, adapter or match pattern is
          production-looking is refused under
          :data:`HOLD_SANDBOX_PRODUCTION_LOOKING` -- the declaration is never a
          licence to reach production.
        """
        if not isinstance(target_name, str) or not target_name.strip():
            raise SandboxDeclarationRefused(
                HOLD_SANDBOX_TARGET_UNLISTED, digest=self.digest
            )
        declared = [t for t in self.targets if t.name == target_name]
        if adapter:
            declared = [t for t in declared if t.adapter == adapter]
        if not declared:
            raise SandboxDeclarationRefused(
                HOLD_SANDBOX_TARGET_UNLISTED, digest=self.digest
            )
        if _is_production_looking(target_name, adapter) or any(
            _is_production_looking(t.name, t.adapter, t.match) for t in declared
        ):
            raise SandboxDeclarationRefused(
                HOLD_SANDBOX_PRODUCTION_LOOKING, digest=self.digest
            )
        return declared[0]

    def recheck_digest(self) -> str:
        """Re-read the declaration and refuse if its bytes changed since load.

        The owner decision: "if the file changes between load and apply, the
        controller refuses".  A declaration that has been deleted since load is
        no longer the declaration that was recorded, so it is refused under the
        same named hold; the returned digest is the one *both* reads agreed on.
        """
        try:
            with open(self.declaration_path, "rb") as fh:
                current = fh.read()
        except OSError as exc:
            raise SandboxDeclarationRefused(
                HOLD_SANDBOX_DECLARATION_CHANGED, digest=self.digest
            ) from exc
        if hashlib.sha256(current).hexdigest() != self.digest:
            raise SandboxDeclarationRefused(
                HOLD_SANDBOX_DECLARATION_CHANGED, digest=self.digest
            )
        return self.digest

    def receipt_for(self, target_name: str, *, adapter: str = "") -> dict[str, Any]:
        """Resolve ``target_name`` and return the admission receipt to record.

        The digest is the *loaded* declaration's, re-checked against the bytes
        on disk as they are at admission time -- so the receipt a caller records
        is bound to the declaration the owner actually authored, not to one that
        was swapped underneath it.
        """
        target = self.resolve_target(target_name, adapter=adapter)
        digest = self.recheck_digest()
        return {
            "declaration_path": self.declaration_path,
            "declaration_digest": digest,
            "target": target.to_dict(),
        }


def load_owner_sandbox_declaration(owner_state_dir: Any) -> OwnerSandboxDeclaration:
    """Load the owner-authored sandbox declaration from durable owner state.

    ``owner_state_dir`` is a directory the **production caller resolves itself**
    from durable owner-authored state (the planning repository's initiative
    folder, ``skillweave-planning/.skillweave/planning/prds/<initiative>/``).  It
    is never an argv, tool or MCP argument, and no argument can name the
    declaration *file*: the loader appends only the pinned
    :data:`OWNER_SANDBOX_DECLARATION_FILENAME`, so a caller can name a directory
    of owner state but cannot point the loader at arbitrary bytes.  The SHA-256
    of the bytes actually read is returned with the parsed targets.

    Fail closed with a named hold: an absent or unreadable declaration is
    :data:`HOLD_SANDBOX_UNDECLARED` (a file that is not a declaration declares
    nothing); a target entry that is not an explicit ``production: false``
    sandbox target is :data:`HOLD_SANDBOX_PRODUCTION_LOOKING`.
    """
    import yaml

    declaration_path = os.path.join(
        os.fspath(owner_state_dir), OWNER_SANDBOX_DECLARATION_FILENAME
    )
    try:
        with open(declaration_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED) from exc
    digest = hashlib.sha256(data).hexdigest()
    try:
        document = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED) from exc
    if not isinstance(document, Mapping):
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED)
    raw_targets = document.get("sandbox_targets")
    if not isinstance(raw_targets, list):
        raise SandboxDeclarationRefused(HOLD_SANDBOX_UNDECLARED)
    targets = tuple(_sandbox_target_from(entry) for entry in raw_targets)
    return OwnerSandboxDeclaration(
        declaration_path=declaration_path, digest=digest, targets=targets
    )


#: The environment variable naming the durable *owner-state directory* the
#: production caller resolves the sandbox declaration in.  It names a directory,
#: never the declaration file itself: the loader appends the pinned file name, so
#: no value can point the loader at arbitrary bytes.  This is the caller's own
#: durable-state configuration (like ``SKILLWEAVE_REVIEWER_ROUTE``), not a
#: capability argument a task can supply.
_ENV_OWNER_STATE_DIR = "SKILLWEAVE_OWNER_STATE_DIR"

#: The planning-repository directory layout the owner decision pins, joined from
#: the repository root when the explicit owner-state directory is not set.  The
#: initiative folder is resolved under it, so no caller ever names a file.
_OWNER_STATE_ROOT_RELPATH = (".skillweave", "planning", "prds")


def _owner_state_root(project_root: Any) -> str:
    """The owner-state directory: the configured one, else the planning tree."""
    configured = os.environ.get(_ENV_OWNER_STATE_DIR, "").strip()
    if configured:
        return configured
    return os.path.join(os.fspath(project_root), *_OWNER_STATE_ROOT_RELPATH)


def resolve_sandbox_admission(
    target_name: str, *, adapter: str = "", project_root: str = ""
) -> dict[str, Any]:
    """Admit a sandbox target against durable owner-authored state.

    This is the **production caller** of :func:`load_owner_sandbox_declaration`
    (criterion 2's seam): it resolves the owner-state directory from its *own*
    durable configuration -- ``SKILLWEAVE_OWNER_STATE_DIR`` if set, else the
    planning tree under ``project_root`` (the repository root by default) --
    loads ``owner-sandbox.yaml``, records the SHA-256 of the bytes it read, and
    returns the admission receipt a caller binds to the sandboxed dispatch.
    Neither the declaration file nor a target is taken from an argument a task
    could set: only the target *name* to admit is passed, and it is checked
    against the owner's declaration.

    Every failure to admit is a named :class:`SandboxDeclarationRefused`:
    unlisted, undeclared, production-looking or changed.
    """
    if not project_root:
        from skillweave import __file__ as _pkg_file

        project_root = os.path.abspath(
            os.path.join(os.path.dirname(_pkg_file), os.pardir)
        )
    declaration = load_owner_sandbox_declaration(_owner_state_root(project_root))
    return declaration.receipt_for(target_name, adapter=adapter)


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
    resume: bool,
    principal: str,
    holder: str,
    worker: WorkerAdapter,
    reviewer: ReviewerAdapter,
    store: Any,
    gr: Any,
) -> DriveResult:
    """The state-driven, verdict-driven loop body (inside the lease guard)."""
    from skillweave.runtime.native_process import (
        INTEGRATION_CONTENT_TRANSACTION,
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

    # Step 0 -- restart (criterion 2).  A restarted controller adopts the
    # ordering-first in-flight intent whose owned identity still verifies
    # instead of replaying the predecessor's launch intent; with no resume flag
    # a live in-flight attempt is refused by name rather than replayed.  With no
    # in-flight attempt this is a no-op and the fresh dispatch below proceeds.
    adoption = _adopt_in_flight_attempt(svc, store, run_id=run_id, resume=resume)
    if adoption is not None and adoption.adopted:
        # The predecessor's launch intent is *adopted*, never replayed: the
        # drive takes ownership from the durable intent and writes no second
        # launch.  The persisted checkpoint is the durable statement of the
        # adoption (the new fencing token re-owns the in-flight intent).
        _persist_transition_checkpoint(
            svc, step=adoption.intent.step, intent_id=adoption.intent.intent_id
        )
        # Criterion 3: the recovered controller reads the *persisted*
        # harness-continuation decision through the run-scoped reader -- the
        # same decision the handoff tool wrote -- instead of deriving its own.
        # With no readable decision for this run the restart refuses with a
        # named continuation hold and never replays the launch intent.
        continuation = _read_continuation_decision(
            run_id=run_id,
            principal=principal,
            adopted_intent_id=adoption.intent.intent_id,
        )
        return DriveResult(
            run_id=run_id,
            task_id=task_id,
            attempt=adoption.intent.attempt,
            intent_id=adoption.intent.intent_id,
            fencing_token=int(token),
            steps=(),
            accepted=False,
            released_dependents=(),
            integration="none",
            reviews=(),
            adoption=adoption.to_dict(),
            continuation=continuation,
        )

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

    # Step 5 -- controlled integration.  The surface is discriminated from the
    # subject *kind*, never from the mere presence of a subject: a repository
    # subject integrates through Git, a non-repository subject through the
    # guarded content transaction, and a subject of unknown kind is refused by
    # name *before* any durable mutation (see _integration_mode_for_subject).
    # Integration is guarded from the reviewed state.
    integration_mode = _integration_mode_for_subject(execution.integration_subject)
    if integration_mode == INTEGRATION_CONTENT_TRANSACTION:
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
