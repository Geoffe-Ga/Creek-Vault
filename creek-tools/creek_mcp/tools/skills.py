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
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override

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

    The caller's ceiling is converted with ``to_privacy_override`` and
    threaded into ``SkillTreeGenerator``, which applies it as a **hard rank
    cutoff**: an above-ceiling fragment is omitted from exemplar harvesting
    outright rather than summarised, so no above-ceiling *fragment* body,
    title or id reaches the untiered ``<vault>/creek-skills`` tree. Omission
    is the point — a ``"[Personal-tier summary: <title>]"`` stub written into
    ``## Exemplar Passages`` would be a fabricated voice exemplar carrying
    the very title it claims to protect.

    That claim covers fragments and stops there. **Thread and eddy skills are
    not gated.** ``Thread`` and ``Eddy`` carry no ``privacy_tier`` field, so
    the generator's ``_collect_typed`` walk has nothing to rank them by and
    takes no override, while their titles, ids, descriptions and fragment
    counts are all derived from their member fragments. An eddy whose members
    sit above the ceiling still reaches ``creek-skills/eddies/`` under a title
    built from their vocabulary — and because that title, slugified, is the
    filename, it also reaches the ``skill_paths`` this function returns, with
    ``skill_count`` moving as threads and eddies clear their member
    thresholds. Closing that is a product decision about which files the tree
    emits at all, tracked separately in #1284.

    ``intimate`` needs more than a ceiling. It additionally requires the
    Python-API ``allow_intimate`` consent opt-in, which this tool **never**
    passes, so intimate exemplars are unreachable through MCP at every
    ceiling — ``all`` included.

    Consequence to know: because an untiered fragment ranks with
    ``personal`` (#876), a vault that has never been through
    ``creek classify`` produces, at the default ``ceiling=open``, a complete
    tree whose ``## Exemplar Passages`` sections all carry the "no
    qualifying exemplars" placeholder. That is the gate working, not an
    empty vault; a broader ceiling recovers them.
    """
    output = vault_path / _SKILLS_RELDIR
    written = SkillTreeGenerator(
        override=to_privacy_override(privacy_tier_ceiling),
    ).generate_all_skills(vault_path, output)
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
