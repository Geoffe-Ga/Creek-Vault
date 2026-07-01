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

import hmac
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
    from google import genai

    from creek.classify.llm.base import LLMProvider
    from creek.config import LLMConfig

logger = logging.getLogger(__name__)

# ``AnthropicCompletion`` is the deprecated alias for :class:`Completion`,
# imported here so the historical path
# ``from creek.classify.llm.providers import AnthropicCompletion`` keeps
# resolving after the type moved to :mod:`creek.classify.llm.completion` (#604).
__all__ = [
    "ANTHROPIC_CLOUD_WARNING",
    "DEFAULT_MODELS",
    "AnthropicCompletion",
    "AnthropicProvider",
    "Completion",
    "EnclaveAttestationError",
    "EnclaveProvider",
    "GeminiProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderRateLimitError",
    "build_provider",
    "call_enclave",
    "cloud_warning",
    "known_providers",
    "provider_display_name",
    "provider_is_cloud",
    "verify_enclave_attestation",
]


ANTHROPIC_CLOUD_WARNING: str = cloud_warning("Anthropic")
"""Deprecated: the cloud-egress warning bound to Anthropic.

Retained for back-compat; new code should call
:func:`creek.classify.llm.consent.cloud_warning` with the active provider name.
"""


def _resolve_configured_model(configured: str | None, default: str) -> str:
    """Resolve a provider's model id from the config value and its own default.

    "Unset" is explicit — ``None`` or blank — never a sentinel match against
    another module's default literal, so each provider's resolution cannot be
    broken by a change to ``LLMConfig``'s declared default and an explicit
    model id is always honored verbatim.

    Args:
        configured: The ``LLMConfig.model`` value (``None`` means unset).
        default: The calling provider's own default model id.

    Returns:
        *configured* stripped when it holds a non-blank value, else *default*.
    """
    model = (configured or "").strip()
    return model or default


_HTTP_TOO_MANY_REQUESTS: int = 429
"""The HTTP status code for a rate-limited request."""


class ProviderRateLimitError(RuntimeError):
    """A vendor rate-limit (429) response, with the server's backoff hint.

    Subclasses :class:`RuntimeError` so every existing retry/except path keeps
    catching it; the message stays redacted to the exception type name like
    any other provider error. The only extra information carried is the
    ``Retry-After`` value — never request state or response bodies — so the
    retry loop can honor the server's backoff instead of retrying blind.

    Attributes:
        retry_after: Seconds the server asked clients to wait before retrying,
            or ``None`` when the response carried no usable hint.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        """Store the redacted message and the optional backoff hint.

        Args:
            message: The redacted, provider-prefixed error message.
            retry_after: Seconds from the response's ``Retry-After`` header,
                or ``None`` when absent.
        """
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after_seconds(exc: object) -> float | None:
    """Extract a ``Retry-After`` hint (in seconds) from a vendor exception.

    Every supported SDK attaches the underlying ``httpx.Response`` to its
    rate-limit exception; the header is read defensively so a missing
    response, missing header, or HTTP-date form (which ``float`` rejects)
    degrades to ``None`` rather than raising.

    Args:
        exc: The vendor SDK exception.

    Returns:
        The non-negative seconds value, or ``None`` when unavailable.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


_ERROR_DETAIL_MAX_CHARS: int = 500
"""Cap on the API error message surfaced in a wrapped ``RuntimeError``."""

_CLIENT_ERROR_MIN: int = 400
"""Inclusive lower bound of the HTTP client-error (4xx) range."""

_SERVER_ERROR_MIN: int = 500
"""Exclusive upper bound of the 4xx range / start of the 5xx server range."""


