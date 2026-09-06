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
import secrets
from typing import TYPE_CHECKING

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

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


def _effective_key_env(config: LLMConfig, default: str) -> str:
    """Return the env var name holding this stage's API key (BYOK-aware).

    The BYOK override (``config.api_key_env``) when set, else the provider's
    *default* env var. Returns a variable **name**, never a key value — the key
    itself always stays in the environment.

    Args:
        config: The stage's LLM configuration.
        default: The provider's default key env var (e.g. ``ANTHROPIC_API_KEY``).

    Returns:
        The environment-variable name to read the API key from.
    """
    return config.api_key_env or default


def _read_key_env(env_var: str) -> str:
    """Read an API key from *env_var*, raising ``RuntimeError`` if unset.

    Used at lazy client-build time so a var that was present at construction but
    cleared before first use fails with the module's normal ``RuntimeError``
    rather than a raw ``KeyError``. The key value is never logged.

    Args:
        env_var: The environment variable name holding the key.

    Returns:
        The API key value.

    Raises:
        RuntimeError: When *env_var* is unset or blank.
    """
    key = os.environ.get(env_var, "").strip()
    if not key:
        msg = f"{env_var} environment variable is not set"
        raise RuntimeError(msg)
    return key


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

    def _missing_prerequisite(self) -> str | None:
        """Return why the Anthropic prerequisites are unmet, or ``None``.

        The API key and the explicit cloud-egress consent are both read from
        the environment at call time, so the check reflects the *current* env
        rather than the env at construction. The key is read from the BYOK
        override env var (``config.api_key_env``) when set, else the default
        ``ANTHROPIC_API_KEY`` — either way from the environment only. The consent
        half delegates to the provider-neutral :mod:`creek.classify.llm.consent`
        gate (#606), which accepts ``CREEK_CLOUD_CONSENT`` or the legacy
        ``CREEK_ANTHROPIC_CONSENT``. :meth:`__init__` raises the returned message;
        :attr:`available` reports whether it is ``None``.

        Returns:
            A human-readable explanation when the key is missing or consent is
            not affirmative; ``None`` when both prerequisites are satisfied.
        """
        key_env = _effective_key_env(self.config, self.API_KEY_ENV)
        if not os.environ.get(key_env, "").strip():
            return (
                f"{key_env} environment variable is not set; "
                "required for the Anthropic provider."
            )
        if not has_cloud_consent():
            return consent_error_message(self.PROVIDER_NAME)
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

        With a BYOK override (``config.api_key_env``), the user's key is read
        from that env var and passed explicitly; otherwise the SDK reads
        ``ANTHROPIC_API_KEY`` itself (behaviour unchanged). The key is only ever
        held in memory by the SDK — never logged or persisted.

        Returns:
            An ``anthropic.Anthropic`` client instance.
        """
        if self._client is None:
            import anthropic

            if self.config.api_key_env:
                self._client = anthropic.Anthropic(
                    api_key=_read_key_env(
                        _effective_key_env(self.config, self.API_KEY_ENV)
                    )
                )
            else:
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
    """Check that Ollama can serve the configured model.

    Args:
        config: LLM provider configuration with the Ollama URL.
        timeout: HTTP request timeout in seconds.

    Returns:
        ``True`` when Ollama replies ``200`` and its model inventory contains
        the configured (or default) model; ``False`` otherwise. Ollama treats
        an omitted tag as ``latest``, so those two spellings are equivalent.
    """
    model = _resolve_configured_model(config.model, DEFAULT_MODELS["ollama"])
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{config.ollama_url}/api/tags")
            if resp.status_code == 200 and _ollama_has_model(resp.json(), model):
                return True
    except (httpx.HTTPError, TypeError, ValueError):
        pass
    logger.warning(
        "Ollama model %r is not available at %s",
        model,
        config.ollama_url,
    )
    return False


def _ollama_has_model(payload: object, requested: str) -> bool:
    """Return whether an Ollama tags payload contains *requested*.

    Ollama's inventory has used both ``name`` and ``model`` for the canonical
    identifier, so either field is accepted. Malformed rows are ignored and a
    malformed envelope fails closed. An untagged generation request means the
    ``latest`` tag; explicit tags and digests must match verbatim.
    """
    if not isinstance(payload, dict):
        return False
    models = payload.get("models")
    if not isinstance(models, list):
        return False
    accepted = {requested}
    tail = requested.rsplit("/", maxsplit=1)[-1]
    if ":" not in tail and "@" not in tail:
        accepted.add(f"{requested}:latest")
    for item in models:
        if not isinstance(item, dict):
            continue
        for field in ("name", "model"):
            installed = item.get(field)
            if isinstance(installed, str) and installed in accepted:
                return True
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
        """Whether the local Ollama endpoint can serve this provider's model.

        Returns:
            ``True`` when ``/api/tags`` lists :attr:`model`.
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

