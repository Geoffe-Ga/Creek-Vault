# Cleaning pipeline

The `creek/clean/` package contains the modules that decide *which* content survives ingestion and *what shape* it takes once it does. They run inline during `creek ingest` (and again, against the saved vault, during `creek clean *`). Every module is wired to a `cleaning.*` section in [`configuration.md`](configuration.md) so the same knobs that this doc names also appear in `~/.config/creek/config.yaml`.

This page is the missing manual: what each module does, when it runs, what failure looks like, and which knob to turn when something is off.

For the **CLI hygiene commands** (`creek clean orphans`, `clean stale-reviews`, etc.), see [`cleaning-and-purge.md`](cleaning-and-purge.md). This page is about the *inline* pipeline, not the post-hoc CLI.

---

## Order of operations

```
raw bytes → pre-ingestion filter (per source)
          → ingestor (parse + frontmatter)
          → AuthorshipTagger
          → ContextExtractor
          → FragmentValidator
          → QualityScorer
          → Deduplicator + SemanticDeduplicator
          → privacy classifier  (then: vault writer)
```

A fragment that fails validation lands in the **review queue** rather than getting silently dropped. A fragment that fails quality gets `action=skip` and is recorded but not written. A fragment flagged as a duplicate gets a `dedup` tag and points at the canonical fragment.

---

## Pre-ingestion filters (`creek/clean/filters/`)

Each filter receives raw source data **before** the ingestor turns it into a Fragment. They are the cheapest place to drop noise — every byte filtered here saves work downstream.

| Module | What it does | Config section | Failure mode |
|---|---|---|---|
| [`filters/chatbot.py`](../creek/clean/filters/chatbot.py) (`ChatbotFilter`) | Strips system prompts, tool-call outputs, regeneration loops, abandoned conversations, short human turns from Claude / ChatGPT exports. | `cleaning.chatbot` | If too aggressive, real human turns disappear. Lower `min_human_turn_chars` or disable specific `skip_*` flags. |
| [`filters/discord.py`](../creek/clean/filters/discord.py) (`DiscordFilter`) | Skips bot messages, emoji-only / sticker-only posts, command invocations, link dumps, below-min-length text. Reply chains preserved even when short. | `cleaning.discord` | If your guild's `?cmd` prefix is non-standard, set `command_prefixes` so commands are still recognised. |
| [`filters/google_drive.py`](../creek/clean/filters/google_drive.py) (`GoogleDriveFilter`) | Skips "Copy of …" duplicates, empty docs, multi-author files, oversized exports. | `cleaning.google_drive` | A "Copy of" file you actually want kept needs `prefer_newest=False` or a manual rename. |
| [`filters/markdown.py`](../creek/clean/filters/markdown.py) (`MarkdownFilter`) | Skips empty / frontmatter-only / template-residue markdown files. | `cleaning.markdown` | Lower `min_body_chars` if short notes are being dropped. |

A filter never raises on bad input — it returns a `FilterResult` with `keep=False` and a reason, so the ingest pipeline can record "filtered N files" without crashing.

---

## Inline cleaners (run on every Fragment)

These run after the ingestor produces a Fragment but before it reaches the vault writer. Each is a pure function over `Fragment` so they compose without ordering surprises.

### [`authorship.py`](../creek/clean/authorship.py) — `AuthorshipTagger`

Sets `Fragment.source.author` to one of `self | ai | other | collaborative`. Source-aware:

- **Claude / ChatGPT** — human turns → `self`, assistant turns → `ai`.
- **Discord** — match author against `self_usernames`; mixed authorship → `collaborative`.
- **Markdown / journal** — defaults to `self`.

**No config section** — driven by `interlocutor` and platform metadata. Failure: missing `interlocutor` falls back to `self`; check `creek/clean/authorship.py` rules if attribution looks wrong.

### [`context.py`](../creek/clean/context.py) — `ContextExtractor`

Decides what to do with non-self content. Three configurable modes (set `cleaning.context.mode` or pass programmatically):

| Mode | Behaviour |
|---|---|
| `context_metadata` (default) | Store others' content as `context` entries on the nearest user fragment. Drop the standalone fragment. |
| `low_priority` | Keep as separate fragments tagged `low-priority`; exclude from voice-proxy generation. |
| `skip` | Drop entirely. Only user content survives. |

Universal constraints applied across all modes:

- Non-self fragments are **never** voice-proxy eligible (the field is now a derived property — see `BUG-009`; use `git log --grep='BUG-009'` for the origin commit).
- Non-self fragments are **never** classified `intimate`; an `intimate` tier on a non-self fragment is downgraded to `personal`.

