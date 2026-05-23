"""Tests for the FEAT-021 structural splitter (zoom-in re-atomization)."""

from __future__ import annotations

import re

import pytest

from creek.atomize.split import (
    SentenceTokenizer,
    default_sentence_tokenizer,
    split,
)
from creek.ingest.base import IngestedFragment, generate_child_fragment_id
from creek.models import Fragment, FragmentLevel, FragmentSource, SourcePlatform

# ---- Helpers ----


def _make(
    *,
    body: str,
    level: FragmentLevel = "document",
    frag_id: str = "frag-rootroot0000",
    structural_path: list[str] | None = None,
    title: str = "Root Doc",
) -> IngestedFragment:
    """Build an ``IngestedFragment`` at the requested structural level."""
    fragment = Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.ESSAY),
        level=level,
        structural_path=structural_path or [],
        tags=["root-tag"],
    )
    return IngestedFragment(fragment=fragment, body=body)


def _bodies(children: list[IngestedFragment]) -> list[str]:
    return [c.body for c in children]


def _levels(children: list[IngestedFragment]) -> list[str]:
    return [c.fragment.level for c in children]


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---- Sentence-level: terminal ----


class TestSentenceTerminal:
    """A sentence is the smallest atom; splitting one returns []."""

    @pytest.mark.parametrize(
        "body",
        [
            "The sentence stands alone.",
            "Even with multiple? Punctuation marks! Inside.",
        ],
    )
    def test_sentence_level_returns_empty(self, body: str) -> None:
        assert split(_make(body=body, level="sentence")) == []


# ---- Paragraph → sentence ----


class TestParagraphToSentence:
    """Paragraph splits into sentences via the configured tokenizer."""

    def test_paragraph_to_multiple_sentences(self) -> None:
        body = "First sentence. Second sentence! Third one?"
        ingested = _make(body=body, level="paragraph")
        children = split(ingested)
        assert _levels(children) == ["sentence", "sentence", "sentence"]
        assert _bodies(children) == [
            "First sentence.",
            "Second sentence!",
            "Third one?",
        ]

    def test_single_sentence_paragraph_returns_empty(self) -> None:
        ingested = _make(body="No further atom is possible.", level="paragraph")
        assert split(ingested) == []

    def test_empty_paragraph_returns_empty(self) -> None:
        ingested = _make(body="   \n  ", level="paragraph")
        assert split(ingested) == []

    def test_custom_tokenizer_is_used(self) -> None:
        body = "one|two|three"

        def pipe_tokenizer(text: str) -> list[str]:
            return [p.strip() for p in text.split("|") if p.strip()]

        ingested = _make(body=body, level="paragraph")
        children = split(ingested, sentence_tokenizer=pipe_tokenizer)
        assert _bodies(children) == ["one", "two", "three"]
        assert _levels(children) == ["sentence"] * 3


# ---- Subsection → paragraph ----


class TestSubsectionToParagraph:
    """Subsection splits at blank-line paragraph boundaries."""

    def test_multiple_paragraphs(self) -> None:
        body = "Para one.\n\nPara two.\n\nPara three."
        children = split(_make(body=body, level="subsection"))
        assert _levels(children) == ["paragraph"] * 3
        assert _bodies(children) == ["Para one.", "Para two.", "Para three."]

    def test_paragraphs_with_blank_whitespace_lines(self) -> None:
        body = "Alpha.\n   \nBravo.\n\t\nCharlie."
        children = split(_make(body=body, level="subsection"))
        assert _bodies(children) == ["Alpha.", "Bravo.", "Charlie."]

    def test_subsection_with_single_paragraph_cascades_to_sentences(self) -> None:
        body = "Only one para. But two sentences here."
        children = split(_make(body=body, level="subsection"))
        assert _levels(children) == ["sentence", "sentence"]
        assert _bodies(children) == ["Only one para.", "But two sentences here."]

    def test_subsection_with_single_sentence_returns_empty(self) -> None:
        assert split(_make(body="Indivisible content.", level="subsection")) == []


# ---- Section → subsection ----


