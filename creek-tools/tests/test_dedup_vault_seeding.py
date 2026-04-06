"""Tests for vault-seeding deduplication — cross-run persistence.

Tests cover:
- seed_from_vault: loading a dedup manifest from vault
- save_manifest: persisting indexes to vault manifest
- Round-trip: save then seed recovers state
- Cross-run deduplication: fragments survive process restarts
- Performance: 10k+ fragments load efficiently
- Edge cases: missing manifest, empty manifest, corrupt manifest, missing dirs
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from creek.clean.dedup import Deduplicator

# Manifest path constant (mirrors implementation)
_MANIFEST_RELATIVE = Path("00-Creek-Meta") / "dedup-manifest.json"


def _create_vault_structure(tmp_path: Path) -> Path:
    """Create a minimal vault directory structure for testing.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path to the vault root.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "00-Creek-Meta").mkdir()
    (vault / "01-Fragments").mkdir()
    return vault


def _write_manifest(vault: Path, data: dict[str, object]) -> Path:
    """Write a manifest JSON file to the vault.

    Args:
        vault: Path to vault root.
        data: Manifest data to write.

    Returns:
        Path to the written manifest file.
    """
    manifest_path = vault / _MANIFEST_RELATIVE
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# save_manifest
# ---------------------------------------------------------------------------


