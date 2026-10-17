"""Release readiness gate module.

Integrates with Initiative 03's readiness assessment to provide
a GitHub Actions-compatible release gate. Checks version bump,
changelog, tests, required files, and WIP markers.
"""

import re
import os
import sys
import json
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional

from .capability_sync import CapaciumManifestSync
from skillweave.gates import integration_gate_result
from skillweave.release import environment_attestation


@dataclass
class GateCheck:
    id: str
    name: str
    passed: bool
    detail: str = ""
    required: bool = True


@dataclass
class ReleaseGateResult:
    timestamp: str = ""
    checks: list[GateCheck] = field(default_factory=list)
    all_required_passed: bool = False
    can_release: bool = False
    errors: list[str] = field(default_factory=list)
    candidate_sha: str = ""
    base_sha: str = ""
    environment_attestation: dict = field(default_factory=dict)
    integration_gate_result: dict = field(default_factory=dict)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def required_failed(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed and c.required]


_WIP_PATTERNS = [
    r"\bWIP\b",
    r"\bTODO\b",
    r"\bFIXME\b",
    r"\bHACK\b",
    r"\bdraft\b",
    r"\bwork in progress\b",
    r"\bnot\s*ready\b",
]


class ReleaseReadinessGate:
    def __init__(self, repo_root: str | None = None):
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def check_version_bump(self, current_version: str, latest_tag: Optional[str] = None) -> GateCheck:
        if not latest_tag:
            return GateCheck(
                id="version-bump",
                name="Version bumped",
                passed=True,
                detail=f"Current version: {current_version} (no previous tag found)",
                required=True,
            )

        current = current_version.lstrip("v")
        latest = latest_tag.lstrip("v")

        if current != latest:
            return GateCheck(
                id="version-bump",
                name="Version bumped",
                passed=True,
                detail=f"Version changed: {latest} → {current}",
                required=True,
            )

        return GateCheck(
            id="version-bump",
            name="Version bumped",
            passed=False,
            detail=f"Version unchanged: {current} (matches latest tag v{latest})",
            required=True,
        )

    def check_changelog(self, version: str) -> GateCheck:
        changelog = self.repo_root / "CHANGELOG.md"
        if not changelog.exists():
            return GateCheck(
                id="changelog-exists",
                name="CHANGELOG.md exists",
                passed=False,
                detail="CHANGELOG.md not found",
                required=True,
            )

        content = changelog.read_text()
        clean_ver = version.lstrip("v")
        escaped = re.escape(clean_ver)
        pattern = rf"##\s*(?:\[)?{escaped}(?:\])?"
        if re.search(pattern, content):
            return GateCheck(
                id="changelog-updated",
                name="CHANGELOG has version entry",
                passed=True,
                detail=f"Entry found for version {clean_ver}",
                required=True,
            )

        if re.search(r"##\s*\[\d+\.\d+\.\d+\]", content):
            return GateCheck(
                id="changelog-updated",
                name="CHANGELOG has version entry",
                passed=False,
                detail=f"No entry found for version {clean_ver} (other entries exist)",
                required=True,
            )

        return GateCheck(
            id="changelog-updated",
            name="CHANGELOG has version entry",
            passed=False,
            detail="No version entries found in CHANGELOG.md",
            required=True,
        )

    def check_tests(self) -> GateCheck:
        tests_dir = self.repo_root / "tests"
        if not tests_dir.exists():
            return GateCheck(
                id="tests-exist",
                name="Test suite exists",
                passed=False,
                detail="tests/ directory not found",
                required=True,
            )

        test_files = list(tests_dir.rglob("test_*.py"))
        if not test_files:
            return GateCheck(
                id="tests-exist",
                name="Test suite exists",
                passed=False,
                detail="No test_*.py files found in tests/",
                required=True,
            )

        return GateCheck(
            id="tests-exist",
            name="Test suite exists",
            passed=True,
            detail=f"{len(test_files)} test files found",
            required=True,
        )

    def check_required_files(self, required_files: list[str] | None = None) -> list[GateCheck]:
        checks = []
        files = required_files or [
            "README.md",
            "pyproject.toml",
            "CHANGELOG.md",
            "LICENSE",
        ]
        for filename in files:
            fpath = self.repo_root / filename
            exists = fpath.exists()
            checks.append(GateCheck(
                id=f"file-{filename.lower().replace('.', '-')}",
                name=f"Required file: {filename}",
                passed=exists,
                detail=f"{'Found' if exists else 'Not found'}: {filename}",
                required=True,
            ))
        return checks

    def check_wip_markers(self, paths: list[str] | None = None) -> GateCheck:
        wip_found = []

        if paths:
            search_paths = [self.repo_root / p for p in paths]
        else:
            search_paths = [self.repo_root / path for path in
                            ["src", "README.md", "CHANGELOG.md", "AGENTS.md"]
                            if (self.repo_root / path).exists()]

        combined_pattern = "|".join(_WIP_PATTERNS)

        for sp in search_paths:
            if sp.is_file():
                files_to_check = [sp]
            elif sp.is_dir():
                files_to_check = list(sp.rglob("*.py")) + list(sp.rglob("*.md"))
            else:
                continue

            for f in files_to_check:
                try:
                    content = f.read_text()
                    for match in re.finditer(combined_pattern, content, re.IGNORECASE):
                        for line_no, line in enumerate(content.split("\n"), 1):
                            if match.group() in line:
                                wip_found.append(f"{f.relative_to(self.repo_root)}:{line_no}")
                                break
                except Exception:
                    continue

        if wip_found:
            return GateCheck(
                id="no-wip-markers",
                name="No WIP/draft markers",
                passed=False,
                detail=f"Found {len(wip_found)} WIP marker(s): {', '.join(wip_found[:10])}",
                required=False,
            )

        return GateCheck(
            id="no-wip-markers",
            name="No WIP/draft markers",
            passed=True,
            detail="No WIP markers detected",
            required=False,
        )

    def check_pyproject_toml(self) -> GateCheck:
        pyproject = self.repo_root / "pyproject.toml"
        if not pyproject.exists():
            return GateCheck(
                id="pyproject-exists",
                name="pyproject.toml exists",
                passed=False,
                detail="pyproject.toml not found",
                required=True,
            )
        return GateCheck(
            id="pyproject-exists",
            name="pyproject.toml exists",
            passed=True,
            detail="pyproject.toml found",
            required=True,
        )

    def check_release_version_matches_capabilities(self, current_version: str) -> GateCheck:
        manifest_paths = [
            self.repo_root / "capability.yaml",
            *sorted((self.repo_root / "skills").glob("skillweave-*/capability.yaml")),
        ]
        missing = [path for path in manifest_paths if not path.exists()]
        if missing:
            preview = ", ".join(str(path.relative_to(self.repo_root)) for path in missing[:5])
            return GateCheck(
                id="release-version-capabilities-match",
                name="Release version matches capability manifests",
                passed=False,
                detail=f"Missing capability manifest(s): {preview}",
                required=True,
            )

        release_version = current_version.lstrip("v")
        mismatches = []
        try:
            for path in manifest_paths:
                manifest = yaml.safe_load(path.read_text()) or {}
                manifest_version = str(manifest.get("version", "")).lstrip("v")
                if manifest_version != release_version:
                    rel_path = path.relative_to(self.repo_root)
                    mismatches.append(f"{rel_path}={manifest_version or 'missing'}")
        except Exception as exc:
            return GateCheck(
                id="release-version-capabilities-match",
                name="Release version matches capability manifests",
                passed=False,
                detail=f"Could not read capability manifest: {exc}",
                required=True,
            )

        if mismatches:
            preview = ", ".join(mismatches[:5])
            remainder = "" if len(mismatches) <= 5 else f" (+{len(mismatches) - 5} more)"
            return GateCheck(
                id="release-version-capabilities-match",
                name="Release version matches capability manifests",
                passed=False,
                detail=f"Release version {release_version} differs from: {preview}{remainder}",
                required=True,
            )

        return GateCheck(
            id="release-version-capabilities-match",
            name="Release version matches capability manifests",
            passed=True,
            detail=f"{len(manifest_paths)} capability manifest(s) match release version {release_version}",
            required=True,
        )

    def check_capacium_manifests(self) -> GateCheck:
        try:
            syncer = CapaciumManifestSync(repo_root=str(self.repo_root))
            issues = syncer.check()
        except Exception as exc:
            return GateCheck(
                id="capacium-manifests",
                name="Capacium manifests synced",
                passed=False,
                detail=f"Could not validate Capacium manifests: {exc}",
                required=True,
            )

        if issues:
            preview = ", ".join(issue.path for issue in issues[:5])
            remainder = "" if len(issues) <= 5 else f" (+{len(issues) - 5} more)"
            return GateCheck(
                id="capacium-manifests",
                name="Capacium manifests synced",
                passed=False,
                detail=f"{len(issues)} manifest(s) out of sync: {preview}{remainder}",
                required=True,
            )

        manifest_count = len(syncer.manifest_paths())
        return GateCheck(
            id="capacium-manifests",
            name="Capacium manifests synced",
            passed=True,
            detail=f"{manifest_count} manifest(s) match pyproject version and bundle layout",
            required=True,
        )

    def _gate_environments(
        self, declaration: environment_attestation.EnvironmentDeclaration
    ) -> list[str]:
        """The one declared interpreter the gate attests: the session's own.

        ``SKILLWEAVE_ENV_ATTEST_PYTHONS`` (comma-separated) widens this when a
        caller wants a receipt per declared interpreter, so the criterion-1
        surface stays available without making every gate run build three
        environments.
        """
        requested = os.environ.get("SKILLWEAVE_ENV_ATTEST_PYTHONS", "")
        wanted = [p.strip() for p in requested.split(",") if p.strip()]
        if not wanted:
            session = "%d.%d" % tuple(sys.version_info[:2])
            wanted = [session] if session in declaration.pythons else list(declaration.pythons)
        undeclared = [p for p in wanted if p not in declaration.pythons]
        if undeclared:
            raise environment_attestation.EnvironmentDeclarationError(
                f"SKILLWEAVE_ENV_ATTEST_PYTHONS names undeclared interpreter(s) "
                f"{undeclared}; the declaration declares {list(declaration.pythons)}"
            )
        return wanted

    def check_test_environment(
        self,
        *,
        declaration: environment_attestation.EnvironmentDeclaration | None = None,
        build: Callable[[str], Path] | None = None,
        available: Mapping[str, bool] | None = None,
        session_python: Path | str | None = None,
        sdk_dir: Path | str | None = None,
        receipt: Mapping[str, Any] | None = None,
        environments: Iterable[str] | None = None,
    ) -> tuple[GateCheck, dict, dict[str, dict]]:
        """Run the SW-1511A-ENV-001 seam of the gate: build, guard, receipt.

        A gate run (a) *builds* the declared environment for each declared
        interpreter -- or for the one declared interpreter matching the
        session -- (b) *refuses* a session whose declared collection
        dependencies are missing (the selective-ignore cause, named), and
        (c) *writes* the ``execution-environment-attestation/v1`` receipt with
        the eight criterion-3 fields.

        The heavy builder and the session probe are injection seams: ``build``
        and ``available``/``session_python`` let a caller (or a test) supply
        fakes, so nothing here reaches the network, an index or a credential.
        Returns ``(check, receipt, receipts_by_python)``.
        """
        try:
            decl = declaration or environment_attestation.load_declaration()
        except environment_attestation.EnvironmentDeclarationError as exc:
            return (
                GateCheck(
                    id="test-environment-attestation",
                    name="Declared test environment attested",
                    passed=False,
                    detail=f"cannot load the declared test-environment input: {exc}",
                    required=True,
                ),
                {},
                {},
            )

        # Build *one* declared environment, not three: the gate's receipt is
        # the evidence for the environment the unit suite actually ran in. A
        # caller may widen ``environments`` to attest more than the session's.
        targets = list(environments) if environments is not None else self._gate_environments(decl)
        env_root = self.repo_root / ".skillweave" / "release" / "test-environments"

        def default_build(minor: str) -> Path:
            refusals: list[str] = []
            venv_dir = environment_attestation.build_environment(
                minor, decl, env_root, sdk_dir=sdk_dir, refusals=refusals
            )
            if refusals:
                raise environment_attestation.EnvironmentBuildError(
                    "incomplete environment for py"
                    f"{minor}; refused dependencies: " + "; ".join(refusals)
                )
            return venv_dir

        build_fn = build if build is not None else default_build

        receipts: dict[str, dict] = {}
        for minor in targets:
            try:
                venv_dir = Path(build_fn(minor))
            except environment_attestation.EnvironmentBuildError as exc:
                return (
                    GateCheck(
                        id="test-environment-attestation",
                        name="Declared test environment attested",
                        passed=False,
                        detail=f"py{minor}: {exc}",
                        required=True,
                    ),
                    {},
                    receipts,
                )
            try:
                env_receipt = environment_attestation.build_attestation(
                    venv_dir, decl, sdk_dir=sdk_dir
                ).to_dict()
            except (
                environment_attestation.EnvironmentBuildError,
                environment_attestation.AttestationError,
            ) as exc:
                return (
                    GateCheck(
                        id="test-environment-attestation",
                        name="Declared test environment attested",
                        passed=False,
                        detail=f"py{minor}: no attestation: {exc}",
                        required=True,
                    ),
                    {},
                    receipts,
                )
            receipts[minor] = env_receipt

        # A built environment that attests it did not provide a declared
        # dependency is incomplete, never evidence: the gate refuses it here as
        # well as in the builder, so a partial environment cannot pass the gate
        # when a caller supplies its own builder.
        unprovided = sorted(
            {name for r in receipts.values() for name in r.get("missing_dependencies", ())}
        )
        if unprovided:
            return (
                GateCheck(
                    id="test-environment-attestation",
                    name="Declared test environment attested",
                    passed=False,
                    detail=(
                        "selective-ignore cause: the built environment(s) "
                        f"{sorted(receipts)} attest they did not provide "
                        f"{unprovided}; the full unit suite would collect with "
                        "those dependencies skipped"
                    ),
                    required=True,
                ),
                {},
                receipts,
            )

        primary = "%d.%d" % tuple(sys.version_info[:2])
        if primary not in receipts:
            primary = targets[0]
        session_receipt = receipts[primary]
        try:
            session_attestation = environment_attestation.guard_session(
                decl,
                session_python=session_python,
                sdk_dir=sdk_dir,
                available=available,
                receipt=receipt,
            ).to_dict()
        except (
            environment_attestation.SessionGuardError,
            environment_attestation.AttestationError,
        ) as exc:
            return (
                GateCheck(
                    id="test-environment-attestation",
                    name="Declared test environment attested",
                    passed=False,
                    detail=f"refusing the unit-suite session: {exc}",
                    required=True,
                ),
                {},
                receipts,
            )

        # The attestation must be complete: a receipt that omits a declared
        # dependency is an incomplete environment, never a pass.
        missing = list(session_attestation.get("missing_dependencies", ()))
        if missing:
            return (
                GateCheck(
                    id="test-environment-attestation",
                    name="Declared test environment attested",
                    passed=False,
                    detail=(
                        "selective-ignore cause: the session attestation omits "
                        f"{missing}; the full unit suite would collect with those "
                        "dependencies skipped"
                    ),
                    required=True,
                ),
                {},
                receipts,
            )

        complete = "complete" if all(
            r.get("complete") for r in receipts.values()
        ) else "incomplete"
        check = GateCheck(
            id="test-environment-attestation",
            name="Declared test environment attested",
            passed=True,
            detail=(
                f"{len(receipts)} declared environment(s) built ({complete}); "
                f"session py{session_attestation['python_short']} guarded; SDK "
                f"{session_attestation['sdk_version']} "
                f"({session_attestation['sdk_install_type']}); lock digest "
                f"{session_attestation['dependency_lock_digest'][:12]}"
            ),
            required=True,
        )
        return check, session_attestation, receipts

    def check_integration_gate(
        self,
        *,
        candidate_sha: str = "",
        base_sha: str = "",
        expected_candidate_sha: str | None = None,
        base_root: Path | str | None = None,
        diff_paths: Iterable[str] = (),
        prior_surfaces: Iterable[str] = (),
        source_suite: Any = None,
        installed_wheel: Any = None,
        environment_digest: str = "",
        tasks: Iterable[str] = (),
        review_probe: Optional[Callable[[], None]] = None,
        artifacts_root: Path | str | None = None,
    ) -> tuple[GateCheck, dict]:
        """The SW-1511A-INTEGRATION-001 cumulative gate seam.

        It (a) *derives* the cumulative test manifest from the frozen base, the
        candidate diff and the prior accepted surfaces, (b) *binds* the result
        to the exact candidate -- a complete-suite result for another SHA is
        refused, so final-head integration cannot proceed without an attested
        complete-suite result for this candidate, and (c) *proves* review was
        read-only: when ``review_probe`` is supplied, the candidate tree (and
        the evidence artifacts) are digested around it and a change is a
        refusal.

        Every observation is an injection seam (``source_suite``,
        ``installed_wheel``, ``environment_digest``); nothing here launches a
        suite, reaches the network, names a provider or reads a credential.
        Red-proof and destructive checks belong in
        :func:`skillweave.gates.integration_gate_result.disposable_worktree`,
        never on the candidate. Returns ``(check, result_dict)``.
        """
        refusals: list[str] = []
        candidate = str(candidate_sha).strip().lower()
        base = str(base_sha).strip().lower()

        for label, value in (("candidate_sha", candidate), ("base_sha", base)):
            if not value or not re.fullmatch(r"[0-9a-f]{40}", value):
                refusals.append(f"{label} is not a full 40-hex SHA: {value!r}")

        # Source-binding: the record must name the *exact* candidate the gate
        # is authorizing. A result for a different SHA never authorizes this one.
        if expected_candidate_sha is not None:
            expected = str(expected_candidate_sha).strip().lower()
            if expected != candidate:
                refusals.append(
                    f"source-binding: the result names candidate {candidate or '<absent>'} "
                    f"but final-head integration requires the exact candidate {expected}"
                )

        manifest = integration_gate_result.derive_test_manifest(
            candidate_root=self.repo_root,
            base_root=base_root,
            diff_paths=diff_paths,
            prior_surfaces=prior_surfaces,
        )
        if not manifest:
            refusals.append(
                "the cumulative manifest is empty: no test file is bound to the "
                "base, the candidate diff and the prior accepted surfaces"
            )

        def _totals(value: Any, label: str):
            if value is None:
                refusals.append(f"no attested {label} result was supplied")
                return integration_gate_result.SuiteTotals()
            if isinstance(value, integration_gate_result.SuiteTotals):
                return value
            try:
                return integration_gate_result.SuiteTotals.from_mapping(value)
            except integration_gate_result.IntegrationGateError as exc:
                refusals.append(f"{label}: {exc}")
                return integration_gate_result.SuiteTotals()

        source = _totals(source_suite, "source-suite")
        wheel = _totals(installed_wheel, "installed-wheel")

        env_digest = str(environment_digest).strip()
        if not env_digest:
            refusals.append(
                "no environment digest: the result cannot bind to an attested "
                "execution environment"
            )

        # Read-only review: digest the candidate (and its evidence artifacts)
        # around the review action. Any change is a refusal, never a silent pass.
        review_read_only = False
        if review_probe is not None:
            before = integration_gate_result.tree_digest(self.repo_root)
            artifacts_before = (
                integration_gate_result.tree_digest(artifacts_root)
                if artifacts_root is not None
                else None
            )
            review_probe()
            after = integration_gate_result.tree_digest(self.repo_root)
            artifacts_after = (
                integration_gate_result.tree_digest(artifacts_root)
                if artifacts_root is not None
                else None
            )
            review_read_only = before == after and artifacts_before == artifacts_after
            if not review_read_only:
                refusals.append(
                    "review mutated the candidate or its evidence artifacts "
                    "(tree or artifact digest changed across review)"
                )

        verdict = (
            integration_gate_result.VERDICT_REFUSED
            if refusals
            else integration_gate_result.VERDICT_PASS
        )
        result = integration_gate_result.IntegrationGateResult(
            candidate_sha=candidate or "0" * 40,
            base_sha=base or "0" * 40,
            manifest=manifest,
            source_suite=source,
            installed_wheel=wheel,
            environment_digest=env_digest,
            verdict=verdict,
            tasks=tuple(str(t) for t in tasks),
            review_read_only=review_read_only,
            refusals=tuple(refusals),
        )

        check = GateCheck(
            id="integration-gate-result",
            name="Cumulative integration gate",
            passed=not refusals,
            detail=(
                (
                    f"{len(manifest)} cumulative test file(s); source "
                    f"{source.total} ({source.skipped} skipped), wheel "
                    f"{wheel.total} ({wheel.skipped} skipped); env "
                    f"{env_digest[:12]}; candidate {candidate[:12]}"
                )
                if not refusals
                else "refused: " + "; ".join(refusals)
            ),
            required=True,
        )
        return check, result.to_dict()

    def evaluate(
        self,
        current_version: str,
        latest_tag: Optional[str] = None,
        required_files: list[str] | None = None,
        wip_scan_paths: list[str] | None = None,
        *,
        environments: Iterable[str] | None = None,
        build: Callable[[str], Path] | None = None,
        available: Mapping[str, bool] | None = None,
        session_python: Path | str | None = None,
        sdk_dir: Path | str | None = None,
        receipt: Mapping[str, Any] | None = None,
        integration: Mapping[str, Any] | None = None,
    ) -> ReleaseGateResult:
        result = ReleaseGateResult(
            timestamp=datetime.utcnow().isoformat() + "Z",
        )

        result.checks.append(self.check_version_bump(current_version, latest_tag))
        result.checks.append(self.check_changelog(current_version))
        result.checks.append(self.check_tests())
        result.checks.append(self.check_pyproject_toml())
        result.checks.append(self.check_release_version_matches_capabilities(current_version))
        result.checks.append(self.check_capacium_manifests())
        result.checks.extend(self.check_required_files(required_files))
        result.checks.append(self.check_wip_markers(wip_scan_paths))

        if environments is not None or build is not None or available is not None:
            env_check, env_receipt, _all_receipts = self.check_test_environment(
                build=build,
                available=available,
                session_python=session_python,
                sdk_dir=sdk_dir,
                receipt=receipt,
                environments=environments,
            )
            result.checks.append(env_check)
            result.environment_attestation = env_receipt

        if integration is not None:
            # The cumulative integration gate (SW-1511A-INTEGRATION-001) is an
            # additive seam. ``candidate``/``base`` default to the assessed
            # result so the gate binds to the exact candidate it is authorizing.
            integration_kwargs = dict(integration)
            integration_kwargs.setdefault("candidate_sha", result.candidate_sha)
            integration_kwargs.setdefault("base_sha", result.base_sha)
            int_check, int_result = self.check_integration_gate(**integration_kwargs)
            result.checks.append(int_check)
            result.integration_gate_result = int_result

        result.all_required_passed = len(result.required_failed) == 0
        result.can_release = result.all_required_passed
        return result

    def generate_markdown(self, result: ReleaseGateResult) -> str:
        lines = [
            "# Release Readiness Gate Report",
            "",
            f"_Generated: {result.timestamp}_",
            f"_Can release: {'**YES**' if result.can_release else '**NO**'}_",
            f"_Passed: {result.passed_count}/{len(result.checks)}_",
            "",
            "## Check Results",
            "",
        ]

        for check in result.checks:
            status = "✅" if check.passed else "❌"
            required = " (required)" if check.required else ""
            lines.append(f"- {status} **{check.name}**{required}")
            lines.append(f"  - {check.detail}")
            lines.append("")

        failed = result.required_failed
        if failed:
            lines.append("## Failed Required Checks")
            for check in failed:
                lines.append(f"- ❌ **{check.name}**: {check.detail}")
            lines.append("")

        lines.append("---")
        lines.append("_Auto-generated by SkillWeave Release Readiness Gate_")
        return "\n".join(lines)

    def generate_json(self, result: ReleaseGateResult) -> str:
        return json.dumps({
            "timestamp": result.timestamp,
            "can_release": result.can_release,
            "all_required_passed": result.all_required_passed,
            "passed_count": result.passed_count,
            "total_checks": len(result.checks),
            "candidate_sha": result.candidate_sha,
            "base_sha": result.base_sha,
            "checks": [
                {
                    "id": c.id,
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "required": c.required,
                }
                for c in result.checks
            ],
            "errors": result.errors,
            "environment_attestation": result.environment_attestation,
            "integration_gate_result": result.integration_gate_result,
        }, indent=2)


