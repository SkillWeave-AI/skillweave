"""The Discovery lifecycle hook (SW-ADAPT-001, AC4/AC8).

:class:`DiscoveryResearchHook` is the *executable* Discovery consumer. It is a
plain :class:`~skillweave.studio.hooks.HookAdapter`, so the Studio hook engine --
the only mechanism that runs code at a lifecycle phase boundary -- invokes it at
``discovery/pre`` (the ``pre_discovery`` injection point, which is free tier).

Its one job: drive the registry-backed search seam
(:func:`skillweave.discovery.lifecycle_search.resolve_search_outcome`) and turn
the **typed** outcome into a phase-boundary result.

It is deliberately honest about failure:

* an unknown provider is reported, never silently re-routed;
* a policy refusal (unapproved adapter, egress denied, missing credential) is
  reported with its ``asset``;
* a non-success outcome (outage, block, no results) is a hook ``fail`` that
  names the typed status and error code -- an outage is never dressed up as a
  retrieved source;
* a backend that returns something other than a
  :class:`~skillweave.adapters.SearchOutcome` is refused as ``malformed``.

The hook never sets ``gate_override``: a hook is not gate authority. It reports;
the phase gate and any acceptance decision stay where they belong.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from skillweave.adapters import (
    AdapterBackend,
    AdapterDescriptor,
    AdapterError,
    SearchOutcome,
)
from skillweave.studio.hooks.adapter import HookAdapter
from skillweave.studio.hooks.models import HookResult, Phase, PhaseContext

from .lifecycle_search import (
    DEFAULT_DISCOVERY_PROVIDER,
    UnknownDiscoveryProvider,
    resolve_search_outcome,
)

__all__ = [
    "DiscoveryResearchHook",
    "DISCOVERY_HOOK_MODULE",
    "DEFAULT_DISCOVERY_QUERY",
    "load_adapter_factories",
]

#: The dotted ``module.ClassName`` a binding names for this hook.
DISCOVERY_HOOK_MODULE = "skillweave.discovery.lifecycle_hook.DiscoveryResearchHook"

#: The grounded query used when neither the hook config nor the project config
#: names one. It is a real query a Discovery phase would ask.
DEFAULT_DISCOVERY_QUERY = "SkillWeave discovery problem statement research"


@dataclass(frozen=True)
class _QueryRun:
    query: str
    outcome: SearchOutcome
    receipt: Any


def load_adapter_factories(
    specs: Sequence[str],
) -> list[tuple[AdapterDescriptor, AdapterBackend, str]]:
    """Load externally-named adapter factories into registry triples.

    Each ``spec`` is ``"module:callable"`` (or ``"module.callable"``). The
    callable returns either one ``(descriptor, backend)`` pair or an iterable of
    them. This is how an adapter the core has never heard of is registered with
    **no core edit** -- the binding file names it, at wiring time, exactly as the
    extension point promises.
    """
    triples: list[tuple[AdapterDescriptor, AdapterBackend, str]] = []
    for spec in specs:
        module_name, sep, attr = spec.partition(":")
        if not sep:
            module_name, _, attr = spec.rpartition(".")
        if not module_name or not attr:
            raise AdapterError(
                f"adapter factory spec {spec!r} must be 'module:callable'",
                asset="external_adapter",
            )
        module = importlib.import_module(module_name)
        factory = getattr(module, attr)
        produced = factory()
        if isinstance(produced, tuple) and len(produced) == 2:
            produced = [produced]
        for descriptor, backend in produced:
            if not isinstance(descriptor, AdapterDescriptor):
                raise AdapterError(
                    f"adapter factory {spec!r} did not yield an AdapterDescriptor",
                    asset="external_adapter",
                )
            if not isinstance(backend, AdapterBackend):
                raise AdapterError(
                    f"adapter factory {spec!r} did not yield an AdapterBackend",
                    asset="external_adapter",
                )
            triples.append((descriptor, backend, f"external:{module_name}"))
    return triples


class DiscoveryResearchHook(HookAdapter):
    """Runs Discovery research through the shared, registry-backed search seam."""

    def should_run(self, ctx: PhaseContext) -> bool:
        """Only at ``discovery`` / ``pre`` -- the phase's research boundary."""
        return ctx.phase == Phase.DISCOVERY and ctx.position.value == "pre"

    async def execute(self, ctx: PhaseContext) -> HookResult:
        cfg: Mapping[str, Any] = ctx.hook_config or {}

        if cfg.get("enabled", True) is False:
            return HookResult(status="skip", message="discovery research disabled by config")

        provider = str(cfg.get("provider", DEFAULT_DISCOVERY_PROVIDER))
        adapter_id = cfg.get("adapter_id")
        queries = self._queries(cfg, ctx)
        max_results = int(cfg.get("max_results", 10))
        allow_network = bool(cfg.get("allow_network", False))
        endpoint_allowlist = tuple(cfg.get("endpoint_allowlist", ())) or None
        mcp_command = cfg.get("mcp_command")
        mcp_env_allowlist = tuple(cfg.get("mcp_env_allowlist", ()))
        credential_availability = cfg.get("credential_availability")
        api_key = cfg.get("api_key")

        try:
            extra = load_adapter_factories(tuple(cfg.get("adapter_factories", ())))
        except Exception as exc:  # factory wiring is a hard, reported failure
            return HookResult(
                status="fail",
                message=f"discovery external adapter could not be wired: {exc}",
            )

        runs: list[_QueryRun] = []
        for query in queries:
            try:
                outcome, receipt = await resolve_search_outcome(
                    query,
                    provider=provider,
                    adapter_id=adapter_id,
                    allow_network=allow_network,
                    endpoint_allowlist=endpoint_allowlist,
                    credential_availability=credential_availability,
                    api_key=api_key,
                    max_results=max_results,
                    mcp_command=mcp_command,
                    mcp_env_allowlist=mcp_env_allowlist,
                    extra_adapters=extra,
                )
            except UnknownDiscoveryProvider as exc:
                return HookResult(status="fail", message=f"unknown provider: {exc}")
            except AdapterError as exc:
                return HookResult(
                    status="fail",
                    message=(
                        f"discovery search refused by policy "
                        f"(asset={getattr(exc, 'asset', None)}): {exc}"
                    ),
                )
            if not isinstance(outcome, SearchOutcome):
                return HookResult(
                    status="fail",
                    message=(
                        "malformed search outcome: backend returned "
                        f"{type(outcome).__name__}, not a typed SearchOutcome"
                    ),
                )
            runs.append(_QueryRun(query=query, outcome=outcome, receipt=receipt))

        failed = [r for r in runs if r.outcome.status != "success"]
        evidence_path = self._write_evidence(ctx, provider, runs)
        artifacts = [str(evidence_path)] if evidence_path else []

        if failed:
            detail = "; ".join(
                f"{r.outcome.requested_provider}: {r.outcome.status}"
                + (f"/{r.outcome.error_code}" if r.outcome.error_code else "")
                for r in failed
            )
            return HookResult(
                status="fail",
                message=f"discovery search did not succeed ({detail})",
                artifacts=artifacts,
            )

        total_sources = sum(len(r.outcome.results) for r in runs)
        return HookResult(
            status="pass",
            message=(
                f"discovery search via {provider}: {len(runs)} query(ies), "
                f"{total_sources} real source(s)"
            ),
            artifacts=artifacts,
        )

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _queries(cfg: Mapping[str, Any], ctx: PhaseContext) -> list[str]:
        if cfg.get("queries"):
            queries = [str(q) for q in cfg["queries"] if str(q).strip()]
            if queries:
                return queries
        if cfg.get("query"):
            return [str(cfg["query"])]
        project_cfg = ctx.config or {}
        for key in ("discovery_query", "query"):
            if project_cfg.get(key):
                return [str(project_cfg[key])]
        return [DEFAULT_DISCOVERY_QUERY]

    @staticmethod
    def _write_evidence(
        ctx: PhaseContext, provider: str, runs: Sequence[_QueryRun]
    ) -> Optional[Path]:
        """Persist the typed outcome/receipt record as hook evidence.

        The record carries no credential value: only the receipt (which names the
        adapter, its descriptor digest and the contract) and the typed outcome.
        """
        try:
            out_dir = Path(ctx.project_root) / ".skillweave" / "discovery"
            out_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(
                ("|".join(r.query for r in runs)).encode("utf-8")
            ).hexdigest()[:16]
            payload = {
                "phase": ctx.phase.value,
                "position": ctx.position.value,
                "requested_provider": provider,
                "runs": [
                    {
                        "query_sha256": r.outcome.query_sha256,
                        "status": r.outcome.status,
                        "error_code": r.outcome.error_code,
                        "result_count": len(r.outcome.results),
                        "results": [s.to_dict() for s in r.outcome.results],
                        "attempts": [a.to_dict() for a in r.outcome.attempts],
                        "receipt": r.receipt.to_dict(),
                    }
                    for r in runs
                ],
            }
            path = out_dir / f"research-{digest}.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=True))
            return path
        except Exception:
            return None
