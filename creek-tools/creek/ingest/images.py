"""Image and OCR ingestor for the Creek ingest pipeline.

Implements §54: ingest screenshots, photos of handwritten notes, and
diagrams by running OCR. The ingestor is decoupled from the OCR backend
through the :class:`OcrEngine` protocol so callers can plug in any OCR
implementation; tests inject a deterministic stub. The default
:class:`PytesseractOcrEngine` lazily imports ``pytesseract``,
``Pillow``, and ``pdf2image`` so unit tests run cleanly even when those
optional dependencies (and the ``tesseract``/``poppler`` system
binaries) are not installed.

The ingestor also exposes :meth:`ImageIngestor.ingest_pdf`, which the
:class:`~creek.ingest.documents.DocumentIngestor` can call when it
detects a scanned (image-only) PDF, routing the OCR work here instead
of trying to extract text that does not exist.

Optional dependencies (install separately to enable real OCR):

* ``pytesseract`` — Python wrapper around the ``tesseract`` binary.
* ``Pillow`` — image preprocessing (contrast / rotation).
* ``pdf2image`` — converts PDF pages to images (requires ``poppler``).

Without these the ingestor still discovers files and the test suite
runs end-to-end via the stub engine; only :class:`PytesseractOcrEngine`
itself raises :class:`PytesseractUnavailableError` at OCR time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from creek.ingest.base import Ingestor, ParsedFragment, RawDocument
from creek.models import SourcePlatform

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"},
)
"""File extensions handled by :class:`ImageIngestor`."""


_DEFAULT_LANGUAGE: str = "eng"
"""Default tesseract language code; override per-engine for multi-lang OCR."""


_IMAGE_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "screenshot": ("screenshot", "screen-shot", "screen_shot", "screencap"),
    "photo_of_text": ("scan", "scanned", "handwritten", "notebook", "page"),
    "diagram": ("diagram", "chart", "schematic", "flowchart", "wireframe"),
}
"""Filename token hints used by :func:`detect_image_type`."""


# ---- Public dataclasses + protocol --------------------------------------


@dataclass(frozen=True)
class OcrResult:
    """Result of running OCR on a single image or PDF page.

    Attributes:
        text: Extracted text. Empty when OCR returned nothing usable.
        confidence: OCR confidence in ``[0.0, 1.0]``.
        page: 1-based page index (PDFs); ``0`` for standalone images.
        image_type: One of ``screenshot``, ``photo_of_text``, ``diagram``,
            ``scanned_pdf_page``, ``image``, ``other``.
    """

    text: str
    confidence: float
    page: int = 0
    image_type: str = "image"


@runtime_checkable
class OcrEngine(Protocol):
    """Pluggable OCR backend.

    Implementations must be deterministic given the same input file —
    callers (including tests) rely on stable output for the same path.
    """

    def is_available(self) -> bool:
        """Return ``True`` when the backend can perform OCR right now."""

    def extract_text(self, image_path: Path) -> OcrResult:
        """Run OCR on a single image file."""

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Run OCR on every page of a (scanned) PDF."""


class PytesseractUnavailableError(RuntimeError):
    """Raised when an OCR call is made but pytesseract is not installed."""


# ---- Default OCR engine --------------------------------------------------


