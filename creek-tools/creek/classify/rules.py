"""Rule-based classification using keyword/pattern matching.

Provides a scoring-based keyword classifier that scans fragment content
for signal words associated with APTITUDE frequencies, wavelength phases,
engagement modes, voice registers, and confidence levels.  This is intended
as a fast first-pass classifier before optional LLM refinement.

Scoring uses positional weighting: title matches count 3x, first-paragraph
matches count 2x, and body matches count 1x.  Word-boundary matching
(via ``re.findall``) prevents partial-word false positives.
"""

import logging
import operator
import re
from enum import Enum
from typing import TypeVar

from creek.classify.evidence import layer_determined_over
from creek.models import (
    Confidence,
    Fragment,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    VoiceClassification,
    VoiceRegister,
    WavelengthClassification,
)

logger = logging.getLogger(__name__)

_EnumT = TypeVar("_EnumT", bound=Enum)

# ---- Signal Dictionaries ----

FREQUENCY_SIGNALS: dict[Frequency, list[str]] = {
    Frequency.F1: [
        "survival",
        "safety",
        "security",
        "threat",
        "danger",
        "shelter",
        "hunger",
        "thirst",
        "instinct",
        "primal",
        "basic needs",
        "fight or flight",
    ],
    Frequency.F2: [
        "belonging",
        "ritual",
        "tradition",
        "ancestors",
        "sacred",
        "tribe",
        "loyalty",
        "kinship",
        "ceremony",
        "mystical",
        "spirits",
        "elders",
    ],
    Frequency.F3: [
        "power",
        "dominance",
        "control",
        "conquest",
        "force",
        "aggression",
        "bold",
        "fearless",
        "warrior",
        "rage",
        "impulsive",
        "rebellion",
    ],
    Frequency.F4: [
        "order",
        "discipline",
        "rules",
        "duty",
        "authority",
        "morality",
        "righteousness",
        "structure",
        "hierarchy",
        "obedience",
        "law",
    ],
    Frequency.F5: [
        "achievement",
        "success",
        "strategy",
        "competition",
        "innovation",
        "progress",
        "efficiency",
        "ambition",
        "goal",
        "winning",
        "profit",
        "excellence",
    ],
    Frequency.F6: [
        "community",
        "equality",
        "consensus",
        "empathy",
        "feelings",
        "harmony",
        "inclusion",
        "diversity",
        "caring",
        "sharing",
        "sustainability",
        "social justice",
    ],
    Frequency.F7: [
        "systems",
        "complexity",
        "integration",
        "patterns",
        "emergence",
        "adaptive",
        "flexibility",
        "knowledge",
        "synthesis",
        "meta",
        "perspective",
        "paradox",
    ],
    Frequency.F8: [
        "holistic",
        "collective",
        "ecology",
        "global",
        "consciousness",
        "interconnection",
        "transcendence",
        "integral",
        "planetary",
        "evolution",
        "awakening",
        "wholeness",
    ],
    Frequency.F9: [
        "cosmic",
        "infinite",
        "nondual",
        "unity",
        "source",
        "dissolution",
        "beyond",
        "transpersonal",
        "absolute",
        "ineffable",
        "primordial",
    ],
    Frequency.F10: [
        "clear light",
        "emptiness",
        "suchness",
        "luminosity",
        "groundless",
        "unborn",
        "buddha nature",
        "rigpa",
        "dzogchen",
    ],
}
"""Full APTITUDE frequency keyword signals (F1-F10)."""

