"""#1517: the four ``ocr.*`` keys must reach the OCR that runs, or not exist.

---------------------------------------------------------------------------
The defect, reproduced through the real CLI at HEAD
---------------------------------------------------------------------------
``creek init`` writes an ``ocr`` block into every vault's
``creek_config.yaml``. ``CreekConfig.ocr`` parses it. Nothing then reads it.
Measured before the fix, with ``ocr.enabled: false`` **and** a deliberately
bogus ``ocr.engine: NOT_A_REAL_ENGINE`` in the vault config::

    $ creek ingest --type image --input <dir-with-1-png> --vault <vault> -y
      File ".../creek/ingest/images.py", line 483, in parse
        result = self.engine.extract_text(raw.path)
      File ".../creek/ingest/images.py", line 323, in _ocr_image
        raise PytesseractUnavailableError(_TESSERACT_BINARY_MISSING_MSG) from exc
    creek.ingest.images.PytesseractUnavailableError: The `tesseract` system
    binary was not found on PATH...
    Ingest summary: 0 created, 0 updated, 0 unchanged, 0 tombed, 0 skipped
    Errors: 1

Both keys were inert: the run constructed a ``PytesseractOcrEngine`` and
attempted OCR anyway. That is the broken promise, proven by execution.

---------------------------------------------------------------------------
What these tests assert, and why in this shape
---------------------------------------------------------------------------
A test that only checks a config value reached a constructor is the vacuous
form of this fix — it would pass against a wire-in that then dropped the
value on the floor. So every test here asserts an **observable consequence**:

* ``enabled`` — a spy engine that records each ``extract_text`` call is
  asserted **never called**, and the vault is asserted to hold zero
  fragments. The spy is registered under its own name in
  :data:`~creek.ingest.images.OCR_ENGINES`, which is the same channel
  ``ocr.engine`` resolves through, so the test exercises the production
  resolution path rather than an injection back door.
* ``languages`` — the list must reach Tesseract as its ``+``-joined string,
  because that is the only spelling Tesseract accepts. Asserted on the
  engine the factory actually built.
* ``min_confidence`` — asserted on the **frontmatter bytes written to
  disk**, not on a metadata dict, and at two thresholds that straddle the
  engine's reported confidence so neither outcome can be the constant.
* ``engine`` — an unknown name must fail loudly. A silent fall back to
  pytesseract is the exact defect class this issue exists to close.

The control in :func:`_assert_control_ingest_writes_one` opens the
confidence tests: without it, "no ``review:`` key on disk" would be
satisfied by a vault with no fragments in it at all.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, ClassVar

import frontmatter
import pytest

from creek.config import OCRConfig
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.documents import (
    DocumentIngestor,
    _count_pdf_pages,
    _detect_scanned_pdf,
    _parse_pdf_to_text,
)
from creek.ingest.images import (
    _DEFAULT_LANGUAGE,
    _DEFAULT_MIN_CONFIDENCE,
    ImageIngestor,
    OcrConfigError,
    OcrResult,
    PytesseractOcrEngine,
    UnknownOcrEngineError,
    join_ocr_languages,
    resolve_ocr_engine,
)
from creek.ingest.pipeline import (
    _SCANNED_PDF_OCR_DECLINED_ADVISORY,
    _SUPERSEDED_SCAN_SAFE_TEMPLATE,
    IngestRunResult,
    build_ingestor,
    ocr_is_disabled,
    run_ingest,
)
from tests.scanned_pdf_support import SCAN_PAGES, scanned_pdf, unscannable_pdf

if TYPE_CHECKING:
    from pathlib import Path


_OCR_TEXT = "TESSERACT-SPY-1517-9f2c the gauge held at four feet"
"""Body text the spy engine returns, so a vault hit is provably this fixture."""


class SpyOcrEngine:
    """OCR engine that records every call and returns a canned result.

    Class-level call log rather than an instance one: the production path
    constructs the engine itself (that is the point of ``ocr.engine``), so a
    test has no instance to interrogate until after the run.

    Attributes:
        languages: The language string the factory passed in.
    """

    calls: ClassVar[list[str]] = []
    """Paths handed to :meth:`extract_text`, across every instance."""

    instances: ClassVar[list[SpyOcrEngine]] = []
    """Every engine the production factory constructed."""

    confidence: float = 0.9
    """Confidence the canned :class:`OcrResult` reports."""

    pdf_pages: ClassVar[tuple[str, ...]] = (_OCR_TEXT,)
    """Body text this engine reports for each page of a PDF, in page order.

    A class attribute rather than a constructor argument because the
    production path (``ocr.engine`` -> :func:`resolve_ocr_engine`) builds the
    engine itself, so a test never holds the instance before the run.
    """

    def __init__(self, language: str = "eng") -> None:
        """Record the language string the production factory chose.

        Args:
            language: Tesseract language code(s), already joined.
        """
        self.language = language
        type(self).instances.append(self)

    def is_available(self) -> bool:
        """Report availability; the spy needs no system binary."""
        return True

    def extract_text(self, image_path: Path) -> OcrResult:
        """Record the call and return the canned result.

        Args:
            image_path: Image the ingestor asked about.

        Returns:
            The canned :class:`OcrResult`.
        """
        type(self).calls.append(str(image_path))
        return OcrResult(text=_OCR_TEXT, confidence=type(self).confidence)

    def extract_pdf_pages(self, pdf_path: Path) -> list[OcrResult]:
        """Record the call and return one canned result per configured page.

        Args:
            pdf_path: PDF the ingestor asked about.

        Returns:
            One :class:`OcrResult` per entry in :attr:`pdf_pages`, numbered
            from 1 the way a real engine numbers pages.
        """
        type(self).calls.append(str(pdf_path))
        return [
            OcrResult(
                text=text,
                confidence=type(self).confidence,
                page=number,
                image_type="scanned_pdf_page",
            )
            for number, text in enumerate(type(self).pdf_pages, start=1)
        ]


@pytest.fixture
def spy_engine(monkeypatch: pytest.MonkeyPatch) -> type[SpyOcrEngine]:
    """Register :class:`SpyOcrEngine` in the production engine registry.

    Args:
        monkeypatch: Pytest monkeypatch fixture; the registry entry is
            removed again at teardown.

    Returns:
        The spy class, with its shared logs emptied.
    """
    from creek.ingest import images

    SpyOcrEngine.calls = []
    SpyOcrEngine.instances = []
    SpyOcrEngine.confidence = 0.9
    SpyOcrEngine.pdf_pages = (_OCR_TEXT,)
    monkeypatch.setitem(images.OCR_ENGINES, "spy", SpyOcrEngine)
    return SpyOcrEngine


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault tree ``run_ingest`` writes into.

    Args:
        tmp_path: Pytest-provided temporary directory to build under.

    Returns:
        Path to the scaffolded vault root.
    """
    vault = tmp_path / "vault"
    for folder in (
        "00-Creek-Meta/Processing-Log",
        # ``document`` is in LEDGERED_SOURCES, so the document route writes a
        # ledger; ``Unsorted`` is where an unclassified fragment lands.
        "00-Creek-Meta/State/ingest",
        "01-Fragments/Journal",
        "01-Fragments/Notes",
        "01-Fragments/Unsorted",
        "10-Liminal/Orphaned",
    ):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    return vault


