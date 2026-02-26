"""Tests for creek.redact — redaction scanner and redactor.

Tests cover:
- REDACTION_PATTERNS compilation and matching
- RedactionMatch model (must NOT store matched text, only salted hashes)
- RedactionScanner: scan_file, scan_directory, generate_report
- Redactor: redact_content, log_redactions
- False positive allowlisting
- Custom pattern support
- Security: ensure sensitive data never leaks into match objects
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
            assert isinstance(
                pattern, re.Pattern
            ), f"Pattern '{name}' is not a compiled regex"

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
            assert (
                field_name not in fields
            ), f"RedactionMatch must NOT store matched text (found '{field_name}')"

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
