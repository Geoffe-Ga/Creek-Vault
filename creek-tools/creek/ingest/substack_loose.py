"""Recognise a Substack export that arrives one LOOSE file at a time (#2123).

A Substack export is a ``posts.csv`` manifest plus one
``<post_id>.<slug>.html`` file per post. Zipped, it is identified from its
contents by :func:`creek.ingest.archive.detect_export_type`. Unzipped and sent
one file per ``POST /v1/uploads`` — which is how adepthood's folder import
sends a folder — each file used to be judged on its extension alone, so every
post became an ``Unsorted`` HTML document and ``posts.csv`` a spreadsheet.

This module answers the two questions a single file can answer about itself:

* :func:`is_substack_manifest` — is this ``posts.csv`` Substack's manifest?
  By name **and** by header, so an unrelated ``posts.csv`` stays a
  spreadsheet.
* :func:`substack_post_identity` — is this HTML file a Substack post? By name
  **and** by content: the filename must be ``<post_id>.<slug>.html`` and the
  bytes must carry Substack's editor markup, or the post must be listed in a
  manifest already received. The owner's own ``2024.notes.html`` has the
  shape and none of the evidence, and stays a document.

A received manifest is not a fragment. Its rows are kept in a small
Creek-owned store (:data:`SUBSTACK_MANIFEST_RELPATH`) so a post arriving later
can be dated and titled from it. Each row remembers the tier its manifest was
declared at, and is joined only into a post declared at that tier or a more
sensitive one: a ``personal`` manifest's titles never enter an ``open`` post.

The store is vault-wide rather than per import. ``/v1`` carries no import
namespace (adepthood hashes it into the opaque ``external_id``), and none is
needed: Substack post ids are global, so two publications' manifests cannot
collide on a key.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from creek._fsio import atomic_write_text
from creek.classify.privacy_filter import tier_sensitivity
from creek.ingest._detection import POSTS_CSV_FILENAME, extract_post_id
from creek.ingest.substack import SubstackPost, parse_posts_rows
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Mapping

SUBSTACK_MANIFEST_RELPATH: Final[Path] = Path(
    "00-Creek-Meta/adepthood/substack-manifest.json"
)
"""Vault-relative store of every Substack manifest row received by upload.

Outside :data:`creek.ingest.journal_staging.UPLOAD_STAGING_RELDIR` on
purpose: that directory holds one staged document per ``external_id`` and is
walked as such by the RTBF sweep and the export. ``purge vault`` destroys this
file with the rest of the unkept ``00-Creek-Meta`` residue.
"""

MANIFEST_REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"post_id", "post_date", "is_published"}
)
"""Header columns that make a ``posts.csv`` Substack's manifest."""

SUBSTACK_MARKUP_MARKERS: Final[tuple[bytes, ...]] = (
    b"substackcdn.com",
    b"substack-post-media",
    b"captioned-image-container",
    b"subscription-widget",
    b".substack.com/",
)
"""Byte strings Substack's editor writes into a post body and little else does.

Image containers and the CDN serving them, the subscribe widget, and links
back to the publication. Matched case-insensitively. A text-only post may
carry none of them; it is recognised by its manifest row instead.
"""

_POST_FILENAME: Final[re.Pattern[str]] = re.compile(
    r"^(\d+)\.([a-z0-9-]+)\.html?$", re.IGNORECASE
)
"""``<post_id>.<slug>.html`` — the name Substack gives every exported post."""

_STORE_KEY: Final[str] = "posts"
"""The store's single top-level key."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One manifest row as stored, with the tier its manifest arrived at.

    Attributes:
        tier: The tier the manifest upload was declared at.
        row: The row's known ``posts.csv`` columns.
    """

    tier: PrivacyTier
    row: dict[str, str]


def is_substack_manifest(filename: str, raw: bytes) -> bool:
    """Return whether *raw* is Substack's ``posts.csv`` export manifest.

    Args:
        filename: The caller's filename; only its last component is read.
        raw: The file's bytes.

    Returns:
        ``True`` when the file is named ``posts.csv`` and its header carries
        every column in :data:`MANIFEST_REQUIRED_COLUMNS`.
    """
    if Path(filename).name.lower() != POSTS_CSV_FILENAME:
        return False
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    header = next(csv.reader(io.StringIO(text)), [])
    return {column.strip().lower() for column in header} >= MANIFEST_REQUIRED_COLUMNS


