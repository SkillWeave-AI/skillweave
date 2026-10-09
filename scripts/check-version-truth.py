#!/usr/bin/env python3
"""check-version-truth — release-facing version claims must match the tree.

Where ``check-manifest.py`` asks "does a bundle pin agree with its member file",
and ``version-sync`` asks "does every declared location agree with the source",
this tool asks a third, release-facing question:

    Does what the *public release surface* says about the version match the
    facts the tree and an installed artifact actually carry?

The four facts (see ``docs/version-truth.md``) are:

    candidate   pyproject.toml [project].version and the bundle capability.yaml
    released    the latest canonical ANNOTATED tag, by its PEELED commit
    installed   importlib.metadata.version("skillweave") in this interpreter
    historical  an old version, allowed only when not advertised as current

It checks, and fails closed on each:

  1. candidate            pyproject == bundle capability.yaml (the two
                          ``required: true`` surfaces of .version.yaml); member
                          pins / skill files are informational and may lag.
  2. release-facing badge the README version badge advertises the RELEASED tag.
  3. changelog            the CHANGELOG's top heading names the released version.
  4. installed identity   when a distribution is importable, its metadata equals
                          the candidate; absence is reported, never faked.
  5. skill inventory      the README skill count and every on-disk
                          ``skills/*/capability.yaml`` name are present.

Historical references are ALLOWED: the check reads only the *release-advertising
positions* (the badge, the "What's new in X" heading, the CHANGELOG top). An old
version mentioned elsewhere as history (e.g. "1.4.0 scope") is not a failure.
What is forbidden is advertising an old version as current, or advertising a
version AHEAD of the tag (e.g. an unreleased 1.5.10) as released.

Annotation-aware: the released version is established from the tag's PEELED
commit (``git rev-parse '<tag>^{}'``) plus the tag's canonical subject, never by
requiring the tag to equal the current branch HEAD.

Stdlib only. Usage:
    check-version-truth.py [--repo PATH]
Exit 0 = release claims match the facts.
Exit 1 = at least one release-facing claim drifted.
Exit 2 = setup error (missing repo files, git unavailable, tag not found).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

SEMVER = r"\d+\.\d+\.\d+"
# Release-advertising positions (historical tokens elsewhere are ignored).
README_BADGE_RE = re.compile(r"badge/version-(" + SEMVER + r")")
README_WHATSNEW_RE = re.compile(r"^##\s+What's new in\s+(" + SEMVER + r")", re.MULTILINE)
README_SKILLS_BADGE_RE = re.compile(r"badge/skills-(\d+)")
CHANGELOG_TOP_RE = re.compile(r"^##\s+v(" + SEMVER + r")\b", re.MULTILINE)
PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"(' + SEMVER + r')"', re.MULTILINE)
BUNDLE_VERSION_RE = re.compile(r"^version:\s*(" + SEMVER + r")\s*$", re.MULTILINE)


def _die(msg: str) -> NoReturn:
    print(f"check-version-truth: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _read(path: Path) -> str:
    if not path.exists():
        _die(f"missing {path}")
    return path.read_text(encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        _die("git is not available; cannot resolve the released tag")
    if proc.returncode != 0:
        _die(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _candidate(repo: Path) -> str:
    m = PYPROJECT_VERSION_RE.search(_read(repo / "pyproject.toml"))
    if not m:
        _die("pyproject.toml has no [project].version")
    return m.group(1)


def _bundle(repo: Path) -> str:
    m = BUNDLE_VERSION_RE.search(_read(repo / "capability.yaml"))
    if not m:
        _die("capability.yaml has no top-level version:")
    return m.group(1)


def _skills_on_disk(repo: Path) -> list[str]:
    skills = repo / "skills"
    if not skills.is_dir():
        _die("no skills/ directory")
    return sorted(
        d.name for d in skills.iterdir() if (d / "capability.yaml").is_file()
    )


def _semver_key(version: str) -> tuple[int, int, int]:
    return tuple(int(p) for p in version.split("."))  # type: ignore[return-value]


def _latest_release_tag(repo: Path) -> str:
    """The released tag is the highest semver ``vX.Y.Z`` tag in the repository.

    This is the canonical release-metadata fact: the check evaluates the tree
    against *the latest canonical tag*, never against branch HEAD and never
    against whatever the badge happens to claim (a badge naming a tag that does
    not exist is a drift to report, not a lookup key).
    """
    out = _git(repo, "tag", "--list", "v*")
    versions = [t[1:] for t in out.split() if re.fullmatch(r"v" + SEMVER, t)]
    if not versions:
        _die("no annotated vX.Y.Z release tag found")
    return "v" + max(versions, key=_semver_key)


def _release_face(repo: Path) -> tuple[str, str, str]:
    """Return (tag_name, peeled_commit, subject) for the released tag.

    Annotation-aware: the released version is established from the latest
    canonical tag's PEELED commit (``git rev-parse '<tag>^{}'``) plus the tag's
    canonical subject, never by requiring the tag to equal the branch HEAD.
    """
    tag = _latest_release_tag(repo)
    version = tag.lstrip("v")
    # Annotation-aware: resolve the commit the tag PEELS to, not branch HEAD.
    peeled = _git(repo, "rev-parse", f"{tag}^{{}}")
    if not re.fullmatch(r"[0-9a-f]{40}", peeled):
        _die(f"could not peel {tag} to a commit")
    subject = _git(
        repo, "for-each-ref", f"refs/tags/{tag}", "--format=%(contents:subject)"
    )
    if not subject:
        _die(f"no canonical subject for {tag}")
    if version not in subject:
        _die(
            f"canonical release metadata for {tag} does not name {version}: "
            f"{subject!r}"
        )
    return tag, peeled, subject


def _installed_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return str(version("skillweave"))
        except PackageNotFoundError:
            return None
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="check-version-truth")
    ap.add_argument("--repo", type=Path, default=Path("."))
    args = ap.parse_args()
    repo = args.repo

    candidate = _candidate(repo)
    bundle = _bundle(repo)
    readme = _read(repo / "README.md")
    changelog = _read(repo / "CHANGELOG.md")
    tag, peeled, subject = _release_face(repo)
    released = tag.lstrip("v")

    failed = False

    def check(ok: bool, label: str, detail: str) -> None:
        nonlocal failed
        if ok:
            print(f"  ok        {label}: {detail}")
        else:
            failed = True
            print(f"  DRIFT     {label}: {detail}")

    # 1. required surfaces agree (candidate == bundle).
    check(
        candidate == bundle,
        "candidate==bundle",
        f"pyproject {candidate} vs capability.yaml {bundle}",
    )

    # 2. release-facing badge advertises the released tag.
    badge = README_BADGE_RE.search(readme)
    check(
        badge is not None and badge.group(1) == released,
        "badge==released",
        f"badge {badge.group(1) if badge else None!r} vs released {released}",
    )

    # 3. changelog top heading names the released version.
    top = CHANGELOG_TOP_RE.search(changelog)
    check(
        top is not None and top.group(1) == released,
        "changelog-top==released",
        f"top {top.group(1) if top else None!r} vs released {released}",
    )

    # 3b. the "What's new" heading (if present) advertises the released version.
    whatsnew = README_WHATSNEW_RE.search(readme)
    if whatsnew is not None:
        check(
            whatsnew.group(1) == released,
            "whats-new==released",
            f"whats-new {whatsnew.group(1)!r} vs released {released}",
        )

    # 4. installed identity matches the candidate (absence is reported).
    installed = _installed_version()
    if installed is None:
        print("  note      installed: not installed in this interpreter "
              "(reported, not faked)")
    else:
        check(
            installed == candidate,
            "installed==candidate",
            f"installed {installed} vs candidate {candidate}",
        )

    # 5. skill inventory agrees (count and names).
    on_disk = _skills_on_disk(repo)
    n = len(on_disk)
    mb = README_SKILLS_BADGE_RE.search(readme)
    check(
        mb is not None and int(mb.group(1)) == n,
        "skills-badge==inventory",
        f"badge {mb.group(1) if mb else None!r} vs on-disk {n}",
    )
    check(
        f"all {n} skills" in readme,
        "skills-count-in-prose",
        f"README states 'all {n} skills': {f'all {n} skills' in readme}",
    )
    missing = [name for name in on_disk if name not in readme]
    check(
        missing == [],
        "skills-named-in-readme",
        f"missing: {missing}",
    )

    if failed:
        print("check-version-truth: FAIL — a release-facing claim drifted")
        return 1
    print(
        f"check-version-truth: OK — candidate {candidate}, released {tag} "
        f"(peeled {peeled[:12]}), {n} skills"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
