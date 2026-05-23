"""Haiku-driven intent extraction (FEAT-014).

Given the user's message, recent conversation history, and the loaded
session state, the router asks Haiku for a strict JSON
``{intents: [...]}`` array. The dispatcher (:mod:`crawdad.dispatcher`)
takes it from there.

ADOPT-008 §pre-decided choices:

* The router's job is *intent extraction only*, never prose. Haiku
  emits JSON; Sonnet (FEAT-015) does composition.
* Wavelength-aware: the prompt names the current phase so Haiku biases
  toward phase-appropriate intents (no ``mine`` / ``draft`` during
  Bottoming Out; prefer ``surface_paradox`` / ``compost``).
* History truncation cliff: last 20 entries x 2000 chars each (enforced
  by :class:`crawdad.history.ConversationHistory`, not here).
* Per-intent ``privacy_tier_ceiling`` defaults to ``open`` for the
  developer; configurable in v1.1.

The router accepts an injected Anthropic client so tests can drive it
with a fake. The model identifier is passed in (resolved from
:data:`crawdad.config.DEFAULT_ROUTER_MODEL` by callers) so no literal
model ID lives in this module — see the
``test_no_model_id_literals_outside_config`` regression.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import anthropic
from pydantic import ValidationError

from crawdad.intents import (
    ACTIVATE_REGISTER_INTENT_TYPE,
    RouterResponse,
    ToolInfo,
    build_intents_schema,
)

if TYPE_CHECKING:
    from crawdad.history import ConversationHistory
    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.router")

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_BOTTOMING_OUT_HINT = (
    "If the current phase is Bottoming Out or Diminishing, PREFER intents "
    "that surface compost, paradoxes, or synchronicities. AVOID `creek.mine` "
    "and `creek.draft` until the wavelength shifts upward."
)
_RISING_HINT = (
    "If the current phase is Rising or Peaking, mining and drafting "
    "intents are appropriate."
)
_DEFAULT_PHASE_HINT = (
    "Tune intent choice to the current wavelength phase if one is present."
)


class RouterParseError(RuntimeError):
    """The Haiku response could not be parsed into a :class:`RouterResponse`.

    The bot maps this to a user-facing "I lost the thread; can you
    rephrase?" reply rather than crashing.
    """


def build_router_prompt(
    *,
    message: str,
    history: ConversationHistory,
    state: SessionState | None,
    tools: list[ToolInfo],
) -> str:
    """Assemble the system + user prompt the router sends to Haiku.

    Returns a single string the caller passes as the ``messages`` body.
    Kept as a free function so prompt assembly is fully testable without
    an LLM client.
    """
    schema = build_intents_schema(tools)
    wavelength = (
        state.wavelength_snapshot if state and state.wavelength_snapshot else None
    )
    phase_hint = _phase_hint_for(wavelength)
    history_lines = [
        f"  - {entry.role}: {entry.content}" for entry in history.as_list()
    ]
    history_block = (
        "Recent history:\n" + "\n".join(history_lines) if history_lines else ""
    )
    wavelength_block = (
        f"Current wavelength:\n  {wavelength}"
        if wavelength
        else "Current wavelength: (no wavelength snapshot available)"
    )
    tools_block = "Available MCP tools:\n" + "\n".join(
        f"  - {tool.name}: {tool.description}" for tool in tools
    )
    schema_block = "Intents JSON schema (your output MUST validate):\n" + json.dumps(
        schema, indent=2
    )
    return (
        "You are the intent-extraction router for the CrawDad spiritual "
        "companion. Your job is to read the user's latest message and emit "
        'ONLY a JSON object of the form {"intents": [...], "compose": bool}.\n\n'
        "Set `compose: true` (with `intents: []`) ONLY when no more tool "
        "calls are needed and the Sonnet composer should produce the "
        "user-facing reply right now. If prior tool results in the history "
        "include a paradox surfacing, include a `creek.save` intent "
        "(target=paradox) BEFORE setting `compose: true` so paradoxes are "
        "routed to `10-Liminal/Paradoxes/`. Never propose paradox resolution.\n\n"
        "Register-switch detection (FEAT-029): when the user asks you to "
        "switch voice register, change voice, or activate a different "
        'voice — phrases like "switch to the analytic register", "use '
        'confessional voice", "speak in praxis mode", "can you talk like '
        'the X register now" — emit a single intent with '
        f'type "{ACTIVATE_REGISTER_INTENT_TYPE}" and '
        '`args: {"register": "<name>"}` BEFORE setting `compose: true`. '
        "Use the lowercase, hyphenated register name (e.g. "
        "`confessional`, `praxis`, `analytic`). Do NOT call any MCP tool "
        "for register switches — the dispatcher handles them locally.\n\n"
        f"{phase_hint}\n\n"
        f"{wavelength_block}\n\n"
        f"{history_block}\n\n"
        f"{tools_block}\n\n"
        f"{schema_block}\n\n"
        f"User message: {message}\n\n"
        "Reply with the JSON object only. No prose, no markdown fences."
    )


def _phase_hint_for(wavelength: str | None) -> str:
    """Return the phase-aware instruction string for the prompt."""
    if not wavelength:
        return _DEFAULT_PHASE_HINT
    lowered = wavelength.lower()
    if "bottoming" in lowered or "diminishing" in lowered or "withdrawal" in lowered:
        return _BOTTOMING_OUT_HINT
    if "rising" in lowered or "peaking" in lowered:
        return _RISING_HINT
    return _DEFAULT_PHASE_HINT


class IntentRouter:
    """Wraps the Anthropic SDK call + JSON parse for one user message."""

    def __init__(
        self,
        *,
        anthropic_client: Any,
        model: str,
        tools: list[ToolInfo],
        max_tokens: int = 1024,
    ) -> None:
        """Store the injected client and the cached tools schema.

        Args:
            anthropic_client: An :class:`anthropic.AsyncAnthropic`
                instance (or a test double with the same shape).
            model: The Haiku model identifier — resolved from
                :data:`crawdad.config.DEFAULT_ROUTER_MODEL` by the
                caller, NEVER a literal in this module.
            tools: The advertised MCP tool surface, snapshotted at
                session start by :meth:`MCPSession.list_tool_details`.
            max_tokens: Hard cap on the router's reply length.
        """
        self._client = anthropic_client
        self._model = model
        self._tools = tools
        self._max_tokens = max_tokens

    async def extract_intents(
        self,
        *,
        message: str,
        history: ConversationHistory,
        state: SessionState | None,
    ) -> RouterResponse:
        """Return the parsed :class:`RouterResponse` for ``message``.

        Raises:
            RouterParseError: when the SDK call fails (rate-limit,
                auth, connection, etc.) or when the response body is
                not parseable JSON. The bot handler maps both to the
                "I lost the thread; can you rephrase?" user-facing
                reply rather than crashing.
        """
        prompt = build_router_prompt(
            message=message,
            history=history,
            state=state,
            tools=self._tools,
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            _LOGGER.warning("Haiku API call failed: %s: %r", type(exc).__name__, exc)
            msg = f"Haiku API call failed: {type(exc).__name__}"
            raise RouterParseError(msg) from exc
        raw_text = _extract_text(response)
        return _parse_router_payload(raw_text)


def _extract_text(response: Any) -> str:
    """Pull the concatenated text from a (possibly multi-block) reply."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts)


def _parse_router_payload(raw: str) -> RouterResponse:
    """Decode the JSON body out of *raw* and validate it."""
    candidate = _strip_fences(raw).strip()
    if not candidate:
        msg = "router returned an empty body — expected JSON intents object"
        raise RouterParseError(msg)
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError as exc:
        _LOGGER.debug("router raw body: %r", raw)
        msg = "router did not return valid JSON"
        raise RouterParseError(msg) from exc
    try:
        return RouterResponse.model_validate(decoded)
    except ValidationError as exc:
        msg = "router JSON did not match the intents schema"
        raise RouterParseError(msg) from exc


def _strip_fences(raw: str) -> str:
    """Return the JSON body of a possibly fenced markdown response."""
    match = _FENCED_JSON_RE.search(raw)
    if match:
        return match.group(1)
    return raw
