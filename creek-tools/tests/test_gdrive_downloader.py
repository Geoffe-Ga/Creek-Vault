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


_FORBIDDEN_DRIVE_METHODS: frozenset[str] = frozenset(
    {"update", "delete", "trash", "copy", "create"},
)


def _audit_no_write_calls(source: str) -> None:
    """Raise :class:`AssertionError` if *source* calls a forbidden Drive method.

    AST-based: ignores method names that appear inside docstrings,
    string literals, or comments. Only flags real call sites such as
    ``service.files().delete(...)``. Lives in the test module rather
    than in the production module so test infrastructure is not
    shipped as production code.
    """
    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_DRIVE_METHODS:
            msg = f"Forbidden write call detected: {func.attr}() at line {node.lineno}"
            raise AssertionError(msg)


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

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Write the canned bytes for *file_id* to *destination*.

        When *export_mime* is provided the (file_id, export_mime) entry
        in ``_exports`` is used; otherwise the bytes from ``_media``.
        """
        if export_mime is not None:
            self.calls.append(f"export_to:{file_id}:{export_mime}")
            data = self._exports[file_id, export_mime]
        else:
            self.calls.append(f"download_to:{file_id}")
            data = self._media[file_id]
        destination.write_bytes(data)


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

    def test_ast_audit_flags_create_call(self) -> None:
        """A `permissions().create(...)` call site is detected.

        The Protocol covers ``create`` as forbidden (Drive
        ``permissions().create()`` is the share-API write surface) so
        the source-level AST audit should also flag it. This ensures
        the two layers of read-only enforcement stay in sync.
        """

        sample = (
            "def bad(service):\n"
            "    return service.permissions().create(fileId='x').execute()\n"
        )
        with pytest.raises(AssertionError, match="create"):
            _audit_no_write_calls(sample)


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

    def test_download_to_raises_without_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """download_to raises with actionable instructions when deps missing."""
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
            client.download_to("file-id", tmp_path / "x.bin")

    def test_download_to_export_raises_without_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """download_to with an export_mime also raises when deps missing."""
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
            client.download_to(
                "file-id",
                tmp_path / "x.docx",
                export_mime="application/pdf",
            )


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
            ("manual.rtf", "document"),
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

    def test_regular_file_is_streamed_to_disk(
        self,
        tmp_path: Path,
    ) -> None:
        """A non-Google-native file is streamed straight to disk via download_to."""
        client = StubDriveClient(
            files=[_file(fid="abc", name="Notes.pdf")],
            media={"abc": b"%PDF-1.4 raw bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        path = downloader.download_file("abc", tmp_path)
        assert path == tmp_path / "Notes.pdf"
        assert path.read_bytes() == b"%PDF-1.4 raw bytes"
        assert "download_to:abc" in client.calls

    def test_google_doc_is_exported_to_docx(self, tmp_path: Path) -> None:
        """A Google Doc exports through download_to with export_mime to .docx."""
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
        assert f"export_to:g1:{docx_mime}" in client.calls

    def test_google_sheet_is_exported_to_xlsx(self, tmp_path: Path) -> None:
        """A Google Sheet exports through download_to to .xlsx."""
        xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        client = StubDriveClient(
            files=[_file(fid="s1", name="Budget", mime=GOOGLE_SHEETS_MIME)],
            exports={("s1", xlsx_mime): b"PK\x03\x04 xlsx bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        path = downloader.download_file("s1", tmp_path)
        assert path.name == "Budget.xlsx"

    def test_google_slides_is_exported_to_pptx(self, tmp_path: Path) -> None:
        """Google Slides exports through download_to to .pptx."""
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
        result = downloader.download_all(tmp_path)
        assert {p.name for p in result.all_paths} == {"a.pdf", "b.docx"}
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
        result = downloader.download_all(tmp_path)
        assert result.downloaded[0] == tmp_path / "Docs" / "2026" / "a.pdf"

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
        # Reset call log; second pass should not invoke download_to again.
        client.calls.clear()
        downloader.download_all(tmp_path)
        assert all(not c.startswith("download_to") for c in client.calls)

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
        assert any(c.startswith("download_to") for c in client.calls)
        assert (tmp_path / "a.pdf").read_bytes() == b"v2"

    def test_download_all_returns_separate_downloaded_and_skipped_lists(
        self,
        tmp_path: Path,
    ) -> None:
        """The return shape distinguishes freshly-downloaded vs skipped files."""
        modified = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        client = StubDriveClient(
            files=[
                _file(fid="a", name="a.pdf", modified=modified),
                _file(fid="b", name="b.pdf", modified=modified),
            ],
            media={"a": b"a", "b": b"b"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        first = downloader.download_all(tmp_path)
        assert len(first.downloaded) == 2
        assert first.skipped == ()

        # Re-run: nothing changed, both files should be skipped.
        second = downloader.download_all(tmp_path)
        assert second.downloaded == ()
        assert {p.name for p in second.skipped} == {"a.pdf", "b.pdf"}

    def test_skipped_google_native_file_keeps_export_suffix(
        self,
        tmp_path: Path,
    ) -> None:
        """An up-to-date Google Doc still reports its on-disk ``.docx`` path.

        Regression: previously the skip branch appended the un-suffixed
        target (``Letter``), so a caller piping the result through
        ``route_to_ingestor`` would mis-classify it.
        """
        docx_mime = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        modified = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        client = StubDriveClient(
            files=[
                _file(
                    fid="g1",
                    name="Letter",
                    mime=GOOGLE_DOCS_MIME,
                    modified=modified,
                ),
            ],
            exports={("g1", docx_mime): b"PK\x03\x04"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.download_all(tmp_path)  # first run downloads
        result = downloader.download_all(tmp_path)  # second run skips
        assert result.downloaded == ()
        assert len(result.skipped) == 1
        assert result.skipped[0].name == "Letter.docx"
        assert route_to_ingestor(result.skipped[0]) == "document"

    def test_download_all_returns_result_iterable_for_legacy_callers(
        self,
        tmp_path: Path,
    ) -> None:
        """`DownloadResult.all_paths` returns the union for callers that want it."""
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf")],
            media={"a": b"x"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        result = downloader.download_all(tmp_path)
        assert {p.name for p in result.all_paths} == {"a.pdf"}

    def test_download_result_lists_are_immutable(self, tmp_path: Path) -> None:
        """`DownloadResult` exposes tuples so callers cannot mutate the record.

        ``frozen=True`` only prevents field reassignment; mutable list
        attributes would still allow ``result.downloaded.append(...)``.
        Switching to tuples makes the value object truly immutable.
        """
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf")],
            media={"a": b"x"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        result = downloader.download_all(tmp_path)
        assert isinstance(result.downloaded, tuple)
        assert isinstance(result.skipped, tuple)
        assert isinstance(result.errors, tuple)

    def test_partial_failure_continues_and_records_per_file_errors(
        self,
        tmp_path: Path,
    ) -> None:
        """A mid-loop client failure records the error and continues.

        Without this, a 500-file sync that hits a transient quota error
        at file 250 would abort and leave the staging directory in a
        partial state with no programmatic record of how many files
        succeeded.
        """
        good = _file(fid="ok", name="good.pdf")
        bad = _file(fid="bad", name="bad.pdf")

        class _FlakyClient(StubDriveClient):
            def download_to(
                self,
                file_id: str,
                destination: Path,
                *,
                export_mime: str | None = None,
            ) -> None:
                if file_id == "bad":
                    msg = "simulated rate limit"
                    raise RuntimeError(msg)
                super().download_to(
                    file_id,
                    destination,
                    export_mime=export_mime,
                )

        client = _FlakyClient(
            files=[good, bad],
            media={"ok": b"good bytes"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        result = downloader.download_all(tmp_path)

        assert len(result.downloaded) == 1
        assert result.downloaded[0].name == "good.pdf"
        assert len(result.errors) == 1
        failed_file, exc = result.errors[0]
        assert failed_file.id == "bad"
        assert "rate limit" in str(exc)
        # The good file still landed.
        assert (tmp_path / "good.pdf").read_bytes() == b"good bytes"

    def test_partial_failure_does_not_leave_stale_destination_file(
        self,
        tmp_path: Path,
    ) -> None:
        """A failed download must not leave a half-written file at the target.

        Otherwise the next run sees ``local.mtime > drive.modified_time``
        (because the half-write happened *now*, after Drive's
        modified_time) and skips the file forever. Regression for the
        BLOCKING finding in PR #163's fourth review.
        """
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        old = _datetime(2024, 1, 1, tzinfo=_UTC)
        bad_file = _file(fid="bad", name="bad.pdf", modified=old)

        class _MidstreamFlakyClient(StubDriveClient):
            def download_to(
                self,
                file_id: str,
                destination: Path,
                *,
                export_mime: str | None = None,
            ) -> None:
                # Simulate writing some bytes then failing partway.
                if file_id == "bad":
                    destination.write_bytes(b"PARTIAL")
                    msg = "stream interrupted"
                    raise RuntimeError(msg)
                super().download_to(
                    file_id,
                    destination,
                    export_mime=export_mime,
                )

        client = _MidstreamFlakyClient(
            files=[bad_file],
            media={"bad": b"never-fully-written"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.download_all(tmp_path)

        # The destination file must not exist after a failed download —
        # otherwise its mtime would shadow Drive's older modified_time
        # and the file would be permanently skipped.
        assert not (tmp_path / "bad.pdf").exists()


# ---- Read-only audit ---------------------------------------------------


class TestReadOnlyContract:
    """The downloader never invokes write APIs against Drive."""

    def test_downloader_does_not_call_write_methods(self, tmp_path: Path) -> None:
        """A full cycle records only list_files / download_to / export_to calls."""
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


# ---- Module exports ----------------------------------------------------


class TestModuleExports:
    """The package re-exports the downloader's public surface."""

    def test_downloader_importable_from_package(self) -> None:
        """``from creek.ingest import GoogleDriveDownloader`` works."""
        from creek.ingest import (
            GoogleDriveDownloader as Reexported,
        )

        assert Reexported is GoogleDriveDownloader

    def test_drive_file_importable_from_package(self) -> None:
        """``DriveFile`` is part of the package's public surface."""
        from creek.ingest import DriveFile as Reexported

        assert Reexported is DriveFile

    def test_drive_client_importable_from_package(self) -> None:
        """``DriveClient`` is part of the package's public surface."""
        from creek.ingest import DriveClient as Reexported

        assert Reexported is DriveClient

    def test_unavailable_error_importable_from_package(self) -> None:
        """``GoogleApiUnavailableError`` is part of the public surface."""
        from creek.ingest import GoogleApiUnavailableError as Reexported

        assert Reexported is GoogleApiUnavailableError

    def test_route_to_ingestor_importable_from_package(self) -> None:
        """``route_to_ingestor`` is part of the public surface."""
        from creek import ingest as ingest_pkg

        assert ingest_pkg.route_to_ingestor is route_to_ingestor


