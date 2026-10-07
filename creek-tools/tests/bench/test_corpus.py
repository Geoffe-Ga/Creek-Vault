"""The synthetic benchmark corpus: deterministic, neutral, and confined.

The corpus stands in for a vault so the harness never reads a real one. It
must be byte-identical for a seed (or runs are not comparable), must never
trip the care guard (or trials would measure the escalation path instead of
the model), and must refuse any root that is not a fresh directory under the
system temp dir — above all an existing vault.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import frontmatter
import pytest

from creek.care.guardrail import acute_distress_guard
from creek.vault.reader import try_validate_fragment
from creek_mcp.bench.corpus import (
    VOCABULARY,
    CorpusConfinementError,
    build_corpus,
    entry_text,
)


def _tree(root: Path) -> dict[str, bytes]:
    """Return every file under *root* keyed by its relative posix path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_deterministic_and_confined(tmp_path: Path) -> None:
    """Seed 7 twice is byte-identical, seed 8 differs, all under the root."""
    first = build_corpus(tmp_path / "a", seed=7, entries=4, words_per_entry=12)
    second = build_corpus(tmp_path / "b", seed=7, entries=4, words_per_entry=12)
    third = build_corpus(tmp_path / "c", seed=8, entries=4, words_per_entry=12)
    assert _tree(first.root) == _tree(second.root)
    assert _tree(first.root) != _tree(third.root)
    notes = sorted((first.root / "01-Fragments" / "Notes").glob("*.md"))
    assert len(notes) == 4
    assert first.entry_count == 4
    for path in first.root.rglob("*"):
        assert path.resolve().is_relative_to(first.root.resolve())


def test_corpus_notes_are_model_valid_and_open(tmp_path: Path) -> None:
    """Every note is a valid ``open`` fragment the grounder can actually see."""
    corpus = build_corpus(tmp_path / "v", seed=1, entries=2, words_per_entry=5)
    for path in (corpus.root / "01-Fragments" / "Notes").glob("*.md"):
        post = frontmatter.load(path)
        assert post.metadata["privacy_tier"] == "open"
        assert try_validate_fragment(post.metadata, path) is not None
        assert len(post.content.split()) == 5
    assert (corpus.root / "00-Creek-Meta").is_dir()


def test_refuses_root_outside_tempdir() -> None:
    """A root outside the system temp dir is refused before any write."""
    outside = Path(__file__).resolve().parent / "never-created-bench-corpus"
    with pytest.raises(CorpusConfinementError, match="temp"):
        build_corpus(outside, seed=1, entries=1, words_per_entry=1)
    assert not outside.exists()


def test_refuses_existing_vault(tmp_path: Path) -> None:
    """An existing vault is refused and left exactly as it was."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    before = sorted(vault.rglob("*"))
    with pytest.raises(CorpusConfinementError, match="empty"):
        build_corpus(vault, seed=1, entries=1, words_per_entry=1)
    assert sorted(vault.rglob("*")) == before


def test_refuses_a_file_root(tmp_path: Path) -> None:
    """A path that exists as a file is not a corpus root."""
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(CorpusConfinementError, match="directory"):
        build_corpus(target, seed=1, entries=1, words_per_entry=1)


def test_accepts_an_existing_empty_directory(tmp_path: Path) -> None:
    """An empty directory under temp (a ``TemporaryDirectory``) is fine."""
    corpus = build_corpus(tmp_path, seed=1, entries=1, words_per_entry=1)
    assert corpus.root == tmp_path.resolve()


def test_refuses_the_tempdir_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The temp dir root is shared; only a directory *under* it is admitted."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(Path.cwd()))
    with pytest.raises(CorpusConfinementError, match="temp"):
        build_corpus(Path.cwd(), seed=1, entries=1, words_per_entry=1)


def test_entry_text_size_scales_with_words() -> None:
    """The word count is exact, so the context sweep's input size is known."""
    for words in (1, 64, 512):
        assert len(entry_text(seed=3, index=0, words=words).split()) == words
    assert entry_text(seed=3, index=0, words=0) == ""


def test_entry_text_is_pinned_across_processes() -> None:
    """A golden value: ``hash()`` or an unseeded RNG would break this.

    Equality within one process proves nothing about another, because
    ``hash()`` is salted per process; only a pinned literal does.
    """
    assert entry_text(seed=7, index=0, words=6) == (
        "violet anchor ribbon marble anchor window"
    )


def test_corpus_bytes_are_pinned(tmp_path: Path) -> None:
    """The whole corpus, front matter included, is byte-stable for a seed."""
    corpus = build_corpus(tmp_path / "v", seed=7, entries=2, words_per_entry=4)
    digest = hashlib.sha256()
    for path in sorted(corpus.root.rglob("*.md")):
        digest.update(path.relative_to(corpus.root).as_posix().encode())
        digest.update(path.read_bytes())
    assert digest.hexdigest() == (
        "4c88185c99a2dbee12dc18518332d783ddf393bd7bddb62481ef6646723645dd"
    )


def test_entry_text_differs_by_index_and_seed() -> None:
    """Each trial gets distinct text, so no runtime cache can serve it."""
    base = entry_text(seed=3, index=0, words=16)
    assert entry_text(seed=3, index=1, words=16) != base
    assert entry_text(seed=4, index=0, words=16) != base
    assert entry_text(seed=3, index=0, words=16) == base


def test_vocabulary_never_trips_the_care_guard() -> None:
    """The synthetic text must measure the model, never the escalation path."""
    assert acute_distress_guard(" ".join(VOCABULARY)) is None
    assert acute_distress_guard(entry_text(seed=9, index=2, words=2048)) is None


def test_rejects_negative_sizes(tmp_path: Path) -> None:
    """Sizes are counts; a negative one is a caller bug."""
    with pytest.raises(ValueError, match="entries"):
        build_corpus(tmp_path / "v", seed=1, entries=-1, words_per_entry=1)
    with pytest.raises(ValueError, match="words"):
        entry_text(seed=1, index=0, words=-1)
