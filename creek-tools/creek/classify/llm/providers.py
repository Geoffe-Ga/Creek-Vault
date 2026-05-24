"""LLM client adapters for the Creek classification pipeline.

Wraps the two supported provider backends:

- :class:`AnthropicProvider` — the official Anthropic SDK, opt-in via
  ``ANTHROPIC_API_KEY`` and an explicit ``CREEK_ANTHROPIC_CONSENT``
  acknowledgement that fragment content is leaving the device.
- :func:`call_ollama` / :func:`check_ollama_available` — the local
  HTTP API exposed by Ollama, used as the default.

Both providers raise plain :class:`RuntimeError` on failure so the
orchestrator's retry loop can react without provider-specific
exception handling.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import anthropic

    from creek.config import LLMConfig

logger = logging.getLogger(__name__)


ANTHROPIC_CLOUD_WARNING: str = (
    "WARNING: Cloud classification enabled. "
    "Fragment content will be sent to Anthropic's servers."
)
"""Warning displayed when the Anthropic cloud provider is selected."""


class AnthropicProvider:
    """Anthropic API provider for cloud-based fragment classification.

    Sends classification prompts to the Anthropic ``messages`` API using
    the official Python SDK.  The API key is read exclusively from the
    ``ANTHROPIC_API_KEY`` environment variable and is never stored in
    the configuration file, logs, or error messages.

    Attributes:
        config: The LLM configuration specifying model, batch size, etc.
    """

    DEFAULT_MODEL: str = "claude-sonnet-4-5-20250929"
    """Default Anthropic model when the config does not override it."""

    API_KEY_ENV: str = "ANTHROPIC_API_KEY"
    """Environment variable name for the Anthropic API key."""

    CONSENT_ENV: str = "CREEK_ANTHROPIC_CONSENT"
    """Environment variable name for cloud-classification consent."""

    CONSENT_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes"})
    """String values accepted as affirmative consent."""

    MAX_TOKENS: int = 1024
    """Maximum tokens requested from the Anthropic API per call."""

    _OLLAMA_DEFAULT_MODEL: str = "mistral"
    """The ``LLMConfig`` default model name (indicates no Anthropic override)."""

    def __init__(self, config: LLMConfig) -> None:
        """Validate environment prerequisites and prepare the client.

        The SDK client itself is instantiated lazily on first use to
        avoid unnecessary network/imports during construction.

        Args:
            config: LLM provider configuration.

        Raises:
            RuntimeError: If the API key or consent environment
                variables are not set.
        """
        self.config = config
        if not os.environ.get(self.API_KEY_ENV, "").strip():
            msg = (
                f"{self.API_KEY_ENV} environment variable is not set; "
                "required for the Anthropic provider."
            )
            raise RuntimeError(msg)
        consent = os.environ.get(self.CONSENT_ENV, "").strip().lower()
        if consent not in self.CONSENT_TRUTHY:
            msg = (
                "Anthropic cloud classification requires explicit consent. "
                f"Set {self.CONSENT_ENV}=1 to confirm that fragment content "
                "may be sent to Anthropic's servers."
            )
            raise RuntimeError(msg)
        self._client: anthropic.Anthropic | None = None

    @property
    def model(self) -> str:
        """The Anthropic model identifier to use for API calls.

        Falls back to :attr:`DEFAULT_MODEL` when ``config.model`` still
        holds the Ollama default (``"mistral"``) or is empty.

        Returns:
            The resolved model identifier.
        """
        model = self.config.model.strip()
        if not model or model == self._OLLAMA_DEFAULT_MODEL:
            return self.DEFAULT_MODEL
        return model

    @property
    def client(self) -> anthropic.Anthropic:
        """The lazily-initialised Anthropic SDK client.

        Returns:
            An ``anthropic.Anthropic`` client instance.
        """
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def call(self, prompt: str) -> str:
        """Send a classification prompt to the Anthropic API.

        Args:
            prompt: The fully-formatted classification prompt.

        Returns:
            The concatenated text content of the API response.

        Raises:
            RuntimeError: If the SDK raises any ``AnthropicError``; the
                original exception is re-raised as ``RuntimeError`` with
                its type name to avoid leaking sensitive request state.
        """
        import anthropic

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            msg = f"Anthropic API call failed: {type(exc).__name__}"
            raise RuntimeError(msg) from None
        return _extract_anthropic_text(response)


def _extract_anthropic_text(response: object) -> str:
    """Concatenate the textual blocks from an Anthropic API response.

    Args:
        response: A ``messages.create`` response object.

    Returns:
        The joined ``text`` attributes of each content block.
    """
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def call_ollama(config: LLMConfig, prompt: str, *, timeout: float) -> str:
    """Send a prompt to a local Ollama instance and return the response text.

    Args:
        config: LLM provider configuration carrying the Ollama URL and
            model name.
        prompt: The fully-formatted classification prompt.
        timeout: HTTP request timeout in seconds.

    Returns:
        The raw response text from the LLM.

    Raises:
        httpx.HTTPStatusError: On HTTP error responses.
        httpx.HTTPError: On connection or transport errors.
    """
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{config.ollama_url}/api/generate",
            json={
                "model": config.model,
                "prompt": prompt,
                "stream": False,
            },
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", ""))


def check_ollama_available(config: LLMConfig, *, timeout: float) -> bool:
    """Health-check the Ollama HTTP endpoint at ``/api/tags``.

    Args:
        config: LLM provider configuration with the Ollama URL.
        timeout: HTTP request timeout in seconds.

    Returns:
        ``True`` when Ollama replies ``200``; ``False`` on any HTTP
        error or non-200 status.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{config.ollama_url}/api/tags")
            if resp.status_code == 200:
                return True
    except httpx.HTTPError:
        pass
    logger.warning("Ollama not available at %s", config.ollama_url)
    return False
