"""Host-owned authority service (SW-1510-IDENTITY).

This module is the *authentication* seam the SDK deliberately refuses to be.
``skillweave_sdk.validator.AuthenticatedServiceContext`` is a shape check, not
authentication; it documents that the runtime which owns the seam must build
that context from a real credential verification result, derive none of its
fields from untrusted wire data, and call the SDK verifiers at the actual
production boundary. :class:`HostAuthorityService` is that seam for Core's run
application path.

What this service does
----------------------

* :meth:`HostAuthorityService.verify_credential` is the only way to turn an
  opaque bearer token into an authenticated :class:`VerifiedCredential`. The
  service owns the credential registry; a caller cannot mint one, and the token
  is looked up by a constant-time comparison against stored *digests*, never
  compared to a caller-supplied principal/role string. There is no API that
  accepts ``auth_real``/``obs_real`` style strings as proof of anything.
* The service issues attempt identities and credentials
  (:meth:`issue_attempt_credential`) bound to an exact (run_id, task_id,
  attempt) and to a scope digest, and issues the scope-bound policy decision
  (:meth:`issue_authority_decision`) that alone admits a receipt or a gate
  write. Tokens, handles and decision digests live only in service memory; they
  are never written to the wire record a worker can read.
* :meth:`HostAuthorityService.ingest_evidence_receipt` is the *actual* receipt
  boundary. It authenticates the presented credential, builds the trusted
  context strictly from the verification result (never from the receipt), calls
  the SDK's :func:`verify_evidence_receipt_authority` and then applies the
  semantic artifact checks a signature cannot supply: bound principal/issuer,
  exact (run_id, task_id, attempt), exact artifact and subject digests, a
  freshly resolved raw artifact whose bytes hash to the claimed digest, an
  actually executed independent review, and no worker self-attestation.

What remains descriptive policy
-------------------------------

``skillweave.runtime.authority.AuthorityGuard`` and ``ROLE_CAPABILITY_MATRIX``
(and ``gates.attestation``) are preserved unchanged for backward compatibility.
They are *descriptive policy helpers*, not identity proof: an approval carrying
``actor="auth_real", role="reviewer"`` is an inert data record here, not an
authentication. Protected native task acceptance goes through this service.

Threat model
------------

The adversary is a delegated worker/harness (and any caller of the protected
API), not a malicious host administrator. A host process that can read this
module's memory or edit the credential registry can defeat any in-process
boundary; that is out of scope. File-mode separation is an additional,
best-effort layer against a same-user child, and the process launcher refuses
to start a worker unless a real platform isolation adapter is available (or the
worker is explicitly denied the protected stores), so no quiet unsandboxed
launch occurs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from skillweave_sdk.validator import (
    AuthenticatedServiceContext,
    AuthorityBoundaryError,
    REVIEWER_ROLES,
    verify_evidence_receipt_authority,
)

#: Roles the host service will ever issue. ``worker`` is explicitly absent: a
#: worker credential is authenticated but carries no approving role and can
#: never satisfy a reviewer/observer/controller decision.
ISSUABLE_ROLES: frozenset[str] = frozenset(
    {"observer", "reviewer", "controller", "integrator", "run-service"}
)

#: Roles that may independently review an attempt. Structurally disjoint from
#: the worker principal: a review cannot be performed by the attempt's worker.
REVIEW_CAPABLE_ROLES: frozenset[str] = frozenset({"reviewer", "observer"})

#: Roles that may admit task/dependency state at a gate. Never a worker.
GATE_CAPABLE_ROLES: frozenset[str] = frozenset({"controller", "integrator"})

_DIGEST_RE = frozenset("0123456789abcdef")


class IdentityBoundaryError(AuthorityBoundaryError):
    """A credential, attempt binding or artifact failed identity validation."""


class IdentityCapabilityError(RuntimeError):
    """A required isolation capability is unavailable; the path fails closed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_full_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _DIGEST_RE
    )


def _require_sha(value: Any, field_name: str) -> str:
    if not _is_full_sha(value):
        raise IdentityBoundaryError(f"{field_name} must be a full 64-hex sha256")
    return value


