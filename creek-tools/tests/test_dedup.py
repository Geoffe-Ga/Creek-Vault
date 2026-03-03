"""Tests for creek.clean.dedup — normalized deduplication engine.

Tests cover:
- DeduplicationResult model (is_duplicate, match_type, matched_fragment_id)
- Deduplicator: exact duplicate detection (same source + timestamp + content)
- Deduplicator: normalized duplicate detection (whitespace/case/punctuation variants)
- Deduplicator: non-duplicate correctly identified
- Deduplicator: register and check fragments
- Deduplicator: clear/reset functionality
- Edge cases: empty content, unicode normalization, whitespace-only differences
- Vault seeding: seed_from_vault populates indexes from markdown files
- Index persistence: save_index/load_index round-trip
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from creek.clean.dedup import (
    DeduplicationResult,
    Deduplicator,
    _compute_exact_hash,
    _compute_normalized_hash,
)
from creek.ingest.base import generate_fragment_id
from creek.models import Fragment, FragmentSource

# ---------------------------------------------------------------------------
# DeduplicationResult model
# ---------------------------------------------------------------------------


class TestDeduplicationResult:
    """Tests for the DeduplicationResult Pydantic model."""

    def test_non_duplicate_result(self) -> None:
        """Non-duplicate result should have correct default fields."""
        result = DeduplicationResult(
            is_duplicate=False,
            match_type="none",
            matched_fragment_id=None,
        )
        assert result.is_duplicate is False
        assert result.match_type == "none"
        assert result.matched_fragment_id is None

    def test_exact_duplicate_result(self) -> None:
        """Exact duplicate result should store the matched fragment ID."""
        result = DeduplicationResult(
            is_duplicate=True,
            match_type="exact",
            matched_fragment_id="frag-abc123def456",
        )
        assert result.is_duplicate is True
        assert result.match_type == "exact"
        assert result.matched_fragment_id == "frag-abc123def456"

    def test_normalized_duplicate_result(self) -> None:
        """Normalized duplicate result should store the matched fragment ID."""
        result = DeduplicationResult(
            is_duplicate=True,
            match_type="normalized",
            matched_fragment_id="frag-deadbeef0000",
        )
        assert result.is_duplicate is True
        assert result.match_type == "normalized"
        assert result.matched_fragment_id == "frag-deadbeef0000"

    def test_match_type_literal_validation(self) -> None:
        """match_type must be one of 'exact', 'normalized', 'none'."""
        with pytest.raises(ValidationError):
            DeduplicationResult(
                is_duplicate=True,
                match_type="invalid",  # type: ignore[arg-type]
                matched_fragment_id=None,
            )

    def test_serializable(self) -> None:
        """DeduplicationResult should be JSON-serializable."""
        result = DeduplicationResult(
            is_duplicate=True,
            match_type="exact",
            matched_fragment_id="frag-abc123",
        )
        data = result.model_dump(mode="json")
        assert data["is_duplicate"] is True
        assert data["match_type"] == "exact"
        assert data["matched_fragment_id"] == "frag-abc123"


# ---------------------------------------------------------------------------
# Deduplicator — registration
# ---------------------------------------------------------------------------


class TestDeduplicatorRegister:
    """Tests for Deduplicator.register."""

    def test_register_returns_result(self) -> None:
        """register() should return a DeduplicationResult."""
        dedup = Deduplicator()
        result = dedup.register(
            source="chat.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello world",
        )
        assert isinstance(result, DeduplicationResult)

    def test_first_registration_is_not_duplicate(self) -> None:
        """First registration of a fragment should not be a duplicate."""
        dedup = Deduplicator()
        result = dedup.register(
            source="chat.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello world",
        )
        assert result.is_duplicate is False
        assert result.match_type == "none"
        assert result.matched_fragment_id is None

    def test_registry_size_after_register(self) -> None:
        """Registry size should increase after registration."""
        dedup = Deduplicator()
        assert dedup.size == 0
        dedup.register(
            source="chat.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello world",
        )
        assert dedup.size == 1


# ---------------------------------------------------------------------------
# Deduplicator — exact duplicate detection
# ---------------------------------------------------------------------------


class TestExactDuplicate:
    """Tests for exact duplicate detection (same source + timestamp + content)."""

    def test_exact_same_content_is_duplicate(self) -> None:
        """Re-registering identical source+timestamp+content is exact dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello world")
        result = dedup.register(source="a.json", timestamp=ts, content="Hello world")
        assert result.is_duplicate is True
        assert result.match_type == "exact"
        assert result.matched_fragment_id is not None

    def test_exact_duplicate_returns_original_id(self) -> None:
        """Exact duplicate should reference the first-registered fragment ID."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        first = dedup.register(source="a.json", timestamp=ts, content="Test content")
        second = dedup.register(source="a.json", timestamp=ts, content="Test content")
        assert second.is_duplicate is True
        assert second.matched_fragment_id is not None
        # The matched ID should be deterministic (same inputs => same hash)
        assert first.is_duplicate is False

    def test_different_source_not_exact_duplicate(self) -> None:
        """Different source should not trigger exact duplicate."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        result = dedup.register(source="b.json", timestamp=ts, content="Hello")
        assert result.match_type != "exact"

    def test_different_timestamp_not_exact_duplicate(self) -> None:
        """Different timestamp should not trigger exact duplicate."""
        dedup = Deduplicator()
        dedup.register(
            source="a.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello",
        )
        result = dedup.register(
            source="a.json",
            timestamp=datetime(2025, 6, 1, tzinfo=UTC),
            content="Hello",
        )
        assert result.match_type != "exact"

    def test_different_content_not_exact_duplicate(self) -> None:
        """Different content should not trigger exact duplicate."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        result = dedup.register(source="a.json", timestamp=ts, content="Goodbye")
        assert result.match_type != "exact"


# ---------------------------------------------------------------------------
# Deduplicator — normalized duplicate detection
# ---------------------------------------------------------------------------


class TestNormalizedDuplicate:
    """Tests for normalized duplicate detection (whitespace/case/punctuation)."""

    def test_whitespace_variation_is_normalized_dup(self) -> None:
        """Content differing only in whitespace should be normalized dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello   world")
        result = dedup.register(source="b.json", timestamp=ts, content="Hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_case_variation_is_normalized_dup(self) -> None:
        """Content differing only in case should be normalized dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello World")
        result = dedup.register(source="b.json", timestamp=ts, content="hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_punctuation_variation_is_normalized_dup(self) -> None:
        """Content differing only in punctuation should be normalized dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello, world!")
        result = dedup.register(source="b.json", timestamp=ts, content="Hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_combined_normalization(self) -> None:
        """Content differing in case, whitespace, AND punctuation."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(
            source="a.json",
            timestamp=ts,
            content="  Hello,  WORLD!  ",
        )
        result = dedup.register(
            source="b.json",
            timestamp=ts,
            content="hello world",
        )
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_exact_match_takes_priority_over_normalized(self) -> None:
        """If both exact and normalized match, exact should be reported."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello world")
        result = dedup.register(source="a.json", timestamp=ts, content="Hello world")
        assert result.match_type == "exact"

    def test_newline_variation_is_normalized_dup(self) -> None:
        """Content differing only in newlines should be normalized dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello\nworld")
        result = dedup.register(source="b.json", timestamp=ts, content="Hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_tab_variation_is_normalized_dup(self) -> None:
        """Content differing only in tabs should be normalized dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello\tworld")
        result = dedup.register(source="b.json", timestamp=ts, content="Hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_semantically_different_content_not_dup(self) -> None:
        """Genuinely different content should not be a normalized dup."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello world")
        result = dedup.register(
            source="b.json",
            timestamp=ts,
            content="Goodbye universe",
        )
        assert result.is_duplicate is False
        assert result.match_type == "none"


# ---------------------------------------------------------------------------
# Deduplicator — check (read-only)
# ---------------------------------------------------------------------------


class TestDeduplicatorCheck:
    """Tests for Deduplicator.check (read-only duplicate check)."""

    def test_check_returns_result(self) -> None:
        """check() should return a DeduplicationResult without registering."""
        dedup = Deduplicator()
        result = dedup.check(
            source="a.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello",
        )
        assert isinstance(result, DeduplicationResult)
        assert dedup.size == 0  # check should NOT register

    def test_check_finds_exact_duplicate(self) -> None:
        """check() should detect an exact duplicate after registration."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        result = dedup.check(source="a.json", timestamp=ts, content="Hello")
        assert result.is_duplicate is True
        assert result.match_type == "exact"

    def test_check_finds_normalized_duplicate(self) -> None:
        """check() should detect a normalized duplicate after registration."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello World")
        result = dedup.check(source="b.json", timestamp=ts, content="hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_check_does_not_register(self) -> None:
        """check() should not modify the registry."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.check(source="a.json", timestamp=ts, content="Hello")
        assert dedup.size == 0
        # Subsequent check for same content should still not find it
        result = dedup.check(source="a.json", timestamp=ts, content="Hello")
        assert result.is_duplicate is False


# ---------------------------------------------------------------------------
# Deduplicator — clear/reset
# ---------------------------------------------------------------------------


class TestDeduplicatorClear:
    """Tests for Deduplicator.clear (reset the registry)."""

    def test_clear_empties_registry(self) -> None:
        """clear() should remove all registered fragments."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        dedup.register(source="b.json", timestamp=ts, content="World")
        assert dedup.size == 2
        dedup.clear()
        assert dedup.size == 0

    def test_clear_allows_re_registration(self) -> None:
        """After clear(), previously-duplicate content should be new."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        dedup.clear()
        result = dedup.register(source="a.json", timestamp=ts, content="Hello")
        assert result.is_duplicate is False
        assert result.match_type == "none"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases in deduplication."""

    def test_empty_content(self) -> None:
        """Empty string content should be handled without error."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        result = dedup.register(source="a.json", timestamp=ts, content="")
        assert result.is_duplicate is False
        # Re-registering empty should detect duplicate
        result2 = dedup.register(source="a.json", timestamp=ts, content="")
        assert result2.is_duplicate is True

    def test_whitespace_only_content(self) -> None:
        """Whitespace-only content should be handled without error."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="   \n\t  ")
        result = dedup.register(source="b.json", timestamp=ts, content="  ")
        # Both normalize to empty, so should be normalized dup
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_unicode_content(self) -> None:
        """Unicode content should be handled correctly."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        result = dedup.register(
            source="a.json",
            timestamp=ts,
            content="Caf\u00e9 na\u00efve r\u00e9sum\u00e9",
        )
        assert result.is_duplicate is False

    def test_unicode_normalized_dup(self) -> None:
        """Unicode content with case/punctuation variation should normalize."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Caf\u00e9!")
        result = dedup.register(source="b.json", timestamp=ts, content="caf\u00e9")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_very_long_content(self) -> None:
        """Very long content should not cause issues."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        long_content = "word " * 10_000
        result = dedup.register(source="a.json", timestamp=ts, content=long_content)
        assert result.is_duplicate is False

    def test_multiple_fragments_no_collision(self) -> None:
        """Multiple distinct fragments should not collide."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(100):
            result = dedup.register(
                source=f"file{i}.json",
                timestamp=ts,
                content=f"Unique content number {i}",
            )
            assert result.is_duplicate is False
        assert dedup.size == 100


# ---------------------------------------------------------------------------
# Fragment ID generation
# ---------------------------------------------------------------------------


class TestFragmentIdGeneration:
    """Tests for deterministic fragment ID generation in Deduplicator."""

    def test_deterministic_id(self) -> None:
        """Same inputs should always produce the same fragment ID."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        # Register same again to get the matched_fragment_id
        result = dedup.register(source="a.json", timestamp=ts, content="Hello")
        assert result.matched_fragment_id is not None
        assert result.matched_fragment_id.startswith("frag-")

    def test_id_matches_canonical_function(self) -> None:
        """Fragment IDs should match the canonical generate_fragment_id."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        result = dedup.register(source="a.json", timestamp=ts, content="Hello")
        expected_id = generate_fragment_id("a.json", ts, "Hello")
        assert result.matched_fragment_id == expected_id

    def test_different_inputs_different_ids(self) -> None:
        """Different inputs should produce different fragment IDs."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        dedup.register(source="a.json", timestamp=ts, content="World")
        # Check that both are stored (neither is duplicate of the other)
        assert dedup.size == 2


# ---------------------------------------------------------------------------
# Integration: Deduplicator with real pipeline types
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDeduplicatorIntegration:
    """Integration test: Deduplicator with Fragment models from the pipeline."""

    def test_dedup_with_fragment_model(self) -> None:
        """Deduplicator should detect duplicates using Fragment model fields."""
        dedup = Deduplicator()
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        source_file = "notes/journal.md"
        content = "Today I reflected on the nature of creativity."

        fragment = Fragment(
            title="Journal Entry",
            source=FragmentSource(
                platform="journal",
                original_file=source_file,
            ),
            created=ts,
        )

        # Register using fields from the Fragment model
        source_key = fragment.source.original_file or fragment.source.platform
        first = dedup.register(
            source=source_key,
            timestamp=fragment.created,
            content=content,
        )
        assert first.is_duplicate is False

        # Same fragment re-ingested should be exact duplicate
        second = dedup.register(
            source=source_key,
            timestamp=fragment.created,
            content=content,
        )
        assert second.is_duplicate is True
        assert second.match_type == "exact"

        # Fragment ID should match canonical function
        expected_id = generate_fragment_id(source_file, ts, content)
        assert second.matched_fragment_id == expected_id

    def test_dedup_normalized_across_sources(self) -> None:
        """Normalized dedup catches same content from different sources."""
        dedup = Deduplicator()
        ts = datetime(2025, 6, 15, tzinfo=UTC)
        content_original = "The key insight is that patterns repeat."

        frag_discord = Fragment(
            title="Discord message",
            source=FragmentSource(
                platform="discord",
                original_file="discord/general.json",
            ),
            created=ts,
        )
        frag_journal = Fragment(
            title="Journal note",
            source=FragmentSource(
                platform="journal",
                original_file="notes/journal.md",
            ),
            created=ts,
        )

        discord_key = frag_discord.source.original_file or frag_discord.source.platform
        journal_key = frag_journal.source.original_file or frag_journal.source.platform

        dedup.register(
            source=discord_key,
            timestamp=frag_discord.created,
            content=content_original,
        )

        # Same content with minor formatting differences from another source
        result = dedup.register(
            source=journal_key,
            timestamp=frag_journal.created,
            content="  The Key Insight Is That Patterns Repeat!  ",
        )
        assert result.is_duplicate is True
        assert result.match_type == "normalized"


# ---------------------------------------------------------------------------
# Helpers for vault seeding tests
# ---------------------------------------------------------------------------


def _write_vault_md(
    vault_dir: Path,
    fragment_id: str,
    source: str,
    timestamp: datetime,
    content: str,
    *,
    subfolder: str = "01-Fragments/Conversations",
) -> Path:
    """Write a minimal vault markdown file with YAML frontmatter.

    Creates a file in the given vault subfolder with ``---`` delimited
    YAML frontmatter containing ``id``, ``source``, ``created``, and
    ``title`` fields, followed by body content.

    Args:
        vault_dir: Root of the vault directory.
        fragment_id: The fragment ID for frontmatter.
        source: The source identifier (``original_file``).
        timestamp: The fragment creation timestamp.
        content: The body content below frontmatter.
        subfolder: Vault subfolder to write into.

    Returns:
        Path to the written markdown file.
    """
    target = vault_dir / subfolder
    target.mkdir(parents=True, exist_ok=True)
    filename = f"{fragment_id}.md"
    file_path = target / filename

    ts_iso = timestamp.isoformat()
    md_text = (
        "---\n"
        f"id: {fragment_id}\n"
        f"title: Test Fragment\n"
        f"source:\n"
        f"  platform: claude\n"
        f"  original_file: {source}\n"
        f"created: '{ts_iso}'\n"
        "type: fragment\n"
        "---\n"
        f"{content}\n"
    )
    file_path.write_text(md_text, encoding="utf-8")
    return file_path


def _make_vault(tmp_path: Path) -> Path:
    """Create a minimal vault directory structure for testing.

    Args:
        tmp_path: The pytest tmp_path fixture directory.

    Returns:
        Path to the vault root.
    """
    vault = tmp_path / "vault"
    for d in ("01-Fragments/Conversations", "01-Fragments/Journal"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


# ---------------------------------------------------------------------------
# Deduplicator — seed_from_vault
# ---------------------------------------------------------------------------


class TestSeedFromVault:
    """Tests for Deduplicator.seed_from_vault (vault-seeding for cross-run dedup)."""

    def test_seed_from_vault_returns_count(self, tmp_path: Path) -> None:
        """seed_from_vault should return the number of fragments seeded."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        _write_vault_md(vault, "frag-aaa111bbb222", "chat.json", ts, "Hello world")
        _write_vault_md(vault, "frag-ccc333ddd444", "notes.md", ts, "Goodbye world")

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 2

    def test_seed_populates_exact_index(self, tmp_path: Path) -> None:
        """After seeding, exact duplicates should be detected."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "chat.json"
        content = "Hello world"
        frag_id = generate_fragment_id(source, ts, content)
        _write_vault_md(vault, frag_id, source, ts, content)

        dedup = Deduplicator()
        dedup.seed_from_vault(vault)

        result = dedup.check(source=source, timestamp=ts, content=content)
        assert result.is_duplicate is True
        assert result.match_type == "exact"
        assert result.matched_fragment_id == frag_id

    def test_seed_populates_normalized_index(self, tmp_path: Path) -> None:
        """After seeding, normalized duplicates should be detected."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "chat.json"
        content = "Hello world"
        frag_id = generate_fragment_id(source, ts, content)
        _write_vault_md(vault, frag_id, source, ts, content)

        dedup = Deduplicator()
        dedup.seed_from_vault(vault)

        # Different source+timestamp but same normalized content
        result = dedup.check(
            source="other.json",
            timestamp=datetime(2025, 6, 1, tzinfo=UTC),
            content="  HELLO   WORLD!  ",
        )
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_seed_size_reflects_fragments(self, tmp_path: Path) -> None:
        """After seeding, size should match the number of seeded fragments."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(5):
            _write_vault_md(
                vault,
                f"frag-{i:012d}",
                f"file{i}.json",
                ts,
                f"Content number {i}",
            )

        dedup = Deduplicator()
        dedup.seed_from_vault(vault)
        assert dedup.size == 5

    def test_seed_empty_vault(self, tmp_path: Path) -> None:
        """Seeding an empty vault should return 0 and leave indexes empty."""
        vault = _make_vault(tmp_path)
        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0
        assert dedup.size == 0

    def test_seed_skips_non_md_files(self, tmp_path: Path) -> None:
        """seed_from_vault should only process .md files."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        _write_vault_md(vault, "frag-aaa111bbb222", "chat.json", ts, "Hello")

        # Write a non-md file
        txt_file = vault / "01-Fragments" / "Conversations" / "notes.txt"
        txt_file.write_text("Not a markdown file", encoding="utf-8")

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 1

    def test_seed_scans_subdirectories(self, tmp_path: Path) -> None:
        """seed_from_vault should scan all subdirectories recursively."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        _write_vault_md(
            vault,
            "frag-aaa111bbb222",
            "chat.json",
            ts,
            "Conv content",
            subfolder="01-Fragments/Conversations",
        )
        _write_vault_md(
            vault,
            "frag-ccc333ddd444",
            "journal.md",
            ts,
            "Journal content",
            subfolder="01-Fragments/Journal",
        )

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 2

    def test_seed_skips_files_without_frontmatter(self, tmp_path: Path) -> None:
        """Files without valid YAML frontmatter should be skipped."""
        vault = _make_vault(tmp_path)
        no_fm = vault / "01-Fragments" / "Conversations" / "no-frontmatter.md"
        no_fm.write_text("Just some text, no frontmatter markers.\n", encoding="utf-8")

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0
        assert dedup.size == 0

    def test_seed_skips_files_missing_id(self, tmp_path: Path) -> None:
        """Files with frontmatter but no 'id' field should be skipped."""
        vault = _make_vault(tmp_path)
        missing_id = vault / "01-Fragments" / "Conversations" / "missing-id.md"
        missing_id.write_text(
            "---\ntitle: No ID Here\nsource:\n  original_file: x.json\n"
            "created: '2025-01-01T00:00:00+00:00'\n---\nSome content\n",
            encoding="utf-8",
        )

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0

    def test_seed_skips_files_missing_source(self, tmp_path: Path) -> None:
        """Files with frontmatter but no 'source' field should be skipped."""
        vault = _make_vault(tmp_path)
        missing_source = vault / "01-Fragments" / "Conversations" / "missing-src.md"
        missing_source.write_text(
            "---\nid: frag-abc123def456\ntitle: No Source\n"
            "created: '2025-01-01T00:00:00+00:00'\n---\nSome content\n",
            encoding="utf-8",
        )

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0

    def test_seed_skips_files_missing_created(self, tmp_path: Path) -> None:
        """Files with frontmatter but no 'created' field should be skipped."""
        vault = _make_vault(tmp_path)
        missing_ts = vault / "01-Fragments" / "Conversations" / "missing-ts.md"
        missing_ts.write_text(
            "---\nid: frag-abc123def456\ntitle: No Timestamp\nsource:\n"
            "  original_file: x.json\n---\nSome content\n",
            encoding="utf-8",
        )

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0

    def test_seed_merges_with_existing_registrations(self, tmp_path: Path) -> None:
        """Seeding should add to existing registrations, not replace them."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        dedup = Deduplicator()
        dedup.register(source="existing.json", timestamp=ts, content="Already here")
        assert dedup.size == 1

        _write_vault_md(vault, "frag-aaa111bbb222", "chat.json", ts, "New content")
        count = dedup.seed_from_vault(vault)
        assert count == 1
        assert dedup.size == 2

    def test_seed_cross_run_dedup(self, tmp_path: Path) -> None:
        """Seeding should enable cross-run deduplication of re-ingested content."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "chat.json"
        content = "Important insight about patterns"
        frag_id = generate_fragment_id(source, ts, content)
        _write_vault_md(vault, frag_id, source, ts, content)

        # Simulate new process run
        dedup = Deduplicator()
        dedup.seed_from_vault(vault)

        # Re-ingest same content
        result = dedup.register(source=source, timestamp=ts, content=content)
        assert result.is_duplicate is True
        assert result.match_type == "exact"
        assert result.matched_fragment_id == frag_id


# ---------------------------------------------------------------------------
# Deduplicator — save_index / load_index
# ---------------------------------------------------------------------------


class TestSaveIndex:
    """Tests for Deduplicator.save_index (persist index to JSON file)."""

    def test_save_creates_json_file(self, tmp_path: Path) -> None:
        """save_index should create a JSON file at the given path."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello world")

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)
        assert index_path.exists()

    def test_save_valid_json(self, tmp_path: Path) -> None:
        """save_index should produce valid JSON."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello world")

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert "exact_index" in data
        assert "normalized_index" in data

    def test_save_contains_correct_hashes(self, tmp_path: Path) -> None:
        """Saved index should contain the correct hash-to-id mappings."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "a.json"
        content = "Hello world"
        dedup.register(source=source, timestamp=ts, content=content)

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        data = json.loads(index_path.read_text(encoding="utf-8"))
        exact_hash = _compute_exact_hash(source, ts, content)
        norm_hash = _compute_normalized_hash(content)
        frag_id = generate_fragment_id(source, ts, content)

        assert data["exact_index"][exact_hash] == frag_id
        assert data["normalized_index"][norm_hash] == frag_id

    def test_save_empty_deduplicator(self, tmp_path: Path) -> None:
        """Saving an empty deduplicator should create a valid JSON with empty dicts."""
        dedup = Deduplicator()
        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert data["exact_index"] == {}
        assert data["normalized_index"] == {}

    def test_save_overwrites_existing_file(self, tmp_path: Path) -> None:
        """save_index should overwrite an existing index file."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        index_path = tmp_path / ".creek-dedup-index.json"

        dedup.register(source="a.json", timestamp=ts, content="First")
        dedup.save_index(index_path)

        dedup.register(source="b.json", timestamp=ts, content="Second")
        dedup.save_index(index_path)

        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert len(data["exact_index"]) == 2


