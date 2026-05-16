"""LLM-based classification for Creek fragments via Ollama or Anthropic.

Provides an ``LLMClassifier`` that delegates fragment classification to
either a local Ollama instance (default) or the Anthropic cloud API
(opt-in).  Falls back gracefully when the configured provider is
unavailable, returning fragments unchanged.

The Anthropic provider requires two environment variables to be set:

- ``ANTHROPIC_API_KEY``: The API key is **never** stored in the
  configuration file, logs, or error messages.
- ``CREEK_ANTHROPIC_CONSENT``: Must be ``"1"`` (or ``"true"`` / ``"yes"``)
  to acknowledge that fragment content will be sent to Anthropic's
  servers.
"""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import httpx
import yaml
from tqdm import tqdm

from creek.classify import few_shot

if TYPE_CHECKING:
    import anthropic

    from creek.config import LLMConfig

from creek.models import (
    Confidence,
    Dosage,
    Fragment,
    Frequency,
    FrequencyClassification,
    Mode,
    Orientation,
    Phase,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

logger = logging.getLogger(__name__)

_EnumT = TypeVar("_EnumT", bound=StrEnum)

CLASSIFICATION_PROMPT: str = """\
You are a classification assistant for the Creek knowledge organization system.

Given a fragment of content, classify it along the following dimensions:

1. **Frequency** (APTITUDE F1-F10): Which frequency best describes the content?
   - F1: Survival/Safety, F2: Belonging/Tribe, F3: Power/Agency,
   - F4: Order/Structure, F5: Achievement/Strategy, F6: Community/Empathy,
   - F7: Systems/Integration, F8: Holistic/Ecology, F9: Witness/Being,
   - F10: Unity/Non-dual

2. **Wavelength Phase**: rising, peaking, withdrawal, diminishing, \
bottoming_out, restoration

3. **Engagement Mode**: inhabit, express, collaborate, integrate, absorb

4. **Orientation**: do, feel, do_feel

5. **Dosage**: medicine, toxic, ambiguous

6. **Voice Register**: confessional, analytical, playful, prophetic, \
instructional, raw, conversational

7. **Confidence**: musing, exploring, forming, settled, conviction

A few labelled examples (do not include these in your YAML output):

{examples}

Two-step protocol (FEAT-017):

Step 1 — Reason briefly. Walk through which frequency this most resonates
with and why, then which phase, then which mode, orientation, and dosage,
and finally the voice register. Two to four short sentences total.

Step 2 — Emit your classification as valid YAML inside a fenced
```yaml ... ``` block. The YAML must follow this exact schema (do not
include any other keys; ``confidence_scores`` reports how certain you
are about each gated dimension, on a 0.0-1.0 scale):

```yaml
frequency:
  primary: F3
  secondary: [F5]
wavelength:
  phase: rising
  mode: express
  orientation: do
  dosage: medicine
voice:
  voice_register: analytical
  confidence: forming
confidence_scores:
  mode: 0.7
  orientation: 0.7
  dosage: 0.7
```

If a dimension is genuinely unclear, set ``unclassified`` rather than
guessing. For Mode / Orientation / Dosage we will treat any
``confidence_scores`` entry below {threshold:.2f} as ``unclassified``,
so calibrate honestly.

Fragment title: {title}
Fragment content:
{content}
"""
"""Prompt template for two-step LLM-based fragment classification (FEAT-017).

Placeholders:

- ``{examples}``: rendered few-shot block from :mod:`creek.classify.few_shot`.
- ``{threshold}``: ``LLMConfig.unclassified_threshold`` (decimal, two
  decimals) shown to the model so honesty about uncertainty is
  rewarded rather than penalised.
- ``{title}``, ``{content}``: the fragment being classified, after
  :func:`_sanitise_for_prompt` neutralises injection vectors.
"""

_DOSAGE_AMBIGUOUS_MARKERS: frozenset[str] = frozenset(
    {
        "ambiguous",
        "unclear",
        "mixed",
        "both",
        "uncertain",
    },
)
"""String values treated as ``Dosage.AMBIGUOUS``."""


_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"frequency", "wavelength", "voice", "confidence_scores"},
)
"""Top-level keys recognised in a documented LLM classification response.

Mirrors the sections in :data:`CLASSIFICATION_PROMPT`. ``confidence_scores``
was added by FEAT-017 to carry per-dimension self-reported confidence
for the default-unclassified bias on Mode / Orientation / Dosage.
Update both when adding a new section — ``validate_response`` will
otherwise reject the LLM's output as unexpected.
"""


