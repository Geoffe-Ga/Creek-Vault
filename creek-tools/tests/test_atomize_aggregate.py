"""Tests for the FEAT-022 conversational aggregator.

Exercises ``creek.atomize.aggregate.aggregate`` end-to-end:

- Each level transition (``document`` → ``exchange`` → ``burst`` →
  ``session``).
- Single-message roll-up through all three levels.
- Idempotency contract.
- Session-as-terminal behaviour.
- Gap and similarity boundary tests.
- Source-key grouping and cross-source isolation.
- Helper functions (cosine, ID, source-key).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creek.atomize.aggregate import (
    AggregationConfig,
    _aggregate_id,
    _contributing_source_keys,
    _cosine,
    _parent_source_key,
    _source_key,
    aggregate,
)
from creek.models import (
    Authorship,
    Fragment,
    FragmentLevel,
    FragmentSource,
    SourcePlatform,
)

# ---- Helpers ----


def _src(
    *,
    platform: SourcePlatform = SourcePlatform.DISCORD,
    channel: str | None = "general",
    conversation_id: str | None = "conv-1",
    interlocutor: str | None = "alice",
    author: Authorship = Authorship.SELF,
) -> FragmentSource:
    """Build a FragmentSource with chat-friendly defaults."""
    return FragmentSource(
        platform=platform,
        channel=channel,
        conversation_id=conversation_id,
        interlocutor=interlocutor,
        author=author,
    )


def _frag(
    *,
    id_: str,
    title: str,
    minute: int,
    level: FragmentLevel = "document",
    source: FragmentSource | None = None,
    base: datetime | None = None,
) -> Fragment:
    """Build a Fragment at a specific minute offset from ``base``."""
    when = (base or datetime(2025, 5, 1, 12, 0, tzinfo=UTC)) + timedelta(minutes=minute)
    return Fragment(
        id=id_,
        title=title,
        source=source or _src(),
        created=when,
        level=level,
    )


def _fake_embedder(vectors: dict[str, list[float]]):
    """Build a deterministic embedder from a title → vector mapping."""

    def _embed(texts: list[str]) -> list[list[float]]:
        return [vectors[t] for t in texts]

    return _embed


# ---- AggregationConfig defaults ----


class TestAggregationConfigDefaults:
    """Defaults must match the values published in the FEAT-022 spec."""

    def test_exchange_gap_default(self) -> None:
        """Default exchange_max_gap_minutes is 30."""
        assert AggregationConfig().exchange_max_gap_minutes == 30

    def test_burst_similarity_default(self) -> None:
        """Default burst_similarity_threshold is 0.7."""
        assert AggregationConfig().burst_similarity_threshold == pytest.approx(0.7)

    def test_session_gap_default(self) -> None:
        """Default session_max_gap_minutes is 360 (6 hours)."""
        assert AggregationConfig().session_max_gap_minutes == 360

    def test_joiner_default(self) -> None:
        """Default joiner is a blank-line separator."""
        assert AggregationConfig().joiner == "\n\n"

    def test_embedder_default(self) -> None:
        """Embedder is unset by default — burst transitions must inject."""
        assert AggregationConfig().embedder is None


# ---- Empty / passthrough ----


class TestEmptyAndPassthrough:
    """Edge cases for the eligibility partition."""

    def test_empty_input_returns_empty(self) -> None:
        """Empty input returns an empty list at every level."""
        cfg = AggregationConfig()
        assert aggregate([], level="exchange", config=cfg) == []
        assert aggregate([], level="burst", config=cfg) == []
        assert aggregate([], level="session", config=cfg) == []

    def test_session_level_input_returns_unchanged(self) -> None:
        """Session-level fragments are terminal.

        Mechanism: ``_SOURCE_LEVELS["session"] == "burst"``, so a fragment
        already at ``session`` fails the eligibility check and falls into
        the passthrough partition. The "session is terminal" guarantee is
        a consequence of that source-level mismatch, not a special-cased
        branch in :func:`aggregate`.
        """
        cfg = AggregationConfig()
        sessions = [
            _frag(id_="s1", title="session 1", minute=0, level="session"),
            _frag(id_="s2", title="session 2", minute=10, level="session"),
        ]
        result = aggregate(sessions, level="session", config=cfg)
        assert result == sessions

    def test_input_at_unrelated_level_passes_through(self) -> None:
        """Mismatched level on every fragment returns inputs unchanged."""
        cfg = AggregationConfig()
        frags = [_frag(id_="d1", title="doc", minute=0, level="document")]
        # Asking for burst but only have documents → passthrough.
        result = aggregate(frags, level="burst", config=cfg)
        assert result == frags

    def test_mixed_eligible_and_passthrough(self) -> None:
        """Pass-through fragments accompany aggregated parents in the output."""
        cfg = AggregationConfig()
        docs = [
            _frag(id_="d1", title="hi", minute=0, level="document"),
            _frag(id_="d2", title="there", minute=5, level="document"),
        ]
        burst_input = _frag(id_="b1", title="burst", minute=0, level="burst")
        result = aggregate([*docs, burst_input], level="exchange", config=cfg)
        # One exchange parent + the burst pass-through.
        assert len(result) == 2
        assert result[-1] is burst_input


# ---- Exchange transition ----


class TestExchangeTransition:
    """document → exchange grouping (gap + ≤2 speakers)."""

    def test_two_speakers_within_gap_form_one_exchange(self) -> None:
        """A short back-and-forth between two speakers becomes one exchange."""
        cfg = AggregationConfig(exchange_max_gap_minutes=30)
        src_self = _src(author=Authorship.SELF, interlocutor="alice")
        src_other = _src(author=Authorship.OTHER, interlocutor="alice")
        docs = [
            _frag(id_="d1", title="hello", minute=0, source=src_self),
            _frag(id_="d2", title="hi", minute=5, source=src_other),
            _frag(id_="d3", title="how are you?", minute=10, source=src_self),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.level == "exchange"
        assert parent.child_ids == ["d1", "d2", "d3"]
        assert parent.title == "hello\n\nhi\n\nhow are you?"

    def test_gap_exceeded_starts_new_exchange(self) -> None:
        """A gap above the threshold breaks the exchange."""
        cfg = AggregationConfig(exchange_max_gap_minutes=10)
        src_self = _src(author=Authorship.SELF, interlocutor="alice")
        src_other = _src(author=Authorship.OTHER, interlocutor="alice")
        docs = [
            _frag(id_="d1", title="hi", minute=0, source=src_self),
            _frag(id_="d2", title="hey", minute=5, source=src_other),
            _frag(id_="d3", title="back later", minute=100, source=src_self),
        ]
        parents = aggregate(docs, level="exchange", config=cfg)
        assert [p.child_ids for p in parents] == [["d1", "d2"], ["d3"]]

    def test_gap_equal_to_threshold_keeps_group(self) -> None:
        """The gap ceiling is inclusive: exactly-threshold gaps stay grouped."""
        cfg = AggregationConfig(exchange_max_gap_minutes=10)
        docs = [
            _frag(id_="d1", title="a", minute=0),
            _frag(id_="d2", title="b", minute=10),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.child_ids == ["d1", "d2"]

    def test_third_speaker_starts_new_exchange(self) -> None:
        """Introducing a third speaker forces a new exchange group."""
        cfg = AggregationConfig(exchange_max_gap_minutes=120)
        a = _src(author=Authorship.SELF, interlocutor="alice")
        b = _src(author=Authorship.OTHER, interlocutor="alice")
        c = _src(author=Authorship.OTHER, interlocutor="bob")
        docs = [
            _frag(id_="d1", title="me", minute=0, source=a),
            _frag(id_="d2", title="alice", minute=5, source=b),
            _frag(id_="d3", title="bob", minute=10, source=c),
        ]
        parents = aggregate(docs, level="exchange", config=cfg)
        assert [p.child_ids for p in parents] == [["d1", "d2"], ["d3"]]

    def test_single_document_rolls_up_into_one_exchange(self) -> None:
        """A solo message still produces an exchange parent (1-child)."""
        cfg = AggregationConfig()
        docs = [_frag(id_="d1", title="lonely", minute=0)]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.child_ids == ["d1"]
        assert parent.level == "exchange"

    def test_exchange_parent_inherits_source_channel(self) -> None:
        """The parent's source carries the children's channel/platform."""
        cfg = AggregationConfig()
        src = _src(channel="creek-dev", platform=SourcePlatform.DISCORD)
        docs = [_frag(id_="d1", title="hi", minute=0, source=src)]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.source.channel == "creek-dev"
        assert parent.source.platform == SourcePlatform.DISCORD.value

    def test_exchange_parent_promotes_author_to_collaborative(self) -> None:
        """Mixed-author exchanges set parent.source.author = COLLABORATIVE."""
        cfg = AggregationConfig(exchange_max_gap_minutes=30)
        a = _src(author=Authorship.SELF, interlocutor="alice")
        b = _src(author=Authorship.OTHER, interlocutor="alice")
        docs = [
            _frag(id_="d1", title="hi", minute=0, source=a),
            _frag(id_="d2", title="hey", minute=5, source=b),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.source.author == Authorship.COLLABORATIVE.value

    def test_exchange_parent_keeps_single_author_when_homogeneous(self) -> None:
        """A solo-self exchange keeps author=SELF (no spurious promotion)."""
        cfg = AggregationConfig()
        docs = [_frag(id_="d1", title="thinking", minute=0)]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.source.author == Authorship.SELF.value

    def test_exchange_parent_tags_include_daterange(self) -> None:
        """Parent tags carry the inclusive date-range of its children."""
        cfg = AggregationConfig(exchange_max_gap_minutes=30 * 24 * 60)
        base = datetime(2025, 5, 1, 12, 0, tzinfo=UTC)
        docs = [
            _frag(id_="d1", title="a", minute=0, base=base),
            _frag(id_="d2", title="b", minute=60, base=base),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert "daterange/2025-05-01_2025-05-01" in parent.tags
        assert "aggregated/exchange" in parent.tags

    def test_exchange_parent_interlocutor_joins_speakers(self) -> None:
        """The parent's interlocutor field surfaces every child's interlocutor."""
        cfg = AggregationConfig(exchange_max_gap_minutes=30)
        a = _src(author=Authorship.SELF, interlocutor="alice")
        b = _src(author=Authorship.SELF, interlocutor="bob")
        docs = [
            _frag(id_="d1", title="to alice", minute=0, source=a),
            _frag(id_="d2", title="to bob", minute=5, source=b),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert parent.source.interlocutor == "alice, bob"


# ---- Burst transition ----


class TestBurstTransition:
    """exchange → burst grouping via embedding similarity."""

    def test_high_similarity_collapses_to_one_burst(self) -> None:
        """Adjacent same-topic exchanges merge into one burst."""
        cfg = AggregationConfig(
            burst_similarity_threshold=0.7,
            embedder=_fake_embedder(
                {"a": [1.0, 0.0], "b": [0.99, 0.1], "c": [0.98, 0.2]},
            ),
        )
        exchanges = [
            _frag(id_="e1", title="a", minute=0, level="exchange"),
            _frag(id_="e2", title="b", minute=30, level="exchange"),
            _frag(id_="e3", title="c", minute=60, level="exchange"),
        ]
        [burst] = aggregate(exchanges, level="burst", config=cfg)
        assert burst.child_ids == ["e1", "e2", "e3"]
        assert burst.level == "burst"

    def test_low_similarity_splits_into_multiple_bursts(self) -> None:
        """Topic shift below threshold starts a new burst."""
        cfg = AggregationConfig(
            burst_similarity_threshold=0.7,
            embedder=_fake_embedder(
                {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [0.0, 1.0]},
            ),
        )
        exchanges = [
            _frag(id_="e1", title="a", minute=0, level="exchange"),
            _frag(id_="e2", title="b", minute=10, level="exchange"),
            _frag(id_="e3", title="c", minute=20, level="exchange"),
        ]
        parents = aggregate(exchanges, level="burst", config=cfg)
        assert [p.child_ids for p in parents] == [["e1"], ["e2", "e3"]]

    def test_similarity_equal_to_threshold_stays_in_burst(self) -> None:
        """Boundary: similarity exactly at threshold keeps the burst joined."""
        # cosine([1,0], [0.5, 0.866]) ≈ 0.5 — equality boundary.
        exchanges = [
            _frag(id_="e1", title="a", minute=0, level="exchange"),
            _frag(id_="e2", title="b", minute=10, level="exchange"),
        ]
        cfg = AggregationConfig(
            burst_similarity_threshold=_cosine([1.0, 0.0], [0.5, 0.8660254]),
            embedder=_fake_embedder({"a": [1.0, 0.0], "b": [0.5, 0.8660254]}),
        )
        [burst] = aggregate(exchanges, level="burst", config=cfg)
        assert burst.child_ids == ["e1", "e2"]

    def test_single_exchange_rolls_up_without_embedder(self) -> None:
        """A single-exchange burst skips the similarity check."""
        cfg = AggregationConfig(embedder=None)
        exchanges = [_frag(id_="e1", title="solo", minute=0, level="exchange")]
        [burst] = aggregate(exchanges, level="burst", config=cfg)
        assert burst.child_ids == ["e1"]
        assert burst.level == "burst"

    def test_missing_embedder_raises_on_multi_exchange_burst(self) -> None:
        """Calling burst with no embedder + >1 exchange is a contract error."""
        cfg = AggregationConfig(embedder=None)
        exchanges = [
            _frag(id_="e1", title="a", minute=0, level="exchange"),
            _frag(id_="e2", title="b", minute=10, level="exchange"),
        ]
        with pytest.raises(ValueError, match="embedder"):
            aggregate(exchanges, level="burst", config=cfg)


# ---- Session transition ----


class TestSessionTransition:
    """burst → session grouping by temporal gap."""

    def test_close_bursts_merge_into_one_session(self) -> None:
        """Bursts within the session window collapse into one session."""
        cfg = AggregationConfig(session_max_gap_minutes=120)
        bursts = [
            _frag(id_="b1", title="a", minute=0, level="burst"),
            _frag(id_="b2", title="b", minute=60, level="burst"),
            _frag(id_="b3", title="c", minute=180, level="burst"),
        ]
        [session] = aggregate(bursts, level="session", config=cfg)
        assert session.child_ids == ["b1", "b2", "b3"]
        assert session.level == "session"

    def test_large_gap_splits_into_two_sessions(self) -> None:
        """A gap above session_max_gap_minutes starts a new session."""
        cfg = AggregationConfig(session_max_gap_minutes=60)
        bursts = [
            _frag(id_="b1", title="a", minute=0, level="burst"),
            _frag(id_="b2", title="b", minute=30, level="burst"),
            _frag(id_="b3", title="c", minute=500, level="burst"),
        ]
        parents = aggregate(bursts, level="session", config=cfg)
        assert [p.child_ids for p in parents] == [["b1", "b2"], ["b3"]]

    def test_session_gap_equal_to_threshold_keeps_group(self) -> None:
        """Inclusive boundary: equal-to-threshold gaps stay in the session."""
        cfg = AggregationConfig(session_max_gap_minutes=60)
        bursts = [
            _frag(id_="b1", title="a", minute=0, level="burst"),
            _frag(id_="b2", title="b", minute=60, level="burst"),
        ]
        [session] = aggregate(bursts, level="session", config=cfg)
        assert session.child_ids == ["b1", "b2"]


# ---- Cross-source isolation ----


class TestCrossSourceIsolation:
    """Only fragments sharing a source key combine (v1 scope)."""

    def test_different_channels_do_not_merge(self) -> None:
        """Two channels in the same platform stay separate."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_a),
            _frag(id_="d2", title="y", minute=10, source=src_b),
        ]
        parents = aggregate(docs, level="exchange", config=cfg)
        assert len(parents) == 2
        assert {p.source.channel for p in parents} == {"alpha", "beta"}

    def test_different_platforms_do_not_merge(self) -> None:
        """Different platforms with the same channel stay separate."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_d = _src(platform=SourcePlatform.DISCORD, channel="dev")
        src_c = _src(platform=SourcePlatform.CLAUDE, channel="dev")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_d),
            _frag(id_="d2", title="y", minute=10, source=src_c),
        ]
        parents = aggregate(docs, level="exchange", config=cfg)
        assert len(parents) == 2


# ---- Idempotency ----


class TestIdempotency:
    """Re-running aggregation against identical input is deterministic."""

    def test_exchange_ids_stable_across_runs(self) -> None:
        """Running exchange aggregation twice yields identical IDs/content."""
        cfg = AggregationConfig(exchange_max_gap_minutes=30)
        docs = [
            _frag(id_="d1", title="a", minute=0),
            _frag(id_="d2", title="b", minute=10),
        ]
        first = aggregate(docs, level="exchange", config=cfg)
        second = aggregate(docs, level="exchange", config=cfg)
        assert [p.id for p in first] == [p.id for p in second]
        assert [p.title for p in first] == [p.title for p in second]
        assert [p.child_ids for p in first] == [p.child_ids for p in second]

    def test_full_roll_up_is_idempotent(self) -> None:
        """document→exchange→burst→session chain is reproducible end-to-end."""
        embedder = _fake_embedder({"hello": [1.0, 0.0]})
        cfg = AggregationConfig(embedder=embedder)
        docs = [_frag(id_="d1", title="hello", minute=0)]
        chain_1 = _roll_up(docs, cfg)
        chain_2 = _roll_up(docs, cfg)
        assert chain_1.id == chain_2.id
        assert chain_1.title == chain_2.title
        assert chain_1.child_ids == chain_2.child_ids


# ---- Single-message roll-up (acceptance criterion) ----


class TestSingleMessageRollUp:
    """A solo document rolls upward through exchange → burst → session."""

    def test_single_message_rolls_to_session(self) -> None:
        """Document → exchange → burst → session preserves the lineage."""
        embedder = _fake_embedder({"only": [1.0, 0.0]})
        cfg = AggregationConfig(embedder=embedder)
        docs = [_frag(id_="d1", title="only", minute=0)]
        [exchange] = aggregate(docs, level="exchange", config=cfg)
        [burst] = aggregate([exchange], level="burst", config=cfg)
        [session] = aggregate([burst], level="session", config=cfg)
        assert exchange.level == "exchange"
        assert burst.level == "burst"
        assert session.level == "session"
        assert exchange.child_ids == ["d1"]
        assert burst.child_ids == [exchange.id]
        assert session.child_ids == [burst.id]


# ---- Helpers (cosine, IDs, source keys) ----


class TestHelpers:
    """Internal helpers — independently tested for boundary coverage."""

    def test_cosine_orthogonal_is_zero(self) -> None:
        """Orthogonal vectors → cosine 0."""
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_cosine_parallel_is_one(self) -> None:
        """Parallel vectors → cosine 1."""
        assert _cosine([1.0, 2.0], [2.0, 4.0]) == pytest.approx(1.0)

    def test_cosine_zero_vector_returns_zero(self) -> None:
        """A zero vector cannot be normalised — return 0.0."""
        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert _cosine([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_aggregate_id_changes_with_child_order(self) -> None:
        """ID derivation factors child order — a reorder yields a new ID."""
        a = _aggregate_id("key", "exchange", ["c1", "c2"])
        b = _aggregate_id("key", "exchange", ["c2", "c1"])
        assert a != b

    def test_aggregate_id_changes_with_level(self) -> None:
        """Same children at different levels produce different parent IDs."""
        a = _aggregate_id("key", "exchange", ["c1"])
        b = _aggregate_id("key", "burst", ["c1"])
        assert a != b

    def test_source_key_none_fields_collapse_to_empty(self) -> None:
        """Missing channel/conversation_id collapse to empty placeholders."""
        source = FragmentSource(platform=SourcePlatform.JOURNAL)
        key = _source_key(source)
        assert key == "journal||"


# ---- Module-private helpers used in tests ----


def _roll_up(docs: list[Fragment], cfg: AggregationConfig) -> Fragment:
    """Roll a document chain to session, returning the final parent."""
    [exchange] = aggregate(docs, level="exchange", config=cfg)
    [burst] = aggregate([exchange], level="burst", config=cfg)
    [session] = aggregate([burst], level="session", config=cfg)
    return session


# ---- Cross-source aggregation (FEAT-027) ----


class TestCrossSourceDefault:
    """``cross_source`` defaults to False and preserves FEAT-022 behaviour."""

    def test_default_is_false_matches_single_source_behaviour(self) -> None:
        """Omitting the flag keeps two-source inputs split into two parents."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_a),
            _frag(id_="d2", title="y", minute=10, source=src_b),
        ]
        parents = aggregate(docs, level="exchange", config=cfg)
        assert len(parents) == 2

    def test_explicit_false_matches_default(self) -> None:
        """``cross_source=False`` yields the same parents as omitting it."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        docs = [
            _frag(id_="d1", title="x", minute=0, source=_src(channel="alpha")),
            _frag(id_="d2", title="y", minute=10, source=_src(channel="beta")),
        ]
        default = aggregate(docs, level="exchange", config=cfg)
        explicit = aggregate(docs, level="exchange", config=cfg, cross_source=False)
        assert [p.id for p in default] == [p.id for p in explicit]
        assert [p.child_ids for p in default] == [p.child_ids for p in explicit]

    def test_single_source_parents_omit_source_tag(self) -> None:
        """Single-source parents must NOT carry ``source/<key>`` tags."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        docs = [
            _frag(id_="d1", title="x", minute=0),
            _frag(id_="d2", title="y", minute=10),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg)
        assert not any(tag.startswith("source/") for tag in parent.tags)


