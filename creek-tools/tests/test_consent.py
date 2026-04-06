"""Tests for creek.consent — ConsentManager for first-time source processing.

Covers consent checking, recording, consent log persistence, file summary
generation, glob-pattern exclusions, and integration with the consent log
JSON format.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from creek.consent import (
    ConsentLog,
    ConsentManager,
    ConsentRecord,
    SourceSummary,
    _build_source_summary,
    _matches_any_glob,
)

LA_TZ = ZoneInfo("America/Los_Angeles")


# ---- Fixtures ----


@pytest.fixture()
def consent_dir(tmp_path: Path) -> Path:
    """Create a consent log directory structure.

    Returns:
        Path to the 00-Creek-Meta/Processing-Log/ directory.
    """
    log_dir = tmp_path / "00-Creek-Meta" / "Processing-Log"
    log_dir.mkdir(parents=True)
    return log_dir


@pytest.fixture()
def manager(consent_dir: Path) -> ConsentManager:
    """Create a ConsentManager with a test consent log directory."""
    return ConsentManager(log_dir=consent_dir)


@pytest.fixture()
def source_dir(tmp_path: Path) -> Path:
    """Create a source directory with mixed files for testing.

    Contains:
    - notes.txt (small text file)
    - report.md (markdown file)
    - data.csv (CSV file)
    - debug.log (log file to be excluded)
    - sub/nested.txt (nested file)
    """
    (tmp_path / "source").mkdir()
    src = tmp_path / "source"
    (src / "notes.txt").write_text("Some notes.", encoding="utf-8")
    (src / "report.md").write_text("# Report\n\nContent.", encoding="utf-8")
    (src / "data.csv").write_text("a,b,c\n1,2,3", encoding="utf-8")
    (src / "debug.log").write_text("DEBUG: some log", encoding="utf-8")
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("Nested content.", encoding="utf-8")
    return src


# ---- ConsentRecord Model Tests ----


class TestConsentRecord:
    """Tests for the ConsentRecord Pydantic model."""

    def test_creation_with_required_fields(self) -> None:
        """ConsentRecord should be creatable with all required fields."""
        record = ConsentRecord(
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
            source_type="claude",
            source_path="/data/claude-exports",
            file_count=42,
            exclusions=[],
            operator="user",
        )
        assert record.source_type == "claude"
        assert record.file_count == 42

    def test_serialization_roundtrip(self) -> None:
        """ConsentRecord should serialize to JSON and back."""
        record = ConsentRecord(
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
            source_type="chatgpt",
            source_path="/data/chatgpt",
            file_count=10,
            exclusions=["*.log"],
            operator="admin",
        )
        data = record.model_dump(mode="json")
        restored = ConsentRecord.model_validate(data)
        assert restored.source_type == record.source_type
        assert restored.exclusions == ["*.log"]


# ---- ConsentLog Model Tests ----


class TestConsentLog:
    """Tests for the ConsentLog Pydantic model."""

    def test_empty_log(self) -> None:
        """ConsentLog should be creatable with default empty records."""
        log = ConsentLog()
        assert log.records == []

    def test_log_with_records(self) -> None:
        """ConsentLog should accept a list of ConsentRecords."""
        record = ConsentRecord(
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            source_type="claude",
            source_path="/data",
            file_count=5,
            exclusions=[],
            operator="user",
        )
        log = ConsentLog(records=[record])
        assert len(log.records) == 1


# ---- SourceSummary Model Tests ----


class TestSourceSummary:
    """Tests for the SourceSummary Pydantic model."""

    def test_creation(self) -> None:
        """SourceSummary should be creatable with all fields."""
        summary = SourceSummary(
            file_count=10,
            total_size_bytes=4096,
            content_types={".txt": 5, ".md": 5},
            sample_filenames=["a.txt", "b.md"],
        )
        assert summary.file_count == 10
        assert summary.total_size_bytes == 4096


# ---- _matches_any_glob Tests ----


class TestMatchesAnyGlob:
    """Tests for the _matches_any_glob helper."""

    def test_matches_extension_glob(self) -> None:
        """Should match a file against an extension glob pattern."""
        assert _matches_any_glob(Path("debug.log"), ["*.log"]) is True

    def test_no_match(self) -> None:
        """Should return False when no patterns match."""
        assert _matches_any_glob(Path("notes.txt"), ["*.log"]) is False

    def test_empty_patterns(self) -> None:
        """Should return False with empty pattern list."""
        assert _matches_any_glob(Path("notes.txt"), []) is False

    def test_multiple_patterns(self) -> None:
        """Should match against any of multiple patterns."""
        assert _matches_any_glob(Path("data.csv"), ["*.log", "*.csv"]) is True


# ---- _build_source_summary Tests ----


class TestBuildSourceSummary:
    """Tests for the _build_source_summary helper."""

    def test_counts_files(self, source_dir: Path) -> None:
        """Should count all files in the source directory."""
        summary = _build_source_summary(source_dir, exclusions=[])
        assert summary.file_count == 5

    def test_excludes_files(self, source_dir: Path) -> None:
        """Should exclude files matching glob patterns."""
        summary = _build_source_summary(source_dir, exclusions=["*.log"])
        assert summary.file_count == 4

    def test_calculates_total_size(self, source_dir: Path) -> None:
        """Should calculate total file size in bytes."""
        summary = _build_source_summary(source_dir, exclusions=[])
        assert summary.total_size_bytes > 0

    def test_collects_content_types(self, source_dir: Path) -> None:
        """Should collect file extension counts."""
        summary = _build_source_summary(source_dir, exclusions=[])
        assert ".txt" in summary.content_types
        assert ".md" in summary.content_types

    def test_limits_sample_filenames(self, source_dir: Path) -> None:
        """Should include at most 10 sample filenames."""
        summary = _build_source_summary(source_dir, exclusions=[])
        assert len(summary.sample_filenames) <= 10

    def test_sample_filenames_are_strings(self, source_dir: Path) -> None:
        """Sample filenames should be strings."""
        summary = _build_source_summary(source_dir, exclusions=[])
        assert all(isinstance(f, str) for f in summary.sample_filenames)


# ---- ConsentManager.check_consent Tests ----


class TestConsentManagerCheckConsent:
    """Tests for ConsentManager.check_consent method."""

    def test_no_prior_consent(self, manager: ConsentManager) -> None:
        """Should return False when no consent has been recorded."""
        assert manager.check_consent("claude", "/data/claude") is False

    def test_has_prior_consent(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """Should return True when consent has been previously recorded."""
        record = ConsentRecord(
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        log = ConsentLog(records=[record])
        log_path = consent_dir / "consent-log.json"
        log_path.write_text(log.model_dump_json(indent=2), encoding="utf-8")
        assert manager.check_consent("claude", "/data/claude") is True

    def test_consent_for_different_source(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """Should return False for a different source type."""
        record = ConsentRecord(
            timestamp=datetime(2024, 1, 15, tzinfo=LA_TZ),
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        log = ConsentLog(records=[record])
        log_path = consent_dir / "consent-log.json"
        log_path.write_text(log.model_dump_json(indent=2), encoding="utf-8")
        assert manager.check_consent("chatgpt", "/data/chatgpt") is False


# ---- ConsentManager.record_consent Tests ----


class TestConsentManagerRecordConsent:
    """Tests for ConsentManager.record_consent method."""

    def test_records_consent(self, manager: ConsentManager) -> None:
        """Should create a consent log entry."""
        manager.record_consent(
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        assert manager.check_consent("claude", "/data/claude") is True

    def test_appends_to_existing_log(self, manager: ConsentManager) -> None:
        """Should append to an existing consent log, not overwrite."""
        manager.record_consent(
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        manager.record_consent(
            source_type="chatgpt",
            source_path="/data/chatgpt",
            file_count=5,
            exclusions=["*.log"],
            operator="admin",
        )
        assert manager.check_consent("claude", "/data/claude") is True
        assert manager.check_consent("chatgpt", "/data/chatgpt") is True

    def test_log_file_is_valid_json(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """The consent log file should be valid JSON."""
        manager.record_consent(
            source_type="test",
            source_path="/data/test",
            file_count=1,
            exclusions=[],
            operator="user",
        )
        log_path = consent_dir / "consent-log.json"
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert "records" in data
        assert len(data["records"]) == 1

    def test_record_stores_exclusions(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """Exclusion patterns should be stored in the consent record."""
        manager.record_consent(
            source_type="test",
            source_path="/data/test",
            file_count=1,
            exclusions=["*.log", "*.tmp"],
            operator="user",
        )
        log_path = consent_dir / "consent-log.json"
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert data["records"][0]["exclusions"] == ["*.log", "*.tmp"]

    def test_record_stores_operator(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """Operator name should be stored in the consent record."""
        manager.record_consent(
            source_type="test",
            source_path="/data/test",
            file_count=1,
            exclusions=[],
            operator="admin_user",
        )
        log_path = consent_dir / "consent-log.json"
        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert data["records"][0]["operator"] == "admin_user"


# ---- ConsentManager.get_source_summary Tests ----


class TestConsentManagerGetSourceSummary:
    """Tests for ConsentManager.get_source_summary method."""

    def test_returns_source_summary(
        self, manager: ConsentManager, source_dir: Path
    ) -> None:
        """Should return a SourceSummary for the source directory."""
        summary = manager.get_source_summary(source_dir, exclusions=[])
        assert isinstance(summary, SourceSummary)
        assert summary.file_count > 0

    def test_applies_exclusions(
        self, manager: ConsentManager, source_dir: Path
    ) -> None:
        """Should exclude files matching exclusion patterns."""
        full = manager.get_source_summary(source_dir, exclusions=[])
        filtered = manager.get_source_summary(source_dir, exclusions=["*.log"])
        assert filtered.file_count < full.file_count