def _make_images(tmp_path: Path) -> Path:
    """Write one real PNG into a fresh source directory.

    A real image file rather than a stub byte string: ``discover`` selects
    on extension but ``PytesseractOcrEngine`` would open it, so a genuine
    PNG keeps the fixture honest if the wire-in ever leaks the default
    engine into this path.

    Args:
        tmp_path: Pytest-provided temporary directory to build under.

    Returns:
        Path to the directory holding the image.
    """
    from PIL import Image

    source = tmp_path / "images"
    source.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 32), "white").save(source / "note.png")
    return source


def _scan_source(tmp_path: Path, *, pages: int = 3) -> tuple[Path, Path]:
    """Build a source directory holding exactly one image-only PDF.

    Args:
        tmp_path: Pytest-provided temporary directory to build under.
        pages: Page count for the PDF.

    Returns:
        ``(source_directory, pdf_path)``.
    """
    source = tmp_path / "scans"
    source.mkdir(parents=True, exist_ok=True)
    return source, scanned_pdf(source / "Scan.pdf", pages)


def _fragment_bodies(vault: Path) -> list[str]:
    """Return every live fragment's markdown body, read off the written file.

    Parsed with ``frontmatter.loads`` rather than split on ``"---\n"``: a
    fragment with an empty body ends at the closing fence with no trailing
    newline, so the split idiom returns the whole frontmatter block and
    reports a misleading failure (measured at HEAD, #1639).

    Args:
        vault: Vault root to read.

    Returns:
        Sorted list of fragment bodies, stripped.
    """
    return sorted(
        frontmatter.loads(path.read_text(encoding="utf-8")).content.strip()
        for path in _vault_fragments(vault)
    )


def _vault_fragments(vault: Path) -> list[Path]:
    """Return the live fragment files under ``01-Fragments``.

    Args:
        vault: Vault root to read.

    Returns:
        Sorted list of live fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


class TestOcrEnabledIsHonoured:
    """``ocr.enabled: false`` must stop OCR happening at all."""

    def test_disabled_never_reaches_the_engine_and_writes_nothing(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """A disabled OCR pass performs no OCR and produces no fragment."""
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)

        result = run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=False, engine="spy"),
        )

        assert spy_engine.calls == [], (
            "ocr.enabled: false still ran OCR — the engine was called with "
            f"{spy_engine.calls}"
        )
        assert result.errors == []
        assert result.written == 0
        assert _vault_fragments(vault) == []

    def test_enabled_still_ingests_through_the_configured_engine(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The control: with OCR enabled the same fixture writes a fragment.

        Without this, "nothing was written" above would be satisfied by a
        fixture the ingestor never picked up in the first place.
        """
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)

        result = run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        assert len(spy_engine.calls) == 1
        assert result.errors == []
        assert result.written == 1
        written = _vault_fragments(vault)
        assert len(written) == 1
        assert _OCR_TEXT in written[0].read_text(encoding="utf-8")


