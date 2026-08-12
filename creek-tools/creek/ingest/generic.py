"""Generic/fallback ingestor for unrecognized file formats.

Handles files that are not claimed by any specialized ingestor (Markdown,
ChatGPT, Claude, Discord).  Attempts text reading with multiple encoding
strategies (UTF-8, UTF-16, Latin-1, chardet fallback), skips binary files,
and routes unclassified content to ``01-Fragments/Unsorted/`` with
``source.platform: "other"`` in frontmatter.

Exports:
    GenericIngestor: Concrete ``Ingestor`` subclass for unclaimed files.
    _is_binary_content: Heuristic check for binary file content.
    _try_decode: Multi-strategy text decoding with fallback chain.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import chardet

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    file_modified_time,
    normalize_encoding,
)
from creek.models import SourcePlatform
from creek.time import LA_TZ

logger = logging.getLogger(__name__)

# File extensions claimed by specialized ingestors
# (.md for MarkdownIngestor, .json for ChatGPT/Claude/Discord ingestors)
_CLAIMED_EXTENSIONS: frozenset[str] = frozenset({".md", ".json"})

# Extensions rendered as plain text (no code block wrapping)
_PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".text", ".log"})

# Number of bytes to sample for binary content detection
_BINARY_CHECK_SIZE = 8192

# If more than 10% of bytes are non-text control chars, treat as binary
_BINARY_CONTROL_THRESHOLD = 0.10

# UTF-16 byte-order marks. UTF-16 text is full of null bytes, so it trips
# every binary heuristic here and has to be recognised before they run.
_UTF16_BOMS: tuple[bytes, bytes] = (b"\xff\xfe", b"\xfe\xff")


def _safe_file_mtime(path: Path) -> datetime | None:
    """Return *path*'s mtime as a tz-aware datetime, or ``None`` defensively.

    FEAT-031 (issue #335): the GenericIngestor's authored_at extraction
    chain is filesystem mtime only — the lowest-fidelity honest answer
    when nothing better is available. Synthetic :class:`RawDocument`
    instances in tests (or any future caller that passes a path with no
    file behind it) must not raise; this wrapper swallows
    ``FileNotFoundError`` / ``OSError`` from :func:`Path.stat` and
    surfaces ``None`` instead, matching the FEAT-031 contract that
    ``authored_at`` is optional.

    Args:
        path: The filesystem path to stat.

    Returns:
        A tz-aware UTC datetime when the file exists, else ``None``.
    """
    try:
        return file_modified_time(path)
    except OSError:
        return None


def _is_binary_content(raw_bytes: bytes) -> bool:
    """Determine whether raw bytes represent binary (non-text) content.

    Uses two heuristics:
    1. Presence of null bytes (``\\x00``) — almost always binary.
    2. High ratio of non-text control characters (codes 0-8, 14-31).

    Args:
        raw_bytes: The raw bytes to check.

    Returns:
        ``True`` if the content appears to be binary, ``False`` otherwise.
    """
    if not raw_bytes:
        return False

    if b"\x00" in raw_bytes:
        return True

    sample = raw_bytes[:_BINARY_CHECK_SIZE]
    control_count = sum(1 for byte in sample if byte < 9 or (14 <= byte <= 31))
    return control_count / len(sample) > _BINARY_CONTROL_THRESHOLD


def _try_decode(raw_bytes: bytes) -> str | None:
    """Attempt to decode raw bytes using multiple encoding strategies.

    Tries encodings in order: UTF-8, UTF-16, Latin-1, then chardet
    detection as a final fallback. Returns ``None`` only if all
    strategies fail or the content is detected as binary.

    Args:
        raw_bytes: The raw bytes to decode.

    Returns:
        The decoded text string, or ``None`` if decoding fails.
    """
    if not raw_bytes:
        return ""

    # Try UTF-16 first if BOM is present (contains null bytes so check before binary)
    if raw_bytes[:2] in _UTF16_BOMS:
        with suppress(UnicodeDecodeError, ValueError):
            return raw_bytes.decode("utf-16")

    if _is_binary_content(raw_bytes):
        return None

    # Try UTF-8 first (most common encoding)
    with suppress(UnicodeDecodeError, ValueError):
        return raw_bytes.decode("utf-8")

    # Try chardet detection before falling back to Latin-1
    detected_encoding = chardet.detect(raw_bytes).get("encoding")
    if detected_encoding:
        with suppress(UnicodeDecodeError, ValueError, LookupError):
            return raw_bytes.decode(detected_encoding)

    # Latin-1 as final fallback (can decode any byte sequence)
    return raw_bytes.decode("latin-1")


def _read_unless_binary(file_path: Path) -> bytes | None:
    """Read *file_path* whole, or return ``None`` once a prefix proves it binary.

    ``parse`` has always discarded binary content (``_try_decode``
    returns ``None`` for it), but ``discover`` used to slurp the entire
    file into memory first — so a source tree of photos, videos or Office
    documents was read cover to cover on every ``creek process`` run and
    thrown away. This reads a bounded prefix, decides on that, and only
    continues reading when the content might survive parsing.

    Output-neutral by construction rather than by an extension list.
    :func:`_is_binary_content` looks for a null byte anywhere and
    otherwise scores only the first ``_BINARY_CHECK_SIZE`` bytes, so if
    the prefix tests binary the whole file necessarily does too, and
    ``parse`` would have dropped it. The UTF-16 BOM exemption is applied
    here for the same reason ``_try_decode`` applies it: UTF-16 text is
    riddled with nulls and is not binary.

    Args:
        file_path: File to read.

    Returns:
        The file's bytes, or ``None`` when it is binary and ``parse``
        would have discarded it anyway.
    """
    with file_path.open("rb") as handle:
        prefix = handle.read(_BINARY_CHECK_SIZE)
        if prefix[:2] not in _UTF16_BOMS and _is_binary_content(prefix):
            logger.debug("Skipping binary file without reading it: %s", file_path)
            return None
        return prefix + handle.read()


class GenericIngestor(Ingestor):
    """Fallback ingestor for files not claimed by specialized ingestors.

    Discovers all files whose extensions are not in ``_CLAIMED_EXTENSIONS``,
    attempts multi-encoding text decoding, skips binary files, and routes
    successfully parsed content to ``01-Fragments/Unsorted/`` with
    ``source.platform: "other"``.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find files not claimed by any specialized ingestor.

        Recursively walks ``source_path``, skipping files with extensions
        in ``_CLAIMED_EXTENSIONS`` (e.g. ``.md``, ``.json``).

        Args:
            source_path: The directory to search.

        Returns:
            A list of ``RawDocument`` objects for unclaimed files.
        """
        if not source_path.exists():
            return []

        if source_path.is_file():
            return self._discover_single_file(source_path)

        return self._discover_directory(source_path)

    def _discover_single_file(self, file_path: Path) -> list[RawDocument]:
        """Check and read a single file if not claimed by a specialized ingestor.

        Args:
            file_path: Path to the file.

        Returns:
            A single-element list if the file is unclaimed and not
            binary, else empty.
        """
        if file_path.suffix.lower() in _CLAIMED_EXTENSIONS:
            return []

        raw_bytes = _read_unless_binary(file_path)
        if raw_bytes is None:
            return []
        _text, encoding = normalize_encoding(raw_bytes)
        return [
            RawDocument(
                path=file_path,
                content=raw_bytes,
                metadata={"source_type": "generic"},
                detected_encoding=encoding,
            )
        ]

    def _discover_directory(self, dir_path: Path) -> list[RawDocument]:
        """Recursively discover unclaimed files in a directory.

        Args:
            dir_path: Directory path to search.

        Returns:
            A list of ``RawDocument`` objects for unclaimed, non-binary
            files.
        """
        docs: list[RawDocument] = []
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in _CLAIMED_EXTENSIONS:
                continue

            raw_bytes = _read_unless_binary(file_path)
            if raw_bytes is None:
                continue
            _text, encoding = normalize_encoding(raw_bytes)
            docs.append(
                RawDocument(
                    path=file_path,
                    content=raw_bytes,
                    metadata={"source_type": "generic"},
                    detected_encoding=encoding,
                )
            )
        return docs

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse a raw document, skipping binary files.

        Attempts multi-encoding text decoding via ``_try_decode``.
        Binary files and empty files are skipped (return empty list).

        FEAT-031 (issue #335): populates ``metadata['authored_at']``
        from the file's mtime — the lowest-fidelity honest answer the
        generic fallback can offer. Defensive against synthetic
        :class:`RawDocument` instances whose ``path`` does not exist
        on disk: those produce ``authored_at = None`` rather than
        raising.

        Issue #911 — re-ingest identity contract. The mtime is stamped on
        ``timestamp`` as well, because
        :func:`creek.ingest.base.generate_fragment_id` hashes
        ``timestamp.isoformat()``: keying it on ``datetime.now()`` made every
        re-ingest of an *unchanged* file mint a fresh id and write a duplicate
        fragment. Derived from the mtime, an unchanged file re-ingests to the
        same deterministic id and the write is a no-op.

        The mtime is used verbatim in UTC — never converted to
        :data:`~creek.ingest.base.LA_TZ` — because the UTC offset string is
        part of the hashed input. ``datetime.fromtimestamp(st_mtime, tz=UTC)``
        is a pure function of the epoch float, so identity does not vary with
        the host's tzdata, ``TZ`` env var, or DST state; rendering in LA would
        bake ``-07:00``/``-08:00`` into the hash.

        Known limitation: the generic source has no ingest ledger, so a bare
        ``touch`` (or any edit) bumps the mtime and therefore mints a new
        fragment rather than updating the existing one. Ledger-backed
        update-in-place is tracked in issue #953.

        The wall clock survives only as the fallback for the pathless /
        synthetic case where ``stat()`` fails: there is no mtime to key on, so
        ``timestamp`` is ``now`` and ``authored_at`` stays honestly ``None``.

        Args:
            raw: The raw document to parse.

        Returns:
            A single-element list with the parsed fragment, or empty
            if the file is binary or empty.
        """
        if not raw.content:
            return []

        text = _try_decode(raw.content)
        if text is None:
            logger.info("Skipping binary file: %s", raw.path)
            return []

        if not text.strip():
            return []

        # One stat, two uses: the same mtime is both the honest authored_at and
        # the identity-bearing timestamp (issue #911). Kept in UTC verbatim.
        authored_at = _safe_file_mtime(raw.path)
        timestamp = authored_at if authored_at is not None else datetime.now(tz=LA_TZ)
        return [
            ParsedFragment(
                content=text,
                metadata={
                    "file_extension": raw.path.suffix.lower(),
                    "source_type": "generic",
                    "authored_at": authored_at,
                },
                source_path=str(raw.path),
                timestamp=timestamp,
            )
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to Markdown.

        Plain text files (``.txt``, ``.text``, ``.log``) are rendered
        as-is. Other file types are wrapped in a fenced code block
        with the file extension as the language hint.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string.
        """
        ext = fragment.metadata.get("file_extension", "")
        if ext in _PLAIN_TEXT_EXTENSIONS:
            return fragment.content

        lang = ext.lstrip(".") if ext else ""
        return f"```{lang}\n{fragment.content}\n```"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter for an unclaimed file.

        Sets ``source.platform`` to :data:`~creek.models.SourcePlatform.OTHER`
        (``"other"``) and routes to ``01-Fragments/Unsorted/``. Issue #911:
        this previously emitted ``"unknown"``, which is not a
        :class:`~creek.models.SourcePlatform` member at all, so
        ``assemble_ingested_fragment`` raised a pydantic ``ValidationError``
        that ``run_ingest`` swallowed into ``result.errors`` — generic
        fragments never reached the vault. ``OTHER`` is the enum's fallback
        member and maps to the same ``Unsorted`` subfolder, so the routing
        target is unchanged.

        FEAT-031 (issue #335): emits ``authored_at`` as an ISO string
        when the parse stage captured the file's mtime; omits the key
        when the metadata value is missing or ``None`` (honest absence).

        ``created`` is the parse-stage ``timestamp``, i.e. the file's mtime in
        UTC since #911. That is deliberate: :mod:`creek.time` documents that
        ``created`` is not part of the authored-at precedence chain (all
        time-bucketing routes through ``effective_authored_at``), and the other
        file-based ingestors already key on mtime.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        title = Path(fragment.source_path).stem
        frontmatter_dict: dict[str, Any] = {
            "type": "fragment",
            "title": title,
            "source": {
                "platform": SourcePlatform.OTHER,
                "original_file": fragment.source_path,
            },
            "created": fragment.timestamp.isoformat(),
            "routing": "01-Fragments/Unsorted/",
        }
        authored_at: datetime | None = fragment.metadata.get("authored_at")
        if authored_at is not None:
            frontmatter_dict["authored_at"] = authored_at.isoformat()
        return frontmatter_dict
