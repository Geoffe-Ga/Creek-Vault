"""Versioned identifiers for the Adepthood ↔ Creek MCP contract (#749 / #750).

These constants are the runtime source of the version strings published in
``docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md`` and surfaced by
:func:`creek_mcp.tools.handshake.handshake_tool` so a connecting client can
negotiate compatibility before any read or write.

Bump :data:`CONTRACT_VERSION` when the tool surface or its semantics change;
bump :data:`ONTOLOGY_VERSION` when the shared APTITUDE-frequency / Wavelength-phase
vocabulary changes (it is dated to the last canonical change — the frequency
naming decision, ``docs/decisions/2026-05-23-frequency-naming.md``).
"""

from __future__ import annotations

from typing import Final

CONTRACT_VERSION: Final[str] = "0.5.0"
"""Semantic version of the Adepthood ↔ Creek MCP contract (draft).

0.5.0 (#1372): ``creek.journal`` and ``creek.upload`` now return the
content-free ``warnings`` their ingest run produced, and ``creek.link``
returns the three cluster-health counts the CLI console renders. Bumping a
minor for three tools gaining response fields follows the precedent set by
0.3.0 (one new tool) and 0.4.0 (one new status value).

**Bumping this string is not sufficient on its own.**
:data:`creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS` derives its head entry
from here, so the outgoing minor has to be added to that tuple by hand in the
same change or every client still sending it is refused.
"""

ONTOLOGY_VERSION: Final[str] = "aptitude-wavelength/2026-05-23"
"""Pinned version of the shared frequency/phase vocabulary.

Dated to the canonical frequency-naming decision. A mismatch in this string
between client and server means "renegotiate the contract".
"""
