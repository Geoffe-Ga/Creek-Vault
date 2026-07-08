"""Tier-A consumption of the bot-capture dir (#687).

The CrawDad bot appends ``discord-capture/<channel>/<date>.jsonl`` in the
lowercase Discord message schema. These tests prove the creek side reshapes that
dir into the Data-Package layout and ingests it via the existing
``DiscordIngestor`` — with no parser rewrite — and that a backfill that overlaps
the live stream is idempotent at ingest (stable message ids).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from creek.config import DiscordSourceConfig
from creek.ingest.discord import (
    ingest_capture_dir,
    stage_capture_as_data_package,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault folders the writer needs."""
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta/Processing-Log", "01-Fragments/Messages"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _capture_line(*, msg_id: str, ts: str, content: str) -> str:
    """One JSONL line in the schema CrawDad's MessageCapture writes."""
    return json.dumps(
        {
            "id": msg_id,
            "timestamp": ts,
            "content": content,
            "author": {"name": "Ada"},
        }
    )


def _write_capture(
    capture_dir: Path, channel: str, date: str, lines: list[str]
) -> None:
    """Append capture *lines* to ``<capture_dir>/<channel>/<date>.jsonl``."""
    path = capture_dir / channel / f"{date}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fragments(vault: Path) -> list[Path]:
    """Every fragment markdown file under 01-Fragments."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


class TestCaptureSubpathConfig:
    """The capture dir is a vault-relative, traversal-safe config field."""

    def test_default_is_discord_capture(self) -> None:
        """The bot-capture dir defaults to ``discord-capture``."""
        assert DiscordSourceConfig().capture_subpath == "discord-capture"

    def test_absolute_path_rejected(self) -> None:
        """An absolute capture path is refused (must stay in the vault)."""
        try:
            DiscordSourceConfig(capture_subpath="/etc/discord")
        except ValueError as exc:
            assert "vault-relative" in str(exc)
        else:  # pragma: no cover - the assertion above is the contract
            msg = "expected ValueError for absolute capture_subpath"
            raise AssertionError(msg)

    def test_parent_traversal_rejected(self) -> None:
        """A ``..``-traversing capture path is refused."""
        try:
            DiscordSourceConfig(capture_subpath="../escape")
        except ValueError as exc:
            assert "vault-relative" in str(exc)
        else:  # pragma: no cover - the assertion above is the contract
            msg = "expected ValueError for parent-traversing capture_subpath"
            raise AssertionError(msg)


class TestStageCaptureAsDataPackage:
    """Reshaping the capture dir produces the Data-Package layout the parser reads."""

    def test_reshapes_deduped_and_sorted(self, tmp_path: Path) -> None:
        """Lines across files dedupe by id and sort chronologically."""
        capture = tmp_path / "discord-capture"
        _write_capture(
            capture,
            "general",
            "2026-06-26",
            [
                _capture_line(
                    msg_id="100", ts="2026-06-26T10:00:00+00:00", content="hi"
                ),
                _capture_line(
                    msg_id="098", ts="2026-06-26T09:58:00+00:00", content="earlier"
                ),
                _capture_line(  # duplicate id (live stream + backfill overlap)
                    msg_id="100", ts="2026-06-26T10:00:00+00:00", content="hi"
                ),
            ],
        )
        staging = tmp_path / "staging"
        stage_capture_as_data_package(capture, staging)

        messages_file = staging / "messages" / "general" / "messages.json"
        messages = json.loads(messages_file.read_text(encoding="utf-8"))
        assert [m["id"] for m in messages] == ["098", "100"]  # deduped + sorted
        channel = json.loads(
            (staging / "messages" / "general" / "channel.json").read_text(
                encoding="utf-8"
            )
        )
        assert channel["name"] == "general"

    def test_missing_capture_dir_is_noop(self, tmp_path: Path) -> None:
        """A non-existent capture dir reshapes to nothing (no crash)."""
        staging = tmp_path / "staging"
        stage_capture_as_data_package(tmp_path / "absent", staging)
        assert not (staging / "messages").exists()


class TestIngestCaptureDir:
    """End-to-end: a capture dir becomes vault fragments, idempotently."""

    def test_ingests_messages(self, tmp_path: Path) -> None:
        """A captured message lands as a fragment in the vault."""
        vault = _make_vault(tmp_path)
        capture = tmp_path / "discord-capture"
        _write_capture(
            capture,
            "general",
            "2026-06-26",
            [
                _capture_line(
                    msg_id="100",
                    ts="2026-06-26T10:00:00+00:00",
                    content="a private reflection worth keeping in the vault",
                )
            ],
        )

        written = ingest_capture_dir(capture, vault)

        assert written > 0
        assert len(_fragments(vault)) == written

    def test_overlapping_rerun_is_idempotent(self, tmp_path: Path) -> None:
        """A second ingest over an overlapping capture writes no duplicate."""
        vault = _make_vault(tmp_path)
        capture = tmp_path / "discord-capture"
        _write_capture(
            capture,
            "general",
            "2026-06-26",
            [
                _capture_line(
                    msg_id="100",
                    ts="2026-06-26T10:00:00+00:00",
                    content="a private reflection worth keeping in the vault",
                )
            ],
        )
        ingest_capture_dir(capture, vault)
        before = len(_fragments(vault))
        assert before > 0

        # Backfill re-captures the same id into a later file; ingest again.
        _write_capture(
            capture,
            "general",
            "2026-06-27",
            [
                _capture_line(
                    msg_id="100",
                    ts="2026-06-26T10:00:00+00:00",
                    content="a private reflection worth keeping in the vault",
                )
            ],
        )
        ingest_capture_dir(capture, vault)

        assert len(_fragments(vault)) == before  # stable id -> no duplicate
