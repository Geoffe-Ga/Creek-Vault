"""Tests for ``creek ingest --refresh-dates`` (FEAT-031 / #263).

Covers the one-shot ``authored_at`` backfill engine: per-platform
extraction routing, idempotency, frontmatter rewrite preserving body
content and non-Fragment keys, and the CLI summary line.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — runtime use by pytest fixtures

import frontmatter
import pytest

from creek.ingest.refresh import (
    RefreshOutcome,
    RefreshSummary,
    extract_authored_at_for_fragment,
    refresh_vault_authored_at,
)
from creek.models import (
    Fragment,
    FragmentSource,
    SourcePlatform,
)

# ---- Fixtures ----


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Build a vault skeleton with the required scaffold dirs."""
    (tmp_path / "00-Creek-Meta").mkdir()
    (tmp_path / "01-Fragments").mkdir()
    return tmp_path


def _write_fragment(
    vault_path: Path,
    *,
    fragment_id: str,
    source_path: Path | None,
    platform: SourcePlatform,
    authored_at: datetime | None,
    extra_metadata: dict[str, object] | None = None,
    body: str = "Body content unchanged across the refresh.\n",
) -> Path:
    """Write a Fragment markdown file under ``01-Fragments/Generic/``."""
    target_dir = vault_path / "01-Fragments" / "Generic"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{fragment_id}.md"
    fragment = Fragment(
        id=fragment_id,
        title="Test Fragment",
        source=FragmentSource(
            platform=platform,
            original_file=str(source_path) if source_path is not None else None,
        ),
        authored_at=authored_at,
    )
    fm_data = fragment.model_dump(mode="json")
    if extra_metadata:
        fm_data.update(extra_metadata)
    post = frontmatter.Post(content=body, **fm_data)
    target_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target_path


# ---- Engine tests ----


