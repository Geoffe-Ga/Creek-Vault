"""Tests for creek.consent — ConsentManager for first-time source processing.

Covers consent checking, recording, consent log persistence, file summary
generation, glob-pattern exclusions, and integration with the consent log
JSON format.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from creek.consent import (
    ConsentLog,
    ConsentLogUnavailableError,
    ConsentManager,
    ConsentRecord,
    SourceSummary,
    _matches_any_glob,
    build_source_summary,
)

LA_TZ = ZoneInfo("America/Los_Angeles")

requires_mode_bits = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="root and Windows ignore mode bits, so an unreadable path stays readable",
)
"""Skip marker for tests whose provocation is a chmod that root would ignore."""


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


# ---- build_source_summary Tests ----


class TestBuildSourceSummary:
    """Tests for the build_source_summary helper."""

    def test_counts_files(self, source_dir: Path) -> None:
        """Should count all files in the source directory."""
        summary = build_source_summary(source_dir, exclusions=[])
        assert summary.file_count == 5

    def test_excludes_files(self, source_dir: Path) -> None:
        """Should exclude files matching glob patterns."""
        summary = build_source_summary(source_dir, exclusions=["*.log"])
        assert summary.file_count == 4

    def test_calculates_total_size(self, source_dir: Path) -> None:
        """Should calculate total file size in bytes."""
        summary = build_source_summary(source_dir, exclusions=[])
        assert summary.total_size_bytes > 0

    def test_collects_content_types(self, source_dir: Path) -> None:
        """Should collect file extension counts."""
        summary = build_source_summary(source_dir, exclusions=[])
        assert ".txt" in summary.content_types
        assert ".md" in summary.content_types

    def test_limits_sample_filenames(self, source_dir: Path) -> None:
        """Should include at most 10 sample filenames."""
        summary = build_source_summary(source_dir, exclusions=[])
        assert len(summary.sample_filenames) <= 10

    def test_sample_filenames_are_strings(self, source_dir: Path) -> None:
        """Sample filenames should be strings."""
        summary = build_source_summary(source_dir, exclusions=[])
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


# ---- Consent log corruption / unavailability tests ----


def _corrupt_siblings(consent_dir: Path) -> list[Path]:
    """Return every quarantined consent log in *consent_dir*.

    Args:
        consent_dir: The Processing-Log directory holding the consent log.

    Returns:
        Sorted list of ``consent-log.json.corrupt-*`` paths.
    """
    return sorted(consent_dir.glob("consent-log.json.corrupt-*"))


def _tmp_residue(consent_dir: Path) -> list[str]:
    """Return the names of any leftover ``.tmp`` files in *consent_dir*.

    Args:
        consent_dir: The Processing-Log directory holding the consent log.

    Returns:
        Sorted names of every entry whose name ends in ``.tmp``.
    """
    return sorted(p.name for p in consent_dir.iterdir() if p.name.endswith(".tmp"))


class TestConsentLogRecovery:
    """The consent log is append-only: no failure may destroy prior grants.

    Every case here pins one way the old ``_load_log``/``_save_log`` pair
    could lose or misreport recorded consent — swallowing a torn file and
    overwriting it, reporting an unreadable log as "no consent recorded",
    or leaving a half-written file behind.
    """

    def test_truncated_log_is_quarantined_before_a_fresh_log_is_written(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """A torn log is preserved on disk; the fresh log never buries it.

        This is the whole defect in one test: recording a new grant over
        an unparsable log used to silently overwrite it, taking every
        previously recorded source path with it.
        """
        manager.record_consent(
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        log_path = consent_dir / "consent-log.json"
        torn = log_path.read_bytes()[:-5]
        log_path.write_bytes(torn)

        manager.record_consent(
            source_type="chatgpt",
            source_path="/data/chatgpt",
            file_count=5,
            exclusions=[],
            operator="user",
        )

        quarantined = _corrupt_siblings(consent_dir)
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == torn
        assert b"/data/claude" in quarantined[0].read_bytes()

        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert [r["source_type"] for r in data["records"]] == ["chatgpt"]

    def test_directory_at_log_path_raises_rather_than_reporting_no_consent(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """An I/O failure surfaces as an error, not as a False consent answer.

        A directory sitting where the log file belongs raises
        ``IsADirectoryError`` from ``read_text``; the old code swallowed
        it and answered "no consent recorded".
        """
        log_path = consent_dir / "consent-log.json"
        log_path.mkdir()

        with pytest.raises(ConsentLogUnavailableError) as excinfo:
            manager.check_consent("claude", "/data/claude")

        assert excinfo.value.path == log_path
        assert str(log_path) in str(excinfo.value)

    @requires_mode_bits
    def test_unreadable_log_raises_instead_of_reporting_no_consent(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """A log that exists but cannot be read must not read as "no consent".

        Answering ``False`` here re-prompts (or, under ``--yes``,
        re-records) for a source the operator already consented to,
        and the rewrite then destroys the unreadable record.
        """
        manager.record_consent(
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        log_path = consent_dir / "consent-log.json"
        log_path.chmod(0o000)
        try:
            with pytest.raises(ConsentLogUnavailableError):
                manager.check_consent("claude", "/data/claude")
        finally:
            log_path.chmod(0o600)

    @requires_mode_bits
    def test_unreadable_parent_directory_raises_through_the_same_contract(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """An unsearchable parent is an I/O failure, not a bare traceback.

        The old ``exists()`` guard sat outside the ``try`` and let
        ``PermissionError`` escape ``check_consent`` untyped.
        """
        manager.record_consent(
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )
        consent_dir.chmod(0o600)
        try:
            with pytest.raises(ConsentLogUnavailableError):
                manager.check_consent("claude", "/data/claude")
        finally:
            consent_dir.chmod(0o700)

    def test_non_utf8_bytes_are_quarantined_as_corruption_not_propagated(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """Undecodable bytes are corruption: quarantine and carry on."""
        log_path = consent_dir / "consent-log.json"
        payload = b"\xff\xfe\x00bad"
        log_path.write_bytes(payload)

        assert manager.check_consent("claude", "/data/claude") is False

        quarantined = _corrupt_siblings(consent_dir)
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == payload

    def test_empty_log_file_is_quarantined_under_the_same_rule(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """A zero-byte log is a torn write, not an empty consent history."""
        log_path = consent_dir / "consent-log.json"
        log_path.write_bytes(b"")

        assert manager.check_consent("claude", "/data/claude") is False

        quarantined = _corrupt_siblings(consent_dir)
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == b""

    def test_two_quarantines_in_the_same_second_do_not_collide(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """Back-to-back quarantines keep both payloads and touch no older one.

        A second-resolution timestamp alone would collide; the reserved
        random suffix is what keeps each corrupt file recoverable.
        """
        log_path = consent_dir / "consent-log.json"
        preexisting = consent_dir / "consent-log.json.corrupt-19700101T000000-aaaa"
        preexisting_bytes = b'{"records": [ truncated long ago'
        preexisting.write_bytes(preexisting_bytes)

        first_payload = b'{"records": 1'
        log_path.write_bytes(first_payload)
        assert manager.check_consent("claude", "/data/claude") is False

        second_payload = b"not json at all"
        log_path.write_bytes(second_payload)
        assert manager.check_consent("claude", "/data/claude") is False

        quarantined = _corrupt_siblings(consent_dir)
        assert len(quarantined) == 3

        fresh = [p for p in quarantined if p != preexisting]
        assert len({p.name for p in fresh}) == 2
        assert sorted(p.read_bytes() for p in fresh) == sorted(
            [first_payload, second_payload],
        )
        assert preexisting.read_bytes() == preexisting_bytes

    def test_failed_quarantine_never_lets_a_fresh_log_clobber_the_corrupt_bytes(
        self,
        manager: ConsentManager,
        consent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the corrupt log cannot be moved aside, refuse rather than overwrite.

        Quarantine is the only thing standing between a torn log and a
        fresh one written over it, so a failed rename has to abort the
        write — and clean up its own reserved placeholder.
        """
        log_path = consent_dir / "consent-log.json"
        payload = b'{"records": 1'
        log_path.write_bytes(payload)

        def _refuse(*_args: object, **_kwargs: object) -> None:
            """Stand in for ``os.replace`` on a filesystem that refuses it."""
            raise OSError("rename refused")

        monkeypatch.setattr("creek.consent.os.replace", _refuse)

        with pytest.raises(ConsentLogUnavailableError):
            manager.record_consent(
                source_type="chatgpt",
                source_path="/data/chatgpt",
                file_count=5,
                exclusions=[],
                operator="user",
            )

        assert log_path.read_bytes() == payload
        assert _corrupt_siblings(consent_dir) == []

    @requires_mode_bits
    def test_quarantine_failure_on_a_read_only_directory_preserves_the_corrupt_bytes(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """A read-only log directory blocks quarantine, so nothing is rewritten.

        Mode ``0o500`` still permits the read that discovers the
        corruption, which is exactly the window where an overwrite
        would be silent.
        """
        log_path = consent_dir / "consent-log.json"
        payload = b'{"records": 1'
        log_path.write_bytes(payload)

        consent_dir.chmod(0o500)
        try:
            with pytest.raises(ConsentLogUnavailableError):
                manager.check_consent("claude", "/data/claude")
        finally:
            consent_dir.chmod(0o700)

        assert log_path.read_bytes() == payload

    def test_missing_log_is_a_first_run_not_a_corruption(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """No log at all is the first run: answer False and quarantine nothing."""
        assert not (consent_dir / "consent-log.json").exists()

        assert manager.check_consent("claude", "/data/claude") is False

        assert _corrupt_siblings(consent_dir) == []

    def test_save_log_failure_leaves_the_previous_log_intact(
        self,
        manager: ConsentManager,
        consent_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A write that dies mid-flight must not truncate the recorded grants.

        Asserts the specific ``ConsentLogUnavailableError`` rather than
        its ``OSError`` ancestor: a failed *save* must reach the caller
        under the same typed contract as a failed *load*, so the CLI's
        single handler covers both. Matching on the base class would
        still pass if ``_save_log`` stopped wrapping and let the raw
        ``OSError`` through.
        """
        manager.record_consent(
            source_type="claude",
            source_path="/data/claude",
            file_count=10,
            exclusions=[],
            operator="user",
        )

        def _refuse(*_args: object, **_kwargs: object) -> None:
            """Stand in for ``os.replace`` failing after the temp write."""
            raise OSError("rename refused")

        monkeypatch.setattr("creek._fsio.os.replace", _refuse)

        with pytest.raises(ConsentLogUnavailableError, match="rename refused"):
            manager.record_consent(
                source_type="chatgpt",
                source_path="/data/chatgpt",
                file_count=5,
                exclusions=[],
                operator="user",
            )

        log_path = consent_dir / "consent-log.json"
        log = ConsentLog.model_validate_json(log_path.read_text(encoding="utf-8"))
        assert [r.source_type for r in log.records] == ["claude"]
        assert _tmp_residue(consent_dir) == []

    def test_valid_log_is_never_quarantined(
        self, manager: ConsentManager, consent_dir: Path
    ) -> None:
        """The happy path keeps appending: no quarantine, both records kept."""
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
            exclusions=[],
            operator="user",
        )

        assert _corrupt_siblings(consent_dir) == []
        log_path = consent_dir / "consent-log.json"
        log = ConsentLog.model_validate_json(log_path.read_text(encoding="utf-8"))
        assert [r.source_type for r in log.records] == ["claude", "chatgpt"]
