"""Tests for the read-only Google Drive downloader (#56)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from creek.config import GoogleDriveConfig
from creek.ingest.gdrive import (
    GOOGLE_DOCS_MIME,
    GOOGLE_SHEETS_MIME,
    GOOGLE_SLIDES_MIME,
    DriveClient,
    DriveFile,
    GoogleApiDriveClient,
    GoogleApiUnavailableError,
    GoogleDriveDownloader,
    route_to_ingestor,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---- Stub client --------------------------------------------------------


class StubDriveClient:
    """Deterministic in-memory Drive client for tests.

    Tracks which methods were called so tests can assert that no write
    methods were ever invoked on the real Drive client.
    """

    def __init__(
        self,
        files: list[DriveFile] | None = None,
        media: dict[str, bytes] | None = None,
        exports: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        """Seed listing, raw media, and export media keyed by id (and mime)."""
        self._files = list(files or [])
        self._media = dict(media or {})
        self._exports = dict(exports or {})
        self.calls: list[str] = []

    def is_available(self) -> bool:
        """Stub clients are always available."""
        return True

    def list_files(self) -> list[DriveFile]:
        """Return the canned listing."""
        self.calls.append("list_files")
        return list(self._files)

    def get_media(self, file_id: str) -> bytes:
        """Return raw media bytes for *file_id*."""
        self.calls.append(f"get_media:{file_id}")
        return self._media[file_id]

    def export_media(self, file_id: str, mime_type: str) -> bytes:
        """Return exported media bytes for *file_id* + *mime_type*."""
        self.calls.append(f"export_media:{file_id}:{mime_type}")
        return self._exports[file_id, mime_type]


# ---- DriveFile ---------------------------------------------------------


class TestDriveFile:
    """Invariants on the :class:`DriveFile` dataclass."""

    def test_drive_file_carries_required_fields(self) -> None:
        """All metadata fields the routing logic needs are present."""
        df = DriveFile(
            id="abc",
            name="Notes.docx",
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            modified_time=datetime(2026, 1, 1, tzinfo=UTC),
            size=1024,
            parent_path="Docs/2026",
        )
        assert df.id == "abc"
        assert df.name == "Notes.docx"
        assert df.parent_path == "Docs/2026"

    def test_is_google_native_recognises_docs_sheets_slides(self) -> None:
        """``is_google_native`` is True for the three google-apps mimes."""
        for mime in (GOOGLE_DOCS_MIME, GOOGLE_SHEETS_MIME, GOOGLE_SLIDES_MIME):
            df = DriveFile(
                id="x",
                name="x",
                mime_type=mime,
                modified_time=datetime(2026, 1, 1, tzinfo=UTC),
                size=0,
                parent_path="",
            )
            assert df.is_google_native is True

    def test_is_google_native_false_for_regular_files(self) -> None:
        """Plain ``.docx`` / ``.pdf`` are not Google-native."""
        df = DriveFile(
            id="x",
            name="x.pdf",
            mime_type="application/pdf",
            modified_time=datetime(2026, 1, 1, tzinfo=UTC),
            size=0,
            parent_path="",
        )
        assert df.is_google_native is False


# ---- DriveClient protocol contract ------------------------------------


class TestDriveClientProtocol:
    """`DriveClient` exposes only read-side methods.

    The Creek ontology forbids any write/update/delete/trash/copy
    operation against the user's Drive. The Protocol enforces this by
    construction — nothing on it should mutate.
    """

    def test_stub_satisfies_protocol(self) -> None:
        """A stub client is a structural :class:`DriveClient`."""
        assert isinstance(StubDriveClient(), DriveClient)

    def test_protocol_has_no_write_methods(self) -> None:
        """Forbidden write surface is absent from the Protocol."""
        forbidden = {"update", "delete", "trash", "copy", "create"}
        attrs = set(dir(DriveClient))
        for name in forbidden:
            assert name not in attrs, name


# ---- GoogleApiDriveClient ---------------------------------------------


def _make_import_blocker(blocked: set[str]) -> object:
    """Build a ``__import__`` replacement that raises for *blocked*."""
    import builtins

    real_import = builtins.__import__

    def _blocked_import(
        name: str,
        module_globals: dict[str, object] | None = None,
        module_locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        """Raise ImportError for blocked roots; defer everything else."""
        root = name.split(".", 1)[0]
        if root in blocked or name in blocked:
            msg = f"mocked-missing: {name}"
            raise ImportError(msg)
        return real_import(name, module_globals, module_locals, fromlist, level)

    return _blocked_import


class TestGoogleApiDriveClient:
    """Default Drive client respects the optional-dep contract."""

    def test_is_available_returns_false_without_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without google-api-python-client, the client reports unavailable."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker(
                {"googleapiclient", "google_auth_oauthlib", "google"},
            ),
        )
        client = GoogleApiDriveClient(GoogleDriveConfig())
        assert not client.is_available()

    def test_list_files_raises_without_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """list_files raises GoogleApiUnavailableError when deps missing."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker(
                {"googleapiclient", "google_auth_oauthlib", "google"},
            ),
        )
        client = GoogleApiDriveClient(GoogleDriveConfig())
        with pytest.raises(GoogleApiUnavailableError, match="google-api-python-client"):
            client.list_files()

    def test_get_media_raises_without_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """get_media raises with actionable instructions when deps missing."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker(
                {"googleapiclient", "google_auth_oauthlib", "google"},
            ),
        )
        client = GoogleApiDriveClient(GoogleDriveConfig())
        with pytest.raises(GoogleApiUnavailableError):
            client.get_media("file-id")

    def test_export_media_raises_without_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """export_media raises with actionable instructions when deps missing."""
        import builtins

        monkeypatch.setattr(
            builtins,
            "__import__",
            _make_import_blocker(
                {"googleapiclient", "google_auth_oauthlib", "google"},
            ),
        )
        client = GoogleApiDriveClient(GoogleDriveConfig())
        with pytest.raises(GoogleApiUnavailableError):
            client.export_media("file-id", "application/pdf")


# ---- route_to_ingestor ------------------------------------------------


class TestRouteToIngestor:
    """`route_to_ingestor` maps file extensions to ingestor keys."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("notes.md", "markdown"),
            ("memo.docx", "document"),
            ("scan.pdf", "document"),
            ("page.html", "document"),
            ("readme.txt", "document"),
            ("budget.xlsx", "spreadsheet"),
            ("data.csv", "spreadsheet"),
            ("deck.pptx", "presentation"),
            ("photo.jpg", "image"),
            ("shot.PNG", "image"),
        ],
    )
    def test_extension_routing(self, name: str, expected: str) -> None:
        """Each well-known extension routes to the canonical ingestor key."""
        from pathlib import Path

        assert route_to_ingestor(Path(name)) == expected

    def test_unknown_extension_routes_to_generic(self) -> None:
        """Unrecognised extensions fall through to the generic ingestor."""
        from pathlib import Path

        assert route_to_ingestor(Path("mystery.xyz")) == "generic"


