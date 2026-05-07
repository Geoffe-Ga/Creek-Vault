"""Tests for the pre-LLM yield summary writer (FEAT-005).

The summary writer is the audit substrate that records, per pipeline run,
how much work the deterministic and local-model passes (Pass 1 + Pass 2)
accomplished without ever invoking the network LLM (Pass 3). Each entry
is one JSONL line under ``00-Creek-Meta/Processing-Log/run-summary.jsonl``
so downstream tooling (FEAT-006 audit report) can stream the file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from creek.audit.yield_summary import (
    PreLLMYieldSummary,
    format_yield_line,
    write_yield_summary,
)

if TYPE_CHECKING:
    from pathlib import Path


def _summary(**overrides: object) -> PreLLMYieldSummary:
    """Return a default ``PreLLMYieldSummary`` with overrides applied."""
    base: dict[str, object] = {
        "run_id": "run-1",
        "deterministic_classified": 7,
        "local_model_processed": 5,
        "residue": 2,
        "no_llm": True,
    }
    base.update(overrides)
    return PreLLMYieldSummary(**base)  # type: ignore[arg-type]


def test_format_yield_line_matches_documented_template() -> None:
    """The stdout line follows the format pinned in FEAT-005."""
    line = format_yield_line(
        _summary(deterministic_classified=7, local_model_processed=5, residue=2),
    )
    assert line == (
        "Deterministic: 7 classified | "
        "Local-model: 5 embedded/OCR'd | "
        "Residue: 2 (would go to LLM if Pass-3 enabled)"
    )


def test_write_yield_summary_creates_jsonl(tmp_path: Path) -> None:
    """A single write produces one JSONL line at the documented path."""
    written = write_yield_summary(vault_path=tmp_path, summary=_summary())

    expected = tmp_path / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    assert written == expected
    assert expected.exists()

    lines = expected.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["run_id"] == "run-1"
    assert payload["deterministic_classified"] == 7
    assert payload["local_model_processed"] == 5
    assert payload["residue"] == 2
    assert payload["no_llm"] is True
    assert "timestamp" in payload


def test_write_yield_summary_appends_across_runs(tmp_path: Path) -> None:
    """Subsequent runs append rather than overwrite — JSONL is per-run."""
    write_yield_summary(vault_path=tmp_path, summary=_summary(run_id="run-1"))
    write_yield_summary(vault_path=tmp_path, summary=_summary(run_id="run-2"))

    log = tmp_path / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["run_id"] == "run-1"
    assert json.loads(lines[1])["run_id"] == "run-2"


def test_write_yield_summary_creates_log_dir(tmp_path: Path) -> None:
    """The Processing-Log directory is created lazily on first write."""
    log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
    assert not log_dir.exists()
    write_yield_summary(vault_path=tmp_path, summary=_summary())
    assert log_dir.is_dir()


def test_yield_summary_records_no_llm_flag(tmp_path: Path) -> None:
    """The persisted entry distinguishes --no-llm runs from normal ones."""
    write_yield_summary(
        vault_path=tmp_path,
        summary=_summary(no_llm=False),
    )
    log = tmp_path / "00-Creek-Meta" / "Processing-Log" / "run-summary.jsonl"
    payload = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert payload["no_llm"] is False
