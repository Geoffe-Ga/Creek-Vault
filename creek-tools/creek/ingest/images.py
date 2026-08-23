"""Image and OCR ingestor for the Creek ingest pipeline.

Implements §54: ingest screenshots, photos of handwritten notes, and
diagrams by running OCR. The ingestor is decoupled from the OCR backend
through the :class:`OcrEngine` protocol so callers can plug in any OCR
implementation; tests inject a deterministic stub. The default
:class:`PytesseractOcrEngine` lazily imports ``pytesseract``,
``Pillow``, and ``pdf2image`` so unit tests run cleanly even when those
optional dependencies (and the ``tesseract``/``poppler`` system
binaries) are not installed.

The ingestor also exposes :meth:`ImageIngestor.ingest_pdf`, intended for
:class:`~creek.ingest.documents.DocumentIngestor` to call when it detects
a scanned (image-only) PDF, routing the OCR work here instead of trying
to extract text that does not exist. **That routing does not exist yet**:
``ingest_pdf`` has no production caller, so a scanned PDF is not OCR'd
today by any command. Callers can invoke it directly on a known-scanned
PDF in the meantime.

Which of the four ``ocr.*`` config keys reach this module, and how, is
:meth:`ImageIngestor.from_ocr_config` — the one place the block becomes
behaviour (#1517).

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
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from creek.config import OCRConfig
from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    file_modified_time,
    parse_authored_at,
)
from creek.models import SourcePlatform

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)


IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"},
)
"""File extensions handled by :class:`ImageIngestor`."""


class OcrConfigError(ValueError):
    """Raised when the vault's ``ocr`` block cannot be turned into an engine.

    One base class for every way the block can be unusable, so a surface can
    translate "your OCR config is wrong" into its own refusal idiom with a
    single ``except`` — without widening that ``except`` to bare
    :class:`ValueError` and swallowing failures that have nothing to do with
    configuration.
    """


def join_ocr_languages(languages: Sequence[str]) -> str:
    """Render a configured language list the one way Tesseract accepts it.

    :pyattr:`creek.config.OCRConfig.languages` is a **list** (``["eng",
    "fra"]``) because that is the shape an operator can read and extend in
    YAML. Tesseract's ``lang`` argument is a single ``+``-joined **string**
    (``"eng+fra"``). Nothing bridged the two before #1517, which is a large
    part of why ``ocr.languages`` never reached an engine: there was no
    agreed spelling for the join, so no caller could have performed it
    consistently. This function is that agreement, and it is the only place
    the join is written.

    Blank and whitespace-only codes are dropped rather than passed through,
    because ``"eng+"`` makes Tesseract fail with a message that names the
    engine rather than the config key the operator mistyped.

    Args:
        languages: Tesseract language codes, in the operator's order. Order
            is preserved — Tesseract treats the first code as primary.

    Returns:
        The ``+``-joined language string.

    Raises:
        OcrConfigError: When *languages* contains no usable code. An empty
            list cannot produce OCR, so it is refused here with a message
            naming the config key, rather than handed to Tesseract as ``""``.
    """
    joined = "+".join(code.strip() for code in languages if code.strip())
    if not joined:
        msg = (
            "`ocr.languages` must name at least one Tesseract language code "
            "(e.g. `[eng]`); got no usable code."
        )
        raise OcrConfigError(msg)
    return joined


_OCR_DEFAULTS: Final[OCRConfig] = OCRConfig()
"""The single authority for this module's standalone OCR defaults.

Materialising the config model, rather than restating its numbers as module
constants, is what keeps the two from drifting: before #1517 this module
carried its own ``0.6`` and its own ``"eng"`` beside a docstring promising
they mirrored :class:`~creek.config.OCRConfig`, and a promise in a docstring
is not a mechanism. Changing a default in ``OCRConfig`` now changes it here
by construction.
"""


_DEFAULT_LANGUAGE: Final[str] = join_ocr_languages(_OCR_DEFAULTS.languages)
"""Tesseract language string used when no :class:`OCRConfig` is supplied.

