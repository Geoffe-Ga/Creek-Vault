"""Measured prompt and output tokens per reflection surface (#1893).

Every cost figure in ADR-0016 rested on one unmeasured planning assumption —
2,500 prompt tokens and 128 output tokens per reflection. These tests pin the
block that replaces it: three surfaces measured separately, through the real
``reflect_tool`` and ``write_letter`` paths, over the synthetic corpus, with no
vendor call and no spend.

The counts come from the provider's own usage report. A provider that reports
none leaves them ``None``; nothing is estimated on its behalf.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.config import AuthorConfig
from creek_mcp.bench.corpus import build_corpus
from creek_mcp.bench.outcome import Outcome, ProviderUnavailableError
from creek_mcp.bench.tokens import (
    ADR_0016_ASSUMED_OUTPUT_TOKENS,
    ADR_0016_ASSUMED_PROMPT_TOKENS,
    PROMPT_OVERHEAD_TOKENS,
    Surface,
    TokenMeasurement,
    measure_tokens_per_reflection,
    unmeasured_tokens,
)
from creek_mcp.bench.usage import TokenBasis
from creek_mcp.tools import reflect as reflect_module
from creek_mcp.tools.reflect import _REFLECTION_MAX_OUTPUT_TOKENS
from tests.bench.conftest import FakeModel

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _measurement(root: Path, model: FakeModel, *, words: int = 24) -> TokenMeasurement:
    """Build a measurement request over a fresh synthetic corpus under *root*."""
    corpus = build_corpus(root, seed=5, entries=3, words_per_entry=12)
    return TokenMeasurement(
        vault_path=corpus.root,
        factory=model.factory,
        clock=lambda: 0.0,
        seed=5,
        index=corpus.entry_count,
        words=words,
    )


def test_every_surface_is_measured_separately(tmp_path: Path) -> None:
    """The block names all three surfaces, each with its own counts."""
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    assert set(block.per_surface) == set(Surface)
    for surface in Surface:
        measured = block.per_surface[surface]
        assert measured.surface is surface
        assert measured.outcome is Outcome.OK
        assert measured.prompt_tokens is not None
        assert measured.output_tokens is not None
    assert block.basis is TokenBasis.BYTE_UPPER_BOUND


def test_grounding_changes_the_prompt_materially(tmp_path: Path) -> None:
    """Filled grounding slots cost more prompt than an empty one, by the
    length of the titles the production grounder would have supplied."""
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    bare = block.per_surface[Surface.REFLECTION_UNGROUNDED].prompt_tokens
    grounded = block.per_surface[Surface.REFLECTION_GROUNDED].prompt_tokens
    assert bare is not None
    assert grounded is not None
    assert grounded > bare


def test_the_letter_route_is_measured_on_its_own_budget(tmp_path: Path) -> None:
    """The letter ships a far larger output ceiling than a reflection does."""
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    letter = block.per_surface[Surface.LETTER]
    reflection = block.per_surface[Surface.REFLECTION_GROUNDED]
    assert reflection.max_output_tokens == _REFLECTION_MAX_OUTPUT_TOKENS
    assert letter.max_output_tokens is not None
    assert letter.max_output_tokens > _REFLECTION_MAX_OUTPUT_TOKENS


def test_measured_output_stays_within_the_shipped_cap(tmp_path: Path) -> None:
    """A well-behaved model never exceeds the budget reflect asked for."""
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    for measured in block.per_surface.values():
        assert measured.output_tokens is not None
        assert measured.max_output_tokens is not None
        assert measured.output_tokens <= measured.max_output_tokens
        assert not measured.output_cap_exceeded


def test_a_model_that_overruns_its_budget_is_reported_not_clipped(
    tmp_path: Path,
) -> None:
    """An over-long completion is recorded as exceeding the cap."""
    model = FakeModel(echo_canary=True)
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    reflection = block.per_surface[Surface.REFLECTION_GROUNDED]
    assert reflection.output_tokens is not None
    assert reflection.max_output_tokens is not None
    assert reflection.output_tokens > reflection.max_output_tokens
    assert reflection.output_cap_exceeded


def test_measured_prompt_overhead_is_within_the_asserted_bound(
    tmp_path: Path,
) -> None:
    """The 1,536-token allowance is a ceiling, and the measurement proves it."""
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    assert block.prompt_overhead_bound == PROMPT_OVERHEAD_TOKENS
    assert block.prompt_overhead_tokens is not None
    assert block.prompt_overhead_tokens <= PROMPT_OVERHEAD_TOKENS
    assert block.prompt_overhead_within_bound is True


def test_an_inflated_template_makes_the_overhead_bound_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity: grow reflect's template and the bound check goes red.

    Proves the check is measuring the real prompt rather than restating a
    constant — if the allowance ever becomes the binding constraint rather
    than a ceiling, the report says so instead of the sweep truncating.
    """
    monkeypatch.setattr(
        reflect_module, "CARE_POLICY", "x" * (PROMPT_OVERHEAD_TOKENS + 1)
    )
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    assert block.prompt_overhead_tokens is not None
    assert block.prompt_overhead_tokens > PROMPT_OVERHEAD_TOKENS
    assert block.prompt_overhead_within_bound is False


