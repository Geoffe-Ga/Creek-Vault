"""One home for the image-only PDF fixture the #1639 tests share.

A scanned PDF is the input two otherwise-unrelated suites need: the OCR
config wiring in ``tests/test_ocr_config_wiring.py`` asserts what the route
*writes*, and ``tests/test_ingest_source_key_identity.py`` asserts what it
*keys*. Both need the same bytes, and a second copy of a hand-assembled PDF
is exactly the kind of duplication a fixture drifts through — one suite
gaining a page, the other not, and the two silently testing different files.

**Why it must be hand-assembled and not mocked.** ``_detect_scanned_pdf``
reads a real page count from ``pdfminer`` and a real extraction result, so a
stub would test the test rather than the detector. And the detector requires
``page_count > 1`` (``creek/ingest/documents.py``), which is a deliberate
boundary — a single-page image-only PDF is structurally indistinguishable
from a single-page PDF that genuinely says nothing, so it is not flagged.
Fixtures here are therefore >= 2 pages by contract, and
:func:`unscannable_pdf` exists for the single-page case that must **not** be
flagged.

**Why there is no ``/Contents``.** The pages carry no content stream at all:
no font, no text operator, no ``Tj`` anywhere in the file. That is what makes
an OCR assertion non-vacuous — the expected strings cannot come from the
input, because the input contains no text of any kind. Measured against these
bytes: ``_parse_pdf_to_text`` returns ``"\\x0c"`` per page, stripped length 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


SCAN_PAGES: tuple[str, str, str] = (
    "SCAN-1639-p1 the ledger shows a debit of four pounds",
    "SCAN-1639-p2 and the second page names the surveyor",
    "SCAN-1639-p3 with a signature nobody has read since",
)
"""Per-page bodies a stub OCR engine reports for the three-page fixture.

Deliberately disjoint from the PDF's bytes by content, by length and by
cardinality, so a test asserting them on disk cannot be satisfied by anything
the extractor found in the file.
"""


def _assemble_pdf(objects: list[bytes]) -> bytes:
    """Serialise *objects* into a valid PDF with a correct xref table.

    Args:
        objects: The PDF objects, in order, numbered from 1.

    Returns:
        The complete PDF bytes.
    """
    body = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref_offset = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        body += b"%010d 00000 n \n" % offset
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(body)


def scanned_pdf(path: Path, pages: int = 3) -> Path:
    """Write a genuine, structurally valid, image-only PDF at *path*.

    Args:
        path: Where to write the PDF.
        pages: How many pages to give it. Must be >= 2, because
            ``_detect_scanned_pdf`` deliberately refuses to call a
            single-page PDF scanned; use :func:`unscannable_pdf` for that
            case rather than lowering this floor.

    Returns:
        *path*, for chaining.

    Raises:
        ValueError: When *pages* is below the detector's own boundary, which
            would make the fixture quietly untestable rather than failing.
    """
    if pages < 2:
        msg = (
            "_detect_scanned_pdf requires page_count > 1; a 1-page fixture "
            "is never flagged as scanned. Use unscannable_pdf() instead."
        )
        raise ValueError(msg)
    kids = b" ".join(b"%d 0 R" % (number + 3) for number in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages,
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>" for _ in range(pages)
    ]
    path.write_bytes(_assemble_pdf(objects))
    return path


def unscannable_pdf(path: Path) -> Path:
    """Write a **single-page** image-only PDF, which must never be flagged.

    Structurally identical to :func:`scanned_pdf` but for its page count, so
    a test using it isolates the ``page_count > 1`` boundary and nothing else.

    Args:
        path: Where to write the PDF.

    Returns:
        *path*, for chaining.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    path.write_bytes(_assemble_pdf(objects))
    return path
