"""Tests for the vault-driven linking engine."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

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
    from collections.abc import Callable
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

    def _selective_write(
        self: _Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self.suffix == ".md" and self.parent.name == "Notes":
            raise OSError("read-only volume")
        return real_write_text(
            self,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

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


def test_run_link_eddies_preserves_extra_frontmatter_and_body(
    tmp_path: Path,
) -> None:
    """Rewriting a fragment's ``eddies:`` list does not drop other keys.

    The fragment-rewrite path re-reads the raw YAML so operator-applied
    keys (``classification_method``, custom tags) and the markdown body
    survive verbatim. Without this guarantee a future refactor that
    dropped the ``raw.copy()`` step would silently corrupt user vaults.
    """
    from datetime import datetime, timedelta

    import frontmatter

    vault = tmp_path / "vault"
    base = datetime(2026, 4, 1)
    day_offsets = [600, 45, 400, 120, 500, 15]
    fragments: list[Fragment] = []
    custom_body = "Body paragraph one.\n\nBody paragraph two with [[link]]."
    for i in range(6):
        frag = Fragment(
            id=f"frag-extras-{i:03d}",
            title="Ceramics studio practice",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            created=base - timedelta(days=day_offsets[i]),
        )
        _write_fragment(
            vault=vault,
            fragment=frag,
            body=custom_body,
            method="manual",
            extras={"operator_tags": ["studio", "ritual"], "custom_key": "preserved"},
        )
        fragments.append(frag)

    config = CreekConfig()
    linker = EmbeddingLinker(config=config.embeddings)
    vectors = {frag.id: [1.0, 0.0, 0.0] for frag in fragments}
    entries = linker.build_cache_entries(fragments, vectors)
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(entries, cache_path)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )
    assert summary.member_fragments_updated == len(fragments)

    fragments_dir = vault / "01-Fragments" / "Notes"
    for frag in fragments:
        post = frontmatter.load(str(fragments_dir / f"{frag.id}.md"))
        assert post.get("classification_method") == "manual"
        assert post.get("operator_tags") == ["studio", "ritual"]
        assert post.get("custom_key") == "preserved"
        assert post.content == custom_body
        eddies_field = post.get("eddies") or []
        assert any(
            isinstance(entry, str) and entry.startswith("[[") for entry in eddies_field
        ), f"Fragment {frag.id} lost its eddy wiki-link"


# ----- Issue #880: cluster-limit and segmentation plumbing -------------------


def test_run_link_eddies_reports_largest_cluster(tmp_path: Path) -> None:
    """``LinkSummary`` carries the largest detected cluster size.

    Issue #880: ``docs/linking.md`` has advertised a ``largest: N fragments``
    line since the linker shipped, and no code ever produced the number —
    which is exactly the statistic that would have made the 30,795-fragment
    mega-eddy visible without opening the vault.
    """
    vault = tmp_path / "vault"
    fragments = _embedded_eddy_fragments(vault, count=8)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )

    assert summary.eddies_detected == 1
    assert summary.largest_cluster_fragments == len(fragments)
    assert summary.clusters_split == 0
    assert summary.oversized_discarded == 0


def test_run_link_honours_configured_cluster_ceiling(tmp_path: Path) -> None:
    """A configured ceiling reaches the detector and bounds what is emitted.

    Issue #880: ``run_link`` used to construct ``EddyDetector(embeddings=...)``
    with embeddings only, so every threshold fell back to a module constant
    and no configured value could change the outcome. Here the ceiling is set
    below the single dense cluster, which is eight identical vectors and
    therefore cannot be split by any epsilon — so the guardrail's
    discard-to-noise path fires and is reported.
    """
    vault = tmp_path / "vault"
    fragments = _embedded_eddy_fragments(vault, count=8)
    config = CreekConfig()
    config.linking.cluster_size_ceiling = 1
    config.linking.cluster_max_fraction = 0.5

    summary = run_link(
        vault_path=vault,
        config=config,
        method="eddies",
        rebuild=False,
    )

    assert summary.eddies_detected == 0
    assert summary.clusters_split == 1
    assert summary.oversized_discarded == len(fragments)
    assert summary.largest_cluster_fragments == 0


def _embedded_message_stream(vault: Path) -> list[Fragment]:
    """Seed *vault* with two Discord channels ten days apart, one dense blob.

    Every fragment carries the same title and an identical embedding, so
    without segmentation DBSCAN sees exactly one cluster spanning both
    channels — the shape of the #880 mega-eddy in miniature.

    Args:
        vault: Vault root.

    Returns:
        The created fragments in creation order.
    """
    from datetime import datetime, timedelta

    base = datetime(2026, 4, 1, 9, 0)
    fragments: list[Fragment] = []
    for channel_index, channel in enumerate(("stagecraft", "starlight")):
        for message in range(5):
            authored = base + timedelta(days=10 * channel_index, hours=message)
            frag = Fragment(
                id=f"frag-880-{channel_index}-{message:02d}",
                title="messages",
                source=FragmentSource(
                    platform=SourcePlatform.DISCORD,
                    channel=channel,
                ),
                created=authored,
                authored_at=authored,
            )
            _write_fragment(vault=vault, fragment=frag, body="body")
            fragments.append(frag)

    config = CreekConfig()
    linker = EmbeddingLinker(config=config.embeddings)
    vectors = {frag.id: [1.0, 0.0, 0.0] for frag in fragments}
    cache_path = embeddings_cache_path(vault)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(linker.build_cache_entries(fragments, vectors), cache_path)
    return fragments


def test_run_link_honours_configured_stream_platforms(tmp_path: Path) -> None:
    """The configured stream-platform list reaches the detector, both ways.

    Issue #880: with the default list, two Discord channels ten days apart
    are two conversation episodes and therefore two eddies. Emptying the
    list — the documented way to disable segmentation — collapses them back
    into the single ten-fragment blob the bug produced. Neither outcome is
    reachable unless ``run_link`` actually passes the configuration to
    ``EddyDetector``, which it previously did not.
    """
    vault = tmp_path / "vault"
    fragments = _embedded_message_stream(vault)

    segmented = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )
    assert segmented.eddies_detected == 2
    assert segmented.largest_cluster_fragments == len(fragments) // 2

    unsegmented_config = CreekConfig()
    unsegmented_config.linking.stream_platforms = []
    unsegmented = run_link(
        vault_path=vault,
        config=unsegmented_config,
        method="eddies",
        rebuild=False,
    )
    assert unsegmented.eddies_detected == 1
    assert unsegmented.largest_cluster_fragments == len(fragments)


def test_embedding_pass_splits_computed_from_reused(tmp_path: Path) -> None:
    """``EmbeddingPass`` must distinguish fresh vectors from cache hits.

    The distinction is what ``creek process`` bills ``local_model_processed``
    against (#1303). Before it existed, the only number available to a
    caller was ``len(fragments)``, which counted every cache hit as
    local-model work the run never actually did.
    """
    from creek.link.link_engine import _load_or_compute_embeddings

    vault = tmp_path / "vault"
    first = Fragment(
        id="frag-embedpass01",
        title="First embedded fragment",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(vault=vault, fragment=first, body="body")
    cache_path = embeddings_cache_path(vault)

    cold = _load_or_compute_embeddings(
        fragments=[first],
        config=CreekConfig(),
        cache_path=cache_path,
    )
    assert cold.computed == 1
    assert cold.reused == 0
    assert set(cold.vectors) == {first.id}

    second = Fragment(
        id="frag-embedpass02",
        title="Second embedded fragment",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    warm = _load_or_compute_embeddings(
        fragments=[first, second],
        config=CreekConfig(),
        cache_path=cache_path,
    )
    assert warm.computed == 1, "only the new fragment should have been embedded"
    assert warm.reused == 1
    assert set(warm.vectors) == {first.id, second.id}


def test_run_link_reports_fragments_embedded_per_method(tmp_path: Path) -> None:
    """Every method's summary reports the vectors that pass actually computed.

    ``eddies`` and ``threads`` both load embeddings, so the first of the
    two to run pays for them and the second reports zero. ``creek process``
    relies on exactly that to avoid double-counting Pass-2 work (#1303).
    """
    vault = tmp_path / "vault"
    for index in range(3):
        _write_fragment(
            vault=vault,
            fragment=Fragment(
                id=f"frag-embedcount{index}",
                title=f"Counted fragment {index}",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body="body",
        )

    first = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="eddies",
        rebuild=False,
    )
    second = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="threads",
        rebuild=False,
    )
    assert first.fragments_embedded == 3
    assert second.fragments_embedded == 0


# ----- Issue #1337: report what actually reached the parquet -----------------


def _seeded_embeddings_vault(vault: Path, *, count: int) -> list[Fragment]:
    """Seed *vault* with *count* fragments and no embeddings cache.

    Deliberately does **not** pre-write the parquet: these tests are about
    what a run persists, so the artifact must be created by the run under
    test and by nothing else.

    Args:
        vault: Vault root.
        count: Number of fragment files to write.

    Returns:
        The created fragments, in creation order.
    """
    fragments: list[Fragment] = []
    for index in range(count):
        frag = Fragment(
            id=f"frag-1337persist{index:02d}",
            title=f"Persisted vector {index}",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        )
        _write_fragment(vault=vault, fragment=frag, body=f"Body {index}.")
        fragments.append(frag)
    return fragments


def test_run_link_embeddings_reports_vectors_actually_persisted(
    tmp_path: Path,
) -> None:
    """``vectors_persisted`` equals the rows the parquet now holds.

    Issue #1337: the CLI claimed "vectors cached in
    00-Creek-Meta/embeddings.parquet" from a hardcoded clause, with nothing
    in the summary to contradict it. The count is now structured, and it is
    pinned here against the artifact — ``pq.read_table(...).num_rows`` —
    rather than a literal, so the fixture and the assertion cannot drift.
    """
    import pyarrow.parquet as pq

    vault = tmp_path / "vault"
    fragments = _seeded_embeddings_vault(vault, count=3)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )

    cache_path = embeddings_cache_path(vault)
    assert cache_path.exists()
    assert summary.fragment_count == len(fragments)
    assert summary.vectors_persisted == summary.fragment_count
    assert summary.vectors_persisted == pq.read_table(cache_path).num_rows


def test_run_link_embeddings_empty_vault_persists_no_vectors(
    tmp_path: Path,
) -> None:
    """The fragmentless early return must carry ``vectors_persisted == 0``.

    Issue #1337: ``run_link`` returns a bare ``LinkSummary`` for an empty
    vault, so the honest zero has to come from the field's default rather
    than from a special case at the return site. If a later refactor gives
    the field a non-zero default, or populates it unconditionally, this
    fails — and so does the paired disk assertion, because no parquet is
    written on this path at all.
    """
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )

    assert summary.fragment_count == 0
    assert summary.vectors_persisted == 0
    assert not embeddings_cache_path(vault).exists()


def test_run_link_embeddings_save_failure_reports_zero_persisted(
    tmp_path: Path,
) -> None:
    """A swallowed ``OSError`` from ``save_cache`` must report zero persisted.

    Issue #1337: ``_persist_cache`` degrades gracefully on a full disk or a
    read-only volume, which is the right behaviour and is preserved (the
    run still completes). What was wrong is that the operator was told the
    vectors had been cached anyway. Two fragments are embedded and zero
    persisted here on purpose: an implementation that aliased
    ``vectors_persisted`` to ``fragments_embedded`` would pass a
    single-fragment version of this test's disk check but dies on the
    counter comparison.
    """
    from unittest.mock import patch

    vault = tmp_path / "vault"
    fragments = _seeded_embeddings_vault(vault, count=2)

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

    assert summary.fragments_embedded == len(fragments)
    assert summary.vectors_persisted == 0
    assert not embeddings_cache_path(vault).exists()


def test_embedding_pass_requires_an_explicit_persisted_count() -> None:
    """``EmbeddingPass.persisted`` has no default; every call site must state it.

    Issue #1337: a default would let a future pass under-report silently —
    the whole point of the field is that the number is measured, not
    assumed. The dataclass-field check kills the mutant that swaps the
    missing default for a ``default_factory`` returning zero, which would
    keep the constructor call from raising while restoring the hazard.
    """
    from dataclasses import MISSING, fields

    from creek.link import link_engine

    # Widened deliberately: a precisely-typed call to a constructor with a
    # required argument omitted is a type error, and the point here is the
    # runtime contract, not the annotation.
    constructor: Callable[..., object] = link_engine.EmbeddingPass
    with pytest.raises(TypeError):
        constructor(vectors={}, computed=0, reused=0)

    persisted_field = next(
        field
        for field in fields(link_engine.EmbeddingPass)
        if field.name == "persisted"
    )
    assert persisted_field.default is MISSING
    assert persisted_field.default_factory is MISSING

    complete = link_engine.EmbeddingPass(
        vectors={},
        computed=0,
        reused=0,
        persisted=0,
    )
    assert complete.persisted == 0


class _FlatEncoder:
    """Deterministic stand-in for the sentence-transformer model.

    Row ``i`` is ``[1.0, 0.001 * i]``, so every pair clears the default
    0.75 cosine threshold and the run produces a known-non-zero edge count.
    The shared conftest mock returns random 384-dimension vectors, which
    are near-orthogonal and typically yield zero edges — useless for a test
    whose whole subject is what happens per edge.
    """

    def encode(self, texts: str | list[str], **_kwargs: object) -> list[list[float]]:
        """Return one deterministic vector per entry in *texts*.

        Args:
            texts: A single text, or the batch the linker passes.
            **_kwargs: ``show_progress_bar`` / ``batch_size``; ignored.

        Returns:
            One ``[1.0, 0.001 * i]`` row per input text.
        """
        batch = [texts] if isinstance(texts, str) else list(texts)
        return [[1.0, 0.001 * index] for index, _text in enumerate(batch)]


def test_run_link_embeddings_counts_edges_without_building_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek link --method embeddings`` must not allocate a model per edge.

    Issue #1337: ``_run_embeddings`` called ``find_resonances`` and took
    ``len`` of the result, so every above-threshold pair became a Pydantic
    :class:`~creek.link.embeddings.Resonance` that was counted and dropped.
    Measured at ~1KB of heap each, which at the documented 35k-fragment
    scale is tens of gigabytes — the run dies before it can report anything.

    ``tests/test_embeddings.py`` pins that ``count_resonances`` itself
    allocates nothing. This pins the *call site*, which is the part a
    revert would touch: swapping ``count_resonances`` back for
    ``find_resonances`` here leaves every count in this suite correct and
    reinstates the whole defect, so nothing else in the codebase notices.
    """
    from creek.link import embeddings as embeddings_module

    vault = tmp_path / "vault"
    _seeded_embeddings_vault(vault, count=6)

    constructed = 0
    real_resonance = embeddings_module.Resonance

    def _spy(**kwargs: Any) -> object:
        """Count each Resonance the link run builds."""
        nonlocal constructed
        constructed += 1
        return real_resonance(**kwargs)

    monkeypatch.setattr(embeddings_module, "Resonance", _spy)
    monkeypatch.setattr(
        embeddings_module.EmbeddingLinker,
        "load_model",
        lambda _self: _FlatEncoder(),
    )

    summary = run_link(
        vault_path=vault,
        config=CreekConfig(),
        method="embeddings",
        rebuild=False,
    )

    # Vacuity guard: 6 mutually-similar fragments must yield C(6,2) edges,
    # so "zero objects built" is a real property and not an empty traversal.
    assert summary.similarity_edges == 15
    assert constructed == 0, (
        f"the link run built {constructed} Resonance object(s) to count "
        f"{summary.similarity_edges} edge(s); counting must allocate none"
    )


class TestEmbeddingsCacheParquetFormat:
    """The on-disk parquet crossing a pyarrow major would break (#1001).

    The embeddings cache is the only file this project keeps in a
    third-party binary format, and ``pyarrow`` was declared ``>=17.0.0``
    with no ceiling — so a routine relock could carry the writer across
    a parquet major while caches written under the old one were still on
    disk. Bounding the specifier stops the *unreviewed* crossing; these
    tests are what make a reviewed one safe, by pinning the format the
    two halves agree on rather than only that a round trip returns
    something.

    All six ``pyarrow`` call sites in ``creek.link.embeddings`` are
    covered: the writer's ``pa.table`` / ``pa.array`` and
    ``pq.write_table`` (``save_cache``), the reader's ``pq.read_table``
    (``load_cache``), and the purge path's ``pq.read_table``,
    ``pq.write_table`` and ``pq.read_metadata``. The purge *rewrite* is
    the one an upgrade is most likely to break silently and the one the
    issue's own call-site list omitted.
    """

    @staticmethod
    def _entries() -> dict[str, Any]:
        """Return three cache rows with distinguishable vectors.

        Returns:
            Mapping of fragment ID to :class:`CachedEmbedding`, ordered
            so a purge can drop the middle row and leave neighbours to
            assert against.
        """
        from creek.link.embeddings import CachedEmbedding

        computed_at = datetime(2026, 8, 21, 12, 30, 45, tzinfo=UTC)
        return {
            f"frag-{index}": CachedEmbedding(
                fragment_id=f"frag-{index}",
                content_hash=f"hash-{index}",
                model_name="all-MiniLM-L6-v2",
                vector=[0.5 * index, -0.25 * index, 0.125],
                computed_at=computed_at,
            )
            for index in range(3)
        }

    def test_saved_cache_declares_the_column_types_the_reader_expects(
        self,
        tmp_path: Path,
    ) -> None:
        """``save_cache`` writes the exact arrow schema, not merely a table.

        A parquet major that widened ``float32`` to ``float64`` or
        dropped the timestamp's timezone would still round-trip through
        ``to_pylist()`` while changing every persisted vector's width
        and every timestamp's meaning. Asserting the schema is what
        makes that visible.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        cache_path = tmp_path / "embeddings.parquet"
        EmbeddingLinker(config=config).save_cache(self._entries(), cache_path)

        schema = pq.read_schema(cache_path)
        assert schema.field("fragment_id").type == pa.string()
        assert schema.field("content_hash").type == pa.string()
        assert schema.field("model_name").type == pa.string()
        assert schema.field("embedding").type == pa.list_(pa.float32())
        assert schema.field("computed_at").type == pa.timestamp("us", tz="UTC")

    def test_saved_cache_is_snappy_compressed(self, tmp_path: Path) -> None:
        """The writer's ``compression="snappy"`` reaches the file footer.

        Snappy is what keeps a large vault's cache readable without a
        codec the reader may not have; a default-compression regression
        is invisible to every behavioural assertion except this one.
        """
        import pyarrow.parquet as pq

        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        cache_path = tmp_path / "embeddings.parquet"
        EmbeddingLinker(config=config).save_cache(self._entries(), cache_path)

        metadata = pq.read_metadata(cache_path)
        codecs = {
            metadata.row_group(0).column(index).compression
            for index in range(metadata.num_columns)
        }
        assert codecs == {"SNAPPY"}, f"cache columns compressed with {codecs}"

    def test_round_trip_preserves_every_field_exactly(self, tmp_path: Path) -> None:
        """``load_cache`` returns what ``save_cache`` was given.

        Exact values, not shapes: a float32 column narrows the vector,
        so the assertion is written against the narrowed value the
        reader must reproduce rather than against the input floats.
        """
        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        linker = EmbeddingLinker(config=config)
        cache_path = tmp_path / "embeddings.parquet"
        entries = self._entries()
        linker.save_cache(entries, cache_path)

        loaded = linker.load_cache(cache_path)

        assert set(loaded) == set(entries)
        for frag_id, original in entries.items():
            row = loaded[frag_id]
            assert row.fragment_id == original.fragment_id
            assert row.content_hash == original.content_hash
            assert row.model_name == original.model_name
            assert row.computed_at == original.computed_at
            assert row.vector == pytest.approx(original.vector, abs=1e-6)

    def test_purge_rewrites_a_cache_that_still_round_trips(
        self,
        tmp_path: Path,
    ) -> None:
        """The purge rewrite keeps the file readable by ``load_cache``.

        ``purge_fragment_ids_from_cache`` reads the table, filters it and
        writes it back — a second, independent writer over the same
        format. A major that changed ``Table.filter`` or the rewrite's
        defaults would corrupt a vault's cache during a
        right-to-be-forgotten erasure, which is the worst possible time
        to find out.
        """
        from creek.link.embeddings import purge_fragment_ids_from_cache

        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        linker = EmbeddingLinker(config=config)
        cache_path = tmp_path / "embeddings.parquet"
        entries = self._entries()
        linker.save_cache(entries, cache_path)

        removed = purge_fragment_ids_from_cache(cache_path, ["frag-1"])

        assert removed == 1
        survivors = linker.load_cache(cache_path)
        assert set(survivors) == {"frag-0", "frag-2"}
        assert survivors["frag-2"].vector == pytest.approx(
            entries["frag-2"].vector,
            abs=1e-6,
        )

    def test_footer_row_count_matches_the_rows_written(self, tmp_path: Path) -> None:
        """``delete_embeddings_cache`` counts rows from the parquet footer.

        It reports how much was erased, so a footer read that stopped
        agreeing with the writer would under- or over-claim an erasure
        in the audit record while the deletion itself still succeeded.
        """
        from creek.link.embeddings import delete_embeddings_cache

        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        cache_path = tmp_path / "embeddings.parquet"
        EmbeddingLinker(config=config).save_cache(self._entries(), cache_path)

        assert delete_embeddings_cache(cache_path, dry_run=True) == 3
        assert cache_path.exists()
        assert delete_embeddings_cache(cache_path) == 3
        assert not cache_path.exists()
