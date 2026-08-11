"""Tests for the redaction audit log and ``creek redact --apply`` wiring.

Two surfaces are exercised:

* :class:`creek.redact.audit.RedactionAuditLog` — append/read round trip
  and chain integrity inherited from :class:`creek.audit.AuditLog`.
* The ``creek redact --apply`` CLI flow, which writes a three-phase
  record: one ``intent`` line naming every candidate file BEFORE the
  first byte is rewritten, one ``file`` line per file appended
  immediately after that file's own atomic write, and one ``outcome``
  line (``complete`` or ``partial``) closing the run. Dry runs emit the
  same grammar with ``dry_run=True`` throughout (#1308).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.redact.audit import RedactionAuditEntry, RedactionAuditLog

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

SECRET_SSN = "123-45-6789"


def _write_secret_file(path: Path) -> None:
    """Write a file containing a synthetic SSN match."""
    path.write_text(f"contact: {SECRET_SSN}\n", encoding="utf-8")


def _make_vault(tmp_path: Path) -> Path:
    """Create a vault with the directories the audit log needs."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta" / "audit").mkdir(parents=True)
    return vault


def _make_bare_vault(tmp_path: Path) -> Path:
    """Create a vault root with **no** ``00-Creek-Meta`` directory.

    Deliberately not :func:`_make_vault`. ``AuditLog.append`` begins with
    ``self.path.parent.mkdir(parents=True, exist_ok=True)``
    (creek/audit/log.py:206), which short-circuits when ``audit/``
    already exists and therefore never needs write permission on the
    vault ROOT. A permission test built on :func:`_make_vault` would
    pass identically before and after the #1308 fix — certifying
    nothing. The directory must be absent for the append to have to
    create it.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _make_two_secret_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create ``src/a.md`` and ``src/b.md``, both carrying an SSN.

    ``_scannable_candidates`` ends ``return sorted(candidates), escaped``
    (creek/redact/scanner.py:866), so the rewrite loop visits ``a.md``
    first and ``b.md`` second. Tests here key their injected failures on
    CALL ORDER rather than on a filename precisely so they stay pinned
    to that loop order.

    Returns:
        ``(source_dir, a_md, b_md)``.
    """
    source = tmp_path / "src"
    source.mkdir()
    first = source / "a.md"
    second = source / "b.md"
    _write_secret_file(first)
    _write_secret_file(second)
    return source, first, second


def test_redaction_audit_round_trips(tmp_path: Path) -> None:
    """An appended entry is readable through :meth:`RedactionAuditLog.read`."""
    vault = _make_vault(tmp_path)
    log = RedactionAuditLog(vault)
    log.append(
        RedactionAuditEntry(
            source_path="/tmp/file.md",
            pattern_names=["ssn"],
            match_counts={"ssn": 2},
        ),
    )

    entries = log.read()
    assert len(entries) == 1
    assert entries[0].source_path == "/tmp/file.md"
    assert entries[0].match_counts == {"ssn": 2}


def test_redaction_audit_path_is_jsonl(tmp_path: Path) -> None:
    """The redaction audit lives at ``00-Creek-Meta/audit/redact.jsonl``."""
    vault = _make_vault(tmp_path)
    log = RedactionAuditLog(vault)
    log.append(
        RedactionAuditEntry(source_path="/tmp/x.md", pattern_names=["ssn"]),
    )

    expected = vault / "00-Creek-Meta" / "audit" / "redact.jsonl"
    assert log.log_path == expected
    assert expected.exists()
    raw = expected.read_text(encoding="utf-8")
    assert raw.count("\n") == 1


def test_cli_redact_apply_writes_audit_entry(tmp_path: Path) -> None:
    """A committed redact-apply call brackets its file entry in a run.

    Count changed from 1 to 3 deliberately in #1308. A lone ``file``
    line could not distinguish "this file was rewritten and the run
    finished" from "this file was rewritten and the process died before
    reaching the next one", which is exactly the ambiguity the issue was
    filed about. The run now brackets its per-file entries with an
    ``intent`` line (written before the first rewrite) and an
    ``outcome`` line, so the count is ``2 + len(files)``.
    """
    vault = _make_vault(tmp_path)
    target = tmp_path / "secret.md"
    _write_secret_file(target)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(target),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    audit_log = RedactionAuditLog(vault)
    entries = audit_log.read()
    assert [e.phase for e in entries] == ["intent", "file", "outcome"]
    entry = entries[1]
    assert entry.source_path == str(target)
    assert entry.dry_run is False
    assert entry.match_counts.get("ssn", 0) >= 1


