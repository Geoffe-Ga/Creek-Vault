"""Privacy tier enforcement — assign and apply Open/Personal/Intimate handling.

Section 13.2 of the Creek Ontology defines three privacy tiers:

* ``PUBLIC`` — published essays and named guild/channel posts. No
  restrictions; voice proxy eligible.
* ``PERSONAL`` — chatbot conversations, Discord DMs and unclassified
  channels. Auto-process the bulk; flag genuinely sensitive content.
* ``INTIMATE`` — journal entries, recovery-related text, and
  high-confidence confessional voice. Always reviewed by a human and
  excluded from voice proxy generation.

When in doubt the classifier returns :class:`PrivacyTier.PERSONAL`
rather than :class:`PrivacyTier.PUBLIC` — leaking content the user
considered private is far worse than the inverse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.models import (
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
        "meeting",
        "step work",
        "amends",
        "inventory",
    },
)
"""Keywords that flag a fragment as recovery-related and therefore INTIMATE.

The list intentionally errs on the side of high-recall: false positives
land in INTIMATE (extra review), false negatives risk publishing
something the user considered private. See ontology §13.2.
"""


_PUBLIC_PLATFORMS: frozenset[SourcePlatform] = frozenset(
    {SourcePlatform.ESSAY},
)

_CHATBOT_PLATFORMS: frozenset[SourcePlatform] = frozenset(
    {SourcePlatform.CLAUDE, SourcePlatform.CHATGPT},
)

_PRIVATE_DISCORD_HINTS: frozenset[str] = frozenset({"dm", "private"})


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

        1. Recovery keyword in title or body → :class:`PrivacyTier.INTIMATE`.
        2. Journal source → :class:`PrivacyTier.INTIMATE`.
        3. Confessional register with conviction → ``INTIMATE``.
        4. Essay source → :class:`PrivacyTier.PUBLIC`.
        5. Discord with a non-DM channel name → ``PUBLIC``.
        6. Chatbot or unclassified-channel Discord → ``PERSONAL``.
        7. Anything else → ``PERSONAL`` (safer default).

        Args:
            fragment: The fragment to classify.
            content: Optional body text scanned for recovery keywords.

        Returns:
            The assigned :class:`PrivacyTier`.
        """
        if self._has_recovery_content(fragment, content):
            return PrivacyTier.INTIMATE
        platform = SourcePlatform(fragment.source.platform)
        if platform == SourcePlatform.JOURNAL:
            return PrivacyTier.INTIMATE
        if self._is_high_confidence_confessional(fragment):
            return PrivacyTier.INTIMATE
        if platform in _PUBLIC_PLATFORMS:
            return PrivacyTier.PUBLIC
        if platform == SourcePlatform.DISCORD:
            return self._classify_discord(fragment)
        if platform in _CHATBOT_PLATFORMS:
            return PrivacyTier.PERSONAL
        return PrivacyTier.PERSONAL

    def enforce_tier(
        self,
        fragment: Fragment,
        tier: PrivacyTier,
    ) -> Fragment:
        """Apply *tier*-specific handling rules and return a new fragment.

        * ``INTIMATE`` sets :attr:`Fragment.voice_proxy_eligible` to ``False``.
          Combined with the review queue's INTIMATE check, this means
          intimate fragments are both excluded from voice proxy
          generation and surfaced for human review.
        * ``PUBLIC`` and ``PERSONAL`` keep voice-proxy eligibility.

        Args:
            fragment: The fragment to update. Not mutated.
            tier: The privacy tier to apply.

        Returns:
            A new :class:`Fragment` with ``privacy_tier`` and (when
            relevant) ``voice_proxy_eligible`` updated.
        """
        updates: dict[str, object] = {"privacy_tier": tier}
        if tier == PrivacyTier.INTIMATE:
            updates["voice_proxy_eligible"] = False
        return fragment.model_copy(update=updates)

    def classify_and_enforce(
        self,
        fragment: Fragment,
        content: str = "",
    ) -> Fragment:
        """Classify *fragment* and immediately enforce the resulting tier.

        Args:
            fragment: The fragment to classify and update.
            content: Optional body text passed through to
                :meth:`classify_tier`.

        Returns:
            A new :class:`Fragment` with the assigned tier and any
            tier-specific field changes.
        """
        tier = self.classify_tier(fragment, content=content)
        return self.enforce_tier(fragment, tier)

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
        """Map a Discord fragment to PUBLIC / PERSONAL by channel hint."""
        channel = (fragment.source.channel or "").lower()
        if not channel:
            return PrivacyTier.PERSONAL
        if any(hint in channel for hint in _PRIVATE_DISCORD_HINTS):
            return PrivacyTier.PERSONAL
        return PrivacyTier.PUBLIC
