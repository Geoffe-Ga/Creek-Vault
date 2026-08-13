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
| `--title`         | Optional. Falls back to the first non-empty body line.                                                                   |
| `--provenance`    | Comma-separated fragment IDs (e.g. `frag-001,frag-002`).                                                                 |
| `--source`        | Opaque source identifier — conversation/discord-msg/claude-session.                                                      |
| `--source-kind`   | `discord` / `claude-session` / `manual` (default) / `mcp`.                                                               |
| `--tier`          | **Required.** Privacy tier (`open`/`personal`/`intimate`). Never inferred — see "Tier is always explicit" below.         |
| `--full-body`     | Allow personal-tier bodies through unredacted (off by default).                                                          |
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

| Tier       | Vault body                                                              | Off-vault stash                                       |
| ---------- | ----------------------------------------------------------------------- | ----------------------------------------------------- |
| `open`     | Full body                                                               | None                                                  |
| `personal` | Title-only summary; full body only when `--full-body` is passed         | None                                                  |
| `intimate` | Title-only summary, with `intimate_body_pointer` in frontmatter         | Full body to `10-Liminal/Compost/intimate-stubs/`     |

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
  stub-relpath under `10-Liminal/Compost/intimate-stubs/`.
* `creek save --target paradox` always lands in
  `10-Liminal/Paradoxes/`.
* `creek save --target paradox --tier intimate` writes
  `privacy_tier: intimate` and no cleartext body — asserted at the
  writer seam, through the CLI, and through the `creek.save` MCP
  tool, so the guarantee cannot hold on one transport only (#1491).
* A one-way-ratchet table over every
  (`SaveTarget` × tier × `--full-body`) combination asserts no save
  ever files a note at a tier weaker than the one requested.
* `creek save` with no `--tier` exits 2, with or without
  `--provenance`.
* `intimate`-tier saves never write the full body anywhere under the
  tracked vault tree (verified by file-system inspection).
* End-to-end thread save round-trip via `CliRunner`.
