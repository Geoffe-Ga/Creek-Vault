"""Every Discord ingest path writes inside the content-mutation lock (#2086).

All three Discord paths — bot capture (``ingest_capture_dir``), the Data
Package helper (``run_discord_data_package``) and the user-token exporter
(``ExporterConnector._ingest``) — write ``01-Fragments`` through
:func:`creek.ingest.discord.write_fragments`. That one function therefore
takes :func:`~creek.vault.mutations.hold_content_mutation` around its writes,
so no caller can forget to, and so none of them may already hold it.

``ingest_capture_dir`` also runs behind the CrawDad bot with no Typer
context, so contention there is :class:`ContentMutationBusyError`, never
``typer.Exit``. The two CLI paths turn it into the one-line busy message and
exit 1, and a busy exporter run leaves its ``--after`` cursor where it was so
the next run re-exports the same window.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.config import CreekConfig, DiscordModeToggle, DiscordSourceConfig
from creek.ingest.discord import ingest_capture_dir, run_discord_data_package
from creek.vault.mutations import (
    CONTENT_MUTATION_BUSY_REASON,
    ContentMutationBusyError,
)
from creek.vault.writer import VaultWriter
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

_TOKEN_ENV_VAR = "DISCORD_USER_TOKEN"
"""Where the exporter connector reads its token from."""

_BUSY_LINE = (
    f"{CONTENT_MUTATION_BUSY_REASON}: another Creek writer holds this vault; "
    "retry once it finishes."
)
"""The whole line the CLI prints for contention, as the operator reads it."""

_MESSAGES: list[dict[str, object]] = [
    {
        "id": "m1",
        "timestamp": "2026-06-26T10:00:00+00:00",
        "content": "a reflection that is worth keeping in the vault",
        "author": {"name": "Ada"},
    },
]
"""One substantial message, enough for one fragment."""


def _flat(text: str) -> str:
    """Collapse Rich's soft wrapping so a phrase can be matched whole."""
    return " ".join(text.split())


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault folders the writer needs."""
    vault = tmp_path / "vault"
    for sub in ("00-Creek-Meta/Processing-Log", "01-Fragments/Messages"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


def _write_data_package(root: Path) -> Path:
    """Write a one-channel Discord Data Package under *root*; return it."""
    package = root / "discord-export"
    channel = package / "messages" / "general"
    channel.mkdir(parents=True, exist_ok=True)
    (channel / "channel.json").write_text(
        json.dumps({"id": "general", "name": "general"}), encoding="utf-8"
    )
    (channel / "messages.json").write_text(json.dumps(_MESSAGES), encoding="utf-8")
    return package


def _write_capture(vault: Path) -> Path:
    """Append one bot-capture line under *vault*; return the capture root."""
    capture = vault / "discord-capture"
    day = capture / "general" / "2026-06-26.jsonl"
    day.parent.mkdir(parents=True)
    day.write_text(json.dumps(_MESSAGES[0]) + "\n", encoding="utf-8")
    return capture


def _probe_writes(monkeypatch: pytest.MonkeyPatch, vault: Path) -> list[bool]:
    """Record whether the content lock is held at each fragment write."""
    held: list[bool] = []
    real = VaultWriter.write_fragment

    def _probed(self: VaultWriter, *args: Any, **kwargs: Any) -> Path:
        """Probe the content lock, then write for real."""
        held.append(_content_lock_is_held(vault))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(VaultWriter, "write_fragment", _probed)
    return held


class _StagingExporter:
    """Stands in for the exporter binary: stages a package, no network."""

    def __init__(self, binary: str, *, timeout_seconds: int = 600) -> None:
        """Accept the real runner's constructor arguments."""
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    def run(self, *, argv: list[str], token: str, out_dir: Path) -> None:
        """Stage the sample Data Package where the connector reads it."""
        assert argv
        assert token
        channel = out_dir / "messages" / "general"
        channel.mkdir(parents=True, exist_ok=True)
        (channel / "channel.json").write_text(
            json.dumps({"id": "general", "name": "general"}), encoding="utf-8"
        )
        (channel / "messages.json").write_text(json.dumps(_MESSAGES), encoding="utf-8")


def _cursor_path(vault: Path) -> Path:
    """Where the exporter connector persists its ``--after`` cursor."""
    return vault / "00-Creek-Meta" / "State" / "discord" / "cursor.json"


