"""Vault-driven linking engine for the ``creek link`` command.

Loads every fragment from ``<vault>/01-Fragments/``, dispatches to the
chosen linker stage (embeddings, temporal, or eddies), and reports the
resulting link count back to the CLI. Honours ``--rebuild`` by deleting
the cached embeddings archive so the embedding linker recomputes from
scratch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # no issue: runtime dataclass field
from typing import TYPE_CHECKING, Final

import frontmatter
import yaml
from pydantic import ValidationError

from creek.link.eddies import EddyDetector
from creek.link.embeddings import EmbeddingLinker
from creek.link.temporal import TemporalLinker
from creek.models import Fragment

if TYPE_CHECKING:
    from creek.config import CreekConfig

logger = logging.getLogger(__name__)

_EMBEDDINGS_CACHE_NAME: Final[str] = "embeddings.npz"
"""Filename of the persisted embeddings archive within the vault."""

_EMBEDDINGS_CACHE_DIR: Final[str] = "00-Creek-Meta"


@dataclass(frozen=True)
class LinkSummary:
    """Counts produced by a single ``creek link`` run.

    Attributes:
        method: Linker that was run (``embeddings`` / ``temporal`` /
            ``eddies``).
        fragment_count: Number of fragments loaded from the vault.
        link_count: Edges or clusters produced (resonances, temporal
            links, or eddy clusters depending on ``method``).
    """

    method: str
    fragment_count: int
    link_count: int


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
            the cached embeddings archive before recomputing.

    Returns:
        A :class:`LinkSummary` capturing per-method counts.
    """
    cache_path = vault_path / _EMBEDDINGS_CACHE_DIR / _EMBEDDINGS_CACHE_NAME
    if rebuild and cache_path.exists():
        cache_path.unlink()
        logger.info("Removed cached embeddings archive at %s", cache_path)

    fragments = _load_fragments(vault_path)

    if not fragments:
        return LinkSummary(method=method, fragment_count=0, link_count=0)

    if method == "embeddings":
        link_count = _run_embeddings(
            fragments=fragments,
            config=config,
            cache_path=cache_path,
            rebuild=rebuild,
        )
    elif method == "temporal":
        temporal = TemporalLinker()
        links = temporal.find_temporal_links(
            fragments,
            window_hours=config.linking.temporal_window_hours,
        )
        link_count = len(links)
    else:
        embeddings = _load_or_compute_embeddings(
            fragments=fragments,
            config=config,
            cache_path=cache_path,
            rebuild=False,
        )
        detector = EddyDetector(embeddings=embeddings)
        eddies = detector.detect_eddies(
            fragments,
            min_fragments=config.linking.eddy_min_fragments,
        )
        link_count = len(eddies)

    return LinkSummary(
        method=method,
        fragment_count=len(fragments),
        link_count=link_count,
    )


def _run_embeddings(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
    rebuild: bool,
) -> int:
    """Compute embeddings, optionally re-using the on-disk cache.

    Args:
        fragments: Fragments to embed.
        config: Loaded Creek configuration.
        cache_path: Where to read/write the cached embeddings archive.
        rebuild: When ``True``, ignore any cached archive.

    Returns:
        Number of resonance edges discovered.
    """
    embeddings = _load_or_compute_embeddings(
        fragments=fragments,
        config=config,
        cache_path=cache_path,
        rebuild=rebuild,
    )

    linker = EmbeddingLinker(config=config.embeddings)
    resonances = linker.find_resonances(embeddings)
    return len(resonances)


def _load_or_compute_embeddings(
    *,
    fragments: list[Fragment],
    config: CreekConfig,
    cache_path: Path,
    rebuild: bool,
) -> dict[str, list[float]]:
    """Return embeddings for *fragments*, hitting the cache when fresh.

    Args:
        fragments: Fragments to embed.
        config: Loaded Creek configuration.
        cache_path: Embedding archive path.
        rebuild: When ``True``, ignore the cache and recompute.

    Returns:
        Mapping of fragment ID to embedding vector.
    """
    linker = EmbeddingLinker(config=config.embeddings)

    if not rebuild and cache_path.exists():
        try:
            cached = linker.load_embeddings(cache_path)
        except (OSError, ValueError):
            logger.warning("Cached embeddings unreadable; recomputing")
            cached = {}
        existing_ids = {fragment.id for fragment in fragments} & cached.keys()
        new = linker.generate_embeddings(fragments, existing_ids=existing_ids)
        merged = {**cached, **new}
    else:
        merged = linker.generate_embeddings(fragments)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        linker.save_embeddings(merged, cache_path)
    except OSError as exc:
        # Disk full / permission denied / read-only volume. Linking
        # itself succeeded — losing the cache only costs a recompute on
        # the next run, so we degrade gracefully instead of crashing.
        logger.warning(
            "Failed to persist embeddings cache to %s: %s",
            cache_path,
            exc,
        )
    return merged


def _load_fragments(vault_path: Path) -> list[Fragment]:
    """Load every fragment file under ``<vault>/01-Fragments/``.

    Files that fail to parse are logged and skipped; they should never
    block a linking pass.

    Args:
        vault_path: Vault root.

    Returns:
        Sorted list of :class:`Fragment` instances.
    """
    fragments_root = vault_path / "01-Fragments"
    if not fragments_root.exists():
        return []

    out: list[Fragment] = []
    for md_file in sorted(fragments_root.rglob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except (OSError, ValueError, yaml.YAMLError):
            logger.debug("Skipping unreadable markdown file: %s", md_file)
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "fragment":
            continue
        try:
            out.append(Fragment.model_validate(metadata))
        except ValidationError:
            logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
            continue
    return out
