"""``creek.wheel`` MCP tool — per-frequency balance read for the Map (#752).

Exposes a *balance-shaped* read of the classified corpus: how full or thin each
APTITUDE frequency is, so Adepthood's Map can render the wheel of wholeness
rather than a linear ladder. Every frequency F1-F10 is always present (thin ones
at zero) and each carries a normalised ``share`` so the client can draw balance,
not ranking.

Read-only and LLM-free. The ``privacy_tier_ceiling`` is enforced for real (unlike
``creek.report``): above-ceiling fragments are excluded from the tally via
``tier_within_override``. An empty (new-user) vault yields a complete all-zero
wheel with no special-casing — :func:`iter_vault_fragments` returns ``[]`` for a
missing directory.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from creek.classify.privacy_filter import tier_within_override
from creek.generate.indexes import CANONICAL_FREQUENCY_NAMES
from creek.models import Frequency
from creek.vault.reader import iter_vault_fragments
from creek_mcp.audit import MCPAuditLog
from creek_mcp.tier_ceiling import TierCeiling, to_privacy_override

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.privacy_filter import PrivacyTierOverride

TOOL_NAME = "creek.wheel"
_FRAGMENTS_SUBDIR = "01-Fragments"


def _frequency_counts(
    vault_path: Path, override: PrivacyTierOverride
) -> Counter[Frequency]:
    """Tally fragments by primary frequency, excluding above-ceiling content.

    ``Fragment.frequency.primary`` is a bare string at runtime
    (``use_enum_values=True``), so it is coerced back to :class:`Frequency`;
    anything unrecognised falls into :attr:`Frequency.UNCLASSIFIED` (fail closed
    to "not a real frequency" rather than guessing a bucket).
    """
    counts: Counter[Frequency] = Counter()
    for _path, fragment, _body, _meta in iter_vault_fragments(
        vault_path / _FRAGMENTS_SUBDIR
    ):
        if not tier_within_override(fragment.privacy_tier, override):
            continue
        try:
            freq = Frequency(fragment.frequency.primary)
        except ValueError:
            freq = Frequency.UNCLASSIFIED
        counts[freq] += 1
    return counts


def wheel_tool(
    *,
    vault_path: Path,
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Return a per-frequency balance map over the (ceiling-filtered) corpus.

    Args:
        vault_path: Vault root.
        privacy_tier_ceiling: Admission ceiling — fragments above it are excluded
            from the tally.
        consumer: Free-form consumer id for the audit log.

    Returns:
        ``{status, tool, tier_ceiling, total_classified, unclassified, wheel}``
        where ``wheel`` is an ordered ``{F1..F10: {name, count, share}}`` map.
        ``share`` is the fraction of *classified* fragments at that frequency
        (``0.0`` for every frequency when the corpus is empty).
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
        created_path=None,
        created_tier=None,
        affected_fragment_ids=[],
    )

    override = to_privacy_override(privacy_tier_ceiling)
    counts = _frequency_counts(vault_path, override)
    classified = sum(counts.get(code, 0) for code in CANONICAL_FREQUENCY_NAMES)

    wheel = {
        code.value: {
            "name": CANONICAL_FREQUENCY_NAMES[code],
            "count": counts.get(code, 0),
            "share": (counts.get(code, 0) / classified) if classified else 0.0,
        }
        for code in CANONICAL_FREQUENCY_NAMES
    }
    return {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "total_classified": classified,
        "unclassified": counts.get(Frequency.UNCLASSIFIED, 0),
        "wheel": wheel,
    }