class TestCrossSourceExchange:
    """``cross_source=True`` drops the source-identity gate at exchange level."""

    def test_two_channels_merge_into_one_exchange(self) -> None:
        """Two channels within gap form a single cross-source exchange."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_a = _src(channel="alpha", interlocutor="alice")
        src_b = _src(channel="beta", interlocutor="alice")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_a),
            _frag(id_="d2", title="y", minute=10, source=src_b),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert parent.child_ids == ["d1", "d2"]
        assert parent.level == "exchange"

    def test_two_platforms_merge_into_one_exchange(self) -> None:
        """Different platforms within gap form a single cross-source exchange."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_d = _src(platform=SourcePlatform.DISCORD, channel="dev")
        src_c = _src(platform=SourcePlatform.CLAUDE, channel="dev")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_d),
            _frag(id_="d2", title="y", minute=10, source=src_c),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert parent.child_ids == ["d1", "d2"]

    def test_parent_tags_record_every_contributing_source(self) -> None:
        """Parent tags list every distinct source key under ``source/...``."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_d = _src(platform=SourcePlatform.DISCORD, channel="dev")
        src_c = _src(platform=SourcePlatform.CLAUDE, channel="dev")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_d),
            _frag(id_="d2", title="y", minute=10, source=src_c),
        ]
        [parent] = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        source_tags = sorted(t for t in parent.tags if t.startswith("source/"))
        assert source_tags == [
            "source/claude|dev|conv-1",
            "source/discord|dev|conv-1",
        ]

    def test_children_retain_their_original_sources(self) -> None:
        """Provenance: child fragments are returned untouched (not mutated)."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_a),
            _frag(id_="d2", title="y", minute=10, source=src_b),
        ]
        aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert docs[0].source.channel == "alpha"
        assert docs[1].source.channel == "beta"

    def test_gap_still_breaks_cross_source_exchange(self) -> None:
        """Cross-source mode still honours ``exchange_max_gap_minutes``."""
        cfg = AggregationConfig(exchange_max_gap_minutes=10)
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        docs = [
            _frag(id_="d1", title="x", minute=0, source=src_a),
            _frag(id_="d2", title="y", minute=100, source=src_b),
        ]
        parents = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert [p.child_ids for p in parents] == [["d1"], ["d2"]]

    def test_speaker_rule_still_applies_across_sources(self) -> None:
        """≤2 speakers still holds even when sources are interleaved."""
        cfg = AggregationConfig(exchange_max_gap_minutes=120)
        a = _src(channel="alpha", interlocutor="alice", author=Authorship.SELF)
        b = _src(channel="beta", interlocutor="alice", author=Authorship.OTHER)
        c = _src(channel="gamma", interlocutor="bob", author=Authorship.OTHER)
        docs = [
            _frag(id_="d1", title="me", minute=0, source=a),
            _frag(id_="d2", title="alice", minute=5, source=b),
            _frag(id_="d3", title="bob", minute=10, source=c),
        ]
        parents = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert [p.child_ids for p in parents] == [["d1", "d2"], ["d3"]]


