"""Audience-weighted voice authority for the voice pipeline.

Public-facing (``OPEN``) fragments carry more authority over the voice proxy
than ``PERSONAL`` ones; ``INTIMATE`` stays excluded; and the previously-unused
``representativeness`` field now shapes ranking. The multiplier composes with
the existing ``voice_weight`` gate and flows through exemplar ranking so the
patterns of public work dominate generated profiles.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.config import VoiceAudienceWeightingConfig
from creek.generate.voice import (
    Exemplar,
    VoiceExemplarCollector,
    VoiceProfileGenerator,
    audience_authority,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


def _fragment(
    *,
    frag_id: str,
    privacy: PrivacyTier = PrivacyTier.PERSONAL,
    representativeness: str = "self",
    confidence: Confidence = Confidence.SETTLED,
    register: VoiceRegister = VoiceRegister.ANALYTICAL,
) -> Fragment:
    """Build a fully-classified Fragment with the given audience attributes."""
    return Fragment(
        id=frag_id,
        title=frag_id,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=datetime(2026, 1, 15, 12, 0, 0),
        ingested=datetime(2026, 1, 15, 12, 0, 0),
        frequency=FrequencyClassification(primary=Frequency.F5),
        wavelength=WavelengthClassification(phase=Phase.RISING, mode=Mode.EXPRESS),
        voice=VoiceClassification(voice_register=register, confidence=confidence),
        privacy_tier=privacy,
        representativeness=representativeness,  # type: ignore[arg-type]
    )


def _write(vault: Path, fragment: Fragment, *, body: str) -> None:
    """Persist *fragment* with *body* under ``01-Fragments/Writing``."""
    target = vault / "01-Fragments" / "Writing" / f"{fragment.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    target.write_text(frontmatter.dumps(post), encoding="utf-8")


# --- audience_authority helper ----------------------------------------------


def test_open_outranks_personal_authority() -> None:
    """An OPEN fragment carries more authority than a PERSONAL one."""
    weighting = VoiceAudienceWeightingConfig()
    open_frag = _fragment(frag_id="o", privacy=PrivacyTier.OPEN)
    personal = _fragment(frag_id="p", privacy=PrivacyTier.PERSONAL)
    assert audience_authority(open_frag, weighting) > audience_authority(
        personal,
        weighting,
    )


def test_intimate_authority_is_zero() -> None:
    """INTIMATE carries zero authority (excluded from the proxy)."""
    weighting = VoiceAudienceWeightingConfig()
    intimate = _fragment(frag_id="i", privacy=PrivacyTier.INTIMATE)
    assert audience_authority(intimate, weighting) == 0.0


def test_representativeness_orders_authority() -> None:
    """self > endorsed > aspirational > reference for equal privacy tier."""
    weighting = VoiceAudienceWeightingConfig()
    authorities = [
        audience_authority(
            _fragment(frag_id=rep, privacy=PrivacyTier.OPEN, representativeness=rep),
            weighting,
        )
        for rep in ("self", "endorsed", "aspirational", "reference")
    ]
    assert authorities == sorted(authorities, reverse=True)
    assert len(set(authorities)) == 4


def test_disabled_weighting_is_uniform() -> None:
    """When weighting is disabled every fragment has authority 1.0."""
    weighting = VoiceAudienceWeightingConfig(enabled=False)
    open_frag = _fragment(frag_id="o", privacy=PrivacyTier.OPEN)
    intimate = _fragment(frag_id="i", privacy=PrivacyTier.INTIMATE)
    assert audience_authority(open_frag, weighting) == 1.0
    assert audience_authority(intimate, weighting) == 1.0


# --- collector ranking ------------------------------------------------------


def test_open_outranks_personal_in_exemplar_ranking() -> None:
    """With equal base score, the OPEN fragment ranks above the PERSONAL one."""
    collector = VoiceExemplarCollector()
    personal = _fragment(frag_id="frag-personal", privacy=PrivacyTier.PERSONAL)
    open_frag = _fragment(frag_id="frag-open", privacy=PrivacyTier.OPEN)
    ranked = collector.rank_exemplars([personal, open_frag])
    assert [f.id for f in ranked] == ["frag-open", "frag-personal"]


def test_self_outranks_aspirational_in_ranking() -> None:
    """For equal privacy tier, self-representative ranks above aspirational."""
    collector = VoiceExemplarCollector()
    aspirational = _fragment(
        frag_id="frag-asp",
        privacy=PrivacyTier.OPEN,
        representativeness="aspirational",
    )
    own = _fragment(
        frag_id="frag-self",
        privacy=PrivacyTier.OPEN,
        representativeness="self",
    )
    ranked = collector.rank_exemplars([aspirational, own])
    assert [f.id for f in ranked] == ["frag-self", "frag-asp"]


def test_uniform_tier_preserves_confidence_ordering() -> None:
    """Backward-compat: same tier/representativeness keeps the old ranking."""
    collector = VoiceExemplarCollector()
    settled = _fragment(frag_id="frag-settled", confidence=Confidence.SETTLED)
    conviction = _fragment(frag_id="frag-conv", confidence=Confidence.CONVICTION)
    ranked = collector.rank_exemplars([settled, conviction])
    # Conviction still outranks settled when audience authority is uniform.
    assert [f.id for f in ranked] == ["frag-conv", "frag-settled"]


# --- end-to-end profile generation ------------------------------------------


def test_generated_profile_favours_open_over_personal(tmp_path: Path) -> None:
    """With a single exemplar slot, the OPEN fragment wins the profile."""
    vault = tmp_path / "vault"
    open_frag = _fragment(frag_id="frag-open", privacy=PrivacyTier.OPEN)
    personal = _fragment(frag_id="frag-personal", privacy=PrivacyTier.PERSONAL)
    _write(vault, open_frag, body="OPEN_SIGNATURE " + "word " * 300)
    _write(vault, personal, body="PERSONAL_SIGNATURE " + "word " * 300)

    generator = VoiceProfileGenerator(max_exemplars=1, min_exemplars=1)
    written = generator.generate_all_profiles(vault)

    # Both fragments share the ANALYTICAL register, but only one exemplar slot
    # exists — the OPEN fragment must win it, so its signature appears in the
    # generated profile note(s) and the PERSONAL one does not.
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in written)
    assert "OPEN_SIGNATURE" in rendered
    assert "PERSONAL_SIGNATURE" not in rendered


def test_generator_rank_exemplars_weights_audience() -> None:
    """The generator's exemplar ranking applies the audience multiplier."""
    generator = VoiceProfileGenerator(max_exemplars=2, min_exemplars=1)
    personal = Exemplar(
        fragment=_fragment(frag_id="frag-personal", privacy=PrivacyTier.PERSONAL),
        body="word " * 300,
    )
    open_ex = Exemplar(
        fragment=_fragment(frag_id="frag-open", privacy=PrivacyTier.OPEN),
        body="word " * 300,
    )
    ranked = generator._rank_exemplars([personal, open_ex])
    assert [e.fragment.id for e in ranked] == ["frag-open", "frag-personal"]


