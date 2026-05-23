"""Conversational aggregator — stitch fragments into coarser parents (FEAT-022).

Zoom-out twin of the FEAT-021 splitter: stitches a stream of too-small
fragments (chat one-liners) into the parent units where meaning lives.
Vocabulary is chat-first (``exchange`` → ``burst`` → ``session``), but
the mechanics are general: any sequence of fragments at ``level=document``
(or below) sharing a ``FragmentSource`` rolls upward through the same
transitions. ``session`` is terminal — session-level input is a no-op.

The operator never mutates inputs on disk; it returns new parents and
leaves persistence to the caller (FEAT-023).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from creek.models import Authorship, Fragment, FragmentSource

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from creek.models import FragmentLevel

__all__ = ["AggregateLevel", "AggregationConfig", "aggregate"]

AggregateLevel = Literal["exchange", "burst", "session"]
"""Target levels supported by :func:`aggregate`.

``exchange`` groups consecutive ``document``-level messages (≤2 speakers,
within ``exchange_max_gap_minutes``). ``burst`` groups consecutive
``exchange``-level fragments by topic continuity (cosine similarity ≥
``burst_similarity_threshold``). ``session`` groups consecutive
``burst``-level fragments (within ``session_max_gap_minutes``); session
is terminal.
"""

_SOURCE_LEVELS: dict[AggregateLevel, FragmentLevel] = {
    "exchange": "document",
    "burst": "exchange",
    "session": "burst",
}


@dataclass
class AggregationConfig:
    """Knobs for the conversational aggregator (FEAT-022).

    The three numeric knobs mirror the YAML ``linking:`` section so the
    aggregator tunes from a per-vault config; ``joiner`` and ``embedder``
    are runtime-only. All threshold checks are inclusive.

    Attributes:
        exchange_max_gap_minutes: Max minute-gap between messages in the
            same exchange.
        burst_similarity_threshold: Cosine-similarity floor that keeps
            two consecutive exchanges in the same burst.
        session_max_gap_minutes: Max minute-gap between bursts in the
            same session.
        joiner: Separator used to concatenate child titles.
        embedder: Callable returning dense embeddings for a list of
            strings; required for multi-exchange ``burst`` runs. Injected
            so tests and calibration scripts can stub the
            sentence-transformers dependency.
    """

    exchange_max_gap_minutes: int = 30
    burst_similarity_threshold: float = 0.7
    session_max_gap_minutes: int = 360
    joiner: str = "\n\n"
    embedder: Callable[[list[str]], list[list[float]]] | None = None


def aggregate(
    fragments: list[Fragment],
    *,
    level: AggregateLevel,
    config: AggregationConfig,
) -> list[Fragment]:
    """Aggregate fragments upward to the requested ``level``.

    The aggregator partitions the input into fragments whose ``level``
    matches the expected source level for ``level`` (eligible) and the
    rest (pass-through). Eligible fragments are grouped by source key
    and aggregated; pass-through fragments are returned unchanged.

    Idempotency: re-running on the same input produces parents with
    identical IDs and identical concatenated text. Cross-source
    grouping is out of scope in v1 — only fragments sharing a
    ``(platform, channel, conversation_id)`` key combine.

    Args:
        fragments: Fragments to aggregate. Mixed levels are allowed;
            only fragments at the expected source level participate.
        level: Target aggregation level (``exchange``, ``burst``, or
            ``session``).
        config: Aggregator knobs.

    Returns:
        A new list containing aggregated parents followed by any
        pass-through fragments (in their original relative order).
    """
    if not fragments:
        return []

    source_level = _SOURCE_LEVELS[level]
    eligible: list[Fragment] = []
    passthrough: list[Fragment] = []
    for frag in fragments:
        if frag.level == source_level:
            eligible.append(frag)
        else:
            passthrough.append(frag)

    if not eligible:
        return fragments.copy()

    parents = _aggregate_eligible(eligible, level, config)
    return parents + passthrough


def _aggregate_eligible(
    eligible: list[Fragment],
    level: AggregateLevel,
    config: AggregationConfig,
) -> list[Fragment]:
    """Group eligible fragments by source key and dispatch the transition."""
    by_source: dict[str, list[Fragment]] = {}
    for frag in eligible:
        key = _source_key(frag.source)
        by_source.setdefault(key, []).append(frag)

    parents: list[Fragment] = []
    for source_frags in by_source.values():
        sorted_frags = sorted(source_frags, key=lambda f: f.created)
        parents.extend(_group_by_level(sorted_frags, level, config))
    return parents


def _group_by_level(
    fragments: list[Fragment],
    level: AggregateLevel,
    config: AggregationConfig,
) -> list[Fragment]:
    """Dispatch grouping to the per-level transition and build parents."""
    if level == "exchange":
        groups = _group_to_exchanges(fragments, config)
    elif level == "burst":
        groups = _group_to_bursts(fragments, config)
    else:
        groups = _group_to_sessions(fragments, config)
    return [_build_parent(g, level, config) for g in groups]


def _group_to_exchanges(
    fragments: list[Fragment],
    config: AggregationConfig,
) -> list[list[Fragment]]:
    """Group consecutive messages into exchanges (≤2 speakers, gap-bounded)."""
    groups: list[list[Fragment]] = [[fragments[0]]]
    for current in fragments[1:]:
        prev = groups[-1][-1]
        gap_minutes = _minutes_between(prev.created, current.created)
        candidate = [*groups[-1], current]
        within_gap = gap_minutes <= config.exchange_max_gap_minutes
        within_speakers = len(_speakers(candidate)) <= 2
        if within_gap and within_speakers:
            groups[-1].append(current)
        else:
            groups.append([current])
    return groups


def _group_to_bursts(
    fragments: list[Fragment],
    config: AggregationConfig,
) -> list[list[Fragment]]:
    """Group consecutive exchanges into topic-continuous bursts.

    Topic continuity = cosine similarity ≥ ``burst_similarity_threshold``
    between consecutive exchange title embeddings.

    Raises:
        ValueError: If more than one exchange is supplied and
            ``config.embedder`` is ``None``.
    """
    if len(fragments) == 1:
        return [fragments.copy()]

    if config.embedder is None:
        msg = (
            "aggregate(level='burst', ...) requires AggregationConfig.embedder "
            "to compute topic-continuity similarity between exchanges."
        )
        raise ValueError(msg)

    embeddings = config.embedder([f.title for f in fragments])
    groups: list[list[Fragment]] = [[fragments[0]]]
    for i in range(1, len(fragments)):
        sim = _cosine(embeddings[i - 1], embeddings[i])
        if sim >= config.burst_similarity_threshold:
            groups[-1].append(fragments[i])
        else:
            groups.append([fragments[i]])
    return groups


def _group_to_sessions(
    fragments: list[Fragment],
    config: AggregationConfig,
) -> list[list[Fragment]]:
    """Group consecutive bursts into sessions (gap-bounded)."""
    groups: list[list[Fragment]] = [[fragments[0]]]
    for current in fragments[1:]:
        prev = groups[-1][-1]
        gap_minutes = _minutes_between(prev.created, current.created)
        if gap_minutes <= config.session_max_gap_minutes:
            groups[-1].append(current)
        else:
            groups.append([current])
    return groups


def _build_parent(
    children: list[Fragment],
    level: AggregateLevel,
    config: AggregationConfig,
) -> Fragment:
    """Assemble an aggregated parent Fragment from an ordered child list.

    Carries a deterministic ID from ``(source_key, level, child_ids)``,
    ``child_ids`` in input order, joiner-concatenated titles, inherited
    source (with ``author`` promoted to ``COLLABORATIVE`` on multi-speaker
    groups), and tags for the date-range and participants.
    """
    child_ids = [c.id for c in children]
    source = _inherit_source(children)
    parent_id = _aggregate_id(_source_key(source), level, child_ids)
    title = config.joiner.join(c.title for c in children)
    earliest = min(c.created for c in children)
    latest = max(c.created for c in children)
    return Fragment(
        id=parent_id,
        title=title,
        source=source,
        created=earliest,
        child_ids=child_ids,
        level=level,
        tags=_parent_tags(level, earliest, latest, _speakers(children)),
    )


def _inherit_source(children: list[Fragment]) -> FragmentSource:
    """Build the parent's :class:`FragmentSource` from its children.

    Channel, platform, conversation_id, and original_file are inherited
    from the first child (callers guarantee a shared source key).
    ``author`` is promoted to ``COLLABORATIVE`` when ≥2 distinct authors
    appear; ``interlocutor`` is the comma-joined sorted set of child
    interlocutors.
    """
    first = children[0].source
    authors = {c.source.author for c in children}
    interlocutors = sorted(
        {c.source.interlocutor for c in children if c.source.interlocutor},
    )
    author = Authorship.COLLABORATIVE if len(authors) > 1 else next(iter(authors))
    return FragmentSource(
        platform=first.platform,
        original_file=first.original_file,
        original_encoding=first.original_encoding,
        conversation_id=first.conversation_id,
        channel=first.channel,
        interlocutor=", ".join(interlocutors) if interlocutors else None,
        author=author,
    )


def _parent_tags(
    level: FragmentLevel,
    earliest: datetime,
    latest: datetime,
    speakers: set[str],
) -> list[str]:
    """Build deterministic parent tags (level marker, date-range, speakers)."""
    tags = [
        f"aggregated/{level}",
        f"daterange/{earliest.date().isoformat()}_{latest.date().isoformat()}",
    ]
    tags.extend(f"speaker/{s}" for s in sorted(speakers))
    return tags


def _speakers(fragments: list[Fragment]) -> set[str]:
    """Return distinct speaker identities (``"{author}|{interlocutor}"``)."""
    return {f"{f.source.author}|{f.source.interlocutor or ''}" for f in fragments}


def _source_key(source: FragmentSource) -> str:
    """Build the cross-fragment grouping key (platform|channel|conversation_id)."""
    return f"{source.platform}|{source.channel or ''}|{source.conversation_id or ''}"


def _aggregate_id(
    source_key: str,
    level: FragmentLevel,
    child_ids: list[str],
) -> str:
    """Compute the deterministic parent ID that anchors FEAT-022 idempotency.

    Order of ``child_ids`` matters — a reorder yields a different ID. The
    shape mirrors :func:`creek.ingest.base.generate_fragment_id` so
    downstream code cannot distinguish a deterministic root from an
    aggregated parent.
    """
    child_hash = hashlib.sha256("|".join(child_ids).encode()).hexdigest()
    hash_input = f"{source_key}:{level}:{child_hash}"
    digest = hashlib.sha256(hash_input.encode()).hexdigest()[:12]
    return f"frag-{digest}"


def _minutes_between(earlier: datetime, later: datetime) -> float:
    """Return the absolute gap between two timestamps, in minutes."""
    return abs((later - earlier).total_seconds()) / 60.0


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length float vectors.

    Pure-Python so the aggregator does not pull numpy into the import
    graph for callers who only need exchange/session transitions.
    Returns ``0.0`` when either vector has zero norm.
    """
    dot = sum((x * y for x, y in zip(a, b, strict=True)), 0.0)
    norm_a = math.sqrt(sum((x * x for x in a), 0.0))
    norm_b = math.sqrt(sum((x * x for x in b), 0.0))
    if 0.0 in (norm_a, norm_b):
        return 0.0
    return dot / (norm_a * norm_b)