# ---- Folder hierarchy resolution --------------------------------------


class TestFolderHierarchyResolution:
    """`_resolve_folder_paths` walks Drive folder IDs into slash-paths."""

    def test_root_folder_has_bare_name(self) -> None:
        """A folder with no parents resolves to just its name."""
        from creek.ingest.gdrive import _resolve_folder_paths

        folders = [{"id": "f1", "name": "Docs", "parents": []}]
        paths = _resolve_folder_paths(folders)
        assert paths == {"f1": "Docs"}

    def test_nested_folder_chain_resolves_to_slash_path(self) -> None:
        """A nested chain of folders resolves to ``Top/Mid/Bottom``."""
        from creek.ingest.gdrive import _resolve_folder_paths

        folders = [
            {"id": "f1", "name": "Docs", "parents": []},
            {"id": "f2", "name": "2026", "parents": ["f1"]},
            {"id": "f3", "name": "Drafts", "parents": ["f2"]},
        ]
        paths = _resolve_folder_paths(folders)
        assert paths["f3"] == "Docs/2026/Drafts"
        assert paths["f2"] == "Docs/2026"

    def test_folder_with_unknown_parent_treats_self_as_root(self) -> None:
        """An orphaned parent reference falls back to the folder's own name.

        Drive sometimes returns a parent id that is not visible to the
        user (e.g. a shared folder above the visible root); the safe
        behaviour is to anchor the orphan at the staging root.
        """
        from creek.ingest.gdrive import _resolve_folder_paths

        folders = [{"id": "f1", "name": "Orphan", "parents": ["unknown"]}]
        paths = _resolve_folder_paths(folders)
        assert paths == {"f1": "Orphan"}


