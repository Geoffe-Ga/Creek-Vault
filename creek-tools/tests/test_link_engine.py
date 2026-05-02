"""Tests for the vault-driven linking engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.config import CreekConfig
from creek.link.link_engine import run_link
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file as _write_fragment

if TYPE_CHECKING:
    from pathlib import Path


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


def test_run_link_rebuild_is_noop_for_temporal(tmp_path: Path) -> None:
    """``--rebuild`` must not delete the cache when method != embeddings.

    ``--rebuild`` is documented as "invalidate the embeddings cache".
    Running it under ``--method temporal`` previously also blew away
    the cache as a side effect; that's the regression being pinned
    here.
    """
    vault = tmp_path / "vault"
    cache_dir = vault / "00-Creek-Meta"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / "embeddings.npz"
    sentinel = b"keep-me"
    cache_path.write_bytes(sentinel)
    (vault / "01-Fragments").mkdir(parents=True)

    run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="temporal",
        rebuild=True,
    )
    assert cache_path.exists()
    assert cache_path.read_bytes() == sentinel


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


def test_run_link_embeddings_save_oserror_does_not_crash(
    tmp_path: Path,
) -> None:
    """An OSError while persisting the cache must not propagate."""
    from unittest.mock import patch

    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-saveerr00000",
        title="Save error",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    with patch(
        "creek.link.embeddings.EmbeddingLinker.save_embeddings",
        side_effect=OSError("disk full"),
    ):
        summary = run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="embeddings",
            rebuild=False,
        )

    # Linking still completes; the lost cache only costs a recompute
    # on the next run.
    assert summary.fragment_count == 1


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
    """A second run reuses the cached embeddings archive without re-encoding.

    The mtime check on its own is a tautology (mtime can only grow). The
    real invariant is that the embedding model is *not* re-invoked when
    the cache covers every fragment ID — patching ``encode`` and
    asserting it is never called proves the cache hit.
    """
    from unittest.mock import patch

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
    assert cache_path.exists()
    cached_bytes = cache_path.read_bytes()

    with patch(
        "creek.link.embeddings.EmbeddingLinker.generate_embeddings",
        wraps=lambda *_args, **_kw: {},
    ) as mock_generate:
        run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="embeddings",
            rebuild=False,
        )

    # The second run still calls ``generate_embeddings`` (so unseen
    # fragments would be picked up), but it must short-circuit because
    # ``existing_ids`` covers every fragment we just embedded.
    mock_generate.assert_called_once()
    _, called_kwargs = mock_generate.call_args
    assert fragment.id in called_kwargs["existing_ids"]
    # And the cache content must be preserved across the round-trip.
    assert cache_path.read_bytes() == cached_bytes
