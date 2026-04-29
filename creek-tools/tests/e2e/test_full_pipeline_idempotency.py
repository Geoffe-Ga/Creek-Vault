"""End-to-end idempotency test (TEST-001).

Running the pipeline twice against the same source must not duplicate
fragments. The second run should observe the existing IDs and add zero
new files. This guards against deterministic-ID regressions (BUG-007)
and dedup logic regressions in the vault writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline

pytestmark = pytest.mark.e2e


def _count_vault_files(vault: Path) -> int:
    """Count the .md files under the vault, excluding meta directories."""
    return sum(1 for p in vault.rglob("*.md") if "00-Creek-Meta" not in p.parts)


def test_pipeline_is_idempotent_for_same_source(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Running pipeline twice writes the same fragments, not duplicates."""
    (synthetic_source / "note.md").write_text(
        "# Idempotency\n\nA stable body.\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)

    pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    after_first = _count_vault_files(synthetic_vault)

    pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    after_second = _count_vault_files(synthetic_vault)

    assert after_second == after_first, (
        f"Pipeline duplicated fragments on rerun: {after_first} -> {after_second} "
        "(BUG-007 sentinel)"
    )
