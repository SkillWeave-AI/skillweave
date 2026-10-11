"""Perplexity MCP launcher resolution (SW-COUNCIL-003, criterion 2).

The addendum's Perplexity MCP defect launched the server from a *floating* git
install. This module replaces that with a **resolver** that names exactly which
binary is executed, in a fixed, auditable precedence:

    1. an **explicit launcher config** (``SearchConfig.mcp_command``) -- the
       caller names the command; it wins over everything;
    2. a **registered compatible runtime** -- a package registered with this
       resolver (Capacium supplies **launcher metadata only**), whose exact
       distribution/version is pinned;
    3. a **verified installed binary** -- a console script resolved on ``PATH``
       whose distribution, version and origin are read from installed metadata
       and recorded as provenance;
    4. a **uvx** fallback -- only with an **exact** distribution/version
       (``name==version``), an **explicit** network permission
       (``allow_network=True``) and a **recorded** provenance. A uvx package
       spec without a pinned version is refused.

Nothing here installs, downloads or executes a floating ``git+``/URL install:
a command that would (a ``git+``/``http(s)`` marker, or an unpinned ``uvx``
spec) is refused *before* any process is spawned. An explicit ``--from``/``-f``
provenance hint wins over an embedded marker -- so a local mirror path that
merely *contains* the substring ``http`` is still a legitimate explicit pin.

The resolver is pure: it reads installed metadata but launches nothing. The
chosen command and its provenance become the *argument* the MCP backend
executes and the *receipt* the facade records.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "FLOATING_MARKERS",
    "Registration",
    "UvxFallback",
    "LauncherResolution",
    "LauncherResolutionError",
    "is_floating_command",
    "default_registrations",
    "register",
    "clear_registrations",
    "resolve_launcher",
    "mcp_argument_from_resolution",
]

#: Markers that betray a floating install. Matched against the *effective* spec
#: only -- an explicit ``--from``/``-f`` provenance hint is stripped first, so a
#: local path that merely contains the substring ``http`` is not misread.
FLOATING_MARKERS: tuple[str, ...] = ("git+", "http://", "https://")

#: ``name==version`` / ``name@version`` -- an *exact* pin. A bare ``name`` or a
#: range (``name>=1``) is not a pin and cannot be a uvx fallback.
_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?==[^=\s]+$")
_AT_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]+\])?@[^@\s]+$")

#: Interpreters that may front a ``-m module`` launch; only these are treated
#: as the launcher when a module is named.
_INTERPRETER_BASENAMES = ("python", "python3", "uv", "uvx")


class LauncherResolutionError(ValueError):
    """No admissible launcher was found, or the request was malformed.

    ``asset`` names the blocking fact so the refusal is attributable rather
    than a bare "no launcher".
    """

    def __init__(self, message: str, *, asset: Optional[str] = None) -> None:
        super().__init__(message)
        self.asset = asset


@dataclass(frozen=True)
class Registration:
    """A known-compatible MCP launcher runtime, registered by a provider.

    ``launch`` is the base command (e.g. ``["capacium-mcp"]`` or
    ``["python3", "-m", "capacium_mcp.server"]``). ``distribution`` /
    ``version`` name the exact installed distribution the base command belongs
    to -- never a floating version directory. ``distribution`` is optional for
    an explicitly-registered local runtime whose provenance the caller owns.
    """

    id: str
    launch: tuple[str, ...]
    distribution: Optional[str] = None
    version: Optional[str] = None
    transport: str = "mcp-stdio"
    source: str = "registered"
    default_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise LauncherResolutionError("a registration needs a non-empty id", asset="id")
        launch = tuple(str(p) for p in self.launch)
        if not launch:
            raise LauncherResolutionError(
                f"registration {self.id!r} needs a non-empty launch command", asset="launch"
            )
        object.__setattr__(self, "launch", launch)
        object.__setattr__(self, "default_args", tuple(str(a) for a in self.default_args))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "launch": list(self.launch),
            "transport": self.transport,
            "source": self.source,
        }
        if self.distribution:
            payload["distribution"] = self.distribution
        if self.version:
            payload["version"] = self.version
        if self.default_args:
            payload["default_args"] = list(self.default_args)
        return payload


@dataclass(frozen=True)
class UvxFallback:
    """An explicit uvx fallback: an exact pin plus an explicit network grant."""

    package: str            # ``name==version`` (exact distribution/version)
    allow_network: bool     # the *explicit* permission to reach the network
    provenance: str         # a recorded, human-readable source of the pin

    def __post_init__(self) -> None:
        if not (self.package and _is_pinned(self.package)):
            raise LauncherResolutionError(
                f"uvx fallback package {self.package!r} must be pinned as "
                "'name==version' (an exact distribution and version)",
                asset="uvx.package",
            )
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise LauncherResolutionError(
                "a uvx fallback must record its provenance", asset="uvx.provenance"
            )
        if is_floating_command([self.package], uvx_spec=self.package):
            raise LauncherResolutionError(
                f"uvx fallback package {self.package!r} resolves to a floating spec",
                asset="uvx.package",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "allow_network": self.allow_network,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class LauncherResolution:
    """The resolved launcher: an exact command, its provenance and how chosen."""

    command: tuple[str, ...]
    kind: str                       # "explicit" | "registered" | "installed-bin" | "uvx"
    provenance: Mapping[str, Any]
    transport: str = "mcp-stdio"

    def with_args(self, extra: Sequence[str]) -> "LauncherResolution":
        return LauncherResolution(
            command=tuple(self.command) + tuple(str(a) for a in extra),
            kind=self.kind,
            provenance=dict(self.provenance),
            transport=self.transport,
        )


# ── floating-spec detection ────────────────────────────────────────────────


def _is_pinned(spec: str) -> bool:
    return bool(_PIN_RE.match(spec) or _AT_PIN_RE.match(spec))


def _effective_spec(part: str) -> str:
    """Strip an explicit ``--from``/``-f`` provenance hint from a uvx spec.

    ``"pkg @ --from /mirror/pkg.whl"`` -> ``"/mirror/pkg.whl"``. The hint is an
    explicit local provenance, so it is what must be judged -- not the package
    token that precedes it.
    """
    text = str(part)
    for flag in (" --from ", " --from=", " -f ", " -f="):
        if flag in text:
            return text.split(flag, 1)[1].strip()
    return text


def is_floating_command(
    command: Sequence[str], *, uvx_spec: Optional[str] = None
) -> bool:
    """Whether a launch command would install floating code.

    A ``git+``/``http(s)`` marker anywhere in the *effective* spec, or an
    unpinned ``uvx`` package spec, is floating. An explicit ``-f``/``--from``
    hint is stripped before the marker scan.
    """
    for part in command:
        effective = _effective_spec(part)
        for marker in FLOATING_MARKERS:
            if marker in effective:
                return True
    if uvx_spec is not None:
        spec = _effective_spec(uvx_spec)
        if not _is_pinned(spec):
            return True
    return False


# ── projections ────────────────────────────────────────────────────────────


def _base_provenance(
    *, kind: str, registration: Optional[Registration], detail: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "source": (registration.source if registration is not None else "explicit_config"),
        "detail": detail,
    }
    if registration is not None:
        payload["registration_id"] = registration.id
        if registration.distribution:
            payload["distribution"] = registration.distribution
        if registration.version:
            payload["version"] = registration.version
    return payload


def _launcher_of(command: Sequence[str], registration: Optional[Registration]) -> str:
    """The launcher token: the first element, or the interpreter before ``-m``."""
    parts = [str(p) for p in command]
    if not parts:
        return ""
    if len(parts) >= 2 and parts[1] == "-m":
        return parts[0]
    return parts[0]


# ── the registry of known-compatible runtimes ──────────────────────────────

_REGISTRY: dict[str, Registration] = {}


def register(registration: Registration) -> Registration:
    """Register a known-compatible launcher runtime (idempotent by id).

    Re-registering the *same* command for an id is a no-op; re-registering a
    *different* command under an existing id is refused, so a registration
    cannot be silently shadowed.
    """
    if not isinstance(registration, Registration):
        raise LauncherResolutionError(
            "register() requires a Registration", asset="registration"
        )
    existing = _REGISTRY.get(registration.id)
    if existing is not None and existing.launch != registration.launch:
        raise LauncherResolutionError(
            f"launcher id {registration.id!r} is already registered with a "
            "different launch command",
            asset="id",
        )
    _REGISTRY[registration.id] = registration
    return registration


def clear_registrations() -> None:
    _REGISTRY.clear()


def default_registrations() -> list[Registration]:
    """The source-owned known-compatible runtimes.

    Capacium appears here as **launcher metadata only**: the id, the real
    console script, and the distribution/version its installed metadata must
    match. Nothing from Capacium is imported to build this list.
    """
    return [
        Registration(
            id="capacium-mcp",
            launch=("capacium-mcp",),
            distribution="capacium-mcp",
            version="0.3.0",
            transport="mcp-stdio",
            source="capacium:launcher-metadata",
        ),
    ]


for _registration in default_registrations():
    register(_registration)


# ── resolution ─────────────────────────────────────────────────────────────


def _with_resolved_launcher(
    launch: Sequence[str], resolved: Optional[str]
) -> tuple[str, ...]:
    """Substitute the *resolved* launcher path for the bare launcher token.

    A registration declares a bare console-script name (``"capacium-mcp"``); the
    resolver located a concrete path for it. Executing the path (never the bare
    name, which can pick up a different ``PATH``) is the point of "verified
    installed binary".
    """
    parts = [str(p) for p in launch]
    if not parts or not resolved:
        return tuple(parts)
    parts[0] = str(resolved)
    return tuple(parts)


def _installed_version(distribution: str) -> Optional[str]:
    try:
        return importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _installed_origin(launcher: str, search_path: Optional[str] = None) -> Optional[str]:
    found = shutil.which(launcher, path=search_path)
    if found:
        return os.path.realpath(found)
    return launcher if os.path.exists(launcher) else None


def _verify_registration(
    registration: Registration,
    *,
    verify_installed: bool,
    search_path: Optional[str],
) -> Optional[tuple[str, dict[str, Any]]]:
    """Return ``(launcher_path, provenance)`` when the registration is verified.

    ``None`` means "not verified" -- the caller continues to the next step.
    The distribution's *exact* pinned version is required when the registration
    names one; a launcher binary must resolve on ``PATH`` (or exist as a path).
    """
    launcher = _launcher_of(registration.launch, registration)
    location = None
    if shutil.which(launcher, path=search_path) or os.path.exists(launcher):
        location = _installed_origin(launcher, search_path)
    provenance = _base_provenance(
        kind="registered",
        registration=registration,
        detail=(
            f"registered runtime {registration.id!r} resolved at {location!r}"
            if location
            else f"registered runtime {registration.id!r} is not installed"
        ),
    )
    if registration.distribution:
        observed = _installed_version(registration.distribution)
        provenance["observed_version"] = observed
        provenance["expected_version"] = registration.version
        if observed is None:
            return None
        if registration.version and observed != registration.version:
            return None
    if location is None:
        if verify_installed and registration.distribution:
            # The distribution metadata is present but the console script is
            # not: not usable as-is.
            return None
        if verify_installed:
            return None
    if location is not None:
        provenance["location"] = location
    return location or launcher, provenance


def resolve_launcher(
    *,
    explicit_command: Optional[Sequence[str]] = None,
    explicit_provenance: Optional[Mapping[str, Any]] = None,
    registration_id: Optional[str] = None,
    registrations: Optional[Sequence[Registration]] = None,
    uvx_fallback: Optional[UvxFallback] = None,
    verify_installed: bool = True,
    search_path: Optional[str] = None,
) -> LauncherResolution:
    """Resolve the launcher to execute, or fail closed with an attributable asset.

    Precedence: explicit config -> registered runtime -> verified installed
    binary -> uvx fallback (pinned + explicit network + provenance). A uvx
    fallback is never attempted without the explicit network grant; a floating
    or unpinned command is refused at every step.
    """
    # 1. explicit launcher config ------------------------------------------
    if explicit_command:
        command = tuple(str(p) for p in explicit_command)
        # An explicit binding may legitimately redirect to a local mirror via
        # ``-f``/``--from``; that explicit provenance is honoured.
        uvx_spec = command[-1] if command and command[0].endswith("uvx") else None
        if is_floating_command(command, uvx_spec=uvx_spec):
            raise LauncherResolutionError(
                "explicit launcher command is a floating install; an explicit "
                "launcher must name a pinned local/installed command",
                asset="explicit_command",
            )
        provenance = dict(explicit_provenance or {})
        provenance.setdefault("kind", "explicit")
        provenance.setdefault("source", "explicit_config")
        provenance.setdefault("detail", "explicit launcher config named the command")
        return LauncherResolution(
            command=command, kind="explicit", provenance=provenance
        )

    # 2. registered compatible runtime -------------------------------------
    candidates: list[Registration] = []
    if registration_id is not None:
        found = _REGISTRY.get(str(registration_id))
        if found is None:
            raise LauncherResolutionError(
                f"no launcher runtime registered with id {registration_id!r}; "
                f"registered: {sorted(_REGISTRY)}",
                asset="registration_id",
            )
        candidates = [found]
    elif registrations is not None:
        candidates = list(registrations)
    else:
        candidates = [r for r in _REGISTRY.values() if r.id == "capacium-mcp"]

    for registration in candidates:
        verified = _verify_registration(
            registration, verify_installed=verify_installed, search_path=search_path
        )
        if verified is None:
            continue
        launcher_path, provenance = verified
        return LauncherResolution(
            command=_with_resolved_launcher(registration.launch, launcher_path)
            + tuple(registration.default_args),
            kind="registered",
            provenance=provenance,
            transport=registration.transport,
        )

    # 3. verified installed binary -----------------------------------------
    if registration_id is None:
        for registration in _REGISTRY.values():
            launcher = _launcher_of(registration.launch, registration)
            location = shutil.which(launcher, path=search_path)
            if location is None:
                continue
            if registration.distribution:
                observed = _installed_version(registration.distribution)
                if observed is None or (
                    registration.version and observed != registration.version
                ):
                    continue
            provenance = _base_provenance(
                kind="installed-bin",
                registration=registration,
                detail=f"installed binary {launcher!r} resolved on PATH",
            )
            provenance["location"] = os.path.realpath(location)
            if registration.distribution:
                provenance["distribution"] = registration.distribution
                provenance["version"] = registration.version
                provenance["observed_version"] = _installed_version(
                    registration.distribution
                )
            return LauncherResolution(
                command=_with_resolved_launcher(tuple(registration.launch), location)
                + tuple(registration.default_args),
                kind="installed-bin",
                provenance=provenance,
                transport=registration.transport,
            )

    # 4. uvx fallback (exact pin + explicit network + recorded provenance) ----
    if uvx_fallback is not None:
        if not uvx_fallback.allow_network:
            raise LauncherResolutionError(
                "a uvx fallback requires an explicit network permission "
                "(allow_network=True); the default is deny",
                asset="uvx.network",
            )
        uvx = shutil.which("uvx", path=search_path)
        if uvx is None:
            raise LauncherResolutionError(
                "uvx fallback requested but no 'uvx' executable is installed",
                asset="uvx.binary",
            )
        if is_floating_command(["uvx", "--from", uvx_fallback.package],
                               uvx_spec=uvx_fallback.package):
            raise LauncherResolutionError(
                f"uvx fallback package {uvx_fallback.package!r} is not an exact pin",
                asset="uvx.package",
            )
        provenance = {
            "kind": "uvx",
            "source": "uvx_fallback",
            "package": uvx_fallback.package,
            "allow_network": True,
            "provenance": uvx_fallback.provenance,
            "detail": f"explicit uvx fallback pinned to {uvx_fallback.package!r}",
        }
        return LauncherResolution(
            command=(uvx, "--from", uvx_fallback.package, _distribution_name(uvx_fallback.package)),
            kind="uvx",
            provenance=provenance,
        )

    raise LauncherResolutionError(
        "no launcher could be resolved: no explicit command, no verified "
        "registered runtime, no verified installed binary and no admissible "
        "uvx fallback; a floating install is never constructed",
        asset="launcher",
    )


def _distribution_name(spec: str) -> str:
    """The bare distribution name of a pinned spec (``name==1`` -> ``name``)."""
    for sep in ("==", "@"):
        if sep in spec:
            return spec.split(sep, 1)[0]
    return spec


def mcp_argument_from_resolution(
    resolution: LauncherResolution, base: Optional[Mapping[str, Any]] = None
) -> dict[str, Any]:
    """Project a resolution into the MCP backend argument mapping."""
    argument: dict[str, Any] = dict(base or {})
    argument["command"] = list(resolution.command)
    argument["launcher_kind"] = resolution.kind
    argument["launcher_provenance"] = dict(resolution.provenance)
    argument.pop("launcher_resolution", None)
    return argument
