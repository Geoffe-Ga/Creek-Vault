# `creek-tools-mcp` registration guide (FEAT-010)

The `creek-tools-mcp` server exposes the read-only `creek` CLI surface
as MCP tools. Both the developer's Claude Code and the CrawDad Discord
bot (FEAT-013+) consume the same surface, so privacy-tier enforcement
and audit-log writes happen at one boundary.

> **MCP is the *agent* adapter.** For the Adepthood-facing HTTP/JSON
> *application* adapter — `creek-tools-api`, serving `/v1` — see
> [`api.md`](api.md). Both adapters call the same `creek_mcp.tools.*`
> functions and share one bearer-token registry, one tier-ceiling cap
> (`creek_mcp.policy`) and one transport-confidentiality posture
> (`creek_mcp.transport_posture`); they differ only in wire vocabulary.

## Installation

```bash
pip install -e .   # from creek-tools/
```

The entry point is registered by `pyproject.toml` as
`creek-tools-mcp`. The server speaks JSON-RPC over stdio; running it
interactively will appear to hang, which is correct.

## Tools

### Handshake (epic #748)

| Tool              | Purpose                                                                   |
|-------------------|---------------------------------------------------------------------------|
| `creek.handshake` | Negotiate vault presence, contract/ontology versions, tier model, and the live capability list. |

A connecting client (the Adepthood app) calls `creek.handshake` first. It is
read-only and needs no LLM provider, so the negotiation succeeds on any host and
even on a fresh/absent vault. It returns at least `available`,
`contract_version`, `ontology_version`, `tiers` (`open`/`personal`/`intimate`),
and `capabilities` (the names of the tools actually registered), plus the
`tier_model` and `transport`. Versions come from `creek_mcp/contract.py`; the
cross-repo contract is
[`docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md`](../../docs/decisions/2026-06-30-adepthood-creek-mcp-contract.md).

### Read tools (FEAT-010)

