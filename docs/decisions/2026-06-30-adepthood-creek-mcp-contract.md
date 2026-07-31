# Adepthood ↔ Creek MCP Contract

- **Status**: Draft — pending agreement
- **Date**: 2026-06-30
- **Contract version**: `0.2.0` (draft)
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
vault is present (`available`), the **contract version** (`0.2.0`, from
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
  "contract_version": "0.2.0",
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

The shared vocabulary — **Adepthood Aspects = Creek APTITUDE frequencies =
Archetypal Wavelength phases** — is defined canonically in
[`docs/Ontology/creek_ontology_agent_prompt.md`](../Ontology/creek_ontology_agent_prompt.md)
(frequencies §6.1, Wavelength §7) and implemented as the single source of truth in
`creek-tools/creek/generate/ontology_glossary.py`, `…/indexes.py`, and
`…/models.py`.

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

## Change log

| Contract version | Date | Change |
|---|---|---|
| `0.2.0` | 2026-07-30 | `unclassified` (untiered) content now ranks with `personal`, not `open`, on the MCP ceiling — matching `creek.classify.privacy_filter` since #876 (#961). `open`-ceiling consumers no longer read untiered content; remedy is `creek classify`. See the amendment note under [Tier ceiling semantics](#tier-ceiling-semantics). |
| `0.1.0` | 2026-06-30 | Initial draft, mirroring `adepthood#950`. Enumerates capabilities, maps to existing (`creek.ingest`, `creek.classify`) and planned (`creek.handshake`/`reflect`/`wheel`) tools, fixes tier/care/auth/transport semantics, pins ontology version. |
