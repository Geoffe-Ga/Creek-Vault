"""``creek purge`` joins the vault content-mutation boundary (#2086).

Every purge subcommand deletes or rewrites ``01-Fragments`` — the snapshot
``creek classify`` and ``creek link`` load and rewrite under
:func:`creek.vault.mutations.content_mutation_lock_path`. A purge that ran
outside that lock could delete a fragment between a classify pass's load and
its write, and the pass would then recreate it. Each command therefore runs
its engine call inside :func:`~creek.vault.mutations.hold_content_mutation`
and says so legibly when another writer holds the vault first.

The hold is narrow on purpose. The confirmation prompt, the ``purge vault``
path phrase and the ``purge source`` count preview all wait on a human, so
none of them runs under the lock: an operator reading a prompt must not stall
every other writer for as long as they read it.

:class:`~creek.purge.engine.PurgeEngine` itself stays lock-free, because the
``/v1`` journal withdraw already holds the lock when it calls the engine and
the lock is not reentrant.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from creek._fslock import DEFAULT_LOCK_TIMEOUT_SECONDS
from creek.cli import app
from creek.models import Fragment, FragmentSource, PrivacyTier, SourcePlatform
from creek.purge.engine import VAULT_PURGE_CONFIRMATION, PurgeEngine
from creek.vault.mutations import CONTENT_MUTATION_BUSY_REASON, hold_content_mutation
from tests.helpers import write_fragment_file
from tests.test_mcp_content_mutation import (
    _content_lock_held_elsewhere,
    _fragment_digests,
    _shorten_lock_wait,
)
from tests.test_mcp_upload import _content_lock_is_held

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result

runner = CliRunner()

_FRAGMENT_ID = "frag-purge-lock-2086"
"""The one fragment every purge here targets."""

_SOURCE_FILE = "notes/purge-lock.md"
"""The ``source.original_file`` the ``--source-path`` variant matches."""

_BUSY_LINE = (
    f"{CONTENT_MUTATION_BUSY_REASON}: another Creek writer holds this vault; "
    "retry once it finishes."
)
"""The whole line the CLI prints for contention, as the operator reads it."""

_PURGES: dict[str, tuple[str, list[str]]] = {
    "fragment": ("purge_fragment", ["purge", "fragment", _FRAGMENT_ID, "--yes"]),
    "source": ("purge_source", ["purge", "source", "journal", "--yes"]),
    "source-path": (
        "purge_source_path",
        ["purge", "source", "--source-path", _SOURCE_FILE, "--yes"],
    ),
    "classifications": (
        "purge_classifications",
        ["purge", "classifications", "--yes"],
    ),
    "daterange": (
        "purge_daterange",
        ["purge", "daterange", "2026-01-01", "2026-12-31", "--yes"],
    ),
    "vault": (
        "purge_vault",
        [
            "purge",
            "vault",
            "--force-non-interactive",
            "--confirm-text",
            VAULT_PURGE_CONFIRMATION,
        ],
    ),
}
"""Each purge subcommand: the engine method it reaches, and its argv."""

_EACH_PURGE = pytest.mark.parametrize("purge", sorted(_PURGES))


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so a phrase can be matched whole."""
    return " ".join(text.split())


def _seed_vault(tmp_path: Path) -> Path:
    """Write a Creek vault holding one OPEN journal fragment; return its root."""
    vault = tmp_path / "vault"
    fragment = Fragment(
        id=_FRAGMENT_ID,
        title="Purge lock",
        created=datetime(2026, 5, 1, tzinfo=UTC),
        source=FragmentSource(
            platform=SourcePlatform.JOURNAL, original_file=_SOURCE_FILE
        ),
        privacy_tier=PrivacyTier.OPEN,
    )
    write_fragment_file(vault=vault, fragment=fragment, body="synthetic body")
    # The marker ``purge vault`` requires before it treats a tree as a vault.
    meta = vault / "00-Creek-Meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "creek_config.yaml").write_text("# minimal marker\n", encoding="utf-8")
    return vault


