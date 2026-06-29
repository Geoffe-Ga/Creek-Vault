"""CLI tests for ``creek index`` (standalone index regeneration, #717)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_TOP_LEVEL = (
    "00-Creek-Meta",
    "01-Fragments",
    "02-Threads",
    "03-Eddies",
    "04-Praxis",
    "05-Wavelength",
    "06-Frequencies",
    "07-Voice",
    "08-Decisions",
    "09-Reference",
    "10-Liminal",
)

_FREQ_SUBDIRS = (
    "F1-Agency",
    "F2-Receptivity",
    "F3-Self-Love-Power",
    "F4-Community-Love",
    "F5-Achievism",
    "F6-Pluralism",
    "F7-Integration",
    "F8-True-Self",
    "F9-Unity",
    "F10-Emptiness",
)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Create a scaffolded vault with the folders the index generator writes to."""
    for folder in _TOP_LEVEL:
        (tmp_path / folder).mkdir()
    for subdir in _FREQ_SUBDIRS:
        (tmp_path / "06-Frequencies" / subdir).mkdir()
    return tmp_path


def test_index_writes_all_index_notes(vault: Path) -> None:
    """``creek index`` writes the Dataview index notes and reports them."""
    result = runner.invoke(app, ["index", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    # The four named non-frequency indexes plus a representative frequency index.
    assert (vault / "02-Threads" / "Thread-Index.md").is_file()
    assert (vault / "03-Eddies" / "Eddy-Map.md").is_file()
    assert (vault / "00-Creek-Meta" / "Temporal-Index.md").is_file()
    assert (vault / "00-Creek-Meta" / "Source-Index.md").is_file()
    assert (vault / "06-Frequencies" / "F1-Agency" / "F1-Index.md").is_file()
    # Reports the count and at least one written path.
    assert "Wrote" in result.output
    assert "Thread-Index.md" in result.output


def test_index_is_idempotent(vault: Path) -> None:
    """Re-running ``creek index`` overwrites in place with identical bytes."""
    first = runner.invoke(app, ["index", "--vault", str(vault)])
    assert first.exit_code == 0, first.output

    written = sorted(vault.rglob("*-Index.md")) + sorted(vault.rglob("Eddy-Map.md"))
    snapshot = {p: p.read_bytes() for p in written}
    assert snapshot, "expected index notes to have been written"

    second = runner.invoke(app, ["index", "--vault", str(vault)])
    assert second.exit_code == 0, second.output

    for path, original in snapshot.items():
        assert path.read_bytes() == original, f"{path} changed on re-run"


def test_index_reports_count_matching_written_files(vault: Path) -> None:
    """The reported count equals the number of index notes actually on disk."""
    result = runner.invoke(app, ["index", "--vault", str(vault)])
    assert result.exit_code == 0, result.output

    on_disk = {
        *vault.rglob("*-Index.md"),
        *vault.rglob("Eddy-Map.md"),
    }
    assert f"Wrote {len(on_disk)} index note" in result.output
