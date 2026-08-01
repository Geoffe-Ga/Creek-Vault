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

CREEK_MARKER = Path("00-Creek-Meta")
"""The directory whose presence means "a vault has been scaffolded here"."""

# Retained under the original private name: it predates #1074 and is referenced
# by existing tests. The public name exists because the ``/v1`` capabilities
# handler needs the same probe, and a second copy of the literal is a second
# definition of "initialised" free to drift from this one.
_CREEK_MARKER = CREEK_MARKER

TRANSPORT = "stdio"


def vault_available(vault_path: Path) -> bool:
    """Return whether a scaffolded vault is readable at *vault_path*.

    The whole readiness probe, in one predicate with no side effect. That
    matters more than it looks: :func:`handshake_tool` also appends to the
    audit log, and :meth:`creek_mcp.audit.MCPAuditLog.append` reaches a
    ``mkdir(parents=True, exist_ok=True)`` — so calling the *tool* against an
    absent vault **creates** ``00-Creek-Meta/audit/`` and makes the next probe
    answer ``True`` for a vault nobody ever initialised. Callers that only need
    the answer (notably ``GET /v1/capabilities``, which must keep the ADR's
    "present" and "uninitialised" states distinct across repeated calls) must
    use this and not reach for the tool.

    Args:
        vault_path: Vault root to probe.

    Returns:
        ``True`` when :data:`CREEK_MARKER` is a directory under *vault_path*.
    """
    return (vault_path / CREEK_MARKER).is_dir()


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
    server_name: str,
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
        server_name: The MCP server's name, injected by ``build_server`` (its
            ``SERVER_NAME``) so this module need not duplicate the literal.
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
    available = vault_available(vault_path)
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
        "server": server_name,
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
