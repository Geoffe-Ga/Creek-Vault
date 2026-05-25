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


# ----- Issue #338: eddy materialization tests --------------------------------


def _embedded_eddy_fragments(
    vault: Path,
    *,
    count: int = 6,
) -> list[Fragment]:
    """Seed *vault* with *count* fragments and matching tight-cluster embeddings.

    Mirrors the production layout: each fragment lives under
    ``01-Fragments/Notes/`` and there is a parquet cache at
    ``00-Creek-Meta/embeddings.parquet`` with near-identical vectors so
    DBSCAN forms one dense, temporally scattered cluster (an eddy).

    Args:
        vault: Vault root.
        count: Number of fragments to seed (defaults to 6, above the
            default ``min_fragments`` of 5).

    Returns:
        The list of created fragments in creation order.
    """
    from datetime import datetime, timedelta

    # Two-year spread keeps the Spearman correlation low so the cluster
    # qualifies as a scattered eddy rather than a directional thread.
    base = datetime(2026, 4, 1)
    day_offsets = [600, 45, 400, 120, 500, 15, 250, 75]
    fragments: list[Fragment] = []
    for i in range(count):
        frag = Fragment(
            id=f"frag-issue338-{i:03d}",
            title="Ceramics studio practice",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            created=base - timedelta(days=day_offsets[i % len(day_offsets)]),
        )
        _write_fragment(vault=vault, fragment=frag, body="body")
        fragments.append(frag)

    # Persist the embeddings cache so the eddies linker can consume it
    # without invoking the sentence-transformer.
    config = CreekConfig()
    linker = EmbeddingLinker(config=config.embeddings)
    vectors = {frag.id: [1.0, 0.0, 0.0] for frag in fragments}
    entries = linker.build_cache_entries(fragments, vectors)
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(entries, cache_path)
    return fragments


def test_run_link_eddies_materialises_eddy_files(tmp_path: Path) -> None:
    """The eddies linker writes a markdown file under ``03-Eddies/`` per detected eddy.

    Issue #338 Problem A: the linker previously reported eddy counts but
    never persisted the eddy notes to disk. The user-visible vault stayed
    empty even when ``1 link(s)`` was reported.
    """
    vault = tmp_path / "vault"
    _embedded_eddy_fragments(vault)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )

    eddy_dir = vault / "03-Eddies"
    # At least one eddy file landed on disk.
    md_files = [p for p in eddy_dir.rglob("*.md") if p.name != ".gitkeep"]
    assert md_files, (
        f"Expected eddies linker to write at least one .md file under "
        f"{eddy_dir}, found none. Summary: {summary}"
    )
    # The reported count matches the number of files written.
    assert summary.eddies_written == len(md_files)
    assert summary.eddies_detected == len(md_files)


def test_run_link_eddies_updates_member_fragment_frontmatter(tmp_path: Path) -> None:
    """Member fragments have the eddy's wikilink in their ``eddies:`` frontmatter.

    Without this the eddy file is unreachable from the fragments that
    compose it — operators see a stub note but the constituent fragments
    do not point at the eddy from their YAML headers.
    """
    import frontmatter

    vault = tmp_path / "vault"
    seeded = _embedded_eddy_fragments(vault)

    run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )

    # Read the eddy file back to get the expected wikilink target.
    eddy_dir = vault / "03-Eddies"
    eddy_files = [p for p in eddy_dir.rglob("*.md") if p.name != ".gitkeep"]
    assert eddy_files, "eddies linker must materialize at least one eddy file"
    eddy_post = frontmatter.load(str(eddy_files[0]))
    eddy_title = eddy_post.get("title")
    assert isinstance(eddy_title, str) and eddy_title

    expected_link = f"[[{eddy_title}]]"

    # Every seeded fragment is a cluster member, so every fragment file
    # should now reference the eddy by wikilink.
    fragments_dir = vault / "01-Fragments" / "Notes"
    seen_links = 0
    for frag in seeded:
        path = fragments_dir / f"{frag.id}.md"
        assert path.exists(), f"Seeded fragment {frag.id} should still exist on disk"
        post = frontmatter.load(str(path))
        eddies_field = post.get("eddies") or []
        if expected_link in eddies_field:
            seen_links += 1
    assert seen_links == len(seeded), (
        f"Expected every member fragment to reference the eddy "
        f"{expected_link!r}; only {seen_links}/{len(seeded)} did."
    )


