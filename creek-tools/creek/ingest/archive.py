"""Export-archive extraction and export-type detection (#1525).

The four richest sources a person owns — Discord history, a ChatGPT export, a
Claude export, a Substack export — are shipped by their platforms as one
``.zip`` and read by Creek as a *directory*. Every one of those ingestors bails
on a non-directory input (``chatgpt.py``/``claude.py``/``discord.py``/
``substack.py`` each open ``discover`` with an ``is_dir()`` guard), so before
this module the only upload surface Creek published could not accept any of
them: ``creek_mcp.tools.upload`` refused ``.zip`` with a "unpack it yourself"
remedy, which a remote consumer holding only bytes cannot carry out.

This module supplies the two things that were missing, and deliberately
nothing else — it never writes a fragment, never touches a ledger and never
reads the vault:

1. :func:`extract_archive`, a **containment-first** unpacker. It is not
   ``ZipFile.extractall``: that helper sanitises member names and would answer
   a crafted archive by silently writing a *different* file rather than by
   refusing, and it follows a member whose mode says "symlink" by writing the
   link's text into a regular file, which quietly destroys the export.
2. :func:`detect_export_type`, which decides *which* ingestor an unpacked tree
   belongs to **from the tree's own contents**. That is the point of the rule
   ``upload_tool``'s docstring states: there is no caller-supplied
   ``source_type``, because a caller that could name one could name an
   ingestor that discovers nothing and have the silent no-op reported as a
   success.

**Why the refusal reasons are constants and content-free.** Every string this
module can put in front of a caller is declared at module level, names no
archive member, no path and no byte of the caller's content, and is imported
by the HTTP adapter rather than copied — a refusal built by matching a
re-spelled literal is one that silently becomes a ``500``.

**The bounds are checked against the declaration, and that is sound here
rather than merely convenient.** :data:`MAX_ARCHIVE_ENTRIES` and
:data:`MAX_EXTRACTED_BYTES` are both tested against the central directory
before a byte is decompressed, which is the only way a zip bomb is refused
without the host paying to unpack it. The obvious objection is that a header
can lie — and it can, in both directions, and neither direction wins:

* claiming *more* than the member holds is refused by that pre-check, which is
  the case the check exists for;
* claiming *less* cannot smuggle content past it, because
  :class:`zipfile.ZipExtFile` stops at the declared ``file_size`` and then
  verifies the member's CRC, so an under-stated size fails the checksum and the
  archive is refused outright.

:func:`_write_member` still meters every member against a running budget. With
:mod:`zipfile` that budget is unreachable — the two properties above already
bound it — and it is kept, and tested at its own seam rather than through an
archive that cannot reach it, because it is the bound that survives a change of
reader or the addition of a second container format, neither of which comes
with the truncate-and-verify guarantee this reasoning rests on.
"""

from __future__ import annotations

import io
import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from creek.ingest._detection import POSTS_CSV_FILENAME, extract_post_id

if TYPE_CHECKING:
    from collections.abc import Callable

ARCHIVE_SUFFIXES: Final[frozenset[str]] = frozenset({".zip"})
"""The archive containers the upload surface unpacks.

``.zip`` alone, and that is the honest set rather than a placeholder: it is
what ChatGPT, Claude, Discord and Substack actually hand a user when they ask
for their data. ``.tar``/``.gz``/``.7z``/``.rar`` stay in
:data:`creek.ingest.gdrive._EXTENSION_ROUTES`' refusal table, where they earn
:data:`~creek.ingest.gdrive.ARCHIVE_GUIDANCE`, because an unpacker Creek has no
export to test it against is an unpacker nobody has proven safe.
"""

MAX_ARCHIVE_ENTRIES: Final[int] = 20_000
"""Most members an archive may declare or contain.

Sized above a real export and far below a directory bomb: a Discord export of
a heavy decade runs to a few thousand channel directories, and the cost this
bounds is one filesystem entry per member, which no byte cap can express.
"""

