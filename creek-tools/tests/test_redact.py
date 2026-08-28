"""Tests for creek.redact — redaction scanner and redactor.

Tests cover:
- REDACTION_PATTERNS compilation and matching
- RedactionMatch model (must NOT store matched text, only salted hashes)
- RedactionScanner: scan_file, scan_batch, generate_markdown_summary
- Redactor: redact_content
- False positive allowlisting
- Custom pattern support
- Security: ensure sensitive data never leaks into match objects
- Binary file detection (Issue #14)
- File extension filtering (Issue #14)
- Directory exclusion patterns (Issue #14)
- Context extraction (Issue #14)
- Markdown summary (Issue #14)
- Review queue generation (Issue #14)
- Batch scanning with ScanSummary (Issue #14)
"""

import ast
import inspect
import json
import random
import re
import string
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

    def test_scan_batch_skips_binary(self, tmp_path: Path) -> None:
        """scan_batch should skip binary files with supported extensions."""
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


# ---------------------------------------------------------------------------
# Progress Bar (Issue #14)
# ---------------------------------------------------------------------------


class TestProgressBar:
    """Tests for tqdm progress bar during scanning."""

    def test_scan_batch_with_progress(self, tmp_path: Path) -> None:
        """scan_batch with progress=True should not raise."""
        (tmp_path / "data.txt").write_text("email: test@example.com\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        summary = scanner.scan_batch(tmp_path, progress=True)

        assert summary.files_scanned >= 1


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
# has_high_entropy_region — the sub-run entropy gate (Issue #942)
# ---------------------------------------------------------------------------

# Geometry of the helper fixtures. Every figure is Shannon entropy in
# bits/char as measured by ``creek.redact.scanner.shannon_entropy``:
#   _CLEARING_20         20 chars, all distinct -> log2(20) = 4.3219, which
#                        is the ceiling for any 20-char window.
#   _WHOLE_RUN_CLEARING  29 chars, all distinct -> log2(29) = 4.8580. It
#                        clears a 4.5 threshold that NO 20-char window can
#                        ever reach, so a True there can only have come
#                        from the whole-run fast path.
#   _INERT_20            20 chars over an 8-symbol alphabet -> 2.9710.
#   _SHORT_HIGH_ENTROPY  10 chars, all distinct -> log2(10) = 3.3219, and
#                        shorter than HIGH_ENTROPY_MIN_RUN so there is no
#                        window to scan at all.
#   _FOUR_SYMBOL_RUN     60 chars over a 4-symbol alphabet -> 2.0000; no
#                        window can exceed log2(4) = 2.0 either, which is
#                        exactly what a distinct-character prefilter
#                        exploits to skip the window scan.
_CLEARING_20 = "Xq4Ln8Tz1Wm6Bd0Vs3Hy"  # pragma: allowlist secret
_WHOLE_RUN_CLEARING = _CLEARING_20 + "Jf5Cg2Kr9"
_INERT_20 = "abcdefghabcdefghabcd"
_SHORT_HIGH_ENTROPY = "Xq4Ln8Tz1W"  # pragma: allowlist secret
_FOUR_SYMBOL_RUN = "abcd" * 15

# Seeded corpus for the sliding-window oracle comparison. Seeded with a
# fixed value rather than generated by Hypothesis on purpose: an entropy
# value landing exactly on a threshold must never flake CI.
_ORACLE_SEED = 20260730
_ORACLE_SAMPLES = 500
_ORACLE_MAX_LENGTH = 120
_ORACLE_MAX_ALPHABET = 20
_ORACLE_THRESHOLDS = (2.5, 3.3, 3.7, 4.1, 4.5)


def _brute_force_has_high_entropy_region(
    text: str,
    threshold: float,
    min_run: int,
) -> bool:
    """Reference (naive) implementation of the sub-run entropy gate.

    Deliberately re-measures every window from scratch instead of carrying
    an incremental character-count accumulator. That is what makes it a
    trustworthy oracle: it has no shared state to get wrong.

    Args:
        text: The candidate run to measure.
        threshold: Entropy threshold in bits/char.
        min_run: Window width, i.e. ``HIGH_ENTROPY_MIN_RUN``.

    Returns:
        ``True`` when the whole run, or any contiguous *min_run*-character
        window of it, reaches *threshold*.
    """
    from creek.redact.scanner import shannon_entropy

    if not text:
        return False
    if shannon_entropy(text) >= threshold:
        return True
    return any(
        shannon_entropy(text[i : i + min_run]) >= threshold
        for i in range(len(text) - min_run + 1)
    )


def _oracle_corpus() -> list[str]:
    """Build the seeded random corpus for the sliding-window oracle test.

    Alphabet size is cycled from 1 to ``_ORACLE_MAX_ALPHABET`` symbols so
    low-diversity runs — the ones a distinct-character prefilter is meant
    to short-circuit — are as well represented as near-random ones.

    Returns:
        ``_ORACLE_SAMPLES`` candidate runs of length 1 to
        ``_ORACLE_MAX_LENGTH``, drawn from the HIGH_ENTROPY_CANDIDATE
        character class.
    """
    alphabet = string.ascii_letters + string.digits + "+/=_-"
    rng = random.Random(_ORACLE_SEED)
    corpus: list[str] = []
    for index in range(_ORACLE_SAMPLES):
        symbols = rng.sample(alphabet, 1 + index % _ORACLE_MAX_ALPHABET)
        length = rng.randint(1, _ORACLE_MAX_LENGTH)
        corpus.append("".join(rng.choice(symbols) for _ in range(length)))
    return corpus


class TestHasHighEntropyRegion:
    """Unit tests for the public ``has_high_entropy_region`` helper (#942).

    The helper is the single decision point both ``--scan``
    (``RedactionScanner._scan_high_entropy``) and ``--apply``
    (``Redactor._collect_high_entropy_spans``) consult in place of the old
    whole-run-average test. Its contract: ``True`` when the whole run
    clears *threshold*, or when any contiguous window of exactly
    ``HIGH_ENTROPY_MIN_RUN`` characters does.

    ``HIGH_ENTROPY_MIN_RUN`` is 20 because that is
    ``HIGH_ENTROPY_CANDIDATE``'s own ``{20,}`` floor — the smallest run the
    detector treats as a candidate at all. The invariant being restored is:
    a substring the detector would flag standing alone must not become
    undetectable merely by being concatenated to a neighbour.
    """

    def test_whole_run_above_threshold_short_circuits_true(self) -> None:
        """A run whose own entropy clears the bar needs no window scan.

        ``_WHOLE_RUN_CLEARING`` measures log2(29) = 4.8580 bits/char. At a
        4.5 threshold this can only be answered by the whole-run fast
        path, because a 20-char window tops out at log2(20) = 4.3219 and
        therefore can never clear 4.5.
        """
        from creek.redact.scanner import has_high_entropy_region

        assert has_high_entropy_region(_WHOLE_RUN_CLEARING, 4.5) is True
        assert has_high_entropy_region(_WHOLE_RUN_CLEARING, 3.7) is True

    def test_run_of_exactly_min_run_matches_whole_run_semantics(self) -> None:
        """At length exactly L the new gate reduces to the old one.

        This is the executable proof that issue #945 is not worsened.
        Every key in ``PATTERN_METADATA`` is at most
        ``HIGH_ENTROPY_MIN_RUN`` characters (pinned separately by
        ``test_pattern_metadata_names_fit_within_min_run``), so every
        emitted ``[REDACTED:<name>]`` marker's candidate run is at most L
        characters long. At exactly L there is a single window, identical
        to the whole run, so the sub-run gate returns exactly what the
        whole-run comparison returned — and the 0.016-bit margin that
        keeps ``[REDACTED:email_password_combo]`` (3.6842 bits/char
        against a 3.70 threshold) inert is untouched.
        """
        from creek.redact.patterns import HIGH_ENTROPY_MIN_RUN
        from creek.redact.scanner import has_high_entropy_region, shannon_entropy

        assert len(_CLEARING_20) == HIGH_ENTROPY_MIN_RUN
        assert len(_INERT_20) == HIGH_ENTROPY_MIN_RUN

        assert has_high_entropy_region(_CLEARING_20, 3.7) is True
        assert has_high_entropy_region(_CLEARING_20, 3.7) is (
            shannon_entropy(_CLEARING_20) >= 3.7
        )

        assert has_high_entropy_region(_INERT_20, 3.7) is False
        assert has_high_entropy_region(_INERT_20, 3.7) is (
            shannon_entropy(_INERT_20) >= 3.7
        )

    def test_run_shorter_than_min_run_uses_whole_run_only(self) -> None:
        """Below L there is no window, so whole-run semantics apply.

        ``_SHORT_HIGH_ENTROPY`` is 10 chars at log2(10) = 3.3219
        bits/char: inert at 3.7, flagged at 3.0. A window scan that
        silently measured a short tail (or ran off the end of the string)
        would break this.
        """
        from creek.redact.patterns import HIGH_ENTROPY_MIN_RUN
        from creek.redact.scanner import has_high_entropy_region, shannon_entropy

        assert len(_SHORT_HIGH_ENTROPY) < HIGH_ENTROPY_MIN_RUN

        assert has_high_entropy_region(_SHORT_HIGH_ENTROPY, 3.7) is False
        assert has_high_entropy_region(_SHORT_HIGH_ENTROPY, 3.0) is True
        assert has_high_entropy_region(_SHORT_HIGH_ENTROPY, 3.0) is (
            shannon_entropy(_SHORT_HIGH_ENTROPY) >= 3.0
        )

    def test_low_alphabet_run_cannot_clear_threshold(self) -> None:
        """A run over a 4-symbol alphabet is inert at 3.7 however long it is.

        Entropy is bounded above by log2(distinct symbols), so a 60-char
        run over 4 symbols cannot exceed 2.0 bits/char in *any* window.
        This is the case a distinct-character prefilter is allowed to
        short-circuit — and the case that proves the prefilter must not
        answer ``True`` by accident.
        """
        from creek.redact.scanner import has_high_entropy_region

        assert has_high_entropy_region(_FOUR_SYMBOL_RUN, 3.7) is False
        assert has_high_entropy_region(_FOUR_SYMBOL_RUN, 1.9) is True

    def test_empty_string_is_false(self) -> None:
        """The empty string must return False, not raise.

        A naive entropy computation would evaluate ``log2(0)``; the guard
        has to come first. The answer is ``False`` at every threshold,
        including the degenerate ``0.0`` a caller could pass directly to
        this public helper.
        """
        from creek.redact.scanner import has_high_entropy_region

        assert has_high_entropy_region("", 3.7) is False
        assert has_high_entropy_region("", 0.0) is False

    def test_sliding_window_agrees_with_bruteforce_oracle(self) -> None:
        """The incremental accumulator must equal a naive re-scan, exactly.

        The production helper keeps a running character-count accumulator
        so each window costs O(1) instead of O(L); this test is what makes
        that optimisation safe to ship in place of the roughly 48x-slower
        naive re-measurement. Every string in the corpus is checked at
        five thresholds against a literal transcription of the contract.

        The corpus is a seeded ``random.Random`` draw rather than a
        Hypothesis strategy on purpose: a float landing exactly on a
        threshold must produce the same verdict on every CI run, not a
        shrink-happy intermittent failure.
        """
        from creek.redact.patterns import HIGH_ENTROPY_MIN_RUN
        from creek.redact.scanner import has_high_entropy_region

        for text in _oracle_corpus():
            for threshold in _ORACLE_THRESHOLDS:
                expected = _brute_force_has_high_entropy_region(
                    text,
                    threshold,
                    HIGH_ENTROPY_MIN_RUN,
                )
                assert has_high_entropy_region(text, threshold) is expected, (
                    f"disagreed with the oracle at threshold {threshold} "
                    f"for a {len(text)}-char run: {text!r}"
                )


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
        """A high-entropy hex secret must be replaced by the redactor.

        Asserts the exact full output: a substring check would pass even
        if part of the secret leaked alongside the marker (Issue #909).
        """
        from creek.config import RedactionConfig as CfgCls

        secret = "a3f1c8b2e9d74105fb6c2e8a91d34c70"
        cfg = CfgCls()
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        # Bare line — avoids env_secret picking up `token = …` first.
        out = redactor.redact_content(secret)

        assert out == "[REDACTED:high_entropy_string]"

    def test_redactor_high_entropy_respects_allowlist(self) -> None:
        """An allowlisted high-entropy substring must not be replaced.

        Asserts the exact full output, so a partial rewrite around the
        allowlisted text cannot slip through (Issue #909).
        """
        from creek.config import RedactionConfig as CfgCls

        secret = "a3f1c8b2e9d74105fb6c2e8a91d34c70"
        cfg = CfgCls(false_positive_allowlist=[secret])
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        out = redactor.redact_content(secret)

        assert out == secret

    def test_redactor_high_entropy_respects_min_confidence(self) -> None:
        """A low-entropy string must survive at min_confidence=1.0.

        Asserts the exact full output rather than containment, so a
        partial redaction of the run would fail (Issue #909).
        """
        from creek.config import RedactionConfig as CfgCls

        # Predictable, repeating content.
        low_entropy = "ababababababababababab"
        cfg = CfgCls(min_confidence=1.0)
        scanner = RedactionScanner(config=cfg)
        redactor = Redactor(config=cfg, salt=scanner.salt)

        out = redactor.redact_content(low_entropy)

        assert out == low_entropy


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

    def test_candidate_regex_built_from_min_run_constant(self) -> None:
        """The candidate regex must be generated from HIGH_ENTROPY_MIN_RUN.

        Issue #942 adds a sub-run window of exactly
        ``HIGH_ENTROPY_MIN_RUN`` characters, chosen to equal the regex's
        own ``{20,}`` floor — the smallest run the detector considers a
        candidate at all. If the regex kept a separately hard-coded floor
        the two could drift apart, and a window narrower or wider than the
        candidate floor silently reopens the masking leak. Asserting the
        constant's own repetition suffix appears in the compiled pattern
        welds them together.
        """
        from creek.redact.patterns import HIGH_ENTROPY_MIN_RUN
        from creek.redact.scanner import HIGH_ENTROPY_CANDIDATE

        assert f"{{{HIGH_ENTROPY_MIN_RUN},}}" in HIGH_ENTROPY_CANDIDATE.pattern

    def test_pattern_metadata_names_fit_within_min_run(self) -> None:
        """Every pattern name must fit inside the sub-run window.

        Tripwire for issue #945. Each emitted ``[REDACTED:<name>]`` marker
        embeds its pattern name as a candidate run; while every name is at
        most ``HIGH_ENTROPY_MIN_RUN`` characters, that run is never longer
        than the window, so the sub-run gate reduces to the old whole-run
        comparison and marker inertness is unchanged. A future pattern
        name over 20 characters pushes markers into the sub-run regime,
        where a high-entropy window inside a marker could re-flag the
        marker itself — making #945 worse.
        """
        from creek.redact.patterns import HIGH_ENTROPY_MIN_RUN, PATTERN_METADATA

        for name in PATTERN_METADATA:
            assert len(name) <= HIGH_ENTROPY_MIN_RUN, (
                f"Pattern name {name!r} is {len(name)} chars, over the "
                f"{HIGH_ENTROPY_MIN_RUN}-char sub-run window"
            )


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

    def test_a_newline_straddling_password_survives_apply(self, tmp_path: Path) -> None:
        """A credential the scan reports must not survive the apply (#900).

        The scanner walks ``text.splitlines()`` and matches per line
        (``scanner.py:332``); the redactor matches the whole document
        (``redactor.py:379``). ``password``'s ``\\s*`` matches a newline, so on
        this input the whole-document pass matches ``'password =\npassword'``
        -- the span ENDS before the secret -- while the per-line pass matches
        ``'password = supersecret123'``.

        So ``--scan`` reports a critical finding and ``--apply`` writes the
        secret straight back out. Measured at HEAD before the fix.
        """
        content = "password =\npassword = supersecret123\n"
        leaky = tmp_path / "creds.txt"
        leaky.write_text(content, encoding="utf-8")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(leaky)
        assert [m.match_type for m in matches] == ["password"], (
            f"fixture no longer reproduces the scan finding: {matches}"
        )

        redactor = Redactor(config=config, salt=scanner.salt)
        result = redactor.redact_content(content)

        assert "supersecret123" not in result, (
            "the scan reported a critical `password` finding and the apply "
            f"left the secret in the output: {result!r}"
        )

    def test_a_custom_line_anchored_pattern_survives_apply(
        self, tmp_path: Path
    ) -> None:
        """The issue's original framing: a custom per-line pattern.

        ``$`` is not multiline by default, so a custom pattern anchored to
        end-of-line matches on every line under the scanner's per-line walk
        but only at the very end of the document under a whole-content pass.
        """
        content = "token: aaaa-secret-one\ntoken: bbbb-secret-two\n"
        leaky = tmp_path / "tokens.txt"
        leaky.write_text(content, encoding="utf-8")

        config = RedactionConfig(custom_patterns={"line_token": r"token: \S+$"})
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(leaky)
        assert len(matches) == 2, f"the scanner should report both lines, got {matches}"

        redactor = Redactor(config=config, salt=scanner.salt)
        result = redactor.redact_content(content)

        assert "aaaa-secret-one" not in result, (
            f"the first line's token survived the apply: {result!r}"
        )
        assert "bbbb-secret-two" not in result, (
            f"the second line's token survived the apply: {result!r}"
        )

    def test_a_whole_document_match_is_not_lost_by_the_per_line_pass(
        self,
    ) -> None:
        """Adding per-line collection must not remove whole-document coverage.

        This is the companion that stops the fix being a *replacement* rather
        than a union. A pattern whose match legitimately spans a newline is
        invisible to a per-line walk, so if the whole-content pass were
        dropped the coverage would silently narrow -- trading one parity gap
        for another, in the direction that leaks.
        """
        config = RedactionConfig(
            custom_patterns={"multi_line_secret": r"BEGIN-SECRET\s+\S+"},
        )
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        content = "BEGIN-SECRET\nspanning-secret-value\n"
        result = redactor.redact_content(content)

        assert "spanning-secret-value" not in result, (
            "a match that legitimately spans a newline is no longer redacted; "
            f"the whole-document pass was dropped instead of unioned: {result!r}"
        )

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


# ---------------------------------------------------------------------------
# High-entropy detector overlapping a regex pattern (Issue #909)
# ---------------------------------------------------------------------------

# Synthetic secret shapes, composed from parts so the leak geometry stays
# explicit: a documented AWS example key (covered by `api_key`, severity
# "critical") glued to a high-entropy remainder that only the
# `high_entropy_string` detector (severity "medium") covers.
_AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret
_ENTROPY_TAIL = "Zq7Wp3Rf9Tk2Ng"  # pragma: allowlist secret
_KEY_THEN_TAIL = f"{_AWS_EXAMPLE_KEY}{_ENTROPY_TAIL}"
_TAIL_THEN_KEY = f"{_ENTROPY_TAIL}{_AWS_EXAMPLE_KEY}"
_HEX_SECRET = "a3f1c8b2e9d74105fb6c2e8a91d34c70"  # pragma: allowlist secret
_REPEATING_RUN = "ababababababababababab"
# 24 chars drawn uniformly from an 8-symbol alphabet, so the Shannon
# entropy is exactly log2(8) = 3.0 bits/char: below the default 3.7
# threshold (min_confidence=0.6) but above the 2.5 floor
# (min_confidence=0.0). That window is what makes it a gating probe.
_SUB_THRESHOLD_RUN = "abcdefghabcdefghabcdefgh"
_ENTROPIC_EMAIL = "aB3xY7zQ9mK2pL5nR8vT4wd@example.com"  # pragma: allowlist secret
# A deliberately *predictable* tail: 14 repeats of one character. Glued to
# `_AWS_EXAMPLE_KEY` the combined 34-char run measures 3.1446 bits/char —
# BELOW the default 3.7 threshold — so the entropy detector contributes no
# span at all and only token-boundary snapping can keep the tail covered.
_LOW_ENTROPY_TAIL = "a" * 14
_KEY_THEN_LOW_ENTROPY_TAIL = f"{_AWS_EXAMPLE_KEY}{_LOW_ENTROPY_TAIL}"


class TestHighEntropyOverlapLeak:
    """Entropy hits must union with regex spans on the ORIGINAL text (#909).

    Issue #832 made the *regular* patterns collect spans against the
    untouched content, merge overlaps, and splice markers in one pass.
    The generic high-entropy detector was left as a post-pass over the
    ALREADY-MUTATED text, so when a regex match only partially covers a
    high-entropy run the uncovered remainder is no longer ≥20 contiguous
    base64url chars and silently survives in cleartext.

    Concretely, ``_AWS_EXAMPLE_KEY`` glued to ``_ENTROPY_TAIL`` returns
    the ``api_key`` marker followed by the 14-char tail in cleartext.
    Every core assertion here compares the **complete** returned string
    with ``==``: a ``not in`` assertion on the key alone passes while the
    bug is live, which is exactly how this shipped.
    """

    def test_entropy_tail_after_api_key_fully_redacted(self) -> None:
        """A trailing high-entropy remainder must not survive (RED at HEAD).

        The merged span is labelled by its most severe contributor, so the
        marker is ``api_key`` (critical), never ``high_entropy_string``
        (medium).
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_KEY_THEN_TAIL)

        assert result == "[REDACTED:api_key]"
        assert "[REDACTED:high_entropy_string]" not in result

    def test_sub_threshold_entropy_tail_after_api_key_still_redacted(self) -> None:
        """A sub-threshold tail of the SAME token must not leak (RED at HEAD).

        ``_KEY_THEN_LOW_ENTROPY_TAIL`` is one contiguous 34-char
        ``HIGH_ENTROPY_CANDIDATE`` run whose Shannon entropy is
        **3.145 bits/char — BELOW the default 3.7 threshold**
        (``min_confidence=0.6``). The entropy detector therefore does not
        fire at all, so span-unioning alone cannot cover the remainder:
        only **token-boundary snapping** — widening the ``api_key`` match
        outward to the enclosing run's boundaries — keeps the tail from
        leaking.

        At HEAD this returns ``[REDACTED:api_key]aaaaaaaaaaaaaa``: 14
        characters of the very token a *critical* ``api_key`` detector
        fired on survive in cleartext.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_KEY_THEN_LOW_ENTROPY_TAIL)

        assert result == "[REDACTED:api_key]"

    def test_entropy_tail_redacted_at_max_confidence(self) -> None:
        """The fix must not be contingent on a tuning knob (RED at HEAD).

        This pins **threshold-independence**. At ``min_confidence=1.0``
        the entropy threshold is 4.5 bits/char and ``_KEY_THEN_TAIL``
        measures 4.5725 — it clears by **0.07 bits**. That margin is
        luck, not design: nudge the tail's alphabet, the ceiling, or the
        confidence mapping and the union silently stops firing.

        A P1 secret-leak fix must hold at *every* confidence setting, so
        the covering mechanism has to be token-boundary snapping (a
        structural property of the matched run) rather than an entropy
        score that happens to land on the right side of a threshold.
        """
        config = RedactionConfig(min_confidence=1.0)
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_KEY_THEN_TAIL)

        assert result == "[REDACTED:api_key]"

    def test_entropy_tail_redacted_with_surrounding_text(self) -> None:
        """Surrounding plain words are preserved exactly (RED at HEAD).

        Pins that the union splice is positional — it removes the whole
        overlapping region and nothing else.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(f"prefix {_KEY_THEN_TAIL} suffix")

        assert result == "prefix [REDACTED:api_key] suffix"

    def test_entropy_prefix_before_api_key_fully_redacted(self) -> None:
        """A *leading* high-entropy remainder must not survive (RED at HEAD).

        The mirror image of the trailing case: at HEAD the output is
        ``Zq7Wp3Rf9Tk2Ng[REDACTED:api_key]``. This pins that the fix is a
        span union, not a "swallow the following characters" hack.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_TAIL_THEN_KEY)

        assert result == "[REDACTED:api_key]"

    def test_multiline_content_keeps_following_lines(self) -> None:
        """The union must stop at the newline it never matched (RED at HEAD).

        ``redact_content`` sees whole content while the scanner works
        line by line; this pins that widening the merged span does not
        swallow the line break or the inert text after it.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(f"{_KEY_THEN_TAIL}\nplain text\n")

        assert result == "[REDACTED:api_key]\nplain text\n"

    def test_scan_apply_parity_for_overlapping_entropy_match(
        self, tmp_path: Path
    ) -> None:
        """Every scan finding must be gone after apply (RED at HEAD).

        This is the Issue #832 invariant restored for the entropy
        detector: the scan reports both ``api_key`` and
        ``high_entropy_string``, so an apply must leave no cleartext
        fragment of either behind.

        The apply step redacts ``test_file.read_text()`` rather than the
        in-memory constant, so the parity claim genuinely round-trips
        through the same bytes the scanner read off disk.

        Args:
            tmp_path: pytest-provided temporary directory.
        """
        test_file = tmp_path / "entropy_leak.txt"
        test_file.write_text(_KEY_THEN_TAIL)

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        match_types = {match.match_type for match in matches}
        assert {"api_key", "high_entropy_string"}.issubset(match_types)

        redactor = Redactor(config=config, salt=scanner.salt)
        result = redactor.redact_content(test_file.read_text())

        assert result == "[REDACTED:api_key]"

    def test_bare_high_entropy_hex_secret_redacted_whole(self) -> None:
        """A lone high-entropy run is still redacted whole (non-regression).

        Entropy ≈3.95 bits/char clears the default 3.7 threshold, and no
        regex pattern overlaps, so the detector must fire on its own.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_HEX_SECRET)

        assert result == "[REDACTED:high_entropy_string]"

    def test_allowlisted_high_entropy_secret_survives_verbatim(self) -> None:
        """The allowlist still suppresses entropy hits (non-regression).

        The allowlist check must survive the move from a regex-``sub``
        replacer to a span collector.
        """
        config = RedactionConfig(false_positive_allowlist=[_HEX_SECRET])
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_HEX_SECRET)

        assert result == _HEX_SECRET

    def test_allowlisted_run_not_snapped(self) -> None:
        """An allowlisted run is never widened — explicit user intent wins.

        This documents a deliberate carve-out in the token-boundary
        snapping rule, with exact bytes so nobody can "tidy" it away.
        ``_KEY_THEN_TAIL`` is the whole 34-char run *and* the allowlist
        entry, so the entropy detector skips it and the snapping step
        must leave its boundaries alone.

        A regex match *inside* an allowlisted run still redacts its own
        span, though: the caller allowlisted the run, not the
        ``api_key`` hit within it. The exact expected output is therefore
        ``"[REDACTED:api_key]Zq7Wp3Rf9Tk2Ng"`` — marker for the 20-char
        AWS key, and the 14-char tail left verbatim by request.

        The inverse failure is what this guards: a snapping rule that
        ignored the allowlist would widen the ``api_key`` span to the
        full run and swallow bytes the user explicitly excused.
        """
        config = RedactionConfig(false_positive_allowlist=[_KEY_THEN_TAIL])
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_KEY_THEN_TAIL)

        assert result == f"[REDACTED:api_key]{_ENTROPY_TAIL}"

    def test_repeating_run_survives_at_max_confidence(self) -> None:
        """A predictable run is left intact at min_confidence=1.0.

        Non-regression: entropy 1.0 bits/char is far below the 4.5
        bits/char threshold that ``min_confidence=1.0`` demands.
        """
        config = RedactionConfig(min_confidence=1.0)
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_REPEATING_RUN)

        assert result == _REPEATING_RUN

    def test_sub_threshold_run_redacted_at_min_confidence_zero(self) -> None:
        """The threshold must still come from config (non-regression).

        ``_SUB_THRESHOLD_RUN`` sits at exactly 3.0 bits/char: inert at the
        default 3.7 threshold, flagged at the 2.5 floor that
        ``min_confidence=0.0`` selects. If a refactor hard-coded the
        threshold or dropped the config read, this test fails.
        """
        config = RedactionConfig(min_confidence=0.0)
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_SUB_THRESHOLD_RUN)

        assert result == "[REDACTED:high_entropy_string]"

    def test_sub_threshold_run_inert_at_default_confidence(self) -> None:
        """The same run is inert at the default threshold (non-regression).

        Pairs with the test above to bracket the 3.0 bits/char probe from
        both sides, so a threshold regression cannot hide behind a value
        that happens to be flagged either way.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_SUB_THRESHOLD_RUN)

        assert result == _SUB_THRESHOLD_RUN

    def test_pattern_types_without_entropy_detector_leaves_tail(self) -> None:
        """A narrowed ``pattern_types`` intentionally leaves the tail.

        Non-regression, and deliberately NOT a leak: the caller excluded
        ``high_entropy_string`` from scope, so the remainder is out of
        scope by request. Do NOT "fix" this to ``[REDACTED:api_key]`` —
        that would make the ``pattern_types`` filter a lie. The
        whole-string redaction is pinned by the sibling test that passes
        ``high_entropy_string`` explicitly.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_KEY_THEN_TAIL, pattern_types=["api_key"])

        assert result == f"[REDACTED:api_key]{_ENTROPY_TAIL}"

    def test_pattern_types_with_entropy_detector_redacts_whole_span(self) -> None:
        """The detector-only name works inside ``pattern_types`` (RED at HEAD).

        ``high_entropy_string`` lives in ``PATTERN_METADATA`` but is
        excluded from ``REDACTION_PATTERNS``, so it is absent from
        ``Redactor._patterns``; naming it must still bring the detector
        into scope and union its span with the ``api_key`` match.

        Opting the detector in explicitly did not save the tail at HEAD
        either: the old code spliced the regex span first and only then
        ran the entropy pass over the already-mutated text, so the
        remainder leaked exactly as in the unfiltered case.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(
            _KEY_THEN_TAIL,
            pattern_types=["api_key", "high_entropy_string"],
        )

        assert result == "[REDACTED:api_key]"

    def test_equal_severity_tie_break_prefers_widest_span(self) -> None:
        """Equal-severity overlaps fall to the widest span (non-regression).

        ``email`` and ``high_entropy_string`` are both "medium", and the
        entropy candidate (the local part) is strictly narrower than the
        email match, so the merged region must carry the ``email``
        marker.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(f"key {_ENTROPIC_EMAIL} end")

        assert result == "key [REDACTED:email] end"

    def test_overlapping_redaction_is_idempotent(self) -> None:
        """Re-redacting redacted output is a no-op (non-regression).

        The marker text itself must not look like a secret to any
        pattern, whatever the union logic produced on the first pass.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        once = redactor.redact_content(_KEY_THEN_TAIL)

        assert redactor.redact_content(once) == once

    def test_markers_are_inert_for_every_pattern_name(self) -> None:
        """Every replacement marker must survive a re-redaction unchanged.

        Marker inertness is not a comfortable margin — it sits on a
        **0.016-bit cliff**. ``[REDACTED:email_password_combo]`` contains
        the run ``email_password_combo``, which is exactly 20 characters
        (the ``HIGH_ENTROPY_CANDIDATE`` floor) at exactly **3.6842
        bits/char** against the default **3.7** threshold. Any change
        that widens a span outward to a candidate run, nudges the
        entropy threshold, renames a pattern, or adds a pattern name of
        20+ base64url characters can tip a marker over that edge — and
        then the marker itself is re-detected as a secret and rewritten
        into a nested ``[REDACTED:[REDACTED:...]]``.

        Asserting on the complete string for *every* name in
        ``PATTERN_METADATA`` makes that cliff visible the moment someone
        steps off it, rather than one release later in a corrupted
        vault.

        Deliberately **not** parametrised over ``min_confidence=0.0``:
        that genuinely fails today (the 2.5 floor flags the 3.6842-bit
        run) for a pre-existing reason outside the scope of #909, filed
        as a separate follow-up.
        """
        from creek.redact.patterns import PATTERN_METADATA

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        for name in PATTERN_METADATA:
            marker = f"[REDACTED:{name}]"
            assert redactor.redact_content(marker) == marker


# ---------------------------------------------------------------------------
# A secret masked by a low-entropy neighbour (Issue #942)
# ---------------------------------------------------------------------------

# Geometry of the masking fixtures. Entropy is Shannon entropy in
# bits/char, measured against the default 3.70 threshold that
# ``min_confidence=0.6`` selects:
#   _MASKED_SECRET        20 chars, all distinct -> log2(20) = 4.3219.
#                         Standing alone it clears 3.70 and is redacted at
#                         HEAD.
#   _MASKED_RUN           40 chars = 20x "a" + the secret -> 3.0160. One
#                         contiguous HIGH_ENTROPY_CANDIDATE run whose
#                         *average* falls under the threshold, so at HEAD
#                         the whole run — secret included — is returned in
#                         full cleartext by both --scan and --apply.
#   _SUFFIX_MASKED_RUN    34 chars = 14x "a" + the secret -> 3.3638; sits
#                         inside the measured 0%-detection band.
#   _MID_MASKED_RUN       34 chars = 7x "a" + secret + 7x "b" -> 3.8036.
#                         The masker is thin enough that the whole-run
#                         average still clears 3.70, so this one is
#                         already redacted at HEAD: it is a non-regression
#                         guard, not a leak probe.
#   _WIDE_MID_MASKED_RUN  44 chars = 12x "a" + secret + 12x "b" -> 3.3884:
#                         sub-threshold *and* the secret touches neither
#                         edge of the run.
#   _INERT_40_CHAR_RUN    40 chars over an 8-symbol alphabet -> 3.0000,
#                         best 20-char window 2.9710, and no redaction
#                         regex matches any part of it.
_MASKED_SECRET = "aB3xK9mQ7zR2vN5tL8wY"  # pragma: allowlist secret
_MASKED_RUN = "a" * 20 + _MASKED_SECRET
_SUFFIX_MASKED_RUN = "a" * 14 + _MASKED_SECRET
_MID_MASKED_RUN = "a" * 7 + _MASKED_SECRET + "b" * 7
_WIDE_MID_MASKED_RUN = "a" * 12 + _MASKED_SECRET + "b" * 12
_INERT_40_CHAR_RUN = "abcdefgh" * 5

# Realistic non-secrets the detector must leave alone. Each entry was
# measured against the detector; see the corpus test's docstring before
# touching any of them.
_NEGATIVE_CORPUS = (
    "antidisestablishmentarianism",  # 28ch 3.3388 / 3.4087 long single word
    "my-really-long-blog-post-title-about-nothing-at-all",  # 51ch 3.6958/3.4464
    "one_two_three_four_five_six_seven_eight_nine_ten",  # 48ch 3.5136 / 3.3842
    "creek-tools/creek/redact/scanner",  # 32ch 3.4917 / 3.4842 path ('/' matches)
    "550e8400-e29b-41d4-a716-446655440000",  # 36ch 3.3905 / 3.5464 UUID
    "2026-07-30T14_22_05_000000Z_snapshot",  # 36ch 3.6916 / 3.1842 timestamp
    "test_redaction_preserves_non_sensitive_text",  # 43ch 3.4672 / 3.5842
    "0000000000000000000000000000000042",  # 34ch 0.3816 / 0.5690 padded id
    "TODO_TODO_TODO_TODO_TODO_TODO_TODO_TODO",  # 39ch 1.9097 / 1.9219
    "the_the_the_the_the_the_the_the_the_the_",  # 40ch 2.0000 / 2.0000
    "ONE-TWO-THREE-FOUR-FIVE-SIX-SEVEN-EIGHT-NINE",  # 44ch 3.5615 / 3.3842
    "banana_banana_banana_banana_banana_banana",  # 41ch 1.8161 / 1.8710
    "a" * 40,  # 40ch 0.0000 / 0.0000
    "ab" * 20,  # 40ch 1.0000 / 1.0000
    "1234567890" * 4,  # 40ch 3.3219 / 3.3219 digit run
    "-" * 40,  # 40ch 0.0000 / 0.0000
    "_" * 40,  # 40ch 0.0000 / 0.0000
    "a" * 40 + "b" * 20,  # 60ch 0.9183 / 1.0000 two-segment low-entropy
)


class TestHighEntropyMaskedRunLeak:
    """A low-entropy neighbour must not hide a secret (Issue #942).

    Both call sites gate a ``HIGH_ENTROPY_CANDIDATE`` run on the *whole
    run's average* Shannon entropy. Concatenation is an averaging
    operation, so gluing predictable filler to a genuine secret drags the
    average under the threshold and the secret disappears from ``--scan``
    and ``--apply`` alike::

        "aB3xK9mQ7zR2vN5tL8wY"        -> 4.3219 bits/char, clears 3.70
        "a" * 20 + that same secret   -> 3.0160 bits/char, fails 3.70

    At HEAD ``redact_content`` returns that 40-char run verbatim: full
    cleartext. The fix routes both call sites through
    ``has_high_entropy_region``, which also measures every contiguous
    window of exactly ``HIGH_ENTROPY_MIN_RUN`` (= 20) characters — the
    candidate regex's own ``{20,}`` floor, i.e. the smallest run the
    detector treats as a candidate at all. The invariant restored is: a
    substring the detector would flag standing alone must not become
    undetectable merely by being concatenated to a neighbour.

    Every core assertion compares the **complete** returned string with
    ``==``. A ``not in`` assertion passes while the bug is live, which is
    exactly how the #909 leak shipped.
    """

    def test_masked_high_entropy_run_redacted_on_apply(self) -> None:
        """A masked secret must not survive --apply (RED at HEAD).

        The emitted span is the whole candidate run, so the entire 40-char
        run collapses to a single marker — the masker is part of the same
        token and has no business surviving either.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_MASKED_RUN)

        assert result == "[REDACTED:high_entropy_string]"

    def test_masked_high_entropy_run_reported_on_scan(self, tmp_path: Path) -> None:
        """--scan must report the masked run too (RED at HEAD).

        ``RedactionScanner._scan_high_entropy`` carries its own copy of the
        whole-run gate, so fixing only the redactor would leave the scan
        blind and silently break scan/apply parity in the other direction.

        Args:
            tmp_path: pytest-provided temporary directory.
        """
        test_file = tmp_path / "masked.txt"
        test_file.write_text(_MASKED_RUN + "\n")

        scanner = RedactionScanner(config=RedactionConfig())
        matches = scanner.scan_file(test_file)

        assert "high_entropy_string" in {match.match_type for match in matches}

    def test_suffix_masked_secret_in_32_to_39_band_redacted(self) -> None:
        """A 34-char masked run must be redacted (RED at HEAD).

        ``_SUFFIX_MASKED_RUN`` is 14 filler chars plus the 20-char secret,
        measuring 3.3638 bits/char against the 3.70 default. Run lengths
        32-39 (masker 12-19 characters) are the measured **0%-detection
        band**: every secret hidden behind a masker of that width escapes
        the whole-run gate entirely.

        This is also why an "only scan runs >= 40 chars" optimisation must
        never be added to the window scan: it would silently reopen this
        exact band while every other test in the class stayed green.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_SUFFIX_MASKED_RUN)

        assert result == "[REDACTED:high_entropy_string]"

    def test_mid_masked_secret_redacted(self) -> None:
        """A secret bracketed by filler on both sides must be redacted.

        GREEN at HEAD, and deliberately so: with only 7 filler characters
        per side the whole-run average is 3.8036 bits/char — still above
        3.70 — so the existing fast path already covers it. It is the
        non-regression half of the mid-position pair, pinning that adding
        a window scan does not somehow *lose* a run the whole-run gate
        used to catch. The RED half, where the secret touches neither edge
        *and* the average is sub-threshold, is
        ``test_wide_mid_masked_secret_redacted``.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_MID_MASKED_RUN)

        assert result == "[REDACTED:high_entropy_string]"

    def test_wide_mid_masked_secret_redacted(self) -> None:
        """A sub-threshold run with the secret mid-way must be redacted.

        ``_WIDE_MID_MASKED_RUN`` is 12 filler chars, the 20-char secret,
        then 12 more filler chars: 3.3884 bits/char overall, so the
        whole-run gate does not fire (RED at HEAD), and the secret touches
        neither edge. Together those two properties disqualify **both**
        the ">= 2L runs only" variant of the fix *and* every edge-anchored
        (prefix/suffix-window-only) variant: only a full sliding window
        finds it.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(_WIDE_MID_MASKED_RUN)

        assert result == "[REDACTED:high_entropy_string]"

    def test_masked_run_redacted_with_surrounding_text(self) -> None:
        """Surrounding plain words are preserved exactly (RED at HEAD).

        Pins that the splice stays positional: it removes the candidate
        run and nothing else, and the marker lands where the run was.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        result = redactor.redact_content(f"prefix {_MASKED_RUN} suffix")

        assert result == "prefix [REDACTED:high_entropy_string] suffix"

    def test_scan_apply_parity_for_masked_run(self, tmp_path: Path) -> None:
        """Everything --scan reports must be gone after --apply (RED at HEAD).

        The apply step redacts ``test_file.read_text()`` rather than the
        in-memory constant, so the parity claim genuinely round-trips
        through the same bytes the scanner read off disk.

        Args:
            tmp_path: pytest-provided temporary directory.
        """
        test_file = tmp_path / "masked_parity.txt"
        test_file.write_text(_MASKED_RUN)

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        assert "high_entropy_string" in {match.match_type for match in matches}

        redactor = Redactor(config=config, salt=scanner.salt)
        result = redactor.redact_content(test_file.read_text())

        assert result == "[REDACTED:high_entropy_string]"
        assert _MASKED_SECRET not in result

    def test_apply_leaves_run_that_scan_did_not_report(self, tmp_path: Path) -> None:
        """The reverse parity direction: no scan finding, no redaction.

        GREEN at HEAD by design — this is the guard that the fix does not
        become "redact everything". ``_INERT_40_CHAR_RUN`` is 3.0000
        bits/char whole-run and 2.9710 in its best 20-char window, so
        neither gate may fire at either call site.

        The fixture deliberately contains **no regex-pattern match at
        all**: ``--apply`` legitimately over-redacts relative to ``--scan``
        when a regex span bisects a candidate run and token snapping
        widens it (Issue #909, documented at
        ``creek/redact/redactor.py:28-33``), so a reverse-parity fixture
        that matched a regex would contradict
        ``test_allowlisted_run_not_snapped``.

        Args:
            tmp_path: pytest-provided temporary directory.
        """
        test_file = tmp_path / "inert.txt"
        test_file.write_text(_INERT_40_CHAR_RUN + "\n")

        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        assert "high_entropy_string" not in {match.match_type for match in matches}

        redactor = Redactor(config=config, salt=scanner.salt)
        result = redactor.redact_content(_INERT_40_CHAR_RUN)

        assert result == _INERT_40_CHAR_RUN

    def test_sub_run_detection_inert_at_max_confidence(self) -> None:
        """At min_confidence=1.0 no window can ever fire.

        The guarantee here is **mathematical, not empirical**: that
        setting selects a 4.5 bits/char threshold, and a window of exactly
        20 characters cannot exceed log2(20) = 4.3219 bits/char even when
        every character is distinct. No sub-run window can clear 4.5, at
        any input, ever — so the strictest confidence setting is provably
        unaffected by the new gate.
        """
        config = RedactionConfig(min_confidence=1.0)
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        assert redactor.redact_content(_MASKED_RUN) == _MASKED_RUN
        assert redactor.redact_content(_MID_MASKED_RUN) == _MID_MASKED_RUN

    def test_allowlisted_masked_run_skipped_entirely(self, tmp_path: Path) -> None:
        """An allowlisted run is never window-scanned (GREEN at HEAD).

        Pins that the allowlist check still runs BEFORE the entropy gate
        at both call sites: the fix replaces the ``shannon_entropy(...) <
        threshold`` comparison in place, it does not move or duplicate the
        allowlist test. A user who excused a run must keep getting it back
        verbatim, and must not see it appear in a scan report either.

        Args:
            tmp_path: pytest-provided temporary directory.
        """
        test_file = tmp_path / "allowlisted.txt"
        test_file.write_text(_MASKED_RUN + "\n")

        config = RedactionConfig(false_positive_allowlist=[_MASKED_RUN])
        scanner = RedactionScanner(config=config)
        matches = scanner.scan_file(test_file)
        assert "high_entropy_string" not in {match.match_type for match in matches}

        redactor = Redactor(config=config, salt=scanner.salt)

        assert redactor.redact_content(_MASKED_RUN) == _MASKED_RUN

    def test_realistic_non_secret_strings_stay_unredacted(self) -> None:
        """Ordinary text must survive the sub-run gate untouched.

        The literals in ``_NEGATIVE_CORPUS`` are **load-bearing**: each was
        measured against the detector and chosen because it is a realistic
        non-secret the detector must leave alone. Shortening, regenerating,
        or "simplifying" any of them silently destroys false-positive
        coverage while leaving the suite green. Each comment records the
        pair (whole-run entropy, best 20-char-window entropy) in bits/char;
        every member is below the 3.70 default threshold on *both*, which
        is why this test is green at HEAD and must stay green after the
        fix.

        The corpus is the cost side of the trade. At the default
        ``min_confidence=0.6`` the sub-run gate flags **+9.9%** more runs
        repo-wide (**+3.0%** on markdown); raising ``min_confidence`` to
        0.8 cuts that to **+1.2%**. Deleting members to quiet a future
        false positive hides that cost instead of tuning it.
        """
        config = RedactionConfig()
        scanner = RedactionScanner(config=config)
        redactor = Redactor(config=config, salt=scanner.salt)

        for text in _NEGATIVE_CORPUS:
            assert redactor.redact_content(text) == text, (
                f"false positive: {text!r} was rewritten"
            )


