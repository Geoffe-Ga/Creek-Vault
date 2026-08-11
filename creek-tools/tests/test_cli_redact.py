"""Tests for the ``creek redact`` CLI command.

Exercises all three redaction modes (``--scan``, ``--apply``, ``--review``)
together with the supporting flags (``--report``, ``--dry-run``,
``--verbose``, ``--yes``) and the consent prompt behaviour.
"""

from __future__ import annotations

import logging
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


# ---------------------------------------------------------------------------
# #1293: containment for a DIRECTLY NAMED path
#
# Both guards above are directory-only — ``if source.is_dir():`` in
# ``run_apply`` and ``if vault.is_dir():`` in ``run_review`` — and what they
# guard is a walk of the tree looking for links *inside* it. The path the
# operator names on the command line is never itself examined, so naming a
# symlink walks straight past SEC-003 and into the target:
#
#   * ``--apply --source <named symlink to a file>`` reads the out-of-tree
#     file, prints its finding types, and then lands the redacted copy on the
#     link itself — the operator's link is destroyed and the victim's content
#     has been disclosed.
#   * ``--apply --source <named symlink to a directory>`` rewrites files
#     outside the named tree in place. That is a genuine out-of-root write.
#   * ``--scan --source <named symlink>`` reads the target and reports it.
#
# The two contracts established above stay distinct, and the fix must keep
# them distinct — this is the whole reason the guard belongs at the
# ``_scan_source`` chokepoint with a *policy*, rather than being copied a
# fourth time into a handler:
#
#   * the WRITE path (``--apply``, ``--review``) REFUSES: exit 1, before
#     anything is read through the link.
#   * the READ path (``--scan``) SKIPS the file, counts it under
#     ``_SYMLINK_SKIP_LABEL``, and still exits 0 — see the #1087 banner above
#     for why refusing there would be a denial of service on the safety pass.
# ---------------------------------------------------------------------------


def test_redact_apply_refuses_a_named_symlink_whose_target_escapes_its_parent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--apply`` on a named symlink refuses instead of reading the target.

    The primary red for #1293. Four separately observable failures at HEAD,
    each pinned by its own assertion so that a partial fix cannot pass:

    * the run succeeds (exit 0) rather than refusing;
    * the out-of-tree target's finding types reach the console, which is
      disclosure of the content of a file the operator never named;
    * ``os.replace`` lands the redacted copy on the link, so the named path
      comes back as an ordinary in-tree regular file and the link is gone;
    * nothing in the output says "symlink", so the operator has no way to
      learn any of the above happened.

    Deliberately run without ``--verbose``: the per-match table renders
    absolute temporary paths that Rich truncates to the console width, which
    would make any path assertion here an accident of formatting. The
    "Findings by Type" table names the pattern types on its own.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log capture — the refusal must not disclose the
            resolved target through the logs either.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.env"
    victim.write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\nssn: 999-88-7777\n",
        encoding="utf-8",
    )
    original_bytes = victim.read_bytes()
    source = tmp_path / "src"
    source.mkdir()
    link = source / "leak.env"
    link.symlink_to(victim)

    with caplog.at_level(logging.WARNING):
        result = runner.invoke(
            app,
            ["redact", "--apply", "--source", str(link), "--yes"],
        )

    assert result.exit_code == 1, (
        "--apply followed a symlink named directly on the command line; the "
        "directory-only SEC-003 guard never looked at the named path "
        "itself.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "api_key" not in result.output.lower(), (
        "the findings-by-type table names a pattern that only exists in the "
        "out-of-tree target, so the target was opened and read.\n\n"
        f"{result.output}"
    )
    assert "ssn" not in result.output.lower(), (
        "the findings-by-type table names a pattern that only exists in the "
        "out-of-tree target, so the target was opened and read.\n\n"
        f"{result.output}"
    )
    assert link.is_symlink(), (
        "the named symlink is no longer a symlink: the atomic replace wrote "
        "the redacted copy over the link, silently converting the operator's "
        "link into a regular file.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "symlink" in result.output.lower(), (
        "the refusal does not say why it refused; an operator cannot act on "
        f"an unexplained exit 1.\n\nexit_code={result.exit_code}\n"
        f"{result.output}"
    )

    disclosing = [
        record.getMessage()
        for record in caplog.records
        if str(victim) in record.getMessage()
    ]
    assert not disclosing, (
        "a log record spells out the resolved out-of-tree target. The "
        "refusal should name the path the operator typed, not the one it "
        f"points at.\n\n{disclosing}"
    )
    assert victim.name not in result.output, (
        "the console output names the out-of-tree target file. Asserted on "
        "the bare filename rather than the absolute path on purpose: Rich "
        "truncates long paths to the console width, so an assertion on "
        f"str(victim) would hold no matter what was printed.\n\n"
        f"{result.output}"
    )

    # REGRESSION INVARIANT ONLY — this assertion ALREADY PASSES AT HEAD and
    # is not part of the red. In the named-*file* case HEAD reads the victim
    # and then writes the redacted copy over the *link*, so the victim's own
    # bytes survive; it is pinned here so a fix cannot start writing through
    # the link on its way to refusing. The load-bearing "bytes unchanged"
    # red is the named-*directory* case in the next test, where HEAD really
    # does rewrite the out-of-tree file in place.
    assert victim.read_bytes() == original_bytes, (
        "the out-of-tree target's bytes changed; a run that refuses must "
        f"write nothing anywhere.\n\nexit_code={result.exit_code}\n"
        f"{result.output}"
    )


