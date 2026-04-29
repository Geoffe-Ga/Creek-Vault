# INC-009: `RedactionConfig.replacement_template` documented but not exposed

**Severity:** Low
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5; confirmed by parallel agent

## Files affected
- `creek/redact/redactor.py:95` (or thereabouts) — hardcoded `f"[REDACTED:{name}]"`
- `creek/config.py` — no `replacement_template` field
- `creek-tools/docs/redaction.md:71`

## Dependencies
None.

## Reproduction
```bash
grep -n "replacement_template\|RedactionConfig" creek/config.py
# no replacement_template field
grep -n "REDACTED" creek/redact/redactor.py
# hardcoded
```

## Analysis

`docs/redaction.md:71`:
> Replaces the match with `[REDACTED:<pattern_name>]` (configurable via `RedactionConfig.replacement_template`).

The replacement string is hardcoded; no `replacement_template` field exists on the `RedactionConfig` Pydantic model. Either drop the doc claim or add the field.

Confidence: verified.

## Proposed remediation

Add `RedactionConfig.replacement_template: str = "[REDACTED:{name}]"` and use `.format(name=match_name)` in the redactor. Validate that the template contains `{name}` (or accept no placeholder if the user explicitly wants a fixed string). Update `docs/redaction.md` to point at the field.

## Acceptance criteria

- A test with `replacement_template="<<{name}>>"` produces `<<api_key>>` in the output.
- An invalid template (e.g., wrong placeholder) is rejected at config load.
- Docs and code agree.

## References
- `creek-tools/docs/redaction.md:71`
- `creek/redact/redactor.py:~95`
- `creek/config.py` `RedactionConfig`
