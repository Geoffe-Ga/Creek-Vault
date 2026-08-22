"""End-to-end purge round-trip: ingest one source, then erase it (TEST-001).

The pipeline stamps a ``.md`` source with ``source.platform: markdown``
(pinned independently by ``tests/e2e/test_pipeline_markdown_e2e.py``), so
that is the platform this module purges. It reads the ingested fragment's
id off disk, asserts the fragment file is gone, and asserts the GAP-002
intent/outcome audit pair *by content* rather than by the presence of a
file in the audit directory.

Before #1345 this module purged ``source_type="other"`` — a platform the
pipeline never stamps on a ``.md`` file — so the purge matched zero
fragments and deleted nothing, while the test passed on two assertions
that could not fail: ``assert purge_result is not None`` against a
non-Optional return, and ``assert audit_files`` against a log that
:meth:`creek.purge.engine.PurgeEngine._run_audited` writes whether or not
anything matched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from creek.config import CreekConfig
from creek.pipeline import Pipeline
from creek.purge.audit import PurgeAuditLog
from creek.purge.engine import PurgeEngine

pytestmark = [pytest.mark.e2e]

PLATFORM = "markdown"
"""The platform the markdown ingestor stamps, and the platform purged.

One constant drives both the frontmatter assertion and the ``purge_source``
call on purpose. If an ingestor change moves the stamp, the failure is a
loud ``assert '<new>' == 'markdown'`` at the stamp line rather than a
silently empty purge. Reading the value out of the frontmatter and feeding
it back into ``purge_source`` would agree with itself forever, hiding
exactly the drift this test exists to catch.
"""


def test_purge_round_trip_clears_fragments_and_writes_audit(
    synthetic_vault: Path, synthetic_source: Path
) -> None:
    """Ingest one markdown source, purge its platform, assert erasure and audit."""
    (synthetic_source / "ephemeral.md").write_text(
        "# Ephemeral\n\nWill be purged shortly.\n",
        encoding="utf-8",
    )

    config = CreekConfig()
    pipeline = Pipeline(config=config)
    run_result = pipeline.run(source_path=synthetic_source, vault_path=synthetic_vault)
    assert run_result.errors == []
    assert run_result.fragments_created == 1

    fragments_dir = synthetic_vault / "01-Fragments"
    ingested = list(fragments_dir.rglob("*.md"))
    assert len(ingested) == 1, f"expected exactly one fragment, got {ingested}"
    frag_path = ingested[0]
    post = frontmatter.load(str(frag_path))
    source_meta = post["source"]
    assert isinstance(source_meta, dict)
    assert source_meta["platform"] == PLATFORM
    ingested_id = str(post["id"])

    engine = PurgeEngine(vault_path=synthetic_vault)
    purge_result = engine.purge_source(source_type=PLATFORM)

    assert purge_result.fragments_affected == 1
    assert purge_result.affected_fragment_ids == [ingested_id]
    assert purge_result.deleted_files == [str(frag_path)]
    assert purge_result.outcome_status == "complete"
    assert purge_result.dry_run is False
    assert not frag_path.exists()
    assert list(fragments_dir.rglob("*.md")) == []

    audit = PurgeAuditLog(synthetic_vault)
    audit.verify()
    entries = audit.read()
    assert [entry.phase for entry in entries] == ["intent", "outcome"]
    intent, outcome = entries
    assert intent.operation_id != ""
    assert outcome.operation_id == intent.operation_id
    for entry in entries:
        assert entry.operation == "source"
        assert entry.criteria == {"source_type": PLATFORM}
    assert outcome.fragments_deleted == 1
    assert outcome.affected_fragments == [ingested_id]
    assert outcome.status == "complete"
    assert outcome.failure_reason is None
