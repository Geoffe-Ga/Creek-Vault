"""The runnable Liminal generators (#711): paradox + synchronicity note-writers.

Both detectors were implemented but never invoked in production, so
``10-Liminal/Paradoxes`` and ``10-Liminal/Synchronicities`` stayed empty. These
tests prove the new ``generate_paradoxes`` / ``generate_synchronicities`` wiring
writes notes, is idempotent, and degrades to an empty result without crashing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from creek.config import EmbeddingsConfig
from creek.generate.paradox import generate_paradoxes
from creek.generate.synchronicity import generate_synchronicities
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    embeddings_cache_path,
)
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    SourcePlatform,
    VoiceClassification,
)
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path


def _vault(tmp_path: Path) -> Path:
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


def _paradox_notes(vault: Path) -> list[Path]:
    return sorted((vault / "10-Liminal" / "Paradoxes").glob("*.md"))


def _sync_notes(vault: Path) -> list[Path]:
    return sorted((vault / "10-Liminal" / "Synchronicities").glob("*.md"))


class TestGenerateParadoxes:
    """`generate_paradoxes` wires ParadoxDetector to a runnable result."""

    def test_writes_note_for_confidence_contradiction(self, tmp_path: Path) -> None:
        """Two fragments on a shared thread with opposite confidence → a note."""
        vault = _vault(tmp_path)
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
        vault = _vault(tmp_path)
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
        vault = _vault(tmp_path)
        assert generate_paradoxes(vault, EmbeddingsConfig()) == []


def _seed_synchronicity_vault(tmp_path: Path) -> tuple[Path, EmbeddingsConfig]:
    """A vault with a cross-source pair + a crafted identical-embedding cache."""
    vault = _vault(tmp_path)
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


class TestGenerateSynchronicities:
    """`generate_synchronicities` wires SynchronicityDetector + embeddings."""

    def test_writes_note_for_cross_source_resonance(self, tmp_path: Path) -> None:
        """Cross-source, >30-day, identical-embedding pair → a synchronicity note."""
        vault, config = _seed_synchronicity_vault(tmp_path)
        written = generate_synchronicities(vault, config)
        assert len(written) == 1
        assert len(_sync_notes(vault)) == 1

    def test_idempotent(self, tmp_path: Path) -> None:
        """Re-running writes the same {sync.id}.md in place (no duplicate)."""
        vault, config = _seed_synchronicity_vault(tmp_path)
        generate_synchronicities(vault, config)
        generate_synchronicities(vault, config)
        assert len(_sync_notes(vault)) == 1  # stable path → overwrite

    def test_no_embeddings_cache_no_notes(self, tmp_path: Path) -> None:
        """With no embeddings cache there are no resonances, so no notes."""
        vault = _vault(tmp_path)
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

        ``_existing_synchronicity_pairs`` reads each note via
        ``read_text(encoding="utf-8")``; an invalid-UTF-8 file raises
        ``UnicodeDecodeError`` (a ``ValueError`` subclass), which the scan's
        ``except (OSError, ValueError)`` swallows so one corrupt note cannot
        abort the idempotency bookkeeping.
        """
        from creek.generate.synchronicity import _existing_synchronicity_pairs

        sync_dir = tmp_path / "10-Liminal" / "Synchronicities"
        sync_dir.mkdir(parents=True)
        (sync_dir / "corrupt.md").write_bytes(b"\xff\xfe not valid utf-8")

        assert _existing_synchronicity_pairs(tmp_path) == set()