# ---------------------------------------------------------------------------
# Symlink containment at the walk (#1087)
#
# ``scan_batch`` selects its candidates with ``child.is_file()``, and
# ``is_file()`` follows symlinks — so a link inside the scanned root whose
# target resolves *outside* it is opened, matched, reported, and counted in
# ``files_scanned``. Three callers inherit the hole (``creek redact
# --scan/--apply/--review`` via ``_scan_source``, ``creek.redact.scan`` over
# MCP, and ``creek process`` via ``scan_batch``), which is why the tests
# below sit on the chokepoint rather than on any one caller.
#
# The admission policy under test is the one the shipped SEC-003 *write* guard
# already uses — ``creek/redact/cli_commands.py::_assert_no_escaping_symlinks``
# — resolve the root once, and for a child that *is* a symlink require its
# resolved target to be a descendant of that root. Non-symlink children are
# never resolved, which is what keeps a root reached through a symlinked
# component (``/tmp`` → ``/private/tmp`` on macOS) scannable.
# ---------------------------------------------------------------------------

_IN_ROOT_PII = "Contact: alice@example.com\n"
"""Payload for the in-root control file: exactly one ``email`` match.

Deliberately a different *pattern type* from :data:`_OUT_OF_ROOT_PII` rather
than merely different text. A :class:`~creek.redact.scanner.RedactionMatch`
carries a salted hash and never the matched string, so ``match_type`` is the
only observable that can say *which file* a finding came out of besides the
path — and asserting on both is what makes "nothing was read through the
link" hard to satisfy by accident.
"""

