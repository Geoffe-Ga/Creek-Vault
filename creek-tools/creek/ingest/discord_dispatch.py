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

from enum import StrEnum
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from creek.config import DiscordSourceConfig

console = Console()


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
        """Print the Terms-of-Service caveat before any (future) export.

        Short lines keep each asserted phrase intact (no terminal-width wrap).
        """
        console.print("[yellow][discord] DM exporter caveat:[/yellow]")
        console.print("[yellow]- A user token drives your account.[/yellow]")
        console.print("[yellow]- The Discord Terms of Service prohibit this.[/yellow]")
        console.print("[yellow]- The real stake is account suspension.[/yellow]")
        console.print(
            "[yellow]- Use it read-only, for your own DMs, at a low rate.[/yellow]",
        )

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
