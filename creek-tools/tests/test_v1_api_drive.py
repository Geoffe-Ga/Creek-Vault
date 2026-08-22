"""``/v1/connectors/drive`` — the read-only Drive connector over the network (#1527).

**RED at HEAD.** Before this issue the route table held six specs and no path
matching ``connectors``; ``Capability`` was a closed five-name enum; and the
only way to reach ``GoogleApiDriveClient`` was ``creek gdrive`` on the host.
Every test below fails at that commit with a ``404``, an ``ImportError`` or a
``KeyError``.

The suite is organised around the three things this route could plausibly get
wrong, in order of how much they would cost:

1. **The credential leaking.** The token is a long-lived Drive grant. It must
   not reach a response body, a log line, the audit trail or the published
   OpenAPI, and the sweep here looks for a sentinel in the *actual emitted
   bytes* of all four rather than reasoning about which code paths could emit
   it. Mutation-checked: making the status handler echo the token turns it red.
2. **A silent no-op.** A route that answers ``200`` while ingesting nothing is
   the failure epic #1523 exists to prevent, so the flow test asserts fragments
   on **disk**, and the "fetched but unsupported" case asserts that the
   response says so rather than reporting a clean success.
3. **A tier getting weaker.** ``privacy_tier`` is a one-way ratchet, and this
   is a second door onto the same corpus. The tier assertions read the bytes on
   disk and compare against a real ``run_ingest`` over the same input — the
   path ``creek ingest`` takes — rather than against a remembered literal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import frontmatter
import pytest

from creek._fslock import vault_lock
from creek.config import _READONLY_SCOPES, GoogleDriveConfig
from creek.ingest import INGESTOR_REGISTRY
from creek.ingest import gdrive as gdrive_module
from creek.ingest.gdrive import _REVOKE_URL, DriveFile, RevokeResult
from creek.ingest.pipeline import run_ingest
from creek_mcp.api.bundle import build_bundle
from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    CONTRACT_MINOR,
    SUPPORTED_CONTRACT_MINORS,
    Capability,
    DriveConnectionState,
    DriveConnectorStatusResponse,
    DriveDisconnectResponse,
    DriveSyncResponse,
    ErrorCode,
)
from creek_mcp.api.openapi import build_openapi
from creek_mcp.httpapi import drive as drive_module
from creek_mcp.tools import drive as drive_tools
from tests.v1_api_support import (
    DRIVE_CONNECTOR_PATH,
    DRIVE_SYNC_PATH,
    client,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from starlette.testclient import TestClient

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_OK_STATUS: Final[int] = 200
"""A served connector call."""

_UNAVAILABLE_STATUS: Final[int] = 503
"""Both connector refusals; they differ by ``code``, not by status."""

_INCOMPATIBLE_STATUS: Final[int] = 409
"""What a client below contract 0.9 is answered with on all three routes."""

_DRIVE_MINOR: Final[str] = "0.9"
"""The minor ``drive-connector`` was published at, restated from the ADR."""

_PREVIOUS_MINOR: Final[str] = "0.8"
"""The newest minor that predates the capability; still served for everything else."""

# A low-entropy test literal shaped like a Google refresh token, never a real
# credential. Spelled as a concatenation so a secret scanner reads it as a
# constructed string, matching ``tests/v1_api_support.py``'s token literals.
_TOKEN_SENTINEL: Final[str] = "1//0g-unit-test-refresh-" + "z" * 24

_DRIVE_FILE_SENTINEL: Final[str] = "quarterly-therapy-notes-1527"
"""A Drive *file name*, which is the user's content and may never cross the wire."""

_TENDER_BODY: Final[bytes] = (
    b"---\nprivacy_tier: intimate\n---\n\nsynthetic-tender-marker-1527\n"
)
"""A Drive markdown file that declares ``intimate`` in its own frontmatter."""

_PLAIN_BODY: Final[bytes] = (
    b"# Ridge notes\n\nsynthetic-plain-marker-1527 walked the ridge before dawn.\n"
)
"""A Drive markdown file that declares no tier at all."""

_PLAIN_MARKER: Final[bytes] = b"synthetic-plain-marker-1527"
"""The sentinel inside :data:`_PLAIN_BODY`, for reading fragments back off disk."""

_MARKDOWN_MIME: Final[str] = "text/markdown"
"""What Drive reports for an uploaded ``.md``; not a Google-native type."""

_MODIFIED: Final[datetime] = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
"""A fixed Drive ``modified_time``; never ``now()``, which would race the mtime skip."""

_BARRIER_TIMEOUT_SECONDS: Final[float] = 10.0
"""How long one overlapping sync waits for the other before giving up.

Long enough that a loaded machine does not turn a real measurement into a
timeout, short enough that a genuine deadlock fails the run rather than hanging
it. A wait that expires leaves :attr:`_Rendezvous.met` ``False``, and the test
refuses to draw any conclusion from that run."""

_HELD_LOCK_SECONDS: Final[float] = 1.0
"""How long the test itself holds the sync lock while a request is refused."""

_OVERLAP_WINDOW_SECONDS: Final[float] = 0.5
"""How long one sync waits *inside* the connector for the other to join it.

Not a vacuity guard — the opposite. It is the window in which an unlocked
second sync is guaranteed to catch the first, which is what turns "duplicates
about a third of the time" into "duplicates every time" and makes the fix's
absence provable in one run rather than ten. Once the lock is in place the
wait expires exactly once per test, which is the whole price this deliberate
amplifier charges the green suite.
"""

_GDRIVE_LEDGER_RELPATH: Final[str] = "00-Creek-Meta/State/ingest/gdrive.jsonl"
"""Where ``run_ingest(ledger_source=DRIVE_LEDGER_SOURCE)`` records what it saw."""


# --------------------------------------------------------------------------- #
# A Drive backend that never touches the network
# --------------------------------------------------------------------------- #


class _StubDrive:
    """A :class:`~creek.ingest.gdrive.DriveClient` over an in-memory listing.

    Satisfies the protocol and nothing more: there is no ``update``, ``delete``
    or ``trash`` here either, which is the read-only contract the protocol
    states structurally.

    Attributes:
        listing: What :meth:`list_files` answers with.
        bodies: ``{file id: bytes}`` for :meth:`download_to`.
        available: What :meth:`is_available` answers with.
        raises: An exception :meth:`list_files` raises instead of answering.
        downloads: Every ``file_id`` this stub was asked to stream.
    """

    def __init__(
        self,
        listing: list[DriveFile],
        bodies: dict[str, bytes],
        *,
        available: bool = True,
        raises: Exception | None = None,
    ) -> None:
        """Build a stub over *listing* and *bodies*.

        Args:
            listing: The files this Drive appears to hold.
            bodies: Their contents, by file id.
            available: Whether the optional Google libraries "import".
            raises: An exception ``list_files`` should raise instead.
        """
        self.listing = listing
        self.bodies = bodies
        self.available = available
        self.raises = raises
        self.downloads: list[str] = []

    def is_available(self) -> bool:
        """Return whether this backend can serve requests."""
        return self.available

    def list_files(self) -> list[DriveFile]:
        """Return the configured listing, or raise the configured failure.

        Returns:
            The listing.

        Raises:
            Exception: The exception this stub was built with, if any.
        """
        if self.raises is not None:
            raise self.raises
        return list(self.listing)

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Write the stubbed bytes for *file_id* to *destination*.

        Args:
            file_id: The Drive file id.
            destination: Where to stream it.
            export_mime: Unused; no stubbed file is Google-native.
        """
        del export_mime
        self.downloads.append(file_id)
        destination.write_bytes(self.bodies[file_id])


def _drive_file(file_id: str, name: str) -> DriveFile:
    """Return a :class:`DriveFile` in the shape the real client emits.

    Args:
        file_id: The Drive file id.
        name: The Drive file name.

    Returns:
        The metadata record.
    """
    return DriveFile(
        id=file_id,
        name=name,
        mime_type=_MARKDOWN_MIME,
        modified_time=_MODIFIED,
        size=len(name),
        parent_path="",
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Return a seeded vault the app is built over.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    return seed_vault(tmp_path / "vault")


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    """Return where the cached credential would live for this test.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The token file path — deliberately not created.
    """
    return tmp_path / "drive-token.json"


@pytest.fixture
def configured(
    tmp_path: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point ``load_config`` at a Drive configuration inside *tmp_path*.

    The connector tools resolve their configuration exactly as ``creek gdrive``
    does — through ``CREEK_CONFIG`` — so the test steers the real resolution
    rather than substituting it.

    Args:
        tmp_path: pytest's per-test directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.

    Returns:
        The staging directory the mirror is written into.
    """
    staging = tmp_path / "drive-staging"
    config = tmp_path / "creek_config.yaml"
    config.write_text(
        "source_drive: " + str(tmp_path) + "\n"
        "google_drive:\n"
        "  credentials_file: " + str(tmp_path / "credentials.json") + "\n"
        "  token_file: " + str(token_path) + "\n"
        "  staging_dir: drive-staging\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREEK_CONFIG", str(config))
    return staging


