"""Tests for creek.generate.indexes — index generator for Creek vault.

Tests cover the IndexGenerator class and all its generation methods:
frequency indexes, thread index, eddy map, temporal index, source index,
and the generate_all aggregator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from creek.generate.indexes import FREQUENCY_NAMES, IndexGenerator
from creek.models import Frequency

# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a mock vault directory structure for testing.

    Sets up all required vault subdirectories including the 10 frequency
    subdirs under 06-Frequencies/.
    """
    top_level = [
        "00-Creek-Meta",
        "01-Fragments",
        "02-Threads",
        "03-Eddies",
        "04-Praxis",
        "05-Wavelength",
        "06-Frequencies",
        "07-Voice",
        "08-Decisions",
        "09-Reference",
        "10-Liminal",
    ]
    for folder in top_level:
        (tmp_path / folder).mkdir()

    freq_subdirs = [
        "F1-Agency",
        "F2-Receptivity",
        "F3-Self-Love-Power",
        "F4-Community-Love",
        "F5-Achievism",
        "F6-Pluralism",
        "F7-Integration",
        "F8-True-Self",
        "F9-Unity",
        "F10-Emptiness",
    ]
    for subdir in freq_subdirs:
        (tmp_path / "06-Frequencies" / subdir).mkdir()

    return tmp_path


@pytest.fixture()
def generator(vault: Path) -> IndexGenerator:
    """Create an IndexGenerator instance with the mock vault path."""
    return IndexGenerator(vault)


# ---- FREQUENCY_NAMES Tests ----


class TestFrequencyNames:
    """Tests for the FREQUENCY_NAMES mapping."""

    def test_all_classified_frequencies_mapped(self) -> None:
        """Every non-UNCLASSIFIED Frequency enum member should have a name."""
        for freq in Frequency:
            if freq != Frequency.UNCLASSIFIED:
                assert freq in FREQUENCY_NAMES, f"Missing name for {freq}"

    def test_unclassified_not_in_names(self) -> None:
        """UNCLASSIFIED should not appear in FREQUENCY_NAMES."""
        assert Frequency.UNCLASSIFIED not in FREQUENCY_NAMES

    def test_names_are_human_readable(self) -> None:
        """Each name should be a non-empty string with a slash separator."""
        for freq, name in FREQUENCY_NAMES.items():
            assert isinstance(name, str)
            assert len(name) > 0
            assert "/" in name, f"Name for {freq} should contain '/': {name}"

    def test_correct_count(self) -> None:
        """There should be exactly 10 frequency names (F1-F10)."""
        assert len(FREQUENCY_NAMES) == 10

    def test_specific_names(self) -> None:
        """Spot-check some specific frequency name mappings."""
        assert FREQUENCY_NAMES[Frequency.F1] == "Survival/Safety"
        assert FREQUENCY_NAMES[Frequency.F5] == "Achievement/Strategy"
        assert FREQUENCY_NAMES[Frequency.F10] == "Unity/Transcendence"


# ---- IndexGenerator Init Tests ----


class TestIndexGeneratorInit:
    """Tests for IndexGenerator.__init__."""

    def test_stores_vault_path(self, vault: Path) -> None:
        """IndexGenerator should store the provided vault path."""
        gen = IndexGenerator(vault)
        assert gen.vault_path == vault

    def test_vault_path_is_path_object(self, vault: Path) -> None:
        """The stored vault_path should be a Path instance."""
        gen = IndexGenerator(vault)
        assert isinstance(gen.vault_path, Path)


# ---- Frequency Index Tests ----