### [`validator.py`](../creek/clean/validator.py) — `FragmentValidator`

Checks required fields, UTF-8 encoding, ISO-8601 timestamps, and a configurable minimum content length. Invalid fragments are routed to the review queue rather than discarded — see `cleaning.validation`.

Failure mode: a fragment with mojibake (e.g. CSV decoded as cp1252 by mistake — see `BUG-010`; use `git log --grep='BUG-010'` for the origin commit) lands in review, where you can re-ingest with the right encoding.

### [`quality.py`](../creek/clean/quality.py) — `QualityScorer`

Computes a 0.0 – 1.0 score from Shannon entropy, stop-word ratio, length, URL-only / emoji-only detection, and alphanumeric presence. Recommends `accept`, `review`, or `skip` (`cleaning.quality.skip_threshold` / `review_threshold`).

Failure mode: legitimate short notes scored `skip`. Lower `min_chars` or `min_words`.

### [`dedup.py`](../creek/clean/dedup.py) — `Deduplicator`

Two-tier dedup:

- **Exact** — same SHA-256 over `(source, timestamp, content)`. The deterministic ID (see `creek/ingest/base.py:generate_fragment_id`) means re-running ingestion is idempotent.
- **Normalised** — match after lowercasing, whitespace strip, punctuation removal.

Cross-run state lives at `<vault>/00-Creek-Meta/dedup-manifest.json`. Drop the file to force a fresh dedup pass.

### [`semantic_dedup.py`](../creek/clean/semantic_dedup.py) — `SemanticDeduplicator`

Cosine similarity over pre-computed embeddings. Two thresholds:

- ≥ `duplicate_threshold` (default 0.95) → flagged as duplicate.
- between `resonance_threshold` (default 0.75) and the duplicate threshold → flagged as a *resonance* (related but distinct).

Runs only after embeddings exist (`creek link --method embeddings` populates them). Failure mode: false positives on boilerplate; raise `duplicate_threshold` and / or extend the per-source filter list.

### [`hygiene.py`](../creek/clean/hygiene.py) — vault scanners

Drives the `creek clean *` CLI commands:

- `OrphanScanner` — fragments with zero links after `cleaning.hygiene.orphan_age_days`.
- `StaleReviewScanner` — review-queue items older than `cleaning.hygiene.stale_review_days`.
- `BrokenLinkScanner` — wiki-links pointing at nonexistent files, surveyed vault-wide except Creek's own report folders; see [`lint.md`](lint.md#check-names) for the withheld set and why.
- `DuplicateScanner` — runs normalised + semantic dedup against the saved vault.
- `HygieneReporter` — aggregate report of vault health.

These run only via the CLI (`creek clean orphans`, …); they are **not** part of the inline ingestion pipeline.

---

## Disabling a step

Most config sections have a `enabled: bool` (or per-rule `skip_*` toggles). To disable a step entirely, set the matching `enabled` flag to `false` and re-run ingestion. Where no `enabled` flag exists, the rules can be neutralised individually (`min_chars: 0`, empty `command_prefixes: []`, etc.). See `creek/config.py` for the authoritative shape.

---

## Common symptoms

| Symptom | Likely culprit | Knob to turn |
|---|---|---|
| 30 % of fragments dropped silently | `QualityScorer` skip threshold too high | `cleaning.quality.skip_threshold` |
| Real human turns missing from chatbot exports | `ChatbotFilter` over-aggressive | `cleaning.chatbot.min_human_turn_chars`, `skip_short_human` |
| Discord short replies missing | `DiscordFilter` short-text rule | `cleaning.discord.min_chars`, `preserve_replies` |
| Mojibake in CSV-derived fragments | `cp1252` fallback fired (`BUG-010`) | Re-ingest with explicit encoding; check the WARNING log line for the offending file |
| Voice-proxy eligibility wrong | Stale `voice_proxy_eligible` field | Pull from a build that includes `BUG-009` — the field is now a derived property |
| Duplicate fragments accepted | Stale `dedup-manifest.json` from earlier run | Delete `<vault>/00-Creek-Meta/dedup-manifest.json` and re-ingest |

---

## See also

- [`configuration.md`](configuration.md) — config schema for every `cleaning.*` knob.
- [`cleaning-and-purge.md`](cleaning-and-purge.md) — CLI hygiene commands (post-hoc, not inline).
- [`ingestion.md`](ingestion.md) — how ingestors plug into the cleaning pipeline.
