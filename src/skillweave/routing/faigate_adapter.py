"""Multi-provider model routing for SkillWeave Council.

Auto-detection of available routers:
- Faigate (local, brew-installed at 127.0.0.1:$(FAIGATE_PORT, default 8090)/v1)
- OpenRouter (cloud, api.openrouter.ai)
- ClawRouter (local/cloud, clawrouter.ai)
- KiloRouter (local/cloud, kilorouter.com)
- OmniRoute (local, localhost:20128/v1)
- 9router (local/cloud, 9router)
- SingleModel (fallback — no router needed)

Architecture:
  CouncilEngine → CouncilProvider (interface)
    ├── FaigateProvider     (GET /v1/models → {"object":"list","data":[▶]})
    ├── OpenRouterProvider  (GET /models → {"data":[▶]})
    ├── GenericRouterProvider  (Kilo/Claw/OmniRoute/9router via /chat/completions)
    └── SingleModelProvider (fallback — no router needed)
"""

import asyncio
import json
import os
import urllib.request
import urllib.error
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Optional


#: The gateway namespace the dispatch layer qualifies model ids with. Council
#: profile data (ROUTER_PROFILES, ``council-profiles.md``, ``capability.yaml``)
#: never carries this prefix: it names the outer transport, so it lives only at
#: the adapter boundary and is translated exactly once here.
PROVIDER_NAMESPACE = "faigate"


class ModelNamespaceError(ValueError):
    """A model id carried an invalid or doubled outer gateway prefix.

    Translation happens exactly once in this adapter. A doubled prefix
    (``faigate/faigate/x``), a missing translation, or an unrecognised outer
    prefix is refused here — never silently stripped — so a downstream provider
    is never handed a half-mangled id.
    """

    def __init__(self, model: str, reason: str):
        super().__init__(f"invalid model namespace for {model!r}: {reason}")
        self.model = model
        self.reason = reason


class UnavailableModelError(RuntimeError):
    """The provider confirmed it cannot serve the requested model id."""

    def __init__(self, model: str, provider: str = "faigate"):
        super().__init__(f"{provider} reports model {model!r} unavailable")
        self.model = model
        self.provider = provider


class RateLimitedError(RuntimeError):
    """The provider rate-limited the request (HTTP 429 or an explicit rate message)."""

    def __init__(self, model: str, provider: str = "faigate"):
        super().__init__(f"{provider} rate-limited the request for {model!r}")
        self.model = model
        self.provider = provider


def translate_model_id(model: str, provider: str = "faigate") -> str:
    """Return the provider-native model id, translating the outer prefix once.

    This is the single, owning translation point. The rules are exact:

    * a bare provider-native id (no prefix) passes through unchanged;
    * a single ``faigate/`` prefix (dispatch's gateway form) or a single
      ``faigate:`` prefix (the legacy form) is stripped exactly once;
    * a doubled prefix (``faigate/faigate/x``), any other outer prefix
      (``openrouter/x``, ``omniroute/x``, …) or a dangling ``faigate/`` raises
      :class:`ModelNamespaceError` — it is never silently collapsed.

    After the namespace is resolved exactly once, the provider's own alias table
    (``FAIGATE_MAP`` / ``OPENROUTER_MAP``) is applied as the single provider
    backend. That mapping is *not* a second translation path and never runs on a
    prefixed id: it only rewrites an already-resolved, provider-native alias into
    the concrete target the router expects. An unmapped alias passes through
    unchanged.

    The function is pure: no network, no global state. It exists so that callers
    (``FaidateProvider``, the council engine) never hand-code ``replace`` on an
    id, which is how the v1.3.9 prefix leaked and how the v1.3.10 correction
    left a silent ``replace`` that could collapse an unknown prefix.
    """
    if not model:
        raise ModelNamespaceError(model, "empty model id")
    if "/" not in model and ":" not in model:
        body = model
    # A single leading ``faigate/`` or ``faigate:`` prefix translates away
    # exactly once. Both delimiters name the one outer gateway namespace.
    elif model.startswith(f"{PROVIDER_NAMESPACE}/") or model.startswith(f"{PROVIDER_NAMESPACE}:"):
        body = model[len(PROVIDER_NAMESPACE) + 1:]
        if not body:
            raise ModelNamespaceError(model, "dangling prefix without a body")
        if body.startswith(f"{PROVIDER_NAMESPACE}/") or body.startswith(f"{PROVIDER_NAMESPACE}:"):
            raise ModelNamespaceError(model, "prefix applied twice")
        if "/" in body or ":" in body:
            raise ModelNamespaceError(model, "prefix followed by a nested namespace")
    # Any other outer prefix is refused rather than silently mangled.
    else:
        raise ModelNamespaceError(model, "unrecognised provider prefix")

    # Provider-specific alias backend: maps a resolved native alias to the
    # concrete target id the provider expects. This is the only second lookup in
    # the module, and it is reached only through this single translation path.
    mapping = _PROVIDER_MODEL_MAPS.get(provider)
    if mapping is not None:
        body = mapping.get(body, body)
    return body


def validate_council_model_ids(*, models, chairman, source: str = "council profile") -> None:
    """Refuse any outer gateway prefix in Council profile data, before a call.

    Council data must be provider-native. A ``faigate/`` or other outer prefix
    leaking into a Council profile is a data error and is refused here — before
    any provider call — with the offending id and the source named. This is the
    fail-closed counterpart to :func:`translate_model_id`: the dispatch layer may
    qualify ids with ``faigate/`` (that remains valid), but that syntax cannot
    leak into the Council's own casting.
    """
    ids = list(models) + ([chairman] if chairman else [])
    for mid in ids:
        if "/" in mid or ":" in mid:
            raise ModelNamespaceError(
                mid,
                f"outer gateway prefix is not allowed in {source}; "
                f"expected a provider-native id like {translate_naive_body(mid)}",
            )


def translate_naive_body(model: str) -> str:
    """Return the body after a single leading prefix, used only for error text."""
    return model.split("/", 1)[1] if "/" in model else model


def _is_rate_limit(error_text: str) -> bool:
    """Whether an error string signals a provider rate limit (HTTP 429 etc.).

    Rate-limiting is a *technical* failure, distinct from an unknown/unavailable
    model. It must surface as :class:`RateLimitedError` so a caller can tell it
    apart from a substitution or a namespace error, and can retry without
    re-classifying the seat.
    """
    low = (error_text or "").lower()
    return "429" in low or "rate limit" in low or "rate-limited" in low or "too many requests" in low


