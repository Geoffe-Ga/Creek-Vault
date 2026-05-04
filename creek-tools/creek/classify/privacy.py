"""Privacy tier enforcement — assign and apply Open/Personal/Intimate handling.

Section 13.2 of the Creek Ontology defines three privacy tiers:

* ``PUBLIC`` — published essays and named guild/channel posts. No
  restrictions; voice proxy eligible.
* ``PERSONAL`` — chatbot conversations, Discord DMs and unclassified
  channels. Auto-process the bulk; flag genuinely sensitive content.
* ``INTIMATE`` — journal entries, recovery-related text, and
  high-confidence confessional voice. Always reviewed by a human and
  excluded from voice proxy generation. Per the
  :class:`~creek.models.PrivacyTier` docstring, ``INTIMATE`` is
  reserved for self-authored content; AI-authored fragments
  (``Authorship.AI`` etc.) are never silently elevated to ``INTIMATE``
  by content signals.

When in doubt the classifier returns :class:`PrivacyTier.PERSONAL`
rather than :class:`PrivacyTier.OPEN` — leaking content the user
considered private is far worse than the inverse.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from creek.models import (
    Authorship,
    Confidence,
    PrivacyTier,
    SourcePlatform,
    VoiceRegister,
)

if TYPE_CHECKING:
    from creek.models import Fragment


RECOVERY_KEYWORDS: frozenset[str] = frozenset(
    {
        "recovery",
        "sobriety",
        "relapse",
        "sponsor",
        "dharma",
        "step work",
        "amends",
        "aa meeting",
        "na meeting",
        "recovery meeting",
        "moral inventory",
        "step inventory",
    },
)
"""Keywords that flag a fragment as recovery-related and therefore INTIMATE.

The list intentionally errs on the side of high-recall: false positives
land in INTIMATE (extra review), false negatives risk publishing
something the user considered private. See ontology §13.2.

The bare words ``meeting`` and ``inventory`` are too common in
non-recovery contexts (work meetings, stock inventories) to use
unscoped, so this set scopes them via the canonical recovery phrases
``aa meeting``, ``na meeting``, ``recovery meeting``,
``moral inventory`` and ``step inventory``.
"""


_PRIVATE_DISCORD_TOKENS: frozenset[str] = frozenset({"dm", "private"})
"""Channel-name tokens that mark a Discord fragment as PERSONAL.

