"""Tests for creek.ingest.substack — Substack-aware ingestor.

Covers Substack-export detection, ``posts.csv`` metadata extraction,
publication-date preservation via ``authored_at``, source taxonomy
(``platform = SUBSTACK`` / ``kind = WRITING``), folder routing under
``01-Fragments/Writing/Substack/``, idempotent re-ingestion, and the
deliberate skip of subscriber-engagement CSVs (PII).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.ingest.base import assemble_ingested_fragment
from creek.ingest.substack import (
    SUBSCRIBER_CSV_SUFFIXES,
    SubstackIngestor,
    _extract_post_id,
    _parse_posts_csv,
    is_substack_export,
)
from creek.models import SourceKind, SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path


# ---- Sample data ----

_POSTS_CSV = (
    "post_id,post_date,title,subtitle,type,audience,is_published\n"
    "111,2024-03-15T08:30:00.000Z,Hello World,A first post,newsletter,everyone,true\n"
    "222,2025-01-02T12:00:00.000Z,Second Essay,On craft,newsletter,only_paid,true\n"
    "333,2026-04-01T09:00:00.000Z,Latest,About spring,newsletter,everyone,true\n"
)

_HTML_TEMPLATE = (
    "<html><body><h1>{title}</h1>"
    "<p>This is the body of the {title} essay.</p></body></html>"
)


def _write_substack_export(tmp_path: Path) -> Path:
    """Create a faithful (minimal) Substack export at *tmp_path*.

    Returns the export root.
    """
    (tmp_path / "posts.csv").write_text(_POSTS_CSV, encoding="utf-8")
    posts_dir = tmp_path / "posts"
    posts_dir.mkdir()
    for post_id, slug, title in [
        ("111", "hello-world", "Hello World"),
        ("222", "second-essay", "Second Essay"),
        ("333", "latest", "Latest"),
    ]:
        (posts_dir / f"{post_id}.{slug}.html").write_text(
            _HTML_TEMPLATE.format(title=title),
            encoding="utf-8",
        )
    # Subscriber-engagement files that MUST be ignored (PII).
    (tmp_path / "email_list.csv").write_text(
        "email\nuser@example.com\n",
        encoding="utf-8",
    )
    (posts_dir / "111.hello-world.delivers.csv").write_text(
        "email,delivered\nuser@example.com,true\n",
        encoding="utf-8",
    )
    (posts_dir / "111.hello-world.opens.csv").write_text(
        "email,opened\nuser@example.com,true\n",
        encoding="utf-8",
    )
    return tmp_path


# ---- Detection ----


class TestIsSubstackExport:
    """Auto-detection of Substack export directories."""

    def test_detects_directory_with_posts_csv_and_html(self, tmp_path: Path) -> None:
        _write_substack_export(tmp_path)
        assert is_substack_export(tmp_path) is True

    def test_rejects_directory_without_posts_csv(self, tmp_path: Path) -> None:
        (tmp_path / "foo.html").write_text("<p>x</p>", encoding="utf-8")
        assert is_substack_export(tmp_path) is False

    def test_rejects_directory_without_html_files(self, tmp_path: Path) -> None:
        (tmp_path / "posts.csv").write_text(_POSTS_CSV, encoding="utf-8")
        assert is_substack_export(tmp_path) is False

    def test_rejects_nonexistent_path(self, tmp_path: Path) -> None:
        assert is_substack_export(tmp_path / "nope") is False

    def test_rejects_file_path(self, tmp_path: Path) -> None:
        target = tmp_path / "posts.csv"
        target.write_text(_POSTS_CSV, encoding="utf-8")
        assert is_substack_export(target) is False


# ---- posts.csv parsing ----


class TestParsePostsCsv:
    """Parsing of the Substack ``posts.csv`` metadata sidecar."""

    def test_parses_rows_into_post_id_map(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "posts.csv"
        csv_path.write_text(_POSTS_CSV, encoding="utf-8")
        result = _parse_posts_csv(csv_path)
        assert set(result) == {"111", "222", "333"}
        assert result["111"]["title"] == "Hello World"
        assert result["222"]["audience"] == "only_paid"
        assert result["333"]["post_date"].startswith("2026-04-01")

    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        assert _parse_posts_csv(tmp_path / "missing.csv") == {}

    def test_rows_without_post_id_are_skipped(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "posts.csv"
        csv_path.write_text(
            "post_id,title\n,Has no id\n42,Has id\n",
            encoding="utf-8",
        )
        result = _parse_posts_csv(csv_path)
        assert set(result) == {"42"}


# ---- File-name parsing ----


class TestExtractPostId:
    """Mapping ``<id>.<slug>.html`` filenames back to their post ID."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("111.hello-world.html", "111"),
            ("222.second-essay.html", "222"),
            ("9999999.long-post-slug.html", "9999999"),
            ("42.foo.bar.html", "42"),
        ],
    )
    def test_extracts_leading_numeric_component(
        self,
        filename: str,
        expected: str,
    ) -> None:
        assert _extract_post_id(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        ["welcome.html", "no-id.html", ".html", ""],
    )
    def test_returns_none_for_unparseable_filenames(self, filename: str) -> None:
        assert _extract_post_id(filename) is None


