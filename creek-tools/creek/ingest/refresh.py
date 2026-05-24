"""FEAT-031 backfill: populate ``Fragment.authored_at`` on existing fragments.

This module implements the ``creek ingest --refresh-dates`` workflow:
walk a vault's ``01-Fragments/`` tree, look up each fragment's
original source file, re-run the per-format ``authored_at`` extraction
chain, and rewrite the fragment's YAML frontmatter in place — without
touching the body or any other metadata.

Idempotency is by construction:

* Fragments that already carry an ``authored_at`` are skipped.
* Fragments whose ``source.original_file`` cannot be located on disk
  are skipped (and counted).
* The extraction chains used here are the same per-format helpers
  the ingestors call at first-ingest time, so a successful backfill
  produces the same ``authored_at`` a fresh ingest would.

Out of scope for v1:

* Chat-style sources (ChatGPT, Claude, Discord) where the
  ``original_file`` is a multi-conversation JSON export — recovering
  the per-turn ``authored_at`` would require re-pairing turns to
  fragment IDs. These fragments are reported as ``skipped`` so an
  operator knows to re-ingest the export instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path  # runtime use in type hints
from typing import TYPE_CHECKING

import frontmatter

from creek.ingest.documents import (
    _extract_docx_metadata,
    _extract_pdf_metadata,
    _parse_docx_authored_at,
    _parse_pdf_authored_at,
    _parse_rtf_authored_at,
)
from creek.ingest.html import extract_html_authored_at
from creek.ingest.images import _extract_exif_authored_at
from creek.ingest.markdown import _extract_authored_at_from_frontmatter
from creek.ingest.presentations import _extract_pptx_authored_at
from creek.ingest.spreadsheets import _extract_xlsx_authored_at
from creek.vault.reader import try_load_fragment

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class RefreshDatesResult:
    """Summary of a :func:`refresh_authored_dates` run.

    Attributes:
        scanned: Total fragments inspected (skipped + updated + already_set
            + missing_source + unsupported all sum to this).
        already_set: Fragments that already had ``authored_at`` populated;
            left untouched.
        updated: Fragments where the backfill extracted a new
            ``authored_at`` and rewrote the file.
        no_date_found: Source file was located and inspected but no
            extractable date was present (``authored_at`` remains ``None``).
        missing_source: ``source.original_file`` was empty or pointed at
            a path that no longer exists on disk.
        unsupported: Source format (chat JSON, generic blob, …) is not
            yet handled by the backfill.
        errors: Human-readable per-fragment failure messages.
    """

    scanned: int = 0
    already_set: int = 0
    updated: int = 0
    no_date_found: int = 0
    missing_source: int = 0
    unsupported: int = 0
    errors: list[str] = field(default_factory=list)


# Extension → callback for per-format authored-date extraction. The
# callback receives the source ``Path`` and returns ``datetime | None``.
# Keeping this as a table (rather than nested if/elif) keeps the dispatch
# loop's cyclomatic complexity flat as new formats are added.
def _markdown_authored_at_from_file(path: Path) -> datetime | None:
    """Re-run the markdown frontmatter chain against a single source file."""
    post = frontmatter.load(str(path))
    return _extract_authored_at_from_frontmatter(post.metadata)


def _html_authored_at_from_file(path: Path) -> datetime | None:
    """Re-run the HTML metadata chain against a single source file."""
    from creek.ingest.base import parse_authored_at

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    raw_value = extract_html_authored_at(text)
    if not raw_value:
        return None
    try:  # noqa: TRY101  # Separate I/O failure from date-parse failure.
        return parse_authored_at(raw_value)
    except ValueError:
        return None


def _pdf_authored_at_from_file(path: Path) -> datetime | None:
    """Re-run the PDF metadata chain against a single source file."""
    try:
        pdf_bytes = path.read_bytes()
    except OSError:
        return None
    metadata = _extract_pdf_metadata(pdf_bytes)
    return _parse_pdf_authored_at(metadata)


def _docx_authored_at_from_file(path: Path) -> datetime | None:
    """Re-run the DOCX metadata chain against a single source file."""
    try:
        docx_bytes = path.read_bytes()
    except OSError:
        return None
    metadata = _extract_docx_metadata(docx_bytes)
    return _parse_docx_authored_at(metadata)


def _rtf_authored_at_from_file(path: Path) -> datetime | None:
    """Re-run the RTF control-word chain against a single source file."""
    try:
        return _parse_rtf_authored_at(path.read_bytes())
    except OSError:
        return None


def _txt_authored_at_from_file(_path: Path) -> datetime | None:
    """TXT files carry no embedded date metadata (FEAT-031 ``None``)."""
    return None


_AuthoredAtHandler = Callable[[Path], "datetime | None"]

_EXTENSION_HANDLERS: dict[str, _AuthoredAtHandler] = {
    ".md": _markdown_authored_at_from_file,
    ".markdown": _markdown_authored_at_from_file,
    ".html": _html_authored_at_from_file,
    ".htm": _html_authored_at_from_file,
    ".pdf": _pdf_authored_at_from_file,
    ".docx": _docx_authored_at_from_file,
    ".rtf": _rtf_authored_at_from_file,
    ".txt": _txt_authored_at_from_file,
    ".xlsx": _extract_xlsx_authored_at,
    ".pptx": _extract_pptx_authored_at,
    ".png": _extract_exif_authored_at,
    ".jpg": _extract_exif_authored_at,
    ".jpeg": _extract_exif_authored_at,
    ".gif": _extract_exif_authored_at,
    ".bmp": _extract_exif_authored_at,
    ".tiff": _extract_exif_authored_at,
    ".webp": _extract_exif_authored_at,
}


def _resolve_authored_at_for_source(source: Path) -> datetime | None:
    """Dispatch a source path to the right per-format extraction helper.

    Returns ``None`` when the extension is unsupported or the helper
    fails — the caller distinguishes "no date found" from "unsupported
    format" via the absence of a handler for the extension.
    """
    handler = _EXTENSION_HANDLERS.get(source.suffix.lower())
    if handler is None:
        return None
    try:
        return handler(source)
    except Exception:  # per-format helpers raise wide errors
        logger.exception("Backfill helper failed for %s", source)
        return None


def _is_supported_extension(source: Path) -> bool:
    """Return whether *source*'s extension has a registered handler."""
    return source.suffix.lower() in _EXTENSION_HANDLERS


