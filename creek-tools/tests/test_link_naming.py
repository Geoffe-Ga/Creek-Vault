"""Tests for :mod:`creek.link.naming` — cluster titles and descriptions.

The module under test is the root fix for the platform-name-as-topic-label
defect: a corpus whose fragments all carry the same filename-derived title
(``title: messages`` on every Discord fragment) used to hand that word
straight to ``most_common(3)`` and name the cluster ``Messages``. These
tests pin the four properties that make that impossible:

1. Real inverse document frequency, so a term carried by most of the
   corpus can never contribute a title token.
2. A *structural* fallback — dominant source plus date span — for the
   case IDF creates: a cluster with no surviving text at all. Nothing
   else is available, because :class:`~creek.models.Fragment` has no
   body field; the title string is the only text the linker ever sees.
3. A never-empty description.
4. Title uniqueness within one detection run, because the eddy assigner
   writes ``[[<title>]]`` wiki-links and colliding titles collapse them.

The IDF suppression is deliberately disabled for a corpus smaller than
:attr:`NamingPolicy.min_idf_corpus`: document frequency over a handful of
documents is noise, not evidence, and the historical docstring already
promised "falls back to raw counts when the corpus is small".
"""

from __future__ import annotations

from datetime import datetime

import pytest

from creek.link.naming import (
    DEFAULT_NAMING_POLICY,
    STOPWORDS,
    NamingPolicy,
    cluster_description,
    cluster_title,
    content_words,
    cosine,
    disambiguate,
    distinctive_terms,
    document_frequency,
    structural_label,
    tokenize,
)
from creek.models import Fragment, FragmentSource, SourcePlatform


def _frag(
    fid: str,
    title: str,
    *,
    platform: SourcePlatform = SourcePlatform.DISCORD,
    channel: str | None = None,
    conversation_id: str | None = None,
    interlocutor: str | None = None,
    authored_at: datetime | None = None,
) -> Fragment:
    """Build a Fragment with the source metadata the namer reads.

    ``authored_at`` is set explicitly (rather than mirrored through
    ``ingested``) because :func:`creek.time.effective_authored_date`
    prefers it, and every date-span assertion here cares about the
    source-side date rather than construction-time wall-clock.
    """
    return Fragment(
        id=fid,
        title=title,
        source=FragmentSource(
            platform=platform,
            channel=channel,
            conversation_id=conversation_id,
            interlocutor=interlocutor,
        ),
        authored_at=authored_at or datetime(2023, 3, 15, 12, 0, 0),
    )


def _corpus(
    *,
    ubiquitous: int,
    total: int,
    ubiquitous_word: str = "messages",
) -> list[Fragment]:
    """Return *total* fragments, *ubiquitous* of which carry a shared word."""
    corpus = [
        _frag(f"ubiq-{i}", f"{ubiquitous_word} entry {i}") for i in range(ubiquitous)
    ]
    corpus.extend(
        _frag(f"rest-{i}", f"essay {i} recital") for i in range(total - ubiquitous)
    )
    return corpus


# ---- Shared text helpers (canonical home for the linker's duplicates) ----


class TestTextHelpers:
    """The tokenising helpers hoisted out of the two detector modules."""

    def test_tokenize_lowercases_and_drops_punctuation_and_digits(self) -> None:
        """Tokenising keeps alphabetic runs only, lowercased."""
        assert tokenize("Garden, Journal! 2023 -- Monday") == [
            "garden",
            "journal",
            "monday",
        ]

    def test_content_words_drops_stopwords_and_short_words(self) -> None:
        """Stopwords and words under three characters are not content."""
        assert content_words("The art of a Garden in Spring") == [
            "art",
            "garden",
            "spring",
        ]

    def test_stopwords_does_not_contain_messages(self) -> None:
        """'messages' is a real word — the stoplist is not the fix here.

        Pinning this keeps a future reader from "fixing" the mega-cluster
        title by bolting platform nouns onto the stoplist, which would
        paper over the missing IDF and break the moment a vault used a
        different filename.
        """
        assert "messages" not in STOPWORDS

    def test_cosine_of_orthogonal_and_identical_vectors(self) -> None:
        """Cosine returns 1.0 for identical and 0.0 for orthogonal vectors."""
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_of_zero_norm_vector_is_zero(self) -> None:
        """A zero vector has no direction — similarity is 0.0, not NaN."""
        assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---- Document frequency ----


class TestDocumentFrequency:
    """Corpus-wide document frequency over fragment titles."""

    def test_counts_documents_not_occurrences(self) -> None:
        """A word repeated inside one title counts that title once."""
        corpus = [
            _frag("a", "garden garden garden"),
            _frag("b", "garden harvest"),
            _frag("c", "recital"),
        ]
        assert document_frequency(corpus)["garden"] == 2

    def test_empty_corpus_yields_empty_mapping(self) -> None:
        """No documents means no document frequencies."""
        assert document_frequency([]) == {}


