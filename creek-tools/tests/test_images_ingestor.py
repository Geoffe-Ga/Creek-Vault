"""Tests for the Image/OCR ingestor (issue #54)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.ingest.images import (
    IMAGE_EXTENSIONS,
    ImageIngestor,
    OcrEngine,
    OcrResult,
    PytesseractOcrEngine,
    detect_image_type,
)
from creek.ingest.images import (
    PytesseractUnavailableError as PytesseractMissingError,
)
from creek.models import SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path


# ---- Stub OCR engine -----------------------------------------------------


class StubOcrEngine:
    """Deterministic in-memory OCR engine for tests."""

    def __init__(
        self,
        image_results: dict[str, OcrResult] | None = None,
        pdf_results: dict[str, list[OcrResult]] | None = None,
    ) -> None:
        self._image_results = image_results or {}
        self._pdf_results = pdf_results or {}
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        self.calls.append(str(image_path))
        if image_path.name in self._image_results:
            return self._image_results[image_path.name]
        return OcrResult(
            text=f"OCR text from {image_path.name}",
            confidence=0.9,
            image_type="image",
        )

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        self.calls.append(str(pdf_path))
        if pdf_path.name in self._pdf_results:
            return self._pdf_results[pdf_path.name]
        return [
            OcrResult(
                text=f"Page 1 from {pdf_path.name}",
                confidence=0.85,
                page=1,
                image_type="scanned_pdf_page",
            ),
        ]


# ---- Module exports -----------------------------------------------------


class TestModuleExports:
    """``creek.ingest.images`` exposes the expected public surface."""

    def test_image_extensions_covers_required_set(self) -> None:
        """All seven image extensions from §54 are recognised."""
        expected = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}
        assert expected.issubset(IMAGE_EXTENSIONS)

    def test_ocr_engine_protocol_has_required_methods(self) -> None:
        """Stub OCR engines satisfy the protocol."""
        assert isinstance(StubOcrEngine(), OcrEngine)

    def test_image_ingestor_re_exported_from_package(self) -> None:
        """``from creek.ingest import ImageIngestor`` works."""
        from creek.ingest import ImageIngestor as Reexported

        assert Reexported is ImageIngestor


# ---- detect_image_type --------------------------------------------------


class TestDetectImageType:
    """File-name heuristics for image type detection."""

    def test_screenshot_filename_is_screenshot(self) -> None:
        """``Screenshot 2026-04-20.png`` → screenshot."""
        from pathlib import Path

        result = detect_image_type(Path("Screenshot 2026-04-20 at 09.32.png"))
        assert result == "screenshot"

    def test_screen_dash_shot_filename(self) -> None:
        """``screen-shot-1.png`` → screenshot."""
        from pathlib import Path

        assert detect_image_type(Path("screen-shot-1.png")) == "screenshot"

    def test_scanned_filename_is_photo_of_text(self) -> None:
        """``scanned-page-3.jpg`` → photo_of_text."""
        from pathlib import Path

        assert detect_image_type(Path("scanned-page-3.jpg")) == "photo_of_text"

    def test_diagram_filename_is_diagram(self) -> None:
        """``diagram-architecture.png`` → diagram."""
        from pathlib import Path

        assert detect_image_type(Path("diagram-architecture.png")) == "diagram"

    def test_unknown_filename_is_other(self) -> None:
        """Unrecognised filenames fall back to ``other``."""
        from pathlib import Path

        assert detect_image_type(Path("random-photo.jpg")) == "other"


# ---- ImageIngestor.discover --------------------------------------------


class TestDiscover:
    """``discover`` finds image files recursively and ignores non-images."""

    def test_finds_all_image_extensions(self, tmp_path: Path) -> None:
        """All seven image extensions are discovered."""
        for name in (
            "a.png",
            "b.jpg",
            "c.jpeg",
            "d.gif",
            "e.bmp",
            "f.tiff",
            "g.webp",
        ):
            (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        raws = ingestor.discover(tmp_path)
        assert {raw.path.name for raw in raws} == {
            "a.png",
            "b.jpg",
            "c.jpeg",
            "d.gif",
            "e.bmp",
            "f.tiff",
            "g.webp",
        }

    def test_ignores_non_image_extensions(self, tmp_path: Path) -> None:
        """Non-image files (``.md``, ``.txt``) are skipped."""
        (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "note.md").write_text("# note")
        (tmp_path / "data.csv").write_text("a,b,c")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        raws = ingestor.discover(tmp_path)
        assert [raw.path.name for raw in raws] == ["image.png"]

    def test_recursive_discovery(self, tmp_path: Path) -> None:
        """Images in subdirectories are discovered."""
        nested = tmp_path / "deep" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "deep.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        raws = ingestor.discover(tmp_path)
        assert any("deep.png" in str(raw.path) for raw in raws)

    def test_extension_matching_is_case_insensitive(self, tmp_path: Path) -> None:
        """``.PNG`` and ``.JPG`` are still images."""
        (tmp_path / "shout.PNG").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "shout.JPG").write_bytes(b"\xff\xd8\xff\xe0")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        raws = ingestor.discover(tmp_path)
        assert {raw.path.name for raw in raws} == {"shout.PNG", "shout.JPG"}


# ---- ImageIngestor.parse + ingest --------------------------------------


def _write_image(path: Path) -> None:
    """Write a placeholder PNG file (signature only, not a real image)."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


