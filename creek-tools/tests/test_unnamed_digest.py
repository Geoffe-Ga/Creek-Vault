"""Tests for creek.generate.unnamed — Unnamed weekly digest generator.

Tests cover the ``UnnamedDigestGenerator`` class:

- Collecting fragments from ``10-Liminal/Unnamed/`` in a target week
- Clustering unnamed fragments via embedding similarity
- Rendering the weekly digest markdown note with all required sections
- Tracking week-over-week growth in a JSON history file
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek.config import EmbeddingsConfig
from creek.generate.unnamed import (
    UnnamedDigestGenerator,
    _connected_components,
    _iter_unnamed_records,
    _read_history_entries,
    _render_fragments_section,
)
from creek.link.embeddings import EmbeddingLinker
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.conftest import CORRUPT_NOTE_SHAPES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a mock vault directory structure including 10-Liminal/Unnamed/."""
    for folder in (
        "00-Creek-Meta",
        "00-Creek-Meta/Processing-Log",
        "01-Fragments",
        "10-Liminal",
        "10-Liminal/Unnamed",
    ):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def linker() -> EmbeddingLinker:
    """Create an EmbeddingLinker backed by the autouse mock model."""
    return EmbeddingLinker(config=EmbeddingsConfig())


@pytest.fixture()
def generator(linker: EmbeddingLinker) -> UnnamedDigestGenerator:
    """Create a default UnnamedDigestGenerator for testing."""
    return UnnamedDigestGenerator(embedding_linker=linker)


def _week_start(year: int = 2026, month: int = 2, day: int = 9) -> date:
    """Return a canonical Monday for deterministic week boundaries."""
    return date(year, month, day)


def _write_unnamed_fragment(
    vault: Path,
    name: str,
    *,
    title: str | None = None,
    created: datetime,
    frag_id: str | None = None,
) -> Path:
    """Write an Unnamed fragment markdown file with full frontmatter.

    Args:
        vault: Root vault path.
        name: Filename stem (without extension).
        title: Fragment title; defaults to *name*.
        created: Creation timestamp used for the ``created`` field.
        frag_id: Fragment ID; defaults to ``frag-<name>``.

    Returns:
        Path to the written markdown file.
    """
    fragment = Fragment(
        id=frag_id or f"frag-{name}",
        title=title or name,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        ingested=created,
    )
    data = fragment.model_dump(mode="json")
    post = frontmatter.Post(content=f"Body content for {name}.", **data)
    path = vault / "10-Liminal" / "Unnamed" / f"{name}.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


# ---- Init Tests ----


class TestInit:
    """Tests for UnnamedDigestGenerator.__init__."""

    def test_stores_linker(self, linker: EmbeddingLinker) -> None:
        """Init stores the provided embedding linker."""
        gen = UnnamedDigestGenerator(embedding_linker=linker)
        assert gen.embedding_linker is linker

    def test_default_similarity_threshold_is_low(self, linker: EmbeddingLinker) -> None:
        """Default similarity threshold is 0.7 per ontology spec."""
        gen = UnnamedDigestGenerator(embedding_linker=linker)
        assert gen.similarity_threshold == pytest.approx(0.7)

    def test_custom_similarity_threshold(self, linker: EmbeddingLinker) -> None:
        """Custom threshold overrides the default."""
        gen = UnnamedDigestGenerator(embedding_linker=linker, similarity_threshold=0.85)
        assert gen.similarity_threshold == pytest.approx(0.85)


# ---- load_unnamed_fragments ----


class TestLoadUnnamedFragments:
    """Tests for UnnamedDigestGenerator.load_unnamed_fragments."""

    def test_empty_dir_returns_empty(
        self, generator: UnnamedDigestGenerator, vault: Path
    ) -> None:
        """An empty Unnamed/ directory yields no fragments."""
        assert generator.load_unnamed_fragments(vault) == []

    def test_reads_fragments_from_unnamed(
        self, generator: UnnamedDigestGenerator, vault: Path
    ) -> None:
        """Unnamed/*.md markdown files are parsed into Fragment models."""
        when = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
        _write_unnamed_fragment(vault, "alpha", created=when)
        _write_unnamed_fragment(vault, "beta", created=when)
        loaded = generator.load_unnamed_fragments(vault)
        titles = sorted(f.title for f in loaded)
        assert titles == ["alpha", "beta"]

    def test_skips_digests_subdirectory(
        self, generator: UnnamedDigestGenerator, vault: Path
    ) -> None:
        """The Digests/ subdirectory is excluded from the fragment list."""
        when = datetime(2026, 2, 10, tzinfo=UTC)
        _write_unnamed_fragment(vault, "alpha", created=when)
        digests = vault / "10-Liminal" / "Unnamed" / "Digests"
        digests.mkdir(parents=True, exist_ok=True)
        (digests / "2026-W06.md").write_text("---\ntype: digest\n---\n", "utf-8")
        loaded = generator.load_unnamed_fragments(vault)
        assert len(loaded) == 1
        assert loaded[0].title == "alpha"

    def test_skips_non_fragment_markdown(
        self, generator: UnnamedDigestGenerator, vault: Path
    ) -> None:
        """Markdown files that don't parse as Fragment are silently skipped."""
        when = datetime(2026, 2, 10, tzinfo=UTC)
        _write_unnamed_fragment(vault, "alpha", created=when)
        bad = vault / "10-Liminal" / "Unnamed" / "stray.md"
        bad.write_text("---\ntype: not-a-fragment\n---\n", "utf-8")
        loaded = generator.load_unnamed_fragments(vault)
        assert [f.title for f in loaded] == ["alpha"]

    def test_missing_unnamed_dir_returns_empty(
        self, generator: UnnamedDigestGenerator, tmp_path: Path
    ) -> None:
        """A missing 10-Liminal/Unnamed/ directory returns an empty list."""
        assert generator.load_unnamed_fragments(tmp_path) == []


