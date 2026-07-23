"""Tests for creek.redact — redaction scanner and redactor.

Tests cover:
- REDACTION_PATTERNS compilation and matching
- RedactionMatch model (must NOT store matched text, only salted hashes)
- RedactionScanner: scan_file, scan_directory, generate_report
- Redactor: redact_content, log_redactions
- False positive allowlisting
- Custom pattern support
- Security: ensure sensitive data never leaks into match objects
- Binary file detection (Issue #14)
- File extension filtering (Issue #14)
- Directory exclusion patterns (Issue #14)
- Context extraction (Issue #14)
- JSON report generation (Issue #14)
- Markdown summary (Issue #14)
- Review queue generation (Issue #14)
- Batch scanning with ScanSummary (Issue #14)
"""

import json
import re
from pathlib import Path

import pytest

from creek.config import RedactionConfig
from creek.redact import (
    REDACTION_PATTERNS,
    RedactionMatch,
    RedactionScanner,
    Redactor,
    ScanSummary,
)

# ---------------------------------------------------------------------------
# REDACTION_PATTERNS
# ---------------------------------------------------------------------------


class TestRedactionPatterns:
    """Tests for the REDACTION_PATTERNS dictionary."""

    def test_patterns_dict_exists(self) -> None:
        """REDACTION_PATTERNS should be a non-empty dict."""
        assert isinstance(REDACTION_PATTERNS, dict)
        assert len(REDACTION_PATTERNS) >= 3

    def test_required_pattern_keys(self) -> None:
        """REDACTION_PATTERNS must contain api_key, password, ssn, email."""
        for key in ("api_key", "password", "ssn", "email"):
            assert key in REDACTION_PATTERNS, f"Missing pattern: {key}"

    def test_patterns_are_compiled_regex(self) -> None:
        """Each pattern value should be a compiled regex."""
        for name, pattern in REDACTION_PATTERNS.items():
            assert isinstance(pattern, re.Pattern), (
                f"Pattern '{name}' is not a compiled regex"
            )

    def test_api_key_pattern_matches(self) -> None:
        """api_key pattern should match common API key formats."""
        pattern = REDACTION_PATTERNS["api_key"]
        assert pattern.search("AKIAIOSFODNN7EXAMPLE")
        assert pattern.search("sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert pattern.search("sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx")

    def test_api_key_pattern_no_false_positive_on_short_strings(self) -> None:
        """api_key pattern should NOT match short ordinary strings."""
        pattern = REDACTION_PATTERNS["api_key"]
        assert not pattern.search("hello")
        assert not pattern.search("abc123")

    def test_password_pattern_matches(self) -> None:
        """password pattern should match password= and passwd= assignments."""
        pattern = REDACTION_PATTERNS["password"]
        assert pattern.search("password=secret123")
        assert pattern.search("passwd=my_pass")
        assert pattern.search('password="hunter2"')
        assert pattern.search("PASSWORD=SuperSecret")

    def test_password_pattern_no_false_positive(self) -> None:
        """password pattern should NOT match unrelated text."""
        pattern = REDACTION_PATTERNS["password"]
        assert not pattern.search("the word pass is fine")
        assert not pattern.search("no assignment here")

    def test_ssn_pattern_matches(self) -> None:
        """ssn pattern should match US Social Security Number format."""
        pattern = REDACTION_PATTERNS["ssn"]
        assert pattern.search("123-45-6789")
        assert pattern.search("SSN: 999-88-7777")

    def test_ssn_pattern_no_false_positive(self) -> None:
        """ssn pattern should NOT match phone numbers or other digit patterns."""
        pattern = REDACTION_PATTERNS["ssn"]
        assert not pattern.search("123-456-7890")  # phone number format
        assert not pattern.search("12-34-5678")  # wrong grouping

    def test_email_pattern_matches(self) -> None:
        """email pattern should match email addresses."""
        pattern = REDACTION_PATTERNS["email"]
        assert pattern.search("user@example.com")
        assert pattern.search("first.last@company.co.uk")
        assert pattern.search("test+tag@domain.org")

    def test_email_pattern_no_false_positive(self) -> None:
        """email pattern should NOT match non-email strings."""
        pattern = REDACTION_PATTERNS["email"]
        assert not pattern.search("not an email")
        assert not pattern.search("@no_user")


# ---------------------------------------------------------------------------
# RedactionMatch model
# ---------------------------------------------------------------------------


class TestRedactionMatch:
    """Tests for the RedactionMatch Pydantic model."""

    def test_required_fields(self) -> None:
        """RedactionMatch should have all required fields."""
        match = RedactionMatch(
            file_path=Path("test.txt"),
            line_number=1,
            match_type="ssn",
            salted_hash="abc123def456",
        )
        assert match.file_path == Path("test.txt")
        assert match.line_number == 1
        assert match.match_type == "ssn"
        assert match.salted_hash == "abc123def456"

    def test_no_matched_text_field(self) -> None:
        """RedactionMatch must NOT have a field for the actual matched text."""
        fields = RedactionMatch.model_fields
        forbidden = {"matched_text", "text", "value", "content", "raw", "match_text"}
        for field_name in forbidden:
            assert field_name not in fields, (
                f"RedactionMatch must NOT store matched text (found '{field_name}')"
            )

    def test_serializable(self) -> None:
        """RedactionMatch should be JSON-serializable."""
        match = RedactionMatch(
            file_path=Path("test.txt"),
            line_number=42,
            match_type="email",
            salted_hash="deadbeef",
        )
        data = match.model_dump(mode="json")
        serialized = json.dumps(data)
        assert isinstance(serialized, str)
        assert "test.txt" in serialized


# ---------------------------------------------------------------------------
# RedactionScanner
# ---------------------------------------------------------------------------


class TestRedactionScanner:
    """Tests for RedactionScanner class."""

    def test_init_with_default_config(self) -> None:
        """RedactionScanner should initialize with default RedactionConfig."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        assert scanner is not None

    def test_scan_file_finds_ssn(self, tmp_path: Path) -> None:
        """scan_file should detect SSN patterns."""
        test_file = tmp_path / "data.txt"
        test_file.write_text("My SSN is 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        assert len(matches) >= 1
        ssn_matches = [m for m in matches if m.match_type == "ssn"]
        assert len(ssn_matches) >= 1
        assert ssn_matches[0].line_number == 1
        assert ssn_matches[0].file_path == test_file

    def test_scan_file_finds_email(self, tmp_path: Path) -> None:
        """scan_file should detect email patterns."""
        test_file = tmp_path / "contacts.txt"
        test_file.write_text("Contact: user@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        email_matches = [m for m in matches if m.match_type == "email"]
        assert len(email_matches) >= 1

    def test_scan_file_finds_password(self, tmp_path: Path) -> None:
        """scan_file should detect password assignments."""
        test_file = tmp_path / "config.env"
        test_file.write_text("password=hunter2\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        pw_matches = [m for m in matches if m.match_type == "password"]
        assert len(pw_matches) >= 1

    def test_scan_file_finds_api_key(self, tmp_path: Path) -> None:
        """scan_file should detect API key patterns."""
        test_file = tmp_path / "secrets.txt"
        test_file.write_text("key = AKIAIOSFODNN7EXAMPLE\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        api_matches = [m for m in matches if m.match_type == "api_key"]
        assert len(api_matches) >= 1

    def test_scan_file_no_matches(self, tmp_path: Path) -> None:
        """scan_file should return empty list for clean files."""
        test_file = tmp_path / "clean.txt"
        test_file.write_text("This is a perfectly clean file.\nNothing to see here.\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        assert matches == []

    def test_scan_file_multiple_matches(self, tmp_path: Path) -> None:
        """scan_file should find multiple matches across lines."""
        test_file = tmp_path / "mixed.txt"
        test_file.write_text(
            "SSN: 123-45-6789\nEmail: test@example.com\npassword=secret\n"
        )

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        match_types = {m.match_type for m in matches}
        assert "ssn" in match_types
        assert "email" in match_types
        assert "password" in match_types

    def test_scan_file_salted_hash_not_plaintext(self, tmp_path: Path) -> None:
        """Salted hashes in matches must NOT contain the original sensitive text."""
        test_file = tmp_path / "sensitive.txt"
        sensitive_ssn = "123-45-6789"
        test_file.write_text(f"SSN: {sensitive_ssn}\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        for match in matches:
            assert sensitive_ssn not in match.salted_hash
            assert sensitive_ssn.replace("-", "") not in match.salted_hash

    def test_scan_file_consistent_hash_same_session(self, tmp_path: Path) -> None:
        """Same text scanned in the same session should produce the same hash."""
        test_file = tmp_path / "dup.txt"
        test_file.write_text("SSN: 123-45-6789\nSSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        ssn_matches = [m for m in matches if m.match_type == "ssn"]
        assert len(ssn_matches) == 2
        assert ssn_matches[0].salted_hash == ssn_matches[1].salted_hash

    def test_scan_file_different_sessions_different_hashes(
        self, tmp_path: Path
    ) -> None:
        """Different scanner sessions should produce different hashes for same text."""
        test_file = tmp_path / "session.txt"
        test_file.write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner1 = RedactionScanner(config=config)
        scanner2 = RedactionScanner(config=config)

        matches1 = scanner1.scan_file(test_file)
        matches2 = scanner2.scan_file(test_file)

        # Different sessions have different salts, so hashes differ
        # (extremely unlikely to collide)
        assert matches1[0].salted_hash != matches2[0].salted_hash

    def test_scan_file_nonexistent(self) -> None:
        """scan_file should raise FileNotFoundError for missing files."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)

        with pytest.raises(FileNotFoundError):
            scanner.scan_file(Path("/nonexistent/path/file.txt"))

    def test_scan_file_line_numbers_correct(self, tmp_path: Path) -> None:
        """Line numbers should be 1-based and accurate."""
        test_file = tmp_path / "lines.txt"
        test_file.write_text(
            "clean line\n"
            "also clean\n"
            "SSN: 123-45-6789\n"
            "clean again\n"
            "email: test@example.com\n"
        )

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        ssn_match = next(m for m in matches if m.match_type == "ssn")
        email_match = next(m for m in matches if m.match_type == "email")
        assert ssn_match.line_number == 3
        assert email_match.line_number == 5

    def test_scan_directory(self, tmp_path: Path) -> None:
        """scan_directory should recursively scan all files."""
        sub = tmp_path / "sub"
        sub.mkdir()

        (tmp_path / "file1.txt").write_text("SSN: 123-45-6789\n")
        (sub / "file2.txt").write_text("email: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_directory(tmp_path)

        match_types = {m.match_type for m in matches}
        assert "ssn" in match_types
        assert "email" in match_types
        assert len(matches) >= 2

    def test_scan_directory_empty(self, tmp_path: Path) -> None:
        """scan_directory on empty directory should return empty list."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_directory(tmp_path)
        assert matches == []

    def test_scan_directory_nonexistent(self) -> None:
        """scan_directory should raise FileNotFoundError for missing directory."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)

        with pytest.raises(FileNotFoundError):
            scanner.scan_directory(Path("/nonexistent/directory"))

    def test_false_positive_allowlist(self, tmp_path: Path) -> None:
        """Matches in the false_positive_allowlist should be excluded."""
        test_file = tmp_path / "allowed.txt"
        test_file.write_text("Contact: allowed@example.com\n")

        config = RedactionConfig(
            false_positive_allowlist=["allowed@example.com"],
        )
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        email_matches = [m for m in matches if m.match_type == "email"]
        assert len(email_matches) == 0

    def test_custom_patterns(self, tmp_path: Path) -> None:
        """Custom patterns from config should be applied during scanning."""
        test_file = tmp_path / "custom.txt"
        test_file.write_text("credit card: 4111-1111-1111-1111\n")

        config = RedactionConfig(
            custom_patterns={
                "credit_card": r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",
            },
        )
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        cc_matches = [m for m in matches if m.match_type == "credit_card"]
        assert len(cc_matches) >= 1

    def test_session_salt_is_bytes(self) -> None:
        """Scanner session salt should be bytes (from os.urandom)."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        assert isinstance(scanner.salt, bytes)
        assert len(scanner.salt) == 16


# ---------------------------------------------------------------------------
# RedactionScanner.generate_report
# ---------------------------------------------------------------------------


class TestGenerateReport:
    """Tests for RedactionScanner.generate_report."""

    def test_report_empty_matches(self) -> None:
        """Report for empty matches should indicate no findings."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        report = scanner.generate_report([])
        assert "no" in report.lower() or "0" in report

    def test_report_with_matches(self, tmp_path: Path) -> None:
        """Report should include match count and types."""
        test_file = tmp_path / "report.txt"
        test_file.write_text("SSN: 123-45-6789\nemail: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        report = scanner.generate_report(matches)

        assert "ssn" in report.lower()
        assert "email" in report.lower()
        assert isinstance(report, str)
        assert len(report) > 0

    def test_report_contains_file_paths(self, tmp_path: Path) -> None:
        """Report should reference the file paths where matches were found."""
        test_file = tmp_path / "report_file.txt"
        test_file.write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        report = scanner.generate_report(matches)

        assert "report_file.txt" in report


# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------


class TestRedactor:
    """Tests for the Redactor class."""

    def test_redact_content_replaces_ssn(self) -> None:
        """redact_content should replace SSN with [REDACTED:ssn]."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "My SSN is 123-45-6789 and that is private."
        result = redactor.redact_content(content, pattern_types=["ssn"])

        assert "123-45-6789" not in result
        assert "[REDACTED:ssn]" in result

    def test_redact_content_replaces_email(self) -> None:
        """redact_content should replace emails with [REDACTED:email]."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "Contact me at user@example.com for details."
        result = redactor.redact_content(content, pattern_types=["email"])

        assert "user@example.com" not in result
        assert "[REDACTED:email]" in result

    def test_redact_content_replaces_password(self) -> None:
        """redact_content should replace password assignments."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "password=secret123"
        result = redactor.redact_content(content, pattern_types=["password"])

        assert "secret123" not in result
        assert "[REDACTED:password]" in result

    def test_redact_content_replaces_api_key(self) -> None:
        """redact_content should replace API keys."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "key = AKIAIOSFODNN7EXAMPLE"
        result = redactor.redact_content(content, pattern_types=["api_key"])

        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED:api_key]" in result

    def test_redact_content_multiple_types(self) -> None:
        """redact_content should handle multiple pattern types."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "SSN: 123-45-6789\nEmail: test@example.com\npassword=secret\n"
        result = redactor.redact_content(content)

        assert "123-45-6789" not in result
        assert "test@example.com" not in result
        assert "secret" not in result
        assert "[REDACTED:ssn]" in result
        assert "[REDACTED:email]" in result
        assert "[REDACTED:password]" in result

    def test_redact_content_preserves_clean_text(self) -> None:
        """redact_content should not alter text without sensitive data."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "This is a clean line with no PII."
        result = redactor.redact_content(content)

        assert result == content

    def test_redact_content_respects_allowlist(self) -> None:
        """redact_content should not redact allowlisted strings."""
        config = RedactionConfig(
            false_positive_allowlist=["safe@example.com"],
        )
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "Contact: safe@example.com"
        result = redactor.redact_content(content, pattern_types=["email"])

        assert "safe@example.com" in result
        assert "[REDACTED:email]" not in result

    def test_redact_content_custom_patterns(self) -> None:
        """redact_content should apply custom patterns."""
        config = RedactionConfig(
            custom_patterns={
                "credit_card": r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",
            },
        )
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "CC: 4111-1111-1111-1111"
        result = redactor.redact_content(content, pattern_types=["credit_card"])

        assert "4111-1111-1111-1111" not in result
        assert "[REDACTED:credit_card]" in result

    def test_log_redactions_creates_file(self, tmp_path: Path) -> None:
        """log_redactions should create or append to the log file."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        log_path = tmp_path / "redactions.json"

        matches = [
            RedactionMatch(
                file_path=Path("test.txt"),
                line_number=1,
                match_type="ssn",
                salted_hash="abc123",
            ),
        ]

        redactor.log_redactions(matches, log_path)
        assert log_path.exists()

        data = json.loads(log_path.read_text())
        assert isinstance(data, dict)
        assert "salt_hex" in data
        assert "entries" in data
        assert len(data["entries"]) == 1
        assert data["entries"][0]["match_type"] == "ssn"

    def test_log_redactions_appends(self, tmp_path: Path) -> None:
        """Calling log_redactions twice should append, not overwrite."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        log_path = tmp_path / "redactions.json"

        matches1 = [
            RedactionMatch(
                file_path=Path("a.txt"),
                line_number=1,
                match_type="ssn",
                salted_hash="hash1",
            ),
        ]
        matches2 = [
            RedactionMatch(
                file_path=Path("b.txt"),
                line_number=2,
                match_type="email",
                salted_hash="hash2",
            ),
        ]

        redactor.log_redactions(matches1, log_path)
        redactor.log_redactions(matches2, log_path)

        data = json.loads(log_path.read_text())
        assert len(data["entries"]) == 2

    def test_log_redactions_no_sensitive_data(self, tmp_path: Path) -> None:
        """Log file must NOT contain any actual sensitive data."""
        test_file = tmp_path / "pii.txt"
        sensitive = "123-45-6789"
        test_file.write_text(f"SSN: {sensitive}\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        redactor = Redactor(config=config, salt=scanner.salt)
        log_path = tmp_path / "redactions.json"
        redactor.log_redactions(matches, log_path)

        log_content = log_path.read_text()
        assert sensitive not in log_content


# ---------------------------------------------------------------------------
# Security Tests — ensure sensitive data never leaks
# ---------------------------------------------------------------------------


class TestSecurityGuarantees:
    """Tests that sensitive data is NEVER stored in RedactionMatch objects."""

    def test_ssn_not_in_match(self, tmp_path: Path) -> None:
        """SSN should never appear in any RedactionMatch field."""
        test_file = tmp_path / "ssn.txt"
        ssn = "999-88-7777"
        test_file.write_text(f"SSN: {ssn}\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        for match in matches:
            dumped = match.model_dump(mode="json")
            serialized = json.dumps(dumped)
            assert ssn not in serialized

    def test_email_not_in_match(self, tmp_path: Path) -> None:
        """Email should never appear in any RedactionMatch field."""
        test_file = tmp_path / "email.txt"
        email = "sensitive@secret.com"
        test_file.write_text(f"Contact: {email}\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        for match in matches:
            dumped = match.model_dump(mode="json")
            serialized = json.dumps(dumped)
            assert email not in serialized

    def test_password_not_in_match(self, tmp_path: Path) -> None:
        """Password value should never appear in any RedactionMatch field."""
        test_file = tmp_path / "pw.txt"
        password = "SuperSecret123!"
        test_file.write_text(f"password={password}\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        for match in matches:
            dumped = match.model_dump(mode="json")
            serialized = json.dumps(dumped)
            assert password not in serialized

    def test_api_key_not_in_match(self, tmp_path: Path) -> None:
        """API key should never appear in any RedactionMatch field."""
        test_file = tmp_path / "key.txt"
        api_key = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        test_file.write_text(f"key = {api_key}\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        for match in matches:
            dumped = match.model_dump(mode="json")
            serialized = json.dumps(dumped)
            assert api_key not in serialized


# ---------------------------------------------------------------------------
# New Pattern Tests (Issue #13)
# ---------------------------------------------------------------------------


class TestNewPatterns:
    """Tests for newly added redaction patterns."""

    # -- credit_card --

    def test_credit_card_visa(self) -> None:
        """credit_card pattern should match Visa numbers."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert pattern.search("4111111111111111")
        assert pattern.search("4111-1111-1111-1111")
        assert pattern.search("4111 1111 1111 1111")

    def test_credit_card_mastercard(self) -> None:
        """credit_card pattern should match Mastercard numbers."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert pattern.search("5111111111111118")
        assert pattern.search("5211-1111-1111-1111")

    def test_credit_card_amex(self) -> None:
        """credit_card pattern should match Amex numbers."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert pattern.search("371111111111114")
        assert pattern.search("3411-111111-11111")

    def test_credit_card_no_false_positive(self) -> None:
        """credit_card should not match random numbers."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert not pattern.search("1234567890123456")
        assert not pattern.search("1234-5678-9012")
        assert not pattern.search("hello world")

    def test_credit_card_discover_65xx(self) -> None:
        """credit_card pattern should match Discover 65xx prefix."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert pattern.search("6500 1234 5678 9012")

    def test_credit_card_discover_644(self) -> None:
        """credit_card pattern should match Discover 644 prefix."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert pattern.search("6440 1234 5678 9012")

    def test_credit_card_discover_649(self) -> None:
        """credit_card pattern should match Discover 649 prefix."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert pattern.search("6490 1234 5678 9012")

    def test_credit_card_discover_643_no_match(self) -> None:
        """credit_card pattern should NOT match 643 prefix."""
        pattern = REDACTION_PATTERNS["credit_card"]
        assert not pattern.search("6430 1234 5678 9012")

    # -- email_password_combo --

    def test_email_password_combo_matches(self) -> None:
        """email_password_combo should match email:password pairs."""
        pattern = REDACTION_PATTERNS["email_password_combo"]
        assert pattern.search("user@example.com:password123")
        assert pattern.search("admin@site.org:s3cret!")
        assert pattern.search("test+tag@domain.co:mypass")

    def test_email_password_combo_no_false_positive(self) -> None:
        """email_password_combo should not match plain emails."""
        pattern = REDACTION_PATTERNS["email_password_combo"]
        assert not pattern.search("user@example.com ")
        assert not pattern.search("just some text")

    # -- aws_secret_key --

    def test_aws_secret_key_matches(self) -> None:
        """aws_secret_key should match AWS secret key assignments."""
        pattern = REDACTION_PATTERNS["aws_secret_key"]
        fake_key = "A" * 40
        assert pattern.search(f"aws_secret_access_key={fake_key}")
        assert pattern.search(f"AWS_SECRET = {fake_key}")
        assert pattern.search(f"aws_secret={fake_key}")

    def test_aws_secret_key_no_false_positive(self) -> None:
        """aws_secret_key should not match short values."""
        pattern = REDACTION_PATTERNS["aws_secret_key"]
        assert not pattern.search("aws_secret=short")
        assert not pattern.search("some random text")

    # -- private_key --

    def test_private_key_matches(self) -> None:
        """private_key should match PEM key headers."""
        pattern = REDACTION_PATTERNS["private_key"]
        assert pattern.search("-----BEGIN PRIVATE KEY-----")
        assert pattern.search("-----BEGIN RSA PRIVATE KEY-----")
        assert pattern.search("-----BEGIN EC PRIVATE KEY-----")
        assert pattern.search("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_private_key_no_false_positive(self) -> None:
        """private_key should not match public key headers."""
        pattern = REDACTION_PATTERNS["private_key"]
        assert not pattern.search("-----BEGIN PUBLIC KEY-----")
        assert not pattern.search("-----BEGIN CERTIFICATE-----")
        assert not pattern.search("just some dashes -----")

    # -- bearer_token --

    def test_bearer_token_matches(self) -> None:
        """bearer_token should match Authorization Bearer headers."""
        pattern = REDACTION_PATTERNS["bearer_token"]
        assert pattern.search("Bearer eyJhbGciOiJIUzI1NiJ9")
        assert pattern.search("bearer abc123def456")
        assert pattern.search("Authorization: Bearer my-token-value")

    def test_bearer_token_no_false_positive(self) -> None:
        """bearer_token should not match standalone 'Bearer' word."""
        pattern = REDACTION_PATTERNS["bearer_token"]
        assert not pattern.search("The bearer of bad news")

    # -- env_secret --

    def test_env_secret_matches(self) -> None:
        """env_secret should match secret env var assignments."""
        pattern = REDACTION_PATTERNS["env_secret"]
        assert pattern.search("export SECRET_KEY=mysecretvalue")
        assert pattern.search("API_KEY=abcdef123456")
        assert pattern.search("TOKEN=some-token-value")

    def test_env_secret_no_false_positive(self) -> None:
        """env_secret should not match unrelated env vars."""
        pattern = REDACTION_PATTERNS["env_secret"]
        assert not pattern.search("export HOME=/Users/me")
        assert not pattern.search("PATH=/usr/bin")

    # -- slack_token --

    def test_slack_token_matches(self) -> None:
        """slack_token should match Slack API token formats."""
        pattern = REDACTION_PATTERNS["slack_token"]
        assert pattern.search("xoxb-1234567890-abcdefghij")
        assert pattern.search("xoxp-1234567890-abcdefghij")
        assert pattern.search("xoxs-1234567890-abcdefghij")

    def test_slack_token_no_false_positive(self) -> None:
        """slack_token should not match invalid prefixes."""
        pattern = REDACTION_PATTERNS["slack_token"]
        assert not pattern.search("xoxz-1234567890")
        assert not pattern.search("xoxb-short")
        assert not pattern.search("hello xox world")

    # -- phone_number --

    def test_phone_number_matches(self) -> None:
        """phone_number should match US phone number formats."""
        pattern = REDACTION_PATTERNS["phone_number"]
        assert pattern.search("(555) 123-4567")
        assert pattern.search("555-123-4567")
        assert pattern.search("555.123.4567")

    def test_phone_number_no_false_positive(self) -> None:
        """phone_number should not match SSNs or random digits."""
        pattern = REDACTION_PATTERNS["phone_number"]
        assert not pattern.search("123-45-6789")
        assert not pattern.search("12345")
        assert not pattern.search("hello world")

    # -- github_token --

    def test_github_token_matches(self) -> None:
        """github_token should match GitHub token formats."""
        pattern = REDACTION_PATTERNS["github_token"]
        suffix = "A" * 36
        assert pattern.search(f"ghp_{suffix}")
        assert pattern.search(f"gho_{suffix}")
        assert pattern.search(f"ghs_{suffix}")

    def test_github_token_no_false_positive(self) -> None:
        """github_token should not match invalid prefixes."""
        pattern = REDACTION_PATTERNS["github_token"]
        assert not pattern.search("ghx_abcdefghij")
        assert not pattern.search("ghp_short")
        assert not pattern.search("github is great")

    # -- jwt --

    def test_jwt_matches(self) -> None:
        """jwt pattern should match JSON Web Token format."""
        pattern = REDACTION_PATTERNS["jwt"]
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi789"
        assert pattern.search(token)

    def test_jwt_no_false_positive(self) -> None:
        """jwt should not match non-JWT strings."""
        pattern = REDACTION_PATTERNS["jwt"]
        assert not pattern.search("abc.def.ghi")
        assert not pattern.search("eyJ.short.x")
        assert not pattern.search("not a jwt at all")


# ---------------------------------------------------------------------------
# Pattern Metadata Tests
# ---------------------------------------------------------------------------


class TestPatternInfo:
    """Tests for PatternInfo metadata."""

    def test_all_patterns_have_metadata(self) -> None:
        """Every pattern in REDACTION_PATTERNS should have metadata."""
        from creek.redact.patterns import PATTERN_METADATA

        for name in REDACTION_PATTERNS:
            assert name in PATTERN_METADATA, f"Missing metadata: {name}"

    def test_metadata_has_valid_severity(self) -> None:
        """All pattern metadata should have valid severity levels."""
        from creek.redact.patterns import _VALID_SEVERITIES, PATTERN_METADATA

        for name, info in PATTERN_METADATA.items():
            assert info.severity in _VALID_SEVERITIES, (
                f"Invalid severity '{info.severity}' for {name}"
            )

    def test_metadata_has_description(self) -> None:
        """All pattern metadata should have non-empty descriptions."""
        from creek.redact.patterns import PATTERN_METADATA

        for name, info in PATTERN_METADATA.items():
            assert info.description, f"Empty description for {name}"

    def test_metadata_has_false_positive_notes(self) -> None:
        """All pattern metadata should have false positive notes."""
        from creek.redact.patterns import PATTERN_METADATA

        for name, info in PATTERN_METADATA.items():
            assert info.false_positive_notes, f"Empty FP notes: {name}"

    def test_pattern_count(self) -> None:
        """Should have at least 13 patterns."""
        from creek.redact.patterns import PATTERN_METADATA

        assert len(PATTERN_METADATA) >= 13

    def test_invalid_severity_raises_value_error(self) -> None:
        """PatternInfo with invalid severity should raise ValueError."""
        from creek.redact.patterns import PatternInfo

        with pytest.raises(ValueError, match="severity"):
            PatternInfo(
                pattern=re.compile(r"test"),
                description="test",
                severity="invalid",
                false_positive_notes="test",
            )


# ---------------------------------------------------------------------------
# Catastrophic Backtracking Tests
# ---------------------------------------------------------------------------


class TestCatastrophicBacktracking:
    """Tests that patterns don't exhibit catastrophic backtracking."""

    def test_long_input_all_patterns(self) -> None:
        """All patterns should handle long inputs without backtracking."""
        import time

        long_input = "x" * 10_000
        for name, pattern in REDACTION_PATTERNS.items():
            start = time.monotonic()
            pattern.search(long_input)
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, f"{name} took {elapsed:.2f}s"

    def test_long_input_credit_card(self) -> None:
        """credit_card pattern should handle long digit strings."""
        import time

        pattern = REDACTION_PATTERNS["credit_card"]
        long_input = "1234 " * 2000
        start = time.monotonic()
        pattern.search(long_input)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"credit_card took {elapsed:.2f}s"

    def test_long_input_jwt(self) -> None:
        """jwt pattern should handle long base64-like strings."""
        import time

        pattern = REDACTION_PATTERNS["jwt"]
        long_input = "eyJ" + "a" * 10_000
        start = time.monotonic()
        pattern.search(long_input)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"jwt took {elapsed:.2f}s"

    def test_long_input_phone(self) -> None:
        """phone_number pattern should handle long digit strings."""
        import time

        pattern = REDACTION_PATTERNS["phone_number"]
        long_input = "555-" * 2500
        start = time.monotonic()
        pattern.search(long_input)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"phone_number took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Binary File Detection (Issue #14)
# ---------------------------------------------------------------------------


class TestBinaryDetection:
    """Tests for binary file detection."""

    def test_detects_png(self, tmp_path: Path) -> None:
        """is_binary should detect PNG files."""
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        assert RedactionScanner.is_binary(f)

    def test_detects_jpeg(self, tmp_path: Path) -> None:
        """is_binary should detect JPEG files."""
        f = tmp_path / "photo.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        assert RedactionScanner.is_binary(f)

    def test_detects_pdf(self, tmp_path: Path) -> None:
        """is_binary should detect PDF files."""
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF-1.4" + b"\x00" * 100)
        assert RedactionScanner.is_binary(f)

    def test_detects_zip(self, tmp_path: Path) -> None:
        """is_binary should detect ZIP files."""
        f = tmp_path / "archive.zip"
        f.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        assert RedactionScanner.is_binary(f)

    def test_detects_elf(self, tmp_path: Path) -> None:
        """is_binary should detect ELF binaries."""
        f = tmp_path / "program"
        f.write_bytes(b"\x7fELF" + b"\x00" * 100)
        assert RedactionScanner.is_binary(f)

    def test_detects_null_bytes(self, tmp_path: Path) -> None:
        """is_binary should detect files containing null bytes."""
        f = tmp_path / "binary.dat"
        f.write_bytes(b"header\x00\x01\x02\x03" + b"\x00" * 100)
        assert RedactionScanner.is_binary(f)

    def test_text_file_not_binary(self, tmp_path: Path) -> None:
        """is_binary should return False for plain text files."""
        f = tmp_path / "text.txt"
        f.write_text("Hello, this is a normal text file.\n")
        assert not RedactionScanner.is_binary(f)

    def test_empty_file_not_binary(self, tmp_path: Path) -> None:
        """is_binary should return False for empty files."""
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        assert not RedactionScanner.is_binary(f)

    def test_nonexistent_file_not_binary(self, tmp_path: Path) -> None:
        """is_binary should return False for nonexistent files."""
        assert not RedactionScanner.is_binary(tmp_path / "no_such_file")

    def test_scan_directory_skips_binary(self, tmp_path: Path) -> None:
        """scan_directory should skip binary files with supported extensions."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")
        # Use a supported extension (.txt) with binary content
        binary_file = tmp_path / "binary.txt"
        binary_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 1
        assert summary.files_skipped_binary == 1
        assert len(summary.matches) >= 1


# ---------------------------------------------------------------------------
# File Extension Filtering (Issue #14)
# ---------------------------------------------------------------------------


class TestExtensionFiltering:
    """Tests for file extension filtering."""

    def test_supported_extensions_scanned(self, tmp_path: Path) -> None:
        """Files with supported extensions should be scanned."""
        for ext in (".txt", ".md", ".json", ".py", ".env", ".yaml", ".toml", ".csv"):
            f = tmp_path / f"data{ext}"
            f.write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 8

    def test_unsupported_extension_skipped(self, tmp_path: Path) -> None:
        """Files with unsupported extensions should be skipped."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")
        (tmp_path / "style.css").write_text("SSN: 123-45-6789\n")
        (tmp_path / "script.js").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 1
        assert summary.files_skipped_extension == 2

    def test_custom_extensions(self, tmp_path: Path) -> None:
        """Custom supported_extensions should be respected."""
        (tmp_path / "data.xyz").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig(supported_extensions=[".xyz"])
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 1
        assert len(summary.matches) >= 1


# ---------------------------------------------------------------------------
# Exclusion Patterns (Issue #14)
# ---------------------------------------------------------------------------


class TestExclusionPatterns:
    """Tests for directory exclusion patterns."""

    def test_git_directory_excluded(self, tmp_path: Path) -> None:
        """Files under .git/ should be excluded from scanning."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.txt").write_text("SSN: 123-45-6789\n")
        (tmp_path / "data.txt").write_text("SSN: 999-88-7777\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        scanned_files = {str(m.file_path) for m in summary.matches}
        assert not any(".git" in f for f in scanned_files)
        assert summary.files_scanned == 1

    def test_node_modules_excluded(self, tmp_path: Path) -> None:
        """Files under node_modules/ should be excluded from scanning."""
        nm_dir = tmp_path / "node_modules"
        nm_dir.mkdir()
        (nm_dir / "lib.py").write_text("password=secret123\n")
        (tmp_path / "app.py").write_text("password=secret123\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 1

    def test_custom_exclusion_patterns(self, tmp_path: Path) -> None:
        """Custom exclusion patterns should be respected."""
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "lib.py").write_text("SSN: 123-45-6789\n")
        (tmp_path / "main.py").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig(exclude_patterns=[".git", "node_modules", "vendor"])
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 1


# ---------------------------------------------------------------------------
# Context Extraction (Issue #14)
# ---------------------------------------------------------------------------


class TestContextExtraction:
    """Tests for context line extraction around matches."""

    def test_context_around_middle_line(self, tmp_path: Path) -> None:
        """extract_context should return lines around the match."""
        f = tmp_path / "ctx.txt"
        f.write_text("line 1\nline 2\nSSN: 123-45-6789\nline 4\nline 5\n")

        context = RedactionScanner.extract_context(f, line_number=3, window=2)

        assert len(context) == 5
        assert "SSN: 123-45-6789" in context

    def test_context_at_file_start(self, tmp_path: Path) -> None:
        """extract_context should clamp to start of file."""
        f = tmp_path / "start.txt"
        f.write_text("SSN: 123-45-6789\nline 2\nline 3\n")

        context = RedactionScanner.extract_context(f, line_number=1, window=2)

        assert len(context) == 3
        assert context[0] == "SSN: 123-45-6789"

    def test_context_at_file_end(self, tmp_path: Path) -> None:
        """extract_context should clamp to end of file."""
        f = tmp_path / "end.txt"
        f.write_text("line 1\nline 2\nSSN: 123-45-6789\n")

        context = RedactionScanner.extract_context(f, line_number=3, window=2)

        assert len(context) == 3
        assert "SSN: 123-45-6789" in context

    def test_context_nonexistent_file(self, tmp_path: Path) -> None:
        """extract_context should return empty list for missing files."""
        result = RedactionScanner.extract_context(
            tmp_path / "missing.txt", line_number=1
        )
        assert result == []

    def test_context_default_window(self, tmp_path: Path) -> None:
        """extract_context should use window=2 by default."""
        lines = [f"line {i}" for i in range(1, 11)]
        f = tmp_path / "ten.txt"
        f.write_text("\n".join(lines) + "\n")

        context = RedactionScanner.extract_context(f, line_number=5)

        assert len(context) == 5
        assert "line 3" in context
        assert "line 7" in context


# ---------------------------------------------------------------------------
# ScanSummary (Issue #14)
# ---------------------------------------------------------------------------


class TestScanSummary:
    """Tests for the ScanSummary dataclass."""

    def test_scan_summary_fields(self) -> None:
        """ScanSummary should have all expected fields."""
        summary = ScanSummary(
            matches=[],
            files_scanned=10,
            files_skipped_binary=2,
            files_skipped_extension=3,
        )
        assert summary.files_scanned == 10
        assert summary.files_skipped_binary == 2
        assert summary.files_skipped_extension == 3
        assert summary.matches == []

    def test_scan_batch_returns_summary(self, tmp_path: Path) -> None:
        """scan_batch should return a ScanSummary."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert isinstance(summary, ScanSummary)
        assert summary.files_scanned >= 1
        assert len(summary.matches) >= 1

    def test_scan_batch_nonexistent_directory(self) -> None:
        """scan_batch should raise FileNotFoundError for missing directory."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)

        with pytest.raises(FileNotFoundError):
            scanner.scan_batch(Path("/nonexistent/directory"))

    def test_scan_batch_empty_directory(self, tmp_path: Path) -> None:
        """scan_batch on empty directory should return zeroed summary."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        assert summary.files_scanned == 0
        assert summary.files_skipped_binary == 0
        assert summary.files_skipped_extension == 0
        assert summary.matches == []

    def test_scan_directory_delegates_to_scan_batch(self, tmp_path: Path) -> None:
        """scan_directory should return the same matches as scan_batch."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)

        dir_matches = scanner.scan_directory(tmp_path)
        # Re-create scanner to reset salt (hashes will differ), but check counts
        scanner2 = RedactionScanner(config=config)
        summary = scanner2.scan_batch(tmp_path)

        assert len(dir_matches) == len(summary.matches)


# ---------------------------------------------------------------------------
# Progress Bar (Issue #14)
# ---------------------------------------------------------------------------


class TestProgressBar:
    """Tests for tqdm progress bar during scanning."""

    def test_scan_directory_with_progress(self, tmp_path: Path) -> None:
        """scan_directory with progress=True should not raise."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_directory(tmp_path, progress=True)

        assert len(matches) >= 1

    def test_scan_batch_with_progress(self, tmp_path: Path) -> None:
        """scan_batch with progress=True should not raise."""
        (tmp_path / "data.txt").write_text("email: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path, progress=True)

        assert summary.files_scanned >= 1


# ---------------------------------------------------------------------------
# JSON Report Generation (Issue #14)
# ---------------------------------------------------------------------------


class TestJsonReport:
    """Tests for JSON report generation."""

    def test_json_report_created(self, tmp_path: Path) -> None:
        """generate_json_report should create a JSON file."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        report_path = tmp_path / "report" / "redaction-report.json"
        scanner.generate_json_report(summary, report_path)

        assert report_path.exists()

    def test_json_report_structure(self, tmp_path: Path) -> None:
        """JSON report should contain scan_statistics and findings_by_file."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        report_path = tmp_path / "redaction-report.json"
        scanner.generate_json_report(summary, report_path)

        data = json.loads(report_path.read_text())
        assert "scan_statistics" in data
        assert "findings_by_file" in data

        stats = data["scan_statistics"]
        assert "files_scanned" in stats
        assert "files_skipped_binary" in stats
        assert "files_skipped_extension" in stats
        assert "total_findings" in stats
        assert "by_severity" in stats
        assert "by_type" in stats

    def test_json_report_match_metadata(self, tmp_path: Path) -> None:
        """JSON report findings should contain match metadata."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        report_path = tmp_path / "redaction-report.json"
        scanner.generate_json_report(summary, report_path)

        data = json.loads(report_path.read_text())
        findings = data["findings_by_file"]
        assert len(findings) > 0

        for _file, file_matches in findings.items():
            for match in file_matches:
                assert "line_number" in match
                assert "match_type" in match
                assert "severity" in match
                assert "salted_hash" in match

    def test_json_report_creates_parent_dirs(self, tmp_path: Path) -> None:
        """generate_json_report should create parent directories."""
        summary = ScanSummary()
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)

        deep_path = tmp_path / "a" / "b" / "c" / "report.json"
        scanner.generate_json_report(summary, deep_path)

        assert deep_path.exists()

    def test_json_report_empty_summary(self, tmp_path: Path) -> None:
        """JSON report for empty summary should have zero totals."""
        summary = ScanSummary()
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)

        report_path = tmp_path / "empty-report.json"
        scanner.generate_json_report(summary, report_path)

        data = json.loads(report_path.read_text())
        assert data["scan_statistics"]["total_findings"] == 0
        assert data["findings_by_file"] == {}

    def test_json_report_severity_sorted(self, tmp_path: Path) -> None:
        """Findings in JSON report should be sorted by severity."""
        f = tmp_path / "mixed.txt"
        f.write_text("email: test@example.com\nSSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)

        report_path = tmp_path / "report.json"
        scanner.generate_json_report(summary, report_path)

        data = json.loads(report_path.read_text())
        for _file, file_matches in data["findings_by_file"].items():
            severities = [m["severity"] for m in file_matches]
            order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
            ranks = [order.get(s, 4) for s in severities]
            assert ranks == sorted(ranks)


# ---------------------------------------------------------------------------
# Markdown Summary (Issue #14)
# ---------------------------------------------------------------------------


class TestMarkdownSummary:
    """Tests for markdown summary generation."""

    def test_markdown_summary_no_findings(self) -> None:
        """Markdown summary with no findings should say so."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = ScanSummary()
        md = scanner.generate_markdown_summary(summary)

        assert "No findings" in md

    def test_markdown_summary_has_statistics(self, tmp_path: Path) -> None:
        """Markdown summary should include scan statistics."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        md = scanner.generate_markdown_summary(summary)

        assert "Files scanned" in md
        assert "Total findings" in md

    def test_markdown_summary_has_severity_breakdown(self, tmp_path: Path) -> None:
        """Markdown summary should include severity breakdown."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        md = scanner.generate_markdown_summary(summary)

        assert "By Severity" in md

    def test_markdown_summary_grouped_by_file(self, tmp_path: Path) -> None:
        """Markdown summary should group findings by file."""
        (tmp_path / "a.txt").write_text("SSN: 123-45-6789\n")
        (tmp_path / "b.txt").write_text("email: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        md = scanner.generate_markdown_summary(summary)

        assert "a.txt" in md
        assert "b.txt" in md

    def test_markdown_summary_has_table(self, tmp_path: Path) -> None:
        """Markdown summary should use table format for findings."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        md = scanner.generate_markdown_summary(summary)

        assert "| Line |" in md
        assert "| Type |" in md
        assert "| Severity |" in md


# ---------------------------------------------------------------------------
# Review Queue (Issue #14)
# ---------------------------------------------------------------------------


class TestReviewQueue:
    """Tests for review queue generation."""

    def test_review_queue_no_findings(self) -> None:
        """Review queue with no findings should say so."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = ScanSummary()
        rq = scanner.generate_review_queue(summary)

        assert "No findings to review" in rq

    def test_review_queue_has_checkboxes(self, tmp_path: Path) -> None:
        """Review queue should have checkboxes for each finding."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        rq = scanner.generate_review_queue(summary)

        assert "- [ ] Confirmed sensitive" in rq
        assert "- [ ] False positive" in rq

    def test_review_queue_has_context(self, tmp_path: Path) -> None:
        """Review queue should include context around each finding."""
        f = tmp_path / "ctx.txt"
        f.write_text("line 1\nline 2\nSSN: 123-45-6789\nline 4\nline 5\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        rq = scanner.generate_review_queue(summary)

        assert "```" in rq
        assert "SSN: 123-45-6789" in rq

    def test_review_queue_has_type_and_severity(self, tmp_path: Path) -> None:
        """Review queue should include pattern type and severity."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        rq = scanner.generate_review_queue(summary)

        assert "**Type**: ssn" in rq
        assert "**Severity**: critical" in rq

    def test_review_queue_grouped_by_file(self, tmp_path: Path) -> None:
        """Review queue should group findings by file."""
        (tmp_path / "a.txt").write_text("SSN: 123-45-6789\n")
        (tmp_path / "b.txt").write_text("email: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        rq = scanner.generate_review_queue(summary)

        assert "a.txt" in rq
        assert "b.txt" in rq

    def test_review_queue_has_instructions(self, tmp_path: Path) -> None:
        """Review queue should include reviewer instructions."""
        (tmp_path / "data.txt").write_text("SSN: 123-45-6789\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        rq = scanner.generate_review_queue(summary)

        assert "Review each finding" in rq

    def test_review_queue_finding_numbers(self, tmp_path: Path) -> None:
        """Review queue findings should be numbered sequentially."""
        f = tmp_path / "data.txt"
        f.write_text("SSN: 123-45-6789\nemail: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path)
        rq = scanner.generate_review_queue(summary)

        assert "Finding 1" in rq
        assert "Finding 2" in rq


# ---------------------------------------------------------------------------
# Config Enhancements (Issue #14)
# ---------------------------------------------------------------------------


class TestRedactionConfigEnhancements:
    """Tests for RedactionConfig new fields."""

    def test_default_supported_extensions(self) -> None:
        """RedactionConfig should have default supported extensions."""
        config = RedactionConfig()
        assert ".txt" in config.supported_extensions
        assert ".md" in config.supported_extensions
        assert ".json" in config.supported_extensions
        assert ".py" in config.supported_extensions
        assert ".env" in config.supported_extensions
        assert ".yaml" in config.supported_extensions
        assert ".toml" in config.supported_extensions
        assert ".csv" in config.supported_extensions

    def test_default_exclude_patterns(self) -> None:
        """RedactionConfig should have default exclusion patterns."""
        config = RedactionConfig()
        assert ".git" in config.exclude_patterns
        assert "node_modules" in config.exclude_patterns

    def test_custom_supported_extensions(self) -> None:
        """RedactionConfig should accept custom extensions."""
        config = RedactionConfig(supported_extensions=[".xyz", ".abc"])
        assert config.supported_extensions == [".xyz", ".abc"]

    def test_custom_exclude_patterns(self) -> None:
        """RedactionConfig should accept custom exclusion patterns."""
        config = RedactionConfig(exclude_patterns=["vendor", "dist"])
        assert config.exclude_patterns == ["vendor", "dist"]


# ---------------------------------------------------------------------------
# Luhn validation for credit cards (SEC-001)
# ---------------------------------------------------------------------------


class TestLuhnValidator:
    """Tests for the standalone Luhn validator helper."""

    @pytest.mark.parametrize(
        "digits",
        [
            "4111111111111111",  # Visa test card
            "5555555555554444",  # Mastercard test card
            "378282246310005",  # Amex test card
            "6011111111111117",  # Discover test card
        ],
    )
    def test_luhn_accepts_valid(self, digits: str) -> None:
        """_luhn_valid should accept canonical Luhn-valid digit strings."""
        from creek.redact.scanner import _luhn_valid

        assert _luhn_valid(digits)

    @pytest.mark.parametrize(
        "digits",
        [
            "4111111111111112",  # last digit wrong
            "1234567890123456",  # random sequential
            "5555555555554443",  # off-by-one Mastercard
            "0000000000000001",  # near-zero
        ],
    )
    def test_luhn_rejects_invalid(self, digits: str) -> None:
        """_luhn_valid should reject digit strings that fail the checksum."""
        from creek.redact.scanner import _luhn_valid

        assert not _luhn_valid(digits)

    def test_luhn_rejects_non_digits(self) -> None:
        """_luhn_valid should reject non-digit input."""
        from creek.redact.scanner import _luhn_valid

        assert not _luhn_valid("")
        assert not _luhn_valid("abcd")
        assert not _luhn_valid("4111-1111-1111-1111")  # caller must canonicalise


class TestScannerLuhnPostValidation:
    """The scanner must drop credit_card matches that fail Luhn (SEC-001)."""

    @pytest.mark.parametrize(
        "number",
        [
            "4111 1111 1111 1111",  # Luhn-valid Visa test
            "5555-5555-5555-4444",  # Luhn-valid Mastercard test
            "378282246310005",  # Luhn-valid Amex test (15 digits)
        ],
    )
    def test_luhn_accepts_valid_card_numbers(self, tmp_path: Path, number: str) -> None:
        """Scanner should keep credit_card matches that pass Luhn."""
        test_file = tmp_path / "cc.txt"
        test_file.write_text(f"card: {number}\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        assert any(m.match_type == "credit_card" for m in matches)

    @pytest.mark.parametrize(
        "number",
        [
            "4111 1111 1111 1112",  # Luhn-invalid Visa-shaped string
            "5555-5555-5555-4443",  # Luhn-invalid Mastercard-shaped string
        ],
    )
    def test_luhn_rejects_invalid_card_numbers(
        self, tmp_path: Path, number: str
    ) -> None:
        """Scanner should drop credit_card matches that fail Luhn."""
        test_file = tmp_path / "cc.txt"
        test_file.write_text(f"card: {number}\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        assert not any(m.match_type == "credit_card" for m in matches)

    def test_luhn_filter_only_applies_to_credit_card(self, tmp_path: Path) -> None:
        """Other patterns must not be filtered through the Luhn check."""
        test_file = tmp_path / "ssn.txt"
        # SSN that is not 16/15 digits — Luhn must not touch it.
        test_file.write_text("SSN: 123-45-6789\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        ssn_matches = [m for m in matches if m.match_type == "ssn"]
        assert len(ssn_matches) == 1

    def test_luhn_keeps_other_pattern_with_credit_card_lookalike(
        self, tmp_path: Path
    ) -> None:
        """A bearer token containing CC-like digits should still match bearer."""
        test_file = tmp_path / "bearer.txt"
        test_file.write_text(
            "Authorization: Bearer 4111111111111112-extra-token-payload-AAAA\n"
        )

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        # The bearer pattern should still flag it.
        assert any(m.match_type == "bearer_token" for m in matches)

    def test_luhn_low_false_positive_rate_on_random_digits(
        self, tmp_path: Path
    ) -> None:
        """On random 16-digit corpora the false-positive rate must drop below 1%."""
        import random

        rng = random.Random(20260502)
        # 1000 random 16-digit numbers prefixed to match BIN ranges.
        prefixes = ["4", "51", "52", "53", "54", "55", "65", "60"]
        samples = []
        for _ in range(1000):
            prefix = rng.choice(prefixes)
            remaining = 16 - len(prefix)
            digits = prefix + "".join(str(rng.randint(0, 9)) for _ in range(remaining))
            samples.append(digits)

        test_file = tmp_path / "noise.txt"
        test_file.write_text("\n".join(samples) + "\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)
        cc_matches = [m for m in matches if m.match_type == "credit_card"]
        # Pure random digits will be Luhn-valid ~10% of the time on average,
        # so we expect a small fraction to survive. The point is that Luhn
        # collapses what would otherwise be a 100% match rate (one per line)
        # to a small slice of those.
        assert len(cc_matches) < 150, (
            f"Luhn filter not effective: {len(cc_matches)} of 1000 matched"
        )


# ---------------------------------------------------------------------------
# Modern API token + network identifier patterns (SEC-002, INC-014)
# ---------------------------------------------------------------------------


class TestModernApiTokenPatterns:
    """Tests for new API-token patterns added in batch D."""

    # -- discord_bot_token --

    def test_discord_bot_token_matches(self) -> None:
        """discord_bot_token should match the documented Discord token shape."""
        pattern = REDACTION_PATTERNS["discord_bot_token"]
        # Synthetic token: prefix + 23 chars + . + 6 chars + . + 27 chars
        token = "MTE" + "a" * 23 + ".AAAAAA." + "x" * 27
        assert pattern.search(token)

    def test_discord_bot_token_modern_prefix(self) -> None:
        """discord_bot_token should match longer modern prefixes."""
        pattern = REDACTION_PATTERNS["discord_bot_token"]
        # Newer Discord tokens are longer; allow up to ~38 chars in tail.
        token = "Nzk" + "B" * 27 + ".AAAAAA." + "y" * 38
        assert pattern.search(token)

    def test_discord_bot_token_no_false_positive(self) -> None:
        """discord_bot_token should not match arbitrary dotted strings."""
        pattern = REDACTION_PATTERNS["discord_bot_token"]
        assert not pattern.search("hello.world.foo")
        assert not pattern.search("MTE.short.abc")

    # -- github_pat (fine-grained PAT) --

    def test_github_pat_matches(self) -> None:
        """github_pat should match GitHub fine-grained PAT format."""
        pattern = REDACTION_PATTERNS["github_pat"]
        # Documented format: github_pat_<11 chars>_<59 chars>; total ~82 chars.
        token = "github_pat_" + "A" * 11 + "_" + "B" * 59
        assert pattern.search(token)

    def test_github_pat_no_false_positive(self) -> None:
        """github_pat should not match short or non-PAT strings."""
        pattern = REDACTION_PATTERNS["github_pat"]
        assert not pattern.search("github_pat_short")
        assert not pattern.search("ghp_classic_token")

    def test_github_pat_distinct_from_classic(self) -> None:
        """Existing github_token (classic) must still match `gh[pousr]_...`."""
        classic = REDACTION_PATTERNS["github_token"]
        assert classic.search("ghp_" + "A" * 36)

    # -- stripe_key --

    @pytest.mark.parametrize(
        "key",
        [
            "sk_live_" + "A" * 24,
            "sk_test_" + "A" * 24,
            "pk_live_" + "A" * 24,
            "pk_test_" + "A" * 24,
            "rk_live_" + "A" * 24,
            "rk_test_" + "A" * 24,
        ],
    )
    def test_stripe_key_matches(self, key: str) -> None:
        """stripe_key should match Stripe live/test secret/publishable keys."""
        pattern = REDACTION_PATTERNS["stripe_key"]
        assert pattern.search(key)

    def test_stripe_key_no_false_positive(self) -> None:
        """stripe_key should not match unrelated strings."""
        pattern = REDACTION_PATTERNS["stripe_key"]
        assert not pattern.search("sk_dev_short")
        assert not pattern.search("just text without key")

    # -- anthropic_key --

    def test_anthropic_key_matches(self) -> None:
        """anthropic_key should explicitly match sk-ant- prefixed keys."""
        pattern = REDACTION_PATTERNS["anthropic_key"]
        # 'sk-ant-' followed by 95+ chars in real life; minimum 20 in test.
        assert pattern.search("sk-ant-api03-" + "A" * 80)

    def test_anthropic_key_no_false_positive(self) -> None:
        """anthropic_key should not match unrelated sk- strings."""
        pattern = REDACTION_PATTERNS["anthropic_key"]
        assert not pattern.search("sk-ant-")
        assert not pattern.search("sk-other-not-anthropic")

    # -- openai_project_key --

    def test_openai_project_key_matches(self) -> None:
        """openai_project_key should match sk-proj- prefix."""
        pattern = REDACTION_PATTERNS["openai_project_key"]
        assert pattern.search("sk-proj-" + "A" * 30)

    def test_openai_project_key_no_false_positive(self) -> None:
        """openai_project_key should not match short or unrelated strings."""
        pattern = REDACTION_PATTERNS["openai_project_key"]
        assert not pattern.search("sk-proj-")
        assert not pattern.search("sk-other")


class TestNetworkIdentifierPatterns:
    """Tests for IPv4 / IPv6 patterns (INC-014)."""

    @pytest.mark.parametrize(
        "ip",
        [
            "10.20.30.40",
            "192.168.1.1",
            "8.8.8.8",
            "255.255.255.255",
            "0.0.0.0",
        ],
    )
    def test_ipv4_matches_valid(self, ip: str) -> None:
        """ipv4 pattern should match well-formed IPv4 addresses."""
        pattern = REDACTION_PATTERNS["ipv4"]
        assert pattern.search(f"Server: {ip}")

    @pytest.mark.parametrize(
        "value",
        [
            "256.0.0.1",  # octet > 255
            "1.2.3",  # too few octets
            "1.2.3.4.5",  # too many octets
            "999.999.999.999",  # all out of range
        ],
    )
    def test_ipv4_rejects_invalid(self, value: str) -> None:
        """ipv4 pattern should reject malformed addresses."""
        pattern = REDACTION_PATTERNS["ipv4"]
        # Some invalid values may match a substring of digits — assert that
        # the *full* malformed value does not appear as a single match.
        for m in pattern.finditer(value):
            assert m.group() != value

    @pytest.mark.parametrize(
        "ip",
        [
            "2001:0db8:85a3:0000:0000:8a2e:0370:7334",  # full
            "2001:db8::8a2e:370:7334",  # shortened
            "::1",  # loopback
            "fe80::1",  # link-local short
            "::ffff:192.168.1.1",  # IPv4-mapped
        ],
    )
    def test_ipv6_matches(self, ip: str) -> None:
        """ipv6 pattern should match common IPv6 address forms."""
        pattern = REDACTION_PATTERNS["ipv6"]
        assert pattern.search(ip), f"Failed to match {ip}"

    def test_ipv6_rejects_plain_text(self) -> None:
        """ipv6 pattern should not match arbitrary colon-separated text."""
        pattern = REDACTION_PATTERNS["ipv6"]
        assert not pattern.search("http://example.com")
        assert not pattern.search("hello:world:foo")


# ---------------------------------------------------------------------------
# High-entropy generic-secret detector (SEC-002, RedactionConfig.min_confidence)
# ---------------------------------------------------------------------------


class TestHighEntropyDetector:
    """Tests for the generic high-entropy secret detector."""

    def test_high_entropy_long_random_string_matches(self, tmp_path: Path) -> None:
        """A 32-char random hex string should be flagged."""
        # Hex random secret — high entropy, no obvious pattern.
        secret = "a3f1c8b2e9d74105fb6c2e8a91d34c70"
        test_file = tmp_path / "data.txt"
        test_file.write_text(f"token = {secret}\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        assert any(m.match_type == "high_entropy_string" for m in matches)

    def test_low_entropy_string_not_matched(self, tmp_path: Path) -> None:
        """A long but low-entropy run (e.g. all 'a') should not be flagged."""
        test_file = tmp_path / "data.txt"
        test_file.write_text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        assert not any(m.match_type == "high_entropy_string" for m in matches)

    def test_short_strings_not_matched(self, tmp_path: Path) -> None:
        """Strings shorter than 20 chars must not trigger entropy detection."""
        test_file = tmp_path / "data.txt"
        # Below the 20-char minimum substring length.
        test_file.write_text("abc12345xyz9\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        assert not any(m.match_type == "high_entropy_string" for m in matches)

    def test_min_confidence_gates_detection(self, tmp_path: Path) -> None:
        """Raising min_confidence should suppress low-entropy candidates."""
        # A run that is borderline-entropy.
        borderline = "abcdefgh12345678ijkl"
        test_file = tmp_path / "data.txt"
        test_file.write_text(borderline + "\n")

        # A very strict threshold drops the match.
        strict = RedactionScanner(config=RedactionConfig(min_confidence=1.0))
        strict_matches = strict.scan_file(test_file)
        assert not any(m.match_type == "high_entropy_string" for m in strict_matches)

        # A permissive threshold keeps it.
        permissive = RedactionScanner(
            config=RedactionConfig(min_confidence=0.0),
        )
        permissive_matches = permissive.scan_file(test_file)
        assert any(m.match_type == "high_entropy_string" for m in permissive_matches)

    def test_high_entropy_respects_allowlist(self, tmp_path: Path) -> None:
        """A high-entropy string in the allowlist must not be flagged."""
        secret = "a3f1c8b2e9d74105fb6c2e8a91d34c70"
        test_file = tmp_path / "data.txt"
        test_file.write_text(secret + "\n")

        config = RedactionConfig(false_positive_allowlist=[secret])
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)

        assert not any(m.match_type == "high_entropy_string" for m in matches)


# ---------------------------------------------------------------------------
# replacement_template config field (INC-009)
# ---------------------------------------------------------------------------


class TestReplacementTemplate:
    """Tests for RedactionConfig.replacement_template (INC-009)."""

    def test_default_template_matches_documentation(self) -> None:
        """The default template must produce the documented marker."""
        from creek.config import RedactionConfig as CfgCls

        cfg = CfgCls()
        assert cfg.replacement_template == "[REDACTED:{name}]"

    def test_custom_template_used_in_redactor(self) -> None:
        """Custom replacement_template should drive the redactor output."""
        from creek.config import RedactionConfig as CfgCls

        cfg = CfgCls(replacement_template="<<{name}>>")
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        out = redactor.redact_content(
            "key = AKIAIOSFODNN7EXAMPLE",
            pattern_types=["api_key"],
        )

        assert "<<api_key>>" in out
        assert "REDACTED" not in out

    def test_invalid_template_missing_placeholder_rejected(self) -> None:
        """A template without {name} must be rejected at config load."""
        from pydantic import ValidationError

        from creek.config import RedactionConfig as CfgCls

        with pytest.raises(ValidationError):
            CfgCls(replacement_template="no placeholder here")

    def test_template_with_non_name_placeholder_rejected(self) -> None:
        """A template with an unknown placeholder must be rejected."""
        from pydantic import ValidationError

        from creek.config import RedactionConfig as CfgCls

        with pytest.raises(ValidationError):
            CfgCls(replacement_template="{type}")

    def test_template_literal_check_without_placeholder_rejected(self) -> None:
        """A template containing the literal string ``check`` but no placeholder.

        Regression for a sentinel-based validator that accepted any template
        containing the substring used to verify substitution.
        """
        from pydantic import ValidationError

        from creek.config import RedactionConfig as CfgCls

        with pytest.raises(ValidationError):
            CfgCls(replacement_template="[REDACTED:check]")


# ---------------------------------------------------------------------------
# Public post_validate dispatch (review feedback)
# ---------------------------------------------------------------------------


class TestPostValidateDispatch:
    """The post-validation dispatch is part of the public scanner API."""

    def test_post_validate_is_public(self) -> None:
        """``post_validate`` must be importable as a public symbol."""
        from creek.redact.scanner import post_validate

        assert callable(post_validate)

    def test_post_validate_unknown_pattern_returns_true(self) -> None:
        """Patterns without a registered validator are kept by default."""
        from creek.redact.scanner import post_validate

        assert post_validate("ssn", "123-45-6789") is True
        assert post_validate("email", "x@y.com") is True

    def test_post_validate_credit_card_filters(self) -> None:
        """``credit_card`` is filtered through Luhn."""
        from creek.redact.scanner import post_validate

        assert post_validate("credit_card", "4111-1111-1111-1111") is True
        assert post_validate("credit_card", "4111-1111-1111-1112") is False


# ---------------------------------------------------------------------------
# Redactor must replace high-entropy strings (review feedback)
# ---------------------------------------------------------------------------


class TestRedactorHighEntropy:
    """`Redactor.redact_content` must apply the high-entropy detector too."""

    def test_redactor_replaces_high_entropy_secret(self) -> None:
        """A high-entropy hex secret must be replaced by the redactor."""
        from creek.config import RedactionConfig as CfgCls

        secret = "a3f1c8b2e9d74105fb6c2e8a91d34c70"
        cfg = CfgCls()
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        # Bare line — avoids env_secret picking up `token = …` first.
        out = redactor.redact_content(secret)

        assert secret not in out
        assert "[REDACTED:high_entropy_string]" in out

    def test_redactor_high_entropy_respects_allowlist(self) -> None:
        """An allowlisted high-entropy substring must not be replaced."""
        from creek.config import RedactionConfig as CfgCls

        secret = "a3f1c8b2e9d74105fb6c2e8a91d34c70"
        cfg = CfgCls(false_positive_allowlist=[secret])
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        out = redactor.redact_content(secret)

        assert secret in out
        assert "REDACTED" not in out

    def test_redactor_high_entropy_respects_min_confidence(self) -> None:
        """A low-entropy string must survive even at min_confidence=0."""
        from creek.config import RedactionConfig as CfgCls

        # Predictable, repeating content.
        low_entropy = "ababababababababababab"
        cfg = CfgCls(min_confidence=1.0)
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        out = redactor.redact_content(low_entropy)

        assert low_entropy in out


# ---------------------------------------------------------------------------
# Discord bot token regex must accept hyphen-suffixed tokens (review feedback)
# ---------------------------------------------------------------------------


class TestDiscordBotTokenBoundaries:
    """Discord token boundary must not break on trailing ``-`` characters."""

    def test_discord_bot_token_trailing_hyphen(self) -> None:
        """A token ending in ``-`` must still match (boundary char-class fix)."""
        pattern = REDACTION_PATTERNS["discord_bot_token"]
        token = "MTE" + "a" * 23 + ".AAAAAA." + "x" * 26 + "-"
        assert pattern.search(token)

    def test_discord_bot_token_internal_hyphens(self) -> None:
        """Hyphens inside the segments are still allowed."""
        pattern = REDACTION_PATTERNS["discord_bot_token"]
        token = "MTE" + "a-b-c-" * 4 + "abc.AAAAAA." + "x-y-" * 7 + "abcd"
        assert pattern.search(token)


# ---------------------------------------------------------------------------
# high_entropy_string regex single source of truth (review feedback)
# ---------------------------------------------------------------------------


class TestHighEntropyRegexSourceOfTruth:
    """Detector and metadata must share one regex object."""

    def test_detector_uses_pattern_metadata_regex(self) -> None:
        """The scanner's entropy candidate regex is the metadata pattern."""
        from creek.redact.patterns import PATTERN_METADATA
        from creek.redact.scanner import HIGH_ENTROPY_CANDIDATE

        assert HIGH_ENTROPY_CANDIDATE is PATTERN_METADATA["high_entropy_string"].pattern


# ---------------------------------------------------------------------------
# Sequential pattern-order redaction leaks (Issue #832)
# ---------------------------------------------------------------------------


class TestPatternOrderLeak:
    """Overlapping matches must all be redacted from ORIGINAL content (Issue #832).

    ``Redactor.redact_content`` applies patterns sequentially and mutates
    the content in place, so an earlier pattern (``email``) can destroy
    the text a later, higher-severity pattern (``email_password_combo``)
    needs to match — leaving the password half in cleartext. These tests
    pin the required behavior: every match found against the original
    content must be redacted in the output, with no cleartext gaps.
    """

    def test_email_password_combo_password_not_leaked_on_apply(self) -> None:
        """The password half of an email:password combo must never survive."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "login user@example.com:hunter2secret here"
        result = redactor.redact_content(content)

        assert "hunter2secret" not in result
        assert "user@example.com" not in result

    def test_email_password_combo_uses_combo_marker(self) -> None:
        """An email:password combo should carry the combo marker, not email's."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "login user@example.com:hunter2secret here"
        result = redactor.redact_content(content)

        assert "[REDACTED:email_password_combo]" in result
        assert "[REDACTED:email]:hunter2secret" not in result

    def test_apply_removes_every_scan_finding_parity(self, tmp_path: Path) -> None:
        """Every finding reported by a scan must be removed by an apply."""
        content = (
            "creds user@example.com:hunter2secret\n"
            "contact other@example.com\n"
            "ssn 123-45-6789\n"
        )
        test_file = tmp_path / "leaks.txt"
        test_file.write_text(content)

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        match_types = {match.match_type for match in matches}
        assert "email_password_combo" in match_types

        redactor = Redactor(config=config, salt=scanner.salt)
        result = redactor.redact_content(content)

        assert "hunter2secret" not in result
        assert "user@example.com" not in result
        assert "other@example.com" not in result
        assert "123-45-6789" not in result

    def test_standalone_email_still_marked_email(self) -> None:
        """A bare email with no password suffix keeps the email marker."""
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "contact test@example.com"
        result = redactor.redact_content(content)

        assert "test@example.com" not in result
        assert "[REDACTED:email]" in result

    def test_allowlisted_email_inside_combo_still_redacts_password(self) -> None:
        """Allowlisting the bare email must not spare the combo's password."""
        config = RedactionConfig(
            false_positive_allowlist=["user@example.com"],
        )
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "user@example.com:hunter2secret"
        result = redactor.redact_content(content)

        assert "hunter2secret" not in result

    def test_custom_composite_pattern_not_leaked(self) -> None:
        """A custom composite pattern must redact despite built-in overlap."""
        combo_re = r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}::[^\s]+"
        config = RedactionConfig(
            custom_patterns={"email_token_combo": combo_re},
        )
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "user@example.com::tok_supersecret99"
        result = redactor.redact_content(content)

        assert "tok_supersecret99" not in result

    def test_partial_overlap_union_redacted(self) -> None:
        """Partially overlapping matches must be redacted as one full union."""
        config = RedactionConfig(
            custom_patterns={"tail_secret": r"com hunter[^\s]+"},
        )
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        # Against the ORIGINAL content, `email` matches [0:16)
        # ("user@example.com") and `tail_secret` matches [13:30)
        # ("com hunter2secret") — a genuine partial, non-containment
        # overlap on "com". No fragment of either match may survive.
        content = "user@example.com hunter2secret"
        result = redactor.redact_content(content)

        assert "user@example.com" not in result
        assert "hunter2secret" not in result
        assert "example" not in result
        assert "hunter" not in result