WAVELENGTH_PHASE_SIGNALS: dict[Phase, list[str]] = {
    Phase.RISING: [
        "emerging",
        "building",
        "growing",
        "momentum",
        "ascending",
        "intensifying",
        "gathering",
        "awakening",
        "stirring",
        "kindling",
    ],
    Phase.PEAKING: [
        "peak",
        "climax",
        "intense",
        "maximum",
        "fullness",
        "overflow",
        "culmination",
        "apex",
        "zenith",
        "crest",
    ],
    Phase.WITHDRAWAL: [
        "retreating",
        "pulling back",
        "withdrawing",
        "receding",
        "contracting",
        "fading",
        "letting go",
    ],
    Phase.DIMINISHING: [
        "diminishing",
        "declining",
        "waning",
        "subsiding",
        "settling",
        "calming",
        "quieting",
        "releasing",
    ],
    Phase.BOTTOMING_OUT: [
        "bottoming",
        "void",
        "stillness",
        "dormant",
        "fallow",
        "desolate",
        "exhausted",
        "depleted",
    ],
    Phase.RESTORATION: [
        "restoring",
        "renewing",
        "recovering",
        "regenerating",
        "healing",
        "replenishing",
        "reviving",
        "returning",
    ],
}
"""Mapping of wavelength phases to their keyword signals."""

MODE_SIGNALS: dict[Mode, list[str]] = {
    Mode.INHABIT: [
        "dwelling",
        "immersed",
        "inhabiting",
        "living in",
        "being with",
        "sitting with",
        "resting in",
        "holding",
        "containing",
        "marinating",
    ],
    Mode.EXPRESS: [
        "expressing",
        "creating",
        "articulating",
        "voicing",
        "manifesting",
        "outputting",
        "projecting",
        "communicating",
        "generating",
        "producing",
    ],
    Mode.COLLABORATE: [
        "collaborating",
        "co-creating",
        "partnering",
        "dialoguing",
        "engaging with",
        "building together",
        "sharing",
        "mutual",
        "reciprocal",
    ],
    Mode.INTEGRATE: [
        "integrating",
        "synthesizing",
        "weaving",
        "combining",
        "reconciling",
        "harmonizing",
        "unifying",
        "bridging",
        "connecting",
        "merging",
    ],
    Mode.ABSORB: [
        "absorbing",
        "receiving",
        "taking in",
        "learning",
        "studying",
        "digesting",
        "processing",
        "contemplating",
        "reflecting",
        "listening",
    ],
}
"""Mapping of engagement modes to their keyword signals."""

VOICE_REGISTER_SIGNALS: dict[VoiceRegister, list[str]] = {
    VoiceRegister.CONFESSIONAL: [
        "confess",
        "admit",
        "reveal",
        "vulnerable",
        "honest truth",
        "deeply",
        "personally",
        "shame",
        "guilt",
        "bare",
    ],
    VoiceRegister.ANALYTICAL: [
        "analyze",
        "examine",
        "consider",
        "therefore",
        "evidence",
        "hypothesis",
        "framework",
        "systematically",
        "data",
        "observe",
    ],
    VoiceRegister.PLAYFUL: [
        "play",
        "fun",
        "silly",
        "joke",
        "laugh",
        "whimsy",
        "delightful",
        "absurd",
        "quirky",
        "tease",
    ],
    VoiceRegister.PROPHETIC: [
        "prophesy",
        "vision",
        "calling",
        "destiny",
        "must",
        "shall",
        "truth is",
        "mark my words",
        "revelation",
    ],
    VoiceRegister.INSTRUCTIONAL: [
        "step",
        "guide",
        "how to",
        "method",
        "practice",
        "technique",
        "approach",
        "follow",
        "instruction",
        "tip",
    ],
    VoiceRegister.RAW: [
        "raw",
        "unfiltered",
        "painful",
        "gut",
        "visceral",
        "real",
        "ugly",
        "broken",
        "screaming",
        "bleeding",
    ],
    VoiceRegister.CONVERSATIONAL: [
        "chat",
        "you know",
        "anyway",
        "honestly",
        "right",
        "thing is",
        "basically",
        "kind of",
        "sort of",
    ],
}
"""Mapping of voice registers to keyword signals."""