_OUT_OF_ROOT_PII = "SSN: 999-88-7777\n"
"""Payload for the file parked outside the scan root: exactly one ``ssn``."""

_IN_ROOT_MATCH_TYPE = "email"
"""The pattern name :data:`_IN_ROOT_PII` is expected to trigger."""

_OUT_OF_ROOT_MATCH_TYPE = "ssn"
"""The pattern name :data:`_OUT_OF_ROOT_PII` is expected to trigger."""


def _seed_scan_root(tmp_path: Path) -> Path:
    """Create a scan root holding one ordinary, in-root, PII-bearing file.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        The scan root, containing ``real.md``. Every test below keeps this
        file so that "the scan still works" is asserted alongside "the escape
        was declined" — a guard that fails a fix which simply stops scanning.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.md").write_text(_IN_ROOT_PII, encoding="utf-8")
    return root


def _seed_out_of_root_secret(tmp_path: Path) -> Path:
    """Create a PII-bearing file in a sibling directory of the scan root.

    Args:
        tmp_path: pytest-provided temporary directory.

    Returns:
        The out-of-root file. It is a sibling of the root rather than a
        hard-coded absolute path, so "outside the scanned tree" stays true by
        construction if the fixture layout ever moves.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.md"
    target.write_text(_OUT_OF_ROOT_PII, encoding="utf-8")
    return target


