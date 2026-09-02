"""Tests for the Image/OCR ingestor."""

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
    PytesseractUnavailableError,
    _average_confidence,
    _is_non_negative_number,
    detect_image_type,
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
        """Seed canned results keyed by file name."""
        self._image_results = image_results or {}
        self._pdf_results = pdf_results or {}
        self.calls: list[str] = []

    def is_available(self) -> bool:
        """Stub engines are always available — no system deps."""
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        """Return the canned image result, or a deterministic fallback."""
        self.calls.append(str(image_path))
        if image_path.name in self._image_results:
            return self._image_results[image_path.name]
        return OcrResult(
            text=f"OCR text from {image_path.name}",
            confidence=0.9,
        )

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Return the canned PDF page results, or a single-page fallback."""
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

    def test_discover_single_image_file_returns_one_raw(
        self,
        tmp_path: Path,
    ) -> None:
        """Calling discover with a single image file returns one RawDocument."""
        image_path = tmp_path / "shot.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        raws = ingestor.discover(image_path)
        assert len(raws) == 1
        assert raws[0].path == image_path

    def test_discover_single_non_image_file_returns_empty(
        self,
        tmp_path: Path,
    ) -> None:
        """A non-image file passed directly is rejected, not OCR'd.

        Without the extension guard, a stray ``.txt`` or ``.py`` could
        flow into the OCR engine and produce noise — the document
        ingestor pipeline routes non-images through other handlers.
        """
        text_path = tmp_path / "notes.txt"
        text_path.write_text("hello", encoding="utf-8")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        raws = ingestor.discover(text_path)
        assert not raws


# ---- ImageIngestor.parse + ingest --------------------------------------


def _write_image(path: Path) -> None:
    """Write a placeholder PNG file (signature only, not a real image)."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n")


def _write_real_png(path: Path) -> None:
    """Write a genuine 1x1 PNG that ``PIL.Image.open`` can decode.

    The :func:`_write_image` placeholder only carries the PNG signature
    and cannot be opened; tests that exercise the real
    :class:`PytesseractOcrEngine` OCR path (mocking only the
    ``image_to_*`` calls, not ``Image.open``) need a decodable file.
    """
    from PIL import Image

    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(path, format="PNG")


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

    def test_parse_propagates_language_to_metadata(self, tmp_path: Path) -> None:
        """The ingestor's configured language reaches fragment metadata."""
        image_path = tmp_path / "ocr.png"
        _write_image(image_path)
        ingestor = ImageIngestor(engine=StubOcrEngine(), language="eng+fra")
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments[0].metadata["language"] == "eng+fra"

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
        assert not fragments

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

    def test_pdf_page_fragment_renders_with_page_anchor(
        self,
        tmp_path: Path,
    ) -> None:
        """PDF-page fragments render with Obsidian's ``#page=N`` anchor.

        Without the anchor, every page from the same PDF would embed
        the whole document, losing the per-page context that
        :meth:`ImageIngestor.ingest_pdf` carefully preserved in
        metadata.
        """
        from creek.ingest.base import ParsedFragment

        fragment = ParsedFragment(
            content="OCR'd page 3 body",
            metadata={
                "original_file": str(tmp_path / "scan.pdf"),
                "ocr_confidence": 0.7,
                "image_type": "scanned_pdf_page",
                "page": 3,
            },
            source_path=str(tmp_path / "scan.pdf"),
            timestamp=datetime(2026, 4, 27, tzinfo=UTC),
        )
        ingestor = ImageIngestor(engine=StubOcrEngine())
        markdown = ingestor.convert_to_markdown(fragment)
        assert markdown.startswith("![[scan.pdf#page=3]]")
        assert "OCR'd page 3 body" in markdown

    def test_image_fragment_without_page_omits_anchor(
        self,
        tmp_path: Path,
    ) -> None:
        """Standalone image fragments use the bare ``![[name]]`` embed."""
        from creek.ingest.base import ParsedFragment

        fragment = ParsedFragment(
            content="hello",
            metadata={
                "original_file": str(tmp_path / "shot.png"),
                "ocr_confidence": 0.9,
                "image_type": "screenshot",
                # ``page`` deliberately absent.
            },
            source_path=str(tmp_path / "shot.png"),
            timestamp=datetime(2026, 4, 27, tzinfo=UTC),
        )
        ingestor = ImageIngestor(engine=StubOcrEngine())
        markdown = ingestor.convert_to_markdown(fragment)
        assert markdown.startswith("![[shot.png]]")
        assert "#page=" not in markdown


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


