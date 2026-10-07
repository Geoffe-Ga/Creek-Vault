"""The benchmark's Ollama client: pinned context, eviction, digest, residency.

Production's :class:`creek.classify.llm.providers.OllamaProvider` sends only
``options.num_predict``, so the context window is whatever the runtime
defaults to and no input-size promise can be benchmarked. This client pins
``num_ctx`` on every request and adds what a capacity run needs and production
does not: evicting the model for a cold trial (``keep_alive: 0``), resolving
the weights' digest and details for the report, and reading the resident
model size from ``/api/ps``.

It shares production's request timeout (read from ``OllamaProvider`` at call
time) so a trial is shed exactly when a real reflection would be. It never
changes production behaviour; whether production should pin ``num_ctx`` too is
B05's call.

Nothing a response says reaches the caller as free text: the digest is
validated as hex, the details are returned for pattern-validated metadata, and
the license is reduced to an identifier from :data:`_LICENSE_MARKERS` or
``unknown`` — the license *text* is never returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from creek.classify.llm import providers
from creek.classify.llm.local_boundary import ollama_client
from creek_mcp.bench.metadata import UNKNOWN
from creek_mcp.bench.outcome import ContextOverflowError, ProviderUnavailableError

if TYPE_CHECKING:
    from creek.models import PrivacyTier
    from creek_mcp.bench.protocols import LLMCallable, LLMFactory

_LICENSE_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("apache license", "Apache-2.0"),
    ("mit license", "MIT"),
    ("llama 3", "Llama-3-Community"),
    ("llama 2", "Llama-2-Community"),
    ("gemma terms of use", "Gemma"),
    ("mistral ai research license", "MRL"),
)
"""Lower-cased license-text markers and the fixed identifier each maps to."""

_HEX_DIGEST: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_DEFAULT_TAG: Final[str] = "latest"
_UNREACHABLE: Final[str] = "the benchmark's Ollama endpoint is unreachable"
_NOT_INSTALLED: Final[str] = "the pinned model is not installed"
_MISMATCH: Final[str] = "the installed model digest does not match the pin"
_OVERFLOW: Final[str] = "the prompt filled the context window and was truncated"


class DigestMismatchError(ValueError):
    """The weights Ollama serves are not the weights the run pinned."""


@dataclass(frozen=True, slots=True)
class PinnedModel:
    """What the runtime reports about the exact weights it serves.

    Attributes:
        digest: ``sha256:<hex>``, or ``None`` when the model is not installed.
        quantization: Reported quantization level, if any.
        parameter_count: Reported parameter size, if any.
        license_id: A fixed identifier, or ``unknown``; never license text.
    """

    digest: str | None
    quantization: str | None
    parameter_count: str | None
    license_id: str


def _canonical_tag(tag: str) -> str:
    """Spell an omitted tag the way Ollama lists it (``name:latest``)."""
    return tag if ":" in tag else f"{tag}:{_DEFAULT_TAG}"


def _license_id(text: object) -> str:
    """Reduce license *text* to a fixed identifier, never echoing it."""
    lowered = text.lower() if isinstance(text, str) else ""
    for marker, identifier in _LICENSE_MARKERS:
        if marker in lowered:
            return identifier
    return UNKNOWN


def _optional_str(value: object) -> str | None:
    """Return *value* when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


class _BenchLLM:
    """One bounded completion callable; positively local."""

    is_cloud: bool = False

    def __init__(self, client: BenchOllamaClient, num_predict: int) -> None:
        """Bind *client* and this call's output ceiling."""
        self._client = client
        self._num_predict = num_predict

    def __call__(self, prompt: str) -> str:
        """Generate a completion for *prompt*."""
        return self._client.generate(prompt, num_predict=self._num_predict)


