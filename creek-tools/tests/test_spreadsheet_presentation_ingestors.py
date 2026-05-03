"""Tests for the spreadsheet and presentation ingestors (#57)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.ingest.presentations import (
    PRESENTATION_EXTENSIONS,
    PresentationBackend,
    PresentationData,
    PresentationIngestor,
    PythonPptxBackend,
    PythonPptxUnavailableError,
    SlideData,
)
from creek.ingest.spreadsheets import (
    SPREADSHEET_EXTENSIONS,
    OpenpyxlBackend,
    OpenpyxlUnavailableError,
    SheetData,
    SpreadsheetBackend,
    SpreadsheetIngestor,
    WorkbookData,
)
from creek.models import SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path


# ---- Stub backends -----------------------------------------------------


class StubSpreadsheetBackend:
    """Deterministic spreadsheet backend for tests."""

    def __init__(self, workbooks: dict[str, WorkbookData] | None = None) -> None:
        """Seed the backend with canned workbook data keyed by filename."""
        self._workbooks = dict(workbooks or {})
        self.calls: list[str] = []

    def is_available(self) -> bool:
        """Stubs are always available."""
        return True

    def read_workbook(self, path: Path) -> WorkbookData:
        """Return the canned workbook for *path*."""
        self.calls.append(f"read_workbook:{path.name}")
        return self._workbooks[path.name]


class StubPresentationBackend:
    """Deterministic presentation backend for tests."""

    def __init__(
        self,
        presentations: dict[str, PresentationData] | None = None,
    ) -> None:
        """Seed the backend with canned presentation data keyed by filename."""
        self._presentations = dict(presentations or {})
        self.calls: list[str] = []

    def is_available(self) -> bool:
        """Stubs are always available."""
        return True

    def read_presentation(self, path: Path) -> PresentationData:
        """Return the canned presentation for *path*."""
        self.calls.append(f"read_presentation:{path.name}")
        return self._presentations[path.name]


def _make_import_blocker(blocked: set[str]) -> object:
    """Build a ``__import__`` replacement that raises for *blocked*."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(
        name: str,
        module_globals: dict[str, object] | None = None,
        module_locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        """Raise ImportError for blocked roots; defer everything else."""
        root = name.split(".", 1)[0]
        if root in blocked or name in blocked:
            msg = f"mocked-missing: {name}"
            raise ImportError(msg)
        return real_import(name, module_globals, module_locals, fromlist, level)

    return _blocked_import


# ---- Helpers -----------------------------------------------------------


def _write_xlsx_placeholder(path: Path) -> None:
    """Write a minimal XLSX magic-byte placeholder (parsed via stub backend)."""
    path.write_bytes(b"PK\x03\x04 placeholder")


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    """Write a comma-separated value file at *path*."""
    import csv

    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def _write_pptx_placeholder(path: Path) -> None:
    """Write a minimal PPTX magic-byte placeholder (parsed via stub backend)."""
    path.write_bytes(b"PK\x03\x04 placeholder pptx")


# =======================================================================
# SpreadsheetIngestor
# =======================================================================


class TestModuleConstants:
    """Public constants exposed by the spreadsheet module."""

    def test_extensions_cover_xlsx_and_csv(self) -> None:
        """Both .xlsx and .csv are recognised."""
        assert ".xlsx" in SPREADSHEET_EXTENSIONS
        assert ".csv" in SPREADSHEET_EXTENSIONS


class TestSheetData:
    """Invariants on the :class:`SheetData` value object."""

    def test_immutable_rows_are_tuples(self) -> None:
        """Rows are stored as tuples of tuples — immutable end-to-end."""
        sheet = SheetData(
            name="Q1",
            headers=("month", "amount"),
            rows=(("Jan", "100"), ("Feb", "200")),
        )
        assert isinstance(sheet.rows, tuple)
        assert isinstance(sheet.rows[0], tuple)


# ---- discover ---------------------------------------------------------


