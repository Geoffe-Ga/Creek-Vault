"""Issue #1936: ``voice_distance`` must discriminate at eval-candidate length.

Before #1936 the score was a weighted mean of saturated magnitudes over
*every* article-applicable tell — 17 of them, total weight 16.5 — so the
denominator counted the families that did **not** diverge. One tripped
family in a 60-90 word draft contributed about 0.042, and nothing under
nine simultaneous families reached the shipped ``voice_distance_upper`` of
0.35.

The aggregation is now a **union of per-family evidence** (see
:func:`creek.generate.ai_style.distance.voice_distance`). A family that
stays inside its own margin contributes a factor of exactly 1, so it cannot
dilute anything; the families that do diverge combine as independent
evidence. Measured against the fingerprint this module builds from
``tests/fixtures/voice/in_voice`` with the production profiler, old
aggregation beside new:

| specimen                        | words | families | old    | new    |
|---------------------------------|-------|----------|--------|--------|
| the base paragraph (no slop)    |    44 |        0 | 0.0000 | 0.0000 |
| + 1 injection                   |    53 |        1 | 0.0535 | 0.8830 |
| + 2 injections                  |    62 |        2 | 0.0987 | 0.9682 |
| + 4 injections                  |    77 |        4 | 0.1821 | 0.9965 |
| + 6 injections                  |    87 |        6 | 0.2685 | 0.9997 |
| + all 16 injections             |   162 |       16 | 0.5101 | 1.0000 |

Only the last row ever cleared 0.35, and it is a lab specimen.

Five properties are pinned, each one a way the decision could silently
re-break: the family-count sweep above (the instrument discriminates), the
flag boundary for a single family (where the ceiling actually falls),
length-at-fixed-density invariance (a long draft is not punished for being
long), score-versus-findings agreement (the scalar and the findings can no
longer disagree), and what a best-of-N selector may read into the score —
including the two things it may **not** (#1939).
:class:`TestInjectionTableCompleteness` is the structural half: a tell
family added to the registry without an injection that fires it reds the
suite.

Every specimen here is invented for this test — plain synthetic sentences
plus obviously synthetic slop injections. No vault content, per FEAT-019.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Final

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style import build_fingerprint, scan
from creek.generate.ai_style.distance import (
    SATURATION_SCALE,
    FeatureContribution,
    evidence_for,
    voice_distance,
)
from creek.generate.ai_style.tells import effective_polarity, get_tells

if TYPE_CHECKING:
    from pathlib import Path

    from creek.generate.ai_style import VoiceFingerprint
    from creek.generate.ai_style.model import ScanReport

_BASE: Final[str] = (
    "I woke before the alarm. The kettle took its time. I stood at the window "
    "and watched the street fill up. I did not write anything down. I made "
    "toast and ate it standing. I washed the one cup and left it to dry."
)
"""An invented, deliberately plain paragraph that fires no tell at all."""

_INJECTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("ai_vocab_density", "I had to delve into the tapestry of it."),
    ("significance_phrase_density", "The hour plays a vital role in the day."),
    (
        "didactic_disclaimer_density",
        "It is important to note that the kettle was slow.",
    ),
    ("provenance_density", "My journals keep circling this."),
    ("transition_opener_rate", "Moreover, the toast went cold."),
    ("peacock_density", "The bustling street looked picturesque."),
    (
        "negative_parallelism_density",
        "It is not about the toast, it is about the hour.",
    ),
    ("rule_of_three_rate", "I had bread, jam, and tea."),
    ("vague_attribution_density", "Experts argue that mornings matter."),
    ("section_summary_density", "In conclusion, the morning went by."),
    (
        "knowledge_cutoff_density",
        "As of my last knowledge update, the street stayed quiet.",
    ),
    (
        "challenges_section_density",
        "Despite its charm, the street faces several challenges.",
    ),
    ("superficial_ing_density", "The gulls started, highlighting the hour."),
    ("placeholder_date_rate", "I wrote 2025-xx-xx on the pad."),
    ("list_title_lead_density", "\nThe list of morning habits is a long one.\n"),
    ("marketing_verb_ratio", "The shed boasts tools. The yard features boats."),
)
"""One synthetic injection per active tell family, each firing exactly its own.

