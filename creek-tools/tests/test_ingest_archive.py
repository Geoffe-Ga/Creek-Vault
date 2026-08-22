"""``creek.ingest.archive`` — safe unpacking and content-based detection (#1525).

The unit half of #1525. Everything here runs against a bare directory and
never touches a vault, an ingestor or the MCP layer, because the two
properties this module owns are decidable without any of them:

* **containment** — a crafted member must not be written outside the root, and
  the proof is on the *filesystem*, not in a return value. A refusal that
  arrives after the write is not a refusal;
* **identification** — an unpacked tree names itself. The upload tool
  deliberately accepts no ``source_type``, so if detection can be fooled the
  whole no-override rule is decorative.

The bounds get two tests each and they are not redundant. The declared bound
is checked by an archive whose *header lies upward* — a kilobyte of real
payload claiming a gigabyte — so a pass can only come from reading the
declaration. The reverse lie, a header claiming less than the member holds, is
checked separately because the pre-extraction bound is only sound if a
declaration cannot be under-stated into a bypass.

The end-to-end behaviour these properties support — four export types, tiers
on disk, idempotency, partial failure — is asserted in
``tests/test_mcp_upload_archive.py`` against the real ingest pipeline.
"""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING, Final

import pytest

from creek.ingest.archive import (
    MAX_ARCHIVE_ENTRIES,
    MAX_EXTRACTED_BYTES,
    TOO_LARGE_REASON,
    TOO_MANY_ENTRIES_REASON,
    UNREADABLE_ARCHIVE_REASON,
    UNSAFE_ENTRY_REASON,
    ArchiveRefusedError,
    _write_member,
    detect_export_type,
    extract_archive,
    is_supported_archive,
)
from tests.archive_export_support import (
    chatgpt_archive,
    chatgpt_conversations,
    claude_archive,
    declared_huge_archive,
    discord_archive,
    substack_archive,
    symlink_archive,
    underdeclared_archive,
    zip_slip_archive,
)

if TYPE_CHECKING:
    from pathlib import Path

_ESCAPE_MARKER: Final[bytes] = b"escaped-past-the-staging-root-1525"
"""Payload a successful escape would leave behind, distinctive enough to grep."""


def _root(tmp_path: Path) -> Path:
    """Return an empty extraction root nested two levels under *tmp_path*.

    Two levels deep so a ``../../`` member's target lands inside *tmp_path* and
    can be asserted on, rather than somewhere the test has no business writing.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The created extraction root.
    """
    root = tmp_path / "vault" / "unpack"
    root.mkdir(parents=True)
    return root


def _files_under(path: Path) -> list[Path]:
    """Return every regular file under *path*, sorted.

    Args:
        path: The directory to walk.

    Returns:
        The files found.
    """
    return sorted(item for item in path.rglob("*") if item.is_file())


# --------------------------------------------------------------------------- #
# AC3 — containment
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("member", "case"),
    [
        ("../../etc/x", "parent-traversal"),
        ("../escaped.txt", "single-parent"),
        ("/etc/x", "absolute-posix"),
        ("nested/../../../escaped.txt", "traversal-mid-path"),
        ("C:\\Windows\\escaped.txt", "windows-drive"),
    ],
)
def test_an_escaping_member_is_refused_and_writes_nothing_outside_the_root(
    tmp_path: Path, member: str, case: str
) -> None:
    """Zip slip is refused, and the FILESYSTEM is what says so.

    The return value is not the assertion. An extractor that wrote
    ``../../etc/x`` and *then* noticed would return the same refusal as one
    that never wrote it, and only the second is safe. So this walks the whole
    of ``tmp_path`` — the extraction root's grandparent — and requires the
    escape marker to appear in no file at all.

    Swept over five shapes because they fail through different layers: ``..``
    components, an absolute POSIX path, and a Windows drive specifier that
    :class:`~pathlib.PurePosixPath` does not read as a path separator at all
    and would therefore let through as an ordinary filename on Linux.

    Args:
        tmp_path: pytest's per-test directory.
        member: The crafted member name.
        case: The shape's name, for the failure message.
    """
    root = _root(tmp_path)

    with pytest.raises(ArchiveRefusedError) as refusal:
        extract_archive(zip_slip_archive(member, _ESCAPE_MARKER), root)

    assert refusal.value.reason == UNSAFE_ENTRY_REASON, case
    leaked = [
        path for path in _files_under(tmp_path) if _ESCAPE_MARKER in path.read_bytes()
    ]
    assert leaked == [], (case, leaked)
    assert _files_under(root) == [], case


