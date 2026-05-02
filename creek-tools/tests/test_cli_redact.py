"""Tests for the ``creek redact`` CLI command.

Exercises all three redaction modes (``--scan``, ``--apply``, ``--review``)
together with the supporting flags (``--report``, ``--dry-run``,
``--verbose``, ``--yes``) and the consent prompt behaviour.
"""

from __future__ import annotations

import os as real_os
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_audit_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test inside its own tmp_path so audit logs do not leak.

    ``creek redact --apply`` writes to ``<vault>/00-Creek-Meta/audit/``
    where ``<vault>`` defaults to ``Path(".")`` when no ``--vault`` is
    supplied. Without this fixture the per-test audit JSONL would land
    in the project's working directory and pollute the source tree.
    """
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_sensitive_source(tmp_path: Path) -> Path:
    """Create a source directory containing files with sensitive data.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the populated source directory.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "leak.env").write_text(
        "password=hunter2\nAPI_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    (source / "notes.md").write_text(
        "Contact: alice@example.com\nSSN: 123-45-6789\n",
        encoding="utf-8",
    )
    (source / "safe.md").write_text(
        "nothing interesting here\n",
        encoding="utf-8",
    )
    return source


def _write_empty_source(tmp_path: Path) -> Path:
    """Create a source directory containing a single non-sensitive file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the populated source directory.
    """
    source = tmp_path / "clean_source"
    source.mkdir()
    (source / "ok.md").write_text("hello world\n", encoding="utf-8")
    return source


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------


def test_redact_requires_exactly_one_mode() -> None:
    """Invoking redact without a mode flag should error out."""
    result = runner.invoke(app, ["redact"])
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower()


def test_redact_rejects_multiple_modes(tmp_path: Path) -> None:
    """Two mode flags at once should error out."""
    source = _write_empty_source(tmp_path)
    result = runner.invoke(
        app,
        [
            "redact",
            "--scan",
            "--apply",
            "--source",
            str(source),
        ],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower()


def test_redact_scan_requires_source() -> None:
    """--scan without --source should error out."""
    result = runner.invoke(app, ["redact", "--scan"])
    assert result.exit_code != 0
    assert "--source" in result.output


def test_redact_apply_requires_source() -> None:
    """--apply without --source should error out."""
    result = runner.invoke(app, ["redact", "--apply"])
    assert result.exit_code != 0
    assert "--source" in result.output


def test_redact_review_requires_vault() -> None:
    """--review without --vault should error out."""
    result = runner.invoke(app, ["redact", "--review"])
    assert result.exit_code != 0
    assert "--vault" in result.output


def test_redact_scan_missing_source(tmp_path: Path) -> None:
    """--scan on a non-existent path should exit with an error."""
    missing = tmp_path / "does-not-exist"
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(missing)],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# --scan
# ---------------------------------------------------------------------------


def test_redact_scan_finds_sensitive_data(tmp_path: Path) -> None:
    """--scan prints a summary showing the sensitive findings."""
    source = _write_sensitive_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source)],
    )
    assert result.exit_code == 0
    assert "Redaction Scan Summary" in result.output
    assert "Total findings" in result.output


def test_redact_scan_empty_directory(tmp_path: Path) -> None:
    """--scan on a directory with no sensitive data reports zero findings."""
    source = _write_empty_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source)],
    )
    assert result.exit_code == 0
    assert "Total findings" in result.output


def test_redact_scan_single_file(tmp_path: Path) -> None:
    """--scan accepts a single file path, not only directories."""
    target = tmp_path / "one.md"
    target.write_text("password=leaked123\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(target)],
    )
    assert result.exit_code == 0
    assert "Total findings" in result.output


def test_redact_scan_report_prints_markdown(tmp_path: Path) -> None:
    """--scan --report prints the detailed markdown summary."""
    source = _write_sensitive_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source), "--report"],
    )
    assert result.exit_code == 0
    # Markdown summary renders the "Findings by File" heading.
    assert "Findings by File" in result.output


def test_redact_scan_verbose_lists_matches(tmp_path: Path) -> None:
    """--verbose emits a match-level table in scan mode."""
    source = _write_sensitive_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source), "--verbose"],
    )
    assert result.exit_code == 0
    # The per-match table title from the CLI helper.
    assert "Matches" in result.output


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


