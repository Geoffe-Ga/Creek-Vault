"""Tests for CrawDad's async LLM provider seam (#609).

The Anthropic SDK is mocked throughout — no live calls. Covers the normalized
``Completion`` shape, the ``AnthropicAsyncProvider`` request/response mapping
and error redaction, and the ``build_async_provider`` factory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from crawdad.llm import (
    AnthropicAsyncProvider,
    AsyncLLMProvider,
    build_async_provider,
)
from crawdad.llm.base import Completion


class _FakeMessages:
    """Records the create kwargs and returns a canned response/exception."""

    def __init__(self, result: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def create(self, **kwargs: Any) -> Any:
        """Record the call and return the canned result (or raise it)."""
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeAsyncClient:
    """Minimal stand-in for ``anthropic.AsyncAnthropic``."""

    def __init__(self, result: Any) -> None:
        self.messages = _FakeMessages(result)


def _response(text: str, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    """Build a fake messages response with one text block."""
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


def test_completion_defaults() -> None:
    """``Completion`` defaults to an end_turn stop reason and no usage."""
    completion = Completion(text="hi")
    assert completion.stop_reason == "end_turn"
    assert completion.usage is None


async def test_complete_returns_completion() -> None:
    """A successful call maps the SDK reply to a normalized Completion."""
    client = _FakeAsyncClient(_response("hello", stop_reason="max_tokens"))
    provider = AnthropicAsyncProvider(client)  # type: ignore[arg-type]

    result = await provider.complete(
        [{"role": "user", "content": "hi"}], model="claude-x", max_tokens=64
    )

    assert isinstance(result, Completion)
    assert result.text == "hello"
    assert result.stop_reason == "max_tokens"
    assert client.messages.calls[0]["model"] == "claude-x"
    assert client.messages.calls[0]["max_tokens"] == 64


async def test_complete_concatenates_blocks() -> None:
    """Multiple content blocks are joined in order."""
    response = SimpleNamespace(
        content=[
            SimpleNamespace(text="part-a"),
            SimpleNamespace(text="part-b"),
        ],
        stop_reason="end_turn",
    )
    provider = AnthropicAsyncProvider(_FakeAsyncClient(response))  # type: ignore[arg-type]
    result = await provider.complete([], model="m", max_tokens=1)
    assert result.text == "part-a\npart-b"


async def test_complete_defaults_stop_reason_when_absent() -> None:
    """A missing stop reason defaults to end_turn."""
    response = SimpleNamespace(content=[SimpleNamespace(text="x")], stop_reason=None)
    provider = AnthropicAsyncProvider(_FakeAsyncClient(response))  # type: ignore[arg-type]
    result = await provider.complete([], model="m", max_tokens=1)
    assert result.stop_reason == "end_turn"


async def test_complete_redacts_sdk_errors() -> None:
    """``AnthropicError`` becomes ``RuntimeError`` carrying only the type name."""
    import anthropic

    client = _FakeAsyncClient(
        anthropic.AnthropicError("secret request payload and key state")
    )
    provider = AnthropicAsyncProvider(client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError) as exc:
        await provider.complete([], model="m", max_tokens=1)
    message = str(exc.value)
    assert message == "AnthropicError"
    assert "secret" not in message


def test_provider_satisfies_protocol() -> None:
    """``AnthropicAsyncProvider`` is a structural ``AsyncLLMProvider``."""
    provider = AnthropicAsyncProvider(_FakeAsyncClient(_response("x")))  # type: ignore[arg-type]
    assert isinstance(provider, AsyncLLMProvider)


def test_build_async_provider_constructs_anthropic() -> None:
    """The factory wires an AnthropicAsyncProvider with the configured key."""
    config = SimpleNamespace(anthropic_api_key="sk-not-real")
    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        provider = build_async_provider(config)  # type: ignore[arg-type]
    assert isinstance(provider, AnthropicAsyncProvider)
    assert mock_client_cls.call_args.kwargs["api_key"] == "sk-not-real"
