"""Tests for the provider-neutral cloud-egress consent gate (#606).

Locks the generalisation of the Anthropic-specific consent gate into a shared
helper that covers any cloud provider (OpenAI/Gemini next), keyed off the new
``CREEK_CLOUD_CONSENT`` env var with the legacy ``CREEK_ANTHROPIC_CONSENT``
accepted as an alias. Ollama (local) never requires consent.
"""

from __future__ import annotations

import pytest

from creek.classify.llm.consent import (
    CLOUD_CONSENT_ENV,
    LEGACY_CONSENT_ENV,
    cloud_warning,
    consent_error_message,
    has_cloud_consent,
    require_cloud_consent,
)
from creek.classify.llm.providers import (
    ANTHROPIC_CLOUD_WARNING,
    AnthropicProvider,
    OllamaProvider,
)
from creek.config import LLMConfig


@pytest.fixture(autouse=True)
def _clear_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with neither consent variable set (hermetic)."""
    monkeypatch.delenv(CLOUD_CONSENT_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CONSENT_ENV, raising=False)


class TestHasCloudConsent:
    """Tests for has_cloud_consent across both env vars."""

    def test_false_when_neither_set(self) -> None:
        """No consent variable set means no consent."""
        assert has_cloud_consent() is False

    def test_true_for_new_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The new ``CREEK_CLOUD_CONSENT`` grants consent."""
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        assert has_cloud_consent() is True

    def test_true_for_legacy_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The legacy ``CREEK_ANTHROPIC_CONSENT`` still grants consent."""
        monkeypatch.setenv(LEGACY_CONSENT_ENV, "1")
        assert has_cloud_consent() is True

    @pytest.mark.parametrize("value", ["true", "TRUE", "Yes", "yes"])
    def test_accepts_truthy_words_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """``true``/``yes`` (any case) are affirmative."""
        monkeypatch.setenv(CLOUD_CONSENT_ENV, value)
        assert has_cloud_consent() is True

    @pytest.mark.parametrize("value", ["", "0", "no", "false", "  "])
    def test_rejects_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Anything outside the truthy set is not consent."""
        monkeypatch.setenv(CLOUD_CONSENT_ENV, value)
        assert has_cloud_consent() is False


class TestRequireCloudConsent:
    """Tests for the raising consent gate."""

    def test_raises_naming_provider_when_absent(self) -> None:
        """The error names the active provider and the new env var."""
        with pytest.raises(RuntimeError) as exc:
            require_cloud_consent("OpenAI")
        message = str(exc.value)
        assert "OpenAI" in message
        assert CLOUD_CONSENT_ENV in message

    def test_silent_when_consent_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No exception once consent is on file."""
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        require_cloud_consent("OpenAI")  # must not raise

    def test_silent_with_legacy_consent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The legacy variable also satisfies the gate."""
        monkeypatch.setenv(LEGACY_CONSENT_ENV, "yes")
        require_cloud_consent("Gemini")  # must not raise


def test_consent_error_message_mentions_both_vars() -> None:
    """The remediation surfaces the new var and honors the legacy one."""
    message = consent_error_message("Anthropic")
    assert "Anthropic" in message
    assert CLOUD_CONSENT_ENV in message
    assert LEGACY_CONSENT_ENV in message


def test_cloud_warning_names_provider() -> None:
    """The neutral warning names the provider and flags cloud egress."""
    warning = cloud_warning("Gemini")
    assert "Gemini" in warning
    assert "cloud" in warning.lower()


def test_anthropic_cloud_warning_is_neutral_warning_for_anthropic() -> None:
    """The deprecated constant equals the neutral warning bound to Anthropic."""
    assert cloud_warning("Anthropic") == ANTHROPIC_CLOUD_WARNING


class TestAnthropicProviderConsent:
    """Anthropic provider routes through the shared consent gate."""

    def test_constructs_with_new_consent_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CREEK_CLOUD_CONSENT`` satisfies the Anthropic consent gate."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        provider = AnthropicProvider(LLMConfig(provider="anthropic"))
        assert provider.available is True

    def test_constructs_with_legacy_consent_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy ``CREEK_ANTHROPIC_CONSENT`` still works unchanged."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        monkeypatch.setenv(LEGACY_CONSENT_ENV, "1")
        provider = AnthropicProvider(LLMConfig(provider="anthropic"))
        assert provider.available is True

    def test_raises_naming_anthropic_when_consent_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a key but no consent, construction raises naming Anthropic."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        with pytest.raises(RuntimeError, match="Anthropic"):
            AnthropicProvider(LLMConfig(provider="anthropic"))

    def test_consent_message_references_new_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Anthropic consent failure points at the neutral var."""
        monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
        with pytest.raises(RuntimeError, match=CLOUD_CONSENT_ENV):
            AnthropicProvider(LLMConfig(provider="anthropic"))


def test_ollama_never_requires_consent() -> None:
    """The local Ollama provider constructs and is usable with no consent."""
    provider = OllamaProvider(LLMConfig())
    # No consent env set (autouse fixture cleared both); must not raise and
    # is_cloud stays False so no gate ever applies.
    assert provider.is_cloud is False