def test_redact_apply_refuses_a_named_symlinked_directory_whose_target_escapes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A named symlinked *directory* is the case that writes out of root.

    The issue body describes the named-file case; this is the one that does
    real damage. ``Path.is_dir()`` follows symlinks, so a link to a directory
    satisfies ``if source.is_dir():`` and the SEC-003 walk runs — but it
    walks the *target's* tree, finds no links inside it, and passes. Every
    file discovered under the link is then a plain regular file, so the
    atomic replace rewrites it where it lives: outside the tree the operator
    named.

    The unchanged-bytes assertion here IS load-bearing red (HEAD rewrites
    the file), unlike its labelled counterpart in the named-file test above.

    Args:
        tmp_path: Pytest-provided temporary directory.
        caplog: Pytest log capture, for the same non-disclosure pin.
    """
    secrets_dir = tmp_path / "outside" / "secrets"
    secrets_dir.mkdir(parents=True)
    victim = secrets_dir / "a.md"
    victim.write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    original_bytes = victim.read_bytes()
    source = tmp_path / "src"
    source.mkdir()
    link = source / "linkdir"
    link.symlink_to(secrets_dir)

    with caplog.at_level(logging.WARNING):
        result = runner.invoke(
            app,
            ["redact", "--apply", "--source", str(link), "--yes"],
        )

    assert result.exit_code == 1, (
        "--apply accepted a symlinked directory named on the command line; "
        "is_dir() follows the link, so the SEC-003 walk ran over the "
        "target's tree instead of rejecting the link.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "symlink" in result.output.lower(), (
        "the refusal does not say why it refused.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert victim.read_bytes() == original_bytes, (
        "a file outside the named tree was rewritten in place. This is the "
        "out-of-root WRITE: the operator named one directory and another "
        f"directory's contents changed.\n\nexit_code={result.exit_code}\n"
        f"{result.output}"
    )

    disclosing = [
        record.getMessage()
        for record in caplog.records
        if str(secrets_dir) in record.getMessage()
    ]
    assert not disclosing, (
        "a log record spells out the resolved out-of-tree directory; report "
        f"the named path, not what it resolves to.\n\n{disclosing}"
    )


def test_redact_apply_refuses_a_named_symlinked_directory_before_walking_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must land *before* the tree is walked, not merely before a write.

    ``run_apply`` guards ``--source`` in the handler and again at the
    ``_scan_source`` chokepoint. The chokepoint alone already prevents the
    out-of-root *write*, so a test that only asserts ``exit_code == 1`` and
    unchanged victim bytes passes with the handler guard deleted — the two
    layers are indistinguishable by outcome. Mutation testing found exactly
    that survivor.

    What the handler guard uniquely buys is ordering, and it is the reason
    the call sits above ``if source.is_dir():`` with a comment saying so:
    ``is_dir()`` follows the link, so without the early refusal
    :func:`_assert_no_escaping_symlinks` runs ``os.walk`` over the *target's*
    tree. That enumerates directory names outside the tree the operator
    named — a structure disclosure of the victim directory, and the same
    class of oracle #1087 closes — before anything refuses. Measured on the
    unmutated tree the count is zero directories; with the handler guard
    removed it is the link plus every subdirectory beneath it.

    Spying on ``os.walk`` rather than ``os.scandir`` keeps this stable across
    3.11-3.13: the guard calls the module-level ``os.walk`` directly, whereas
    ``scandir`` is a pathlib implementation detail that has moved between
    versions and could make this test vacuously green.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture, used to install the
            ``os.walk`` spy.
    """
    secrets_dir = tmp_path / "outside" / "secrets"
    (secrets_dir / "nested").mkdir(parents=True)
    (secrets_dir / "a.md").write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    link = source / "linkdir"
    link.symlink_to(secrets_dir)

    walked: list[str] = []
    real_walk = real_os.walk

    # ``Any`` rather than ``object``: this spy is a transparent passthrough to
    # ``os.walk``, which typeshed declares as an overload set (str vs bytes
    # paths). Typing the parameters as ``object`` makes the delegation
    # untypeable under mypy strict and invites a type suppression, which the
    # house rules forbid without a tracking issue and which would paper over
    # the symptom rather than the cause. ``Any`` splats into the overload
    # cleanly and needs nothing suppressed. Imported here, not at module
    # scope, to leave the pre-existing import block untouched.
    from typing import Any

    def spy_walk(top: Any, *args: Any, **kwargs: Any) -> Any:
        """Record each walk root, then delegate to the real ``os.walk``."""
        walked.append(str(top))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(real_os, "walk", spy_walk)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(link), "--yes"],
    )

    assert result.exit_code == 1, (
        "--apply accepted a named escaping symlinked directory.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )

    # ``realpath`` on plain strings, not ``Path``: this module imports
    # ``pathlib.Path`` only under ``TYPE_CHECKING``, so naming it here would
    # raise ``NameError`` at runtime and turn a genuine regression into a
    # red-for-the-wrong-reason. Resolving both sides also collapses the
    # macOS ``/tmp`` -> ``/private/tmp`` alias.
    secrets_real = real_os.path.realpath(secrets_dir)
    escaping_walks = [
        root
        for root in walked
        if real_os.path.realpath(root) == secrets_real
        or real_os.path.realpath(root).startswith(secrets_real + real_os.sep)
    ]
    assert not escaping_walks, (
        "the run walked out of the tree the operator named before refusing. "
        "The write was still blocked downstream, but the victim directory's "
        "structure was enumerated on the way there; the guard exists to "
        "refuse before is_dir() follows the link.\n\n"
        f"out-of-root walk roots={escaping_walks}\nall walk roots={walked}"
    )


def test_redact_review_refuses_a_named_symlink_whose_target_escapes_its_parent(
    tmp_path: Path,
) -> None:
    """``--review`` refuses a named symlink, matching its in-tree contract.

    ``--review`` already refuses an escaping link *found inside* the vault
    (see the SEC-003 test above, whose message shape this mirrors). Naming
    that same link as ``--vault`` must not be the way around it: at HEAD
    ``vault.is_dir()`` is False for a link to a file, the guard is skipped
    entirely, and the out-of-tree target is scanned and rendered into the
    review queue.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.env"
    victim.write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    link = source / "linkvault.md"
    link.symlink_to(victim)

    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(link)],
    )

    assert result.exit_code == 1, (
        "--review followed a symlink named directly as --vault and rendered "
        "a review queue for a file outside it.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "symlink" in result.output.lower(), (
        "the refusal message does not name the reason, so it does not match "
        "the message-shape contract the in-tree SEC-003 refusal already "
        f"holds to.\n\nexit_code={result.exit_code}\n{result.output}"
    )


def test_redact_review_refusal_reads_no_config_through_the_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal happens BEFORE anything is read through the link.

    "Refuses before reading anything through the link" is the half of the
    contract that prose cannot check. A handler that resolves
    ``<vault>/00-Creek-Meta/creek_config.yaml`` through the symlink, opens
    it, parses it, applies its privacy and redaction settings — and only
    then refuses — still exits 1, still says "symlink", and still satisfies
    every other test in this section. It has nonetheless already read a file
    from outside the tree the operator named and let it steer the run.

    So the ordering is pinned directly: ``load_config`` is swapped for a
    recording wrapper and asserted never to have been reached. The wrapper
    delegates to the real loader rather than raising, because a raising
    sentinel would be caught by ``CliRunner`` and reported as exit 1 —
    manufacturing a pass for the exit-code assertion at HEAD.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.config import load_config as real_load_config

    outside_vault = tmp_path / "outside" / "vault"
    (outside_vault / "00-Creek-Meta").mkdir(parents=True)
    (outside_vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# empty config: parses to {} and validates to the defaults\n",
        encoding="utf-8",
    )
    (outside_vault / "notes.md").write_text(
        "SSN: 999-88-7777\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    link = source / "linkvault"
    link.symlink_to(outside_vault)

    calls: list[Path | None] = []

    def _recording_load_config(config_path=None, **kwargs):
        """Record the call, then delegate to the real loader."""
        calls.append(config_path)
        return real_load_config(config_path, **kwargs)

    monkeypatch.setattr(
        "creek.redact.cli_commands.load_config",
        _recording_load_config,
    )

    result = runner.invoke(
        app,
        ["redact", "--review", "--vault", str(link)],
    )

    assert result.exit_code == 1, (
        "--review accepted a symlinked directory named as --vault.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert not calls, (
        "load_config was called before the refusal, so the run read "
        "<vault>/00-Creek-Meta/creek_config.yaml through the symlink — a "
        "file outside the named tree, resolved and parsed, and allowed to "
        f"configure the run that then refused.\n\ncalls={calls}\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )


def test_redact_apply_refusal_reads_no_config_through_the_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--apply`` refuses before it reads config through the named link.

    The ``--apply`` counterpart to the ``--review`` ordering pin above, and
    the test that makes ``run_apply``'s own guard load-bearing rather than
    merely redundant. Without it, deleting the handler guard entirely still
    left every other test in this section green: the ``_scan_source``
    chokepoint caught the escape a few lines later and the run still exited
    1. Defence in depth is the point of having both, but a layer no test can
    tell the absence of is not a layer.

    What the later catch does not prevent is this: ``run_apply`` takes a
    ``--vault`` of its own, and ``resolve_config_path`` reads
    ``<vault>/00-Creek-Meta/creek_config.yaml`` when that file exists. Point
    both flags at the same escaping link and the chokepoint-only ordering
    opens, parses, and applies a config file from outside the named tree —
    letting the target steer the privacy and redaction settings of the run
    that is about to refuse it. Refusing after being configured by the thing
    you refused is not refusing.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from creek.config import load_config as real_load_config

    outside_vault = tmp_path / "outside" / "vault"
    (outside_vault / "00-Creek-Meta").mkdir(parents=True)
    (outside_vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# empty config: parses to {} and validates to the defaults\n",
        encoding="utf-8",
    )
    (outside_vault / "notes.md").write_text(
        "SSN: 999-88-7777\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    link = source / "linkvault"
    link.symlink_to(outside_vault)

    calls: list[Path | None] = []

    def _recording_load_config(config_path=None, **kwargs):
        """Record the call, then delegate to the real loader."""
        calls.append(config_path)
        return real_load_config(config_path, **kwargs)

    monkeypatch.setattr(
        "creek.redact.cli_commands.load_config",
        _recording_load_config,
    )

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(link),
            "--vault",
            str(link),
            "--yes",
        ],
    )

    assert result.exit_code == 1, (
        "--apply accepted a symlinked directory named on the command "
        f"line.\n\nexit_code={result.exit_code}\n{result.output}"
    )
    assert not calls, (
        "load_config was called before the refusal, so the run read "
        "<vault>/00-Creek-Meta/creek_config.yaml through the symlink and "
        "let a file outside the named tree configure the run that then "
        f"refused it.\n\ncalls={calls}\nexit_code={result.exit_code}\n"
        f"{result.output}"
    )


def test_redact_apply_refuses_an_escaping_vault_symlink(tmp_path: Path) -> None:
    """``--apply --vault <escaping link>`` is an out-of-root WRITE.

    ``--source`` is not the only path an operator names. ``run_apply`` also
    takes a ``--vault``, and it does two things with it that ``--source``
    never does: it reads ``<vault>/00-Creek-Meta/creek_config.yaml`` to
    configure the run, and it *appends the audit record* to
    ``<vault>/00-Creek-Meta/audit/redact.jsonl``. Point ``--vault`` at a
    symlink leaving its parent and both happen outside the named tree — the
    write creating the ``audit/`` directory itself if it does not exist.

    That makes this the second genuine out-of-root write in #1293, and the
    one the issue body, the guard, and every other test here missed: the
    source can be entirely innocent. Guarding only the scanned path leaves
    the audit trail — the thing an operator trusts to say what was touched —
    landing wherever a link points.

    The assertion is on the filesystem rather than the exit code alone: a
    refusal that still created the out-of-tree directory would satisfy an
    exit-code-only test while having already written outside the root.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    outside_vault = tmp_path / "outside" / "vault"
    (outside_vault / "00-Creek-Meta").mkdir(parents=True)
    (outside_vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "redaction:\n  enabled: true\n",
        encoding="utf-8",
    )
    root = tmp_path / "root"
    source = root / "src"
    source.mkdir(parents=True)
    (source / "leak.env").write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    # The link must sit somewhere that does NOT contain the target, or it
    # does not escape its own parent and there is nothing to refuse.
    link = root / "linkvault"
    link.symlink_to(outside_vault)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(link),
            "--yes",
        ],
    )

    assert result.exit_code == 1, (
        "--apply accepted an escaping symlink as --vault. The scanned source "
        "was innocent, so the source guard never fired, and the run went on "
        f"to write its audit trail through the link.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "symlink" in result.output.lower(), (
        f"the refusal does not say why it refused.\n\n{result.output}"
    )
    assert not (outside_vault / "00-Creek-Meta" / "audit").exists(), (
        "the audit log was written outside the tree the operator named — a "
        "real out-of-root write, and to the very record that is supposed to "
        f"say truthfully what this run touched.\n\n{result.output}"
    )


