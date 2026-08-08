"""Reproduction corpus for issue #880 — message-stream cluster degeneration.

The existing scale fixture (``_clustered_corpus`` in
``tests/test_link_scale_790.py``) cannot reproduce this defect: it builds
one-hot vectors, so cross-cluster cosine is exactly ``0.0`` and transitive
density-bridging is arithmetically impossible; every cluster is exactly
``per_cluster`` members, so the largest-cluster share is fixed by
construction; every title is distinct, so the naming defect cannot fire; and
every fragment is ``Frequency.UNCLASSIFIED``, which ``_frequency_overlap``
discards, so thread detection yields nothing. A ceiling test written on top of
it would pass vacuously whether or not the bug exists.

This module builds the corpus the demo vault actually contains:

* A **random walk on the unit sphere** confined to the first ``_WALK_DIMS``
  dimensions. Consecutive messages sit at cosine ``~0.95``, well inside the
  ``0.70`` edge threshold implied by ``eps=0.3``, so every point is a DBSCAN
  core point and the epsilon-graph is a *single connected component with a
  large diameter* — a continuous semantic manifold with no low-density
  separator for DBSCAN to find. This is the real mechanism behind the 87%
  mega-eddy, not injected noise.
* The walk is **out-and-back**: it drifts away for the first half and returns
  along its own path for the second. Cosine drift from the earliest member
  therefore rises and then falls, so ``_spearman_with_time`` lands near zero
  and ``EddyDetector._has_temporal_direction`` does *not* reject the
  mega-cluster. A monotone walk would be filtered out and every downstream
  assertion would pass against zero eddies.
* Stream fragments all carry ``title="messages"`` verbatim (matching the
  30,795 Discord fragments on disk), ``platform=discord``, and a shared ``F2``
  frequency (matching the demo vault's ``frequency_affinity: [F2]``), so both
  the naming defect and the thread-union defect reproduce.
* Three **planted topics** sit on dimensions orthogonal to the entire walk
  subspace, interleaved into the same time range with real, distinct titles
  and their own frequencies. They must survive as separate eddies and threads,
  which is what stops "split everything harder" from looking like a fix.

Determinism comes from ``numpy.random.default_rng(_SEED)``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import NamedTuple
from unittest.mock import patch

import numpy as np

from creek.config import LinkingConfig
from creek.link.cluster_limits import SplitPolicy
from creek.link.eddies import EddyDetector, _cosine_distance, _spearman_with_time
from creek.link.segments import partition
from creek.link.threads import ThreadDetector
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)
from creek.time import effective_authored_at

# --- Detector settings under test (production defaults) ---------------------
_EPS = 0.3
_MIN_SAMPLES = 5
_CORRELATION_THRESHOLD = 0.3
_WINDOW_DAYS = 30
_SIMILARITY_THRESHOLD = 0.6

# --- Corpus geometry --------------------------------------------------------
_SEED = 880
_DIMS = 16
_TOPIC_DIMS = 3
_WALK_DIMS = _DIMS - _TOPIC_DIMS
_STREAM_FRAGMENTS = 600
#: Step length for the walk. ``cos(v_k, v_{k+1}) ~= 1/sqrt(1 + step**2)``,
#: i.e. ~0.95 — comfortably above the 0.70 edge threshold at ``eps=0.3``.
_WALK_STEP = 0.33
#: Tiny perturbation on the return leg so the inbound half is a near-retrace
#: rather than a literal mirror image of the outbound half.
_RETURN_JITTER = 0.02
_MESSAGE_SPACING_HOURS = 6
_CHANNEL_GAP_DAYS = 4
_CORPUS_START = datetime(2023, 1, 1, 9, 0)
_STREAM_TITLE = "messages"
_STREAM_FREQUENCY = Frequency.F2
_STREAM_CHANNELS = ("Aspiring Drag Queens", "Backyard Astronomy", "Sourdough Hours")
_TOPIC_MEMBERS = 12
_PLANTED_TOPICS = (
    ("harmonic analysis in cathedrals", Frequency.F5),
    ("tidepool taxonomy fieldnotes", Frequency.F7),
    ("estate planning for musicians", Frequency.F9),
)

# --- Fixture-validity bar ---------------------------------------------------
#: The single connected component must swallow at least 95% of the stream.
_DEGENERATE_COMPONENT_SHARE = 0.95

# --- Split policy under test ------------------------------------------------
#: Deliberately far below the 500 production default so the *fractional* term
#: governs on a 636-fragment corpus; the default ceiling of 500 exceeds the
#: whole stream, so the test could never fail with it.
_SPLIT_SIZE_CEILING = 50
_SPLIT_MAX_FRACTION = 0.10
_SPLIT_MAX_DEPTH = 3

# --- Single-episode variant -------------------------------------------------
#: One channel, no inter-channel gap, and hourly spacing so all 600 messages
#: span 25 days — inside both the 24-hour inactivity gap and the 30-day span
#: backstop. Segmentation therefore cannot cut it, leaving the size ceiling as
#: the only thing standing between the corpus and a 600-fragment mega-page.
_SINGLE_EPISODE_SPACING_HOURS = 1

#: How many of a planted topic's own words its generated title must carry
#: before the title counts as naming that topic rather than merely being
#: distinct from the others.
_MIN_SHARED_TITLE_TOKENS = 2


def _unit_vector(rng: np.random.Generator) -> np.ndarray:
    """Draw a uniformly random unit vector in the walk subspace.

    Args:
        rng: Seeded generator supplying the draw.

    Returns:
        A unit-norm array of length ``_WALK_DIMS``.
    """
    raw = rng.normal(size=_WALK_DIMS)
    return raw / np.linalg.norm(raw)


def _walk_step(rng: np.random.Generator, current: np.ndarray) -> np.ndarray:
    """Take one small random step away from *current* on the unit sphere.

    Args:
        rng: Seeded generator supplying the step direction.
        current: The unit vector to step away from.

    Returns:
        A new unit vector at cosine ``~0.95`` from *current*.
    """
    stepped = current + _WALK_STEP * _unit_vector(rng)
    return stepped / np.linalg.norm(stepped)


def _jittered(rng: np.random.Generator, vector: np.ndarray) -> np.ndarray:
    """Return *vector* nudged by a small random perturbation.

    Args:
        rng: Seeded generator supplying the perturbation.
        vector: The unit vector to nudge.

    Returns:
        A unit vector very close to *vector*.
    """
    nudged = vector + _RETURN_JITTER * rng.normal(size=_WALK_DIMS)
    return nudged / np.linalg.norm(nudged)


def _embed(walk_vector: np.ndarray) -> list[float]:
    """Pad a walk vector into full corpus dimensionality.

    The trailing ``_TOPIC_DIMS`` coordinates stay zero, so every walk vector is
    exactly orthogonal to every planted-topic vector.

    Args:
        walk_vector: A unit vector of length ``_WALK_DIMS``.

    Returns:
        A ``_DIMS``-long embedding as plain floats.
    """
    return [float(x) for x in walk_vector] + [0.0] * _TOPIC_DIMS


def _out_and_back_walk(rng: np.random.Generator, count: int) -> list[list[float]]:
    """Generate *count* embeddings tracing an out-and-back random walk.

    The outbound half drifts away from the start; the inbound half retraces it
    with a small jitter. Cosine drift from the first point therefore rises and
    falls, keeping the Spearman time-vs-drift correlation near zero so
    ``EddyDetector._has_temporal_direction`` does not reject the cluster.

    Args:
        rng: Seeded generator driving the walk.
        count: Number of embeddings to produce.

    Returns:
        A list of *count* embeddings, each ``_DIMS`` long.
    """
    half = (count + 1) // 2
    outbound = [_unit_vector(rng)]
    for _ in range(half - 1):
        outbound.append(_walk_step(rng, outbound[-1]))
    inbound = [_jittered(rng, vector) for vector in reversed(outbound)]
    return [_embed(vector) for vector in (outbound + inbound)[:count]]


def _fragment(
    fid: str,
    *,
    title: str,
    platform: SourcePlatform,
    channel: str | None,
    frequency: Frequency,
    at: datetime,
) -> Fragment:
    """Build one corpus fragment.

    Args:
        fid: Fragment ID.
        title: Fragment title (verbatim; the linker's only text signal).
        platform: Source platform.
        channel: Source channel, or ``None`` for non-stream fragments.
        frequency: Primary APTITUDE frequency.
        at: Authored/created timestamp.

    Returns:
        A populated :class:`~creek.models.Fragment`.
    """
    return Fragment(
        id=fid,
        title=title,
        source=FragmentSource(platform=platform, channel=channel),
        created=at,
        ingested=at,
        authored_at=at,
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(phase=Phase.UNCLASSIFIED),
    )


def _stream_fragments(
    rng: np.random.Generator,
    *,
    channels: tuple[str, ...],
    spacing_hours: float,
    gap_days: float,
) -> tuple[list[Fragment], dict[str, list[float]], timedelta]:
    """Build the Discord-style message stream.

    Args:
        rng: Seeded generator driving the walk.
        channels: Channel names, taken in contiguous equal blocks.
        spacing_hours: Hours between consecutive messages.
        gap_days: Inactivity gap inserted between channel blocks.

    Returns:
        The fragments, their embeddings, and the stream's total time span.
    """
    vectors = _out_and_back_walk(rng, _STREAM_FRAGMENTS)
    per_channel = _STREAM_FRAGMENTS // len(channels)
    fragments: list[Fragment] = []
    embeddings: dict[str, list[float]] = {}
    at = _CORPUS_START
    for index, vector in enumerate(vectors):
        if index and index % per_channel == 0:
            at += timedelta(days=gap_days)
        fid = f"msg-{index:04d}"
        fragments.append(
            _fragment(
                fid,
                title=_STREAM_TITLE,
                platform=SourcePlatform.DISCORD,
                channel=channels[min(index // per_channel, len(channels) - 1)],
                frequency=_STREAM_FREQUENCY,
                at=at,
            )
        )
        embeddings[fid] = vector
        at += timedelta(hours=spacing_hours)
    return fragments, embeddings, at - _CORPUS_START


def _planted_topic_fragments(
    span: timedelta,
) -> tuple[list[Fragment], dict[str, list[float]]]:
    """Build the three planted topics interleaved across the stream's span.

    Each topic owns one trailing dimension, so its cosine against every stream
    vector is exactly ``0.0``. Members are spaced closely enough to chain
    inside the 30-day thread window.

    Args:
        span: Total time span of the message stream.

    Returns:
        The topic fragments and their embeddings.
    """
    fragments: list[Fragment] = []
    embeddings: dict[str, list[float]] = {}
    for topic_index, (label, frequency) in enumerate(_PLANTED_TOPICS):
        vector = [0.0] * _DIMS
        vector[_WALK_DIMS + topic_index] = 1.0
        for member in range(_TOPIC_MEMBERS):
            fid = f"topic-{topic_index}-{member:02d}"
            at = _CORPUS_START + span * (member / _TOPIC_MEMBERS)
            fragments.append(
                _fragment(
                    fid,
                    title=f"{label} {member}",
                    platform=SourcePlatform.CLAUDE,
                    channel=None,
                    frequency=frequency,
                    at=at,
                )
            )
            embeddings[fid] = list(vector)
    return fragments, embeddings


def _message_stream_corpus(
    *,
    seed: int = _SEED,
    channels: tuple[str, ...] = _STREAM_CHANNELS,
    spacing_hours: float = _MESSAGE_SPACING_HOURS,
    gap_days: float = _CHANNEL_GAP_DAYS,
    with_topics: bool = True,
) -> tuple[list[Fragment], dict[str, list[float]]]:
    """Build the #880 reproduction corpus.

    Args:
        seed: Seed for ``numpy.random.default_rng`` — determinism, not chance.
        channels: Channel names for the stream, in contiguous blocks. Pass a
            single channel with ``gap_days=0`` for a one-episode variant that
            segmentation cannot break up.
        spacing_hours: Hours between consecutive messages.
        gap_days: Inactivity gap between channel blocks.
        with_topics: Whether to include the three planted topics.

    Returns:
        The fragments and the fragment-ID to embedding mapping.
    """
    rng = np.random.default_rng(seed)
    fragments, embeddings, span = _stream_fragments(
        rng,
        channels=channels,
        spacing_hours=spacing_hours,
        gap_days=gap_days,
    )
    if with_topics:
        topic_fragments, topic_embeddings = _planted_topic_fragments(span)
        fragments.extend(topic_fragments)
        embeddings.update(topic_embeddings)
    return fragments, embeddings


def _stream_ids(fragments: list[Fragment]) -> list[str]:
    """Return the IDs of the message-stream fragments only.

    Args:
        fragments: The full corpus.

    Returns:
        Stream fragment IDs in corpus order.
    """
    return [f.id for f in fragments if f.source.channel is not None]


def _title_tokens(title: str) -> list[str]:
    """Lowercase alphabetic tokens of *title*.

    Args:
        title: A generated eddy or thread title.

    Returns:
        The title's lowercase word tokens.
    """
    return re.findall(r"[a-z]+", title.lower())


def _dominant_channel(members: list[Fragment]) -> str | None:
    """Return the most common non-``None`` channel across *members*.

    Args:
        members: Fragments belonging to one cluster.

    Returns:
        The dominant channel name, or ``None`` if no member has one.
    """
    counts = Counter(f.source.channel for f in members if f.source.channel)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


class _Run(NamedTuple):
    """One detection run, reduced to the facts the acceptance criteria name.

    Both detectors expose the same shape — a membership map plus a page
    per cluster — so reducing them to this lets one assertion body cover
    eddies and threads without pretending the two algorithms are the same
    underneath.

    Attributes:
        members: Cluster ID to member fragment IDs, for emitted clusters.
        titles: Cluster ID to generated page title.
        descriptions: Cluster ID to generated page description.
        clusters_split: Clusters that exceeded the ceiling and were
            re-clustered.
        discarded: Fragments dropped to noise by the size guardrail.
    """

    members: dict[str, list[str]]
    titles: dict[str, str]
    descriptions: dict[str, str]
    clusters_split: int
    discarded: int


def _eddy_run(
    fragments: list[Fragment],
    embeddings: dict[str, list[float]],
    *,
    policy: SplitPolicy | None = None,
) -> _Run:
    """Detect eddies over the corpus and reduce the result to a :class:`_Run`.

    Args:
        fragments: The corpus.
        embeddings: Fragment-ID to embedding mapping.
        policy: Split policy, or ``None`` for the production default.

    Returns:
        The run's memberships, pages and guardrail counters.
    """
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
        split_policy=policy,
    )
    eddies = detector.detect_eddies(fragments)
    return _Run(
        members=detector.eddy_members,
        titles={e.id: e.title for e in eddies},
        descriptions={e.id: e.description for e in eddies},
        clusters_split=detector.clusters_split,
        discarded=detector.oversized_discarded,
    )


def _thread_run(
    fragments: list[Fragment],
    embeddings: dict[str, list[float]],
    *,
    policy: SplitPolicy | None = None,
) -> _Run:
    """Detect threads over the corpus and reduce the result to a :class:`_Run`.

    Args:
        fragments: The corpus.
        embeddings: Fragment-ID to embedding mapping.
        policy: Split policy, or ``None`` for the production default.

    Returns:
        The run's memberships, pages and guardrail counters.
    """
    detector = ThreadDetector(
        embeddings=embeddings,
        window_days=_WINDOW_DAYS,
        similarity_threshold=_SIMILARITY_THRESHOLD,
        split_policy=policy,
    )
    threads = detector.detect_threads(fragments)
    return _Run(
        members=detector.thread_members,
        titles={t.id: t.title for t in threads},
        descriptions={t.id: t.description for t in threads},
        clusters_split=detector.clusters_split,
        discarded=detector.oversized_discarded,
    )


def _topic_members(topic_index: int) -> set[str]:
    """Return the fragment IDs the generator planted for one topic.

    Args:
        topic_index: Position of the topic in :data:`_PLANTED_TOPICS`.

    Returns:
        The topic's complete member set.
    """
    return {f"topic-{topic_index}-{member:02d}" for member in range(_TOPIC_MEMBERS)}


def _claimants(members: dict[str, list[str]], wanted: set[str]) -> dict[str, set[str]]:
    """Return every emitted cluster whose membership touches *wanted*.

    Args:
        members: Cluster ID to member fragment IDs.
        wanted: The fragment IDs of one planted topic.

    Returns:
        Cluster ID to full membership, for each cluster that holds at
        least one of *wanted*.
    """
    return {
        cid: set(member_ids)
        for cid, member_ids in members.items()
        if wanted & set(member_ids)
    }


def test_message_stream_fixture_reproduces_a_single_density_connected_component() -> (
    None
):
    """The stream must degenerate into one huge, temporally-undirected cluster.

    This is the anti-vacuity guard for the whole module, and it is *green on
    unmodified main by design*: it asserts that the bug exists. It fails the
    moment the generator stops producing a single connected epsilon-graph, or
    the moment the out-and-back walk becomes monotone and
    ``_has_temporal_direction`` starts silently filtering the mega-cluster away
    — either of which would make every other assertion here pass for free.
    """
    fragments, embeddings = _message_stream_corpus()
    stream_ids = _stream_ids(fragments)
    assert len(stream_ids) == _STREAM_FRAGMENTS

    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
    )
    # Issue #880: drive the raw, unpartitioned, unlimited clustering path
    # directly so the fixture's own geometry is what is under assertion.
    clusters = detector._dbscan(stream_ids)
    assert len(clusters) == 1
    assert len(clusters[0]) >= _STREAM_FRAGMENTS * _DEGENERATE_COMPONENT_SHARE

    by_id = {f.id: f for f in fragments}
    ordered = sorted((by_id[fid] for fid in clusters[0]), key=effective_authored_at)
    anchor = embeddings[ordered[0].id]
    drifts = [_cosine_distance(anchor, embeddings[f.id]) for f in ordered]
    assert abs(_spearman_with_time(drifts)) < _CORRELATION_THRESHOLD


def test_no_eddy_exceeds_the_configured_corpus_fraction() -> None:
    """No eddy may hold more than the configured fraction of the corpus.

    Acceptance criterion 1 for eddies. On unmodified main the epsilon-graph is
    one connected component, ``_expand_cluster`` propagates a single label
    across every stream fragment, ``_has_temporal_direction`` passes it, and
    the only size guard in the module is a *minimum* — so the largest eddy is
    ~94% of the corpus against a 10% bar.
    """
    fragments, embeddings = _message_stream_corpus()
    policy = SplitPolicy(
        size_ceiling=_SPLIT_SIZE_CEILING,
        max_fraction=_SPLIT_MAX_FRACTION,
        max_depth=_SPLIT_MAX_DEPTH,
    )
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
        split_policy=policy,
    )
    detector.detect_eddies(fragments)

    ceiling = policy.ceiling_for(len(fragments))
    assert ceiling == math.floor(len(fragments) * _SPLIT_MAX_FRACTION)
    assert detector.eddy_members
    largest = max(len(members) for members in detector.eddy_members.values())
    assert largest <= ceiling


def test_eddy_titles_never_reduce_to_the_platform_word() -> None:
    """A word carried by 87% of the corpus must never become a topic label.

    All 600 stream fragments carry ``title="messages"`` verbatim, exactly as
    the demo vault does. On unmodified main ``_generate_title`` is a raw
    ``Counter`` over title content words with no inverse-document-frequency
    term at all — despite its docstring claiming "TF-IDF-lite" — and
    ``"messages"`` is absent from ``_STOPWORDS``, so ``most_common(3)`` over
    600 identical titles yields ``[("messages", 600)]`` and the eddy is named
    ``Messages``. Every stream-derived eddy must instead be named from
    structure that actually distinguishes it: its dominant channel.
    """
    fragments, embeddings = _message_stream_corpus()
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
    )
    eddies = detector.detect_eddies(fragments)
    assert eddies

    for eddy in eddies:
        assert _STREAM_TITLE not in _title_tokens(eddy.title)

    by_id = {f.id: f for f in fragments}
    for eddy in eddies:
        members = [by_id[fid] for fid in detector.eddy_members[eddy.id]]
        channel = _dominant_channel(members)
        if channel is None:
            continue
        assert channel in eddy.title


def test_no_thread_exceeds_the_configured_corpus_fraction() -> None:
    """No thread may hold more than the configured fraction of the corpus.

    Acceptance criterion 1 for threads, which the eddy half of this
    criterion cannot stand in for: threads degenerate through a *different*
    mechanism. The 30-day window bounds only which pairs are ever
    *compared*; ``_UnionFind.union`` is unbounded, so overlapping windows
    take the transitive closure of the whole stream and put message 0 and
    message 599 in one component without the two ever being compared.
    Measured on unmodified main this corpus yields thread sizes
    ``[600, 12, 12, 12]`` — a largest share of 0.943 against a 10% bar.

    ``clusters_split`` is asserted too, so the test cannot pass by the
    detector quietly finding nothing at all.
    """
    fragments, embeddings = _message_stream_corpus()
    policy = SplitPolicy(
        size_ceiling=_SPLIT_SIZE_CEILING,
        max_fraction=_SPLIT_MAX_FRACTION,
        max_depth=_SPLIT_MAX_DEPTH,
    )
    run = _thread_run(fragments, embeddings, policy=policy)

    ceiling = policy.ceiling_for(len(fragments))
    assert ceiling == math.floor(len(fragments) * _SPLIT_MAX_FRACTION)
    assert run.clusters_split >= 1
    assert run.members
    largest = max(len(members) for members in run.members.values())
    assert largest <= ceiling


def test_planted_topics_survive_as_distinct_eddies_and_threads() -> None:
    """Bounding the mega-cluster must not shred the corpus's real topics.

    This is the anti-overcorrection criterion, and it is what separates a
    fix from "shatter everything" or "cluster nothing": both of those pass
    every ceiling assertion in this module. The three planted topics sit on
    dimensions orthogonal to the entire walk subspace, are interleaved
    across the stream's whole time range, and carry real distinct titles,
    so each must come back as *exactly one* cluster holding *exactly* its
    own twelve members — no stream fragment absorbed, no member shed — and
    the three must carry three different, topic-bearing titles.

    Run at the production default :class:`SplitPolicy`, so the stream is
    clustered alongside the topics rather than being discarded out from
    under them.

    Without the fix the stream collapses into one 600-member component
    named ``Messages``; the topics survive that run, but any subsequent
    attempt to bound the mega-cluster by tightening a global parameter
    (eps, or the similarity floor) shatters the twelve-member topics first,
    because they are the smallest, tightest clusters in the corpus. This
    test is the reason segmentation was chosen over global tightening.
    """
    fragments, embeddings = _message_stream_corpus(with_topics=True)
    runs = (
        _eddy_run(fragments, embeddings),
        _thread_run(fragments, embeddings),
    )

    for run in runs:
        claimed: dict[str, int] = {}
        for topic_index, (label, _) in enumerate(_PLANTED_TOPICS):
            wanted = _topic_members(topic_index)
            claimants = _claimants(run.members, wanted)
            assert len(claimants) == 1
            cluster_id, membership = next(iter(claimants.items()))
            assert membership == wanted
            shared = set(_title_tokens(run.titles[cluster_id])) & set(
                _title_tokens(label)
            )
            assert len(shared) >= _MIN_SHARED_TITLE_TOKENS
            claimed[cluster_id] = topic_index
        assert len(claimed) == len(_PLANTED_TOPICS)
        assert len({run.titles[cid] for cid in claimed}) == len(_PLANTED_TOPICS)


def test_every_emitted_eddy_and_thread_has_a_non_empty_description() -> None:
    """Every generated page must ship a usable one-line ``description``.

    Acceptance criterion 3. ``grep -rn description creek/link/*.py``
    returned zero hits before the fix: neither detector ever passed the
    field, so :class:`~creek.models.Eddy` and :class:`~creek.models.Thread`
    fell back to their empty-string default and 212 of the demo vault's 214
    linker-written pages shipped ``description: ''``. Against unmodified
    main every emitted page here fails the non-empty assertion.

    Non-emptiness alone would be satisfiable by a constant, so the
    description is also required to be a single line (it is serialised as a
    YAML frontmatter scalar) and to open with this cluster's own member
    count — which a constant cannot do.
    """
    fragments, embeddings = _message_stream_corpus()
    runs = (
        _eddy_run(fragments, embeddings),
        _thread_run(fragments, embeddings),
    )

    for run in runs:
        assert run.descriptions
        for cluster_id, description in run.descriptions.items():
            assert description.strip()
            assert "\n" not in description
            count = len(run.members[cluster_id])
            assert description.startswith(f"{count} fragment")


def test_single_episode_stream_is_bounded_by_the_ceiling_guardrail() -> None:
    """The ceiling must hold even where segmentation is powerless.

    Every other ceiling assertion in this module runs on a corpus
    segmentation can cut, so a guardrail that never fired would still let
    them pass. This variant defeats segmentation on purpose: one channel,
    no inactivity gap, and 600 messages spanning 25 days, which is inside
    both the 24-hour episode gap and the 30-day span backstop. The
    partition is therefore a single domain, and the size ceiling is the
    only bound left.

    Pins the documented "unsplittable remainder becomes noise, not a page"
    contract end to end: the emitted clusters and the discarded count must
    together account for every fragment, so the guardrail can neither
    silently emit an oversized page nor silently lose fragments off the
    books. ``tests/test_cluster_limits.py`` pins the same contract against
    a stub ``recluster``; this pins it against the real degenerate geometry,
    where tightening epsilon genuinely fails to separate anything.

    Without the fix there is no guardrail to exercise — ``EddyDetector``
    and ``ThreadDetector`` accept no ``split_policy`` at all, so the test
    fails at construction, and the corpus produces one 600-member page.
    """
    fragments, embeddings = _message_stream_corpus(
        channels=(_STREAM_CHANNELS[0],),
        spacing_hours=_SINGLE_EPISODE_SPACING_HOURS,
        gap_days=0,
        with_topics=False,
    )
    assert len(partition(fragments, config=LinkingConfig())) == 1

    policy = SplitPolicy(
        size_ceiling=_SPLIT_SIZE_CEILING,
        max_fraction=_SPLIT_MAX_FRACTION,
        max_depth=_SPLIT_MAX_DEPTH,
    )
    ceiling = policy.ceiling_for(len(fragments))
    runs = (
        _eddy_run(fragments, embeddings, policy=policy),
        _thread_run(fragments, embeddings, policy=policy),
    )

    for run in runs:
        assert run.clusters_split == 1
        assert all(len(members) <= ceiling for members in run.members.values())
        kept = sum(len(members) for members in run.members.values())
        assert kept + run.discarded == _STREAM_FRAGMENTS


def test_recluster_work_is_bounded_by_split_depth() -> None:
    """Re-clustering must cost at most one pass over the cluster per round.

    The perf acceptance criterion rests on segmentation *reducing* work,
    and the splitter is the one place the fix can add any. Its recursion is
    breadth-first over a work queue that is extended with each round's
    subclusters, so the danger is fan-out: if a round ever returned
    overlapping or enlarged subclusters, total work would grow
    multiplicatively in the depth budget instead of linearly.

    The invariant is that each round re-clusters a partition of its parent,
    so no depth level costs more than the original cluster and the whole
    split costs at most ``max_depth`` passes over it. Measured on the
    single-episode corpus the bound is tight — three rounds, 1800
    re-clustered members against 3 x 600 — because the continuous manifold
    never separates.

    ``tests/test_cluster_limits.py`` bounds the *schedule* (how many
    tightened parameters exist); nothing else bounds the aggregate work
    those rounds actually perform. Without the fix there is no splitter,
    so the detector rejects the ``split_policy`` argument outright.
    """
    fragments, embeddings = _message_stream_corpus(
        channels=(_STREAM_CHANNELS[0],),
        spacing_hours=_SINGLE_EPISODE_SPACING_HOURS,
        gap_days=0,
        with_topics=False,
    )
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
        split_policy=SplitPolicy(
            size_ceiling=_SPLIT_SIZE_CEILING,
            max_fraction=_SPLIT_MAX_FRACTION,
            max_depth=_SPLIT_MAX_DEPTH,
        ),
    )
    with patch.object(detector, "_recluster", wraps=detector._recluster) as spy:
        detector.detect_eddies(fragments)

    assert detector.clusters_split == 1
    sizes: list[int] = [len(call.args[0]) for call in spy.call_args_list]
    assert len(sizes) <= _SPLIT_MAX_DEPTH
    assert sum(sizes) <= _SPLIT_MAX_DEPTH * _STREAM_FRAGMENTS
