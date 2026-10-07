"""Compile, draft and the Writing Desk voice carry the reflection pin (#1849).

B05 pinned ``creek.reflect`` to the operator's model package: with a manifest
configured, or in container mode, reflection is served only by an Ollama stage
on a loopback URL whose model is the pinned tag at the pinned inventory digest.
The other MCP paths that show vault text to a model resolve their provider
from the same writable vault config, so the same rule applies to each of them
here. Outside both modes nothing changes.

The cloud cases build the **real** cloud provider with its key and consent
set, and assert it would report itself available. That way the refusal can
only come from the pin's provider check, not from a missing key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
import yaml

from creek.classify.llm.consent import CLOUD_CONSENT_ENV
from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.classify.llm.providers import build_provider
from creek.config import CreekConfig, LLMConfig
from creek.models import PrivacyTier
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from tests.test_container_model_probe import _MODEL, _pin, _served, _tags
from tests.test_reflect_model_pin import _Tags

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CONFIG_SUBPATH: Final = ("00-Creek-Meta", "creek_config.yaml")
_BUILDERS: Final = [("_build_draft_llm", "draft"), ("_build_compile_llm", "compile")]
_CLOUD_KEYS: Final = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
_REMOTE_URL: Final = "http://evil.example:11434"


def _refusal(verb: str) -> str:
    """Return the unchanged refusal text for *verb*, matched in full."""
    return (
        f"^LLM provider unavailable for {verb}\\. "
        "Check Ollama or ANTHROPIC_API_KEY configuration\\.$"
    )


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from an empty cwd with no ambient config, pin, flag, key or consent."""
    for name in (
        "CREEK_CONFIG",
        MODEL_PACKAGE_FILE_ENV,
        LOOPBACK_ONLY_ENV,
        CLOUD_CONSENT_ENV,
        *_CLOUD_KEYS.values(),
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def _vault(tmp_path: Path, stage: str = "generation", **override: str) -> Path:
    """Scaffold a vault whose *stage* is the pinned-tag default plus *override*."""
    vault = tmp_path / "served"
    (vault / "01-Fragments").mkdir(parents=True)
    (vault / "00-Creek-Meta").mkdir(parents=True)
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["default"].update(model=_MODEL)
    if override:
        data["llm"][stage] = {**data["llm"]["default"], **override}
    vault.joinpath(*_CONFIG_SUBPATH).write_text(
        yaml.dump(data, sort_keys=False), encoding="utf-8"
    )
    return vault


def _cloud_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, stage: str
) -> Path:
    """Route *stage* to a keyed, consented cloud *provider* at the pinned tag.

    Every other pin condition would pass: the model is the pinned tag and
    ``ollama_url`` keeps its loopback default. The precondition assertion
    proves the real provider is available, so only the pin can refuse it.
    """
    monkeypatch.setenv(_CLOUD_KEYS[provider], "sk-test-not-a-real-key")
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    vault = _vault(tmp_path, stage, provider=provider)
    cloud = build_provider(LLMConfig(provider=provider, model=_MODEL))
    assert cloud.available, f"precondition: a keyed {provider} stage is available"
    assert cloud.model == _MODEL
    return vault


def _builder(name: str) -> Callable[[Path, PrivacyTier], Callable[[str], Any]]:
    """Return the named generation builder from ``creek_mcp.server``."""
    import creek_mcp.server as server_mod

    builder: Callable[[Path, PrivacyTier], Callable[[str], Any]] = getattr(
        server_mod, name
    )
    return builder


@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_serves_the_pinned_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, verb: str
) -> None:
    """Positive control: a served pin over loopback yields a callable."""
    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())

    llm = _builder(name)(_vault(tmp_path), PrivacyTier.INTIMATE)

    assert callable(llm), verb
    assert len(tags.urls) == 1
    assert tags.urls[0].startswith("http://localhost:11434/api/tags")


@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_refuses_when_pinned_digest_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, verb: str
) -> None:
    """The right name at the wrong digest is a substituted model: refuse."""
    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    tags = _Tags(monkeypatch, _served("e" * 64))

    with pytest.raises(RuntimeError, match=_refusal(verb)):
        _builder(name)(_vault(tmp_path), PrivacyTier.OPEN)
    assert len(tags.urls) == 1


@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_refuses_another_model_when_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, verb: str
) -> None:
    """With a pin set, a stage resolving to another served model is refused."""
    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    other = "creek-other-model:q4"
    tags = _Tags(monkeypatch, _tags({"name": other, "digest": "d" * 64}))

    with pytest.raises(RuntimeError, match=_refusal(verb)):
        _builder(name)(_vault(tmp_path, model=other), PrivacyTier.OPEN)
    assert tags.urls == []


