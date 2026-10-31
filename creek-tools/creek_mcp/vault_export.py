"""Build one page of the owner's vault-held content (#2033, adepthood#3579).

The reader behind ``GET /v1/export``, kept free of any web framework so the
paging rules can be tested — and reasoned about — without an app. It walks two
trees, in one fixed order:

1. **Staged documents**: the flat upload staging directory
   (:data:`creek.ingest.journal_staging.UPLOAD_STAGING_RELDIR`), where
   ``creek.upload`` keeps every uploaded document and attachment verbatim.
   Ordered by file name.
2. **Fragments**: every ``*.md`` under
   :data:`creek.vault.reader.CORPUS_SUBDIRS`, ordered by vault-relative POSIX
   path. A file that is not a fragment (an ordinary note) is passed over; a
   file that cannot be read or validated is counted in ``skipped``.

**Bounded memory.** Each page lists *names* only, keeping the smallest
``limit + 1`` past the cursor with :func:`heapq.nsmallest`, and reads the bytes
of just the items it returns. Nothing scales with the corpus but one pass over
directory entries.

**A restart-stable cursor that carries no content.** Items are ordered by the
SHA-256 of their key — a staged document's name, a fragment's vault-relative
path — and the cursor is ``<kind>-<digest>`` of the last item visited. The
order is derived from the disk alone, so a vault that was autostopped between
two pages resumes exactly where it left off. And the cursor names no file: a
staged name embeds the caller's external id and a fragment's file name is
often its title, so a cursor spelling either would put content into every
access log, HTTP client log and proxy that ever sees a URL. Keyset paging also
means an item written behind the cursor mid-walk is not revisited, and one
written ahead of it is picked up.

**Containment.** A staged document must be a regular file, never a link. A
fragment is read only when its fully resolved path lies inside the vault, so a
symlinked file or directory cannot carry content from elsewhere into an export.

**No logging.** Nothing here logs — not a skip, not a path. A staged name embeds
the caller's external id, and a fragment's file name is often its title, so a
path is content. The shared reader's skip diagnostics are avoided for exactly
that reason: :func:`creek.vault.reader.try_validate_fragment` would log a path.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

import frontmatter
from pydantic import ValidationError

from creek._containment import resolves_within
from creek.ingest.journal_staging import UPLOAD_STAGING_RELDIR
from creek.ingest.source_unit import split_source_unit
from creek.models import Fragment, PrivacyTier
from creek.vault.links import read_header_meta
from creek.vault.reader import CORPUS_SUBDIRS, FRONTMATTER_LOAD_ERRORS
from creek_mcp.api.models import (
    EXPORT_CURSOR_PATTERN,
    EXPORT_DOCUMENT_ID_PATTERN,
    OK_STATUS,
    ExportDocument,
    ExportFragment,
    ExportResponse,
    ExportTier,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

CursorKind = Literal["d", "f"]
"""Which tree a cursor points into: staged documents or fragments."""

DOCUMENT_KIND: Final[CursorKind] = "d"
"""The cursor kind of a staged document."""

FRAGMENT_KIND: Final[CursorKind] = "f"
"""The cursor kind of a fragment file."""

_CURSOR_SEPARATOR: Final[str] = "-"
"""Separates a cursor's kind from its digest."""

_CURSOR_RE: Final[re.Pattern[str]] = re.compile(EXPORT_CURSOR_PATTERN)
"""The published cursor pattern, compiled once."""

_DOCUMENT_ID_RE: Final[re.Pattern[str]] = re.compile(EXPORT_DOCUMENT_ID_PATTERN)
"""The published staged-name pattern, compiled once."""

_FRAGMENT_SUFFIX: Final[str] = ".md"
"""Fragment files are markdown."""

_FRAGMENT_TYPE: Final[str] = "fragment"
"""The frontmatter ``type`` that marks a file as a Creek fragment."""

_UPLOAD_STAGING_KEY: Final[str] = UPLOAD_STAGING_RELDIR.as_posix()
"""The staging directory as it appears inside a fragment's ``origin_key``."""