def _describe_error(exc: Exception, url: str) -> str:
    """Return a human-readable cause for a transport failure, naming timeouts."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
    if reason is not None and (isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason)):
        return f"timeout at {url}"
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} at {url}"
    return f"{exc} (at {url})"


DEFAULT_CACHE_TTL = 300.0  # 5 minutes default TTL for gateway model cache
 
 
@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str = "faigate"
    available: bool = True
    credits_remaining: float = -1.0
    context_window: int = 128000
    cost_per_1k: float = 0.0
    cost: float = 0.0
    reasoning: bool = False
    capabilities: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.cost == 0.0 and self.cost_per_1k != 0.0:
            self.cost = self.cost_per_1k
        elif self.cost_per_1k == 0.0 and self.cost != 0.0:
            self.cost_per_1k = self.cost
        if "reasoning" in self.capabilities and not self.reasoning:
            self.reasoning = True
        elif self.reasoning and "reasoning" not in self.capabilities:
            self.capabilities.append("reasoning")

    def has_capability(self, capability: str) -> bool:
        cap_low = capability.lower().strip()
        if cap_low == "reasoning" and self.reasoning:
            return True
        return cap_low in [c.lower() for c in self.capabilities]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "available": self.available,
            "credits_remaining": self.credits_remaining,
            "context_window": self.context_window,
            "cost_per_1k": self.cost_per_1k,
            "cost": self.cost,
            "reasoning": self.reasoning,
            "capabilities": list(self.capabilities),
            "raw": dict(self.raw),
        }


def parse_model_info(entry: dict | str | ModelInfo, provider: str = "faigate") -> ModelInfo:
    """Parse raw gateway model entry into a ModelInfo instance with parsed capabilities."""
    if isinstance(entry, ModelInfo):
        return entry

    if isinstance(entry, str):
        mid = entry.strip()
        caps: set[str] = set()
        id_lower = mid.lower()
        if (
            "reason" in id_lower
            or "-r1" in id_lower
            or "r1-" in id_lower
            or id_lower.endswith("r1")
            or "o1" in id_lower
            or "o3" in id_lower
        ):
            caps.add("reasoning")
        if "vision" in id_lower or "4o" in id_lower or "vl" in id_lower:
            caps.add("vision")
        if "coder" in id_lower or "code" in id_lower:
            caps.add("coding")
        return ModelInfo(
            id=mid,
            name=mid,
            provider=provider,
            available=True,
            context_window=128000,
            cost_per_1k=0.0,
            cost=0.0,
            reasoning=("reasoning" in caps),
            capabilities=sorted(list(caps)),
            raw={"id": mid},
        )

    if not isinstance(entry, dict):
        mid = str(entry)
        return ModelInfo(
            id=mid,
            name=mid,
            provider=provider,
            available=True,
            capabilities=[],
            raw={},
        )

    mid = str(entry.get("id") or "")
    name = str(entry.get("name") or mid)
    available = bool(entry.get("available", True))
    try:
        credits_remaining = float(entry.get("credits_remaining", -1.0))
    except (ValueError, TypeError):
        credits_remaining = -1.0

    context_window = 128000
    for cw_key in ("context_window", "context_length", "max_context_length", "max_tokens"):
        val = entry.get(cw_key)
        if val is not None:
            try:
                context_window = int(val)
                break
            except (ValueError, TypeError):
                pass
    if context_window == 128000 and isinstance(entry.get("top_provider"), dict):
        top = entry["top_provider"]
        for cw_key in ("context_length", "max_completion_tokens"):
            val = top.get(cw_key)
            if val is not None:
                try:
                    context_window = int(val)
                    break
                except (ValueError, TypeError):
                    pass

    cost = 0.0
    for cost_key in ("cost", "cost_per_1k"):
        val = entry.get(cost_key)
        if val is not None:
            try:
                cost = float(val)
                break
            except (ValueError, TypeError):
                pass
    if cost == 0.0 and "pricing" in entry:
        pricing = entry["pricing"]
        if isinstance(pricing, dict):
            try:
                prompt_p = float(pricing.get("prompt") or 0.0)
                cost = prompt_p
            except (ValueError, TypeError):
                pass
        else:
            try:
                cost = float(pricing)
            except (ValueError, TypeError):
                pass

    caps: set[str] = set()
    raw_caps = entry.get("capabilities")
    if isinstance(raw_caps, list):
        for c in raw_caps:
            if isinstance(c, str) and c.strip():
                caps.add(c.lower().strip())
    elif isinstance(raw_caps, dict):
        for k, v in raw_caps.items():
            if v:
                caps.add(str(k).lower().strip())
    elif isinstance(raw_caps, str):
        for part in raw_caps.split(","):
            if part.strip():
                caps.add(part.lower().strip())

    supp_params = entry.get("supported_parameters")
    if isinstance(supp_params, list):
        for sp in supp_params:
            if isinstance(sp, str) and sp.strip():
                caps.add(sp.lower().strip())

    flags = entry.get("flags")
    if isinstance(flags, list):
        for f in flags:
            if isinstance(f, str) and f.strip():
                caps.add(f.lower().strip())

    arch = entry.get("architecture")
    if isinstance(arch, dict):
        inst_type = str(arch.get("instruct_type") or "").lower().strip()
        if inst_type:
            caps.add(inst_type)
        modality = str(arch.get("modality") or "").lower().strip()
        if "image" in modality or "vision" in modality:
            caps.add("vision")

    if entry.get("reasoning") or entry.get("is_reasoning"):
        caps.add("reasoning")

    id_lower = mid.lower()
    name_lower = name.lower()
    desc_lower = str(entry.get("description") or "").lower()

    if (
        "reason" in id_lower
        or "-r1" in id_lower
        or "r1-" in id_lower
        or id_lower.endswith("r1")
        or "o1" in id_lower
        or "o3" in id_lower
        or "reasoning" in desc_lower
        or "thinking" in desc_lower
    ):
        caps.add("reasoning")

    if "vision" in id_lower or "4o" in id_lower or "vl" in id_lower or "vision" in desc_lower:
        caps.add("vision")

    if "coder" in id_lower or "code" in id_lower or "coding" in desc_lower or "coder" in name_lower:
        caps.add("coding")

    if "tool" in desc_lower or "tools" in id_lower:
        caps.add("tools")

    reasoning = ("reasoning" in caps)

    return ModelInfo(
        id=mid,
        name=name,
        provider=provider,
        available=available,
        credits_remaining=credits_remaining,
        context_window=context_window,
        cost_per_1k=cost,
        cost=cost,
        reasoning=reasoning,
        capabilities=sorted(list(caps)),
        raw=entry,
    )


def is_substitution(
    requested_model: str,
    served_by: Optional[str] = None,
    answering_model: Optional[str] = None,
    provider: str = "faigate",
) -> bool:
    """Compare requested model ID with serving/answering model ID to flag substitutions.

    Implements Anti-Masking fallback detection: when a gateway transparently or
    silently falls back to another model (e.g. routing Claude to DeepSeek or Gemini
    to GPT), this checks whether the requested model matches the actual model that
    served the response.

    Returns:
        bool: True if a substitution occurred, False otherwise.
    """
    actual = served_by or answering_model
    if not actual or not requested_model:
        return False

    # Direct match
    if actual == requested_model:
        return False

    # Check translated provider-native forms (e.g. stripping gateway namespace or mapping aliases)
    try:
        clean_requested = translate_model_id(requested_model, provider)
    except Exception:
        clean_requested = requested_model

    try:
        clean_actual = translate_model_id(actual, provider)
    except Exception:
        clean_actual = actual

    if actual == clean_requested or clean_actual == requested_model or clean_actual == clean_requested:
        return False

    return True


detect_substitution = is_substitution


class AttributedResponse(str):
    """A ``str`` that also carries which model actually answered and served.

    The council engine reads ``query()`` as a plain string, so this stays a
    real ``str`` subclass — f-strings, truthiness and slicing all behave like
    ``str``. It adds ``requested_model``, ``answering_model``, ``served_by``,
    ``substituted`` / ``is_substituted`` and ``provider`` so the run record can see,
    per seat: which model was requested, which one actually answered, which one
    served it, whether identity enforcement flagged a substitution (Anti-Masking
    fallback detection), and through which provider.

    Attributes:
        requested_model: The model identifier requested by the caller.
        answering_model: The model reported in the response envelope's ``model`` field.
        served_by: The underlying model that actually served the completion.
        substituted: Boolean flag indicating if substitution / fallback occurred.
        is_substituted: Alias for ``substituted``.
        provider: The provider / transport name.
    """

    def __new__(
        cls,
        content: str,
        *,
        requested_model: str,
        answering_model: str,
        provider: str = "unknown",
        served_by: Optional[str] = None,
        is_substituted: Optional[bool] = None,
        substituted: Optional[bool] = None,
    ) -> "AttributedResponse":
        obj = super().__new__(cls, content)
        obj.requested_model = requested_model
        obj.answering_model = answering_model
        obj.provider = provider
        actual_served_by = served_by if served_by is not None else answering_model
        obj.served_by = actual_served_by

        if is_substituted is not None:
            flag = bool(is_substituted)
        elif substituted is not None:
            flag = bool(substituted)
        else:
            flag = is_substitution(
                requested_model=requested_model,
                served_by=actual_served_by,
                answering_model=answering_model,
                provider=provider,
            )
        obj.is_substituted = flag
        obj.substituted = flag
        return obj

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, Any]]:
        """Hand ``copy`` and ``pickle`` the keyword-only arguments ``__new__`` needs.

        Both rebuild a ``str`` subclass by calling ``__new__``. Without this they
        pass the text alone, and ``dataclasses.asdict()`` or ``copy.deepcopy()``
        on a council result raises ``TypeError``. The recorded substitution
        verdict is handed over as recorded, never recomputed.
        """
        return (str(self),), {
            "requested_model": self.requested_model,
            "answering_model": self.answering_model,
            "provider": self.provider,
            "served_by": self.served_by,
            "is_substituted": self.is_substituted,
        }


def _extract_answer(envelope: dict, requested_model: str, provider: str = "unknown") -> AttributedResponse:
    """Read the answer content, actual answering model, and served_by from a response.

    ``answering_model`` is read from the envelope's ``model`` field, never
    inferred from the request. When a router omits the field, we fall back to
    the requested model and keep the record cheap — but we never fabricate a
    model that did not answer.

    ``served_by`` is parsed from the Faigate response envelope (or metadata/headers)
    to detect gateway-level fallback and silent model masking (Anti-Masking fallback detection).

    ``provider`` names the transport that produced the envelope, and is carried verbatim.
    """
    content = envelope["choices"][0]["message"]["content"]
    if not content:
        # A reasoning model that spent its budget on reasoning returns an empty
        # completion. That is a failure, not an answer. One guard here rather
        # than one per provider, because four copies is how one of them drifts.
        raise RuntimeError("model returned an empty completion")
    answering_model = envelope.get("model") or requested_model
    headers = envelope.get("headers") if isinstance(envelope.get("headers"), dict) else {}
    served_by = (
        envelope.get("served_by")
        or envelope.get("x_faigate_served_by")
        or headers.get("x-faigate-served-by")
        or headers.get("x-served-by")
        or answering_model
    )
    sub = is_substitution(
        requested_model=requested_model,
        served_by=served_by,
        answering_model=answering_model,
        provider=provider,
    )
    return AttributedResponse(
        content,
        requested_model=requested_model,
        answering_model=answering_model,
        provider=provider,
        served_by=served_by,
        is_substituted=sub,
        substituted=sub,
    )


FAIGATE_MAP = {
    "sonnet": "anthropic-sonnet",
    "claude-sonnet-4-5": "anthropic-sonnet",
    "haiku": "anthropic-haiku",
    "claude-haiku-3-5": "anthropic-haiku",
    "opus": "anthropic-claude",
    "claude-opus-4": "anthropic-claude",
    "gpt-4o": "openai-gpt4o",
    "gpt-4o-mini": "gemini-flash",
    "gemini-pro": "gemini-pro",
    "gemini-2-5-pro": "gemini-pro",
    "deepseek-v4": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "llama-4": "openrouter-fallback",
    "llama-4-maverick": "openrouter-fallback",
    "mistral": "anthropic-claude",
    "mistral-large": "anthropic-claude",
}

OPENROUTER_MAP = {
    "sonnet": "anthropic/claude-3.5-sonnet",
    "claude-sonnet-4-5": "anthropic/claude-3.5-sonnet",
    "haiku": "anthropic/claude-3-haiku",
    "claude-haiku-3-5": "anthropic/claude-3-haiku",
    "opus": "anthropic/claude-3-opus",
    "claude-opus-4": "anthropic/claude-3-opus",
    "gpt-4o": "openai/gpt-4o",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gemini-pro": "google/gemini-pro-1.5",
    "gemini-2-5-pro": "google/gemini-pro-1.5",
    "deepseek-v4": "deepseek/deepseek-coder",
    "deepseek-v4-pro": "deepseek/deepseek-coder",
    "llama-4": "meta-llama/llama-3-70b-instruct",
    "llama-4-maverick": "meta-llama/llama-3-70b-instruct",
    "mistral": "mistralai/mistral-large",
    "mistral-large": "mistralai/mistral-large",
}

#: Provider name -> alias table. ``translate_model_id`` reads this as its single
#: provider-specific backend; no call site may consult ``FAIGATE_MAP`` or
#: ``OPENROUTER_MAP`` directly — that would be a second translation path.
_PROVIDER_MODEL_MAPS = {
    "faigate": FAIGATE_MAP,
    "openrouter": OPENROUTER_MAP,
}

class CouncilProvider:
    """Abstract base: all providers implement query + availability + model registry."""

    def __init__(self):
        self._models_cache: list[ModelInfo] = []
        self._cache_timestamp: float = 0.0
        self._cache_ttl: float = float(os.environ.get("FAIGATE_CACHE_TTL", DEFAULT_CACHE_TTL))

    async def query(self, model: str, messages: list[dict], temperature: float = 0.5, timeout: float | None = None) -> str:
        raise NotImplementedError

    async def check_availability(self, models: list[str]) -> dict[str, bool]:
        return {m: True for m in models}

    async def check_credits(self, model: str) -> float:
        return -1.0

    def _is_cache_valid(self) -> bool:
        cache = getattr(self, "_models_cache", None)
        ts = getattr(self, "_cache_timestamp", 0.0)
        ttl = getattr(self, "_cache_ttl", DEFAULT_CACHE_TTL)
        return bool(cache) and (time.time() - ts < ttl)

    def clear_cache(self) -> None:
        self._models_cache = []
        self._cache_timestamp = 0.0

    async def fetch_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        """Fetch available models and their capabilities from the provider (cached)."""
        return []

    async def get_model_info(self, model: str, force_refresh: bool = False) -> Optional[ModelInfo]:
        """Get capabilities for a specific model."""
        models = await self.fetch_models(force_refresh=force_refresh)
        clean = translate_model_id(model, self.provider_name()) if model else model
        for m in models:
            if m.id == clean or m.id == model:
                return m
        return None

    def get_available_models(self, force_refresh: bool = False) -> list[str]:
        """Return model ids currently known to the provider."""
        cache = getattr(self, "_models_cache", [])
        return [m.id for m in cache if getattr(m, "available", True)]

    def provider_name(self) -> str:
        return "generic"


# ── Router Profiles ────────────────────────────────────────────────

# Council casts name provider-native Faigate roster ids directly — no symbolic
# seat aliases and no outer ``faigate/`` gateway prefix. The prefix belongs to
# dispatch qualification and never appears here; it is translated exactly once
# at the adapter boundary (``translate_model_id``) and refused in Council profile
# data by ``validate_council_model_ids``. Every id below is a real ``/v1/models``
# roster id measured (live, ``http://127.0.0.1:8090/v1``) to answer AS itself —
# its response envelope ``model`` field echoes the requested id. Faigate's roster
# self-answers only ``deepseek-v4-pro`` and ``deepseek-v4-flash``; every other id
# it serves silently collapses onto ``deepseek-v4-flash``, so those two are the
# only truthful seat ids and the only two that can hold the ``>=2`` distinct
# answering-model gate. Both are DeepSeek v4 models.
ROUTER_PROFILES = {
    "default": {
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "chairman": "deepseek-v4-pro",
        "mode": "standard",
        "temperature": 0.5,
    },
    "quick": {
        "models": ["deepseek-v4-flash"],
        "chairman": "deepseek-v4-flash",
        "mode": "quick",
        "temperature": 0.3,
    },
    "deep": {
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "chairman": "deepseek-v4-pro",
        "mode": "full",
        "temperature": 0.5,
    },
    "expert": {
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "chairman": "deepseek-v4-pro",
        "mode": "full",
        "temperature": 0.4,
    },
}

#: The revision of the council profile data in this checkout. Recorded per seat
#: and per phase by the engine so a run record can tell which profile revision
#: produced it. This is a DATA revision, not a bundle version: the release that
#: ships this revision is gate-tagged by the release readiness gate, not here.
COUNCIL_PROFILE_VERSION = "1.3.11"


# ── Provider Detection ──────────────────────────────────────────────

def detect_providers() -> dict[str, CouncilProvider]:
    """Auto-detect all available router providers.

    Checks environment variables, local config files, and default endpoints:
    - Faigate (FAIGATE_API_KEY or ~/.faigate or ~/.config/faigate/tokens.json)
    - OpenRouter (OPENROUTER_API_KEY)
    - ClawRouter (CLAWROUTER_API_KEY)
    - KiloRouter (KILO_API_KEY or KILO_BASE_URL)
    - OmniRoute (OMNIROUTE_API_KEY or localhost:20128)
    - 9router (NINEROUTER_API_KEY)

    Returns dict of {router_name: CouncilProvider}.
    """
    providers = {}

    # Faigate — check port + config files
    faigate_port = int(os.environ.get("FAIGATE_PORT", FAIGATE_DEFAULT_PORT))
    faigate_available = (
        os.environ.get("FAIGATE_API_KEY") or
        os.path.exists(os.path.expanduser("~/.faigate")) or
        os.path.exists(os.path.expanduser("~/.config/faigate/tokens.json")) or
        _check_port_open(FAIGATE_DEFAULT_HOST, faigate_port)
    )
    if faigate_available:
        providers["faigate"] = FaigateProvider()

    # OpenRouter
    if os.environ.get("OPENROUTER_API_KEY"):
        providers["openrouter"] = OpenRouterProvider()

    # ClawRouter
    if os.environ.get("CLAWROUTER_API_KEY"):
        providers["claw"] = GenericRouterProvider(
            base_url=os.environ.get("CLAWROUTER_BASE_URL", "https://api.clawrouter.ai/v1"),
            api_key=os.environ.get("CLAWROUTER_API_KEY"),
        )

    # KiloRouter
    if os.environ.get("KILO_API_KEY") or os.environ.get("KILO_BASE_URL"):
        providers["kilo"] = GenericRouterProvider(
            base_url=os.environ.get("KILO_BASE_URL", "https://api.kilorouter.com/v1")
        )

    # OmniRoute — local (default port 20128) or cloud
    omniroute_available = (
        os.environ.get("OMNIROUTE_API_KEY") or
        _check_port_open("localhost", int(os.environ.get("OMNIROUTE_PORT", "20128")))
    )
    if omniroute_available:
        providers["omniroute"] = GenericRouterProvider(
            base_url=os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128/v1"),
            api_key=os.environ.get("OMNIROUTE_API_KEY", "any-string"),
        )

    # 9router
    if os.environ.get("NINEROUTER_API_KEY"):
        providers["9router"] = GenericRouterProvider(
            base_url=os.environ.get("NINEROUTER_BASE_URL", "http://localhost:9200/v1"),
            api_key=os.environ.get("NINEROUTER_API_KEY"),
        )

    return providers


def get_best_provider() -> CouncilProvider:
    """Return the best available provider, or SingleModelProvider as fallback."""
    providers = detect_providers()
    # Priority: faigate > openrouter > omniroute > claw > kilo > 9router
    for name in ["faigate", "openrouter", "omniroute", "claw", "kilo", "9router"]:
        if name in providers:
            return providers[name]
    return SingleModelProvider()


def _check_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is open (for local providers like OmniRoute/faigate)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


# ── FaigateProvider ────────────────────────────────────────────────

FAIGATE_DEFAULT_HOST = "127.0.0.1"
FAIGATE_DEFAULT_PORT = "8090"


class FaigateProvider(CouncilProvider):
    def __init__(self, base_url: str | None = None, api_key: str | None = None, cache_ttl: float | None = None):
        super().__init__()
        self.base_url = (base_url or os.environ.get(
            "FAIGATE_BASE_URL",
            f"http://{os.environ.get('FAIGATE_HOST', FAIGATE_DEFAULT_HOST)}:"
            f"{os.environ.get('FAIGATE_PORT', FAIGATE_DEFAULT_PORT)}/v1"
        )).rstrip("/")
        self.api_key = api_key or os.environ.get("FAIGATE_API_KEY")
        if cache_ttl is not None:
            self._cache_ttl = cache_ttl
        # If no explicit key, try reading from ~/.config/faigate/tokens.json
        if not self.api_key:
            token_file = os.path.expanduser("~/.config/faigate/tokens.json")
            if os.path.exists(token_file):
                try:
                    tokens = json.loads(open(token_file).read())
                    for provider, config in tokens.items():
                        if "access_token" in config:
                            self.api_key = config["access_token"]
                            break
                except Exception:
                    pass

    def _req(self, path: str, method: str = "GET", body: dict | None = None, timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                if isinstance(result, dict) and "served_by" not in result:
                    served_by_header = resp.headers.get("X-Faigate-Served-By") or resp.headers.get("X-Served-By")
                    if served_by_header:
                        result["served_by"] = served_by_header
                return result
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            return {"error": f"HTTP {e.code} at {url}: {error_body}"}
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)) or "timed out" in str(e.reason):
                return {"error": f"timeout at {url}"}
            return {"error": f"URL Error at {url}: {e.reason}"}
        except Exception as e:
            return {"error": f"{str(e)} (at {url})"}

    def _fetch_models_sync(self, force_refresh: bool = False) -> list[ModelInfo]:
        """Fetch models synchronously from GET /models with caching."""
        if not force_refresh and self._is_cache_valid():
            return list(getattr(self, "_models_cache", []))

        info = self._req("/models")
        if info.get("error"):
            cache = getattr(self, "_models_cache", [])
            if cache:
                return list(cache)
            return []

        model_list = info if isinstance(info, list) else info.get("data", [])
        models: list[ModelInfo] = []
        for entry in model_list:
            if isinstance(entry, (dict, str)):
                models.append(parse_model_info(entry, provider=self.provider_name()))

        self._models_cache = models
        self._cache_timestamp = time.time()
        return list(models)

    async def fetch_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        """Query gateway for available models and their capabilities (cached)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._fetch_models_sync(force_refresh=force_refresh))

    def get_available_models(self, force_refresh: bool = False) -> list[str]:
        """Return list of available model ids."""
        models = self._fetch_models_sync(force_refresh=force_refresh)
        return [m.id for m in models if getattr(m, "available", True)]

    async def get_model_info(self, model: str, force_refresh: bool = False) -> Optional[ModelInfo]:
        """Return ModelInfo for a specific model id."""
        clean = translate_model_id(model, self.provider_name())
        models = await self.fetch_models(force_refresh=force_refresh)
        for m in models:
            if m.id == clean or m.id == model:
                return m
        return None

    def get_model_capabilities(self, model: str, force_refresh: bool = False) -> Optional[ModelInfo]:
        """Synchronously get ModelInfo with capabilities for a model id."""
        clean = translate_model_id(model, self.provider_name())
        models = self._fetch_models_sync(force_refresh=force_refresh)
        for m in models:
            if m.id == clean or m.id == model:
                return m
        return None

    async def check_availability(self, models: list[str]) -> dict[str, bool]:
        """Check model availability via Faigate GET /v1/models (single call with caching).

        Each model id is translated exactly once before matching; a malformed
        prefix raises :class:`ModelNamespaceError` rather than being silently
        stripped. A probe error fails open (UNVERIFIED), matching the resolution
        path's documented behaviour.
        """
        native = {}
        for m in models:
            native[m] = translate_model_id(m)

        loop = asyncio.get_event_loop()
        model_infos = await loop.run_in_executor(None, lambda: self._fetch_models_sync())
        if not model_infos:
            info = self._req("/models")
            if info.get("error"):
                return {m: True for m in models}  # fail open
            model_list = info if isinstance(info, list) else info.get("data", [])
            available_ids = set()
            for entry in model_list:
                if isinstance(entry, dict):
                    mid = entry.get("id", "")
                    if mid:
                        available_ids.add(mid)
                elif isinstance(entry, str):
                    available_ids.add(entry)
        else:
            available_ids = {info.id for info in model_infos if getattr(info, "available", True)}

        result = {}
        for m, clean in native.items():
            result[m] = clean in available_ids or any(clean in aid for aid in available_ids)
        return result

    async def check_credits(self, model: str) -> float:
        return -1.0  # Faigate doesn't expose credit checking — defer to availability

    async def query(self, model: str, messages: list[dict], temperature: float = 0.5, timeout: float | None = None) -> str:
        clean_model = translate_model_id(model)
        body = {"model": clean_model, "messages": messages, "temperature": temperature}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: (self._req("/chat/completions", "POST", body) if timeout is None
                     else self._req("/chat/completions", "POST", body, timeout)))
        if result.get("error"):
            if _is_rate_limit(result["error"]):
                raise RateLimitedError(clean_model)
            raise RuntimeError(f"Faigate query failed: {result['error']}")
        return _extract_answer(result, model, self.provider_name())

    def provider_name(self) -> str:
        return "faigate"


