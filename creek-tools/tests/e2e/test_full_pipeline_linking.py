"""End-to-end proof that ``creek process`` *persists* its link stage (#1303).

Stage 5 of :meth:`creek.pipeline.Pipeline.run` used to compute the whole
link graph in memory and throw it away, while the CLI printed
``Links found: N``. Nothing reached the vault: no eddy page, no thread
page, no ``eddies:``/``threads:`` frontmatter, no embeddings parquet.

Every assertion in this module is therefore phrased against **on-disk
state**, never against a returned count — the defect was precisely that
the count lied about the disk.

Two fake-green traps this module deliberately avoids:

* ``03-Eddies/Eddy-Map.md`` and ``02-Threads/Thread-Index.md`` already
  exist after every ``creek process`` today — Stage 6's ``IndexGenerator``
  writes them, and ``tests/test_pipeline.py`` asserts exactly those two
  paths and is green even with Stage 5 orphaned. A test phrased as "some
  file exists under ``03-Eddies/``" passes *before* the fix. The helpers
  below exclude the index artefacts and look only for real cluster pages.
* Asserting a fragment's in-memory ``eddies``/``threads`` list proves
  nothing, because ``LinkingPipeline`` populated exactly those lists and
  still wrote no bytes. Membership is re-read off disk with
  :func:`frontmatter.load`.

Fixture facts that are load-bearing, each verified against source:

* **Six notes per topic, not four.** ``eddy_min_fragments`` and
  ``eddy_min_samples`` both default to 5 (``creek/config.py``), so a
  four-note neighbourhood is DBSCAN noise and the eddy assertion could
  never go green.
* **Identical unit vectors per topic, with no jitter.**
  ``EddyDetector._has_temporal_direction`` discards any cluster whose
  |Spearman(chronological rank, cosine drift)| reaches
  ``eddy_correlation_threshold`` (0.3). Identical vectors force every
  drift to 0.0, so the rank correlation short-circuits to 0.0 and the
  cluster survives. Jitter makes that filter fire on a large fraction of
  ingestion orderings at n=6 — it is what would make this test flaky.
* **The topic keyword lives in the H1.**
  ``fragment_embedding_text`` returns the *title* only and the markdown
  ingestor derives the title from the H1, so the same word drives both
  the embedding and — at ``TITLE_WEIGHT`` 3 against a
  ``PRIMARY_THRESHOLD`` of 3 — the rule classifier's
  ``frequency.primary``. A classified primary frequency is what makes
  threads reachable on a ``--no-llm`` corpus:
  ``ThreadDetector._topic_consistent`` short-circuits on
  ``_frequency_overlap``, which treats ``unclassified`` as no signal.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import frontmatter
import numpy as np
import pytest
from typer.testing import CliRunner

from creek.cli import app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.e2e

_TOPIC_A = "strategy"
"""An F5 keyword (``creek/classify/rules.py``), one per topic-A H1."""

_TOPIC_B = "community"
"""An F6 keyword, one per topic-B H1."""

_NOTES_PER_TOPIC = 6
"""Floor, not preference: ``eddy_min_samples``/``eddy_min_fragments`` are 5."""

_VECTORS: dict[str, list[float]] = {
    _TOPIC_A: [1.0, 0.0, 0.0],
    _TOPIC_B: [0.0, 1.0, 0.0],
}
"""Orthonormal per-topic vectors. Within-topic cosine distance is 0.0
(well inside ``eddy_eps`` 0.3 and above ``thread_similarity_threshold``
0.6); cross-topic distance is 1.0, so the two topics never merge."""

_OTHER_VECTOR = [0.0, 0.0, 1.0]
"""Anything the fixture did not author (e.g. a review-queue note)."""


def _topic_of(text: str) -> str | None:
    """Return the fixture topic a piece of text belongs to, if any.

    Args:
        text: Title (the only thing the embedder ever sees).

    Returns:
        ``_TOPIC_A`` / ``_TOPIC_B``, or ``None`` for foreign text.
    """
    lowered = text.lower()
    if _TOPIC_A in lowered:
        return _TOPIC_A
    if _TOPIC_B in lowered:
        return _TOPIC_B
    return None


def _make_topic_encoder() -> MagicMock:
    """Build a sentence-transformer stub that emits one vector per topic.

    The repo-wide autouse mock in ``tests/conftest.py`` seeds a random
    normal off ``hash(text)``, which is ``PYTHONHASHSEED``-dependent and
    near-orthogonal in high dimensions — it never clusters. This stub
    overrides it for the duration of a test.

    Returns:
        A mock whose ``.encode`` returns the topic's exact unit vector.
    """
    model = MagicMock()

    def _vector(text: str) -> list[float]:
        topic = _topic_of(text)
        return _VECTORS[topic] if topic else _OTHER_VECTOR

    def _encode(sentences: str | list[str], **_kwargs: Any) -> np.ndarray:
        if isinstance(sentences, str):
            return np.array(_vector(sentences), dtype=np.float32)
        return np.array([_vector(s) for s in sentences], dtype=np.float32)

    model.encode = MagicMock(side_effect=_encode)
    return model


@pytest.fixture()
def topic_encoder() -> Iterator[MagicMock]:
    """Patch the embedding model with the deterministic topic encoder."""
    model = _make_topic_encoder()
    with patch(
        "creek.link.embeddings._load_sentence_transformer",
        return_value=model,
    ):
        yield model


def _write_corpus(source: Path) -> None:
    """Populate *source* with a two-topic markdown corpus.

    Args:
        source: Empty source directory the pipeline will ingest.
    """
    for topic in (_TOPIC_A, _TOPIC_B):
        for index in range(_NOTES_PER_TOPIC):
            title = f"{topic.capitalize()} note {index}"
            (source / f"{topic}-{index}.md").write_text(
                f"# {title}\n\nParagraph {index} of the {topic} corpus.\n",
                encoding="utf-8",
            )


def _run_process(source: Path, vault: Path) -> str:
    """Drive ``creek process`` through Typer's runner and return its output.

    The CLI surface — not :meth:`Pipeline.run` — because the printed
    summary is itself under test: the operator-facing number has to match
    what landed on disk.

    Args:
        source: Source directory to ingest.
        vault: Vault root to write into.

    Returns:
        The command's captured stdout.
    """
    result = CliRunner().invoke(
        app,
        [
            "process",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
            "--no-llm",
        ],
    )
    assert result.exit_code == 0, (
        f"creek process exited {result.exit_code}\n{result.output}\n"
        f"{result.exception!r}"
    )
    return result.output


def _cluster_pages(vault: Path) -> tuple[list[Path], list[Path]]:
    """Return the real eddy pages and thread pages written to *vault*.

    ``Eddy-Map.md`` is excluded because ``IndexGenerator`` writes it on
    every run — including runs where Stage 5 persisted nothing.
    ``Thread-Index.md`` is excluded structurally: real thread pages live
    one level down, under ``02-Threads/{Active,Dormant,Resolved}/``.

    Args:
        vault: Vault root.

    Returns:
        ``(eddy_pages, thread_pages)``.
    """
    eddies = [
        path
        for path in (vault / "03-Eddies").glob("*.md")
        if path.name != "Eddy-Map.md"
    ]
    threads = sorted((vault / "02-Threads").glob("*/*.md"))
    return sorted(eddies), threads


def _links_of(fragment_path: Path, key: str) -> list[str]:
    """Read a fragment's ``eddies:``/``threads:`` list back off disk.

    Args:
        fragment_path: Path to a fragment markdown file.
        key: Frontmatter key to read.

    Returns:
        The wiki-links recorded in that key, or ``[]``.
    """
    post = frontmatter.load(fragment_path)
    return list(post.metadata.get(key) or [])


def _fragment_paths(vault: Path) -> list[Path]:
    """Return every fragment file under ``01-Fragments/``.

    Args:
        vault: Vault root.

    Returns:
        Sorted fragment paths.
    """
    return sorted((vault / "01-Fragments").rglob("*.md"))


def _titles(pages: list[Path]) -> set[str]:
    """Return the frontmatter ``title`` of each page.

    Args:
        pages: Cluster pages to read.

    Returns:
        The set of page titles.
    """
    return {str(frontmatter.load(page).metadata["title"]) for page in pages}


def _membership_id(prefix: str, member_ids: list[str]) -> str:
    """Re-derive a cluster's membership-stable id from its members.

    Mirrors ``_stable_eddy_id``/``_stable_thread_id``: a sha256 over the
    sorted member ids, truncated to eight hex characters. Re-deriving it
    here (rather than importing the private helper) means the assertion
    would catch the id formula silently changing.

    Args:
        prefix: ``"eddy"`` or ``"thread"``.
        member_ids: The cluster's member fragment ids.

    Returns:
        The expected ``<prefix>-<8hex>`` identifier.
    """
    member_key = ",".join(sorted(member_ids))
    digest = hashlib.sha256(member_key.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def _members_linking_to(vault: Path, key: str, title: str) -> list[str]:
    """Return the ids of fragments whose *key* frontmatter cites *title*.

    Args:
        vault: Vault root.
        key: ``"eddies"`` or ``"threads"``.
        title: Cluster page title.

    Returns:
        Fragment ids, unsorted.
    """
    members: list[str] = []
    for path in _fragment_paths(vault):
        post = frontmatter.load(path)
        if f"[[{title}]]" in list(post.metadata.get(key) or []):
            members.append(str(post.metadata["id"]))
    return members


def _parse_count(output: str, pattern: str) -> int:
    """Pull a single integer out of the CLI summary.

    Args:
        output: Captured stdout of ``creek process``.
        pattern: Regex with one integer capture group.

    Returns:
        The captured integer.
    """
    # Rich soft-wraps long lines at the console width, so collapse
    # whitespace before matching rather than assuming one physical line.
    flattened = " ".join(output.split())
    match = re.search(pattern, flattened)
    assert match is not None, f"{pattern!r} not found in:\n{output}"
    return int(match.group(1))


def test_process_persists_eddy_and_thread_pages(
    synthetic_vault: Path,
    synthetic_source: Path,
    topic_encoder: MagicMock,
) -> None:
    """``creek process`` must leave its link graph on disk, not in memory."""
    _write_corpus(synthetic_source)

    output = _run_process(synthetic_source, synthetic_vault)

    eddy_pages, thread_pages = _cluster_pages(synthetic_vault)
    eddy_dir = sorted(p.name for p in (synthetic_vault / "03-Eddies").iterdir())
    thread_dir = sorted(p.name for p in (synthetic_vault / "02-Threads").rglob("*"))
    assert eddy_pages, (
        "creek process reported links but wrote no eddy page; 03-Eddies "
        f"holds only {eddy_dir} (#1303)."
    )
    assert thread_pages, (
        "creek process wrote no thread page under 02-Threads/"
        "{Active,Dormant,Resolved}/; the directory holds only "
        f"{thread_dir} (#1303)."
    )
    assert (synthetic_vault / "00-Creek-Meta" / "embeddings.parquet").exists(), (
        "creek process embedded fragments but persisted no embeddings cache."
    )

    # The clobber guard. ``_persist_fragment_link_updates`` overwrites
    # every Fragment-owned frontmatter key from the in-memory model, so a
    # design that loads fragments once and runs both detectors off that
    # single list writes ``threads: []`` over the eddies pass's work (or
    # vice versa). Such a design still passes the page-existence checks
    # above; it fails here.
    eddy_links = {f"[[{title}]]" for title in _titles(eddy_pages)}
    thread_links = {f"[[{title}]]" for title in _titles(thread_pages)}
    linked_both = [
        path
        for path in _fragment_paths(synthetic_vault)
        if eddy_links & set(_links_of(path, "eddies"))
        and thread_links & set(_links_of(path, "threads"))
    ]
    fragments = _fragment_paths(synthetic_vault)
    # Both topics cluster, so EVERY fragment is a member of one eddy and
    # one thread. Asserting equality rather than a lower bound matters:
    # ``>= _NOTES_PER_TOPIC`` would still pass if one whole topic silently
    # failed to link, since the other topic alone supplies six.
    assert len(fragments) == 2 * _NOTES_PER_TOPIC
    assert len(linked_both) == len(fragments), (
        "every fragment must carry BOTH its eddy and its thread wiki-link on "
        f"disk; only {len(linked_both)} of {len(fragments)} do."
    )

    # No dangling wiki-links — and no cross-filed ones. The two link kinds
    # are checked against their OWN page set, not the union: in this
    # fixture the eddy and thread titles are the same strings, so a bug
    # that filed an eddy link under ``threads:`` would sail through a
    # pooled membership check.
    links_by_key = {"eddies": eddy_links, "threads": thread_links}
    for path in fragments:
        for key, valid in links_by_key.items():
            found = _links_of(path, key)
            for link in found:
                assert link in valid, (
                    f"{path.name} cites {link} under {key}:, which is not a "
                    f"page written by the {key} stage (valid: {sorted(valid)})."
                )
            # Exactly one, not merely "at least one from the right set".
            # Each fragment belongs to exactly one eddy and one thread here,
            # and — because this fixture's eddy and thread pages share a
            # title — a bug that ALSO filed the eddy link under ``threads:``
            # would be textually indistinguishable from a correct thread
            # link. The arity is what catches it.
            assert len(found) == 1, (
                f"{path.name} carries {len(found)} {key}: links ({found}); "
                "each fragment belongs to exactly one cluster per kind, so a "
                "second entry means a link was filed under the wrong key."
            )

    # Membership-derived ids, re-derived from what is actually on disk.
    for page in eddy_pages:
        meta = frontmatter.load(page).metadata
        title = str(meta["title"])
        expected = _membership_id(
            "eddy",
            _members_linking_to(synthetic_vault, "eddies", title),
        )
        assert meta["id"] == expected, (
            f"eddy page {page.name} carries id {meta['id']!r}, but its on-disk "
            f"membership hashes to {expected!r}."
        )

    # The printed summary must match the disk, not the in-memory graph.
    assert _parse_count(output, r"(\d+) eddy file\(s\) written") == len(eddy_pages)
    assert _parse_count(output, r"(\d+) page\(s\) written") == len(thread_pages)
    assert _parse_count(output, r"Link artefacts persisted: (\d+)") == len(
        eddy_pages
    ) + len(thread_pages)


def test_second_process_run_is_idempotent_on_disk(
    synthetic_vault: Path,
    synthetic_source: Path,
    topic_encoder: MagicMock,
) -> None:
    """A re-run over unchanged sources must not churn pages or fragments."""
    _write_corpus(synthetic_source)
    _run_process(synthetic_source, synthetic_vault)

    first_eddies, first_threads = _cluster_pages(synthetic_vault)
    assert first_eddies and first_threads
    before = {
        path: path.read_text(encoding="utf-8")
        for path in _fragment_paths(synthetic_vault)
    }

    embedded: list[set[str]] = []
    from creek.link.embeddings import EmbeddingLinker

    original = EmbeddingLinker.generate_embeddings

    def _spy(
        self: EmbeddingLinker,
        fragments: list[Any],
        existing_ids: set[str] | None = None,
    ) -> dict[str, list[float]]:
        computed = original(self, fragments, existing_ids=existing_ids)
        embedded.append(set(computed))
        return computed

    with patch.object(EmbeddingLinker, "generate_embeddings", _spy):
        _run_process(synthetic_source, synthetic_vault)

    second_eddies, second_threads = _cluster_pages(synthetic_vault)
    assert [p.name for p in second_eddies] == [p.name for p in first_eddies], (
        "a second creek process minted new eddy pages for unchanged membership."
    )
    assert [p.name for p in second_threads] == [p.name for p in first_threads], (
        "a second creek process minted new thread pages for unchanged membership."
    )

    after = {
        path: path.read_text(encoding="utf-8")
        for path in _fragment_paths(synthetic_vault)
    }
    for path, text in before.items():
        assert after.get(path) == text, (
            f"{path.name} was rewritten on the second run despite unchanged "
            "cluster membership."
        )

    assert embedded, "the embedding linker was never called on the second run"
    fresh = set().union(*embedded)
    assert not fresh, (
        "the second run recomputed vectors for unchanged fragments instead of "
        f"reusing the parquet cache: {sorted(fresh)}"
    )


def test_process_preserves_foreign_frontmatter_and_body(
    synthetic_vault: Path,
    synthetic_source: Path,
    topic_encoder: MagicMock,
) -> None:
    """Rewriting link frontmatter must not disturb anything else in the file.

    The rewrite has to actually happen for this to prove anything. Picking
    the first fragment that carries an eddy link is not enough: cluster ids
    are membership-derived, so only the topic whose membership *changes*
    gets rewritten, and the trigger note added below belongs to topic A.
    Selecting a topic-B fragment (which is what alphabetical order yields —
    ``Community`` sorts before ``Strategy``) makes every assertion here
    pass because nothing ever wrote to the file. The rewrite is therefore
    asserted explicitly, before preservation is asserted at all.
    """
    _write_corpus(synthetic_source)
    _run_process(synthetic_source, synthetic_vault)

    member = next(
        path
        for path in _fragment_paths(synthetic_vault)
        if _TOPIC_A in str(frontmatter.load(path).metadata["title"]).lower()
        and _links_of(path, "eddies")
    )
    post = frontmatter.load(member)
    post.metadata["operator_tag"] = "keep-me"
    post.metadata["classification_method"] = "hand"
    body_before = post.content
    eddies_before = list(post.metadata["eddies"])
    # The baseline itself has to be real. Run 1 already rewrote this file
    # (that is what put the eddy link in it), so a bug that dropped the
    # body would have dropped it *before* this line — and comparing an
    # empty baseline to an empty result passes. Anchor on the fixture's
    # own text instead of trusting whatever survived run 1.
    assert "corpus." in body_before, (
        "the fragment body was already lost before this test started, so the "
        f"preservation assertion below would be vacuous; body={body_before!r}"
    )
    member.write_text(frontmatter.dumps(post), encoding="utf-8")

    # A fresh topic-A note shifts topic A's eddy membership, which changes
    # its membership-derived id and title and so forces a rewrite of every
    # topic-A member's frontmatter.
    (synthetic_source / f"{_TOPIC_A}-extra.md").write_text(
        f"# {_TOPIC_A.capitalize()} note extra\n\nAnother paragraph.\n",
        encoding="utf-8",
    )
    _run_process(synthetic_source, synthetic_vault)

    reread = frontmatter.load(member)
    assert list(reread.metadata["eddies"]) != eddies_before, (
        "the trigger note did not change this fragment's eddy membership, so "
        "_persist_fragment_link_updates never rewrote the file and the "
        "preservation assertions below would pass vacuously"
    )
    assert reread.metadata["operator_tag"] == "keep-me"
    assert reread.metadata["classification_method"] == "hand"
    assert reread.content == body_before
