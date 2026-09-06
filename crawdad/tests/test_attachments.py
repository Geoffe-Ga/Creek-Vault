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
- Multibyte UTF-8 codepoints straddling the text-sample byte boundary
  (issue #916) still verify as ``match``, while genuinely invalid bytes
  at that same offset still verify as ``mismatch``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from crawdad.attachments import (
    _MAX_FILENAME_CHARS,
    _TEXT_SAMPLE_BYTES,
    AcceptedAttachment,
    MimeVerification,
    ProcessedAttachments,
    RejectedAttachment,
    _suffixed_name,
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


# ---------------------------------------------------------------------------
# #1088 — staging_subpath must stay inside the one subtree creek.redact.scan
# will actually scan. Outside it, the scan is refused at every ceiling a
# CrawDad channel ever declares, so the safety pass silently never runs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Path("00-Creek-Meta/Inbound"), id="the-canonical-root-itself"),
        pytest.param(Path("00-Creek-Meta/Inbound/discord"), id="one-level-below"),
        pytest.param(Path("00-Creek-Meta/Inbound/a/b"), id="two-levels-below"),
        pytest.param(Path("00-Creek-Meta/./Inbound"), id="dot-segment-normalised"),
        pytest.param(Path("00-Creek-Meta/Inbound/"), id="trailing-slash-normalised"),
        pytest.param("00-Creek-Meta/Inbound", id="plain-str-coerced-by-pydantic"),
    ],
)
def test_config_accepts_staging_subpath_inside_canonical_root(
    value: Path | str,
) -> None:
    """#1088: the canonical scan scope, and anything under it, are accepted.

    ``creek.redact.scan`` is hard-scoped to ``00-Creek-Meta/Inbound/``
    (``creek-tools/creek_mcp/tools/redact.py:98``) and admits that subtree
    at *every* ceiling, so every value here can actually be scanned and
    must keep parsing.

    ``dot-segment-normalised`` and ``trailing-slash-normalised`` pin that
    ``pathlib`` collapses ``.`` and a trailing separator before the
    validator ever sees the value, so the scope check does not have to
    re-implement normalisation. ``plain-str-coerced-by-pydantic`` pins
    that a YAML string flows through the same validator as a ``Path``.
    """
    config = AttachmentConfig(staging_subpath=value)
    assert config.staging_subpath.parts[:2] == ("00-Creek-Meta", "Inbound")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Path("01-Fragments"), id="unrelated-vault-folder"),
        pytest.param(Path("00-Creek-Meta/Inbound-other"), id="sibling-prefix"),
        pytest.param(Path("00-Creek-Meta/Outbound"), id="sibling-folder"),
        pytest.param(Path("00-Creek-Meta"), id="the-parent-is-not-inside"),
        pytest.param(Path("00-creek-meta/inbound"), id="case-differs"),
        pytest.param(Path(""), id="empty-normalises-to-dot"),
    ],
)
def test_config_refuses_staging_subpath_outside_canonical_root(value: Path) -> None:
    """#1088: a staging root the scan cannot reach is refused at parse time.

    Outside ``00-Creek-Meta/Inbound/`` the scan ranks the target as
    intimate content, so a ``personal``/``open`` CrawDad channel gets
    ``status="refused"`` on every call and the safety pass never runs.
    Refusing at config-parse time turns that silent runtime hole into a
    startup error naming the fix.

    ``sibling-prefix`` (``00-Creek-Meta/Inbound-other``) is the case that
    forces ``Path.is_relative_to`` over ``str.startswith``: it shares the
    canonical root's string prefix but is a different directory, and a
    ``startswith`` check would wave it through.

    ``case-differs`` (``00-creek-meta/inbound``) must be refused on every
    platform, including case-insensitive filesystems. Case folding is
    deliberately NOT performed: ``creek_mcp/tools/redact.py:340`` compares
    the target against ``00-Creek-Meta/Inbound`` literally, so folding
    here would admit a config the server then refuses — reopening the
    exact never-scanned hole this issue closes.
    """
    with pytest.raises(ValueError, match="00-Creek-Meta/Inbound") as excinfo:
        AttachmentConfig(staging_subpath=value)
    # The message must restate the scope rule, not just name the folder —
    # the operator needs to know *why* their path cannot be scanned.
    assert "ranked as intimate content" in str(excinfo.value)


