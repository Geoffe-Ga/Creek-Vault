"""Tests for the markdown outline parser (issue #354).

The parser is the pure, LLM-free half of the ``--seed-outline`` feature:
it turns a markdown document with ATX headers into an ordered list of
:class:`~creek.generate.outline.OutlineSection` records, each one a
header plus the body text beneath it up to the next header of any level.
The per-section orchestration that detects, retrieves, composes, and
stitches lives in :mod:`creek.generate.drafts` and is exercised in
``test_drafts.py``.
"""

from __future__ import annotations

import pytest

from creek.generate.outline import (
    OutlineParseError,
    OutlineSection,
    build_stitch_prompt,
    format_stitch_directive,
    parse_outline,
)

# ---------------------------------------------------------------------------
# parse_outline — header detection & body capture
# ---------------------------------------------------------------------------


class TestParseOutline:
    """``parse_outline`` splits markdown into header-rooted sections."""

    def test_two_section_outline(self) -> None:
        """Two sibling headers yield two sections, in order."""
        text = (
            "## I. The first claim\n"
            "Riff on the opening thesis.\n\n"
            "## II. The second claim\n"
            "The complication that follows.\n"
        )
        sections = parse_outline(text)
        assert [s.heading for s in sections] == [
            "I. The first claim",
            "II. The second claim",
        ]
        assert sections[0].body == "Riff on the opening thesis."
        assert sections[1].body == "The complication that follows."

    def test_five_section_outline_preserves_order(self) -> None:
        """A five-section outline keeps every section in document order."""
        text = "\n\n".join(
            f"## Section {n}\nBody for section {n}." for n in range(1, 6)
        )
        sections = parse_outline(text)
        assert len(sections) == 5
        assert [s.heading for s in sections] == [f"Section {n}" for n in range(1, 6)]
        assert sections[2].body == "Body for section 3."

    def test_title_header_then_sections(self) -> None:
        """A ``#`` title plus two ``##`` claims yields three sections.

        Matches the issue's worked example: the title and each claim
        compose independently and appear in document order.
        """
        text = (
            "# Paranoia's purpose, revisited\n\n"
            "## I. The paranoia I once explained\n"
            "Paranoia translates unmet needs.\n\n"
            "## II. What the explanation missed\n"
            "The deeper question is what the message is.\n"
        )
        sections = parse_outline(text)
        headings = [s.heading for s in sections]
        assert headings == [
            "Paranoia's purpose, revisited",
            "I. The paranoia I once explained",
            "II. What the explanation missed",
        ]
        assert sections[0].level == 1
        assert sections[0].body == ""
        assert sections[1].level == 2
        assert sections[2].body == "The deeper question is what the message is."

    def test_every_header_opens_its_own_section(self) -> None:
        """Each header — at any depth — composes as its own section."""
        text = (
            "## Parent claim\n"
            "Intro line.\n\n"
            "### Supporting point\n"
            "A nested detail.\n\n"
            "## Sibling claim\n"
            "Standalone.\n"
        )
        sections = parse_outline(text)
        headings = [s.heading for s in sections]
        # Every header opens a section so each composes independently.
        assert headings == ["Parent claim", "Supporting point", "Sibling claim"]
        assert sections[0].body == "Intro line."
        assert sections[1].body == "A nested detail."
        assert sections[1].level == 3

    def test_any_header_closes_the_current_section(self) -> None:
        """A following header (any depth) ends the current section's body."""
        text = "### Deep start\nDeep body.\n\n## Shallower header\nShallow body.\n"
        sections = parse_outline(text)
        assert [s.heading for s in sections] == ["Deep start", "Shallower header"]
        assert sections[0].body == "Deep body."

    def test_section_with_empty_body(self) -> None:
        """A header with no body produces an empty-body section."""
        text = "## Heading only\n\n## Next heading\nBody.\n"
        sections = parse_outline(text)
        assert sections[0].heading == "Heading only"
        assert sections[0].body == ""

    def test_records_header_level(self) -> None:
        """The hash depth is recorded as the section level."""
        text = "# Title\nbody\n## Sub\nmore\n"
        sections = parse_outline(text)
        assert sections[0].level == 1
        assert sections[1].level == 2

    def test_strips_setext_is_not_treated_as_header(self) -> None:
        """Only ATX (``#``) headers count; underline syntax is body text."""
        text = "## Real header\nUnderline title\n=====\nbody\n"
        sections = parse_outline(text)
        assert len(sections) == 1
        assert "Underline title" in sections[0].body
        assert "=====" in sections[0].body

    def test_ignores_leading_preamble_before_first_header(self) -> None:
        """Text before the first header is dropped (it has no section)."""
        text = "Some loose preamble.\n\n## First header\nReal body.\n"
        sections = parse_outline(text)
        assert len(sections) == 1
        assert sections[0].heading == "First header"
        assert "preamble" not in sections[0].body

    def test_hash_without_space_is_not_a_header(self) -> None:
        """``#tag`` (no space after hashes) is body, not a header."""
        text = "## Real\n#hashtag is just text\nbody\n"
        sections = parse_outline(text)
        assert len(sections) == 1
        assert "#hashtag is just text" in sections[0].body


