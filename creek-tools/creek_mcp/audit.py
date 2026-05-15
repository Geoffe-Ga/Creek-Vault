"""MCP-side audit log (FEAT-010 + FEAT-012 hardening).

Every MCP tool invocation appends one entry to
``<vault>/00-Creek-Meta/audit/mcp.jsonl``. The module exists as its own
boundary from day 1 — FEAT-011 adds write-side fields, and FEAT-012
adds the per-entry ``entry_hash`` plus a chain verifier so that
tampering with any single field is detectable without comparing to a
known-good snapshot. The storage layer delegates to
:class:`creek.audit.AuditLog`, so the ``prev_hash`` chain and the
exclusive ``flock`` during writes that guard ``redact.jsonl`` also
guard ``mcp.jsonl``.

The module records ``args_summary``, not raw arguments: long strings
become ``{"len": N}``, lists become counts, dicts become key sets, so
an ``intimate``-tier draft request never leaks the body to the log.
Callers are expected to pass *only* the MCP-supplied parameters into
``append(args=...)`` — never internal state such as ``vault_path``,
which is resolved by the server bootstrap and so does not enter the
audit trail.

FEAT-012 hardening details:

* ``entry_hash`` = SHA-256 of the entry's content excluding the
  ``entry_hash`` field itself (sorted JSON, UTF-8). Mutating any field
  invalidates the hash, so a verifier can flag the tampered entry on
  its own — without needing the next entry's ``prev_hash``.
* ``prev_hash`` continues to chain through ``creek.audit.AuditLog``,
  so removing or reordering entries breaks the chain even if every
  individual ``entry_hash`` survives.
* :func:`verify_mcp_audit_chain` walks both invariants and raises
  :class:`MCPAuditChainBrokenError` on any mismatch.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creek.audit import AuditChainBrokenError, AuditLog

if TYPE_CHECKING:
    from creek_mcp.tier_ceiling import TierCeiling

MCP_AUDIT_RELPATH = Path("00-Creek-Meta/audit/mcp.jsonl")
"""Canonical MCP audit log location under the vault root."""

ENTRY_HASH_FIELD = "entry_hash"
"""Reserved field carrying ``sha256`` of the entry's content."""


class MCPAuditChainBrokenError(Exception):
    """Raised when :func:`verify_mcp_audit_chain` detects tampering.

    The error message names the offending line index (zero-based) and
    which invariant failed — ``entry_hash`` mismatch (mutation) or
    ``prev_hash`` mismatch (reordering / removal) — so operators can
    bisect which entry the editor touched.
    """


_HASH_EXCLUDED_FIELDS = frozenset({"entry_hash", "prev_hash"})
"""Fields stripped before computing ``entry_hash``.

``entry_hash`` is removed to avoid the obvious circularity, and
``prev_hash`` is removed because the chain link is owned by
:class:`creek.audit.AuditLog` and stamped *after* the MCP layer fixes
its content hash. Excluding both means ``entry_hash`` covers the
caller-supplied payload — the part the MCP layer is responsible for —
while ``prev_hash`` covers placement in the chain. The two invariants
are independent: mutating any non-hash field invalidates
``entry_hash``; reordering or removing lines invalidates the chain.
"""


def _content_hash(entry: dict[str, Any]) -> str:
    """Return ``sha256`` of *entry* with chain / hash fields removed.

    Sorted JSON keys pin the byte layout so two callers serialising
    the same logical entry agree on the digest.
    """
    payload = {k: v for k, v in entry.items() if k not in _HASH_EXCLUDED_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ).hexdigest()


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
        created_path: str | None = None,
        created_tier: str | None = None,
        affected_fragment_ids: list[str] | None = None,
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
            created_path: Path of the file the tool produced
                (FEAT-011 write-side field). Read tools pass ``None``.
            created_tier: Privacy tier of the produced content
                (FEAT-011 write-side field).
            affected_fragment_ids: IDs of fragments touched by the call
                (FEAT-011 write-side field). Stored as IDs, never as
                bodies, so the audit log remains body-free.
        """
        entry: dict[str, Any] = {
            "tool": tool,
            "args_summary": summarise_args(args),
            "tier_ceiling": tier_ceiling.value,
            "consumer": consumer,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        if created_path is not None:
            entry["created_path"] = created_path
        if created_tier is not None:
            entry["created_tier"] = created_tier
        if affected_fragment_ids is not None:
            entry["affected_fragment_ids"] = list(affected_fragment_ids)
        # entry_hash before prev_hash: prev_hash is added inside
        # AuditLog.append and folds entry_hash into the chained bytes,
        # so a verifier that recomputes entry_hash on read sees an
        # entry whose content matches the chain link of its successor.
        entry[ENTRY_HASH_FIELD] = _content_hash(entry)
        self._audit.append(entry)


def verify_mcp_audit_chain(vault_path: Path) -> None:
    """Walk ``mcp.jsonl`` under *vault_path* and validate both invariants.

    Two checks run in tandem:

    * ``prev_hash`` chain — delegated to :class:`creek.audit.AuditLog`,
      which detects removal / reordering by comparing each entry's
      ``prev_hash`` against ``sha256`` of the previous full line.
    * ``entry_hash`` per entry — recomputed from the stored payload
      (sans the field itself) and compared to the persisted digest, so
      a mutation of any other field is flagged on its own.

    A missing log is trivially valid (no entries to disagree about).

    Raises:
        MCPAuditChainBrokenError: When either invariant fails.
    """
    log_path = vault_path / MCP_AUDIT_RELPATH
    if not log_path.exists():
        return
    audit_log = AuditLog(log_path)
    try:
        audit_log.verify()
    except AuditChainBrokenError as exc:
        msg = f"MCP audit chain broken: {exc}"
        raise MCPAuditChainBrokenError(msg) from exc
    for index, entry in enumerate(audit_log.read()):
        stored = entry.get(ENTRY_HASH_FIELD)
        if stored is None:
            msg = f"MCP audit line {index} is missing 'entry_hash'"
            raise MCPAuditChainBrokenError(msg)
        expected = _content_hash(entry)
        if stored != expected:
            msg = (
                f"MCP audit entry_hash mismatch at line {index}: "
                f"expected {expected!r}, found {stored!r}"
            )
            raise MCPAuditChainBrokenError(msg)
