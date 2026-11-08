"""Shared writer-boundary regressions: journal withdrawal (#1799), ingest (#2079)."""

from __future__ import annotations

import base64
import json
import threading
from typing import TYPE_CHECKING

import pytest

from creek._fslock import VaultLockTimeoutError, vault_lock
from creek.classify.classify_engine import run_classify
from creek.compile.engine import compile_to_vault
from creek.config import CreekConfig
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest.pipeline import run_ingest
from creek.link.link_engine import run_link
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.mutations import (
    CONTENT_MUTATION_BUSY_REASON,
    ContentMutationBusyError,
    content_mutation_lock_path,
    hold_content_mutation,
)
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify_entry import entry_classification_tool
from creek_mcp.tools.ingest import ingest_tool
from creek_mcp.tools.upload import upload_tool
from tests.helpers import write_fragment_file
from tests.test_mcp_content_mutation import (
    _content_lock_held_elsewhere,
    _fragment_digests,
    _shorten_lock_wait,
)
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _seed_fragment(vault: Path) -> Fragment:
    """Write and return one source fragment for writer-boundary tests."""
    fragment = Fragment(
        id="frag-content-mutation-1799",
        title="Content mutation boundary",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
    )
    write_fragment_file(vault=vault, fragment=fragment, body="synthetic body")
    return fragment


def _assert_operation_waits_for_content_lock(
    vault: Path,
    operation: Callable[[], object],
) -> None:
    """Assert *operation* cannot cross the shared writer boundary concurrently."""
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        """Execute the operation and retain any thread failure for the caller."""
        started.set()
        try:
            operation()
        except BaseException as exc:  # captured and re-raised below
            errors.append(exc)
        finally:
            finished.set()

    with vault_lock(content_mutation_lock_path(vault), timeout=1):
        worker = threading.Thread(target=run)
        worker.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.2)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []


def test_link_waits_at_the_shared_content_mutation_boundary(tmp_path: Path) -> None:
    """Link cannot materialise a pre-withdraw snapshot after journal DELETE."""
    vault = tmp_path / "vault"
    _seed_fragment(vault)

    _assert_operation_waits_for_content_lock(
        vault,
        lambda: run_link(
            vault_path=vault,
            config=CreekConfig(),
            method="temporal",
            rebuild=False,
        ),
    )


def test_compile_waits_at_the_shared_content_mutation_boundary(tmp_path: Path) -> None:
    """Compile cannot materialise a pre-withdraw snapshot after journal DELETE."""
    vault = tmp_path / "vault"
    fragment = _seed_fragment(vault)
    response = json.dumps(
        {
            "claims": [
                {
                    "id": "claim-content-mutation-1799",
                    "text": "Synthetic claim.",
                    "fragment_ids": [fragment.id],
                }
            ],
            "paradoxes": [],
        }
    )

    _assert_operation_waits_for_content_lock(
        vault,
        lambda: compile_to_vault(
            fragment_ids=[fragment.id],
            vault_path=vault,
            target_kind="thread",
            target_id="thread-content-mutation-1799",
            target_title="Content mutation boundary",
            llm_factory=lambda _tier: lambda _prompt: response,
        ),
    )


def test_upload_waits_at_the_shared_content_mutation_boundary(tmp_path: Path) -> None:
    """Upload's #970 gate and its ingest cannot interleave with a re-tier (#2069).

    The overwrite gate reads the tier ``creek classify`` writes under this
    lock; reading it outside the lock let a concurrent escalation land between
    the admission and the overwrite it admitted.
    """
    vault = tmp_path / "vault"
    _seed_fragment(vault)

    _assert_operation_waits_for_content_lock(
        vault,
        lambda: upload_tool(
            vault_path=vault,
            filename="boundary.txt",
            content_base64=base64.b64encode(b"synthetic upload body").decode(),
            external_id="u-boundary-2069",
            tier="open",
            privacy_tier_ceiling=TierCeiling.ALL,
        ),
    )


