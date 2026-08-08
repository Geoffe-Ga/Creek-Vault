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

CONTRACT_VERSION: Final[str] = "0.3.0"
"""Semantic version of the Adepthood ↔ Creek MCP contract (draft)."""

ONTOLOGY_VERSION: Final[str] = "aptitude-wavelength/2026-05-23"
"""Pinned version of the shared frequency/phase vocabulary.

Dated to the canonical frequency-naming decision. A mismatch in this string
between client and server means "renegotiate the contract".
"""
