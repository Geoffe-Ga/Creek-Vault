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

import logging
from pathlib import Path
from typing import Any

from creek.models import PrivacyTier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.tier_ceiling import TierCeiling

logger = logging.getLogger(__name__)

TOOL_NAME = "creek.handshake"

CREEK_MARKER = Path("00-Creek-Meta")
"""The directory whose presence means "a vault has been scaffolded here"."""

TRANSPORT = "stdio"


def vault_available(vault_path: Path) -> bool:
    """Return whether a scaffolded vault is readable at *vault_path*.

    The whole readiness probe, in one predicate with no side effect, so callers
    that only need the answer — notably ``GET /v1/capabilities``, which must keep
    the ADR's "present" and "uninitialised" states distinct across repeated
    calls — can ask without entering the tool.

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


def _record_call(
    vault_path: Path,
    *,
    available: bool,
    privacy_tier_ceiling: TierCeiling,
    consumer: str,
) -> None:
    """Record this handshake somewhere that does not falsify what it reports.

    The vault's audit log is the right home for the entry **when there is a
    vault**. When there is not, it is the wrong home by construction:
    ``mcp.jsonl`` lives at ``00-Creek-Meta/audit/mcp.jsonl`` and
    :meth:`creek.audit.AuditLog.append` does ``mkdir(parents=True,
    exist_ok=True)``, so writing the entry creates the marker directory whose
    absence the entry is about — and the next handshake then reports
    ``available: true`` for a vault nobody initialised (#1108). Creating the
    vault in order to store the trail falsifies the state the trail describes.

    So the entry moves rather than disappearing. Dropping it silently would
    trade one dishonesty for another: an operator polling an uninitialised vault
    would find no evidence they had ever called. The process log is the honest
    third option the issue asks for — it is the one surface that exists whether
    or not the vault does.

    Why this guard sits here and not in :class:`creek.audit.AuditLog`: that
    ``mkdir`` is the only one in either audit module, and it is on the path of
    all thirty-one ``MCPAuditLog(vault_path).append(...)`` call sites (this one
    included) plus five direct :class:`~creek.audit.AuditLog` construction sites
    — the privacy filter, the purge and redact audit logs, the compile engine
    and the vault writer's provenance log. Making it no-create by default
    changes behaviour for every one of them, and making it opt-in collides with
    :func:`creek_mcp.audit._shared_audit_log`, whose ``lru_cache`` is keyed on
    the log path alone (#1126) — one path would need two log objects with
    different creation policies, or a wider key that reinstates the cold-cache
    re-read that cache exists to remove. Handshake is also the only tool whose
    *return value* is a claim about the marker, so it is the only one that can
    contradict itself. Tools that scaffold a vault root they were pointed at by
    mistake remain a real defect, and a separate one.

    Args:
        vault_path: Vault root the call was made against.
        available: Whether a scaffolded vault was found there.
        privacy_tier_ceiling: The ceiling the caller declared.
        consumer: Free-form consumer identifier.
    """
    if not available:
        logger.info(
            "%s: no Creek vault at %s — recording the call here rather than in "
            "an audit log that would have to create the vault it reports "
            "missing (consumer=%s, tier_ceiling=%s)",
            TOOL_NAME,
            vault_path,
            consumer,
            privacy_tier_ceiling.value,
        )
        return
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )


def handshake_tool(
    *,
    vault_path: Path,
    capabilities: list[str],
    server_name: str,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Negotiate vault presence, versions, tier model, and capability list.

    Read-only with respect to the vault, and — since #1108 — read-only with
    respect to whether the vault *exists*. Against a scaffolded vault the call
    is audit-logged like every other tool (the only side effect, into a
    directory the audit substrate creates on first write); against a vault that
    is not there the record goes to the process log instead, because writing it
    to ``00-Creek-Meta/audit/`` would conjure the marker this function's
    ``available`` reports missing. :func:`_record_call` carries the reasoning.

    The probe still runs before the record, which is what keeps the *first*
    call honest; the guard in :func:`_record_call` is what keeps every call
    after it honest.

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
    _record_call(
        vault_path,
        available=available,
        privacy_tier_ceiling=privacy_tier_ceiling,
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
