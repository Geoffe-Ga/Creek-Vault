"""Characterisation lock for the MCP tool-registration surface (#1385).

``build_server`` publishes 24 tools whose entire advertised contract —
name, order, ``inputSchema`` and ``description`` — is derived by FastMCP
from the 24 registration closures' own signatures and docstrings. Nothing
in the suite pinned that surface as a whole: every existing consumer of
``server.list_tools()`` compares a *set* of names
(``tests/test_wiring_contract.py``, ``tests/test_mcp_server.py``,
``tests/test_mcp_contract_adr_shipped_surface.py``) or a *count*
(``tests/test_mcp_read_gate.py``). A refactor that permuted the
registration order, dropped a default, reflowed a docstring or renamed a
closure would pass all of them.

This module is the missing lock, and it exists because #1385 moves those
24 registrations out of ``build_server`` into four module-level
``_register_*_tools`` helpers. The three characterisation suites below --
order, ``inputSchema`` and ``description`` -- were written and proven
green against the *unrefactored* server, in a commit of their own, so
that the move had something to be measured against.

:func:`test_no_top_level_function_nests_more_than_eight_defs` at the foot
of the module is the one exception and is deliberately the opposite: it
was added second and was **red** against the unrefactored server, which
is what made it a real guard rather than a restatement of the status quo.
It cannot pass pre-refactor, because pre-refactor ``build_server`` is
itself the thing it refuses and the four registrars do not yet exist.
That is why its registrar probes use :func:`hasattr` rather than a
module-level import -- so it could go red without turning this whole file
into a collection error and destroying the evidence above.

Three literal tables are the pin, and **nothing here recomputes them from
the live server**:

* :data:`_EXPECTED_ORDER` — the 24 names in registration order, as a
  ``tuple``. ``FastMCP.list_tools()`` yields its registry's insertion
  order, so regrouping the registrations is exactly what can permute it.
* :data:`_EXPECTED_SCHEMAS` — the **full** ``inputSchema`` per tool.
  Full dicts rather than a digest on purpose: ``title`` is derived from
  the closure's ``__name__`` (``_handshake`` renders as
  ``"_handshakeArguments"``), so pinning the whole dict is what locks the
  closure names, and the live property shapes include ``$ref``, ``anyOf``,
  ``items`` and bare ``type`` — which no hand-rolled projection covers
  reliably.
* :data:`_EXPECTED_DESCRIPTIONS` — the summary line FastMCP takes from
  each closure docstring. A reflowed docstring during a "pure move" is
  precisely the silent edit this pin exists to exclude.

**Known, deliberate fragility.** 19 of the 24 schemas embed
``$defs.TierCeiling`` carrying :class:`~creek_mcp.policy.TierCeiling`'s own
class docstring verbatim, and the schemas are rendered by pydantic. An
``mcp`` or ``pydantic`` bump, or an edit to that enum's docstring, will
turn this module red. That is a *contract-moved* signal, not flakiness:
the advertised surface really did change, and the reader (most likely a
Dependabot PR) should regenerate the tables deliberately rather than
loosen the assertions.

The transport half of the handshake is **not** re-tested here;
``tests/test_mcp_server.py::test_the_registered_handshake_reports_the_servers_own_transport``
already drives it through ``server.call_tool`` parametrized over both
transports (#1583).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp.policy import Transport
from creek_mcp.server import build_server

if TYPE_CHECKING:
    from mcp.types import Tool

_TIER_CEILING_DEFS: Final[dict[str, Any]] = {
    "TierCeiling": {
        "description": (
            "MCP-side ceiling parameter values.\n\nOrdering: ``OPEN`` is the "
            "most restrictive (only ``open`` content is\nvisible) and "
            "``ALL`` is the broadest (every tier is visible, including\n"
            "``intimate``). ``unclassified`` is not an ``ALL``-only tier — "
            "it ranks\nwith ``personal`` (#961), so ``PERSONAL``, "
            "``INTIMATE`` and ``ALL`` all\nadmit it and ``OPEN`` alone "
            "refuses it."
        ),
        "enum": [
            "open",
            "personal",
            "intimate",
            "all",
        ],
        "title": "TierCeiling",
        "type": "string",
    },
}


_EXPECTED_ORDER: Final[tuple[str, ...]] = (
    "creek.handshake",
    "creek.reflect",
    "creek.wheel",
    "creek.journal",
    "creek.upload",
    "creek.state.read",
    "creek.state.render",
    "creek.lint",
    "creek.mine",
    "creek.draft",
    "creek.author",
    "creek.save",
    "creek.ingest",
    "creek.redact.scan",
    "creek.classify",
    "creek.link",
    "creek.report",
    "creek.skills.refresh",
    "creek.compile",
    "creek.purge.fragment",
    "creek.purge.source",
    "creek.purge.classifications",
    "creek.purge.daterange",
    "creek.purge.vault",
)


_EXPECTED_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "creek.handshake": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_handshakeArguments",
        "type": "object",
    },
    "creek.reflect": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "content": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Content",
            },
            "entry_ref": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Entry Ref",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_reflectArguments",
        "type": "object",
    },
    "creek.wheel": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_wheelArguments",
        "type": "object",
    },
    "creek.journal": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "content": {
                "title": "Content",
                "type": "string",
            },
            "external_id": {
                "title": "External Id",
                "type": "string",
            },
            "timestamp": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Timestamp",
            },
            "tier": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Tier",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "content",
            "external_id",
        ],
        "title": "_journalArguments",
        "type": "object",
    },
    "creek.upload": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "filename": {
                "title": "Filename",
                "type": "string",
            },
            "content_base64": {
                "title": "Content Base64",
                "type": "string",
            },
            "external_id": {
                "title": "External Id",
                "type": "string",
            },
            "timestamp": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Timestamp",
            },
            "tier": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Tier",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "filename",
            "content_base64",
            "external_id",
        ],
        "title": "_uploadArguments",
        "type": "object",
    },
    "creek.state.read": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_state_readArguments",
        "type": "object",
    },
    "creek.state.render": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_state_renderArguments",
        "type": "object",
    },
    "creek.lint": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
            "checks": {
                "anyOf": [
                    {
                        "items": {
                            "type": "string",
                        },
                        "type": "array",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Checks",
            },
            "since": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Since",
            },
        },
        "title": "_lintArguments",
        "type": "object",
    },
    "creek.mine": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
            "phase": {
                "default": "unclassified",
                "title": "Phase",
                "type": "string",
            },
            "limit": {
                "default": 10,
                "title": "Limit",
                "type": "integer",
            },
        },
        "title": "_mineArguments",
        "type": "object",
    },
    "creek.draft": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
            "phase": {
                "default": "unclassified",
                "title": "Phase",
                "type": "string",
            },
            "index": {
                "default": 0,
                "title": "Index",
                "type": "integer",
            },
        },
        "title": "_draftArguments",
        "type": "object",
    },
    "creek.author": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "query": {
                "title": "Query",
                "type": "string",
            },
            "medium": {
                "default": "research",
                "title": "Medium",
                "type": "string",
            },
            "max_rounds": {
                "anyOf": [
                    {
                        "type": "integer",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Max Rounds",
            },
            "dry_run": {
                "default": False,
                "title": "Dry Run",
                "type": "boolean",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "query",
        ],
        "title": "_authorArguments",
        "type": "object",
    },
    "creek.save": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "target": {
                "title": "Target",
                "type": "string",
            },
            "body": {
                "title": "Body",
                "type": "string",
            },
            "title": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Title",
            },
            "tier": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Tier",
            },
            "provenance": {
                "anyOf": [
                    {
                        "items": {
                            "type": "string",
                        },
                        "type": "array",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Provenance",
            },
            "source_kind": {
                "default": "mcp",
                "title": "Source Kind",
                "type": "string",
            },
            "source_id": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Source Id",
            },
            "saved_by": {
                "default": "mcp",
                "title": "Saved By",
                "type": "string",
            },
            "full_body": {
                "default": False,
                "title": "Full Body",
                "type": "boolean",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "target",
            "body",
        ],
        "title": "_saveArguments",
        "type": "object",
    },
    "creek.ingest": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "source_type": {
                "title": "Source Type",
                "type": "string",
            },
            "input_path": {
                "title": "Input Path",
                "type": "string",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "source_type",
            "input_path",
        ],
        "title": "_ingestArguments",
        "type": "object",
    },
    "creek.redact.scan": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "input_path": {
                "title": "Input Path",
                "type": "string",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "input_path",
        ],
        "title": "_redact_scanArguments",
        "type": "object",
    },
    "creek.classify": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "method": {
                "default": "rules",
                "title": "Method",
                "type": "string",
            },
            "force": {
                "default": False,
                "title": "Force",
                "type": "boolean",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_classifyArguments",
        "type": "object",
    },
    "creek.link": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "method": {
                "default": "embeddings",
                "title": "Method",
                "type": "string",
            },
            "rebuild": {
                "default": False,
                "title": "Rebuild",
                "type": "boolean",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_linkArguments",
        "type": "object",
    },
    "creek.report": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "report_type": {
                "default": "tags",
                "title": "Report Type",
                "type": "string",
            },
            "period": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Period",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_reportArguments",
        "type": "object",
    },
    "creek.skills.refresh": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "title": "_skills_refreshArguments",
        "type": "object",
    },
    "creek.compile": {
        "$defs": _TIER_CEILING_DEFS,
        "properties": {
            "fragment_ids": {
                "items": {
                    "type": "string",
                },
                "title": "Fragment Ids",
                "type": "array",
            },
            "target_kind": {
                "title": "Target Kind",
                "type": "string",
            },
            "target_id": {
                "title": "Target Id",
                "type": "string",
            },
            "target_title": {
                "title": "Target Title",
                "type": "string",
            },
            "privacy_tier_ceiling": {
                "$ref": "#/$defs/TierCeiling",
                "default": "open",
            },
        },
        "required": [
            "fragment_ids",
            "target_kind",
            "target_id",
            "target_title",
        ],
        "title": "_compileArguments",
        "type": "object",
    },
    "creek.purge.fragment": {
        "properties": {
            "fragment_id": {
                "title": "Fragment Id",
                "type": "string",
            },
            "auth_token": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Auth Token",
            },
            "dry_run": {
                "default": False,
                "title": "Dry Run",
                "type": "boolean",
            },
        },
        "required": [
            "fragment_id",
        ],
        "title": "_purge_fragmentArguments",
        "type": "object",
    },
    "creek.purge.source": {
        "properties": {
            "source_type": {
                "title": "Source Type",
                "type": "string",
            },
            "auth_token": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Auth Token",
            },
            "dry_run": {
                "default": False,
                "title": "Dry Run",
                "type": "boolean",
            },
        },
        "required": [
            "source_type",
        ],
        "title": "_purge_sourceArguments",
        "type": "object",
    },
    "creek.purge.classifications": {
        "properties": {
            "auth_token": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Auth Token",
            },
            "dry_run": {
                "default": False,
                "title": "Dry Run",
                "type": "boolean",
            },
        },
        "title": "_purge_classificationsArguments",
        "type": "object",
    },
    "creek.purge.daterange": {
        "properties": {
            "start": {
                "title": "Start",
                "type": "string",
            },
            "end": {
                "title": "End",
                "type": "string",
            },
            "auth_token": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Auth Token",
            },
            "dry_run": {
                "default": False,
                "title": "Dry Run",
                "type": "boolean",
            },
        },
        "required": [
            "start",
            "end",
        ],
        "title": "_purge_daterangeArguments",
        "type": "object",
    },
    "creek.purge.vault": {
        "properties": {
            "confirm_vault_path": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Confirm Vault Path",
            },
            "auth_token": {
                "anyOf": [
                    {
                        "type": "string",
                    },
                    {
                        "type": "null",
                    },
                ],
                "default": None,
                "title": "Auth Token",
            },
            "dry_run": {
                "default": False,
                "title": "Dry Run",
                "type": "boolean",
            },
        },
        "title": "_purge_vaultArguments",
        "type": "object",
    },
}


_EXPECTED_DESCRIPTIONS: Final[dict[str, str]] = {
    "creek.handshake": (
        "Negotiate vault presence, versions, tier model, and capabilities."
    ),
    "creek.reflect": (
        "Return anchored Higher-Self margin notes on a single journal entry."
    ),
    "creek.wheel": "Return a per-frequency balance read of the corpus for the Map.",
    "creek.journal": (
        "Ingest one journal entry as a fragment, idempotently; ``tier`` required."
    ),
    "creek.upload": "Stage one uploaded document and ingest it; ``tier`` required.",
    "creek.state.read": "Return the latest 00-Creek-Meta/State/latest.md content.",
    "creek.state.render": "Re-render the audit report (the expensive path).",
    "creek.lint": "Run the unified hygiene lint pass.",
    "creek.mine": "Mine essay seeds from the vault.",
    "creek.draft": "Generate an essay draft from a mined idea.",
    "creek.author": (
        "Author a draft for a query via the Writing Desk.\n\n        "
        "``max_rounds`` defaults to the vault's "
        "``author.max_author_rounds`` (3).\n        "
    ),
    "creek.save": (
        "Save a Discord/Claude answer back into the vault; ``tier`` required."
    ),
    "creek.ingest": "Ingest a single source into the vault.",
    "creek.redact.scan": (
        "Read-only PII / secret scan of the FEAT-027 staging subtree.\n\n"
        "        Scoped to ``00-Creek-Meta/Inbound/``, which every "
        "ceiling admits. The\n        scan reads no per-file privacy "
        "tier, so any other vault path is ranked\n        as intimate "
        "content and needs privacy_tier_ceiling=intimate or all.\n      "
        "  "
    ),
    "creek.classify": "Re-classify existing fragments via rules or LLM.",
    "creek.link": "Run a single linker stage.",
    "creek.report": (
        "Generate a vault-state report; `period` is for `wavelength` only."
    ),
    "creek.skills.refresh": "Regenerate the voice-skill tree.",
    "creek.compile": "Roll fragments up into a compiled-layer page (FEAT-003).",
    "creek.purge.fragment": (
        "Delete one fragment by ID (elevated authorization required)."
    ),
    "creek.purge.source": (
        "Delete every fragment from *source_type* (elevated auth required)."
    ),
    "creek.purge.classifications": (
        "Reset classification metadata vault-wide (elevated auth required)."
    ),
    "creek.purge.daterange": (
        "Delete fragments created in ``[start, end]`` (elevated auth required)."
    ),
    "creek.purge.vault": (
        "Destroy all vault content (elevated auth + path confirmation)."
    ),
}


@pytest.fixture(scope="module")
def registered_tools(tmp_path_factory: pytest.TempPathFactory) -> list[Tool]:
    """Return the live MCP tool surface as a list, in registration order.

    Built once per module from a throwaway vault, mirroring the proven
    idiom at ``tests/test_mcp_read_gate.py``'s ``registered_tools``: the
    schemas are derived from the closure signatures at registration time,
    so nothing here depends on vault contents. A **list**, not a dict, so
    the order this module exists to pin survives the fixture.

    Args:
        tmp_path_factory: pytest's module-scoped temporary directory maker.

    Returns:
        Every registered :class:`~mcp.types.Tool`, in registration order.
    """
    vault = tmp_path_factory.mktemp("registration-surface")
    for sub in ("00-Creek-Meta/audit", "01-Fragments/Notes", "creek-skills"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    server = build_server(
        transport=Transport.STDIO,
        vault_path=vault,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    return asyncio.run(server.list_tools())


def _structured(result: object) -> dict[str, Any]:
    """Pull the structured-content dict out of a FastMCP ``call_tool`` result.

    Written type-safely rather than copying the
    ``# type: ignore[return-value, index]`` that the equivalent helper in
    ``tests/test_mcp_server.py`` carries: a suppression is a bypass, and
    the narrowing costs two lines.

    Args:
        result: Whatever ``FastMCP.call_tool`` returned.

    Returns:
        The structured payload as a dict.
    """
    payload = result[1] if isinstance(result, tuple) else result
    assert isinstance(payload, dict)
    return payload


def test_registration_order_is_pinned(registered_tools: list[Tool]) -> None:
    """The 24 tools register in exactly today's order.

    Compared as a **tuple**, never a set and never ``sorted()``: order is
    the property that a regrouping of 24 registrations can silently
    permute, and it is unpinned everywhere else in the suite.
    ``creek.handshake`` itself sorts the ``capabilities`` list it
    publishes (``creek_mcp/server.py``), so a permutation is invisible in
    the handshake payload too.

    Args:
        registered_tools: The live tool surface, in registration order.
    """
    assert len(registered_tools) == 24
    assert tuple(tool.name for tool in registered_tools) == _EXPECTED_ORDER


def test_the_pinned_table_covers_the_whole_surface(
    registered_tools: list[Tool],
) -> None:
    """Every live tool is pinned, and every pin names a live tool.

    The anti-vacuous guard for the two parametrized suites below: without
    it, a table that quietly lost entries would still report green,
    because a shrinking parametrize list simply runs fewer cases.

    Args:
        registered_tools: The live tool surface, in registration order.
    """
    live = {tool.name for tool in registered_tools}
    assert len(registered_tools) == 24
    assert len(_EXPECTED_ORDER) == 24
    assert len(_EXPECTED_SCHEMAS) == 24
    assert len(_EXPECTED_DESCRIPTIONS) == 24
    assert set(_EXPECTED_ORDER) == live
    assert set(_EXPECTED_SCHEMAS) == live
    assert set(_EXPECTED_DESCRIPTIONS) == live


@pytest.mark.parametrize("tool_name", _EXPECTED_ORDER)
def test_input_schema_is_pinned(registered_tools: list[Tool], tool_name: str) -> None:
    """Each tool advertises byte-identical JSON Schema for its arguments.

    Parametrized over the **literal** :data:`_EXPECTED_ORDER` rather than
    anything derived from the live server, so the case list cannot
    silently empty. The full dict is compared, which is what makes the
    ``title`` field — derived from the closure's ``__name__`` — a pin on
    the closure names themselves.

    Args:
        registered_tools: The live tool surface, in registration order.
        tool_name: The tool under test.
    """
    live = {tool.name: tool for tool in registered_tools}
    assert live[tool_name].inputSchema == _EXPECTED_SCHEMAS[tool_name]


@pytest.mark.parametrize("tool_name", _EXPECTED_ORDER)
def test_description_is_pinned(registered_tools: list[Tool], tool_name: str) -> None:
    """Each tool publishes byte-identical prose to its consumers.

    FastMCP lifts this straight from the registration closure's
    docstring, so it is the cheapest available detector of a docstring
    reflowed during a supposedly pure move.

    Args:
        registered_tools: The live tool surface, in registration order.
        tool_name: The tool under test.
    """
    live = {tool.name: tool for tool in registered_tools}
    assert live[tool_name].description == _EXPECTED_DESCRIPTIONS[tool_name]


def test_the_handshake_still_names_the_server(tmp_path: Path) -> None:
    """``SERVER_NAME`` still reaches the handshake as a module constant.

    ``SERVER_NAME`` (``creek_mcp/server.py``) is resolved from module
    globals at both of its use sites — the ``_BoundedFastMCP`` constructor
    call in ``build_server`` and the ``server_name=`` argument inside the
    ``creek.handshake`` closure — rather than closed over, so it must
    survive the #1385 move untouched. Both ends are asserted: the server's
    own ``.name``, and the value the handshake actually puts on the wire.

    Note the wire key is ``server``, not ``server_name``: ``SERVER_NAME``
    is *passed* to ``handshake_tool`` as ``server_name=`` and published
    under ``server``.

    The *transport* half of the same payload is already pinned,
    parametrized over both transports, by
    ``tests/test_mcp_server.py::test_the_registered_handshake_reports_the_servers_own_transport``.

    Args:
        tmp_path: A throwaway vault root.
    """
    for sub in ("00-Creek-Meta/audit", "01-Fragments/Notes", "creek-skills"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    server = build_server(
        transport=Transport.STDIO,
        vault_path=tmp_path,
        draft_llm_factory=lambda tier: lambda prompt: "ignored",
    )
    structured = _structured(
        asyncio.run(
            server.call_tool("creek.handshake", {"privacy_tier_ceiling": "open"}),
        )
    )
    assert server.name == "creek-tools-mcp"
    assert structured["server"] == "creek-tools-mcp"


_MAX_NESTED_DEFS: Final[int] = 8

_REGISTRARS: Final[tuple[str, ...]] = (
    "_register_conversation_tools",
    "_register_authoring_tools",
    "_register_pipeline_tools",
    "_register_purge_tools",
)


def _nested_def_counts() -> dict[str, int]:
    """Count nested ``def``\\ s per module-level function in ``creek_mcp.server``.

    Parses the shipped source rather than introspecting objects, which is
    the idiom already used at ``tests/test_mcp_auth.py``,
    ``tests/test_mcp_read_gate.py`` and
    ``tests/test_vault_config_resolver.py``.

    Returns:
        Every module-level function name mapped to the number of
        ``def``/``async def`` nodes anywhere inside its body.
    """
    server_mod = importlib.import_module("creek_mcp.server")
    source = Path(server_mod.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    counts: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        counts[node.name] = (
            sum(
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                for child in ast.walk(node)
            )
            - 1
        )
    return counts


def test_no_top_level_function_nests_more_than_eight_defs() -> None:
    """Tool registration lives in module-level registrars, not in the constructor.

    This is the guard for #1385 and it stands in for a C901 gate the repo
    does not have: ``[tool.ruff.lint] select`` in ``pyproject.toml`` omits
    ``C901`` (adding it is deferred to a follow-up because two *test*
    files would fail it immediately), and ``scripts/complexity.sh`` runs
    ``xenon`` over ``creek/`` only, never ``creek_mcp/``. So the class of
    defect had no gate at all.

    A nested ``def`` is exactly what ruff's mccabe adds one for, which is
    why this counts the growth mechanism rather than a derived score:
    ``build_server`` scored ``26 = 1 base + 24 nested defs + 1 if``.

    The registrar probes use :func:`hasattr` rather than a module-level
    ``from creek_mcp.server import _register_...``. A top-of-module import
    of a not-yet-existing symbol turns this whole file into a collection
    error, which would have destroyed the evidence that the
    characterisation tables above were green against unrefactored code.

    The sum -- not the per-registrar vector -- is asserted: the sum catches
    a dropped or duplicated registration without freezing today's grouping
    into a permanent test that a fifth registrar would have to edit.
    """
    counts = _nested_def_counts()
    offenders = {
        name: count for name, count in counts.items() if count > _MAX_NESTED_DEFS
    }
    assert not offenders, (
        f"creek_mcp/server.py: {offenders} nests more than "
        f"{_MAX_NESTED_DEFS} defs - registration belongs in a module-level "
        "_register_*_tools helper, not in the constructor; split the group "
        "or add a registrar rather than raising this ceiling"
    )

    server_mod = importlib.import_module("creek_mcp.server")
    missing = [name for name in _REGISTRARS if not hasattr(server_mod, name)]
    assert not missing, f"creek_mcp.server is missing registrars: {missing}"
    assert sum(counts[name] for name in _REGISTRARS) == 24, (
        "the four registrars must between them hold exactly the 24 tool "
        f"closures, got {[(n, counts[n]) for n in _REGISTRARS]}"
    )