class TestOcrLanguagesReachTheEngine:
    """``ocr.languages`` must arrive in the spelling Tesseract accepts."""

    def test_language_list_is_plus_joined_for_the_engine(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """A two-code list reaches the constructed engine as ``eng+fra``."""
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)

        run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(engine="spy", languages=["eng", "fra"]),
        )

        assert [engine.language for engine in spy_engine.instances] == ["eng+fra"]

    def test_the_engine_and_the_fragment_metadata_get_the_same_string(
        self,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """Engine ``lang`` and fragment ``language`` come from one value.

        Read off the ingestor rather than off the written note because
        ``Fragment`` does not model ``language`` and pydantic's
        ``extra="ignore"`` drops it before the write — a separate provenance
        gap, filed as its own issue rather than widened into here. What is
        assertable, and what #1517 needs, is that a single joined string
        feeds both, so the engine can never be reading languages the
        fragment does not claim.
        """
        assert spy_engine is SpyOcrEngine
        ingestor = ImageIngestor.from_ocr_config(
            OCRConfig(engine="spy", languages=["deu", "eng"]),
        )

        assert ingestor.language == "deu+eng"
        assert [engine.language for engine in spy_engine.instances] == ["deu+eng"]

    def test_blank_codes_are_dropped_rather_than_joined(self) -> None:
        """``["eng", " ", ""]`` is ``eng``, never ``eng+ +``."""
        assert join_ocr_languages(["eng", " ", ""]) == "eng"

    def test_order_is_preserved_because_tesseract_reads_the_first_as_primary(
        self,
    ) -> None:
        """The join must not sort — the operator's order is meaningful."""
        assert join_ocr_languages(["fra", "eng"]) == "fra+eng"

    @pytest.mark.parametrize("languages", [[], [""], ["  ", "\t"]])
    def test_a_language_list_with_no_usable_code_is_refused(
        self,
        languages: list[str],
    ) -> None:
        """An empty language list cannot OCR, so it is refused by name."""
        with pytest.raises(OcrConfigError, match=r"ocr\.languages"):
            join_ocr_languages(languages)


class TestOcrMinConfidenceGovernsTheReviewTag:
    """``ocr.min_confidence`` must decide the on-disk ``review:`` key."""

    @staticmethod
    def _ingest_at(
        tmp_path: Path,
        *,
        min_confidence: float,
        reported: float,
    ) -> str:
        """Ingest one image and return the note's text.

        Args:
            tmp_path: Temporary directory for the vault and source.
            min_confidence: The ``ocr.min_confidence`` under test.
            reported: The confidence the spy engine reports.

        Returns:
            The full text of the single written note.
        """
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)
        SpyOcrEngine.confidence = reported

        run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(engine="spy", min_confidence=min_confidence),
        )

        written = _vault_fragments(vault)
        assert len(written) == 1, "control failed: no fragment was written"
        return written[0].read_text(encoding="utf-8")

    def test_confidence_above_the_configured_floor_is_not_flagged(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """0.55 under a 0.50 floor writes no ``review:`` key."""
        assert spy_engine is SpyOcrEngine
        note = self._ingest_at(tmp_path, min_confidence=0.5, reported=0.55)

        assert "pending_review" not in note

    def test_confidence_below_the_configured_floor_is_flagged(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The same 0.55 under a 0.90 floor writes ``review: pending_review``.

        The pair straddles ``_DEFAULT_MIN_CONFIDENCE`` (0.6): under the
        unwired code both runs used 0.6 and 0.55 was flagged either way, so
        it is the *first* of these two that catches a dropped config value
        and the second that catches a threshold hard-wired to 1.0.
        """
        assert spy_engine is SpyOcrEngine
        note = self._ingest_at(tmp_path, min_confidence=0.9, reported=0.55)

        assert "review: pending_review" in note


class TestUnknownOcrEngineFailsLoudly:
    """``ocr.engine`` must refuse a name it cannot resolve."""

    def test_unknown_engine_name_raises_rather_than_falling_back(
        self,
        tmp_path: Path,
    ) -> None:
        """A misspelled engine stops the run instead of using pytesseract."""
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)

        with pytest.raises(UnknownOcrEngineError, match="NOT_A_REAL_ENGINE"):
            run_ingest(
                ingestor_cls=ImageIngestor,
                source_type="image",
                input_path=source,
                vault_path=vault,
                ocr=OCRConfig(engine="NOT_A_REAL_ENGINE"),
            )

    def test_the_refusal_names_every_engine_the_operator_could_pick(
        self,
    ) -> None:
        """The message lists the known names, because that is the next step."""
        with pytest.raises(UnknownOcrEngineError, match="pytesseract"):
            resolve_ocr_engine("tesseract4", "eng")

    def test_a_known_engine_name_resolves_to_that_backend(self) -> None:
        """The control: the shipped default name really does resolve."""
        engine = resolve_ocr_engine("pytesseract", "eng+fra")

        assert isinstance(engine, PytesseractOcrEngine)
        assert engine.language == "eng+fra"

    def test_an_unknown_engine_is_not_resolved_when_ocr_is_off(
        self,
        tmp_path: Path,
    ) -> None:
        """Disabling OCR must not require a valid engine name.

        ``enabled`` is answered before ``engine`` is resolved, so an operator
        switching OCR off is never refused for the name of a backend they
        just said they did not want. This is the exact shape of the original
        #1517 repro, which set both keys at once.
        """
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)

        result = run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=False, engine="NOT_A_REAL_ENGINE"),
        )

        assert result.written == 0
        assert result.errors == []


class TestTheFactoryStillBuildsEveryOtherIngestor:
    """Threading OCR config must not disturb the other ten registry entries."""

    @pytest.mark.parametrize("name", sorted(INGESTOR_REGISTRY))
    def test_every_registry_entry_constructs_with_a_config_in_hand(
        self,
        name: str,
    ) -> None:
        """Each registered ingestor is still built, OCR config or not."""
        built = build_ingestor(INGESTOR_REGISTRY[name], ocr=OCRConfig())

        assert isinstance(built, INGESTOR_REGISTRY[name])

    @pytest.mark.parametrize("name", sorted(INGESTOR_REGISTRY))
    def test_every_registry_entry_constructs_without_a_config(
        self,
        name: str,
    ) -> None:
        """The ``ocr=None`` path — every API caller with no vault config."""
        built = build_ingestor(INGESTOR_REGISTRY[name], ocr=None)

        assert isinstance(built, INGESTOR_REGISTRY[name])

    def test_only_the_image_ingestor_is_switched_off_by_ocr_enabled(
        self,
    ) -> None:
        """``ocr.enabled: false`` must not silence the other ten ingestors.

        A boolean that stopped markdown ingest as a side effect would be a
        far worse defect than the dormant key it replaced.
        """
        off = OCRConfig(enabled=False)
        disabled = {
            name for name, cls in INGESTOR_REGISTRY.items() if ocr_is_disabled(cls, off)
        }

        assert disabled == {"image"}

    def test_no_ocr_config_leaves_the_image_ingestor_enabled(self) -> None:
        """``ocr=None`` is "no opinion", never "off"."""
        assert ocr_is_disabled(ImageIngestor, None) is False


class TestTheOperatorIsToldWhenOcrIsOff:
    """A silently-empty run is the mirror image of the #1517 defect."""

    def test_disabled_ocr_warns_instead_of_writing_nothing_quietly(
        self,
        tmp_path: Path,
    ) -> None:
        """The run names the config key that made it a no-op."""
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)
        seen: list[str] = []

        result = run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=False),
            on_warning=seen.append,
        )

        assert any("ocr.enabled" in message for message in result.warnings)
        assert any("ocr.enabled" in message for message in seen)
        # The advisory carries no vault content, so a remote caller gets it
        # too rather than being told nothing at all (#1372).
        assert any("ocr.enabled" in message for message in result.ceiling_safe_warnings)

    def test_an_enabled_run_raises_no_such_advisory(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The control: the advisory is not printed on every image ingest."""
        assert spy_engine is SpyOcrEngine
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)

        result = run_ingest(
            ingestor_cls=ImageIngestor,
            source_type="image",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(engine="spy"),
        )

        assert not any("ocr.enabled" in message for message in result.warnings)


class TestTheStandaloneDefaultsComeFromTheConfigModel:
    """A bare ``ImageIngestor()`` and the config must not be able to drift."""

    def test_default_min_confidence_is_the_config_field_default(self) -> None:
        """The module constant is read off ``OCRConfig``, not restated."""
        assert OCRConfig().min_confidence == _DEFAULT_MIN_CONFIDENCE
        assert ImageIngestor().min_confidence == OCRConfig().min_confidence

    def test_default_language_is_the_joined_config_field_default(self) -> None:
        """Likewise the language, through the same join production uses."""
        assert join_ocr_languages(OCRConfig().languages) == _DEFAULT_LANGUAGE
        assert ImageIngestor().language == join_ocr_languages(OCRConfig().languages)


class TestTheVaultConfigFileReachesTheOcr:
    """End to end: the YAML on disk, through ``creek ingest --type image``.

    Every test above hands ``run_ingest`` an :class:`OCRConfig` object. That
    proves the pipeline honours one, and proves nothing about whether the
    operator's ``creek_config.yaml`` ever becomes one — which is the half of
    #1517 the operator actually experiences. These drive the real Typer app
    so the load, the parse and the wire-in are all in the path.

    The registry key is ``image``, singular. ``--type images`` is not a
    value ``creek ingest`` accepts, and never was.
    """

    @staticmethod
    def _write_vault_config(vault: Path, ocr_block: str) -> None:
        """Write a minimal vault config carrying *ocr_block*.

        Args:
            vault: Vault root; ``00-Creek-Meta`` must already exist.
            ocr_block: YAML for the ``ocr`` mapping, already indented.
        """
        (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
            f"vault_path: {vault}\nocr:\n{ocr_block}",
            encoding="utf-8",
        )

    def _invoke_ingest(self, source: Path, vault: Path) -> object:
        """Run ``creek ingest --type image`` against *source*.

        Args:
            source: Directory of images to ingest.
            vault: Vault root to write into.

        Returns:
            The Typer ``Result``.
        """
        from typer.testing import CliRunner

        from creek.cli import app

        return CliRunner().invoke(
            app,
            [
                "ingest",
                "--type",
                "image",
                "--input",
                str(source),
                "--vault",
                str(vault),
                "--yes",
            ],
        )

    def test_enabled_false_in_the_vault_yaml_skips_ocr_and_says_so(
        self,
        tmp_path: Path,
    ) -> None:
        """The exact #1517 repro: both keys set, and now both honoured.

        At HEAD this same invocation raised ``PytesseractUnavailableError``
        out of ``images.py`` and reported ``Errors: 1``, because ``enabled``
        and ``engine`` were each read from YAML and then discarded.
        """
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)
        self._write_vault_config(
            vault,
            "  enabled: false\n  engine: NOT_A_REAL_ENGINE\n",
        )

        result = self._invoke_ingest(source, vault)

        assert result.exit_code == 0, result.output
        assert "PytesseractUnavailable" not in result.output
        assert "ocr.enabled" in " ".join(result.output.split())
        assert _vault_fragments(vault) == []

    def test_disabled_ocr_does_not_also_claim_the_png_is_unreadable(
        self,
        tmp_path: Path,
    ) -> None:
        """The empty-harvest warning must not contradict the OCR advisory.

        ``_warn_if_discovered_but_empty`` fires on "files present, nothing
        discovered" and concludes "none of them is a file this ingestor
        reads — Present but not read: note.png". That is false when OCR was
        switched off: the ingestor reads ``.png`` perfectly well and was told
        not to look. Printed directly under an advisory saying OCR is off, it
        sends the operator to check their file extensions instead of the one
        key that actually stopped the run — and under ``--strict`` it would
        fail the run for a deliberate configuration choice.
        """
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)
        self._write_vault_config(vault, "  enabled: false\n")

        result = self._invoke_ingest(source, vault)

        normalized = " ".join(result.output.split())
        assert "ocr.enabled" in normalized
        assert "Present but not read" not in normalized
        assert "none of them is a file this ingestor reads" not in normalized

    def test_enabled_true_in_the_vault_yaml_ingests_through_that_engine(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The control: the same YAML path, switched on, writes a fragment."""
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)
        self._write_vault_config(vault, "  enabled: true\n  engine: spy\n")

        result = self._invoke_ingest(source, vault)

        assert result.exit_code == 0, result.output
        assert len(spy_engine.calls) == 1
        written = _vault_fragments(vault)
        assert len(written) == 1
        assert _OCR_TEXT in written[0].read_text(encoding="utf-8")

    def test_an_unknown_engine_in_the_vault_yaml_refuses_the_run(
        self,
        tmp_path: Path,
    ) -> None:
        """A misspelled engine exits 2 and names the key, not a traceback."""
        vault = _make_vault(tmp_path)
        source = _make_images(tmp_path)
        self._write_vault_config(vault, "  enabled: true\n  engine: tesseract4\n")

        result = self._invoke_ingest(source, vault)

        assert result.exit_code == 2, result.output
        normalized = " ".join(result.output.split())
        assert "tesseract4" in normalized
        assert "pytesseract" in normalized