# ---- filter_fragments_in_week ----


class TestFilterFragmentsInWeek:
    """Tests for UnnamedDigestGenerator.filter_fragments_in_week."""

    def test_includes_week_start_inclusive(
        self, generator: UnnamedDigestGenerator
    ) -> None:
        """Fragments created exactly at ``week_start`` are included.

        FEAT-031: ``filter_fragments_in_week`` now buckets via
        ``effective_authored_date`` (``authored_at`` → ``ingested``);
        the test mirrors ``created`` into ``ingested`` so the fallback
        sees the date the test intends.
        """
        week_start = _week_start()
        when = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        frag = Fragment(
            id="frag-000000000024",
            title="boundary-low",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=when,
            ingested=when,
        )
        result = generator.filter_fragments_in_week([frag], week_start)
        assert result == [frag]

    def test_excludes_week_end_exclusive(
        self, generator: UnnamedDigestGenerator
    ) -> None:
        """Fragments created on ``week_start + 7`` are excluded."""
        week_start = _week_start()
        when = datetime.combine(
            week_start + timedelta(days=7), datetime.min.time(), tzinfo=UTC
        )
        frag = Fragment(
            id="frag-000000000023",
            title="boundary-high",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=when,
            ingested=when,
        )
        assert generator.filter_fragments_in_week([frag], week_start) == []

    def test_excludes_previous_week(self, generator: UnnamedDigestGenerator) -> None:
        """Fragments from before the window are excluded."""
        week_start = _week_start()
        when = datetime.combine(
            week_start - timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        frag = Fragment(
            id="frag-000000000022",
            title="old",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            created=when,
            ingested=when,
        )
        assert generator.filter_fragments_in_week([frag], week_start) == []


# ---- detect_unnamed_clusters ----


class TestDetectUnnamedClusters:
    """Tests for UnnamedDigestGenerator.detect_unnamed_clusters."""

    def test_empty_input_returns_empty(self, generator: UnnamedDigestGenerator) -> None:
        """Empty input yields no clusters."""
        assert generator.detect_unnamed_clusters([]) == []

    def test_single_fragment_no_clusters(
        self, generator: UnnamedDigestGenerator
    ) -> None:
        """A single fragment cannot form a cluster."""
        frag = Fragment(
            id="frag-000000000021",
            title="solo",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        assert generator.detect_unnamed_clusters([frag]) == []

    def test_identical_titles_cluster_together(self, linker: EmbeddingLinker) -> None:
        """Identical titles yield identical embeddings and form one cluster."""
        gen = UnnamedDigestGenerator(embedding_linker=linker, similarity_threshold=0.7)
        frag_a = Fragment(
            id="frag-000000000020",
            title="recurring-dream",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        frag_b = Fragment(
            id="frag-00000000001f",
            title="recurring-dream",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        clusters = gen.detect_unnamed_clusters([frag_a, frag_b])
        assert len(clusters) == 1
        assert {f.id for f in clusters[0]} == {frag_a.id, frag_b.id}

    def test_threshold_gates_cluster_membership(self, linker: EmbeddingLinker) -> None:
        """An impossibly high threshold collapses all clusters to singletons."""
        gen = UnnamedDigestGenerator(embedding_linker=linker, similarity_threshold=1.01)
        frag_a = Fragment(
            id="frag-00000000001e",
            title="same-string",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        frag_b = Fragment(
            id="frag-00000000001d",
            title="same-string",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        assert gen.detect_unnamed_clusters([frag_a, frag_b]) == []

    def test_distinct_fragments_do_not_cluster(self, linker: EmbeddingLinker) -> None:
        """Unrelated titles produce no cluster at a near-unit threshold.

        The autouse ``mock_sentence_transformer`` fixture returns a
        deterministic random vector per input string; distinct strings
        therefore hash to near-orthogonal 384-dim vectors whose cosine
        similarity is nowhere near ``0.99``. Setting the threshold that
        high asserts the clustering path is actually evaluating pair
        similarity (not blindly grouping).
        """
        gen = UnnamedDigestGenerator(embedding_linker=linker, similarity_threshold=0.99)
        frag_a = Fragment(
            id="frag-00000000001c",
            title="alpha",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        frag_b = Fragment(
            id="frag-00000000001b",
            title="bravo",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        frag_c = Fragment(
            id="frag-00000000001a",
            title="charlie",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        assert gen.detect_unnamed_clusters([frag_a, frag_b, frag_c]) == []


# ---- generate_weekly_digest ----


class TestGenerateWeeklyDigest:
    """Tests for UnnamedDigestGenerator.generate_weekly_digest."""

    def test_creates_digest_in_expected_directory(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """The digest is written to 10-Liminal/Unnamed/Digests/."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        path = generator.generate_weekly_digest(vault, week_start)
        assert path.exists()
        assert path.parent == vault / "10-Liminal" / "Unnamed" / "Digests"

    def test_digest_filename_uses_iso_week(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Digest filename encodes ISO year-week (e.g. 2026-W07)."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        path = generator.generate_weekly_digest(vault, week_start)
        iso_year, iso_week, _ = week_start.isocalendar()
        assert path.name == f"{iso_year}-W{iso_week:02d}.md"

    def test_digest_frontmatter(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Digest frontmatter includes type, week_start, and week_end.

        Parses via :mod:`frontmatter` so the assertions are insensitive
        to the library's quoting style.
        """
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        path = generator.generate_weekly_digest(vault, week_start)
        post = frontmatter.load(str(path))
        assert post.get("type") == "unnamed-digest"
        assert post.get("week_start") == week_start.isoformat()
        assert post.get("week_end") == (week_start + timedelta(days=6)).isoformat()

    def test_digest_lists_fragments_with_excerpts(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Digest includes each fragment's title, wiki-link, and excerpt."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            title="A curious recurring feeling",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-alpha-1",
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")
        assert "## Fragments This Week" in content
        assert "A curious recurring feeling" in content
        assert "[[frag-alpha-1]]" in content
        assert "Body content for alpha" in content

    def test_digest_includes_prompt_section(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Digest includes the open-ended reflection prompt."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")
        assert "## Reflection Prompt" in content
        assert (
            "What do these have in common that the current ontology can't express?"
            in content
        )

    def test_digest_includes_growth_section(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Digest reports week-over-week growth of the Unnamed folder."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")
        assert "## Week-over-Week Growth" in content
        assert "This week" in content

    def test_digest_reports_cluster_when_titles_repeat(
        self,
        linker: EmbeddingLinker,
        vault: Path,
    ) -> None:
        """Duplicate titles produce a similarity cluster in the digest output."""
        gen = UnnamedDigestGenerator(embedding_linker=linker, similarity_threshold=0.7)
        week_start = _week_start()
        when = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        _write_unnamed_fragment(
            vault, "a", title="same phrase", frag_id="frag-a", created=when
        )
        _write_unnamed_fragment(
            vault, "b", title="same phrase", frag_id="frag-b", created=when
        )
        path = gen.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")
        assert "## Similarity Clusters" in content
        assert "Cluster 1" in content
        assert "[[frag-a]]" in content
        assert "[[frag-b]]" in content

    def test_digest_reports_no_clusters_when_none_found(
        self,
        linker: EmbeddingLinker,
        vault: Path,
    ) -> None:
        """When no clusters meet the threshold, the digest says so."""
        gen = UnnamedDigestGenerator(embedding_linker=linker, similarity_threshold=1.01)
        week_start = _week_start()
        when = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        _write_unnamed_fragment(vault, "a", title="unique-a", created=when)
        _write_unnamed_fragment(vault, "b", title="unique-b", created=when)
        path = gen.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")
        assert "## Similarity Clusters" in content
        assert "No similarity clusters emerged this week." in content

    def test_digest_ignores_fragments_outside_window(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Fragments outside the week window aren't listed in this week's digest."""
        week_start = _week_start()
        in_week = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        out_of_week = datetime.combine(
            week_start - timedelta(days=3), datetime.min.time(), tzinfo=UTC
        )
        _write_unnamed_fragment(
            vault,
            "in",
            title="in-window",
            frag_id="frag-in",
            created=in_week,
        )
        _write_unnamed_fragment(
            vault,
            "out",
            title="out-of-window",
            frag_id="frag-out",
            created=out_of_week,
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")
        assert "[[frag-in]]" in content
        assert "[[frag-out]]" not in content

    def test_digest_handles_empty_week(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """An empty week still produces a digest file with an empty-week message."""
        path = generator.generate_weekly_digest(vault, _week_start())
        content = path.read_text(encoding="utf-8")
        assert "No unnamed fragments recorded this week." in content

    def test_persists_history_entry(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Each run appends to 00-Creek-Meta/Processing-Log/unnamed-history.json."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        generator.generate_weekly_digest(vault, week_start)
        history_path = (
            vault / "00-Creek-Meta" / "Processing-Log" / "unnamed-history.json"
        )
        assert history_path.exists()
        history = json.loads(history_path.read_text(encoding="utf-8"))
        assert len(history) == 1
        assert history[0]["week_start"] == week_start.isoformat()
        assert history[0]["fragment_count"] == 1
        assert history[0]["total_unnamed"] == 1

    def test_regenerating_same_week_is_byte_idempotent(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Re-running the same week rewrites neither the digest nor the history.

        Guards two non-idempotency sources fixed for #736: the wall-clock
        ``generated`` stamp (now preserved when content is unchanged) and the
        self-referential growth section (history now excludes the current week
        and upserts its row instead of appending a duplicate).
        """
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        history_path = (
            vault / "00-Creek-Meta" / "Processing-Log" / "unnamed-history.json"
        )

        first = generator.generate_weekly_digest(vault, week_start)
        digest_bytes = first.read_bytes()
        history_bytes = history_path.read_bytes()

        second = generator.generate_weekly_digest(vault, week_start)

        assert second == first
        assert second.read_bytes() == digest_bytes  # digest unchanged on re-run
        assert history_path.read_bytes() == history_bytes  # history did not grow
        assert len(json.loads(history_path.read_text(encoding="utf-8"))) == 1

    def test_reports_growth_against_previous_week(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Subsequent runs report delta vs. the previous recorded week."""
        week_one = _week_start()
        week_two = week_one + timedelta(days=7)
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_one, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-alpha",
        )
        generator.generate_weekly_digest(vault, week_one)

        _write_unnamed_fragment(
            vault,
            "beta",
            created=datetime.combine(week_two, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-beta",
        )
        _write_unnamed_fragment(
            vault,
            "gamma",
            created=datetime.combine(week_two, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-gamma",
        )
        path_two = generator.generate_weekly_digest(vault, week_two)
        content = path_two.read_text(encoding="utf-8")
        assert "Previous week" in content
        # Previous week: 1 fragment, this week: 2 fragments, delta: +1
        assert "Change: +1" in content

    def test_corrupt_history_does_not_abort_generation(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """A corrupt unnamed-history.json is tolerated; the digest still writes."""
        history_path = (
            vault / "00-Creek-Meta" / "Processing-Log" / "unnamed-history.json"
        )
        history_path.write_text("{ not json", encoding="utf-8")
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        path = generator.generate_weekly_digest(vault, week_start)
        assert path.exists()
        # History is overwritten with a valid JSON array containing this run.
        history = json.loads(history_path.read_text(encoding="utf-8"))
        assert len(history) == 1
        assert history[0]["week_start"] == week_start.isoformat()

    def test_rerun_on_same_week_replaces_digest(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Running twice for the same week overwrites the digest file."""
        week_start = _week_start()
        _write_unnamed_fragment(
            vault,
            "alpha",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        first = generator.generate_weekly_digest(vault, week_start)
        first_mtime = first.stat().st_mtime
        _write_unnamed_fragment(
            vault,
            "beta",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
        )
        second = generator.generate_weekly_digest(vault, week_start)
        assert second == first
        assert second.stat().st_mtime >= first_mtime
        assert "[[frag-beta]]" in second.read_text(encoding="utf-8")


class TestRenderFragmentsSectionBucketsByAuthoredAt:
    """FEAT-031: the rendered ``Created`` line uses the authored date."""

    def test_rendered_created_uses_authored_at_not_created(self) -> None:
        """The ``Created`` line reflects ``authored_at`` over ``created``.

        Two fragments share the same wall-clock ``created`` (the moment
        Creek ingested them) but carry ``authored_at`` values years
        apart. The "Fragments This Week" listing must render each
        fragment's *authored* date, not the shared ingest date — the
        same class of bug FEAT-031 exists to fix on every time-bucketing
        surface.
        """
        ingest_moment = datetime(2026, 5, 20, 14, 0, 0, tzinfo=UTC)
        authored_dates = [
            datetime(2024, 1, 1, 9, 0, 0, tzinfo=UTC),
            datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC),
        ]
        fragments = [
            Fragment(
                id=f"frag-feat031-unnamed-{i}",
                title=f"Unnamed Reflection {i}",
                source=FragmentSource(platform=SourcePlatform.JOURNAL),
                created=ingest_moment,
                ingested=ingest_moment,
                authored_at=authored,
            )
            for i, authored in enumerate(authored_dates)
        ]
        section = _render_fragments_section(fragments, excerpts={})
        # Each fragment renders its own authored date, not the shared
        # ingest date.
        assert "- Created: 2024-01-01" in section
        assert "- Created: 2025-06-01" in section
        # The shared wall-clock ingest date must never leak in.
        assert ingest_moment.date().isoformat() not in section


# ---- _stable_digest_generated ----


class TestStableDigestGenerated:
    """Unit coverage for the digest's idempotent ``generated`` stamp."""

    def test_no_existing_file_stamps_now(self, tmp_path: Path) -> None:
        """With no prior digest the helper returns a fresh ISO timestamp."""
        from creek.generate.unnamed import _stable_digest_generated

        out = _stable_digest_generated(
            tmp_path / "2026-W07.md", "body", "2026-02-09", "2026-02-15"
        )
        assert out.endswith("+00:00")

    def test_unchanged_content_preserves_prior_stamp(self, tmp_path: Path) -> None:
        """An unchanged digest reuses its recorded stamp (byte-idempotent)."""
        from creek.generate.unnamed import _stable_digest_generated

        path = tmp_path / "2026-W07.md"
        path.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    content="body",
                    week_start="2026-02-09",
                    week_end="2026-02-15",
                    generated="2020-01-01T00:00:00+00:00",
                )
            ),
            encoding="utf-8",
        )
        out = _stable_digest_generated(path, "body", "2026-02-09", "2026-02-15")
        assert out == "2020-01-01T00:00:00+00:00"

    def test_changed_body_restamps(self, tmp_path: Path) -> None:
        """A changed body discards the old stamp for a fresh one."""
        from creek.generate.unnamed import _stable_digest_generated

        path = tmp_path / "2026-W07.md"
        path.write_text(
            frontmatter.dumps(
                frontmatter.Post(
                    content="old body",
                    week_start="2026-02-09",
                    week_end="2026-02-15",
                    generated="2020-01-01T00:00:00+00:00",
                )
            ),
            encoding="utf-8",
        )
        out = _stable_digest_generated(path, "new body", "2026-02-09", "2026-02-15")
        assert out != "2020-01-01T00:00:00+00:00"

    def test_unreadable_file_restamps(self, tmp_path: Path) -> None:
        """An undecodable prior digest is treated as absent, not a crash."""
        from creek.generate.unnamed import _stable_digest_generated

        path = tmp_path / "2026-W07.md"
        path.write_bytes(b"\xff\xfe not valid utf-8")
        out = _stable_digest_generated(path, "body", "2026-02-09", "2026-02-15")
        assert out.endswith("+00:00")


# ---- #1321: one parse per file, and the excerpt behaviour that must survive ----


def _spy_on_frontmatter_load(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path handed to ``frontmatter.load``, delegating to the real one.

    :mod:`creek.generate.unnamed` does a module-level ``import frontmatter`` and
    calls ``frontmatter.load(...)`` from three separate helpers, so the attribute
    is resolved on the module object at call time and patching it here is picked
    up by every one of them. The real loader still runs — including raising — so
    nothing about the behaviour under test changes; this only observes.

    Deliberately a call counter rather than a wall-clock or ``tracemalloc``
    probe. The cost being pinned is "how many times is the Unnamed tree parsed",
    which is exact, machine-independent, and cheap enough for the default lane;
    a timing probe would be none of those and would earn a ``slow`` marker.

    Args:
        monkeypatch: The builtin pytest fixture; it restores the real
            ``frontmatter.load`` at teardown.

    Returns:
        The live record — ``str(path)`` per call, in call order. Read it after
        the call under test.
    """
    real_load = frontmatter.load
    loaded: list[str] = []

    def _spy(fd: Any, *args: Any, **kwargs: Any) -> Any:
        """Record *fd*, then delegate to (and raise exactly like) the real load."""
        loaded.append(str(fd))
        return real_load(fd, *args, **kwargs)

    monkeypatch.setattr(frontmatter, "load", _spy)
    return loaded


def _write_unnamed_fragment_with_body(
    vault: Path,
    name: str,
    *,
    body: str,
    created: datetime,
    title: str | None = None,
    frag_id: str | None = None,
) -> Path:
    """Write an Unnamed fragment whose markdown *body* the caller controls.

    :func:`_write_unnamed_fragment` hard-codes ``"Body content for <name>."``,
    which cannot express the three cases the excerpt renderer actually branches
    on (over-length, empty, multi-line). This is the same writer with the body
    lifted into a parameter; the frontmatter it emits is identical.

    Args:
        vault: Root vault path.
        name: Filename stem (without extension). Also decides ``rglob``
            sort order, which some callers below depend on.
        body: Exact markdown body to write beneath the frontmatter.
        created: Creation timestamp, mirrored into ``ingested`` so the
            FEAT-031 authored-date fallback buckets the fragment as intended.
        title: Fragment title; defaults to *name*.
        frag_id: Fragment ID; defaults to ``frag-<name>``.

    Returns:
        Path to the written markdown file.
    """
    fragment = Fragment(
        id=frag_id or f"frag-{name}",
        title=title or name,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        ingested=created,
    )
    data = fragment.model_dump(mode="json")
    post = frontmatter.Post(content=body, **data)
    path = vault / "10-Liminal" / "Unnamed" / f"{name}.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _excerpt_lines(digest_text: str) -> list[str]:
    """Return the excerpt text of every ``- Excerpt:`` line in a rendered digest.

    Asserting on this rather than on ``in digest_text`` substrings makes the
    *absence* of an excerpt assertable (an empty list) and pins the rendered
    text exactly, so a truncation off-by-one cannot hide inside a substring
    match.

    Args:
        digest_text: The full text of a generated digest note.

    Returns:
        One entry per excerpt line, in document order, with the
        ``"- Excerpt: "`` prefix stripped.
    """
    prefix = "- Excerpt: "
    return [
        line.removeprefix(prefix)
        for line in digest_text.splitlines()
        if line.startswith(prefix)
    ]


class TestDigestParsesEachUnnamedFileOnce:
    """#1321: the weekly digest must parse each Unnamed file exactly once."""

    @pytest.mark.parametrize("n_fragments", [2, 8])
    def test_generate_weekly_digest_parses_each_unnamed_file_exactly_once(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        n_fragments: int,
    ) -> None:
        """One ``frontmatter.load`` per Unnamed markdown file, whatever N is (#1321).

        COST MODEL. ``generate_weekly_digest`` currently walks the Unnamed tree
        three separate times for a week in which all ``N`` fragments fall:
        ``load_unnamed_fragments`` parses every file (``N``), then
        ``_build_excerpt_map`` calls ``_find_fragment_file`` per fragment and
        each of those re-parses the tree until it hits its ID
        (``N(N+1)/2`` on the reference run), then ``_excerpt_from_file``
        re-loads the very file the scan just opened (``N``). Measured at HEAD:
        ``N=2 -> 7`` calls against an expected 2, and ``N=8 -> 52`` against an
        expected 8 — a 6.5x over-read that grows quadratically
        (``N=200 -> 20,500``). One pass over the tree is sufficient: the
        excerpt is in the body of the file ``load_unnamed_fragments`` already
        opened.

        WHAT THE COUNTER PREVENTS. A future lane reaching for the convenient
        ``_find_fragment_file(vault_path, frag.id)`` inside a per-fragment loop
        reintroduces the whole quadratic immediately and silently — every
        existing test still passes, because the output is byte-identical. Only
        a call count notices. The uniqueness assertion is the durable half: it
        survives a feature that legitimately adds a file to the folder, where a
        bare ``== N`` would not.

        ANTI-VACUITY. ``n_files`` is read off the filesystem, not from the spy,
        and is anchored to the parametrized ``N`` first — otherwise a fixture
        that wrote nothing would satisfy ``0 == 0``. The trailing excerpt
        assertions stop the cheapest possible "fix" (dropping excerpt
        resolution entirely) from turning this green.

        ``N`` is capped low on purpose: ``detect_unnamed_clusters`` is
        genuinely ``O(N^2)`` in numpy over the in-week set, and that axis is
        out of scope here. No ``slow`` marker — this repo reserves it for
        wall-clock and ``tracemalloc`` benchmarks, and a call count is neither.
        """
        week_start = _week_start()
        for i in range(n_fragments):
            _write_unnamed_fragment(
                vault,
                f"n{i:02d}",
                created=datetime.combine(
                    week_start + timedelta(days=i % 7), datetime.min.time(), tzinfo=UTC
                ),
            )

        unnamed_dir = vault / "10-Liminal" / "Unnamed"
        md_files = sorted(unnamed_dir.rglob("*.md"))
        n_files = len(md_files)
        # Anchor the expectation to the fixture before trusting it as a bound.
        assert n_files == n_fragments

        loaded = _spy_on_frontmatter_load(monkeypatch)
        digest_path = generator.generate_weekly_digest(vault, week_start)

        assert len(loaded) == n_files  # exactly one parse per file
        assert len(set(loaded)) == len(loaded)  # and no file parsed twice

        # The parse budget is only meaningful if the digest is still complete.
        content = digest_path.read_text(encoding="utf-8")
        for md_file in md_files:
            assert f"- Excerpt: Body content for {md_file.stem}." in content

    def test_iter_unnamed_records_parses_lazily_one_file_at_a_time(
        self,
        vault: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_iter_unnamed_records`` must stay a generator, not materialise a list.

        WHY THIS IS A SEPARATE TEST. Fixing #1321's quadratic *parse* count would
        be a hollow win if it were bought with an unbounded *memory* cost. The
        walk yields ``(fragment, body)`` and the whole `10-Liminal/Unnamed/`
        tree is, by design, the vault's accumulating residue — the digest exists
        to report that it grows. Holding every body live at once is the scale
        failure that a naive ``return [record for ...]`` would introduce.

        WHAT THIS CATCHES THAT NOTHING ELSE DOES. Converting the generator to a
        list comprehension is behaviourally invisible: every other test in this
        file, the parse *count* test above included, stays green, because the
        totals and the rendered bytes are identical either way. Only the
        *timing* of the parses distinguishes them. Drawing a single record and
        asserting exactly one file has been opened is the observable difference:
        eager materialisation parses all five here before ``next`` returns.
        """
        when = datetime.combine(_week_start(), datetime.min.time(), tzinfo=UTC)
        for i in range(5):
            _write_unnamed_fragment(vault, f"lazy{i:02d}", created=when)

        loaded = _spy_on_frontmatter_load(monkeypatch)
        records = _iter_unnamed_records(vault)

        # Creating the generator must not touch the filesystem at all.
        assert loaded == []

        next(records)
        assert len(loaded) == 1  # one record drawn == exactly one file parsed


class TestDigestExcerptRendering:
    """Excerpt behaviour the #1321 refactor must preserve byte-for-byte.

    ``_excerpt_from_file`` is about to lose its file-reading half and become a
    pure ``_excerpt_from_body(body: str) -> str``. These three pin the
    formatting contract at the *digest output* level rather than by calling the
    helper, so they keep holding under whatever the helper ends up being named.
    All three are expected to PASS at HEAD: they are the equivalence net around
    the refactor, not the RED test.
    """

    def test_long_body_excerpt_truncates_to_199_chars_plus_ellipsis(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """A body over 200 chars renders 199 characters then U+2026.

        ``_EXCERPT_MAX_CHARS`` is 200 and the truncation spends one of those on
        the ellipsis, so the rendered excerpt is exactly 200 characters wide.
        Asserting the exact string (not ``startswith``) is what makes an
        off-by-one in the slice fatal, and the second assertion pins that the
        tail was genuinely dropped rather than merely decorated.
        """
        week_start = _week_start()
        long_body = "a" * 250
        _write_unnamed_fragment_with_body(
            vault,
            "long",
            body=long_body,
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-long",
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")

        expected = "a" * 199 + "…"
        assert len(expected) == 200
        assert _excerpt_lines(content) == [expected]
        assert long_body not in content

    def test_empty_body_emits_no_excerpt_line(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """A fragment with an empty body gets no ``- Excerpt:`` line at all.

        The renderer suppresses the bullet rather than emitting a dangling
        ``- Excerpt:`` with nothing after it. Expected GREEN at HEAD; it is
        here so the refactor cannot quietly start rendering empty bullets.
        The link assertion keeps the test honest — the fragment really is in
        the digest, so the missing excerpt line means suppression, not absence.
        """
        week_start = _week_start()
        _write_unnamed_fragment_with_body(
            vault,
            "hollow",
            body="",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-hollow",
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")

        assert "[[frag-hollow]]" in content
        assert _excerpt_lines(content) == []

    def test_multiline_body_collapses_newlines_to_spaces(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """A multi-line body renders as one line with single spaces at the joins.

        An excerpt is a markdown list item, so an embedded newline would break
        out of the bullet and corrupt the digest's structure. Untested at the
        digest level until now, which is precisely the kind of gap a refactor
        of the excerpt helper walks into. Expected GREEN at HEAD.
        """
        week_start = _week_start()
        _write_unnamed_fragment_with_body(
            vault,
            "multiline",
            body="line one\nline two\nline three",
            created=datetime.combine(week_start, datetime.min.time(), tzinfo=UTC),
            frag_id="frag-multiline",
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")

        assert _excerpt_lines(content) == ["line one line two line three"]


class TestDigestExcerptResolutionCorrectness:
    """#1321: which file an excerpt may come from — a deliberate behaviour change.

    Both tests in this class are expected RED at HEAD. They are not regressions
    the refactor must avoid; they are defects the refactor is required to fix,
    recorded here so the change of behaviour is deliberate and reviewed rather
    than incidental.
    """

    def test_duplicate_ids_resolve_to_the_sorted_first_file(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """Two valid fragments sharing an ID resolve deterministically (#1321).

        HEAD's ``_find_fragment_file`` returns the first **unsorted**
        ``rglob`` match, and ``rglob`` yields ``os.scandir`` order — hash-
        ordered on APFS, roughly insertion-ordered on ext4, promised by
        nobody. So today the excerpt published under a duplicated ID is
        whichever file the filesystem happened to hand over first: this test
        may pass by accident on one machine and fail on the next, which is
        itself the bug. ``load_unnamed_fragments`` already iterates
        ``sorted(...)``, so building the excerpt map from that single pass
        makes sorted-first-wins the defined answer everywhere.

        The assertion is on body content, not on a path, so it pins the
        observable outcome rather than the resolution mechanism. Asserting
        the list is non-empty first stops a "no excerpt for duplicates"
        implementation from satisfying the set comparison vacuously.
        """
        week_start = _week_start()
        when = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        _write_unnamed_fragment_with_body(
            vault,
            "aaa-dupe",
            body="AAA BODY.",
            title="Duplicate A",
            frag_id="frag-dupe",
            created=when,
        )
        _write_unnamed_fragment_with_body(
            vault,
            "zzz-dupe",
            body="ZZZ BODY.",
            title="Duplicate Z",
            frag_id="frag-dupe",
            created=when,
        )
        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")

        excerpts = _excerpt_lines(content)
        assert excerpts, "the duplicated ID must still resolve to some excerpt"
        assert set(excerpts) == {"AAA BODY."}
        assert "ZZZ BODY." not in content

    def test_non_fragment_file_never_supplies_a_fragment_excerpt(
        self,
        generator: UnnamedDigestGenerator,
        vault: Path,
    ) -> None:
        """A stray non-fragment note must not be published under a fragment (#1321).

        HEAD's ``_find_fragment_file`` matches on ``post.get("id")`` alone and
        never checks ``type``, while ``load_unnamed_fragments`` — the pass that
        decides what a fragment *is* — skips anything whose ``type`` is not
        ``fragment``. The two disagree. With ``aab-stray.md`` sorting ahead of
        the real ``ccc-empty.md`` and carrying the same ``id``, the generated
        digest at HEAD contains ``- Excerpt: STRAY BODY.`` beneath the real
        fragment's heading: a non-fragment file's text, attributed to a
        fragment, in a note the user reads as a summary of their own writing.

        This is a deliberate correction, not a preserved behaviour. Resolving
        excerpts from the fragments ``load_unnamed_fragments`` already parsed
        removes the second, laxer definition of "fragment file" entirely; the
        real fragment's body is empty, so the correct output has no excerpt
        line. The wiki-link assertion pins that the fragment itself is still
        listed, so the absent excerpt cannot be an absent fragment.
        """
        week_start = _week_start()
        when = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
        _write_unnamed_fragment_with_body(
            vault,
            "ccc-empty",
            body="",
            title="The Real Fragment",
            frag_id="frag-empty",
            created=when,
        )
        stray = vault / "10-Liminal" / "Unnamed" / "aab-stray.md"
        stray.write_text(
            "---\ntype: not-a-fragment\nid: frag-empty\n---\nSTRAY BODY.\n",
            encoding="utf-8",
        )

        path = generator.generate_weekly_digest(vault, week_start)
        content = path.read_text(encoding="utf-8")

        assert "[[frag-empty]]" in content
        assert "STRAY BODY." not in content
        assert _excerpt_lines(content) == []


# ---------------------------------------------------------------------------
# Issue #871: the digest's remaining unwitnessed arms
# ---------------------------------------------------------------------------


class TestUnnamedRecordsStepOverBadNotes:
    """``_iter_unnamed_records`` drops unreadable and schema-invalid notes.

    Issue #871 asked for coverage of ``_excerpt_from_file`` and
    ``_find_fragment_file``. **Both are gone.** The #1321 one-pass
    refactor replaced them with ``_excerpt_from_body`` (which cuts the
    excerpt from the body already in hand rather than reopening the file)
    and ``_iter_unnamed_records`` (a lazy generator carrying the
    ``Digests/`` skip). Both replacements are already covered, so those
    two asks are moot; what was genuinely uncovered is
    ``_load_fragment``'s ``except ValidationError`` arm, which is what
    this class pins.

    As everywhere in this sweep, the assertions name the **survivors** by
    list equality. ``_iter_unnamed_records`` yields nothing for an empty
    vault, so "it did not raise" would be satisfied by a generator that
    dropped every note on the floor.
    """

    @staticmethod
    def _seed_good(vault: Path) -> list[str]:
        """Write two readable unnamed fragments and return their ids."""
        for name in ("good-a", "good-b"):
            _write_unnamed_fragment(
                vault,
                name,
                created=datetime(2026, 2, 10, tzinfo=UTC),
            )
        return ["frag-good-a", "frag-good-b"]

    @staticmethod
    def _ids(vault: Path) -> list[str]:
        """Return the ids recovered from *vault*'s Unnamed folder, in order."""
        return [frag.id for frag, _body in _iter_unnamed_records(vault)]

    def test_positive_control_yields_both_records(self, vault: Path) -> None:
        """With no poison present, both readable fragments come back."""
        expected = self._seed_good(vault)

        assert self._ids(vault) == expected

    @pytest.mark.parametrize("shape", CORRUPT_NOTE_SHAPES)
    def test_skips_each_corrupt_shape(
        self,
        vault: Path,
        shape: str,
        corrupt_note: Callable[[Path, str], Path],
    ) -> None:
        """An unreadable note is stepped over; both good records survive.

        The poison is named to sort *between* the two good notes, so a
        walk that aborted on it would return one id and be caught. A
        length assertion would not catch that.
        """
        expected = self._seed_good(vault)
        corrupt_note(vault / "10-Liminal" / "Unnamed" / "good-aa.md", shape)

        assert self._ids(vault) == expected

    def test_skips_schema_invalid_fragment(
        self,
        vault: Path,
        pydantic_invalid_note: Callable[[Path], Path],
    ) -> None:
        """A note that parses but fails ``Fragment.model_validate`` is dropped.

        This is the arm a corrupt note can never reach: the note carries
        ``type: fragment``, so it passes the type gate and fails only on
        the schema.
        """
        expected = self._seed_good(vault)
        pydantic_invalid_note(vault / "10-Liminal" / "Unnamed" / "good-aa.md")

        assert self._ids(vault) == expected


class TestReadHistoryEntries:
    """``_read_history_entries`` fails soft on every unusable history file.

    Each arm returns the *same* ``[]`` the happy path returns for an empty
    log, so these tests are only meaningful next to the positive control
    below — without it, a reader that returned ``[]`` unconditionally
    would pass every one of them.
    """

    def test_positive_control_returns_recorded_entries(self, tmp_path: Path) -> None:
        """A well-formed history file round-trips to its entry list."""
        path = tmp_path / "history.json"
        entries = [{"week_start": "2026-02-09", "fragment_count": 3}]
        path.write_text(json.dumps(entries), encoding="utf-8")

        assert _read_history_entries(path) == entries

    def test_missing_file_yields_no_entries(self, tmp_path: Path) -> None:
        """A history file that was never written reads as no prior data."""
        assert _read_history_entries(tmp_path / "absent.json") == []

    @pytest.mark.parametrize("raw", ["", "   ", "\n\n  \t\n"])
    def test_whitespace_only_file_is_silent_not_corrupt(
        self,
        tmp_path: Path,
        raw: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A blank file reads as no prior data **without** a corruption warning.

        The silence is the whole point of the empty-after-strip guard, and
        it is the only thing that distinguishes the guard from its
        absence. Deleting ``if not raw: return []`` still returns ``[]``,
        because ``json.loads("")`` raises ``JSONDecodeError`` and the arm
        below catches it — but it does so while crying "Corrupt unnamed
        history" at the operator about a perfectly ordinary empty file.
        Asserting only the return value would let that regression through;
        asserting the empty log is what makes this test non-vacuous.
        """
        path = tmp_path / "history.json"
        path.write_text(raw, encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger="creek.generate.unnamed"):
            assert _read_history_entries(path) == []

        assert [record.message for record in caplog.records] == []

    def test_corrupt_json_warns_and_yields_no_entries(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unparseable JSON is reported at WARNING and treated as no data.

        The log is part of the product here: silently discarding a
        history file the operator can still see on disk is exactly the
        kind of degradation that needs to surface at default level.
        """
        path = tmp_path / "history.json"
        path.write_text("{not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="creek.generate.unnamed"):
            assert _read_history_entries(path) == []

        assert any(
            "Corrupt unnamed history" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )

    def test_json_object_root_yields_no_entries(self, tmp_path: Path) -> None:
        """Valid JSON that is not a list is rejected rather than returned.

        JSON permits a non-list root, and every caller indexes the result
        as a list of rows — so a dict here would sail past the parse and
        break downstream instead.
        """
        path = tmp_path / "history.json"
        path.write_text('{"week_start": "2026-02-09"}', encoding="utf-8")

        assert _read_history_entries(path) == []


class TestConnectedComponents:
    """``_connected_components`` is unchanged by edges inside a component.

    Stated honestly, because the alternative reading flatters this test:
    the ``if root_u != root_v`` check it drives is a **pure
    optimisation**, not a correctness guard. Deleting it leaves
    ``parent[root_u] = root_v`` with ``root_u == root_v`` — a
    self-assignment, and therefore a no-op — so *no* behavioural test can
    kill that mutant, and this one does not claim to. What it does pin is
    the contract (a redundant edge must not disturb the partition) and
    the previously unexecuted ``580->577`` branch. The redundancy is
    reported as a finding rather than papered over with a test that
    cannot fail.
    """

    def test_redundant_edge_leaves_components_unchanged(self) -> None:
        """A second edge inside one component changes nothing.

        The exact component list is asserted, not its length: a length
        assertion survives a mutant that puts the right *number* of
        components together out of the wrong nodes.
        """
        without_redundancy = _connected_components(4, [(0, 1), (1, 2)])
        with_redundancy = _connected_components(4, [(0, 1), (1, 2), (2, 0), (0, 1)])

        assert without_redundancy == [[0, 1, 2], [3]]
        assert with_redundancy == [[0, 1, 2], [3]]

    def test_isolated_nodes_each_form_their_own_component(self) -> None:
        """With no edges at all every node stands alone, in index order."""
        assert _connected_components(3, []) == [[0], [1], [2]]