MAX_EXTRACTED_BYTES: Final[int] = 64 * 1024 * 1024
"""Most bytes an archive may unpack to, declared *and* actually written.

Larger than :data:`creek_mcp.tools.upload.MAX_UPLOAD_BYTES` on purpose: a
JSON export compresses hard, so a 10 MiB upload legitimately becomes tens of
megabytes of conversation text, and a cap equal to the upload cap would refuse
every real archive. It is the ceiling on what a *compressed* payload may cost
the host, which is the quantity a zip bomb attacks.
"""

_COPY_CHUNK_BYTES: Final[int] = 64 * 1024
"""How much of a member is held in memory at once while it is written."""

_MAX_DETECTION_PROBES: Final[int] = 64
"""JSON files parsed per candidate directory while identifying an export.

Bounds detection's cost independently of the byte cap: a directory of ten
thousand tiny JSON files fits well under :data:`MAX_EXTRACTED_BYTES` and would
otherwise cost ten thousand parses to answer one question.
"""

_MAX_DETECTION_DEPTH: Final[int] = 4
"""How far below the extraction root an export root is looked for.

Exports nest their payload one or two levels down (``package/messages/…``,
``export/posts/…``); four is slack over the deepest layout observed, and a
bound rather than an unbounded walk so a wide tree cannot turn detection into
the cost the entry cap exists to refuse.
"""

UNREADABLE_ARCHIVE_REASON: Final[str] = "the upload is not a readable .zip archive"
"""Refusal for bytes that are not a ZIP at all, or whose directory is corrupt."""

TOO_MANY_ENTRIES_REASON: Final[str] = (
    f"the archive holds more than {MAX_ARCHIVE_ENTRIES} entries"
)
"""Refusal for an archive over the member-count bound, declared or real."""

TOO_LARGE_REASON: Final[str] = (
    f"the archive's contents exceed the {MAX_EXTRACTED_BYTES}-byte extraction cap"
)
"""Refusal for an archive over the byte bound, declared or real."""

UNSAFE_ENTRY_REASON: Final[str] = (
    "the archive holds an entry that would be written outside the staging directory"
)
"""Refusal for a member that escapes the staging root, or is a link or device.

Named for the property that is violated — containment — rather than for the
member that violates it, so the refusal cannot be read back as a listing of
what the archive contained.
"""

UNRECOGNISED_EXPORT_REASON: Final[str] = (
    "Creek could not identify this archive as a chatgpt, claude, discord or "
    "substack export. Unpack it and run `creek ingest --type "
    "<chatgpt|claude|discord|substack> --input <unpacked-dir>` if it is one."
)
"""Refusal for an archive whose contents match no known export layout.

Carries a remedy, like every other refusal in the #1526 family: an archive
Creek cannot name is one whose owner may still know what it is.
"""


class ArchiveRefusedError(Exception):
    """An archive Creek will not unpack, carrying the content-free reason.

    Attributes:
        reason: One of this module's refusal constants, verbatim. Never a
            composed string: the HTTP adapter recognises upload refusals by
            comparing against these constants, and a reason assembled from an
            archive member's name would both defeat that recognition and put
            the caller's content into a refusal body.
    """

    def __init__(self, reason: str) -> None:
        """Refuse with *reason*.

        Args:
            reason: One of the module-level refusal constants.
        """
        super().__init__(reason)
        self.reason = reason


def is_supported_archive(filename: str) -> bool:
    """Report whether *filename*'s extension names an archive Creek unpacks.

    Decided from the caller's own filename and nothing else, exactly as
    ingestor dispatch is, so asking the question discloses nothing.

    Args:
        filename: The caller's original filename.

    Returns:
        ``True`` when the suffix is in :data:`ARCHIVE_SUFFIXES`.
    """
    return Path(filename).suffix.lower() in ARCHIVE_SUFFIXES


