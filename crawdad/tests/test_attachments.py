"""Tests for ``crawdad.attachments`` (FEAT-027 + FEAT-035).

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

FEAT-035 adds:

- Matched binary content (PDF / PNG bytes under ``.pdf`` / ``.png`` ext).
- Matched text content (plain ASCII under ``.md`` / ``.txt``).
- Mismatched binary (ZIP bytes under ``.pdf`` — the polyglot case).
- Mismatched text (PE/ELF/NUL bytes under ``.md`` — disguised executable).
- Unknown extension (``.xyz`` — no verifier, status="unknown").
- ``reject_on_mime_mismatch=True`` flips soft warning into hard reject.
- Summary surfaces the mismatch warning line for accepted-but-suspect files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from crawdad.attachments import (
    AcceptedAttachment,
    MimeVerification,
    ProcessedAttachments,
    RejectedAttachment,
    format_attachment_summary,
    infer_ingestor_type,
    process_attachments,
    sanitize_filename,
    staging_dir_for,
    verify_mime_type,
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


def test_config_refuses_parent_traversal_in_staging_subpath() -> None:
    """``..`` segments are refused — defense in depth against vault escape.

    Reviewer-flagged: ``Path("../../tmp").is_absolute()`` is ``False``,
    so the absolute-path check alone is not enough.
    """
    with pytest.raises(ValueError, match=r"\.\."):
        AttachmentConfig(staging_subpath=Path("../../tmp"))


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
    """A run with only rejections uses a neutral header — nothing was staged."""
    processed = ProcessedAttachments(
        staging_dir=tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2",
        accepted=(),
        rejected=(RejectedAttachment(filename="oops.exe", size=1, reason="denied"),),
    )
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert "Rejected" in summary
    assert "Accepted" not in summary
    # Header must NOT claim "staged at" when nothing landed on disk.
    assert "staged at" not in summary
    assert "would stage to" in summary
    # The staging path is still shown for operator reference.
    assert "00-Creek-Meta/Inbound/1/2" in summary


def test_format_attachment_summary_outside_vault_renders_absolute(
    tmp_path: Path,
) -> None:
    """When staging_dir is outside vault_path the summary falls back to absolute."""

    elsewhere = Path("/var/spool/staged")
    processed = ProcessedAttachments(staging_dir=elsewhere, accepted=(), rejected=())
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert str(elsewhere) in summary


# ---------------------------------------------------------------------------
# FEAT-035: MIME-type / magic-byte verification
# ---------------------------------------------------------------------------

# Real PDF, PNG, JPEG, GIF magic bytes — the verifier hands these straight
# to ``filetype.guess`` so the constants need to match the canonical file
# headers, not just look-alikes.
_PDF_HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
_GIF_HEADER = b"GIF89a" + b"\x00" * 16
_ZIP_HEADER = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 16
_PE_HEADER = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"


def test_verify_mime_type_matched_pdf_signature_returns_match() -> None:
    """A real PDF header under a ``.pdf`` extension verifies as ``match``."""
    result = verify_mime_type("doc.pdf", _PDF_HEADER + b"rest of body")
    assert result.status == "match"
    assert result.detected_mime == "application/pdf"
    assert result.expected_mime == "application/pdf"
    assert result.is_mismatch is False


def test_verify_mime_type_matched_png_signature_returns_match() -> None:
    """A real PNG header under a ``.png`` extension verifies as ``match``."""
    result = verify_mime_type("photo.PNG", _PNG_HEADER)
    assert result.status == "match"
    assert result.detected_mime == "image/png"


def test_verify_mime_type_matched_text_sample_returns_match() -> None:
    """Plain UTF-8 text under a ``.md`` extension passes the text sample check.

    The reported MIME is per-extension (``text/markdown`` for ``.md``,
    ``application/json`` for ``.json``, etc.) so the user-facing summary
    does not lie about format identity when a match surfaces.
    """
    result = verify_mime_type("notes.md", b"# Hello\n\nSome notes.\n")
    assert result.status == "match"
    assert result.detected_mime == "text/markdown"


def test_verify_mime_type_text_match_uses_per_extension_mime() -> None:
    """``.json`` / ``.html`` / ``.csv`` matches report their specific MIMEs."""
    json_result = verify_mime_type("config.json", b'{"key": "value"}\n')
    assert json_result.status == "match"
    assert json_result.detected_mime == "application/json"

    html_result = verify_mime_type("page.html", b"<!doctype html><p>hi</p>\n")
    assert html_result.status == "match"
    assert html_result.detected_mime == "text/html"

    csv_result = verify_mime_type("data.csv", b"a,b,c\n1,2,3\n")
    assert csv_result.status == "match"
    assert csv_result.detected_mime == "text/csv"


def test_verify_mime_type_zip_disguised_as_pdf_is_mismatch() -> None:
    """The polyglot case: ZIP bytes claimed as a PDF flag as ``mismatch``.

    This is the canonical FEAT-035 scenario — a ``.pdf`` filename with
    a zip container body. The verifier reports the detected MIME so the
    user-facing summary can name the actual format.
    """
    result = verify_mime_type("invoice.pdf", _ZIP_HEADER)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/zip"
    assert result.expected_mime == "application/pdf"
    assert result.is_mismatch is True


def test_verify_mime_type_executable_disguised_as_markdown_is_mismatch() -> None:
    """A PE / EXE header renamed to ``.md`` trips the NUL-byte heuristic."""
    result = verify_mime_type("evil.md", _PE_HEADER + b"\x00" * 100)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/markdown"


def test_verify_mime_type_invalid_utf8_under_text_extension_is_mismatch() -> None:
    """Garbage bytes that aren't valid UTF-8 flag a mismatch under .txt."""
    invalid_utf8 = b"\xff\xfe\xfd\xfc" * 50
    result = verify_mime_type("garbled.txt", invalid_utf8)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_unknown_extension_returns_unknown_status() -> None:
    """An extension outside both tables can't be verified — status=unknown."""
    result = verify_mime_type("weird.xyz", b"any bytes here")
    assert result.status == "unknown"
    assert result.detected_mime is None
    assert result.expected_mime is None
    assert result.is_mismatch is False


