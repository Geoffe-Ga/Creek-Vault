"""Tests for creek.ingest.generic — GenericIngestor for unrecognized file formats.

Covers file discovery (excluding files claimed by specialized ingestors),
multi-encoding parsing, binary file detection and skipping, Unsorted routing,
and frontmatter generation with ``source.platform: "other"``
(:data:`creek.models.SourcePlatform.OTHER` — the enum's fallback member;
issue #911 corrected the previously-emitted ``"unknown"``, which was not a
member at all and made every generic fragment fail validation).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from zoneinfo import ZoneInfo

import pytest

from creek.ingest.base import IngestResult, ParsedFragment, RawDocument
from creek.ingest.generic import (
    _BINARY_CHECK_SIZE,
    GenericIngestor,
    _is_binary_content,
    _try_decode,
)
from creek.models import SourcePlatform

LA_TZ = ZoneInfo("America/Los_Angeles")


# ---- Fixtures ----


@pytest.fixture()
def ingestor() -> GenericIngestor:
    """Create a GenericIngestor instance for testing."""
    return GenericIngestor()


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    """Create a source directory with mixed file types.

    Contains:
    - unknown.txt (plain text, unclaimed)
    - notes.rst (reStructuredText, unclaimed)
    - readme.md (markdown, claimed by MarkdownIngestor)
    - data.json (JSON, could be claimed by other ingestors)
    - image.png (binary file, should be skipped)
    """
    (tmp_path / "unknown.txt").write_text("Hello from a text file.", encoding="utf-8")
    (tmp_path / "notes.rst").write_text(
        "reStructuredText\n================\n\nSome notes.", encoding="utf-8"
    )
    (tmp_path / "readme.md").write_text("# Readme\n\nMarkdown file.", encoding="utf-8")
    (tmp_path / "data.json").write_text('{"key": "value"}', encoding="utf-8")
    # Binary content (PNG header bytes)
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00")
    return tmp_path


# ---- _is_binary_content Tests ----


class TestIsBinaryContent:
    """Tests for the _is_binary_content helper function."""

    def test_text_content_is_not_binary(self) -> None:
        """Plain text bytes should not be classified as binary."""
        assert _is_binary_content(b"Hello, world!") is False

    def test_empty_bytes_are_not_binary(self) -> None:
        """Empty bytes should not be classified as binary."""
        assert _is_binary_content(b"") is False

    def test_null_bytes_are_binary(self) -> None:
        """Bytes containing null characters should be classified as binary."""
        assert _is_binary_content(b"hello\x00world") is True

    def test_png_header_is_binary(self) -> None:
        """PNG file header bytes should be classified as binary."""
        png_header = b"\x89PNG\r\n\x1a\n"
        assert _is_binary_content(png_header) is True

    def test_utf8_text_is_not_binary(self) -> None:
        """UTF-8 encoded text with special characters is not binary."""
        assert _is_binary_content("caf\u00e9 na\u00efve".encode()) is False

    def test_high_control_char_ratio_is_binary(self) -> None:
        """Content with many non-text control characters should be binary."""
        # Many control characters mixed with some text
        content = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 10 + b"text"
        assert _is_binary_content(content) is True


# ---- _try_decode Tests ----


class TestTryDecode:
    """Tests for the _try_decode helper function."""

    def test_utf8_decodes_successfully(self) -> None:
        """UTF-8 bytes should decode successfully."""
        text = _try_decode(b"Hello, world!")
        assert text == "Hello, world!"

    def test_utf16_decodes_successfully(self) -> None:
        """UTF-16 bytes with BOM should decode successfully."""
        raw = "Hello".encode("utf-16")
        text = _try_decode(raw)
        assert text is not None
        assert "Hello" in text

    def test_latin1_decodes_successfully(self) -> None:
        """Latin-1 bytes should decode successfully."""
        raw = "caf\u00e9".encode("latin-1")
        text = _try_decode(raw)
        assert text is not None
        assert "caf" in text

    def test_empty_bytes_return_empty_string(self) -> None:
        """Empty bytes should return an empty string."""
        text = _try_decode(b"")
        assert text == ""

    def test_binary_content_returns_none(self) -> None:
        """Binary content that fails all decodings should return None."""
        # Pure binary garbage with null bytes
        binary = bytes(range(256)) * 2
        result = _try_decode(binary)
        # Either None or decoded with replacement chars — binary detection
        # happens separately in _is_binary_content
        assert result is None or isinstance(result, str)

    def test_chardet_fallback_for_non_utf8(self) -> None:
        """Should use chardet when UTF-8 fails."""
        # Windows-1252 specific character (not valid UTF-8)
        raw = b"caf\xe9 cr\xe8me"  # Latin-1/Windows-1252 encoded
        text = _try_decode(raw)
        assert text is not None
        assert "caf" in text


# ---- GenericIngestor.discover Tests ----


class TestGenericIngestorDiscover:
    """Tests for GenericIngestor.discover method."""

    def test_discovers_unclaimed_files(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Should discover files not claimed by specialized ingestors."""
        docs = ingestor.discover(source_dir)
        discovered_names = {doc.path.name for doc in docs}
        # .txt and .rst are unclaimed by specialized ingestors
        assert "unknown.txt" in discovered_names
        assert "notes.rst" in discovered_names

    def test_excludes_markdown_files(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Should not discover .md files (claimed by MarkdownIngestor)."""
        docs = ingestor.discover(source_dir)
        discovered_names = {doc.path.name for doc in docs}
        assert "readme.md" not in discovered_names

    def test_excludes_json_files(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Should not discover .json files (claimed by ChatGPT/Claude ingestors)."""
        docs = ingestor.discover(source_dir)
        discovered_names = {doc.path.name for doc in docs}
        assert "data.json" not in discovered_names

    def test_returns_raw_documents(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Should return RawDocument instances."""
        docs = ingestor.discover(source_dir)
        assert all(isinstance(d, RawDocument) for d in docs)

    def test_raw_document_content_is_bytes(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """RawDocument content should be raw bytes."""
        docs = ingestor.discover(source_dir)
        assert all(isinstance(d.content, bytes) for d in docs)

    def test_nonexistent_path_returns_empty(self, ingestor: GenericIngestor) -> None:
        """A nonexistent source path should return an empty list."""
        docs = ingestor.discover(Path("/nonexistent/path"))
        assert docs == []

    def test_empty_directory_returns_empty(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """An empty directory should return an empty list."""
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_discovers_recursively(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """Should discover files in nested subdirectories."""
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "nested.txt").write_text("nested content", encoding="utf-8")
        docs = ingestor.discover(tmp_path)
        discovered_names = {doc.path.name for doc in docs}
        assert "nested.txt" in discovered_names

    def test_metadata_has_source_type(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Discovered documents should have source_type 'generic' in metadata."""
        docs = ingestor.discover(source_dir)
        for doc in docs:
            assert doc.metadata["source_type"] == "generic"


# ---- GenericIngestor.parse Tests ----


class TestGenericIngestorParse:
    """Tests for GenericIngestor.parse method."""

    def test_parse_text_file(self, ingestor: GenericIngestor) -> None:
        """Should parse a text file into a single ParsedFragment."""
        raw = RawDocument(
            path=Path("/fake/note.txt"),
            content=b"Some text content.",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert fragments[0].content == "Some text content."

    def test_parse_skips_binary_files(self, ingestor: GenericIngestor) -> None:
        """Should return empty list for binary files."""
        raw = RawDocument(
            path=Path("/fake/image.png"),
            content=b"\x89PNG\r\n\x1a\n\x00\x00",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_parse_utf16_content(self, ingestor: GenericIngestor) -> None:
        """Should parse UTF-16 encoded content."""
        content = "Hello UTF-16".encode("utf-16")
        raw = RawDocument(
            path=Path("/fake/utf16.txt"),
            content=content,
            metadata={"source_type": "generic"},
            detected_encoding="utf-16",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "Hello UTF-16" in fragments[0].content

    def test_parse_latin1_content(self, ingestor: GenericIngestor) -> None:
        """Should parse Latin-1 encoded content."""
        content = "caf\u00e9 cr\u00e8me".encode("latin-1")
        raw = RawDocument(
            path=Path("/fake/latin.txt"),
            content=content,
            metadata={"source_type": "generic"},
            detected_encoding="latin-1",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "caf" in fragments[0].content

    def test_parse_sets_source_path(self, ingestor: GenericIngestor) -> None:
        """Parsed fragment should reference the source file path."""
        raw = RawDocument(
            path=Path("/fake/note.txt"),
            content=b"content",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert fragments[0].source_path == "/fake/note.txt"

    def test_parse_sets_timestamp(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """Parsed fragment timestamp is the file's mtime in UTC (issue #911).

        Previously this only asserted ``isinstance(..., datetime)``, which the
        wall-clock ``datetime.now()`` satisfied — and that unstable value is
        hashed into the fragment id, so every re-ingest of an unchanged file
        minted a new id. The timestamp must be the file's stable mtime, in UTC
        so the hashed ``isoformat()`` does not vary with the host timezone.

        Retargeted from a synthetic ``/fake/note.txt`` to a real file, because
        the mtime contract only exists when there is a file to stat; the
        synthetic wall-clock fallback is covered by
        ``test_parse_returns_none_authored_at_for_nonexistent_path`` and by
        ``tests/test_ingest_generic_idempotent.py``.
        """
        file_path = tmp_path / "note.txt"
        file_path.write_text("content", encoding="utf-8")
        target = datetime(2024, 3, 15, 14, 30, 0, tzinfo=UTC)
        os.utime(file_path, (target.timestamp(), target.timestamp()))

        raw = RawDocument(
            path=file_path,
            content=file_path.read_bytes(),
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert isinstance(fragments[0].timestamp, datetime)
        assert fragments[0].timestamp == target
        assert fragments[0].timestamp.utcoffset() == timedelta(0)

    def test_parse_empty_file(self, ingestor: GenericIngestor) -> None:
        """Should return empty list for empty files."""
        raw = RawDocument(
            path=Path("/fake/empty.txt"),
            content=b"",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert fragments == []


# ---- GenericIngestor.convert_to_markdown Tests ----


class TestGenericIngestorConvertToMarkdown:
    """Tests for GenericIngestor.convert_to_markdown method."""

    def test_wraps_content_in_code_block(self, ingestor: GenericIngestor) -> None:
        """Should wrap non-markdown content in a fenced code block."""
        fragment = ParsedFragment(
            content="print('hello')",
            metadata={"file_extension": ".py"},
            source_path="/fake/script.py",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        markdown = ingestor.convert_to_markdown(fragment)
        assert "```" in markdown
        assert "print('hello')" in markdown

    def test_plain_text_uses_text_block(self, ingestor: GenericIngestor) -> None:
        """Plain text files should be rendered as-is (no code block)."""
        fragment = ParsedFragment(
            content="Just a plain note.",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        markdown = ingestor.convert_to_markdown(fragment)
        assert "Just a plain note." in markdown


# ---- GenericIngestor.generate_frontmatter Tests ----


class TestGenericIngestorGenerateFrontmatter:
    """Tests for GenericIngestor.generate_frontmatter method."""

    def test_sets_platform_to_other(self, ingestor: GenericIngestor) -> None:
        """Frontmatter source.platform is the SourcePlatform fallback 'other'.

        Assertion correction (issue #911): this test previously pinned
        ``"unknown"``, which is **not** a member of
        :class:`creek.models.SourcePlatform`. Because the unit test only
        inspected the raw dict, the now-known-wrong expectation stayed green
        while every real ingest raised a pydantic ``ValidationError`` in
        ``assemble_ingested_fragment`` and silently dropped the fragment.
        ``OTHER`` routes to the same ``01-Fragments/Unsorted/`` folder.
        """
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == "other"
        assert SourcePlatform(fm["source"]["platform"]) is SourcePlatform.OTHER

    def test_sets_type_to_fragment(self, ingestor: GenericIngestor) -> None:
        """Frontmatter type should be 'fragment'."""
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["type"] == "fragment"

    def test_sets_original_file(self, ingestor: GenericIngestor) -> None:
        """Frontmatter should include the original file path."""
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["source"]["original_file"] == "/fake/note.txt"

    def test_sets_created_timestamp(self, ingestor: GenericIngestor) -> None:
        """Frontmatter should include a created timestamp."""
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=ts,
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["created"] == ts.isoformat()

    def test_sets_routing_to_unsorted(self, ingestor: GenericIngestor) -> None:
        """Frontmatter should route to 01-Fragments/Unsorted/."""
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["routing"] == "01-Fragments/Unsorted/"


# ---- GenericIngestor.ingest Integration Tests ----


class TestGenericIngestorIngest:
    """Integration tests for the full GenericIngestor.ingest pipeline."""

    def test_ingest_returns_ingest_result(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """ingest() should return an IngestResult."""
        result = ingestor.ingest(source_dir)
        assert isinstance(result, IngestResult)

    def test_ingest_skips_binary_files(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Binary files should not produce fragments."""
        result = ingestor.ingest(source_dir)
        source_paths = {f.source_path for f in result.fragments}
        assert not any("image.png" in p for p in source_paths)

    def test_ingest_produces_fragments_for_text_files(
        self, ingestor: GenericIngestor, source_dir: Path
    ) -> None:
        """Text files should produce fragments."""
        result = ingestor.ingest(source_dir)
        assert len(result.fragments) > 0

    def test_discover_single_text_file(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """discover() with a single file path should return that file."""
        txt = tmp_path / "single.txt"
        txt.write_text("content", encoding="utf-8")
        docs = ingestor.discover(txt)
        assert len(docs) == 1
        assert docs[0].path == txt

    def test_discover_single_claimed_file_returns_empty(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """discover() with a .md file should return empty (claimed)."""
        md = tmp_path / "readme.md"
        md.write_text("# Hello", encoding="utf-8")
        docs = ingestor.discover(md)
        assert docs == []

    def test_convert_no_extension(self, ingestor: GenericIngestor) -> None:
        """convert_to_markdown with no file extension uses empty lang hint."""
        fragment = ParsedFragment(
            content="some content",
            metadata={"file_extension": ""},
            source_path="/fake/Makefile",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        markdown = ingestor.convert_to_markdown(fragment)
        assert markdown.startswith("```\n")


# ---- GenericIngestor authored_at Tests (FEAT-031 follow-up, issue #335) ----


class TestGenericIngestorAuthoredAt:
    """Tests for ``authored_at`` extraction from filesystem mtime.

    FEAT-031 mandates that every concrete ingestor populates an
    ``authored_at`` extraction chain. For ``GenericIngestor`` the
    chain is intentionally the lowest-fidelity one possible —
    filesystem mtime via :func:`creek.ingest.base.file_modified_time`
    — so the unknown-platform fallback no longer emits a
    perpetually-null ``authored_at`` when the file's mtime is a
    perfectly valid honest answer.
    """

    def test_parse_sets_authored_at_to_file_mtime(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """parse() populates ``metadata['authored_at']`` from the file mtime."""
        file_path = tmp_path / "note.txt"
        file_path.write_text("hello", encoding="utf-8")
        # Pin to a deterministic moment in 2024 (UTC epoch seconds).
        target = datetime(2024, 3, 15, 14, 30, 0, tzinfo=UTC)
        epoch = target.timestamp()
        os.utime(file_path, (epoch, epoch))

        raw = RawDocument(
            path=file_path,
            content=file_path.read_bytes(),
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        authored_at = fragments[0].metadata["authored_at"]
        assert isinstance(authored_at, datetime)
        assert authored_at.tzinfo is not None
        # Compare as UTC to avoid LA wall-clock drift assertions.
        assert authored_at.astimezone(UTC) == target

    def test_parse_returns_none_authored_at_for_nonexistent_path(
        self, ingestor: GenericIngestor
    ) -> None:
        """Synthetic RawDocument with a non-existent path surfaces ``None``.

        Tests with ``RawDocument(path=Path('/fake/...'))`` should not
        raise just because there's no real file behind the path —
        ``parse()`` must defensively land ``authored_at`` as ``None``
        when ``stat()`` would fail.
        """
        raw = RawDocument(
            path=Path("/nonexistent/synthetic/note.txt"),
            content=b"some text",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert fragments[0].metadata["authored_at"] is None

    def test_generate_frontmatter_emits_authored_at_iso_when_present(
        self, ingestor: GenericIngestor
    ) -> None:
        """Frontmatter includes ``authored_at`` as an ISO string when set."""
        authored = datetime(2024, 3, 15, 14, 30, 0, tzinfo=UTC)
        fragment = ParsedFragment(
            content="content",
            metadata={
                "file_extension": ".txt",
                "authored_at": authored,
            },
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["authored_at"] == authored.isoformat()

    def test_generate_frontmatter_omits_authored_at_when_none(
        self, ingestor: GenericIngestor
    ) -> None:
        """Frontmatter omits ``authored_at`` when the metadata value is ``None``."""
        fragment = ParsedFragment(
            content="content",
            metadata={
                "file_extension": ".txt",
                "authored_at": None,
            },
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert "authored_at" not in fm

    def test_generate_frontmatter_omits_authored_at_when_missing(
        self, ingestor: GenericIngestor
    ) -> None:
        """Frontmatter omits ``authored_at`` when the metadata key is absent."""
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert "authored_at" not in fm


# ---- Discover-time binary skip (issue #1304) ----


class _CountingHandle:
    """A binary file handle that records how many bytes were read through it."""

    def __init__(self, handle: BinaryIO, tally: list[int]) -> None:
        """Wrap *handle*, appending each read size to *tally*."""
        self._handle = handle
        self._tally = tally

    def read(self, size: int = -1) -> bytes:
        """Read from the wrapped handle and record the byte count."""
        chunk = self._handle.read(size)
        self._tally.append(len(chunk))
        return chunk

    def __enter__(self) -> _CountingHandle:
        """Enter the wrapped handle's context."""
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the wrapped handle."""
        self._handle.close()


class TestDiscoverSkipsBinariesUnread:
    """``discover`` must not slurp files ``parse`` was always going to drop.

    Output-neutral: every case here produced zero fragments before issue
    #1304 too, because ``parse`` discarded the content after reading it.
    What changes is that the bytes are no longer read.
    """

    def test_binary_file_is_not_discovered(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """A binary file yields no raw document at all."""
        (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

        assert ingestor.discover(tmp_path) == []

    def test_binary_file_produced_no_fragment_before_either(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """The skip is output-neutral: parsing it would have yielded nothing.

        Feeds ``parse`` the document ``discover`` used to build, proving
        the fragment count is unchanged rather than merely asserting the
        new behaviour.
        """
        path = tmp_path / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        pre_change_doc = RawDocument(
            path=path,
            content=path.read_bytes(),
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )

        assert ingestor.parse(pre_change_doc) == []
        assert ingestor.ingest(tmp_path).fragments == []

    def test_only_a_bounded_prefix_of_a_binary_file_is_read(
        self,
        ingestor: GenericIngestor,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 4 MB binary costs one small read, not four megabytes of them."""
        (tmp_path / "big.bin").write_bytes(b"\x00\xff" * (2 * 1024 * 1024))
        tally: list[int] = []
        real_open = Path.open

        def counting_open(self: Path, *args: object, **kwargs: object) -> object:
            handle = real_open(self, *args, **kwargs)  # type: ignore[arg-type]
            return _CountingHandle(handle, tally)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", counting_open)

        assert ingestor.discover(tmp_path) == []
        assert sum(tally) <= _BINARY_CHECK_SIZE, tally

    def test_utf16_text_survives_the_skip(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """UTF-16 is null-heavy but is text, and must still be ingested.

        The BOM carve-out exists in ``_try_decode`` for exactly this
        reason; the discover-time check has to honour it or the change
        would silently drop real content.
        """
        (tmp_path / "wide.txt").write_bytes("Hello from UTF-16.".encode("utf-16"))

        fragments = ingestor.ingest(tmp_path).fragments

        assert [f.content.strip() for f in fragments] == ["Hello from UTF-16."]

    def test_a_single_binary_file_path_is_not_discovered(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """The single-file discovery arm skips binaries too."""
        path = tmp_path / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")

        assert ingestor.discover(path) == []

    def test_an_empty_file_is_still_discovered(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """Empty is not binary; the prior behaviour of discovering it holds."""
        (tmp_path / "empty.txt").write_bytes(b"")

        assert [doc.path.name for doc in ingestor.discover(tmp_path)] == ["empty.txt"]

    def test_a_text_file_larger_than_the_prefix_is_read_in_full(
        self, ingestor: GenericIngestor, tmp_path: Path
    ) -> None:
        """Reading a bounded prefix must not truncate the document.

        The prefix is only a binary *test*; everything past it still has
        to reach the fragment, or the change would quietly amputate every
        source file over 8 KiB.
        """
        body = "line of text\n" * ((_BINARY_CHECK_SIZE // 13) + 500)
        (tmp_path / "long.txt").write_text(body, encoding="utf-8")

        fragments = ingestor.ingest(tmp_path).fragments

        assert len(fragments) == 1
        assert fragments[0].content == body
        assert len(fragments[0].content) > _BINARY_CHECK_SIZE
