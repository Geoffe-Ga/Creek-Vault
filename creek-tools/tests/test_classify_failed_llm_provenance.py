"""A failed LLM classification must not be stamped ``llm`` (#744).

When the provider is unavailable or all retries are exhausted, the orchestrator
returns the fragment unchanged with ``succeeded=False``. The engine must then
stamp the honest ``rules`` provenance (the result that actually stands) — never
``classification_method: llm`` with a fresh ``classified_at`` — so the fragment
stays eligible for a later re-classify instead of being skipped as "already llm".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek.classify.classify_engine import run_classify
from creek.classify.constants import CLASSIFICATION_PROVIDER_KEY
from creek.classify.llm.orchestrator import LLMClassificationResult
from creek.config import CreekConfig, LLMConfig, LLMRoutingConfig
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from creek.config import LLMConfig as _LLMConfig


class _FailingClassifier:
    """Stands in for a provider whose calls all fail (returns ``succeeded=False``)."""

    def __init__(self, config: _LLMConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return True  # availability passes; the *call* fails

    def classify_with_reasoning(
        self, fragment: Fragment, content: str
    ) -> LLMClassificationResult:
        del content
        return LLMClassificationResult(fragment=fragment, reasoning="", succeeded=False)


class _SucceedingClassifier:
    """Stands in for a provider whose calls succeed (default ``succeeded=True``)."""

    def __init__(self, config: _LLMConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return True

    def classify_with_reasoning(
        self, fragment: Fragment, content: str
    ) -> LLMClassificationResult:
        del content
        return LLMClassificationResult(fragment=fragment, reasoning="ok")


def _local_config() -> CreekConfig:
    """A config whose classification stage is local (no env needed)."""
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="ollama", model="qwen3:8b"),
        )
    )


def _seed(vault: Path) -> Path:
    """Write one fragment with no classifiable keywords, return its path."""
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-000000fail01",
            title="zzz",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body="zzz zzz zzz",
    )
    return next((vault / "01-Fragments").rglob("*.md"))


def test_failed_llm_is_not_stamped_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed LLM call leaves the honest rules provenance, never ``llm``."""
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _FailingClassifier
    )
    vault = tmp_path / "vault"
    md = _seed(vault)

    run_classify(vault_path=vault, config=_local_config(), method="llm", force=True)

    meta = frontmatter.load(md).metadata
    assert meta.get("classification_method") != "llm"  # the bug: was llm
    assert meta.get("classification_method") == "rules"
    assert CLASSIFICATION_PROVIDER_KEY not in meta  # no provider stamp off the llm path


def test_successful_llm_is_still_stamped_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path is unchanged — a real classification still stamps ``llm``."""
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _SucceedingClassifier
    )
    vault = tmp_path / "vault"
    md = _seed(vault)

    run_classify(vault_path=vault, config=_local_config(), method="llm", force=True)

    meta = frontmatter.load(md).metadata
    assert meta.get("classification_method") == "llm"
    assert meta.get(CLASSIFICATION_PROVIDER_KEY) == "ollama"
