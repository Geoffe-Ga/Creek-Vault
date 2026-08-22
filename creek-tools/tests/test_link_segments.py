"""Tests for creek.link.segments — clustering-domain partitioning (issue #880).

The partitioner is the root fix for the mega-cluster degeneration: a
continuous chat stream violates DBSCAN's "clusters separated by
low-density regions" precondition, so the stream is cut into
conversation episodes *before* any eps-graph is built. These tests pin
the partition contract itself — channel separation, the inactivity-gap
boundary, the span backstop, the single shared non-stream domain, and
the invariant that partitioning never drops or duplicates a fragment.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from creek.link.segments import (
    SHARED_DOMAIN_KEY,
    _episodes,
    domain_key,
    partition,
)
from creek.models import (
    Fragment,
    FragmentSource,
    SourcePlatform,
)

_BASE = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Segmentation:
    """Minimal stand-in for the ``linking`` config block.

    ``partition`` reads three attributes structurally, so this frozen
    dataclass satisfies the same protocol
    :class:`~creek.config.LinkingConfig` does without dragging the whole
    settings tree into a unit test.
    """

    stream_platforms: tuple[str, ...] = ("discord", "email")
    stream_episode_max_gap_hours: int = 24
    stream_episode_max_span_days: int = 30


def _fragment(
    fid: str,
    *,
    platform: SourcePlatform = SourcePlatform.DISCORD,
    channel: str | None = None,
    conversation_id: str | None = None,
    interlocutor: str | None = None,
    authored_at: datetime = _BASE,
) -> Fragment:
    """Build a Fragment with the source identity the partitioner keys on.

    ``authored_at`` is set explicitly (not mirrored through ``ingested``)
    because :func:`creek.time.effective_authored_at` prefers it, and
    every episode boundary in these tests is expressed in source time.
    """
    return Fragment(
        id=fid,
        title="messages",
        source=FragmentSource(
            platform=platform,
            channel=channel,
            conversation_id=conversation_id,
            interlocutor=interlocutor,
        ),
        created=authored_at,
        ingested=authored_at,
        authored_at=authored_at,
    )


def _ids(domains: list[list[Fragment]]) -> list[set[str]]:
    """Return one id-set per domain, for order-insensitive comparison."""
    return [{frag.id for frag in domain} for domain in domains]


# ---- domain_key ----


class TestDomainKey:
    """Tests for the per-fragment clustering-domain key."""

    def test_non_stream_fragment_maps_to_the_shared_domain(self) -> None:
        """A platform outside ``stream_platforms`` gets the shared key."""
        frag = _fragment("a", platform=SourcePlatform.CLAUDE)
        assert (
            domain_key(frag, stream_platforms=("discord", "email")) == SHARED_DOMAIN_KEY
        )

    def test_stream_key_carries_platform_series_and_episode(self) -> None:
        """A stream fragment's key names platform, series and episode."""
        frag = _fragment("a", channel="general")
        key = domain_key(frag, stream_platforms=("discord",), episode_index=7)
        assert key != SHARED_DOMAIN_KEY
        assert "discord" in key
        assert "general" in key
        assert key.endswith("7")

    def test_episode_index_defaults_to_zero(self) -> None:
        """Omitting ``episode_index`` names the first episode."""
        frag = _fragment("a", channel="general")
        assert domain_key(frag, stream_platforms=("discord",)) == domain_key(
            frag,
            stream_platforms=("discord",),
            episode_index=0,
        )

    def test_channel_falls_back_to_conversation_id(self) -> None:
        """With no ``channel``, the series identity is ``conversation_id``."""
        frag = _fragment("a", channel=None, conversation_id="dm-42")
        assert "dm-42" in domain_key(frag, stream_platforms=("discord",))

    def test_conversation_id_falls_back_to_interlocutor(self) -> None:
        """With neither channel nor conversation, ``interlocutor`` is used."""
        frag = _fragment("a", channel=None, interlocutor="alex")
        assert "alex" in domain_key(frag, stream_platforms=("discord",))

    def test_no_series_identity_still_yields_a_stream_key(self) -> None:
        """A stream fragment with no identity fields still keys as a stream."""
        frag = _fragment("a")
        key = domain_key(frag, stream_platforms=("discord",))
        assert key != SHARED_DOMAIN_KEY
        assert "discord" in key

    def test_platform_absent_from_stream_platforms_is_shared(self) -> None:
        """A Discord fragment is shared when Discord is not configured."""
        frag = _fragment("a", channel="general")
        assert domain_key(frag, stream_platforms=("email",)) == SHARED_DOMAIN_KEY


# ---- partition: shared (non-stream) domain ----