def _contained_destination(root: Path, member_name: str) -> Path:
    """Return where *member_name* may be written under *root*, or refuse.

    **This is the zip-slip guard**, and it is one function rather than a
    condition spread over the extraction loop so that there is exactly one
    place containment is decided and exactly one place a reviewer has to look
    to see whether it still holds.

    Two independent layers, because each catches what the other cannot:

    * the *lexical* layer refuses a member whose POSIX path is absolute, holds
      a ``..`` component, or carries a Windows drive or backslash separator —
      the shapes ``../../etc/passwd`` and ``C:\\Windows\\x`` take, which a
      containment test alone would let through on the platforms where
      :class:`~pathlib.Path` does not read them as separators at all;
    * the *resolved* layer requires the destination, resolved against the
      filesystem, to lie inside the resolved root — which is what catches an
      escape assembled out of components none of which is itself suspicious.

    Args:
        root: The staging root the archive is being unpacked into. Must exist.
        member_name: The member's name exactly as the archive declares it.

    Returns:
        The absolute destination path, proven contained.

    Raises:
        ArchiveRefusedError: With :data:`UNSAFE_ENTRY_REASON` when the member
            escapes, under either layer.
    """
    pure = PurePosixPath(member_name)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in member_name
        or ":" in member_name
    ):
        raise ArchiveRefusedError(UNSAFE_ENTRY_REASON)
    destination = (root / pure).resolve()
    if destination != root.resolve() and not destination.is_relative_to(root.resolve()):
        raise ArchiveRefusedError(UNSAFE_ENTRY_REASON)
    return destination


def _refuse_unwritable_kind(info: zipfile.ZipInfo) -> None:
    """Refuse a member that is neither a regular file nor a directory.

    A ZIP can carry a symlink, and it carries it as an ordinary entry whose
    *body* is the link text and whose external attributes say ``S_IFLNK``. A
    reader that ignores the mode writes the link text into a regular file and
    silently corrupts the export; a reader that honours it without checking
    the target creates a link out of the staging root, which is the same
    escape :func:`_contained_destination` refuses spelled a second way. Neither
    is a trade worth making for a format no real export uses, so the entry is
    refused outright. Device nodes and FIFOs go the same way.

    Mode ``0`` is accepted: MS-DOS-created archives — which is what a browser
    download often is — leave the Unix half of ``external_attr`` empty, and
    those members are plain files.

    Args:
        info: The member's central-directory record.

    Raises:
        ArchiveRefusedError: With :data:`UNSAFE_ENTRY_REASON` for any other kind.
    """
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ArchiveRefusedError(UNSAFE_ENTRY_REASON)


def _refuse_declared_excess(infos: list[zipfile.ZipInfo]) -> None:
    """Refuse an archive whose own central directory exceeds the bounds.

    Runs before a single member is opened, which is the whole point: a zip
    bomb is refused on the strength of what it claims about itself, without
    the host paying to decompress it. The same bounds are re-checked against
    the bytes actually written, because a declaration is the attacker's to
    write.

    Args:
        infos: Every member record in the archive.

    Raises:
        ArchiveRefusedError: With :data:`TOO_MANY_ENTRIES_REASON` or
            :data:`TOO_LARGE_REASON`.
    """
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ArchiveRefusedError(TOO_MANY_ENTRIES_REASON)
    if sum(info.file_size for info in infos) > MAX_EXTRACTED_BYTES:
        raise ArchiveRefusedError(TOO_LARGE_REASON)


def _write_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    budget: int,
) -> int:
    """Stream one member to *destination*, returning the bytes it consumed.

    Streamed in fixed chunks against a running *budget* rather than read whole:
    a member that declared a kilobyte and decompresses to a gigabyte is stopped
    at the cap, having allocated one chunk, instead of after the allocation the
    cap exists to prevent.

    Args:
        archive: The open archive.
        info: The member's record.
        destination: The proven-contained path to write.
        budget: Bytes still allowed across the whole extraction.

    Returns:
        How many bytes this member wrote.

    Raises:
        ArchiveRefusedError: With :data:`TOO_LARGE_REASON` when the member's real
            size pushes the extraction past :data:`MAX_EXTRACTED_BYTES`.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with archive.open(info) as source, destination.open("wb") as sink:
        while True:
            chunk = source.read(_COPY_CHUNK_BYTES)
            if not chunk:
                return written
            written += len(chunk)
            if written > budget:
                raise ArchiveRefusedError(TOO_LARGE_REASON)
            sink.write(chunk)


def _extract_members(raw: bytes, root: Path) -> None:
    """Write every member of the ZIP in *raw* under *root*, or refuse.

    Split from :func:`extract_archive` so the refusal cleanup there wraps one
    call rather than being repeated at each raise site — a cleanup spelled
    five times is one that gets forgotten on the sixth path.

    Args:
        raw: The archive's bytes.
        root: The extraction root, which must already exist.

    Raises:
        ArchiveRefusedError: With one of this module's constants.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            _refuse_declared_excess(infos)
            budget = MAX_EXTRACTED_BYTES
            for info in infos:
                _refuse_unwritable_kind(info)
                destination = _contained_destination(root, info.filename)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                budget -= _write_member(archive, info, destination, budget)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ArchiveRefusedError(UNREADABLE_ARCHIVE_REASON) from exc


