"""Cost-control tests for the Creek Writing Desk (FEAT-041, #474).

These pin the three cost/configurability levers added in #474 without touching
agent logic or outputs:

* **Prompt caching** — the voice call's static ``creek-skills`` prefix is sent
  as a cached ``system`` block, and the SDK's token usage (including cache
  reads) is surfaced onto :class:`~creek.author.models.AuthoredDraft`.
* **Usage plumbing** — provider → client → voice → conductor → draft, with
  :meth:`VoiceAgent.render` still returning a plain ``str``.
* **Config-driven model tiers** — the voice model resolves from
  :class:`~creek.config.AuthorConfig` / :class:`~creek.config.LLMConfig`, never
  a hard-coded id.

No live network: the Anthropic SDK is a ``MagicMock`` throughout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from creek.author.client import AuthorLLMClient, resolve_voice_model
from creek.author.conductor import Conductor, build_default_conductor
from creek.author.models import EvidenceBundle, EvidenceClaim
from creek.author.reflection import ReflectionNode
from creek.author.voice import VoiceAgent
from creek.classify.llm.providers import AnthropicCompletion, AnthropicProvider
from creek.config import AuthorConfig, LLMConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _seed_fragment(vault: Path, frag_id: str, title: str) -> None:
    """Write a minimal owner fragment so the real specialists have a corpus."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )


def _provider_with_usage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    usage: dict[str, int],
    stop_reason: str = "end_turn",
) -> AnthropicProvider:
    """Build an :class:`AnthropicProvider` over a mock SDK returning *usage*."""
    monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test")
    monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")
    provider = AnthropicProvider(LLMConfig(provider="anthropic", model="claude-x"))
    sdk = MagicMock()
    response = MagicMock()
    response.stop_reason = stop_reason
    block = MagicMock()
    block.text = "voiced body"
    response.content = [block]
    response.usage = MagicMock(**usage)
    sdk.messages.create.return_value = response
    # Inject the mock SDK so no live network call is made; the lazy ``client``
    # property returns this instead of constructing a real ``anthropic.Anthropic``.
    monkeypatch.setattr(provider, "_client", sdk)
    return provider


def test_provider_extracts_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """``call_with_metadata`` surfaces the SDK usage (input/output + cache)."""
    provider = _provider_with_usage(
        monkeypatch,
        usage={
            "input_tokens": 12,
            "output_tokens": 7,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 0,
        },
    )

    completion = provider.call_with_metadata("ask")

    assert completion.usage is not None
    assert completion.usage["input_tokens"] == 12
    assert completion.usage["output_tokens"] == 7
    assert completion.usage["cache_read_input_tokens"] == 5


def test_provider_sends_cached_system_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``system=`` arg reaches the SDK as a cache-controlled content block."""
    provider = _provider_with_usage(monkeypatch, usage={"input_tokens": 1})

    provider.call_with_metadata("dynamic ask", system="STATIC PREFIX")

    kwargs = provider.client.messages.create.call_args.kwargs
    system_blocks = kwargs["system"]
    assert system_blocks[0]["text"] == "STATIC PREFIX"
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # The dynamic prompt stays the user content; the static prefix never
    # contaminates it (behaviour unchanged, only cheaper).
    assert kwargs["messages"][0]["content"] == "dynamic ask"


def test_provider_omits_system_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``system`` the call is byte-for-byte the legacy single-user shape."""
    provider = _provider_with_usage(monkeypatch, usage={"input_tokens": 1})

    provider.call_with_metadata("just the prompt")

    kwargs = provider.client.messages.create.call_args.kwargs
    assert "system" not in kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "just the prompt"}]


def test_complete_uses_default_unbounded_max_tokens() -> None:
    """``complete`` with no ``max_tokens`` passes ``None`` through (#455).

    The default (unbounded) path was previously only exercised with an explicit
    ceiling; this pins that the default forwards ``max_tokens=None``.
    """
    provider = MagicMock()
    provider.complete.return_value = AnthropicCompletion(
        text="completion", usage={"input_tokens": 1}
    )

    client = AuthorLLMClient(provider)
    text = client.complete("ask")

    assert text == "completion"
    provider.complete.assert_called_once_with("ask", max_tokens=None)


def test_complete_with_usage_returns_text_and_usage() -> None:
    """``complete_with_usage`` returns the full completion; ``complete`` stays str."""
    provider = MagicMock()
    provider.complete.return_value = AnthropicCompletion(
        text="hi", usage={"input_tokens": 3, "cache_read_input_tokens": 2}
    )

    client = AuthorLLMClient(provider)
    completion = client.complete_with_usage("ask", system="static")

    assert isinstance(completion, AnthropicCompletion)
    assert completion.text == "hi"
    assert completion.usage == {"input_tokens": 3, "cache_read_input_tokens": 2}
    provider.complete.assert_called_once_with(
        "ask",
        system="static",
        max_tokens=None,
    )


