"""Tests for the no-fabrication cohesion pass.

The cohesion pass smooths seams between independently-composed sections of
a single-topic draft by inserting transitions. It is gated behind a hard,
deterministic *entity-preservation guard*: the post-cohesion text may not
introduce any proper noun, multi-word name, number, or bespoke ontology
term that was absent from the pre-cohesion text. Adding transition words
("However," "And so") is allowed; smuggling in a new named entity, number,
or fact is rejected and the pass falls back to the original body.

These tests exercise the guard as a pure function (no LLM) and the wiring
that runs the pass only when explicitly enabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.generate.cohesion import (
    build_cohesion_prompt,
    extract_preservable_tokens,
    format_cohesion_directive,
    is_entity_preserving,
    run_cohesion_pass,
)

if TYPE_CHECKING:
    import pytest


class TestExtractPreservableTokens:
    """``extract_preservable_tokens`` isolates the load-bearing tokens."""

    def test_captures_capitalized_proper_nouns(self) -> None:
        """A mid-sentence capitalized word is a proper noun to preserve."""
        tokens = extract_preservable_tokens("My dad watched Pluribus last night.")
        assert "pluribus" in tokens

    def test_captures_numbers(self) -> None:
        """Bare digits are preservable tokens — a fabricated year is a fact."""
        tokens = extract_preservable_tokens("It happened in 1997 and again in 2003.")
        assert "1997" in tokens
        assert "2003" in tokens

    def test_ignores_sentence_initial_lowercase_words(self) -> None:
        """A lowercase ordinary word is not a preservable token."""
        tokens = extract_preservable_tokens("the spine of the essay is doubt.")
        assert "spine" not in tokens
        assert "doubt" not in tokens

    def test_sentence_initial_capital_is_not_a_proper_noun(self) -> None:
        """A capital only because it starts a sentence is not an entity.

        Otherwise every transition opener ("However,") would register as a
        new entity and the guard would reject all cohesion output.
        """
        tokens = extract_preservable_tokens("However, the doubt remains.")
        assert "however" not in tokens

    def test_trailing_punctuation_yields_no_phantom_token(self) -> None:
        """A trailing sentence boundary produces an empty segment, skipped cleanly.

        The boundary split on text ending in terminal punctuation emits an
        empty final segment; it must be ignored rather than crash on an empty
        word list.
        """
        tokens = extract_preservable_tokens("My dad watched Pluribus. ")
        assert "pluribus" in tokens

    def test_bespoke_ontology_terms_are_captured(self) -> None:
        """Owner ontology terms are preserved even when given explicitly."""
        tokens = extract_preservable_tokens(
            "The fragment resonates.",
            bespoke_terms=("fragment", "resonance"),
        )
        assert "fragment" in tokens

    def test_capitalized_common_term_captured_only_when_capitalized(self) -> None:
        """A capitalized-only common term is caught in ontological form only.

        ``Eddy``/``Eddies`` are common English words; only their capitalized
        ontological form should register as a preservable bespoke token so a
        lowercase prose transition ("the eddies of thought") is never treated
        as a fabricated ontology term.
        """
        capped = extract_preservable_tokens(
            "The Eddies of the mind.",
            capitalized_terms=("Eddy", "Eddies"),
        )
        assert "eddies" in capped
        lowered = extract_preservable_tokens(
            "the eddies of thought",
            capitalized_terms=("Eddy", "Eddies"),
        )
        assert "eddies" not in lowered

    def test_markdown_header_word_not_captured_as_proper_noun(self) -> None:
        """Structural header words are not preservable proper nouns.

        A multi-word ``## Rising Tide`` header is structure, not prose; its
        title-cased words must not register as fabricated proper nouns.
        """
        tokens = extract_preservable_tokens("Intro.\n\n## Rising Tide\n\nThe body.")
        assert "tide" not in tokens
        assert "rising" not in tokens


class TestIsEntityPreserving:
    """The deterministic pre/post entity-preservation guard."""

    def test_added_transitions_only_passes(self) -> None:
        """Adding bridge words but no new entity passes the guard."""
        pre = "My dad watched Pluribus. The spine of doubt is real."
        post = (
            "My dad watched Pluribus. And so, the spine of doubt is real. "
            "However, it holds."
        )
        assert is_entity_preserving(pre=pre, post=post) is True

    def test_new_proper_noun_is_rejected(self) -> None:
        """A post that adds a brand-new proper noun is rejected."""
        pre = "My dad watched the show."
        post = "My dad, who lives in Provo, watched the show."
        assert is_entity_preserving(pre=pre, post=post) is False

    def test_new_number_is_rejected(self) -> None:
        """A fabricated year/number in the post is rejected."""
        pre = "It changed everything for him."
        post = "In 1997, it changed everything for him."
        assert is_entity_preserving(pre=pre, post=post) is False

    def test_new_bespoke_ontology_term_is_rejected(self) -> None:
        """Introducing an owner ontology term not present before is rejected."""
        pre = "The idea sits there, unmoving."
        post = "The Eddy of the idea sits there, unmoving."
        assert (
            is_entity_preserving(
                pre=pre,
                post=post,
                bespoke_terms=("eddy", "fragment", "praxis"),
            )
            is False
        )

    def test_new_pluralised_bespoke_term_is_rejected(self) -> None:
        """A fabricated *plural* ontology term cannot slip the guard.

        Without ``wavelengths`` in the preserve set a cohesion pass could
        introduce the plural while ``wavelength`` was absent — fabricating
        ontology structure. The plural must be guarded just like the singular.
        """
        pre = "The idea sits there, unmoving."
        post = "The Wavelengths of the idea sit there, unmoving."
        assert (
            is_entity_preserving(
                pre=pre,
                post=post,
                bespoke_terms=("wavelength", "wavelengths", "praxis", "praxes"),
            )
            is False
        )

    def test_dropping_an_entity_still_passes(self) -> None:
        """Removing (not adding) an entity is not a fabrication and passes."""
        pre = "My dad watched Pluribus in Provo."
        post = "My dad watched Pluribus."
        assert is_entity_preserving(pre=pre, post=post) is True

    def test_lowercase_common_ontology_transition_is_accepted(self) -> None:
        """A lowercase "eddies" transition is not a fabricated ontology term.

        ``eddy``/``eddies`` are common English words; introducing them lowercased
        as connective prose ("the eddies of thought") must NOT be rejected as a
        fabricated ontology term — only their capitalized form is guarded.
        """
        pre = "The idea sits there. Doubt is the spine."
        post = "The idea sits there, one of the eddies of thought. Doubt is the spine."
        assert (
            is_entity_preserving(
                pre=pre,
                post=post,
                capitalized_terms=("Eddy", "Eddies", "Thread", "Threads"),
            )
            is True
        )

    def test_fabricated_capitalized_common_term_is_rejected(self) -> None:
        """A fabricated capitalized ``Eddy``/``Thread`` is still rejected.

        When the pre-body had no eddy/Eddy at all, a post that introduces the
        capitalized ontological ``Eddy`` is fabricating structure and must be
        rejected — caught either as a capitalized bespoke term or as a proper
        noun.
        """
        pre = "The idea sits there. Doubt is the spine."
        post = "The Eddy of the idea sits there. Doubt is the spine."
        assert (
            is_entity_preserving(
                pre=pre,
                post=post,
                capitalized_terms=("Eddy", "Eddies", "Thread", "Threads"),
            )
            is False
        )


class TestFormatCohesionDirective:
    """The transitions-only directive states the no-fabrication contract."""

    def test_names_the_anti_fabrication_constraint(self) -> None:
        """The directive forbids inventing entities/numbers/facts."""
        directive = format_cohesion_directive()
        lowered = directive.lower()
        assert "transition" in lowered
        assert "no new" in lowered or "do not" in lowered

    def test_build_prompt_includes_body_and_directive(self) -> None:
        """The cohesion prompt carries the body and the directive."""
        prompt = build_cohesion_prompt("Body text here.", voice_core="My voice.")
        assert "Body text here." in prompt
        assert "My voice." in prompt
        assert "transition" in prompt.lower()


def _no_grounding_findings(_body: str) -> list[str]:
    """Grounding check stub that finds nothing ungrounded."""
    return []


class TestRunCohesionPass:
    """``run_cohesion_pass`` orchestrates LLM + guards + fallback."""

    def test_disabled_returns_original_and_skips_llm(self) -> None:
        """With cohesion off the original body returns and the LLM is untouched."""
        calls: list[str] = []

        def llm(prompt: str) -> str:
            calls.append(prompt)
            return "SHOULD NOT RUN"

        result = run_cohesion_pass(
            "Original body.",
            cohesion_llm=llm,
            enabled=False,
            grounding_check=_no_grounding_findings,
        )
        assert result == "Original body."
        assert calls == []

    def test_no_llm_returns_original_and_skips(self) -> None:
        """A ``None`` cohesion LLM (the --no-llm path) skips the pass."""
        result = run_cohesion_pass(
            "Original body.",
            cohesion_llm=None,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert result == "Original body."

    def test_enabled_applies_smoothed_body(self) -> None:
        """When enabled and the guard passes, the smoothed body is returned."""
        pre = "My dad watched Pluribus. Doubt is the spine."
        smoothed = "My dad watched Pluribus. And so, doubt is the spine."

        result = run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: smoothed,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert result == smoothed

    def test_fabricated_entity_falls_back_to_original(self) -> None:
        """A cohesion output adding a new entity is rejected; original returns."""
        pre = "My dad watched the show. Doubt is the spine."
        fabricated = "In 1997, my dad watched the show in Provo. Doubt is the spine."

        result = run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: fabricated,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert result == pre

    def test_bespoke_term_fabrication_falls_back(self) -> None:
        """A new bespoke ontology term in the output triggers fallback."""
        pre = "The idea sits there. Doubt is the spine."
        fabricated = "The Praxis idea sits there. Doubt is the spine."

        result = run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: fabricated,
            enabled=True,
            grounding_check=_no_grounding_findings,
            bespoke_terms=("praxis", "eddy", "fragment"),
        )
        assert result == pre

    def test_ungrounded_biographical_claim_falls_back(self) -> None:
        """A smoothed body that trips the grounding flag falls back."""
        pre = "My dad watched the show. Doubt is the spine."
        # Entity-preserving (no new proper noun / number) but smuggles in an
        # ungrounded first-person biographical claim.
        smoothed = "My dad watched the show. And so doubt is the spine."

        def grounding_flags(_body: str) -> list[str]:
            return ["ungrounded first-person claim"]

        result = run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: smoothed,
            enabled=True,
            grounding_check=grounding_flags,
        )
        assert result == pre

    def test_empty_llm_output_falls_back(self) -> None:
        """An empty cohesion response falls back rather than blanking the draft."""
        result = run_cohesion_pass(
            "Original body.",
            cohesion_llm=lambda _p: "   ",
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert result == "Original body."

    def test_rejected_output_warns_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A guard-rejected cohesion output emits a stderr fallback diagnostic.

        An operator who opted into ``--cohesion`` should see *why* the pass
        had no effect, mirroring the biographical grounding-guard warning.
        """
        pre = "My dad watched the show. Doubt is the spine."
        fabricated = "In 1997, my dad watched the show in Provo. Doubt is the spine."

        run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: fabricated,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        err = capsys.readouterr().err
        assert "cohesion guard" in err.lower()

    def test_accepted_output_emits_no_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An accepted cohesion output emits no fallback diagnostic."""
        pre = "My dad watched Pluribus. Doubt is the spine."
        smoothed = "My dad watched Pluribus. And so, doubt is the spine."

        run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: smoothed,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert capsys.readouterr().err == ""

    def test_multi_section_body_skips_cohesion(self) -> None:
        """A body with 2+ top-level ``## `` headers is returned unchanged.

        Multi-section outline drafts carry their own seam stitch; the
        single-topic cohesion pass must not smooth across section seams, so it
        no-ops and never calls the LLM.
        """
        calls: list[str] = []

        def llm(prompt: str) -> str:
            calls.append(prompt)
            return "SHOULD NOT RUN"

        multi = "## Rising\n\nFirst part.\n\n## Falling\n\nSecond part."
        result = run_cohesion_pass(
            multi,
            cohesion_llm=llm,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert result == multi
        assert calls == []

    def test_multi_section_body_warns_skipped(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A skipped multi-section body emits the multi-section diagnostic."""
        multi = "## Rising\n\nFirst part.\n\n## Falling\n\nSecond part."
        run_cohesion_pass(
            multi,
            cohesion_llm=lambda _p: "x",
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        err = capsys.readouterr().err.lower()
        assert "cohesion guard" in err
        assert "multi-section" in err

    def test_single_section_body_is_processed(self) -> None:
        """A single-topic body (one or zero ``## `` headers) is smoothed."""
        pre = "## Rising\n\nMy dad watched Pluribus. Doubt is the spine."
        smoothed = "## Rising\n\nMy dad watched Pluribus. And so, doubt is the spine."
        result = run_cohesion_pass(
            pre,
            cohesion_llm=lambda _p: smoothed,
            enabled=True,
            grounding_check=_no_grounding_findings,
        )
        assert result == smoothed
