"""A deterministic synthetic corpus, confined to a fresh temp directory.

The harness must never read a real vault, so it writes its own: ``entries``
model-valid ``open`` fragments under ``01-Fragments/Notes``, plus the
``00-Creek-Meta`` directory ``reflect_tool`` appends its audit row to. Text is
drawn from a fixed neutral vocabulary by hashing ``seed:index:position`` with
SHA-256, so it is byte-stable across processes and platforms (``hash()`` is
salted per process; ``random`` without a seed is not reproducible) and no
runtime cache can serve one trial's text for another's.

**Confinement.** The root must resolve strictly *under*
:func:`tempfile.gettempdir`, and be either absent or an empty directory. Both
are checked before anything is written, so an existing vault — or any
populated directory — is refused untouched.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from creek._containment import escaping_child

VOCABULARY: Final[tuple[str, ...]] = (
    "garden",
    "river",
    "stone",
    "window",
    "morning",
    "kettle",
    "orchard",
    "lantern",
    "meadow",
    "pebble",
    "harbor",
    "willow",
    "ladder",
    "candle",
    "basket",
    "bridge",
    "copper",
    "valley",
    "thread",
    "marble",
    "pocket",
    "saddle",
    "timber",
    "violet",
    "walnut",
    "anchor",
    "button",
    "canvas",
    "cobalt",
    "fennel",
    "glacier",
    "hazel",
    "indigo",
    "jasmine",
    "kayak",
    "lemon",
    "maple",
    "nutmeg",
    "olive",
    "paddle",
    "quartz",
    "ribbon",
    "spruce",
    "teapot",
    "umber",
    "velvet",
    "wicker",
    "yarrow",
    "zephyr",
    "acorn",
    "barley",
    "cedar",
    "dune",
    "ember",
    "fjord",
    "granite",
    "heron",
    "island",
    "juniper",
    "kelp",
    "linen",
    "moss",
    "nectar",
    "oat",
)
"""Neutral, care-safe words. None can form a first-person distress phrase."""

FRAGMENTS_DIR: Final[Path] = Path("01-Fragments") / "Notes"
"""Where the corpus notes live, relative to the corpus root."""

META_DIR: Final[str] = "00-Creek-Meta"
"""The vault meta directory ``reflect_tool`` writes its audit log under."""

_INDEX_BYTES: Final[int] = 4
"""Digest bytes read per word choice; ample for a 64-word vocabulary."""

_FENCE: Final[str] = "---"
"""Front-matter delimiter."""

_STAMP: Final[str] = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
"""Fixed creation time, so the files are byte-identical across runs."""

_NOT_UNDER_TEMP: Final[str] = "corpus root must be a directory under the temp dir"
_NOT_EMPTY: Final[str] = "corpus root must be absent or an empty directory"
_NOT_A_DIRECTORY: Final[str] = "corpus root exists and is not a directory"
_ESCAPED: Final[str] = "corpus directory resolves outside its root"
_NEGATIVE_WORDS: Final[str] = "words must be non-negative"
_NEGATIVE_ENTRIES: Final[str] = "entries must be non-negative"


class CorpusConfinementError(ValueError):
    """The requested corpus root is not a fresh directory under temp."""


@dataclass(frozen=True, slots=True)
class Corpus:
    """A built synthetic corpus.

    Attributes:
        root: The resolved corpus root, usable as ``vault_path``.
        entry_count: How many fragments were written.
    """

    root: Path
    entry_count: int


def _word_index(seed: int, index: int, position: int) -> int:
    """Return the vocabulary index for one word, stable across processes."""
    digest = hashlib.sha256(f"{seed}:{index}:{position}".encode()).digest()
    return int.from_bytes(digest[:_INDEX_BYTES], "big") % len(VOCABULARY)


def entry_text(*, seed: int, index: int, words: int) -> str:
    """Return *words* deterministic vocabulary words for entry *index*.

    Raises:
        ValueError: When *words* is negative.
    """
    if words < 0:
        raise ValueError(_NEGATIVE_WORDS)
    return " ".join(
        VOCABULARY[_word_index(seed, index, position)] for position in range(words)
    )


def _confined_root(root: Path) -> Path:
    """Resolve *root* and refuse anything but a fresh directory under temp."""
    resolved = root.resolve(strict=False)
    temp = Path(tempfile.gettempdir()).resolve()
    if resolved == temp or not resolved.is_relative_to(temp):
        raise CorpusConfinementError(_NOT_UNDER_TEMP)
    if resolved.exists() and not resolved.is_dir():
        raise CorpusConfinementError(_NOT_A_DIRECTORY)
    if resolved.is_dir() and any(resolved.iterdir()):
        raise CorpusConfinementError(_NOT_EMPTY)
    return resolved


def _note(seed: int, index: int, words: int) -> str:
    """Render one model-valid ``open`` fragment note.

    Each front-matter value is written as JSON, which is valid YAML flow
    syntax. Serialising by hand rather than through a YAML emitter keeps the
    bytes independent of the installed PyYAML's formatting choices, so the
    corpus stays byte-stable across environments.
    """
    note_id = f"bench-{index:04d}"
    metadata: dict[str, Any] = {
        "type": "fragment",
        "id": note_id,
        "title": f"bench note {index:04d}",
        "created": _STAMP,
        "ingested": _STAMP,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": "open",
        "eddies": [],
    }
    body = entry_text(seed=seed, index=index, words=words)
    header = "\n".join(f"{key}: {json.dumps(value)}" for key, value in metadata.items())
    return f"{_FENCE}\n{header}\n{_FENCE}\n{body}\n"


def build_corpus(
    root: Path, *, seed: int, entries: int, words_per_entry: int
) -> Corpus:
    """Write a synthetic corpus of *entries* notes under *root*.

    Args:
        root: Absent or empty directory strictly under the system temp dir.
        seed: Selects the text; equal seeds give byte-identical corpora.
        entries: Number of fragment notes to write.
        words_per_entry: Words in each note body.

    Returns:
        The built corpus.

    Raises:
        CorpusConfinementError: When *root* is not admissible; nothing is
            written.
        ValueError: When a size is negative.
    """
    if entries < 0:
        raise ValueError(_NEGATIVE_ENTRIES)
    resolved = _confined_root(root)
    notes_dir = resolved / FRAGMENTS_DIR
    meta_dir = resolved / META_DIR
    for directory in (notes_dir, meta_dir):
        directory.mkdir(parents=True, exist_ok=True)
        if escaping_child(directory, resolved):
            raise CorpusConfinementError(_ESCAPED)
    for index in range(entries):
        (notes_dir / f"bench-{index:04d}.md").write_text(
            _note(seed, index, words_per_entry), encoding="utf-8"
        )
    return Corpus(root=resolved, entry_count=entries)
