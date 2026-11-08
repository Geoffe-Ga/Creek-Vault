"""``creek ingest``'s vault migrations join the content boundary (#2086).

``--pin-source-ids`` rewrites the ingest ledger and fragment frontmatter,
``--refresh-dates`` rewrites frontmatter, and ``--refresh-ai-chat`` writes two
fragments and unlinks a third. Each is a ``01-Fragments`` writer that
``creek classify`` or ``creek link`` could otherwise load half-way through, so
each runs inside :func:`~creek.vault.mutations.hold_content_mutation` — after
the existing ``Vault path does not exist`` check, so a mistyped ``--vault``
still gains no ``00-Creek-Meta/locks`` directory — and contention is the
one-line busy message and exit 1 that ``creek ingest`` already prints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.ingest import pin_ids as pin_ids_mod
from creek.ingest import refresh as refresh_mod
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from creek.vault.mutations import CONTENT_MUTATION_BUSY_REASON
from tests.helpers import write_fragment_file
from tests.test_mcp_content_mutation import (
    _content_lock_held_elsewhere,
    _fragment_digests,
    _shorten_lock_wait,
)
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from typer.testing import Result

runner = CliRunner()

_BUSY_LINE = (
    f"{CONTENT_MUTATION_BUSY_REASON}: another Creek writer holds this vault; "
    "retry once it finishes."
)
"""The whole line the CLI prints for contention, as the operator reads it."""

_MIGRATIONS: dict[str, tuple[ModuleType, str, list[str]]] = {
    "pin-source-ids": (pin_ids_mod, "pin_source_ids", ["--pin-source-ids"]),
    "pin-source-ids-dry-run": (
        pin_ids_mod,
        "pin_source_ids",
        ["--pin-source-ids", "--dry-run"],
    ),
    "refresh-dates": (refresh_mod, "refresh_authored_dates", ["--refresh-dates"]),
    "refresh-ai-chat": (
        refresh_mod,
        "resplit_merged_ai_chat",
        ["--refresh-ai-chat"],
    ),
}
"""Each migration: the module and function it reaches, and its flags."""

_EACH_MIGRATION = pytest.mark.parametrize("migration", sorted(_MIGRATIONS))


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so a phrase can be matched whole."""
    return " ".join(text.split())


def _seed_vault(tmp_path: Path) -> Path:
    """Write a vault holding one OPEN note fragment; return its root."""
    vault = tmp_path / "vault"
    (vault / "00-Creek-Meta" / "Processing-Log").mkdir(parents=True)
    fragment = Fragment(
        id="frag-migration-lock-2086",
        title="Migration lock",
        source=FragmentSource(platform=SourcePlatform.JOURNAL),
        privacy_tier=PrivacyTier.OPEN,
    )
    write_fragment_file(vault=vault, fragment=fragment, body="synthetic body")
    return vault


def _probe(monkeypatch: pytest.MonkeyPatch, vault: Path, migration: str) -> list[bool]:
    """Record whether the content lock is held each time the migration runs."""
    module, name, _flags = _MIGRATIONS[migration]
    held: list[bool] = []
    real = getattr(module, name)

    def _probed(*args: Any, **kwargs: Any) -> Any:
        """Probe the content lock, then run the real migration."""
        held.append(_content_lock_is_held(vault))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, _probed)
    return held


def _invoke(migration: str, vault: Path) -> Result:
    """Run ``creek ingest <migration flags> --vault <vault>``."""
    flags = _MIGRATIONS[migration][2]
    return runner.invoke(app, ["ingest", *flags, "--vault", str(vault)])


@_EACH_MIGRATION
def test_each_migration_runs_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration: str,
) -> None:
    """The migration's write call is under the hold, and the hold is let go."""
    vault = _seed_vault(tmp_path)
    held = _probe(monkeypatch, vault, migration)

    result = _invoke(migration, vault)

    assert result.exit_code == 0, result.output
    assert held == [True]
    assert _content_lock_is_held(vault) is False


@_EACH_MIGRATION
def test_a_busy_vault_is_one_line_and_exit_1_and_migrates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration: str,
) -> None:
    """Contention is the busy line, never a traceback, and nothing runs."""
    vault = _seed_vault(tmp_path)
    before = _fragment_digests(vault)
    held = _probe(monkeypatch, vault, migration)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        result = _invoke(migration, vault)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert held == []
    assert _fragment_digests(vault) == before


@_EACH_MIGRATION
def test_a_mistyped_vault_gains_no_lock_directory(
    tmp_path: Path,
    migration: str,
) -> None:
    """The existence check runs before the lock, which would scaffold the path."""
    missing = tmp_path / "no-such-vault"

    result = _invoke(migration, missing)

    assert result.exit_code == 1, result.output
    assert "Vault path does not exist" in _flat(result.output)
    assert not missing.exists()
