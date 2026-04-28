"""Read-only Google Drive downloader for the Creek ingest pipeline.

Implements §3.4 of the Creek Ontology — a download-first, read-only
architecture that pulls files from a user's Google Drive into a local
staging directory, then hands them off to the regular ingestor
pipeline. The downloader **never** writes back to Drive: there is no
``update``, ``delete``, ``trash``, or ``copy`` call anywhere in this
module, and the :class:`DriveClient` Protocol exposes only read
methods so future refactors cannot sneak a write in by accident.

Architecture:

* :class:`DriveClient` — Protocol with ``list_files``, ``get_media``,
  ``export_media``, and ``is_available``. Tests inject a deterministic
  stub; production code uses :class:`GoogleApiDriveClient`.
* :class:`GoogleApiDriveClient` — concrete implementation that lazily
  imports ``googleapiclient`` and ``google_auth_oauthlib``. Raises
  :class:`GoogleApiUnavailableError` with actionable install
  instructions when the optional dependencies are missing.
* :class:`GoogleDriveDownloader` — orchestrator. Mirrors the local
  filesystem: ``DriveFile.parent_path`` becomes a sub-directory under
  the staging root. Skips files whose local mtime is at least as new
  as the Drive ``modified_time`` for incremental sync.
* :func:`route_to_ingestor` — maps a downloaded file's extension to
  the canonical Creek ingestor key (``markdown``, ``document``,
  ``image``, ``spreadsheet``, ``presentation``, or ``generic``).

Optional dependencies (install separately to enable real downloads):

* ``google-api-python-client`` — Drive API client.
* ``google-auth-oauthlib`` — OAuth2 flow helpers.

Without these the module still imports cleanly and the
:class:`GoogleDriveDownloader` accepts any :class:`DriveClient` stub.
"""

from __future__ import annotations

import ast
import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from creek.config import GoogleDriveConfig


logger = logging.getLogger(__name__)


GOOGLE_DOCS_MIME: str = "application/vnd.google-apps.document"
"""Mime type returned by the Drive API for native Google Docs."""

GOOGLE_SHEETS_MIME: str = "application/vnd.google-apps.spreadsheet"
"""Mime type returned by the Drive API for native Google Sheets."""

GOOGLE_SLIDES_MIME: str = "application/vnd.google-apps.presentation"
"""Mime type returned by the Drive API for native Google Slides."""


_GOOGLE_NATIVE_MIMES: frozenset[str] = frozenset(
    {GOOGLE_DOCS_MIME, GOOGLE_SHEETS_MIME, GOOGLE_SLIDES_MIME},
)


_DOCX_MIME: str = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_XLSX_MIME: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME: str = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)


_NATIVE_EXPORT_TARGETS: dict[str, tuple[str, str]] = {
    GOOGLE_DOCS_MIME: (_DOCX_MIME, ".docx"),
    GOOGLE_SHEETS_MIME: (_XLSX_MIME, ".xlsx"),
    GOOGLE_SLIDES_MIME: (_PPTX_MIME, ".pptx"),
}
"""Per-native-mime: (export mime type, export filename suffix)."""


_INGESTOR_BY_EXTENSION: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "document",
    ".pdf": "document",
    ".html": "document",
    ".htm": "document",
    ".txt": "document",
    ".rtf": "document",
    ".xlsx": "spreadsheet",
    ".csv": "spreadsheet",
    ".pptx": "presentation",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".bmp": "image",
    ".tiff": "image",
    ".webp": "image",
}


# ---- Public dataclasses + protocol -------------------------------------


@dataclass(frozen=True)
class DriveFile:
    """Lightweight metadata for a single Drive file.

    Attributes:
        id: Drive file id (used for ``get_media`` / ``export_media``).
        name: File name as it appears in Drive.
        mime_type: Drive-reported mime type.
        modified_time: Drive-reported last-modified timestamp.
        size: File size in bytes (``0`` for Google-native files).
        parent_path: Slash-separated logical path of parent folders.
            Used to mirror Drive folder structure under staging.
    """

    id: str
    name: str
    mime_type: str
    modified_time: datetime
    size: int
    parent_path: str

    @property
    def is_google_native(self) -> bool:
        """Return ``True`` for Google Docs / Sheets / Slides."""
        return self.mime_type in _GOOGLE_NATIVE_MIMES


@runtime_checkable
class DriveClient(Protocol):
    """Pluggable read-only Drive backend.

    By construction the Protocol exposes no write surface — there is
    no ``update``, ``delete``, ``trash``, or ``copy`` method. Any
    implementation that adds one violates the read-only contract.
    """

    def is_available(self) -> bool:
        """Return ``True`` when the backend can serve requests."""

    def list_files(self) -> list[DriveFile]:
        """Return every file visible to the configured credentials."""

    def get_media(self, file_id: str) -> bytes:
        """Download the raw bytes of a non-Google-native file."""

    def export_media(self, file_id: str, mime_type: str) -> bytes:
        """Export a Google-native file to *mime_type* and return bytes."""


