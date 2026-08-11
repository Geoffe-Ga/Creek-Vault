"""Lexicon generation — coined terms, metaphors, phrases, borrowings.

Implements Section 11.3 of the Creek Ontology: build a living glossary
under ``07-Voice/Lexicon/`` containing four cross-linked inventories:

* **Coined terms** — words created by the human voice that do not appear
  in the reference word set and recur at least
  :attr:`LexiconGenerator.min_coinage_occurrences` times. Detection uses
  the same reference-word-absence heuristic as
  :class:`creek.generate.voice.VoicePatternExtractor`, so callers who
  want NLTK-style coverage can supply their own reference frozenset.
* **Recurring metaphors** — each metaphor family detected in
  :class:`VoicePatterns` is rehydrated with fragment-linked usages so
  the glossary entry points back to the original prose.
* **Distinctive phrases** — n-grams (2- to 4-word windows by default)
  with counts ≥ :attr:`LexiconGenerator.min_phrase_occurrences`.
* **Borrowed terms** — tokens matching a tradition-specific glossary
  (Buddhist, recovery, Spiral Dynamics, Jungian) with usage counts and
  example contexts, capturing "the human's particular spin" by
  preserving the local sentence.

The module is deliberately self-contained: it does not add heavy NLP
dependencies (NLTK, scikit-learn). Sentence splitting, tokenization,
and n-gram counting are implemented with ``re`` and ``collections``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter

from creek.classify.privacy_filter import PrivacyTierOverride

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.generate.voice import Exemplar, MetaphorFamily, VoicePatterns
    from creek.models import Fragment

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


TRADITION_GLOSSARIES: dict[str, frozenset[str]] = {
    "buddhist": frozenset(
        {
            "dharma",
            "sangha",
            "anatta",
            "sunyata",
            "metta",
            "samsara",
            "nirvana",
            "bodhisattva",
            "karma",
            "zazen",
            "satori",
            "koan",
            "dukkha",
        },
    ),
    "recovery": frozenset(
        {
            "sobriety",
            "amends",
            "sponsor",
            "sponsee",
            "resentment",
            "surrender",
            "powerlessness",
            "step",
            "steps",
            "fellowship",
            "meeting",
            "serenity",
            "higher",
        },
    ),
    "spiral_dynamics": frozenset(
        {
            "beige",
            "purple",
            "meme",
            "memes",
            "memetic",
            "tier",
            "yellow",
            "turquoise",
            "integral",
            "altitude",
            "vmeme",
        },
    ),
    "jungian": frozenset(
        {
            "shadow",
            "anima",
            "animus",
            "individuation",
            "archetype",
            "archetypes",
            "persona",
            "collective",
            "synchronicity",
            "numinous",
        },
    ),
}
"""Default keyword sets for detecting terms borrowed from named traditions."""


_LEXICON_SUBPATH: tuple[str, str] = ("07-Voice", "Lexicon")
"""Vault subpath where the glossary and metaphor notes are persisted."""

_METAPHORS_SUBDIR: str = "Metaphors"
"""Subdirectory under ``07-Voice/Lexicon/`` for per-domain metaphor notes."""

_GLOSSARY_FILENAME: str = "glossary.md"
"""Filename for the master glossary note."""

_DEFAULT_MIN_COINAGE_OCCURRENCES: int = 3
"""Minimum occurrences for a candidate to be recorded as a coinage."""

_DEFAULT_MIN_PHRASE_OCCURRENCES: int = 2
"""Minimum occurrences for an n-gram to count as a distinctive phrase."""

_DEFAULT_TOP_PHRASES: int = 20
"""Upper bound on the number of distinctive phrases returned."""

_DEFAULT_NGRAM_RANGE: tuple[int, int] = (2, 4)
"""Inclusive ``(min_n, max_n)`` window for distinctive-phrase extraction."""

_SENTENCE_SPLIT_RE: re.Pattern[str] = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\"'\u201c])",
)
"""Mirror of the voice module's heuristic split on terminal punctuation."""

