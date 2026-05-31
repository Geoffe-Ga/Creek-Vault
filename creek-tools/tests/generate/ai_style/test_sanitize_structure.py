"""Tests for the Markdown-structure sanitizer (FEAT-040.4)."""

from __future__ import annotations

from creek.generate.ai_style.sanitize_structure import (
    flatten_inline_header_lists,
    sanitize_structure,
    strip_breaks_before_headings,
    strip_leading_emoji,
    strip_placeholders,
    unwrap_code_fence,
)


class TestUnwrapCodeFence:
    """A whole-body fence is unwrapped; partial fences are left alone."""

    def test_whole_body_fence_unwrapped(self) -> None:
        """An essay wrapped in ```markdown ... ``` is unwrapped."""
        body = "```markdown\n# Title\n\nReal prose here.\n```"
        out = unwrap_code_fence(body)
        assert out == "# Title\n\nReal prose here."

    def test_inline_fence_untouched(self) -> None:
        """A code block inside real prose is not unwrapped."""
        body = "Here is code:\n\n```py\nx = 1\n```\n\nand more prose."
        assert unwrap_code_fence(body) == body


class TestThematicBreaks:
    """Breaks directly above headings are AI artifacts."""

    def test_break_before_heading_removed(self) -> None:
        """A --- line directly above a heading is removed."""
        body = "Intro paragraph.\n\n----\n## History\n\nText."
        out = strip_breaks_before_headings(body)
        assert "----" not in out
        assert "## History" in out

    def test_standalone_break_kept(self) -> None:
        """A thematic break not followed by a heading is preserved."""
        body = "Section one.\n\n---\n\nSection two prose, no heading."
        assert strip_breaks_before_headings(body) == body

    def test_setext_heading_not_destroyed(self) -> None:
        """A setext underline (Title\\n---) above a heading is left intact."""
        body = "My Section Title\n---\n## Subsection\n\nText."
        assert strip_breaks_before_headings(body) == body

    def test_blank_line_preserved_after_removal(self) -> None:
        """Removing the break keeps the blank line separating prose/heading."""
        out = strip_breaks_before_headings("Intro.\n\n----\n## H\n\nx")
        assert out == "Intro.\n\n## H\n\nx"


class TestInlineHeaderLists:
    """Canned bold-colon list items are flattened."""

    def test_bold_colon_inside(self) -> None:
        """`- **Setup:** text` loses the mechanical bold."""
        out = flatten_inline_header_lists("- **Setup:** do the thing")
        assert out == "- Setup: do the thing"

    def test_bold_then_colon(self) -> None:
        """`1. **Setup**: text` is flattened and keeps its colon."""
        out = flatten_inline_header_lists("1. **Setup**: do it")
        assert out == "1. Setup: do it"

    def test_ordinary_bold_untouched(self) -> None:
        """Mid-sentence bold (not a list-item header) is left alone."""
        body = "This is **important** to me."
        assert flatten_inline_header_lists(body) == body


class TestEmoji:
    """Decorative leading emoji are stripped, markers preserved."""

    def test_emoji_on_heading(self) -> None:
        """A heading's leading emoji is removed; the hashes remain."""
        out = strip_leading_emoji("## \U0001f680 Launch plan")
        assert out == "## Launch plan"

    def test_emoji_on_bullet(self) -> None:
        """A bullet's leading emoji is removed; the marker remains."""
        out = strip_leading_emoji("- \U0001f9e0 think hard")
        assert out == "- think hard"

    def test_no_emoji_untouched(self) -> None:
        """Ordinary heading text is unchanged."""
        assert strip_leading_emoji("## Plan") == "## Plan"


class TestPlaceholders:
    """Unfilled placeholders are stripped."""

    def test_insert_paste_tokens(self) -> None:
        """INSERT_/PASTE_ tokens are removed."""
        out = strip_placeholders("url INSERT_SOURCE_URL_30 and PASTE_SPOTIFY_URL_HERE")
        assert "INSERT_" not in out
        assert "PASTE_" not in out

    def test_placeholder_date_and_your_name(self) -> None:
        """2025-xx-xx and [Your Name] are removed."""
        out = strip_placeholders("dated 2025-xx-xx by [Your Name] today")
        assert "2025-xx-xx" not in out
        assert "Your Name" not in out

    def test_describe_placeholder_removed(self) -> None:
        """A [Describe ...] mad-lib placeholder is removed."""
        out = strip_placeholders("For [Describe the specific section] here.")
        assert "[Describe" not in out

    def test_return_arrow_removed(self) -> None:
        """The footnote return arrow is removed."""
        out = strip_placeholders("See footnote 1. ↩")
        assert "↩" not in out


class TestSanitizeStructure:
    """The composed, idempotent structure pass."""

    def test_composes_and_is_idempotent(self) -> None:
        """All repairs apply together and running twice equals once."""
        body = "```markdown\nIntro.\n\n----\n## \U0001f680 Plan\n\n- **Step:** go\n```"
        once = sanitize_structure(body)
        assert "```" not in once
        assert "----" not in once
        assert "## Plan" in once
        assert "- Step: go" in once
        assert sanitize_structure(once) == once
