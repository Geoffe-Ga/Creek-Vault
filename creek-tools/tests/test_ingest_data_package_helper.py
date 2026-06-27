"""Incremental Discord Data Package ingest helper (#688).

The clean, always-available compliant fallback: point ``run_discord_data_package``
at a downloaded Data Package and it ingests incrementally — stable Discord ids
make a re-pointed or overlapping package a clean no-op, so there is no re-spend
on already-seen messages.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from creek.ingest.discord import run_discord_data_package

if TYPE_CHECKING:
    from pathlib import Path


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold the minimal vault folders the writer needs."""
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta/Processing-Log", "01-Fragments/Messages"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _msg(msg_id: str, *, minute: int) -> dict[str, object]:
    """A substantial message, spaced >5 min apart so each is its own fragment."""
    return {
        "id": msg_id,
        "timestamp": f"2026-06-26T{10 + minute:02d}:00:00+00:00",
        "content": f"reflection number {msg_id} that is worth keeping in the vault",
        "author": {"name": "Ada"},
    }


def _write_data_package(root: Path, messages: list[dict[str, object]]) -> Path:
    """Write a one-channel Discord Data Package under *root*; return its path."""
    package = root / "discord-export"
    channel = package / "messages" / "general"
    channel.mkdir(parents=True, exist_ok=True)
    (channel / "channel.json").write_text(
        json.dumps({"id": "general", "name": "general"}), encoding="utf-8"
    )
    (channel / "messages.json").write_text(json.dumps(messages), encoding="utf-8")
    return package


def _fragments(vault: Path) -> list[Path]:
    """Every fragment markdown file under 01-Fragments."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


class TestRunDiscordDataPackage:
    """Pointing at a package ingests incrementally; overlap is a clean no-op."""

    def test_ingests_then_idempotent_then_delta(self, tmp_path: Path) -> None:
        """First ingest counts all; re-point is 0; a new message ingests only it."""
        vault = _make_vault(tmp_path)
        pkg = _write_data_package(tmp_path, [_msg("1", minute=0), _msg("2", minute=10)])

        first = run_discord_data_package(vault, pkg)
        assert first == 2
        assert len(_fragments(vault)) == 2

        # Re-point at the same package -> clean no-op (stable ids).
        second = run_discord_data_package(vault, pkg)
        assert second == 0
        assert len(_fragments(vault)) == 2  # no duplicates written

        # A package with one new message ingests only the delta.
        pkg2 = _write_data_package(
            tmp_path,
            [_msg("1", minute=0), _msg("2", minute=10), _msg("3", minute=20)],
        )
        third = run_discord_data_package(vault, pkg2)
        assert third == 1
        assert len(_fragments(vault)) == 3

    def test_empty_package_is_zero(self, tmp_path: Path) -> None:
        """A package with no messages ingests nothing (no crash)."""
        vault = _make_vault(tmp_path)
        pkg = _write_data_package(tmp_path, [])
        assert run_discord_data_package(vault, pkg) == 0
        assert _fragments(vault) == []