class TestImageAuthoredAtExif:
    """FEAT-031: EXIF ``DateTimeOriginal`` / ``DateTime`` → ``authored_at``.

    Pillow's image writer can stamp EXIF in JPEG output, so these
    tests round-trip a real Pillow image through the ingestor's parse
    path instead of mocking the metadata layer.
    """

    def _write_jpeg_with_exif(
        self,
        path: Path,
        *,
        datetime_original: str | None = None,
        datetime_modified: str | None = None,
    ) -> None:
        """Write a small JPEG with the requested EXIF date tags.

        EXIF strings follow the standard ``"YYYY:MM:DD HH:MM:SS"``
        format (colons in the date, not the ISO ``-``). The image
        itself is 8x8 white pixels — content does not matter because
        the ingestor stub returns canned OCR text.
        """
        from PIL import Image

        img = Image.new("RGB", (8, 8), color="white")
        exif = img.getexif()
        # 36867 = DateTimeOriginal, 306 = DateTime
        if datetime_original is not None:
            exif[36867] = datetime_original
        if datetime_modified is not None:
            exif[306] = datetime_modified
        img.save(path, "JPEG", exif=exif.tobytes() if exif else b"")

    def test_authored_at_from_datetime_original(self, tmp_path: Path) -> None:
        """``DateTimeOriginal`` (shutter time) is promoted to ``authored_at``."""
        jpeg = tmp_path / "photo.jpg"
        self._write_jpeg_with_exif(jpeg, datetime_original="2024:03:15 08:30:45")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fragments = ingestor.parse(ingestor.discover(jpeg)[0])
        assert len(fragments) == 1
        authored = fragments[0].metadata["authored_at"]
        assert authored is not None
        assert authored == datetime(2024, 3, 15, 8, 30, 45, tzinfo=UTC)

    def test_authored_at_falls_back_to_datetime_tag(self, tmp_path: Path) -> None:
        """No ``DateTimeOriginal`` → ``DateTime`` (modified) is next-best."""
        jpeg = tmp_path / "photo.jpg"
        self._write_jpeg_with_exif(jpeg, datetime_modified="2024:04:01 09:00:00")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fragments = ingestor.parse(ingestor.discover(jpeg)[0])
        authored = fragments[0].metadata["authored_at"]
        assert authored is not None
        assert authored == datetime(2024, 4, 1, 9, 0, tzinfo=UTC)

    def test_image_without_exif_has_none(self, tmp_path: Path) -> None:
        """A PNG without EXIF → ``authored_at`` is ``None``, no mtime guess."""
        from PIL import Image

        png = tmp_path / "plain.png"
        Image.new("RGB", (8, 8), color="white").save(png, "PNG")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fragments = ingestor.parse(ingestor.discover(png)[0])
        assert fragments[0].metadata["authored_at"] is None

    def test_authored_at_surfaces_in_frontmatter(self, tmp_path: Path) -> None:
        """Generated frontmatter carries ``authored_at`` as ISO string."""
        jpeg = tmp_path / "photo.jpg"
        self._write_jpeg_with_exif(jpeg, datetime_original="2024:03:15 08:30:45")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fragments = ingestor.parse(ingestor.discover(jpeg)[0])
        fm = ingestor.generate_frontmatter(fragments[0])
        assert "authored_at" in fm
        assert fm["authored_at"].startswith("2024-03-15")

    def test_no_authored_at_omits_key_from_frontmatter(self, tmp_path: Path) -> None:
        """When extraction yields ``None`` the key is absent."""
        from PIL import Image

        png = tmp_path / "plain.png"
        Image.new("RGB", (8, 8), color="white").save(png, "PNG")
        ingestor = ImageIngestor(engine=StubOcrEngine())
        fragments = ingestor.parse(ingestor.discover(png)[0])
        fm = ingestor.generate_frontmatter(fragments[0])
        assert "authored_at" not in fm


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
        # Minted here, at the one place pages are made, so every caller
        # inherits it. ``document`` is a ledgered source type and without a
        # discriminator its three pages share one origin key and one ledger
        # record — measured, that collapses them to a single file holding the
        # last page's body under the first page's name (#1305 / #1639).
        assert [fragment.source_unit for fragment in fragments] == [
            "page-1",
            "page-2",
        ]
        # The page rides ``source_unit``; ``source_path`` stays the whole
        # file, because ``routing.arbitrate`` groups on exactly that string.
        assert {fragment.source_path for fragment in fragments} == {str(pdf_path)}

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


