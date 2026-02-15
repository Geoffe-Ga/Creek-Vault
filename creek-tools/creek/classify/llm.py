"""LLM-based classification stub for Creek fragments.

Provides a stub classifier that will eventually call an LLM provider
(Ollama, Anthropic, or OpenAI) to classify fragments along frequency,
wavelength, and voice dimensions. Currently returns fragments unchanged
and logs what it would do.
"""

import logging

from creek.config import LLMConfig
from creek.models import Fragment

logger = logging.getLogger(__name__)

CLASSIFICATION_PROMPT: str = """\
You are a classification assistant for the Creek knowledge organization system.

Given a fragment of content, classify it along the following dimensions:

1. **Frequency** (APTITUDE F1-F10): Which frequency best describes the content?
   - F1: Survival/Safety, F2: Belonging/Tribe, F3: Power/Agency,
   - F4: Order/Structure, F5: Achievement/Strategy, F6: Community/Empathy,
   - F7: Systems/Integration, F8: Holistic/Ecology, F9: Witness/Being,
   - F10: Unity/Non-dual

2. **Wavelength Phase**: What phase is the content in?
   - Rising, Peaking, Withdrawal, Diminishing, Bottoming Out, Restoration

3. **Engagement Mode**: How is the frequency being experienced?
   - Inhabit, Express, Collaborate, Integrate, Absorb

4. **Voice Register**: What tone does the content use?
   - Confessional, Analytical, Playful, Prophetic, Instructional, Raw, Conversational

Respond with a JSON object containing your classifications and confidence level.

Fragment title: {title}
Fragment content:
{content}
"""
"""Prompt template for LLM-based fragment classification."""


class LLMClassifier:
    """Stub LLM classifier for Creek fragments.

    This is a placeholder that will eventually call an LLM provider
    to classify fragments. Currently returns fragments unchanged and
    logs a message indicating stub behavior.

    Attributes:
        config: The LLM configuration specifying provider, model, etc.
    """

    def __init__(self, config: LLMConfig) -> None:
        """Initialize the LLM classifier with configuration.

        Args:
            config: LLM provider configuration (provider, model, etc.).
        """
        self.config = config

    def classify(self, fragment: Fragment) -> Fragment:
        """Classify a fragment using an LLM (stub — returns fragment unchanged).

        In the future, this will send the fragment content to the configured
        LLM provider for classification. Currently logs a message and returns
        the fragment unchanged.

        Args:
            fragment: The fragment to classify.

        Returns:
            The same fragment, unchanged.
        """
        logger.info(
            "LLM classification stub: would classify fragment '%s' "
            "using %s/%s (not yet implemented)",
            fragment.title,
            self.config.provider,
            self.config.model,
        )
        return fragment

    def classify_batch(self, fragments: list[Fragment]) -> list[Fragment]:
        """Classify a batch of fragments using an LLM (stub — returns unchanged).

        In the future, this will send fragments in batches to the LLM
        for classification. Currently logs a message and returns all
        fragments unchanged.

        Args:
            fragments: List of fragments to classify.

        Returns:
            The same list of fragments, unchanged.
        """
        logger.info(
            "LLM batch classification stub: would classify %d fragments "
            "using %s/%s (batch_size=%d, not yet implemented)",
            len(fragments),
            self.config.provider,
            self.config.model,
            self.config.batch_size,
        )
        return fragments
