# GAP-012 — Right-to-be-forgotten leaves the full intimate body behind on a scoped purge

- **Severity:** High
- **Prod-readiness criterion threatened:** data safety, doc honesty
- **Status:** Open (untracked — no open issue exists)

## Summary

`creek save` of an `intimate`-tier answer writes a **title-only** stub into the
vault and routes the **full body** to a separate file under
`10-Liminal/Compost/intimate-stubs/<slug>.md`, recording the link in the vault
note's `intimate_body_pointer` frontmatter. `creek purge` (the right-to-be-
forgotten subsystem) never follows that pointer, never walks the Compost
intimate-stub directory for deletion, and only *scrubs references* (wiki-links
by title and word-boundary fragment-ID mentions) from `.md` files. A scoped
purge — `creek purge fragment`, `creek purge source`, `creek purge date-range`
— therefore deletes the title-only note while leaving the **full intimate body
intact on disk**. Only `creek purge vault` removes it, because that path wipes
all of `10-Liminal/`.

## Evidence (today's code)

- **Full intimate body is written to a stub file:**
  `creek/classify/privacy_filter.py:262-267` — `pre_save_filter` returns
  `stub_body=body` (the full body) and `stub_relpath=_stub_relpath_for(title)`
  for `INTIMATE`.
  `creek/save/_constants.py:15` — `INTIMATE_STUB_RELPATH = Path("10-Liminal/Compost/intimate-stubs")`.
  `creek/save/writer.py:94-102` — writes the full `stub_body` to that path;
  `creek/save/writer.py:173` — writes `intimate_body_pointer` into the vault
  note's frontmatter.
- **The stub is keyed by title slug, not fragment ID**, so even the reference
  scrub cannot find it by ID:
  `creek/classify/privacy_filter.py:222` `_stub_relpath_for(title)`.
- **Purge never touches it.** A grep across `creek/purge/` for
  `intimate`, `stub`, `Compost`, or `intimate_body_pointer` returns nothing.
  The only `intimate_body_pointer` consumers in the codebase are
  `creek/save/_slug.py:57` and `creek/save/writer.py:173,242` — never the
  purge engine.
- **Scoped purge deletes only the fragment's own file + scrubs references.**
  `docs/cleaning-and-purge.md:73` documents the contract: "wiki-links … are
  removed … and every word-boundary mention of the deleted fragment ID is
  replaced with the `[purged]` placeholder … The walk covers all `.md` files
  …, including … `10-Liminal/`." The walk *scrubs references* inside `.md`
  files; it does not delete a stub whose body is the sensitive content itself
  (the stub body is not a "reference").
- **Vault-wide purge does cover it** (bounding the blast radius):
  `_VAULT_CONTENT_FOLDERS` spans `01-Fragments` … `10-Liminal`, and
  `purge_vault` wipes each, so the intimate-stub directory under `10-Liminal/`
  is removed only on the vault-scope path.
- **The promise that is violated:** top-level `README.md:20` — "`creek purge`
  … scrubs every reference along the way." `docs/save.md:70,143` document that
  the stub exists and is gitignored, but **no doc states the intimate stub
  survives a scoped purge.**

## Why it matters

This tool's explicit headline use case is "trusting Creek with intimate
journal content" (`creek-tools/README.md:137`). Right-to-be-forgotten is the
exact operation a user reaches for to erase that content, and the per-fragment
/ per-source / per-date-range scopes are the natural way to do it. Today those
scopes leave the single most sensitive artifact — the verbatim intimate body —
on disk, with no audit line claiming otherwise and no documentation warning the
user. A user who runs `creek purge source discord-dms` believing the contract
("scrubs every reference") will be left with the full text in
`10-Liminal/Compost/intimate-stubs/`.

## Reproduction

1. `creek init --vault /tmp/v`
2. `creek save` an answer with `--tier intimate` and a title, e.g. "Therapy
   notes". Confirm `10-Liminal/Compost/intimate-stubs/therapy-notes.md` now
   contains the full body, and the vault note carries
   `intimate_body_pointer: 10-Liminal/Compost/intimate-stubs/therapy-notes.md`.
3. `creek purge fragment <id-of-the-vault-note> --yes` (or the `source` /
   `date-range` scope that covers it).
4. **Observe:** the title-only vault note is gone, but
   `10-Liminal/Compost/intimate-stubs/therapy-notes.md` still holds the full
   intimate body. The purge audit entry records `fragments_deleted: 1` and says
   nothing about the orphaned stub.

(No test exercises this: a grep of `tests/test_purge.py` for `intimate`,
`stub`, or `Compost` returns nothing.)

## Acceptance criteria

- A scoped purge that deletes (or matches) a vault note carrying an
  `intimate_body_pointer` also deletes the pointed-to stub file, OR the scrub
  walk explicitly deletes any orphaned `10-Liminal/Compost/intimate-stubs/`
  file whose owning note has been removed.
- The purge audit entry reports the number of intimate stubs removed (analogous
  to `embeddings_removed`), so the compliance log does not under-report.
- A `tests/test_purge.py` case files an intimate stub, purges its owning note by
  fragment/source/date-range, and asserts the stub file no longer exists.
- `docs/cleaning-and-purge.md` and the `creek purge --help` text state how
  intimate stubs are handled — either "removed" (preferred) or, if deferred, an
  explicit "scoped purge does NOT remove intimate stubs; use `creek purge
  vault` or delete them manually" caveat.

## Files affected

`creek/purge/engine.py`, `creek/save/writer.py` (pointer schema),
`creek/classify/privacy_filter.py`, `tests/test_purge.py`,
`docs/cleaning-and-purge.md`, `docs/save.md`, `README.md`.

## Dependencies / blockers

None. Self-contained; the `intimate_body_pointer` linkage already exists in
frontmatter, so the engine has everything it needs to follow it.
