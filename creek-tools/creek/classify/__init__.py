"""Creek classification pipeline — rules, LLM providers, privacy, and review queue.

This package provides the classification subsystem for the Creek pipeline:

- **RuleClassifier**: Keyword/pattern-based frequency, phase, and mode classification.
- **LLMClassifier**: LLM-powered classification via Ollama or Anthropic.
- **AnthropicProvider**: Opt-in cloud classification provider (Anthropic API).
- **PrivacyClassifier**: Assigns Open/Personal/Intimate tier and enforces handling.
- **ReviewQueueGenerator**: Generates a markdown review queue for uncertain fragments.
"""

from creek.classify.llm import AnthropicProvider, LLMClassifier
from creek.classify.privacy import RECOVERY_KEYWORDS, PrivacyClassifier
from creek.classify.review import ReviewQueueGenerator
from creek.classify.rules import RuleClassifier

__all__ = [
    "RECOVERY_KEYWORDS",
    "AnthropicProvider",
    "LLMClassifier",
    "PrivacyClassifier",
    "ReviewQueueGenerator",
    "RuleClassifier",
]
