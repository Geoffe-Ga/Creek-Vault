"""Live Discord message capture for the bot-capture ingest path (#687).

CrawDad logs each message in the servers/channels it is in to
``<capture_dir>/<channel>/<date>.jsonl`` — one JSON object per line in the
lowercase Discord message schema the creek ``DiscordIngestor`` already reads::

    {"id": "...", "timestamp": "ISO8601", "content": "...",
     "author": {"name": "..."}, "reference": {"messageId": "..."}}

Tier-A ingest (Epic B's ``creek sync``) reshapes the capture dir into the
Data-Package layout and ingests it with **no parser rewrite**. A bot **cannot**
read DMs (a hard Discord API limit), so this path covers only channels the bot
is in; DMs are the opt-in user-token exporter's job (#686).

The bot token lives in the **environment only**; capture adds no new secret
surface. Discord message ids are stable, so appending the same message twice is
a no-op (the writer skips a known id) and a backfill that overlaps the live
stream is idempotent both here and at ingest.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


@runtime_checkable
class _AuthorLike(Protocol):
    """The author bits we read — ``name`` always, ``display_name`` if present."""

    @property
    def name(self) -> str: ...  # pragma: no cover - protocol stub


@runtime_checkable
class _MessageLike(Protocol):
    """Structural view of ``discord.Message`` covering the bits capture reads."""

    @property
    def id(self) -> int: ...  # pragma: no cover - protocol stub

    @property
    def content(self) -> str: ...  # pragma: no cover - protocol stub

    @property
    def created_at(self) -> datetime: ...  # pragma: no cover - protocol stub

    @property
    def author(self) -> _AuthorLike: ...  # pragma: no cover - protocol stub

    @property
    def channel(self) -> object: ...  # pragma: no cover - protocol stub


@runtime_checkable
class _BackfillChannel(Protocol):
    """A channel that can replay its history for gap-filling backfill."""

    def history(
        self, *, after: datetime | None, oldest_first: bool
    ) -> AsyncIterator[_MessageLike]: ...  # pragma: no cover - protocol stub


def _author_name(message: _MessageLike) -> str:
    """The display name if the author has one, else the plain name."""
    display = getattr(message.author, "display_name", "")
    return display or message.author.name


def _reference_id(message: _MessageLike) -> str | None:
    """The replied-to message id, if this message is a reply."""
    ref = getattr(message, "reference", None)
    ref_id = getattr(ref, "message_id", None) if ref is not None else None
    return str(ref_id) if ref_id is not None else None


def _channel_label(channel: object) -> str:
    """A filesystem-safe channel folder name (``name`` if usable, else id).

    Discord text-channel names are already restricted, but path separators are
    stripped and a bare ``.`` / ``..`` name is refused (falling back to the
    channel id) so a channel folder can never escape the capture root — even
    from a mocked channel or if Discord's naming rules ever loosen.
    """
    name = getattr(channel, "name", None)
    raw = name if isinstance(name, str) and name.strip() else None
    fallback = str(getattr(channel, "id", "unknown"))
    label = (raw or fallback).replace("/", "_").replace("\\", "_").strip()
    if not label or label in {".", ".."}:
        return fallback or "unknown"
    return label


def _record_for(message: _MessageLike) -> dict[str, object]:
    """Build the lowercase-schema JSON record for *message*."""
    record: dict[str, object] = {
        "id": str(message.id),
        "timestamp": message.created_at.isoformat(),
        "content": message.content,
        "author": {"name": _author_name(message)},
    }
    ref_id = _reference_id(message)
    if ref_id is not None:
        record["reference"] = {"messageId": ref_id}
    return record


class MessageCapture:
    """Append-only JSONL capture of Discord messages, idempotent on message id.

    One instance serves the bot's lifetime. ``on_message`` tails the live stream;
    ``backfill`` replays a channel's ``history`` to fill the gap since the last
    captured message. Both skip ids already on disk, so overlap is a clean no-op.
    """

    def __init__(self, capture_dir: Path) -> None:
        """Bind the capture root; per-channel seen-id sets load lazily from disk.

        ``_seen`` caches every captured message id per channel for the bot's
        lifetime — fine for the personal-use deployment target; a very high
        volume server running for months could grow it to tens of thousands of
        ids per channel, at which point a bounded/evicting cache would be worth
        adding.
        """
        self._dir = capture_dir
        self._seen: dict[str, set[str]] = {}

    async def on_message(self, message: _MessageLike) -> None:
        """Append one live message to ``<channel>/<date>.jsonl`` (id-idempotent)."""
        self._append(message)

    async def backfill(self, channel: _BackfillChannel) -> None:
        """Replay *channel*'s history since the last capture; append the gap.

        The pull is bounded by the latest captured timestamp for the channel
        (an optimisation); id-dedup makes any overlap with the live stream safe.
        """
        label = _channel_label(channel)
        after = self._latest_timestamp(label)
        async for message in channel.history(after=after, oldest_first=True):
            self._append(message)

    def _append(self, message: _MessageLike) -> None:
        """Write *message* as a JSON line, skipping a message id already seen."""
        label = _channel_label(message.channel)
        msg_id = str(message.id)
        seen = self._seen_ids(label)
        if msg_id in seen:
            return
        date = message.created_at.date().isoformat()
        path = self._dir / label / f"{date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_record_for(message)) + "\n")
        seen.add(msg_id)

    def _seen_ids(self, channel: str) -> set[str]:
        """The set of message ids already captured for *channel* (cached)."""
        if channel not in self._seen:
            self._seen[channel] = {
                str(rec["id"])
                for rec in self._iter_records(channel)
                if isinstance(rec.get("id"), str)
            }
        return self._seen[channel]

    def _latest_timestamp(self, channel: str) -> datetime | None:
        """The newest captured ``timestamp`` for *channel*, or ``None`` if empty."""
        latest: datetime | None = None
        for rec in self._iter_records(channel):
            raw = rec.get("timestamp")
            if not isinstance(raw, str):
                continue
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if latest is None or parsed > latest:
                latest = parsed
        return latest

    def _iter_records(self, channel: str) -> Iterator[dict[str, object]]:
        """Yield every parsed JSON record under ``<capture_dir>/<channel>/``."""
        channel_dir = self._dir / channel
        if not channel_dir.is_dir():
            return
        for jsonl in sorted(channel_dir.glob("*.jsonl")):
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