class GoogleApiUnavailableError(RuntimeError):
    """Raised when a Drive call is made but the API client is not installed."""


# ---- Default Drive client ----------------------------------------------


class GoogleApiDriveClient:
    """Drive client backed by ``google-api-python-client``.

    Imports of the optional Google libraries are deferred to call
    time so the rest of the package — and the unit tests — run on
    systems without ``google-api-python-client`` installed.

    Attributes:
        config: :class:`GoogleDriveConfig` carrying credentials path,
            cached token path, and scopes (which the model already
            validates as read-only).
    """

    def __init__(self, config: GoogleDriveConfig) -> None:
        """Initialise with a read-only-validated config."""
        self.config = config
        self._service: Any = None

    def is_available(self) -> bool:
        """Return ``True`` when the optional Google libs import cleanly."""
        try:
            import google_auth_oauthlib.flow  # noqa: F401
            import googleapiclient.discovery  # noqa: F401
        except ImportError:
            return False
        return True

    def list_files(self) -> list[DriveFile]:
        """List every Drive file visible to the credentials.

        First enumerates folders so each file's ``parents[0]`` id can
        be resolved into a slash-separated path, then enumerates files
        and stamps the resolved path onto every :class:`DriveFile`.

        Raises:
            GoogleApiUnavailableError: When the optional Google
                libraries are not installed.
        """
        service = self._get_service()
        folders = self._list_raw(
            service,
            query="mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="nextPageToken, files(id, name, parents)",
        )
        folder_paths = _resolve_folder_paths(folders)
        files = self._list_raw(
            service,
            query="mimeType!='application/vnd.google-apps.folder' and trashed=false",
            fields=(
                "nextPageToken, files(id, name, mimeType, modifiedTime, size, parents)"
            ),
        )
        return [_drive_file_from_raw(raw, folder_paths) for raw in files]

    @staticmethod
    def _list_raw(
        service: Any,
        *,
        query: str,
        fields: str,
    ) -> list[dict[str, Any]]:
        """Page through ``service.files().list`` returning raw dicts."""
        results: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            response: dict[str, Any] = (
                service.files()
                .list(
                    q=query,
                    pageSize=1000,
                    fields=fields,
                    pageToken=page_token,
                )
                .execute()
            )
            results.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return results

    def get_media(self, file_id: str) -> bytes:
        """Download raw media for a non-Google-native Drive file."""
        service = self._get_service()
        return bytes(service.files().get_media(fileId=file_id).execute())

    def export_media(self, file_id: str, mime_type: str) -> bytes:
        """Export a Google-native file to *mime_type* and return bytes."""
        service = self._get_service()
        return bytes(
            service.files().export_media(fileId=file_id, mimeType=mime_type).execute(),
        )

    def _get_service(self) -> Any:
        """Build (or return cached) Drive API service.

        The OAuth flow caches credentials at
        :attr:`GoogleDriveConfig.token_file` so subsequent runs skip
        the browser dance.

        Raises:
            GoogleApiUnavailableError: When the optional Google
                libraries are not installed.
        """
        if self._service is not None:
            return self._service
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            msg = (
                "google-api-python-client and google-auth-oauthlib are "
                "required for Google Drive downloads. Install them with "
                "`pip install google-api-python-client google-auth-oauthlib`."
            )
            raise GoogleApiUnavailableError(msg) from exc

        creds = None
        token_path = Path(self.config.token_file)
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path),
                self.config.scopes,
            )
        if creds is None or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.config.credentials_file,
                    self.config.scopes,
                )
                creds = flow.run_local_server(port=0)
            _write_token_file(token_path, creds.to_json())
        self._service = build("drive", "v3", credentials=creds)
        return self._service


def _drive_file_from_raw(
    raw: dict[str, Any],
    folder_paths: dict[str, str],
) -> DriveFile:
    """Convert a Drive API ``files.list`` row into a :class:`DriveFile`.

    *folder_paths* maps folder ids to slash-separated paths and is
    produced by :func:`_resolve_folder_paths`. The file's first parent
    id (Drive supports multi-parenting; we use the first) is looked up
    to build :attr:`DriveFile.parent_path`. Files at the Drive root
    keep ``parent_path = ""``.
    """
    modified_str = str(raw.get("modifiedTime", "1970-01-01T00:00:00Z"))
    if modified_str.endswith("Z"):
        modified_str = modified_str[:-1] + "+00:00"
    modified = datetime.fromisoformat(modified_str)
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    parents = raw.get("parents") or []
    parent_path = folder_paths.get(parents[0], "") if parents else ""
    return DriveFile(
        id=str(raw["id"]),
        name=str(raw["name"]),
        mime_type=str(raw.get("mimeType", "application/octet-stream")),
        modified_time=modified,
        size=int(raw.get("size", 0)),
        parent_path=parent_path,
    )


