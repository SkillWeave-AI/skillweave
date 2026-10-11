"""Grounding scanner — bounded, deterministic, read-only scans.

The scanner walks a repository root and produces a ``ScanResult`` that can be
turned into a ``GroundingManifest``.  It never modifies the filesystem, never
follows symlinks outside the root, and stops after a configurable depth limit.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 — used for read-only git queries, not dynamic
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .manifest import (
    GapKind,
    GroundingGap,
    GroundingManifest,
    LanguageScan,
    NeighborRepo,
    RequestedSymbol,
    RevisionPin,
)


@dataclass(frozen=True)
class ScanLimit:
    """Bounding limits for a grounding scan."""

    max_depth: int = 4
    max_files_per_language: int = 200
    max_manifests: int = 20
    max_roots: int = 20
    max_commands: int = 20
    max_symbols: int = 50
    max_neighbors: int = 10
    max_total_files: int = 2000


@dataclass
class ScanResult:
    """Result of a bounded scan, ready to build a GroundingManifest."""

    target: str = ""
    full_revision: str = ""
    requesting_model: str = ""
    resolving_model: str = ""
    answering_model: str = ""

    languages: Dict[str, List[str]] = field(default_factory=dict)
    manifests: List[str] = field(default_factory=list)
    roots: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    requested_symbols: List[RequestedSymbol] = field(default_factory=list)
    neighbor_repos: List[NeighborRepo] = field(default_factory=list)
    gaps: List[GroundingGap] = field(default_factory=list)

    _manifest: Optional[GroundingManifest] = None

    def build_manifest(self) -> GroundingManifest:
        """Build and cache a GroundingManifest from this scan result."""
        if self._manifest is not None:
            return self._manifest

        lang_scans = [
            LanguageScan(language=lang, files=sorted(paths))
            for lang, paths in sorted(self.languages.items())
        ]

        self._manifest = GroundingManifest(
            schema_version="1.0",
            target=self.target,
            full_revision=self.full_revision,
            requesting_model=self.requesting_model,
            resolving_model=self.resolving_model,
            answering_model=self.answering_model,
            languages=lang_scans,
            manifests=sorted(self.manifests),
            roots=sorted(self.roots),
            commands=sorted(self.commands),
            requested_symbols=list(self.requested_symbols),
            neighbor_repos=list(self.neighbor_repos),
            gaps=list(self.gaps),
        ).with_digest()

        return self._manifest


class GroundingScanner:
    """Bounded, read-only repository scanner.

    Usage::

        scanner = GroundingScanner(repo_root, limits=ScanLimit())
        result = scanner.scan()
        manifest = result.build_manifest()
    """

    # File extensions mapped to language names.  Additive — extend via subclass
    # or instance mutation if needed.
    LANG_EXTENSIONS: Dict[str, str] = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".kt": "Kotlin",
        ".rb": "Ruby",
        ".swift": "Swift",
        ".c": "C",
        ".h": "C",
        ".cpp": "C++",
        ".hpp": "C++",
        ".cs": "C#",
        ".sh": "Shell",
        ".bash": "Shell",
        ".zsh": "Shell",
        ".yaml": "YAML",
        ".yml": "YAML",
        ".json": "JSON",
        ".md": "Markdown",
        ".toml": "TOML",
    }

    # Known manifest file names
    KNOWN_MANIFESTS: Set[str] = {
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "Pipfile",
        "setup.py",
        "setup.cfg",
        "build.gradle",
        "pom.xml",
        ".ops.yaml",
        ".version.yaml",
        "capability.yaml",
    }

    def __init__(
        self,
        repo_root: Path,
        limits: Optional[ScanLimit] = None,
    ) -> None:
        self._root = repo_root.resolve()
        self._limits = limits or ScanLimit()
        self._total_files = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> ScanResult:
        """Run a bounded, read-only scan of the repository root."""
        result = ScanResult()
        result.target = str(self._root)
        result.full_revision = self._git_revision()

        # Language scan
        self._scan_languages(result)

        # Manifest scan
        self._scan_manifests(result)

        # Root directories
        result.roots = self._list_roots()

        return result

    def scan_with_gaps(
        self,
        requested_targets: Optional[List[str]] = None,
        requested_symbols: Optional[List[str]] = None,
        expected_revision: Optional[str] = None,
    ) -> ScanResult:
        """Run a full scan and produce typed blocking gaps."""
        result = self.scan()

        # Gap: missing targets
        if requested_targets:
            self._check_missing_targets(result, requested_targets)

        # Gap: missing symbols
        if requested_symbols:
            self._check_missing_symbols(result, requested_symbols)

        # Gap: changed revision
        if expected_revision and result.full_revision != expected_revision:
            result.gaps.append(
                GroundingGap(
                    kind=GapKind.CHANGED_REVISION,
                    description=(
                        f"Expected revision {expected_revision}, "
                        f"got {result.full_revision}"
                    ),
                    detail=f"expected={expected_revision} actual={result.full_revision}",
                )
            )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _git_revision(self) -> str:
        """Return the full SHA of HEAD (read-only)."""
        try:
            return subprocess.check_output(  # noqa: S603, S607
                ["git", "rev-parse", "HEAD"],
                cwd=self._root,
                stderr=subprocess.DEVNULL,
                timeout=30,
            ).decode().strip()
        except Exception:
            return ""

    def _scan_languages(self, result: ScanResult) -> None:
        """Walk the repository tree and classify files by language."""
        for dirpath_str, dirnames, filenames in os.walk(self._root):
            if self._total_files >= self._limits.max_total_files:
                break

            dirpath = Path(dirpath_str)
            depth = len(dirpath.relative_to(self._root).parts)

            # Bounded depth
            if depth > self._limits.max_depth:
                dirnames.clear()
                continue

            # Skip hidden directories (except .github, .forgejo)
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") or d in (".github", ".forgejo", ".skillweave")
            ]

            rel_dir = dirpath.relative_to(self._root)

            for filename in filenames:
                if self._total_files >= self._limits.max_total_files:
                    break

                ext = Path(filename).suffix.lower()
                lang = self.LANG_EXTENSIONS.get(ext)
                if lang is None:
                    continue

                rel_path = str(rel_dir / filename) if str(rel_dir) != "." else filename
                if lang not in result.languages:
                    result.languages[lang] = []

                if (
                    len(result.languages[lang])
                    < self._limits.max_files_per_language
                ):
                    result.languages[lang].append(rel_path)
                    self._total_files += 1

    def _scan_manifests(self, result: ScanResult) -> None:
        """Find known manifest files in the root."""
        for name in sorted(self.KNOWN_MANIFESTS):
            if len(result.manifests) >= self._limits.max_manifests:
                break
            path = self._root / name
            if path.is_file():
                result.manifests.append(name)

        # Also check one level deep
        for child in sorted(self._root.iterdir()):
            if len(result.manifests) >= self._limits.max_manifests:
                break
            if child.is_dir() and not child.name.startswith("."):
                for name in sorted(self.KNOWN_MANIFESTS):
                    if len(result.manifests) >= self._limits.max_manifests:
                        break
                    path = child / name
                    if path.is_file():
                        result.manifests.append(
                            f"{child.name}/{name}"
                        )

    def _list_roots(self) -> List[str]:
        """List top-level directory names."""
        roots: List[str] = []
        for child in sorted(self._root.iterdir()):
            if len(roots) >= self._limits.max_roots:
                break
            if child.is_dir() and not child.name.startswith("."):
                roots.append(child.name)
        return roots

    def _check_missing_targets(
        self, result: ScanResult, targets: List[str]
    ) -> None:
        """Check that requested targets exist as paths relative to root."""
        for target in targets:
            path = self._root / target
            if not path.exists():
                result.gaps.append(
                    GroundingGap(
                        kind=GapKind.MISSING_TARGET,
                        description=f"Target path does not exist: {target}",
                        detail=f"resolved={path}",
                    )
                )

    def _check_missing_symbols(
        self, result: ScanResult, symbols: List[str]
    ) -> None:
        """Check that requested symbols are found in scanned files.

        Simple text-based search — does not parse AST.
        """
        scanned_files: List[str] = []
        for files in result.languages.values():
            scanned_files.extend(files)

        for symbol in symbols:
            found = False
            for rel_path in scanned_files:
                full_path = self._root / rel_path
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            if symbol in line:
                                result.requested_symbols.append(
                                    RequestedSymbol(
                                        symbol=symbol,
                                        file_path=rel_path,
                                        line=line_no,
                                    )
                                )
                                found = True
                                break
                except Exception:
                    continue
                if found:
                    break

            if not found:
                result.gaps.append(
                    GroundingGap(
                        kind=GapKind.MISSING_SYMBOL,
                        description=f"Symbol not found: {symbol}",
                        detail="",
                    )
                )
