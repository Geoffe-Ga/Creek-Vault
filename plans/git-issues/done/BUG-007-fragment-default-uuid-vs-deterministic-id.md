# BUG-007: `Fragment._generate_frag_id` uses uuid4, conflicting with the deterministic `generate_fragment_id`

**Severity:** High
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1 (idempotency)

## Files affected
- `creek/models.py:215-217` — `_generate_frag_id` returning `uuid.uuid4().hex[:8]`
- `creek/models.py:316` — `Fragment.id = Field(default_factory=_generate_frag_id)`
- `creek/ingest/base.py:226-242` — the *correct* deterministic implementation

## Dependencies
Should be fixed alongside BUG-001 (pipeline) so the chosen ID actually flows through.

## Blockers
None.

## Reproduction
```python
from creek.models import Fragment, FragmentSource, SourcePlatform
f1 = Fragment(title="x", source=FragmentSource(platform=SourcePlatform.OTHER))
f2 = Fragment(title="x", source=FragmentSource(platform=SourcePlatform.OTHER))
print(f1.id, f2.id)        # frag-XXXXXXXX, frag-YYYYYYYY (different)
```

Compare with the deterministic helper:
```python
from creek.ingest.base import generate_fragment_id
from datetime import datetime, UTC
print(generate_fragment_id("a.md", datetime(2025,1,1, tzinfo=UTC), "body"))
# frag-2c9f7a4d... (same on every call)
```

Two different generators with the same name prefix and different ID widths (8 vs 12 hex chars) is a recipe for confusion. The README and `docs/getting-started.md` claim the deterministic version is the system's identity scheme.

## Analysis

The codebase has two parallel ID generators:
1. `creek.ingest.base.generate_fragment_id(source, timestamp, content) -> str` — SHA-256, 12-char prefix, deterministic. This is what the docs promise.
2. `creek.models._generate_frag_id() -> str` — uuid4, 8-char prefix, random. This is what every `Fragment(...)` constructor falls back to when no `id` is passed.

Today, every code path that constructs a `Fragment` *without* explicitly passing an `id` gets a random one. Because of BUG-001, the pipeline never passes the deterministic ID through, so today every fragment in the vault has a uuid4 ID. Idempotency is silently broken.

Even after BUG-001 is fixed, having two ID generators with different prefix widths invites future regressions: someone constructs a `Fragment` somewhere without thinking, gets a uuid4, and the fragment ends up duplicated on the next run.

Confidence: verified.

## Proposed remediation

Pick one strategy and remove the other.

**Recommended:** Keep `generate_fragment_id` as the only generator. Remove the `default_factory` from `Fragment.id` (force callers to supply the ID). For the small number of cases where a fragment is created without an obvious deterministic input (synthesised praxis notes, etc.), provide a clearly-named separate helper `synthetic_fragment_id()` that uses uuid4 *and* sets a marker bit so they're distinguishable.

Apply the same treatment to the other `_generate_*_id` helpers in `models.py` if they're meant to be deterministic too — at minimum, document which are random vs deterministic.

## Acceptance criteria

- `Fragment(title=..., source=...)` without an explicit `id` raises a validation error.
- Every ingestor passes a deterministic ID through to the constructed `Fragment`.
- IDs are uniformly 12 hex chars wide (or whatever you choose, but pick *one*).
- Round-trip test: ingest a small directory twice; the second run produces zero new fragment files.

## References
- `creek-tools/README.md` line 88 ("fragment IDs are deterministic from `(source, timestamp, content)`")
- `creek-tools/docs/ingestion.md` line 78 (`SHA-256(source, timestamp, content)[:12]`)
- `creek/ingest/base.py:226-242`
- `creek/models.py:215-217`
