"""Letters and reflections speak in the owner's voice only from the owner's writing.

:func:`creek.author.reply_voice.reply_style` is the one place the ``/v1``
letter and reflection prompts get their style from (#2126). These tests pin
what may and may not supply it:

* owner-authored, source-free style skills;
* the owner's register samples, admitted at the caller's ceiling, reduced to a
  numeric profile;
* the persisted voice fingerprint, when the caller's ceiling admits every tier
  it is measured from;

and never canonical signatures (the same for every vault), borrowed
``11-Other-Authors`` material, or the AI's own earlier letters and reflections.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Final

import frontmatter
import pytest

from creek.author.reply_voice import REPLY_REGISTER, reply_style
from creek.author.voice import owner_style_sections
from creek.classify.privacy_filter import PrivacyTierOverride
from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import build_fingerprint, save_fingerprint
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.skills import render_voice_signature
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
    VoiceRegister,
)
from creek.reply_voice import HIGHER_SELF_STYLE, ReplyVoice
from creek.voice_drafts import VoiceDraftDocument, create_voice_draft_if_absent
from tests.test_revoice_evidence import FIXTURES, install_fixture_samples, write_sample

if TYPE_CHECKING:
    from pathlib import Path

OWNER_STYLE: Final[str] = (
    "Write in lowercase only.\nNever use em-dashes.\nKeep every sentence short."
)
"""An owner-authored style the issue's acceptance test names."""

_LETTER_PROSE: Final[str] = (
    "zz-earlier-letter-zz Dear one — you have always known how to wait, "
    "and the river you walked toward is a practice you already keep."
)
"""An earlier AI-written letter. It must never become the owner's voice."""

_FINGERPRINT_FRAGMENTS: Final[int] = 12
"""Comfortably above :attr:`AIStyleConfig.min_fingerprint_fragments`."""


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A bare vault: no skills, samples or fingerprint unless a test adds them."""
    (tmp_path / "00-Creek-Meta").mkdir()
    return tmp_path


def _owner_skill(vault: Path, body: str = OWNER_STYLE) -> None:
    """Install an owner-authored (non-native) voice-core skill."""
    path = vault / "creek-skills" / "meta" / "voice-core.SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _native_skill(vault: Path) -> None:
    """Install a generated voice-core cache, which is no one's voice."""
    path = vault / "creek-skills" / "meta" / "voice-core.SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content="zz-native-exemplar-zz private passage",
        type="skill",
        category="meta",
        key="voice-core",
        variant="skill",
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _persist_fingerprint(vault: Path, fragment_count: int) -> None:
    """Save a fingerprint as ``creek report --type fingerprint`` would."""
    fingerprint = VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=0.1, support=fragment_count)},
        fragment_count=fragment_count,
    )
    save_fingerprint(fingerprint, vault, AIStyleConfig())


def _write_fragment(
    vault: Path,
    relative: str,
    body: str,
    *,
    author: Authorship,
    voice_weight: float = 1.0,
    author_slug: str | None = None,
) -> None:
    """Write one fragment the fingerprint walk could visit."""
    fragment = Fragment(
        id=relative.replace("/", "-"),
        title=relative,
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL, author=author, author_slug=author_slug
        ),
        created=datetime(2026, 1, 15, 12, 0, 0),
        ingested=datetime(2026, 1, 15, 12, 0, 0),
        privacy_tier=PrivacyTier.OPEN,
        voice_weight=voice_weight,
    )
    target = vault / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    target.write_text(frontmatter.dumps(post), encoding="utf-8")


def _store_earlier_letter(vault: Path, external_id: str) -> None:
    """Store an AI-written letter exactly where ``/v1/letters`` stores one."""
    create_voice_draft_if_absent(
        vault_path=vault,
        external_id=external_id,
        document=VoiceDraftDocument(
            content=_LETTER_PROSE * 4, title=None, tier=PrivacyTier.OPEN
        ),
        existing_is_admitted=lambda _tier: True,
    )


# --------------------------------------------------------------------------- #
# Which voice applies
# --------------------------------------------------------------------------- #


def test_no_owner_evidence_is_the_higher_self_with_no_style(vault: Path) -> None:
    """Nothing owner-authored: today's prompt, and the response says so."""
    style = reply_style(vault, override=PrivacyTierOverride.ALL)

    assert style == HIGHER_SELF_STYLE
    assert style.voice is ReplyVoice.HIGHER_SELF
    assert style.block == ""


