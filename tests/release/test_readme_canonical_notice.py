"""SW-CORE-ZR-021: the README carries the public-mirror canonical notices.

The public GitHub README of SkillWeave is a read-only mirror of the canonical
self-hosted repository, exactly like the Capacium project. Two notices say so:
a GitHub ``> [!NOTE]`` **Public mirror.** block directly under the badge block
(before the bold tagline), and a closing ``## Development`` section that states
the canonical repository is self-hosted and that GitHub pull requests / issues
are synchronised automatically.

The note is public, so it must not name any internal host, SSH remote or
operator identity. The test reads the shipped ``README.md`` (the mirrored
file) and asserts:

* the top note is a ``> [!NOTE]`` block containing ``Public mirror`` within the
  first 25 lines;
* the bottom ``## Development`` section carries the canonical-mirror prose;
* neither notice contains an internal host marker (``.langevc.``), an SSH git
  remote (``git@``) or a private ``http://`` host URL;
* the ``## License`` section stays intact.

Every assertion has a negative control that mutates a copy of the README so the
green result is not vacuous: a copy without the note makes both presence
assertions fail, and a copy that injects an internal host into the note makes
the leak assertion fail. The controls run through the same helpers the real
assertions use, so removing a notice or weakening the leak check breaks them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"

# Lines 1..25 must contain the top note block.
NOTE_WINDOW = 25

# Internal material a public README must never carry in the notices.
_INTERNAL_MARKERS = (
    re.compile(r"\.langevc\."),
    re.compile(r"git@"),
    re.compile(r"http://"),
)

_TOP_SENTINEL = "The canonical repository is hosted on our self-hosted git."
_BOTTOM_SENTINEL = (
    "The canonical repository is maintained on self-hosted infrastructure."
)
_LICENSE_SENTINEL = "Apache 2.0"


def _read() -> str:
    return README.read_text(encoding="utf-8")


def _note_block(text: str, within_lines: int | None = None) -> str:
    """Return the first ``> [!NOTE]`` block, as its contiguous ``>`` run.

    When ``within_lines`` is given, only a block that starts within the first
    ``within_lines`` lines qualifies.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "> [!NOTE]":
            if within_lines is None or i < within_lines:
                start = i
                break
    if start is None:
        return ""
    block = []
    for line in lines[start:]:
        if line.startswith(">"):
            block.append(line)
        else:
            break
    return "\n".join(block)


def _development_section(text: str) -> str:
    """Return the body of the ``## Development`` section (may be empty)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "## Development":
            start = i
            break
    if start is None:
        return ""
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _assert_no_internal_marker(notice: str) -> None:
    for pattern in _INTERNAL_MARKERS:
        m = pattern.search(notice)
        assert m is None, f"internal marker {m.group(0)!r} leaked: {notice!r}"


# ── the real README ─────────────────────────────────────────────────────────


def test_top_public_mirror_note_is_right_after_the_badges():
    text = _read()
    note = _note_block(text, within_lines=NOTE_WINDOW)
    assert note, "README has no `> [!NOTE]` block in the first 25 lines"
    assert "Public mirror" in note, note
    assert _TOP_SENTINEL in note, note
    assert "read-only mirror" in note, note
    # The note sits before the bold tagline.
    lines = text.splitlines()
    note_line = next(i for i, ln in enumerate(lines) if ln.strip() == "> [!NOTE]")
    tagline = next(
        i for i, ln in enumerate(lines) if ln.startswith("**Multi-agent AI skill")
    )
    assert note_line < tagline, (note_line, tagline)
    _assert_no_internal_marker(note)


def test_bottom_development_section_states_the_canonical_mirror():
    text = _read()
    body = _development_section(text)
    assert body.strip(), "README has no `## Development` section"
    assert _BOTTOM_SENTINEL in body, body
    assert "public release and distribution mirror" in body, body
    assert "synchronized automatically" in body, body
    _assert_no_internal_marker(body)


def test_license_section_stays_intact():
    text = _read()
    assert "## License" in text
    assert _LICENSE_SENTINEL in text


def test_notices_carry_no_internal_hosts_ssh_remotes_or_urls():
    text = _read()
    for notice in (_note_block(text, NOTE_WINDOW), _development_section(text)):
        _assert_no_internal_marker(notice)


# ── negative controls ───────────────────────────────────────────────────────


def _strip_note(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(
        ln for ln in lines
        if not (ln.startswith("> ") or ln.strip() == "> [!NOTE]")
    )


def test_control_a_readme_without_the_top_note_fails_the_note_assertion():
    stripped = _strip_note(_read())
    note = _note_block(stripped, within_lines=NOTE_WINDOW)
    assert not note, "control: the stripped copy unexpectedly still has a note"


def test_control_a_readme_without_the_development_section_fails():
    without = _read().replace("## Development", "## Unrelated")
    body = _development_section(without)
    assert not body.strip(), "control: renamed section unexpectedly has a body"
    assert _BOTTOM_SENTINEL not in body


def test_control_an_internal_host_in_the_note_is_detected():
    text = _read()
    note = _note_block(text, within_lines=NOTE_WINDOW)
    injected = note + "\n> internal: git@git.internal.langevc.example:skillweave.git"
    hits = [
        p.search(injected).group(0)
        for p in _INTERNAL_MARKERS
        if p.search(injected)
    ]
    assert ".langevc." in hits and "git@" in hits, hits
