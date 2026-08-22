"""Tests for creek.generate.tags — Tag Garden generator for Creek vault.

Tests cover the TagGardenGenerator class and all its methods:
scan_tags, detect_growth, detect_clusters, suggest_maintenance,
and generate_garden.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.generate.tags import TagGardenGenerator, TagScanResult

if TYPE_CHECKING:
    from pathlib import Path


# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a mock vault directory structure for testing.

    Sets up all required vault subdirectories including
    00-Creek-Meta/Processing-Log for tag history persistence.
    """
    top_level = [
        "00-Creek-Meta",
        "01-Fragments",
        "02-Threads",
        "03-Eddies",
        "04-Praxis",
        "05-Wavelength",
        "06-Frequencies",
    ]
    for folder in top_level:
        (tmp_path / folder).mkdir()

    (tmp_path / "00-Creek-Meta" / "Processing-Log").mkdir()
    return tmp_path


@pytest.fixture()
def generator(vault: Path) -> TagGardenGenerator:
    """Create a TagGardenGenerator instance with the mock vault path."""
    return TagGardenGenerator(vault)


@pytest.fixture()
def bare_vault(tmp_path: Path) -> Path:
    """Create a vault holding ``01-Fragments/`` and nothing else.

    This fixture deliberately omits ``00-Creek-Meta/`` — and with it
    ``00-Creek-Meta/Processing-Log/``. That omission is the whole point: the
    ``vault`` fixture above pre-creates both, so it cannot express the state a
    real vault is in before anything has scaffolded the meta folder, which is
    exactly the state in which ``generate_garden`` used to die on a missing
    parent directory (issue #1318). ``01-Fragments/`` is present because
    ``_write_fragment`` writes into it, and because a vault with no content at
    all would make the assertions about the seeded tag vacuous.
    """
    (tmp_path / "01-Fragments").mkdir()
    return tmp_path


def _write_fragment(vault: Path, name: str, tags: list[str]) -> Path:
    """Write a mock fragment markdown file with frontmatter tags.

    Args:
        vault: Root vault path.
        name: Filename (without extension).
        tags: List of tags to include in frontmatter.

    Returns:
        Path to the created markdown file.
    """
    tag_yaml = "\n".join(f"  - {t}" for t in tags)
    content = f"""---
type: fragment
id: frag-{name}
title: "{name}"
tags:
{tag_yaml}
---

Content of {name}.
"""
    path = vault / "01-Fragments" / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---- TagScanResult Tests ----


class TestTagScanResult:
    """Tests for the TagScanResult data class."""

    def test_creation(self) -> None:
        """TagScanResult can be created with required fields."""
        result = TagScanResult(
            tag_counts={"python": 5},
            tag_fragments={"python": ["frag-1"]},
        )
        assert result.tag_counts == {"python": 5}
        assert result.tag_fragments == {"python": ["frag-1"]}

    def test_empty_result(self) -> None:
        """TagScanResult can be created with empty data."""
        result = TagScanResult(tag_counts={}, tag_fragments={})
        assert result.tag_counts == {}
        assert result.tag_fragments == {}


# ---- TagGardenGenerator Init Tests ----


class TestTagGardenGeneratorInit:
    """Tests for TagGardenGenerator.__init__."""

    def test_stores_vault_path(self, vault: Path) -> None:
        """Generator stores the vault path."""
        gen = TagGardenGenerator(vault)
        assert gen.vault_path == vault

    def test_history_path(self, vault: Path) -> None:
        """Generator computes the history file path correctly."""
        gen = TagGardenGenerator(vault)
        expected = vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        assert gen.history_path == expected


# ---- scan_tags Tests ----


