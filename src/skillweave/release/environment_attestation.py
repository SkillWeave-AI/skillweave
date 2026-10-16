"""Reproducible release-gate test environments (SW-1511A-ENV-001).

Completes SW-SCOPE-009: every claimed test result must name and verify its
interpreter, SDK and dependency set. The producer is the test environment
builder and session guard; the consumer is the gate runner, reviewer and
acceptance ledger. The boundary contract is
``execution-environment-attestation/v1``: exact pins are immutable, so a pin
change requires a new attestation digest.

Three concerns, one per lane criterion:

1. :func:`build_environment` -- **one declared input**
   (``profiles/test-environments.v1.toml``) builds a *complete non-editable*
   environment for each declared interpreter (3.11, 3.12, 3.14) with the
   pinned SDK and every declared collection dependency (including
   ``setuptools``, ``wheel``, ``build``, ``jsonschema`` and ``mcp``). The
   declaration is validated fail-closed: a dropped interpreter, a range or
   ``latest`` SDK pin, or a collection set missing a required dependency is
   *refused*, never silently narrowed.

2. :func:`guard_session` -- the full unit suite must collect and run in each
   built environment **without selective ignore caused by missing
   dependencies**. A dependency the declaration requires but the environment
   cannot provide is an explicit *selective-ignore cause*: the guard refuses
   with that cause named, instead of letting the missing dependency degrade
   into a skip.

3. :meth:`EnvironmentAttestation.to_dict` /
   :func:`verify_attestation` -- every test receipt records interpreter
   path/version, SDK version/origin/install type, dependency lock digest and
   ``SKILLWEAVE_SDK_DIR``. A receipt missing any of those fields is refused by
   :func:`verify_attestation`.

Nothing here reads a wall clock, opens a socket, names a provider/model or
touches a credential. ``pip`` is invoked only with ``--no-index``, so the
builder never resolves from an index; a dependency with no declared wheelhouse
and no locally available distribution is reported as refused, never
fabricated. ``SKILLWEAVE_SDK_DIR`` is the one explicit SDK input and an
explicitly invalid value fails closed rather than falling back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

# ── Contract identity ────────────────────────────────────────────────────────

ENV_ATTESTATION_SCHEMA_ID = "execution-environment-attestation/v1"
ENV_ATTESTATION_SCHEMA_VERSION = "1.0.0"

DECLARATION_SCHEMA_ID = "test-environments/v1"
DECLARATION_RELATIVE_PATH = ("profiles", "test-environments.v1.toml")

#: The interpreters the lane declares. A declaration that drops one is refused.
SUPPORTED_PYTHONS: Tuple[str, ...] = ("3.11", "3.12", "3.14")

#: Dependencies the *collection* proof requires. Criterion 1 names these
#: explicitly; ``build``/``wheel``/``setuptools`` provide the build backend,
#: ``jsonschema`` the schema assertions and ``mcp`` the optional-capability
#: surface whose absence would otherwise degrade into a silent skip.
REQUIRED_COLLECTION_DEPENDENCIES: Tuple[str, ...] = (
    "build",
    "jsonschema",
    "mcp",
    "setuptools",
    "wheel",
)

#: Dependencies needed to *run* the suite rather than merely collect it.
REQUIRED_RUNNER_DEPENDENCIES: Tuple[str, ...] = ("pytest", "pytest-asyncio", "PyYAML")

#: The receipt fields criterion 3 requires. A receipt lacking any of these is
#: refused by :func:`verify_attestation`; they are recorded from observed facts
#: (``sys``/importlib/dist metadata), never from the declaration.
REQUIRED_RECEIPT_FIELDS: Tuple[str, ...] = (
    "interpreter_path",
    "interpreter_version",
    "python_short",
    "sdk_version",
    "sdk_origin",
    "sdk_install_type",
    "dependency_lock_digest",
    "sdk_dir",
)

_SDK_PIN_RE = re.compile(r"^skillweave-sdk==(\d+\.\d+\.\d+)$")
_PYPROJECT_PIN_RE = re.compile(r'"skillweave-sdk==([^"]+)"')
_PY_MINOR_RE = re.compile(r"^\d+\.\d+$")

_SDK_DIR_ENV = "SKILLWEAVE_SDK_DIR"
_SDK_MODULE = "skillweave_sdk"
_SDK_ANCHOR = "schema_version.toml"


# ── Exceptions (fail closed) ─────────────────────────────────────────────────


class EnvironmentDeclarationError(Exception):
    """The declared input is absent, malformed or narrower than the contract."""


class EnvironmentBuildError(Exception):
    """A declared environment could not be built (a real, named failure)."""


class SessionGuardError(Exception):
    """The session does not match the declared, receipted environment."""


class AttestationError(Exception):
    """An attestation receipt is incomplete or not an ``.../v1`` receipt."""


# ── The one declared input ───────────────────────────────────────────────────


def _require_tomllib():
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError as exc:  # pragma: no cover - floor is 3.11
        raise EnvironmentDeclarationError(
            "the declared test-environment input is TOML and needs the stdlib "
            "tomllib parser (Python 3.11+); this interpreter has none"
        ) from exc
    return tomllib


@dataclass(frozen=True)
class EnvironmentDeclaration:
    """The validated, canonical view of the single declared input.

    ``dependency_lock_digest`` is the canonical digest of every pin in the
    declaration, so any pin change produces a different digest and therefore a
    different attestation.
    """

    path: Path
    repo_root: Path
    pythons: Tuple[str, ...]
    non_editable: bool
    collection_dependencies: Tuple[str, ...]
    runner_dependencies: Tuple[str, ...]
    sdk_pin: str
    unit_suite: str
    declared_sdk_pin: str

    @property
    def sdk_version(self) -> str:
        match = _SDK_PIN_RE.match(self.sdk_pin)
        assert match is not None  # validated by load_declaration
        return match.group(1)

    @property
    def dependency_lock_digest(self) -> str:
        payload = {
            "schema": DECLARATION_SCHEMA_ID,
            "python": list(self.pythons),
            "non_editable": self.non_editable,
            "collection_dependencies": list(self.collection_dependencies),
            "runner_dependencies": list(self.runner_dependencies),
            "sdk": self.sdk_pin,
            "unit_suite": self.unit_suite,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DECLARATION_SCHEMA_ID,
            "path": str(self.path),
            "repo_root": str(self.repo_root),
            "python": list(self.pythons),
            "non_editable": self.non_editable,
            "collection_dependencies": list(self.collection_dependencies),
            "runner_dependencies": list(self.runner_dependencies),
            "sdk": self.sdk_pin,
            "unit_suite": self.unit_suite,
            "dependency_lock_digest": self.dependency_lock_digest,
        }


def default_declaration_path(repo_root: Optional[Path] = None) -> Path:
    """The declared input's canonical location (one input, one path)."""
    root = Path(repo_root) if repo_root is not None else _repo_root()
    return root.joinpath(*DECLARATION_RELATIVE_PATH)


