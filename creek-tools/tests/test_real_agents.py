"""Tests for the real Graph + Retrieval specialist agents (FEAT-041 #463).

The Graph agent walks a bounded backlink graph; the Retrieval agent ranks
fragments by semantic similarity (embeddings are auto-mocked deterministically
in conftest). Both return structured, provenance-tracked evidence, carry
other-author attribution, and degrade gracefully on a thin/offline vault.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
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
from tests.helpers import write_raw_fragment_file as _write

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any leaked ``CREEK_CONFIG`` so tests use vault discovery/defaults."""
    monkeypatch.delenv("CREEK_CONFIG", raising=False)


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
    """Write a fragment carrying optional pre-classified frontmatter blocks.

    ``privacy_tier: open`` is explicit because since #876 an untiered
    fragment ranks as PERSONAL and reaches the specialists as a title-only
    summary at the default OPEN ceiling. These tests assert on signal
    keywords in the *body*, so an implicit tier would strip exactly the
    text under test. Tier policy itself is covered elsewhere.
    """
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"privacy_tier: open\n"
        f"source:\n  platform: journal\n  author: self\n{frontmatter}---\n{body}\n",
        encoding="utf-8",
    )


# Bodies dense with rule-classifier signal keywords (creek/classify/rules.py)
# so deterministic keyword classification fires without any LLM.
_F6_BODY = "community empathy harmony inclusion caring sharing equality consensus"
_F3_BODY = "power dominance control conquest force aggression warrior rebellion"
_RISING_BODY = "emerging building growing momentum ascending intensifying gathering"
_DIMINISHING_BODY = "diminishing declining waning subsiding settling calming quieting"
# Confidence-keyword bodies (CONFIDENCE_SIGNALS) drawn from an opposite pair —
# MUSING vs CONVICTION — so the rule classifier resolves opposite confidence and
# the Ontology agent surfaces a confidence paradox deterministically.
_MUSING_BODY = "maybe perhaps wondering possibly might not sure could be"
_CONVICTION_BODY = "absolutely undeniably fundamental non-negotiable i am certain"
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


def test_ontology_surfaces_confidence_paradox_without_resolving(
    tmp_path: Path,
) -> None:
    """Opposite confidence levels across fragments surface a confidence paradox."""
    _write_classified(tmp_path, "frag-musing", "Tentative take", body=_MUSING_BODY)
    _write_classified(tmp_path, "frag-sure", "Firm take", body=_CONVICTION_BODY)

    bundle = OntologySpecialist().gather("take", tmp_path)

    assert bundle.ontology is not None
    confidence_paradoxes = [
        p for p in bundle.ontology.paradoxes if p.kind == "confidence"
    ]
    assert confidence_paradoxes, "confidence contradiction must be surfaced"
    assert set(confidence_paradoxes[0].fragment_ids) == {"frag-musing", "frag-sure"}


def test_ontology_overall_confidence_scales_with_axis_coverage(
    tmp_path: Path,
) -> None:
    """``overall_confidence`` is a real aggregate, not 1.0 whenever a signal exists.

    A fragment keyworded only on frequency and phase leaves mode and dosage
    UNCLASSIFIED, so corpus-wide axis coverage is strictly below full
    confidence — a distinction the old ``1.0 if signals else 0.0`` placeholder
    could not express.
    """
    _write_classified(
        tmp_path, "frag-partial", "Rising power", body=f"{_F3_BODY} {_RISING_BODY}"
    )

    bundle = OntologySpecialist().gather("power", tmp_path)

    assert bundle.ontology is not None
    assert 0.0 < bundle.ontology.overall_confidence < 1.0


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
    assert bundle.ontology.overall_confidence == 0.0
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


def _cold_vault(vault: Path, count: int) -> list[str]:
    """Write *count* corpus fragments with no parquet cache, and return their ids.

    Ids are zero-padded so their lexicographic order is their numeric order,
    which is what lets a budget test name *which* fragments a bounded pass is
    required to spend on.

    Args:
        vault: Vault root.
        count: How many fragments to write.

    Returns:
        The fragment ids, in ascending id order.
    """
    ids = [f"frag-{index:02d}" for index in range(count)]
    for frag_id in ids:
        _write(vault, "01-Fragments/Notes", frag_id, f"Title {frag_id}")
    return ids


def _embed_spy() -> Any:
    """Return a patcher spying on ``generate_embedding`` while still running it.

    The same autospec/side-effect shape
    :func:`test_retrieval_cache_hit_avoids_re_embedding` uses, so the count and
    the behaviour are observed on one seam rather than a stub that changes it.

    Returns:
        The unstarted ``patch`` context manager.
    """
    return patch(
        "creek.link.embeddings.EmbeddingLinker.generate_embedding",
        autospec=True,
        side_effect=EmbeddingLinker.generate_embedding,
    )


