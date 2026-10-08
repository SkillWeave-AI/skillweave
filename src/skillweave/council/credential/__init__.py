"""Credential reference resolver for owned MCP child environments (SW-MCP-001).

The resolver answers one question: is a named credential reference *available*?
It never reads, stores, or transmits the credential value. The value is injected
only into an owned child process environment by the launcher --- never into
arguments, exports, logs, evidence or persisted configuration.

envctl is an optional host agent for sourcing environment variables. The resolver
does not require envctl to be installed; it falls back to environment-variable
presence when envctl is absent. No secret value is ever written into a fixture,
a receipt, or a persisted config file.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Mapping, Optional

__all__ = [
    "CredentialResolver",
    "CredentialResolution",
    "ENVCTL_COMMAND",
    "resolve_envctl_available",
]

#: The optional envctl command name, used when available.
ENVCTL_COMMAND = "envctl"


@dataclass(frozen=True)
class CredentialResolution:
    """One credential reference and its observed availability.

    ``reference_id`` is the key used in adapter descriptors (e.g.
    ``PERPLEXITY_COOKIES``). ``available`` is true when the value can be
    injected into an owned child environment. ``source`` records where the
    resolution came from (``"env"``, ``"envctl"``, ``"fixture"``).
    """

    reference_id: str
    available: bool
    source: str = "env"
    variable_names: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "available": self.available,
            "source": self.source,
            "variable_names": list(self.variable_names),
        }


def resolve_envctl_available() -> bool:
    """Whether the optional envctl command is present on this host.

    envctl is not a mandatory installation dependency. Returns False when
    envctl is not found, so callers can fall back to environment-variable
    presence.
    """
    return shutil.which(ENVCTL_COMMAND) is not None


class CredentialResolver:
    """Resolves credential references without leaking secret values.

    The resolver checks whether a named reference is *available* (i.e., the
    corresponding environment variable is set) without reading, storing, or
    logging the value. It does not require envctl.

    Usage::

        resolver = CredentialResolver()
        resolution = resolver.resolve("PERPLEXITY_COOKIES")
        if resolution.available:
            # The value exists in the environment; the launcher will inject it
            # into the owned child process.
            pass
    """

    def __init__(
        self,
        *,
        env_overrides: Optional[Mapping[str, str]] = None,
        allow_envctl: bool = False,
    ) -> None:
        """Create a resolver.

        Args:
            env_overrides: Override environment for testing (maps var name to
                value). When None, reads from the real process environment.
                Values are accepted only for *availability* checks; they are
                never persisted or logged.
            allow_envctl: Whether to attempt envctl resolution when the
                environment variable is not found.
        """
        self._env_overrides = dict(env_overrides) if env_overrides else None
        self._allow_envctl = allow_envctl and resolve_envctl_available()

    def resolve(self, reference_id: str) -> CredentialResolution:
        """Check whether a credential reference is available.

        Args:
            reference_id: The credential reference identifier (e.g.
                ``PERPLEXITY_COOKIES``, ``PERPLEXITY_API_KEY``).

        Returns:
            A CredentialResolution with the observed availability.

        The value is never logged, stored, or returned.
        """
        env = self._env_overrides if self._env_overrides is not None else os.environ
        var_names = self._variable_names_for(reference_id)

        for var_name in var_names:
            value = env.get(var_name)
            if value is not None and len(value) > 0:
                return CredentialResolution(
                    reference_id=reference_id,
                    available=True,
                    source="env",
                    variable_names=(var_name,),
                )

        if self._allow_envctl:
            for var_name in var_names:
                try:
                    import subprocess
                    result = subprocess.run(
                        [ENVCTL_COMMAND, "get", var_name],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return CredentialResolution(
                            reference_id=reference_id,
                            available=True,
                            source="envctl",
                            variable_names=(var_name,),
                        )
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                    pass

        return CredentialResolution(
            reference_id=reference_id,
            available=False,
            source="env",
            variable_names=var_names,
        )

    def resolve_all(
        self, reference_ids: tuple[str, ...]
    ) -> dict[str, CredentialResolution]:
        """Resolve multiple references at once."""
        return {rid: self.resolve(rid) for rid in reference_ids}

    @staticmethod
    def _variable_names_for(reference_id: str) -> tuple[str, ...]:
        """Return the likely environment variable names for a reference."""
        mapping = {
            "PERPLEXITY_COOKIES": ("PERPLEXITY_COOKIES",),
            "PERPLEXITY_API_KEY": ("PERPLEXITY_API_KEY",),
            "SERPER_API_KEY": ("SERPER_API_KEY",),
            "SERPAPI_API_KEY": ("SERPAPI_API_KEY",),
            "TAVILY_API_KEY": ("TAVILY_API_KEY",),
            "BRAVE_API_KEY": ("BRAVE_API_KEY",),
        }
        return mapping.get(reference_id, (reference_id,))

    @staticmethod
    def child_env(
        reference_id: str,
        env_allowlist: tuple[str, ...] = (),
    ) -> dict[str, str]:
        """Build an owned child environment, injecting only allowlisted vars.

        This is the ONLY place secret values enter the child process. The
        returned dict contains only environment variables that are on the
        allowlist. The value is never logged or persisted.
        """
        child: dict[str, str] = {}
        for var in env_allowlist:
            value = os.environ.get(var)
            if value is not None:
                child[var] = value
        return child
