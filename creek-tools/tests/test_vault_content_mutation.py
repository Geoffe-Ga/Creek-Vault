"""Shared writer-boundary regressions for journal withdrawal (#1799)."""

from __future__ import annotations

import base64
import json
import threading
from typing import TYPE_CHECKING

from creek._fslock import vault_lock
from creek.compile.engine import compile_to_vault
from creek.config import CreekConfig
from creek.link.link_engine import run_link
from creek.models import Fragment, FragmentSource, SourcePlatform
from creek.vault.mutations import content_mutation_lock_path
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify_entry import entry_classification_tool
from creek_mcp.tools.upload import upload_tool
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