def _error_status(exc: Exception) -> int | None:
    """Return the integer HTTP status of a provider SDK error, if it exposes one.

    Reads ``status_code`` (Anthropic / OpenAI) then ``code`` (Gemini); anything
    non-integer (or absent) yields ``None`` so callers fail closed to the most
    redacted form.

    Args:
        exc: The vendor SDK exception.

    Returns:
        The HTTP status as an ``int``, or ``None`` when unavailable.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def _error_detail(exc: Exception) -> str:
    """Return a redaction-safe detail string for a provider SDK error.

    Two tiers, by HTTP status:

    - **4xx client errors** — the API's own ``message`` is surfaced (collapsed
      and length-capped) after the status. A 4xx message is an API-side
      description of what is wrong with the *request or account* — "credit
      balance too low", an invalid model id, a bad parameter — never the prompt
      or fragment content, and never the API key (which lives only in request
      headers). This is what turns an opaque ``BadRequestError`` into an
      actionable cause.
    - **5xx / unknown** — status only (or nothing). Server-error bodies are
      opaque and may carry unredacted internals, so they stay redacted.

    Args:
        exc: The vendor SDK exception.

    Returns:
        A suffix ready to append after the exception type name — one of
        ``" (HTTP <s>): <message>"``, ``" (HTTP <s>)"``, or ``""``.
    """
    status = _error_status(exc)
    if status is None:
        return ""
    if not _CLIENT_ERROR_MIN <= status < _SERVER_ERROR_MIN:
        return f" (HTTP {status})"
    message = getattr(exc, "message", None)
    if not isinstance(message, str) or not message.strip():
        return f" (HTTP {status})"
    collapsed = " ".join(message.split())
    if len(collapsed) > _ERROR_DETAIL_MAX_CHARS:
        collapsed = collapsed[:_ERROR_DETAIL_MAX_CHARS] + "…"
    return f" (HTTP {status}): {collapsed}"


def _chain_is_safe(exc: Exception) -> bool:
    """Whether the SDK error may remain as the wrapped ``RuntimeError.__cause__``.

    True only for 4xx client errors — the same tier whose ``message``
    :func:`_error_detail` already surfaces, so the chain exposes nothing the
    message does not. For 5xx / unknown errors the body is deliberately
    withheld, so the caller suppresses the chain (``raise … from None``) to keep
    that redaction airtight even if a future caller logs with
    ``logging.exception`` / ``exc_info=True`` (which would otherwise render
    ``__cause__``).

    Args:
        exc: The vendor SDK exception.

    Returns:
        ``True`` when *exc* carries a 4xx status, else ``False``.
    """
    status = _error_status(exc)
    return status is not None and _CLIENT_ERROR_MIN <= status < _SERVER_ERROR_MIN


# Per-provider default model IDs. This matrix is the ONLY place model literals
# live in the package — provider classes read their ``DEFAULT_MODEL`` from it
# and other modules read the resolved value, never a literal (enforced by
# ``tests/test_no_model_literals.py``). Mirrors CrawDad's ``config.py`` dicts
# so "introduce another model" is a one-place edit in both packages.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "enclave": "llama-3.3-70b",
    "gemini": "gemini-3.5-flash",
    "ollama": "mistral",
    "openai": "gpt-5.4",
}


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

    DEFAULT_MODEL: str = DEFAULT_MODELS["anthropic"]
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

        Falls back to :attr:`DEFAULT_MODEL` when ``config.model`` is unset
        (``None`` or blank); an explicit value is honored verbatim.

        Returns:
            The resolved model identifier.
        """
        return _resolve_configured_model(self.config.model, self.DEFAULT_MODEL)

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
            RuntimeError: If the SDK raises any ``AnthropicError``; re-raised
                as ``RuntimeError`` with the exception type name and, for 4xx
                client errors, the API's own error message (see
                :func:`_error_detail`). 5xx / unknown bodies are withheld and
                their exception chain suppressed to avoid leaking request state.
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
            RuntimeError: If the SDK raises any ``AnthropicError``; re-raised
                as ``RuntimeError`` with the exception type name and, for 4xx
                client errors, the API's own error message (see
                :func:`_error_detail`). 5xx / unknown bodies are withheld and
                their exception chain suppressed to avoid leaking request state.
        """
        import anthropic

        ceiling = self.MAX_TOKENS if max_tokens is None else max_tokens
        try:
            response = self._create_message(prompt, ceiling, system)
        except anthropic.RateLimitError as exc:
            msg = f"Anthropic API call rate-limited: {type(exc).__name__}"
            raise ProviderRateLimitError(
                msg, retry_after=_retry_after_seconds(exc)
            ) from None
        except anthropic.AnthropicError as exc:
            detail = _error_detail(exc)
            msg = f"Anthropic API call failed: {type(exc).__name__}{detail}"
            raise RuntimeError(msg) from (exc if _chain_is_safe(exc) else None)
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
                "model": _resolve_configured_model(
                    config.model, DEFAULT_MODELS["ollama"]
                ),
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

    DEFAULT_MODEL: str = DEFAULT_MODELS["ollama"]
    """Default Ollama model when the config does not override it."""

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
            ``config.model`` verbatim when set; :attr:`DEFAULT_MODEL` when
            unset (``None`` or blank).
        """
        return _resolve_configured_model(self.config.model, self.DEFAULT_MODEL)

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