def parse_manifest_rows(raw: bytes) -> dict[str, dict[str, str]]:
    """Parse a manifest's rows, keyed by the numeric post id.

    Some exports spell a ``post_id`` cell ``<id>.<slug>``, the post file's
    stem; others the bare id. Both are keyed by the leading digits, which is
    what a post filename yields, and a row with no leading digits cannot be
    joined and is dropped.

    Args:
        raw: The manifest's bytes.

    Returns:
        ``{post_id: {column: value}}`` over Substack's known columns.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    rows: dict[str, dict[str, str]] = {}
    for cell, row in parse_posts_rows(io.StringIO(text, newline="")).items():
        post_id = extract_post_id(cell)
        if post_id is not None:
            rows[post_id] = row
    return rows


def _entry_from_json(value: object) -> ManifestEntry | None:
    """Return the stored entry *value* encodes, or ``None`` when malformed.

    Args:
        value: One decoded store value.

    Returns:
        The entry, or ``None``.
    """
    if not isinstance(value, dict):
        return None
    row = value.get("row")
    if not isinstance(row, dict):
        return None
    stored_tier = value.get("tier")
    if not isinstance(stored_tier, str):
        return None
    try:
        tier = PrivacyTier(stored_tier)
    except ValueError:
        return None
    return ManifestEntry(tier=tier, row={str(k): str(v) for k, v in row.items()})


def load_manifest(vault_path: Path) -> dict[str, ManifestEntry]:
    """Return every stored manifest row, or nothing when the store is unusable.

    A missing, unreadable or malformed store is no evidence at all rather than
    an error: it can only cost a post its manifest date, never its upload.

    Args:
        vault_path: Vault root.

    Returns:
        ``{post_id: entry}``.
    """
    try:
        decoded = json.loads(
            (vault_path / SUBSTACK_MANIFEST_RELPATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    posts = decoded.get(_STORE_KEY) if isinstance(decoded, dict) else None
    if not isinstance(posts, dict):
        return {}
    entries: dict[str, ManifestEntry] = {}
    for post_id, value in posts.items():
        entry = _entry_from_json(value)
        if entry is None:
            return {}
        entries[str(post_id)] = entry
    return entries


def record_manifest(
    vault_path: Path, rows: Mapping[str, dict[str, str]], tier: PrivacyTier
) -> None:
    """Merge *rows* into the store, each remembered at *tier*.

    A row for a post already stored replaces it, tier included: the latest
    manifest is the owner's latest statement about that post. The caller holds
    the vault content-mutation lock, so read-merge-write cannot interleave.

    Args:
        vault_path: Vault root.
        rows: Parsed manifest rows, keyed by post id.
        tier: The tier the manifest upload was declared at.
    """
    merged = load_manifest(vault_path) | {
        post_id: ManifestEntry(tier=tier, row=row.copy())
        for post_id, row in rows.items()
    }
    document = {
        _STORE_KEY: {
            post_id: {"tier": entry.tier.value, "row": entry.row}
            for post_id, entry in sorted(merged.items())
        }
    }
    path = vault_path / SUBSTACK_MANIFEST_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")


def _admitted_row(
    entry: ManifestEntry | None, post_tier: PrivacyTier
) -> dict[str, str] | None:
    """Return *entry*'s row when a *post_tier* post may carry it.

    Args:
        entry: The stored row for the post, if any.
        post_tier: The tier the post upload was declared at.

    Returns:
        The row, or ``None`` when there is none or it is more sensitive.
    """
    if entry is None:
        return None
    if tier_sensitivity(entry.tier) > tier_sensitivity(post_tier):
        return None
    return dict(entry.row)


def _has_substack_markup(raw: bytes) -> bool:
    """Return whether *raw* carries any of :data:`SUBSTACK_MARKUP_MARKERS`."""
    lowered = raw.lower()
    return any(marker in lowered for marker in SUBSTACK_MARKUP_MARKERS)


def substack_post_identity(
    filename: str,
    raw: bytes,
    manifest: Mapping[str, ManifestEntry],
    tier: PrivacyTier,
) -> SubstackPost | None:
    """Return the post *filename* names, when the evidence says it is one.

    Args:
        filename: The caller's filename; only its last component is read.
        raw: The file's bytes.
        manifest: The stored manifest rows (:func:`load_manifest`).
        tier: The tier the upload was declared at, which decides whether a
            stored row may be used — as evidence or as metadata.

    Returns:
        The post's identity and admitted manifest row, or ``None`` when the
        name is not post-shaped or neither markup nor an admitted manifest
        row vouches for it.
    """
    match = _POST_FILENAME.fullmatch(Path(filename).name)
    if match is None:
        return None
    post_id, slug = match.group(1), match.group(2)
    row = _admitted_row(manifest.get(post_id), tier)
    if row is None and not _has_substack_markup(raw):
        return None
    return SubstackPost(post_id=post_id, slug=slug, row=row)
