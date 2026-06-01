"""Tests for the ``voice-fidelity`` lint check (FEAT-040.10).

The check is the audit surface for the voice-fidelity subsystem: it walks
``07-Voice/Drafts/``, reads the ``voice_distance`` / ``voice_findings``
frontmatter stamped by the FEAT-040.9 guard (re-scanning unstamped files
against the fingerprint), and surfaces any draft whose distance exceeds
``ai_style.voice_distance_upper``. It is framed as "this diverges from your
voice", never as AI accusation, and it never mutates a file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.config import AIStyleConfig
from creek.generate.ai_style.fingerprint import save_fingerprint
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint
from creek.lint import ALL_CHECKS, DETERMINISTIC_CHECKS, LintRunner
from creek.lint.checks import voice_fidelity as voice_fidelity_check

if TYPE_CHECKING:
    from pathlib import Path

_TROPEY = (
    "Additionally, this delves into the rich tapestry of the subject. "
    "Moreover, it is important to note the vibrant and multifaceted nuances. "
    "Furthermore, the landscape stands as a testament to its significance."
)


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

    def test_high_distance_stamped_is_flagged(self, tmp_path: Path) -> None:
        """A stamped draft above the ceiling appears in the findings."""
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_config(tmp_path, voice_distance_upper=0.1)
        _write_draft(
            tmp_path,
            name="2026-05-30-divergent",
            voice_distance=0.5,
            voice_findings=[
                {"feature_key": "ai_vocab_density", "direction": "over"},
            ],
        )
        result = voice_fidelity_check.run(tmp_path)
        assert result.name == "voice-fidelity"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert "2026-05-30-divergent" in finding
        assert "0.50" in finding
        assert "ai_vocab_density" in finding

    def test_on_voice_stamped_is_silent(self, tmp_path: Path) -> None:
        """A stamped draft under the ceiling produces no finding."""
        _seed(tmp_path)
        _write_fingerprint(tmp_path)
        _write_config(tmp_path, voice_distance_upper=0.35)
        _write_draft(tmp_path, name="2026-05-30-onvoice", voice_distance=0.05)
        result = voice_fidelity_check.run(tmp_path)
        assert result.findings == []
        assert "0 " in result.summary

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
