"""``creek mission`` — a personal mission and values document for Reconcile.

The command picks the owner's own fragments under ``--include-tier``, routes
the one model turn by the most sensitive of them, saves the result under
``07-Voice/Mission/`` and prints the title and body ready to paste.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest
from typer.testing import CliRunner

from creek import cli as cli_module
from creek.classify.privacy_filter import PRIVACY_AUDIT_RELPATH
from creek.cli import app
from creek.generate.mission import (
    DEFAULT_MISSION_LIMIT,
    MAX_MISSION_LIMIT,
    MISSION_MAX_OUTPUT_TOKENS,
    MISSION_SUBDIR,
)
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()

_ANSWER = json.dumps(
    {
        "title": "What I [really] stand for",
        "mission": "I keep my word and make room for the quiet ones.",
        "values": [
            {"name": "Honesty", "statement": "I say what I mean."},
            {"name": "Care", "statement": "I check on people first."},
            {"name": "Patience", "statement": "I let repair take time."},
        ],
    }
)


def _write_fragment(vault: Path, frag_id: str, body: str, tier: str) -> None:
    """Write one self-authored fragment under ``01-Fragments/Notes``."""
    when = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    metadata = {
        "type": "fragment",
        "id": frag_id,
        "title": f"Note {frag_id}",
        "created": when,
        "ingested": when,
        "source": {"platform": "journal", "author": "self"},
        "frequency": {"primary": "F1", "secondary": []},
        "privacy_tier": tier,
    }
    target = vault / "01-Fragments" / "Notes" / f"{frag_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        frontmatter.dumps(frontmatter.Post(content=body, **metadata)),
        encoding="utf-8",
    )


class _FactoryStub:
    """Stands in for ``_build_draft_llm_factory``, recording what it is given."""

    def __init__(self, answer: str = _ANSWER, error: Exception | None = None) -> None:
        """Answer every prompt with *answer*, or raise *error* from the call."""
        self.max_tokens: list[int | None] = []
        self.tiers: list[PrivacyTier] = []
        self.prompts: list[str] = []
        self._answer = answer
        self._error = error

    def __call__(
        self, _config: Any, max_tokens: int | None = None
    ) -> Callable[[PrivacyTier], Callable[[str], str]]:
        """Record the budget and return the tier-keyed factory."""
        self.max_tokens.append(max_tokens)
        return self._for_tier

    def _for_tier(self, tier: PrivacyTier) -> Callable[[str], str]:
        """Record the tier and return the completion callable."""
        self.tiers.append(tier)
        return self._complete

    def _complete(self, prompt: str) -> str:
        """Record the prompt and answer, or raise."""
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        return self._answer


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a vault with one open and one personal value note."""
    root = tmp_path / "vault"
    _write_fragment(root, "frag-open", "I believe in keeping my word.", "open")
    _write_fragment(root, "frag-personal", "I value repair after a fight.", "personal")
    return root


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _FactoryStub:
    """Replace the CLI's draft LLM factory with a recording stub."""
    factory = _FactoryStub()
    monkeypatch.setattr(cli_module, "_build_draft_llm_factory", factory)
    return factory


