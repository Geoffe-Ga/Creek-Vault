FEAT-040.11 (optional / lowest priority): Citation-integrity detectors

## Context
The guide's Citations § (WP:AIFICTREF) targets fabricated/broken references. This is **voice-neutral** (it's about factual integrity, not idiolect) and lowest priority — `creek draft` essays are not citation-heavy — but it catches the most policy-dangerous LLM failure (hallucinated sources) and is valuable for any MCP "lint my draft" surface that emits references.

## Scope (per the guide)
- Broken external links (opt-in network check + Internet Archive fallback; cached).
- Invalid DOI/ISBN (ISBN checksum; DOI format/resolve; the "DOI resolves to an unrelated article" case).
- Page-less / URL-less book citations on general-topic books.
- Named-but-unused / undefined refs (the `Cite error: … is not used` family).
- Tracking-param provenance (`utm_source=chatgpt.com`) as a ChatGPT-involvement signal (detect; stripping lives in 03).

## Out of scope
Rewriting/replacing citations. Everything in 01–10. Any voice/idiolect concern (this issue is orthogonal to the fingerprint).

## Design constraints
- Network checks opt-in, cached, rate-limited, timeout-bounded; offline → format/checksum only.
- Encode the guide's false-positive caveats: paywalled/library links, bot-mangled URLs, the 2018–2023 VisualEditor low-PMID artifact (not AI).
- Detection/surfacing only; never auto-delete a citation.

## Files to touch
- new: `creek/generate/ai_style/citations.py` (registers `category="citation"` tells)
- edit: `AIStyleConfig` (network toggle, cache path); optionally surfaced by the 10 lint check
- tests: `tests/generate/ai_style/test_citations.py` (valid/invalid ISBN+DOI, unused ref, mocked-network link checks, low-PMID negative)

## Acceptance criteria
- Invalid ISBN/DOI flagged; unused named ref detected; page-less general-topic book cite flagged; offline mode skips network cleanly; caveated negatives don't hard-fail.
- check-all.sh green; ≥90% cov.

## Est. LOC
~500–700. Depends on: 01. Optional; schedule after 01–10.