class TestGenerateFrequencyIndexes:
    """Tests for IndexGenerator.generate_frequency_indexes."""

    def test_returns_list_of_paths(self, generator: IndexGenerator) -> None:
        """Should return a list of Path objects."""
        result = generator.generate_frequency_indexes()
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)

    def test_generates_ten_indexes(self, generator: IndexGenerator) -> None:
        """Should generate exactly 10 frequency index notes (F1-F10)."""
        result = generator.generate_frequency_indexes()
        assert len(result) == 10

    def test_files_exist_on_disk(self, generator: IndexGenerator) -> None:
        """All returned paths should exist as files on disk."""
        result = generator.generate_frequency_indexes()
        for path in result:
            assert path.is_file(), f"Generated file does not exist: {path}"

    def test_files_in_frequency_subdirs(
        self, generator: IndexGenerator, vault: Path
    ) -> None:
        """Each index file should be in its corresponding frequency subdir."""
        result = generator.generate_frequency_indexes()
        freq_dir = vault / "06-Frequencies"
        for path in result:
            assert path.parent.parent == freq_dir

    def test_files_are_markdown(self, generator: IndexGenerator) -> None:
        """All generated files should have .md extension."""
        result = generator.generate_frequency_indexes()
        for path in result:
            assert path.suffix == ".md"

    def test_files_have_yaml_frontmatter(self, generator: IndexGenerator) -> None:
        """Each index note should start with YAML frontmatter delimiters."""
        result = generator.generate_frequency_indexes()
        for path in result:
            content = path.read_text(encoding="utf-8")
            assert content.startswith("---\n"), f"Missing frontmatter start: {path}"
            # Find the closing delimiter (skip the opening one)
            rest = content[4:]
            assert "\n---\n" in rest, f"Missing frontmatter end: {path}"

    def test_files_contain_dataview_query(self, generator: IndexGenerator) -> None:
        """Each index note should contain a Dataview query block."""
        result = generator.generate_frequency_indexes()
        for path in result:
            content = path.read_text(encoding="utf-8")
            assert "```dataview" in content, f"Missing dataview query: {path}"
            assert "```\n" in content or content.endswith("```")

    def test_dataview_queries_reference_correct_frequency(
        self, generator: IndexGenerator
    ) -> None:
        """Each frequency index note should query for its specific frequency."""
        result = generator.generate_frequency_indexes()
        for path in result:
            content = path.read_text(encoding="utf-8")
            # Extract the frequency code from the parent directory name
            freq_code = path.parent.name.split("-")[0]  # e.g., "F1"
            assert (
                f'"{freq_code}"' in content
            ), f"Query should reference {freq_code}: {path}"

    def test_dataview_queries_reference_fragments_folder(
        self, generator: IndexGenerator
    ) -> None:
        """Dataview queries should reference the 01-Fragments folder."""
        result = generator.generate_frequency_indexes()
        for path in result:
            content = path.read_text(encoding="utf-8")
            assert "01-Fragments" in content

    def test_frontmatter_contains_type(self, generator: IndexGenerator) -> None:
        """Frontmatter should include a type field."""
        result = generator.generate_frequency_indexes()
        for path in result:
            content = path.read_text(encoding="utf-8")
            assert "type:" in content

    def test_idempotent_generation(self, generator: IndexGenerator) -> None:
        """Running generation twice should overwrite without errors."""
        result1 = generator.generate_frequency_indexes()
        result2 = generator.generate_frequency_indexes()
        assert len(result1) == len(result2)
        for p1, p2 in zip(result1, result2, strict=True):
            assert p1 == p2
            assert p1.is_file()


# ---- Thread Index Tests ----


