## Role

You are a senior Python engineer working in `creek-tools/creek/ingest/`, fluent in the Claude and
ChatGPT ingestors, the `ParsedFragment` → `Fragment` assembly path, and the `Authorship` model.

## Goal

Stop AI prose from poisoning the voice corpus. When ingesting an AI chatbot conversation, emit the
**human turn and the AI turn as separately-attributed fragments**: the human turn is the owner's
voice (`source.author = self`, normal `voice_weight`), and the AI turn is AI-authored
(`source.author = ai`, `voice_weight = 0.0`) so it can never train the voice proxy. This must drop
the diagnostic's `ai_corpus_leak` to ≈0 for freshly-ingested chats.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_01_NUMBER (the `ai_corpus_leak` probe exists to prove the fix).
- **Findings:** SPEC summary §"AI chat turns leak into the voice corpus".
- **Files involved:**
  - `creek-tools/creek/ingest/claude.py` — `ClaudeIngestor._build_fragment` currently **merges**
    `human_text` + `assistant_text` into one fragment and `generate_frontmatter` never sets
    `source.author` (so it defaults to `Authorship.SELF`).
  - `creek-tools/creek/ingest/chatgpt.py` — same merge defect in `_pair_messages_to_fragments`.
  - `creek-tools/creek/models.py` — `Authorship` enum (`self/ai/other/collaborative`),
    `FragmentSource.author` default, and the `voice_proxy_eligible` computed field that already
    excludes non-`self` authors. **No model change is required** under the default decision below.
  - `creek-tools/tests/test_ingest_claude.py`, `tests/test_chatgpt_ingest.py`.
- **Prior decisions / state of the world:** both ingestors already keep `human_text` and
  `assistant_text` separate in `ParsedFragment.metadata`; they just collapse them at write time.
  The human and AI turn are genuinely different authors and should be different fragments.

## Output Format

A single PR containing:

- [ ] Claude + ChatGPT ingestors emit a human fragment (`source.author=self`) and an AI fragment
  (`source.author=ai`, `voice_weight=0.0`) per conversational turn, preserving conversation id,
  turn index, and timestamps so threading/linking still works.
- [ ] Tests: ingesting a fixture conversation yields per-role fragments with the correct
  `source.author` and `voice_weight`; the AI fragment is proven **excluded** from the voice corpus
  (`voice_proxy_eligible is False`).
- [ ] A test asserting the diagnostic's `ai_corpus_leak` is ≈0 over a freshly-ingested fixture chat.

## Examples

```python
frags = list(ClaudeIngestor().parse(conversation_export))
human = [f for f in frags if f.source.author == Authorship.SELF]
ai    = [f for f in frags if f.source.author == Authorship.AI]
assert human and ai
assert all(f.voice_weight == 0.0 for f in ai)
assert all(f.voice_proxy_eligible is False for f in ai)
```

## Constraints

**Open decision (resolve in this PR, surface in the PR body):** the owner described AI output as
"ai-assisted." The default for this issue is to reuse `Authorship.AI` (no enum change), because it
already excludes the turn from the proxy. Human turns stay `self` and carry the existing `PERSONAL`
privacy tier, so Issue 03's audience weighting ranks them below published essays automatically.
- [ ] If the owner wants a *distinct* `ai-assisted` authorship value, add it to the `Authorship`
  enum and route AI turns to it (still `voice_weight=0.0`, still excluded). Otherwise reuse `AI`.

**Scope fence:** Do not implement re-ingestion/migration of *existing* merged fragments here — that
is Issue 06. This issue changes how *new* ingests are attributed. Do not alter audience weighting
or the voice corpus selection logic.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Backward-compat invariant:** ingesting non-chat sources (essays, markdown, documents) is
unchanged; their fragments keep their existing single-author attribution.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Proven by a test that an AI turn is excluded from the voice corpus and a human turn is not.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`, and states which attribution decision was taken.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `ingest`, `voice`
