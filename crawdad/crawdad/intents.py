"""Intent schema for the Haiku router (FEAT-014).

ADOPT-008 §pre-decided choices: the ``intents`` JSON schema lives next
to the MCP tool registry — every intent ``type`` is an MCP tool name,
1-to-1. Generating the schema from the live ``tools/list`` response
keeps the router prompt and the dispatcher honest as the tool surface
grows (FEAT-011 / FEAT-012 / future).

This module owns five things:

* :class:`Intent` — a single router-emitted call (``type`` + ``args``).
* :class:`RouterResponse` — the strict JSON shape Haiku must produce.
* :func:`build_intents_schema` — turn a list of MCP tool descriptors
  into a JSON Schema the router prompt can paste verbatim.
* :data:`CEILING_RANK` — the one tier-ordering table in the codebase.
* :data:`COMPOSER_ADMITTED_CEILINGS` + :func:`cap_ceiling` — the
  cloud-composer privacy cap every CrawDad path narrows down to.

The last two live *here* rather than in ``crawdad.loop`` (where the rank
table used to live) for one structural reason: every other module in the
package imports ``crawdad.intents`` and ``crawdad.intents`` imports none
of them, so this is the only home a shared table can occupy without
creating an import cycle — ``crawdad.dispatcher``, which needs the cap
most, cannot import ``crawdad.loop`` at all (#1152).
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Mapping

# FEAT-029: client-side intent for dynamic voice-register switching. The
# dispatcher recognises this type *before* the MCP known-tools check and
# routes it to the loop's :class:`SkillStackRegistry` instead of an MCP
# tool call. The ``crawdad.`` namespace prefix keeps it visually distinct
# from MCP ``creek.*`` tools and immune to a future MCP-side collision.
ACTIVATE_REGISTER_INTENT_TYPE: str = "crawdad.activate_register"

# ADAPT-003: client-side intent for running an authored workflow. Like
# ``crawdad.activate_register`` the dispatcher recognises this type
# *before* the MCP known-tools check and routes it to the workflow
# walker (:func:`crawdad.workflows.run_workflow_and_compose`) instead of
# an MCP tool call. The ``crawdad.`` namespace prefix keeps it visually
# distinct from MCP ``creek.*`` tools and immune to a future MCP-side
# collision.
RUN_WORKFLOW_INTENT_TYPE: str = "crawdad.run_workflow"


class PrivacyTierCeiling(StrEnum):
    """Per-intent privacy tier ceiling.

    Mirrors :class:`creek_mcp.tier_ceiling.TierCeiling` enum values so
    the router emits the exact string the MCP server validates.
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    ALL = "all"


# The single tier-ordering table, least-to-most permissive. Relocated
# here from ``crawdad.loop`` by #1152 so the dispatcher can share it.
#
# It is hand-written rather than derived because
# :class:`PrivacyTierCeiling` is a ``StrEnum``: comparing two members
# directly compares their *spelling*, which would put ``"personal"``
# above ``"open"`` (right answer, wrong reason) and ``"all"`` below
# everything (flatly wrong). Any ranking of these tiers must go through
# this table, and there must be exactly one of it — a second copy that
# merely agrees today is a second place to drift tomorrow.
#
# ``MappingProxyType`` because a mutable module-level dict is a
# privacy-relevant global anyone could reorder at runtime.
CEILING_RANK: Final[Mapping[PrivacyTierCeiling, int]] = MappingProxyType(
    {
        PrivacyTierCeiling.OPEN: 0,
        PrivacyTierCeiling.PERSONAL: 1,
        PrivacyTierCeiling.INTIMATE: 2,
        PrivacyTierCeiling.ALL: 3,
    }
)

# The only privacy tier ceilings any CrawDad path may reach the MCP
# server with.
#
# Every :class:`~crawdad.dispatcher.ToolResult` body — whether it came
# from a router-emitted intent or an authored workflow walk — is relayed
# to a cloud LLM composer and then posted into a Discord message, so
# ``intimate`` / ``all`` content must never be requestable. ``ALL``
# subsumes ``intimate``, so the admitted set is the complement.
#
# ``crawdad.workflows.WORKFLOW_ADMITTED_CEILINGS`` is an alias of this
# object, not a copy: the workflow cap and the router cap are the same
# boundary, and one of two agreeing frozensets would eventually be
# widened without the other showing up in the reviewer's diff.
COMPOSER_ADMITTED_CEILINGS: Final[frozenset[PrivacyTierCeiling]] = frozenset(
    {PrivacyTierCeiling.OPEN, PrivacyTierCeiling.PERSONAL}
)

# The only ceilings :func:`build_intents_schema` shows the router, in
# rank order so the list reads most-restrictive first. Derived from the
# cap rather than written out: a hand-listed literal would be a second
# place to widen, and the schema is the router's only hint about what it
# may ask for. (Advertising ``intimate`` / ``all`` there would be an
# outright invitation — a poisoned tool body rendered into the router
# prompt would only have to echo a tier the schema already named.)
_ADVERTISED_CEILINGS: Final[tuple[str, ...]] = tuple(
    tier.value
    for tier in sorted(COMPOSER_ADMITTED_CEILINGS, key=CEILING_RANK.__getitem__)
)


