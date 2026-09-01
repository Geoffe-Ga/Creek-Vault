"""Tests for the synchronous GeminiProvider (#608).

The ``google-genai`` SDK is mocked throughout — no live calls. Covers the
consent + key gate (either ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY``), model
resolution, the ``generate_content`` call, ``finish_reason`` and usage mapping,
error redaction, and a mocked end-to-end classify call.
"""

from __future__ import annotations

from typing import Final
from unittest.mock import MagicMock, patch

import pytest

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import Completion
from creek.classify.llm.consent import CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV
from creek.classify.llm.providers import (
    GeminiProvider,
    _extract_gemini_text,
    _extract_gemini_usage,
    _map_gemini_stop_reason,
    build_provider,
    provider_is_cloud,
)
from creek.config import LLMConfig

_KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no consent and no Gemini key variables set."""
    monkeypatch.delenv(CLOUD_CONSENT_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CONSENT_ENV, raising=False)
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set GOOGLE_API_KEY and cloud consent so a GeminiProvider constructs."""
    monkeypatch.setenv("GOOGLE_API_KEY", "g-not-real")
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")


_DEFAULT: Final[object] = object()
"""Sentinel: build the well-formed default value for this response attribute."""

_ABSENT: Final[object] = object()
"""Sentinel: omit the attribute, so ``getattr(response, name, None)`` is ``None``."""


def _set_or_delete(
    target: MagicMock, name: str, value: object, default: object
) -> None:
    """Apply the ``_DEFAULT`` / ``_ABSENT`` / explicit-value dispatch to *target*.

    Args:
        target: The mock to mutate.
        name: The attribute name.
        value: ``_DEFAULT``, ``_ABSENT``, or the literal value to set.
        default: The well-formed value used when *value* is ``_DEFAULT``.
    """
    if value is _ABSENT:
        delattr(target, name)
        return
    setattr(target, name, default if value is _DEFAULT else value)


def _make_mock_genai_response(
    text: object = "hello world",
    *,
    finish_reason: object = "STOP",
    prompt_tokens: object = 12,
    candidate_tokens: object = 8,
    texts: tuple[object, ...] | None = None,
    content: object = _DEFAULT,
    candidates: object = _DEFAULT,
    usage_metadata: object = _DEFAULT,
) -> MagicMock:
    """Build a mock ``generate_content`` response, degenerate envelopes included.

    ``content``, ``candidates`` and ``usage_metadata`` each accept ``_DEFAULT``
    (the well-formed value built from the other arguments), ``_ABSENT`` (the
    attribute is deleted, so ``getattr(obj, name, None)`` returns ``None``), or
    an explicit value such as ``[]`` or ``None``.

    Args:
        text: The single part's ``text``, when *texts* is not given.
        finish_reason: The first candidate's raw ``finish_reason``.
        prompt_tokens: The usage object's ``prompt_token_count``.
        candidate_tokens: The usage object's ``candidates_token_count``.
        texts: One part is built per element, for multi-part joins.
        content: Sentinel or explicit override for ``candidates[0].content``.
        candidates: Sentinel or explicit override for ``response.candidates``.
        usage_metadata: Sentinel or explicit override for the usage object.

    Returns:
        The mock response object.
    """
    parts: list[MagicMock] = []
    for value in (text,) if texts is None else texts:
        part = MagicMock()
        part.text = value
        parts.append(part)
    default_content = MagicMock()
    default_content.parts = parts
    candidate = MagicMock()
    _set_or_delete(candidate, "content", content, default_content)
    candidate.finish_reason = finish_reason
    default_usage = MagicMock()
    default_usage.prompt_token_count = prompt_tokens
    default_usage.candidates_token_count = candidate_tokens
    response = MagicMock()
    _set_or_delete(response, "candidates", candidates, [candidate])
    _set_or_delete(response, "usage_metadata", usage_metadata, default_usage)
    return response


def _make_mock_genai_client(
    text: str,
    *,
    finish_reason: object = "STOP",
    prompt_tokens: int = 12,
    candidate_tokens: int = 8,
) -> MagicMock:
    """Build a mock ``genai.Client`` returning one generate_content response."""
    response = _make_mock_genai_response(
        text,
        finish_reason=finish_reason,
        prompt_tokens=prompt_tokens,
        candidate_tokens=candidate_tokens,
    )
    client = MagicMock()
    client.models.generate_content.return_value = response
    return client


