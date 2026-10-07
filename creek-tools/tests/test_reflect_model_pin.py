"""The reflection factory carries the model pin on every request (#1849).

``creek_mcp.httpapi.reflect`` builds the factory per request from the vault's
own config, which lives inside the writable vault. So the boot-time loopback
check alone would not hold: an edit after boot could route reflection to a
remote host or another model. With a pin configured — or in container mode —
reflection is served only by the pinned model at its pinned digest, over
loopback (AC8, AC9, AC11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml

from creek.classify.llm.local_boundary import LOOPBACK_ONLY_ENV
from creek.config import CreekConfig
from creek.models import PrivacyTier
from creek_mcp.model_package import MODEL_PACKAGE_FILE_ENV
from tests.test_container_model_probe import _MODEL, _pin, _served, _tags

if TYPE_CHECKING:
    from pathlib import Path

_CONFIG_SUBPATH = ("00-Creek-Meta", "creek_config.yaml")


def _vault(tmp_path: Path, **generation: str) -> Path:
    """Scaffold a vault whose generation stage is set to *generation*."""
    vault = tmp_path / "served"
    (vault / "01-Fragments").mkdir(parents=True)
    (vault / "00-Creek-Meta").mkdir(parents=True)
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["default"].update(model=_MODEL)
    if generation:
        data["llm"]["generation"] = {**data["llm"]["default"], **generation}
    vault.joinpath(*_CONFIG_SUBPATH).write_text(
        yaml.dump(data, sort_keys=False), encoding="utf-8"
    )
    return vault


class _Tags:
    """Stand in for ``httpx.Client`` and count every inventory request."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, response: httpx.Response):
        self.urls: list[str] = []
        self._response = response
        monkeypatch.setattr(httpx, "Client", self._client)

    def _client(self, **_kwargs: object) -> _Tags:
        return self

    def __enter__(self) -> _Tags:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        return self._response


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from an empty cwd with no ambient config, pin or boundary flag."""
    monkeypatch.delenv("CREEK_CONFIG", raising=False)
    monkeypatch.delenv(MODEL_PACKAGE_FILE_ENV, raising=False)
    monkeypatch.delenv(LOOPBACK_ONLY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


_REFUSAL = "LLM provider unavailable for reflection"


def test_reflect_factory_serves_the_pinned_digest_with_one_inventory_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: a served pin over loopback yields a callable."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())

    llm = _build_reflect_llm_factory(_vault(tmp_path))(
        PrivacyTier.INTIMATE, max_tokens=16
    )

    assert callable(llm)
    assert len(tags.urls) == 1


def test_reflect_factory_refuses_when_pinned_digest_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The right name at the wrong digest is a substituted model: refuse."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    _Tags(monkeypatch, _served("e" * 64))

    factory = _build_reflect_llm_factory(_vault(tmp_path))

    with pytest.raises(RuntimeError, match=_REFUSAL):
        factory(PrivacyTier.OPEN, max_tokens=16)


def test_reflect_factory_refuses_another_model_when_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a pin set, a stage resolving to another local model is refused."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    _Tags(monkeypatch, _tags({"name": "creek-other-model:q4", "digest": "d" * 64}))

    other = _build_reflect_llm_factory(_vault(tmp_path, model="creek-other-model:q4"))
    with pytest.raises(RuntimeError, match=_REFUSAL):
        other(PrivacyTier.OPEN, max_tokens=16)


def test_reflect_factory_refuses_a_cloud_stage_even_when_the_pin_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cloud-routed stage is refused on its provider alone, before any dial.

    Everything else about the stage would pass: the model is the pinned tag,
    ``ollama_url`` keeps its loopback default, and the runtime serves the
    pinned digest. Only ``provider`` differs, so this isolates that check.
    """
    from creek.classify.llm import providers as providers_mod
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    tags = _Tags(monkeypatch, _served())

    class _Cloud:
        """A cloud provider that would report itself available."""

        available = True
        model = _MODEL

    monkeypatch.setattr(providers_mod, "build_provider", lambda _cfg: _Cloud())
    cloud = _build_reflect_llm_factory(_vault_cloud(tmp_path))

    with pytest.raises(RuntimeError, match=_REFUSAL):
        cloud(PrivacyTier.OPEN, max_tokens=16)
    assert tags.urls == []


def _vault_cloud(tmp_path: Path) -> Path:
    """Scaffold a vault routing generation to the pinned tag on a cloud provider."""
    vault = tmp_path / "cloud"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    data: dict[str, Any] = CreekConfig().model_dump(mode="json")
    data["vault_path"] = str(vault)
    data["llm"]["generation"] = {
        **data["llm"]["default"],
        "provider": "anthropic",
        "model": _MODEL,
    }
    vault.joinpath(*_CONFIG_SUBPATH).write_text(
        yaml.dump(data, sort_keys=False), encoding="utf-8"
    )
    return vault


def test_reflect_factory_keeps_existing_behaviour_without_pin_or_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside container mode with no manifest nothing changes."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    tags = _Tags(monkeypatch, _tags({"name": _MODEL}))

    llm = _build_reflect_llm_factory(_vault(tmp_path))(PrivacyTier.OPEN, max_tokens=16)

    assert callable(llm)
    assert len(tags.urls) == 1


@pytest.mark.parametrize("flag", [True, False])
def test_reflect_factory_refuses_remote_url_after_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, flag: bool
) -> None:
    """A post-boot config edit to a remote host is refused before any dial."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    _pin(tmp_path, monkeypatch)
    if flag:
        monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    tags = _Tags(monkeypatch, _served())

    factory = _build_reflect_llm_factory(
        _vault(tmp_path, ollama_url="http://evil.example:11434")
    )

    with pytest.raises(RuntimeError, match=_REFUSAL) as caught:
        factory(PrivacyTier.OPEN, max_tokens=16)
    assert tags.urls == []
    assert "evil.example" not in str(caught.value)


def test_reflect_factory_refuses_in_loopback_mode_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container mode without a verified pin stays model-unavailable (AC11)."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(LOOPBACK_ONLY_ENV, "1")
    _Tags(monkeypatch, _served())

    factory = _build_reflect_llm_factory(_vault(tmp_path))

    with pytest.raises(RuntimeError, match=_REFUSAL):
        factory(PrivacyTier.OPEN, max_tokens=16)


def test_reflect_factory_refuses_an_unloadable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured but invalid pin fails closed rather than unpinned."""
    from creek_mcp.server import _build_reflect_llm_factory

    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(MODEL_PACKAGE_FILE_ENV, str(tmp_path / "absent.json"))
    tags = _Tags(monkeypatch, _served())

    factory = _build_reflect_llm_factory(_vault(tmp_path))

    with pytest.raises(RuntimeError, match=_REFUSAL):
        factory(PrivacyTier.OPEN, max_tokens=16)
    assert tags.urls == []