def test_the_overhead_probe_fills_every_grounding_slot(tmp_path: Path) -> None:
    """Slots filled, because that is the case the allowance must cover."""
    model = FakeModel()
    measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    grounded = [p for p in model.prompts if "- bench note" in p]
    assert grounded, "no prompt carried grounding lines"
    top_k = AuthorConfig().retrieval_top_k
    assert all(p.count("- bench note") == top_k for p in grounded)


def test_a_backend_reporting_no_usage_leaves_the_counts_none(
    tmp_path: Path,
) -> None:
    """Nothing is guessed: unreported means ``None``, and the block still holds."""
    model = FakeModel(reports_usage=False)
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    assert block.basis is TokenBasis.UNREPORTED
    assert block.prompt_overhead_tokens is None
    assert block.prompt_overhead_within_bound is None
    for measured in block.per_surface.values():
        assert measured.prompt_tokens is None
        assert measured.output_tokens is None
        assert measured.outcome is Outcome.OK


def test_a_failed_surface_records_its_outcome_and_no_counts(tmp_path: Path) -> None:
    """A surface the model never answered is classified, not dropped."""
    model = FakeModel(call_exc=ProviderUnavailableError("down"))
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    for measured in block.per_surface.values():
        assert measured.outcome is Outcome.PROVIDER_UNAVAILABLE
        assert measured.prompt_tokens is None
        assert measured.output_tokens is None


def test_the_block_records_the_assumption_it_replaces(tmp_path: Path) -> None:
    """ADR-0016's planning numbers travel beside the measurement."""
    model = FakeModel()
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    assert block.assumed_prompt_tokens == ADR_0016_ASSUMED_PROMPT_TOKENS == 2500
    assert block.assumed_output_tokens == ADR_0016_ASSUMED_OUTPUT_TOKENS == 128


def test_an_unmeasured_block_says_so() -> None:
    """A run with the pass switched off is explicit rather than empty-looking."""
    block = unmeasured_tokens()
    assert block.basis is TokenBasis.NOT_MEASURED
    assert block.per_surface == {}
    assert block.prompt_overhead_tokens is None
    assert block.prompt_overhead_within_bound is None


def test_an_oom_during_the_pass_is_classified_not_raised(tmp_path: Path) -> None:
    """A ``MemoryError`` escapes reflect and the letter; the pass survives it.

    ``reflect_tool`` converts only ``RuntimeError`` into a refusal and
    ``write_letter`` only ``RuntimeError``/``OSError``/``ValueError``, so an
    out-of-memory runtime propagates out of both. The pass must record it as
    the surface's outcome rather than losing a whole run's report.
    """
    model = FakeModel(call_exc=MemoryError())
    block = measure_tokens_per_reflection(_measurement(tmp_path / "c", model))
    assert set(block.per_surface) == set(Surface)
    for measured in block.per_surface.values():
        assert measured.outcome is Outcome.OOM
        assert measured.prompt_tokens is None
    assert block.basis is TokenBasis.UNREPORTED
    assert block.prompt_overhead_within_bound is None