def test_an_owner_style_skill_is_the_owner_voice(vault: Path) -> None:
    """Lowercase, no em-dashes, short sentences: the owner's words reach the block."""
    _owner_skill(vault)

    style = reply_style(vault, override=PrivacyTierOverride.OPEN)

    assert style.voice is ReplyVoice.OWNER
    for instruction in (
        "Write in lowercase only.",
        "Never use em-dashes.",
        "Keep every sentence short.",
    ):
        assert instruction in style.block


def test_a_generated_skill_cache_is_no_ones_voice(vault: Path) -> None:
    """A native cache is a canonical signature: the same for every vault."""
    _native_skill(vault)

    style = reply_style(vault, override=PrivacyTierOverride.ALL)

    assert style == HIGHER_SELF_STYLE
    assert "zz-native-exemplar-zz" not in style.block


def test_canonical_signatures_never_enter_the_owner_block(vault: Path) -> None:
    """The owner block carries owner style only, not generic layering advice."""
    _owner_skill(vault)

    block = reply_style(vault, override=PrivacyTierOverride.ALL).block

    assert render_voice_signature("voice-core").strip() not in block
    assert render_voice_signature(f"register-{REPLY_REGISTER}").strip() not in block


def test_an_owner_register_skill_for_the_reply_register_counts(vault: Path) -> None:
    """Letters are conversational, so the owner's conversational skill applies."""
    path = vault / "creek-skills" / "registers" / f"{REPLY_REGISTER}.SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("zz-register-style-zz talk to me like a friend.", encoding="utf-8")

    style = reply_style(vault, override=PrivacyTierOverride.OPEN)

    assert style.voice is ReplyVoice.OWNER
    assert "zz-register-style-zz" in style.block
    assert VoiceRegister.CONVERSATIONAL.value == REPLY_REGISTER


def test_an_exemplar_section_of_an_owner_skill_is_never_sent(vault: Path) -> None:
    """Source-free style only: an exemplar heading cuts the file there."""
    _owner_skill(vault, OWNER_STYLE + "\n\n## Exemplars\nzz-exemplar-passage-zz")

    block = reply_style(vault, override=PrivacyTierOverride.ALL).block

    assert "Keep every sentence short." in block
    assert "zz-exemplar-passage-zz" not in block


def test_owner_samples_at_the_ceiling_ground_a_numeric_profile(vault: Path) -> None:
    """Three admitted owner samples give a profile, and none of their words."""
    install_fixture_samples(vault)

    style = reply_style(vault, override=PrivacyTierOverride.OPEN)

    assert style.voice is ReplyVoice.OWNER
    assert "Owner style profile" in style.block
    for index in (1, 2, 3):
        sample = (FIXTURES / "owner-samples" / f"owner-{index}.txt").read_text()
        first_sentence = sample.split(".")[0]
        assert first_sentence not in style.block


def test_register_samples_above_the_ceiling_are_never_read(vault: Path) -> None:
    """Personal samples cannot ground an ``open`` caller's letter."""
    for index in (1, 2, 3, 4):
        write_sample(
            vault,
            f"personal-{index}",
            (FIXTURES / "owner-samples" / "personal.txt").read_text() * 3,
            tier=PrivacyTier.PERSONAL,
        )

    assert reply_style(vault, override=PrivacyTierOverride.OPEN) == HIGHER_SELF_STYLE
    assert (
        reply_style(vault, override=PrivacyTierOverride.PERSONAL).voice
        is ReplyVoice.OWNER
    )


def test_a_persisted_fingerprint_is_the_owner_voice_at_a_ceiling_admitting_it(
    vault: Path,
) -> None:
    """The fingerprint is measured from open and personal writing alike."""
    _persist_fingerprint(vault, _FINGERPRINT_FRAGMENTS)

    personal = reply_style(vault, override=PrivacyTierOverride.PERSONAL)

    assert personal.voice is ReplyVoice.OWNER
    assert "## Voice targets" in personal.block


def test_the_fingerprint_is_withheld_from_an_open_caller(vault: Path) -> None:
    """It summarises personal-tier writing, so an ``open`` ceiling gets none of it."""
    _persist_fingerprint(vault, _FINGERPRINT_FRAGMENTS)

    assert reply_style(vault, override=PrivacyTierOverride.OPEN) == HIGHER_SELF_STYLE


