"""BYOK key plumbing for the generation stage (#761).

"Bring your own key": a stage can point at a **user-supplied** provider key held
in a named environment variable (``LLMConfig.api_key_env``) instead of the
operator's default (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` /
``GOOGLE_API_KEY``). These tests assert:

- the config carries a variable **name**, never a key value — the key stays in
  the environment and is passed straight to the SDK client (never logged or
  persisted);
- the BYOK env var (not the operator default) is what gates availability and is
  read into the client; absent an override, behaviour is unchanged;
- cloud-consent gating still composes with BYOK;
- BYOK does **not** relax the ``ModelRouter`` INTIMATE chokepoint — INTIMATE is
  still redirected to the local ``default`` regardless of the BYOK key.

Key material here is fake test literals; no real key is used.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from creek.classify.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAIProvider,
)
from creek.classify.llm.router import ModelRouter
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier

_CONSENT_ENV = "CREEK_CLOUD_CONSENT"
_BYOK_ANTHROPIC = "CREEK_BYOK_ANTHROPIC_KEY"
_BYOK_OPENAI = "CREEK_BYOK_OPENAI_KEY"
_BYOK_GEMINI = "CREEK_BYOK_GEMINI_KEY"


@pytest.fixture
def _clean_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear operator default keys + BYOK vars; grant consent."""
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        _BYOK_ANTHROPIC,
        _BYOK_OPENAI,
        _BYOK_GEMINI,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(_CONSENT_ENV, "1")


# --------------------------------------------------------------------------- #
# The config carries a variable name, never a key value
# --------------------------------------------------------------------------- #


def test_api_key_env_holds_a_variable_name_not_a_key() -> None:
    """``api_key_env`` is a variable name (BYOK); default is ``None`` (operator)."""
    assert LLMConfig(provider="anthropic").api_key_env is None
    cfg = LLMConfig(provider="anthropic", api_key_env=_BYOK_ANTHROPIC)
    assert cfg.api_key_env == _BYOK_ANTHROPIC


# --------------------------------------------------------------------------- #
# Anthropic BYOK
# --------------------------------------------------------------------------- #


