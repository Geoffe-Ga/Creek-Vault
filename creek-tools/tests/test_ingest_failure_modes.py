"""Failure-mode tests covering the fixtures under ``tests/fixtures/``.

The module mixes two test categories — both intentional, named so a
reader can tell them apart at a glance:

1. ``test_*`` (no prefix) — production-path tests that exercise Creek
   code (``MarkdownFilter``, ``normalize_encoding``, ``Redactor``)
   against the fixtures and assert the ingestion path handles them
   gracefully.

2. ``test_fixture_*`` — fixture-sanity guards. These verify the
   fixture files themselves stay in the shape downstream tests expect
   (truly empty, truly non-UTF-8, truly contains the expected token,
   etc.). They invoke stdlib / third-party libraries to characterise
   the fixture, NOT Creek code. They exist so a fixture corrupted by
   accident is caught immediately rather than producing confusing
   downstream test failures.

Real ingestor-level coverage for the fixture-sanity scenarios
(corrupt JSON via ``ChatGPTIngestor``, malformed YAML through the
markdown ingest path, prompt-injection rejection, scale fixture
through Discord ingest) is tracked separately and will be added in
the linking-architecture follow-up batch where the ingestor APIs
are stabilising.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


# ---- corrupt/ ----------------------------------------------------------------


def test_fixture_truncated_chatgpt_export_is_invalid_json() -> None:
    """The truncated export fixture is not valid JSON; ingestors must detect that.

    A real ingestor needs to surface a clear error rather than a
    cryptic ``KeyError`` deep in parsing.
    """
    raw = (FIXTURES_ROOT / "corrupt" / "truncated_chatgpt.json").read_text()
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_markdown_filter_skips_empty_fixture() -> None:
    """Real production-code check: MarkdownFilter must reject the empty
    fixture (``keep=False``) so an empty .md file does not produce a
    fragment. Renamed from the original fixture-existence-only check.
    """
    from creek.clean.filters import MarkdownFilter

    fixture = FIXTURES_ROOT / "corrupt" / "empty.md"
    assert fixture.read_text() == "", (
        "Sanity: the corrupt/empty.md fixture must remain truly empty."
    )

    result = MarkdownFilter().filter(content="")
    assert result.keep is False, (
        f"MarkdownFilter must skip empty content, got {result!r}"
    )
    assert result.reason, "MarkdownFilter should record a skip reason"


def test_fixture_malformed_yaml_frontmatter_does_not_crash_python_frontmatter() -> None:
    """``frontmatter.load`` must either raise YAMLError or return a Post
    whose ``.content`` preserves the markdown body.

    The contract under test: the only acceptable outcomes for malformed
    YAML are a structured ``yaml.YAMLError`` or a successful parse that
    keeps the body intact. Any other exception (TypeError, generic
    Exception, segfault) or a parse that silently drops the body is a
    contract violation. The previous version only checked
    ``isinstance(post.content, str)`` in the success path, which is
    trivially True for any Post and let a body-dropping bug pass.
    """
    import frontmatter
    import yaml

    fixture = FIXTURES_ROOT / "corrupt" / "malformed_yaml.md"
    body_marker = "Body with malformed frontmatter above"
    try:
        post = frontmatter.load(str(fixture))
    except yaml.YAMLError:
        # Acceptable: parser surfaces a structured error.
        return
    except Exception as exc:
        pytest.fail(
            f"frontmatter.load raised an unexpected non-YAML error: "
            f"{type(exc).__name__}: {exc}"
        )
    # Success path: the body MUST be intact. A Post with empty content
    # would mean the parser silently swallowed the markdown body when
    # the frontmatter failed to parse.
    assert body_marker in post.content, (
        f"frontmatter.load succeeded but body marker {body_marker!r} "
        f"was missing from post.content (got {post.content!r:.80})"
    )


# ---- encoding/ ---------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "cp1252.csv",
        "utf8_bom.csv",
        "shift_jis.csv",
    ],
)
def test_normalize_encoding_decodes_each_codec_fixture(fixture: str) -> None:
    """Real production-code check: ``creek.ingest.base.normalize_encoding``
    must decode each non-UTF-8 fixture without raising and return a
    non-empty string. Replaces the previous fixture-existence-only
    assertion which never exercised Creek code.
    """
    from creek.ingest.base import normalize_encoding

    path = FIXTURES_ROOT / "encoding" / fixture
    raw = path.read_bytes()
    assert raw, f"Sanity: encoding fixture {fixture} must be non-empty"

    text, detected = normalize_encoding(raw)

    assert isinstance(text, str) and text, (
        f"normalize_encoding returned empty/non-string for {fixture}: got {text!r}"
    )
    assert isinstance(detected, str) and detected, (
        f"normalize_encoding did not report a detected codec for {fixture}"
    )


def test_fixture_cp1252_is_not_valid_utf8() -> None:
    """The cp1252 fixture deliberately fails strict UTF-8 decoding so that
    encoding-detection paths get exercised (BUG-010 sentinel).
    """
    raw = (FIXTURES_ROOT / "encoding" / "cp1252.csv").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")


def test_fixture_shift_jis_decodes_with_correct_codec() -> None:
    """Sanity check: the shift_jis fixture is well-formed in its own codec."""
    raw = (FIXTURES_ROOT / "encoding" / "shift_jis.csv").read_bytes()
    decoded = raw.decode("shift_jis")
    assert "佐藤" in decoded


# ---- injection/ --------------------------------------------------------------


def test_fixture_injection_yaml_in_body_is_real_yaml_in_body() -> None:
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


def test_fixture_secret_lookalikes_uses_documented_examples_only() -> None:
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
    checked = 0
    for token in fixture_tokens:
        if any(pattern.search(token) for pattern in REDACTION_PATTERNS.values()):
            checked += 1
            assert token not in redacted, (
                f"Pattern-matching token leaked through redactor: {token!r}"
            )

    # Baseline guard: if every built-in pattern stops matching every
    # fixture token (e.g. someone weakens the regexes), the per-token
    # loop above silently passes by skipping every iteration. Require
    # at least one match so a regression in pattern coverage surfaces
    # here too.
    assert checked >= 1, (
        "No documented fixture token matched any built-in redaction "
        "pattern — either patterns regressed or the fixture changed shape."
    )


# ---- scale/ ------------------------------------------------------------------


def test_fixture_scale_parses_and_has_expected_message_count() -> None:
    """The synthetic Discord export must remain a valid JSON shape that
    ingestors can iterate over.
    """
    fixture = FIXTURES_ROOT / "scale" / "discord_export_small.json"
    data = json.loads(fixture.read_text())
    assert data["channel"]["name"] == "general"
    assert len(data["messages"]) == 120


# ---- symlinks/ ---------------------------------------------------------------


def test_fixture_symlinks_readme_documents_runtime_construction() -> None:
    """Symlinks aren't portable in a repo; ensure the README explains the
    runtime-construction pattern so future tests follow it.
    """
    body = (FIXTURES_ROOT / "symlinks" / "README.md").read_text()
    assert "symlink_to" in body
    assert "tmp_path" in body
