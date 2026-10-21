"""Tests for the ``voice-fidelity`` lint check (FEAT-040.10).

The check is the audit surface for the voice-fidelity subsystem: it walks
``07-Voice/Drafts/``, measures each draft's body against the fingerprint,
and surfaces any draft whose distance exceeds
``ai_style.voice_distance_upper``. It is framed as "this diverges from your
voice", never as AI accusation, and it never mutates a file.

Two #1936 properties are pinned here. The check must actually fire on a real
draft — a 60-word body tripping two tell families is surfaced at the shipped
0.35 ceiling, where the pre-#1936 weighted mean scored it 0.0987 and said
nothing. And the stamped ``voice_distance`` no longer decides: a stamp
records what some earlier run measured, against the fingerprint, config and
aggregation of that moment, so the audit pass re-measures the prose in front
of it and the same content cannot reach two verdicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import load_fingerprint, save_fingerprint
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.generate.ai_style.scanner import scan
from creek.lint import ALL_CHECKS, DETERMINISTIC_CHECKS, LintRunner
from creek.lint.checks import voice_fidelity as voice_fidelity_check

if TYPE_CHECKING:
    from pathlib import Path

_TROPEY = (
    "Additionally, this delves into the rich tapestry of the subject. "
    "Moreover, it is important to note the vibrant and multifaceted nuances. "
    "Furthermore, the landscape stands as a testament to its significance."
)

_ON_VOICE = (
    "I woke before the alarm. The kettle took its time. I stood at the window "
    "and watched the street fill up. I did not write anything down. I made "
    "toast and ate it standing. I washed the one cup and left it to dry."
)
"""Invented plain prose that fires no tell, so it must never be surfaced."""

_TWO_FAMILIES = (
    f"{_ON_VOICE} I had to delve into the tapestry of it. "
    "The hour plays a vital role in the day."
)
"""The same prose plus two obviously synthetic slop sentences (#1936).

Trips exactly two tell families — AI vocabulary and significance puffery —
at 62 words. Measures 0.9682 under the union aggregation and 0.0987 under
the weighted mean it replaced, so the shipped 0.35 ceiling now surfaces it
and used not to.
"""


def _seed(vault: Path) -> None:
    """Create the meta + drafts folders the check needs."""
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    (vault / "07-Voice" / "Drafts").mkdir(parents=True, exist_ok=True)


def _write_fingerprint(vault: Path, *, fragments: int = 20) -> None:
    """Persist a non-thin fingerprint with near-zero AI-ish rates."""
    save_fingerprint(
        VoiceFingerprint(
            features={"em_dash_density": FeatureStat(rate=0.0, support=fragments)},
            fragment_count=fragments,
        ),
        vault,
        AIStyleConfig(),
    )


def _write_config(vault: Path, *, voice_distance_upper: float) -> None:
    """Drop a creek_config.yaml with an explicit voice-distance ceiling."""
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        yaml.safe_dump({"ai_style": {"voice_distance_upper": voice_distance_upper}}),
        encoding="utf-8",
    )


def _write_draft(
    vault: Path,
    *,
    name: str,
    body: str = "A plain body.",
    voice_distance: float | None = None,
    voice_findings: list[dict[str, object]] | None = None,
) -> Path:
    """Write a draft markdown file with optional guard frontmatter."""
    meta: dict[str, object] = {"type": "draft", "title": name, "status": "draft"}
    if voice_distance is not None:
        meta["voice_distance"] = voice_distance
    if voice_findings is not None:
        meta["voice_findings"] = voice_findings
    target = vault / "07-Voice" / "Drafts" / f"{name}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **meta)),
        encoding="utf-8",
    )
    return target


class TestVoiceFidelityRegistry:
    """The check must be reachable through the public lint surface."""

    def test_registered_as_deterministic(self) -> None:
        """Frontmatter-read + regex re-scan is cheap — it runs by default."""
        assert "voice-fidelity" in DETERMINISTIC_CHECKS
        assert "voice-fidelity" in ALL_CHECKS

    def test_runner_dispatches_check(self, tmp_path: Path) -> None:
        """The registry resolves the name to its module callable."""
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        report = LintRunner(tmp_path).run(checks=["voice-fidelity"])
        assert [r.name for r in report.results] == ["voice-fidelity"]


class TestVoiceFidelityFindings:
    """Threshold comparison over stamped and unstamped drafts."""

    def test_a_draft_tripping_two_families_is_flagged(self, tmp_path: Path) -> None:
        """#1936's headline: a short real draft clears the *shipped* ceiling.

        No config is written, so this runs at the stock 0.35 — the ceiling
        the pre-#1936 mean scored this body 0.0987 against and said nothing.
        """
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_draft(tmp_path, name="2026-05-30-divergent", body=_TWO_FAMILIES)
        result = voice_fidelity_check.run(tmp_path)
        assert result.name == "voice-fidelity"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert "2026-05-30-divergent" in finding
        assert "0.97" in finding
        assert "ai_vocab_density" in finding

    def test_an_on_voice_draft_is_silent(self, tmp_path: Path) -> None:
        """The same prose without the slop sentences is not surfaced."""
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_draft(tmp_path, name="2026-05-30-onvoice", body=_ON_VOICE)
        result = voice_fidelity_check.run(tmp_path)
        assert result.findings == []
        assert "0 " in result.summary

    def test_a_stale_stamp_does_not_decide(self, tmp_path: Path) -> None:
        """The body is measured, not the stamp — in both directions (#1936).

        A stamp is one earlier run's measurement. Trusting it would mean the
        same content reached two verdicts depending on whether a guard had
        been past, and would leave every draft stamped under the old
        diluting mean permanently unflaggable.
        """
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_draft(
            tmp_path,
            name="2026-05-30-understamped",
            body=_TWO_FAMILIES,
            voice_distance=0.0987,
            voice_findings=[{"feature_key": "ai_vocab_density", "direction": "over"}],
        )
        _write_draft(
            tmp_path,
            name="2026-05-30-overstamped",
            body=_ON_VOICE,
            voice_distance=0.99,
        )
        findings = voice_fidelity_check.run(tmp_path).findings
        assert len(findings) == 1
        assert "2026-05-30-understamped" in findings[0]

    def test_a_stamped_and_an_unstamped_copy_agree(self, tmp_path: Path) -> None:
        """Same content, one stamped by the guard and one not: one verdict.

        The stamp is written by the real guard rather than hand-set, so this
        is the production shape of the AC: a draft the guard measured and a
        hand-authored copy of the same prose are judged identically.
        """
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        fingerprint = load_fingerprint(tmp_path, AIStyleConfig())
        measured = scan(_TWO_FAMILIES, fingerprint=fingerprint, config=AIStyleConfig())
        _write_draft(
            tmp_path,
            name="2026-05-30-stamped-copy",
            body=_TWO_FAMILIES,
            voice_distance=round(measured.voice_distance, 4),
        )
        _write_draft(tmp_path, name="2026-05-30-plain-copy", body=_TWO_FAMILIES)
        findings = voice_fidelity_check.run(tmp_path).findings
        assert len(findings) == 2
        assert {finding.split("`")[1] for finding in findings} == {
            "2026-05-30-stamped-copy",
            "2026-05-30-plain-copy",
        }

    def test_unstamped_draft_is_rescanned(self, tmp_path: Path) -> None:
        """A draft with no stamped distance is re-scanned against the fingerprint."""
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_config(tmp_path, voice_distance_upper=0.05)
        _write_draft(tmp_path, name="2026-05-30-unstamped", body=_TROPEY)
        result = voice_fidelity_check.run(tmp_path)
        assert len(result.findings) == 1
        assert "2026-05-30-unstamped" in result.findings[0]

    def test_missing_fingerprint_is_informational(self, tmp_path: Path) -> None:
        """No fingerprint yields one informational finding, not an error."""
        _seed(tmp_path)
        _write_draft(tmp_path, name="2026-05-30-anything", body=_TROPEY)
        result = voice_fidelity_check.run(tmp_path)
        assert result.name == "voice-fidelity"
        assert len(result.findings) == 1
        assert "fingerprint" in result.findings[0].lower()

    def test_drafts_dir_missing_is_a_no_op(self, tmp_path: Path) -> None:
        """A vault with a fingerprint but no Drafts dir is a clean no-op."""
        (tmp_path / "00-Creek-Meta").mkdir(parents=True)
        _write_fingerprint(tmp_path)
        result = voice_fidelity_check.run(tmp_path)
        assert result.findings == []
        assert result.name == "voice-fidelity"

    def test_does_not_mutate_files(self, tmp_path: Path) -> None:
        """The check must leave every scanned draft byte-identical."""
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_config(tmp_path, voice_distance_upper=0.05)
        stamped = _write_draft(tmp_path, name="2026-05-30-stamped", voice_distance=0.5)
        unstamped = _write_draft(tmp_path, name="2026-05-30-plain", body=_TROPEY)
        before = {p: p.read_bytes() for p in (stamped, unstamped)}
        voice_fidelity_check.run(tmp_path)
        after = {p: p.read_bytes() for p in (stamped, unstamped)}
        assert before == after
