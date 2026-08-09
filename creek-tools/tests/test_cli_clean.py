"""Tests for creek clean CLI subcommands.

Verifies that the ``creek clean`` command group and all five subcommands
(orphans, stale-reviews, broken-links, duplicates, report) are registered,
accept the expected flags, and execute without errors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

SCAN_SUBCOMMANDS: tuple[str, ...] = (
    "orphans",
    "stale-reviews",
    "broken-links",
    "duplicates",
)
"""The ``creek clean`` subcommands that only ever read the vault."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    """Create a minimal vault structure for CLI testing.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the vault root.
    """
    vault = tmp_path / "vault"
    for d in [
        "00-Creek-Meta",
        "01-Fragments/Conversations",
        "02-Threads/Active",
        "03-Eddies",
    ]:
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _write_fragment(
    vault: Path,
    name: str,
    content: str = "Some meaningful content here.",
    *,
    created: datetime | None = None,
) -> Path:
    """Write a fragment markdown file with frontmatter.

    Args:
        vault: Vault root path.
        name: Filename stem.
        content: Body content.
        created: Optional created datetime.

    Returns:
        Path to the written file.
    """
    target = vault / "01-Fragments" / "Conversations" / f"{name}.md"
    metadata: dict[str, object] = {
        "id": f"frag-{name}",
        "title": name,
        "type": "fragment",
        "source": {"platform": "claude", "original_file": f"{name}.json"},
    }
    if created is not None:
        metadata["created"] = created.isoformat()
    post = frontmatter.Post(content=content, **metadata)
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Help tests
# ---------------------------------------------------------------------------


def test_clean_help() -> None:
    """Test that ``creek clean --help`` shows the command group."""
    result = runner.invoke(app, ["clean", "--help"])
    assert result.exit_code == 0
    assert "hygiene" in result.output.lower() or "clean" in result.output.lower()


def test_clean_orphans_help() -> None:
    """Test that ``creek clean orphans --help`` shows help text."""
    result = runner.invoke(app, ["clean", "orphans", "--help"])
    assert result.exit_code == 0
    assert "orphan" in result.output.lower()


def test_clean_stale_reviews_help() -> None:
    """Test that ``creek clean stale-reviews --help`` shows help text."""
    result = runner.invoke(app, ["clean", "stale-reviews", "--help"])
    assert result.exit_code == 0
    assert "review" in result.output.lower()


def test_clean_broken_links_help() -> None:
    """Test that ``creek clean broken-links --help`` shows help text."""
    result = runner.invoke(app, ["clean", "broken-links", "--help"])
    assert result.exit_code == 0
    assert "link" in result.output.lower()


def test_clean_duplicates_help() -> None:
    """Test that ``creek clean duplicates --help`` shows help text."""
    result = runner.invoke(app, ["clean", "duplicates", "--help"])
    assert result.exit_code == 0
    assert "duplicate" in result.output.lower() or "dedup" in result.output.lower()


def test_clean_report_help() -> None:
    """Test that ``creek clean report --help`` shows help text."""
    result = runner.invoke(app, ["clean", "report", "--help"])
    assert result.exit_code == 0
    assert "report" in result.output.lower() or "summary" in result.output.lower()


# ---------------------------------------------------------------------------
# Execution tests
# ---------------------------------------------------------------------------


def test_clean_orphans_runs(tmp_path: Path) -> None:
    """Test that ``creek clean orphans`` runs successfully."""
    vault = _make_vault(tmp_path)
    result = runner.invoke(app, ["clean", "orphans", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Orphan Scan" in result.output


def test_clean_orphans_with_age(tmp_path: Path) -> None:
    """Test that ``creek clean orphans --age-days`` works."""
    vault = _make_vault(tmp_path)
    result = runner.invoke(
        app,
        ["clean", "orphans", "--vault", str(vault), "--age-days", "7"],
    )
    assert result.exit_code == 0


def test_clean_orphans_shows_table(tmp_path: Path) -> None:
    """Test that orphan results display in a table when orphans exist."""
    vault = _make_vault(tmp_path)
    old = datetime.now(tz=UTC) - timedelta(days=60)
    _write_fragment(vault, "orphan-frag", created=old)
    result = runner.invoke(
        app,
        ["clean", "orphans", "--vault", str(vault), "--age-days", "30"],
    )
    assert result.exit_code == 0
    assert "Orphans found: 1" in result.output
    assert "Orphaned Fragments" in result.output


def test_clean_stale_reviews_runs(tmp_path: Path) -> None:
    """Test that ``creek clean stale-reviews`` runs successfully."""
    vault = _make_vault(tmp_path)
    result = runner.invoke(app, ["clean", "stale-reviews", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Stale Review Scan" in result.output


def test_clean_broken_links_runs(tmp_path: Path) -> None:
    """Test that ``creek clean broken-links`` runs successfully."""
    vault = _make_vault(tmp_path)
    result = runner.invoke(app, ["clean", "broken-links", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Broken Link Scan" in result.output


def test_clean_broken_links_shows_broken(tmp_path: Path) -> None:
    """Test that broken links are displayed in output."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "link-broken", content="See [[nonexistent]] here.")
    result = runner.invoke(
        app,
        ["clean", "broken-links", "--vault", str(vault)],
    )
    assert result.exit_code == 0
    assert "nonexistent" in result.output


