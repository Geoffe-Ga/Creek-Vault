# INC-014: `docs/redaction.md` claims IPv4/IPv6 are scanned; no such patterns exist

**Severity:** Medium
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes — pairs with SEC-002
**Discovered by:** Dimension 5

## Files affected
- `creek-tools/docs/redaction.md:39`
- `creek/redact/patterns.py` — no IP pattern

## Dependencies
SEC-002 (broader pattern coverage). Fold into that issue when remediated.

## Reproduction
```bash
grep -n "ipv4\|ipv6\|ip_address" creek/redact/patterns.py    # zero
```

## Analysis

`docs/redaction.md:39`:
> IP addresses (IPv4 + IPv6).

The pattern set has no IP addresses. A user reviewing the docs to decide whether to ship logs through `creek redact --scan` will assume IPs are stripped; they aren't.

## Proposed remediation

Bundle into SEC-002. Add IPv4 (`\b(?:\d{1,3}\.){3}\d{1,3}\b` with octet-range validation) and IPv6 (use a vetted regex; the spec one from RFC 4291 is fine; tests for shortened forms `::1`, mixed `::ffff:1.2.3.4`, etc.).

## Acceptance criteria

- IPv4 in arbitrary contexts is flagged.
- IPv6 in standard, shortened, and mixed forms is flagged.
- Excluded ranges (e.g., `127.0.0.1`, `0.0.0.0`, RFC1918 if user opts out) are configurable via the false-positive allow-list.

## References
- `creek-tools/docs/redaction.md:39`
- SEC-002