def test_scan_batch_skips_a_symlink_resolving_outside_the_scan_root(
    tmp_path: Path,
) -> None:
    """The chokepoint: an escaping symlink is not opened, reported, or counted.

    The twin of the MCP-boundary test in ``tests/test_mcp_redact.py``, aimed
    one layer down. It exists because the guard belongs at the *walk*: three
    independent callers reach this method, and a containment check installed
    in any one of them leaves the other two open.

    The behavioural assertion is ``files_scanned == 1`` — today the walk opens
    the link as well and reports ``2``. ``files_skipped_symlink`` is a new
    field, so that assertion cannot fail behaviourally today; it is stated
    next to the count it explains so the post-fix green means "declined one
    file" rather than merely "read one file".

    Non-vacuity is asserted in both directions: the out-of-root pattern type
    must be absent *and* the in-root pattern type must be present, so a fix
    that scanned nothing at all fails here.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    root = _seed_scan_root(tmp_path)
    outside = _seed_out_of_root_secret(tmp_path)
    (root / "link.md").symlink_to(outside)

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(root)

    assert summary.files_scanned == 1, (
        "scan_batch opened a symlinked child whose target resolves outside "
        "the scan root, so the count reports on a file the caller never "
        f"pointed it at.\n\nfiles_scanned: {summary.files_scanned}"
    )
    assert summary.files_skipped_symlink == 1, (
        "the escaping symlink was not accounted for: a safety scanner that "
        "silently declines to read a file has to say so.\n\n"
        f"files_skipped_symlink: {summary.files_skipped_symlink}"
    )
    reported = {match.file_path for match in summary.matches}
    assert reported == {root / "real.md"}, (
        "scan_batch reported a finding from outside the scan root.\n\n"
        f"reported: {sorted(str(path) for path in reported)}"
    )
    match_types = {match.match_type for match in summary.matches}
    assert _OUT_OF_ROOT_MATCH_TYPE not in match_types, (
        "the out-of-root file's PII type surfaced in the results, so its "
        f"content was read even if its path was not named.\n\n{match_types}"
    )
    assert _IN_ROOT_MATCH_TYPE in match_types, (
        "the in-root control produced no finding, so every assertion above "
        f"would also pass over a scan that read nothing.\n\n{match_types}"
    )


def test_scan_batch_admits_an_in_root_symlink(tmp_path: Path) -> None:
    """An alias staying inside the root is still scanned: the breadth guard.

    Mirrors ``tests/test_cli_redact.py::test_redact_apply_allows_internal_symlink``
    for the read path: the SEC-003 write guard permits intra-tree aliases, and
    the walk must draw the same line, or "fix the escape" quietly becomes
    "stop following symlinks", which is a different and unrequested change.

    Green today by construction — nothing about an in-root alias changes — and
    that is the point: it is the assertion a too-broad fix breaks. The only
    part that cannot pass today is ``files_skipped_symlink == 0``, which pins
    that the new counter counts *escapes* rather than symlinks.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    root = _seed_scan_root(tmp_path)
    (root / "alias.md").symlink_to(root / "real.md")

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(root)

    assert summary.files_scanned == 2, (
        "an intra-tree alias must still be scanned; the SEC-003 write guard "
        "permits it and the read path may not be stricter than the writer."
        f"\n\nfiles_scanned: {summary.files_scanned}"
    )
    assert summary.files_skipped_symlink == 0, (
        "an in-root alias was counted as an escape, so the counter is "
        "counting symlinks rather than escaping symlinks.\n\n"
        f"files_skipped_symlink: {summary.files_skipped_symlink}"
    )
    reported = {match.file_path for match in summary.matches}
    assert reported == {root / "real.md", root / "alias.md"}, (
        "both the real file and its in-root alias must report, each under "
        f"the name the walk opened it by.\n\n"
        f"reported: {sorted(str(path) for path in reported)}"
    )


