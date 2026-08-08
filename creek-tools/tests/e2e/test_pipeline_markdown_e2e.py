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
from creek.scaffold import scaffold_vault

if TYPE_CHECKING:
    from pathlib import Path


def _make_empty_vault(vault_path: Path) -> Path:
    """Materialise the vault layout ``creek init`` ships.

    Built by the real scaffold rather than a local directory list: a
    hand-copied list is what drifted from the template in issue #1025,
    and every writer's ``mkdir(parents=True, exist_ok=True)`` hides the
    difference until a user hits it.
    """
    scaffold_vault(vault_path)
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