def test_a_thin_fingerprint_is_not_the_owner_voice(vault: Path) -> None:
    """A thin fingerprint renders generic avoids, which describe no one."""
    _persist_fingerprint(vault, 1)

    assert reply_style(vault, override=PrivacyTierOverride.ALL) == HIGHER_SELF_STYLE


def test_a_vault_config_that_cannot_load_only_costs_the_fingerprint(
    vault: Path,
) -> None:
    """Voice never refuses: a broken config drops the fingerprint, not the skill."""
    _owner_skill(vault)
    _persist_fingerprint(vault, _FINGERPRINT_FRAGMENTS)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "llm: [unclosed", encoding="utf-8"
    )

    style = reply_style(vault, override=PrivacyTierOverride.ALL)

    assert style.voice is ReplyVoice.OWNER
    assert "## Voice targets" not in style.block


# --------------------------------------------------------------------------- #
# Never the AI's own earlier output, never borrowed authors
# --------------------------------------------------------------------------- #


def test_earlier_letters_never_become_the_owner_voice(vault: Path) -> None:
    """Stored letters (AI, weight 0, ``11-Other-Authors``) supply nothing."""
    for index in range(_FINGERPRINT_FRAGMENTS):
        _store_earlier_letter(vault, f"adepthood-voicedraft-{index:04d}")

    assert reply_style(vault, override=PrivacyTierOverride.ALL) == HIGHER_SELF_STYLE


def test_earlier_letters_never_enter_the_fingerprint_corpus(vault: Path) -> None:
    """``creek report --type fingerprint`` measures the owner, not prior letters."""
    for index in range(_FINGERPRINT_FRAGMENTS):
        _store_earlier_letter(vault, f"adepthood-voicedraft-{index:04d}")
    _write_fragment(
        vault,
        "01-Fragments/Notes/earlier-reflection.md",
        _LETTER_PROSE,
        author=Authorship.AI,
        voice_weight=0.0,
        author_slug="ai-as-user",
    )

    fingerprint = build_fingerprint(vault, AIStyleConfig(), audience_weighting=None)

    assert fingerprint.fragment_count == 0


def test_an_ai_letter_curated_into_the_samples_folder_is_refused(vault: Path) -> None:
    """The samples folder proves nothing: an AI letter is re-screened out."""
    for index in (1, 2, 3, 4):
        write_sample(
            vault,
            f"letter-{index}",
            _LETTER_PROSE * 4,
            author=Authorship.AI,
            voice_weight=0.0,
        )

    assert reply_style(vault, override=PrivacyTierOverride.ALL) == HIGHER_SELF_STYLE


def test_borrowed_authors_are_never_style(vault: Path) -> None:
    """``11-Other-Authors`` material is attributed, never the owner's voice."""
    for index in range(_FINGERPRINT_FRAGMENTS):
        _write_fragment(
            vault,
            f"11-Other-Authors/rilke/borrowed-{index}.md",
            "zz-borrowed-zz Be patient toward all that is unsolved in your heart.",
            author=Authorship.OTHER,
            voice_weight=0.0,
            author_slug="rilke",
        )
    fingerprint = build_fingerprint(vault, AIStyleConfig(), audience_weighting=None)

    assert fingerprint.fragment_count == 0
    style = reply_style(vault, override=PrivacyTierOverride.ALL)
    assert style == HIGHER_SELF_STYLE
    assert "zz-borrowed-zz" not in style.block


def test_owner_style_sections_is_the_subset_revoice_counts(vault: Path) -> None:
    """One owner-style rule for revoice and replies, not two."""
    _owner_skill(vault)

    sections = owner_style_sections(vault, phase=None, register=REPLY_REGISTER)

    assert [name for name, _style in sections] == ["voice-core"]
    assert json.dumps(sections)  # plain, serialisable text only


def test_the_wire_vocabulary_is_the_domain_vocabulary() -> None:
    """``creek_mcp.api.models.ReplyVoice`` cannot drift from the domain enum."""
    from creek_mcp.api.models import ReplyVoice as WireReplyVoice

    assert {member.value for member in WireReplyVoice} == {
        member.value for member in ReplyVoice
    }
