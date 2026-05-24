"""``creek ingest --refresh-dates`` backfill engine (FEAT-031 / #263).

Walks every fragment in a vault, re-runs *only* the ``authored_at``
extraction step against the original source file, and writes the
updated date back into the fragment's frontmatter. Body content is
left untouched — this is purely a date refresh, not a re-ingest.

The operation is idempotent: a second run against the same vault
extracts the same dates and writes nothing because the on-disk value
already matches.

Per-platform extractors live in their owning ingestor modules; this
module routes by :attr:`Fragment.source.platform` and applies the
correct fallback chain for each:

* DOCUMENT (.html/.pdf/.docx/.rtf/.txt) →
  :func:`creek.ingest.documents._resolve_document_authored_at`
* MARKDOWN / JOURNAL / ESSAY →
  :func:`creek.ingest._authored_at.frontmatter_authored_at`
* CODE → :func:`creek.ingest.code._resolve_authored_at`
* IMAGE_OCR → :func:`creek.ingest.images._resolve_image_authored_at`
* SPREADSHEET → :func:`creek.ingest.spreadsheets._resolve_spreadsheet_authored_at`
* PRESENTATION → OOXML core properties then mtime
* CHATGPT / CLAUDE / DISCORD / SUBSTACK → source-file mtime fallback
  only (per-message extraction requires re-parsing the export, which
  is out of scope for a one-shot backfill — re-ingest those sources
  through the normal pipeline for per-turn fidelity).
* All other platforms → filesystem mtime.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.ingest._authored_at import ooxml_authored_at, safe_file_modified_time
from creek.models import Fragment, SourcePlatform
from creek.vault.reader import try_load_fragment

if TYPE_CHECKING:
    from datetime import datetime

PlatformAuthoredAtExtractor = Callable[[Path], "datetime | None"]
"""Signature shared by every per-platform ``authored_at`` extractor."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshOutcome:
    """Per-fragment outcome of a ``--refresh-dates`` pass.

    Attributes:
        fragment_path: Path to the fragment markdown file.
        previous: The previous ``authored_at`` value (``None`` when
            the field was absent).
        new: The newly extracted value (``None`` when the source is
            unreadable or has no extractable date).
        changed: Whether the file was rewritten.
        reason: Short human-readable status string. One of
            ``"updated"``, ``"unchanged"``, ``"no-source"``,
            ``"unsupported"``, or ``"error"``.
    """

    fragment_path: Path
    previous: datetime | None
    new: datetime | None
    changed: bool
    reason: str


@dataclass(frozen=True)
class RefreshSummary:
    """Aggregate summary returned by :func:`refresh_vault_authored_at`.

    Attributes:
        outcomes: Per-fragment outcomes in walk order.
        total: Number of fragments inspected.
        updated: Number of fragments rewritten.
        unchanged: Number of fragments where the value already matched.
        missing_source: Number of fragments whose source file was absent.
        errors: Number of fragments that failed to load or refresh.
    """

    outcomes: list[RefreshOutcome]
    total: int
    updated: int
    unchanged: int
    missing_source: int
    errors: int


def refresh_vault_authored_at(vault_path: Path) -> RefreshSummary:
    """Backfill ``authored_at`` for every fragment under *vault_path*.

    Walks ``<vault_path>/01-Fragments`` recursively, re-runs the
    appropriate per-platform extractor against the original source
    file, and rewrites the markdown only when the value changed.
    Bodies are preserved byte-for-byte (the body string from the
    loaded frontmatter is fed straight back to
    :class:`frontmatter.Post`).

    Args:
        vault_path: Root of the Obsidian vault to refresh.

    Returns:
        A :class:`RefreshSummary` describing what changed.
    """
    fragments_root = vault_path / "01-Fragments"
    outcomes: list[RefreshOutcome] = []
    if not fragments_root.exists():
        return RefreshSummary(
            outcomes=[],
            total=0,
            updated=0,
            unchanged=0,
            missing_source=0,
            errors=0,
        )

    for md_file in sorted(fragments_root.rglob("*.md")):
        outcome = _refresh_one(md_file)
        if outcome is not None:
            outcomes.append(outcome)
    return _summarise(outcomes)


