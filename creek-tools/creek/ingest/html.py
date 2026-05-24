"""HTML → markdown conversion helper shared across ingestors.

Promoting this out of :mod:`creek.ingest.documents` (where it lived as the
private ``_parse_html_to_markdown``) breaks the import cycle that would
otherwise form between :mod:`creek.ingest.documents` and
:mod:`creek.ingest.substack`: both ingest HTML, and both should import
the converter from a neutral module rather than reaching into each
other's private API.

FEAT-031: this module also hosts :func:`extract_html_authored_at` so
HTML-emitting ingestors share a single extraction chain (Open Graph,
``article:published_time``, ``DC.date``, JSON-LD ``datePublished``)
without each rolling its own regex pile.
"""

from __future__ import annotations

import json
import re

# Regex for a single ``<meta name|property="..." content="...">`` tag.
# We accept either ordering of name/property + content because real-world
# HTML mixes the two; ``re.IGNORECASE | re.DOTALL`` covers multi-line
# tags and arbitrary casing.
_META_TAG = re.compile(
    r"<meta\b[^>]*?\b(?:name|property|itemprop)\s*=\s*[\"']"
    r"(?P<name>[^\"']+)[\"'][^>]*?\bcontent\s*=\s*[\"'](?P<content>[^\"']+)[\"']"
    r"|"
    r"<meta\b[^>]*?\bcontent\s*=\s*[\"'](?P<content2>[^\"']+)[\"'][^>]*?"
    r"\b(?:name|property|itemprop)\s*=\s*[\"'](?P<name2>[^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)

# JSON-LD scripts may carry ``datePublished`` (single object or list).
# We scan the raw script body rather than running a full HTML parser
# because that's enough to recover the publication date and lets the
# module stay stdlib-only.
_JSON_LD_SCRIPT = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

# Meta-tag keys we consider for the FEAT-031 ``authored_at`` chain,
# in precedence order. Open-Graph and ``article:`` carry the truest
# publish dates on Substack / WordPress / Medium / Ghost exports;
# ``DC.date`` and friends cover older / academic sources; ``date``
# is the lowest-fidelity catch-all.
_HTML_AUTHORED_AT_META_KEYS: tuple[str, ...] = (
    "article:published_time",
    "og:published_time",
    "dc.date.issued",
    "dc.date",
    "dcterms.created",
    "dcterms.issued",
    "pubdate",
    "publish-date",
    "date",
)


def parse_html_to_markdown(html: str) -> str:
    """Convert an HTML string to clean ATX-style markdown.

    Uses ``markdownify`` with ATX heading style for consistent output.

    Args:
        html: The HTML content to convert.

    Returns:
        A markdown-formatted string.

    Raises:
        ImportError: If ``markdownify`` is not installed. Install with
            ``pip install creek-tools[documents]``.
    """
    try:
        import markdownify
    except ImportError as exc:
        msg = (
            "markdownify is required for HTML ingestion. "
            "Install with: pip install creek-tools[documents]"
        )
        raise ImportError(msg) from exc
    result: str = markdownify.markdownify(html, heading_style="ATX")
    return result


def _collect_meta_dates(html: str) -> dict[str, str]:
    """Scan ``<meta>`` tags and return a lowercase ``{key: content}`` map.

    Later occurrences of the same key win — matches how a browser would
    treat a duplicated tag — but FEAT-031 callers look up by precedence
    so the ordering rarely matters.
    """
    found: dict[str, str] = {}
    for match in _META_TAG.finditer(html):
        name = (match.group("name") or match.group("name2") or "").strip().lower()
        content = (match.group("content") or match.group("content2") or "").strip()
        if name and content:
            found[name] = content
    return found


def _collect_json_ld_date(html: str) -> str | None:
    """Return the first ``datePublished`` found in any JSON-LD ``<script>``.

    JSON-LD payloads may be a single object or a list (or even nested
    under ``@graph``). We walk the simplest shapes — single object and
    top-level list — because Substack / WordPress / Ghost stick to
    them, and FEAT-031 is fine returning ``None`` for the long-tail
    schemas it does not understand.
    """
    for match in _JSON_LD_SCRIPT.finditer(html):
        body = match.group("body").strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            continue
        candidates: list[object] = (
            [parsed] if isinstance(parsed, dict) else list(parsed)
        )
        for entry in candidates:
            if isinstance(entry, dict):
                value = entry.get("datePublished") or entry.get("dateCreated")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def extract_html_authored_at(html: str) -> str | None:
    """Return the source-side authored date string from HTML metadata.

    FEAT-031 extraction chain (highest precedence first):

    1. ``<meta property="article:published_time">`` /
       ``<meta property="og:published_time">``.
    2. Dublin Core variants (``DC.date.issued``, ``DC.date``,
       ``DCTERMS.created``, ``DCTERMS.issued``).
    3. Generic ``<meta name="pubdate">`` / ``publish-date`` / ``date``.
    4. JSON-LD ``datePublished`` from any ``application/ld+json`` block.

    Returns the raw string for the caller to feed into
    :func:`creek.ingest.base.parse_authored_at` — keeps this module
    free of timezone helpers and avoids an import cycle.
    """
    metas = _collect_meta_dates(html)
    for key in _HTML_AUTHORED_AT_META_KEYS:
        value = metas.get(key)
        if value:
            return value
    return _collect_json_ld_date(html)