Matched against tokens (split on common separators) rather than as
substrings so that channels like ``#admin`` are not falsely flagged by
the substring ``dm``.
"""

_CHANNEL_TOKEN_SPLIT: re.Pattern[str] = re.compile(r"[-_\s/]+")


class PrivacyClassifier:
    """Assigns a :class:`PrivacyTier` and enforces tier-specific handling.

    Attributes:
        recovery_keywords: Keyword set scanned in title + body to detect
            recovery content. Defaults to :data:`RECOVERY_KEYWORDS`.
    """

    def __init__(
        self,
        *,
        recovery_keywords: frozenset[str] | None = None,
    ) -> None:
        """Initialise the classifier.

        Args:
            recovery_keywords: Optional override for the default recovery
                keyword set. Useful for tests and for users with their
                own privacy vocabulary.
        """
        self.recovery_keywords = (
            recovery_keywords if recovery_keywords is not None else RECOVERY_KEYWORDS
        )

    def classify_tier(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> PrivacyTier:
        """Return the privacy tier for *fragment*.

        Order of checks (first match wins):

        1. Self-authored + recovery keyword in title or body → ``INTIMATE``.
        2. Self-authored journal source → ``INTIMATE``.
        3. Self-authored confessional register with conviction → ``INTIMATE``.
        4. Essay source → :class:`PrivacyTier.OPEN`.
        5. Discord with a non-DM channel name → ``PUBLIC``.
        6. Anything else (chatbots, DMs, unmapped sources, AI-authored
           content with sensitive keywords) → ``PERSONAL``.

        ``INTIMATE`` is gated on :class:`Authorship.SELF` per the
        :class:`~creek.models.PrivacyTier` model docstring; AI- or
        other-authored fragments cannot be elevated to ``INTIMATE`` via
        content signals.

        Args:
            fragment: The fragment to classify.
            content: Optional body text scanned for recovery keywords.
                Callers must pass the raw body explicitly — the
                ``Fragment`` model does not carry the body text.

        Returns:
            The assigned :class:`PrivacyTier`.
        """
        platform = SourcePlatform(fragment.source.platform)
        if self._is_self_authored(fragment):
            if self._has_recovery_content(fragment, content):
                return PrivacyTier.INTIMATE
            if platform == SourcePlatform.JOURNAL:
                return PrivacyTier.INTIMATE
            if self._is_high_confidence_confessional(fragment):
                return PrivacyTier.INTIMATE
        if platform == SourcePlatform.ESSAY:
            return PrivacyTier.OPEN
        if platform == SourcePlatform.DISCORD:
            return self._classify_discord(fragment)
        # Chatbots, DMs, unmapped sources, AI-authored sensitive content
        # → PERSONAL. Leaking content is worse than over-restricting.
        return PrivacyTier.PERSONAL

    def enforce_tier(
        self,
        fragment: Fragment,
        tier: PrivacyTier,
    ) -> Fragment:
        """Apply *tier*-specific handling rules and return a new fragment.

        The resulting fragment carries ``privacy_tier`` set to *tier*.
        ``voice_proxy_eligible`` is a derived property
        (:class:`Fragment.voice_proxy_eligible`, BUG-009) computed from
        ``privacy_tier`` and ``source.author``, so re-classification
        between tiers automatically flips eligibility without requiring
        a paired write.

        Args:
            fragment: The fragment to update. Not mutated.
            tier: The privacy tier to apply.

        Returns:
            A new :class:`Fragment` with ``privacy_tier`` set to *tier*.
        """
        return fragment.model_copy(update={"privacy_tier": tier})

    def classify_and_enforce(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> Fragment:
        """Classify *fragment* and immediately enforce the resulting tier.

        Args:
            fragment: The fragment to classify and update.
            content: Optional body text passed through to
                :meth:`classify_tier`. Callers must pass the raw body
                explicitly to enable recovery-keyword detection.

        Returns:
            A new :class:`Fragment` with the assigned tier and any
            tier-specific field changes.
        """
        tier = self.classify_tier(fragment, content=content)
        return self.enforce_tier(fragment, tier)

    @staticmethod
    def _is_self_authored(fragment: Fragment) -> bool:
        """Return ``True`` when the fragment carries the user's own words."""
        return Authorship(fragment.source.author) == Authorship.SELF

    def _has_recovery_content(self, fragment: Fragment, content: str) -> bool:
        """Return ``True`` if any recovery keyword appears in title or body."""
        haystack = f"{fragment.title}\n{content}".lower()
        return any(keyword in haystack for keyword in self.recovery_keywords)

    @staticmethod
    def _is_high_confidence_confessional(fragment: Fragment) -> bool:
        """Confessional voice with conviction signals INTIMATE content."""
        register = fragment.voice.voice_register
        confidence = fragment.voice.confidence
        if register is None or confidence is None:
            return False
        return (
            VoiceRegister(register) == VoiceRegister.CONFESSIONAL
            and Confidence(confidence) == Confidence.CONVICTION
        )

    @staticmethod
    def _classify_discord(fragment: Fragment) -> PrivacyTier:
        """Map a Discord fragment to PUBLIC / PERSONAL by channel hint.

        The channel name is split into tokens on common separators
        (``-``, ``_``, whitespace, ``/``) and each token is checked for
        exact membership in :data:`_PRIVATE_DISCORD_TOKENS`. Token-level
        matching prevents substrings like ``dm`` from triggering on
        unrelated channel names like ``admin`` or ``stream``.
        """
        channel = (fragment.source.channel or "").strip().lower()
        if not channel:
            return PrivacyTier.PERSONAL
        tokens = {token for token in _CHANNEL_TOKEN_SPLIT.split(channel) if token}
        if tokens & _PRIVATE_DISCORD_TOKENS:
            return PrivacyTier.PERSONAL
        return PrivacyTier.OPEN
