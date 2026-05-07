"""Tamper-evident JSONL audit log substrate.

The :class:`AuditLog` primitive backs every compliance log in the
project — purge, redaction, privacy-tier overrides — by writing each
entry as a single JSONL line with a sha256 hash chain so any post-hoc
tampering can be detected via :meth:`AuditLog.verify`.

Use the primitive via the per-domain wrappers (e.g.
:class:`creek.purge.audit.PurgeAuditLog`); reach for :class:`AuditLog`
directly only when introducing a new compliance log surface.
"""

from creek.audit.log import AuditChainBroken, AuditChainBrokenError, AuditLog
from creek.audit.yield_summary import (
    PreLLMYieldSummary,
    format_yield_line,
    write_yield_summary,
    yield_summary_path,
)

__all__ = [
    "AuditChainBroken",
    "AuditChainBrokenError",
    "AuditLog",
    "PreLLMYieldSummary",
    "format_yield_line",
    "write_yield_summary",
    "yield_summary_path",
]