class TestRefreshVault:
    """End-to-end refresh against synthetic vault contents."""

    def test_html_source_authored_at_is_backfilled(self, vault: Path) -> None:
        html_source = vault / "post.html"
        html_source.write_text(
            "<html><head>"
            '<meta property="article:published_time" '
            'content="2024-03-15T08:00:00Z">'
            "</head><body>Hi</body></html>",
            encoding="utf-8",
        )
        frag_path = _write_fragment(
            vault,
            fragment_id="frag-abc",
            source_path=html_source,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        summary = refresh_vault_authored_at(vault)
        assert summary.updated == 1
        post = frontmatter.load(str(frag_path))
        assert str(post["authored_at"]).startswith("2024-03-15")

    def test_idempotent_second_run(self, vault: Path) -> None:
        """A second pass against an unchanged source rewrites nothing."""
        html_source = vault / "post.html"
        html_source.write_text(
            "<html><head>"
            '<meta property="article:published_time" '
            'content="2024-03-15T08:00:00Z">'
            "</head><body>Hi</body></html>",
            encoding="utf-8",
        )
        _write_fragment(
            vault,
            fragment_id="frag-abc",
            source_path=html_source,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        first = refresh_vault_authored_at(vault)
        assert first.updated == 1
        second = refresh_vault_authored_at(vault)
        assert second.updated == 0
        assert second.unchanged == 1

    def test_body_preserved_byte_for_byte(self, vault: Path) -> None:
        """Refresh does not perturb the markdown body of the fragment."""
        html_source = vault / "post.html"
        html_source.write_text(
            "<html><head>"
            '<meta property="article:published_time" '
            'content="2024-03-15T08:00:00Z">'
            "</head><body>Hi</body></html>",
            encoding="utf-8",
        )
        original_body = "## Heading\n\nA paragraph with *emphasis* and a [link](x).\n"
        frag_path = _write_fragment(
            vault,
            fragment_id="frag-body",
            source_path=html_source,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
            body=original_body,
        )
        refresh_vault_authored_at(vault)
        post = frontmatter.load(str(frag_path))
        assert post.content == original_body.rstrip("\n")

    def test_non_fragment_extra_keys_preserved(self, vault: Path) -> None:
        """Custom frontmatter keys survive the rewrite untouched."""
        html_source = vault / "post.html"
        html_source.write_text(
            "<html><head>"
            '<meta property="article:published_time" '
            'content="2024-03-15T08:00:00Z">'
            "</head><body>Hi</body></html>",
            encoding="utf-8",
        )
        frag_path = _write_fragment(
            vault,
            fragment_id="frag-keys",
            source_path=html_source,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
            extra_metadata={
                "obsidian_aliases": ["my-post"],
                "custom_scratch": {"reviewer": "geoff"},
            },
        )
        refresh_vault_authored_at(vault)
        post = frontmatter.load(str(frag_path))
        assert post["obsidian_aliases"] == ["my-post"]
        assert post["custom_scratch"] == {"reviewer": "geoff"}

    def test_missing_source_marked_no_source(self, vault: Path) -> None:
        """A fragment whose source file vanished is flagged, not updated."""
        _write_fragment(
            vault,
            fragment_id="frag-ghost",
            source_path=vault / "gone.html",
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        summary = refresh_vault_authored_at(vault)
        assert summary.updated == 0

    def test_txt_source_uses_mtime(self, vault: Path) -> None:
        path = vault / "note.txt"
        path.write_text("Meeting notes\n", encoding="utf-8")
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        os.utime(path, (expected.timestamp(), expected.timestamp()))
        frag_path = _write_fragment(
            vault,
            fragment_id="frag-txt",
            source_path=path,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        refresh_vault_authored_at(vault)
        post = frontmatter.load(str(frag_path))
        assert str(post["authored_at"]).startswith("2024-05-01")

    def test_summary_aggregate_counters(self, vault: Path) -> None:
        """Counters tally updated / unchanged / missing buckets correctly."""
        path = vault / "note.txt"
        path.write_text("Hello\n", encoding="utf-8")
        _write_fragment(
            vault,
            fragment_id="frag-1",
            source_path=path,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        _write_fragment(
            vault,
            fragment_id="frag-2",
            source_path=vault / "missing.txt",
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        summary = refresh_vault_authored_at(vault)
        assert summary.total == 2
        assert isinstance(summary, RefreshSummary)
        assert all(isinstance(o, RefreshOutcome) for o in summary.outcomes)


# ---- Per-platform routing tests ----


class TestExtractorRouting:
    """Routing from :class:`SourcePlatform` to per-format extractor."""

    def test_markdown_platform_uses_frontmatter_dates(
        self,
        tmp_path: Path,
    ) -> None:
        md_source = tmp_path / "post.md"
        md_source.write_text(
            "---\npublished: 2024-03-15\n---\n# Hi\n",
            encoding="utf-8",
        )
        fragment = Fragment(
            id="frag-md",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                original_file=str(md_source),
            ),
        )
        result = extract_authored_at_for_fragment(fragment)
        assert result is not None
        assert result.date().isoformat() == "2024-03-15"

    def test_unknown_source_path_returns_none(self) -> None:
        fragment = Fragment(
            id="frag-empty",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.MARKDOWN,
                original_file=None,
            ),
        )
        assert extract_authored_at_for_fragment(fragment) is None

    def test_image_platform_uses_mtime_for_png(self, tmp_path: Path) -> None:
        from PIL import Image

        path = tmp_path / "photo.png"
        Image.new("RGB", (4, 4), color="white").save(path, "png")
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        os.utime(path, (expected.timestamp(), expected.timestamp()))
        fragment = Fragment(
            id="frag-png",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.IMAGE_OCR,
                original_file=str(path),
            ),
        )
        result = extract_authored_at_for_fragment(fragment)
        assert result == expected

    def test_code_platform_uses_mtime_when_not_git(self, tmp_path: Path) -> None:
        path = tmp_path / "README.md"
        path.write_text("# Hi\n", encoding="utf-8")
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        os.utime(path, (expected.timestamp(), expected.timestamp()))
        fragment = Fragment(
            id="frag-code",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.CODE,
                original_file=str(path),
            ),
        )
        result = extract_authored_at_for_fragment(fragment)
        assert result == expected

    def test_presentation_platform_uses_mtime_for_stub(self, tmp_path: Path) -> None:
        path = tmp_path / "deck.pptx"
        path.write_bytes(b"")
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        os.utime(path, (expected.timestamp(), expected.timestamp()))
        fragment = Fragment(
            id="frag-pptx",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.PRESENTATION,
                original_file=str(path),
            ),
        )
        result = extract_authored_at_for_fragment(fragment)
        assert result == expected

    def test_spreadsheet_platform_uses_mtime_for_csv(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        path.write_text("col\n", encoding="utf-8")
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        os.utime(path, (expected.timestamp(), expected.timestamp()))
        fragment = Fragment(
            id="frag-csv",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.SPREADSHEET,
                original_file=str(path),
            ),
        )
        result = extract_authored_at_for_fragment(fragment)
        assert result == expected

    def test_chat_platform_uses_mtime_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "export.json"
        path.write_text('[{"a":1}]', encoding="utf-8")
        expected = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
        os.utime(path, (expected.timestamp(), expected.timestamp()))
        fragment = Fragment(
            id="frag-chat",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.CHATGPT,
                original_file=str(path),
            ),
        )
        result = extract_authored_at_for_fragment(fragment)
        assert result == expected

    def test_nonexistent_source_path_returns_none(self) -> None:
        fragment = Fragment(
            id="frag-ghost",
            title="Test",
            source=FragmentSource(
                platform=SourcePlatform.DOCUMENT,
                original_file="/no/such/path.html",
            ),
        )
        assert extract_authored_at_for_fragment(fragment) is None


# ---- CLI integration ----


class TestRefreshDatesCli:
    """Integration check for ``creek ingest --refresh-dates``."""

    def test_cli_runs_end_to_end(self, vault: Path) -> None:
        """The CLI command rewrites the fragment when invoked via Typer."""
        from typer.testing import CliRunner

        from creek.cli import app

        html_source = vault / "post.html"
        html_source.write_text(
            "<html><head>"
            '<meta property="article:published_time" '
            'content="2024-03-15T08:00:00Z">'
            "</head><body>Hi</body></html>",
            encoding="utf-8",
        )
        frag_path = _write_fragment(
            vault,
            fragment_id="frag-cli",
            source_path=html_source,
            platform=SourcePlatform.DOCUMENT,
            authored_at=None,
        )
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["ingest", "--refresh-dates", "--vault", str(vault)],
        )
        assert result.exit_code == 0, result.output
        assert "Refreshed authored_at on 1" in result.output
        post = frontmatter.load(str(frag_path))
        assert str(post["authored_at"]).startswith("2024-03-15")