class TestSpreadsheetDiscover:
    """`SpreadsheetIngestor.discover` finds the right files."""

    def test_finds_xlsx_and_csv(self, tmp_path: Path) -> None:
        """Both .xlsx and .csv files are discovered."""
        _write_xlsx_placeholder(tmp_path / "a.xlsx")
        _write_csv(tmp_path / "b.csv", [["h"], ["v"]])
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        raws = ingestor.discover(tmp_path)
        assert {raw.path.name for raw in raws} == {"a.xlsx", "b.csv"}

    def test_ignores_non_spreadsheet_files(self, tmp_path: Path) -> None:
        """Files with unsupported extensions are skipped."""
        _write_xlsx_placeholder(tmp_path / "ok.xlsx")
        (tmp_path / "note.md").write_text("# md")
        (tmp_path / "shot.png").write_bytes(b"\x89PNG")
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        raws = ingestor.discover(tmp_path)
        assert [raw.path.name for raw in raws] == ["ok.xlsx"]

    def test_recursive_discovery(self, tmp_path: Path) -> None:
        """Files in subdirectories are found."""
        nested = tmp_path / "deep"
        nested.mkdir()
        _write_xlsx_placeholder(nested / "deep.xlsx")
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        raws = ingestor.discover(tmp_path)
        assert any("deep.xlsx" in str(raw.path) for raw in raws)

    def test_extension_match_is_case_insensitive(self, tmp_path: Path) -> None:
        """``.XLSX`` and ``.CSV`` count too."""
        _write_xlsx_placeholder(tmp_path / "shout.XLSX")
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        raws = ingestor.discover(tmp_path)
        assert {raw.path.name for raw in raws} == {"shout.XLSX"}

    def test_single_image_file_returns_empty(self, tmp_path: Path) -> None:
        """A single non-spreadsheet file returns an empty discovery."""
        png = tmp_path / "x.png"
        png.write_bytes(b"\x89PNG")
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        assert ingestor.discover(png) == []

    def test_does_not_load_file_bytes(self, tmp_path: Path) -> None:
        """Discovery records the path but does not slurp the bytes."""
        path = tmp_path / "huge.xlsx"
        _write_xlsx_placeholder(path)
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        raws = ingestor.discover(tmp_path)
        assert raws[0].content == b""


# ---- parse / ingest ---------------------------------------------------