def test_anthropic_byok_reads_key_from_named_env_var(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a BYOK var set (operator default unset), the user's key is used."""
    monkeypatch.setenv(_BYOK_ANTHROPIC, "sk-user-byok")  # fake test literal
    provider = AnthropicProvider(
        LLMConfig(provider="anthropic", api_key_env=_BYOK_ANTHROPIC)
    )
    assert provider.available is True
    with patch("anthropic.Anthropic", return_value=MagicMock()) as ctor:
        _ = provider.client
    assert ctor.call_args.kwargs == {"api_key": "sk-user-byok"}


def test_anthropic_without_byok_is_behaviour_unchanged(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent an override, the SDK reads the default env var itself (no api_key)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operator")  # fake test literal
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    assert provider.available is True
    with patch("anthropic.Anthropic", return_value=MagicMock()) as ctor:
        _ = provider.client
    assert ctor.call_args.kwargs == {}


def test_anthropic_byok_missing_key_refuses_naming_the_byok_var(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BYOK stage is unmet when *its* var is empty — even if the default is set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-operator")  # fake test literal
    # BYOK var deliberately left unset by the fixture.
    with pytest.raises(RuntimeError, match=_BYOK_ANTHROPIC):
        AnthropicProvider(LLMConfig(provider="anthropic", api_key_env=_BYOK_ANTHROPIC))


def test_anthropic_byok_still_requires_consent(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloud-consent gating composes with BYOK — a key alone is not enough."""
    monkeypatch.setenv(_BYOK_ANTHROPIC, "sk-user-byok")  # fake test literal
    monkeypatch.setenv(_CONSENT_ENV, "no")
    with pytest.raises(RuntimeError, match="consent"):
        AnthropicProvider(LLMConfig(provider="anthropic", api_key_env=_BYOK_ANTHROPIC))


# --------------------------------------------------------------------------- #
# OpenAI BYOK (composes with api_base)
# --------------------------------------------------------------------------- #


def test_openai_byok_reads_key_and_composes_with_api_base(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BYOK key is passed to the SDK, alongside an ``api_base`` gateway override."""
    monkeypatch.setenv(_BYOK_OPENAI, "sk-openai-user")  # fake test literal
    provider = OpenAIProvider(
        LLMConfig(
            provider="openai",
            api_key_env=_BYOK_OPENAI,
            api_base="https://gateway.example/v1",
        )
    )
    assert provider.available is True
    with patch("openai.OpenAI", return_value=MagicMock()) as ctor:
        _ = provider.client
    assert ctor.call_args.kwargs == {
        "base_url": "https://gateway.example/v1",
        "api_key": "sk-openai-user",
    }


def test_openai_byok_without_api_base_passes_only_the_key(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common BYOK case (no gateway override) passes just the user's key."""
    monkeypatch.setenv(_BYOK_OPENAI, "sk-openai-user")  # fake test literal
    provider = OpenAIProvider(LLMConfig(provider="openai", api_key_env=_BYOK_OPENAI))
    with patch("openai.OpenAI", return_value=MagicMock()) as ctor:
        _ = provider.client
    assert ctor.call_args.kwargs == {"api_key": "sk-openai-user"}


def test_openai_without_byok_is_behaviour_unchanged(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent an override, the SDK reads ``OPENAI_API_KEY`` itself (no api_key)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator")  # fake test literal
    provider = OpenAIProvider(LLMConfig(provider="openai"))
    with patch("openai.OpenAI", return_value=MagicMock()) as ctor:
        _ = provider.client
    assert ctor.call_args.kwargs == {}


# --------------------------------------------------------------------------- #
# Gemini BYOK (single var overrides the two-var default)
# --------------------------------------------------------------------------- #


def test_gemini_byok_reads_key_from_named_env_var(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BYOK var overrides the GOOGLE_API_KEY / GEMINI_API_KEY default pair."""
    monkeypatch.setenv(_BYOK_GEMINI, "g-user-byok")  # fake test literal
    provider = GeminiProvider(LLMConfig(provider="gemini", api_key_env=_BYOK_GEMINI))
    assert provider.available is True
    with patch("google.genai.Client", return_value=MagicMock()) as ctor:
        _ = provider.client
    assert ctor.call_args.kwargs == {"api_key": "g-user-byok"}


def test_gemini_byok_missing_key_refuses_naming_the_byok_var(
    _clean_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BYOK Gemini stage is unmet when its var is empty (default pair ignored)."""
    monkeypatch.setenv("GOOGLE_API_KEY", "g-operator")  # fake test literal
    with pytest.raises(RuntimeError, match=_BYOK_GEMINI):
        GeminiProvider(LLMConfig(provider="gemini", api_key_env=_BYOK_GEMINI))


# --------------------------------------------------------------------------- #
# BYOK does not relax the INTIMATE chokepoint
# --------------------------------------------------------------------------- #


def test_byok_does_not_bypass_intimate_chokepoint() -> None:
    """A BYOK cloud generation stage is still redirected off INTIMATE."""
    router = ModelRouter(
        LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama"},
                "generation": {
                    "provider": "anthropic",
                    "api_key_env": _BYOK_ANTHROPIC,
                },
            }
        )
    )
    # OPEN/PERSONAL keep the BYOK cloud stage (with its key env preserved)...
    open_cfg = router.resolve("generation")
    assert open_cfg.provider == "anthropic"
    assert open_cfg.api_key_env == _BYOK_ANTHROPIC
    # ...but INTIMATE is still redirected to the local default — BYOK or not.
    assert router.resolve("generation", PrivacyTier.INTIMATE).provider == "ollama"


def test_byok_through_writing_desk_role() -> None:
    """BYOK also flows through a ``writing_desk`` role via ``resolve_role``."""
    router = ModelRouter(
        LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama"},
                "writing_desk": {
                    "voice_drafter": {
                        "provider": "anthropic",
                        "api_key_env": _BYOK_ANTHROPIC,
                    }
                },
            }
        )
    )
    # Non-intimate keeps the role's BYOK cloud config...
    role_cfg = router.resolve_role("voice_drafter")
    assert role_cfg.provider == "anthropic"
    assert role_cfg.api_key_env == _BYOK_ANTHROPIC
    # ...INTIMATE is still redirected to the local default.
    assert (
        router.resolve_role("voice_drafter", PrivacyTier.INTIMATE).provider == "ollama"
    )


# --------------------------------------------------------------------------- #
# A pasted key value is rejected fail-fast (api_key_env holds a NAME)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "pasted",
    [
        "sk-ant-api03-notarealkey",
        "sk-openai-notarealkey",
        "AIzaSyNotARealGeminiKey",
        "has spaces not a name",
    ],
)
def test_api_key_env_rejects_a_pasted_key_value(pasted: str) -> None:
    """A value that looks like a key (not a var name) fails at config-load."""
    with pytest.raises(ValidationError, match="environment variable NAME"):
        LLMConfig(provider="anthropic", api_key_env=pasted)