def test_classify_entry_waits_at_the_shared_content_mutation_boundary(
    tmp_path: Path,
) -> None:
    """A per-entry read takes its one snapshot inside the boundary (#2068).

    It writes no content, but the tier it gates on and the classification it
    serves must come from one state of the fragment, and ``creek classify``
    rewrites both under this lock.
    """
    vault = tmp_path / "vault"
    fragment = _seed_fragment(vault)

    _assert_operation_waits_for_content_lock(
        vault,
        lambda: entry_classification_tool(
            vault_path=vault,
            entry_ref=fragment.id,
            privacy_tier_ceiling=TierCeiling.ALL,
        ),
    )


# --------------------------------------------------------------------------- #
# The acquisition-only hold every ingest surface takes (#2079)
# --------------------------------------------------------------------------- #


def test_the_hold_runs_its_body_with_the_content_lock_held(tmp_path: Path) -> None:
    """The body of :func:`hold_content_mutation` is inside the boundary."""
    vault = tmp_path / "vault"
    held: list[bool] = []

    with hold_content_mutation(vault):
        held.append(_content_lock_is_held(vault))

    assert held == [True]
    assert _content_lock_is_held(vault) is False


def test_run_link_does_its_work_inside_the_content_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_link``'s whole pass runs under the hold, not after it (#2087).

    The busy test only proves the wait happens at acquisition; this one fails
    if the pass is moved out of the ``with`` block, for example to load the
    embedding model first, which would reopen the #1799 withdraw race for link.
    """
    vault = tmp_path / "vault"
    held: list[bool] = []

    def _recording_pass(**_kwargs: object) -> object:
        """Record whether the content lock is held while the pass runs."""
        held.append(_content_lock_is_held(vault))
        return None

    monkeypatch.setattr(
        "creek.link.link_engine._run_link_with_content_lock_held", _recording_pass
    )

    run_link(vault_path=vault, config=CreekConfig(), method="temporal", rebuild=False)

    assert held == [True]
    assert _content_lock_is_held(vault) is False


def test_a_contended_hold_raises_busy_and_never_runs_its_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the race is the typed busy signal, not a bare lock timeout.

    ``ContentMutationBusyError`` is deliberately *not* a
    ``VaultLockTimeoutError``: no existing ``except VaultLockTimeoutError`` arm
    may absorb it, and an inner timeout must never be mistaken for it.
    """
    vault = tmp_path / "vault"
    ran: list[bool] = []
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with (
        _content_lock_held_elsewhere(vault),
        pytest.raises(ContentMutationBusyError) as caught,
        hold_content_mutation(vault),
    ):
        ran.append(True)

    assert ran == []
    assert str(caught.value) == CONTENT_MUTATION_BUSY_REASON
    assert not isinstance(caught.value, VaultLockTimeoutError)
    assert isinstance(caught.value.__cause__, VaultLockTimeoutError)


def test_a_lock_timeout_inside_the_hold_propagates_unchanged(tmp_path: Path) -> None:
    """Only *acquisition* is busy; a timeout raised under the hold is not.

    The writer's ``.id-index.lock`` can time out mid-ingest, after fragments
    may already be on disk. Re-labelling that as "busy" would tell a caller
    nothing was written when something was.
    """
    vault = tmp_path / "vault"
    inner = VaultLockTimeoutError("id-index lock busy")

    with pytest.raises(VaultLockTimeoutError) as caught, hold_content_mutation(vault):
        raise inner

    assert caught.value is inner
    assert not isinstance(caught.value, ContentMutationBusyError)


def test_the_hold_releases_the_lock_when_its_body_raises(tmp_path: Path) -> None:
    """An exception in the body cannot strand the boundary."""
    vault = tmp_path / "vault"

    msg = "body failed"
    with pytest.raises(RuntimeError, match=msg), hold_content_mutation(vault):
        raise RuntimeError(msg)

    assert _content_lock_is_held(vault) is False


def test_run_ingest_itself_takes_no_content_lock(tmp_path: Path) -> None:
    """``run_ingest`` stays lock-free: its callers own the boundary (#2079).

    ``creek.upload`` and ``creek.journal`` already call ``run_ingest`` while
    holding the non-reentrant content lock. Were ``run_ingest`` to take it as
    well, this same-thread call would wait out the timeout and fail.
    """
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta", "01-Fragments"):
        (vault / sub).mkdir(parents=True)
    source = tmp_path / "src" / "note.md"
    source.parent.mkdir()
    source.write_text("# Note\n\nA sentence written under the caller's hold.\n")

    with hold_content_mutation(vault):
        result = run_ingest(
            ingestor_cls=INGESTOR_REGISTRY["markdown"],
            source_type="markdown",
            input_path=source,
            vault_path=vault,
        )

    assert result.written == 1
    assert result.errors == []


