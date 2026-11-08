"""MCP refusals for the shared vault-content mutation boundary (#1799, #2079)."""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import json
import threading
from typing import TYPE_CHECKING, Any

import pytest

from creek._fslock import VaultLockTimeoutError, vault_lock
from creek.ingest.pipeline import run_ingest
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from creek.vault.mutations import (
    CONTENT_MUTATION_BUSY_REASON,
    ContentMutationBusyError,
    content_mutation_lock_path,
)
from creek_mcp.audit import MCP_AUDIT_RELPATH
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools import ingest as ingest_mod
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.classify_entry import entry_classification_tool
from creek_mcp.tools.compile import compile_tool
from creek_mcp.tools.ingest import ingest_tool
from creek_mcp.tools.link import link_tool
from creek_mcp.tools.upload import _upload_staged_path, upload_tool
from tests.helpers import write_fragment_file
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from creek.ingest.pipeline import IngestRunResult


_STATIC_BUSY_DETAIL = "busy at a private synthetic path"


def _raise_lock_timeout(**_kwargs: object) -> object:
    """Model another load-to-write operation holding the vault boundary."""
    raise VaultLockTimeoutError(_STATIC_BUSY_DETAIL)


def _raise_content_busy(**_kwargs: object) -> object:
    """Model an engine that lost the acquisition race in its hold (#2087)."""
    raise ContentMutationBusyError(_STATIC_BUSY_DETAIL)


_BUSY_SIGNALS = pytest.mark.parametrize(
    "raise_busy",
    [_raise_content_busy, _raise_lock_timeout],
    ids=["content-mutation-busy", "legacy-lock-timeout"],
)
"""Both shapes an engine's contention can take reach the MCP tools.

Since #2087 the engines raise :class:`ContentMutationBusyError` when acquiring
the lock times out. The legacy ``VaultLockTimeoutError`` arm stays beside it,
so a stub or an older engine raising the bare timeout still reads busy and the
``/v1`` durable-job ``busy`` mapping keyed on the reason text is unchanged.
"""


def _assert_content_free_refusal(result: dict[str, object]) -> None:
    """Assert a mutation timeout has the stable, content-free MCP shape."""
    assert result["status"] == "refused"
    assert result["reason"] == "vault content mutation busy"
    assert _STATIC_BUSY_DETAIL not in str(result["reason"])


@_BUSY_SIGNALS
def test_classify_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_busy: Callable[..., object],
) -> None:
    """A bounded classifier-lock timeout remains a structured refusal."""
    from creek_mcp.tools import classify as classify_mod

    monkeypatch.setattr(classify_mod, "run_classify", raise_busy)

    _assert_content_free_refusal(classify_tool(vault_path=tmp_path, method="rules"))


@_BUSY_SIGNALS
def test_link_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_busy: Callable[..., object],
) -> None:
    """A bounded linker-lock timeout remains a structured refusal."""
    from creek_mcp.tools import link as link_mod

    monkeypatch.setattr(link_mod, "run_link", raise_busy)

    _assert_content_free_refusal(link_tool(vault_path=tmp_path, method="temporal"))


@_BUSY_SIGNALS
def test_compile_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_busy: Callable[..., object],
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
        raise_busy,
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


_BUSY_TIMEOUT_SECONDS = 0.05
"""The injected acquisition bound: long enough to be a real wait, short enough
that a contended call refuses well inside the test instead of after the 10s
production default."""

_HOLDER_READY_SECONDS = 5.0
"""How long the test waits for its holder thread to report the lock taken."""