class TestCrossSourceSession:
    """Session-level cross-source merging is purely time-gap based."""

    def test_close_cross_source_bursts_merge_into_one_session(self) -> None:
        """Bursts from two sources within the window collapse to one session."""
        cfg = AggregationConfig(session_max_gap_minutes=120)
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        bursts = [
            _frag(id_="b1", title="a", minute=0, level="burst", source=src_a),
            _frag(id_="b2", title="b", minute=60, level="burst", source=src_b),
            _frag(id_="b3", title="c", minute=180, level="burst", source=src_a),
        ]
        [session] = aggregate(bursts, level="session", config=cfg, cross_source=True)
        assert session.child_ids == ["b1", "b2", "b3"]
        assert session.level == "session"


class TestCrossSourceBurst:
    """Burst-level cross-source merging still requires topical continuity."""

    def test_high_similarity_collapses_cross_source_exchanges(self) -> None:
        """Same-topic exchanges from two sources merge into one burst."""
        cfg = AggregationConfig(
            burst_similarity_threshold=0.7,
            embedder=_fake_embedder(
                {"a": [1.0, 0.0], "b": [0.99, 0.1]},
            ),
        )
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        exchanges = [
            _frag(id_="e1", title="a", minute=0, level="exchange", source=src_a),
            _frag(id_="e2", title="b", minute=30, level="exchange", source=src_b),
        ]
        [burst] = aggregate(exchanges, level="burst", config=cfg, cross_source=True)
        assert burst.child_ids == ["e1", "e2"]


