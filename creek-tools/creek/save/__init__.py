"""``creek save`` — answer-filing-back primitive (FEAT-009).

Public surface for the save module: the :class:`SaveTarget` enum, the
:class:`SaveRequest` payload, the :func:`target_directory` router, and
the :func:`save_to_vault` writer. CrawDad's ``/crawdad save`` (FEAT-016)
will wrap :func:`save_to_vault` via MCP — keep the surface narrow.
"""

from __future__ import annotations

from creek.save.router import (
    INTIMATE_STUB_RELPATH,
    TARGET_SUBDIRS,
    SaveTarget,
    target_directory,
)
from creek.save.writer import SaveRequest, save_to_vault

__all__ = [
    "INTIMATE_STUB_RELPATH",
    "TARGET_SUBDIRS",
    "SaveRequest",
    "SaveTarget",
    "save_to_vault",
    "target_directory",
]
