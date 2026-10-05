"""Bridge from SDK execution-envelope-v1 contract to ReservationEnvelope.

The ``execution-envelope-v1`` contract is the boundary artifact consumed
by Faigate at provider ingress (see ``reservation.py`` docstring, line 53).
This module defines the SDK contract shape and provides conversion to
:class:`~skillweave.runtime.reservation.ReservationEnvelope` so that
cumulative token, byte, cost, and request budgets are enforced atomically
through the :class:`~skillweave.runtime.reservation.ReservationStore`
admission gate.

SDK contract fields (execution-envelope-v1)
-------------------------------------------
* ``allowed_routes`` --- list of permitted provider route names.
* ``limit_policy`` --- one of ``"observe_adapt"`` (track but do not
  enforce) or ``"enforcing"`` (strict cumulative enforcement).
* ``max_tokens`` --- cumulative token upper bound (int, optional).
* ``max_bytes`` --- cumulative byte upper bound (int, optional).
* ``max_cost`` --- cumulative monetary cost ceiling (float, optional).
* ``max_requests`` --- cumulative request count (int, optional).
* ``max_deadline`` --- ISO instant deadline (str, optional).
* ``max_in_flight`` --- simultaneous in-flight slot limit (int, optional).
* ``max_per_request_bytes`` --- per-request byte ceiling (int, optional).
* ``max_per_request_tokens`` --- per-request token ceiling (int, optional).

The bridge converts ``observe_adapt`` policy to an unlimited envelope
(``None`` for cumulative bounds) and ``enforcing`` to the explicit finite
envelope.  This is the single module that maps the SDK contract to the
core reservation envelope --- any host that provides an execution-envelope-v1
contract MUST route through this bridge rather than constructing a
:class:`ReservationEnvelope` directly, so that the mapping is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from skillweave.runtime.reservation import ReservationEnvelope


@dataclass(frozen=True)
class ExecutionEnvelopeV1:
    """SDK execution-envelope-v1 contract shape.

    This is the boundary artifact consumed by Faigate at provider ingress.
    Every field is optional; ``None`` means "not specified" (no limit).

    ``allowed_routes`` lists the routes the provider is permitted to use
    for this execution.  An empty or missing list means all routes are
    permitted (subject to the host's own route policy).

    ``limit_policy`` governs whether cumulative limits are strictly
    enforced (``"enforcing"``) or only observed for adaptation
    (``"observe_adapt"``).  Under ``observe_adapt`` the envelope still
    reserves and tracks usage but never refuses admission based on a
    cumulative cap --- the adapter observes and the host adapts.
    """

    allowed_routes: Optional[list[str]] = None
    limit_policy: str = "enforcing"
    max_tokens: Optional[int] = None
    max_bytes: Optional[int] = None
    max_cost: Optional[float] = None
    max_requests: Optional[int] = None
    max_deadline: Optional[str] = None
    max_in_flight: Optional[int] = 3
    max_per_request_bytes: Optional[int] = None
    max_per_request_tokens: Optional[int] = None

    def to_reservation_envelope(self) -> ReservationEnvelope:
        """Convert this SDK contract to a :class:`ReservationEnvelope`.

        Under ``observe_adapt`` policy, all cumulative limits are set to
        ``None`` (unlimited) --- the adapter observes but never enforces
        hard caps.  Per-request limits and in-flight slots still apply
        because they govern individual request safety, not cumulative
        budget.

        Under ``enforcing`` policy, all specified limits are passed
        through as finite bounds.
        """
        if self.limit_policy == "observe_adapt":
            return ReservationEnvelope(
                max_in_flight=self.max_in_flight,
                max_per_request_bytes=self.max_per_request_bytes,
                max_per_request_tokens=self.max_per_request_tokens,
            )
        return ReservationEnvelope(
            max_cost=self.max_cost,
            max_tokens=self.max_tokens,
            max_bytes=self.max_bytes,
            max_requests=self.max_requests,
            max_deadline=self.max_deadline,
            max_in_flight=self.max_in_flight,
            max_per_request_bytes=self.max_per_request_bytes,
            max_per_request_tokens=self.max_per_request_tokens,
        )

    def conservative_per_request_bounds(self) -> dict[str, Any]:
        """Return conservative per-request upper bounds for ``reserve()``.

        These are the pessimistic estimates of what a single request
        might consume, used as ``reserved_bytes`` / ``reserved_tokens``
        in the
        :meth:`~skillweave.runtime.reservation.ReservationStore.reserve`
        call.  When the envelope has no per-request limit for a dimension,
        returns ``None`` for that dimension (unknown --- reserve at 0 so
        the cumulative check does not pre-commit more than it must).

        Returns a dict with keys ``reserved_bytes`` and ``reserved_tokens``.
        """
        return {
            "reserved_bytes": self.max_per_request_bytes,
            "reserved_tokens": (
                float(self.max_per_request_tokens)
                if self.max_per_request_tokens is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionEnvelopeV1":
        """Create from a parsed JSON/dict (e.g. deserialised contract)."""
        return cls(
            allowed_routes=data.get("allowed_routes"),
            limit_policy=data.get("limit_policy", "enforcing"),
            max_tokens=_safe_int(data, "max_tokens"),
            max_bytes=_safe_int(data, "max_bytes"),
            max_cost=_safe_float(data, "max_cost"),
            max_requests=_safe_int(data, "max_requests"),
            max_deadline=data.get("max_deadline"),
            max_in_flight=_safe_int(data, "max_in_flight", default=3),
            max_per_request_bytes=_safe_int(data, "max_per_request_bytes"),
            max_per_request_tokens=_safe_int(data, "max_per_request_tokens"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "allowed_routes": self.allowed_routes,
            "limit_policy": self.limit_policy,
            "max_tokens": self.max_tokens,
            "max_bytes": self.max_bytes,
            "max_cost": self.max_cost,
            "max_requests": self.max_requests,
            "max_deadline": self.max_deadline,
            "max_in_flight": self.max_in_flight,
            "max_per_request_bytes": self.max_per_request_bytes,
            "max_per_request_tokens": self.max_per_request_tokens,
        }


def _safe_int(data: dict, key: str, default: Any = None) -> Optional[int]:
    v = data.get(key)
    if v is None:
        return default
    return int(v)


def _safe_float(data: dict, key: str) -> Optional[float]:
    v = data.get(key)
    if v is None:
        return None
    return float(v)
