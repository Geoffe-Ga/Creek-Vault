"""The harness can only ever measure a local model.

``LocalOnlyFactory`` refuses a provider registered as cloud at construction,
and refuses any built callable that does not positively declare
``is_cloud = False`` — a missing attribute fails *closed*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.models import PrivacyTier
from creek_mcp.bench.local_only import CloudProviderRefusedError, LocalOnlyFactory

if TYPE_CHECKING:
    from creek_mcp.bench.protocols import LLMCallable


class _Callable:
    """A completion callable with a configurable ``is_cloud`` flag."""

    def __init__(self, is_cloud: bool) -> None:
        """Record the flag."""
        self.is_cloud = is_cloud

    def __call__(self, prompt: str) -> str:
        """Echo the prompt."""
        return prompt


def _bare(prompt: str) -> str:
    """A completion callable that declares nothing about where it runs."""
    return prompt


@pytest.mark.parametrize("name", ["anthropic", "openai", "gemini"])
def test_cloud_name_refused_at_construction(name: str) -> None:
    """A cloud provider name is refused before anything is built."""
    built: list[int] = []

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        built.append(max_tokens)
        return _Callable(is_cloud=False)

    with pytest.raises(CloudProviderRefusedError, match="cloud"):
        LocalOnlyFactory(_factory, provider_name=name)
    assert built == []


def test_cloud_flagged_callable_refused() -> None:
    """A callable that says it is cloud is refused when built."""

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        return _Callable(is_cloud=True)

    factory = LocalOnlyFactory(_factory, provider_name="ollama")
    with pytest.raises(CloudProviderRefusedError, match="cloud"):
        factory(PrivacyTier.OPEN, max_tokens=8)


def test_missing_is_cloud_fails_closed() -> None:
    """A callable that declares nothing is treated as cloud."""

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        return _bare

    factory = LocalOnlyFactory(_factory, provider_name="fake")
    with pytest.raises(CloudProviderRefusedError, match="cloud"):
        factory.preflight()


def test_local_callable_is_returned_unchanged() -> None:
    """A positively-local callable passes through and receives the arguments."""
    seen: list[tuple[PrivacyTier, int]] = []
    local = _Callable(is_cloud=False)

    def _factory(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        seen.append((tier, max_tokens))
        return local

    factory = LocalOnlyFactory(_factory, provider_name="ollama")
    assert factory(PrivacyTier.PERSONAL, max_tokens=64) is local
    factory.preflight()
    assert seen == [(PrivacyTier.PERSONAL, 64), (PrivacyTier.OPEN, 1)]