def _make_import_blocker(blocked_modules: set[str]) -> object:
    """Build an ``__import__`` replacement that raises for *blocked_modules*.

    Used to make :class:`PytesseractOcrEngine` tests deterministic
    regardless of whether ``pytesseract`` / ``Pillow`` / ``pdf2image``
    are installed in the surrounding environment.
    """
    import builtins

    real_import = builtins.__import__

    def _blocked_import(
        name: str,
        module_globals: dict[str, object] | None = None,
        module_locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        """Raise ImportError for blocked modules; defer everything else."""
        root = name.split(".", 1)[0]
        if root in blocked_modules or name in blocked_modules:
            msg = f"mocked-missing: {name}"
            raise ImportError(msg)
        return real_import(name, module_globals, module_locals, fromlist, level)

    return _blocked_import


class TestPytesseractOcrEngine:
    """Default OCR backend respects the optional-dep contract.

    These tests deterministically simulate the missing-dep state by
    monkeypatching ``builtins.__import__`` so they pass regardless of
    whether pytesseract / Pillow / pdf2image happen to be installed in
    the surrounding environment.
    """

    def test_is_available_returns_false_when_pytesseract_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without pytesseract, the engine reports unavailable."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"pytesseract", "PIL"}),
        )
        engine = PytesseractOcrEngine()
        assert not engine.is_available()

    def test_extract_text_raises_when_pytesseract_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calling extract_text without pytesseract raises a clear error."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"pytesseract", "PIL"}),
        )
        image_path = tmp_path / "x.png"
        _write_image(image_path)
        engine = PytesseractOcrEngine()
        with pytest.raises(PytesseractUnavailableError, match="pytesseract"):
            engine.extract_text(image_path)

    def test_extract_pdf_pages_raises_when_pdf2image_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calling extract_pdf_pages without deps raises a clear error."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"pytesseract", "pdf2image"}),
        )
        pdf_path = tmp_path / "x.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        engine = PytesseractOcrEngine()
        with pytest.raises(PytesseractUnavailableError):
            engine.extract_pdf_pages(pdf_path)

    def test_is_available_returns_false_when_binary_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pytesseract present but tesseract binary absent → unavailable (GAP-015)."""
        import shutil as shutil_module

        import pytesseract

        monkeypatch.setattr(shutil_module, "which", lambda _cmd: None)

        def _raise_version() -> object:
            raise pytesseract.TesseractNotFoundError

        monkeypatch.setattr(pytesseract, "get_tesseract_version", _raise_version)
        engine = PytesseractOcrEngine()
        assert not engine.is_available()

    def test_is_available_true_when_binary_on_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When shutil.which resolves the configured binary, the engine is available."""
        import shutil as shutil_module

        monkeypatch.setattr(shutil_module, "which", lambda _cmd: "/usr/bin/tesseract")
        engine = PytesseractOcrEngine()
        assert engine.is_available()

    def test_is_available_true_when_version_probe_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``which`` misses but ``get_tesseract_version`` succeeds, stay available.

        Covers a configured absolute ``tesseract_cmd`` that ``shutil.which``
        cannot resolve but pytesseract can still execute.
        """
        import shutil as shutil_module

        import pytesseract

        monkeypatch.setattr(shutil_module, "which", lambda _cmd: None)
        monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.3.0")
        engine = PytesseractOcrEngine()
        assert engine.is_available()

    def test_is_available_honours_configured_tesseract_cmd(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The probe resolves the configured ``tesseract_cmd``, not a fixed name."""
        import shutil as shutil_module

        import pytesseract

        seen: list[str] = []

        def _which(cmd: str) -> str | None:
            seen.append(cmd)
            return "/opt/tess/mytess" if cmd == "/opt/tess/mytess" else None

        monkeypatch.setattr(shutil_module, "which", _which)
        monkeypatch.setattr(
            pytesseract.pytesseract,
            "tesseract_cmd",
            "/opt/tess/mytess",
        )
        engine = PytesseractOcrEngine()
        assert engine.is_available()
        assert seen == ["/opt/tess/mytess"]

    def test_extract_text_maps_binary_missing_to_curated_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A raw TesseractNotFoundError becomes the curated install-the-binary error."""
        import pytesseract

        def _raise(*_args: object, **_kwargs: object) -> str:
            raise pytesseract.TesseractNotFoundError

        monkeypatch.setattr(pytesseract, "image_to_string", _raise)
        image_path = tmp_path / "x.png"
        _write_real_png(image_path)
        engine = PytesseractOcrEngine()
        with pytest.raises(PytesseractUnavailableError, match="tesseract"):
            engine.extract_text(image_path)

    def test_extract_text_returns_result_when_all_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: with deps and binary present, extract_text returns OCR'd text."""
        import pytesseract

        monkeypatch.setattr(
            pytesseract,
            "image_to_string",
            lambda *_a, **_k: "decoded body",
        )
        monkeypatch.setattr(
            pytesseract,
            "image_to_data",
            lambda *_a, **_k: {"conf": [90, 95]},
        )
        image_path = tmp_path / "screenshot.png"
        _write_real_png(image_path)
        engine = PytesseractOcrEngine()
        result = engine.extract_text(image_path)
        assert result.text == "decoded body"
        assert result.confidence == pytest.approx(0.925)


