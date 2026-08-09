"""The FEAT-032 grounding guard is wired into both production draft paths (#1040).

The guard, its ``default_embedding_fn`` factory, and the ``draft-grounding``
lint check that consumes its output all shipped complete — and completely
disconnected. ``DraftGenerator`` skips the guard unless *both* ``embedding_fn``
and ``grounding_thresholds`` are supplied, and neither production construction
site supplied them, so every real draft was saved without
``derivative_score`` / ``grounding_score`` and the lint check's
"scores absent → clean" branch reported every vault spotless.

Every test here asserts on the **frontmatter the run produced**, never on the
exit code. A dormant guard exits 0 exactly like a live one; only the stamped
scores tell them apart. The lint check's own scoring logic is covered by
``tests/test_lint_draft_grounding.py`` against hand-written frontmatter — what
was missing, and what these tests supply, is proof that a real draft ever
carries the frontmatter that check reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.link.embeddings import EmbeddingLinker, EmbeddingModelUnavailableError
from tests.grounding_stubs import (
    BIOGRAPHICAL_DRAFT_BODY,
    GROUNDED_SOURCE_BODY,
    IN_BOUNDS_DRAFT_BODY,
    UNGROUNDED_DRAFT_BODY,
    BagOfWordsEncoder,
)
from tests.helpers import write_raw_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_SOURCE_ID = "frag-ground-001"


def _seed() -> Any:
    """Build the fixed idea seed both draft surfaces mine.

    Returns ``Any`` because :class:`~creek.generate.mining.IdeaSeed` is
    imported inside the function — importing the mining module at test-module
    scope pulls in the draft stack before ``monkeypatch`` can reach it.
    """
    from creek.generate.mining import IdeaSeed, MiningStrategy

    return IdeaSeed(
        strategy=MiningStrategy.THREAD_TERMINUS,
        title="Grounding wiring probe",
        source_fragments=(_SOURCE_ID,),
        threads=(),
        eddies=(),
        frequency_affinity=(),
        brief_description="A draft whose grounding must be measured.",
        score=0.8,
    )


def _build_vault(tmp_path: Path) -> Path:
    """Scaffold a vault holding the single source fragment drafts ground against."""
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
    return vault


def _stub_miner(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    """Force *target*'s ``IdeaMiner`` to surface exactly one seed."""

    def _mine_all(
        _self: object,
        _vault: object,
        *,
        current_phase: object = None,
    ) -> list[object]:
        """Return the one fixed seed, ignoring the phase the caller asked for."""
        del current_phase
        return [_seed()]

    monkeypatch.setattr(target, _mine_all)


def _stub_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the model weights, keeping the production embedding path."""
    monkeypatch.setattr(
        EmbeddingLinker,
        "load_model",
        lambda _self: BagOfWordsEncoder(),
    )


def _saved_draft_metadata(vault: Path) -> dict[str, Any]:
    """Return the frontmatter of the single draft saved under ``07-Voice/Drafts``."""
    drafts = sorted((vault / "07-Voice" / "Drafts").glob("*.md"))
    assert len(drafts) == 1, f"expected exactly one saved draft, got {drafts}"
    return dict(frontmatter.load(str(drafts[0])).metadata)


def _run_cli_draft(
    vault: Path,
    body: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cohesion: bool = False,
) -> Any:
    """Drive ``creek draft`` with a fixed LLM emitter, returning click's ``Result``."""
    import creek.cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm",
        lambda *_a, **_k: lambda _prompt: body,
    )
    _stub_miner(monkeypatch, "creek.generate.mining.IdeaMiner.mine_all")
    argv = ["draft", "--vault", str(vault)]
    if cohesion:
        argv.append("--cohesion")
    return runner.invoke(app, argv)