Derived from :data:`_OCR_DEFAULTS`, not restated.
"""


_DEFAULT_MIN_CONFIDENCE: Final[float] = _OCR_DEFAULTS.min_confidence
"""OCR confidence threshold used when no :class:`OCRConfig` is supplied.

Derived from :data:`_OCR_DEFAULTS`, not restated, so a direct
``ImageIngestor()`` (tests, ad-hoc API use) and a config-driven
``ImageIngestor.from_ocr_config`` agree by construction.
"""


_REVIEW_FRONTMATTER_KEY: str = "review"
"""Frontmatter key used to flag fragments awaiting human review."""

_REVIEW_PENDING_VALUE: str = "pending_review"
"""Value written to :data:`_REVIEW_FRONTMATTER_KEY` when OCR is uncertain."""


_IMAGE_TYPE_HINTS: dict[str, tuple[str, ...]] = {
    "screenshot": ("screenshot", "screen-shot", "screen_shot", "screencap"),
    "photo_of_text": ("scan", "scanned", "handwritten", "notebook"),
    "diagram": ("diagram", "chart", "schematic", "flowchart", "wireframe"),
}
"""Filename token hints used by :func:`detect_image_type`.

The dict is iterated in insertion order and the **first matching
category wins** (priority: screenshot > photo_of_text > diagram).
The bare token ``page`` is intentionally absent because substrings
like ``webpage`` would falsely classify "webpage screenshot" as
``photo_of_text``; ``scanned-page-3.jpg`` still matches via ``scan``.
"""


# ---- Public dataclasses + protocol --------------------------------------


@dataclass(frozen=True)
class OcrResult:
    """Result of running OCR on a single image or PDF page.

    Attributes:
        text: Extracted text. Empty when OCR returned nothing usable.
        confidence: OCR confidence in ``[0.0, 1.0]``.
        page: 1-based page index (PDFs); ``0`` for standalone images.
        image_type: One of ``screenshot``, ``photo_of_text``, ``diagram``,
            ``scanned_pdf_page``, or ``other``. Defaults to ``"other"``
            so a callers that build an ``OcrResult`` without classifying
            the image still produces a value :func:`detect_image_type`
            could return.
    """

    text: str
    confidence: float
    page: int = 0
    image_type: str = "other"


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


_TESSERACT_BINARY_MISSING_MSG: str = (
    "The `tesseract` system binary was not found on PATH. The pytesseract "
    "Python package is a thin wrapper that shells out to it, so OCR cannot "
    "run without it. Install Tesseract via your OS package manager (e.g. "
    "`brew install tesseract`, `apt-get install tesseract-ocr`, or see "
    "https://github.com/tesseract-ocr/tesseract) and ensure it is on PATH "
    "(or set `pytesseract.pytesseract.tesseract_cmd` to its location)."
)
"""Actionable remediation message for the missing-binary case (GAP-015)."""


def _tesseract_binary_available(pytesseract: Any) -> bool:
    """Return ``True`` when the configured ``tesseract`` binary is runnable.

    ``pytesseract`` is only a wrapper around the ``tesseract`` system
    binary; importing the Python package succeeds even when the binary
    is absent. We honour any configured command path
    (``pytesseract.pytesseract.tesseract_cmd``, which defaults to
    ``"tesseract"``) and resolve it via :func:`shutil.which`, falling
    back to :func:`pytesseract.get_tesseract_version` when ``which``
    cannot resolve it (e.g. an absolute ``tesseract_cmd`` path that is
    on PATH-lookup-immune but still executable).
    """
    cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    if shutil.which(cmd) is not None:
        return True
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # pytesseract raises TesseractNotFoundError and others here
        return False
    return True


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
        """Return ``True`` when OCR can actually run right now.

        Both the Python packages (``pytesseract``, ``Pillow``) **and**
        the ``tesseract`` system binary they wrap must be present.
        Importing ``pytesseract`` succeeds even when the binary is
        missing (GAP-015), so the import guard alone would wrongly
        report availability; we additionally probe the binary.
        """
        try:
            import pytesseract
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        return _tesseract_binary_available(pytesseract)

    def extract_text(self, image_path: Path) -> OcrResult:
        """Run OCR on *image_path* and return the resulting text.

        Args:
            image_path: Filesystem path to the image file.

        Returns:
            An :class:`OcrResult` carrying the extracted text and the
            average per-word confidence reported by tesseract.

        Raises:
            PytesseractUnavailableError: When ``pytesseract`` or
                ``Pillow`` cannot be imported, or when the ``tesseract``
                system binary they wrap is not installed (GAP-015).
        """
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            msg = (
                "pytesseract and Pillow are required for OCR. "
                "Install them with `pip install pytesseract pillow` "
                "and ensure the `tesseract` system binary is on PATH."
            )
            raise PytesseractUnavailableError(msg) from exc

        with Image.open(image_path) as raw:
            image = self._preprocess(raw)
            text, confidence = self._ocr_image(pytesseract, image)
        return OcrResult(
            text=text,
            confidence=confidence,
            image_type=detect_image_type(image_path),
        )

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Convert *pdf_path* to images page by page and OCR each one.

        Args:
            pdf_path: Filesystem path to the PDF.

        Returns:
            One :class:`OcrResult` per page (1-based ``page`` index).

        Raises:
            PytesseractUnavailableError: When ``pdf2image``,
                ``pytesseract``, or ``Pillow`` cannot be imported, or
                when the ``tesseract`` system binary is not installed
                (GAP-015).
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

        return self._ocr_pdf_pages(pytesseract, convert_from_path(str(pdf_path)))

    def _ocr_pdf_pages(
        self,
        pytesseract: Any,
        pages: list[Any],
    ) -> list[OcrResult]:
        """OCR each pre-rendered PDF page image into an :class:`OcrResult`."""
        results: list[OcrResult] = []
        for index, page_image in enumerate(pages, start=1):
            preprocessed = self._preprocess(page_image)
            text, confidence = self._ocr_image(pytesseract, preprocessed)
            results.append(
                OcrResult(
                    text=text,
                    confidence=confidence,
                    page=index,
                    image_type="scanned_pdf_page",
                ),
            )
        return results

    @staticmethod
    def _preprocess(image: Any) -> Any:
        """Boost contrast on *image* prior to OCR.

        Centralised so :meth:`extract_text` and :meth:`extract_pdf_pages`
        produce comparable OCR quality — previously the PDF path skipped
        contrast enhancement which yielded systematically worse results
        on faint scans.
        """
        from PIL import ImageEnhance

        return ImageEnhance.Contrast(image.convert("RGB")).enhance(2.0)

    def _ocr_image(self, pytesseract: Any, image: Any) -> tuple[str, float]:
        """Run tesseract on a preprocessed image, returning ``(text, confidence)``.

        Translates the raw ``pytesseract.TesseractNotFoundError`` (raised
        when the ``tesseract`` system binary is absent — distinct from the
        ``ImportError`` raised when the Python package is missing) into the
        curated :class:`PytesseractUnavailableError` carrying the
        install-the-binary remediation message (GAP-015). This is the single
        choke point both :meth:`extract_text` and :meth:`extract_pdf_pages`
        funnel through, so neither leaks the raw error.
        """
        try:
            text = pytesseract.image_to_string(image, lang=self.language)
            data = pytesseract.image_to_data(
                image,
                lang=self.language,
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractNotFoundError as exc:
            raise PytesseractUnavailableError(_TESSERACT_BINARY_MISSING_MSG) from exc
        confidence = _average_confidence(data.get("conf", []))
        return text.strip(), confidence


class UnknownOcrEngineError(OcrConfigError):
    """Raised when ``ocr.engine`` names an engine no registry entry provides.

    A distinct type rather than a bare :class:`OcrConfigError` so a caller
    that wants to offer "did you mean pytesseract?" can, while a surface that
    only needs to refuse the run catches the base class.
    """


OCR_ENGINES: Final[dict[str, Callable[[str], OcrEngine]]] = {
    "pytesseract": PytesseractOcrEngine,
}
"""Name → constructor for every OCR backend ``ocr.engine`` may select.

