"""End-to-end acceptance proof for the Essay Voice Authenticity epic.

Ties the four fixes together on one vault: ingest an AI chat and an OPEN
citation-heavy essay, generate the voice profile, draft an essay seeded with
tells, and assert (a) AI turns are excluded from the voice corpus, (b) the
OPEN citation habit shows up in the voice skill, (c) the planted tells are
stripped from the draft, and (d) ``creek voice-authenticity`` reports a low
AI-corpus leak, weighting ON, and a ``rewritten`` de-slop status.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.drafts import Draft, DraftGenerator
from creek.generate.voice import VoiceProfileGenerator
from creek.generate.voice_authenticity import build_voice_authenticity_report
from creek.ingest.base import RawDocument, assemble_ingested_fragment
from creek.ingest.claude import ClaudeIngestor
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

_CITATION_ESSAY = (
    "According to Smith, knowledge compounds [1]. As Jones writes, structure "
    "matters (Jones, 2023); see https://example.com/essay for the argument.\n"
    "> A system should mirror how you actually think.\n" + "word " * 260
)
_TROPEY_DRAFT = (
    "Additionally, this delves into the rich tapestry of the subject. "
    "Moreover, it is important to note the vibrant, multifaceted nuances."
)
_CLEAN_REWRITE = "The valley holds water. Time moves through it. I watched and learned."


def _claude_export() -> bytes:
    """A one-turn Claude export JSON blob."""
    conv = {
        "uuid": "conv-e2e",
        "name": "Knowledge chat",
        "created_at": "2024-11-15T10:00:00Z",
        "messages": [
            {
                "role": "human",
                "content": "How should I organise what I know?",
                "created_at": "2024-11-15T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Let me delve into the rich tapestry of knowledge systems.",
                "created_at": "2024-11-15T10:00:15Z",
            },
        ],
    }
    return json.dumps({"conversations": [conv]}).encode("utf-8")


def _ingest_claude_chat(vault: Path) -> None:
    """Ingest the Claude chat into the vault as per-turn attributed fragments."""
    ingestor = ClaudeIngestor()
    raw = RawDocument(
        path=Path("claude_export.json"),
        content=_claude_export(),
        metadata={},
        detected_encoding="utf-8",
    )
    fragments_dir = vault / "01-Fragments" / "Conversations"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    for parsed in ingestor.parse(raw):
        parsed.metadata["markdown"] = ingestor.convert_to_markdown(parsed)
        parsed.metadata["frontmatter"] = ingestor.generate_frontmatter(parsed)
        fragment = assemble_ingested_fragment(parsed).fragment
        payload = fragment.model_dump(mode="json", exclude={"voice_proxy_eligible"})
        (fragments_dir / f"{fragment.id}.md").write_text(
            frontmatter.dumps(frontmatter.Post("body", **payload)),
            encoding="utf-8",
        )


def _write_open_citation_essay(vault: Path) -> None:
    """Write an OPEN, citation-heavy analytical essay fragment."""
    fragment = Fragment(
        id="frag-open-essay",
        title="On structuring knowledge",
        source=FragmentSource(platform=SourcePlatform.ESSAY),
        created=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=FrequencyClassification(primary=Frequency.F5),
        wavelength=WavelengthClassification(phase=Phase.RISING, mode=Mode.EXPRESS),
        voice=VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        ),
        privacy_tier=PrivacyTier.OPEN,
    )
    writing = vault / "01-Fragments" / "Writing"
    writing.mkdir(parents=True, exist_ok=True)
    payload = fragment.model_dump(mode="json", exclude={"voice_proxy_eligible"})
    (writing / "essay.md").write_text(
        frontmatter.dumps(frontmatter.Post(_CITATION_ESSAY, **payload)),
        encoding="utf-8",
    )


@pytest.mark.e2e
def test_voice_authenticity_epic_end_to_end(tmp_path: Path) -> None:
    """All four epic acceptance points hold on one vault."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    _ingest_claude_chat(vault)
    _write_open_citation_essay(vault)

    report = build_voice_authenticity_report(vault, draft_path=None)

    # (a) AI turns are excluded from the voice corpus; (d) leak is low + weighting ON.
    assert report.audience_mix.weighting_active is True
    assert report.ai_corpus_leak.leaked == 0
    from creek.vault.reader import iter_vault_fragments

    fragments = [f for _p, f, _b, _r in iter_vault_fragments(vault / "01-Fragments")]
    ai_fragments = [f for f in fragments if str(f.source.author) == "ai"]
    assert ai_fragments
    assert all(f.voice_proxy_eligible is False for f in ai_fragments)

    # (b) The OPEN citation habit is reflected in the generated voice skill.
    written = VoiceProfileGenerator(
        max_exemplars=5, min_exemplars=1
    ).generate_all_profiles(
        vault,
    )
    rendered = "\n".join(p.read_text(encoding="utf-8") for p in written).lower()
    assert "cite" in rendered or "reference" in rendered or "quote" in rendered

    # (c) A draft seeded with tells comes out of the real path with them stripped,
    # and (d) the de-slop status is recorded as rewritten.
    fingerprint = VoiceFingerprint(
        features={"em_dash_density": FeatureStat(rate=0.0, support=20)},
        fragment_count=20,
    )
    generator = DraftGenerator(
        llm=lambda _prompt: _CLEAN_REWRITE,
        skills_root=tmp_path / "skills",
        fingerprint=fingerprint,
        ai_style_config=AIStyleConfig(voice_distance_upper=0.001),
    )
    draft = Draft(
        title="Seeded essay",
        body=_TROPEY_DRAFT,
        idea_strategy="thread_terminus",
        source_fragments=(),
        threads=(),
        eddies=(),
        skill_stack=(),
        prompt="prompt",
        generated_date=datetime(2026, 6, 6, 12, 0, tzinfo=UTC),
    )
    draft_path = generator.save_draft(draft, vault)
    draft_text = draft_path.read_text(encoding="utf-8")
    assert "rich tapestry" not in draft_text
    assert "delve" not in draft_text

    deslop = build_voice_authenticity_report(vault, draft_path=draft_path).deslop
    assert deslop is not None
    assert deslop.status == "rewritten"
