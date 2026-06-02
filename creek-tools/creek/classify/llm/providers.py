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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import anthropic

    from creek.config import LLMConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnthropicCompletion:
    """An Anthropic completion paired with its stop reason.

    The classification path discards the stop reason; the draft path
    needs it so a ``max_tokens`` ceiling can be surfaced instead of
    truncating the essay mid-sentence without warning.

    Attributes:
        text: The concatenated textual content of the response.
        stop_reason: The reason the model stopped generating. ``"end_turn"``
            on a clean completion; ``"max_tokens"`` when the ceiling was hit.
        usage: Token-usage counts from the SDK response, when available
            (``input_tokens``, ``output_tokens`` and, when prompt caching is
            active, ``cache_creation_input_tokens`` / ``cache_read_input_tokens``).
            ``None`` when the SDK omitted usage. The author desk surfaces this on
            :class:`~creek.author.models.AuthoredDraft` so a run's cost — and
            cache-hit rate — is observable (#474).
    """

    text: str
    stop_reason: str = "end_turn"
    usage: dict[str, int] | None = None


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

    DEFAULT_MODEL: str = "claude-sonnet-4-6"
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
        return self.call_with_metadata(prompt).text

    def call_with_metadata(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> AnthropicCompletion:
        """Send a prompt and return its text, stop reason, and token usage.

        The draft pipeline needs the ``stop_reason`` so it can detect a
        ``max_tokens`` truncation; the classification path discards it by
        calling :meth:`call` instead.

        When *system* is supplied it is sent as a cached ``system`` content
        block (prompt caching, #474): the static prefix is billed once and
        re-read cheaply on subsequent calls while *prompt* stays the dynamic
        user content. When *system* is ``None`` the call is byte-for-byte the
        historical single-user-string shape, so every existing caller is
        unchanged.

        Args:
            prompt: The dynamic, fully-formatted user prompt.
            max_tokens: Maximum tokens to request. ``None`` (the default)
                keeps the historical :attr:`MAX_TOKENS` ceiling so the
                classification path is unchanged.
            system: Optional static system prefix to send as an ephemeral
                cached block. ``None`` (the default) preserves the legacy
                behaviour.

        Returns:
            An :class:`AnthropicCompletion` carrying the concatenated text, the
            response ``stop_reason`` (``"end_turn"`` when the SDK omits one),
            and the SDK's token :attr:`~AnthropicCompletion.usage` when present.

        Raises:
            RuntimeError: If the SDK raises any ``AnthropicError``; the
                original exception is re-raised as ``RuntimeError`` with
                its type name to avoid leaking sensitive request state.
        """
        import anthropic

        ceiling = self.MAX_TOKENS if max_tokens is None else max_tokens
        try:
            response = self._create_message(prompt, ceiling, system)
        except anthropic.AnthropicError as exc:
            msg = f"Anthropic API call failed: {type(exc).__name__}"
            raise RuntimeError(msg) from None
        stop_reason = getattr(response, "stop_reason", None) or "end_turn"
        return AnthropicCompletion(
            text=_extract_anthropic_text(response),
            stop_reason=str(stop_reason),
            usage=_extract_anthropic_usage(response),
        )

    def _create_message(
        self,
        prompt: str,
        ceiling: int,
        system: str | None,
    ) -> object:
        """Call ``messages.create``, adding a cached system block when given.

        The two branches keep the SDK call typed: when *system* is ``None`` the
        request is the historical single-user shape (no ``system`` argument);
        otherwise the static prefix is sent as a single ephemeral
        cache-controlled text block so it is billed once and re-read cheaply
        (#474). The desk only ever uses ephemeral caching, so the directive is
        a typed literal here rather than a caller-supplied dict.

        Args:
            prompt: The dynamic user prompt.
            ceiling: The resolved ``max_tokens`` value.
            system: Optional static system prefix to cache, or ``None``.

        Returns:
            The raw ``messages.create`` response object.
        """
        if system is None:
            return self.client.messages.create(
                model=self.model,
                max_tokens=ceiling,
                messages=[{"role": "user", "content": prompt}],
            )
        cache: anthropic.types.CacheControlEphemeralParam = {"type": "ephemeral"}
        return self.client.messages.create(
            model=self.model,
            max_tokens=ceiling,
            messages=[{"role": "user", "content": prompt}],
            system=[{"type": "text", "text": system, "cache_control": cache}],
        )


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


#: SDK ``response.usage`` fields the author desk surfaces for cost tracking.
#: ``cache_*`` appear only when prompt caching is active; absent fields are
#: simply omitted rather than zero-filled.
_USAGE_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _extract_anthropic_usage(response: object) -> dict[str, int] | None:
    """Extract integer token-usage counts from an Anthropic response.

    Args:
        response: A ``messages.create`` response object.

    Returns:
        A plain dict of the present :data:`_USAGE_FIELDS`, or ``None`` when the
        SDK omitted usage entirely. Non-integer or missing fields are skipped
        so a partial usage object never raises.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    counts: dict[str, int] = {}
    for field in _USAGE_FIELDS:
        value = getattr(usage, field, None)
        if isinstance(value, int):
            counts[field] = value
    return counts or None


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
