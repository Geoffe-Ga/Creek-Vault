"""End-to-end: ``creek draft`` feeds ``creek lint --check draft-grounding`` (#1040).

The two commands were shipped as a pair — the guard stamps
``derivative_score`` / ``grounding_score`` into a saved draft, and the lint
check audits those scores against the vault's configured thresholds — but
nothing ever joined them. The guard was never wired into ``creek draft``, so
the check's "no scores → clean" branch swallowed every vault, and a test that
only asserted ``creek lint`` exited 0 would have called that a pass.

This harness runs both real commands, in order, over the same throwaway
vault, and asserts on the **lint report the second command wrote**:

* an ungrounded draft must produce a finding naming its failing metric, and
* an in-bounds draft from the identical path must produce none.

Only the LLM hop and the sentence-transformer weights are stubbed; the draft
composition, the grounding scoring, the frontmatter write, the check
registry, the threshold resolution and the report render are all production
code. Marked ``e2e`` so it runs in CI's ``integration-e2e`` lane rather than
the default unit gate; the wiring assertions that must run on every commit
live in ``tests/test_draft_grounding_wiring.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.link.embeddings import EmbeddingLinker
from tests.grounding_stubs import (
    GROUNDED_SOURCE_BODY,
    IN_BOUNDS_DRAFT_BODY,
    UNGROUNDED_DRAFT_BODY,
    BagOfWordsEncoder,
)
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.e2e

runner = CliRunner()

_SOURCE_ID = "frag-ground-e2e"


def _seed() -> Any:
    """Build the single idea seed the miner is forced to surface.

    Returns ``Any`` because :class:`~creek.generate.mining.IdeaSeed` is
    imported inside the function, matching
    ``tests/test_draft_grounding_wiring.py``.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Grounding audit chain",
        source_fragments=(_SOURCE_ID,),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="A draft the lint check must be able to judge.",
        score=0.8,
    )


def _draft_then_lint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> tuple[str, dict[str, Any]]:
    """Run ``creek draft`` then ``creek lint`` over one throwaway vault.

    Args:
        tmp_path: Pytest's per-test temporary directory.
        monkeypatch: Patch handle for the LLM and the embedding weights.
        body: The exact draft body the stubbed LLM emits.

    Returns:
        ``(lint_report_text, draft_frontmatter)``. Callers assert on both:
        the report alone cannot distinguish "the check judged this draft
        clean" from "the check found nothing to judge", which is precisely
        the ambiguity #1040 was made of.
    """
    import creek.cli as cli_module

    vault = tmp_path / "vault"
    for sub in ("01-Fragments/Notes", "02-Threads", "03-Eddies", "07-Voice/Drafts"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    write_raw_fragment_file(
        vault,
        "01-Fragments/Notes",
        _SOURCE_ID,
        "Phonetic source",
        body=GROUNDED_SOURCE_BODY,
    )
    monkeypatch.setattr(
        EmbeddingLinker,
        "load_model",
        lambda _self: BagOfWordsEncoder(),
    )
    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _prompt: body,
    )
    monkeypatch.setattr(
        "creek.generate.mining.IdeaMiner.mine_all",
        lambda _self, _vault, *, current_phase=None: [_seed()],
    )

    drafted = runner.invoke(app, ["draft", "--vault", str(vault)])
    assert drafted.exit_code == 0, drafted.output

    linted = runner.invoke(
        app,
        ["lint", "--vault", str(vault), "--check", "draft-grounding"],
    )
    assert linted.exit_code == 0, linted.output
    reports = sorted((vault / "00-Creek-Meta" / "Processing-Log").glob("lint-*.md"))
    assert len(reports) == 1, f"expected one lint report, got {reports}"
    drafts = sorted((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1, f"expected one saved draft, got {drafts}"
    return (
        reports[0].read_text(encoding="utf-8"),
        dict(frontmatter.load(str(drafts[0])).metadata),
    )


def test_lint_flags_an_ungrounded_draft_the_draft_command_produced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft with no anchor in its sources is surfaced by the audit pass."""
    report, metadata = _draft_then_lint(tmp_path, monkeypatch, UNGROUNDED_DRAFT_BODY)
    assert metadata["grounding_score"] == pytest.approx(0.0)
    assert "1 draft(s) scanned; 1 outside" in report
    assert "grounding=0.00 < 0.30" in report


def test_lint_clears_an_in_bounds_draft_from_the_same_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is discriminating, not merely noisy.

    Asserting the scores exist *and* the report is empty is what separates
    "judged clean" from "found nothing to judge" — without the first
    assertion this test passes against the dormant guard #1040 fixed.
    """
    report, metadata = _draft_then_lint(tmp_path, monkeypatch, IN_BOUNDS_DRAFT_BODY)
    assert metadata["derivative_score"] == pytest.approx(0.5)
    assert metadata["grounding_score"] == pytest.approx(1.0)
    assert "1 draft(s) scanned; 0 outside" in report
    assert "grounding=" not in report
