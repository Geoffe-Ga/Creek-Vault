"""``creek ingest`` and ``creek sync`` Tier A join the content boundary (#2079).

Both reach the vault through :func:`creek.cli._run_ingest`, which writes
``01-Fragments``, its ledger and any tombs. Those are exactly what
``creek classify`` and ``creek link`` load and rewrite under
:func:`creek.vault.mutations.content_mutation_lock_path`, so the ingest must
hold that lock for its whole run — and must say so legibly, not with a
traceback, when another writer holds it first.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from typer.testing import CliRunner

from creek._fslock import VaultLockTimeoutError
from creek.cli import _sync_state_path, app
from creek.config import CreekConfig, SyncConfig
from creek.ingest import pipeline as pipeline_mod
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
    from click.testing import Result

    from creek.ingest.pipeline import IngestRunResult

runner = CliRunner()

_LOCKS_RELPATH = "00-Creek-Meta/locks"
"""Where :func:`creek._fslock.vault_lock` would scaffold the content lock."""


def _seed_vault(vault: Path) -> None:
    """Scaffold the two directories :class:`VaultWriter` requires."""
    for sub in ("00-Creek-Meta/Processing-Log", "01-Fragments/Notes"):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def _seed_source(root: Path) -> Path:
    """Write one markdown note under *root*/src and return the directory."""
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "note.md").write_text("# Hello\n\nA first note about the creek.\n")
    return src


def _invoke_ingest(vault: Path, src: Path) -> Result:
    """Run ``creek ingest --type markdown`` over *src* into *vault*."""
    return runner.invoke(
        app,
        [
            "ingest",
            "--type",
            "markdown",
            "--input",
            str(src),
            "--vault",
            str(vault),
            "--yes",
        ],
    )


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so a phrase can be matched whole."""
    return " ".join(text.split())


def test_creek_ingest_runs_run_ingest_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole ``run_ingest`` call is inside the hold, and it is let go after."""
    vault = tmp_path / "vault"
    _seed_vault(vault)
    src = _seed_source(tmp_path)
    held: list[bool] = []
    real = pipeline_mod.run_ingest

    def _probed(**kwargs: Any) -> IngestRunResult:
        """Probe the content lock, then run the real pipeline."""
        held.append(_content_lock_is_held(vault))
        return real(**kwargs)

    monkeypatch.setattr(pipeline_mod, "run_ingest", _probed)

    result = _invoke_ingest(vault, src)

    assert result.exit_code == 0, result.output
    assert held == [True]
    assert _content_lock_is_held(vault) is False
    assert len(_fragment_digests(vault)) == 1


def test_creek_ingest_exits_1_legibly_when_content_mutation_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contention is one line and exit 1, never a traceback, and writes nothing."""
    vault = tmp_path / "vault"
    _seed_vault(vault)
    src = _seed_source(tmp_path)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    before = _fragment_digests(vault)

    with _content_lock_held_elsewhere(vault):
        result = _invoke_ingest(vault, src)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert CONTENT_MUTATION_BUSY_REASON in _flat(result.output)
    assert "Traceback" not in result.output
    assert "Ingested" not in result.output
    assert _fragment_digests(vault) == before


def test_an_inner_lock_timeout_under_creek_ingest_is_not_reported_as_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only acquiring the content lock is "busy"; a timeout under it propagates."""
    vault = tmp_path / "vault"
    _seed_vault(vault)
    src = _seed_source(tmp_path)

    def _index_lock_times_out(**_kwargs: object) -> IngestRunResult:
        """Time out the way a contended id-index lock does mid-ingest."""
        msg = "id-index lock busy"
        raise VaultLockTimeoutError(msg)

    monkeypatch.setattr(pipeline_mod, "run_ingest", _index_lock_times_out)

    result = _invoke_ingest(vault, src)

    assert isinstance(result.exception, VaultLockTimeoutError)
    assert CONTENT_MUTATION_BUSY_REASON not in _flat(result.output)


def test_a_missing_vault_is_not_scaffolded_by_the_content_lock(
    tmp_path: Path,
) -> None:
    """A mistyped ``--vault`` still exits "Vault unavailable" and gains no lock dir.

    :func:`creek._fslock.vault_lock` creates its lock's parent directory, so
    taking the hold before the vault validates would plant
    ``00-Creek-Meta/locks/`` in whatever path the operator typed.
    """
    vault = tmp_path / "mistyped-vault"
    src = _seed_source(tmp_path)

    result = _invoke_ingest(vault, src)

    assert result.exit_code == 1, result.output
    assert "Vault unavailable" in _flat(result.output)
    assert not (vault / _LOCKS_RELPATH).exists()


def test_a_vault_missing_its_fragments_dir_is_not_scaffolded_either(
    tmp_path: Path,
) -> None:
    """A directory that is not a vault is refused before the lock, too.

    The consent gate already creates ``00-Creek-Meta/Processing-Log`` there,
    so a check that the vault root merely exists would not be enough.
    """
    vault = tmp_path / "not-a-vault"
    vault.mkdir()
    src = _seed_source(tmp_path)

    result = _invoke_ingest(vault, src)

    assert result.exit_code == 1, result.output
    assert "Vault unavailable" in _flat(result.output)
    assert not (vault / _LOCKS_RELPATH).exists()


# --------------------------------------------------------------------------- #
# creek sync Tier A
# --------------------------------------------------------------------------- #


def _sync_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed a vault with one journal source and point ``creek sync`` at it."""
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/Processing-Log", "01-Fragments/Journal"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    journal = tmp_path / "src" / "personal" / "journal"
    journal.mkdir(parents=True)
    (journal / "day.md").write_text(
        "---\ndate: 2026-06-26\n---\nA journal entry today.\n", encoding="utf-8"
    )
    config = CreekConfig(
        vault_path=vault,
        source_drive=tmp_path / "src",
        sync=SyncConfig(sources={"journal": True}),
    )
    monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: config)
    return vault


