"""Creek, not its clients, decides whether a revoice actually applied a voice.

Regression for the 22 KB *Scattered Minds* analytical essay that came back
``ok``. Its opening was nearly identical, its edits were citation removal,
punctuation, spelling and compression, and one section was generically
rewritten, giving it a large aggregate diff. ``tests/fixtures/revoice/``
reproduces that pair (see its README).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creek.author.revoice_check import (
    MIN_REWRITTEN_RATIO,
    Diagnostic,
    RevoiceReason,
    check_revoice,
    copied_sentence_ratio,
    main,
    rewritten_ratio,
)
from creek.author.revoice_style import (
    StyleVector,
    citation_markers,
    headings,
    render_profile,
    sentences,
    split_sections,
    style_distance,
    style_vector,
    tokens,
)
from tests.test_revoice_evidence import install_fixture_samples

FIXTURES = Path(__file__).parent / "fixtures" / "revoice"
SOURCE = (FIXTURES / "source.md").read_text()
FAILED_AFTER = (FIXTURES / "failed_after.md").read_text()
OWNER_VOICED = (FIXTURES / "owner_voiced.md").read_text()


@pytest.fixture(scope="module")
def owner() -> StyleVector:
    """The owner's profile, measured from the fixture's owner samples only."""
    return style_vector(
        "\n\n".join(
            (FIXTURES / "owner-samples" / f"owner-{i}.txt").read_text()
            for i in (1, 2, 3)
        )
    )


def test_the_failed_after_is_refused_as_insufficient_transformation(
    owner: StyleVector,
) -> None:
    """Acceptance: the reported failure is not a successful revoice."""
    check = check_revoice(SOURCE, FAILED_AFTER, owner=owner)
    assert check.reason is RevoiceReason.INSUFFICIENT_TRANSFORMATION
    assert check.diagnostics == (
        Diagnostic.OPENING_UNCHANGED,
        Diagnostic.SECTION_UNCHANGED,
        Diagnostic.COPYEDIT_ONLY,
        Diagnostic.NO_STYLE_SHIFT,
        Diagnostic.CITATIONS_DROPPED,
    )
    assert check.as_dict()["unchanged_sections"] == [0, 2]


def test_the_failed_after_would_pass_a_raw_diff_criterion(owner: StyleVector) -> None:
    """Why the gate cannot be a raw diff: this failure clears that floor."""
    check = check_revoice(SOURCE, FAILED_AFTER, owner=owner)
    assert check.rewritten_ratio > MIN_REWRITTEN_RATIO
    rewritten_section = check.sections[1]
    assert not rewritten_section.unchanged
    assert rewritten_section.rewritten_ratio > 0.6


def test_a_strong_section_cannot_hide_unchanged_ones(owner: StyleVector) -> None:
    """Each section is judged on its own; the opening is judged too."""
    check = check_revoice(SOURCE, FAILED_AFTER, owner=owner)
    assert [s.unchanged for s in check.sections] == [True, False, True]
    assert check.opening_similarity >= 0.95


def test_a_genuinely_owner_voiced_rewrite_passes(owner: StyleVector) -> None:
    """Positive case: same claims, headings and citations; the owner's cadence."""
    check = check_revoice(SOURCE, OWNER_VOICED, owner=owner)
    assert check.reason is None
    assert check.diagnostics == ()
    assert check.copied_sentence_ratio == pytest.approx(0.0)
    assert check.style_distance_output is not None
    assert check.style_distance_source is not None
    assert check.style_distance_output < check.style_distance_source * 0.6
    assert check.as_dict()["voice_applied"] is True


def test_generic_model_prose_does_not_count_as_the_owner(owner: StyleVector) -> None:
    """A fluent rewrite that moves away from the owner fails the style shift."""
    source = split_sections(SOURCE)[1]
    generic = split_sections(FAILED_AFTER)[1]
    check = check_revoice(source, generic, owner=owner)
    assert check.reason is RevoiceReason.INSUFFICIENT_TRANSFORMATION
    assert check.diagnostics == (
        Diagnostic.NO_STYLE_SHIFT,
        Diagnostic.CITATIONS_DROPPED,
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: text.replace("behaviour", "behavior").replace(";", "."),
        lambda text: text.replace(" (Maté, 1999)", "").replace(" [1]", ""),
        lambda text: "\n\n".join(text.split("\n\n")[::2]),
    ],
    ids=["spelling-and-punctuation", "citation-cleanup", "compression"],
)
def test_cosmetic_edits_cannot_satisfy_the_gate(
    edit: object, owner: StyleVector
) -> None:
    """Copyediting, citation cleanup and shortening are never a voice."""
    assert callable(edit)
    check = check_revoice(SOURCE, edit(SOURCE), owner=owner)
    assert check.reason is RevoiceReason.INSUFFICIENT_TRANSFORMATION
    assert Diagnostic.OPENING_UNCHANGED in check.diagnostics