def test_scan_batch_handles_a_circular_symlink(tmp_path: Path) -> None:
    """A loop (``a → b → a``) inside the root must not crash the walk.

    The discipline is copied from
    ``tests/test_cli_redact.py::test_redact_apply_handles_circular_symlink``:
    ``Path.resolve(strict=False)`` on a loop is interpreter- and
    platform-dependent (it may raise ``OSError``/``RuntimeError``, or return
    something surprising), so pinning a *disposition* here would pin an
    accident. The guarantee is that the containment check terminates and the
    rest of the tree is still scanned.

    The call itself is the assertion for "does not raise". The finding
    assertion that follows is the non-vacuity guard: a walk that aborted
    silently after the loop would produce no findings at all.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    root = _seed_scan_root(tmp_path)
    first = root / "a.md"
    second = root / "b.md"
    first.symlink_to(second)
    second.symlink_to(first)

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(root)

    match_types = {match.match_type for match in summary.matches}
    assert _IN_ROOT_MATCH_TYPE in match_types, (
        "the symlink loop swallowed the rest of the walk: the ordinary "
        f"in-root file was never scanned.\n\n{match_types}"
    )


def test_scan_batch_handles_a_dangling_symlink(tmp_path: Path) -> None:
    """A link to a path that does not exist is a non-candidate, not an escape.

    The disposition is pinned explicitly, and it is decidable rather than
    interpreter-dependent: the target named below is *inside* the root, so
    ``resolve(strict=False)`` lands under the resolved root and the link is
    not escaping — while ``is_file()`` is ``False`` for a broken link, so it
    never becomes a candidate either. It is therefore neither scanned nor
    counted as skipped, and that holds whichever order the implementation
    applies the two checks in.

    A dangling link pointing *outside* the root is deliberately not exercised
    here: its skip count depends on that ordering, and pinning it would pin an
    implementation detail rather than the contract.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    root = _seed_scan_root(tmp_path)
    (root / "dangling.md").symlink_to(root / "never-written.md")

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(root)

    assert summary.files_scanned == 1, (
        "a broken link is not a readable file and must not be counted as "
        f"one.\n\nfiles_scanned: {summary.files_scanned}"
    )
    assert summary.files_skipped_symlink == 0, (
        "a dangling link whose target path is inside the root resolves inside "
        "the root, so it is not an escape and must not inflate the escape "
        f"count.\n\nfiles_skipped_symlink: {summary.files_skipped_symlink}"
    )
    reported = {match.file_path for match in summary.matches}
    assert reported == {root / "real.md"}, (
        "the dangling link produced a finding, which means something opened "
        f"it.\n\nreported: {sorted(str(path) for path in reported)}"
    )


