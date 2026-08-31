"""``/v1/connectors/drive/authorizations`` — connecting Drive remotely (#1568).

**RED at HEAD.** Before this issue the route table held eleven specs and no
path matching ``authorizations``; ``grep -rniE "def .*(authoriz|oauth|callback)"``
over ``creek_mcp`` and ``creek`` returned one unrelated hit; and the only way to
mint a Drive credential was ``creek gdrive --download`` on the host, whose
``InstalledAppFlow.run_local_server(port=0)`` opens a browser **on the server**.
Every HTTP test below answers ``404`` at that commit.

The design is ADR-0012's option C, and the three things it could get wrong are
the three this module is organised around:

1. **The client secret leaking.** Option C exists precisely so the secret stays
   on this host. Both responses are closed models, and the sweep here looks for
   a sentinel in the *emitted bytes* of the response, the audit trail and the
   published OpenAPI rather than reasoning about which paths could emit it.
2. **A state that is not really single-use.** ``state`` binds a code to an
   authorization this server issued. If a consumed or expired one still worked,
   the binding would be decorative. Mutation-checked: deleting the consume step
   turns the replay test red.
3. **A refusal becoming an oracle.** Neither route reads the token file, so no
   refusal can disclose whether a credential already exists — asserted as
   byte-identity between the credential-present and credential-absent runs
   rather than as a claim about the code.

Acceptance criterion 5 of #1568 — "everything fetched still flows through the
same tier machinery" — needs no test here and gets none: this issue mints a
credential and opens no fetch path. The criterion is already pinned by
``tests/test_v1_api_drive.py::test_a_synced_fragment_carries_the_tier_creek_ingest_would_give_it``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlparse

import pytest

from creek.config import _READONLY_SCOPES
from creek_mcp.api.models import (
    CONTRACT_MINOR,
    SUPPORTED_CONTRACT_MINORS,
    Capability,
    DriveConnectionState,
    ErrorCode,
)
from creek_mcp.api.openapi import build_openapi
from creek_mcp.api.routes import ROUTES
from creek_mcp.httpapi import drive_grant as drive_grant_module
from creek_mcp.httpapi.drive_grant import grant_refusal_code
from creek_mcp.tools import drive_grant as grant_tools
from tests.v1_api_support import (
    DRIVE_AUTHORIZATION_PATH,
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
"""A served grant call."""

_UNAVAILABLE_STATUS: Final[int] = 503
"""Both grant refusals; they differ by ``code``, not by status."""

_INVALID_STATUS: Final[int] = 422
"""A body this contract cannot parse."""

_INTERNAL_ERROR_STATUS: Final[int] = 500
"""A success this contract cannot express; never a partially-assembled 200."""

_GRANT_MINOR: Final[str] = "0.11"
"""The minor the grant is published at, restated from ADR-0012."""

_PREVIOUS_MINOR: Final[str] = "0.10"
"""The newest minor that predates the grant; still served for everything else."""

_DRIVE_CAPABILITY_MINOR: Final[str] = "0.9"
"""The minor ``drive-connector`` was published at, and therefore gated on."""

_BEFORE_DRIVE_MINOR: Final[str] = "0.8"
"""The newest minor that predates the capability; still served everything else."""

_INCOMPATIBLE_STATUS: Final[int] = 409
"""What a client below ``drive-connector``'s minor is answered on both routes."""

# Low-entropy test literals shaped like Google client-secret material, never
# real credentials. Spelled as concatenations so a secret scanner reads them as
# constructed strings, matching ``tests/v1_api_support.py``'s token literals.
_CLIENT_SECRET_SENTINEL: Final[str] = "GOCSPX-unit-test-web-secret-" + "q" * 20
_CLIENT_ID_SENTINEL: Final[str] = "1234567890-unit-test.apps.googleusercontent.com"
_REFRESH_SENTINEL: Final[str] = "1//0g-unit-test-grant-refresh-" + "w" * 20

_REDIRECT_URI: Final[str] = "https://adepthood.example/connectors/drive/return"
"""A redirect URI the *caller* owns. Option C never mounts one on this server."""

