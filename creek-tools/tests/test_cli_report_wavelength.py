"""CLI tests for ``creek report --type wavelength`` (#719).

Wires the existing descriptive :class:`WavelengthTracker` generators to the
report surface: ``--period weekly|monthly`` (anchored on today) plus explicit
``YYYY-Www`` / ``YYYY-MM`` periods, and an informative path when the
``wavelength`` classification dimension is unpopulated (no silent empty file).
Wavelength output is descriptive only — never prescriptive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app
from creek.models import (
    Fragment,
    FragmentSource,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)
from creek.time import now_la
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

runner = CliRunner()


def _classified_vault(
    vault: Path,
    *,
    classified: bool = True,
    authored_at: datetime | None = None,
) -> None:
    """Write three fragments, with or without a classified wavelength phase.

    *authored_at* anchors every fragment's authored date so a test can place
    them inside a specific target window (e.g. an explicit ISO week); it
    defaults to now.
    """
    when = authored_at if authored_at is not None else now_la()
    phases = [Phase.RISING, Phase.PEAKING, Phase.WITHDRAWAL]
    for i in range(3):
        wavelength = (
            WavelengthClassification(phase=phases[i])
            if classified
            else WavelengthClassification()
        )
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=f"frag-wave00000{i:03d}",
                title=f"Wave note {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
                authored_at=when,
                wavelength=wavelength,
            ),
            body=f"Reflective entry {i}.",
        )


def _phase_maps(vault: Path) -> list[Path]:
    """Return the phase-map notes written under 05-Wavelength/Phase-Maps/."""
    pm = vault / "05-Wavelength" / "Phase-Maps"
    return sorted(pm.glob("*.md")) if pm.exists() else []


def test_wavelength_weekly_writes_phase_map(tmp_path: Path) -> None:
    """``--type wavelength --period weekly`` writes a descriptive phase-map."""
    vault = tmp_path / "vault"
    _classified_vault(vault)

    result = runner.invoke(
        app,
        ["report", "--type", "wavelength", "--period", "weekly", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    pages = _phase_maps(vault)
    assert len(pages) == 1
    assert pages[0].name.endswith("-wavelength.md")
    assert "wavelength" in pages[0].name
    assert "descriptive" in result.output.lower()
    assert "non-prescriptive" in result.output.lower()


def test_wavelength_monthly_writes_phase_map(tmp_path: Path) -> None:
    """``--period monthly`` writes the monthly phase-map note."""
    vault = tmp_path / "vault"
    _classified_vault(vault)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "monthly",
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(_phase_maps(vault)) == 1


def test_wavelength_explicit_iso_week_period(tmp_path: Path) -> None:
    """An explicit ``YYYY-Www`` period regenerates that ISO week from its data."""
    vault = tmp_path / "vault"
    # Anchor the fragments inside ISO week 2026-W26 (Mon 2026-06-22) so the
    # historical report actually aggregates them, not just names the file.
    in_week = now_la().replace(year=2026, month=6, day=23)
    _classified_vault(vault, authored_at=in_week)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "2026-W26",
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "non-prescriptive" in result.output.lower()
    page = vault / "05-Wavelength" / "Phase-Maps" / "2026-W26-wavelength.md"
    assert page.is_file()
    # The in-window fragments make the report non-trivial, not an empty shell.
    assert "Wave note" in page.read_text(encoding="utf-8")


def test_wavelength_explicit_month_period(tmp_path: Path) -> None:
    """An explicit ``YYYY-MM`` period regenerates that month from its data."""
    vault = tmp_path / "vault"
    in_month = now_la().replace(year=2026, month=6, day=15)
    _classified_vault(vault, authored_at=in_month)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "2026-06",
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "non-prescriptive" in result.output.lower()
    page = vault / "05-Wavelength" / "Phase-Maps" / "2026-06-wavelength.md"
    assert page.is_file()
    assert "Wave note" in page.read_text(encoding="utf-8")


def test_wavelength_unpopulated_dimension_is_informative(tmp_path: Path) -> None:
    """An unpopulated wavelength dimension prints a message and writes no file."""
    vault = tmp_path / "vault"
    _classified_vault(vault, classified=False)

    result = runner.invoke(
        app,
        ["report", "--type", "wavelength", "--period", "weekly", "--vault", str(vault)],
    )

    assert result.exit_code == 0, result.output
    assert _phase_maps(vault) == []
    # Informative, not a silent empty file — pin to the actual message.
    assert "no wavelength phase-map written" in result.output.lower()


def test_wavelength_invalid_period_exits_two(tmp_path: Path) -> None:
    """An unparseable period exits 2 with a clear message."""
    vault = tmp_path / "vault"
    _classified_vault(vault)

    result = runner.invoke(
        app,
        [
            "report",
            "--type",
            "wavelength",
            "--period",
            "not-a-period",
            "--vault",
            str(vault),
        ],
    )

    assert result.exit_code == 2, result.output
    assert "period" in result.output.lower()
