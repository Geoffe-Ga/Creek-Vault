"""Substack-aware ingestor for the Creek ingest pipeline.

A Substack export bundles a ``posts.csv`` metadata sidecar alongside per-post
``<post_id>.<slug>.html`` files. The generic :class:`DocumentIngestor` treats
each HTML file as opaque, throws away the real publication date, and routes
the result to ``01-Fragments/Unsorted/`` — actively misleading the State
report (which then claims a 2024 essay is part of *this week's* wavelength)
and burying published essays alongside private chat fragments.

This ingestor:

* auto-detects a Substack export by the presence of ``posts.csv`` + at least
  one matching per-post HTML file,
* parses ``posts.csv`` once and maps each post ID (the leading numeric
  component of the HTML filename) to its publication date, title, subtitle,
  audience, and any other useful columns,
* emits a fragment per HTML post with
  ``source.platform = SUBSTACK`` / ``source.kind = WRITING`` and
  ``authored_at`` populated from ``posts.csv`` (never the filesystem
  date),
* explicitly skips ``email_list*.csv``, ``*.delivers.csv``, and
  ``*.opens.csv`` — subscriber PII that has no place in the vault.

Re-running the ingestor against an already-ingested export is idempotent:
fragment IDs derive from ``(source_path, authored_at, content)`` so
identical inputs produce identical IDs and the vault writer's per-directory
ID index recognises and skips duplicates.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from creek.ingest._detection import (
    POSTS_CSV_FILENAME,
    extract_post_id,
    is_substack_export,
)
from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    file_modified_time,
    normalize_encoding,
    parse_authored_at,
)
from creek.ingest.html import parse_html_to_markdown
from creek.models import SourceKind, SourcePlatform

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


# ---- Public re-exports ----

# Detection helpers live in ``creek.ingest._detection`` so
# ``creek.ingest.documents`` can import them without forming a cycle
# with this module. They are re-exported here for the existing
# substack-facing test surface and for downstream callers that want a
# single "all things Substack" import location.
__all__ = [
    "POSTS_CSV_FILENAME",
    "SUBSCRIBER_CSV_SUFFIXES",
    "SUBSCRIBER_LIST_PREFIX",
    "SubstackIngestor",
    "is_substack_export",
]


# ---- Constants ----

SUBSCRIBER_CSV_SUFFIXES: frozenset[str] = frozenset(
    {".delivers.csv", ".opens.csv"},
)
"""Filename suffixes for per-post subscriber-engagement CSVs.

