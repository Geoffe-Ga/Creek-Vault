"""Tests for ``crawdad.router``."""

from __future__ import annotations

import json
from typing import Any

import pytest

from crawdad.history import ConversationHistory
from crawdad.intents import ACTIVATE_REGISTER_INTENT_TYPE, PrivacyTierCeiling, ToolInfo
from crawdad.router import (
    IntentRouter,
    RouterParseError,
    build_router_prompt,
)
from crawdad.state import SessionState


@pytest.fixture
def tools() -> list[ToolInfo]:
    return [
        ToolInfo(
            name="creek.state.read",
            description="Read latest.md",
            input_schema={"type": "object"},
        ),
        ToolInfo(
            name="creek.mine",
            description="Mine essay seeds.",
            input_schema={
                "type": "object",
                "properties": {"phase": {"type": "string"}},
            },
        ),
    ]


@pytest.fixture
def session_state() -> SessionState:
    return SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot="Phase: **rising** (confidence 0.84)",
        eddies=("eddy-clarity",),
        threads=("thread-voice",),
        suggested_questions=("What is surfacing?",),
    )


class _FakeAnthropic:
    """Minimal stand-in for ``anthropic.AsyncAnthropic`` used in tests."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # so router can call client.messages.create

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _FakeResponse(self._response_text)


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


def test_build_router_prompt_includes_wavelength_and_history(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """The prompt names the wavelength snapshot, history, and tools."""
    history = ConversationHistory()
    history.append("user", "what's surfacing?")

    prompt = build_router_prompt(
        message="what about now?",
        history=history,
        state=session_state,
        tools=tools,
    )

    assert "rising" in prompt
    assert "what's surfacing?" in prompt
    assert "creek.state.read" in prompt
    assert "creek.mine" in prompt
    assert "what about now?" in prompt


def test_build_router_prompt_handles_missing_state(tools: list[ToolInfo]) -> None:
    """A ``None`` session_state degrades gracefully — no wavelength line."""
    prompt = build_router_prompt(
        message="hi",
        history=ConversationHistory(),
        state=None,
        tools=tools,
    )

    assert "hi" in prompt
    # No wavelength prefix when state is unavailable.
    assert "wavelength" not in prompt.lower() or "no wavelength" in prompt.lower()


def test_build_router_prompt_phase_aware_instruction(
    tools: list[ToolInfo],
) -> None:
    """Bottoming-Out phases steer Haiku away from mine/draft (ADOPT-008)."""
    state = SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot="Phase: **bottoming-out** (confidence 0.71)",
        eddies=(),
        threads=(),
        suggested_questions=(),
    )

    prompt = build_router_prompt(
        message="anything",
        history=ConversationHistory(),
        state=state,
        tools=tools,
    )

    assert "bottoming-out" in prompt
    assert "compost" in prompt.lower() or "paradox" in prompt.lower()


async def test_router_extract_intents_parses_valid_json(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """A well-formed Haiku reply lands on a :class:`RouterResponse`."""
    fake_haiku = _FakeAnthropic(
        json.dumps(
            {
                "intents": [
                    {
                        "type": "creek.state.read",
                        "privacy_tier_ceiling": "open",
                    }
                ]
            }
        )
    )
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="claude-haiku-test",
        tools=tools,
    )

    response = await router.extract_intents(
        message="what's surfacing?",
        history=ConversationHistory(),
        state=session_state,
    )

    assert len(response.intents) == 1
    assert response.intents[0].type == "creek.state.read"
    assert response.intents[0].privacy_tier_ceiling == PrivacyTierCeiling.OPEN


async def test_router_uses_configured_model(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """The injected model name is the one passed to the Anthropic SDK."""
    fake_haiku = _FakeAnthropic(json.dumps({"intents": []}))
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="custom-haiku-id",
        tools=tools,
    )

    await router.extract_intents(
        message="hi",
        history=ConversationHistory(),
        state=session_state,
    )

    assert fake_haiku.calls[0]["model"] == "custom-haiku-id"


async def test_router_rejects_non_json_response(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """A non-JSON Haiku reply raises ``RouterParseError``."""
    fake_haiku = _FakeAnthropic("here is some prose, not JSON")
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="claude-haiku-test",
        tools=tools,
    )

    with pytest.raises(RouterParseError, match="JSON"):
        await router.extract_intents(
            message="hi",
            history=ConversationHistory(),
            state=session_state,
        )


async def test_router_rejects_missing_intents_key(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """JSON without the ``intents`` array is malformed router output."""
    fake_haiku = _FakeAnthropic(json.dumps({"reply": "hi"}))
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="claude-haiku-test",
        tools=tools,
    )

    with pytest.raises(RouterParseError):
        await router.extract_intents(
            message="hi",
            history=ConversationHistory(),
            state=session_state,
        )


class _RaisingAnthropic:
    """``messages.create`` raises an injected exception, modeling SDK errors."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.messages = self

    async def create(self, **_kwargs: Any) -> Any:
        raise self._exc


