"""Single source of truth for the Adepthood journal staging directory (#845).

The ``creek.journal`` MCP tool (``creek_mcp/tools/journal.py``) stages each
Adepthood entry's full body as a markdown file under this vault-relative
directory so the ledger-backed markdown ingest picks it up, and the purge
engine (``creek/purge/engine.py``) follows a purged fragment's
``source.origin_key`` back here to delete that staged plaintext — the RTBF
sweep is scoped to exactly this directory. Both sides import the constant
from this module so the two paths can never silently drift apart; it lives
on the ``creek`` side because the layering rule is that ``creek_mcp``
imports ``creek``, never the reverse.

Relocating this path is a breaking change: the source ledger keys staged
entries by it, so a move without a ledger migration would orphan every
existing staged entry.
"""

from __future__ import annotations

from pathlib import Path

JOURNAL_STAGING_RELDIR = Path("00-Creek-Meta/adepthood/journal")
"""Vault-relative staging dir for Adepthood journal entry bodies.

The ``journal`` path segment makes the markdown ingestor classify staged
entries as ``SourcePlatform.JOURNAL``; the ``adepthood`` segment identifies
the source in each fragment's ``source.origin_key``.
"""
