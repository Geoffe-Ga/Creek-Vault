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


# ---------------------------------------------------------------------------
# SEC-003: symlink refusal
# ---------------------------------------------------------------------------


def test_redact_apply_refuses_symlink_escaping_source(tmp_path: Path) -> None:
    """A symlink whose target lies outside the source root aborts --apply.

    Demonstrates the SEC-003 path-traversal guard: even if the symlink
    points at a victim file containing nothing sensitive, ``creek redact
    --apply`` must refuse to follow it. The victim file's contents are
    asserted untouched after the run.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "leak.env").write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    target = tmp_path / "outside.md"
    target.write_text("preserve-me", encoding="utf-8")
    queue_dir = source / ".creek-redactions"
    queue_dir.mkdir()
    (queue_dir / "queue.json").symlink_to(target)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )

    assert result.exit_code != 0
    assert "symlink" in result.output.lower()
    # The would-be victim file is untouched.
    assert target.read_text(encoding="utf-8") == "preserve-me"


def test_redact_apply_refuses_deeply_nested_symlink(tmp_path: Path) -> None:
    """A symlink nested several directories deep still triggers refusal."""
    source = tmp_path / "src"
    nested = source / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (source / "leak.env").write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    target = tmp_path / "outside.txt"
    target.write_text("untouched", encoding="utf-8")
    (nested / "linked.md").symlink_to(target)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )

    assert result.exit_code != 0
    assert "symlink" in result.output.lower()
    assert target.read_text(encoding="utf-8") == "untouched"


def test_redact_apply_allows_internal_symlink(tmp_path: Path) -> None:
    """A symlink whose resolved target stays under the source proceeds."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.md").write_text(
        "Contact: alice@example.com\nSSN: 123-45-6789\n",
        encoding="utf-8",
    )
    # Internal symlink: resolves to a file inside the same source root.
    (source / "alias.md").symlink_to(source / "real.md")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )

    assert result.exit_code == 0


def test_redact_apply_no_symlinks_proceeds(tmp_path: Path) -> None:
    """A symlink-free source tree is unaffected by the new guard."""
    source = _write_sensitive_source(tmp_path)
    leak = _leak_path(source)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )

    assert result.exit_code == 0
    assert "hunter2" not in leak.read_text(encoding="utf-8")


def test_redact_apply_handles_circular_symlink(tmp_path: Path) -> None:
    """A loop (`a → b → a`) inside the source must not crash the guard.

    ``os.walk(followlinks=False)`` will not descend into the loop, but
    ``Path.resolve(strict=False)`` on a circular symlink could return
    an unexpected target. The guard must terminate cleanly: either by
    refusing (resolved target escapes) or by allowing (resolved target
    stays under root). Either is acceptable; an uncaught exception is
    not.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "leak.env").write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    a = source / "a.md"
    b = source / "b.md"
    a.symlink_to(b)
    b.symlink_to(a)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(source), "--yes"],
    )

    # Exit code may be 0 (loop allowed if it resolves under root) or
    # non-zero (loop rejected). The guarantee is that we don't crash.
    assert result.exit_code in (0, 1)
    if result.exit_code != 0:
        assert "symlink" in result.output.lower()


def test_redact_review_refuses_symlink_escaping_vault(tmp_path: Path) -> None:
    """--review also refuses symlinks that point outside the vault root."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "good.md").write_text("Contact: alice@example.com\n", encoding="utf-8")
    target = tmp_path / "secrets.md"
    target.write_text("API_KEY=sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8")
    (vault / "linked.md").symlink_to(target)

    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(vault)],
    )

    assert result.exit_code != 0
    assert "symlink" in result.output.lower()


# ---------------------------------------------------------------------------
# #1087: the read path SKIPS an escaping symlink, it does not refuse
#
# SEC-003 above governs the *write* path: ``--apply`` and ``--review`` refuse
# outright, because following a link there could rewrite a file outside the
# tree the operator named. ``--scan`` writes nothing, so refusing a whole scan
# over one bad link would be a denial of service on the safety pass itself.
# The contract is therefore different in kind: decline the file, keep the
# scan, and say so in the statistics.
# ---------------------------------------------------------------------------

_SYMLINK_SKIP_LABEL = "Files skipped (escaping symlink)"
"""The statistics-table row reporting a declined symlink.

Written out in full rather than probed by substring: the row sits beside
"Files skipped (binary)" and "Files skipped (extension)", and a partial match
would not distinguish the new counter from either of them.
"""

_ESCAPING_TARGET_PII = "SSN: 999-88-7777\n"
"""Payload for the file parked outside the source root: exactly one ``ssn``."""

_IN_ROOT_PII = "Contact: alice@example.com\n"
"""Payload for the in-root control file: exactly one ``email``.

A different pattern *type* from :data:`_ESCAPING_TARGET_PII`, so the report
itself says which of the two files was read — the CLI prints match types, and
never the matched text.
"""


def test_redact_scan_does_not_read_a_symlink_escaping_the_source_root(
    tmp_path: Path,
) -> None:
    """``--scan`` declines the escaping link, finishes the scan, and reports it.

    Three properties in one run, because they only mean anything together:

    * ``exit_code == 0`` — the scan is skipped, not refused. A non-zero exit
      here would make one stray link disable the operator's whole safety pass.
    * the escaping target's pattern type never reaches the output, while the
      in-root file's does. The second half is the non-vacuity guard: a scan
      that read nothing would satisfy the first half perfectly.
    * the skip is named in the statistics table. A safety tool that silently
      declines to read a file is its own hazard — the operator reads "no
      findings" and concludes the tree is clean.

    Deliberately run without ``--verbose``: the pattern-type table is enough
    to say which file was read, and the per-match table renders absolute
    temporary paths whose length would make this test a formatting assertion.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "notes.md").write_text(_IN_ROOT_PII, encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text(_ESCAPING_TARGET_PII, encoding="utf-8")
    (source / "linked.md").symlink_to(outside)

    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source)],
    )

    assert result.exit_code == 0, (
        "--scan refused the whole tree over one escaping symlink; the read "
        "path is supposed to skip the file and carry on.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "ssn" not in result.output.lower(), (
        "the scan reported the PII type of a file outside the source root, "
        f"so it followed the symlink and read it.\n\n{result.output}"
    )
    assert "email" in result.output.lower(), (
        "the in-root control produced no finding, so every other assertion "
        f"here would pass over a scan that read nothing.\n\n{result.output}"
    )
    assert _SYMLINK_SKIP_LABEL in result.output, (
        "the summary table does not report that a file was skipped, so the "
        f"operator reads a clean scan over a tree that was not.\n\n"
        f"{result.output}"
    )


def test_redact_scan_omits_the_symlink_skip_row_when_nothing_was_skipped(
    tmp_path: Path,
) -> None:
    """An ordinary scan's table gains no new row.

    The over-reporting guard for the row above. Rendering it unconditionally
    would put a permanent "escaping symlink: 0" line in front of every
    operator whose tree has never held one, which is the fastest way to teach
    a reader to skip that part of the table.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    source = _write_sensitive_source(tmp_path)

    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(source)],
    )

    assert result.exit_code == 0, (
        f"the ordinary scan failed.\n\nexit_code={result.exit_code}\n{result.output}"
    )
    assert "escaping symlink" not in result.output.lower(), (
        "the escaping-symlink row is rendered for a scan that declined "
        f"nothing.\n\n{result.output}"
    )
    assert "Files scanned" in result.output, (
        f"the statistics table did not render at all.\n\n{result.output}"
    )