#: Bytes of entropy in the per-call attestation nonce (anti-replay challenge).
_ENCLAVE_NONCE_BYTES: int = 32

# Fixed refusal messages, kept out of the ``raise`` sites so the exception carries
# a stable string and no long literal sits at the raise (tryceratops TRY003).
_ENCLAVE_NO_URL = "enclave provider requires enclave_url; refusing to send data"
_ENCLAVE_INSECURE = (
    "enclave_url must use https:// (attestation and prompts require TLS); "
    "refusing to send data"
)
_ENCLAVE_NO_POLICY = (
    "enclave provider requires enclave_expected_measurement (attestation policy); "
    "refusing to send data"
)
_ENCLAVE_NO_TRUST_ROOT = (
    "enclave provider requires enclave_attestation_pubkey (trust root); "
    "refusing to send data"
)
_ENCLAVE_UNVERIFIED = "enclave attestation could not be verified; refusing to send data"
_ENCLAVE_STALE = (
    "enclave attestation nonce mismatch (stale/replayed quote); refusing to send data"
)
_ENCLAVE_BAD_SIGNATURE = (
    "enclave attestation signature is not valid under the configured trust root; "
    "refusing to send data"
)
_ENCLAVE_MISMATCH = "enclave attestation measurement mismatch; refusing to send data"


class EnclaveAttestationError(RuntimeError):
    """Raised when the GPU-CC enclave cannot be attested — egress is refused.

    A subclass of :class:`RuntimeError` so the classifier's existing
    retry/degradation handling (which catches ``RuntimeError``) treats a failed
    attestation as provider-unavailable rather than crashing — but the fail is
    always *closed*: no prompt is ever sent when this is raised.
    """


def _attestation_signed_payload(measurement: str, nonce: str) -> bytes:
    """Return the exact bytes the enclave signs: measurement bound to the nonce.

    Domain-separated (newline between fields) so a measurement value can never be
    reinterpreted as part of the nonce or vice versa.
    """
    return f"creek-enclave-attestation\n{measurement}\n{nonce}".encode()


def _load_attestation_pubkey(pubkey_hex: str) -> Ed25519PublicKey:
    """Load the configured hex-encoded Ed25519 trust-root public key."""
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))