# ── OpenRouterProvider ──────────────────────────────────────────────

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class OpenRouterProvider(CouncilProvider):
    def __init__(self, base_url: str | None = None, api_key: str | None = None, cache_ttl: float | None = None):
        super().__init__()
        self.base_url = (base_url or OPENROUTER_BASE).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if cache_ttl is not None:
            self._cache_ttl = cache_ttl
        if self.api_key is None:
            raise ValueError("OPENROUTER_API_KEY not set")

    def _req(self, path: str, body: dict | None = None, method: str = "POST", timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": os.environ.get("OR_SITE_URL", "https://github.com/typelicious/SkillWeave"),
            "X-Title": os.environ.get("OR_APP_NAME", "SkillWeave Council"),
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                if "error" in result:
                    return {"error": result["error"].get("message", json.dumps(result["error"]))}
                return result
        except Exception as e:
            return {"error": _describe_error(e, url)}

    def _fetch_models_sync(self, force_refresh: bool = False) -> list[ModelInfo]:
        if not force_refresh and self._is_cache_valid():
            return list(getattr(self, "_models_cache", []))
        result = self._req("/models", method="GET")
        if result.get("error"):
            cache = getattr(self, "_models_cache", [])
            if cache:
                return list(cache)
            return []
        model_list = result.get("data", [])
        models: list[ModelInfo] = []
        for entry in model_list:
            if isinstance(entry, (dict, str)):
                models.append(parse_model_info(entry, provider=self.provider_name()))
        self._models_cache = models
        self._cache_timestamp = time.time()
        return list(models)

    async def fetch_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._fetch_models_sync(force_refresh=force_refresh))

    def get_available_models(self, force_refresh: bool = False) -> list[str]:
        models = self._fetch_models_sync(force_refresh=force_refresh)
        return [m.id for m in models if getattr(m, "available", True)]

    async def query(self, model: str, messages: list[dict], temperature: float = 0.5, timeout: float | None = None) -> str:
        clean_model = translate_model_id(model, "openrouter")
        body = {"model": clean_model, "messages": messages, "temperature": temperature}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: (self._req("/chat/completions", body) if timeout is None
                     else self._req("/chat/completions", body, timeout=timeout)))
        if result.get("error"):
            raise RuntimeError(f"OpenRouter query failed: {result['error']}")
        return _extract_answer(result, model, self.provider_name())

    async def check_availability(self, models: list[str]) -> dict[str, bool]:
        """OpenRouter: check model availability via /models endpoint (cached)."""
        try:
            model_infos = await self.fetch_models()
            available_ids = {m.id for m in model_infos if getattr(m, "available", True)}
            if not available_ids:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._req("/models", method="GET")
                )
                available_ids = {m.get("id", "") for m in result.get("data", [])}
            results = {}
            for m in models:
                clean_m = translate_model_id(m, "openrouter")
                results[m] = (clean_m in available_ids or any(clean_m in a for a in available_ids))
            return results
        except Exception:
            return {m: True for m in models}

    def provider_name(self) -> str:
        return "openrouter"


