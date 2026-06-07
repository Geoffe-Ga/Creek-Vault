"""Tests for weekly/monthly wavelength reports (issue #43).

Covers the `WavelengthTracker.generate_weekly_report` and
`generate_monthly_report` methods that extend the existing tracker with
the §7.1 domain-mapped report templates.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import frontmatter
import pytest

from creek.generate.wavelength import (
    PHASE_DOMAIN_MAPPINGS,
    ModeProfileGenerator,
    WavelengthTracker,
)
from creek.models import (
    Confidence,
    Dosage,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    SourcePlatform,
    VoiceClassification,
    WavelengthClassification,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---- Fixtures ----


def _make_fragment(
    *,
    frag_id: str,
    created: datetime,
    title: str = "test fragment",
    phase: Phase = Phase.UNCLASSIFIED,
    mode: Mode = Mode.UNCLASSIFIED,
    dosage: Dosage = Dosage.UNCLASSIFIED,
    emotional_texture: list[str] | None = None,
    frequency: Frequency = Frequency.UNCLASSIFIED,
    confidence: Confidence | None = None,
) -> Fragment:
    """Construct a test Fragment with wavelength/frequency fields.

    Mirrors ``created`` into ``ingested`` so the FEAT-031
    ``effective_authored_date`` helper — which falls back to
    ``ingested`` when ``authored_at`` is unset — sees the test's
    target date instead of the construction-time wall clock.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        created=created,
        ingested=created,
        frequency=FrequencyClassification(primary=frequency),
        wavelength=WavelengthClassification(
            phase=phase,
            mode=mode,
            dosage=dosage,
        ),
        voice=VoiceClassification(confidence=confidence),
        emotional_texture=emotional_texture or [],
    )