CONFIDENCE_SIGNALS: dict[Confidence, list[str]] = {
    Confidence.MUSING: [
        "maybe",
        "perhaps",
        "wondering",
        "what if",
        "could be",
        "not sure",
        "possibly",
        "might",
    ],
    Confidence.EXPLORING: [
        "exploring",
        "investigating",
        "looking into",
        "trying to understand",
        "curious about",
        "digging into",
    ],
    Confidence.FORMING: [
        "starting to see",
        "beginning to",
        "it seems like",
        "leaning toward",
        "crystallizing",
        "taking shape",
    ],
    Confidence.SETTLED: [
        "i believe",
        "i know",
        "clearly",
        "certainly",
        "definitely",
        "without doubt",
        "convinced",
    ],
    Confidence.CONVICTION: [
        "absolutely",
        "undeniably",
        "truth is",
        "i am certain",
        "this is",
        "fundamental",
        "non-negotiable",
    ],
}
"""Mapping of confidence levels to keyword signals."""


def _count_keyword_matches(text: str, keywords: list[str]) -> int:
    """Count word-boundary keyword matches in text.

    Uses ``re.findall`` with word boundaries to prevent partial-word
    matches (e.g. 'power' won't match inside 'empower').

    Args:
        text: The text to search (should already be lowercased).
        keywords: List of keywords to look for.

    Returns:
        Total number of keyword matches found.
    """
    count = 0
    for keyword in keywords:
        escaped = re.escape(keyword)
        count += len(re.findall(rf"\b{escaped}\b", text, re.IGNORECASE))
    return count


def _split_content_sections(
    content: str,
) -> tuple[str, str]:
    """Split content into first paragraph and body.

    The first paragraph is everything before the first blank line
    (``\\n\\n``).  The body is everything after.

    Args:
        content: Full content string.

    Returns:
        Tuple of (first_paragraph, body).
    """
    parts = content.split("\n\n", maxsplit=1)
    first_para = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    return first_para, body