# ---- Subscriber-CSV safety ----


class TestSubscriberCsvSkip:
    """PII-bearing engagement CSVs must never produce a fragment."""

    def test_constants_cover_all_substack_subscriber_files(self) -> None:
        assert ".delivers.csv" in SUBSCRIBER_CSV_SUFFIXES
        assert ".opens.csv" in SUBSCRIBER_CSV_SUFFIXES

    def test_ingestor_does_not_emit_fragments_for_engagement_csvs(
        self,
        tmp_path: Path,
    ) -> None:
        _write_substack_export(tmp_path)
        result = SubstackIngestor().ingest(tmp_path)
        emitted_paths = [frag.source_path for frag in result.fragments]
        assert not any("delivers.csv" in p for p in emitted_paths)
        assert not any("opens.csv" in p for p in emitted_paths)
        assert not any("email_list.csv" in p for p in emitted_paths)


# ---- End-to-end ingestion ----


class TestSubstackIngestion:
    """Whole-export ingestion: metadata, dates, folder routing, taxonomy."""

    def test_emits_one_fragment_per_html_post(self, tmp_path: Path) -> None:
        _write_substack_export(tmp_path)
        result = SubstackIngestor().ingest(tmp_path)
        assert len(result.fragments) == 3
        assert not result.errors

    def test_authored_at_matches_posts_csv_publish_date(
        self,
        tmp_path: Path,
    ) -> None:
        _write_substack_export(tmp_path)
        result = SubstackIngestor().ingest(tmp_path)
        by_id: dict[str, str] = {}
        for parsed in result.fragments:
            assembled = assemble_ingested_fragment(parsed)
            authored = assembled.fragment.authored_at
            assert authored is not None, "Substack fragments must carry authored_at"
            by_id[assembled.fragment.title] = authored.date().isoformat()
        assert by_id["Hello World"] == "2024-03-15"
        assert by_id["Second Essay"] == "2025-01-02"
        assert by_id["Latest"] == "2026-04-01"

    def test_source_platform_and_kind(self, tmp_path: Path) -> None:
        _write_substack_export(tmp_path)
        result = SubstackIngestor().ingest(tmp_path)
        for parsed in result.fragments:
            assembled = assemble_ingested_fragment(parsed)
            assert assembled.fragment.source.platform == SourcePlatform.SUBSTACK
            assert assembled.fragment.source.kind == SourceKind.WRITING

    def test_title_pulled_from_posts_csv(self, tmp_path: Path) -> None:
        _write_substack_export(tmp_path)
        result = SubstackIngestor().ingest(tmp_path)
        titles = sorted(
            assemble_ingested_fragment(p).fragment.title for p in result.fragments
        )
        assert titles == ["Hello World", "Latest", "Second Essay"]

    def test_optional_metadata_columns_surface_in_frontmatter(
        self,
        tmp_path: Path,
    ) -> None:
        """``subtitle``, ``audience``, and ``podcast_url`` round-trip into frontmatter.

        These are the optional ``posts.csv`` columns the ingestor
        promises in its docstring; if any silently disappeared,
        downstream surfaces (privacy gates, voice-proxy training) would
        lose useful context.
        """
        (tmp_path / "posts.csv").write_text(
            "post_id,post_date,title,subtitle,audience,podcast_url\n"
            "111,2024-03-15T08:30:00.000Z,Hello,A subtitle,only_paid,"
            "https://example.com/p/111.mp3\n",
            encoding="utf-8",
        )
        (tmp_path / "111.hello.html").write_text(
            "<html><body><p>hi</p></body></html>",
            encoding="utf-8",
        )
        result = SubstackIngestor().ingest(tmp_path)
        assert len(result.fragments) == 1
        frontmatter = result.fragments[0].metadata["frontmatter"]
        assert frontmatter["subtitle"] == "A subtitle"
        assert frontmatter["audience"] == "only_paid"
        assert frontmatter["podcast_url"] == "https://example.com/p/111.mp3"
        assert frontmatter["substack_post_id"] == "111"

    def test_html_body_converted_to_markdown(self, tmp_path: Path) -> None:
        _write_substack_export(tmp_path)
        result = SubstackIngestor().ingest(tmp_path)
        for parsed in result.fragments:
            assembled = assemble_ingested_fragment(parsed)
            # Markdownify renders the H1 as an ATX heading.
            assert assembled.body.lstrip().startswith("#")

    def test_html_without_csv_row_falls_back_to_mtime(
        self,
        tmp_path: Path,
    ) -> None:
        # Export with a stranded HTML — no matching posts.csv row.
        (tmp_path / "posts.csv").write_text(
            "post_id,post_date,title\n111,2024-03-15T00:00:00.000Z,Hello\n",
            encoding="utf-8",
        )
        (tmp_path / "111.hello.html").write_text(
            "<html><body><p>hi</p></body></html>",
            encoding="utf-8",
        )
        stranded = tmp_path / "999.stranded.html"
        stranded.write_text(
            "<html><body><p>orphan</p></body></html>",
            encoding="utf-8",
        )
        result = SubstackIngestor().ingest(tmp_path)
        assert len(result.fragments) == 2
        stranded_frags = [f for f in result.fragments if f.source_path == str(stranded)]
        assert len(stranded_frags) == 1
        assembled = assemble_ingested_fragment(stranded_frags[0])
        # No authored date available → field is None, honest answer.
        assert assembled.fragment.authored_at is None


