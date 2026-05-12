"""MCP-side audit log (FEAT-010).

Every MCP tool invocation appends one entry to
``<vault>/00-Creek-Meta/audit/mcp.jsonl``. The module exists as its own
boundary from day 1 — FEAT-011 adds write-side fields; FEAT-012 hardens
further if needed. The storage layer already delegates to
:class:`creek.audit.AuditLog`, so the hash-chain + flock guarantees that
guard ``redact.jsonl`` also guard ``mcp.jsonl``.

The module records ``args_summary``, not raw arguments: long strings
become ``{"len": N}``, lists become counts, dicts become key sets, so
an ``intimate``-tier draft request never leaks the body to the log.
Callers are expected to pass *only* the MCP-supplied parameters into
``append(args=...)`` — never internal state such as ``vault_path``,
which is resolved by the server bootstrap and so does not enter the
audit trail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creek.audit import AuditLog

if TYPE_CHECKING:
    from creek_mcp.tier_ceiling import TierCeiling

MCP_AUDIT_RELPATH = Path("00-Creek-Meta/audit/mcp.jsonl")
"""Canonical MCP audit log location under the vault root."""


def summarise_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a privacy-safe summary of *args* for the audit log.

    Strings are kept up to a short prefix length so the audit log never
    captures a fragment body or draft prompt; long fields are reported
    as ``{"len": N}`` instead. Pure scalars and short strings pass
    through unchanged so the audit entry still names *which* skill /
    phase / index a tool was invoked with.
    """
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            if len(value) <= 64:
                summary[key] = value
            else:
                summary[key] = {"len": len(value)}
        elif isinstance(value, list | tuple):
            summary[key] = {"count": len(value)}
        elif isinstance(value, dict):
            summary[key] = {"keys": sorted(value.keys())}
        else:
            summary[key] = value
    return summary


class MCPAuditLog:
    """Append-only audit log for MCP tool invocations.

    Args:
        vault_path: Vault root under which ``00-Creek-Meta/audit/mcp.jsonl``
            lives. The parent directory is created on the first append.
    """

    def __init__(self, vault_path: Path) -> None:
        """Resolve the canonical audit path under *vault_path*."""
        self.vault_path = vault_path
        self.log_path = vault_path / MCP_AUDIT_RELPATH
        self._audit = AuditLog(self.log_path)

    def append(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        tier_ceiling: TierCeiling,
        consumer: str,
    ) -> None:
        """Append one structured entry for an MCP tool call.

        Args:
            tool: Dot-namespaced tool name (e.g. ``creek.state.read``).
            args: Raw kwargs the tool was invoked with; summarised
                before persistence so bodies cannot leak.
            tier_ceiling: The ceiling the caller supplied.
            consumer: A free-form identifier (``"crawdad"``,
                ``"claude-code"``, ``"unknown"``) recording who invoked
                the tool. CrawDad and Claude Code register distinct
                values; unknown consumers fall back to ``"unknown"``.
        """
        entry = {
            "tool": tool,
            "args_summary": summarise_args(args),
            "tier_ceiling": tier_ceiling.value,
            "consumer": consumer,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        self._audit.append(entry)
