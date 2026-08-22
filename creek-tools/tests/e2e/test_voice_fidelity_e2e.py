"""Voice-fidelity regression harness — Layer B, opt-in true E2E (issue #520).

The end-to-end backstop: it runs the *real* draft path
(:meth:`creek.generate.drafts.DraftGenerator.generate_draft`) on a fixed
seed in a throwaway vault, then scores the produced draft body with the
production :func:`creek.generate.ai_style.scan` / ``voice_distance`` metric
against a :class:`VoiceFingerprint` built from that vault. This is the layer
that would have caught the bad draft for real, not just in fixtures.

A genuine network run requires ``CREEK_ANTHROPIC_CONSENT`` and is
non-deterministic, so it cannot be hermetic or assert on exact text. Per the
issue, this test therefore **stubs the LLM** (no network) while keeping the
``e2e`` marker and the full threshold-assertion structure: the draft flows
through ``generate_draft`` exactly as in production, and only the model hop
is replaced by a fixed emitter. Two emitters are exercised:

* an *in-voice* emitter, whose draft must score ``voice_distance <=
  DRAFT_MAX_VOICE_DISTANCE`` and fire no HARD tell above baseline; and
* a *slop* emitter, whose draft the harness must *catch* (distance above the
  in-voice ceiling) — proving the backstop actually fires end to end.

Marked ``@pytest.mark.e2e`` so it is **excluded from the default
``./scripts/test.sh`` gate** (which runs ``-m "not e2e"``, CI-003) and only
runs under ``--all`` / ``--e2e``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style import build_fingerprint, scan
from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.models import Frequency
from creek.scaffold import scaffold_vault

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

DRAFT_MAX_VOICE_DISTANCE = 0.03
"""Upper bound on a generated draft's ``voice_distance`` for the in-voice
emitter. Matches the Layer A in-voice ceiling: a draft that genuinely sounds
like the owner must land in the same band as the owner's own prose. Bounded
threshold (not exact text) because LLM output is non-deterministic in a real
run; the stub stays well inside it."""

SLOP_DRAFT_MIN_VOICE_DISTANCE = 0.05
"""Lower bound the slop emitter's draft must exceed so the harness is proven
to *catch* divergence end to end (the regression tripwire), not merely pass
trivially. Matches the Layer A slop floor."""

_IN_VOICE_DRAFT = (
    "I sat with the cold coffee again this morning and did not do much. "
    "The dog needed walking and I did not feel like it, so I went anyway. "
    "The air helped, the way it usually does. Nothing was solved but I felt "
    "a little steadier by the time I came back in.\n"
)

_SLOP_DRAFT = (
    "Not metaphorically. Functionally. The work isn't to save everyone. "
    "The work is to be findable. My journals keep circling this. From one of "
    "them: this pivotal moment serves as a testament to the intricate "
    "tapestry of resilience. It is important to note that this represents a "
    "significant shift.\n"
)

_OWNER_EXEMPLARS = (
    "I woke up early and sat with the coffee going cold. Nothing dramatic. "
    "I just wanted to write down that the dog needed walking.",
    "We talked for a long time about the boat. He wants to fix the engine "
    "himself. I told him that was a bad idea but he is stubborn and so am I.",
    "The kids were loud at dinner. I lost my temper over something small and "
    "felt bad after. Some days I manage to slow down. Today I did not.",
    "I have been reading the same book for a month. A few pages a night. It "
    "is slow but I like it and there is no hurry.",
    "Spent the afternoon clearing the shed. Found my father's old tools at "
    "the back and sat on the floor for a while holding the hammer.",
    "Rain again. The garden is a mess and I cannot get out to it. I made soup "
    "instead and it was fine, a bit too salty.",
)


def _seed_vault(vault: Path) -> None:
    """Scaffold a real vault and fill it with self-authored seed fragments.

    The folder tree comes from :func:`creek.scaffold.scaffold_vault` — the
    ``creek init`` code path — rather than a local four-entry list, so the
    harness cannot quietly run against a shape no user ever gets (#1025).

    Args:
        vault: The throwaway vault root.
    """
    scaffold_vault(vault)
    fragments = vault / "01-Fragments"
    for index, body in enumerate(_OWNER_EXEMPLARS):
        front = (
            "---\n"
            f"id: frag-{index:03d}\n"
            "source:\n"
            "  author: self\n"
            "  platform: markdown\n"
            "privacy_tier: open\n"
            "---\n"
            f"{body}\n"
        )
        (fragments / f"frag-{index:03d}.md").write_text(front, encoding="utf-8")


def _skill_tree(vault: Path) -> Path:
    """Create an empty skill tree and return its root.

    Args:
        vault: The throwaway vault root.

    Returns:
        The skills-root directory.
    """
    root = vault / "skills"
    for sub in ("frequencies", "phases", "modes", "registers"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _seed() -> IdeaSeed:
    """Return the fixed idea seed every emitter drafts from."""
    return IdeaSeed(
        strategy=MiningStrategy.LIMINAL_CROSS_EDDY,
        title="A quiet morning",
        source_fragments=("frag-000",),
        threads=(),
        eddies=(),
        frequency_affinity=(Frequency.F1,),
        brief_description="An ordinary morning, written plainly.",
        score=0.8,
    )


def _generate_body(vault: Path, emitted: str) -> str:
    """Run the real draft path with a fixed LLM emitter and return the body.

    Args:
        vault: The scaffolded throwaway vault.
        emitted: The exact draft body the stubbed LLM returns.

    Returns:
        The generated :class:`~creek.generate.drafts.Draft` body.
    """
    generator = DraftGenerator(
        llm=lambda _prompt: emitted,
        skills_root=_skill_tree(vault),
    )
    draft = generator.generate_draft(_seed(), vault_path=vault)
    return draft.body


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a scaffolded throwaway vault with owner exemplar fragments."""
    _seed_vault(tmp_path)
    return tmp_path


class TestDraftVoiceFidelityE2E:
    """End-to-end: real draft path scored by the production voice metric."""

    def test_in_voice_draft_scores_within_threshold(self, vault: Path) -> None:
        """An owner-sounding draft lands in the in-voice band with no findings."""
        config = AIStyleConfig()
        fingerprint = build_fingerprint(vault, config)
        assert not fingerprint.is_thin(config.min_fingerprint_fragments)
        body = _generate_body(vault, _IN_VOICE_DRAFT)
        report = scan(body, fingerprint=fingerprint, config=config)
        assert report.voice_distance <= DRAFT_MAX_VOICE_DISTANCE
        assert report.findings == []

    def test_slop_draft_is_caught(self, vault: Path) -> None:
        """A slop draft from the same path is flagged above the in-voice ceiling.

        Proves the backstop fires end to end: had the bad draft come through
        ``generate_draft``, this assertion would have caught it.
        """
        config = AIStyleConfig()
        fingerprint = build_fingerprint(vault, config)
        body = _generate_body(vault, _SLOP_DRAFT)
        report = scan(body, fingerprint=fingerprint, config=config)
        assert report.voice_distance >= SLOP_DRAFT_MIN_VOICE_DISTANCE
        assert report.findings

    def test_saved_draft_body_is_what_was_scored(self, vault: Path) -> None:
        """The body flowing through the draft path is the prose under test.

        A guard against the harness silently scoring an empty or
        frontmatter-only body and passing for the wrong reason.
        """
        body = _generate_body(vault, _IN_VOICE_DRAFT)
        post = frontmatter.loads(body)
        assert post.content.strip()