_WORD_RE: re.Pattern[str] = re.compile(r"[a-zA-Z]+")
"""Alphabetic token extractor; discards digits and symbols."""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LexiconContext:
    """A single usage of a lexicon term, linked back to its fragment.

    Attributes:
        fragment_id: Identifier of the fragment where the usage occurs.
        sentence: The surrounding sentence containing the term.
    """

    fragment_id: str
    sentence: str


@dataclass(frozen=True)
class CoinedTermEntry:
    """A word coined by the voice: absent from the reference set.

    Attributes:
        term: The coined token, lowercased.
        count: Total occurrences across the supplied exemplars.
        contexts: Example usages, one per distinct fragment sentence.
    """

    term: str
    count: int
    contexts: tuple[LexiconContext, ...]


@dataclass(frozen=True)
class MetaphorInventory:
    """A metaphor family rehydrated with fragment-linked usages.

    Attributes:
        domain: Metaphor domain (``"water"``, ``"light"``, ``"fire"``, ...).
        keywords: Domain keywords that matched the corpus.
        usages: Example usages with fragment id and surrounding sentence.
    """

    domain: str
    keywords: tuple[str, ...]
    usages: tuple[LexiconContext, ...]


@dataclass(frozen=True)
class DistinctivePhraseEntry:
    """An n-gram appearing across the corpus with high relative frequency.

    Attributes:
        phrase: Lowercased whitespace-joined n-gram.
        count: Occurrences across all exemplars.
    """

    phrase: str
    count: int


@dataclass(frozen=True)
class BorrowedTermEntry:
    """A term borrowed from a specific tradition with the voice's spin.

    Attributes:
        term: The borrowed token, lowercased.
        tradition: Name of the originating tradition glossary.
        count: Total occurrences across the supplied exemplars.
        contexts: Example usages, one per distinct fragment sentence,
            preserving the surrounding prose so the voice's particular
            spin is visible.
    """

    term: str
    tradition: str
    count: int
    contexts: tuple[LexiconContext, ...]


@dataclass(frozen=True)
class Lexicon:
    """The complete generated glossary for a voice.

    Attributes:
        coined_terms: Coinages with counts and example contexts.
        metaphors: Metaphor inventories, one per detected domain.
        distinctive_phrases: Recurring n-grams with counts.
        borrowed_terms: Terms borrowed from named traditions.
    """

    coined_terms: tuple[CoinedTermEntry, ...]
    metaphors: tuple[MetaphorInventory, ...]
    distinctive_phrases: tuple[DistinctivePhraseEntry, ...]
    borrowed_terms: tuple[BorrowedTermEntry, ...]


# ---------------------------------------------------------------------------
# Tokenisation helpers
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Split *text* into sentences on terminal punctuation."""
    stripped = text.strip()
    if not stripped:
        return []
    parts = _SENTENCE_SPLIT_RE.split(stripped)
    return [s.strip() for s in parts if s.strip()]


def _tokenize_words(text: str) -> list[str]:
    """Return lowercase alphabetic tokens from *text*."""
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]


def _fragment_id(fragment: Fragment) -> str:
    """Return a fragment's identifier as a plain string."""
    return fragment.id


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _iter_exemplar_sentences(
    exemplars: list[Exemplar],
) -> list[tuple[str, str]]:
    """Flatten *exemplars* into ``(fragment_id, sentence)`` pairs."""
    pairs: list[tuple[str, str]] = []
    for exemplar in exemplars:
        frag_id = _fragment_id(exemplar.fragment)
        for sentence in _split_sentences(exemplar.body):
            pairs.append((frag_id, sentence))
    return pairs


def _count_word_in_sentence(sentence: str, word: str) -> int:
    """Count case-insensitive whole-word matches of *word* in *sentence*."""
    pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
    return len(pattern.findall(sentence))