class TestParseAndIngest:
    """Parse runs OCR and produces a ParsedFragment with body + metadata."""

    def test_parse_returns_one_fragment_per_image(self, tmp_path: Path) -> None:
        """Each image produces exactly one ParsedFragment."""
        image_path = tmp_path / "screenshot.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "screenshot.png": OcrResult(
                    text="Hello creek",
                    confidence=0.92,
                    image_type="screenshot",
                ),
            },
        )
        ingestor = ImageIngestor(engine=engine)
        raws = ingestor.discover(tmp_path)
        fragments = ingestor.parse(raws[0])
        assert len(fragments) == 1
        assert fragments[0].content == "Hello creek"

    def test_parse_records_ocr_confidence_in_metadata(
        self,
        tmp_path: Path,
    ) -> None:
        """The fragment's metadata carries the OCR confidence score."""
        image_path = tmp_path / "screenshot.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "screenshot.png": OcrResult(
                    text="Hello",
                    confidence=0.74,
                    image_type="screenshot",
                ),
            },
        )
        ingestor = ImageIngestor(engine=engine)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments[0].metadata["ocr_confidence"] == pytest.approx(0.74)
        assert fragments[0].metadata["image_type"] == "screenshot"

    def test_parse_records_original_image_path(self, tmp_path: Path) -> None:
        """The original image path is preserved on the fragment."""
        image_path = tmp_path / "ocr.png"
        _write_image(image_path)
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments[0].source_path == str(image_path)

    def test_parse_skips_empty_ocr_text(self, tmp_path: Path) -> None:
        """An image with no recoverable text yields no fragment."""
        image_path = tmp_path / "blank.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "blank.png": OcrResult(text="", confidence=0.0),
            },
        )
        ingestor = ImageIngestor(engine=engine)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments == []

    def test_full_ingest_pipeline_succeeds(self, tmp_path: Path) -> None:
        """The full ingest() pipeline returns a populated IngestResult."""
        image_path = tmp_path / "screenshot.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "screenshot.png": OcrResult(
                    text="A screenshot",
                    confidence=0.88,
                    image_type="screenshot",
                ),
            },
        )
        ingestor = ImageIngestor(engine=engine)
        result = ingestor.ingest(tmp_path)
        assert len(result.fragments) == 1
        assert result.fragments[0].content == "A screenshot"
        assert result.errors == []


# ---- ImageIngestor.convert_to_markdown ---------------------------------


class TestConvertToMarkdown:
    """Markdown rendering embeds the original image link."""

    def test_markdown_starts_with_image_link(self, tmp_path: Path) -> None:
        """The rendered markdown begins with an Obsidian image embed."""
        from creek.ingest.base import ParsedFragment

        fragment = ParsedFragment(
            content="OCR'd text body",
            metadata={
                "original_file": str(tmp_path / "shot.png"),
                "ocr_confidence": 0.9,
                "image_type": "screenshot",
            },
            source_path=str(tmp_path / "shot.png"),
            timestamp=datetime(2026, 4, 27, tzinfo=UTC),
        )
        ingestor = ImageIngestor(engine=StubOcrEngine())
        markdown = ingestor.convert_to_markdown(fragment)
        assert markdown.startswith("![[")
        assert "shot.png" in markdown
        assert "OCR'd text body" in markdown


