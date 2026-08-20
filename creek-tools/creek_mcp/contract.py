"""Versioned identifiers for the Adepthood ↔ Creek MCP contract (#749 / #750).

These constants are the runtime source of the version strings published in
``docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md`` and surfaced by
:func:`creek_mcp.tools.handshake.handshake_tool` so a connecting client can
negotiate compatibility before any read or write.

Bump :data:`CONTRACT_VERSION` when the tool surface or its semantics change;
bump :data:`ONTOLOGY_VERSION` when the shared classification vocabulary changes
on **either** of its two axes — the ten APTITUDE frequencies (ontology prompt
§6.1) or the Archetypal Wavelength phases and Modes (§7). They are separate
axes classified independently, not three names for one list, and the version
string covers both: a client that renegotiated only on a frequency change would
miss a Mode or phase revision it also has to interpret. It is dated to the last
canonical change — the frequency naming decision,
``docs/decisions/2026-05-23-frequency-naming.md``.
"""

from __future__ import annotations

from typing import Final

CONTRACT_VERSION: Final[str] = "0.9.0"
"""Semantic version of the Adepthood ↔ Creek MCP contract (draft).

0.9.0 (#873): ``creek.reflect`` and ``POST /v1/reflections`` may now carry two
**optional** fields — ``related_praxis`` and ``related_eddies`` — naming the
compiled-layer structures the reflected entry belongs to. Shape-wise this is a
0.5.0-shaped bump: two optional response fields on one published ``/v1``
response, plus two new wire models (``RelatedPraxis`` / ``RelatedEddy``) and the
schemas for them. No route, no capability, no error code, and no *required*
field moves, so ``SUPPORTED_CONTRACT_MINORS`` widens rather than shifts and a
``0.8`` client keeps being served.

Be precise about what it costs that client, because "nothing" is not the honest
answer: the route drops both keys when nothing qualified, so the common case is
byte-identical — but a reflection that *does* find an admitted eddy or praxis
carries two extra keys regardless of the minor the caller negotiated, and a
``0.8`` consumer validating closed sees them in exactly that case. Retained on
the 0.5.0 reasoning: refusing every ``0.8`` request outright is strictly worse
than serving one that occasionally carries a field the client can ignore, and
the window is published on ``GET /v1/capabilities`` for a client that wants to
move.

The bump is what makes the addition *detectable* rather than silent, and that
matters more here than for ``warnings`` at 0.5.0, because these fields are
compiled artifacts: a consumer needs to know the field exists in order to know
that its absence means "nothing qualified or nothing was admitted" rather than
"this server is too old to answer". Admission itself is unchanged — a compiled
page reaches the wire only when every fragment it was compiled from is within
the caller's ceiling, and the remote cap still stops any network consumer
declaring more than ``personal``.

0.8.0 (#1524): ``/v1`` publishes a **fifth capability**, ``upload``, served by
``POST /v1/uploads``. This is the first bump since 0.2.0 that adds a route
rather than only moving the MCP tool surface, and it is the reason the previous
four bumps all had to widen ``SUPPORTED_CONTRACT_MINORS`` rather than shift it:
a client pinned to an older minor is answered by a ``GET /v1/capabilities`` that
does **not** list ``upload``, and ``POST /v1/uploads`` refuses it with
``incompatible_version``, because a capability a client's vendored contract does
not describe is one it must not be silently handed. Both halves read the same
:data:`creek_mcp.api.models.CAPABILITY_SINCE_MINOR` table. Three further
additions ride along, all of them additive: two wire models
(``UploadRequest`` / ``UploadResponse``), one error code
(:attr:`~creek_mcp.api.models.ErrorCode.UNSUPPORTED_SOURCE`) and — with it —
``415`` joining the published status set, which is the one part of this bump an
older client can meet on a route it already calls only if it starts calling a
route it was never told about. ``creek.upload`` itself is unchanged: the route
delegates to :func:`creek_mcp.tools.upload.upload_tool` and reimplements nothing.

0.7.0 (#1494): ``creek.journal`` and ``creek.upload`` now **require** an
explicit ``tier``. Both used to default it to ``open``, so a client that
omitted the field had its content filed in the clear; both now return
``{"status": "refused", ...}`` naming the missing ``tier``. An input becoming
mandatory is a strictly larger break than the optional *response* field that
carried 0.5.0 for these same two tools, so it cannot carry less than a minor.
This row also covers ``creek.save``, which took the identical break for issue
#1434 — shipped as PR #1495, one commit before this one — without a bump. Both
numbers are given because the rest of the tree cites the issue while the
missing bump is a property of the PR: a miss, not a precedent, and cheaper to
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
"""Pinned version of the shared APTITUDE-frequency and Archetypal-Wavelength
vocabularies — the same two axes the module docstring names, versioned
together, which is why the string reads ``aptitude-wavelength/…``.

Dated to the canonical frequency-naming decision. A mismatch in this string
between client and server means "renegotiate the contract".
"""
