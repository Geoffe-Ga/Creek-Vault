"""Tests for the quality-aware classification upgrade in ``creek fill`` (#736).

Covers the ``classification_provider`` provenance stamp, the upgrade-offer
detection, and the three ``fill`` paths: ``--upgrade`` applies it, a
non-interactive run is a safe no-op, and Intimate is never routed to cloud.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import frontmatter
from typer.testing import CliRunner

import creek.cli as cli_mod
from creek.classify import fidelity as fid
from creek.classify.classify_engine import _write_fragment
from creek.classify.constants import CLASSIFICATION_PROVIDER_KEY
from creek.cli import _detect_classify_upgrade, app
from creek.config import CreekConfig, LLMConfig, LLMRoutingConfig
from creek.models import Fragment, FragmentSource, SourcePlatform
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()


class _StubProvider:
    """Provider double with a controllable availability + cloud flag."""

    def __init__(self, *, available: bool, is_cloud: bool) -> None:
        self.available = available
        self.is_cloud = is_cloud


def _cloud_config() -> CreekConfig:
    """A config whose classification stage is cloud and default is local."""
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(provider="ollama", model="qwen3:8b"),
            classification=LLMConfig(provider="anthropic", model="claude-haiku-4-5"),
        )
    )


def _patch_cloud_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fidelity probe see every provider as available."""
    monkeypatch.setattr(
        fid,
        "build_provider",
        lambda cfg: _StubProvider(
            available=True, is_cloud=fid.provider_is_cloud(cfg.provider)
        ),
    )


def _write_rules_fragment(vault: Path, frag_id: str) -> None:
    """Write a fragment stamped ``classification_method: rules``."""
    write_fragment_file(
        vault=vault,
        fragment=Fragment(
            id=frag_id,
            title="a note",
            source=FragmentSource(platform=SourcePlatform.MARKDOWN),
        ),
        body="body",
        method="rules",
    )


# ---- provenance stamp ----


def test_write_fragment_stamps_provider_for_llm(tmp_path: Path) -> None:
    """An ``llm`` write records the provider as ``classification_provider``."""
    md = tmp_path / "frag.md"
    md.write_text("---\ntype: fragment\n---\nbody\n", encoding="utf-8")
    fragment = Fragment(
        id="frag-00000000llm1",
        title="t",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        md_file=md,
        fragment=fragment,
        body="body",
        method="llm",
        raw={"type": "fragment"},
        reasoning="",
        provider="anthropic",
    )
    assert frontmatter.load(md).metadata[CLASSIFICATION_PROVIDER_KEY] == "anthropic"


def test_write_fragment_clears_provider_for_rules(tmp_path: Path) -> None:
    """A ``rules`` write clears any stale ``classification_provider`` stamp."""
    md = tmp_path / "frag.md"
    md.write_text("---\ntype: fragment\n---\nbody\n", encoding="utf-8")
    fragment = Fragment(
        id="frag-00000000rul1",
        title="t",
        source=FragmentSource(platform=SourcePlatform.MARKDOWN),
    )
    _write_fragment(
        md_file=md,
        fragment=fragment,
        body="body",
        method="rules",
        raw={"type": "fragment", CLASSIFICATION_PROVIDER_KEY: "anthropic"},
        reasoning="",
        provider=None,
    )
    assert CLASSIFICATION_PROVIDER_KEY not in frontmatter.load(md).metadata


# ---- offer detection ----


def test_detect_offers_upgrade_for_rules_with_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rules fragments + an available LLM yield an offer counting them."""
    _patch_cloud_available(monkeypatch)
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    _write_rules_fragment(vault, "frag-00000000rua1")
    _write_rules_fragment(vault, "frag-00000000rua2")

    offer = _detect_classify_upgrade(vault, _cloud_config())

    assert offer is not None
    assert offer.count == 2
    assert "anthropic" in offer.non_intimate_label
    assert "ollama" in offer.intimate_label  # Intimate route stays local


def test_detect_none_when_already_llm_or_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No offer when nothing is rules-classified."""
    _patch_cloud_available(monkeypatch)
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    for fid_, method in (
        ("frag-0000000manual", "manual"),
        ("frag-00000000llm9", "llm"),
    ):
        write_fragment_file(
            vault=vault,
            fragment=Fragment(
                id=fid_,
                title="t",
                source=FragmentSource(platform=SourcePlatform.MARKDOWN),
            ),
            body="body",
            method=method,
        )

    assert _detect_classify_upgrade(vault, _cloud_config()) is None


def test_detect_none_when_no_llm_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No offer when no LLM is reachable — rules is already the best."""
    monkeypatch.setattr(
        fid, "build_provider", lambda cfg: _StubProvider(available=False, is_cloud=True)
    )
    vault = tmp_path / "vault"
    (vault / "01-Fragments").mkdir(parents=True)
    _write_rules_fragment(vault, "frag-00000000rub1")

    assert _detect_classify_upgrade(vault, _cloud_config()) is None


# ---- fill paths ----


def _fill_vault(tmp_path: Path) -> Path:
    """A scaffolded vault with one rules fragment, ready for ``creek fill``."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta").mkdir(parents=True)
    (vault / "01-Fragments").mkdir(parents=True)
    _write_rules_fragment(vault, "frag-00000000fil1")
    return vault


def test_fill_upgrade_flag_reclassifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``creek fill --upgrade`` re-classifies via the LLM without force."""
    _patch_cloud_available(monkeypatch)
    monkeypatch.setattr(cli_mod, "_load_config_for_vault", lambda _v: _cloud_config())
    monkeypatch.setattr(cli_mod, "_build_fill_steps", lambda *a, **k: [])
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "creek.classify.classify_engine.run_classify",
        lambda **kwargs: calls.append(kwargs),
    )

    vault = _fill_vault(tmp_path)
    result = runner.invoke(app, ["fill", "--vault", str(vault), "--upgrade"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["method"] == "llm"
    assert calls[0]["force"] is False  # only rules upgraded; manual/llm preserved


def test_fill_non_interactive_default_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --upgrade (and no TTY) fill never re-classifies — just hints."""
    _patch_cloud_available(monkeypatch)
    monkeypatch.setattr(cli_mod, "_load_config_for_vault", lambda _v: _cloud_config())
    monkeypatch.setattr(cli_mod, "_build_fill_steps", lambda *a, **k: [])
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "creek.classify.classify_engine.run_classify",
        lambda **kwargs: calls.append(kwargs),
    )

    vault = _fill_vault(tmp_path)
    result = runner.invoke(app, ["fill", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    assert calls == []  # never silently re-classified / egressed
    assert "--upgrade" in result.output  # surfaced the option as a hint