class TestGenerateThreadIndex:
    """Tests for IndexGenerator.generate_thread_index."""

    def test_returns_path(self, generator: IndexGenerator) -> None:
        """Should return a single Path object."""
        result = generator.generate_thread_index()
        assert isinstance(result, Path)

    def test_file_exists(self, generator: IndexGenerator) -> None:
        """The generated file should exist on disk."""
        result = generator.generate_thread_index()
        assert result.is_file()

    def test_file_in_threads_dir(self, generator: IndexGenerator, vault: Path) -> None:
        """The thread index should be in the 02-Threads directory."""
        result = generator.generate_thread_index()
        assert result.parent == vault / "02-Threads"

    def test_file_is_markdown(self, generator: IndexGenerator) -> None:
        """The generated file should have .md extension."""
        result = generator.generate_thread_index()
        assert result.suffix == ".md"

    def test_has_yaml_frontmatter(self, generator: IndexGenerator) -> None:
        """The thread index should have YAML frontmatter."""
        result = generator.generate_thread_index()
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        rest = content[4:]
        assert "\n---\n" in rest

    def test_contains_dataview_query(self, generator: IndexGenerator) -> None:
        """The thread index should contain a Dataview query."""
        result = generator.generate_thread_index()
        content = result.read_text(encoding="utf-8")
        assert "```dataview" in content

    def test_dataview_queries_threads(self, generator: IndexGenerator) -> None:
        """The Dataview query should reference thread-related fields."""
        result = generator.generate_thread_index()
        content = result.read_text(encoding="utf-8")
        assert "status" in content.lower()

    def test_frontmatter_contains_type(self, generator: IndexGenerator) -> None:
        """Frontmatter should include a type field."""
        result = generator.generate_thread_index()
        content = result.read_text(encoding="utf-8")
        assert "type:" in content


# ---- Eddy Map Tests ----


class TestGenerateEddyMap:
    """Tests for IndexGenerator.generate_eddy_map."""

    def test_returns_path(self, generator: IndexGenerator) -> None:
        """Should return a single Path object."""
        result = generator.generate_eddy_map()
        assert isinstance(result, Path)

    def test_file_exists(self, generator: IndexGenerator) -> None:
        """The generated file should exist on disk."""
        result = generator.generate_eddy_map()
        assert result.is_file()

    def test_file_in_eddies_dir(self, generator: IndexGenerator, vault: Path) -> None:
        """The eddy map should be in the 03-Eddies directory."""
        result = generator.generate_eddy_map()
        assert result.parent == vault / "03-Eddies"

    def test_file_is_markdown(self, generator: IndexGenerator) -> None:
        """The generated file should have .md extension."""
        result = generator.generate_eddy_map()
        assert result.suffix == ".md"

    def test_has_yaml_frontmatter(self, generator: IndexGenerator) -> None:
        """The eddy map should have YAML frontmatter."""
        result = generator.generate_eddy_map()
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        rest = content[4:]
        assert "\n---\n" in rest

    def test_contains_dataview_query(self, generator: IndexGenerator) -> None:
        """The eddy map should contain a Dataview query."""
        result = generator.generate_eddy_map()
        content = result.read_text(encoding="utf-8")
        assert "```dataview" in content

    def test_frontmatter_contains_type(self, generator: IndexGenerator) -> None:
        """Frontmatter should include a type field."""
        result = generator.generate_eddy_map()
        content = result.read_text(encoding="utf-8")
        assert "type:" in content


# ---- Temporal Index Tests ----


class TestGenerateTemporalIndex:
    """Tests for IndexGenerator.generate_temporal_index."""

    def test_returns_path(self, generator: IndexGenerator) -> None:
        """Should return a single Path object."""
        result = generator.generate_temporal_index()
        assert isinstance(result, Path)

    def test_file_exists(self, generator: IndexGenerator) -> None:
        """The generated file should exist on disk."""
        result = generator.generate_temporal_index()
        assert result.is_file()

    def test_file_in_meta_dir(self, generator: IndexGenerator, vault: Path) -> None:
        """The temporal index should be in 00-Creek-Meta."""
        result = generator.generate_temporal_index()
        assert result.parent == vault / "00-Creek-Meta"

    def test_file_is_markdown(self, generator: IndexGenerator) -> None:
        """The generated file should have .md extension."""
        result = generator.generate_temporal_index()
        assert result.suffix == ".md"

    def test_has_yaml_frontmatter(self, generator: IndexGenerator) -> None:
        """The temporal index should have YAML frontmatter."""
        result = generator.generate_temporal_index()
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        rest = content[4:]
        assert "\n---\n" in rest

    def test_contains_dataview_query(self, generator: IndexGenerator) -> None:
        """The temporal index should contain Dataview queries."""
        result = generator.generate_temporal_index()
        content = result.read_text(encoding="utf-8")
        assert "```dataview" in content

    def test_contains_temporal_grouping(self, generator: IndexGenerator) -> None:
        """The temporal index should contain year/month/week references."""
        result = generator.generate_temporal_index()
        content = result.read_text(encoding="utf-8")
        # Should reference date-based grouping
        assert "created" in content.lower() or "date" in content.lower()

    def test_frontmatter_contains_type(self, generator: IndexGenerator) -> None:
        """Frontmatter should include a type field."""
        result = generator.generate_temporal_index()
        content = result.read_text(encoding="utf-8")
        assert "type:" in content