class TestTheProcessPathHonoursTheSameConfig:
    """``creek process`` reaches the image ingestor by its own route.

    ``Pipeline._run_ingestion`` loops the whole registry rather than
    resolving one ``--type``, so it is a second construction site. Wiring
    only ``creek ingest`` would leave ``ocr.enabled`` true on one command
    and false on the other for the same vault — which is the shape of
    #1517's own AC#2, and the failure mode a single-surface fix produces.
    """

    def test_process_skips_ocr_when_the_config_disables_it(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """No engine is constructed on the ``creek process`` path either."""
        from creek.config import CreekConfig
        from creek.pipeline import Pipeline

        source = _make_images(tmp_path)
        config = CreekConfig(
            vault_path=_make_vault(tmp_path),
            ocr=OCRConfig(enabled=False, engine="spy"),
        )

        Pipeline(config=config).run(
            source_path=source,
            vault_path=config.vault_path,
        )

        assert spy_engine.calls == []
        assert spy_engine.instances == []

    def test_process_runs_the_configured_engine_when_enabled(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The control: enabled, the same path builds and calls the spy.

        ``creek process`` loops the whole registry, so since #1639 it
        constructs an engine **twice** — once for ``ImageIngestor`` and once
        for ``DocumentIngestor``, which now reaches OCR by its own
        scanned-PDF route. The assertion is therefore that every engine the
        production factory built got the configured language, not that it
        built exactly one; pinning the count would make this test a tripwire
        for the registry's length rather than for ``ocr.languages``.
        """
        from creek.config import CreekConfig
        from creek.pipeline import Pipeline

        source = _make_images(tmp_path)
        config = CreekConfig(
            vault_path=_make_vault(tmp_path),
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        Pipeline(config=config).run(
            source_path=source,
            vault_path=config.vault_path,
        )

        assert spy_engine.calls == [str(source / "note.png")]
        assert spy_engine.instances != []
        assert {engine.language for engine in spy_engine.instances} == {"eng"}


class TestAScannedPdfIsRoutedToOcr:
    """#1639: a scanned PDF must not land as one empty fragment.

    ``ImageIngestor.ingest_pdf`` has OCR'd PDF pages since it was written,
    and until this change **nothing called it**. A scanned PDF ingested
    through ``creek ingest --type document`` reached
    ``DocumentIngestor._extract_pdf_content``, which asked ``pdfminer`` for
    text that does not exist, and wrote a single fragment whose body was the
    empty string. Measured at HEAD: one file, ``2024-01-05-Scan.md``, ending
    at its closing ``---`` with nothing after it, and no frontmatter key of
    any kind recording that the pages had been images.
    """

    def test_a_scanned_pdf_ingests_one_fragment_per_page_with_the_ocrd_text(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """A >=2-page image-only PDF lands as one fragment per page."""
        vault = _make_vault(tmp_path)
        source, pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        # Fixture premise, GREEN at HEAD, so a later red can only be routing.
        raw = pdf.read_bytes()
        assert _count_pdf_pages(raw) == 3
        assert _detect_scanned_pdf(_parse_pdf_to_text(raw), 3) is True
        assert _parse_pdf_to_text(raw).strip() == ""

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy", languages=["eng"]),
        )

        # An EVENT assertion: no fixture can pre-satisfy a call record.
        assert spy_engine.calls == [str(pdf)]
        # Full-body equality, which pins two things at once: the OCR text
        # reached disk (the issue's complaint), and each page rendered with
        # its own ``#page=N`` embed rather than every page embedding the
        # whole document. That embed branch in ``ImageIngestor`` was dead
        # production code before this route existed.
        assert _fragment_bodies(vault) == sorted(
            f"![[{pdf.name}#page={number}]]\n\n{text}"
            for number, text in enumerate(SCAN_PAGES, start=1)
        )

    def test_a_single_page_image_only_pdf_is_deliberately_not_routed(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The ``page_count > 1`` boundary is load-bearing, not an oversight.

        The issue asked for "a directory containing ONE image-only PDF" to be
        routed, and a one-**page** one cannot be: a single-page PDF holding
        little text is structurally indistinguishable from a single-page PDF
        that genuinely says little, and ``_detect_scanned_pdf`` refuses to
        guess. Pinned here so the fix cannot be "completed" later by moving
        the boundary and OCR-ing every short one-pager in a vault.
        """
        vault = _make_vault(tmp_path)
        source = tmp_path / "scans"
        source.mkdir()
        unscannable_pdf(source / "One.pdf")
        spy_engine.pdf_pages = SCAN_PAGES

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        assert spy_engine.calls == []
        assert len(_vault_fragments(vault)) == 1


def _fragment_texts(vault: Path) -> list[str]:
    """Return every live fragment file's **raw text**, verbatim.

    The frontmatter assertions in this module read these bytes rather than a
    ``generate_frontmatter`` return value. That distinction is the whole
    subject of #1517 and #1639: an ingestor can emit a key that
    ``Fragment.model_validate`` then discards, so a test that calls the
    generator directly passes against a writer which never wrote it.

    Args:
        vault: Vault root to read.

    Returns:
        One string per live fragment file.
    """
    return [path.read_text(encoding="utf-8") for path in _vault_fragments(vault)]


class TestTheScannedPdfRouteFailsClosed:
    """No ``ocr`` block must mean no OCR, the opposite of the image default.

    ``build_ingestor`` reads ``ocr=None`` as "use the defaults" for the image
    ingestor, i.e. OCR **on**. Every MCP surface — ``ingest``, ``drive``,
    ``upload`` — passes no block at all. Mirroring that default here would
    hand a remote caller OCR in a vault whose operator had written
    ``ocr.enabled: false``, so the scanned-PDF route declines instead. These
    tests pin today's behaviour byte-for-byte so a future widening cannot be
    made quietly.
    """

    def test_no_ocr_block_leaves_the_scan_un_ocrd_exactly_as_before(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """``ocr=None`` writes the same single empty fragment it always did."""
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
        )

        assert spy_engine.calls == []
        assert _fragment_bodies(vault) == [""]

    def test_the_un_routed_scan_still_says_on_disk_that_it_was_scanned(
        self,
        tmp_path: Path,
    ) -> None:
        """The operator is told the pages were images even with OCR off.

        ``DocumentIngestor`` has set a scanned flag since it learnt to detect
        one, and measured at HEAD it reached **no** vault file: it was written
        under ``source``, which ``FragmentSource`` does not model, so
        ``model_validate`` dropped it. An operator got an empty fragment and
        nothing to explain it. Asserted here on the raw bytes, and asserted
        *top-level*, because the nesting is what made it disappear.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
        )

        (text,) = _fragment_texts(vault)
        post = frontmatter.loads(text)
        assert post["scanned"] is True
        assert "scanned" not in post["source"]


class TestTheOcrMarkersReachTheVaultFile:
    """``page``, ``scanned`` and ``review`` must survive to disk, not a dict."""

    def test_every_ocr_marker_is_present_in_the_written_frontmatter(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """Read off the file: page number, scanned flag, review marker."""
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES
        # Below the threshold, so ``_tag_low_confidence`` marks every page.
        spy_engine.confidence = 0.2

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy", min_confidence=0.6),
        )

        posts = [frontmatter.loads(text) for text in _fragment_texts(vault)]
        assert sorted(post["page"] for post in posts) == [1, 2, 3]
        assert [post["scanned"] for post in posts] == [True, True, True]
        assert [post["review"] for post in posts] == ["pending_review"] * 3
        # Distinct titles are legibility, not loss prevention: measured, the
        # writer disambiguates identical titles with ``-1`` / ``-2`` suffixes
        # and loses no content. The per-page title is so a reader can tell
        # them apart in a vault, not so the writer can.
        assert sorted(str(post["title"]) for post in posts) == [
            "Scan — page 1",
            "Scan — page 2",
            "Scan — page 3",
        ]

    def test_the_telemetry_keys_are_deliberately_not_written(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """``ocr_confidence`` / ``image_type`` / ``language`` stay internal.

        Considered for :data:`~creek.ingest.base.PASSTHROUGH_FRONTMATTER_KEYS`
        and refused: each describes how the reading went, not what the
        fragment is. The refusal is pinned so it stays a decision rather than
        drifting into an oversight nobody re-examines.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        posts = [frontmatter.loads(text) for text in _fragment_texts(vault)]
        assert posts != []
        for post in posts:
            assert "ocr_confidence" not in post
            assert "image_type" not in post
            assert "language" not in post


class TestADisabledBlockDeclinesOnlyTheScannedLeg:
    """``ocr.enabled: false`` must not stop DOCX / TXT / HTML / RTF ingest.

    ``ocr_is_disabled`` short-circuits the **whole pass** to an empty result,
    which is right for the image ingestor — its entire job is OCR — and would
    be catastrophic for the document one, whose OCR leg is a single file
    format's edge case. So it is deliberately not widened; the decision is
    made per file, inside ``parse``.
    """

    def _make_mixed_source(self, tmp_path: Path) -> Path:
        """Build a directory holding one scanned PDF and one plain TXT."""
        source, _pdf = _scan_source(tmp_path)
        (source / "notes.txt").write_text(
            "A sibling document with real text in it.\n", encoding="utf-8"
        )
        return source

    def test_the_sibling_text_file_still_ingests(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The pass runs; only the scanned-PDF leg declines."""
        vault = _make_vault(tmp_path)
        source = self._make_mixed_source(tmp_path)

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=False, engine="spy"),
        )

        assert spy_engine.calls == []
        bodies = _fragment_bodies(vault)
        assert any("A sibling document with real text" in body for body in bodies)
        assert "" in bodies

    def test_the_operator_is_told_the_scan_was_left_un_ocrd(
        self,
        tmp_path: Path,
    ) -> None:
        """A quietly under-read file is the #1639 defect under a config key."""
        vault = _make_vault(tmp_path)
        source = self._make_mixed_source(tmp_path)
        seen: list[str] = []

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=False),
            on_warning=seen.append,
        )

        expected = _SCANNED_PDF_OCR_DECLINED_ADVISORY.format(count=1)
        assert expected in result.warnings
        assert expected in seen
        # No path and no fragment content, so it crosses an MCP tier ceiling
        # verbatim rather than being withheld from a remote caller (#1372).
        assert expected in result.ceiling_safe_warnings

    def test_an_enabled_run_raises_no_such_advisory(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The control: the advisory is not printed on every document pass."""
        vault = _make_vault(tmp_path)
        source = self._make_mixed_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        assert not any("left un-OCR" in message for message in result.warnings)


class TestABadOcrBlockRefusesTheDocumentRun:
    """An unusable ``ocr`` block must refuse loudly, never fall back."""

    def test_an_unknown_engine_refuses_before_a_file_is_read(
        self,
        tmp_path: Path,
    ) -> None:
        """The refusal escapes construction, not ``IngestResult.errors``.

        If it were raised inside ``parse`` instead, ``Ingestor._parse_safe``
        would swallow it into ``errors`` and the run would report a partial
        success. Raised from the constructor it reaches the CLI's exit 2.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)

        with pytest.raises(UnknownOcrEngineError) as excinfo:
            run_ingest(
                ingestor_cls=DocumentIngestor,
                source_type="document",
                input_path=source,
                vault_path=vault,
                ocr=OCRConfig(enabled=True, engine="NOT_A_REAL_ENGINE"),
            )

        assert "NOT_A_REAL_ENGINE" in str(excinfo.value)
        assert _vault_fragments(vault) == []

    def test_empty_languages_refuses_the_document_run(
        self,
        tmp_path: Path,
    ) -> None:
        """``ocr.languages: []`` is an unusable block, not a default."""
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)

        with pytest.raises(OcrConfigError):
            run_ingest(
                ingestor_cls=DocumentIngestor,
                source_type="document",
                input_path=source,
                vault_path=vault,
                ocr=OCRConfig(enabled=True, languages=[]),
            )

    def test_a_disabled_block_is_not_refused_for_its_engine_name(
        self,
        tmp_path: Path,
    ) -> None:
        """Turning OCR off must not resolve the backend you turned off.

        The same reasoning ``ImageIngestor.from_ocr_config`` records: an
        operator who has just declared they do not want a backend must not be
        refused for the name of it.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=False, engine="NOT_A_REAL_ENGINE"),
        )

        assert result.errors == []
        assert len(_vault_fragments(vault)) == 1


class TestTheScannedRouteDegradesWithoutCrashing:
    """A missing dependency or an unreadable PDF must not sink the pass."""

    def test_missing_pdf2image_is_reported_and_the_sibling_still_ingests(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The poppler remediation lands in ``errors``; the run continues.

        ``None`` in ``sys.modules`` is CPython's own "this import is blocked"
        sentinel, so the real ``PytesseractOcrEngine.extract_pdf_pages``
        raises its real curated message rather than a stub's imitation of one.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        (source / "notes.txt").write_text("Sibling text.\n", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "pdf2image", None)

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True),
        )

        assert any("poppler" in error for error in result.errors)
        assert any("pdf2image" in error for error in result.errors)
        assert _fragment_bodies(vault) == ["# Sibling text."]

    def test_a_scan_ocr_recovered_nothing_from_does_not_vanish(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """An all-blank OCR result must not delete the file from the run.

        ``ingest_pdf`` skips pages it recovered no text from, so a scan it
        could read nothing at all from returns an empty list. Returning that
        would make the PDF disappear from the run entirely — strictly worse
        than the empty fragment this issue was filed about, because an empty
        fragment at least tells the operator the file exists. The route falls
        through to the un-routed shape instead.
        """
        vault = _make_vault(tmp_path)
        source, pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = ("", "   ", "")

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        # OCR really was attempted; this is the empty-result path, not the
        # route being skipped.
        assert spy_engine.calls == [str(pdf)]
        assert result.errors == []
        # Named first, so the failure reads "the file vanished" rather than
        # an unpacking error three lines later.
        assert len(_vault_fragments(vault)) == 1
        (text,) = _fragment_texts(vault)
        post = frontmatter.loads(text)
        assert post.content.strip() == ""
        assert post["scanned"] is True

    def test_a_pdf_whose_page_count_cannot_be_read_still_ingests(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """No page count means no ``scanned`` key, so the route is not taken.

        ``_add_pdf_metadata`` already swallows an unreadable page count, and
        the routing decision reads the key that swallowing leaves unset — so
        it degrades exactly the way the metadata does, with no branch of its
        own to get this wrong.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)

        def _unreadable(_pdf_bytes: bytes) -> int:
            raise ValueError("cannot count pages")

        monkeypatch.setattr("creek.ingest.documents._count_pdf_pages", _unreadable)

        result = run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        assert result.errors == []
        assert spy_engine.calls == []
        assert len(_vault_fragments(vault)) == 1

    def test_the_scanned_route_extracts_the_pdf_text_exactly_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """Detection costs one extraction; the route must not add more.

        An unrouted PDF already pays two ``_parse_pdf_to_text`` calls
        (``_extract_pdf_content`` and ``_add_pdf_metadata``). Routing after
        metadata extraction and returning before content extraction takes the
        scanned path down to one — the cheapest of the three, on the file
        where extraction is guaranteed to find nothing.
        """
        from creek.ingest import documents as documents_module

        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES
        real = documents_module._parse_pdf_to_text
        calls: list[int] = []

        def _counted(pdf_bytes: bytes) -> str:
            calls.append(len(pdf_bytes))
            return str(real(pdf_bytes))

        monkeypatch.setattr(documents_module, "_parse_pdf_to_text", _counted)

        run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=OCRConfig(enabled=True, engine="spy"),
        )

        assert len(calls) == 1


