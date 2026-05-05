# BUG-004: `creek process` redaction stage scans but never applies — sensitive data flows through

**Severity:** Critical
**Category:** BUG
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 2 (security) — `creek/pipeline.py:172-197`

## Files affected
- `creek/pipeline.py:172-197` — `_run_redaction`

## Dependencies
Conceptually relates to INC-001 (CLI stubs). Independent fix.

## Blockers
This is a security correctness blocker for any "single-shot" process workflow.

## Reproduction
Drop a file with a real-looking secret into a source dir, run `creek process`. Inspect the resulting fragment in the vault — the secret will be present.

```bash
echo "AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE_xx_xx_xx_xx_xx_x" > /tmp/secret.md
creek process --source /tmp --vault ~/vault
grep -r "AKIAIOS" ~/vault/01-Fragments/
# match: secret leaked into vault
```

(Note: also blocked by BUG-001 today; once that's fixed, this bug becomes user-visible.)

## Analysis

`pipeline._run_redaction` only *scans* and *logs* matches:

```python
if self.config.redaction.enabled:
    matches = self.scanner.scan_directory(source_path)
    if matches:
        logger.info("Redaction scan found %d potential PII match(es)", len(matches))
return file_count
```

It never calls `Redactor.apply()` and the matches are not propagated downstream. The README's quickstart sequence puts `creek redact --apply` *before* `creek process` as a separate step, which works *if* the user remembers to do it. But the spec (`creek-tools/docs/redaction.md` line 3) says "`creek redact` is the **first** thing you run" — and `creek process` is supposed to be the user-facing one-shot pipeline. A user running `creek process` against a fresh export will silently propagate every secret into the vault.

Documentation also claims (`docs/redaction.md` line 88-89): "**Always** scan before `creek ingest` on any new export." But `process` *contains* ingest, so scan-only-without-apply is a footgun.

Confidence: verified.

## Proposed remediation

Two sane options:

**A.** Make `_run_redaction` actually apply redactions when `redaction.enabled and not redaction.dry_run`. Pass `dry_run` through. Refuse to ingest until the queue is empty.

**B.** Make `creek process` *fail loudly* if the redaction queue contains unresolved matches, with a message instructing the user to run `creek redact --apply` (or `--review`) first. This preserves the "review before commit" workflow but stops silent leakage.

Option B matches the spec better; option A is closer to what casual users expect from a one-shot pipeline. Pick one and document it.

## Acceptance criteria

- A test puts a fake secret in a temp source dir, runs the pipeline against it, and asserts (a) the pipeline either redacted the secret in the vault, or (b) refused to write the fragment and logged a clear remediation step.
- The README quickstart no longer relies on the user remembering to run `redact --apply` separately, *or* the README explicitly warns that `creek process` will fail until redactions are applied.
- Existing redaction CLI behaviour (`creek redact --scan/--apply/--review`) still works unchanged.

## References
- `creek-tools/docs/redaction.md` lines 1-90
- `creek/pipeline.py:172-197`
- `creek/redact/redactor.py`
