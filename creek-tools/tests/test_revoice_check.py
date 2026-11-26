"""Creek, not its clients, judges whether a revoice delivered the owner's voice.

Judged on the end state: the finished rewrite's rating against the vault's
voice fingerprint, plus fidelity (headings and citations survive). Changed
words are not counted. Regression fixtures for the 22 KB *Scattered Minds*
analytical essay live in ``tests/fixtures/revoice/`` (see its README).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creek.author.revoice_check import (
    Diagnostic,
    RevoiceReason,
    check_revoice,
    main,
)
from creek.author.revoice_style import (
    StyleVector,
    citation_markers,
    headings,
    render_profile,
    sentences,
    style_vector,
    tokens,
)
from creek.generate.ai_style.rating import VoiceRating, load_voice_scorer
from tests.test_revoice_evidence import install_owner_fingerprint

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


@pytest.fixture
def rate(tmp_path: Path):
    """Rate text against the fixture owner's fingerprint."""
    install_owner_fingerprint(tmp_path)
    scorer = load_voice_scorer(tmp_path)
    assert scorer is not None
    return scorer.rate


def test_the_failed_after_rates_off_voice_and_loses_citations(rate) -> None:
    """Acceptance: the reported failure is not a successful revoice."""
    check = check_revoice(SOURCE, FAILED_AFTER, rating=rate(FAILED_AFTER))
    assert check.diagnostics == (Diagnostic.OFF_VOICE, Diagnostic.CITATIONS_DROPPED)
    assert check.reason is RevoiceReason.FIDELITY_VIOLATION
    assert check.as_dict()["voice_applied"] is False
    rating = check.as_dict()["voice_rating"]
    assert rating["voice_distance"] > rating["voice_distance_upper"]
    assert "ai_vocab_density" in rating["diverging"]


def test_a_genuinely_owner_voiced_rewrite_is_clean(rate) -> None:
    """Positive case: same claims, headings and citations; the owner's voice."""
    check = check_revoice(SOURCE, OWNER_VOICED, rating=rate(OWNER_VOICED))
    assert check.clean
    assert check.reason is None
    assert check.as_dict()["voice_applied"] is True
    assert check.distance == 0.0


def test_an_echo_of_an_in_voice_passage_succeeds(rate) -> None:
    """The end state decides, not the edit: an unchanged owner passage is in voice.

    The pre-rating gate refused this as a copyedit.
    """
    check = check_revoice(OWNER_VOICED, OWNER_VOICED, rating=rate(OWNER_VOICED))
    assert check.clean
    assert check.as_dict()["voice_applied"] is True


def test_off_voice_is_reported_but_never_refused(rate) -> None:
    """A faithful rewrite in generic prose is diagnosed, not refused."""
    generic = OWNER_VOICED + "\n\nMoreover, we must delve into this rich tapestry."
    check = check_revoice(SOURCE, generic, rating=rate(generic))
    assert check.diagnostics == (Diagnostic.OFF_VOICE,)
    assert check.reason is None
    assert not check.clean
    assert check.as_dict()["voice_applied"] is False


def test_fidelity_alone_is_its_own_reason(rate) -> None:
    """An in-voice rewrite that loses a heading is a fidelity violation."""
    output = OWNER_VOICED.replace("## What Follows From This", "## So What?")
    check = check_revoice(SOURCE, output, rating=rate(output))
    assert check.reason is RevoiceReason.FIDELITY_VIOLATION
    assert check.diagnostics == (Diagnostic.HEADINGS_CHANGED,)


def test_an_unrated_rewrite_is_judged_on_fidelity_only() -> None:
    """Without a fingerprint nothing is claimed about the voice."""
    check = check_revoice(SOURCE, OWNER_VOICED, rating=None)
    assert check.clean
    assert check.distance == 0.0
    assert check.as_dict() == {
        "voice_applied": None,
        "diagnostics": [],
        "voice_rating": None,
    }


def test_the_boundary_is_inclusive() -> None:
    """A rating exactly at the ceiling is in voice."""
    at = VoiceRating(0.35, 0.35, (), ())
    over = VoiceRating(0.3501, 0.35, ("ai_vocab_density",), ())
    assert check_revoice(SOURCE, OWNER_VOICED, rating=at).clean
    assert check_revoice(SOURCE, OWNER_VOICED, rating=over).diagnostics == (
        Diagnostic.OFF_VOICE,
    )


def test_verdict_projection_is_content_free(rate) -> None:
    """The wire shape carries numbers and closed codes, never prose."""
    payload = check_revoice(SOURCE, FAILED_AFTER, rating=rate(FAILED_AFTER)).as_dict()
    assert set(payload) == {"voice_applied", "diagnostics", "voice_rating"}
    assert set(payload["voice_rating"]) == {
        "in_voice",
        "voice_distance",
        "voice_distance_upper",
        "diverging",
        "signature_gaps",
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


def test_profile_rendering_carries_no_owner_words(owner: StyleVector) -> None:
    """The prompt-facing profile is numbers, not the owner's sentences."""
    rendered = render_profile(owner)
    assert rendered.startswith("## Owner style profile")
    for word in ("busyness", "housekeeping", "brittle"):
        assert word not in rendered


def test_local_command_reports_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m creek.author.revoice_check`` rates a local pair."""
    install_owner_fingerprint(tmp_path)
    source, after = str(FIXTURES / "source.md"), str(FIXTURES / "failed_after.md")
    assert main([source, after, "--vault", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "fidelity_violation"
    assert report["voice_check"]["voice_applied"] is False
    assert report["voice_check"]["voice_rating"]["in_voice"] is False
    voiced = str(FIXTURES / "owner_voiced.md")
    assert main([source, voiced, "--vault", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["voice_check"]["voice_applied"] is True


def test_local_command_without_a_fingerprint_is_unrated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No fingerprint: the pair is checked for fidelity only and left unrated."""
    source, voiced = str(FIXTURES / "source.md"), str(FIXTURES / "owner_voiced.md")
    assert main([source, voiced, "--vault", str(tmp_path)]) == 0
    check = json.loads(capsys.readouterr().out)["voice_check"]
    assert check == {"voice_applied": None, "diagnostics": [], "voice_rating": None}