async def test_router_translates_anthropic_api_error_to_parse_error(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """Rate-limit / connection / auth errors surface as ``RouterParseError``.

    The bot maps ``RouterParseError`` to a user-visible reply; without
    this translation the SDK error would propagate through
    ``handle_message`` into discord.py and the user would receive no
    reply at all.
    """
    import anthropic

    # ``APIError`` requires a request kwarg in modern SDK versions; we
    # construct a minimal subclass exception that still belongs to the
    # base ``AnthropicError`` hierarchy.
    fake_haiku = _RaisingAnthropic(
        anthropic.AnthropicError("simulated rate-limit / API failure")
    )
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="claude-haiku-test",
        tools=tools,
    )

    with pytest.raises(RouterParseError, match="Haiku API call failed"):
        await router.extract_intents(
            message="hi",
            history=ConversationHistory(),
            state=session_state,
        )


def test_build_router_prompt_mentions_register_switch_intent(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """FEAT-029: the prompt explains how to emit the activate_register intent.

    Without the instruction, Haiku will try to satisfy "switch to the
    analytic register" by hallucinating an MCP tool name. The prompt
    must name the client-side intent type explicitly.
    """
    prompt = build_router_prompt(
        message="switch to the analytic register",
        history=ConversationHistory(),
        state=session_state,
        tools=tools,
    )

    assert ACTIVATE_REGISTER_INTENT_TYPE in prompt
    assert "register" in prompt.lower()
    assert "switch" in prompt.lower() or "activate" in prompt.lower()


async def test_router_emits_activate_register_for_natural_language_phrase(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """A switch phrase yields an activate_register intent.

    With the prompt updated to teach Haiku about the intent type, a
    well-behaved Haiku reply emits ``crawdad.activate_register`` with
    the requested register name. We stub the model so the test pins
    the parsing path, not the model's behavior.
    """
    fake_haiku = _FakeAnthropic(
        json.dumps(
            {
                "intents": [
                    {
                        "type": ACTIVATE_REGISTER_INTENT_TYPE,
                        "args": {"register": "analytic"},
                    }
                ],
                "compose": True,
            }
        )
    )
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="claude-haiku-test",
        tools=tools,
    )

    response = await router.extract_intents(
        message="switch to the analytic voice",
        history=ConversationHistory(),
        state=session_state,
    )

    assert len(response.intents) == 1
    assert response.intents[0].type == ACTIVATE_REGISTER_INTENT_TYPE
    assert response.intents[0].args == {"register": "analytic"}
    assert response.compose is True


async def test_router_extracts_fenced_json_blocks(
    tools: list[ToolInfo], session_state: SessionState
) -> None:
    """Markdown fences around the JSON are tolerated."""
    payload = json.dumps({"intents": [{"type": "creek.state.read"}]})
    fake_haiku = _FakeAnthropic(
        f"Sure, here is the result:\n\n```json\n{payload}\n```\n"
    )
    router = IntentRouter(
        anthropic_client=fake_haiku,  # type: ignore[arg-type]
        model="claude-haiku-test",
        tools=tools,
    )

    response = await router.extract_intents(
        message="hi",
        history=ConversationHistory(),
        state=session_state,
    )

    assert response.intents[0].type == "creek.state.read"