Ordered so that *every prefix* fires exactly as many families as it has
injections — asserted by
:meth:`TestFamilyCountSweep.test_each_prefix_fires_exactly_its_own_families`,
without which the sweep below would be measuring something other than what
it claims.
"""

_INERT_FAMILY: Final[str] = "one_line_fragment_density"
"""The one article tell that is *inert* against this fingerprint (#635).

``one_line_fragment`` is a ``signature`` tell: it scores only for a writer
whose own one-line-paragraph rate clears
``signature_polarity_threshold``. The fixture voice does not, so the family
cannot be made to fire and is excluded from the injection table by
construction — asserted, not assumed, by
:meth:`TestInjectionTableCompleteness.test_the_uninjectable_family_is_inert`.
"""

_SWEEP: Final[tuple[tuple[int, float], ...]] = (
    (0, 0.0000),
    (1, 0.8830),
    (2, 0.9682),
    (4, 0.9965),
    (6, 0.9997),
    (16, 1.0000),
)
"""``(injection count, measured distance)`` — the table in the module docstring."""

_TOLERANCE: Final[float] = 5e-4
"""Slack on a pinned measurement: real drift reds, rounding does not."""

_PAD_REPEATS: Final[int] = 20
"""How many copies of :data:`_BASE` make the ~890-word pad for the boundary."""

_FIXTURE_PARTS: Final[tuple[str, ...]] = ("fixtures", "voice", "in_voice")
"""Where the in-voice exemplars that train the fixture fingerprint live."""


def _specimen(injections: int) -> str:
    """Return the base paragraph plus the first *injections* slop sentences."""
    return " ".join([_BASE, *(sentence for _key, sentence in _INJECTIONS[:injections])])


def _families(report: ScanReport) -> set[str]:
    """Return the set of feature keys that produced at least one finding."""
    return {finding.feature_key for finding in report.findings}


def _article_tell_keys(config: AIStyleConfig) -> set[str]:
    """Return every feature key the scanner measures in an ``article`` scan."""
    return {
        tell.feature_key
        for tell in get_tells(config.enabled_categories)
        if tell.applies_in("article")
    }


@pytest.fixture(scope="module")
def config() -> AIStyleConfig:
    """The shipped defaults: every measurement here is against stock config."""
    return AIStyleConfig()


@pytest.fixture(scope="module")
def fingerprint(
    tmp_path_factory: pytest.TempPathFactory,
    pytestconfig: pytest.Config,
) -> VoiceFingerprint:
    """Build a fingerprint from the in-voice fixtures via the real profiler.

    The same construction the #520 regression harness uses: write each
    exemplar as a self-authored fragment into a throwaway vault and let
    :func:`creek.generate.ai_style.build_fingerprint` measure it, so no
    rate in this module is hand-written.
    """
    source = pytestconfig.rootpath.joinpath("tests", *_FIXTURE_PARTS)
    vault: Path = tmp_path_factory.mktemp("voice-1936")
    fragments = vault / "01-Fragments"
    fragments.mkdir(parents=True)
    for index, path in enumerate(sorted(source.glob("*.md"))):
        body = path.read_text(encoding="utf-8")
        (fragments / f"exemplar_{index:02d}.md").write_text(
            "---\n"
            "source:\n"
            "  author: self\n"
            "  platform: markdown\n"
            "privacy_tier: open\n"
            f"---\n{body}\n",
            encoding="utf-8",
        )
    return build_fingerprint(vault, AIStyleConfig())


class TestEvidencePerFamily:
    """The per-family evidence term, which is where the dilution was fixed."""

    def test_a_family_inside_its_margin_is_no_evidence(self) -> None:
        """At or below its margin a family contributes nothing at all."""
        inside = FeatureContribution("a", weight=1.0, magnitude=1.4, margin=1.5)
        at_margin = FeatureContribution("a", weight=1.0, magnitude=1.5, margin=1.5)
        assert evidence_for(inside) == 0.0
        assert evidence_for(at_margin) == 0.0

    def test_a_family_past_its_margin_saturates(self) -> None:
        """Past the margin the evidence is the saturated magnitude."""
        contribution = FeatureContribution("a", weight=1.0, magnitude=5.0, margin=1.5)
        assert evidence_for(contribution) == pytest.approx(0.5)

    def test_non_diverging_families_cannot_dilute(self) -> None:
        """The #1936 defect: sixteen clean families must not shrink the score."""
        diverging = FeatureContribution("a", weight=1.0, magnitude=10.0, margin=0.5)
        clean = [
            FeatureContribution(f"clean-{i}", weight=1.0, magnitude=0.0, margin=0.5)
            for i in range(16)
        ]
        assert voice_distance([diverging]) == pytest.approx(
            voice_distance([diverging, *clean])
        )

    def test_the_worst_family_sets_a_floor(self) -> None:
        """One egregious family can no longer be averaged away by the rest."""
        worst = FeatureContribution("a", weight=1.0, magnitude=45.0, margin=0.5)
        mild = FeatureContribution("b", weight=1.0, magnitude=0.6, margin=0.5)
        assert voice_distance([worst, mild]) >= evidence_for(worst)

    def test_breadth_still_accumulates(self) -> None:
        """Two diverging families score strictly above either one alone."""
        one = FeatureContribution("a", weight=1.0, magnitude=4.0, margin=0.5)
        two = FeatureContribution("b", weight=1.0, magnitude=4.0, margin=0.5)
        assert voice_distance([one, two]) > voice_distance([one]) > 0.0

    def test_a_lighter_family_counts_for_less_than_a_heavier_one(self) -> None:
        """Weight still orders the families — relative to the heaviest in play."""
        heavy = FeatureContribution("a", weight=1.0, magnitude=20.0, margin=0.5)
        light = FeatureContribution("b", weight=0.5, magnitude=20.0, margin=0.5)
        assert voice_distance([heavy, light]) > voice_distance([heavy])
        assert voice_distance([heavy, light]) < voice_distance([heavy, heavy])

    def test_only_weight_ratios_matter(self) -> None:
        """Scaling every weight changes nothing, as under the mean it replaced.

        Each family is weighted relative to the heaviest one in play, so a
        vault that doubles all of its ``category_weights`` keeps exactly the
        same scores — and a scan where every family happens to share one
        weight is scored as if that weight were 1.0.
        """
        scaled = voice_distance(
            [
                FeatureContribution("a", weight=2.0, magnitude=20.0, margin=0.5),
                FeatureContribution("b", weight=1.0, magnitude=20.0, margin=0.5),
            ]
        )
        unscaled = voice_distance(
            [
                FeatureContribution("a", weight=1.0, magnitude=20.0, margin=0.5),
                FeatureContribution("b", weight=0.5, magnitude=20.0, margin=0.5),
            ]
        )
        assert scaled == pytest.approx(unscaled)

    def test_bounded_by_one_when_every_family_saturates(self) -> None:
        """Sixteen saturated families score at most 1.0, never above it.

        The union of 16 near-certain evidences underflows the product to
        zero in float64, so the aggregate is ``[0, 1]`` and not the ``[0,
        1)`` the per-family term enjoys. It takes a physically impossible
        rate to get there (``1e6`` occurrences per 1000 words); the real
        all-sixteen specimen in :class:`TestFamilyCountSweep` scores
        ``0.9999970``, strictly under 1.0. Pinned because
        ``voice_distance_upper`` is capped at 1.0 and a ceiling *at* the
        attainable maximum would silently stop flagging anything.
        """
        saturated = [
            FeatureContribution(f"f-{i}", weight=1.0, magnitude=1e6, margin=0.0)
            for i in range(16)
        ]
        assert 0.99 < voice_distance(saturated) <= 1.0

    def test_no_contributions_is_zero(self) -> None:
        """An empty contribution list scores zero, not a divide-by-zero."""
        assert voice_distance([]) == 0.0


class TestFamilyCountSweep:
    """The headline property: a short draft tripping one family is flagged."""

    @pytest.mark.parametrize(("injections", "expected"), _SWEEP)
    def test_sweep_matches_the_pinned_measurements(
        self,
        injections: int,
        expected: float,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """Each specimen scores what the module docstring records."""
        report = scan(_specimen(injections), fingerprint=fingerprint, config=config)
        assert report.voice_distance == pytest.approx(expected, abs=_TOLERANCE)
        assert report.voice_distance < 1.0

    @pytest.mark.parametrize(("injections", "expected"), _SWEEP)
    def test_everything_but_the_clean_specimen_is_flagged(
        self,
        injections: int,
        expected: float,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """One tripped family at candidate length clears the shipped ceiling."""
        del expected
        report = scan(_specimen(injections), fingerprint=fingerprint, config=config)
        flagged = report.voice_distance > config.voice_distance_upper
        assert flagged is (injections > 0)

    @pytest.mark.parametrize("injections", range(len(_INJECTIONS) + 1))
    def test_each_prefix_fires_exactly_its_own_families(
        self,
        injections: int,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """An injection that fires a second family would corrupt the sweep."""
        report = scan(_specimen(injections), fingerprint=fingerprint, config=config)
        expected = {key for key, _sentence in _INJECTIONS[:injections]}
        assert _families(report) == expected

    @pytest.mark.parametrize("repeats", [4, 32])
    def test_length_does_not_change_the_score_at_fixed_density(
        self,
        repeats: int,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """Same tell rate, 62 → 248 → 1984 words: identical score and verdict.

        Rates are per-1000-words, so a long draft is never punished for its
        length — the span covers the 200-word and 2000-word cases #1936 names.
        """
        specimen = _specimen(2)
        short = scan(specimen, fingerprint=fingerprint, config=config)
        long = scan(
            " ".join([specimen] * repeats), fingerprint=fingerprint, config=config
        )
        assert long.voice_distance == pytest.approx(short.voice_distance)
        assert long.family_evidence == pytest.approx(short.family_evidence)
        assert _families(long) == _families(short)
        assert (long.voice_distance > config.voice_distance_upper) is (
            short.voice_distance > config.voice_distance_upper
        )


class TestInjectionTableCompleteness:
    """A new tell family cannot be added without a specimen that fires it."""

    def test_the_table_covers_every_active_article_family(
        self,
        config: AIStyleConfig,
    ) -> None:
        """Injections plus the one inert family account for every article tell."""
        injected = {key for key, _sentence in _INJECTIONS}
        assert injected | {_INERT_FAMILY} == _article_tell_keys(config)

    def test_the_saturation_specimen_fires_every_injected_family(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """The full specimen really does trip all sixteen at once."""
        report = scan(
            _specimen(len(_INJECTIONS)), fingerprint=fingerprint, config=config
        )
        assert _families(report) == {key for key, _sentence in _INJECTIONS}

    def test_the_uninjectable_family_is_inert(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """The excluded family is excluded because it cannot fire, not by choice."""
        (tell,) = [
            candidate
            for candidate in get_tells(config.enabled_categories)
            if candidate.feature_key == _INERT_FAMILY
        ]
        user_rate = fingerprint.rate_for(tell.feature_key) or 0.0
        polarity = effective_polarity(
            tell,
            user_rate,
            signature_threshold=config.signature_polarity_threshold,
        )
        assert polarity is None

    def test_every_measured_family_reports_its_evidence(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """``family_evidence`` is the complete per-family view of one scan."""
        report = scan(
            _specimen(len(_INJECTIONS)), fingerprint=fingerprint, config=config
        )
        assert set(report.family_evidence) == _article_tell_keys(config)
        assert report.family_evidence[_INERT_FAMILY] == 0.0
        assert all(report.family_evidence[key] > 0.0 for key, _sentence in _INJECTIONS)


class TestFlagBoundary:
    """Where the ceiling falls for a single family, derived not guessed."""

    @staticmethod
    def _flagging_rate(config: AIStyleConfig) -> float:
        """Return the per-1k rate at which one weight-1.0 family hits the ceiling.

        ``evidence = rate / (rate + scale)``, so the ceiling is cleared when
        ``rate > scale * ceiling / (1 - ceiling)`` — 2.692 per 1000 words at
        the shipped ``0.35`` ceiling and saturation scale ``5.0``, i.e. about
        one extra occurrence per 371 words.
        """
        ceiling = config.voice_distance_upper
        return SATURATION_SCALE * ceiling / (1.0 - ceiling)

    def test_the_derived_flagging_rate_is_one_per_371_words(
        self,
        config: AIStyleConfig,
    ) -> None:
        """The boundary is arithmetic, so state it: 2.692 occurrences per 1k."""
        assert self._flagging_rate(config) == pytest.approx(2.6923, abs=1e-4)

    @pytest.mark.parametrize(("occurrences", "flagged"), [(2, False), (3, True)])
    def test_one_family_straddles_the_ceiling_at_fixed_length(
        self,
        occurrences: int,
        flagged: bool,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """Same draft length, one family: two occurrences clean, three flagged.

        The only thing that moves is the occurrence count, so this is the
        just-under / just-over pin: it reds if the ceiling, the saturation
        scale, or that family's margin ever moves.
        """
        pad = " ".join([_BASE] * _PAD_REPEATS)
        text = ("In conclusion, the morning went by. " * occurrences) + pad
        report = scan(text, fingerprint=fingerprint, config=config)
        rate = occurrences * 1000.0 / len(text.split())
        expected = rate / (rate + SATURATION_SCALE)
        assert _families(report) == {"section_summary_density"}
        assert report.voice_distance == pytest.approx(expected, abs=5e-3)
        assert (report.voice_distance > config.voice_distance_upper) is flagged
        assert (rate > self._flagging_rate(config)) is flagged


class TestScoreAgreesWithFindings:
    """The scalar and the findings can no longer disagree about a draft."""

    @pytest.mark.parametrize("injections", range(len(_INJECTIONS) + 1))
    def test_distance_is_zero_exactly_when_nothing_fired(
        self,
        injections: int,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """``distance == 0`` ⟺ no finding, which is what makes the ceiling honest."""
        report = scan(_specimen(injections), fingerprint=fingerprint, config=config)
        assert (report.voice_distance == 0.0) is (report.findings == [])

    def test_the_in_voice_exemplars_stay_at_exactly_zero(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
        pytestconfig: pytest.Config,
    ) -> None:
        """The no-false-reject guarantee: the fixture voice scores 0.0, not 0.0x.

        Sub-margin drift used to leak into the mean. Gating each family at
        its own margin means prose that fires nothing scores exactly zero,
        so a stricter aggregation cannot cost a false positive.
        """
        source = pytestconfig.rootpath.joinpath("tests", *_FIXTURE_PARTS)
        for path in sorted(source.glob("*.md")):
            report = scan(
                path.read_text(encoding="utf-8"),
                fingerprint=fingerprint,
                config=config,
            )
            assert report.findings == [], path.name
            assert report.voice_distance == 0.0, path.name


class TestCandidateComparability:
    """What a best-of-N selector (#1939) may and may not read into the score."""

    def test_scores_strictly_increase_over_the_useful_range(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """Ordering is sound where it matters: 0 → 1 → 2 → 4 → 6 families."""
        scores = [
            scan(_specimen(n), fingerprint=fingerprint, config=config).voice_distance
            for n, _expected in _SWEEP
        ]
        assert all(a < b for a, b in pairwise(scores))

    def test_resolution_collapses_once_many_families_diverge(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """The other documented limit: past ~7 families the score is ~1.0.

        A union of evidences approaches 1 fast, so candidates tripping seven
        or more families all land within 1e-3 of each other — and because
        each extra injection also lengthens the specimen (diluting every
        other family's density), the ordering up there is not even
        guaranteed. A best-of-N selector (#1939) therefore gets a usable
        ranking between a clean, a one-family and a two-family candidate,
        and essentially no signal among thoroughly slopped ones. Ranking
        those needs ``family_evidence``, not the scalar.
        """
        crowded = [
            scan(_specimen(n), fingerprint=fingerprint, config=config).voice_distance
            for n in range(7, len(_INJECTIONS) + 1)
        ]
        assert all(score > 0.999 for score in crowded)
        assert max(crowded) - min(crowded) < 1e-3

    def test_candidates_of_different_length_compare_by_density(
        self,
        fingerprint: VoiceFingerprint,
        config: AIStyleConfig,
    ) -> None:
        """The documented limit: one tell in a longer draft scores lower.

        The score is a *density*, so between two candidates carrying the same
        absolute number of tells the longer one wins. #1939 must therefore
        either compare candidates of comparable length or rank on
        ``family_evidence`` first and use the distance as a tie-break; this
        test pins the asymmetry rather than leaving it to be discovered.
        """
        one_tell = "I had to delve into it. "
        short = scan(one_tell + _BASE, fingerprint=fingerprint, config=config)
        long = scan(
            one_tell + " ".join([_BASE] * 4), fingerprint=fingerprint, config=config
        )
        assert _families(short) == _families(long)
        assert long.voice_distance < short.voice_distance
        assert long.voice_distance > config.voice_distance_upper