def run_cli(
    argv: Optional[list[str]] = None,
    *,
    build: Optional[Callable[[str], Path]] = None,
    available: Optional[Mapping[str, bool]] = None,
    environments: Optional[Iterable[str]] = None,
    session_python: Optional[Path | str] = None,
    receipt: Optional[Mapping[str, Any]] = None,
    integration: Optional[Mapping[str, Any]] = None,
) -> int:
    """CLI entry for GitHub Actions: reads env vars, runs gate, writes outputs.

    Also runs the SW-1511A-ENV-001 environment seam: the gate builds the
    declared environment (``profiles/test-environments.v1.toml``) for the
    session interpreter, refuses the run if that session is missing a declared
    collection dependency, and records the
    ``execution-environment-attestation/v1`` receipt -- all eight criterion-3
    fields -- in ``release-gate-data.json``. A missing declared input raises
    :class:`EnvironmentDeclarationError`, never a green report with no
    environment evidence; ``SKILLWEAVE_ATTEST_ENV=0`` is the explicit opt-out.

    ``build``/``available``/``environments``/``session_python``/``receipt`` are
    the injection seams for a caller (or test) that supplies fakes; the real
    run builds and probes for real.
    """
    version = os.environ.get("RELEASE_VERSION", "unknown")
    latest_tag = os.environ.get("RELEASE_LATEST_TAG") or None
    gate = ReleaseReadinessGate()

    attest_env = {
        "environments": environments,
        "build": build,
        "available": available,
        "session_python": session_python,
        "receipt": receipt,
    }
    if "SKILLWEAVE_ATTEST_ENV" not in os.environ:
        # Native runs always attest the declared environment. The declared
        # input is required: a checkout that dropped it is refused loudly
        # rather than reported green with no environment evidence.
        declaration = environment_attestation.load_declaration()
        if environments is None:
            attest_env["environments"] = gate._gate_environments(declaration)
    elif os.environ["SKILLWEAVE_ATTEST_ENV"] != "0":
        try:
            declaration = environment_attestation.load_declaration(
                require_supported_pythons=False
            )
        except environment_attestation.EnvironmentDeclarationError:
            declaration = None
        if declaration is not None and environments is None:
            attest_env["environments"] = gate._gate_environments(declaration)

    # The candidate/base identity the gate binds the integration result to. In
    # Action runs these are provided by GitHub; locally they may be absent, in
    # which case keep them empty rather than fabricate a value.
    candidate_sha = os.environ.get("GITHUB_SHA", os.environ.get("CANDIDATE_SHA", ""))
    base_sha = os.environ.get("GITHUB_BASE_SHA", "") or candidate_sha
    if integration is not None:
        integration = dict(integration)
        integration.setdefault("candidate_sha", candidate_sha)
        integration.setdefault("base_sha", base_sha)

    result = gate.evaluate(
        current_version=version,
        latest_tag=latest_tag,
        wip_scan_paths=["src", "README.md", "CHANGELOG.md"],
        integration=integration,
        **attest_env,
    )
    # The gate always records which commit it inspected so a rework brief can
    # later point at the exact candidate/base pair.
    result.candidate_sha = candidate_sha
    result.base_sha = base_sha

    markdown = gate.generate_markdown(result)
    with open("release-gate-report.md", "w") as f:
        f.write(markdown)

    json_out = gate.generate_json(result)
    with open("release-gate-data.json", "w") as f:
        f.write(json_out)

    data = json.loads(json_out)
    print(f"can_release={'true' if result.can_release else 'false'}")
    print(f"passed_count={data['passed_count']}")
    print(f"total_checks={data['total_checks']}")

    for check in result.checks:
        if not check.passed:
            level = "error" if check.required else "warning"
            print(f"::{level} title=ReleaseGate::{check.name}: {check.detail}")

    return 0
