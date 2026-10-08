---
type: author_manifest
author_slug: "example-author"
display_name: "Example Author"
author_kind: human_source # human_source | ai_as_user | collaborator
voice_weight: 0.0
representativeness: reference # self | endorsed | aspirational | reference
default_privacy_tier: open
attribution_required: true
notes: "Captured for ideas, not voice."
---

# Example Author

This is the **author manifest template**. Copy this file into a new author
folder (e.g. `11-Other-Authors/montaigne/_author.md`) and edit the frontmatter
above to describe who they are and how their material should be handled.

## Fields

- **author_slug** — must match the author's folder name; it's how Creek knows who this is.
- **display_name** — the name you want to see in citations and anywhere Creek mentions them.
- **author_kind** — `human_source` (a real other person), `ai_as_user` (AI output
  you endorse as your own interests), or `collaborator` (co-authored with you).
- **voice_weight** — how much this author may influence generated voice. Leave at
  `0.0`; Creek never learns your voice from anything in this folder.
- **representativeness** — how closely this stands for *your* views: `self`,
  `endorsed`, `aspirational`, or `reference` (merely cited).
- **default_privacy_tier** — `open`, `personal`, or `intimate`.
- **attribution_required** — when `true`, anything drawing on this author must
  cite them.
- **notes** — free-form reminder of why you captured this author.
