"""``creek.report`` MCP tool — produce a vault-state report (FEAT-011).

Wraps the CLI's report dispatcher. Generating report types exposed via
MCP today: ``tags`` (the tag-garden generator), ``voice`` (the
per-register voice profiles), and ``lexicon`` (the voice glossary +
metaphor index, #580). ``decisions`` is wired as a skeleton (#579) —
routable but not yet generating; it returns a typed "would generate"
note and writes nothing. ``unnamed`` and ``wavelength`` are deferred to
the CLI because they need date arithmetic the MCP shape should not own.
The wrapper writes one audit entry per invocation including
``created_path`` for the resulting report file(s).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.lexicon import generate_lexicon
from creek.generate.tags import TagGardenGenerator
from creek.generate.voice import VoiceProfileGenerator
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.report"
_VALID_TYPES = ("tags", "voice", "decisions", "lexicon")

#: Skeleton report types (#579): routing is wired, generation is not. Each maps
#: to the human-readable target the follow-up will persist. Stubs write nothing.
#: ``lexicon`` graduated to real generation in #580.
_STUB_TYPES = {
    "decisions": "decision notes at 08-Decisions/",
}


def report_tool(
    *,
    vault_path: Path,
    report_type: str = "tags",
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Generate the requested report and return its written path(s).

    Reports iterate vault content internally rather than operating on a
    caller-supplied fragment list, so ``affected_fragment_ids`` is the
    empty list and ``created_path`` carries the rendered file location.
    """
    if report_type not in _VALID_TYPES:
        return refusal_response(
            tool=TOOL_NAME,
            ceiling=privacy_tier_ceiling,
            reason=(
                f"unsupported report_type {report_type!r}; "
                f"available via MCP: {', '.join(_VALID_TYPES)}"
            ),
        )
    if report_type in _STUB_TYPES:
        # Skeleton tracer (#579): the type is routable but its generator is not
        # yet wired. Audit the invocation (nothing written) and report intent.
        MCPAuditLog(vault_path).append(
            tool=TOOL_NAME,
            args={"report_type": report_type},
            tier_ceiling=privacy_tier_ceiling,
            consumer=consumer,
            created_path=None,
            created_tier=None,
            affected_fragment_ids=[],
        )
        return {
            "status": "ok",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "report_type": report_type,
            "report_paths": [],
            "note": (
                f"would generate: {_STUB_TYPES[report_type]} "
                "(not yet wired — see follow-up)"
            ),
        }
    if report_type == "tags":
        written_paths = [
            TagGardenGenerator(vault_path=vault_path).generate_garden(),
        ]
    elif report_type == "lexicon":
        _lexicon, written_paths = generate_lexicon(vault_path)
    else:
        written_paths = list(
            VoiceProfileGenerator().generate_all_profiles(vault_path),
        )
    relative_paths = [str(p.relative_to(vault_path)) for p in written_paths]
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"report_type": report_type},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=relative_paths[0] if relative_paths else None,
        created_tier=None,
        affected_fragment_ids=[],
    )
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_type": report_type,
        "report_paths": relative_paths,
    }