def extract_archive(raw: bytes, root: Path) -> None:
    """Unpack the ZIP in *raw* into *root*, refusing anything unsafe.

    *root* must already exist and should be empty: this function creates the
    tree below it but makes no attempt to reconcile with what is already there.

    **A refusal leaves the root empty**, and that is a contract rather than a
    courtesy. Several refusals are only decidable part-way through — a member
    whose name escapes may be the tenth, and a member whose declared size
    under-states its content is only caught by the CRC check :mod:`zipfile`
    runs when the member is closed, by which point its truncated bytes are on
    disk. Handing the caller a refusal over a directory holding a partial,
    silently-corrupt export is how a refused upload becomes content somebody
    later reads. So everything written is removed before the refusal is
    raised, and the caller's own cleanup becomes belt to this braces.

    Args:
        raw: The archive's bytes, already decoded and size-capped by the
            caller's own upload guards.
        root: The staging directory to unpack into.

    Raises:
        ArchiveRefusedError: For a payload that is not a readable ZIP, one over
            either bound, or one holding a member that escapes *root* or is
            not a plain file or directory. The reason is always one of this
            module's constants.
    """
    try:
        _extract_members(raw, root)
    except ArchiveRefusedError:
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        raise


def _probe_json(path: Path) -> object | None:
    """Return *path* parsed as JSON, or ``None`` when it is not readable JSON.

    Args:
        path: A candidate export file.

    Returns:
        The parsed document, or ``None``.
    """
    try:
        parsed: object = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return parsed


def _json_probes(directory: Path) -> list[object]:
    """Return the parsed ``*.json`` files directly inside *directory*.

    Non-recursive and capped at :data:`_MAX_DETECTION_PROBES`, matching the
    ``glob("*.json")`` the ChatGPT and Claude ingestors themselves use: a file
    those ingestors would never discover must not be what decides which of
    them runs.

    Args:
        directory: A candidate export root.

    Returns:
        The documents that parsed, in filename order.
    """
    parsed: list[object] = []
    for path in sorted(directory.glob("*.json"))[:_MAX_DETECTION_PROBES]:
        if not path.is_file():
            continue
        document = _probe_json(path)
        if document is not None:
            parsed.append(document)
    return parsed


def _looks_like_discord(directory: Path) -> bool:
    """Report whether *directory* is a Discord export root.

    Mirrors :meth:`creek.ingest.discord.DiscordIngestor.discover` exactly —
    ``messages/<channel>/messages.json`` — rather than approximating it, so a
    directory this answers ``True`` for is one that ingestor really walks.

    Args:
        directory: A candidate export root.

    Returns:
        ``True`` when at least one channel directory holds ``messages.json``.
    """
    messages = directory / "messages"
    if not messages.is_dir():
        return False
    return any(
        (channel / "messages.json").is_file()
        for channel in messages.iterdir()
        if channel.is_dir()
    )


def _looks_like_substack(directory: Path) -> bool:
    """Report whether *directory* is a Substack export root.

    Wider than :func:`creek.ingest._detection.is_substack_export`, which
    requires ``posts.csv``: current exports often ship none, and #594 already
    taught :class:`~creek.ingest.substack.SubstackIngestor` to derive the
    metadata from ``<post_id>.<slug>.html`` filenames instead. Detection that
    still demanded the sidecar would refuse exactly the exports the ingestor
    was taught to read.

    Args:
        directory: A candidate export root.

    Returns:
        ``True`` for a ``posts.csv``, or for a post-shaped HTML file.
    """
    if (directory / POSTS_CSV_FILENAME).is_file():
        return True
    return any(
        extract_post_id(path.name) is not None
        for path in directory.glob("*.html")
        if path.is_file()
    )


