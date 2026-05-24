"""Tests for the ``creek.redact.scan`` MCP tool (FEAT-027).

Covers: an empty staging dir, findings present, findings absent, refusal
for paths outside the vault, refusal for missing paths, audit logging,
and scanning a single file (not a directory).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.config import CreekConfig
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.redact import redact_scan_tool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Vault skeleton with the audit dir + Inbound staging dir and a stub config."""
    (tmp_path / "00-Creek-Meta" / "audit").mkdir(parents=True)
    (tmp_path / "00-Creek-Meta" / "Inbound").mkdir(parents=True)

    monkeypatch.setattr(
        "creek_mcp.tools.redact.load_config",
        lambda: CreekConfig(),
    )
    yield tmp_path


def _read_audit(vault: Path) -> list[dict[str, object]]:
    path = vault / MCP_AUDIT_RELPATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_scan_returns_empty_when_no_findings(vault: Path) -> None:
    """A clean staging directory yields ``status="empty"`` with no findings."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg1"
    staging.mkdir(parents=True)
    (staging / "note.md").write_text("# Title\n\nSome safe markdown text.\n")

    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/ch1/msg1",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result["status"] == "empty"
    assert result["tool"] == "creek.redact.scan"
    assert result["statistics"]["total_findings"] == 0
    assert result["statistics"]["files_scanned"] == 1
    assert result["findings"] == []
    assert "Redaction Scan Summary" in result["report_markdown"]


def test_scan_reports_findings_when_pii_present(vault: Path) -> None:
    """A staging file with an email address surfaces a structured finding."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg2"
    staging.mkdir(parents=True)
    (staging / "leak.md").write_text(
        "Reach me at someone@example.com any time.\n",
    )

    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/ch1/msg2",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="crawdad",
    )

    assert result["status"] == "ok"
    assert result["statistics"]["total_findings"] >= 1
    finding = result["findings"][0]
    assert finding["match_type"] == "email"
    assert finding["severity"] in {"medium", "high"}
    # File path is rendered relative to the vault root — no absolute leak.
    assert finding["file_path"] == "00-Creek-Meta/Inbound/ch1/msg2/leak.md"
    # The matched text itself is never returned, only a salted hash.
    assert "someone@example.com" not in json.dumps(result)
    assert len(finding["salted_hash"]) == 64  # SHA-256 hex


def test_scan_refuses_absolute_path_outside_vault(
    vault: Path,
    tmp_path: Path,
) -> None:
    """Paths outside the vault root are refused, never scanned."""
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    result = redact_scan_tool(
        vault_path=vault,
        input_path=str(outside),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "outside the vault" in result["reason"]


def test_scan_refuses_missing_path(vault: Path) -> None:
    """A non-existent vault-relative path is refused cleanly."""
    result = redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/does/not/exist",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert "not found" in result["reason"]


def test_scan_writes_audit_entry(vault: Path) -> None:
    """Every invocation appends one entry to ``mcp.jsonl`` with the tool name."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg3"
    staging.mkdir(parents=True)
    (staging / "note.txt").write_text("nothing to see here\n")

    redact_scan_tool(
        vault_path=vault,
        input_path="00-Creek-Meta/Inbound/ch1/msg3",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="crawdad",
    )

    entries = _read_audit(vault)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "creek.redact.scan"
    assert entry["tier_ceiling"] == "personal"
    assert entry["consumer"] == "crawdad"


def test_scan_accepts_single_file_path(vault: Path) -> None:
    """A file (not a directory) is scanned as a one-file batch."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg4"
    staging.mkdir(parents=True)
    target = staging / "single.md"
    target.write_text("Plain notes, no secrets.\n")

    result = redact_scan_tool(
        vault_path=vault,
        input_path=str(target),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "empty"
    assert result["statistics"]["files_scanned"] == 1


def test_scan_accepts_absolute_path_inside_vault(vault: Path) -> None:
    """An absolute path inside the vault is accepted (not just relative)."""
    staging = vault / "00-Creek-Meta" / "Inbound" / "ch1" / "msg5"
    staging.mkdir(parents=True)
    (staging / "note.md").write_text("clean\n")

    result = redact_scan_tool(
        vault_path=vault,
        input_path=str(staging),
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "empty"
    assert result["input_path"].startswith("00-Creek-Meta/Inbound/")
