"""Shared Discord text limits and soft-error messages."""

_DISCORD_REPLY_LIMIT = 1900
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."


def _truncate_for_discord(text: str) -> str:
    """Cap *text* at Discord's soft limit with an ellipsis marker if needed."""
    if len(text) <= _DISCORD_REPLY_LIMIT:
        return text
    return text[: _DISCORD_REPLY_LIMIT - 3] + "..."