def _repo_root() -> Path:
    """The checkout root, derived from this module's own location."""
    return Path(__file__).resolve().parents[3]


def _pyproject_pin(repo_root: Path) -> str:
    text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    match = _PYPROJECT_PIN_RE.search(text)
    if match is None:
        raise EnvironmentDeclarationError(
            f"{repo_root / 'pyproject.toml'} does not pin skillweave-sdk==<version>"
        )
    return match.group(1)


def load_declaration(
    path: Optional[Path] = None,
    *,
    require_supported_pythons: bool = True,
) -> EnvironmentDeclaration:
    """Load and validate the single declared input, fail-closed.

    Refused: a missing input, a wrong schema id, a ``latest``/range SDK pin, a
    pin that disagrees with ``pyproject.toml``, ``non_editable`` false, a
    collection set missing a criterion-1 dependency, and (when
    ``require_supported_pythons``) a declaration that dropped one of the three
    supported interpreters.
    """
    toml_path = Path(path) if path is not None else default_declaration_path()
    if not toml_path.is_file():
        raise EnvironmentDeclarationError(
            f"the declared test-environment input is missing: {toml_path}"
        )
    tomllib = _require_tomllib()
    with toml_path.open("rb") as handle:
        raw = tomllib.load(handle)

    schema = raw.get("schema")
    if schema != DECLARATION_SCHEMA_ID:
        raise EnvironmentDeclarationError(
            f"{toml_path} declares schema {schema!r}, expected {DECLARATION_SCHEMA_ID!r}"
        )

    pythons = tuple(str(v) for v in raw.get("python", ()))
    if not pythons:
        raise EnvironmentDeclarationError(f"{toml_path} declares no interpreters")
    for minor in pythons:
        if not _PY_MINOR_RE.match(minor):
            raise EnvironmentDeclarationError(
                f"{toml_path} declares malformed interpreter {minor!r}"
            )
    if require_supported_pythons:
        missing = [p for p in SUPPORTED_PYTHONS if p not in pythons]
        if missing:
            raise EnvironmentDeclarationError(
                f"{toml_path} dropped supported interpreter(s) {missing}; the "
                f"declared input must build {list(SUPPORTED_PYTHONS)}"
            )

    non_editable = bool(raw.get("non_editable", False))
    if not non_editable:
        raise EnvironmentDeclarationError(
            f"{toml_path} does not declare non_editable=true; a source-linked "
            f"environment is not evidence"
        )

    collection = tuple(str(v) for v in raw.get("collection_dependencies", ()))
    absent = [d for d in REQUIRED_COLLECTION_DEPENDENCIES if d not in collection]
    if absent:
        raise EnvironmentDeclarationError(
            f"{toml_path} omits required collection dependencies {absent}"
        )
    runner = tuple(str(v) for v in raw.get("runner_dependencies", ()))
    absent_runner = [d for d in REQUIRED_RUNNER_DEPENDENCIES if d not in runner]
    if absent_runner:
        raise EnvironmentDeclarationError(
            f"{toml_path} omits required runner dependencies {absent_runner}"
        )

    declared_pin = str(raw.get("sdk", ""))
    if not _SDK_PIN_RE.match(declared_pin):
        raise EnvironmentDeclarationError(
            f"{toml_path} declares sdk {declared_pin!r}; an exact "
            f"skillweave-sdk==<x.y.z> pin is required (never 'latest' or a range)"
        )

    repo_root = toml_path.resolve().parent.parent
    pyproject_pin = _pyproject_pin(repo_root)
    if pyproject_pin != _SDK_PIN_RE.match(declared_pin).group(1):  # type: ignore[union-attr]
        raise EnvironmentDeclarationError(
            f"the declared SDK pin {declared_pin!r} disagrees with pyproject's "
            f"skillweave-sdk=={pyproject_pin}"
        )

    unit_suite = str(raw.get("unit_suite", ""))
    if not unit_suite:
        raise EnvironmentDeclarationError(f"{toml_path} declares no unit_suite")

    return EnvironmentDeclaration(
        path=toml_path,
        repo_root=repo_root,
        pythons=pythons,
        non_editable=non_editable,
        collection_dependencies=collection,
        runner_dependencies=runner,
        sdk_pin=declared_pin,
        unit_suite=unit_suite,
        declared_sdk_pin=pyproject_pin,
    )