def test_intimate_excluded_from_collection(tmp_path: Path) -> None:
    """INTIMATE fragments are still gated out of collection by default."""
    vault = tmp_path / "vault"
    intimate = _fragment(frag_id="frag-intimate", privacy=PrivacyTier.INTIMATE)
    _write(vault, intimate, body="word " * 300)

    collector = VoiceExemplarCollector()
    buckets = collector.collect_exemplars(vault)

    assert all(intimate.id not in [f.id for f in frags] for frags in buckets.values())


def test_audience_weighting_config_defaults() -> None:
    """The config exposes graduated, documented default multipliers."""
    cfg = VoiceAudienceWeightingConfig()
    assert cfg.enabled is True
    assert cfg.privacy_tier_authority["open"] > cfg.privacy_tier_authority["personal"]
    assert cfg.privacy_tier_authority["intimate"] == 0.0
    assert (
        cfg.representativeness_authority["self"]
        > cfg.representativeness_authority["reference"]
    )


@pytest.mark.parametrize("rep", ["self", "endorsed", "aspirational", "reference"])
def test_authority_is_positive_for_open_representativeness(rep: str) -> None:
    """Every representativeness keeps OPEN authority strictly positive."""
    weighting = VoiceAudienceWeightingConfig()
    frag = _fragment(frag_id=rep, privacy=PrivacyTier.OPEN, representativeness=rep)
    assert audience_authority(frag, weighting) > 0.0
