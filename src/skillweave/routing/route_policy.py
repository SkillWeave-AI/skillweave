"""Owner-controlled route policy document.

The route policy is the *owner pin* that every observed route proof must be a
subset of.  It is loaded from configuration (``SKILLWEAVE_ROUTE_POLICY`` env
var or an explicit path) and is the single source of truth for:

* the approved route set (the pinned provider/model pairs),
* the required policy revision the proof must carry,
* the approved egress gateway host and port.

No provider literals, no port constant and no revision literal live in generic
Core source — the policy document supplies them all.

Schema version ``route-policy-v1``::

    {
        "schema": "route-policy-v1",
        "required_revision": "1.3.11",
        "approved_gateway": {
            "host": "127.0.0.1",
            "port": 8090
        },
        "approved_routes": [
            "byteplus/deepseek-v4-flash",
            ...
        ]
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional


#: Environment variable that names the route policy JSON file path.
_ENV_ROUTE_POLICY = "SKILLWEAVE_ROUTE_POLICY"


class RoutePolicyError(ValueError):
    """A route-policy document could not be loaded or validated."""

    def __init__(self, reason: str, path: str = ""):
        self.reason = reason
        self.path = path
        prefix = f"{path}: " if path else ""
        super().__init__(f"{prefix}route-policy rejected: {reason}")


@dataclass(frozen=True)
class RoutePolicy:
    """Owner-controlled route policy pin.

    Every field is the owner-configured value.  A route proof must be a
    subset of / match this policy — the proof never defines the policy.
    """

    approved_routes: frozenset[str] = field(default_factory=frozenset)
    required_revision: str = ""
    approved_gateway_host: str = "127.0.0.1"
    approved_gateway_port: int = 8090
    policy_revision: str = ""
    schema_version: str = "route-policy-v1"

    @classmethod
    def load(cls, path: str) -> "RoutePolicy":
        """Load and validate a route-policy document from a JSON file.

        Raises ``RoutePolicyError`` if the file cannot be read, the JSON
        is malformed, or the schema is invalid.
        """
        if not path:
            raise RoutePolicyError("empty path", path=path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise RoutePolicyError(f"file not found", path=path)
        except json.JSONDecodeError as exc:
            raise RoutePolicyError(f"invalid JSON: {exc}", path=path)
        except OSError as exc:
            raise RoutePolicyError(f"read error: {exc}", path=path)
        try:
            return cls.from_dict(data)
        except RoutePolicyError:
            raise
        except Exception as exc:
            raise RoutePolicyError(f"parse error: {exc}", path=path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutePolicy":
        """Validate and construct from a parsed dict."""
        schema = data.get("schema", "")
        if schema != "route-policy-v1":
            raise RoutePolicyError(
                f"unsupported schema {schema!r}; expected 'route-policy-v1'"
            )

        # Required revision
        required_revision = data.get("required_revision", "")
        if not isinstance(required_revision, str) or not required_revision:
            raise RoutePolicyError(
                f"required_revision must be a non-empty string, "
                f"got {required_revision!r}"
            )

        # Gateway
        gw = data.get("approved_gateway", {})
        if not isinstance(gw, dict):
            raise RoutePolicyError("approved_gateway must be a dict")
        host = gw.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host:
            raise RoutePolicyError(
                "approved_gateway.host must be a non-empty string"
            )
        port = gw.get("port", 8090)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise RoutePolicyError(
                f"approved_gateway.port must be an int 1-65535, got {port!r}"
            )

        # Approved routes
        raw_routes = data.get("approved_routes", [])
        if not isinstance(raw_routes, list):
            raise RoutePolicyError("approved_routes must be a list")
        if not raw_routes:
            raise RoutePolicyError("approved_routes must not be empty")
        for r in raw_routes:
            if not isinstance(r, str) or not r:
                raise RoutePolicyError(
                    f"route must be a non-empty string, got {r!r}"
                )
            if "/" not in r:
                raise RoutePolicyError(
                    f"route {r!r} is a bare model name without a provider "
                    "prefix; direct-provider bypass is not allowed"
                )
        approved = frozenset(raw_routes)

        policy_revision = data.get("policy_revision", required_revision)

        return cls(
            approved_routes=approved,
            required_revision=required_revision,
            approved_gateway_host=host,
            approved_gateway_port=port,
            policy_revision=policy_revision or required_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "schema": self.schema_version,
            "required_revision": self.required_revision,
            "policy_revision": self.policy_revision,
            "approved_gateway": {
                "host": self.approved_gateway_host,
                "port": self.approved_gateway_port,
            },
            "approved_routes": sorted(self.approved_routes),
        }

    def check_route_allowed(self, route: str) -> None:
        """Raise ``RoutePolicyError`` if *route* is not in the approved set."""
        if route not in self.approved_routes:
            raise RoutePolicyError(
                f"route {route!r} is not in the policy-approved set; "
                f"approved: {sorted(self.approved_routes)}"
            )


def resolve_route_policy(policy_path: str = "") -> Optional[RoutePolicy]:
    """Resolve the route policy from an explicit path or the environment.

    Returns ``None`` when no policy is configured (the lock is not active).
    Raises ``RoutePolicyError`` when the configured path cannot be loaded.
    """
    path = policy_path or os.environ.get(_ENV_ROUTE_POLICY, "")
    if not path:
        return None
    return RoutePolicy.load(path)


__all__ = [
    "RoutePolicy",
    "RoutePolicyError",
    "resolve_route_policy",
    "_ENV_ROUTE_POLICY",
]