def _token_digest(token: str) -> str:
    """A one-way digest of an opaque token.

    Tokens are high-entropy (``secrets.token_urlsafe``), so an unsalted SHA-256
    is a sound at-rest representation: it is not brute-forceable and it lets the
    registry store no recoverable secret. Comparison is constant-time.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Credential registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CredentialRecord:
    """One host-issued credential. Never handed to a caller in this shape."""

    token_digest: str
    principal: str
    roles: frozenset[str]
    service_principal: str
    issued_at: str
    not_before: str
    expires_at: Optional[str]
    revoked: bool = False
    scope_digest: Optional[str] = None
    attempt_key: Optional[tuple[str, str, int]] = None
    capability: Optional[str] = None

    def is_live(self, at: str) -> bool:
        if self.revoked:
            return False
        if at < self.not_before:
            return False
        if self.expires_at is not None and at >= self.expires_at:
            return False
        return True


@dataclass
class VerifiedCredential:
    """The result of an actual credential verification, not a wire claim.

    Only :meth:`HostAuthorityService.verify_credential` constructs this. Its
    fields come from the host registry, never from caller-supplied JSON.
    """

    principal: str
    roles: frozenset[str]
    service_principal: str
    issued_at: str
    expires_at: Optional[str]
    scope_digest: Optional[str]
    attempt_key: Optional[tuple[str, str, int]]
    capability: Optional[str]
    #: The host service's in-memory receipt-signing key, carried only on a
    #: host-verified credential so signing does not depend on a guessable public
    #: string. It is never serialized and never placed on a wire record.
    signing_secret: bytes = field(default=b"", repr=False, compare=False)

    def has_role(self, role: str) -> bool:
        return role in self.roles


class CredentialRegistry:
    """Host-owned store of issued credentials and admitted decisions.

    Deliberately not exposed on the wire and not serialized. A worker process
    has no reference to an instance of this class; the only way to obtain a
    :class:`VerifiedCredential` is to present a live token to the owning
    :class:`HostAuthorityService`.
    """

    def __init__(self, service_principal: str = "skillweave-host-service"):
        self.service_principal = service_principal
        #: The host service's receipt-signing key. Generated in-memory at
        #: construction and never written to any wire record or disk path, so a
        #: worker has no way to obtain it: it cannot forge a receipt signature.
        self._signing_key: bytes = secrets.token_bytes(32)
        self._by_digest: dict[str, _CredentialRecord] = {}
        self._issued_handle_ids: set[str] = set()
        self._admitted_decisions: set[str] = set()
        self._attempt_credentials: dict[tuple[str, str, int], set[str]] = {}

    # -- issuing ------------------------------------------------------------

    def issue(
        self,
        *,
        principal: str,
        roles: Iterable[str],
        ttl_seconds: Optional[float] = None,
        scope_digest: Optional[str] = None,
        attempt_key: Optional[tuple[str, str, int]] = None,
        capability: Optional[str] = None,
    ) -> str:
        """Mint an opaque token. Returns the secret; only its digest is kept."""
        role_set = frozenset(roles)
        unknown = role_set - ISSUABLE_ROLES
        if unknown:
            raise IdentityBoundaryError(
                f"host service will not issue role(s) {sorted(unknown)}; "
                f"issuable roles are {sorted(ISSUABLE_ROLES)}"
            )
        if not role_set:
            raise IdentityBoundaryError("a credential must carry at least one role")
        if not isinstance(principal, str) or not principal:
            raise IdentityBoundaryError("a credential requires a principal")
        token = secrets.token_urlsafe(32)
        issued = _now()
        expires = None
        if ttl_seconds is not None:
            expires = _add_seconds(issued, ttl_seconds)
        self._by_digest[_token_digest(token)] = _CredentialRecord(
            token_digest=_token_digest(token),
            principal=principal,
            roles=role_set,
            service_principal=self.service_principal,
            issued_at=issued,
            not_before=issued,
            expires_at=expires,
            scope_digest=scope_digest,
            attempt_key=attempt_key,
            capability=capability,
        )
        return token

    def issue_worker(
        self,
        *,
        principal: str,
        ttl_seconds: Optional[float],
        scope_digest: str,
        attempt_key: tuple[str, str, int],
    ) -> str:
        """Mint a worker credential: authenticated, scope-bound, non-approving.

        The worker role is a descriptive label only; it is deliberately outside
        :data:`ISSUABLE_ROLES` and outside the SDK's reviewer role set, so this
        credential can never satisfy a reviewer/observer/controller decision.
        """
        if not isinstance(principal, str) or not principal:
            raise IdentityBoundaryError("a worker credential requires a principal")
        token = secrets.token_urlsafe(32)
        issued = _now()
        expires = None
        if ttl_seconds is not None:
            expires = _add_seconds(issued, ttl_seconds)
        self._by_digest[_token_digest(token)] = _CredentialRecord(
            token_digest=_token_digest(token),
            principal=principal,
            roles=frozenset({"worker"}),
            service_principal=self.service_principal,
            issued_at=issued,
            not_before=issued,
            expires_at=expires,
            scope_digest=scope_digest,
            attempt_key=attempt_key,
            capability="execute-attempt",
        )
        return token

    def register_handle(self, handle_id: str) -> None:
        if not isinstance(handle_id, str) or not handle_id:
            raise IdentityBoundaryError("handle id must be a non-empty string")
        self._issued_handle_ids.add(handle_id)

    def admit_decision(self, digest: str) -> None:
        _require_sha(digest, "authority decision digest")
        self._admitted_decisions.add(digest)

    # -- verifying ----------------------------------------------------------

    def lookup(self, token: str, at: Optional[str] = None) -> Optional[_CredentialRecord]:
        if not isinstance(token, str) or not token:
            return None
        digest = _token_digest(token)
        record = None
        # Constant-time scan: compare every digest so a timing observer cannot
        # learn whether a near-miss prefix exists.
        for stored, candidate in self._by_digest.items():
            if hmac.compare_digest(stored, digest):
                record = candidate
        if record is None:
            return None
        if not record.is_live(at or _now()):
            return None
        return record

    def issued_handle_ids(self) -> frozenset[str]:
        return frozenset(self._issued_handle_ids)

    def admitted_decisions(self) -> frozenset[str]:
        return frozenset(self._admitted_decisions)

    def revoke_principal(self, principal: str) -> int:
        """Revoke every live credential for *principal*. Returns the count."""
        count = 0
        for digest, record in list(self._by_digest.items()):
            if record.principal == principal and not record.revoked:
                self._by_digest[digest] = _CredentialRecord(
                    **{**_record_fields(record), "revoked": True}
                )
                count += 1
        return count

    def expire(self, token: str) -> None:
        """Test seam: force a token past its expiry without waiting."""
        digest = _token_digest(token)
        record = self._by_digest.get(digest)
        if record is not None:
            self._by_digest[digest] = _CredentialRecord(
                **{**_record_fields(record), "expires_at": _past()}
            )

    def revoke_token(self, token: str) -> None:
        """Test seam: revoke exactly one token."""
        digest = _token_digest(token)
        record = self._by_digest.get(digest)
        if record is not None:
            self._by_digest[digest] = _CredentialRecord(
                **{**_record_fields(record), "revoked": True}
            )

    def register_attempt_credential(self, attempt_key: tuple[str, str, int], token: str) -> None:
        self._attempt_credentials.setdefault(attempt_key, set()).add(_token_digest(token))

    def attempt_credential_digests(self, attempt_key: tuple[str, str, int]) -> frozenset[str]:
        return frozenset(self._attempt_credentials.get(attempt_key, set()))


def _record_fields(record: _CredentialRecord) -> dict[str, Any]:
    return {
        "token_digest": record.token_digest,
        "principal": record.principal,
        "roles": record.roles,
        "service_principal": record.service_principal,
        "issued_at": record.issued_at,
        "not_before": record.not_before,
        "expires_at": record.expires_at,
        "revoked": record.revoked,
        "scope_digest": record.scope_digest,
        "attempt_key": record.attempt_key,
        "capability": record.capability,
    }


def _add_seconds(iso: str, seconds: float) -> str:
    from datetime import timedelta

    base = datetime.fromisoformat(iso)
    return (base + timedelta(seconds=seconds)).isoformat()


def _past() -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


# ---------------------------------------------------------------------------
# Scope / brief / artifact digests
# ---------------------------------------------------------------------------


def canonical_digest(obj: Any) -> str:
    """A stable sha256 over canonical JSON of *obj*."""
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def digest_scope(scope: Mapping[str, Any]) -> str:
    """Digest an exact write scope (paths + permissions), order-independent."""
    return canonical_digest(scope)


def digest_brief(brief: Mapping[str, Any]) -> str:
    """Digest a task brief. Canonical JSON, so key order never matters."""
    return canonical_digest(brief)


def digest_candidate_map(candidate_sha_map: Mapping[str, str]) -> str:
    """Digest a complete candidate SHA map (task_id -> sha)."""
    return canonical_digest(dict(candidate_sha_map))


# ---------------------------------------------------------------------------
# The authority service
# ---------------------------------------------------------------------------


@dataclass
class AuthorityDecision:
    """A service-issued, scope-bound policy decision.

    The decision digest is the only value a caller presents; the binding facts
    (principal, attempt, brief/scope/policy digests) live in the registry, never
    in the receipt. A caller that fabricates a digest it was never issued is
    rejected before any mutation.
    """

    decision_digest: str
    principal: str
    run_id: str
    task_id: str
    attempt: int
    scope_digest: str
    brief_digest: str
    policy_digest: str
    issued_at: str


@dataclass
class HostAuthorityService:
    """The host-owned authentication and admission boundary.

    Construct once per host process. Workers receive only an opaque token they
    must present back to a method on this service; they never receive the
    service, the registry, or a decision digest.
    """

    registry: CredentialRegistry = field(default_factory=CredentialRegistry)
    isolation: Any = None  # optional WorkerIsolationAdapter (see worker_launcher.py)

    # -- credential verification -------------------------------------------

    def verify_credential(
        self,
        token: str,
        *,
        required_capability: Optional[str] = None,
        required_attempt: Optional[tuple[str, str, int]] = None,
    ) -> VerifiedCredential:
        """The only path from an opaque token to an authenticated principal.

        A missing, forged, replayed-after-revocation or expired token, a token
        that lacks a required capability, or a token issued for a different
        attempt all fail here, before any mutation.
        """
        record = self.registry.lookup(token)
        if record is None:
            raise IdentityBoundaryError("credential is unknown, expired or revoked")
        if required_capability is not None and record.capability != required_capability:
            raise IdentityBoundaryError(
                f"credential for {record.principal!r} lacks capability "
                f"{required_capability!r}"
            )
        if required_attempt is not None and record.attempt_key != required_attempt:
            raise IdentityBoundaryError(
                "credential attempt binding does not match the target attempt"
            )
        # Only a review-capable credential may carry the host signing key. A
        # worker's own (non-approving) credential is verified through this same
        # path, so gating here is what stops a delegated worker from obtaining
        # the key and forging an authentic-looking receipt signature.
        signing_secret = (
            self.registry._signing_key if record.roles & REVIEW_CAPABLE_ROLES else b""
        )
        return VerifiedCredential(
            principal=record.principal,
            roles=record.roles,
            service_principal=record.service_principal,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            scope_digest=record.scope_digest,
            attempt_key=record.attempt_key,
            capability=record.capability,
            signing_secret=signing_secret,
        )

    # -- attempt identity / scope-bound credential issuing ------------------

    def issue_attempt_credential(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        scope: Mapping[str, Any],
        ttl_seconds: Optional[float] = None,
    ) -> str:
        """Issue the worker its own non-approving, scope-bound credential.

        The credential carries no approving role and is bound to the exact
        attempt and scope digest. It is recorded as *the* worker credential for
        that attempt, so the service can later refuse self-review.
        """
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise IdentityBoundaryError("attempt must be a positive integer")
        scope_digest = digest_scope(scope)
        principal = f"worker:{run_id}:{task_id}:{attempt}"
        attempt_key = (run_id, task_id, attempt)
        token = self.registry.issue_worker(
            principal=principal,
            ttl_seconds=ttl_seconds,
            scope_digest=scope_digest,
            attempt_key=attempt_key,
        )
        # The worker token is authenticated but carries no approving role: it
        # is recorded as *the* worker credential for this attempt so the service
        # can later refuse self-review.
        self.registry.register_attempt_credential(attempt_key, token)
        return token

    def issue_authority_decision(
        self,
        *,
        principal: str,
        run_id: str,
        task_id: str,
        attempt: int,
        scope: Mapping[str, Any],
        brief: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> AuthorityDecision:
        """Admit a scope-bound decision that alone authorizes a gate/receipt.

        The service records the decision and its exact bindings; the returned
        digest is what a trusted *service-side* caller presents. It is never
        written into a worker-visible record.
        """
        scope_digest = digest_scope(scope)
        brief_digest = digest_brief(brief)
        policy_digest = canonical_digest(policy)
        body = {
            "principal": principal,
            "run_id": run_id,
            "task_id": task_id,
            "attempt": attempt,
            "scope_digest": scope_digest,
            "brief_digest": brief_digest,
            "policy_digest": policy_digest,
            "issued_at": _now(),
            "nonce": secrets.token_hex(8),
        }
        decision_digest = canonical_digest(body)
        self.registry.admit_decision(decision_digest)
        return AuthorityDecision(
            decision_digest=decision_digest,
            principal=principal,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            scope_digest=scope_digest,
            brief_digest=brief_digest,
            policy_digest=policy_digest,
            issued_at=body["issued_at"],
        )

    # -- trusted context construction --------------------------------------

    def build_context(self, credential: VerifiedCredential) -> AuthenticatedServiceContext:
        """Build the SDK trusted context strictly from a verification result.

        Every field derives from *credential* (host-verified), never from an
        untrusted instance. The SDK verifiers consume this; the SDK still cannot
        prove authentication, so this method is the only sanctioned producer.
        """
        return AuthenticatedServiceContext(
            principal=credential.principal,
            roles=credential.roles,
            service_principal=credential.service_principal,
            issued_handle_ids=self.registry.issued_handle_ids(),
            authority_decision_digests=self.registry.admitted_decisions(),
            worker_principal=(
                self.worker_principal_for(credential.attempt_key)
                if credential.attempt_key is not None
                else None
            ),
        )

    def worker_principal_for(self, attempt_key: Optional[tuple[str, str, int]]) -> Optional[str]:
        if attempt_key is None:
            return None
        run_id, task_id, attempt = attempt_key
        return f"worker:{run_id}:{task_id}:{attempt}"

    # -- the actual receipt boundary ---------------------------------------

    def ingest_evidence_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        worker_token: str,
        raw_artifacts: Any,
        observed_review: Optional["ObservedReview"] = None,
        candidate_sha_map: Optional[Mapping[str, str]] = None,
        known_candidate_sha_map: Optional[Mapping[str, str]] = None,
    ) -> "IngestedReceipt":
        """Authenticate, authorize and semantically check a wire receipt.

        This is the production ingestion path. Order is deliberate:

        1. Authenticate the *presenting* credential (not any string inside the
           receipt). This is the reviewer/observer proving who it is.
        2. Build the trusted context from that verification result and call the
           SDK :func:`verify_evidence_receipt_authority` — pure authority, no
           semantic truth yet.
        3. Apply the semantic artifact contract: bound principal/issuer, exact
           attempt, exact artifact/subject digests, raw bytes that hash to the
           claimed digest, an actually executed independent review, and no
           worker self-attestation.

        A correctly signed receipt whose text claims PASS but whose raw bytes
        are missing or whose digest does not match is rejected here.
        """
        credential = self.verify_credential(worker_token)
        if not (credential.roles & REVIEWER_ROLES):
            raise IdentityBoundaryError(
                "presenting credential holds no reviewer/observer role"
            )
        if credential.capability != "attest-receipt":
            raise IdentityBoundaryError(
                "presenting credential lacks capability 'attest-receipt'"
            )
        attempt_key = credential.attempt_key
        if attempt_key is None:
            raise IdentityBoundaryError("reviewer credential is not bound to an attempt")

        context = self.build_context(credential)
        # SDK pure authority (shape + admitted decision + reviewer role).
        verify_evidence_receipt_authority(receipt, context)

        # --- semantic artifact contract (what a signature cannot supply) ---
        if not isinstance(receipt, Mapping):
            raise IdentityBoundaryError("receipt must be a mapping")
        run_id, task_id, attempt = attempt_key

        if receipt.get("run_id") != run_id:
            raise IdentityBoundaryError(
                "receipt run_id does not match the authenticated attempt"
            )
        if receipt.get("task_id") != task_id:
            raise IdentityBoundaryError(
                "receipt task_id does not match the authenticated attempt"
            )
        if receipt.get("attempt") != attempt:
            raise IdentityBoundaryError(
                "receipt attempt does not match the authenticated attempt"
            )

        issuer = receipt.get("issuer")
        if not isinstance(issuer, str) or issuer != credential.service_principal:
            raise IdentityBoundaryError(
                "receipt issuer is not the authenticated host service"
            )

        worker_principal = self.worker_principal_for(attempt_key)
        if receipt.get("principal") == worker_principal:
            raise IdentityBoundaryError("a worker cannot attest its own attempt")

        # The signature must actually be over the receipt body, and the raw
        # artifact must resolve to bytes that hash to the claimed digest.
        claimed_artifact = _require_sha(receipt.get("artifact_digest"), "artifact_digest")
        _require_sha(receipt.get("subject_digest"), "subject_digest")
        signature = receipt.get("signature")
        expected_signature = sign_receipt_body(receipt, credential)
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, expected_signature
        ):
            raise IdentityBoundaryError(
                "receipt signature does not verify over the receipt body"
            )
        # Resolve the exact raw bytes; a missing or mutated artifact fails.
        try:
            resolved = raw_artifacts.resolve(claimed_artifact)
        except Exception as exc:  # ArtifactIntegrityError and friends
            raise IdentityBoundaryError(
                f"receipt artifact does not resolve to raw bytes: {exc}"
            ) from exc
        if hashlib.sha256(resolved).hexdigest() != claimed_artifact:
            raise IdentityBoundaryError("resolved raw bytes do not match artifact_digest")

        # An independent review must have actually executed, not merely be
        # asserted by a signed string.
        if observed_review is None or not observed_review.independent:
            raise IdentityBoundaryError(
                "no independently observed review execution backs this receipt"
            )
        if observed_review.artifact_digest != claimed_artifact:
            raise IdentityBoundaryError(
                "observed review did not judge the receipt's artifact"
            )
        if observed_review.attempt_key != attempt_key:
            raise IdentityBoundaryError(
                "observed review was executed for a different attempt"
            )
        if observed_review.reviewer_principal == worker_principal:
            raise IdentityBoundaryError("the observed review was a self-review")

        # Semantic checks must be real and passing; a signed PASS with no
        # executed semantic check is not acceptance evidence.
        checks = receipt.get("semantic_checks")
        if not isinstance(checks, list) or not checks:
            raise IdentityBoundaryError("receipt carries no semantic checks")
        failing = [c for c in checks if not isinstance(c, Mapping) or c.get("result") != "pass"]
        if failing:
            raise IdentityBoundaryError("receipt carries a non-passing semantic check")

        # Candidate binding: the complete candidate SHA map must match exactly
        # when the host has a known map (replay/stale-candidate refusal).
        if known_candidate_sha_map is not None:
            if candidate_sha_map is None:
                raise IdentityBoundaryError("receipt is not bound to a candidate SHA map")
            if digest_candidate_map(candidate_sha_map) != digest_candidate_map(
                known_candidate_sha_map
            ):
                raise IdentityBoundaryError(
                    "candidate SHA map is stale or substituted"
                )

        return IngestedReceipt(
            receipt=dict(receipt),
            reviewer_principal=credential.principal,
            attempt_key=attempt_key,
            artifact_digest=claimed_artifact,
            expected_signature=expected_signature,
            observed_review=observed_review,
            received_at=_now(),
        )


# ---------------------------------------------------------------------------
# Receipt signing (host service only)
# ---------------------------------------------------------------------------


def sign_receipt_body(receipt: Mapping[str, Any], credential: VerifiedCredential) -> str:
    """Sign the *semantic* body of a receipt with the host service secret.

    Only the host service can produce this signature: it is an HMAC over the
    canonical receipt body keyed by the service's in-memory signing secret,
    which is carried only by a host-verified *review-capable* credential — never
    by a worker's own credential, and never written to any wire record or disk
    path. A worker therefore has no way to reproduce the key, and a
    worker-produced fake receipt fails its signature step even before the
    semantic checks run (fail closed if no secret is present).
    """
    key = credential.signing_secret
    if not key:
        raise IdentityBoundaryError(
            "credential carries no receipt-signing authority; refusing to sign "
            f"a receipt as principal {credential.principal!r}"
        )
    body = {
        k: v
        for k, v in sorted(receipt.items())
        if k != "signature"
    }
    material = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ObservedReview:
    """A review actually executed and observed by the host service.

    A signed string claiming a review is not this. The host service constructs
    this only after it has run (or observed) an independent reviewer evaluating
    the exact attempt and artifact.
    """

    reviewer_principal: str
    attempt_key: tuple[str, str, int]
    artifact_digest: str
    verdict: str
    independent: bool
    observed_at: str

    def __post_init__(self) -> None:
        if self.verdict != "pass":
            raise IdentityBoundaryError("observed review did not return pass")
        if not self.independent:
            raise IdentityBoundaryError("observed review is not independent")
        if self.reviewer_principal.startswith("worker:"):
            raise IdentityBoundaryError("observed review is a worker self-review")


@dataclass(frozen=True)
class IngestedReceipt:
    """The admissible result of :meth:`ingest_evidence_receipt`."""

    receipt: dict[str, Any]
    reviewer_principal: str
    attempt_key: tuple[str, str, int]
    artifact_digest: str
    expected_signature: str
    observed_review: ObservedReview
    received_at: str


@dataclass
class GateAdmission:
    """The result of admitting a gate: a controller decision, not a worker claim."""

    run_id: str
    task_id: str
    attempt: int
    gate_state: str
    admitted_by: str
    decision_digest: str
    receipt_artifact_digest: str
    admitted_at: str


def admit_gate(
    service: HostAuthorityService,
    *,
    controller_token: str,
    ingested: IngestedReceipt,
    decision: AuthorityDecision,
    gate_state: str,
) -> GateAdmission:
    """Admit task/dependency state at the gate boundary.

    Only a controller/integrator credential may admit, and only when the
    service-issued decision matches this exact attempt and the ingested
    receipt's artifact. A worker cannot reach here: its credential lacks the
    gate capability and no decision was issued to it.
    """
    credential = service.verify_credential(controller_token)
    if not (credential.roles & GATE_CAPABLE_ROLES):
        raise IdentityBoundaryError("credential holds no controller/integrator role")
    if credential.capability != "admit-gate":
        raise IdentityBoundaryError("credential lacks capability 'admit-gate'")
    if decision.decision_digest not in service.registry.admitted_decisions():
        raise IdentityBoundaryError("gate decision was not issued by this service")
    if (decision.run_id, decision.task_id, decision.attempt) != ingested.attempt_key:
        raise IdentityBoundaryError("gate decision is bound to a different attempt")
    return GateAdmission(
        run_id=ingested.attempt_key[0],
        task_id=ingested.attempt_key[1],
        attempt=ingested.attempt_key[2],
        gate_state=gate_state,
        admitted_by=credential.principal,
        decision_digest=decision.decision_digest,
        receipt_artifact_digest=ingested.artifact_digest,
        admitted_at=_now(),
    )


__all__ = [
    "ISSUABLE_ROLES",
    "REVIEW_CAPABLE_ROLES",
    "GATE_CAPABLE_ROLES",
    "IdentityBoundaryError",
    "IdentityCapabilityError",
    "CredentialRegistry",
    "VerifiedCredential",
    "AuthorityDecision",
    "HostAuthorityService",
    "ObservedReview",
    "IngestedReceipt",
    "GateAdmission",
    "admit_gate",
    "sign_receipt_body",
    "canonical_digest",
    "digest_scope",
    "digest_brief",
    "digest_candidate_map",
]
