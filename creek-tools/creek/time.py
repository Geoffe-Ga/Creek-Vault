"""Centralised timezone helpers for the Creek pipeline.

The Creek ontology (§8.3) and ``docs/ingestion.md`` mandate that every
timestamp the pipeline produces is normalised to America/Los_Angeles.
Bare :func:`datetime.now` calls leak the host timezone (or, worse,
produce naive datetimes that fail to compare against tz-aware ones with
``TypeError``), so production code routes through :func:`now_la` /
:func:`today_la` instead. :func:`ensure_aware` is the repair valve for
values that arrive from outside that discipline — persisted frontmatter,
legacy callers — making them safe to compare without moving the clock.

This module is the single, dependency-free home of :data:`LA_TZ`: the
constant is declared here exactly once and imported everywhere else
(#1339). :mod:`creek.ingest.base` is among those importers and
re-exports it, so the historical ``from creek.ingest.base import
LA_TZ`` path some callers still use keeps resolving to this one
definition. The LA anchor is an ontology mandate, not a preference, so
it gets one place to change, audit and reason about — not two constants
that agree until somebody edits one of them.
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

    There are three production callers, at three different layers, and
    together they are what makes "never naive" hold end to end:

    * :class:`~creek.models.Fragment`'s field validator over ``created``
      / ``ingested`` / ``authored_at`` (#976) — the *write*-side
      enforcement point, covering every fragment built through the
      constructor or ``model_validate``, which is what ingest and vault
      reads use. See
      :meth:`~creek.models.Fragment._normalise_timestamp` for the three
      construction paths that bypass it, and why they stay open.
    * :func:`effective_authored_at` — the *read*-side chokepoint that
      covers those bypasses (#1116). Every time-bucketing surface routes
      through it, so a fragment built by ``model_construct`` still sorts
      and compares correctly.
    * :mod:`creek.clean.validator` — anchors the timestamps it checks,
      after #1115 consolidated away a divergent local copy of this
      helper that attached UTC instead of LA.

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
       wrote the fragment.

    **The result is never naive** (#1116). That guarantee is
    unconditional, and it has to be made here rather than inherited from
    the model, because :class:`~creek.models.Fragment`'s
    :meth:`~creek.models.Fragment._normalise_timestamp` validator is
    skipped by three construction paths Pydantic does not route through
    field validators: ``model_construct``, ``model_copy(update=...)``,
    and direct attribute assignment on an existing instance. Those paths
    stay open deliberately — production code depends on them — so the
    repair moves to the read instead. Both returns therefore pass
    through :func:`ensure_aware`, which *attaches* America/Los_Angeles to
    a naive value without moving its wall clock.

    This is the chokepoint that makes the repair worth one line rather
    than thirty: an AST scan of ``creek/`` and ``creek_mcp/`` finds this
    function to be the only production read of ``authored_at`` /
    ``ingested`` that does arithmetic on the value, and roughly thirty
    downstream call sites sort, subtract, ``min`` and ``max`` on its
    result. One naive fragment reaching any of them raised ``TypeError:
    can't compare offset-naive and offset-aware datetimes`` and took the
    whole pass with it.

    :attr:`Fragment.created` is deliberately not in the chain:
    historically it conflated "source authored date" and "filesystem
    mtime" (and frequently defaulted to wall-clock at parse time), so
    bucketing on it produced the misleading behaviour FEAT-031 exists
    to fix — a 2024 essay registering as part of *this week's* vault
    activity. ``ingested`` is the honest fallback because it answers
    a different question ("when did Creek see this?") without
    pretending to know the authored date. Its exclusion also keeps this
    function clear of the id-hashing contract: ``ingest/pin_ids.py``
    deliberately hashes the *raw* frontmatter ``created`` rather than
    ``Fragment.created``, so nothing here may re-anchor it.

    Args:
        fragment: The fragment to bucket.

    Returns:
        ``authored_at`` when populated, otherwise ``ingested`` — in
        either case tz-aware, with its wall clock and microseconds
        intact.
    """
    if fragment.authored_at is not None:
        return ensure_aware(fragment.authored_at)
    return ensure_aware(fragment.ingested)


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
