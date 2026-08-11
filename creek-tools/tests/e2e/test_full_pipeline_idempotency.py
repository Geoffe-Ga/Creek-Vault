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


def _vault_files(vault: Path) -> list[Path]:
    """Return the durable .md artefacts of a run, in a stable order.

    Excludes two things, for two different reasons:

    * ``00-Creek-Meta/`` — bookkeeping, never a fragment.
    * ``review-queue-<YYYY-MM-DD_HHMMSS>.md`` — written to the vault root
      by ``ReviewQueueGenerator`` with a *second-resolution timestamp in
      its filename*, so it is one-file-per-invocation **by design**. Two
      runs that straddle a second boundary therefore leave two of them,
      and counting it made this BUG-007 sentinel a clock race: it passed
      only while the pipeline was fast enough to finish both runs inside
      the same second. #1303 gave the link stage real work to do and the
      race started firing. The exclusion narrows the assertion to what it
      always meant — fragments and cluster pages must not duplicate —
      rather than papering over a duplication bug.

    Args:
        vault: Vault root.

    Returns:
        Sorted paths of the .md files a rerun must not duplicate.
    """
    return sorted(
        p
        for p in vault.rglob("*.md")
        if "00-Creek-Meta" not in p.parts and not p.name.startswith("review-queue-")
    )


def test_pipeline_is_idempotent_for_same_source(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Running pipeline twice writes the same fragments, not duplicates.

    Since #1303 the counter also covers the link stage's output: eddy
    pages under ``03-Eddies/`` and thread pages under ``02-Threads/*/``
    are real files now, so a link stage that re-minted a page per run
    (rather than re-deriving the same membership-stable id) would show up
    here as a duplicate.
    """
    (synthetic_source / "note.md").write_text(
        "# Idempotency\n\nA stable body.\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)

    pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    after_first = _vault_files(synthetic_vault)

    pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    after_second = _vault_files(synthetic_vault)

    assert after_second == after_first, (
        "Pipeline duplicated vault files on rerun: "
        f"{sorted(p.name for p in set(after_second) - set(after_first))} appeared, "
        f"{sorted(p.name for p in set(after_first) - set(after_second))} vanished "
        "(BUG-007 sentinel)"
    )
