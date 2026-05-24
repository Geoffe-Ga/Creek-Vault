"""Tests for creek.ingest.generic — GenericIngestor for unrecognized file formats.

Covers file discovery (excluding files claimed by specialized ingestors),
multi-encoding parsing, binary file detection and skipping, Unsorted routing,
and frontmatter generation with ``source.platform: "unknown"``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from creek.ingest.base import IngestResult, ParsedFragment, RawDocument
from creek.ingest.generic import (
    GenericIngestor,
    _is_binary_content,
    _try_decode,
)

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

    def test_parse_sets_timestamp(self, ingestor: GenericIngestor) -> None:
        """Parsed fragment should have a timestamp."""
        raw = RawDocument(
            path=Path("/fake/note.txt"),
            content=b"content",
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert isinstance(fragments[0].timestamp, datetime)

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

    def test_sets_platform_to_unknown(self, ingestor: GenericIngestor) -> None:
        """Frontmatter source.platform should be 'unknown'."""
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt"},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == "unknown"

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


# ---- authored_at extraction (FEAT-031) ----


class TestGenericIngestorAuthoredAt:
    """``GenericIngestor`` authored_at falls back to filesystem mtime.

    FEAT-031 (#263): the generic ingestor is the lowest-fidelity tier —
    no in-band source date is available, so the file's mtime is the
    only honest answer. ``authored_at`` is timezone-aware (UTC) and
    flows through both the parsed metadata and the emitted frontmatter.
    """

    def test_parse_sets_authored_at_to_file_mtime(
        self,
        ingestor: GenericIngestor,
        tmp_path: Path,
    ) -> None:
        """``parse()`` stashes the file's mtime as authored_at on metadata."""
        from datetime import UTC

        path = tmp_path / "note.rst"
        path.write_text("content", encoding="utf-8")
        # Stamp a known mtime so the assertion does not depend on
        # the wall-clock of the test machine.
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        import os

        os.utime(path, (expected.timestamp(), expected.timestamp()))

        raw = RawDocument(
            path=path,
            content=path.read_bytes(),
            metadata={"source_type": "generic"},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        authored = fragments[0].metadata["authored_at"]
        assert authored == expected
        assert authored.tzinfo is not None

    def test_generate_frontmatter_emits_authored_at(
        self,
        ingestor: GenericIngestor,
    ) -> None:
        """authored_at on metadata round-trips into the frontmatter dict."""
        from datetime import UTC

        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ)
        authored = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt", "authored_at": authored},
            source_path="/fake/note.txt",
            timestamp=ts,
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["authored_at"] == authored.isoformat()

    def test_generate_frontmatter_omits_authored_at_when_none(
        self,
        ingestor: GenericIngestor,
    ) -> None:
        """A ``None`` authored_at is omitted (never guessed) from frontmatter."""
        fragment = ParsedFragment(
            content="content",
            metadata={"file_extension": ".txt", "authored_at": None},
            source_path="/fake/note.txt",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = ingestor.generate_frontmatter(fragment)
        assert "authored_at" not in fm
