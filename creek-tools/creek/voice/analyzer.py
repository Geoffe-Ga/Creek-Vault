"""Voice analysis pipeline — collect exemplars, extract patterns, generate profiles.

Analyzes classified fragments to extract voice patterns per register,
collecting exemplar passages from high-confidence fragments and generating
profile notes with voice proxy prompts for LLM instruction.
"""

import logging
import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from creek.models import Confidence, Fragment

logger = logging.getLogger(__name__)

# Confidence levels considered high enough for exemplar collection.
_EXEMPLAR_CONFIDENCE: frozenset[str] = frozenset(
    {Confidence.SETTLED, Confidence.CONVICTION}
)

# ---- Pattern Extraction Helpers ----

_SENTENCE_END = re.compile(r"[.!?]+\s+")
_EM_DASH = re.compile(r"\u2014|--")
_ELLIPSIS = re.compile(r"\.{3}|\u2026")
_PARENTHETICAL = re.compile(r"\([^)]+\)")


def _average_sentence_length(text: str) -> float:
    """Compute average sentence length in words.

    Args:
        text: Plain text content to analyze.

    Returns:
        Average number of words per sentence, or 0.0 if no sentences found.
    """
    sentences = _SENTENCE_END.split(text.strip())
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return 0.0
    total_words = sum(len(s.split()) for s in sentences)
    return round(total_words / len(sentences), 1)


def _count_pattern(text: str, pattern: re.Pattern[str]) -> int:
    """Count regex pattern occurrences in text.

    Args:
        text: Text to search.
        pattern: Compiled regex pattern.

    Returns:
        Number of matches found.
    """
    return len(pattern.findall(text))


def _extract_distinctive_words(
    texts: list[str],
    top_n: int = 20,
) -> list[str]:
    """Extract the most frequent non-stopword tokens across texts.

    Args:
        texts: List of text content to analyze.
        top_n: Number of top words to return.

    Returns:
        List of the most frequent distinctive words.
    """
    stop_words = frozenset(
        {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "is",
            "it",
            "was",
            "are",
            "be",
            "has",
            "had",
            "do",
            "did",
            "not",
            "this",
            "that",
            "from",
            "as",
            "i",
            "you",
            "he",
            "she",
            "we",
            "they",
            "my",
            "your",
            "his",
            "her",
            "our",
            "its",
        }
    )
    counter: Counter[str] = Counter()
    for text in texts:
        words = re.findall(r"\b[a-z]{3,}\b", text.lower())
        counter.update(w for w in words if w not in stop_words)
    return [word for word, _ in counter.most_common(top_n)]


# ---- Data Models ----


class VoicePattern(BaseModel):
    """Extracted voice patterns for a single register.

    Attributes:
        avg_sentence_length: Average sentence length in words.
        em_dash_frequency: Average em-dash count per exemplar.
        ellipsis_frequency: Average ellipsis count per exemplar.
        parenthetical_frequency: Average parenthetical aside count per exemplar.
        distinctive_words: Most frequent distinctive vocabulary.
        exemplar_count: Number of high-confidence exemplars analyzed.
    """

    avg_sentence_length: float = 0.0
    em_dash_frequency: float = 0.0
    ellipsis_frequency: float = 0.0
    parenthetical_frequency: float = 0.0
    distinctive_words: list[str] = Field(default_factory=list)
    exemplar_count: int = 0


class VoiceProfile(BaseModel):
    """Complete voice profile for a single register.

    Attributes:
        voice_register_name: The voice register this profile describes.
        pattern: Extracted structural and vocabulary patterns.
        exemplar_titles: Titles of fragments used as exemplars.
    """

    voice_register_name: str
    pattern: VoicePattern = Field(default_factory=VoicePattern)
    exemplar_titles: list[str] = Field(default_factory=list)


# ---- Main Analyzer ----


