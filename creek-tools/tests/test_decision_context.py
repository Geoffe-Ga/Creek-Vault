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

if TYPE_CHECKING:
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
