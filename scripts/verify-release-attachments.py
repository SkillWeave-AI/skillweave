#!/usr/bin/env python3
"""Post-publication verification of the attached release assets (SW-1511-GATE c9).

Publication is not the end of a release. ``ReleaseHandler.publish_release``
reports which destinations accepted the release, but it never proves that the
release object a consumer downloads from carries the assets that were built:
an engine that publishes a release with a missing, extra or partially uploaded
attachment reports success all the same. This script closes that gap for the
Forgejo release workflow (``.forgejo/workflows/forgejo-release.yml``): after the
publication step it fetches the release by tag, downloads every attached asset
and compares the asset names and their bytes with the local ``SHA256SUMS`` the
build step produced in the same job.

Stdlib only: it runs on the runner's plain ``python3`` with no install step, so
the release job gains no new dependency. Nothing here is skippable and nothing
is inferred. The release object is the forge's, not a local reconstruction; the
expected digest for each name is read from the checksum file, never computed
from the same bytes it is meant to verify.

It fails by name. Every refusal raises an ``AttachmentVerificationError``
subclass whose class name is printed as a workflow error annotation, so a red
run says *which* obligation broke:

* ``MissingReleaseTokenError`` - no credential was supplied.
* ``MissingChecksumManifestError`` - no readable ``SHA256SUMS`` beside the assets.
* ``ReleaseLookupError`` - the release/tag could not be read from the forge, the
  service refused the read, or the release advertises an asset on a host other
  than the release host (the credential is never sent there).
* ``MissingReleaseAttachmentError`` - an asset the manifest declares is absent.
* ``UnexpectedReleaseAttachmentError`` - an attached asset the manifest does not
  declare (the release is not the set that was reviewed and built).
* ``AttachmentChecksumMismatchError`` - an attached asset's bytes differ from
  the digest in ``SHA256SUMS``.

Exit status: 0 when every declared asset is attached and byte-identical, 2 for a
named refusal, 1 for an unexpected error. Nothing is repaired or re-uploaded
here: the operator sees the drift. A local dry run never reaches this script,
because a local dry run never publishes and therefore never produces the release
object it reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHECKSUM_FILE = "SHA256SUMS"
API_PREFIX = "/api/v1/repos"
TOKEN_ENV = "FORGEJO_TOKEN"
_DOWNLOAD_TIMEOUT_SECONDS = 60
_HEX_DIGITS = set("0123456789abcdef")


class AttachmentVerificationError(Exception):
    """Base refusal: the class name is the workflow's error annotation."""


class MissingReleaseTokenError(AttachmentVerificationError):
    """No credential was supplied, so the release read cannot be authorized."""


class MissingChecksumManifestError(AttachmentVerificationError):
    """The local SHA256SUMS is absent, unreadable or not a checksum file."""


class ReleaseLookupError(AttachmentVerificationError):
    """The release object could not be read, or it points off the release host."""


class MissingReleaseAttachmentError(AttachmentVerificationError):
    """An asset declared by SHA256SUMS is not attached to the release."""


class UnexpectedReleaseAttachmentError(AttachmentVerificationError):
    """The release carries an asset SHA256SUMS does not declare."""


class AttachmentChecksumMismatchError(AttachmentVerificationError):
    """An attached asset's bytes differ from the digest in SHA256SUMS."""


class _CrossOriginRedirectRefused(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect whose target origin differs from the request origin.

    ``urllib`` re-sends the ``Authorization`` header to a redirect target, so a
    forge that answered ``302`` to another host would hand the release token to
    that host. Redirects inside the same origin (scheme, host and port) are
    followed; anything else is a named refusal.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise ReleaseLookupError(
                "the release host redirected %s to %s, whose origin differs "
                "(%s vs %s). The release credential is never sent to another "
                "origin; refusing the verification."
                % (req.full_url, newurl, _origin(req.full_url), _origin(newurl))
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_CrossOriginRedirectRefused)


def _origin(url: str) -> tuple[str, str, object]:
    """The (scheme, host, port) triple two URLs must share to be same-origin."""
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)


def read_checksum_manifest(assets_dir: Path) -> dict[str, str]:
    """Map asset name -> expected lowercase sha256, from ``SHA256SUMS``.

    The manifest is the reviewed expectation: it is produced by the build step
    in this same job and travelled with the assets. A missing file, a line that
    is not ``<64 hex>  <name>`` or a duplicate name is a named refusal -- the
    verification must never fall back to trusting the bytes it downloads.
    """
    manifest = assets_dir / CHECKSUM_FILE
    if not manifest.is_file():
        raise MissingChecksumManifestError(
            "no %s beside the release assets in %s; the expected attachment set "
            "is unknown, so nothing can be verified."
            % (CHECKSUM_FILE, assets_dir)
        )
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissingChecksumManifestError(
            "%s is unreadable: %s" % (manifest, exc)
        ) from exc

    expected: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64 or set(parts[0].lower()) - _HEX_DIGITS:
            raise MissingChecksumManifestError(
                "%s line %d is not '<sha256>  <name>': %r"
                % (manifest, number, raw)
            )
        digest, name = parts[0].lower(), parts[1]
        if name in expected:
            raise MissingChecksumManifestError(
                "%s declares %r twice; the expected set is ambiguous."
                % (manifest, name)
            )
        expected[name] = digest

    if not expected:
        raise MissingChecksumManifestError(
            "%s declares no assets; refusing to verify an empty expectation."
            % manifest
        )
    return expected