@dataclass(frozen=True, slots=True)
class ExportCursor:
    """A position in the export order.

    Attributes:
        kind: :data:`DOCUMENT_KIND` or :data:`FRAGMENT_KIND`.
        digest: The order key — the SHA-256 hex digest of the item's key — of
            the last item visited.
    """

    kind: CursorKind
    digest: str


def order_key(key: str) -> str:
    """Return the order key of an item's on-disk key.

    Args:
        key: A staged name or a vault-relative fragment path.

    Returns:
        Its SHA-256, as lower-case hex.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def encode_cursor(cursor: ExportCursor) -> str:
    """Return *cursor* as the opaque string a client hands back.

    Args:
        cursor: The position to encode.

    Returns:
        ``<kind>-<digest>``.
    """
    return f"{cursor.kind}{_CURSOR_SEPARATOR}{cursor.digest}"


def decode_cursor(raw: str) -> ExportCursor | None:
    """Return the position *raw* encodes, or ``None`` when it encodes none.

    Args:
        raw: The cursor as the client sent it.

    Returns:
        The decoded position, or ``None`` for anything this server would not
        have minted.
    """
    if not _CURSOR_RE.fullmatch(raw):
        return None
    kind, _separator, digest = raw.partition(_CURSOR_SEPARATOR)
    return ExportCursor(
        kind=DOCUMENT_KIND if kind == DOCUMENT_KIND else FRAGMENT_KIND,
        digest=digest,
    )


@dataclass(slots=True)
class _PageBuilder:
    """Accumulates one page under its two budgets.

    Attributes:
        items_left: How many more items the page may hold.
        bytes_left: How many more raw content bytes the page may carry.
        documents: Documents placed so far.
        fragments: Fragments placed so far.
        skipped: Unreadable fragment files passed over so far.
        last: The last position visited, which the next cursor resumes after.
    """

    items_left: int
    bytes_left: int
    documents: list[ExportDocument] = field(default_factory=list)
    fragments: list[ExportFragment] = field(default_factory=list)
    skipped: int = 0
    last: ExportCursor | None = None

    def fits(self, size: int) -> bool:
        """Return whether *size* more content bytes fit on this page.

        Args:
            size: The candidate item's raw content size.

        Returns:
            ``True`` when it fits in what is left of the byte budget.
        """
        return size <= self.bytes_left

    def spend(self, size: int) -> None:
        """Account for one placed item carrying *size* content bytes.

        Args:
            size: The bytes the item carries; zero when its content is withheld.
        """
        self.items_left -= 1
        self.bytes_left -= size

    def response(self, *, more: bool) -> ExportResponse:
        """Return the finished page.

        Args:
            more: Whether anything remains past :attr:`last`.

        Returns:
            The page, with a cursor only when more remains.
        """
        cursor = encode_cursor(self.last) if more and self.last is not None else None
        return ExportResponse(
            status=OK_STATUS,
            documents=self.documents,
            fragments=self.fragments,
            skipped=self.skipped,
            next_cursor=cursor,
        )


class _PageFullError(Exception):
    """Raised when the next item does not fit; the page ends before it."""


# --------------------------------------------------------------------------
# Listing: names only, the smallest few past the cursor
# --------------------------------------------------------------------------


def _past(key: str, after: str | None) -> tuple[str, str] | None:
    """Return ``(order key, key)`` when *key* sorts past *after*.

    Args:
        key: An item's on-disk key.
        after: The cursor digest to start past, or ``None`` for the start.

    Returns:
        The ordered pair, or ``None`` when the item was already visited.
    """
    digest = order_key(key)
    return (digest, key) if after is None or digest > after else None


def _document_names(staging: Path, after: str | None) -> Iterator[tuple[str, str]]:
    """Yield every staged document past *after*, unordered.

    Args:
        staging: The upload staging directory.
        after: The cursor digest to start past, or ``None`` for the start.

    Yields:
        ``(order key, name)`` of regular, non-link files matching the
        staged-name pattern.
    """
    with os.scandir(staging) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            ordered = _past(entry.name, after)
            if ordered is not None and _DOCUMENT_ID_RE.fullmatch(entry.name):
                yield ordered


def _next_document_names(
    vault: Path, after: str | None, count: int
) -> list[tuple[str, str]]:
    """Return up to *count* staged names past *after*, in order.

    Args:
        vault: The vault root.
        after: The cursor digest to start past, or ``None``.
        count: How many to return at most.

    Returns:
        The *count* earliest ``(order key, name)`` pairs past *after*.
    """
    staging = vault / UPLOAD_STAGING_RELDIR
    if staging.is_symlink() or not staging.is_dir():
        return []
    return heapq.nsmallest(count, _document_names(staging, after))


def _unlistable(error: OSError) -> None:
    """Re-raise a directory listing failure ``os.walk`` would otherwise swallow.

    A subtree that cannot be listed is a vault that cannot be exported whole,
    and a page that quietly omits it would be a short export reported as a
    complete one.

    Args:
        error: The listing failure.

    Raises:
        OSError: Always, *error* itself.
    """
    raise error


def _fragment_keys(vault: Path, after: str | None) -> Iterator[tuple[str, str]]:
    """Yield every corpus markdown file past *after*, unordered.

    Args:
        vault: The vault root.
        after: The cursor digest to start past, or ``None``.

    Yields:
        ``(order key, vault-relative POSIX path)`` of ``*.md`` entries under
        the corpus trees.
    """
    for subdir in CORPUS_SUBDIRS:
        root = vault / subdir
        if root.is_symlink() or not root.is_dir():
            continue
        for folder, _dirs, files in os.walk(
            root, onerror=_unlistable, followlinks=False
        ):
            base = Path(folder).relative_to(vault).as_posix()
            for name in files:
                if not name.endswith(_FRAGMENT_SUFFIX):
                    continue
                ordered = _past(f"{base}/{name}", after)
                if ordered is not None:
                    yield ordered


def _next_fragment_keys(
    vault: Path, after: str | None, count: int
) -> list[tuple[str, str]]:
    """Return up to *count* fragment keys past *after*, in order.

    Args:
        vault: The vault root.
        after: The cursor digest to start past, or ``None``.
        count: How many to return at most.

    Returns:
        The *count* earliest ``(order key, path)`` pairs past *after*.
    """
    return heapq.nsmallest(count, _fragment_keys(vault, after))


# --------------------------------------------------------------------------
# Reading: the bytes of the items a page returns, and nothing else
# --------------------------------------------------------------------------


def _regular_size(path: Path) -> int | None:
    """Return *path*'s size when it is a regular file and not a link.

    Args:
        path: The candidate.

    Returns:
        Its size, or ``None`` when it vanished or is anything but a regular
        file.
    """
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    return status.st_size if stat.S_ISREG(status.st_mode) else None


def _read_bounded(path: Path, cap: int) -> bytes | None:
    """Read *path*, or ``None`` when it vanished or grew past *cap*.

    Args:
        path: The file to read.
        cap: The most bytes to accept.

    Returns:
        The bytes, or ``None``.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(cap + 1)
    except FileNotFoundError:
        return None
    return raw if len(raw) <= cap else None