# ---- Helper function unit tests ---------------------------------------


class TestAverageConfidence:
    """`_average_confidence` normalises tesseract's per-word values to [0, 1]."""

    def test_empty_input_is_zero(self) -> None:
        """An empty list returns 0.0 — there is no signal to average."""
        assert _average_confidence([]) == 0.0

    def test_all_negative_sentinels_is_zero(self) -> None:
        """Tesseract uses -1 for "missing"; an all-negative list filters down to 0.0."""
        assert _average_confidence([-1, -1, -1]) == 0.0

    def test_negative_sentinels_are_filtered_out(self) -> None:
        """Mixed input ignores negatives and averages only the non-negative values."""
        # 100 + 50 average to 75, normalised to 0.75.
        result = _average_confidence([100, 50, -1])
        assert result == pytest.approx(0.75)

    def test_zero_confidence_is_kept(self) -> None:
        """A legitimate 0% confidence is *not* the same as the -1 sentinel."""
        result = _average_confidence([0, 100])
        assert result == pytest.approx(0.5)

    def test_string_inputs_are_coerced(self) -> None:
        """Tesseract's JSON output sometimes hands back strings; coerce gracefully."""
        result = _average_confidence(["80", "60", "40"])
        assert result == pytest.approx(0.6)

    def test_unparseable_inputs_are_skipped(self) -> None:
        """Junk values (None, non-numeric strings) are dropped, not exploded."""
        result = _average_confidence([None, "junk", 80, 40])
        assert result == pytest.approx(0.6)


