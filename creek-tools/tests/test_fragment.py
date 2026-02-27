"""Tests for the creek.fragment fragmentation engine."""

from datetime import UTC, datetime

from creek.fragment import FragmentationConfig, FragmentationEngine
from creek.ingest.base import ParsedFragment

# ---- Helpers ----


def _make_parsed(
    content: str = "Some content.",
    source_path: str = "test.md",
    timestamp: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> ParsedFragment:
    """Create a ParsedFragment for testing."""
    return ParsedFragment(
        content=content,
        metadata=metadata or {},
        source_path=source_path,
        timestamp=timestamp or datetime(2025, 1, 1, tzinfo=UTC),
    )


# ---- FragmentationConfig ----


class TestFragmentationConfig:
    """Tests for FragmentationConfig defaults and overrides."""

    def test_default_max_words(self) -> None:
        """Default max_words should be 3000."""
        config = FragmentationConfig()
        assert config.max_words == 3000

    def test_default_min_words(self) -> None:
        """Default min_words should be 50."""
        config = FragmentationConfig()
        assert config.min_words == 50

    def test_custom_thresholds(self) -> None:
        """Config should accept custom threshold values."""
        config = FragmentationConfig(max_words=500, min_words=10)
        assert config.max_words == 500
        assert config.min_words == 10

    def test_default_section_split_level(self) -> None:
        """Default section_split_level should be 2 (H2)."""
        config = FragmentationConfig()
        assert config.section_split_level == 2


# ---- should_split ----


class TestShouldSplit:
    """Tests for FragmentationEngine.should_split."""

    def test_long_content_should_split(self) -> None:
        """Content exceeding max_words should be split."""
        engine = FragmentationEngine(FragmentationConfig(max_words=10))
        frag = _make_parsed(content=" ".join(["word"] * 20))
        assert engine.should_split(frag) is True

    def test_short_content_should_not_split(self) -> None:
        """Content under max_words should not be split."""
        engine = FragmentationEngine(FragmentationConfig(max_words=100))
        frag = _make_parsed(content="Short content here.")
        assert engine.should_split(frag) is False

    def test_exact_threshold_should_not_split(self) -> None:
        """Content at exactly max_words should not be split."""
        engine = FragmentationEngine(FragmentationConfig(max_words=5))
        frag = _make_parsed(content="one two three four five")
        assert engine.should_split(frag) is False


# ---- split_by_sections ----


class TestSplitBySections:
    """Tests for section-based splitting at heading boundaries."""

    def test_split_at_h2_boundaries(self) -> None:
        """Content should split at ## headings."""
        engine = FragmentationEngine()
        content = (
            "Intro text.\n\n## Section One\n\nBody one.\n\n## Section Two\n\nBody two."
        )
        frag = _make_parsed(content=content)
        result = engine.split_by_sections(frag)
        assert len(result) == 3
        assert "Intro text." in result[0].content
        assert "Section One" in result[1].content
        assert "Section Two" in result[2].content

    def test_no_headings_returns_single(self) -> None:
        """Content without headings should return unchanged."""
        engine = FragmentationEngine()
        frag = _make_parsed(content="Just plain text without any headings.")
        result = engine.split_by_sections(frag)
        assert len(result) == 1
        assert result[0].content == frag.content

    def test_preserves_metadata(self) -> None:
        """Split fragments should preserve source metadata."""
        engine = FragmentationEngine()
        content = "Intro.\n\n## Part\n\nBody."
        frag = _make_parsed(
            content=content,
            source_path="doc.md",
            metadata={"author": "test"},
        )
        result = engine.split_by_sections(frag)
        for child in result:
            assert child.source_path == "doc.md"

    def test_parent_id_in_children(self) -> None:
        """Split children should have parent_id in metadata."""
        engine = FragmentationEngine()
        content = "Intro.\n\n## Part\n\nBody."
        frag = _make_parsed(content=content)
        result = engine.split_by_sections(frag)
        if len(result) > 1:
            for child in result:
                assert "parent_id" in child.metadata

    def test_preserves_heading_markers(self) -> None:
        """Split sections should retain their heading markers (## prefix)."""
        engine = FragmentationEngine()
        content = (
            "Intro text.\n\n"
            "## Section One\n\nBody one.\n\n"
            "## Section Two\n\nBody two."
        )
        frag = _make_parsed(content=content)
        result = engine.split_by_sections(frag)
        assert len(result) == 3
        # Intro has no heading marker
        assert not result[0].content.startswith("#")
        # Section children must start with ## heading marker
        assert result[1].content.startswith("## ")
        assert result[2].content.startswith("## ")

    def test_preserves_heading_markers_h3(self) -> None:
        """Split sections at H3 level should retain ### prefix."""
        engine = FragmentationEngine(
            FragmentationConfig(section_split_level=3),
        )
        content = "Intro.\n\n### Alpha\n\nAlpha body.\n\n### Beta\n\nBeta body."
        frag = _make_parsed(content=content)
        result = engine.split_by_sections(frag)
        assert len(result) == 3
        assert result[1].content.startswith("### ")
        assert result[2].content.startswith("### ")


# ---- split_by_length ----


class TestSplitByLength:
    """Tests for length-based splitting at paragraph boundaries."""

    def test_split_long_content(self) -> None:
        """Content over max_words should split at paragraph boundaries."""
        engine = FragmentationEngine(FragmentationConfig(max_words=10))
        paragraphs = [" ".join(["word"] * 8) for _ in range(3)]
        content = "\n\n".join(paragraphs)
        frag = _make_parsed(content=content)
        result = engine.split_by_length(frag)
        assert len(result) > 1

    def test_short_content_not_split(self) -> None:
        """Content under max_words should not be split."""
        engine = FragmentationEngine(FragmentationConfig(max_words=100))
        frag = _make_parsed(content="Short content.")
        result = engine.split_by_length(frag)
        assert len(result) == 1

    def test_preserves_all_content(self) -> None:
        """Combined split content should equal original content."""
        engine = FragmentationEngine(FragmentationConfig(max_words=10))
        paragraphs = [" ".join(["word"] * 8) for _ in range(3)]
        content = "\n\n".join(paragraphs)
        frag = _make_parsed(content=content)
        result = engine.split_by_length(frag)
        recombined = "\n\n".join(r.content for r in result)
        assert recombined == content

    def test_custom_max_words(self) -> None:
        """split_by_length should accept a custom max_words override."""
        engine = FragmentationEngine(FragmentationConfig(max_words=1000))
        paragraphs = [" ".join(["word"] * 8) for _ in range(3)]
        content = "\n\n".join(paragraphs)
        frag = _make_parsed(content=content)
        result = engine.split_by_length(frag, max_words=10)
        assert len(result) > 1


# ---- should_group / group_fragments ----


class TestGrouping:
    """Tests for grouping short fragments."""

    def test_should_group_short_fragments(self) -> None:
        """Consecutive short fragments from same source should group."""
        engine = FragmentationEngine(FragmentationConfig(min_words=50))
        frags = [
            _make_parsed(content="Short.", source_path="a.md"),
            _make_parsed(content="Also short.", source_path="a.md"),
        ]
        assert engine.should_group(frags) is True

    def test_should_not_group_long_fragments(self) -> None:
        """Fragments above min_words should not group."""
        engine = FragmentationEngine(FragmentationConfig(min_words=2))
        frags = [
            _make_parsed(
                content=" ".join(["word"] * 10),
                source_path="a.md",
            ),
        ]
        assert engine.should_group(frags) is False

    def test_should_not_group_different_sources(self) -> None:
        """Fragments from different sources should not group."""
        engine = FragmentationEngine(FragmentationConfig(min_words=50))
        frags = [
            _make_parsed(content="Short.", source_path="a.md"),
            _make_parsed(content="Short.", source_path="b.md"),
        ]
        assert engine.should_group(frags) is False

    def test_should_not_group_outside_time_window(self) -> None:
        """Fragments outside time window should not be grouped."""
        engine = FragmentationEngine(
            FragmentationConfig(min_words=50, group_time_window_minutes=5),
        )
        t1 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        t2 = datetime(2025, 1, 1, 13, 0, tzinfo=UTC)  # 1 hour apart
        frags = [
            _make_parsed(content="Short.", source_path="a.md", timestamp=t1),
            _make_parsed(content="Short.", source_path="a.md", timestamp=t2),
        ]
        result = engine.fragment(frags)
        assert len(result) == 2  # not grouped

    def test_group_merges_content(self) -> None:
        """Grouped fragments should have combined content."""
        engine = FragmentationEngine()
        frags = [
            _make_parsed(content="First part."),
            _make_parsed(content="Second part."),
        ]
        result = engine.group_fragments(frags)
        assert "First part." in result.content
        assert "Second part." in result.content

    def test_group_preserves_earliest_timestamp(self) -> None:
        """Grouped fragment should use the earliest timestamp."""
        engine = FragmentationEngine()
        early = datetime(2025, 1, 1, tzinfo=UTC)
        late = datetime(2025, 6, 1, tzinfo=UTC)
        frags = [
            _make_parsed(content="A.", timestamp=late),
            _make_parsed(content="B.", timestamp=early),
        ]
        result = engine.group_fragments(frags)
        assert result.timestamp == early


# ---- fragment (end-to-end) ----


class TestFragmentEndToEnd:
    """Tests for the top-level fragment() orchestrator."""

    def test_passthrough_medium_content(self) -> None:
        """Medium-length content should pass through unchanged."""
        engine = FragmentationEngine(
            FragmentationConfig(max_words=100, min_words=2),
        )
        frag = _make_parsed(content=" ".join(["word"] * 50))
        result = engine.fragment([frag])
        assert len(result) == 1

    def test_splits_long_content(self) -> None:
        """Long content should be split."""
        engine = FragmentationEngine(
            FragmentationConfig(max_words=10, min_words=2),
        )
        content = (
            "## A\n\n"
            + " ".join(["word"] * 15)
            + "\n\n## B\n\n"
            + " ".join(["word"] * 15)
        )
        frag = _make_parsed(content=content)
        result = engine.fragment([frag])
        assert len(result) > 1

    def test_groups_short_fragments(self) -> None:
        """Short consecutive fragments should be grouped."""
        engine = FragmentationEngine(FragmentationConfig(min_words=50))
        frags = [
            _make_parsed(content="Tiny.", source_path="a.md"),
            _make_parsed(content="Also tiny.", source_path="a.md"),
        ]
        result = engine.fragment(frags)
        assert len(result) < len(frags)

    def test_empty_input(self) -> None:
        """Empty input should return empty output."""
        engine = FragmentationEngine()
        assert engine.fragment([]) == []