class TestCrossSourceIdempotency:
    """FEAT-027 cross-source aggregation is deterministic across runs."""

    def test_cross_source_ids_stable_across_runs(self) -> None:
        """Re-running cross-source aggregation yields identical parent IDs."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        docs = [
            _frag(id_="d1", title="x", minute=0, source=_src(channel="alpha")),
            _frag(id_="d2", title="y", minute=10, source=_src(channel="beta")),
        ]
        first = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        second = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert [p.id for p in first] == [p.id for p in second]
        assert [p.child_ids for p in first] == [p.child_ids for p in second]
        assert [p.tags for p in first] == [p.tags for p in second]

    def test_cross_source_id_differs_from_single_source(self) -> None:
        """Same children grouped cross-source carry a different parent ID."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        docs = [
            _frag(id_="d1", title="x", minute=0),
            _frag(id_="d2", title="y", minute=10),
        ]
        [single] = aggregate(docs, level="exchange", config=cfg, cross_source=False)
        [cross] = aggregate(docs, level="exchange", config=cfg, cross_source=True)
        assert single.id != cross.id

    def test_cross_source_orders_chronologically_regardless_of_input(self) -> None:
        """Permuted input still produces a chronologically-ordered parent."""
        cfg = AggregationConfig(exchange_max_gap_minutes=60)
        d1 = _frag(id_="d1", title="x", minute=0, source=_src(channel="alpha"))
        d2 = _frag(id_="d2", title="y", minute=10, source=_src(channel="beta"))
        ordered = aggregate([d1, d2], level="exchange", config=cfg, cross_source=True)
        reversed_ = aggregate([d2, d1], level="exchange", config=cfg, cross_source=True)
        assert [p.id for p in ordered] == [p.id for p in reversed_]
        assert [p.child_ids for p in ordered] == [p.child_ids for p in reversed_]