def _invoke_sync(vault: Path) -> Result:
    """Run one ``creek sync --tier A`` tick over *vault*."""
    return runner.invoke(app, ["sync", "--tier", "A", "--vault", str(vault)])


def test_sync_tier_a_ingests_inside_the_lock_and_classifies_outside_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ingest holds the lock; it is released before the classify pass.

    ``_sync_classify`` takes the same non-reentrant lock itself, so a hold
    still open when it starts would deadlock the tick for the full timeout.
    """
    vault = _sync_vault(tmp_path, monkeypatch)
    ingest_held: list[bool] = []
    classify_held: list[bool] = []
    real = pipeline_mod.run_ingest

    def _probed(**kwargs: Any) -> IngestRunResult:
        """Probe the content lock, then run the real pipeline."""
        ingest_held.append(_content_lock_is_held(vault))
        return real(**kwargs)

    def _classify_probe(vault_path: Path, _config: CreekConfig, _method: str) -> None:
        """Record whether the lock is still held when classify starts."""
        classify_held.append(_content_lock_is_held(vault_path))

    monkeypatch.setattr(pipeline_mod, "run_ingest", _probed)
    monkeypatch.setattr("creek.cli._sync_classify", _classify_probe)

    result = _invoke_sync(vault)

    assert result.exit_code == 0, result.output
    assert ingest_held == [True]
    assert classify_held == [False]


def test_a_content_busy_sync_records_the_source_and_still_writes_last_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy vault is a per-source failure, never an exit that eats the record.

    ``typer.Exit`` out of the Tier-A ingest would skip ``_write_sync_state``
    and leave no trace that the scheduled tick ran at all (#1372).
    """
    vault = _sync_vault(tmp_path, monkeypatch)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    monkeypatch.setattr("creek.cli._sync_classify", lambda _v, _c, _m: None)

    with _content_lock_held_elsewhere(vault):
        result = _invoke_sync(vault)

    assert result.exit_code == 0, result.output
    state = json.loads(_sync_state_path(vault).read_text(encoding="utf-8"))
    journal = state["sources"]["journal"]
    assert journal["ingested"] == 0
    assert journal["failures"] == [f"skipped: {CONTENT_MUTATION_BUSY_REASON}"]
    assert _fragment_digests(vault) == {}


# --------------------------------------------------------------------------- #
# creek sync: a busy classify / link pass is a skipped step, not a lost tick
# (#2087)
# --------------------------------------------------------------------------- #

_CLASSIFY_SKIPPED = f"classify skipped: {CONTENT_MUTATION_BUSY_REASON}"
"""The ``skipped_steps`` line a busy classify pass records."""

_LINK_SKIPPED = f"link skipped: {CONTENT_MUTATION_BUSY_REASON}"
"""The ``skipped_steps`` line a busy Tier-B link pass records."""
_SKIP_LOG_CLASSIFY = "sync.classify.skip reason=content-busy"
"""The scheduler-log line an operator greps for when sync classify is skipped."""
_SKIP_LOG_LINK = "sync.link.skip reason=content-busy"
"""The scheduler-log line an operator greps for when sync link is skipped."""


def _read_sync_state(vault: Path) -> dict[str, Any]:
    """Return the parsed ``last-run.json`` the tick wrote under *vault*."""
    loaded: dict[str, Any] = json.loads(
        _sync_state_path(vault).read_text(encoding="utf-8")
    )
    return loaded


