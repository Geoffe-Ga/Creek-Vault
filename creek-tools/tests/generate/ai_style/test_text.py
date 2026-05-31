"""Tests for the shared text utilities (FEAT-040)."""

from __future__ import annotations

from creek.generate.ai_style._text import tidy_whitespace


def test_collapses_internal_runs() -> None:
    """Runs of 2+ spaces/tabs collapse to a single space."""
    assert tidy_whitespace("a   b\t\tc") == "a b c"


def test_strips_trailing_line_whitespace() -> None:
    """Trailing per-line whitespace is removed, newlines preserved."""
    assert tidy_whitespace("a  \nb \n") == "a\nb\n"
