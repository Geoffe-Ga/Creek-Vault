"""Tests for the vault-driven linking engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.config import CreekConfig, EmbeddingsConfig
from creek.link.embeddings import (
    EmbeddingLinker,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
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
    """``rebuild=True`` removes any pre-existing embeddings parquet."""
    vault = tmp_path / "vault"
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True)
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
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True)
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
    """Embeddings are persisted to the on-disk parquet cache after a run."""
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
    cache_path = embeddings_cache_path(vault)
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
        "creek.link.embeddings.EmbeddingLinker.save_cache",
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


def test_run_link_unchanged_fragments_skip_recompute(tmp_path: Path) -> None:
    """Re-running link must short-circuit fragments whose hash is unchanged.

    The acceptance test for INC-006: with a fully fresh cache, the
    sentence-transformer is never invoked on the second run.
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

    with patch.object(EmbeddingLinker, "generate_embeddings") as mock_generate:
        mock_generate.return_value = {}
        run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="embeddings",
            rebuild=False,
        )

    mock_generate.assert_called_once()
    _, called_kwargs = mock_generate.call_args
    assert fragment.id in called_kwargs["existing_ids"]


def test_run_link_changed_title_triggers_recompute(tmp_path: Path) -> None:
    """A fragment whose title changes must be re-embedded on the next run."""
    from unittest.mock import patch

    vault = tmp_path / "vault"
    fragment_old = Fragment(
        id="frag-changetitle0",
        title="Original title",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment_old, body="body")
    run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )

    # Rewrite the same fragment with a different title.
    import shutil

    shutil.rmtree(vault / "01-Fragments")
    fragment_new = Fragment(
        id="frag-changetitle0",
        title="Updated title",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment_new, body="body")

    with patch.object(EmbeddingLinker, "generate_embeddings") as mock_generate:
        mock_generate.return_value = {fragment_new.id: [0.0] * 384}
        run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="embeddings",
            rebuild=False,
        )

    mock_generate.assert_called_once()
    _, called_kwargs = mock_generate.call_args
    # The stale fragment is NOT in existing_ids, so it gets recomputed.
    assert fragment_new.id not in called_kwargs["existing_ids"]


def test_run_link_model_change_invalidates_entire_cache(tmp_path: Path) -> None:
    """Switching ``embeddings.model`` recomputes every fragment.

    Acceptance test for INC-006: cache invalidation by model name.
    """
    from unittest.mock import patch

    from creek.config import EmbeddingsConfig

    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-modelswap00",
        title="Stable title",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    first_config = CreekConfig(embeddings=EmbeddingsConfig(model="model-a"))
    run_link(
        vault_path=vault,
        config=first_config,
        method="embeddings",
        rebuild=False,
    )

    second_config = CreekConfig(embeddings=EmbeddingsConfig(model="model-b"))
    with patch.object(EmbeddingLinker, "generate_embeddings") as mock_generate:
        mock_generate.return_value = {fragment.id: [0.0] * 384}
        run_link(
            vault_path=vault,
            config=second_config,
            method="embeddings",
            rebuild=False,
        )

    mock_generate.assert_called_once()
    _, called_kwargs = mock_generate.call_args
    # Model swap invalidates the cache entry, so the fragment is NOT in existing_ids.
    assert fragment.id not in called_kwargs["existing_ids"]


def test_run_link_persists_content_hash_and_model(tmp_path: Path) -> None:
    """The persisted parquet records the content hash and active model."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-persist0000",
        title="Persisted fragment",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    config = CreekConfig(embeddings=EmbeddingsConfig(model="checkmodel"))
    run_link(
        vault_path=vault,
        config=config,
        method="embeddings",
        rebuild=False,
    )

    cache_path = embeddings_cache_path(vault)
    loaded = EmbeddingLinker(config=config.embeddings).load_cache(cache_path)
    assert fragment.id in loaded
    expected_hash = content_hash_for_text(fragment_embedding_text(fragment))
    assert loaded[fragment.id].content_hash == expected_hash
    assert loaded[fragment.id].model_name == "checkmodel"


def test_run_link_unreadable_cache_recomputes(tmp_path: Path) -> None:
    """A corrupted cache file must not crash the run; it triggers recompute."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-corruptdat0",
        title="Corrupted cache test",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"not a parquet file")

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )
    assert summary.fragment_count == 1
    # Cache was rewritten as a valid parquet.
    loaded = EmbeddingLinker(config=CreekConfig().embeddings).load_cache(cache_path)
    assert fragment.id in loaded
