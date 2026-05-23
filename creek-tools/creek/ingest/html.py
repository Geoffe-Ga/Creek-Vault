"""HTML → markdown conversion helper shared across ingestors.

Promoting this out of :mod:`creek.ingest.documents` (where it lived as the
private ``_parse_html_to_markdown``) breaks the import cycle that would
otherwise form between :mod:`creek.ingest.documents` and
:mod:`creek.ingest.substack`: both ingest HTML, and both should import
the converter from a neutral module rather than reaching into each
other's private API.
"""

from __future__ import annotations


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
