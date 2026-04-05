"""Tests for creek.clean.markdown_filter — markdown pre-ingestion filter.

Tests cover:
- MarkdownFilterResult model structure and constraints
- Empty/stub file detection (no body, body below minimum length)
- Frontmatter-only file detection
- Template residue detection (``{{...}}``, ``[FILL IN]``, ``TBD``, ``TODO``)
- Broken internal wiki-link detection (flag only, don't skip)
- Configurable minimum body length
- Integration of multiple filter rules
- Edge cases: whitespace-only body, valid content, mixed issues
"""

from pathlib import Path

from creek.clean.markdown_filter import (
    MarkdownFilter,
    MarkdownFilterResult,
)

# ---------------------------------------------------------------------------
# MarkdownFilterResult model
# ---------------------------------------------------------------------------


class TestMarkdownFilterResult:
    """Tests for the MarkdownFilterResult model."""

    def test_required_fields(self) -> None:
        """MarkdownFilterResult should have action, reasons, and warnings."""
        result = MarkdownFilterResult(
            action="accept",
            reasons=[],
            warnings=[],
        )
        assert result.action == "accept"
        assert result.reasons == []
        assert result.warnings == []

    def test_action_values(self) -> None:
        """Action should accept 'accept', 'review', and 'skip'."""
        for action in ("accept", "review", "skip"):
            result = MarkdownFilterResult(
                action=action,
                reasons=["test"],
                warnings=[],
            )
            assert result.action == action

    def test_reasons_and_warnings_are_lists(self) -> None:
        """Reasons and warnings should be lists of strings."""
        result = MarkdownFilterResult(
            action="skip",
            reasons=["empty body"],
            warnings=["broken link: [[missing]]"],
        )
        assert isinstance(result.reasons, list)
        assert isinstance(result.warnings, list)
        assert len(result.reasons) == 1
        assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# MarkdownFilter — Empty / stub detection
# ---------------------------------------------------------------------------


class TestMarkdownFilterEmpty:
    """Tests for empty and stub file detection."""

    def test_empty_string_skips(self) -> None:
        """Completely empty content should be skipped."""
        mf = MarkdownFilter()
        result = mf.filter("")
        assert result.action == "skip"

    def test_whitespace_only_skips(self) -> None:
        """Whitespace-only content should be skipped."""
        mf = MarkdownFilter()
        result = mf.filter("   \n\n  \t  ")
        assert result.action == "skip"

    def test_frontmatter_only_skips(self) -> None:
        """Files with only YAML frontmatter and no body should be skipped."""
        mf = MarkdownFilter()
        content = "---\ntitle: Test\ndate: 2024-01-01\n---\n"
        result = mf.filter(content)
        assert result.action == "skip"
        assert any("frontmatter" in r.lower() for r in result.reasons)

    def test_frontmatter_with_whitespace_body_skips(self) -> None:
        """Frontmatter followed by only whitespace should be skipped."""
        mf = MarkdownFilter()
        content = "---\ntitle: Test\n---\n\n   \n  "
        result = mf.filter(content)
        assert result.action == "skip"

    def test_body_below_minimum_skips(self) -> None:
        """Body shorter than min_body_length should be skipped."""
        mf = MarkdownFilter(min_body_length=10)
        content = "---\ntitle: X\n---\nShort"
        result = mf.filter(content)
        assert result.action == "skip"
        assert any(
            "short" in r.lower() or "minimum" in r.lower() for r in result.reasons
        )

    def test_body_at_minimum_accepts(self) -> None:
        """Body exactly at min_body_length should not be skipped."""
        mf = MarkdownFilter(min_body_length=10)
        content = "---\ntitle: X\n---\n0123456789"
        result = mf.filter(content)
        assert result.action != "skip" or not any(
            "short" in r.lower() or "minimum" in r.lower() for r in result.reasons
        )

    def test_no_frontmatter_empty_body_skips(self) -> None:
        """A file with no frontmatter and empty content should skip."""
        mf = MarkdownFilter()
        result = mf.filter("")
        assert result.action == "skip"

    def test_non_frontmatter_whitespace_body_skips(self) -> None:
        """Content with only non-printable chars should skip."""
        mf = MarkdownFilter()
        result = mf.filter("  \x00  ")
        assert result.action == "skip"

    def test_default_min_body_length(self) -> None:
        """Default min_body_length should be 10."""
        mf = MarkdownFilter()
        assert mf.min_body_length == 10


# ---------------------------------------------------------------------------
# MarkdownFilter — Template residue detection
# ---------------------------------------------------------------------------