def test_identity_reports_every_transformation_finding(owner: StyleVector) -> None:
    """An echo fails every transformation check and no fidelity check."""
    check = check_revoice(SOURCE, SOURCE, owner=owner)
    assert check.diagnostics == (
        Diagnostic.OPENING_UNCHANGED,
        Diagnostic.SECTION_UNCHANGED,
        Diagnostic.COPYEDIT_ONLY,
        Diagnostic.NO_STYLE_SHIFT,
    )


def test_fidelity_alone_is_its_own_reason() -> None:
    """A real rewrite that loses a heading is a fidelity violation."""
    output = OWNER_VOICED.replace("## What Follows From This", "## So What?")
    check = check_revoice(SOURCE, output, owner=None)
    assert check.reason is RevoiceReason.FIDELITY_VIOLATION
    assert check.diagnostics == (Diagnostic.HEADINGS_CHANGED,)
    assert len(check.sections) == 1  # misaligned headings: judged as a whole


def test_without_a_profile_the_style_shift_is_not_measured() -> None:
    """Skill-only grounding is judged on transformation and fidelity alone."""
    check = check_revoice(SOURCE, OWNER_VOICED, owner=None)
    assert check.reason is None
    assert check.style_distance_output is None


def test_rewritten_ratio_counts_only_new_words() -> None:
    """Deletion is not rewriting; an empty output divides by nothing."""
    assert rewritten_ratio(SOURCE, "\n\n".join(SOURCE.split("\n\n")[:2])) == 0.0
    assert rewritten_ratio(SOURCE, "") == 0.0
    assert copied_sentence_ratio(SOURCE, "") == 0.0


def test_verdict_projection_is_content_free(owner: StyleVector) -> None:
    """The wire shape carries numbers and closed codes, never prose."""
    payload = check_revoice(SOURCE, FAILED_AFTER, owner=owner).as_dict()
    assert set(payload) == {
        "voice_applied",
        "diagnostics",
        "rewritten_ratio",
        "copied_sentence_ratio",
        "opening_similarity",
        "unchanged_sections",
        "style_distance_source",
        "style_distance_output",
    }
    assert "Maté" not in json.dumps(payload)
    assert "attention" not in json.dumps(payload).lower()


def test_apparatus_is_extracted_and_removed_from_prose() -> None:
    """Citations, links, footnotes and headings are apparatus, not voice."""
    text = (
        "# Title\n\nA claim (Smith, 2019) with [a link](https://example.org/x) "
        "and a note.[^n] See https://example.org/y [2, 3].\n\n[^n]: The note."
    )
    assert citation_markers(text) == {
        "(Smith, 2019)",
        "https://example.org/x",
        "[^n]",
        "https://example.org/y",
        "[2, 3]",
    }
    assert headings(text) == ("title",)
    assert tokens(text) == [
        "a",
        "claim",
        "with",
        "a",
        "link",
        "and",
        "a",
        "note",
        "see",
    ]
    assert sentences(text) == ["A claim with a link and a note.", "See."]


def test_style_vector_measures_cadence_and_habits() -> None:
    """Every axis is measured, and an empty passage is all zeros."""
    vector = style_vector("I can't stop. Why not? Because it's mine — always (mostly).")
    assert vector.words == 10
    assert vector.question_share == pytest.approx(1 / 3)
    assert vector.short_sentence_share == 1.0
    assert vector.em_dashes_per_k > 0
    assert vector.parentheticals_per_k > 0
    assert vector.first_person_per_k > 0
    assert vector.contractions_per_k > 0
    empty = style_vector("")
    assert empty.words == 0
    assert empty.mean_sentence_words == 0.0
    assert style_distance(empty, empty) == 0.0


def test_profile_rendering_carries_no_owner_words(owner: StyleVector) -> None:
    """The prompt-facing profile is numbers, not the owner's sentences."""
    rendered = render_profile(owner)
    assert rendered.startswith("## Owner style profile")
    for word in ("busyness", "housekeeping", "brittle"):
        assert word not in rendered


def test_local_command_reports_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m creek.author.revoice_check`` judges a local pair."""
    install_fixture_samples(tmp_path)
    source, after = str(FIXTURES / "source.md"), str(FIXTURES / "failed_after.md")
    assert main([source, after, "--vault", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "insufficient_transformation"
    assert report["voice_check"]["style_distance_output"] is not None
    assert main([source, str(FIXTURES / "owner_voiced.md")]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] is None
