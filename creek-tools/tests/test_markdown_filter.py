"""Tests for creek.clean.filters.markdown -- Markdown pre-ingestion filter.

Tests cover:
- FilterResult model structure and defaults
- Empty and whitespace-only file rejection
- Stub file rejection (body below configurable minimum)
- Frontmatter-only file rejection
- Template residue detection ({{...}}, [FILL IN], [TBD], TODO:, FIXME:)
- Broken wiki-link detection and warnings
- Configurable min_body_length threshold
- Files with valid content that should pass through
- Edge cases: malformed frontmatter, mixed warnings, no vault_path
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.clean.filters import FilterResult, MarkdownFilter

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# FilterResult model
# ---------------------------------------------------------------------------


class TestFilterResult:
    """Tests for the FilterResult model."""

    def test_keep_true_defaults(self) -> None:
        """FilterResult with keep=True should have no reason and empty warnings."""
        result = FilterResult(keep=True)
        assert result.keep is True
        assert result.reason is None
        assert result.warnings == []

    def test_keep_false_with_reason(self) -> None:
        """FilterResult with keep=False should accept a reason string."""
        result = FilterResult(keep=False, reason="empty body")
        assert result.keep is False
        assert result.reason == "empty body"

    def test_warnings_list(self) -> None:
        """FilterResult should accept a list of warning strings."""
        result = FilterResult(
            keep=True,
            warnings=["template residue detected", "broken link: [[missing]]"],
        )
        assert len(result.warnings) == 2
        assert "template residue detected" in result.warnings

    def test_all_fields_populated(self) -> None:
        """FilterResult should allow all fields to be populated."""
        result = FilterResult(
            keep=False,
            reason="stub file",
            warnings=["has template markers"],
        )
        assert result.keep is False
        assert result.reason == "stub file"
        assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# MarkdownFilter — empty / whitespace-only files
# ---------------------------------------------------------------------------


class TestMarkdownFilterEmpty:
    """Tests for filtering empty and whitespace-only files."""

    def test_empty_string_skip(self) -> None:
        """Empty string should be skipped."""
        f = MarkdownFilter()
        result = f.filter("")
        assert result.keep is False
        assert result.reason is not None
        assert "empty" in result.reason.lower() or "body" in result.reason.lower()

    def test_whitespace_only_skip(self) -> None:
        """Whitespace-only content should be skipped."""
        f = MarkdownFilter()
        result = f.filter("   \n\n\t  \n")
        assert result.keep is False

    def test_newlines_only_skip(self) -> None:
        """Content with only newlines should be skipped."""
        f = MarkdownFilter()
        result = f.filter("\n\n\n")
        assert result.keep is False


# ---------------------------------------------------------------------------
# MarkdownFilter — frontmatter-only files
# ---------------------------------------------------------------------------


class TestMarkdownFilterFrontmatterOnly:
    """Tests for filtering files that contain only YAML frontmatter."""

    def test_frontmatter_only_skip(self) -> None:
        """File with only frontmatter and no body should be skipped."""
        content = "---\ntitle: Test\ntags: [a, b]\n---\n"
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is False
        assert result.reason is not None
        assert "frontmatter" in result.reason.lower() or "body" in result.reason.lower()

    def test_frontmatter_with_whitespace_only_body_skip(self) -> None:
        """Frontmatter followed by whitespace-only body should be skipped."""
        content = "---\ntitle: Test\n---\n\n   \n\t\n"
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is False

    def test_frontmatter_with_real_body_keep(self) -> None:
        """Frontmatter followed by a real body should be kept."""
        content = (
            "---\ntitle: Test\n---\n\n"
            "This is a meaningful body with enough content to pass."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True

    def test_no_frontmatter_with_body_keep(self) -> None:
        """File with no frontmatter but real body should be kept."""
        content = "This is a plain markdown file with sufficient content."
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True


# ---------------------------------------------------------------------------
# MarkdownFilter — stub file detection (body below minimum)
# ---------------------------------------------------------------------------


class TestMarkdownFilterStub:
    """Tests for filtering stub files with body below minimum length."""

    def test_short_body_skip(self) -> None:
        """Body shorter than min_body_length should be skipped."""
        content = "---\ntitle: Stub\n---\nHi"
        f = MarkdownFilter(min_body_length=10)
        result = f.filter(content)
        assert result.keep is False
        assert result.reason is not None

    def test_body_exactly_at_minimum_keep(self) -> None:
        """Body exactly at min_body_length should be kept."""
        content = "---\ntitle: T\n---\n0123456789"
        f = MarkdownFilter(min_body_length=10)
        result = f.filter(content)
        assert result.keep is True

    def test_body_just_below_minimum_skip(self) -> None:
        """Body one char below min_body_length should be skipped."""
        content = "---\ntitle: T\n---\n012345678"
        f = MarkdownFilter(min_body_length=10)
        result = f.filter(content)
        assert result.keep is False

    def test_custom_min_body_length(self) -> None:
        """Custom min_body_length should be respected."""
        content = "---\ntitle: T\n---\nABC"
        strict = MarkdownFilter(min_body_length=50)
        lenient = MarkdownFilter(min_body_length=2)
        assert strict.filter(content).keep is False
        assert lenient.filter(content).keep is True

    def test_no_frontmatter_short_body_skip(self) -> None:
        """Short content without frontmatter should also be skipped."""
        f = MarkdownFilter(min_body_length=10)
        result = f.filter("Hi")
        assert result.keep is False


# ---------------------------------------------------------------------------
# MarkdownFilter — template residue detection
# ---------------------------------------------------------------------------


class TestMarkdownFilterTemplateResidue:
    """Tests for detecting unfilled template markers."""

    def test_double_curly_braces_warning(self) -> None:
        """Content with {{...}} should produce a template residue warning."""
        content = (
            "---\ntitle: T\n---\nThis is a note about {{topic}} that needs filling in."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert any("template" in w.lower() for w in result.warnings)

    def test_fill_in_marker_warning(self) -> None:
        """Content with [FILL IN] should produce a template residue warning."""
        content = "---\ntitle: T\n---\nPlease [FILL IN] the details here soon."
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert any("template" in w.lower() for w in result.warnings)

    def test_tbd_marker_warning(self) -> None:
        """Content with [TBD] should produce a template residue warning."""
        content = "---\ntitle: T\n---\nThe deadline is [TBD] for this project."
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert any("template" in w.lower() for w in result.warnings)

    def test_todo_marker_warning(self) -> None:
        """Content with TODO: should produce a template residue warning."""
        content = (
            "---\ntitle: T\n---\nThis section needs work. TODO: add examples here."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert any("template" in w.lower() for w in result.warnings)

    def test_fixme_marker_warning(self) -> None:
        """Content with FIXME: should produce a template residue warning."""
        content = (
            "---\ntitle: T\n---\n"
            "The formula is incorrect. FIXME: verify calculation steps."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert any("template" in w.lower() for w in result.warnings)

    def test_no_template_markers_no_warning(self) -> None:
        """Content without template markers should have no template warnings."""
        content = (
            "---\ntitle: Clean\n---\n"
            "This is a clean note with no template markers at all."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert not any("template" in w.lower() for w in result.warnings)

    def test_multiple_template_markers(self) -> None:
        """Content with multiple different markers should produce warnings."""
        content = (
            "---\ntitle: T\n---\n"
            "Section: {{section_name}}\n"
            "Status: [TBD]\n"
            "TODO: write the rest\n"
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert len(result.warnings) >= 1


# ---------------------------------------------------------------------------
# MarkdownFilter — broken wiki-link detection
# ---------------------------------------------------------------------------


class TestMarkdownFilterBrokenLinks:
    """Tests for detecting broken internal wiki-links."""

    def test_broken_link_warning(self, tmp_path: Path) -> None:
        """Wiki-link to nonexistent file should produce a warning."""
        vault = tmp_path / "vault"
        vault.mkdir()
        content = (
            "---\ntitle: T\n---\nSee [[nonexistent-note]] for details on this topic."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert result.keep is True
        assert any("nonexistent-note" in w for w in result.warnings)

    def test_valid_link_no_warning(self, tmp_path: Path) -> None:
        """Wiki-link to existing file should not produce a warning."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "existing-note.md").write_text("# Existing Note\nContent here.")
        content = (
            "---\ntitle: T\n---\nSee [[existing-note]] for more details on this topic."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert not any("existing-note" in w for w in result.warnings)

    def test_no_vault_path_skips_link_check(self) -> None:
        """When vault_path is None, broken links should not be checked."""
        content = (
            "---\ntitle: T\n---\n"
            "See [[some-note]] for details on this particular topic."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert not any("some-note" in w for w in result.warnings)

    def test_multiple_broken_links(self, tmp_path: Path) -> None:
        """Multiple broken links should each produce a warning."""
        vault = tmp_path / "vault"
        vault.mkdir()
        content = (
            "---\ntitle: T\n---\nSee [[missing-a]] and [[missing-b]] for more details."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert any("missing-a" in w for w in result.warnings)
        assert any("missing-b" in w for w in result.warnings)

    def test_mixed_valid_and_broken_links(self, tmp_path: Path) -> None:
        """Mix of valid and broken links should only warn about broken ones."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "good-note.md").write_text("# Good\nContent.")
        content = (
            "---\ntitle: T\n---\nSee [[good-note]] and [[bad-note]] for more details."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert not any("good-note" in w for w in result.warnings)
        assert any("bad-note" in w for w in result.warnings)

    def test_nested_vault_file(self, tmp_path: Path) -> None:
        """Wiki-link to a file in a subdirectory should resolve correctly."""
        vault = tmp_path / "vault"
        subdir = vault / "subdir"
        subdir.mkdir(parents=True)
        (subdir / "nested-note.md").write_text("# Nested\nContent.")
        content = (
            "---\ntitle: T\n---\n"
            "See [[nested-note]] for details on this particular item."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert not any("nested-note" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# MarkdownFilter — combined scenarios
# ---------------------------------------------------------------------------


class TestMarkdownFilterCombined:
    """Tests for combined filter scenarios."""

    def test_valid_file_passes(self) -> None:
        """A well-formed markdown file should pass all filters."""
        content = (
            "---\ntitle: Good Note\ntags: [python]\n---\n\n"
            "# Good Note\n\n"
            "This is a well-written note with enough content to pass "
            "all the quality filters without any issues."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True
        assert result.reason is None
        assert result.warnings == []

    def test_stub_with_template_residue(self) -> None:
        """A stub file should be skipped regardless of template warnings."""
        content = "---\ntitle: T\n---\nHi {{name}}"
        f = MarkdownFilter(min_body_length=20)
        result = f.filter(content)
        assert result.keep is False

    def test_template_and_broken_link_warnings(self, tmp_path: Path) -> None:
        """A valid file can have both template and broken-link warnings."""
        vault = tmp_path / "vault"
        vault.mkdir()
        content = (
            "---\ntitle: T\n---\n"
            "This is enough body content to pass the minimum length.\n"
            "Reference: [[missing-ref]]\n"
            "Status: [TBD]\n"
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert result.keep is True
        assert any("missing-ref" in w for w in result.warnings)
        assert any("template" in w.lower() for w in result.warnings)

    def test_no_frontmatter_valid_body(self) -> None:
        """A file with no frontmatter but valid body should be kept."""
        content = "This is a plain file with enough content to be meaningful."
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True

    def test_malformed_frontmatter_treated_as_body(self) -> None:
        """Malformed frontmatter should be treated as body content."""
        content = "---\ntitle: incomplete\nThis is not valid YAML frontmatter."
        f = MarkdownFilter()
        result = f.filter(content)
        # Should still be kept since the total content is long enough
        assert isinstance(result, FilterResult)


# ---------------------------------------------------------------------------
# MarkdownFilter — edge cases
# ---------------------------------------------------------------------------


class TestMarkdownFilterEdgeCases:
    """Tests for edge cases and unusual inputs."""

    def test_frontmatter_delimiters_in_body(self) -> None:
        """Triple dashes in body should not be confused with frontmatter."""
        content = (
            "---\ntitle: T\n---\n\n"
            "Here is some content.\n\n"
            "---\n\n"
            "This is a horizontal rule, not frontmatter."
        )
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True

    def test_empty_frontmatter_with_body(self) -> None:
        """Empty frontmatter block followed by body should be kept."""
        content = "---\n---\nThis is the body with enough text to pass."
        f = MarkdownFilter()
        result = f.filter(content)
        assert result.keep is True

    def test_wiki_link_with_alias(self, tmp_path: Path) -> None:
        """Wiki-link with alias like [[target|display]] should check target."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "real-note.md").write_text("# Real\nContent.")
        content = (
            "---\ntitle: T\n---\n"
            "See [[real-note|My Display Text]] for this particular item."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert not any("real-note" in w for w in result.warnings)

    def test_wiki_link_alias_broken(self, tmp_path: Path) -> None:
        """Wiki-link with alias to missing file should still warn."""
        vault = tmp_path / "vault"
        vault.mkdir()
        content = (
            "---\ntitle: T\n---\n"
            "See [[missing|Display Text]] for more details on this item."
        )
        f = MarkdownFilter()
        result = f.filter(content, vault_path=vault)
        assert any("missing" in w for w in result.warnings)

    def test_default_min_body_length(self) -> None:
        """Default min_body_length should be 10."""
        f = MarkdownFilter()
        assert f.min_body_length == 10
