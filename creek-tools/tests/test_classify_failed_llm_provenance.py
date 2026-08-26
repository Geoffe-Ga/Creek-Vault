"""A failed LLM classification must not be stamped ``llm`` (#744).

When the provider is unavailable or all retries are exhausted, the orchestrator
returns the fragment unchanged with ``succeeded=False``. The engine must then
stamp the honest ``rules`` provenance (the result that actually stands) — never
``classification_method: llm`` with a fresh ``classified_at`` — so the fragment
stays eligible for a later re-classify instead of being skipped as "already llm".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

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


# ---- #1356: the two reasons the LLM did not classify are counted apart ----
#
# ``skipped_high_confidence`` is documented as "the rules were confident", but
# since #744 a failed provider call reaches the same write path. Conflating the
# two inverts the operator's next move: rule-confident skips mean the run went
# well, provider failures mean the corpus is under-classified and wants a
# re-run. Every assertion below is on the run summary the CLI and the MCP tool
# both read.

_CONFIDENT_BODY: Final[str] = (
    "Power dominance control conquest force aggression bold rage warrior"
)
"""A body the rule classifier scores above any floor, so the LLM is never called."""


def _confident_config() -> CreekConfig:
    """A local config whose confidence floor every rule verdict clears.

    Returns:
        A :class:`CreekConfig` forcing the rule short-circuit in ``_classify_one``.
    """
    config = _local_config()
    config.classification.confidence_threshold = 0.0
    return config


def _seed_confident(vault: Path) -> Path:
    """Write one fragment the rule classifier can classify confidently.

    Args:
        vault: Vault root (created on demand).

    Returns:
        Path to the freshly-written fragment file.
    """
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id="frag-000000conf01",
            title="confident",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body=_CONFIDENT_BODY,
    )
    return next((vault / "01-Fragments").rglob("*.md"))


def test_failed_llm_call_is_not_counted_as_a_rule_short_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider failure lands on ``llm_call_failed``, never on the rule counter."""
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _FailingClassifier
    )
    vault = tmp_path / "vault"
    _seed(vault)

    summary = run_classify(
        vault_path=vault, config=_local_config(), method="llm", force=True
    )

    assert summary.total == 1, "positive control: the run must visit the fixture"
    assert summary.classified == 1
    assert summary.llm_call_failed == 1
    assert summary.skipped_high_confidence == 0


def test_rule_short_circuit_is_not_counted_as_an_llm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rules-sufficed skip stays on ``skipped_high_confidence`` alone."""
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _FailingClassifier
    )
    vault = tmp_path / "vault"
    _seed_confident(vault)

    summary = run_classify(
        vault_path=vault, config=_confident_config(), method="llm", force=True
    )

    assert summary.total == 1, "positive control: the run must visit the fixture"
    assert summary.classified == 1
    assert summary.skipped_high_confidence == 1
    assert summary.llm_call_failed == 0


def test_successful_llm_call_is_counted_on_neither_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success control: a real LLM verdict is not a skip of either kind."""
    monkeypatch.setattr(
        "creek.classify.classify_engine.LLMClassifier", _SucceedingClassifier
    )
    vault = tmp_path / "vault"
    _seed(vault)

    summary = run_classify(
        vault_path=vault, config=_local_config(), method="llm", force=True
    )

    assert summary.classified == 1
    assert summary.skipped_high_confidence == 0
    assert summary.llm_call_failed == 0


def test_rules_method_run_reports_neither_skip_reason(tmp_path: Path) -> None:
    """``--method rules`` invokes no LLM at all, so neither counter moves.

    Guards the mapping in the other direction: a rules run must not start
    reporting its whole corpus as a rule short-circuit just because the
    outcome value now has a name for that state.
    """
    vault = tmp_path / "vault"
    _seed_confident(vault)

    summary = run_classify(
        vault_path=vault, config=_local_config(), method="rules", force=True
    )

    assert summary.classified == 1
    assert summary.skipped_high_confidence == 0
    assert summary.llm_call_failed == 0