def test_a_symlink_member_is_refused(tmp_path: Path) -> None:
    """A ZIP can carry a symlink, and this one is refused rather than followed.

    Both plausible readings of such a member are wrong. Ignoring the mode bit
    writes the link *text* — here an absolute path — into a regular file,
    silently replacing the export's content with a path string. Honouring it
    creates a real link out of the staging root, which is
    :func:`~creek.ingest.archive._contained_destination`'s whole subject
    reached by a second road. Neither is worth having for a shape no real
    export uses.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)

    with pytest.raises(ArchiveRefusedError) as refusal:
        extract_archive(symlink_archive("link.json", "/etc/passwd"), root)

    assert refusal.value.reason == UNSAFE_ENTRY_REASON
    assert _files_under(root) == []
    assert not (root / "link.json").is_symlink()


def test_a_benign_nested_member_still_extracts(tmp_path: Path) -> None:
    """The positive control for the containment guard.

    Without it, a guard tightened until every member was refused would pass
    every test above.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)

    extract_archive(discord_archive(prefix="package/"), root)

    landed = _files_under(root)
    assert len(landed) == 4
    assert all(path.is_relative_to(root) for path in landed)


# --------------------------------------------------------------------------- #
# AC4 — bounds
# --------------------------------------------------------------------------- #


def test_a_declared_huge_archive_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """A zip bomb is refused on its own declaration, having written nothing.

    The fixture's real payload is one kilobyte and its header claims twice the
    cap, so the refusal can only have come from
    :func:`~creek.ingest.archive._refuse_declared_excess` reading the central
    directory. An archive that was genuinely huge would also be refused —
    by the streaming budget, after the host had paid to decompress it — and a
    test built on one could not tell the two guards apart.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)

    with pytest.raises(ArchiveRefusedError) as refusal:
        extract_archive(declared_huge_archive(MAX_EXTRACTED_BYTES * 2), root)

    assert refusal.value.reason == TOO_LARGE_REASON
    assert _files_under(root) == []


def test_an_entry_flood_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """Too many members is its own refusal, not a byte question.

    A directory bomb costs one filesystem entry per member and can sit far
    under the byte cap the whole way, so the entry bound is the only thing that
    expresses it.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(MAX_ARCHIVE_ENTRIES + 1):
            archive.writestr(f"d{index}/x.txt", b"")

    with pytest.raises(ArchiveRefusedError) as refusal:
        extract_archive(buffer.getvalue(), root)

    assert refusal.value.reason == TOO_MANY_ENTRIES_REASON
    assert _files_under(root) == []


def test_an_under_declared_member_cannot_overflow_the_declared_bound(
    tmp_path: Path,
) -> None:
    """A header that lies *downward* buys the attacker nothing.

    This is what makes the pre-extraction bound sound rather than merely
    optimistic — if a member could hold sixteen kilobytes while declaring one,
    the declared *sum* would be a number the archive got to choose and checking
    it would bound nothing.

    It cannot. :class:`zipfile.ZipExtFile` stops reading at the declared
    ``file_size`` and then verifies the member's CRC against the header, so an
    under-stated size is not merely truncated, it is **refused**: the truncated
    stream cannot match the recorded checksum. The refusal arrives as an
    unreadable-archive answer and, as with every other refusal here, leaves
    nothing on disk. If a future reader ever stopped verifying, this test would
    stop seeing a refusal and say so.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    real, declared = 16 * 1024, 1024

    with pytest.raises(ArchiveRefusedError) as refusal:
        extract_archive(underdeclared_archive(real, declared), root)

    assert refusal.value.reason == UNREADABLE_ARCHIVE_REASON
    assert _files_under(root) == []


def test_the_running_byte_budget_refuses_a_member_that_exceeds_it(
    tmp_path: Path,
) -> None:
    """The per-member budget really stops a write, exercised at its own seam.

    Driven directly rather than through an archive, and deliberately so. With
    :mod:`zipfile` the declared bound already dominates — the reader truncates
    at ``file_size`` (the test above), and the declared *sum* is refused before
    extraction — so no archive can reach this branch, and a test that claimed
    to reach it through one would be asserting nothing. The check is kept as
    the bound that stays true if the reader is ever swapped or a second
    container format added, and it is proven the only way it honestly can be:
    by calling it with the budget state it exists for.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    payload = b"y" * 4096
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writing:
        writing.writestr("big.json", payload)

    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        info = archive.infolist()[0]
        with pytest.raises(ArchiveRefusedError) as refusal:
            _write_member(archive, info, root / "big.json", budget=16)

    assert refusal.value.reason == TOO_LARGE_REASON