def _refresh_one(md_file: Path) -> RefreshOutcome | None:
    """Refresh ``authored_at`` on one fragment file, or ``None`` for non-fragments."""
    try:
        loaded = try_load_fragment(md_file)
    except (OSError, ValueError, yaml.YAMLError):
        logger.warning("Could not read fragment for refresh: %s", md_file)
        return RefreshOutcome(
            fragment_path=md_file,
            previous=None,
            new=None,
            changed=False,
            reason="error",
        )
    if loaded is None:
        return None
    fragment, body, raw_metadata = loaded
    new_value = extract_authored_at_for_fragment(fragment)
    if new_value is None is fragment.source.original_file:
        return RefreshOutcome(
            fragment_path=md_file,
            previous=fragment.authored_at,
            new=None,
            changed=False,
            reason="no-source",
        )
    if new_value == fragment.authored_at:
        return RefreshOutcome(
            fragment_path=md_file,
            previous=fragment.authored_at,
            new=new_value,
            changed=False,
            reason="unchanged",
        )
    _rewrite_with_new_authored_at(md_file, body, raw_metadata, new_value)
    return RefreshOutcome(
        fragment_path=md_file,
        previous=fragment.authored_at,
        new=new_value,
        changed=True,
        reason="updated",
    )


def extract_authored_at_for_fragment(fragment: Fragment) -> datetime | None:
    """Return the freshly-extracted ``authored_at`` for a stored fragment.

    Dispatches by :attr:`Fragment.source.platform` to the per-ingestor
    extraction chain. Returns ``None`` when the source file does not
    exist or the platform has no file-based extractor (e.g. chat
    exports whose authored date lives inside a JSON message blob and
    cannot be located without re-parsing the whole conversation —
    these surface as ``None`` from the ``mtime``-only branch unless a
    source path is recorded).

    Args:
        fragment: The fragment whose ``authored_at`` should be refreshed.

    Returns:
        A timezone-aware datetime, or ``None`` when the source cannot
        be located or carries no extractable date.
    """
    original = fragment.source.original_file
    if not original:
        return None
    source_path = Path(original)
    if not source_path.exists():
        return None
    extractor = _PLATFORM_EXTRACTORS.get(SourcePlatform(fragment.source.platform))
    if extractor is None:
        return safe_file_modified_time(source_path)
    return extractor(source_path)


def _document_extractor(path: Path) -> datetime | None:
    """Refresh extractor for HTML/PDF/DOCX/RTF/TXT files."""
    from creek.ingest.base import normalize_encoding
    from creek.ingest.documents import (
        _extract_pdf_metadata,
        _resolve_document_authored_at,
    )

    raw = path.read_bytes()
    text, encoding = normalize_encoding(raw)
    file_type = path.suffix.lower()
    metadata: dict[str, object] = {
        "file_type": file_type,
        "source_encoding": encoding,
    }
    if file_type == ".pdf":
        metadata.update(_extract_pdf_metadata(raw))
    elif file_type == ".docx":
        from creek.ingest.documents import _extract_docx_metadata

        metadata.update(_extract_docx_metadata(raw))
    return _resolve_document_authored_at(file_type, raw, text, metadata, path)


def _markdown_extractor(path: Path) -> datetime | None:
    """Refresh extractor for ``.md`` files (frontmatter dates → mtime)."""
    from creek.ingest._authored_at import frontmatter_authored_at
    from creek.ingest.markdown import _AUTHORED_AT_FRONTMATTER_KEYS

    post = frontmatter.load(str(path))
    return frontmatter_authored_at(
        post.metadata,
        _AUTHORED_AT_FRONTMATTER_KEYS,
        path,
        source_label="MarkdownIngestor",
    )


def _code_extractor(path: Path) -> datetime | None:
    """Refresh extractor for code files (git first-commit then mtime)."""
    from creek.ingest.code import _resolve_authored_at

    return _resolve_authored_at(path)


