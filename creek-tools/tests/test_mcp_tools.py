"""Per-tool integration tests for the MCP wrappers (FEAT-010).

This PR covers ``creek.state.read`` and ``creek.state.render``. The
``lint``/``mine``/``draft`` wrappers land in FEAT-010 part 2 with their
own per-tool tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.state import state_render_tool
from creek_mcp.tools.state_read import state_read_tool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _seed_vault(vault: Path) -> None:
    """Create the minimum folder layout the read tools expect."""
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "04-Praxis/Daily",
        "10-Liminal/Synchronicities",
    ):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    title: str = "Note",
    privacy_tier: str = "open",
    body: str = "body text",
) -> None:
    """Write a minimal fragment file (frontmatter + body)."""
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": title,
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault rooted under ``tmp_path``."""
    _seed_vault(tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# state.read
# ---------------------------------------------------------------------------


def test_state_read_returns_latest_md_content(vault: Path) -> None:
    """``state.read`` reads ``00-Creek-Meta/State/latest.md`` verbatim."""
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit report\n\nVault summary lives here.\n",
        encoding="utf-8",
    )
    result = state_read_tool(vault_path=vault)
    assert result["status"] == "ok"
    assert result["tool"] == "creek.state.read"
    assert result["tier_ceiling"] == "open"
    assert "Audit report" in result["content"]


def test_state_read_returns_empty_on_missing_report(vault: Path) -> None:
    """A fresh vault with no rendered report yields a structured "empty"."""
    result = state_read_tool(vault_path=vault)
    assert result["status"] == "empty"
    assert result["content"] == ""


def test_state_read_writes_audit_entry(vault: Path) -> None:
    """Every invocation appends one chained line to ``mcp.jsonl``."""
    state_read_tool(
        vault_path=vault,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="claude-code",
    )
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.state.read"
    assert entry["consumer"] == "claude-code"
    assert entry["tier_ceiling"] == "personal"


def test_state_read_does_not_embed_fragment_bodies(vault: Path) -> None:
    """Intimate fragment bodies must not appear in the audit report.

    The audit report aggregates titles + counts. A vault with an
    ``intimate``-tier fragment must not see its body surface through
    ``state.read`` even when the caller specifies ``ceiling=open``.
    """
    _write_fragment(
        vault,
        frag_id="frag-intimate-1",
        title="Private moment",
        privacy_tier="intimate",
        body="this is a secret journal body",
    )
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit report\n\n- Fragments: 1\n",
        encoding="utf-8",
    )
    result = state_read_tool(vault_path=vault)
    assert "secret journal body" not in result["content"]


# ---------------------------------------------------------------------------
# state.render
# ---------------------------------------------------------------------------


def test_state_render_writes_report_file(vault: Path) -> None:
    """``state.render`` regenerates ``State/<iso-week>.md`` and returns it."""
    result = state_render_tool(vault_path=vault)
    assert result["status"] == "ok"
    assert result["report_path"].startswith("00-Creek-Meta/State/")
    assert "# Creek state" in result["content"]


def test_state_render_writes_audit_entry(vault: Path) -> None:
    """Render path also writes the audit entry."""
    state_render_tool(vault_path=vault, consumer="crawdad")
    raw = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8")
    entry = json.loads(raw.splitlines()[0])
    assert entry["tool"] == "creek.state.render"
    assert entry["consumer"] == "crawdad"