def test_clean_duplicates_runs(tmp_path: Path) -> None:
    """Test that ``creek clean duplicates`` runs successfully."""
    vault = _make_vault(tmp_path)
    result = runner.invoke(app, ["clean", "duplicates", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Duplicate Scan" in result.output


def test_clean_report_runs(tmp_path: Path) -> None:
    """Test that ``creek clean report`` runs successfully."""
    vault = _make_vault(tmp_path)
    result = runner.invoke(app, ["clean", "report", "--vault", str(vault)])
    assert result.exit_code == 0
    assert "Vault Health Summary" in result.output


def test_clean_report_with_output(tmp_path: Path) -> None:
    """Test that ``creek clean report --output`` writes a markdown file."""
    vault = _make_vault(tmp_path)
    output = tmp_path / "health-report.md"
    result = runner.invoke(
        app,
        ["clean", "report", "--vault", str(vault), "--output", str(output)],
    )
    assert result.exit_code == 0
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "Vault Hygiene Report" in content


def test_clean_report_shows_quality_distribution(tmp_path: Path) -> None:
    """Test that report includes quality distribution."""
    vault = _make_vault(tmp_path)
    _write_fragment(vault, "q-frag", content="Some meaningful quality content here.")
    result = runner.invoke(app, ["clean", "report", "--vault", str(vault)])
    assert result.exit_code == 0
    # Rich may wrap the title across lines, so check parts separately
    assert "Quality" in result.output
    assert "Distribution" in result.output


# ---------------------------------------------------------------------------
# Honesty of the read-only scan surface (#1039)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", SCAN_SUBCOMMANDS)
def test_clean_scan_rejects_apply_flag(tmp_path: Path, subcommand: str) -> None:
    """``--apply`` is gone: the scanners have no mutation path to gate.

    ``creek/clean/hygiene.py`` exposes ``scan()`` and nothing else — no
    ``apply``, ``fix``, ``remove`` or ``delete`` — so a flag advertising
    "Apply changes (default is dry-run)" promised a mode that could not
    exist. Passing it must now fail loudly rather than print a red banner
    over a read-only scan.
    """
    vault = _make_vault(tmp_path)
    result = runner.invoke(
        app,
        ["clean", subcommand, "--vault", str(vault), "--apply"],
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("subcommand", SCAN_SUBCOMMANDS)
def test_clean_scan_help_does_not_offer_apply(subcommand: str) -> None:
    """No ``creek clean`` scan advertises an ``--apply`` flag in its help."""
    result = runner.invoke(app, ["clean", subcommand, "--help"])
    assert result.exit_code == 0
    assert "--apply" not in result.output


@pytest.mark.parametrize("subcommand", SCAN_SUBCOMMANDS)
def test_clean_scan_prints_no_mode_banner(tmp_path: Path, subcommand: str) -> None:
    """A read-only scan claims neither APPLY nor DRY-RUN.

    ``DRY-RUN`` is as misleading as ``APPLY`` here: it implies a wet run
    exists. These commands only ever read.
    """
    vault = _make_vault(tmp_path)
    result = runner.invoke(app, ["clean", subcommand, "--vault", str(vault)])
    assert result.exit_code == 0
    assert "APPLY" not in result.output
    assert "DRY-RUN" not in result.output


def test_purge_still_reports_apply_mode(tmp_path: Path) -> None:
    """``creek purge`` keeps its APPLY banner — that one genuinely deletes.

    Guards the deletion in #1039 from over-reaching: ``_render_purge_result``
    is driven by ``PurgeResult.dry_run`` on a command that really does unlink
    files, so its banner is honest and must survive.
    """
    vault = _make_vault(tmp_path)
    old = datetime.now(tz=UTC) - timedelta(days=60)
    _write_fragment(vault, "doomed", created=old)
    result = runner.invoke(
        app,
        ["purge", "fragment", "frag-doomed", "--vault", str(vault), "--yes"],
    )
    assert result.exit_code == 0
    assert "APPLY" in result.output
