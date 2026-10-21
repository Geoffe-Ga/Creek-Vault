"""The usage seam: a provider's own token report, and the byte-level fallback.

Token *counts* are evidence; token *content* is not. These tests pin that the
seam carries counts only, that a provider which reports nothing is represented
as ``None`` rather than guessed at, and that the fake's self-reported counts
are the pessimistic byte-level bound they claim to be.
"""

from __future__ import annotations

from creek.models import PrivacyTier
from creek_mcp.bench.fake import FakeLLM, fake_factory
from creek_mcp.bench.usage import (
    TokenBasis,
    TokenUsage,
    UsageReporting,
    byte_upper_bound_usage,
)

_PROMPT = "ENTRY:\nriver stone window"


def test_byte_upper_bound_counts_one_token_per_utf8_byte() -> None:
    """The bound is bytes, because no tokenizer spends less than a byte."""
    usage = byte_upper_bound_usage("abc", "defg")
    assert usage.prompt_tokens == 3
    assert usage.output_tokens == 4
    assert usage.basis is TokenBasis.BYTE_UPPER_BOUND


def test_byte_upper_bound_counts_every_byte_of_a_multibyte_character() -> None:
    """A two-byte character costs two, so the bound never under-counts."""
    assert byte_upper_bound_usage("é", "").prompt_tokens == 2


def test_a_plain_completion_callable_is_not_usage_reporting() -> None:
    """A backend with no usage accessor is detected, not assumed."""

    def _plain(prompt: str) -> str:
        return prompt

    assert not isinstance(_plain, UsageReporting)


def test_the_fake_model_reports_its_own_byte_bound_usage() -> None:
    """``--mode fake`` is a provider, so it reports usage like one."""
    llm = fake_factory(PrivacyTier.OPEN, max_tokens=128)
    assert isinstance(llm, FakeLLM)
    assert isinstance(llm, UsageReporting)
    assert llm.token_usage() is None
    response = llm(_PROMPT)
    usage = llm.token_usage()
    assert usage == TokenUsage(
        prompt_tokens=len(_PROMPT.encode()),
        output_tokens=len(response.encode()),
        basis=TokenBasis.BYTE_UPPER_BOUND,
    )


def test_usage_is_counts_only() -> None:
    """The record has no field a prompt or a completion could hide in."""
    assert set(TokenUsage.__dataclass_fields__) == {
        "prompt_tokens",
        "output_tokens",
        "basis",
    }
