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
"""

from __future__ import annotations

import time as time

# Re-imported at package level so tests can patch
# ``creek.classify.llm.httpx.Client`` and ``creek.classify.llm.time.sleep``;
# the orchestrator and providers reference the same module objects at
# call time, so the patches reach the inner code paths.
import httpx as httpx

from creek.classify.llm.batch import BatchStats
from creek.classify.llm.calibration import _BIASED_DIMENSIONS
from creek.classify.llm.orchestrator import (
    LLMClassificationResult,
    LLMClassifier,
)
from creek.classify.llm.parsing import (
    _parse_dosage,
    _parse_enum,
    _parse_optional_enum,
    _split_reasoning_and_yaml,
    _strip_code_fences,
)
from creek.classify.llm.prompts import CLASSIFICATION_PROMPT
from creek.classify.llm.providers import ANTHROPIC_CLOUD_WARNING, AnthropicProvider

__all__ = [
    "ANTHROPIC_CLOUD_WARNING",
    "CLASSIFICATION_PROMPT",
    "_BIASED_DIMENSIONS",
    "AnthropicProvider",
    "BatchStats",
    "LLMClassificationResult",
    "LLMClassifier",
    "_parse_dosage",
    "_parse_enum",
    "_parse_optional_enum",
    "_split_reasoning_and_yaml",
    "_strip_code_fences",
]