def test_mission_prints_the_title_and_body_ready_to_paste(
    vault: Path, stub: _FactoryStub
) -> None:
    """The document is printed as written, brackets and all, and saved."""
    result = runner.invoke(app, ["mission", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert "Title: What I [really] stand for\n" in result.output
    assert "## Mission\nI keep my word and make room for the quiet ones.\n" in (
        result.output
    )
    assert "### Patience\nI let repair take time.\n" in result.output
    (saved,) = sorted((vault / MISSION_SUBDIR).glob("*.md"))
    assert frontmatter.load(saved)["source_fragments"] == ["frag-open"]
    assert stub.max_tokens == [MISSION_MAX_OUTPUT_TOKENS]


def test_mission_routes_by_the_sources_own_tier(
    vault: Path, stub: _FactoryStub
) -> None:
    """At the default tier only open sources are carried, so the call is open."""
    runner.invoke(app, ["mission", "--vault", str(vault)])

    assert stub.tiers == [PrivacyTier.OPEN]
    assert "repair after a fight" not in stub.prompts[0]


def test_mission_include_tier_widens_the_sources_and_is_audited(
    vault: Path, stub: _FactoryStub
) -> None:
    """``--include-tier personal`` carries personal notes, routes personal, and logs."""
    result = runner.invoke(
        app, ["mission", "--vault", str(vault), "--include-tier", "personal"]
    )

    assert result.exit_code == 0, result.output
    assert stub.tiers == [PrivacyTier.PERSONAL]
    assert "repair after a fight" in stub.prompts[0]
    audit = (vault / PRIVACY_AUDIT_RELPATH).read_text(encoding="utf-8")
    assert '"mission"' in audit
    assert "frag-personal" in audit


def test_mission_passes_max_tokens_through(vault: Path, stub: _FactoryStub) -> None:
    """``--max-tokens`` overrides the mission's own output budget."""
    runner.invoke(app, ["mission", "--vault", str(vault), "--max-tokens", "999"])

    assert stub.max_tokens == [999]


def test_mission_adds_the_voice_core_to_the_prompt(
    vault: Path, stub: _FactoryStub, tmp_path: Path
) -> None:
    """``--voice-core`` reaches the prompt inside its own delimiter."""
    core = tmp_path / "core.txt"
    core.write_text("Short lines. No slogans.", encoding="utf-8")

    runner.invoke(app, ["mission", "--vault", str(vault), "--voice-core", str(core)])

    assert "<voice_core>\nShort lines. No slogans.\n</voice_core>" in stub.prompts[0]


def test_mission_with_nothing_to_draw_on_reaches_no_model(
    tmp_path: Path, stub: _FactoryStub
) -> None:
    """An empty vault says so and exits 0 without building a provider."""
    empty = tmp_path / "empty"
    empty.mkdir()

    result = runner.invoke(app, ["mission", "--vault", str(empty)])

    assert result.exit_code == 0
    assert "nothing to draft a mission from" in result.output
    assert stub.tiers == []


def test_mission_refuses_a_limit_above_the_cap(vault: Path, stub: _FactoryStub) -> None:
    """``--limit`` above the cap exits 2 before the corpus is read."""
    result = runner.invoke(
        app, ["mission", "--vault", str(vault), "--limit", str(MAX_MISSION_LIMIT + 1)]
    )

    assert result.exit_code == 2
    assert f"--limit is at most {MAX_MISSION_LIMIT}" in result.output
    assert stub.tiers == []


def test_mission_exits_1_and_saves_nothing_on_an_unreadable_answer(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer that is not a mission is reported and leaves no note."""
    monkeypatch.setattr(
        cli_module, "_build_draft_llm_factory", _FactoryStub(answer="no thanks")
    )

    result = runner.invoke(app, ["mission", "--vault", str(vault)])

    assert result.exit_code == 1
    assert "could not be read as a mission" in result.output
    assert not (vault / MISSION_SUBDIR).exists()


def test_mission_exits_1_when_the_provider_call_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed call names only the exception type, never its text."""
    monkeypatch.setattr(
        cli_module,
        "_build_draft_llm_factory",
        _FactoryStub(error=RuntimeError("vendor-secret-text")),
    )

    result = runner.invoke(app, ["mission", "--vault", str(vault)])

    assert result.exit_code == 1
    assert "RuntimeError" in result.output
    assert "vendor-secret-text" not in result.output
    assert not (vault / MISSION_SUBDIR).exists()


def test_mission_limit_help_states_the_real_numbers() -> None:
    """The help text's two numbers are the module's, so they cannot drift."""
    help_text = cli_module._MISSION_LIMIT_HELP

    assert f"default {DEFAULT_MISSION_LIMIT}" in help_text
    assert f"at most {MAX_MISSION_LIMIT}" in help_text