class TestSaveManifest:
    """Tests for Deduplicator.save_manifest."""

    def test_save_creates_manifest_file(self, tmp_path: Path) -> None:
        """save_manifest should create the manifest JSON file."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        dedup.register(
            source="a.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello world",
        )
        dedup.save_manifest(vault)
        assert (vault / _MANIFEST_RELATIVE).exists()

    def test_save_manifest_contains_version(self, tmp_path: Path) -> None:
        """Manifest should contain a version field."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        dedup.save_manifest(vault)
        data = json.loads((vault / _MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        assert data["version"] == 1

    def test_save_manifest_contains_indexes(self, tmp_path: Path) -> None:
        """Manifest should contain exact_index and normalized_index."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        dedup.register(
            source="a.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello world",
        )
        dedup.save_manifest(vault)
        data = json.loads((vault / _MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        assert "exact_index" in data
        assert "normalized_index" in data
        assert len(data["exact_index"]) == 1
        assert len(data["normalized_index"]) == 1

    def test_save_empty_deduplicator(self, tmp_path: Path) -> None:
        """Saving an empty deduplicator should write empty indexes."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        dedup.save_manifest(vault)
        data = json.loads((vault / _MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        assert data["exact_index"] == {}
        assert data["normalized_index"] == {}

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        """save_manifest should create parent dirs if needed."""
        vault = tmp_path / "vault"
        vault.mkdir()
        # Don't create 00-Creek-Meta — save_manifest should handle it
        dedup = Deduplicator()
        dedup.save_manifest(vault)
        assert (vault / _MANIFEST_RELATIVE).exists()

    def test_save_overwrites_existing_manifest(self, tmp_path: Path) -> None:
        """save_manifest should overwrite an existing manifest file."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        dedup.register(
            source="a.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello",
        )
        dedup.save_manifest(vault)

        # Register more and save again
        dedup.register(
            source="b.json",
            timestamp=datetime(2025, 1, 2, tzinfo=UTC),
            content="World",
        )
        dedup.save_manifest(vault)

        data = json.loads((vault / _MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        assert len(data["exact_index"]) == 2


# ---------------------------------------------------------------------------
# seed_from_vault
# ---------------------------------------------------------------------------


class TestSeedFromVault:
    """Tests for Deduplicator.seed_from_vault."""

    def test_seed_returns_count(self, tmp_path: Path) -> None:
        """seed_from_vault should return the number of seeded fragments."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        dedup.register(
            source="a.json",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            content="Hello",
        )
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        count = fresh.seed_from_vault(vault)
        assert count == 1

    def test_seed_populates_exact_index(self, tmp_path: Path) -> None:
        """Seeded deduplicator should detect exact duplicates."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup = Deduplicator()
        dedup.register(source="a.json", timestamp=ts, content="Hello world")
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        fresh.seed_from_vault(vault)
        result = fresh.check(source="a.json", timestamp=ts, content="Hello world")
        assert result.is_duplicate is True
        assert result.match_type == "exact"

    def test_seed_populates_normalized_index(self, tmp_path: Path) -> None:
        """Seeded deduplicator should detect normalized duplicates."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        dedup = Deduplicator()
        dedup.register(source="a.json", timestamp=ts, content="Hello World!")
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        fresh.seed_from_vault(vault)
        # Different source but normalized-equivalent content
        result = fresh.check(source="b.json", timestamp=ts, content="hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_seed_updates_size(self, tmp_path: Path) -> None:
        """After seeding, deduplicator size should reflect seeded entries."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        for i in range(5):
            dedup.register(
                source=f"file{i}.json",
                timestamp=datetime(2025, 1, 1, tzinfo=UTC),
                content=f"Content {i}",
            )
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        fresh.seed_from_vault(vault)
        assert fresh.size == 5

    def test_seed_missing_manifest_returns_zero(self, tmp_path: Path) -> None:
        """seed_from_vault with no manifest should return 0."""
        vault = _create_vault_structure(tmp_path)
        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0
        assert dedup.size == 0

    def test_seed_empty_manifest(self, tmp_path: Path) -> None:
        """seed_from_vault with empty indexes should return 0."""
        vault = _create_vault_structure(tmp_path)
        _write_manifest(
            vault,
            {
                "version": 1,
                "exact_index": {},
                "normalized_index": {},
            },
        )
        dedup = Deduplicator()
        count = dedup.seed_from_vault(vault)
        assert count == 0

    def test_seed_corrupt_manifest_raises(self, tmp_path: Path) -> None:
        """seed_from_vault with invalid JSON should raise ValueError."""
        vault = _create_vault_structure(tmp_path)
        manifest_path = vault / _MANIFEST_RELATIVE
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("not valid json {{{", encoding="utf-8")

        dedup = Deduplicator()
        with pytest.raises(ValueError, match="manifest"):
            dedup.seed_from_vault(vault)

    def test_seed_unsupported_version_raises(self, tmp_path: Path) -> None:
        """seed_from_vault should reject manifests with unknown version."""
        vault = _create_vault_structure(tmp_path)
        _write_manifest(
            vault,
            {
                "version": 999,
                "exact_index": {},
                "normalized_index": {},
            },
        )
        dedup = Deduplicator()
        with pytest.raises(ValueError, match="version"):
            dedup.seed_from_vault(vault)

    def test_seed_merges_with_existing(self, tmp_path: Path) -> None:
        """Seeding should merge with already-registered fragments."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        # Save one fragment to manifest
        original = Deduplicator()
        original.register(source="a.json", timestamp=ts, content="Hello")
        original.save_manifest(vault)

        # Create fresh deduplicator with one different fragment
        fresh = Deduplicator()
        fresh.register(source="b.json", timestamp=ts, content="World")
        fresh.seed_from_vault(vault)

        # Should now have both
        assert fresh.size == 2


# ---------------------------------------------------------------------------
# Round-trip: save -> seed
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Tests for save/seed round-trip consistency."""

    def test_round_trip_preserves_exact_detection(self, tmp_path: Path) -> None:
        """Save then seed should preserve exact duplicate detection."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        source = "notes/journal.md"
        content = "Today I reflected on the nature of creativity."

        dedup = Deduplicator()
        dedup.register(source=source, timestamp=ts, content=content)
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        fresh.seed_from_vault(vault)

        result = fresh.register(source=source, timestamp=ts, content=content)
        assert result.is_duplicate is True
        assert result.match_type == "exact"

    def test_round_trip_preserves_normalized_detection(self, tmp_path: Path) -> None:
        """Save then seed should preserve normalized duplicate detection."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 6, 15, tzinfo=UTC)

        dedup = Deduplicator()
        dedup.register(source="a.json", timestamp=ts, content="Hello, World!")
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        fresh.seed_from_vault(vault)

        result = fresh.register(source="b.json", timestamp=ts, content="hello world")
        assert result.is_duplicate is True
        assert result.match_type == "normalized"

    def test_round_trip_many_fragments(self, tmp_path: Path) -> None:
        """Round-trip should preserve all fragments for 100 entries."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        dedup = Deduplicator()
        for i in range(100):
            dedup.register(
                source=f"file{i}.json",
                timestamp=ts,
                content=f"Unique content {i}",
            )
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        count = fresh.seed_from_vault(vault)
        assert count == 100
        assert fresh.size == 100

        # Verify all are detected as duplicates
        for i in range(100):
            result = fresh.check(
                source=f"file{i}.json",
                timestamp=ts,
                content=f"Unique content {i}",
            )
            assert result.is_duplicate is True

    def test_multiple_save_seed_cycles(self, tmp_path: Path) -> None:
        """Multiple save/seed cycles should accumulate entries."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        # Cycle 1
        dedup1 = Deduplicator()
        dedup1.register(source="a.json", timestamp=ts, content="First")
        dedup1.save_manifest(vault)

        # Cycle 2
        dedup2 = Deduplicator()
        dedup2.seed_from_vault(vault)
        dedup2.register(source="b.json", timestamp=ts, content="Second")
        dedup2.save_manifest(vault)

        # Cycle 3
        dedup3 = Deduplicator()
        count = dedup3.seed_from_vault(vault)
        assert count == 2
        assert dedup3.size == 2


# ---------------------------------------------------------------------------
# Performance: 10k+ fragments
# ---------------------------------------------------------------------------


class TestPerformance:
    """Tests for performance with large fragment counts."""

    def test_seed_10k_fragments(self, tmp_path: Path) -> None:
        """Seeding 10k+ fragments should complete without error."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        dedup = Deduplicator()
        for i in range(10_000):
            dedup.register(
                source=f"file{i}.json",
                timestamp=ts,
                content=f"Content number {i}",
            )
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        count = fresh.seed_from_vault(vault)
        assert count == 10_000
        assert fresh.size == 10_000

    def test_seed_10k_detects_duplicates(self, tmp_path: Path) -> None:
        """After seeding 10k fragments, duplicate detection still works."""
        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        dedup = Deduplicator()
        for i in range(10_000):
            dedup.register(
                source=f"file{i}.json",
                timestamp=ts,
                content=f"Content number {i}",
            )
        dedup.save_manifest(vault)

        fresh = Deduplicator()
        fresh.seed_from_vault(vault)

        # Check a specific entry
        result = fresh.check(
            source="file5000.json",
            timestamp=ts,
            content="Content number 5000",
        )
        assert result.is_duplicate is True
        assert result.match_type == "exact"


# ---------------------------------------------------------------------------
# Integration with Fragment model
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestVaultSeedingIntegration:
    """Integration tests: vault seeding with Fragment models."""

    def test_cross_run_fragment_dedup(self, tmp_path: Path) -> None:
        """Fragments should be detected across process restarts."""
        from creek.models import Fragment, FragmentSource

        vault = _create_vault_structure(tmp_path)
        ts = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
        content = "The key insight is that patterns repeat."

        fragment = Fragment(
            title="Insight note",
            source=FragmentSource(
                platform="journal",
                original_file="notes/insight.md",
            ),
            created=ts,
        )
        source_key = fragment.source.original_file or fragment.source.platform

        # Run 1: register and save
        dedup1 = Deduplicator()
        dedup1.register(source=source_key, timestamp=ts, content=content)
        dedup1.save_manifest(vault)

        # Run 2: seed and check
        dedup2 = Deduplicator()
        dedup2.seed_from_vault(vault)
        result = dedup2.register(source=source_key, timestamp=ts, content=content)
        assert result.is_duplicate is True
        assert result.match_type == "exact"
