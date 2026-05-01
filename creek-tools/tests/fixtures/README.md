# Test fixtures

Realistic input data for ingestor and pipeline tests, organised by the
*kind of failure* the fixture exercises so we know which file to add
when introducing a new edge case.

## Layout

```
tests/fixtures/
├── README.md                       — this file (TEST-004)
├── corrupt/                        — malformed inputs (truncated, empty, bad YAML)
├── encoding/                       — non-UTF-8 inputs (cp1252, shift_jis, BOM)
├── injection/                      — adversarial inputs (YAML in body, secret look-alikes)
├── scale/                          — large inputs to surface quadratic blowups
├── symlinks/                       — instructions for runtime-built symlink fixtures
└── sample_*.{json,md}              — happy-path fixtures (legacy location)
```

## Adding a new fixture

1. Choose the right subdirectory based on the *failure mode* it
   exercises, not the file extension.
2. Keep secrets ﬁctional. Use placeholders that are obviously not
   real and will not trigger upstream secret scanners:
   - `AKIAIOSFODNN7EXAMPLE` (canonical AWS docs example)
   - `sk_test_PLACEHOLDER_NOT_A_REAL_KEY` (Stripe test-key shape)
   - `user@example.com` (RFC-2606 reserved domain)
   - `4111111111111111` (canonical card test PAN)
3. Add a corresponding test under `tests/test_*_failure_modes.py` that
   asserts the system handles the input gracefully (clear error, no
   crash, no data loss).

## Why

Six integration tests with mocked ingestors did not catch BUG-001
(pipeline drops fragments) or BUG-008 (vault writer stores empty body).
A fixture-driven failure-mode test is the cheapest way to lock in
real-disk-I/O behaviour without standing up a full e2e suite for every
bug.
