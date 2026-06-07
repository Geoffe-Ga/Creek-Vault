"""Tests for the real Graph + Retrieval specialist agents (FEAT-041 #463).

The Graph agent walks a bounded backlink graph; the Retrieval agent ranks
fragments by semantic similarity (embeddings are auto-mocked deterministically
in conftest). Both return structured, provenance-tracked evidence, carry
other-author attribution, and degrade gracefully on a thin/offline vault.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from creek.author.agents import (
    GraphSpecialist,
    OntologySpecialist,
    RetrievalSpecialist,
    _build_link_graph,
    _load_config,
    _load_corpus,
)
from creek.author.models import EvidenceBundle
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    EmbeddingModelUnavailableError,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
from creek.models import Frequency, Phase, VoiceRegister

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


def _write_classified(
    vault: Path,
    frag_id: str,
    title: str,
    *,
    body: str,
    frontmatter: str = "",
) -> None:
    """Write a fragment carrying optional pre-classified frontmatter blocks."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"source:\n  platform: journal\n  author: self\n{frontmatter}---\n{body}\n",
        encoding="utf-8",
    )


# Bodies dense with rule-classifier signal keywords (creek/classify/rules.py)
# so deterministic keyword classification fires without any LLM.
_F6_BODY = "community empathy harmony inclusion caring sharing equality consensus"
_F3_BODY = "power dominance control conquest force aggression warrior rebellion"
_RISING_BODY = "emerging building growing momentum ascending intensifying gathering"
_DIMINISHING_BODY = "diminishing declining waning subsiding settling calming quieting"
# Dense with analytical voice-register signals (VOICE_REGISTER_SIGNALS) so the
# rule classifier resolves the fragment's register to ANALYTICAL deterministically.
_ANALYTICAL_BODY = (
    "analyze examine consider therefore evidence hypothesis framework "
    "systematically data observe"
)


def test_ontology_surfaces_dominant_voice_register(tmp_path: Path) -> None:
    """The Ontology agent aggregates a weighted, non-empty voice register."""
    _write_classified(
        tmp_path, "frag-an", "Analytical examination", body=_ANALYTICAL_BODY
    )

    bundle = OntologySpecialist().gather("analysis", tmp_path)

    assert bundle.ontology is not None
    assert bundle.ontology.voice_registers  # a register was surfaced
    assert bundle.ontology.voice_registers[0].value is VoiceRegister.ANALYTICAL


def test_ontology_returns_canonical_taxonomy(tmp_path: Path) -> None:
    """The Ontology agent emits only canonical F1-F10 frequencies and phases."""
    _write_classified(tmp_path, "frag-f6", "Community and belonging", body=_F6_BODY)
    _write_classified(tmp_path, "frag-rise", "Emerging momentum", body=_RISING_BODY)

    bundle = OntologySpecialist().gather("community", tmp_path)

    assert bundle.ontology is not None
    canonical_freqs = {f"F{i}" for i in range(1, 11)}
    assert bundle.ontology.frequencies  # F6 detected
    for entry in bundle.ontology.frequencies:
        assert isinstance(entry.value, Frequency)
        assert entry.value.value in canonical_freqs
    assert any(e.value is Frequency.F6 for e in bundle.ontology.frequencies)
    canonical_phases = {p for p in Phase if p is not Phase.UNCLASSIFIED}
    for entry in bundle.ontology.phases:
        assert entry.value in canonical_phases
    assert any(e.value is Phase.RISING for e in bundle.ontology.phases)
    # Claims are grounded in the scanned fragments.
    assert bundle.claims
    assert all(claim.source_fragments for claim in bundle.claims)
    cited = {fid for claim in bundle.claims for fid in claim.source_fragments}
    assert cited == {"frag-f6", "frag-rise"}


def test_ontology_surfaces_dosage_paradox_without_resolving(tmp_path: Path) -> None:
    """Same frequency marked medicine vs toxic surfaces a paradox, both kept."""
    _write_classified(
        tmp_path,
        "frag-med",
        "Power as medicine",
        body=_F3_BODY,
        frontmatter="frequency:\n  primary: F3\nwavelength:\n  dosage: medicine\n",
    )
    _write_classified(
        tmp_path,
        "frag-tox",
        "Power as toxic",
        body=_F3_BODY,
        frontmatter="frequency:\n  primary: F3\nwavelength:\n  dosage: toxic\n",
    )

    bundle = OntologySpecialist().gather("power", tmp_path)

    assert bundle.ontology is not None
    dosage_paradoxes = [p for p in bundle.ontology.paradoxes if p.kind == "dosage"]
    assert dosage_paradoxes, "dosage contradiction must be surfaced"
    paradox = dosage_paradoxes[0]
    assert set(paradox.fragment_ids) == {"frag-med", "frag-tox"}
    # The contradiction is preserved, not flattened to one dosage.
    dosage_values = {e.value.value for e in bundle.ontology.dosages}
    assert {"medicine", "toxic"} <= dosage_values