class TestSpreadsheetParse:
    """Each sheet becomes a :class:`ParsedFragment`."""

    def test_one_fragment_per_sheet(self, tmp_path: Path) -> None:
        """A workbook with two sheets yields two fragments."""
        path = tmp_path / "report.xlsx"
        _write_xlsx_placeholder(path)
        backend = StubSpreadsheetBackend(
            workbooks={
                "report.xlsx": WorkbookData(
                    sheets=(
                        SheetData(
                            name="Q1",
                            headers=("month", "amount"),
                            rows=(("Jan", "100"),),
                        ),
                        SheetData(
                            name="Q2",
                            headers=("month", "amount"),
                            rows=(("Apr", "300"),),
                        ),
                    ),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        raws = ingestor.discover(tmp_path)
        fragments = ingestor.parse(raws[0])
        assert len(fragments) == 2
        assert {f.metadata["sheet"] for f in fragments} == {"Q1", "Q2"}

    def test_fragment_metadata_records_dimensions(self, tmp_path: Path) -> None:
        """``rows`` / ``columns`` are recorded on each fragment's metadata."""
        path = tmp_path / "data.xlsx"
        _write_xlsx_placeholder(path)
        backend = StubSpreadsheetBackend(
            workbooks={
                "data.xlsx": WorkbookData(
                    sheets=(
                        SheetData(
                            name="Sheet1",
                            headers=("a", "b", "c"),
                            rows=(("1", "2", "3"), ("4", "5", "6")),
                        ),
                    ),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments[0].metadata["rows"] == 2
        assert fragments[0].metadata["columns"] == 3

    def test_empty_sheet_yields_no_fragment(self, tmp_path: Path) -> None:
        """A sheet with no headers and no rows is dropped."""
        path = tmp_path / "blank.xlsx"
        _write_xlsx_placeholder(path)
        backend = StubSpreadsheetBackend(
            workbooks={
                "blank.xlsx": WorkbookData(
                    sheets=(SheetData(name="Empty", headers=None, rows=()),),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments == []


# ---- convert_to_markdown ----------------------------------------------


class TestSpreadsheetMarkdown:
    """Markdown rendering uses GFM tables."""

    def test_small_sheet_renders_full_table(self, tmp_path: Path) -> None:
        """A small sheet (≤ summary threshold) renders every row."""
        path = tmp_path / "small.xlsx"
        _write_xlsx_placeholder(path)
        backend = StubSpreadsheetBackend(
            workbooks={
                "small.xlsx": WorkbookData(
                    sheets=(
                        SheetData(
                            name="Sheet1",
                            headers=("month", "amount"),
                            rows=(("Jan", "100"), ("Feb", "200")),
                        ),
                    ),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "| month | amount |" in markdown
        assert "| --- | --- |" in markdown
        assert "| Jan | 100 |" in markdown
        assert "| Feb | 200 |" in markdown

    def test_large_sheet_is_summarised(self, tmp_path: Path) -> None:
        """A sheet > summary threshold shows first/last N rows + total count."""
        path = tmp_path / "huge.xlsx"
        _write_xlsx_placeholder(path)
        big_rows = tuple((str(i), f"v{i}") for i in range(150))
        backend = StubSpreadsheetBackend(
            workbooks={
                "huge.xlsx": WorkbookData(
                    sheets=(
                        SheetData(
                            name="Big",
                            headers=("idx", "value"),
                            rows=big_rows,
                        ),
                    ),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "150 rows" in markdown  # Total count surfaced.
        assert "v0" in markdown  # First few rows kept.
        assert "v149" in markdown  # Last few rows kept.
        # Middle rows trimmed:
        assert "v75" not in markdown

    def test_large_sheet_renders_single_header_after_summary(
        self,
        tmp_path: Path,
    ) -> None:
        """Summary note precedes a single GFM table — no duplicate header."""
        path = tmp_path / "huge.xlsx"
        _write_xlsx_placeholder(path)
        big_rows = tuple((str(i), f"v{i}") for i in range(150))
        backend = StubSpreadsheetBackend(
            workbooks={
                "huge.xlsx": WorkbookData(
                    sheets=(
                        SheetData(
                            name="Big",
                            headers=("idx", "value"),
                            rows=big_rows,
                        ),
                    ),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        header_line = "| idx | value |"
        # The header row should appear exactly once.
        assert markdown.count(header_line) == 1
        # The separator row should appear exactly once too.
        assert markdown.count("| --- | --- |") == 1
        # The summary note should appear before the header in the document.
        summary_idx = markdown.index("Showing first")
        header_idx = markdown.index(header_line)
        assert summary_idx < header_idx, (
            "Summary note should precede the (single) header row."
        )

    def test_pipe_and_newline_in_cell_are_escaped(self, tmp_path: Path) -> None:
        """Cells containing ``|`` or newlines do not corrupt GFM tables."""
        path = tmp_path / "escapes.xlsx"
        _write_xlsx_placeholder(path)
        backend = StubSpreadsheetBackend(
            workbooks={
                "escapes.xlsx": WorkbookData(
                    sheets=(
                        SheetData(
                            name="Sheet1",
                            headers=("a|b", "c"),
                            rows=(("x | y", "line1\nline2"),),
                        ),
                    ),
                ),
            },
        )
        ingestor = SpreadsheetIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        # The literal cell text must be present in escaped form.
        assert r"a\|b" in markdown
        assert r"x \| y" in markdown
        # Newlines inside a cell must not split the table row.
        assert "line1<br>line2" in markdown
        # Every non-empty markdown line that begins with `|` should have
        # the same column count (3 pipes for 2 columns).
        table_rows = [line for line in markdown.splitlines() if line.startswith("|")]
        column_counts = {line.count("|") - line.count(r"\|") for line in table_rows}
        assert column_counts == {3}, (
            f"Mismatched pipe counts indicate broken escaping: {column_counts}"
        )


# ---- generate_frontmatter ---------------------------------------------


class TestSpreadsheetFrontmatter:
    """Frontmatter shape for spreadsheet fragments."""

    def test_frontmatter_records_platform_and_dimensions(
        self,
        tmp_path: Path,
    ) -> None:
        """Frontmatter has source.platform=spreadsheet plus sheet/row/col counts."""
        from datetime import UTC, datetime

        from creek.ingest.base import ParsedFragment

        fragment = ParsedFragment(
            content="",
            metadata={
                "original_file": str(tmp_path / "x.xlsx"),
                "sheet": "Q1",
                "rows": 5,
                "columns": 3,
            },
            source_path=str(tmp_path / "x.xlsx"),
            timestamp=datetime(2026, 4, 27, tzinfo=UTC),
        )
        ingestor = SpreadsheetIngestor(backend=StubSpreadsheetBackend())
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == SourcePlatform.SPREADSHEET.value
        assert fm["sheet"] == "Q1"
        assert fm["rows"] == 5
        assert fm["columns"] == 3


# ---- CSV path --------------------------------------------------------


class TestCsvFiles:
    """CSV files are read directly through Python's stdlib."""

    def test_csv_renders_as_single_sheet(self, tmp_path: Path) -> None:
        """A CSV file is parsed as one sheet named after the file."""
        csv_path = tmp_path / "rows.csv"
        _write_csv(csv_path, [["a", "b"], ["1", "2"], ["3", "4"]])
        # Use the production OpenpyxlBackend; CSV parsing does not
        # require the optional openpyxl dep.
        ingestor = SpreadsheetIngestor(backend=OpenpyxlBackend())
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert len(fragments) == 1
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "| a | b |" in markdown
        assert "| 1 | 2 |" in markdown

    def test_csv_with_utf8_bom_is_decoded(self, tmp_path: Path) -> None:
        """CSV files saved with a UTF-8 BOM (common from Excel) decode cleanly."""
        csv_path = tmp_path / "bom.csv"
        # Real-world Excel "CSV UTF-8" exports prepend the BOM.
        csv_path.write_bytes(
            "﻿name,city\nAlice,Münster\nBob,São Paulo\n".encode(),
        )
        ingestor = SpreadsheetIngestor(backend=OpenpyxlBackend())
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        # Header row must not carry the BOM character.
        assert "﻿" not in markdown
        assert "| name | city |" in markdown
        assert "Münster" in markdown
        assert "São Paulo" in markdown

    def test_csv_with_cp1252_falls_back(self, tmp_path: Path) -> None:
        """CSV files saved as CP1252 (legacy Excel default) decode without crashing."""
        csv_path = tmp_path / "legacy.csv"
        csv_path.write_bytes("name,note\nAlice,café\n".encode("cp1252"))
        ingestor = SpreadsheetIngestor(backend=OpenpyxlBackend())
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "| name | note |" in markdown
        assert "café" in markdown

    def test_csv_shift_jis_decodes_with_chardet(self, tmp_path: Path) -> None:
        """A Shift-JIS CSV is decoded via the chardet probe (BUG-010).

        Without the probe, ``cp1252`` would silently turn the multi-byte
        Japanese sequences into mojibake. ``chardet`` needs a generous
        sample to reach the confidence threshold, so this test uses
        a long, varied corpus rather than a couple of rows.
        """
        csv_path = tmp_path / "shift_jis.csv"
        # A varied phrase repeated until chardet has hundreds of bytes
        # of distinguishing signal — small samples produce low
        # confidence and would (correctly) fall through to cp1252.
        phrase = (
            "名前,都市,メモ\n"
            "田中,東京,ありがとうございます\n"
            "鈴木,大阪,初めまして、よろしくお願いします\n"
            "佐藤,京都,日本語のテキストはマルチバイトです\n"
        )
        rows = phrase * 60
        csv_path.write_bytes(rows.encode("shift_jis"))
        ingestor = SpreadsheetIngestor(backend=OpenpyxlBackend())
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        # If we had decoded as cp1252 the kanji would be replaced by
        # Latin-1 characters. The exact byte-for-byte output depends on
        # chardet's choice (Shift-JIS / cp932 are equivalent for these
        # characters), so the test asserts that at least one of the
        # input kanji round-trips intact.
        assert any(ch in markdown for ch in "東京名前田中大阪京都")

    def test_csv_cp1252_fallback_logs_warning(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The cp1252 fallback logs a WARNING with the file path (BUG-010).

        Pure-ASCII content fits every codec, so chardet declines to
        commit (its top guess is ``ascii``, which we reject in favour
        of cp1252 to keep behaviour stable). The warning lets the user
        spot mojibake before it lands in the vault.
        """
        import logging

        csv_path = tmp_path / "ascii.csv"
        # Bytes invalid as utf-8 force the fallback path even on
        # shorter samples where chardet is uncertain.
        csv_path.write_bytes(b"name,note\r\nAlice,caf\xe9\r\n")
        ingestor = SpreadsheetIngestor(backend=OpenpyxlBackend())
        with caplog.at_level(logging.WARNING, logger="creek.ingest.spreadsheets"):
            ingestor.parse(ingestor.discover(tmp_path)[0])
        assert any(
            "decoded as cp1252" in record.message and "ascii.csv" in record.message
            for record in caplog.records
        )


# ---- OpenpyxlBackend lazy-import ---------------------------------------


class TestOpenpyxlBackend:
    """Default backend respects the optional-dep contract."""

    def test_is_available_returns_false_without_openpyxl(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without openpyxl, the backend reports unavailable."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"openpyxl"}),
        )
        backend = OpenpyxlBackend()
        assert not backend.is_available()

    def test_xlsx_read_raises_without_openpyxl(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Reading an .xlsx without openpyxl raises a clear error."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"openpyxl"}),
        )
        path = tmp_path / "x.xlsx"
        _write_xlsx_placeholder(path)
        backend = OpenpyxlBackend()
        with pytest.raises(OpenpyxlUnavailableError, match="openpyxl"):
            backend.read_workbook(path)


# =======================================================================
# PresentationIngestor
# =======================================================================


class TestPresentationConstants:
    """Public constants exposed by the presentation module."""

    def test_extensions_cover_pptx(self) -> None:
        """``.pptx`` is the only supported extension."""
        assert ".pptx" in PRESENTATION_EXTENSIONS


class TestSlideData:
    """Invariants on the :class:`SlideData` value object."""

    def test_default_notes_is_empty(self) -> None:
        """Slides without speaker notes carry an empty notes string."""
        slide = SlideData(index=1, title="Title", body="Body")
        assert slide.notes == ""


# ---- discover ---------------------------------------------------------


class TestPresentationDiscover:
    """`PresentationIngestor.discover` finds .pptx files."""

    def test_finds_pptx(self, tmp_path: Path) -> None:
        """``.pptx`` files are discovered."""
        _write_pptx_placeholder(tmp_path / "deck.pptx")
        ingestor = PresentationIngestor(backend=StubPresentationBackend())
        raws = ingestor.discover(tmp_path)
        assert [raw.path.name for raw in raws] == ["deck.pptx"]

    def test_ignores_non_pptx_files(self, tmp_path: Path) -> None:
        """Other extensions are skipped."""
        _write_pptx_placeholder(tmp_path / "ok.pptx")
        (tmp_path / "ignored.docx").write_bytes(b"PK")
        ingestor = PresentationIngestor(backend=StubPresentationBackend())
        raws = ingestor.discover(tmp_path)
        assert [raw.path.name for raw in raws] == ["ok.pptx"]


# ---- parse / convert_to_markdown ---------------------------------------


class TestPresentationParse:
    """One :class:`ParsedFragment` per presentation, with all slides inlined."""

    def test_parse_returns_one_fragment(self, tmp_path: Path) -> None:
        """A single presentation file yields a single fragment."""
        path = tmp_path / "deck.pptx"
        _write_pptx_placeholder(path)
        backend = StubPresentationBackend(
            presentations={
                "deck.pptx": PresentationData(
                    title="My Talk",
                    slides=(
                        SlideData(
                            index=1,
                            title="Hello",
                            body="World",
                            notes="speaker note",
                        ),
                        SlideData(
                            index=2,
                            title="Two",
                            body="Bullet 1\nBullet 2",
                        ),
                    ),
                ),
            },
        )
        ingestor = PresentationIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert len(fragments) == 1
        assert fragments[0].metadata["slide_count"] == 2
        assert fragments[0].metadata["title"] == "My Talk"

    def test_markdown_renders_one_section_per_slide(
        self,
        tmp_path: Path,
    ) -> None:
        """The markdown body has ``## Slide 1: Hello`` style sections."""
        path = tmp_path / "deck.pptx"
        _write_pptx_placeholder(path)
        backend = StubPresentationBackend(
            presentations={
                "deck.pptx": PresentationData(
                    title="My Talk",
                    slides=(
                        SlideData(
                            index=1,
                            title="Hello",
                            body="World",
                            notes="speaker note",
                        ),
                        SlideData(index=2, title="Two", body="Bullet"),
                    ),
                ),
            },
        )
        ingestor = PresentationIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "## Slide 1: Hello" in markdown
        assert "## Slide 2: Two" in markdown
        assert "World" in markdown
        assert "Bullet" in markdown
        # Speaker notes get a labelled subsection.
        assert "speaker note" in markdown

    def test_empty_presentation_yields_no_fragment(
        self,
        tmp_path: Path,
    ) -> None:
        """A presentation with no slides is dropped."""
        path = tmp_path / "empty.pptx"
        _write_pptx_placeholder(path)
        backend = StubPresentationBackend(
            presentations={
                "empty.pptx": PresentationData(title=None, slides=()),
            },
        )
        ingestor = PresentationIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments == []


# ---- generate_frontmatter ---------------------------------------------


class TestPresentationFrontmatter:
    """Frontmatter shape for presentation fragments."""

    def test_frontmatter_records_platform_and_slide_count(
        self,
        tmp_path: Path,
    ) -> None:
        """source.platform=presentation plus slide_count and title."""
        from datetime import UTC, datetime

        from creek.ingest.base import ParsedFragment

        fragment = ParsedFragment(
            content="",
            metadata={
                "original_file": str(tmp_path / "deck.pptx"),
                "title": "My Talk",
                "slide_count": 4,
            },
            source_path=str(tmp_path / "deck.pptx"),
            timestamp=datetime(2026, 4, 27, tzinfo=UTC),
        )
        ingestor = PresentationIngestor(backend=StubPresentationBackend())
        fm = ingestor.generate_frontmatter(fragment)
        assert fm["source"]["platform"] == SourcePlatform.PRESENTATION.value
        assert fm["title"] == "My Talk"
        assert fm["slide_count"] == 4


# ---- PythonPptxBackend lazy-import -----------------------------------


class TestPythonPptxBackend:
    """Default backend respects the optional-dep contract."""

    def test_is_available_returns_false_without_pptx(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without python-pptx, the backend reports unavailable."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"pptx"}),
        )
        backend = PythonPptxBackend()
        assert not backend.is_available()

    def test_read_raises_without_pptx(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Reading a .pptx without python-pptx raises a clear error."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker({"pptx"}),
        )
        path = tmp_path / "x.pptx"
        _write_pptx_placeholder(path)
        backend = PythonPptxBackend()
        with pytest.raises(PythonPptxUnavailableError, match="python-pptx"):
            backend.read_presentation(path)

    def test_extract_title_returns_none_when_core_property_is_none(
        self,
    ) -> None:
        """python-pptx sets core_properties.title to None — not the string."""

        class _CoreProps:
            title = None

        class _Prs:
            core_properties = _CoreProps()

        assert PythonPptxBackend._extract_title(_Prs()) is None

    def test_extract_title_strips_whitespace_and_treats_blank_as_none(
        self,
    ) -> None:
        """A title made entirely of whitespace is treated as absent."""

        class _CoreProps:
            title = "   "

        class _Prs:
            core_properties = _CoreProps()

        assert PythonPptxBackend._extract_title(_Prs()) is None

    def test_extract_title_returns_real_title_unchanged(self) -> None:
        """A populated title is returned verbatim (after .strip())."""

        class _CoreProps:
            title = "  My Talk  "

        class _Prs:
            core_properties = _CoreProps()

        assert PythonPptxBackend._extract_title(_Prs()) == "My Talk"


class TestPresentationParseHandlesMissingTitle:
    """Untitled presentations must not surface the literal string ``None``."""

    def test_untitled_pptx_falls_back_to_file_stem(self, tmp_path: Path) -> None:
        """When ``data.title`` is ``None`` the fragment uses the file stem."""
        path = tmp_path / "anonymous.pptx"
        _write_pptx_placeholder(path)
        backend = StubPresentationBackend(
            presentations={
                "anonymous.pptx": PresentationData(
                    title=None,
                    slides=(SlideData(index=1, title="Hi", body="There"),),
                ),
            },
        )
        ingestor = PresentationIngestor(backend=backend)
        fragments = ingestor.parse(ingestor.discover(tmp_path)[0])
        assert fragments[0].metadata["title"] == "anonymous"
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "# anonymous" in markdown
        # The literal string "None" should never surface as the document title.
        assert "# None" not in markdown


# ---- Module re-exports -----------------------------------------------


class TestModuleExports:
    """Both ingestors are part of the ``creek.ingest`` public surface."""

    def test_spreadsheet_importable_from_package(self) -> None:
        """``from creek.ingest import SpreadsheetIngestor`` works."""
        from creek.ingest import SpreadsheetIngestor as Reexported

        assert Reexported is SpreadsheetIngestor

    def test_presentation_importable_from_package(self) -> None:
        """``from creek.ingest import PresentationIngestor`` works."""
        from creek.ingest import PresentationIngestor as Reexported

        assert Reexported is PresentationIngestor

    def test_protocols_satisfy_structural_typing(self) -> None:
        """Stub backends are structural matches for their Protocols."""
        assert isinstance(StubSpreadsheetBackend(), SpreadsheetBackend)
        assert isinstance(StubPresentationBackend(), PresentationBackend)

    def test_registry_keys_route_correctly(self) -> None:
        """The INGESTOR_REGISTRY keys ``spreadsheet`` and ``presentation`` resolve."""
        from creek.ingest import INGESTOR_REGISTRY

        assert INGESTOR_REGISTRY["spreadsheet"] is SpreadsheetIngestor
        assert INGESTOR_REGISTRY["presentation"] is PresentationIngestor


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
