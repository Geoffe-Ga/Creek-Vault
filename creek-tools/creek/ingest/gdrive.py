"""Read-only Google Drive downloader for the Creek ingest pipeline.

Implements §3.4 of the Creek Ontology — a download-first, read-only
architecture that pulls files from a user's Google Drive into a local
staging directory, then hands them off to the regular ingestor
pipeline. The downloader **never** writes back to Drive: there is no
``update``, ``delete``, ``trash``, or ``copy`` call anywhere in this
module, and the :class:`DriveClient` Protocol exposes only read
methods so future refactors cannot sneak a write in by accident.

Architecture:

* :class:`DriveClient` — Protocol with ``list_files``, ``download_to``,
  and ``is_available``. Tests inject a deterministic stub; production
  code uses :class:`GoogleApiDriveClient`.
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

import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

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
        id: Drive file id (used for ``download_to``).
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

    Downloads are streamed straight to disk via :meth:`download_to`
    rather than collected to ``bytes``; callers do not need to hold
    a full file in memory regardless of size.
    """

    def is_available(self) -> bool:
        """Return ``True`` when the backend can serve requests."""

    def list_files(self) -> list[DriveFile]:
        """Return every file visible to the configured credentials."""

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Stream *file_id* to *destination*.

        When *export_mime* is provided the file is treated as a
        Google-native document and exported to that mime type;
        otherwise the raw media is downloaded.
        """


class GoogleApiUnavailableError(RuntimeError):
    """Raised when a Drive call is made but the API client is not installed."""


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a :meth:`GoogleDriveDownloader.download_all` invocation.

    Tuples (not lists) so the value object is fully immutable —
    ``frozen=True`` would otherwise still allow callers to
    ``result.downloaded.append(...)``.

    Attributes:
        downloaded: Paths of files that were freshly fetched (or
            re-fetched because Drive's modified_time was newer).
        skipped: Paths of files whose local mtime was at least as new
            as Drive — the incremental-sync skip set.
        errors: ``(DriveFile, exception)`` pairs for any per-file
            download that failed mid-loop. ``download_all`` records
            each failure and continues so a transient quota/network
            error mid-sync does not abandon the rest of the run.
    """

    downloaded: tuple[Path, ...]
    skipped: tuple[Path, ...]
    errors: tuple[tuple[DriveFile, Exception], ...] = ()

    @property
    def all_paths(self) -> tuple[Path, ...]:
        """Return the union of downloaded + skipped paths in listing order."""
        return (*self.downloaded, *self.skipped)


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

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Stream *file_id* to *destination* in chunks.

        Uses :class:`googleapiclient.http.MediaIoBaseDownload` so
        large files (videos, multi-MB scans) are written in 1 MiB
        chunks rather than collected into a single ``bytes`` object
        in memory.

        Args:
            file_id: Drive file id.
            destination: Local filesystem destination.
            export_mime: Mime type to export to for Google-native
                files. ``None`` issues a raw ``get_media`` request.

        Raises:
            GoogleApiUnavailableError: When the optional Google
                libraries are not installed.
        """
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            msg = (
                "google-api-python-client is required for Drive downloads. "
                "Install it with `pip install google-api-python-client`."
            )
            raise GoogleApiUnavailableError(msg) from exc

        service = self._get_service()
        request = (
            service.files().export_media(fileId=file_id, mimeType=export_mime)
            if export_mime is not None
            else service.files().get_media(fileId=file_id)
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file and atomically rename on success.
        # If next_chunk() raises mid-stream, the destination is never
        # touched — the local mtime stays correctly older than Drive's
        # modified_time so the file is re-downloaded on the next run
        # rather than silently treated as up-to-date.
        tmp_path = destination.with_name(destination.name + ".download.tmp")
        try:  # noqa: TRY101  # Separate failure modes: optional-import check vs the actual download stream.
            with tmp_path.open("wb") as handle:
                downloader = MediaIoBaseDownload(
                    handle,
                    request,
                    chunksize=1024 * 1024,
                )
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
            os.replace(tmp_path, destination)
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

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
    ``0o644`` umask would leave it group/world readable. The function
    writes a fresh ``0o600`` sibling temp file and then ``os.replace``
    s it atomically over *path*. ``os.replace`` is the rename system
    call on POSIX, so the new file's mode (``0o600``) replaces the old
    file's mode in one step — closing the brief TOCTOU window that an
    in-place truncate-then-chmod sequence would leave open.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    # The mode argument to os.open applies only when O_CREAT actually
    # creates the file; combined with the unique tmp_path that means
    # the new file is owner-only from byte zero.
    fd = os.open(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# ---- OAuth token revocation (SEC-008) ---------------------------------


_REVOKE_URL: str = "https://oauth2.googleapis.com/revoke"
"""Google OAuth2 token revocation endpoint.

