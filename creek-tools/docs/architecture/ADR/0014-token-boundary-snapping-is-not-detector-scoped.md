# ADR-0014: Token-boundary snapping is not detector-scoped

- **Status**: Accepted (ratified 2026-09-08)
- **Date**: 2026-09-08
- **Driving issue**: #946, ITEM 1 — escalated to the owner in
  [issue comment 5506768576](https://github.com/Geoffe-Ga/Creek-Vault/issues/946#issuecomment-5506768576)
- **Related**: #909 (the leak snapping exists to close), #942 (the sub-run
  entropy window gate), #945 (the emitted-marker carve-out), #832/#902 (the
  single-pass span union)

## Context

`Redactor._snap_to_candidate_runs` widens any match span whose start or end
falls **strictly inside** a `HIGH_ENTROPY_CANDIDATE` run
(`[A-Za-z0-9+/=_-]{20,}`) out to that run's edge, before the spans are merged
and spliced. It is the threshold-independent backstop beneath both entropy
gates: if a regex match covers only part of a token, the remainder is redacted
anyway, whatever `min_confidence` is set to (#909).

Issue #946 reported that `--apply` over-redacts hyphen-joined prose:
`order-555-123-4567-confirmation` is removed whole rather than as
`order-[REDACTED:phone_number]-confirmation`. It proposed exempting the
*self-delimiting* detectors — `phone_number`, `ssn`, `ipv4`, `ipv6`, `email` —
from snapping, on the ground that such a pattern "cannot leave the rest of the
secret behind", and asked for an explicit architectural ruling.

All figures below are Shannon entropy in bits/char, measured at commit
`1b46d076` with `creek.redact.scanner.shannon_entropy`, against
`entropy_threshold(mc) = 2.5 + 2.0 * mc` — so the shipped default
`min_confidence=0.6` is a bar of 3.70 bits/char.

## Decision — the carve-out is refused

**No detector is exempt from token-boundary snapping.** The rule stays
boundary-driven: it keys on where a span edge falls relative to a candidate
run, never on which pattern produced the span. Three independent grounds, in
order of force.

### Ground 1 — it does not fix the reported harm

Both examples in the issue are already redacted whole by the **entropy
detector**, with zero contribution from snapping:

| Input | whole-run | best 20-char window | default bar |
|---|---|---|---|
| `order-555-123-4567-confirmation` | 4.002268 | 4.021928 | 3.70 |
| `path/123-45-6789/more` | 4.201841 | 4.121928 | 3.70 |

Each is a single candidate run that clears the bar on its own, so
`_collect_high_entropy_spans` emits a span over the whole string. Exempting
`phone_number` from snapping would change only which **name**
`_select_marker_name` puts in the marker — from `phone_number` to
`high_entropy_string`, because the entropy span is the wider contributor — and
the output would still be one whole-string marker. Pinned by
`tests/test_redact.py::TestSnappingIsNotDetectorScoped::test_prose_over_redaction_survives_a_snapping_carve_out`.

The lever that actually governs this class of over-redaction is
`min_confidence` and the entropy gates, not the snap. Over a sample of ten
realistic hyphen- and underscore-joined prose strings carrying a phone number
or SSN, seven are covered by the entropy detector at the default confidence
and would be swallowed whole with or without the carve-out
(`invoice-2024-555-123-4567-final` 3.8858, `customer-ssn-123-45-6789-record`
4.0794, `ticket_555-123-4567_open` 3.9183, and the two above among them).
Only the low-entropy remainder (`the-the-the-the-555-123-4567` 3.1106 / 3.2219,
`acct-123-45-6789-2024` 3.5945, `case-555-123-4567-closed` 3.6683) escapes
both gates, and those are the cases where snapping is the sole cause — a
minority of the motivating class, bought at the cost of the leak in Ground 2.

### Ground 2 — it is fail-open, and the safety proof cannot be produced

"Self-delimiting" bounds a match against bisecting **its own** value. It says
nothing about the rest of the **run**, which may hold something else entirely.
A counterexample is constructible from the shipped patterns alone:

- `deadbeefdeadbeefdead-555-123-4567` — one candidate run at offsets 0..33,
  measuring **3.327090** whole-run and **3.346439** best-window. Both entropy
  gates are inert for every `min_confidence` strictly greater than
  **0.4232196723355077**, the shipped default of 0.6 included.
  `phone_number` matches 21..33, its start strictly inside the run. Today the
  whole run is redacted. Exempt the detector and `deadbeefdeadbeefdead`
  survives in cleartext.
- `deadbeefdeadbeefdead-192.168.1.1` mirrors it for `ipv4`. Its candidate run
  is only `deadbeefdeadbeefdead-192` (0..24), because `.` is outside
  `[A-Za-z0-9+/=_-]`; that run measures **2.755123** whole-run and
  **2.846439** best-window, so the whole-run gate fires only up to
  `min_confidence` 0.12756152200862747 and the window gate up to
  **0.17321967233550772**. Above that it too rests on snapping alone.

The surviving prefix is exactly the class #909 exists to cover: opaque,
under 20 characters after the split, invisible to both entropy gates.
Structurally, exempting selected detectors from the span union is the same
move that left the entropy detector outside the #832/#902 union — the move
that created #909.

Any future attempt to re-litigate this must ship and pass
`tests/test_redact.py::TestHighEntropyOverlapLeak::test_an_exempted_detector_cannot_leave_a_run_remainder_in_cleartext`,
asserting for **every** exempted pattern that a match whose edge falls
strictly inside a candidate run leaves no byte of that run in cleartext. It
fails on `deadbeefdeadbeefdead-555-123-4567` at the default confidence, which
is why the carve-out is refused rather than deferred.

### Ground 3 — the blast radius is unrecoverable, and asymmetric

Commit `c8c5131b` established that there is no queue and no backup:
`_atomic_write` (`creek/redact/cli_commands.py`) writes a temp file and
`os.replace`s it over the original, and the audit log records pattern names
and counts but no original text and no offsets. Over-redaction by `--apply`
is therefore irreversible — which the issue used to bound the stakes, but
which cuts harder the other way. A leaked secret written to disk is
unrecoverable in the privacy sense; over-redaction is at least *visible* in
the output and fixable by adding the token to `false_positive_allowlist`.
Under the escalate-only rule this settles the direction.

## Two corrections recorded, because the refusal rests on measurement

**The justification in the code was stale.** Four places — the
`creek/redact/redactor.py` module docstring, the `_snap_to_candidate_runs`
docstring, `docs/redaction.md`, and the fixture comment in
`tests/test_redact.py` — justified snapping with "an AWS example key followed
by fourteen repeats of a single character measures 3.14 bits/char whole-run
with no clearing 20-character window". The whole-run figure is right
(3.1445847115159293); the window claim is **false at HEAD**. That run's best
20-char window, at offset 1, measures **3.821928**, which clears the 3.70 bar,
so `has_high_entropy_region` returns `True` and the detector emits a span over
the whole run on its own. The fixture was invisible to the whole-run gate
only, and became visible to the detector when #942's window gate landed. All
four copies are corrected, and
`test_the_low_entropy_tail_fixture_measures_what_the_docs_claim` now pins both
numbers so the prose cannot drift from the fixture again.

**The guard was unpinned behaviourally.** Replacing the body of
`Redactor._snap_to_candidate_runs` with `return spans` and running
`./scripts/test.sh -k redact` at `1b46d076` leaves **every behavioural test
green**. The only failure is the structural AST gate
`TestEmittedMarkerCarveOut::test_the_candidacy_gate_has_exactly_one_definition`,
which notices the vanished `iter_unmarked_candidates` call site rather than
any change in output. Every fixture in `TestHighEntropyOverlapLeak` was
already covered by an entropy gate, and the tests naming snapping in their own
docstrings passed without it. That is why the refusal needed its own fixtures
(`_PREFIX_THEN_PHONE`, `_PREFIX_THEN_IPV4`) with an arithmetic guard
(`test_the_snapping_fixtures_are_inert_to_both_entropy_gates`) rather than
resting on the existing suite.

## Consequences

- `--apply` may still redact slightly **more** than `--scan` reported, because
  snapping widens on token shape rather than on a reported finding. That
  asymmetry is deliberate and fail-closed; it is recorded as an open reporting
  gap in `docs/redaction.md`.
- Operators who need a specific token left intact use
  `false_positive_allowlist`, which is the supported, explicit, per-value
  exemption — and which exempts the run from widening as well as from the
  entropy detector.
- Operators who find hyphen-joined prose over-redacted should raise
  `min_confidence`, the knob that actually governs it. Ground 1 shows the
  proposed carve-out would not have helped them.
- The refusal is pinned by `TestSnappingIsNotDetectorScoped`, so a future
  refactor cannot narrow the guard silently.
- Not decided here: whether `--scan` should report the widened span, which is
  a real reporting asymmetry and deserves its own issue; and whether narrowed
  `pattern_types` calls should keep the snap, which contradicts the contract
  pinned by `test_pattern_types_without_entropy_detector_leaves_tail` and is
  likewise separate.