def _resolve_folder_paths(folders: list[dict[str, Any]]) -> dict[str, str]:
    """Walk a flat folder listing into ``{folder_id: slash-path}``.

    Folders whose declared parent is not in the visible listing
    (orphans — usually shared folders above the visible root) are
    anchored at their own name rather than being skipped, so files
    inside them still mirror correctly under the staging root.
    """
    by_id: dict[str, dict[str, Any]] = {str(f["id"]): f for f in folders}
    paths: dict[str, str] = {}

    def _walk(folder_id: str, seen: frozenset[str]) -> str:
        if folder_id in paths:
            return paths[folder_id]
        if folder_id in seen or folder_id not in by_id:
            return ""
        folder = by_id[folder_id]
        parents = folder.get("parents") or []
        if parents and parents[0] in by_id:
            prefix = _walk(parents[0], seen | {folder_id})
            path = f"{prefix}/{folder['name']}" if prefix else str(folder["name"])
        else:
            path = str(folder["name"])
        paths[folder_id] = path
        return path

    for folder_id in by_id:
        _walk(folder_id, frozenset())
    return paths


def _write_token_file(path: Path, contents: str) -> None:
    """Write *contents* to *path* with owner-only (``0o600``) permissions.

    The OAuth refresh token grants long-lived Drive access; a default
    ``0o644`` umask would leave it group/world readable. Using
    :func:`os.open` with mode ``0o600`` makes the file owner-only on
    creation and atomically truncates an existing file.
    """
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(contents)
    # Re-chmod in case the file already existed (O_CREAT mode is
    # ignored by os.open when the file is not newly created).
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


_FORBIDDEN_DRIVE_METHODS: frozenset[str] = frozenset(
    {"update", "delete", "trash", "copy"},
)


