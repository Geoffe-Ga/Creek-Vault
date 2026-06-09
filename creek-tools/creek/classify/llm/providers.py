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

from creek.classify.llm.completion import AnthropicCompletion, Completion
from creek.classify.llm.consent import (
    cloud_warning,
    consent_error_message,
    has_cloud_consent,
)

if TYPE_CHECKING:
    import anthropic
    import openai

    from creek.classify.llm.base import LLMProvider
    from creek.config import LLMConfig

logger = logging.getLogger(__name__)

# ``AnthropicCompletion`` is the deprecated alias for :class:`Completion`,
# imported here so the historical path
# ``from creek.classify.llm.providers import AnthropicCompletion`` keeps
# resolving after the type moved to :mod:`creek.classify.llm.completion` (#604).
__all__ = [
    "ANTHROPIC_CLOUD_WARNING",
    "AnthropicCompletion",
    "AnthropicProvider",
    "Completion",
    "OllamaProvider",
    "OpenAIProvider",
    "build_provider",
    "cloud_warning",
    "provider_display_name",
    "provider_is_cloud",
]


ANTHROPIC_CLOUD_WARNING: str = cloud_warning("Anthropic")
"""Deprecated: the cloud-egress warning bound to Anthropic.

Retained for back-compat; new code should call
:func:`creek.classify.llm.consent.cloud_warning` with the active provider name.
"""


