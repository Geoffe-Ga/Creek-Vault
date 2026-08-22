# Adepthood ↔ Creek MCP Contract

<!-- capability-set: contract-versions -->
- **Status**: Draft — pending agreement
- **Date**: 2026-06-30 (surface truthed-up 2026-08-21, #875/#1094)
- **Contract version**: `0.9.0`
- **Ontology version**: `aptitude-wavelength/2026-05-23` (see [Ontology version](#ontology-version))
<!-- /capability-set -->
- **Driving issues**: [#748](https://github.com/Geoffe-Ga/Creek-Vault/issues/748) (epic), [#749](https://github.com/Geoffe-Ga/Creek-Vault/issues/749) (this doc), [#875](https://github.com/Geoffe-Ga/Creek-Vault/issues/875) (truth-up), [#1094](https://github.com/Geoffe-Ga/Creek-Vault/issues/1094) (cross-repo reconciliation)
- **Mirrors**: [`geoffe-ga/adepthood#950`](https://github.com/geoffe-ga/adepthood/issues/950) (Adepthood's required side)
- **Boundary**: [`geoffe-ga/adepthood#927`](https://github.com/geoffe-ga/adepthood/issues/927)

> **The two version strings above are machine-checked.** They sit inside an
> invisible `capability-set` fence and
> `creek-tools/tests/test_mcp_contract_adr_shipped_surface.py` asserts they
> equal `creek_mcp.contract.CONTRACT_VERSION` and
> `creek_mcp.contract.ONTOLOGY_VERSION` at every run. The header used to read
> `0.4.0 (draft)` while the change log's own top row said `0.9.0`; that was a
> **stale header, not five unrecorded contract events**. `creek_mcp/contract.py`
> has always been the runtime source of truth, and each minor between `0.1.0`
> and `0.9.0` is now recorded in the [change log](#change-log) with the change
> that earned it — four of those rows (`0.5.0`–`0.8.0`) were written by the
> 2026-08-21 truth-up, which found them missing.
> `test_the_change_log_has_a_row_for_every_minor_up_to_the_current_one` asserts
> the range stays complete as the contract moves.

> **This is still a draft pending agreement with the Adepthood side.** It is
> published in lock-step with `adepthood#950` so both repos implement against
> one agreed surface. Until both sides mark their copy `Accepted`, tool names,
> shapes and version strings here are proposals, not commitments. What changed
> on 2026-08-21 is that the *Creek* half now describes what Creek ships rather
> than what Creek planned; the status stays `Draft` because
> [Open questions](#open-questions-resolve-before-accepted) still carries a
> substantive, unreconciled divergence about intimate transit, and neither repo
> may flip to `Accepted` alone.

> **Sibling document (2026-07-31, #1072).** Adepthood also reaches Creek over
> a second, HTTP/JSON application adapter, `/v1`, documented separately in
> [`2026-07-31-adepthood-http-application-api.md`](./2026-07-31-adepthood-http-application-api.md)
> (status `Accepted (Creek side)`). Both adapters call the same
> `creek_mcp.tools.*` implementations and share one `contract_version` /
> `ontology_version`; this document remains the record for the MCP **agent**
> adapter described below, and does not itself change status as a result.

## Purpose

The Adepthood app reaches a user's local Creek vault to: read and write journal
content, classify it against the shared APTITUDE/Wavelength ontology, request
Higher-Self "margin note" reflections on a single entry, and read a per-frequency
balance ("wheel") for the Map. This contract enumerates the capabilities
Adepthood depends on, maps each to a Creek MCP tool, and fixes the
privacy/care/auth/transport semantics both sides build against.

Creek's existing MCP surface and registration are documented in
[`creek-tools/docs/mcp.md`](../../creek-tools/docs/mcp.md); this contract is the
cross-repo view layered on top of it. Where the two disagree, `docs/mcp.md`
describes *what ships today* and this contract describes *what the two repos have
agreed to*.

## Transport

Both transports ship and the choice is made at server start, by
`creek-tools-mcp --transport`. The table is machine-checked against the
argument parser's own published choices.

<!-- capability-set: mcp-transports -->
| `--transport` | What it is | Auth | Remote tier cap |
|---|---|---|---|
| `stdio` (default) | JSON-RPC over stdio; the consumer launches the server as a child process and no socket is bound. | None. Whoever can spawn the process is the operator. | None — a local caller is not capped. |
| `network` | Authenticated **streamable-HTTP**. Binds `--host`/`--port` (default `127.0.0.1:8000`) and serves `server.run(transport="streamable-http")`. | **Required.** Per-consumer bearer tokens from `CREEK_MCP_CONSUMER_TOKENS`; the process refuses to start without them. A non-loopback `--host` additionally requires `--tls-cert`/`--tls-key`. | `open` and `personal` only. See [Tier ceiling semantics](#tier-ceiling-semantics). |
<!-- /capability-set -->

Code: `creek_mcp/server.py` — the `--transport` argument and its
`--host`/`--port`/`--tls-cert`/`--tls-key` companions in `_build_arg_parser`;
`_serve_network`, which calls `server.run(transport="streamable-http")`;
`DEFAULT_MCP_NETWORK_PORT = 8000`; and the `main` branch that refuses to serve
`network` without `CREEK_MCP_CONSUMER_TOKENS` and calls
`_require_transport_confidentiality` before binding.

This supersedes the original draft's "there is no network listener … remote is
out of scope for this contract version, being decided in #755". **#755 is
closed**: it landed the network transport, the per-consumer bearer model and the
remote tier cap together, precisely because none of the three is safe without
the other two.

> **The handshake's `transport` field does not report this.**
> `creek_mcp/tools/handshake.py` holds `TRANSPORT = "stdio"` as a module-level
> literal and emits it unconditionally, so a consumer connected over
> streamable-HTTP is still told `"transport": "stdio"`. That is a **known
> defect in the code, not in this document**, and it is recorded here rather
> than papered over: a consumer must not use this field to decide whether it is
> remote. Tracked as a Creek-side code issue; deliberately not "fixed" in the
> doc, because describing a field as transport-aware when it is a constant
> would document behaviour that does not exist.

## Consumer identity & auth model

| Mechanism | Env var | Enforced? | Purpose |
|---|---|---|---|
| Consumer identity (local stdio) | `CREEK_MCP_CONSUMER` | **No — audit-only** | Tags every audit-log entry so calls from `claude-code` vs `crawdad` vs an Adepthood consumer are distinguishable. Free-form string; defaults to `unknown`; read once at server start (`_consumer_from_env`, `server.py`). It grants and denies nothing. |
| Consumer identity (network) | *(none — from the bearer token)* | **Yes — it is the credential** | A network call is audited under the consumer its bearer token names, resolved **per call** from the request-scoped access token (`_caller_identity` → `CallerIdentity(consumer=token.client_id, is_remote=True)`). `creek_mcp/policy.py:effective_consumer` returns that consumer for a remote call and **raises** when a remote identity names none, rather than falling back to the local default. |
| Consumer auth (network) | `CREEK_MCP_CONSUMER_TOKENS` | **Yes — constant-time compare** | `consumer=token` pairs. `ConsumerTokenVerifier.verify_token` compares the supplied bearer against every known token with `hmac.compare_digest` over UTF-8 bytes and returns an `AccessToken` whose `client_id` **is** the consumer name. Duplicate consumer names and shared token values are refused at load; a rotation window is announced on stderr. There is no anonymous access in network mode. |
| Elevated operations | `CREEK_MCP_ELEVATED_TOKEN` | **Yes — constant-time HMAC compare** | Gates the destructive `creek.purge.*` family only ([`creek_mcp/auth.py`](../../creek-tools/creek_mcp/auth.py)). Fails closed when either side is empty. Adepthood is **not** expected to hold this token; purge is out of the Adepthood surface, and a per-consumer bearer alone does not satisfy it. |

An Adepthood consumer on **stdio** SHOULD set `CREEK_MCP_CONSUMER=adepthood` so
its traffic is attributable. An Adepthood consumer on the **network** transport
does not set it at all: its identity is whatever name its token was registered
under, and that name reaches the audit log without the client being able to
choose it. This supersedes the original draft's "a capability-bearing
consumer-auth model … is part of #755, not this version."

> **Residual, deliberately not claimed closed:
> [#1100](https://github.com/Geoffe-Ga/Creek-Vault/issues/1100) is open.** A
> *blank* consumer `client_id` is not refused at the verifier boundary.
> `load_consumer_tokens` drops blank consumer names, so only a custom
> `token_verifier` passed to `build_server` can produce one, and
> `effective_consumer` deliberately returns `""` rather than raising mid-dispatch
> (raising there would skip the audit entry for the very call whose attribution
> is suspect — the reasoning is recorded in `creek_mcp/policy.py`). Do not read
> this section as "the verifier boundary is fully closed".

## Privacy gate and care gate — who enforces what

- **Privacy gate — enforced Creek-side (server), at the MCP boundary.** Every
  non-purge tool takes a `privacy_tier_ceiling` parameter
  (`TierCeiling`, default `OPEN`). Reads above the ceiling are refused and writes
  above the ceiling are **refused, not silently downgraded**
  ([`creek_mcp/tier_ceiling.py`](../../creek-tools/creek_mcp/tier_ceiling.py)).
  See [Tier ceiling semantics](#tier-ceiling-semantics). This is the same single
  boundary that already serves Claude Code and CrawDad — Adepthood does not get a
  side door.
- **Care gate — enforced on both sides. Creek's half ships.**
  [#753](https://github.com/Geoffe-Ga/Creek-Vault/issues/753) is closed:
  `creek.reflect` is registered with `care_guard=acute_distress_guard`
  (`server.py`), and `creek/care/guardrail.py` supplies the two halves. An entry
  matching the guard's narrow acute-intent patterns returns
  `{status: "escalate", tool, tier_ceiling, reason: "acute_distress_markers",
  care_signal: CARE_SIGNAL}` and **the model is never called**; `CARE_POLICY` is
  additionally woven into the reflection prompt
  (`creek_mcp/tools/reflect.py`). This supersedes the original draft's "not
  modelled in Creek code today … the care gate is the consumer's
  responsibility."

  **What Creek's half is not.** The guard matches four literal first-person
  imminent-intent phrasings and is biased toward false negatives by design, so
  paraphrase, typos and non-English phrasing pass through it. Its own source
  says so: *"Adepthood runs the primary acute pre-check; this is Creek's
  safe-by-construction backstop."* Adepthood remains the primary care gate. The
  ordering matters too — the read-side ceiling check runs **above** the care
  seam, so an unadmitted read is refused rather than escalated, because an
  `escalate` reply would otherwise be a one-bit oracle telling a caller who
  cannot read a fragment that it carries distress markers.

## Capabilities → MCP tools

Every row names a tool that is registered today. The table is machine-checked
in both directions against `build_server(...).list_tools()`: a row naming an
unregistered tool fails, and a newly registered Adepthood-surface tool with no
row fails. The word *Planned* is not permitted on a row whose tool is
registered.

<!-- capability-set: mcp-capability-tools -->
| Capability Adepthood depends on | MCP tool | Status | Reference |
|---|---|---|---|
| Handshake / capabilities (+ contract & ontology version, tier model) | `creek.handshake` | **Ships** | [#750](https://github.com/Geoffe-Ga/Creek-Vault/issues/750) |
| Journal entry → fragment, idempotent by `external_id` | `creek.journal` | **Ships** | [#754](https://github.com/Geoffe-Ga/Creek-Vault/issues/754) |
| Ingest one source **file path** into fragments | `creek.ingest` | **Ships** | — |
| Upload one document's bytes, staged and ingested | `creek.upload` | **Ships** | [#1023](https://github.com/Geoffe-Ga/Creek-Vault/issues/1023) (contract `0.3.0`) |
| Classify existing fragments | `creek.classify` | **Ships** | — |
| Reflect — Higher-Self margin notes on one entry | `creek.reflect` | **Ships** | [#751](https://github.com/Geoffe-Ga/Creek-Vault/issues/751) |
| Wheel — per-frequency balance read for the Map | `creek.wheel` | **Ships** | [#752](https://github.com/Geoffe-Ga/Creek-Vault/issues/752) |
<!-- /capability-set -->

Two capabilities that belong to the Adepthood surface are **not** MCP tools and
so are not in the table above: the tier ceiling is a parameter on every tool
(see [Tier ceiling semantics](#tier-ceiling-semantics)), and `drive-connector`
— the read-only Google Drive connector published at contract `0.9.0` — exists
only as three `/v1` HTTP routes, documented in the sibling ADR. The `upload`
capability arrived as an MCP tool at contract `0.3.0` and was published on
`/v1` at `0.8.0`.

The original draft's preamble promised that "sections describing tools that do
not exist yet are explicitly labelled *Planned* with their tracking issue."
**No section of this document is labelled *Planned* any more**, because every
tool it describes is registered. The promise is kept by the machine check
rather than by the sentence: if a future capability is added to this table
before its tool exists, the test fails.

### Handshake / capabilities — `creek.handshake`

A read-only, LLM-free tool that lets a consumer discover, in one call: whether a
vault is present (`available`), the **contract version** and **ontology
version** (both from `creek_mcp/contract.py`), the **tier model** (the
`TierCeiling` ceilings and the INTIMATE-never-egresses guarantee), and the
**capabilities** list — the names of the tools actually registered, sourced live
from `server.list_tools()` so it cannot drift. Adepthood calls this first to
confirm both sides speak the same contract before any read/write.

The example below is machine-checked twice over: its `contract_version` and
`ontology_version` must equal the runtime constants, and its **key set** must
equal the key set of a real `handshake_tool(...)` call, so a field added to or
removed from the handshake cannot sit undocumented here.

<!-- capability-set: handshake-example -->
```json
{
  "status": "ok",
  "tool": "creek.handshake",
  "tier_ceiling": "open",
  "server": "creek-tools-mcp",
  "transport": "stdio",
  "available": true,
  "contract_version": "0.9.0",
  "ontology_version": "aptitude-wavelength/2026-05-23",
  "tiers": ["open", "personal", "intimate"],
  "tier_model": { "ceilings": ["open", "personal", "intimate", "all"],
                  "default": "open", "intimate_never_egresses": true },
  "capabilities": ["creek.classify", "creek.handshake", "creek.ingest", "..."]
}
```
<!-- /capability-set -->

`tiers` is the tiers *content* can hold (`PrivacyTier` minus `unclassified`);
`tier_model.ceilings` is what a *caller* may request, which is the four-member
`TierCeiling`. Over the network only two of those four are admitted, and on
`/v1` only two are constructible at all — see below. `transport` is the
hardcoded literal described under [Transport](#transport) and must not be
trusted.

### Journal entry — `creek.journal`

`creek.journal(content, external_id, timestamp=None, tier=None,
privacy_tier_ceiling)` is the **entry → fragment** path Adepthood wants: no file
path, an `external_id` that makes the call idempotent, and a required `tier`.
It returns
`{status, tool, tier_ceiling, external_id, fragment_id, action, tier, warnings[]}`,
where `action` distinguishes a create from an update and `warnings` is
deliberately the *ceiling-safe* advisory channel — the operator-facing warning
channel interpolates real vault fragment ids the caller's ceiling may not admit,
so it does not cross this boundary (`creek_mcp/tools/journal.py`).

This supersedes the original draft's "a dedicated path is planned in #754";
#754 is closed. **Adepthood should call `creek.journal`, not `creek.ingest`,
for journal entries.**

### Ingest a file — `creek.ingest`

`creek.ingest(source_type, input_path, privacy_tier_ceiling)` remains the
separate, file-path tool: it ingests a single source file into fragments and
returns
`{status, tool, tier_ceiling, written, errors[], affected_fragment_ids[], created_tier}`.
Writes are subject to the write-side tier ceiling: a consumer at `ceiling=open`
cannot create `personal`/`intimate` fragments (the call is refused).

### Upload — `creek.upload`

`creek.upload(filename, content_base64, external_id, timestamp=None, tier=None,
privacy_tier_ceiling)` stages one document's base64 bytes under
`00-Creek-Meta/adepthood/uploads/`, routes it to an ingestor by extension, and
ingests it through the `upload` ledger so the staged bytes carry an `origin_key`
and fall inside the RTBF purge sweep (contract `0.3.0`, #1023).

**`tier` is required**, exactly as on `creek.journal`, since contract `0.7.0`
(#1494). The `None` in the signature is not a default tier: omitting it is
refused before anything is staged, ingested or audited, with the shared
`TIER_REQUIRED_REASON` (`creek_mcp/tools/upload.py`). It returns `warnings`
alongside its counts since contract `0.5.0` (#1372) — the same ceiling-safe
advisory channel described under [`creek.journal`](#journal-entry--creekjournal).

### Classify — `creek.classify`

`creek.classify(method="rules", force=False, privacy_tier_ceiling)` re-classifies
existing fragments (`rules` is offline/local; `llm` requires consent) and returns
counts: `{total, classified, preserved_manual, preserved_llm, skipped_high_confidence, errors[]}`.
Classification assigns ontology coordinates (frequency, phase, …) but creates no
new privacy tier.

### Reflect — `creek.reflect`

`creek.reflect(content=None, entry_ref=None, privacy_tier_ceiling)` produces
anchored **Higher-Self margin notes** on a *single* entry — short second-person
reflections in the user's own ontology language, bound to spans of the entry.

Four statuses, all observable:

| `status` | When | Payload |
|---|---|---|
| `ok` | Notes were produced | `{tool, tier_ceiling, routed_tier, notes[], essay_grounded: false}`, plus `essay` when the model wrote one |
| `empty` | The model returned nothing usable | Same shape, `notes: []` |
| `escalate` | The care guard matched | `{tool, tier_ceiling, reason: "acute_distress_markers", care_signal}` — no model call |
| `refused` | No entry content, `entry_ref` not found, or the entry's tier exceeds the ceiling | `{tool, tier_ceiling, reason}` |

`essay_grounded` is always `false` and says so on the wire: `essay` is free
model prose and is **not** verbatim/grounding-checked the way `notes[].quote`
is. A client must not treat it as grounded.

#### The compiled layer on the reflection surface (contract `0.9.0`, #873)

Since contract `0.9.0`, an `ok`/`empty` reflection may additionally carry two
**optional** fields naming the compiled structures the reflected entry belongs
to:

| Field | Shape | Bound |
|---|---|---|
| `related_praxis` | `{title, praxis_type, status, excerpt}` | ≤ 3 |
| `related_eddies` | `{title, description, fragment_count, formed}` | ≤ 2 |

Both are **absent** — not present-and-empty — whenever nothing qualifies, so a
consumer written against `0.8` parses an unchanged response in the ordinary
case. `praxis_type` and `status` are closed enums mirroring the vault's own
`PraxisType` / `PraxisStatus` vocabularies.

**These are compiled artifacts, and the tier rule for them is stricter than
for a fragment.** An eddy page's `description` and `fragment_count` are
synthesised *from its members*, and a praxis page is distilled *from* the
fragments its `derived_from` names; neither page carries a `privacy_tier` of
its own, so there is nothing on the page itself to rank. The rule Creek
applies, in `creek_mcp/compiled_pages.py`, is therefore:

> **Provenance authorizes; seeds only select.**

A page is published only when **every** fragment it was compiled from resolves
on disk *and* ranks within the caller's `privacy_tier_ceiling`. A page whose
provenance cannot be enumerated in full — an eddy whose `fragment_count`
exceeds the members that can be found, a praxis naming an id that no longer
resolves, a page declaring no sources at all — is treated as **opaque** and
withheld. "No provenance" is never read as "no sources". The consequence a
consumer should rely on: a remote caller capped at `personal` can never be
handed an eddy compiled from an `intimate` fragment, and cannot distinguish
"no such eddy" from "that eddy was withheld".

Selection reuses the fragment ids the grounding retrieval pass already
resolved, so there is no second embedding sweep and **no new egress path** —
the lookup reads vault markdown and nothing else (ADR-0004).

### Wheel — `creek.wheel`

`creek.wheel(privacy_tier_ceiling)` returns a per-frequency **balance read**
(F1–F10) for the Adepthood Map — how much of the user's classified material
resonates with each APTITUDE frequency — without egressing fragment bodies:

```json
{
  "status": "ok",
  "tool": "creek.wheel",
  "tier_ceiling": "open",
  "total_classified": 412,
  "unclassified": 37,
  "wheel": { "F1": { "name": "Agency", "count": 61, "share": 0.148 }, "…": {} }
}
```

`share` is `count / total_classified` — the fraction of the **classified**
corpus, not of the whole vault — and is `0.0` for every frequency when nothing
is classified. `unclassified` is reported alongside rather than folded in, so a
client can tell "this user is balanced" from "this vault has not been
classified" (`creek_mcp/tools/wheel.py`).

## Tier ceiling semantics

`TierCeiling` is a `StrEnum` with four values, least- to most-permissive:

| Ceiling | Admits content up to tier |
|---|---|
| `open` (default) | open only |
| `personal` | open + personal + unclassified |
| `intimate` | open + personal + intimate |
| `all` | everything |

> **Amendment (2026-07-30, #961):** `unclassified` (untiered) content now
> ranks with `personal`, not `open`. It is content nobody has classified,
> so it needs an explicit `personal` ceiling to be admitted — matching the
> ranking `creek.classify.privacy_filter` has used since #876. Previously
> the MCP ceiling ranked it with `open`, so an `open`-ceiling consumer
> could read untiered content; that is no longer true. Operator remedy: run
> `creek classify` (or `creek process`) to assign every fragment a
> deliberate tier — an `open`-ceiling consumer reads nothing from a vault
> that has been ingested but not yet classified.

- A fragment is admitted only when `rank(tier) <= rank(ceiling)`. **INTIMATE
  (rank 2) is therefore excluded under any ceiling below `intimate`** — this is
  how "INTIMATE never egresses" is enforced: not a special case, but a rank
  comparison applied uniformly to reads and writes.
- **Writes are refused, not downgraded.** A consumer at `ceiling=open` attempting
  to create personal/intimate content gets a structured refusal
  (`{status: "refused", tool, tier_ceiling, reason}`), never a silent tier drop.

### The remote cap — the rule the original draft omitted entirely

The rank comparison above is only half the story, and the missing half is the
load-bearing one. **A remote caller may request only `open` or `personal`.**
`intimate` and `all` are refused *before dispatch*, so intimate content is never
even read for such a call.

<!-- capability-set: remote-ceiling-cap -->
The cap is `REMOTE_ADMITTED_CEILINGS` in `creek_mcp/policy.py`, and it admits
exactly these ceilings for a remote caller:

| Ceiling | Admitted remotely |
|---|---|
| `open` | yes |
| `personal` | yes |
<!-- /capability-set -->

Three properties a consumer should rely on:

- **The refusal is a single constant, `REMOTE_CEILING_REFUSAL_REASON`, not a
  template.** It names no fragment, path, count or requested value, so a remote
  consumer learns nothing from a refusal beyond the published rule it already
  had (#1090). An unrecognised ceiling from a remote caller is refused too —
  never coerced to `open` and dispatched.
- **It is enforced before dispatch**, in `_BoundedFastMCP.call_tool`
  (`creek_mcp/server.py`), which dispatches only on the positive `Admission`
  verdict. Local stdio callers are not capped: *the network is the boundary,
  not the tier.*
- **On `/v1` the two forbidden members are not constructible at all.**
  `creek_mcp.api.models.WireTierCeiling` has exactly two members, `open` and
  `personal`, which is why the `/v1` handshake advertises
  `tier_model.ceilings == ["open", "personal"]` while the MCP handshake
  advertises all four.

The original draft said an Adepthood consumer "must explicitly request
`ceiling=intimate` (or `all`), which the user/operator controls". That is true
of a **local stdio** consumer and false of a remote one, for which no request
can reach intimate content. See
[Open questions](#open-questions-resolve-before-accepted) — this is the
divergence from Adepthood's ratified Decisions (b) and (c), and it is not
resolved here.

> **Naming note (#875/#1094).** Both issues spell this cap
> `_REMOTE_ADMITTED_CEILINGS`; #875 additionally places it at
> `server.py:73-75`, while #1094 names the symbol and cites no file or line.
> **No such symbol exists under that spelling.** The constant is
> `REMOTE_ADMITTED_CEILINGS`, public, at `creek_mcp/policy.py:69`, where it
> moved when #1073 split policy from the MCP adapter so `/v1` could reach the
> same verdict without an MCP request context.
> `test_the_adr_names_the_remote_ceiling_cap_by_its_real_constant` reads the
> fenced `remote-ceiling-cap` block above — not this whole document — and
> asserts that block names the live spelling, cites `creek_mcp/policy.py`, and
> contains no occurrence of the dead one. This paragraph quotes the dead
> spelling deliberately, in order to correct it, and sits outside that fence
> for exactly that reason.

Every tool call (including refusals) is appended to a **body-free, hash-chained**
audit log (`00-Creek-Meta/audit/mcp.jsonl`): argument *summaries* only (long
strings become `{"len": N}`), tagged with the `consumer`, tamper-evident via a
`prev_hash` chain. An intimate body never enters the log.

## Ontology version

The shared vocabulary is Creek's **APTITUDE frequency** axis, defined
canonically in
[`docs/Ontology/creek_ontology_agent_prompt.md`](../Ontology/creek_ontology_agent_prompt.md)
§6.1 and implemented as the single source of truth in
`creek-tools/creek/generate/ontology_glossary.py`, `…/indexes.py`, and
`…/models.py`. Adepthood addresses its counterpart axis as **Aspects** and its
curriculum axis as **Stages**, so the three names are expected to line up
member-for-member — but **that alignment is at the naming layer only; it is
not a semantic identity.** That hedge is not optional here: it is the ruling
of the Accepted sibling ADR
[`2026-07-31-adepthood-http-application-api.md`](./2026-07-31-adepthood-http-application-api.md)
(*"the F1–F10 ↔ ten-stage numeric coincidence is not a semantic identity"*),
which this draft does not reopen. Concretely, a Creek frequency `share` and an
Adepthood per-stage `fullness` are different quantities under matching names,
and nothing may be projected from one onto the other on the strength of the
name alone.

> **Unverified from this repo.** Nothing in this repository records
> Adepthood's Aspect or Stage definitions, names, or ordering. The
> member-for-member alignment above is asserted by the Adepthood side
> (`adepthood#950`) and mirrored here; it is not something Creek's own
> sources can show. Only the Creek half — the frequency axis of §6.1 and its
> implementation — is verifiable in-tree.

`ONTOLOGY_VERSION` **is** a code constant:
`creek_mcp/contract.py` holds `ONTOLOGY_VERSION: Final[str] =
"aptitude-wavelength/2026-05-23"`, dated to the last canonical change — the
frequency-naming decision
([`2026-05-23-frequency-naming.md`](./2026-05-23-frequency-naming.md), ONTOLOGY-001/#265).
Both surfaces publish it: the MCP handshake
(`creek_mcp/tools/handshake.py`) and `GET /v1/capabilities`
(`creek_mcp/httpapi/capabilities.py`), the latter at every status, so a client
can renegotiate against a server whose vault does not exist yet. Both sides
MUST treat a mismatch in this string as "renegotiate the contract".

This supersedes the original draft's "there is no ontology version constant in
code today … a follow-up may promote it". #750 shipped, and the promotion
happened with it; the doc and the wire now agree mechanically, and the header
fence at the top of this document is asserted against the constant on every
test run.

### Frequencies (F1–F10)

| Code | Canonical name | Core theme |
|---|---|---|
| F1 | Agency | Survival, intentional action, willpower, initiative |
| F2 | Receptivity | Kinship, receptivity to pleasure and Source, surrender, trust |
| F3 | Self-Love / Power | Self-love as the foundation of healthy power |
| F4 | Community Love / Conformity | Community love, devotion, moral grounding, hierarchy |
| F5 | Achievism | Innovation, analysis, goal-setting, material success |
| F6 | Pluralism | Empathy, inclusivity, embodied connection, shadow work |
| F7 | Integration | Systems thinking, synthesis, holistic understanding |
| F8 | True Self / Transcendence | Higher self, monad, gnosis, transcendent pattern recognition |
| F9 | Unity | Source connection, cosmic harmony, non-dual awareness |
| F10 | Emptiness | Impermanence, no-self, egolessness |

### Wavelength phases

`rising → peaking → withdrawal → diminishing → bottoming_out → restoration`
(plus `unclassified`). A six-phase rise-and-fall cycle; an entry sits at one
phase.

**Neither Wavelength axis is that shared vocabulary.** This is the confusion
worth guarding against, and it has already produced wrong text in both repos —
first as *"= Wavelength phases"*, then as *"= Wavelength Modes"*. §7 of the
ontology prompt defines the Archetypal Wavelength, and it is a *different*
subject from §6.1: §7.1 gives the phases, §7.2 the Modes. Both are axes the
frequency vocabulary is classified *along*, not other names for it:

- **Modes** — the five functional stances §7.2 names (Inhabit, Express,
  Collaborate, Integrate, Absorb), each paired with a Do/Feel orientation.
  §7.2 scopes itself to the nine frequencies Beige through Ultraviolet and
  assigns Clear Light (F10) no Mode at all; `creek.models.Mode` implements the
  same five (plus `unclassified`). A Mode *groups* frequencies — Beige and
  Purple are both Inhabit — so it cannot be one of them.
- **Phases** — the six-part rise-and-fall cycle above. A phase is where in that
  cycle an entry sits, not which frequency it is.

Ten, five, six: three axes, three cardinalities. Cardinality is used here in
one direction only. A *differing* count is enough to rule an axis out — a
ten-member list is not a five- or six-member one, which is what disqualifies
both Wavelength axes. A *matching* count proves nothing: **shared cardinality
is not evidence of identity.** That is the reasoning error that put the wrong
axis into these ADRs in the first place, and it is the same error the sibling
ADR names when it refuses to read the F1–F10 ↔ ten-stage coincidence as a
semantic identity — which is why the frequency/Aspect/Stage alignment above
stays a naming-layer claim rather than being argued from ten-ness.

## Open questions (resolve before `Accepted`)

The original draft carried five. Four of them are answered in code and have
been folded into the body above: the ontology version **is** a constant; Creek
**does** enforce the care guardrail; the remote transport and per-consumer auth
**shipped**; and both the reflect and wheel return shapes are implemented, with
their `/v1` counterparts hash-pinned as JSON Schemas in the committed
`docs/contracts/adepthood-v1/` bundle. What remains is genuinely open, and
listing it explicitly is the point — none of it is resolved by omission.

1. **Intimate transit — the substantive divergence. Neither side wins here.**
   Adepthood's ratified contract Decision (b) expects attestation-gated
   INTIMATE writes (ciphertext-only transit, enclave attestation returned via
   an `attestation` field on the handshake) and Decision (c) an
   end-to-end-encrypted reflection channel. **Creek's shipped answer is a flat,
   unconditional refusal**: `REMOTE_ADMITTED_CEILINGS` admits only
   `open`/`personal` for any remote caller, `_BoundedFastMCP.call_tool` refuses
   before dispatch, the handshake returns **no `attestation` field**, and
   `/v1`'s `WireTierCeiling` cannot express `intimate` at all. The two contracts
   are consistent *today* only because Adepthood skips the vault pre-call for
   intimate entries — which is a behaviour, not an agreement. **Neither
   document may flip to `Accepted` until this is reconciled**, and neither may
   silently adopt an end-state: a doc that quietly ratifies either answer is
   worse than a stale one.

2. **#757 is closed, and what it shipped points the other way.**
   [#757](https://github.com/Geoffe-Ga/Creek-Vault/issues/757) — the
   confidential-hosting epic #1094 named as owning the confidential path — is
   **CLOSED**, so "blocked on #757, until it lands" is no longer the right
   frame, and "no attestation path exists" is no longer accurate either.
   Enclave attestation machinery is in-tree: `creek/config.py` carries
   `enclave_url`, `enclave_expected_measurement` and
   `enclave_attestation_pubkey`, and `creek/classify/llm/providers.py` has
   `verify_enclave_attestation` with a nonce anti-replay check, an Ed25519
   signature check and fail-closed refusals. **But it attests an *egress* peer**
   — Creek verifying a model enclave before sending it prompts — **not an
   *ingress* consumer**, which is what Adepthood's Decision (b) needs. So the
   named capability is still structurally unreachable while its owning epic is
   closed. The open question is: does the existing attestation primitive get
   reused for consumer ingress, or does the contract drop attestation and
   ratify the flat refusal of question 1? Neither answer is Creek's alone to
   give.

3. **Client-supplied consumer identity.** `CREEK_MCP_CONSUMER` still exists as
   the local-stdio audit default, while remote MCP derives the consumer from
   the bearer's `client_id` and `/v1` does the same.
   [#1094](https://github.com/Geoffe-Ga/Creek-Vault/issues/1094)'s own
   acceptance item 4 asks for the client-side `CONSUMER_ID = "CREEK_MCP_CONSUMER"`
   constant in `backend/src/domain/creek_vault.py` to be **deleted** rather than
   corrected. **This repository cannot verify that deletion** — that file does
   not exist here, and a `backend/` directory does not either; what the
   truth-up could observe of the consumer side is recorded, with its method and
   its date, in [Scope of the 2026-08-21 truth-up](#scope-of-the-2026-08-21-truth-up-875--1094).
   Creek's side is described precisely under
   [Consumer identity & auth model](#consumer-identity--auth-model); the
   Adepthood side is theirs to change and theirs to confirm.

4. **Blank consumer `client_id` at the verifier boundary** —
   [#1100](https://github.com/Geoffe-Ga/Creek-Vault/issues/1100) is open. See
   the residual note under
   [Consumer identity & auth model](#consumer-identity--auth-model). The auth
   story is not being claimed as fully closed.

5. **The handshake's `transport` field is a constant** and reports `stdio` over
   the network transport. Recorded under [Transport](#transport) as a code
   defect. Whether the fix is to make the field live, or to remove it as
   unfixable-without-a-request-context, is undecided; a consumer must not
   depend on it either way in the meantime.

6. **Whether an `incompatible` capabilities response should probe the vault at
   all.** It does today, so `vault.available` is a real boolean at that status,
   and both capability-state tables now say so — that was
   [#1150](https://github.com/Geoffe-Ga/Creek-Vault/issues/1150)'s requested
   correction, and it is delivered and pinned. The residual question #1150
   raised in passing — whether the cheap version check should be reordered
   *ahead* of the vault probe, which would make the cell genuinely unspecified
   rather than merely undocumented — **outlives the issue**:
   [#1148](https://github.com/Geoffe-Ga/Creek-Vault/issues/1148) is closed and
   changed only the *audit* half. It is therefore recorded here, in a document
   consulted at every bump, rather than left on a closed issue — the same
   reasoning as the OpenAPI revisit trigger in the sibling ADR. The current
   behaviour and the argument for keeping the probe are in that ADR's state
   table and in `creek_mcp/httpapi/capabilities.py`.

7. **Join key: colour or name?** — unresolved, and stated as a question rather
   than as guidance because neither answer is currently implementable. The
   *case* for colour is in-repo: this repo's own frequency labels drifted from
   the canon once already
   ([`2026-05-23-frequency-naming.md`](./2026-05-23-frequency-naming.md),
   ONTOLOGY-001 — canonical names won and the drifted artefacts were
   re-pinned), so a name join can mismatch silently wherever two sides' labels
   differ, while a colour designation is a 1:1 map this repo machine-checks
   (`FREQUENCY_COLORS` in `creek-tools/creek/generate/indexes.py`, sourced from
   §6.1 of the ontology prompt — **the code-side source of truth for the colour
   designations**, which the prompt itself also publishes in prose in the
   colour column of §6.1 and again in §7.2, and which the repo's two derived
   maps track: `_ALTITUDE_COLORS` in `creek/generate/ontology_glossary.py` and
   `_FREQUENCY_TO_COLOR` in `creek/classify/weighted.py`, the latter pinned
   against `FREQUENCY_COLORS` by `tests/test_weighted.py`). Two limits keep
   this an open question:
   - **Colour is on neither side's wire.** No `/v1` response model and no
     schema under `docs/contracts/adepthood-v1/schemas/` publishes a colour
     field; `FREQUENCY_COLORS` is an internal map that renders vault indexes
     and classifier prompts. Adopting a colour join therefore means publishing
     the designation on both surfaces first — a coordinated contract change,
     not a documentation preference.
   - **Only Creek's half of the map is knowable here.** Whether Adepthood
     designates its Aspects or Stages by the same colours is **not recorded
     anywhere in this repo** and is unverified from it. Until it is, "join on
     the colour" is advice about a key one side may not have.

### Scope of the 2026-08-21 truth-up (#875 / #1094)

[#1094](https://github.com/Geoffe-Ga/Creek-Vault/issues/1094) asks for four
things. **Only the first is performable in this repository**, and it is done
above. Acceptance items 2–4 change files that exist only in
`Geoffe-Ga/adepthood` — `docs/creek-vault-mcp-contract.md` and
`backend/src/domain/creek_vault.py`; neither path, nor a `backend/` directory
at all, exists here. **No pull request in this repository can close them**, so
#1094 is answered here only for its Creek half, and its consumer half is
reported below as an observation rather than as a Creek deliverable.

The three consumer-side items were checked on **2026-08-21** by reading
`Geoffe-Ga/adepthood` through the GitHub API — not from this working tree, and
not from any file this repository's tests can pin. Nothing in this repository
re-checks these rows; they can go stale the moment the consumer changes, and
the authority on each is the adepthood repository itself.

| #1094 item | Where it lives | Status (this repo's evidence) |
|---|---|---|
| 1. Update the Creek copy to the shipped surface | this document | **Done** — pinned by `tests/test_mcp_contract_adr_shipped_surface.py` |
| 2. Mark adepthood's Decisions (b)/(c) as blocked, not guaranteed | `adepthood` | Not closeable here. Observed 2026-08-21: `docs/creek-vault-mcp-contract.md` now points at ADR 0004 Decision 6 and records the ciphertext/attested transit topology as "entirely unshipped", with journal entries and document uploads both skip-only |
| 3. Remove adepthood's "one vocabulary under three names" / "same shape as `WheelBalanceResponse`" | `adepthood` | Not closeable here. Observed 2026-08-21: that document's Wheel section now calls the shared cardinality "a numeric coincidence, **not a semantic identity**" |
| 4. Delete client-side `CONSUMER_ID = "CREEK_MCP_CONSUMER"` | `adepthood` | Not closeable here. Observed 2026-08-21: `CONSUMER_ID` is absent from `backend/src/domain/creek_vault.py`, and adepthood's ADR 0004 records it as no longer existing in that codebase, resolved by `adepthood#2047` |

The consumer-side work landed under `adepthood#2044` (ADR 0004, which moved the
application boundary to HTTP/JSON) and the `/v1` cutover `adepthood#2047`,
which together retired that document's role as a mirror of this one. Note that
the boundary those issues ratify is the **`/v1` HTTP adapter**, described in the
[sibling ADR](./2026-07-31-adepthood-http-application-api.md); this document
remains the record for the MCP *agent* adapter, which Adepthood no longer
speaks.

Creek's side of item 3 is already ruled: the naming-layer hedge under
[Ontology version](#ontology-version) is the Accepted sibling ADR's ruling,
`tests/test_ontology_vocabulary_docs_drift.py` pins it, and this truth-up does
not upgrade it to a semantic identity.

## Change log

| Contract version | Date | Change |
|---|---|---|
| *(no contract change)* | 2026-08-21 | **Documentation truth-up (#875/#1094).** The Creek copy now describes the shipped surface. No tool, shape, status, error code or version moved — this row records a doc catching up, not a contract event. The header's `Contract version` was corrected from a stale `0.4.0` to `0.9.0`; `creek_mcp/contract.py` has always been the runtime source of truth, so **the five-minor jump in the header is one stale string being fixed, not five contract events landing at once**. Four of the intervening minors had no row below either, and this truth-up writes them: `0.5.0` (#1372), `0.6.0` (#1453) and `0.7.0` (#1494) each moved the MCP tool surface and belonged in this document all along, while `0.8.0` (#1524) is `/v1`-only and is recorded because the minor is shared. Substantively: the *Planned* labels on `creek.reflect` (#751), `creek.wheel` (#752), the Adepthood-journal path (now `creek.journal`, #754) and the Creek-side care guardrail (#753) are replaced by statements of fact, all four issues being closed; the network transport and per-consumer bearer auth (#755, closed) replace "out of scope for this contract version"; `creek.upload` and `drive-connector` join the capability record; and the **remote tier cap** — `REMOTE_ADMITTED_CEILINGS`, which admits only `open`/`personal` for a network caller and was absent from this document entirely — is now written down, under its real name and module. Status stays `Draft`: the intimate-transit divergence in [Open questions](#open-questions-resolve-before-accepted) blocks `Accepted` on both sides. Five machine-checked fences now pin the version strings, the capability→tool table, the handshake example, the transport table and the remote-ceiling constant against live code. |
| `0.9.0` | 2026-08-19 | `creek.reflect` and `POST /v1/reflections` may carry two **optional** fields, `related_praxis` and `related_eddies`, naming the compiled-layer structures the reflected entry belongs to (#873). Two wire models (`RelatedPraxis` / `RelatedEddy`) and their schemas join the published bundle. No route, capability, error code, or *required* field moves, so `SUPPORTED_CONTRACT_MINORS` widens rather than shifts and a `0.8` client keeps being served — byte-identically whenever nothing qualified, since the route omits both keys when they are absent. Admission is stricter than for a fragment: a compiled page is published only when every fragment it was compiled from is within the caller's ceiling, and a page whose provenance cannot be enumerated in full is withheld. See [The compiled layer on the reflection surface](#the-compiled-layer-on-the-reflection-surface-contract-090-873). The same minor publishes the sixth `/v1` capability, `drive-connector` (#1527), over the existing read-only Google Drive connector; it is additive for the same reason, because both halves read `CAPABILITY_SINCE_MINOR`. |
| `0.8.0` | 2026-08-18 | `/v1` publishes a fifth capability, `upload`, served by `POST /v1/uploads` (#1524). **No MCP surface moved**: `creek.upload` has existed since `0.3.0` and this minor only gives the HTTP adapter a door onto the same `upload_tool` — staging, extension dispatch, the ledger-backed `run_ingest`, the write-tier gate and the audit append are unchanged. The row is here because the contract minor is *shared* between the two adapters, not because an MCP client sees anything new. `SUPPORTED_CONTRACT_MINORS` widens rather than shifts. The full HTTP-side account, including the `unsupported_source` error code and the per-route body cap, is in the [sibling ADR](./2026-07-31-adepthood-http-application-api.md#change-log). |
| `0.7.0` | 2026-08-13 | **`tier` becomes mandatory on `creek.journal` and `creek.upload`** (#1494). Both verbs declared `tier: str = "open"` twice each — once on the tool function and again on the `build_server` wrapper MCP clients actually reach — so a client that omitted the field had its content filed at `open` and was told so nowhere, and `privacy_tier_ceiling` could not catch it because a defaulted `open` is trivially within an `open` caller's own ceiling. Both now refuse before anything is staged, ingested or audited, with the shared `TIER_REQUIRED_REASON` (`creek_mcp/tier_ceiling.py`): *"tier is required; pass open\|personal\|intimate explicitly"* (`creek_mcp/tools/journal.py`, `creek_mcp/tools/upload.py`). The same row covers **`creek.save`**, which took the identical break in [#1495](https://github.com/Geoffe-Ga/Creek-Vault/issues/1495) one commit earlier and shipped without a bump — recorded retroactively because that was a miss, not a precedent. An input becoming *required* is a break, so it cannot carry less than a minor. **No `/v1` shape moved** — `JournalUpsertRequest.tier` never had a default — so `SUPPORTED_CONTRACT_MINORS` widens rather than shifts and a `0.6`-pinned HTTP client is unaffected. |
| `0.6.0` | 2026-08-13 | `creek.purge.*` results gain two integer counters (#1453): `ledger_rows_removed`, the ingest-ledger rows a scoped purge physically erased, and `meta_artifacts_removed`, the files a whole-vault purge destroyed under `00-Creek-Meta/` under its new deny-by-default sweep. `_result_payload` forwards every field of `PurgeResult` (`creek_mcp/tools/purge.py`), so both reach the wire with no payload code change — which is why the minor has to move rather than why it need not: a client validating the payload closed would otherwise meet two keys it never negotiated. **No `/v1` shape moved**; `SUPPORTED_CONTRACT_MINORS` widens. Neither counter can carry vault content: both are plain integers, and the sweep's record of *what* it destroyed never leaves the vault's audit log. |
| `0.5.0` | 2026-08-12 | **Three MCP tools' return shapes changed** (#1372). `creek.link` now reports `largest_cluster_fragments`, `clusters_split` and `oversized_discarded` — the cluster-health diagnostics `creek link` already printed to its console, a discarded fragment being data loss (`creek_mcp/tools/link.py`). `creek.journal` and `creek.upload` now report `warnings`, the ingest run's content-free advisory channel. That is what carries the minor, by `contract.py`'s own rule: a tool's return shape moved. **Unlike `0.3.0`, `0.4.0`, `0.6.0` and `0.7.0`, a `/v1` shape moved too** — `JournalUpsertResponse` gained an *optional* `warnings` field — so the compatibility argument for that half is the sibling ADR's; here it is enough that `SUPPORTED_CONTRACT_MINORS` widens rather than shifts. Every advisory crossing this boundary is content-free *by construction at the producer*: the operator-facing warning channel interpolates real vault fragment ids the caller's ceiling may not admit, so those reach the CLI console only, never a remote caller. |
| `0.4.0` | 2026-08-08 | `creek.purge.*` gains a third status, `partial` (#1246). An erasure that finished but fell short — a fragment whose body is not valid UTF-8 skips the content-keyed `07-Voice` sweep, so a profile may still quote it — used to be reported over MCP as unqualified `ok`, while `purge.jsonl` recorded `status="partial"` and the CLI said so in red. The payload is now derived from `PurgeResult` rather than a hand-picked subset, so all six previously-dropped fields reach the caller (`embeddings_removed`, `provenance_scrubbed`, `intimate_stubs_removed`, `journal_staged_removed`, `voice_artifacts_removed`, `voice_body_undecodable` — the last names the fragments the sweep could not reach). A tool's return shape moved, so the contract minor moves. `refused` and `ok` keep their spellings; a client that does not know `partial` falls through its branches rather than reading an incomplete erasure as a clean one. No `/v1` shape changed — `creek.purge.*` is elevated-token-gated and out of the Adepthood surface — so the HTTP adapter keeps serving `0.3` and `0.2` alongside `0.4` (`SUPPORTED_CONTRACT_MINORS`). |
| `0.3.0` | 2026-08-08 | `creek.upload` joins the tool surface (#1023): one document's base64 bytes are staged under `00-Creek-Meta/adepthood/uploads/`, routed to an ingestor by extension, and ingested through the `upload` ledger so the staged bytes carry an `origin_key` and fall inside the RTBF purge sweep. A capability was added, so the contract minor moves. Nothing existing changed shape: the `/v1` HTTP adapter keeps serving contract minor `0.2` alongside `0.3` (`SUPPORTED_CONTRACT_MINORS`), and no existing tool's arguments or return shape were touched. |
| `0.2.0` | 2026-07-30 | `unclassified` (untiered) content now ranks with `personal`, not `open`, on the MCP ceiling — matching `creek.classify.privacy_filter` since #876 (#961). `open`-ceiling consumers no longer read untiered content; remedy is `creek classify`. See the amendment note under [Tier ceiling semantics](#tier-ceiling-semantics). |
| `0.1.0` | 2026-06-30 | Initial draft, mirroring `adepthood#950`. Enumerates capabilities, maps to existing (`creek.ingest`, `creek.classify`) and planned (`creek.handshake`/`reflect`/`wheel`) tools, fixes tier/care/auth/transport semantics, pins ontology version. |

> **Every minor from `0.1.0` to `0.9.0` has a row above**, including the four
> — `0.5.0`, `0.6.0`, `0.7.0`, `0.8.0` — that this document omitted until
> 2026-08-21. Three of them moved the MCP surface and belonged here all along:
> `0.5.0` changed `creek.link` / `creek.journal` / `creek.upload` return
> shapes, `0.6.0` added two `creek.purge.*` counters, and `0.7.0` made `tier`
> mandatory on the write verbs. Only `0.8.0` is `/v1`-only, and it is recorded
> here anyway because the minor is shared between the two adapters. Where a row
> concerns the HTTP half, the fuller account is in the [sibling ADR's change
> log](./2026-07-31-adepthood-http-application-api.md#change-log).
> `SUPPORTED_CONTRACT_MINORS` (`creek_mcp/api/models.py`) is the authority on
> which minors are served, and it currently spans `0.2`–`0.9`.