_AUTH_ENDPOINT: Final[str] = "https://accounts.google.com/o/oauth2/auth"
"""Where a real web-client authorization URL points."""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Yield a freshly seeded vault.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The vault root.
    """
    return seed_vault(tmp_path)


def _write_web_client_secrets(path: Path) -> None:
    """Write a Google **web** client secrets file at *path*.

    The shape Google's console downloads for a web application, which is what
    ADR-0012 requires: the ``web`` key rather than ``installed``.

    Args:
        path: Where to write it.
    """
    path.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": _CLIENT_ID_SENTINEL,
                    "client_secret": _CLIENT_SECRET_SENTINEL,
                    "auth_uri": _AUTH_ENDPOINT,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    """Return where the cached credential would live.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The token-file path. It deliberately does not exist yet.
    """
    return tmp_path / "token.json"


@pytest.fixture
def drive_config(
    tmp_path: Path,
    token_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point ``load_config`` at a Drive configuration with a web client.

    Steers the real ``CREEK_CONFIG`` resolution the connector tools use, rather
    than substituting the config object, so the tools read what a deployment
    would.

    Args:
        tmp_path: pytest's per-test directory.
        token_path: Where the cached credential lives.
        monkeypatch: The active monkeypatch fixture.

    Returns:
        The client-secrets path the config names.
    """
    credentials = tmp_path / "credentials.json"
    _write_web_client_secrets(credentials)
    config = tmp_path / "creek_config.yaml"
    config.write_text(
        "source_drive: " + str(tmp_path) + "\n"
        "google_drive:\n"
        "  credentials_file: " + str(credentials) + "\n"
        "  token_file: " + str(token_path) + "\n"
        "  staging_dir: drive-staging\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREEK_CONFIG", str(config))
    return credentials


class _StubFlow:
    """The three members of ``google_auth_oauthlib.flow.Flow`` the grant uses.

    Only the exchange leg is stubbed in tests that use this: building an
    authorization URL is a pure local computation and
    ``test_the_authorization_url_is_built_by_the_real_google_flow`` runs the
    real library for it.
    """

    def __init__(self, credentials_json: str) -> None:
        """Record what ``fetch_token`` will yield.

        Args:
            credentials_json: The serialised credential the exchange produces.
        """
        self._credentials_json = credentials_json
        self.codes: list[str] = []

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        """Return a URL echoing the state it was given.

        Args:
            **kwargs: The real flow's keyword arguments; ``state`` is read.

        Returns:
            The ``(url, state)`` pair the real library returns.
        """
        state = str(kwargs["state"])
        return f"{_AUTH_ENDPOINT}?state={state}", state

    def fetch_token(self, *, code: str) -> None:
        """Record *code* instead of exchanging it over the network.

        Args:
            code: The authorization code presented.
        """
        self.codes.append(code)

    @property
    def credentials(self) -> _StubCredentials:
        """Return the credential ``fetch_token`` would have minted.

        Returns:
            A stand-in exposing only ``to_json``.
        """
        return _StubCredentials(self._credentials_json)


class _StubCredentials:
    """The single member of ``google.oauth2.credentials.Credentials`` used."""

    def __init__(self, payload: str) -> None:
        """Hold the serialised credential.

        Args:
            payload: The JSON text ``to_json`` returns.
        """
        self._payload = payload

    def to_json(self) -> str:
        """Return the serialised credential.

        Returns:
            The JSON text the token file is written from.
        """
        return self._payload


def _credential_json() -> str:
    """Return a serialised credential in the shape ``Credentials`` writes.

    Returns:
        JSON carrying the refresh-token sentinel, so a leak is greppable.
    """
    return json.dumps(
        {
            "token": _REFRESH_SENTINEL,
            "refresh_token": _REFRESH_SENTINEL,
            "client_id": _CLIENT_ID_SENTINEL,
            "client_secret": _CLIENT_SECRET_SENTINEL,
            "scopes": sorted(_READONLY_SCOPES),
        }
    )


