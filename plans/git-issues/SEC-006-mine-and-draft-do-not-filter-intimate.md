# SEC-006: `creek mine` and `creek draft` ignore privacy tier — intimate fragments leak into prompts

**Severity:** Critical
**Category:** SEC
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** partial — pairs with INC-007 (CLI flag); the filtering logic itself is independent
**Discovered by:** Reading dimension 2; confirmed by parallel agent

## Files affected
- `creek/generate/mining.py:336-359` — `_load_mining_snapshot` / `_load_fragments`
- `creek/generate/drafts.py:116-134` — `_load_fragments_by_id`
- `creek/cli.py:399-565` — `mine` / `draft` Typer commands (no `--include-tier` flag)
- `creek-tools/docs/generation.md:131-137` — claims "intimate fragments are excluded from prompts entirely"
- `creek-tools/docs/classification.md:100` — claims `--include-tier intimate` flag

## Dependencies
INC-007 (`--include-tier` CLI flag) — pair these in remediation.

## Blockers
This is the headline privacy guarantee of the project. Until fixed, `creek draft` will happily feed therapy reflections, recovery content, and journal entries to whatever LLM the user has configured (including Anthropic if that's the provider). For a tool that markets itself as "local-first" and "privacy-tier-aware", this is launch-blocking.

## Reproduction
1. Manually create a fragment with `privacy_tier: intimate` under `01-Fragments/Journal/`.
2. Run `creek mine --vault <vault>` → the intimate fragment will appear among candidate seeds for at least the resonance-chain and thread-terminus strategies.
3. Run `creek draft --vault <vault> --index 0` → the intimate fragment is included in the prompt that goes to the LLM.

(Confirmed by `grep -n "tier\|privacy" creek/generate/mining.py creek/generate/drafts.py` — no hits.)

## Analysis

`docs/generation.md`:

> All generation flows respect the privacy tier configuration. By default:
> - `intimate` fragments are **excluded** from prompts entirely.
> - `personal` fragments contribute summaries, not full bodies.
> - `open` fragments contribute full content.
> You can override with `--include-tier intimate` if you genuinely want intimate content in the prompt — the override is logged in the audit trail.

None of this is true:
- `mining.py` and `drafts.py` have no privacy-tier filter; they read every fragment.
- No CLI flag `--include-tier` exists in `creek/cli.py`.
- No audit-log entry is written when intimate content is sent to the LLM.
- The "personal → summaries" rule is also unimplemented.

Only `creek/generate/voice.py:147-160` and `creek/generate/skills.py:499` actually filter on `privacy_tier == "intimate"`. So *some* generation paths are safe; the most user-facing ones (`mine`, `draft`) are not.

Confidence: verified.

## Proposed remediation

1. Add a privacy filter at the top of `mining._load_fragments` and `drafts._load_fragments_by_id`. Default policy:
   - Skip fragments with `privacy_tier == intimate`
   - For `privacy_tier == personal`, replace the body with a synthesized summary (Pydantic-derived; or use the title alone)
   - `open` (or `public`) fragments pass through unchanged

2. Add `--include-tier {intimate,personal,open,all}` flag to both `mine` and `draft`. Default is "open and personal-summary".

3. When `--include-tier intimate` is given, write an audit log entry (see SEC-005 for the audit log redesign) capturing: timestamp, command, fragment IDs included, operator. Refuse silently if the audit log is unavailable.

4. Apply the same filter to `creek report` types that consume fragment bodies (`wavelength`, `synchronicity`, `unnamed`).

## Acceptance criteria

- A test creates fragments tagged `intimate` and confirms `mine`/`draft` produce empty/summary output without `--include-tier intimate`.
- With `--include-tier intimate`, an audit log entry is written and the intimate content appears.
- The `personal`-as-summary rule works — `draft` prompts for personal fragments contain only the title or a configured summary.
- Documentation lines reflect the actual behaviour.

## References
- `creek-tools/docs/generation.md:131-137`
- `creek-tools/docs/classification.md:94-100`
- `creek/generate/mining.py`
- `creek/generate/drafts.py`
- `creek/generate/voice.py:147-160` (the one place tier filtering already works)
- INC-007 (CLI flag); SEC-005 (audit log)