# ---- IDF: the missing half of the documented TF-IDF-lite ----


class TestDistinctiveTerms:
    """Ranking by tf * idf, with a ubiquity floor that hard-drops terms."""

    def test_idf_suppresses_terms_carried_by_most_of_the_corpus(self) -> None:
        """A term in 87% of the corpus contributes no title token.

        This is the exact demo-vault shape: 'messages' is the single most
        frequent word *inside* the cluster, and it is also carried by
        almost the whole corpus. Raw ``Counter.most_common`` picks it;
        IDF must not.
        """
        corpus = _corpus(ubiquitous=87, total=100)
        cluster = [_frag(f"c-{i}", "messages garden") for i in range(30)]
        corpus.extend(cluster)
        df = document_frequency(corpus)

        terms = distinctive_terms(cluster, df, len(corpus))

        assert "messages" not in terms
        assert "garden" in terms

    def test_term_at_exactly_the_ubiquity_floor_is_dropped(self) -> None:
        """The floor is inclusive: df / corpus == floor means suppressed."""
        policy = NamingPolicy(ubiquity_floor=0.5, min_idf_corpus=4)
        corpus = [_frag(f"in-{i}", "shared topic") for i in range(2)]
        corpus.extend(_frag(f"out-{i}", "elsewhere") for i in range(2))
        df = document_frequency(corpus)
        assert df["shared"] / len(corpus) == 0.5

        terms = distinctive_terms(corpus[:2], df, len(corpus), policy=policy)

        assert "shared" not in terms

    def test_term_just_below_the_ubiquity_floor_survives(self) -> None:
        """One document less than the floor and the term is still usable."""
        policy = NamingPolicy(ubiquity_floor=0.5, min_idf_corpus=4)
        corpus = [_frag(f"in-{i}", "shared topic") for i in range(2)]
        corpus.extend(_frag(f"out-{i}", "elsewhere") for i in range(3))
        df = document_frequency(corpus)
        assert df["shared"] / len(corpus) < 0.5

        terms = distinctive_terms(corpus[:2], df, len(corpus), policy=policy)

        assert "shared" in terms

    def test_small_corpus_falls_back_to_raw_counts(self) -> None:
        """Below ``min_idf_corpus`` the ubiquity floor does not apply.

        Document frequency over a dozen documents is noise: in the
        existing two-cluster detector fixtures a perfectly good topic word
        sits at df/corpus == 0.5 purely because there are two clusters.
        Suppressing it there would destroy real titles to fix a
        35,000-fragment problem.
        """
        corpus = [_frag(f"g-{i}", "garden journal") for i in range(6)]
        df = document_frequency(corpus)
        assert df["garden"] == len(corpus)

        terms = distinctive_terms(corpus, df, len(corpus))

        assert terms[0] == "garden"

    def test_ranking_prefers_higher_frequency_within_the_cluster(self) -> None:
        """Among equally-rare terms, the more frequent one ranks first."""
        cluster = [_frag(f"r-{i}", "garden") for i in range(5)]
        cluster.append(_frag("r-solo", "harvest"))
        df = document_frequency(cluster)

        terms = distinctive_terms(cluster, df, len(cluster))

        assert terms[0] == "garden"

    def test_limit_is_honoured(self) -> None:
        """No more than ``title_terms`` terms are returned."""
        cluster = [_frag("m", "alpha bravo charlie delta echo foxtrot")]
        df = document_frequency(cluster)

        terms = distinctive_terms(cluster, df, len(cluster))

        assert len(terms) == DEFAULT_NAMING_POLICY.title_terms

    def test_cluster_with_no_content_words_yields_no_terms(self) -> None:
        """Titles made entirely of stopwords produce nothing."""
        cluster = [_frag("s", "the and of it")]
        assert distinctive_terms(cluster, {}, 1) == []

    def test_ranking_is_deterministic_for_tied_terms(self) -> None:
        """Equal scores break alphabetically, so runs are reproducible."""
        cluster = [_frag("t", "zulu alpha mike bravo")]
        df = document_frequency(cluster)

        assert distinctive_terms(cluster, df, len(cluster)) == [
            "alpha",
            "bravo",
            "mike",
        ]


# ---- Structural labels: the only path left for a message cluster ----


