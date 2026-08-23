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

CONTRACT_VERSION: Final[str] = "0.11.0"
"""Semantic version of the Adepthood ↔ Creek MCP contract (draft).

0.11.0 (#1568): two more routes under the **existing** ``drive-connector``
capability — ``POST /v1/connectors/drive/authorizations`` and ``POST
/v1/connectors/drive/authorizations/{state}``. The minor moves because the
route set widened; the capability list does not, because a consumer cannot
usefully negotiate "may I sync" apart from "may I connect", and a server
advertising sync-without-connect would be advertising half a connector.

**What it closes.** The last unmet clause of the seeding epic (#1523).
``/v1`` could sync Drive and disconnect it and could not *connect* it: the
first authorisation was ``creek gdrive --download`` on the host, whose
``InstalledAppFlow.run_local_server(port=0)`` opens a browser on the server.
With these two routes a user connects Drive over the network, with no CLI and
no shell access on the vault host.

**How, and why it is shaped that way.** ADR-0012 option C: Creek holds the
Google **web** client secret and mints the authorization URL; the *caller* owns
the redirect URI and the browser leg; the authorization code comes back over
the caller's existing bearer. The alternative — a callback endpoint on this
server — would need the first anonymous path
:class:`creek_mcp.httpapi.auth.BearerAuthMiddleware` has ever had, because a
Google redirect is a browser navigation carrying no ``Authorization`` header.
Option C needs no such exemption, no public hostname and no TLS this server
controls.

**Three wire models ride along** (``DriveAuthorizationRequest``,
``DriveAuthorizationResponse``, ``DriveAuthorizationExchangeRequest``) and **no
new error code**: an unknown, expired, consumed or Google-refused
authorization is the published ``unavailable``, all four identical, because
telling them apart would describe which authorizations this server has
outstanding.

0.10.0 (#1570): a seventh capability, ``pipeline``, served by two routes —
``POST /v1/classifications`` and ``POST /v1/links``. Additive in exactly the
0.8.0 / 0.9.0 shape: ``SUPPORTED_CONTRACT_MINORS`` widens to keep ``0.9`` and
everything below it served, and a client pinned under 0.10 is neither told the
capability exists nor served it, because both halves read
:data:`creek_mcp.api.models.CAPABILITY_SINCE_MINOR`. No existing response
shape, field, status or error code moves.

**What it closes.** Until now ``/v1`` could ingest and nothing else:
``creek.classify`` and ``creek.link`` existed only as MCP tools, so a vault
seeded entirely over the network held fragments with no APTITUDE frequency, no
Archetypal Wavelength phase and no resonances — inert, with nothing erroring.
The seeding epic's Definition of Done promises fragments land
*"correctly-typed, correctly-tiered — over the network, with no CLI and no
shell access"*, and typing and tiering are what these two passes produce.

**What it deliberately does not add.** ``classify --method llm`` and ``link
--method embeddings`` are absent from the wire enums, so they are unreachable
in the *type* rather than refused at runtime. Both are minutes-to-hours of work, and
these are **write** routes, which since #1109 deliberately run to completion
rather than being shed at the thirty-second deadline — abandoning a vault
mutation to meet a deadline would report a failure for work that landed. So the
caller would simply wait minutes to hours with no refusal to act on. Reaching
them over the network needs an asynchronous job surface this contract does not
have; until then they remain an operator step on the host. That is **not** a
promise that the served members all finish inside the deadline either:
``eddies`` and ``threads`` cluster over embeddings and so fill the parquet
cache with a *local* model pass on a cold vault, which a first call can far
outrun. They ship anyway because the pass is idempotent and the cache keeps
what it computed, so a client that hangs up and retries converges (#1605).

**This is the first double-digit minor.**
:func:`creek_mcp.api.models.minor_at_least` compares componentwise as integers
precisely for this day: read as text, ``"0.10"`` sorts *below* ``"0.8"``.

0.9.0 (#1527 / #873): two additive changes ship at this minor. Both widen
``SUPPORTED_CONTRACT_MINORS`` rather than shift it, and neither takes anything
away from a client pinned below 0.9.

**#1527 — a sixth capability.** ``/v1`` publishes ``drive-connector``,
served by three routes on the existing read-only Google Drive OAuth connector:
``GET /v1/connectors/drive`` (connection state), ``POST
/v1/connectors/drive/syncs`` (one incremental sync) and ``DELETE
/v1/connectors/drive`` (revoke and erase the cached token). Shaped exactly like
0.8.0 and additive for the same reason: a client below 0.9 is neither told the
capability exists nor served it, because both halves read
:data:`creek_mcp.api.models.CAPABILITY_SINCE_MINOR`. Three wire models ride
along (``DriveConnectorStatusResponse``, ``DriveSyncResponse``,
``DriveDisconnectResponse``) and **no new error code** — the connector's
refusals are the published ``unavailable`` and ``temporarily_unavailable``,
which is deliberate: a code minted for "Drive is not connected" would be a
Drive-specific fact on a shared error table.

**What this bump does not add is the authorisation step.** Granting Drive
access still happens locally, through ``creek gdrive --download``: the cached
credential is an installed-app loopback flow, which needs a browser on the
machine holding the client secret, and no ``/v1`` route begins, completes or
carries one. The token stays server-side and never crosses the wire in either
direction.

**#873 — two optional reflection fields.** ``creek.reflect`` and ``POST
/v1/reflections`` may now carry two **optional** fields — ``related_praxis``
and ``related_eddies`` — naming the
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