def _probe_engine(
    monkeypatch: pytest.MonkeyPatch, vault: Path, method: str
) -> list[bool]:
    """Record whether the content lock is held each time *method* runs.

    The real engine method still runs after the probe, so a passing purge is
    the real deletion, not a stub's.
    """
    held: list[bool] = []
    real = getattr(PurgeEngine, method)

    def _probed(self: PurgeEngine, *args: Any, **kwargs: Any) -> Any:
        """Probe the content lock, then run the real engine method."""
        held.append(_content_lock_is_held(vault))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PurgeEngine, method, _probed)
    return held


def _invoke(argv: list[str], vault: Path) -> Result:
    """Run one ``creek purge`` subcommand against *vault*."""
    return runner.invoke(app, [*argv, "--vault", str(vault)])


@_EACH_PURGE
def test_each_purge_runs_its_engine_call_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purge: str,
) -> None:
    """The engine runs under the hold, and the hold is let go after."""
    vault = _seed_vault(tmp_path)
    method, argv = _PURGES[purge]
    held = _probe_engine(monkeypatch, vault, method)

    result = _invoke(argv, vault)

    assert result.exit_code == 0, result.output
    assert held == [True]
    assert _content_lock_is_held(vault) is False


def test_creek_purge_fragment_deletes_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dossier's RED: the real deletion lands, and only under the hold."""
    vault = _seed_vault(tmp_path)
    held = _probe_engine(monkeypatch, vault, "purge_fragment")

    result = _invoke(_PURGES["fragment"][1], vault)

    assert result.exit_code == 0, result.output
    assert held == [True]
    assert _content_lock_is_held(vault) is False
    assert _fragment_digests(vault) == {}


@_EACH_PURGE
def test_a_dry_run_purge_reads_inside_the_content_lock_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purge: str,
) -> None:
    """A preview reads the same snapshot a real purge would act on."""
    vault = _seed_vault(tmp_path)
    before = _fragment_digests(vault)
    method, argv = _PURGES[purge]
    held = _probe_engine(monkeypatch, vault, method)

    result = _invoke([*argv, "--dry-run"], vault)

    assert result.exit_code == 0, result.output
    assert held == [True]
    assert _fragment_digests(vault) == before


@_EACH_PURGE
def test_a_busy_vault_is_one_line_and_exit_1_and_purges_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purge: str,
) -> None:
    """Contention is the busy line, never a traceback, and the engine never runs."""
    vault = _seed_vault(tmp_path)
    before = _fragment_digests(vault)
    method, argv = _PURGES[purge]
    held = _probe_engine(monkeypatch, vault, method)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        result = _invoke(argv, vault)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert held == []
    assert _fragment_digests(vault) == before


@pytest.mark.parametrize("purge", ["fragment", "classifications", "daterange"])
def test_the_confirmation_prompt_runs_outside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purge: str,
) -> None:
    """An operator reading ``Continue?`` must not stall every other writer."""
    vault = _seed_vault(tmp_path)
    held_at_prompt: list[bool] = []

    def _answer_yes(_message: str, *, assume_yes: bool) -> bool:
        """Probe the lock where the human would be reading the prompt."""
        assert not assume_yes
        held_at_prompt.append(_content_lock_is_held(vault))
        return True

    monkeypatch.setattr("creek.cli._confirm", _answer_yes)
    argv = [arg for arg in _PURGES[purge][1] if arg != "--yes"]

    result = _invoke(argv, vault)

    assert result.exit_code == 0, result.output
    assert held_at_prompt == [False]


def test_purge_source_counts_and_prompts_outside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count preview and its prompt both precede the hold."""
    vault = _seed_vault(tmp_path)
    held_at_count = _probe_engine(monkeypatch, vault, "count_fragments_from_source")
    held_at_prompt: list[bool] = []

    def _answer_yes(_message: str, *, assume_yes: bool) -> bool:
        """Probe the lock where the human would be reading the prompt."""
        assert not assume_yes
        held_at_prompt.append(_content_lock_is_held(vault))
        return True

    monkeypatch.setattr("creek.cli._confirm", _answer_yes)

    result = _invoke(["purge", "source", "journal"], vault)

    assert result.exit_code == 0, result.output
    assert held_at_count == [False]
    assert held_at_prompt == [False]


def test_purge_vault_resolves_its_phrase_outside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path phrase is a human prompt on a TTY, so it precedes the hold."""
    vault = _seed_vault(tmp_path)
    held_at_phrase: list[bool] = []

    def _phrase(**_kwargs: object) -> str:
        """Probe the lock where the operator would be typing the vault path."""
        held_at_phrase.append(_content_lock_is_held(vault))
        return VAULT_PURGE_CONFIRMATION

    monkeypatch.setattr("creek.cli._resolve_purge_phrase", _phrase)

    result = _invoke(_PURGES["vault"][1], vault)

    assert result.exit_code == 0, result.output
    assert held_at_phrase == [False]


