"""Spreadsheet ingestor for the Creek pipeline.

Implements §57 of the Creek Ontology — ingest XLSX workbooks (one
fragment per sheet) and CSV files (one fragment per file). The
spreadsheet backend is decoupled through the
:class:`SpreadsheetBackend` Protocol so callers can plug in any
implementation; tests inject a deterministic stub. The default
:class:`OpenpyxlBackend` lazily imports ``openpyxl`` so the rest of
the package — and the unit tests — run cleanly even when that
optional dependency is not installed. CSV parsing uses Python's
stdlib ``csv`` module and works without any optional dependency.

Sheets larger than :data:`SUMMARY_THRESHOLD` rows render as a
truncated table with the first :data:`SUMMARY_HEAD_ROWS` and last
:data:`SUMMARY_TAIL_ROWS` rows plus the total row count, so giant
exports stay legible without re-paginating the entire vault entry.

Optional dependency (install separately to enable XLSX support):

* ``openpyxl`` — pure-Python XLSX reader.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import chardet

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    file_modified_time,
)
from creek.models import SourcePlatform

logger = logging.getLogger(__name__)


SPREADSHEET_EXTENSIONS: frozenset[str] = frozenset({".xlsx", ".csv"})
"""File extensions handled by :class:`SpreadsheetIngestor`."""


SUMMARY_THRESHOLD: int = 100
"""Row count above which a sheet renders as a head/tail summary."""

SUMMARY_HEAD_ROWS: int = 10
"""Number of leading rows kept in a summarised table."""

SUMMARY_TAIL_ROWS: int = 5
"""Number of trailing rows kept in a summarised table."""


# ---- Public dataclasses + protocol --------------------------------------


@dataclass(frozen=True)
class SheetData:
    """A single worksheet within a workbook.

    Attributes:
        name: Sheet name as it appears in the workbook.
        headers: Optional header row (auto-detected by the backend);
            ``None`` when the first row is data.
        rows: All data rows as tuples of strings (formulas resolved to
            calculated values, dates ISO-formatted).
    """

    name: str
    headers: tuple[str, ...] | None
    rows: tuple[tuple[str, ...], ...]

    @property
    def is_empty(self) -> bool:
        """``True`` when neither headers nor rows carry any signal."""
        return not self.rows and not self.headers


@dataclass(frozen=True)
class WorkbookData:
    """A workbook (XLSX or CSV) decomposed into sheets.

    A CSV file always presents as a one-sheet workbook whose sheet is
    named after the file stem.
    """

    sheets: tuple[SheetData, ...]


@runtime_checkable
class SpreadsheetBackend(Protocol):
    """Pluggable spreadsheet backend.

    Implementations must be deterministic and side-effect-free; the
    same input file should always produce the same
    :class:`WorkbookData`.
    """

    def is_available(self) -> bool:
        """Return ``True`` when the backend can perform reads right now."""

    def read_workbook(self, path: Path) -> WorkbookData:
        """Read *path* and return its decomposed :class:`WorkbookData`."""


class OpenpyxlUnavailableError(RuntimeError):
    """Raised when an XLSX read is attempted without ``openpyxl`` installed."""


# ---- Default backend ---------------------------------------------------


class OpenpyxlBackend:
    """Spreadsheet backend backed by ``openpyxl`` (XLSX) and stdlib ``csv``.

    Imports of the optional ``openpyxl`` dependency are deferred to
    call time so the rest of the package runs cleanly without it.
    CSV files are read via Python's stdlib :mod:`csv` module and
    therefore always work — ``is_available()`` reflects only the
    XLSX path.
    """

    def is_available(self) -> bool:
        """Return ``True`` when ``openpyxl`` imports cleanly."""
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return False
        return True

    def read_workbook(self, path: Path) -> WorkbookData:
        """Read *path* and return a :class:`WorkbookData`.

        Args:
            path: Filesystem path to a ``.xlsx`` or ``.csv`` file.

        Returns:
            A :class:`WorkbookData` with one or more
            :class:`SheetData` entries.

        Raises:
            OpenpyxlUnavailableError: When *path* is an XLSX file and
                ``openpyxl`` is not installed.
        """
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return _read_csv(path)
        return self._read_xlsx(path)

    @staticmethod
    def _read_xlsx(path: Path) -> WorkbookData:
        """Read an XLSX file via ``openpyxl``."""
        try:
            import openpyxl
        except ImportError as exc:
            msg = (
                "openpyxl is required for XLSX ingestion. Install it with "
                "`pip install openpyxl`."
            )
            raise OpenpyxlUnavailableError(msg) from exc

        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
        sheets: list[SheetData] = []
        for name in workbook.sheetnames:
            worksheet = workbook[name]
            raw_rows = [
                tuple(_cell_to_str(cell) for cell in row)
                for row in worksheet.iter_rows(values_only=True)
            ]
            sheets.append(_split_header(name, raw_rows))
        workbook.close()
        return WorkbookData(sheets=tuple(sheets))


def _cell_to_str(value: Any) -> str:
    """Coerce a workbook cell value to its display string form."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


