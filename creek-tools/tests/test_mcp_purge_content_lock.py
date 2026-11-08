"""The ``creek.purge.*`` MCP tools join the content-mutation boundary (#2086).

Each tool runs its :class:`~creek.purge.engine.PurgeEngine` call inside
:func:`~creek.vault.mutations.hold_content_mutation`, so a purge cannot delete
a fragment between a ``creek.classify`` pass's load and its write. Losing the
acquisition race is the fixed, content-free ``vault content mutation busy``
refusal — audited, like every other purge outcome, because this surface
promises an audit row for every call. A lock timeout raised *under* the hold
is not contention for the boundary and keeps the fixed ``purge engine failed``
reason (#2084), its own text withheld.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import frontmatter
import pytest

from creek._fslock import VaultLockTimeoutError
from creek.purge.engine import PurgeEngine
from creek.vault.mutations import CONTENT_MUTATION_BUSY_REASON
from creek_mcp.tools.purge import (
    purge_classifications_tool,
    purge_daterange_tool,
    purge_fragment_tool,
    purge_source_tool,
    purge_vault_tool,
)
from tests.test_mcp_content_mutation import (
    _audit_lines,
    _content_lock_held_elsewhere,
    _fragment_digests,
    _shorten_lock_wait,
)
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# 41 chars — clears the 32-char floor (#907). Test literal, not a credential.
_ELEVATED_TOKEN = "test-elevated-secret-" + "c" * 20

_FRAGMENT_ID = "frag-mcp-purge-lock-2086"
"""The one fragment every purge here targets."""

_INNER_DETAIL = "id-index lock busy"
"""The message of a timeout raised under the purge's hold."""


def _fragment_tool(vault: Path) -> dict[str, Any]:
    """Call ``creek.purge.fragment`` on the seeded fragment."""
    return purge_fragment_tool(
        vault_path=vault, fragment_id=_FRAGMENT_ID, auth_token=_ELEVATED_TOKEN
    )


def _source_tool(vault: Path) -> dict[str, Any]:
    """Call ``creek.purge.source`` on the seeded fragment's platform."""
    return purge_source_tool(
        vault_path=vault, source_type="journal", auth_token=_ELEVATED_TOKEN
    )


def _classifications_tool(vault: Path) -> dict[str, Any]:
    """Call ``creek.purge.classifications``."""
    return purge_classifications_tool(vault_path=vault, auth_token=_ELEVATED_TOKEN)


def _daterange_tool(vault: Path) -> dict[str, Any]:
    """Call ``creek.purge.daterange`` over the seeded fragment's year."""
    return purge_daterange_tool(
        vault_path=vault,
        start="2026-01-01",
        end="2026-12-31",
        auth_token=_ELEVATED_TOKEN,
    )


def _vault_tool(vault: Path) -> dict[str, Any]:
    """Call ``creek.purge.vault`` with the path echoed back."""
    return purge_vault_tool(
        vault_path=vault,
        confirm_vault_path=str(vault),
        auth_token=_ELEVATED_TOKEN,
    )


_TOOLS: dict[str, tuple[str, Callable[[Path], dict[str, Any]]]] = {
    "creek.purge.fragment": ("purge_fragment", _fragment_tool),
    "creek.purge.source": ("purge_source", _source_tool),
    "creek.purge.classifications": ("purge_classifications", _classifications_tool),
    "creek.purge.daterange": ("purge_daterange", _daterange_tool),
    "creek.purge.vault": ("purge_vault", _vault_tool),
}
"""Each tool: the engine method it reaches, and how to call it."""

_EACH_TOOL = pytest.mark.parametrize("tool", sorted(_TOOLS))


@pytest.fixture(autouse=True)
def _elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the elevated token every purge tool here passes."""
    monkeypatch.setenv("CREEK_MCP_ELEVATED_TOKEN", _ELEVATED_TOKEN)


def _seed_vault(tmp_path: Path) -> Path:
    """Write a Creek vault holding one OPEN journal fragment; return its root."""
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/audit", "01-Fragments/Notes", "03-Eddies"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    (vault / "00-Creek-Meta" / "creek_config.yaml").write_text(
        "# minimal marker\n", encoding="utf-8"
    )
    stamp = datetime(2026, 5, 1, tzinfo=UTC).isoformat()
    post = frontmatter.Post(
        content="Body.",
        type="fragment",
        id=_FRAGMENT_ID,
        title="MCP purge lock",
        created=stamp,
        ingested=stamp,
        source={"platform": "journal", "author": "self"},
        privacy_tier="open",
        eddies=[],
    )
    (vault / "01-Fragments" / "Notes" / f"{_FRAGMENT_ID}.md").write_text(
        frontmatter.dumps(post), encoding="utf-8"
    )
    return vault


def _probe_engine(
    monkeypatch: pytest.MonkeyPatch, vault: Path, method: str
) -> list[bool]:
    """Record whether the content lock is held each time *method* runs."""
    held: list[bool] = []
    real = getattr(PurgeEngine, method)

    def _probed(self: PurgeEngine, *args: Any, **kwargs: Any) -> Any:
        """Probe the content lock, then run the real engine method."""
        held.append(_content_lock_is_held(vault))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PurgeEngine, method, _probed)
    return held


@_EACH_TOOL
def test_each_purge_tool_runs_its_engine_call_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    """The engine runs under the hold, and the hold is let go after."""
    vault = _seed_vault(tmp_path)
    method, invoke = _TOOLS[tool]
    held = _probe_engine(monkeypatch, vault, method)

    response = invoke(vault)

    assert response["status"] == "ok", response
    assert held == [True]
    assert _content_lock_is_held(vault) is False


@_EACH_TOOL
def test_a_busy_vault_is_an_audited_busy_refusal_that_purges_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    """Contention is the canonical refusal, audited, with the engine never run."""
    vault = _seed_vault(tmp_path)
    before = _fragment_digests(vault)
    method, invoke = _TOOLS[tool]
    held = _probe_engine(monkeypatch, vault, method)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        response = invoke(vault)

    assert response == {
        "status": "refused",
        "tool": tool,
        "reason": CONTENT_MUTATION_BUSY_REASON,
    }
    assert held == []
    assert len(_audit_lines(vault, tool)) == 1
    assert _fragment_digests(vault) == before


@_EACH_TOOL
def test_a_timeout_under_the_purge_hold_is_not_reported_as_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    """Only acquisition is busy; an inner timeout is the fixed engine failure.

    By the time an inner lock times out the purge may already have deleted
    files, so telling the caller the vault was busy would be a false account
    of a vault nothing touched.
    """
    vault = _seed_vault(tmp_path)
    method, invoke = _TOOLS[tool]

    def _inner_times_out(_self: PurgeEngine, *_args: object) -> object:
        """Time out the way a contended inner lock does mid-purge."""
        raise VaultLockTimeoutError(_INNER_DETAIL)

    monkeypatch.setattr(PurgeEngine, method, _inner_times_out)

    response = invoke(vault)

    assert response == {
        "status": "refused",
        "tool": tool,
        "reason": "purge engine failed",
    }
    assert response["reason"] != CONTENT_MUTATION_BUSY_REASON
    assert _INNER_DETAIL not in repr(response)
    assert len(_audit_lines(vault, tool)) == 1
    assert _content_lock_is_held(vault) is False
