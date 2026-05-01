"""Property-based tests for ``creek.ingest.base.generate_fragment_id``.

The fragment ID is a public contract: any (source, timestamp, content)
triple maps deterministically to a stable 12-hex-char digest prefixed
with ``frag-``. Hypothesis fuzzes that contract across the input space
that handwritten unit tests are unlikely to enumerate.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from creek.ingest.base import generate_fragment_id

# Property-based tests can be slow to enumerate; cap examples and disable
# the deadline so CI's noisy timing doesn't flake the suite. 200 examples
# completes in well under 30s per the TEST-005 acceptance criterion.
_PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_TZ_AWARE_DATETIMES = st.datetimes(
    timezones=st.just(UTC),
    min_value=datetime(1970, 1, 1),
    max_value=datetime(2100, 1, 1),
)


@_PROFILE
@given(
    source=st.text(min_size=1, max_size=200),
    ts=_TZ_AWARE_DATETIMES,
    content=st.text(min_size=1, max_size=2000),
)
def test_fragment_id_is_deterministic(source: str, ts: datetime, content: str) -> None:
    """Same inputs must always yield the same ID."""
    a = generate_fragment_id(source, ts, content)
    b = generate_fragment_id(source, ts, content)
    assert a == b


@_PROFILE
@given(
    source=st.text(min_size=1, max_size=200),
    ts=_TZ_AWARE_DATETIMES,
    content=st.text(min_size=1, max_size=2000),
)
def test_fragment_id_has_canonical_shape(
    source: str, ts: datetime, content: str
) -> None:
    """ID must be ``frag-`` plus exactly 12 lowercase hex characters."""
    fid = generate_fragment_id(source, ts, content)
    assert fid.startswith("frag-")
    digest = fid.removeprefix("frag-")
    assert len(digest) == 12
    assert all(c in "0123456789abcdef" for c in digest)


@_PROFILE
@given(
    source_a=st.text(min_size=1, max_size=50),
    source_b=st.text(min_size=1, max_size=50),
    ts=_TZ_AWARE_DATETIMES,
    content=st.text(min_size=1, max_size=200),
)
def test_fragment_id_changes_with_source(
    source_a: str, source_b: str, ts: datetime, content: str
) -> None:
    """Distinct sources should yield distinct IDs (with overwhelming probability).

    A 12-hex digest has 16**12 ≈ 2.8e14 buckets; collisions on hand-rolled
    inputs are vanishingly unlikely. We accept Hypothesis-generated input
    pairs that happen to be identical (no semantic difference).
    """
    if source_a == source_b:
        return
    a = generate_fragment_id(source_a, ts, content)
    b = generate_fragment_id(source_b, ts, content)
    assert a != b


@_PROFILE
@given(
    source=st.text(min_size=1, max_size=50),
    ts_a=_TZ_AWARE_DATETIMES,
    ts_b=_TZ_AWARE_DATETIMES,
    content=st.text(min_size=1, max_size=200),
)
def test_fragment_id_changes_with_timestamp(
    source: str, ts_a: datetime, ts_b: datetime, content: str
) -> None:
    """Distinct timestamps should yield distinct IDs."""
    if ts_a == ts_b:
        return
    a = generate_fragment_id(source, ts_a, content)
    b = generate_fragment_id(source, ts_b, content)
    assert a != b


@_PROFILE
@given(
    source=st.text(min_size=1, max_size=50),
    ts=_TZ_AWARE_DATETIMES,
    content_a=st.text(min_size=1, max_size=200),
    content_b=st.text(min_size=1, max_size=200),
)
def test_fragment_id_changes_with_content(
    source: str, ts: datetime, content_a: str, content_b: str
) -> None:
    """Distinct content should yield distinct IDs."""
    if content_a == content_b:
        return
    a = generate_fragment_id(source, ts, content_a)
    b = generate_fragment_id(source, ts, content_b)
    assert a != b


def test_fragment_id_unicode_smoke() -> None:
    """Concrete unicode-heavy regression case for the property tests above."""
    ts = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    content = "résumé café 日本語 🌊"
    fid = generate_fragment_id("source", ts, content)
    assert fid.startswith("frag-")
    assert len(fid.removeprefix("frag-")) == 12


def test_fragment_id_naive_timestamp_diverges_from_aware() -> None:
    """Naive vs aware datetimes serialise differently and must yield distinct IDs.

    This guards against a regression where ``timestamp.isoformat()`` was
    replaced by a naive ``str(timestamp)`` — the contract distinguishes
    timezone-aware events from naive ones.
    """
    naive = datetime(2026, 4, 28, 12, 0)
    aware = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    # naive.isoformat() = "2026-04-28T12:00:00";
    # aware.isoformat() = "2026-04-28T12:00:00+00:00".
    # They differ by construction — no defensive skip needed.
    a = generate_fragment_id("s", naive, "c")
    b = generate_fragment_id("s", aware, "c")
    assert a != b
