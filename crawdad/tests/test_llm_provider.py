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
    GeminiAsyncProvider,
    OpenAIAsyncProvider,
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
    """The default provider wires an AnthropicAsyncProvider (SDK reads env)."""
    config = SimpleNamespace(llm_provider="anthropic")
    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        provider = build_async_provider(config)  # type: ignore[arg-type]
    assert isinstance(provider, AnthropicAsyncProvider)
    # The SDK reads ANTHROPIC_API_KEY from env; no key is passed through.
    assert mock_client_cls.call_args.kwargs == {}


def test_build_async_provider_constructs_openai() -> None:
    """``llm_provider='openai'`` wires an OpenAIAsyncProvider."""
    config = SimpleNamespace(llm_provider="openai")
    with patch("openai.AsyncOpenAI"):
        provider = build_async_provider(config)  # type: ignore[arg-type]
    assert isinstance(provider, OpenAIAsyncProvider)


def test_build_async_provider_constructs_gemini() -> None:
    """``llm_provider='gemini'`` wires a GeminiAsyncProvider."""
    config = SimpleNamespace(llm_provider="gemini")
    with patch("google.genai.Client"):
        provider = build_async_provider(config)  # type: ignore[arg-type]
    assert isinstance(provider, GeminiAsyncProvider)


def test_build_async_provider_unknown_raises() -> None:
    """An unknown provider raises a clear ``ValueError``."""
    config = SimpleNamespace(llm_provider="frobnicate")
    with pytest.raises(ValueError, match="unknown provider 'frobnicate'"):
        build_async_provider(config)  # type: ignore[arg-type]


# ---- OpenAI async provider ----


class _FakeAsyncCompletions:
    """Records create kwargs; returns a canned response or raises."""

    def __init__(self, result: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def create(self, **kwargs: Any) -> Any:
        """Record the call and return the canned result (or raise it)."""
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeAsyncOpenAI:
    """Minimal stand-in for ``openai.AsyncOpenAI``."""

    def __init__(self, result: Any) -> None:
        self.chat = SimpleNamespace(completions=_FakeAsyncCompletions(result))


def _openai_response(
    text: str | None, *, finish_reason: str = "stop"
) -> SimpleNamespace:
    """Build a fake OpenAI chat response."""
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=9)
    return SimpleNamespace(choices=[choice], usage=usage)


async def test_openai_complete_returns_completion() -> None:
    """OpenAI replies map to a populated Completion."""
    client = _FakeAsyncOpenAI(_openai_response("hi", finish_reason="length"))
    provider = OpenAIAsyncProvider(client)  # type: ignore[arg-type]
    result = await provider.complete(
        [{"role": "user", "content": "p"}], model="gpt-x", max_tokens=10
    )
    assert result.text == "hi"
    assert result.stop_reason == "max_tokens"
    assert result.usage == {"input_tokens": 5, "output_tokens": 9}
    assert client.chat.completions.calls[0]["model"] == "gpt-x"


async def test_openai_complete_handles_null_content() -> None:
    """A ``None`` content normalizes to empty text."""
    provider = OpenAIAsyncProvider(_FakeAsyncOpenAI(_openai_response(None)))  # type: ignore[arg-type]
    result = await provider.complete([], model="m", max_tokens=1)
    assert result.text == ""


async def test_openai_complete_redacts_errors() -> None:
    """``OpenAIError`` becomes ``RuntimeError`` carrying only the type name."""
    import openai

    client = _FakeAsyncOpenAI(openai.OpenAIError("secret payload"))
    provider = OpenAIAsyncProvider(client)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError) as exc:
        await provider.complete([], model="m", max_tokens=1)
    assert str(exc.value) == "OpenAIError"
    assert "secret" not in str(exc.value)


def test_openai_provider_satisfies_protocol() -> None:
    """``OpenAIAsyncProvider`` is a structural ``AsyncLLMProvider``."""
    provider = OpenAIAsyncProvider(_FakeAsyncOpenAI(_openai_response("x")))  # type: ignore[arg-type]
    assert isinstance(provider, AsyncLLMProvider)


# ---- Gemini async provider ----


class _FakeAioModels:
    """Records generate_content kwargs; returns a response or raises."""

    def __init__(self, result: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def generate_content(self, **kwargs: Any) -> Any:
        """Record the call and return the canned result (or raise it)."""
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeGenaiClient:
    """Minimal stand-in for ``google.genai.Client`` with an ``.aio`` accessor."""

    def __init__(self, result: Any) -> None:
        self.aio = SimpleNamespace(models=_FakeAioModels(result))


def _gemini_response(text: str, *, finish_reason: object = "STOP") -> SimpleNamespace:
    """Build a fake Gemini generate_content response."""
    part = SimpleNamespace(text=text)
    content = SimpleNamespace(parts=[part])
    candidate = SimpleNamespace(content=content, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_token_count=4, candidates_token_count=6)
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


async def test_gemini_complete_returns_completion() -> None:
    """Gemini replies map to a populated Completion."""
    client = _FakeGenaiClient(_gemini_response("hello", finish_reason="MAX_TOKENS"))
    provider = GeminiAsyncProvider(client)
    result = await provider.complete(
        [{"role": "user", "content": "p"}], model="gemini-x", max_tokens=10
    )
    assert result.text == "hello"
    assert result.stop_reason == "max_tokens"
    assert result.usage == {"input_tokens": 4, "output_tokens": 6}
    assert client.aio.models.calls[0]["model"] == "gemini-x"
    assert client.aio.models.calls[0]["contents"] == "p"


async def test_gemini_complete_threads_max_tokens() -> None:
    """max_tokens flows into the generation config."""
    client = _FakeGenaiClient(_gemini_response("x"))
    provider = GeminiAsyncProvider(client)
    await provider.complete(
        [{"role": "user", "content": "p"}], model="m", max_tokens=77
    )
    config = client.aio.models.calls[0]["config"]
    assert config.max_output_tokens == 77


async def test_gemini_complete_redacts_errors() -> None:
    """``APIError`` becomes ``RuntimeError`` carrying only the type name."""
    from google.genai import errors

    client = _FakeGenaiClient(errors.APIError(503, {"error": {"message": "secret"}}))
    provider = GeminiAsyncProvider(client)
    with pytest.raises(RuntimeError) as exc:
        await provider.complete([], model="m", max_tokens=1)
    assert str(exc.value) == "APIError"
    assert "secret" not in str(exc.value)


def test_gemini_provider_satisfies_protocol() -> None:
    """``GeminiAsyncProvider`` is a structural ``AsyncLLMProvider``."""
    provider = GeminiAsyncProvider(_FakeGenaiClient(_gemini_response("x")))
    assert isinstance(provider, AsyncLLMProvider)