def test_config_refuses_out_of_scope_staging_subpath_at_every_ceiling() -> None:
    """#1088: the config gate does not merely shadow the downstream tier rule.

    A channel declaring ``intimate`` *would* be admitted by
    ``creek.redact.scan`` for an out-of-scope path, so a gate that only
    fired for narrow ceilings would be a restatement of the server's rule
    rather than an independent one. The refusal must turn on the path
    alone, whatever ``channel_privacy_tiers`` says.
    """
    with pytest.raises(ValueError, match="00-Creek-Meta/Inbound"):
        AttachmentConfig(
            staging_subpath=Path("01-Fragments"),
            channel_privacy_tiers={1: "intimate"},
        )


def test_config_refuses_traversal_out_of_canonical_root_via_the_dotdot_arm() -> None:
    """#1088: ``..`` inside the canonical root is caught by the ``..`` arm.

    ``00-Creek-Meta/Inbound/../../01-Fragments`` is *lexically*
    ``is_relative_to`` the canonical root — ``Path.is_relative_to`` is a
    pure prefix comparison and does not resolve ``..`` — so the new scope
    arm alone would wave it through. Only the pre-existing ``..`` arm
    catches it, which means the arms must stay in order: absolute, then
    ``..``, then scope.

    The negative assertion is the real discriminator: both arms
    interpolate ``value!r``, so the path's own dots satisfy ``match``
    either way, but only the scope arm restates the tier rule.
    """
    with pytest.raises(ValueError, match=r"\.\.") as excinfo:
        AttachmentConfig(
            staging_subpath=Path("00-Creek-Meta/Inbound/../../01-Fragments")
        )
    assert "ranked as intimate content" not in str(excinfo.value)


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


# ---------------------------------------------------------------------------
# Issue #916: the text sample is cut at a fixed byte offset, so a multibyte
# UTF-8 codepoint can straddle the window edge. Truncation is not corruption
# — the tests below pin "a split codepoint is still text" without loosening
# the checks that catch genuinely binary or malformed bodies at that offset.
# All boundary arithmetic derives from ``_TEXT_SAMPLE_BYTES`` so the tests
# follow the constant if it is ever retuned.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("char", "inside_bytes"),
    [
        pytest.param("é", 1, id="2byte-char-1-inside-1-outside"),
        pytest.param("文", 1, id="3byte-char-1-inside-2-outside"),
        pytest.param("文", 2, id="3byte-char-2-inside-1-outside"),
        pytest.param("😀", 1, id="4byte-char-1-inside-3-outside"),
        pytest.param("😀", 2, id="4byte-char-2-inside-2-outside"),
        pytest.param("😀", 3, id="4byte-char-3-inside-1-outside"),
    ],
)
def test_verify_mime_type_multibyte_split_at_sample_boundary_is_match(
    char: str, inside_bytes: int
) -> None:
    """A codepoint split across the sample edge is text, not a binary blob.

    ``inside_bytes`` of the character's UTF-8 encoding land inside the
    sampled window and the remainder falls past it, for every split of
    every multibyte width (2, 3 and 4 bytes). The body is valid UTF-8
    end to end, so the only reason a naive strict decode of the slice
    fails is the cut itself — issue #916's false ``mismatch``.
    """
    encoded = char.encode()
    pad = b"a" * (_TEXT_SAMPLE_BYTES - inside_bytes)
    data = pad + encoded + b" trailing\n"

    # Self-checks: fail loudly if the fixture does not actually straddle.
    assert len(data) > _TEXT_SAMPLE_BYTES
    assert data[:_TEXT_SAMPLE_BYTES][-inside_bytes:] == encoded[:inside_bytes]
    # The premise: the whole payload is valid UTF-8, only the slice is cut.
    assert data.decode("utf-8").endswith(f"{char} trailing\n")

    result = verify_mime_type("notes.md", data)
    assert result.status == "match"
    assert result.detected_mime == "text/markdown"
    assert result.expected_mime == "text/markdown"
    assert result.is_mismatch is False


