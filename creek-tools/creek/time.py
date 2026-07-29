"""Centralised timezone helpers for the Creek pipeline.

The Creek ontology (§8.3) and ``docs/ingestion.md`` mandate that every
timestamp the pipeline produces is normalised to America/Los_Angeles.
Bare :func:`datetime.now` calls leak the host timezone (or, worse,
produce naive datetimes that fail to compare against tz-aware ones with
``TypeError``), so production code routes through :func:`now_la` /
:func:`today_la` instead. :func:`ensure_aware` is the repair valve for
values that arrive from outside that discipline — persisted frontmatter,
legacy callers — making them safe to compare without moving the clock.

The constant :data:`LA_TZ` is re-exported from
:mod:`creek.ingest.base` to preserve the historical import path while
giving callers a dependency-free home for the helper.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from creek.models import Fragment

LA_TZ = ZoneInfo("America/Los_Angeles")
"""Target timezone for all normalized timestamps (America/Los_Angeles)."""


def now_la() -> datetime:
    """Return the current time as a tz-aware datetime in America/Los_Angeles."""
    return datetime.now(tz=LA_TZ)


def today_la() -> date:
    """Return today's date as observed in America/Los_Angeles."""
    return now_la().date()


def ensure_aware(value: datetime) -> datetime:
    """Return *value* guaranteed comparable against other aware datetimes.

    Comparing a naive datetime with a tz-aware one raises ``TypeError:
    can't compare offset-naive and offset-aware datetimes``, which is the
    failure mode this module exists to prevent. Callers that mix a
    pipeline-generated clock with timestamps read back off disk route
    through this helper first.

    The contract is deliberately asymmetric:

    * **Aware in, untouched out.** The instant, the ``tzinfo`` object and
      the microseconds all survive — no normalisation to LA or UTC.
      A fragment authored in Sydney keeps its Sydney rendering, matching
      the promise :func:`effective_authored_at` already makes to callers
      that display source-local time.
    * **Naive in, LA attached — never converted.** Every wall-clock field
      is preserved; only the missing offset is filled in. LA is the
      right assumption because the ontology (§8.3) normalises every
      timestamp this pipeline writes to LA, so a naive value is an LA
      wall-clock reading that lost its offset in transit — typically
      through YAML frontmatter serialised without one. Converting rather
      than attaching would shift such a value by the LA offset and could
      move it across a day boundary.

    Args:
        value: A datetime that may be naive or timezone-aware.

    Returns:
        *value* unchanged when it is already aware, otherwise the same
        wall clock anchored to :data:`LA_TZ`.
    """
    # ``datetime.utcoffset()`` returns ``None`` for exactly the values
    # CPython treats as naive when comparing: ``tzinfo is None``, plus the
    # rarer ``tzinfo`` whose own ``utcoffset`` yields ``None``. Both need
    # the anchor, so test the offset rather than ``tzinfo`` itself.
    if value.utcoffset() is not None:
        return value
    return value.replace(tzinfo=LA_TZ)


def effective_authored_at(fragment: Fragment) -> datetime:
    """Return the datetime a fragment should be time-bucketed under (FEAT-031).

    Single source of truth for the authored-date precedence pinned by
    FEAT-031: every downstream surface that buckets fragments by time —
    the State report, the wavelength tracker, the temporal linker, any
    future "what changed this week" view — MUST route through this
    helper so the precedence stays consistent.

    Precedence (highest to lowest):

    1. :attr:`Fragment.authored_at` when the ingestor extracted a
       source-side timestamp (a Substack post's ``post_date``, a
       Discord message's ``timestamp``, an EXIF ``DateTimeOriginal``).
    2. :attr:`Fragment.ingested` — the wall-clock moment the vault
       wrote the fragment. Always present, never naive.

    :attr:`Fragment.created` is deliberately not in the chain:
    historically it conflated "source authored date" and "filesystem
    mtime" (and frequently defaulted to wall-clock at parse time), so
    bucketing on it produced the misleading behaviour FEAT-031 exists
    to fix — a 2024 essay registering as part of *this week's* vault
    activity. ``ingested`` is the honest fallback because it answers
    a different question ("when did Creek see this?") without
    pretending to know the authored date.

    Args:
        fragment: The fragment to bucket.

    Returns:
        ``authored_at`` when populated, otherwise ``ingested``.
    """
    if fragment.authored_at is not None:
        return fragment.authored_at
    return fragment.ingested


def effective_authored_date(fragment: Fragment) -> date:
    """Return the date a fragment should be time-bucketed under (FEAT-031).

    Convenience wrapper around :func:`effective_authored_at` for
    callers that only need the date (week / month / year aggregates).
    See :func:`effective_authored_at` for the precedence contract.

    Args:
        fragment: The fragment to bucket.

    Returns:
        The local date component of the effective authored datetime.
    """
    return effective_authored_at(fragment).date()