# ---- Source Index Tests ----


class TestGenerateSourceIndex:
    """Tests for IndexGenerator.generate_source_index."""

    def test_returns_path(self, generator: IndexGenerator) -> None:
        """Should return a single Path object."""
        result = generator.generate_source_index()
        assert isinstance(result, Path)

    def test_file_exists(self, generator: IndexGenerator) -> None:
        """The generated file should exist on disk."""
        result = generator.generate_source_index()
        assert result.is_file()

    def test_file_in_meta_dir(self, generator: IndexGenerator, vault: Path) -> None:
        """The source index should be in 00-Creek-Meta."""
        result = generator.generate_source_index()
        assert result.parent == vault / "00-Creek-Meta"

    def test_file_is_markdown(self, generator: IndexGenerator) -> None:
        """The generated file should have .md extension."""
        result = generator.generate_source_index()
        assert result.suffix == ".md"

    def test_has_yaml_frontmatter(self, generator: IndexGenerator) -> None:
        """The source index should have YAML frontmatter."""
        result = generator.generate_source_index()
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        rest = content[4:]
        assert "\n---\n" in rest

    def test_contains_dataview_query(self, generator: IndexGenerator) -> None:
        """The source index should contain a Dataview query."""
        result = generator.generate_source_index()
        content = result.read_text(encoding="utf-8")
        assert "```dataview" in content

    def test_references_source_platform(self, generator: IndexGenerator) -> None:
        """The source index should reference source.platform in its query."""
        result = generator.generate_source_index()
        content = result.read_text(encoding="utf-8")
        assert "source" in content.lower()

    def test_frontmatter_contains_type(self, generator: IndexGenerator) -> None:
        """Frontmatter should include a type field."""
        result = generator.generate_source_index()
        content = result.read_text(encoding="utf-8")
        assert "type:" in content


# ---- Generate All Tests ----


