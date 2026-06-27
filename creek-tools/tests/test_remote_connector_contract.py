"""Reusable contract any ``RemoteSourceConnector`` must satisfy (#684).

Parametrised over connector factories — Drive is the only implementation today,
but a future connector (Substack/Notion/RSS) added to ``_FACTORIES`` is held to
the same contract for free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.config import GoogleDriveConfig
from creek.ingest.connectors import RemoteSourceConnector
from creek.ingest.gdrive import DriveFile, build_drive_connector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    ConnectorFactory = Callable[..., RemoteSourceConnector]


class _StubClient:
    """Minimal in-memory Drive client for the contract (one markdown file)."""

    def __init__(self) -> None:
        """Seed a single markdown file with media bytes."""
        self._file = DriveFile(
            id="x",
            name="x.md",
            mime_type="text/markdown",
            modified_time=datetime(2026, 4, 1, tzinfo=UTC),
            size=2,
            parent_path="",
        )

    def is_available(self) -> bool:
        """Stub is always available."""
        return True

    def list_files(self) -> list[DriveFile]:
        """Return the canned one-file listing."""
        return [self._file]

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Write canned bytes (the contract only needs a successful fetch)."""
        del file_id, export_mime
        destination.write_bytes(b"hi")


def _make_drive_connector(*, staging: Path) -> RemoteSourceConnector:
    """Factory: a Drive connector wired to the stub client + a cursor file."""
    return build_drive_connector(
        GoogleDriveConfig(),
        client=_StubClient(),
        staging=staging,
        cursor_path=staging / "cursor.json",
    )


_FACTORIES = [_make_drive_connector]


@pytest.mark.parametrize("make_connector", _FACTORIES)
class TestRemoteConnectorContract:
    """Every connector must satisfy this contract."""

    def test_conforms_to_protocol(
        self,
        make_connector: ConnectorFactory,
        tmp_path: Path,
    ) -> None:
        """The connector is a RemoteSourceConnector and is_available never raises."""
        conn = make_connector(staging=tmp_path)
        assert isinstance(conn, RemoteSourceConnector)
        assert conn.is_available() in (True, False)

    def test_cursor_makes_listing_idempotent(
        self,
        make_connector: ConnectorFactory,
        tmp_path: Path,
    ) -> None:
        """After fetch + save_cursor, the reloaded cursor yields no changes."""
        conn = make_connector(staging=tmp_path)
        first = list(conn.list_changed_since(cursor=conn.load_cursor()))
        assert first  # a fresh cursor surfaces the initial file(s)
        conn.fetch_to(tmp_path)
        conn.save_cursor(first)
        assert list(conn.list_changed_since(cursor=conn.load_cursor())) == []

    def test_cursor_persists_across_instances(
        self,
        make_connector: ConnectorFactory,
        tmp_path: Path,
    ) -> None:
        """A brand-new instance resumes from the persisted cursor."""
        first = make_connector(staging=tmp_path)
        changed = list(first.list_changed_since(cursor=first.load_cursor()))
        first.fetch_to(tmp_path)
        first.save_cursor(changed)

        second = make_connector(staging=tmp_path)
        assert list(second.list_changed_since(cursor=second.load_cursor())) == []