def test_verify_mime_type_unrecognised_signature_under_binary_ext_mismatches() -> None:
    """A binary extension whose body has no matching magic bytes is a mismatch.

    ``filetype.guess`` returns ``None`` when no signature matches. The
    verifier treats that as a mismatch under a binary extension so the
    summary still surfaces the discrepancy.
    """
    result = verify_mime_type("photo.png", b"random non-PNG content here")
    assert result.status == "mismatch"
    assert result.detected_mime is None
    assert result.expected_mime == "image/png"


def test_verify_mime_type_docx_zip_is_match() -> None:
    """A real DOCX (which is a ZIP container) verifies as ``match`` under .docx."""
    result = verify_mime_type("report.docx", _ZIP_HEADER)
    assert result.status == "match"
    # The library may report either the canonical OOXML MIME or the
    # generic ZIP MIME depending on signature depth — both are acceptable.
    assert result.detected_mime in {
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }


async def test_process_attachment_with_matching_mime_lands_with_match_status(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """A well-formed PDF lands in staging with mime_verification.status='match'."""
    attachment = _FakeAttachment(
        filename="contract.pdf",
        size=len(_PDF_HEADER),
        payload=_PDF_HEADER,
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].mime_verification.status == "match"
    assert result.accepted[0].staged_path.exists()


async def test_process_attachment_with_mismatched_mime_still_stages_by_default(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Soft-warning mode: a polyglot still lands, but carries the mismatch flag.

    Default config has ``reject_on_mime_mismatch=False`` — the bot
    surfaces the warning in the Discord reply and keeps the file staged
    so the user-consent gate stays the authoritative refusal point.
    """
    attachment = _FakeAttachment(
        filename="invoice.pdf",
        size=len(_ZIP_HEADER),
        payload=_ZIP_HEADER,
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=attachment_config,
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].mime_verification.is_mismatch is True
    assert result.accepted[0].mime_verification.detected_mime == "application/zip"
    # File still lands on disk — soft warning, not hard reject.
    assert result.accepted[0].staged_path.exists()


async def test_process_attachment_with_mismatched_mime_rejected_when_configured(
    tmp_path: Path,
) -> None:
    """``reject_on_mime_mismatch=True`` hard-rejects polyglots at the gate."""
    config = AttachmentConfig(reject_on_mime_mismatch=True)
    attachment = _FakeAttachment(
        filename="invoice.pdf",
        size=len(_ZIP_HEADER),
        payload=_ZIP_HEADER,
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
    assert "MIME mismatch" in result.rejected[0].reason
    assert "application/pdf" in result.rejected[0].reason
    assert "application/zip" in result.rejected[0].reason
    # The polyglot never landed on disk in reject mode.
    staged = tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2" / "invoice.pdf"
    assert not staged.exists()


async def test_process_attachment_text_polyglot_rejected_when_configured(
    tmp_path: Path,
) -> None:
    """An EXE-disguised-as-markdown polyglot is rejected when reject mode is on."""
    config = AttachmentConfig(reject_on_mime_mismatch=True)
    attachment = _FakeAttachment(
        filename="evil.md",
        size=64,
        payload=_PE_HEADER + b"\x00" * 48,
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
    assert "MIME mismatch" in result.rejected[0].reason


async def test_process_attachment_unknown_extension_still_accepts(
    tmp_path: Path,
) -> None:
    """Unknown extension types accept silently — no verifier, no warning.

    ``reject_on_mime_mismatch`` only fires on detected mismatches; an
    ``unknown`` verification status is treated as "no information" and
    must not trigger rejection (otherwise the knob would secretly
    narrow the allow list). The fixture explicitly allow-lists
    ``.toml`` so the test pins the MIME-gate behaviour — not the
    extension-gate behaviour (covered by
    :func:`test_empty_allowed_extensions_passes_anything_not_denied`).
    """
    config = AttachmentConfig(
        allowed_extensions=frozenset({".toml"}),
        reject_on_mime_mismatch=True,
    )
    attachment = _FakeAttachment(
        filename="config.toml",
        size=8,
        payload=b"key = 1\n",
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=config,
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].mime_verification.status == "unknown"


def test_format_attachment_summary_surfaces_mime_mismatch_warning(
    tmp_path: Path,
) -> None:
    """A mismatch flag on an accepted file shows up as a ⚠ line in the summary."""
    staging = tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2"
    accepted = AcceptedAttachment(
        filename="invoice.pdf",
        original_filename="invoice.pdf",
        size=128,
        staged_path=staging / "invoice.pdf",
        content_hash="a" * 64,
        inferred_type="document",
        already_present=False,
        mime_verification=MimeVerification(
            status="mismatch",
            detected_mime="application/zip",
            expected_mime="application/pdf",
        ),
    )
    processed = ProcessedAttachments(
        staging_dir=staging,
        accepted=(accepted,),
        rejected=(),
    )
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert "⚠ MIME mismatch" in summary
    assert "application/zip" in summary
    assert "application/pdf" in summary


def test_format_attachment_summary_hides_warning_when_mime_matches(
    tmp_path: Path,
) -> None:
    """A match (or unknown) verification adds no warning line — no false noise."""
    staging = tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2"
    accepted = AcceptedAttachment(
        filename="contract.pdf",
        original_filename="contract.pdf",
        size=128,
        staged_path=staging / "contract.pdf",
        content_hash="a" * 64,
        inferred_type="document",
        already_present=False,
        mime_verification=MimeVerification(
            status="match",
            detected_mime="application/pdf",
            expected_mime="application/pdf",
        ),
    )
    processed = ProcessedAttachments(
        staging_dir=staging,
        accepted=(accepted,),
        rejected=(),
    )
    summary = format_attachment_summary(processed, vault_path=tmp_path)
    assert "⚠" not in summary
    assert "MIME mismatch" not in summary


def test_attachment_config_reject_on_mime_mismatch_defaults_false() -> None:
    """The knob defaults to soft-warning mode so the v1 consent gate is unchanged."""
    config = AttachmentConfig()
    assert config.reject_on_mime_mismatch is False


def test_extension_mime_specs_canonical_is_member_of_acceptable() -> None:
    """Structural integrity: every spec's ``canonical`` MIME is also acceptable.

    Guards against a future contributor adding an entry where the
    user-facing ``canonical`` label drifts away from the membership
    set used to decide ``match`` vs ``mismatch`` — a real file of the
    right type would otherwise be flagged as a mismatch against its
    own canonical name.
    """
    from crawdad.attachments import _EXTENSION_MIME_SPECS

    for suffix, spec in _EXTENSION_MIME_SPECS.items():
        assert spec.canonical in spec.acceptable, (
            f"{suffix}: canonical {spec.canonical!r} missing from "
            f"acceptable set {sorted(spec.acceptable)!r}"
        )


def test_attachment_config_default_allowed_extensions_omits_legacy_office() -> None:
    """FEAT-035 narrows the default allow list to verifiable extensions only.

    Legacy Office formats (``.doc`` / ``.xls`` / ``.ppt``) are not in
    :data:`crawdad.attachments._EXTENSION_TO_ACCEPTABLE_MIMES` because
    ``filetype`` does not reliably detect OLE compound documents, so
    they no longer ship in the default allow list. Operators who need
    them can re-add the extensions explicitly in ``crawdad.yaml``.
    """
    config = AttachmentConfig()
    for legacy in (".doc", ".xls", ".ppt"):
        assert legacy not in config.allowed_extensions
    # The verifiable types are still allowed by default.
    for verifiable in (".pdf", ".docx", ".xlsx", ".pptx", ".png", ".md"):
        assert verifiable in config.allowed_extensions