# ---- Idempotency ----


class TestIdempotentReIngest:
    """Re-running SubstackIngestor against the same export is a no-op."""

    def test_fragment_ids_stable_across_runs(self, tmp_path: Path) -> None:
        _write_substack_export(tmp_path)
        first = SubstackIngestor().ingest(tmp_path)
        second = SubstackIngestor().ingest(tmp_path)
        first_ids = sorted(
            assemble_ingested_fragment(p).fragment.id for p in first.fragments
        )
        second_ids = sorted(
            assemble_ingested_fragment(p).fragment.id for p in second.fragments
        )
        assert first_ids == second_ids


# ---- Folder routing (writer-side) ----


class TestFolderRouting:
    """Substack fragments must land under ``01-Fragments/Writing/Substack/``."""

    def test_writer_routes_substack_to_writing_substack(self, tmp_path: Path) -> None:
        from creek.vault.writer import _PLATFORM_SUBFOLDER

        assert _PLATFORM_SUBFOLDER[SourcePlatform.SUBSTACK] == "Writing/Substack"

    def test_end_to_end_write_lands_in_writing_substack(
        self,
        tmp_path: Path,
    ) -> None:
        from creek.vault.writer import VaultWriter

        # Minimal vault scaffold.
        vault = tmp_path / "vault"
        (vault / "00-Creek-Meta" / "Processing-Log").mkdir(parents=True)
        (vault / "01-Fragments").mkdir(parents=True)

        export = tmp_path / "export"
        export.mkdir()
        _write_substack_export(export)

        result = SubstackIngestor().ingest(export)
        writer = VaultWriter(vault_path=vault)
        for parsed in result.fragments:
            assembled = assemble_ingested_fragment(parsed)
            written = writer.write_fragment(assembled.fragment, body=assembled.body)
            assert "01-Fragments/Writing/Substack" in str(written)

    def test_registry_exposes_substack(self) -> None:
        from creek.ingest import INGESTOR_REGISTRY

        assert "substack" in INGESTOR_REGISTRY
        assert INGESTOR_REGISTRY["substack"] is SubstackIngestor

    def test_document_ingestor_defers_to_substack(self, tmp_path: Path) -> None:
        """``DocumentIngestor`` must yield no HTML fragments from a Substack export.

        Without this guard, ``creek process`` (which runs every registered
        ingestor against the source) would double-emit each essay: once
        as an opaque HTML document and again as a publication-dated
        Substack post.
        """
        from creek.ingest.documents import DocumentIngestor

        _write_substack_export(tmp_path)
        docs = DocumentIngestor().discover(tmp_path)
        assert docs == []