def test_voice_render_splits_static_and_dynamic(tmp_path: Path) -> None:
    """The voice-skill prefix is the cached ``system``; evidence+ask are dynamic."""
    core = tmp_path / "creek-skills" / "voice-core" / "SKILL.md"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text("VOICE-CORE-PREFIX", encoding="utf-8")
    client = MagicMock()
    client.complete_with_usage.return_value = AnthropicCompletion(
        text="voiced", usage={"cache_read_input_tokens": 0}
    )
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a grounded claim", source_fragments=["f1"])]
    )

    agent = VoiceAgent(llm_client=client)
    body = agent.render("q", evidence, tmp_path, medium="research")

    assert body == "voiced"
    call = client.complete_with_usage.call_args
    dynamic = call.args[0]
    static = call.kwargs["system"]
    assert "VOICE-CORE-PREFIX" in static
    assert "VOICE-CORE-PREFIX" not in dynamic
    assert "a grounded claim" in dynamic
    assert agent.last_usage == {"cache_read_input_tokens": 0}


def test_voice_render_resets_last_usage_on_deterministic_path(tmp_path: Path) -> None:
    """A deterministic render after an LLM render must not leak stale usage (#474)."""
    core = tmp_path / "creek-skills" / "voice-core" / "SKILL.md"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text("PREFIX", encoding="utf-8")
    client = MagicMock()
    client.complete_with_usage.return_value = AnthropicCompletion(
        text="voiced", usage={"cache_read_input_tokens": 7}
    )
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["f1"])]
    )
    agent = VoiceAgent(llm_client=client)

    agent.render("q", evidence, tmp_path, medium="research")
    assert agent.last_usage == {"cache_read_input_tokens": 7}

    # Reuse the same agent on the deterministic path (no vault) — usage clears.
    agent.render("q", evidence, vault=None, medium="research")
    assert agent.last_usage is None


def test_voice_render_returns_str_and_last_usage_none_offline() -> None:
    """The deterministic path returns a ``str`` and leaves ``last_usage`` None."""
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["f1"])]
    )

    agent = VoiceAgent()
    body = agent.render("q", evidence)

    assert isinstance(body, str)
    assert body.strip()
    assert agent.last_usage is None


def test_conductor_surfaces_cache_hit_on_repeat(tmp_path: Path) -> None:
    """Repeating a run over a static prefix yields a cache read on draft.usage."""
    _seed_fragment(tmp_path, "frag-a", "F6 Pluralism")
    core = tmp_path / "creek-skills" / "voice-core" / "SKILL.md"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text("STATIC VOICE PREFIX", encoding="utf-8")

    client = MagicMock()
    client.complete_with_usage.side_effect = [
        AnthropicCompletion(
            text="first voiced body",
            usage={"input_tokens": 50, "cache_read_input_tokens": 0},
        ),
        AnthropicCompletion(
            text="second voiced body",
            usage={"input_tokens": 50, "cache_read_input_tokens": 48},
        ),
    ]

    def _make_conductor() -> Conductor:
        return Conductor(
            specialists=build_default_conductor(max_rounds=1).specialists,
            voice=VoiceAgent(llm_client=client),
            reflection=ReflectionNode(),
            max_rounds=1,
        )

    first = _make_conductor().run(
        medium="research", query="What is F6?", vault=tmp_path
    )
    second = _make_conductor().run(
        medium="research", query="What is F6?", vault=tmp_path
    )

    assert first.usage is not None
    assert first.usage["cache_read_input_tokens"] == 0
    assert second.usage is not None
    assert second.usage["cache_read_input_tokens"] > 0


def test_resolve_voice_model_prefers_override() -> None:
    """The voice model resolves from the author override, falling back to llm."""
    llm = LLMConfig(provider="anthropic", model="base-model")

    assert resolve_voice_model(AuthorConfig(voice_model="tier-model"), llm) == (
        "tier-model"
    )
    assert resolve_voice_model(AuthorConfig(), llm) == "base-model"


def test_resolve_voice_model_unset_means_provider_default() -> None:
    """With neither tier set, resolution yields ``None`` = provider default (#621).

    ``None`` flows into :meth:`AuthorLLMClient.from_config`, which leaves
    ``config.model`` untouched so the provider applies its own default.
    """
    assert resolve_voice_model(AuthorConfig(), LLMConfig()) is None


def test_author_client_from_config_threads_model_override() -> None:
    """``from_config(model=...)`` overrides the model id without hard-coding one."""
    from unittest.mock import patch

    with patch("creek.author.client.build_provider") as mock_build:
        AuthorLLMClient.from_config(
            LLMConfig(provider="anthropic", model="base"), model="tier-x"
        )

    passed_config = mock_build.call_args.args[0]
    assert passed_config.model == "tier-x"
