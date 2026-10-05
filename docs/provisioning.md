# Provisioning SkillWeave Core

This document is the reproducible recipe for installing the `skillweave` Python
distribution — either as an **editable dev install** or from a **built wheel** —
and for running the acceptance smoke checks that prove the install came from the
distribution and not from the checkout. It is written to be followed verbatim in
a throwaway environment; nothing here requires a global symlink or writes into a
shared Desktop/agent install.

Scope note: this ticket (SW-ENV-001) repairs *provisioning* — metadata, the
console entrypoints, the base-vs-optional dependency boundary, and the smoke
tests. It does **not** re-implement native CLI behavior. The final composed
installed smoke check consumes qualified SW-1510-CLI semantics and is a **held
composition gate**; see [Held composition gate](#held-composition-gate).

## What is declared

Read straight from the distribution metadata (`pyproject.toml` is the source of
truth; `importlib.metadata` reports the installed copy):

| Field | Value |
|---|---|
| Distribution | `skillweave` |
| Version | `1.5.9` |
| `requires-python` | `>=3.9` |
| Base dependencies | `PyYAML>=6.0`, `skillweave-sdk==0.2.0` (pinned exact) |
| Optional extras | `dev`, `mcp` |
| Console scripts | `skillweave`, `skillweave-install`, `skillweave-risk-mode`, `skillweave-interactive-mode` |

`skillweave-sdk` is pinned **exactly** (`==0.2.0`). It owns the contract bytes
(JSON Schemas + taxonomy); Core consumes the pinned artifact and never copies the
schema authority into its own tree (see `docs/architecture.md`, "Repository
boundary"). Do not float it.

### Base vs optional dependencies

Base dependencies are the ones `import skillweave` genuinely needs:

* **`PyYAML>=6.0`** — imported at module top by `persistence.py`, `design_thinking.py`,
  `templates.py`, `capability.py` and others. `skillweave/__init__.py` imports
  `persistence`, so PyYAML is on the *unconditional* core import path. It cannot
  be moved to an extra without breaking `import skillweave`.
* **`skillweave-sdk==0.2.0`** — the pinned contract authority.

Optional capabilities are **never** base dependencies:

* **MCP hosting (`mcp` extra).** `src/skillweave/mcp/server.py` imports the MCP
  SDK inside a `try/except ImportError` and falls back to an inert `FastMCP`
  stub. The base install therefore imports and runs without it; the stub is a
  deliberate degradation, not a missing hard requirement. Because the fallback
  is silent, run the diagnostic below before relying on MCP.
* **`dev` extra** — `pytest`, `pytest-asyncio`, `black`, `flake8`, `jsonschema`.
  `jsonschema` is a test-only dependency; the base install does not need it
  (verified: importing `skillweave` does not import `jsonschema`).

### Optional-capability diagnostic

When a capability is missing, the failure should be actionable, not silent:

```console
$ check-optional-capability.py mcp        # or: python scripts/check-optional-capability.py mcp
{
  "capabilities": { "mcp": { "status": "missing",
                             "install_hint": "pip install 'skillweave[mcp]'" } },
  "missing": ["mcp"]
}
$ echo $?
1
```

Exit `0` = available, `1` = missing (with the exact `pip install 'skillweave[…]'`
hint), `2` = unknown capability name. The script is stdlib-only and mutates
nothing.

## Environment activation

Use a fresh virtual environment per install. Activation is optional — you can
always call the environment's binaries by path — but activate it so the console
scripts resolve from the environment and not from an ambient one.

```bash
# POSIX
python3 -m venv .venv
. .venv/bin/activate

# Windows (PowerShell)
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
```

Activating must put the environment's `bin/` (POSIX) or `Scripts\` (Windows)
first on `PATH`. Verify:

```bash
python -c "import sys; print(sys.prefix)"
```

## Install / bootstrap commands

### From a built wheel (distribution install)

```bash
python -m pip install --upgrade pip
python -m pip install /path/to/skillweave-1.5.9-py3-none-any.whl
```

This pulls the base dependencies (`PyYAML`, `skillweave-sdk==0.2.0`) from the
configured index. For an offline/hermetic install, point pip at a directory of
wheels instead of the network:

```bash
python -m pip install --no-index --find-links /path/to/wheelhouse \
    skillweave-1.5.9-py3-none-any.whl PyYAML>=6.0 skillweave-sdk==0.2.0
```

### Editable dev install (checkout)

```bash
python -m pip install -e /path/to/skillweave
```

Editable installs need the build backend (`setuptools`, `wheel`) available in
the installing environment; pip's default build isolation provisions it from the
index. If the environment is offline, either pre-seed `setuptools`/`wheel` or
pass `--no-build-isolation` with the backend already importable.

### Optional extras

```bash
python -m pip install "skillweave[mcp]"          # MCP hosting
python -m pip install "skillweave[dev]"          # test/lint toolchain
python -m pip install "skillweave[mcp,dev]"      # both
```

The `mcp` extra is a pure-Python dependency and carries no interpreter pin
beyond the project's own `requires-python >=3.9`; it is deliberately *not* a
base dependency (see above).

### Build a wheel yourself

```bash
python -m build --wheel --outdir dist /path/to/skillweave
# or, without the `build` frontend:
python -m pip wheel --no-deps -w dist /path/to/skillweave
```

## Entrypoint options

All four console scripts are declared in `[project.scripts]` and are created by
pip. Each also has a module equivalent that needs no console-script wrapper.

| Console script | Module equivalent |
|---|---|
| `skillweave` | `python -m skillweave.cli.main` |
| `skillweave-install` | `python -m skillweave.installer` |
| `skillweave-risk-mode` | `python -m skillweave.scripts.resolve_risk_mode` |
| `skillweave-interactive-mode` | `python -m skillweave.scripts.interactive_mode` |

**uv** — install into a uv-managed environment, then run via `uv run`:

```bash
uv venv
uv pip install /path/to/skillweave-1.5.9-py3-none-any.whl
uv run skillweave --help
```

**pipx** — for a self-contained, globally-callable install in an isolated
environment (no global symlink into a shared Desktop install):

```bash
pipx install /path/to/skillweave-1.5.9-py3-none-any.whl
skillweave --help
```

Confirm the entrypoint resolves from the install, not the checkout:

```bash
python -c "import skillweave, sys; print(skillweave.__file__)"
skillweave --help
```

## Smoke checks (fresh install, outside the source tree)

These are the checks the packaging suite runs. Run them from an unrelated
working directory so the checkout's `src/` cannot shadow the install, and with
`PYTHONPATH` unset so the import can only come from the installed package.

```bash
cd "$(mktemp -d)"
env -u PYTHONPATH python -c "import skillweave, importlib.metadata as m; \
    print(skillweave.__file__); print(m.requires('skillweave'))"
env -u PYTHONPATH skillweave --help
env -u PYTHONPATH python scripts/check-optional-capability.py mcp   # base has no MCP
```

The `tests/packaging/test_install_provisioning.py` suite automates exactly this
against a freshly built wheel and a freshly created editable install, asserting:

1. the base distribution's `Requires-Dist` is `PyYAML>=6.0` + `skillweave-sdk==0.2.0`;
2. all four console-script targets import and expose a callable `main`;
3. `skillweave.<module>` resolves under the *installed* location, not `src/`;
4. the base install imports and runs with no MCP SDK present;
5. a missing optional capability yields the actionable hint (exit 1).

## Held composition gate

Criterion 5 of SW-ENV-001 — "consume qualified SW-1510-CLI semantics in the
final composed installed smoke test" — is **held**. The provisioning repairs and
the smoke checks above are complete and independently runnable, but the *final
composed* smoke check depends on the native CLI behavior gate owned by
SW-1510-CLI, which has not yet been independently qualified in this sequence.
This ticket repairs provisioning; it does not duplicate or bypass that gate.

The exact future invocation, once SW-1510-CLI is qualified, is the composed
installed smoke — the base checks above **plus** the native CLI semantics
substituted in at the `skillweave --help` / `skillweave <subcommand>` step:

```bash
# after SW-1510-CLI qualification (not run today):
cd "$(mktemp -d)"
env -u PYTHONPATH skillweave <qualified-cli-invocation>
```

Until then, treat the composed criterion as **HOLD**, not PASS.

## Version markers and Python support

`pyproject.toml` declares `requires-python = ">=3.9"` and classifiers for Python
3.9–3.12; both are preserved by this ticket (no breaking support change). The
distribution version is `1.5.9`; release-facing claims about a future `1.5.10`
are owned by the separate SW-DOC-004 version-truth ticket and are not made here.
Apache license metadata is preserved as-is; PEP 639 SPDX normalization is owned
by the separate LICENSE task and is not performed here.

## Honest limits

* A fully offline/hermetic **base** install additionally requires `PyYAML` and
  the pinned `skillweave-sdk` wheel to be present in the local wheelhouse. Where
  they are absent from the private material and network installs are
  disallowed, the base install is a **setup HOLD** — an explicit blocker, not a
  fabricated pass.
