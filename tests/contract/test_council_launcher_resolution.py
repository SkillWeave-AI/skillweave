"""Council's Perplexity MCP launcher resolution (SW-COUNCIL-003, criterion 2).

The addendum launched the Perplexity MCP server from a *floating* git install.
This suite pins the replacement: a resolver that names exactly which binary is
executed, in a fixed, auditable precedence --

    1. explicit launcher config (``SearchConfig.mcp_command``)  -- wins;
    2. registered compatible runtime (a caller-owned registration; NO default
       is shipped -- an installed script is not trusted until verified);
    3. verified installed binary (console script + matching dist metadata);
    4. uvx fallback -- only with an EXACT pin, an EXPLICIT network grant and a
       recorded provenance.

Every step is asserted, plus the refusals: a floating ``git+``/URL command, an
unpinned uvx spec, a uvx fallback without an explicit network grant, and a
registration whose installed version does not match its pin. Nothing here
launches uvx or dials the network: the resolver is pure (it reads installed
metadata only).
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

FIX_C = Path(__file__).resolve().parents[1] / "fixtures" / "council"
PINNED = FIX_C / "pinned"
PINNED_BIN = PINNED / "bin"

from skillweave.council import launcher as L  # noqa: E402
from skillweave.council.search import SearchConfig, WebSearch  # noqa: E402


def _search_path() -> str:
    return os.pathsep.join(
        [str(PINNED_BIN), "/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"]
    )


def _pinned_registration(version: str = "9.9.9") -> L.Registration:
    return L.Registration(
        id="sw-council-pinned-mcp",
        launch=("sw-council-pinned-mcp",),
        distribution="sw-council-pinned-mcp",
        version=version,
        transport="mcp-stdio",
        source="fixture:pinned",
    )


def _pinned_importable(monkeypatch):
    monkeypatch.syspath_prepend(str(PINNED))
    importlib.invalidate_caches()


@pytest.fixture
def no_ambient_registrations(monkeypatch):
    """Isolate the module registry, so only a test-owned registration is considered.

    The module ships no default registration (``default_registrations()`` is
    empty), but a prior test in the same process may have left one registered;
    a test about the *uvx* rung (or a *refusal*) must not be served by any
    ambient runtime, so the registry is emptied for the duration of the test.
    """
    monkeypatch.setattr(L, "_REGISTRY", {})
    return monkeypatch


# ── criterion 1: the default Capacium launcher is dropped ──────────────────


class TestDefaultLauncherIsDropped:
    def test_no_capacium_launcher_remains_in_the_source(self):
        """The default ``capacium-mcp`` launcher is gone from ``src`` entirely."""
        import subprocess

        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "grep", "-n", r"capacium-mcp\|capacium_mcp", "--", "src"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        # git grep exits 1 (no match) when the token is absent -- the required state.
        assert result.returncode == 1, result.stdout
        assert result.stdout == "", result.stdout

    def test_default_registrations_is_empty(self):
        assert L.default_registrations() == []

    def test_import_registers_nothing_ambiently(self):
        """Importing the module must not auto-register a launcher.

        Proven in a *fresh* interpreter so the observation is of import-time
        state, not of anything a prior test may have registered in-process.
        """
        import subprocess

        repo_root = Path(__file__).resolve().parents[2]
        probe = (
            "import sys; sys.path.insert(0, 'src');"
            "from skillweave.council import launcher as L;"
            "print(len(L._REGISTRY), L.default_registrations())"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "0 []", result.stdout

    def test_no_input_fails_closed_with_the_launcher_asset(self):
        """``resolve_launcher()`` with no input refuses (``asset="launcher"``)."""
        L.clear_registrations()
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher()
        assert excinfo.value.asset == "launcher"

    def test_council_search_yields_typed_no_binding_and_spawns_nothing(
        self, tmp_path, monkeypatch
    ):
        """The Council search seam: an unregistered-default is a typed
        ``no_binding`` and *no* process is spawned (proven by the argv log)."""
        log = tmp_path / "argv.log"
        monkeypatch.setenv("COUNCIL_REFUSAL_ARGV_LOG", str(log))
        L.clear_registrations()
        ws = WebSearch()
        cfg = SearchConfig(provider="perplexity", full_content_results=0, time_range="any")
        arg = ws._backend_argument("q", cfg, "perplexity-search-mcp")
        assert not arg.get("command")
        assert arg["launcher_resolution_error"] == "launcher"
        assert arg["launcher_provenance"]["kind"] == "unresolved"
        # Nothing was launched: the argv log (written by any spawned fake launcher)
        # is absent/empty.
        spawned = log.read_text() if log.exists() else ""
        assert spawned == "", f"a launcher was spawned: {spawned!r}"


# ── precedence: explicit config wins ───────────────────────────────────────


class TestExplicitConfigWins:
    def test_explicit_command_beats_a_registered_runtime(self, monkeypatch):
        _pinned_importable(monkeypatch)
        res = L.resolve_launcher(
            explicit_command=["/bin/echo", "explicit-wins"],
            registrations=[_pinned_registration()],
            search_path=_search_path(),
        )
        assert res.kind == "explicit"
        assert res.command == ("/bin/echo", "explicit-wins")
        assert res.provenance["source"] == "explicit_config"

    def test_explicit_provenance_is_recorded(self):
        res = L.resolve_launcher(
            explicit_command=["/bin/echo"],
            explicit_provenance={"source": "ops:runbook", "asset": "mirror-01"},
        )
        assert res.provenance["source"] == "ops:runbook"
        assert res.provenance["asset"] == "mirror-01"

    def test_search_config_threads_the_registration_and_uvx_controls(self):
        cfg = SearchConfig(provider="perplexity")
        assert cfg.launcher_registration_id is None
        assert cfg.uvx_fallback is None
        assert cfg.verify_launcher_installed is True
        assert cfg.launcher_provenance is None


# ── registered compatible runtime -> verified installed binary ─────────────


class TestRegisteredAndInstalled:
    def test_registered_runtime_resolves_the_verified_installed_binary(self, monkeypatch):
        _pinned_importable(monkeypatch)
        res = L.resolve_launcher(
            registrations=[_pinned_registration()], search_path=_search_path()
        )
        assert res.kind == "registered"
        assert res.command[0] == os.path.realpath(str(PINNED_BIN / "sw-council-pinned-mcp"))
        assert res.provenance["registration_id"] == "sw-council-pinned-mcp"
        assert res.provenance["observed_version"] == "9.9.9"
        assert res.provenance["location"] == os.path.realpath(
            str(PINNED_BIN / "sw-council-pinned-mcp")
        )

    def test_installed_binary_rung_when_no_registration_candidate_matches(self, monkeypatch):
        """Registered rung skips the pinned runtime; the installed-bin rung finds it.

        With only a non-matching registration candidate, the registered rung is
        exhausted and resolution reaches the verified-installed-binary rung,
        which resolves the console script straight on PATH.
        """
        _pinned_importable(monkeypatch)
        # Swap the module registry so only the (installed) pinned runtime is known,
        # and pass a candidate list that holds a *different*, uninstalled runtime.
        monkeypatch.setattr(
            L, "_REGISTRY", {"sw-council-pinned-mcp": _pinned_registration()}
        )
        other = L.Registration(
            id="some-other-mcp",
            launch=("some-other-mcp",),
            distribution=None,
            version=None,
            source="fixture:other",
        )
        res = L.resolve_launcher(registrations=[other], search_path=_search_path())
        assert res.kind == "installed-bin"
        assert os.path.realpath(res.command[0]) == os.path.realpath(
            str(PINNED_BIN / "sw-council-pinned-mcp")
        )
        assert res.provenance["observed_version"] == "9.9.9"

    def test_version_mismatch_refuses_the_registration(
        self, monkeypatch, no_ambient_registrations
    ):
        _pinned_importable(monkeypatch)
        # The pinned distribution is 9.9.9, the registration claims 0.0.1: the
        # registration is not verified, and with no other launcher it fails closed.
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(
                registrations=[_pinned_registration(version="0.0.1")],
                search_path=_search_path(),
                verify_installed=True,
            )
        assert excinfo.value.asset == "launcher"


# ── the uvx fallback: exact pin + explicit network + provenance ────────────


class TestUvxFallback:
    def test_pinned_uvx_with_network_and_provenance_resolves(
        self, no_ambient_registrations
    ):
        fallback = L.UvxFallback(
            package="sw-council-pinned-mcp==9.9.9",
            allow_network=True,
            provenance="ops:release-manifest 2026-10-04",
        )
        res = L.resolve_launcher(
            uvx_fallback=fallback, search_path=_search_path()
        )
        assert res.kind == "uvx"
        assert res.command[0].endswith("uvx")
        assert "--from" in res.command
        assert "sw-council-pinned-mcp==9.9.9" in res.command
        assert res.provenance["allow_network"] is True
        assert res.provenance["package"] == "sw-council-pinned-mcp==9.9.9"
        assert res.provenance["provenance"] == "ops:release-manifest 2026-10-04"

    def test_uvx_fallback_without_network_grant_is_refused(
        self, no_ambient_registrations
    ):
        fallback = L.UvxFallback(
            package="sw-council-pinned-mcp==9.9.9",
            allow_network=False,
            provenance="ops:release-manifest",
        )
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(uvx_fallback=fallback, search_path=_search_path())
        assert excinfo.value.asset == "uvx.network"

    def test_an_unpinned_uvx_spec_is_refused_at_construction(self):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.UvxFallback(
                package="sw-council-pinned-mcp",
                allow_network=True,
                provenance="ops:release-manifest",
            )
        assert excinfo.value.asset == "uvx.package"

    def test_a_uvx_spec_without_an_exact_pin_is_refused(self):
        with pytest.raises(L.LauncherResolutionError):
            L.UvxFallback(
                package="sw-council-pinned-mcp>=1.0",
                allow_network=True,
                provenance="ops:release-manifest",
            )

    def test_a_uvx_fallback_without_provenance_is_refused(self):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.UvxFallback(
                package="sw-council-pinned-mcp==9.9.9",
                allow_network=True,
                provenance="   ",
            )
        assert excinfo.value.asset == "uvx.provenance"


# ── floating installs are never executed ───────────────────────────────────


class TestFloatingRefusal:
    @pytest.mark.parametrize(
        "command",
        [
            ["npx", "-y", "git+https://github.com/example/mcp-server"],
            ["mcp-server", "https://example.org/install"],
            ["uvx", "http://example.org/pkg"],
        ],
    )
    def test_explicit_floating_command_is_refused(self, command):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(explicit_command=command)
        assert excinfo.value.asset == "explicit_command"

    @pytest.mark.parametrize(
        "command",
        [
            ["git+https://github.com/example/mcp-server"],
            ["https://example.org/x"],
        ],
    )
    def test_is_floating_command_detects_markers(self, command):
        assert L.is_floating_command(command) is True

    def test_a_local_path_containing_http_is_not_floating(self, tmp_path):
        pinned = tmp_path / "http-mirror" / "pkg.whl"
        pinned.parent.mkdir(parents=True)
        pinned.write_text("")
        assert L.is_floating_command([str(pinned)]) is False

    def test_no_launcher_at_all_fails_closed(
        self, tmp_path, no_ambient_registrations
    ):
        with pytest.raises(L.LauncherResolutionError) as excinfo:
            L.resolve_launcher(search_path=str(tmp_path))
        assert excinfo.value.asset == "launcher"


# ── the SearchConfig -> backend argument seam ──────────────────────────────


class TestBackendArgument:
    def test_unresolved_launcher_becomes_a_typed_no_binding_not_a_launch(
        self, tmp_path, monkeypatch
    ):
        import skillweave.council.search as search_mod

        def _no_launcher(**kwargs):
            kwargs.pop("search_path", None)
            return L.resolve_launcher(search_path=str(tmp_path), **kwargs)

        monkeypatch.setattr(search_mod, "resolve_launcher", _no_launcher)
        ws = WebSearch()
        cfg = SearchConfig(provider="perplexity")
        arg = ws._backend_argument("q", cfg, "perplexity-search-mcp")
        assert arg["launcher_resolution_error"]
        assert arg["launcher_provenance"]["kind"] == "unresolved"
        # No owned launcher was resolved, so no command is executed -- the
        # backend will report a typed ``no_binding`` rather than launching.
        assert not arg.get("command")
