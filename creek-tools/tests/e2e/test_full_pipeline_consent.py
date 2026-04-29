"""End-to-end consent gating test (TEST-001 / INC-010).

The pipeline must skip ingestion when no consent has been recorded for
the source. This test runs the pipeline against a fresh source with no
prior consent and asserts that the resulting vault is empty of
fragments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.consent import ConsentManager
from creek.pipeline import Pipeline

pytestmark = pytest.mark.e2e


def test_pipeline_skips_ingestion_without_consent(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Without a prior consent record, ingestion is gated and no fragments are written.

    A ``ConsentManager`` rooted in the vault metadata directory is given
    to the pipeline. Because no consent file exists, the pipeline should
    log a warning and stop before ingestion, leaving the vault empty.
    """
    (synthetic_source / "private.md").write_text(
        "# Private content\nthat must not be ingested without consent\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    consent_log = synthetic_vault / "00-Creek-Meta" / "Consent"
    consent = ConsentManager(log_dir=consent_log)

    pipeline = Pipeline(config=config, consent_manager=consent)
    result = pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)

    assert result.fragments_created == 0, (
        "Pipeline ingested fragments without recorded consent (INC-010 sentinel)"
    )
