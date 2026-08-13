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

CONTRACT_VERSION: Final[str] = "0.7.0"
"""Semantic version of the Adepthood ↔ Creek MCP contract (draft).

0.7.0 (#1494): ``creek.journal`` and ``creek.upload`` now **require** an
explicit ``tier``. Both used to default it to ``open``, so a client that
omitted the field had its content filed in the clear; both now return
``{"status": "refused", ...}`` naming the missing ``tier``. An input becoming
mandatory is a strictly larger break than the optional *response* field that
carried 0.5.0 for these same two tools, so it cannot carry less than a minor.
This row also covers ``creek.save``, which took the identical break in #1495
one commit earlier without a bump — a miss, not a precedent, and cheaper to
absorb into this row than to leave the published version silent about a
mandatory input on a third write verb. **No ``/v1`` wire shape moves**:
``JournalUpsertRequest.tier`` never had a default, and ``creek.upload`` has no
``/v1`` route at all, so this is a 0.3.0/0.4.0-shaped bump and
``SUPPORTED_CONTRACT_MINORS`` is widened rather than shifted.

0.6.0 (#1453): ``creek.purge.*`` results carry two new erasure counters,
``ledger_rows_removed`` (ingest-ledger rows erased by a scoped purge) and
``meta_artifacts_removed`` (files destroyed by the deny-by-default sweep of
``00-Creek-Meta/`` during a whole-vault purge). ``_result_payload`` forwards
every field of the result model, so both reach the wire without any payload
code change — which is exactly why the minor has to move: a client validating
the payload closed would otherwise meet two keys it never negotiated. Both are
plain integers and neither can carry vault content.

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