def _withheld_document(name: str, size: int) -> ExportDocument:
    """Return the listing of a document too large to carry.

    Args:
        name: The staged name.
        size: Its size.

    Returns:
        The document with no content and ``content_included: false``.
    """
    return ExportDocument(
        document_id=name,
        size_bytes=size,
        sha256=None,
        content_base64=None,
        content_included=False,
    )


def _place_document(page: _PageBuilder, staging: Path, name: str, cap: int) -> None:
    """Place one staged document on *page*, or raise when it does not fit.

    Args:
        page: The page being built.
        staging: The upload staging directory.
        name: The staged name.
        cap: The per-item content cap.

    Raises:
        _PageFullError: When the document fits no more of this page's budget.
    """
    path = staging / name
    size = _regular_size(path)
    if size is None:
        return
    if size > cap:
        page.documents.append(_withheld_document(name, size))
        page.spend(0)
        return
    if not page.fits(size):
        raise _PageFullError
    raw = _read_bounded(path, cap)
    if raw is None:
        return
    if not page.fits(len(raw)):
        raise _PageFullError
    page.documents.append(
        ExportDocument(
            document_id=name,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            content_base64=base64.b64encode(raw).decode("ascii"),
            content_included=True,
        )
    )
    page.spend(len(raw))


def _document_of(fragment: Fragment) -> str | None:
    """Return the staged document *fragment* was ingested from, if any.

    Args:
        fragment: The validated fragment.

    Returns:
        The staged name when the fragment's ``origin_key`` points into the
        upload staging directory, else ``None``.
    """
    origin = fragment.source.origin_key
    if origin is None:
        return None
    base, _unit = split_source_unit(origin)
    path = PurePosixPath(base)
    if path.parent.as_posix() != _UPLOAD_STAGING_KEY:
        return None
    return path.name if _DOCUMENT_ID_RE.fullmatch(path.name) else None


