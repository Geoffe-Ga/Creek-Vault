# Adepthood ↔ Creek MCP Contract

- **Status**: Draft — pending agreement
- **Date**: 2026-06-30
- **Contract version**: `0.4.0` (draft)
- **Ontology version**: `aptitude-wavelength/2026-05-23` (see [Ontology version](#ontology-version))
- **Driving issues**: [#748](https://github.com/Geoffe-Ga/Creek-Vault/issues/748) (epic), [#749](https://github.com/Geoffe-Ga/Creek-Vault/issues/749) (this doc)
- **Mirrors**: [`geoffe-ga/adepthood#950`](https://github.com/geoffe-ga/adepthood/issues/950) (Adepthood's required side)
- **Boundary**: [`geoffe-ga/adepthood#927`](https://github.com/geoffe-ga/adepthood/issues/927)

> **This is a draft pending agreement with the Adepthood side.** It is published
> in lock-step with `adepthood#950` so both repos implement against one agreed
> surface. Until both sides mark their copy `Accepted`, tool names, shapes, and
> version strings here are proposals, not commitments. Sections describing tools
> that **do not exist yet** are explicitly labelled *Planned* with their tracking
> issue.

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
Adepthood depends on, maps each to an existing or planned Creek MCP tool, and
fixes the privacy/care/auth/transport semantics both sides build against.

Creek's existing MCP surface and registration are documented in
[`creek-tools/docs/mcp.md`](../../creek-tools/docs/mcp.md); this contract is the
cross-repo view layered on top of it. Where the two disagree, `docs/mcp.md`
describes *what ships today* and this contract describes *what the two repos have
agreed to*.

## Transport

- **Today**: JSON-RPC over **stdio**. The server (`creek-tools-mcp`,
  `FastMCP("creek-tools-mcp")`) is launched with `run(transport="stdio")`
  ([`creek_mcp/server.py`](../../creek-tools/creek_mcp/server.py)). A consumer
  connects it as a child process; there is no network listener.
- **Remote**: a remote Adepthood client reaching a local vault requires a
  network transport and a stronger consumer-auth model. That is **out of scope
  for this contract version** and is being decided in
  [#755](https://github.com/Geoffe-Ga/Creek-Vault/issues/755). Until #755 lands,
  the contract assumes the consumer and the vault share a host and the stdio
  channel.

## Consumer identity & auth model

| Mechanism | Env var | Enforced? | Purpose |
|---|---|---|---|
| Consumer identity | `CREEK_MCP_CONSUMER` | **No — audit-only** | Tags every audit-log entry so calls from `claude-code` vs `crawdad` vs an Adepthood consumer are distinguishable. Free-form string; defaults to `unknown`. Read once at server start (`server.py`). It does **not** grant or deny any capability. |
| Elevated operations | `CREEK_MCP_ELEVATED_TOKEN` | **Yes — constant-time HMAC compare** | Gates the destructive `creek.purge.*` family only ([`creek_mcp/auth.py`](../../creek-tools/creek_mcp/auth.py)). Fails closed when either side is empty. Adepthood is **not** expected to hold this token; purge is out of the Adepthood surface. |

For this contract, an Adepthood consumer SHOULD set
`CREEK_MCP_CONSUMER=adepthood` so its traffic is attributable in the audit log.
A capability-bearing consumer-auth model (tokens that actually gate non-purge
tools) is part of the remote-transport decision ([#755](https://github.com/Geoffe-Ga/Creek-Vault/issues/755)),
not this version.

## Privacy gate and care gate — who enforces what

- **Privacy gate — enforced Creek-side (server), at the MCP boundary.** Every
  non-purge tool takes a `privacy_tier_ceiling` parameter
  (`TierCeiling`, default `OPEN`). Reads above the ceiling are refused and writes
  above the ceiling are **refused, not silently downgraded**
  ([`creek_mcp/tier_ceiling.py`](../../creek-tools/creek_mcp/tier_ceiling.py)).
  See [Tier ceiling semantics](#tier-ceiling-semantics). This is the same single
  boundary that already serves Claude Code and CrawDad — Adepthood does not get a
  side door.
- **Care gate — enforced Adepthood-side (caller) today; a Creek-side guardrail is
  *planned*.** The "care gate" (no medical advice; crisis content routes to a
  human, never an autoreply) is **not modelled in Creek code today**. Adepthood,
  as the user-facing app, owns it. Creek will add a defensive guardrail in the
  `reflect`/voice path so a reflection request carrying crisis markers cannot
  produce an unsafe autoreply — tracked in
  [#753](https://github.com/Geoffe-Ga/Creek-Vault/issues/753). Until #753 lands,
  the care gate is the consumer's responsibility and this contract records it as
  caller-enforced.

## Capabilities → MCP tools

| Capability Adepthood depends on | Status | MCP tool | Tracking |
|---|---|---|---|
| Handshake / capabilities (+ contract & ontology version, tier model) | **Exists** | `creek.handshake` | [#750](https://github.com/Geoffe-Ga/Creek-Vault/issues/750) |
| Ingest — journal entry → fragment | **Exists** (generic) + dedicated path *planned* | `creek.ingest` | [#754](https://github.com/Geoffe-Ga/Creek-Vault/issues/754) (Adepthood-journal path) |
| Classify a fragment | **Exists** | `creek.classify` | — |
| Reflect — Higher-Self margin notes on one entry | **Planned** | `creek.reflect` | [#751](https://github.com/Geoffe-Ga/Creek-Vault/issues/751) |
| Wheel — per-frequency balance read for the Map | **Planned** | `creek.wheel` | [#752](https://github.com/Geoffe-Ga/Creek-Vault/issues/752) |
| Tier ceiling — INTIMATE never egresses | **Exists** | (parameter on every tool) | — |

### Handshake / capabilities — *Exists* (#750)

A read-only, LLM-free tool that lets a consumer discover, in one call: whether a
vault is present (`available`), the **contract version** (`0.3.0`, from
`creek_mcp/contract.py`), the **ontology version**
(`aptitude-wavelength/2026-05-23`), the **tier model** (the `TierCeiling`
ceilings and the INTIMATE-never-egresses guarantee), and the **capabilities**
list — the names of the tools actually registered, sourced live from
`server.list_tools()` so it cannot drift. Adepthood calls this first to confirm
both sides speak the same contract before any read/write. Return shape:

```jsonc
{
  "status": "ok",
  "tool": "creek.handshake",
  "server": "creek-tools-mcp",
  "transport": "stdio",
  "available": true,
  "contract_version": "0.3.0",
  "ontology_version": "aptitude-wavelength/2026-05-23",
  "tiers": ["open", "personal", "intimate"],
  "tier_model": { "ceilings": ["open", "personal", "intimate", "all"],
                  "default": "open", "intimate_never_egresses": true },
  "capabilities": ["creek.classify", "creek.handshake", "creek.ingest", "..."]
}
```

### Ingest — `creek.ingest` (exists); Adepthood-journal path *Planned* (#754)

Today `creek.ingest(source_type, input_path, privacy_tier_ceiling)` ingests a
single source file into fragments and returns
`{status, tool, tier_ceiling, written, errors[], affected_fragment_ids[], created_tier}`.
A journal **entry** (not a file path) → fragment path tuned for the Adepthood
data shape is planned in [#754](https://github.com/Geoffe-Ga/Creek-Vault/issues/754).
Writes are subject to the write-side tier ceiling: a consumer at `ceiling=open`
cannot create `personal`/`intimate` fragments (the call is refused).

### Classify — `creek.classify` (exists)

`creek.classify(method="rules", force=False, privacy_tier_ceiling)` re-classifies
existing fragments (`rules` is offline/local; `llm` requires consent) and returns
counts: `{total, classified, preserved_manual, preserved_llm, skipped_high_confidence, errors[]}`.
Classification assigns ontology coordinates (frequency, phase, …) but creates no
new privacy tier.

### Reflect — *Planned* (#751)

`creek.reflect` produces anchored **Higher-Self margin notes** on a *single*
entry — short second-person reflections in the user's own ontology language,
bound to spans of the entry. This is the capability that most needs the **care
gate** ([#753](https://github.com/Geoffe-Ga/Creek-Vault/issues/753)): it must not
emit medical advice and must route crisis content to a human.

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

### Wheel — *Planned* (#752)

`creek.wheel` returns a per-frequency **balance read** (F1–F10) for the Adepthood
Map — how much of the user's recent material resonates with each APTITUDE
frequency — without egressing fragment bodies.

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
- `INTIMATE` is reserved for self-authored fragments. An Adepthood consumer that
  needs to read or write intimate content must explicitly request
  `ceiling=intimate` (or `all`), which the user/operator controls — Adepthood
  cannot widen its own ceiling.

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

There is **no ontology version constant in code today.** This contract pins the
agreed vocabulary to the version string **`aptitude-wavelength/2026-05-23`**,
dated to the last canonical change — the frequency-naming decision
([`2026-05-23-frequency-naming.md`](./2026-05-23-frequency-naming.md), ONTOLOGY-001/#265).
The handshake tool ([#750](https://github.com/Geoffe-Ga/Creek-Vault/issues/750))
will surface this string at runtime; a follow-up may promote it to an
`ONTOLOGY_VERSION` code constant so the doc and the wire agree mechanically. Both
sides MUST treat a mismatch in this string as "renegotiate the contract".

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

1. **Ontology version string** — is the `aptitude-wavelength/2026-05-23` scheme
   acceptable to Adepthood, and should it become a code constant now or after the
   handshake tool ships?
2. **Care gate boundary** — does Adepthood want Creek's `reflect` path to *also*
   enforce the care guardrail (defence in depth, #753), or remain caller-only?
3. **Remote transport & consumer auth** — depends on
   [#755](https://github.com/Geoffe-Ga/Creek-Vault/issues/755); this contract
   assumes co-located stdio until then.
4. **Reflect / wheel return shapes** — pinned here only loosely; finalise the
   field-level schemas when #751/#752 are implemented.
5. **Join key: colour or name?** — unresolved, and stated as a question rather
   than as guidance because neither answer is currently implementable. The
   *case* for colour is in-repo: this repo's own frequency labels drifted from
   the canon once already
   ([`2026-05-23-frequency-naming.md`](./2026-05-23-frequency-naming.md),
   ONTOLOGY-001 — canonical names won and the drifted artefacts were
   re-pinned), so a name join can mismatch silently wherever two sides' labels
   differ, while a colour designation is a 1:1 map this repo machine-checks
   (`FREQUENCY_COLORS` in `creek-tools/creek/generate/indexes.py`, sourced from
   §6.1 of the ontology prompt — **the only in-repo source for the colour
   designations**). Two limits keep this an open question:
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

## Change log

| Contract version | Date | Change |
|---|---|---|
| `0.9.0` | 2026-08-19 | `creek.reflect` and `POST /v1/reflections` may carry two **optional** fields, `related_praxis` and `related_eddies`, naming the compiled-layer structures the reflected entry belongs to (#873). Two wire models (`RelatedPraxis` / `RelatedEddy`) and their schemas join the published bundle. No route, capability, error code, or *required* field moves, so `SUPPORTED_CONTRACT_MINORS` widens rather than shifts and a `0.8` client keeps being served — byte-identically whenever nothing qualified, since the route omits both keys when they are absent. Admission is stricter than for a fragment: a compiled page is published only when every fragment it was compiled from is within the caller's ceiling, and a page whose provenance cannot be enumerated in full is withheld. See [The compiled layer on the reflection surface](#the-compiled-layer-on-the-reflection-surface-contract-090-873). |
| `0.4.0` | 2026-08-08 | `creek.purge.*` gains a third status, `partial` (#1246). An erasure that finished but fell short — a fragment whose body is not valid UTF-8 skips the content-keyed `07-Voice` sweep, so a profile may still quote it — used to be reported over MCP as unqualified `ok`, while `purge.jsonl` recorded `status="partial"` and the CLI said so in red. The payload is now derived from `PurgeResult` rather than a hand-picked subset, so all six previously-dropped fields reach the caller (`embeddings_removed`, `provenance_scrubbed`, `intimate_stubs_removed`, `journal_staged_removed`, `voice_artifacts_removed`, `voice_body_undecodable` — the last names the fragments the sweep could not reach). A tool's return shape moved, so the contract minor moves. `refused` and `ok` keep their spellings; a client that does not know `partial` falls through its branches rather than reading an incomplete erasure as a clean one. No `/v1` shape changed — `creek.purge.*` is elevated-token-gated and out of the Adepthood surface — so the HTTP adapter keeps serving `0.3` and `0.2` alongside `0.4` (`SUPPORTED_CONTRACT_MINORS`). |
| `0.3.0` | 2026-08-08 | `creek.upload` joins the tool surface (#1023): one document's base64 bytes are staged under `00-Creek-Meta/adepthood/uploads/`, routed to an ingestor by extension, and ingested through the `upload` ledger so the staged bytes carry an `origin_key` and fall inside the RTBF purge sweep. A capability was added, so the contract minor moves. Nothing existing changed shape: the `/v1` HTTP adapter keeps serving contract minor `0.2` alongside `0.3` (`SUPPORTED_CONTRACT_MINORS`), and no existing tool's arguments or return shape were touched. |
| `0.2.0` | 2026-07-30 | `unclassified` (untiered) content now ranks with `personal`, not `open`, on the MCP ceiling — matching `creek.classify.privacy_filter` since #876 (#961). `open`-ceiling consumers no longer read untiered content; remedy is `creek classify`. See the amendment note under [Tier ceiling semantics](#tier-ceiling-semantics). |
| `0.1.0` | 2026-06-30 | Initial draft, mirroring `adepthood#950`. Enumerates capabilities, maps to existing (`creek.ingest`, `creek.classify`) and planned (`creek.handshake`/`reflect`/`wheel`) tools, fixes tier/care/auth/transport semantics, pins ontology version. |
