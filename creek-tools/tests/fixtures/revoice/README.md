# Revoice regression fixtures

Synthetic stand-ins for the Book Essay Forge pair that `creek.revoice`
wrongly reported as `ok` (a 22 KB analytical essay on *Scattered Minds*).
The real files live outside this repository (FEAT-019: no user content is
committed). These reproduce the failure as reported:

- `source.md`: an analytical essay with headings and citations.
- `failed_after.md`: the failure's shape. The opening is nearly identical,
  citations are stripped, punctuation and spelling are touched, the last
  section is compressed, and one section is rewritten in fluent, generic
  prose. That last change gives the pair a large aggregate diff.
- `owner_voiced.md`: the same claims, headings and citations, rewritten in
  the owner's measured style.
- `owner-samples/`: owner-authored exemplars the profile is measured from.
  It also holds one AI draft (`voice_weight: 0.0`) and one `personal`-tier
  note, which must never ground an `open`-ceiling revoice.

To check the real pair locally:
`python -m creek.author.revoice_check SOURCE AFTER --vault VAULT`.