class VoiceAnalyzer:
    """Analyze fragments to extract voice patterns and generate register profiles.

    Collects high-confidence exemplars grouped by voice register, extracts
    structural patterns (sentence length, punctuation habits, vocabulary),
    and generates profile notes as markdown files in the vault.

    Attributes:
        vault_path: Path to the Obsidian vault root.
    """

    def __init__(self, vault_path: Path) -> None:
        """Initialize the VoiceAnalyzer with a vault path.

        Args:
            vault_path: Path to the root of the Obsidian vault directory.
        """
        self.vault_path = vault_path

    def collect_exemplars(
        self,
        fragments: list[Fragment],
    ) -> dict[str, list[Fragment]]:
        """Group high-confidence fragments by voice register.

        Filters fragments to those with confidence >= settled and a
        non-None voice register, then groups them by register name.

        Args:
            fragments: All classified fragments to filter.

        Returns:
            Dictionary mapping register name to list of exemplar fragments.
        """
        grouped: dict[str, list[Fragment]] = {}
        for frag in fragments:
            if frag.voice.confidence not in _EXEMPLAR_CONFIDENCE:
                continue
            if frag.voice.voice_register is None:
                continue
            register = str(frag.voice.voice_register)
            grouped.setdefault(register, []).append(frag)

        logger.info(
            "Collected exemplars: %s",
            {k: len(v) for k, v in grouped.items()},
        )
        return grouped

    def extract_patterns(
        self,
        content_by_register: dict[str, list[str]],
    ) -> dict[str, VoicePattern]:
        """Extract voice patterns from content grouped by register.

        Analyzes sentence structure, punctuation habits, and vocabulary
        for each register's exemplar content.

        Args:
            content_by_register: Mapping of register name to list of
                text content strings for that register's exemplars.

        Returns:
            Dictionary mapping register name to extracted VoicePattern.
        """
        patterns: dict[str, VoicePattern] = {}
        for register, texts in content_by_register.items():
            if not texts:
                patterns[register] = VoicePattern()
                continue

            avg_lengths = [_average_sentence_length(t) for t in texts]
            avg_len = 0.0
            if avg_lengths:
                avg_len = round(sum(avg_lengths) / len(avg_lengths), 1)

            em_dashes = [_count_pattern(t, _EM_DASH) for t in texts]
            ellipses = [_count_pattern(t, _ELLIPSIS) for t in texts]
            parens = [_count_pattern(t, _PARENTHETICAL) for t in texts]

            n = len(texts)
            patterns[register] = VoicePattern(
                avg_sentence_length=avg_len,
                em_dash_frequency=round(sum(em_dashes) / n, 1),
                ellipsis_frequency=round(sum(ellipses) / n, 1),
                parenthetical_frequency=round(sum(parens) / n, 1),
                distinctive_words=_extract_distinctive_words(texts),
                exemplar_count=n,
            )

        return patterns

    def build_profiles(
        self,
        fragments: list[Fragment],
        content_map: dict[str, str],
    ) -> list[VoiceProfile]:
        """Build voice profiles from fragments and their content.

        Combines exemplar collection and pattern extraction into
        complete VoiceProfile objects for each register found.

        Args:
            fragments: All classified fragments.
            content_map: Mapping of fragment ID to text content.

        Returns:
            List of VoiceProfile objects, one per register found.
        """
        exemplars = self.collect_exemplars(fragments)
        content_by_register: dict[str, list[str]] = {}
        titles_by_register: dict[str, list[str]] = {}

        for register, frags in exemplars.items():
            texts: list[str] = []
            titles: list[str] = []
            for frag in frags:
                text = content_map.get(frag.id, "")
                if text:
                    texts.append(text)
                titles.append(frag.title)
            content_by_register[register] = texts
            titles_by_register[register] = titles

        patterns = self.extract_patterns(content_by_register)
        profiles: list[VoiceProfile] = []
        for register, pattern in patterns.items():
            profiles.append(
                VoiceProfile(
                    voice_register_name=register,
                    pattern=pattern,
                    exemplar_titles=titles_by_register.get(register, []),
                )
            )

        logger.info("Built %d voice profiles", len(profiles))
        return profiles

    def generate_profile_notes(
        self,
        profiles: list[VoiceProfile],
    ) -> list[Path]:
        """Write voice profile markdown notes to the vault.

        Generates one markdown file per profile in ``07-Voice/``,
        each containing a voice proxy prompt template with extracted
        patterns and exemplar references.

        Args:
            profiles: List of VoiceProfile objects to write.

        Returns:
            List of paths to the generated profile note files.
        """
        voice_dir = self.vault_path / "07-Voice"
        voice_dir.mkdir(parents=True, exist_ok=True)

        generated: list[Path] = []
        for profile in profiles:
            content = self._build_profile_markdown(profile)
            filename = f"Voice-Profile-{profile.voice_register_name}.md"
            note_path = voice_dir / filename
            note_path.write_text(content, encoding="utf-8")
            generated.append(note_path)

        logger.info("Generated %d voice profile notes", len(generated))
        return generated

    def _build_profile_markdown(self, profile: VoiceProfile) -> str:
        """Build markdown content for a voice profile note.

        Args:
            profile: The VoiceProfile to render as markdown.

        Returns:
            Complete markdown string with YAML frontmatter and body.
        """
        p = profile.pattern
        lines = [
            "---",
            "type: voice-profile",
            f"register: {profile.voice_register_name}",
            f"exemplar_count: {p.exemplar_count}",
            "---",
            "",
            f"# Voice Proxy: {profile.voice_register_name.title()}",
            "",
            "## Prompt for LLM",
            "",
            "Write in the voice of a Liminal Trickster Mystic "
            "with the following characteristics:",
            "",
            "### Structural Patterns",
            "",
            f"- Average sentence length: {p.avg_sentence_length} words",
            f"- Em-dash frequency: {p.em_dash_frequency} per passage",
            f"- Ellipsis frequency: {p.ellipsis_frequency} per passage",
            f"- Parenthetical asides: {p.parenthetical_frequency} per passage",
            "",
            "### Vocabulary Fingerprint",
            "",
        ]

        if p.distinctive_words:
            lines.append(", ".join(p.distinctive_words))
        else:
            lines.append("*No distinctive vocabulary extracted yet.*")

        lines.extend(
            [
                "",
                "### Exemplar Passages",
                "",
            ]
        )

        if profile.exemplar_titles:
            for title in profile.exemplar_titles[:10]:
                lines.append(f"- [[{title}]]")
        else:
            lines.append("*No exemplars collected yet.*")

        lines.extend(
            [
                "",
                "### Anti-Patterns (DO NOT)",
                "",
                "*Anti-patterns will be populated after deeper analysis.*",
                "",
            ]
        )

        return "\n".join(lines)

    def run(
        self,
        fragments: list[Fragment],
        content_map: dict[str, str],
    ) -> list[Path]:
        """Execute the full voice analysis pipeline.

        Collects exemplars, extracts patterns, builds profiles,
        and generates profile notes in the vault.

        Args:
            fragments: All classified fragments.
            content_map: Mapping of fragment ID to text content.

        Returns:
            List of paths to generated voice profile notes.
        """
        profiles = self.build_profiles(fragments, content_map)
        return self.generate_profile_notes(profiles)
