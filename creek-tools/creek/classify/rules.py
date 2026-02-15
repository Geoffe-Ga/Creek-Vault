"""Rule-based classification using keyword/pattern matching.

Provides a simple keyword-matching classifier that scans fragment content
for signal words associated with APTITUDE frequencies, wavelength phases,
and engagement modes. This is intended as a fast first-pass classifier
before optional LLM refinement.
"""

import logging

from creek.models import (
    Fragment,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    WavelengthClassification,
)

logger = logging.getLogger(__name__)

# ---- Signal Dictionaries ----

FREQUENCY_SIGNALS: dict[Frequency, list[str]] = {
    Frequency.F1: ["survival", "safety", "security", "threat"],
    Frequency.F3: ["power", "ambition", "drive", "dominance"],
    Frequency.F5: ["achievement", "strategy", "success", "goal"],
    Frequency.F7: ["systems", "patterns", "complexity", "integrate"],
    Frequency.F8: ["community", "ecology", "holistic", "collective"],
}
"""Stub mapping of APTITUDE frequencies to keyword signals.

Only F1, F3, F5, F7, F8 are covered in this stub. The remaining
frequencies (F2, F4, F6, F9, F10) will be added when the full
classification rule set is implemented in later issues.
"""

WAVELENGTH_PHASE_SIGNALS: dict[Phase, list[str]] = {
    Phase.RISING: ["emerging", "building", "growing", "momentum"],
    Phase.PEAKING: ["peak", "climax", "intense", "maximum"],
    Phase.WITHDRAWAL: ["retreating", "pulling back", "withdrawing", "receding"],
}
"""Mapping of wavelength phases to their keyword signals."""

MODE_SIGNALS: dict[Mode, list[str]] = {
    Mode.INHABIT: ["dwelling", "immersed", "inhabiting", "living in"],
    Mode.EXPRESS: ["expressing", "creating", "articulating", "voicing"],
    Mode.ABSORB: ["absorbing", "receiving", "taking in", "learning"],
}
"""Mapping of engagement modes to their keyword signals."""


class RuleClassifier:
    """Keyword-based classifier for Creek fragments.

    Scans fragment content for signal words and updates frequency,
    wavelength phase, and engagement mode classifications accordingly.
    Uses simple case-insensitive substring matching.
    """

    def classify(self, fragment: Fragment, content: str = "") -> Fragment:
        """Apply keyword/pattern matching to classify a fragment.

        Scans the provided content string for keywords associated with
        each frequency, phase, and mode. The first match wins for each
        classification dimension. The original fragment is not mutated.

        Args:
            fragment: The fragment to classify.
            content: The markdown body text to scan for keywords.

        Returns:
            A new Fragment with updated classification fields.
        """
        content_lower = content.lower()

        frequency = self._match_frequency(content_lower)
        phase = self._match_phase(content_lower)
        mode = self._match_mode(content_lower)

        updates: dict[str, object] = {}

        if frequency != Frequency.UNCLASSIFIED:
            updates["frequency"] = FrequencyClassification(primary=frequency)
            logger.info("Rule classifier matched frequency %s", frequency)

        if phase != Phase.UNCLASSIFIED or mode != Mode.UNCLASSIFIED:
            wl_phase = phase if phase != Phase.UNCLASSIFIED else Phase.UNCLASSIFIED
            wl_mode = mode if mode != Mode.UNCLASSIFIED else Mode.UNCLASSIFIED
            if phase != Phase.UNCLASSIFIED:
                logger.info("Rule classifier matched phase %s", phase)
            if mode != Mode.UNCLASSIFIED:
                logger.info("Rule classifier matched mode %s", mode)
            updates["wavelength"] = WavelengthClassification(
                phase=wl_phase,
                mode=wl_mode,
            )

        if updates:
            return fragment.model_copy(update=updates)

        return fragment.model_copy()

    def _match_frequency(self, content_lower: str) -> Frequency:
        """Match content against frequency signal keywords.

        Args:
            content_lower: Lowercased content string to scan.

        Returns:
            The first matching Frequency, or UNCLASSIFIED if none match.
        """
        for freq, keywords in FREQUENCY_SIGNALS.items():
            for keyword in keywords:
                if keyword in content_lower:
                    return freq
        return Frequency.UNCLASSIFIED

    def _match_phase(self, content_lower: str) -> Phase:
        """Match content against wavelength phase signal keywords.

        Args:
            content_lower: Lowercased content string to scan.

        Returns:
            The first matching Phase, or UNCLASSIFIED if none match.
        """
        for phase, keywords in WAVELENGTH_PHASE_SIGNALS.items():
            for keyword in keywords:
                if keyword in content_lower:
                    return phase
        return Phase.UNCLASSIFIED

    def _match_mode(self, content_lower: str) -> Mode:
        """Match content against engagement mode signal keywords.

        Args:
            content_lower: Lowercased content string to scan.

        Returns:
            The first matching Mode, or UNCLASSIFIED if none match.
        """
        for mode, keywords in MODE_SIGNALS.items():
            for keyword in keywords:
                if keyword in content_lower:
                    return mode
        return Mode.UNCLASSIFIED