def _gather_term_matches(
    pairs: list[tuple[str, str]],
    predicate: Callable[[str], bool],
) -> dict[str, tuple[int, list[LexiconContext]]]:
    """Collect counts and unique contexts for tokens matching *predicate*.

    Args:
        pairs: ``(fragment_id, sentence)`` pairs flattened from exemplars.
        predicate: Called with a lowercased token; truthy values are
            retained as lexicon candidates.

    Returns:
        Mapping of candidate token to ``(count, contexts)`` where
        ``contexts`` preserves insertion order and is de-duplicated on
        ``(fragment_id, sentence)``.
    """
    buckets: dict[str, tuple[int, list[LexiconContext]]] = {}
    seen_contexts: dict[str, set[tuple[str, str]]] = {}
    for frag_id, sentence in pairs:
        for token in _tokenize_words(sentence):
            if not predicate(token):
                continue
            count, contexts = buckets.get(token, (0, []))
            seen = seen_contexts.setdefault(token, set())
            key = (frag_id, sentence)
            if key not in seen:
                contexts.append(
                    LexiconContext(fragment_id=frag_id, sentence=sentence),
                )
                seen.add(key)
            buckets[token] = (count + 1, contexts)
    return buckets


def _extract_ngrams(tokens: list[str], n: int) -> list[str]:
    """Return whitespace-joined ``n``-grams from *tokens*."""
    if n <= 0 or len(tokens) < n:
        return []
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _count_phrases(
    exemplars: list[Exemplar],
    ngram_range: tuple[int, int],
) -> Counter[str]:
    """Count all ``min_n``- through ``max_n``-word n-grams across exemplars."""
    min_n, max_n = ngram_range
    counts: Counter[str] = Counter()
    for exemplar in exemplars:
        tokens = _tokenize_words(exemplar.body)
        for n in range(min_n, max_n + 1):
            counts.update(_extract_ngrams(tokens, n))
    return counts


def _scan_domain_usages(
    family: MetaphorFamily,
    pairs: list[tuple[str, str]],
) -> tuple[LexiconContext, ...]:
    """Collect unique sentence usages for a metaphor family's keywords."""
    usages: list[LexiconContext] = []
    seen: set[tuple[str, str]] = set()
    for frag_id, sentence in pairs:
        if not any(_count_word_in_sentence(sentence, kw) for kw in family.matches):
            continue
        key = (frag_id, sentence)
        if key in seen:
            continue
        usages.append(LexiconContext(fragment_id=frag_id, sentence=sentence))
        seen.add(key)
    return tuple(usages)


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------


def _render_context_line(context: LexiconContext) -> str:
    """Render a bullet line linking back to the fragment."""
    return f"- [[{context.fragment_id}]] — {context.sentence}"


def _render_coined_terms(entries: tuple[CoinedTermEntry, ...]) -> list[str]:
    """Render the ``## Coined Terms`` section."""
    lines: list[str] = ["## Coined Terms", ""]
    if not entries:
        lines.extend(("_No coined terms detected._", ""))
        return lines
    for entry in entries:
        lines.extend(
            (
                f"### {entry.term}",
                "",
                f"- **Usage count:** {entry.count}",
                "- **Contexts:**",
            )
        )
        lines.extend(f"  {_render_context_line(c)}" for c in entry.contexts)
        lines.append("")
    return lines


def _render_metaphors(inventories: tuple[MetaphorInventory, ...]) -> list[str]:
    """Render the ``## Metaphor Families`` section."""
    lines: list[str] = ["## Metaphor Families", ""]
    if not inventories:
        lines.extend(("_No metaphor families detected._", ""))
        return lines
    for inv in inventories:
        lines.extend(
            (
                f"### {inv.domain.title()}",
                "",
                f"- **Keywords:** {', '.join(inv.keywords)}",
                "- **Usages:**",
            )
        )
        lines.extend(f"  {_render_context_line(u)}" for u in inv.usages)
        lines.append("")
    return lines