def test_cli_redact_dry_run_writes_audit_entry(tmp_path: Path) -> None:
    """A dry run emits the same three-phase grammar, all ``dry_run=True``.

    Count changed from 1 to 3 deliberately in #1308. Dry runs COULD have
    kept emitting a bare ``file`` line, but then a reader of
    ``redact.jsonl`` would meet orphan ``phase="file"`` entries belonging
    to no run, and would have no way to tell one three-file preview from
    three separate one-file previews — ``operation_id`` is the only thing
    that groups them, and it only exists on a bracketed run. ``creek
    purge`` already emits intent/outcome for its dry runs
    (creek/purge/engine.py:1802), so this is the consistent choice too.
    """
    vault = _make_vault(tmp_path)
    target = tmp_path / "secret.md"
    _write_secret_file(target)
    original = target.read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(target),
            "--vault",
            str(vault),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == original
    entries = RedactionAuditLog(vault).read()
    assert [e.phase for e in entries] == ["intent", "file", "outcome"]
    assert all(e.dry_run is True for e in entries)
    assert entries[1].source_path == str(target)


def test_cli_redact_no_findings_writes_no_audit_entry(tmp_path: Path) -> None:
    """A clean source produces no audit entries."""
    vault = _make_vault(tmp_path)
    target = tmp_path / "clean.md"
    target.write_text("nothing to see here\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(target),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0
    assert RedactionAuditLog(vault).read() == []


def test_redaction_audit_chain_integrity(tmp_path: Path) -> None:
    """Tampering with the redaction log fails verify()."""
    from creek.audit import AuditChainBroken

    vault = _make_vault(tmp_path)
    log = RedactionAuditLog(vault)
    log.append(RedactionAuditEntry(source_path="a.md"))
    log.append(RedactionAuditEntry(source_path="b.md"))

    lines = log.log_path.read_text(encoding="utf-8").splitlines()
    log.log_path.write_text(lines[1] + "\n", encoding="utf-8")
    with pytest.raises(AuditChainBroken):
        log.verify()


# ---------------------------------------------------------------------------
# #1308 — the audit record must not lag the destruction it records
# ---------------------------------------------------------------------------


def test_apply_records_the_file_it_destroyed_when_the_batch_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-batch abort still names the file whose bytes were rewritten.

    This is the load-bearing regression test for #1308. Before the fix
    ``run_apply`` called ``_apply_redactions`` (which rewrites every
    file) and only then ``_write_redaction_audit``, so an ``OSError`` on
    the second of two files left ``a.md`` redacted on disk with the
    audit log not even created.

    Note that adding only an ``intent`` line and a ``partial``
    ``outcome`` line — the fix the issue body proposes — would satisfy
    "the log exists" and "status is partial" while STILL recording
    nothing whatsoever about ``a.md``. The per-file assertion below is
    what makes this test non-vacuous, and it only passes when the
    append moved INSIDE the rewrite loop.
    """
    import creek.redact.cli_commands as cli_commands

    vault = _make_vault(tmp_path)
    source, first, second = _make_two_secret_files(tmp_path)
    original_second = second.read_text(encoding="utf-8")

    real_atomic_write = cli_commands._atomic_write
    calls = {"n": 0}

    def flaky_atomic_write(file_path: Path, content: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real_atomic_write(file_path, content)

    monkeypatch.setattr(cli_commands, "_atomic_write", flaky_atomic_write)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    log = RedactionAuditLog(vault)
    assert log.log_path.exists(), (
        "a.md's bytes were rewritten and no audit line survives the abort"
    )
    assert "[REDACTED:" in first.read_text(encoding="utf-8")
    assert second.read_text(encoding="utf-8") == original_second

    entries = log.read()
    assert [e.phase for e in entries] == ["intent", "file", "outcome"]

    file_entries = [e for e in entries if e.phase == "file"]
    assert [e.source_path for e in file_entries] == [str(first)]
    assert str(second) not in {e.source_path for e in entries}

    assert entries[0].files == [str(first), str(second)]

    operation_ids = {e.operation_id for e in entries}
    assert len(operation_ids) == 1
    assert operation_ids != {""}

    outcome = entries[-1]
    assert outcome.status == "partial"
    # Exact equality, never a substring check: ``_apply_redactions``
    # converts the OSError into ``typer.Exit``, which subclasses
    # RuntimeError, so a wrapper placed OUTSIDE that conversion would
    # record the literal string "Exit" and certify nothing.
    assert outcome.failure_reason == "OSError"

    assert result.exit_code == 1, result.output
    assert "I/O error" in result.output
    log.verify()


def test_apply_never_rewrites_the_audit_log_it_is_writing(
    tmp_path: Path,
) -> None:
    """``--apply`` leaves ``00-Creek-Meta/audit/`` byte-identical.

    The audit directory sits inside the tree ``--apply`` walks:
    ``exclude_patterns`` defaults to ``['.git', 'node_modules']``
    (creek/config.py:795) and does not mention ``00-Creek-Meta``. Before
    #1308 ``redact.jsonl`` escaped only because ``.jsonl`` is absent
    from the *user-editable* ``supported_extensions`` list — add it, and
    a run rewrote its own first log line and then appended fresh entries
    that re-anchored the hash chain onto the mutated line, so
    ``verify()`` still returned ``True``. Silent, undetectable mutation
    of a tamper-evident log.

    The exclusion must therefore be unconditional, not an extension
    coincidence. The fixture vault is kept minimal on purpose: any extra
    scannable file would give the assertion somewhere else to land.
    """
    vault = _make_vault(tmp_path)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "redaction:\n"
        "  enabled: true\n"
        "  supported_extensions:\n"
        "    - .md\n"
        "    - .jsonl\n",
        encoding="utf-8",
    )
    log = RedactionAuditLog(vault)
    log.append(
        RedactionAuditEntry(
            source_path=f"/exports/ssn-{SECRET_SSN}.md",
            pattern_names=["ssn"],
            match_counts={"ssn": 1},
        ),
    )
    before = log.log_path.read_bytes()

    note = vault / "note.md"
    _write_secret_file(note)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(vault),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "[REDACTED:" in note.read_text(encoding="utf-8"), (
        "the ordinary note should still have been redacted"
    )
    assert log.log_path.read_bytes().startswith(before), (
        "the run rewrote its own audit history in place"
    )
    assert SECRET_SSN in before.decode("utf-8")
    log.verify()

    file_entries = [e for e in log.read() if e.phase == "file"]
    assert str(log.log_path) not in {e.source_path for e in file_entries}


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignores mode bits, so the unwritable vault is writable",
)
def test_apply_refuses_before_destroying_anything_when_audit_is_unwritable(
    tmp_path: Path,
) -> None:
    """An unwritable audit destination aborts BEFORE the first rewrite.

    This is the strongest argument for writing the intent line first,
    and the issue body never mentions it: before #1308 the run rewrote
    the source file and only then discovered it could not create
    ``00-Creek-Meta/``, dying with a raw ``PermissionError`` traceback
    and zero audit lines. "Destroy, then discover you cannot log it"
    becomes "fail before touching anything".

    The vault must have no ``00-Creek-Meta`` directory — see
    :func:`_make_bare_vault` for why reusing :func:`_make_vault` would
    make this test vacuous.
    """
    vault = _make_bare_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    target = source / "secret.md"
    _write_secret_file(target)
    original = target.read_text(encoding="utf-8")

    vault.chmod(0o500)
    try:
        result = runner.invoke(
            app,
            [
                "redact",
                "--apply",
                "--source",
                str(source),
                "--vault",
                str(vault),
                "--yes",
            ],
        )
    finally:
        vault.chmod(0o700)

    assert result.exit_code == 1, result.output
    assert target.read_text(encoding="utf-8") == original, (
        "the file was destroyed before the audit destination was proven"
    )
    assert isinstance(result.exception, SystemExit), (
        f"expected a clean typer.Exit, got {result.exception!r}"
    )
    assert "Traceback" not in result.output
    # Rich hard-wraps console output at the terminal width, so a long
    # path in the middle of the message can split any phrase after it.
    flattened = " ".join(result.output.split())
    assert "audit" in flattened.lower()
    assert "No files were modified" in flattened
    assert not RedactionAuditLog(vault).log_path.exists()


def test_failure_reason_records_the_type_and_never_the_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``failure_reason`` carries the exception TYPE NAME only.

    ``str(OSError)`` embeds the offending path, and filenames in a
    redaction workflow demonstrably carry the very secrets being
    redacted. ``creek purge`` already makes this argument at
    creek/purge/engine.py:1776 — "the type is the forensic value; the
    message is the leak".

    Asserted on the RAW JSONL BYTES rather than on the parsed model, so
    a leak through any other field is caught too.
    """
    import creek.redact.cli_commands as cli_commands

    vault = _make_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    target = source / "secret.md"
    _write_secret_file(target)

    leaky_path = f"/vault/ssn-{SECRET_SSN}.md"

    def leaky_atomic_write(file_path: Path, content: str) -> None:
        # errno 28 (ENOSPC) deliberately: errno 2 would be auto-promoted
        # to FileNotFoundError and the assertion below would be testing
        # the subclass name rather than the type-not-message rule.
        raise OSError(28, "No space left on device", leaky_path)

    monkeypatch.setattr(cli_commands, "_atomic_write", leaky_atomic_write)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )
    assert result.exit_code == 1

    log = RedactionAuditLog(vault)
    raw = log.log_path.read_bytes()
    assert leaky_path.encode("utf-8") not in raw
    assert f"ssn-{SECRET_SSN}".encode() not in raw
    assert b"No space left on device" not in raw

    outcome = log.read()[-1]
    assert outcome.phase == "outcome"
    assert outcome.failure_reason == "OSError"