class AnthropicProvider:
    """Anthropic API provider for cloud-based fragment classification.

    Sends classification prompts to the Anthropic ``messages`` API using
    the official Python SDK.  The API key is read exclusively from the
    ``ANTHROPIC_API_KEY`` environment variable and is never stored in
    the configuration file, logs, or error messages.

    Attributes:
        config: The LLM configuration specifying model, batch size, etc.
    """

    is_cloud: bool = True
    """Anthropic sends fragment content off-device; gated by consent."""

    PROVIDER_NAME: str = "Anthropic"
    """Display name woven into consent and cloud-egress messages."""

    DEFAULT_MODEL: str = "claude-sonnet-4-6"
    """Default Anthropic model when the config does not override it."""

    API_KEY_ENV: str = "ANTHROPIC_API_KEY"
    """Environment variable name for the Anthropic API key."""

    CONSENT_ENV: str = "CREEK_ANTHROPIC_CONSENT"
    """Deprecated: the legacy consent variable, still honored as an alias.

    Retained so existing setups and tests that reference
    ``AnthropicProvider.CONSENT_ENV`` keep working; the consent check itself now
    lives in :mod:`creek.classify.llm.consent` and accepts ``CREEK_CLOUD_CONSENT``.
    """

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
        unmet = self._missing_prerequisite()
        if unmet is not None:
            raise RuntimeError(unmet)
        self._client: anthropic.Anthropic | None = None

    @classmethod
    def _missing_prerequisite(cls) -> str | None:
        """Return why the Anthropic prerequisites are unmet, or ``None``.

        The API key and the explicit cloud-egress consent are both read from
        the environment at call time, so the check reflects the *current* env
        rather than the env at construction. The consent half delegates to the
        provider-neutral :mod:`creek.classify.llm.consent` gate (#606), which
        accepts ``CREEK_CLOUD_CONSENT`` or the legacy ``CREEK_ANTHROPIC_CONSENT``.
        :meth:`__init__` raises the returned message; :attr:`available` reports
        whether it is ``None``.

        Returns:
            A human-readable explanation when the key is missing or consent is
            not affirmative; ``None`` when both prerequisites are satisfied.
        """
        if not os.environ.get(cls.API_KEY_ENV, "").strip():
            return (
                f"{cls.API_KEY_ENV} environment variable is not set; "
                "required for the Anthropic provider."
            )
        if not has_cloud_consent():
            return consent_error_message(cls.PROVIDER_NAME)
        return None

    @property
    def available(self) -> bool:
        """Whether the Anthropic prerequisites (API key + consent) are met.

        Delegates to :meth:`_missing_prerequisite`, the same check
        :meth:`__init__` enforces, so the :class:`~creek.classify.llm.base.LLMProvider`
        protocol's ``available`` contract reflects the live environment.

        Returns:
            ``True`` when the API key is set and consent is affirmative.
        """
        return self._missing_prerequisite() is None

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

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Provider-neutral entry point: send *prompt*, return a ``Completion``.

        Satisfies the :class:`~creek.classify.llm.base.LLMProvider` protocol by
        delegating verbatim to :meth:`call_with_metadata`, the historical
        Anthropic-named method existing callers still use. No behaviour change;
        the two names are interchangeable while callers migrate (#604).

        Args:
            prompt: The dynamic, fully-formatted user prompt.
            max_tokens: Maximum tokens to request, or ``None`` for the
                historical :attr:`MAX_TOKENS` ceiling.
            system: Optional static system prefix to cache, or ``None``.

        Returns:
            The :class:`Completion` produced by :meth:`call_with_metadata`.

        Raises:
            RuntimeError: Propagated from :meth:`call_with_metadata` when the
                SDK raises; the original exception type name is preserved.
        """
        return self.call_with_metadata(prompt, max_tokens=max_tokens, system=system)

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


class OllamaProvider:
    """Local Ollama provider implementing the :class:`LLMProvider` protocol.

    Wraps the module-level :func:`call_ollama` / :func:`check_ollama_available`
    HTTP helpers so the factory returns one uniform provider type. Ollama runs
    on-device, so :attr:`is_cloud` is ``False`` and no consent is required;
    construction performs no I/O (the health check runs on first
    :attr:`available` access).

    Attributes:
        config: The LLM configuration carrying the Ollama URL and model name.
    """

    is_cloud: bool = False
    """Ollama runs locally; content never leaves the device."""

    PROVIDER_NAME: str = "Ollama"
    """Display name (local; never appears in a cloud-egress message)."""

    REQUEST_TIMEOUT: float = 30.0
    """HTTP request timeout for completion calls, in seconds."""

    AVAILABILITY_TIMEOUT: float = 5.0
    """Timeout for the Ollama health check, in seconds."""

    def __init__(self, config: LLMConfig) -> None:
        """Store the configuration; performs no network I/O.

        Args:
            config: LLM provider configuration (Ollama URL + model name).
        """
        self.config = config

    @property
    def model(self) -> str:
        """The configured Ollama model identifier.

        Returns:
            ``config.model`` verbatim — Ollama has no cloud-model fallback.
        """
        return self.config.model

    @property
    def available(self) -> bool:
        """Whether the local Ollama endpoint is reachable.

        Returns:
            ``True`` when the ``/api/tags`` health check returns ``200``.
        """
        return check_ollama_available(
            self.config,
            timeout=self.AVAILABILITY_TIMEOUT,
        )

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Send *prompt* to Ollama and return a normalized :class:`Completion`.

        Ollama's generate API exposes neither a ``max_tokens`` ceiling nor a
        cached ``system`` prefix nor token usage, so those kwargs are accepted
        for protocol compatibility but ignored, and the result carries the
        default ``"end_turn"`` stop reason with ``usage=None``.

        Args:
            prompt: The fully-formatted prompt.
            max_tokens: Ignored (Ollama has no output ceiling knob here).
            system: Ignored (Ollama has no cached system block).

        Returns:
            A :class:`Completion` wrapping the raw Ollama response text.

        Raises:
            httpx.HTTPStatusError: On HTTP error responses.
            httpx.HTTPError: On connection or transport errors.
        """
        text = call_ollama(
            self.config,
            prompt,
            timeout=self.REQUEST_TIMEOUT,
        )
        return Completion(text=text)


#: OpenAI ``finish_reason`` → normalized :class:`Completion` stop reason.
_OPENAI_STOP_REASONS: dict[str, str] = {"stop": "end_turn", "length": "max_tokens"}


class OpenAIProvider:
    """OpenAI API provider for cloud-based fragment classification (#607).

    Sends prompts to the OpenAI ``chat.completions`` API via the official SDK.
    The API key is read exclusively from ``OPENAI_API_KEY`` by the SDK and is
    never stored in config, logs, or error messages; an optional
    OpenAI-compatible gateway base URL may be supplied via ``config.api_base``
    (or the SDK's own ``OPENAI_BASE_URL``). Structure mirrors
    :class:`AnthropicProvider` so the two stay legible.

    Attributes:
        config: The LLM configuration specifying model, batch size, etc.
    """

    is_cloud: bool = True
    """OpenAI sends fragment content off-device; gated by consent."""

    PROVIDER_NAME: str = "OpenAI"
    """Display name woven into consent and cloud-egress messages."""

    DEFAULT_MODEL: str = "gpt-4o"
    """Default OpenAI model when the config does not override it."""

    API_KEY_ENV: str = "OPENAI_API_KEY"
    """Environment variable name for the OpenAI API key."""

    MAX_TOKENS: int = 1024
    """Maximum tokens requested from the OpenAI API per call."""

    _OLLAMA_DEFAULT_MODEL: str = "mistral"
    """The ``LLMConfig`` default model name (indicates no OpenAI override)."""

    def __init__(self, config: LLMConfig) -> None:
        """Validate environment prerequisites and prepare the client.

        The SDK client is instantiated lazily on first use. The cloud-egress
        consent gate is shared with every cloud provider (#606).

        Args:
            config: LLM provider configuration.

        Raises:
            RuntimeError: If the API key is unset or cloud consent is missing.
        """
        self.config = config
        unmet = self._missing_prerequisite()
        if unmet is not None:
            raise RuntimeError(unmet)
        self._client: openai.OpenAI | None = None

    @classmethod
    def _missing_prerequisite(cls) -> str | None:
        """Return why the OpenAI prerequisites are unmet, or ``None``.

        The key and the shared cloud-egress consent are read from the
        environment at call time; :meth:`__init__` raises the returned message
        and :attr:`available` reports whether it is ``None``.

        Returns:
            A human-readable explanation when the key is missing or consent is
            not affirmative; ``None`` when both prerequisites are satisfied.
        """
        if not os.environ.get(cls.API_KEY_ENV, "").strip():
            return (
                f"{cls.API_KEY_ENV} environment variable is not set; "
                "required for the OpenAI provider."
            )
        if not has_cloud_consent():
            return consent_error_message(cls.PROVIDER_NAME)
        return None

    @property
    def available(self) -> bool:
        """Whether the OpenAI prerequisites (API key + consent) are met.

        Returns:
            ``True`` when the API key is set and cloud consent is affirmative.
        """
        return self._missing_prerequisite() is None

    @property
    def model(self) -> str:
        """The OpenAI model identifier to use for API calls.

        Falls back to :attr:`DEFAULT_MODEL` when ``config.model`` still holds
        the Ollama default (``"mistral"``) or is empty.

        Returns:
            The resolved model identifier.
        """
        model = self.config.model.strip()
        if not model or model == self._OLLAMA_DEFAULT_MODEL:
            return self.DEFAULT_MODEL
        return model

    @property
    def client(self) -> openai.OpenAI:
        """The lazily-initialised OpenAI SDK client.

        The SDK reads ``OPENAI_API_KEY`` itself; ``config.api_base`` (when set)
        is forwarded as ``base_url`` for an OpenAI-compatible gateway.

        Returns:
            An ``openai.OpenAI`` client instance.
        """
        if self._client is None:
            import openai

            if self.config.api_base:
                self._client = openai.OpenAI(base_url=self.config.api_base)
            else:
                self._client = openai.OpenAI()
        return self._client

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Send *prompt* to OpenAI and return a normalized :class:`Completion`.

        Args:
            prompt: The dynamic, fully-formatted user prompt.
            max_tokens: Maximum tokens to request. ``None`` keeps the historical
                :attr:`MAX_TOKENS` ceiling.
            system: Optional static system prefix sent as a leading system
                message. ``None`` sends the user prompt alone.

        Returns:
            A :class:`Completion` carrying the response text, the mapped stop
            reason, and token usage when present.

        Raises:
            RuntimeError: If the SDK raises any ``OpenAIError``; re-raised with
                only the exception type name so no request state leaks.
        """
        import openai

        ceiling = self.MAX_TOKENS if max_tokens is None else max_tokens
        try:
            response = self._create_chat(prompt, ceiling, system)
        except openai.OpenAIError as exc:
            msg = f"OpenAI API call failed: {type(exc).__name__}"
            raise RuntimeError(msg) from None
        return Completion(
            text=_extract_openai_text(response),
            stop_reason=_map_openai_stop_reason(response),
            usage=_extract_openai_usage(response),
        )

    def _create_chat(
        self,
        prompt: str,
        ceiling: int,
        system: str | None,
    ) -> object:
        """Call ``chat.completions.create``, adding a system message when given.

        Keeping the two message shapes as inline literals (rather than a built
        list) preserves the SDK's typed-param inference.

        Args:
            prompt: The dynamic user prompt.
            ceiling: The resolved ``max_tokens`` value.
            system: Optional static system prefix, or ``None``.

        Returns:
            The raw ``chat.completions.create`` response object.
        """
        if system is None:
            return self.client.chat.completions.create(
                model=self.model,
                max_tokens=ceiling,
                messages=[{"role": "user", "content": prompt}],
            )
        return self.client.chat.completions.create(
            model=self.model,
            max_tokens=ceiling,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )


def _extract_openai_text(response: object) -> str:
    """Concatenate the textual content of an OpenAI chat response.

    Args:
        response: A ``chat.completions.create`` response object.

    Returns:
        The first choice's message content, or ``""`` when absent/non-string.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", None)
    return content if isinstance(content, str) else ""


def _map_openai_stop_reason(response: object) -> str:
    """Map an OpenAI ``finish_reason`` to a normalized stop reason.

    Args:
        response: A ``chat.completions.create`` response object.

    Returns:
        ``"end_turn"`` for ``"stop"`` (and unknown/absent reasons),
        ``"max_tokens"`` for ``"length"``.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "end_turn"
    reason = getattr(choices[0], "finish_reason", None)
    if not isinstance(reason, str):
        return "end_turn"
    return _OPENAI_STOP_REASONS.get(reason, "end_turn")


def _extract_openai_usage(response: object) -> dict[str, int] | None:
    """Extract integer token-usage counts from an OpenAI chat response.

    Args:
        response: A ``chat.completions.create`` response object.

    Returns:
        A dict with ``input_tokens`` / ``output_tokens`` mirroring the
        Anthropic usage shape, or ``None`` when the SDK omitted usage.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    counts: dict[str, int] = {}
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if isinstance(prompt_tokens, int):
        counts["input_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int):
        counts["output_tokens"] = completion_tokens
    return counts or None


#: Registry mapping the ``LLMConfig.provider`` string to its provider class.
#: Adding a backend (Gemini #608) is a one-line entry here. The value is the
#: concrete class (not the ``LLMProvider`` protocol) so it is both
#: constructible and exposes the class-level ``is_cloud`` flag; widen the union
#: as new providers land.
_PROVIDER_REGISTRY: dict[
    str, type[AnthropicProvider | OllamaProvider | OpenAIProvider]
] = {
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
}


def build_provider(config: LLMConfig) -> LLMProvider:
    """Construct the :class:`LLMProvider` selected by ``config.provider``.

    The single dispatch point replacing the scattered ``== "anthropic"``
    branching (#605). Cloud providers validate their environment in their
    constructor and may raise; callers that want graceful degradation should
    catch :class:`RuntimeError` (the classifier does, to report unavailability).

    Args:
        config: LLM provider configuration; ``config.provider`` selects the
            backend.

    Returns:
        A provider instance satisfying the :class:`LLMProvider` protocol.

    Raises:
        ValueError: If ``config.provider`` names no registered backend.
    """
    try:
        provider_cls = _PROVIDER_REGISTRY[config.provider]
    except KeyError:
        known = ", ".join(sorted(_PROVIDER_REGISTRY))
        msg = f"unknown LLM provider {config.provider!r}; expected one of: {known}"
        raise ValueError(msg) from None
    return provider_cls(config)


def provider_is_cloud(provider: str) -> bool:
    """Report whether *provider* is a cloud backend without instantiating it.

    Read from the class-level :attr:`LLMProvider.is_cloud` flag so the
    classifier can emit the cloud-egress warning at construction time, before
    any API key is guaranteed present (instantiating a cloud provider then
    would raise).

    Args:
        provider: The ``LLMConfig.provider`` string.

    Returns:
        ``True`` when the named provider is registered and cloud-backed;
        ``False`` for local or unknown providers.
    """
    provider_cls = _PROVIDER_REGISTRY.get(provider)
    return bool(provider_cls is not None and provider_cls.is_cloud)


def provider_display_name(provider: str) -> str:
    """Return the human-readable display name for a provider string.

    Used to name the active provider in cloud-egress warnings and consent
    errors (e.g. ``"anthropic"`` → ``"Anthropic"``).

    Args:
        provider: The ``LLMConfig.provider`` string.

    Returns:
        The registered provider's ``PROVIDER_NAME``, or *provider* verbatim
        when it names no registered backend.
    """
    provider_cls = _PROVIDER_REGISTRY.get(provider)
    return provider_cls.PROVIDER_NAME if provider_cls is not None else provider
