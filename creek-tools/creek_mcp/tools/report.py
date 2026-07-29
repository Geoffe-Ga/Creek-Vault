"""``creek.report`` MCP tool — produce a vault-state report (FEAT-011).

Wraps the CLI's report dispatcher. Generating report types exposed via
MCP today: ``tags`` (the tag-garden generator), ``voice`` (the
per-register voice profiles), ``lexicon`` (the voice glossary + metaphor
index, #580), and ``decisions`` (draft Decision notes from
decision-signalling fragments, #581), ``rhetorical-patterns``
(per-register rhetorical-move notes, #582), and ``mode-profiles``
(per-mode engagement profiles, #583). ``unnamed`` and ``wavelength`` are
deferred to the CLI because they need date arithmetic the MCP shape
should not own. The wrapper writes one audit entry per invocation
including ``created_path`` for the resulting report file(s).

Read-side posture (#968): the ceiling is audited and echoed but never
converted or threaded — ``to_privacy_override`` is never called here.
Four of the six generators (``creek/generate/{tags,lexicon,decisions,
wavelength}.py``) contain no tier filtering whatsoever;
``creek/generate/voice.py`` is the only one with an ``allow_intimate``
filter. Reproduced: ``report_type="tags"`` at ``ceiling=open`` wrote an
``intimate`` fragment's tag verbatim into
``00-Creek-Meta/Tag-Garden.md`` and
``00-Creek-Meta/Processing-Log/tag-history.json``. The exposure is
write-side, not read-side: this tool returns only ``report_paths``,
never content, and no MCP tool reads an arbitrary vault file back — so
a low-ceiling caller cannot read the leaked artifact *through MCP*.
What it causes instead is above-ceiling content distilled into an
unlabelled vault file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.decisions import generate_decisions
from creek.generate.lexicon import generate_lexicon
from creek.generate.tags import TagGardenGenerator
from creek.generate.voice import VoiceProfileGenerator
from creek.generate.wavelength import ModeProfileGenerator
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.report"
_VALID_TYPES = (
    "tags",
    "voice",
    "decisions",
    "lexicon",
    "rhetorical-patterns",
    "mode-profiles",
)


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
    if report_type == "tags":
        written_paths = [
            TagGardenGenerator(vault_path=vault_path).generate_garden(),
        ]
    elif report_type == "lexicon":
        _lexicon, written_paths = generate_lexicon(vault_path)
    elif report_type == "decisions":
        written_paths = generate_decisions(vault_path)
    elif report_type == "rhetorical-patterns":
        written_paths = list(
            VoiceProfileGenerator().generate_rhetorical_patterns(vault_path),
        )
    elif report_type == "mode-profiles":
        written_paths = list(ModeProfileGenerator().generate_mode_profiles(vault_path))
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
