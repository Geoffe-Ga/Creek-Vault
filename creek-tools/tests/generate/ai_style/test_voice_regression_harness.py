"""Voice-fidelity regression harness — Layer A (issue #520).

The deterministic, offline backstop that would have caught the bad draft:
it proves the existing :mod:`creek.generate.ai_style` scanner actually
*discriminates* the owner's plain reflective prose from AI slop, and that
it does so without rejecting the owner himself.

Two properties are pinned here, both using the real production API
(:func:`creek.generate.ai_style.build_fingerprint` to build a
:class:`VoiceFingerprint` and :func:`creek.generate.ai_style.scan` to score
text — the same ``voice_distance`` metric ``creek report --type
fingerprint`` and the draft guard consume; no invented metric):

1. **Discrimination.** Every in-voice fixture scores *below*
   :data:`IN_VOICE_MAX_DISTANCE` (and fires no findings), while every
   AI-slop fixture scores *above* :data:`SLOP_MIN_DISTANCE` (and fires at
   least one finding), with a clear :data:`DISCRIMINATION_MARGIN` band
   between the two thresholds.
2. **Calibration / no-false-reject.** One in-voice exemplar is *held out*
   of the fingerprint-training set; the held-out genuine snippet must still
   score as in-voice. This guards against a metric tuned so strict that it
   would flag the owner's own writing.

The fixtures live under ``tests/fixtures/voice/`` and are short,
hand-written stand-ins (this repo holds no vault content per FEAT-019); the
slop set reproduces the real failure modes — antithesis saturation
(``"Not metaphorically. Functionally."``), the journal-meta provenance tell
(``"My journals keep circling this. From one of them:"``), and
Wikipedia-style significance/peacock padding.

Pure, deterministic, offline → runs in the normal ``./scripts/check-all.sh``
gate. As the sibling detectors (#515-#518) sharpen, the observed margin
should only widen; these thresholds pin the floor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style import build_fingerprint, scan

if TYPE_CHECKING:
    from pathlib import Path

    from creek.generate.ai_style import VoiceFingerprint

# --- Calibration constants -------------------------------------------------
# All three are derived empirically from the shipped fixtures (see the
# module docstring). The owner's plain prose diverges from his own
# fingerprint by exactly 0.0 (it *is* the baseline); the slop fixtures land
# at 0.057 (antithesis), 0.061 (journal-meta) and 0.295 (wiki padding). The
# thresholds carve a generous dead-band around the weakest discriminator so
# the test fails loudly if a future change collapses the separation, while
# never sitting so close to the in-voice band that ordinary prose could trip
# it.

IN_VOICE_MAX_DISTANCE = 0.03
"""Upper bound on ``voice_distance`` for genuine owner prose. Observed
value is 0.0; the headroom absorbs benign drift in the fixtures or the
feature extractors without ever approaching the slop band."""

SLOP_MIN_DISTANCE = 0.05
"""Lower bound on ``voice_distance`` for AI slop. The weakest slop fixture
(antithesis saturation) measures ~0.057, so 0.05 is the floor every slop
sample must clear — the regression tripwire if a detector regresses."""

DISCRIMINATION_MARGIN = SLOP_MIN_DISTANCE - IN_VOICE_MAX_DISTANCE
"""The required dead-band between the in-voice ceiling and the slop floor.
A non-trivial gap (0.02) is the whole point: it certifies the metric does
not merely rank slop higher, it separates the two populations cleanly."""

# Minimum eligible fragments below which the fingerprint is "thin" and
# distance is softened (mirrors ``AIStyleConfig.min_fingerprint_fragments``).
# The held-out training set must stay above this so the calibration check
# scores against a *trusted* fingerprint, not a softened one.
_MIN_TRUSTED_FRAGMENTS = AIStyleConfig().min_fingerprint_fragments

_FIXTURE_ROOT_PARTS = ("fixtures", "voice")


def _voice_fixture_root(request: pytest.FixtureRequest) -> Path:
    """Return the ``tests/fixtures/voice`` directory for this test module.

    Args:
        request: The active pytest request (used to locate ``tests/``).

    Returns:
        The absolute path to the voice fixture root.
    """
    tests_dir = request.config.rootpath / "tests"
    return tests_dir.joinpath(*_FIXTURE_ROOT_PARTS)


def _load_snippets(directory: Path) -> list[tuple[str, str]]:
    """Return ``(name, text)`` for every ``*.md`` snippet under *directory*.

    Args:
        directory: A fixture subdirectory (``in_voice`` or ``slop``).

    Returns:
        Sorted ``(filename, raw prose)`` pairs.
    """
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.md"))
    ]


def _fingerprint_from_texts(
    texts: list[str],
    tmp_path: Path,
    config: AIStyleConfig,
) -> VoiceFingerprint:
    """Build a :class:`VoiceFingerprint` from in-memory exemplar *texts*.

    Each text is written as a minimal self-authored fragment into a throwaway
    vault so the real :func:`build_fingerprint` (the production profiler)
    measures them — no metric is reimplemented here.

    Args:
        texts: The in-voice exemplar bodies.
        tmp_path: A unique throwaway directory to scaffold the vault under.
        config: The AI-style configuration to profile with.

    Returns:
        The fingerprint built from *texts*.
    """
    fragments_dir = tmp_path / "01-Fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    for index, text in enumerate(texts):
        fragment = (
            "---\n"
            "source:\n"
            "  author: self\n"
            "  platform: markdown\n"
            "privacy_tier: open\n"
            "---\n"
            f"{text}\n"
        )
        (fragments_dir / f"exemplar_{index:02d}.md").write_text(
            fragment,
            encoding="utf-8",
        )
    return build_fingerprint(tmp_path, config)


@pytest.fixture
def config() -> AIStyleConfig:
    """Return the default AI-style configuration used by every assertion."""
    return AIStyleConfig()


@pytest.fixture
def in_voice_snippets(request: pytest.FixtureRequest) -> list[tuple[str, str]]:
    """Return the in-voice ``(name, text)`` fixtures."""
    return _load_snippets(_voice_fixture_root(request) / "in_voice")


@pytest.fixture
def slop_snippets(request: pytest.FixtureRequest) -> list[tuple[str, str]]:
    """Return the AI-slop ``(name, text)`` fixtures."""
    return _load_snippets(_voice_fixture_root(request) / "slop")


class TestFixtureSanity:
    """Guard the fixtures themselves so the harness can't silently no-op."""

    def test_threshold_band_is_well_formed(self) -> None:
        """The slop floor sits strictly above the in-voice ceiling."""
        assert IN_VOICE_MAX_DISTANCE < SLOP_MIN_DISTANCE
        assert DISCRIMINATION_MARGIN > 0.0

    def test_enough_in_voice_fixtures_for_holdout(
        self,
        in_voice_snippets: list[tuple[str, str]],
    ) -> None:
        """Holding one out must still leave a trusted (non-thin) fingerprint."""
        assert len(in_voice_snippets) - 1 >= _MIN_TRUSTED_FRAGMENTS

    def test_slop_fixtures_present(
        self,
        slop_snippets: list[tuple[str, str]],
    ) -> None:
        """The known failure modes are all represented."""
        names = {name for name, _ in slop_snippets}
        assert {"01_antithesis.md", "02_journal_meta.md", "03_wiki_padding.md"} <= names