# ── GenericRouterProvider (Kilo, Claw, OmniRoute, 9router) ────────

class GenericRouterProvider(CouncilProvider):
    """OpenAI-compatible router: works with any /v1 endpoint."""

    def __init__(self, base_url: str, api_key: str | None = None, cache_ttl: float | None = None):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        if cache_ttl is not None:
            self._cache_ttl = cache_ttl

    def _req(self, path: str, body: dict | None = None, method: str = "POST", timeout: float | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                if "error" in result:
                    return {"error": result["error"].get("message", str(result["error"]))}
                return result
        except Exception as e:
            return {"error": _describe_error(e, url)}

    def _fetch_models_sync(self, force_refresh: bool = False) -> list[ModelInfo]:
        if not force_refresh and self._is_cache_valid():
            return list(getattr(self, "_models_cache", []))
        result = self._req("/models", method="GET")
        if result.get("error"):
            cache = getattr(self, "_models_cache", [])
            if cache:
                return list(cache)
            return []
        model_list = result if isinstance(result, list) else result.get("data", [])
        models: list[ModelInfo] = []
        for entry in model_list:
            if isinstance(entry, (dict, str)):
                models.append(parse_model_info(entry, provider=self.provider_name()))
        self._models_cache = models
        self._cache_timestamp = time.time()
        return list(models)

    async def fetch_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._fetch_models_sync(force_refresh=force_refresh))

    def get_available_models(self, force_refresh: bool = False) -> list[str]:
        models = self._fetch_models_sync(force_refresh=force_refresh)
        return [m.id for m in models if getattr(m, "available", True)]

    async def query(self, model: str, messages: list[dict], temperature: float = 0.5, timeout: float | None = None) -> str:
        clean_model = translate_model_id(model, "openrouter")
        body = {"model": clean_model, "messages": messages, "temperature": temperature}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: (self._req("/chat/completions", body) if timeout is None
                     else self._req("/chat/completions", body, timeout=timeout)))
        if result.get("error"):
            raise RuntimeError(f"Router query failed: {result['error']}")
        return _extract_answer(result, model, self.provider_name())

    async def check_availability(self, models: list[str]) -> dict[str, bool]:
        """Try /models endpoint, fall back to assuming all available (cached)."""
        try:
            model_infos = await self.fetch_models()
            available_ids = {m.id for m in model_infos if getattr(m, "available", True)}
            if not available_ids:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._req("/models", method="GET")
                )
                available_ids = {m.get("id", "") for m in result.get("data", [])}
            return {m: (m in available_ids or any(m in a for a in available_ids)) for m in models}
        except Exception:
            return {m: True for m in models}

    def provider_name(self) -> str:
        return "generic_router"


