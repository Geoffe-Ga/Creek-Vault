"""CLI tests for the ``creek fill`` umbrella command (#720).

``creek fill`` is pure orchestration over already-merged steps, so these tests
mock each underlying step and assert: the steps run in dependency order, a step
failure is non-fatal, ``--dry-run`` runs nothing, ``--with-compost`` appends the
compost step, and the summary reports real per-folder counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

import creek.cli as cli_mod
from creek.cli import app
from creek.generate import compost as compost_mod
from creek.generate import indexes as indexes_mod
from creek.link import link_engine

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

_EXPECTED_ORDER = [
    "link/embeddings",
    "link/temporal",
    "link/eddies",
    "link/threads",
    "report/decisions",
    "report/unnamed",
    "report/paradox",
    "report/synchronicity",
    "report/mode-profiles",
    "report/wavelength",
    "index",
]


def _install_recorders(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    fail: str | None = None,
) -> None:
    """Replace every underlying step with a recorder appending its label."""

    def rec(label: str) -> object:
        def _step(*_a: object, **_k: object) -> None:
            calls.append(label)
            if label == fail:
                msg = f"boom in {label}"
                raise RuntimeError(msg)

        return _step

    monkeypatch.setattr(
        link_engine,
        "run_link",
        lambda **k: rec(f"link/{k['method']}")(),
    )
    for name, label in (
        ("_report_decisions", "report/decisions"),
        ("_report_unnamed", "report/unnamed"),
        ("_report_paradox", "report/paradox"),
        ("_report_synchronicity", "report/synchronicity"),
        ("_report_mode_profiles", "report/mode-profiles"),
    ):
        monkeypatch.setattr(cli_mod, name, rec(label))
    monkeypatch.setattr(
        cli_mod,
        "_report_wavelength",
        lambda _vault, _period: rec("report/wavelength")(),
    )

    class _FakeIndex:
        def __init__(self, *, vault_path: Path) -> None:
            self._vault_path = vault_path

        def generate_all(self) -> list[Path]:
            rec("index")()
            return []

    monkeypatch.setattr(indexes_mod, "IndexGenerator", _FakeIndex)

    class _FakeCompost:
        def generate_compost_report(self, vault_path: Path) -> Path:
            rec("compost/report")()
            return vault_path

    monkeypatch.setattr(compost_mod, "CompostTracker", _FakeCompost)


def test_fill_runs_all_steps_in_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``creek fill`` runs every step once, in the documented order."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert calls == _EXPECTED_ORDER


def test_fill_step_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing step is logged and the remaining steps still run."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls, fail="report/decisions")

    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    # Every step was still attempted despite the failure mid-sequence.
    assert calls == _EXPECTED_ORDER
    assert "report/decisions failed" in result.output


def test_fill_dry_run_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` prints the plan and invokes no step."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "dry-run" in result.output.lower()
    for label in _EXPECTED_ORDER:
        assert label in result.output


def test_fill_with_compost_appends_compost_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--with-compost`` adds the compost overview step after the core sequence."""
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault), "--with-compost"])

    assert result.exit_code == 0, result.output
    assert calls == [*_EXPECTED_ORDER, "compost/report"]


def test_fill_summary_reports_folder_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final summary counts the markdown actually present per folder."""
    vault = tmp_path / "vault"
    (vault / "02-Threads" / "Active").mkdir(parents=True)
    (vault / "02-Threads" / "Active" / "a.md").write_text("x", encoding="utf-8")
    (vault / "02-Threads" / "Active" / "b.md").write_text("x", encoding="utf-8")
    # A hidden index file must not be counted.
    (vault / "02-Threads" / "Active" / ".id-index.jsonl").write_text(
        "{}", encoding="utf-8"
    )
    (vault / "08-Decisions").mkdir(parents=True)
    (vault / "08-Decisions" / "d.md").write_text("x", encoding="utf-8")

    calls: list[str] = []
    _install_recorders(monkeypatch, calls)

    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "02-Threads 2" in result.output
    assert "08-Decisions 1" in result.output
