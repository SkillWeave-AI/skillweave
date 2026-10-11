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
installed smoke check **consumes** qualified SW-1510-CLI semantics through the
installed console script; see [Composed installed gate](#composed-installed-gate).
The gate runs wherever the built wheel carries the SW-1510-CLI surface
(`skillweave.cli.support`); on a base that predates that integration it is
skipped with an explicit reason, never fabricated into a pass.

## What is declared

Read straight from the distribution metadata (`pyproject.toml` is the source of
truth; `importlib.metadata` reports the installed copy):

| Field | Value |
|---|---|
| Distribution | `skillweave` |
| Version | `1.5.9` |
| `requires-python` | `>=3.9` |
| Base dependencies | `PyYAML>=6.0`, `skillweave-sdk==0.3.0` (pinned exact) |
| Optional extras | `dev`, `mcp` |
| Console scripts | `skillweave`, `skillweave-install`, `skillweave-risk-mode`, `skillweave-interactive-mode` |

`skillweave-sdk` is pinned **exactly** (`==0.3.0`). It owns the contract bytes
(JSON Schemas + taxonomy); Core consumes the pinned artifact and never copies the
schema authority into its own tree (see `docs/architecture.md`, "Repository
boundary"). Do not float it.

### Base vs optional dependencies

Base dependencies are the ones `import skillweave` genuinely needs:

* **`PyYAML>=6.0`** — imported at module top by `persistence.py`, `design_thinking.py`,
  `templates.py`, `capability.py` and others. `skillweave/__init__.py` imports
  `persistence`, so PyYAML is on the *unconditional* core import path. It cannot
  be moved to an extra without breaking `import skillweave`.
* **`skillweave-sdk==0.3.0`** — the pinned contract authority.

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

When a capability is missing, the failure should be actionable, not silent. The
diagnostic ships **inside the distribution** so the documented invocation works
from a fresh wheel with no checkout on the path:

```console
$ python -m skillweave.scripts.check_optional_capability mcp
{
  "capabilities": { "mcp": { "status": "missing", "available": false,
                             "install_hint": "pip install 'skillweave[mcp]'" } },
  "missing": ["mcp"]
}
$ echo $?
1
```

`scripts/check-optional-capability.py` is a thin source-tree wrapper over that
same packaged module (the two can never drift):

```console
$ python scripts/check-optional-capability.py mcp
```

Exit `0` = available, `1` = missing (with the exact `pip install 'skillweave[…]'`
hint), `2` = unknown capability name. The module is stdlib-only and mutates
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
python -m pip install /path/to/skillweave-1.5.10-py3-none-any.whl
```

This pulls the base dependencies (`PyYAML`, `skillweave-sdk==0.3.0`) from the
configured index. For an offline/hermetic install, point pip at a directory of
wheels instead of the network:

```bash
python -m pip install --no-index --find-links /path/to/wheelhouse \
    skillweave-1.5.10-py3-none-any.whl PyYAML>=6.0 skillweave-sdk==0.3.0
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
uv pip install /path/to/skillweave-1.5.10-py3-none-any.whl
uv run skillweave --help
```

**pipx** — for a self-contained, globally-callable install in an isolated
environment (no global symlink into a shared Desktop install):

```bash
pipx install /path/to/skillweave-1.5.10-py3-none-any.whl
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
export HOME="$(mktemp -d)"                       # isolate, never a shared env
env -u PYTHONPATH python -c "import skillweave, importlib.metadata as m; \
    print(skillweave.__file__); print(m.requires('skillweave'))"
env -u PYTHONPATH skillweave --help
env -u PYTHONPATH python -m skillweave.scripts.check_optional_capability mcp  # base has no MCP
```

The `tests/packaging/test_install_provisioning.py` suite automates exactly this
against a freshly built wheel and a freshly created editable install, asserting:

1. the base distribution's `Requires-Dist` is `PyYAML>=6.0` + `skillweave-sdk==0.3.0`;
2. all four console-script targets import and expose a callable `main`;
3. `skillweave.<module>` resolves under the *installed* location, not `src/`;
4. the base install imports and runs with no MCP SDK present;
5. **every non-optional runtime module under `skillweave` imports** in the base
   install (the whole-runtime-surface sweep — see [Runtime-surface import
   audit](#runtime-surface-import-audit));
6. a missing optional capability yields the actionable hint (exit 1) via
   `python -m skillweave.scripts.check_optional_capability`;
7. the composed installed smoke below reproduces the accepted SW-1510-CLI
   semantics through the installed console script.

### Runtime-surface import audit

A stale base or an unqualified `skillweave_sdk` copy can hide an import break
inside a module that no smoke command happens to touch — the pre-existing
`skillweave_sdk.validator.AuthenticatedServiceContext` drift was exactly such a
case. Criterion 2 is therefore checked by *walking* the installed package:
`pkgutil.walk_packages` enumerates every non-optional module under
`skillweave` (the optional `mcp` extra and the packaged `test_*` modules are
excluded) and imports each. The sweep asserts **zero** import failures, so a
base-install import break fails loudly rather than silently, and it asserts the
flagged `skillweave.runtime.authority_service` module was among those swept.

The sweep needs the pinned SDK **in the fresh venv**, not merely on the running
interpreter (a stale copy reusing the same version string, or a metadata-only
dist-info, can sit on the interpreter's path). `_provide_sdk_source` supplies
it, preferring, in order:

1. the offline pinned wheel named by `SKILLWEAVE_SDK_DIR` (default: the private
   `real-baseline-artifacts/*/wheels`), installed with `pip install --no-index
   --no-deps` — a source file's `AuthenticatedServiceContext` is verified by
   source scan before it is trusted; else
2. the running interpreter's own install, linked with a `skillweave_sdk.pth`
   and its `skillweave_sdk-*.dist-info` copied beside it.

Only when *neither* source exists — no wheel and no qualified interpreter
install — is the sweep a **setup HOLD** (skip with an explicit reason). A
source that is present but stale is rejected for the same reason, never
fabricated into a pass. Neither path installs anything globally or into a
shared environment.

## Composed installed gate

Criterion 5 of SW-ENV-001 — "consume qualified SW-1510-CLI semantics in the
final composed installed smoke test" — is exercised by running the **installed**
`skillweave` console script through the accepted CLI surface. The tests assert
the accepted semantics; they do **not** re-implement or duplicate them. On a
built wheel that carries the SW-1510-CLI surface (`skillweave.cli.support`,
integration head `67c35c8c`), each case runs in its own isolated `HOME` and
project directory with `PYTHONPATH` unset:

```bash
cd "$(mktemp -d)"; export HOME="$(mktemp -d)"
env -u PYTHONPATH skillweave --help                       # async surfaces listed
env -u PYTHONPATH skillweave support --json               # skillweave.support/1 table
env -u PYTHONPATH skillweave start --async --run-id demo -- sleep 30
env -u PYTHONPATH skillweave inspect demo                 # state=running, lane preflight
env -u PYTHONPATH skillweave kill demo <wrong-pid>        # exit 2, refused
env -u PYTHONPATH skillweave kill demo <recorded-pid>     # ok -> killed, lane advance_or_stop
env -u PYTHONPATH skillweave inspect ../x                 # exit 2 (invalid run id)
env -u PYTHONPATH skillweave start --async --run-id x -- /no/such/binary  # exit 127
```

Asserted accepted semantics (observed against the installed console script, not
the checkout):

* `skillweave support` prints the versioned table `skillweave.support/1`, whose
  `surfaces` are exactly `entry`, `run`, `start`, `inspect`, `kill`, `cancel`,
  `resume`, `mcp`, `dispatch`, with the shared `run_id_rule` and exit-code
  table (`invalid_input` = 2, `command_not_found` = 127).
* `start --async` yields a `running` record; `inspect` agrees
  (`state` = `running`, `lane_states` = `{<id>: "preflight"}`).
* `kill <id> <wrong-pid>` is refused with **exit 2**; `kill <id> <recorded-pid>`
  succeeds, and `inspect` then shows `state` = `killed` with lane state
  `advance_or_stop`.
* an invalid run id (e.g. `../x`) is refused with **exit 2** on
  `start`/`inspect`/`kill`, and a command the OS cannot launch is reported with
  **exit 127** — both without a Python traceback.

If the built wheel predates the SW-1510-CLI integration (no
`skillweave.cli.support` module — e.g. the bare checkout base), the composed
gate is **skipped with an explicit "composed gate not present on this base"
reason**. It is never reported as a fabricated pass.

## Known defects (not fixed here)

* **`skillweave-interactive-mode` exits 1.** In a fresh install the declared
  console script `skillweave-interactive-mode`
  (`skillweave.scripts.interactive_mode:main`) fails with
  `'RiskModeResolver' object has no attribute 'get_effective_risk_mode'`. This
  is **pre-existing and outside this ticket's scope** (SW-ENV-001 repairs
  provisioning, not the interactive selector's internal call). It is recorded
  here so the defect is not mistaken for a provisioning regression, it is **not
  fixed** by this ticket, and it is **not asserted as passing** in the smoke
  suite — only its *importability* as a declared entry target is (criterion 2).

## Version markers and Python support

`pyproject.toml` declares `requires-python = ">=3.9"` and classifiers for Python
3.9–3.12; both are preserved by this ticket (no breaking support change). The
distribution version is `1.5.9`; release-facing claims about a future `1.5.10`
are owned by the separate SW-DOC-004 version-truth ticket and are not made here.
Apache license metadata is preserved as-is; PEP 639 SPDX normalization is owned
by the separate LICENSE task and is not performed here.

### Tested Python versions

The classifiers *declare* 3.9–3.12; that is a claim, not a measurement. What was
actually **exercised** for this ticket, and what is only declared, is stated
explicitly so the gap is visible rather than implied:

| Version | Status | How exercised |
|---|---|---|
| 3.9  | declared only | no interpreter available in this environment |
| 3.10 | declared only | no interpreter available in this environment |
| 3.11 | **verified host**   | built-wheel install + runtime-surface sweep in this lane |
| 3.12 | verified by CI    | CI pins 3.12 for the suite |
| 3.13 | declared only | no interpreter available in this environment |
| 3.14 | **verified host**   | built-wheel install + full composed CLI smoke on the controller host |

"Verified host" means the provisioning suite (build, install, entrypoint and —
where the wheel carries the SW-1510-CLI surface — the composed CLI smoke) was
actually run on that interpreter. The declared-only rows are covered by the
preserved `requires-python`/classifier metadata, not by a run.

## Honest limits

* A fully offline/hermetic **base** install additionally requires `PyYAML` and
  the pinned `skillweave-sdk` wheel to be present in the local wheelhouse. Where
  they are absent from the private material and network installs are
  disallowed, the base install is a **setup HOLD** — an explicit blocker, not a
  fabricated pass.
* The runtime-surface sweep needs a *pinned* `skillweave-sdk` exposing
  `validator.AuthenticatedServiceContext`, present in the fresh venv. It is
  supplied from the offline pinned wheel (`SKILLWEAVE_SDK_DIR`) or the running
  interpreter's qualified install; a stale SDK copy (including one reusing the
  same `0.3.0` version string) is detected by source scan and rejected, making
  the sweep a **setup HOLD** (explicit skip), not a pass. For a bare checkout
  the sweep still needs `PyYAML` seedable in the running interpreter.
* The composed SW-1510-CLI gate needs a wheel that carries
  `skillweave.cli.support`. On the bare checkout base it is **skipped with a
  reason**; it is only a real pass on the SW-1510-CLI-composed head.