def _render_phrases(entries: tuple[DistinctivePhraseEntry, ...]) -> list[str]:
    """Render the ``## Distinctive Phrases`` section."""
    lines: list[str] = ["## Distinctive Phrases", ""]
    if not entries:
        lines.extend(("_No distinctive phrases detected._", ""))
        return lines
    lines.extend(f"- `{entry.phrase}` ({entry.count})" for entry in entries)
    lines.append("")
    return lines


def _render_borrowed_terms(entries: tuple[BorrowedTermEntry, ...]) -> list[str]:
    """Render the ``## Borrowed Terms`` section."""
    lines: list[str] = ["## Borrowed Terms", ""]
    if not entries:
        lines.extend(("_No borrowed terms detected._", ""))
        return lines
    for entry in entries:
        lines.extend(
            (
                f"### {entry.term} ({entry.tradition})",
                "",
                f"- **Tradition:** {entry.tradition}",
                f"- **Usage count:** {entry.count}",
                "- **Contexts:**",
            )
        )
        lines.extend(f"  {_render_context_line(c)}" for c in entry.contexts)
        lines.append("")
    return lines


def _render_metaphor_note(inv: MetaphorInventory) -> str:
    """Render a per-domain metaphor note body."""
    lines: list[str] = [
        f"# Metaphor Family: {inv.domain.title()}",
        "",
        f"**Keywords:** {', '.join(inv.keywords) if inv.keywords else '_none_'}",
        "",
        "## Usages",
        "",
    ]
    if not inv.usages:
        lines.append("_No recorded usages._")
    else:
        lines.extend(_render_context_line(u) for u in inv.usages)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_positive(name: str, value: int) -> None:
    """Raise ValueError if *value* is below 1."""
    if value < 1:
        msg = f"{name} must be >= 1, got {value}"
        raise ValueError(msg)


def _validate_ngram_range(ngram_range: tuple[int, int]) -> None:
    """Raise ValueError if ``(min_n, max_n)`` violates 1 ≤ min ≤ max."""
    min_n, max_n = ngram_range
    if min_n < 1 or max_n < 1 or min_n > max_n:
        msg = f"ngram_range must satisfy 1 <= min_n <= max_n, got {ngram_range}"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# LexiconGenerator
# ---------------------------------------------------------------------------


