"""The executable Discovery lifecycle consumer (SW-ADAPT-001, AC4/AC8).

``skillweave-discovery`` ships as a *prompt* skill: ``skills/skillweave-discovery/
SKILL.md`` is Markdown a host runs. A prompt pack does not, by itself, invoke a
search provider -- so the criterion "the actual Discovery lifecycle consumer is
bound to the registry/typed search" cannot be met by the Markdown alone.

This package is that missing, source-owned boundary made executable. It binds
Discovery to the *same* extension point every other search consumer uses -- the
:class:`~skillweave.adapters.AdapterRegistry` / :class:`~skillweave.adapters.
AdapterFacade` -- by way of the SkillWeave lifecycle's real phase-boundary
executor, the Studio hook engine (:mod:`skillweave.studio.hooks`), which is the
only mechanism that runs code at ``Phase.DISCOVERY``.

Three things live here:

* :mod:`skillweave.discovery.lifecycle_search` -- the search seam: resolve a
  provider through the shared registry, admit it under policy, invoke it, and
  return a **typed** :class:`~skillweave.adapters.SearchOutcome` plus an
  :class:`~skillweave.adapters.OperationReceipt`;
* :mod:`skillweave.discovery.lifecycle_hook` --
  :class:`~skillweave.discovery.lifecycle_hook.DiscoveryResearchHook`, a
  :class:`~skillweave.studio.hooks.HookAdapter` bound at ``discovery/pre`` that
  drives that seam;
* :mod:`skillweave.discovery.binding` -- the source-owned binding descriptor a
  consumer materializes into ``.skillweave/hooks/discovery-pre.yaml``.

Nothing here creates a WorkContract, holds gate authority, replaces the SDK
``search-provider/v1`` contract, or invokes a live provider: the default provider
fails closed without a credential, before any network call.
"""

from .lifecycle_search import (
    DEFAULT_DISCOVERY_PROVIDER,
    UnknownDiscoveryProvider,
    adapter_id_for,
    facade_for,
    registry_for,
    resolve_search_outcome,
)
from .lifecycle_hook import (
    DEFAULT_DISCOVERY_QUERY,
    DISCOVERY_HOOK_MODULE,
    DiscoveryResearchHook,
    load_adapter_factories,
)
from .binding import (
    DISCOVERY_BINDING_FILENAME,
    DISCOVERY_BINDING_PATH,
    DISCOVERY_HOOK_NAME,
    SKILL_BINDING_KEY,
    binding_from_skill_manifest,
    discovery_binding_dict,
    discovery_binding_yaml,
    materialize_skill_binding,
    write_discovery_binding,
)

__all__ = [
    "DEFAULT_DISCOVERY_PROVIDER",
    "DEFAULT_DISCOVERY_QUERY",
    "UnknownDiscoveryProvider",
    "adapter_id_for",
    "registry_for",
    "facade_for",
    "resolve_search_outcome",
    "DiscoveryResearchHook",
    "DISCOVERY_HOOK_MODULE",
    "load_adapter_factories",
    "DISCOVERY_BINDING_FILENAME",
    "DISCOVERY_BINDING_PATH",
    "DISCOVERY_HOOK_NAME",
    "SKILL_BINDING_KEY",
    "discovery_binding_dict",
    "discovery_binding_yaml",
    "write_discovery_binding",
    "binding_from_skill_manifest",
    "materialize_skill_binding",
]