def _looks_like_chatgpt(directory: Path) -> bool:
    """Report whether *directory* holds a ChatGPT ``conversations.json``.

    Stricter than :func:`creek.ingest.chatgpt._is_chatgpt_export`, which asks
    only for a JSON list of dicts, and stricter on purpose: a Discord channel's
    ``messages.json`` is *also* a JSON list of dicts, so the loose rule would
    let a Discord export be identified as a ChatGPT one if detection ever
    reached a channel directory first. Requiring a ``mapping`` key — the
    conversation graph every ChatGPT export carries and nothing else does —
    makes the two families disjoint by construction rather than by search
    order.

    Args:
        directory: A candidate export root.

    Returns:
        ``True`` when a JSON file is a list whose first dict carries
        ``mapping``.
    """
    for document in _json_probes(directory):
        if not isinstance(document, list):
            continue
        if any(
            isinstance(entry, dict) and "mapping" in entry for entry in document[:1]
        ):
            return True
    return False


def _looks_like_claude(directory: Path) -> bool:
    """Report whether *directory* holds a Claude conversation export.

    Restates :meth:`creek.ingest.claude.ClaudeIngestor._is_claude_export`'s
    rule — a top-level dict with ``conversations``, or with ``messages`` plus
    an id — because that method is a private static on the ingestor and
    importing it here would couple detection to the ingestor's internals in
    the direction this module exists to avoid.

    Args:
        directory: A candidate export root.

    Returns:
        ``True`` when a JSON file matches either Claude export shape.
    """
    for document in _json_probes(directory):
        if not isinstance(document, dict):
            continue
        data: dict[str, Any] = document
        if "conversations" in data:
            return True
        if "messages" in data and ("conversation_id" in data or "uuid" in data):
            return True
    return False


_DETECTORS: Final[tuple[tuple[str, Callable[[Path], bool]], ...]] = (
    ("discord", _looks_like_discord),
    ("substack", _looks_like_substack),
    ("chatgpt", _looks_like_chatgpt),
    ("claude", _looks_like_claude),
)
"""The export families, in the order a single directory is tested against them.

Order is contract, not taste. ``discord`` is first because its signature is
structural (a ``messages/<channel>/messages.json`` tree) and therefore the
least likely to be coincidence; ``substack`` next, on a filename shape;
``chatgpt`` and ``claude`` last, since they are decided by parsing a document,
which is the most expensive question and the one most easily satisfied by
accident.
"""


def _candidate_roots(root: Path) -> list[Path]:
    """Return *root* and its descendants, breadth-first, bounded by depth.

    Breadth-first so the **shallowest** match wins. That matters for Discord:
    a channel directory's ``messages.json`` is a list of dicts, and a
    depth-first walk that reached it before the export root would have to be
    saved from misidentifying it by the detector order alone. Answering at the
    export root instead means the tree is named by its layout, which is what
    the layout is for.

    Args:
        root: The extraction root.

    Returns:
        Directories to test, shallowest first, then in name order.
    """
    found = [root]
    frontier = [root]
    for _depth in range(_MAX_DETECTION_DEPTH):
        children = [
            child
            for parent in frontier
            for child in sorted(parent.iterdir())
            if child.is_dir() and not child.is_symlink()
        ]
        if not children:
            break
        found.extend(children)
        frontier = children
    return found


def detect_export_type(root: Path) -> tuple[str, Path] | None:
    """Identify the export under *root* from its contents alone.

    The caller never names the export type. That is the rule
    :func:`creek_mcp.tools.upload.upload_tool` has always stated and this
    function is what lets it survive archives: a caller able to say
    ``source_type="discord"`` over a ChatGPT export would get an ingestor that
    discovers nothing and a response that calls it a success.

    Args:
        root: The extraction root.

    Returns:
        ``(source_type, ingest_root)`` — the
        :data:`creek.ingest.INGESTOR_REGISTRY` key and the directory to hand
        that ingestor, which is often a subdirectory of *root* because exports
        wrap their payload in a folder. ``None`` when nothing matches.
    """
    for directory in _candidate_roots(root):
        for source_type, matches in _DETECTORS:
            if matches(directory):
                return source_type, directory
    return None
