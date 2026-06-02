## Role

You are a senior Python engineer working in this repo's `creek-tools/creek/` package, fluent in
the template-tree scaffolding mechanism (`scaffold.py` copies `creek/templates/vault/` verbatim).

## Goal

Add a new top-level vault category `11-Other-Authors/` to the canonical scaffold so that
`creek init --vault <path>` materializes it, including a `_README.md` explaining the attribution
model and voice-training exclusion, an example `_author.md` template, and `.gitkeep`s — with no
change to the copy mechanism itself.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** none — this is the skeleton issue for EPIC_01.
- **SPEC section:** `plans/crawdad-writing-system/SPEC.md` §7.1 (structure), §7.5 (exclusion rationale for `_README.md`).
- **Files involved:**
  - `creek-tools/creek/templates/vault/11-Other-Authors/` — new subtree (`_README.md`, `_author.md` template, `ai-as-user/.gitkeep`, an `<example-author>/.gitkeep`).
  - `creek-tools/creek/scaffold.py` — verify the directory count / no logic change needed.
  - `creek-tools/tests/` — scaffold tests.
- **Prior decisions:** Folders are the source of truth; there is no manifest. `ai-as-user` is a reserved author slug. Open question #3 (slug authority): default to slug = folder name, manual de-dup — document this in `_README.md`.
- **State of the world:** The vault scaffold currently ends at `10-Liminal/`. `scaffold_vault()` copies the template tree wholesale.

## Output Format

A single PR containing:

- [ ] New `11-Other-Authors/` template subtree with `_README.md`, `_author.md` template, and `.gitkeep`s.
- [ ] Test asserting `creek init` creates `11-Other-Authors/`, `11-Other-Authors/ai-as-user/`, and the `_README.md`.
- [ ] If `scaffold.py` exposes a category count or list, update + test it.
- [ ] Docs note in the ontology spec / AGENTS.md referencing the new category (if low-cost; else defer to EPIC_02 polish).

## Examples

**After this issue lands:**
```bash
creek init --vault /tmp/v
ls /tmp/v/11-Other-Authors/            # _README.md  _author.md  ai-as-user/  example-author/
test -f /tmp/v/11-Other-Authors/ai-as-user/.gitkeep && echo ok
```

`_author.md` template frontmatter (the shape EPIC_01_ISSUE_02 will parse):
```yaml
type: author_manifest
author_slug: "example-author"
display_name: "Example Author"
author_kind: human_source   # human_source | ai_as_user | collaborator
voice_weight: 0.0
representativeness: reference  # self | endorsed | aspirational | reference
default_privacy_tier: open
attribution_required: true
notes: "Captured for ideas, not voice."
```

## Constraints

**Scope fence:** Do not add the `AuthorManifest` model or any parsing logic — that is
EPIC_01_ISSUE_02's scope. Do not touch classification or voice generation. Template + scaffold test only.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR merges. `creek init` on
an existing vault must remain idempotent (`dirs_exist_ok=True`) and not disturb `00`–`10`.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean — no skipped hooks.
- [ ] Coverage on changed lines meets the repo threshold (≥90% branch).
- [ ] Public API / docs reflect the new category where touched.
- [ ] PR body uses git-workflow's template and includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `vault`