CSV_CHARDET_CONFIDENCE_THRESHOLD: float = 0.7
"""Minimum ``chardet`` confidence required to trust an auto-detected encoding.

Below this we fall through to ``cp1252`` and emit a warning so the
user knows the result may be mojibake.
"""


def _read_csv(path: Path) -> WorkbookData:
    """Read a CSV file as a single-sheet workbook.

    Probe order:

    1. ``utf-8-sig`` — handles BOM + plain UTF-8, the dominant case.
    2. ``chardet`` — detects non-Western encodings (Shift-JIS, GBK,
       ISO-8859-5, …) when its confidence exceeds
       :data:`CSV_CHARDET_CONFIDENCE_THRESHOLD`.
    3. ``cp1252`` — last-resort fallback that accepts any byte
       sequence. Emits a ``WARNING`` log including the file path so
       the user has a chance to spot mojibake before it lands in the
       vault (BUG-010).
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    else:
        return _csv_text_to_workbook(path, text)

    detection = chardet.detect(raw)
    detected = detection.get("encoding")
    confidence = detection.get("confidence") or 0.0
    if (
        detected
        and confidence >= CSV_CHARDET_CONFIDENCE_THRESHOLD
        and detected.lower() not in {"ascii", "utf-8-sig", "cp1252"}
    ):
        try:
            text = raw.decode(detected)
        except (UnicodeDecodeError, LookupError):
            pass
        else:
            return _csv_text_to_workbook(path, text)

    text = raw.decode("cp1252")
    logger.warning(
        "CSV %s decoded as cp1252 (chardet best guess: %s @ %.2f); "
        "non-Western content may render as mojibake.",
        path,
        detected or "unknown",
        confidence,
    )
    return _csv_text_to_workbook(path, text)


def _csv_text_to_workbook(path: Path, text: str) -> WorkbookData:
    """Parse decoded CSV ``text`` into a single-sheet :class:`WorkbookData`."""
    rows = [tuple(row) for row in csv.reader(text.splitlines())]
    return WorkbookData(sheets=(_split_header(path.stem, rows),))


def _split_header(
    name: str,
    rows: list[tuple[str, ...]],
) -> SheetData:
    """Split *rows* into ``(headers, data_rows)``.

    Auto-detects a header row: the first row qualifies when every
    cell is a non-empty string. Otherwise the first row is treated
    as data and ``headers`` is ``None``.
    """
    if not rows:
        return SheetData(name=name, headers=None, rows=())
    first = rows[0]
    if first and all(cell.strip() for cell in first):
        return SheetData(
            name=name,
            headers=first,
            rows=tuple(rows[1:]),
        )
    return SheetData(name=name, headers=None, rows=tuple(rows))


# ---- SpreadsheetIngestor -----------------------------------------------


class SpreadsheetIngestor(Ingestor):
    """Ingest XLSX and CSV files as one fragment per sheet."""

    def __init__(self, backend: SpreadsheetBackend | None = None) -> None:
        """Initialise with a backend; defaults to :class:`OpenpyxlBackend`."""
        self.backend = backend if backend is not None else OpenpyxlBackend()

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Recursively find ``.xlsx`` and ``.csv`` files under *source_path*.

        Single-file paths whose suffix is unsupported return an empty
        list so the ingest pipeline can route them elsewhere. File
        bytes are not slurped at discovery time — each backend reads
        from disk via the :class:`Path`.
        """
        if source_path.is_file():
            paths = (
                [source_path]
                if source_path.suffix.lower() in SPREADSHEET_EXTENSIONS
                else []
            )
        else:
            paths = [
                p
                for p in sorted(source_path.rglob("*"))
                if p.is_file() and p.suffix.lower() in SPREADSHEET_EXTENSIONS
            ]
        return [
            RawDocument(
                path=path,
                content=b"",
                metadata={"original_file": str(path)},
                detected_encoding="binary"
                if path.suffix.lower() == ".xlsx"
                else "utf-8",
            )
            for path in paths
        ]

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Read the workbook and emit one fragment per non-empty sheet."""
        workbook = self.backend.read_workbook(raw.path)
        timestamp = file_modified_time(raw.path)
        fragments: list[ParsedFragment] = []
        for sheet in workbook.sheets:
            if sheet.is_empty:
                continue
            column_count = (
                len(sheet.headers)
                if sheet.headers
                else (len(sheet.rows[0]) if sheet.rows else 0)
            )
            fragments.append(
                ParsedFragment(
                    content="",  # Markdown rendered in convert_to_markdown.
                    metadata={
                        "original_file": str(raw.path),
                        "sheet": sheet.name,
                        "rows": len(sheet.rows),
                        "columns": column_count,
                        "headers": list(sheet.headers) if sheet.headers else [],
                        "row_data": [list(row) for row in sheet.rows],
                    },
                    source_path=str(raw.path),
                    timestamp=timestamp,
                ),
            )
        return fragments

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Render the sheet as a GFM table with optional summary truncation."""
        headers, rows = _extract_headers_and_rows(fragment)
        sheet_name = str(fragment.metadata.get("sheet", "Sheet"))
        original = Path(
            str(fragment.metadata.get("original_file", fragment.source_path))
        )
        lines = [f"# {original.name} — {sheet_name}", ""]
        if not headers and not rows:
            lines.append("_(empty sheet)_")
            return "\n".join(lines) + "\n"
        lines.extend(_render_table(headers, rows))
        return "\n".join(lines) + "\n"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Produce YAML frontmatter for a spreadsheet fragment."""
        return {
            "type": "fragment",
            "source": {
                "platform": SourcePlatform.SPREADSHEET.value,
                "original_file": fragment.metadata.get(
                    "original_file",
                    fragment.source_path,
                ),
            },
            "sheet": fragment.metadata.get("sheet", ""),
            "rows": fragment.metadata.get("rows", 0),
            "columns": fragment.metadata.get("columns", 0),
            "ingested": fragment.timestamp.isoformat(),
        }


def _extract_headers_and_rows(
    fragment: ParsedFragment,
) -> tuple[list[str], list[list[str]]]:
    """Return ``(headers, rows)`` from a parsed fragment's metadata."""
    headers: list[str] = list(fragment.metadata.get("headers", []))
    rows: list[list[str]] = [list(row) for row in fragment.metadata.get("row_data", [])]
    if not headers and rows:
        headers = [f"col{i + 1}" for i in range(len(rows[0]))]
    return headers, rows


