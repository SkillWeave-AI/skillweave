"""The source-owned Discovery binding descriptor (SW-ADAPT-001, AC4/AC8).

A host that wants Discovery research to run at the phase boundary materializes
this descriptor into its own project as::

    .skillweave/hooks/discovery-pre.yaml

The binding is *data*, not a script in a foreign home: it names the source-owned
:class:`~skillweave.discovery.lifecycle_hook.DiscoveryResearchHook` as a
``type=python`` hook, so the engine imports the packaged class straight from the
installed SDK/Core -- the same wheel that ships the registry.

``discovery-pre`` is the ``pre_discovery`` injection point, which the free tier
permits, so Discovery research runs without a Studio license.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .lifecycle_hook import DISCOVERY_HOOK_MODULE, DEFAULT_DISCOVERY_QUERY

__all__ = [
    "DISCOVERY_BINDING_FILENAME",
    "DISCOVERY_HOOK_NAME",
    "DISCOVERY_BINDING_PATH",
    "discovery_binding_dict",
    "discovery_binding_yaml",
    "write_discovery_binding",
]

#: Where a project places the binding, relative to its root.
DISCOVERY_BINDING_PATH = Path(".skillweave") / "hooks" / "discovery-pre.yaml"
DISCOVERY_BINDING_FILENAME = "discovery-pre.yaml"
DISCOVERY_HOOK_NAME = "skillweave-discovery-research"


def discovery_binding_dict(
    *,
    provider: str = "duckduckgo",
    adapter_id: Optional[str] = None,
    query: Optional[str] = None,
    queries: Optional[list[str]] = None,
    max_results: int = 10,
    allow_network: bool = False,
    endpoint_allowlist: Optional[list[str]] = None,
    adapter_factories: Optional[list[str]] = None,
    credential_availability: Optional[Mapping[str, str]] = None,
    priority: int = 100,
    failure_mode: str = "block",
) -> dict[str, Any]:
    """The binding config as a plain mapping (validates via the binding schema).

    Egress stays denied (``allow_network=False``) and no credential value is
    embedded: the default descriptor makes no network call and stores no secret.
    ``adapter_id`` names an externally registered adapter directly (used by
    conformance fixtures); a consumer normally names only ``provider``.
    """
    config: dict[str, Any] = {
        "provider": provider,
        "query": query if query is not None else DEFAULT_DISCOVERY_QUERY,
        "max_results": max_results,
        "allow_network": allow_network,
    }
    if adapter_id:
        config["adapter_id"] = adapter_id
    if queries:
        config["queries"] = list(queries)
    if endpoint_allowlist:
        config["endpoint_allowlist"] = list(endpoint_allowlist)
    if adapter_factories:
        config["adapter_factories"] = list(adapter_factories)
    if credential_availability:
        config["credential_availability"] = dict(credential_availability)

    return {
        "version": "1",
        "phase": "discovery",
        "position": "pre",
        "hooks": [
            {
                "name": DISCOVERY_HOOK_NAME,
                "type": "python",
                "module": DISCOVERY_HOOK_MODULE,
                "priority": priority,
                "failureMode": failure_mode,
                "config": config,
            }
        ],
    }


def discovery_binding_yaml(**kwargs: Any) -> str:
    """Render the binding descriptor as YAML."""
    return yaml.safe_dump(discovery_binding_dict(**kwargs), sort_keys=False)


def write_discovery_binding(project_root: str | Path, **kwargs: Any) -> Path:
    """Materialize the binding into ``<project_root>/.skillweave/hooks/``."""
    root = Path(project_root)
    target = root / DISCOVERY_BINDING_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(discovery_binding_yaml(**kwargs))
    return target
