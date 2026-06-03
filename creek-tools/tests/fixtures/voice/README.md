# Voice-fidelity regression fixtures

Hand-written, representative snippets used by
`tests/generate/ai_style/test_voice_regression_harness.py` (issue #520) to
prove the `ai_style` scanner separates the owner's voice from AI slop.

**This repo holds no vault content (FEAT-019).** Nothing here is a real
journal export. The `in_voice/` snippets are short, hand-authored,
plain-prose stand-ins for the owner's reflective first-person writing; they
exist only to seed a `VoiceFingerprint` and to act as a held-out
no-false-reject control. The `slop/` snippets are hand-authored caricatures
of the real failure modes that motivated the issue (antithesis saturation,
the journal-meta provenance tell, and Wikipedia-style peacock/significance
padding).

- `in_voice/*.md` — plain, reflective, first-person prose. Must score
  **below** the in-voice distance threshold.
- `slop/*.md` — AI-slop caricatures. Must score **above** the slop distance
  threshold, with a margin over the in-voice band.

Each file is a bare prose snippet (no frontmatter); the harness loads the
raw text.
