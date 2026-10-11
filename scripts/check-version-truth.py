#!/usr/bin/env python3
"""check-version-truth — release-facing version claims must match the tree.

Where ``check-manifest.py`` asks "does a bundle pin agree with its member file",
and ``version-sync`` asks "does every declared location agree with the source",
this tool asks a third, release-facing question:

    Does what the *public release surface* says about the version match the
    facts the tree and an installed artifact actually carry?

The facts (see ``docs/version-truth.md``) are:

    candidate   pyproject.toml [project].version and the bundle capability.yaml
    released    the latest canonical ANNOTATED tag, by its PEELED commit
    installed   the local distribution actually present (importlib.metadata):
                version, dist name, origin path and how it was installed
    discovered  the version a registry/index OBSERVES as available, taken from
                an operator-supplied observation file -- this tool performs NO
                live registry query
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

The ``installed`` and ``discovered`` facts are also **reported** (as separate
observations, in both the human text and ``--json`` output) -- never merged and
never compared as if equal. A discovered observation is shown with its source,
index URL and observation time; its age is reported against a freshness
threshold and a stale observation is marked ``STALE``, never silently trusted
(``--freshness-hours``, default 48). When no observation is supplied the tool
prints ``no discovered observation supplied`` rather than inventing a value; a
malformed observation is a setup error. A difference between the discovered and
the installed/candidate versions is *shown*, not hidden, and is not itself a
release-claim drift.

Historical references are ALLOWED: the check reads only the *release-advertising
positions* (the badge, the "What's new in X" heading, the CHANGELOG top). An old
version mentioned elsewhere as history (e.g. "1.4.0 scope") is not a failure.
What is forbidden is advertising an old version as current, or advertising a
version AHEAD of the tag (e.g. an unreleased 1.5.10) as released.

Release-prep exception: the release commit must carry the new CHANGELOG section,
badge and "What's new" heading BEFORE the tag exists, so those positions will
name the still-unreleased candidate. An explicit, reviewable flat scalar
``release_prep: "X.Y.Z"`` in ``.version.yaml`` declares that state: when it
equals the candidate (pyproject == bundle) and is ahead of the released tag,
the three advertising positions may name the candidate -- provided they all
agree with it. Any of them naming something else is DRIFT. A ``release_prep``
that differs from the candidate, or is not ahead of the released tag, is itself
a named DRIFT. Without the flag nothing changes: a version ahead of the tag in
an advertising position is DRIFT.

Annotation-aware: the released version is established from the tag's PEELED
commit (``git rev-parse '<tag>^{}'``) plus the tag's canonical subject, never by
requiring the tag to equal the current branch HEAD.

Stdlib only. Usage:
    check-version-truth.py [--repo PATH] [--discovered-observation PATH]
                           [--freshness-hours N] [--json]
Exit 0 = release claims match the facts.
Exit 1 = at least one release-facing claim drifted.
Exit 2 = setup error (missing repo files, git unavailable, tag not found,
         unreadable/malformed discovered-observation file).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
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
# The explicit release-prep scalar in .version.yaml (flat top-level key).
RELEASE_PREP_RE = re.compile(
    r'^release_prep:\s*"?' + "(" + SEMVER + r')"?\s*$', re.MULTILINE
)


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


def _release_prep(repo: Path) -> str | None:
    """The explicit release-prep candidate, or None when the flag is absent.

    ``release_prep`` is a flat scalar in ``.version.yaml``. It is present ONLY
    on the release commit, where the new CHANGELOG section, README badge and
    "What's new" heading must name the still-unreleased candidate because the
    release tag does not yet exist. Its absence -- whether the file or the key
    is missing -- means the ordinary rule applies: a version ahead of the
    released tag in a release-advertising position is drift.
    """
    path = repo / ".version.yaml"
    if not path.exists():
        return None
    m = RELEASE_PREP_RE.search(path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


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


def _installed_identity() -> dict | None:
    """The locally installed distribution identity, or None if absent.

    Real producer/consumer: reads the distribution actually present in this
    interpreter via importlib.metadata -- version, dist name, origin path and
    (when available) how it was installed -- so the same numbers the CLI's
    ``product_version()`` would report are shown here, not a helper's guess.
    """
    try:
        from importlib.metadata import (
            PackageNotFoundError,
            distribution,
            version,
        )

        try:
            ver = str(version("skillweave"))
        except PackageNotFoundError:
            return None
        dist = distribution("skillweave")

        origin: str | None = None
        origin_kind = "unknown"
        try:
            direct = dist.read_text("direct_url.json")
        except Exception:  # noqa: BLE001 - best-effort metadata
            direct = None
        if direct:
            try:
                du = json.loads(direct)
                origin = du.get("url")
                dir_info = du.get("dir_info") or {}
                origin_kind = "editable-checkout" if dir_info.get("editable") else "local-vcs"
            except Exception:  # noqa: BLE001 - best-effort metadata
                origin = None
        if origin is None:
            try:
                loc = str(dist.locate_file(""))
                origin = loc
                origin_kind = "site-packages"
            except Exception:  # noqa: BLE001 - best-effort metadata
                origin = None

        return {
            "version": ver,
            "dist_name": dist.metadata["Name"],
            "origin": origin,
            "origin_kind": origin_kind,
        }
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return None


def _load_discovered_observation(fpath: Path, freshness_hours: float) -> dict:
    """Parse an operator-supplied registry/index observation (no live query).

    Expected JSON::

        {"source": "...", "index_url": "...", "name": "skillweave",
         "version": "1.5.9", "observed_at": "2026-10-09T00:00:00Z"}

    ``index_url``/``observed_at`` are optional; the rest are required. A
    malformed or unreadable file is a setup error (exit 2): a bad observation
    must never be silently treated as "no observation".
    """
    if not fpath.is_file():
        _die(f"discovered-observation file not found: {fpath}")
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _die(f"malformed discovered-observation JSON in {fpath}: {exc}")
    if not isinstance(data, dict):
        _die(f"discovered-observation in {fpath} must be a JSON object")

    for key in ("source", "name", "version"):
        if not data.get(key):
            _die(
                f"discovered-observation {fpath} is missing required field "
                f"{key!r}"
            )
    ver = str(data["version"])
    if not re.fullmatch(SEMVER, ver):
        _die(
            f"discovered-observation {fpath} version {ver!r} is not X.Y.Z"
        )

    observed_at = data.get("observed_at")
    age_hours: float | None = None
    if observed_at:
        try:
            ts = str(observed_at).replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_hours = round(
                (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 2
            )
        except Exception:  # noqa: BLE001
            _die(
                f"discovered-observation {fpath} observed_at {observed_at!r} "
                "is not ISO-8601"
            )

    stale = age_hours is not None and age_hours > freshness_hours
    return {
        "source": str(data["source"]),
        "index_url": data.get("index_url"),
        "name": str(data["name"]),
        "version": ver,
        "observed_at": observed_at,
        "age_hours": age_hours,
        "freshness_hours": freshness_hours,
        "stale": bool(stale),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="check-version-truth")
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument(
        "--discovered-observation",
        type=Path,
        default=None,
        help="operator-supplied registry/index observation JSON "
        "({source, index_url, name, version, observed_at}); no live query is run",
    )
    ap.add_argument(
        "--freshness-hours",
        type=float,
        default=48.0,
        help="age beyond which a discovered observation is marked STALE "
        "(default 48)",
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()
    repo = args.repo

    candidate = _candidate(repo)
    bundle = _bundle(repo)
    readme = _read(repo / "README.md")
    changelog = _read(repo / "CHANGELOG.md")
    tag, peeled, subject = _release_face(repo)
    released = tag.lstrip("v")
    release_prep = _release_prep(repo)

    checks: list[dict] = []
    failed = False

    def check(ok: bool, label: str, detail: str) -> None:
        nonlocal failed
        if not ok:
            failed = True
        checks.append({"label": label, "ok": bool(ok), "detail": detail})
        if not args.json:
            print(f"  {'ok       ' if ok else 'DRIFT    '} {label}: {detail}")

    # The explicit release-prep state (SW-CORE-RFX-001). It is ACTIVE only when
    # it names the candidate (pyproject == bundle) AND is ahead of the released
    # tag; then the three advertising positions may name the candidate. A
    # release_prep that is set but differs from the candidate, or is not ahead
    # of the released tag, is itself a named drift.
    prep_active = False
    if release_prep is not None:
        if release_prep != candidate:
            check(
                False,
                "release-prep==candidate",
                f"release_prep {release_prep!r} != candidate {candidate!r}",
            )
        elif candidate == bundle and _semver_key(candidate) > _semver_key(released):
            prep_active = True
            check(
                True,
                "release-prep",
                f"release-prep {candidate}: advertising positions may name "
                f"the candidate (ahead of released {released})",
            )
        else:
            check(
                False,
                "release-prep-ahead",
                f"release_prep {release_prep!r} is not ahead of released "
                f"{released} (or candidate != bundle)",
            )

    if prep_active:
        # During an active release-prep, an advertising position may name the
        # candidate (the fully-prepped release commit) OR the still-released tag
        # (a position not yet moved in the prep window). Anything else is DRIFT.
        adv_accepted = (candidate, released)
        adv_extra = f" (release-prep {candidate})"
    else:
        adv_accepted = (released,)
        adv_extra = ""

    # 1. required surfaces agree (candidate == bundle).
    check(
        candidate == bundle,
        "candidate==bundle",
        f"pyproject {candidate} vs capability.yaml {bundle}",
    )

    # 2. release-facing badge advertises an accepted version (the released tag,
    #    or -- during an active release-prep -- the candidate too).
    badge = README_BADGE_RE.search(readme)
    check(
        badge is not None and badge.group(1) in adv_accepted,
        "badge==released",
        f"badge {badge.group(1) if badge else None!r} vs accepted "
        f"{sorted(adv_accepted)}{adv_extra}",
    )

    # 3. changelog top heading names an accepted version.
    top = CHANGELOG_TOP_RE.search(changelog)
    check(
        top is not None and top.group(1) in adv_accepted,
        "changelog-top==released",
        f"top {top.group(1) if top else None!r} vs accepted "
        f"{sorted(adv_accepted)}{adv_extra}",
    )

    # 3b. the "What's new" heading (if present) advertises an accepted version.
    whatsnew = README_WHATSNEW_RE.search(readme)
    if whatsnew is not None:
        check(
            whatsnew.group(1) in adv_accepted,
            "whats-new==released",
            f"whats-new {whatsnew.group(1)!r} vs accepted "
            f"{sorted(adv_accepted)}{adv_extra}",
        )

    # 4. installed identity matches the candidate (absence is reported).
    installed = _installed_identity()
    if installed is None:
        if not args.json:
            print("  note      installed: not installed in this interpreter "
                  "(reported, not faked)")
    else:
        where = installed["origin"] or "origin unknown"
        if not args.json:
            print(
                f"  installed identity: {installed['dist_name']} "
                f"{installed['version']} ({installed['origin_kind']}): {where}"
            )
        check(
            installed["version"] == candidate,
            "installed==candidate",
            f"installed {installed['version']} vs candidate {candidate}",
        )

    # 4b. discovered (registry/index) version -- an operator observation, never
    #     a live query, never merged with installed/candidate.
    discovered = None
    if args.discovered_observation is not None:
        discovered = _load_discovered_observation(
            args.discovered_observation, args.freshness_hours
        )
        age = discovered["age_hours"]
        age_txt = (
            f"age {age}h" if age is not None else "age unknown (no observed_at)"
        )
        # A stale observation is flagged for the reader; freshness is advisory,
        # so it does not by itself make a release claim drift.
        mark = " [STALE]" if discovered["stale"] else ""
        if not args.json:
            print(
                f"  discovered: {discovered['name']} {discovered['version']} "
                f"from {discovered['source']} "
                f"({discovered['index_url'] or 'no index_url'}), "
                f"{age_txt} (freshness {discovered['freshness_hours']}h){mark}"
            )
        if installed is not None and installed["version"] != discovered["version"]:
            if not args.json:
                print(
                    f"  note      discovered {discovered['version']} != installed "
                    f"{installed['version']} (shown separately; not merged)"
                )
    elif not args.json:
        print("  note      discovered: no discovered observation supplied")

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

    rc = 1 if failed else 0

    if args.json:
        report = {
            "candidate": candidate,
            "bundle": bundle,
            "release_prep": release_prep,
            "released": {
                "tag": tag,
                "version": released,
                "peeled": peeled,
                "subject": subject,
            },
            "installed": installed,
            "discovered": discovered,
            "skills": {"count": n, "names": on_disk},
            "checks": checks,
            "ok": not failed,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return rc

    if failed:
        print("check-version-truth: FAIL — a release-facing claim drifted")
    else:
        prep_txt = f", release-prep {candidate}" if prep_active else ""
        print(
            f"check-version-truth: OK — candidate {candidate}, released {tag} "
            f"(peeled {peeled[:12]}), {n} skills{prep_txt}"
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