#: Endpoint paths on the attested GPU-CC enclave (#760).
ENCLAVE_ATTESTATION_PATH: str = "/attestation"
ENCLAVE_GENERATE_PATH: str = "/generate"

# Fixed refusal messages, kept out of the ``raise`` sites so the exception carries
# a stable string and no long literal sits at the raise (tryceratops TRY003).
_ENCLAVE_NO_URL = "enclave provider requires enclave_url; refusing to send data"
_ENCLAVE_NO_POLICY = (
    "enclave provider requires enclave_expected_measurement (attestation policy); "
    "refusing to send data"
)
_ENCLAVE_UNVERIFIED = "enclave attestation could not be verified; refusing to send data"
_ENCLAVE_MISMATCH = "enclave attestation measurement mismatch; refusing to send data"


class EnclaveAttestationError(RuntimeError):
    """Raised when the GPU-CC enclave cannot be attested — egress is refused.

    A subclass of :class:`RuntimeError` so the classifier's existing
    retry/degradation handling (which catches ``RuntimeError``) treats a failed
    attestation as provider-unavailable rather than crashing — but the fail is
    always *closed*: no prompt is ever sent when this is raised.
    """


def verify_enclave_attestation(config: LLMConfig, *, timeout: float) -> None:
    """Verify the enclave's remote attestation *before* any prompt egress.

    This is the "attest, then mount" gate that lets the enclave be classified
    ``is_cloud=False``: it fetches the enclave's attestation quote from
    ``{enclave_url}/attestation`` and requires the attested measurement to equal
    ``config.enclave_expected_measurement`` (constant-time compare). It refuses —
    raising :class:`EnclaveAttestationError`, never returning — when the endpoint
    or policy is unconfigured, the quote is unreachable, or the measurement does
    not match. Callers MUST invoke this before sending any prompt so a failure
    can never fall back to plaintext egress.

    Args:
        config: LLM configuration carrying ``enclave_url`` and
            ``enclave_expected_measurement``.
        timeout: HTTP timeout for the attestation fetch, in seconds.

    Raises:
        EnclaveAttestationError: On any unconfigured, unreachable, or mismatched
            attestation — egress is refused.
    """
    if not config.enclave_url:
        raise EnclaveAttestationError(_ENCLAVE_NO_URL)
    if not config.enclave_expected_measurement:
        raise EnclaveAttestationError(_ENCLAVE_NO_POLICY)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{config.enclave_url}{ENCLAVE_ATTESTATION_PATH}")
            resp.raise_for_status()
            quote = resp.json()
    except httpx.HTTPError as exc:
        raise EnclaveAttestationError(_ENCLAVE_UNVERIFIED) from exc
    measured = str(quote.get("measurement", "")) if isinstance(quote, dict) else ""
    if not measured or not hmac.compare_digest(
        measured, config.enclave_expected_measurement
    ):
        raise EnclaveAttestationError(_ENCLAVE_MISMATCH)


def call_enclave(
    config: LLMConfig,
    prompt: str,
    *,
    timeout: float,
    max_tokens: int | None = None,
    system: str | None = None,
) -> str:
    """Send *prompt* to the attested enclave's generate endpoint and return text.

    The caller (:meth:`EnclaveProvider.complete`) MUST have verified attestation
    first; this helper performs no gating of its own.

    Args:
        config: LLM configuration carrying ``enclave_url`` and model name.
        prompt: The fully-formatted prompt.
        timeout: HTTP request timeout, in seconds.
        max_tokens: Optional output ceiling forwarded to the enclave.
        system: Optional system prompt forwarded to the enclave.

    Returns:
        The raw response text from the enclave.

    Raises:
        httpx.HTTPError: On connection, transport, or HTTP error responses.
    """
    payload: dict[str, object] = {
        "model": _resolve_configured_model(config.model, DEFAULT_MODELS["enclave"]),
        "prompt": prompt,
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if system is not None:
        payload["system"] = system
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{config.enclave_url}{ENCLAVE_GENERATE_PATH}", json=payload
        )
        response.raise_for_status()
        data = response.json()
        return str(data.get("response", ""))


