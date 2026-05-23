"""Discord attachment handling (FEAT-027).

CrawDad's promise is *"Discord is the door you already use."* FEAT-027
closes the gap that prevented attachments from flowing through that
door: this module downloads ``message.attachments`` to a deterministic
staging directory under the vault, enforces configurable per-attachment
size + extension limits, applies idempotency, infers an ingestor type
from the file extension, and surfaces a human-readable summary suitable
for embedding in a Discord reply.

The module is deliberately UI- and MCP-agnostic. The bot handler is
responsible for calling :func:`process_attachments` once attachments are
detected, then dispatching the optional ``creek.redact.scan`` call via
the MCP client. Ingestion itself is **never** auto-triggered — the
acceptance criteria for FEAT-027 require explicit user consent
before any ``creek.ingest`` call.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

    from crawdad.config import AttachmentConfig

_LOGGER = logging.getLogger("crawdad.attachments")

# Maximum length of a sanitised on-disk filename. Discord enforces 1024
# but most filesystems get cranky long before that; 200 keeps the
# combined ``Inbound/<channel>/<message>/<filename>`` path comfortably
# under typical 4 KB path limits even with deep vault roots.
_MAX_FILENAME_CHARS = 200

# Restrict on-disk filenames to a conservative ASCII subset so a
# malicious attachment name like ``../../../etc/passwd`` cannot escape
# the staging tree. We strip directory separators entirely and replace
# any non-alphanumeric / dash / underscore / dot characters with ``_``.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Map file extensions to ``creek.ingest`` registry types. The values
# must match keys in ``creek.ingest.INGESTOR_REGISTRY`` so the Haiku
# router can emit an intent that the MCP server's ``creek.ingest`` tool
# will accept. Missing extensions yield ``None`` so the bot can ask the
# user inline rather than guessing (per AC §intent inference).
_EXTENSION_TO_INGESTOR_TYPE: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "document",
    ".html": "document",
    ".htm": "document",
    ".pdf": "document",
    ".docx": "document",
    ".doc": "document",
    ".json": "generic",
    ".csv": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".pptx": "presentation",
    ".ppt": "presentation",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
}


class _AttachmentLike(Protocol):
    """Structural protocol covering the bits of ``discord.Attachment`` we use.

    The bot tests inject lightweight fakes via this protocol so the
    download path can be exercised without spinning up a live Discord
    HTTP session.
    """

    @property
    def filename(self) -> str: ...  # pragma: no cover - protocol stub

    @property
    def size(self) -> int: ...  # pragma: no cover - protocol stub

    @property
    def url(self) -> str: ...  # pragma: no cover - protocol stub

    async def read(self) -> bytes: ...  # pragma: no cover - protocol stub


@dataclass(frozen=True)
class RejectedAttachment:
    """One attachment refused before reaching disk.

    Attributes:
        filename: The original attachment filename (raw, unsanitised).
        size: Size in bytes as reported by Discord.
        reason: Human-readable reason ("oversized", "denied extension",
            "download failed: …").
    """

    filename: str
    size: int
    reason: str


@dataclass(frozen=True)
class AcceptedAttachment:
    """One attachment downloaded and staged.

    Attributes:
        filename: The sanitised filename as written on disk.
        original_filename: Raw filename from the Discord attachment.
        size: Size in bytes.
        staged_path: Absolute path where the file now lives.
        content_hash: SHA-256 of the file contents (for idempotency).
        inferred_type: ``creek.ingest`` registry type inferred from the
            file extension, or ``None`` when the extension is unknown.
        already_present: ``True`` when an identical file existed at the
            staged path before this download (idempotent re-upload).
    """

    filename: str
    original_filename: str
    size: int
    staged_path: Path
    content_hash: str
    inferred_type: str | None
    already_present: bool


@dataclass(frozen=True)
class ProcessedAttachments:
    """Result of one :func:`process_attachments` call.

    Attributes:
        staging_dir: The deterministic per-message staging directory.
        accepted: Attachments that landed on disk.
        rejected: Attachments refused at the boundary (size, extension,
            transient download error).
        all_already_present: ``True`` when every accepted attachment was
            an idempotent re-upload. The bot uses this to skip the
            redact scan and reply with "already staged" instead.
    """

    staging_dir: Path
    accepted: tuple[AcceptedAttachment, ...]
    rejected: tuple[RejectedAttachment, ...] = field(default_factory=tuple)

    @property
    def all_already_present(self) -> bool:
        """Return ``True`` when every accepted file was an idempotent re-upload."""
        return bool(self.accepted) and all(a.already_present for a in self.accepted)

    @property
    def inferred_types(self) -> tuple[str | None, ...]:
        """Return the inferred ingestor types in attachment order."""
        return tuple(a.inferred_type for a in self.accepted)


def staging_dir_for(
    vault_path: Path,
    *,
    channel_id: int,
    message_id: int,
    config: AttachmentConfig,
) -> Path:
    """Return the deterministic staging path for one Discord message.

    Args:
        vault_path: Vault root.
        channel_id: Discord channel id; used as the first staging
            segment so each channel has its own subtree.
        message_id: Discord message id; used as the second segment so
            re-uploads of the same message land at the same path
            (idempotency).
        config: Attachment config providing the ``staging_subpath``
            override (default ``00-Creek-Meta/Inbound``).

    Returns:
        Absolute path ``<vault>/<staging_subpath>/<channel_id>/<message_id>/``.
    """
    return vault_path / config.staging_subpath / str(channel_id) / str(message_id)


def infer_ingestor_type(filename: str) -> str | None:
    """Return the ``creek.ingest`` registry type for *filename*, or ``None``.

    The mapping mirrors the canonical extensions accepted by each
    ingestor in :data:`creek.ingest.INGESTOR_REGISTRY`. When the
    extension is unknown the bot is expected to ask the user inline
    rather than dispatching ``creek.ingest`` with a guessed type
    (per FEAT-027 §intent inference).
    """
    suffix = Path(filename).suffix.lower()
    return _EXTENSION_TO_INGESTOR_TYPE.get(suffix)


def sanitize_filename(raw: str) -> str:
    """Return a filesystem-safe filename derived from *raw*.

    Strips directory separators and replaces every non-alphanumeric /
    dash / underscore / dot character with ``_``. Truncates to
    :data:`_MAX_FILENAME_CHARS` while preserving the original
    extension. Empty or all-stripped names fall back to
    ``attachment.bin`` so a malicious empty name cannot create a
    file at the staging directory itself.
    """
    # Drop any path component — only the basename matters on disk.
    base = Path(raw).name
    cleaned = _FILENAME_SAFE_RE.sub("_", base).strip("._") or "attachment.bin"
    if len(cleaned) <= _MAX_FILENAME_CHARS:
        return cleaned
    stem, _, ext = cleaned.rpartition(".")
    if not stem:
        # No extension to preserve — just truncate.
        return cleaned[:_MAX_FILENAME_CHARS]
    max_stem = _MAX_FILENAME_CHARS - len(ext) - 1
    if max_stem <= 0:
        return cleaned[:_MAX_FILENAME_CHARS]
    return f"{stem[:max_stem]}.{ext}"


def _extension_allowed(filename: str, config: AttachmentConfig) -> bool:
    """Return ``True`` when *filename* passes the extension allow/deny list.

    The deny list takes precedence over the allow list — an extension
    present in both is rejected. An empty allow list means "allow all
    not denied".
    """
    suffix = Path(filename).suffix.lower()
    if suffix in config.denied_extensions:
        return False
    if not config.allowed_extensions:
        return True
    return suffix in config.allowed_extensions


def _content_hash(data: bytes) -> str:
    """Return the hex SHA-256 of *data* (used for idempotency)."""
    return hashlib.sha256(data).hexdigest()


async def process_attachments(
    *,
    attachments: Iterable[_AttachmentLike],
    vault_path: Path,
    channel_id: int,
    message_id: int,
    config: AttachmentConfig,
) -> ProcessedAttachments:
    """Download every Discord *attachments* into a deterministic staging dir.

    For each attachment:

    1. Reject if the extension is on the deny list or absent from a
       non-empty allow list.
    2. Reject if the reported size exceeds :attr:`AttachmentConfig.max_size_bytes`.
    3. Download the bytes via ``attachment.read()``.
    4. Hash the bytes for idempotency. If a file with the same content
       already lives at the staged path, mark the result as
       ``already_present`` and do not re-write it.
    5. Otherwise write the bytes to ``<staging>/<sanitised filename>``.

    Args:
        attachments: Iterable of ``discord.Attachment``-shaped objects.
        vault_path: Vault root.
        channel_id: Discord channel id.
        message_id: Discord message id.
        config: Attachment limits + staging subpath.

    Returns:
        A :class:`ProcessedAttachments` describing accepted + rejected
        files plus the staging directory the caller should pass to
        ``creek.redact.scan``.
    """
    staging = staging_dir_for(
        vault_path, channel_id=channel_id, message_id=message_id, config=config
    )
    accepted: list[AcceptedAttachment] = []
    rejected: list[RejectedAttachment] = []

    for attachment in attachments:
        result = await _process_one(attachment, staging=staging, config=config)
        if isinstance(result, AcceptedAttachment):
            accepted.append(result)
        else:
            rejected.append(result)

    return ProcessedAttachments(
        staging_dir=staging,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


async def _process_one(
    attachment: _AttachmentLike,
    *,
    staging: Path,
    config: AttachmentConfig,
) -> AcceptedAttachment | RejectedAttachment:
    """Apply size + extension gates, then download a single attachment.

    Returns either an :class:`AcceptedAttachment` (file written or
    already present at the staged path) or a :class:`RejectedAttachment`
    (limits failed, download errored).
    """
    if not _extension_allowed(attachment.filename, config):
        _LOGGER.info(
            "rejecting attachment %r: extension on deny list or off allow list",
            attachment.filename,
        )
        return RejectedAttachment(
            filename=attachment.filename,
            size=attachment.size,
            reason="extension not allowed",
        )
    if attachment.size > config.max_size_bytes:
        _LOGGER.info(
            "rejecting attachment %r: size %d exceeds max %d",
            attachment.filename,
            attachment.size,
            config.max_size_bytes,
        )
        return RejectedAttachment(
            filename=attachment.filename,
            size=attachment.size,
            reason=(
                f"size {attachment.size} bytes exceeds max "
                f"{config.max_size_bytes} bytes"
            ),
        )

    try:
        data = await attachment.read()
    except Exception as exc:
        _LOGGER.warning(
            "download failed for attachment %r: %s", attachment.filename, exc
        )
        return RejectedAttachment(
            filename=attachment.filename,
            size=attachment.size,
            reason=f"download failed: {exc}",
        )

    safe_name = sanitize_filename(attachment.filename)
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / safe_name

    content_hash = _content_hash(data)
    already_present = False
    if target.exists():
        existing_hash = _content_hash(target.read_bytes())
        if existing_hash == content_hash:
            already_present = True
        else:
            # Same filename, different content → re-stage. Discord
            # message_ids are unique, so the same (channel, message,
            # filename) collision with different bytes means the user
            # is intentionally overwriting. Preserve the latest copy.
            target.write_bytes(data)
    else:
        target.write_bytes(data)

    return AcceptedAttachment(
        filename=safe_name,
        original_filename=attachment.filename,
        size=len(data),
        staged_path=target,
        content_hash=content_hash,
        inferred_type=infer_ingestor_type(attachment.filename),
        already_present=already_present,
    )


def format_attachment_summary(
    processed: ProcessedAttachments,
    *,
    vault_path: Path,
) -> str:
    """Return a Discord-friendly summary of the processed attachments.

    The summary lists accepted files (with inferred types) and
    rejected files (with reasons). Paths are rendered relative to the
    vault root so absolute filesystem paths do not leak into chat.

    Args:
        processed: Result from :func:`process_attachments`.
        vault_path: Vault root, used to render the staging path
            relative to the vault.

    Returns:
        A multi-line markdown string.
    """
    try:
        rel_staging = processed.staging_dir.relative_to(vault_path)
    except ValueError:
        rel_staging = processed.staging_dir

    lines: list[str] = [
        f"**Attachments staged at** `{rel_staging}/`",
    ]
    if processed.accepted:
        lines.append("")
        lines.append("**Accepted:**")
        for a in processed.accepted:
            type_hint = a.inferred_type or "unknown type"
            marker = " (already staged)" if a.already_present else ""
            lines.append(
                f"- `{a.filename}` — {_format_size(a.size)}, {type_hint}{marker}"
            )
    if processed.rejected:
        lines.append("")
        lines.append("**Rejected:**")
        for r in processed.rejected:
            lines.append(f"- `{r.filename}` — {r.reason}")
    return "\n".join(lines)


def _format_size(num_bytes: int) -> str:
    """Render *num_bytes* as a short human-readable size (KiB/MiB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    return f"{num_bytes / (1024 * 1024):.1f} MiB"