def test_scan_batch_does_not_descend_into_a_symlinked_directory(
    tmp_path: Path,
) -> None:
    """A symlinked *directory* out of the root contributes nothing.

    This pins the bound that makes a leaf-only ``is_symlink()`` check sound.
    ``Path.rglob`` yields a symlinked directory as an entry but does not
    descend into it, and the ``is_file()`` filter then drops the entry — so
    the whole residual is bounded to symlinked *files*. If a future Python
    flips the default of ``Path.glob(recurse_symlinks=...)``, the bound
    disappears and a per-leaf check stops being sufficient; this test is the
    canary for that day.

    Green today, and expected to stay green: it is a boundary pin, not a
    reproduction. ``files_skipped_symlink`` is deliberately *not* asserted —
    whether a symlinked directory reaches the escape check before the
    ``is_file()`` filter drops it is an ordering detail, not a contract.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    root = _seed_scan_root(tmp_path)
    outside = _seed_out_of_root_secret(tmp_path)
    (root / "linkdir").symlink_to(outside.parent, target_is_directory=True)

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(root)

    assert summary.files_scanned == 1, (
        "the walk descended into a symlinked directory pointing outside the "
        f"scan root.\n\nfiles_scanned: {summary.files_scanned}"
    )
    reported = {match.file_path for match in summary.matches}
    assert reported == {root / "real.md"}, (
        "a file underneath a symlinked directory was reported.\n\n"
        f"reported: {sorted(str(path) for path in reported)}"
    )
    match_types = {match.match_type for match in summary.matches}
    assert _OUT_OF_ROOT_MATCH_TYPE not in match_types, (
        "content from under the symlinked directory reached the results.\n\n"
        f"{match_types}"
    )
    assert _IN_ROOT_MATCH_TYPE in match_types, (
        "the in-root control produced no finding, so the assertions above "
        f"would pass over an empty scan.\n\n{match_types}"
    )


def test_scan_batch_accepts_a_scan_root_reached_through_a_symlinked_component(
    tmp_path: Path,
) -> None:
    """A root reached *through* a symlink is scanned, not treated as an escape.

    The real-world case rather than an exotic one: ``/tmp`` is a symlink to
    ``/private/tmp`` on macOS, and a vault under a synced or home-relative
    link hits the same shape. The naive containment check — resolve each child
    and compare it against the *unresolved* root — flags every child of such a
    root as escaping, which would turn this fix into a total outage on one
    platform. Resolving the root once, and only resolving children that are
    themselves symlinks, is what avoids it.

    The MCP-boundary twin of this case is ``tests/test_mcp_redact.py``'s
    ``test_scan_accepts_a_vault_root_with_a_symlinked_component``, which must
    keep passing unmodified.

    The reported path is asserted to be the *as-scanned* spelling (through the
    link), matching the #972 rule that a finding names the path the scanner
    opened rather than the path it resolves to.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    real = _seed_scan_root(tmp_path)
    linked = tmp_path / "linked-root"
    linked.symlink_to(real, target_is_directory=True)

    summary = RedactionScanner(config=RedactionConfig()).scan_batch(linked)

    assert summary.files_scanned == 1, (
        "a scan root reached through a symlinked component scanned nothing, "
        "so the containment check is comparing resolved children against an "
        f"unresolved root.\n\nfiles_scanned: {summary.files_scanned}"
    )
    assert summary.files_skipped_symlink == 0, (
        "an ordinary child of a symlink-reached root was counted as an "
        "escaping symlink; the child is not a symlink, the root is.\n\n"
        f"files_skipped_symlink: {summary.files_skipped_symlink}"
    )
    reported = {match.file_path for match in summary.matches}
    assert reported == {linked / "real.md"}, (
        "the finding must name the path as scanned, through the link the "
        "caller supplied.\n\n"
        f"reported: {sorted(str(path) for path in reported)}"
    )