class TestCrossSourceHelpers:
    """Direct tests for the FEAT-027 helper functions."""

    def test_contributing_source_keys_returns_sorted_unique(self) -> None:
        """The helper deduplicates and sorts contributing source keys."""
        src_a = _src(channel="alpha")
        src_b = _src(channel="beta")
        children = [
            _frag(id_="d1", title="x", minute=0, source=src_a),
            _frag(id_="d2", title="y", minute=5, source=src_b),
            _frag(id_="d3", title="z", minute=10, source=src_a),
        ]
        keys = _contributing_source_keys(children)
        assert keys == [
            "discord|alpha|conv-1",
            "discord|beta|conv-1",
        ]

    def test_parent_source_key_single_source_uses_source_key(self) -> None:
        """Single-source mode reuses ``_source_key`` of the first child."""
        children = [_frag(id_="d1", title="x", minute=0)]
        key = _parent_source_key(children, cross_source=False)
        assert key == _source_key(children[0].source)

    def test_parent_source_key_cross_source_uses_canonical_prefix(self) -> None:
        """Cross-source mode prefixes with ``cross-source:`` for stable IDs."""
        children = [
            _frag(id_="d1", title="x", minute=0, source=_src(channel="alpha")),
            _frag(id_="d2", title="y", minute=10, source=_src(channel="beta")),
        ]
        key = _parent_source_key(children, cross_source=True)
        assert key.startswith("cross-source:")
        assert "discord|alpha|conv-1" in key
        assert "discord|beta|conv-1" in key