class TestStructuralLabel:
    """Labels built from source metadata plus date span."""

    def test_uses_dominant_channel_and_date_span(self) -> None:
        """A Discord cluster names itself after its channel and month."""
        cluster = [
            _frag(
                f"d-{i}",
                "messages",
                channel="Aspiring Drag Queens",
                authored_at=datetime(2023, 3, 4 + i),
            )
            for i in range(5)
        ]

        label = structural_label(cluster)

        assert "Aspiring Drag Queens" in label
        assert "Mar 2023" in label

    def test_dominant_channel_wins_over_a_minority_channel(self) -> None:
        """The label names the channel most members came from."""
        cluster = [
            _frag(f"maj-{i}", "messages", channel="Aspiring Drag Queens")
            for i in range(5)
        ]
        cluster.append(_frag("min-0", "messages", channel="Side Chat"))

        assert "Aspiring Drag Queens" in structural_label(cluster)
        assert "Side Chat" not in structural_label(cluster)

    def test_falls_back_to_conversation_id_then_interlocutor(self) -> None:
        """Without a channel, other source identity fields are used."""
        by_conversation = [
            _frag("c-0", "messages", conversation_id="conv-alpha"),
        ]
        by_interlocutor = [
            _frag("i-0", "messages", interlocutor="Dana"),
        ]

        assert "conv-alpha" in structural_label(by_conversation)
        assert "Dana" in structural_label(by_interlocutor)

    def test_label_is_non_empty_when_every_member_lacks_a_channel(self) -> None:
        """Platform plus date span still names the cluster.

        The boundary the demo vault would hit if channel were ever
        unpopulated: there must be no path to an empty label.
        """
        cluster = [
            _frag(
                f"p-{i}",
                "messages",
                platform=SourcePlatform.EMAIL,
                authored_at=datetime(2021, 7, 2 + i),
            )
            for i in range(4)
        ]

        label = structural_label(cluster)

        assert label
        assert "Email" in label
        assert "Jul 2021" in label

    def test_blank_channel_string_is_ignored(self) -> None:
        """A whitespace-only channel is not an identity — fall through."""
        cluster = [_frag("b-0", "messages", channel="   ")]

        assert "Discord" in structural_label(cluster)

    def test_single_member_cluster_produces_a_single_month_span(self) -> None:
        """Exactly one member: the span is that one month, not a range."""
        cluster = [
            _frag(
                "solo",
                "messages",
                channel="Aspiring Drag Queens",
                authored_at=datetime(2023, 3, 15),
            ),
        ]

        assert structural_label(cluster) == "Aspiring Drag Queens, Mar 2023"

    def test_span_within_one_year_shows_both_months_once(self) -> None:
        """A same-year range collapses to one trailing year."""
        cluster = [
            _frag(
                "s-0", "messages", channel="Studio", authored_at=datetime(2023, 3, 1)
            ),
            _frag(
                "s-1", "messages", channel="Studio", authored_at=datetime(2023, 8, 1)
            ),
        ]

        assert structural_label(cluster) == "Studio, Mar-Aug 2023"

    def test_span_across_years_shows_both_years(self) -> None:
        """A cross-year range names both endpoints in full."""
        cluster = [
            _frag(
                "y-0", "messages", channel="Studio", authored_at=datetime(2023, 3, 1)
            ),
            _frag(
                "y-1", "messages", channel="Studio", authored_at=datetime(2024, 8, 1)
            ),
        ]

        assert structural_label(cluster) == "Studio, Mar 2023-Aug 2024"

    def test_empty_cluster_still_returns_a_non_empty_label(self) -> None:
        """The never-empty contract has no exceptions, not even for []."""
        assert structural_label([])


# ---- Titles ----


class TestClusterTitle:
    """The composed title: distinctive terms, else structural label."""

    def test_message_cluster_is_named_from_channel_not_the_filename(self) -> None:
        """The headline regression: 'Messages' must never be the title.

        Every member carries the filename-derived ``title: messages``, so
        after IDF suppression there is no text left — ``Fragment`` has no
        body field, and the title string is the linker's only text
        signal. The name has to come from structure.
        """
        corpus = _corpus(ubiquitous=87, total=100)
        cluster = [
            _frag(
                f"m-{i}",
                "messages",
                channel="Aspiring Drag Queens",
                authored_at=datetime(2023, 3, 4 + i),
            )
            for i in range(12)
        ]
        corpus.extend(cluster)
        df = document_frequency(corpus)

        title = cluster_title(cluster, df, len(corpus))

        assert title != "Messages"
        assert "Aspiring Drag Queens" in title
        assert "Mar 2023" in title

    def test_distinctive_terms_are_preferred_and_capitalised(self) -> None:
        """When real topic words survive, they make the title."""
        cluster = [_frag(f"g-{i}", "garden harvest journal") for i in range(6)]
        df = document_frequency(cluster)

        assert cluster_title(cluster, df, len(cluster)) == "Garden Harvest Journal"

    def test_title_is_never_empty(self) -> None:
        """No corpus shape produces an empty title."""
        cluster = [_frag("e-0", "the and of")]

        assert cluster_title(cluster, {}, 1)

    def test_title_strips_wiki_link_hostile_characters(self) -> None:
        """Brackets and pipes would break the ``[[...]]`` link.

        ``assign_fragments_to_eddies`` interpolates the title directly
        into a wiki-link, so a channel named ``Chat [dev]|main`` must not
        be able to produce a malformed or ambiguous link target. The
        member title is all stopwords so the structural path — the one
        that reads operator-controlled source metadata — is what runs.
        """
        cluster = [_frag("w-0", "the and of", channel="Chat [dev]|main#top^x")]

        title = cluster_title(cluster, {}, 1)

        assert not set(title) & set("[]|#^")
        assert "Chat dev" in title


