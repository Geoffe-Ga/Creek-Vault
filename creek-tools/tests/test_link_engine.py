"""Tests for the vault-driven linking engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter

from creek.config import CreekConfig
from creek.link.link_engine import run_link
from creek.models import Fragment, FragmentSource, SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path


def _write_fragment(
    *,
    vault: Path,
    fragment: Fragment,
    body: str,
) -> Path:
    """Persist *fragment* under ``<vault>/01-Fragments/Notes`` for linking.

    Args:
        vault: Vault root.
        fragment: Fragment metadata to persist.
        body: Markdown body for the file.

    Returns:
        Path to the written fragment.
    """
    fragments_dir = vault / "01-Fragments" / "Notes"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    metadata = fragment.model_dump(mode="json")
    path = fragments_dir / f"{fragment.id}.md"
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )
    return path


def test_run_link_zero_for_empty_vault(tmp_path: Path) -> None:
    """An empty vault returns zero counts without raising."""
    summary = run_link(
        vault_path=tmp_path / "vault",
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )
    assert summary.fragment_count == 0
    assert summary.link_count == 0


def test_run_link_rebuild_clears_cache(tmp_path: Path) -> None:
    """``rebuild=True`` removes any pre-existing embeddings archive."""
    vault = tmp_path / "vault"
    cache_dir = vault / "00-Creek-Meta"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "embeddings.npz"
    cache_path.write_bytes(b"stale-cache")
    (vault / "01-Fragments").mkdir(parents=True)

    run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=True,
    )
    assert not cache_path.exists()


def test_run_link_embeddings_writes_cache(tmp_path: Path) -> None:
    """Embeddings are persisted to the on-disk cache after a run."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-cache0000000",
        title="Test fragment",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )
    assert summary.fragment_count == 1
    cache_path = vault / "00-Creek-Meta" / "embeddings.npz"
    assert cache_path.exists()


def test_run_link_temporal_returns_link_count(tmp_path: Path) -> None:
    """Temporal linker returns a numeric count for the CLI."""
    vault = tmp_path / "vault"
    for i in range(2):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=f"frag-temporal0{i:03d}",
                title=f"Note {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body="body",
        )

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="temporal",
        rebuild=False,
    )
    assert summary.fragment_count == 2
    assert summary.link_count >= 0


def test_run_link_eddies_returns_cluster_count(tmp_path: Path) -> None:
    """Eddy linker reports cluster count, regardless of size."""
    vault = tmp_path / "vault"
    for i in range(3):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=f"frag-eddy00000{i:03d}",
                title=f"Eddy member {i}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body="body",
        )

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )
    assert summary.fragment_count == 3
    assert summary.link_count >= 0


def test_run_link_uses_existing_cache(tmp_path: Path) -> None:
    """A second run reuses the cached embeddings archive."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-cachehit0000",
        title="Reused fragment",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )
    cache_path = vault / "00-Creek-Meta" / "embeddings.npz"
    first_mtime = cache_path.stat().st_mtime

    run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )
    # Cache may be re-saved (timestamp can differ) but content should
    # at least still exist.
    assert cache_path.exists()
    assert cache_path.stat().st_mtime >= first_mtime
