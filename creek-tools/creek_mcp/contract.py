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

CONTRACT_VERSION: Final[str] = "0.23.0"
"""Semantic version of the Adepthood ↔ Creek MCP contract (draft).

0.23.0 (#1981): MCP adds ``creek.revoice``, a DESK-granted transformation
of caller-supplied text through the owner's voice skills. Detached output is
available through ``creek.jobs.read`` only in bounded process memory, to its
initiating agent at an adequate ceiling; no body is saved to drafts or durable
job records. No ``/v1`` route or payload shape changes. Handshake metadata
advertises the new version; ``0.22`` and earlier supported minors remain served.

0.22.0 (#1968): ``/v1`` publishes an eleventh capability, ``agent-activity``,
served by ``GET /v1/agent-activity?limit=&since=``: a content-free projection
of the MCP audit chain, so a person can see what each connected agent asked
their vault for and when. Each row is ``{consumer, tool, at, outcome}`` with
``outcome`` closed at ``admitted | refused``; no argument summary, fragment id,
path, hash or prose crosses. ``limit`` defaults to 50 and is clamped to 200;
malformed log lines are counted in ``skipped`` rather than failing the route.
The chain is read, never rewritten, and not re-verified per request. Three
wire models (``AgentActivityQuery``, ``AgentActivityRow``,
``AgentActivityResponse``) and the first published query parameters widen the
HTTP contract. Alongside it the MCP adapter audits a remote call refused for a
ceiling above ``personal`` as ``"outcome": "refused"`` under the tool name
only, so such an attempt shows on the card; no tool's return shape moves.
``0.21`` remains served without seeing or reaching the route.

0.21.0 (#1903 step 6, ADR-0016): **one cap, one surface.**
``rate_limited`` is served on ``POST /v1/letters/{external_id}`` as well as on
``POST /v1/reflections``, so a caller refused admission for a model call is
told the same thing by both model-backed routes it can meet that refusal on —
the same
``429``, the same closed code, the same envelope and the same advisory
``Retry-After``, through the one
:func:`creek_mcp.httpapi.generation.served_error_code`.

The cap itself does not move and never was per route: both routes resolve the
one ``generation`` stage through ``_bounded_generation_factory``, so the refusal
is the shared pool's admission decision or the ``429`` of whichever vendor the
call reached, and a single budget was already answering both. This server mints
no cap of its own — it keeps no counter — and the pool request shape carries no
route, so a per-route budget could not exist even in principle; whether the
pool's budget is per vault or per account is not visible from here.

What 0.19 left split was the
*reporting* — a cap arriving mid-letter was collapsed into the
``temporarily_unavailable`` that every other provider failure earns, because
``ProviderRateLimitError`` subclasses ``RuntimeError`` and the letter tool
caught the base class. A caller rate limited on one route and silently told an
outage story on the other is being told two things about one limit.

No model, field, status or route is added; ``429`` was already in the published
set. The one thing that moves is which route documents and reaches the code, so
it is withheld from callers below
:data:`~creek_mcp.api.models.LETTER_RATE_LIMITED_SINCE_MINOR` and every ``0.20``
letter response stays exactly as ``0.20`` published it. ``0.20`` therefore stays
in the window.

**It is 0.21 and not 0.20 for the reason this record exists.** 0.20 was already
published by #1938 for ``keyless_inference``, so 0.20 is a minor that already
describes this path; a consumer that negotiated it for that field never agreed
to a ``429`` on a letter. Serving one to it would be the move this contract's
whole version window forbids, and would be the ``incompatible_version`` class of
failure rather than a cosmetic one.

0.20.0 (#1938): ``GET /v1/capabilities`` gains ``keyless_inference``. It is
``false`` when this vault is certain that a model call carrying no writer key
will be refused before any model is dialled. Today that means a managed vault
with no shared pool pinned: it ships no inference runtime and refuses its
operator's cloud key. ``inference_pool: false`` could not say this, because a
self-connected vault running its own model reports the same. Without the field
a consumer could only learn it by charging a writer, dialling, and refunding a
``temporarily_unavailable`` that retrying never clears. ``true`` means a
keyless call may be attempted, which is all a self-connected vault has ever
promised. The field is withheld from callers below 0.20, so every response a
``0.19`` consumer can reach is byte-identical, and ``0.19`` stays in the window.

0.19.0 (#1894, ADR-0016): **provenance, and the admission refusal the shared
inference pool needs.** Three additions, all gated on this minor.

``inference_source`` is published on :class:`creek_mcp.api.models.ReflectionResponse`,
:class:`~creek_mcp.api.models.LetterResponse` and
:class:`~creek_mcp.api.models.ClassificationResponse` (which a consumer reads
inside ``JobStatusResponse.result``): the route that answered, one of
``pool:creek``, ``pool:attested:<vendor>``, ``request_key:<vendor>`` or
``local``. It is ADR-0016 Decision 1 item 11's "every answer names its route",
and it is what lets adepthood treat an answer whose route differs from the one
it disclosed as its existing ``409`` stale-route refusal. The field is
**absent** rather than guessed on an answer this vocabulary does not label —
neither an operator's own cloud key on a self-hosted vault nor an attested
enclave is the pool, the writer's key or local, the enclave because it is
unreadable without being on-device — and absent on a pass that called no model
at all, such as ``method=rules`` classification. A label the server cannot
stand behind would be worse than no label, because the consumer's whole use for
the field is to detect a route it did not expect.

``GET /v1/capabilities`` gains ``inference_pool``: whether this vault answers
model calls from the shared pool, so a consumer can detect the route before
dialling rather than inferring it from the first answer.

``rate_limited`` joins the closed error vocabulary — HTTP **429**, disposition
``retry_with_backoff`` in the published ``retry-policy.json``, with an advisory
``Retry-After`` header that is deliberately outside that table. It is the pool's
admission refusal (Decision 1 items 4 and 8): a per-vault cap, not an outage,
and a writer should be shown a wait. It is served **only** to callers declaring
0.19 or later; an older minor keeps receiving ``temporarily_unavailable``, which
is what it has always received for a shed request and what its vendored closed
vocabulary can name. ``429`` joins the published status set.

``0.18`` stays in the window: every response a ``0.18`` consumer can reach is
byte-identical, because all three additions are withheld from it.

0.18.0 (#1873, ADR-0015): a writer's request-scoped provider key.
``POST /v1/reflections``, ``POST /v1/letters/{external_id}`` and
``POST /v1/classifications`` publish two optional headers,
``X-Creek-Provider`` (``anthropic`` | ``openai``) and
``X-Creek-Provider-Key``. On a managed vault the key powers that request's
OPEN and PERSONAL model calls through the vault's own grounding and voice
pipeline, and is never stored, logged or echoed; INTIMATE is still forced
local or refused by the one router chokepoint, and the operator's own cloud
key stays refused. A malformed pair is ``invalid_request``. ``GET
/v1/capabilities`` gains ``request_key_providers``, the providers this vault
will spend a writer's key on (empty on a self-connected vault), served only to
callers declaring 0.18 or later so a ``0.17`` consumer validating closed sees
its vendored shape. ``0.17`` remains served.

0.17.0 (#1862): ``/v1`` publishes ``letters`` and
``POST /v1/letters/{external_id}``: the vault itself expands one margin note
into its Voice Draft letter, so a vault-bound writer's letter never leaves the
vault for an app-side provider. Generation routes through the same tier-keyed
``generation`` chokepoint reflection uses, so an above-ceiling request is
refused before any model and an intimate-routed one is forced local or
refused. The letter is stored as the AI-attributed Voice Draft at the same
caller-owned id, retractable by the existing ``DELETE
/v1/voice-drafts/{external_id}``, and a repeat returns the first letter
unchanged without a second model call. The new route and two closed models
widen the HTTP contract; ``0.16`` remains served without seeing or reaching
them.

0.16.0 (#1799): ``/v1`` publishes ``journal-withdraw`` and
``DELETE /v1/journal-entries/{external_id}``, the consumer-scoped,
idempotent destructive inverse of journal upsert. A complete response means
the staged source, fragment, ingest-ledger membership, references, and
retrieval-facing derived state were removed and persistent index/provenance
membership was removed or tombstoned; partial cleanup is retryable and never
reported as success. The new route and closed response model widen the HTTP
contract; ``0.15`` remains served without seeing or reaching them.

0.15.0 (#1727): ``/v1`` publishes an eighth capability,
``voice-drafts``, over one caller-owned ``external_id`` resource. ``PUT``
creates or atomically updates the AI-authored draft, ``GET`` recalls the same
persisted document, and ``DELETE`` retracts it after a source entry becomes
intimate. The write/read models carry fixed AI attribution — ``author=ai``,
``author_slug=ai-as-user`` and ``voice_weight=0`` — and the on-disk fragment
uses the same metadata under ``11-Other-Authors/ai-as-user/``, so mirrored
model prose is readable as evidence but can never enter the owner's voice
corpus. The route set and five wire schemas widen the HTTP contract; ``0.14``
remains served and is neither told about nor allowed to reach the capability.

0.14.0 (#1605): ``POST /v1/classifications`` now admits ``method=llm`` and
``POST /v1/links`` admits ``method=embeddings`` as durable asynchronous jobs.
Both return ``202`` with an opaque job id; ``GET /v1/jobs/{job_id}`` reports a
closed queued/running/succeeded/failed lifecycle and, on success, the same
counts-only model the synchronous route returns. Existing short methods keep
their byte-identical ``200`` responses. Records are atomically persisted
inside the vault, bound to the authenticated consumer, and an active record
owned by a prior server instance becomes ``failed`` after restart instead of
remaining plausibly live forever. No fragment id, path, title, prose, or tool
error enters either job response. LLM execution still goes through
``classify_tool`` and ``ModelRouter._enforce_local_for_intimate``, preserving
the intimate-never-cloud chokepoint. The route, request enums, two wire models,
and ``202`` response widen the HTTP contract, so the minor moves and ``0.13``
is retained in the compatibility window.

0.13.0 (#874): one new **read-only** MCP tool, ``creek.classify.entry``. It
takes an ``entry_ref`` and a ``privacy_tier_ceiling`` and reports the
classification that fragment **already carries on disk** — ``frequency``,
``phase``, ``privacy_tier`` and ``classification_method``. It computes
nothing: no rule classifier on demand, no LLM, no persisted verdict. The tool
surface widened, so the minor moves; it cannot be a patch, because a consumer
negotiating capabilities meets a tool name it never agreed to. **No ``/v1``
route, capability, wire model, error code, status or schema moves**, so this
is a pure MCP-surface bump of the 0.3/0.4/0.6/0.7 kind and
:data:`creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS` widens to keep ``0.12``
served.

**What it closes.** A consumer could write a journal entry and could not ask
what the ontology made of it. ``creek.journal`` already returns ``fragment_id``
as a required field on both transports, so ``journal → fragment_id →
creek.classify.entry`` is a complete round trip with **zero change to the
journal surface** — which is why the capability landed here rather than as an
inline field on ``creek.journal``. Inline would have been a constant in
disguise: ``run_ingest`` never runs the frequency/phase classifier, so the
answer would have been ``unclassified`` on every call forever.

That fact is now published rather than merely true. A freshly written entry
reads ``frequency="unclassified"``, ``phase="unclassified"``,
``classification_method="none"`` until a pass runs, and the tool's fourth field
is what makes that legible: the provenance stamp is written unconditionally by
any classify write, so ``method="rules"`` with ``frequency="unclassified"``
means *a pass ran and could not classify this*, while ``method="none"`` means
*no pass has run*. The remedy for the latter is the route that shipped at
``0.10.0`` — run ``creek.classify`` or ``POST /v1/classifications``, then read.

Nothing is newly disclosed. The four published values are bounded enum-valued
strings, already served at strictly broader granularity by
``creek.state.render``, ``creek.wheel`` and the compiled layer, and strictly
less than ``creek.reflect`` — which returns model-generated prose grounded in
the fragment *body* — already publishes over the same id namespace. The tool
is ``GATED`` on the shared ``refuse_above_ceiling`` primitive against the
fragment's current **persisted** tier, never a caller-declared one, failing
closed to ``intimate`` both for a missing ``privacy_tier`` key and for an id
that resolves to nothing. ``classification_method`` is **clamped** to the three
published methods rather than echoed, because raw frontmatter is arbitrary
user-controlled bytes.

0.12.0 (#1292): ``creek.redact.scan``'s ``statistics`` object gains a fifth
typed key, ``files_skipped_symlink`` — the count of symlinked children the
scan declined unopened because their target resolves outside the scanned
root. The counter has existed on ``ScanSummary`` since #1087 and has been
rendered into ``report_markdown`` ever since; it simply never reached the
wire, so a consumer was told how many files were skipped as binary and by
extension and never how many were declined for pointing out of the subtree.
A tool's return shape moved, so the minor moves: it cannot be a patch,
because a client validating the payload closed meets a key it never
negotiated. **No ``/v1`` route, capability, wire model, error code or status
moves**, so this is a pure MCP-surface bump of the 0.3/0.4/0.6/0.7 kind and
:data:`creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS` widens to keep
``0.11`` served. The key is **unconditional** on the wire — it renders at
zero — while the markdown row stays conditional on being non-zero, which is
deliberate: a report line that fires on every scan is noise, while a typed
field whose presence varies is a second contract. Nothing new is disclosed:
the counter is a bare integer carrying no path, filename, target name or PII
type; the identical count already reached the identical audience through
``report_markdown``; and a refusal still carries the canonical four keys
with no ``statistics`` block at all, so it cannot become a skip-count oracle
over a subtree the caller was refused.

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
