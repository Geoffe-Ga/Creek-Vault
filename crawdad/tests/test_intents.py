"""Tests for ``crawdad.intents``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from crawdad.intents import (
    Intent,
    PrivacyTierCeiling,
    RouterResponse,
    ToolInfo,
    build_intents_schema,
)


def test_intent_default_privacy_tier_is_open() -> None:
    """Per FEAT-014 §pre-decided choices, ``open`` is the default."""
    intent = Intent(type="creek.state.read")

    assert intent.privacy_tier_ceiling == PrivacyTierCeiling.OPEN
    assert intent.args == {}


def test_intent_rejects_empty_type() -> None:
    """An empty intent type is a programming error from a bad router output."""
    with pytest.raises(ValidationError):
        Intent(type="")


def test_intent_accepts_args() -> None:
    """Tool-specific args ride in ``args`` (validated by the MCP server)."""
    intent = Intent(
        type="creek.mine",
        args={"phase": "rising", "limit": 5},
        privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL,
    )

    assert intent.args == {"phase": "rising", "limit": 5}
    assert intent.privacy_tier_ceiling == PrivacyTierCeiling.PERSONAL


def test_router_response_parses_json_payload() -> None:
    """``RouterResponse`` is the strict JSON shape Haiku must emit."""
    payload: dict[str, Any] = {
        "intents": [
            {"type": "creek.state.read"},
            {"type": "creek.mine", "args": {"phase": "rising"}},
        ]
    }

    response = RouterResponse.model_validate(payload)

    assert len(response.intents) == 2
    assert response.intents[0].type == "creek.state.read"
    assert response.intents[1].args == {"phase": "rising"}


def test_router_response_accepts_empty_intents() -> None:
    """No intents = the router decided no tool calls are needed."""
    response = RouterResponse.model_validate({"intents": []})

    assert response.intents == []


def test_router_response_rejects_missing_intents_key() -> None:
    """Missing ``intents`` key is malformed router output."""
    with pytest.raises(ValidationError):
        RouterResponse.model_validate({"reply": "hello"})


def test_router_response_compose_defaults_false() -> None:
    """The FEAT-015 ``compose`` field defaults to ``False`` for back-compat."""
    response = RouterResponse(intents=[])

    assert response.compose is False


def test_router_response_compose_true_signals_termination() -> None:
    """``{intents: [], compose: true}`` is the canonical 'compose now' signal."""
    response = RouterResponse.model_validate({"intents": [], "compose": True})

    assert response.intents == []
    assert response.compose is True


def test_build_intents_schema_includes_compose_field() -> None:
    """The schema advertises the ``compose`` flag so Haiku knows to emit it."""
    schema = build_intents_schema([])

    assert "compose" in schema["properties"]
    assert schema["properties"]["compose"]["type"] == "boolean"


def test_build_intents_schema_lists_each_tool() -> None:
    """The schema's ``type`` enum reflects every advertised MCP tool."""
    tools = [
        ToolInfo(
            name="creek.state.read",
            description="Read latest.md",
            input_schema={"type": "object", "properties": {}},
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

    schema = build_intents_schema(tools)

    assert schema["type"] == "object"
    assert "intents" in schema["properties"]
    intent_item = schema["properties"]["intents"]["items"]
    assert sorted(intent_item["properties"]["type"]["enum"]) == [
        "creek.mine",
        "creek.state.read",
    ]


def test_build_intents_schema_empty_tool_list() -> None:
    """An empty registry produces a schema that allows no intent types."""
    schema = build_intents_schema([])

    intent_item = schema["properties"]["intents"]["items"]
    assert intent_item["properties"]["type"]["enum"] == []