def cap_ceiling(requested: PrivacyTierCeiling) -> PrivacyTierCeiling:
    """Narrow *requested* to the broadest admitted ceiling no looser than it.

    The router's ceiling is untrusted input: raw MCP tool-result bodies
    (vault fragments ``creek ingest`` built from third-party ChatGPT /
    Discord / Substack exports) are appended to the conversation history
    and rendered verbatim into the router prompt, so attacker-authored
    text can ask for ``intimate``. Nothing downstream stops it — CrawDad
    speaks MCP over stdio, which the server classifies as a LOCAL caller,
    so its remote cap never fires (#1152).

    Clamps rather than refuses. Refusing would let one poisoned vault
    fragment permanently DoS the bot: ``AgentLoop.run`` catches only
    ``UnknownIntentError`` / ``MCPUnavailableError``, so a new exception
    would escape ``handle_message`` and the bot would simply go silent.
    Clamping gives identical confidentiality — the call proceeds at
    ``personal`` — plus an operator-visible warning at the call site.

    Args:
        requested: The ceiling the caller (router or workflow) asked for.

    Returns:
        ``requested`` itself when it is already admitted, otherwise the
        highest-ranked admitted ceiling that does not exceed it —
        ``open`` → ``open``, ``personal`` → ``personal``, and both
        ``intimate`` and ``all`` → ``personal``.
    """
    # Looked up rather than subscripted. The annotation says
    # ``PrivacyTierCeiling``, but ``model_construct`` and
    # ``model_copy(update=...)`` can put a raw ``str`` on an
    # :class:`Intent` without pydantic ever coercing it.
    #
    # A well-spelled one is fine: ``PrivacyTierCeiling`` is a
    # ``StrEnum``, so its members hash and compare as their *value*
    # (verified — ``hash("all") == hash(PrivacyTierCeiling.ALL)``), and
    # ``"all"`` therefore hits the table and clamps to ``personal``
    # exactly as the member would. It is a MIS-spelled one — ``"ALL"``,
    # ``""``, a typo — that used to raise ``KeyError`` here. That
    # failure was safe (it precedes any tool call) but it is an
    # uncaught exception, and the agent loop catches only
    # ``UnknownIntentError`` / ``MCPUnavailableError`` — so the bot
    # would go silent, which is the very DoS this function clamps to
    # avoid. An unrecognised tier is instead treated as maximally
    # suspicious and clamped to the most restrictive tier there is.
    requested_rank = CEILING_RANK.get(requested)
    if requested_rank is None:
        return PrivacyTierCeiling.OPEN
    # The ``<=`` filter, not "return the highest admitted tier": the
    # result must never rank *above* the request, so a future
    # non-contiguous admitted set could not widen an ``open`` request.
    # ``key=CEILING_RANK.__getitem__``, never a bare ``max()``, which
    # would compare the StrEnum members lexically.
    return max(
        (
            tier
            for tier in COMPOSER_ADMITTED_CEILINGS
            if CEILING_RANK[tier] <= requested_rank
        ),
        key=CEILING_RANK.__getitem__,
        default=PrivacyTierCeiling.OPEN,
    )


class Intent(BaseModel):
    """One tool call the router asked the dispatcher to make."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(min_length=1)
    privacy_tier_ceiling: PrivacyTierCeiling = PrivacyTierCeiling.OPEN
    args: dict[str, Any] = Field(default_factory=dict)


class RouterResponse(BaseModel):
    """The exact JSON shape Haiku must emit — no prose, no extras.

    FEAT-015 adds the ``compose`` flag: it ends router iteration, so the
    Sonnet composer produces the reply for this round. An empty
    ``intents`` list ends iteration too. The pair ``{intents: [],
    compose: true}`` is the canonical "I have enough context; compose
    now" signal.

    ``compose`` and ``intents`` are independent: a response may carry
    both, and the loop then dispatches those intents *before* composing
    rather than discarding them (#915). The router prompt invites this
    shape by asking for the intent "BEFORE setting ``compose: true``".
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
        Its ``privacy_tier_ceiling`` enum is :data:`_ADVERTISED_CEILINGS`
        — the capped set — not every :class:`PrivacyTierCeiling` member.
    """
    tool_names = [tool.name for tool in tools]
    allowed_types = [
        *tool_names,
        ACTIVATE_REGISTER_INTENT_TYPE,
        RUN_WORKFLOW_INTENT_TYPE,
    ]
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
                                "MCP tool name to invoke, or a client-side "
                                f"intent: {ACTIVATE_REGISTER_INTENT_TYPE!r} to "
                                "switch the active voice register mid-session, "
                                f"or {RUN_WORKFLOW_INTENT_TYPE!r} to run an "
                                "authored workflow by name."
                            ),
                        },
                        "privacy_tier_ceiling": {
                            "type": "string",
                            # Capped, not the whole enum. The dispatcher
                            # clamps regardless; this stops the schema
                            # from suggesting a tier in the first place.
                            "enum": list(_ADVERTISED_CEILINGS),
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
