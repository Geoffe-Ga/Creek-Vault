"""Authorship tagger — classify fragment authors by source platform.

Determines who authored a fragment's content based on the source platform
and contextual metadata (interlocutor role, Discord username). Returns an
:class:`~creek.models.Authorship` tag or ``None`` for ambiguous cases.

Inference rules:
- **Claude / ChatGPT**: ``human`` / ``user`` turns are ``self``;
  ``assistant`` turns are ``ai``; unknown roles return ``None``.
- **Discord**: Messages matching the configured Discord username are
  ``self``; others are ``other``; missing username returns ``None``.
- **Journal / Essay / Code**: Always ``self`` (personal writing).
- **Email / Image OCR / Other**: ``None`` (ambiguous, needs review).
"""

from __future__ import annotations

from creek.models import Authorship, Fragment, SourcePlatform

# Interlocutor values that indicate the user's own turns in AI chats
_HUMAN_ROLES: frozenset[str] = frozenset({"human", "user"})

# Interlocutor values that indicate the AI assistant's turns
_ASSISTANT_ROLES: frozenset[str] = frozenset({"assistant"})

# Platforms where content is always authored by the user
_SELF_PLATFORMS: frozenset[str] = frozenset(
    {
        SourcePlatform.JOURNAL,
        SourcePlatform.ESSAY,
        SourcePlatform.CODE,
    }
)

# Platforms that use AI-chat role-based inference
_CHAT_PLATFORMS: frozenset[str] = frozenset(
    {
        SourcePlatform.CLAUDE,
        SourcePlatform.CHATGPT,
    }
)


class AuthorshipTagger:
    """Tag fragments with authorship based on source platform and context.

    A stateless tagger that examines a fragment's source platform and
    interlocutor metadata to determine who authored the content. Use the
    :meth:`tag` method to classify individual fragments.
    """

    def tag(
        self,
        fragment: Fragment,
        *,
        discord_username: str | None = None,
    ) -> Authorship | None:
        """Determine the authorship of a fragment.

        Args:
            fragment: The fragment to classify.
            discord_username: The user's Discord username, used to
                distinguish own messages from others' on Discord.

        Returns:
            An :class:`~creek.models.Authorship` value, or ``None`` if
            the authorship is ambiguous and requires manual review.
        """
        platform = fragment.source.platform

        if platform in _SELF_PLATFORMS:
            return Authorship.SELF

        if platform in _CHAT_PLATFORMS:
            return self._tag_chat(fragment)

        if platform == SourcePlatform.DISCORD:
            return self._tag_discord(fragment, discord_username)

        # Email, Image OCR, Other — ambiguous
        return None

    def _tag_chat(self, fragment: Fragment) -> Authorship | None:
        """Classify a chat-platform fragment by interlocutor role.

        Args:
            fragment: A fragment from Claude or ChatGPT.

        Returns:
            ``Authorship.SELF`` for human/user turns,
            ``Authorship.AI`` for assistant turns,
            or ``None`` if the role is missing or unrecognised.
        """
        interlocutor = fragment.source.interlocutor
        if interlocutor is None:
            return None

        role = interlocutor.strip().lower()

        if role in _HUMAN_ROLES:
            return Authorship.SELF
        if role in _ASSISTANT_ROLES:
            return Authorship.AI

        return None

    def _tag_discord(
        self,
        fragment: Fragment,
        discord_username: str | None,
    ) -> Authorship | None:
        """Classify a Discord fragment by comparing the sender to the user.

        Args:
            fragment: A fragment from Discord.
            discord_username: The user's own Discord username.

        Returns:
            ``Authorship.SELF`` if the sender matches
            ``discord_username``, ``Authorship.OTHER`` if it does not,
            or ``None`` if either the username or interlocutor is missing.
        """
        interlocutor = fragment.source.interlocutor
        if interlocutor is None or discord_username is None:
            return None

        if interlocutor.strip().lower() == discord_username.strip().lower():
            return Authorship.SELF

        return Authorship.OTHER
