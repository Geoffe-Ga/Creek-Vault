"""The synthetic voice corpus: one invented voice, 10-20 entries, temp-confined.

Nothing here is a real journal. The entries are written for this harness, in a
voice chosen to be measurably distinct (short declaratives, heavy first person,
plain concrete nouns), so a candidate that drifts into generic essay prose is
measurably further away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import load_fingerprint
from creek_mcp.bench.corpus import META_DIR, CorpusConfinementError
from creek_mcp.bench.eval.voice_corpus import (
    DRAFTS_DIR,
    MAX_ENTRIES,
    MIN_ENTRIES,
    VOICE_ENTRIES,
    build_voice_corpus,
)


def test_the_shipped_voice_sits_in_the_ticket_s_range() -> None:
    """#1875 asks for 10-20 sample entries; the corpus ships that many."""
    assert MIN_ENTRIES <= len(VOICE_ENTRIES) <= MAX_ENTRIES


def test_entries_are_distinct_texts() -> None:
    """Duplicate entries would inflate the fingerprint's apparent support."""
    assert len(set(VOICE_ENTRIES)) == len(VOICE_ENTRIES)


def test_build_writes_readable_fragments_and_a_meta_dir(tmp_path: Path) -> None:
    """The corpus is a vault the shared voice code paths can actually read."""
    corpus = build_voice_corpus(tmp_path / "voice")
    assert corpus.entry_count == len(VOICE_ENTRIES)
    assert len(corpus.fragment_ids) == corpus.entry_count
    assert (corpus.root / META_DIR).is_dir()
    assert (corpus.root / DRAFTS_DIR).is_dir()


def test_build_persists_a_non_empty_fingerprint(tmp_path: Path) -> None:
    """Without a fingerprint the shared voice-fidelity check cannot measure."""
    corpus = build_voice_corpus(tmp_path / "voice")
    fingerprint = load_fingerprint(corpus.root, AIStyleConfig())
    assert fingerprint.fragment_count == corpus.entry_count
    assert fingerprint.features


def test_voice_sample_is_the_reference_the_judge_compares_against(
    tmp_path: Path,
) -> None:
    """The reference text is the corpus's own prose, nothing else."""
    corpus = build_voice_corpus(tmp_path / "voice")
    for entry in VOICE_ENTRIES:
        assert entry in corpus.voice_sample


def test_build_is_byte_stable(tmp_path: Path) -> None:
    """Two builds give identical files, so a run is reproducible."""
    first = build_voice_corpus(tmp_path / "a")
    second = build_voice_corpus(tmp_path / "b")
    for ident in first.fragment_ids:
        name = f"{ident}.md"
        left = next(first.root.rglob(name)).read_text(encoding="utf-8")
        right = next(second.root.rglob(name)).read_text(encoding="utf-8")
        assert left == right


def test_build_refuses_a_root_outside_the_temp_dir() -> None:
    """A real vault can never be used as the eval corpus."""
    with pytest.raises(CorpusConfinementError):
        build_voice_corpus(Path.home() / "Creek-Vault-not-a-temp-dir")


def test_build_refuses_a_populated_directory(tmp_path: Path) -> None:
    """An occupied directory is refused untouched."""
    root = tmp_path / "occupied"
    root.mkdir()
    (root / "keep.md").write_text("mine", encoding="utf-8")
    with pytest.raises(CorpusConfinementError):
        build_voice_corpus(root)
    assert (root / "keep.md").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("entries", [MIN_ENTRIES - 1, MAX_ENTRIES + 1])
def test_build_refuses_an_out_of_range_entry_count(
    tmp_path: Path, entries: int
) -> None:
    """Fewer than ten entries is too thin a voice to score against."""
    with pytest.raises(ValueError, match="entries"):
        build_voice_corpus(tmp_path / "voice", entries=entries)


def test_a_smaller_corpus_is_a_prefix_of_the_full_one(tmp_path: Path) -> None:
    """Shrinking the corpus must not reshuffle which entries are used."""
    small = build_voice_corpus(tmp_path / "small", entries=MIN_ENTRIES)
    assert small.entry_count == MIN_ENTRIES
    assert small.fragment_ids == tuple(
        f"voice-{index:04d}" for index in range(MIN_ENTRIES)
    )
