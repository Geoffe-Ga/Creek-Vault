"""Tests for the redaction audit log and ``creek redact --apply`` wiring.

Two surfaces are exercised:

* :class:`creek.redact.audit.RedactionAuditLog` — append/read round trip
  and chain integrity inherited from :class:`creek.audit.AuditLog`.
* The ``creek redact --apply`` CLI flow — both ``--dry-run`` and the
  committed apply must write one entry per touched file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.redact.audit import RedactionAuditEntry, RedactionAuditLog

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _write_secret_file(path: Path) -> None:
    """Write a file containing a synthetic SSN match."""
    path.write_text("contact: 123-45-6789\n", encoding="utf-8")


def _make_vault(tmp_path: Path) -> Path:
    """Create a vault with the directories the audit log needs."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta" / "audit").mkdir(parents=True)
    return vault


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
    """A committed redact-apply call writes one audit entry per file."""
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
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source_path == str(target)
    assert entry.dry_run is False
    assert entry.match_counts.get("ssn", 0) >= 1


def test_cli_redact_dry_run_writes_audit_entry(tmp_path: Path) -> None:
    """A dry-run apply still writes an audit entry, marked dry_run=True."""
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
    assert len(entries) == 1
    assert entries[0].dry_run is True


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
