"""The runnable Liminal generators (#711): paradox + synchronicity note-writers.

Both detectors were implemented but never invoked in production, so
``10-Liminal/Paradoxes`` and ``10-Liminal/Synchronicities`` stayed empty. These
tests prove the new ``generate_paradoxes`` / ``generate_synchronicities`` wiring
writes notes, is idempotent, and degrades to an empty result without crashing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.config import EmbeddingsConfig
from creek.generate.paradox import generate_paradoxes
from creek.generate.synchronicity import generate_synchronicities
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    SourcePlatform,
    VoiceClassification,
)
from tests.helpers import write_fragment_file
from tests.synchronicity_support import (
    HOSTILE_CASES,
    plant_hostile_entry,
    scaffold_vault,
    seed_synchronicity_vault,
    sync_notes,
)

if TYPE_CHECKING:
    from pathlib import Path


def _paradox_notes(vault: Path) -> list[Path]:
    return sorted((vault / "10-Liminal" / "Paradoxes").glob("*.md"))


class TestGenerateParadoxes:
    """`generate_paradoxes` wires ParadoxDetector to a runnable result."""

    def test_writes_note_for_confidence_contradiction(self, tmp_path: Path) -> None:
        """Two fragments on a shared thread with opposite confidence → a note."""
        vault = scaffold_vault(tmp_path)
        for fid, conf in (
            ("frag-parax-aaaa", Confidence.MUSING),
            ("frag-parax-bbbb", Confidence.SETTLED),
        ):
            write_fragment_file(
                vault=vault,
                fragment=Fragment(
                    id=fid,
                    title="career ambitions",
                    source=FragmentSource(platform=SourcePlatform.JOURNAL),
                    voice=VoiceClassification(confidence=conf),
                    threads=["thread-career"],
                ),
                body="a reflection on the same thread, settled differently",
            )

        written = generate_paradoxes(vault, EmbeddingsConfig())

        assert len(written) == 1
        assert len(_paradox_notes(vault)) == 1

    def test_idempotent(self, tmp_path: Path) -> None:
        """Re-running records nothing new (#1320).

        This assertion used to read "writes the same note in place", which was
        only ever true within a single calendar day — the note path embeds
        ``detected_date``. The cross-day case, and the read-back scan that now
        supplies the identity, live in ``tests/test_paradox_idempotency.py``.
        """
        vault = scaffold_vault(tmp_path)
        for fid, conf in (
            ("frag-parax-cccc", Confidence.MUSING),
            ("frag-parax-dddd", Confidence.SETTLED),
        ):
            write_fragment_file(
                vault=vault,
                fragment=Fragment(
                    id=fid,
                    title="career ambitions",
                    source=FragmentSource(platform=SourcePlatform.JOURNAL),
                    voice=VoiceClassification(confidence=conf),
                    threads=["thread-career"],
                ),
                body="body",
            )
        first = generate_paradoxes(vault, EmbeddingsConfig())
        second = generate_paradoxes(vault, EmbeddingsConfig())
        assert len(first) == 1
        assert second == []  # the pair is already recorded → skipped, not rewritten
        assert len(_paradox_notes(vault)) == 1

    def test_empty_vault_no_notes(self, tmp_path: Path) -> None:
        """A vault with fewer than two fragments yields no paradoxes."""
        vault = scaffold_vault(tmp_path)
        assert generate_paradoxes(vault, EmbeddingsConfig()) == []


class TestGenerateSynchronicities:
    """`generate_synchronicities` wires SynchronicityDetector + embeddings."""

    def test_writes_note_for_cross_source_resonance(self, tmp_path: Path) -> None:
        """Cross-source, >30-day, identical-embedding pair → a synchronicity note."""
        vault, config = seed_synchronicity_vault(tmp_path)
        written = generate_synchronicities(vault, config)
        assert len(written) == 1
        assert len(sync_notes(vault)) == 1

    def test_idempotent(self, tmp_path: Path) -> None:
        """Re-running writes the same {sync.id}.md in place (no duplicate)."""
        vault, config = seed_synchronicity_vault(tmp_path)
        generate_synchronicities(vault, config)
        generate_synchronicities(vault, config)
        assert len(sync_notes(vault)) == 1  # stable path → overwrite

    def test_no_embeddings_cache_no_notes(self, tmp_path: Path) -> None:
        """With no embeddings cache there are no resonances, so no notes."""
        vault = scaffold_vault(tmp_path)
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id="frag-synx-cccc",
                title="a lone fragment",
                source=FragmentSource(platform=SourcePlatform.JOURNAL),
            ),
            body="body",
        )
        assert generate_synchronicities(vault, EmbeddingsConfig()) == []

    def test_existing_pairs_skips_unreadable_note(self, tmp_path: Path) -> None:
        """An undecodable note is skipped by the dedup scan, not raised (#726).

        The outcome is unchanged since #726; the mechanism is not.
        ``_existing_synchronicity_pairs`` now reads headers through
        ``creek.vault.links.read_header_meta``, which opens with
        ``errors="replace"`` — so these bytes no longer raise
        ``UnicodeDecodeError`` at all. They decode to replacement
        characters, the first line is therefore not ``---``, and the
        header-only reader returns ``{}``: the note contributes no pair
        and one corrupt file still cannot abort the idempotency
        bookkeeping.
        """
        from creek.generate.synchronicity import _existing_synchronicity_pairs

        sync_dir = tmp_path / "10-Liminal" / "Synchronicities"
        sync_dir.mkdir(parents=True)
        (sync_dir / "corrupt.md").write_bytes(b"\xff\xfe not valid utf-8")

        assert _existing_synchronicity_pairs(tmp_path) == set()


class TestSynchronicityReadBackScanRobustness:
    """``10-Liminal/Synchronicities/`` is operator-editable (#1416).

    Obsidian, a text editor and ``creek save`` all write into this folder,
    so the dedup read-back scan meets YAML nobody validated. One bad note
    must cost itself its dedup entry and nothing else: the run still writes
    the note the vault has earned.

    Red-before-green applies to ``hand-broken-yaml`` (``yaml.parser.ParserError``,
    not a ``ValueError``) and ``non-string-key`` (``TypeError: keywords must be
    strings``, raised by ``Post(content, handler, **metadata)`` past any except
    tuple). ``directory-named-md`` is a parity lock, not a red-before-green case —
    ``read_text``/``open`` on a directory already raised ``OSError``, which both
    the old tuple and ``read_header_meta`` skip.
    """

    @pytest.mark.parametrize("case", HOSTILE_CASES)
    def test_hostile_entry_is_skipped_and_the_run_still_writes(
        self,
        tmp_path: Path,
        case: str,
    ) -> None:
        """Each hostile entry costs itself, not the run.

        The seeded vault earns exactly one synchronicity, so ``len(written)
        == 1`` is the assertion that separates "survived" from "crashed" —
        and, because the hostile entry carries no usable pair, it is also
        the assertion that separates "skipped the bad note" from "swallowed
        the whole scan". The written path is checked to be a real file so a
        directory entry can never satisfy it.

        Args:
            tmp_path: pytest's per-test temporary directory.
            case: The hostile entry to plant, from :data:`HOSTILE_CASES`.
        """
        vault, config = seed_synchronicity_vault(tmp_path)
        plant_hostile_entry(vault, case)

        written = generate_synchronicities(vault, config)

        assert len(written) == 1
        assert written[0].is_file()
        # ``glob("*.md")`` matches the directory-named entry too, so the
        # on-disk check filters to real files rather than counting entries:
        # the note this run reported is genuinely in the folder.
        assert written[0] in [p for p in sync_notes(vault) if p.is_file()]
