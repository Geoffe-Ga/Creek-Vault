"""``creek process`` holds the content lock across stages 2-4 only (#2086).

Stages 2-4 (ingest, classify, vault write) build fragments and then write
them into ``01-Fragments``, the snapshot ``creek classify`` and ``creek link``
load under :func:`creek.vault.mutations.content_mutation_lock_path`.
:meth:`creek.pipeline.Pipeline.run` therefore holds that lock across those
three stages. It must let go before stage 5, because ``run_link`` takes the
same lock itself and the lock is not reentrant: holding it across stage 5
would make every ``creek process`` wait out the bound and fail at link time.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from creek import pipeline as pipeline_mod
from creek._fslock import DEFAULT_LOCK_TIMEOUT_SECONDS
from creek.cli import app
from creek.link.link_engine import LinkSummary, run_link
from creek.vault.mutations import CONTENT_MUTATION_BUSY_REASON
from tests.test_mcp_content_mutation import (
    _content_lock_held_elsewhere,
    _fragment_digests,
    _shorten_lock_wait,
)
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from typer.testing import Result

runner = CliRunner()

_BUSY_LINE = (
    f"{CONTENT_MUTATION_BUSY_REASON}: another Creek writer holds this vault; "
    "retry once it finishes."
)
"""The whole line the CLI prints for contention, as the operator reads it."""


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so a phrase can be matched whole."""
    return " ".join(text.split())


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(source, vault)``: one markdown note and a scaffolded vault."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("# A note\n\nBody.\n", encoding="utf-8")
    vault = tmp_path / "vault"
    for name in ("00-Creek-Meta", "01-Fragments", "02-Threads", "03-Eddies"):
        (vault / name).mkdir(parents=True, exist_ok=True)
    return source, vault


def _invoke_process(source: Path, vault: Path) -> Result:
    """Run ``creek process --no-llm`` from *source* into *vault*."""
    return runner.invoke(
        app,
        [
            "process",
            "--source",
            str(source),
            "--vault",
            str(vault),
            "--yes",
            "--no-llm",
        ],
    )


def _probe_vault_write(
    monkeypatch: pytest.MonkeyPatch, vault: Path, held: list[str]
) -> None:
    """Record ``write held``/``write free`` each time stage 4 runs."""
    real = pipeline_mod.Pipeline._write_to_vault

    def _probed(self: pipeline_mod.Pipeline, *args: Any, **kwargs: Any) -> None:
        """Probe the content lock, then run the real vault write."""
        held.append("write held" if _content_lock_is_held(vault) else "write free")
        real(self, *args, **kwargs)

    monkeypatch.setattr(pipeline_mod.Pipeline, "_write_to_vault", _probed)


def _probe_link_stage(
    monkeypatch: pytest.MonkeyPatch, vault: Path, held: list[str]
) -> None:
    """Record the lock state at stage 5, then take the real link hold.

    The real :func:`run_link` runs (``temporal``, which loads no model), so a
    stage 5 still nested inside the stage 2-4 hold would meet the
    non-reentrant lock for real rather than merely be reported as nested.
    """

    def _probed(**kwargs: Any) -> LinkSummary:
        """Probe the content lock, then link for real."""
        held.append("link held" if _content_lock_is_held(vault) else "link free")
        return run_link(
            vault_path=kwargs["vault_path"],
            config=kwargs["config"],
            method="temporal",
            rebuild=False,
        )

    monkeypatch.setattr(pipeline_mod, "run_link", _probed)


def test_stages_2_to_4_write_inside_the_lock_and_stage_5_links_outside_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 4 writes under the hold; stage 5 is entered with it released.

    The run also finishes well inside one lock bound: a nested stage 5 would
    either fail on the non-reentrant lock or wait the whole bound out.
    """
    source, vault = _seed(tmp_path)
    held: list[str] = []
    _probe_vault_write(monkeypatch, vault, held)
    _probe_link_stage(monkeypatch, vault, held)
    started = time.monotonic()

    result = _invoke_process(source, vault)

    elapsed = time.monotonic() - started
    assert result.exit_code == 0, result.output
    assert held == ["write held", "link free", "link free"]
    assert elapsed < DEFAULT_LOCK_TIMEOUT_SECONDS
    assert _content_lock_is_held(vault) is False
    assert len(_fragment_digests(vault)) == 1


def test_a_busy_vault_stops_creek_process_before_it_writes_a_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contention at the stage 2-4 hold is the busy line and nothing written."""
    source, vault = _seed(tmp_path)
    held: list[str] = []
    _probe_vault_write(monkeypatch, vault, held)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        result = _invoke_process(source, vault)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert held == []
    assert _fragment_digests(vault) == {}