class TestMarkdownFilterTemplateResidue:
    """Tests for detecting unfilled template markers."""

    def test_double_brace_template_flags_review(self) -> None:
        """Content with double-brace markers should be flagged."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nThis has {{placeholder}} in the text here."
        result = mf.filter(content)
        assert result.action == "review"
        assert any("template" in r.lower() for r in result.reasons)

    def test_fill_in_marker_flags_review(self) -> None:
        """Content with FILL IN markers should be flagged."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nPlease [FILL IN] the details and complete it."
        result = mf.filter(content)
        assert result.action == "review"

    def test_tbd_marker_flags_review(self) -> None:
        """Content with standalone TBD should be flagged."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nThe implementation details are TBD for now."
        result = mf.filter(content)
        assert result.action == "review"
        assert any("template" in r.lower() for r in result.reasons)

    def test_todo_marker_flags_review(self) -> None:
        """Content with standalone TODO should be flagged."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nThis section needs work. TODO: add context."
        result = mf.filter(content)
        assert result.action == "review"

    def test_multiple_template_markers(self) -> None:
        """Content with multiple template markers should report all."""
        mf = MarkdownFilter()
        content = (
            "---\ntitle: X\n---\n"
            "Section: {{topic}}\nStatus: TBD\n"
            "More text here to meet the minimum length."
        )
        result = mf.filter(content)
        assert result.action == "review"
        assert len(result.reasons) >= 1

    def test_template_markers_case_insensitive(self) -> None:
        """Template marker detection should be case-insensitive."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nThe plan is tbd for now. Need more info."
        result = mf.filter(content)
        assert result.action == "review"

    def test_no_template_markers_accepts(self) -> None:
        """Clean content without template markers should be accepted."""
        mf = MarkdownFilter()
        content = (
            "---\ntitle: Good Note\n---\n"
            "This is a well-written note with substantive content "
            "about quantum computing paradigms."
        )
        result = mf.filter(content)
        assert result.action == "accept"


# ---------------------------------------------------------------------------
# MarkdownFilter — Broken wiki-link detection
# ---------------------------------------------------------------------------


class TestMarkdownFilterBrokenLinks:
    """Tests for broken internal wiki-link detection."""

    def test_broken_link_adds_warning(self, tmp_path: Path) -> None:
        """Wiki-links to nonexistent files should produce warnings."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nSee [[nonexistent-note]] for more details."
        result = mf.filter(content, vault_path=tmp_path)
        assert result.action == "accept"
        assert any("nonexistent-note" in w for w in result.warnings)

    def test_valid_link_no_warning(self, tmp_path: Path) -> None:
        """Wiki-links to existing files should not produce warnings."""
        target = tmp_path / "existing-note.md"
        target.write_text("# Existing", encoding="utf-8")
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nSee [[existing-note]] for details about this."
        result = mf.filter(content, vault_path=tmp_path)
        assert not any("existing-note" in w for w in result.warnings)

    def test_broken_link_does_not_skip(self, tmp_path: Path) -> None:
        """Broken links should flag but NOT skip the file."""
        mf = MarkdownFilter()
        content = (
            "---\ntitle: X\n---\nThis references [[missing-page]] but has content."
        )
        result = mf.filter(content, vault_path=tmp_path)
        assert result.action != "skip"

    def test_multiple_broken_links(self, tmp_path: Path) -> None:
        """Multiple broken links should all be reported."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nSee [[link-a]] and [[link-b]] and [[link-c]]."
        result = mf.filter(content, vault_path=tmp_path)
        assert len(result.warnings) >= 3

    def test_no_vault_path_skips_link_check(self) -> None:
        """Without vault_path, broken link checks should be skipped."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nSee [[some-link]] for context about this topic."
        result = mf.filter(content)
        assert result.warnings == []


# ---------------------------------------------------------------------------
# MarkdownFilter — Valid content
# ---------------------------------------------------------------------------


class TestMarkdownFilterValidContent:
    """Tests for content that should pass all filters."""

    def test_well_formed_note_accepts(self) -> None:
        """A well-formed markdown note should be accepted."""
        mf = MarkdownFilter()
        content = (
            "---\ntitle: Quantum Computing\ndate: 2024-03-15\n---\n"
            "# Quantum Computing\n\n"
            "Quantum computing leverages quantum mechanical "
            "phenomena to process information differently."
        )
        result = mf.filter(content)
        assert result.action == "accept"
        assert result.reasons == []
        assert result.warnings == []

    def test_no_frontmatter_with_body_accepts(self) -> None:
        """A file without frontmatter but with body should accept."""
        mf = MarkdownFilter()
        content = (
            "This is a plain markdown file with enough content to pass all filters."
        )
        result = mf.filter(content)
        assert result.action == "accept"

    def test_custom_min_body_length(self) -> None:
        """MarkdownFilter should accept custom min_body_length."""
        mf = MarkdownFilter(min_body_length=5)
        content = "---\ntitle: X\n---\nHello"
        result = mf.filter(content)
        assert result.action == "accept"


# ---------------------------------------------------------------------------
# MarkdownFilter — Combined scenarios
# ---------------------------------------------------------------------------


class TestMarkdownFilterCombined:
    """Tests for interactions between filter rules."""

    def test_skip_takes_priority_over_review(self) -> None:
        """Empty body should skip even if template markers exist."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\n"
        result = mf.filter(content)
        assert result.action == "skip"

    def test_template_residue_with_broken_links(
        self,
        tmp_path: Path,
    ) -> None:
        """Template residue gives review, broken links add warnings."""
        mf = MarkdownFilter()
        content = "---\ntitle: X\n---\nStatus is TBD. See [[missing-ref]] for detail."
        result = mf.filter(content, vault_path=tmp_path)
        assert result.action == "review"
        assert len(result.reasons) >= 1
        assert len(result.warnings) >= 1