class TestGeminiConstruction:
    """Consent + API-key gate at construction."""

    def test_raises_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither key var set → RuntimeError mentioning the key vars."""
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        with pytest.raises(RuntimeError, match=r"GOOGLE_API_KEY|GEMINI_API_KEY"):
            GeminiProvider(LLMConfig(provider="gemini"))

    def test_constructs_with_google_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GOOGLE_API_KEY`` satisfies the key requirement."""
        monkeypatch.setenv("GOOGLE_API_KEY", "g-not-real")
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        assert GeminiProvider(LLMConfig(provider="gemini")).available is True

    def test_constructs_with_gemini_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The alternate ``GEMINI_API_KEY`` also satisfies the requirement."""
        monkeypatch.setenv("GEMINI_API_KEY", "g-not-real")
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        assert GeminiProvider(LLMConfig(provider="gemini")).available is True

    def test_raises_when_consent_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Key present but no consent → RuntimeError naming Gemini."""
        monkeypatch.setenv("GOOGLE_API_KEY", "g-not-real")
        with pytest.raises(RuntimeError, match="Gemini"):
            GeminiProvider(LLMConfig(provider="gemini"))

    def test_is_cloud_and_provider_name(self, gemini_env: None) -> None:
        """Gemini is a cloud backend with a display name."""
        provider = GeminiProvider(LLMConfig(provider="gemini"))
        assert provider.is_cloud is True
        assert provider.PROVIDER_NAME == "Gemini"


class TestGeminiFactoryRegistration:
    """The factory knows the gemini backend."""

    def test_build_provider_returns_gemini(self, gemini_env: None) -> None:
        """``build_provider`` constructs a GeminiProvider for 'gemini'."""
        provider = build_provider(LLMConfig(provider="gemini"))
        assert isinstance(provider, GeminiProvider)
        assert isinstance(provider, LLMProvider)

    def test_provider_is_cloud_gemini(self) -> None:
        """'gemini' is registered as a cloud provider (no instantiation)."""
        assert provider_is_cloud("gemini") is True


class TestGeminiModelResolution:
    """Model resolution mirrors the other providers."""

    def test_default_model_literal(self) -> None:
        """The default Gemini model is the current stable flash tier."""
        assert GeminiProvider.DEFAULT_MODEL == "gemini-3.5-flash"

    def test_falls_back_when_model_unset(self, gemini_env: None) -> None:
        """An unset ``config.model`` (``None``) falls back to the Gemini default."""
        provider = GeminiProvider(LLMConfig(provider="gemini"))
        assert provider.model == GeminiProvider.DEFAULT_MODEL

    def test_honors_explicit_model(self, gemini_env: None) -> None:
        """An explicit config model is honoured."""
        provider = GeminiProvider(LLMConfig(provider="gemini", model="gemini-x"))
        assert provider.model == "gemini-x"

    def test_honors_explicit_mistral_verbatim(self, gemini_env: None) -> None:
        """An explicit ``model: mistral`` is sent verbatim, never swapped (#621)."""
        provider = GeminiProvider(LLMConfig(provider="gemini", model="mistral"))
        assert provider.model == "mistral"