def _fetch_attestation_quote(
    url: str, nonce: str, *, timeout: float
) -> dict[str, object]:
    """Challenge the enclave with *nonce* and return its attestation quote.

    Args:
        url: The enclave base URL (already validated as ``https://``).
        nonce: The fresh anti-replay challenge sent to the enclave.
        timeout: HTTP timeout, in seconds.

    Returns:
        The parsed attestation quote (``measurement`` / ``nonce`` / ``signature``).

    Raises:
        EnclaveAttestationError: When the quote cannot be fetched or parsed.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"{url}{ENCLAVE_ATTESTATION_PATH}", params={"nonce": nonce}
            )
            resp.raise_for_status()
            quote = resp.json()
    except httpx.HTTPError as exc:
        raise EnclaveAttestationError(_ENCLAVE_UNVERIFIED) from exc
    if not isinstance(quote, dict):
        raise EnclaveAttestationError(_ENCLAVE_UNVERIFIED)
    return quote


def _verify_attestation_quote(
    quote: dict[str, object],
    *,
    sent_nonce: str,
    expected_measurement: str,
    pubkey_hex: str,
) -> None:
    """Cryptographically verify a fetched attestation quote, or refuse.

    In order: the quote must echo the fresh nonce (freshness / anti-replay); its
    signature over ``measurement || nonce`` must validate under the configured
    trust-root key (authenticity / integrity — an impersonator without the
    private key cannot forge this); and the attested measurement must equal the
    configured expectation (the enclave is the expected image).

    Args:
        quote: The parsed attestation quote from the enclave.
        sent_nonce: The fresh nonce this client challenged the enclave with.
        expected_measurement: The configured measurement the enclave must present.
        pubkey_hex: The configured hex Ed25519 trust-root public key.

    Raises:
        EnclaveAttestationError: On a stale nonce, an invalid signature, or a
            measurement mismatch — egress is refused.
    """
    measurement = str(quote.get("measurement", ""))
    returned_nonce = str(quote.get("nonce", ""))
    signature_hex = str(quote.get("signature", ""))

    if not returned_nonce or not hmac.compare_digest(returned_nonce, sent_nonce):
        raise EnclaveAttestationError(_ENCLAVE_STALE)

    try:
        pubkey = _load_attestation_pubkey(pubkey_hex)
        pubkey.verify(
            bytes.fromhex(signature_hex),
            _attestation_signed_payload(measurement, returned_nonce),
        )
    except (InvalidSignature, ValueError) as exc:
        raise EnclaveAttestationError(_ENCLAVE_BAD_SIGNATURE) from exc

    if not measurement or not hmac.compare_digest(measurement, expected_measurement):
        raise EnclaveAttestationError(_ENCLAVE_MISMATCH)


def verify_enclave_attestation(config: LLMConfig, *, timeout: float) -> None:
    """Cryptographically verify the enclave's attestation *before* any egress.

    This is the "attest, then mount" gate that justifies classifying the enclave
    ``is_cloud=False``. It challenges the enclave with a fresh random nonce over
    an ``https://`` channel, fetches the signed attestation quote, and refuses —
    raising :class:`EnclaveAttestationError`, never returning — unless **all** of
    the following hold, so a failure can never fall back to plaintext egress:

    - ``enclave_url`` is configured and uses ``https://`` (TLS-protected);
    - an attestation policy (``enclave_expected_measurement``) and a trust root
      (``enclave_attestation_pubkey``) are configured;
    - the quote echoes the fresh nonce (freshness — an old quote cannot be
      replayed);
    - the quote's signature over ``measurement || nonce`` validates under the
      configured Ed25519 trust-root key (authenticity — an impersonator without
      the private key cannot forge it);
    - the attested measurement equals the configured expectation.

    The trust root is the operator-provisioned public key, not a full vendor
    certificate chain; see ADR-0006 for the trust model and its boundaries.

    Args:
        config: LLM configuration carrying the enclave URL, attestation policy,
            and trust-root public key.
        timeout: HTTP timeout for the attestation fetch, in seconds.

    Raises:
        EnclaveAttestationError: On any missing prerequisite, insecure transport,
            unreachable quote, stale nonce, bad signature, or measurement
            mismatch — egress is refused.
    """
    if not config.enclave_url:
        raise EnclaveAttestationError(_ENCLAVE_NO_URL)
    if not config.enclave_url.lower().startswith("https://"):
        raise EnclaveAttestationError(_ENCLAVE_INSECURE)
    if not config.enclave_expected_measurement:
        raise EnclaveAttestationError(_ENCLAVE_NO_POLICY)
    if not config.enclave_attestation_pubkey:
        raise EnclaveAttestationError(_ENCLAVE_NO_TRUST_ROOT)
    nonce = secrets.token_hex(_ENCLAVE_NONCE_BYTES)
    quote = _fetch_attestation_quote(config.enclave_url, nonce, timeout=timeout)
    _verify_attestation_quote(
        quote,
        sent_nonce=nonce,
        expected_measurement=config.enclave_expected_measurement,
        pubkey_hex=config.enclave_attestation_pubkey,
    )


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
    every :meth:`complete` call: it cryptographically verifies — over ``https``,
    against an operator-configured Ed25519 trust root, bound to a fresh nonce —
    that the enclave is the expected confidential-compute image before any prompt
    or key leaves the device. This is what lets INTIMATE-tier content get
    GPU-quality voice output without egressing to a readable cloud — the
    attestation gate, not a bare flag, is the security boundary (issue #760,
    decision #757/#755; trust model in ADR-0006).

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

    def _missing_prerequisite(self) -> str | None:
        """Return why the OpenAI prerequisites are unmet, or ``None``.

        The key (from the BYOK override env var ``config.api_key_env`` when set,
        else the default ``OPENAI_API_KEY``) and the shared cloud-egress consent
        are read from the environment at call time; :meth:`__init__` raises the
        returned message and :attr:`available` reports whether it is ``None``.

        Returns:
            A human-readable explanation when the key is missing or consent is
            not affirmative; ``None`` when both prerequisites are satisfied.
        """
        key_env = _effective_key_env(self.config, self.API_KEY_ENV)
        if not os.environ.get(key_env, "").strip():
            return (
                f"{key_env} environment variable is not set; "
                "required for the OpenAI provider."
            )
        if not has_cloud_consent():
            return consent_error_message(self.PROVIDER_NAME)
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

        Without a BYOK override the SDK reads ``OPENAI_API_KEY`` itself; with one
        (``config.api_key_env``) the user's key is read from that env var and
        passed explicitly. ``config.api_base`` (when set) is forwarded as
        ``base_url`` for an OpenAI-compatible gateway. The key is only held in
        memory by the SDK — never logged or persisted.

        Returns:
            An ``openai.OpenAI`` client instance.
        """
        if self._client is None:
            import openai

            base_url = self.config.api_base
            byok_key = (
                _read_key_env(_effective_key_env(self.config, self.API_KEY_ENV))
                if self.config.api_key_env
                else None
            )
            if base_url is not None and byok_key is not None:
                self._client = openai.OpenAI(base_url=base_url, api_key=byok_key)
            elif base_url is not None:
                self._client = openai.OpenAI(base_url=base_url)
            elif byok_key is not None:
                self._client = openai.OpenAI(api_key=byok_key)
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
        Non-integer or missing fields are skipped so a partial usage object
        never raises.
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

    def _missing_prerequisite(self) -> str | None:
        """Return why the Gemini prerequisites are unmet, or ``None``.

        Requires a key plus the shared cloud consent, both read from the
        environment at call time so :attr:`available` reflects the live env. With
        a BYOK override (``config.api_key_env``) the key must be in that single
        var; otherwise one of :attr:`API_KEY_ENVS` must be set (behaviour
        unchanged).

        Returns:
            A human-readable explanation when no key is present or consent is
            not affirmative; ``None`` when both prerequisites are satisfied.
        """
        if self.config.api_key_env:
            if not os.environ.get(self.config.api_key_env, "").strip():
                return (
                    f"{self.config.api_key_env} environment variable is not set; "
                    "required for the Gemini provider."
                )
        elif not any(os.environ.get(var, "").strip() for var in self.API_KEY_ENVS):
            joined = " or ".join(self.API_KEY_ENVS)
            return f"Neither {joined} is set; one is required for the Gemini provider."
        if not has_cloud_consent():
            return consent_error_message(self.PROVIDER_NAME)
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

        Without a BYOK override the SDK reads ``GOOGLE_API_KEY`` /
        ``GEMINI_API_KEY`` itself; with one (``config.api_key_env``) the user's
        key is read from that env var and passed explicitly. The key is only held
        in memory by the SDK — never logged or persisted.

        Returns:
            A ``google.genai.Client`` instance.
        """
        if self._client is None:
            from google import genai

            if self.config.api_key_env:
                self._client = genai.Client(
                    api_key=_read_key_env(self.config.api_key_env)
                )
            else:
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
        Non-integer or missing fields are skipped so a partial usage object
        never raises.
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