# ---- Discover handling ----


class TestDiscoverEdgeCases:
    """Discover behaviour outside the happy path."""

    def test_nonexistent_path_returns_empty(self, tmp_path: Path) -> None:
        result = SubstackIngestor().ingest(tmp_path / "missing")
        assert result.fragments == []

    def test_file_input_is_rejected_gracefully(self, tmp_path: Path) -> None:
        # A SubstackIngestor only makes sense against a directory; a
        # single HTML file lacks the posts.csv sidecar, so nothing is
        # emitted and no exception escapes.
        sole = tmp_path / "sole.html"
        sole.write_text("<html><body><p>solo</p></body></html>", encoding="utf-8")
        result = SubstackIngestor().ingest(sole)
        assert result.fragments == []

    def test_export_without_posts_csv_derives_metadata(
        self,
        tmp_path: Path,
    ) -> None:
        """Without posts.csv, a post_id-prefixed HTML still ingests (#594).

        Title is derived from the slug; this replaces the old behaviour of
        emitting nothing (which silently dropped whole exports).
        """
        (tmp_path / "111.foo.html").write_text(
            "<html><body><p>hi</p></body></html>",
            encoding="utf-8",
        )
        result = SubstackIngestor().ingest(tmp_path)
        assert len(result.fragments) == 1
        assert assemble_ingested_fragment(result.fragments[0]).fragment.title == "Foo"

    def test_html_files_without_post_id_prefix_are_skipped(
        self,
        tmp_path: Path,
    ) -> None:
        """``index.html`` and other unprefixed HTML files are dropped silently.

        Substack exports sometimes ship a top-level ``index.html`` table
        of contents; without a leading post-ID component there is no
        join key into ``posts.csv``, so emitting a fragment would just
        create a stub with no metadata. Honest answer: skip it.
        """
        (tmp_path / "posts.csv").write_text(
            "post_id,post_date,title\n111,2024-03-15T00:00:00.000Z,Hi\n",
            encoding="utf-8",
        )
        (tmp_path / "111.real-post.html").write_text(
            "<html><body><p>hi</p></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "index.html").write_text(
            "<html><body><p>toc</p></body></html>",
            encoding="utf-8",
        )
        result = SubstackIngestor().ingest(tmp_path)
        assert len(result.fragments) == 1
        assert "111.real-post.html" in result.fragments[0].source_path

    def test_unparseable_post_date_falls_back_to_mtime(
        self,
        tmp_path: Path,
    ) -> None:
        """A malformed ``post_date`` in ``posts.csv`` is not allowed to crash ingest.

        Substack's export schema has drifted in the past; the ingestor
        should warn and fall back to filesystem mtime rather than dump
        a stack trace and lose every other post in the batch.
        """
        (tmp_path / "posts.csv").write_text(
            "post_id,post_date,title\n111,not-a-real-date,Broken Date\n",
            encoding="utf-8",
        )
        (tmp_path / "111.broken.html").write_text(
            "<html><body><p>hi</p></body></html>",
            encoding="utf-8",
        )
        result = SubstackIngestor().ingest(tmp_path)
        assert len(result.fragments) == 1
        assembled = assemble_ingested_fragment(result.fragments[0])
        assert assembled.fragment.authored_at is None