class TestTurningOcrOnAfterAScanWasAlreadyIngested:
    """The off->on upgrade must not silently duplicate the fragment.

    This is the transition every existing user takes by default, not an
    opt-in: ``CreekConfig().ocr`` defaults to ``enabled=True`` and ``creek
    ingest`` passes it unconditionally at both call sites. Anyone who
    ingested a scanned PDF before the OCR route existed holds one fragment
    keyed ``external/<hash>/Scan.pdf`` with an empty body. Their next
    ordinary run writes three more under ``…Scan.pdf#page-1/2/3`` and leaves
    the original untouched — four live fragments for one PDF, permanently,
    because ``document`` is not in ``TOMBING_SOURCES`` so nothing sweeps it.

    ``collapsed_unit_warning`` exists for exactly this "one fragment
    superseded by N" shape and **cannot** fire here: it recomputes the legacy
    id from ``parsed.content``, and content is precisely what the OCR
    changed, so it computes an id no vault ever held. Detection has to key on
    the one identifier OCR does not change — the ledger's ``source_key``.

    Left alone rather than tombed, following the answer #1305 and #1304
    ratified for this shape: an ingest run does not delete vault content,
    because it cannot know whether the operator has since edited, linked or
    curated that fragment. Tombing would additionally require adding
    ``document`` to ``TOMBING_SOURCES``, which reverses #1329's deliberate
    split of identity from deletion authority.
    """

    def _ingest(
        self,
        source: Path,
        vault: Path,
        ocr: OCRConfig | None,
        seen: list[str] | None = None,
    ) -> IngestRunResult:
        """Run one document ingest, optionally capturing the warning stream.

        Args:
            source: Directory to ingest.
            vault: Vault root to write into.
            ocr: The ``ocr`` block for this run, or ``None``.
            seen: When given, collects every advisory as the operator sees it.

        Returns:
            The run's result.
        """
        return run_ingest(
            ingestor_cls=DocumentIngestor,
            source_type="document",
            input_path=source,
            vault_path=vault,
            ocr=ocr,
            on_warning=None if seen is None else seen.append,
        )

    def test_the_operator_is_told_which_fragment_the_pages_superseded(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """Turning OCR on must name the stale fragment, not write in silence."""
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        first = self._ingest(source, vault, OCRConfig(enabled=False))
        assert first.created == 1
        (stale,) = _vault_fragments(vault)
        stale_post = frontmatter.loads(stale.read_text(encoding="utf-8"))
        stale_id = str(stale_post["id"])
        assert stale_post.content.strip() == ""

        seen: list[str] = []
        second = self._ingest(
            source, vault, OCRConfig(enabled=True, engine="spy"), seen
        )

        assert second.created == 3
        # The advisory names the superseded fragment, so the operator can act
        # in one step instead of finding a duplicate months later.
        assert any(stale_id in message for message in second.warnings)
        assert any(stale_id in message for message in seen)
        # The ceiling-safe rendering carries the count and no fragment id,
        # because an id is vault content and must not cross an MCP ceiling.
        assert _SUPERSEDED_SCAN_SAFE_TEMPLATE.format(count=1) in (
            second.ceiling_safe_warnings
        )
        assert not any(stale_id in message for message in second.ceiling_safe_warnings)

    def test_the_advisory_reaches_the_operator_through_the_real_cli(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """Asserted on the console, not on ``result.warnings``.

        An advisory that only ever appears in a dataclass field is one the
        operator never sees. This drives the real Typer app, so
        ``creek ingest``'s ``on_warning=_print_ingest_warning`` seam and the
        vault's own ``creek_config.yaml`` are both in the path — the half of
        the fix the operator actually experiences.
        """
        from typer.testing import CliRunner

        from creek.cli import app

        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES
        config = vault / "00-Creek-Meta" / "creek_config.yaml"

        def invoke() -> object:
            """Run ``creek ingest --type document`` against the fixture."""
            return CliRunner().invoke(
                app,
                [
                    "ingest",
                    "--type",
                    "document",
                    "--input",
                    str(source),
                    "--vault",
                    str(vault),
                    "--yes",
                ],
            )

        config.write_text(
            f"vault_path: {vault}\nocr:\n  enabled: false\n  engine: spy\n",
            encoding="utf-8",
        )
        assert invoke().exit_code == 0
        (stale,) = _vault_fragments(vault)
        stale_id = str(frontmatter.loads(stale.read_text(encoding="utf-8"))["id"])

        # The operator turns OCR on -- the default next run for an upgrade.
        config.write_text(
            f"vault_path: {vault}\nocr:\n  enabled: true\n  engine: spy\n",
            encoding="utf-8",
        )
        result = invoke()

        assert result.exit_code == 0, result.output
        # Rich wraps the console at the pinned terminal width, so the id can
        # land across a line break; compare on the whitespace-collapsed text.
        printed = " ".join(result.output.split())
        assert stale_id in printed
        assert "superseded" in printed

    def test_the_advisory_is_silent_when_there_was_no_earlier_ingest(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """The control: a first-ever OCR'd ingest supersedes nothing.

        Without it, "the operator is warned" would be satisfied by an
        advisory that fired on every scanned PDF anyone ever ingested.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        result = self._ingest(source, vault, OCRConfig(enabled=True, engine="spy"))

        assert result.created == 3
        assert not any("superseded" in message for message in result.warnings)

    def test_the_advisory_clears_once_the_stale_fragment_is_removed(
        self,
        tmp_path: Path,
        spy_engine: type[SpyOcrEngine],
    ) -> None:
        """It is self-clearing, so acting on it makes the run go quiet.

        An advisory that survives the fix it asks for is one the operator
        learns to ignore, which is how a real warning becomes console noise.
        """
        vault = _make_vault(tmp_path)
        source, _pdf = _scan_source(tmp_path)
        spy_engine.pdf_pages = SCAN_PAGES

        self._ingest(source, vault, OCRConfig(enabled=False))
        (stale,) = _vault_fragments(vault)
        self._ingest(source, vault, OCRConfig(enabled=True, engine="spy"))
        assert len(_vault_fragments(vault)) == 4

        # The operator does what the advisory asked.
        stale.unlink()
        third = self._ingest(source, vault, OCRConfig(enabled=True, engine="spy"))

        assert len(_vault_fragments(vault)) == 3
        assert not any("superseded" in message for message in third.warnings)