def _connect(token_path: Path, *, expires_in: timedelta = timedelta(days=7)) -> None:
    """Write a cached credential carrying :data:`_TOKEN_SENTINEL`.

    The shape ``Credentials.to_json()`` writes, so
    :func:`creek.ingest.gdrive.inspect_token` and
    :func:`~creek.ingest.gdrive.revoke_token` both read it for real.

    Args:
        token_path: Where to write it.
        expires_in: How far ahead the expiry sits.
    """
    token_path.write_text(
        json.dumps(
            {
                "token": _TOKEN_SENTINEL,
                "refresh_token": _TOKEN_SENTINEL,
                "client_secret": _TOKEN_SENTINEL,
                "expiry": (datetime.now(UTC) + expires_in).isoformat(),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def no_revocation_network(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Intercept the one outbound call the connector makes, and record it.

    :func:`creek.ingest.gdrive.revoke_token` posts the refresh token to
    Google's revocation endpoint — the single legitimate journey the credential
    makes, back to its issuer. The test suite must not make it, and the
    recorded payload is what lets
    ``test_the_credential_travels_only_to_googles_revocation_endpoint`` assert
    where it went.

    Args:
        monkeypatch: The active monkeypatch fixture.

    Returns:
        A list accumulating each intercepted ``data`` mapping.
    """
    sent: list[dict[str, str]] = []

    class _Response:
        """The two attributes ``revoke_token`` reads off a response."""

        is_success = True
        status_code = 200

    def _post(url: str, **kwargs: Any) -> _Response:
        """Record the revocation call instead of making it.

        Args:
            url: The endpoint being posted to.
            **kwargs: The rest of the httpx call, carrying ``data``.

        Returns:
            A successful stub response.
        """
        sent.append({"url": url, **dict(kwargs.get("data", {}))})
        return _Response()

    monkeypatch.setattr(gdrive_module.httpx, "post", _post)
    return sent


def _stubbed(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubDrive,
) -> None:
    """Substitute the single Drive-client construction site.

    Patched on :func:`creek_mcp.tools.drive.build_drive_client` — the one seam —
    so the tools themselves take no ``client`` argument and there is no
    alternative code path a test could exercise instead of production's.

    Args:
        monkeypatch: The active monkeypatch fixture.
        stub: The backend to return.
    """
    monkeypatch.setattr(drive_tools, "build_drive_client", lambda _config: stub)


def _status(test_client: TestClient, **kwargs: Any) -> httpx.Response:
    """``GET`` the connector state.

    Args:
        test_client: The client under test.
        **kwargs: Header overrides for :func:`headers`.

    Returns:
        The response.
    """
    return test_client.get(DRIVE_CONNECTOR_PATH, headers=headers(**kwargs))


def _sync(test_client: TestClient, **kwargs: Any) -> httpx.Response:
    """``POST`` one incremental sync.

    Args:
        test_client: The client under test.
        **kwargs: Header overrides for :func:`headers`.

    Returns:
        The response.
    """
    return test_client.post(DRIVE_SYNC_PATH, headers=headers(**kwargs))


def _disconnect(test_client: TestClient, **kwargs: Any) -> httpx.Response:
    """``DELETE`` the connector.

    Args:
        test_client: The client under test.
        **kwargs: Header overrides for :func:`headers`.

    Returns:
        The response.
    """
    return test_client.request(
        "DELETE", DRIVE_CONNECTOR_PATH, headers=headers(**kwargs)
    )


def _fragments(vault_path: Path) -> list[Path]:
    """Return every fragment file in *vault_path*, sorted.

    Args:
        vault_path: The vault root.

    Returns:
        Sorted fragment paths.
    """
    return sorted((vault_path / "01-Fragments").rglob("*.md"))


def _tiers_by_marker(vault_path: Path) -> dict[str, str]:
    """Return ``{marker: privacy_tier}`` for the fragments in *vault_path*.

    Keyed on a marker embedded in each body rather than on a fragment id or a
    path, so the same two files can be compared across two vaults ingested by
    two different doors.

    Args:
        vault_path: The vault root.

    Returns:
        One entry per fragment.
    """
    tiers: dict[str, str] = {}
    for path in _fragments(vault_path):
        post = frontmatter.load(path)
        marker = "tender" if "synthetic-tender-marker-1527" in post.content else "plain"
        tiers[marker] = str(post.metadata["privacy_tier"])
    return tiers


def _audit_bytes(vault_path: Path) -> bytes:
    """Return the raw MCP audit log, or empty when nothing was appended.

    Read as **bytes**, never parsed, because the question this suite asks of it
    is "does this file contain the credential anywhere", and a parse would only
    look where the parser expects.

    Args:
        vault_path: The vault root.

    Returns:
        The audit log's bytes.
    """
    log = vault_path / "00-Creek-Meta" / "audit" / "mcp.jsonl"
    return log.read_bytes() if log.is_file() else b""


# --------------------------------------------------------------------------- #
# Contract: the capability, its minor, and the two-sided negotiation
# --------------------------------------------------------------------------- #


def test_drive_connector_is_a_published_capability() -> None:
    """The capability exists and is published from the one since-minor table.

    At HEAD ``Capability`` had five members and no Drive entry, so this is the
    first assertion that goes red without the change.
    """
    assert Capability.DRIVE_CONNECTOR.value == "drive-connector"
    assert CAPABILITY_SINCE_MINOR[Capability.DRIVE_CONNECTOR] == _DRIVE_MINOR


def test_the_capability_minor_is_served_and_current() -> None:
    """0.9 is the current minor and the compatibility window widened, not shifted.

    ``0.8`` staying in the window is the half that costs nothing to assert and
    everything to get wrong: dropping it would refuse every correct ``0.8``
    client over a capability it was never told about.
    """
    assert CONTRACT_MINOR == _DRIVE_MINOR
    assert _DRIVE_MINOR in SUPPORTED_CONTRACT_MINORS
    assert _PREVIOUS_MINOR in SUPPORTED_CONTRACT_MINORS


def test_the_capability_is_advertised_to_a_current_client(
    vault: Path,
) -> None:
    """``GET /v1/capabilities`` lists the connector for a 0.9 client.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        body = envelope(test_client.get("/v1/capabilities", headers=headers()))
    assert Capability.DRIVE_CONNECTOR.value in body["capabilities"]


def test_the_capability_is_withheld_from_an_older_client(vault: Path) -> None:
    """A 0.8 client is not told the connector exists.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        body = envelope(
            test_client.get("/v1/capabilities", headers=headers(minor=_PREVIOUS_MINOR))
        )
    assert Capability.DRIVE_CONNECTOR.value not in body["capabilities"]
    assert Capability.UPLOAD.value in body["capabilities"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", DRIVE_CONNECTOR_PATH),
        ("POST", DRIVE_SYNC_PATH),
        ("DELETE", DRIVE_CONNECTOR_PATH),
    ],
    ids=["status", "sync", "disconnect"],
)
def test_a_withheld_capability_is_also_refused(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """**Withheld-but-served**: a 0.8 client is refused all three routes.

    The regression #1524 established, restated for the new capability. A
    capability filtered out of the handshake but still answered is an endpoint a
    client integrates against without ever having negotiated it — and the
    connector would be answered *successfully*, since the vault, config and
    credential are all in place here.

    Args:
        vault: A seeded vault.
        configured: The staging directory (fixture wires ``CREEK_CONFIG``).
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        method: The verb under test.
        path: The path under test.
    """
    del configured
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        response = test_client.request(
            method, path, headers=headers(minor=_PREVIOUS_MINOR)
        )
    assert response.status_code == _INCOMPATIBLE_STATUS
    assert envelope(response)["code"] == ErrorCode.INCOMPATIBLE_VERSION.value


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", DRIVE_CONNECTOR_PATH),
        ("POST", DRIVE_SYNC_PATH),
        ("DELETE", DRIVE_CONNECTOR_PATH),
    ],
    ids=["status", "sync", "disconnect"],
)
def test_an_advertised_capability_is_not_refused(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_revocation_network: list[dict[str, str]],
    method: str,
    path: str,
) -> None:
    """**Advertised-but-refused**: a 0.9 client is served, not stubbed out.

    The other direction of the same regression. ``501
    unsupported_capability`` and ``409 incompatible_version`` are both the
    answers a *published but unreachable* capability would give, and a server
    that advertises one of those is calling itself a liar.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        no_revocation_network: The intercepted revocation call.
        method: The verb under test.
        path: The path under test.
    """
    del configured, no_revocation_network
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        response = test_client.request(method, path, headers=headers())
    assert response.status_code == _OK_STATUS


def test_the_tool_and_wire_state_vocabularies_cannot_drift() -> None:
    """The MCP side's state words are exactly the wire enum's values.

    :mod:`creek_mcp.tools.drive` declares them separately, because it must not
    import the wire models; that separation is only safe while something holds
    the two equal, and this is that something. Without it a renamed state would
    turn every success into ``internal_error`` at the projection step.
    """
    assert {state.value for state in DriveConnectionState} == (
        drive_tools.CONNECTION_STATES
    )


# --------------------------------------------------------------------------- #
# Security: the credential never crosses
# --------------------------------------------------------------------------- #


def test_no_response_log_or_audit_entry_carries_the_credential(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    no_revocation_network: list[dict[str, str]],
) -> None:
    """Sweep the emitted bytes of all three routes for the token. **The crux.**

    The credential on disk carries :data:`_TOKEN_SENTINEL` in all three of the
    fields a real ``token.json`` holds — ``token``, ``refresh_token`` and
    ``client_secret`` — so no single field is the whole of what is being looked
    for. The sweep then reads the *actual* emitted bytes of every channel a
    caller or an operator could later read: the three response bodies, their
    headers, everything logged at ``DEBUG`` or above, and the raw audit file.

    Falsifiable, not decorative: adding ``"token": open(token_path).read()`` to
    :func:`creek_mcp.tools.drive.drive_status_tool`'s return — or handing the
    token to ``_audit``'s ``args``, where ``summarise_args`` copies a short
    string through verbatim — turns it red. It is mutation-checked in the PR.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        caplog: Captures every log record the three calls emit.
        no_revocation_network: The intercepted revocation call.
    """
    del configured, no_revocation_network
    _connect(token_path)
    stub = _StubDrive(
        [_drive_file("id-1", f"{_DRIVE_FILE_SENTINEL}.md")],
        {"id-1": _PLAIN_BODY},
    )
    _stubbed(monkeypatch, stub)
    caplog.set_level(logging.DEBUG)

    # The precondition, asserted rather than assumed: a sweep over a vault that
    # never held the credential would pass for the wrong reason.
    assert _TOKEN_SENTINEL in token_path.read_text(encoding="utf-8")

    with client(vault_path=vault) as test_client:
        emitted = [
            _status(test_client),
            _sync(test_client),
            _disconnect(test_client),
        ]

    assert [response.status_code for response in emitted] == [_OK_STATUS] * 3
    for response in emitted:
        assert _TOKEN_SENTINEL not in response.text
        assert _TOKEN_SENTINEL not in str(dict(response.headers))
    assert _TOKEN_SENTINEL not in caplog.text
    assert _TOKEN_SENTINEL.encode() not in _audit_bytes(vault)
    # Nor for the other wrong reason: all three calls really ran, the sync
    # really reached the connector, and the audit trail really was written.
    assert stub.downloads == ["id-1"]
    assert _fragments(vault)
    assert b"creek.drive.sync" in _audit_bytes(vault)
    assert not token_path.exists()


def test_the_credential_travels_only_to_googles_revocation_endpoint(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_revocation_network: list[dict[str, str]],
) -> None:
    """The one journey the token makes is back to its issuer, on disconnect.

    Stated positively as well as negatively. The sweep above proves the token
    reaches no caller; this proves the single outbound call it *does* provoke
    goes to Google's published revocation URL and nowhere else — which is what
    makes "the credential never leaves" a precise claim rather than an absolute
    one that the code quietly violates.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        no_revocation_network: The intercepted revocation call.
    """
    del configured
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        assert _disconnect(test_client).status_code == _OK_STATUS

    assert len(no_revocation_network) == 1
    call = no_revocation_network[0]
    assert call["url"] == _REVOKE_URL
    assert call["url"].startswith("https://")
    assert call["token"] == _TOKEN_SENTINEL


@pytest.mark.parametrize(
    "model",
    [DriveConnectorStatusResponse, DriveSyncResponse, DriveDisconnectResponse],
    ids=["status", "sync", "disconnect"],
)
def test_the_published_schema_has_no_field_a_credential_could_sit_in(
    model: type,
) -> None:
    """Each Drive response's field set is pinned, closed, and credential-free.

    The structural half of the guarantee: the models forbid extras, so the only
    way a token could reach a body is a *declared* field — and the declared
    fields are enumerated here, so adding one is a deliberate edit to this test
    rather than a quiet widening.

    Args:
        model: The wire model under test.
    """
    expected = {
        "DriveConnectorStatusResponse": {
            "status",
            "tier_ceiling",
            "connection",
            "scopes",
            "can_sync",
        },
        "DriveSyncResponse": {
            "status",
            "tier_ceiling",
            "files_fetched",
            "files_unchanged",
            "files_failed",
            "files_unsupported",
            "fragments_failed",
            "fragments_created",
            "fragments_updated",
            "fragments_unchanged",
        },
        "DriveDisconnectResponse": {
            "status",
            "tier_ceiling",
            "connection",
            "remote_revoked",
        },
    }[model.__name__]
    assert set(model.model_fields) == expected
    assert model.model_config["extra"] == "forbid"


def test_the_published_document_and_bundle_name_no_credential() -> None:
    """Neither the OpenAPI document nor the fixture bundle mentions a token.

    The published artifacts are what a consumer generates a client from and
    what a reviewer reads instead of the code. A ``token`` property documented
    there would be integrated against long before anyone noticed the server
    does not send one — and a fixture carrying a *sample* token is a credential
    committed to the repository.
    """
    document = json.dumps(build_openapi())
    bundle = "".join(build_bundle().values())
    for forbidden in ("refresh_token", "client_secret", "access_token"):
        assert forbidden not in document
        assert forbidden not in bundle
    assert _TOKEN_SENTINEL not in document
    assert _TOKEN_SENTINEL not in bundle


# --------------------------------------------------------------------------- #
# Security: the scopes stay read-only
# --------------------------------------------------------------------------- #


def test_the_configured_scope_set_is_read_only() -> None:
    """Every admissible Drive scope is a ``.readonly`` one.

    A widened scope is a silent privilege escalation: nothing about a sync
    would look different, and Creek would hold a grant that can delete the
    user's Drive. Asserted as a property of every member rather than as an
    equality with today's single scope, so adding a second *read-only* scope
    stays legal while adding ``drive.file`` or ``drive`` does not.
    """
    assert _READONLY_SCOPES
    assert all(scope.endswith(".readonly") for scope in _READONLY_SCOPES)


def test_a_write_scope_cannot_be_configured() -> None:
    """The config refuses a write scope outright, so none can reach the flow."""
    with pytest.raises(ValueError, match="read-only"):
        GoogleDriveConfig(scopes=["https://www.googleapis.com/auth/drive"])


def test_the_status_response_publishes_the_read_only_scopes(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advertised scope set is the configured one, verbatim.

    This is what lets a client *show* the user "read-only" and be telling the
    truth: the field is read off the same config object the OAuth flow requests
    with, and that object cannot hold a write scope.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        body = envelope(_status(test_client))
    assert set(body["scopes"]) == _READONLY_SCOPES
    assert all(scope.endswith(".readonly") for scope in body["scopes"])


# --------------------------------------------------------------------------- #
# The full flow: connect → status → sync → disk → disconnect → refused
# --------------------------------------------------------------------------- #


def test_the_whole_connector_flow_over_the_network(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_revocation_network: list[dict[str, str]],
) -> None:
    """Status → sync → fragments on disk → disconnect → refused.

    The end-to-end acceptance criterion, and it asserts on the **vault**, not
    on the response: a route that reported a clean sync while writing nothing
    would satisfy every count assertion and still be the silent no-op this epic
    exists to prevent.

    The last step is the revocation criterion. After a disconnect the cached
    credential is gone, and the next sync must *refuse* — not fall through to
    :class:`~creek.ingest.gdrive.GoogleApiDriveClient`, which answers a missing
    token by opening a browser on the server.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        no_revocation_network: The intercepted revocation call.
    """
    del no_revocation_network
    stub = _StubDrive(
        [_drive_file("id-1", "ridge.md")],
        {"id-1": _PLAIN_BODY},
    )
    _stubbed(monkeypatch, stub)

    with client(vault_path=vault) as test_client:
        before = envelope(_status(test_client))
        assert before["connection"] == DriveConnectionState.NOT_CONNECTED.value
        assert before["can_sync"] is False

        refused = _sync(test_client)
        assert refused.status_code == _UNAVAILABLE_STATUS
        assert envelope(refused)["code"] == ErrorCode.UNAVAILABLE.value
        assert stub.downloads == []

        _connect(token_path)
        connected = envelope(_status(test_client))
        assert connected["connection"] == DriveConnectionState.CONNECTED.value
        assert connected["can_sync"] is True

        synced = envelope(_sync(test_client))
        assert synced["files_fetched"] == 1
        assert synced["fragments_created"] == 1
        assert synced["files_failed"] == 0
        assert stub.downloads == ["id-1"]
        assert (configured / "ridge.md").is_file()

        fragments = _fragments(vault)
        assert len(fragments) == 1
        assert b"synthetic-plain-marker-1527" in fragments[0].read_bytes()

        gone = envelope(_disconnect(test_client))
        assert gone["connection"] == DriveConnectionState.NOT_CONNECTED.value
        assert gone["remote_revoked"] is True
        assert not token_path.exists()

        after = envelope(_status(test_client))
        assert after["connection"] == DriveConnectionState.NOT_CONNECTED.value

        post_revoke = _sync(test_client)
        assert post_revoke.status_code == _UNAVAILABLE_STATUS
        assert stub.downloads == ["id-1"]


def test_a_second_sync_of_an_unchanged_drive_creates_no_fragment(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incrementality, asserted on the corpus rather than on the clock.

    The downloader skips any file whose local mtime is at least as new as
    Drive's ``modified_time``, and only downloaded files are ingested — so the
    second pass fetches nothing, writes nothing, and says so. Asserted three
    ways because each alone could pass while the sync was doing real work: the
    reported counts, the stub's own record of which ids it was asked to stream,
    and the fragment files on disk.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    stub = _StubDrive(
        [_drive_file("id-1", "ridge.md")],
        {"id-1": _PLAIN_BODY},
    )
    _stubbed(monkeypatch, stub)

    with client(vault_path=vault) as test_client:
        first = envelope(_sync(test_client))
        after_first = _fragments(vault)
        second = envelope(_sync(test_client))

    assert first["files_fetched"] == 1
    assert first["fragments_created"] == 1
    assert second["files_fetched"] == 0
    assert second["files_unchanged"] == 1
    assert second["fragments_created"] == 0
    assert stub.downloads == ["id-1"]
    assert _fragments(vault) == after_first


# --------------------------------------------------------------------------- #
# Tier: the one-way ratchet holds on this door too
# --------------------------------------------------------------------------- #


def test_a_synced_fragment_carries_the_tier_creek_ingest_would_give_it(
    vault: Path,
    configured: Path,
    token_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Drive files, both filed exactly as ``creek ingest`` would file them.

    Measured, not remembered: the same bytes are run through a real
    ``run_ingest`` into a second vault — the path ``creek ingest`` takes, with
    no ``privacy_tier`` — and the tiers on disk are compared. The declared-tier
    file must land ``intimate``; the undeclared one must land at whatever the
    ordinary path gives it, which is emphatically **not** ``open``.

    The second assertion is the one that catches a weakening: making
    :func:`~creek_mcp.tools.drive._ingest_downloaded` pass
    ``privacy_tier=PrivacyTier.OPEN`` leaves the ``intimate`` file untouched —
    escalation is one-way — while filing the undeclared file in the clear.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        tmp_path: pytest's per-test directory, for the comparison vault.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(
        monkeypatch,
        _StubDrive(
            [_drive_file("id-1", "tender.md"), _drive_file("id-2", "ridge.md")],
            {"id-1": _TENDER_BODY, "id-2": _PLAIN_BODY},
        ),
    )
    with client(vault_path=vault) as test_client:
        assert envelope(_sync(test_client))["fragments_created"] == 2

    synced = _tiers_by_marker(vault)

    reference_vault = seed_vault(tmp_path / "reference")
    source = tmp_path / "source"
    source.mkdir()
    (source / "tender.md").write_bytes(_TENDER_BODY)
    (source / "ridge.md").write_bytes(_PLAIN_BODY)
    run_ingest(
        ingestor_cls=INGESTOR_REGISTRY["markdown"],
        source_type="markdown",
        input_path=source,
        vault_path=reference_vault,
    )
    reference = _tiers_by_marker(reference_vault)

    assert synced == reference
    assert set(synced) == {"tender", "plain"}
    assert synced["tender"] == "intimate"
    # The mutation this catches: passing ``privacy_tier=PrivacyTier.OPEN``
    # leaves the tender file at ``intimate`` — escalation is one-way — while
    # filing the undeclared one in the clear.
    assert synced["plain"] != "open"


# --------------------------------------------------------------------------- #
# Refusals and reports leak no Drive content
# --------------------------------------------------------------------------- #


def test_a_failed_sync_refuses_without_naming_what_failed(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Drive error carrying a file name is refused, not narrated.

    Drive's own exceptions embed the request URI and its failure lines lead
    with the file's name; both are the vault owner's content. The refusal
    therefore carries one published constant with no interpolation at all —
    ``transporting the exception's text is the mutation this test is here to
    catch``.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(
        monkeypatch,
        _StubDrive(
            [],
            {},
            raises=RuntimeError(f"quota exceeded reading {_DRIVE_FILE_SENTINEL}"),
        ),
    )
    with client(vault_path=vault) as test_client:
        response = _sync(test_client)

    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert _DRIVE_FILE_SENTINEL not in response.text
    assert _DRIVE_FILE_SENTINEL.encode() not in _audit_bytes(vault)


def test_a_successful_sync_never_names_a_drive_file(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success payload is counts only — no name, no id, no fragment id.

    A sync reaches content the caller may be nowhere near entitled to read, so
    the receipt cannot be a list of what it touched. Asserted against both the
    Drive file name and the Drive file id, and against the body of the file
    itself, on the actual response text.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(
        monkeypatch,
        _StubDrive(
            [_drive_file("drive-id-1527", f"{_DRIVE_FILE_SENTINEL}.md")],
            {"drive-id-1527": _TENDER_BODY},
        ),
    )
    with client(vault_path=vault) as test_client:
        response = _sync(test_client)

    assert response.status_code == _OK_STATUS
    body = envelope(response)
    assert body["fragments_created"] == 1
    assert _DRIVE_FILE_SENTINEL not in response.text
    assert "drive-id-1527" not in response.text
    assert "synthetic-tender-marker-1527" not in response.text
    assert set(body) == set(DriveSyncResponse.model_fields)


def test_an_unsupported_file_is_reported_rather_than_silently_dropped(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetched-but-unreadable export is counted, not reported as ingested.

    ``conversations.json`` is the #1526 case: routing it to ``generic`` would
    turn a whole ChatGPT history into one undifferentiated blob. Here it is
    fetched, refused by the router, and *published as a count* — a sync that
    reported ``fragments_created: 0`` with no other signal would look
    indistinguishable from a Drive with nothing new in it.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(
        monkeypatch,
        _StubDrive(
            [_drive_file("id-1", "conversations.json")],
            {"id-1": b'{"mapping": {}}'},
        ),
    )
    with client(vault_path=vault) as test_client:
        body = envelope(_sync(test_client))

    assert body["files_fetched"] == 1
    assert body["files_unsupported"] == 1
    assert body["fragments_created"] == 0
    assert _fragments(vault) == []


# --------------------------------------------------------------------------- #
# States: each one is reachable and each one refuses a sync
# --------------------------------------------------------------------------- #


def test_missing_google_libraries_report_unsupported_not_disconnected(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server without the optional libraries says so, so the remedy is right.

    Conflating this with ``not_connected`` would send the user round an
    authorisation loop that cannot terminate: the credential is fine, the
    server cannot use it.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}, available=False))
    with client(vault_path=vault) as test_client:
        body = envelope(_status(test_client))
        refused = _sync(test_client)

    assert body["connection"] == DriveConnectionState.UNSUPPORTED.value
    assert body["can_sync"] is False
    assert refused.status_code == _UNAVAILABLE_STATUS


def test_a_lapsed_unrefreshable_credential_reports_expired(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired credential with no refresh token cannot be renewed silently.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    token_path.write_text(
        json.dumps(
            {
                "token": _TOKEN_SENTINEL,
                "expiry": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        body = envelope(_status(test_client))
        refused = _sync(test_client)

    assert body["connection"] == DriveConnectionState.EXPIRED.value
    assert body["can_sync"] is False
    assert refused.status_code == _UNAVAILABLE_STATUS


def test_every_unusable_state_earns_the_same_sync_refusal(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three unusable states are indistinguishable from a refusal alone.

    The no-oracle property, asserted as byte-equality of the refusals rather
    than as a claim about the code. A caller that could tell "no credential"
    from "expired credential" from "no libraries" *by probing the sync verb*
    would have a side channel onto the operator's setup that it never
    negotiated — the status route is where that question is answered, once and
    deliberately.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    bodies: list[dict[str, Any]] = []
    statuses: set[int] = set()

    with client(vault_path=vault) as test_client:
        # not_connected: no token file at all.
        _stubbed(monkeypatch, _StubDrive([], {}))
        first = _sync(test_client)
        # unsupported: a valid credential, no libraries.
        _connect(token_path)
        _stubbed(monkeypatch, _StubDrive([], {}, available=False))
        second = _sync(test_client)
        # expired: libraries present, credential lapsed and unrefreshable.
        token_path.write_text(
            json.dumps(
                {
                    "token": _TOKEN_SENTINEL,
                    "expiry": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        _stubbed(monkeypatch, _StubDrive([], {}))
        third = _sync(test_client)

    for response in (first, second, third):
        statuses.add(response.status_code)
        body = envelope(response)
        body.pop("request_id")
        bodies.append(body)

    assert statuses == {_UNAVAILABLE_STATUS}
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["code"] == ErrorCode.UNAVAILABLE.value


def test_disconnecting_a_disconnected_connector_is_a_success(
    vault: Path,
    configured: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_revocation_network: list[dict[str, str]],
) -> None:
    """Disconnect is idempotent and reports no revocation it did not perform.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        monkeypatch: The active monkeypatch fixture.
        no_revocation_network: The intercepted revocation call.
    """
    del configured
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        body = envelope(_disconnect(test_client))

    assert body["connection"] == DriveConnectionState.NOT_CONNECTED.value
    assert body["remote_revoked"] is False
    assert no_revocation_network == []


# --------------------------------------------------------------------------- #
# Refusal projection
# --------------------------------------------------------------------------- #


def _published_reasons() -> tuple[str, ...]:
    """Return every refusal reason :mod:`creek_mcp.tools.drive` exports.

    Discovered rather than listed. A hand-maintained tuple silently stops
    covering the newest reason the moment one is added — which is exactly what
    happened when ``SYNC_BUSY_REASON`` arrived — and a sweep that has quietly
    stopped sweeping reads identically to one that passed.

    Returns:
        The reason strings, sorted by constant name.
    """
    names = sorted(
        name
        for name in vars(drive_tools)
        if name.endswith("_REASON") and isinstance(getattr(drive_tools, name), str)
    )
    return tuple(getattr(drive_tools, name) for name in names)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (drive_tools.CONFIG_UNAVAILABLE_REASON, ErrorCode.UNAVAILABLE),
        (drive_tools.NOT_CONNECTED_REASON, ErrorCode.UNAVAILABLE),
        (drive_tools.ERASE_FAILED_REASON, ErrorCode.UNAVAILABLE),
        (drive_tools.SYNC_FAILED_REASON, ErrorCode.TEMPORARILY_UNAVAILABLE),
        (drive_tools.SYNC_BUSY_REASON, ErrorCode.TEMPORARILY_UNAVAILABLE),
        (drive_tools.VAULT_UNAVAILABLE_REASON, ErrorCode.TEMPORARILY_UNAVAILABLE),
        ("something this adapter has never heard of", ErrorCode.INTERNAL_ERROR),
        ("", ErrorCode.INTERNAL_ERROR),
    ],
)
def test_the_refusal_projection_is_total_and_fails_closed(
    reason: str,
    expected: ErrorCode,
) -> None:
    """Every reason the tools emit maps, and anything else becomes a server fault.

    Failing closed to ``internal_error`` rather than to a plausible refusal is
    the whole design: a reason this adapter cannot classify is one it must not
    narrate, and narrating it is how a Drive exception's text would reach a
    caller.

    Args:
        reason: The tool refusal reason under test.
        expected: The code it must project onto.
    """
    from creek_mcp.httpapi.drive import drive_refusal_code

    assert drive_refusal_code(reason) is expected


def test_every_published_reason_is_classified_rather_than_defaulted() -> None:
    """No reason the tools can emit falls through to ``internal_error``.

    The row-by-row test above pins the mapping it knows about; this one asks
    the module what reasons exist, so a new constant that nobody classified
    fails here instead of quietly reaching a caller as a ``500``.
    """
    from creek_mcp.httpapi.drive import drive_refusal_code

    reasons = _published_reasons()
    assert len(reasons) >= 6, reasons
    unclassified = [
        reason
        for reason in reasons
        if drive_refusal_code(reason) is ErrorCode.INTERNAL_ERROR
    ]
    assert unclassified == []


def test_the_reason_constants_carry_no_interpolation() -> None:
    """No connector refusal is built from a string something else composed.

    The structural reason the leak tests above hold: a reason with a ``%s`` or
    a ``{}`` in it is one that will eventually be handed a Drive file name.
    """
    reasons = _published_reasons()
    assert len(reasons) == len(set(reasons))
    for reason in reasons:
        assert "{" not in reason
        assert "%" not in reason


# --------------------------------------------------------------------------- #
# The three refusals a happy path never reaches
# --------------------------------------------------------------------------- #


def test_an_unreadable_configuration_refuses_rather_than_defaulting(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``CREEK_CONFIG`` pointing nowhere is refused, never silently defaulted.

    The default ``GoogleDriveConfig`` names ``token.json`` *relative to the
    working directory*, so falling back to it would make a remote sync read
    whatever credential happened to be beside the server process — a different
    Drive account than the operator configured, with no error anywhere.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    monkeypatch.setenv("CREEK_CONFIG", str(vault / "nonexistent-config.yaml"))
    _stubbed(monkeypatch, _StubDrive([], {}))
    with client(vault_path=vault) as test_client:
        response = _status(test_client)

    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value


def test_a_credential_that_survives_the_erase_is_a_refusal_not_a_success(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnect that could not erase the token must not answer ``200``.

    The one outcome a caller must never read as done: the credential is still
    on disk and still usable, and a success here would leave the user believing
    they had revoked access they still grant.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    monkeypatch.setattr(
        drive_tools,
        "revoke_token",
        lambda _config: RevokeResult(
            token_file_existed=True,
            token_file_removed=False,
            remote_revoked=False,
            error="permission denied",
        ),
    )
    with client(vault_path=vault) as test_client:
        response = _disconnect(test_client)

    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value
    assert "permission denied" not in response.text


def test_a_vault_that_vanishes_mid_ingest_is_transient_not_a_fault(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing vault under the ingest earns a retryable refusal.

    ``run_ingest`` raises :class:`FileNotFoundError` from ``VaultWriter`` when
    the vault is not there; the tool translates it rather than letting it reach
    the error boundary as a ``500``, because an unmounted volume is exactly the
    condition a backoff clears.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(
        monkeypatch,
        _StubDrive([_drive_file("id-1", "ridge.md")], {"id-1": _PLAIN_BODY}),
    )

    def _vanished(**_kwargs: Any) -> None:
        """Stand in for a vault that disappeared between resolve and write.

        Raises:
            FileNotFoundError: Always, as ``VaultWriter`` would.
        """
        raise FileNotFoundError(str(vault))

    monkeypatch.setattr(drive_tools, "run_ingest", _vanished)
    with client(vault_path=vault) as test_client:
        response = _sync(test_client)

    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert str(vault) not in response.text


def test_a_file_routed_to_an_unregistered_ingestor_is_counted_not_lost(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routing-table bug degrades to one unsupported file, not to a dead sync.

    ``route_to_ingestor`` naming a key ``INGESTOR_REGISTRY`` does not hold
    would be a Creek bug; letting the ``KeyError`` out would abort the run and
    lose every file after the offending one, and the response would say nothing
    at all. It is counted instead, and the rest of the listing still lands.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    _stubbed(
        monkeypatch,
        _StubDrive(
            [_drive_file("id-1", "broken.md"), _drive_file("id-2", "ridge.md")],
            {"id-1": _PLAIN_BODY, "id-2": _PLAIN_BODY},
        ),
    )
    real = drive_tools.route_to_ingestor

    def _route(path: Path) -> str:
        """Route ``broken.md`` to a key the registry does not hold.

        Args:
            path: The staged file.

        Returns:
            The ingestor key.
        """
        return "no-such-ingestor" if path.name == "broken.md" else real(path)

    monkeypatch.setattr(drive_tools, "route_to_ingestor", _route)
    with client(vault_path=vault) as test_client:
        body = envelope(_sync(test_client))

    assert body["files_fetched"] == 2
    assert body["files_unsupported"] == 1
    assert body["fragments_created"] == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", DRIVE_CONNECTOR_PATH),
        ("POST", DRIVE_SYNC_PATH),
        ("DELETE", DRIVE_CONNECTOR_PATH),
    ],
    ids=["status", "sync", "disconnect"],
)
def test_an_absent_vault_refuses_and_scaffolds_nothing(
    tmp_path: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """No connector verb writes into a vault that is not there.

    All three audit, and ``MCPAuditLog.append`` creates its parent directories,
    so a route that ran before probing would scaffold a vault tree from the
    network — and then report a connector state for a corpus that does not
    exist. The probe is the same side-effect-free marker check
    ``GET /v1/capabilities`` uses, and the assertion is that the directory is
    still absent afterwards, not merely that the status line said ``503``.

    Args:
        tmp_path: pytest's per-test directory.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        method: The verb under test.
        path: The path under test.
    """
    del configured
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    missing = tmp_path / "no-such-vault"

    with client(vault_path=missing) as test_client:
        response = test_client.request(method, path, headers=headers())

    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value
    assert not missing.exists()
    assert token_path.exists()


def _a_running_loop_is_visible() -> bool:
    """Return whether the calling thread is currently running an event loop.

    Returns:
        ``True`` when :func:`asyncio.get_running_loop` succeeds here.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", DRIVE_CONNECTOR_PATH),
        ("POST", DRIVE_SYNC_PATH),
        ("DELETE", DRIVE_CONNECTOR_PATH),
    ],
    ids=["status", "sync", "disconnect"],
)
def test_no_connector_verb_does_its_blocking_work_on_the_event_loop(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_revocation_network: list[dict[str, str]],
    method: str,
    path: str,
) -> None:
    """All three verbs resolve the vault — and everything after it — off the loop.

    A sync is the longest unit of work ``/v1`` performs: a config read and YAML
    parse, a token-file read, a whole Drive download, an ingest per file, and
    an audit append holding an ``fcntl`` lock across an ``fsync``. On the event
    loop that stalls every other connection the process is serving *and* leaves
    :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
    unable to fire, because its cancel scope is evaluated on the loop — so the
    one request that most needs a timeout is the one that cannot get one.

    Asserting latency would not see this; the on-loop implementation returns
    the same ``200``. What distinguishes them is which thread ran the work, so
    that is what is asserted, from the stdlib.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
        no_revocation_network: The intercepted revocation call.
        method: The verb under test.
        path: The path under test.
    """
    del configured, no_revocation_network
    _connect(token_path)
    _stubbed(monkeypatch, _StubDrive([], {}))
    seen: list[bool] = []
    real = drive_module.configured_vault

    def _probe(request: Any) -> Path | None:
        """Record the calling thread, then resolve the vault for real.

        Args:
            request: The request in flight.

        Returns:
            Whatever the real resolver returns.
        """
        seen.append(_a_running_loop_is_visible())
        return real(request)

    monkeypatch.setattr(drive_module, "configured_vault", _probe)
    with client(vault_path=vault) as test_client:
        response = test_client.request(method, path, headers=headers())

    # Non-vacuity: the probe was genuinely reached through the production path
    # and its answer consumed, rather than the request short-circuiting above it.
    assert response.status_code == _OK_STATUS
    assert seen == [False]


def test_a_file_that_downloads_but_fails_to_ingest_is_reported(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync whose content half-failed must not answer a clean ``ok``.

    ``run_ingest`` reports a per-unit failure in ``errors`` and never logs it
    itself. Discarding that list put a file which downloaded cleanly but whose
    fragment failed to assemble — or whose write raised ``OSError`` — into
    NONE of the published counts: ``files_failed`` covers only a download that
    never landed, and ``files_unsupported`` is decided before the ingest even
    starts. The response was a 200 with silently fewer fragments and no signal
    anywhere, which is the exact shortfall this connector exists to avoid.

    Caught by adversarial review of this PR.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    from creek.ingest.pipeline import IngestRunResult

    def _failing_ingest(**_kwargs: object) -> IngestRunResult:
        """One unit in, zero fragments out, one error recorded."""
        return IngestRunResult(
            written=0,
            discovered=1,
            created=0,
            updated=0,
            unchanged=0,
            tombed=0,
            skipped=0,
            errors=["[gdrive] failed to write frag-x: disk full"],
        )

    stub = _StubDrive([_drive_file("id-1", "ridge.md")], {"id-1": _PLAIN_BODY})
    _stubbed(monkeypatch, stub)
    monkeypatch.setattr(drive_tools, "run_ingest", _failing_ingest)
    _connect(token_path)

    with client(vault_path=vault) as test_client:
        response = _sync(test_client)

    assert response.status_code == _OK_STATUS, response.text
    body = envelope(response)
    assert body["fragments_failed"] >= 1, body
    assert body["fragments_created"] == 0, body

    # The count is published; the error STRING is not. It carries a source
    # path, and a Drive file name is user content this response must not echo.
    assert "disk full" not in response.text
    assert "frag-x" not in response.text


# --------------------------------------------------------------------------- #
# Two syncs at once: what they may not do to the corpus (#1569, #1590)
# --------------------------------------------------------------------------- #


class _Rendezvous:
    """A two-party meeting point that each thread reaches at most once.

    :class:`threading.Barrier` alone is not enough here. The seam this
    rendezvous sits on can be entered more than once per run, so a bare barrier
    would trip a second time with only one party and hang. Recording which
    threads have already passed makes each one wait exactly once, whatever the
    seam does afterwards.

    :attr:`met` is the non-vacuity flag. A run where the wait timed out
    measured two *sequential* syncs, and the caller must refuse to draw any
    conclusion from it rather than reporting a green test.

    **It must sit outside the lock.** The predecessor of the test below held
    both syncs inside :meth:`~_StubDrive.list_files` and
    :meth:`~_StubDrive.download_to` — both of which are now *behind* the
    connector's lock, so the second sync can no longer reach either until the
    first has finished. A rendezvous there would not deadlock (each wait is
    bounded) but it would expire twice over, spending twenty seconds to prove
    nothing. The meeting point therefore moved up to the one seam every sync
    passes *before* the lock: constructing the Drive client.

    Attributes:
        met: Whether both parties arrived before the timeout.
    """

    def __init__(self, timeout: float = _BARRIER_TIMEOUT_SECONDS) -> None:
        """Build an unmet, two-party rendezvous.

        Args:
            timeout: How long one party waits for the other. The default
                is the vacuity-guard deadline; the overlap amplifier
                inside :class:`_OccupancyDrive` passes a much shorter one
                because there it is *expected* to expire once the lock is
                doing its job.
        """
        self._barrier = threading.Barrier(2)
        self._timeout = timeout
        self._seen: set[int] = set()
        self._lock = threading.Lock()
        self.met = False

    def arrive(self) -> None:
        """Block until the other thread arrives, at most once per thread."""
        with self._lock:
            if threading.get_ident() in self._seen:
                return
            self._seen.add(threading.get_ident())
        try:
            self._barrier.wait(timeout=self._timeout)
        except threading.BrokenBarrierError:
            # An expired wait also *breaks* the barrier, so the party that
            # arrives later returns from here immediately rather than
            # paying the deadline a second time.
            return
        self.met = True


class _OccupancyDrive(_StubDrive):
    """A Drive that records how many syncs were inside it at the same time.

    The window measured runs from :meth:`list_files` — the first thing
    :meth:`~creek.ingest.gdrive.GoogleDriveDownloader.download_all` does, and
    the first thing behind the lock — to the return of :meth:`download_to`.
    :attr:`peak_inside` is therefore a direct reading of "were two syncs in the
    connector at once", rather than something inferred from a timeout.

    A sync that is serialised behind another never calls :meth:`download_to`
    at all: by the time it runs, the staging file already carries Drive's
    ``modified_time`` and the incremental skip claims it. Its occupancy is
    consequently never handed back, which is harmless — only the *peak* is
    asserted, and one sync that never leaves still peaks at one.

    :attr:`overlap` is the amplifier that makes the *consequences* of a
    missing lock deterministic rather than load-dependent. Without it the two
    runs, released together at the client seam, drift apart by microseconds and
    duplicate only sometimes — measured 3 times in 10, which is a flaky test
    that also under-reports the defect. With it, an unserialised second run
    waits here for the first and the two proceed together every time. Under the
    lock the wait simply expires (:data:`_OVERLAP_WINDOW_SECONDS`, once) and the
    run continues, because the second sync is not in this method to be waited
    for — which is the finding, stated as a bounded cost rather than a hang.

    Attributes:
        peak_inside: The greatest number of syncs seen inside at once.
        overlap: The bounded meeting point inside the connector.
    """

    def __init__(self, listing: list[DriveFile], bodies: dict[str, bytes]) -> None:
        """Build an occupancy-counting stub over the usual one.

        Args:
            listing: The files this Drive appears to hold.
            bodies: Their contents, by file id.
        """
        super().__init__(listing, bodies)
        self._occupancy_lock = threading.Lock()
        self._inside = 0
        self.peak_inside = 0
        self.overlap = _Rendezvous(timeout=_OVERLAP_WINDOW_SECONDS)

    def list_files(self) -> list[DriveFile]:
        """Record one sync entering the connector, then answer the listing.

        Returns:
            The listing.
        """
        with self._occupancy_lock:
            self._inside += 1
            self.peak_inside = max(self.peak_inside, self._inside)
        self.overlap.arrive()
        return super().list_files()

    def download_to(
        self,
        file_id: str,
        destination: Path,
        *,
        export_mime: str | None = None,
    ) -> None:
        """Stream the stubbed bytes, then record this sync leaving.

        Args:
            file_id: The Drive file id.
            destination: Where to stream it.
            export_mime: Unused; no stubbed file is Google-native.
        """
        super().download_to(file_id, destination, export_mime=export_mime)
        with self._occupancy_lock:
            self._inside -= 1


def _stubbed_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubDrive,
    dispatched: _Rendezvous,
) -> None:
    """Substitute the Drive client, holding both syncs at that seam.

    :func:`~creek_mcp.tools.drive.build_drive_client` is the last thing
    ``drive_sync_tool`` does before it reaches for the connector's lock, so a
    meeting point here proves both requests were genuinely in flight inside the
    tool at the same moment — without patching any production internal and
    without sitting anywhere the lock could hold one party out.

    Args:
        monkeypatch: The active monkeypatch fixture.
        stub: The backend to return.
        dispatched: Where the two syncs meet.
    """

    def _build(_config: object) -> _StubDrive:
        """Wait for the other sync, then hand back the stub.

        Args:
            _config: The Drive configuration, unused by the stub.

        Returns:
            The stub backend.
        """
        dispatched.arrive()
        return stub

    monkeypatch.setattr(drive_tools, "build_drive_client", _build)


def _ledger_rows(vault_path: Path) -> list[dict[str, Any]]:
    """Return the Drive ingest ledger's rows, oldest first.

    Args:
        vault_path: The vault root.

    Returns:
        One dict per non-blank line, or an empty list when nothing ingested.
    """
    path = vault_path / _GDRIVE_LEDGER_RELPATH
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fragment_ids(fragments: list[Path]) -> list[str]:
    """Return the ``id`` each fragment file declares in its frontmatter.

    Args:
        fragments: The fragment paths to read.

    Returns:
        The declared ids, in the order the paths were given.
    """
    return [str(frontmatter.load(path).get("id", "")) for path in fragments]


def test_two_overlapping_syncs_cannot_duplicate_the_corpus(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One Drive file stays one fragment however the two requests interleave.

    ``POST /v1/connectors/drive/syncs`` is served by a worker thread and
    :data:`~creek_mcp.httpapi.middleware.limits.DEFAULT_MAX_CONCURRENCY` is 32,
    so two callers — or one caller that retried — reach the connector at once.
    Until #1590 nothing stopped them at any of the three layers that might
    have: the downloader's mtime skip is a check-then-act against a staging
    file neither run had written; the ingest ledger is append-only JSONL loaded
    once per run; and the writer's ``.id-index.jsonl`` is read before either
    run appends to it. Measured on this box that duplicated **8 runs in 10**,
    producing ``…-Ridge-notes.md`` and ``…-Ridge-notes-1.md`` carrying the same
    ``frag-`` id, two ledger rows for one ``source_key``, and — because
    ``_load_index_locked`` lets the last write win — an id lookup that resolved
    to the *second* file and orphaned the first.

    The fix serialises the download and the ingest per vault, so the two runs
    become the sync-then-no-op pair
    ``test_a_second_sync_of_an_unchanged_drive_creates_no_fragment`` already
    pins: one fetch, one fragment, one ledger row, and a second response
    reporting ``files_unchanged: 1``.

    **Non-vacuity.** ``dispatched`` is a two-party barrier at the one seam
    every sync passes *before* taking the lock, so a green run cannot be two
    sequential syncs that never overlapped. ``peak_inside`` then reads the
    occupancy behind the lock directly: two syncs were dispatched together and
    exactly one was ever inside the connector. The stub's second, bounded
    meeting point (:data:`_OVERLAP_WINDOW_SECONDS`) then makes the *corruption*
    deterministic instead of load-dependent. Measured against the unfixed code
    on this box: ``peak_inside`` red 10/10; ``downloads``, the ledger row count
    and the fragment tallies red 9/10 *with the occupancy assertion relaxed so
    they are what reports* — against 3/10 for a version of this test without the
    amplifier, which is a flaky test that also under-reports the defect. The
    on-disk file count is the one assertion that stays probabilistic (two racing
    index reads duplicated 8 runs in 10) and it is asserted anyway, because the
    duplicate file is the thing this issue is named after.

    Args:
        vault: A seeded vault, shared by both runs.
        configured: The staging directory, shared by both runs.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    stub = _OccupancyDrive([_drive_file("id-1", "ridge.md")], {"id-1": _PLAIN_BODY})
    dispatched = _Rendezvous()
    _stubbed_concurrently(monkeypatch, stub, dispatched)

    with client(vault_path=vault) as test_client, ThreadPoolExecutor(2) as pool:
        responses = [
            future.result()
            for future in [pool.submit(_sync, test_client) for _ in range(2)]
        ]
    payloads = [envelope(response) for response in responses]

    assert dispatched.met, "the two syncs were never in flight together"
    assert [response.status_code for response in responses] == [_OK_STATUS] * 2
    assert stub.peak_inside == 1, "two syncs were inside the connector at once"

    # Layer 1 — the mtime skip is no longer raced: the file is fetched once,
    # and the run that lost reports the unchanged pass a serial second sync gives.
    assert stub.downloads == ["id-1"]
    assert sorted(payload["files_fetched"] for payload in payloads) == [0, 1]
    assert sorted(payload["files_unchanged"] for payload in payloads) == [0, 1]

    # Layer 2 — the ledger records one source unit once.
    rows = _ledger_rows(vault)
    assert len(rows) == 1, rows
    assert len({row["fragment_id"] for row in rows}) == 1

    # Layer 3 — the corpus. One file for that one id, and no two files sharing
    # an id, which is the assertion that says "not corrupted" rather than
    # merely "not duplicated".
    fragments = _fragments(vault)
    ids = _fragment_ids(fragments)
    assert len(fragments) == 1, fragments
    assert len(set(ids)) == len(ids), ids
    assert set(ids) == {row["fragment_id"] for row in rows}
    assert all(_PLAIN_MARKER in path.read_bytes() for path in fragments)

    # The published counts add up to what actually landed, rather than telling
    # a caller that summed the two responses it got two fragments for one.
    handled = [
        int(payload["fragments_created"]) + int(payload["fragments_updated"])
        for payload in payloads
    ]
    assert sum(handled) == 1, payloads
    assert [payload["fragments_failed"] for payload in payloads] == [0, 0]
    assert [payload["files_failed"] for payload in payloads] == [0, 0]


def test_the_sync_lock_lives_beside_the_drive_ledger_it_protects(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock file is named here as well as derived there.

    Its location is not decoration: a lock somewhere else would not be found
    by a second *process* — the CLI, a second uvicorn worker — which is the
    whole point of it being a file rather than an object.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    listing = [_drive_file("id-1", "ridge.md")]
    _stubbed(monkeypatch, _StubDrive(listing, {"id-1": _PLAIN_BODY}))
    lock_path = vault / "00-Creek-Meta" / "State" / "ingest" / "gdrive.lock"
    assert not lock_path.exists()

    with client(vault_path=vault) as test_client:
        assert _sync(test_client).status_code == _OK_STATUS

    assert lock_path.exists()
    assert lock_path.parent == (vault / _GDRIVE_LEDGER_RELPATH).parent


def test_a_sync_that_cannot_take_the_lock_is_refused_rather_than_queued(
    vault: Path,
    configured: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that loses the race is told to retry, in the published vocabulary.

    ``503 temporarily_unavailable`` rather than a hung request: the work is
    off the event loop in a worker thread, so a sync that waited indefinitely
    would hold that thread while the request-timeout middleware answered
    without it. The refusal names nothing about the vault or the holder.

    Args:
        vault: A seeded vault.
        configured: The staging directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.
    """
    del configured
    _connect(token_path)
    stub = _StubDrive([_drive_file("id-1", "ridge.md")], {"id-1": _PLAIN_BODY})
    _stubbed(monkeypatch, stub)
    monkeypatch.setattr(drive_tools, "_SYNC_LOCK_TIMEOUT_SECONDS", 0.05)
    lock_path = vault / "00-Creek-Meta" / "State" / "ingest" / "gdrive.lock"

    with (
        vault_lock(lock_path, timeout=_HELD_LOCK_SECONDS),
        client(vault_path=vault) as test_client,
    ):
        response = _sync(test_client)

    assert response.status_code == _UNAVAILABLE_STATUS, response.text
    body = envelope(response)
    assert body["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value, body
    assert stub.downloads == []
    assert _fragments(vault) == []
    assert str(vault) not in response.text