class TestSubscriberCsvHelpers:
    """Direct unit tests for the PII-filter helper."""

    def test_email_list_csv_is_recognised(self, tmp_path: Path) -> None:
        from creek.ingest.substack import _is_subscriber_csv

        path = tmp_path / "email_list.csv"
        path.write_text("email\n", encoding="utf-8")
        assert _is_subscriber_csv(path) is True

    def test_delivers_csv_is_recognised(self, tmp_path: Path) -> None:
        from creek.ingest.substack import _is_subscriber_csv

        path = tmp_path / "111.foo.delivers.csv"
        path.write_text("email\n", encoding="utf-8")
        assert _is_subscriber_csv(path) is True

    def test_posts_csv_is_not_a_subscriber_csv(self, tmp_path: Path) -> None:
        from creek.ingest.substack import _is_subscriber_csv

        path = tmp_path / "posts.csv"
        path.write_text("post_id\n", encoding="utf-8")
        assert _is_subscriber_csv(path) is False


class TestSubstackWithoutPostsCsv:
    """Ingest current Substack exports that lack ``posts.csv`` (#594)."""

    @staticmethod
    def _write_export_no_csv(tmp_path: Path) -> Path:
        posts = tmp_path / "posts"
        posts.mkdir()
        (posts / "111.hello-world.html").write_text(
            '<html><head><script type="application/ld+json">'
            '{"datePublished":"2024-03-15T08:30:00Z"}</script></head>'
            "<body><h1>Hello World</h1><p>Body of the hello world essay.</p>"
            "</body></html>",
            encoding="utf-8",
        )
        (posts / "222.second-essay.html").write_text(
            "<html><body><h1>Second</h1><p>Second body here.</p></body></html>",
            encoding="utf-8",
        )
        # per-post engagement CSV present; NO posts.csv at the root.
        (posts / "111.hello-world.delivers.csv").write_text(
            "email,delivered\nu@e.com,true\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_discover_without_posts_csv_finds_posts(self, tmp_path: Path) -> None:
        """discover derives posts from filenames when posts.csv is absent."""
        root = self._write_export_no_csv(tmp_path)
        docs = SubstackIngestor().discover(root)
        assert len(docs) == 2

    def test_titles_derived_from_slug(self, tmp_path: Path) -> None:
        """Titles come from the HTML filename slug when no posts.csv exists."""
        root = self._write_export_no_csv(tmp_path)
        result = SubstackIngestor().ingest(root)
        assert result.errors == []
        titles = sorted(
            assemble_ingested_fragment(p).fragment.title for p in result.fragments
        )
        assert titles == ["Hello world", "Second essay"]

    def test_authored_at_scraped_from_html_datepublished(self, tmp_path: Path) -> None:
        """authored_at is scraped from the HTML datePublished when available."""
        root = self._write_export_no_csv(tmp_path)
        result = SubstackIngestor().ingest(root)
        by_title = {
            assemble_ingested_fragment(p).fragment.title: assemble_ingested_fragment(
                p
            ).fragment.authored_at
            for p in result.fragments
        }
        assert by_title["Hello world"] is not None
        assert by_title["Hello world"].date().isoformat() == "2024-03-15"