# ── SDK resolution and attestation ───────────────────────────────────────────


def resolve_sdk_dir(
    sdk_dir: Optional[Path | str] = None,
    *,
    repo_root: Optional[Path] = None,
) -> Path:
    """Resolve the SDK root from ``SKILLWEAVE_SDK_DIR`` or the sibling checkout.

    An explicitly provided (or environment-provided) invalid path fails closed:
    it never silently falls back to a different checkout.
    """
    explicit = sdk_dir if sdk_dir is not None else os.environ.get(_SDK_DIR_ENV)
    if explicit:
        candidate = Path(explicit)
        if not (candidate / _SDK_ANCHOR).is_file():
            raise EnvironmentBuildError(
                f"{_SDK_DIR_ENV}={explicit} is not an SDK root (missing "
                f"{_SDK_ANCHOR}); refusing to fall back to another checkout"
            )
        return candidate.resolve()
    root = Path(repo_root) if repo_root is not None else _repo_root()
    sibling = root.parent / "skillweave-sdk"
    if (sibling / _SDK_ANCHOR).is_file():
        return sibling.resolve()
    raise EnvironmentBuildError(
        f"no SDK source: set {_SDK_DIR_ENV} to an SDK root (anchor "
        f"{_SDK_ANCHOR}) or expose a sibling ../skillweave-sdk"
    )


@dataclass(frozen=True)
class SdkAttestation:
    """The SDK facts read from the *built environment*, never from a claim."""

    version: str
    origin: str
    install_type: str
    package_path: str
    dist_info_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "origin": self.origin,
            "install_type": self.install_type,
            "package_path": self.package_path,
            "dist_info_path": self.dist_info_path,
        }


_SDK_PROBE = r'''
import json, importlib.util, pathlib
spec = importlib.util.find_spec("skillweave_sdk")
if spec is None or not spec.submodule_search_locations:
    print(json.dumps({"found": False}))
else:
    pkg = pathlib.Path(next(iter(spec.submodule_search_locations))).resolve()
    import skillweave_sdk
    parent = pkg.parent
    info = sorted(p.name for p in parent.glob("skillweave_sdk-*.dist-info"))
    links = sorted(
        p.name for p in parent.glob("*.pth")
    ) + sorted(p.name for p in parent.glob("*.egg-link"))
    editable = any((parent / n).is_file() and "skillweave_sdk" in (parent / n).read_text(errors="ignore")
                   for n in links)
    print(json.dumps({
        "found": True,
        "version": getattr(skillweave_sdk, "SCHEMA_VERSION", ""),
        "package_path": str(pkg),
        "dist_info_path": str(parent / info[0]) if info else "",
        "install_type": "editable" if editable else ("wheel" if info else "directory"),
        "pth_links": links,
    }))
'''


def _run(cmd: Sequence[str | Path], *, env: Mapping[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in cmd], cwd=str(cwd), env=dict(env),
        capture_output=True, text=True,
    )


def _venv_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(venv_dir: Path) -> Path:
    return _venv_bin(venv_dir) / ("python.exe" if os.name == "nt" else "python")


