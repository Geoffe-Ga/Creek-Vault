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
from creek.pipeline import Pipeline

pytestmark = pytest.mark.e2e


def test_classify_review_round_trip(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Two classification passes must not regress prior manual decisions.

    This is intentionally lightweight: it verifies that the classify
    stage runs without error against a real source twice in a row,
    leaving the vault's review queue file present after each pass.
    Replacing the simple "queue exists" assertion with a true persisted-
    decision check requires INC-002 (review command) to be implemented.
    """
    (synthetic_source / "thought.md").write_text(
        "# A musing\n\nSomething I want to keep thinking about.\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)

    pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    review_dir = synthetic_vault / "07-Review"
    assert review_dir.exists(), (
        "Review directory missing after first pipeline pass — INC-002 sentinel"
    )

    pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    assert review_dir.exists(), (
        "Review directory disappeared on second pipeline pass — INC-011 sentinel"
    )
