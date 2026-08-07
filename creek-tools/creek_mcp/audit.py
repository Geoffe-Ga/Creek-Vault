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

**The underlying log object is shared per path, not built per call** (#1126).
:class:`creek.audit.AuditLog` caches the chain hash of the line it
last wrote so that repeated appends by one writer cost a ``stat`` rather than a
full re-read of the file — the optimisation PR #193 added for the 10k-fragment
ingest path. Every call site here spells the append as
``MCPAuditLog(vault_path).append(...)``, which built a fresh
:class:`~creek.audit.AuditLog` each time and so found that cache cold every
time. Harmless at one append per interactive tool call; not harmless once
``GET /v1/capabilities`` reaches the same append on every authenticated
request, because the re-read happens *inside* the exclusive lock and its cost
grows with the length of the log — an amplification a remote consumer controls
by calling the endpoint. :func:`_shared_audit_log` gives one path one log
object, so the cache survives between requests. Correctness does not depend on
it: :meth:`~creek.audit.AuditLog._compute_prev_hash` compares the cached size
against the file's own before trusting the cached hash, so an append by another
instance, another thread or another process still forces the rescan it needs.
The one thing that check cannot see is an out-of-band rewrite preserving the
file's byte length — pre-existing, but the window is longer now that a cache
lives for the process rather than for one call, so it is written down and
tracked in #1149 rather than left to be rediscovered.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from creek.audit import AuditLog
from creek.audit.log import GENESIS_PREV_HASH

if TYPE_CHECKING:
    from creek_mcp.tier_ceiling import TierCeiling

MCP_AUDIT_RELPATH = Path("00-Creek-Meta/audit/mcp.jsonl")
"""Canonical MCP audit log location under the vault root."""

ENTRY_HASH_FIELD = "entry_hash"
"""Reserved field carrying ``sha256`` of the entry's content."""

_SHARED_LOG_CACHE_SIZE: Final[int] = 32
"""How many distinct log paths keep a warm chain-hash cache.

Bounded rather than unbounded: a server serves one vault, so one entry does
the whole job, and a cap means neither a long-lived process nor a test session
that touches thousands of temporary vaults can accumulate log objects without
limit. Evicting an entry costs the next append one rescan and nothing else.
"""


@lru_cache(maxsize=_SHARED_LOG_CACHE_SIZE)
def _shared_audit_log(log_path: Path) -> AuditLog:
    """Return the one :class:`~creek.audit.AuditLog` writing *log_path*.

    Keyed on an already-resolved path so two spellings of the same file share
    one object — the same normalisation
    :func:`creek.audit.log._thread_lock_holder_for` applies to the write lock,
    for the same reason: two objects for one file would each hold a cold cache
    and neither would know the other had written.

    Args:
        log_path: The resolved path of the log file.

    Returns:
        The shared log object for that path.
    """
    return AuditLog(log_path)


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

    A string of at most 64 characters passes through verbatim; a longer one
    (which could be a fragment body or draft prompt) is replaced by its length
    as ``{"len": N}`` — never truncated to a prefix — so the log never captures
    sensitive content. Lists/tuples report a ``{"count": N}``, dicts their
    sorted keys, and pure scalars pass through, so the entry still names *which*
    skill / phase / index a tool was invoked with.
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
        """Resolve the canonical audit path under *vault_path*.

        Cheap by construction — no I/O beyond the path resolution the shared
        log is keyed on — so the ``MCPAuditLog(vault)`` spelling every tool
        uses stays a per-call expression rather than something each call site
        has to remember to hoist.
        """
        self.vault_path = vault_path
        self.log_path = vault_path / MCP_AUDIT_RELPATH
        self._audit = _shared_audit_log(self.log_path.resolve(strict=False))

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

    The function reads the log exactly once and validates both checks
    against the same byte snapshot:

    * ``prev_hash`` chain — each entry's stored ``prev_hash`` is
      compared against ``sha256`` of the previous full line. Removing
      or reordering an entry breaks the link at the next line.
    * ``entry_hash`` per entry — recomputed from the stored payload
      (excluding ``entry_hash``/``prev_hash`` to avoid circularity)
      and compared to the persisted digest. Mutating any other field
      invalidates the hash on its own.

    Reading once is intentional: an earlier two-pass version (verify
    chain via :class:`AuditLog.verify`, then iterate via
    :class:`AuditLog.read`) had a TOCTOU window. A concurrent appender
    could grow the file between the two opens, leaving the trailing
    new entries unchecked by ``entry_hash``. The single snapshot here
    closes that gap — both invariants see the same set of lines.

    A missing log is trivially valid (no entries to disagree about).

    Raises:
        MCPAuditChainBrokenError: When either invariant fails.
    """
    log_path = vault_path / MCP_AUDIT_RELPATH
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8") as handle:
        lines = [
            stripped for stripped in (raw.rstrip("\n") for raw in handle) if stripped
        ]
    previous_line: str | None = None
    for index, raw_line in enumerate(lines):
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            msg = f"MCP audit line {index} is not valid JSON"
            raise MCPAuditChainBrokenError(msg) from exc
        _verify_prev_hash(entry, previous_line, index)
        _verify_entry_hash(entry, index)
        previous_line = raw_line


def _verify_prev_hash(
    entry: dict[str, Any],
    previous_line: str | None,
    index: int,
) -> None:
    """Confirm *entry*'s ``prev_hash`` matches ``sha256`` of *previous_line*."""
    if "prev_hash" not in entry:
        msg = f"MCP audit line {index} is missing 'prev_hash'"
        raise MCPAuditChainBrokenError(msg)
    expected = (
        GENESIS_PREV_HASH
        if previous_line is None
        else hashlib.sha256(previous_line.encode("utf-8")).hexdigest()
    )
    if entry["prev_hash"] != expected:
        msg = (
            f"MCP audit chain broken at line {index}: "
            f"expected prev_hash {expected!r}, found {entry['prev_hash']!r}"
        )
        raise MCPAuditChainBrokenError(msg)


def _verify_entry_hash(entry: dict[str, Any], index: int) -> None:
    """Confirm *entry*'s stored ``entry_hash`` matches recomputed content."""
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