class TestListFilesParentPathResolution:
    """`GoogleApiDriveClient.list_files` resolves folder hierarchy.

    The previous implementation hard-coded ``parent_path=""`` so files
    in different folders silently collided in the staging directory.
    These tests exercise the real `list_files` path against a fake
    service that mimics the Drive API surface.
    """

    @staticmethod
    def _make_fake_service(
        folders: list[dict[str, object]],
        files: list[dict[str, object]],
    ) -> object:
        """Return a stand-in for a Drive service with `.files().list()`."""

        class _Request:
            def __init__(self, payload: dict[str, object]) -> None:
                self._payload = payload

            def execute(self) -> dict[str, object]:
                return self._payload

        class _Files:
            def list(self, **kwargs: object) -> _Request:
                q = str(kwargs.get("q", ""))
                # The folder-listing query starts with ``mimeType=``;
                # the file-listing query uses ``mimeType!=``.
                if "mimeType=" in q and "mimeType!=" not in q:
                    return _Request({"files": folders})
                return _Request({"files": files})

        class _Service:
            def files(self) -> _Files:
                return _Files()

        return _Service()

    def test_files_inherit_parent_folder_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file whose parent is a known folder gets the folder's path."""
        folders = [
            {"id": "f1", "name": "Docs", "parents": []},
            {"id": "f2", "name": "2026", "parents": ["f1"]},
        ]
        files = [
            {
                "id": "a",
                "name": "a.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-04-01T00:00:00Z",
                "size": 10,
                "parents": ["f2"],
            },
        ]
        client = GoogleApiDriveClient(GoogleDriveConfig())
        monkeypatch.setattr(
            client,
            "_get_service",
            lambda: self._make_fake_service(folders, files),
        )
        listed = client.list_files()
        assert len(listed) == 1
        assert listed[0].parent_path == "Docs/2026"

    def test_files_at_drive_root_have_empty_parent_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A file at the Drive root keeps an empty parent_path."""
        files = [
            {
                "id": "a",
                "name": "a.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-04-01T00:00:00Z",
                "size": 10,
                "parents": [],
            },
        ]
        client = GoogleApiDriveClient(GoogleDriveConfig())
        monkeypatch.setattr(
            client,
            "_get_service",
            lambda: self._make_fake_service([], files),
        )
        listed = client.list_files()
        assert listed[0].parent_path == ""


# ---- Token file permissions ------------------------------------------


class TestTokenFilePermissions:
    """`_write_token_file` lays the OAuth token down at mode 0600."""

    def test_token_file_has_owner_only_permissions(self, tmp_path: Path) -> None:
        """The cached token is not world- or group-readable."""
        import stat

        from creek.ingest.gdrive import _write_token_file

        token_path = tmp_path / "token.json"
        _write_token_file(token_path, '{"refresh_token": "secret"}')
        mode = token_path.stat().st_mode & 0o777
        assert mode == stat.S_IRUSR | stat.S_IWUSR  # 0o600

    def test_token_file_overwrites_existing(self, tmp_path: Path) -> None:
        """Re-writing replaces the file rather than appending."""
        from creek.ingest.gdrive import _write_token_file

        token_path = tmp_path / "token.json"
        _write_token_file(token_path, "{}")
        _write_token_file(token_path, '{"refresh_token": "second"}')
        assert token_path.read_text(encoding="utf-8") == '{"refresh_token": "second"}'

    def test_overwrite_of_world_readable_file_lands_at_0o600(
        self,
        tmp_path: Path,
    ) -> None:
        """Replacing a 0o644 token file leaves the new file at 0o600.

        Closes the TOCTOU window where a previous run's wider-permission
        token file could briefly contain the new refresh token before
        the chmod tightened it. Atomic rename writes a fresh 0o600
        sibling and atomically replaces the old file.
        """
        import stat

        from creek.ingest.gdrive import _write_token_file

        token_path = tmp_path / "token.json"
        token_path.write_text("{}", encoding="utf-8")
        token_path.chmod(0o644)
        _write_token_file(token_path, '{"refresh_token": "new-secret"}')
        mode = token_path.stat().st_mode & 0o777
        assert mode == stat.S_IRUSR | stat.S_IWUSR  # 0o600
        assert (
            token_path.read_text(encoding="utf-8") == '{"refresh_token": "new-secret"}'
        )


# ---- Path traversal guard --------------------------------------------


class TestPathTraversalGuard:
    """`download_all` rejects parent_paths that escape the staging root."""

    def test_dotdot_parent_path_is_rejected(self, tmp_path: Path) -> None:
        """A ``..``-laden parent_path raises ValueError before any I/O."""
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf", parent_path="../../etc")],
            media={"a": b"x"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        with pytest.raises(ValueError, match="escapes staging"):
            downloader.download_all(tmp_path)

    def test_absolute_parent_path_is_rejected(self, tmp_path: Path) -> None:
        """An absolute parent_path also escapes the staging root."""
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf", parent_path="/etc/passwd-dir")],
            media={"a": b"x"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        with pytest.raises(ValueError, match="escapes staging"):
            downloader.download_all(tmp_path)

    def test_normal_relative_parent_path_is_allowed(self, tmp_path: Path) -> None:
        """A plain relative path lands inside staging and writes successfully."""
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf", parent_path="Docs/2026")],
            media={"a": b"x"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        result = downloader.download_all(tmp_path)
        assert result.downloaded[0] == tmp_path / "Docs" / "2026" / "a.pdf"


# ---- list_files caching ----------------------------------------------


class TestListFilesCache:
    """`download_file` does not re-list on every invocation."""

    def test_repeated_download_file_calls_share_one_listing(
        self,
        tmp_path: Path,
    ) -> None:
        """Two ``download_file`` calls produce only one ``list_files`` call."""
        client = StubDriveClient(
            files=[
                _file(fid="a", name="a.pdf"),
                _file(fid="b", name="b.pdf"),
            ],
            media={"a": b"a", "b": b"b"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.download_file("a", tmp_path)
        downloader.download_file("b", tmp_path)
        list_calls = [c for c in client.calls if c == "list_files"]
        assert len(list_calls) == 1

    def test_explicit_list_files_always_fetches_from_client(
        self,
        tmp_path: Path,
    ) -> None:
        """`list_files()` is a live call — every invocation hits the client.

        The internal cache is for the ``download_file`` fast-path only;
        a direct ``list_files()`` from a caller refreshes it.
        """
        client = StubDriveClient(
            files=[_file(fid="a", name="a.pdf")],
            media={"a": b"a"},
        )
        downloader = GoogleDriveDownloader(client=client, config=GoogleDriveConfig())
        downloader.list_files()
        downloader.list_files()
        list_calls = [c for c in client.calls if c == "list_files"]
        assert len(list_calls) == 2


# ---- OAuth token revoke (SEC-008) -------------------------------------


class _StubResponse:
    """Minimal stand-in for httpx.Response inside the revoke tests."""

    def __init__(self, status_code: int) -> None:
        """Capture the response status code for ``is_success`` and inspection."""
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300


class TestRevokeToken:
    """`revoke_token` deletes the local token and best-effort revokes upstream."""

    def test_removes_existing_token_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cached token file is unlinked after revocation."""
        from creek.ingest.gdrive import revoke_token

        token = tmp_path / "token.json"
        token.write_text('{"refresh_token": "rt-abc"}', encoding="utf-8")
        config = GoogleDriveConfig(token_file=str(token))

        monkeypatch.setattr(
            "creek.ingest.gdrive.httpx.post",
            lambda *_a, **_kw: _StubResponse(200),
        )

        result = revoke_token(config)

        assert result.token_file_existed is True
        assert result.token_file_removed is True
        assert result.remote_revoked is True
        assert not token.exists()

    def test_handles_missing_token_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Revoking when no token is cached is a clean no-op."""
        from creek.ingest.gdrive import revoke_token

        token = tmp_path / "token.json"
        config = GoogleDriveConfig(token_file=str(token))

        called = {"n": 0}

        def _post(*_a: object, **_kw: object) -> _StubResponse:
            called["n"] += 1
            return _StubResponse(200)

        monkeypatch.setattr("creek.ingest.gdrive.httpx.post", _post)

        result = revoke_token(config)

        assert result.token_file_existed is False
        assert result.token_file_removed is False
        assert result.remote_revoked is False
        # No remote revoke is attempted when there's no token to revoke.
        assert called["n"] == 0

    def test_calls_google_revoke_endpoint_with_refresh_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cached refresh token is POSTed to Google's revocation URL."""
        from creek.ingest.gdrive import REVOKE_URL, revoke_token

        token = tmp_path / "token.json"
        token.write_text('{"refresh_token": "rt-secret"}', encoding="utf-8")
        config = GoogleDriveConfig(token_file=str(token))

        captured: list[tuple[str, dict[str, str] | None]] = []

        def _post(
            url: str,
            data: dict[str, str] | None = None,
            **_kw: object,
        ) -> _StubResponse:
            captured.append((url, data))
            return _StubResponse(200)

        monkeypatch.setattr("creek.ingest.gdrive.httpx.post", _post)

        revoke_token(config)

        assert captured == [(REVOKE_URL, {"token": "rt-secret"})]

    def test_continues_local_delete_when_remote_endpoint_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A network failure must not block local deletion of the token file."""
        import httpx

        from creek.ingest.gdrive import revoke_token

        token = tmp_path / "token.json"
        token.write_text('{"refresh_token": "rt-abc"}', encoding="utf-8")
        config = GoogleDriveConfig(token_file=str(token))

        def _boom(*_a: object, **_kw: object) -> _StubResponse:
            msg = "network down"
            raise httpx.ConnectError(msg)

        monkeypatch.setattr("creek.ingest.gdrive.httpx.post", _boom)

        result = revoke_token(config)

        assert not token.exists()
        assert result.token_file_removed is True
        assert result.remote_revoked is False
        assert result.error is not None

    def test_overwrites_token_bytes_before_unlink(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Best-effort secure erase: bytes are zeroed before unlinking.

        We monkeypatch ``Path.unlink`` to leave the file in place so we
        can inspect its contents and confirm the secure-erase pass ran.
        """
        from creek.ingest.gdrive import revoke_token

        token = tmp_path / "token.json"
        original = '{"refresh_token": "rt-very-secret"}'
        token.write_text(original, encoding="utf-8")
        config = GoogleDriveConfig(token_file=str(token))

        monkeypatch.setattr(
            "creek.ingest.gdrive.httpx.post",
            lambda *_a, **_kw: _StubResponse(200),
        )
        # Suppress the unlink so the overwritten bytes survive long
        # enough for us to inspect them.
        monkeypatch.setattr(
            "creek.ingest.gdrive.Path.unlink",
            lambda _self, **_kw: None,
        )

        revoke_token(config)

        on_disk = token.read_bytes()
        assert "rt-very-secret" not in on_disk.decode("utf-8", errors="replace")
        assert on_disk == b"\x00" * len(original)


# ---- AST-based read-only audit ---------------------------------------


class TestAstReadOnlyAudit:
    """`_audit_no_write_calls` flags only real call sites, not strings/comments."""

    def test_audit_passes_on_clean_module_source(self) -> None:
        """The current gdrive module passes the AST audit."""
        from creek.ingest import gdrive

        source = __import__("pathlib").Path(gdrive.__file__).read_text(encoding="utf-8")
        _audit_no_write_calls(source)

    def test_audit_ignores_method_name_in_docstring(self) -> None:
        """A docstring mentioning ``.delete(`` does not trip the audit."""

        sample = (
            "def foo():\n"
            '    """Docstring discussing a hypothetical .delete( call."""\n'
            "    return 1\n"
        )
        # Should not raise.
        _audit_no_write_calls(sample)

    def test_audit_flags_real_write_call(self) -> None:
        """A genuine ``.delete(`` call site raises a clear AssertionError."""

        sample = (
            "def bad(service):\n"
            "    return service.files().delete(fileId='x').execute()\n"
        )
        with pytest.raises(AssertionError, match="delete"):
            _audit_no_write_calls(sample)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