class TestDiscrimination:
    """Property 1: in-voice scores low, slop scores high, with a margin."""

    def test_in_voice_scores_below_threshold(
        self,
        in_voice_snippets: list[tuple[str, str]],
        tmp_path: Path,
        config: AIStyleConfig,
    ) -> None:
        """Every in-voice fixture lands in the in-voice band with no findings."""
        texts = [text for _, text in in_voice_snippets]
        fingerprint = _fingerprint_from_texts(texts, tmp_path, config)
        assert not fingerprint.is_thin(_MIN_TRUSTED_FRAGMENTS)
        for name, text in in_voice_snippets:
            report = scan(text, fingerprint=fingerprint, config=config)
            assert report.voice_distance <= IN_VOICE_MAX_DISTANCE, name
            assert report.findings == [], name

    def test_slop_scores_above_threshold(
        self,
        in_voice_snippets: list[tuple[str, str]],
        slop_snippets: list[tuple[str, str]],
        tmp_path: Path,
        config: AIStyleConfig,
    ) -> None:
        """Every slop fixture clears the slop floor and fires ≥1 finding."""
        texts = [text for _, text in in_voice_snippets]
        fingerprint = _fingerprint_from_texts(texts, tmp_path, config)
        for name, text in slop_snippets:
            report = scan(text, fingerprint=fingerprint, config=config)
            assert report.voice_distance >= SLOP_MIN_DISTANCE, name
            assert report.findings, name

    def test_every_slop_beats_every_in_voice_with_margin(
        self,
        in_voice_snippets: list[tuple[str, str]],
        slop_snippets: list[tuple[str, str]],
        tmp_path: Path,
        config: AIStyleConfig,
    ) -> None:
        """The worst slop still out-distances the worst in-voice by the margin."""
        texts = [text for _, text in in_voice_snippets]
        fingerprint = _fingerprint_from_texts(texts, tmp_path, config)
        in_voice_scores = [
            scan(text, fingerprint=fingerprint, config=config).voice_distance
            for _, text in in_voice_snippets
        ]
        slop_scores = [
            scan(text, fingerprint=fingerprint, config=config).voice_distance
            for _, text in slop_snippets
        ]
        assert min(slop_scores) - max(in_voice_scores) >= DISCRIMINATION_MARGIN


class TestCalibration:
    """Property 2: a held-out genuine exemplar is not falsely rejected."""

    def test_held_out_owner_exemplar_scores_in_voice(
        self,
        in_voice_snippets: list[tuple[str, str]],
        tmp_path: Path,
        config: AIStyleConfig,
    ) -> None:
        """Train on all but one exemplar; the held-out real snippet stays low.

        This is the no-false-reject guard: a metric so strict it would flag
        the owner's own unseen writing would fail here even while passing
        discrimination.
        """
        held_out_name, held_out_text = in_voice_snippets[-1]
        training_texts = [text for _, text in in_voice_snippets[:-1]]
        fingerprint = _fingerprint_from_texts(training_texts, tmp_path, config)
        # The held-out exemplar must be scored against a *trusted* (non-thin)
        # fingerprint, else softening — not genuine similarity — would carry it.
        assert not fingerprint.is_thin(_MIN_TRUSTED_FRAGMENTS)
        report = scan(held_out_text, fingerprint=fingerprint, config=config)
        assert report.voice_distance <= IN_VOICE_MAX_DISTANCE, held_out_name
        assert report.findings == [], held_out_name