def test_cli_draft_stamps_grounding_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek draft`` runs the guard and writes both scalars to frontmatter."""
    vault = _build_vault(tmp_path)
    _stub_embedding_model(monkeypatch)
    result = _run_cli_draft(vault, UNGROUNDED_DRAFT_BODY, monkeypatch)
    assert result.exit_code == 0, result.output
    metadata = _saved_draft_metadata(vault)
    assert metadata["derivative_score"] == pytest.approx(0.0)
    assert metadata["grounding_score"] == pytest.approx(0.0)
    assert metadata["paragraph_grounding"][0]["is_grounded"] is False


def test_cli_draft_scores_an_in_bounds_draft_inside_both_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-overlapping draft scores 0.5 derivative / 1.0 grounded.

    Pins the guard to real numbers rather than mere key presence: a stub that
    stamped zeros for everything would satisfy the sibling test but fail here.
    """
    vault = _build_vault(tmp_path)
    _stub_embedding_model(monkeypatch)
    result = _run_cli_draft(vault, IN_BOUNDS_DRAFT_BODY, monkeypatch)
    assert result.exit_code == 0, result.output
    metadata = _saved_draft_metadata(vault)
    assert metadata["derivative_score"] == pytest.approx(0.5)
    assert metadata["grounding_score"] == pytest.approx(1.0)


def test_cli_draft_survives_an_unavailable_embedding_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing local model skips the guard instead of crashing the draft."""

    def _explode(_self: object) -> object:
        """Fail exactly as ``EmbeddingLinker.load_model`` does on an offline host."""
        raise EmbeddingModelUnavailableError("model weights are missing")

    monkeypatch.setattr(EmbeddingLinker, "load_model", _explode)
    vault = _build_vault(tmp_path)
    result = _run_cli_draft(vault, UNGROUNDED_DRAFT_BODY, monkeypatch)
    assert result.exit_code == 0, result.output
    # The warning is the load-bearing assertion. Without it this test would
    # also pass against a guard that was never wired at all — a dormant guard
    # exits 0 and stamps nothing too, which is the whole defect of #1040.
    assert "grounding guard skipped" in result.output
    assert "model weights are missing" in result.output
    metadata = _saved_draft_metadata(vault)
    assert "derivative_score" not in metadata
    assert "grounding_score" not in metadata


def test_cohesion_pass_survives_an_unavailable_embedding_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cohesion re-check degrades too — it embeds *before* the guard.

    ``_apply_cohesion`` runs ahead of ``_build_guard_report`` in
    ``generate_draft``, so under ``--cohesion`` the biographical re-scan is
    the first thing to touch the model and therefore the first thing an
    offline host can crash on.
    """

    def _explode(_self: object) -> object:
        """Fail exactly as ``EmbeddingLinker.load_model`` does on an offline host."""
        raise EmbeddingModelUnavailableError("model weights are missing")

    monkeypatch.setattr(EmbeddingLinker, "load_model", _explode)
    vault = _build_vault(tmp_path)
    result = _run_cli_draft(
        vault,
        BIOGRAPHICAL_DRAFT_BODY,
        monkeypatch,
        cohesion=True,
    )
    assert result.exit_code == 0, result.output
    assert "grounding guard skipped" in result.output
    metadata = _saved_draft_metadata(vault)
    assert "grounding_score" not in metadata


def test_mcp_draft_stamps_grounding_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP ``creek.draft`` surface is not left behind by the CLI wiring."""
    from creek_mcp.tools.draft import draft_tool

    vault = _build_vault(tmp_path)
    _stub_embedding_model(monkeypatch)
    _stub_miner(monkeypatch, "creek_mcp.tools.draft.IdeaMiner.mine_all")
    result = draft_tool(
        vault_path=vault,
        llm_factory=lambda _tier: lambda _prompt: UNGROUNDED_DRAFT_BODY,
        skills_root=vault / "creek-skills",
        phase="unclassified",
    )
    assert result["status"] == "ok", result
    metadata = _saved_draft_metadata(vault)
    assert metadata["derivative_score"] == pytest.approx(0.0)
    assert metadata["grounding_score"] == pytest.approx(0.0)