class BenchOllamaClient:
    """A minimal Ollama client for capacity trials."""

    def __init__(
        self,
        base_url: str,
        model_tag: str,
        *,
        num_ctx: int,
        num_predict: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Configure the client; performs no I/O.

        Args:
            base_url: The Ollama endpoint, e.g. ``http://127.0.0.1:11434``.
            model_tag: The model to drive.
            num_ctx: Context window pinned on every completion.
            num_predict: Run-wide ceiling on output tokens; a request's own
                ``max_tokens`` can only lower it.
            transport: Injected transport (tests use ``httpx.MockTransport``).
        """
        self._base_url = base_url
        self._model_tag = model_tag
        self._num_ctx = num_ctx
        self._num_predict = num_predict
        self._transport = transport

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        """Send one request and return its decoded JSON body.

        Raises:
            ProviderUnavailableError: When the endpoint cannot be reached.
            httpx.HTTPStatusError: On a non-2xx answer.
        """
        timeout = providers.OllamaProvider.REQUEST_TIMEOUT
        try:
            with ollama_client(
                timeout, base_url=self._base_url, transport=self._transport
            ) as client:
                response = client.request(method, path, json=body)
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(_UNREACHABLE) from exc
        response.raise_for_status()
        return response.json()

    def generate(self, prompt: str, *, num_predict: int) -> str:
        """Return the model's completion for *prompt*.

        Args:
            prompt: The fully-formatted prompt.
            num_predict: The caller's output ceiling, capped by the run's.

        Returns:
            The completion text; empty when the body is malformed.
        """
        ceiling = min(num_predict, self._num_predict)
        payload = {
            "model": self._model_tag,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": ceiling,
            },
        }
        data = self._request("POST", "/api/generate", payload)
        if not isinstance(data, dict):
            return ""
        self._refuse_truncation(data.get("prompt_eval_count"), ceiling)
        return str(data.get("response", ""))

    def _refuse_truncation(self, evaluated: object, ceiling: int) -> None:
        """Raise when the evaluated prompt left no room for the output.

        Ollama silently truncates a prompt longer than ``num_ctx``; the only
        trace is ``prompt_eval_count`` reaching the window. A prompt evaluated
        at or past ``num_ctx - ceiling`` is treated as truncated. A runtime that
        reuses a cached prefix reports fewer evaluated tokens, so this can miss
        an overflow but never flags a prompt that left room for its output.

        Raises:
            ContextOverflowError: When the prompt filled the window.
        """
        if isinstance(evaluated, int) and evaluated >= self._num_ctx - ceiling:
            raise ContextOverflowError(_OVERFLOW)

    def factory(self) -> LLMFactory:
        """Return a reflect-shaped factory over this client."""

        def _build(tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
            del tier
            return _BenchLLM(self, max_tokens)

        return _build

    def evict(self) -> None:
        """Unload the model now, so the next trial starts cold."""
        self._request(
            "POST", "/api/generate", {"model": self._model_tag, "keep_alive": 0}
        )

    def _listed(self, path: str) -> dict[str, Any] | None:
        """Return this model's entry in the ``models`` list at *path*."""
        data = self._request("GET", path, None)
        models = data.get("models", []) if isinstance(data, dict) else []
        wanted = _canonical_tag(self._model_tag)
        for entry in models if isinstance(models, list) else []:
            if (
                isinstance(entry, dict)
                and _canonical_tag(str(entry.get("name"))) == wanted
            ):
                return entry
        return None

    def resolve_pinned(self) -> PinnedModel:
        """Report the digest, details and license id of the served weights."""
        entry = self._listed("/api/tags") or {}
        raw_digest = str(entry.get("digest", "")).removeprefix("sha256:")
        details = entry.get("details")
        details = details if isinstance(details, dict) else {}
        shown = self._request("POST", "/api/show", {"model": self._model_tag})
        license_text = shown.get("license") if isinstance(shown, dict) else None
        return PinnedModel(
            digest=f"sha256:{raw_digest}"
            if _HEX_DIGEST.fullmatch(raw_digest)
            else None,
            quantization=_optional_str(details.get("quantization_level")),
            parameter_count=_optional_str(details.get("parameter_size")),
            license_id=_license_id(license_text),
        )

    def verify_digest(self, expected: str) -> PinnedModel:
        """Refuse unless the served weights carry exactly *expected*.

        Returns:
            The pinned model description.

        Raises:
            DigestMismatchError: When the model is absent or its digest differs.
        """
        pinned = self.resolve_pinned()
        if pinned.digest is None:
            raise DigestMismatchError(_NOT_INSTALLED)
        if pinned.digest != expected:
            raise DigestMismatchError(_MISMATCH)
        return pinned

    def resident_bytes(self) -> int | None:
        """Return the loaded model's resident size, or ``None`` if not loaded."""
        entry = self._listed("/api/ps")
        size = entry.get("size") if entry is not None else None
        return size if isinstance(size, int) else None