# ---------------------------------------------------------------------------
# One walk, one gate (#1087)
# ---------------------------------------------------------------------------

_FILESYSTEM_WALK_CALLS = frozenset(
    {"glob", "iterdir", "listdir", "rglob", "scandir", "walk"},
)
"""Call names that enumerate a directory's children.

Both spellings are matched: the bound-method form (``dir_path.rglob``,
``os.walk``) and the bare form a ``from os import walk`` would produce.
"""

_SCANNABLE_CANDIDATES = "_scannable_candidates"
"""The one function in ``creek/redact/scanner.py`` allowed to walk the tree."""

_MODULE_SCOPE = "<module>"
"""Attribution for a walk call sitting at module level, outside any ``def``."""

_UNGUARDED_SCANNER_SOURCE = '''\
"""A scanner module that walks the tree behind its own helper's back."""


def _scannable_candidates(dir_path):
    """Admit the children that may be scanned."""
    return sorted(dir_path.rglob("*"))


def scan_batch(dir_path):
    """Enumerate the tree a second time, skipping the admission policy."""
    return [child for child in dir_path.rglob("*") if child.is_file()]
'''
"""Negative control for :func:`_filesystem_walk_sites`.

A detector that silently stopped finding anything would leave the guard below
green forever while the hole reopened. This source is the shape that must be
flagged, and :func:`test_the_walk_detector_flags_a_walk_outside_the_helper`
asserts it is.
"""


def _own_nodes(node: ast.AST) -> list[ast.AST]:
    """Return the nodes belonging to *node* itself, not to a nested ``def``.

    Mirrors the idiom in ``tests/test_mcp_auth.py``: ``def``/``async def``
    bodies are not descended into, so a call inside a nested helper is
    attributed to that helper alone and never double-counted against its
    parent.

    Args:
        node: The subtree root to walk.

    Returns:
        Every descendant that is part of *node*'s own code.
    """
    own: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        own.append(child)
        own.extend(_own_nodes(child))
    return own