class PytesseractOcrEngine:
    """OCR engine backed by ``pytesseract`` + ``pdf2image``.

    Imports of the optional dependencies are deferred until the moment
    of use so the rest of the package — and the unit tests — can run
    on systems without ``tesseract`` or ``poppler`` installed.

    Attributes:
        language: Tesseract language code(s) (e.g. ``eng`` or ``eng+fra``).
    """

    def __init__(self, language: str = _DEFAULT_LANGUAGE) -> None:
        """Initialise the engine.

        Args:
            language: Tesseract language code(s). See the tesseract
                documentation for available codes.
        """
        self.language = language

    def is_available(self) -> bool:
        """Return ``True`` when both pytesseract and Pillow import cleanly."""
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        """Run OCR on *image_path* and return the resulting text.

        Args:
            image_path: Filesystem path to the image file.

        Returns:
            An :class:`OcrResult` carrying the extracted text and the
            average per-word confidence reported by tesseract.

        Raises:
            PytesseractUnavailableError: When ``pytesseract`` or
                ``Pillow`` cannot be imported.
        """
        try:
            import pytesseract
            from PIL import Image, ImageEnhance
        except ImportError as exc:
            msg = (
                "pytesseract and Pillow are required for OCR. "
                "Install them with `pip install pytesseract pillow` "
                "and ensure the `tesseract` system binary is on PATH."
            )
            raise PytesseractUnavailableError(msg) from exc

        with Image.open(image_path) as raw:
            image = ImageEnhance.Contrast(raw.convert("RGB")).enhance(2.0)
            text = pytesseract.image_to_string(image, lang=self.language)
            data = pytesseract.image_to_data(
                image,
                lang=self.language,
                output_type=pytesseract.Output.DICT,
            )
        confidence = _average_confidence(data.get("conf", []))
        image_type = detect_image_type(image_path)
        return OcrResult(
            text=text.strip(),
            confidence=confidence,
            image_type=image_type,
        )

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Convert *pdf_path* to images page by page and OCR each one.

        Args:
            pdf_path: Filesystem path to the PDF.

        Returns:
            One :class:`OcrResult` per page (1-based ``page`` index).

        Raises:
            PytesseractUnavailableError: When ``pdf2image``,
                ``pytesseract``, or ``Pillow`` cannot be imported.
        """
        try:
            import pytesseract
            from pdf2image import convert_from_path
        except ImportError as exc:
            msg = (
                "pytesseract and pdf2image are required for scanned-PDF OCR. "
                "Install them with `pip install pytesseract pdf2image pillow` "
                "and ensure both `tesseract` and `poppler` are on PATH."
            )
            raise PytesseractUnavailableError(msg) from exc

        pages = convert_from_path(str(pdf_path))
        results: list[OcrResult] = []
        for index, page_image in enumerate(pages, start=1):
            text = pytesseract.image_to_string(page_image, lang=self.language)
            data = pytesseract.image_to_data(
                page_image,
                lang=self.language,
                output_type=pytesseract.Output.DICT,
            )
            confidence = _average_confidence(data.get("conf", []))
            results.append(
                OcrResult(
                    text=text.strip(),
                    confidence=confidence,
                    page=index,
                    image_type="scanned_pdf_page",
                ),
            )
        return results


def _average_confidence(conf_values: list[Any]) -> float:
    """Compute mean confidence in ``[0, 1]`` from tesseract's per-word values.

    Tesseract reports confidences in ``0-100`` (or ``-1`` for missing).
    Empty or all-negative input returns ``0.0``.
    """
    numeric = [float(v) for v in conf_values if _is_positive_number(v)]
    if not numeric:
        return 0.0
    return sum(numeric) / len(numeric) / 100.0


def _is_positive_number(value: Any) -> bool:
    """Return ``True`` when *value* parses to a non-negative float."""
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


# ---- Image-type heuristics ----------------------------------------------


def detect_image_type(path: Path) -> str:
    """Classify an image file by filename hints.

    Returns one of ``screenshot``, ``photo_of_text``, ``diagram``, or
    ``other``. The classification is intentionally cheap — real
    content-aware detection is out of scope for this ingestor.
    """
    name = path.stem.lower()
    for category, hints in _IMAGE_TYPE_HINTS.items():
        if any(hint in name for hint in hints):
            return category
    return "other"


# ---- ImageIngestor -------------------------------------------------------


class ImageIngestor(Ingestor):
    """Ingest images by running OCR and emitting markdown fragments.

    The OCR backend is injected through :class:`OcrEngine` so callers
    can substitute a deterministic stub in tests. The default backend
    is :class:`PytesseractOcrEngine`, which lazily imports its
    dependencies; production users must install ``pytesseract`` and
    ``Pillow`` themselves and have the ``tesseract`` binary on PATH.
    """

    def __init__(
        self,
        engine: OcrEngine | None = None,
        language: str = _DEFAULT_LANGUAGE,
    ) -> None:
        """Initialise the ingestor.

        Args:
            engine: Optional OCR backend. Defaults to a fresh
                :class:`PytesseractOcrEngine` when ``None``.
            language: Forwarded to the default engine when *engine*
                is ``None``. Ignored otherwise.
        """
        self.engine = engine if engine is not None else PytesseractOcrEngine(language)
        self.language = language

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Recursively find every supported image under *source_path*.

        Args:
            source_path: Directory (or single file) to scan.

        Returns:
            A list of :class:`RawDocument` per discovered image.
        """
        if source_path.is_file():
            paths = [source_path]
        else:
            paths = [
                p
                for p in sorted(source_path.rglob("*"))
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            ]
        return [
            RawDocument(
                path=path,
                content=path.read_bytes(),
                metadata={"original_file": str(path)},
                detected_encoding="binary",
            )
            for path in paths
        ]

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Run OCR on the raw image and return at most one fragment.

        An image with no recoverable text yields an empty list rather
        than a fragment with an empty body — empty fragments would
        clutter the vault without carrying signal.
        """
        result = self.engine.extract_text(raw.path)
        if not result.text.strip():
            logger.info("OCR produced no text for %s; skipping fragment.", raw.path)
            return []
        return [
            ParsedFragment(
                content=result.text.strip(),
                metadata={
                    "original_file": str(raw.path),
                    "ocr_confidence": result.confidence,
                    "image_type": result.image_type,
                    "language": self.language,
                },
                source_path=str(raw.path),
                timestamp=_modified_time(raw.path),
            ),
        ]

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Embed the original image, then the OCR'd body."""
        original = fragment.metadata.get("original_file", fragment.source_path)
        image_name = Path(original).name
        return f"![[{image_name}]]\n\n{fragment.content.strip()}\n"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Produce the YAML frontmatter for an OCR'd image fragment."""
        return {
            "type": "fragment",
            "source": {
                "platform": SourcePlatform.IMAGE_OCR.value,
                "original_file": fragment.metadata.get(
                    "original_file",
                    fragment.source_path,
                ),
            },
            "ocr_confidence": fragment.metadata.get("ocr_confidence", 0.0),
            "image_type": fragment.metadata.get("image_type", "other"),
            "language": fragment.metadata.get("language", self.language),
            "ingested": fragment.timestamp.isoformat(),
        }

    def ingest_pdf(self, pdf_path: Path) -> list[ParsedFragment]:
        """Run OCR on every page of *pdf_path* and return per-page fragments.

        Used by :class:`~creek.ingest.documents.DocumentIngestor` (or a
        future pipeline orchestrator) when a PDF is detected as
        scanned. Pages with no recoverable text are skipped.

        Args:
            pdf_path: Path to the (scanned) PDF.

        Returns:
            One :class:`ParsedFragment` per non-empty page.
        """
        results = self.engine.extract_pdf_pages(pdf_path)
        fragments: list[ParsedFragment] = []
        for result in results:
            if not result.text.strip():
                continue
            fragments.append(
                ParsedFragment(
                    content=result.text.strip(),
                    metadata={
                        "original_file": str(pdf_path),
                        "ocr_confidence": result.confidence,
                        "image_type": result.image_type or "scanned_pdf_page",
                        "language": self.language,
                        "page": result.page,
                    },
                    source_path=str(pdf_path),
                    timestamp=_modified_time(pdf_path),
                ),
            )
        return fragments


def _modified_time(path: Path) -> datetime:
    """Return the file's mtime as a timezone-aware UTC datetime."""
    from datetime import UTC

    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


__all__ = [
    "IMAGE_EXTENSIONS",
    "ImageIngestor",
    "OcrEngine",
    "OcrResult",
    "PytesseractOcrEngine",
    "PytesseractUnavailableError",
    "detect_image_type",
]
