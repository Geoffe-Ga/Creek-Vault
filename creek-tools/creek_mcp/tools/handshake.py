"""``creek.handshake`` MCP tool — capability & version negotiation (#750).

A connecting client (the Adepthood app) calls this first to learn, in one
round-trip: whether a Creek vault is present, which contract and ontology
versions the server speaks, the privacy-tier model, and the list of tools
actually registered. The call is read-only and LLM-free, so it works on any
host and on a fresh (or absent) vault — the negotiation must succeed before the
client knows whether anything else will.

The capability list is supplied by the caller (``build_server`` passes the live
``server.list_tools()`` names) rather than hardcoded here, so it cannot drift
from the tools actually registered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.tier_ceiling import TierCeiling

TOOL_NAME = "creek.handshake"
SERVER_NAME = "creek-tools-mcp"
"""Mirrors ``creek_mcp.server.SERVER_NAME``; duplicated to avoid a circular import
(``server`` imports this module)."""

_CREEK_MARKER = Path("00-Creek-Meta")
TRANSPORT = "stdio"


def _content_tiers() -> list[str]:
    """Return the privacy tiers content can hold, least- to most-restricted.

    Derived from :class:`~creek.models.PrivacyTier` (minus ``unclassified``) so
    the advertised tier model tracks the real enum: ``open``, ``personal``,
    ``intimate``.
    """
    return [t.value for t in PrivacyTier if t is not PrivacyTier.UNCLASSIFIED]


def handshake_tool(
    *,
    vault_path: Path,
    capabilities: list[str],
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Negotiate vault presence, versions, tier model, and capability list.

    Audit-logs the call like every other tool (the only side effect — the
    audit substrate creates its own directory on first write), then returns a
    body-free negotiation envelope. ``available`` is computed *before* the audit
    append so a fresh vault still reports ``False`` rather than being flipped
    ``True`` by the audit directory the append creates.

    Args:
        vault_path: Vault root. Presence of ``00-Creek-Meta`` under it
            determines ``available``.
        capabilities: The tool names the server actually registered — supplied
            by ``build_server`` so the list cannot drift.
        privacy_tier_ceiling: The ceiling the caller declared; recorded and
            echoed. Handshake returns no fragment content, so the ceiling does
            not gate anything here, but it is audited like every other call.
        consumer: Free-form consumer identifier (e.g. ``"adepthood"``), recorded
            in the audit log.

    Returns:
        A dict with at least ``available``, ``contract_version``,
        ``ontology_version``, ``tiers``, and ``capabilities``, plus the
        ``tier_model``, ``transport``, and ``server`` name for the client.
    """
    available = (vault_path / _CREEK_MARKER).is_dir()
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "server": SERVER_NAME,
        "transport": TRANSPORT,
        "available": available,
        "contract_version": CONTRACT_VERSION,
        "ontology_version": ONTOLOGY_VERSION,
        "tiers": _content_tiers(),
        "tier_model": {
            "ceilings": [ceiling.value for ceiling in TierCeiling],
            "default": TierCeiling.OPEN.value,
            "intimate_never_egresses": True,
        },
        "capabilities": capabilities.copy(),
    }
