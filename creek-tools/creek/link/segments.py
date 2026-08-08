"""Clustering-domain partitioning for continuous message streams.

Both detectors in this package assume their input corpus has *shape*:
:class:`~creek.link.eddies.EddyDetector` builds a single epsilon-graph
and expands each cluster by unbounded transitive density-reachability,
and :class:`~creek.link.threads.ThreadDetector` takes the transitive
closure of overlapping time windows through a union-find. Both are
correct on a corpus whose clusters are separated by low-density
regions. A chat stream is not such a corpus. Short messages from one
channel are semantically homogeneous and temporally continuous, so the
epsilon-graph over them is one connected component and the sliding
windows chain end to end — the entire stream collapses into a single
mega-cluster whatever threshold is chosen. There is no separator to
find, so no threshold value fixes it.

This module removes the precondition violation instead of tuning around
it. Before any similarity graph is built, the corpus is partitioned into
independent **clustering domains**:

* A fragment whose ``source.platform`` is one of the configured
  ``stream_platforms`` belongs to a *conversation episode*, keyed on
  ``(platform, series, episode_index)``. ``series`` is the channel, or
  the conversation id, or the interlocutor — the first one the source
  records. The series is then cut into episodes by an inactivity gap
  (the primary, conversational rule: people stop talking, and that
  silence is the real topic boundary) with a span backstop (so a
  permanently-busy channel that never falls idle still yields
  channel-month units rather than one multi-year blob).
* Every other fragment — essays, journal entries, chat *transcripts*,
  notes — lands in one shared domain, so cross-source resonance,
  cross-platform eddies and multi-year threads over long-form material
  keep working exactly as before. Segmentation is deliberately narrow:
  it touches only the corpus shape that breaks the algorithms.

Partitioning strictly *reduces* work. Clustering ``d`` domains of
``n / d`` fragments each costs ``O(n^2 / d)`` in
:func:`~creek.link.neighbours.cosine_neighbours` and in the thread
detector's windowed inner loop, against ``O(n^2)`` for the undivided
corpus — so it relieves the quadratic-cost pressure on large vaults
rather than adding to it.

Time-bucketing routes through :func:`creek.time.effective_authored_at`
so episode boundaries follow source time (a Discord message's own
timestamp) and not the wall-clock moment of a bulk ingest.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final, Protocol

from creek.time import effective_authored_at

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from creek.models import Fragment

SHARED_DOMAIN_KEY: Final[str] = "shared"
"""Domain key carrying every fragment that is not part of a message stream."""

_STREAM_KEY_PREFIX: Final[str] = "stream:"
"""Prefix distinguishing a stream episode key from :data:`SHARED_DOMAIN_KEY`."""


class SegmentationConfig(Protocol):
    """The linking-config attributes :func:`partition` reads.

    Declared structurally so this module stays independent of the
    settings tree: :class:`creek.config.LinkingConfig` satisfies it, and
    so does any small stand-in a caller or test supplies.
    """

    @property
    def stream_platforms(self) -> Sequence[str]:
        """Platform values whose fragments are segmented into episodes."""

    @property
    def stream_episode_max_gap_hours(self) -> int:
        """Inactivity gap that ends an episode. Inclusive boundary."""

    @property
    def stream_episode_max_span_days(self) -> int:
        """Maximum span of a single episode. Inclusive boundary."""


def _is_stream(fragment: Fragment, stream_platforms: Sequence[str]) -> bool:
    """Return whether *fragment* came from a configured stream platform.

    Args:
        fragment: The fragment to test.
        stream_platforms: Platform values treated as message streams.

    Returns:
        ``True`` when the fragment's source platform is listed.
    """
    return str(fragment.source.platform) in stream_platforms


def _series_identity(fragment: Fragment) -> tuple[str, str]:
    """Return the ``(platform, series)`` identity of a stream fragment.

    The series is the first populated one of ``channel``,
    ``conversation_id`` and ``interlocutor``; a source that records none
    of them yields an empty series, which groups its fragments together
    under the platform alone rather than scattering them.

    Args:
        fragment: The fragment to identify.

    Returns:
        The platform value and the series identity, both as plain strings.
    """
    source = fragment.source
    series = source.channel or source.conversation_id or source.interlocutor or ""
    return str(source.platform), series


def domain_key(
    fragment: Fragment,
    *,
    stream_platforms: Sequence[str],
    episode_index: int = 0,
) -> str:
    """Return the clustering-domain key for *fragment*.

    Args:
        fragment: The fragment to key.
        stream_platforms: Platform values treated as message streams.
        episode_index: Zero-based position of the episode this fragment
            belongs to within its series. Ignored for non-stream
            fragments, which never carry an episode.

    Returns:
        :data:`SHARED_DOMAIN_KEY` for a non-stream fragment, otherwise a
        ``stream:<platform>:<series>:<episode_index>`` key. The episode
        index is the final component, so a series identity containing
        ``":"`` cannot collide with another series.
    """
    if not _is_stream(fragment, stream_platforms):
        return SHARED_DOMAIN_KEY
    platform, series = _series_identity(fragment)
    return f"{_STREAM_KEY_PREFIX}{platform}:{series}:{episode_index}"


def _episodes(
    ordered: Sequence[Fragment],
    *,
    max_gap: timedelta,
    max_span: timedelta,
) -> list[list[Fragment]]:
    """Cut one time-ordered series into conversation episodes.

    A new episode starts when the gap since the previous fragment
    exceeds *max_gap* (the conversational rule), or when adding the
    fragment would push the current episode's span past *max_span* (the
    backstop for a channel that never falls idle). Both boundaries are
    inclusive: a gap or span exactly equal to the limit does not cut.

    Args:
        ordered: Fragments of a single series, ascending by effective
            authored time.
        max_gap: Inactivity gap that ends an episode.
        max_span: Maximum span from an episode's first fragment.

    Returns:
        The episodes, in chronological order. Empty for empty input.
    """
    if not ordered:
        return []
    episodes: list[list[Fragment]] = []
    current: list[Fragment] = [ordered[0]]
    started_at = effective_authored_at(ordered[0])
    previous_at = started_at
    for fragment in ordered[1:]:
        authored_at = effective_authored_at(fragment)
        if authored_at - previous_at > max_gap or authored_at - started_at > max_span:
            episodes.append(current)
            current = []
            started_at = authored_at
        current.append(fragment)
        previous_at = authored_at
    episodes.append(current)
    return episodes


def partition(
    fragments: Iterable[Fragment],
    *,
    config: SegmentationConfig,
) -> list[list[Fragment]]:
    """Partition a corpus into independent clustering domains.

    Message-platform fragments are grouped by ``(platform, series)`` and
    cut into conversation episodes; every other fragment lands in one
    shared domain. The result is a true partition — each input fragment
    appears in exactly one domain, and no empty domain is emitted — so a
    caller may cluster each domain independently and concatenate the
    results without losing or duplicating anything.

    Args:
        fragments: The corpus. Any iterable; consumed once.
        config: Supplies ``stream_platforms``,
            ``stream_episode_max_gap_hours`` and
            ``stream_episode_max_span_days``.

    Returns:
        One list of fragments per domain, ordered by domain key so the
        result does not depend on input ordering. Stream domains are
        ordered internally by effective authored time; the shared domain
        preserves input order.
    """
    stream_platforms = tuple(config.stream_platforms)
    shared: list[Fragment] = []
    series: dict[tuple[str, str], list[Fragment]] = {}
    for fragment in fragments:
        if _is_stream(fragment, stream_platforms):
            series.setdefault(_series_identity(fragment), []).append(fragment)
        else:
            shared.append(fragment)

    max_gap = timedelta(hours=config.stream_episode_max_gap_hours)
    max_span = timedelta(days=config.stream_episode_max_span_days)
    domains: dict[str, list[Fragment]] = {}
    if shared:
        domains[SHARED_DOMAIN_KEY] = shared
    for members in series.values():
        ordered = sorted(members, key=effective_authored_at)
        episodes = _episodes(ordered, max_gap=max_gap, max_span=max_span)
        for index, episode in enumerate(episodes):
            key = domain_key(
                episode[0],
                stream_platforms=stream_platforms,
                episode_index=index,
            )
            domains[key] = episode
    return [domains[key] for key in sorted(domains)]
