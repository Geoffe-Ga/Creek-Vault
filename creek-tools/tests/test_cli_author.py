"""CLI tests for ``creek author`` (FEAT-041 Writing Desk)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import _compose_author_query, app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# The Unicode Box Drawing block — the ``│``/``─``/``╭`` glyphs Rich uses to
# frame a panel. Matched as a range so a Rich style change to a different
# border set cannot quietly reintroduce the wrapping brittleness above.
_BOX_DRAWING_RE = re.compile("[─-╿]")


def _strip_ansi(text: str) -> str:
    """Return *text* with ANSI SGR colour escape codes removed.

    typer/rich style option names per-character under a colour-forcing
    terminal, which splits a literal like ``--work`` across escape sequences;
    stripping them rejoins the token so substring assertions are environment
    agnostic.
    """
    return _ANSI_RE.sub("", text)


def _flatten_panel(text: str) -> str:
    """Return *text* with ANSI codes, Rich panel borders and wrapping removed.

    Rich renders typer's errors inside a box and hard-wraps the body to the
    terminal width, so a message that embeds a filesystem path can break
    *mid-phrase*: ``'…/on-leverage.md' is a`` on one line and ``file.`` on the
    next, with ``│`` borders and padding in between. Whether that happens is a
    function of how long the path is, which makes any plain substring
    assertion a coin flip on the length of ``tmp_path``.

    That is not hypothetical. It is exactly what surfaced when the unit lane
    moved to pytest-xdist (issue #1141): xdist inserts a ``popen-gwN/``
    segment into ``tmp_path``, the path grew, and the wrap landed inside the
    phrase under test. The assertion was brittle before the move — any change
    to the temp-path layout would have tripped it — so the fix belongs here
    rather than in the runner.

    Dropping the box-drawing range and collapsing runs of whitespace rejoins
    the sentence, leaving assertions about *wording* independent of *width*.
    """
    return " ".join(_BOX_DRAWING_RE.sub(" ", _strip_ansi(text)).split())


def _vault(tmp_path: Path) -> Path:
    """Create and return an empty vault directory under *tmp_path*."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_author_dry_run_prints_plan_and_evidence(tmp_path: Path) -> None:
    """``--dry-run`` prints the pipeline plan and a (real) evidence summary."""
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
    assert "EVIDENCE:" in result.output  # real evidence, not a stub (#712)
    assert "(stub)" not in result.output
    assert "claims" in result.output
    assert "source_fragments" in result.output


def test_author_command_surface_is_not_stale_stub_text() -> None:
    """The author surface no longer mislabels the live desk as an all-stub (#712).

    Graph/Retrieval/Ontology specialists + Reflection are live deterministic work;
    Voice is live-when-a-provider-is-available. The command docstring + the
    author package docstrings must reflect that, not the original #455 scaffold.
    """
    import creek.author as author_pkg
    from creek.author import client as author_client
    from creek.author.conductor import Conductor, build_default_conductor
    from creek.cli import author as author_cmd

    surfaces = (
        author_cmd.__doc__,
        author_pkg.__doc__,
        author_client.__doc__,
        Conductor.__doc__,
        build_default_conductor.__doc__,
    )
    for doc in surfaces:
        assert doc is not None
        low = doc.lower()
        assert "stub skeleton" not in low
        assert "stub specialist" not in low
        assert "pure stubs" not in low
        assert "typed stubs" not in low
        assert "stub collaborators" not in low
    # The command docstring states the desk does real, deterministic work.
    assert "real, deterministic" in author_cmd.__doc__.lower()


def test_author_run_prints_verdict(tmp_path: Path) -> None:
    """A full author run prints the verdict and a body."""
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
    # typer renders the option name with per-character ANSI styling under a
    # colour-forcing terminal (CI), which splits the literal "--work"; strip
    # escape codes before matching so the assertion holds in every environment.
    plain = _strip_ansi(result.output)
    assert "--work" in plain
    assert "does not exist" in plain


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
    plain = _flatten_panel(result.output)
    assert "--work" in plain
    assert "is a file" in plain


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
