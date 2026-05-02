"""End-to-end classify-review round-trip test (TEST-001 / INC-002 / INC-011).

Ingest, classify, write to a manual review queue, then run classify
again and assert that human review decisions persist across the second
run rather than being overwritten by the rule classifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline, PipelineResult

pytestmark = pytest.mark.e2e


def test_classify_runs_twice_without_error(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Two classification passes must not raise and must not duplicate fragments.

    The original incarnation of this test asserted on
    ``(synthetic_vault / "07-Review").exists()`` — but the
    ``synthetic_vault`` fixture pre-creates that directory, so the
    assertion was vacuous. The honest round-trip semantics require
    INC-002 (review command) and INC-011 (classify --force flag) to
    land. Until then this test guards two real properties:

    1. Calling Pipeline.run() twice in a row never raises (smoke).
    2. The second pass does not create duplicate fragments — i.e. the
       ID-based dedup in VaultWriter still holds when classification
       runs on already-classified content.

    A persistence-of-manual-decisions assertion will replace #2 once
    INC-002 ships.
    """
    (synthetic_source / "thought.md").write_text(
        "# A musing\n\nSomething I want to keep thinking about.\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)

    first = pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    assert isinstance(first, PipelineResult), (
        f"Pipeline.run() returned {type(first).__name__}, expected PipelineResult"
    )
    fragment_files_after_first = sorted(
        p.name for p in synthetic_vault.glob("01-Fragments/**/*.md")
    )

    second = pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    assert isinstance(second, PipelineResult)
    fragment_files_after_second = sorted(
        p.name for p in synthetic_vault.glob("01-Fragments/**/*.md")
    )

    # Real assertion: the second classification pass must not duplicate
    # fragment files. The exact set should be identical between passes.
    assert fragment_files_after_first == fragment_files_after_second, (
        "Second classify pass changed the fragment set on disk — "
        "INC-011 sentinel (re-classify should be idempotent without "
        "the --force flag)."
    )