def _validated(metadata: dict[str, object]) -> Fragment | Literal[False] | None:
    """Validate parsed frontmatter as a fragment.

    Args:
        metadata: The parsed frontmatter.

    Returns:
        The fragment; ``None`` for a file that does not claim to be one (an
        ordinary note); ``False`` for one that claims to be and is invalid.
    """
    if metadata.get("type") != _FRAGMENT_TYPE:
        return None
    try:
        return Fragment.model_validate(metadata)
    except ValidationError:
        return False


def _exported(fragment: Fragment, markdown: str | None) -> ExportFragment | None:
    """Project *fragment* onto the wire, or ``None`` when it cannot be.

    Args:
        fragment: The validated fragment.
        markdown: The file verbatim, or ``None`` when withheld.

    Returns:
        The wire fragment, or ``None`` when a label falls outside its pattern.
    """
    try:
        return ExportFragment(
            fragment_id=fragment.id,
            tier=ExportTier(PrivacyTier(fragment.privacy_tier).value),
            frequency=str(fragment.frequency.primary),
            phase=str(fragment.wavelength.phase),
            source_platform=str(fragment.source.platform),
            document_id=_document_of(fragment),
            markdown=markdown,
            content_included=markdown is not None,
        )
    except ValidationError:
        return None


def _parsed(raw: bytes) -> tuple[str, dict[str, object]] | None:
    """Decode and parse one fragment file's bytes.

    Args:
        raw: The file's bytes.

    Returns:
        ``(text, frontmatter)``, or ``None`` when it is not UTF-8 or its
        header will not parse.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        post = frontmatter.loads(text)
    except FRONTMATTER_LOAD_ERRORS:
        return None
    return text, dict(post.metadata)


def _record(
    page: _PageBuilder, outcome: ExportFragment | bool | None, size: int
) -> None:
    """Place a fragment outcome on *page*.

    Args:
        page: The page being built.
        outcome: The wire fragment; ``None`` for a plain note (passed over);
            ``False`` for an unreadable or invalid fragment (counted).
        size: The content bytes the fragment carries.
    """
    if isinstance(outcome, ExportFragment):
        page.fragments.append(outcome)
        page.spend(size)
    elif outcome is False:
        page.skipped += 1


def _project(
    validated: Fragment | Literal[False] | None, markdown: str | None
) -> ExportFragment | bool | None:
    """Carry a validation outcome through to the wire.

    Args:
        validated: What :func:`_validated` returned.
        markdown: The file verbatim, or ``None`` when withheld.

    Returns:
        The wire fragment, ``None`` for a plain note, or ``False`` for a
        fragment that could not be validated or projected.
    """
    if validated is None or validated is False:
        return validated
    return _exported(validated, markdown) or False


def _place_fragment(page: _PageBuilder, vault: Path, key: str, cap: int) -> None:
    """Place one fragment on *page*, or raise when it does not fit.

    Args:
        page: The page being built.
        vault: The vault root.
        key: The fragment's vault-relative path.
        cap: The per-item content cap.

    Raises:
        _PageFullError: When the fragment fits no more of this page's budget.
    """
    path = vault / key
    size = _regular_size(path)
    if size is None or not resolves_within(path, Path(os.path.realpath(vault))):
        return
    if size > cap:
        _record(page, _project(_validated(read_header_meta(path)), None), 0)
        return
    if not page.fits(size):
        raise _PageFullError
    raw = _read_bounded(path, cap)
    if raw is None:
        return
    if not page.fits(len(raw)):
        raise _PageFullError
    parsed = _parsed(raw)
    if parsed is None:
        page.skipped += 1
        return
    text, metadata = parsed
    _record(page, _project(_validated(metadata), text), len(raw))


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def _fill_documents(
    page: _PageBuilder, vault: Path, after: str | None, cap: int
) -> bool:
    """Place staged documents past *after* until the page or the tree ends.

    Args:
        page: The page being built.
        vault: The vault root.
        after: The document cursor digest, or ``None`` for the first document.
        cap: The per-item content cap.

    Returns:
        ``True`` when documents may remain unvisited: the page filled, or every
        listed name was visited and the listing was full. A name that placed
        nothing (a file that vanished) still uses a listing slot, so a full
        listing proves nothing about what lies past it.

    Raises:
        _PageFullError: When the next document does not fit.
    """
    wanted = page.items_left + 1
    names = _next_document_names(vault, after, wanted)
    staging = vault / UPLOAD_STAGING_RELDIR
    for digest, name in names:
        if page.items_left == 0:
            return True
        _place_document(page, staging, name, cap)
        page.last = ExportCursor(kind=DOCUMENT_KIND, digest=digest)
    return len(names) == wanted


def _fill_fragments(
    page: _PageBuilder, vault: Path, after: str | None, cap: int
) -> bool:
    """Place fragments past *after* until the page or the corpus ends.

    Args:
        page: The page being built.
        vault: The vault root.
        after: The fragment cursor digest, or ``None`` for the first fragment.
        cap: The per-item content cap.

    Returns:
        ``True`` when fragments may remain unvisited: the page filled, or every
        listed key was visited and the listing was full. A plain note uses a
        listing slot without being placed, so a full listing proves nothing
        about what lies past it.

    Raises:
        _PageFullError: When the next fragment does not fit.
    """
    wanted = page.items_left + 1
    keys = _next_fragment_keys(vault, after, wanted)
    for digest, key in keys:
        if page.items_left == 0:
            return True
        _place_fragment(page, vault, key, cap)
        page.last = ExportCursor(kind=FRAGMENT_KIND, digest=digest)
    return len(keys) == wanted


def _fill(
    page: _PageBuilder, vault: Path, cursor: ExportCursor | None, cap: int
) -> bool:
    """Fill *page* from *cursor* onward.

    Args:
        page: The page being built.
        vault: The vault root.
        cursor: Where the previous page stopped, or ``None``.
        cap: The per-item content cap.

    Returns:
        Whether anything remains past the page.

    Raises:
        _PageFullError: When the next item does not fit.
    """
    fragment_after: str | None = None
    if cursor is None or cursor.kind == DOCUMENT_KIND:
        if _fill_documents(page, vault, cursor.digest if cursor else None, cap):
            return True
    else:
        fragment_after = cursor.digest
    return _fill_fragments(page, vault, fragment_after, cap)


def build_page(
    vault: Path, cursor: ExportCursor | None, *, limit: int, content_bytes: int
) -> ExportResponse:
    """Return the export page that starts after *cursor*.

    Args:
        vault: The vault root.
        cursor: Where the previous page stopped, or ``None`` for the first.
        limit: The most items the page may hold; at least one.
        content_bytes: The most raw content bytes the page may carry, and the
            size above which an item is listed without its content.

    Returns:
        The page. ``next_cursor`` is ``None`` only when nothing remains.

    Raises:
        OSError: When a tree cannot be listed — the vault is unreadable.
    """
    page = _PageBuilder(items_left=limit, bytes_left=content_bytes, last=cursor)
    try:
        more = _fill(page, vault, cursor, content_bytes)
    except _PageFullError:
        more = True
    return page.response(more=more)