def test_ontology_surfaces_phase_paradox_without_resolving(tmp_path: Path) -> None:
    """Opposite phases across fragments surface a phase paradox, both kept."""
    _write_classified(tmp_path, "frag-rise", "Rising tide", body=_RISING_BODY)
    _write_classified(tmp_path, "frag-fall", "Falling away", body=_DIMINISHING_BODY)

    bundle = OntologySpecialist().gather("tide", tmp_path)

    assert bundle.ontology is not None
    phase_paradoxes = [p for p in bundle.ontology.paradoxes if p.kind == "phase"]
    assert phase_paradoxes, "phase contradiction must be surfaced"
    assert set(phase_paradoxes[0].fragment_ids) == {"frag-rise", "frag-fall"}
    phase_values = {e.value for e in bundle.ontology.phases}
    assert {Phase.RISING, Phase.DIMINISHING} <= phase_values


def test_ontology_empty_vault_returns_empty_bundle(tmp_path: Path) -> None:
    """An empty corpus yields an empty bundle with no ontology and no crash."""
    bundle = OntologySpecialist().gather("q", tmp_path)

    assert bundle == EvidenceBundle()
    assert bundle.ontology is None
    assert bundle.claims == []


def test_ontology_unclassified_corpus_still_grounds_claim(tmp_path: Path) -> None:
    """Even an all-unclassified corpus emits a fragment-grounded claim."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha note about q")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Beta note")

    bundle = OntologySpecialist().gather("q", tmp_path)

    assert bundle.ontology is not None
    assert bundle.claims
    cited = {fid for claim in bundle.claims for fid in claim.source_fragments}
    assert cited == {"frag-a", "frag-b"}


def test_retrieval_reuses_linker_across_gather_calls(
    tmp_path: Path, mock_sentence_transformer: MagicMock
) -> None:
    """One ``RetrievalSpecialist`` instance loads the model at most once.

    A long-lived specialist reused across two ``gather()`` calls must not
    re-instantiate the sentence-transformer — the per-instance lazy linker
    holds the loaded model, so the underlying loader fires exactly once.
    """
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Beta")

    specialist = RetrievalSpecialist()
    specialist.gather("alpha", tmp_path)
    specialist.gather("beta", tmp_path)

    assert mock_sentence_transformer.call_count == 1


def _seed_embeddings_cache(vault: Path, frag_ids: list[str]) -> None:
    """Persist a real parquet cache covering *frag_ids* with fresh hashes.

    These are UNIT tests, not integration: the vectors come from the autouse
    ``mock_sentence_transformer`` fixture (deterministic mock embeddings), so
    both this seeding step and the agent under test use the same mock model and
    never load the real sentence-transformer. That is why the cache vector for a
    fragment equals the vector the agent would compute live — and why no
    ``@pytest.mark.integration`` marker is needed.
    """
    corpus = _load_corpus(vault)
    by_id = {fragment.id: fragment for fragment, _ in corpus}
    config = _load_config(vault)
    linker = EmbeddingLinker(config.embeddings)
    entries = {
        fid: CachedEmbedding(
            fragment_id=fid,
            content_hash=content_hash_for_text(
                fragment_embedding_text(by_id[fid]),
            ),
            model_name=config.embeddings.model,
            vector=linker.generate_embedding(
                fragment_embedding_text(by_id[fid]),
            ),
            computed_at=datetime.now(tz=UTC),
        )
        for fid in frag_ids
    }
    path = embeddings_cache_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(entries, path)


def test_retrieval_cache_hit_avoids_re_embedding(tmp_path: Path) -> None:
    """A warm cache embeds only the query, never the cached fragments.

    With a persisted parquet cache whose content hashes match the current
    fragment text, ``gather`` reuses the cached vectors; the only
    ``generate_embedding`` call is for the query itself.
    """
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Beta")
    _seed_embeddings_cache(tmp_path, ["frag-a", "frag-b"])

    with patch(
        "creek.link.embeddings.EmbeddingLinker.generate_embedding",
        autospec=True,
        side_effect=EmbeddingLinker.generate_embedding,
    ) as spy:
        RetrievalSpecialist().gather("alpha", tmp_path)

    embedded_texts = [call.args[1] for call in spy.call_args_list]
    assert embedded_texts == ["alpha"]


def test_retrieval_loads_cache_once_across_gather_calls(tmp_path: Path) -> None:
    """The parquet cache is read once per instance + vault, not every gather()."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha")
    _seed_embeddings_cache(tmp_path, ["frag-a"])
    specialist = RetrievalSpecialist()

    with patch(
        "creek.link.embeddings.EmbeddingLinker.load_cache",
        autospec=True,
        side_effect=EmbeddingLinker.load_cache,
    ) as spy:
        specialist.gather("alpha", tmp_path)
        specialist.gather("beta", tmp_path)

    assert spy.call_count == 1