def test_bytes_that_are_not_a_zip_are_refused_as_unreadable(tmp_path: Path) -> None:
    """A ``.zip`` filename over non-ZIP bytes is a caller error, named as one.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)

    with pytest.raises(ArchiveRefusedError) as refusal:
        extract_archive(b'{"conversations": []}', root)

    assert refusal.value.reason == UNREADABLE_ARCHIVE_REASON
    assert _files_under(root) == []


def test_no_refusal_reason_names_a_member_or_a_path(tmp_path: Path) -> None:
    """Refusals disclose the property violated, never the archive's contents.

    A refusal that echoed the offending member would hand back a listing of the
    caller's own archive — harmless when the caller sent it, and not harmless
    at all once the same strings are rendered into an HTTP body, a log line or
    an audit entry that other surfaces serve onward.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    secret = "a-member-name-that-must-not-be-echoed"
    reasons: list[str] = []
    for payload in (
        zip_slip_archive(f"../{secret}.txt", _ESCAPE_MARKER),
        symlink_archive(f"{secret}.json", f"/etc/{secret}"),
        declared_huge_archive(MAX_EXTRACTED_BYTES * 2),
    ):
        with pytest.raises(ArchiveRefusedError) as refusal:
            extract_archive(payload, root)
        reasons.append(refusal.value.reason)

    assert len(reasons) == 3
    for reason in reasons:
        assert secret not in reason
        assert str(root) not in reason


# --------------------------------------------------------------------------- #
# Identification — from contents, never from a caller-supplied name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("prefix", ["", "package/", "export/2026/"])
@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (chatgpt_archive, "chatgpt"),
        (claude_archive, "claude"),
        (discord_archive, "discord"),
        (substack_archive, "substack"),
    ],
)
def test_each_export_is_identified_from_its_contents_at_any_nesting(
    tmp_path: Path,
    prefix: str,
    build: object,
    expected: str,
) -> None:
    """All four families are named correctly, wrapped or not.

    The nesting sweep matters because every platform wraps its payload
    differently — Discord's download nests everything under ``package/``,
    ChatGPT's does not — and an implementation that only looked at the
    extraction root would identify half of them and silently refuse the rest.

    Args:
        tmp_path: pytest's per-test directory.
        prefix: The directory the export is wrapped in.
        build: The fixture builder for this export family.
        expected: The registry key detection must return.
    """
    root = _root(tmp_path)
    extract_archive(build(prefix=prefix), root)  # type: ignore[operator]

    detected = detect_export_type(root)

    assert detected is not None
    source_type, ingest_root = detected
    assert source_type == expected
    assert ingest_root.is_relative_to(root)


def test_a_discord_export_is_not_mistaken_for_a_chatgpt_one(tmp_path: Path) -> None:
    """The one collision that would silently ingest the wrong way.

    A Discord channel's ``messages.json`` is a JSON list of dicts, which is
    exactly what :func:`creek.ingest.chatgpt._is_chatgpt_export` accepts. If
    detection reused that loose rule and reached a channel directory first, a
    Discord export would be handed to the ChatGPT ingestor, whose
    ``glob("*.json")`` would find those same files and whose parse would return
    nothing from them — a zero-fragment run reported as a success, which is the
    exact failure mode #1525 exists to close.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    extract_archive(discord_archive(prefix="package/"), root)

    detected = detect_export_type(root)

    assert detected is not None
    assert detected[0] == "discord"
    assert detected[1].name == "package"


def test_an_archive_of_ordinary_files_is_not_identified(tmp_path: Path) -> None:
    """Nothing recognisable means ``None``, never a plausible guess.

    Guessing here is how a caller gets an ingestor that discovers nothing and a
    response that calls it a success.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes/readme.txt", b"just some notes")
        archive.writestr("notes/data.json", b'{"unrelated": true}')
    extract_archive(buffer.getvalue(), root)

    assert detect_export_type(root) is None


def test_detection_reads_the_conversation_graph_not_the_filename(
    tmp_path: Path,
) -> None:
    """A ChatGPT export renamed is still a ChatGPT export.

    Detection is content-based or it is nothing: the upload surface accepts no
    ``source_type``, so a filename-driven answer would put the caller back in
    charge of choosing the ingestor by another name.

    Args:
        tmp_path: pytest's per-test directory.
    """
    root = _root(tmp_path)
    import json as _json

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "an-unhelpfully-named-file.json",
            _json.dumps(chatgpt_conversations()).encode(),
        )
    extract_archive(buffer.getvalue(), root)

    detected = detect_export_type(root)

    assert detected is not None
    assert detected[0] == "chatgpt"


@pytest.mark.parametrize(
    ("filename", "supported"),
    [
        ("export.zip", True),
        ("EXPORT.ZIP", True),
        ("export.tar", False),
        ("export.tgz", False),
        ("conversations.json", False),
        ("notes.md", False),
    ],
)
def test_only_the_declared_archive_suffixes_are_unpacked(
    filename: str, supported: bool
) -> None:
    """The fork is decided from the suffix, case-insensitively, and no wider.

    ``.tar`` and friends stay in the #1526 refusal table with their remedy;
    claiming them here would promise an unpacker Creek has no export to prove
    safe against.

    Args:
        filename: The caller's filename.
        supported: Whether it should take the archive fork.
    """
    assert is_supported_archive(filename) is supported