def test_verify_mime_type_accented_char_at_byte_1024_boundary_is_match() -> None:
    """Issue #916 acceptance criterion 1, spelled out verbatim.

    ``verify_mime_type("notes.md", b"a" * 1023 + "é".encode() + b" more
    text\\n").status == "match"``. The literal 1023 is intentional here
    (every other test derives from :data:`_TEXT_SAMPLE_BYTES`) so the
    stated criterion stays greppable against the issue text.
    """
    data = b"a" * 1023 + "é".encode() + b" more text\n"
    result = verify_mime_type("notes.md", data)
    assert result.status == "match"
    assert result.detected_mime == "text/markdown"
    assert result.expected_mime == "text/markdown"


def test_verify_mime_type_truncated_utf8_tail_in_short_file_is_mismatch() -> None:
    """Invariant: a dangling lead byte at end-of-file is still a mismatch.

    The whole body fits inside the sample window, so nothing was cut by
    sampling — the file itself ends mid-codepoint and is genuinely not
    valid UTF-8. The #916 fix must not forgive this.
    """
    data = b"hello\xc3"
    assert len(data) < _TEXT_SAMPLE_BYTES

    result = verify_mime_type("notes.txt", data)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_dangling_lead_byte_at_sample_size_is_mismatch() -> None:
    """Invariant: a body of exactly ``_TEXT_SAMPLE_BYTES`` was never cut.

    Adjacent pair with
    :func:`test_verify_mime_type_complete_char_one_byte_past_sample_is_match`:
    together they pin the off-by-one. Truncation forgiveness may only
    apply when the body extends *past* the window (``len(data) >
    _TEXT_SAMPLE_BYTES``), never when it ends exactly at it — here the
    trailing ``\\xc3`` has no continuation byte anywhere in the file.
    """
    data = b"a" * (_TEXT_SAMPLE_BYTES - 1) + b"\xc3"
    assert len(data) == _TEXT_SAMPLE_BYTES

    result = verify_mime_type("notes.txt", data)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_complete_char_one_byte_past_sample_is_match() -> None:
    """Invariant: one byte past the window completes the codepoint.

    Adjacent pair with
    :func:`test_verify_mime_type_dangling_lead_byte_at_sample_size_is_mismatch`:
    the same 1023 ``a``\\ s plus ``\\xc3``, but this body carries the
    ``\\xa9`` continuation just outside the window. Same leading bytes,
    opposite verdict — that is the ``>`` vs ``>=`` boundary.
    """
    data = b"a" * (_TEXT_SAMPLE_BYTES - 1) + b"\xc3\xa9"
    assert len(data) == _TEXT_SAMPLE_BYTES + 1

    result = verify_mime_type("notes.txt", data)
    assert result.status == "match"
    assert result.detected_mime == "text/plain"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_invalid_bytes_mid_sample_is_mismatch() -> None:
    """Invariant: undecodable bytes far from the edge stay a mismatch.

    ``\\xff\\xfe`` sits at offset 500 with 600 bytes of text after it —
    nowhere near the sample boundary, so no truncation-tolerance logic
    may excuse it.
    """
    data = b"a" * 500 + b"\xff\xfe" + b"a" * 600
    assert len(data) > _TEXT_SAMPLE_BYTES

    result = verify_mime_type("notes.txt", data)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_invalid_bytes_at_sample_boundary_is_mismatch() -> None:
    """Invariant: never blanket-drop the tail of the sample.

    ``\\xff`` and ``\\xfe`` are not legal UTF-8 lead bytes anywhere, and
    here they occupy the last two bytes of the window. A fix that simply
    shaves up to three trailing bytes off the sample and re-decodes
    would call this text — it is not.
    """
    data = b"a" * (_TEXT_SAMPLE_BYTES - 2) + b"\xff\xfe" + b"a" * 100
    assert len(data) > _TEXT_SAMPLE_BYTES
    assert data[_TEXT_SAMPLE_BYTES - 2 : _TEXT_SAMPLE_BYTES] == b"\xff\xfe"

    result = verify_mime_type("notes.txt", data)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_bad_continuation_at_boundary_is_mismatch() -> None:
    """Invariant: a legal lead byte with an illegal continuation is binary.

    ``\\xe6`` is a valid 3-byte lead and sits two bytes from the window
    edge, but ``\\x28`` is not a continuation byte — the sequence is
    malformed where it stands, not merely unfinished. Only an *unfinished
    but well-formed* trailing sequence may be forgiven.
    """
    data = b"a" * (_TEXT_SAMPLE_BYTES - 2) + b"\xe6\x28" + b"a" * 100
    assert len(data) > _TEXT_SAMPLE_BYTES

    result = verify_mime_type("notes.txt", data)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_nul_byte_early_in_long_file_is_mismatch() -> None:
    """Invariant: the NUL-byte heuristic survives the #916 fix.

    A NUL at offset 100 of a 2101-byte body is the disguised-binary
    signal; UTF-8 truncation handling must not route around this check.
    """
    data = b"a" * 100 + b"\x00" + b"a" * 2000
    assert len(data) > _TEXT_SAMPLE_BYTES

    result = verify_mime_type("notes.txt", data)
    assert result.status == "mismatch"
    assert result.detected_mime == "application/octet-stream"
    assert result.expected_mime == "text/plain"