_MAX_PROMPT_CONTENT_CHARS: int = 8192
"""Cap on fragment content length before injection into the prompt (~8 KiB)."""


_INJECTION_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("---", "[FENCE]"),
    ("<!--", "[CMT-OPEN]"),
    ("-->", "[CMT-CLOSE]"),
)
"""Substrings neutralised in fragment title/content before prompt formation.

The first entry replaces YAML document separators, which an attacker
could otherwise use to forge a second document the parser might
accept. The second/third entries replace HTML comment delimiters,
which can hide instructions from a casual reader while still being
visible to the LLM.
"""


def _sanitise_for_prompt(text: str) -> str:
    """Neutralise injection vectors in untrusted text before prompt formatting.

    Replaces YAML-document separators and HTML-comment delimiters with
    inert placeholders, then truncates the result to
    :data:`_MAX_PROMPT_CONTENT_CHARS` so an oversized fragment cannot
    push the model's response over its context window.

    Args:
        text: Raw, attacker-controlled fragment title or content.

    Returns:
        A sanitised, length-capped copy safe to ``str.format`` into the
        classification prompt template.
    """
    cleaned = text
    for needle, placeholder in _INJECTION_REPLACEMENTS:
        cleaned = cleaned.replace(needle, placeholder)
    if len(cleaned) > _MAX_PROMPT_CONTENT_CHARS:
        cleaned = cleaned[:_MAX_PROMPT_CONTENT_CHARS]
    return cleaned


_BIASED_DIMENSIONS: frozenset[str] = frozenset({"mode", "orientation", "dosage"})
"""Dimensions gated by ``LLMConfig.unclassified_threshold`` (FEAT-017).

Frequency, Phase, and Voice Register are not biased because they are
more stable signals empirically; using them is the point of having an
LLM pass at all.
"""

_YAML_FENCE_RE: re.Pattern[str] = re.compile(
    r"```(?:yaml)?\s*\n(.*?)```",
    flags=re.DOTALL,
)
"""Match a fenced YAML block; group 1 is the inner YAML text."""

_YAML_HEAD_RE: re.Pattern[str] = re.compile(
    r"^(frequency|wavelength|voice):", re.MULTILINE
)
"""Match the start of an unfenced YAML payload (a documented top-level key)."""


def _split_reasoning_and_yaml(response: str) -> tuple[str, str]:
    r"""Split a two-step LLM response into reasoning preamble and YAML payload.

    Tolerates three response shapes the model produces in practice:

    1. A fenced ``\`\`\`yaml ... \`\`\`\`` block preceded by reasoning prose.
    2. Reasoning prose followed by bare YAML starting at column 0.
    3. Pure YAML with no reasoning (backwards-compat with the
       pre-FEAT-017 prompt).

    Args:
        response: Raw LLM response text.

    Returns:
        ``(reasoning, yaml_text)``. ``reasoning`` is the stripped
        preamble, possibly empty. ``yaml_text`` is the YAML block,
        with code fences stripped if present.
    """
    fenced = _YAML_FENCE_RE.search(response)
    if fenced is not None:
        return response[: fenced.start()].strip(), fenced.group(1).strip()
    head = _YAML_HEAD_RE.search(response)
    if head is not None:
        return response[: head.start()].strip(), response[head.start() :].strip()
    return "", response.strip()


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


@dataclass
class BatchStats:
    """Aggregated statistics for a batch classification run.

    Attributes:
        total: Total number of fragments in the batch.
        classified: Fragments successfully classified.
        failed: Fragments that failed after all retries.
    """

    total: int = 0
    classified: int = 0
    failed: int = 0