def _venv_site(venv_dir: Path) -> Path:
    return next(venv_dir.glob("lib/python*/site-packages"))


def _sdk_attestation(venv_dir: Path) -> SdkAttestation:
    proc = _run([_venv_python(venv_dir), "-c", _SDK_PROBE], env=_clean_env(), cwd=venv_dir)
    if proc.returncode != 0:
        raise EnvironmentBuildError(
            f"the built environment cannot import {_SDK_MODULE}: {proc.stderr.strip()[:300]}"
        )
    facts = json.loads(proc.stdout.strip().splitlines()[-1])
    if not facts.get("found"):
        raise EnvironmentBuildError(
            f"the built environment has no {_SDK_MODULE} package; the pinned SDK "
            f"dependency was not installed"
        )
    return SdkAttestation(
        version=facts["version"],
        origin=f"pyproject pin {facts['version']} installed into the built venv",
        install_type=facts["install_type"],
        package_path=facts["package_path"],
        dist_info_path=facts["dist_info_path"],
    )


def _clean_env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """An environment with ``PYTHONPATH`` removed and no index access."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PIP_NO_INDEX"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    if extra:
        env.update(extra)
    return env


def _build_sdk_wheel(sdk_dir: Path, outdir: Path) -> Path:
    """Build the pinned SDK wheel from its source tree, offline."""
    outdir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--no-build-isolation", "--no-index", "-w", str(outdir), str(sdk_dir)],
        env=_clean_env(), cwd=outdir,
    )
    wheels = sorted(outdir.glob("skillweave_sdk-*.whl"))
    if proc.returncode != 0 or not wheels:
        raise EnvironmentBuildError(
            f"could not build the pinned SDK wheel from {sdk_dir}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return wheels[0]


# ── dependency provisioning (declared, never fabricated) ─────────────────────


def _dist_info_name(name: str) -> str:
    return name.lower().replace("-", "_")


def _local_wheel(name: str, wheelhouse: Optional[Path]) -> Optional[Path]:
    if wheelhouse is None or not Path(wheelhouse).is_dir():
        return None
    normalized = _dist_info_name(name)
    for pattern in (f"{normalized}-*.whl", f"{name}-*.whl"):
        found = sorted(Path(wheelhouse).glob(pattern))
        if found:
            return found[0]
    return None


def _seed_source(name: str) -> Optional[Path]:
    """The running interpreter's own distribution for ``name``, if installed.

    An offline fallback: the payload carries genuine, non-editable
    distribution metadata (a real ``.dist-info``), and is only used when no
    declared wheelhouse provides the wheel. A missing distribution yields
    ``None`` and therefore a *refusal*, never a fabricated pass.
    """
    purelib = Path(sysconfig.get_paths()["purelib"])
    normalized = _dist_info_name(name)
    for candidate in purelib.glob(f"{normalized}-*.dist-info"):
        return candidate
    for candidate in purelib.glob(f"{name}-*.dist-info"):
        return candidate
    return None


def _distribution_tops(dist_info: Path) -> list[str]:
    """Top-level payload entries the distribution's ``RECORD`` names."""
    tops: list[str] = []
    record = dist_info / "RECORD"
    if not record.is_file():
        return tops
    for line in record.read_text(encoding="utf-8", errors="ignore").splitlines():
        rel = line.split(",", 1)[0]
        if not rel or ".." in rel:
            continue
        top = rel.split("/", 1)[0]
        if top and not top.endswith(".dist-info") and top not in tops:
            tops.append(top)
    return tops


def wheel_from_payload(payload_root: Path, dist_info: Path, outdir: Path) -> Optional[Path]:
    """Zip an unpacked distribution payload into a real wheel, offline.

    The wheelhouse may be absent and the index unreachable; a locally present
    payload is still complete and metadata-bearing. Producing a wheel (rather
    than linking or copying the payload) means the dependency is then
    *installed by pip* with real ``.dist-info`` metadata -- a non-editable,
    verifiable install, never a source link.
    """
    tops = _distribution_tops(dist_info)
    if not tops:
        return None

    tag = "py3-none-any"
    wheel_meta = dist_info / "WHEEL"
    if wheel_meta.is_file():
        for line in wheel_meta.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("Tag:"):
                tag = line.split(":", 1)[1].strip()
                break
    outdir.mkdir(parents=True, exist_ok=True)
    wheel_path = outdir / f"{dist_info.name[: -len('.dist-info')]}-{tag}.whl"

    import zipfile

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for top in tops:
            src = payload_root / top
            if src.is_dir():
                for child in sorted(src.rglob("*")):
                    if child.is_file():
                        archive.write(child, str(child.relative_to(payload_root)))
            elif src.is_file():
                archive.write(src, top)
        for child in sorted(dist_info.rglob("*")):
            if child.is_file():
                archive.write(child, str(child.relative_to(payload_root)))
    return wheel_path


