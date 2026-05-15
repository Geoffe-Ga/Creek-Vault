"""Tests for ``crawdad.composer`` — the Sonnet composer step."""

from __future__ import annotations

from typing import Any

import pytest

from crawdad.composer import (
    ComposerFailureError,
    SonnetComposer,
    build_composer_prompt,
)
from crawdad.dispatcher import ToolResult
from crawdad.history import ConversationHistory
from crawdad.skill_loader import VoiceSkill, VoiceSkillStack
from crawdad.state import SessionState


@pytest.fixture
def session_state() -> SessionState:
    return SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot="Phase: **rising** (confidence 0.84)",
        eddies=("eddy-clarity",),
        threads=("thread-voice",),
        suggested_questions=("What is surfacing?",),
    )


@pytest.fixture
def skills() -> VoiceSkillStack:
    return VoiceSkillStack(
        skills=(
            VoiceSkill(name="voice-core", body="Speak in the user's first person."),
            VoiceSkill(name="phase:rising", body="Energy is rising; lean in."),
            VoiceSkill(
                name="register:confessional", body="Reflective, soft, no advice-giving."
            ),
        )
    )


@pytest.fixture
def tool_results() -> list[ToolResult]:
    return [
        ToolResult(intent_type="creek.state.read", body="latest.md content here"),
    ]


class _FakeAnthropic:
    """Minimal stand-in for ``anthropic.AsyncAnthropic``."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        return _FakeResponse(self._reply)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


def test_build_composer_prompt_embeds_skills_and_results(
    session_state: SessionState,
    skills: VoiceSkillStack,
    tool_results: list[ToolResult],
) -> None:
    """The prompt names voice skills, wavelength, tool results, and user msg."""
    prompt = build_composer_prompt(
        user_message="what's surfacing?",
        tool_results=tool_results,
        history=ConversationHistory(),
        state=session_state,
        skills=skills,
    )

    assert "voice-core" in prompt or "first person" in prompt
    assert "rising" in prompt
    assert "creek.state.read" in prompt
    assert "latest.md content here" in prompt
    assert "what's surfacing?" in prompt


def test_build_composer_prompt_never_inlines_creek_draft_body(
    session_state: SessionState,
    skills: VoiceSkillStack,
) -> None:
    """``creek.draft`` is invoked as a tool; the prompt must never embed it directly.

    FEAT-015 §pre-decided choices: voice fidelity for drafts is owned by
    ``creek-tools``'s own LLM call, NOT by the composer. The composer
    wraps the draft tool's output in conversational framing only.
    """
    # The composer prompt CAN include the draft tool's BODY (the result
    # of calling the tool); what it must never do is treat the draft
    # generator as inline instructions ("draft an essay about X"). The
    # safeguard is verified at the prompt-construction layer.
    draft_result = ToolResult(
        intent_type="creek.draft",
        body="Generated essay draft from creek-tools.",
    )

    prompt = build_composer_prompt(
        user_message="draft my next essay",
        tool_results=[draft_result],
        history=ConversationHistory(),
        state=session_state,
        skills=skills,
    )

    # The tool's body shows up wrapped in the tool-results block, but
    # there is no "draft an essay" instruction that bypasses the tool.
    assert "Generated essay draft" in prompt
    assert "draft an essay" not in prompt.lower()
    assert "compose an essay" not in prompt.lower()


def test_build_composer_prompt_includes_paradox_guidance_when_present(
    session_state: SessionState,
    skills: VoiceSkillStack,
) -> None:
    """Tool results mentioning paradox surface the paradox-tolerance rule."""
    result = ToolResult(
        intent_type="creek.lint",
        body="paradox detected in 03-Eddies/eddy-clarity",
    )

    prompt = build_composer_prompt(
        user_message="anything?",
        tool_results=[result],
        history=ConversationHistory(),
        state=session_state,
        skills=skills,
    )

    assert "paradox" in prompt.lower()
    assert "resolution" in prompt.lower() or "name" in prompt.lower()


def test_build_composer_prompt_handles_empty_tool_results(
    session_state: SessionState, skills: VoiceSkillStack
) -> None:
    """No tool results = the composer answers from session state + skills alone."""
    prompt = build_composer_prompt(
        user_message="hi",
        tool_results=[],
        history=ConversationHistory(),
        state=session_state,
        skills=skills,
    )

    assert "hi" in prompt
    assert "tool results: (none" in prompt.lower()


async def test_composer_returns_reply(
    session_state: SessionState,
    skills: VoiceSkillStack,
    tool_results: list[ToolResult],
) -> None:
    """A successful Sonnet call returns the model's text body."""
    fake = _FakeAnthropic("Here's what's surfacing this week...")
    composer = SonnetComposer(
        anthropic_client=fake,  # type: ignore[arg-type]
        model="claude-sonnet-test",
    )

    reply = await composer.compose(
        user_message="what's surfacing?",
        tool_results=tool_results,
        history=ConversationHistory(),
        state=session_state,
        skills=skills,
    )

    assert "surfacing this week" in reply


async def test_composer_uses_configured_model(
    session_state: SessionState,
    skills: VoiceSkillStack,
) -> None:
    """The injected model name is the one passed to the Anthropic SDK."""
    fake = _FakeAnthropic("ok")
    composer = SonnetComposer(
        anthropic_client=fake,  # type: ignore[arg-type]
        model="custom-sonnet-id",
    )

    await composer.compose(
        user_message="hi",
        tool_results=[],
        history=ConversationHistory(),
        state=session_state,
        skills=skills,
    )

    assert fake.calls[0]["model"] == "custom-sonnet-id"


async def test_composer_translates_anthropic_errors(
    session_state: SessionState, skills: VoiceSkillStack
) -> None:
    """SDK errors surface as ``ComposerFailureError`` so the loop can recover."""
    import anthropic

    fake = _FakeAnthropic(anthropic.AnthropicError("simulated rate-limit"))
    composer = SonnetComposer(
        anthropic_client=fake,  # type: ignore[arg-type]
        model="claude-sonnet-test",
    )

    with pytest.raises(ComposerFailureError, match="Sonnet"):
        await composer.compose(
            user_message="hi",
            tool_results=[],
            history=ConversationHistory(),
            state=session_state,
            skills=skills,
        )


async def test_composer_handles_empty_response(
    session_state: SessionState, skills: VoiceSkillStack
) -> None:
    """A response with no text blocks raises ``ComposerFailureError``."""
    fake = _FakeAnthropic("")
    composer = SonnetComposer(
        anthropic_client=fake,  # type: ignore[arg-type]
        model="claude-sonnet-test",
    )

    with pytest.raises(ComposerFailureError, match="empty"):
        await composer.compose(
            user_message="hi",
            tool_results=[],
            history=ConversationHistory(),
            state=session_state,
            skills=skills,
        )
