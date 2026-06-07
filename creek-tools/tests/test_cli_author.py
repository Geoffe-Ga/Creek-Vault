"""CLI tests for ``creek author`` (FEAT-041 Writing Desk skeleton, #455)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import _compose_author_query, app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _vault(tmp_path: Path) -> Path:
    """Create and return an empty vault directory under *tmp_path*."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_author_dry_run_prints_plan_and_evidence(tmp_path: Path) -> None:
    """``--dry-run`` prints the pipeline plan and a stub evidence summary."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "research",
            "--query",
            "What is F6 Pluralism?",
            "--vault",
            str(_vault(tmp_path)),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "PLAN:" in result.output
    assert "graph" in result.output
    assert "reflect" in result.output
    assert "EVIDENCE (stub):" in result.output
    assert "claims" in result.output
    assert "source_fragments" in result.output


def test_author_run_prints_verdict(tmp_path: Path) -> None:
    """A full (stub) run prints the verdict and a body."""
    result = runner.invoke(
        app,
        ["author", "--query", "q", "--vault", str(_vault(tmp_path))],
    )

    assert result.exit_code == 0, result.output
    assert "verdict=" in result.output


def test_author_rejects_unknown_medium(tmp_path: Path) -> None:
    """An unsupported medium exits non-zero with a clear message."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "not-a-medium",
            "--query",
            "q",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    assert result.exit_code != 0
    assert "not-a-medium" in result.output


def test_author_book_report_requires_work(tmp_path: Path) -> None:
    """``--medium book-report`` without ``--work`` exits 2 with a clear error."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "--work" in result.output


def test_author_non_book_report_requires_query(tmp_path: Path) -> None:
    """A non-book-report medium without ``--query`` exits 2 with a clear error."""
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "essay",
            "--vault",
            str(_vault(tmp_path)),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "--query" in result.output


def test_author_book_report_runs_from_work_without_query(tmp_path: Path) -> None:
    """``--medium book-report --work <path>`` (no ``--query``) prints a draft."""
    vault = _vault(tmp_path)
    work = vault / "11-Other-Authors" / "naval-ravikant" / "on-leverage"
    work.mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--work",
            str(work),
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "verdict=" in result.output


def test_author_book_report_rejects_missing_work(tmp_path: Path) -> None:
    """``--work`` pointing at a non-existent path fails fast with a typer error."""
    vault = _vault(tmp_path)
    missing = vault / "11-Other-Authors" / "nobody" / "no-such-work"
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--work",
            str(missing),
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "--work" in result.output
    assert "does not exist" in result.output


def test_author_book_report_rejects_file_work(tmp_path: Path) -> None:
    """``--work`` pointing at a file (not a work directory) is rejected."""
    vault = _vault(tmp_path)
    work_file = vault / "11-Other-Authors" / "naval-ravikant" / "on-leverage.md"
    work_file.parent.mkdir(parents=True)
    work_file.write_text("not a directory", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "author",
            "--medium",
            "book-report",
            "--work",
            str(work_file),
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "--work" in result.output


def test_compose_author_query_requires_query_or_work() -> None:
    """The composer makes its upstream-validated invariant explicit.

    ``_validate_author_inputs`` guarantees a non-None query for every
    non-book-report medium (and book-report always carries ``--work``), so
    reaching the composer with both ``None`` is a programming error — it raises
    rather than silently authoring from an empty query.
    """
    with pytest.raises(ValueError, match="--query or --work"):
        _compose_author_query(None, None)


def test_author_max_rounds_out_of_range(tmp_path: Path) -> None:
    """``--max-rounds`` outside [1, 10] is rejected by the CLI."""
    result = runner.invoke(
        app,
        [
            "author",
            "--query",
            "q",
            "--vault",
            str(_vault(tmp_path)),
            "--max-rounds",
            "0",
        ],
    )

    assert result.exit_code != 0
