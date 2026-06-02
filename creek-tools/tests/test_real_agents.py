"""Tests for the real Graph + Retrieval specialist agents (FEAT-041 #463).

The Graph agent walks a bounded backlink graph; the Retrieval agent ranks
fragments by semantic similarity (embeddings are auto-mocked deterministically
in conftest). Both return structured, provenance-tracked evidence, carry
other-author attribution, and degrade gracefully on a thin/offline vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from creek.author.agents import GraphSpecialist, RetrievalSpecialist
from creek.author.models import EvidenceBundle
from creek.link.embeddings import EmbeddingModelUnavailableError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any leaked ``CREEK_CONFIG`` so tests use vault discovery/defaults."""
    monkeypatch.delenv("CREEK_CONFIG", raising=False)


_FM = (
    '---\ntype: fragment\nid: {id}\ntitle: "{title}"\n'
    "source:\n  platform: {platform}\n  author: {author}\n{extra}---\n{body}\n"
)


def _write(
    vault: Path,
    subdir: str,
    frag_id: str,
    title: str,
    *,
    body: str = "body",
    author: str = "self",
    author_slug: str | None = None,
    platform: str = "journal",
) -> None:
    """Write a minimal fragment markdown file under *subdir*."""
    folder = vault / subdir
    folder.mkdir(parents=True, exist_ok=True)
    extra = f"  author_slug: {author_slug}\n" if author_slug else ""
    (folder / f"{frag_id}.md").write_text(
        _FM.format(
            id=frag_id,
            title=title,
            platform=platform,
            author=author,
            extra=extra,
            body=body,
        ),
        encoding="utf-8",
    )


def test_retrieval_returns_cited_claims(tmp_path: Path) -> None:
    """Retrieval returns claims traced to real fragment ids."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Pluralism and F6")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Agency and F1")

    bundle = RetrievalSpecialist().gather("What is F6 Pluralism?", tmp_path)

    assert bundle.claims
    ids = {fid for claim in bundle.claims for fid in claim.source_fragments}
    assert ids <= {"frag-a", "frag-b"}
    assert all(claim.source_fragments for claim in bundle.claims)


def test_retrieval_respects_top_k(tmp_path: Path) -> None:
    """Retrieval surfaces at most ``retrieval_top_k`` (default 5) claims."""
    for i in range(8):
        _write(tmp_path, "01-Fragments/Notes", f"frag-{i}", f"Note {i}")

    bundle = RetrievalSpecialist().gather("note", tmp_path)

    assert 0 < len(bundle.claims) <= 5


def test_retrieval_is_deterministic(tmp_path: Path) -> None:
    """Two identical retrieval calls return the same bundle."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Beta")

    first = RetrievalSpecialist().gather("alpha", tmp_path)
    second = RetrievalSpecialist().gather("alpha", tmp_path)

    assert first == second


def test_retrieval_degrades_when_embeddings_unavailable(tmp_path: Path) -> None:
    """An unavailable embedding model yields an empty bundle, not a crash."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha")

    def _boom(self: object, text: str) -> list[float]:
        raise EmbeddingModelUnavailableError("offline")

    with patch("creek.link.embeddings.EmbeddingLinker.generate_embedding", _boom):
        bundle = RetrievalSpecialist().gather("q", tmp_path)

    assert bundle == EvidenceBundle()


def test_retrieval_carries_other_author_attribution(tmp_path: Path) -> None:
    """A claim drawn from 11-Other-Authors carries its author slug."""
    _write(
        tmp_path,
        "11-Other-Authors/naval-ravikant",
        "frag-naval",
        "On leverage",
        author="other",
        author_slug="naval-ravikant",
        platform="markdown",
    )

    bundle = RetrievalSpecialist().gather("leverage", tmp_path)

    assert bundle.claims
    assert bundle.claims[0].author_slug == "naval-ravikant"


def test_graph_walk_respects_depth_bound(tmp_path: Path) -> None:
    """The Graph walk never exceeds the configured depth bound (default 2)."""
    # A chain a -> b -> c -> d -> e; depth 2 from any seed reaches <= depth 2.
    chain = ["a", "b", "c", "d", "e"]
    for i, name in enumerate(chain):
        link = f"[[frag-{chain[i + 1]}]]" if i + 1 < len(chain) else ""
        _write(
            tmp_path, "01-Fragments/Notes", f"frag-{name}", f"Node {name}", body=link
        )

    bundle = GraphSpecialist().gather("Node a", tmp_path)

    assert bundle.walk_stats is not None
    assert bundle.walk_stats.max_depth <= 2
    assert bundle.claims  # at least the seed


def test_graph_walk_respects_breadth_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small breadth bound caps how many neighbours the walk expands."""
    hub_links = "".join(f"[[frag-leaf-{i}]]" for i in range(10))
    _write(tmp_path, "01-Fragments/Notes", "frag-hub", "Hub node", body=hub_links)
    for i in range(10):
        _write(tmp_path, "01-Fragments/Notes", f"frag-leaf-{i}", f"Leaf {i}")
    config = tmp_path / "00-Creek-Meta" / "creek_config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "author:\n  graph_breadth_bound: 3\n  graph_depth_bound: 1\n",
        encoding="utf-8",
    )
    # Pin the config explicitly so a leaked CREEK_CONFIG can't override it.
    monkeypatch.setenv("CREEK_CONFIG", str(config))

    bundle = GraphSpecialist().gather("Hub node", tmp_path)

    # seed + at most 3 neighbours at depth 1.
    assert bundle.walk_stats is not None
    assert bundle.walk_stats.fragments_visited <= 1 + 3


def test_graph_empty_vault_returns_empty_with_stats(tmp_path: Path) -> None:
    """An empty vault yields an empty bundle carrying zeroed walk stats."""
    bundle = GraphSpecialist().gather("q", tmp_path)

    assert bundle.claims == []
    assert bundle.walk_stats is not None
    assert bundle.walk_stats.fragments_visited == 0