def _leak_path(source: Path) -> Path:
    """Return the path of the .env leak file used by the sensitive fixture.

    Args:
        source: Source directory created by :func:`_write_sensitive_source`.

    Returns:
        Path to the leak file inside *source*.
    """
    return source / "leak.env"


def test_redact_apply_confirmed_modifies_files(tmp_path: Path) -> None:
    """Confirming the prompt redacts sensitive data in-place."""
    source = _write_sensitive_source(tmp_path)
    leak = _leak_path(source)
    original = leak.read_text(encoding="utf-8")
    assert "hunter2" in original

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source)],
        input="y\n",
    )
    assert result.exit_code == 0
    modified = leak.read_text(encoding="utf-8")
    assert "hunter2" not in modified
    assert "[REDACTED:" in modified


def test_redact_apply_declined_leaves_files_untouched(tmp_path: Path) -> None:
    """Declining the prompt should leave source files unchanged."""
    source = _write_sensitive_source(tmp_path)
    leak = _leak_path(source)
    original = leak.read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source)],
        input="n\n",
    )
    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert leak.read_text(encoding="utf-8") == original


def test_redact_apply_dry_run_preserves_files(tmp_path: Path) -> None:
    """--dry-run must never modify the source tree."""
    source = _write_sensitive_source(tmp_path)
    leak = _leak_path(source)
    original = leak.read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert leak.read_text(encoding="utf-8") == original


def test_redact_apply_yes_skips_confirmation(tmp_path: Path) -> None:
    """--yes bypasses the confirmation prompt and applies redactions."""
    source = _write_sensitive_source(tmp_path)
    leak = _leak_path(source)
    assert "hunter2" in leak.read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )
    assert result.exit_code == 0
    modified = leak.read_text(encoding="utf-8")
    assert "hunter2" not in modified


def test_redact_apply_no_findings_short_circuits(tmp_path: Path) -> None:
    """--apply on a clean tree reports nothing to do and never prompts."""
    source = _write_empty_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source)],
    )
    assert result.exit_code == 0
    assert "nothing to redact" in result.output.lower()


def test_redact_apply_partial_io_failure_preserves_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O error mid-batch must never leave a file half-written.

    Simulates ``os.replace`` failing on the second file. The first file
    is expected to be redacted, the second file must retain its
    original, un-redacted content (i.e. not be a half-written temp
    file or empty), and no stray temp files may be left behind.
    """
    source = _write_sensitive_source(tmp_path)
    leak = source / "leak.env"
    notes = source / "notes.md"
    original_notes = notes.read_text(encoding="utf-8")

    real_replace = real_os.replace
    call_count = {"n": 0}

    def flaky_replace(src: object, dst: object) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            msg = "simulated disk full"
            raise OSError(msg)
        real_replace(src, dst)

    monkeypatch.setattr(
        "creek.redact.cli_commands.os.replace",
        flaky_replace,
    )

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )

    assert result.exit_code != 0
    assert "I/O error" in result.output
    # First file was redacted atomically and committed.
    assert "hunter2" not in leak.read_text(encoding="utf-8")
    # Second file is exactly as it started — not empty, not partial.
    assert notes.read_text(encoding="utf-8") == original_notes
    # No stray temp files left in the source directory.
    assert not list(source.glob("*.redact-tmp"))
    assert not list(source.glob(".*.redact-tmp"))


def test_redact_apply_verbose_lists_matches(tmp_path: Path) -> None:
    """--verbose in apply mode surfaces the per-match table."""
    source = _write_sensitive_source(tmp_path)
    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--dry-run",
            "--verbose",
        ],
    )
    assert result.exit_code == 0
    assert "Matches" in result.output


# ---------------------------------------------------------------------------
# --review
# ---------------------------------------------------------------------------


def test_redact_review_renders_queue(tmp_path: Path) -> None:
    """--review on a vault prints the markdown review queue."""
    vault = _write_sensitive_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(vault)],
    )
    assert result.exit_code == 0
    assert "Redaction Review Queue" in result.output


def test_redact_review_empty_vault(tmp_path: Path) -> None:
    """--review on an empty vault prints a no-findings banner."""
    vault = _write_empty_source(tmp_path)
    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(vault)],
    )
    assert result.exit_code == 0
    assert "No findings" in result.output


def test_redact_review_missing_vault(tmp_path: Path) -> None:
    """--review with a missing vault path errors out."""
    missing = tmp_path / "no-vault"
    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(missing)],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
