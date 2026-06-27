"""Discord ingest-mode dispatch — skeleton stubs (#685).

Resolves a configured Discord mode (``data_package`` | ``exporter`` |
``bot_capture``) to a handler that echoes the pull-then-ingest plan. **No
network, no exporter binary, no bot capture** — those are issues #686/#687/#688.

A bot cannot read DMs (Discord API limit), so the three modes differ by coverage
and compliance (SPEC R5, decision #3). The ``exporter`` mode is OFF by default
and prints a Terms-of-Service caveat on enable. The Discord token (user or bot)
lives in the **environment only** — never in config, never echoed or logged.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from rich.console import Console

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import DiscordSourceConfig

console = Console()
logger = logging.getLogger(__name__)

_TOKEN_ENV_VAR = "DISCORD_USER_TOKEN"
_EPOCH_CURSOR = "1970-01-01T00:00:00+00:00"


def announce_exporter_caveat() -> None:
    """Print the user-token exporter's Terms-of-Service caveat (#685/#686).

    Short lines keep each phrase intact regardless of terminal width.
    """
    console.print("[yellow][discord] DM exporter caveat:[/yellow]")
    console.print("[yellow]- A user token drives your account.[/yellow]")
    console.print("[yellow]- The Discord Terms of Service prohibit this.[/yellow]")
    console.print("[yellow]- The real stake is account suspension.[/yellow]")
    console.print(
        "[yellow]- Use it read-only, for your own DMs, at a low rate.[/yellow]",
    )


class DiscordMode(StrEnum):
    """The three explicit Discord ingest modes."""

    DATA_PACKAGE = "data_package"
    EXPORTER = "exporter"
    BOT_CAPTURE = "bot_capture"


class ModeDisabledError(RuntimeError):
    """Raised when a Discord mode is selected but disabled in config."""


class ExporterDisabledError(ModeDisabledError):
    """Raised specifically when the (opt-in) exporter mode is disabled."""


class DiscordHandler:
    """Base stub handler: echoes a plan, performs no I/O (#685)."""

    mode: DiscordMode

    def plan(self) -> list[str]:
        """Return the ordered plan lines this mode *would* perform."""
        raise NotImplementedError

    def announce(self) -> None:
        """Print any pre-run notice (e.g. the exporter ToS caveat)."""


class DataPackageHandler(DiscordHandler):
    """Stub for the clean Data Package import path (#685)."""

    mode = DiscordMode.DATA_PACKAGE

    def plan(self) -> list[str]:
        """Echo the Data Package ingest plan."""
        return [
            "[discord] mode=data_package (clean fallback: a downloaded export)",
            "[discord] would ingest discord-export/ via the DiscordIngestor",
            "[discord] no network performed (skeleton stub)",
        ]


class BotCaptureHandler(DiscordHandler):
    """Stub for the bot message-capture path (servers/channels, #685)."""

    mode = DiscordMode.BOT_CAPTURE

    def plan(self) -> list[str]:
        """Echo the bot-capture plan."""
        return [
            "[discord] mode=bot_capture (clean: servers/channels the bot is in)",
            "[discord] would append discord-capture/<channel>/<date>.jsonl "
            "via on_message",
            "[discord] would backfill via channel.history(); Tier-A ingest consumes it",
            "[discord] no network performed (skeleton stub)",
        ]


class ExporterHandler(DiscordHandler):
    """Stub for the opt-in user-token exporter path (DMs, #685)."""

    mode = DiscordMode.EXPORTER

    def announce(self) -> None:
        """Print the Terms-of-Service caveat before any (future) export."""
        announce_exporter_caveat()

    def plan(self) -> list[str]:
        """Echo the exporter plan."""
        return [
            "[discord] mode=exporter (opt-in: user-token export incl. DMs)",
            "[discord] would run the exporter --after <cursor> into discord-export/",
            "[discord] then ingest via the DiscordIngestor",
            "[discord] no network performed (skeleton stub)",
        ]


_HANDLERS: dict[DiscordMode, type[DiscordHandler]] = {
    DiscordMode.DATA_PACKAGE: DataPackageHandler,
    DiscordMode.EXPORTER: ExporterHandler,
    DiscordMode.BOT_CAPTURE: BotCaptureHandler,
}


def resolve_discord_handler(
    mode: DiscordMode,
    config: DiscordSourceConfig,
) -> DiscordHandler:
    """Return the stub handler for *mode*, or raise if it is disabled (#685).

    Args:
        mode: The selected Discord ingest mode.
        config: The Discord source config carrying the per-mode toggles.

    Returns:
        The mode's stub :class:`DiscordHandler`.

    Raises:
        ExporterDisabledError: When *mode* is ``EXPORTER`` and it is off.
        ModeDisabledError: When any other mode is selected but disabled.
    """
    toggle = getattr(config, mode.value)
    if not toggle.enabled:
        if mode is DiscordMode.EXPORTER:
            msg = (
                "Discord exporter mode is disabled. It is opt-in and OFF by "
                "default; enable it explicitly to use it."
            )
            raise ExporterDisabledError(msg)
        msg = f"Discord mode {mode.value!r} is disabled; enable it in config."
        raise ModeDisabledError(msg)
    return _HANDLERS[mode]()


# ---- Real opt-in user-token DM exporter connector (#686) ---------------


class TokenMissingError(RuntimeError):
    """Raised when the exporter is enabled but the token env var is unset."""


@runtime_checkable
class ExporterRunner(Protocol):
    """Mockable seam over the operator-provided user-token exporter binary."""

    def run(self, *, argv: list[str], token: str, out_dir: Path) -> None:
        """Run the exporter with *argv*, staging Data-Package JSON into *out_dir*.

        The caller creates *out_dir* before calling; implementations may assume
        it exists. The token is passed out-of-band (not in *argv*) so it never
        lands in a logged command line.
        """


class SubprocessExporterRunner:
    """Shell out to an operator-provided exporter binary, read-only (#686).

    The binary is invoked with a fixed argument list (``shell=False``) and the
    Discord token supplied via the child process environment — never on the
    command line — so it cannot leak into process listings or logs. A *timeout*
    bounds a hung binary.
    """

    def __init__(self, binary: str, *, timeout_seconds: int = 600) -> None:
        """Bind to the operator-provided exporter binary path + a timeout."""
        self._binary = binary
        self._timeout_seconds = timeout_seconds

    def run(self, *, argv: list[str], token: str, out_dir: Path) -> None:
        """Run ``<binary> <argv...>`` with the token in the child env only.

        *out_dir* is created by the caller (the connector) before this runs.
        """
        env = os.environ | {_TOKEN_ENV_VAR: token}
        # Fixed argv (no shell, no untrusted interpolation); token via env only.
        subprocess.run(
            [self._binary, *argv],
            check=True,
            env=env,
            timeout=self._timeout_seconds,
        )


class ExporterConnector:
    """Opt-in user-token DM exporter -> stage -> ingest, incrementally (#686).

    Runs only when ``exporter`` mode is enabled (else raises). Prints the ToS
    caveat, reads the token from the environment (never config/logs), invokes
    the exporter ``--after <cursor> --scope dms-only --read-only`` into a local
    staging dir, ingests the staged Data-Package JSON via the existing
    ``DiscordIngestor`` (append-only, idempotent on Discord's stable ids), and
    advances a persisted cursor so a re-run resumes rather than re-exports.
    """

    def __init__(
        self,
        config: DiscordSourceConfig,
        runner: ExporterRunner,
        vault: Path,
        *,
        cursor_path: Path | None = None,
        token_env_var: str = _TOKEN_ENV_VAR,
    ) -> None:
        """Bind config, the (mockable) exporter runner, vault, and cursor file."""
        self._config = config
        self._runner = runner
        self._vault = vault
        self._token_env_var = token_env_var
        self._cursor_path = cursor_path or (
            vault / "00-Creek-Meta" / "State" / "discord" / "cursor.json"
        )

    def run(self, after_cursor: str | None = None) -> None:
        """Export since *after_cursor* (or the persisted cursor), stage, ingest.

        Raises:
            ExporterDisabledError: When exporter mode is off (no work done).
            TokenMissingError: When the token env var is unset.
        """
        if not self._config.exporter.enabled:
            msg = "Discord exporter mode is disabled; it is opt-in and OFF by default."
            raise ExporterDisabledError(msg)
        announce_exporter_caveat()
        token = os.environ.get(self._token_env_var)
        if not token:
            msg = (
                f"{self._token_env_var} is not set. The Discord token is read "
                "from the environment only, never from config."
            )
            raise TokenMissingError(msg)

        cursor = after_cursor or self.load_cursor() or _EPOCH_CURSOR
        staging = self._vault / "discord-export"
        staging.mkdir(parents=True, exist_ok=True)
        argv = [
            "--after",
            cursor,
            "--scope",
            "dms-only",
            "--read-only",
            "--output",
            str(staging),
        ]
        # Log the plan WITHOUT the token (it is passed out-of-band to the runner).
        logger.info("discord.exporter run after=%s scope=dms-only read-only", cursor)
        self._runner.run(argv=argv, token=token, out_dir=staging)
        self._ingest(staging)
        self.save_cursor(datetime.now(UTC).isoformat())

    def _ingest(self, staging: Path) -> None:
        """Ingest the staged Data Package via the existing DiscordIngestor."""
        from creek.ingest.base import assemble_ingested_fragment
        from creek.ingest.discord import DiscordIngestor
        from creek.vault.writer import VaultWriter

        writer = VaultWriter(vault_path=self._vault)
        result = DiscordIngestor().ingest(staging)
        for parsed in result.fragments:
            assembled = assemble_ingested_fragment(parsed)
            writer.write_fragment(assembled.fragment, body=assembled.body)

    def load_cursor(self) -> str | None:
        """Return the persisted ``--after`` cursor, or ``None`` if unset."""
        if not self._cursor_path.exists():
            return None
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
            cursor = data.get("cursor") if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None
        return cursor if isinstance(cursor, str) else None

    def save_cursor(self, cursor: str) -> None:
        """Persist the ``--after`` cursor atomically (no secrets)."""
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cursor_path.with_name(self._cursor_path.name + ".tmp")
        tmp.write_text(json.dumps({"cursor": cursor}), encoding="utf-8")
        os.replace(tmp, self._cursor_path)