@pytest.fixture
def stub_flow(monkeypatch: pytest.MonkeyPatch) -> _StubFlow:
    """Substitute the single OAuth-flow construction site.

    Patched on :func:`creek_mcp.tools.drive_grant.build_flow` — the one seam —
    so the tools take no ``flow`` argument and there is no alternative code
    path a test could exercise instead of production's.

    Args:
        monkeypatch: The active monkeypatch fixture.

    Returns:
        The stub every grant call in the test will be served by.
    """
    flow = _StubFlow(_credential_json())
    monkeypatch.setattr(
        grant_tools,
        "build_flow",
        lambda **_kwargs: flow,
    )
    return flow


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _authorize(
    test_client: TestClient,
    *,
    redirect_uri: str = _REDIRECT_URI,
    **kwargs: Any,
) -> httpx.Response:
    """Begin an authorization.

    Args:
        test_client: The client under test.
        redirect_uri: The caller-owned redirect URI to request.
        **kwargs: Header overrides for :func:`headers`.

    Returns:
        The raw response.
    """
    return test_client.post(
        DRIVE_AUTHORIZATION_PATH,
        json={"redirect_uri": redirect_uri},
        headers=headers(**kwargs),
    )


def _exchange(
    test_client: TestClient,
    state: str,
    *,
    code: str = "unit-test-authorization-code",
    **kwargs: Any,
) -> httpx.Response:
    """Complete an authorization by relaying *code*.

    Args:
        test_client: The client under test.
        state: The state the authorization was issued under.
        code: The authorization code to relay.
        **kwargs: Header overrides for :func:`headers`.

    Returns:
        The raw response.
    """
    return test_client.post(
        f"{DRIVE_AUTHORIZATION_PATH}/{state}",
        json={"code": code},
        headers=headers(**kwargs),
    )


def _issued_state(test_client: TestClient) -> str:
    """Begin an authorization and return the state it minted.

    Args:
        test_client: The client under test.

    Returns:
        The ``state`` value the server issued.
    """
    response = _authorize(test_client)
    assert response.status_code == _OK_STATUS, response.text
    return str(response.json()["state"])


# --------------------------------------------------------------------------- #
# 1. The contract: two routes, no new capability name
# --------------------------------------------------------------------------- #


def test_the_grant_is_published_under_the_existing_drive_connector_capability() -> None:
    """Both grant routes negotiate as ``drive-connector``, not a new name.

    The reasoning ``Capability.DRIVE_CONNECTOR`` gives for keeping status and
    sync together applies verbatim to connecting it: a consumer cannot usefully
    negotiate "may I sync" apart from "may I connect", and a server advertising
    sync-without-connect would be advertising half a connector.
    """
    grant_specs = [spec for spec in ROUTES if "authorizations" in spec.path]
    assert len(grant_specs) == 2, "the two grant routes are not both published"
    assert {spec.capability for spec in grant_specs} == {Capability.DRIVE_CONNECTOR}


def test_the_contract_minor_moved_because_the_route_set_widened() -> None:
    """Contract ``0.11`` ships the grant, and ``0.10`` is still served.

    Additive, exactly as ``upload`` (0.8), ``drive-connector`` (0.9) and
    ``pipeline`` (0.10) were: a client pinned below 0.11 keeps everything it
    had.
    """
    assert CONTRACT_MINOR == _GRANT_MINOR
    assert _PREVIOUS_MINOR in SUPPORTED_CONTRACT_MINORS


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_both_grant_routes_are_gated_on_the_capabilitys_own_minor(
    vault: Path,
) -> None:
    """A client below ``drive-connector``'s minor is ``409``ed; one at it is served.

    The gate is per **capability**, not per route, and the grant deliberately
    introduces no new capability name — so the minor it is served from is
    ``drive-connector``'s own ``0.9``, not the ``0.11`` at which the route set
    widened. Both halves are asserted, because only the second one proves the
    gate is not simply refusing everything.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        below = _authorize(test_client, minor=_BEFORE_DRIVE_MINOR)
        below_complete = _exchange(test_client, "anything", minor=_BEFORE_DRIVE_MINOR)
        served = _authorize(test_client, minor=_DRIVE_CAPABILITY_MINOR)
    assert below.status_code == _INCOMPATIBLE_STATUS
    assert below_complete.status_code == _INCOMPATIBLE_STATUS
    assert served.status_code == _OK_STATUS, served.text


# --------------------------------------------------------------------------- #
# 2. The happy path
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_beginning_an_authorization_returns_a_url_and_a_state(vault: Path) -> None:
    """``POST .../authorizations`` answers ``{authorization_url, state}``.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _OK_STATUS, response.text
    body = response.json()
    assert body["authorization_url"].startswith(_AUTH_ENDPOINT)
    assert body["state"]
    assert set(body) == {"status", "tier_ceiling", "authorization_url", "state"}


