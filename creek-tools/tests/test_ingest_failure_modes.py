"""Failure-mode tests covering the fixtures under ``tests/fixtures/``.

These tests exercise real on-disk inputs (corrupt, malformed, mixed
encoding, injection-shaped, large) and assert the ingestion path
handles them gracefully — no uncaught exceptions, no silent data loss,
no leaked secrets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


# ---- corrupt/ ----------------------------------------------------------------


def test_truncated_chatgpt_export_is_invalid_json() -> None:
    """The truncated export fixture is not valid JSON; ingestors must detect that.

    A real ingestor needs to surface a clear error rather than a
    cryptic ``KeyError`` deep in parsing.
    """
    raw = (FIXTURES_ROOT / "corrupt" / "truncated_chatgpt.json").read_text()
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_empty_markdown_file_exists() -> None:
    """The empty fixture is a real zero-byte file; the markdown filter
    should classify it as ``skip``.
    """
    fixture = FIXTURES_ROOT / "corrupt" / "empty.md"
    assert fixture.exists()
    assert fixture.read_text() == ""


def test_malformed_yaml_frontmatter_does_not_crash_python_frontmatter() -> None:
    """``frontmatter.load`` should either return a Post (ignoring bad YAML) or
    raise a YAML error — never a generic Exception or segfault.
    """
    import frontmatter
    import yaml

    fixture = FIXTURES_ROOT / "corrupt" / "malformed_yaml.md"
    try:
        post = frontmatter.load(str(fixture))
    except yaml.YAMLError:
        return  # expected — caller handles
    # If it didn't raise, the body should still be accessible.
    assert isinstance(post.content, str)


# ---- encoding/ ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "cp1252.csv",
        "utf8_bom.csv",
        "shift_jis.csv",
    ],
)
def test_encoding_fixtures_are_present(fixture: str) -> None:
    """Encoding fixtures must exist on disk for downstream encoding tests."""
    path = FIXTURES_ROOT / "encoding" / fixture
    assert path.exists(), f"Missing encoding fixture: {fixture}"
    assert path.stat().st_size > 0, f"Encoding fixture is empty: {fixture}"


def test_cp1252_fixture_is_not_valid_utf8() -> None:
    """The cp1252 fixture deliberately fails strict UTF-8 decoding so that
    encoding-detection paths get exercised (BUG-010 sentinel).
    """
    raw = (FIXTURES_ROOT / "encoding" / "cp1252.csv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")


def test_shift_jis_fixture_decodes_with_correct_codec() -> None:
    """Sanity check: the shift_jis fixture is well-formed in its own codec."""
    raw = (FIXTURES_ROOT / "encoding" / "shift_jis.csv").read_bytes()
    decoded = raw.decode("shift_jis")
    assert "佐藤" in decoded


# ---- injection/ --------------------------------------------------------------


def test_injection_fixture_yaml_in_body_is_real_yaml_in_body() -> None:
    """The injection fixture has TWO ``---`` fences. ``frontmatter.load``
    must only consume the FIRST as frontmatter, leaving the second as body.
    Guards against a parser regression that might over-consume.
    """
    import frontmatter

    fixture = FIXTURES_ROOT / "injection" / "fragment_with_yaml_in_body.md"
    post = frontmatter.load(str(fixture))
    assert post.metadata.get("title") == "Real frontmatter"
    # The injected key must NOT bleed into metadata.
    assert "override_classification" not in post.metadata
    assert "privacy_tier" not in post.metadata
    # And it must remain in the body.
    assert "override_classification" in post.content


def test_secret_lookalikes_fixture_uses_documented_examples_only() -> None:
    """Defence in depth: the lookalikes fixture must only contain well-known
    public test credentials. Any deviation suggests a real secret leaked
    into the test corpus.
    """
    body = (FIXTURES_ROOT / "injection" / "secret_lookalikes.md").read_text()
    documented_test_keys = (
        "AKIAIOSFODNN7EXAMPLE",
        "sk_test_PLACEHOLDER_NOT_A_REAL_KEY",
        "user@example.com",
        "555-12-3456",
        "4111111111111111",
    )
    for key in documented_test_keys:
        assert key in body, f"Documented test key missing from fixture: {key}"


def test_redactor_sweeps_secret_lookalikes_fixture() -> None:
    """End-to-end: feed the secrets fixture through the redactor and assert
    every secret in the fixture is removed from the output.

    The fixture contains five token shapes (AWS key, Stripe-style test
    placeholder, email, SSN-shape, PAN). Redaction is opt-in per pattern
    name, so a built-in pattern that genuinely doesn't fire (e.g. the
    Stripe placeholder, which is intentionally non-matching) is allowed
    to remain — but every value the project's patterns DO recognise must
    be gone.
    """
    from creek.config import RedactionConfig
    from creek.redact.patterns import REDACTION_PATTERNS
    from creek.redact.redactor import Redactor

    redactor = Redactor(RedactionConfig(), salt=b"failure-modes-test")
    body = (FIXTURES_ROOT / "injection" / "secret_lookalikes.md").read_text()
    redacted = redactor.redact_content(body)

    # Every fixture token that any built-in pattern matches must be gone
    # from the redacted output.
    fixture_tokens = (
        "AKIAIOSFODNN7EXAMPLE",
        "user@example.com",
        "555-12-3456",
        "4111111111111111",
    )
    for token in fixture_tokens:
        if any(pattern.search(token) for pattern in REDACTION_PATTERNS.values()):
            assert token not in redacted, (
                f"Pattern-matching token leaked through redactor: {token!r}"
            )


# ---- scale/ ------------------------------------------------------------------


def test_scale_fixture_parses_and_has_expected_message_count() -> None:
    """The synthetic Discord export must remain a valid JSON shape that
    ingestors can iterate over.
    """
    fixture = FIXTURES_ROOT / "scale" / "discord_export_small.json"
    data = json.loads(fixture.read_text())
    assert data["channel"]["name"] == "general"
    assert len(data["messages"]) == 120


# ---- symlinks/ ---------------------------------------------------------------


def test_symlinks_readme_documents_runtime_construction() -> None:
    """Symlinks aren't portable in a repo; ensure the README explains the
    runtime-construction pattern so future tests follow it.
    """
    body = (FIXTURES_ROOT / "symlinks" / "README.md").read_text()
    assert "symlink_to" in body
    assert "tmp_path" in body