def test_ingest_then_classify_then_link_in_one_thread_releases_every_lock(
    tmp_path: Path,
) -> None:
    """The three surfaces run back to back in one thread without deadlock.

    Each takes the non-reentrant content lock in turn; none may still hold it
    when the next starts, and nothing may hold it at the end.
    """
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/State", "00-Creek-Meta/audit", "01-Fragments"):
        (vault / sub).mkdir(parents=True)
    (vault / "inbox").mkdir()
    (vault / "inbox" / "note.md").write_text(
        "# Note\n\nA morning walk along the creek before work.\n"
    )

    ingested = ingest_tool(
        vault_path=vault,
        source_type="markdown",
        input_path="inbox/note.md",
        privacy_tier_ceiling=TierCeiling.ALL,
    )
    run_classify(vault_path=vault, config=CreekConfig(), method="rules", force=False)
    run_link(vault_path=vault, config=CreekConfig(), method="temporal", rebuild=False)

    assert ingested["status"] == "ok", ingested
    assert ingested["written"] == 1
    assert _content_lock_is_held(vault) is False


# --------------------------------------------------------------------------- #
# The three engines take the boundary through the same hold (#2087)
# --------------------------------------------------------------------------- #

_ENGINES: tuple[str, ...] = ("classify", "link", "compile")
"""The engines that acquire the content lock themselves, by short name."""

_INNER_BODIES: dict[str, str] = {
    "classify": "creek.classify.classify_engine._run_classify_with_content_lock_held",
    "link": "creek.link.link_engine._run_link_with_content_lock_held",
    "compile": "creek.compile.engine._compile_to_vault_with_content_lock_held",
}
"""Where each engine's work runs once it holds the lock."""


def _run_engine(name: str, vault: Path) -> object:
    """Call the public entry point of engine *name* over *vault*."""
    if name == "classify":
        return run_classify(
            vault_path=vault, config=CreekConfig(), method="rules", force=False
        )
    if name == "link":
        return run_link(
            vault_path=vault, config=CreekConfig(), method="temporal", rebuild=False
        )
    return compile_to_vault(
        fragment_ids=["frag-content-mutation-1799"],
        vault_path=vault,
        target_kind="thread",
        target_id="thread-content-mutation-2087",
        target_title="Content mutation boundary",
        llm_factory=lambda _tier: lambda _prompt: "not reached",
    )


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_contended_engine_raises_busy_rather_than_a_bare_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    """Losing the acquisition race is :class:`ContentMutationBusyError` (#2087).

    The patch target is the one in :mod:`creek.vault.mutations`: an engine that
    still took the raw lock would wait out the production default and raise
    the bare ``VaultLockTimeoutError`` its callers cannot tell from an inner
    one.
    """
    vault = tmp_path / "vault"
    _seed_fragment(vault)
    before = _fragment_digests(vault)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with (
        _content_lock_held_elsewhere(vault),
        pytest.raises(ContentMutationBusyError) as caught,
    ):
        _run_engine(engine, vault)

    assert str(caught.value) == CONTENT_MUTATION_BUSY_REASON
    assert isinstance(caught.value.__cause__, VaultLockTimeoutError)
    assert _fragment_digests(vault) == before


@pytest.mark.parametrize("engine", _ENGINES)
def test_a_lock_timeout_under_an_engine_hold_propagates_as_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    """A timeout inside the held body is not re-labelled busy (#2087).

    The engine may already have written by then, so reporting "busy — nothing
    happened" would be false.
    """
    vault = tmp_path / "vault"
    _seed_fragment(vault)
    inner = VaultLockTimeoutError("id-index lock busy")

    def _inner_times_out(**_kwargs: object) -> object:
        """Time out the way a contended inner lock does mid-pass."""
        raise inner

    monkeypatch.setattr(_INNER_BODIES[engine], _inner_times_out)

    with pytest.raises(VaultLockTimeoutError) as caught:
        _run_engine(engine, vault)

    assert caught.value is inner
    assert _content_lock_is_held(vault) is False
