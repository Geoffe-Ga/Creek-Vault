"""``creek.draft`` MCP tool — generate an essay draft from a mined idea.

The wrapper accepts an injectable ``llm`` callable so tests can
exercise it without hitting a real model. In production the server
bootstrap injects the same Ollama/Anthropic adapter the CLI uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaMiner
from creek.models import Phase
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import (
    TierCeiling,
    refusal_response,
    to_privacy_override,
)

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.draft"


class _DraftLLM(Protocol):
    def __call__(self, prompt: str) -> str: ...


def draft_tool(
    *,
    vault_path: Path,
    llm: _DraftLLM,
    skills_root: Path | None = None,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    phase: str = "unclassified",
    index: int = 0,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Mine ideas, draft the *index*-th, and persist with full provenance.

    The full draft body never enters the response so tier violations
    cannot leak via MCP — callers follow up with ``creek.state.read``
    or open the saved file directly to inspect the body.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"phase": phase, "index": index},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    override = to_privacy_override(privacy_tier_ceiling)
    skills_dir = skills_root if skills_root is not None else vault_path / "creek-skills"
    seeds = IdeaMiner(privacy_override=override).mine_all(
        vault_path,
        current_phase=Phase(phase),
    )
    if not seeds:
        return {
            "status": "empty",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "reason": "no idea seeds surfaced",
        }
    if index < 0 or index >= len(seeds):
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=f"index {index} out of range (0..{len(seeds) - 1})",
        )
    idea = seeds[index]
    generator = DraftGenerator(
        llm=llm,
        skills_root=skills_dir,
        privacy_override=override,
    )
    try:
        draft = generator.generate_draft(idea, vault_path=vault_path)
    except RuntimeError as exc:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=str(exc),
        )
    saved = generator.save_draft(draft, vault_path)
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "draft_path": str(saved.relative_to(vault_path)),
        "title": draft.title,
        "idea_strategy": draft.idea_strategy,
        "source_fragments": list(draft.source_fragments),
    }
