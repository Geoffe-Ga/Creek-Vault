"""Tests for ``crawdad.capture`` — the bot-capture JSONL writer (#687).

Driven entirely by a **fake message stream** — no live Discord connection. Proves
a live message is appended to the correct ``<channel>/<date>.jsonl``, that
``backfill`` fills the gap via ``channel.history()`` and overlaps dedupe by
stable message id, and that the captured lines carry the lowercase schema creek's
``DiscordIngestor`` reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from crawdad.capture import MessageCapture

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _FakeAuthor:
    name: str
    display_name: str = ""


@dataclass
class _FakeChannel:
    name: str
    id: int = 1


@dataclass
class _FakeReference:
    message_id: int


@dataclass
class _FakeMessage:
    id: int
    content: str
    created_at: datetime
    author: _FakeAuthor
    channel: _FakeChannel
    reference: _FakeReference | None = None


@dataclass
class _FakeHistoryChannel:
    name: str
    history_messages: list[_FakeMessage]
    id: int = 1
    received_after: datetime | None = None

    def history(
        self, *, after: datetime | None, oldest_first: bool
    ) -> AsyncIterator[_FakeMessage]:
        """Record the *after* cursor, then replay the canned history."""
        assert oldest_first is True
        self.received_after = after
        messages = self.history_messages

        async def _gen() -> AsyncIterator[_FakeMessage]:
            for message in messages:
                yield message

        return _gen()


def _message(
    *,
    msg_id: int,
    content: str,
    ts: str,
    channel: str = "general",
    author: str = "Ada",
    reference: int | None = None,
) -> _FakeMessage:
    """Build a fake discord-message-shaped object."""
    return _FakeMessage(
        id=msg_id,
        content=content,
        created_at=datetime.fromisoformat(ts).replace(tzinfo=UTC),
        author=_FakeAuthor(name=author),
        channel=_FakeChannel(name=channel),
        reference=(
            _FakeReference(message_id=reference) if reference is not None else None
        ),
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Parse a captured JSONL file into a list of records."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestOnMessage:
    """Live messages append to ``<channel>/<date>.jsonl`` in the creek schema."""

    async def test_appends_lowercase_schema(self, tmp_path: Path) -> None:
        """A captured message carries id/timestamp/content/author."""
        capture = MessageCapture(capture_dir=tmp_path / "discord-capture")
        await capture.on_message(
            _message(msg_id=100, content="hi", ts="2026-06-26T10:00:00")
        )

        path = tmp_path / "discord-capture" / "general" / "2026-06-26.jsonl"
        records = _read_jsonl(path)
        assert len(records) == 1
        assert records[0]["id"] == "100"
        assert records[0]["content"] == "hi"
        assert records[0]["author"] == {"name": "Ada"}
        assert records[0]["timestamp"].startswith("2026-06-26T10:00:00")

    async def test_reply_records_reference(self, tmp_path: Path) -> None:
        """A reply captures the replied-to message id for the parser."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        await capture.on_message(
            _message(msg_id=101, content="re", ts="2026-06-26T10:01:00", reference=100)
        )
        record = _read_jsonl(tmp_path / "cap" / "general" / "2026-06-26.jsonl")[0]
        assert record["reference"] == {"messageId": "100"}

    async def test_duplicate_id_is_skipped(self, tmp_path: Path) -> None:
        """The same message id twice writes only one line (idempotent)."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        msg = _message(msg_id=100, content="hi", ts="2026-06-26T10:00:00")
        await capture.on_message(msg)
        await capture.on_message(msg)
        records = _read_jsonl(tmp_path / "cap" / "general" / "2026-06-26.jsonl")
        assert len(records) == 1

    async def test_display_name_preferred(self, tmp_path: Path) -> None:
        """A member's display name wins over the bare account name."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        msg = _FakeMessage(
            id=1,
            content="x is a thoughtful note worth keeping",
            created_at=datetime.fromisoformat("2026-06-26T10:00:00").replace(
                tzinfo=UTC
            ),
            author=_FakeAuthor(name="ada#1", display_name="Ada Lovelace"),
            channel=_FakeChannel(name="general"),
        )
        await capture.on_message(msg)
        record = _read_jsonl(tmp_path / "cap" / "general" / "2026-06-26.jsonl")[0]
        assert record["author"] == {"name": "Ada Lovelace"}

    async def test_channel_name_with_separator_sanitised(self, tmp_path: Path) -> None:
        """A path separator in a channel name cannot escape the capture root."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        msg = _message(msg_id=1, content="hi", ts="2026-06-26T10:00:00", channel="a/b")
        await capture.on_message(msg)
        assert (tmp_path / "cap" / "a_b" / "2026-06-26.jsonl").is_file()

    async def test_parent_traversal_channel_name_falls_back_to_id(
        self, tmp_path: Path
    ) -> None:
        """A ``..`` channel name writes under the channel id, never the parent."""
        capture = MessageCapture(capture_dir=tmp_path / "cap" / "root")
        msg = _FakeMessage(
            id=1,
            content="a note worth keeping in the vault",
            created_at=datetime.fromisoformat("2026-06-26T10:00:00").replace(
                tzinfo=UTC
            ),
            author=_FakeAuthor(name="Ada"),
            channel=_FakeChannel(name="..", id=4242),
        )
        await capture.on_message(msg)
        # Written under the id folder inside the root — not the escaped parent.
        assert (tmp_path / "cap" / "root" / "4242" / "2026-06-26.jsonl").is_file()
        assert not (tmp_path / "cap" / "2026-06-26.jsonl").exists()


class TestBackfill:
    """Backfill replays channel history and overlaps dedupe by message id."""

    async def test_fills_gap_and_overlap_is_idempotent(self, tmp_path: Path) -> None:
        """Backfill appends the earlier message; the live dup is a no-op."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        # Live stream already captured id 100.
        await capture.on_message(
            _message(msg_id=100, content="hi", ts="2026-06-26T10:00:00")
        )
        channel = _FakeHistoryChannel(
            name="general",
            history_messages=[
                _message(msg_id=98, content="earlier", ts="2026-06-26T09:58:00"),
                _message(msg_id=100, content="hi", ts="2026-06-26T10:00:00"),  # dup
            ],
        )

        await capture.backfill(channel)

        records = _read_jsonl(tmp_path / "cap" / "general" / "2026-06-26.jsonl")
        ids = sorted(str(r["id"]) for r in records)
        assert ids == ["100", "98"]  # gap filled once; the duplicate skipped

    async def test_backfill_passes_latest_timestamp_as_after(
        self, tmp_path: Path
    ) -> None:
        """Backfill bounds the history pull at the newest captured timestamp."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        await capture.on_message(
            _message(msg_id=10, content="older", ts="2026-06-26T09:00:00")
        )
        await capture.on_message(
            _message(msg_id=11, content="newer", ts="2026-06-26T10:30:00")
        )
        channel = _FakeHistoryChannel(name="general", history_messages=[])

        await capture.backfill(channel)

        assert channel.received_after == datetime.fromisoformat(
            "2026-06-26T10:30:00"
        ).replace(tzinfo=UTC)

    async def test_backfill_after_is_none_when_channel_empty(
        self, tmp_path: Path
    ) -> None:
        """With nothing captured yet, backfill pulls the full history (after=None)."""
        capture = MessageCapture(capture_dir=tmp_path / "cap")
        channel = _FakeHistoryChannel(name="general", history_messages=[])

        await capture.backfill(channel)

        assert channel.received_after is None

    async def test_tolerates_malformed_and_odd_lines(self, tmp_path: Path) -> None:
        """Blank/malformed/non-dict lines and bad timestamps are skipped, not fatal."""
        cap_dir = tmp_path / "cap"
        channel_dir = cap_dir / "general"
        channel_dir.mkdir(parents=True)
        (channel_dir / "2026-06-26.jsonl").write_text(
            "\n"  # blank line
            "{not valid json}\n"  # malformed
            + json.dumps({"id": "1", "timestamp": 123, "content": "x"})  # non-str ts
            + "\n"
            + json.dumps(
                {"id": "2", "timestamp": "not-a-date", "content": "y"}
            )  # bad iso
            + "\n"
            + json.dumps(["not", "a", "dict"])  # non-dict record
            + "\n"
            + json.dumps(
                {"id": "3", "timestamp": "2026-06-26T10:00:00+00:00", "content": "z"}
            )
            + "\n",
            encoding="utf-8",
        )

        capture = MessageCapture(capture_dir=cap_dir)
        channel = _FakeHistoryChannel(
            name="general",
            history_messages=[
                _message(msg_id=3, content="z", ts="2026-06-26T10:00:00"),  # seen
                _message(msg_id=4, content="new note", ts="2026-06-26T11:00:00"),
            ],
        )
        await capture.backfill(channel)

        text = (channel_dir / "2026-06-26.jsonl").read_text(encoding="utf-8")
        assert text.count('"id": "3"') == 1  # already captured -> not re-appended
        assert '"id": "4"' in text  # the genuinely new message is appended

    async def test_backfill_seeds_from_existing_capture(self, tmp_path: Path) -> None:
        """A fresh instance loads already-captured ids from disk (no re-append)."""
        cap_dir = tmp_path / "cap"
        first = MessageCapture(capture_dir=cap_dir)
        await first.on_message(
            _message(msg_id=100, content="hi", ts="2026-06-26T10:00:00")
        )

        # A brand-new instance must see id 100 as already captured.
        second = MessageCapture(capture_dir=cap_dir)
        channel = _FakeHistoryChannel(
            name="general",
            history_messages=[
                _message(msg_id=100, content="hi", ts="2026-06-26T10:00:00"),
            ],
        )
        await second.backfill(channel)

        records = _read_jsonl(cap_dir / "general" / "2026-06-26.jsonl")
        assert len(records) == 1  # no duplicate from the second instance
