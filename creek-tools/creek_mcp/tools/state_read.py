"""``creek.state.read`` MCP tool — cheap read of ``State/latest.md``.

This is the FEAT-010 cheap path: no re-render, no walking the vault,
just hand back the most recent audit-report bytes. ``creek.state.render``
in :mod:`creek_mcp.tools.state` regenerates the report; ``read`` exists
so CrawDad's Discord turn-around stays fast.

**Posture: GATED, and it REFUSES rather than excludes (#969).** The asymmetry
with ``creek.state.render`` is deliberate. Render names no target and is a
corpus walk, so it filters its inputs; read addresses **one atomic cached
artifact**, and there is nothing to partially admit — you cannot exclude half a
rendered markdown document without re-rendering it, and re-rendering is what
``render`` is. This is the same rule #1068 applied to ``compile`` and
``reflect`` (whose targets are caller-*named*), read one level up: read's
target is caller-*addressed* and singular, which is the property that matters.

The comparison is against the artifact's own ``privacy_tier`` stamp —
the highest tier the render admitted — and never against the
``tier_ceiling`` the render ran under. Comparing the latter would refuse a
broad render over a narrow corpus for no reason: an ``all``-ceiling report
over an all-``open`` vault contains nothing above ``open``.

**A pre-#969 ``latest.md`` is unstamped, and therefore refused below
``ceiling=intimate``.** That is accurate rather than merely cautious: every
report written before this change was rendered completely unfiltered, i.e. at
the equivalent of ``--include-tier all``, so ``INTIMATE`` states a fact about
those bytes. Recovery is one command and loses nothing — ``creek.state.render``
re-renders and re-stamps at the caller's ceiling, or ``creek state
--include-tier open`` does the same from the CLI — and ``ceiling=all`` admits
every stamp, including the unstamped one, so no report is ever permanently
unreachable. Note the cache-thrash consequence recorded on ``render``:
``latest.md`` is a single slot, so re-rendering narrower replaces the broader
report for every other caller too.

The refusal is the canonical four-key payload carrying
:data:`~creek_mcp.read_gate.GENERIC_ABOVE_CEILING_REASON`, and it is
byte-identical for an above-ceiling stamp and for an unstamped legacy report.
One reason for both causes is the point: a distinguishable "this predates the
stamp" reason would itself be an oracle for whether the vault holds
above-ceiling content.

A **missing** report stays ``status="empty"`` rather than refused. There is no
content for a ceiling to be above, and answering ``refused`` there would be a
vault-emptiness oracle in the other direction — as well as making a first run
of CrawDad or ``/creek`` look like a permissions failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creek.generate.state_tiers import stamped_content_tier
from creek_mcp.audit import MCPAuditLog
from creek_mcp.read_gate import refuse_above_ceiling
from creek_mcp.tier_ceiling import TierCeiling

_STATE_LATEST_RELPATH = Path("00-Creek-Meta/State/latest.md")
TOOL_NAME = "creek.state.read"


def state_read_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Return the latest audit-report bytes, refusing above the ceiling.

    The ordering is the one :mod:`creek_mcp.read_gate` sets out, and each step
    is load-bearing:

    1. audit-log the attempt first and unconditionally, recording the declared
       ceiling and consumer but never the outcome, so the trail cannot answer
       "did consumer X read an above-ceiling report?" in either direction;
    2. resolve the target and answer *not found* before anything else touches
       it;
    3. read the artifact's own tier stamp and run the ceiling gate;
    4. only then hand back content.

    Args:
        vault_path: Root of the Obsidian vault.
        privacy_tier_ceiling: The caller's declared ceiling, compared against
            the artifact's stamped ``privacy_tier``.
        consumer: Identifier recorded in the MCP audit log.

    Returns:
        ``status="ok"`` with the artifact's bytes when the stamp is within the
        ceiling; ``status="empty"`` when no report has been rendered yet; and
        otherwise the canonical four-key refusal, which carries no ``content``
        key at all and never names the stamped tier.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    report_path = vault_path / _STATE_LATEST_RELPATH
    if not report_path.exists():
        return {
            "status": "empty",
            "tool": TOOL_NAME,
            "tier_ceiling": privacy_tier_ceiling.value,
            "report_path": str(_STATE_LATEST_RELPATH),
            "content": "",
        }
    content = report_path.read_text(encoding="utf-8")
    stamped = stamped_content_tier(content)
    if (
        refusal := refuse_above_ceiling(
            tool=TOOL_NAME,
            content_tier=stamped,
            ceiling=privacy_tier_ceiling,
        )
    ) is not None:
        return refusal
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "report_path": str(_STATE_LATEST_RELPATH),
        "content": content,
    }