# ---- GoogleDriveDownloader.download_file ------------------------------


def _file(
    *,
    fid: str,
    name: str,
    mime: str = "application/pdf",
    modified: datetime | None = None,
    size: int = 0,
    parent_path: str = "",
) -> DriveFile:
    """Build a DriveFile with sensible defaults for tests."""
    return DriveFile(
        id=fid,
        name=name,
        mime_type=mime,
        modified_time=modified or datetime(2026, 4, 1, tzinfo=UTC),
        size=size,
        parent_path=parent_path,
    )


class TestDownloadFile:
    """`download_file` handles regular files and Google-native exports."""

    def test_regular_file_is_downloaded_via_get_media(
        self,
        tmp_path: Path,
    ) -> None:
        """A non-Google-native file calls ``get_media`` and writes bytes."""
        client = StubDriveClient(
            files=[_file(fid="abc", name="Notes.pdf")],
            media={"abc": b"%PDF-1.4 raw bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        path = downloader.download_file("abc", tmp_path)
        assert path == tmp_path / "Notes.pdf"
        assert path.read_bytes() == b"%PDF-1.4 raw bytes"
        assert "get_media:abc" in client.calls

    def test_google_doc_is_exported_to_docx(self, tmp_path: Path) -> None:
        """A Google Doc exports through ``export_media`` to .docx bytes."""
        docx_mime = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        client = StubDriveClient(
            files=[_file(fid="g1", name="Letter", mime=GOOGLE_DOCS_MIME)],
            exports={("g1", docx_mime): b"PK\x03\x04 docx bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        path = downloader.download_file("g1", tmp_path)
        assert path.name == "Letter.docx"
        assert path.read_bytes().startswith(b"PK")
        assert f"export_media:g1:{docx_mime}" in client.calls

    def test_google_sheet_is_exported_to_xlsx(self, tmp_path: Path) -> None:
        """A Google Sheet exports through ``export_media`` to .xlsx bytes."""
        xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        client = StubDriveClient(
            files=[_file(fid="s1", name="Budget", mime=GOOGLE_SHEETS_MIME)],
            exports={("s1", xlsx_mime): b"PK\x03\x04 xlsx bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        path = downloader.download_file("s1", tmp_path)
        assert path.name == "Budget.xlsx"

    def test_google_slides_is_exported_to_pptx(self, tmp_path: Path) -> None:
        """Google Slides exports through ``export_media`` to .pptx bytes."""
        pptx_mime = (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
        client = StubDriveClient(
            files=[_file(fid="p1", name="Deck", mime=GOOGLE_SLIDES_MIME)],
            exports={("p1", pptx_mime): b"PK\x03\x04 pptx bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        path = downloader.download_file("p1", tmp_path)
        assert path.name == "Deck.pptx"

    def test_unknown_file_id_raises_keyerror(self, tmp_path: Path) -> None:
        """Asking for a missing id raises a clear KeyError."""
        client = StubDriveClient(files=[_file(fid="known", name="x.pdf")])
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        with pytest.raises(KeyError, match="missing"):
            downloader.download_file("missing", tmp_path)


# ---- GoogleDriveDownloader.download_all -------------------------------


class TestDownloadAll:
    """`download_all` walks the listing and writes every file."""

    def test_downloads_all_files_to_staging(self, tmp_path: Path) -> None:
        """All listed files land under the staging directory."""
        client = StubDriveClient(
            files=[
                _file(fid="a", name="a.pdf"),
                _file(fid="b", name="b.docx", mime="application/vnd.openxmlformats"),
            ],
            media={"a": b"a", "b": b"b"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        paths = downloader.download_all(tmp_path)
        assert {p.name for p in paths} == {"a.pdf", "b.docx"}
        assert (tmp_path / "a.pdf").read_bytes() == b"a"

    def test_preserves_folder_structure(self, tmp_path: Path) -> None:
        """Files retain their Drive folder hierarchy under staging."""
        client = StubDriveClient(
            files=[
                _file(fid="a", name="a.pdf", parent_path="Docs/2026"),
            ],
            media={"a": b"a"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        paths = downloader.download_all(tmp_path)
        assert paths[0] == tmp_path / "Docs" / "2026" / "a.pdf"

    def test_skips_unchanged_files_on_subsequent_runs(
        self,
        tmp_path: Path,
    ) -> None:
        """Files whose mtime matches the Drive modified_time are skipped.

        On a second invocation, only newer/missing files should be
        re-fetched — incremental sync, not full re-download.
        """
        modified = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf", modified=modified)],
            media={"a": b"first"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.download_all(tmp_path)
        # Reset call log; second pass should not call get_media again.
        client.calls.clear()
        downloader.download_all(tmp_path)
        assert all(not c.startswith("get_media") for c in client.calls)

    def test_redownloads_when_drive_file_is_newer(self, tmp_path: Path) -> None:
        """A bumped modified_time triggers re-download."""
        old = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf", modified=old)],
            media={"a": b"v1"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.download_all(tmp_path)
        # Bump remote modified_time; v2 bytes should land.
        new = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        client._files = [_file(fid="a", name="a.pdf", modified=new)]
        client._media = {"a": b"v2"}
        client.calls.clear()
        downloader.download_all(tmp_path)
        assert any(c.startswith("get_media") for c in client.calls)
        assert (tmp_path / "a.pdf").read_bytes() == b"v2"


# ---- Read-only audit ---------------------------------------------------


class TestReadOnlyContract:
    """The downloader never invokes write APIs against Drive."""

    def test_downloader_does_not_call_write_methods(self, tmp_path: Path) -> None:
        """A full download cycle records only list/get/export_media calls."""
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf")],
            media={"a": b"x"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.list_files()
        downloader.download_all(tmp_path)
        forbidden = ("update", "delete", "trash", "copy", "create")
        for call in client.calls:
            for method in forbidden:
                assert method not in call.split(":", 1)[0], call

    def test_module_source_has_no_write_method_calls(self) -> None:
        """The module source code never references forbidden Drive write methods.

        A code-audit assertion: per ontology §3.4 the downloader
        architecture is read-only by construction. This test grep-style
        scans the implementation to ensure no future refactor sneaks
        in a write call.
        """
        from creek.ingest import gdrive

        source = (
            __import__("pathlib")
            .Path(gdrive.__file__)
            .read_text(
                encoding="utf-8",
            )
        )
        for forbidden in (
            ".update(",
            ".delete(",
            ".trash(",
            ".copy(",
            "permissions().create(",
        ):
            assert forbidden not in source, forbidden


# ---- Module exports ----------------------------------------------------


class TestModuleExports:
    """The package re-exports the downloader's public surface."""

    def test_downloader_importable_from_package(self) -> None:
        """``from creek.ingest import GoogleDriveDownloader`` works."""
        from creek.ingest import (
            GoogleDriveDownloader as Reexported,
        )

        assert Reexported is GoogleDriveDownloader


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