class TestSharedDomain:
    """Tests that every non-stream fragment stays in one shared domain."""

    def test_empty_input_yields_no_domains(self) -> None:
        """An empty corpus partitions into zero domains."""
        assert partition([], config=_Segmentation()) == []

    def test_non_stream_platforms_share_one_domain(self) -> None:
        """Claude, journal and essay fragments land in a single domain.

        This is the property that keeps cross-source threads and
        cross-platform eddies working, and the reason every existing
        detection test — whose fixtures use ``SourcePlatform.CLAUDE`` —
        is untouched by segmentation.
        """
        frags = [
            _fragment("a", platform=SourcePlatform.CLAUDE),
            _fragment("b", platform=SourcePlatform.JOURNAL),
            _fragment("c", platform=SourcePlatform.ESSAY),
        ]
        domains = partition(frags, config=_Segmentation())
        assert len(domains) == 1
        assert _ids(domains) == [{"a", "b", "c"}]

    def test_non_stream_fragments_are_never_split_by_time(self) -> None:
        """Years apart, non-stream fragments still share one domain."""
        frags = [
            _fragment("a", platform=SourcePlatform.CLAUDE, authored_at=_BASE),
            _fragment(
                "b",
                platform=SourcePlatform.CLAUDE,
                authored_at=_BASE + timedelta(days=2000),
            ),
        ]
        assert len(partition(frags, config=_Segmentation())) == 1

    def test_stream_platform_list_is_honoured(self) -> None:
        """Emptying ``stream_platforms`` collapses everything to one domain."""
        frags = [
            _fragment("a", channel="general"),
            _fragment(
                "b",
                channel="general",
                authored_at=_BASE + timedelta(days=400),
            ),
        ]
        domains = partition(frags, config=_Segmentation(stream_platforms=()))
        assert _ids(domains) == [{"a", "b"}]


# ---- partition: stream segmentation ----


class TestStreamSegmentation:
    """Tests for channel separation and episode cutting."""

    def test_stream_fragments_partition_by_channel_and_inactivity_gap(
        self,
    ) -> None:
        """Three channels, each with one multi-day gap, yield six domains."""
        frags: list[Fragment] = []
        for channel in ("general", "art", "music"):
            for index, offset in enumerate(
                (timedelta(0), timedelta(hours=1), timedelta(days=9)),
            ):
                frags.append(
                    _fragment(
                        f"{channel}-{index}",
                        channel=channel,
                        authored_at=_BASE + offset,
                    ),
                )
        domains = partition(frags, config=_Segmentation())
        assert len(domains) == 6
        assert {"general-0", "general-1"} in _ids(domains)
        assert {"general-2"} in _ids(domains)
        assert sum(len(domain) for domain in domains) == len(frags)

    def test_gap_equal_to_max_gap_does_not_cut(self) -> None:
        """The inactivity boundary is inclusive: a gap of exactly max stays."""
        frags = [
            _fragment("a", channel="general", authored_at=_BASE),
            _fragment(
                "b",
                channel="general",
                authored_at=_BASE + timedelta(hours=24),
            ),
        ]
        assert _ids(partition(frags, config=_Segmentation())) == [{"a", "b"}]

    def test_gap_one_second_beyond_max_gap_cuts(self) -> None:
        """One second past the inactivity boundary starts a new episode."""
        frags = [
            _fragment("a", channel="general", authored_at=_BASE),
            _fragment(
                "b",
                channel="general",
                authored_at=_BASE + timedelta(hours=24, seconds=1),
            ),
        ]
        assert _ids(partition(frags, config=_Segmentation())) == [{"a"}, {"b"}]

    def test_same_channel_name_on_two_platforms_stays_separate(self) -> None:
        """Series identity includes the platform, not just the channel."""
        frags = [
            _fragment("a", platform=SourcePlatform.DISCORD, channel="general"),
            _fragment("b", platform=SourcePlatform.EMAIL, channel="general"),
        ]
        domains = partition(frags, config=_Segmentation())
        assert len(domains) == 2

    def test_series_identity_falls_through_to_interlocutor(self) -> None:
        """Two DM partners with no channel form two distinct series."""
        frags = [
            _fragment("a", interlocutor="alex"),
            _fragment("b", interlocutor="robin"),
        ]
        assert len(partition(frags, config=_Segmentation())) == 2

    def test_episode_members_are_time_ordered(self) -> None:
        """Within an episode, fragments are sorted by effective authored time."""
        frags = [
            _fragment(
                "late",
                channel="general",
                authored_at=_BASE + timedelta(hours=2),
            ),
            _fragment("early", channel="general", authored_at=_BASE),
        ]
        domains = partition(frags, config=_Segmentation())
        assert [frag.id for frag in domains[0]] == ["early", "late"]


# ---- partition: span backstop ----