def test_verify_mime_type_nul_byte_at_last_sample_byte_is_mismatch() -> None:
    """Invariant: the NUL scan still covers the final byte of the window.

    The NUL sits at offset ``_TEXT_SAMPLE_BYTES - 1`` — the last byte
    inspected. A fix that shrinks the scanned window to dodge a split
    codepoint would stop seeing it.
    """
    data = b"a" * (_TEXT_SAMPLE_BYTES - 1) + b"\x00" + b"a" * 100
    assert len(data) > _TEXT_SAMPLE_BYTES
    assert data[_TEXT_SAMPLE_BYTES - 1] == 0

    result = verify_mime_type("notes.txt", data)
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


async def test_process_attachment_boundary_straddle_accepted_in_reject_mode(
    tmp_path: Path,
) -> None:
    """Issue #916 acceptance criterion 3: valid text is not hard-rejected.

    Reject mode is the strictest setting, so it is where the false
    ``mismatch`` costs the user their file. A markdown note whose only
    peculiarity is an accented character landing on the sample boundary
    must be accepted, verified as ``match``, and written to staging.
    """
    config = AttachmentConfig(reject_on_mime_mismatch=True)
    payload = b"a" * (_TEXT_SAMPLE_BYTES - 1) + "é".encode() + b" tail\n"
    attachment = _FakeAttachment(
        filename="notes.md",
        size=len(payload),
        payload=payload,
    )
    result = await process_attachments(
        attachments=[attachment],
        vault_path=tmp_path,
        channel_id=1,
        message_id=2,
        config=config,
    )
    assert result.rejected == ()
    assert len(result.accepted) == 1
    assert result.accepted[0].mime_verification.status == "match"
    staged = tmp_path / "00-Creek-Meta" / "Inbound" / "1" / "2" / "notes.md"
    assert staged.exists()
    assert staged.read_bytes() == payload


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


# --- Issue #917: same-batch filename collision after sanitisation -----------


