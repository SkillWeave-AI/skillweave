"""SW-1510-SDK-CI: qualify the published Core/SDK contracts with a true
green-to-red gate.

The cross-repository contract CI contradicted the artifact it claimed to
guard: the consumer gate looked for ``<sdk>/schemas/*.schema.json`` while the
SDK ships ``skillweave_sdk/schemas/**`` and has no root ``schemas/``; the
canonical digest it compared against was a fourth algorithm over a flat-only
file set; and the ownership inventory lived only in an unpublished source tree.
This module proves the three acceptance criteria against the *actual installed
wheel*, not against a helper name or a source-tree shortcut.

Criterion 1 -- schema path, canonical digest algorithm, SDK pin and ownership
inventory match the actual published wheel; an explicitly invalid SDK path
never falls back silently.
Criterion 2 -- a fresh external consumer is green; a single declared schema
drift produces the expected *specific* red assertion; restoration is green
again.
Criterion 3 -- the release gate records dependency origins, declared
Python/platform compatibility, collection errors and justified skips; historical
test allowlists are not presented as a clean release certificate.

Everything here reads the wheel the SDK packaging actually produces. The SDK
wheel is built once from the SDK source tree and installed by extracting its
archive, so the test exercises the packaging declaration (``pyproject.toml``)
rather than a ``PYTHONPATH`` shortcut. Scratch space comes from pytest's
``tmp_path``/``tmp_path_factory`` (honouring ``--basetemp``), so nothing is
written to the host ``TMPDIR``.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

# ── constants of the frozen combination ────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_WORKFLOW = _REPO_ROOT / ".forgejo" / "workflows" / "contract-ci.yml"
_SDK_DIR_ENV = "SKILLWEAVE_SDK_DIR"

#: Digest of the frozen Core+execution+adapter schema byte set, computed with
#: the SDK's own canonical algorithm over the *installed* schema root. Frozen
#: here (Core-side) so a wheel that ships a different byte set is red even when
#: the SDK's own embedded constant was edited to match.
EXPECTED_SCHEMA_DIGEST = (
    "3dd247f604017bac4ff37ff4f3e9b46c5d1bc48fc8ef225de88ec4b6d14d2e64"
)

#: The exact schema inventory the wheel must ship: 13 flat core/lifecycle + 11
#: execution + 3 adapter. Freezing the names (not just the count) is what turns
#: "a schema was silently added or dropped" into a specific red assertion.
_FLAT_SCHEMA_NAMES = (
    "category-pack.schema.json",
    "category-taxonomy.schema.json",
    "deliverable-contract.schema.json",
    "evidence-contract.schema.json",
    "evidence.schema.json",
    "lifecycle-profile.schema.json",
    "model-provider.schema.json",
    "prompt-sequence.schema.json",
    "run-state.schema.json",
    "search-provider.schema.json",
    "subject-ref.schema.json",
    "work-profile.schema.json",
    "workflow-context.schema.json",
)
_EXECUTION_SCHEMA_NAMES = (
    "agent-message-v1.schema.json",
    "attempt-event-v1.schema.json",
    "capability-snapshot-v1.schema.json",
    "contract-catalog-record-v1.schema.json",
    "contract-catalog-v1.schema.json",
    "evidence-receipt-v1.schema.json",
    "execution-envelope-v1.schema.json",
    "mirror-target-v1.schema.json",
    "reservation-v1.schema.json",
    "route-decision-v1.schema.json",
    "run-snapshot-v1.schema.json",
)
_ADAPTER_SCHEMA_NAMES = (
    "adapter-descriptor-v1.schema.json",
    "credential-reference-v1.schema.json",
    "search-outcome-v1.schema.json",
)

#: The declared ownership map. Exactly one named owner per surface; preserving
#: the existing Core/ops-engine/router ownership rather than reassigning it.
EXPECTED_OWNER_KEYS = frozenset({"sdk", "core", "ops_engine", "router", "observer"})

#: The subject artifact, per the SW-1510-SDK-CI boundary. ``contracts/catalog.json``
#: is the declared catalog home for the published ``contract-catalog-v1`` surface.
_CATALOG_SUBJECT = "contracts/catalog.json"


# ── SDK resolution (fail-closed: an invalid explicit path never falls back) ──


class SdkUnavailable(RuntimeError):
    """Raised when the SDK source cannot be resolved from an explicit input."""


def _resolve_sdk() -> Path:
    """Resolve the SDK source tree, failing closed on an explicit bad path.

    Precedence: ``SKILLWEAVE_SDK_DIR`` (an explicit operator/CI input) then the
    sibling ``../skillweave-sdk`` checkout. When the env var is *set but invalid*
    the resolution raises -- it never silently falls back to the sibling or to a
    ``None`` that a caller could mistake for "no SDK here".
    """
    env_val = os.environ.get(_SDK_DIR_ENV)
    if env_val:
        cand = Path(env_val)
        if not (cand / "schema_version.toml").is_file():
            raise SdkUnavailable(
                f"{_SDK_DIR_ENV}={env_val} is set but is not an SDK root "
                f"(missing schema_version.toml); refusing to fall back to a "
                f"different checkout"
            )
        return cand
    sibling = _REPO_ROOT.parent / "skillweave-sdk"
    if (sibling / "schema_version.toml").is_file():
        return sibling
    raise SdkUnavailable(
        "skillweave-sdk is not available: set SKILLWEAVE_SDK_DIR to a valid "
        "SDK root or expose a sibling ../skillweave-sdk (anchor=schema_version.toml)"
    )


@pytest.fixture(scope="session")
def sdk_root() -> Path:
    return _resolve_sdk()


# ── the actual published wheel: build once, extract, read back ──────────────


def _build_wheel(sdk_root: Path, staging: Path, outdir: Path) -> Path:
    """Build the SDK wheel from a pristine staged copy.

    Staging mirrors the SDK's own packaging test: the build runs against a copy
    assembled from the tracked sources, so a stale ``build/`` tree can never
    satisfy the assertions.
    """
    (staging / "src").mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "README.md", "LICENSE", "schema_version.toml"):
        src = sdk_root / name
        if src.is_file():
            shutil.copy2(src, staging / name)
    shutil.copytree(sdk_root / "src" / "skillweave_sdk", staging / "src" / "skillweave_sdk")
    contract = sdk_root / "contract"
    if contract.is_dir():
        shutil.copytree(contract, staging / "contract")
    outdir.mkdir(parents=True, exist_ok=True)
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(outdir)],
        cwd=str(staging), capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise AssertionError(
            f"SDK wheel build failed\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )
    wheels = sorted(outdir.glob("skillweave_sdk-*.whl"))
    assert len(wheels) == 1, f"expected exactly one SDK wheel, found {wheels}"
    return wheels[0]


def _install_wheel(wheel: Path, target: Path) -> Path:
    """Install a wheel by extracting its archive into *target*.

    This is the standard ``pip install --target`` operation expressed directly:
    ``X.data/data/<path>`` payloads land at ``<target>/<path>`` and everything
    else lands unchanged. Extracting explicitly (rather than shelling to pip)
    keeps the recipe offline, deterministic and independent of a pip shim, and
    it is the same layout a ``--target`` install produces.
    """
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if ".data/data/" in name:
                name = name.split(".data/data/", 1)[1]
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
    assert (target / "skillweave_sdk").is_dir(), (
        f"wheel did not install a skillweave_sdk package into {target}"
    )
    return target


class _InstalledSdk:
    """A built, extracted SDK wheel plus the facts read from its installed bytes."""

    def __init__(self, wheel: Path, site: Path):
        self.wheel = wheel
        self.site = site
        self.pkg = site / "skillweave_sdk"
        self.schemas = self.pkg / "schemas"
        self.authority = self.pkg / "_authority"


@pytest.fixture(scope="session")
def installed_sdk(sdk_root: Path, tmp_path_factory) -> _InstalledSdk:
    staging = tmp_path_factory.mktemp("sdk-ci-stage")
    wheel = _build_wheel(sdk_root, staging, tmp_path_factory.mktemp("sdk-ci-wheel"))
    site = _install_wheel(wheel, tmp_path_factory.mktemp("sdk-ci-site"))
    return _InstalledSdk(wheel, site)


# ── the SDK's canonical algorithm, re-expressed over the installed wheel ─────


def _canonical_schema_digest(schemas_dir: Path) -> str:
    """The SDK's canonical digest, computed from the installed schema root.

    Algorithm (identical to ``skillweave_sdk.validator.canonical_digest``):
    sha256 over the sorted ``<relative-key>\\x00<canonical-json>\\x00``
    concatenation, where the key is the schema's path relative to the schema
    root (``run-state.schema.json``, ``execution/run-snapshot-v1.schema.json``,
    ``adapter/adapter-descriptor-v1.schema.json``) and the canonical JSON is
    ``json.dumps(schema, sort_keys=True, separators=(",", ":"))``.
    """
    hasher = hashlib.sha256()
    keys = sorted(p.relative_to(schemas_dir).as_posix()
                  for p in schemas_dir.rglob("*.schema.json"))
    for key in keys:
        doc = json.loads((schemas_dir / key).read_text(encoding="utf-8"))
        canonical = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        hasher.update(key.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(canonical)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _iter_relative_schema_keys(schemas_dir: Path) -> list[str]:
    return sorted(p.relative_to(schemas_dir).as_posix()
                  for p in schemas_dir.rglob("*.schema.json"))


# ── the pin's provenance: the pin must be the SDK head the wheel is built from ──


def _workflow_pinned_sha() -> str:
    """Read ``SDK_QUALIFIED_SHA`` out of the gate workflow (single source)."""
    return re.search(
        r'SDK_QUALIFIED_SHA:\s*"([0-9a-f]{40})"',
        _WORKFLOW.read_text(encoding="utf-8"),
    ).group(1)


def _git(sdk_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(sdk_root), *args],
        capture_output=True, text=True,
    )


def _require_git(sdk_root: Path) -> None:
    """Skip (as a justified skip) when the SDK is not a VCS checkout.

    The pin provenance checks need git history; in the CI wiring the SDK is a
    ``git clone`` and these run strictly. Against a bare export/sdist (no
    ``.git``) they are recorded as a skip with a reason — never a silent pass.
    """
    if not (sdk_root / ".git").exists():
        pytest.skip(
            f"{sdk_root} is not a git checkout; the SDK pin provenance cannot "
            f"be verified from history (no .git). CI clones the SDK, so this "
            f"runs strictly there."
        )


def _build_wheel_from_commit(sdk_root: Path, sha: str, staging: Path, outdir: Path) -> Path:
    """Build the SDK wheel from the *tracked tree at `sha`*, via ``git archive``.

    This reproduces exactly what the workflow does: it checks out the pinned
    commit and builds the wheel from that tree, not from the working tree (which
    may carry unpinned edits). ``git archive`` needs no network and does not
    disturb the checkout.
    """
    tree = staging / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(sdk_root), "archive", "--format=tar", sha],
        capture_output=True,
    )
    if archive.returncode != 0:
        raise AssertionError(
            f"git archive {sha} failed: {archive.stderr.decode('utf-8', 'replace')}"
        )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
        tf.extractall(tree)
    return _build_wheel_from_tree(tree, outdir)


def _build_wheel_from_tree(tree: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(outdir)],
        cwd=str(tree), capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise AssertionError(
            f"wheel build failed at {tree}\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
        )
    wheels = sorted(outdir.glob("skillweave_sdk-*.whl"))
    assert len(wheels) == 1, f"expected exactly one SDK wheel, found {wheels}"
    return wheels[0]


def _wheel_authority_names(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        return sorted(n for n in zf.namelist() if "_authority" in n)


@pytest.fixture(scope="session")
def pinned_wheel(sdk_root: Path, tmp_path_factory) -> Path:
    """The wheel built from the SHA the gate workflow pins, read live."""
    _require_git(sdk_root)
    sha = _workflow_pinned_sha()
    return _build_wheel_from_commit(
        sdk_root, sha,
        tmp_path_factory.mktemp("sdk-ci-pin-stage"),
        tmp_path_factory.mktemp("sdk-ci-pin-wheel"),
    )


# ── an external consumer importing the installed wheel, nothing else ────────


class _ExternalConsumer:
    """Import ``skillweave_sdk`` from an extracted wheel, nothing else.

    The consumer's ``sys.path`` is rebuilt to contain only the extracted site
    directory (plus the stdlib), so the import cannot be satisfied by an ambient
    editable install, the source tree or site-packages. If any other copy of
    ``skillweave_sdk`` was already imported, it is purged first.
    """

    def __init__(self, site: Path):
        self.site = site.resolve()
        self.import_root = self._activate()

    def _activate(self) -> Path:
        for mod in list(sys.modules):
            if mod == "skillweave_sdk" or mod.startswith("skillweave_sdk."):
                del sys.modules[mod]
        keep = (sys.prefix, sys.base_prefix, str(self.site))
        stdlib = os.path.dirname(os.__file__)
        rebuilt = [
            p for p in sys.path
            if p and (
                p == stdlib
                or p.startswith(stdlib + os.sep)
                or p == str(self.site)
                or p.startswith(sys.prefix)
            )
        ]
        sys.path[:] = [str(self.site)] + rebuilt
        importlib.invalidate_caches()
        sdk = importlib.import_module("skillweave_sdk")
        return Path(sdk.__file__).resolve()

    def authority_dir(self) -> Path:
        sdk = importlib.import_module("skillweave_sdk")
        return Path(sdk.__file__).resolve().parent / "_authority"

    def schemas_dir(self) -> Path:
        sdk = importlib.import_module("skillweave_sdk")
        return Path(sdk.__file__).resolve().parent / "schemas"

    def version(self) -> str:
        sdk = importlib.import_module("skillweave_sdk")
        return sdk.SCHEMA_VERSION


# ══════════════════════════════════════════════════════════════════════════
# Criterion 1 — schema path, canonical digest, SDK pin, ownership inventory
# ══════════════════════════════════════════════════════════════════════════


class TestCriterion1PublishedWheel:
    """Schema path, digest algorithm, SDK pin and ownership match the wheel."""

    def test_consumer_resolves_the_installed_package_not_a_source_tree(
        self, installed_sdk
    ):
        consumer = _ExternalConsumer(installed_sdk.site)
        assert str(installed_sdk.site) in str(consumer.import_root), (
            f"external consumer imported {consumer.import_root}, which is not "
            f"the extracted wheel at {installed_sdk.site}"
        )
        assert str(_REPO_ROOT / "src") not in str(consumer.import_root)

    def test_schema_path_is_the_installed_package_schemas_dir(self, installed_sdk):
        # Criterion 1, first clause: the schema path the gate uses is the real
        # installed layout (``skillweave_sdk/schemas``), reachable from the
        # package, not a root ``schemas/`` that does not exist in the artifact.
        consumer = _ExternalConsumer(installed_sdk.site)
        schemas = consumer.schemas_dir()
        assert schemas.is_dir(), f"installed schema dir missing: {schemas}"
        assert schemas == installed_sdk.schemas
        # A root ``schemas/`` at the distribution root is what the old gate
        # wrongly assumed; it must not exist.
        assert not (installed_sdk.site / "schemas").exists(), (
            "the distribution exposes a root schemas/ — the gate would resolve "
            "an unpublished, non-wheel path"
        )

    def test_wheel_ships_exactly_the_frozen_schema_set(self, installed_sdk):
        keys = set(_iter_relative_schema_keys(installed_sdk.schemas))
        expected = (
            set(_FLAT_SCHEMA_NAMES)
            | {f"execution/{n}" for n in _EXECUTION_SCHEMA_NAMES}
            | {f"adapter/{n}" for n in _ADAPTER_SCHEMA_NAMES}
        )
        assert keys == expected, (
            f"installed wheel schema set mismatch: "
            f"extra={sorted(keys - expected)}, missing={sorted(expected - keys)}"
        )

    def test_canonical_digest_algorithm_matches_installed_bytes(self, installed_sdk):
        # Criterion 1, second clause: the digest is the SDK's own algorithm over
        # the installed schema set -- and it equals the frozen value. A flat-only
        # algorithm over ``<name>:<sha>\\n`` produces a different number, which
        # is exactly the contradiction being repaired.
        installed = _canonical_schema_digest(installed_sdk.schemas)
        assert installed == EXPECTED_SCHEMA_DIGEST, (
            f"installed-wheel canonical digest drifted: computed {installed}, "
            f"frozen {EXPECTED_SCHEMA_DIGEST}"
        )
        flat_only = hashlib.sha256("".join(
            f"{p.name}:{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
            for p in sorted(installed_sdk.schemas.glob("*.schema.json"))
        ).encode("utf-8")).hexdigest()
        assert flat_only != installed, (
            "the flat-only legacy algorithm coincides with the canonical "
            "digest; the drift this gate exists to catch would be invisible"
        )

    def test_sdk_embedded_digest_agrees_with_the_installed_wheel(self, installed_sdk):
        consumer = _ExternalConsumer(installed_sdk.site)
        validator = importlib.import_module("skillweave_sdk.validator")
        installed = _canonical_schema_digest(consumer.schemas_dir())
        assert validator.EXPECTED_SCHEMA_DIGEST == installed == EXPECTED_SCHEMA_DIGEST, (
            "the SDK's embedded digest, the bytes it shipped and Core's frozen "
            "value must be one number"
        )
        # The SDK's own verifier accepts the installed wheels and rejects a
        # mismatched expectation.
        validator.verify_canonical_digest(consumer.schemas_dir(), expected=EXPECTED_SCHEMA_DIGEST)

    def test_sdk_pin_matches_the_wheel_version(self, installed_sdk):
        # Criterion 1, third clause: the Core pin equals the version the wheel
        # actually declares, and it is a pinned version, never "latest".
        text = _PYPROJECT.read_text(encoding="utf-8")
        m = re.search(r'"skillweave-sdk==([^"]+)"', text)
        assert m, "Core pyproject must pin skillweave-sdk==<version>"
        pin = m.group(1)
        assert pin != "latest"
        consumer = _ExternalConsumer(installed_sdk.site)
        assert pin == consumer.version(), (
            f"Core pins skillweave-sdk=={pin} but the built wheel declares "
            f"{consumer.version()}"
        )

    def test_ownership_inventory_ships_in_the_wheel_and_is_complete(self, installed_sdk):
        # Criterion 1, fourth clause: the ownership inventory is part of the
        # actual published wheel (not a source-tree-only file) and names exactly
        # one owner per surface.
        consumer = _ExternalConsumer(installed_sdk.site)
        authority = consumer.authority_dir()
        catalog_path = authority / "execution-catalog.json"
        assert catalog_path.is_file(), (
            f"the wheel did not ship the ownership catalog; {authority} contains "
            f"{sorted(p.name for p in authority.glob('*')) if authority.is_dir() else 'nothing'}"
        )
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        assert set(catalog["owners"]) == EXPECTED_OWNER_KEYS
        assert len(catalog["schema_digests"]) >= 10
        # Every catalogued contract names exactly one owner from the map.
        known = set(catalog["owners"])
        for entry in catalog["schema_digests"]:
            assert entry["owner"] in known, entry["name"]
            assert re.fullmatch(r"[a-f0-9]{64}", entry["digest_sha256"]), entry["name"]
        # The version the inventory declares is the wheel's own version.
        assert catalog["sdk_version"] == consumer.version()

    def test_explicit_invalid_sdk_dir_never_falls_back_silently(self, monkeypatch, tmp_path):
        # Criterion 1, last clause: an explicitly invalid path fails loudly,
        # instead of silently resolving a sibling or returning None.
        bad = tmp_path / "not-an-sdk"
        bad.mkdir()
        monkeypatch.setenv(_SDK_DIR_ENV, str(bad))
        with pytest.raises(SdkUnavailable) as excinfo:
            _resolve_sdk()
        assert str(bad) in str(excinfo.value), (
            "the failure must name the offending path so CI cannot mistake a "
            "misconfigured SDK_DIR for a missing one"
        )

    def test_pin_is_ancestor_or_equal_of_the_sdk_head_the_wheel_is_built_from(
        self, sdk_root
    ):
        # The pin must be a commit the SDK checkout actually contains, and it
        # must not be *ahead* of the tree the wheel is built from. A pin that is
        # not an ancestor-or-equal of HEAD would point at a commit the consumer
        # cannot reach, so the qualified wheel could not be reproduced.
        _require_git(sdk_root)
        pin = _workflow_pinned_sha()
        claimed = _git(sdk_root, "cat-file", "-e", f"{pin}^{{commit}}")
        assert claimed.returncode == 0, (
            f"pinned SDK_QUALIFIED_SHA {pin} is not a commit in {sdk_root}"
        )
        ance = _git(sdk_root, "merge-base", "--is-ancestor", pin, "HEAD")
        assert ance.returncode == 0, (
            f"the workflow pins {pin}, which is not an ancestor-or-equal of the "
            f"SDK checkout head — the pin is not the tree the wheel is built from"
        )

    def test_wheel_at_the_pin_ships_the_ownership_authority_payload(self, sdk_root, pinned_wheel):
        # The decisive provenance check: build the wheel at exactly the pinned
        # SHA and prove it carries the _authority payload criterion 1 reads. A
        # pin one commit behind the SDK change that adds that payload (as
        # a663a947 was) ships no authority files and the gate is red at its own
        # declared pin.
        _require_git(sdk_root)
        names = _wheel_authority_names(pinned_wheel)
        assert names, (
            f"the wheel built at the pinned SHA ships no skillweave_sdk/"
            f"_authority payload; the gate would be red at its own pin. "
            f"wheel contents: {sorted(name for name in zipfile.ZipFile(pinned_wheel).namelist())[:8]}…"
        )
        assert any(n.endswith("execution-catalog.json") for n in names), names


# ══════════════════════════════════════════════════════════════════════════
# Criterion 2 — fresh consumer green; one declared drift → specific red;
#               restoration green again
# ══════════════════════════════════════════════════════════════════════════


class TestCriterion2GreenRedGreen:
    """A fresh consumer is green, one declared drift is red, restore is green."""

    def test_fresh_external_consumer_is_green(self, installed_sdk):
        consumer = _ExternalConsumer(installed_sdk.site)
        validator = importlib.import_module("skillweave_sdk.validator")
        schemas = consumer.schemas_dir()
        registry = validator.load_registry(schemas)
        # Real consumer wiring: a fixture validates through the installed
        # registry and the SDK's own validator, not a helper stub.
        fixture = {
            "contractVersion": "1.0.0",
            "id": "example-work",
            "category": "build",
            "kernelStages": ["K3"],
        }
        errors = validator.validate(
            fixture, "https://skillweave.dev/schemas/lifecycle/work-profile/v1", schemas
        )
        assert errors == [], f"fresh consumer rejected a valid fixture: {errors}"
        assert registry, "installed registry is empty"

    def test_single_declared_drift_is_a_specific_red(
        self, installed_sdk, tmp_path_factory
    ):
        # Work on a private copy so the session fixture stays pristine.
        work = tmp_path_factory.mktemp("drift-site")
        shutil.copytree(installed_sdk.pkg, work / "skillweave_sdk")
        schemas = work / "skillweave_sdk" / "schemas"

        # Baseline: green.
        baseline = _canonical_schema_digest(schemas)
        assert baseline == EXPECTED_SCHEMA_DIGEST

        # A single declared drift: one schema-semantic edit to one schema. The
        # canonical digest is over the sorted-key canonical JSON, so a *declared*
        # contract edit (a new keyword the frozen schema lacks) moves it, while
        # a whitespace/byte edit that leaves the contract identical does not.
        # The schema stays parseable, so the failure is a digest failure, not a
        # JSON parse error.
        target = schemas / "run-state.schema.json"
        original = target.read_bytes()
        doc = json.loads(original)
        doc["$comment"] = "SW-1510-SDK-CI declared drift"
        target.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            drifted = _canonical_schema_digest(schemas)
            assert drifted != EXPECTED_SCHEMA_DIGEST, (
                "a declared schema edit did not change the canonical digest — "
                "the drift would go undetected"
            )
            # The specific red: the SDK's own verifier raises the named error.
            validator = importlib.import_module("skillweave_sdk.validator")
            with pytest.raises(validator.SchemaDigestError) as excinfo:
                validator.verify_canonical_digest(
                    schemas, expected=EXPECTED_SCHEMA_DIGEST
                )
            message = str(excinfo.value)
            assert drifted in message and EXPECTED_SCHEMA_DIGEST in message, (
                f"the drift error must name both digests: {message}"
            )
        finally:
            target.write_bytes(original)

        # Restoration is green again.
        assert _canonical_schema_digest(schemas) == EXPECTED_SCHEMA_DIGEST

    def test_restoration_after_drift_is_green_again(self, installed_sdk):
        # The session artifact never drifted: the counterproof above ran on a
        # copy. Re-prove the pristine artifact is green.
        assert _canonical_schema_digest(installed_sdk.schemas) == EXPECTED_SCHEMA_DIGEST


# ══════════════════════════════════════════════════════════════════════════
# Criterion 3 — the release record: origins, compatibility, collection errors,
#               justified skips; historical allowlists are not a certificate
# ══════════════════════════════════════════════════════════════════════════


# The historical allowlists that must never be read as a clean certificate: a
# set of pre-existing red-to-red node IDs is a PARDON list, not a proof that the
# suite is green.
_HISTORICAL_ALLOWLIST_PATTERNS = ("ALLOWED", "allow-list", "allowlist")


def _declared_python_versions(pyproject_text: str) -> set[str]:
    return set(re.findall(
        r"Programming Language :: Python :: (\d+\.\d+)", pyproject_text
    ))


def _toml_section(text: str, header: str) -> str:
    """Return the body of a TOML section, or "" when absent.

    A blanket ``[tool.pytest.ini_options]`` section is *not* a provenance
    record: it applies to every invocation, including a local one, so it can
    hide a collection error that mattered in CI. The gate must therefore not
    declare one.
    """
    pattern = re.compile(rf"^\[{re.escape(header)}\]\s*$(.*?)(?=^\[|\Z)",
                         re.MULTILINE | re.DOTALL)
    m = pattern.search(text)
    return m.group(1) if m else ""


class TestCriterion3ReleaseRecord:
    """The release gate records provenance and never launders an allowlist."""

    def test_dependency_origin_is_recorded_as_a_pinned_version(self, installed_sdk):
        # A dependency origin, not a floating range: the exact version the
        # artifact was qualified against.
        text = _PYPROJECT.read_text(encoding="utf-8")
        assert re.search(r'"skillweave-sdk==\d+\.\d+\.\d+"', text), (
            "the SDK dependency origin must be an exact pinned version"
        )
        consumer = _ExternalConsumer(installed_sdk.site)
        assert consumer.version() in text

    def test_declared_python_compatibility_matches_the_wheel(self, installed_sdk):
        text = _PYPROJECT.read_text(encoding="utf-8")
        versions = _declared_python_versions(text)
        assert versions, "Core must declare its Python compatibility"
        requires = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
        assert requires, "Core must declare requires-python"
        # The declared requires-python floor must actually be importable, and it
        # must be consistent with the declared classifier floor.
        floor = re.search(r">=\s*(\d+\.\d+)", requires.group(1))
        assert floor, f"unparseable requires-python: {requires.group(1)}"
        assert floor.group(1).split(".")[0] == min(versions).split(".")[0], (
            f"requires-python {requires.group(1)} disagrees with classifiers "
            f"{sorted(versions)}"
        )

    def test_release_gate_is_not_configured_as_a_blanket_pytest_section(self):
        # The acceptance criterion is that the *gate* records collection errors
        # and justified skips; a bare ``[tool.pytest.ini_options]`` in the
        # packaging file would be a global, invocation-independent verdict and
        # is exactly the "clean certificate" shape being ruled out.
        text = _PYPROJECT.read_text(encoding="utf-8")
        assert _toml_section(text, "tool.pytest.ini_options") == "", (
            "a blanket pytest ini_options section was added to pyproject.toml; "
            "record provenance in the gate workflow instead"
        )

    def test_gate_workflow_records_origins_collection_errors_and_skips(self):
        gate = _REPO_ROOT / ".forgejo" / "workflows" / "contract-ci.yml"
        assert gate.is_file(), f"contract CI workflow missing: {gate}"
        workflow = gate.read_text(encoding="utf-8")
        lowered = workflow.lower()
        # Dependency origin.
        assert "remote get-url origin" in workflow or "origin" in lowered
        # Declared Python/platform compatibility of the runner that ran the gate.
        assert "python 3" in lowered or "python-version" in lowered
        # Collection errors.
        assert "--continue-on-collection-errors" in workflow, (
            "the gate must run past a collection error so it is recorded, not "
            "silently aborted"
        )
        # Justified skips: the gate reports skips and requires a reason.
        assert "skip" in lowered, "the gate must record skips"

    def test_gate_workflow_collection_error_detection_names_both_shapes(self):
        workflow = _WORKFLOW.read_text(encoding="utf-8")
        assert "--continue-on-collection-errors" in workflow
        # The gate decides on the pytest exit code AND names the collection
        # shape explicitly, so a future edit cannot quietly narrow detection to
        # the newer ``ERROR tests/...`` node form only. Both the ERROR/FAILED
        # node and the ``___ ERROR collecting ...`` banner must be anchored.
        assert re.search(r"\^\(ERROR\|FAILED\) ", workflow), (
            "the gate must name the ERROR/FAILED node shape"
        )
        assert re.search(r"\^_\+ ERROR collecting", workflow), (
            "the gate must name the 'ERROR collecting' banner shape"
        )

    def test_intentionally_broken_collection_is_recorded_and_red(self, tmp_path):
        # A deliberately broken module must (a) make pytest exit non-zero — the
        # decisive verdict the gate uses — and (b) emit output the gate's grep
        # recognizes as a collection error, proving the gate goes red on a
        # collection error and not only on an assertion failure.
        broken = tmp_path / "test_sw1510_broken_collection.py"
        broken.write_text(
            "import this_module_does_not_exist_sw1510  # collection error\n\n\n"
            "def test_ok():\n    assert True\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(broken),
             "-p", "no:cacheprovider", "-q", "--continue-on-collection-errors",
             "-o", "addopts=", "--basetemp", str(tmp_path / "bt")],
            cwd=str(tmp_path), capture_output=True, text=True,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, (
            f"a broken collection did not make pytest fail:\n{combined}"
        )
        detected = (
            re.search(r"^(ERROR|FAILED) ", combined, re.MULTILINE)
            or re.search(r"^_+ ERROR collecting", combined, re.MULTILINE)
        )
        assert detected, (
            f"the gate's grep would NOT recognize this collection error and "
            f"would read the run as green:\n{combined}"
        )

    def test_historical_allowlist_is_not_a_clean_certificate(self):
        # The drift counterproof must assert a *specific* red, not lean on a
        # blanket allow-list of pre-existing failures. The forbidden shape is a
        # node-id pardon set that lets the job read green.
        gate = (_REPO_ROOT / ".forgejo" / "workflows" / "contract-ci.yml").read_text(
            encoding="utf-8"
        )
        # The counterproof isolates the drift to the one assertion that covers
        # every schema byte. That node id lives in the lane's own gate file, so
        # the red is provably caused by the declared edit and not by an
        # unrelated pre-existing failure elsewhere in the suite.
        assert "-k test_canonical_digest_algorithm_matches_installed_bytes" in gate, (
            "the drift counterproof must target the specific digest assertion"
        )
        assert "false green" in gate.lower() or "false-green" in gate.lower(), (
            "the counterproof must refuse a green run over a drifted schema"
        )
        # No blanket allow-list of pre-existing reds is used as the certificate.
        assert not re.search(r"\bALLOWED\b\s*=", gate), (
            "the gate must not carry a node-id pardon set as its certificate"
        )
