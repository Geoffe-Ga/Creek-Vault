"""Shared writer-boundary regressions for journal withdrawal (#1799)."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

from creek._fslock import vault_lock
from creek.compile.engine import compile_to_vault
from creek.config import CreekConfig
from creek.link.link_engine import run_link
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.mutations import content_mutation_lock_path
from tests.helpers import write_fragment_file

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
