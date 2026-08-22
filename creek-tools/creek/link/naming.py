"""Cluster naming for the linker — titles, descriptions, and uniqueness.

Both detectors (:mod:`creek.link.eddies` and :mod:`creek.link.threads`)
need the same thing once they have a set of member fragments: a short
human-readable name, a one-line description, and a guarantee that the
name does not collide with another cluster from the same run. This
module is the single home for that logic, and for the small text
helpers the two detectors previously carried in duplicate.

Why it exists as its own module
-------------------------------

The naive implementation — a :class:`collections.Counter` over member
fragment titles, take the three most common words — has a failure mode
that is invisible on a small corpus and catastrophic on a large one.
A :class:`~creek.models.Fragment` carries no body or content field, so
its ``title`` is the *only* text the linker ever sees. When an ingestor
does not supply a title the vault falls back to the source filename, and
a whole platform's worth of fragments can therefore share one identical,
meaningless title. Raw frequency then hands that word to every cluster
in the corpus.

Three properties make that impossible here:

*Inverse document frequency.* :func:`distinctive_terms` scores a term by
``tf * log(corpus_size / (1 + df))`` and hard-drops any term whose
document frequency reaches :attr:`NamingPolicy.ubiquity_floor`. A word
carried by most of the corpus carries no information about which cluster
it is in, so it can never become a title. IDF is deliberately *not*
applied below :attr:`NamingPolicy.min_idf_corpus` documents: document
frequency over a handful of documents is noise rather than evidence, and
a two-cluster corpus puts every legitimate topic word at ``df/n == 0.5``
through no fault of its own.

*A structural fallback.* Suppression can leave a cluster with no text at
all — which is exactly what happens to a stream of identically-titled
messages. :func:`structural_label` then names the cluster from what is
still distinctive about it: the dominant source identity (channel,
conversation, or interlocutor, falling back to the platform) plus the
date span its members cover.

*Uniqueness.* :func:`disambiguate` separates titles that would otherwise
collide, because the eddy assigner interpolates a title straight into a
``[[...]]`` wiki-link; two clusters sharing a name means every member
fragment links to whichever page resolves first.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.time import effective_authored_date

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date

    from creek.models import Fragment

TITLE_TOP_WORDS = 3
MIN_TITLE_WORD_LEN = 3

#: Document-frequency ratio at (or above) which a term is discarded as
#: uninformative. Inclusive: a term in exactly this share of the corpus
#: is suppressed.
DEFAULT_UBIQUITY_FLOOR = 0.6

#: Smallest corpus for which inverse document frequency is trusted.
#: Below this, ranking falls back to raw in-cluster counts.
DEFAULT_MIN_IDF_CORPUS = 20

_UNKNOWN_SOURCE = "Unsourced"
_UNTITLED = "Untitled"

_WORD_RE = re.compile(r"[a-z]+")
# Characters that break or make ambiguous an Obsidian ``[[wiki-link]]``.
_WIKI_HOSTILE_RE = re.compile(r"[\[\]|#^]")
_WHITESPACE_RE = re.compile(r"\s+")

# Month names are spelled out rather than taken from ``strftime("%b")``
# so the label is identical under every locale.
_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "doing",
        "don",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "s",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "t",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    },
)


@dataclass(frozen=True)
class NamingPolicy:
    """Tunable knobs for cluster naming.

    Attributes:
        ubiquity_floor: Document-frequency ratio at or above which a
            term is discarded outright. ``0.6`` means "a term carried by
            60% or more of the corpus tells us nothing about which
            cluster we are looking at". The comparison is inclusive, so
            a term sitting exactly on the floor is suppressed.
        min_idf_corpus: Smallest corpus size for which inverse document
            frequency is applied at all. Below it, terms are ranked by
            raw in-cluster frequency and nothing is suppressed — document
            frequency over a dozen documents is noise, and a two-cluster
            corpus necessarily puts each cluster's own topic word at
            ``df / corpus_size == 0.5``.
        title_terms: Maximum number of distinctive terms to compose a
            title from.
    """

    ubiquity_floor: float = DEFAULT_UBIQUITY_FLOOR
    min_idf_corpus: int = DEFAULT_MIN_IDF_CORPUS
    title_terms: int = TITLE_TOP_WORDS


#: The policy used when a caller does not supply one.
DEFAULT_NAMING_POLICY = NamingPolicy()


# ---- Text helpers ----


def tokenize(text: str) -> list[str]:
    """Tokenise *text* into lowercase alphabetic words.

    Args:
        text: Input string to tokenise.

    Returns:
        A list of lowercase word tokens (no punctuation, no digits).
    """
    return _WORD_RE.findall(text.lower())


def content_words(text: str) -> list[str]:
    """Extract content words from *text* (no stopwords, length-filtered).

    Args:
        text: Input string.

    Returns:
        A list of lowercase content words suitable for title heuristics.
    """
    return [
        w for w in tokenize(text) if w not in STOPWORDS and len(w) >= MIN_TITLE_WORD_LEN
    ]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two equal-length vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity in ``[-1.0, 1.0]``; ``0.0`` if either vector
        has zero norm.
    """
    import numpy as np  # lazy: numpy lives in the [embeddings] extra

    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if 0.0 in (na, nb):
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _collapse(text: str) -> str:
    """Collapse all whitespace runs in *text* to single spaces and strip.

    Args:
        text: Input string, possibly carrying newlines or double spaces.

    Returns:
        A single-line, single-spaced, stripped string.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def _wiki_safe(text: str) -> str:
    """Make *text* safe to interpolate into an Obsidian ``[[wiki-link]]``.

    Bracket, pipe, hash and caret characters would terminate the link,
    turn the remainder into an alias, or address a heading/block — so
    they are replaced with spaces rather than deleted, keeping word
    boundaries intact.

    Args:
        text: Candidate link target.

    Returns:
        A single-line string containing none of ``[``, ``]``, ``|``,
        ``#`` or ``^``.
    """
    return _collapse(_WIKI_HOSTILE_RE.sub(" ", text))


# ---- Document frequency and distinctive terms ----


def document_frequency(fragments: Iterable[Fragment]) -> dict[str, int]:
    """Count how many corpus fragments carry each content word.

    Document frequency, not term frequency: a word repeated three times
    inside one title counts once. This is the corpus-wide statistic
    :func:`distinctive_terms` needs to tell a topic word apart from a
    word that merely happens to be everywhere.

    Args:
        fragments: The whole corpus under consideration — not just one
            cluster. Passing a single cluster makes every one of its
            terms look ubiquitous.

    Returns:
        A mapping of content word to the number of fragments whose title
        contains it.
    """
    counts: Counter[str] = Counter()
    for frag in fragments:
        counts.update(set(content_words(frag.title)))
    return dict(counts)


def _is_ubiquitous(term_df: int, corpus_size: int, policy: NamingPolicy) -> bool:
    """Report whether a term is carried by enough of the corpus to be useless.

    Args:
        term_df: How many corpus fragments carry the term.
        corpus_size: Total number of corpus fragments.
        policy: Naming policy supplying the ubiquity floor.

    Returns:
        ``True`` when ``term_df / corpus_size`` reaches
        :attr:`NamingPolicy.ubiquity_floor`.
    """
    return corpus_size > 0 and term_df / corpus_size >= policy.ubiquity_floor


def _score_terms(
    tf: Mapping[str, int],
    df: Mapping[str, int],
    corpus_size: int,
    policy: NamingPolicy,
) -> list[tuple[float, str]]:
    """Score each in-cluster term, dropping the corpus-ubiquitous ones.

    Args:
        tf: In-cluster term frequencies.
        df: Corpus-wide document frequencies.
        corpus_size: Total number of corpus fragments.
        policy: Naming policy supplying the floor and the IDF cut-in.

    Returns:
        ``(score, term)`` pairs for every term that survived suppression.
    """
    apply_idf = corpus_size >= max(policy.min_idf_corpus, 1)
    scored: list[tuple[float, str]] = []
    for term, count in tf.items():
        term_df = df.get(term, 0)
        if not apply_idf:
            scored.append((float(count), term))
            continue
        if _is_ubiquitous(term_df, corpus_size, policy):
            continue
        scored.append((count * math.log(corpus_size / (1 + term_df)), term))
    return scored


def distinctive_terms(
    cluster: Sequence[Fragment],
    df: Mapping[str, int],
    corpus_size: int,
    *,
    policy: NamingPolicy = DEFAULT_NAMING_POLICY,
) -> list[str]:
    """Return the terms that best distinguish *cluster* from the corpus.

    Implements the TF-IDF-lite the linker's docstrings have long claimed:
    in-cluster term frequency scaled by ``log(corpus_size / (1 + df))``,
    with a hard ubiquity cut so a term carried by most of the corpus
    contributes nothing at all. Ties break alphabetically so a run over
    the same corpus always produces the same title.

    Args:
        cluster: Member fragments of one cluster (may be empty).
        df: Corpus-wide document frequencies from
            :func:`document_frequency`.
        corpus_size: Total number of fragments the ``df`` mapping was
            built from.
        policy: Naming policy. Defaults to
            :data:`DEFAULT_NAMING_POLICY`.

    Returns:
        Up to :attr:`NamingPolicy.title_terms` lowercase terms, best
        first. Empty when the cluster carries no content words, or when
        every one of them is corpus-ubiquitous — which is the normal
        outcome for a stream of identically-titled message fragments.
    """
    tf: Counter[str] = Counter()
    for frag in cluster:
        tf.update(content_words(frag.title))
    if not tf:
        return []
    scored = _score_terms(tf, df, corpus_size, policy)
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [term for _, term in scored[: policy.title_terms]]


# ---- Structural labels ----


def _source_identity(fragment: Fragment) -> str:
    """Return the most specific source identity available for *fragment*.

    Args:
        fragment: The fragment to describe.

    Returns:
        The channel, else the conversation id, else the interlocutor,
        else the platform name in title case. Never empty.
    """
    src = fragment.source
    for candidate in (src.channel, src.conversation_id, src.interlocutor):
        if candidate and candidate.strip():
            return candidate.strip()
    return str(src.platform).replace("_", " ").title()


def _dominant_source(cluster: Sequence[Fragment]) -> str:
    """Return the source identity shared by most of *cluster*.

    Args:
        cluster: Member fragments (may be empty).

    Returns:
        The most common source identity, ties broken alphabetically for
        determinism; a placeholder when the cluster is empty.
    """
    counts = Counter(_source_identity(f) for f in cluster)
    if not counts:
        return _UNKNOWN_SOURCE
    best = max(counts.values())
    return min(name for name, seen in counts.items() if seen == best)


def _month_of(day: date) -> str:
    """Return the locale-independent three-letter month name for *day*.

    Args:
        day: The date to name.

    Returns:
        A month abbreviation such as ``"Mar"``.
    """
    return _MONTH_ABBR[day.month - 1]


def _date_span(cluster: Sequence[Fragment]) -> str:
    """Describe the period *cluster* covers, at month granularity.

    Buckets on :func:`creek.time.effective_authored_date` so the span
    reflects when the content was authored rather than when Creek
    happened to ingest it.

    Args:
        cluster: Member fragments (may be empty).

    Returns:
        ``"Mar 2023"`` for a single month, ``"Mar-Aug 2023"`` within one
        year, ``"Mar 2023-Aug 2024"`` across years, and ``""`` for an
        empty cluster.
    """
    if not cluster:
        return ""
    dates = [effective_authored_date(f) for f in cluster]
    first, last = min(dates), max(dates)
    if (first.year, first.month) == (last.year, last.month):
        return f"{_month_of(first)} {first.year}"
    if first.year == last.year:
        return f"{_month_of(first)}-{_month_of(last)} {last.year}"
    return f"{_month_of(first)} {first.year}-{_month_of(last)} {last.year}"


def structural_label(cluster: Sequence[Fragment]) -> str:
    """Name *cluster* from its structure when it has no usable text.

    This is the only naming path available to a cluster of
    identically-titled fragments: once IDF has suppressed the shared
    word there is no text left, because :class:`~creek.models.Fragment`
    stores no body. What remains distinctive is *where* the fragments
    came from and *when* — so the label is the dominant source identity
    plus the date span, e.g. ``"Aspiring Drag Queens, Mar 2023"``.

    Args:
        cluster: Member fragments (may be empty).

    Returns:
        A non-empty, wiki-link-safe label.
    """
    source = _dominant_source(cluster)
    span = _date_span(cluster)
    label = f"{source}, {span}" if span else source
    return _wiki_safe(label) or _UNTITLED


# ---- Composed titles and descriptions ----


def cluster_title(
    cluster: Sequence[Fragment],
    df: Mapping[str, int],
    corpus_size: int,
    *,
    policy: NamingPolicy = DEFAULT_NAMING_POLICY,
) -> str:
    """Return a short, non-empty, wiki-link-safe title for *cluster*.

    Prefers the cluster's distinctive terms; falls back to
    :func:`structural_label` when IDF leaves no term standing. The result
    is not yet guaranteed unique across a run — pass the full set of
    titles through :func:`disambiguate` for that.

    Args:
        cluster: Member fragments (may be empty).
        df: Corpus-wide document frequencies.
        corpus_size: Total number of corpus fragments.
        policy: Naming policy. Defaults to
            :data:`DEFAULT_NAMING_POLICY`.

    Returns:
        A non-empty title containing none of ``[``, ``]``, ``|``, ``#``
        or ``^``.
    """
    terms = distinctive_terms(cluster, df, corpus_size, policy=policy)
    if not terms:
        return structural_label(cluster)
    return _wiki_safe(" ".join(term.capitalize() for term in terms)) or _UNTITLED


def cluster_description(
    cluster: Sequence[Fragment],
    df: Mapping[str, int],
    corpus_size: int,
    *,
    policy: NamingPolicy = DEFAULT_NAMING_POLICY,
) -> str:
    """Return a one-line, never-empty description of *cluster*.

    Generated eddy and thread pages historically shipped
    ``description: ''`` because neither detector ever supplied one. The
    line here carries what an operator needs to triage a page without
    opening it: how many fragments, where they came from, when, and
    which terms recur.

    Args:
        cluster: Member fragments (may be empty).
        df: Corpus-wide document frequencies.
        corpus_size: Total number of corpus fragments.
        policy: Naming policy. Defaults to
            :data:`DEFAULT_NAMING_POLICY`.

    Returns:
        A single-line, non-empty sentence safe to serialise as a YAML
        frontmatter scalar.
    """
    count = len(cluster)
    noun = "fragment" if count == 1 else "fragments"
    head = f"{count} {noun} from {_dominant_source(cluster)}"
    span = _date_span(cluster)
    if span:
        head = f"{head}, {span}"
    terms = distinctive_terms(cluster, df, corpus_size, policy=policy)
    tail = f"; recurring terms: {', '.join(terms)}" if terms else ""
    return _collapse(f"{head}{tail}.")


def disambiguate(titles: Sequence[str]) -> list[str]:
    """Make *titles* unique within one detection run, preserving order.

    The first claimant of a title keeps it bare; every later collision
    gains a ``" (n)"`` suffix, incrementing until the result is free —
    so a title that already looks like a suffixed one cannot be
    duplicated. The suffix survives
    :func:`creek.vault.writer._sanitize_title`, which strips punctuation
    but keeps the digit, so the generated page filenames stay distinct
    too.

    This matters because the eddy assigner writes ``[[<title>]]`` onto
    every member fragment: two clusters sharing a title would send both
    memberships to whichever page Obsidian resolved first.

    Args:
        titles: Candidate titles, in emission order.

    Returns:
        A list of the same length, in the same order, with no duplicates.
    """
    seen: set[str] = set()
    result: list[str] = []
    for title in titles:
        candidate = title
        suffix = 2
        while candidate in seen:
            candidate = f"{title} ({suffix})"
            suffix += 1
        seen.add(candidate)
        result.append(candidate)
    return result