def _invoke_exporter(monkeypatch: pytest.MonkeyPatch, vault: Path) -> Result:
    """Run ``creek discord --mode exporter`` against a staging fake."""
    monkeypatch.setenv(_TOKEN_ENV_VAR, "synthetic-token")
    monkeypatch.setattr(
        "creek.ingest.discord_dispatch.SubprocessExporterRunner", _StagingExporter
    )
    config = CreekConfig(
        discord=DiscordSourceConfig(
            exporter=DiscordModeToggle(enabled=True),
            exporter_binary="/abs/exporter",
        )
    )
    monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: config)
    return runner.invoke(app, ["discord", "--mode", "exporter", "--vault", str(vault)])


def _invoke_data_package(vault: Path, package: Path) -> Result:
    """Run ``creek discord --mode data_package --package <package>``."""
    return runner.invoke(
        app,
        [
            "discord",
            "--mode",
            "data_package",
            "--package",
            str(package),
            "--vault",
            str(vault),
        ],
    )


def test_bot_capture_writes_every_fragment_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ingest_capture_dir`` writes under the hold and lets it go after."""
    vault = _make_vault(tmp_path)
    held = _probe_writes(monkeypatch, vault)

    written = ingest_capture_dir(_write_capture(vault), vault)

    assert written == 1
    assert held == [True]
    assert _content_lock_is_held(vault) is False


def test_a_busy_bot_capture_raises_the_typed_error_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot has no Typer context, so busy is the typed error, not an exit."""
    vault = _make_vault(tmp_path)
    capture = _write_capture(vault)
    held = _probe_writes(monkeypatch, vault)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with (
        _content_lock_held_elsewhere(vault),
        pytest.raises(ContentMutationBusyError),
    ):
        ingest_capture_dir(capture, vault)

    assert held == []
    assert _fragment_digests(vault) == {}


def test_the_data_package_helper_writes_inside_the_content_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_discord_data_package`` writes under the hold and lets it go after."""
    vault = _make_vault(tmp_path)
    held = _probe_writes(monkeypatch, vault)

    new_count = run_discord_data_package(vault, _write_data_package(tmp_path))

    assert new_count == 1
    assert held == [True]
    assert _content_lock_is_held(vault) is False


def test_a_data_package_into_a_non_vault_gains_no_lock_directory(
    tmp_path: Path,
) -> None:
    """``write_fragments`` refuses a non-vault before the lock scaffolds it.

    Acquiring the content lock creates ``00-Creek-Meta/locks/``, so the
    ``VaultWriter`` — whose constructor checks the vault layout — is built
    before the hold, and a mistyped ``--vault`` stays exactly as it was.
    """
    vault = tmp_path / "not-a-vault"
    vault.mkdir()
    package = _write_data_package(tmp_path)

    with pytest.raises(FileNotFoundError, match="Required vault directory missing"):
        run_discord_data_package(vault, package)

    assert not (vault / "00-Creek-Meta" / "locks").exists()
    assert list(vault.iterdir()) == []


def test_a_busy_data_package_cli_run_is_one_line_and_exit_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek discord --mode data_package`` reports contention legibly."""
    vault = _make_vault(tmp_path)
    package = _write_data_package(tmp_path)
    held = _probe_writes(monkeypatch, vault)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        result = _invoke_data_package(vault, package)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert held == []
    assert _fragment_digests(vault) == {}


def test_the_exporter_writes_inside_the_content_lock_then_advances_its_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``creek discord --mode exporter`` writes under the hold, then saves."""
    vault = _make_vault(tmp_path)
    held = _probe_writes(monkeypatch, vault)

    result = _invoke_exporter(monkeypatch, vault)

    assert result.exit_code == 0, result.output
    assert held == [True]
    assert _content_lock_is_held(vault) is False
    assert _cursor_path(vault).exists()


def test_a_busy_exporter_run_exits_1_and_leaves_its_cursor_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy run must not advance the cursor past messages it never wrote."""
    vault = _make_vault(tmp_path)
    held = _probe_writes(monkeypatch, vault)
    _shorten_lock_wait(monkeypatch, "creek.vault.mutations")

    with _content_lock_held_elsewhere(vault):
        result = _invoke_exporter(monkeypatch, vault)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert _BUSY_LINE in _flat(result.output)
    assert "Traceback" not in result.output
    assert held == []
    assert not _cursor_path(vault).exists()
    assert _fragment_digests(vault) == {}
