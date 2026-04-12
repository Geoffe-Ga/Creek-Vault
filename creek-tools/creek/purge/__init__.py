"""Creek purge module — right-to-be-forgotten operations for vault content.

Provides the :class:`PurgeEngine` for deleting fragments, wiping
classifications, and destroying vault content, along with an
:class:`PurgeAuditLog` that records every purge operation to
``00-Creek-Meta/Processing-Log/purge-log.json``.
"""

from creek.purge.audit import PurgeAuditEntry, PurgeAuditLog
from creek.purge.engine import PurgeEngine, PurgeResult

__all__ = [
    "PurgeAuditEntry",
    "PurgeAuditLog",
    "PurgeEngine",
    "PurgeResult",
]