class TestLoadIndex:
    """Tests for Deduplicator.load_index (load index from JSON file)."""

    def test_load_returns_count(self, tmp_path: Path) -> None:
        """load_index should return the number of entries loaded."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")
        dedup.register(source="b.json", timestamp=ts, content="World")

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        new_dedup = Deduplicator()
        count = new_dedup.load_index(index_path)
        assert count == 2

    def test_load_restores_exact_index(self, tmp_path: Path) -> None:
        """Loaded index should detect exact duplicates."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "a.json"
        content = "Hello world"
        dedup.register(source=source, timestamp=ts, content=content)

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        new_dedup = Deduplicator()
        new_dedup.load_index(index_path)

        result = new_dedup.check(source=source, timestamp=ts, content=content)
        assert result.is_duplicate is True
        assert result.match_type == "exact"

    def test_load_restores_normalized_index(self, tmp_path: Path) -> None:
        """Loaded index should detect normalized duplicates."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello world")

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        new_dedup = Deduplicator()
        new_dedup.load_index(index_path)

        result = new_dedup.check(
            source="b.json",
            timestamp=datetime(2025, 6, 1, tzinfo=UTC),
            content="  HELLO   WORLD!  ",
        )
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_load_merges_with_existing(self, tmp_path: Path) -> None:
        """load_index should merge with existing registrations."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello")

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        new_dedup = Deduplicator()
        new_dedup.register(source="z.json", timestamp=ts, content="Already here")
        assert new_dedup.size == 1

        new_dedup.load_index(index_path)
        assert new_dedup.size == 2

    def test_load_nonexistent_file_raises(self, tmp_path: Path) -> None:
        """load_index should raise FileNotFoundError for missing file."""
        dedup = Deduplicator()
        with pytest.raises(FileNotFoundError):
            dedup.load_index(tmp_path / "nonexistent.json")


