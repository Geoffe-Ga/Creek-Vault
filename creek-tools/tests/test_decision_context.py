"""Tests for DecisionContextGatherer — Section 12.3-12.5 context gathering.

Covers cross-vault context aggregation, anti-manipulation guardrails, the
Phase x Frequency Practice Map, and appending context sections to existing
Decision notes.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.decisions import (
    PRACTICE_MAP,
    DecisionContext,
    DecisionContextGatherer,
    decision_from_note,
)
from creek.models import (
    Decision,
    DecisionStatus,
    Frequency,
    Phase,
)
from tests.conftest import CORRUPT_NOTE_SHAPES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a vault scaffolding with all relevant subdirectories."""
    dirs = [
        "02-Threads",
        "04-Praxis",
        "05-Wavelength/Observations",
        "08-Decisions/Active",
        "08-Decisions/Archive",
        "09-Reference",
    ]
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def gatherer() -> DecisionContextGatherer:
    """Return a DecisionContextGatherer with default similarity threshold."""
    return DecisionContextGatherer()


@pytest.fixture()
def sample_decision() -> Decision:
    """Return a Decision model about changing careers."""
    return Decision(
        id="decision-career01",
        title="Change careers from engineering to teaching",
        status=DecisionStatus.SENSING,
        opened=date(2025, 3, 10),
        frequency_context=[Frequency.F1, Frequency.F5],
        wavelength_phase_at_opening="rising",
        tags=["career"],
    )


def _write_post(path: Path, **fields: object) -> None:
    """Write a frontmatter post with the given metadata to ``path``."""
    body = str(fields.pop("content", ""))
    post = frontmatter.Post(content=body, **fields)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


@pytest.fixture()
def seeded_vault(vault_path: Path) -> Path:
    """Populate the vault with threads, praxis, decisions, and observations."""
    _write_post(
        vault_path / "02-Threads" / "career-thread.md",
        type="thread",
        id="thread-career",
        title="Career direction and meaningful work",
        description="Ongoing exploration of engineering, teaching, and craft.",
        frequency_affinity=["F1", "F5"],
    )
    _write_post(
        vault_path / "02-Threads" / "cooking-thread.md",
        type="thread",
        id="thread-cooking",
        title="Weeknight cooking",
        description="Recipes for busy evenings.",
        frequency_affinity=["F2"],
    )
    _write_post(
        vault_path / "04-Praxis" / "goal-setting.md",
        type="praxis",
        id="praxis-goals",
        title="Quarterly goal setting",
        frequency=["F1", "F5"],
    )
    _write_post(
        vault_path / "04-Praxis" / "morning-pages.md",
        type="praxis",
        id="praxis-pages",
        title="Morning pages",
        frequency=["F2"],
    )
    _write_post(
        vault_path / "08-Decisions" / "Archive" / "2024-04-01-prior-job.md",
        type="decision",
        id="decision-prior01",
        title="Should I change jobs this year",
        status="enacted",
        opened=date(2024, 4, 1).isoformat(),
        wavelength_phase_at_opening="rising",
        frequency_context=["F1", "F5"],
    )
    _write_post(
        vault_path / "08-Decisions" / "Archive" / "2023-11-15-teach-class.md",
        type="decision",
        id="decision-prior02",
        title="Teaching a one-off class",
        status="enacted",
        opened=date(2023, 11, 15).isoformat(),
        wavelength_phase_at_opening="peaking",
        frequency_context=["F5"],
    )
    _write_post(
        vault_path / "05-Wavelength" / "Observations" / "2025-03-09.md",
        type="wavelength_observation",
        id="wave-0309",
        date=date(2025, 3, 9).isoformat(),
        phase="peaking",
    )
    _write_post(
        vault_path / "05-Wavelength" / "Observations" / "2025-03-01.md",
        type="wavelength_observation",
        id="wave-0301",
        date=date(2025, 3, 1).isoformat(),
        phase="rising",
    )
    return vault_path


# ---- PRACTICE_MAP Tests ----