def test_retrieval_cache_is_pure_optimization(tmp_path: Path) -> None:
    """Warm-cache ranking equals cold ranking — the cache changes nothing.

    The cached vector for a fragment is the same deterministic vector the
    mock produces on the embed path, so ids and order must be identical with
    and without a cache file present.
    """
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Pluralism and F6")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Agency and F1")
    _write(tmp_path, "01-Fragments/Notes", "frag-c", "Leverage and scale")

    cold = RetrievalSpecialist().gather("What is F6 Pluralism?", tmp_path)
    _seed_embeddings_cache(tmp_path, ["frag-a", "frag-b", "frag-c"])
    warm = RetrievalSpecialist().gather("What is F6 Pluralism?", tmp_path)

    assert cold == warm


def test_retrieval_stale_cache_entry_is_recomputed(tmp_path: Path) -> None:
    """A cache entry whose hash no longer matches falls back to a live embed.

    Freshness is keyed on the SHA-256 of the current
    ``fragment_embedding_text``; a stale hash must not be trusted, so the
    fragment is re-embedded rather than served from the cache.
    """
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Alpha")
    corpus = _load_corpus(tmp_path)
    by_id = {fragment.id: fragment for fragment, _ in corpus}
    config = _load_config(tmp_path)
    linker = EmbeddingLinker(config.embeddings)
    stale = {
        "frag-a": CachedEmbedding(
            fragment_id="frag-a",
            content_hash=content_hash_for_text("a different, stale title"),
            model_name=config.embeddings.model,
            vector=linker.generate_embedding(
                fragment_embedding_text(by_id["frag-a"]),
            ),
            computed_at=datetime.now(tz=UTC),
        )
    }
    path = embeddings_cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    linker.save_cache(stale, path)

    with patch(
        "creek.link.embeddings.EmbeddingLinker.generate_embedding",
        autospec=True,
        side_effect=EmbeddingLinker.generate_embedding,
    ) as spy:
        RetrievalSpecialist().gather("alpha", tmp_path)

    embedded_texts = [call.args[1] for call in spy.call_args_list]
    # Exactly the query plus the one stale fragment, re-embedded live (computed
    # via fragment_embedding_text so the assertion survives a shape change and
    # still proves the stale cache entry was NOT served).
    assert embedded_texts == ["alpha", fragment_embedding_text(by_id["frag-a"])]


def test_build_link_graph_skips_ambiguous_title(tmp_path: Path) -> None:
    """A wikilink to a title shared by 2+ fragments resolves to NEITHER (#487).

    Silently picking one (the old last-wins behaviour) could link the wrong
    fragment, so an ambiguous title is dropped from the resolver. An exact-id
    wikilink still resolves even when the fragment's title is ambiguous, since
    ids are preferred over titles.
    """
    _write(tmp_path, "01-Fragments/Notes", "frag-dup-1", "Shared Title")
    _write(tmp_path, "01-Fragments/Notes", "frag-dup-2", "Shared Title")
    _write(
        tmp_path,
        "01-Fragments/Notes",
        "frag-title-link",
        "TitleLinker",
        body="[[Shared Title]]",
    )
    _write(
        tmp_path,
        "01-Fragments/Notes",
        "frag-id-link",
        "IdLinker",
        body="[[frag-dup-1]]",
    )

    corpus = sorted(_load_corpus(tmp_path), key=lambda rec: rec[0].id)
    graph = _build_link_graph(corpus)

    # Ambiguous title → no edge to either duplicate (no silent mis-link).
    assert "frag-dup-1" not in graph["frag-title-link"]
    assert "frag-dup-2" not in graph["frag-title-link"]
    # Exact id still resolves despite the shared, ambiguous title.
    assert "frag-dup-1" in graph["frag-id-link"]
