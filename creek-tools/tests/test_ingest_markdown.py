"""Tests for creek.ingest.markdown — Markdown file ingestor.

Covers discovery, parsing (with and without frontmatter), content normalization,
frontmatter generation with merge strategy, document type detection,
platform inference, edge cases (empty files, merge conflicts), and
INGESTOR_REGISTRY registration.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from creek.ingest.base import IngestResult, ParsedFragment, RawDocument
from creek.ingest.markdown import (
    MarkdownIngestor,
    _detect_document_type,
    _infer_platform,
    _merge_frontmatter,
)
from creek.models import SourcePlatform

LA_TZ = ZoneInfo("America/Los_Angeles")


# ---- Fixtures ----


@pytest.fixture()
def md_ingestor() -> MarkdownIngestor:
    """Create a MarkdownIngestor instance for testing."""
    return MarkdownIngestor()


@pytest.fixture()
def tmp_md_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with sample markdown files.

    Returns:
        Path to the temporary directory.
    """
    # File with frontmatter
    with_fm = tmp_path / "with_frontmatter.md"
    with_fm.write_text(
        "---\n"
        "title: Existing Title\n"
        "tags:\n"
        "  - python\n"
        "  - testing\n"
        "---\n"
        "\n"
        "# Existing Title\n"
        "\n"
        "Some content about Python testing.\n",
        encoding="utf-8",
    )

    # File without frontmatter
    without_fm = tmp_path / "without_frontmatter.md"
    without_fm.write_text(
        "# My Notes\n\nSome plain markdown content.\n",
        encoding="utf-8",
    )

    # Nested file
    subdir = tmp_path / "sub"
    subdir.mkdir()
    nested = subdir / "nested.md"
    nested.write_text("# Nested\n\nNested content.\n", encoding="utf-8")

    # Empty file
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")

    # Non-markdown file (should be ignored)
    txt = tmp_path / "not_markdown.txt"
    txt.write_text("This is not markdown.", encoding="utf-8")

    return tmp_path


