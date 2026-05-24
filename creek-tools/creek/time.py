"""Centralised timezone helpers for the Creek pipeline.

The Creek ontology (§8.3) and ``docs/ingestion.md`` mandate that every
timestamp the pipeline produces is normalised to America/Los_Angeles.
Bare :func:`datetime.now` calls leak the host timezone (or, worse,
produce naive datetimes that fail to compare against tz-aware ones with
``TypeError``), so production code routes through :func:`now_la` /
:func:`today_la` instead.

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
