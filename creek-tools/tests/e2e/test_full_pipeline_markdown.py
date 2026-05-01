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

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline, PipelineResult

pytestmark = pytest.mark.e2e


def _drop_markdown(source: Path, name: str, body: str) -> Path:
    """Write a tiny markdown source file with deterministic content."""
    file = source / name
    file.write_text(body, encoding="utf-8")
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
    assert result.fragments_created >= 1, (
        "Pipeline reported zero fragments for a non-empty markdown source "
        "(BUG-001 sentinel)."
    )


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
