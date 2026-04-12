"""Creek classification pipeline — rules, LLM providers, and review queue.

This package provides the classification subsystem for the Creek pipeline:

- **RuleClassifier**: Keyword/pattern-based frequency, phase, and mode classification.
- **LLMClassifier**: LLM-powered classification via Ollama or Anthropic.
- **AnthropicProvider**: Opt-in cloud classification provider (Anthropic API).
- **ReviewQueueGenerator**: Generates a markdown review queue for uncertain fragments.
"""

from creek.classify.llm import AnthropicProvider, LLMClassifier
from creek.classify.review import ReviewQueueGenerator
from creek.classify.rules import RuleClassifier

__all__ = [
    "AnthropicProvider",
    "LLMClassifier",
    "ReviewQueueGenerator",
    "RuleClassifier",
]