def _repackage_distribution(name: str, outdir: Path) -> Optional[Path]:
    """Repackage the running interpreter's own distribution for ``name``."""
    dist_info = _seed_source(name)
    if dist_info is None:
        return None
    return wheel_from_payload(dist_info.parent, dist_info, outdir)


def _host_purelibs() -> list[Path]:
    """Other interpreters' ``site-packages`` on this host.

    A declared interpreter may be minimal (for example a bare ``python3.12``
    without ``jsonschema``) while a sibling interpreter on the same host
    carries the distribution. Those roots are searched only *after* the running
    interpreter's own, so the declared interpreter is never overridden by an
    unrelated one.
    """
    import glob

    roots: list[Path] = []
    patterns = [
        "/opt/homebrew/opt/python@*/Frameworks/Python.framework/Versions/*/lib/python*/site-packages",
        "/Library/Frameworks/Python.framework/Versions/*/lib/python*/site-packages",
        "/opt/homebrew/lib/python*/site-packages",
        "/usr/local/lib/python*/site-packages",
        "/usr/lib/python3/dist-packages",
    ]
    for pattern in patterns:
        for found in glob.glob(pattern):
            root = Path(found)
            if root.is_dir():
                roots.append(root)
    return roots


def dependency_wheel(name: str, outdir: Path) -> Optional[Path]:
    """An offline wheel for a declared dependency, from the local host only.

    Order: the running interpreter's own installed distribution (repackaged),
    then another interpreter's ``site-packages`` on the same host. Nothing is
    fetched; a dependency the host does not have yields ``None``.
    """
    wheel = _repackage_distribution(name, outdir)
    if wheel is not None:
        return wheel
    normalized = _dist_info_name(name)
    for root in _host_purelibs():
        for candidate in sorted(root.glob(f"{normalized}-*.dist-info")):
            wheel = wheel_from_payload(root, candidate, outdir)
            if wheel is not None:
                return wheel
    return None


def _pip_install(wheel: Path, venv_dir: Path) -> Optional[str]:
    """Install one wheel, offline, returning a refusal reason or ``None``."""
    proc = _run(
        [_venv_python(venv_dir), "-m", "pip", "install", "--no-index",
         "--no-deps", "--ignore-installed", str(wheel)],
        env=_clean_env(), cwd=venv_dir,
    )
    if proc.returncode != 0:
        return f"wheel {wheel.name} failed to install: {proc.stderr.strip()[:160]}"
    return None


def provision_dependency(
    name: str,
    venv_dir: Path,
    *,
    wheelhouse: Optional[Path] = None,
    wheel_root: Optional[Path] = None,
) -> Optional[str]:
    """Provision one declared dependency; return ``None`` on success.

    Order: the declared wheelhouse, then a locally installed distribution
    repackaged into a wheel. A dependency with neither is an explicit refusal,
    so a caller can name exactly which dependency turned into a selective
    ignore instead of a skip.
    """
    wheel = _local_wheel(name, wheelhouse)
    if wheel is None:
        outdir = Path(wheel_root) if wheel_root is not None else venv_dir / "wheels"
        wheel = dependency_wheel(name, outdir)
    if wheel is not None:
        reason = _pip_install(wheel, venv_dir)
        if reason is None:
            return None
        return f"{name}: {reason}"
    return (
        f"{name}: no wheel in the declared wheelhouse and no distribution on the "
        f"running interpreter; offline, so it cannot be provided"
    )


def _verify_no_source_link(venv_dir: Path, repo_root: Path) -> None:
    """Assert the environment is non-editable: no link back to the checkout."""
    site = _venv_site(venv_dir)
    linked: list[str] = []
    for pattern in ("*.pth", "*.egg-link"):
        for path in site.glob(pattern):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                line = line.strip()
                if line and str(repo_root / "src") in line:
                    linked.append(f"{path.name}: {line}")
    if linked:
        raise EnvironmentBuildError(
            f"the built environment links the checkout source tree ({linked}); it "
            f"is editable and therefore not evidence"
        )


def _python_executable(minor: str) -> Path:
    found = shutil.which(f"python{minor}")
    if found:
        return Path(found)
    raise EnvironmentBuildError(
        f"declared interpreter python{minor} is not available on this host"
    )