def test_an_unbounded_gather_embeds_every_admitted_fragment_once(
    tmp_path: Path,
) -> None:
    """With no budget, a cold gather live-embeds the whole admitted corpus (#1034).

    The control for the live-embed cap, and the reason the cap is provably
    invisible to :func:`default_specialists` and therefore to the Writing Desk:
    ``max_live_embeds`` defaults to ``None``, and ``None`` must mean *exactly*
    today's behaviour — K fragments plus the query, no fragment dropped.

    Without this test, "capped" and "silently degraded" are indistinguishable
    from the outside: a cap that accidentally applied to the default path would
    quietly shrink the desk's evidence and every other test would stay green.

    The assertion is a **count**, not a claim list: the conftest mock seeds each
    vector from ``hash(text)``, which ``PYTHONHASHSEED`` randomises per process,
    so ranking order is not stable across runs and nothing here may depend on it.
    """
    # Four, so nothing is truncated by the default ``retrieval_top_k`` of 5 and
    # "embedded" is exactly set-equal to "returned" -- an assertion that holds
    # regardless of the run's ranking order.
    ids = _cold_vault(tmp_path, 4)

    with _embed_spy() as spy:
        bundle = RetrievalSpecialist().gather("title", tmp_path)

    assert spy.call_count == len(ids) + 1, "expected one embed per fragment + query"
    cited = {fid for claim in bundle.claims for fid in claim.source_fragments}
    assert cited == set(ids), "an unbounded pass dropped a fragment"


def test_a_live_embed_budget_bounds_a_cold_gather(tmp_path: Path) -> None:
    """A budget caps live embeds at ``budget + 1`` and still returns grounding.

    The cold-cache bound (#1034). With no parquet and a corpus larger than the
    budget, one call embeds the query plus at most *budget* fragments — the
    remainder are dropped from ranking rather than embedded.

    Asserted as counts and bounds only, never as which specific titles came
    back, for the ordering reason in
    :func:`test_an_unbounded_gather_embeds_every_admitted_fragment_once`. The
    non-empty-bundle half matters as much as the count: a cap that bounded the
    work by returning nothing would satisfy the count alone.
    """
    budget = 2
    _cold_vault(tmp_path, 6)

    with _embed_spy() as spy:
        bundle = RetrievalSpecialist(max_live_embeds=budget).gather("title", tmp_path)

    assert spy.call_count == budget + 1, "budget + the query, and no more"
    assert len(bundle.claims) == budget, "a bounded pass still ranks what it embedded"


def test_budget_spend_is_deterministic_and_vault_layout_independent(
    tmp_path: Path,
) -> None:
    """Which fragments spend a bounded budget is fixed by fragment id (#1034).

    A cap that spent its budget in corpus-walk order would make the answer
    depend on ``CORPUS_SUBDIRS`` order and on directory traversal — neither
    stable across platforms nor explicable to an operator, and different for the
    same vault depending on which subtree a fragment happens to live in.

    So the ranking input is walked in **fragment-id order**, and this asserts the
    consequence twice over: the same two ids survive across two independent
    gathers, and they are exactly the two lowest ids rather than whatever the
    filesystem yielded first. The second half is what makes this test fail if the
    ``sorted()`` is dropped; the first alone could pass on a stable-but-arbitrary
    traversal.
    """
    # Laid out so id order and corpus-walk order DISAGREE. ``CORPUS_SUBDIRS`` is
    # ``('01-Fragments', '09-Reference', '11-Other-Authors')``, so a walk-order
    # cap spends its two embeds on ``frag-02``/``frag-03`` under 01-Fragments,
    # while an id-order cap spends them on ``frag-00``/``frag-01`` under
    # 09-Reference. Without this disagreement the test could not tell the two
    # apart and would pass on the defect.
    for frag_id in ("frag-00", "frag-01"):
        _write(tmp_path, "09-Reference", frag_id, f"Title {frag_id}")
    for frag_id in ("frag-02", "frag-03", "frag-04", "frag-05"):
        _write(tmp_path, "01-Fragments/Notes", frag_id, f"Title {frag_id}")
    ids = ["frag-00", "frag-01", "frag-02", "frag-03", "frag-04", "frag-05"]

    first = RetrievalSpecialist(max_live_embeds=2).gather("title", tmp_path)
    second = RetrievalSpecialist(max_live_embeds=2).gather("title", tmp_path)

    def _cited(bundle: EvidenceBundle) -> set[str]:
        return {fid for claim in bundle.claims for fid in claim.source_fragments}

    assert _cited(first) == _cited(second), "budget spend differed between runs"
    assert _cited(first) == set(ids[:2]), "budget was not spent in fragment-id order"


def test_warm_fills_both_memos_so_a_later_gather_mutates_nothing(
    tmp_path: Path,
) -> None:
    """After ``warm``, ``gather`` reads both memo slots and rebinds neither (#1034).

    This is the property that makes the shared-specialist counts true under
    ``/v1``'s concurrent worker threads rather than merely likely: if any memo
    were still filled lazily *inside* ``gather``, two racing first requests would
    each find the slot empty and each would construct a linker and read the
    parquet, which atomic rebinding prevents corruption of but not duplication of.

    Identity, not equality, on both slots — an equal-but-rebuilt linker would be
    a second model load.
    """
    _cold_vault(tmp_path, 2)
    specialist = RetrievalSpecialist()

    specialist.warm(tmp_path)
    before = dict(vars(specialist))
    specialist.gather("title", tmp_path)
    after = dict(vars(specialist))

    assert before["_linker"] is not None, "warm did not build the linker"
    assert before["_cache"] is not None, "warm did not read the parquet"
    assert after["_linker"] is before["_linker"], "gather rebuilt the linker"
    assert after["_cache"] is before["_cache"], "gather re-read the parquet"
    assert set(after) == set(before), "gather added an instance attribute"


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
