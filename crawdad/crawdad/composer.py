"""Sonnet composer (FEAT-015).

The composer is the user-facing prose step of CrawDad's agent loop.
The router (Haiku, FEAT-014) extracts intents and the dispatcher
invokes MCP tools; this module takes the resulting context — user
message, tool results, session state, voice skills — and asks Sonnet
for a single voice-faithful Discord reply.

FEAT-015 §pre-decided choices honoured here:

* **Composer model** is read from
  :data:`crawdad.config.DEFAULT_COMPOSER_MODEL`. No literal model IDs
  live outside ``config.py`` — verified by
  ``tests/test_no_model_literals.py``.
* **`creek.draft` is never inlined.** The composer prompt only embeds
  the *result* of a draft tool call; it never asks Sonnet to draft an
  essay. Voice fidelity for drafts is owned by ``creek-tools``'s own
  LLM (FEAT-005) with its own skill-stack assembly.
* **Paradox-tolerant.** When a tool result mentions a paradox the
  prompt includes the "name it, never propose resolution" rule.
* **Phase-aware.** Wavelength snapshot lands in the prompt unchanged
  so Sonnet can read the current phase/dosage.
* **Voice-faithful.** The :class:`VoiceSkillStack` is concatenated
  into the prompt verbatim.

SDK errors (rate limits, auth, network) surface as
:class:`ComposerFailureError` so the loop can drop back to the
documented soft reply rather than crashing ``on_message``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anthropic

if TYPE_CHECKING:
    from crawdad.dispatcher import ToolResult
    from crawdad.history import ConversationHistory
    from crawdad.skill_loader import VoiceSkillStack
    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.composer")

_PARADOX_KEYWORDS = ("paradox", "paradoxes")
_PARADOX_RULE = (
    "Paradox rule: if any tool result names a paradox, NAME the paradox "
    "in your reply but do NOT propose resolution. Paradoxes are evidence "
    "of polygnostic stance, not defects to flatten."
)
_DEFAULT_BEHAVIOR = (
    "Speak in the user's voice register — reflective, first-person, soft. "
    "Do not urge action during low-energy phases. Conditioning: phase + "
    "dosage from the wavelength snapshot."
)
_ROLE_PREAMBLE = (
    "You are CrawDad's voice — the conversational composer that wraps "
    "Creek vault tool results into a single Discord reply for the user. "
    "Read the user's voice-skill stack BELOW carefully; everything you "
    "write must sound like the user, not like a generic assistant."
)
_CLOSING_INSTRUCTION = (
    "Compose a single voice-faithful reply now. Keep it tight — Discord "
    "soft-caps around 2000 characters. Do not propose paradox resolution. "
    "Do not generate fresh essays inline; refer the user to the "
    "`creek.draft` tool output if a draft was produced."
)


class ComposerFailureError(RuntimeError):
    """The Sonnet composer call failed — SDK error or empty body.

    The loop maps this to the documented soft-error reply rather than
    letting it propagate through ``handle_message`` into Discord.
    """


def build_composer_prompt(
    *,
    user_message: str,
    tool_results: list[ToolResult],
    history: ConversationHistory,
    state: SessionState | None,
    skills: VoiceSkillStack,
) -> str:
    """Assemble the Sonnet composer prompt.

    Kept as a free function so prompt assembly is fully testable
    without an LLM client and the "no creek.draft instructions inlined"
    invariant can be grep-checked.
    """
    sections = [
        _ROLE_PREAMBLE,
        _DEFAULT_BEHAVIOR,
        _PARADOX_RULE if _mentions_paradox(tool_results) else "",
        f"## Voice-skill stack\n{_skills_block(skills)}",
        f"## {_wavelength_block(state)}",
        f"## {_history_block(history)}",
        f"## {_results_block(tool_results)}",
        f"## User message\n{user_message}",
        _CLOSING_INSTRUCTION,
    ]
    return "\n\n".join(section for section in sections if section)


def _skills_block(skills: VoiceSkillStack) -> str:
    return skills.as_prompt_context() or "(no voice-skill stack loaded)"


def _wavelength_block(state: SessionState | None) -> str:
    wavelength = (
        state.wavelength_snapshot if state and state.wavelength_snapshot else None
    )
    if wavelength:
        return f"Current wavelength snapshot:\n{wavelength}"
    return "Current wavelength: (no wavelength snapshot available)"


def _history_block(history: ConversationHistory) -> str:
    lines = [f"  - {entry.role}: {entry.content}" for entry in history.as_list()]
    if not lines:
        return "Recent conversation history: (empty)"
    return "Recent conversation history:\n" + "\n".join(lines)


def _results_block(tool_results: list[ToolResult]) -> str:
    if not tool_results:
        return "Tool results: (none — answer from state + skills)"
    lines: list[str] = []
    for result in tool_results:
        lines.append(f"### Tool: `{result.intent_type}`")
        lines.append(result.body.strip() or "(empty body)")
        lines.append("")
    return "Tool results:\n" + "\n".join(lines)


def _mentions_paradox(results: list[ToolResult]) -> bool:
    """Return True when any tool result body or intent mentions a paradox."""
    for result in results:
        haystack = f"{result.intent_type} {result.body}".lower()
        if any(kw in haystack for kw in _PARADOX_KEYWORDS):
            return True
    return False


class SonnetComposer:
    """Wraps the Anthropic Sonnet call + prompt assembly for one turn."""

    def __init__(
        self,
        *,
        anthropic_client: Any,
        model: str,
        max_tokens: int = 1024,
    ) -> None:
        """Store the injected client and model identifier.

        Args:
            anthropic_client: An :class:`anthropic.AsyncAnthropic`
                instance (or a test double with the same shape).
            model: The Sonnet model identifier — resolved from
                :data:`crawdad.config.DEFAULT_COMPOSER_MODEL` by the
                caller, NEVER a literal in this module.
            max_tokens: Hard cap on the composer's reply length.
        """
        self._client = anthropic_client
        self._model = model
        self._max_tokens = max_tokens

    async def compose(
        self,
        *,
        user_message: str,
        tool_results: list[ToolResult],
        history: ConversationHistory,
        state: SessionState | None,
        skills: VoiceSkillStack,
    ) -> str:
        """Produce the user-facing reply.

        Raises:
            ComposerFailureError: when the SDK call fails or the
                response has no text content.
        """
        prompt = build_composer_prompt(
            user_message=user_message,
            tool_results=tool_results,
            history=history,
            state=state,
            skills=skills,
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            _LOGGER.warning(
                "Sonnet composer call failed: %s: %r", type(exc).__name__, exc
            )
            msg = f"Sonnet composer call failed: {type(exc).__name__}"
            raise ComposerFailureError(msg) from exc
        reply = _extract_text(response)
        if not reply.strip():
            msg = "Sonnet composer returned an empty body"
            raise ComposerFailureError(msg)
        return reply


def _extract_text(response: Any) -> str:
    """Pull the concatenated text from a (possibly multi-block) reply."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts)
