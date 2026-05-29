"""Tests for ``crawdad.intents``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from crawdad.intents import (
    ACTIVATE_REGISTER_INTENT_TYPE,
    RUN_WORKFLOW_INTENT_TYPE,
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
    enum = intent_item["properties"]["type"]["enum"]
    assert "creek.mine" in enum
    assert "creek.state.read" in enum


def test_build_intents_schema_empty_tool_list() -> None:
    """Empty tool list still permits the client-side intents.

    The FEAT-029 activate_register and ADAPT-003 run_workflow intents
    are handled locally by the dispatcher — they don't require an MCP
    tool — so their types must appear in the schema's ``enum``
    regardless of which tools the MCP server advertises.
    """
    schema = build_intents_schema([])

    intent_item = schema["properties"]["intents"]["items"]
    assert intent_item["properties"]["type"]["enum"] == [
        ACTIVATE_REGISTER_INTENT_TYPE,
        RUN_WORKFLOW_INTENT_TYPE,
    ]


def test_build_intents_schema_includes_activate_register_intent_type() -> None:
    """FEAT-029: the schema enumerates the client-side register-switch intent."""
    tools = [
        ToolInfo(
            name="creek.state.read",
            description="Read latest.md",
            input_schema={"type": "object"},
        ),
    ]

    schema = build_intents_schema(tools)

    enum = schema["properties"]["intents"]["items"]["properties"]["type"]["enum"]
    assert ACTIVATE_REGISTER_INTENT_TYPE in enum
    assert "creek.state.read" in enum


def test_activate_register_intent_type_constant_value() -> None:
    """The constant uses the ``crawdad.`` prefix to distinguish from MCP tools.

    MCP tools are namespaced ``creek.*`` so the prefix flip makes this
    intent visually distinct in router output and immune to a future
    MCP tool collision.
    """
    assert ACTIVATE_REGISTER_INTENT_TYPE == "crawdad.activate_register"
    assert ACTIVATE_REGISTER_INTENT_TYPE.startswith("crawdad.")


def test_build_intents_schema_includes_run_workflow_intent_type() -> None:
    """ADAPT-003: the schema enumerates the client-side run-workflow intent."""
    tools = [
        ToolInfo(
            name="creek.state.read",
            description="Read latest.md",
            input_schema={"type": "object"},
        ),
    ]

    schema = build_intents_schema(tools)

    enum = schema["properties"]["intents"]["items"]["properties"]["type"]["enum"]
    assert RUN_WORKFLOW_INTENT_TYPE in enum
    assert "creek.state.read" in enum


def test_run_workflow_intent_type_constant_value() -> None:
    """The constant uses the ``crawdad.`` prefix, mirroring activate_register.

    MCP tools are namespaced ``creek.*`` so the prefix flip makes this
    intent visually distinct in router output and immune to a future
    MCP tool collision.
    """
    assert RUN_WORKFLOW_INTENT_TYPE == "crawdad.run_workflow"
    assert RUN_WORKFLOW_INTENT_TYPE.startswith("crawdad.")
