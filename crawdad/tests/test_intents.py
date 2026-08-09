"""Tests for ``crawdad.intents``."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from crawdad import intents as intents_module
from crawdad.intents import (
    ACTIVATE_REGISTER_INTENT_TYPE,
    RUN_WORKFLOW_INTENT_TYPE,
    Intent,
    PrivacyTierCeiling,
    RouterResponse,
    ToolInfo,
    build_intents_schema,
)

# Every tier the enum knows about, in the order the cap table ranks them
# (most restrictive first). Parametrising over the *enum* rather than a
# hand-written list means adding a fifth tier without deciding how the
# cap treats it fails these tests instead of slipping through.
_ALL_CEILINGS = tuple(PrivacyTierCeiling)
_ALL_CEILING_IDS = [tier.value for tier in _ALL_CEILINGS]


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


# ---------------------------------------------------------------------------
# Composer privacy-tier cap (#1152)
# ---------------------------------------------------------------------------
#
# Attacker-authored text reaches the Haiku router: ``loop._record_tool_round``
# appends raw MCP tool-result bodies (vault fragments built by ``creek
# ingest`` from third-party ChatGPT / Discord / Substack exports) into the
# conversation history, and ``router.build_router_prompt`` renders that
# history verbatim into the router prompt. So the ceiling the router emits
# is untrusted input, and this module owns the single table + cap every
# other module reads.


def test_intents_schema_advertises_only_composer_admitted_ceilings() -> None:
    """The router is shown ``open`` / ``personal`` only, in rank order.

    Advertising ``intimate`` / ``all`` in the schema is an invitation:
    a poisoned tool body only has to echo a tier the schema already
    names. Rank order (not lexical ``sorted()``) so the list reads
    most-restrictive-first, matching :data:`crawdad.intents.CEILING_RANK`.
    """
    schema = build_intents_schema([])

    ceiling = schema["properties"]["intents"]["items"]["properties"][
        "privacy_tier_ceiling"
    ]

    assert ceiling["enum"] == ["open", "personal"]
    assert ceiling["default"] == "open"


@pytest.mark.parametrize("requested", _ALL_CEILINGS, ids=_ALL_CEILING_IDS)
def test_cap_ceiling_never_widens(requested: PrivacyTierCeiling) -> None:
    """``cap_ceiling`` returns an admitted tier no looser than the request.

    Two properties in one, because either alone is insufficient: the
    result must be inside the cloud-composer cap *and* must never rank
    above what was asked for. A ``return PERSONAL`` stub satisfies the
    first and fails the second at ``requested=open``.
    """
    capped = intents_module.cap_ceiling(requested)

    assert capped in intents_module.COMPOSER_ADMITTED_CEILINGS
    assert intents_module.CEILING_RANK[capped] <= intents_module.CEILING_RANK[requested]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (PrivacyTierCeiling.OPEN, PrivacyTierCeiling.OPEN),
        (PrivacyTierCeiling.PERSONAL, PrivacyTierCeiling.PERSONAL),
        (PrivacyTierCeiling.INTIMATE, PrivacyTierCeiling.PERSONAL),
        (PrivacyTierCeiling.ALL, PrivacyTierCeiling.PERSONAL),
    ],
    ids=_ALL_CEILING_IDS,
)
def test_cap_ceiling_maps_each_tier_to_an_exact_result(
    requested: PrivacyTierCeiling, expected: PrivacyTierCeiling
) -> None:
    """Pin the whole mapping by value, not by property.

    The property tests above would still pass if ``cap_ceiling``
    clamped ``personal`` down to ``open``. This one would not.
    """
    assert intents_module.cap_ceiling(requested) is expected


@pytest.mark.parametrize("requested", _ALL_CEILINGS, ids=_ALL_CEILING_IDS)
def test_cap_ceiling_is_idempotent(requested: PrivacyTierCeiling) -> None:
    """Capping an already-capped tier is a no-op.

    The dispatcher caps at the head of its intent loop and the CLI caps
    the workflow's declared ceiling; a value can therefore pass through
    the function twice on one turn without ratcheting anywhere.
    """
    once = intents_module.cap_ceiling(requested)

    assert intents_module.cap_ceiling(once) is once


def test_ceiling_rank_covers_every_member_and_open_is_the_minimum() -> None:
    """The relocated rank table is total, strict, and starts at ``open``.

    ``CEILING_RANK`` moved here from ``crawdad.loop`` because
    ``crawdad.dispatcher`` cannot import ``crawdad.loop``. A partial
    copy — three of four members, or a permuted order — would make
    ``_max_ceiling_from`` raise ``KeyError`` or silently mis-rank, so
    the table is pinned whole rather than spot-checked.
    """
    rank = intents_module.CEILING_RANK

    assert set(rank) == set(PrivacyTierCeiling)
    assert len(set(rank.values())) == len(PrivacyTierCeiling)
    assert min(rank, key=lambda tier: rank[tier]) is PrivacyTierCeiling.OPEN
    assert (
        rank[PrivacyTierCeiling.OPEN]
        < rank[PrivacyTierCeiling.PERSONAL]
        < rank[PrivacyTierCeiling.INTIMATE]
        < rank[PrivacyTierCeiling.ALL]
    )


def test_composer_admitted_ceilings_excludes_intimate_and_all() -> None:
    """The cap is exactly ``{open, personal}`` — an immutable frozenset.

    Pinned as an exact set (not a membership spot-check) so widening
    the cap is a deliberate, reviewable edit to this assertion rather
    than a silent addition nobody notices. Mirrors
    ``tests/test_workflows.py::test_workflow_admitted_ceilings_is_open_and_personal``
    — and, after #1152, the same object.
    """
    admitted = intents_module.COMPOSER_ADMITTED_CEILINGS

    assert admitted == frozenset({PrivacyTierCeiling.OPEN, PrivacyTierCeiling.PERSONAL})
    assert isinstance(admitted, frozenset)
    assert PrivacyTierCeiling.INTIMATE not in admitted
    assert PrivacyTierCeiling.ALL not in admitted


@pytest.mark.parametrize("bogus", ["ALL", "", "not-a-tier", "Open", " personal"])
def test_cap_ceiling_clamps_an_unrecognised_tier_to_open(bogus: str) -> None:
    """A string that names no tier clamps to ``open``, never raises.

    The annotation says :class:`PrivacyTierCeiling`, but pydantic's
    ``model_construct`` / ``model_copy(update=...)`` can put a bare
    ``str`` on an :class:`Intent` without ever coercing it. Subscripting
    the rank table with an unrecognised one used to raise ``KeyError``:
    safe, in that it precedes any tool call, but an uncaught exception
    that escapes the agent loop — which catches only
    ``UnknownIntentError`` / ``MCPUnavailableError`` — and silences the
    bot. That is the exact DoS the clamp exists to avoid, so an
    unnameable tier is treated as maximally suspicious instead.

    Matching is exact: wrong case and stray whitespace name no tier and
    are never repaired into one, mirroring
    ``creek_mcp.policy._as_ceiling``.

    ``cast`` rather than a type-ignore: the test deliberately drives a
    contract violation, so the call site should say so rather than
    suppress the checker.
    """
    capped = intents_module.cap_ceiling(cast("PrivacyTierCeiling", bogus))

    assert capped is PrivacyTierCeiling.OPEN


@pytest.mark.parametrize(
    ("spelled", "expected"),
    [
        ("open", PrivacyTierCeiling.OPEN),
        ("personal", PrivacyTierCeiling.PERSONAL),
        ("intimate", PrivacyTierCeiling.PERSONAL),
        ("all", PrivacyTierCeiling.PERSONAL),
    ],
)
def test_cap_ceiling_treats_a_raw_tier_string_as_its_member(
    spelled: str, expected: PrivacyTierCeiling
) -> None:
    """A correctly-spelled raw ``str`` clamps exactly like the member.

    :class:`PrivacyTierCeiling` is a ``StrEnum``, so its members hash
    and compare as their *value* — ``hash("all") == hash(
    PrivacyTierCeiling.ALL)``. An uncoerced ``"all"`` therefore reaches
    :data:`CEILING_RANK` and is clamped to ``personal`` rather than
    falling through to the unrecognised-value branch above.

    Pinned because the two branches must not be confused: if this ever
    started returning ``open`` it would look like defence-in-depth
    while actually meaning the rank table had stopped recognising its
    own tier spellings.
    """
    capped = intents_module.cap_ceiling(cast("PrivacyTierCeiling", spelled))

    assert capped is expected