def build_environment(
    minor: str,
    declaration: EnvironmentDeclaration,
    env_root: Path,
    *,
    sdk_dir: Optional[Path | str] = None,
    wheelhouse: Optional[Path] = None,
    refusals: Optional[list[str]] = None,
) -> Path:
    """Build one declared, complete, non-editable environment.

    The interpreter is taken from the declared input; the pinned SDK is built
    from ``SKILLWEAVE_SDK_DIR`` (or the sibling checkout) and installed
    non-editably; every declared collection and runner dependency is
    provisioned from the declared wheelhouse, else from the running
    interpreter's own distribution, else it is *refused* by name.

    By default an incomplete environment raises :class:`EnvironmentBuildError`
    so a caller can never treat a partial environment as evidence. A caller
    that passes ``refusals`` receives the refusal list instead and keeps the
    built environment, so it can inspect what *was* provided (and prove the
    install is non-editable) while still reporting the environment as
    incomplete; the contract stays complete-or-refuse.
    """
    if minor not in declaration.pythons:
        raise EnvironmentBuildError(
            f"interpreter {minor} is not declared by {declaration.path}"
        )
    interpreter = _python_executable(minor)
    env_root.mkdir(parents=True, exist_ok=True)
    venv_dir = env_root / f"py{minor.replace('.', '')}"
    venv.EnvBuilder(with_pip=True).create(venv_dir)

    resolved_sdk = resolve_sdk_dir(sdk_dir, repo_root=declaration.repo_root)
    wheel_out = env_root / f"wheels-py{minor.replace('.', '')}"
    wheel = _build_sdk_wheel(resolved_sdk, wheel_out)
    reason = _pip_install(wheel, venv_dir)
    if reason is not None:
        raise EnvironmentBuildError(
            f"the pinned SDK wheel {wheel.name} did not install into py{minor}: {reason}"
        )

    collected: list[str] = []
    for name in declaration.collection_dependencies + declaration.runner_dependencies:
        dep_root = env_root / f"deps-{_dist_info_name(name)}"
        dep_root.mkdir(parents=True, exist_ok=True)
        reason = provision_dependency(
            name, venv_dir, wheelhouse=wheelhouse, wheel_root=dep_root
        )
        if reason is not None:
            collected.append(reason)
    _verify_no_source_link(venv_dir, declaration.repo_root)
    if refusals is not None:
        refusals.extend(collected)
    if collected and refusals is None:
        raise EnvironmentBuildError(
            "incomplete environment for py"
            f"{minor}; refused dependencies: " + "; ".join(collected)
        )
    # The declared interpreter must be the one that actually built the venv.
    probe = _run(
        [_venv_python(venv_dir), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        env=_clean_env(), cwd=venv_dir,
    )
    actual = probe.stdout.strip()
    if actual != minor:
        raise EnvironmentBuildError(
            f"the py{minor} environment reports interpreter {actual!r}"
        )
    return venv_dir


# ── the receipt: interpreter, SDK, lock digest and SKILLWEAVE_SDK_DIR ────────

#: Distribution name → import name, for names that differ from a plain
#: ``-``→``_`` substitution.
_IMPORT_NAMES = {
    "PyYAML": "yaml",
    "pytest-asyncio": "pytest_asyncio",
    "skillweave-sdk": _SDK_MODULE,
}


def import_name(distribution: str) -> str:
    """The importable module name for a declared distribution name."""
    return _IMPORT_NAMES.get(distribution, distribution.replace("-", "_"))


def declared_distributions(declaration: EnvironmentDeclaration) -> Tuple[str, ...]:
    """Every distribution the declared input requires to collect *and* run."""
    return tuple(declaration.collection_dependencies) + tuple(
        declaration.runner_dependencies
    )


_INTERPRETER_PROBE = r'''
import json, sys
print(json.dumps({
    "path": sys.executable,
    "version": sys.version.split()[0],
    "short": "%d.%d" % sys.version_info[:2],
}))
'''

_DEP_PROBE = r'''
import importlib.util, json, sys
names = json.loads(sys.argv[1])
print(json.dumps({n: importlib.util.find_spec(n) is not None for n in names}))
'''


def _interpreter_facts(python: Path) -> Tuple[str, str, str]:
    """``(path, version, python_short)`` read from the interpreter itself."""
    proc = _run([python, "-c", _INTERPRETER_PROBE], env=_clean_env(), cwd=python.parent)
    if proc.returncode != 0:
        raise EnvironmentBuildError(
            f"cannot read interpreter facts from {python}: {proc.stderr.strip()[:300]}"
        )
    facts = json.loads(proc.stdout.strip().splitlines()[-1])
    return (
        str(Path(facts["path"]).resolve()),
        str(facts["version"]),
        str(facts["short"]),
    )


def probe_dependencies(python: Path, names: Iterable[str]) -> dict[str, bool]:
    """Which of ``names`` (import names) the interpreter can import."""
    wanted = list(names)
    proc = _run(
        [python, "-c", _DEP_PROBE, json.dumps(wanted)],
        env=_clean_env(), cwd=python.parent,
    )
    if proc.returncode != 0:
        raise EnvironmentBuildError(
            f"cannot probe dependencies with {python}: {proc.stderr.strip()[:300]}"
        )
    return {str(k): bool(v) for k, v in json.loads(proc.stdout.strip().splitlines()[-1]).items()}


@dataclass(frozen=True)
class EnvironmentAttestation:
    """A test-environment receipt bound to the candidate it attests.

    It records exactly what criterion 3 requires -- interpreter path/version,
    the SDK version/origin/install type, the declaration's dependency-lock
    digest and the resolved ``SKILLWEAVE_SDK_DIR`` -- plus the environment path
    and the dependencies that were observed importable.
    """

    interpreter_path: str
    interpreter_version: str
    python_short: str
    sdk_version: str
    sdk_origin: str
    sdk_install_type: str
    dependency_lock_digest: str
    sdk_dir: str
    environment_path: str = ""
    provided_dependencies: Tuple[str, ...] = ()
    missing_dependencies: Tuple[str, ...] = ()

    schema: str = ENV_ATTESTATION_SCHEMA_ID
    schema_version: str = ENV_ATTESTATION_SCHEMA_VERSION

    @property
    def complete(self) -> bool:
        """Whether every declared dependency was observed importable."""
        return not self.missing_dependencies

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "interpreter_path": self.interpreter_path,
            "interpreter_version": self.interpreter_version,
            "python_short": self.python_short,
            "sdk_version": self.sdk_version,
            "sdk_origin": self.sdk_origin,
            "sdk_install_type": self.sdk_install_type,
            "dependency_lock_digest": self.dependency_lock_digest,
            "sdk_dir": self.sdk_dir,
            "environment_path": self.environment_path,
            "provided_dependencies": list(self.provided_dependencies),
            "missing_dependencies": list(self.missing_dependencies),
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, receipt: Mapping[str, Any]) -> "EnvironmentAttestation":
        verify_attestation(receipt)
        return cls(
            interpreter_path=str(receipt["interpreter_path"]),
            interpreter_version=str(receipt["interpreter_version"]),
            python_short=str(receipt["python_short"]),
            sdk_version=str(receipt["sdk_version"]),
            sdk_origin=str(receipt["sdk_origin"]),
            sdk_install_type=str(receipt["sdk_install_type"]),
            dependency_lock_digest=str(receipt["dependency_lock_digest"]),
            sdk_dir=str(receipt["sdk_dir"]),
            environment_path=str(receipt.get("environment_path", "")),
            provided_dependencies=tuple(
                str(v) for v in receipt.get("provided_dependencies", ())
            ),
            missing_dependencies=tuple(
                str(v) for v in receipt.get("missing_dependencies", ())
            ),
        )