class RuleClassifier:
    """Keyword-based classifier for Creek fragments.

    Scans fragment content for signal words and updates frequency,
    wavelength phase, engagement mode, voice register, and confidence
    classifications.  Uses word-boundary matching with positional
    weighting (title 3x, first paragraph 2x, body 1x).

    Class Attributes:
        PRIMARY_THRESHOLD: Minimum score for primary frequency assignment.
        SECONDARY_THRESHOLD: Minimum score for secondary frequency.
        TITLE_WEIGHT: Score multiplier for title matches.
        FIRST_PARA_WEIGHT: Score multiplier for first-paragraph matches.
        BODY_WEIGHT: Score multiplier for body matches.
        CONFIDENCE_MATCH_WEIGHT: Weight for match component in confidence score.
        CONFIDENCE_DIMENSION_WEIGHT: Weight for dimension component.
        CONFIDENCE_GAP_WEIGHT: Weight for gap component.
    """

    PRIMARY_THRESHOLD: int = 3
    SECONDARY_THRESHOLD: int = 2
    TITLE_WEIGHT: int = 3
    FIRST_PARA_WEIGHT: int = 2
    BODY_WEIGHT: int = 1
    CONFIDENCE_MATCH_WEIGHT: float = 0.4
    CONFIDENCE_DIMENSION_WEIGHT: float = 0.3
    CONFIDENCE_GAP_WEIGHT: float = 0.3

    def classify(
        self,
        fragment: Fragment,
        content: str = "",
        title: str = "",
    ) -> Fragment:
        """Apply keyword/pattern matching to classify a fragment.

        Scans the provided content and title for keywords associated
        with each frequency, phase, mode, voice register, and confidence
        level.  Uses scoring with positional weighting.

        The verdict is **merged, not assigned**: a dimension the
        matchers said nothing about keeps whatever the fragment already
        carried, because keyword silence is an absence of evidence and
        not evidence of absence. See :meth:`_layer_over_fragment` for
        the rule, including why frequency alone is replaced wholesale.

        This matters beyond bookkeeping. The ``voice.confidence`` the
        old wholesale assignment destroyed is half of the
        ``confessional + conviction`` INTIMATE trigger that
        :class:`~creek.classify.privacy.PrivacyClassifier` reads in
        ``_is_high_confidence_confessional``
        (``creek/classify/privacy.py:205-214``), so on a confessional
        body this pass used to supply one half of the trigger and erase
        the other in the same breath — a missed escalation (fail-open),
        not merely a lossy one (#1331).

        Args:
            fragment: The fragment to classify.
            content: The markdown body text to scan for keywords.
            title: Optional title text (weighted 3x).  Falls back to
                ``fragment.title`` if empty.

        Returns:
            A new Fragment with updated classification fields.
        """
        if not title:
            title = fragment.title

        first_para, body = _split_content_sections(content)

        freq_scores = self._score_signals(
            title,
            first_para,
            body,
            FREQUENCY_SIGNALS,
        )
        phase_scores = self._score_signals(
            title,
            first_para,
            body,
            WAVELENGTH_PHASE_SIGNALS,
        )
        mode_scores = self._score_signals(
            title,
            first_para,
            body,
            MODE_SIGNALS,
        )

        primary, secondaries = self._pick_primary_secondary(freq_scores)
        phase = self._pick_phase(phase_scores)
        mode = self._pick_mode(mode_scores)
        voice_register = self._match_voice_register(
            title,
            first_para,
            body,
        )
        confidence = self._match_confidence(title, first_para, body)

        updates = self._build_updates(
            primary,
            secondaries,
            phase,
            mode,
            voice_register,
            confidence,
        )

        if updates:
            return fragment.model_copy(
                update=self._layer_over_fragment(fragment, updates),
            )

        return fragment.model_copy()

    def confidence_score(
        self,
        fragment: Fragment,
        content: str = "",
        title: str = "",
    ) -> float:
        """Return a 0.0-1.0 confidence score for rule-based classification.

        Score is based on: number of matching keywords, spread across
        classification dimensions, and gap between primary and secondary
        frequency scores.

        Args:
            fragment: The fragment to evaluate.
            content: The markdown body text to scan.
            title: Optional title text.

        Returns:
            A float between 0.0 and 1.0.
        """
        if not title:
            title = fragment.title

        first_para, body = _split_content_sections(content)

        freq_scores = self._score_signals(
            title,
            first_para,
            body,
            FREQUENCY_SIGNALS,
        )
        phase_scores = self._score_signals(
            title,
            first_para,
            body,
            WAVELENGTH_PHASE_SIGNALS,
        )
        mode_scores = self._score_signals(
            title,
            first_para,
            body,
            MODE_SIGNALS,
        )

        dimensions_matched = 0
        max_freq = max(freq_scores.values()) if freq_scores else 0
        max_phase = max(phase_scores.values()) if phase_scores else 0
        max_mode = max(mode_scores.values()) if mode_scores else 0

        if max_freq >= self.PRIMARY_THRESHOLD:
            dimensions_matched += 1
        if max_phase >= self.SECONDARY_THRESHOLD:
            dimensions_matched += 1
        if max_mode >= self.SECONDARY_THRESHOLD:
            dimensions_matched += 1

        # Gap between top two frequency scores
        sorted_freq = sorted(freq_scores.values(), reverse=True)
        gap = 0
        if len(sorted_freq) >= 2:
            gap = sorted_freq[0] - sorted_freq[1]

        # Normalize components
        total_matches = max_freq + max_phase + max_mode
        match_score = min(total_matches / 15.0, 1.0)
        dimension_score = dimensions_matched / 3.0
        gap_score = min(gap / 5.0, 1.0)

        return round(
            self.CONFIDENCE_MATCH_WEIGHT * match_score
            + self.CONFIDENCE_DIMENSION_WEIGHT * dimension_score
            + self.CONFIDENCE_GAP_WEIGHT * gap_score,
            3,
        )

    def _score_signals(
        self,
        title: str,
        first_para: str,
        body: str,
        signals: dict[_EnumT, list[str]],
    ) -> dict[_EnumT, int]:
        """Score each signal category by weighted keyword matches.

        Args:
            title: Title text (weighted by ``TITLE_WEIGHT``).
            first_para: First paragraph text (weighted by
                ``FIRST_PARA_WEIGHT``).
            body: Remaining body text (weighted by ``BODY_WEIGHT``).
            signals: Mapping of category to keyword list.

        Returns:
            Dictionary mapping each category to its total score.
        """
        scores: dict[_EnumT, int] = {}
        for category, keywords in signals.items():
            score = (
                _count_keyword_matches(title, keywords) * self.TITLE_WEIGHT
                + _count_keyword_matches(first_para, keywords) * self.FIRST_PARA_WEIGHT
                + _count_keyword_matches(body, keywords) * self.BODY_WEIGHT
            )
            scores[category] = score
        return scores

    def _pick_primary_secondary(
        self,
        scores: dict[Frequency, int],
    ) -> tuple[Frequency, list[Frequency]]:
        """Pick primary and secondary frequencies from scores.

        Args:
            scores: Mapping of frequency to score.

        Returns:
            Tuple of (primary_frequency, secondary_frequencies).
        """
        sorted_items = sorted(
            scores.items(),
            key=operator.itemgetter(1),
            reverse=True,
        )

        primary = Frequency.UNCLASSIFIED
        secondaries: list[Frequency] = []

        for i, (freq, score) in enumerate(sorted_items):
            if i == 0 and score >= self.PRIMARY_THRESHOLD:
                primary = freq
            elif score >= self.SECONDARY_THRESHOLD:
                secondaries.append(freq)

        return primary, secondaries

    def _pick_phase(
        self,
        scores: dict[Phase, int],
    ) -> Phase:
        """Pick the highest-scoring phase if above threshold.

        Args:
            scores: Mapping of phase to score.

        Returns:
            The best Phase, or ``Phase.UNCLASSIFIED`` if none meets threshold.
        """
        if not scores:
            return Phase.UNCLASSIFIED

        best_key, best_score = max(scores.items(), key=operator.itemgetter(1))
        if best_score >= self.SECONDARY_THRESHOLD:
            return best_key
        return Phase.UNCLASSIFIED

    def _pick_mode(
        self,
        scores: dict[Mode, int],
    ) -> Mode:
        """Pick the highest-scoring mode if above threshold.

        Args:
            scores: Mapping of mode to score.

        Returns:
            The best Mode, or ``Mode.UNCLASSIFIED`` if none meets threshold.
        """
        if not scores:
            return Mode.UNCLASSIFIED

        best_key, best_score = max(scores.items(), key=operator.itemgetter(1))
        if best_score >= self.SECONDARY_THRESHOLD:
            return best_key
        return Mode.UNCLASSIFIED

    def _build_updates(
        self,
        primary: Frequency,
        secondaries: list[Frequency],
        phase: Phase,
        mode: Mode,
        voice_register: VoiceRegister | None,
        confidence: Confidence | None,
    ) -> dict[str, object]:
        """Build the sparse verdict for what the matchers determined.

        A **pure function of the match results**: it never sees the
        fragment, and it only names blocks whose classification produced
        a result above the configured thresholds. Within a named block,
        every axis the matchers were silent about is left at that
        field's "not determined" default sentinel — the wavelength block
        carries no orientation, dosage, colour or descriptor, and the
        voice block carries ``None`` for whichever of its two axes did
        not match.

        That makes the result **unsafe to assign wholesale**. Passing it
        straight to ``fragment.model_copy(update=...)`` replaces each
        whole block, so those untouched axes overwrite the fragment's
        prior evidence with sentinels — including the
        ``voice.confidence`` the INTIMATE privacy escalation reads
        (#1331). Callers must route it through
        :meth:`_layer_over_fragment` first.

        The split mirrors the weighted path, where
        :meth:`~creek.classify.weighted.WeightedFragmentClassification.to_legacy`
        is a documented pure collapse and
        :meth:`~creek.classify.weighted.WeightedFragmentClassification.merge_onto`
        is the separate layering step (#1309).

        Args:
            primary: Primary frequency.
            secondaries: Secondary frequencies.
            phase: Wavelength phase.
            mode: Engagement mode.
            voice_register: Matched voice register (or None).
            confidence: Matched confidence level (or None).

        Returns:
            Dictionary of field names to the values the matchers
            determined, ready for :meth:`_layer_over_fragment`.
        """
        updates: dict[str, object] = {}

        if primary != Frequency.UNCLASSIFIED:
            updates["frequency"] = FrequencyClassification(
                primary=primary,
                secondary=secondaries,
            )

        if phase != Phase.UNCLASSIFIED or mode != Mode.UNCLASSIFIED:
            updates["wavelength"] = WavelengthClassification(
                phase=phase,
                mode=mode,
            )

        if voice_register is not None or confidence is not None:
            updates["voice"] = VoiceClassification(
                voice_register=voice_register,
                confidence=confidence,
            )

        return updates

    @staticmethod
    def _layer_over_fragment(
        fragment: Fragment,
        updates: dict[str, object],
    ) -> dict[str, object]:
        """Layer a sparse verdict over the evidence *fragment* already holds.

        Turns the unsafe-to-assign output of :meth:`_build_updates` into
        an update dict that is safe to hand to
        ``fragment.model_copy(update=...)``: the ``wavelength`` and
        ``voice`` entries are replaced by the fragment's own blocks with
        only the determined axes overlaid, via
        :func:`~creek.classify.evidence.layer_determined_over`. An axis
        the matchers said nothing about keeps whatever was on record.

        THE FREQUENCY ASYMMETRY: ``frequency`` is deliberately **not**
        layered. :attr:`~creek.models.FrequencyClassification.secondary`
        is a list, and a list cannot be merged field-wise the way the
        scalar wavelength and voice axes can — so a freshly picked
        primary replaces the block wholesale, clearing stale secondaries
        from an earlier verdict rather than accumulating them. When no
        primary clears the threshold the block is absent from *updates*
        entirely and is left completely alone. This matches
        :meth:`~creek.classify.weighted.WeightedFragmentClassification.merge_onto`
        on purpose: two divergent merge semantics for one problem is how
        this defect came to be filed twice (#1331 and #1400).

        Args:
            fragment: The fragment being classified, carrying whatever
                evidence a previous run or the operator established.
            updates: The sparse verdict from :meth:`_build_updates`. Not
                mutated.

        Returns:
            A copy of *updates* whose classification blocks are merged
            onto the fragment's rather than replacing them.
        """
        layered = updates.copy()

        wavelength = layered.get("wavelength")
        if isinstance(wavelength, WavelengthClassification):
            layered["wavelength"] = layer_determined_over(
                prior=fragment.wavelength,
                determined=wavelength,
            )

        voice = layered.get("voice")
        if isinstance(voice, VoiceClassification):
            layered["voice"] = layer_determined_over(
                prior=fragment.voice,
                determined=voice,
            )

        return layered

    def _match_voice_register(
        self,
        title: str,
        first_para: str,
        body: str,
    ) -> VoiceRegister | None:
        """Match content against voice register signal keywords.

        Args:
            title: Title text.
            first_para: First paragraph text.
            body: Remaining body text.

        Returns:
            The best matching VoiceRegister, or None if none match.
        """
        scores = self._score_signals(
            title,
            first_para,
            body,
            VOICE_REGISTER_SIGNALS,
        )
        best_key, best_score = max(scores.items(), key=operator.itemgetter(1))
        if best_score >= self.SECONDARY_THRESHOLD:
            return best_key
        return None

    def _match_confidence(
        self,
        title: str,
        first_para: str,
        body: str,
    ) -> Confidence | None:
        """Match content against confidence signal keywords.

        Args:
            title: Title text.
            first_para: First paragraph text.
            body: Remaining body text.

        Returns:
            The best matching Confidence, or None if none match.
        """
        scores = self._score_signals(
            title,
            first_para,
            body,
            CONFIDENCE_SIGNALS,
        )
        best_key, best_score = max(scores.items(), key=operator.itemgetter(1))
        if best_score >= self.SECONDARY_THRESHOLD:
            return best_key
        return None