# ── SingleModelProvider (Fallback — no router, 1 model) ─────────────

class SingleModelProvider(CouncilProvider):
    """No router detected — use a single model for all council roles.

    Council modes adapt:
    - quick/standard: model answers solo (no peer review)
    - full: model acts as both council + chairman (solo deliberation)

    Works with any OpenAI-compatible API via:
    - OPENAI_API_KEY → https://api.openai.com/v1
    - COUNCIL_MODEL / OPENAI_MODEL for model name
    """

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        super().__init__()
        self.model = model or os.environ.get(
            "COUNCIL_MODEL",
            os.environ.get("OPENAI_MODEL", os.environ.get("DEFAULT_MODEL", "gpt-3.5-turbo"))
        )
        self.base_url = (base_url or os.environ.get(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1"
        )).rstrip("/")
        self.api_key = (
            api_key or
            os.environ.get("OPENAI_API_KEY") or
            os.environ.get("ANTHROPIC_API_KEY")
        )
        self._single_model_mode = True

    def _req(self, body: dict, timeout: float | None = None) -> dict:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                if "error" in result:
                    return {"error": result["error"].get("message", str(result["error"]))}
                return result
        except Exception as e:
            return {"error": _describe_error(e, url)}

    async def fetch_models(self, force_refresh: bool = False) -> list[ModelInfo]:
        return [
            ModelInfo(
                id=self.model,
                name=self.model,
                provider=self.provider_name(),
                available=True,
                context_window=128000,
                cost_per_1k=0.0,
                cost=0.0,
                reasoning=False,
                capabilities=[],
            )
        ]

    def get_available_models(self, force_refresh: bool = False) -> list[str]:
        return [self.model]

    async def query(self, model: str, messages: list[dict], temperature: float = 0.5, timeout: float | None = None) -> str:
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: self._req(body, timeout))
        if result.get("error"):
            raise RuntimeError(f"Model query failed: {result['error']}")
        # SingleModelProvider always names itself; still read the envelope so
        # any router in front of it (custom OPENAI_BASE_URL) is attributed too.
        return _extract_answer(result, self.model, self.provider_name())

    async def check_availability(self, models: list[str]) -> dict[str, bool]:
        return {m: True for m in models}

    def provider_name(self) -> str:
        return "single_model"

    @property
    def is_single_model(self) -> bool:
        return True


