# `creek save` — answer-filing-back primitive

`creek save` is the verb that turns a good Q&A response into a piece
of vault content rather than letting it vanish into chat history.
FEAT-009 implements ADOPT-003: a wiki compounds when good answers
get filed back in. CrawDad's `/crawdad save` (FEAT-014/015/016)
wraps this primitive over MCP.

## CLI surface

```bash
creek save --target <thread|eddy|praxis|paradox|unnamed|draft> \
           --body <path-or-stdin> \
           --title <optional> \
           --provenance frag-XXX,frag-YYY \
           --source <conversation-id|discord-msg-id|claude-session-id> \
           --source-kind <discord|claude-session|manual|mcp> \
           --tier <open|personal|intimate> \
           [--full-body] \
           [--vault PATH]
```

| Flag              | Meaning                                                                                                                  |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `--target`        | **Required.** Destination type. No auto-classification in v1.                                                            |
| `--body`          | Path to a markdown file, `-` for stdin, or omitted to read stdin.                                                        |
| `--title`         | Optional. Omitted, it falls back to the first non-empty body line **at `--tier open` only** — see "An omitted `--title`" below (#1505). |
| `--provenance`    | Comma-separated fragment IDs (e.g. `frag-001,frag-002`).                                                                 |
| `--source`        | Opaque source identifier — conversation/discord-msg/claude-session.                                                      |
| `--source-kind`   | `discord` / `claude-session` / `manual` (default) / `mcp`.                                                               |
| `--tier`          | **Required.** Privacy tier (`open`/`personal`/`intimate`). Never inferred — see "Tier is always explicit" below.         |
| `--full-body`     | Allow `personal` **and `unclassified`** bodies through unredacted (off by default; they rank together, #876/#961).       |
| `--vault`         | Vault root; falls back to the configured default.                                                                        |

## Destination routing

| `--target` | Lands in                  | Frontmatter shape   |
| ---------- | ------------------------- | ------------------- |
| `thread`   | `02-Threads/Active/`      | Thread model        |
| `eddy`     | `03-Eddies/`              | Eddy model          |
| `praxis`   | `04-Praxis/Situational/`  | Praxis model        |
| `paradox`  | `10-Liminal/Paradoxes/`   | Lightweight note    |
| `unnamed`  | `10-Liminal/Unnamed/`     | Lightweight note    |
| `draft`    | `07-Voice/Drafts/`        | Lightweight note    |

**Paradox routing is unconditional.** A `--target paradox` save
*always* lands in `10-Liminal/Paradoxes/`, no matter what other
flags are passed. **The tier is not.** Paradox honours `--tier`
exactly like every other target: the routing override is the only
override.

> ⚠️ **This changed in #1491.** Paradox saves used to force
> `tier=open`, writing the body into the vault in full even when you
> passed `--tier intimate`, on the reasoning that what is preserved
> is the *fact* of the contradiction. That reasoning holds — but the
> fact does not require the body. The location, title, tags and
> `saved_from` provenance record the contradiction on their own, so
> an `intimate` paradox body is now diverted to the gitignored
> `10-Liminal/Compost/intimate-stubs/` directory (with
> `saved_from.intimate_body_pointer` naming it) and a `personal` body
> is summarised, both exactly as they are for any other target. The
> paradox note itself still lands in `10-Liminal/Paradoxes/`. The
> yellow stderr warning about widening is gone, because nothing is
> widened any more.

## Privacy-tier rules

`creek save` honours the tier system in `docs/security/` and
`creek/classify/privacy_filter.py`:

| Tier           | Vault body                                                              | Off-vault stash                                       |
| -------------- | ----------------------------------------------------------------------- | ----------------------------------------------------- |
| `open`         | Full body                                                               | None                                                  |
| `unclassified` | Title-only summary; full body only when `--full-body` is passed         | None                                                  |
| `personal`     | Title-only summary; full body only when `--full-body` is passed         | None                                                  |
| `intimate`     | Title-only summary, with `intimate_body_pointer` in frontmatter         | Full body to `10-Liminal/Compost/intimate-stubs/`     |

The branch is chosen by the tier's **rank** in
`creek.classify.privacy_filter.tier_sensitivity`, not by its name, so
`unclassified` — which ranks with `personal` (#876/#961), because
untiered content is content nobody has vouched for — is redacted
exactly as `personal` is, and a tier the ranking has never heard of is
handled as `intimate`. Until #1508 the filter compared against the two
names instead, so `unclassified` matched neither and its body was written
into the vault note in the clear: choosing the *less* specific tier
produced *more* exposure, the exact inverse of the one-way ratchet the
save path otherwise enforces (see the ratchet table under Tests below).
Selecting on rank rather than name also means a tier added to
`PrivacyTier` later is redacted by default instead of falling through
to this verbatim write.

`10-Liminal/Compost/intimate-stubs/` is gitignored at the repo level
by the existing whitelist gitignore (`10-Liminal/**` ignores
everything except `.gitkeep`). Intimate bodies therefore never enter
the tracked git history. The stub file itself carries a back-pointer
to the vault note for local recovery.

When you later delete an intimate note with a scoped `creek purge`
(`fragment` / `source` / `daterange`), the purge engine follows the
note's `saved_from.intimate_body_pointer` and deletes the stub too, so
the full intimate body does not survive a right-to-be-forgotten request
(GAP-012) — but only when the pointer resolves inside
`10-Liminal/Compost/intimate-stubs/`; a pointer aimed anywhere else is
refused and nothing is deleted. Accepted side effect: a hand-authored
stub parked outside that directory survives the purge and must be
removed explicitly. See
[Purge](cleaning-and-purge.md#purge-right-to-be-forgotten).

### An omitted `--title` is not derived from the body above `open`

A title is *not* a redacted surface. It is written into the vault note's
frontmatter in the clear, slugified into the **filename**, and — for
`--target ai-as-user` — built into the fragment `id`, the note's stable
handle that other notes, Dataview queries and retrieval quote back. The
filename is the loudest of the three: a directory listing, the Obsidian
sidebar, `git status`, a backup or sync client, and the `created_path`
field of the hash-chained MCP audit log all show it without ever opening
the note.

So deriving the title from the body's first line is safe only when the
body itself is safe. Since #1505:

| Tier                        | Untitled save is titled                          |
| --------------------------- | ------------------------------------------------ |
| `open`                      | First non-empty body line (unchanged)            |
| `unclassified` / `personal` / `intimate` | `untitled <target> <8-hex content digest>` |

The rank comes from
`creek.classify.privacy_filter.tier_sensitivity`, so `unclassified`
(which ranks with `personal`, #876) is covered too — `--tier
unclassified` parses even though the CLI does not advertise it.

**`--full-body` does not relax this.** It widens the *body*, which one
reader sees on purpose; the filename has the wider audience. One rule
with no exceptions is what keeps the leak from being reintroduced. If
you want a descriptive filename on a private save, pass `--title`
explicitly — an operator-supplied title is still written verbatim at
every tier, because only the operator can say whether it is safe.

The digest disambiguates: two untitled `intimate` thread saves with
different bodies get different filenames, and identical bodies fall
through to the writer's existing collision-retry suffix. It publishes
nothing new — `--target ai-as-user` has appended the same
`sha256(body)[:8]` to every fragment `id` since FEAT-041 §7.

### Tier is always explicit

* `--tier` is **required** on every save. There is no default and no
  automatic inheritance — not from `--provenance`, not from the source
  fragments, regardless of whether provenance is supplied.
* Omitting `--tier` **refuses** the save with a clear error and exit
  code 2, whether or not `--provenance` is present.
* The doctrine that a derived note carries the most-restrictive tier
  of its sources still holds — but it is the **calling agent's** job
  to determine that tier (the most-restrictive tier among the
  contributing fragments) and pass it explicitly as `--tier`. Nothing
  in `creek save` computes it for you.
* This is the regression case FEAT-009 calls out by name: silent
  defaults are how intimate content leaks into vaults. Requiring an
  explicit `--tier` on every call is the fix.

## Provenance frontmatter

Every saved note carries a `saved_from` block:

```yaml
saved_from:
  source_kind: discord | claude-session | manual | mcp
  source_id: <opaque-id>
  contributing_fragments: [frag-XXX, frag-YYY]
  saved_at: 2026-05-06T17:35:00Z
  saved_by: <operator-or-mcp-client>
  intimate_body_pointer: 10-Liminal/Compost/intimate-stubs/<slug>.md  # intimate only
  #   titled   -> <slug>.md, the slugified title
  #   untitled -> intimate-<digest>.md, a base32 digest of the body (#1509)
```

Combined with the per-target model frontmatter (Thread / Eddy /
Praxis), this is enough for the compile, lint, and audit passes to
treat saved notes as first-class vault content.

## Examples

File an answer back as a Thread synthesis page:

```bash
creek save --target thread \
           --body answer.md \
           --title "How creeks compound" \
           --provenance frag-001,frag-002 \
           --source claude-session-xyz \
           --source-kind claude-session \
           --tier open
```

Capture a contradiction without resolving it:

```bash
creek save --target paradox \
           --body contradiction.md \
           --title "Both true at once" \
           --provenance frag-101 \
           --tier open
```

Capture an intimate reflection without leaking it into git:

```bash
echo "Confessional body content." | creek save \
  --target unnamed \
  --title "Private reflection" \
  --provenance frag-200 \
  --tier intimate
```

The vault note contains only `[Tier-redacted summary: Private
reflection]` and an `intimate_body_pointer` field. The full body
lives under `10-Liminal/Compost/intimate-stubs/` and is gitignored.

## Tests

Coverage lives in `tests/test_save.py`:

* Each destination type produces a note in the correct directory.
* `pre_save_filter(body, tier=intimate)` returns title-only and the
  stub-relpath under `10-Liminal/Compost/intimate-stubs/`. A titled save
  uses the slugified title; an **untitled** save is addressed by a base32
  digest of its own body (`intimate-<digest>.md`), so untitled saves do
  not all pile onto one stem. Existing stubs are never renamed, so
  pointers already written into vault notes keep resolving (#1509).
* An 8-row (every `PrivacyTier` × `--full-body`) table declares
  `pre_save_filter`'s whole decision by hand — vault body, stub body and
  stub path per row — and its size is asserted separately, so deleting a
  row fails instead of silently not running (#1508). The two `open` rows
  are the positive control that the cleartext check can fire at all. A
  ninth case passes a tier the ranking has never heard of: it must take
  the *intimate* branch, and it is the only test that can see that
  threshold, since over the four real members `rank >= 2` and
  `tier == INTIMATE` select the same rows.
* `creek save --target paradox` always lands in
  `10-Liminal/Paradoxes/`.
* `creek save --target paradox --tier intimate` writes
  `privacy_tier: intimate` and no cleartext body — asserted at the
  writer seam, through the CLI, and through the `creek.save` MCP
  tool, so the guarantee cannot hold on one transport only (#1491).
* A one-way-ratchet table over every
  (`SaveTarget` × tier × `--full-body`) combination asserts no save
  ever files a note at a tier weaker than the one requested.
* A 64-row (`SaveTarget` × every `PrivacyTier` × `--full-body`) table
  asserts that an untitled save derives its title from the body's first
  line **only** at `open` — checked on all three surfaces the title
  reaches (frontmatter, filename, and the `ai-as-user` fragment `id`) —
  and the sixteen `open` rows are the positive control that the
  derivation still works where it is safe (#1505).
* `creek save` with no `--tier` exits 2, with or without
  `--provenance`.
* `intimate`-tier saves never write the full body anywhere under the
  tracked vault tree (verified by file-system inspection).
* End-to-end thread save round-trip via `CliRunner`.
