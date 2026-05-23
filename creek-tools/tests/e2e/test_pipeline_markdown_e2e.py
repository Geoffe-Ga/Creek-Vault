"""End-to-end test: markdown source becomes a populated vault fragment.

This test exercises the full ``Pipeline`` against a markdown source
directory and verifies the canonical batch-A acceptance criteria:

- A real fragment file is produced under ``01-Fragments/``
- The fragment carries the platform reported by the ingestor (``markdown``)
- The fragment ID is the deterministic 12-hex SHA-256 prefix
- The converted markdown body is preserved below the frontmatter
- ``PipelineResult.errors`` is empty for a clean run
- A second run is idempotent (no duplicate fragment files)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.config import CreekConfig
from creek.pipeline import Pipeline

if TYPE_CHECKING:
    from pathlib import Path


VAULT_DIRS: list[str] = [
    "00-Creek-Meta/Processing-Log",
    "01-Fragments/Conversations",
    "01-Fragments/Messages",
    "01-Fragments/Writing",
    "01-Fragments/Writing/Substack",
    "01-Fragments/Journal",
    "01-Fragments/Technical",
    "01-Fragments/Notes",
    "01-Fragments/Documents",
    "01-Fragments/Data",
    "01-Fragments/Decks",
    "01-Fragments/Images",
    "01-Fragments/Unsorted",
    "02-Threads/Active",
    "02-Threads/Dormant",
    "02-Threads/Resolved",
    "03-Eddies",
    "04-Praxis/Daily",
    "04-Praxis/Seasonal",
    "04-Praxis/Situational",
    "06-Frequencies",
    "08-Decisions/Active",
    "08-Decisions/Archive",
]


def _make_empty_vault(vault_path: Path) -> Path:
    """Materialise the minimal vault directory layout."""
    for d in VAULT_DIRS:
        (vault_path / d).mkdir(parents=True, exist_ok=True)
    return vault_path


@pytest.mark.e2e
def test_pipeline_writes_real_fragment(tmp_path: Path) -> None:
    """A markdown source file becomes a real, well-formed vault fragment."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text(
        "# A note\n\nBody text.\n",
        encoding="utf-8",
    )
    vault = _make_empty_vault(tmp_path / "vault")

    config = CreekConfig()
    result = Pipeline(config=config).run(source_path=source, vault_path=vault)

    assert result.fragments_created == 1
    assert result.errors == []

    written = list((vault / "01-Fragments").rglob("*.md"))
    assert len(written) == 1
    post = frontmatter.load(str(written[0]))
    assert post["source"]["platform"] == "markdown"
    assert post["id"].startswith("frag-")
    # SHA-256[:12], not uuid8
    assert len(post["id"].removeprefix("frag-")) == 12
    assert "Body text." in post.content

    # Idempotency: re-running writes nothing new.
    Pipeline(config=config).run(source_path=source, vault_path=vault)
    assert len(list((vault / "01-Fragments").rglob("*.md"))) == 1