def test_a_content_busy_sync_classify_pass_is_skipped_and_last_run_still_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Tier-A tick whose rules-classify pass meets contention still records.

    ``_sync_classify`` is deliberately not stubbed: the real
    :func:`~creek.classify.classify_engine.run_classify` takes the content
    lock, and before #2087 its timeout escaped the tick as a traceback that
    skipped ``_write_sync_state`` — losing the per-source busy record the
    ingest step had just made.
    """
    vault = _sync_vault(tmp_path, monkeypatch)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with (
        _content_lock_held_elsewhere(vault),
        caplog.at_level(logging.INFO, logger="creek.cli"),
    ):
        result = _invoke_sync(vault)

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert f"[sync] {_CLASSIFY_SKIPPED}" in _flat(result.output)
    assert _SKIP_LOG_CLASSIFY in caplog.messages
    state = _read_sync_state(vault)
    assert state["sources"]["journal"]["failures"] == [
        f"skipped: {CONTENT_MUTATION_BUSY_REASON}"
    ]
    assert state["skipped_steps"] == [_CLASSIFY_SKIPPED]
    assert _fragment_digests(vault) == {}


def test_a_clean_sync_tick_writes_an_empty_skipped_steps_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``skipped_steps`` is rewritten every tick, so a clean one clears it."""
    vault = _sync_vault(tmp_path, monkeypatch)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    with _content_lock_held_elsewhere(vault):
        busy = _invoke_sync(vault)
    assert busy.exit_code == 0, busy.output

    result = _invoke_sync(vault)

    assert result.exit_code == 0, result.output
    assert _read_sync_state(vault)["skipped_steps"] == []


def _invoke_tier_b(vault: Path) -> Result:
    """Run one confirmed ``creek sync --tier B`` tick over *vault*."""
    return runner.invoke(app, ["sync", "--tier", "B", "--yes", "--vault", str(vault)])


def test_a_busy_tier_b_classify_skips_link_and_index_and_still_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier B stops after a busy classify, records why, and exits 0.

    Link would almost certainly meet the same holder and spend a second
    bounded wait, so the tick ends there; the next one re-derives the work.
    """
    vault = _sync_vault(tmp_path, monkeypatch)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    later_steps: list[str] = []

    def _link_spy(_vault: Path, _config: CreekConfig) -> None:
        """Record that link ran."""
        later_steps.append("link")

    def _index_spy(_vault: Path) -> None:
        """Record that index ran."""
        later_steps.append("index")

    monkeypatch.setattr("creek.cli._sync_link", _link_spy)
    monkeypatch.setattr("creek.cli._sync_index", _index_spy)

    with _content_lock_held_elsewhere(vault):
        result = _invoke_tier_b(vault)

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert later_steps == []
    state = _read_sync_state(vault)
    assert state["tier"] == "B"
    assert state["skipped_steps"] == [_CLASSIFY_SKIPPED]


def test_a_busy_tier_b_link_is_recorded_and_index_still_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A busy link pass is a recorded skip; the index rebuild is not lost.

    Classify is stubbed to a clean pass so the contention lands on the real
    :func:`~creek.link.link_engine.run_link`, which takes the lock before it
    loads any embedding model.
    """
    vault = _sync_vault(tmp_path, monkeypatch)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    indexed: list[Path] = []

    def _classify_clean(**_kwargs: object) -> None:
        """Stand in for a classify pass that met no contention."""

    def _index_spy(vault_path: Path) -> None:
        """Record that index ran."""
        indexed.append(vault_path)

    monkeypatch.setattr("creek.classify.classify_engine.run_classify", _classify_clean)
    monkeypatch.setattr("creek.cli._sync_index", _index_spy)

    with (
        _content_lock_held_elsewhere(vault),
        caplog.at_level(logging.INFO, logger="creek.cli"),
    ):
        result = _invoke_tier_b(vault)

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert f"[sync] {_LINK_SKIPPED}" in _flat(result.output)
    assert _SKIP_LOG_LINK in caplog.messages
    assert indexed == [vault]
    assert _read_sync_state(vault)["skipped_steps"] == [_LINK_SKIPPED]


def test_a_timeout_under_the_sync_classify_hold_is_not_recorded_as_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only acquisition is a skip; an inner timeout fails the tick as itself.

    A timeout under the hold can follow writes the pass already made, so
    recording ``classify skipped`` — "nothing ran" — would be false.
    """
    vault = _sync_vault(tmp_path, monkeypatch)
    inner = VaultLockTimeoutError("id-index lock busy")

    def _inner_times_out(**_kwargs: object) -> object:
        """Time out the way a contended inner lock does mid-pass."""
        raise inner

    monkeypatch.setattr(
        "creek.classify.classify_engine._run_classify_with_content_lock_held",
        _inner_times_out,
    )

    result = _invoke_sync(vault)

    assert result.exception is inner
    assert _CLASSIFY_SKIPPED not in _flat(result.output)