class LexiconGenerator:
    """Build and persist the Creek voice lexicon.

    Combines exemplar bodies with extracted :class:`VoicePatterns` to
    produce a :class:`Lexicon` containing four cross-linked inventories
    (coined terms, metaphors, phrases, borrowed terms) and writes the
    glossary plus per-domain metaphor notes under ``07-Voice/Lexicon/``.

    Attributes:
        reference_words: Known-word set used for coinage detection. When
            ``None``, no coinages are produced.
        tradition_glossaries: Mapping of tradition name to its keyword
            set; defaults to :data:`TRADITION_GLOSSARIES`.
        min_coinage_occurrences: Minimum occurrences for a token to be
            recorded as a coinage.
        min_phrase_occurrences: Minimum occurrences for an n-gram to be
            recorded as a distinctive phrase.
        top_phrases: Upper bound on phrases returned.
        ngram_range: Inclusive ``(min_n, max_n)`` window used for
            distinctive-phrase extraction.
    """

    def __init__(
        self,
        *,
        reference_words: frozenset[str] | None = None,
        tradition_glossaries: dict[str, frozenset[str]] | None = None,
        min_coinage_occurrences: int = _DEFAULT_MIN_COINAGE_OCCURRENCES,
        min_phrase_occurrences: int = _DEFAULT_MIN_PHRASE_OCCURRENCES,
        top_phrases: int = _DEFAULT_TOP_PHRASES,
        ngram_range: tuple[int, int] = _DEFAULT_NGRAM_RANGE,
    ) -> None:
        """Initialise the generator.

        Args:
            reference_words: Known-word set for coinage detection.
            tradition_glossaries: Tradition → keyword set mapping.
            min_coinage_occurrences: Coinage occurrence threshold
                (must be ≥ 1).
            min_phrase_occurrences: Phrase occurrence threshold
                (must be ≥ 1).
            top_phrases: Cap on distinctive phrases returned
                (must be ≥ 1).
            ngram_range: ``(min_n, max_n)`` inclusive window for
                n-gram extraction; both bounds must be ≥ 1 and
                ``min_n ≤ max_n``.

        Raises:
            ValueError: If any numeric parameter is below its minimum
                or if ``min_n`` exceeds ``max_n``.
        """
        _validate_positive("min_coinage_occurrences", min_coinage_occurrences)
        _validate_positive("min_phrase_occurrences", min_phrase_occurrences)
        _validate_positive("top_phrases", top_phrases)
        _validate_ngram_range(ngram_range)
        self.reference_words = reference_words
        self.tradition_glossaries = (
            tradition_glossaries
            if tradition_glossaries is not None
            else TRADITION_GLOSSARIES
        )
        self.min_coinage_occurrences = min_coinage_occurrences
        self.min_phrase_occurrences = min_phrase_occurrences
        self.top_phrases = top_phrases
        self.ngram_range = ngram_range
        self._lower_ref: frozenset[str] | None = (
            frozenset(w.lower() for w in reference_words)
            if reference_words is not None
            else None
        )
        self._lower_traditions: dict[str, frozenset[str]] = {
            name: frozenset(w.lower() for w in words)
            for name, words in self.tradition_glossaries.items()
        }

    def build_lexicon(
        self,
        exemplars: list[Exemplar],
        patterns: VoicePatterns,
    ) -> Lexicon:
        """Produce a :class:`Lexicon` from exemplars and voice patterns.

        Args:
            exemplars: Fragment + body pairs. The issue spec names this
                argument ``fragments``, but the body text is required
                for contextual rendering, so the established
                :class:`Exemplar` (fragment + body) pair is used here
                to avoid re-reading the vault from disk.
            patterns: Voice patterns extracted by
                :class:`VoicePatternExtractor`; used for the metaphor
                family set.

        Returns:
            A populated :class:`Lexicon`.
        """
        pairs = _iter_exemplar_sentences(exemplars)
        return Lexicon(
            coined_terms=self._build_coined_terms(pairs),
            metaphors=self._build_metaphors(pairs, patterns),
            distinctive_phrases=self._build_phrases(exemplars),
            borrowed_terms=self._build_borrowed_terms(pairs),
        )

    def write_lexicon(self, lexicon: Lexicon, vault_path: Path) -> Path:
        """Write *lexicon* to ``07-Voice/Lexicon/glossary.md``.

        Args:
            lexicon: The lexicon to persist.
            vault_path: Root of the Obsidian vault.

        Returns:
            Absolute path of the written glossary file.
        """
        target_dir = vault_path.joinpath(*_LEXICON_SUBPATH)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _GLOSSARY_FILENAME
        body = self._render_glossary_body(lexicon)
        post = frontmatter.Post(
            content=body,
            type="lexicon",
            coined_term_count=len(lexicon.coined_terms),
            metaphor_domain_count=len(lexicon.metaphors),
            distinctive_phrase_count=len(lexicon.distinctive_phrases),
            borrowed_term_count=len(lexicon.borrowed_terms),
            generated_date=datetime.now(tz=UTC).isoformat(),
            tags=["voice", "lexicon"],
        )
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target

    def write_metaphor_index(
        self,
        lexicon: Lexicon,
        vault_path: Path,
    ) -> list[Path]:
        """Write one detailed note per metaphor domain.

        Notes are written to ``07-Voice/Lexicon/Metaphors/<domain>.md``.

        Args:
            lexicon: The lexicon whose metaphor inventories to persist.
            vault_path: Root of the Obsidian vault.

        Returns:
            List of absolute paths written, in input order.
        """
        target_dir = vault_path.joinpath(*_LEXICON_SUBPATH, _METAPHORS_SUBDIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for inv in lexicon.metaphors:
            target = target_dir / f"{inv.domain}.md"
            body = _render_metaphor_note(inv)
            post = frontmatter.Post(
                content=body,
                type="metaphor-family",
                domain=inv.domain,
                keyword_count=len(inv.keywords),
                usage_count=len(inv.usages),
                tags=["voice", "lexicon", "metaphor", inv.domain],
            )
            target.write_text(frontmatter.dumps(post), encoding="utf-8")
            written.append(target)
        return written

    # ---- Private helpers ----

    def _build_coined_terms(
        self,
        pairs: list[tuple[str, str]],
    ) -> tuple[CoinedTermEntry, ...]:
        """Detect coinages: tokens absent from reference + recurring."""
        if self._lower_ref is None:
            return ()
        lower_ref = self._lower_ref
        threshold = self.min_coinage_occurrences
        buckets = _gather_term_matches(
            pairs,
            lambda token: token not in lower_ref,
        )
        entries = [
            CoinedTermEntry(
                term=term,
                count=count,
                contexts=tuple(contexts),
            )
            for term, (count, contexts) in buckets.items()
            if count >= threshold
        ]
        return tuple(sorted(entries, key=lambda e: (-e.count, e.term)))

    def _build_metaphors(
        self,
        pairs: list[tuple[str, str]],
        patterns: VoicePatterns,
    ) -> tuple[MetaphorInventory, ...]:
        """Rehydrate each metaphor family with fragment-linked usages."""
        inventories: list[MetaphorInventory] = []
        for family in patterns.metaphor_families:
            usages = _scan_domain_usages(family, pairs)
            inventories.append(
                MetaphorInventory(
                    domain=family.domain,
                    keywords=family.matches,
                    usages=usages,
                ),
            )
        return tuple(inventories)

    def _build_phrases(
        self,
        exemplars: list[Exemplar],
    ) -> tuple[DistinctivePhraseEntry, ...]:
        """Extract recurring n-grams filtered by threshold and top-n."""
        if not exemplars:
            return ()
        counts = _count_phrases(exemplars, self.ngram_range)
        filtered = [
            DistinctivePhraseEntry(phrase=phrase, count=count)
            for phrase, count in counts.most_common()
            if count >= self.min_phrase_occurrences
        ]
        return tuple(filtered[: self.top_phrases])

    def _build_borrowed_terms(
        self,
        pairs: list[tuple[str, str]],
    ) -> tuple[BorrowedTermEntry, ...]:
        """Detect tokens matching any tradition glossary."""
        entries: list[BorrowedTermEntry] = []
        for tradition, keywords in self._lower_traditions.items():

            def _is_borrowed(token: str, kws: frozenset[str] = keywords) -> bool:
                return token in kws

            buckets = _gather_term_matches(pairs, _is_borrowed)
            entries.extend(
                BorrowedTermEntry(
                    term=term,
                    tradition=tradition,
                    count=count,
                    contexts=tuple(contexts),
                )
                for term, (count, contexts) in buckets.items()
            )
        return tuple(sorted(entries, key=lambda e: (e.tradition, -e.count, e.term)))

    @staticmethod
    def _render_glossary_body(lexicon: Lexicon) -> str:
        """Render the full glossary markdown body."""
        lines: list[str] = ["# Voice Lexicon", ""]
        lines.extend(_render_coined_terms(lexicon.coined_terms))
        lines.extend(_render_metaphors(lexicon.metaphors))
        lines.extend(_render_phrases(lexicon.distinctive_phrases))
        lines.extend(_render_borrowed_terms(lexicon.borrowed_terms))
        return "\n".join(lines)


def generate_lexicon(
    vault_path: Path,
    *,
    override: PrivacyTierOverride = PrivacyTierOverride.ALL,
) -> tuple[Lexicon | None, list[Path]]:
    """Collect the voice corpus, build the lexicon, and persist it (#580).

    Reuses :meth:`creek.generate.voice.VoiceExemplarCollector.collect_all_exemplars`
    (the same eligibility gate the voice-profile report uses) so the lexicon and
    voice reports draw on one exemplar source of truth, extracts voice patterns
    from their bodies, builds a :class:`Lexicon`, and writes the glossary plus
    the per-domain metaphor index under ``07-Voice/Lexicon/``.

    Coinage detection is reference-corpus gated (see
    :class:`LexiconGenerator`); with no corpus configured the glossary still
    fills from metaphors, distinctive phrases, and borrowed terms.

    Args:
        vault_path: Root of the Obsidian vault.
        override: Tier ceiling for the corpus walk (#968), forwarded to the
            collector. Defaults to
            :attr:`~creek.classify.privacy_filter.PrivacyTierOverride.ALL`,
            meaning "no ceiling declared" — a genuine no-op for callers that
            predate #968. The default is safe because both production surfaces
            state an override explicitly, which
            ``tests/test_mcp_report_tier_ceiling.py``'s
            ``test_production_report_callers_always_state_an_override``
            enforces structurally. This matters more here than elsewhere:
            ``_build_borrowed_terms`` records the *whole surrounding sentence*
            verbatim, so an admitted body lands in ``glossary.md`` byte for
            byte.

    Note:
        There is deliberately **no** ``audience_weighting`` parameter here, and
        the collector below is deliberately built without one, even though
        #1313 threaded that config into every other exemplar-path caller. Every
        artifact this function writes is provably invariant to the weighting:

        1. :meth:`VoiceExemplarCollector.collect_all_exemplars` performs no
           ranking and no capping — it returns the whole eligible corpus, so
           the multiplier has nothing to reorder or cut.
        2. :meth:`VoicePatternExtractor.extract_patterns` is called with no
           ``weights=``, and ``weights`` reaches only ``citation_density``.
        3. :meth:`LexiconGenerator.build_lexicon` consumes *patterns* solely via
           ``_build_metaphors``, which reads only ``metaphor_families``.

        Threading the kwarg — or reaching for the weighted ``weights=`` hatch —
        would therefore be an edit no test could fail on, and an untestable
        no-op is worse than an honest omission: it reads as working wiring.
        ``generate_lexicon`` is consequently absent from the audience-weighting
        structural guard while remaining in the override guard. What protects
        this path instead is the invariance tripwire
        ``test_lexicon_output_is_invariant_to_the_audience_weighting`` in
        ``tests/test_voice_audience_weighting_wiring.py``, which goes red the
        day the lexicon starts consuming a weighted metric — something a guard
        exclusion could never do.

    Returns:
        ``(lexicon, written_paths)``. When the vault has no qualifying
        exemplars, returns ``(None, [])`` and writes nothing — the caller
        renders a friendly "no exemplars" message.
    """
    from creek.generate.voice import VoiceExemplarCollector, VoicePatternExtractor

    exemplars = VoiceExemplarCollector(override=override).collect_all_exemplars(
        vault_path,
    )
    if not exemplars:
        return None, []
    patterns = VoicePatternExtractor().extract_patterns([e.body for e in exemplars])
    generator = LexiconGenerator()
    lexicon = generator.build_lexicon(exemplars, patterns)
    glossary_path = generator.write_lexicon(lexicon, vault_path)
    metaphor_paths = generator.write_metaphor_index(lexicon, vault_path)
    return lexicon, [glossary_path, *metaphor_paths]


__all__ = [
    "TRADITION_GLOSSARIES",
    "BorrowedTermEntry",
    "CoinedTermEntry",
    "DistinctivePhraseEntry",
    "Lexicon",
    "LexiconContext",
    "LexiconGenerator",
    "MetaphorInventory",
    "generate_lexicon",
]