These files record which subscribers received or opened each post and
are pure PII. They MUST NOT produce fragments. The check is suffix-based
because Substack names them ``<post_id>.<slug>.delivers.csv`` /
``<post_id>.<slug>.opens.csv``.
"""

SUBSCRIBER_LIST_PREFIX = "email_list"
"""Filename prefix for the top-level subscriber list CSV (also PII)."""

# Columns ``_parse_posts_csv`` will surface verbatim. Anything else in
# the CSV is ignored — Substack's export schema has drifted over the
# years and a future column we don't recognise should not break ingest.
_KNOWN_POSTS_COLUMNS: frozenset[str] = frozenset(
    {
        "post_id",
        "post_date",
        "title",
        "subtitle",
        "type",
        "audience",
        "is_published",
        "is_public",
        "podcast_url",
    },
)


# ---- Helper functions ----


# Backwards-compatible alias for the test surface that imports
# ``_extract_post_id`` from this module by name.
_extract_post_id = extract_post_id


# Optional ``posts.csv`` columns whose values are surfaced verbatim
# into the fragment frontmatter when present. Driving the assignment
# from a table keeps :meth:`SubstackIngestor.generate_frontmatter`
# below the project's cyclomatic-complexity ceiling and makes adding
# a new column a one-line change.
_OPTIONAL_FRONTMATTER_FIELDS: tuple[str, ...] = (
    "subtitle",
    "audience",
    "podcast_url",
)


def _merge_optional_fields(
    frontmatter_dict: dict[str, Any],
    row: dict[str, str],
    post_id: object,
) -> None:
    """Copy non-empty optional Substack columns into *frontmatter_dict*.

    Empty / whitespace-only values are skipped so the frontmatter does
    not carry blank keys that downstream consumers would have to
    null-check. ``post_id`` is treated separately because its source
    is the parsed-fragment metadata, not the CSV row.
    """
    for key in _OPTIONAL_FRONTMATTER_FIELDS:
        value = (row.get(key) or "").strip()
        if value:
            frontmatter_dict[key] = value
    if post_id:
        frontmatter_dict["substack_post_id"] = post_id


def _parse_posts_csv(csv_path: Path) -> dict[str, dict[str, str]]:
    """Parse ``posts.csv`` into ``{post_id: {column: value}}``.

    Unknown columns are dropped (Substack's export schema drifts over
    the years). Rows without a ``post_id`` are skipped — the value is
    the join key, and a row that cannot be joined is useless to the
    ingestor. A missing file yields an empty dict so the caller can
    treat "no metadata" as a recoverable state.
    """
    if not csv_path.is_file():
        return {}
    metadata: dict[str, dict[str, str]] = {}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            post_id = (row.get("post_id") or "").strip()
            if not post_id:
                continue
            metadata[post_id] = {
                key: value
                for key, value in row.items()
                if key in _KNOWN_POSTS_COLUMNS and value is not None
            }
    return metadata


def _is_subscriber_csv(path: Path) -> bool:
    """Return whether *path* is a subscriber-engagement CSV (PII).

    Covers both layouts: per-post ``<post_id>.<slug>.delivers.csv`` /
    ``.opens.csv`` and the top-level ``email_list*.csv`` roster.
    """
    name = path.name.lower()
    if name.startswith(SUBSCRIBER_LIST_PREFIX) and name.endswith(".csv"):
        return True
    return any(name.endswith(suffix) for suffix in SUBSCRIBER_CSV_SUFFIXES)


def _resolve_authored_at(
    row: dict[str, str] | None,
    file_path: Path,
) -> tuple[datetime | None, datetime]:
    """Return ``(authored_at, timestamp)`` for a Substack post.

    ``authored_at`` is the post's true publication date from
    ``posts.csv``, or ``None`` when no CSV row matches the HTML file
    (the honest answer — never guess).

    ``timestamp`` is what the base-class pipeline feeds into the
    deterministic fragment-ID hash. We use ``authored_at`` when we have
    one so re-running the ingestor against the same export produces
    identical IDs (the writer's ID index then skips the duplicate),
    and the filesystem mtime otherwise — the same fallback every other
    Creek ingestor uses for an undated source.
    """
    raw_date = (row or {}).get("post_date", "").strip()
    if raw_date:
        try:
            authored = parse_authored_at(raw_date)
        except ValueError:
            logger.warning(
                "Substack posts.csv row for %s has unparseable post_date %r",
                file_path,
                raw_date,
            )
        else:
            if authored is not None:
                return authored, authored
    return None, file_modified_time(file_path)


# ---- SubstackIngestor ----


class SubstackIngestor(Ingestor):
    """Ingestor for Substack newsletter exports.

    Discovers ``posts.csv`` once at the export root, recursively walks
    the directory for per-post ``<post_id>.<slug>.html`` files, and
    produces one fragment per post with publication-date-aware metadata.

    Subscriber-engagement CSVs are filtered explicitly (see
    :data:`SUBSCRIBER_CSV_SUFFIXES`) and never reach the parse stage,
    so subscriber PII cannot escape into the vault even by accident.
    """

    def __init__(self) -> None:
        """Initialise the ingestor with empty per-run state.

        ``_posts_metadata`` is the parsed ``posts.csv`` mapping; it is
        populated during :meth:`discover` so :meth:`parse` can join
        each HTML file back to its publication date in O(1) without
        re-reading the CSV per post.
        """
        self._posts_metadata: dict[str, dict[str, str]] = {}

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find post HTML files inside a Substack export directory.

        The export root must contain ``posts.csv``; otherwise the
        ingestor refuses to emit anything (auto-detection failed and
        the caller likely meant ``--type document``). Subscriber-PII
        CSVs are filtered here so the parse stage cannot see them.

        ``_posts_metadata`` is reset unconditionally on every call so a
        reused ingestor instance cannot leak the previous export's
        rows into a later parse — defensive against the (rare) caller
        that runs the same ingestor against several sources in sequence.
        """
        self._posts_metadata = {}

        if not source_path.exists() or not source_path.is_dir():
            return []

        csv_path = source_path / POSTS_CSV_FILENAME
        if not csv_path.is_file():
            return []

        self._posts_metadata = _parse_posts_csv(csv_path)

        docs: list[RawDocument] = []
        for html_file in sorted(source_path.rglob("*.html")):
            if not html_file.is_file():
                continue
            # Paranoia — an HTML extension shouldn't match the subscriber
            # filter, but a future export-layout change could; keep the
            # PII guard at the discover boundary regardless.
            if _is_subscriber_csv(html_file):
                continue
            post_id = extract_post_id(html_file.name)
            if post_id is None:
                logger.debug(
                    "Substack HTML %s has no leading post_id; skipping",
                    html_file,
                )
                continue
            docs.append(self._read_html(html_file, post_id))
        return docs

    def _read_html(self, file_path: Path, post_id: str) -> RawDocument:
        """Read one post HTML into a :class:`RawDocument`."""
        raw_bytes = file_path.read_bytes()
        _text, encoding = normalize_encoding(raw_bytes)
        return RawDocument(
            path=file_path,
            content=raw_bytes,
            metadata={
                "source_type": "substack",
                "post_id": post_id,
            },
            detected_encoding=encoding,
        )

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Parse one post HTML, joining it against the cached ``posts.csv`` row.

        The fragment's ``timestamp`` is the publication date when
        available so the base class's deterministic fragment-ID hash is
        stable across re-ingests — the idempotency contract.
        """
        post_id = str(raw.metadata.get("post_id", ""))
        row = self._posts_metadata.get(post_id)
        authored_at, timestamp = _resolve_authored_at(row, raw.path)

        text, encoding = normalize_encoding(raw.content)
        markdown_body = parse_html_to_markdown(text)

        return [
            ParsedFragment(
                content=markdown_body,
                metadata={
                    "post_id": post_id,
                    "post_row": dict(row) if row else {},
                    "authored_at": authored_at,
                    "source_encoding": encoding,
                },
                source_path=str(raw.path),
                timestamp=timestamp,
            ),
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Return the already-converted markdown body."""
        return fragment.content

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Build Creek frontmatter with Substack-aware fields.

        Sets ``source.platform = SUBSTACK`` / ``source.kind = WRITING``
        and ``authored_at`` from ``posts.csv``. Pulls the title and any
        optional metadata (subtitle, audience, podcast URL) from the
        cached row when available; falls back to the HTML filename
        stem when the row is missing.
        """
        row = fragment.metadata.get("post_row") or {}
        title = (row.get("title") or "").strip() or Path(fragment.source_path).stem

        frontmatter_dict: dict[str, Any] = {
            "type": "fragment",
            "title": title,
            "source": {
                "platform": SourcePlatform.SUBSTACK,
                "kind": SourceKind.WRITING,
                "original_file": fragment.source_path,
                "original_encoding": fragment.metadata.get("source_encoding", "utf-8"),
            },
            "created": fragment.timestamp.isoformat(),
        }

        authored_at: datetime | None = fragment.metadata.get("authored_at")
        if authored_at is not None:
            frontmatter_dict["authored_at"] = authored_at.isoformat()

        _merge_optional_fields(frontmatter_dict, row, fragment.metadata.get("post_id"))
        return frontmatter_dict