def test_keyboard_interrupt_mid_batch_records_a_partial_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C mid-batch still closes the run with ``status="partial"``.

    Interactive interruption is the likeliest real-world abort, and
    ``KeyboardInterrupt`` does not inherit from ``Exception`` — only a
    ``BaseException`` handler (mirroring creek/purge/engine.py:1805)
    records it.
    """
    import creek.redact.cli_commands as cli_commands

    vault = _make_vault(tmp_path)
    source, first, second = _make_two_secret_files(tmp_path)
    original_second = second.read_text(encoding="utf-8")

    real_atomic_write = cli_commands._atomic_write
    calls = {"n": 0}

    def interrupted_atomic_write(file_path: Path, content: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        real_atomic_write(file_path, content)

    monkeypatch.setattr(cli_commands, "_atomic_write", interrupted_atomic_write)

    runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    entries = RedactionAuditLog(vault).read()
    assert [e.phase for e in entries] == ["intent", "file", "outcome"]
    assert [e.source_path for e in entries if e.phase == "file"] == [str(first)]
    assert second.read_text(encoding="utf-8") == original_second
    assert entries[-1].status == "partial"
    assert entries[-1].failure_reason == "KeyboardInterrupt"


def test_audit_write_failure_stops_the_batch_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the AUDIT device fails, the loop stops and says so.

    The scenario the issue is actually about is ENOSPC on the vault
    volume — which fails the audit append just as readily as the file
    rewrite. If the per-file append raises and nothing catches it, the
    run cannot record what it is still destroying, so it must stop with
    the damage bounded to the entries already written, and it must not
    print the "I/O error after redacting K of N file(s)" line, which
    would misreport a SUCCESSFUL rewrite as a failed one.
    """
    from creek.redact.audit import RedactionAuditLog as LogClass

    vault = _make_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    names = ["a.md", "b.md", "c.md"]
    paths = [source / name for name in names]
    for path in paths:
        _write_secret_file(path)
    original_third = paths[2].read_text(encoding="utf-8")

    real_append = LogClass.append
    calls = {"n": 0}

    def flaky_append(self: LogClass, entry: RedactionAuditEntry) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        real_append(self, entry)

    monkeypatch.setattr(LogClass, "append", flaky_append)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert "audit" in result.output.lower()
    assert "I/O error after redacting" not in result.output
    assert paths[2].read_text(encoding="utf-8") == original_third, (
        "the loop kept destroying files it could no longer record"
    )

    monkeypatch.undo()
    entries = RedactionAuditLog(vault).read()
    assert [e.phase for e in entries] == ["intent", "file", "outcome"]
    assert [e.source_path for e in entries if e.phase == "file"] == [
        str(paths[0]),
    ]
    assert entries[-1].status == "partial"
    assert entries[-1].failure_reason == "OSError"