# ---- Property test (FEAT-027 acceptance criterion) ----


class TestCrossSourceMergeInvariant:
    """At session level the cross-source mode can only merge, never split.

    Session grouping is purely time-gap based — no speaker rule, no
    similarity gate — so dropping the source-identity boundary always
    yields ≤ the number of single-source parents. This is the strongest
    direction of the FEAT-027 invariant that holds independent of input
    shape, and the acceptance-criterion property test relies on it.
    """

    @pytest.mark.parametrize(
        ("layout", "session_max_gap_minutes"),
        [
            # Two sources, small gap → cross merges, single keeps split.
            ([(0, "alpha"), (30, "beta")], 60),
            # Three sources, all close → cross merges to 1, single keeps 3.
            ([(0, "alpha"), (10, "beta"), (20, "gamma")], 60),
            # Two sources, alternating timestamps → cross merges to 1.
            (
                [(0, "alpha"), (15, "beta"), (30, "alpha"), (45, "beta")],
                60,
            ),
            # Wide gaps → both modes produce the same parents.
            (
                [(0, "alpha"), (500, "alpha"), (1000, "beta")],
                60,
            ),
            # Single source → cross_source has no effect.
            ([(0, "alpha"), (5, "alpha"), (10, "alpha")], 60),
        ],
    )
    def test_cross_source_never_produces_more_session_parents(
        self,
        layout: list[tuple[int, str]],
        session_max_gap_minutes: int,
    ) -> None:
        """Across diverse layouts, cross-source ≤ single-source parent count."""
        cfg = AggregationConfig(session_max_gap_minutes=session_max_gap_minutes)
        bursts = [
            _frag(
                id_=f"b{i}",
                title=f"t{i}",
                minute=minute,
                level="burst",
                source=_src(channel=channel),
            )
            for i, (minute, channel) in enumerate(layout)
        ]
        single = aggregate(bursts, level="session", config=cfg, cross_source=False)
        cross = aggregate(bursts, level="session", config=cfg, cross_source=True)
        assert len(cross) <= len(single)

    def test_cross_source_idempotent_under_session_invariant(self) -> None:
        """Re-running cross-source twice yields the same parent count."""
        cfg = AggregationConfig(session_max_gap_minutes=60)
        bursts = [
            _frag(
                id_=f"b{i}",
                title=f"t{i}",
                minute=i * 10,
                level="burst",
                source=_src(channel=("alpha" if i % 2 == 0 else "beta")),
            )
            for i in range(6)
        ]
        run1 = aggregate(bursts, level="session", config=cfg, cross_source=True)
        run2 = aggregate(bursts, level="session", config=cfg, cross_source=True)
        assert len(run1) == len(run2)
        assert [p.id for p in run1] == [p.id for p in run2]
