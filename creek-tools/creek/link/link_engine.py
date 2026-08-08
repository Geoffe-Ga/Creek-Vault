"""Vault-driven linking engine for the ``creek link`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
chosen linker stage (embeddings, temporal, eddies, or threads), and reports
the resulting link count back to the CLI. Honours ``--rebuild`` by deleting
the cached embeddings parquet so the embedding linker recomputes from
scratch.

The eddies and threads linkers materialise their output inline — they write
one markdown page per detected cluster/thread (under ``03-Eddies/`` /
``02-Threads/{status}/``) and update each member fragment's ``eddies:`` /
``threads:`` frontmatter with the corresponding wiki-link, so the CLI's
summary counts match what operators see on disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING, Protocol, TypeVar

import frontmatter

from creek.link.eddies import EddyDetector
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
from creek.link.temporal import TemporalLinker
from creek.link.threads import ThreadDetector
from creek.vault.reader import iter_vault_fragments
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from creek.config import CreekConfig
    from creek.models import Fragment


class _Materialisable(Protocol):
    """A detected link model that can be written to the vault as a page.

    Both :class:`~creek.models.Eddy` and :class:`~creek.models.Thread` satisfy
    this — the generic materialiser only needs an ``id`` and ``title`` for
    logging.
    """

    id: str
    title: str


_M = TypeVar("_M", bound=_Materialisable)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkSummary:
    """Counts produced by a single ``creek link`` run.

    The structured fields make the contract per-method explicit so the
    CLI can phrase what actually happened — pairwise similarity edges
    cached in a parquet are *not* per-fragment frontmatter links, and an
    eddy cluster *detected* in memory is not the same as an eddy file
    *written* to disk. ``link_count`` is preserved as a back-compat
    mirror of the method-specific count.

    Attributes:
        method: Linker that was run (``embeddings`` / ``temporal`` /
            ``eddies`` / ``threads``).
        fragment_count: Number of fragments loaded from the vault.
        link_count: Generic count for back-compat; mirrors the
            method-specific count (resonance edges, temporal links, or
            eddies detected).
        similarity_edges: Pairwise cosine-similarity edges cached in the
            embeddings parquet. Only populated for ``method ==
            "embeddings"`` (zero elsewhere).
        eddies_detected: Eddy clusters returned by the detector. Only
            populated for ``method == "eddies"``.
        eddies_written: Eddy markdown files persisted under
            ``03-Eddies/`` by the materialisation step. Equal to
            ``eddies_detected`` when every write succeeds; smaller when
            duplicate-id collisions short-circuit a write or the writer
            fails. Only populated for ``method == "eddies"``.
        member_fragments_updated: Fragments whose on-disk ``eddies:`` or
            ``threads:`` frontmatter was rewritten to include a new wiki-link.
            Populated for ``method == "eddies"`` and ``method == "threads"``.
        threads_detected: Narrative threads returned by the detector. Only
            populated for ``method == "threads"``.
        threads_written: Thread markdown pages persisted under
            ``02-Threads/{status}/`` by the materialisation step. Equal to
            ``threads_detected`` when every write succeeds; smaller when a
            write fails. Only populated for ``method == "threads"``.
        largest_cluster_fragments: Member count of the biggest eddy or
            thread emitted. The operator-visible proof that no cluster
            swallowed the vault (issue #880); ``0`` when nothing was
            detected.
        clusters_split: Clusters that exceeded the configured ceiling and
            were re-clustered at a tighter parameter.
        oversized_discarded: Fragments dropped to noise because their
            cluster stayed above the ceiling even after the split budget
            was spent. Those fragments carry no ``eddies:``/``threads:``
            link at all, so a non-zero value is worth an operator's
            attention.
    """

    method: str
    fragment_count: int
    link_count: int
    similarity_edges: int = 0
    eddies_detected: int = 0
    eddies_written: int = 0
    member_fragments_updated: int = 0
    threads_detected: int = 0
    threads_written: int = 0
    largest_cluster_fragments: int = 0
    clusters_split: int = 0
    oversized_discarded: int = 0


def run_link(
    *,
    vault_path: Path,
    config: CreekConfig,
    method: str,
    rebuild: bool,
) -> LinkSummary:
    """Run a single linker stage against the vault.

    Args:
        vault_path: Vault root.
        config: Loaded Creek configuration.
        method: ``"embeddings"``, ``"temporal"``, ``"eddies"``, or
            ``"threads"``.
        rebuild: When ``True`` and ``method == "embeddings"``, delete
            the cached embeddings parquet before recomputing.

    Returns:
        A :class:`LinkSummary` capturing per-method counts.
    """
    cache_path = embeddings_cache_path(vault_path)
    # ``--rebuild`` is documented as "invalidate the embeddings cache",
    # so only act on it for the embeddings linker. Temporal and eddy
    # methods don't own the cache and shouldn't side-effect it — eddy
    # *consumes* the cache, so blowing it away under that method would
    # silently force a recompute the operator didn't ask for.
    if rebuild and method == "embeddings" and cache_path.exists():
        cache_path.unlink()
        logger.info("Removed cached embeddings parquet at %s", cache_path)

    fragments = _load_fragments(vault_path)

    if not fragments:
        return LinkSummary(method=method, fragment_count=0, link_count=0)

    if method == "embeddings":
        link_count = _run_embeddings(
            fragments=fragments,
            config=config,
            cache_path=cache_path,
        )
        return LinkSummary(
            method=method,
            fragment_count=len(fragments),
            link_count=link_count,
            similarity_edges=link_count,
        )
    if method == "temporal":
        temporal = TemporalLinker()
        links = temporal.find_temporal_links(
            fragments,
            window_hours=config.linking.temporal_window_hours,
        )
        return LinkSummary(
            method=method,
            fragment_count=len(fragments),
            link_count=len(links),
        )
    if method == "threads":
        return _run_threads(
            fragments=fragments,
            config=config,
            cache_path=cache_path,
            vault_path=vault_path,
        )

    # method == "eddies"
    return _run_eddies(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
        vault_path=vault_path,
    )


def _run_threads(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
    vault_path: Path,
) -> LinkSummary:
    """Detect narrative threads and materialise one page per thread inline.

    Mirrors :func:`_run_eddies`: the existing :class:`ThreadDetector` already
    produces fully-formed :class:`~creek.models.Thread` models and a membership
    map, and :meth:`VaultWriter.write_thread` already routes a page under
    ``02-Threads/{Active,Dormant,Resolved}/`` by the thread's status. The only
    missing step was the bulk filesystem write — previously a page existed only
    when an operator ran ``creek compile`` per thread, leaving 02-Threads
    near-empty while fragments carried ``threads:`` links to non-existent pages.

    Detection semantics are unchanged — embeddings are loaded from cache to feed
    the detector's semantic topic-consistency exactly as the pipeline does, and
    ``thread_min_fragments`` comes from config. The function then writes one page
    per thread and rewrites each member fragment's ``threads:`` frontmatter so
    the wiki-links resolve to the pages now on disk.

    Args:
        fragments: Fragments loaded from the vault.
        config: Loaded Creek configuration.
        cache_path: Embeddings parquet path.
        vault_path: Vault root.

    Returns:
        A populated :class:`LinkSummary` for the threads method.
    """
    embeddings = _load_or_compute_embeddings(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
    )
    detector = ThreadDetector.from_linking_config(embeddings, config.linking)
    threads = detector.detect_threads(
        fragments,
        min_fragments=config.linking.thread_min_fragments,
    )

    if not threads:
        return LinkSummary(
            method="threads",
            fragment_count=len(fragments),
            link_count=0,
            clusters_split=detector.clusters_split,
            oversized_discarded=detector.oversized_discarded,
        )

    updated_fragments = detector.assign_fragments_to_threads(fragments, threads)
    written_paths = _materialise_link_models(
        threads,
        vault_path,
        kind="thread",
        write=lambda writer, thread: writer.write_thread(thread),
    )
    fragments_updated = _persist_fragment_link_updates(
        original=fragments,
        updated=updated_fragments,
        vault_path=vault_path,
        changed=lambda before, after: before.threads != after.threads,
    )

    return LinkSummary(
        method="threads",
        fragment_count=len(fragments),
        link_count=len(threads),
        threads_detected=len(threads),
        threads_written=len(written_paths),
        member_fragments_updated=fragments_updated,
        largest_cluster_fragments=_largest_cluster(detector.thread_members),
        clusters_split=detector.clusters_split,
        oversized_discarded=detector.oversized_discarded,
    )


def _run_eddies(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
    vault_path: Path,
) -> LinkSummary:
    """Detect eddies and materialise them inline.

    Inline materialisation is chosen over deferring to ``creek compile``
    for two reasons:

    1. The detector already produces fully-formed :class:`Eddy` models
       and a membership map; the only missing step is the filesystem
       write — :meth:`VaultWriter.write_eddy` already exists.
    2. The ``creek compile`` flow is a per-target manual invocation; it
       is not the bulk persistence path for detected eddies.

    The function:

    * runs DBSCAN over the cached embeddings,
    * writes one ``Eddy`` markdown file per detected cluster under
      ``03-Eddies/``,
    * rewrites each member fragment's ``eddies:`` frontmatter to include
      the corresponding ``[[Eddy Title]]`` wiki-link (no rewrites for
      fragments that already carried the link).

    Disk failures degrade gracefully: a write that raises ``OSError`` is
    logged and skipped so a single bad file cannot block the rest of
    the run, and the returned summary reflects the partial work.

    Args:
        fragments: Fragments loaded from the vault.
        config: Loaded Creek configuration.
        cache_path: Embeddings parquet path.
        vault_path: Vault root.

    Returns:
        A populated :class:`LinkSummary` for the eddies method.
    """
    embeddings = _load_or_compute_embeddings(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
    )
    detector = EddyDetector.from_linking_config(embeddings, config.linking)
    eddies = detector.detect_eddies(
        fragments,
        min_fragments=config.linking.eddy_min_fragments,
    )

    if not eddies:
        return LinkSummary(
            method="eddies",
            fragment_count=len(fragments),
            link_count=0,
            clusters_split=detector.clusters_split,
            oversized_discarded=detector.oversized_discarded,
        )

    updated_fragments = detector.assign_fragments_to_eddies(fragments, eddies)
    written_paths = _materialise_link_models(
        eddies,
        vault_path,
        kind="eddy",
        write=lambda writer, eddy: writer.write_eddy(eddy),
    )
    fragments_updated = _persist_fragment_link_updates(
        original=fragments,
        updated=updated_fragments,
        vault_path=vault_path,
        changed=lambda before, after: before.eddies != after.eddies,
    )

    return LinkSummary(
        method="eddies",
        fragment_count=len(fragments),
        link_count=len(eddies),
        eddies_detected=len(eddies),
        eddies_written=len(written_paths),
        member_fragments_updated=fragments_updated,
        largest_cluster_fragments=_largest_cluster(detector.eddy_members),
        clusters_split=detector.clusters_split,
        oversized_discarded=detector.oversized_discarded,
    )


def _largest_cluster(members_by_id: dict[str, list[str]]) -> int:
    """Return the member count of the biggest cluster in a membership map.

    Args:
        members_by_id: Cluster ID to member fragment IDs, as exposed by
            :attr:`EddyDetector.eddy_members` /
            :attr:`ThreadDetector.thread_members`.

    Returns:
        The largest membership size, or ``0`` when nothing was detected.
    """
    return max((len(members) for members in members_by_id.values()), default=0)


def _materialise_link_models(
    models: Sequence[_M],
    vault_path: Path,
    *,
    kind: str,
    write: Callable[[VaultWriter, _M], Path],
) -> list[Path]:
    """Persist each detected link *model* as a markdown page via *write*.

    Shared by the eddies and threads linkers: both already produce
    fully-formed models and own a :class:`VaultWriter` page-writer
    (:meth:`~VaultWriter.write_eddy` / :meth:`~VaultWriter.write_thread`), so
    the only generic step is the filesystem write. Using :class:`VaultWriter`
    keeps per-directory ID indexing, atomic create, and provenance logging
    consistent with the rest of the pipeline. A failed write (``OSError`` from a
    full disk or read-only volume) is logged and the remaining models are still
    attempted — losing one page should not break the others.

    Args:
        models: Detected models to write (each carries ``id`` + ``title``).
        vault_path: Vault root.
        kind: Human-readable model kind for log messages (``"eddy"`` /
            ``"thread"``).
        write: The :class:`VaultWriter` method that persists one model and
            returns its path.

    Returns:
        Paths of the pages that were successfully written.
    """
    try:
        writer = VaultWriter(vault_path=vault_path)
    except FileNotFoundError:
        # The vault scaffold is incomplete (no ``00-Creek-Meta`` or
        # ``01-Fragments`` yet). The link engine is invoked against
        # whatever the operator points at; we shouldn't crash on a
        # half-set-up vault when there's nothing to materialise into.
        logger.warning(
            "Skipping %s materialisation: vault %s is missing required dirs",
            kind,
            vault_path,
        )
        return []

    written: list[Path] = []
    for model in models:
        try:
            path = write(writer, model)
        except OSError as exc:
            logger.warning(
                "Failed to write %s %s (%s) under %s: %s",
                kind,
                model.id,
                model.title,
                vault_path,
                exc,
            )
            continue
        written.append(path)
    return written


def _persist_fragment_link_updates(
    *,
    original: list[Fragment],
    updated: list[Fragment],
    vault_path: Path,
    changed: Callable[[Fragment, Fragment], bool],
) -> int:
    """Rewrite fragment frontmatter when a link list (eddies/threads) changed.

    Re-reads each fragment file so non-Fragment frontmatter keys (e.g.
    operator-applied tags, ``classification_method``) are preserved
    verbatim — the same approach the classify engine uses for in-place
    fragment rewrites. Only fragments for which *changed* reports a difference
    between the loaded and the assigned copy are touched.

    Args:
        original: Fragments as loaded from disk.
        updated: Fragments returned by the detector's ``assign_*`` method. Must
            be the same length as *original* and in the same order.
        vault_path: Vault root.
        changed: Predicate deciding whether a fragment's relevant link list
            (``eddies`` or ``threads``) differs and so needs rewriting.

    Returns:
        Number of fragment files that were successfully rewritten.
    """
    path_by_id: dict[str, Path] = {}
    raw_by_id: dict[str, dict[str, object]] = {}
    body_by_id: dict[str, str] = {}
    for path, frag, body, raw in iter_vault_fragments(vault_path / "01-Fragments"):
        path_by_id[frag.id] = path
        raw_by_id[frag.id] = raw
        body_by_id[frag.id] = body

    written = 0
    for before, after in zip(original, updated, strict=True):
        if not changed(before, after):
            continue
        md_path = path_by_id.get(after.id)
        if md_path is None:
            # The fragment landed in the detector via the in-memory
            # path but doesn't have a backing file (test scenario or
            # the file was unlinked between load and rewrite). Skip
            # rather than crash — there is nothing to update.
            logger.debug(
                "Skipping fragment frontmatter update for %s: no file on disk",
                after.id,
            )
            continue
        raw = raw_by_id.get(after.id, {}).copy()
        raw.update(after.model_dump(mode="json"))
        post = frontmatter.Post(content=body_by_id.get(after.id, ""))
        post.metadata.update(raw)
        try:
            md_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Failed to rewrite fragment %s at %s: %s",
                after.id,
                md_path,
                exc,
            )
            continue
        written += 1
    return written


def _run_embeddings(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
) -> int:
    """Compute embeddings, optionally re-using the on-disk cache.

    Args:
        fragments: Fragments to embed.
        config: Loaded Creek configuration.
        cache_path: Where to read/write the cached embeddings parquet.

    Returns:
        Number of resonance edges discovered.
    """
    embeddings = _load_or_compute_embeddings(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
    )

    # FEAT-024: hierarchy-aware filtering needs both the fragment map
    # (parent/child relations) and the configured sibling skip window.
    fragments_by_id = {f.id: f for f in fragments}
    resonances = EmbeddingLinker(config=config.embeddings).find_resonances(
        embeddings,
        fragments_by_id,
        sibling_skip_window=config.linking.hierarchy_sibling_skip_window,
    )
    return len(resonances)


def _load_or_compute_embeddings(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
) -> dict[str, list[float]]:
    """Return embeddings for *fragments*, hitting the cache when fresh.

    The cache is consulted per-fragment: rows whose ``content_hash``
    matches the current fragment text and whose ``model_name`` matches
    ``config.embeddings.model`` are reused verbatim; everything else
    is recomputed. ``--rebuild`` handling lives upstream (the cache
    file is deleted before this function runs), so a missing file
    naturally degrades to a full recompute.

    Args:
        fragments: Fragments to embed.
        config: Loaded Creek configuration.
        cache_path: Embedding cache path.

    Returns:
        Mapping of fragment ID to embedding vector.
    """
    linker = EmbeddingLinker(config=config.embeddings)

    cached = _safe_load_cache(linker, cache_path)
    fresh_ids = _ids_with_fresh_cache(fragments, cached)

    new_vectors = linker.generate_embeddings(fragments, existing_ids=fresh_ids)

    embeddings: dict[str, list[float]] = {
        frag.id: cached[frag.id].vector for frag in fragments if frag.id in fresh_ids
    }
    embeddings.update(new_vectors)

    _persist_cache(linker, fragments, cached, new_vectors, fresh_ids, cache_path)
    return embeddings


def _safe_load_cache(
    linker: EmbeddingLinker,
    cache_path: Path,
) -> dict[str, CachedEmbedding]:
    """Load the cache while degrading gracefully on corrupted files.

    Args:
        linker: Linker bound to the current embeddings config.
        cache_path: Cache parquet path.

    Returns:
        The cache mapping, or an empty dict if the file is unreadable.
    """
    try:
        return linker.load_cache(cache_path)
    except (OSError, ValueError):
        logger.warning("Cached embeddings unreadable; recomputing")
        return {}


def _ids_with_fresh_cache(
    fragments: list[Fragment],
    cached: dict[str, CachedEmbedding],
) -> set[str]:
    """Return the IDs of fragments whose cache entry is still valid.

    An entry is fresh when its ``content_hash`` matches the current
    fragment text. Model invalidation is already applied by
    :meth:`EmbeddingLinker.load_cache`, so any entry still in ``cached``
    is from the active model.

    Args:
        fragments: All fragments in this run.
        cached: Cache entries loaded from disk.

    Returns:
        Set of fragment IDs that can skip recomputation.
    """
    fresh: set[str] = set()
    for frag in fragments:
        entry = cached.get(frag.id)
        if entry is None:
            continue
        expected_hash = content_hash_for_text(fragment_embedding_text(frag))
        if entry.content_hash == expected_hash:
            fresh.add(frag.id)
    return fresh


def _persist_cache(
    linker: EmbeddingLinker,
    fragments: list[Fragment],
    cached: dict[str, CachedEmbedding],
    new_vectors: dict[str, list[float]],
    fresh_ids: set[str],
    cache_path: Path,
) -> None:
    """Write the merged cache back to disk, degrading gracefully on IO errors.

    Args:
        linker: Linker bound to the current embeddings config.
        fragments: Fragments in this run; defines the IDs we keep.
        cached: Cache entries loaded from disk before recompute.
        new_vectors: Vectors produced this run.
        fresh_ids: Fragment IDs whose cached entry is still valid.
        cache_path: Cache parquet path.
    """
    new_entries = linker.build_cache_entries(fragments, new_vectors)
    entries = {frag.id: cached[frag.id] for frag in fragments if frag.id in fresh_ids}
    entries.update(new_entries)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        linker.save_cache(entries, cache_path)
    except OSError as exc:
        # Disk full / permission denied / read-only volume. Linking
        # itself succeeded — losing the cache only costs a recompute on
        # the next run, so we degrade gracefully instead of crashing.
        logger.warning(
            "Failed to persist embeddings cache to %s: %s",
            cache_path,
            exc,
        )


def _load_fragments(vault_path: Path) -> list[Fragment]:
    """Load every fragment file under ``<vault>/01-Fragments/``.

    Delegates the per-file validation chain to
    :func:`creek.vault.reader.iter_vault_fragments` so the link
    engine, classify engine, and review runner share one definition
    of "is this a Creek fragment?" Linking doesn't surface I/O
    failures to the operator (yet) — they're skipped at DEBUG
    level inside the helper, the same way they were before.

    Args:
        vault_path: Vault root.

    Returns:
        Sorted list of :class:`Fragment` instances.
    """
    records = iter_vault_fragments(vault_path / "01-Fragments")
    return [fragment for _path, fragment, _body, _raw in records]
