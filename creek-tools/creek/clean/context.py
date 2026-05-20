"""Non-user content handling — context extraction and filtering.

Implements three configurable modes for handling content authored by
others (e.g. Discord messages, collaborative documents):

- **context_metadata**: Store others' content as ``context`` entries in
  the nearest user fragment's frontmatter; do not index separately.
- **low_priority**: Ingest as separate fragments with ``author: other``
  and reduced quality indicators; exclude from voice proxy.
- **skip**: Drop others' content entirely; preserve only user's words.

Universal constraints enforced across all modes:

- Others' content is **never** used for voice proxy generation.
- Others' content is **never** classified as ``intimate`` privacy tier.
- Attribution is **always** preserved.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from pydantic import BaseModel, Field

from creek.config import ContextConfig
from creek.models import Authorship, Fragment, PrivacyTier

logger = logging.getLogger(__name__)

# Authorships that represent non-user content
_NON_USER_AUTHORS: frozenset[str] = frozenset({Authorship.OTHER, Authorship.AI})


class ContextResult(BaseModel):
    """Result of context extraction.

    Attributes:
        kept: Fragments retained after context processing.
        dropped: Fragments removed during context processing.
        context_entries: Number of context entries added to kept fragments.
    """

    kept: list[Fragment] = Field(default_factory=list)
    dropped: list[Fragment] = Field(default_factory=list)
    context_entries: int = 0


class ContextExtractor:
    """Extract and handle non-user content across three configurable modes.

    Attributes:
        mode: The active content handling mode.
        quality_penalty: Multiplier for quality reduction in low_priority mode.
    """

    def __init__(self, config: ContextConfig | None = None) -> None:
        """Initialise the extractor with optional configuration.

        Args:
            config: Content handling configuration.  Defaults to
                :class:`~creek.config.ContextConfig` with default values.
        """
        resolved = config or ContextConfig()
        self.mode: str = resolved.mode
        self.quality_penalty: float = resolved.quality_penalty

    def extract(self, fragments: list[Fragment]) -> ContextResult:
        """Process fragments according to the configured handling mode.

        Applies universal constraints (voice proxy exclusion, privacy
        tier restrictions, attribution preservation) regardless of mode,
        then delegates to the mode-specific handler.

        Args:
            fragments: Input fragments with authorship already tagged.

        Returns:
            A :class:`ContextResult` with kept, dropped, and context
            entry counts.
        """
        if not fragments:
            return ContextResult()

        if self.mode == "context_metadata":
            return self._extract_context_metadata(fragments)
        if self.mode == "low_priority":
            return self._extract_low_priority(fragments)
        return self._extract_skip(fragments)

    # ---- Mode handlers ----

    def _extract_context_metadata(
        self,
        fragments: list[Fragment],
    ) -> ContextResult:
        """Handle fragments in context_metadata mode.

        Others' content is stored as context entries on the next
        self-authored fragment in the same conversation.  Fragments
        without a conversation_id or without a matching self-fragment
        are dropped.

        Args:
            fragments: Input fragments.

        Returns:
            Processed :class:`ContextResult`.
        """
        kept: list[Fragment] = []
        dropped: list[Fragment] = []
        context_entries = 0

        # Group fragments by conversation_id for context linking
        conv_groups: dict[str | None, list[Fragment]] = defaultdict(list)
        for frag in fragments:
            conv_groups[frag.source.conversation_id].append(frag)

        # Process each conversation group
        for conv_id, group in conv_groups.items():
            pending_context: list[str] = []

            for frag in group:
                if frag.source.author in _NON_USER_AUTHORS:
                    # Non-user content: build context entry if in conversation
                    if conv_id is not None:
                        entry = _build_context_entry(frag)
                        pending_context.append(entry)
                    dropped.append(frag)

                elif frag.source.author == Authorship.COLLABORATIVE:
                    # Collaborative: keep but mark voice-proxy ineligible
                    updated = _enforce_constraints(frag)
                    kept.append(updated)

                else:
                    # Self-authored: attach any pending context
                    updated = frag.model_copy(deep=True)
                    if pending_context:
                        updated.context = pending_context.copy()
                        context_entries += len(pending_context)
                        pending_context.clear()
                    kept.append(updated)

        return ContextResult(
            kept=kept,
            dropped=dropped,
            context_entries=context_entries,
        )

    def _extract_low_priority(
        self,
        fragments: list[Fragment],
    ) -> ContextResult:
        """Handle fragments in low_priority mode.

        Non-user fragments are kept but marked as voice-proxy ineligible,
        tagged as ``low-priority``, and have privacy constraints enforced.

        Args:
            fragments: Input fragments.

        Returns:
            Processed :class:`ContextResult`.
        """
        kept: list[Fragment] = []

        for frag in fragments:
            if frag.source.author == Authorship.SELF:
                kept.append(frag.model_copy(deep=True))
            elif frag.source.author == Authorship.COLLABORATIVE:
                updated = _enforce_constraints(frag)
                kept.append(updated)
            else:
                # Other or AI: keep with reduced priority
                updated = _enforce_constraints(frag)
                if "low-priority" not in updated.tags:
                    updated.tags.append("low-priority")
                kept.append(updated)

        return ContextResult(kept=kept, dropped=[], context_entries=0)

    def _extract_skip(self, fragments: list[Fragment]) -> ContextResult:
        """Handle fragments in skip mode.

        Non-user content is dropped entirely.  Collaborative fragments
        are kept but marked voice-proxy ineligible.

        Args:
            fragments: Input fragments.

        Returns:
            Processed :class:`ContextResult`.
        """
        kept: list[Fragment] = []
        dropped: list[Fragment] = []

        for frag in fragments:
            if frag.source.author == Authorship.SELF:
                kept.append(frag.model_copy(deep=True))
            elif frag.source.author == Authorship.COLLABORATIVE:
                updated = _enforce_constraints(frag)
                kept.append(updated)
            else:
                dropped.append(frag)

        return ContextResult(kept=kept, dropped=dropped, context_entries=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enforce_constraints(fragment: Fragment) -> Fragment:
    """Apply universal constraints to a non-self fragment.

    - Downgrades ``intimate`` privacy tier to ``personal``.
    - ``voice_proxy_eligible`` is a derived property (BUG-009): non-self
      fragments are excluded automatically because the property requires
      ``source.author == Authorship.SELF``. No explicit write needed.

    Args:
        fragment: The fragment to constrain.

    Returns:
        A deep copy of the fragment with constraints applied.
    """
    updated = fragment.model_copy(deep=True)

    if updated.privacy_tier == PrivacyTier.INTIMATE:
        updated.privacy_tier = PrivacyTier.PERSONAL

    return updated


def _build_context_entry(fragment: Fragment) -> str:
    """Build a context string from a non-user fragment.

    The format preserves attribution by including the author type
    and the fragment title.

    Args:
        fragment: The non-user fragment to summarise.

    Returns:
        A human-readable context entry string.
    """
    author = fragment.source.author
    return f"[{author}] {fragment.title}"
