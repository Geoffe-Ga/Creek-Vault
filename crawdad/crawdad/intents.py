"""Intent schema for the Haiku router (FEAT-014).

ADOPT-008 §pre-decided choices: the ``intents`` JSON schema lives next
to the MCP tool registry — every intent ``type`` is an MCP tool name,
1-to-1. Generating the schema from the live ``tools/list`` response
keeps the router prompt and the dispatcher honest as the tool surface
grows (FEAT-011 / FEAT-012 / future).

This module owns three things:

* :class:`Intent` — a single router-emitted call (``type`` + ``args``).
* :class:`RouterResponse` — the strict JSON shape Haiku must produce.
* :func:`build_intents_schema` — turn a list of MCP tool descriptors
  into a JSON Schema the router prompt can paste verbatim.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# FEAT-029: client-side intent for dynamic voice-register switching. The
# dispatcher recognises this type *before* the MCP known-tools check and
# routes it to the loop's :class:`SkillStackRegistry` instead of an MCP
# tool call. The ``crawdad.`` namespace prefix keeps it visually distinct
# from MCP ``creek.*`` tools and immune to a future MCP-side collision.
ACTIVATE_REGISTER_INTENT_TYPE: str = "crawdad.activate_register"


class PrivacyTierCeiling(StrEnum):
    """Per-intent privacy tier ceiling.

    Mirrors :class:`creek_mcp.tier_ceiling.TierCeiling` enum values so
    the router emits the exact string the MCP server validates.
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    ALL = "all"


class Intent(BaseModel):
    """One tool call the router asked the dispatcher to make."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1)
    privacy_tier_ceiling: PrivacyTierCeiling = PrivacyTierCeiling.OPEN
    args: dict[str, Any] = Field(default_factory=dict)


class RouterResponse(BaseModel):
    """The exact JSON shape Haiku must emit — no prose, no extras.

    FEAT-015 adds the ``compose`` flag: when ``True`` (or when
    ``intents`` is empty) the loop terminates router iteration and
    invokes the Sonnet composer. The pair ``{intents: [], compose:
    true}`` is the canonical "I have enough context; compose now"
    signal.
    """

    model_config = ConfigDict(frozen=True)

    intents: list[Intent]
    compose: bool = False


class ToolInfo(BaseModel):
    """A normalised view of one entry in the MCP ``tools/list`` response."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]


def build_intents_schema(tools: list[ToolInfo]) -> dict[str, Any]:
    """Return a JSON Schema for the router's ``{intents: [...]}`` output.

    Args:
        tools: The MCP server's currently advertised tools, fetched once
            at session start and cached. The schema's ``type`` enum is
            the list of ``tool.name`` values.

    Returns:
        A JSON-Schema-compatible dict that the router prompt can paste
        verbatim and that a downstream JSON parser can validate against.
    """
    tool_names = [tool.name for tool in tools]
    allowed_types = [*tool_names, ACTIVATE_REGISTER_INTENT_TYPE]
    return {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": allowed_types,
                            "description": (
                                "MCP tool name to invoke, or the client-side "
                                f"intent {ACTIVATE_REGISTER_INTENT_TYPE!r} to "
                                "switch the active voice register mid-session."
                            ),
                        },
                        "privacy_tier_ceiling": {
                            "type": "string",
                            "enum": [t.value for t in PrivacyTierCeiling],
                            "default": PrivacyTierCeiling.OPEN.value,
                        },
                        "args": {
                            "type": "object",
                            "description": "Tool-specific arguments.",
                        },
                    },
                    "required": ["type"],
                },
            },
            "compose": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set true ONLY when no more tool calls are needed and "
                    "the composer should produce the user-facing reply. "
                    "Pair with ``intents: []`` for the terminal signal."
                ),
            },
        },
        "required": ["intents"],
    }
