# BUG-003: `Pipeline._run_classification` always runs LLM, ignoring `confidence_threshold`

**Severity:** High
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1 — `creek/pipeline.py:255-262`

## Files affected
- `creek/pipeline.py:255-262`

## Dependencies
None directly. Should be fixed alongside BUG-001 since both touch pipeline.

## Blockers
None.

## Reproduction
Read the function:
```python
for fragment in fragments:
    frag = self.rule_classifier.classify(fragment)
    frag = self.llm_classifier.classify(frag)        # always runs
    classified.append(frag)
```
The LLM classifier runs on every fragment, regardless of the rule classifier's confidence and regardless of `ClassificationConfig.confidence_threshold`.

## Analysis

`docs/classification.md` documents the intended workflow:

> A common workflow: run `--method rules` over the whole vault first, then run `--method llm` only on fragments whose classification is `unclassified` or whose confidence is below `ClassificationConfig.confidence_threshold`.

The pipeline does not honour that. It chains both classifiers on every fragment. Consequences:

1. **Cost.** With `provider: anthropic`, a 10k-fragment vault will spend $10–30 (Sonnet) on every `creek process` run — even if rules already classified everything with high confidence.
2. **Wall-clock.** Ollama at 2–4s/fragment × 10k = 5.5–11 hours per `process` invocation. The cost of a re-run after fixing one ingestor is enormous.
3. **Spec drift.** The "rules first, LLM only for low-confidence tail" architecture is what the docs and ontology spec promise. The implementation breaks it.

Confidence: verified — read pipeline.py.

## Proposed remediation

Gate the LLM call on rule-classifier confidence:

```python
threshold = self.config.classification.confidence_threshold
for fragment in fragments:
    frag = self.rule_classifier.classify(fragment)
    if self._needs_llm(frag, threshold):
        frag = self.llm_classifier.classify(frag)
    classified.append(frag)
```

Where `_needs_llm` checks (a) the rule classifier left at least one dimension unclassified, or (b) `frag.classification.confidence < threshold`. Also respect the `auto_classify_sources` / `human_review_sources` lists.

Alternative: do the gating inside `LLMClassifier.classify()` itself (skip if not needed). Keeps `pipeline.py` simpler.

## Acceptance criteria

- A unit test confirms a fragment with rule-derived `confidence >= threshold` does *not* invoke `LLMClassifier.classify`.
- A unit test confirms a fragment with rule-derived `confidence < threshold` *does* invoke it.
- Sources in `human_review_sources` always go to the review queue, never to the LLM directly.
- A 10k-fragment vault that classifies cleanly with rules makes zero LLM calls in `creek process`.

## References
- `creek-tools/docs/classification.md` workflow section
- `creek/config.py` `ClassificationConfig.confidence_threshold`, `auto_classify_sources`, `human_review_sources`
