"""Cluster-size ceiling + recursive re-cluster for eddies and threads (#880).

On the 35,330-fragment demo vault ``creek link --method eddies`` collapsed 87%
of the corpus into a single eddy (30,795 members) and ``--method threads``
produced one 30,108-member thread, both with an empty ``description``. Messages
form a temporally-continuous, semantically-similar stream, so thread union-find
chains years of chat into one component and eddy DBSCAN merges the dense blob
into one cluster.

These tests pin the agreed remedy:

* :class:`creek.link.cluster_limits.SplitPolicy` — an absolute floor plus a
  corpus-relative fraction, mapped from :class:`~creek.config.LinkingConfig`.
* Both detectors re-cluster any over-ceiling cluster on its own membership at a
  tightened parameter, and **discard** (with a ``WARNING``) anything still
  oversized at the depth cap or once the tightened parameter leaves its valid
  range.
* ``_build_eddy`` / ``_build_thread`` emit a non-empty ``description``.
* ``run_link`` actually wires the configured ceiling through, so the knob is
  not silently ignored the way ``eps`` currently is.

Fixture design notes
--------------------

``_message_stream_corpus`` models the pathology honestly: every fragment's
embedding is a sliding 0/1 window over its own dimension block, so *consecutive*
fragments have cosine ``1 - jitter`` (0.99 by default) while distant ones fall to
zero — a chain that is transitively connected under both detectors without any
two distant members actually being similar.

``fold=True`` walks the window out and back so a cluster's content drift from its
chronologically-earliest member is symmetric in time. That matters: with a purely
monotone walk the eddy detector's Spearman time-vs-drift filter would throw the
mega-cluster away for the *wrong* reason, and the pathology under test would never
appear. Threads have no such filter, so thread corpora use the plain monotone walk.

``_make_fragment`` in ``tests/test_link.py`` mirrors ``created`` into ``ingested``
because :func:`creek.time.effective_authored_at` falls back to ``ingested``;
:func:`_fragment` here does the same, otherwise every fragment would bucket at
construction-time wall-clock and the sliding-window assertions would collapse.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import frontmatter

from creek.config import CreekConfig, LinkingConfig
from creek.link import eddies as eddies_module
from creek.link import link_engine
from creek.link.cluster_limits import SplitPolicy
from creek.link.eddies import EddyDetector
from creek.link.link_engine import run_link
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
from creek.vault.writer import VaultWriter
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest

# --- shared constants -------------------------------------------------------

_BASE = datetime(2019, 1, 1)
"""Anchor date for every fixture corpus (well outside the ACTIVE window)."""

_NOW = datetime(2022, 1, 1)
"""Deterministic 'now' handed to ThreadDetector so status never drifts."""

_EPS = 0.3
"""Production default DBSCAN eps."""

_MIN_SAMPLES = 5
"""Production default DBSCAN min_samples."""

_EDDY_MIN_FRAGMENTS = 5
"""Production default minimum cluster size for an eddy."""

_THREAD_MIN_FRAGMENTS = 3
"""Production default minimum cluster size for a thread."""

_DEFAULT_JITTER = 0.01
"""Per-step cosine decrement of the message stream (consecutive cosine 0.99)."""

_CEILING = 50
"""Small absolute ceiling used by the pathology tests so corpora stay fast."""

_MAX_FRACTION = 0.10
"""Production default corpus fraction; the floor dominates at these sizes."""

_MAX_DEPTH = 3
"""Production default re-cluster depth."""

_EPS_STEP = 0.05
"""Production default eps tightening step for eddy splits."""

_TOPIC_TEMPLATE = "{word} notes entry {index}"
"""Default planted-topic title template (yields real content words)."""

_STOPWORD_TEMPLATE = "{word} the of {index}"
"""Title template whose tokens are all digits or stopwords."""

_STREAM_N = 201
"""Odd length: the fold pairs i with n-1-i exactly, giving Spearman == 0."""

_SIMILARITY_ROUND_BUDGET = 5
"""Upper bound on _cluster invocations before similarity would reach 1.0."""


# --- counting helpers -------------------------------------------------------


class _Counter:
    """Mutable invocation counter shared with a monkeypatched wrapper.

    Attributes:
        count: How many times the wrapped callable has been invoked.
    """

    def __init__(self) -> None:
        """Start the counter at zero."""
        self.count = 0


def _count_function(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
) -> _Counter:
    """Wrap ``module.name`` with a pass-through invocation counter.

    Args:
        monkeypatch: Pytest patcher; restores the original at teardown.
        module: Module object owning the function.
        name: Attribute name of the function to wrap.

    Returns:
        The counter whose ``count`` tracks invocations.
    """
    original = getattr(module, name)
    counter = _Counter()

    def _wrapper(*args: object, **kwargs: object) -> object:
        """Count the call, then delegate verbatim to the original."""
        counter.count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, _wrapper)
    return counter


def _count_method(
    monkeypatch: pytest.MonkeyPatch,
    cls: type,
    name: str,
) -> _Counter:
    """Wrap the unbound method ``cls.name`` with an invocation counter.

    A plain function is installed (not a callable object) so descriptor
    binding still supplies ``self``.

    Args:
        monkeypatch: Pytest patcher; restores the original at teardown.
        cls: Class owning the method.
        name: Attribute name of the method to wrap.

    Returns:
        The counter whose ``count`` tracks invocations.
    """
    original = getattr(cls, name)
    counter = _Counter()

    def _wrapper(self: object, *args: object, **kwargs: object) -> object:
        """Count the call, then delegate verbatim to the original."""
        counter.count += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, name, _wrapper)
    return counter


# --- corpus fixtures --------------------------------------------------------


def _fragment(
    fid: str,
    title: str,
    created: datetime,
    *,
    primary: Frequency = Frequency.UNCLASSIFIED,
) -> Fragment:
    """Build a minimal embeddable fragment with a fixed id and authored time.

    ``created`` is mirrored into ``ingested`` so
    :func:`creek.time.effective_authored_at` buckets the fragment at the time
    the test cares about rather than at construction wall-clock.

    Args:
        fid: Fragment id (also the on-disk stem when persisted).
        title: Fragment title; drives the title/description heuristics.
        created: Authored timestamp, mirrored into ``ingested``.
        primary: Primary APTITUDE frequency.

    Returns:
        A populated :class:`~creek.models.Fragment`.
    """
    return Fragment(
        id=fid,
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=created,
        ingested=created,
        frequency=FrequencyClassification(primary=primary),
        wavelength=WavelengthClassification(phase=Phase.UNCLASSIFIED),
    )


def _stream_window(jitter: float) -> int:
    """Return the sliding-window width that yields *jitter* per-step decay.

    Args:
        jitter: Desired cosine decrement between consecutive fragments.

    Returns:
        The number of active dimensions per embedding vector.
    """
    return int(round(1.0 / jitter))


def _window_positions(n: int, *, fold: bool) -> list[int]:
    """Return the per-index window offset of a message stream.

    Args:
        n: Number of fragments. Use an **odd** value with ``fold=True`` so
            index ``i`` and ``n - 1 - i`` share a position exactly and no two
            *adjacent* indices collide.
        fold: When ``True`` the walk goes out and back, making content drift
            from the earliest fragment symmetric in time (Spearman == 0).

    Returns:
        One window offset per fragment index.
    """
    if not fold:
        return list(range(n))
    mid = (n - 1) // 2
    return [i if i <= mid else (n - 1 - i) for i in range(n)]


def _stream_width(n: int, jitter: float = _DEFAULT_JITTER, *, fold: bool) -> int:
    """Return the number of dimensions a message stream occupies.

    Args:
        n: Number of fragments.
        jitter: Per-step cosine decrement.
        fold: Whether the walk folds back on itself.

    Returns:
        The exclusive upper dimension index used by the stream.
    """
    return max(_window_positions(n, fold=fold)) + _stream_window(jitter)


def _message_stream_corpus(
    n: int,
    jitter: float = _DEFAULT_JITTER,
    *,
    fold: bool = False,
    dims: int | None = None,
    frequency: Frequency = Frequency.F5,
    prefix: str = "msg",
    start: datetime = _BASE,
) -> tuple[list[Fragment], dict[str, list[float]]]:
    """Return a temporally-continuous, semantically-continuous message stream.

    Fragment ``i`` is authored one day after fragment ``i - 1`` and its
    embedding is a contiguous block of ones starting at ``position(i)``. Two
    fragments therefore have cosine ``max(0, w - |Δposition|) / w`` — 0.99 for
    neighbours, 0.0 once they are a whole window apart. Every fragment shares a
    single primary frequency, so thread topic-consistency reduces to the cosine
    gate.

    Args:
        n: Number of fragments.
        jitter: Per-step cosine decrement (``0.01`` -> consecutive cosine 0.99).
        fold: Fold the walk so time-vs-drift Spearman is exactly zero (required
            for eddy corpora, harmless for thread corpora).
        dims: Total vector width; defaults to the stream's own width. Pass a
            wider value to leave room for planted topics.
        frequency: Primary frequency stamped on every fragment.
        prefix: Fragment-id prefix.
        start: Date of fragment ``0``.

    Returns:
        ``(fragments, embeddings)`` with embeddings keyed by fragment id.
    """
    window = _stream_window(jitter)
    positions = _window_positions(n, fold=fold)
    total = dims if dims is not None else max(positions) + window

    fragments: list[Fragment] = []
    embeddings: dict[str, list[float]] = {}
    for index, position in enumerate(positions):
        fid = f"{prefix}-{index:04d}"
        fragments.append(
            _fragment(
                fid,
                f"stream message note {index}",
                start + timedelta(days=index),
                primary=frequency,
            ),
        )
        vector = [0.0] * total
        for dim in range(position, position + window):
            vector[dim] = 1.0
        embeddings[fid] = vector
    return fragments, embeddings


def _planted_topics(
    k: int,
    size: int,
    *,
    dims: int,
    first_dim: int = 0,
    frequency: Frequency = Frequency.UNCLASSIFIED,
    prefix: str = "topic",
    words: Sequence[str] | None = None,
    template: str = _TOPIC_TEMPLATE,
    start: datetime = _BASE,
    step_days: int = 1,
) -> tuple[list[Fragment], dict[str, list[float]], list[list[str]]]:
    """Return *k* well-separated one-hot topic clusters of *size* fragments.

    Each topic owns a single dimension, so within-topic cosine is ``1.0`` and
    cross-topic cosine is ``0.0``. Identical within-topic vectors give zero
    content drift, so the eddy detector's Spearman filter never rejects them.

    Args:
        k: Number of topics.
        size: Fragments per topic.
        dims: Total vector width (must exceed ``first_dim + k - 1``).
        first_dim: Dimension owned by topic ``0``.
        frequency: Primary frequency stamped on every fragment.
        prefix: Fragment-id prefix.
        words: Per-topic distinctive title word; defaults to ``<prefix>a``,
            ``<prefix>b``, ...
        template: Title template formatted with ``word`` and ``index``.
        start: Date of the first fragment.
        step_days: Days between consecutive fragments.

    Returns:
        ``(fragments, embeddings, groups)`` where ``groups[t]`` lists the
        fragment ids of topic ``t``.
    """
    if words is None:
        words = [f"{prefix}{chr(ord('a') + topic)}" for topic in range(k)]
    topic_words = list(words)

    fragments: list[Fragment] = []
    embeddings: dict[str, list[float]] = {}
    groups: list[list[str]] = []
    for topic in range(k):
        vector = [0.0] * dims
        vector[first_dim + topic] = 1.0
        members: list[str] = []
        for member in range(size):
            fid = f"{prefix}-{topic:02d}-{member:03d}"
            offset = (topic * size + member) * step_days
            fragments.append(
                _fragment(
                    fid,
                    template.format(word=topic_words[topic], index=member),
                    start + timedelta(days=offset),
                    primary=frequency,
                ),
            )
            embeddings[fid] = list(vector)
            members.append(fid)
        groups.append(members)
    return fragments, embeddings, groups


def _two_blob_corpus(
    size: int,
    *,
    cross_cosine: float,
    frequency: Frequency = Frequency.UNCLASSIFIED,
    start: datetime = _BASE,
) -> tuple[list[Fragment], dict[str, list[float]], list[str], list[str]]:
    """Return two internally-identical blobs bridged at *cross_cosine*.

    The blobs are interleaved in time (A on even days, B on odd days) so the
    merged cluster's time-vs-drift Spearman correlation stays far below the
    eddy detector's directionality threshold — the merged cluster must survive
    to *be* oversized, otherwise the pathology never appears.

    Args:
        size: Fragments per blob.
        cross_cosine: Cosine similarity between the two blob vectors.
        frequency: Primary frequency stamped on every fragment.
        start: Date of the first fragment.

    Returns:
        ``(fragments, embeddings, ids_a, ids_b)``.
    """
    vec_a = [1.0, 0.0]
    vec_b = [cross_cosine, math.sqrt(1.0 - cross_cosine * cross_cosine)]

    fragments: list[Fragment] = []
    embeddings: dict[str, list[float]] = {}
    ids_a: list[str] = []
    ids_b: list[str] = []
    for member in range(size):
        for label, vector, bucket, word in (
            ("a", vec_a, ids_a, "alpha"),
            ("b", vec_b, ids_b, "bravo"),
        ):
            fid = f"blob-{label}-{member:03d}"
            day = member * 2 + (0 if label == "a" else 1)
            fragments.append(
                _fragment(
                    fid,
                    f"{word} channel note {member}",
                    start + timedelta(days=day),
                    primary=frequency,
                ),
            )
            embeddings[fid] = list(vector)
            bucket.append(fid)
    return fragments, embeddings, ids_a, ids_b


def _stream_with_planted_topics(
    *,
    n: int = _STREAM_N,
    topics: int = 3,
    topic_size: int = 8,
) -> tuple[list[Fragment], dict[str, list[float]], list[list[str]]]:
    """Return a folded message stream mixed with well-separated planted topics.

    Args:
        n: Message-stream length (odd, so the fold is exactly symmetric).
        topics: Number of planted topics.
        topic_size: Fragments per planted topic.

    Returns:
        ``(fragments, embeddings, topic_groups)``.
    """
    width = _stream_width(n, fold=True)
    dims = width + topics
    stream_frags, stream_emb = _message_stream_corpus(n, fold=True, dims=dims)
    topic_frags, topic_emb, groups = _planted_topics(
        topics,
        topic_size,
        dims=dims,
        first_dim=width,
        start=_BASE + timedelta(days=n + 10),
    )
    return stream_frags + topic_frags, {**stream_emb, **topic_emb}, groups


def _stream_with_side_thread(
    *,
    n: int = 120,
    side_size: int = 5,
) -> tuple[list[Fragment], dict[str, list[float]], list[str]]:
    """Return a continuous F5 message stream plus a distinct F9 side thread.

    The side thread shares no frequency with the stream, so union-find can
    never merge the two — it is the "real signal must survive" control.

    Args:
        n: Message-stream length.
        side_size: Fragments in the planted side thread.

    Returns:
        ``(fragments, embeddings, side_ids)``.
    """
    width = _stream_width(n, fold=False)
    dims = width + 1
    stream_frags, stream_emb = _message_stream_corpus(
        n,
        dims=dims,
        frequency=Frequency.F5,
    )
    side_frags, side_emb, groups = _planted_topics(
        1,
        side_size,
        dims=dims,
        first_dim=width,
        frequency=Frequency.F9,
        prefix="side",
    )
    return stream_frags + side_frags, {**stream_emb, **side_emb}, groups[0]


def _scaffold_vault(tmp_path: Path) -> Path:
    """Create the minimum vault skeleton :class:`VaultWriter` demands.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        The vault root.
    """
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)
    return vault


def _page_counts(pages: list[Path]) -> list[int]:
    """Return the ``fragment_count`` frontmatter value of each page.

    Args:
        pages: Markdown pages written by :class:`VaultWriter`.

    Returns:
        One integer per page.
    """
    return [int(frontmatter.load(page).metadata["fragment_count"]) for page in pages]


def _memberships(members: dict[str, list[str]]) -> set[frozenset[str]]:
    """Return an order-independent view of a detector's membership map.

    Args:
        members: ``eddy_members`` / ``thread_members`` mapping.

    Returns:
        The set of member sets.
    """
    return {frozenset(ids) for ids in members.values()}


def _warning_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the rendered messages of every captured WARNING record.

    Args:
        caplog: Pytest log-capture fixture.

    Returns:
        Rendered WARNING messages in capture order.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]


# --- 1-4: SplitPolicy contract ---------------------------------------------


class TestSplitPolicy:
    """The ceiling arithmetic and the LinkingConfig mapping."""

    def test_ceiling_uses_absolute_floor_for_small_corpora(self) -> None:
        """A 12-fragment corpus keeps the absolute floor, not 10% of 12."""
        assert SplitPolicy(500, 0.10, 3).ceiling_for(12) == 500

    def test_ceiling_uses_fraction_for_large_corpora(self) -> None:
        """The demo vault's 35,330 fragments yield a 3,533 ceiling."""
        assert SplitPolicy(500, 0.10, 3).ceiling_for(35330) == 3533

    def test_full_fraction_disables_splitting(self) -> None:
        """``max_fraction=1.0`` makes the ceiling the whole corpus (opt out)."""
        assert SplitPolicy(500, 1.0, 3).ceiling_for(1000) == 1000

    def test_from_linking_config_maps_every_key(self) -> None:
        """Every policy field is sourced from the matching config key."""
        policy = SplitPolicy.from_linking_config(LinkingConfig())
        assert policy.size_ceiling == 500
        assert policy.max_fraction == 0.10
        assert policy.max_depth == 3