def test_declined_prompt_writes_no_audit_lines(tmp_path: Path) -> None:
    """Declining the confirmation prompt leaves the log untouched.

    The intent line must be written AFTER consent, not before it: an
    operator who says "no" has authorised nothing, and a log naming
    their candidate files would be a disclosure they declined.
    """
    vault = _make_vault(tmp_path)
    target = tmp_path / "secret.md"
    _write_secret_file(target)
    original = target.read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(target),
            "--vault",
            str(vault),
        ],
        input="n\n",
    )

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == original
    assert RedactionAuditLog(vault).read() == []
    assert not RedactionAuditLog(vault).log_path.exists()


def test_successful_apply_writes_intent_files_outcome_in_order(
    tmp_path: Path,
) -> None:
    """A clean two-file apply logs intent, file, file, outcome — in order."""
    vault = _make_vault(tmp_path)
    source, first, second = _make_two_secret_files(tmp_path)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    log = RedactionAuditLog(vault)
    entries = log.read()
    assert [e.phase for e in entries] == ["intent", "file", "file", "outcome"]
    assert [e.source_path for e in entries if e.phase == "file"] == [
        str(first),
        str(second),
    ]
    assert entries[0].files == [str(first), str(second)]
    assert entries[-1].status == "complete"
    assert entries[-1].failure_reason is None
    assert len({e.operation_id for e in entries}) == 1
    log.verify()


