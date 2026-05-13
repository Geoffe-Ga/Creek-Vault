"""``creek.skills.refresh`` MCP tool — regenerate the voice-skill tree.

Per FEAT-011: skill refresh is read-only as far as user content goes —
it regenerates the voice-skill tree from existing fragments — but is
treated as a write tool because it produces new files. The wrapper
records the skill-tree directory in the audit entry's ``created_path``
field; ``affected_fragment_ids`` stays empty because the generator does
not surface per-fragment touches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.skills import SkillTreeGenerator
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.skills.refresh"
_SKILLS_RELDIR = "creek-skills"


def skills_refresh_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Regenerate the voice-skill tree and return the written paths.

    The generator already excludes ``intimate`` exemplars so the
    operation is safe at any ceiling. The skill tree always lands under
    ``<vault>/creek-skills``; the MCP surface does not expose an
    override (the CLI flag is the right tool for that).
    """
    output = vault_path / _SKILLS_RELDIR
    written = SkillTreeGenerator().generate_all_skills(vault_path, output)
    relative = [str(p.relative_to(vault_path)) for p in written]
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=_SKILLS_RELDIR,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "skill_count": len(written),
        "skill_paths": relative,
    }
