"""Per-upload receipts: which caller's document became which fragments, when (#2124).

The upload ledger (``State/ingest/upload.jsonl``) keys each staged file by a
path derived from ``safe_stem(external_id)``, a one-way slug, and never records
the caller's original filename. ``GET /v1/pipeline/status`` has to name the
document a person uploaded, so ``creek.upload`` appends one receipt line per
fragment an upload produced: the consumer, the caller's ``external_id`` and
``filename``, the declared tier, the ``fragment_id`` and the instant it landed.

**Beside the ledger, on purpose.** The file is
``00-Creek-Meta/State/ingest/upload-receipts.jsonl``, inside
:func:`creek.ingest.ledger.ledger_dir`. Every erasure that reaches the ledger —
``creek purge``'s per-fragment pass and journal withdrawal both call
:func:`creek.ingest.ledger.forget_fragment_ids` — walks that directory's
``*.jsonl`` and physically removes any line whose ``fragment_id`` is doomed, so
a forgotten fragment's filename and caller id go with it, with no second erasure
path to keep in step. ``creek purge vault`` sweeps the directory whole.

Append-only, one object per line, never rewritten except by that erasure. A
damaged line is skipped on read and costs only itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from creek.ingest.ledger import ledger_dir

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

RECEIPTS_FILENAME: Final[str] = "upload-receipts.jsonl"
"""The receipts file's name inside the ingest ledger directory."""

_META_DIRNAME: Final[str] = "00-Creek-Meta"
"""The vault marker directory; a root without it is not a vault."""

_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "consumer",
    "external_id",
    "filename",
    "tier",
    "fragment_id",
)
"""The receipt fields that must each be a non-empty string."""


@dataclass(frozen=True)
class UploadReceipt:
    """One fragment an upload produced.

    Attributes:
        consumer: The authenticated caller that uploaded the document.
        external_id: The caller's idempotency key for the document.
        filename: The caller's original filename.
        tier: The tier the caller declared for the upload.
        fragment_id: One fragment the upload wrote or updated.
        at: When the upload's ingest finished.
    """

    consumer: str
    external_id: str
    filename: str
    tier: str
    fragment_id: str
    at: datetime


def receipts_path(vault_path: Path) -> Path:
    """Return ``<vault>/00-Creek-Meta/State/ingest/upload-receipts.jsonl``.

    Args:
        vault_path: Vault root.

    Returns:
        The receipts file's path, whether or not it exists.
    """
    return ledger_dir(vault_path) / RECEIPTS_FILENAME


def record_upload_receipts(
    vault_path: Path,
    *,
    consumer: str,
    external_id: str,
    filename: str,
    tier: str,
    fragment_ids: Sequence[str],
    at: datetime,
) -> bool:
    """Append one receipt per fragment, never failing the upload that landed.

    Args:
        vault_path: Vault root.
        consumer: The authenticated caller.
        external_id: The caller's idempotency key.
        filename: The caller's original filename.
        tier: The declared upload tier.
        fragment_ids: Every fragment the upload produced.
        at: When the ingest finished.

    Returns:
        ``True`` when receipts were appended; ``False`` when there were no
        fragments, no vault, or the write failed (logged by exception type).
    """
    if not fragment_ids or not (vault_path / _META_DIRNAME).is_dir():
        return False
    stamp = at.isoformat()
    lines = "".join(
        json.dumps(
            {
                "consumer": consumer,
                "external_id": external_id,
                "filename": filename,
                "tier": tier,
                "fragment_id": fragment_id,
                "at": stamp,
            },
            separators=(",", ":"),
        )
        + "\n"
        for fragment_id in fragment_ids
    )
    path = receipts_path(vault_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(lines)
    except OSError as exc:
        logger.warning("upload receipt append failed: %s", type(exc).__name__)
        return False
    return True


def _aware_instant(raw: object) -> datetime | None:
    """Parse an ISO-8601 instant carrying an offset, or return ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _text_fields(data: object) -> list[str] | None:
    """Return the receipt's text fields in order, or ``None`` when any is bad."""
    if not isinstance(data, dict):
        return None
    values = [data.get(name) for name in _TEXT_FIELDS]
    texts = [value for value in values if isinstance(value, str) and value]
    return texts if len(texts) == len(_TEXT_FIELDS) else None


def _parsed_receipt(line: str) -> UploadReceipt | None:
    """Return one line as a receipt, or ``None`` when it is damaged."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    texts = _text_fields(data)
    at = _aware_instant(data.get("at")) if texts is not None else None
    if texts is None or at is None:
        return None
    consumer, external_id, filename, tier, fragment_id = texts
    return UploadReceipt(
        consumer=consumer,
        external_id=external_id,
        filename=filename,
        tier=tier,
        fragment_id=fragment_id,
        at=at,
    )


def read_upload_receipts(vault_path: Path) -> list[UploadReceipt]:
    """Return every readable receipt, in append order.

    Args:
        vault_path: Vault root.

    Returns:
        The receipts; ``[]`` when the file is absent or unreadable.
    """
    try:
        text = receipts_path(vault_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    receipts = (_parsed_receipt(line) for line in text.splitlines() if line.strip())
    return [receipt for receipt in receipts if receipt is not None]
