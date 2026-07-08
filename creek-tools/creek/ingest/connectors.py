"""Source-agnostic remote-connector protocol (#683 / SPEC R4).

A ``RemoteSourceConnector`` is a **read-only** pull adapter for a remote
source: it reports availability, lists the files that changed since a cursor,
and fetches them into a local staging directory (where the regular ingestors
then pick them up via ``route_to_ingestor``). It does **not** ingest, and by
construction exposes no write/update/delete surface.

Google Drive is the reference implementation (``creek.ingest.gdrive``); future
sources (Substack, Notion, an RSS feed, a prose git repo) implement the same
methods and reuse the stage -> route -> ingest machinery. ``list_changed_since``
is incremental against a **persisted cursor** (a durable, non-secret bookmark
such as the last-seen modified time) so a fresh process or a new host resumes
correctly — :meth:`RemoteSourceConnector.load_cursor` /
:meth:`RemoteSourceConnector.save_cursor` read and advance it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path


@runtime_checkable
class RemoteFile(Protocol):
    """Structural descriptor for one remote file.

    The concrete :class:`creek.ingest.gdrive.DriveFile` conforms to this
    without inheritance — any connector's file type that carries these
    read-only attributes satisfies the contract.
    """

    @property
    def id(self) -> str:
        """Stable remote id (used to fetch the file)."""

    @property
    def name(self) -> str:
        """File name as the remote reports it."""

    @property
    def modified_time(self) -> datetime:
        """Remote last-modified timestamp (drives the incremental skip)."""

    @property
    def size(self) -> int:
        """File size in bytes (0 for export-only/native files)."""

    @property
    def parent_path(self) -> str:
        """Slash-separated logical parent path, mirrored under staging."""


@runtime_checkable
class RemoteSourceConnector(Protocol):
    """Read-only pull connector for a remote source (#683).

    By construction there is **no** write/update/delete method — a connector
    only reads and downloads. Implementations adapt an existing client; the
    protocol is what the future sync machinery depends on.
    """

    def is_available(self) -> bool:
        """Report whether the backend can serve requests (capability only).

        Never reads or returns credentials — presence/capability only.
        """

    def list_changed_since(self, cursor: object) -> Sequence[RemoteFile]:
        """Return the files changed since *cursor* (the incremental set).

        *cursor* (as :meth:`load_cursor` returns) of ``None`` means the first
        pass — everything. After :meth:`fetch_to` + :meth:`save_cursor`, the
        reloaded cursor excludes the already-fetched files, so the next call
        returns only what is genuinely new.
        """

    def fetch_to(self, staging: Path) -> object:
        """Download the changed files into *staging*, returning a result.

        The return is the implementation's own result object (e.g. Drive's
        ``DownloadResult``); callers route the staged files through
        ``route_to_ingestor``.
        """

    def load_cursor(self) -> object:
        """Return the persisted incremental cursor, or ``None`` if none (#684).

        The cursor is a durable bookmark (e.g. the last-seen modified time) so a
        fresh process or a new host resumes incrementally — it is *not* tied to
        in-process state. It stores only non-secret bookkeeping, never
        credentials.
        """

    def save_cursor(self, fetched: Sequence[RemoteFile]) -> None:
        """Advance the persisted cursor past *fetched* (#684).

        Called after a successful :meth:`fetch_to`; subsequent
        :meth:`list_changed_since` calls against the reloaded cursor exclude the
        already-fetched files.
        """