def test_legacy_entries_read_back_unchanged_under_the_new_schema(
    tmp_path: Path,
) -> None:
    """A pre-#1308 log line still parses, as the per-file record it was.

    ``phase`` defaults to ``"file"`` rather than purge's ``"outcome"``
    because a legacy redaction line genuinely WAS a per-file record.
    """
    from creek.audit import AuditLog

    vault = _make_vault(tmp_path)
    log = RedactionAuditLog(vault)
    AuditLog(log.log_path).append(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "source_path": "/legacy/file.md",
            "pattern_names": ["ssn"],
            "match_counts": {"ssn": 3},
            "replacement_template": "[REDACTED:{name}]",
            "operator": "human via CLI",
            "dry_run": False,
        },
    )

    entries = log.read()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source_path == "/legacy/file.md"
    assert entry.match_counts == {"ssn": 3}
    assert entry.phase == "file"
    assert entry.operation_id == ""
    assert entry.status is None
    assert entry.failure_reason is None
    assert entry.files == []


def test_a_file_entry_must_name_the_file_it_records() -> None:
    """``phase="file"`` without a ``source_path`` is rejected.

    Making ``source_path`` optional (so intent and outcome lines can
    omit it) would otherwise silently drop the invariant the required
    field used to carry: a per-file record that names no file certifies
    nothing.
    """
    with pytest.raises(ValueError, match="source_path"):
        RedactionAuditEntry(phase="file", source_path=None)

    intent = RedactionAuditEntry(phase="intent", files=["/a.md"])
    assert intent.source_path is None


def test_apply_never_rewrites_the_legacy_purge_log(tmp_path: Path) -> None:
    """``00-Creek-Meta/Processing-Log/purge-log.json`` is protected too.

    It sits *outside* the ``audit/`` directory, and ``.json`` is in the
    default ``supported_extensions``, so a vault-wide apply rewrote it.
    That is worse than losing data:
    ``PurgeAuditLog._migrate_legacy_if_needed`` replays this file into
    the hash-chained ``purge.jsonl`` on next use, so the mutated records
    would be chain-*signed* into the purge trail as authentic.
    """
    from creek.purge.audit import LEGACY_PURGE_LOG_RELPATH

    vault = _make_vault(tmp_path)
    legacy = vault / LEGACY_PURGE_LOG_RELPATH
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy_body = f'[{{"target": "fragment-{SECRET_SSN}", "count": 1}}]\n'
    legacy.write_text(legacy_body, encoding="utf-8")

    note = vault / "note.md"
    _write_secret_file(note)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(vault),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "[REDACTED:" in note.read_text(encoding="utf-8")
    assert legacy.read_text(encoding="utf-8") == legacy_body, (
        "the legacy purge log was redacted; its mutated records would be "
        "replayed and chain-signed into purge.jsonl on next use"
    )