def test_redact_scan_skips_a_named_symlink_whose_target_escapes_its_parent(
    tmp_path: Path,
) -> None:
    """``--scan`` SKIPS a named escaping symlink; it does not refuse.

    The distinct contract, and the reason the fix needs a policy rather than
    an unconditional guard at the chokepoint. ``--scan`` writes nothing, so
    refusing an entire scan over one bad link would be a denial of service
    on the safety pass itself — the operator's only way of finding out what
    is exposed. That is the #1087 reasoning in the banner above, and naming
    the link on the command line does not change it: decline the file, keep
    the exit code at 0, and say so in the statistics.

    At HEAD there is no guard on ``run_scan`` at all, so the target's ``ssn``
    is read and reported. The skip-label assertion is also the non-vacuity
    guard here: unlike the #1087 test there is no in-root control file to
    read (the named source *is* the link), so "reported nothing" would
    otherwise be indistinguishable from "did nothing".

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text(_ESCAPING_TARGET_PII, encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    link = source / "linked.md"
    link.symlink_to(victim)

    result = runner.invoke(
        app,
        ["redact", "--scan", "--source", str(link)],
    )

    assert result.exit_code == 0, (
        "--scan refused a named escaping symlink. The read path skips; only "
        "the write path refuses, or one stray link disables the operator's "
        f"whole safety pass.\n\nexit_code={result.exit_code}\n{result.output}"
    )
    assert "ssn" not in result.output.lower(), (
        "the scan reported the PII type of a file outside the named path's "
        f"own parent, so it followed the link and read it.\n\n{result.output}"
    )
    assert _SYMLINK_SKIP_LABEL in result.output, (
        "the statistics table does not report the skip, so the operator "
        "reads a clean scan of a path whose contents were never "
        f"examined.\n\n{result.output}"
    )


def test_scan_source_refuses_an_escaping_named_path_under_the_refuse_policy(
    tmp_path: Path,
) -> None:
    """The chokepoint enforces the policy, not each individual handler.

    This is the regression guard for the exact omission that produced #1293.
    The guard lived in the handlers: ``run_apply`` calls it, ``run_review``
    calls it, ``run_scan`` does not — and all three then call the same
    ``_scan_source``. Under that shape a fourth mode, or any new caller of
    ``_scan_source``, inherits the hole by default and nothing fails until
    somebody notices.

    Moving the decision onto ``_scan_source`` with a required ``policy``
    inverts that: a new caller has to state what it wants before it will run
    at all. This test exercises the chokepoint directly, with no CLI in the
    way, so it keeps holding whatever the handlers are later refactored into.

    The imports are function-local on purpose. ``SymlinkPolicy`` does not
    exist yet, and a module-level import of a missing name is a collection
    error for the whole file — it would take the other tests here down with
    it and hide the real signal. The ``ImportError`` from this one test is
    the expected RED until the implementation lands.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    import typer
    from rich.console import Console

    from creek.config import load_config
    from creek.redact.cli_commands import SymlinkPolicy, _scan_source

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.env"
    victim.write_text(
        "API_KEY=sk-abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    link = source / "leak.env"
    link.symlink_to(victim)

    console = Console()
    config = load_config(None)

    with pytest.raises(typer.Exit) as excinfo:
        _scan_source(
            link,
            config,
            console=console,
            label="source",
            policy=SymlinkPolicy.REFUSE,
        )

    assert excinfo.value.exit_code == 1, (
        "the chokepoint aborted with the wrong code: 1 is the SEC-003 "
        "refusal, 2 is the generic 'bad usage' exit that a missing path "
        f"produces.\n\nexit_code={excinfo.value.exit_code}"
    )


def test_redact_apply_admits_a_named_intra_tree_symlink(tmp_path: Path) -> None:
    """A named symlink pointing inside its own parent still works.

    The over-breadth guard. "Refuse any named symlink" would pass every
    other test in this section while breaking the ordinary case of an alias
    beside the file it aliases — the same case ``--source <dir>`` has always
    allowed (see ``test_redact_apply_allows_internal_symlink``). Containment
    is about the target escaping, not about the link existing.

    Asserted through the named path rather than through ``real.md``, so the
    test holds whether the fix writes through the link or replaces it.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "real.md").write_text(
        "SSN: 123-45-6789\n",
        encoding="utf-8",
    )
    alias = source / "alias.md"
    alias.symlink_to(source / "real.md")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(alias), "--yes"],
    )

    assert result.exit_code == 0, (
        "a named symlink whose target sits under its own parent was "
        "refused; the fix is over-broad and now rejects ordinary "
        f"aliases.\n\nexit_code={result.exit_code}\n{result.output}"
    )
    redacted = alias.read_text(encoding="utf-8")
    assert "123-45-6789" not in redacted, (
        "the run exited 0 without redacting anything, so the exit code "
        f"above proves nothing.\n\n{redacted!r}\n\n{result.output}"
    )
    assert "[REDACTED" in redacted, (
        "the sensitive value is gone but no redaction marker replaced it; "
        f"the file was truncated or rewritten wholesale.\n\n{redacted!r}"
    )


def test_redact_apply_on_a_dangling_named_symlink_exits_two(tmp_path: Path) -> None:
    """ORDERING PIN THAT PASSES AT HEAD: existence is checked first.

    ``_require_existing`` gates on ``Path.exists()``, which follows the link
    and returns False when the target is missing, so a dangling link is a
    "not found" (exit 2) and never reaches the symlink guard at all. Pinned
    because the obvious way to implement #1293 — hoisting a symlink check
    above the existence check — would silently reclassify every broken link
    as a containment refusal and change an exit code operators script
    against.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    source = tmp_path / "src"
    source.mkdir()
    dangling = source / "dangling.md"
    dangling.symlink_to(tmp_path / "outside" / "gone.md")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(dangling), "--yes"],
    )

    assert result.exit_code == 2, (
        "a dangling link no longer exits 2; the existence check has been "
        "reordered behind the symlink guard.\n\n"
        f"exit_code={result.exit_code}\n{result.output}"
    )
    assert "not found" in result.output.lower(), (
        "a broken link should be reported as a missing path, not as a "
        f"containment failure.\n\n{result.output}"
    )


def test_redact_apply_on_a_looping_named_symlink_exits_two(tmp_path: Path) -> None:
    """ORDERING PIN THAT PASSES AT HEAD: a link loop is also "not found".

    ``a → b → a`` raises ``OSError(ELOOP)`` on resolution, and
    ``Path.exists()`` swallows that and returns False — so, exactly as with
    a dangling link, ``_require_existing`` reports "not found" and exits 2
    before any symlink logic runs. Written to that fact rather than to the
    tempting "exit 1 with a symlink message", which is not what the code
    does and would be a permanently red test dressed up as a requirement.

    Args:
        tmp_path: Pytest-provided temporary directory.
    """
    source = tmp_path / "src"
    source.mkdir()
    a = source / "a.md"
    b = source / "b.md"
    a.symlink_to(b)
    b.symlink_to(a)

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(a), "--yes"],
    )

    assert result.exit_code == 2, (
        "a looping link no longer exits 2; ELOOP is being handled somewhere "
        "other than the existence check, which is where Python's False "
        f"return puts it.\n\nexit_code={result.exit_code}\n{result.output}"
    )
    assert "not found" in result.output.lower(), (
        "the loop was reported as something other than a missing path.\n\n"
        f"{result.output}"
    )
