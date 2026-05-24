"""Shared authored-date extraction helpers (FEAT-031 / #263).

Every ingestor must surface the source's *authored* date as
:attr:`creek.models.Fragment.authored_at` — separately from the
``created`` and ``ingested`` fields which describe when the vault saw
the bytes. Each ingestor has its own fallback chain (frontmatter,
EXIF, ``posts.csv``, git, …), but they all share two operations:

* **Parse an opaque scalar** (string, ``date``, ``datetime``) into a
  *timezone-aware* :class:`datetime.datetime` — naive values are
  anchored to UTC per the issue's "UTC otherwise; never naive" rule
  rather than localised to LA (which would silently shift the
  calendar date for date-only sources).
* **Honestly report absence** when nothing is extractable: ``None`` is
  the contracted value, never a guess.

These helpers live here so the ingestor modules don't accumulate
near-duplicate copies that drift apart.
"""

from __future__ import annotations

import logging
from datetime import UTC, date as date_cls, datetime
from typing import Any

from creek.ingest.base import file_modified_time

logger = logging.getLogger(__name__)


_FALLBACK_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)
"""Strict ``datetime.strptime`` patterns tried after ISO 8601 parsing fails.

Order matters: more specific formats come first so a leading
``YYYY-MM-DD HH:MM:SS`` does not get re-parsed as a date-only value
that drops the time component.
"""


def parse_authored_value(value: Any) -> datetime | None:
    """Parse an opaque date value into a timezone-aware datetime.

    The input may be a :class:`datetime`, :class:`date`, ISO 8601
    string, or one of the common fallback patterns in
    :data:`_FALLBACK_DATETIME_FORMATS`. Unparseable values return
    ``None`` (the caller decides whether to fall back further or to
    surface :class:`Fragment.authored_at` as ``None``).

    Naive results are anchored to UTC rather than localised to LA so
    a bare ``"2024-03-15"`` survives as the 15th in UTC instead of
    drifting back to the 14th in LA local time. Sources that *do*
    carry timezone info (``"2024-03-15T08:00:00+02:00"``) are
    preserved verbatim — we never re-zone a tz-aware input.

    Args:
        value: The frontmatter/JSON/metadata field to parse. ``None``
            and empty strings short-circuit to ``None``.

    Returns:
        A timezone-aware :class:`datetime`, or ``None`` if unparseable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date_cls):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, int | float):
        return _epoch_to_utc(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return _parse_string_value(text)


def _parse_string_value(text: str) -> datetime | None:
    """Try ISO 8601 then the strict fallback patterns; anchor naive to UTC."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = _try_fallback_formats(text)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _try_fallback_formats(text: str) -> datetime | None:
    """Return the first :data:`_FALLBACK_DATETIME_FORMATS` match, or ``None``."""
    for fmt in _FALLBACK_DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007 — anchored by caller
        except ValueError:
            continue
    return None


def _epoch_to_utc(epoch: float) -> datetime | None:
    """Convert a Unix-epoch number to a UTC datetime, or ``None`` on ``0``.

    The ``0.0`` sentinel is what ChatGPT/Claude exports use when a
    message lacks a real creation time. Treating it as 1970-01-01 would
    poison time-bucket reports, so we surface it as ``None``.
    """
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def safe_file_modified_time(path: Any) -> datetime | None:
    """Return ``file_modified_time(path)`` or ``None`` when unreadable.

    Synthetic :class:`~creek.ingest.base.RawDocument` instances in
    tests (and a vanished file mid-run) can produce ``OSError`` from
    ``Path.stat`` — the honest answer per FEAT-031 is ``None``, not a
    fabricated date.
    """
    try:
        return file_modified_time(path)
    except OSError:
        return None


def frontmatter_authored_at(
    fm_data: dict[str, Any],
    keys: tuple[str, ...],
    file_path: Any,
    *,
    source_label: str = "ingestor",
) -> datetime | None:
    """Run a frontmatter fallback chain, ending in the filesystem mtime.

    Walks ``keys`` in order; the first key whose value parses cleanly
    wins. If a key is present but unparseable, the chain *stops* (we do
    not silently skip a corrupt explicit date in favour of a less
    authoritative one) and the filesystem mtime is consulted instead.

    Args:
        fm_data: Parsed frontmatter dictionary.
        keys: Ordered tuple of frontmatter keys to consult.
        file_path: Source file path for the mtime fallback.
        source_label: Ingestor name used in warning messages so a stray
            log line is traceable.

    Returns:
        A timezone-aware datetime, or ``None`` when nothing is extractable.
    """
    for key in keys:
        if key not in fm_data:
            continue
        parsed = parse_authored_value(fm_data[key])
        if parsed is not None:
            return parsed
        logger.warning(
            "%s: unparseable %s=%r in frontmatter of %s",
            source_label,
            key,
            fm_data[key],
            file_path,
        )
        break
    return safe_file_modified_time(file_path)