@pytest.mark.parametrize("provider", sorted(_CLOUD_KEYS))
@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_refuses_a_keyed_cloud_stage_in_container_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    verb: str,
    provider: str,
) -> None:
    """Container mode with a served pin still refuses a cloud stage, undialled."""
    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())
    vault = _cloud_vault(tmp_path, monkeypatch, provider, "generation")

    with pytest.raises(RuntimeError, match=_refusal(verb)):
        _builder(name)(vault, PrivacyTier.OPEN)
    assert tags.urls == []


@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_refuses_a_keyed_cloud_stage_without_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, verb: str
) -> None:
    """Container mode with no pin is model-unavailable, cloud key or not."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())
    vault = _cloud_vault(tmp_path, monkeypatch, "anthropic", "generation")

    with pytest.raises(RuntimeError, match=_refusal(verb)):
        _builder(name)(vault, PrivacyTier.OPEN)
    assert tags.urls == []


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_refuses_a_remote_url_after_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    verb: str,
    *,
    flag: bool,
) -> None:
    """A post-boot edit to a remote host is refused before any dial."""
    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    if flag:
        monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())

    with pytest.raises(RuntimeError, match=_refusal(verb)) as caught:
        _builder(name)(_vault(tmp_path, ollama_url=_REMOTE_URL), PrivacyTier.OPEN)
    assert tags.urls == []
    assert "evil.example" not in str(caught.value)


@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_refuses_an_unloadable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, verb: str
) -> None:
    """A configured but invalid pin fails closed rather than unpinned."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(tmp_path / "absent.json"))
    tags = _Tags(monkeypatch, _served())

    with pytest.raises(RuntimeError, match=_refusal(verb)):
        _builder(name)(_vault(tmp_path), PrivacyTier.OPEN)
    assert tags.urls == []


@pytest.mark.parametrize(("name", "verb"), _BUILDERS)
def test_generation_builder_keeps_existing_behaviour_without_pin_or_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, verb: str
) -> None:
    """Outside container mode with no manifest an unpinned model still serves."""
    _isolate(monkeypatch, tmp_path)
    tags = _Tags(monkeypatch, _tags({"name": _MODEL}))

    llm = _builder(name)(_vault(tmp_path), PrivacyTier.OPEN)

    assert callable(llm), verb
    assert len(tags.urls) == 1


def test_generation_builder_keeps_a_keyed_cloud_stage_without_pin_or_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-hosted with the person's own key and no pin: cloud still serves.

    The counterpart of the container cases above: without this the cloud
    refusals could pass because cloud stages are refused everywhere.
    """
    _isolate(monkeypatch, tmp_path)
    tags = _Tags(monkeypatch, _served())
    vault = _cloud_vault(tmp_path, monkeypatch, "anthropic", "generation")

    assert callable(_builder("_build_draft_llm")(vault, PrivacyTier.OPEN))
    assert tags.urls == []


# ---------------------------------------------------------------------------
# The Writing Desk's voice client degrades to deterministic rendering
# ---------------------------------------------------------------------------


def test_author_llm_serves_the_pinned_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a served pin yields the voice client."""
    from creek_mcp.server import _build_author_llm

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())

    client = _build_author_llm(_vault(tmp_path), PrivacyTier.OPEN)

    assert client is not None
    assert len(tags.urls) == 1


@pytest.mark.parametrize("override", [{"model": "creek-other-model:q4"}, {}])
def test_author_llm_degrades_when_the_pin_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: dict[str, str]
) -> None:
    """Another model, or the pinned name at another digest, renders without a model."""
    from creek_mcp.server import _build_author_llm

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    inventory = (
        _tags({"name": "creek-other-model:q4", "digest": "d" * 64})
        if override
        else _served("e" * 64)
    )
    _Tags(monkeypatch, inventory)

    assert _build_author_llm(_vault(tmp_path, **override), PrivacyTier.OPEN) is None


def test_author_llm_degrades_on_a_keyed_cloud_stage_in_container_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyed cloud voice stage is not used in container mode, undialled."""
    from creek_mcp.server import _build_author_llm

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())
    vault = _cloud_vault(tmp_path, monkeypatch, "anthropic", "generation")

    assert _build_author_llm(vault, PrivacyTier.OPEN) is None
    assert tags.urls == []
