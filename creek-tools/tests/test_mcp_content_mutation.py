"""MCP refusals for the shared vault-content mutation boundary (#1799)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek._fslock import VaultLockTimeoutError
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.compile import compile_tool
from creek_mcp.tools.link import link_tool
from tests.helpers import write_fragment_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_STATIC_BUSY_DETAIL = "busy at a private synthetic path"


def _raise_lock_timeout(**_kwargs: object) -> object:
    """Model another load-to-write operation holding the vault boundary."""
    raise VaultLockTimeoutError(_STATIC_BUSY_DETAIL)


def _assert_content_free_refusal(result: dict[str, object]) -> None:
    """Assert a mutation timeout has the stable, content-free MCP shape."""
    assert result["status"] == "refused"
    assert result["reason"] == "vault content mutation busy"
    assert _STATIC_BUSY_DETAIL not in str(result["reason"])


def test_classify_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded classifier-lock timeout remains a structured refusal."""
    from creek_mcp.tools import classify as classify_mod

    monkeypatch.setattr(classify_mod, "run_classify", _raise_lock_timeout)

    _assert_content_free_refusal(classify_tool(vault_path=tmp_path, method="rules"))


def test_link_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded linker-lock timeout remains a structured refusal."""
    from creek_mcp.tools import link as link_mod

    monkeypatch.setattr(link_mod, "run_link", _raise_lock_timeout)

    _assert_content_free_refusal(link_tool(vault_path=tmp_path, method="temporal"))


def test_compile_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded compiler-lock timeout remains a structured refusal."""
    fragment = Fragment(
        id="frag-content-busy-1799",
        title="Content mutation boundary",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        privacy_tier=PrivacyTier.OPEN,
    )
    write_fragment_file(vault=tmp_path, fragment=fragment, body="synthetic body")
    monkeypatch.setattr(
        "creek_mcp.tools.compile.compile_to_vault",
        _raise_lock_timeout,
    )

    result = compile_tool(
        vault_path=tmp_path,
        fragment_ids=[fragment.id],
        target_kind="thread",
        target_id="thread-content-busy-1799",
        target_title="Content mutation boundary",
        llm_factory=lambda _tier: lambda _prompt: "not reached",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )

    _assert_content_free_refusal(result)
