"""Credential resolver tests (SW-MCP-001, criteria 1-2).

The resolver never reads, stores, or transmits the credential value.
It only checks availability. The value is injected only into an owned
child process environment by the launcher.
"""
from __future__ import annotations

import os
import sys

_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, str(_src))

import pytest  # noqa: E402

from skillweave.council.credential import (  # noqa: E402
    CredentialResolver,
    CredentialResolution,
    resolve_envctl_available,
    ENVCTL_COMMAND,
)


class TestCredentialResolution:
    """CredentialResolution dataclass invariants."""

    def test_available_credential(self):
        r = CredentialResolution(
            reference_id="PERPLEXITY_COOKIES",
            available=True,
            source="env",
            variable_names=("PERPLEXITY_COOKIES",),
        )
        assert r.available is True
        d = r.to_dict()
        assert d["reference_id"] == "PERPLEXITY_COOKIES"
        assert d["available"] is True
        assert d["source"] == "env"
        assert d["variable_names"] == ["PERPLEXITY_COOKIES"]

    def test_missing_credential(self):
        r = CredentialResolution(
            reference_id="PERPLEXITY_COOKIES",
            available=False,
            source="env",
            variable_names=("PERPLEXITY_COOKIES",),
        )
        assert r.available is False

    def test_envctl_source(self):
        r = CredentialResolution(
            reference_id="BRAVE_API_KEY",
            available=True,
            source="envctl",
            variable_names=("BRAVE_API_KEY",),
        )
        assert r.available is True
        assert r.source == "envctl"


class TestCredentialResolverNoSecrets:
    """The resolver never leaks secret values.

    It returns a boolean availability flag, never the credential value.
    """

    def test_resolve_available_from_env_overrides(self):
        resolver = CredentialResolver(
            env_overrides={"PERPLEXITY_COOKIES": "any-value-checked-only-for-presence"},
        )
        r = resolver.resolve("PERPLEXITY_COOKIES")
        assert r.available is True
        assert r.source == "env"
        # The value itself is never stored in the resolution object
        assert not hasattr(r, "value")
        assert r.reference_id == "PERPLEXITY_COOKIES"

    def test_resolve_missing_from_empty_env(self):
        resolver = CredentialResolver(env_overrides={})
        r = resolver.resolve("PERPLEXITY_COOKIES")
        assert r.available is False
        assert r.source == "env"

    def test_resolve_missing_when_var_is_empty_string(self):
        resolver = CredentialResolver(
            env_overrides={"PERPLEXITY_COOKIES": ""},
        )
        r = resolver.resolve("PERPLEXITY_COOKIES")
        assert r.available is False

    def test_resolve_serper_api_key(self):
        resolver = CredentialResolver(
            env_overrides={"SERPER_API_KEY": "present"},
        )
        r = resolver.resolve("SERPER_API_KEY")
        assert r.available is True

    def test_resolve_tavily_api_key(self):
        resolver = CredentialResolver(
            env_overrides={"TAVILY_API_KEY": "sk-test-value"},
        )
        r = resolver.resolve("TAVILY_API_KEY")
        assert r.available is True

    def test_resolve_brave_api_key(self):
        resolver = CredentialResolver(
            env_overrides={"BRAVE_API_KEY": "present"},
        )
        r = resolver.resolve("BRAVE_API_KEY")
        assert r.available is True

    def test_resolve_unknown_reference(self):
        """Unknown reference id still returns a resolution (not available)."""
        resolver = CredentialResolver(env_overrides={})
        r = resolver.resolve("UNKNOWN_REFERENCE")
        assert r.available is False
        assert "UNKNOWN_REFERENCE" in r.variable_names

    def test_resolve_all(self):
        resolver = CredentialResolver(
            env_overrides={
                "PERPLEXITY_COOKIES": "present",
                "SERPER_API_KEY": "present",
            },
        )
        results = resolver.resolve_all(
            ("PERPLEXITY_COOKIES", "SERPER_API_KEY", "TAVILY_API_KEY"),
        )
        assert results["PERPLEXITY_COOKIES"].available is True
        assert results["SERPER_API_KEY"].available is True
        assert results["TAVILY_API_KEY"].available is False

    def test_to_dict_never_contains_value(self):
        resolver = CredentialResolver(
            env_overrides={"PERPLEXITY_COOKIES": "secret-value"},
        )
        r = resolver.resolve("PERPLEXITY_COOKIES")
        d = r.to_dict()
        assert "value" not in d
        assert d["available"] is True

    def test_resolver_does_not_log_values(self, capsys):
        resolver = CredentialResolver(
            env_overrides={"PERPLEXITY_COOKIES": "super-secret-12345"},
        )
        r = resolver.resolve("PERPLEXITY_COOKIES")
        # No output should be produced
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert r.available is True