_BAD_VAULTS: dict[str, bool] = {"missing": False, "not-a-vault": True}
"""Each kind of mistyped ``--vault``, and whether the directory exists."""


@_EACH_PURGE
@pytest.mark.parametrize("mode", ["dry-run", "confirmed"])
@pytest.mark.parametrize("bad_vault", sorted(_BAD_VAULTS))
def test_purge_mistyped_vault_gains_no_lock_or_audit(
    tmp_path: Path,
    purge: str,
    mode: str,
    bad_vault: str,
) -> None:
    """A mistyped ``--vault`` is refused before the hold scaffolds anything.

    Acquiring the content lock creates ``00-Creek-Meta/locks/``, and the
    engine's audit log ``00-Creek-Meta/audit/``, so the purge target must be
    validated before either: a typo must not leave Creek folders behind in
    whatever directory it happened to name (#2086, #2098).
    """
    target = tmp_path / bad_vault
    if _BAD_VAULTS[bad_vault]:
        target.mkdir()
    argv = _PURGES[purge][1]

    result = _invoke([*argv, "--dry-run"] if mode == "dry-run" else argv, target)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "does not appear to be a Creek vault" in _flat(result.output)
    assert "Traceback" not in result.output
    assert not (target / "00-Creek-Meta" / "locks").exists()
    assert not (target / "00-Creek-Meta" / "audit").exists()
    assert target.exists() is _BAD_VAULTS[bad_vault]
    if target.exists():
        assert list(target.iterdir()) == []


@pytest.mark.parametrize("mode", ["dry-run", "confirmed"])
def test_purge_vault_on_an_unmarked_vault_gains_no_lock(
    tmp_path: Path,
    mode: str,
) -> None:
    """``purge vault`` checks its ``creek_config.yaml`` marker before the hold.

    A directory with both vault folders but no marker passes the layout check
    every purge shares, yet ``purge vault`` refuses it (GAP-003). That refusal
    must also come before the lock, which would otherwise leave
    ``00-Creek-Meta/locks/`` behind in a tree the command then declines to
    touch (#2086).
    """
    target = tmp_path / "unmarked"
    for name in ("00-Creek-Meta", "01-Fragments"):
        (target / name).mkdir(parents=True)
    argv = _PURGES["vault"][1]

    result = _invoke([*argv, "--dry-run"] if mode == "dry-run" else argv, target)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "does not appear to be a Creek vault" in _flat(result.output)
    assert not (target / "00-Creek-Meta" / "locks").exists()
    assert not (target / "00-Creek-Meta" / "audit").exists()
    assert sorted(path.name for path in target.rglob("*")) == [
        "00-Creek-Meta",
        "01-Fragments",
    ]


def test_purge_engine_under_an_existing_hold_does_not_nest_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PurgeEngine`` stays lock-free, as the ``/v1`` withdraw relies on.

    The withdraw holds the content lock and then calls the engine. Were the
    engine to take the non-reentrant lock itself, that call would wait out the
    bound and fail; it must instead finish at once and delete the fragment.
    """
    vault = _seed_vault(tmp_path)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")
    started = time.monotonic()

    with hold_content_mutation(vault):
        result = PurgeEngine(vault).purge_fragment(_FRAGMENT_ID)

    assert time.monotonic() - started < DEFAULT_LOCK_TIMEOUT_SECONDS
    assert result.affected_fragment_ids == [_FRAGMENT_ID]
    assert _fragment_digests(vault) == {}