class TestPracticeMap:
    """Tests for the module-level PRACTICE_MAP constant."""

    def test_is_nonempty(self) -> None:
        """PRACTICE_MAP should have canonical entries from Section 12.5."""
        assert len(PRACTICE_MAP) >= 10

    def test_keys_are_tuple_of_str(self) -> None:
        """Keys should be (color, phase) tuples of strings."""
        for key in PRACTICE_MAP:
            assert isinstance(key, tuple)
            assert len(key) == 2
            assert all(isinstance(p, str) for p in key)

    def test_values_are_string_tuples(self) -> None:
        """Values should be tuples of practice names."""
        for value in PRACTICE_MAP.values():
            assert isinstance(value, tuple)
            assert all(isinstance(p, str) for p in value)

    def test_contains_core_interventions(self) -> None:
        """Spot check the canonical interventions from the ontology."""
        assert "Cold Shower" in PRACTICE_MAP[("beige", "rising")]
        assert "Yoga" in PRACTICE_MAP[("green", "rising")]
        assert "Journaling" in PRACTICE_MAP[("green", "diminishing")]


# ---- interventions_lookup Tests ----


class TestInterventionsLookup:
    """Tests for DecisionContextGatherer.interventions_lookup."""

    def test_by_color_and_phase(self, gatherer: DecisionContextGatherer) -> None:
        """Color + phase returns the mapped practices."""
        assert "Cold Shower" in gatherer.interventions_lookup("beige", "rising")

    def test_by_frequency_code(self, gatherer: DecisionContextGatherer) -> None:
        """Frequency code arg is translated to color before lookup."""
        assert "Metta Meditation" in gatherer.interventions_lookup("F4", "rising")

    def test_returns_empty_for_unmapped(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Unknown (frequency, phase) pairs return an empty list."""
        assert gatherer.interventions_lookup("beige", "peaking") == []

    def test_case_insensitive_phase(self, gatherer: DecisionContextGatherer) -> None:
        """Phase matching should be case-insensitive."""
        assert gatherer.interventions_lookup("beige", "RISING")

    def test_unknown_frequency_falls_through(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Unknown frequency string without a color mapping returns []."""
        assert gatherer.interventions_lookup("magenta", "rising") == []


# ---- gather_context Tests ----


class TestGatherContext:
    """Tests for DecisionContextGatherer.gather_context."""

    def test_returns_decision_context(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """gather_context should return a DecisionContext dataclass."""
        result = gatherer.gather_context(sample_decision, seeded_vault)
        assert isinstance(result, DecisionContext)

    def test_finds_related_threads(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """Related threads by semantic similarity and frequency overlap."""
        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        assert "thread-career" in ctx.related_threads
        assert "thread-cooking" not in ctx.related_threads

    def test_finds_past_decisions(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """Past decisions with overlapping frequency are surfaced."""
        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        assert "decision-prior01" in ctx.related_decisions

    def test_excludes_self_from_related_decisions(
        self,
        gatherer: DecisionContextGatherer,
        vault_path: Path,
        sample_decision: Decision,
    ) -> None:
        """A decision should never cite itself as related."""
        _write_post(
            vault_path / "08-Decisions" / "Active" / "self.md",
            type="decision",
            id=sample_decision.id,
            title=sample_decision.title,
            frequency_context=["F1", "F5"],
        )
        ctx = gatherer.gather_context(sample_decision, vault_path)
        assert sample_decision.id not in ctx.related_decisions

    def test_surfaces_relevant_praxis(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """Praxis with overlapping frequencies are surfaced."""
        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        assert "praxis-goals" in ctx.relevant_praxis
        assert "praxis-pages" not in ctx.relevant_praxis

    def test_current_wavelength_uses_most_recent_observation(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """Most recent observation by date wins (2025-03-09 -> peaking)."""
        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        assert ctx.current_wavelength == "peaking"

    def test_current_wavelength_falls_back(
        self,
        gatherer: DecisionContextGatherer,
        vault_path: Path,
        sample_decision: Decision,
    ) -> None:
        """With no observations, falls back to decision's opening phase."""
        ctx = gatherer.gather_context(sample_decision, vault_path)
        assert ctx.current_wavelength == "rising"

    def test_frequency_affinity_ranks_decision_first(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """Frequencies from the decision should lead the affinity list."""
        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        assert set(ctx.frequency_affinity[:2]) == {"F1", "F5"}

    def test_interventions_match_phase_and_frequency(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """Interventions reflect current phase (peaking) and affinity."""
        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        # F5 -> orange; no mapped practice at peaking. F1 -> beige; none.
        # Add F7 (yellow) to activate a peaking intervention.
        enriched = sample_decision.model_copy(
            update={"frequency_context": [Frequency.F7, Frequency.F1]},
        )
        ctx = gatherer.gather_context(enriched, seeded_vault)
        assert "Samatha Vipassana" in ctx.interventions


# ---- generate_context_section Tests ----


class TestGenerateContextSection:
    """Tests for DecisionContextGatherer.generate_context_section."""

    def test_returns_string(self, gatherer: DecisionContextGatherer) -> None:
        """generate_context_section should return a string."""
        ctx = DecisionContext(
            decision_id="decision-x",
            decision_title="t",
            current_wavelength="rising",
        )
        assert isinstance(gatherer.generate_context_section(ctx), str)

    def test_includes_context_heading(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Result should start with '## Context'."""
        ctx = DecisionContext(decision_id="decision-x", decision_title="t")
        section = gatherer.generate_context_section(ctx)
        assert section.startswith("## Context")

    def test_includes_related_thread_links(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Related threads appear as Obsidian wiki links."""
        ctx = DecisionContext(
            decision_id="d",
            decision_title="t",
            related_threads=["thread-career"],
        )
        section = gatherer.generate_context_section(ctx)
        assert "[[thread-career]]" in section

    def test_includes_related_decision_links(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Past decisions appear as Obsidian wiki links."""
        ctx = DecisionContext(
            decision_id="d",
            decision_title="t",
            related_decisions=["decision-prior01"],
        )
        section = gatherer.generate_context_section(ctx)
        assert "[[decision-prior01]]" in section

    def test_wavelength_advisory_when_history_sufficient(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Advisory surfaces only when past-decision history is sufficient."""
        ctx = DecisionContext(
            decision_id="d",
            decision_title="t",
            current_wavelength="peaking",
            related_decisions=["d1", "d2"],
        )
        section = gatherer.generate_context_section(ctx)
        assert "Historically" in section
        assert "peaking" in section

    def test_no_advisory_when_history_insufficient(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Advisory must NOT surface when we lack comparable history."""
        ctx = DecisionContext(
            decision_id="d",
            decision_title="t",
            current_wavelength="rising",
            related_decisions=[],
        )
        section = gatherer.generate_context_section(ctx)
        assert "Historically" not in section

    def test_no_prescriptive_language(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Generated output must not contain prescriptive language."""
        ctx = DecisionContext(
            decision_id="d",
            decision_title="t",
            current_wavelength="rising",
        )
        section = gatherer.generate_context_section(ctx)
        for banned in ("you should", "you need to", "the best option"):
            assert banned.lower() not in section.lower()

    def test_interventions_labelled_as_mapped(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Intervention section should use non-prescriptive framing."""
        ctx = DecisionContext(
            decision_id="d",
            decision_title="t",
            interventions=["Yoga"],
        )
        section = gatherer.generate_context_section(ctx)
        assert "mapped as useful" in section
        assert "Yoga" in section


# ---- apply_guardrails Tests ----


class TestApplyGuardrails:
    """Tests for DecisionContextGatherer.apply_guardrails."""

    def test_strips_you_should(self, gatherer: DecisionContextGatherer) -> None:
        """'you should' becomes descriptive framing."""
        out = gatherer.apply_guardrails("You should take the job.")
        assert "you should" not in out.lower()
        assert "one option" in out.lower()

    def test_strips_best_option(self, gatherer: DecisionContextGatherer) -> None:
        """'the best option' is neutralised."""
        out = gatherer.apply_guardrails("The best option is X.")
        assert "best option" not in out.lower()

    def test_strips_urgency(self, gatherer: DecisionContextGatherer) -> None:
        """'act now' urgency framing is removed."""
        out = gatherer.apply_guardrails("Act now before it's too late.")
        assert "act now" not in out.lower()
        assert "too late" not in out.lower()

    def test_strips_permanence(self, gatherer: DecisionContextGatherer) -> None:
        """'this is permanent' is neutralised."""
        out = gatherer.apply_guardrails("This is permanent, choose wisely.")
        assert "permanent" not in out.lower()

    def test_preserves_neutral_text(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Neutral descriptive text passes through unchanged."""
        msg = "## Context\n\n- [[thread-career]]\n"
        assert gatherer.apply_guardrails(msg) == msg

    def test_case_insensitive_replacement(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Guardrail patterns match regardless of case."""
        out = gatherer.apply_guardrails("YOU SHOULD decide.")
        assert "should" not in out.lower() or "option" in out.lower()


# ---- append_context_section Tests ----


class TestAppendContextSection:
    """Tests for DecisionContextGatherer.append_context_section."""

    @pytest.fixture()
    def existing_decision(self, vault_path: Path) -> Path:
        """Create a decision note without any context section yet."""
        note_path = vault_path / "08-Decisions" / "Active" / "2025-03-10-choice.md"
        _write_post(
            note_path,
            content="## Options\n\n- _A_\n- _B_\n",
            type="decision",
            id="decision-choice01",
            title="Choice",
            status="sensing",
            opened=date(2025, 3, 10).isoformat(),
            wavelength_phase_at_opening="rising",
        )
        return note_path

    def test_appends_new_section(
        self,
        gatherer: DecisionContextGatherer,
        existing_decision: Path,
    ) -> None:
        """Appending should add the Context section to the body."""
        gatherer.append_context_section(
            existing_decision,
            "## Context\n\n_nothing yet_\n",
        )
        content = existing_decision.read_text(encoding="utf-8")
        assert "## Options" in content
        assert "## Context" in content

    def test_replaces_existing_section(
        self,
        gatherer: DecisionContextGatherer,
        existing_decision: Path,
    ) -> None:
        """An existing Context section should be replaced, not duplicated."""
        gatherer.append_context_section(
            existing_decision,
            "## Context\n\nold content\n",
        )
        gatherer.append_context_section(
            existing_decision,
            "## Context\n\nnew content\n",
        )
        content = existing_decision.read_text(encoding="utf-8")
        assert content.count("## Context") == 1
        assert "new content" in content
        assert "old content" not in content

    def test_preserves_frontmatter(
        self,
        gatherer: DecisionContextGatherer,
        existing_decision: Path,
    ) -> None:
        """Appending must not destroy the note's YAML frontmatter."""
        gatherer.append_context_section(
            existing_decision,
            "## Context\n\nbody\n",
        )
        post = frontmatter.load(str(existing_decision))
        assert post["id"] == "decision-choice01"
        assert post["status"] == "sensing"


# ---- decision_from_note Tests ----


class TestDecisionFromNote:
    """Tests for the decision_from_note helper."""

    def test_reads_basic_fields(self, vault_path: Path) -> None:
        """Helper reads id, title, and frequency context from frontmatter."""
        note = vault_path / "08-Decisions" / "Active" / "2025-03-10-x.md"
        _write_post(
            note,
            type="decision",
            id="decision-xyz",
            title="X decision",
            status="sensing",
            opened=date(2025, 3, 10).isoformat(),
            wavelength_phase_at_opening="rising",
            frequency_context=["F1", "F5"],
        )
        decision = decision_from_note(note)
        assert decision.id == "decision-xyz"
        assert decision.title == "X decision"
        assert Frequency.F1 in decision.frequency_context
        assert decision.wavelength_phase_at_opening == "rising"

    def test_skips_unknown_frequencies(self, vault_path: Path) -> None:
        """Unknown frequency codes are dropped silently."""
        note = vault_path / "08-Decisions" / "Active" / "2025-03-10-y.md"
        _write_post(
            note,
            type="decision",
            id="decision-abc",
            title="Y decision",
            status="sensing",
            opened=date(2025, 3, 10).isoformat(),
            frequency_context=["F1", "F99"],
        )
        decision = decision_from_note(note)
        assert decision.frequency_context == [Frequency.F1]

    def test_coerces_phase_enum(self, vault_path: Path) -> None:
        """Unknown phases collapse to an empty string (model default)."""
        note = vault_path / "08-Decisions" / "Active" / "2025-03-10-z.md"
        _write_post(
            note,
            type="decision",
            id="decision-def",
            title="Z decision",
            status="sensing",
            opened=date(2025, 3, 10).isoformat(),
            wavelength_phase_at_opening="not-a-phase",
        )
        decision = decision_from_note(note)
        assert decision.wavelength_phase_at_opening == ""

    def test_accepts_valid_phase(self, vault_path: Path) -> None:
        """Valid phase strings are preserved."""
        note = vault_path / "08-Decisions" / "Active" / "2025-03-10-w.md"
        _write_post(
            note,
            type="decision",
            id="decision-www",
            title="W decision",
            status="sensing",
            opened=date(2025, 3, 10).isoformat(),
            wavelength_phase_at_opening=Phase.PEAKING.value,
        )
        decision = decision_from_note(note)
        assert decision.wavelength_phase_at_opening == "peaking"


# ---- Integration: gather -> render -> append end-to-end ----


class TestEndToEnd:
    """End-to-end flow for context gathering on a real vault."""

    def test_full_flow(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """gather -> generate -> append produces a valid updated note."""
        note_path = seeded_vault / "08-Decisions" / "Active" / "2025-03-10.md"
        _write_post(
            note_path,
            content="## Source Fragments\n\n- frag-01\n",
            type="decision",
            id=sample_decision.id,
            title=sample_decision.title,
            status="sensing",
            opened=sample_decision.opened.isoformat(),
            wavelength_phase_at_opening=sample_decision.wavelength_phase_at_opening,
            frequency_context=[str(f) for f in sample_decision.frequency_context],
        )

        ctx = gatherer.gather_context(sample_decision, seeded_vault)
        section = gatherer.generate_context_section(ctx)
        gatherer.append_context_section(note_path, section)

        content = note_path.read_text(encoding="utf-8")
        assert "## Context" in content
        assert "[[thread-career]]" in content
        assert "[[praxis-goals]]" in content
        assert "[[decision-prior01]]" in content
        assert "peaking" in content
        # Never contains recommendation language:
        for banned in ("you should", "best option", "you need to"):
            assert banned not in content.lower()


# ---------------------------------------------------------------------------
# Issue #1448: the context gatherer's unwitnessed skip and fallback arms
# ---------------------------------------------------------------------------


class TestGatherContextStepsOverUnreadableNotes:
    """One unreadable note anywhere must not empty a context section.

    ``gather_context`` walks four folders through ``_load_post``, and
    every one of those call sites answered a ``None`` with a bare
    ``continue``/``return None`` that nothing had ever driven. Rather
    than five unit tests on five private scorers, this drives the
    **public** entry point with poison in each folder in turn — the same
    path an operator's hand-edited vault takes.

    The assertions name the surviving context by list equality against
    the clean baseline. "It did not raise" would be satisfied by a
    gatherer that returned an empty context for everything, which is
    precisely the regression this guards.
    """

    @staticmethod
    def _context_tuple(ctx: DecisionContext) -> tuple[list[str], list[str], list[str]]:
        """Return the three id lists that must survive a poisoned vault."""
        return (
            sorted(ctx.related_threads),
            sorted(ctx.related_decisions),
            sorted(ctx.relevant_praxis),
        )

    @pytest.mark.parametrize(
        "folder",
        [
            "02-Threads",
            "04-Praxis",
            "08-Decisions/Archive",
            "05-Wavelength/Observations",
        ],
    )
    @pytest.mark.parametrize("shape", CORRUPT_NOTE_SHAPES)
    def test_corrupt_note_in_any_folder_preserves_context(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
        folder: str,
        shape: str,
        corrupt_note: Callable[[Path, str], Path],
    ) -> None:
        """Poison in each walked folder leaves the gathered context intact."""
        baseline = self._context_tuple(
            gatherer.gather_context(sample_decision, seeded_vault),
        )
        assert baseline != ([], [], []), "baseline must be non-empty to be meaningful"

        corrupt_note(seeded_vault / folder / "zz-unreadable.md", shape)

        after = gatherer.gather_context(sample_decision, seeded_vault)
        assert self._context_tuple(after) == baseline

    def test_corrupt_wavelength_note_keeps_the_readable_phase(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
        corrupt_note: Callable[[Path, str], Path],
    ) -> None:
        """The phase still comes from the newest *readable* observation.

        ``current_wavelength`` is not one of the id lists above, so it
        needs its own assertion — otherwise the wavelength walk could
        silently fall back to the decision's own phase and every other
        assertion here would still pass.
        """
        corrupt_note(
            seeded_vault / "05-Wavelength" / "Observations" / "9999-99-99.md",
            "malformed-yaml",
        )

        ctx = gatherer.gather_context(sample_decision, seeded_vault)

        assert ctx.current_wavelength == "peaking"

    def test_notes_without_an_id_are_skipped(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """A readable note carrying no ``id`` never reaches a context list.

        This is the arm *after* the unreadable-note skip: the note parses
        fine, so the ``_load_post`` sentinel never fires and only the
        empty-id guard can reject it. An empty string sneaking into these
        lists would render as a broken ``[[]]`` wiki-link in the note.
        """
        baseline = self._context_tuple(
            gatherer.gather_context(sample_decision, seeded_vault),
        )
        _write_post(
            seeded_vault / "02-Threads" / "no-id-thread.md",
            type="thread",
            title="Career direction and meaningful work",
            frequency_affinity=["F1", "F5"],
        )
        _write_post(
            seeded_vault / "04-Praxis" / "no-id-praxis.md",
            type="praxis",
            title="Quarterly goal setting",
            frequency=["F1", "F5"],
        )
        _write_post(
            seeded_vault / "08-Decisions" / "Archive" / "no-id-decision.md",
            type="decision",
            title="Should I change jobs this year",
            frequency_context=["F1", "F5"],
        )

        after = gatherer.gather_context(sample_decision, seeded_vault)

        assert self._context_tuple(after) == baseline
        for ids in self._context_tuple(after):
            assert "" not in ids

    def test_observation_missing_date_or_phase_is_ignored(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """A half-filled observation cannot become the current phase.

        Both halves are tested at once with two separate notes, each
        dated *after* every readable observation — so if either were
        admitted it would win the recency comparison and change the
        answer.
        """
        _write_post(
            seeded_vault / "05-Wavelength" / "Observations" / "2025-03-20.md",
            type="wavelength_observation",
            id="wave-0320",
            date=date(2025, 3, 20).isoformat(),
        )
        _write_post(
            seeded_vault / "05-Wavelength" / "Observations" / "2025-03-21.md",
            type="wavelength_observation",
            id="wave-0321",
            phase="bottoming",
        )

        ctx = gatherer.gather_context(sample_decision, seeded_vault)

        assert ctx.current_wavelength == "peaking"

    def test_absent_folders_yield_empty_context(
        self,
        gatherer: DecisionContextGatherer,
        tmp_path: Path,
        sample_decision: Decision,
    ) -> None:
        """A vault with none of the walked folders gathers nothing, calmly.

        This is ``_iter_markdown``'s absent-directory arm reached through
        the public API. ``tmp_path`` rather than the ``vault_path``
        fixture, because that fixture scaffolds every folder and the arm
        would never be reached through it.
        """
        assert not (tmp_path / "02-Threads").exists()

        ctx = gatherer.gather_context(sample_decision, tmp_path)

        assert self._context_tuple(ctx) == ([], [], [])


class TestGatherInterventions:
    """``_gather_interventions`` needs a phase, and never repeats a practice."""

    def test_empty_phase_yields_no_interventions(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """Without a phase there is no practice map row to consult."""
        assert gatherer._gather_interventions(["F1", "F5"], "") == []

    def test_practices_are_deduplicated_across_frequencies(
        self,
        gatherer: DecisionContextGatherer,
    ) -> None:
        """A practice shared by two frequencies is listed once, in first-seen order.

        The same frequency is passed twice, which guarantees every
        practice on the second pass is already in ``seen`` — the dedupe
        branch. Asserting the exact list (not its length) is what catches
        a mutant that drops the guard and returns each practice twice.
        """
        once = gatherer._gather_interventions(["F1"], "rising")
        twice = gatherer._gather_interventions(["F1", "F1"], "rising")

        assert once != []
        assert twice == once


class TestCoerceOpenedAndDecisionFromNote:
    """``opened`` is coerced to a date, or dropped so the model default wins."""

    def test_iso_string_becomes_a_date(self, vault_path: Path) -> None:
        """A well-formed ISO string round-trips into a real ``date``."""
        note = vault_path / "08-Decisions" / "Active" / "good.md"
        _write_post(
            note,
            type="decision",
            id="decision-good0001",
            title="A readable decision",
            status="sensing",
            opened="2025-06-01",
        )

        assert decision_from_note(note).opened == date(2025, 6, 1)

    def test_real_date_object_passes_through(self, vault_path: Path) -> None:
        """A YAML date scalar is already a ``date`` and is kept as-is.

        YAML parses an unquoted ``2025-06-02`` into a ``datetime.date``
        before the coercer ever sees it, so this exercises the
        short-circuit rather than the string-parsing branch.
        """
        note = vault_path / "08-Decisions" / "Active" / "yamldate.md"
        note.write_text(
            "---\n"
            "type: decision\n"
            "id: decision-yamldate1\n"
            "title: A YAML-dated decision\n"
            "status: sensing\n"
            "opened: 2025-06-02\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )

        assert decision_from_note(note).opened == date(2025, 6, 2)

    @pytest.mark.parametrize("raw", ["not-a-date", "2025-13-45", "", "  "])
    def test_unparseable_opened_falls_back_to_the_model_default(
        self,
        vault_path: Path,
        raw: str,
    ) -> None:
        """An unparseable ``opened`` is dropped, never passed through as a string.

        The assertion is on the *type*, not just on inequality: the
        failure this pins is ``_coerce_opened`` returning the raw string,
        which would sail into ``Decision.opened`` and make every
        downstream date comparison a ``TypeError``.
        """
        note = vault_path / "08-Decisions" / "Active" / "bad.md"
        _write_post(
            note,
            type="decision",
            id="decision-bad00001",
            title="An unparseable decision",
            status="sensing",
            opened=raw,
        )

        opened = decision_from_note(note).opened

        assert isinstance(opened, date)
        # The model default for a dropped ``opened`` is today's local date.
        assert opened == date.today()

    def test_non_scalar_opened_falls_back_to_the_model_default(
        self,
        vault_path: Path,
    ) -> None:
        """An ``opened`` that is neither a date nor a string is dropped.

        This is the coercer's final fallthrough — a list here reaches
        neither the ``isinstance(raw, date)`` branch nor the string
        branch.
        """
        note = vault_path / "08-Decisions" / "Active" / "listopened.md"
        _write_post(
            note,
            type="decision",
            id="decision-listopen1",
            title="A list-opened decision",
            status="sensing",
            opened=["2025-06-01"],
        )

        assert isinstance(decision_from_note(note).opened, date)


class TestRelatedDecisionRanking:
    """The two ranking arms nothing had driven: too-weak, and not-newer."""

    def test_unrelated_decision_is_scored_out(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """A decision sharing neither tokens nor frequencies is excluded.

        Every prior decision in the seeded vault overlaps on frequency and
        is therefore boosted to the threshold, so the "score below
        threshold" arm had no witness. This note overlaps on nothing, so
        only that arm can reject it — and the assertion names the
        surviving ids rather than merely checking the newcomer's absence,
        which would also pass if the whole section had gone empty.
        """
        _write_post(
            seeded_vault / "08-Decisions" / "Archive" / "2024-01-01-pasta.md",
            type="decision",
            id="decision-pasta001",
            title="Weeknight pasta recipes",
            status="enacted",
            opened=date(2024, 1, 1).isoformat(),
            frequency_context=["F2"],
        )

        ctx = gatherer.gather_context(sample_decision, seeded_vault)

        assert "decision-pasta001" not in ctx.related_decisions
        assert "decision-prior01" in ctx.related_decisions

    def test_older_observation_does_not_displace_a_newer_one(
        self,
        gatherer: DecisionContextGatherer,
        seeded_vault: Path,
        sample_decision: Decision,
    ) -> None:
        """A complete but older observation leaves the current phase alone.

        The filename deliberately sorts *after* the newest observation
        while its ``date`` field is older, so the walk visits it last and
        must reject it on the date comparison rather than on file order.
        Without this, the "not newer" branch never executes and a mutant
        that took the last file it saw would go unnoticed.
        """
        _write_post(
            seeded_vault / "05-Wavelength" / "Observations" / "zzz-old.md",
            type="wavelength_observation",
            id="wave-old001",
            date=date(2025, 1, 1).isoformat(),
            phase="bottoming",
        )

        ctx = gatherer.gather_context(sample_decision, seeded_vault)

        assert ctx.current_wavelength == "peaking"