# --- 5-11: eddy splitting ---------------------------------------------------


class TestEddyCeiling:
    """The eddy detector must never emit a cluster above the ceiling."""

    def test_dense_message_stream_produces_no_mega_eddy(self) -> None:
        """A 201-message continuous stream stops collapsing into one eddy."""
        fragments, embeddings, _groups = _stream_with_planted_topics()
        policy = SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH)
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
            split_policy=policy,
            split_eps_step=_EPS_STEP,
        )

        detector.detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)

        ceiling = policy.ceiling_for(len(fragments))
        sizes = [len(ids) for ids in detector.eddy_members.values()]
        assert sizes, "the planted topics must still surface as eddies"
        assert max(sizes) <= ceiling, (
            f"largest eddy has {max(sizes)} members, ceiling is {ceiling}"
        )

    def test_distinct_topics_still_surface_as_separate_eddies(self) -> None:
        """Splitting the mega-blob must not disturb genuinely distinct topics."""
        fragments, embeddings, groups = _stream_with_planted_topics()
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH),
            split_eps_step=_EPS_STEP,
        )

        detector.detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)

        emitted = _memberships(detector.eddy_members)
        for topic_ids in groups:
            planted = frozenset(topic_ids)
            holders = [members for members in emitted if planted <= members]
            assert len(holders) == 1, (
                f"topic {topic_ids[0]} landed in {len(holders)} eddies, want 1"
            )

    def test_oversized_cluster_splits_when_tightening_separates(self) -> None:
        """Two blobs bridged at eps 0.3 separate cleanly at eps 0.25."""
        fragments, embeddings, ids_a, ids_b = _two_blob_corpus(30, cross_cosine=0.72)
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH),
            split_eps_step=_EPS_STEP,
        )

        eddies = detector.detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)

        assert len(eddies) == 2
        assert _memberships(detector.eddy_members) == {
            frozenset(ids_a),
            frozenset(ids_b),
        }

    def test_unsplittable_oversized_eddy_is_discarded_not_emitted(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """60 identical vectors above a 50 ceiling are dropped, and logged."""
        fragments, embeddings, groups = _planted_topics(1, 60, dims=1)
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH),
            split_eps_step=_EPS_STEP,
        )

        with caplog.at_level(logging.WARNING, logger="creek.link.eddies"):
            eddies = detector.detect_eddies(
                fragments,
                min_fragments=_EDDY_MIN_FRAGMENTS,
            )

        assert eddies == []
        assert detector.eddy_members == {}
        emitted = {fid for ids in detector.eddy_members.values() for fid in ids}
        assert emitted.isdisjoint(groups[0])
        warnings = _warning_messages(caplog)
        assert any("60" in message for message in warnings), warnings

    def test_default_policy_does_not_fire_below_absolute_floor(self) -> None:
        """The stock 500-fragment floor leaves the #790 2x6 corpus untouched."""
        fragments, embeddings, groups = _planted_topics(2, 6, dims=2)
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
            split_policy=SplitPolicy.from_linking_config(LinkingConfig()),
        )

        eddies = detector.detect_eddies(fragments, min_fragments=_MIN_SAMPLES)

        assert len(eddies) == 2
        assert _memberships(detector.eddy_members) == {
            frozenset(groups[0]),
            frozenset(groups[1]),
        }

    def test_dbscan_honours_eps_override_and_default(self) -> None:
        """``_dbscan(ids, eps=...)`` tightens; the no-arg form is unchanged."""
        fragments, embeddings, ids_a, ids_b = _two_blob_corpus(30, cross_cosine=0.72)
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
        )
        ids = [frag.id for frag in fragments]

        # Issue #790 calls the no-arg form; it must keep merging both blobs.
        merged = detector._dbscan(ids)
        tightened = detector._dbscan(ids, eps=0.05)

        assert {frozenset(cluster) for cluster in merged} == {frozenset(ids)}
        assert {frozenset(cluster) for cluster in tightened} == {
            frozenset(ids_a),
            frozenset(ids_b),
        }
        assert detector.eps == _EPS, "an override must not mutate detector state"

    def test_split_max_depth_zero_discards_oversized_immediately(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``max_depth=0`` drops an oversized cluster with no re-cluster attempt."""
        fragments, embeddings, _ids_a, _ids_b = _two_blob_corpus(
            30,
            cross_cosine=0.72,
        )
        neighbour_calls = _count_function(
            monkeypatch,
            eddies_module,
            "cosine_neighbours",
        )
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, 0),
            split_eps_step=_EPS_STEP,
        )

        eddies = detector.detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)

        assert eddies == []
        assert detector.eddy_members == {}
        assert neighbour_calls.count == 1, "depth 0 must not re-run DBSCAN"


# --- 12-16: thread splitting ------------------------------------------------


class TestThreadCeiling:
    """The thread detector must never emit a component above the ceiling."""

    def test_continuous_message_stream_produces_no_mega_thread(self) -> None:
        """A 120-day continuous F5 stream stops chaining into one thread."""
        fragments, embeddings, _side_ids = _stream_with_side_thread()
        policy = SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH)
        detector = ThreadDetector(
            embeddings=embeddings,
            split_policy=policy,
            now=_NOW,
        )

        threads = detector.detect_threads(
            fragments,
            min_fragments=_THREAD_MIN_FRAGMENTS,
        )

        ceiling = policy.ceiling_for(len(fragments))
        assert threads, "the distinct-topic side thread must survive"
        largest = max(thread.fragment_count for thread in threads)
        assert largest <= ceiling, f"largest thread holds {largest}, ceiling {ceiling}"

    def test_thread_split_preserves_distinct_topic_thread(self) -> None:
        """The planted F9 side thread survives the stream's demolition intact."""
        fragments, embeddings, side_ids = _stream_with_side_thread()
        detector = ThreadDetector(
            embeddings=embeddings,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH),
            now=_NOW,
        )

        detector.detect_threads(fragments, min_fragments=_THREAD_MIN_FRAGMENTS)

        assert frozenset(side_ids) in _memberships(detector.thread_members)

    def test_unsplittable_thread_component_is_discarded_with_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """60 identical F5 fragments cannot be split, so they are discarded."""
        fragments, embeddings, groups = _planted_topics(
            1,
            60,
            dims=1,
            frequency=Frequency.F5,
        )
        detector = ThreadDetector(
            embeddings=embeddings,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, _MAX_DEPTH),
            now=_NOW,
        )

        with caplog.at_level(logging.WARNING, logger="creek.link.threads"):
            threads = detector.detect_threads(
                fragments,
                min_fragments=_THREAD_MIN_FRAGMENTS,
            )

        assert threads == []
        assert detector.thread_members == {}
        emitted = {fid for ids in detector.thread_members.values() for fid in ids}
        assert emitted.isdisjoint(groups[0])
        warnings = _warning_messages(caplog)
        assert any("60" in message for message in warnings), warnings

    def test_thread_split_terminates_before_similarity_reaches_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A generous depth cap must not outlive the similarity range guard.

        With ``max_depth=20`` and a 0.1 step, tightening 0.6 -> 1.0 exhausts the
        valid range after four rounds. The component must be discarded there
        rather than looping to the depth cap (or forever).
        """
        fragments, embeddings, _groups = _planted_topics(
            1,
            60,
            dims=1,
            frequency=Frequency.F5,
        )
        cluster_calls = _count_method(monkeypatch, ThreadDetector, "_cluster")
        detector = ThreadDetector(
            embeddings=embeddings,
            split_policy=SplitPolicy(_CEILING, _MAX_FRACTION, 20),
            now=_NOW,
        )

        threads = detector.detect_threads(
            fragments,
            min_fragments=_THREAD_MIN_FRAGMENTS,
        )

        assert threads == []
        assert detector.thread_members == {}
        assert cluster_calls.count <= _SIMILARITY_ROUND_BUDGET, (
            f"{cluster_calls.count} clustering rounds — the range guard is missing"
        )

    def test_cluster_honours_similarity_threshold_override(self) -> None:
        """``_cluster(..., similarity_threshold=0.99)`` splits the default merge."""
        fragments, embeddings = _message_stream_corpus(
            40,
            jitter=0.05,
            frequency=Frequency.F5,
        )
        detector = ThreadDetector(embeddings=embeddings, now=_NOW)
        ordered = sorted(fragments, key=effective_authored_at)

        merged = detector._cluster(ordered)
        tightened = detector._cluster(ordered, similarity_threshold=0.99)

        assert max(len(ids) for ids in merged.groups().values()) == 40
        assert {len(ids) for ids in tightened.groups().values()} == {1}
        assert detector.similarity_threshold == 0.6, (
            "an override must not mutate detector state"
        )


# --- 17-20: descriptions ----------------------------------------------------


class TestDescriptions:
    """Emitted pages must carry a real description, never ``''``."""

    def test_eddy_description_is_non_empty_and_names_distinctive_terms(self) -> None:
        """The eddy description names a top content word and the member count."""
        fragments, embeddings, _groups = _planted_topics(
            1,
            6,
            dims=1,
            words=["sourdough"],
        )
        detector = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
        )

        eddies = detector.detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)

        assert len(eddies) == 1
        description = eddies[0].description
        assert description.strip(), "eddy description must not be empty"
        assert "sourdough" in description.lower()
        assert "6" in description, description

    def test_thread_description_is_non_empty_and_names_the_span(self) -> None:
        """The thread description names the member count and both bound years."""
        fragments, embeddings, _groups = _planted_topics(
            1,
            32,
            dims=1,
            frequency=Frequency.F5,
            step_days=25,
            start=datetime(2019, 1, 1),
        )
        detector = ThreadDetector(embeddings=embeddings, now=_NOW)

        threads = detector.detect_threads(
            fragments,
            min_fragments=_THREAD_MIN_FRAGMENTS,
        )

        assert len(threads) == 1
        description = threads[0].description
        assert description.strip(), "thread description must not be empty"
        assert "32" in description, description
        assert "2019" in description, description
        assert "2021" in description, description

    def test_description_falls_back_when_no_content_words(self) -> None:
        """Titles made only of digits and stopwords still yield a description."""
        fragments, embeddings, _groups = _planted_topics(
            1,
            6,
            dims=1,
            frequency=Frequency.F5,
            words=["123"],
            template=_STOPWORD_TEMPLATE,
        )

        eddies = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
        ).detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)
        threads = ThreadDetector(
            embeddings=embeddings,
            now=_NOW,
        ).detect_threads(fragments, min_fragments=_THREAD_MIN_FRAGMENTS)

        assert len(eddies) == 1
        assert len(threads) == 1
        assert eddies[0].description.strip(), "eddy fallback description is empty"
        assert threads[0].description.strip(), "thread fallback description is empty"

    def test_written_eddy_page_frontmatter_has_non_empty_description(
        self,
        tmp_path: Path,
    ) -> None:
        """A materialised eddy page carries a non-empty ``description`` key."""
        fragments, embeddings, _groups = _planted_topics(
            1,
            6,
            dims=1,
            words=["sourdough"],
        )
        eddies = EddyDetector(
            embeddings=embeddings,
            eps=_EPS,
            min_samples=_MIN_SAMPLES,
        ).detect_eddies(fragments, min_fragments=_EDDY_MIN_FRAGMENTS)
        vault = _scaffold_vault(tmp_path)

        path = VaultWriter(vault_path=vault).write_eddy(eddies[0])

        description = frontmatter.load(path).metadata.get("description")
        assert isinstance(description, str)
        assert description.strip(), "written eddy page has an empty description"

    def test_written_thread_page_frontmatter_has_non_empty_description(
        self,
        tmp_path: Path,
    ) -> None:
        """A materialised thread page carries a non-empty ``description`` key."""
        fragments, embeddings, _groups = _planted_topics(
            1,
            6,
            dims=1,
            frequency=Frequency.F5,
            words=["sourdough"],
        )
        threads = ThreadDetector(
            embeddings=embeddings,
            now=_NOW,
        ).detect_threads(fragments, min_fragments=_THREAD_MIN_FRAGMENTS)
        vault = _scaffold_vault(tmp_path)

        path = VaultWriter(vault_path=vault).write_thread(threads[0])

        description = frontmatter.load(path).metadata.get("description")
        assert isinstance(description, str)
        assert description.strip(), "written thread page has an empty description"


# --- 21: end-to-end wiring --------------------------------------------------


class TestRunLinkWiring:
    """``run_link`` must actually honour the configured ceiling."""

    def _seed(self, vault: Path, fragments: list[Fragment]) -> None:
        """Persist *fragments* under ``<vault>/01-Fragments/Notes``.

        Args:
            vault: Vault root.
            fragments: Fragments to write.
        """
        for fragment in fragments:
            write_fragment_file(vault=vault, fragment=fragment, body=fragment.title)

    def test_run_link_eddies_respects_configured_ceiling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No written eddy page may exceed ``linking.cluster_size_ceiling``."""
        vault = _scaffold_vault(tmp_path)
        fragments, embeddings, _ids_a, _ids_b = _two_blob_corpus(8, cross_cosine=0.72)
        self._seed(vault, fragments)
        monkeypatch.setattr(
            link_engine,
            "_load_or_compute_embeddings",
            lambda **_kwargs: embeddings,
        )
        config = CreekConfig(
            linking=LinkingConfig(cluster_size_ceiling=10, eddy_min_fragments=5),
        )

        summary = run_link(
            vault_path=vault,
            config=config,
            method="eddies",
            rebuild=False,
        )

        pages = sorted((vault / "03-Eddies").rglob("*.md"))
        counts = _page_counts(pages)
        assert counts, "expected eddy pages to be written"
        assert summary.eddies_written == len(pages)
        assert max(counts) <= 10, f"eddy page holds {max(counts)} fragments"

    def test_run_link_threads_respects_configured_ceiling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No written thread page may exceed ``linking.cluster_size_ceiling``."""
        vault = _scaffold_vault(tmp_path)
        fragments, embeddings, _ids_a, _ids_b = _two_blob_corpus(
            5,
            cross_cosine=0.65,
            frequency=Frequency.F5,
        )
        self._seed(vault, fragments)
        monkeypatch.setattr(
            link_engine,
            "_load_or_compute_embeddings",
            lambda **_kwargs: embeddings,
        )
        config = CreekConfig(
            linking=LinkingConfig(cluster_size_ceiling=6, thread_min_fragments=3),
        )

        summary = run_link(
            vault_path=vault,
            config=config,
            method="threads",
            rebuild=False,
        )

        pages = sorted((vault / "02-Threads").rglob("*.md"))
        counts = _page_counts(pages)
        assert counts, "expected thread pages to be written"
        assert summary.threads_written == len(pages)
        assert max(counts) <= 6, f"thread page holds {max(counts)} fragments"
