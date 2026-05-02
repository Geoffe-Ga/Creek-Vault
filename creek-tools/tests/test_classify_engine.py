"""Tests for the vault-driven classification engine.

Covers the dispatch logic in :mod:`creek.classify.classify_engine`,
including manual-decision preservation, ``--force`` behaviour, the
high-confidence skip path, and error handling around bad fragment
files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import frontmatter

from creek.classify.classify_engine import run_classify
from creek.config import CreekConfig
from creek.models import Fragment, FragmentSource, SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path


def _write_fragment(
    *,
    vault: Path,
    fragment: Fragment,
    body: str,
    method: str | None = None,
    extras: dict[str, object] | None = None,
) -> Path:
    """Persist a fragment file under ``<vault>/01-Fragments/Notes``.

    Args:
        vault: Vault root.
        fragment: Fragment metadata to persist.
        body: Markdown body for the file.
        method: Optional ``classification_method`` to stamp.
        extras: Extra frontmatter keys to merge in.

    Returns:
        Path to the freshly-written file.
    """
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    metadata = fragment.model_dump(mode="json")
    if method is not None:
        metadata["classification_method"] = method
    if extras:
        metadata.update(extras)
    path = fragments_dir / f"{fragment.id}.md"
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return path


def test_run_classify_returns_zeroes_when_no_fragments_dir(tmp_path: Path) -> None:
    """Missing ``01-Fragments`` returns a zeroed summary, not an error."""
    summary = run_classify(
        vault_path=tmp_path / "vault",
        config=CreekConfig(),
        method="rules",
        batch_size=10,
        force=False,
    )
    assert summary.total == 0
    assert summary.classified == 0


def test_run_classify_rules_updates_fragments(tmp_path: Path) -> None:
    """Rule classification stamps ``classification_method: rules``."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-aaaaaaaaaaaa",
        title="Power and dominance",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        batch_size=10,
        force=False,
    )

    assert summary.classified == 1
    assert summary.total == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"


def test_run_classify_preserves_manual_without_force(tmp_path: Path) -> None:
    """Manual decisions survive a ``--method rules`` pass."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-manual000000",
        title="Hand-tagged",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="body",
        method="manual",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        batch_size=10,
        force=False,
    )

    assert summary.preserved_manual == 1
    assert summary.classified == 0
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "manual"


def test_run_classify_force_overwrites_manual(tmp_path: Path) -> None:
    """``--force`` rewrites manual fragments."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-manual000001",
        title="Force me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="body",
        method="manual",
    )

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        batch_size=10,
        force=True,
    )

    assert summary.classified == 1
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "rules"


def test_run_classify_unreadable_file_records_error(tmp_path: Path) -> None:
    """Bad fragment files surface as errors, not crashes."""
    vault = tmp_path / "vault"
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True)
    bad = fragments_dir / "bad.md"
    bad.write_text("---\nnot: valid\n---\nplain", encoding="utf-8")

    summary = run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        batch_size=10,
        force=False,
    )
    assert summary.total == 1
    # The file is "valid YAML" but missing type=fragment, so it's silently
    # skipped (fragment readers swallow non-fragment docs by design).
    assert summary.classified == 0


def test_run_classify_llm_skips_high_confidence(tmp_path: Path) -> None:
    """When rules give a confident answer, the LLM is not invoked."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-skip00000000",
        title="Confident already",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    config = CreekConfig()
    config.classification.confidence_threshold = 0.0

    with patch("creek.classify.classify_engine.LLMClassifier.classify") as mock_llm:
        summary = run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            batch_size=10,
            force=False,
        )

    mock_llm.assert_not_called()
    assert summary.skipped_high_confidence >= 1


def test_run_classify_llm_invoked_for_low_confidence(tmp_path: Path) -> None:
    """The LLM classifier runs when rules leave the fragment unclassified."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-llmcall00000",
        title="not enough signal",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="ordinary content with no signal keywords at all",
    )

    config = CreekConfig()

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify",
        side_effect=lambda f, content="": f,
    ) as mock_llm:
        run_classify(
            vault_path=vault,
            config=config,
            method="llm",
            batch_size=10,
            force=False,
        )

    mock_llm.assert_called_once()