class TestIndexRoundTrip:
    """Tests for save_index / load_index round-trip persistence."""

    def test_round_trip_preserves_dedup_behavior(self, tmp_path: Path) -> None:
        """Save then load should preserve full deduplication behavior."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        entries = [
            ("chat.json", "Hello world"),
            ("notes.md", "Important insight"),
            ("journal.md", "Today I learned something"),
        ]
        for source, content in entries:
            dedup.register(source=source, timestamp=ts, content=content)

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        restored = Deduplicator()
        count = restored.load_index(index_path)
        assert count == 3
        assert restored.size == 3

        # Check all original content is detected as duplicate
        for source, content in entries:
            result = restored.check(source=source, timestamp=ts, content=content)
            assert result.is_duplicate is True
            assert result.match_type == "exact"

    def test_round_trip_normalized_detection(self, tmp_path: Path) -> None:
        """Saved normalized index should detect variants after reload."""
        dedup = Deduplicator()
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup.register(source="a.json", timestamp=ts, content="Hello, World!")

        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        restored = Deduplicator()
        restored.load_index(index_path)

        result = restored.check(
            source="b.json",
            timestamp=datetime(2025, 6, 1, tzinfo=UTC),
            content="hello world",
        )
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_seed_then_save_then_load(self, tmp_path: Path) -> None:
        """Full workflow: seed from vault, save index, load in new instance."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "chat.json"
        content = "Pattern recognition is key"
        frag_id = generate_fragment_id(source, ts, content)
        _write_vault_md(vault, frag_id, source, ts, content)

        # Seed
        dedup = Deduplicator()
        dedup.seed_from_vault(vault)
        assert dedup.size == 1

        # Save
        index_path = tmp_path / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        # Load in fresh instance
        restored = Deduplicator()
        restored.load_index(index_path)
        assert restored.size == 1

        # Cross-run dedup works
        result = restored.register(source=source, timestamp=ts, content=content)
        assert result.is_duplicate is True
        assert result.match_type == "exact"
        assert result.matched_fragment_id == frag_id

    def test_index_preferred_over_vault_scan(self, tmp_path: Path) -> None:
        """When index file exists, load_index is faster than rescanning vault."""
        vault = _make_vault(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        for i in range(10):
            _write_vault_md(
                vault,
                f"frag-{i:012d}",
                f"file{i}.json",
                ts,
                f"Content {i}",
            )

        # First run: seed from vault and save index
        dedup = Deduplicator()
        dedup.seed_from_vault(vault)
        index_path = vault / ".creek-dedup-index.json"
        dedup.save_index(index_path)

        # Second run: load from index (should have same result)
        restored = Deduplicator()
        count = restored.load_index(index_path)
        assert count == 10
        assert restored.size == 10


# ---------------------------------------------------------------------------
# Edge cases — frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Tests for _parse_frontmatter edge cases."""

    def test_no_frontmatter_markers(self) -> None:
        """Text without --- markers should return empty metadata."""
        from creek.clean.dedup import _parse_frontmatter

        metadata, body = _parse_frontmatter("Just plain text")
        assert metadata == {}
        assert body == "Just plain text"

    def test_only_opening_marker(self) -> None:
        """Text with only one --- marker should return empty metadata."""
        from creek.clean.dedup import _parse_frontmatter

        metadata, _body = _parse_frontmatter("---\nsome yaml\n")
        assert metadata == {}

    def test_invalid_yaml(self) -> None:
        """Invalid YAML between markers should return empty metadata."""
        from creek.clean.dedup import _parse_frontmatter

        metadata, _body = _parse_frontmatter("---\n: : : invalid\n---\nBody")
        assert metadata == {}

    def test_non_dict_yaml(self) -> None:
        """YAML that parses to a non-dict (e.g., a list) should return empty."""
        from creek.clean.dedup import _parse_frontmatter

        metadata, _body = _parse_frontmatter("---\n- item1\n- item2\n---\nBody")
        assert metadata == {}


# ---------------------------------------------------------------------------
# Edge cases — _extract_source_key and _parse_timestamp
# ---------------------------------------------------------------------------


class TestExtractSourceKey:
    """Tests for Deduplicator._extract_source_key edge cases."""

    def test_string_source(self) -> None:
        """Plain string source should be returned directly."""
        result = Deduplicator._extract_source_key("myfile.json")
        assert result == "myfile.json"

    def test_empty_string_source(self) -> None:
        """Empty string source should return None."""
        result = Deduplicator._extract_source_key("")
        assert result is None

    def test_dict_with_original_file(self) -> None:
        """Dict source with original_file should return it."""
        result = Deduplicator._extract_source_key(
            {"original_file": "chat.json", "platform": "claude"}
        )
        assert result == "chat.json"

    def test_dict_with_platform_only(self) -> None:
        """Dict source with only platform should return platform."""
        result = Deduplicator._extract_source_key({"platform": "discord"})
        assert result == "discord"

    def test_dict_with_empty_values(self) -> None:
        """Dict source with empty values should return None."""
        result = Deduplicator._extract_source_key({"original_file": "", "platform": ""})
        assert result is None

    def test_non_string_non_dict_source(self) -> None:
        """Non-string, non-dict source (e.g., int) should return None."""
        result = Deduplicator._extract_source_key(42)
        assert result is None


class TestParseTimestamp:
    """Tests for Deduplicator._parse_timestamp edge cases."""

    def test_datetime_passthrough(self) -> None:
        """datetime object should be returned directly."""
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        result = Deduplicator._parse_timestamp(ts)
        assert result == ts

    def test_iso_string(self) -> None:
        """ISO format string should be parsed into datetime."""
        result = Deduplicator._parse_timestamp("2025-01-01T00:00:00+00:00")
        assert result is not None
        assert result.year == 2025

    def test_invalid_string(self) -> None:
        """Invalid timestamp string should return None."""
        result = Deduplicator._parse_timestamp("not-a-date")
        assert result is None

    def test_non_string_non_datetime(self) -> None:
        """Non-string, non-datetime (e.g., int) should return None."""
        result = Deduplicator._parse_timestamp(12345)
        assert result is None


# ---------------------------------------------------------------------------
# Edge cases — load_index with overlapping keys
# ---------------------------------------------------------------------------


class TestLoadIndexOverlap:
    """Tests for load_index when loaded keys overlap with existing."""

    def test_load_does_not_overwrite_existing_keys(self, tmp_path: Path) -> None:
        """load_index should not overwrite existing entries."""
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        source = "a.json"
        content = "Hello world"

        # Create index with one entry
        dedup1 = Deduplicator()
        dedup1.register(source=source, timestamp=ts, content=content)
        index_path = tmp_path / ".creek-dedup-index.json"
        dedup1.save_index(index_path)

        # Load into dedup that already has the same key
        dedup2 = Deduplicator()
        dedup2.register(source=source, timestamp=ts, content=content)
        original_size = dedup2.size

        count = dedup2.load_index(index_path)
        assert count == 0  # No new entries loaded
        assert dedup2.size == original_size


# ---------------------------------------------------------------------------
# Edge cases — _seed_from_file error handling
# ---------------------------------------------------------------------------


class TestSeedFromFileEdgeCases:
    """Tests for Deduplicator._seed_from_file error handling paths."""

    def test_seed_skips_unreadable_file(self, tmp_path: Path) -> None:
        """Files that cannot be read (e.g., permission denied) should be skipped."""
        vault = _make_vault(tmp_path)
        bad_file = vault / "01-Fragments" / "Conversations" / "unreadable.md"
        bad_file.write_text("---\nid: test\n---\ncontent\n", encoding="utf-8")
        bad_file.chmod(0o000)

        dedup = Deduplicator()
        result = dedup._seed_from_file(bad_file)
        assert result is False

        # Cleanup permissions for tmp_path cleanup
        bad_file.chmod(0o644)

    def test_seed_skips_non_extractable_source(self, tmp_path: Path) -> None:
        """Files whose source field is a non-extractable type should be skipped."""
        vault = _make_vault(tmp_path)
        bad_source = vault / "01-Fragments" / "Conversations" / "bad-source.md"
        bad_source.write_text(
            "---\nid: frag-xyz\nsource: 42\n"
            "created: '2025-01-01T00:00:00+00:00'\n---\nContent\n",
            encoding="utf-8",
        )

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0

    def test_seed_skips_invalid_timestamp(self, tmp_path: Path) -> None:
        """Files whose created field is not a valid timestamp should be skipped."""
        vault = _make_vault(tmp_path)
        bad_ts = vault / "01-Fragments" / "Conversations" / "bad-ts.md"
        bad_ts.write_text(
            "---\nid: frag-xyz\nsource:\n  original_file: chat.json\n"
            "created: 'not-a-date'\n---\nContent\n",
            encoding="utf-8",
        )

        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0
