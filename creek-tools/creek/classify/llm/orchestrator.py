"""Recursive / batch orchestration for LLM-based fragment classification.

Owns the :class:`LLMClassifier` that wires the prompt, provider,
parsing, and FEAT-017 confidence-bias submodules together. Handles the
retry loop, batch concurrency, and OPS-003 structured logging so the
collaborators can stay pure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import yaml

from creek.classify.llm.batch import run_batch
from creek.classify.llm.calibration import _apply_wavelength
from creek.classify.llm.parsing import (
    _apply_frequency,
    _apply_voice,
    _split_reasoning_and_yaml,
    validate_response,
)
from creek.classify.llm.prompts import build_classification_prompt
from creek.classify.llm.providers import (
    ANTHROPIC_CLOUD_WARNING,
    AnthropicProvider,
    call_ollama,
    check_ollama_available,
)

if TYPE_CHECKING:
    from creek.config import LLMConfig
    from creek.models import Fragment

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMClassificationResult:
    """Outcome of a single FEAT-017 two-step classification call.

    Attributes:
        fragment: The fragment with updated classification fields.
            When the call short-circuits (provider unavailable, retries
            exhausted), this is the input fragment unchanged.
        reasoning: Reasoning preamble emitted by the model before the
            YAML payload. Empty when the model produced pure YAML (the
            pre-FEAT-017 response shape) or the call short-circuited.
            Truncation / tier-routing is the engine's responsibility,
            not this dataclass's — the raw trace lives here.
    """

    fragment: Fragment
    reasoning: str


class LLMClassifier:
    """LLM classifier for Creek fragments.

    Dispatches classification requests to either a local Ollama instance
    (default) or the Anthropic cloud API (``config.provider ==
    "anthropic"``).  Falls back gracefully when the configured provider
    is unreachable, returning fragments unchanged with a warning.

    Attributes:
        config: The LLM configuration specifying provider, model, etc.
    """

    MAX_RETRIES: int = 3
    """Maximum retry attempts for failed LLM calls."""

    RETRY_DELAY: float = 1.0
    """Seconds to wait between retries."""

    RATE_LIMIT_DELAY: float = 0.1
    """Seconds between batch request submissions."""

    REQUEST_TIMEOUT: float = 30.0
    """HTTP request timeout in seconds."""

    AVAILABILITY_TIMEOUT: float = 5.0
    """Timeout for the Ollama health check."""

    ANTHROPIC_PROVIDER: str = "anthropic"
    """Provider identifier for the Anthropic cloud API."""

    def __init__(self, config: LLMConfig) -> None:
        """Initialize the LLM classifier.

        Provider availability is checked lazily on first use.  When the
        Anthropic provider is selected, a warning is logged immediately
        so operators see it even before the first classification call.

        Args:
            config: LLM provider configuration (provider, model, URL, etc.).
        """
        self.config = config
        self._available: bool | None = None
        self._anthropic: AnthropicProvider | None = None
        if config.provider == self.ANTHROPIC_PROVIDER:
            logger.warning(ANTHROPIC_CLOUD_WARNING)

    @property
    def available(self) -> bool:
        """Whether the configured LLM provider is reachable.

        For Ollama, checks the HTTP health endpoint; for Anthropic,
        validates that the required environment variables are present.

        Returns:
            ``True`` if the provider responded successfully.
        """
        if self._available is None:
            self._available = self._check_availability()
        return self._available

    def _check_availability(self) -> bool:
        """Check if the configured LLM provider is reachable.

        For the Anthropic provider this validates that the required
        environment variables are present.  For Ollama it issues a
        health-check HTTP request against ``/api/tags``.

        Returns:
            ``True`` if the provider is ready to service requests.
        """
        if self.config.provider == self.ANTHROPIC_PROVIDER:
            return self._check_anthropic_availability()
        return check_ollama_available(
            self.config,
            timeout=self.AVAILABILITY_TIMEOUT,
        )

    def _check_anthropic_availability(self) -> bool:
        """Validate that the Anthropic provider can be constructed.

        Returns:
            ``True`` when the provider initialises successfully.
        """
        try:
            self._get_anthropic_provider()
        except RuntimeError as exc:
            logger.warning("Anthropic provider not available: %s", exc)
            return False
        return True

    def _get_anthropic_provider(self) -> AnthropicProvider:
        """Lazily instantiate the :class:`AnthropicProvider`.

        Returns:
            The cached ``AnthropicProvider`` instance.
        """
        if self._anthropic is None:
            self._anthropic = AnthropicProvider(self.config)
        return self._anthropic

    def _invoke_llm(self, prompt: str) -> str:
        """Dispatch a prompt to the configured provider.

        Args:
            prompt: The fully-formatted classification prompt.

        Returns:
            Raw response text from the provider.
        """
        if self.config.provider == self.ANTHROPIC_PROVIDER:
            return self._get_anthropic_provider().call(prompt)
        return self._call_ollama(prompt)

    def invoke_prompt(self, prompt: str) -> str:
        """Public dispatch helper for callers outside classification.

        Wraps :meth:`_invoke_llm` so consumers (e.g. the draft generator)
        can route prompts through the configured provider without
        depending on a private name.

        Args:
            prompt: The fully-formatted prompt.

        Returns:
            Raw response text from the provider.
        """
        return self._invoke_llm(prompt)

    def _build_prompt(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> str:
        """Build the FEAT-017 two-step classification prompt for a fragment.

        Thin wrapper around
        :func:`creek.classify.llm.prompts.build_classification_prompt`
        kept on the class so existing tests that call
        ``classifier._build_prompt(...)`` continue to work unchanged.

        Args:
            fragment: The fragment to classify.
            content: Optional content text for the fragment.

        Returns:
            The formatted prompt string.
        """
        return build_classification_prompt(self.config, fragment, content)

    def _call_ollama(self, prompt: str) -> str:
        """Send a prompt to Ollama and return the response text.

        Args:
            prompt: The prompt to send.

        Returns:
            The raw response text from the LLM.

        Raises:
            httpx.HTTPStatusError: On HTTP error responses.
            httpx.HTTPError: On connection or transport errors.
        """
        return call_ollama(
            self.config,
            prompt,
            timeout=self.REQUEST_TIMEOUT,
        )

    def validate_response(self, response_text: str) -> dict[str, object]:
        """Parse and strictly validate a YAML response from the LLM.

        Thin wrapper around
        :func:`creek.classify.llm.parsing.validate_response` kept on the
        class so existing callers and tests can call
        ``classifier.validate_response(...)`` unchanged.

        Args:
            response_text: Raw YAML text from the LLM.

        Returns:
            Parsed dictionary with classification data.

        Raises:
            ValueError: If the text is not a single YAML dict, or
                contains undocumented top-level keys.
        """
        return validate_response(response_text)

    def _apply_classification(
        self,
        fragment: Fragment,
        data: dict[str, object],
    ) -> Fragment:
        """Apply parsed LLM classification data to a fragment.

        Args:
            fragment: The original fragment.
            data: Parsed classification dict from ``validate_response``.

        Returns:
            A new fragment with updated classification fields.
        """
        updates: dict[str, object] = {}
        _apply_frequency(data, updates)
        _apply_wavelength(
            data,
            updates,
            unclassified_threshold=self.config.unclassified_threshold,
        )
        _apply_voice(data, updates)
        if not updates:
            return fragment
        return fragment.model_copy(update=updates)

    def classify(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> Fragment:
        """Classify a fragment using the configured LLM provider.

        Back-compat wrapper around :meth:`classify_with_reasoning` that
        drops the reasoning trace. Existing callers that only need the
        Fragment continue to work unchanged. New callers (e.g. the
        classification engine in :mod:`creek.classify.classify_engine`)
        should call :meth:`classify_with_reasoning` so the reasoning
        can be persisted alongside the classification.

        Args:
            fragment: The fragment to classify.
            content: Optional content text for the fragment.

        Returns:
            The fragment with updated classification fields.
        """
        return self.classify_with_reasoning(fragment, content).fragment

    def classify_with_reasoning(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> LLMClassificationResult:
        """Run the FEAT-017 two-step pipeline and return Fragment + reasoning.

        Builds the two-step prompt, dispatches it to the configured
        provider (Ollama or Anthropic), splits the response into a
        reasoning preamble and YAML payload, parses the YAML, and
        applies the classification. Retries on failure up to
        ``MAX_RETRIES`` times. Returns the fragment unchanged with an
        empty reasoning trace if the provider is unavailable or all
        retries are exhausted.

        Args:
            fragment: The fragment to classify.
            content: Optional content text for the fragment.

        Returns:
            :class:`LLMClassificationResult` carrying the updated
            fragment and the raw reasoning trace (empty when the call
            short-circuited or the model returned pure YAML).
        """
        if not self.available:
            logger.warning(
                "LLM provider unavailable — returning fragment '%s' unchanged",
                fragment.title,
            )
            return LLMClassificationResult(fragment=fragment, reasoning="")

        prompt = self._build_prompt(fragment, content)

        for attempt in range(self.MAX_RETRIES):
            try:
                raw = self._invoke_llm(prompt)
                reasoning, yaml_text = _split_reasoning_and_yaml(raw)
                data = self.validate_response(yaml_text)
            except (
                httpx.HTTPError,
                RuntimeError,
                ValueError,
                yaml.YAMLError,
            ) as exc:
                # OPS-003: include fragment ID + source path so the
                # operator can find the failing fragment quickly when
                # scanning logs. Prefix tag matches OPS-003 convention.
                logger.warning(
                    "[fragment=%s path=%s provider=%s] "
                    "Classify attempt %d/%d failed for '%s': %s",
                    fragment.id,
                    fragment.source.original_file or "<inline>",
                    self.config.provider,
                    attempt + 1,
                    self.MAX_RETRIES,
                    fragment.title,
                    exc,
                )
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY)
            else:
                # _apply_classification is a pure transform (no network I/O)
                # and lives in the else block intentionally: a failure here
                # is a programmer error, not a transient LLM/network glitch,
                # so it should propagate instead of triggering a retry.
                return LLMClassificationResult(
                    fragment=self._apply_classification(fragment, data),
                    reasoning=reasoning,
                )

        logger.error(
            "[fragment=%s path=%s provider=%s] "
            "All %d retries exhausted for '%s' — returning unchanged",
            fragment.id,
            fragment.source.original_file or "<inline>",
            self.config.provider,
            self.MAX_RETRIES,
            fragment.title,
        )
        return LLMClassificationResult(fragment=fragment, reasoning="")

    def classify_batch(
        self,
        fragments: list[Fragment],
        *,
        progress: bool = True,
    ) -> list[Fragment]:
        """Classify a batch of fragments with concurrency and progress.

        Delegates the thread-pool driver to
        :func:`creek.classify.llm.batch.run_batch`; this method handles
        the empty-list and provider-unavailable short-circuits so the
        driver stays focused on the happy path.

        Args:
            fragments: Fragments to classify.
            progress: Whether to show a tqdm progress bar.

        Returns:
            List of classified fragments in the same order.
        """
        if not fragments:
            return []

        if not self.available:
            logger.warning(
                "LLM provider unavailable — returning %d fragments unchanged",
                len(fragments),
            )
            return fragments.copy()

        return run_batch(self, fragments, progress=progress)


__all__ = [
    "LLMClassificationResult",
    "LLMClassifier",
]