Reference: https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke.

Private — Google has rotated this URL before, so callers shouldn't
take a hard dependency on it. Tests that need the constant import it
under its private name explicitly.
"""


_REVOKE_TIMEOUT: float = 10.0
"""HTTP timeout for the best-effort revocation call."""


@dataclass(frozen=True)
class RevokeResult:
    """Outcome of a :func:`revoke_token` call.

    Attributes:
        token_file_existed: ``True`` if a token file was present at
            invocation time.
        token_file_removed: ``True`` if the token file was unlinked
            during this call.
        remote_revoked: ``True`` if Google's revocation endpoint
            confirmed the token was invalidated.
        error: Optional human-readable description of why the remote
            revocation did not succeed (network error, non-2xx status).
            ``None`` on full success or when no remote call was made.
    """

    token_file_existed: bool
    token_file_removed: bool
    remote_revoked: bool
    error: str | None = None


def _read_refresh_token(path: Path) -> str | None:
    """Best-effort read of the refresh token from a cached token file.

    The file is JSON written by ``Credentials.to_json()``; the field is
    typically ``refresh_token`` but older tokens may only carry the
    short-lived ``token`` field. Returns ``None`` if neither is
    present or the file is unreadable — revocation can still proceed
    locally even if the remote endpoint cannot be informed.

    Args:
        path: Cached token file location.

    Returns:
        The refresh-token string when found, otherwise ``None``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not parse token file %s for revocation: %s",
            path,
            exc,
        )
        return None
    candidate = data.get("refresh_token") or data.get("token")
    if not isinstance(candidate, str) or not candidate:
        return None
    return candidate


def _secure_erase(path: Path) -> bool:
    """Best-effort overwrite of *path* with zero bytes before unlinking.

    Modern SSDs and copy-on-write filesystems (APFS, btrfs, ZFS) cannot
    guarantee that the original bytes are unrecoverable — only that the
    visible file no longer references them. Writing zeros over the
    file before unlinking still defeats casual recovery tools that
    enumerate inodes. The unlink itself is unconditional: if the
    overwrite fails we still drop the directory entry rather than
    leaving the token in place.

    The return value lets callers tell the operator whether the file
    is actually gone — a previous version assumed it was, producing a
    false assurance whenever the unlink raised on read-only or
    permission-denied paths.

    Args:
        path: Token file to erase. Must exist when called.

    Returns:
        ``True`` when the directory entry was successfully removed,
        ``False`` when the unlink raised ``OSError``.
    """
    try:
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                # fsync is advisory; failure is non-fatal here.
                logger.debug("fsync of %s failed during secure erase", path)
    except OSError as exc:
        logger.warning(
            "Could not overwrite %s before unlink: %s",
            path,
            exc,
        )
    try:  # noqa: TRY101  # Separate failure modes: secure-overwrite vs the directory-entry unlink each have distinct fallbacks.
        path.unlink()
    except OSError as exc:
        logger.warning("Could not unlink %s: %s", path, exc)
        return False
    return True


