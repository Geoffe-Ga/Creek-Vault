"""Vault-driven linking engine for the ``creek link`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
chosen linker stage (embeddings, temporal, or eddies), and reports the
resulting link count back to the CLI. Honours ``--rebuild`` by deleting
the cached embeddings parquet so the embedding linker recomputes from
scratch.

The eddies linker materialises its output inline — it writes an
``Eddy`` markdown file per detected cluster under ``03-Eddies/`` and
updates each member fragment's ``eddies:`` frontmatter with the
corresponding wiki-link, so the CLI's summary counts match what
operators see on disk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING

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
from creek.vault.reader import iter_vault_fragments
from creek.vault.writer import VaultWriter

if TYPE_CHECKING:
    from creek.config import CreekConfig
    from creek.models import Eddy, Fragment

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
            ``eddies``).
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
        member_fragments_updated: Fragments whose on-disk ``eddies:``
            frontmatter was rewritten to include a new wiki-link. Only
            populated for ``method == "eddies"``.
    """

    method: str
    fragment_count: int
    link_count: int
    similarity_edges: int = 0
    eddies_detected: int = 0
    eddies_written: int = 0
    member_fragments_updated: int = 0


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
        method: ``"embeddings"``, ``"temporal"``, or ``"eddies"``.
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

    # method == "eddies"
    return _run_eddies(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
        vault_path=vault_path,
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
    detector = EddyDetector(embeddings=embeddings)
    eddies = detector.detect_eddies(
        fragments,
        min_fragments=config.linking.eddy_min_fragments,
    )

    if not eddies:
        return LinkSummary(
            method="eddies",
            fragment_count=len(fragments),
            link_count=0,
        )

    updated_fragments = detector.assign_fragments_to_eddies(fragments, eddies)
    written_paths = _write_eddy_files(eddies, vault_path)
    fragments_updated = _persist_fragment_eddy_updates(
        original=fragments,
        updated=updated_fragments,
        vault_path=vault_path,
    )

    return LinkSummary(
        method="eddies",
        fragment_count=len(fragments),
        link_count=len(eddies),
        eddies_detected=len(eddies),
        eddies_written=len(written_paths),
        member_fragments_updated=fragments_updated,
    )


def _write_eddy_files(eddies: list[Eddy], vault_path: Path) -> list[Path]:
    """Persist each eddy as a markdown file under ``03-Eddies/``.

    Uses :class:`VaultWriter` so per-directory ID indexing, atomic
    create, and provenance logging all behave consistently with the rest
    of the pipeline. A failed write (``OSError`` from a full disk or
    read-only volume) is logged and the remaining eddies are still
    attempted — losing one eddy file should not break the others.

    Args:
        eddies: Eddies returned by :meth:`EddyDetector.detect_eddies`.
        vault_path: Vault root.

    Returns:
        Paths of the eddy files that were successfully written.
    """
    try:
        writer = VaultWriter(vault_path=vault_path)
    except FileNotFoundError:
        # The vault scaffold is incomplete (no ``00-Creek-Meta`` or
        # ``01-Fragments`` yet). The link engine is invoked against
        # whatever the operator points at; we shouldn't crash on a
        # half-set-up vault when there's nothing to materialise into.
        logger.warning(
            "Skipping eddy materialisation: vault %s is missing required dirs",
            vault_path,
        )
        return []

    written: list[Path] = []
    for eddy in eddies:
        try:
            path = writer.write_eddy(eddy)
        except OSError as exc:
            logger.warning(
                "Failed to write eddy %s (%s) under %s: %s",
                eddy.id,
                eddy.title,
                vault_path / "03-Eddies",
                exc,
            )
            continue
        written.append(path)
    return written


def _persist_fragment_eddy_updates(
    *,
    original: list[Fragment],
    updated: list[Fragment],
    vault_path: Path,
) -> int:
    """Rewrite fragment frontmatter when the ``eddies:`` list changed.

    Re-reads each fragment file so non-Fragment frontmatter keys (e.g.
    operator-applied tags, ``classification_method``) are preserved
    verbatim — the same approach the classify engine uses for in-place
    fragment rewrites. Only fragments whose ``eddies`` list actually
    grew are touched.

    Args:
        original: Fragments as loaded from disk.
        updated: Fragments returned by
            :meth:`EddyDetector.assign_fragments_to_eddies`. Must be
            the same length as *original* and in the same order.
        vault_path: Vault root.

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
        if before.eddies == after.eddies:
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
