"""Route-proof document loader and schema validator.

A route-proof document is a JSON file that declares the approved gateway
endpoint, the set of approved provider routes, and the tier-to-route
binding.  It is the *observed current route proof* that criterion 3
requires: the approved set comes from this document, not from literals
in generic Core.

Schema version ``route-proof-v1``::

    {
        "schema": "route-proof-v1",
        "revision": "<policy revision>",
        "catalog_revision": "<catalog revision>",
        "approved_gateway": {
            "host": "127.0.0.1",
            "port": 8090
        },
        "approved_routes": [
            "byteplus/deepseek-v4-flash",
            ...
        ],
        "tier_routes": {
            "flash": ["byteplus/deepseek-v4-flash", ...],
            "pro": ["byteplus/deepseek-v4-pro", ...]
        }
    }

Every route in ``approved_routes`` must be a ``provider/model`` pair with
a ``/`` separator.  A route without a ``/`` (a bare model name) is a
direct-provider bypass and is refused at load time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional


class RouteProofError(ValueError):
    """A route-proof document could not be loaded or validated."""

    def __init__(self, reason: str, path: str = ""):
        self.reason = reason
        self.path = path
        prefix = f"{path}: " if path else ""
        super().__init__(f"{prefix}route-proof rejected: {reason}")


def _validate_route(route: str) -> None:
    """Validate that a route string is a ``provider/model`` pair.

    A route without ``/`` is a bare model name — a direct-provider
    bypass — and is refused at load time.
    """
    if not isinstance(route, str) or not route:
        raise RouteProofError(f"route must be a non-empty string, got {route!r}")
    if "/" not in route:
        raise RouteProofError(
            f"route {route!r} is a bare model name without a provider "
            "prefix; direct-provider bypass is not allowed"
        )
    parts = route.split("/", 1)
    if not parts[0] or not parts[1]:
        raise RouteProofError(
            f"route {route!r} has empty provider or model part"
        )


@dataclass(frozen=True)
class RouteProof:
    """A validated route-proof document.

    ``approved_routes`` is the set of provider route strings that are
    permitted (e.g. ``byteplus/deepseek-v4-flash``).  ``tier_routes``
    maps capability tier names to their permitted route subsets.
    ``catalog_revision`` and ``policy_revision`` record the document
    versions so callers can propagate them to ``RouteDecision`` records.
    """

    approved_gateway_host: str = "127.0.0.1"
    approved_gateway_port: int = 8090
    approved_routes: frozenset[str] = field(default_factory=frozenset)
    tier_routes: dict[str, frozenset[str]] = field(default_factory=dict)
    catalog_revision: str = ""
    policy_revision: str = ""
    schema_version: str = "route-proof-v1"

    @classmethod
    def load(cls, path: str) -> "RouteProof":
        """Load and validate a route-proof document from a JSON file.

        Raises ``RouteProofError`` if the file cannot be read, the JSON
        is malformed, or the schema is invalid.
        """
        if not path:
            raise RouteProofError("empty path", path=path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise RouteProofError(f"file not found", path=path)
        except json.JSONDecodeError as exc:
            raise RouteProofError(f"invalid JSON: {exc}", path=path)
        except OSError as exc:
            raise RouteProofError(f"read error: {exc}", path=path)
        try:
            return cls.from_dict(data)
        except RouteProofError:
            raise
        except Exception as exc:
            raise RouteProofError(f"parse error: {exc}", path=path)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouteProof":
        """Validate and construct from a parsed dict."""
        schema = data.get("schema", "")
        if schema != "route-proof-v1":
            raise RouteProofError(
                f"unsupported schema {schema!r}; expected 'route-proof-v1'"
            )

        # Gateway
        gw = data.get("approved_gateway", {})
        if not isinstance(gw, dict):
            raise RouteProofError("approved_gateway must be a dict")
        host = gw.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host:
            raise RouteProofError("approved_gateway.host must be a non-empty string")
        port = gw.get("port", 8090)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise RouteProofError(
                f"approved_gateway.port must be an int 1-65535, got {port!r}"
            )

        # Approved routes
        raw_routes = data.get("approved_routes", [])
        if not isinstance(raw_routes, list):
            raise RouteProofError("approved_routes must be a list")
        if not raw_routes:
            raise RouteProofError("approved_routes must not be empty")
        for r in raw_routes:
            _validate_route(r)
        approved = frozenset(raw_routes)

        # Tier routes
        raw_tiers = data.get("tier_routes", {})
        if not isinstance(raw_tiers, dict):
            raise RouteProofError("tier_routes must be a dict")
        tier_routes: dict[str, frozenset[str]] = {}
        for tier, routes in raw_tiers.items():
            if not isinstance(tier, str) or not tier:
                raise RouteProofError(f"tier_routes key must be a non-empty string, got {tier!r}")
            if not isinstance(routes, list):
                raise RouteProofError(
                    f"tier_routes[{tier!r}] must be a list, got {type(routes).__name__}"
                )
            if not routes:
                raise RouteProofError(f"tier_routes[{tier!r}] must not be empty")
            validated: list[str] = []
            for r in routes:
                _validate_route(r)
                validated.append(r)
            # Each tier route must be in the overall approved set
            for r in validated:
                if r not in approved:
                    raise RouteProofError(
                        f"tier_routes[{tier!r}] contains {r!r} which is not "
                        f"in approved_routes"
                    )
            tier_routes[tier] = frozenset(validated)

        revision = data.get("revision", "")
        catalog_revision = data.get("catalog_revision", revision)
        policy_revision = revision

        return cls(
            approved_gateway_host=host,
            approved_gateway_port=port,
            approved_routes=approved,
            tier_routes=tier_routes,
            catalog_revision=catalog_revision,
            policy_revision=policy_revision,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "schema": self.schema_version,
            "revision": self.policy_revision,
            "catalog_revision": self.catalog_revision,
            "approved_gateway": {
                "host": self.approved_gateway_host,
                "port": self.approved_gateway_port,
            },
            "approved_routes": sorted(self.approved_routes),
            "tier_routes": {
                tier: sorted(routes)
                for tier, routes in sorted(self.tier_routes.items())
            },
        }

    def resolve_allowed_routes_for_tier(self, tier: str) -> frozenset[str]:
        """Return the approved routes for a capability tier.

        Raises ``RouteProofError`` when the tier is not defined in the
        proof document.
        """
        routes = self.tier_routes.get(tier)
        if routes is None:
            raise RouteProofError(
                f"tier {tier!r} is not defined in route-proof; "
                f"known tiers: {sorted(self.tier_routes)}"
            )
        return routes

    def check_route_allowed(self, route: str) -> None:
        """Raise ``RouteProofError`` if *route* is not in the approved set.

        A bare model name (without ``provider/`` prefix) is always
        refused as a direct-provider bypass.
        """
        _validate_route(route)
        if route not in self.approved_routes:
            raise RouteProofError(
                f"route {route!r} is not in approved_routes; "
                f"approved: {sorted(self.approved_routes)}"
            )


__all__ = [
    "RouteProof",
    "RouteProofError",
]
