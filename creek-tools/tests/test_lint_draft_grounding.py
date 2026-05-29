"""Tests for the ``draft-grounding`` lint check (issue #355).

The check scans ``07-Voice/Drafts/`` for drafts whose recorded
``derivative_score`` / ``grounding_score`` frontmatter values fall
outside the configured ``draft.derivative_upper`` / ``draft.grounding_lower``
bounds and emits one finding per failing draft. Drafts that pre-date
the guard (no scores in frontmatter) are skipped silently — the
operator can re-run ``creek draft`` to backfill the metric.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
import yaml

from creek.lint import ALL_CHECKS, DETERMINISTIC_CHECKS, LintRunner
from creek.lint.checks import draft_grounding as draft_grounding_check

if TYPE_CHECKING:
    from pathlib import Path


def _seed_meta_dir(vault: Path) -> None:
    """Create the canonical meta + drafts folders the check needs."""
    (vault / "00-Creek-Meta").mkdir(parents=True, exist_ok=True)
    (vault / "07-Voice" / "Drafts").mkdir(parents=True, exist_ok=True)


def _write_draft(
    vault: Path,
    *,
    name: str,
    derivative_score: float | None,
    grounding_score: float | None,
    title: str = "A draft",
) -> None:
    """Write a draft markdown file with the guard's frontmatter shape."""
    meta: dict[str, object] = {
        "type": "draft",
        "title": title,
        "status": "draft",
    }
    if derivative_score is not None:
        meta["derivative_score"] = derivative_score
    if grounding_score is not None:
        meta["grounding_score"] = grounding_score
    target = vault / "07-Voice" / "Drafts" / f"{name}.md"
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content="body", **meta)),
        encoding="utf-8",
    )


def _write_config(
    vault: Path,
    *,
    derivative_upper: float,
    grounding_lower: float,
    grounding_fraction_lower: float | None = None,
) -> None:
    """Drop a ``creek_config.yaml`` with explicit draft thresholds.

    ``grounding_fraction_lower`` defaults to ``grounding_lower`` when not
    supplied, matching the lenient 0.30/0.30 baseline so existing callers
    keep the behaviour they relied on before the knob was split out.
    """
    draft: dict[str, float] = {
        "derivative_upper": derivative_upper,
        "grounding_lower": grounding_lower,
        "grounding_fraction_lower": (
            grounding_lower
            if grounding_fraction_lower is None
            else grounding_fraction_lower
        ),
    }
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        yaml.safe_dump({"draft": draft}),
        encoding="utf-8",
    )


class TestDraftGroundingRegistry:
    """The new check must be reachable through the public lint surface."""

    def test_check_is_registered_as_deterministic(self) -> None:
        """The check is cheap (frontmatter-only) — must live in the default set."""
        assert "draft-grounding" in DETERMINISTIC_CHECKS
        assert "draft-grounding" in ALL_CHECKS

    def test_runner_dispatches_check(self, tmp_path: Path) -> None:
        """The runner registry resolves the new name to its module callable."""
        _seed_meta_dir(tmp_path)
        report = LintRunner(tmp_path).run(checks=["draft-grounding"])
        assert [r.name for r in report.results] == ["draft-grounding"]