@pytest.fixture()
def tracker() -> WavelengthTracker:
    """Return a default WavelengthTracker."""
    return WavelengthTracker()


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Return an empty vault root.

    Both report methods create ``05-Wavelength/Phase-Maps`` themselves
    via ``mkdir(parents=True, exist_ok=True)``, so the fixture does not
    pre-create it.
    """
    return tmp_path


def _week_fragments() -> list[Fragment]:
    """Return a representative week of classified fragments (Mon → Sun)."""
    base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)  # Monday of ISO week 17
    return [
        _make_fragment(
            frag_id="frag-rising-1",
            title="A new project's first sparks",
            created=base,
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            dosage=Dosage.MEDICINE,
            frequency=Frequency.F3,
            emotional_texture=["enthusiasm", "curiosity"],
            confidence=Confidence.SETTLED,
        ),
        _make_fragment(
            frag_id="frag-rising-2",
            title="Drafting the outline",
            created=base.replace(day=21),
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            dosage=Dosage.MEDICINE,
            frequency=Frequency.F3,
            emotional_texture=["enthusiasm", "focus"],
            confidence=Confidence.CONVICTION,
        ),
        _make_fragment(
            frag_id="frag-rising-3",
            title="Tension in the middle",
            created=base.replace(day=22),
            phase=Phase.RISING,
            mode=Mode.COLLABORATE,
            dosage=Dosage.MEDICINE,
            frequency=Frequency.F4,
            emotional_texture=["focus"],
            confidence=Confidence.SETTLED,
        ),
        _make_fragment(
            frag_id="frag-withdrawal-1",
            title="The first doubts",
            created=base.replace(day=23),
            phase=Phase.WITHDRAWAL,
            mode=Mode.INHABIT,
            dosage=Dosage.TOXIC,
            frequency=Frequency.F3,
            emotional_texture=["doubt"],
            confidence=Confidence.FORMING,
        ),
    ]


# ---- Domain mappings table -----------------------------------------------


class TestPhaseDomainMappings:
    """`PHASE_DOMAIN_MAPPINGS` carries §7.1 domain mappings for each phase."""

    def test_table_has_all_six_classified_phases(self) -> None:
        """Every classified phase has a row in the mapping."""
        for phase in (
            Phase.RISING,
            Phase.PEAKING,
            Phase.WITHDRAWAL,
            Phase.DIMINISHING,
            Phase.BOTTOMING_OUT,
            Phase.RESTORATION,
        ):
            assert phase.value in PHASE_DOMAIN_MAPPINGS

    def test_each_mapping_carries_required_domains(self) -> None:
        """Every row has the eight domains listed in ontology §7.1."""
        required = {
            "season",
            "mood",
            "spaciousness",
            "relation_to_others",
            "relation_to_self",
            "buddhist_attachment",
            "meditation",
            "breath",
        }
        for phase, mapping in PHASE_DOMAIN_MAPPINGS.items():
            assert required.issubset(mapping.keys()), phase

    def test_rising_maps_to_summer_and_belonging(self) -> None:
        """Spot-check the Rising row against ontology §7.1."""
        rising = PHASE_DOMAIN_MAPPINGS[Phase.RISING.value]
        assert rising["season"] == "Summer"
        assert rising["relation_to_others"] == "Belonging"
        assert rising["relation_to_self"] == "Esteem"

    def test_bottoming_out_maps_to_winter_solstice(self) -> None:
        """Spot-check the Bottoming Out row."""
        bottoming = PHASE_DOMAIN_MAPPINGS[Phase.BOTTOMING_OUT.value]
        assert bottoming["season"] == "Winter Solstice"
        assert bottoming["spaciousness"] == "Contracted"


# ---- generate_weekly_report ---------------------------------------------


class TestGenerateWeeklyReport:
    """`generate_weekly_report` writes a §7.1-shaped weekly report."""

    def test_report_filename_has_iso_week_format(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Filename follows ``YYYY-WNN-wavelength.md``."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),  # ISO week 17, Monday
            fragments=_week_fragments(),
        )
        assert path.name == "2026-W17-wavelength.md"
        assert path.parent == vault_path / "05-Wavelength" / "Phase-Maps"
        assert path.is_file()

    def test_frontmatter_records_period_and_iso_week(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Frontmatter carries type, period, week, year."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        post = frontmatter.load(str(path))
        assert post.metadata["type"] == "wavelength_report"
        assert post.metadata["period"] == "weekly"
        assert post.metadata["week"] == 17
        assert post.metadata["year"] == 2026

    def test_body_contains_required_sections(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """All seven §43 sections appear in the rendered body."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        for heading in (
            "## Phase Summary",
            "## Domain Mappings",
            "## Mode Distribution",
            "## Dosage Balance",
            "## Emotional Texture Cloud",
            "## Notable Fragments",
            "## Transition Watch",
        ):
            assert heading in body, heading

    def test_domain_mappings_render_for_dominant_phase(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """The Domain Mappings block names the Rising-row values."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        # Rising → Summer, Belonging, Esteem, etc.
        assert "Summer" in body
        assert "Belonging" in body
        assert "Esteem" in body

    def test_notable_fragments_links_top_confidence(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Notable Fragments lists the highest-confidence fragments."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        # The conviction-confidence fragment should appear; the
        # forming-confidence one is least likely to be selected.
        assert "frag-rising-2" in body
        assert "Drafting the outline" in body

    def test_emotional_texture_cloud_counts_tags(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Emotional Texture Cloud reports counts per tag."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        # `enthusiasm` appears twice across the test fragments.
        assert "enthusiasm" in body

    def test_mode_distribution_lists_observed_modes(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Mode Distribution lists modes observed in the period."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        assert "express" in body.lower()

    def test_dosage_balance_shows_medicine_and_toxic(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Dosage Balance reports both medicine and toxic shares."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        assert "Medicine" in body
        assert "Toxic" in body

    def test_report_is_descriptive_not_prescriptive(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """The report uses descriptive language, never prescriptions."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content.lower()
        for phrase in ("you should", "you must", "you ought", "you need to"):
            assert phrase not in body, phrase

    def test_empty_week_produces_valid_report(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Empty fragment list still produces a parseable report."""
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
            fragments=[],
        )
        post = frontmatter.load(str(path))
        assert post.metadata["period"] == "weekly"
        assert "## Phase Summary" in post.content

    def test_loads_fragments_from_vault_when_omitted(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """When fragments are not passed, they are loaded from the vault."""
        frags_dir = vault_path / "01-Fragments"
        frags_dir.mkdir(parents=True, exist_ok=True)
        seeded = _week_fragments()
        for fragment in seeded:
            data = fragment.model_dump(mode="json")
            post = frontmatter.Post(content=fragment.title, **data)
            (frags_dir / f"{fragment.id}.md").write_text(
                frontmatter.dumps(post),
                encoding="utf-8",
            )
        path = tracker.generate_weekly_report(
            vault_path,
            week_of=date(2026, 4, 20),
        )
        body = frontmatter.load(str(path)).content
        assert "frag-rising-2" in body
        # All four seeded fragments survive the round-trip and reach the
        # report, not just the highest-confidence one.
        for fragment in seeded:
            assert fragment.id in body, fragment.id


# ---- generate_monthly_report --------------------------------------------


class TestGenerateMonthlyReport:
    """`generate_monthly_report` aggregates a calendar month."""

    def test_filename_uses_year_month_format(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Monthly file name is ``YYYY-MM-wavelength.md``."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=_week_fragments(),
        )
        assert path.name == "2026-04-wavelength.md"

    def test_frontmatter_records_month_and_year(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Frontmatter carries period=monthly, month, year."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=_week_fragments(),
        )
        post = frontmatter.load(str(path))
        assert post.metadata["period"] == "monthly"
        assert post.metadata["month"] == 4
        assert post.metadata["year"] == 2026

    def test_body_includes_week_by_week_chart(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Monthly body adds a week-by-week ASCII progression chart."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        assert "## Week-by-Week Progression" in body
        # Bar chart rows look like ``Week NN: #### phase``.
        assert "Week" in body and "#" in body

    def test_body_includes_month_over_month_section(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Monthly body adds a month-over-month comparison."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        assert "## Month-over-Month" in body

    def test_monthly_keeps_required_weekly_sections(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Monthly retains all weekly sections."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=_week_fragments(),
        )
        body = frontmatter.load(str(path)).content
        for heading in (
            "## Phase Summary",
            "## Domain Mappings",
            "## Mode Distribution",
            "## Dosage Balance",
            "## Emotional Texture Cloud",
            "## Notable Fragments",
            "## Transition Watch",
        ):
            assert heading in body, heading

    def test_monthly_empty_data_is_graceful(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Empty fragment list still yields a parseable monthly report."""
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=[],
        )
        post = frontmatter.load(str(path))
        assert post.metadata["month"] == 4

    def test_monthly_only_aggregates_within_month(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Fragments outside the target month are ignored."""
        in_month = _make_fragment(
            frag_id="in-month",
            created=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            dosage=Dosage.MEDICINE,
        )
        out_of_month = _make_fragment(
            frag_id="out-of-month",
            created=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
            phase=Phase.PEAKING,
            mode=Mode.EXPRESS,
            dosage=Dosage.MEDICINE,
        )
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=[in_month, out_of_month],
        )
        body = frontmatter.load(str(path)).content
        assert "in-month" in body
        # The out-of-month fragment should not appear in any section.
        assert "out-of-month" not in body

    def test_rerunning_same_month_overwrites_existing_report(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """Generating the same month twice replaces the file in place."""
        first = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=_week_fragments(),
        )
        first_body_len = first.read_text(encoding="utf-8")
        # Second run with empty fragments should still land at the same path
        # and replace, not append to, the original.
        second = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=[],
        )
        assert second == first
        assert second.read_text(encoding="utf-8") != first_body_len
        # Only one wavelength markdown file should exist for this month.
        files = list((vault_path / "05-Wavelength" / "Phase-Maps").glob("*.md"))
        assert len(files) == 1

    def test_week_by_week_chart_caps_bar_width(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """A busy week renders a capped bar, not 50+ ``#`` characters."""
        base = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)  # Monday of ISO week 15
        many = [
            _make_fragment(
                frag_id=f"frag-{i}",
                created=base,
                phase=Phase.RISING,
                mode=Mode.EXPRESS,
                dosage=Dosage.MEDICINE,
                frequency=Frequency.F3,
            )
            for i in range(50)
        ]
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=many,
        )
        body = frontmatter.load(str(path)).content
        # 50 ``#`` characters in a row would be a regression.
        assert "#" * 30 not in body

    def test_week_by_week_chart_marks_zero_fragment_weeks(
        self,
        tracker: WavelengthTracker,
        vault_path: Path,
    ) -> None:
        """An ``(empty)`` marker distinguishes silent weeks from one-fragment weeks.

        Without the marker a zero-fragment week and a one-fragment week
        would both render with no leading bar (or a stray ``#``), which
        is misleading.
        """
        # Single fragment in week 1 of April; weeks 2-5 stay silent.
        single = _make_fragment(
            frag_id="single",
            created=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            phase=Phase.RISING,
            mode=Mode.EXPRESS,
            dosage=Dosage.MEDICINE,
        )
        path = tracker.generate_monthly_report(
            vault_path,
            month=date(2026, 4, 1),
            fragments=[single],
        )
        body = frontmatter.load(str(path)).content
        assert "(empty)" in body


