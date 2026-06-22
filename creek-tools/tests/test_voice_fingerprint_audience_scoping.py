"""Voice fingerprint scoped to audience-facing documents (#633).

The fingerprint historically averaged ALL self-authored fragments — journals,
DMs, chats — weighted only by platform, so it encoded the user's *texting
average* rather than their *audience-facing voice*. These tests pin the new
behaviour: when an audience-weighting config is supplied, audience-facing
platforms (essay/substack) dominate the fingerprint over private ones
(journal), and the un-scoped default path is unchanged (backward compatible).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from creek.config import AIStyleConfig, VoiceAudienceWeightingConfig
from creek.generate.ai_style.fingerprint import build_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

# Equal-length bodies; the essay uses em-dashes, the journal uses none, so the
# em_dash_density feature isolates which fragment shaped the fingerprint.
_ESSAY_BODY = "alpha—beta—gamma—delta—epsilon zeta eta theta iota kappa"
_JOURNAL_BODY = "alpha beta gamma delta epsilon zeta eta theta iota kappa"


def _write_fragment(
    vault: Path,
    name: str,
    *,
    platform: str,
    body: str,
    privacy_tier: str = "unclassified",
    representativeness: str = "self",
) -> None:
    """Write a minimal self-authored fragment markdown file into *vault*."""
    frag_dir = vault / "01-Fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "source": {"author": "self", "platform": platform},
        "privacy_tier": privacy_tier,
        "representativeness": representativeness,
    }
    (frag_dir / name).write_text(
        "---\n" + yaml.safe_dump(meta, sort_keys=True) + "---\n\n" + body,
        encoding="utf-8",
    )


def _em_dash_rate(vault: Path, **kwargs: object) -> float:
    """Build a fingerprint and return its em_dash_density rate (0.0 if absent)."""
    fp = build_fingerprint(vault, AIStyleConfig(), **kwargs)  # type: ignore[arg-type]
    stat = fp.features.get("em_dash_density")
    return stat.rate if stat is not None else 0.0


def test_audience_facing_fragment_dominates_equal_length_journal(
    tmp_path: Path,
) -> None:
    """With audience weighting, the essay's em-dashes dominate over the journal."""
    vault = tmp_path / "vault"
    _write_fragment(vault, "essay.md", platform="essay", body=_ESSAY_BODY)
    _write_fragment(vault, "journal.md", platform="journal", body=_JOURNAL_BODY)

    scoped = _em_dash_rate(vault, audience_weighting=VoiceAudienceWeightingConfig())
    unscoped = _em_dash_rate(vault)

    # Scoping up-weights the em-dash-bearing essay and down-weights the journal,
    # so the fingerprint's em-dash rate rises strictly above the flat average.
    assert scoped > unscoped > 0.0

    # And it lands close to the essay-only rate (journal heavily down-weighted).
    essay_only = tmp_path / "essay_only"
    _write_fragment(essay_only, "essay.md", platform="essay", body=_ESSAY_BODY)
    essay_rate = _em_dash_rate(essay_only)
    assert scoped >= 0.9 * essay_rate


def test_default_path_unchanged_without_audience_weighting(tmp_path: Path) -> None:
    """Omitting audience_weighting preserves the historical flat-average path."""
    vault = tmp_path / "vault"
    _write_fragment(vault, "essay.md", platform="essay", body=_ESSAY_BODY)
    _write_fragment(vault, "journal.md", platform="journal", body=_JOURNAL_BODY)

    # Disabled weighting must equal the no-weighting default (both = flat average).
    disabled = _em_dash_rate(
        vault,
        audience_weighting=VoiceAudienceWeightingConfig(enabled=False),
    )
    default = _em_dash_rate(vault)
    assert disabled == default


def test_platform_authority_default_prefers_audience_facing() -> None:
    """The default platform-authority ranks audience-facing above private."""
    weighting = VoiceAudienceWeightingConfig()
    essay = weighting.platform_authority.get("essay", 1.0)
    journal = weighting.platform_authority.get("journal", 1.0)
    assert essay > journal
