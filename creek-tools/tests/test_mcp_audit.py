"""Tests for the MCP audit log (FEAT-010)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from creek_mcp.audit import MCP_AUDIT_RELPATH, MCPAuditLog, summarise_args
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path


def test_summarise_args_keeps_short_scalars() -> None:
    """Scalars and short strings pass through unchanged."""
    summary = summarise_args({"phase": "rising", "index": 3, "limit": 10})
    assert summary == {"phase": "rising", "index": 3, "limit": 10}


def test_summarise_args_compacts_long_strings() -> None:
    """Long strings are reported by length, not body."""
    body = "x" * 4096
    summary = summarise_args({"body": body, "title": "ok"})
    assert summary["body"] == {"len": 4096}
    assert summary["title"] == "ok"


def test_summarise_args_compacts_lists_and_dicts() -> None:
    """Lists report counts; dicts report keys (not values)."""
    summary = summarise_args(
        {
            "fragments": ["a", "b", "c"],
            "meta": {"author": "Geoff", "tier": "intimate"},
        },
    )
    assert summary["fragments"] == {"count": 3}
    assert summary["meta"] == {"keys": ["author", "tier"]}


def test_append_writes_jsonl_entry_under_vault(tmp_path: Path) -> None:
    """A tool invocation writes one chained JSONL line to mcp.jsonl."""
    log = MCPAuditLog(tmp_path)
    log.append(
        tool="creek.mine",
        args={"phase": "rising", "limit": 10},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    log_file = tmp_path / MCP_AUDIT_RELPATH
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "creek.mine"
    assert entry["tier_ceiling"] == "open"
    assert entry["consumer"] == "claude-code"
    assert entry["args_summary"] == {"phase": "rising", "limit": 10}
    assert "prev_hash" in entry  # chained via creek.audit.AuditLog
    assert "timestamp" in entry


def test_append_does_not_leak_full_body(tmp_path: Path) -> None:
    """Regression: full fragment bodies must not enter the audit log.

    The FEAT-010 acceptance criterion: ``args_summary`` not full args.
    An intimate-tier draft request with a 10kB body should record only
    the length.
    """
    log = MCPAuditLog(tmp_path)
    body = "intimate prose " * 1000
    log.append(
        tool="creek.draft",
        args={"prompt_body": body, "phase": "rising"},
        tier_ceiling=TierCeiling.INTIMATE,
        consumer="crawdad",
    )
    contents = (tmp_path / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert body not in contents
    entry = json.loads(contents.splitlines()[0])
    assert entry["args_summary"]["prompt_body"] == {"len": len(body)}
    assert entry["args_summary"]["phase"] == "rising"