class TestSpanBackstop:
    """Tests for the span backstop on a permanently-busy channel."""

    def test_busy_channel_is_cut_into_span_sized_episodes(self) -> None:
        """A channel with no idle gap still yields bounded episodes.

        Ninety days of traffic every twelve hours never trips the
        24-hour inactivity rule, so without the backstop the whole run
        would be one domain — exactly the seven-year blob issue #880
        reports. The 30-day span cap splits it into three.
        """
        frags = [
            _fragment(
                f"m{index}",
                channel="general",
                authored_at=_BASE + timedelta(hours=12 * index),
            )
            for index in range(181)
        ]
        domains = partition(frags, config=_Segmentation())
        assert len(domains) == 3
        assert sum(len(domain) for domain in domains) == 181
        assert max(len(domain) for domain in domains) == 61

    def test_span_equal_to_max_span_does_not_cut(self) -> None:
        """The span boundary is inclusive: exactly max span stays together."""
        config = _Segmentation(stream_episode_max_gap_hours=24 * 365)
        frags = [
            _fragment("a", channel="general", authored_at=_BASE),
            _fragment(
                "b",
                channel="general",
                authored_at=_BASE + timedelta(days=30),
            ),
        ]
        assert _ids(partition(frags, config=config)) == [{"a", "b"}]

    def test_span_one_second_beyond_max_span_cuts(self) -> None:
        """One second past the span boundary starts a new episode."""
        config = _Segmentation(stream_episode_max_gap_hours=24 * 365)
        frags = [
            _fragment("a", channel="general", authored_at=_BASE),
            _fragment(
                "b",
                channel="general",
                authored_at=_BASE + timedelta(days=30, seconds=1),
            ),
        ]
        assert _ids(partition(frags, config=config)) == [{"a"}, {"b"}]

    def test_span_is_measured_from_the_episode_start_not_the_corpus_start(
        self,
    ) -> None:
        """Each new episode restarts the span clock."""
        config = _Segmentation(stream_episode_max_gap_hours=24 * 365)
        frags = [
            _fragment(
                f"m{index}",
                channel="general",
                authored_at=_BASE + timedelta(days=20 * index),
            )
            for index in range(4)
        ]
        domains = partition(frags, config=config)
        assert _ids(domains) == [{"m0", "m1"}, {"m2", "m3"}]


# ---- episode cutting, directly ----


class TestEpisodes:
    """Tests for the episode cutter's own defensive contract."""

    def test_empty_series_yields_no_episodes(self) -> None:
        """An empty series produces no episode rather than raising.

        ``partition`` never builds an empty series, so this is a
        defensive guard — pinned here so a later refactor cannot turn it
        into an ``IndexError`` unnoticed.
        """
        assert (
            _episodes([], max_gap=timedelta(hours=24), max_span=timedelta(days=30))
            == []
        )


# ---- partition: structural invariants ----


class TestPartitionInvariants:
    """Tests that partitioning is a true partition of the input."""

    @staticmethod
    def _mixed_corpus() -> list[Fragment]:
        """Return a corpus mixing stream and non-stream fragments."""
        stream = [
            _fragment(
                f"s{index}",
                channel="general" if index % 2 else "art",
                authored_at=_BASE + timedelta(days=3 * index),
            )
            for index in range(12)
        ]
        shared = [
            _fragment("c0", platform=SourcePlatform.CLAUDE),
            _fragment("j0", platform=SourcePlatform.JOURNAL),
        ]
        return [*stream, *shared]

    def test_every_fragment_appears_in_exactly_one_domain(self) -> None:
        """No drops and no duplicates — a bug here deletes vault content."""
        frags = self._mixed_corpus()
        domains = partition(frags, config=_Segmentation())
        emitted = [frag.id for domain in domains for frag in domain]
        assert len(emitted) == len(frags)
        assert set(emitted) == {frag.id for frag in frags}

    def test_domains_are_never_empty(self) -> None:
        """A domain with no members is never emitted."""
        domains = partition(self._mixed_corpus(), config=_Segmentation())
        assert domains
        assert all(domain for domain in domains)

    def test_partition_is_deterministic_under_input_reordering(self) -> None:
        """Domain content does not depend on the input ordering."""
        frags = self._mixed_corpus()
        forward = _ids(partition(frags, config=_Segmentation()))
        reverse = _ids(partition(list(reversed(frags)), config=_Segmentation()))
        assert forward == reverse

    def test_partition_accepts_any_iterable(self) -> None:
        """A generator input is consumed exactly once and partitioned."""
        frags = self._mixed_corpus()
        domains = partition((frag for frag in frags), config=_Segmentation())
        assert sum(len(domain) for domain in domains) == len(frags)

    @pytest.mark.parametrize("platform", list(SourcePlatform))
    def test_every_platform_lands_somewhere(
        self,
        platform: SourcePlatform,
    ) -> None:
        """The partition is total over ``SourcePlatform`` — nothing vanishes."""
        frags = [_fragment("a", platform=platform, channel="general")]
        assert _ids(partition(frags, config=_Segmentation())) == [{"a"}]