class TestCredentialResolverChildEnv:
    """child_env() builds an owned child environment from an allowlist.

    This is the ONLY place secret values enter the child process.
    """

    def test_child_env_contains_only_allowlisted_vars(self):
        resolver = CredentialResolver()
        child = resolver.child_env(
            env_allowlist=("PATH", "HOME"),
        )
        # Only allowlisted vars are in the child env
        assert "PATH" in child
        assert "HOME" in child
        # The credential reference itself is NOT injected here
        # (that happens in the launcher, not in the resolver)

    def test_child_env_does_not_contain_the_reference_var_by_default(self):
        resolver = CredentialResolver()
        child = resolver.child_env(
            env_allowlist=(),
        )
        assert "PERPLEXITY_COOKIES" not in child


class TestResolveEnvctlAvailable:
    """envctl is optional; resolver works without it."""

    def test_envctl_not_required(self):
        """resolve_envctl_available returns False when envctl is absent."""
        # In the hermetic test suite envctl is unlikely to be present
        available = resolve_envctl_available()
        assert isinstance(available, bool)

    def test_allow_envctl_false_by_default(self):
        resolver = CredentialResolver()
        assert resolver._allow_envctl is False


class TestVariableNameMapping:
    """Known reference ids map to known environment variable names."""

    def test_perplexity_cookies_mapping(self):
        names = CredentialResolver._variable_names_for("PERPLEXITY_COOKIES")
        assert "PERPLEXITY_COOKIES" in names

    def test_perplexity_api_key_mapping(self):
        names = CredentialResolver._variable_names_for("PERPLEXITY_API_KEY")
        assert "PERPLEXITY_API_KEY" in names

    def test_serper_api_key_mapping(self):
        names = CredentialResolver._variable_names_for("SERPER_API_KEY")
        assert "SERPER_API_KEY" in names

    def test_serpapi_api_key_mapping(self):
        names = CredentialResolver._variable_names_for("SERPAPI_API_KEY")
        assert "SERPAPI_API_KEY" in names

    def test_tavily_api_key_mapping(self):
        names = CredentialResolver._variable_names_for("TAVILY_API_KEY")
        assert "TAVILY_API_KEY" in names

    def test_brave_api_key_mapping(self):
        names = CredentialResolver._variable_names_for("BRAVE_API_KEY")
        assert "BRAVE_API_KEY" in names


class TestCredentialResolverHermetic:
    """Guard tests: resolver with explicit empty env_overrides is hermetic.

    These tests set PERPLEXITY_COOKIES / TAVILY_API_KEY / PERPLEXITY_API_KEY
    in os.environ via monkeypatch and then prove that a resolver constructed
    with an explicit empty ``env_overrides={}`` still sees "missing" --- it
    does NOT fall through to the real process environment.
    """

    def test_empty_env_overrides_ignores_os_environ(self, monkeypatch):
        """Even with env var set in os.environ, empty env_overrides sees missing."""
        monkeypatch.setenv("PERPLEXITY_COOKIES", "real-cookie-value")
        resolver = CredentialResolver(env_overrides={})
        r = resolver.resolve("PERPLEXITY_COOKIES")
        assert r.available is False, (
            "empty env_overrides must isolate from os.environ"
        )

    def test_empty_env_overrides_tavily(self, monkeypatch):
        """Empty env_overrides isolates TAVILY_API_KEY from os.environ."""
        monkeypatch.setenv("TAVILY_API_KEY", "sk-real-key")
        resolver = CredentialResolver(env_overrides={})
        r = resolver.resolve("TAVILY_API_KEY")
        assert r.available is False

    def test_empty_env_overrides_perplexity_api_key(self, monkeypatch):
        """Empty env_overrides isolates PERPLEXITY_API_KEY from os.environ."""
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-real-key")
        resolver = CredentialResolver(env_overrides={})
        r = resolver.resolve("PERPLEXITY_API_KEY")
        assert r.available is False

    def test_resolver_never_returns_secret_value(self, monkeypatch):
        """Resolver never returns or prints the credential value."""
        monkeypatch.setenv("PERPLEXITY_COOKIES", "super-secret-cookie-value")
        resolver = CredentialResolver(env_overrides={"PERPLEXITY_COOKIES": "override-value"})
        r = resolver.resolve("PERPLEXITY_COOKIES")
        assert r.available is True
        # The resolution object never contains the value
        d = r.to_dict()
        for val in d.values():
            val_str = str(val)
            assert "super-secret" not in val_str
            assert "override-value" not in val_str

    def test_child_env_honors_empty_overrides(self, monkeypatch):
        """child_env() with empty env_overrides does not read os.environ."""
        monkeypatch.setenv("PATH", "/usr/bin")
        resolver = CredentialResolver(env_overrides={})
        child = resolver.child_env(env_allowlist=("PATH",))
        # PATH should NOT be in child because env_overrides is empty
        assert "PATH" not in child, (
            "child_env with empty env_overrides must not leak os.environ"
        )

    def test_child_env_uses_override_not_os_environ(self, monkeypatch):
        """child_env() uses env_overrides values, not os.environ."""
        monkeypatch.setenv("PERPLEXITY_COOKIES", "real-value")
        resolver = CredentialResolver(
            env_overrides={"PERPLEXITY_COOKIES": "override-value"}
        )
        child = resolver.child_env(env_allowlist=("PERPLEXITY_COOKIES",))
        assert child.get("PERPLEXITY_COOKIES") == "override-value"
        assert child.get("PERPLEXITY_COOKIES") != "real-value"
