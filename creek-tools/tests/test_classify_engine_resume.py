"""Regression tests for the OPS-001 LLM checkpoint/resume contract.

After a partial ``creek classify --method llm`` run, re-running the
command must not re-classify fragments that already carry
``classification_method: llm``. The progress checkpoint at
``<vault>/00-Creek-Meta/Processing-Log/llm-progress.json`` must capture
each newly-classified fragment ID so an operator can audit the run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import frontmatter

from creek.classify.classify_engine import LLM_PROGRESS_FILENAME, run_classify
from creek.config import CreekConfig
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file as _write_fragment

if TYPE_CHECKING:
    from pathlib import Path


def _llm_low_confidence_config() -> CreekConfig:
    """Force the engine to invoke the LLM rather than short-circuiting on rules."""
    config = CreekConfig()
    # Threshold above any plausible rule confidence so the LLM is always
    # invoked — keeps the test deterministic.
    config.classification.confidence_threshold = 1.0
    return config


def _seed_unclassified_fragment(
    *,
    vault: Path,
    frag_id: str,
    title: str,
) -> Path:
    """Write a fragment with no rule signal, so the LLM path is exercised."""
    fragment = Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    return _write_fragment(vault=vault, fragment=fragment, body="ambiguous body")


def test_already_llm_classified_fragment_is_skipped(tmp_path: Path) -> None:
    """A fragment with ``classification_method: llm`` is left alone (OPS-001).

    This is what makes a re-run after a crash a resume rather than a
    full restart — and what saves the operator from re-paying for
    Anthropic tokens already spent.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-already1234",
        title="already done",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    file = _write_fragment(
        vault=vault,
        fragment=fragment,
        body="ambiguous body",
        method="llm",
    )

    with patch("creek.classify.classify_engine.LLMClassifier.classify") as mock_llm:
        summary = run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=False,
        )

    mock_llm.assert_not_called()
    assert summary.preserved_manual == 1
    assert summary.classified == 0
    reloaded = frontmatter.load(str(file))
    assert reloaded["classification_method"] == "llm"


def test_force_overrides_resume_skip(tmp_path: Path) -> None:
    """``--force`` re-classifies even fragments already marked ``llm``."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-force00000a",
        title="force me",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="ambiguous body",
        method="llm",
    )

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify",
        side_effect=lambda frag, content="": frag,
    ) as mock_llm:
        run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=True,
        )

    mock_llm.assert_called_once()


def test_progress_file_records_classified_fragment_ids(tmp_path: Path) -> None:
    """Each LLM-classified fragment ID is appended to the progress file."""
    vault = tmp_path / "vault"
    _seed_unclassified_fragment(
        vault=vault,
        frag_id="frag-progress001",
        title="first",
    )
    _seed_unclassified_fragment(
        vault=vault,
        frag_id="frag-progress002",
        title="second",
    )

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify",
        side_effect=lambda frag, content="": frag,
    ):
        run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=False,
        )

    progress_file = vault / "00-Creek-Meta" / "Processing-Log" / LLM_PROGRESS_FILENAME
    assert progress_file.exists()
    recorded_ids = [
        json.loads(line)["id"]
        for line in progress_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert set(recorded_ids) == {"frag-progress001", "frag-progress002"}


def test_progress_file_appends_across_runs(tmp_path: Path) -> None:
    """A second resume run appends to the existing progress file (OPS-001).

    The recovery guarantee depends on the progress file growing —
    truncating on every invocation would erase the audit trail of
    fragments classified before a crash. This test seeds two
    fragments, lets the first run classify only one of them, then
    seeds a third, runs again, and confirms all three IDs are present
    in the appended file.
    """
    vault = tmp_path / "vault"
    _seed_unclassified_fragment(
        vault=vault,
        frag_id="frag-append0001",
        title="first",
    )

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify",
        side_effect=lambda frag, content="": frag,
    ):
        run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=False,
        )

    # Add two more unclassified fragments and resume.
    _seed_unclassified_fragment(
        vault=vault,
        frag_id="frag-append0002",
        title="second",
    )
    _seed_unclassified_fragment(
        vault=vault,
        frag_id="frag-append0003",
        title="third",
    )

    with patch(
        "creek.classify.classify_engine.LLMClassifier.classify",
        side_effect=lambda frag, content="": frag,
    ):
        run_classify(
            vault_path=vault,
            config=_llm_low_confidence_config(),
            method="llm",
            force=False,
        )

    progress_file = vault / "00-Creek-Meta" / "Processing-Log" / LLM_PROGRESS_FILENAME
    recorded_ids = [
        json.loads(line)["id"]
        for line in progress_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    # The first ID survives the second run (file appended, not truncated).
    assert set(recorded_ids) == {
        "frag-append0001",
        "frag-append0002",
        "frag-append0003",
    }


def test_progress_file_not_created_for_rules_method(tmp_path: Path) -> None:
    """``--method rules`` does not touch the LLM-progress checkpoint."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-rulesonly00",
        title="Power and dominance",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        vault=vault,
        fragment=fragment,
        body="Power dominance control conquest force aggression bold rage warrior",
    )

    run_classify(
        vault_path=vault,
        config=CreekConfig(),
        method="rules",
        force=False,
    )

    progress_file = vault / "00-Creek-Meta" / "Processing-Log" / LLM_PROGRESS_FILENAME
    assert not progress_file.exists()
