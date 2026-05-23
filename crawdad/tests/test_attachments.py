"""Tests for ``crawdad.attachments`` (FEAT-027).

Covers the acceptance-criteria scenarios:

- Single attachment downloaded to a deterministic staging path.
- Multiple attachments same type → one batch.
- Multiple attachments mixed types → routed through separately.
- Oversized attachment → rejected.
- Denied extension → rejected.
- Idempotent re-upload → marked ``already_present``.
- Filename sanitisation refuses path traversal.
- Inferred ingestor type for known + unknown extensions.
- Summary formatting renders relative paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from crawdad.attachments import (
    AcceptedAttachment,
    ProcessedAttachments,
    RejectedAttachment,
    format_attachment_summary,
    infer_ingestor_type,
    process_attachments,
    sanitize_filename,
    staging_dir_for,
)
from crawdad.config import AttachmentConfig


@dataclass
class _FakeAttachment:
    """Stand-in for ``discord.Attachment`` used by the unit tests."""

    filename: str
    size: int
    url: str = "https://cdn.example/file"
    payload: bytes = b""
    fail: Exception | None = None

    async def read(self) -> bytes:
        if self.fail is not None:
            raise self.fail
        return self.payload


@pytest.fixture
def attachment_config() -> AttachmentConfig:
    """Default attachment config used by most tests."""
    return AttachmentConfig()


async def test_process_single_attachment_writes_to_staging(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """A single .md attachment lands at the deterministic staged path."""
    attachment = _FakeAttachment(
        filename="journal.md",
        size=42,
        payload=b"# Journal\n\nLine.\n",
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=999,
        message_id=12345,
        config=attachment_config,
    )

    assert len(result.accepted) == 1
    assert result.rejected == ()
    accepted = result.accepted[0]
    assert accepted.filename == "journal.md"
    assert accepted.staged_path == (
        tmp_path / "00-Creek-Meta" / "Inbound" / "999" / "12345" / "journal.md"
    )
    assert accepted.staged_path.read_bytes() == attachment.payload
    assert accepted.inferred_type == "markdown"
    assert accepted.already_present is False


async def test_process_multiple_same_type_attachments_share_batch(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Multiple .html attachments land in the same staging dir."""
    attachments = [
        _FakeAttachment(
            filename=f"export_{i}.html",
            size=10,
            payload=f"hi {i}".encode(),
        )
        for i in range(3)
    ]
    result = await process_attachments(
        attachments=attachments,
        vault_path=tmp_path,
        channel_id=11,
        message_id=22,
        config=attachment_config,
    )

    assert len(result.accepted) == 3
    assert all(a.inferred_type == "document" for a in result.accepted)
    parents = {a.staged_path.parent for a in result.accepted}
    assert len(parents) == 1
    assert result.staging_dir in parents


async def test_process_mixed_types_each_has_inferred_type(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Mixed-type attachments each carry their own inferred type."""
    attachments = [
        _FakeAttachment(filename="notes.md", size=4, payload=b"text"),
        _FakeAttachment(filename="photo.png", size=4, payload=b"PNG\x00"),
        _FakeAttachment(filename="data.csv", size=4, payload=b"a,b,c"),
    ]
    result = await process_attachments(
        attachments=attachments,
        vault_path=tmp_path,
        channel_id=11,
        message_id=33,
        config=attachment_config,
    )
    types = [a.inferred_type for a in result.accepted]
    assert types == ["markdown", "image", "spreadsheet"]
    assert result.inferred_types == tuple(types)


async def test_oversized_attachment_is_rejected_without_download(
    tmp_path: Path,
) -> None:
    """An attachment whose reported size exceeds the cap is refused."""
    config = AttachmentConfig(max_size_bytes=1024)
    attachment = _FakeAttachment(filename="big.md", size=2048, payload=b"x" * 2048)
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=config,
    )
    assert result.accepted == ()
    assert len(result.rejected) == 1
    assert "exceeds max" in result.rejected[0].reason
    # The staging directory is not created when nothing was written.
    assert not result.staging_dir.exists()


async def test_denied_extension_is_rejected(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """An attachment with a denied extension never reaches disk."""
    attachment = _FakeAttachment(filename="payload.exe", size=10, payload=b"MZ")
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    assert result.accepted == ()
    assert len(result.rejected) == 1
    assert "extension" in result.rejected[0].reason


async def test_idempotent_reupload_returns_already_present(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Re-uploading the same bytes to the same staged path is a no-op."""
    attachment = _FakeAttachment(filename="note.md", size=12, payload=b"hello world\n")
    first = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=7,
        message_id=8,
        config=attachment_config,
    )
    assert first.accepted[0].already_present is False
    second = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=7,
        message_id=8,
        config=attachment_config,
    )
    assert second.accepted[0].already_present is True
    assert second.all_already_present is True