@pytest.fixture()
def journal_md(tmp_path: Path) -> Path:
    """Create a journal-style markdown file.

    Returns:
        Path to the temporary directory.
    """
    journal = tmp_path / "journal.md"
    journal.write_text(
        "# 2024-01-15\n\n"
        "Today I reflected on my progress.\n"
        "Dear diary, things are going well.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def essay_md(tmp_path: Path) -> Path:
    """Create an essay-style markdown file.

    Returns:
        Path to the temporary directory.
    """
    essay = tmp_path / "essay.md"
    essay.write_text(
        "# The Nature of Knowledge\n\n"
        "## Introduction\n\n"
        "In this essay, I argue that knowledge is best understood as a "
        "network of interconnected fragments.\n\n"
        "## Thesis\n\n"
        "My thesis is that systematic organization leads to deeper insights.\n\n"
        "## Conclusion\n\n"
        "Therefore, we should embrace structured thinking.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def technical_md(tmp_path: Path) -> Path:
    """Create a technical-style markdown file.

    Returns:
        Path to the temporary directory.
    """
    tech = tmp_path / "technical.md"
    tech.write_text(
        "# API Reference\n\n"
        "```python\ndef foo() -> None:\n    pass\n```\n\n"
        "## Configuration\n\n"
        "```yaml\nkey: value\n```\n",
        encoding="utf-8",
    )
    return tmp_path


# ---- Discovery Tests ----


class TestMarkdownIngestorDiscover:
    """Tests for MarkdownIngestor.discover()."""

    def test_discovers_md_files(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should discover all .md files in the source directory."""
        docs = md_ingestor.discover(tmp_md_dir)
        md_paths = {str(doc.path.name) for doc in docs}
        assert "with_frontmatter.md" in md_paths
        assert "without_frontmatter.md" in md_paths
        assert "empty.md" in md_paths

    def test_discovers_recursively(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should discover .md files in subdirectories."""
        docs = md_ingestor.discover(tmp_md_dir)
        nested_paths = [doc for doc in docs if "nested" in str(doc.path)]
        assert len(nested_paths) == 1

    def test_ignores_non_md_files(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should not discover non-markdown files."""
        docs = md_ingestor.discover(tmp_md_dir)
        txt_paths = [doc for doc in docs if str(doc.path).endswith(".txt")]
        assert len(txt_paths) == 0

    def test_returns_raw_documents(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should return a list of RawDocument objects."""
        docs = md_ingestor.discover(tmp_md_dir)
        assert all(isinstance(doc, RawDocument) for doc in docs)

    def test_raw_document_has_content(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Discovered documents should have byte content."""
        docs = md_ingestor.discover(tmp_md_dir)
        non_empty = [doc for doc in docs if doc.path.name != "empty.md"]
        assert all(len(doc.content) > 0 for doc in non_empty)

    def test_raw_document_has_encoding(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Discovered documents should have encoding detected."""
        docs = md_ingestor.discover(tmp_md_dir)
        non_empty = [doc for doc in docs if doc.path.name != "empty.md"]
        assert all(doc.detected_encoding != "" for doc in non_empty)

    def test_empty_directory(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should return empty list for directory with no .md files."""
        docs = md_ingestor.discover(tmp_path)
        assert docs == []

    def test_single_file_source(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should handle a single file path as source."""
        single_file = tmp_md_dir / "with_frontmatter.md"
        docs = md_ingestor.discover(single_file)
        assert len(docs) == 1
        assert docs[0].path == single_file


# ---- Parse Tests ----


class TestMarkdownIngestorParse:
    """Tests for MarkdownIngestor.parse()."""

    def test_parse_with_frontmatter(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should detect and preserve existing YAML frontmatter."""
        docs = md_ingestor.discover(tmp_md_dir)
        fm_doc = next(d for d in docs if d.path.name == "with_frontmatter.md")
        fragments = md_ingestor.parse(fm_doc)
        assert len(fragments) == 1
        assert fragments[0].metadata.get("existing_frontmatter", {}).get("title") == (
            "Existing Title"
        )

    def test_parse_preserves_existing_tags(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should preserve existing frontmatter tags."""
        docs = md_ingestor.discover(tmp_md_dir)
        fm_doc = next(d for d in docs if d.path.name == "with_frontmatter.md")
        fragments = md_ingestor.parse(fm_doc)
        existing_fm = fragments[0].metadata.get("existing_frontmatter", {})
        assert existing_fm.get("tags") == ["python", "testing"]

    def test_parse_without_frontmatter(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should handle files without frontmatter."""
        docs = md_ingestor.discover(tmp_md_dir)
        plain_doc = next(d for d in docs if d.path.name == "without_frontmatter.md")
        fragments = md_ingestor.parse(plain_doc)
        assert len(fragments) == 1
        assert fragments[0].metadata.get("existing_frontmatter") == {}

    def test_parse_extracts_content(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should extract markdown content body (without frontmatter delimiters)."""
        docs = md_ingestor.discover(tmp_md_dir)
        plain_doc = next(d for d in docs if d.path.name == "without_frontmatter.md")
        fragments = md_ingestor.parse(plain_doc)
        assert "# My Notes" in fragments[0].content
        assert "Some plain markdown content." in fragments[0].content

    def test_parse_content_excludes_frontmatter_delimiters(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Content should not include YAML frontmatter delimiters."""
        docs = md_ingestor.discover(tmp_md_dir)
        fm_doc = next(d for d in docs if d.path.name == "with_frontmatter.md")
        fragments = md_ingestor.parse(fm_doc)
        # Content should not start with ---
        assert not fragments[0].content.startswith("---")

    def test_parse_sets_source_path(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should set source_path to the original file path."""
        docs = md_ingestor.discover(tmp_md_dir)
        doc = docs[0]
        fragments = md_ingestor.parse(doc)
        assert fragments[0].source_path == str(doc.path)

    def test_parse_sets_timestamp(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should set a timestamp on parsed fragments."""
        docs = md_ingestor.discover(tmp_md_dir)
        doc = next(d for d in docs if d.path.name == "with_frontmatter.md")
        fragments = md_ingestor.parse(doc)
        assert isinstance(fragments[0].timestamp, datetime)

    def test_parse_empty_file(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should handle empty markdown files gracefully."""
        docs = md_ingestor.discover(tmp_md_dir)
        empty_doc = next(d for d in docs if d.path.name == "empty.md")
        fragments = md_ingestor.parse(empty_doc)
        assert len(fragments) == 1
        assert fragments[0].content == ""

    def test_parse_detects_document_type(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should detect document type from content patterns."""
        docs = md_ingestor.discover(tmp_md_dir)
        doc = next(d for d in docs if d.path.name == "without_frontmatter.md")
        fragments = md_ingestor.parse(doc)
        assert "document_type" in fragments[0].metadata

    def test_parse_returns_parsed_fragments(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should return a list of ParsedFragment objects."""
        docs = md_ingestor.discover(tmp_md_dir)
        fragments = md_ingestor.parse(docs[0])
        assert all(isinstance(f, ParsedFragment) for f in fragments)


# ---- Document Type Detection Tests ----


class TestDocumentTypeDetection:
    """Tests for _detect_document_type helper function."""

    def test_detects_journal(self) -> None:
        """Should detect journal-style content."""
        content = (
            "# 2024-01-15\n\nToday I reflected on my progress.\n"
            "Dear diary, things are going well.\n"
        )
        assert _detect_document_type(content) == "journal"

    def test_detects_essay(self) -> None:
        """Should detect essay-style content with multiple sections."""
        content = (
            "# The Nature of Knowledge\n\n"
            "## Introduction\n\n"
            "In this essay, I argue that knowledge is interconnected.\n\n"
            "## Thesis\n\n"
            "My thesis is that systematic organization leads to insights.\n\n"
            "## Conclusion\n\n"
            "Therefore, we should embrace structured thinking.\n"
        )
        assert _detect_document_type(content) == "essay"

    def test_detects_technical(self) -> None:
        """Should detect technical content with code blocks."""
        content = (
            "# API Reference\n\n"
            "```python\ndef foo() -> None:\n    pass\n```\n\n"
            "## Configuration\n\n"
            "```yaml\nkey: value\n```\n"
        )
        assert _detect_document_type(content) == "technical"

    def test_detects_notes_as_default(self) -> None:
        """Should default to 'notes' for unclassifiable content."""
        content = "# My Notes\n\nSome plain markdown content.\n"
        assert _detect_document_type(content) == "notes"

    def test_empty_content(self) -> None:
        """Should return 'notes' for empty content."""
        assert _detect_document_type("") == "notes"


# ---- Platform Inference Tests ----


class TestPlatformInference:
    """Tests for _infer_platform helper function."""

    def test_infers_journal_platform(self) -> None:
        """Should infer JOURNAL platform for journal-type documents."""
        assert _infer_platform("journal", Path("/notes/daily/2024-01-15.md")) == (
            SourcePlatform.JOURNAL
        )

    def test_infers_essay_platform(self) -> None:
        """Should infer ESSAY platform for essay-type documents."""
        assert _infer_platform("essay", Path("/writing/my-essay.md")) == (
            SourcePlatform.ESSAY
        )

    def test_infers_code_platform(self) -> None:
        """Should infer CODE platform for technical-type documents."""
        assert _infer_platform("technical", Path("/docs/api.md")) == (
            SourcePlatform.CODE
        )

    def test_infers_other_platform_for_notes(self) -> None:
        """Should infer OTHER platform for notes-type documents."""
        assert _infer_platform("notes", Path("/notes/misc.md")) == (
            SourcePlatform.OTHER
        )

    def test_infers_journal_from_path(self) -> None:
        """Should infer JOURNAL platform from directory path patterns."""
        assert _infer_platform("notes", Path("/daily/2024-01-15.md")) == (
            SourcePlatform.JOURNAL
        )

    def test_infers_journal_from_journal_dir(self) -> None:
        """Should infer JOURNAL from 'journal' in path."""
        assert _infer_platform("notes", Path("/journal/entry.md")) == (
            SourcePlatform.JOURNAL
        )

    def test_infers_essay_from_path(self) -> None:
        """Should infer ESSAY platform from 'essays' in directory path."""
        assert _infer_platform("notes", Path("/essays/my-essay.md")) == (
            SourcePlatform.ESSAY
        )


# ---- Frontmatter Merge Tests ----


class TestFrontmatterMerge:
    """Tests for _merge_frontmatter helper function."""

    def test_creek_defaults_when_no_existing(self) -> None:
        """Creek fields should be used when no existing frontmatter exists."""
        creek_fm: dict[str, Any] = {"type": "fragment", "source": {"platform": "other"}}
        existing: dict[str, Any] = {}
        merged = _merge_frontmatter(creek_fm, existing)
        assert merged["type"] == "fragment"

    def test_existing_takes_priority(self) -> None:
        """Existing frontmatter fields should take priority over Creek defaults."""
        creek_fm: dict[str, Any] = {"title": "Creek Title", "type": "fragment"}
        existing: dict[str, Any] = {"title": "Original Title"}
        merged = _merge_frontmatter(creek_fm, existing)
        assert merged["title"] == "Original Title"

    def test_creek_fields_added_as_defaults(self) -> None:
        """Creek fields not present in existing should be added."""
        creek_fm: dict[str, Any] = {"type": "fragment", "source": {"platform": "other"}}
        existing: dict[str, Any] = {"title": "My Title"}
        merged = _merge_frontmatter(creek_fm, existing)
        assert merged["title"] == "My Title"
        assert merged["type"] == "fragment"

    def test_empty_both(self) -> None:
        """Merging two empty dicts should return an empty dict."""
        merged = _merge_frontmatter({}, {})
        assert merged == {}

    def test_nested_dicts_are_not_deep_merged(self) -> None:
        """Nested dicts should be replaced entirely, not deep-merged."""
        creek_fm: dict[str, Any] = {"source": {"platform": "other", "file": "a.md"}}
        existing: dict[str, Any] = {"source": {"platform": "journal"}}
        merged = _merge_frontmatter(creek_fm, existing)
        # Existing takes priority at the top-level key
        assert merged["source"] == {"platform": "journal"}


# ---- convert_to_markdown Tests ----


class TestMarkdownIngestorConvertToMarkdown:
    """Tests for MarkdownIngestor.convert_to_markdown()."""

    def test_preserves_content(self, md_ingestor: MarkdownIngestor) -> None:
        """Should return the content as-is since it is already markdown."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = md_ingestor.convert_to_markdown(fragment)
        assert result == "# Hello\n\nWorld.\n"

    def test_preserves_formatting(self, md_ingestor: MarkdownIngestor) -> None:
        """Should preserve existing markdown formatting (bold, italic, etc.)."""
        content = "**bold** and *italic* and `code`\n\n- list item\n"
        fragment = ParsedFragment(
            content=content,
            metadata={},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = md_ingestor.convert_to_markdown(fragment)
        assert result == content

    def test_empty_content(self, md_ingestor: MarkdownIngestor) -> None:
        """Should handle empty content gracefully."""
        fragment = ParsedFragment(
            content="",
            metadata={},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        result = md_ingestor.convert_to_markdown(fragment)
        assert result == ""


# ---- generate_frontmatter Tests ----


class TestMarkdownIngestorGenerateFrontmatter:
    """Tests for MarkdownIngestor.generate_frontmatter()."""

    def test_generates_type_field(self, md_ingestor: MarkdownIngestor) -> None:
        """Should include 'type: fragment' in generated frontmatter."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={"document_type": "notes", "existing_frontmatter": {}},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        assert fm["type"] == "fragment"

    def test_generates_source_platform(self, md_ingestor: MarkdownIngestor) -> None:
        """Should include source.platform in generated frontmatter."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={"document_type": "notes", "existing_frontmatter": {}},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        assert "source" in fm
        assert "platform" in fm["source"]

    def test_generates_original_file(self, md_ingestor: MarkdownIngestor) -> None:
        """Should include source.original_file in generated frontmatter."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={"document_type": "notes", "existing_frontmatter": {}},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        assert fm["source"]["original_file"] == "/fake/test.md"

    def test_merges_with_existing_frontmatter(
        self, md_ingestor: MarkdownIngestor
    ) -> None:
        """Should merge Creek defaults with existing frontmatter."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={
                "document_type": "notes",
                "existing_frontmatter": {"title": "My Custom Title", "tags": ["foo"]},
            },
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        # Existing title takes priority
        assert fm["title"] == "My Custom Title"
        # Existing tags take priority
        assert fm["tags"] == ["foo"]
        # Creek type is still added
        assert fm["type"] == "fragment"

    def test_generates_created_timestamp(self, md_ingestor: MarkdownIngestor) -> None:
        """Should include a 'created' timestamp."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={"document_type": "notes", "existing_frontmatter": {}},
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        assert "created" in fm

    def test_existing_frontmatter_priority_over_creek_defaults(
        self, md_ingestor: MarkdownIngestor
    ) -> None:
        """Existing frontmatter should override Creek-generated defaults."""
        fragment = ParsedFragment(
            content="# Hello\n\nWorld.\n",
            metadata={
                "document_type": "notes",
                "existing_frontmatter": {
                    "type": "custom_type",
                    "source": {"platform": "journal"},
                },
            },
            source_path="/fake/test.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        assert fm["type"] == "custom_type"
        assert fm["source"] == {"platform": "journal"}

    def test_journal_type_gets_journal_platform(
        self, md_ingestor: MarkdownIngestor
    ) -> None:
        """Journal document type should get JOURNAL platform."""
        fragment = ParsedFragment(
            content="Today I reflected on my progress.\n",
            metadata={"document_type": "journal", "existing_frontmatter": {}},
            source_path="/fake/journal.md",
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
        )
        fm = md_ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == SourcePlatform.JOURNAL


# ---- Full Pipeline Integration Tests ----


class TestMarkdownIngestorFullPipeline:
    """Tests for the full MarkdownIngestor.ingest() pipeline."""

    def test_ingest_returns_result(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Full ingest pipeline should return an IngestResult."""
        result = md_ingestor.ingest(tmp_md_dir)
        assert isinstance(result, IngestResult)

    def test_ingest_finds_all_md_files(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should process all markdown files in the directory."""
        result = md_ingestor.ingest(tmp_md_dir)
        # 4 md files: with_frontmatter, without_frontmatter, nested, empty
        assert len(result.fragments) == 4

    def test_ingest_has_provenance(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Should generate provenance entries for all processed files."""
        result = md_ingestor.ingest(tmp_md_dir)
        assert len(result.provenance) == 4

    def test_ingest_provenance_names_ingestor(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Provenance should identify MarkdownIngestor as the processing class."""
        result = md_ingestor.ingest(tmp_md_dir)
        assert all(p.ingestor_name == "MarkdownIngestor" for p in result.provenance)

    def test_ingest_no_errors(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """Full pipeline should complete without errors for valid files."""
        result = md_ingestor.ingest(tmp_md_dir)
        assert result.errors == []

    def test_ingest_fragments_have_frontmatter(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """All fragments should have frontmatter generated."""
        result = md_ingestor.ingest(tmp_md_dir)
        for frag in result.fragments:
            assert "frontmatter" in frag.metadata

    def test_ingest_fragments_have_markdown(
        self, md_ingestor: MarkdownIngestor, tmp_md_dir: Path
    ) -> None:
        """All fragments should have markdown content."""
        result = md_ingestor.ingest(tmp_md_dir)
        for frag in result.fragments:
            assert "markdown" in frag.metadata


# ---- Registry Tests ----


class TestMarkdownIngestorRegistry:
    """Tests for MarkdownIngestor registration in INGESTOR_REGISTRY."""

    def test_registered_in_registry(self) -> None:
        """MarkdownIngestor should be registered in INGESTOR_REGISTRY."""
        from creek.ingest import INGESTOR_REGISTRY

        assert "markdown" in INGESTOR_REGISTRY

    def test_registry_maps_to_class(self) -> None:
        """Registry entry should map to the MarkdownIngestor class."""
        from creek.ingest import INGESTOR_REGISTRY

        assert INGESTOR_REGISTRY["markdown"] is MarkdownIngestor


# ---- Edge Case Tests ----


class TestMarkdownIngestorEdgeCases:
    """Tests for edge cases in MarkdownIngestor."""

    def test_file_with_only_frontmatter(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should handle files that contain only frontmatter and no body."""
        only_fm = tmp_path / "only_fm.md"
        only_fm.write_text(
            "---\ntitle: Only Frontmatter\n---\n",
            encoding="utf-8",
        )
        docs = md_ingestor.discover(tmp_path)
        fragments = md_ingestor.parse(docs[0])
        assert len(fragments) == 1
        assert fragments[0].content.strip() == ""

    def test_frontmatter_with_creek_fields(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should preserve Creek-compatible fields from existing frontmatter."""
        creek_fm = tmp_path / "creek_fm.md"
        creek_fm.write_text(
            "---\n"
            "type: fragment\n"
            "title: Creek Fragment\n"
            "source:\n"
            "  platform: claude\n"
            "  conversation_id: conv-001\n"
            "---\n\n"
            "# Creek Fragment\n\nSome content.\n",
            encoding="utf-8",
        )
        docs = md_ingestor.discover(tmp_path)
        fragments = md_ingestor.parse(docs[0])
        fm = md_ingestor.generate_frontmatter(fragments[0])
        # Existing Creek fields should take priority
        assert fm["type"] == "fragment"
        assert fm["title"] == "Creek Fragment"
        assert fm["source"]["platform"] == "claude"
        assert fm["source"]["conversation_id"] == "conv-001"

    def test_malformed_frontmatter(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should handle files with malformed frontmatter gracefully."""
        bad_fm = tmp_path / "bad_fm.md"
        bad_fm.write_text(
            "---\ntitle: [invalid yaml\n---\n\n# Content\n",
            encoding="utf-8",
        )
        docs = md_ingestor.discover(tmp_path)
        # Should not raise
        fragments = md_ingestor.parse(docs[0])
        assert len(fragments) == 1

    def test_nonexistent_directory(self, md_ingestor: MarkdownIngestor) -> None:
        """Should return empty list for non-existent directory."""
        docs = md_ingestor.discover(Path("/nonexistent/path"))
        assert docs == []

    def test_timestamp_from_frontmatter_date(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should extract timestamp from frontmatter 'date' field."""
        dated = tmp_path / "dated.md"
        dated.write_text(
            "---\ndate: 2024-06-15\n---\n\n# Dated\n",
            encoding="utf-8",
        )
        docs = md_ingestor.discover(tmp_path)
        fragments = md_ingestor.parse(docs[0])
        assert fragments[0].timestamp.year == 2024
        # Date-only timestamps are midnight UTC, which normalizes
        # to the previous evening in LA timezone
        assert fragments[0].timestamp.tzinfo is not None

    def test_timestamp_from_frontmatter_created(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should extract timestamp from frontmatter 'created' field."""
        created = tmp_path / "created.md"
        created.write_text(
            "---\ncreated: 2024-03-20T12:00:00\n---\n\n# Created\n",
            encoding="utf-8",
        )
        docs = md_ingestor.discover(tmp_path)
        fragments = md_ingestor.parse(docs[0])
        assert fragments[0].timestamp.year == 2024
        assert fragments[0].timestamp.month == 3

    def test_invalid_frontmatter_timestamp_falls_back(
        self, md_ingestor: MarkdownIngestor, tmp_path: Path
    ) -> None:
        """Should fall back to file timestamp when frontmatter date is invalid."""
        invalid_date = tmp_path / "invalid_date.md"
        invalid_date.write_text(
            "---\ndate: not-a-real-date\n---\n\n# Content\n",
            encoding="utf-8",
        )
        docs = md_ingestor.discover(tmp_path)
        fragments = md_ingestor.parse(docs[0])
        # Should still get a valid timestamp (from filesystem)
        assert isinstance(fragments[0].timestamp, datetime)
        assert fragments[0].timestamp.tzinfo is not None