class TestScanTags:
    """Tests for TagGardenGenerator.scan_tags."""

    def test_empty_vault_returns_empty(self, generator: TagGardenGenerator) -> None:
        """Scanning an empty vault returns empty counts."""
        result = generator.scan_tags()
        assert result.tag_counts == {}
        assert result.tag_fragments == {}

    def test_single_fragment_single_tag(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """A single fragment with one tag produces count of 1."""
        _write_fragment(vault, "note1", ["python"])
        result = generator.scan_tags()
        assert result.tag_counts == {"python": 1}
        assert result.tag_fragments == {"python": ["frag-note1"]}

    def test_multiple_fragments_same_tag(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Multiple fragments sharing a tag produces correct count."""
        _write_fragment(vault, "note1", ["python"])
        _write_fragment(vault, "note2", ["python"])
        result = generator.scan_tags()
        assert result.tag_counts == {"python": 2}
        assert len(result.tag_fragments["python"]) == 2

    def test_multiple_tags_per_fragment(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """A fragment with multiple tags counts each tag independently."""
        _write_fragment(vault, "note1", ["python", "coding", "tutorial"])
        result = generator.scan_tags()
        assert result.tag_counts == {"python": 1, "coding": 1, "tutorial": 1}

    def test_scans_all_vault_directories(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Scanner reads tags from fragments, threads, and eddies."""
        _write_fragment(vault, "note1", ["python"])

        # Write a thread with tags
        thread_content = """---
type: thread
id: thread-test1
title: "Test Thread"
tags:
  - architecture
---

Thread content.
"""
        (vault / "02-Threads" / "thread1.md").write_text(
            thread_content,
            encoding="utf-8",
        )
        result = generator.scan_tags()
        assert "python" in result.tag_counts
        assert "architecture" in result.tag_counts

    def test_handles_fragment_without_tags(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Fragments without tags field are handled gracefully."""
        no_tag_content = """---
type: fragment
id: frag-notags
title: "No Tags"
---

Content without tags.
"""
        (vault / "01-Fragments" / "notags.md").write_text(
            no_tag_content,
            encoding="utf-8",
        )
        result = generator.scan_tags()
        assert result.tag_counts == {}

    def test_non_string_id_falls_back_to_filename(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """A fragment whose `id` is not a string is recorded by filename."""
        weird = """---
type: fragment
id:
  - not
  - a-string
title: "Weird ID"
tags:
  - python
---

Body.
"""
        (vault / "01-Fragments" / "weird-id.md").write_text(
            weird,
            encoding="utf-8",
        )
        result = generator.scan_tags()
        assert result.tag_fragments["python"] == ["weird-id"]


# ---- detect_growth Tests ----


class TestDetectGrowth:
    """Tests for TagGardenGenerator.detect_growth."""

    def test_no_history_all_tags_are_new(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """With no previous scan, all tags should be flagged as new."""
        current = TagScanResult(
            tag_counts={"python": 3, "rust": 1},
            tag_fragments={"python": ["f1", "f2", "f3"], "rust": ["f4"]},
        )
        growth = generator.detect_growth(current, previous_counts=None)
        assert "python" in growth["new_tags"]
        assert "rust" in growth["new_tags"]
        assert growth["growing_tags"] == {}

    def test_detects_growth_over_fifty_percent(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """Tags with >50% growth are flagged as growing."""
        current = TagScanResult(
            tag_counts={"python": 8},
            tag_fragments={"python": ["f1"]},
        )
        previous = {"python": 4}
        growth = generator.detect_growth(current, previous_counts=previous)
        assert "python" in growth["growing_tags"]
        assert growth["growing_tags"]["python"]["previous"] == 4
        assert growth["growing_tags"]["python"]["current"] == 8

    def test_no_growth_under_threshold(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """Tags with <=50% growth are not flagged."""
        current = TagScanResult(
            tag_counts={"python": 6},
            tag_fragments={"python": ["f1"]},
        )
        previous = {"python": 5}
        growth = generator.detect_growth(current, previous_counts=previous)
        assert "python" not in growth["growing_tags"]
        assert growth["new_tags"] == []

    def test_new_tag_detected(self, generator: TagGardenGenerator) -> None:
        """Tags not in previous scan are flagged as new."""
        current = TagScanResult(
            tag_counts={"python": 3, "rust": 1},
            tag_fragments={"python": ["f1"], "rust": ["f2"]},
        )
        previous = {"python": 2}
        growth = generator.detect_growth(current, previous_counts=previous)
        assert "rust" in growth["new_tags"]


# ---- detect_clusters Tests ----


class TestDetectClusters:
    """Tests for TagGardenGenerator.detect_clusters."""

    def test_empty_scan_no_clusters(self, generator: TagGardenGenerator) -> None:
        """Empty scan produces no clusters."""
        scan = TagScanResult(tag_counts={}, tag_fragments={})
        clusters = generator.detect_clusters(scan)
        assert clusters == []

    def test_co_occurring_tags_form_cluster(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Tags that frequently co-occur on the same fragments form clusters."""
        _write_fragment(vault, "note1", ["python", "coding"])
        _write_fragment(vault, "note2", ["python", "coding"])
        _write_fragment(vault, "note3", ["python", "coding"])
        scan = generator.scan_tags()
        clusters = generator.detect_clusters(scan)
        # python and coding co-occur on 3 fragments
        tag_pairs = [(c["tags"][0], c["tags"][1]) for c in clusters]
        assert any(("coding" in pair and "python" in pair) for pair in tag_pairs)

    def test_non_co_occurring_tags_no_cluster(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Tags that never appear together don't form clusters."""
        _write_fragment(vault, "note1", ["python"])
        _write_fragment(vault, "note2", ["cooking"])
        scan = generator.scan_tags()
        clusters = generator.detect_clusters(scan)
        tag_pairs = [tuple(sorted(c["tags"])) for c in clusters]
        assert ("cooking", "python") not in tag_pairs


# ---- suggest_maintenance Tests ----


class TestSuggestMaintenance:
    """Tests for TagGardenGenerator.suggest_maintenance."""

    def test_suggests_merge_for_similar_tags(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """Tags with edit distance <3 should be suggested for merging."""
        scan = TagScanResult(
            tag_counts={"python": 5, "pythons": 3},
            tag_fragments={"python": ["f1"], "pythons": ["f2"]},
        )
        suggestions = generator.suggest_maintenance(scan)
        merge_pairs = [
            (s["tag_a"], s["tag_b"]) for s in suggestions if s["type"] == "merge"
        ]
        assert any(("python" in pair and "pythons" in pair) for pair in merge_pairs)

    def test_suggests_split_for_broad_tags(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """Tags with >100 uses should be suggested for splitting."""
        scan = TagScanResult(
            tag_counts={"general": 101},
            tag_fragments={"general": [f"f{i}" for i in range(101)]},
        )
        suggestions = generator.suggest_maintenance(scan)
        split_tags = [s["tag"] for s in suggestions if s["type"] == "split"]
        assert "general" in split_tags

    def test_flags_orphan_tags(self, generator: TagGardenGenerator) -> None:
        """Tags with only 1 use should be flagged as orphans."""
        scan = TagScanResult(
            tag_counts={"rare-tag": 1},
            tag_fragments={"rare-tag": ["f1"]},
        )
        suggestions = generator.suggest_maintenance(scan)
        orphans = [s["tag"] for s in suggestions if s["type"] == "orphan"]
        assert "rare-tag" in orphans

    def test_no_suggestions_for_healthy_tags(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """Tags with moderate usage and unique names produce no suggestions."""
        scan = TagScanResult(
            tag_counts={"python": 10, "cooking": 15},
            tag_fragments={
                "python": [f"f{i}" for i in range(10)],
                "cooking": [f"g{i}" for i in range(15)],
            },
        )
        suggestions = generator.suggest_maintenance(scan)
        assert suggestions == []


# ---- generate_garden Tests ----


class TestGenerateGarden:
    """Tests for TagGardenGenerator.generate_garden."""

    def test_generates_markdown_file(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """generate_garden creates a Tag-Garden.md file."""
        _write_fragment(vault, "note1", ["python"])
        path = generator.generate_garden()
        assert path.exists()
        assert path.name == "Tag-Garden.md"
        assert path.parent == vault / "00-Creek-Meta"

    def test_output_contains_frontmatter(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Generated file has YAML frontmatter."""
        _write_fragment(vault, "note1", ["python"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "type: tag-garden" in content

    def test_output_contains_all_tags_section(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Generated file has an All Tags section listing tags with counts."""
        _write_fragment(vault, "note1", ["python", "coding"])
        _write_fragment(vault, "note2", ["python"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "## All Tags" in content
        assert "python" in content
        assert "coding" in content

    def test_output_contains_growing_tags_section(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Generated file has a Growing Tags section."""
        _write_fragment(vault, "note1", ["python"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "## Growing Tags" in content

    def test_output_contains_clusters_section(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Generated file has a Clusters section."""
        _write_fragment(vault, "note1", ["python"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "## Tag Clusters" in content

    def test_output_contains_suggestions_section(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Generated file has a Maintenance Suggestions section."""
        _write_fragment(vault, "note1", ["python"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "## Maintenance Suggestions" in content

    def test_empty_vault_produces_valid_output(
        self,
        generator: TagGardenGenerator,
    ) -> None:
        """An empty vault still produces a valid Tag Garden file."""
        path = generator.generate_garden()
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "## All Tags" in content

    def test_persists_history(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """generate_garden saves scan history to tag-history.json."""
        _write_fragment(vault, "note1", ["python"])
        generator.generate_garden()
        history_path = vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        assert history_path.exists()
        history = json.loads(history_path.read_text(encoding="utf-8"))
        assert len(history) == 1
        assert history[0]["tag_counts"]["python"] == 1

    def test_loads_previous_history(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """generate_garden loads previous scan for growth detection.

        The seeded entry states ``tier_ceiling`` since #968. A history entry is
        only a valid growth baseline for a scan taken at the *same* ceiling, and
        a legacy entry that records none is skipped as non-comparable — so
        without the key this test would still pass while exercising none of the
        growth path, because ``python`` also appears in the unconditional "All
        Tags" table. ``"all"`` is what the ``generator`` fixture runs at
        (``TagGardenGenerator``'s default ``PrivacyTierOverride.ALL``).

        The assertion is on the "Rapidly Growing" row rather than on the bare
        tag name, for the same reason: only that row can distinguish "the
        baseline was loaded and compared" from "the tag exists".
        """
        # Write initial history
        history_path = vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        initial_history = [
            {
                "tag_counts": {"python": 2},
                "timestamp": "2026-01-01T00:00:00",
                "tier_ceiling": "all",
            },
        ]
        history_path.write_text(
            json.dumps(initial_history),
            encoding="utf-8",
        )

        # Add fragments that cause >50% growth
        _write_fragment(vault, "note1", ["python"])
        _write_fragment(vault, "note2", ["python"])
        _write_fragment(vault, "note3", ["python"])
        _write_fragment(vault, "note4", ["python"])

        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        # python grew from 2 to 4 (100% growth), should appear in growing section
        assert "python" in content
        assert "| python | 2 | 4 | +100% |" in content

    def test_renders_clusters_in_output(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Clusters with co-occurring tags render in the Tag Clusters table."""
        _write_fragment(vault, "a", ["python", "coding"])
        _write_fragment(vault, "b", ["python", "coding"])
        _write_fragment(vault, "c", ["python", "coding"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "| Tag A | Tag B | Co-occurrences |" in content

    def test_renders_merge_and_orphan_suggestions(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Similar tags and orphan tags render in Maintenance Suggestions."""
        # Create similar tags to trigger merge suggestion
        for i in range(5):
            _write_fragment(vault, f"py{i}", ["python"])
        for i in range(3):
            _write_fragment(vault, f"pys{i}", ["pythons"])
        # Create orphan tag
        _write_fragment(vault, "rare", ["xylophone-solo"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "### Merge Candidates" in content
        assert "### Orphan Tags" in content
        assert "xylophone-solo" in content

    def test_renders_split_suggestion_for_broad_tag(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """Tags with >100 uses render as split candidates."""
        for i in range(101):
            _write_fragment(vault, f"note{i}", ["broad-tag"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "### Split Candidates" in content
        assert "broad-tag" in content

    def test_truncates_fragment_links_over_five(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """All Tags table truncates fragment links after 5."""
        for i in range(7):
            _write_fragment(vault, f"note{i}", ["popular"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        assert "..." in content

    def test_empty_history_file_treated_as_no_history(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """An empty JSON array in history is treated as no previous data."""
        history_path = vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        history_path.write_text("[]", encoding="utf-8")
        _write_fragment(vault, "note1", ["python"])
        path = generator.generate_garden()
        content = path.read_text(encoding="utf-8")
        # With no previous history, all tags are "new"
        assert "### New Tags" in content


# ---- The Tag Garden is reachable once ``tags`` has a producer (#878) ----


class TestTagGardenIsReachableOnceFragmentsCarryTags:
    """The reader was always fine; the vault was always empty (issue #878).

    ``Fragment.tags`` defaulted to ``[]`` and had essentially no
    production writer, so on the operator's 35,330-fragment vault
    ``00-Creek-Meta/Tag-Garden.md`` read ``*No tags found in vault.*`` and
    every section below it was blank. **No production change is needed
    here** — this class is the regression proof that the whole generator
    (scan → growth → clusters → render) lights up the moment the ingest
    and classify passes start writing the field, and that a future change
    to the tags pass cannot silently re-empty the garden.

    It reuses the module's existing ``vault`` fixture and
    ``_write_fragment`` helper deliberately: seeding through the same
    on-disk shape every other test in this file uses is what keeps the
    assertion honest.
    """

    def test_a_tagged_vault_renders_a_populated_garden(
        self,
        generator: TagGardenGenerator,
        vault: Path,
    ) -> None:
        """All-Tags, Clusters and Growing Tags are all non-empty (AC-2).

        One rendered document, four assertions, each pinned to a
        different section so a partial regression cannot pass:

        * the "no tags" sentinel is **absent** — the exact string the
          operator's real vault showed;
        * the All-Tags table names the seeded tags;
        * ``recovery`` and ``writing`` co-occur on three fragments and so
          form a cluster row;
        * ``recovery`` grew 2 → 4 against the seeded history, which is
          the only section that can distinguish "the baseline was loaded
          and compared" from "the tag exists somewhere in the file".

        The history entry states ``tier_ceiling: all`` because since #968
        an entry that records none is skipped as a non-comparable
        baseline, and ``all`` is what the ``generator`` fixture runs at.
        """
        history_path = vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        history_path.write_text(
            json.dumps(
                [
                    {
                        "tag_counts": {"recovery": 2},
                        "timestamp": "2026-01-01T00:00:00",
                        "tier_ceiling": "all",
                    },
                ],
            ),
            encoding="utf-8",
        )
        for name in ("note1", "note2", "note3"):
            _write_fragment(vault, name, ["recovery", "writing"])
        _write_fragment(vault, "note4", ["recovery"])

        content = generator.generate_garden().read_text(encoding="utf-8")

        assert "No tags found in vault." not in content
        assert "recovery" in content
        assert "writing" in content
        assert "| Tag A | Tag B | Co-occurrences |" in content
        assert "| recovery | 2 | 4 | +100% |" in content


# ---- The garden scaffolds its own parent directory (#1318) ----


class TestGenerateGardenOnAnUnscaffoldedVault:
    """``generate_garden`` must create ``00-Creek-Meta/`` instead of crashing.

    Every sibling report generator makes its output directory on demand (the
    #1231 convention); this one wrote straight to
    ``00-Creek-Meta/Tag-Garden.md``, so on a vault where that folder does not
    exist the very first write raised ``FileNotFoundError`` and nothing after
    it ran — history persistence included, even though ``_save_history`` does
    create its own parent, because it never got the chance.

    These tests use ``bare_vault`` rather than the module's ``vault`` fixture:
    ``vault`` pre-creates ``00-Creek-Meta`` and its ``Processing-Log``, so it
    cannot express the defect at all.
    """

    def test_writes_the_garden_when_00_creek_meta_is_absent(
        self,
        bare_vault: Path,
    ) -> None:
        """The garden is written even though its parent folder is missing.

        The absence of ``00-Creek-Meta`` is asserted *before* the call, so the
        test proves it exercised the intended precondition and cannot quietly
        rot into a duplicate of the happy-path cases above if a future fixture
        change starts scaffolding the folder. Before the fix, the call raises
        ``FileNotFoundError`` from ``write_text``.
        """
        _write_fragment(bare_vault, "note1", ["recovery"])
        assert not (bare_vault / "00-Creek-Meta").exists()

        path = TagGardenGenerator(bare_vault).generate_garden()

        assert path == bare_vault / "00-Creek-Meta" / "Tag-Garden.md"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "recovery" in content
        assert "type: tag-garden" in content

    def test_history_is_persisted_when_00_creek_meta_is_absent(
        self,
        bare_vault: Path,
    ) -> None:
        """The scan history is saved too, so the whole call completed.

        ``_save_history`` runs *after* the line that used to crash, which makes
        this the assertion that distinguishes "the first write was repaired"
        from "the method now runs end to end". The counts are compared by value
        rather than checked for mere presence, so a history file written with
        an empty or placeholder entry cannot pass.
        """
        _write_fragment(bare_vault, "note1", ["recovery"])

        TagGardenGenerator(bare_vault).generate_garden()

        history_path = (
            bare_vault / "00-Creek-Meta" / "Processing-Log" / "tag-history.json"
        )
        assert history_path.exists()
        history = json.loads(history_path.read_text(encoding="utf-8"))
        assert len(history) == 1
        assert history[0]["tag_counts"] == {"recovery": 1}

    def test_an_existing_creek_meta_folder_and_its_contents_survive(
        self,
        tmp_path: Path,
    ) -> None:
        """Creating the folder is idempotent and clobbers nothing already there.

        Built from ``tmp_path`` rather than ``bare_vault`` because this case
        needs the opposite precondition: ``00-Creek-Meta`` already exists and
        already holds an unrelated file. A guard that forgets ``exist_ok`` —
        or that removes and recreates the directory — still passes every test
        above but fails here, on ``FileExistsError`` or on the sentinel's
        exact bytes. The generator is run twice for the same reason: the
        second pass exercises the path where the folder exists because *this*
        code made it, not because the vault was scaffolded.
        """
        (tmp_path / "01-Fragments").mkdir()
        (tmp_path / "00-Creek-Meta").mkdir()
        sentinel = tmp_path / "00-Creek-Meta" / "creek_config.yaml"
        sentinel_bytes = b"vault_path: /somewhere\n"
        sentinel.write_bytes(sentinel_bytes)
        _write_fragment(tmp_path, "note1", ["recovery"])

        generator = TagGardenGenerator(tmp_path)
        generator.generate_garden()
        path = generator.generate_garden()

        assert sentinel.read_bytes() == sentinel_bytes
        assert path.exists()
        assert path.parent == tmp_path / "00-Creek-Meta"

    def test_the_only_directory_created_is_under_the_vault_root(
        self,
        bare_vault: Path,
    ) -> None:
        """Scaffolding stays inside the vault, and covers only what is written.

        "Nothing was created outside the vault" cannot be asserted against the
        whole filesystem without a brittle walk, so it is expressed two ways
        that are both cheap and deterministic: the output path is relative to
        the vault root and the new ``00-Creek-Meta`` is a *direct child* of it
        rather than a sibling or an absolute path elsewhere; and the vault's
        own parent directory gains no entry, which is the blast radius a
        ``mkdir(parents=True)`` against a mistyped root would show up in.

        The directory inventory inside the vault is then pinned exactly. A
        report generator may create the folder it writes into, plus
        ``Processing-Log`` for its history — it is not a vault scaffolder, and
        must not materialise the other top-level folders on the way past.
        """
        _write_fragment(bare_vault, "note1", ["recovery"])
        siblings_before = set(bare_vault.parent.iterdir())

        path = TagGardenGenerator(bare_vault).generate_garden()

        meta_dir = bare_vault / "00-Creek-Meta"
        assert meta_dir.is_dir()
        assert meta_dir.parent == bare_vault
        assert path.is_relative_to(bare_vault)
        assert set(bare_vault.parent.iterdir()) == siblings_before
        created = {
            child.relative_to(bare_vault).as_posix()
            for child in bare_vault.rglob("*")
            if child.is_dir()
        }
        assert created == {
            "01-Fragments",
            "00-Creek-Meta",
            "00-Creek-Meta/Processing-Log",
        }