# ---- ImageIngestor.generate_frontmatter --------------------------------


class TestGenerateFrontmatter:
    """Frontmatter carries source.platform=image_ocr + OCR metadata."""

    def test_frontmatter_has_image_ocr_platform(self, tmp_path: Path) -> None:
        """``source.platform`` is the ``image_ocr`` enum value."""
        from creek.ingest.base import ParsedFragment

        fragment = ParsedFragment(
            content="text",
            metadata={
                "original_file": str(tmp_path / "x.png"),
                "ocr_confidence": 0.8,
                "image_type": "screenshot",
            },
            source_path=str(tmp_path / "x.png"),
            timestamp=datetime(2026, 4, 27, tzinfo=UTC),
        )
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == SourcePlatform.IMAGE_OCR.value
        assert fm["source"]["original_file"] == str(tmp_path / "x.png")
        assert fm["ocr_confidence"] == pytest.approx(0.8)
        assert fm["image_type"] == "screenshot"


# ---- ImageIngestor.ingest_pdf -----------------------------------------


class TestIngestPdf:
    """Scanned PDFs route through the OCR engine page by page."""

    def test_ingest_pdf_returns_one_fragment_per_page(
        self,
        tmp_path: Path,
    ) -> None:
        """Each PDF page yields a ParsedFragment with page-numbered metadata."""
        pdf_path = tmp_path / "scan.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        engine = StubOcrEngine(
            pdf_results={
                "scan.pdf": [
                    OcrResult(
                        text="Page 1 body",
                        confidence=0.7,
                        page=1,
                        image_type="scanned_pdf_page",
                    ),
                    OcrResult(
                        text="Page 2 body",
                        confidence=0.65,
                        page=2,
                        image_type="scanned_pdf_page",
                    ),
                ],
            },
        )
        ingestor = ImageIngestor(engine=engine)
        fragments = ingestor.ingest_pdf(pdf_path)
        assert len(fragments) == 2
        assert fragments[0].content == "Page 1 body"
        assert fragments[1].metadata["page"] == 2
        assert fragments[0].metadata["image_type"] == "scanned_pdf_page"

    def test_ingest_pdf_skips_pages_without_text(self, tmp_path: Path) -> None:
        """Pages with empty OCR are skipped."""
        pdf_path = tmp_path / "scan.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        engine = StubOcrEngine(
            pdf_results={
                "scan.pdf": [
                    OcrResult(text="Real content", confidence=0.8, page=1),
                    OcrResult(text="", confidence=0.0, page=2),
                ],
            },
        )
        ingestor = ImageIngestor(engine=engine)
        fragments = ingestor.ingest_pdf(pdf_path)
        assert len(fragments) == 1
        assert fragments[0].content == "Real content"


# ---- PytesseractOcrEngine ---------------------------------------------


class TestPytesseractOcrEngine:
    """Default OCR backend respects the optional-dep contract."""

    def test_is_available_returns_false_when_pytesseract_missing(self) -> None:
        """Without pytesseract, the engine reports unavailable."""
        # pytesseract is not installed in the test environment.
        engine = PytesseractOcrEngine()
        assert engine.is_available() is False

    def test_extract_text_raises_when_pytesseract_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Calling extract_text without pytesseract raises a clear error."""
        image_path = tmp_path / "x.png"
        _write_image(image_path)
        engine = PytesseractOcrEngine()
        with pytest.raises(PytesseractMissingError, match="pytesseract"):
            engine.extract_text(image_path)

    def test_extract_pdf_pages_raises_when_pdf2image_missing(
        self,
        tmp_path: Path,
    ) -> None:
        """Calling extract_pdf_pages without deps raises a clear error."""
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        engine = PytesseractOcrEngine()
        with pytest.raises(PytesseractMissingError):
            engine.extract_pdf_pages(pdf_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