def _image_extractor(path: Path) -> datetime | None:
    """Refresh extractor for images (EXIF then mtime)."""
    from creek.ingest.images import _resolve_image_authored_at

    return _resolve_image_authored_at(path)


def _spreadsheet_extractor(path: Path) -> datetime | None:
    """Refresh extractor for spreadsheets (OOXML core then mtime)."""
    from creek.ingest.spreadsheets import _resolve_spreadsheet_authored_at

    return _resolve_spreadsheet_authored_at(path)


def _presentation_extractor(path: Path) -> datetime | None:
    """Refresh extractor for presentations (OOXML core then mtime)."""
    return ooxml_authored_at(path) or safe_file_modified_time(path)


def _conversation_extractor(path: Path) -> datetime | None:
    """Refresh extractor for chat/conversation exports.

    Per-message extraction would require re-parsing the entire JSON
    export and matching by message ID — out of scope for a one-shot
    backfill. Falls back to the source-file mtime so multi-year chat
    logs at least get a date that's better than ``None`` when no
    per-turn date was extracted at ingest time.
    """
    return safe_file_modified_time(path)


_PLATFORM_EXTRACTORS: dict[SourcePlatform, PlatformAuthoredAtExtractor] = {
    SourcePlatform.DOCUMENT: _document_extractor,
    SourcePlatform.MARKDOWN: _markdown_extractor,
    SourcePlatform.JOURNAL: _markdown_extractor,
    SourcePlatform.ESSAY: _markdown_extractor,
    SourcePlatform.CODE: _code_extractor,
    SourcePlatform.IMAGE_OCR: _image_extractor,
    SourcePlatform.SPREADSHEET: _spreadsheet_extractor,
    SourcePlatform.PRESENTATION: _presentation_extractor,
    SourcePlatform.SUBSTACK: _document_extractor,
    SourcePlatform.CHATGPT: _conversation_extractor,
    SourcePlatform.CLAUDE: _conversation_extractor,
    SourcePlatform.DISCORD: _conversation_extractor,
    SourcePlatform.EMAIL: _conversation_extractor,
    SourcePlatform.OTHER: _conversation_extractor,
}
"""Routing table from :class:`SourcePlatform` to per-platform extractor."""


def _rewrite_with_new_authored_at(
    md_file: Path,
    body: str,
    raw_metadata: dict[str, object],
    new_value: datetime | None,
) -> None:
    """Persist ``new_value`` into ``md_file``'s frontmatter; preserve body.

    Reuses the raw metadata dict so non-Fragment keys (custom Obsidian
    fields, classification scratch space) survive the round-trip
    untouched. The body string we got from :func:`try_load_fragment`
    is fed straight back into :class:`frontmatter.Post`.
    """
    updated = raw_metadata.copy()
    if new_value is None:
        updated.pop("authored_at", None)
    else:
        updated["authored_at"] = new_value.isoformat()
    # frontmatter.Post's __init__ types ``**kwargs`` as ``BaseHandler``
    # for legacy reasons even though dict-shaped metadata is what every
    # caller actually passes; the runtime accepts the dict unchanged.
    post = frontmatter.Post(content=body, **updated)  # type: ignore[arg-type]
    md_file.write_text(frontmatter.dumps(post), encoding="utf-8")


def _summarise(outcomes: list[RefreshOutcome]) -> RefreshSummary:
    """Aggregate per-file outcomes into a :class:`RefreshSummary`."""
    updated = sum(1 for o in outcomes if o.reason == "updated")
    unchanged = sum(1 for o in outcomes if o.reason == "unchanged")
    missing = sum(1 for o in outcomes if o.reason == "no-source")
    errors = sum(1 for o in outcomes if o.reason == "error")
    return RefreshSummary(
        outcomes=outcomes,
        total=len(outcomes),
        updated=updated,
        unchanged=unchanged,
        missing_source=missing,
        errors=errors,
    )


__all__ = [
    "RefreshOutcome",
    "RefreshSummary",
    "extract_authored_at_for_fragment",
    "refresh_vault_authored_at",
]
