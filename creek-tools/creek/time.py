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
from zoneinfo import ZoneInfo

LA_TZ = ZoneInfo("America/Los_Angeles")
"""Target timezone for all normalized timestamps (America/Los_Angeles)."""


def now_la() -> datetime:
    """Return the current time as a tz-aware datetime in America/Los_Angeles."""
    return datetime.now(tz=LA_TZ)


def today_la() -> date:
    """Return today's date as observed in America/Los_Angeles."""
    return now_la().date()
