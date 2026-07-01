"""The care policy is woven into every voice-generating prompt (#753).

AC #1 — "a shared guardrail is applied to every voice-generating prompt" — is
pinned here by asserting :data:`CARE_POLICY` is present in the prompt each voice
tool builds: ``creek.reflect``, ``creek.compile``, and ``creek.author``
(``creek.draft`` is covered in ``test_drafts.py`` where its fixtures live).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from creek.author.models import EvidenceBundle, EvidenceClaim
from creek.author.voice import VoiceAgent
from creek.care.guardrail import CARE_POLICY
from creek.classify.llm.completion import AnthropicCompletion
from creek.compile.engine import _build_prompt as compile_build_prompt
from creek_mcp.tools.reflect import _build_prompt as reflect_build_prompt

if TYPE_CHECKING:
    from pathlib import Path


def test_reflect_prompt_carries_the_care_policy() -> None:
    """``creek.reflect``'s margin-note prompt leads with the care policy."""
    assert CARE_POLICY in reflect_build_prompt("an entry", [])


def test_compile_prompt_carries_the_care_policy() -> None:
    """``creek.compile``'s prompt leads with the care policy."""
    prompt = compile_build_prompt([], target_kind="eddy", target_title="Title")
    assert CARE_POLICY in prompt


def test_author_voice_system_prompt_carries_the_care_policy(tmp_path: Path) -> None:
    """``creek.author``'s cached voice system prompt carries the care policy."""
    client = MagicMock()
    client.complete_with_usage.return_value = AnthropicCompletion(
        text="voiced", usage={"cache_read_input_tokens": 0}
    )
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a grounded claim", source_fragments=["f1"])]
    )
    VoiceAgent(llm_client=client).render("q", evidence, tmp_path, medium="research")
    static = client.complete_with_usage.call_args.kwargs["system"]
    assert CARE_POLICY in static
