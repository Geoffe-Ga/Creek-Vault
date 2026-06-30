"""``creek.wheel`` MCP tool — per-frequency balance read for the Map (#752).

Returns a per-frequency fullness map over the classified corpus — every
frequency F1-F10 present (thin ones at zero), framed as **balance, not
ranking**. Deterministic, ceiling-filtered, audit-logged, read-only, and
graceful on an empty (new-user) vault.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.wheel import TOOL_NAME, wheel_tool

if TYPE_CHECKING:
    from pathlib import Path

_ALL_CODES = [f"F{n}" for n in range(1, 11)]


def _seed_dirs(vault: Path) -> None:
    """Create the folders the loader + audit log expect."""
    for sub in ("00-Creek-Meta/audit", "01-Fragments/Notes"):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _write_fragment(
    vault: Path,
    *,
    frag_id: str,
    primary: str = "F1",
    privacy_tier: str = "open",
) -> None:
    """Write a minimal classified fragment under ``01-Fragments/Notes``."""
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": "Note",
        "created": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "ingested": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": primary, "secondary": []},
        "privacy_tier": privacy_tier,
        "eddies": [],
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="body text", **metadata)),
        encoding="utf-8",
    )


def _audit(vault: Path) -> list[dict[str, object]]:
    """Return parsed MCP audit-log entries under *vault*."""
    log = vault / MCP_AUDIT_RELPATH
    assert log.exists()
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_wheel_includes_every_frequency_even_thin_ones(tmp_path: Path) -> None:
    """All F1-F10 appear; a frequency with no fragments is present at zero."""
    _seed_dirs(tmp_path)
    _write_fragment(tmp_path, frag_id="a", primary="F1")
    _write_fragment(tmp_path, frag_id="b", primary="F1")
    _write_fragment(tmp_path, frag_id="c", primary="F3")

    result = wheel_tool(vault_path=tmp_path)

    assert result["status"] == "ok"
    assert result["tool"] == TOOL_NAME
    assert list(result["wheel"].keys()) == _ALL_CODES  # complete + ordered F1→F10
    assert result["wheel"]["F1"]["count"] == 2
    assert result["wheel"]["F3"]["count"] == 1
    assert result["wheel"]["F7"]["count"] == 0  # thin frequency still present
    assert result["wheel"]["F1"]["name"] == "Agency"


def test_wheel_shares_are_balance_fractions(tmp_path: Path) -> None:
    """``share`` is a normalised balance fraction over the classified total."""
    _seed_dirs(tmp_path)
    _write_fragment(tmp_path, frag_id="a", primary="F1")
    _write_fragment(tmp_path, frag_id="b", primary="F1")
    _write_fragment(tmp_path, frag_id="c", primary="F5")

    wheel = wheel_tool(vault_path=tmp_path)["wheel"]

    assert wheel["F1"]["share"] == 2 / 3
    assert wheel["F5"]["share"] == 1 / 3
    assert wheel["F2"]["share"] == 0.0
    assert sum(cell["share"] for cell in wheel.values()) == 1.0


def test_empty_vault_yields_all_zeros(tmp_path: Path) -> None:
    """A new-user vault returns a complete all-zero wheel, not an error."""
    _seed_dirs(tmp_path)
    result = wheel_tool(vault_path=tmp_path)
    assert result["status"] == "ok"
    assert result["total_classified"] == 0
    assert list(result["wheel"].keys()) == _ALL_CODES
    assert all(
        cell["count"] == 0 and cell["share"] == 0.0 for cell in result["wheel"].values()
    )


def test_ceiling_excludes_above_ceiling_fragments(tmp_path: Path) -> None:
    """An INTIMATE fragment is counted only when the ceiling admits it."""
    _seed_dirs(tmp_path)
    _write_fragment(tmp_path, frag_id="open", primary="F1", privacy_tier="open")
    _write_fragment(tmp_path, frag_id="intimate", primary="F8", privacy_tier="intimate")

    open_wheel = wheel_tool(vault_path=tmp_path, privacy_tier_ceiling=TierCeiling.OPEN)
    assert open_wheel["wheel"]["F8"]["count"] == 0  # intimate excluded under OPEN
    assert open_wheel["total_classified"] == 1

    full_wheel = wheel_tool(
        vault_path=tmp_path, privacy_tier_ceiling=TierCeiling.INTIMATE
    )
    assert full_wheel["wheel"]["F8"]["count"] == 1  # admitted under INTIMATE
    assert full_wheel["total_classified"] == 2


def test_unclassified_fragments_are_counted_separately(tmp_path: Path) -> None:
    """An unclassified fragment lands in ``unclassified``, not in any F bucket."""
    _seed_dirs(tmp_path)
    _write_fragment(tmp_path, frag_id="a", primary="F1")
    _write_fragment(tmp_path, frag_id="u", primary="unclassified")

    result = wheel_tool(vault_path=tmp_path)
    assert result["unclassified"] == 1
    assert result["total_classified"] == 1
    assert sum(cell["count"] for cell in result["wheel"].values()) == 1


def test_wheel_is_audit_logged(tmp_path: Path) -> None:
    """The read is audit-logged with the tool name and consumer."""
    _seed_dirs(tmp_path)
    wheel_tool(vault_path=tmp_path, consumer="adepthood")
    last = _audit(tmp_path)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["consumer"] == "adepthood"


def test_wheel_is_read_only(tmp_path: Path) -> None:
    """Only the audit log is written; the corpus is untouched."""
    _seed_dirs(tmp_path)
    _write_fragment(tmp_path, frag_id="a", primary="F2")
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    wheel_tool(vault_path=tmp_path)
    created = {p for p in tmp_path.rglob("*") if p.is_file()} - before
    assert all("audit" in str(p) for p in created)