class TestGenerateAll:
    """Tests for IndexGenerator.generate_all."""

    def test_returns_list_of_paths(self, generator: IndexGenerator) -> None:
        """Should return a list of Path objects."""
        result = generator.generate_all()
        assert isinstance(result, list)
        assert all(isinstance(p, Path) for p in result)

    def test_generates_all_indexes(self, generator: IndexGenerator) -> None:
        """Should generate at least 14 files total.

        That is 10 frequency + thread + eddy + temporal + source.
        """
        result = generator.generate_all()
        assert len(result) >= 14

    def test_all_files_exist(self, generator: IndexGenerator) -> None:
        """All returned paths should exist on disk."""
        result = generator.generate_all()
        for path in result:
            assert path.is_file(), f"Generated file does not exist: {path}"

    def test_all_files_are_markdown(self, generator: IndexGenerator) -> None:
        """All generated files should have .md extension."""
        result = generator.generate_all()
        for path in result:
            assert path.suffix == ".md"

    def test_all_files_have_frontmatter(self, generator: IndexGenerator) -> None:
        """Every generated file should have YAML frontmatter."""
        result = generator.generate_all()
        for path in result:
            content = path.read_text(encoding="utf-8")
            assert content.startswith("---\n"), f"Missing frontmatter: {path}"

    def test_includes_frequency_indexes(
        self, generator: IndexGenerator, vault: Path
    ) -> None:
        """generate_all should include all 10 frequency indexes."""
        result = generator.generate_all()
        freq_dir = vault / "06-Frequencies"
        freq_files = [
            p for p in result if freq_dir in p.parents or p.parent.parent == freq_dir
        ]
        assert len(freq_files) == 10

    def test_includes_thread_index(
        self, generator: IndexGenerator, vault: Path
    ) -> None:
        """generate_all should include the thread index."""
        result = generator.generate_all()
        threads_dir = vault / "02-Threads"
        thread_files = [p for p in result if p.parent == threads_dir]
        assert len(thread_files) == 1

    def test_includes_eddy_map(self, generator: IndexGenerator, vault: Path) -> None:
        """generate_all should include the eddy map."""
        result = generator.generate_all()
        eddies_dir = vault / "03-Eddies"
        eddy_files = [p for p in result if p.parent == eddies_dir]
        assert len(eddy_files) == 1

    def test_includes_temporal_index(
        self, generator: IndexGenerator, vault: Path
    ) -> None:
        """generate_all should include the temporal index."""
        result = generator.generate_all()
        meta_dir = vault / "00-Creek-Meta"
        meta_files = [p for p in result if p.parent == meta_dir]
        # Should have at least temporal + source = 2 files in meta
        assert len(meta_files) >= 2

    def test_includes_source_index(
        self, generator: IndexGenerator, vault: Path
    ) -> None:
        """generate_all should include the source index."""
        result = generator.generate_all()
        meta_dir = vault / "00-Creek-Meta"
        meta_files = [p for p in result if p.parent == meta_dir]
        assert len(meta_files) >= 2

    def test_idempotent(self, generator: IndexGenerator) -> None:
        """Running generate_all twice should produce the same results."""
        result1 = generator.generate_all()
        result2 = generator.generate_all()
        assert len(result1) == len(result2)
        for p in result1:
            assert p.is_file()


# ---- Edge Case Tests ----


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_missing_frequency_subdirs_creates_files_only_for_existing(
        self, tmp_path: Path
    ) -> None:
        """If some frequency subdirs are missing, only generate for existing ones."""
        # Create vault with only some frequency subdirs
        (tmp_path / "06-Frequencies").mkdir()
        (tmp_path / "06-Frequencies" / "F1-Agency").mkdir()
        (tmp_path / "06-Frequencies" / "F2-Receptivity").mkdir()
        (tmp_path / "00-Creek-Meta").mkdir()
        (tmp_path / "01-Fragments").mkdir()
        (tmp_path / "02-Threads").mkdir()
        (tmp_path / "03-Eddies").mkdir()

        gen = IndexGenerator(tmp_path)
        result = gen.generate_frequency_indexes()
        assert len(result) == 2

    def test_frequency_index_content_structure(self, generator: IndexGenerator) -> None:
        """Frequency index notes should have well-structured content."""
        result = generator.generate_frequency_indexes()
        first = result[0]
        content = first.read_text(encoding="utf-8")

        # Should have frontmatter
        lines = content.split("\n")
        assert lines[0] == "---"

        # Find closing frontmatter delimiter
        closing_idx = None
        for i, line in enumerate(lines[1:], start=1):
            if line == "---":
                closing_idx = i
                break
        assert closing_idx is not None, "No closing frontmatter delimiter found"

        # Content should exist after frontmatter
        body = "\n".join(lines[closing_idx + 1 :])
        assert len(body.strip()) > 0

    def test_generated_notes_use_utf8(self, generator: IndexGenerator) -> None:
        """All generated files should be readable as UTF-8."""
        results = generator.generate_all()
        for path in results:
            # Should not raise UnicodeDecodeError
            path.read_text(encoding="utf-8")