@pytest.mark.usefixtures("drive_config")
def test_the_authorization_url_is_built_by_the_real_google_flow(vault: Path) -> None:
    """No stub: the real ``google_auth_oauthlib`` builds the URL.

    Building an authorization URL is a pure local computation — the network is
    not touched until the code is exchanged — so this leg is testable against
    the real library, and is. The assertions are the parts a consumer's browser
    depends on: Google's own endpoint, the caller-owned ``redirect_uri``, and
    the issued ``state``.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _OK_STATUS, response.text
    body = response.json()
    url = str(body["authorization_url"])
    assert url.startswith(_AUTH_ENDPOINT)
    assert "adepthood.example" in url
    assert f"state={body['state']}" in url
    assert "drive.readonly" in url


@pytest.mark.usefixtures("drive_config")
def test_the_authorization_url_asks_for_a_renewable_grant_it_cannot_widen(
    vault: Path,
) -> None:
    """The three grant switches are on the URL the real library built.

    None of them shows up in ``scope``, so the scope assertions elsewhere in
    this module cannot see them:

    * ``access_type=offline`` is what makes Google issue a refresh token. Drop
      it and the credential dies in an hour and no unattended sync runs again.
    * ``prompt=consent`` is what makes Google issue one *again* on a
      re-authorisation, rather than an access token with nothing to renew it.
    * ``include_granted_scopes=false`` is Google's incremental-authorisation
      switch. Set to ``true`` it merges every scope the user previously
      granted this client into the new token — a write scope reaching a
      credential this route minted, without the ``scope`` parameter ever
      naming one, and therefore behind
      ``test_a_remote_grant_cannot_request_a_write_scope``'s back.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _OK_STATUS, response.text
    query = parse_qs(urlparse(str(response.json()["authorization_url"])).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["include_granted_scopes"] == ["false"]


@pytest.mark.usefixtures("drive_config")
def test_the_exchange_presents_the_verifier_the_url_committed_to(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PKCE spans both legs, so the verifier crosses the two requests.

    ``google_auth_oauthlib`` puts a ``code_challenge`` in every authorization
    URL it builds and sends the matching ``code_verifier`` at exchange time.
    The two legs are two ``Flow`` objects in two requests, so unless the
    verifier is carried across with the ``state``, the second flow presents
    nothing and Google refuses **every real authorization** with
    ``invalid_grant`` — a total failure of the feature that a stubbed exchange
    cannot see, because the stub never checks the verifier.

    Asserted as the RFC 7636 relation itself, ``S256(verifier) == challenge``,
    rather than as "some verifier was passed": a fresh verifier would satisfy
    the weaker claim and still be rejected by Google.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    captured: dict[str, Any] = {}

    def _capturing_build_flow(**kwargs: Any) -> _StubFlow:
        """Record the exchange leg's flow arguments and serve a stub.

        Args:
            **kwargs: What :func:`creek.ingest.gdrive_grant.build_flow` was
                called with.

        Returns:
            The stub standing in for the real flow.
        """
        captured.update(kwargs)
        return _StubFlow(_credential_json())

    with client(vault_path=vault) as test_client:
        begun = _authorize(test_client)
        assert begun.status_code == _OK_STATUS, begun.text
        body = begun.json()
        monkeypatch.setattr(grant_tools, "build_flow", _capturing_build_flow)
        completed = _exchange(test_client, str(body["state"]))
    assert completed.status_code == _OK_STATUS, completed.text
    query = parse_qs(urlparse(str(body["authorization_url"])).query)
    digest = hashlib.sha256(str(captured["code_verifier"]).encode("ascii")).digest()
    presented = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert query["code_challenge"] == [presented]
    assert query["code_challenge_method"] == ["S256"]


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_the_pending_authorization_store_is_written_owner_only(vault: Path) -> None:
    """The store lands at ``0o600``, and holds the verifier rather than a code.

    On its own the stored verifier buys nothing; paired with an intercepted
    authorization code it is half of a redemption, which is why the file is
    owner-only from byte zero rather than after a ``chmod``.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        _authorize(test_client)
    store = grant_tools._store_path(vault)
    assert store.is_file(), "no pending-authorization store was written"
    assert store.stat().st_mode & 0o077 == 0
    entries = json.loads(store.read_text(encoding="utf-8"))
    assert [sorted(entry) for entry in entries.values()] == [
        ["code_verifier", "expires_at", "redirect_uri"]
    ]


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_a_store_entry_this_module_never_wrote_is_dropped_not_raised(
    vault: Path,
) -> None:
    """A hand-edited expiry refuses one exchange; it does not brick the connector.

    ``_read_store`` is read defensively so a corrupt file cannot make the
    connector permanently unauthorisable. Converting an ``expires_at`` the
    writer never produced used to raise straight out of the tool and through
    the adapter as a ``500`` — and, because *every* subsequent call reads the
    same file, it stayed that way until someone with shell access deleted it,
    which is the exact outcome the defensive read exists to prevent.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        state = _issued_state(test_client)
        store = grant_tools._store_path(vault)
        entries = json.loads(store.read_text(encoding="utf-8"))
        entries[state]["expires_at"] = "whenever"
        store.write_text(json.dumps(entries), encoding="utf-8")
        refused = _exchange(test_client, state)
        recovered = _authorize(test_client)
    assert refused.status_code == _UNAVAILABLE_STATUS, refused.text
    assert envelope(refused)["code"] == ErrorCode.UNAVAILABLE.value
    assert recovered.status_code == _OK_STATUS, recovered.text


@pytest.mark.usefixtures("drive_config")
def test_completing_an_authorization_writes_the_credential(
    vault: Path,
    token_path: Path,
    stub_flow: _StubFlow,
) -> None:
    """The relayed code becomes a cached credential and a connected connector.

    The load-bearing end-to-end assertion: the file on disk is what
    ``creek gdrive`` would have written, so ``POST /v1/connectors/drive/syncs``
    can use it without knowing which route minted it.

    Args:
        vault: A seeded vault.
        token_path: Where the cached credential lands.
        stub_flow: The substituted OAuth flow.
    """
    with client(vault_path=vault) as test_client:
        state = _issued_state(test_client)
        response = _exchange(test_client, state, code="the-real-code")
    assert response.status_code == _OK_STATUS, response.text
    assert stub_flow.codes == ["the-real-code"]
    assert token_path.is_file()
    assert json.loads(token_path.read_text(encoding="utf-8"))["refresh_token"]
    assert response.json()["connection"] == DriveConnectionState.CONNECTED.value


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_the_cached_credential_is_written_owner_only(
    vault: Path,
    token_path: Path,
) -> None:
    """The token file lands at ``0o600``, as ``_write_token_file`` guarantees.

    Args:
        vault: A seeded vault.
        token_path: Where the cached credential lands.
    """
    with client(vault_path=vault) as test_client:
        _exchange(test_client, _issued_state(test_client))
    assert token_path.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------- #
# 3. No credential material crosses the wire (AC #3)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_the_authorization_response_carries_no_client_secret(vault: Path) -> None:
    """The client secret is nowhere in the begun-authorization bytes.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert _CLIENT_SECRET_SENTINEL not in response.text


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_the_exchange_response_carries_no_token_and_no_refresh_token(
    vault: Path,
) -> None:
    """The completed-authorization body carries state, never credentials.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = _exchange(test_client, _issued_state(test_client))
    assert response.status_code == _OK_STATUS, response.text
    assert _REFRESH_SENTINEL not in response.text
    assert _CLIENT_SECRET_SENTINEL not in response.text


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_no_credential_material_reaches_the_audit_trail(vault: Path) -> None:
    """Neither the code, the secret nor the credential is persisted in the log.

    The audit line is a permanent record on the vault owner's disk; a
    credential copied into it survives every rotation.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        _exchange(test_client, _issued_state(test_client), code="secret-code-1568")
    log = vault / "00-Creek-Meta" / "audit" / "mcp.jsonl"
    text = log.read_text(encoding="utf-8") if log.is_file() else ""
    assert _REFRESH_SENTINEL not in text
    assert _CLIENT_SECRET_SENTINEL not in text
    assert "secret-code-1568" not in text


def test_the_published_openapi_names_no_credential_field() -> None:
    """No grant schema publishes a token, secret or credential field."""
    document = json.dumps(build_openapi())
    for schema in ("DriveAuthorizationRequest", "DriveAuthorizationExchangeRequest"):
        assert schema in document, f"{schema} is not published"
    grant_schemas = build_openapi()["components"]["schemas"]
    fields = {
        name
        for schema in ("DriveAuthorizationRequest", "DriveAuthorizationResponse")
        for name in grant_schemas[schema].get("properties", {})
    }
    assert not fields & {"client_secret", "token", "refresh_token", "credentials"}


# --------------------------------------------------------------------------- #
# 4. State is single-use, bound and expiring (AC #2)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_a_consumed_state_cannot_be_replayed(vault: Path, token_path: Path) -> None:
    """A second exchange on the same state is refused, and writes nothing.

    Args:
        vault: A seeded vault.
        token_path: Where the cached credential lands.
    """
    with client(vault_path=vault) as test_client:
        state = _issued_state(test_client)
        first = _exchange(test_client, state)
        token_path.unlink()
        second = _exchange(test_client, state)
    assert first.status_code == _OK_STATUS, first.text
    assert second.status_code == _UNAVAILABLE_STATUS
    assert not token_path.exists(), "a replayed state minted a second credential"


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_an_unknown_state_is_refused_identically_to_a_replayed_one(
    vault: Path,
) -> None:
    """The two refusals are byte-identical but for the correlation id.

    A caller that could tell "I never issued that" from "you already used
    that" would learn which states this server has outstanding.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        state = _issued_state(test_client)
        _exchange(test_client, state)
        replayed = _exchange(test_client, state)
        unknown = _exchange(test_client, "a-state-this-server-never-issued")
    assert replayed.status_code == unknown.status_code
    replayed_body = envelope(replayed)
    unknown_body = envelope(unknown)
    del replayed_body["request_id"], unknown_body["request_id"]
    assert replayed_body == unknown_body


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_an_expired_state_is_refused(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state presented after its window closed no longer exchanges.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    monkeypatch.setattr(grant_tools, "STATE_TTL_SECONDS", 0.0)
    with client(vault_path=vault) as test_client:
        state = _issued_state(test_client)
        response = _exchange(test_client, state)
    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_the_state_is_high_entropy(vault: Path) -> None:
    """Two authorizations mint different, long, url-safe states.

    A guessable state would let a caller present a code against an
    authorization it never began.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        first = _issued_state(test_client)
        second = _issued_state(test_client)
    assert first != second
    assert len(first) >= grant_tools.STATE_BYTES
    assert set(first) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )


# --------------------------------------------------------------------------- #
# 5. A refusal is not an oracle (AC #4)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_a_refusal_does_not_reveal_whether_a_credential_already_exists(
    vault: Path,
    token_path: Path,
) -> None:
    """The same refusal, connected or not.

    Neither grant route reads the token file, so credential presence cannot
    reach a refusal. Asserted over the emitted bytes rather than claimed.

    Args:
        vault: A seeded vault.
        token_path: Where the cached credential would live.
    """
    with client(vault_path=vault) as test_client:
        disconnected = _exchange(test_client, "never-issued")
        token_path.write_text(_credential_json(), encoding="utf-8")
        connected = _exchange(test_client, "never-issued")
    assert disconnected.status_code == connected.status_code
    left, right = envelope(disconnected), envelope(connected)
    del left["request_id"], right["request_id"]
    assert left == right


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_a_failed_exchange_is_the_same_refusal_as_an_unknown_state(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google rejecting the code reads exactly like a state this server forgot.

    The exchange failure carries Google's own error text, which names the
    client and the request; none of it may be narrated to the caller.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """

    class _RefusingFlow(_StubFlow):
        """A flow whose token exchange fails the way Google's does."""

        def fetch_token(self, *, code: str) -> None:
            """Refuse *code* with Google's own error shape.

            Args:
                code: The authorization code presented.

            Raises:
                ValueError: Always, carrying material that must not be echoed.
            """
            msg = f"invalid_grant for client {_CLIENT_ID_SENTINEL} ({code})"
            raise ValueError(msg)

    refusing = _RefusingFlow(_credential_json())
    with client(vault_path=vault) as test_client:
        state = _issued_state(test_client)
        monkeypatch.setattr(grant_tools, "build_flow", lambda **_kwargs: refusing)
        failed = _exchange(test_client, state)
        unknown = _exchange(test_client, "never-issued")
    assert failed.status_code == unknown.status_code
    assert _CLIENT_ID_SENTINEL not in failed.text
    left, right = envelope(failed), envelope(unknown)
    del left["request_id"], right["request_id"]
    assert left == right


# --------------------------------------------------------------------------- #
# 6. Scopes route through the config validator
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_a_remote_grant_cannot_request_a_write_scope(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A widened scope list refuses rather than becoming an authorization URL.

    ``GoogleDriveConfig.validate_readonly_scopes`` guards the *config* path. A
    remote connect button is the one place a write scope could be requested
    without a config edit, so the grant re-runs that validator on the list it
    is about to send. The test bypasses pydantic exactly as a bug would — by
    handing the grant a scope list nothing validated.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    monkeypatch.setattr(
        grant_tools,
        "_configured_scopes",
        lambda _drive: ["https://www.googleapis.com/auth/drive"],
    )
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _UNAVAILABLE_STATUS
    assert "drive.readonly" not in response.text


@pytest.mark.usefixtures("drive_config")
def test_the_authorization_requests_exactly_the_configured_readonly_scope(
    vault: Path,
) -> None:
    """The URL's ``scope`` parameter equals the read-only allowlist, exactly.

    Not "contains ``drive.readonly``" — equality, so an authorization that
    asked for the read-only scope *and* a write one would fail here. Read off
    the real library's URL, with no stub in the way.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _OK_STATUS, response.text
    query = parse_qs(urlparse(str(response.json()["authorization_url"])).query)
    assert set(query["scope"][0].split()) == set(_READONLY_SCOPES)


# --------------------------------------------------------------------------- #
# 7. The redirect URI belongs to the caller, and must be one
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://adepthood.example/return",
        "ftp://adepthood.example/return",
        "https://adepthood.example/return#fragment",
        "not-a-uri",
        "",
    ],
    ids=["plain-http", "wrong-scheme", "fragment", "not-a-uri", "empty"],
)
@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_an_unusable_redirect_uri_is_refused(vault: Path, redirect_uri: str) -> None:
    """Only an absolute ``https`` URI without a fragment is accepted.

    OAuth 2.0 forbids a fragment on a redirect URI, and a plain-``http`` one
    would carry the authorization code over the network in the clear.

    Args:
        vault: A seeded vault.
        redirect_uri: The rejected value.
    """
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client, redirect_uri=redirect_uri)
    assert response.status_code == _INVALID_STATUS, response.text


@pytest.mark.usefixtures("drive_config", "stub_flow")
def test_the_exchange_reuses_the_redirect_uri_the_state_was_issued_for(
    vault: Path,
) -> None:
    """The code is exchanged against the stored URI, never a re-supplied one.

    The exchange body carries a ``code`` and nothing else: a second
    ``redirect_uri`` on the wire would be a value the caller could vary between
    the two legs, which is exactly what the stored one exists to prevent.

    Args:
        vault: A seeded vault.
    """
    exchange_spec = next(
        spec for spec in ROUTES if spec.path.endswith("/authorizations/{state}")
    )
    assert exchange_spec.request_model is not None
    assert set(exchange_spec.request_model.model_fields) == {"code"}
    with client(vault_path=vault) as test_client:
        response = _exchange(test_client, _issued_state(test_client))
    assert response.status_code == _OK_STATUS, response.text


# --------------------------------------------------------------------------- #
# 8. Operator-actionable refusals
# --------------------------------------------------------------------------- #


def test_a_missing_client_secrets_file_refuses_with_unavailable(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``credentials.json`` means ``unavailable``, not a server fault.

    ``unavailable`` rather than ``temporarily_unavailable`` on purpose: no
    backoff clears a missing file, and :data:`~creek_mcp.api.models.RETRY_POLICY`
    is what tells the client to surface it to a human instead of retrying.

    Args:
        vault: A seeded vault.
        tmp_path: pytest's per-test directory.
        monkeypatch: The active monkeypatch fixture.
    """
    config = tmp_path / "creek_config.yaml"
    config.write_text(
        "source_drive: " + str(tmp_path) + "\n"
        "google_drive:\n"
        "  credentials_file: " + str(tmp_path / "absent.json") + "\n"
        "  token_file: " + str(tmp_path / "token.json") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CREEK_CONFIG", str(config))
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.UNAVAILABLE.value
    assert str(tmp_path) not in response.text


# --------------------------------------------------------------------------- #
# 9. The adapter's own edges
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reason",
    [
        grant_tools.GRANT_UNAVAILABLE_REASON,
        grant_tools.GRANT_REFUSED_REASON,
    ],
    ids=["operator-actionable", "flat"],
)
def test_both_published_refusal_reasons_project_to_unavailable(reason: str) -> None:
    """The two reasons the grant tools return are the one published code.

    Identical on purpose. An unknown, expired, consumed or Google-refused
    authorization must be indistinguishable from a missing client secrets file
    at the wire, because the alternative narrates which authorizations this
    server has outstanding.

    Args:
        reason: One of the two published refusal reasons.
    """
    assert grant_refusal_code(reason) is ErrorCode.UNAVAILABLE


def test_an_unrecognised_reason_fails_closed_to_a_server_fault() -> None:
    """A reason this adapter cannot classify is not translated, it is refused.

    The fallthrough is load-bearing rather than defensive: a future refusal
    carrying Google's own error text would otherwise be handed to the caller
    as a plausible-sounding ``unavailable`` it would act on.
    """
    assert grant_refusal_code("something no tool here returns") is (
        ErrorCode.INTERNAL_ERROR
    )


@pytest.mark.usefixtures("drive_config", "stub_flow")
@pytest.mark.parametrize("complete", [False, True], ids=["begin", "complete"])
def test_an_unreadable_vault_is_unavailable_and_is_not_scaffolded(
    tmp_path: Path,
    complete: bool,
) -> None:
    """No vault means ``503``, and the directory is left exactly as found.

    The probe runs *before* either tool, because both append to the audit log
    and an audit append creates its parent directories — so an unprobed call
    against a missing vault would scaffold one from the network, and the
    begin-authorization verb would additionally leave a state store behind in
    it.

    Args:
        tmp_path: pytest's per-test directory.
        complete: Whether to drive the completing verb rather than the
            beginning one.
    """
    absent = tmp_path / "not-a-vault"
    absent.mkdir()
    with client(vault_path=absent) as test_client:
        response = (
            _exchange(test_client, "anything") if complete else _authorize(test_client)
        )
    assert response.status_code == _UNAVAILABLE_STATUS
    assert list(absent.iterdir()) == []


@pytest.mark.usefixtures("drive_config")
def test_a_success_the_contract_cannot_express_is_a_server_fault(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``ok`` in an unpublishable shape becomes ``500``, never a partial ``200``.

    Unreachable through the real tool, which is the point: the projection
    catches a key the tool did not set or a ``tier_ceiling`` the wire enum
    cannot name, rather than emitting a body assembled from whatever was
    there.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    monkeypatch.setattr(
        drive_grant_module,
        "drive_authorize_tool",
        lambda **_kwargs: {"status": "ok", "tier_ceiling": "intimate"},
    )
    with client(vault_path=vault) as test_client:
        response = _authorize(test_client)
    assert response.status_code == _INTERNAL_ERROR_STATUS
    assert envelope(response)["code"] == ErrorCode.INTERNAL_ERROR.value