class TestGeminiComplete:
    """The complete() call against a mocked SDK."""

    def test_complete_returns_completion(self, gemini_env: None) -> None:
        """A successful call normalizes to a populated Completion."""
        client = _make_mock_genai_client("hi gemini")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            result = provider.complete("a prompt")
        assert isinstance(result, Completion)
        assert result.text == "hi gemini"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 12, "output_tokens": 8}

    def test_complete_calls_generate_content(self, gemini_env: None) -> None:
        """The SDK is called with the resolved model and the prompt contents."""
        client = _make_mock_genai_client("x")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            provider.complete("the prompt")
        kwargs = client.models.generate_content.call_args.kwargs
        assert kwargs["model"] == GeminiProvider.DEFAULT_MODEL
        assert kwargs["contents"] == "the prompt"

    def test_complete_maps_max_tokens_string(self, gemini_env: None) -> None:
        """A string ``MAX_TOKENS`` finish reason maps to ``max_tokens``."""
        client = _make_mock_genai_client("cut", finish_reason="MAX_TOKENS")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            assert provider.complete("p").stop_reason == "max_tokens"

    def test_complete_maps_enum_finish_reason(self, gemini_env: None) -> None:
        """An enum finish reason (``.name``) maps correctly too."""
        from google.genai.types import FinishReason

        client = _make_mock_genai_client("ok", finish_reason=FinishReason.STOP)
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            assert provider.complete("p").stop_reason == "end_turn"

    def test_complete_threads_max_tokens_into_config(self, gemini_env: None) -> None:
        """An explicit max_tokens flows into the generation config."""
        client = _make_mock_genai_client("x")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            provider.complete("p", max_tokens=321)
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.max_output_tokens == 321

    def test_complete_passes_system_instruction(self, gemini_env: None) -> None:
        """A system prefix flows into the config's system_instruction."""
        client = _make_mock_genai_client("x")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            provider.complete("p", system="be terse")
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.system_instruction == "be terse"

    def test_complete_redacts_sdk_errors(self, gemini_env: None) -> None:
        """SDK errors surface only the exception type name, not request state."""
        from google.genai import errors

        client = MagicMock()
        client.models.generate_content.side_effect = errors.APIError(
            503, {"error": {"message": "secret request payload"}}
        )
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        message = str(exc.value)
        assert "APIError" in message
        assert "secret" not in message

    def test_complete_4xx_surfaces_api_message(self, gemini_env: None) -> None:
        """A 4xx surfaces Gemini's own error message (the actionable cause; #745)."""
        from google.genai import errors

        client = MagicMock()
        client.models.generate_content.side_effect = errors.APIError(
            400, {"error": {"message": "quota exceeded for this project"}}
        )
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        message = str(exc.value)
        assert "quota exceeded" in message
        assert "HTTP 400" in message

    def test_complete_5xx_withholds_body_and_suppresses_chain(
        self, gemini_env: None
    ) -> None:
        """A 5xx is status-only; its chain is suppressed (no body via `__cause__`)."""
        from google.genai import errors

        client = MagicMock()
        client.models.generate_content.side_effect = errors.APIError(
            503, {"error": {"message": "secret internal detail"}}
        )
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        assert "secret internal detail" not in str(exc.value)
        assert "HTTP 503" in str(exc.value)
        assert exc.value.__cause__ is None

    def test_rate_limit_surfaces_retry_after(self, gemini_env: None) -> None:
        """A vendor 429 maps to ProviderRateLimitError carrying Retry-After."""
        import httpx
        from google.genai import errors

        from creek.classify.llm.providers import ProviderRateLimitError

        response = httpx.Response(
            429,
            headers={"retry-after": "11"},
            request=httpx.Request("POST", "https://generativelanguage.googleapis.com"),
        )
        client = MagicMock()
        client.models.generate_content.side_effect = errors.APIError(
            429, {"error": {"message": "secret request payload"}}, response
        )
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            with pytest.raises(ProviderRateLimitError) as exc:
                provider.complete("p")
        assert exc.value.retry_after == 11.0
        assert "secret" not in str(exc.value)

    def test_rate_limit_without_response_has_no_retry_after(
        self, gemini_env: None
    ) -> None:
        """A 429 with no response headers still maps, with ``retry_after=None``."""
        from google.genai import errors

        from creek.classify.llm.providers import ProviderRateLimitError

        client = MagicMock()
        client.models.generate_content.side_effect = errors.APIError(
            429, {"error": {"message": "rate limited"}}
        )
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            with pytest.raises(ProviderRateLimitError) as exc:
                provider.complete("p")
        assert exc.value.retry_after is None


def test_gemini_end_to_end_classify(gemini_env: None) -> None:
    """A mocked end-to-end classify via provider=gemini returns a Completion."""
    from creek.classify.llm.orchestrator import LLMClassifier

    client = _make_mock_genai_client("response body")
    with patch("google.genai.Client", return_value=client):
        classifier = LLMClassifier(config=LLMConfig(provider="gemini"))
        completion = classifier.invoke_prompt_with_metadata("classify this")
    assert isinstance(completion, Completion)
    assert completion.text == "response body"
    assert completion.usage == {"input_tokens": 12, "output_tokens": 8}


# --------------------------------------------------------------------------- #
# Degenerate SDK envelopes: the fallback arms (#1449)
# --------------------------------------------------------------------------- #