| Tool                  | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| `creek.state.read`    | Return the latest `00-Creek-Meta/State/latest.md` content, **refusing** when the artefact's own `privacy_tier` stamp exceeds `ceiling` (#969). |
| `creek.state.render`  | Re-render the audit report (expensive), **excluding** above-ceiling content section by section and stamping what it admitted (#969). |
| `creek.lint`          | Run the unified hygiene lint pass (FEAT-008).              |
| `creek.mine`          | Surface essay seeds from the compiled vault layer.         |
| `creek.draft`         | Draft an essay from a mined idea (requires an LLM).        |
| `creek.redact.scan`   | Regex-scan a path for secrets/PII (FEAT-027); scoped to `00-Creek-Meta/Inbound/` at every ceiling, `intimate`/`all` elsewhere in the vault (#972). |

#### The two `creek.state.*` tools gate in different shapes (#969)

`render` **excludes**; `read` **refuses**. The asymmetry is deliberate and is
the same rule `compile` and `reflect` follow, read one level up. `render` names
no target — it is a corpus walk like `report` / `wheel` / `mine` — so refusing
it would make the audit report unreachable, which is #968's explicit anti-goal.
`read` addresses one atomic cached artefact, and there is nothing to partially
admit: you cannot exclude half a rendered markdown document without
re-rendering it, and re-rendering is what `render` is.

`render`'s gap was **write-side**, which is why it survived earlier
response-level sweeps: its envelope happens to echo `content`, but the durable
evidence is the bytes under `00-Creek-Meta/State`, and three leaks were
reproduced there — an `intimate` fragment's slugified title inside an
**absolute** orphan path, a `10-Liminal/Unnamed` note's file stem, and an eddy
title derived from an above-ceiling member fragment.

What `render` excludes at each ceiling is tabulated in
[`generation.md`](generation.md#what-each-section-does-at-a-tier-ceiling-969).
The four exclusions worth knowing before you call it:

* `10-Liminal/Paradoxes` notes are `type: paradox` and carry no `privacy_tier`
  field at all, so they fail closed and vanish from the Liminal Watch below
  `ceiling=intimate`. Any note missing the key behaves the same way.
* The **lint summary** is a verbatim copy of a Processing-Log artefact and is
  untierable row by row, so it is rendered only at `ceiling=intimate` or
  broader. This closes the caveat `creek_mcp/read_gate.py` recorded under
  `creek.lint` — `creek state` is the surface that serves that artefact back.
* **Suggested questions** are dropped entirely below `ceiling=personal`,
  because the shared tier filter *summarises* a personal fragment as
  `[Personal-tier summary: <title>]` rather than dropping it, so a personal
  title could otherwise ride out inside a mined prompt.
* An **eddy or thread no fragment names** has no tier evidence and is admitted
  only at `ceiling=intimate` or broader.

One axis is **accounted for on the stamp rather than narrowed**, and it is worth
knowing which. The mining corpus behind the suggested questions walks all of
`10-Liminal` except `Synchronicities` — including `Compost`, which the Liminal
Watch never reads — so `render` has no admitted list to narrow it against.
Those notes' tiers are folded into the artefact's stamp instead, which means an
untiered one pushes the stamp to `unclassified` (ranked with `personal`, #961)
and the report is refused at `ceiling=open` — the one ceiling at which the
section does not render anyway. Nothing identifying escapes regardless: the only
liminal field a prompt renders is the fragment's opaque generated id. The
consequence to know is a **posture split on the same file**: an `Unnamed` note
with no `privacy_tier` key needs `ceiling=intimate` to appear in the Liminal
Watch (read fail-closed off raw frontmatter) but only `ceiling=personal` for its
id to reach a prompt (read through the validated model's `unclassified`
default). Tracked as #1079.

`read` compares the artefact's stamped `privacy_tier` — the highest tier the
render admitted — never the `tier_ceiling` the render ran under. Comparing the
latter would refuse an `all`-ceiling render over an all-`open` vault for no
reason. The refusal is the canonical four-key payload with
`GENERIC_ABOVE_CEILING_REASON`: no `content` key at all, and no echo of the
stamped tier. A **missing** report stays `status: "empty"` rather than refused
— there is no content for a ceiling to be above, and refusing there would be a
vault-emptiness oracle in the other direction, as well as making a first run of
CrawDad or `/creek` look like a permissions failure.

#### Upgrading a `latest.md` written before #969

A pre-#969 `latest.md` carries no stamp, and an absent or unreadable
`privacy_tier` fails closed to `intimate`. That is accurate rather than
cautious: every such report was rendered completely unfiltered, i.e. at the
equivalent of `--include-tier all`. So the next `creek.state.read` at the
default `ceiling=open` — CrawDad, `/creek`, `/creek phase`, `/creek wavelength`
— returns `status: "refused"` with the generic reason, which is byte-identical
to an above-ceiling refusal. That identity is the point: a distinguishable
"this report predates the stamp" reason would itself be an oracle for whether
the vault holds above-ceiling content.

Recovery is one command and loses nothing: call `creek.state.render` (which
re-renders and re-stamps at the caller's ceiling), or run `creek state
--include-tier open` from the CLI. The ISO-week archive files are untouched,
and `ceiling=all` admits every stamp — including the unstamped legacy one — so
no report is ever permanently unreachable.

**Cache thrash, stated on the tin.** `latest.md` is a single slot shared across
ceilings, kept single deliberately: per-ceiling filenames would multiply
artefacts in the operator's vault and break `latest.md` as the documented
session-start context. `creek.state.render`'s default ceiling is `open`, so a
**bare MCP render narrows `latest.md` for everyone**, including subsequent CLI
reads. A caller that wants the richer report re-renders at the broader ceiling.

#### `creek.redact.scan` is scoped, not tier-filtered (#972)

The scan is a regex pass over bytes: it opens no front matter and reads no
`privacy_tier` from anything it walks, so it has nothing to rank a fragment
*with*. Its gate (`_refuse_outside_scan_scope`) therefore decides *where*
the tool may look, in two parts. `00-Creek-Meta/Inbound/` — the FEAT-027
staging subtree, where CrawDad stages Discord attachments before
`creek.ingest` runs — is admitted at **every** ceiling, because that is the
one call CrawDad makes, and it runs the safety pass there at the channel's
own configured ceiling (`personal` by default, `open` only where an operator
explicitly mapped that channel — `crawdad/crawdad/bot.py::_channel_tier`),
so admission has to hold at the lowest of them. Every other in-vault target
— `09-Reference/` as much as `01-Fragments/` — is ranked as if it held
`intimate` content, because for all the scan knows it does; only
`ceiling=intimate` or `all` admits it.

That escape hatch doubles as the recovery path. A local stdio caller at
`ceiling=intimate`/`all` can scan any vault path, and a bad path there gets
the precise "resolves outside the vault root" diagnostic rather than the
generic out-of-scope refusal. A **remote** consumer token is capped at
`ceiling=personal` (see "INTIMATE is never reachable remotely" below), so it
can never reach that escape — the whole-vault scan is a local-operator
capability only.

One residual is accepted rather than closed by this fix: **existence
probing within `Inbound/` still works** — a caller can still ask whether a
given staged filename is there — and that is the tool's job, not a leak.
The other residual this fix used to leave open — `RedactionScanner.scan_batch`
following a symlinked child out of the scan root and disclosing its target's
PII types, line numbers, and existence — is closed. `scan_batch`'s one
filesystem walk (`_scannable_candidates`) now resolves the scan root once
and declines any symlinked child whose resolved target lands outside it,
using the same predicate as the shipped SEC-003 write guard; a declined
child is counted on `ScanSummary.files_skipped_symlink` and rendered into
`report_markdown`, but that counter is not yet a typed key on this tool's
`statistics` object — tracked by #1292, deferred so the security fix did not
also carry a contract bump. `rglob` does not descend into symlinked
*directories*, so a link to `01-Fragments/` staged as a directory was never
reachable either way.

One deployment knob rode on top of the fix itself: CrawDad's staging root is
a configurable `staging_subpath` (`AttachmentConfig`, default
`00-Creek-Meta/Inbound`), not a hardcoded constant, so an operator who
pointed it elsewhere got a subtree this scan's hardcoded scope does not
recognise as admitted-at-every-ceiling. #1088 closed that gap from the
CrawDad side: a `staging_subpath` outside `00-Creek-Meta/Inbound/` is now
refused at config-parse time, and a `status="refused"` response is treated
as *un-scanned*, so the batch can never be dispatched to `creek.ingest`.
That CrawDad-side check is lexical — it resolves nothing — so this tool's
`resolve_within_vault` plus its resolved `is_relative_to` remains the
confinement boundary.

### Author tools (FEAT-041)

| Tool           | Inputs                                                                  | Purpose                                                                       |
|----------------|------------------------------------------------------------------------|-------------------------------------------------------------------------------|
| `creek.author` | `query`, `medium?` (default `research`), `max_rounds?`, `dry_run?`      | Author a draft for a query via the Creek Writing Desk.                         |

Mirrors the `creek author` CLI args and returns an `AuthoredDraft` shape:
`medium`, `query`, `body`, `provenance` (a list of provenance entries),
`claims` (each with its `source_fragments`), `verdict` (one of `PASS` /
`REVISE` / `ESCALATE`), and `rounds`. The response was a typed stub in the
#456 skeleton; #460 wired it to the real desk, so it is a live draft today.
All six mediums in `creek.author.SUPPORTED_MEDIUMS` (`research`,
`research-piece`, `essay`, `chat`, `how-to`, `book-report`) are wired — any
other `medium` returns `status: error` naming the wired set.

**A condemned draft is refused, not returned ([#1353](https://github.com/Geoffe-Ga/Creek-Vault/issues/1353)).**
When the Writing Desk's HARD privacy check raises a `privacy_compliance`
finding — the drafted prose reproduces a cited fragment's protected text
verbatim, above the **effective reproduction ceiling**
([#1354](https://github.com/Geoffe-Ga/Creek-Vault/issues/1354)) — the verb
answers `status: "refused"` and **omits `body`, `claims` and `provenance`
entirely**. That effective ceiling has two sources and is the more
restrictive of them: the operator's `author.max_reproduced_tier` in
`creek_config.yaml` (default `open`, the strictest rank) and the medium
contract's `default_privacy_tier`; a contract can only ever narrow the
ceiling, never widen it. The refused response still carries `medium`,
`query`, `verdict`, `rounds`, a fixed `reason`, and `privacy_findings`: the
offending findings' `dimension`, `severity` and `message`, where each message
now names the fragment id, the fragment's own tier, the effective ceiling it
exceeded, and which of the two sources set that ceiling
(`author.max_reproduced_tier`, the medium contract, or no medium contract at
all) — and no corpus text, e.g.:

`Cited fragment 'frag-int' is 'intimate' (above the effective 'open' ceiling, set by author.max_reproduced_tier) yet its protected text appears in the draft.`

The keys are *absent*, never `""` or `null` — a falsy-but-present `body` is
exactly the shape that lets a caller print nothing and believe it published a
draft, whereas a missing key raises at the moment of the refusal. Until #1353
this envelope answered `status: ok` with the verdict, the finding **and** the
protected text still in `body`. The CLI half of the same leak was
[#1310](https://github.com/Geoffe-Ga/Creek-Vault/issues/1310), closed
separately by #1352, so `creek author` and `creek.author` now withhold a
condemned draft on the same trigger.

Three details that are easy to misread:

- **Only the `privacy_compliance` findings are echoed**, which is why the key
  is `privacy_findings` and not `findings`. Two sibling checks interpolate
  corpus text into their own messages — `biographical_grounding` embeds a
  sentence lifted from the drafted body, `attribution_correctness` embeds a
  fragment title — so echoing the whole finding list would undo the scrub.
- **The trigger is the finding dimension, never `verdict == "ESCALATE"`.**
  Escalation is routine on this surface (an empty vault escalates, and any
  unresolved soft finding escalates once the round budget runs out), so
  refusing on the verdict would withhold ordinary drafts.
- **`tier_ceiling_enforced` stays `true`.** It is a claim about the #660
  retrieval filter, which ran and did its job; the breach is against a
  *different* gate — the effective reproduction ceiling described above, the
  more restrictive of `author.max_reproduced_tier` (`open` by default) and the
  medium contract's `default_privacy_tier` (`open` in all six shipped
  templates). This is a different axis from the `privacy_tier_ceiling`
  parameter (documented below, under "Every tool requires a
  `privacy_tier_ceiling` parameter"): `privacy_tier_ceiling` governs what a
  caller is *admitted* to retrieve as evidence, while the effective
  reproduction ceiling governs what the finished draft may *reproduce*
  verbatim. At `ceiling=all` the caller *is* admitted to *read* intimate
  content — that says nothing about whether the drafted prose may reproduce
  it — which is precisely why this leak survived every `ceiling=open` probe.

Returning the text under a second key (`withheld_body` and similar) was
considered and **rejected**: the whole envelope is serialized to the client —
CrawDad feeds it verbatim into an LLM composer — so renaming the key changes
no bytes on the wire and only makes the leak harder to see. A caller that
legitimately needs the text in order to revise should re-run at a ceiling that
excludes the offending fragment, or re-tier the fragment.

### Write tools (FEAT-011)

| Tool                    | Inputs                                                      | Write-side tier rule                                              |
|-------------------------|-------------------------------------------------------------|-------------------------------------------------------------------|
| `creek.save`            | `target`, `body`, `title?`, `tier`, `provenance?`           | Caller's `ceiling` must admit `tier` — intimate via open refused. |
| `creek.ingest`          | `source_type`, `input_path`                                 | Default ingest tier is `personal`; `ceiling=open` is refused.     |
| `creek.classify`        | `method` (`rules`\|`llm`), `force`                          | Rewrites in place; no new tier produced — any ceiling permitted.  |
| `creek.link`            | `method` (`embeddings`\|`temporal`\|`eddies`\|`threads`), `rebuild` | Links existing artefacts in place — any ceiling permitted. The method list is `creek.surface_modes.LINK_METHODS`, the same declaration `creek link` reads; until [#1252](https://github.com/Geoffe-Ga/Creek-Vault/issues/1252) this tool carried a retyped copy that had lost `threads`, putting the whole thread half of #880 out of an MCP caller's reach. The response also carries `largest_cluster_fragments`, `clusters_split` and `oversized_discarded` — the cluster-health counts `creek link` has always printed to its console (`creek.cli._format_cluster_stats`), while the MCP caller was told only `link_count` and so read a `status: ok` degenerate pass as a clean one ([#1372](https://github.com/Geoffe-Ga/Creek-Vault/issues/1372)). `oversized_discarded` in particular is data loss — those fragments carry no wiki-link at all. All three are plain ints on `LinkSummary`, a frozen dataclass of counts; none can ever name a fragment, so unlike the ingest advisories below they need no ceiling gate. |
| `creek.report`          | `report_type` (all eleven of `creek.surface_modes.REPORT_TYPES`), `period` (`wavelength` only) | The accepted types are the CLI's, derived not retyped ([#1253](https://github.com/Geoffe-Ga/Creek-Vault/issues/1253)); this tool used to advertise six of eleven, so `unnamed`, `fingerprint`, `paradox`, `synchronicity` and `wavelength` read to a caller as though they were not report types at all. Reachable is not the same as served: the four generators behind `unnamed`, `fingerprint`, `paradox` and `synchronicity` accept no `PrivacyTierOverride`, so they are served **only at `ceiling=all`** — the ceiling `creek report` itself runs them under — and refused below it by name, naming the generator to widen. `wavelength` takes a `period` (`weekly`\|`monthly`\|`YYYY-Www`\|`YYYY-MM`) and shares `creek.generate.wavelength.generate_phase_map` with the CLI. Ceiling is enforced on the *inputs* of every filterable generator (#968): a note enters the artifact only if its raw front matter is within `ceiling`, and a missing `privacy_tier` fails closed to `intimate`. Consequence for `tags`: the Tag Garden scans five directories, and the four beyond `01-Fragments` hold note types with no `privacy_tier` field at all, so **a ceiling-filtered tag garden is fragment-derived only**. `tag-history.json` entries record the `tier_ceiling` they were taken under, and growth is only ever compared between entries at the same ceiling. |
| `creek.skills.refresh`  | none beyond `ceiling`                                       | Voice-skill tree regen. The ceiling is converted with `to_privacy_override` and threaded into `SkillTreeGenerator` as a **hard rank cutoff** (#971): an above-ceiling fragment is **omitted** from exemplar harvesting, never summarised, because a `[Personal-tier summary: <title>]` stub written into `## Exemplar Passages` would be a fabricated voice exemplar carrying the very title it claims to protect. **Thread and eddy skills are gated too, on a derived tier** ([#1284](https://github.com/Geoffe-Ga/Creek-Vault/issues/1284)): `Thread` and `Eddy` carry no `privacy_tier` field, but their titles, descriptions and member lists are computed from their member fragments, so each is ranked at the maximum tier of every fragment whose `threads`/`eddies` wikilinks name its title — equivalently, a thread or eddy skill is written only when **every fragment naming it** clears the ceiling. The reduction runs over the *unfiltered* corpus (an eddy with one `open` and one `intimate` member is `intimate`), empty evidence reduces to `intimate` (so an orphaned thread left behind by a partial `creek link` needs `ceiling=intimate`), `skill_count` no longer moves with above-ceiling thread/eddy cardinality, and an admitted eddy's rendered "Member threads" line lists only threads that were themselves admitted. That last detail matters on the response, not just on disk: a thread's slugified title *is* its filename, so those `skill_paths` entries were the one place this tool's envelope carried content. For the four fragment-derived categories it returns only a count and fixed skill names, so their evidence is the bytes under the untiered `<vault>/creek-skills`. `unclassified` ranks with `personal` (#876), so a vault that has never been through `creek classify` yields a complete tree whose `## Exemplar Passages` sections all carry the "no qualifying exemplars" placeholder at `ceiling=open`; a broader ceiling recovers them. `intimate` needs more than a ceiling: it additionally requires the Python-API `allow_intimate` consent opt-in, which this tool never passes, so intimate exemplars are unreachable over MCP at **every** ceiling, `all` included. |
| `creek.compile`         | `fragment_ids`, `target_kind`, `target_id`, `target_title`  | Gates the *source* fragments' tier, not the compiled page's tier — a source above `ceiling` refuses the whole call (#848). Idempotent per FEAT-003; no-op re-runs do not log a duplicate. |
| `creek.journal`         | `content`, `external_id`, `timestamp?`, `tier?`             | Stages an Adepthood entry then ledger-ingests it (#754); `external_id` is the idempotency key and `tier` defaults to `open`. Two gates, in this order. (1) The *incoming* entry's tier is honored — a ceiling that would not admit it is refused, never downgraded. (2) An `external_id` that already maps to a fragment is an **update-in-place**, so the tier of the fragment it would destroy is ranked too: **you may only overwrite what you could have read** (#970). That second gate reads the fragment's *current* vault tier (not the caller's declared tier, not the staged copy's stamp) and refuses through the shared `read_gate.refuse_above_ceiling`, so the refusal names neither the fragment nor its tier. It sits **above** staging, so a refusal leaves the staged entry under `00-Creek-Meta/adepthood/journal/` untouched as well. Fail-closed rules: an `external_id` with no ledger record at all is a plain **creation** and still works at every ceiling; an `external_id` whose ledger record points at a fragment that no longer resolves ranks as `intimate` and is refused below `ceiling=intimate` — recovery is one re-send at a broader ceiling from a **local stdio** caller only (remote consumers cannot; see "INTIMATE is never reachable remotely" and the consequence spelled out under "Read-side posture" below), not a ledger hand-edit. The success response also carries `warnings` — the ingest run's content-free advisory channel ([#1372](https://github.com/Geoffe-Ga/Creek-Vault/issues/1372)). An advisory that crosses this boundary is content-free **by construction at the producer**: `run_ingest`'s internal `warn` closure requires every advisory it records to state a `ceiling_safe` rendering or `None`, and this tool's response is built only from `IngestRunResult.ceiling_safe_warnings` — the operator-facing `warnings`, which may name real vault fragment ids, is never echoed over MCP. `warnings` is an empty list, never omitted, when the run produced no advisory. |
| `creek.upload`          | `filename`, `content_base64`, `external_id`, `timestamp?`, `tier?` | Stages one Adepthood **document**'s base64 bytes then ledger-ingests them (#1023) — the `creek.journal` shape for things that are not text. The bytes are written verbatim, **flat**, under `00-Creek-Meta/adepthood/uploads/` at `safe_stem(external_id)` plus the sanitised suffix of `filename`; only that suffix is trusted, and only to pick an ingestor via `route_to_ingestor` — there is deliberately **no** `source_type` override, because the directory-only ingestors would discover nothing from a single file and report it as a silent success. Three gates, in this order. (1) The *incoming* `tier` (default `open`) is checked by `write_tier_allowed` **before a single byte is decoded**, so an `intimate` upload at `ceiling=open` never materialises in memory. (2) A `MAX_UPLOAD_BYTES` (10 MiB) cap — encoded length first, then decoded length — before anything is written. (3) The same **you may only overwrite what you could have read** gate `creek.journal` uses (#970), on the *current vault tier* of the fragment the `external_id` already resolves to, refused through `read_gate.refuse_above_ceiling` so the refusal names neither fragment nor tier. All three sit **above** staging, so a refusal leaves previously staged bytes untouched. The declared tier reaches a *binary* fragment only through `run_ingest(privacy_tier=...)` — no ingestor emits `privacy_tier` and a `.docx` has no frontmatter to carry one — and that channel is escalate-only and effectively **create-branch-only**: an update preserves the on-disk frontmatter, so a re-upload at a higher tier does not rewrite the persisted one. Ingest runs against the upload's own `00-Creek-Meta/State/ingest/upload.jsonl` ledger, which is what stamps `source.origin_key` on the fragment — and that key is what puts the staged bytes inside the RTBF purge sweep (see [Cleaning and purge](./cleaning-and-purge.md)); without it the sweep would silently no-op. An identical re-send is a true no-op (the staged bytes are compared and left alone, preserving mtime, which the fragment id is derived from). A file that yields no fragment — an unmapped binary extension, a `.json` — is an honest **refusal**, never `status: ok` with `written: 0`. **Staged upload bytes are NOT redacted before ingest**: the ingest pipeline offers no redaction hook and `creek redact --apply` would corrupt a `.docx`/`.xlsx` ZIP container, so the plaintext sits under the staging dir until purged — tracked in [#1228](https://github.com/Geoffe-Ga/Creek-Vault/issues/1228). The success response also carries `warnings` — the ingest run's content-free advisory channel, sourced from `IngestRunResult.ceiling_safe_warnings` and never from the operator-facing `warnings`, which may name real vault fragment ids and is never echoed over MCP ([#1372](https://github.com/Geoffe-Ga/Creek-Vault/issues/1372)). This is the surface that most needs it: the pre-#1305 collapsed-unit advisory reports actual data loss — a fragment written before #1305, when every sheet of a multi-sheet workbook shared one id and only the first reached disk — and it fires precisely on the multi-sheet-workbook path, which is this tool's path. |

### Purge tools (FEAT-012, elevated authorization required)

| Tool                            | Inputs                                              | Authorization                                              |
|---------------------------------|-----------------------------------------------------|------------------------------------------------------------|
| `creek.purge.fragment`          | `fragment_id`, `auth_token`, `dry_run?`             | `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.source`            | `source_type`, `auth_token`, `dry_run?`             | `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.classifications`   | `auth_token`, `dry_run?`                            | `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.daterange`         | `start`, `end` (ISO dates), `auth_token`, `dry_run?`| `auth_token` must match `CREEK_MCP_ELEVATED_TOKEN`         |
| `creek.purge.vault`             | `confirm_vault_path`, `auth_token`, `dry_run?`      | BOTH the token AND `confirm_vault_path` matching the vault |

Purge tools deliberately do not accept a `privacy_tier_ceiling`
parameter: they do not return vault content, so the ceiling
invariant from FEAT-010 does not apply. Authorization is the only
gate, and refusals are themselves audited so a hostile client
cannot probe the gate silently.

Every tool requires a `privacy_tier_ceiling` parameter
(`open` | `personal` | `intimate` | `all`); default is `open`. Note
that `open` is the *most restrictive* setting — it restricts the
caller to open-tier (publishable) content only, not "open access".
The ladder goes `open` (publishable) → `personal` (summarised
personal allowed, and untiered content — see below) → `intimate`
(everything self-authored) → `all` (every tier). Content above the
ceiling is omitted or returned as a title-only stub — the ceiling
cannot be bypassed by the caller.

Across the whole MCP surface, an `unclassified` (untiered) fragment
ranks with `personal`, not with `open` (#876, extended to the MCP
ceiling by #961): it is content nobody has vouched for, so it needs
an explicit `personal` ceiling to be admitted. Run `creek classify`
(or `creek process`) so every fragment carries a deliberate tier and
the distinction stops mattering — **on a vault that has been
ingested but not yet classified, every fragment carries an explicit
`privacy_tier: unclassified`, so an `open`-ceiling MCP consumer will
read nothing until `creek classify` runs.**

### Write-side tier-ceiling rule (FEAT-011)

A write tool that would *create* content at tier `T` requires the
caller's `privacy_tier_ceiling` to admit `T`. So `creek.save` with
`ceiling=open` and `tier=intimate` is refused with `status="refused"`
rather than silently downgraded — the body never lands in the vault
and the audit entry records the refusal without the body. The same
gate applies to `creek.ingest` because the default ingestor tier is
`personal`.

`creek.compile` (#848) gates on the opposite side: the tier that
matters is the classified tier of the *source* fragments being rolled
up, not the tier of the compiled page being created. If any requested
source fragment's tier exceeds `ceiling`, the whole call is refused
before the compile LLM is invoked or the page is written — so an
intimate fragment can never be laundered into an open-tier compiled
page. The refusal is audited but names no fragment ids and no tiers.

### Read-side posture (#932)

Every registered tool's relationship to the caller's ceiling is now a
machine-checked audit record: `creek_mcp/read_gate.py`'s `TOOL_POSTURES`
names one of six postures per tool, verified against the live server
surface and, for `GATED` entries, against a real call site in the
named module. Those claims are then checked by *running* the tools —
not with one canary probe, but with **one probe set per egress
channel**, because a tool has more than one way to put content in front
of you and closing one channel says nothing about the others:

| Egress channel | What is checked | Which tools | Forced? |
|----------------|-----------------|-------------|---------|
| **JSON response** | The tool is called at `ceiling=open` against a vault holding an `intimate` fragment whose title, body and tags each carry a unique sentinel; that sentinel must appear nowhere in the serialised response. | Ten of the twelve `GATED` tools. `creek.draft` and `creek.author` carry recorded exemptions instead — a bare fixture does not drive either far enough for its envelope to mean anything. | Yes. A newly `GATED` tool must grow a probe or record a justified exemption; it cannot inherit silence. |
| **Model prompt** | The tool is driven all the way *to* the provider with a recording client, and neither the `intimate` sentinel nor the above-ceiling **`personal`** one may appear in any prompt it sent — nor in the response of that same call ([#1036](https://github.com/Geoffe-Ga/Creek-Vault/issues/1036)). | `creek.reflect`, `creek.compile` and `creek.author`. `creek.draft` is exempt, and its stated reason is executed rather than trusted. | Yes. The set is *derived* from the tools' own signatures — a `GATED` tool taking both an LLM factory and a ceiling — so a tool cannot become model-backed without being asked for a prompt probe. |
| **Disk artifacts** | The bytes the call writes into the vault carry no sentinel, and a refused call leaves the bytes it would have overwritten untouched. | `creek.report`, `creek.state.render`, `creek.journal`, `creek.upload`. | **No.** Four per-tool tests and no forcing function: nothing obliges the fifth artifact-writing tool to grow a fifth test. Tracked in [#1273](https://github.com/Geoffe-Ga/Creek-Vault/issues/1273). |

The `warnings` field #1372 added to `creek.journal` and `creek.upload` needs no JSON-response probe of its own: it carries only `IngestRunResult.ceiling_safe_warnings`, proven content-free at the producer rather than filtered at this response boundary — see the per-tool doctrine under "Write tools" above.

The prompt channel is the one you cannot check from the outside. A
response you can inspect; a prompt has already crossed to the provider
by the time the response exists, so a tool could hand you a perfectly
clean envelope having shipped an above-ceiling fragment to a cloud
endpoint on the way to building it. Note which of that row's two
sentinels does the work, too: the `personal` one. Every path in this
codebase drops `intimate`, but the privacy filter *summarises* a
personal fragment to
`[Personal-tier summary: <title>]` — so on that path an above-ceiling
**title** reaches the model by design, and it is exactly what a weaker
"no intimate content in the prompt" check would never see.

Most of what the sweep found is **write-side**: a tool *acts on* content
above the caller's ceiling (#970) rather than handing it
back. That is a shape the ceiling language elsewhere on this page does
not describe — it is written entirely in read-back terms ("omitted",
"returned as a title-only stub"), which is why those gaps survived.

`creek.report` was the third of those and is now closed (#968): the ceiling
is converted with `to_privacy_override` and threaded into all six
generators, which filter their *inputs* through
`creek.classify.privacy_filter.within_ceiling`. Keep the write-side framing
in mind when reading that fix — `report_tool` returns only `report_paths`,
so its response envelope was clean at `ceiling=open` for the whole life of
the bug and no response-level test could have caught it. The evidence was
always the bytes of the artifact the call wrote.

`creek.state.render` was the fourth, and it is the same shape (#969) — its
envelope *does* echo `content` today, but that is a response shape rather than
a guarantee, so the evidence for it is likewise the bytes under
`00-Creek-Meta/State`. `creek.state.read` is the counterpart on the read side
and the **first production adopter** of `read_gate.refuse_above_ceiling`; both
are documented above under "Read tools".

`creek.journal` was the sharpest of the write-side gaps, the last to close
(#970), and the **second adopter** of `refuse_above_ceiling` — the first that
is a *write* gate. It did not merely act on above-ceiling content: its
idempotent update-in-place *destroyed* it, replacing the body of the fragment
an `external_id` already mapped to without ever ranking that fragment's tier.
No read-back wording covered that, and its response envelope was clean either
way, which is why the gap survived a response-level sweep. The rule it now
enforces is **you may only overwrite what you could have read**, which is a
read question — hence the read primitive, and hence `tier_allowed` rather than
`write_tier_allowed` (that one still gates the incoming entry's own tier,
above it). Because remote consumers are capped at `personal` (see "INTIMATE is
never reachable remotely" below), the crisp consequence is this: **a remote
consumer token can no longer destroy an `intimate` journal fragment.** Before
the fix it could — `creek.journal` is a write tool a consumer token reaches,
and overwriting an entry required only the caller's own, lower ceiling, while
the equivalent destruction through `creek.purge.*` demands
`CREEK_MCP_ELEVATED_TOKEN`.

**Consequence to know: that same cap means a remote consumer can never touch
an `intimate` journal `external_id` again, period** — not to edit it, and not
even to re-send it unchanged. `creek classify`'s privacy pass is
escalate-only, so once an entry reaches `intimate` the overwrite gate refuses
every call below `ceiling=intimate`, and the gate sits above the
`record.content_hash != new_hash` comparison in `write_fragment_idempotent`,
so an unchanged idempotent re-sync is refused identically to a genuine edit.
Adepthood, the primary journal producer, is itself a remote consumer, so this
is the single operational consequence of #970 most likely to bite in
production — the earlier "recovery is one re-send" language elsewhere on this
page holds only for a **local stdio** caller. #1082 tracks a possible
content-hash carve-out that would let an unchanged re-send through below
`ceiling=intimate`; that is a design decision for a later architecture pass,
not implemented here.

**Recovering an already-clobbered vault.** The fix is *preventive only*. A
body overwritten by the pre-#970 behaviour survives nowhere in the vault:
provenance records hashes and paths, not content. In order of preference:

1. **Re-send at `ceiling=intimate`/`all`, from a local stdio caller.**
   Adepthood is the system of record for the original content, but it cannot
   perform this step itself — it is a remote consumer, and remote requests
   are capped at `ceiling=personal` (see "INTIMATE is never reachable
   remotely" below), so it can never request `ceiling=intimate`/`all`. An
   operator with local stdio access re-sends the original entry with the
   same `external_id` at that ceiling. The body is restored, and the
   fragment's persisted `intimate` tier is preserved by the escalate-only
   privacy merge — re-sending never downgrades a tier.
2. **Vault-level backup** — Obsidian's file recovery, `git`, or Time Machine
   on the vault directory.
3. **Scope the damage** from `00-Creek-Meta/audit/mcp.jsonl`: look for
   `creek.journal` entries whose `args_summary.external_id` matches and whose
   `tier_ceiling` is below the affected fragment's tier. The refusal audit
   records only the caller's own arguments, so the trail shows attempts and
   ceilings, never which fragment was behind an id.

There is deliberately **no pre-image backup** taken before an update. It would
mint a second plaintext copy of intimate content — a new leak surface — to
guard against a write this gate now refuses.

One read-side leak was found, and it is worth stating how: the sweep
first concluded there were none, having probed the tools that walk the
corpus themselves. `creek.redact.scan` was set aside as "the caller
named the path" — true, and not sufficient. Its path confinement was to
the whole vault rather than to the FEAT-027 staging subtree, and it
returned matching *filenames*, which are slugified fragment titles — the
filename was the content — plus which PII types and line numbers each one
carried (#972). A posture whose name is accurate can still license a wrong
conclusion; that is what the machine-checked manifest is for.

The fix taught a second lesson on top of the first: narrowing *where* a
tool may look only closes the caller-visible half of a leak like this one.
`00-Creek-Meta/Inbound/` is now admitted at every ceiling and every other
vault path is ranked as intimate, because the scan reads no per-file tier
— but scoping alone would not have stopped a symlink staged under
`Inbound/` from disclosing its target's slugified title from *inside* the
admitted subtree. Closing that took a separate look at *how* paths are
rendered: every finding and the markdown summary CrawDad posts to Discord
now go through one renderer that names a path **as scanned**, never as
resolved — because `RedactionScanner.scan_batch` yields symlinked children
unresolved, and a renderer that resolved first reported such a symlink
under its target's name, out of a scan the scope fix alone would still
have admitted. See "`creek.redact.scan` is scoped, not tier-filtered" under
Read tools above for the full shape of the fix, including the one residual
it still accepts and the one #1087 has since closed.

### Elevated-authorization model (FEAT-012)

The `creek.purge.*` family is gated by a separate token from the
tier-ceiling system. The server reads `CREEK_MCP_ELEVATED_TOKEN`
from its environment at startup; callers present a matching string
via the `auth_token` parameter on each purge tool. Comparison runs
through `hmac.compare_digest` (constant-time), not `==`, so a hostile
client cannot probe the token byte-by-byte through timing.

Operational rules:

- **CrawDad does not get the token.** The Discord bot's MCP client
  (FEAT-013+) is launched without `CREEK_MCP_ELEVATED_TOKEN`, and its
  MCP requests omit `auth_token`. Every purge call from CrawDad
  therefore returns `status="refused"` — there is no Discord command
  surface that could accidentally destroy vault content. CrawDad is
  also the canonical transport-authorised, elevated-denied consumer,
  which makes it exactly the party the attempt-rate bound below
  (#914) is aimed at: its refusals spend from the same process-global
  budget as everyone else's.
- **The developer's Claude Code can be configured with the token.**
  Generate one with high entropy — this is the same recipe the startup
  check prints if the configured token is too weak:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  Add `"CREEK_MCP_ELEVATED_TOKEN": "<generated-token>"` to the `env`
  block of `.mcp.json` (alongside `CREEK_MCP_CONSUMER`). Treat the
  token like any other vault secret — keep it out of public dotfiles
  and shared shells.
- **The token must be at least 32 characters (#907).** A configured
  value below the floor aborts server startup on *both* transports with
  the rotation recipe on stderr (the token value itself is never
  printed), and — for embedders that bypass `main` — is denied silently
  by the gate itself. Leaving `CREEK_MCP_ELEVATED_TOKEN` unset remains
  fully supported: that is the "purge disabled" posture, not an error.
- **Breaking change for operators upgrading (#907).** This floor is
  new: a server that starts fine today with a `CREEK_MCP_ELEVATED_TOKEN`
  under 32 characters will refuse to start the next time it is launched
  — on both transports — with no grace period, warning-only mode, or
  opt-out. If you rely on `creek.purge.*`, rotate the token with the
  recipe above *before* upgrading.
- **`creek.purge.vault` requires both the token AND
  `confirm_vault_path`.** The confirmation must match the resolved
  absolute path of the target vault, mirroring the CLI's interactive
  "type the vault path to proceed" prompt. Either guard alone
  refuses the call.
- **Refusals are audited.** A refused purge attempt still appends an
  entry to `mcp.jsonl`, so a token-less probe leaves a trail. The
  `auth_token` value never enters the audit log — only the
  refusal-or-success outcome and the structured args summary.
- **Failed attempts are rate-bounded (#914).** Constant-time
  comparison stops an attacker learning the token byte-by-byte; it
  does nothing to stop them guessing it whole, at machine speed.
  After 5 consecutive denials (`MAX_FAILED_ATTEMPTS`) the gate stops
  evaluating tokens at all for 60 seconds (`LOCKOUT_SECONDS`) —
  during the window even a correct token is refused, and the
  comparison is never performed. Every denial mode counts toward the
  5, identically: unset token, empty token, a sub-32-char server
  token (the #907 floor above), a missing client token, and a wrong
  client token. Counting only mismatches would make "purge
  disabled/misconfigured" behave differently from "wrong token" and
  hand back the configuration oracle #907/#913 closed. The refusal
  itself is deliberately indistinguishable whether or not it was
  throttled — same `status`, same `tool`, same `reason`; there is no
  `throttled` flag, no `retry_after`, no `attempts_remaining` — so
  detect a lockout by the *rate* of refusals in `mcp.jsonl` (see
  above), not by any field on one. The budget is process-global, not
  per-consumer or per-tool: a per-identity budget would multiply an
  attacker's guesses by the number of identities they hold, and a
  per-tool budget would turn the five purge tools into a 25-guess
  gate. Attempts made inside the window do not extend it, so an
  individual lockout always ends 60 seconds after it was armed, and a
  single successful call resets the counter to zero. The lockout
  self-heals — the full allowance returns after 60 seconds with no
  restart, env var, or file to touch, and there is deliberately no
  way to clear a live lockout in production. Three residuals, stated
  honestly. (1) The counter lives in the process, so restarting the
  server also restores the allowance — this *bounds* the brute force
  rather than closing it, and an attacker able to cycle the process
  already has more than guessing power. (2) Five wrong tokens —
  fat-fingered, or sent by one hostile transport-authorised consumer
  such as CrawDad above — lock `creek.purge.*` process-wide for 60
  seconds. (3) **A *sustained* attacker keeps re-arming the lockout,
  and that is the honest cost of a process-global budget.** One guess
  per second for an hour yields the attacker 285 evaluated guesses
  (4.75/min, against a previously unbounded rate) but leaves the gate
  shut to *everyone* for roughly 92% of that hour: each time the
  window expires the attacker spends the fresh allowance in about
  four seconds and arms the next one. So MCP purge is not merely
  delayed for 60 seconds while an attack is running — it is largely
  unavailable for the duration. That is accepted deliberately: the
  alternative, keying the budget per consumer, multiplies the
  attacker's guess rate by the number of identities they hold, and
  rate-limiting the *secret* is the point. The operator's recovery
  path is not to wait it out but to use `creek purge` on the CLI,
  which calls `PurgeEngine` directly and never reaches this gate.
- **`status` has three values, not two (#1246, contract `0.4`).**
  `refused` means the gate said no and nothing was touched. `ok` means
  the erasure is complete. `partial` means the operation ran to the end
  and something it promised to erase is **still on disk** — read
  `voice_body_undecodable` for the fragment ids, and re-run
  `creek report --type voice` to regenerate `07-Voice/`. Treat anything
  that is not `ok` as work still owed; a client that only knows
  `ok`/`refused` will fall through its branches rather than read an
  incomplete erasure as a clean one.
- **The payload carries every `PurgeResult` field.** It is derived from
  the model rather than hand-picked, so a counter added to the engine
  reaches you without a second change here. That includes the ones a
  hand-maintained list had dropped: `embeddings_removed`,
  `provenance_scrubbed`, `intimate_stubs_removed`,
  `journal_staged_removed`, `voice_artifacts_removed` and
  `voice_body_undecodable`.

Example `.mcp.json` for a Claude Code instance configured for
destructive ops (replace the token with a freshly generated one — the
sample shown here is high-entropy from `secrets.token_urlsafe(32)` and
must not be reused):

```json
{
  "mcpServers": {
    "creek-tools": {
      "command": "creek-tools-mcp",
      "env": {
        "CREEK_MCP_CONSUMER": "claude-code",
        "CREEK_MCP_ELEVATED_TOKEN": "REPLACE_WITH_secrets.token_urlsafe(32)"
      }
    }
  }
}
```

> Do **not** copy this `env` block into the CrawDad host config. The
> token is deliberately withheld from CrawDad so a Discord-side
> intent can never escalate into a vault deletion. If you need to
> rotate the token, rotate it on the developer's Claude Code only.

## Claude Code

Add an entry to your project's `.mcp.json` (or to user-level
`~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "creek-tools": {
      "command": "creek-tools-mcp",
      "env": {"CREEK_MCP_CONSUMER": "claude-code"}
    }
  }
}
```

`CREEK_MCP_CONSUMER` is recorded on every audit-log entry so we can
tell apart calls from Claude Code, CrawDad, or operator-driven runs.

## Claude Desktop / Cursor / Zed

All three read the same `mcp.json` schema; the registration entry
above works verbatim. Drop it in the host's MCP config location.

## CrawDad

CrawDad (FEAT-013+) treats this server's tool registry as its
`intents` schema (FEAT-014). The Discord bot spawns the server as a
stdio child process. Set `CREEK_MCP_CONSUMER=crawdad` in the bot's
environment so the audit trail distinguishes Discord-driven calls.

## Network transport (authenticated, epic #757 / #759 / #837)

Local consumers (Claude Code, CrawDad) speak JSON-RPC over **stdio** — the
default, unchanged. To reach a user's per-user-VM vault from a remote
Adepthood backend, the server also serves an **authenticated
streamable-http** transport:

```bash
# Generate each consumer token as a high-entropy secret, once, per consumer:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
export CREEK_MCP_CONSUMER_TOKENS="adepthood=<token>;other=<token>"
creek-tools-mcp --transport network --host 127.0.0.1 --port 8000
```

A consumer mid-rotation lists more than one currently-valid token,
comma-separated, inside its own entry — `adepthood=<current>,<incoming>` — see
"Rotating a consumer's secret" below. A single-token entry, as above, is the
steady state and parses exactly as it did before #895.

### TLS is enforced for non-loopback binds (#837)

`--host` defaults to `127.0.0.1`. A **loopback** bind — `127.0.0.0/8`,
`::1`, or the literal hostname `localhost` (case-insensitive) — may still
serve plain HTTP, for local dev; anything else is refused unless
`--tls-cert`/`--tls-key` are both supplied and point at existing files:

```bash
creek-tools-mcp --transport network --host 0.0.0.0 --port 8443 \
  --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem
```

With both flags set, the server serves the Starlette app directly under
`uvicorn` with `ssl_certfile`/`ssl_keyfile` — no reverse proxy is required.
Alternatively, keep the server bound to `127.0.0.1` (or `localhost`) and
terminate TLS in a reverse proxy in front of it; either path keeps bearer
tokens off the wire in cleartext. A non-loopback bind without TLS exits
immediately with a nonzero status and an error on stderr, before any socket
opens — no partial startup. Note that only an IP literal or the exact
hostname `localhost` is recognised as loopback; any other hostname (even one
that happens to resolve to `127.0.0.1`, e.g. via `/etc/hosts`) is treated as
routable by design and requires TLS.

- **No anonymous access.** Network mode refuses to start unless
  `CREEK_MCP_CONSUMER_TOKENS` is set. It holds `consumer=token` entries,
  `;`-separated; tokens live in the **environment only** (never in code or
  config), mirroring the `CREEK_MCP_ELEVATED_TOKEN` precedent. Generate each
  token high-entropy — `secrets.token_urlsafe(32)` — so a token is never
  guessable; the constant-time comparison only matters against a strong secret.
  A configured token below 32 characters is refused at startup, the same
  floor `CREEK_MCP_ELEVATED_TOKEN` now enforces (#907) — both surfaces share
  one minimum defined once in `creek_mcp/token_policy.py`.
- **A consumer may hold more than one currently-valid token (#895).** A
  `,`-separated list inside one consumer's entry — `adepthood=<old>,<new>` —
  names an *ordered set* of tokens that all currently authenticate as that
  consumer. A single token, as in the example above, is the unrotated steady
  state and parses exactly as it did before this change. The 32-character
  floor applies to every token in the set, not just the first, because a
  rotation is exactly when a fresh secret gets typed in. Two configurations
  are refused at load time rather than silently resolved: naming the same
  consumer twice (`adepthood=a;adepthood=b` — the second entry no longer
  silently overwrites the first) and configuring the same token value more
  than once, whether inside one consumer's set or across two consumers —
  every token value must be **globally unique**, because a shared value would
  be attributed to whichever consumer the verifier happens to scan last,
  auditing the call under the wrong identity.
- **Per-consumer identity.** Each request must present its bearer token
  (`Authorization: Bearer <token>`). The token maps to a consumer name that
  is stamped on every audit-log entry — so remote calls are attributable the
  same way `CREEK_MCP_CONSUMER` attributes stdio calls. A missing or unknown
  token is rejected `401` before any tool runs; comparison is constant-time.
- **Bearer tokens carry a finite lifetime (#837).** Each verified bearer is
  issued an `AccessToken` that expires `CREEK_MCP_TOKEN_TTL_SECONDS` after
  the moment it was verified (default `3600`, i.e. one hour); an unset,
  non-integer, or non-positive TTL value falls back to the default rather
  than issuing a non-expiring token. In practice the SDK's bearer middleware
  re-verifies the `Authorization` header on every request rather than
  caching a session-scoped token, so a consumer that keeps presenting the
  same configured `CREEK_MCP_CONSUMER_TOKENS` secret is re-verified and
  granted a fresh `AccessToken`/`expires_at` on each call; the TTL bounds how
  long any *individually captured* `AccessToken` (e.g. one logged or cached
  outside the server) would remain valid. It does **not** bound the
  configured secret itself and it is not a rotation mechanism — a consumer
  that keeps presenting a value from `CREEK_MCP_CONSUMER_TOKENS` is
  re-verified indefinitely. Dropping a token from the set and restarting is
  what revokes it; see "Rotating a consumer's secret" below, and
  [ADR-0009](architecture/ADR/0009-mcp-consumer-token-rotation.md) for why
  overlapping static tokens, rather than short-lived derived credentials, is
  the shape of that runbook.
- **A consumer token grants remote _write_ access, by design.** A valid
  `CREEK_MCP_CONSUMER_TOKENS` entry can reach every non-purge tool at or below
  the `personal` ceiling — including the **write** tools (`creek.journal`,
  `creek.ingest`, `creek.classify`, `creek.link`, `creek.report`,
  `creek.compile`, `creek.save`, `creek.skills.refresh`), not just reads. This
  is intentional (Adepthood writes journal entries remotely), so treat each
  token as a write-capable credential — a meaningfully larger attack surface
  than "remote read-only." Purge tools stay gated separately by
  `CREEK_MCP_ELEVATED_TOKEN` (a per-consumer bearer alone cannot purge). The
  one *destructive* path a consumer token could reach without the elevated
  token is now closed: `creek.journal`'s update-in-place refuses to overwrite a
  fragment above the caller's ceiling (#970), and remote callers are capped at
  `personal`, so a remote consumer can no longer destroy an `intimate` journal
  fragment.
- **INTIMATE is never reachable remotely.** The cap is
  **adapter-independent policy**, in `creek_mcp/policy.py::admitted_ceiling`
  — not a property of the MCP transport wrapper. "Remote" is a fact each
  adapter asserts about its own transport (`CallerIdentity.is_remote`),
  never something policy infers from the presence of MCP-specific state.
  That distinction is the whole point of the split: before it, "remote"
  meant "this call carries an MCP access token", so an adapter that stands
  up no MCP request context at all — the Adepthood `/v1` HTTP API (see
  [`docs/decisions/2026-07-31-adepthood-http-application-api.md`](../../docs/decisions/2026-07-31-adepthood-http-application-api.md))
  — would have read as local under the old definition and left the cap
  silently unenforced. The behaviour this page describes is unchanged: a
  remote caller is capped at `personal`, a request for a
  `privacy_tier_ceiling` above it (`intimate` / `all`, or any unrecognised
  value) is **refused before dispatch**, so intimate content is never even
  read for a network consumer, and stdio calls are unaffected — the
  per-tool `open` default still applies locally, and `intimate` remains
  reachable for the local owner. `_BoundedFastMCP.call_tool` is still the
  MCP-side chokepoint that enforces this; it now renders a verdict
  `creek_mcp/policy.py` decides rather than deciding it itself. The `/v1`
  surface of epic #1071 is specified to reach the same verdict through the
  same module, answering `422 invalid_request` where MCP returns its own
  refusal reason — the two adapters are to agree on the boundary, not on the
  wording. No `/v1` handler is mounted yet (#1074), so today
  `_BoundedFastMCP.call_tool` is the only caller. The audited consumer
  identity moved the same way, for the same reason:
  `creek_mcp/policy.py::effective_consumer` decides whose name lands in the
  audit log from the same `CallerIdentity`, and `_effective_consumer` in
  `creek_mcp/server.py` is now MCP's thin adapter over it, unchanged in
  behaviour.

### Rotating a consumer's secret (no downtime)

Rotation widens a consumer's token set so the old and new secrets both work,
lets the consumer redeploy onto the new one, then narrows the set back to
one. Two restarts bound the window, and **the window is not closed until the
second restart runs** — until then the retired secret still authenticates as
that consumer, exactly like the new one.

1. Generate a fresh token for the consumer being rotated:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. Add it alongside the consumer's current token in
   `CREEK_MCP_CONSUMER_TOKENS`, comma-separated, current value first:
   ```bash
   export CREEK_MCP_CONSUMER_TOKENS="adepthood=<current>,<new>;other=<token>"
   ```
3. Restart **every** process that reads this variable — `creek-tools-mcp`,
   `creek-tools-api`, or both, if the host runs the two adapters side by side.
   They share one registry, so a process that was not restarted is still
   serving the pre-edit set. Startup prints a
   rotation notice to stderr naming `adepthood` and its token count — that
   notice is the reminder that a window is open, not a one-time line to
   dismiss. From this restart on, both `<current>` and `<new>` authenticate
   as `adepthood`.
4. Redeploy the consumer onto `<new>` and confirm *at the consumer* that it
   is presenting the new token. (The audit log at
   `<vault>/00-Creek-Meta/audit/mcp.jsonl` attributes every call to
   `adepthood` regardless of which token authenticated it, so it cannot tell
   you which one is in use — check the consumer's own configuration.)
5. Remove `<current>` from `CREEK_MCP_CONSUMER_TOKENS`, leaving only `<new>`:
   ```bash
   export CREEK_MCP_CONSUMER_TOKENS="adepthood=<new>;other=<token>"
   ```
6. Restart again — every process you restarted in step 3, not just one of
   them. **This step is not optional.** Until it runs, `<current>` still
   authenticates as `adepthood` on any process still holding the widened set,
   and the rotation notice keeps firing at that process's every startup as a
   reminder. This second restart is the only thing that actually revokes the
   retired secret — nothing else in this system does.

Skipping step 6 does not fail loudly; it leaves a permanently widened
credential set that keeps working indefinitely. The startup notice is the
only pressure against that, so treat it as an open task, not a status line.
See [ADR-0009](architecture/ADR/0009-mcp-consumer-token-rotation.md) for why
this overlapping-token runbook, rather than short-lived derived credentials,
is the shape of rotation today.

The transport is a thin wrapper around the MCP SDK's `TokenVerifier` /
streamable-http app; the tool registry, tier-ceiling rules, and hash-chained
audit are identical to the stdio path.

## Audit log

Every tool invocation appends one entry to
`<vault>/00-Creek-Meta/audit/mcp.jsonl`:

```json
{
  "tool": "creek.mine",
  "args_summary": {"phase": "rising", "limit": 10},
  "tier_ceiling": "open",
  "consumer": "claude-code",
  "timestamp": "2026-05-11T12:34:56+00:00",
  "prev_hash": "..."
}
```

`args_summary` captures the MCP-supplied arguments to the tool (the
ceiling is already a top-level field, so it's not duplicated here).
The vault path is *not* recorded — it's resolved internally from
`load_config()` and never enters the audit trail. Compact summary
rules: long strings become `{"len": N}`, lists become `{"count": N}`,
dicts become `{"keys": [...]}`. A draft request for an
`intimate`-tier fragment never leaks the body into the audit
trail. Hash chaining is provided by `creek.audit.AuditLog`; FEAT-012
adds the per-entry `entry_hash` and a verifier on top — see below.

**The one exception is a handshake against a vault that does not exist**
(#1108). The log lives under `00-Creek-Meta/`, which is also the marker
`creek.handshake` reads to decide `available` — so writing the entry would
create the directory whose absence the entry records, and the *next* handshake
would then report `available: true` for a vault nobody initialised. Rather than
scaffold a vault in order to file the paperwork about its not existing, that one
call is recorded to the server's process log instead, naming the tool, the path
probed, the consumer and the ceiling. Every call against a real vault — every
tool, `creek.handshake` included — is audited exactly as described above.

#### Tamper-evidence (FEAT-012)

Every entry carries two integrity fields:

- `prev_hash` — SHA-256 of the previous line's bytes (chain link).
  Removing or reordering an entry invalidates the chain at the next
  line.
- `entry_hash` — SHA-256 of the entry's payload (excluding
  `entry_hash` and `prev_hash`). Mutating any other field — `tool`,
  `consumer`, `tier_ceiling`, the args summary — invalidates this
  hash on its own, even if the line position survives.

`creek_mcp.audit.verify_mcp_audit_chain(vault_path)` walks both
invariants and raises `MCPAuditChainBrokenError` on the first
mismatch. The walk is cheap (one read pass, one hash per line) and
is safe to call from any operator script. Writes hold an exclusive
`flock` on the log so two processes appending in parallel cannot
interleave half-written lines; concurrent-write safety is exercised
by `tests/test_mcp_audit.py::test_concurrent_process_appends_do_not_corrupt_log`.

#### Write-tool audit fields (FEAT-011)

Write-tool entries add three optional fields on top of the read-tool
schema:

```json
{
  "tool": "creek.save",
  "args_summary": {"target": "thread", "tier": "open", "body": {"len": 4096}},
  "tier_ceiling": "open",
  "consumer": "crawdad",
  "timestamp": "2026-05-12T12:34:56+00:00",
  "created_path": "02-Threads/Active/2026-05-12-saved-thread.md",
  "created_tier": "open",
  "affected_fragment_ids": ["frag-a", "frag-b"]
}
```

`created_path` is the relative path of the produced file (or the
container directory for batch tools like `creek.skills.refresh`);
`created_tier` is the tier the content was written at;
`affected_fragment_ids` is an ID list — never fragment bodies. Tools
that update artefacts **in place** (`creek.classify`, `creek.link`) do
not produce new files, so they omit `created_path` and `created_tier`
from their audit entry.

For tools that accept a `body` argument (`creek.save`), the audit entry
records `body_len` rather than `body` — on both the success and the
refusal path — so a fragment body never lands in `mcp.jsonl` verbatim,
regardless of length.

`creek.compile` skips the audit append on no-op re-runs (idempotent
per FEAT-003) — the engine still runs (the LLM call is not yet
skipped), but the audit log does not grow when a re-compile produces
an identical target page.

## Troubleshooting

- **Server appears to hang:** correct. It speaks JSON-RPC over stdio.
  Use `python -m creek_mcp.server` for the same effect.
- **`No module named "mcp"`:** install with `pip install -e .` — FEAT-010
  added the `mcp` SDK to `pyproject.toml`.
- **`creek.draft` returns "LLM provider unavailable":** the server
  loads the LLM lazily so only `draft` requires it. Configure
  `ANTHROPIC_API_KEY` or a running Ollama instance.
- **`--transport network` exits with "refusing to serve on non-loopback
  host ... without TLS":** bind `127.0.0.1`/`localhost` for local dev, or
  pass `--tls-cert`/`--tls-key` (both, pointing at existing files) for a
  routable bind.
- **Fewer mine seeds than expected:** the ceiling is filtering intimate
  fragments by design. Raise the ceiling to `intimate` or `all` only
  when the caller is authorised.