@contextlib.contextmanager
def _content_lock_held_elsewhere(vault: Path) -> Iterator[None]:
    """Hold *vault*'s content-mutation lock from another thread for the block.

    Models a second writer (a ``creek classify`` pass, a journal upsert) that
    is mid-mutation. A real thread rather than a same-thread re-acquire, so
    the refusal is the cross-writer contention it will be in production.
    """
    taken = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        """Take the lock, report it, and keep it until told to release."""
        with vault_lock(content_mutation_lock_path(vault), timeout=1):
            taken.set()
            release.wait(timeout=_HOLDER_READY_SECONDS)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert taken.wait(timeout=_HOLDER_READY_SECONDS)
        yield
    finally:
        release.set()
        holder.join(timeout=_HOLDER_READY_SECONDS)


def _shorten_lock_wait(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Bound *module*'s content-lock acquisition at :data:`_BUSY_TIMEOUT_SECONDS`.

    Patched with ``raising=False`` so a module that takes no lock at all is a
    failing assertion (it answers instead of refusing), not a setup error.
    """
    monkeypatch.setattr(
        f"{module}.vault_lock",
        functools.partial(vault_lock, timeout=_BUSY_TIMEOUT_SECONDS),
        raising=False,
    )


def _fragment_digests(vault: Path) -> dict[str, str]:
    """Return ``{relative path: sha256}`` for every file under ``01-Fragments``."""
    root = vault / "01-Fragments"
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.md"))
    }


def _audit_lines(vault: Path, tool: str) -> list[dict[str, object]]:
    """Return the parsed MCP audit entries *tool* wrote under *vault*."""
    log = vault / MCP_AUDIT_RELPATH
    if not log.exists():
        return []
    entries = [json.loads(line) for line in log.read_text().splitlines() if line]
    return [entry for entry in entries if entry.get("tool") == tool]


def test_classify_entry_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-entry read behind a held content lock refuses, content-free (#2068).

    The audit append sits above the lock and stays unconditional — the trail
    records the attempt whatever its outcome — so exactly one line lands, and
    it carries ``has_entry_ref`` only. Nothing under ``01-Fragments`` moves.
    """
    fragment = Fragment(
        id="frag-entry-busy-2068",
        title="Content mutation boundary",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        privacy_tier=PrivacyTier.OPEN,
    )
    write_fragment_file(vault=tmp_path, fragment=fragment, body="synthetic body")
    before = _fragment_digests(tmp_path)
    _shorten_lock_wait(monkeypatch, "creek_mcp.tools.classify_entry")

    with _content_lock_held_elsewhere(tmp_path):
        result = entry_classification_tool(
            vault_path=tmp_path,
            entry_ref=fragment.id,
            privacy_tier_ceiling=TierCeiling.OPEN,
        )

    assert result == {
        "status": "refused",
        "tool": "creek.classify.entry",
        "tier_ceiling": "open",
        "reason": CONTENT_MUTATION_BUSY_REASON,
    }
    lines = _audit_lines(tmp_path, "creek.classify.entry")
    assert [line["args_summary"] for line in lines] == [{"has_entry_ref": True}]
    assert _fragment_digests(tmp_path) == before


def _b64(payload: bytes) -> str:
    """Return the base64 of *payload*, derived at runtime."""
    return base64.b64encode(payload).decode("ascii")


def _upload(
    vault: Path,
    payload: bytes,
    *,
    run: Callable[..., IngestRunResult] | None = None,
) -> dict[str, object]:
    """Upload *payload* as ``u-busy`` ``notes.txt`` at tier open, ceiling ALL."""
    return upload_tool(
        vault_path=vault,
        filename="notes.txt",
        content_base64=_b64(payload),
        external_id="u-busy",
        tier="open",
        privacy_tier_ceiling=TierCeiling.ALL,
        run=run,
    )


def test_upload_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upload behind a held content lock refuses before staging (#2069).

    The busy refusal is **unaudited**, like compile's (#930): it is not a
    ceiling-versus-content decision, and nothing was attempted against the
    vault. A re-send of new bytes under an existing id is the sharp case,
    because the refusal must leave the previously staged bytes and the
    fragment they produced byte-identical.
    """
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    seeded = _upload(tmp_path, b"the first honest sentence about the creek")
    assert seeded["status"] == "ok", seeded
    staged = _upload_staged_path(tmp_path, "u-busy", "notes.txt")
    staged_before = staged.read_bytes()
    fragments_before = _fragment_digests(tmp_path)
    audit_before = len(_audit_lines(tmp_path, "creek.upload"))
    _shorten_lock_wait(monkeypatch, "creek_mcp.tools.upload")

    with _content_lock_held_elsewhere(tmp_path):
        result = _upload(tmp_path, b"a second sentence that must not land")

    assert result == {
        "status": "refused",
        "tool": "creek.upload",
        "tier_ceiling": "all",
        "reason": CONTENT_MUTATION_BUSY_REASON,
    }
    assert staged.read_bytes() == staged_before
    assert _fragment_digests(tmp_path) == fragments_before
    assert len(_audit_lines(tmp_path, "creek.upload")) == audit_before


def test_an_index_lock_timeout_inside_the_upload_hold_is_not_reported_as_busy(
    tmp_path: Path,
) -> None:
    """Only a timeout *acquiring* the content lock becomes the busy refusal.

    A ``VaultLockTimeoutError`` raised under the hold — the VaultWriter's
    ``.id-index.lock``, say — comes after ``_stage_upload`` has written the
    caller's bytes, so reporting it as ``vault content mutation busy`` would
    tell the caller nothing was staged when something was. It propagates, as
    it did before the content lock was taken here.
    """

    def _index_lock_times_out(**_kwargs: object) -> IngestRunResult:
        """Time out the way a contended id-index lock does mid-ingest."""
        msg = "id-index lock busy"
        raise VaultLockTimeoutError(msg)

    with pytest.raises(VaultLockTimeoutError, match="id-index lock busy"):
        _upload(
            tmp_path,
            b"bytes staged before the inner timeout",
            run=_index_lock_times_out,
        )


# --------------------------------------------------------------------------- #
# creek.ingest joins the boundary (#2079)
# --------------------------------------------------------------------------- #


def _seed_ingest_vault(vault: Path) -> None:
    """Scaffold *vault* and stage ``inbox/note.md`` for ``creek.ingest``."""
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    (vault / "inbox").mkdir(exist_ok=True)
    (vault / "inbox" / "note.md").write_text(
        "# Note\n\nA synthetic sentence about the creek at dawn.\n"
    )


def _ingest(
    vault: Path,
    *,
    source_type: str = "markdown",
    input_path: str = "inbox/note.md",
    ceiling: TierCeiling = TierCeiling.ALL,
) -> dict[str, object]:
    """Call ``creek.ingest`` on the staged note (by default) at ceiling ALL."""
    return ingest_tool(
        vault_path=vault,
        source_type=source_type,
        input_path=input_path,
        privacy_tier_ceiling=ceiling,
    )


def test_ingest_refuses_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek.ingest`` behind a held content lock refuses, content-free (#2079).

    Only the #885 attempt row is audited: the success row carries write-side
    facts, and nothing was written. ``01-Fragments`` is byte-identical.
    """
    _seed_ingest_vault(tmp_path)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    before = _fragment_digests(tmp_path)

    with _content_lock_held_elsewhere(tmp_path):
        result = _ingest(tmp_path)

    assert result == {
        "status": "refused",
        "tool": "creek.ingest",
        "tier_ceiling": "all",
        "reason": CONTENT_MUTATION_BUSY_REASON,
    }
    assert _fragment_digests(tmp_path) == before
    assert len(_audit_lines(tmp_path, "creek.ingest")) == 1


def test_ingest_runs_run_ingest_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole ``run_ingest`` call — writes, ledger, tombs — is inside the hold."""
    _seed_ingest_vault(tmp_path)
    held: list[bool] = []

    def _probed(**kwargs: Any) -> IngestRunResult:
        """Probe the content lock, then run the real pipeline."""
        held.append(_content_lock_is_held(tmp_path))
        return run_ingest(**kwargs)

    monkeypatch.setattr(ingest_mod, "run_ingest", _probed)

    result = _ingest(tmp_path)

    assert result["status"] == "ok", result
    assert held == [True]
    assert _content_lock_is_held(tmp_path) is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"ceiling": TierCeiling.OPEN},
            "ingest default tier 'personal' exceeds ceiling 'open'",
        ),
        ({"source_type": "no-such-type"}, "unknown source_type 'no-such-type'"),
        (
            {"input_path": "../outside.md"},
            "input_path '../outside.md' resolves outside the vault root; "
            "the ingest tool only operates on vault-relative paths.",
        ),
        (
            {"input_path": "inbox/missing.md"},
            "input path not found: inbox/missing.md",
        ),
    ],
)
def test_ingest_gate_refusals_precede_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    """Every gate refuses as it always has, even while the lock is contended.

    The busy refusal comes *after* the tier, source_type, confinement and
    existence gates, so contention never masks — or reveals — what they decide.
    """
    vault = tmp_path / "vault"
    _seed_ingest_vault(vault)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        result = _ingest(vault, **overrides)

    assert result["status"] == "refused"
    assert result["reason"] == reason


def test_an_index_lock_timeout_inside_the_ingest_hold_is_not_reported_as_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``VaultLockTimeoutError`` raised under the hold propagates (#2079).

    Fragments may already be written when the writer's ``.id-index.lock``
    times out, so answering ``vault content mutation busy`` would be false.
    """
    _seed_ingest_vault(tmp_path)

    def _index_lock_times_out(**_kwargs: object) -> IngestRunResult:
        """Time out the way a contended id-index lock does mid-ingest."""
        msg = "id-index lock busy"
        raise VaultLockTimeoutError(msg)

    monkeypatch.setattr(ingest_mod, "run_ingest", _index_lock_times_out)

    with pytest.raises(VaultLockTimeoutError, match="id-index lock busy"):
        _ingest(tmp_path)


# --------------------------------------------------------------------------- #
# Real contention through the migrated engines (#2087)
# --------------------------------------------------------------------------- #


def _seed_open_fragment(vault: Path) -> Fragment:
    """Write and return one OPEN journal fragment under *vault*."""
    fragment = Fragment(
        id="frag-content-busy-2087",
        title="Content mutation boundary",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        privacy_tier=PrivacyTier.OPEN,
    )
    write_fragment_file(vault=vault, fragment=fragment, body="synthetic body")
    return fragment


def _call_tool(tool: str, vault: Path) -> dict[str, Any]:
    """Invoke MCP tool *tool* over *vault* with arguments that pass its gates."""
    if tool == "classify":
        return classify_tool(vault_path=vault, method="rules")
    if tool == "link":
        return link_tool(vault_path=vault, method="temporal")
    return compile_tool(
        vault_path=vault,
        fragment_ids=["frag-content-busy-2087"],
        target_kind="thread",
        target_id="thread-content-busy-2087",
        target_title="Content mutation boundary",
        llm_factory=lambda _tier: lambda _prompt: "not reached",
        privacy_tier_ceiling=TierCeiling.OPEN,
    )


@pytest.mark.parametrize("tool", ["classify", "link", "compile"])
def test_a_tool_meeting_real_contention_refuses_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    """The engine's :class:`ContentMutationBusyError` reaches the busy arm.

    No stub: the lock is held by another thread and the engine's own hold
    loses the race, so the refusal proves the tool catches the type the
    engines now raise and not only the legacy bare timeout.
    """
    _seed_open_fragment(tmp_path)
    before = _fragment_digests(tmp_path)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(tmp_path):
        result = _call_tool(tool, tmp_path)

    assert result["status"] == "refused", result
    assert result["reason"] == CONTENT_MUTATION_BUSY_REASON
    assert _fragment_digests(tmp_path) == before