class TestSectionToSubsection:
    """Section splits at H3+ headings, cascades when none present."""

    def test_section_splits_at_h3_headings(self) -> None:
        body = (
            "Preamble paragraph here.\n\n"
            "### First subsection\n\n"
            "Body of first subsection.\n\n"
            "### Second subsection\n\n"
            "Body of second subsection."
        )
        ingested = _make(body=body, level="section")
        children = split(ingested)
        assert _levels(children) == ["subsection", "subsection", "subsection"]
        # The preamble (no heading) appears first, then the two H3 chunks.
        assert children[0].body == "Preamble paragraph here."
        assert children[1].body.startswith("### First subsection")
        assert children[2].body.startswith("### Second subsection")

    def test_section_with_h4_h5_h6_headings(self) -> None:
        body = "#### Four\n\nA\n\n##### Five\n\nB\n\n###### Six\n\nC"
        children = split(_make(body=body, level="section"))
        assert len(children) == 3
        assert all(c.fragment.level == "subsection" for c in children)

    def test_section_with_no_h3_cascades_to_paragraphs(self) -> None:
        children = split(
            _make(body="First paragraph.\n\nSecond paragraph.", level="section"),
        )
        assert _levels(children) == ["paragraph", "paragraph"]
        assert _bodies(children) == ["First paragraph.", "Second paragraph."]

    def test_section_with_no_h3_and_one_paragraph_cascades_to_sentences(
        self,
    ) -> None:
        children = split(
            _make(body="Hello there. World remains.", level="section"),
        )
        assert _levels(children) == ["sentence", "sentence"]


# ---- Document → section ----


class TestDocumentToSection:
    """Document splits at H1/H2 headings or cascades through finer levels."""

    def test_document_splits_at_h1_h2(self) -> None:
        body = (
            "# First section\n\n"
            "Body of first section.\n\n"
            "## Second section\n\n"
            "Body of second section."
        )
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert _levels(children) == ["section", "section"]
        assert children[0].fragment.structural_path == ["First section"]
        assert children[1].fragment.structural_path == ["Second section"]

    def test_document_h3_only_cascades_to_subsection(self) -> None:
        body = (
            "### Only subsection heading\n\n"
            "Some body text.\n\n"
            "### Another subsection\n\n"
            "More body text."
        )
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert _levels(children) == ["subsection", "subsection"]

    def test_document_with_no_headings_collapses_to_paragraph(self) -> None:
        """Acceptance: ``document → paragraph`` when no headings present."""
        body = (
            "First paragraph of an unstructured note.\n\n"
            "Second paragraph continues.\n\n"
            "Third paragraph closes it."
        )
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert _levels(children) == ["paragraph", "paragraph", "paragraph"]
        assert _bodies(children) == [
            "First paragraph of an unstructured note.",
            "Second paragraph continues.",
            "Third paragraph closes it.",
        ]

    def test_document_with_no_headings_one_para_collapses_to_sentence(self) -> None:
        body = "Single para. Two sentences worth."
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert _levels(children) == ["sentence", "sentence"]

    def test_document_preamble_before_first_heading_is_preserved(self) -> None:
        body = "Intro paragraph with no heading.\n\n# A heading\n\nBody under heading."
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert len(children) == 2
        assert children[0].body == "Intro paragraph with no heading."
        # First child has no heading so structural_path is inherited unchanged.
        assert children[0].fragment.structural_path == []
        assert children[1].fragment.structural_path == ["A heading"]


# ---- Inheritance & ID semantics ----


