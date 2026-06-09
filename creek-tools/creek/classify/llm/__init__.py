"""LLM-based classification for Creek fragments via Ollama or Anthropic.

Provides an :class:`LLMClassifier` that delegates fragment classification
to either a local Ollama instance (default) or the Anthropic cloud API
(opt-in).  Falls back gracefully when the configured provider is
unavailable, returning fragments unchanged.

The Anthropic provider requires two environment variables to be set:

- ``ANTHROPIC_API_KEY``: The API key is **never** stored in the
  configuration file, logs, or error messages.
- ``CREEK_ANTHROPIC_CONSENT``: Must be ``"1"`` (or ``"true"`` / ``"yes"``)
  to acknowledge that fragment content will be sent to Anthropic's
  servers.

REFACTOR-001 split this previously-monolithic module into focused
submodules — :mod:`prompts`, :mod:`providers`, :mod:`parsing`,
:mod:`calibration`, :mod:`batch`, and :mod:`orchestrator` — without
changing any observable behaviour. This package's ``__init__`` is a
thin re-export shim so existing imports
(``from creek.classify.llm import …``) keep working.

The underscore-prefixed names below (``_BIASED_DIMENSIONS``,
``_parse_dosage`` etc.) are re-exported solely so that pre-refactor
tests can keep monkey-patching them on the package namespace; they are
deliberately absent from :data:`__all__` and are not part of the public
API.
"""

from __future__ import annotations

import time as time

# Re-imported at package level so tests can patch
# ``creek.classify.llm.httpx.Client`` and ``creek.classify.llm.time.sleep``;
# the orchestrator and providers reference the same module objects at
# call time, so the patches reach the inner code paths.
import httpx as httpx

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.batch import BatchStats
from creek.classify.llm.calibration import _BIASED_DIMENSIONS as _BIASED_DIMENSIONS
from creek.classify.llm.completion import (
    AnthropicCompletion as AnthropicCompletion,
)
from creek.classify.llm.completion import (
    Completion as Completion,
)
from creek.classify.llm.consent import (
    cloud_warning as cloud_warning,
)
from creek.classify.llm.consent import (
    has_cloud_consent as has_cloud_consent,
)
from creek.classify.llm.consent import (
    require_cloud_consent as require_cloud_consent,
)
from creek.classify.llm.orchestrator import (
    LLMClassificationResult,
    LLMClassifier,
)
from creek.classify.llm.parsing import _parse_dosage as _parse_dosage
from creek.classify.llm.parsing import _parse_enum as _parse_enum
from creek.classify.llm.parsing import _parse_optional_enum as _parse_optional_enum
from creek.classify.llm.parsing import (
    _split_reasoning_and_yaml as _split_reasoning_and_yaml,
)
from creek.classify.llm.parsing import _strip_code_fences as _strip_code_fences
from creek.classify.llm.prompts import _COLOR_BLOCK as _COLOR_BLOCK
from creek.classify.llm.prompts import _FREQUENCY_BLOCK as _FREQUENCY_BLOCK
from creek.classify.llm.prompts import (
    _FREQUENCY_COLOR_BLOCK as _FREQUENCY_COLOR_BLOCK,
)
from creek.classify.llm.prompts import CLASSIFICATION_PROMPT
from creek.classify.llm.prompts import _sanitise_for_prompt as _sanitise_for_prompt
from creek.classify.llm.providers import (
    ANTHROPIC_CLOUD_WARNING,
    AnthropicProvider,
    OllamaProvider,
    OpenAIProvider,
    build_provider,
    provider_is_cloud,
)

__all__ = [
    "ANTHROPIC_CLOUD_WARNING",
    "CLASSIFICATION_PROMPT",
    "AnthropicCompletion",
    "AnthropicProvider",
    "BatchStats",
    "Completion",
    "LLMClassificationResult",
    "LLMClassifier",
    "LLMProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "build_provider",
    "cloud_warning",
    "has_cloud_consent",
    "provider_is_cloud",
    "require_cloud_consent",
]
