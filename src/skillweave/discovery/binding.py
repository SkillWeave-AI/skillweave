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

The Markdown skill ``skills/skillweave-discovery`` *declares* this same binding
in its ``capability.yaml`` under the ``studio_binding`` key. That is the exact
criterion-driven path addition: the prompt skill's declared lifecycle boundary
(its Phase 2 "Research" step) is bound, in data, to the source-owned
``DiscoveryResearchHook`` -- so materializing the skill's declaration
(:func:`materialize_skill_binding`) is what makes the Markdown facade actually
drive registry-backed typed search. A Markdown facade on its own invokes
nothing; the declaration plus this reader is the missing, source-owned link.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from skillweave.adapters import AdapterError
from skillweave.studio.hooks.binding.schema import BindingConfig, BindingValidationError

from .lifecycle_hook import DISCOVERY_HOOK_MODULE, DEFAULT_DISCOVERY_QUERY

__all__ = [
    "DISCOVERY_BINDING_FILENAME",
    "DISCOVERY_HOOK_NAME",
    "DISCOVERY_BINDING_PATH",
    "SKILL_BINDING_KEY",
    "discovery_binding_dict",
    "discovery_binding_yaml",
    "write_discovery_binding",
    "binding_from_skill_manifest",
    "materialize_skill_binding",
]

#: Where a project places the binding, relative to its root.
DISCOVERY_BINDING_PATH = Path(".skillweave") / "hooks" / "discovery-pre.yaml"
DISCOVERY_BINDING_FILENAME = "discovery-pre.yaml"
DISCOVERY_HOOK_NAME = "skillweave-discovery-research"

#: The key a *skill* manifest (``skills/skillweave-discovery/capability.yaml``)
#: uses to declare the phase-boundary binding its Markdown facade materializes.
#: It is a plain data field, not an executable: the value names the source-owned
#: :class:`~skillweave.discovery.lifecycle_hook.DiscoveryResearchHook` the engine
#: imports from the installed wheel, so "the skill runs a search" is a
#: declarative statement the consumer acts on -- never a host-shell command.
SKILL_BINDING_KEY = "studio_binding"

#: The only phase/position this binding may fill, and the only module it may
#: name. A skill manifest is data a consumer materializes, so it is validated
#: against this closed shape before it can produce a hook binding.
_DISCOVERY_PHASE = "discovery"
_DISCOVERY_POSITION = "pre"


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


#: The config keys a ``studio_binding`` block may carry. They are exactly the
#: data knobs :func:`discovery_binding_dict` already understands; an unknown key
#: fails closed rather than being silently dropped (a typo must not read as
#: "no network" when the author meant to allow one endpoint).
_ALLOWED_BINDING_CONFIG_KEYS = frozenset(
    {
        "provider",
        "adapter_id",
        "query",
        "queries",
        "max_results",
        "allow_network",
        "endpoint_allowlist",
        "adapter_factories",
        "credential_availability",
        "priority",
        "failure_mode",
    }
)


