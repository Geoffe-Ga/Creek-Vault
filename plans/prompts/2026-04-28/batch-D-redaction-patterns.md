# Batch D — Redaction patterns and pre-ingest hygiene

## Role

You are a secrets-detection engineer. You know that a "thorough" pattern set is one that catches real-world tokens, suppresses false positives via post-validation rather than weaker regexes, and exposes its decisions for the user to audit and tune. You assume the pattern doc is read by a security-aware reader and treat any gap as a launch blocker for that reader's trust.

## Goal

Bring the redaction module up to the coverage that `docs/redaction.md` already advertises: Luhn-validate credit cards, add Discord / GitHub fine-grained / Stripe / Anthropic / OpenAI patterns, IPv4 / IPv6, and a generic high-entropy detector wired to `RedactionConfig.min_confidence`. Make `replacement_template` configurable. Wire `OCRConfig.min_confidence` so low-confidence OCR routes to the review queue.

## Context

The pattern set in `creek/redact/patterns.py` covers the easy wins (AWS, Slack, JWT) but misses the modern formats most likely to appear in real exports: Discord bot tokens, GitHub fine-grained PATs (`github_pat_...`), Stripe keys, IPv4/IPv6, and generic high-entropy strings. The credit-card pattern is structural-only — the docs claim Luhn validation but no Luhn check exists, so false positive noise dominates. `RedactionConfig.replacement_template` is documented but not in the model. `OCRConfig.min_confidence` is documented but not on the config class.

This batch is fully independent of Batches A, B, and C. It can run in parallel with them.

**Read these issue files before starting** (in `plans/git-issues/`):
- `SEC-001-redaction-luhn-validation-missing.md` — Luhn post-filter
- `SEC-002-redaction-pattern-coverage-gaps.md` — the seven missing pattern categories
- `INC-009-redaction-replacement-template-not-configurable.md` — config field
- `INC-014-no-ipv4-ipv6-redaction.md` — duplicate of part of SEC-002 (fold)
- `INC-016-ocr-min-confidence-not-config.md` — OCRConfig field + review-queue routing

**Files you will primarily change:**
- `creek-tools/creek/redact/patterns.py` — `PATTERN_METADATA`
- `creek-tools/creek/redact/scanner.py` — Luhn post-validator + entropy detector
- `creek-tools/creek/redact/redactor.py` — `replacement_template` use
- `creek-tools/creek/config.py` — add `RedactionConfig.replacement_template`, `OCRConfig.min_confidence`
- `creek-tools/creek/ingest/images.py` — route low-confidence OCR to review queue
- `creek-tools/tests/test_redact.py` — pattern-coverage tests
- `creek-tools/tests/fixtures/secrets/` — small fixture files with positive + negative samples
- `creek-tools/docs/redaction.md` and `docs/configuration.md` — keep aligned

**Files to consult:**
- The existing `slack_token`, `github_token`, `aws_secret_key` regexes for naming conventions
- `false_positive_notes` field — every new pattern needs one

## Output format

A small commit per finding, each with positive + negative test cases:

1. **Luhn post-filter for `credit_card`.** A `_luhn_valid(digits: str) -> bool` helper in `scanner.py`; `RedactionScanner` filters CC matches through it.
2. **Pattern coverage additions** (one regex group per commit if you prefer; or one commit grouped by "modern API tokens" / "network identifiers"):
   - Discord bot token
   - GitHub fine-grained PAT (`github_pat_...`)
   - Stripe (`sk_live_...`, `sk_test_...`, `pk_live_...`, `pk_test_...`)
   - Anthropic explicit (`sk-ant-...`)
   - OpenAI project (`sk-proj-...`)
   - IPv4 (with octet validation)
   - IPv6 (full + shortened forms; mixed `::ffff:1.2.3.4`)
3. **Generic high-entropy detector.** Shannon entropy over base64-ish or hex-ish substrings ≥20 chars. Threshold from `RedactionConfig.min_confidence`. Skip if surrounded by an `false_positive_allowlist` substring.
4. **`replacement_template` config field.** `RedactionConfig.replacement_template: str = "[REDACTED:{name}]"`; `Redactor` formats it with the matched pattern name. Validate the template at config-load time.
5. **`OCRConfig.min_confidence` field + routing.** `OCRConfig.min_confidence: float = 0.6`; `ImageIngestor.parse` adds `review: pending_review` to frontmatter when overall page confidence falls below threshold.

## Examples

The Luhn test:

```python
@pytest.mark.parametrize("number", [
    "4111 1111 1111 1111",  # Luhn-valid Visa test
    "5555-5555-5555-4444",  # Luhn-valid Mastercard test
])
def test_luhn_accepts_valid(number):
    matches = list(RedactionScanner(config=RedactionConfig()).scan_text(number))
    assert any(m.pattern == "credit_card" for m in matches)


@pytest.mark.parametrize("number", [
    "4111 1111 1111 1112",   # Luhn-invalid (last digit wrong)
    "1234 5678 9012 3456",   # Random 16-digit string
])
def test_luhn_rejects_invalid(number):
    matches = list(RedactionScanner(config=RedactionConfig()).scan_text(number))
    assert not any(m.pattern == "credit_card" for m in matches)
```

The replacement-template test:

```python
def test_replacement_template_configurable():
    cfg = RedactionConfig(replacement_template="<<{name}>>")
    redactor = Redactor(config=cfg)
    out = redactor.apply_to_text("AKIAEXAMPLEEXAMPLEAB")  # AWS prefix
    assert "<<api_key>>" in out
    assert "REDACTED" not in out


def test_replacement_template_invalid_fails_loud():
    with pytest.raises(ValidationError):
        RedactionConfig(replacement_template="no placeholder here")
```

## Requirements

- **Use `/stay-green`**: write the failing pattern test first; add the regex; verify the test passes. For each new pattern, include at least one positive case and one realistic negative case.
- **Use `/max-quality-no-shortcuts`**: if a regex is too noisy, fix it with a post-validator (Luhn-style) rather than a wider negative-lookahead grab-bag. If the entropy detector flags too much, raise the threshold deliberately and document the call in `false_positive_notes`.
- All new patterns get a `PatternInfo` entry with `description`, `severity`, `false_positive_notes`. Do not add bare regexes to `REDACTION_PATTERNS`.
- **Do not commit real secrets to test fixtures.** Use Stripe test keys (`sk_test_...` documented public set), AWS docs example keys (`AKIAIOSFODNN7EXAMPLE`), and GitHub's documented test fine-grained PAT format (without a real signature). If unsure, parameterise the regex test rather than store the literal string.
- Maintain `mypy --strict` clean.
- Maintain ≥90% branch coverage on `redact/`. The new entropy detector should hit close to 100% — its branches are simple.
- Update `docs/redaction.md` to keep claims aligned with the actual pattern set. The line that lists pattern categories should match the `PATTERN_METADATA` keys.
- Update `docs/configuration.md` so `replacement_template` and `OCRConfig.min_confidence` appear in the field tables.
- Pre-existing tests for `credit_card` may need updating (some fixtures may be Luhn-invalid by accident).
- Defer the symlink guard (SEC-003) and audit-log writes (INC-015) — those belong to Batches G and C respectively.

## Definition of done

`./scripts/check-all.sh` exits 0. The pattern coverage matches `docs/redaction.md` claims with no gaps. A regression test for each missing-format example in SEC-002 catches the format. The Luhn false-positive rate on a synthetic random-digit corpus drops below 1%. `creek redact --scan --report` produces a readable markdown report listing every flagged file and pattern.