class EnclaveProvider:
    """Attested GPU-CC enclave provider — ``is_cloud=False`` via attest-then-mount.

    Sends prompts to a remote confidential-compute endpoint, yet is classified
    ``is_cloud=False`` because :func:`verify_enclave_attestation` runs before
    every :meth:`complete` call: remote attestation proves the enclave is the
    expected, operator-unreadable image before any prompt or key leaves the
    device. This is what lets INTIMATE-tier content get GPU-quality voice output
    without egressing to a readable cloud — the attestation gate, not a bare
    flag, is the security boundary (issue #760, decision #757/#755).

    Attributes:
        config: The LLM configuration carrying the enclave URL, attestation
            policy, and model name.
    """

    is_cloud: bool = False
    """The enclave is operator-unreadable once attested; content stays private."""

    PROVIDER_NAME: str = "Enclave (GPU-CC)"
    """Display name for the confidential-compute endpoint."""

    DEFAULT_MODEL: str = DEFAULT_MODELS["enclave"]
    """Default model served by the enclave when the config does not override it."""

    REQUEST_TIMEOUT: float = 120.0
    """HTTP request timeout for completion calls, in seconds."""

    ATTESTATION_TIMEOUT: float = 10.0
    """Timeout for the attestation-quote fetch, in seconds."""

    def __init__(self, config: LLMConfig) -> None:
        """Store the configuration; performs no network I/O.

        Attestation runs at call time (in :meth:`complete`/:attr:`available`) so
        each egress is gated on a fresh quote — an enclave can be replaced
        between calls, so a single construction-time check would be stale.

        Args:
            config: LLM provider configuration (enclave URL + attestation policy).
        """
        self.config = config

    @property
    def model(self) -> str:
        """The configured enclave model identifier.

        Returns:
            ``config.model`` verbatim when set; :attr:`DEFAULT_MODEL` when unset.
        """
        return _resolve_configured_model(self.config.model, self.DEFAULT_MODEL)

    @property
    def available(self) -> bool:
        """Whether the enclave is configured and currently attests successfully.

        Returns:
            ``True`` when a fresh attestation verifies; ``False`` otherwise (the
            provider then reports unavailable rather than sending any data).
        """
        try:
            verify_enclave_attestation(self.config, timeout=self.ATTESTATION_TIMEOUT)
        except EnclaveAttestationError:
            return False
        return True

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Attest the enclave, then send *prompt* and return a :class:`Completion`.

        Attestation is verified **first**; on any attestation failure this raises
        :class:`EnclaveAttestationError` and no prompt is sent — there is no
        plaintext fallback. Only after attestation succeeds does the prompt leave
        the device.

        Args:
            prompt: The fully-formatted prompt.
            max_tokens: Optional output ceiling forwarded to the enclave.
            system: Optional system prompt forwarded to the enclave.

        Returns:
            A :class:`Completion` wrapping the enclave's response text.

        Raises:
            EnclaveAttestationError: When attestation fails — egress is refused.
            httpx.HTTPError: On a transport/HTTP error of the generate call.
        """
        verify_enclave_attestation(self.config, timeout=self.ATTESTATION_TIMEOUT)
        text = call_enclave(
            self.config,
            prompt,
            timeout=self.REQUEST_TIMEOUT,
            max_tokens=max_tokens,
            system=system,
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

    DEFAULT_MODEL: str = DEFAULT_MODELS["openai"]
    """Default OpenAI model when the config does not override it."""

    API_KEY_ENV: str = "OPENAI_API_KEY"
    """Environment variable name for the OpenAI API key."""

    MAX_TOKENS: int = 1024
    """Maximum tokens requested from the OpenAI API per call."""

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

        Falls back to :attr:`DEFAULT_MODEL` when ``config.model`` is unset
        (``None`` or blank); an explicit value is honored verbatim.

        Returns:
            The resolved model identifier.
        """
        return _resolve_configured_model(self.config.model, self.DEFAULT_MODEL)

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
                the exception type name and, for 4xx client errors, the API's
                own error message (see :func:`_error_detail`). 5xx / unknown
                bodies are withheld and their chain suppressed so no request
                state leaks.
        """
        import openai

        ceiling = self.MAX_TOKENS if max_tokens is None else max_tokens
        try:
            response = self._create_chat(prompt, ceiling, system)
        except openai.RateLimitError as exc:
            msg = f"OpenAI API call rate-limited: {type(exc).__name__}"
            raise ProviderRateLimitError(
                msg, retry_after=_retry_after_seconds(exc)
            ) from None
        except openai.OpenAIError as exc:
            msg = f"OpenAI API call failed: {type(exc).__name__}{_error_detail(exc)}"
            raise RuntimeError(msg) from (exc if _chain_is_safe(exc) else None)
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


#: Gemini ``finish_reason`` → normalized :class:`Completion` stop reason.
_GEMINI_STOP_REASONS: dict[str, str] = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}


class GeminiProvider:
    """Google Gemini provider for cloud-based fragment classification (#608).

    Sends prompts to the Gemini ``generate_content`` API via the current
    ``google-genai`` SDK (``from google import genai``). The API key is read by
    the SDK from ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY`` and is never stored in
    config, logs, or error messages. Structure mirrors :class:`OpenAIProvider`
    and :class:`AnthropicProvider` so the providers stay legible.

    Attributes:
        config: The LLM configuration specifying model, batch size, etc.
    """

    is_cloud: bool = True
    """Gemini sends fragment content off-device; gated by consent."""

    PROVIDER_NAME: str = "Gemini"
    """Display name woven into consent and cloud-egress messages."""

    DEFAULT_MODEL: str = DEFAULT_MODELS["gemini"]
    """Default Gemini model when the config does not override it."""

    API_KEY_ENVS: tuple[str, ...] = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
    """Environment variables the SDK reads for the key; either suffices."""

    MAX_TOKENS: int = 1024
    """Maximum output tokens requested from the Gemini API per call."""

    def __init__(self, config: LLMConfig) -> None:
        """Validate environment prerequisites and prepare the client.

        The SDK client is instantiated lazily on first use. The cloud-egress
        consent gate is shared with every cloud provider (#606).

        Args:
            config: LLM provider configuration.

        Raises:
            RuntimeError: If no key variable is set or cloud consent is missing.
        """
        self.config = config
        unmet = self._missing_prerequisite()
        if unmet is not None:
            raise RuntimeError(unmet)
        self._client: genai.Client | None = None

    @classmethod
    def _missing_prerequisite(cls) -> str | None:
        """Return why the Gemini prerequisites are unmet, or ``None``.

        Requires one of :attr:`API_KEY_ENVS` plus the shared cloud consent;
        both are read from the environment at call time so :attr:`available`
        reflects the live env.

        Returns:
            A human-readable explanation when no key is present or consent is
            not affirmative; ``None`` when both prerequisites are satisfied.
        """
        if not any(os.environ.get(var, "").strip() for var in cls.API_KEY_ENVS):
            joined = " or ".join(cls.API_KEY_ENVS)
            return f"Neither {joined} is set; one is required for the Gemini provider."
        if not has_cloud_consent():
            return consent_error_message(cls.PROVIDER_NAME)
        return None

    @property
    def available(self) -> bool:
        """Whether the Gemini prerequisites (a key + consent) are met.

        Returns:
            ``True`` when a key variable is set and cloud consent is affirmative.
        """
        return self._missing_prerequisite() is None

    @property
    def model(self) -> str:
        """The Gemini model identifier to use for API calls.

        Falls back to :attr:`DEFAULT_MODEL` when ``config.model`` is unset
        (``None`` or blank); an explicit value is honored verbatim.

        Returns:
            The resolved model identifier.
        """
        return _resolve_configured_model(self.config.model, self.DEFAULT_MODEL)

    @property
    def client(self) -> genai.Client:
        """The lazily-initialised ``google-genai`` client.

        The SDK reads ``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` itself.

        Returns:
            A ``google.genai.Client`` instance.
        """
        if self._client is None:
            from google import genai

            self._client = genai.Client()
        return self._client

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Send *prompt* to Gemini and return a normalized :class:`Completion`.

        Args:
            prompt: The dynamic, fully-formatted user prompt.
            max_tokens: Maximum output tokens. ``None`` keeps the historical
                :attr:`MAX_TOKENS` ceiling.
            system: Optional system instruction. ``None`` sends none.

        Returns:
            A :class:`Completion` carrying the response text, the mapped stop
            reason, and token usage when present.

        Raises:
            RuntimeError: If the SDK raises any ``APIError``; re-raised with
                the exception type name and, for 4xx client errors, the API's
                own error message (see :func:`_error_detail`). 5xx / unknown
                bodies are withheld and their chain suppressed so no request
                state leaks.
        """
        from google.genai import errors

        ceiling = self.MAX_TOKENS if max_tokens is None else max_tokens
        try:
            response = self._generate(prompt, ceiling, system)
        except errors.APIError as exc:
            if getattr(exc, "code", None) == _HTTP_TOO_MANY_REQUESTS:
                msg = f"Gemini API call rate-limited: {type(exc).__name__}"
                raise ProviderRateLimitError(
                    msg, retry_after=_retry_after_seconds(exc)
                ) from None
            msg = f"Gemini API call failed: {type(exc).__name__}{_error_detail(exc)}"
            raise RuntimeError(msg) from (exc if _chain_is_safe(exc) else None)
        return Completion(
            text=_extract_gemini_text(response),
            stop_reason=_map_gemini_stop_reason(response),
            usage=_extract_gemini_usage(response),
        )

    def _generate(self, prompt: str, ceiling: int, system: str | None) -> object:
        """Call ``models.generate_content`` with a configured generation request.

        Args:
            prompt: The dynamic user prompt sent as ``contents``.
            ceiling: The resolved ``max_output_tokens`` value.
            system: Optional system instruction, or ``None``.

        Returns:
            The raw ``generate_content`` response object.
        """
        from google.genai import types

        config = types.GenerateContentConfig(
            max_output_tokens=ceiling,
            system_instruction=system,
        )
        return self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )


def _extract_gemini_text(response: object) -> str:
    """Concatenate the textual parts of a Gemini response's first candidate.

    Args:
        response: A ``generate_content`` response object.

    Returns:
        The joined ``text`` of each content part, or ``""`` when absent.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    out: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _map_gemini_stop_reason(response: object) -> str:
    """Map a Gemini ``finish_reason`` to a normalized stop reason.

    Accepts both the SDK's ``FinishReason`` enum (via ``.name``) and a plain
    string.

    Args:
        response: A ``generate_content`` response object.

    Returns:
        ``"end_turn"`` for ``STOP`` (and unknown/absent reasons),
        ``"max_tokens"`` for ``MAX_TOKENS``.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "end_turn"
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return "end_turn"
    name = getattr(reason, "name", None)
    key = name if isinstance(name, str) else str(reason)
    return _GEMINI_STOP_REASONS.get(key, "end_turn")


def _extract_gemini_usage(response: object) -> dict[str, int] | None:
    """Extract integer token-usage counts from a Gemini response.

    Args:
        response: A ``generate_content`` response object.

    Returns:
        A dict with ``input_tokens`` / ``output_tokens`` mirroring the other
        providers' usage shape, or ``None`` when the SDK omitted usage.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    counts: dict[str, int] = {}
    prompt_tokens = getattr(usage, "prompt_token_count", None)
    candidate_tokens = getattr(usage, "candidates_token_count", None)
    if isinstance(prompt_tokens, int):
        counts["input_tokens"] = prompt_tokens
    if isinstance(candidate_tokens, int):
        counts["output_tokens"] = candidate_tokens
    return counts or None


#: Registry mapping the ``LLMConfig.provider`` string to its provider class.
#: The value is the concrete class (not the ``LLMProvider`` protocol) so it is
#: both constructible and exposes the class-level ``is_cloud`` flag; widen the
#: union as new providers land.
_PROVIDER_REGISTRY: dict[
    str,
    type[
        AnthropicProvider
        | OllamaProvider
        | OpenAIProvider
        | GeminiProvider
        | EnclaveProvider
    ],
] = {
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "enclave": EnclaveProvider,
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


def known_providers() -> tuple[str, ...]:
    """Return the registered provider names, sorted.

    The single source of truth for "which providers exist" — config validation
    reads this rather than re-listing the names, so the set can never drift from
    the :data:`_PROVIDER_REGISTRY` the factory dispatches on.

    Returns:
        The registered ``LLMConfig.provider`` strings, sorted.
    """
    return tuple(sorted(_PROVIDER_REGISTRY))


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
