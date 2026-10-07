"""The harness can only ever measure a local model.

``LocalOnlyFactory`` refuses a provider registered as cloud at construction,
and refuses any built callable that does not positively declare
``is_cloud = False`` — a missing attribute fails *closed*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.models import PrivacyTier
from creek_mcp.bench.local_only import (
    CloudProviderRefusedError,
    LocalOnlyFactory,
    endpoint_scope,
    require_local_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _resolver(*addresses: str) -> Callable[[str], list[str]]:
    """A resolver that answers every host with *addresses*."""

    def _resolve(host: str) -> list[str]:
        del host
        return list(addresses)

    return _resolve


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (("127.0.0.1",), "loopback"),
        (("::1",), "loopback"),
        (("127.0.0.1", "::1"), "loopback"),
        (("10.1.2.3",), "private"),
        (("192.168.0.9", "127.0.0.1"), "private"),
        (("fd00::1",), "private"),
        (("192.168.0.9", "8.8.8.8"), "remote"),
        (("8.8.4.4",), "remote"),
        (("100.64.0.1",), "remote"),
        ((), "remote"),
    ],
)
def test_endpoint_scope_comes_from_where_the_host_resolves(
    addresses: tuple[str, ...], expected: str
) -> None:
    """Every resolved address must be local; one public address makes it remote."""
    scope = endpoint_scope("http://ollama.example:11434", resolve=_resolver(*addresses))
    assert scope == expected


def test_endpoint_scope_of_an_ip_literal_needs_no_lookup() -> None:
    """Loopback literals classify with the real resolver, offline."""
    assert endpoint_scope("http://127.0.0.1:11434") == "loopback"
    assert endpoint_scope("http://[::1]:11434") == "loopback"


def test_unresolvable_host_is_remote() -> None:
    """A name that does not resolve cannot be shown local, so it is not."""
    assert endpoint_scope("http://no-such-host.invalid:11434") == "remote"


def test_remote_endpoint_refused_without_the_operator_flag() -> None:
    """A public endpoint is refused unless the operator opts in, and recorded."""
    public = _resolver("8.8.4.4")
    with pytest.raises(CloudProviderRefusedError, match="allow-remote-host"):
        require_local_target(
            "https://gpu.example", "mistral:7b", allow_remote=False, resolve=public
        )
    scope = require_local_target(
        "https://gpu.example", "mistral:7b", allow_remote=True, resolve=public
    )
    assert scope == "remote"


@pytest.mark.parametrize("tag", ["gpt-oss:120b-cloud", "deepseek-v3.1:cloud"])
def test_cloud_offload_tag_refused_even_on_loopback(tag: str) -> None:
    """A local daemon forwards ``*-cloud`` tags off-host; never benchmark them."""
    with pytest.raises(CloudProviderRefusedError, match="cloud"):
        require_local_target(
            "http://127.0.0.1:11434",
            tag,
            allow_remote=True,
            resolve=_resolver("127.0.0.1"),
        )


@pytest.mark.parametrize("tag", ["cloudy:7b", "mistral:7b", "cloud-model:q4"])
def test_ordinary_tags_are_not_mistaken_for_cloud(tag: str) -> None:
    """Only a ``-cloud`` / ``:cloud`` suffix marks an offload tag."""
    scope = require_local_target(
        "http://127.0.0.1:11434",
        tag,
        allow_remote=False,
        resolve=_resolver("127.0.0.1"),
    )
    assert scope == "loopback"


def test_url_without_a_host_refused() -> None:
    """A URL naming no host cannot be judged local."""
    with pytest.raises(CloudProviderRefusedError, match="host"):
        require_local_target("not a url", "mistral:7b", allow_remote=False)