def refresh_authored_dates(vault_path: Path) -> RefreshDatesResult:
    """Backfill ``Fragment.authored_at`` across a vault's fragment tree.

    Walks ``vault_path/01-Fragments/`` recursively, re-runs the
    per-format ``authored_at`` extraction chain against each fragment's
    original source file, and rewrites the fragment file with the
    populated ``authored_at`` (preserving body and every other
    frontmatter field).

    Idempotent: fragments that already have ``authored_at`` are
    skipped, and a second invocation against a freshly backfilled
    vault is a no-op on every fragment.

    Args:
        vault_path: Path to the vault root (the directory containing
            ``01-Fragments/``).

    Returns:
        A :class:`RefreshDatesResult` summarising what changed.
    """
    result = RefreshDatesResult()
    fragments_root = vault_path / "01-Fragments"
    if not fragments_root.exists():
        return result

    for md_file in sorted(fragments_root.rglob("*.md")):
        result.scanned += 1
        try:
            _process_fragment(md_file, result)
        except Exception as exc:  # per-fragment isolation
            logger.exception("Backfill failed for %s", md_file)
            result.errors.append(f"{md_file}: {exc}")
    return result


def _process_fragment(md_file: Path, result: RefreshDatesResult) -> None:
    """Inspect one fragment file, update its ``authored_at`` if possible.

    Each branch updates exactly one counter on *result* so the summary
    accurately reflects the disposition of every scanned fragment.
    """
    record = try_load_fragment(md_file)
    if record is None:
        # Non-fragment markdown coexists in the vault tree (digests,
        # notes); ignore silently.
        result.scanned -= 1
        return
    fragment, _body, raw_metadata = record

    if fragment.authored_at is not None:
        result.already_set += 1
        return

    source_path_str = fragment.source.original_file or ""
    if not source_path_str:
        result.missing_source += 1
        return

    source_path = Path(source_path_str)
    if not source_path.exists():
        result.missing_source += 1
        return

    if not _is_supported_extension(source_path):
        # Chat / Discord JSON exports and other multi-fragment sources:
        # the per-format helpers cannot map back to a single turn, so
        # surface as ``unsupported`` rather than miscount as a missing
        # source.
        result.unsupported += 1
        return

    extracted = _resolve_authored_at_for_source(source_path)
    if extracted is None:
        result.no_date_found += 1
        return

    raw_metadata["authored_at"] = extracted.isoformat()
    _rewrite_frontmatter(md_file, raw_metadata)
    result.updated += 1


def _rewrite_frontmatter(md_file: Path, metadata: dict[str, object]) -> None:
    """Persist *metadata* to *md_file* while leaving the body untouched.

    Uses ``frontmatter.load`` to retain the original body bytes,
    swaps in the updated metadata dict, and writes via the same
    helper so YAML formatting stays consistent with the rest of the
    vault.
    """
    post = frontmatter.load(str(md_file))
    post.metadata = metadata
    md_file.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