class TestParseOutlineErrors:
    """``parse_outline`` rejects outlines that carry no headers."""

    def test_no_headers_raises(self) -> None:
        """A header-less document is rejected with a clear error."""
        with pytest.raises(OutlineParseError, match="no markdown headers"):
            parse_outline("Just a paragraph.\nAnd another line.\n")

    def test_empty_string_raises(self) -> None:
        """An empty outline is rejected."""
        with pytest.raises(OutlineParseError, match="no markdown headers"):
            parse_outline("")

    def test_whitespace_only_raises(self) -> None:
        """A whitespace-only outline is rejected."""
        with pytest.raises(OutlineParseError, match="no markdown headers"):
            parse_outline("   \n\n\t\n")

    def test_hash_only_without_text_raises(self) -> None:
        """A line of bare hashes with no heading text is not a header."""
        with pytest.raises(OutlineParseError, match="no markdown headers"):
            parse_outline("##\n###\nbody\n")


class TestOutlineSection:
    """The :class:`OutlineSection` value object."""

    def test_seed_text_joins_heading_and_body(self) -> None:
        """``seed_text`` concatenates heading and body for detection."""
        section = OutlineSection(heading="The claim", level=2, body="The detail.")
        assert "The claim" in section.seed_text
        assert "The detail." in section.seed_text

    def test_seed_text_handles_empty_body(self) -> None:
        """An empty body leaves only the heading in ``seed_text``."""
        section = OutlineSection(heading="Just a heading", level=1, body="")
        assert section.seed_text.strip() == "Just a heading"

    def test_section_is_frozen(self) -> None:
        """Sections are immutable value objects."""
        section = OutlineSection(heading="x", level=1, body="y")
        with pytest.raises(AttributeError):
            section.heading = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# stitch directive — connective tissue only
# ---------------------------------------------------------------------------


class TestStitchDirective:
    """``format_stitch_directive`` forbids paraphrase / invention."""

    def test_directive_forbids_rewriting_content(self) -> None:
        """The directive constrains the stitch pass to transitions only."""
        lowered = format_stitch_directive(3).lower()
        assert "transition" in lowered
        # The connective-tissue-only contract must be explicit.
        assert "do not" in lowered
        assert "paraphrase" in lowered or "rewrite" in lowered
        assert "invent" in lowered

    def test_directive_mentions_section_count(self) -> None:
        """The directive names how many sections must survive unchanged."""
        directive = format_stitch_directive(5)
        assert "5" in directive


class TestBuildStitchPrompt:
    """``build_stitch_prompt`` assembles the smoothing-pass prompt."""

    def test_prompt_includes_every_section_body(self) -> None:
        """Each section body appears in the stitch prompt verbatim."""
        prompt = build_stitch_prompt(
            [
                ("First heading", "First body content."),
                ("Second heading", "Second body content."),
            ],
            voice_core="A calm voice.",
        )
        assert "First body content." in prompt
        assert "Second body content." in prompt
        assert "First heading" in prompt
        assert "Second heading" in prompt

    def test_prompt_includes_voice_core_when_present(self) -> None:
        """The voice core is prepended when supplied."""
        prompt = build_stitch_prompt(
            [("H", "B")],
            voice_core="My baseline voice.",
        )
        assert "My baseline voice." in prompt

    def test_prompt_carries_the_stitch_directive(self) -> None:
        """The connective-tissue-only directive is part of the prompt."""
        prompt = build_stitch_prompt([("H", "B")], voice_core="")
        assert "transition" in prompt.lower()
        assert "do not" in prompt.lower()