def _sha256(data: bytes) -> str:
    """Return the SHA-256 hex digest of *data* (mirrors ``_content_hash``)."""
    return hashlib.sha256(data).hexdigest()


async def test_same_batch_sanitized_collision_stages_both_files(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Two names that alias after sanitisation both survive (issue #917).

    ``"a b.md"`` and ``"a_b.md"`` both sanitise to ``a_b.md``. Before
    the fix the second attachment's bytes overwrote the first's, both
    ``AcceptedAttachment`` records pointed at one file, and the first
    file's ``content_hash`` no longer described anything on disk — so
    the bot ingested the second payload twice while recording the
    first hash as ingested.
    """
    payload_a = b"# file one, payload P1\n"
    payload_b = b"# file two, DIFFERENT payload P2\n"
    result = await process_attachments(
        attachments=[
            _FakeAttachment(filename="a b.md", size=len(payload_a), payload=payload_a),
            _FakeAttachment(filename="a_b.md", size=len(payload_b), payload=payload_b),
        ],
        vault_path=tmp_path,
        channel_id=5,
        message_id=6,
        config=attachment_config,
    )

    assert result.rejected == ()
    assert len(result.accepted) == 2
    first, second = result.accepted

    # Both files survive at distinct paths.
    assert first.staged_path != second.staged_path
    assert first.staged_path.read_bytes() == payload_a
    assert second.staged_path.read_bytes() == payload_b

    # Every recorded hash describes the bytes actually on disk, so the
    # ingest dispatcher never records a hash for content it did not send.
    for accepted, payload in ((first, payload_a), (second, payload_b)):
        assert accepted.content_hash == _sha256(payload)
        assert accepted.content_hash == _sha256(accepted.staged_path.read_bytes())
        assert accepted.already_present is False

    # Exactly two files landed in the staging dir — no clobbering.
    staged = sorted(p.name for p in result.staging_dir.iterdir())
    assert staged == ["a_b-1.md", "a_b.md"]


async def test_same_batch_truncation_collision_stages_both_files(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Two long names sharing a prefix alias via truncation — both survive.

    ``sanitize_filename`` caps the name at :data:`_MAX_FILENAME_CHARS`,
    so two distinct 300-character names with a common prefix collapse
    onto one staged name just as surely as the punctuation case.
    """
    stem = "z" * 300
    payload_a = b"long-name payload A\n"
    payload_b = b"long-name payload B\n"
    result = await process_attachments(
        attachments=[
            _FakeAttachment(
                filename=f"{stem}-one.md", size=len(payload_a), payload=payload_a
            ),
            _FakeAttachment(
                filename=f"{stem}-two.md", size=len(payload_b), payload=payload_b
            ),
        ],
        vault_path=tmp_path,
        channel_id=5,
        message_id=7,
        config=attachment_config,
    )

    first, second = result.accepted
    # The two raw names really do alias under the sanitiser...
    assert sanitize_filename(f"{stem}-one.md") == sanitize_filename(f"{stem}-two.md")
    # ...but each staged file reports the distinct name it has on disk.
    assert first.filename != second.filename
    assert first.staged_path != second.staged_path
    assert first.staged_path.read_bytes() == payload_a
    assert second.staged_path.read_bytes() == payload_b
    # The disambiguating suffix still respects the filename length cap.
    assert len(second.staged_path.name) <= _MAX_FILENAME_CHARS


async def test_same_batch_collision_reprocessed_is_idempotent(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Re-running a colliding batch re-uses both staged paths, minting no third."""
    payload_a = b"payload P1\n"
    payload_b = b"payload P2\n"

    def _batch() -> list[_FakeAttachment]:
        return [
            _FakeAttachment(filename="a b.md", size=len(payload_a), payload=payload_a),
            _FakeAttachment(filename="a_b.md", size=len(payload_b), payload=payload_b),
        ]

    kwargs: dict[str, object] = {
        "vault_path": tmp_path,
        "channel_id": 8,
        "message_id": 9,
        "config": attachment_config,
    }
    first_run = await process_attachments(attachments=_batch(), **kwargs)  # type: ignore[arg-type]
    second_run = await process_attachments(attachments=_batch(), **kwargs)  # type: ignore[arg-type]

    assert [a.staged_path for a in second_run.accepted] == [
        a.staged_path for a in first_run.accepted
    ]
    assert all(a.already_present for a in second_run.accepted)
    assert second_run.all_already_present is True
    assert len(list(second_run.staging_dir.iterdir())) == 2


async def test_same_batch_identical_payloads_still_stage_separately(
    tmp_path: Path, attachment_config: AttachmentConfig
) -> None:
    """Aliasing names with *identical* bytes still get one file each.

    The de-dup shortcut must not fire for a sibling inside the same
    batch: the two ``PendingFile`` records would otherwise share a
    staged path again, and ``already_present`` would tell the user a
    file they just uploaded was skipped.
    """
    payload = b"identical bytes\n"
    result = await process_attachments(
        attachments=[
            _FakeAttachment(filename="a b.md", size=len(payload), payload=payload),
            _FakeAttachment(filename="a_b.md", size=len(payload), payload=payload),
        ],
        vault_path=tmp_path,
        channel_id=10,
        message_id=11,
        config=attachment_config,
    )

    first, second = result.accepted
    assert first.staged_path != second.staged_path
    assert first.already_present is False
    assert second.already_present is False
    assert len(list(result.staging_dir.iterdir())) == 2


def test_suffixed_name_ordinal_zero_is_the_name_itself() -> None:
    """Ordinal 0 is the undisambiguated name — the common, non-colliding case."""
    assert _suffixed_name("notes.md", 0) == "notes.md"


def test_suffixed_name_splices_marker_before_the_extension() -> None:
    """The extension survives so ``infer_ingestor_type`` still routes the file."""
    assert _suffixed_name("notes.md", 3) == "notes-3.md"
    assert infer_ingestor_type(_suffixed_name("notes.md", 3)) == "markdown"


def test_suffixed_name_handles_extensionless_names() -> None:
    """A name with no dot gets the marker appended, not spliced into nothing."""
    assert _suffixed_name("README", 2) == "README-2"


def test_suffixed_name_drops_extension_when_it_eats_the_whole_budget() -> None:
    """A pathological extension still yields distinct, capped names.

    ``sanitize_filename('a.' + 'b' * 250)`` really does return a name
    whose \"extension\" is 198 characters, leaving no room for both the
    extension and the ``-N`` marker inside the cap. Distinct ordinals
    must still produce distinct names, or the disambiguation loop
    would spin.
    """
    pathological = sanitize_filename(f"a.{'b' * 250}")
    first = _suffixed_name(pathological, 1)
    second = _suffixed_name(pathological, 2)
    assert first != second
    assert len(first) <= _MAX_FILENAME_CHARS
    assert len(second) <= _MAX_FILENAME_CHARS


async def test_pathological_extension_collision_stages_both_files(
    tmp_path: Path,
) -> None:
    """End-to-end cover for the budget-exhausted disambiguation path."""
    ext = "b" * 250
    payload_a = b"pathological A\n"
    payload_b = b"pathological B\n"
    config = AttachmentConfig(allowed_extensions=frozenset())
    result = await process_attachments(
        attachments=[
            _FakeAttachment(
                filename=f"a.{ext}", size=len(payload_a), payload=payload_a
            ),
            _FakeAttachment(
                filename=f"a_.{ext}", size=len(payload_b), payload=payload_b
            ),
        ],
        vault_path=tmp_path,
        channel_id=12,
        message_id=13,
        config=config,
    )

    assert result.rejected == ()
    first, second = result.accepted
    assert first.staged_path != second.staged_path
    assert first.staged_path.read_bytes() == payload_a
    assert second.staged_path.read_bytes() == payload_b