def _walk_call_name(node: ast.AST) -> str | None:
    """Return the directory-enumeration call *node* makes, if it makes one.

    Args:
        node: Any AST node.

    Returns:
        The called name when *node* is a call to one of
        :data:`_FILESYSTEM_WALK_CALLS`, else ``None``.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _FILESYSTEM_WALK_CALLS:
        return func.attr
    if isinstance(func, ast.Name) and func.id in _FILESYSTEM_WALK_CALLS:
        return func.id
    return None


def _filesystem_walk_sites(source: str) -> set[tuple[str, str]]:
    """Return ``(enclosing function, call name)`` for every directory walk.

    Args:
        source: Python source text.

    Returns:
        One pair per distinct walk call site, attributed to the function whose
        own body contains it — or to :data:`_MODULE_SCOPE` for a call at
        module level.
    """
    tree = ast.parse(source)
    sites: set[tuple[str, str]] = set()
    for node in _own_nodes(tree):
        called = _walk_call_name(node)
        if called is not None:
            sites.add((_MODULE_SCOPE, called))
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in _own_nodes(function):
            called = _walk_call_name(node)
            if called is not None:
                sites.add((function.name, called))
    return sites


def _scanner_source() -> str:
    """Return the on-disk source of :mod:`creek.redact.scanner`.

    Returns:
        The module's text, located through the class it defines so the test
        cannot drift onto a stale copy of the file.
    """
    return Path(inspect.getfile(RedactionScanner)).read_text(encoding="utf-8")


def test_scan_batch_is_the_only_filesystem_walk_in_the_scanner_module() -> None:
    """Every directory walk in ``scanner.py`` goes through the admission helper.

    A containment policy applied at one of two walks is not a policy. This is
    the structural half of the fix: ``creek/redact/scanner.py`` may enumerate
    a directory in exactly one place, :data:`_SCANNABLE_CANDIDATES`, so a
    later "just add a quick rglob here" cannot reopen the hole while every
    behavioural test above stays green.

    Today the module's only walk is the ``rglob`` inlined in ``scan_batch``,
    so this fails with that call site named — a structural fact about the
    module, not a missing symbol.

    Scoped to ``scanner.py`` alone, deliberately. The shipped SEC-003 write
    guard in ``creek/redact/cli_commands.py`` runs its own ``os.walk``, and
    widening this rule across ``creek/redact/`` would demand a rewrite of a
    working guard that is out of scope for #1087.

    The non-vacuity guard is the first assertion: a detector that found no
    walk at all would satisfy the second one trivially, and
    :func:`test_the_walk_detector_flags_a_walk_outside_the_helper` covers the
    other way the detector could rot.
    """
    sites = _filesystem_walk_sites(_scanner_source())

    assert sites, (
        "the detector found no directory walk anywhere in scanner.py, so the "
        "guard below asserts nothing — either the module stopped walking the "
        "filesystem or _FILESYSTEM_WALK_CALLS no longer names how it does."
    )
    offenders = {site for site in sites if site[0] != _SCANNABLE_CANDIDATES}
    assert offenders == set(), (
        "creek/redact/scanner.py enumerates the filesystem outside "
        f"{_SCANNABLE_CANDIDATES}(), so the escaping-symlink policy is "
        "applied to some children and not others.\n\n"
        f"offending call sites: {sorted(offenders)}"
    )


def test_the_walk_detector_flags_a_walk_outside_the_helper() -> None:
    """The guard above is not a green no-op.

    Runs the same detector over :data:`_UNGUARDED_SCANNER_SOURCE`, a module
    shaped exactly like the failure the guard exists to catch: the admission
    helper is present *and* a second walk bypasses it. Both call sites must be
    seen, and the bypass must land in the offender set — otherwise the guard
    could pass on a scanner.py with the same defect.
    """
    sites = _filesystem_walk_sites(_UNGUARDED_SCANNER_SOURCE)

    assert (_SCANNABLE_CANDIDATES, "rglob") in sites, (
        "the detector missed the walk inside the admission helper, so it "
        f"cannot tell a guarded walk from an unguarded one.\n\n{sorted(sites)}"
    )
    offenders = {site for site in sites if site[0] != _SCANNABLE_CANDIDATES}
    assert offenders == {("scan_batch", "rglob")}, (
        "the detector did not flag a bare rglob sitting outside the "
        "admission helper, so the guard it powers would stay green over "
        f"exactly the regression it exists to catch.\n\n{sorted(offenders)}"
    )


# ---------------------------------------------------------------------------
# Reporting the skip (#1087)
#
# CrawDad posts ``report_markdown`` verbatim into a Discord channel
# (``crawdad/crawdad/bot.py::_format_scan_section``). A safety scanner that
# quietly declines to read a file is its own hazard, so the skip has to be
# visible in the document a human actually reads.
# ---------------------------------------------------------------------------

_EMPTY_SUMMARY_MARKDOWN = "# Redaction Scan Summary\n\nNo findings.\n"
"""The exact bytes an ordinary empty scan renders today.

Pinned so the new skip wording cannot be bought by changing what every
existing consumer of a clean scan sees.
"""

_SYMLINK_SKIP_ROW = "- **Files skipped (escaping symlink)**: 1"
"""The statistics line reporting one declined symlink."""


def test_markdown_summary_names_a_symlink_skip_when_there_are_no_findings() -> None:
    """A clean scan that declined to read a file may not report "No findings.".

    The one arm where silence is most likely and most misleading: the scan
    found nothing *because* it did not look, and the current early return says
    only ``No findings.`` — which a reader takes as "this tree is clean".

    The second assertion is the control that keeps the first from being paid
    for out of everyone else's pocket: an ordinary empty scan, with nothing
    skipped, must render byte-identical output to today's.
    """
    scanner = RedactionScanner(config=RedactionConfig())

    skipped = scanner.generate_markdown_summary(
        ScanSummary(files_skipped_symlink=1),
    )

    assert skipped != _EMPTY_SUMMARY_MARKDOWN, (
        "a scan that declined to read a file rendered the same 'No findings.' "
        f"document as a scan that read everything.\n\n{skipped!r}"
    )
    assert "skipped" in skipped.lower(), (
        f"the summary does not say that anything was skipped.\n\n{skipped!r}"
    )
    assert "1" in skipped, (
        f"the summary does not say how many files were skipped.\n\n{skipped!r}"
    )
    unchanged = scanner.generate_markdown_summary(ScanSummary())
    assert unchanged == _EMPTY_SUMMARY_MARKDOWN, (
        "an ordinary empty scan's rendering changed for every consumer that "
        f"skipped nothing.\n\n{unchanged!r}"
    )


def test_markdown_summary_renders_the_symlink_skip_row_in_the_statistics_block(
    tmp_path: Path,
) -> None:
    """The skip is reported alongside the other two skip counters.

    ``files_skipped_binary`` and ``files_skipped_extension`` are already
    rendered in the Statistics block; a third kind of skip that is not would
    be the one a reviewer never learns about.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    (tmp_path / "data.txt").write_text(_IN_ROOT_PII, encoding="utf-8")
    scanner = RedactionScanner(config=RedactionConfig())
    scanned = scanner.scan_batch(tmp_path)

    summary = ScanSummary(
        matches=scanned.matches,
        files_scanned=scanned.files_scanned,
        files_skipped_symlink=1,
    )
    markdown = scanner.generate_markdown_summary(summary)

    assert _SYMLINK_SKIP_ROW in markdown, (
        "the Statistics block does not report the escaping-symlink skip "
        f"beside the binary and extension skips.\n\n{markdown}"
    )


def test_markdown_summary_omits_the_symlink_skip_row_when_none_were_skipped(
    tmp_path: Path,
) -> None:
    """A scan that skipped nothing says nothing about skipping.

    The over-reporting guard, and green today. Without it the row could be
    rendered unconditionally, which puts a permanent "escaping symlink" line
    in front of every CrawDad operator whose tree has never contained one —
    the fastest way to teach a reader to ignore it.

    Args:
        tmp_path: pytest-provided temporary directory.
    """
    (tmp_path / "data.txt").write_text(_IN_ROOT_PII, encoding="utf-8")
    scanner = RedactionScanner(config=RedactionConfig())
    summary = scanner.scan_batch(tmp_path)

    markdown = scanner.generate_markdown_summary(summary)

    assert "escaping symlink" not in markdown, (
        "the escaping-symlink row is rendered for a scan that declined "
        f"nothing.\n\n{markdown}"
    )
    assert "Files scanned" in markdown, (
        f"the Statistics block did not render at all.\n\n{markdown}"
    )
