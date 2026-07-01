"""``creek.journal`` MCP tool — Adepthood journal entry → fragment (#754).

Drives the REAL ledger-backed ingest (`run_ingest`) against a temp vault, so the
idempotency and edit-in-place guarantees are exercised end-to-end, not mocked:

- re-sending the same external id is a no-op (one fragment, same id);
- editing an entry (same external id, new content) rewrites in place (same id,
  no orphaned duplicate);
- tier is honored (an INTIMATE entry lands intimate; it is refused under a lower
  ceiling, never downgraded).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frontmatter

from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.journal import TOOL_NAME, journal_ingest_tool

if TYPE_CHECKING:
    from pathlib import Path

_TS = "2026-06-20T10:00:00+00:00"


def _vault(tmp_path: Path) -> Path:
    """Create the minimum vault layout the writer + ledger + audit need."""
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _fragments(vault: Path) -> list[Path]:
    """Return all fragment files under 01-Fragments."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _load(path: Path) -> frontmatter.Post:
    """Load a fragment file's frontmatter + body."""
    return frontmatter.load(path)


def _audit(vault: Path) -> list[dict[str, object]]:
    """Return parsed MCP audit-log entries."""
    log = vault / MCP_AUDIT_RELPATH
    assert log.exists()
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_entry_becomes_a_journal_fragment(tmp_path: Path) -> None:
    """An entry is ingested as one JOURNAL-platform fragment carrying its tier."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="Today I rested and the work survived.",
        external_id="adep-001",
        timestamp=_TS,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "ok"
    assert result["tool"] == TOOL_NAME
    assert result["action"] == "created"
    assert result["fragment_id"]
    frags = _fragments(vault)
    assert len(frags) == 1
    post = _load(frags[0])
    assert post.metadata["privacy_tier"] == "personal"
    assert str(post.metadata["source"]["platform"]) == "journal"


def test_resending_the_same_entry_is_idempotent(tmp_path: Path) -> None:
    """Same external id + same content → one fragment, same id, no duplicate."""
    vault = _vault(tmp_path)
    args = {
        "content": "A steady entry.",
        "external_id": "adep-002",
        "timestamp": _TS,
        "privacy_tier_ceiling": TierCeiling.PERSONAL,
    }
    first = journal_ingest_tool(vault_path=vault, **args)  # type: ignore[arg-type]
    second = journal_ingest_tool(vault_path=vault, **args)  # type: ignore[arg-type]
    assert len(_fragments(vault)) == 1  # no duplicate
    assert second["fragment_id"] == first["fragment_id"]
    assert second["action"] == "unchanged"


def test_editing_an_entry_updates_in_place(tmp_path: Path) -> None:
    """Same external id + new content → same fragment id, body updated, no orphan."""
    vault = _vault(tmp_path)
    first = journal_ingest_tool(
        vault_path=vault,
        content="The original wording.",
        external_id="adep-003",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    edited = journal_ingest_tool(
        vault_path=vault,
        content="The revised, longer wording of the same entry.",
        external_id="adep-003",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert len(_fragments(vault)) == 1  # updated in place, not orphaned
    assert edited["fragment_id"] == first["fragment_id"]
    assert edited["action"] == "updated"
    assert "revised" in _load(_fragments(vault)[0]).content


def test_intimate_entry_is_refused_under_a_lower_ceiling(tmp_path: Path) -> None:
    """An INTIMATE entry is refused (not downgraded) under an OPEN ceiling."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="something tender",
        external_id="adep-004",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )
    assert result["status"] == "refused"
    assert _fragments(vault) == []  # nothing written


def test_intimate_entry_is_stored_intimate_under_an_intimate_ceiling(
    tmp_path: Path,
) -> None:
    """An INTIMATE entry admitted under an intimate ceiling lands intimate."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="a tender private entry",
        external_id="adep-005",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.INTIMATE,
    )
    assert result["status"] == "ok"
    assert result["tier"] == "intimate"
    assert _load(_fragments(vault)[0]).metadata["privacy_tier"] == "intimate"


def test_empty_content_is_refused(tmp_path: Path) -> None:
    """A blank entry is a structured refusal, not a crash or an empty fragment."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="   ",
        external_id="adep-006",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert result["status"] == "refused"


def test_journal_success_audit_records_tier_and_fragment(tmp_path: Path) -> None:
    """The success audit records the tier and the fragment it wrote (#754 review)."""
    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="an entry",
        external_id="adep-007",
        timestamp=_TS,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="adepthood",
    )
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["consumer"] == "adepthood"
    assert last["args_summary"]["tier"] == "personal"
    assert last["created_tier"] == "personal"
    assert last["affected_fragment_ids"] == [result["fragment_id"]]


def test_refused_intimate_attempt_is_audited_with_tier(tmp_path: Path) -> None:
    """A refused INTIMATE attempt is audited (with its tier) so it is investigable."""
    vault = _vault(tmp_path)
    journal_ingest_tool(
        vault_path=vault,
        content="tender",
        external_id="adep-008",
        timestamp=_TS,
        tier="intimate",
        privacy_tier_ceiling=TierCeiling.OPEN,
        consumer="adepthood",
    )
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["args_summary"]["tier"] == "intimate"  # the attempted tier is recorded


def test_slug_colliding_external_ids_stay_distinct(tmp_path: Path) -> None:
    """Distinct external ids that slugify identically stay distinct (#754 review).

    ``"a/b"`` and ``"a-b"`` both slug to ``a-b``; the stable stem's id hash keeps
    them apart, so the idempotency key never collides.
    """
    vault = _vault(tmp_path)
    first = journal_ingest_tool(
        vault_path=vault,
        content="entry A",
        external_id="a/b",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    second = journal_ingest_tool(
        vault_path=vault,
        content="entry B",
        external_id="a-b",
        timestamp=_TS,
        privacy_tier_ceiling=TierCeiling.PERSONAL,
    )
    assert first["fragment_id"] != second["fragment_id"]
    assert len(_fragments(vault)) == 2


def test_ingest_failure_is_refused_and_audited(tmp_path: Path) -> None:
    """A failure inside run_ingest refuses AND leaves an audit trace (#754 review)."""
    from creek.ingest.pipeline import IngestRunResult

    def _failing_runner(**_kwargs: object) -> IngestRunResult:
        return IngestRunResult(
            written=0,
            errors=["boom"],
            discovered=1,
            created=0,
            updated=0,
            unchanged=0,
            tombed=0,
            skipped=0,
        )

    vault = _vault(tmp_path)
    result = journal_ingest_tool(
        vault_path=vault,
        content="an entry",
        external_id="adep-009",
        timestamp=_TS,
        tier="personal",
        privacy_tier_ceiling=TierCeiling.PERSONAL,
        consumer="adepthood",
        run=_failing_runner,
    )
    assert result["status"] == "refused"
    last = _audit(vault)[-1]
    assert last["tool"] == TOOL_NAME
    assert last["args_summary"]["tier"] == "personal"
    assert last["created_tier"] == "personal"
