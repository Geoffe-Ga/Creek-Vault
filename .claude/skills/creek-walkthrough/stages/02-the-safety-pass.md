# Stage 2 — The safety pass

## The pool

This stage comes before ingestion on purpose. Tell the user, in CrawDad's
voice, why:

> A creek carries whatever falls into it. Your exports — chat history,
> notes, code — almost certainly have things in them that should never
> settle into a permanent vault: a password typed in a hurry, an API key
> pasted into a conversation, a phone number. Before any water flows in,
> we walk the bank and look for those. We find them first, so the creek
> never carries them downstream.

## How to run it

1. Quietly run `creek redact --help` to confirm flags.
2. Ask the user where their source material is — the folder of exports
   they want to bring in. If they don't have anything yet, that's fine;
   offer to continue the walkthrough with a small sample so they
   can see the shape of it, and pick up real ingestion later.
3. Run the **scan only** first — never the destructive apply yet:
   `creek redact --scan --source <their path> --report`. Scan looks and
   reports; it changes nothing.

## What to interpret

When the scan returns, translate the report gently:

- If it found nothing: say so plainly and with a little relief — the bank
  is clear.
- If it found candidates: do **not** alarm them. Explain that these are
  *candidates* — the scanner is deliberately cautious and some matches
  will be false alarms (a long git hash can look like a key). Walk them
  through what was found, in plain terms, a few examples, not a wall.
- Explain the next move: applying redaction replaces the sensitive spans
  with markers like `[REDACTED:api_key]`, rewriting the files in place. The
  original secret is never written into the vault or any log.

## Apply — only with a clear yes, and only after a backup

If there were findings and the user wants to proceed, this is the first
genuinely changing action of the walkthrough — and it is final. `--apply`
rewrites the source files in place; once it runs, the original text is
gone, with no undo. Before you get their yes, make sure they know that and
have a copy of their source material somewhere safe (a copy, a commit,
a backup — anything that isn't the only copy).

Get an explicit yes. Always run `creek redact --apply` with `--dry-run`
first and walk them through what it would change, then run the real apply.
Narrate each step.

## The word for this stage

**Redaction.** Not deletion of a fragment — *masking*: matched spans in
the source files are replaced with markers like `[REDACTED:api_key]`
before anything is written into the vault. `--scan` is completely safe and
changes nothing; `--dry-run` lets them preview an apply before it happens;
and a false alarm is best fixed *before* applying, by adding it to
`false_positive_allowlist` and re-scanning. But once `--apply` runs on a
file, that change is final — there is no queue and no restore. The creek
protects you, but it does not keep a copy of the water it already
carried, so a backup is the user's job, not the tool's.

## Check in

Tell them the bank has been walked. Ask if they'd like to bring the water
in now, or rest.

## If something goes sideways

- **No source material yet** → offer the sample-data path; the walkthrough
  continues, real ingestion waits.
- **Scan flags a huge number of things** → reassure: this usually means
  many false positives in code files; the review queue exists exactly for
  this, and you'll show it. Don't let them feel the vault is unsafe.
