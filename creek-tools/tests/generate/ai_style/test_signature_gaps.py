"""Signature under-use is reported, not scored (#1945, option A).

``voice_distance`` measures how much AI slop a draft carries relative to the
owner. Before #1945, under-using a *signature* feature also counted. On a
real 33k-fragment journal vault that made the owner's own register samples
score a median 0.91 against a 0.35 ceiling, so their own writing failed the
voice gate: an essay is not a stack of one-line journal entries.

These tests pin the repair:

* under-use of a live signature lands in ``ScanReport.signature_gaps`` and
  contributes nothing to the distance, so it can neither flag a draft alone
  nor inflate a union beside real slop;
* ``voice_distance == 0.0`` still holds exactly when ``findings`` is empty;
* the fingerprint pools rates by words, so a vault of short notes no longer
  measures an artifactual one-line-fragment rate;
* a markdown heading is not a one-line fragment.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from creek.config import AIStyleConfig
from creek.generate.ai_style import scan
from creek.generate.ai_style.features import one_line_fragment_density
from creek.generate.ai_style.fingerprint import (
    FINGERPRINT_VERSION,
    build_fingerprint,
    load_fingerprint,
)
from creek.generate.ai_style.guard import build_voice_rewrite_prompt
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.tells import word_count

if TYPE_CHECKING:
    from pathlib import Path

_KEY = "one_line_fragment_density"

_FLATTENED = (
    "I went down to the water and it was cold but I stayed anyway while the "
    "light moved across the surface in the way it always does at that hour."
)
"""One dense block: under-uses a one-line-fragment signature by its whole rate."""

_SLOP = (
    "We must delve into the rich tapestry of this journey. It is a testament "
    "to resilience, and we delve deeper into the tapestry of meaning that "
    "underscores every moment of our vibrant, multifaceted lives."
)
"""Dense AI vocabulary: an ``avoid`` family the owner never uses."""


def _fingerprint(**rates: float) -> VoiceFingerprint:
    """Build a non-thin fingerprint with the given per-feature rates."""
    return VoiceFingerprint(
        features={k: FeatureStat(rate=v, support=50) for k, v in rates.items()},
        fragment_count=50,
    )


class TestUnderUseIsAGapNotADistance:
    """A stripped signature is surfaced for review but never scored."""

    def test_flattened_draft_scores_zero_and_reports_the_gap(self) -> None:
        """The signature is live and diverges, yet the distance is exactly 0."""
        report = scan(
            _FLATTENED,
            fingerprint=_fingerprint(**{_KEY: 68.0, "ai_vocab_density": 0.0}),
            config=AIStyleConfig(),
        )

        assert report.voice_distance == 0.0
        assert report.findings == []
        gap = next(g for g in report.signature_gaps if g.feature_key == _KEY)
        assert gap.direction == "under"
        assert gap.user_rate == pytest.approx(68.0)
        assert report.family_evidence[_KEY] == 0.0

    def test_full_weight_cannot_resurrect_under_use(self) -> None:
        """An explicit weight of 1.0 (an old vault's config) still scores 0."""
        config = AIStyleConfig(feature_weights={_KEY: 1.0})
        report = scan(
            _FLATTENED, fingerprint=_fingerprint(**{_KEY: 68.0}), config=config
        )

        assert report.voice_distance == 0.0
        assert [g.feature_key for g in report.signature_gaps] == [_KEY]

    def test_under_use_does_not_inflate_a_union_beside_real_slop(self) -> None:
        """Slop scores the same whether or not the signature is live."""
        config = AIStyleConfig()
        inert = scan(
            _SLOP,
            fingerprint=_fingerprint(**{_KEY: 0.0, "ai_vocab_density": 0.0}),
            config=config,
        )
        live = scan(
            _SLOP,
            fingerprint=_fingerprint(**{_KEY: 68.0, "ai_vocab_density": 0.0}),
            config=config,
        )

        assert inert.voice_distance > config.voice_distance_upper
        assert live.voice_distance == pytest.approx(inert.voice_distance)
        assert live.signature_gaps
        assert not inert.signature_gaps

    def test_over_use_of_an_avoid_tell_is_still_a_finding(self) -> None:
        """Option A removes under-use only: the slop signal is untouched."""
        report = scan(
            _SLOP,
            fingerprint=_fingerprint(**{"ai_vocab_density": 0.0}),
            config=AIStyleConfig(),
        )

        assert report.voice_distance > 0.0
        assert any(f.feature_key == "ai_vocab_density" for f in report.findings)
        assert all(f.direction == "over" for f in report.findings)

    def test_rewrite_prompt_still_asks_to_restore_the_gap(self) -> None:
        """The rewriter still hears about the stripped rhythm, as a restore."""
        report = scan(
            _SLOP,
            fingerprint=_fingerprint(**{_KEY: 68.0, "ai_vocab_density": 0.0}),
            config=AIStyleConfig(),
        )
        gap = report.signature_gaps[0]

        prompt = build_voice_rewrite_prompt(_SLOP, report)

        assert f"(under-use) {gap.message}" in prompt


class TestHeadingsAreNotOneLineFragments:
    """A markdown heading is structure, not the owner's short-beat rhythm."""

    def test_headings_do_not_count(self) -> None:
        """Only the two body one-liners count; the two headings do not."""
        text = "# Title\n\nIt was cold.\n\n## Part two\n\nI stayed anyway."
        words = word_count(text)

        assert one_line_fragment_density(text) == pytest.approx(2 * 1000 / words)


class TestFingerprintPoolsByWords:
    """Per-feature rates are word-weighted, not averaged per fragment."""

    @staticmethod
    def _write(vault: Path, name: str, body: str) -> None:
        """Write one self-authored open fragment."""
        fragments = vault / "01-Fragments"
        fragments.mkdir(parents=True, exist_ok=True)
        (fragments / name).write_text(
            "---\nsource:\n  author: self\n  platform: markdown\n"
            f"privacy_tier: open\n---\n{body}\n",
            encoding="utf-8",
        )

    def test_short_notes_do_not_dominate_the_rate(self, tmp_path: Path) -> None:
        """Rate is total one-liners per 1000 total words, weights being equal.

        Four five-word notes (one one-liner each) and one 94-word block (none)
        pool to ``4 * 1000 / 114``. The old per-fragment mean was
        ``(4 * 200 + 0) / 5 = 160``.
        """
        for index in range(4):
            self._write(tmp_path, f"note_{index}.md", "Bus late again, walked instead.")
        self._write(tmp_path, "essay.md", " ".join(["word"] * 94) + ".")

        fingerprint = build_fingerprint(tmp_path, AIStyleConfig())

        assert fingerprint.fragment_count == 5
        assert fingerprint.features[_KEY].rate == pytest.approx(4 * 1000 / 114)

    def test_a_pre_pooling_fingerprint_is_ignored(self, tmp_path: Path) -> None:
        """A v2 (per-fragment mean) file is not comparable, so it is not used."""
        assert FINGERPRINT_VERSION == 3
        config = AIStyleConfig()
        path = tmp_path / config.fingerprint_path
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "fragment_count": 9,
                    "features": {_KEY: {"rate": 68.0, "support": 9}},
                }
            ),
            encoding="utf-8",
        )

        assert load_fingerprint(tmp_path, config).fragment_count == 0
