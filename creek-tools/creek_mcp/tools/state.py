"""``creek.state.render`` MCP tool — re-render the audit report.

Mirrors ``creek state`` from the CLI. Re-rendering walks the vault, so
callers should prefer :func:`creek_mcp.tools.state_read.state_read_tool`
when they only need the most recent rendered output.

**Posture: GATED, and it EXCLUDES rather than refuses (#969).** The tool names
no target — it is a corpus walk, like ``report`` / ``wheel`` / ``mine`` — so
refusing it would make the audit report unreachable, which is #968's explicit
anti-goal. The ceiling is converted with
:func:`~creek_mcp.tier_ceiling.to_privacy_override` and threaded into
:class:`~creek.generate.state.StateReportGenerator`, which admits ten of its
eleven sections against it and then stamps the written artifact with the
highest tier it actually admitted. The eleventh, ``## Pre-LLM yield``, is
ungated on purpose: it renders the last line of ``run-summary.jsonl`` — a run
id, a timestamp and four integers describing one pipeline run — which names no
fragment, so there is nothing in it to filter by.

The gap this closed was **write-side**, which is why it survived a response-
level sweep: the envelope happens to echo ``content`` today, but the durable
evidence is the bytes under ``00-Creek-Meta/State`` — the file
``creek.state.read``, ``creek state-budget``, a git commit and the operator's
editor all serve. Three leaks were reproduced there: an ``intimate``
fragment's slugified title inside an absolute orphan path, a
``10-Liminal/Unnamed`` note's file stem, and an eddy title derived from an
above-ceiling member fragment.

Consequences worth knowing before calling this:

* ``10-Liminal/Paradoxes`` notes are ``type: paradox`` and carry no
  ``privacy_tier`` field in their model at all, so they fail closed to
  ``intimate`` and vanish from the Liminal Watch below ``ceiling=intimate``.
  The same is true of any hand-written note missing the key.
* The lint summary is an untierable verbatim copy of a Processing-Log
  artifact, so it is admitted whole or not at all — only at
  ``ceiling=intimate`` or broader.
* Suggested questions are dropped entirely below ``ceiling=personal``: the
  miner's tier filter *summarises* a personal fragment as
  ``[Personal-tier summary: <title>]`` rather than dropping it, so a personal
  title could otherwise ride out inside a prompt. At ``personal`` and above,
  the generator narrows the miner's corpus before handing it over — the
  mining loaders gate fragments but not ``02-Threads`` / ``03-Eddies``, so an
  above-ceiling thread title would otherwise reach a prompt.
* An eddy or thread with no member fragments has no tier evidence, so it too
  is admitted only at ``ceiling=intimate`` or broader.

**Cache thrash, stated on the tin.** ``latest.md`` is a single slot shared
across ceilings, kept single deliberately — per-ceiling filenames would
multiply artifacts in the operator's vault and break ``latest.md`` as the
documented session-start context. So a render at this tool's *default*
``ceiling=open`` replaces a richer ``ceiling=all`` report for everybody,
including later CLI readers. The ISO-week archive is untouched, and the
broader caller recovers by re-rendering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.generate.state import StateReportGenerator
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

TOOL_NAME = "creek.state.render"


def state_render_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Re-render the audit report and return the written file path.

    The rendered file lives under ``00-Creek-Meta/State/<iso-week>.md``;
    ``latest.md`` is refreshed by the underlying generator and carries the
    same tier stamp, since it is the file most readers actually open.

    The audit entry is written first and unconditionally, recording the
    declared ceiling and the consumer but no vault content — the ordering
    discipline :mod:`creek_mcp.read_gate` sets out.

    Args:
        vault_path: Root of the Obsidian vault.
        privacy_tier_ceiling: The caller's declared ceiling. Threaded into
            every gated section of the report; see the module docstring for
            what each ceiling excludes.
        consumer: Identifier recorded in the MCP audit log.

    Returns:
        ``status`` / ``tool`` / ``tier_ceiling`` / ``report_path`` / ``content``,
        where ``content`` is the stamped artifact exactly as written.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    override = to_privacy_override(privacy_tier_ceiling)
    written = StateReportGenerator(vault_path=vault_path, override=override).write()
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_path": str(written.relative_to(vault_path)),
        "content": written.read_text(encoding="utf-8"),
    }
