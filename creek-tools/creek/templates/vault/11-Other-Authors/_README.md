# 11-Other-Authors

This category holds material **authored by someone other than you** — quoted
thinkers, collaborators, endorsed sources, and AI-generated pieces you treat as
representative of your own interests. It exists so the rest of the vault can
stay *yours*: the voice corpus, the fingerprint, and every generated draft are
trained only on **self-authored** fragments.

## The attribution model

Every author here gets a `_author.md` manifest that records who they are and how
their work must be credited (`attribution_required`, `representativeness`,
`default_privacy_tier`). That manifest is the attribution model: it keeps
other-authored ideas clearly labelled as theirs, so nothing borrowed is ever
silently reattributed to you.

## Why this folder is special

Creek learns to write like you by measuring your own words. If material written
by other people leaked into that corpus, the voice model would drift toward an
average of everyone you've ever quoted. So this category is the one place in the
vault that is **excluded from voice training** (see `creek-tools` FEAT-041 §7.5).
Capture other authors here freely — for their *ideas*, never for your *voice*.

## Structure — by author, then by work

```
11-Other-Authors/
├── _README.md                  # this file
├── _author.md                  # the manifest template — copy it into each author folder
├── <author-slug>/              # one folder per author
│   ├── _author.md              # who they are + how to attribute them
│   └── <work-slug>/            # one folder per work
│       └── *.md                # fragments of that work
└── ai-as-user/                 # reserved slug (see below)
    └── <piece-slug>/
        └── *.md
```

- **Folders are the source of truth.** There is no central manifest to edit —
  adding an author is just creating a folder and dropping a `_author.md` in it.
- **Slug authority:** an author's slug **is their folder name**. Keep slugs
  unique by hand; if two authors would collide, disambiguate the folder name
  (e.g. `jung-carl` vs `jung-emma`).

## The reserved `ai-as-user` author

`ai-as-user` is a **reserved author slug** for AI-generated pieces that you, the
owner, choose to treat as representative of your interests or beliefs — output
you endorse without having written word-for-word. It still lives here, outside
the voice corpus, because you didn't author its prose.

## Adding an author

1. Create a folder named for the author's slug (e.g. `11-Other-Authors/montaigne/`).
2. Copy `_author.md` into it and fill in the frontmatter.
3. Add one sub-folder per work, and drop that work's fragments inside.
