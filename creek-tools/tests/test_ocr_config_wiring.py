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

from typing import TYPE_CHECKING, ClassVar

import pytest

from creek.config import OCRConfig
from creek.ingest import INGESTOR_REGISTRY
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
from creek.ingest.pipeline import build_ingestor, ocr_is_disabled, run_ingest

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
        """Return one canned page result.

        Args:
            pdf_path: PDF the ingestor asked about.

        Returns:
            A single-page list of canned results.
        """
        type(self).calls.append(str(pdf_path))
        return [OcrResult(text=_OCR_TEXT, confidence=type(self).confidence, page=1)]


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
        "01-Fragments/Journal",
        "01-Fragments/Notes",
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
        """The control: enabled, the same path builds and calls the spy."""
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
        assert [engine.language for engine in spy_engine.instances] == ["eng"]
