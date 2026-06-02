"""CLI tests for ``creek author`` (FEAT-041 Writing Desk skeleton, #455)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app

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
