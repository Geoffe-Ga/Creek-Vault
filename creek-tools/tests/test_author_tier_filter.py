"""Writing Desk tier-filters retrieval against the privacy override (#660).

#463 shipped the real Graph/Retrieval agents but not the tier-filtering its own
comments promised. These tests pin that a fragment whose ``privacy_tier``
exceeds the run's override is **absent** from the evidence, while one at/below it
is present — and that the override threads from the conductor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author.agents import GraphSpecialist
from creek.author.conductor import build_default_conductor
from creek.classify.privacy_filter import PrivacyTierOverride

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author.models import EvidenceBundle


def _seed(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    tier: str | None = None,
    body: str = "body",
) -> None:
    """Write a minimal fragment file, optionally with a ``privacy_tier``."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    tier_line = f"privacy_tier: {tier}\n" if tier else ""
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"source:\n  platform: journal\n  author: self\n{tier_line}---\n{body}\n",
        encoding="utf-8",
    )


def _ids(bundle: EvidenceBundle) -> set[str]:
    return {f for c in bundle.claims for f in c.source_fragments}


def test_graph_specialist_excludes_above_override(tmp_path: Path) -> None:
    """Default (OPEN) drops an INTIMATE fragment; ALL keeps it.

    The open fragment links to the intimate one, so the walk *would* reach it —
    the tier filter is what keeps it out of the evidence.
    """
    _seed(
        tmp_path,
        "frag-open",
        "Open note about q",
        tier="open",
        body="[[frag-int]] body",
    )
    _seed(tmp_path, "frag-int", "Intimate note about q", tier="intimate")

    default = GraphSpecialist().gather("q", tmp_path)
    assert "frag-int" not in _ids(default)
    assert "frag-open" in _ids(default)

    everything = GraphSpecialist().gather(
        "q",
        tmp_path,
        override=PrivacyTierOverride.ALL,
    )
    assert "frag-int" in _ids(everything)


def test_personal_excluded_under_open_admitted_under_personal(
    tmp_path: Path,
) -> None:
    """A PERSONAL fragment is gated by the override rank, not summarised."""
    _seed(
        tmp_path,
        "frag-open",
        "Open about q",
        tier="open",
        body="[[frag-pers]] body",
    )
    _seed(tmp_path, "frag-pers", "Personal about q", tier="personal")

    under_open = GraphSpecialist().gather("q", tmp_path)
    assert "frag-pers" not in _ids(under_open)

    under_personal = GraphSpecialist().gather(
        "q",
        tmp_path,
        override=PrivacyTierOverride.PERSONAL,
    )
    assert "frag-pers" in _ids(under_personal)


def test_untiered_excluded_under_open_admitted_under_personal(
    tmp_path: Path,
) -> None:
    """A fragment with no ``privacy_tier`` is gated like PERSONAL (#876).

    Deliberate behaviour change. ``_load_corpus`` admits evidence through
    :func:`creek.classify.privacy_filter.tier_within_override`, which used
    to rank ``unclassified`` alongside ``open`` — so before ``creek
    classify`` grew a privacy caller, *every* fragment in the vault was
    untiered and the Writing Desk drew its evidence from the whole private
    corpus at the default ceiling.

    Three fragments at three distinct tiers, asserted as a set of admitted
    ids, so the test cannot pass by admitting or dropping everything.
    """
    _seed(
        tmp_path,
        "frag-open",
        "Open about q",
        tier="open",
        body="[[frag-untiered]] [[frag-int]] body",
    )
    _seed(tmp_path, "frag-untiered", "Untiered about q")  # no privacy_tier key
    _seed(tmp_path, "frag-int", "Intimate about q", tier="intimate")

    under_open = _ids(GraphSpecialist().gather("q", tmp_path))
    assert "frag-open" in under_open
    assert "frag-untiered" not in under_open
    assert "frag-int" not in under_open

    under_personal = _ids(
        GraphSpecialist().gather(
            "q",
            tmp_path,
            override=PrivacyTierOverride.PERSONAL,
        )
    )
    assert "frag-untiered" in under_personal
    assert "frag-int" not in under_personal  # personal ceiling still drops intimate


def test_gather_evidence_threads_override(tmp_path: Path) -> None:
    """The conductor passes the override down to the specialists."""
    _seed(tmp_path, "frag-open", "Open about q", tier="open")
    _seed(tmp_path, "frag-int", "Intimate about q", tier="intimate")

    conductor = build_default_conductor(max_rounds=1)
    default = conductor.gather_evidence("q", tmp_path)
    assert "frag-int" not in _ids(default)

    everything = conductor.gather_evidence(
        "q",
        tmp_path,
        override=PrivacyTierOverride.ALL,
    )
    assert "frag-int" in _ids(everything)


def test_conductor_routes_voice_by_content_tier(tmp_path: Path) -> None:
    """The voice-client factory receives the evidence's content tier (#661).

    With an INTIMATE fragment admitted (override=ALL), the factory is called
    with INTIMATE so the chokepoint can redirect the voice call to local.
    """
    from creek.author.conductor import build_default_conductor
    from creek.models import PrivacyTier

    _seed(tmp_path, "frag-int", "Intimate note about q", tier="intimate")

    seen: dict[str, object] = {}

    def _factory(tier: object) -> None:
        seen["tier"] = tier
        return None  # deterministic render; we only assert the tier threaded

    build_default_conductor(max_rounds=1, voice_client_factory=_factory).run(
        medium="research",
        query="q",
        vault=tmp_path,
        override=PrivacyTierOverride.ALL,
    )
    assert seen["tier"] == PrivacyTier.INTIMATE


def test_content_tier_open_when_no_sensitive_evidence(tmp_path: Path) -> None:
    """With only OPEN evidence, the factory gets a non-INTIMATE tier."""
    from creek.author.conductor import build_default_conductor
    from creek.models import PrivacyTier

    _seed(tmp_path, "frag-open", "Open note about q", tier="open")

    seen: dict[str, object] = {}

    def _factory(tier: object) -> None:
        seen["tier"] = tier
        return None

    build_default_conductor(max_rounds=1, voice_client_factory=_factory).run(
        medium="research",
        query="q",
        vault=tmp_path,
    )
    assert seen["tier"] != PrivacyTier.INTIMATE
