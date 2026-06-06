"""Citation/quotation-density voice pattern, audience-weighted.

The voice proxy learns the owner's habit of referencing and quoting sources.
``VoicePatternExtractor`` measures a ``citation_density`` (signals per ~1000
words) and aggregates it with the per-fragment audience-authority weight, so
the heavy-citation habit of public work dominates over citation-light private
writing. The generated voice skill surfaces the tendency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.voice import (
    VoicePatternExtractor,
    VoiceProfileGenerator,
    _count_citation_signals,
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

_CITATION_HEAVY = (
    'According to Smith, the river "shapes the valley over time" [1]. '
    "As Jones writes, geology is slow (Jones, 2023). See https://example.com/geo "
    "for the dataset.\n> The current never argues; it simply persists.\n"
    'In Darwin\'s words, "endless forms most beautiful" remain.'
)
_CITATION_LIGHT = (
    "I went for a walk this morning and thought about the project. "
    "It felt good to move and let the ideas settle on their own."
)


def test_no_citations_yields_zero_density() -> None:
    """A corpus with no citation signals has zero citation density."""
    patterns = VoicePatternExtractor().extract_patterns([_CITATION_LIGHT])
    assert patterns.citation_density == 0.0


def test_other_patterns_unaffected_by_citation_metric() -> None:
    """Adding the citation metric does not disturb the other patterns."""
    patterns = VoicePatternExtractor().extract_patterns([_CITATION_LIGHT])
    # The existing metrics are still populated for a citation-free corpus.
    assert patterns.sentence_metrics.total_sentences > 0
    assert patterns.citation_density == 0.0


@pytest.mark.parametrize(
    "text",
    [
        'He said "this is a quote" clearly.',
        "> a blockquote line",
        "According to Smith, this holds.",
        "The result is robust [1].",
        "Prior work shows it (Smith, 2023).",
        "See https://example.com for details.",
    ],
)
def test_each_citation_signal_registers(text: str) -> None:
    """Each citation signal family produces a positive density."""
    patterns = VoicePatternExtractor().extract_patterns([text + " " + "word " * 50])
    assert patterns.citation_density > 0.0


def test_citation_heavy_corpus_exceeds_light_corpus() -> None:
    """Citation-heavy public work yields a higher density than light work."""
    heavy = VoicePatternExtractor().extract_patterns([_CITATION_HEAVY])
    light = VoicePatternExtractor().extract_patterns([_CITATION_LIGHT])
    assert heavy.citation_density > light.citation_density
    assert light.citation_density == 0.0


def test_audience_weighting_lifts_public_citation_signal() -> None:
    """Weighting toward the citation-heavy public text raises the aggregate."""
    texts = [_CITATION_HEAVY, _CITATION_LIGHT]
    extractor = VoicePatternExtractor()
    uniform = extractor.extract_patterns(texts)
    # The public (citation-heavy) text carries more authority weight.
    weighted = extractor.extract_patterns(texts, weights=[1.5, 1.0])
    assert weighted.citation_density > uniform.citation_density


def test_all_zero_weights_yield_zero_density() -> None:
    """All-zero weights yield a safe zero density (no division by zero)."""
    patterns = VoicePatternExtractor().extract_patterns(
        [_CITATION_HEAVY],
        weights=[0.0],
    )
    assert patterns.citation_density == 0.0


def test_mismatched_weights_fall_back_to_uniform() -> None:
    """A weights list of the wrong length falls back to uniform weighting."""
    texts = [_CITATION_HEAVY, _CITATION_LIGHT]
    extractor = VoicePatternExtractor()
    uniform = extractor.extract_patterns(texts)
    mismatched = extractor.extract_patterns(texts, weights=[1.0])
    assert mismatched.citation_density == uniform.citation_density


def test_negative_weights_are_clamped() -> None:
    """A negative weight is clamped to 0, never producing a sub-zero density."""
    patterns = VoicePatternExtractor().extract_patterns(
        [_CITATION_HEAVY, _CITATION_LIGHT],
        weights=[-5.0, 1.0],
    )
    # The citation-heavy text is clamped out; only the light text remains.
    assert patterns.citation_density == 0.0


def test_dialogue_and_scare_quotes_are_not_citations() -> None:
    """Short quoted spans (dialogue, scare quotes) do not count as citations."""
    prose = (
        '"I\'ll be back," he said, choosing the "obvious" path. '
        'She ran it with "--verbose" and moved on. ' + "word " * 40
    )
    patterns = VoicePatternExtractor().extract_patterns([prose])
    assert patterns.citation_density == 0.0


def test_overlapping_signals_compound_by_design() -> None:
    """A dense line that quotes, attributes, and links fires every counter."""
    # Blockquote + attribution ("according to") + outbound link on one line.
    layered = "> According to Smith, see https://example.com/data"
    assert _count_citation_signals(layered) == 3
    # A plain blockquote with none of the other families fires only once.
    assert _count_citation_signals("> just a quoted thought here") == 1


def _citation_fragment(frag_id: str, privacy: PrivacyTier) -> Fragment:
    """Build a fully-classified analytical fragment at the given privacy tier."""
    return Fragment(
        id=frag_id,
        title=frag_id,
        source=FragmentSource(platform=SourcePlatform.ESSAY),
        frequency=FrequencyClassification(primary=Frequency.F5),
        wavelength=WavelengthClassification(phase=Phase.RISING, mode=Mode.EXPRESS),
        voice=VoiceClassification(
            voice_register=VoiceRegister.ANALYTICAL,
            confidence=Confidence.CONVICTION,
        ),
        privacy_tier=privacy,
    )


def _write_fragment(vault: Path, fragment: Fragment, *, body: str) -> None:
    """Persist *fragment* with *body* under ``01-Fragments/Writing``."""
    target = vault / "01-Fragments" / "Writing" / f"{fragment.id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content=body, **fragment.model_dump(mode="json"))
    target.write_text(frontmatter.dumps(post), encoding="utf-8")


def test_generated_skill_surfaces_citation_via_weighted_path(tmp_path: Path) -> None:
    """The full weighted ``generate_all_profiles`` path surfaces the habit.

    This exercises the production path (``extract_patterns(bodies,
    weights=...)``) rather than an unweighted call, so the audience-weighted
    feature is covered end-to-end at the prompt level.
    """
    vault = tmp_path / "vault"
    body = _CITATION_HEAVY + "\n\n" + "word " * 250
    _write_fragment(
        vault,
        _citation_fragment("frag-open", PrivacyTier.OPEN),
        body=body,
    )

    generator = VoiceProfileGenerator(max_exemplars=5, min_exemplars=1)
    written = generator.generate_all_profiles(vault)

    rendered = "\n".join(path.read_text(encoding="utf-8") for path in written).lower()
    assert "cite" in rendered or "reference" in rendered or "quote" in rendered
