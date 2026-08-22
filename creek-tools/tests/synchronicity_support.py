"""One hostile-fixture set for the readers of ``10-Liminal/Synchronicities/``.

Four production functions read that one folder (#1416): the dedup read-back
scan in ``creek.generate.synchronicity``, the ``synchronicity`` lint check,
``creek state``'s ``_load_synchronicities``, and ``creek mine``'s
``_load_synchronicities``. The folder is operator-editable — Obsidian, a text
editor and ``creek save`` all write into it — so every one of those readers
eventually meets hand-written YAML.

Before #1416 the hostile fixtures existed for the *paradox* sibling only, and
each synchronicity reader was exercised (if at all) against a different scrap
of markdown, which is how three of the four came to share a bug nobody had
tested for. Keeping the fixtures here means a note shape that breaks one
reader is planted into all four by construction rather than by somebody
remembering to copy it across.

Nothing in this module asserts anything about behaviour; it only builds
vaults. The invariants live in the test modules that import it.

This module is deliberately *not* named ``test_*``: pytest's ``python_files``
glob is ``test_*.py``, so it is imported, never collected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from creek.config import EmbeddingsConfig
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    embeddings_cache_path,
)
from creek.models import (
    Fragment,
    FragmentSource,
    SourcePlatform,
)
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path


HAND_BROKEN_YAML = "---\nfragments: [a, b\ntype: synchronicity\n---\nbody\n"
"""An unclosed flow sequence: a hand edit that never got its ``]`` back.

``frontmatter.loads``/``frontmatter.load`` raise ``yaml.parser.ParserError``
on this, which is **not** a ``ValueError`` — an ``except (OSError,
ValueError)`` tuple lets it straight through and the whole scan dies.
"""

NON_STRING_KEY = (
    "---\n"
    "2024-01-01: reflection\n"
    "type: synchronicity\n"
    "id: sync-key\n"
    "fragment_a_id: p\n"
    "fragment_b_id: q\n"
    "similarity: 0.95\n"
    "time_gap_days: 40\n"
    'fragments: ["[[p]]", "[[q]]"]\n'
    "---\n"
    "body\n"
)
"""A perfectly valid YAML header whose first key parses as a ``date``.

The header is well-formed, so no ``yaml.YAMLError`` is raised — but
``frontmatter.load*`` finishes with ``Post(content, handler, **metadata)``
and the splat raises ``TypeError: keywords must be strings``, past *any*
``except`` tuple built out of ``OSError``/``ValueError``/``yaml.YAMLError``.
Every other key here is the real synchronicity schema, so a header-only
reader hands the note back intact.
"""

LEADING_BLANK_LINE = (
    "\n"
    "---\n"
    "type: synchronicity\n"
    "id: sync-blank\n"
    'fragments: ["[[frag-synx-aaaa]]", "[[frag-synx-bbbb]]"]\n'
    "---\n"
    "body\n"
)
"""A note whose ``---`` fence sits on line 2, behind one blank line.

``frontmatter.loads`` strips leading whitespace before hunting the fence, so
it reads the header; a header-only reader that requires ``---`` on line 1
reads the file as carrying no frontmatter at all. The two readers disagree,
and this constant is the fixture that pins which disagreement creek ships.
"""

HOSTILE_CASES: tuple[str, ...] = (
    "hand-broken-yaml",
    "non-string-key",
    "directory-named-md",
)
"""The entries an operator-editable folder is expected to survive.

Parametrised over every reader; two of the three were fatal to at least one
reader at HEAD, and the third (``directory-named-md``) is the parity lock
that keeps the pre-existing ``OSError`` skip honest.
"""

assert len(HOSTILE_CASES) == 3, "emptying the hostile-case list must not silently skip"


def plant_hostile_entry(vault: Path, case: str) -> None:
    """Plant one entry from :data:`HOSTILE_CASES` in *vault*.

    The synchronicity folder is created if it does not already exist, so
    this works both on a scaffolded vault and on a bare ``tmp_path`` used
    directly as a vault root.

    Args:
        vault: Vault root. The entry lands in
            ``10-Liminal/Synchronicities/`` beneath it.
        case: One of :data:`HOSTILE_CASES`.

    Raises:
        ValueError: If *case* is not one of :data:`HOSTILE_CASES`.
    """
    sync_dir = vault / "10-Liminal" / "Synchronicities"
    sync_dir.mkdir(parents=True, exist_ok=True)
    if case == "hand-broken-yaml":
        (sync_dir / "hostile-yaml.md").write_text(HAND_BROKEN_YAML, encoding="utf-8")
    elif case == "non-string-key":
        (sync_dir / "hostile-key.md").write_text(NON_STRING_KEY, encoding="utf-8")
    elif case == "directory-named-md":
        (sync_dir / "not-a-file.md").mkdir()
    else:
        msg = f"unknown hostile case: {case}"
        raise ValueError(msg)


def scaffold_vault(tmp_path: Path) -> Path:
    """Scaffold a vault with the folders the generators read/write."""
    vault = tmp_path / "vault"
    for d in (
        "00-Creek-Meta",
        "01-Fragments",
        "10-Liminal/Paradoxes",
        "10-Liminal/Synchronicities",
    ):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def sync_notes(vault: Path) -> list[Path]:
    """Return the synchronicity notes currently on disk, sorted."""
    return sorted((vault / "10-Liminal" / "Synchronicities").glob("*.md"))


def seed_synchronicity_vault(tmp_path: Path) -> tuple[Path, EmbeddingsConfig]:
    """A vault with a cross-source pair + a crafted identical-embedding cache."""
    vault = scaffold_vault(tmp_path)
    pairs = (
        ("frag-synx-aaaa", SourcePlatform.DISCORD, datetime(2025, 1, 5, tzinfo=UTC)),
        ("frag-synx-bbbb", SourcePlatform.JOURNAL, datetime(2025, 4, 20, tzinfo=UTC)),
    )
    for fid, platform, created in pairs:
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=fid,
                title="the river remembers every stone it has touched",
                source=FragmentSource(platform=platform),
                created=created,
                authored_at=created,  # the gap filter reads effective_authored_at
            ),
            body="a near-identical meaning arriving from a different source",
        )
    # Identical vectors → cosine 1.0 > the 0.9 synchronicity threshold.
    config = EmbeddingsConfig()
    now = datetime.now(tz=UTC)
    entries = {
        fid: CachedEmbedding(
            fragment_id=fid,
            content_hash="h",
            model_name=config.model,
            vector=[1.0, 0.0, 0.0, 0.0],
            computed_at=now,
        )
        for fid, _platform, _created in pairs
    }
    EmbeddingLinker(config).save_cache(entries, embeddings_cache_path(vault))
    return vault, config
