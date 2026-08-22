"""End-to-end markdown pipeline test (TEST-001).

Walks the canonical happy path: drop a markdown source file in a fresh
source directory, run ``Pipeline.run`` against an empty synthetic vault,
and assert the vault contains a fragment file with a non-empty body and
the expected deterministic ID.

This test is the primary safety-net for BUG-001 (pipeline discards
ingestor output) and BUG-008 (vault writer stores empty body). When the
pipeline is correct, this passes; when it's broken, this fails before
review.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline, PipelineResult

pytestmark = pytest.mark.e2e


_FIXED_MTIME = datetime(2026, 1, 2, 12, 0, tzinfo=UTC).timestamp()
"""Pinned modification time so the vault filename's date prefix is exact.

Ingestors derive a fragment's timestamp from the source file's mtime, and
the vault writer prefixes the filename with it. Without pinning, the
filename assertion below would only be checkable as a pattern.
"""


def _drop_markdown(source: Path, name: str, body: str) -> Path:
    """Write a tiny markdown source file with deterministic content and mtime."""
    file = source / name
    file.write_text(body, encoding="utf-8")
    os.utime(file, (_FIXED_MTIME, _FIXED_MTIME))
    return file


def test_pipeline_creates_fragment_for_markdown_source(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """A markdown file must produce at least one fragment in the vault."""
    _drop_markdown(
        synthetic_source,
        "note.md",
        "# Title\n\nThis is the body of the note.\n",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)
    result = pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)

    assert isinstance(result, PipelineResult)
    # Exact, not ``>= 1``. The lower bound this replaced could not detect
    # duplication in principle, which is how issue #1304 — every ingestor
    # run over every file — survived the whole life of this file.
    written = sorted((synthetic_vault / "01-Fragments").rglob("*.md"))
    assert result.fragments_created == 1, (
        "Pipeline reported the wrong fragment count for a single markdown "
        "source (BUG-001 / #1304 sentinel)."
    )
    assert [path.name for path in written] == ["2026-01-02-Title.md"]
    assert result.fragments_created == len(written)


def test_pipeline_run_does_not_raise(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Running an empty source through the pipeline must not raise.

    Lower bar than the markdown test above: simply asserts the pipeline
    is wired well enough to traverse all six stages on empty input.
    """
    config = CreekConfig()
    pipeline = Pipeline(config=config)
    result = pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    assert isinstance(result, PipelineResult)
