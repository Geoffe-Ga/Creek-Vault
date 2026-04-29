# INC-016: `OCRConfig.min_confidence` documented but not exposed as config

**Severity:** Medium
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/config.py:55-66` — `OCRConfig`
- `creek/ingest/images.py:239-240` — captures confidence but no routing
- `creek-tools/docs/ingestion.md:59` — claim "Confidence below `OCRConfig.min_confidence` lands the fragment in the review queue"

## Dependencies
INC-002 (review queue command).

## Reproduction
```bash
grep -n "min_confidence" creek/config.py
# (no match in OCRConfig)
grep -rn "min_confidence" creek/ingest/images.py
# pytesseract returns confidence; no routing logic uses it
```

## Analysis

The doc claim and the code shape are mismatched:
- `docs/ingestion.md:59`: "Confidence below `OCRConfig.min_confidence` lands the fragment in the review queue."
- `creek/config.py` `OCRConfig` has `enabled`, `engine`, `languages` — no `min_confidence`.
- `creek/ingest/images.py` extracts confidence per OCR call but doesn't use it for routing.

Two fixes required:
1. Add `min_confidence: float = 0.6` (or similar) to `OCRConfig`.
2. In `ImageIngestor.parse`, when overall page confidence < `min_confidence`, set `review: pending_review` in the generated frontmatter — once the review queue is real (INC-002).

## Acceptance criteria

- `OCRConfig.min_confidence` exists and is documented.
- A test ingests a synthetic low-confidence image, asserts the fragment lands with `review: pending_review`.
- Above-threshold images don't get the marker.
- `creek review` (per INC-002) surfaces these fragments alongside low-confidence classifications.

## References
- `creek-tools/docs/ingestion.md:59`
- `creek/config.py:55-66`
- `creek/ingest/images.py`
- INC-002