class TestChildMetadata:
    """Children inherit parent metadata and carry deterministic identity."""

    def test_child_ids_are_deterministic_helper_outputs(self) -> None:
        body = "# Alpha\n\nA body.\n\n# Bravo\n\nB body."
        ingested = _make(body=body, level="document", frag_id="frag-parent01abcd")
        children = split(ingested)
        assert [c.fragment.id for c in children] == [
            generate_child_fragment_id("frag-parent01abcd", "section", 0),
            generate_child_fragment_id("frag-parent01abcd", "section", 1),
        ]

    def test_children_carry_parent_id_level_and_inherit_metadata(self) -> None:
        body = "# A\n\nbody one\n\n# B\n\nbody two"
        ingested = _make(body=body, level="document", frag_id="frag-pp1122334455")
        children = split(ingested)
        for child in children:
            assert child.fragment.parent_id == "frag-pp1122334455"
            assert child.fragment.level == "section"
            assert child.fragment.child_ids == []
            assert child.fragment.source.platform == "essay"
            assert child.fragment.tags == ["root-tag"]

    def test_structural_path_extends_with_heading(self) -> None:
        body = "# Outer\n\nbody one\n\n# Other\n\nbody two"
        ingested = _make(body=body, level="document", structural_path=["Top"])
        children = split(ingested)
        assert children[0].fragment.structural_path == ["Top", "Outer"]
        assert children[1].fragment.structural_path == ["Top", "Other"]

    def test_structural_path_unchanged_for_headless_children(self) -> None:
        ingested = _make(
            body="First para.\n\nSecond para.",
            level="document",
            structural_path=["Top"],
        )
        for child in split(ingested):
            assert child.fragment.structural_path == ["Top"]

    def test_child_title_is_heading_when_present(self) -> None:
        body = "# Alpha Heading\n\nbody one\n\n## Bravo Heading\n\nbody two"
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert children[0].fragment.title == "Alpha Heading"
        assert children[1].fragment.title == "Bravo Heading"

    def test_child_title_falls_back_to_content_excerpt(self) -> None:
        body = "Sentence alpha here. Sentence bravo here."
        ingested = _make(body=body, level="paragraph")
        children = split(ingested)
        assert children[0].fragment.title == "Sentence alpha here."
        assert children[1].fragment.title == "Sentence bravo here."

    def test_long_first_line_title_is_truncated_with_ellipsis(self) -> None:
        long_first = "x" * 200
        body = f"{long_first}.\n\nSecond paragraph here."
        ingested = _make(body=body, level="subsection")
        children = split(ingested)
        title = children[0].fragment.title
        assert title.endswith("…")
        assert len(title) <= 60

    def test_whitespace_only_section_preamble_skipped(self) -> None:
        body = "   \n  \n# Heading\n\nBody under heading."
        ingested = _make(body=body, level="document")
        children = split(ingested)
        assert len(children) == 1
        assert children[0].fragment.title == "Heading"


# ---- Idempotency ----


class TestIdempotency:
    """Splitting twice yields identical children."""

    @pytest.mark.parametrize(
        ("level", "body"),
        [
            ("document", "# A\n\none\n\n# B\n\ntwo"),
            ("paragraph", "Alpha sentence. Bravo sentence."),
        ],
    )
    def test_repeat_split_yields_same_ids_and_bodies(
        self,
        level: FragmentLevel,
        body: str,
    ) -> None:
        ingested = _make(body=body, level=level)
        first = split(ingested)
        second = split(ingested)
        assert [c.fragment.id for c in first] == [c.fragment.id for c in second]
        assert _bodies(first) == _bodies(second)


# ---- Property: concatenation preserves text ----


class TestConcatenationProperty:
    """Concatenated child text equals parent text up to whitespace."""

    @pytest.mark.parametrize(
        ("level", "body"),
        [
            (
                "document",
                "# Alpha\n\nA body.\n\n## Bravo\n\nB body.",
            ),
            (
                "document",
                "Para one.\n\nPara two.\n\nPara three.",
            ),
            (
                "section",
                "### Sub one\n\none body.\n\n### Sub two\n\ntwo body.",
            ),
            (
                "subsection",
                "Para one.\n\nPara two.",
            ),
            (
                "paragraph",
                "Sentence one. Sentence two. Sentence three.",
            ),
        ],
    )
    def test_concatenation_equals_parent_up_to_whitespace(
        self,
        level: FragmentLevel,
        body: str,
    ) -> None:
        ingested = _make(body=body, level=level)
        children = split(ingested)
        assert children, "expected non-empty decomposition for this fixture"
        concatenated = " ".join(c.body for c in children)
        assert _normalize_ws(concatenated) == _normalize_ws(body)


# ---- Zoom-out levels are terminal for zoom-in ----


class TestZoomOutLevelsTerminalForZoomIn:
    """Aggregation levels (exchange/burst/session) return [] from zoom-in."""

    @pytest.mark.parametrize("level", ["exchange", "burst", "session"])
    def test_returns_empty(self, level: FragmentLevel) -> None:
        ingested = _make(body="any content here", level=level)
        assert split(ingested) == []


# ---- Default sentence tokenizer ----


class TestDefaultSentenceTokenizer:
    """The default regex tokenizer handles common prose patterns."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("One. Two. Three.", ["One.", "Two.", "Three."]),
            ("Is it? Yes! Continue.", ["Is it?", "Yes!", "Continue."]),
            ('"Stop." She paused.', ['"Stop."', "She paused."]),
            ("", []),
            ("   \n  ", []),
        ],
    )
    def test_tokenizer_cases(self, text: str, expected: list[str]) -> None:
        assert default_sentence_tokenizer(text) == expected

    def test_sentence_tokenizer_protocol_matches_signature(self) -> None:
        tok: SentenceTokenizer = default_sentence_tokenizer
        assert tok("Hello. World.") == ["Hello.", "World."]