This registry is what makes ``ocr.engine`` mean something. Before #1517 the
key parsed and was then discarded, because there was nothing to resolve a
name *against*: :class:`PytesseractOcrEngine` was the only implementation and
callers reached it by constructing it directly. A config key whose values
cannot be distinguished is not a setting, it is decoration.

Each value is called with the joined language string from
:func:`join_ocr_languages` and must return an :class:`OcrEngine`. Keeping the
entry a plain callable — rather than ``type[OcrEngine]``, which a Protocol
cannot express — means a backend needing more setup can register a small
factory without subclassing anything.

Deliberately mutable: this is the seam a test (or a downstream package) uses
to register a deterministic engine and then drive the **production**
resolution path, instead of bypassing config with a hand-built ingestor.
``creek_mcp.tools.upload`` recorded the absence of exactly this seam as the
reason its image leg had no end-to-end test.
"""


def resolve_ocr_engine(name: str, language: str) -> OcrEngine:
    """Build the OCR engine ``ocr.engine`` names, or refuse by name.

    An unknown name **must not** fall back to the default engine. Silently
    substituting pytesseract for a misspelled ``ocr.engine`` is the same
    defect class #1517 exists to close — a config key the operator can set,
    which the run then ignores — only worse, because the run would appear to
    succeed under a setting that was never honoured.

    Args:
        name: The value of ``ocr.engine``.
        language: Joined Tesseract language string, from
            :func:`join_ocr_languages`.

    Returns:
        A freshly constructed :class:`OcrEngine`.

    Raises:
        UnknownOcrEngineError: When *name* is not in :data:`OCR_ENGINES`. The
            message lists every known name, because the operator's next
            action is to pick one.
    """
    factory = OCR_ENGINES.get(name)
    if factory is None:
        known = ", ".join(sorted(OCR_ENGINES))
        msg = f"Unknown OCR engine {name!r} in `ocr.engine`. Known engines: {known}."
        raise UnknownOcrEngineError(msg)
    return factory(language)


def _average_confidence(conf_values: list[Any]) -> float:
    """Compute mean confidence in ``[0, 1]`` from tesseract's per-word values.

    Tesseract reports confidences in ``0-100`` (or ``-1`` for missing).
    Empty or all-negative input returns ``0.0``.
    """
    numeric = [float(v) for v in conf_values if _is_non_negative_number(v)]
    if not numeric:
        return 0.0
    return sum(numeric) / len(numeric) / 100.0


def _is_non_negative_number(value: Any) -> bool:
    """Return ``True`` when *value* parses to a non-negative float.

    Used to filter tesseract's ``-1`` sentinel for "missing confidence"
    while preserving the legitimate ``0`` value for "low confidence".
    """
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

    Matching is **first-wins** on the insertion order of
    :data:`_IMAGE_TYPE_HINTS`: a filename matching both ``screenshot``
    and ``photo_of_text`` hints (e.g. ``screenshot-of-scanned-page.png``)
    is classified as ``screenshot``.
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
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        """Initialise the ingestor.

        Args:
            engine: Optional OCR backend. Defaults to a fresh
                :class:`PytesseractOcrEngine` when ``None``.
            language: Tesseract language code. When *engine* is
                ``None`` the value is forwarded to the default
                :class:`PytesseractOcrEngine`. When a custom engine is
                supplied the engine owns its own language settings;
                this attribute is then used **only** to tag parsed
                fragments via the ``language`` metadata key, so callers
                can reconcile per-fragment language with the engine
                that produced them.
            min_confidence: OCR-confidence threshold below which a
                produced fragment is tagged ``review: pending_review`` in
                its frontmatter. The tag is a marker for a human reader;
                no command filters on it — in particular ``creek redact
                --review`` selects by findings, not by this key (#1338).
                Defaults to :data:`_DEFAULT_MIN_CONFIDENCE`, which is read
                off :class:`~creek.config.OCRConfig` rather than restated.
        """
        self.engine = engine if engine is not None else PytesseractOcrEngine(language)
        self.language = language
        self.min_confidence = min_confidence

    @classmethod
    def from_ocr_config(cls, config: OCRConfig) -> ImageIngestor:
        """Build an ingestor that honours every key in an ``ocr`` block.

        The one place the four ``ocr.*`` keys turn into behaviour, so there
        is a single answer to "what does this setting do":

        * ``engine`` selects the backend through :func:`resolve_ocr_engine`,
          which refuses an unknown name rather than falling back;
        * ``languages`` is joined by :func:`join_ocr_languages` and reaches
          both the engine (as Tesseract's ``lang``) and the fragment
          metadata, so the two can never disagree about what was read;
        * ``min_confidence`` becomes the review-tagging threshold.

        ``enabled`` is **not** consulted here, and deliberately: a disabled
        pass must not construct an engine at all, so that decision belongs
        upstream of construction, at
        :func:`creek.ingest.pipeline.run_ingestor`. Were it honoured here,
        ``enabled: false`` would still have to resolve ``engine`` first — and
        an operator turning OCR off would be refused for the name of a
        backend they had just declared they did not want to run.

        Args:
            config: The vault's ``ocr`` block.

        Returns:
            An :class:`ImageIngestor` wired to the configured engine.

        Raises:
            UnknownOcrEngineError: When ``config.engine`` names no known
                backend.
            OcrConfigError: When ``config.languages`` holds no usable code.
        """
        language = join_ocr_languages(config.languages)
        return cls(
            engine=resolve_ocr_engine(config.engine, language),
            language=language,
            min_confidence=config.min_confidence,
        )

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Recursively find every supported image under *source_path*.

        Args:
            source_path: Directory (or single file) to scan. When given
                a single file, only image extensions in
                :data:`IMAGE_EXTENSIONS` are accepted; anything else
                returns an empty list (the document ingestor pipeline
                routes non-images elsewhere).

        Returns:
            A list of :class:`RawDocument` per discovered image.

        Note:
            Image bytes are *not* loaded into the returned
            :class:`RawDocument` because :meth:`parse` (and the OCR
            engine it delegates to) reads from disk via the file path.
            For an image-heavy vault, eagerly reading every image into
            memory at discovery time would be wasteful and could cause
            OOM on large TIFFs or scanned PDFs.
        """
        if source_path.is_file():
            paths = (
                [source_path] if source_path.suffix.lower() in IMAGE_EXTENSIONS else []
            )
        else:
            paths = [
                p
                for p in sorted(source_path.rglob("*"))
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            ]
        return [
            RawDocument(
                path=path,
                content=b"",
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

        FEAT-031: when EXIF carries ``DateTimeOriginal`` (the moment
        the shutter fired) the fragment records it on ``authored_at``;
        falls through to ``DateTime`` (modified) as the next-best
        source date, and to ``None`` when neither exists or Pillow is
        unavailable. The filesystem mtime is never substituted.

        Raises:
            PytesseractUnavailableError: When the default
                :class:`PytesseractOcrEngine` is in use and
                ``pytesseract`` / ``Pillow`` are not installed.
                Custom :class:`OcrEngine` implementations may raise
                their own errors. The base
                :meth:`Ingestor.ingest` orchestrator catches
                :class:`Exception` and records it on
                :class:`IngestResult.errors`, so callers using the
                full pipeline see a structured error rather than a
                crash.
        """
        result = self.engine.extract_text(raw.path)
        if not result.text.strip():
            logger.info("OCR produced no text for %s; skipping fragment.", raw.path)
            return []
        metadata: dict[str, Any] = {
            "original_file": str(raw.path),
            "ocr_confidence": result.confidence,
            "image_type": result.image_type,
            "language": self.language,
            "authored_at": _extract_exif_authored_at(raw.path),
        }
        self._tag_low_confidence(metadata, result.confidence)
        return [
            ParsedFragment(
                content=result.text.strip(),
                metadata=metadata,
                source_path=str(raw.path),
                timestamp=_modified_time(raw.path),
            ),
        ]

    def _tag_low_confidence(
        self,
        metadata: dict[str, Any],
        confidence: float,
    ) -> None:
        """Mark *metadata* for review when *confidence* is below the threshold.

        Mutates the supplied metadata dict in place — adding the review
        marker only when needed keeps the high-confidence path unchanged
        and avoids polluting frontmatter with an irrelevant field.

        Args:
            metadata: Fragment metadata dict to mutate.
            confidence: The OCR confidence in ``[0.0, 1.0]``.
        """
        if confidence < self.min_confidence:
            metadata[_REVIEW_FRONTMATTER_KEY] = _REVIEW_PENDING_VALUE

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Embed the original image, then the OCR'd body.

        For multi-page scanned-PDF fragments produced by
        :meth:`ingest_pdf`, the embed carries the Obsidian
        ``#page=N`` anchor so each page renders independently.
        Standalone-image fragments use the bare ``![[name]]`` embed.
        """
        original = fragment.metadata.get("original_file", fragment.source_path)
        image_name = Path(original).name
        page = fragment.metadata.get("page", 0)
        embed = f"![[{image_name}#page={page}]]" if page else f"![[{image_name}]]"
        return f"{embed}\n\n{fragment.content.strip()}\n"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Produce the YAML frontmatter for an OCR'd image fragment.

        FEAT-031: ``authored_at`` (from EXIF ``DateTimeOriginal`` or
        ``DateTime``) is rendered as ISO when extracted; absent
        otherwise so downstream readers fall through to the
        ``Fragment.authored_at = None`` default.
        """
        frontmatter: dict[str, Any] = {
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
        review_marker = fragment.metadata.get(_REVIEW_FRONTMATTER_KEY)
        if review_marker:
            frontmatter[_REVIEW_FRONTMATTER_KEY] = review_marker
        authored_at: datetime | None = fragment.metadata.get("authored_at")
        if authored_at is not None:
            frontmatter["authored_at"] = authored_at.isoformat()
        return frontmatter

    def ingest_pdf(self, pdf_path: Path) -> list[ParsedFragment]:
        """Run OCR on every page of *pdf_path* and return per-page fragments.

        This is a deliberately separate entry point — the standard
        :meth:`Ingestor.ingest` pipeline only handles file extensions
        in :data:`IMAGE_EXTENSIONS`. Scanned PDFs are routed here from
        :class:`~creek.ingest.documents.DocumentIngestor` (or a
        higher-level orchestrator) once it detects via
        ``_detect_scanned_pdf`` that text extraction won't work. Until
        that integration lands, callers can invoke this method directly
        on a known-scanned PDF. Pages with no recoverable text are
        skipped.

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
            metadata: dict[str, Any] = {
                "original_file": str(pdf_path),
                "ocr_confidence": result.confidence,
                "image_type": result.image_type,
                "language": self.language,
                "page": result.page,
            }
            self._tag_low_confidence(metadata, result.confidence)
            fragments.append(
                ParsedFragment(
                    content=result.text.strip(),
                    metadata=metadata,
                    source_path=str(pdf_path),
                    timestamp=_modified_time(pdf_path),
                ),
            )
        return fragments


def _modified_time(path: Path) -> datetime:
    """Return the file's mtime as a timezone-aware UTC datetime.

    Delegates to :func:`creek.ingest.base.file_modified_time` so the
    id-anchoring conversion rule lives in exactly one place (#1329); the
    semantics are byte-identical to the expression this replaced.
    """
    return file_modified_time(path)


# EXIF tag IDs — magic numbers chosen by the standard, not by us. Hard-coded
# rather than ``ExifTags.TAGS`` lookup so the Pillow import can stay deferred.
_EXIF_DATETIME_ORIGINAL_TAG_ID: int = 36867  # DateTimeOriginal
_EXIF_DATETIME_TAG_ID: int = 306  # DateTime

# EXIF stores dates in the format ``"YYYY:MM:DD HH:MM:SS"`` (note the
# colons separating the date components — non-ISO and a frequent source
# of bugs in naïve implementations).
_EXIF_DATETIME_FORMAT: str = "%Y:%m:%d %H:%M:%S"


def _extract_exif_authored_at(path: Path) -> datetime | None:
    """Read EXIF ``DateTimeOriginal`` (then ``DateTime``) from an image.

    FEAT-031: returns the shutter-fired moment as a UTC-defaulted
    tz-aware datetime, or ``None`` when neither tag exists, Pillow is
    unavailable, or the image carries no EXIF data (PNGs / WebPs
    often don't). The filesystem mtime is never substituted —
    downstream surfaces fall through to :attr:`Fragment.ingested`.

    EXIF datetimes are naive in the standard (no tz info); we localise
    to UTC per the FEAT-031 spec rather than guessing the camera's
    local zone. Cross-tz wall-clock accuracy is out of scope until an
    ingestor parses the (separate) EXIF ``OffsetTimeOriginal`` tag.
    """
    exif = _load_image_exif(path)
    if exif is None:
        return None
    for tag_id in (_EXIF_DATETIME_ORIGINAL_TAG_ID, _EXIF_DATETIME_TAG_ID):
        candidate = _exif_tag_to_authored_at(exif.get(tag_id))
        if candidate is not None:
            return candidate
    return None


def _load_image_exif(path: Path) -> Any | None:
    """Open *path* via Pillow and return its EXIF mapping (or ``None``).

    Centralising the optional-Pillow import + file-open failure modes
    here keeps :func:`_extract_exif_authored_at` linear and below the
    project's per-function try-block cap.
    """
    try:  # noqa: TRY101  # Separate optional-dep absence from file-open failure.
        from PIL import Image
    except ImportError:
        return None
    try:  # noqa: TRY101  # Distinct failure mode from the ImportError branch.
        with Image.open(path) as image:
            return image.getexif()
    except Exception:  # Pillow raises a wide set on bad files
        return None


def _exif_tag_to_authored_at(raw_value: object) -> datetime | None:
    """Parse a raw EXIF date tag into a tz-aware datetime (or ``None``).

    Handles two failure modes — bad EXIF string format and
    :func:`parse_authored_at` rejection — without exposing more than
    one try block.
    """
    if not raw_value:
        return None
    try:  # noqa: TRY101  # Separate EXIF string-format from parse_authored_at.
        naive = datetime.strptime(str(raw_value), _EXIF_DATETIME_FORMAT)
    except (TypeError, ValueError):
        return None
    try:  # noqa: TRY101  # Distinct failure mode from the format check above.
        return parse_authored_at(naive)
    except ValueError:
        return None


__all__ = [
    "IMAGE_EXTENSIONS",
    "ImageIngestor",
    "OcrEngine",
    "OcrResult",
    "PytesseractOcrEngine",
    "PytesseractUnavailableError",
    "detect_image_type",
]