def _audit_no_write_calls(source: str) -> None:
    """Raise :class:`AssertionError` if *source* calls a forbidden Drive method.

    AST-based: ignores method names that appear inside docstrings,
    string literals, or comments. Only flags real call sites such as
    ``service.files().delete(...)``. Used by the test suite to keep
    the read-only contract enforced as the module evolves.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_DRIVE_METHODS:
            msg = f"Forbidden write call detected: {func.attr}() at line {node.lineno}"
            raise AssertionError(msg)


# ---- Routing helper ----------------------------------------------------


def route_to_ingestor(path: Path) -> str:
    """Return the Creek ingestor key for *path* by file extension.

    Falls back to the ``generic`` ingestor for unrecognised
    extensions, matching the existing :data:`INGESTOR_REGISTRY` keys
    in :mod:`creek.ingest`.
    """
    return _INGESTOR_BY_EXTENSION.get(path.suffix.lower(), "generic")


# ---- Downloader --------------------------------------------------------


class GoogleDriveDownloader:
    """Orchestrate a read-only download of every file in Drive.

    Mirrors the user's Drive folder hierarchy under the staging
    directory (:attr:`DriveFile.parent_path` becomes a relative
    sub-path). Subsequent runs are incremental: a file is re-downloaded
    only when the Drive ``modified_time`` is newer than the local
    file's mtime.

    Attributes:
        client: Pluggable :class:`DriveClient`. Tests inject a stub;
            production passes a :class:`GoogleApiDriveClient`.
        config: :class:`GoogleDriveConfig` with read-only scopes.
    """

    def __init__(
        self,
        *,
        client: DriveClient,
        config: GoogleDriveConfig,
    ) -> None:
        """Initialise with the client and config."""
        self.client = client
        self.config = config
        self._listing_cache: list[DriveFile] | None = None

    def list_files(self) -> list[DriveFile]:
        """Return a fresh listing of every Drive file.

        Always passes through to the client so callers see live state;
        the cache used by :meth:`download_file` is populated as a side
        effect so subsequent single-file downloads reuse this snapshot.
        """
        listing = self.client.list_files()
        self._listing_cache = listing
        return listing

    def download_file(self, file_id: str, staging_dir: Path) -> Path:
        """Download a single file by id and return the local path.

        Reuses the cached listing if one exists (lazily populated on
        first call) so a loop of ``download_file`` calls makes only one
        ``list_files`` API request.

        Args:
            file_id: Drive file id.
            staging_dir: Directory under which the downloaded file is
                written. Subdirectories matching the file's
                :attr:`DriveFile.parent_path` are created on demand.

        Returns:
            Absolute path to the written file.

        Raises:
            KeyError: When *file_id* is not in the current listing.
            ValueError: When the file's ``parent_path`` would escape
                *staging_dir* (path-traversal guard).
        """
        drive_file = self._resolve(file_id)
        return self._write(drive_file, staging_dir)

    def download_all(self, staging_dir: Path) -> list[Path]:
        """Download every file in the listing to *staging_dir*.

        Files whose local mtime is at least as new as the Drive
        ``modified_time`` are skipped (incremental sync). Returns the
        list of *all* destination paths — both downloaded and skipped
        — so callers can pipe them through the ingest registry.

        Raises:
            ValueError: When any file's ``parent_path`` would escape
                *staging_dir* (path-traversal guard).
        """
        staging_dir.mkdir(parents=True, exist_ok=True)
        # Refresh cache so download_all always sees latest state.
        listing = self.list_files()
        paths: list[Path] = []
        for drive_file in listing:
            target = self._target_path(drive_file, staging_dir)
            if self._is_up_to_date(target, drive_file):
                logger.info("Skipping unchanged Drive file: %s", drive_file.name)
                paths.append(target)
                continue
            paths.append(self._write(drive_file, staging_dir))
        return paths

    def _resolve(self, file_id: str) -> DriveFile:
        """Return the :class:`DriveFile` for *file_id* from the cached listing."""
        if self._listing_cache is None:
            self._listing_cache = self.client.list_files()
        for candidate in self._listing_cache:
            if candidate.id == file_id:
                return candidate
        msg = f"missing Drive file id: {file_id!r}"
        raise KeyError(msg)

    def _write(self, drive_file: DriveFile, staging_dir: Path) -> Path:
        """Fetch bytes for *drive_file* and persist them under *staging_dir*."""
        target = self._target_path(drive_file, staging_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if drive_file.is_google_native:
            export_mime, suffix = _NATIVE_EXPORT_TARGETS[drive_file.mime_type]
            data = self.client.export_media(drive_file.id, export_mime)
            target = target.with_name(target.stem + suffix)
        else:
            data = self.client.get_media(drive_file.id)
        target.write_bytes(data)
        timestamp = drive_file.modified_time.timestamp()
        os.utime(target, (timestamp, timestamp))
        return target

    @staticmethod
    def _target_path(drive_file: DriveFile, staging_dir: Path) -> Path:
        """Return the destination path for *drive_file* under *staging_dir*.

        Raises:
            ValueError: If ``drive_file.parent_path`` would resolve
                outside *staging_dir* — defends against malicious or
                malformed Drive folder names that contain ``..``
                segments or absolute paths.
        """
        staging_root = staging_dir.resolve()
        parent_segments = (
            Path(drive_file.parent_path) if drive_file.parent_path else None
        )
        if parent_segments is not None and parent_segments.is_absolute():
            msg = (
                f"parent_path {drive_file.parent_path!r} escapes staging "
                f"root {staging_dir!s} (absolute path)"
            )
            raise ValueError(msg)
        if parent_segments is not None:
            candidate = (staging_root / parent_segments / drive_file.name).resolve()
        else:
            candidate = (staging_root / drive_file.name).resolve()
        try:
            candidate.relative_to(staging_root)
        except ValueError as exc:
            msg = (
                f"parent_path {drive_file.parent_path!r} escapes staging "
                f"root {staging_dir!s}"
            )
            raise ValueError(msg) from exc
        # Return the unresolved path (preserving symlinks etc.) so caller
        # paths look like staging_dir/parent/name rather than the resolved form.
        if parent_segments is not None:
            return staging_dir / parent_segments / drive_file.name
        return staging_dir / drive_file.name

    def _is_up_to_date(self, target: Path, drive_file: DriveFile) -> bool:
        """Return ``True`` when the local file is at least as new as Drive."""
        actual = self._native_target(target, drive_file)
        if not actual.exists():
            return False
        local_mtime = datetime.fromtimestamp(actual.stat().st_mtime, tz=UTC)
        return local_mtime >= drive_file.modified_time

    @staticmethod
    def _native_target(target: Path, drive_file: DriveFile) -> Path:
        """Return the actual on-disk path for *drive_file* (incl. export suffix)."""
        if not drive_file.is_google_native:
            return target
        _, suffix = _NATIVE_EXPORT_TARGETS[drive_file.mime_type]
        return target.with_name(target.stem + suffix)


__all__ = [
    "GOOGLE_DOCS_MIME",
    "GOOGLE_SHEETS_MIME",
    "GOOGLE_SLIDES_MIME",
    "DriveClient",
    "DriveFile",
    "GoogleApiDriveClient",
    "GoogleApiUnavailableError",
    "GoogleDriveDownloader",
    "route_to_ingestor",
]