# ── Profile Helpers ─────────────────────────────────────────────────

def get_profile(profile_name: str = "default") -> dict:
    if profile_name in ROUTER_PROFILES:
        return dict(ROUTER_PROFILES[profile_name])
    return dict(ROUTER_PROFILES["default"])


def list_profiles() -> list[str]:
    return list(ROUTER_PROFILES.keys())


def list_detected_providers() -> dict[str, str]:
    return {name: p.provider_name() for name, p in detect_providers().items()}


def fetch_available_models(
    provider_name: str = "faigate",
    force_refresh: bool = False,
) -> list[ModelInfo]:
    """Fetch available models and their capabilities from the detected or specified provider."""
    providers = detect_providers()
    provider = providers.get(provider_name) or get_best_provider()
    if hasattr(provider, "_fetch_models_sync"):
        return provider._fetch_models_sync(force_refresh=force_refresh)
    if isinstance(provider, SingleModelProvider):
        return [
            ModelInfo(
                id=provider.model,
                name=provider.model,
                provider=provider.provider_name(),
                available=True,
            )
        ]
    return []


def get_model_capabilities(
    model: str,
    provider_name: str = "faigate",
    force_refresh: bool = False,
) -> Optional[ModelInfo]:
    """Get parsed capabilities for a specific model."""
    models = fetch_available_models(provider_name=provider_name, force_refresh=force_refresh)
    clean = translate_model_id(model, provider_name)
    for m in models:
        if m.id == clean or m.id == model:
            return m
    return None