def verify_attestation(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse an incomplete or foreign attestation receipt.

    A receipt missing any criterion-3 field, or carrying a schema other than
    ``execution-environment-attestation/v1``, raises :class:`AttestationError`
    naming the defect; it never degrades into "unknown".
    """
    if not isinstance(receipt, Mapping):
        raise AttestationError(
            f"an {ENV_ATTESTATION_SCHEMA_ID} receipt is a mapping, got "
            f"{type(receipt).__name__}"
        )
    schema = receipt.get("schema")
    if schema != ENV_ATTESTATION_SCHEMA_ID:
        raise AttestationError(
            f"receipt declares schema {schema!r}, expected {ENV_ATTESTATION_SCHEMA_ID!r}"
        )
    absent = [f for f in REQUIRED_RECEIPT_FIELDS if not str(receipt.get(f, "")).strip()]
    if absent:
        raise AttestationError(
            f"receipt is missing required field(s) {absent}; every test receipt "
            f"must record {list(REQUIRED_RECEIPT_FIELDS)}"
        )
    return dict(receipt)


def build_attestation(
    venv_dir: Path,
    declaration: EnvironmentDeclaration,
    *,
    sdk_dir: Optional[Path | str] = None,
) -> EnvironmentAttestation:
    """Build the receipt for a *built* environment, from observed facts."""
    python = _venv_python(venv_dir)
    interpreter_path, version, short = _interpreter_facts(python)
    sdk = _sdk_attestation(venv_dir)
    resolved = resolve_sdk_dir(sdk_dir, repo_root=declaration.repo_root)
    availability = probe_dependencies(
        python, (import_name(d) for d in declared_distributions(declaration))
    )
    provided = tuple(
        d for d in declared_distributions(declaration) if availability.get(import_name(d))
    )
    missing = tuple(
        d for d in declared_distributions(declaration) if not availability.get(import_name(d))
    )
    return EnvironmentAttestation(
        interpreter_path=interpreter_path,
        interpreter_version=version,
        python_short=short,
        sdk_version=sdk.version,
        sdk_origin=sdk.origin,
        sdk_install_type=sdk.install_type,
        dependency_lock_digest=declaration.dependency_lock_digest,
        sdk_dir=str(resolved),
        environment_path=str(Path(venv_dir).resolve()),
        provided_dependencies=provided,
        missing_dependencies=missing,
    )


# ── the session guard: no selective ignore caused by a missing dependency ────


def guard_session(
    declaration: EnvironmentDeclaration,
    *,
    venv_dir: Optional[Path] = None,
    session_python: Optional[Path | str] = None,
    sdk_dir: Optional[Path | str] = None,
    available: Optional[Mapping[str, bool]] = None,
    receipt: Optional[Mapping[str, Any]] = None,
) -> EnvironmentAttestation:
    """Refuse to run the unit suite in a session that is not the declared one.

    Two distinct refusals, each naming its cause:

    * a dependency the declaration requires is missing -- the *selective-ignore
      cause*. The suite would otherwise collect with a silent ``importorskip``
      skip; the guard refuses instead, naming every missing distribution.
    * ``receipt`` is given and does not match this session -- a different
      interpreter, a changed dependency-lock digest, another SDK root or a
      changed ``SKILLWEAVE_SDK_DIR``.

    On success it returns the session's own :class:`EnvironmentAttestation`.
    ``available`` and ``session_python`` are the injection seams: the gate
    runner passes a pre-collected availability map, the default probes the
    interpreter.
    """
    python = (
        _venv_python(venv_dir)
        if venv_dir is not None
        else Path(session_python) if session_python is not None else Path(sys.executable)
    )
    distributions = declared_distributions(declaration)

    if available is None:
        availability = probe_dependencies(
            python, (import_name(d) for d in distributions)
        )
    else:
        availability = {
            import_name(d): bool(available.get(import_name(d)) or available.get(d))
            for d in distributions
        }

    missing = [d for d in distributions if not availability.get(import_name(d))]
    if missing:
        raise SessionGuardError(
            "selective-ignore cause: the declared environment requires "
            f"{missing} but this session cannot import "
            f"{[import_name(d) for d in missing]}. The full unit suite would "
            "collect with those dependencies silently skipped (pytest.importorskip); "
            "run the suite in a built, complete environment instead."
        )

    interpreter_path, version, short = _interpreter_facts(python)
    resolved = resolve_sdk_dir(sdk_dir, repo_root=declaration.repo_root)

    # The receipt binding is checked from the *session's own* recomputable facts
    # -- interpreter identity, the declared lock digest and the resolved SDK
    # root -- before the SDK is imported. A receipt from another session is
    # therefore refused on its own evidence, without depending on whether this
    # session happens to have the pinned SDK installed.
    session_facts = {
        "interpreter_path": interpreter_path,
        "interpreter_version": version,
        "python_short": short,
        "dependency_lock_digest": declaration.dependency_lock_digest,
        "sdk_dir": str(resolved),
    }
    if receipt is not None:
        verify_attestation(receipt)
        for field, value in session_facts.items():
            if str(receipt.get(field, "")) != str(value):
                raise SessionGuardError(
                    f"the session's {field} {value!r} does not "
                    f"match the receipt's {str(receipt.get(field))!r}; the receipt "
                    f"attests a different environment"
                )
        incomplete = tuple(str(v) for v in receipt.get("missing_dependencies", ()))
        if incomplete:
            raise SessionGuardError(
                "selective-ignore cause: the receipt attests an *incomplete* "
                f"environment that did not provide {list(incomplete)}; the full "
                f"unit suite would collect with those dependencies skipped"
            )

    sdk = _sdk_attestation(venv_dir) if venv_dir is not None else _sdk_attestation_for(python)
    return EnvironmentAttestation(
        interpreter_path=interpreter_path,
        interpreter_version=version,
        python_short=short,
        sdk_version=sdk.version,
        sdk_origin=sdk.origin,
        sdk_install_type=sdk.install_type,
        dependency_lock_digest=declaration.dependency_lock_digest,
        sdk_dir=str(resolved),
        environment_path=str(Path(venv_dir).resolve()) if venv_dir is not None else "",
        provided_dependencies=tuple(distributions),
    )


def _sdk_attestation_for(python: Path) -> SdkAttestation:
    """The SDK facts for an arbitrary interpreter (not necessarily a built venv)."""
    proc = _run([python, "-c", _SDK_PROBE], env=_clean_env(), cwd=python.parent)
    if proc.returncode != 0:
        raise SessionGuardError(
            f"cannot read SDK facts from {python}: {proc.stderr.strip()[:300]}"
        )
    facts = json.loads(proc.stdout.strip().splitlines()[-1])
    if not facts.get("found"):
        raise SessionGuardError(
            f"{python} cannot import {_SDK_MODULE}; the pinned SDK is not installed "
            f"in this session"
        )
    return SdkAttestation(
        version=facts["version"],
        origin=f"observed in {python}",
        install_type=facts["install_type"],
        package_path=facts["package_path"],
        dist_info_path=facts["dist_info_path"],
    )
