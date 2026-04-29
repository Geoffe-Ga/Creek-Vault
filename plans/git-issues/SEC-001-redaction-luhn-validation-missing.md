# SEC-001: Credit-card pattern advertised as "Luhn-validated" but Luhn check is missing

**Severity:** High
**Category:** SEC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes — touches `creek/redact/patterns.py` and `scanner.py`; independent of other SEC issues
**Discovered by:** Reading dimension 5 (incomplete docs) cross-referenced with dimension 2 (security) — confirmed by parallel security agent and aspirational-doc audit

## Files affected
- `creek/redact/patterns.py:83-98` — credit-card regex
- `creek/redact/scanner.py` — entry point that calls patterns
- `creek-tools/docs/redaction.md:39` — claim "Credit card numbers (Luhn-validated)"
- `creek-tools/README.md:22` (implicitly, via the redaction feature claim)

## Dependencies
None.

## Blockers
None directly. But this is one of the redaction-doc-claim cluster (SEC-001 through SEC-005, INC-008, INC-010); they should be fixed together so the docs and code converge.

## Reproduction
```python
from creek.redact.patterns import REDACTION_PATTERNS

# Random 16-digit number that PASSES the regex but FAILS Luhn
sample = "4111-1111-1111-1112"   # last digit 2 instead of 1; not Luhn-valid
matches = list(REDACTION_PATTERNS["credit_card"].finditer(sample))
assert matches, "Regex matches"

# No Luhn check is performed downstream
import inspect
import creek.redact.scanner as s
assert "luhn" not in inspect.getsource(s).lower()
```
The regex matches; nothing in `redact/` ever runs Luhn validation.

## Analysis

`docs/redaction.md` line 39 explicitly advertises "Credit card numbers (Luhn-validated)" as a feature. `creek/redact/patterns.py:83-98` is a structural-only regex covering Visa/MC/Amex/Discover BIN ranges. The scanner does not call any Luhn check before flagging the match.

Consequences:
1. **High false-positive rate** in real exports. Random 16-digit identifiers, log timestamps, transaction IDs — anything with the right digit grouping — will be flagged. The user's `false_positive_allowlist` then balloons to compensate, eroding the protection.
2. **Documentation lies.** A user reading the security claims is told the system is more conservative than it is.
3. The fix is simple — Luhn validation is ~10 lines of code — so the cost of leaving it broken is asymmetric with the cost of fixing it.

Confidence: verified — read patterns.py, grep'd `luhn` across the entire codebase, no hits.

## Proposed remediation

Add a `_luhn_valid(digits: str) -> bool` helper in `creek/redact/scanner.py`. Have `RedactionScanner` post-filter `credit_card` matches: if the digits-only canonicalisation fails Luhn, drop the match. Keep the regex permissive (better recall) and let Luhn cull the noise.

Alternative: pull all post-match validators into a "validators" registry keyed by pattern name, so `credit_card → luhn`, future patterns can register their own. This generalises better but is more code.

## Acceptance criteria

- A new test in `tests/test_redact.py` asserts that:
  - `4111 1111 1111 1111` (Luhn-valid) is flagged.
  - `4111 1111 1111 1112` (Luhn-invalid) is **not** flagged.
- Real-world false-positive rate on a random-digit-heavy sample (1k random 16-digit numbers in non-CC contexts) drops to <1%.
- `docs/redaction.md` line 39 still reads accurately ("Credit card numbers (Luhn-validated)") *because the implementation now matches*.
- No other pattern's behaviour changes.

## References
- `creek/redact/patterns.py:83-98`
- `creek-tools/docs/redaction.md:39`
- Luhn algorithm: ISO/IEC 7812-1
