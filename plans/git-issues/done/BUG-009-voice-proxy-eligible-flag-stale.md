# BUG-009: `voice_proxy_eligible` defaults to `True` and is not reset on tier changes outside `enforce_tier`

**Severity:** Medium
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 4 (Pydantic validation gaps)

## Files affected
- `creek/models.py:334` — `Fragment.voice_proxy_eligible: bool = True`
- `creek/classify/privacy.py:151-180` — only path that resets the flag

## Dependencies
Related to SEC-014 (mine/draft don't filter intimate). Independent fix.

## Blockers
None.

## Reproduction
```python
# Construct a Fragment for an intimate journal entry directly (skipping privacy classifier)
f = Fragment(title="Journal", source=FragmentSource(platform=SourcePlatform.JOURNAL))
print(f.voice_proxy_eligible, f.privacy_tier)
# True, "unclassified"
# Now hand-set tier without going through enforce_tier
f.privacy_tier = PrivacyTier.INTIMATE.value
print(f.voice_proxy_eligible)
# True — still — even though tier is INTIMATE
```

## Analysis

`Fragment.voice_proxy_eligible` defaults to `True` and is intended to track whether the fragment may feed voice proxy generation. The only place that maintains the flag in sync with `privacy_tier` is `PrivacyClassifier.enforce_tier`, which uses `model_copy(update=...)` to set both fields together.

Failure modes:
1. **Direct construction skips the classifier.** The pipeline (BUG-001 path) constructs Fragment without classifier → `voice_proxy_eligible=True` regardless of source.
2. **Tier mutated without going through enforce_tier.** Anyone reading a fragment from disk and updating `privacy_tier` manually leaves `voice_proxy_eligible` stale.
3. **Loading from frontmatter.** When `frontmatter.load` reconstructs a fragment, both fields are reloaded from disk. If the disk copy is inconsistent (e.g., from a prior version of the code), the fragment is consistent only by accident.

`creek/generate/voice.py:147-160` filters fragments using `str(fragment.privacy_tier) == "intimate"` *or* `not fragment.voice_proxy_eligible` — so currently the redundancy provides some safety. But the redundancy is the smell: there's no single source of truth.

Confidence: verified.

## Proposed remediation

Two options:
- **A.** Remove the field. Replace every `voice_proxy_eligible` check with a check on `privacy_tier`. One source of truth. Simpler.
- **B.** Compute it dynamically: `@property def voice_proxy_eligible(self) -> bool: return self.privacy_tier != PrivacyTier.INTIMATE.value`. Drop the storage. Pydantic `computed_field`.

Pick B if there's any scenario where the user wants to opt a single non-intimate fragment out of voice proxy generation while leaving the tier alone. Otherwise A.

## Acceptance criteria

- A test constructs a fragment with `privacy_tier=INTIMATE` directly and asserts it is excluded from voice exemplar collection without an extra `enforce_tier` step.
- The `voice_proxy_eligible` field either no longer exists, or is a derived property with no setter.
- Existing tests pass without modification (since the behaviour is the same when the classifier was used).

## References
- `creek/models.py:334`
- `creek/classify/privacy.py:151-180`
- `creek/generate/voice.py:147-160`