async def test_reupload_with_different_bytes_overwrites(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Same filename + same staging path but different bytes overwrites the file."""
    first_payload = b"first version\n"
    second_payload = b"second version\n"
    attach_one = _FakeAttachment(
        filename="note.md", size=len(first_payload), payload=first_payload
    )
    attach_two = _FakeAttachment(
        filename="note.md", size=len(second_payload), payload=second_payload
    )

    await process_attachments(
        attachments=[attach_one],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    second = await process_attachments(
        attachments=[attach_two],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    assert second.accepted[0].already_present is False
    assert second.accepted[0].staged_path.read_bytes() == second_payload


async def test_post_download_size_check_rejects_underreported_size(
    tmp_path: Path,
) -> None:
    """Reviewer-flagged: the post-download size gate catches a lying size field.

    A malicious gateway could report a small ``size`` (passing the
    cheap pre-filter) and then return an unbounded body from
    ``read()``. The second size check on the downloaded bytes is the
    authoritative gate.
    """
    config = AttachmentConfig(max_size_bytes=8)
    attachment = _FakeAttachment(
        filename="liar.md",
        size=4,  # under-reported
        payload=b"x" * 4096,  # actually huge
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=config,
    )
    assert result.accepted == ()
    assert len(result.rejected) == 1
    assert "downloaded size" in result.rejected[0].reason
    # And no file landed on disk.
    staged = tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2" / "liar.md"
    assert not staged.exists()


async def test_download_failure_is_rejected_cleanly(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """A transient download exception surfaces as a rejection, not a crash."""
    attachment = _FakeAttachment(
        filename="note.md", size=12, fail=RuntimeError("network kaput")
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    assert result.accepted == ()
    assert "download failed" in result.rejected[0].reason


def test_sanitize_filename_drops_directory_separators() -> None:
    """A path-traversal filename collapses to a safe basename."""
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("/abs/path.md") == "path.md"


def test_sanitize_filename_replaces_unsafe_chars() -> None:
    """Spaces and exotic characters are replaced with underscores."""
    assert sanitize_filename("my notes (final).md") == "my_notes_final_.md"


def test_sanitize_filename_empty_falls_back_to_default() -> None:
    """An empty / fully-stripped name yields ``attachment.bin``, not a dir."""
    assert sanitize_filename("") == "attachment.bin"
    assert sanitize_filename("///") == "attachment.bin"


def test_sanitize_filename_truncates_preserving_extension() -> None:
    """Long names are truncated but keep their extension."""
    long_stem = "a" * 500
    out = sanitize_filename(f"{long_stem}.md")
    assert out.endswith(".md")
    assert len(out) <= 200


def test_sanitize_filename_truncates_extensionless_long_name() -> None:
    """A long name without an extension truncates to the cap."""
    out = sanitize_filename("a" * 500)
    assert len(out) <= 200
    assert out == "a" * 200


def test_sanitize_filename_truncates_when_extension_consumes_budget() -> None:
    """When the extension itself is too long, fall back to a flat truncate."""
    # 250-char "extension" leaves no room for the stem under the 200-char cap.
    out = sanitize_filename(f"a.{'b' * 250}")
    assert len(out) <= 200


def test_infer_ingestor_type_known_and_unknown() -> None:
    """Known extensions map to ingestor types; unknown ones return None."""
    assert infer_ingestor_type("notes.MD") == "markdown"
    assert infer_ingestor_type("report.pdf") == "document"
    assert infer_ingestor_type("photo.gif") == "image"
    assert infer_ingestor_type("weird.xyz") is None
    assert infer_ingestor_type("noext") is None


def test_staging_dir_for_is_deterministic() -> None:
    """Same channel + message → same path on every call."""
    config = AttachmentConfig()
    vault = Path("/vault")
    a = staging_dir_for(vault, channel_id=1, message_id=2, config=config)
    b = staging_dir_for(vault, channel_id=1, message_id=2, config=config)
    assert a == b == Path("/vault/00-Creek-Meta/Inbound/1/2")


def test_format_attachment_summary_renders_relative_paths(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """The summary shows the staging dir relative to the vault root."""
    staging = tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2"
    accepted = AcceptedAttachment(
        filename="note.md",
        original_filename="note.md",
        size=42,
        staged_path=staging / "note.md",
        content_hash="x" * 64,
        inferred_type="markdown",
        already_present=False,
    )
    rejected = RejectedAttachment(
        filename="big.exe", size=999, reason="extension not allowed"
    )

    processed = ProcessedAttachments(
        staging_dir=staging,
        accepted=(accepted,),
        rejected=(rejected,),
    )
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert "00-Creek-Meta/Inbound/1/2" in summary
    assert "note.md" in summary
    assert "markdown" in summary
    assert "big.exe" in summary
    assert "extension not allowed" in summary
    # Bytes rendered in human-readable form.
    assert "42 B" in summary


def test_format_attachment_summary_size_formats_kib_and_mib(
    tmp_path: Path,
) -> None:
    """Sizes render as B / KiB / MiB across the three brackets."""

    def _accepted(size: int) -> AcceptedAttachment:
        return AcceptedAttachment(
            filename=f"{size}.md",
            original_filename=f"{size}.md",
            size=size,
            staged_path=tmp_path / f"{size}.md",
            content_hash="0" * 64,
            inferred_type="markdown",
            already_present=False,
        )

    processed = ProcessedAttachments(
        staging_dir=tmp_path,
        accepted=(_accepted(50), _accepted(2048), _accepted(3 * 1024 * 1024)),
    )
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert "50 B" in summary
    assert "2.0 KiB" in summary
    assert "3.0 MiB" in summary


def test_config_normalises_extension_case_and_dots() -> None:
    """Allow/deny lists lowercase and prepend dots if missing."""
    config = AttachmentConfig(
        allowed_extensions=frozenset({"MD", ".Txt", "Pdf"}),
        denied_extensions=frozenset({"EXE"}),
    )
    assert ".md" in config.allowed_extensions
    assert ".txt" in config.allowed_extensions
    assert ".pdf" in config.allowed_extensions
    assert ".exe" in config.denied_extensions


def test_config_refuses_absolute_staging_subpath() -> None:
    """Absolute staging paths are refused — they could escape the vault."""
    with pytest.raises(ValueError, match="vault-relative"):
        AttachmentConfig(staging_subpath=Path("/etc"))


async def test_empty_allowed_extensions_passes_anything_not_denied(
    tmp_path: Path,
) -> None:
    """An empty allow list means 'allow all not denied'."""
    config = AttachmentConfig(
        allowed_extensions=frozenset(),
        denied_extensions=frozenset({".exe"}),
    )
    result = await process_attachments(
        attachments=[_FakeAttachment(filename="weird.xyz", size=4, payload=b"abcd")],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=config,
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].inferred_type is None


async def test_process_attachments_with_no_attachments_returns_empty(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """An empty iterable yields an empty :class:`ProcessedAttachments`."""
    result = await process_attachments(
        attachments=[],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    assert result.accepted == ()
    assert result.rejected == ()
    assert result.all_already_present is False


def test_format_attachment_summary_with_only_rejections(
    tmp_path: Path,
) -> None:
    """A run with only rejections still produces a readable summary."""

    processed = ProcessedAttachments(
        staging_dir=tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2",
        accepted=(),
        rejected=(RejectedAttachment(filename="oops.exe", size=1, reason="denied"),),
    )
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert "Rejected" in summary
    assert "Accepted" not in summary


def test_format_attachment_summary_outside_vault_renders_absolute(
    tmp_path: Path,
) -> None:
    """When staging_dir is outside vault_path the summary falls back to absolute."""

    elsewhere = Path("/var/spool/staged")
    processed = ProcessedAttachments(staging_dir=elsewhere, accepted=(), rejected=())
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert str(elsewhere) in summary
