"""Tests for the MCP audit log (FEAT-010 + FEAT-012 hardening)."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
from typing import TYPE_CHECKING

import pytest

from creek_mcp.audit import (
    MCP_AUDIT_RELPATH,
    MCPAuditChainBrokenError,
    MCPAuditLog,
    summarise_args,
    verify_mcp_audit_chain,
)
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


# ---------------------------------------------------------------------------
# FEAT-012 audit hardening: entry_hash + verifier + locking
# ---------------------------------------------------------------------------


def test_append_stamps_entry_hash_alongside_prev_hash(tmp_path: Path) -> None:
    """Every entry carries both ``prev_hash`` and a content ``entry_hash``."""
    log = MCPAuditLog(tmp_path)
    log.append(
        tool="creek.lint",
        args={"checks": ["double-link"]},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    entry = json.loads(
        (tmp_path / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()[0],
    )
    assert "prev_hash" in entry
    assert "entry_hash" in entry
    body = {k: v for k, v in entry.items() if k not in {"entry_hash", "prev_hash"}}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    assert entry["entry_hash"] == expected


def test_verifier_accepts_untampered_chain(tmp_path: Path) -> None:
    """A clean chain passes :func:`verify_mcp_audit_chain` silently."""
    log = MCPAuditLog(tmp_path)
    for tool_name in ("creek.lint", "creek.mine", "creek.state.read"):
        log.append(
            tool=tool_name,
            args={"x": 1},
            tier_ceiling=TierCeiling.OPEN,
            consumer="claude-code",
        )
    verify_mcp_audit_chain(tmp_path)


def test_verifier_detects_mutated_payload(tmp_path: Path) -> None:
    """Mutating any field of a stored entry breaks ``entry_hash``."""
    log = MCPAuditLog(tmp_path)
    log.append(
        tool="creek.lint",
        args={"x": 1},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    log.append(
        tool="creek.mine",
        args={"x": 2},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    log_path = tmp_path / MCP_AUDIT_RELPATH
    lines = log_path.read_text(encoding="utf-8").splitlines()
    tampered_first = lines[0].replace('"creek.lint"', '"creek.LINT"')
    log_path.write_text(tampered_first + "\n" + lines[1] + "\n", encoding="utf-8")

    with pytest.raises(MCPAuditChainBrokenError):
        verify_mcp_audit_chain(tmp_path)


def test_verifier_detects_removed_entry(tmp_path: Path) -> None:
    """Dropping a line breaks the ``prev_hash`` of the next entry."""
    log = MCPAuditLog(tmp_path)
    log.append(
        tool="creek.lint",
        args={"x": 1},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    log.append(
        tool="creek.mine",
        args={"x": 2},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    log.append(
        tool="creek.state.read",
        args={},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )
    log_path = tmp_path / MCP_AUDIT_RELPATH
    lines = log_path.read_text(encoding="utf-8").splitlines()
    log_path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")

    with pytest.raises(MCPAuditChainBrokenError):
        verify_mcp_audit_chain(tmp_path)


def test_verifier_handles_missing_log(tmp_path: Path) -> None:
    """A missing log is trivially valid — there are no entries to disagree about."""
    verify_mcp_audit_chain(tmp_path)


def _append_one(path: Path, worker: int) -> None:
    """Top-level helper so ``multiprocessing`` can import it."""
    log = MCPAuditLog(path)
    log.append(
        tool="creek.lint",
        args={"worker": worker},
        tier_ceiling=TierCeiling.OPEN,
        consumer="claude-code",
    )


def test_concurrent_process_appends_do_not_corrupt_log(tmp_path: Path) -> None:
    """Two processes appending in parallel produce a still-verifiable chain.

    FEAT-012 test plan: concurrent writes from separate processes must
    not corrupt the log. The underlying :class:`creek.audit.AuditLog`
    holds an exclusive flock during write; we exercise that path here
    and assert the chain still passes verification afterwards.
    """
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_append_one, args=(tmp_path, i)) for i in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    lines = (tmp_path / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    verify_mcp_audit_chain(tmp_path)
