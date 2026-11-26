"""The end-state voice rating and its monitoring ledger.

Every writing path is judged by :mod:`creek.generate.ai_style.rating`: the
finished text's fingerprint ``voice_distance`` against the vault's ceiling.
Off-voice results are logged for monitoring, never refused.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style.model import Finding, ScanReport, Span
from creek.generate.ai_style.rating import (
    VOICE_RATINGS_RELPATH,
    VoiceRating,
    load_voice_scorer,
    rating_from,
    record_voice_rating,
)
from tests.test_revoice_evidence import install_owner_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

_SLOP = (
    "We must delve into the rich tapestry of this journey. Moreover, it is a "
    "testament to resilience, and we delve deeper into the tapestry."
)


def _finding(key: str, direction: str = "over") -> Finding:
    """Build a minimal finding for *key*."""
    return Finding(
        tell_id=key,
        category="lexical",
        feature_key=key,
        span=Span(0, 0),
        line=1,
        excerpt="",
        draft_rate=1.0,
        user_rate=0.0,
        direction=direction,  # type: ignore[arg-type]  # Issue #1945: test literal
        message="m",
    )


def _ledger(vault: Path) -> list[dict[str, object]]:
    """Return every ledger entry."""
    path = vault / VOICE_RATINGS_RELPATH
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestScorer:
    """A scorer exists only when the vault's fingerprint can judge."""

    def test_a_built_fingerprint_rates_text(self, tmp_path: Path) -> None:
        """Slop is off-voice and names its families; owner prose is in voice."""
        install_owner_fingerprint(tmp_path)
        scorer = load_voice_scorer(tmp_path)
        assert scorer is not None

        slop = scorer.rate(_SLOP)
        clean = scorer.rate("I went to the shop. It was shut. I walked home.")

        assert not slop.in_voice
        assert "ai_vocab_density" in slop.diverging
        assert clean.in_voice
        assert clean.voice_distance == 0.0
        assert clean.voice_distance_upper == pytest.approx(0.35)

    def test_no_fingerprint_means_no_scorer(self, tmp_path: Path) -> None:
        """Never built: nothing to judge against."""
        assert load_voice_scorer(tmp_path) is None

    def test_a_thin_fingerprint_means_no_scorer(self, tmp_path: Path) -> None:
        """A softened score would pass prose it never measured."""
        path = install_owner_fingerprint(tmp_path)
        data = json.loads(path.read_text())
        data["fragment_count"] = AIStyleConfig().min_fingerprint_fragments - 1
        path.write_text(json.dumps(data))

        assert load_voice_scorer(tmp_path) is None

    def test_a_disabled_subsystem_means_no_scorer(self, tmp_path: Path) -> None:
        """``ai_style.enabled: false`` opts the vault out of rating."""
        install_owner_fingerprint(tmp_path)
        (tmp_path / "00-Creek-Meta" / "creek_config.yaml").write_text(
            "ai_style:\n  enabled: false\n"
        )

        assert load_voice_scorer(tmp_path) is None

    def test_an_unloadable_config_means_no_scorer(self, tmp_path: Path) -> None:
        """A malformed config degrades to unrated, never a crash."""
        install_owner_fingerprint(tmp_path)
        (tmp_path / "00-Creek-Meta" / "creek_config.yaml").write_text(
            "ai_style: [unclosed\n"
        )

        assert load_voice_scorer(tmp_path) is None


class TestRating:
    """The rating carries numbers and closed keys only."""

    def test_rating_from_dedupes_and_sorts_family_keys(self) -> None:
        """Repeated findings collapse; gaps are kept apart from findings."""
        report = ScanReport(
            findings=[_finding("b"), _finding("a"), _finding("b")],
            voice_distance=0.5,
            signature_gaps=[_finding("one_line_fragment_density", "under")],
        )

        rating = rating_from(report, AIStyleConfig(voice_distance_upper=0.4))

        assert rating == VoiceRating(
            0.5, 0.4, ("a", "b"), ("one_line_fragment_density",)
        )

    def test_as_dict_rounds_and_reports_in_voice(self) -> None:
        """The wire shape, exactly."""
        rating = VoiceRating(0.123456, 0.35, ("a",), ("g",))

        assert rating.as_dict() == {
            "in_voice": True,
            "voice_distance": 0.1235,
            "voice_distance_upper": 0.35,
            "diverging": ["a"],
            "signature_gaps": ["g"],
        }

    def test_the_ceiling_is_inclusive(self) -> None:
        """At the ceiling is in voice; a hair over is not."""
        assert VoiceRating(0.35, 0.35, (), ()).in_voice
        assert not VoiceRating(0.3501, 0.35, (), ()).in_voice


class TestLedger:
    """Every rating is logged; off-voice also warns; nothing is refused."""

    def test_an_off_voice_rating_is_logged_and_warned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One content-free line, plus a WARNING naming tool and distance."""
        rating = VoiceRating(0.9, 0.35, ("ai_vocab_density",), ())

        with caplog.at_level(logging.WARNING, logger="creek.generate.ai_style.rating"):
            record_voice_rating(
                tmp_path,
                tool="creek.revoice",
                rating=rating,
                attempts=2,
                artifact="07-Voice/Drafts/x.md",
            )

        [entry] = _ledger(tmp_path)
        timestamp = entry.pop("timestamp")
        assert isinstance(timestamp, str)
        assert timestamp.endswith("+00:00")
        assert entry == {
            "tool": "creek.revoice",
            "rated": True,
            "attempts": 2,
            "artifact": "07-Voice/Drafts/x.md",
            "in_voice": False,
            "voice_distance": 0.9,
            "voice_distance_upper": 0.35,
            "diverging": ["ai_vocab_density"],
            "signature_gaps": [],
        }
        assert "off-voice result from creek.revoice: voice_distance 0.9000" in (
            caplog.text
        )

    def test_an_in_voice_rating_is_logged_without_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Monitoring covers successes too, so rates can be computed."""
        with caplog.at_level(logging.WARNING, logger="creek.generate.ai_style.rating"):
            record_voice_rating(
                tmp_path, tool="creek draft", rating=VoiceRating(0.0, 0.35, (), ())
            )
            record_voice_rating(tmp_path, tool="creek draft", rating=None)

        first, second = _ledger(tmp_path)
        assert first["in_voice"] is True
        assert "artifact" not in first
        assert first["attempts"] == 1
        assert {k: second[k] for k in ("tool", "rated")} == {
            "tool": "creek draft",
            "rated": False,
        }
        assert "in_voice" not in second
        assert caplog.text == ""

    def test_a_write_failure_never_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Monitoring must never cost the owner the text it measures."""
        (tmp_path / "00-Creek-Meta").write_text("a file where a folder belongs")

        with caplog.at_level(logging.WARNING, logger="creek.generate.ai_style.rating"):
            record_voice_rating(tmp_path, tool="creek.revoice", rating=None)

        assert "could not record a voice rating for creek.revoice" in caplog.text