def test_a_clean_source_without_a_vault_flag_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``--vault`` plus no findings must not create an audit log.

    ``CreekConfig.vault_path`` defaults to ``Path()`` — the process CWD.
    If the intent line ever migrated above the "nothing to redact" early
    return, every clean apply run without ``--vault`` would start
    materialising a hash-chained log wherever the operator happens to be
    standing (and, in CI, inside the repo tree). This pins the ordering
    from the other side than the chmod test does.
    """
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "clean.md"
    target.write_text("nothing to see here\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["redact", "--apply", "--source", str(target), "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "00-Creek-Meta").exists()


def test_a_failed_outcome_write_warns_instead_of_masking_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A best-effort outcome write that fails is announced, not swallowed.

    ``record_outcome`` must never raise — on the failure path it runs
    inside an ``except`` clause and would displace the original error.
    But that leniency has a cost on the SUCCESS path: intent + N file
    entries + no outcome is exactly the shape the documented grammar
    calls "aborted". A fully successful run would read as an ambiguous
    partial. The warning line is the only thing that distinguishes them,
    so it is asserted rather than assumed, and the docs say a missing
    outcome may mean the outcome write itself failed.
    """
    from creek.redact.audit import RedactionAuditLog as LogClass

    vault = _make_vault(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    target = source / "secret.md"
    _write_secret_file(target)

    real_append = LogClass.append
    calls = {"n": 0}

    def flaky_append(self: LogClass, entry: RedactionAuditEntry) -> None:
        calls["n"] += 1
        if entry.phase == "outcome":
            raise OSError(28, "No space left on device")
        real_append(self, entry)

    monkeypatch.setattr(LogClass, "append", flaky_append)

    result = runner.invoke(
        app,
        [
            "redact",
            "--apply",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    flattened = " ".join(result.output.split())
    assert "could not record the redaction outcome" in flattened.lower()
    assert "Traceback" not in result.output

    monkeypatch.undo()
    entries = RedactionAuditLog(vault).read()
    assert [e.phase for e in entries] == ["intent", "file"]


def test_an_unresolvable_candidate_is_treated_as_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_is_protected`` fails closed when a path cannot be resolved.

    Refusing to redact a file is recoverable; corrupting the audit log
    is not. This is the arm that :func:`creek._containment.
    resolves_within` gets backwards for this question, which is why the
    exclusion has its own predicate.
    """
    from pathlib import Path as RealPath

    from rich.console import Console

    import creek.redact.cli_commands as cli_commands

    real_resolve = RealPath.resolve
    doomed = tmp_path / "unresolvable.md"

    def flaky_resolve(self: RealPath, strict: bool = False) -> RealPath:
        if self == doomed:
            raise OSError(40, "Too many levels of symbolic links")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(RealPath, "resolve", flaky_resolve)

    kept = cli_commands._exclude_audit_artifacts(
        [doomed, tmp_path / "fine.md"],
        tmp_path / "vault",
        console=Console(),
    )
    assert kept == [tmp_path / "fine.md"]


def test_atomic_write_has_exactly_one_call_site_and_it_records() -> None:
    """Structural guard: rewriting a file and recording it are one step.

    The #1308 defect was an ORDERING bug, and ordering bugs come back.
    Rather than trust review to catch a reintroduced "append all the
    entries after the loop", this pins the shape: ``_atomic_write`` has
    exactly one caller in the module, that caller is
    ``_rewrite_and_record``, and the same function body also calls
    ``record_file``.
    """
    import ast
    import inspect
    from pathlib import Path as RealPath

    import creek.redact.cli_commands as cli_commands

    module_source = RealPath(inspect.getfile(cli_commands)).read_text(
        encoding="utf-8",
    )
    tree = ast.parse(module_source)

    def _called_names(node: ast.AST) -> list[str]:
        names: list[str] = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
        return names

    assert _called_names(tree).count("_atomic_write") == 1, (
        "_atomic_write must have exactly one call site so that rewriting "
        "and recording cannot drift apart"
    )

    recorder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_rewrite_and_record"
    )
    body_calls = _called_names(recorder)
    assert "_atomic_write" in body_calls
    assert "record_file" in body_calls, (
        "the per-file audit append must sit next to the write it records, "
        "not in a batch loop after the rewrite loop"
    )
