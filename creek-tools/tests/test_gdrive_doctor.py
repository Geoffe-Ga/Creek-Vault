"""Tests for the read-only ``creek gdrive --check`` doctor (issue #681).

The doctor reports credentials/token presence, local token validity, library
availability, and a dry-run reachability listing — **without** downloading or
egressing anything when auth is absent. These tests pin that contract via
injected :class:`DriveClient` stubs and assert ``download_to`` is never called.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app
from creek.config import CreekConfig, GoogleDriveConfig
from creek.ingest.gdrive import (
    DriveDoctorReport,
    DriveFile,
    TokenInspection,
    check_drive,
    inspect_token,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

runner = CliRunner()

# A fixed "now" so token-expiry comparisons are deterministic.
_NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=UTC)
_FUTURE = "2099-01-01T00:00:00Z"
_PAST = "2000-01-01T00:00:00Z"


def _write_token(
    path: Path,
    *,
    expiry: str | None = _FUTURE,
    refresh: bool = True,
) -> Path:
    """Write a google-shaped authorized-user token file and return its path.

    The access/refresh token values are placeholders — the doctor must never
    read or surface them, only presence/validity/expiry.
    """
    data: dict[str, object] = {
        "token": "PLACEHOLDER-ACCESS",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    }
    if refresh:
        data["refresh_token"] = "PLACEHOLDER-REFRESH"
    if expiry is not None:
        data["expiry"] = expiry
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _drive_file(name: str) -> DriveFile:
    """Build a minimal :class:`DriveFile` for listing assertions."""
    return DriveFile(
        id=f"id-{name}",
        name=name,
        mime_type="text/markdown",
        modified_time=datetime(2026, 4, 1, tzinfo=UTC),
        size=10,
        parent_path="",
    )


class _DoctorStub:
    """In-memory :class:`DriveClient` that records every method call.

    Lets the doctor tests prove that only ``is_available`` + ``list_files``
    are ever invoked and ``download_to`` never is.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        files: list[DriveFile] | None = None,
        raise_on_list: Exception | None = None,
    ) -> None:
        """Seed availability, the canned listing, and an optional list error."""
        self._available = available
        self._files = list(files or [])
        self._raise = raise_on_list
        self.calls: list[str] = []

    def is_available(self) -> bool:
        """Record the call and report seeded availability."""
        self.calls.append("is_available")
        return self._available

    def list_files(self) -> list[DriveFile]:
        """Record the call; raise the seeded error or return the listing."""
        self.calls.append("list_files")
        if self._raise is not None:
            raise self._raise
        return list(self._files)

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Record a (forbidden-for-the-doctor) download attempt."""
        self.calls.append(f"download_to:{file_id}")


def _no_downloads(stub: _DoctorStub) -> bool:
    """Return True when the stub recorded no ``download_to`` call."""
    return not any(c.startswith("download_to") for c in stub.calls)


# ---- inspect_token (pure, deterministic) -------------------------------


class TestInspectToken:
    """Local token inspection never exposes the token value."""

    def test_absent_token(self, tmp_path: Path) -> None:
        """A missing token file is reported absent, validity unknown."""
        insp = inspect_token(tmp_path / "token.json", now=_NOW)
        assert insp == TokenInspection(
            present=False, valid=None, refreshable=False, expiry=None
        )

    def test_valid_future_token(self, tmp_path: Path) -> None:
        """A well-formed, non-expired token is present + valid + refreshable."""
        path = _write_token(tmp_path / "token.json", expiry=_FUTURE)
        insp = inspect_token(path, now=_NOW)
        assert insp.present is True
        assert insp.valid is True
        assert insp.refreshable is True
        assert insp.expiry == datetime(2099, 1, 1, tzinfo=UTC)

    def test_expired_token(self, tmp_path: Path) -> None:
        """A past-expiry token is present but not valid (still refreshable)."""
        path = _write_token(tmp_path / "token.json", expiry=_PAST)
        insp = inspect_token(path, now=_NOW)
        assert insp.present is True
        assert insp.valid is False
        assert insp.refreshable is True

    def test_malformed_token_is_unknown(self, tmp_path: Path) -> None:
        """Unparseable JSON yields present=True but valid=None (unknown)."""
        path = tmp_path / "token.json"
        path.write_text("{not json", encoding="utf-8")
        insp = inspect_token(path, now=_NOW)
        assert insp.present is True
        assert insp.valid is None

    def test_missing_expiry_is_unknown(self, tmp_path: Path) -> None:
        """A token without an expiry field has unknown validity."""
        path = _write_token(tmp_path / "token.json", expiry=None)
        insp = inspect_token(path, now=_NOW)
        assert insp.present is True
        assert insp.valid is None
        assert insp.expiry is None

    def test_inspection_exposes_no_secret_fields(self, tmp_path: Path) -> None:
        """The inspection dataclass carries only presence/validity/expiry."""
        path = _write_token(tmp_path / "token.json")
        insp = inspect_token(path, now=_NOW)
        assert set(vars(insp)) == {"present", "valid", "refreshable", "expiry"}

    def test_default_now_uses_current_time(self, tmp_path: Path) -> None:
        """Omitting *now* compares against the real clock (far-future = valid)."""
        path = _write_token(tmp_path / "token.json", expiry=_FUTURE)
        insp = inspect_token(path)  # no now -> datetime.now(UTC)
        assert insp.valid is True


# ---- check_drive (read-only doctor) ------------------------------------


class TestCheckDrive:
    """The doctor probes read-only and never downloads."""

    def test_token_absent_skips_reachability_and_never_lists(
        self, tmp_path: Path
    ) -> None:
        """With no token, reachability is skipped and list_files is never called."""
        config = GoogleDriveConfig(
            credentials_file=str(tmp_path / "credentials.json"),
            token_file=str(tmp_path / "token.json"),
        )
        stub = _DoctorStub(available=True, files=[_drive_file("doc.md")])
        report = check_drive(config, client=stub, now=_NOW)

        assert isinstance(report, DriveDoctorReport)
        assert report.credentials_present is False
        assert report.token_present is False
        assert report.drive_reachable is None  # skipped
        assert report.listed_file_count is None
        assert "list_files" not in stub.calls  # the safety guarantee
        assert _no_downloads(stub)

    def test_libs_missing_skips_reachability(self, tmp_path: Path) -> None:
        """A valid token but missing libs skips the live probe."""
        config = GoogleDriveConfig(
            token_file=str(_write_token(tmp_path / "token.json")),
        )
        stub = _DoctorStub(available=False, files=[_drive_file("doc.md")])
        report = check_drive(config, client=stub, now=_NOW)

        assert report.token_present is True
        assert report.token_valid is True
        assert report.libs_available is False
        assert report.drive_reachable is None
        assert "list_files" not in stub.calls
        assert _no_downloads(stub)

    def test_reachable_with_listing(self, tmp_path: Path) -> None:
        """Valid token + available libs → reachable with a dry-run count."""
        creds = tmp_path / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        config = GoogleDriveConfig(
            credentials_file=str(creds),
            token_file=str(_write_token(tmp_path / "token.json")),
        )
        stub = _DoctorStub(
            available=True,
            files=[_drive_file("doc.md"), _drive_file("notes.docx")],
        )
        report = check_drive(config, client=stub, now=_NOW)

        assert report.credentials_present is True
        assert report.token_present is True
        assert report.drive_reachable is True
        assert report.listed_file_count == 2
        assert "list_files" in stub.calls
        assert _no_downloads(stub)  # never downloads on a check

    def test_reachability_error_is_caught(self, tmp_path: Path) -> None:
        """A list error is reported as unreachable, not raised."""
        config = GoogleDriveConfig(
            token_file=str(_write_token(tmp_path / "token.json")),
        )
        stub = _DoctorStub(
            available=True,
            raise_on_list=RuntimeError("quota exceeded"),
        )
        report = check_drive(config, client=stub, now=_NOW)

        assert report.drive_reachable is False
        assert report.listed_file_count is None
        assert _no_downloads(stub)

    def test_expired_token_skips_reachability(self, tmp_path: Path) -> None:
        """An expired token does not trigger a live probe (no OAuth flow)."""
        config = GoogleDriveConfig(
            token_file=str(_write_token(tmp_path / "token.json", expiry=_PAST)),
        )
        stub = _DoctorStub(available=True, files=[_drive_file("doc.md")])
        report = check_drive(config, client=stub, now=_NOW)

        assert report.token_valid is False
        assert report.drive_reachable is None
        assert "list_files" not in stub.calls

    def test_notes_never_contain_token_value(self, tmp_path: Path) -> None:
        """Human-readable notes never leak the placeholder token strings."""
        config = GoogleDriveConfig(
            token_file=str(_write_token(tmp_path / "token.json")),
        )
        stub = _DoctorStub(available=True, files=[_drive_file("doc.md")])
        report = check_drive(config, client=stub, now=_NOW)

        blob = " ".join(report.notes)
        assert "PLACEHOLDER-ACCESS" not in blob
        assert "PLACEHOLDER-REFRESH" not in blob


# ---- Recorded-harness scaffold -----------------------------------------


class TestRecordedHarness:
    """A recorded listing fixture feeds the doctor through a stub client.

    Establishes the recorded/VCR-style pattern issue #682 will extend with
    real captured Drive responses — without any live network in this skeleton.
    """

    def test_recorded_listing_drives_the_doctor(self, tmp_path: Path) -> None:
        """A JSON listing fixture round-trips into a reachable report."""
        recorded = [
            {"name": "essay.md"},
            {"name": "talk.pptx"},
            {"name": "budget.xlsx"},
        ]
        fixture = tmp_path / "recorded_listing.json"
        fixture.write_text(json.dumps(recorded), encoding="utf-8")

        files = [
            _drive_file(entry["name"]) for entry in json.loads(fixture.read_text())
        ]
        config = GoogleDriveConfig(
            token_file=str(_write_token(tmp_path / "token.json")),
        )
        stub = _DoctorStub(available=True, files=files)
        report = check_drive(config, client=stub, now=_NOW)

        assert report.drive_reachable is True
        assert report.listed_file_count == 3
        assert _no_downloads(stub)


# ---- CLI: creek gdrive --check -----------------------------------------


class TestGdriveCheckCli:
    """The ``--check`` flag is additive, safe, and mutually exclusive."""

    def test_check_with_no_auth_reports_missing_and_never_lists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No credentials/token → reports MISSING/absent, never lists, exit 0."""
        config = CreekConfig(
            google_drive=GoogleDriveConfig(
                credentials_file=str(tmp_path / "credentials.json"),
                token_file=str(tmp_path / "token.json"),
            ),
        )
        monkeypatch.setattr("creek.cli.load_config", lambda: config)
        # Real client reports libs available, but list_files must never run.
        from creek.ingest.gdrive import GoogleApiDriveClient

        monkeypatch.setattr(GoogleApiDriveClient, "is_available", lambda _self: True)

        def _boom(_self: object) -> list[DriveFile]:
            msg = "doctor must not list when no valid token is present"
            raise AssertionError(msg)

        monkeypatch.setattr(GoogleApiDriveClient, "list_files", _boom)

        result = runner.invoke(app, ["gdrive", "--check"])
        assert result.exit_code == 0, result.output
        assert "MISSING" in result.output
        assert "absent" in result.output.lower()

    def test_check_renders_reachable_listing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A valid token + stub client renders a reachable dry-run count."""
        config = CreekConfig(
            google_drive=GoogleDriveConfig(
                token_file=str(_write_token(tmp_path / "token.json")),
            ),
        )
        monkeypatch.setattr("creek.cli.load_config", lambda: config)
        stub = _DoctorStub(available=True, files=[_drive_file("a.md")])
        monkeypatch.setattr(
            "creek.ingest.gdrive.GoogleApiDriveClient",
            lambda _config: stub,
        )

        result = runner.invoke(app, ["gdrive", "--check"])
        assert result.exit_code == 0, result.output
        assert "reachable" in result.output.lower()
        assert _no_downloads(stub)

    def test_check_rejects_combined_flags(self, tmp_path: Path) -> None:
        """``--check`` with ``--download`` is rejected before any work."""
        result = runner.invoke(
            app,
            ["gdrive", "--check", "--download", "--staging", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "exactly one" in result.output.lower()