def _escape_cell(value: object) -> str:
    """Escape a cell value for safe inclusion in a GFM table.

    GFM uses ``|`` as the column separator and a literal newline as
    the row terminator, so cells carrying either character would
    corrupt the table layout. We escape ``|`` to ``\\|`` (the GFM
    canonical form) and replace any newline with ``<br>`` so the
    cell renders on a single visual line.
    """
    return (
        str(value)
        .replace("|", r"\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def _render_row(cells: list[str] | tuple[str, ...]) -> str:
    """Render one GFM table row from a sequence of pre-escaped cells."""
    return "| " + " | ".join(cells) + " |"


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Return markdown lines for a GFM table, summarising oversize sheets.

    Cells are escaped via :func:`_escape_cell` so values containing
    ``|`` or newlines do not corrupt the table. Sheets larger than
    :data:`SUMMARY_THRESHOLD` are emitted as a single table whose
    body is the head + tail rows; the summary note appears *before*
    the table so the reader sees the truncation context first.
    """
    safe_headers = [_escape_cell(header) for header in headers]
    separator = _render_row(["---"] * len(safe_headers))
    lines: list[str] = []
    if len(rows) > SUMMARY_THRESHOLD:
        lines.extend(
            (
                f"_Showing first {SUMMARY_HEAD_ROWS} and last "
                f"{SUMMARY_TAIL_ROWS} of {len(rows)} rows._",
                "",
            ),
        )
        displayed = rows[:SUMMARY_HEAD_ROWS] + rows[-SUMMARY_TAIL_ROWS:]
    else:
        displayed = rows
    lines.extend((_render_row(safe_headers), separator))
    lines.extend(_render_row([_escape_cell(cell) for cell in row]) for row in displayed)
    return lines


__all__ = [
    "SPREADSHEET_EXTENSIONS",
    "SUMMARY_HEAD_ROWS",
    "SUMMARY_TAIL_ROWS",
    "SUMMARY_THRESHOLD",
    "OpenpyxlBackend",
    "OpenpyxlUnavailableError",
    "SheetData",
    "SpreadsheetBackend",
    "SpreadsheetIngestor",
    "WorkbookData",
]