def clear_model_cache() -> None:
    """Clear cached models across all detected providers."""
    providers = detect_providers()
    for p in providers.values():
        if hasattr(p, "clear_cache"):
            p.clear_cache()


def known_model_ids() -> frozenset[str]:
    """Return every model id the council casts, across all router presets.

    This is the council's casting surface, derived from ``ROUTER_PROFILES``, not
    a live provider probe: every model any preset casts for a council seat. It
    is NOT the availability gate — availability is resolved against what Faigate
    actually serves (see :func:`_check_unavailable_models`). A model id may be
    absent here and still be a real, resolvable Faigate model (e.g. a pinned id
    not cast by any preset).
    """
    ids = {model for preset in ROUTER_PROFILES.values() for model in preset["models"]}
    return frozenset(ids)


@dataclass
class ModelResolution:
    """What a ``ModelSpec`` resolved to, with the request kept beside it.

    ``requested`` is the spec as handed in; ``resolved`` is the concrete model id
    the adapter produced. Keeping both means evidence never fabricates a model:
    a later reader can always tell "this id was asked for, this id was produced".
    """

    requested: "object"
    resolved: str

    def to_dict(self) -> dict:
        return {
            "requested": self.requested.to_dict() if hasattr(self.requested, "to_dict") else self.requested,
            "resolved": self.resolved,
        }


