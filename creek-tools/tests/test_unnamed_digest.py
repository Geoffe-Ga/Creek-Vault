"""Tests for creek.generate.unnamed — Unnamed weekly digest generator.

Tests cover the ``UnnamedDigestGenerator`` class:

- Collecting fragments from ``10-Liminal/Unnamed/`` in a target week
- Clustering unnamed fragments via embedding similarity
- Rendering the weekly digest markdown note with all required sections
- Tracking week-over-week growth in a JSON history file
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.config import EmbeddingsConfig
from creek.generate.unnamed import (
    UnnamedDigestGenerator,
    _render_fragments_section,
)
from creek.link.embeddings import EmbeddingLinker
from creek.models import Fragment, FragmentSource, SourcePlatform

if TYPE_CHECKING:
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
