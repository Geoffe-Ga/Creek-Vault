"""Chatbot pre-ingestion filter — skip noise, collapse regenerations.

Filters chatbot conversation exports (Claude, ChatGPT) to remove noise
before fragment extraction. Handles:

- **System prompts**: Messages with ``role=system`` or tool-definition preambles.
- **Tool call outputs**: Messages with ``role=tool`` or ``role=tool_result``.
- **Regeneration collapse**: Consecutive assistant messages reduced to final version.
- **Abandoned conversations**: Flagged when ≤N turns with no substantive content.
- **Short human turns**: Skipped when below configurable character threshold.
- **Code-only responses**: Flagged when >threshold% of content is code blocks.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ChatbotFilterConfig(BaseModel):
    """Configuration for the chatbot pre-ingestion filter.

    All thresholds are configurable to tune filtering behaviour per
    deployment without modifying code.

    Attributes:
        min_human_turn_length: Minimum character count for human turns.
        code_block_threshold: Fraction of content that is code blocks
            above which the response is flagged as code-only.
        max_abandoned_turns: Maximum number of turns (human+assistant
            pairs) for a conversation to be considered abandoned.
        skip_system_prompts: Whether to filter out system prompt messages.
        skip_tool_outputs: Whether to filter out tool output messages.
        collapse_regenerations: Whether to collapse consecutive assistant
            messages to keep only the final version.
    """

    min_human_turn_length: int = Field(default=20, ge=0)
    """Minimum character count for human turns (default: 20)."""

    code_block_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    """Code-block ratio above which a response is flagged (default: 0.9)."""

    max_abandoned_turns: int = Field(default=2, ge=0)
    """Max turn pairs for abandoned conversation detection (default: 2)."""

    skip_system_prompts: bool = True
    """Whether to skip system prompt messages."""

    skip_tool_outputs: bool = True
    """Whether to skip tool output messages."""

    collapse_regenerations: bool = True
    """Whether to collapse consecutive assistant messages."""


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class MessageVerdict(BaseModel):
    """Verdict for a single message after filtering.

    Attributes:
        action: The filtering action: ``keep``, ``skip``, or ``flag``.
        reason: Human-readable explanation of the verdict.
    """

    action: Literal["keep", "skip", "flag"]
    reason: str


class ConversationFilterResult(BaseModel):
    """Result of filtering a conversation's messages.

    Attributes:
        messages: Messages that passed filtering (kept messages).
        skipped: Count of messages that were skipped.
        flagged: Count of messages that were flagged for review.
        reasons: Human-readable explanations of filtering actions.
        is_abandoned: Whether the conversation is flagged as abandoned.
    """

    messages: list[dict[str, Any]]
    skipped: int = 0
    flagged: int = 0
    reasons: list[str] = Field(default_factory=list)
    is_abandoned: bool = False


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_TOOL_PREAMBLE_PATTERN: re.Pattern[str] = re.compile(
    r"you have access to the following tools",
    re.IGNORECASE,
)
"""Pattern matching tool-definition preambles in system messages."""

_CODE_BLOCK_PATTERN: re.Pattern[str] = re.compile(
    r"```[\s\S]*?```",
)
"""Pattern matching fenced code blocks."""

_HUMAN_ROLES: frozenset[str] = frozenset({"human", "user"})
"""Roles that represent human/user messages across platforms."""

_SYSTEM_ROLES: frozenset[str] = frozenset({"system"})
"""Roles that represent system messages."""

_TOOL_ROLES: frozenset[str] = frozenset({"tool", "tool_result"})
"""Roles that represent tool output messages."""

_ASSISTANT_ROLES: frozenset[str] = frozenset({"assistant"})
"""Roles that represent assistant messages."""


# ---------------------------------------------------------------------------
# ChatbotFilter
# ---------------------------------------------------------------------------


class ChatbotFilter:
    """Pre-ingestion filter for chatbot conversation exports.

    Applies configurable filtering rules to a list of conversation
    messages, removing noise (system prompts, tool outputs, short turns)
    and collapsing regenerated assistant responses.

    Works with both Claude and ChatGPT message structures.

    Attributes:
        config: The filter configuration.
    """

    def __init__(self, config: ChatbotFilterConfig | None = None) -> None:
        """Initialise the filter with optional configuration.

        Args:
            config: Filter configuration. Uses defaults if ``None``.
        """
        self.config = config or ChatbotFilterConfig()

    def filter_conversation(
        self,
        messages: list[dict[str, Any]],
        *,
        platform: str,
    ) -> ConversationFilterResult:
        """Filter a conversation's messages according to configured rules.

        Applies all enabled filters in sequence: system prompts, tool
        outputs, short human turns, regeneration collapse, and code-only
        flagging. Also detects abandoned conversations.

        Args:
            messages: List of message dicts with ``role`` and ``content`` keys.
            platform: The source platform (``"claude"`` or ``"chatgpt"``).

        Returns:
            A :class:`ConversationFilterResult` with filtered messages and stats.
        """
        if not messages:
            return ConversationFilterResult(
                messages=[],
                skipped=0,
                flagged=0,
                reasons=[],
                is_abandoned=True,
            )

        kept: list[dict[str, Any]] = []
        skipped = 0
        flagged = 0
        reasons: list[str] = []

        # Phase 1: Filter system prompts, tool outputs, short human turns
        phase1: list[dict[str, Any]] = []
        for msg in messages:
            verdict = self._evaluate_message(msg, platform)
            if verdict.action == "skip":
                skipped += 1
                reasons.append(verdict.reason)
            else:
                phase1.append(msg)

        # Phase 2: Collapse regenerations
        if self.config.collapse_regenerations:
            phase2, regen_skipped, regen_reasons = self._collapse_regenerations(phase1)
            skipped += regen_skipped
            reasons.extend(regen_reasons)
        else:
            phase2 = phase1

        # Phase 3: Flag code-only responses
        for msg in phase2:
            role = msg.get("role", "")
            if role in _ASSISTANT_ROLES:
                code_verdict = self._check_code_only(msg, platform)
                if code_verdict.action == "flag":
                    flagged += 1
                    reasons.append(code_verdict.reason)
            kept.append(msg)

        # Phase 4: Detect abandoned conversations
        is_abandoned = self._is_abandoned(kept)

        return ConversationFilterResult(
            messages=kept,
            skipped=skipped,
            flagged=flagged,
            reasons=reasons,
            is_abandoned=is_abandoned,
        )

    # ---- Message evaluation ----

    def _evaluate_message(
        self,
        msg: dict[str, Any],
        platform: str,
    ) -> MessageVerdict:
        """Evaluate a single message against filtering rules.

        Args:
            msg: A message dict with ``role`` and optionally ``content``.
            platform: The source platform identifier.

        Returns:
            A :class:`MessageVerdict` with the filtering decision.
        """
        role = msg.get("role", "")

        # System prompt filtering
        if role in _SYSTEM_ROLES and self.config.skip_system_prompts:
            return MessageVerdict(action="skip", reason=f"System prompt (role={role})")

        # Tool output filtering
        if role in _TOOL_ROLES and self.config.skip_tool_outputs:
            return MessageVerdict(action="skip", reason=f"Tool output (role={role})")

        # Tool-definition preamble in non-system messages
        if self.config.skip_system_prompts:
            text = _extract_text(msg, platform)
            if _TOOL_PREAMBLE_PATTERN.search(text) and role in _SYSTEM_ROLES:
                return MessageVerdict(
                    action="skip",
                    reason="Tool-definition preamble in system message",
                )

        # Short human turn filtering
        if role in _HUMAN_ROLES:
            text = _extract_text(msg, platform)
            min_len = self.config.min_human_turn_length
            if len(text) < min_len:
                return MessageVerdict(
                    action="skip",
                    reason=f"Short human turn ({len(text)} chars < {min_len})",
                )

        return MessageVerdict(action="keep", reason="Passed all filters")

    # ---- Regeneration collapse ----

    def _collapse_regenerations(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, list[str]]:
        """Collapse consecutive assistant messages, keeping only the last.

        Args:
            messages: Pre-filtered message list.

        Returns:
            Tuple of (collapsed messages, skip count, reasons).
        """
        if not messages:
            return [], 0, []

        result: list[dict[str, Any]] = []
        skipped = 0
        reasons: list[str] = []
        i = 0

        while i < len(messages):
            role = messages[i].get("role", "")

            if role in _ASSISTANT_ROLES:
                # Collect consecutive assistant messages
                run_start = i
                while (
                    i < len(messages)
                    and messages[i].get("role", "") in _ASSISTANT_ROLES
                ):
                    i += 1
                run_end = i
                run_length = run_end - run_start

                # Keep only the last one
                result.append(messages[run_end - 1])
                if run_length > 1:
                    collapsed_count = run_length - 1
                    skipped += collapsed_count
                    reasons.append(
                        f"Collapsed {collapsed_count} regenerated assistant response(s)"
                    )
            else:
                result.append(messages[i])
                i += 1

        return result, skipped, reasons

    # ---- Code-only detection ----

    def _check_code_only(
        self,
        msg: dict[str, Any],
        platform: str,
    ) -> MessageVerdict:
        """Check whether an assistant message is predominantly code.

        Args:
            msg: An assistant message dict.
            platform: The source platform identifier.

        Returns:
            A flag verdict if code ratio exceeds threshold, otherwise keep.
        """
        text = _extract_text(msg, platform)
        if not text.strip():
            return MessageVerdict(action="keep", reason="Empty assistant message")

        code_blocks = _CODE_BLOCK_PATTERN.findall(text)
        code_length = sum(len(block) for block in code_blocks)
        total_length = len(text)

        if (
            total_length > 0
            and code_length / total_length > self.config.code_block_threshold
        ):
            ratio = code_length / total_length
            return MessageVerdict(
                action="flag",
                reason=f"Code-only response ({ratio:.0%} code blocks)",
            )

        return MessageVerdict(action="keep", reason="Mixed content response")

    # ---- Abandoned conversation detection ----

    def _is_abandoned(self, messages: list[dict[str, Any]]) -> bool:
        """Detect whether a conversation is abandoned.

        A conversation is considered abandoned if it has at most
        ``max_abandoned_turns`` human+assistant turn pairs and none
        of the messages contain substantive content (≥50 chars).

        Args:
            messages: The filtered message list.

        Returns:
            ``True`` if the conversation appears abandoned.
        """
        if not messages:
            return True

        turn_pairs = _count_turn_pairs(messages)
        if turn_pairs > self.config.max_abandoned_turns:
            return False

        return not _has_substantive_content(messages)


# ---------------------------------------------------------------------------
# Content extraction utility
# ---------------------------------------------------------------------------


def _count_turn_pairs(messages: list[dict[str, Any]]) -> int:
    """Count human+assistant turn pairs in a message list.

    Args:
        messages: List of message dicts.

    Returns:
        The number of complete turn pairs.
    """
    human_count = sum(1 for m in messages if m.get("role", "") in _HUMAN_ROLES)
    assistant_count = sum(1 for m in messages if m.get("role", "") in _ASSISTANT_ROLES)
    return min(human_count, assistant_count)


_SUBSTANTIVE_THRESHOLD: int = 50
"""Minimum character count for a message to be considered substantive."""


def _has_substantive_content(messages: list[dict[str, Any]]) -> bool:
    """Check whether any conversational message has substantive content.

    Args:
        messages: List of message dicts.

    Returns:
        ``True`` if any human or assistant message has ≥50 chars.
    """
    conversational_roles = _HUMAN_ROLES | _ASSISTANT_ROLES
    for msg in messages:
        if msg.get("role", "") not in conversational_roles:
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) >= _SUBSTANTIVE_THRESHOLD:
            return True
    return False


def _extract_text(msg: dict[str, Any], platform: str) -> str:
    """Extract plain text from a message's content field.

    Handles multiple content formats:
    - Plain string
    - Claude list-of-parts format: ``[{"type": "text", "text": "..."}]``
    - ChatGPT parts format: ``{"parts": ["..."]}``

    Args:
        msg: A message dict with an optional ``content`` field.
        platform: The source platform (``"claude"`` or ``"chatgpt"``).

    Returns:
        The extracted text as a string, or empty string if unavailable.
    """
    content = msg.get("content")

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _extract_from_list(content)
    if isinstance(content, dict):
        return _extract_from_dict(content)
    return str(content)


def _extract_from_list(content: list[Any]) -> str:
    """Extract text from a Claude-style list-of-parts content.

    Args:
        content: List of content parts.

    Returns:
        Joined text from all text-type parts.
    """
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(parts)


def _extract_from_dict(content: dict[str, Any]) -> str:
    """Extract text from a ChatGPT-style parts dict content.

    Args:
        content: A dict with a ``parts`` key containing text parts.

    Returns:
        Joined text from all non-None parts.
    """
    parts_list = content.get("parts", [])
    text_parts = [str(p) for p in parts_list if p is not None]
    return "\n".join(text_parts)