def resolve_model_spec(spec: "object") -> str:
    """Resolve a ``ModelSpec`` to a concrete model id (deterministic + recorded).

    A ``concrete`` spec returns its model id unchanged. A ``delegated`` spec names
    a router and a scenario; the adapter resolves them to the concrete header the
    router actually uses, reusing the EXISTING provider detection/mapping — no new
    provider is invented:

    * ``faigate`` — the concrete model is the scenario id itself, carried through
      the ``X-faigate-Prefer-Provider`` / ``X-faigate-Mode`` headers already
      documented in ``~/.config/opencode/opencode.json``. Delegation to faigate is
      done via those headers; a scenario like ``"auto"`` names the routing mode,
      while a scenario like ``"coding-fast"`` names a provider/header preference.
      The header mapping lives at the transport, not in this pure function.
    * any router — the resolved id is ``<router>:<scenario>`` so the concrete
      provider path is named and deterministic. The router prefix preserves the
      delegation identity: ``delegated('faigate','auto')`` and
      ``delegated('omniroute','auto')`` never collapse to the same resolved
      string, even though their scenarios match.

    The function is pure and deterministic given the spec + the provider list: no
    network call, no wall-clock, no global mutable state. It records what it
    resolved (requested vs resolved) through the returned string's provenance, but
    the return type is a plain ``str`` so callers keep the existing model-string
    contract. To surface the record, call :func:`resolve_model_spec_record`.
    """
    kind = getattr(spec, "kind", None)
    if kind == "concrete":
        return spec.model
    if kind == "delegated":
        router = spec.router
        scenario = spec.scenario
        # Resolve to a router-scoped id that never collapses distinct
        # (router, scenario) pairs. Two fan-out children delegated to different
        # routers with the same scenario must yield different resolved ids; a
        # bare scenario would collapse them. The adapter re-reads the router
        # prefix at the transport (the header/mode mapping lives there), so the
        # identity is preserved end to end.
        return f"{router}:{scenario}"
    raise ValueError(f"cannot resolve model spec of unknown kind {kind!r}")


def resolve_model_spec_record(spec: "object") -> ModelResolution:
    """Resolve a spec and keep the request beside the product (for evidence)."""
    return ModelResolution(requested=spec, resolved=resolve_model_spec(spec))


def resolve_tier(profile: "RoutingProfile") -> "ResolutionRecord":
    """Resolve a profile's tier into the models that will actually run (AK 8+9).

    The tier names *intent*, not a model. Faigate's ``ROUTER_PROFILES`` decide
    the concrete model pool and council mode, so the profile stays valid when a
    preset's models change. A profile MAY pin a single concrete model id; in that
    case the pinned model is what runs, and the returned record is marked
    ``pinned`` so a later run can tell the difference between "what was
    requested" and "what really ran".

    Availability of a pinned or declared model is resolved against the live
    ``/v1/models`` roster, not against ``ROUTER_PROFILES``. ``ROUTER_PROFILES``
    keep their own job — casting the council (chairman + model pool + mode) per
    tier — and are not repurposed as an availability registry. The roster is
    the best reachable availability signal but is not proof of what Faigate
    actually answers: measured, a non-self-answering id is still silently
    substituted for ``deepseek-v4-flash``. The council's seats are therefore
    named provider-native (the two self-answering roster ids) directly in
    ``ROUTER_PROFILES``, so no substitution is hidden behind a cast. Judging an
    answer against the requested model is therefore out of scope here
    (SW-COUNCIL-001); this gate only refuses an id whose absence Faigate itself
    reports via the roster.

    The availability outcome is one of three: a model Faigate confirms it
    serves proceeds; a model Faigate confirms it does NOT serve is refused
    (naming the profile and the role); and when no authoritative source is
    reachable, the model is left UNVERIFIED rather than refused, and the
    resolution does not claim Faigate cannot resolve it.

    Returns a :class:`~skillweave.routing.profile.ResolutionRecord` carrying the
    requested tier, the router preset name, the council mode, the resolved model
    ids, and any pin.
    """
    from .profile import ResolutionRecord, RoutingProfileError, tier_to_router

    router_name, mode = tier_to_router(profile.tier)
    preset = ROUTER_PROFILES.get(router_name, ROUTER_PROFILES["default"])

    _check_unavailable_models(profile)

    pin = _profile_pin(profile)
    resolved_models = [pin] if pin else list(preset["models"])

    return ResolutionRecord(
        tier=profile.tier,
        router_name=router_name,
        mode=mode,
        resolved_models=resolved_models,
        pinned=pin,
    )


def _check_unavailable_models(profile: "RoutingProfile") -> None:
    """Refuse a declared model or pin only when Faigate confirms it cannot serve.

    A role's ``model`` and ``pin`` name a concrete model id. ``ROUTER_PROFILES``
    (``known_model_ids()``) keep their own job — casting the council's seats — so
    a cast id is admitted as-is and is never live-gated. Cast ids are the
    provider-native self-answering roster ids (``deepseek-v4-pro``,
    ``deepseek-v4-flash``); every id outside that cast is an explicit override
    whose availability is resolved against what Faigate actually serves, with
    three outcomes:

    * in the live roster    -> proceed;
    * confirmed not served  -> refuse, naming the profile, the role, and the id;
    * unreachable           -> UNVERIFIED: do NOT refuse, and do not claim
      Faigate cannot resolve the id (the source that would have proved that is
      not reachable).

    ``known_model_ids()`` is therefore no longer the availability gate: it can
    never refuse, only short-circuit the council's own seats.
    """
    from .profile import RoutingProfileError

    cast = known_model_ids()
    declared = [
        (key, field, value)
        for key, role in profile.roles.items()
        for field, value in (("model", role.model), ("pin", role.pin))
        if value is not None and value not in cast
    ]
    if not declared:
        return

    provider = detect_providers().get("faigate")
    if provider is None or not isinstance(provider, FaigateProvider):
        # No authoritative source is reachable: the model cannot be verified,
        # but there is no proof it is unavailable, so it is left UNVERIFIED and
        # the resolution proceeds rather than refusing on a static guess.
        return

    models = [value for _, _, value in declared]

    # check_availability is async (it drives a network probe). In the
    # resolution path we need its result synchronously; run the probe on a
    # short-lived loop. A network failure or unreachable endpoint surfaces as
    # fail-open availability (every model reported True), which is exactly the
    # UNVERIFIED outcome: no refusal, and no false "cannot resolve" claim.
    availability = asyncio.run(provider.check_availability(models))

    for key, field, value in declared:
        if value not in availability:
            # The probe returned no verdict for this id: treat as UNVERIFIED.
            continue
        if not availability[value]:
            raise RoutingProfileError(
                f"profile '{profile.name}' role '{key}' names {field} "
                f"'{value}' which Faigate reports unavailable"
            )


def _profile_pin(profile: "RoutingProfile") -> Optional[str]:
    """Return the single pinned model id for a profile, if any role pins one.

    Pinning is per-role data (each ``RoleDefinition`` may carry ``pin``). A
    profile where exactly one role pins a model is treated as pinned. If
    several roles pin *different* models, there is no single resolution — that
    contradiction is reported, not silently collapsed, so the caller can see it.
    """
    pins = {r.pin for r in profile.roles.values() if r.is_pinned}
    if not pins:
        return None
    if len(pins) == 1:
        return next(iter(pins))
    return None