class TestIsNonNegativeNumber:
    """`_is_non_negative_number` filters tesseract's -1 sentinel."""

    @pytest.mark.parametrize("value", [0, 0.0, 1, 100, "0", "75"])
    def test_non_negative_values_are_accepted(self, value: object) -> None:
        """Zero, positive ints/floats, and numeric strings are kept."""
        assert _is_non_negative_number(value)

    @pytest.mark.parametrize("value", [-1, -0.5, "-1", "-100"])
    def test_negative_values_are_rejected(self, value: object) -> None:
        """Negative numbers (incl. tesseract's -1 sentinel) are filtered."""
        assert not _is_non_negative_number(value)

    @pytest.mark.parametrize("value", [None, "junk", object()])
    def test_unparseable_values_are_rejected(self, value: object) -> None:
        """Non-numeric input returns False rather than raising."""
        assert not _is_non_negative_number(value)


# ---- detect_image_type priority ---------------------------------------


class TestDetectImageTypePriority:
    """`detect_image_type` resolves overlapping hints by first-wins order."""

    def test_screenshot_outranks_photo_of_text(self) -> None:
        """A name matching both 'screenshot' and 'scan' resolves to screenshot."""
        from pathlib import Path

        result = detect_image_type(Path("screenshot-of-scanned-page.png"))
        assert result == "screenshot"

    def test_webpage_does_not_match_photo_of_text(self) -> None:
        """The bare 'page' token was removed; webpage thumbnails stay 'other'."""
        from pathlib import Path

        result = detect_image_type(Path("webpage-thumbnail.png"))
        assert result == "other"


# ---- OCRConfig.min_confidence + low-confidence routing (INC-016) -------


class TestOcrMinConfidence:
    """OCRConfig.min_confidence + ImageIngestor routing for low-confidence OCR."""

    def test_ocr_config_has_default_min_confidence(self) -> None:
        """OCRConfig must expose a default min_confidence in [0, 1]."""
        from creek.config import OCRConfig

        cfg = OCRConfig()
        assert 0.0 <= cfg.min_confidence <= 1.0

    def test_ocr_config_min_confidence_rejects_out_of_range(self) -> None:
        """min_confidence outside [0, 1] is rejected at validation."""
        from pydantic import ValidationError

        from creek.config import OCRConfig

        with pytest.raises(ValidationError):
            OCRConfig(min_confidence=1.5)
        with pytest.raises(ValidationError):
            OCRConfig(min_confidence=-0.1)

    def test_low_confidence_marks_fragment_for_review(self, tmp_path: Path) -> None:
        """OCR confidence below the threshold should set review=pending_review."""
        image_path = tmp_path / "blurry.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "blurry.png": OcrResult(
                    text="Indistinct text",
                    confidence=0.3,
                    image_type="photo_of_text",
                ),
            },
        )
        ingestor = ImageIngestor(engine=engine, min_confidence=0.6)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])

        assert len(fragments) == 1
        fragment = fragments[0]
        frontmatter = ingestor.generate_frontmatter(fragment)
        assert frontmatter.get("review") == "pending_review"

    def test_high_confidence_does_not_mark_for_review(self, tmp_path: Path) -> None:
        """Confidence above threshold leaves review unset."""
        image_path = tmp_path / "clear.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "clear.png": OcrResult(
                    text="Crystal clear text",
                    confidence=0.95,
                    image_type="screenshot",
                ),
            },
        )
        ingestor = ImageIngestor(engine=engine, min_confidence=0.6)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])

        frontmatter = ingestor.generate_frontmatter(fragments[0])
        assert "review" not in frontmatter

    def test_min_confidence_default_routes_borderline_ocr(self, tmp_path: Path) -> None:
        """Default min_confidence (0.6) flags an OCR result reporting 0.4."""
        image_path = tmp_path / "borderline.png"
        _write_image(image_path)
        engine = StubOcrEngine(
            image_results={
                "borderline.png": OcrResult(
                    text="Soft focus",
                    confidence=0.4,
                ),
            },
        )
        ingestor = ImageIngestor(engine=engine)  # use default
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])

        frontmatter = ingestor.generate_frontmatter(fragments[0])
        assert frontmatter.get("review") == "pending_review"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
