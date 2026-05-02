# SEC-002: Redaction pattern set misses common modern secret formats

**Severity:** High
**Category:** SEC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 2 (security); confirmed by parallel agent

## Files affected
- `creek/redact/patterns.py:48-188` — `PATTERN_METADATA`
- `creek-tools/docs/redaction.md:34-41` — claim "API keys (AWS, GitHub, Anthropic, OpenAI, Slack tokens, generic high-entropy strings)" and IPv4/IPv6

## Dependencies
None.

## Blockers
None directly. Cluster with SEC-001 (Luhn), SEC-003 (high-entropy detector).

## Reproduction
```python
from creek.redact.patterns import REDACTION_PATTERNS

# Discord bot token (~70 chars, base64-ish, ".gabcde." middle segment, etc.)
# Format documented at: https://discord.com/developers/docs/reference
sample_discord = "<DISCORD-BOT-TOKEN-EXAMPLE-OMITTED>"  # see Discord docs for shape
matched = any(p.search(sample_discord) for p in REDACTION_PATTERNS.values())
# False — no Discord token pattern

# GitHub fine-grained PAT (prefix: github_pat_, total ~95 chars)
sample_pat = "<GITHUB-FINE-GRAINED-PAT-EXAMPLE-OMITTED>"  # see GitHub docs for shape
matched = any(p.search(sample_pat) for p in REDACTION_PATTERNS.values())
# False — github_token pattern is gh[pousr]_, doesn't match github_pat_

# IPv4
sample_ipv4 = "Server: 10.20.30.40"
matched = any(p.search(sample_ipv4) for p in REDACTION_PATTERNS.values())
# False — docs claim IPv4 is covered, no pattern exists

# IPv6
sample_ipv6 = "2001:0db8:85a3::8a2e:0370:7334"
matched = any(p.search(sample_ipv6) for p in REDACTION_PATTERNS.values())
# False
```

## Analysis

`docs/redaction.md` lines 34-41 advertise these patterns:
- API keys (AWS, GitHub, Anthropic, OpenAI, Slack)
- Email addresses
- Phone numbers (US + international)
- Social Security numbers
- Credit cards (Luhn-validated — see SEC-001)
- IP addresses (IPv4 + IPv6)
- Common private-key headers
- Generic high-entropy strings

`creek/redact/patterns.py` actually ships:
- `api_key`: `AKIA...` (AWS access key) and `sk[-_]...` (catches Anthropic and OpenAI by incidental overlap, NOT Stripe `sk_live_` or `sk_test_`)
- `password`, `ssn`, `email`, `credit_card` (no Luhn), `email_password_combo`, `aws_secret_key`, `private_key`, `bearer_token`, `env_secret`, `slack_token`, `phone_number`, `github_token` (only `gh[pousr]_`, NOT `github_pat_`), `jwt`

Missing entirely:
- **IPv4 / IPv6** — claimed in docs, not in `patterns.py`
- **Discord bot tokens** — `MTE...`/`MTA...` base64-encoded, no pattern
- **GitHub fine-grained PATs** — `github_pat_...` (95+ chars), explicit format ≠ `gh[pousr]_`
- **Stripe keys** — `sk_live_...`/`sk_test_...` underscore-separated, mostly covered by `sk[-_]` regex but not explicit
- **Anthropic `sk-ant-...`** — covered incidentally; no explicit test
- **Generic high-entropy strings** — claimed in docs (the `min_confidence` field in config is even named for it), no implementation
- **OpenAI project keys** `sk-proj-...` (newer format) — incidental coverage at best
- **International phone numbers** — claimed in docs; pattern is US-only (no `+44`/`+33` etc.)

`RedactionConfig.min_confidence` exists in `creek/config.py` but no code reads it.

Confidence: verified — read `patterns.py` end-to-end and grepped for entropy.

## Proposed remediation

Add pattern entries for: IPv4, IPv6, Discord bot tokens, GitHub fine-grained PATs (`github_pat_...`), Stripe (`sk_live_`, `sk_test_`, `pk_live_`, `pk_test_`), Anthropic (`sk-ant-...` explicit), OpenAI project (`sk-proj-...`).

For "high-entropy strings", implement a Shannon-entropy-based detector with the threshold drawn from `RedactionConfig.min_confidence`. Apply it only to substrings of length ≥ 20 surrounded by non-word boundaries to keep noise down.

For phone numbers, broaden to E.164-ish: `\+?[1-9]\d{1,14}` (with reasonable boundary anchors).

Each new pattern needs a `false_positive_notes` entry and at least two unit tests (positive + negative).

## Acceptance criteria

- All 7 missing pattern categories covered, each with positive + negative test cases.
- IPv4 and IPv6 examples from `docs/redaction.md` are flagged.
- A real `github_pat_...` token is flagged.
- A real Discord bot token (base64-decoded format) is flagged.
- A `sk-proj-...` OpenAI key is flagged.
- A high-entropy 32-char hex string in a credential context is flagged.
- `min_confidence` config option is actually consulted by the entropy detector.

## References
- `creek-tools/docs/redaction.md:34-41`
- `creek/redact/patterns.py`
- `creek/config.py` `RedactionConfig.min_confidence`
- GitHub fine-grained PAT format: <https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens>