def _seed_fragments(vault: Path, fragments: list[Fragment]) -> None:
    """Write *fragments* to ``01-Fragments/`` for ModeProfileGenerator tests."""
    frags_dir = vault / "01-Fragments"
    frags_dir.mkdir(parents=True, exist_ok=True)
    for fragment in fragments:
        post = frontmatter.Post(
            content=fragment.title,
            **fragment.model_dump(mode="json"),
        )
        (frags_dir / f"{fragment.id}.md").write_text(
            frontmatter.dumps(post),
            encoding="utf-8",
        )


class TestModeProfileGenerator:
    """Per-mode profile notes under ``05-Wavelength/Mode-Profiles/`` (#583)."""

    def test_writes_one_note_per_nonempty_mode(self, vault_path: Path) -> None:
        """One note per engagement mode that has fragments; aggregates counts."""
        base = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        _seed_fragments(
            vault_path,
            [
                _make_fragment(
                    frag_id="m1",
                    created=base,
                    mode=Mode.EXPRESS,
                    frequency=Frequency.F3,
                    phase=Phase.RISING,
                ),
                _make_fragment(
                    frag_id="m2",
                    created=base,
                    mode=Mode.EXPRESS,
                    frequency=Frequency.F3,
                    phase=Phase.RISING,
                ),
                _make_fragment(
                    frag_id="m3",
                    created=base,
                    mode=Mode.INHABIT,
                    frequency=Frequency.F6,
                    phase=Phase.PEAKING,
                ),
            ],
        )

        written = ModeProfileGenerator().generate_mode_profiles(vault_path)

        assert {p.name for p in written} == {"express.md", "inhabit.md"}
        express = vault_path / "05-Wavelength" / "Mode-Profiles" / "express.md"
        post = frontmatter.load(str(express))
        assert post["type"] == "mode_profile"
        assert post["fragment_count"] == 2
        assert "m1" in post.content
        assert "m2" in post.content

    def test_no_classified_modes_writes_nothing(self, vault_path: Path) -> None:
        """An all-unclassified-mode corpus produces no notes and no folder."""
        _seed_fragments(
            vault_path,
            [_make_fragment(frag_id="u1", created=datetime(2026, 4, 20, tzinfo=UTC))],
        )

        written = ModeProfileGenerator().generate_mode_profiles(vault_path)

        assert written == []
        assert not (vault_path / "05-Wavelength" / "Mode-Profiles").exists()