def fetch_release(base_url: str, repository: str, tag: str, token: str) -> dict:
    """The release object the forge returns for ``tag``.

    Read from the forge's release API -- never from a local file, an environment
    variable or a reconstruction of what we believe was uploaded.
    """
    url = "%s%s/%s/releases/tags/%s" % (
        base_url.rstrip("/"),
        API_PREFIX,
        repository.strip("/"),
        urllib.parse.quote(tag, safe=""),
    )
    raw = _request(url, token)
    try:
        release = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseLookupError(
            "the release API answered %s with a body that is not JSON: %s"
            % (url, exc)
        ) from exc
    if not isinstance(release, dict):
        raise ReleaseLookupError(
            "the release API answered %s with %s, not a release object."
            % (url, type(release).__name__)
        )
    return release


def attached_assets(release: dict, base_url: str, repository: str) -> dict[str, str]:
    """Map attached asset name -> download URL, from the release object alone."""
    tag = release.get("tag_name") or ""
    release_id = release.get("id")
    assets: dict[str, str] = {}
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not name:
            continue
        url = asset.get("browser_download_url")
        if not url:
            url = "%s/%s/releases/download/%s/%s" % (
                base_url.rstrip("/"),
                repository.strip("/"),
                urllib.parse.quote(str(tag), safe=""),
                urllib.parse.quote(name, safe=""),
            )
        if name in assets:
            raise UnexpectedReleaseAttachmentError(
                "the release attaches %r more than once; the attached set is "
                "ambiguous." % name
            )
        assets[name] = url
    if release_id is None and not assets:
        raise ReleaseLookupError(
            "the release object carries neither an id nor any attached asset."
        )
    return assets


def verify(base_url: str, repository: str, tag: str, token: str, assets_dir: Path) -> list[str]:
    """Verify the attached set; return the names proven, in order.

    Every check is a refusal by name, in this order: the attached names must be
    exactly the declared names (no missing, no extra), then each declared asset
    is downloaded and its bytes must hash to the digest SHA256SUMS declares.
    """
    expected = read_checksum_manifest(assets_dir)
    release = fetch_release(base_url, repository, tag, token)
    attached = attached_assets(release, base_url, repository)

    missing = sorted(set(expected) - set(attached))
    if missing:
        raise MissingReleaseAttachmentError(
            "release %s is missing %d declared asset(s): %s. The published "
            "release is not the set the build step produced."
            % (tag, len(missing), ", ".join(missing))
        )

    extra = sorted(set(attached) - set(expected))
    if extra:
        raise UnexpectedReleaseAttachmentError(
            "release %s attaches %d undeclared asset(s): %s. The published "
            "release carries files SHA256SUMS does not declare."
            % (tag, len(extra), ", ".join(extra))
        )

    verified: list[str] = []
    for name in sorted(expected):
        url = attached[name]
        if _origin(url) != _origin(base_url):
            raise ReleaseLookupError(
                "asset %r is offered at %s, whose origin differs from the "
                "release host %s. The release credential is never sent to "
                "another origin; refusing the verification."
                % (name, url, base_url)
            )
        digest = hashlib.sha256(_request(url, token)).hexdigest()
        if digest != expected[name]:
            raise AttachmentChecksumMismatchError(
                "attached asset %r hashes to %s but %s declares %s. The bytes "
                "on the released release differ from the reviewed build."
                % (name, digest, CHECKSUM_FILE, expected[name])
            )
        verified.append(name)
        print("verified attached asset %s %s" % (name, digest))
    return verified


def _request(url: str, token: str) -> bytes:
    """GET ``url`` with the release credential; any failure is a named refusal."""
    request = urllib.request.Request(url, headers={"Authorization": "token %s" % token})
    try:
        with _OPENER.open(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            return response.read()
    except AttachmentVerificationError:
        raise
    except urllib.error.HTTPError as exc:
        raise ReleaseLookupError(
            "the release host answered HTTP %s for %s" % (exc.code, url)
        ) from exc
    except urllib.error.URLError as exc:
        raise ReleaseLookupError(
            "the release host could not be reached for %s: %s" % (url, exc.reason)
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the assets attached to a published release against the "
            "local SHA256SUMS (SW-1511-GATE criterion 9)."
        )
    )
    parser.add_argument("--tag", required=True, help="the released tag, e.g. v1.5.10")
    parser.add_argument("--base-url", required=True, help="the canonical forge base URL")
    parser.add_argument("--repository", required=True, help="owner/name on that forge")
    parser.add_argument(
        "--assets-dir",
        default="release-assets",
        help="directory holding the built assets and SHA256SUMS",
    )
    args = parser.parse_args(argv)

    # The token is read from the environment only: an argument would land in the
    # runner's process list and its logs.
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(
            "::error::MissingReleaseTokenError: %s is not set, so the published "
            "release cannot be read and its attachments cannot be verified. A "
            "release is not verified by assuming it was uploaded." % TOKEN_ENV,
            file=sys.stderr,
        )
        return 2

    try:
        verified = verify(
            args.base_url,
            args.repository,
            args.tag,
            token,
            Path(args.assets_dir),
        )
    except AttachmentVerificationError as exc:
        print(
            "::error::%s: %s" % (type(exc).__name__, exc),
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(
            "::error::AttachmentVerificationError: %s" % exc,
            file=sys.stderr,
        )
        return 2

    print(
        "post-publication attachment verification passed for %s: %d asset(s) "
        "match %s" % (args.tag, len(verified), CHECKSUM_FILE)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
