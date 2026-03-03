"""Tests for creek.clean.dedup — normalized deduplication engine.

Tests cover:
- DeduplicationResult model (is_duplicate, match_type, matched_fragment_id)
- Deduplicator: exact duplicate detection (same source + timestamp + content)
- Deduplicator: normalized duplicate detection (whitespace/case/punctuation variants)
- Deduplicator: non-duplicate correctly identified
- Deduplicator: register and check fragments
- Deduplicator: clear/reset functionality
- Edge cases: empty content, unicode normalization, whitespace-only differences
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from creek.clean.dedup import DeduplicationResult, Deduplicator
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