def revoke_token(config: GoogleDriveConfig) -> RevokeResult:
    """Revoke the cached OAuth token and erase its on-disk copy.

    Best-effort by design (SEC-008): if the remote revocation call
    fails — network down, expired token, intermittent quota — the
    local file is still erased and unlinked so a future ``creek
    gdrive --download`` cannot reuse the cached credential. The
    caller (``creek gdrive --revoke``) is expected to surface the
    returned :class:`RevokeResult` so an operator can see whether a
    follow-up manual step (e.g. visiting Google's revocation page) is
    required.

    Args:
        config: Google Drive configuration whose ``token_file`` points
            at the cached credential.

    Returns:
        A :class:`RevokeResult` describing local and remote outcomes.
    """
    token_path = Path(config.token_file)
    existed = token_path.exists()
    refresh_token = _read_refresh_token(token_path) if existed else None
    removed = _secure_erase(token_path) if existed else False

    remote_revoked = False
    error: str | None = None
    if refresh_token:
        try:
            response = httpx.post(
                _REVOKE_URL,
                data={"token": refresh_token},
                timeout=_REVOKE_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            error = f"remote revocation failed: {type(exc).__name__}"
            logger.warning("%s", error)
        else:
            remote_revoked = response.is_success
            if not remote_revoked:
                error = f"revocation endpoint returned HTTP {response.status_code}"
                logger.warning("%s", error)
    elif existed:
        # File was on disk but unparseable / lacked a refresh token.
        # Without an `error` here the CLI would surface a confusing
        # ``confirm: None`` message to the operator.
        error = "no refresh token found in token file; remote revocation skipped"
        logger.warning("%s", error)

    return RevokeResult(
        token_file_existed=existed,
        token_file_removed=removed,
        remote_revoked=remote_revoked,
        error=error,
    )


# ---- Read-only doctor (issue #681) -------------------------------------


@dataclass(frozen=True)
class TokenInspection:
    """Local, non-secret view of a cached OAuth token.

    Carries only presence / validity / expiry — never the token, refresh
    token, or client secret. ``valid`` is ``None`` when validity cannot be
    determined locally (file absent, unparseable, or no ``expiry`` field);
    in that case reachability is decided by the live probe instead.

    Attributes:
        present: Whether a token file exists at the configured path.
        valid: ``True`` when well-formed and not yet expired, ``False``
            when expired, ``None`` when undeterminable locally.
        refreshable: Whether the token carries a refresh token (so the
            real downloader could refresh it non-interactively).
        expiry: The parsed expiry instant, or ``None`` when absent.
    """

    present: bool
    valid: bool | None
    refreshable: bool
    expiry: datetime | None


def _parse_token_expiry(raw: object) -> datetime | None:
    """Parse a token ``expiry`` string into an aware UTC datetime.

    Returns ``None`` for a missing or unparseable value. A trailing ``Z``
    is normalised to ``+00:00`` and naive timestamps are assumed UTC,
    matching the format google-auth writes.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def inspect_token(
    token_path: Path,
    *,
    now: datetime | None = None,
) -> TokenInspection:
    """Inspect a cached OAuth token file without exposing its contents.

    Reads only enough of the token file to report presence, local
    validity (well-formed and not expired), refreshability, and expiry.
    The token, refresh token, and client secret are never read into the
    return value or logged.

    Args:
        token_path: Path to the cached ``token.json``.
        now: Reference time for the expiry comparison; defaults to the
            current UTC time. Injected by tests for determinism.

    Returns:
        A :class:`TokenInspection` summarising local token state.
    """
    if not token_path.exists():
        return TokenInspection(
            present=False,
            valid=None,
            refreshable=False,
            expiry=None,
        )
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TokenInspection(
            present=True,
            valid=None,
            refreshable=False,
            expiry=None,
        )
    if not isinstance(data, dict):
        return TokenInspection(
            present=True,
            valid=None,
            refreshable=False,
            expiry=None,
        )
    refreshable = bool(data.get("refresh_token"))
    expiry = _parse_token_expiry(data.get("expiry"))
    if expiry is None:
        return TokenInspection(
            present=True,
            valid=None,
            refreshable=refreshable,
            expiry=None,
        )
    reference = now if now is not None else datetime.now(UTC)
    return TokenInspection(
        present=True,
        valid=expiry > reference,
        refreshable=refreshable,
        expiry=expiry,
    )


@dataclass(frozen=True)
class DriveDoctorReport:
    """Read-only diagnostic snapshot of the Drive connector's auth state.

    Produced by :func:`check_drive`. Every field is a boolean, count, or
    derived status string — never a secret. ``drive_reachable`` and
    ``listed_file_count`` are ``None`` when the live probe was skipped
    (no locally-valid token, or the optional libraries are missing).

    Attributes:
        credentials_present: Whether the OAuth client-secrets file exists.
        credentials_path: Configured path to that file (paths only).
        token_present: Whether a cached token file exists.
        token_path: Configured path to the token file.
        token_valid: Local token validity (see :class:`TokenInspection`).
        token_refreshable: Whether the token carries a refresh token.
        token_expiry: Parsed token expiry, or ``None``.
        libs_available: Whether the optional Google libraries import.
        drive_reachable: Result of the dry-run listing probe, or ``None``
            when the probe was skipped.
        listed_file_count: Number of files seen by the dry-run listing, or
            ``None`` when skipped.
        notes: Human-readable, secret-free status lines for display.
    """

    credentials_present: bool
    credentials_path: str
    token_present: bool
    token_path: str
    token_valid: bool | None
    token_refreshable: bool
    token_expiry: datetime | None
    libs_available: bool
    drive_reachable: bool | None
    listed_file_count: int | None
    notes: tuple[str, ...]


def _token_note(token: TokenInspection, token_path: str) -> str:
    """Render a secret-free human-readable status line for the token."""
    if not token.present:
        return (
            f"Token ({token_path}): absent — run "
            "`creek gdrive --download` once to authorise"
        )
    if token.valid is True:
        until = token.expiry.isoformat() if token.expiry else "unknown"
        return f"Token ({token_path}): present, valid until {until}"
    if token.valid is False:
        suffix = (
            " (refreshable — run `creek gdrive --download`)"
            if token.refreshable
            else ""
        )
        return f"Token ({token_path}): present but EXPIRED{suffix}"
    return f"Token ({token_path}): present but unreadable (no parseable expiry)"


def check_drive(
    config: GoogleDriveConfig,
    *,
    client: DriveClient,
    now: datetime | None = None,
) -> DriveDoctorReport:
    """Probe Drive auth + reachability read-only, downloading nothing.

    Reports credentials/token presence, local token validity, optional-
    library availability, and — only when a locally-valid token and the
    libraries are both present — a dry-run reachability listing via
    :meth:`DriveClient.list_files`. :meth:`DriveClient.download_to` is
    never called, and when no locally-valid token is present the live
    probe is skipped entirely, so no OAuth browser flow is triggered and
    nothing egresses.

    Args:
        config: The Drive configuration (paths + scopes). Only paths are
            read; file contents (tokens/secrets) are never surfaced.
        client: The read-only Drive backend. Injected so tests (and the
            CLI) can probe without the optional Google libraries.
        now: Reference time for token-expiry checks; defaults to current
            UTC time.

    Returns:
        A :class:`DriveDoctorReport` summarising the connector's state.
    """
    credentials_path = config.credentials_file
    token_path = config.token_file
    credentials_present = Path(credentials_path).exists()
    token = inspect_token(Path(token_path), now=now)
    libs_available = client.is_available()

    notes: list[str] = [
        f"Credentials ({credentials_path}): "
        + ("present" if credentials_present else "MISSING"),
        _token_note(token, token_path),
    ]

    drive_reachable: bool | None = None
    listed_file_count: int | None = None
    if not token.present:
        notes.append("Drive reachable: skipped (no token)")
    elif token.valid is not True:
        notes.append(
            "Drive reachable: skipped (token not valid; run --download to refresh)",
        )
    elif not libs_available:
        notes.append(
            "Drive reachable: skipped (google libraries not installed)",
        )
    else:
        try:
            files = client.list_files()
        except Exception as exc:
            # Mirror the download-loop pattern: the Drive read surface
            # spans HttpError (quota/rate/revoked), network IOErrors, and
            # OAuth failures, none importable at module top-level. Catch
            # broadly, log, and report unreachable rather than raising.
            logger.warning("Drive reachability probe failed: %s", exc)
            drive_reachable = False
            notes.append(f"Drive reachable: NO ({type(exc).__name__})")
        else:
            drive_reachable = True
            listed_file_count = len(files)
            notes.append(
                f"Drive reachable: yes ({listed_file_count} files listed, "
                "dry-run — nothing downloaded)",
            )

    return DriveDoctorReport(
        credentials_present=credentials_present,
        credentials_path=credentials_path,
        token_present=token.present,
        token_path=token_path,
        token_valid=token.valid,
        token_refreshable=token.refreshable,
        token_expiry=token.expiry,
        libs_available=libs_available,
        drive_reachable=drive_reachable,
        listed_file_count=listed_file_count,
        notes=tuple(notes),
    )


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

    def download_all(self, staging_dir: Path) -> DownloadResult:
        """Download every file in the listing to *staging_dir*.

        Files whose local mtime is at least as new as the Drive
        ``modified_time`` are skipped (incremental sync). Returns a
        :class:`DownloadResult` with separate ``downloaded`` /
        ``skipped`` / ``errors`` tuples; the path tuples carry the
        actual on-disk path (including any Google-native export suffix
        such as ``.docx``) so callers can route them through
        ``creek ingest`` reliably.

        Per-file errors are recorded into ``DownloadResult.errors``
        and the loop continues, so a transient quota or network blip
        on file N does not abandon the remaining files. The
        path-traversal guard still raises eagerly because malicious
        ``parent_path`` is a configuration bug, not a transient
        condition.

        Raises:
            ValueError: When any file's ``parent_path`` would escape
                *staging_dir* (path-traversal guard).
        """
        staging_dir.mkdir(parents=True, exist_ok=True)
        # Refresh cache so download_all always sees latest state.
        listing = self.list_files()
        downloaded: list[Path] = []
        skipped: list[Path] = []
        errors: list[tuple[DriveFile, Exception]] = []
        for drive_file in listing:
            target = self._target_path(drive_file, staging_dir)
            if self._is_up_to_date(target, drive_file):
                logger.info("Skipping unchanged Drive file: %s", drive_file.name)
                skipped.append(self._native_target(target, drive_file))
                continue
            try:
                downloaded.append(self._write(drive_file, staging_dir))
            except Exception as exc:
                logger.warning(
                    "Drive download failed for %s: %s",
                    drive_file.name,
                    exc,
                )
                errors.append((drive_file, exc))
        return DownloadResult(
            downloaded=tuple(downloaded),
            skipped=tuple(skipped),
            errors=tuple(errors),
        )

    def changed_files(self, staging_dir: Path) -> list[DriveFile]:
        """Return Drive files not already up-to-date in *staging_dir* (#683).

        The incremental set surfaced via the connector's
        ``list_changed_since`` — the same staging-mtime predicate
        :meth:`download_all` uses to skip unchanged files, expressed as a
        standalone listing (no download side effects).
        """
        return [
            drive_file
            for drive_file in self.list_files()
            if not self._is_up_to_date(
                self._target_path(drive_file, staging_dir),
                drive_file,
            )
        ]

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
        """Stream *drive_file* to disk under *staging_dir* and return the path.

        Writes are guarded so a failed ``download_to`` never leaves a
        partial file at the final path — without this, a stale partial
        whose mtime is now would shadow Drive's older ``modified_time``
        and be treated as up-to-date forever after.
        """
        target = self._target_path(drive_file, staging_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if drive_file.is_google_native:
            export_mime, suffix = _NATIVE_EXPORT_TARGETS[drive_file.mime_type]
            target = target.with_name(target.stem + suffix)
        else:
            export_mime = None
        try:
            self.client.download_to(
                drive_file.id,
                target,
                export_mime=export_mime,
            )
        except BaseException:
            if target.exists():
                target.unlink()
            raise
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


# ---- RemoteSourceConnector adapter (#683) ------------------------------


class GoogleDriveConnector:
    """Adapt the read-only Drive downloader to ``RemoteSourceConnector`` (#683).

    Wraps a :class:`GoogleDriveDownloader` (and its staging directory) so Drive
    satisfies the source-agnostic connector contract without changing any
    download behaviour. ``fetch_to`` delegates to ``download_all`` (whose own
    staging-mtime skip is unchanged); ``list_changed_since`` is driven by a
    **persisted cursor** (#684) — the last-seen Drive ``modified_time`` — so a
    fresh process or new host resumes incrementally. The cursor stores only a
    timestamp, never a credential.
    """

    def __init__(
        self,
        downloader: GoogleDriveDownloader,
        staging: Path,
        cursor_path: Path,
    ) -> None:
        """Bind the connector to a downloader, staging dir, and cursor file."""
        self._downloader = downloader
        self._staging = staging
        self._cursor_path = cursor_path

    def is_available(self) -> bool:
        """Report whether the optional Drive libraries are installed."""
        return self._downloader.client.is_available()

    def list_changed_since(self, cursor: object = None) -> list[DriveFile]:
        """Return Drive files modified strictly after *cursor* (#684).

        *cursor* is an ISO-8601 timestamp string (as :meth:`load_cursor`
        returns) or ``None`` for the first pass (everything). After
        :meth:`fetch_to` + :meth:`save_cursor`, the reloaded cursor equals the
        newest fetched ``modified_time``, so the next call returns nothing new.
        """
        if cursor is None:
            return self._downloader.list_files()
        threshold = datetime.fromisoformat(str(cursor))
        # Strict ">": files exactly at the cursor were fetched on the pass that
        # set it. A true tie at the newest modified_time is astronomically
        # unlikely given Drive's timestamp precision.
        return [
            drive_file
            for drive_file in self._downloader.list_files()
            if drive_file.modified_time > threshold
        ]

    def fetch_to(self, staging: Path) -> DownloadResult:
        """Download the changed files into *staging* (delegates to download_all)."""
        return self._downloader.download_all(staging)

    def load_cursor(self) -> str | None:
        """Return the persisted cursor timestamp, or ``None`` if unset/invalid.

        Any read failure, non-string value, or non-ISO timestamp (e.g. a
        hand-edited cursor file) falls back to ``None`` — a full re-scan —
        rather than raising later in :meth:`list_changed_since`.
        """
        if not self._cursor_path.exists():
            return None
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
            cursor = data.get("cursor") if isinstance(data, dict) else None
            if not isinstance(cursor, str):
                return None
            datetime.fromisoformat(cursor)  # validate shape only
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return cursor

    def save_cursor(self, fetched: Sequence[DriveFile]) -> None:
        """Advance the persisted cursor to the newest *fetched* modified_time.

        An empty *fetched* leaves the cursor untouched (nothing newer was
        seen). Written atomically (temp + ``os.replace``) and storing only the
        timestamp — no credentials.
        """
        if not fetched:
            return
        newest = max(item.modified_time for item in fetched)
        self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cursor_path.with_name(self._cursor_path.name + ".tmp")
        tmp.write_text(json.dumps({"cursor": newest.isoformat()}), encoding="utf-8")
        os.replace(tmp, self._cursor_path)


def build_drive_connector(
    config: GoogleDriveConfig,
    *,
    client: DriveClient | None = None,
    staging: Path | None = None,
    cursor_path: Path | None = None,
) -> GoogleDriveConnector:
    """Build a Drive :class:`GoogleDriveConnector` from *config* (#683/#684).

    Uses the provided *client* (or a fresh :class:`GoogleApiDriveClient`),
    *staging* (or ``config.staging_dir``), and *cursor_path* (or
    ``<staging>/.creek-connector-cursor.json``). The returned object satisfies
    the :class:`~creek.ingest.connectors.RemoteSourceConnector` protocol.
    """
    drive_client = client if client is not None else GoogleApiDriveClient(config)
    downloader = GoogleDriveDownloader(client=drive_client, config=config)
    resolved_staging = staging if staging is not None else Path(config.staging_dir)
    resolved_cursor = (
        cursor_path
        if cursor_path is not None
        else resolved_staging / ".creek-connector-cursor.json"
    )
    return GoogleDriveConnector(downloader, resolved_staging, resolved_cursor)


__all__ = [
    "GOOGLE_DOCS_MIME",
    "GOOGLE_SHEETS_MIME",
    "GOOGLE_SLIDES_MIME",
    "DownloadResult",
    "DriveClient",
    "DriveFile",
    "GoogleApiDriveClient",
    "GoogleApiUnavailableError",
    "GoogleDriveConnector",
    "GoogleDriveDownloader",
    "build_drive_connector",
    "route_to_ingestor",
]