def _parse_enum(
    value: object,
    enum_type: type[_EnumT],
    default: _EnumT,
) -> _EnumT:
    """Parse a value into a StrEnum member with a fallback default.

    Args:
        value: Raw value to parse (converted to string).
        enum_type: The StrEnum subclass to match against.
        default: Fallback value when no match is found.

    Returns:
        The matching enum member or the default.
    """
    if value is None:
        return default
    val_str = str(value).lower().strip()
    for member in enum_type:
        if member.value.lower() == val_str:
            return member
    return default


def _parse_optional_enum(
    value: object,
    enum_type: type[_EnumT],
) -> _EnumT | None:
    """Parse a value into an optional StrEnum member.

    Args:
        value: Raw value to parse (converted to string).
        enum_type: The StrEnum subclass to match against.

    Returns:
        The matching enum member or ``None``.
    """
    if value is None:
        return None
    val_str = str(value).lower().strip()
    for member in enum_type:
        if member.value.lower() == val_str:
            return member
    return None


def _parse_dosage(value: object) -> Dosage:
    """Parse a dosage value, treating ambiguous markers specially.

    Values like ``"unclear"``, ``"mixed"``, or ``"both"`` map to
    ``Dosage.AMBIGUOUS`` rather than ``Dosage.UNCLASSIFIED``.

    Args:
        value: Raw dosage value from the LLM response.

    Returns:
        The parsed ``Dosage`` enum member.
    """
    if value is None:
        return Dosage.UNCLASSIFIED
    val_str = str(value).lower().strip()
    if val_str in _DOSAGE_AMBIGUOUS_MARKERS:
        return Dosage.AMBIGUOUS
    return _parse_enum(val_str, Dosage, Dosage.UNCLASSIFIED)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from LLM output.

    Args:
        text: Raw text that may contain triple-backtick fences.

    Returns:
        The text with code fences removed.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    filtered = [line for line in lines if not line.strip().startswith("```")]
    return "\n".join(filtered)


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
        try:
            with httpx.Client(
                timeout=self.AVAILABILITY_TIMEOUT,
            ) as client:
                resp = client.get(
                    f"{self.config.ollama_url}/api/tags",
                )
                if resp.status_code == 200:
                    return True
        except httpx.HTTPError:
            pass
        logger.warning(
            "Ollama not available at %s",
            self.config.ollama_url,
        )
        return False

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

        The fragment title and content are attacker-controlled (they
        originate from third-party messages, scraped material, etc.).
        Each is passed through :func:`_sanitise_for_prompt` to neutralise
        YAML-fence and HTML-comment injection and to cap the body length
        before substitution — see SEC-004.

        A deterministic-per-fragment few-shot block is rendered from
        :mod:`creek.classify.few_shot` and substituted into the
        ``{examples}`` placeholder so the model sees worked examples
        before classifying the new fragment. The threshold value is
        shown to the model so it has an incentive to report honest
        per-dimension confidence rather than pad numbers up.

        Args:
            fragment: The fragment to classify.
            content: Optional content text for the fragment.

        Returns:
            The formatted prompt string.
        """
        safe_title = _sanitise_for_prompt(fragment.title)
        body = content or "(no content provided)"
        safe_content = _sanitise_for_prompt(body)
        samples = few_shot.sample_examples(fragment.id)
        examples_block = few_shot.render_block(samples) or "(no examples bundled)"
        return CLASSIFICATION_PROMPT.format(
            examples=examples_block,
            threshold=self.config.unclassified_threshold,
            title=safe_title,
            content=safe_content,
        )

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
        with httpx.Client(timeout=self.REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{self.config.ollama_url}/api/generate",
                json={
                    "model": self.config.model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("response", ""))

    def validate_response(self, response_text: str) -> dict[str, object]:
        """Parse and strictly validate a YAML response from the LLM.

        Strips markdown code fences, then parses the result with
        ``yaml.safe_load_all`` so a multi-document stream — a classic
        LLM-output-spoofing payload — is detected explicitly rather
        than silently swallowing the first document. Top-level keys
        outside the documented schema (``frequency``, ``wavelength``,
        ``voice``) are also rejected so a successful injection cannot
        smuggle in fields like ``privacy_tier`` (see SEC-004).

        Args:
            response_text: Raw YAML text from the LLM.

        Returns:
            Parsed dictionary with classification data.

        Raises:
            ValueError: If the text is not a single YAML dict, or
                contains undocumented top-level keys.
        """
        text = _strip_code_fences(response_text)
        docs = list(yaml.safe_load_all(text))
        if len(docs) > 1:
            msg = f"multi-document YAML response rejected ({len(docs)} documents)"
            raise ValueError(msg)
        parsed: object = docs[0] if docs else None
        if not isinstance(parsed, dict):
            msg = f"Expected YAML dict, got {type(parsed).__name__}"
            raise ValueError(msg)  # noqa: TRY004  # ValueError matches the documented schema-validation contract; callers catch ValueError.
        keys = {str(k) for k in parsed}
        extras = keys - _ALLOWED_TOP_LEVEL_KEYS
        if extras:
            msg = f"unexpected top-level keys in LLM response: {sorted(extras)}"
            raise ValueError(msg)
        return {str(k): v for k, v in parsed.items()}

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

        Uses a thread pool for concurrent provider requests, a tqdm bar
        for visual feedback, and rate limiting between submissions.
        Logs aggregated statistics on completion.

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

        stats = BatchStats(total=len(fragments))
        ordered: list[Fragment] = fragments.copy()

        with ThreadPoolExecutor(
            max_workers=self.config.max_concurrent,
        ) as executor:
            futures: dict[Future[Fragment], int] = {}
            for idx, frag in enumerate(fragments):
                future = executor.submit(self.classify, frag)
                futures[future] = idx
                if self.RATE_LIMIT_DELAY > 0 and idx < len(fragments) - 1:
                    time.sleep(self.RATE_LIMIT_DELAY)

            completed_futures = as_completed(futures)
            future_iter = (
                tqdm(
                    completed_futures,
                    total=len(fragments),
                    desc="Classifying",
                )
                if progress
                else completed_futures
            )

            for future in future_iter:
                idx = futures[future]
                self._collect_one_result(future, idx, ordered, stats)

        logger.info(
            "Batch complete: %d total, %d classified, %d failed",
            stats.total,
            stats.classified,
            stats.failed,
        )
        return ordered

    def _collect_one_result(
        self,
        future: Future[Fragment],
        idx: int,
        ordered: list[Fragment],
        stats: BatchStats,
    ) -> None:
        """Pull a future's result into ``ordered`` and update ``stats`` (OPS-003).

        Extracted from :meth:`classify_batch` so the per-result error
        path can include the failing fragment's ID and source path
        without inflating the parent function's complexity.
        """
        try:
            result = future.result()
            ordered[idx] = result
            if result.frequency.primary != Frequency.UNCLASSIFIED:
                stats.classified += 1
        except Exception:
            failing = ordered[idx]
            logger.exception(
                "[fragment=%s path=%s provider=%s] "
                "Unexpected error classifying fragment %d",
                failing.id,
                failing.source.original_file or "<inline>",
                self.config.provider,
                idx,
            )
            stats.failed += 1


# ---- Private helpers for _apply_classification ----


def _apply_frequency(
    data: dict[str, object],
    updates: dict[str, object],
) -> None:
    """Extract frequency classification from parsed data.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with frequency updates.
    """
    freq_data = data.get("frequency")
    if not isinstance(freq_data, dict):
        return
    primary = _parse_enum(
        freq_data.get("primary"),
        Frequency,
        Frequency.UNCLASSIFIED,
    )
    raw_secondary = freq_data.get("secondary")
    secondary: list[Frequency] = []
    if isinstance(raw_secondary, list):
        for item in raw_secondary:
            parsed = _parse_optional_enum(item, Frequency)
            if parsed is not None and parsed != Frequency.UNCLASSIFIED:
                secondary.append(parsed)
    updates["frequency"] = FrequencyClassification(
        primary=primary,
        secondary=secondary,
    )


def _confidence_score(scores: object, dimension: str) -> float | None:
    """Return the per-dimension confidence value from a ``confidence_scores`` map.

    Args:
        scores: The ``confidence_scores`` value from the parsed YAML
            (any type — the LLM may emit garbage).
        dimension: ``"mode"``, ``"orientation"``, or ``"dosage"``.

    Returns:
        The float in ``[0.0, 1.0]`` when reported and parseable, else
        ``None`` (treated by the bias as "no score reported — keep
        the model's pick").
    """
    if not isinstance(scores, dict):
        return None
    value = scores.get(dimension)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _apply_wavelength(
    data: dict[str, object],
    updates: dict[str, object],
    *,
    unclassified_threshold: float,
) -> None:
    """Extract wavelength classification from parsed data with FEAT-017 bias.

    Mode, Orientation, and Dosage are downgraded to ``unclassified``
    when the model's self-reported per-dimension confidence (read from
    ``data['confidence_scores'][dimension]``) is below
    ``unclassified_threshold``. A missing or unparseable score leaves
    the model's pick intact — the bias only fires when the model
    explicitly reports low confidence.

    Phase is **not** gated by the bias (FEAT-017 calls phase a stable
    signal; gating it would be more cost than benefit).

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with wavelength updates.
        unclassified_threshold: Minimum confidence to keep the model's
            pick for Mode / Orientation / Dosage; below this the
            dimension defaults to ``unclassified``.
    """
    wave_data = data.get("wavelength")
    if not isinstance(wave_data, dict):
        return
    scores = data.get("confidence_scores")
    updates["wavelength"] = WavelengthClassification(
        phase=_parse_enum(wave_data.get("phase"), Phase, Phase.UNCLASSIFIED),
        mode=_biased_enum(
            wave_data.get("mode"),
            Mode,
            Mode.UNCLASSIFIED,
            score=_confidence_score(scores, "mode"),
            threshold=unclassified_threshold,
        ),
        orientation=_biased_enum(
            wave_data.get("orientation"),
            Orientation,
            Orientation.UNCLASSIFIED,
            score=_confidence_score(scores, "orientation"),
            threshold=unclassified_threshold,
        ),
        dosage=_biased_dosage(
            wave_data.get("dosage"),
            score=_confidence_score(scores, "dosage"),
            threshold=unclassified_threshold,
        ),
    )


def _biased_enum(
    value: object,
    enum_type: type[_EnumT],
    default: _EnumT,
    *,
    score: float | None,
    threshold: float,
) -> _EnumT:
    """Apply :data:`_BIASED_DIMENSIONS` gating to a regular enum parse.

    A reported confidence strictly below the threshold overrides the
    model's pick with ``default`` (always the ``unclassified`` member
    of the enum). When ``score`` is ``None`` (no value reported), the
    bias does not fire — the model's pick stands.
    """
    if score is not None and score < threshold:
        return default
    return _parse_enum(value, enum_type, default)


def _biased_dosage(
    value: object,
    *,
    score: float | None,
    threshold: float,
) -> Dosage:
    """Apply :data:`_BIASED_DIMENSIONS` gating to dosage parsing.

    A separate helper because dosage uses :func:`_parse_dosage` (which
    treats ambiguity markers specially) rather than the plain
    :func:`_parse_enum` used by mode/orientation.
    """
    if score is not None and score < threshold:
        return Dosage.UNCLASSIFIED
    return _parse_dosage(value)


def _apply_voice(
    data: dict[str, object],
    updates: dict[str, object],
) -> None:
    """Extract voice classification from parsed data.

    Args:
        data: Parsed LLM response.
        updates: Dict to populate with voice updates.
    """
    voice_data = data.get("voice")
    if not isinstance(voice_data, dict):
        return
    updates["voice"] = VoiceClassification(
        voice_register=_parse_optional_enum(
            voice_data.get("voice_register"),
            VoiceRegister,
        ),
        confidence=_parse_optional_enum(
            voice_data.get("confidence"),
            Confidence,
        ),
    )