class TestGeminiDegenerateResponse:
    """A malformed or partial SDK envelope degrades to a fixed value, never raises."""

    def test_extract_text_returns_empty_string_for_empty_candidates(self) -> None:
        """A response with zero candidates yields "" — never an IndexError."""
        response = _make_mock_genai_response(candidates=[])
        assert _extract_gemini_text(response) == ""

    def test_extract_text_returns_empty_string_when_candidates_absent(self) -> None:
        """No ``candidates`` attribute at all yields "".

        ``getattr`` falls back to ``None``, which the ``if not candidates``
        guard treats as empty — the same arm the empty-list case reaches.
        """
        response = _make_mock_genai_response(candidates=_ABSENT)
        assert _extract_gemini_text(response) == ""

    def test_the_absent_sentinel_really_deletes_the_attribute(self) -> None:
        """Guard the fake itself: ``_ABSENT`` must remove the attribute outright.

        Without this, a silent regression in ``_set_or_delete`` would leave
        MagicMock's auto-created attribute in place and every ``_ABSENT`` case
        would still pass — for the wrong reason.
        """
        response = _make_mock_genai_response(candidates=_ABSENT, usage_metadata=_ABSENT)
        assert not hasattr(response, "candidates")
        assert not hasattr(response, "usage_metadata")

    @pytest.mark.parametrize("content", [None, _ABSENT], ids=["null", "absent"])
    def test_extract_text_is_empty_when_the_candidate_carries_no_content(
        self, content: object
    ) -> None:
        """A null or missing ``content`` yields "" — the ``parts`` normalisation."""
        response = _make_mock_genai_response(content=content)
        assert _extract_gemini_text(response) == ""

    def test_extract_text_skips_non_str_parts_and_joins_the_rest(self) -> None:
        """Only ``str`` parts join; a non-str part is dropped, not stringified."""
        response = _make_mock_genai_response(texts=("a", 7, "b"))
        assert _extract_gemini_text(response) == "ab"

    def test_stop_reason_is_end_turn_for_empty_candidates(self) -> None:
        """Zero candidates maps to "end_turn" — never an IndexError."""
        response = _make_mock_genai_response(candidates=[])
        assert _map_gemini_stop_reason(response) == "end_turn"

    def test_stop_reason_is_end_turn_when_finish_reason_is_none(self) -> None:
        """A ``None`` ``finish_reason`` maps to "end_turn"."""
        response = _make_mock_genai_response(finish_reason=None)
        assert _map_gemini_stop_reason(response) == "end_turn"

    def test_stop_reason_is_end_turn_for_an_unmapped_reason(self) -> None:
        """An unrecognised reason falls back to the registry's "end_turn" default."""
        response = _make_mock_genai_response(finish_reason="SAFETY")
        assert _map_gemini_stop_reason(response) == "end_turn"

    @pytest.mark.parametrize("usage_metadata", [None, _ABSENT], ids=["null", "absent"])
    def test_extract_usage_is_none_when_the_sdk_omitted_usage(
        self, usage_metadata: object
    ) -> None:
        """A null or missing ``usage_metadata`` yields ``None``, not an empty dict."""
        response = _make_mock_genai_response(usage_metadata=usage_metadata)
        assert _extract_gemini_usage(response) is None

    def test_extract_usage_keeps_only_an_int_prompt_token_count(self) -> None:
        """A non-int ``candidates_token_count`` is skipped, leaving one key."""
        response = _make_mock_genai_response(
            prompt_tokens=12, candidate_tokens="not-an-int"
        )
        result = _extract_gemini_usage(response)
        assert result is not None
        assert result == {"input_tokens": 12}
        assert "output_tokens" not in result

    def test_extract_usage_keeps_only_an_int_candidates_token_count(self) -> None:
        """A non-int ``prompt_token_count`` is skipped, leaving one key."""
        response = _make_mock_genai_response(
            prompt_tokens="not-an-int", candidate_tokens=8
        )
        result = _extract_gemini_usage(response)
        assert result is not None
        assert result == {"output_tokens": 8}
        assert "input_tokens" not in result

    def test_extract_usage_is_none_not_empty_dict_when_no_field_is_an_int(self) -> None:
        """A usage object carrying no int field collapses to ``None``, not ``{}``."""
        response = _make_mock_genai_response(
            prompt_tokens="not-an-int", candidate_tokens=None
        )
        assert _extract_gemini_usage(response) is None


class TestGeminiClientMemoization:
    """The lazily-built SDK client is constructed once and then reused."""

    def test_client_is_constructed_once_and_reused(self, gemini_env: None) -> None:
        """A second ``.client`` access returns the same object, not a new client.

        ``side_effect`` hands out two DISTINCT clients, so ``first is second``
        is a real assertion: a broken memoization would return the second one.
        """
        built = [MagicMock(), MagicMock()]
        with patch("google.genai.Client", side_effect=built) as ctor:
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            first = provider.client
            second = provider.client
        assert first is second
        assert first is built[0]
        assert ctor.call_count == 1