def binding_from_skill_manifest(
    manifest: Mapping[str, Any],
    *,
    default_provider: str = "duckduckgo",
) -> dict[str, Any]:
    """Turn a skill manifest's ``studio_binding`` block into a binding config.

    This is the *binding* half of the Discovery skill: it reads the data the
    Markdown facade declares in ``skills/skillweave-discovery/capability.yaml``
    and produces the same ``.skillweave/hooks/discovery-pre.yaml`` descriptor
    :func:`discovery_binding_dict` produces -- so the skill's declared boundary
    is what the Studio hook engine actually loads.

    It is deliberately conservative, and fails closed:

    * the block must exist and be a mapping;
    * it must name ``phase: discovery`` / ``position: pre`` and the source-owned
      ``DiscoveryResearchHook`` module -- nothing else can be bound through it;
    * it may only carry the known data knobs (an unknown key is refused);
    * a caller may not smuggle a credential *value* in through it (the block is
      data, and credentials are resolved by reference through the policy gate).

    The block embeds no credential value and denies egress by default: omitting
    ``allow_network`` leaves egress denied, exactly like the bare descriptor.
    """
    if not isinstance(manifest, Mapping):
        raise AdapterError(
            "a skill manifest must be a mapping to read its studio_binding",
            asset="studio_binding",
        )
    block = manifest.get(SKILL_BINDING_KEY)
    if block is None:
        raise AdapterError(
            f"skill manifest declares no {SKILL_BINDING_KEY!r} block",
            asset=SKILL_BINDING_KEY,
        )
    if not isinstance(block, Mapping):
        raise AdapterError(
            f"{SKILL_BINDING_KEY!r} must be a mapping", asset=SKILL_BINDING_KEY
        )

    phase = str(block.get("phase", ""))
    if phase != _DISCOVERY_PHASE:
        raise AdapterError(
            f"the Discovery skill may only bind phase {_DISCOVERY_PHASE!r}, not "
            f"{phase!r}",
            asset=f"{SKILL_BINDING_KEY}.phase",
        )
    position = str(block.get("position", ""))
    if position != _DISCOVERY_POSITION:
        raise AdapterError(
            f"the Discovery skill may only bind position {_DISCOVERY_POSITION!r}, "
            f"not {position!r}",
            asset=f"{SKILL_BINDING_KEY}.position",
        )
    module = str(block.get("module", ""))
    if module != DISCOVERY_HOOK_MODULE:
        raise AdapterError(
            f"the Discovery skill must bind the source-owned hook "
            f"{DISCOVERY_HOOK_MODULE!r}; {module!r} would invoke code the "
            "facade does not own",
            asset=f"{SKILL_BINDING_KEY}.module",
        )

    config: dict[str, Any] = dict(block.get("config", {}) or {})
    if "api_key" in config:
        raise AdapterError(
            "a skill binding is data and embeds no credential value; credentials "
            "are resolved by reference through the policy gate",
            asset=f"{SKILL_BINDING_KEY}.config.api_key",
        )
    unknown = sorted(set(config) - _ALLOWED_BINDING_CONFIG_KEYS)
    if unknown:
        raise AdapterError(
            f"{SKILL_BINDING_KEY}.config carries unknown key(s) {unknown}; allowed: "
            f"{sorted(_ALLOWED_BINDING_CONFIG_KEYS)}",
            asset=f"{SKILL_BINDING_KEY}.config",
        )

    kwargs = dict(config)
    kwargs.setdefault("provider", default_provider)
    args: dict[str, Any] = {"provider": kwargs.pop("provider")}
    for key in (
        "adapter_id",
        "query",
        "queries",
        "max_results",
        "allow_network",
        "endpoint_allowlist",
        "adapter_factories",
        "credential_availability",
        "priority",
        "failure_mode",
    ):
        if key in kwargs:
            args[key] = kwargs.pop(key)
    if kwargs:  # pragma: no cover - defensive; unknown keys were refused above
        raise AdapterError(
            f"unexpected binding config key(s) {sorted(kwargs)}",
            asset=f"{SKILL_BINDING_KEY}.config",
        )
    return discovery_binding_dict(**args)


def materialize_skill_binding(
    manifest: Mapping[str, Any],
    project_root: str | Path,
    *,
    default_provider: str = "duckduckgo",
) -> Path:
    """Validate a skill manifest's binding block and write it into the project.

    The written descriptor is re-validated through the *engine's own* schema
    (:class:`~skillweave.studio.hooks.binding.schema.BindingConfig`) before it is
    persisted, so an un-loadable binding can never be materialized.
    """
    payload = binding_from_skill_manifest(manifest, default_provider=default_provider)
    try:
        BindingConfig.from_dict(payload, source_path=SKILL_BINDING_KEY + ":manifest")
    except BindingValidationError as exc:
        raise AdapterError(
            f"the skill's {SKILL_BINDING_KEY!r} block does not produce a valid "
            f"hook binding: {exc}",
            asset=SKILL_BINDING_KEY,
        ) from exc
    root = Path(project_root)
    target = root / DISCOVERY_BINDING_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=False))
    return target
