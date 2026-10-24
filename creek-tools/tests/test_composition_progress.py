"""Stage-boundary reporting for the two composition pipelines (#1966).

``creek.draft`` and ``creek.author`` stream MCP progress while they compose,
and the stage names they stream come from here: the pipelines themselves say
when they cross a boundary, through a sink that receives a
:class:`~creek.progress.CompositionStage` and nothing else. These tests pin the
vocabulary, the order each pipeline reports in, and that a sink sees no
content — only enum members.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author import run_author
from creek.generate.drafts import DraftGenerator
from creek.generate.mining import IdeaSeed, MiningStrategy
from creek.progress import (
    CompositionStage,
    ignore_stage,
    once_per_stage,
)

if TYPE_CHECKING:
    from pathlib import Path


def _sourceless_seed() -> IdeaSeed:
    """Return the ontology-tuple seed a bare vault mines: no vault content."""
    return IdeaSeed(
        strategy=MiningStrategy.UNEXPLORED_ONTOLOGY,
        title="Unwalked ground",
        source_fragments=(),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="An ontology combination not yet inhabited.",
        score=0.5,
    )


def test_the_stage_vocabulary_is_the_six_draft_boundaries_in_order() -> None:
    """The enum is the closed, ordered list of draft stage names."""
    assert [stage.value for stage in CompositionStage] == [
        "seed",
        "skills",
        "sources",
        "composed",
        "voice_guarded",
        "saved",
    ]


def test_ignore_stage_accepts_every_stage_and_returns_nothing() -> None:
    """The default sink is callable with any stage and has no effect."""
    for stage in CompositionStage:
        ignore_stage(stage)


def test_once_per_stage_forwards_first_sightings_only() -> None:
    """A repeated stage is dropped, so the forwarded sequence stays monotonic."""
    seen: list[CompositionStage] = []
    sink = once_per_stage(seen.append)
    for stage in (
        CompositionStage.SOURCES,
        CompositionStage.COMPOSED,
        CompositionStage.VOICE_GUARDED,
        CompositionStage.COMPOSED,
        CompositionStage.VOICE_GUARDED,
    ):
        sink(stage)
    assert seen == [
        CompositionStage.SOURCES,
        CompositionStage.COMPOSED,
        CompositionStage.VOICE_GUARDED,
    ]


def test_draft_generator_reports_its_five_boundaries_in_order(tmp_path: Path) -> None:
    """``generate_draft`` then ``save_draft`` cross skills .. saved, in order.

    ``SEED`` is not the generator's to report — the seed is chosen before the
    generator exists — so the generator's own sequence starts at ``SKILLS``.
    """
    (tmp_path / "01-Fragments").mkdir()
    (tmp_path / "creek-skills").mkdir()
    seen: list[object] = []
    generator = DraftGenerator(
        llm=lambda prompt: "A drafted body of words.",
        skills_root=tmp_path / "creek-skills",
        on_stage=seen.append,
    )
    draft = generator.generate_draft(_sourceless_seed(), vault_path=tmp_path)
    assert seen == [
        CompositionStage.SKILLS,
        CompositionStage.SOURCES,
        CompositionStage.COMPOSED,
    ]
    generator.save_draft(draft, tmp_path)
    assert seen == [
        CompositionStage.SKILLS,
        CompositionStage.SOURCES,
        CompositionStage.COMPOSED,
        CompositionStage.VOICE_GUARDED,
        CompositionStage.SAVED,
    ]
    assert all(isinstance(stage, CompositionStage) for stage in seen)


def test_draft_generator_without_a_sink_still_drafts(tmp_path: Path) -> None:
    """Omitting ``on_stage`` keeps every existing caller working unchanged."""
    (tmp_path / "creek-skills").mkdir()
    generator = DraftGenerator(
        llm=lambda prompt: "A drafted body of words.",
        skills_root=tmp_path / "creek-skills",
    )
    draft = generator.generate_draft(_sourceless_seed(), vault_path=tmp_path)
    assert generator.save_draft(draft, tmp_path).is_file()


def test_the_writing_desk_reports_sources_composed_and_voice_guarded(
    tmp_path: Path,
) -> None:
    """``run_author`` reports each of its three boundaries exactly once.

    The desk revises inside a bounded loop, so it crosses ``COMPOSED`` and
    ``VOICE_GUARDED`` once per round; the sink must still see each once.
    """
    seen: list[object] = []
    run_author(
        medium="research",
        query="What is pluralism?",
        vault=tmp_path,
        max_rounds=3,
        on_stage=seen.append,
    )
    assert seen == [
        CompositionStage.SOURCES,
        CompositionStage.COMPOSED,
        CompositionStage.VOICE_GUARDED,
    ]