def test_run_link_eddies_summary_reports_zero_when_no_clusters(
    tmp_path: Path,
) -> None:
    """A sparse vault produces a summary with zero detected, zero written.

    Pins the symmetric "nothing happened" contract: when the linker
    finds no eddies the user-visible vault is unchanged and the counters
    both read zero.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-noeddy00000",
        title="Lonely note",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )

    assert summary.eddies_detected == 0
    assert summary.eddies_written == 0
    eddy_dir = vault / "03-Eddies"
    if eddy_dir.exists():
        assert [p for p in eddy_dir.rglob("*.md") if p.name != ".gitkeep"] == []


def test_run_link_embeddings_summary_exposes_edge_count(tmp_path: Path) -> None:
    """The embeddings summary exposes a ``similarity_edges`` count.

    Issue #338 Problem B: the previous output called pairwise similarity
    edges "link(s)", implying a per-fragment frontmatter side effect
    that never happened. The structured summary now exposes a
    method-specific field so the CLI can phrase the side effect
    accurately.
    """
    vault = tmp_path / "vault"
    fragment = Fragment(
        id="frag-edges00000",
        title="Edge fragment",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=fragment, body="body")

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )

    # The new field always exists for the embeddings method.
    assert summary.similarity_edges == summary.link_count


def test_run_link_eddies_persists_even_when_writer_dispatch_changes(
    tmp_path: Path,
) -> None:
    """The materialization path delegates through ``VaultWriter.write_eddy``.

    Pins the architectural decision (inline materialization, not deferral
    to ``creek compile``) so a future refactor cannot silently regress by
    swapping the writer for a no-op.
    """
    from unittest.mock import patch

    vault = tmp_path / "vault"
    _embedded_eddy_fragments(vault)

    with patch(
        "creek.link.link_engine.VaultWriter.write_eddy",
        autospec=True,
    ) as mock_write:
        mock_write.side_effect = lambda self, eddy: (
            self.vault_path / "03-Eddies" / f"{eddy.id}.md"
        )
        run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="eddies",
            rebuild=False,
        )

    assert mock_write.called, (
        "The eddies linker must persist detected eddies via VaultWriter.write_eddy"
    )


def test_run_link_eddies_write_oserror_does_not_crash(tmp_path: Path) -> None:
    """An ``OSError`` while persisting an eddy is logged and skipped.

    Disk full, read-only volume, or permission errors must not propagate
    — the rest of the linker has nothing to roll back and a single bad
    write should not block the entire run.
    """
    from unittest.mock import patch

    vault = tmp_path / "vault"
    _embedded_eddy_fragments(vault)

    with patch(
        "creek.link.link_engine.VaultWriter.write_eddy",
        side_effect=OSError("disk full"),
    ):
        summary = run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="eddies",
            rebuild=False,
        )

    # Detected count survives but written count is zero because every
    # write failed.
    assert summary.eddies_detected >= 1
    assert summary.eddies_written == 0


def test_run_link_eddies_skips_missing_vault_scaffold(tmp_path: Path) -> None:
    """A vault missing ``00-Creek-Meta`` does not crash the eddy materialiser.

    The link engine is invoked against whatever path the operator
    provides; a half-set-up vault should produce a clean warning rather
    than a ``FileNotFoundError`` traceback.
    """
    from unittest.mock import patch

    vault = tmp_path / "vault"
    _embedded_eddy_fragments(vault)

    with patch(
        "creek.link.link_engine.VaultWriter",
        side_effect=FileNotFoundError("missing dir"),
    ):
        summary = run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="eddies",
            rebuild=False,
        )

    # Detection still ran; persistence was skipped wholesale.
    assert summary.eddies_detected >= 1
    assert summary.eddies_written == 0


def test_run_link_eddies_fragment_write_oserror_is_logged(tmp_path: Path) -> None:
    """A failed fragment rewrite is counted as not-updated; no crash."""
    from pathlib import Path as _Path
    from unittest.mock import patch

    vault = tmp_path / "vault"
    _embedded_eddy_fragments(vault)

    # Force every fragment rewrite to fail. write_eddy is left intact so
    # the eddy file itself still lands.
    real_write_text = _Path.write_text

    def _selective_write(self: _Path, *args: object, **kwargs: object) -> int:
        if self.suffix == ".md" and self.parent.name == "Notes":
            raise OSError("read-only volume")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(_Path, "write_text", _selective_write):
        summary = run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="eddies",
            rebuild=False,
        )

    # Eddy file landed (write_eddy uses os.open, not write_text) but no
    # fragment frontmatter was rewritten.
    assert summary.eddies_written >= 1
    assert summary.member_fragments_updated == 0
