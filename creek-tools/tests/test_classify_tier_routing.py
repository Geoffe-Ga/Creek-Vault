"""Per-tier classification routing (#666).

`creek classify --method llm` must route each fragment's classification by its
privacy tier: an Intimate fragment is classified by a LOCAL provider (via the
ModelRouter Intimate-never-cloud chokepoint, #647) even when the classification
stage is a cloud model, while non-Intimate fragments use the configured model.

These tests assert on which provider classified which fragment (not just a
returned string), so a regression that lets Intimate content reach a cloud
provider fails the guard. The LLM is faked — no network, no real provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from creek.classify.classify_engine import run_classify
from creek.classify.llm import LLMClassificationResult
from creek.classify.llm.router import IntimateRoutingError
from creek.config import CreekConfig, LLMConfig, LLMRoutingConfig
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

_BODY = "a reflection worth keeping with enough words to classify cleanly"


@dataclass
class _Call:
    provider: str
    fragment_id: str


class _FakeClassifier:
    """Records which provider classified which fragment; no real LLM call."""

    def __init__(self, config: LLMConfig) -> None:
        """Capture the resolved config (its ``provider`` is what we assert on)."""
        self.config = config

    @property
    def available(self) -> bool:
        """Always reachable — the availability gate is not under test here."""
        return True

    def classify_with_reasoning(
        self, fragment: Fragment, content: str
    ) -> LLMClassificationResult:
        """Record ``(provider, fragment id)`` and echo the fragment back."""
        _ = content
        _CALLS.append(_Call(provider=self.config.provider, fragment_id=fragment.id))
        return LLMClassificationResult(fragment=fragment, reasoning="")


_CALLS: list[_Call] = []


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the engine's LLMClassifier for the recording fake; reset the log."""
    _CALLS.clear()
    monkeypatch.setattr("creek.classify.classify_engine.LLMClassifier", _FakeClassifier)


def _config(*, classification: dict[str, str], default: dict[str, str]) -> CreekConfig:
    """A CreekConfig whose router resolves the given classification + default."""
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**default),
            classification=LLMConfig(**classification),
        )
    )


def _write(vault: Path, frag_id: str, tier: PrivacyTier) -> None:
    """Write a classifiable fragment at *tier* under the vault."""
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=frag_id,
            title=frag_id,
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            privacy_tier=tier,
        ),
        body=_BODY,
    )


def _provider_for(fragment_id: str) -> str:
    """The provider that classified *fragment_id* (asserts exactly one call)."""
    hits = [c.provider for c in _CALLS if c.fragment_id == fragment_id]
    assert len(hits) == 1, f"expected one classify call for {fragment_id}, got {hits}"
    return hits[0]


_CLOUD = {"provider": "anthropic", "model": "claude-haiku-4-5"}
_LOCAL = {"provider": "ollama", "model": "qwen3:8b"}


class TestPerTierRouting:
    """Intimate goes local; non-Intimate uses the configured cloud model."""

    def test_intimate_local_nonintimate_cloud(self, tmp_path: Path) -> None:
        """One run: Intimate classified by ollama, Open by anthropic."""
        vault = tmp_path / "vault"
        _write(vault, "frag-intimateaaa", PrivacyTier.INTIMATE)
        _write(vault, "frag-openbbbbbbb", PrivacyTier.OPEN)

        run_classify(
            vault_path=vault,
            config=_config(classification=_CLOUD, default=_LOCAL),
            method="llm",
            force=True,
        )

        assert _provider_for("frag-intimateaaa") == "ollama"  # redirected local
        assert _provider_for("frag-openbbbbbbb") == "anthropic"  # configured cloud

    def test_local_classification_is_backward_compatible(self, tmp_path: Path) -> None:
        """A local classification stage classifies every tier with that provider."""
        vault = tmp_path / "vault"
        _write(vault, "frag-intimateccc", PrivacyTier.INTIMATE)
        _write(vault, "frag-personalddd", PrivacyTier.PERSONAL)

        run_classify(
            vault_path=vault,
            config=_config(classification=_LOCAL, default=_LOCAL),
            method="llm",
            force=True,
        )

        assert _provider_for("frag-intimateccc") == "ollama"
        assert _provider_for("frag-personalddd") == "ollama"


class TestNoLocalBackend:
    """All-cloud config: the Intimate failure is deferred to actual Intimate work."""

    def test_intimate_fragment_raises_when_no_local_backend(
        self, tmp_path: Path
    ) -> None:
        """An Intimate fragment with cloud classification + cloud default raises."""
        vault = tmp_path / "vault"
        _write(vault, "frag-intimateeee", PrivacyTier.INTIMATE)

        with pytest.raises(IntimateRoutingError):
            run_classify(
                vault_path=vault,
                config=_config(classification=_CLOUD, default=_CLOUD),
                method="llm",
                force=True,
            )

    def test_no_intimate_fragments_still_classifies(self, tmp_path: Path) -> None:
        """All-cloud config with no Intimate content runs fine (error deferred)."""
        vault = tmp_path / "vault"
        _write(vault, "frag-openeeeeeee", PrivacyTier.OPEN)

        run_classify(
            vault_path=vault,
            config=_config(classification=_CLOUD, default=_CLOUD),
            method="llm",
            force=True,
        )

        assert _provider_for("frag-openeeeeeee") == "anthropic"