class TestDraftGroundingFindings:
    """The check's logic boils down to two threshold comparisons."""

    def test_clean_drafts_produce_no_findings(self, tmp_path: Path) -> None:
        """Balanced scores produce a non-flagging summary."""
        _seed_meta_dir(tmp_path)
        _write_config(tmp_path, derivative_upper=0.85, grounding_lower=0.30)
        _write_draft(
            tmp_path,
            name="2026-05-27-balanced",
            derivative_score=0.5,
            grounding_score=0.6,
        )
        result = draft_grounding_check.run(tmp_path)
        assert result.name == "draft-grounding"
        assert result.findings == []
        # The summary must report that 0 drafts crossed either threshold.
        assert "0 outside" in result.summary or "0 draft" in result.summary

    def test_too_derivative_draft_is_flagged(self, tmp_path: Path) -> None:
        """A draft above ``derivative_upper`` appears in the findings list."""
        _seed_meta_dir(tmp_path)
        _write_config(tmp_path, derivative_upper=0.85, grounding_lower=0.30)
        _write_draft(
            tmp_path,
            name="2026-05-27-derivative",
            derivative_score=0.94,
            grounding_score=0.6,
        )
        result = draft_grounding_check.run(tmp_path)
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert "derivative" in finding.lower()
        assert "0.94" in finding
        assert "2026-05-27-derivative" in finding

    def test_too_ungrounded_draft_is_flagged(self, tmp_path: Path) -> None:
        """A draft below ``grounding_lower`` appears in the findings list."""
        _seed_meta_dir(tmp_path)
        _write_config(tmp_path, derivative_upper=0.85, grounding_lower=0.30)
        _write_draft(
            tmp_path,
            name="2026-05-27-ungrounded",
            derivative_score=0.4,
            grounding_score=0.10,
        )
        result = draft_grounding_check.run(tmp_path)
        assert len(result.findings) == 1
        assert "grounding" in result.findings[0].lower()
        assert "0.10" in result.findings[0]

    def test_drafts_without_scores_are_skipped(self, tmp_path: Path) -> None:
        """Pre-guard drafts (no scores in frontmatter) emit no finding."""
        _seed_meta_dir(tmp_path)
        _write_config(tmp_path, derivative_upper=0.85, grounding_lower=0.30)
        _write_draft(
            tmp_path,
            name="2026-05-27-legacy",
            derivative_score=None,
            grounding_score=None,
        )
        result = draft_grounding_check.run(tmp_path)
        assert result.findings == []

    def test_uses_configured_thresholds(self, tmp_path: Path) -> None:
        """Tightening ``derivative_upper`` flips an otherwise-clean draft."""
        _seed_meta_dir(tmp_path)
        _write_config(tmp_path, derivative_upper=0.5, grounding_lower=0.30)
        _write_draft(
            tmp_path,
            name="2026-05-27-mid",
            derivative_score=0.7,
            grounding_score=0.6,
        )
        result = draft_grounding_check.run(tmp_path)
        assert len(result.findings) == 1
        assert "derivative" in result.findings[0].lower()

    def test_grounding_fraction_lower_is_independent_knob(
        self,
        tmp_path: Path,
    ) -> None:
        """The lint check compares the stored fraction against the *fraction*
        floor, independent of the per-paragraph ``grounding_lower``.

        A stored ``grounding_score`` of 0.50 sits above a strict
        per-paragraph floor (0.90) yet below a tightened fraction floor
        (0.60) — the check must flag it, proving it reads
        ``grounding_fraction_lower`` and not ``grounding_lower``.
        """
        _seed_meta_dir(tmp_path)
        _write_config(
            tmp_path,
            derivative_upper=0.85,
            grounding_lower=0.90,
            grounding_fraction_lower=0.60,
        )
        _write_draft(
            tmp_path,
            name="2026-05-27-fraction",
            derivative_score=0.4,
            grounding_score=0.50,
        )
        result = draft_grounding_check.run(tmp_path)
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert "grounding" in finding.lower()
        assert "0.50" in finding
        assert "0.60" in finding

    def test_grounding_fraction_lower_lenient_passes(self, tmp_path: Path) -> None:
        """The same 0.50-fraction draft passes when the fraction floor is lenient.

        Holding ``grounding_lower`` strict (0.90) but loosening
        ``grounding_fraction_lower`` to 0.40 flips the verdict to clean —
        confirming the fraction floor alone drives the grounding finding.
        """
        _seed_meta_dir(tmp_path)
        _write_config(
            tmp_path,
            derivative_upper=0.85,
            grounding_lower=0.90,
            grounding_fraction_lower=0.40,
        )
        _write_draft(
            tmp_path,
            name="2026-05-27-fraction-ok",
            derivative_score=0.4,
            grounding_score=0.50,
        )
        result = draft_grounding_check.run(tmp_path)
        assert result.findings == []

    def test_drafts_dir_missing_is_a_no_op(self, tmp_path: Path) -> None:
        """A vault with no Drafts directory must produce a clean empty result."""
        # Only meta exists — no 07-Voice/Drafts.
        (tmp_path / "00-Creek-Meta").mkdir(parents=True)
        result = draft_grounding_check.run(tmp_path)
        assert result.findings == []
        assert result.name == "draft-grounding"