# ---- Descriptions ----


class TestClusterDescription:
    """One-line, always-populated cluster descriptions."""

    def test_description_is_never_empty(self) -> None:
        """The defect: 212 of 214 linker pages shipped ``description: ''``."""
        cluster = [_frag("d-0", "the and of")]

        assert cluster_description(cluster, {}, 1).strip()

    def test_description_names_count_source_and_span(self) -> None:
        """The line carries the facts an operator needs to triage a page."""
        cluster = [
            _frag(
                f"n-{i}",
                "messages",
                channel="Aspiring Drag Queens",
                authored_at=datetime(2023, 3, 4 + i),
            )
            for i in range(12)
        ]
        df = document_frequency(cluster)

        description = cluster_description(cluster, df, len(cluster))

        assert "12 fragments" in description
        assert "Aspiring Drag Queens" in description
        assert "Mar 2023" in description

    def test_description_lists_surviving_distinctive_terms(self) -> None:
        """Real topic words are surfaced when IDF leaves any standing."""
        corpus = _corpus(ubiquitous=87, total=100)
        cluster = [_frag(f"t-{i}", "messages garden harvest") for i in range(12)]
        corpus.extend(cluster)
        df = document_frequency(corpus)

        description = cluster_description(cluster, df, len(corpus))

        assert "garden" in description
        assert "harvest" in description
        assert "messages" not in description

    def test_single_member_description_uses_the_singular(self) -> None:
        """Exactly one member reads as '1 fragment', not '1 fragments'."""
        cluster = [_frag("s-0", "messages", channel="Studio")]

        assert "1 fragment " in cluster_description(cluster, {}, 1)

    def test_description_is_a_single_line(self) -> None:
        """A frontmatter scalar must not carry embedded newlines."""
        cluster = [_frag("l-0", "messages", channel="Studio\nInjected")]

        assert "\n" not in cluster_description(cluster, {}, 1)

    def test_empty_cluster_description_is_still_non_empty(self) -> None:
        """The never-empty contract holds for the degenerate input too."""
        assert cluster_description([], {}, 0).strip()


# ---- Within-run uniqueness ----


class TestDisambiguate:
    """Titles must be unique within one detection run."""

    def test_titles_are_unique_within_one_detection_run(self) -> None:
        """Colliding titles are separated, so wiki-links stay unambiguous.

        ``assign_fragments_to_eddies`` writes ``[[<title>]]`` on every
        member fragment. Two eddies sharing a title means thousands of
        fragments linking to whichever page Obsidian resolves first.
        """
        titles = ["Garden Harvest", "Garden Harvest", "Recital Notes"]

        result = disambiguate(titles)

        assert len(set(result)) == len(result)
        assert result[0] == "Garden Harvest"

    def test_order_and_first_occurrence_are_preserved(self) -> None:
        """The first claimant keeps the bare title; later ones are suffixed."""
        assert disambiguate(["A", "B", "A", "A"]) == ["A", "B", "A (2)", "A (3)"]

    def test_a_generated_suffix_never_collides_with_a_real_title(self) -> None:
        """A pre-existing 'A (2)' does not get silently duplicated."""
        result = disambiguate(["A", "A (2)", "A"])

        assert len(set(result)) == 3
        assert result == ["A", "A (2)", "A (3)"]

    def test_empty_input_returns_empty_list(self) -> None:
        """Nothing in, nothing out."""
        assert disambiguate([]) == []

    def test_disambiguated_titles_survive_filename_sanitisation(self) -> None:
        """The suffix must still differ after the writer strips punctuation.

        ``creek.vault.writer._sanitize_title`` drops every non-word,
        non-space, non-hyphen character, so a suffix made only of
        punctuation would collapse back into a collision on disk.
        """
        from creek.vault.writer import _sanitize_title

        result = disambiguate(["Garden Harvest"] * 3)

        assert len({_sanitize_title(t) for t in result}) == 3
