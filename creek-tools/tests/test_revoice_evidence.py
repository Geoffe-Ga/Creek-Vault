"""Owner-voice evidence for revoice is owner-authored, weighted and tier-admitted."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import frontmatter
import pytest

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.generate.voice import load_owner_voice_samples
from creek.models import (
    Authorship,
    Confidence,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
)

FIXTURES = Path(__file__).parent / "fixtures" / "revoice"


def write_sample(
    vault: Path,
    name: str,
    body: str,
    *,
    register: VoiceRegister = VoiceRegister.CONVERSATIONAL,
    tier: PrivacyTier = PrivacyTier.OPEN,
    author: Authorship = Authorship.SELF,
    voice_weight: float = 1.0,
) -> Path:
    """Persist one register sample the way the voice collector writes them."""
    fragment = Fragment(
        id=name,
        title=name,
        source=FragmentSource(platform=SourcePlatform.JOURNAL, author=author),
        created=datetime(2026, 1, 15, 12, 0, 0),
        ingested=datetime(2026, 1, 15, 12, 0, 0),
        voice=VoiceClassification(
            voice_register=register, confidence=Confidence.SETTLED
        ),
        privacy_tier=tier,
        voice_weight=voice_weight,
    )
    target = vault / "07-Voice" / "Register-Samples" / register.value / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


def install_fixture_samples(vault: Path) -> None:
    """Install the owner samples plus the two that must never be admitted."""
    for index in (1, 2, 3):
        write_sample(
            vault,
            f"owner-{index}",
            (FIXTURES / "owner-samples" / f"owner-{index}.txt").read_text(),
        )
    write_sample(
        vault,
        "ai-draft",
        (FIXTURES / "owner-samples" / "ai-draft.txt").read_text(),
        register=VoiceRegister.ANALYTICAL,
        voice_weight=0.0,
    )
    write_sample(
        vault,
        "personal",
        (FIXTURES / "owner-samples" / "personal.txt").read_text(),
        tier=PrivacyTier.PERSONAL,
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault holding the fixture owner samples."""
    install_fixture_samples(tmp_path)
    return tmp_path


def test_only_weighted_self_authored_samples_admitted_at_the_ceiling(
    vault: Path,
) -> None:
    """An AI draft (weight 0.0) and an above-ceiling note never ground a revoice."""
    samples = load_owner_voice_samples(vault, override=PrivacyTierOverride.OPEN)
    bodies = " ".join(sample.body for sample in samples)
    assert len(samples) == 3
    assert "multifaceted cognitive capacity" not in bodies
    assert "PERSONAL_SAMPLE_CANARY" not in bodies
    assert {sample.register for sample in samples} == {"conversational"}


def test_a_higher_ceiling_admits_the_personal_sample(vault: Path) -> None:
    """Admission follows the caller's ceiling, not a fixed tier."""
    samples = load_owner_voice_samples(vault, override=PrivacyTierOverride.PERSONAL)
    assert len(samples) == 4
    assert any("PERSONAL_SAMPLE_CANARY" in sample.body for sample in samples)


@pytest.mark.parametrize(
    "author", [Authorship.AI, Authorship.OTHER, Authorship.COLLABORATIVE]
)
def test_non_owner_authorship_is_refused_even_with_weight(
    tmp_path: Path, author: Authorship
) -> None:
    """A curated sample's authorship axis is re-checked, not trusted by folder."""
    write_sample(tmp_path, "borrowed", "Someone else wrote this. " * 10, author=author)
    assert load_owner_voice_samples(tmp_path, override=PrivacyTierOverride.ALL) == []


def test_unsafe_entries_are_skipped(tmp_path: Path) -> None:
    """Summaries, symlinks, unknown registers and non-fragments never load."""
    real = write_sample(tmp_path, "real", "I write like this. " * 10)
    root = real.parent
    (root / "_Summary.md").write_text("---\ntype: fragment\n---\nsummary")
    (root / "linked.md").symlink_to(real)
    (root / "note.md").write_text("---\ntype: note\n---\nnot a fragment")
    (root / "broken.md").write_text("---\n: [unclosed\n---\n")
    stray = tmp_path / "07-Voice" / "Register-Samples" / "not-a-register" / "x.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(real.read_text())
    samples = load_owner_voice_samples(tmp_path, override=PrivacyTierOverride.ALL)
    assert [sample.body for sample in samples] == [
        "I write like this. " * 9 + "I write like this."
    ]


def test_missing_samples_folder_yields_nothing(tmp_path: Path) -> None:
    """A vault that never ran ``creek voice`` simply has no evidence."""
    assert load_owner_voice_samples(tmp_path, override=PrivacyTierOverride.ALL) == []
