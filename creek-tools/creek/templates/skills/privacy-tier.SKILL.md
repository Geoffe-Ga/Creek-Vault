# privacy-tier.SKILL.md

**Concern:** the open / personal / intimate ladder
**Layer:** schema → gates every read, write, and generation flow
**Budget:** ≤1500 tokens
**Loaded by:** Claude Code, CrawDad, the MCP server, every CLI verb that reads or generates from fragments

## The three tiers

Privacy tiers come from spec §13.2. They are the first thing any agent checks before reading a fragment body, generating a derivative, or surfacing a summary.

| Tier | Sources (examples) | Default handling |
|---|---|---|
| **`open`** | Published essays, public Discord messages, openly publishable writing | Full body available to every flow. |
| **`personal`** | Chatbot conversations, private Discord messages, ordinary journaling | Title + summary only by default. Body included only when the operator passes `--include-tier personal` (or higher) for that call, with an audit entry. |
| **`intimate`** | Journal entries on recovery, trauma, sexuality, or anything the human flagged as intimate at ingest | Excluded entirely by default. Included only when the operator passes `--include-tier intimate` (or `all`) *and* explicit consent is on file. Never enters voice-proxy generation without explicit consent. |

Tier names use these spellings, no synonyms: **`open`**, **`personal`**, **`intimate`**. The legacy value `public` was renamed to `open` per INC-003; only `open` is canonical going forward.

## Fail-closed defaults

The system fails closed: when in doubt, restrict.

| Situation | Default tier |
|---|---|
| Source classifier returns ambiguous or low-confidence privacy signal | `personal` |
| Source classifier raises, the file is unreadable, or the tier field is missing | `intimate` |
| Fragment touches an intimate-tier source even briefly during a conversation | `intimate` (the conversation inherits the most-restrictive contributing tier) |
| Liminal note (Paradox / Unnamed / Synchronicity / Compost) | inherits the most-restrictive tier of its contributing fragments |
| Reflection or save generated during a session that touched intimate content | `intimate` until the human explicitly down-tiers, per `save.SKILL.md` |

The rule, in one line: **ambiguous → `personal`; unknown → `intimate`.** Never `open`.

## Per-tier inclusion behaviour for the four generation verbs

The four generation verbs — `creek mine`, `creek draft`, `creek report`, `creek skills` — share a single privacy filter (the `creek.classify.privacy_filter` module). They do not get to drift out of agreement with each other.

| Verb | `open` | `personal` (default) | `personal` (with `--include-tier personal`) | `intimate` (default) | `intimate` (with `--include-tier intimate` + consent) |
|---|---|---|---|---|---|
| `creek mine` (blog-idea seeds) | full body | title + summary | full body | excluded | full body, audit logged |
| `creek draft` (essay generation) | full body | title + summary | full body | excluded | full body, audit logged |
| `creek report` (wavelength / voice / threads reports) | full body in report | title + summary in report | full body in report | counted in metrics only; never quoted | full body, audit logged |
| `creek skills` (voice-skill tree exemplars) | exemplar passages allowed | exemplar passages allowed (title + extracted passage) | full body for exemplar selection | **never used as exemplars**; do not contribute even with `--include-tier intimate` *unless* `allow_intimate=True` plus consent | exemplar passages allowed, audit logged |

Two flow-level rules that override the table:

1. **Voice proxy: always opt-in.** Even with `--include-tier intimate`, intimate fragments contribute to the voice-skill tree only when the human has explicitly opted in (`SkillTreeGenerator(allow_intimate=True)` and consent on file). The tier flag alone is not enough.
2. **Audit on elevation.** Any call that uses `--include-tier personal`, `--include-tier intimate`, or `--include-tier all` writes an entry to `00-Creek-Meta/audit/privacy.jsonl` recording the verb, vault, override, fragment count, and timestamp. Calls without elevation do not write audit entries.

## What the agent must never do

1. **Never write `tier: open` by default.** When the source classifier is silent, write `personal` (or `intimate`, per the fail-closed table).
2. **Never include a personal-tier body in a generation prompt without `--include-tier personal` (or higher).** Title + summary is the default contract.
3. **Never include an intimate-tier fragment in any generation flow without explicit consent on file.** A `--include-tier intimate` flag without consent fails closed.
4. **Never let a Liminal note bypass tier inheritance.** A Paradox between a `personal` and an `intimate` fragment is `intimate`.
5. **Never silently down-tier.** A `creek save` that touched intimate content saves as `intimate` unless the human explicitly down-tiers per `save.SKILL.md`'s consent flag.

## Frontmatter

Every fragment, compiled page, and Liminal note carries:

```yaml
privacy_tier: open | personal | intimate
consent: explicit | inherited      # required when tier is intimate
```

`consent: explicit` requires a human-confirmed flag at the operation that produced the note (via `--consent` or interactive prompt). `consent: inherited` is acceptable only when the originating fragment already carries `consent: explicit`.

## Canonical taxonomy

Tier names verbatim: **`open`**, **`personal`**, **`intimate`**. Wavelength and frequency tags follow the canonical taxonomy (INC-019); see `compile.SKILL.md` and `wavelength-aware.SKILL.md`.

## Reference

- Spec §13.2 (Privacy Tiers); §13.5 (Consent Architecture).
- INC-003 (the `public` → `open` rename).
- SEC-006 (intimate fragments must not leak into mine / draft).
- `creek.classify.privacy_filter` (the canonical implementation).
- `save.SKILL.md` for save-time tier defaults; `wavelength-aware.SKILL.md` for the snapshot's read scope.
