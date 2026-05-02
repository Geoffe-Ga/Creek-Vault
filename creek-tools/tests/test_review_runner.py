"""Tests for the interactive review queue runner."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import frontmatter
from rich.console import Console

from creek.classify.review_runner import (
    ReviewQueueRunner,
    format_review_summary,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    SourcePlatform,
    VoiceClassification,
)
from tests.helpers import write_fragment_file as _write_fragment

if TYPE_CHECKING:
    from pathlib import Path


def test_list_pending_returns_only_review_candidates(tmp_path: Path) -> None:
    """Confident, manual-stamped fragments are not in the queue."""
    vault = tmp_path / "vault"

    confident = Fragment(
        id="frag-confident0001",
        title="Confident",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        frequency=FrequencyClassification(primary=Frequency.F5),
        voice=VoiceClassification(confidence=Confidence.SETTLED),
    )
    _write_fragment(vault=vault, fragment=confident, body="body")

    pending = Fragment(
        id="frag-pending00001",
        title="Pending",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=pending, body="body")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    queue = runner.list_pending()
    titles = {entry.fragment.title for entry in queue}
    assert "Pending" in titles
    assert "Confident" not in titles


def test_list_pending_skips_manual(tmp_path: Path) -> None:
    """Already-resolved (``manual``) fragments are excluded from the queue."""
    vault = tmp_path / "vault"

    fragment = Fragment(
        id="frag-resolved0001",
        title="Resolved",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body", method="manual")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    assert runner.list_pending() == []


def test_run_interactive_accept_persists_manual(tmp_path: Path) -> None:
    """Choosing ``accept`` rewrites the file with manual provenance."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-acceptme0001",
        title="Accept me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(vault=vault, fragment=fragment, body="body")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    pending = runner.list_pending()

    with patch("typer.prompt", return_value="a"):
        summary = runner.run_interactive(pending)

    assert summary.accepted == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "manual"


def test_run_interactive_defer_leaves_file(tmp_path: Path) -> None:
    """``defer`` is a no-op for the file."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-deferme00001",
        title="Defer me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(vault=vault, fragment=fragment, body="body")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    pending = runner.list_pending()
    before = file.read_text(encoding="utf-8")

    with patch("typer.prompt", return_value="d"):
        summary = runner.run_interactive(pending)

    assert summary.deferred == 1
    assert file.read_text(encoding="utf-8") == before


def test_run_interactive_quit_breaks_loop(tmp_path: Path) -> None:
    """``quit`` aborts the loop without touching remaining files."""
    vault = tmp_path / "vault"
    for i in range(2):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=f"frag-quitme0000{i:02d}",
                title=f"Quit {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body="body",
        )

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    pending = runner.list_pending()

    with patch("typer.prompt", return_value="q"):
        summary = runner.run_interactive(pending)

    assert summary.accepted == 0
    assert summary.overridden == 0


def test_run_interactive_override_writes_new_frequency(tmp_path: Path) -> None:
    """``override`` collects a frequency and persists the change."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-override0001",
        title="Override me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(vault=vault, fragment=fragment, body="body")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    pending = runner.list_pending()

    with patch("typer.prompt", side_effect=["o", "F5"]):
        summary = runner.run_interactive(pending)

    assert summary.overridden == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "manual"
    assert reloaded["frequency"]["primary"] == "F5"


def test_run_interactive_override_with_unknown_frequency_defers(
    tmp_path: Path,
) -> None:
    """A bad ``override`` value defers the fragment and prints a warning."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-overridebad01",
        title="Bad override",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(vault=vault, fragment=fragment, body="body")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    pending = runner.list_pending()

    with patch("typer.prompt", side_effect=["o", "magenta"]):
        summary = runner.run_interactive(pending)

    assert summary.deferred == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded.get("classification_method") != "manual"


def test_format_review_summary_contains_id(tmp_path: Path) -> None:
    """The single-line summary surfaces the fragment ID for the operator."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-fmtsummary001",
        title="Format me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    runner = ReviewQueueRunner(vault_path=vault, console=Console())
    pending = runner.list_pending()
    text = format_review_summary(pending[0])
    assert "frag-fmtsummary001" in text
    assert "Format me" in text
