# BUG-002: Naive `datetime.now()` calls bypass the America/Los_Angeles normalization invariant

**Severity:** High
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes — touches several modules but each call site is independent
**Discovered by:** Reading dimension 1 (bugs); confirmed by parallel bug-hunt agent

## Files affected
- `creek/models.py:319-320` — `Fragment.created` / `Fragment.ingested` default factories use bare `datetime.now`
- `creek/vault/writer.py:371` — `_log_provenance` writes `datetime.now().isoformat()` (naive)
- `creek/vault/writer.py:109` — `_extract_date_str` falls back to `date.today()`
- `creek/classify/review.py:101, 123` — naive `datetime.now()` in review-queue timestamps
- `creek/link/threads.py:335` — `ThreadDetector._now = now or datetime.now()` defaults naive
- `creek/purge/audit.py:42-44` — uses `datetime.now(tz=UTC)` (correct *aware*, but UTC instead of LA_TZ)

## Dependencies
None.

## Blockers
Affects every downstream temporal computation: thread dormancy/resolved status, synchronicity time-gap filter (>30 days criterion), wavelength reports, mining "thread terminus" detection, audit timestamps. Until these are consistent, those features misbehave nondeterministically across timezones.

## Reproduction
```python
from creek.models import Fragment, FragmentSource, SourcePlatform
f = Fragment(title="x", source=FragmentSource(platform=SourcePlatform.OTHER))
print(f.created.tzinfo)   # None — naive
```

```python
from creek.link.threads import ThreadDetector
d = ThreadDetector(...)
print(d._now.tzinfo)       # None — naive on default
```

## Analysis

The ontology spec (§8.3, §3.4) and `creek-tools/docs/ingestion.md` both insist on America/Los_Angeles normalization. `creek/ingest/base.py:32` defines `LA_TZ = ZoneInfo("America/Los_Angeles")` and `normalize_timestamp()` honours it. But several downstream modules drop back to bare `datetime.now()`:

1. **`Fragment.created`/`ingested` defaults** (models.py:319-320) — anywhere a `Fragment` is constructed without an explicit timestamp (e.g., `creek/pipeline.py:227`), the model's default factory yields a naive datetime. Pydantic does *not* attach `LA_TZ` automatically.
2. **Vault writer provenance** (writer.py:371) — every fragment write logs `datetime.now().isoformat()` with no tz; a 23:00 PT write will record as if it happened at 23:00 in *some* unknown tz.
3. **Review queue timestamps** (classify/review.py:101, 123) — same issue; whoever reads the queue can't tell what timezone the entries are in.
4. **ThreadDetector** (link/threads.py:335) — naive default means thread "days_since" math depends on the host TZ. `(naive_now.date() - aware_last_seen.date()).days` is nominally OK because `.date()` strips tz, but the result is host-tz-dependent and inconsistent with other LA_TZ-aware code paths.
5. **PurgeAuditLog** (purge/audit.py:42-44) — uses `datetime.now(tz=UTC)`. That's tz-aware, but the rest of the system claims LA_TZ; inconsistency makes audit trails harder to read alongside fragment timestamps.

Consequence: *naive ↔ aware* comparisons throw `TypeError: can't compare offset-naive and offset-aware datetimes` in any code that mixes them. Several places in `link/threads.py` and `generate/wavelength.py` mix Fragment.created (naive) with `datetime.now(tz=LA_TZ)`. Tests pass because tests construct fragments with explicit aware timestamps; production won't.

Confidence: verified — read each call site.

## Proposed remediation

Centralise on a single helper, e.g. `creek.time.now_la()` returning `datetime.now(tz=LA_TZ)`. Replace every offending call site. For Pydantic model defaults, use `Field(default_factory=now_la)`.

For `PurgeAuditLog`, make a deliberate decision: keep UTC for portability (and document it) or align with LA_TZ. Document the choice either way in `creek/purge/audit.py` module docstring.

## Acceptance criteria

- `grep -rn "datetime\.now()" creek/` returns nothing in production code (acceptable in tests).
- `Fragment().created.tzinfo is not None` is true.
- Re-running `tests/` with `TZ=UTC` and again with `TZ=Asia/Tokyo` produces identical assertions for any timestamp-comparing test.
- The thread-status helper continues to compute days correctly when current time and `last_seen` originate in different timezones.

## References
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §8.3 (timestamp normalization)
- `creek-tools/docs/ingestion.md` "Normalize timestamps to America/Los_Angeles"
- `creek/ingest/base.py:32` (LA_TZ definition)
