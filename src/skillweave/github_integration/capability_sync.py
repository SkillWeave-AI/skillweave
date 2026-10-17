"""Capacium capability manifest synchronization helpers.

The runtime product and root distribution bundle follow the version declared in
``pyproject.toml``.  Member skills are independent artifacts: their manifest
versions may lag a packaging-only bundle release, while the corresponding root
bundle pins must continue to match those member manifests exactly.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..installer import AGENT_CONFIG

DEFAULT_FRAMEWORKS = [target.name for target in AGENT_CONFIG]

DEFAULT_LICENSE = "Apache-2.0"
DEFAULT_AUTHOR = "SkillWeave Team"
# The published Capacium identity. The project migrated from ``LangeVC`` to
# ``SkillWeave-AI`` (see tests/unit/test_1510_publish_manifest.py); ``LangeVC``
# is retained only as the declared ``replaces`` / ``previous_identities``
# history. These defaults must name the *current* identity or the sync tool
# would rewrite every manifest back to the retired owner.
DEFAULT_OWNER = "SkillWeave-AI"
DEFAULT_REPOSITORY = "https://github.com/SkillWeave-AI/skillweave"
DEFAULT_HOMEPAGE = DEFAULT_REPOSITORY
DEFAULT_BUNDLE_DESCRIPTION = "A complete 7-phase AI-assisted development lifecycle ecosystem."
DEFAULT_BUNDLE_KEYWORDS = [
    "skills",
    "agents",
    "orchestration",
    "lifecycle",
    "prompt-chains",
    "ralph-loop",
    "release-chain",
]

ROOT_MANIFEST = "capability.yaml"
SKILL_GLOB = "skills/skillweave-*/capability.yaml"


@dataclass
class SyncIssue:
    path: str
    reason: str


class CapaciumManifestSync:
    def __init__(self, repo_root: str | None = None):
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def get_project_version(self) -> str:
        pyproject = self.repo_root / "pyproject.toml"
        if not pyproject.exists():
            raise FileNotFoundError(f"Missing pyproject.toml in {self.repo_root}")

        match = re.search(
            r'^version\s*=\s*["\']([^"\']+)["\']',
            pyproject.read_text(),
            re.MULTILINE,
        )
        if not match:
            raise ValueError("Could not read project version from pyproject.toml")
        return match.group(1)

    def skill_manifest_paths(self) -> list[Path]:
        return sorted(self.repo_root.glob(SKILL_GLOB))

    def manifest_paths(self) -> list[Path]:
        return [self.repo_root / ROOT_MANIFEST, *self.skill_manifest_paths()]

    def load_manifest(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Manifest {path} does not contain a YAML object")
        return data

    def build_bundle_capabilities(self) -> list[dict[str, str]]:
        capabilities = []
        for path in self.skill_manifest_paths():
            manifest = self.load_manifest(path)
            name = manifest.get("name") or path.parent.name
            member_version = manifest.get("version")
            if not member_version:
                raise ValueError(f"Missing version in member manifest {path}")
            capabilities.append(
                {
                    "name": name,
                    "source": f"./skills/{path.parent.name}",
                    "version": str(member_version),
                }
            )
        return capabilities

    def normalize_manifest(self, path: Path, version: str) -> dict[str, Any]:
        current = self.load_manifest(path)
        extras = {
            key: value
            for key, value in current.items()
            if key
            not in {
                "kind",
                "name",
                "version",
                "description",
                "author",
                "license",
                "owner",
                "repository",
                "homepage",
                "frameworks",
                "keywords",
                "capabilities",
            }
        }

        if path == self.repo_root / ROOT_MANIFEST:
            normalized: dict[str, Any] = {
                "kind": "bundle",
                "name": "skillweave",
                "version": version,
                "description": DEFAULT_BUNDLE_DESCRIPTION,
                "author": DEFAULT_AUTHOR,
                "license": DEFAULT_LICENSE,
                "owner": DEFAULT_OWNER,
                "repository": DEFAULT_REPOSITORY,
                "homepage": DEFAULT_HOMEPAGE,
                "frameworks": list(DEFAULT_FRAMEWORKS),
                "keywords": list(DEFAULT_BUNDLE_KEYWORDS),
                "capabilities": self.build_bundle_capabilities(),
            }
        else:
            normalized = {
                "kind": "skill",
                "name": current.get("name", path.parent.name),
                # A member capability owns its own version.  A product/bundle
                # bump must not silently turn every member into a new release.
                "version": current.get("version", version),
                "description": current.get("description", ""),
                "author": DEFAULT_AUTHOR,
                "license": DEFAULT_LICENSE,
                "owner": DEFAULT_OWNER,
                "repository": current.get("repository", DEFAULT_REPOSITORY),
                "homepage": current.get("homepage", DEFAULT_HOMEPAGE),
                "frameworks": list(DEFAULT_FRAMEWORKS),
            }
            if current.get("keywords"):
                normalized["keywords"] = current["keywords"]

        normalized.update(extras)
        return normalized

    def manifest_to_yaml(self, manifest: dict[str, Any]) -> str:
        return yaml.safe_dump(
            manifest,
            sort_keys=False,
            allow_unicode=True,
            width=1000,
        )

    def sync(self, write: bool) -> list[SyncIssue]:
        version = self.get_project_version()
        issues: list[SyncIssue] = []

        for path in self.manifest_paths():
            if path != self.repo_root / ROOT_MANIFEST and not path.exists():
                issues.append(SyncIssue(str(path.relative_to(self.repo_root)), "missing manifest"))
                continue

            expected = self.normalize_manifest(path, version)
            current = self.load_manifest(path)

            if current != expected:
                issues.append(SyncIssue(str(path.relative_to(self.repo_root)), "manifest out of sync"))
                if write:
                    path.write_text(self.manifest_to_yaml(expected))

        return issues

    def check(self) -> list[SyncIssue]:
        return self.sync(write=False)

    def write(self) -> list[SyncIssue]:
        return self.sync(write=True)


def _format_issues(issues: list[SyncIssue]) -> str:
    return "\n".join(f"- {issue.path}: {issue.reason}" for issue in issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync SkillWeave Capacium manifests")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check manifest sync only; do not write changes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print mismatches as JSON",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (defaults to current working directory)",
    )
    args = parser.parse_args(argv)

    syncer = CapaciumManifestSync(repo_root=args.repo_root)
    issues = syncer.check() if args.check else syncer.write()

    if args.json:
        print(json.dumps([issue.__dict__ for issue in issues], indent=2))
    elif issues:
        verb = "Need sync" if args.check else "Updated"
        print(f"{verb} {len(issues)} manifest(s):")
        print(_format_issues(issues))
    else:
        print("All Capacium manifests are in sync.")

    return 1 if args.check and issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
